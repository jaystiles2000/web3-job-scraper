#!/usr/bin/env python3
"""
Web3 Job Board Daily Scraper — v3
===================================
- Global deduplication across all boards by normalised URL
- Extracts: title, company, location, salary, link
- Removes duplicate jobs that appear on multiple boards

Requirements:
    pip install requests beautifulsoup4 feedparser lxml
"""

import json
import re
import time
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

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

def clean(t: str) -> str:
    t = t.strip()
    t = re.sub(r"Read more about.*", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s+", " ", t)
    return t

def clean_company(name: str) -> str:
    """Clean up company names extracted from URLs."""
    import urllib.parse
    name = urllib.parse.unquote(name)
    name = re.sub(r"[-_]", " ", name)
    # Strip trailing " 2" or " 2 <uuid>" suffixes Getro adds
    name = re.sub(r"\s+2\s+[0-9a-f\s]{8,}$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+2$", "", name.strip())
    # Strip hex UUID fragments
    name = re.sub(r"\b[0-9a-f]{4,}\b", "", name, flags=re.IGNORECASE)
    # Strip long greenhouse board slugs like "m0dbathenextthingltd"
    if len(name.replace(" ", "")) > 20 and " " not in name.strip():
        return ""
    name = re.sub(r"\s+", " ", name).strip()
    # Title case preserving acronyms
    acronyms = {"AI", "HQ", "CEO", "CTO", "CFO", "BD", "VC", "UK", "US",
                "UAE", "KYC", "AML", "DeFi", "NFT", "DAO", "ZK", "DEX",
                "RWA", "SDK", "API", "SVM", "EVM", "GTM", "SDR"}
    words = []
    for w in name.split():
        if w.upper() in acronyms:
            words.append(w.upper())
        else:
            words.append(w.capitalize())
    return " ".join(words).strip()

def clean_location(loc: str) -> str:
    """Only return location if it looks like a real place, not a word fragment."""
    if not loc:
        return ""
    loc = loc.strip().title()
    # Must be at least 4 chars and not a common false positive
    skip = {
        "Lead", "Defi", "Engineer", "Manager", "Remote Ok", "Trader",
        "Strategy", "Partnerships", "Management", "Developer", "Emea",
        "Latam", "Apac", "Newark", "Paulo", "Porto", "Franc", "Xico",
        "America", "Asia", "Kong", "York", "States", "Kingdom",
        "Arlington", "Dubai", "Malta", "Singapore Ok",
    }
    if loc in skip or len(loc) < 3:
        return ""
    return loc

def normalise_url(url: str) -> str:
    """
    Strip tracking params and anchors so the same job linked from
    multiple boards deduplicates correctly.
    e.g. ?utm_source=... and #content are removed.
    """
    try:
        p = urlparse(url)
        # Remove fragment (#content etc)
        # Strip utm_* and tracking query params
        qs = parse_qs(p.query, keep_blank_values=True)
        clean_qs = {k: v for k, v in qs.items()
                    if not k.startswith("utm_")
                    and k not in ("gh_src", "lever-source[]", "gh_jid")}
        clean_query = urlencode(clean_qs, doseq=True)
        cleaned = urlunparse((p.scheme, p.netloc, p.path, p.params, clean_query, ""))
        return cleaned.lower().rstrip("/")
    except Exception:
        return url.lower().rstrip("/")

def make_seen_id(title: str, company: str, norm_url: str) -> str:
    """Dedup key: same title + same company = same job, even across boards."""
    t = re.sub(r"[^a-z0-9]", "", title.lower())[:60]
    c = re.sub(r"[^a-z0-9]", "", company.lower())[:30]
    if t and c:
        return f"job::{t}::{c}"
    if norm_url and len(norm_url) > 20:
        return f"url::{norm_url}"
    return f"title::{t}"


# ---------------------------------------------------------------------------
# Non-web3 company blocklist
# ---------------------------------------------------------------------------

BLOCKED_COMPANIES = {
    # HR/payroll/banking - not web3
    "deel", "loft", "earnin", "mercury", "cross river", "runway",
    "valon", "wingspan", "carta", "addi", "sentilink", "current",
    "branch international", "veem", "taxbit", "clutch", "yuno",
    "ng cash", "coinswitch kuber",
    # HR/recruiting software
    "ashby",
    # Non-crypto companies that appear via portfolio boards
    "ftmo", "discord", "inworld ai", "tellus", "hadrian",
    "indeed",    # job board itself appearing as a company
    "world",     # Worldcoin slug "world-2" resolves to just "World"
    # Big tech with no web3 angle
    "audible", "amazon web services",
}

BLOCKED_URL_FRAGMENTS = {
    "loft.teamtailor.com",
    "branchinternational.applytojob.com",
    "veem.applytojob.com",
    "people-job-posts.vercel.app",
    "crossriver.com",
    "current.com/careers",
    "wellfound.com",
    "indeed.com/viewjob",      # Indeed job links via Multicoin board
}

# Company name fixes — map messy extracted names to clean versions
COMPANY_NAME_FIXES = {
    "chainalysis careers": "Chainalysis",
    "chainalysis-careers": "Chainalysis",
    "mesh 3": "Mesh",
    "breeze 2": "Breeze",
    "3box labs 2": "3Box Labs",
    "douro labs 2": "Douro Labs",
    "kast 2": "Kast",
    "anza 2": "Anza",
    "range 2": "Range",
    "crossmint 2": "Crossmint",
    "1inch 2": "1inch",
    "openzeppelin 2": "OpenZeppelin",
    "layerzero 2": "LayerZero",
    "binance 2": "Binance",
    "keyrock 2": "Keyrock",
    "whitebit 2": "WhiteBIT",
    "op labs": "OP Labs",
    "oplabs": "OP Labs",
    "nomic.foundation": "Nomic Foundation",
    "lido.fi": "Lido",
    "monad.foundation": "Monad Foundation",
    "tools for humanity": "Tools for Humanity",
}

# Junk job titles to always skip regardless of source
JUNK_JOB_TITLES = {
    "don't see any role for you? be the wild card",
    "don t see any role for you be the wild card",
    "general application",
    "general applications",
}

def is_web3_relevant(job: dict) -> bool:
    """Filter out obvious non-web3 jobs from aggregator boards."""
    # Always filter junk titles regardless of source
    title_lower = job.get("title", "").lower().strip()
    if title_lower in JUNK_JOB_TITLES:
        return False

    pure_sources = {
        "EthereumJobBoard", "BitcoinerJobs", "TalentWeb3",
        "DeFi.jobs", "CryptoJobsList", "CryptocurrencyJobs",
        "MyWeb3Jobs", "BlockchainHeadhunter", "BitcoinJobs",
    }
    if job.get("source") in pure_sources:
        return True
    company = job.get("company", "").lower().strip()
    if any(blocked in company for blocked in BLOCKED_COMPANIES):
        return False
    url = job.get("url", "").lower()
    if any(frag in url for frag in BLOCKED_URL_FRAGMENTS):
        return False
    return True

def loc_from_url(url: str) -> str:
    """CryptoJobsList embeds location in URL: /jobs/title-CITY-COUNTRY-at-company"""
    # Pattern: everything between last run of location words and -at-company
    m = re.search(r"/jobs/[^/]+-at-[^/]+$", url)
    if not m:
        return ""
    # Strip the -at-company suffix, then strip the job title prefix
    # URL format: job-title-words-LOCATION-WORDS-at-company
    slug = re.sub(r"-at-[^/]+$", "", url.split("/jobs/")[-1])
    # Known location keywords to look for at the end of the slug
    loc_patterns = [
        r"(remote)$",
        r"(worldwide)$",
        r"(global)$",
        r"([a-z]+-remote)$",
        r"(united-states)$",
        r"(united-kingdom)$",
        r"(hong-kong)$",
        r"([a-z]+-[a-z]+)$",   # two-word location like "new-york" or "latin-america"
        r"([a-z]+)$",           # single word location
    ]
    for pat in loc_patterns:
        lm = re.search(pat, slug)
        if lm:
            raw = lm.group(1).replace("-", " ").title()
            return clean_location(raw)
    return ""

JUNK_TITLES = {
    "post job", "post a job", "find a job", "find jobs", "all jobs",
    "companies", "sign up", "login", "log in", "for candidates",
    "for companies", "read more", "engineering", "design", "marketing",
    "sales", "operations", "non tech", "metaverse", "crypto", "gaming",
    "blockchain", "customer support", "entry level", "finance", "legal",
    "research", "devops", "rust", "java", "golang", "moderator",
}

BAD_URL_PATTERNS = [
    r"/jobs$", r"/jobs/$",
    r"/jobs/design$", r"/jobs/engineering$", r"/jobs/marketing$",
    r"/jobs/sales$", r"/jobs/crypto$", r"/jobs/gaming$",
    r"/jobs/\w+-jobs$",
    r"company/jobs/create",
    r"/post$", r"/post-job",
    r"t\.me/", r"linkedin\.com/company/",
    r"twitter\.com", r"x\.com/hashtag",
    r"instagram\.com", r"facebook\.com",
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

# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_ethereumjobboard() -> list[dict]:
    jobs, seen_urls = [], set()
    r = get("https://www.ethereumjobboard.com/jobs")
    if not r: return jobs
    s = soup(r)
    for card in s.select("a[href*='/jobs/']"):
        title = clean(card.get_text())
        href = card["href"]
        if not href.startswith("http"):
            href = "https://www.ethereumjobboard.com" + href
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        # Try to find company from URL slug
        company = ""
        m = re.search(r"/jobs/[^/]+-([^/]+)$", href)
        if m:
            company = m.group(1).replace("-", " ").title()
        if is_real_job(title, href):
            jobs.append({"title": title, "company": company, "url": href,
                         "source": "EthereumJobBoard"})
    return jobs


def scrape_bitcoinerjobs() -> list[dict]:
    """Niceboard-powered — try their public JSON API."""
    jobs, seen_urls = [], set()
    # Niceboard exposes a public jobs JSON endpoint
    r = get("https://bitcoinerjobs.com/api/jobs?limit=50&status=published")
    if r:
        try:
            data = r.json()
            items = data if isinstance(data, list) else data.get("jobs", data.get("data", []))
            for job in items:
                title = clean(job.get("title", ""))
                url = job.get("url") or job.get("job_url") or job.get("apply_url", "")
                company = job.get("company", {})
                if isinstance(company, dict):
                    company = company.get("name", "")
                norm = normalise_url(url)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, url):
                    jobs.append({"title": title, "company": company,
                                 "url": url, "source": "BitcoinerJobs"})
            if jobs:
                return jobs
        except Exception:
            pass
    # Fallback: try RSS
    for e in feedparser.parse("https://bitcoinerjobs.com/feed.xml").entries:
        title = clean(e.title)
        company = getattr(e, "author", "")
        if is_real_job(title, e.link):
            jobs.append({"title": title, "company": company, "url": e.link,
                         "source": "BitcoinerJobs"})
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
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        if is_real_job(title, href):
            jobs.append({"title": title, "company": "", "url": href,
                         "source": "TalentWeb3"})
    return jobs


def _getro(slug: str, display: str, base_url: str) -> list[dict]:
    """Shared scraper for Getro-powered boards."""
    jobs, seen_urls = [], set()
    r = get(base_url)
    if not r: return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        title = clean(a.get_text())
        if not title or "Read more" in title: continue
        href = a["href"]
        if not href.startswith("http"):
            href = base_url.rstrip("/") + href
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        # Extract company from URL: /companies/COMPANY/jobs/...
        company = ""
        m = re.search(r"/companies/([^/]+)/jobs/", href)
        if m:
            raw = re.sub(r"-[0-9a-f-]{8,}$", "", m.group(1))  # strip UUID suffixes
            company = clean_company(raw)
        if is_real_job(title, href):
            jobs.append({"title": title, "company": company, "url": href,
                         "source": display, "location": loc_from_url(href)})
    return jobs

def scrape_safary()      -> list[dict]: return _getro("safary",     "Safary",      "https://jobs.safary.club/jobs")
def scrape_solana_jobs() -> list[dict]: return _getro("solana",     "SolanaJobs",  "https://jobs.solana.com/jobs")
def scrape_a16z_crypto() -> list[dict]:
    """a16z crypto jobs — try Getro network API then ashbyhq fallback."""
    jobs, seen_urls = [], set()
    # a16z uses jobs.a16z.com which redirects to ashbyhq
    for base in [
        "https://jobs.a16z.com/jobs",
        "https://a16zcrypto.com/jobs/",
    ]:
        r = get(base)
        if not r: continue
        s = soup(r)
        for a in s.select("a[href*='/jobs/'], a[href*='ashbyhq.com']"):
            href = a.get("href", "")
            if not href.startswith("http"):
                href = base.rstrip("/") + href
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            title = clean(a.get_text())
            company = ""
            m = re.search(r"ashbyhq\.com/([^/]+)/", href)
            if m:
                company = clean_company(m.group(1))
            if is_real_job(title, href):
                jobs.append({"title": title, "company": company,
                             "url": href, "source": "a16z Crypto"})
        if jobs:
            break
    return jobs
def scrape_bitkraft()    -> list[dict]: return _getro("bitkraft",   "BITKRAFT VC", "https://careers.bitkraft.vc/jobs")


def scrape_cryptojobslist() -> list[dict]:
    jobs, seen_urls = [], set()
    for e in feedparser.parse("https://cryptojobslist.com/rss").entries:
        title = clean(e.title)
        url = e.link
        norm = normalise_url(url)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        # Extract company from URL: /jobs/title-at-COMPANY
        company = ""
        m = re.search(r"-at-([^/]+)$", url)
        if m:
            company = m.group(1).replace("-", " ").title()
        if is_real_job(title, url):
            jobs.append({"title": title, "company": company, "url": url,
                         "source": "CryptoJobsList", "location": loc_from_url(url)})
    return jobs


def scrape_cryptocurrencyjobs() -> list[dict]:
    """Try multiple feed URLs — site uses Next.js so HTML scraping is unreliable."""
    jobs, seen_urls = [], set()
    feed_urls = [
        "https://cryptocurrencyjobs.co/feed/",
        "https://cryptocurrencyjobs.co/feed.xml",
        "https://cryptocurrencyjobs.co/rss.xml",
        "https://cryptocurrencyjobs.co/rss",
    ]
    for feed_url in feed_urls:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            for e in feed.entries:
                title = clean(e.title)
                if not is_real_job(title, e.link): continue
                norm = normalise_url(e.link)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                loc = ""
                summary = getattr(e, "summary", "")
                m = re.search(r"(Remote|[\w\s]+,\s*[\w\s]+)", summary)
                if m: loc = clean_location(m.group(1).strip()[:40])
                company = ""
                cm = re.search(r"at ([\w\s]+)$", title)
                if cm: company = cm.group(1).strip()
                jobs.append({"title": title, "company": company, "url": e.link,
                             "source": "CryptocurrencyJobs", "location": loc})
            break  # stop at first working feed
    # HTML fallback
    if not jobs:
        r = get("https://cryptocurrencyjobs.co/")
        if r:
            s = soup(r)
            for a in s.select("a[href*='/jobs/']"):
                title = clean(a.get_text())
                href = a["href"]
                if not href.startswith("http"):
                    href = "https://cryptocurrencyjobs.co" + href
                norm = normalise_url(href)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, href):
                    jobs.append({"title": title, "company": "", "url": href,
                                 "source": "CryptocurrencyJobs"})
    return jobs


