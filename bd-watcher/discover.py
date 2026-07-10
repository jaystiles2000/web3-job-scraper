"""ATS discovery — one-off command run via GitHub Actions workflow_dispatch.

For each company with ats: null (or ats: none whose re-probe is due),
generate slug candidates and probe Greenhouse, Lever, Ashby in turn.
First hit wins. Writes discovered ats + ats_id back to companies.yaml.

Companies with no ATS found are marked ats: none with today's date on
last_none_probe so we don't hammer them daily.

Run:
  python bd-watcher/discover.py
Env:
  BD_DISCOVER_ALL=1  -> also re-probe every 'none' company regardless of
                       when it was last probed. Use sparingly.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from core import (
    ATS_PROBES, log, load_companies, load_config, load_state, save_companies,
    save_state, should_reprobe_none, slug_candidates,
)


def probe_company(name: str, delay: float) -> tuple[str | None, str | None]:
    """Return (ats, ats_id) if found, else (None, None). Politely spaced."""
    for slug in slug_candidates(name):
        for ats, probe in ATS_PROBES.items():
            time.sleep(delay)
            try:
                result = probe(slug)
            except Exception as e:
                log(f"  probe {ats}/{slug} raised: {e}")
                continue
            if result is not None:
                # Guard: many ATS installs return an empty list for
                # inactive / private boards. Empty is OK — the slug
                # is still correct — but we log it.
                log(f"  ✅ {name}: {ats}/{slug} ({len(result)} jobs)")
                return ats, slug
    return None, None


def run() -> None:
    config = load_config()
    delay = float(config.get("request_delay_seconds", 0.4))
    reprobe_days = int(config.get("none_reprobe_days", 30))

    companies = load_companies()
    state = load_state()

    force_all = os.environ.get("BD_DISCOVER_ALL") == "1"
    counters = {"greenhouse": 0, "lever": 0, "ashby": 0, "none": 0, "already": 0}

    for c in companies:
        ats = (c.get("ats") or "").lower()
        if ats and ats != "none":
            counters["already"] += 1
            continue
        if ats == "none" and not force_all:
            if not should_reprobe_none(c, state, reprobe_days):
                counters["none"] += 1
                continue

        log(f"Discovering: {c['name']}")
        found_ats, found_id = probe_company(c["name"], delay)
        if found_ats:
            c["ats"] = found_ats
            c["ats_id"] = found_id
            counters[found_ats] += 1
            # Clear the none-probe timestamp if we recovered from `none`.
            state["last_none_probe"].pop(c["name"], None)
        else:
            c["ats"] = "none"
            c["ats_id"] = None
            state["last_none_probe"][c["name"]] = datetime.now().strftime("%Y-%m-%d")
            counters["none"] += 1

    save_companies(companies)
    save_state(state)

    log(
        f"Done. greenhouse={counters['greenhouse']} lever={counters['lever']} "
        f"ashby={counters['ashby']} none={counters['none']} "
        f"already={counters['already']}"
    )
    total_hits = counters["greenhouse"] + counters["lever"] + counters["ashby"]
    log(f"Total resolved: {total_hits} / {len(companies)} companies.")


if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    run()
