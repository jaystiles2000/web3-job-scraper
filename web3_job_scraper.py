#!/usr/bin/env python3
"""
Web3 Job Board Daily Scraper — v2
===================================
Scrapes web3/crypto job boards and surfaces only NEW listings each run.
Stores seen job IDs in seen_jobs.json to diff day-over-day.

Requirements:
    pip install requests beautifulsoup4 feedparser lxml
"""

import json
import re
import time
import argparse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import feedparser

SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"
REQUEST_DELAY  = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_JOBS_FILE.exists():
        with open(SEEN_JOBS_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f, indent=2)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get(url: str, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [WARN] {url}: {e}")
        return None

def soup(r) -> BeautifulSoup:
    return BeautifulSoup(r.text, "lxml")

def make_id(source: str, title: str, url: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]", "", (title + url).lower())
    return f"{source}::{slug[:80]}"

def clean(t: str) -> str:
    t = t.strip()
    t = re.sub(r"Read more about.*", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s+", " ", t)
    return t

# Titles and URL patterns that indicate nav/category links, not real jobs
JUNK_TITLES = {
    "post job", "post a job", "find a job", "find jobs", "all jobs",
    "companies", "sign up", "login", "log in", "for candidates",
    "for companies", "read more", "engineering", "design", "marketing",
    "sales", "operations", "non tech", "metaverse", "crypto", "gaming",
    "blockchain", "customer support", "entry level", "finance", "legal",
    "research", "devops", "rust", "java", "golang", "moderator",
    "web3 engineering jobs", "web3 design jobs", "web3 marketing jobs",
    "web3 sales jobs", "web3 operations jobs", "web3 other jobs",
    "public relations (pr)jobs", "customer supportjobs", "salesjobs",
    "marketingjobs", "human resources (hr)jobs", "technical writerjobs",
    "social media managerjobs", "quality assurance (qa)jobs",
    "front end developerjobs", "project managerjobs", "seojobs",
    "designjobs", "financejobs", "creative directorjobs",
    "customer success managerjobs", "copywritingjobs", "legaljobs",
    "researchjobs", "machine learning engineerjobs", "devopsjobs",
    "ux researcherjobs", "data sciencejobs", "ios developerjobs",
    "ui designerjobs", "javajobs", ".net developerjobs",
    "react developerjobs", "rustjobs", "cyber securityjobs",
    "user experience (ux)jobs", "digital marketingjobs", "moderatorjobs",
    "graphic designjobs", "data scientistjobs", "data analystjobs",
    "full stack developerjobs", "content writerjobs", "golangjobs",
    "software engineerjobs", "entry leveljobs",
}

BAD_URL_PATTERNS = [
    r"/jobs$", r"/jobs/$",
    r"/jobs/design$", r"/jobs/engineering$", r"/jobs/marketing$",
    r"/jobs/sales$", r"/jobs/crypto$", r"/jobs/gaming$",
    r"/jobs/\w+-jobs$",
    r"company/jobs/create",
    r"/post$", r"/post-job",
    r"#$",
]

def is_real_job(title: str, url: str) -> bool:
    if not title or len(title) < 5:
        return False
    if title.lower().strip() in JUNK_TITLES:
        return False
    for pat in BAD_URL_PATTERNS:
        if re.search(pat, url):
            return False
    return True

def loc_from_url(url: str) -> str:
    """CryptoJobsList embeds location in URL slug: /jobs/title-LOCATION-at-company"""
    m = re.search(r"/jobs/[^/]+-([a-z][a-z-]+)-at-[^/]+$", url)
    if m:
        raw = m.group(1).replace("-", " ").title()
        # Skip if it looks like a word, not a place
        if len(raw) > 3 and not raw.lower() in {"remote", "global", "worldwide"}:
            return raw
        elif raw.lower() in {"remote", "global", "worldwide"}:
            return "Remote"
    return ""

# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_ethereumjobboard() -> list[dict]:
    jobs, seen_urls = [], set()
    r = get("https://www.ethereumjobboard.com/jobs")
    if not r: return jobs
    for a in soup(r).select("a[href*='/jobs/']"):
        title = clean(a.get_text())
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.ethereumjobboard.com" + href
        if href in seen_urls: continue
        seen_urls.add(href)
        if is_real_job(title, href):
            jobs.append({"title": title, "url": href, "source": "EthereumJobBoard"})
    return jobs


def scrape_bitcoinerjobs() -> list[dict]:
    jobs = []
    for e in feedparser.parse("https://bitcoinerjobs.com/feed").entries:
        title = clean(e.title)
        if is_real_job(title, e.link):
            jobs.append({"title": title, "url": e.link, "source": "BitcoinerJobs"})
    return jobs


def scrape_talentweb3() -> list[dict]:
    jobs, seen_urls = [], set()
    r = get("https://talentweb3.careers-page.com/")
    if not r: return jobs
    for a in soup(r).select("a[href*='/jobs/']"):
        title = clean(a.get_text())
        href = a["href"]
        if not href.startswith("http"):
            href = "https://talentweb3.careers-page.com" + href
        if href in seen_urls: continue
        seen_urls.add(href)
        if is_real_job(title, href):
            jobs.append({"title": title, "url": href, "source": "TalentWeb3"})
    return jobs


def _getro(slug: str, display: str, base_url: str) -> list[dict]:
    """Shared scraper for Getro-powered boards — deduplicates URLs properly."""
    jobs, seen_urls = [], set()
    r = get(base_url)
    if not r: return jobs
    for a in soup(r).select("a[href*='/jobs/']"):
        title = clean(a.get_text())
        if "Read more" in title or not title: continue
        href = a["href"]
        if not href.startswith("http"):
            href = base_url.rstrip("/") + href
        # Normalise: strip #content anchor for dedup, keep full URL
        base_href = href.split("#")[0]
        if base_href in seen_urls: continue
        seen_urls.add(base_href)
        if is_real_job(title, href):
            jobs.append({"title": title, "url": href, "source": display,
                         "location": loc_from_url(href)})
    return jobs

def scrape_safary()      -> list[dict]: return _getro("safary",     "Safary",      "https://jobs.safary.club/jobs")
def scrape_solana_jobs() -> list[dict]: return _getro("solana",     "SolanaJobs",  "https://jobs.solana.com/jobs")
def scrape_a16z_crypto() -> list[dict]: return _getro("a16zcrypto", "a16z Crypto", "https://a16zcrypto.com/jobs/")
def scrape_bitkraft()    -> list[dict]: return _getro("bitkraft",   "BITKRAFT VC", "https://careers.bitkraft.vc/jobs")


def scrape_cryptojobslist() -> list[dict]:
    jobs, seen_urls = [], set()
    for e in feedparser.parse("https://cryptojobslist.com/rss").entries:
        title = clean(e.title)
        url = e.link
        if url in seen_urls: continue
        seen_urls.add(url)
        if is_real_job(title, url):
            jobs.append({"title": title, "url": url, "source": "CryptoJobsList",
                         "location": loc_from_url(url)})
    return jobs


def scrape_cryptocurrencyjobs() -> list[dict]:
    jobs = []
    for e in feedparser.parse("https://cryptocurrencyjobs.co/feed/").entries:
        title = clean(e.title)
        if not is_real_job(title, e.link): continue
        loc = ""
        summary = getattr(e, "summary", "")
        m = re.search(r"(Remote|[\w\s]+,\s*[\w\s]+)", summary)
        if m: loc = m.group(1).strip()[:40]
        jobs.append({"title": title, "url": e.link, "source": "CryptocurrencyJobs",
                     "location": loc})
    return jobs


def scrape_myweb3jobs() -> list[dict]:
    jobs = []
    for e in feedparser.parse("https://myweb3jobs.com/feed/").entries:
        title = clean(e.title)
        if is_real_job(title, e.link):
            jobs.append({"title": title, "url": e.link, "source": "MyWeb3Jobs"})
    return jobs


def scrape_defi_jobs() -> list[dict]:
    jobs, seen_urls = [], set()
    r = get("https://www.defi.jobs/")
    if not r: return jobs
    for a in soup(r).select("a[href*='/jobs/']"):
        title = clean(a.get_text())
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.defi.jobs" + href
        if href in seen_urls: continue
        seen_urls.add(href)
        if is_real_job(title, href):
            jobs.append({"title": title, "url": href, "source": "DeFi.jobs"})
    return jobs


def scrape_hashtagweb3() -> list[dict]:
    """Skip internal nav, only capture links to real external job pages."""
    jobs, seen_urls = [], set()
    r = get("https://hashtagweb3.com/jobs")
    if not r: return jobs
    for a in soup(r).select("a[href]"):
        href = a["href"]
        if not href.startswith("http"): continue
        if "hashtagweb3.com" in href: continue   # skip all internal links
        title = clean(a.get_text())
        if href in seen_urls: continue
        seen_urls.add(href)
        if is_real_job(title, href):
            jobs.append({"title": title, "url": href, "source": "HashtagWeb3"})
    return jobs


def scrape_blockchainheadhunter() -> list[dict]:
    jobs, seen_urls = [], set()
    r = get("https://blockchainheadhunter.com/jobs")
    if not r: return jobs
    for a in soup(r).select("a[href*='/job/']"):
        title = clean(a.get_text())
        href = a["href"]
        if not href.startswith("http"):
            href = "https://blockchainheadhunter.com" + href
        if href in seen_urls: continue
        seen_urls.add(href)
        if is_real_job(title, href):
            jobs.append({"title": title, "url": href, "source": "BlockchainHeadhunter"})
    return jobs


def scrape_bitcoinjobs() -> list[dict]:
    jobs, seen_urls = [], set()
    r = get("https://bitcoinjobs.com/")
    if not r: return jobs
    for a in soup(r).select("a[href*='/job/']"):
        title = clean(a.get_text())
        href = a["href"]
        if not href.startswith("http"):
            href = "https://bitcoinjobs.com" + href
        if href in seen_urls: continue
        seen_urls.add(href)
        if is_real_job(title, href):
            jobs.append({"title": title, "url": href, "source": "BitcoinJobs"})
    return jobs


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SCRAPERS = [
    scrape_ethereumjobboard,
    scrape_bitcoinerjobs,
    scrape_talentweb3,
    scrape_safary,
    scrape_solana_jobs,
    scrape_a16z_crypto,
    scrape_bitkraft,
    scrape_cryptojobslist,
    scrape_cryptocurrencyjobs,
    scrape_myweb3jobs,
    scrape_defi_jobs,
    scrape_hashtagweb3,
    scrape_blockchainheadhunter,
    scrape_bitcoinjobs,
]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(reset: bool = False) -> list[dict]:
    seen = set() if reset else load_seen()
    all_new: list[dict] = []

    print(f"\n{'='*55}")
    print(f"  Web3 Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Seen jobs on record: {len(seen)}")
    print(f"{'='*55}\n")

    for fn in SCRAPERS:
        name = fn.__name__.replace("scrape_", "")
        print(f"→ {name}...")
        try:
            jobs = fn()
        except Exception as e:
            print(f"  [ERROR] {e}")
            jobs = []

        new = []
        for job in jobs:
            jid = make_id(job["source"], job["title"], job.get("url", ""))
            if jid not in seen:
                seen.add(jid)
                new.append(job)

        print(f"  {len(jobs)} found, {len(new)} new")
        all_new.extend(new)
        time.sleep(REQUEST_DELAY)

    save_seen(seen)

    # ---------------------------------------------------------------------------
    # Slack output
    # ---------------------------------------------------------------------------

    if not all_new:
        print(f"✅ *Web3 Jobs — {datetime.now().strftime('%d %b %Y')}*\nNo new jobs since last run.")
        return all_new

    by_source: dict[str, list] = {}
    for job in all_new:
        by_source.setdefault(job["source"], []).append(job)

    lines = [
        f":new: *Web3 Jobs — {datetime.now().strftime('%d %b %Y')}*",
        f"_{len(all_new)} new jobs across {len(by_source)} boards_",
    ]

    for source, jobs in sorted(by_source.items()):
        lines.append(f"\n*── {source} ({len(jobs)}) ──*")
        for job in jobs:
            loc = job.get("location", "").strip()
            sal = job.get("salary", "").strip()
            block = [f"• *{job['title']}*"]
            if loc:   block.append(f"  :round_pushpin: {loc}")
            if sal:   block.append(f"  :moneybag: {sal}")
            block.append(f"  :link: {job['url']}")
            lines.append("\n".join(block))

    print("\n".join(lines))
    return all_new


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    run(reset=args.reset)