def scrape_myweb3jobs() -> list[dict]:
    """WordPress site — try RSS feed variants, then HTML."""
    jobs, seen_urls = [], set()
    feed_urls = [
        "https://myweb3jobs.com/feed/",
        "https://myweb3jobs.com/job-feed/",
        "https://myweb3jobs.com/?feed=rss2",
        "https://myweb3jobs.com/?post_type=job_listing&feed=rss2",
    ]
    for feed_url in feed_urls:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            for e in feed.entries:
                title = clean(e.title)
                norm = normalise_url(e.link)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, e.link):
                    jobs.append({"title": title, "company": "", "url": e.link,
                                 "source": "MyWeb3Jobs"})
            break
    # HTML fallback — WP Job Manager uses /job/ URLs
    if not jobs:
        r = get("https://myweb3jobs.com/")
        if r:
            s = soup(r)
            for a in s.select("a[href*='/job/'], a[href*='/jobs/']"):
                title = clean(a.get_text())
                href = a["href"]
                if not href.startswith("http"):
                    href = "https://myweb3jobs.com" + href
                norm = normalise_url(href)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, href):
                    jobs.append({"title": title, "company": "", "url": href,
                                 "source": "MyWeb3Jobs"})
    return jobs


def scrape_defi_jobs() -> list[dict]:
    """DeFi.jobs - Webflow site, jobs are in HTML as /jobs/slug links."""
    jobs, seen_urls = [], set()
    r = get("https://www.defi.jobs/")
    if not r: return jobs
    s = soup(r)
    # Jobs link to /jobs/job-title-slug
    for a in s.select("a[href]"):
        href = a.get("href", "")
        # Must match /jobs/ with a slug after it (not just /jobs)
        if not re.search(r"/jobs/[a-z0-9-]{3,}", href):
            continue
        if not href.startswith("http"):
            href = "https://www.defi.jobs" + href
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        title = clean(a.get_text())
        if not title:
            # Try to get title from slug
            slug = href.rstrip("/").split("/")[-1]
            title = slug.replace("-", " ").title()
        if is_real_job(title, href):
            jobs.append({"title": title, "company": "Hype Talent", "url": href,
                         "source": "DeFi.jobs"})
    return jobs


