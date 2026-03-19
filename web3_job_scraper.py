#!/usr/bin/env python3
"""
Web3 Job Board Daily Scraper
=============================
Scrapes ~25 web3/crypto job boards and surfaces only NEW listings each run.
Stores seen job IDs in a local JSON file (seen_jobs.json) to diff day-over-day.

Usage:
    python web3_job_scraper.py                  # Print new jobs to terminal
    python web3_job_scraper.py --output email   # Format for email
    python web3_job_scraper.py --output slack   # Format for Slack webhook
    python web3_job_scraper.py --reset          # Clear seen jobs (fresh start)

Schedule (cron example – runs 8am daily):
    0 8 * * * cd /path/to/script && python web3_job_scraper.py >> scraper.log 2>&1

Requirements:
    pip install requests beautifulsoup4 feedparser lxml
"""

import json
import os
import time
import argparse
import re
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import feedparser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"
REQUEST_DELAY = 1.5  # seconds between requests, be polite

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
# Seen-jobs store
# ---------------------------------------------------------------------------

def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        with open(SEEN_JOBS_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen_jobs(seen: set):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f, indent=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get(url: str, timeout=15) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [WARN] Failed to fetch {url}: {e}")
        return None


def soup(response: requests.Response) -> BeautifulSoup:
    return BeautifulSoup(response.text, "lxml")


def make_id(source: str, title: str, url: str = "") -> str:
    """Create a stable dedup key."""
    slug = re.sub(r"[^a-z0-9]", "", (title + url).lower())
    return f"{source}::{slug[:80]}"


# ---------------------------------------------------------------------------
# Scrapers — one function per site (or platform group)
# ---------------------------------------------------------------------------

def scrape_ethereumjobboard() -> list[dict]:
    """ethereumjobboard.com — plain HTML listing"""
    jobs = []
    r = get("https://www.ethereumjobboard.com/jobs")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        href = "https://www.ethereumjobboard.com" + a["href"] if a["href"].startswith("/") else a["href"]
        jobs.append({"title": title, "url": href, "source": "EthereumJobBoard"})
    return jobs


def scrape_bitcoinerjobs() -> list[dict]:
    """bitcoinerjobs.com — Niceboard platform, try RSS then HTML fallback"""
    jobs = []
    # Niceboard boards expose RSS at /feed
    feed = feedparser.parse("https://bitcoinerjobs.com/feed")
    if feed.entries:
        for e in feed.entries:
            jobs.append({"title": e.title, "url": e.link, "source": "BitcoinerJobs"})
        return jobs
    # HTML fallback
    r = get("https://bitcoinerjobs.com/")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            jobs.append({"title": title, "url": a["href"], "source": "BitcoinerJobs"})
    return jobs


def scrape_talentweb3() -> list[dict]:
    """talentweb3.careers-page.com — server-rendered HTML with job links"""
    jobs = []
    r = get("https://talentweb3.careers-page.com/")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = "https://talentweb3.careers-page.com" + a["href"] if a["href"].startswith("/") else a["href"]
            jobs.append({"title": title, "url": href, "source": "TalentWeb3"})
    return jobs


def scrape_hirechain_base() -> list[dict]:
    """base.hirechain.io — Hirechain network, HTML shell + API"""
    jobs = []
    # Try their public API endpoint (Hirechain uses a REST API under the hood)
    r = get("https://app.hirechain.io/api/v1/ecosystems/b8fcbc03-25d8-4620-92d2-70e0de06fb39/jobs?limit=50")
    if r:
        try:
            data = r.json()
            for job in data.get("jobs", data.get("data", [])):
                title = job.get("title", "")
                url = job.get("url") or job.get("applyUrl") or "https://base.hirechain.io/"
                if title:
                    jobs.append({"title": title, "url": url, "source": "Base/Hirechain"})
            return jobs
        except Exception:
            pass
    # HTML fallback (shell only — jobs likely need JS; return empty gracefully)
    print("  [INFO] base.hirechain.io: JS-rendered, API attempt failed — skipping")
    return jobs


def scrape_safary() -> list[dict]:
    """jobs.safary.club — Getro-powered board"""
    return scrape_getro_board(
        api_slug="safary",
        display_name="Safary",
        fallback_url="https://jobs.safary.club/jobs"
    )


def scrape_bondex() -> list[dict]:
    """network.bondex.app/jobs — React SPA, limited HTML"""
    # Bondex is a heavily JS-rendered SPA — no public API found.
    # Best effort: fetch and parse any static job data injected in the HTML.
    jobs = []
    r = get("https://network.bondex.app/jobs")
    if not r:
        return jobs
    # Look for job data in JSON embedded in the page
    matches = re.findall(r'"title"\s*:\s*"([^"]{5,100})".*?"url"\s*:\s*"(https?://[^"]+)"', r.text)
    for title, url in matches:
        jobs.append({"title": title, "url": url, "source": "Bondex"})
    if not jobs:
        print("  [INFO] Bondex: JS-rendered SPA, no static job data found — consider Playwright")
    return jobs


def scrape_solana_jobs() -> list[dict]:
    """jobs.solana.com — Getro-powered"""
    return scrape_getro_board(
        api_slug="solana",
        display_name="SolanaJobs",
        fallback_url="https://jobs.solana.com/jobs"
    )


def scrape_cryptojobs_com() -> list[dict]:
    """cryptojobs.com — HTML job listing"""
    jobs = []
    r = get("https://www.cryptojobs.com/jobs")
    if not r:
        return jobs
    s = soup(r)
    for card in s.select("a[href*='/jobs/']"):
        title = card.get_text(strip=True)
        if title and len(title) > 5:
            href = card["href"]
            if href.startswith("/"):
                href = "https://www.cryptojobs.com" + href
            jobs.append({"title": title, "url": href, "source": "CryptoJobs.com"})
    return jobs


def scrape_crypto_jobs() -> list[dict]:
    """crypto.jobs — try RSS then HTML"""
    jobs = []
    feed = feedparser.parse("https://crypto.jobs/feed")
    if feed.entries:
        for e in feed.entries:
            jobs.append({"title": e.title, "url": e.link, "source": "Crypto.jobs"})
        return jobs
    r = get("https://crypto.jobs/jobs")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = a["href"] if a["href"].startswith("http") else "https://crypto.jobs" + a["href"]
            jobs.append({"title": title, "url": href, "source": "Crypto.jobs"})
    return jobs


def scrape_cryptojobslist() -> list[dict]:
    """cryptojobslist.com — try RSS feed"""
    jobs = []
    feed = feedparser.parse("https://cryptojobslist.com/rss")
    if feed.entries:
        for e in feed.entries:
            jobs.append({"title": e.title, "url": e.link, "source": "CryptoJobsList"})
        return jobs
    r = get("https://cryptojobslist.com/")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href^='/'][href*='-']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            jobs.append({"title": title, "url": "https://cryptojobslist.com" + a["href"], "source": "CryptoJobsList"})
    return jobs


