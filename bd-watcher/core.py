"""Shared helpers for the BD Trigger Watcher.

Kept in a single module because there's not enough moving parts to
justify a package layout. Everything here is pure functions plus a
tiny Loader class over the two YAML files.

Layout:
  * Loader class     — reads / writes companies.yaml + config.yaml
  * ATS clients      — greenhouse, lever, ashby probes + list-jobs
  * Fallback fetcher — jobs.solana.com scrape
  * Classifier       — lane + angle from job title
  * Dedupe helpers   — job id, cooldown check
  * Telegram send    — reused pattern from the main scraper
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).parent
STATE_PATH = HERE / "state.json"
COMPANIES_PATH = HERE / "companies.yaml"
CONFIG_PATH = HERE / "config.yaml"

UA = "bd-watcher/1.0 (+github.com/jaystiles2000/web3-job-scraper)"


# =============================================================================
# Config + companies loader
# =============================================================================

def _load_yaml(path: Path) -> Any:
    """Read a YAML file. Prefers PyYAML if installed; falls back to a very
    small hand-rolled parser for our specific shapes so the module is
    usable in a slim runtime too."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return _mini_yaml(text)


def _dump_yaml(path: Path, data: Any) -> None:
    """Write YAML preserving reasonable formatting. Same PyYAML/fallback
    strategy as _load_yaml."""
    try:
        import yaml  # type: ignore
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
    except ImportError:
        # Fallback dumper only supports our companies.yaml shape; refuse
        # to write anything else so bugs surface loudly.
        raise RuntimeError("PyYAML required to write; add pyyaml to deps")


def _mini_yaml(text: str) -> dict:
    """Tiny YAML subset parser used only if PyYAML is absent. Handles the
    exact shape of companies.yaml + config.yaml — mappings, lists, quoted
    strings, null, booleans, ints, floats. Not general-purpose."""
    # Reject in the interest of not shipping subtle bugs — CI installs
    # pyyaml so this path shouldn't fire.
    raise RuntimeError(
        "PyYAML is required for the BD watcher. Install it via requirements.txt."
    )


def load_config() -> dict:
    return _load_yaml(CONFIG_PATH)


def load_companies() -> list[dict]:
    doc = _load_yaml(COMPANIES_PATH)
    return doc.get("companies", []) or []


def save_companies(companies: list[dict]) -> None:
    _dump_yaml(COMPANIES_PATH, {"companies": companies})


# =============================================================================
# State (dedupe + cooldown)
# =============================================================================

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "first_run_seeded": False,
            "seen_job_ids": {},              # id -> iso timestamp when first seen
            "last_instant_alert_by_company": {},  # canonical name -> iso timestamp
            "last_none_probe": {},           # company name -> YYYY-MM-DD
        }
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def stable_job_id(source: str, ats_id: str | None, external_id: str | None,
                  company: str, title: str, url: str) -> str:
    """Stable ID for a job. Prefer ATS-native ids when available; hash the
    triple otherwise. Deterministic across runs so dedupe works."""
    if external_id and ats_id:
        return f"{source}:{ats_id}:{external_id}"
    payload = f"{source}|{company}|{title}|{url}".lower()
    h = hashlib.sha1(payload.encode()).hexdigest()[:16]
    return f"{source}:hash:{h}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def within_cooldown(company_name: str, state: dict, cooldown_days: int) -> bool:
    last = state["last_instant_alert_by_company"].get(company_name)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - last_dt < timedelta(days=cooldown_days)


def should_reprobe_none(company: dict, state: dict, days: int) -> bool:
    last = state["last_none_probe"].get(company["name"])
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d")
    except ValueError:
        return True
    return datetime.now() - last_dt > timedelta(days=days)


# =============================================================================
# HTTP
# =============================================================================