def scrape_hashtagweb3() -> list[dict]:
    """Only external job links, no social/nav, title fixed via URL slug."""
    jobs, seen_urls = [], set()
    skip_domains = {
        "linkedin.com", "twitter.com", "x.com", "instagram.com",
        "t.me", "facebook.com", "youtube.com", "telegram.org",
    }
    r = get("https://hashtagweb3.com/jobs")
    if not r: return jobs
    for a in soup(r).select("a[href]"):
        href = a["href"]
        if not href.startswith("http"): continue
        if "hashtagweb3.com" in href: continue
        if any(d in href for d in skip_domains): continue
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)

        # Get clean title: split camelCase or domain-appended company suffix
        raw = clean(a.get_text())
        # Split at camelCase boundary e.g. "Senior EngineerAshby" -> "Senior Engineer"
        # Also handles "Growth PMmoonshot.money" and "Engineer3Box Labs"
        title = re.sub(r"([a-z0-9])([A-Z][a-z])", r"\1||||\2", raw).split("||||")[0].strip()
        # Strip appended domain names e.g. "moonshot.money", "3Box Labs"
        title = re.sub(r"[\s]*(\d[A-Z]\w+\s+\w+|\w+\.\w+)$", "", title).strip()
        # Strip known company names appended with no space
        title = re.sub(
            r"(Ashby|Crossmint|Coinbase|Fireblocks|Phantom|Lido|VALR|"
            r"OP Labs|Sui Foundation|Solana Foundation|BCB Group|Tellus|"
            r"1Inch|1inch|Worldcoin|Binance|Ripple|Circle|Alchemy|"
            r"LayerZero|Offchain Labs|Consensys|Eigen|Gensyn|Matrixport|"
            r"Nansen|Range|Veda|Breeze|Anchorage|Paradigm|Chainalysis)$",
            "", title).strip()

        # Extract company from URL where possible
        company = ""
        for pat in [
            r"greenhouse\.io/([^/]+)/jobs",
            r"ashbyhq\.com/([^/]+)/",
            r"lever\.co/([^/]+)/",
            r"jobs\.[^/]+/companies/([^/]+)/jobs",
            r"gem\.com/([^/]+)/",
        ]:
            m = re.search(pat, href)
            if m:
                company = clean_company(m.group(1))
                break

        if is_real_job(title, href):
            jobs.append({"title": title, "company": company, "url": href,
                         "source": "HashtagWeb3"})
    return jobs


