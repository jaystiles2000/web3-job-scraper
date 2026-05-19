#!/usr/bin/env python3
"""
Discover which patch companies have public Greenhouse / Ashby / Lever
job boards, by probing slug variations of each company name against each
platform's public API.

Run once after adding new companies to the patch list, then paste the
output into web3_job_scraper.py under CRYPTO_COMPANIES_GREENHOUSE etc.

Usage:
    python tools/discover_patch_company_boards.py path/to/apply-all-companies.sql

The script:
1. Parses company names from the SQL tuples in apply-all-companies.sql
2. Generates candidate slugs per company (lowercase, hyphenated, with
   common suffixes stripped: "labs", "inc", "the", "&")
3. Concurrently probes each slug against:
     - Greenhouse:  https://boards-api.greenhouse.io/v1/boards/<slug>/jobs
     - Ashby:       https://api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true
     - Lever:       https://api.lever.co/v0/postings/<slug>?mode=json
4. A slug is "working" if HTTP 200 and the JSON has a non-empty
   jobs/postings array (otherwise the board exists but has zero open
   roles, which is fine for now too — we'll print it).
5. Prints a sorted Python literal you can paste into the scraper.
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

# Words we strip from the end before slugifying — they're suffixes that
# usually aren't in the ATS slug.
SUFFIX_NOISE = {
    "labs", "inc", "ltd", "limited", "corp", "co", "the",
    "foundation", "io", "xyz", "ag", "gmbh", "sa",
}

# Common slug variations for a company name. The first is the "ideal"
# (lowercase, hyphenated, no noise) and the rest are fallbacks.
def candidate_slugs(name: str) -> list[str]:
    n = name.strip().lower()
    # Pick the first half of "X / Y" entries (e.g. "Phoenix / Ellipsis Labs")
    n = re.split(r"\s*[/|]\s*", n)[0]
    # Remove punctuation
    n = re.sub(r"[\'\"`]", "", n)
    n = re.sub(r"[^a-z0-9\s-]", " ", n)
    tokens = [t for t in n.split() if t]
    # Drop trailing suffix noise like "labs", "inc"
    while tokens and tokens[-1] in SUFFIX_NOISE:
        tokens.pop()
    if not tokens:
        return []

    base = "-".join(tokens)             # phoenix-ellipsis
    collapsed = "".join(tokens)         # phoenixellipsis
    first = tokens[0]                    # phoenix
    first_two = "-".join(tokens[:2])    # phoenix-ellipsis (same as base for 2-tok)

    out: list[str] = []
    for slug in (base, collapsed, first, first_two):
        if slug and slug not in out:
            out.append(slug)
    return out


# ---------------------------------------------------------------------------
# Probes (return None if not found, str(slug) if found)
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PatchSlugDiscovery/1.0)",
    "Accept": "application/json,text/html;q=0.9",
}
TIMEOUT = 8


def probe_greenhouse(slug: str) -> dict | None:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    jobs = data.get("jobs") or []
    return {"platform": "greenhouse", "slug": slug, "jobs": len(jobs)}


def probe_ashby(slug: str) -> dict | None:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    jobs = data.get("jobs") or []
    return {"platform": "ashby", "slug": slug, "jobs": len(jobs)}


def probe_lever(slug: str) -> dict | None:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return {"platform": "lever", "slug": slug, "jobs": len(data)}


PROBES = (probe_greenhouse, probe_ashby, probe_lever)


def find_board_for(name: str) -> dict | None:
    """Try each slug × each platform until something hits. First win wins."""
    for slug in candidate_slugs(name):
        for probe in PROBES:
            found = probe(slug)
            if found is not None:
                found["name"] = name
                return found
    return None


# ---------------------------------------------------------------------------
# SQL parse
# ---------------------------------------------------------------------------

# Match the first quoted string in a tuple line. e.g.
#   ('Phoenix / Ellipsis Labs', 'solana', ...
NAME_RE = re.compile(r"^\s*\(\s*'([^']+)'", re.MULTILINE)


def parse_company_names(sql_text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in NAME_RE.finditer(sql_text):
        name = m.group(1).replace("''", "'").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} path/to/apply-all-companies.sql", file=sys.stderr)
        return 2
    sql_path = Path(sys.argv[1])
    if not sql_path.exists():
        print(f"SQL file not found: {sql_path}", file=sys.stderr)
        return 2

    names = parse_company_names(sql_path.read_text(encoding="utf-8"))
    print(f"Parsed {len(names)} unique company names from {sql_path.name}", file=sys.stderr)

    found: list[dict] = []
    not_found: list[str] = []

    # Concurrent probes. Each company runs sequentially across its own
    # slugs+probes, but companies run in parallel up to 8 at a time so
    # the whole run finishes in minutes rather than an hour.
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_to_name = {ex.submit(find_board_for, n): n for n in names}
        for i, fut in enumerate(as_completed(future_to_name), 1):
            name = future_to_name[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"  [{i}/{len(names)}] {name!r}: ERROR {e}", file=sys.stderr)
                not_found.append(name)
                continue
            if res is None:
                not_found.append(name)
                print(f"  [{i}/{len(names)}] {name!r}: no public board found", file=sys.stderr)
            else:
                found.append(res)
                print(
                    f"  [{i}/{len(names)}] {name!r}: {res['platform']}/{res['slug']} "
                    f"({res['jobs']} jobs)",
                    file=sys.stderr,
                )

    # Sort + group by platform for clean paste-in output
    found.sort(key=lambda r: (r["platform"], r["name"].lower()))

    by_platform: dict[str, list[dict]] = {}
    for r in found:
        by_platform.setdefault(r["platform"], []).append(r)

    print()
    print(f"# Found {len(found)} working boards out of {len(names)} companies.")
    print(f"# Couldn't auto-discover: {len(not_found)} (custom site or not public).")
    print()

    if "greenhouse" in by_platform:
        print("# Add to CRYPTO_COMPANIES_GREENHOUSE:")
        for r in by_platform["greenhouse"]:
            print(f"    ({r['name']!r:<40s}, {r['slug']!r:<28s}),  # {r['jobs']} jobs")
        print()

    if "ashby" in by_platform:
        print("# Add to CRYPTO_COMPANIES_ASHBY:")
        for r in by_platform["ashby"]:
            print(f"    ({r['name']!r:<40s}, {r['slug']!r:<28s}),  # {r['jobs']} jobs")
        print()

    if "lever" in by_platform:
        print("# Add to CRYPTO_COMPANIES_LEVER (new — needs scrape_company_lever helper):")
        for r in by_platform["lever"]:
            print(f"    ({r['name']!r:<40s}, {r['slug']!r:<28s}),  # {r['jobs']} jobs")
        print()

    if not_found:
        print(f"# Not auto-discovered ({len(not_found)}):")
        for name in not_found:
            print(f"#   - {name}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