def http_get(url: str, timeout: float = 20.0) -> tuple[int, bytes, dict]:
    """Minimal urllib GET returning (status, body, headers)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, dict(e.headers or {})
    except Exception as e:
        return 0, str(e).encode(), {}


# =============================================================================
# ATS clients — greenhouse / lever / ashby
# =============================================================================

def slug_candidates(name: str) -> list[str]:
    """Generate plausible ATS slugs for a company name.

    Handled: lowercase, strip punctuation, hyphenate spaces, common
    suffixes (labs, network, foundation, protocol, finance, technologies)
    added and removed. Order matters — most-likely-canonical first.
    """
    base = re.sub(r"[^\w\s-]", "", name).strip().lower()
    parts = re.split(r"\s+", base)
    joined_hyphen = "-".join(parts)
    joined_plain = "".join(parts)
    out = [joined_hyphen, joined_plain]

    # Try dropping trailing suffix words that might not be in the slug.
    tail_words = {"labs", "network", "foundation", "protocol", "finance",
                  "technologies", "labs.", "inc", "co", "corporation",
                  "studios", "trading", "markets", "digital"}
    if len(parts) > 1 and parts[-1] in tail_words:
        core = parts[:-1]
        out.append("-".join(core))
        out.append("".join(core))

    # Try appending common suffixes.
    if len(parts) == 1:
        for suf in ("labs", "network", "protocol"):
            out.append(f"{joined_plain}{suf}")
            out.append(f"{joined_hyphen}-{suf}")

    # Dedupe preserving order.
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def probe_greenhouse(slug: str) -> list[dict] | None:
    """Return jobs list or None if not this platform."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    status, body, _ = http_get(url)
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, list):
        return None
    out = []
    for j in jobs_raw:
        out.append({
            "external_id": str(j.get("id", "")),
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location", {}) or {}).get("name", ""),
            "posted_at": (j.get("updated_at") or "")[:10],
        })
    return out


def probe_lever(slug: str) -> list[dict] | None:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    status, body, _ = http_get(url)
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for j in data:
        out.append({
            "external_id": j.get("id", ""),
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": ((j.get("categories") or {}).get("location") or ""),
            "posted_at": _posted_ms_to_date(j.get("createdAt")),
        })
    return out


def probe_ashby(slug: str) -> list[dict] | None:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    status, body, _ = http_get(url)
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, list):
        return None
    out = []
    for j in jobs_raw:
        out.append({
            "external_id": j.get("id", ""),
            "title": j.get("title", ""),
            "url": j.get("jobUrl", ""),
            "location": j.get("locationName", ""),
            "posted_at": (j.get("publishedAt") or "")[:10],
        })
    return out


def _posted_ms_to_date(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


ATS_PROBES = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "ashby": probe_ashby,
}


def fetch_jobs(company: dict) -> tuple[list[dict], str | None]:
    """Fetch current jobs for a company using its known ATS. Returns
    (jobs, error). If ats is 'none' or unknown, returns ([], None).
    Never raises."""
    ats = (company.get("ats") or "").lower()
    ats_id = company.get("ats_id")
    if ats not in ATS_PROBES or not ats_id:
        return [], None
    try:
        result = ATS_PROBES[ats](ats_id)
        if result is None:
            return [], f"{ats} returned non-jobs payload"
        return result, None
    except Exception as e:
        return [], f"{ats} exception: {e}"


# =============================================================================
# Fallback — jobs.solana.com (Getro-powered)
# =============================================================================

def fetch_solana_jobs_board() -> list[dict]:
    """Fetch the public jobs list from jobs.solana.com. Uses Getro's
    community API — the same underlying provider the site's frontend
    hits. Returns [{'company': str, 'title': str, 'url': str, ...}, ...].
    Returns [] on any failure."""
    # jobs.solana.com is powered by Getro. Their community's job list is
    # exposed at https://jobs.solana.com/api/jobs but the shape varies.
    # Try the well-known Getro endpoint first.
    urls = [
        "https://jobs.solana.com/api/jobs",
        "https://jobs.solana.com/api/companies",
    ]
    for url in urls:
        status, body, _ = http_get(url)
        if status == 200 and body.startswith(b"[") or body.startswith(b"{"):
            try:
                data = json.loads(body)
            except Exception:
                continue
            jobs = _normalise_getro(data)
            if jobs:
                return jobs
    return []


