"""BD Trigger Watcher — scheduled runner.

Called twice daily by GitHub Actions. For each watchlist company:
  1. Fetch current jobs from its ATS (if known).
  2. Otherwise, if fallback board hasn't been fetched, fetch it once.
  3. Classify each job into a lane.
  4. Dedupe against state.json (never alert twice on the same job id).
  5. Apply per-company cooldown (max one instant alert per company per
     cooldown_days) — extra roles fold into the alert's "+N more" line
     or wait for the next window.
  6. First-run: seed state silently, alert on nothing.
  7. Send instant alerts + daily digest to Telegram.

Env:
  TELEGRAM_TOKEN / TELEGRAM_BOT_TOKEN  — bot auth
  TELEGRAM_CHAT_ID                     — target chat
  BD_WATCHER_DRY_RUN=1                 — parse + classify + log, no send
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core import (  # noqa: E402
    angle_for, classify_lane, fetch_jobs, fetch_solana_jobs_board,
    format_digest, format_instant, load_companies, load_config,
    load_state, log, match_fallback_to_watchlist, now_iso,
    save_state, send_telegram, stable_job_id, within_cooldown,
)


DRY = os.environ.get("BD_WATCHER_DRY_RUN") == "1"


def collect_from_ats(companies: list[dict], delay: float) -> tuple[list[dict], int, int]:
    """For every company with a known ATS, return (jobs, ok_count, fail_count).
    Each job dict has: company (canonical name from watchlist), title, url,
    location, posted_at, _source ('greenhouse'/'lever'/'ashby'), _watch (ref
    to the watchlist entry), _job_id."""
    jobs: list[dict] = []
    ok = fail = 0
    for c in companies:
        ats = (c.get("ats") or "").lower()
        if ats not in ("greenhouse", "lever", "ashby"):
            continue
        time.sleep(delay)
        listings, err = fetch_jobs(c)
        if err:
            log(f"  ⚠️  {c['name']} ({ats}/{c['ats_id']}): {err}")
            fail += 1
            continue
        ok += 1
        for j in listings:
            jobs.append({
                "company": c["name"],
                "title": j["title"],
                "url": j["url"],
                "location": j.get("location", ""),
                "posted_at": j.get("posted_at", ""),
                "_source": ats,
                "_watch": c,
                "_job_id": stable_job_id(ats, c["ats_id"], j.get("external_id"),
                                         c["name"], j["title"], j["url"]),
            })
    return jobs, ok, fail


def collect_from_fallback(companies: list[dict]) -> list[dict]:
    """Fetch jobs.solana.com and match against `ats: none` watchlist
    companies (or any company without a resolved ATS)."""
    fallback_targets = [
        c for c in companies
        if (c.get("ats") or "").lower() in ("", "none")
    ]
    if not fallback_targets:
        return []
    board = fetch_solana_jobs_board()
    if not board:
        log("  ⚠️  jobs.solana.com returned nothing (fallback disabled this run)")
        return []
    matched = match_fallback_to_watchlist(board, fallback_targets)
    out = []
    for m in matched:
        watch = m["_watch"]
        out.append({
            "company": watch["name"],
            "title": m["title"],
            "url": m["url"],
            "location": m.get("location", ""),
            "posted_at": m.get("posted_at", ""),
            "_source": "solana-board",
            "_watch": watch,
            "_job_id": stable_job_id("solana-board", None, None,
                                     watch["name"], m["title"], m["url"]),
        })
    return out


def run() -> int:
    config = load_config()
    companies = load_companies()
    state = load_state()

    delay = float(config.get("request_delay_seconds", 0.4))
    cooldown_days = int(config.get("cooldown_days", 7))
    max_fail_ratio = float(config.get("max_ats_failure_ratio", 0.25))
    instant_lanes = set(config.get("instant_lanes") or [])
    digest_lanes = set(config.get("digest_lanes") or [])
    lanes_cfg = config.get("lanes") or {}
    angles_cfg = config.get("angles") or {}
    prefix_instant = config.get("prefix_instant", "🎯 BD TRIGGER")
    prefix_digest = config.get("prefix_digest", "📋 BD DIGEST")
    prefix_warning = config.get("prefix_warning", "⚠️ BD Watcher warning")

    log(f"Loaded {len(companies)} companies. dry_run={DRY}")

    ats_jobs, ok, fail = collect_from_ats(companies, delay)
    fallback_jobs = collect_from_fallback(companies)
    all_jobs = ats_jobs + fallback_jobs
    log(f"Fetched jobs: ats={len(ats_jobs)} (ok={ok} fail={fail}) fallback={len(fallback_jobs)}")

    # ATS failure warning
    total_probed = ok + fail
    if total_probed > 0 and (fail / total_probed) > max_fail_ratio:
        msg = (
            f"<b>{prefix_warning}</b>\n"
            f"{fail} of {total_probed} ATS calls failed this run "
            f"({int(100 * fail / total_probed)}%). Investigate."
        )
        if not DRY:
            send_telegram(msg)
        log(msg.replace("<b>", "").replace("</b>", ""))

    # =========================
    # First-run seed
    # =========================
    if not state.get("first_run_seeded"):
        for j in all_jobs:
            state["seen_job_ids"][j["_job_id"]] = now_iso()
        state["first_run_seeded"] = True
        save_state(state)
        log(f"First-run seeded silently with {len(all_jobs)} jobs. No alerts sent.")
        return 0

    # =========================
    # Dedupe + classify
    # =========================
    seen_ids = state["seen_job_ids"]
    fresh = [j for j in all_jobs if j["_job_id"] not in seen_ids]
    for j in fresh:
        j["_lane"] = classify_lane(j["title"], lanes_cfg)
    log(f"New (undeduped): {len(fresh)} jobs")

    # =========================
    # Group by company for cooldown
    # =========================
    by_company: dict[str, list[dict]] = {}
    for j in fresh:
        by_company.setdefault(j["company"], []).append(j)

    instant_alerts: list[dict] = []
    digest_items: list[dict] = []

    for company_name, jobs_here in by_company.items():
        # Priority filter — 'normal' priority companies skip weekend runs.
        # Weekday check uses UTC (Actions is UTC anyway).
        priority = ((jobs_here[0]["_watch"].get("priority") or "normal").lower())
        is_weekend = datetime.now(timezone.utc).weekday() >= 5
        if is_weekend and priority != "high":
            for j in jobs_here:
                digest_items.append(j)  # still mark seen; roll into digest
            continue

        cooldown_active = within_cooldown(company_name, state, cooldown_days)

        # Prefer to send an instant alert on the newest instant-lane role
        # for the company. Everything else at that company folds into
        # the "+N more" note or the digest.
        instant_candidates = [j for j in jobs_here if j.get("_lane") in instant_lanes]
        other_here = [j for j in jobs_here if j.get("_lane") not in instant_lanes]

        if instant_candidates and not cooldown_active:
            hero = instant_candidates[0]
            hero["_extra_count"] = len(jobs_here) - 1  # everything else at this co
            instant_alerts.append(hero)
            # Remaining jobs at this company go into the digest silently.
            for j in jobs_here:
                if j is hero:
                    continue
                if j.get("_lane") in digest_lanes:
                    digest_items.append(j)
        else:
            # Either no instant-lane role OR company is cooling down.
            # Route everything into the digest.
            for j in jobs_here:
                if j.get("_lane") in digest_lanes or cooldown_active:
                    digest_items.append(j)
                # instant-lane roles hitting cooldown are dropped from
                # THIS run but their ID is marked seen below so they
                # don't come back tomorrow.

    # =========================
    # Send instant alerts
    # =========================
    for j in instant_alerts:
        text = format_instant(
            company=j["company"],
            title=j["title"],
            url=j["url"],
            lane=j["_lane"],
            posted=j.get("posted_at", ""),
            angle=angle_for(j["_lane"], j["title"], angles_cfg),
            prefix=prefix_instant,
            extra_roles_count=j.get("_extra_count", 0),
        )
        if DRY:
            log("--- DRY instant ---\n" + text)
        else:
            ok_send, err = send_telegram(text)
            if not ok_send:
                log(f"  Telegram send failed: {err}")
                continue
            time.sleep(0.5)  # polite spacing
        state["last_instant_alert_by_company"][j["company"]] = now_iso()

    # =========================
    # Send digest (only at afternoon run — but we don't know which run
    # this is; the workflow can decide. Here we send if any items).
    # =========================
    if digest_items:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        digest_text = format_digest(digest_items, prefix_digest, date_str)
        if DRY:
            log("--- DRY digest ---\n" + digest_text)
        else:
            send_telegram(digest_text)

    # =========================
    # Mark everything fresh as seen (even dropped-for-cooldown items)
    # =========================
    for j in fresh:
        seen_ids[j["_job_id"]] = now_iso()

    save_state(state)
    log(f"Done. instant={len(instant_alerts)} digest={len(digest_items)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