def scrape_cryptocurrencyjobs() -> list[dict]:
    """cryptocurrencyjobs.co — has RSS feed"""
    jobs = []
    feed = feedparser.parse("https://cryptocurrencyjobs.co/feed/")
    if feed.entries:
        for e in feed.entries:
            jobs.append({"title": e.title, "url": e.link, "source": "CryptocurrencyJobs"})
        return jobs
    r = get("https://cryptocurrencyjobs.co/")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            jobs.append({"title": title, "url": a["href"], "source": "CryptocurrencyJobs"})
    return jobs


def scrape_blockchainheadhunter() -> list[dict]:
    """blockchainheadhunter.com — HTML listing"""
    jobs = []
    r = get("https://blockchainheadhunter.com/jobs")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/job']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = a["href"] if a["href"].startswith("http") else "https://blockchainheadhunter.com" + a["href"]
            jobs.append({"title": title, "url": href, "source": "BlockchainHeadhunter"})
    return jobs


def scrape_web3_career() -> list[dict]:
    """web3.career — HTML, has public API"""
    jobs = []
    # Try their JSON API first
    r = get("https://web3.career/api/jobs?page=1")
    if r:
        try:
            data = r.json()
            for job in data.get("jobs", []):
                title = job.get("title", "")
                slug = job.get("slug", "")
                if title:
                    jobs.append({
                        "title": title,
                        "url": f"https://web3.career/{slug}" if slug else "https://web3.career/",
                        "source": "Web3.career"
                    })
            if jobs:
                return jobs
        except Exception:
            pass
    # HTML fallback
    r = get("https://web3.career/")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href^='/'][class*='job']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            jobs.append({"title": title, "url": "https://web3.career" + a["href"], "source": "Web3.career"})
    return jobs


def scrape_remote3() -> list[dict]:
    """remote3.co — HTML"""
    jobs = []
    r = get("https://www.remote3.co/remote-web3-jobs")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/web3-job/'], a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = a["href"] if a["href"].startswith("http") else "https://www.remote3.co" + a["href"]
            jobs.append({"title": title, "url": href, "source": "Remote3"})
    return jobs