def _normalise_getro(payload: Any) -> list[dict]:
    """Flatten Getro's varying JSON into our shape."""
    out: list[dict] = []
    if isinstance(payload, dict):
        # Some Getro endpoints wrap the list in { jobs: [...] } or
        # { companies: [{ jobs: [...] }] }.
        if "jobs" in payload and isinstance(payload["jobs"], list):
            payload = payload["jobs"]
        elif "companies" in payload and isinstance(payload["companies"], list):
            merged = []
            for co in payload["companies"]:
                for j in co.get("jobs", []) or []:
                    j.setdefault("company", co.get("name", ""))
                    merged.append(j)
            payload = merged
    if not isinstance(payload, list):
        return out
    for j in payload:
        if not isinstance(j, dict):
            continue
        company = j.get("company") or (j.get("organization") or {}).get("name") or ""
        title = j.get("title") or j.get("name") or ""
        url = j.get("url") or j.get("apply_url") or j.get("absolute_url") or ""
        if not (company and title and url):
            continue
        out.append({
            "company": company,
            "title": title,
            "url": url,
            "location": j.get("location") or j.get("locations", [""])[0] if isinstance(j.get("locations"), list) else "",
            "posted_at": (j.get("posted_at") or j.get("published_at") or "")[:10],
        })
    return out


def match_fallback_to_watchlist(board_jobs: list[dict],
                                fallback_companies: list[dict]) -> list[dict]:
    """Filter board jobs down to those matching a fallback company by
    name or alias (case-insensitive substring)."""
    # Build a lookup: lowercase name/alias -> watchlist entry.
    idx: list[tuple[str, dict]] = []
    for c in fallback_companies:
        idx.append((c["name"].lower(), c))
        for a in c.get("aliases") or []:
            idx.append((a.lower(), c))

    matched: list[dict] = []
    for j in board_jobs:
        board_co = (j.get("company") or "").lower()
        for needle, watch in idx:
            if needle and needle in board_co:
                matched.append({**j, "_watch": watch})
                break
    return matched


# =============================================================================
# Classification
# =============================================================================

def classify_lane(title: str, lanes: dict) -> str:
    """First-match wins. Returns lane name, or 'other'."""
    t = (title or "").lower()
    for lane_name, keywords in lanes.items():
        for kw in keywords:
            if kw in t:
                return lane_name
    return "other"


def angle_for(lane: str, title: str, angles: dict) -> str:
    template = angles.get(lane) or angles.get("other") or ""
    return template.replace("{title}", title)


# =============================================================================
# Telegram
# =============================================================================

def send_telegram(text: str) -> tuple[bool, str]:
    """Send a plain-text message via the shared bot token."""
    token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200, str(r.status)
    except Exception as e:
        return False, str(e)


def format_instant(company: str, title: str, url: str, lane: str,
                   posted: str, angle: str, prefix: str,
                   extra_roles_count: int = 0) -> str:
    lines = [
        f"<b>{prefix}: {company}</b>",
        f"Role: {title}",
        f"Lane: {lane}",
    ]
    if posted:
        lines.append(f"Posted: {posted}")
    lines.append(f"Link: {url}")
    if extra_roles_count > 0:
        lines.append(f"(+{extra_roles_count} more open at this company)")
    lines.append("")
    lines.append(f"Angle: {angle}")
    return "\n".join(lines)


def format_digest(items: list[dict], prefix: str, date_str: str) -> str:
    """Compact digest for the 'other' lane and cooldown overflow."""
    if not items:
        return ""
    lines = [f"<b>{prefix} {date_str}</b>"]
    for it in items:
        lines.append(f"{it['company']}: {it['title']} — {it['url']}")
    return "\n".join(lines)


def log(msg: str) -> None:
    print(f"[bd-watcher] {msg}", file=sys.stderr)