def scrape_blockchainheadhunter() -> list[dict]:
    """BlockchainHeadhunter loads jobs via JS — scrape their sitemap instead."""
    jobs, seen_urls = [], set()
    # Try sitemap first as it contains static job URLs
    r = get("https://blockchainheadhunter.com/sitemap.xml")
    if r:
        urls = re.findall(r"<loc>(https://blockchainheadhunter\.com/[^<]+)</loc>", r.text)
        for href in urls:
            # Job pages have a slug pattern like /senior-engineer-company
            if any(x in href for x in ["/for-companies", "/news", "/about",
                                        "/contact", "/blog", "/education",
                                        "/jobs", "/sitemap", "/sponsored"]):
                continue
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            # Extract title from slug
            slug = href.rstrip("/").split("/")[-1]
            title = slug.replace("-", " ").title()
            if is_real_job(title, href):
                jobs.append({"title": title, "company": "", "url": href,
                             "source": "BlockchainHeadhunter"})
    return jobs


def scrape_bitcoinjobs() -> list[dict]:
    """BitcoinJobs uses /job-title-company slugs not /job/id paths."""
    jobs, seen_urls = [], set()
    r = get("https://bitcoinjobs.com/")
    if not r: return jobs
    s = soup(r)
    # Job cards are <a> tags linking to slugs like /software-engineer-river
    for a in s.select("a[href]"):
        href = a["href"]
        # Must be a root-level slug (not nav links)
        if not re.match(r"^/[a-z][a-z0-9-]+$", href):
            continue
        if href in ["/", "/companies", "/job-alerts", "/categories", "/post"]:
            continue
        full_href = "https://bitcoinjobs.com" + href
        norm = normalise_url(full_href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        # Get title from the h2 inside the card if possible
        h2 = a.find("h2")
        title = clean(h2.get_text()) if h2 else clean(a.get_text())
        # Get company — usually the next text node or a separate element
        company_el = a.find(string=re.compile(r"[A-Z][a-z]"))
        company = ""
        if is_real_job(title, full_href):
            jobs.append({"title": title, "company": company, "url": full_href,
                         "source": "BitcoinJobs"})
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

    # Global URL dedup across all boards in this run
    this_run_urls: set[str] = set()

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
            norm = normalise_url(job.get("url", ""))
            jid  = make_seen_id(job["title"], job.get("company", ""), norm)

            # Filter out obvious non-web3 jobs
            if not is_web3_relevant(job):
                continue

            # Skip if seen in a previous run OR already seen in this run
            if jid in seen or norm in this_run_urls:
                continue

            seen.add(jid)
            this_run_urls.add(norm)
            new.append(job)

        print(f"  {len(jobs)} found, {len(new)} new (after global dedup)")
        all_new.extend(new)
        time.sleep(REQUEST_DELAY)

    save_seen(seen)

    # ---------------------------------------------------------------------------
    # Slack output
    # ---------------------------------------------------------------------------

    if not all_new:
        print(f"*Web3 Jobs — {datetime.now().strftime('%d %b %Y')}*\nNo new jobs since last run.")
        return all_new

    lines = [
        f":new: *Web3 Jobs — {datetime.now().strftime('%d %b %Y')}*",
        f"_{len(all_new)} new jobs_",
        "",
    ]

    for job in all_new:
        title   = job.get("title", "").strip()
        company = job.get("company", "").strip()
        loc     = clean_location(job.get("location", ""))
        sal     = job.get("salary", "").strip()
        url     = job.get("url", "").strip()

        block = [f"*{title}*"]
        if company:
            block.append(f"🏢 {company}")
        if sal:
            block.append(f"💰 {sal}")
        if loc:
            block.append(f"📍 {loc}")
        block.append(f"🔗 {url}")
        block.append("")  # blank line between jobs

        lines.extend(block)

    print("\n".join(lines))
    return all_new


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    run(reset=args.reset)