def scrape_findweb3() -> list[dict]:
    """findweb3.com — Next.js, try JSON data endpoint"""
    jobs = []
    r = get("https://findweb3.com/api/jobs?limit=50")
    if r:
        try:
            data = r.json()
            for job in data.get("jobs", data if isinstance(data, list) else []):
                title = job.get("title", "")
                slug = job.get("slug", job.get("id", ""))
                if title:
                    jobs.append({
                        "title": title,
                        "url": f"https://findweb3.com/jobs/{slug}",
                        "source": "FindWeb3"
                    })
            if jobs:
                return jobs
        except Exception:
            pass
    # HTML fallback
    r = get("https://findweb3.com/jobs")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = a["href"] if a["href"].startswith("http") else "https://findweb3.com" + a["href"]
            jobs.append({"title": title, "url": href, "source": "FindWeb3"})
    return jobs


def scrape_defi_jobs() -> list[dict]:
    """defi.jobs — Webflow, jobs in HTML"""
    jobs = []
    r = get("https://www.defi.jobs/")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a.job-link, a[href*='defi.jobs/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = a["href"] if a["href"].startswith("http") else "https://www.defi.jobs" + a["href"]
            jobs.append({"title": title, "url": href, "source": "DeFi.jobs"})
    return jobs


def scrape_getro_web3() -> list[dict]:
    """getro.com/web3 — 403 blocked, skip"""
    print("  [INFO] getro.com/web3: 403 blocked — skipping")
    return []


def scrape_hashtagweb3() -> list[dict]:
    """hashtagweb3.com — HTML job board"""
    jobs = []
    r = get("https://hashtagweb3.com/jobs")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/job/'], a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = a["href"] if a["href"].startswith("http") else "https://hashtagweb3.com" + a["href"]
            jobs.append({"title": title, "url": href, "source": "HashtagWeb3"})
    return jobs


def scrape_a16z_crypto() -> list[dict]:
    """a16zcrypto.com/jobs — Getro-powered"""
    return scrape_getro_board(
        api_slug="a16zcrypto",
        display_name="a16z Crypto Jobs",
        fallback_url="https://a16zcrypto.com/jobs/"
    )


def scrape_myweb3jobs() -> list[dict]:
    """myweb3jobs.com — WordPress, has RSS"""
    jobs = []
    feed = feedparser.parse("https://myweb3jobs.com/feed/")
    if feed.entries:
        for e in feed.entries:
            jobs.append({"title": e.title, "url": e.link, "source": "MyWeb3Jobs"})
        return jobs
    r = get("https://myweb3jobs.com/")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/job/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            jobs.append({"title": title, "url": a["href"], "source": "MyWeb3Jobs"})
    return jobs


def scrape_bitcoinjobs() -> list[dict]:
    """bitcoinjobs.com — static HTML, clean listing"""
    jobs = []
    r = get("https://bitcoinjobs.com/")
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/job/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = a["href"] if a["href"].startswith("http") else "https://bitcoinjobs.com" + a["href"]
            jobs.append({"title": title, "url": href, "source": "BitcoinJobs"})
    return jobs


def scrape_crypto_careers() -> list[dict]:
    """crypto-careers.com — 403 blocked"""
    print("  [INFO] crypto-careers.com: 403 blocked — skipping")
    return []


def scrape_bitkraft() -> list[dict]:
    """careers.bitkraft.vc — Getro-powered"""
    return scrape_getro_board(
        api_slug="bitkraft",
        display_name="BITKRAFT VC",
        fallback_url="https://careers.bitkraft.vc/jobs"
    )


# ---------------------------------------------------------------------------
# Getro platform helper (covers solana, a16z, bitkraft, safary)
# ---------------------------------------------------------------------------

def scrape_getro_board(api_slug: str, display_name: str, fallback_url: str) -> list[dict]:
    """
    Getro powers several boards and exposes a public JSON API.
    Endpoint: https://api.getro.com/v2/networks/{slug}/jobs
    """
    jobs = []
    api_url = f"https://api.getro.com/v2/networks/{api_slug}/jobs?per_page=50&page=1"
    r = get(api_url)
    if r:
        try:
            data = r.json()
            for job in data.get("jobs", []):
                title = job.get("title", "")
                url = job.get("url") or job.get("apply_url") or fallback_url
                if title:
                    jobs.append({"title": title, "url": url, "source": display_name})
            if jobs:
                return jobs
        except Exception:
            pass
    # HTML fallback
    r = get(fallback_url)
    if not r:
        return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        if title and len(title) > 5:
            href = a["href"] if a["href"].startswith("http") else fallback_url.rstrip("/") + a["href"]
            jobs.append({"title": title, "url": href, "source": display_name})
    return jobs


# ---------------------------------------------------------------------------
# All scrapers registry
# ---------------------------------------------------------------------------

SCRAPERS = [
    scrape_ethereumjobboard,
    scrape_bitcoinerjobs,
    scrape_talentweb3,
    scrape_hirechain_base,
    scrape_safary,
    scrape_bondex,
    scrape_solana_jobs,
    scrape_cryptojobs_com,
    scrape_crypto_jobs,
    scrape_cryptojobslist,
    scrape_cryptocurrencyjobs,
    scrape_blockchainheadhunter,
    scrape_web3_career,
    scrape_remote3,
    scrape_findweb3,
    scrape_defi_jobs,
    scrape_getro_web3,
    scrape_hashtagweb3,
    scrape_a16z_crypto,
    scrape_myweb3jobs,
    scrape_bitcoinjobs,
    scrape_crypto_careers,
    scrape_bitkraft,
]


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(reset: bool = False, output: str = "terminal") -> list[dict]:
    seen = set() if reset else load_seen_jobs()
    all_new_jobs = []

    print(f"\n{'='*60}")
    print(f"  Web3 Job Board Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Seen jobs on record: {len(seen)}")
    print(f"{'='*60}\n")

    for scraper_fn in SCRAPERS:
        name = scraper_fn.__name__.replace("scrape_", "")
        print(f"→ {name}...")
        try:
            jobs = scraper_fn()
        except Exception as e:
            print(f"  [ERROR] {e}")
            jobs = []

        new_jobs = []
        for job in jobs:
            job_id = make_id(job["source"], job["title"], job.get("url", ""))
            if job_id not in seen:
                seen.add(job_id)
                new_jobs.append(job)

        print(f"  Found {len(jobs)} jobs, {len(new_jobs)} new")
        all_new_jobs.extend(new_jobs)
        time.sleep(REQUEST_DELAY)

    save_seen_jobs(seen)

    # ---------------------------------------------------------------------------
    # Output
    # ---------------------------------------------------------------------------

    if not all_new_jobs:
        print("\n✅ No new jobs since last run.")
        return all_new_jobs

    print(f"\n{'='*60}")
    print(f"  🆕 {len(all_new_jobs)} NEW JOBS FOUND")
    print(f"{'='*60}\n")

    # Group by source
    by_source: dict[str, list[dict]] = {}
    for job in all_new_jobs:
        by_source.setdefault(job["source"], []).append(job)

    if output == "terminal":
        for source, jobs in sorted(by_source.items()):
            print(f"\n📌 {source} ({len(jobs)} new)")
            for j in jobs:
                print(f"   • {j['title']}")
                print(f"     {j['url']}")

    elif output == "email":
        lines = [f"Web3 Jobs Digest — {datetime.now().strftime('%d %b %Y')}", ""]
        lines.append(f"{len(all_new_jobs)} new jobs across {len(by_source)} boards\n")
        for source, jobs in sorted(by_source.items()):
            lines.append(f"\n{source} ({l#!/usr/bin/env python3
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
en(jobs)} new)")#!/usr/bin/env python3
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

            lines.append("-" * 40)
            for j in jobs:
                lines.append(f"  {j['title']}")
                lines.append(f"  {j['url']}\n")
        print("\n".join(lines))

    elif output == "slack":
        # Slack-formatted markdown (mrkdwn)
        lines = [f"*🆕 Web3 Jobs Digest — {datetime.now().strftime('%d %b %Y')}*"]
        lines.append(f"_{len(all_new_jobs)} new jobs across {len(by_source)} boards_\n")
        for source, jobs in sorted(by_source.items()):
            lines.append(f"\n*{source}* ({len(jobs)} new)")
            for j in jobs:
                lines.append(f"  • <{j['url']}|{j['title']}>")
        print("\n".join(lines))

    return all_new_jobs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Web3 Job Board Daily Scraper")
    parser.add_argument("--output", choices=["terminal", "email", "slack"], default="terminal")
    parser.add_argument("--reset", action="store_true", help="Clear seen jobs and start fresh")
    args = parser.parse_args()
    run(reset=args.reset, output=args.output)
