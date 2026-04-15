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

import html
import json
import re
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

# Ensure UTF-8 output on Windows so emoji don't crash
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

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
        print(f"  [WARN] {url}: {e}", file=sys.stderr)
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
    """Only return location if it looks like a real place."""
    if not loc:
        return ""
    loc = loc.strip().title()

    # Direct fixes for truncated/mangled locations
    loc_fixes = {
        "Kong": "Hong Kong", "Kong Sar": "Hong Kong",
        "Hong Kong Sar": "Hong Kong", "York": "New York",
        "States": "United States", "Kingdom": "United Kingdom",
        "Xico": "Mexico", "Paulo": "Sao Paulo",
        "Francisco": "San Francisco", "America": "Latin America",
        "Uae": "UAE", "Uae Dubai": "Dubai",
    }
    if loc in loc_fixes:
        return loc_fixes[loc]

    # Accepted real locations
    valid_locations = {
        "Remote", "Worldwide", "Global", "United States", "United Kingdom",
        "New York", "San Francisco", "London", "Singapore", "Dubai",
        "Hong Kong", "Berlin", "Amsterdam", "Zurich", "Geneva",
        "Lisbon", "Madrid", "Paris", "Tokyo", "Seoul", "Sydney",
        "Toronto", "Vancouver", "Austin", "Miami", "Los Angeles",
        "Chicago", "Boston", "Seattle", "Denver", "Atlanta",
        "Latin America", "Europe", "Asia", "EMEA", "APAC", "LATAM",
        "Remote US", "Remote UK", "Remote Europe", "Remote Global",
        "Malta", "Portugal", "Spain", "Brazil", "India", "Poland",
        "Germany", "Netherlands", "France", "Italy", "Canada",
        "Australia", "Japan", "South Korea", "UAE", "Sao Paulo",
        "Mexico", "Argentina", "Colombia", "Nigeria", "Kenya",
        "New York NY", "Jersey City NJ", "Houston TX",
    }

    # Check exact match
    if loc in valid_locations:
        return loc

    # Check if it starts with a valid location
    for valid in valid_locations:
        if loc.startswith(valid):
            return valid

    # If 2 words or less and looks like a place (not a job title word)
    words = loc.split()
    job_words = {
        "engineer", "manager", "developer", "analyst", "lead", "director",
        "specialist", "consultant", "associate", "coordinator", "executive",
        "officer", "architect", "designer", "researcher", "scientist",
        "trader", "programmer", "founder", "head", "chief", "senior",
        "junior", "staff", "principal", "defi", "blockchain", "crypto",
        "remote", "acquisition", "content", "product", "platform",
        "attribution", "strategy", "partnerships", "management",
    }
    if len(words) <= 2 and not any(w.lower() in job_words for w in words):
        if len(loc) >= 3:
            return loc

    return ""

def normalise_url(url: str) -> str:
    """
    Strip tracking params and anchors so the same job linked from
    multiple boards deduplicates correctly.
    """
    try:
        p = urlparse(url)
        # Normalise LinkedIn regional subdomains to linkedin.com
        netloc = p.netloc
        if "linkedin.com" in netloc:
            netloc = "www.linkedin.com"
        # Strip all tracking/referral query params
        qs = parse_qs(p.query, keep_blank_values=True)
        clean_qs = {k: v for k, v in qs.items()
                    if not k.startswith("utm_")
                    and k not in ("gh_src", "lever-source[]", "gh_jid",
                                  "utm_medium", "utm_campaign", "utm_content",
                                  "gh_src", "trk", "src")}
        clean_query = urlencode(clean_qs, doseq=True)
        # Always strip fragment/anchor (e.g. #content from Getro boards)
        cleaned = urlunparse((p.scheme, netloc, p.path, p.params, clean_query, ""))
        return cleaned.lower().rstrip("/")
    except Exception:
        return url.lower().rstrip("/")


def clean_display_url(url: str) -> str:
    """Strip fragment, tracking params and utm from display URLs."""
    # Strip #anchor (e.g. #content from Getro)
    url = url.split("#")[0]
    # Strip utm params
    if "utm_" in url or "?gh_src" in url:
        try:
            p = urlparse(url)
            qs = parse_qs(p.query, keep_blank_values=True)
            clean_qs = {k: v for k, v in qs.items()
                        if not k.startswith("utm_")
                        and k not in ("gh_src", "trk", "src")}
            clean_query = urlencode(clean_qs, doseq=True)
            url = urlunparse((p.scheme, p.netloc, p.path, p.params, clean_query, ""))
        except Exception:
            pass
    return url.rstrip("/")

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
    "audible", "amazon web services", "amazon",
    # Data center / mining infrastructure (not web3 jobs)
    "crusoe", "genesis digital assets",
    # Non-web3 that keep slipping through
    "audible inc", "audible", "amazon", "amazon web services",
    "delta exchange",  # Indian crypto exchange, not web3 native
    "employinc", "employ inc",
    "fullcircl", "ncino",  # Non-crypto from general VC portfolios
    "zscaler",  # Enterprise security, not web3
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
    "careers.employinc.com",   # Non-web3 HR company
    "hire.withgoogle.com",     # Google hiring nav link
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
    "1inch network": "1inch",
    "openzeppelin 2": "OpenZeppelin",
    "layerzero 2": "LayerZero",
    "layerzerolabs": "LayerZero Labs",
    "binance 2": "Binance",
    "keyrock 2": "Keyrock",
    "whitebit 2": "WhiteBIT",
    "op labs": "OP Labs",
    "oplabs": "OP Labs",
    "nomic.foundation": "Nomic Foundation",
    "lido.fi": "Lido",
    "monad.foundation": "Monad Foundation",
    "tools for humanity": "Tools for Humanity",
    "streamingfast": "StreamingFast",
    "mystenlabs": "Mysten Labs",
    "aptoslabs": "Aptos Labs",
    "avalabs": "Ava Labs",
    "skymavis": "Sky Mavis",
    "bcbgroup": "BCB Group",
    "tryjeeves": "Jeeves",
    "talos trading": "Talos",
    "talos-trading": "Talos",
    "cruxclimate": "Crux Climate",
    "opensea": "OpenSea",
    "eigen labs": "EigenLayer",
    "m0dbathenextthingltd": "M^0 Labs",
    "blackbird labs inc": "Blackbird",
    "blackbird-labs-inc": "Blackbird",
    "tools for humanity": "Tools for Humanity",
    "ondo-finance": "Ondo Finance",
    "ondo finance": "Ondo Finance",
    "wintermute trading": "Wintermute",
    "layerzerolabs": "LayerZero Labs",
    "offchain labs": "Offchain Labs",
}

def apply_company_fixes(name: str) -> str:
    """Apply known company name fixes."""
    if not name:
        return name
    fixed = COMPANY_NAME_FIXES.get(name.lower().strip())
    return fixed if fixed else name

# Junk job titles to always skip regardless of source
JUNK_JOB_TITLES = {
    "don't see any role for you? be the wild card",
    "don t see any role for you be the wild card",
    "general application",
    "general applications",
    "join our community",
    "web3 recruiter (full-time/part-time/intern)",
    "no open positions",
    "see all jobs",
    "view all jobs",
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
        # Extract company from URL slug
        # Format: /jobs/job-title-words-company-name
        # Strategy: title words are known, remainder is company
        slug = href.rstrip("/").split("/")[-1]
        title_slug = re.sub(r"[^a-z0-9]", "-", title.lower())
        # Remove title portion from slug to get company
        company = ""
        # Try to find company by removing title-like prefix from slug
        title_words = set(re.sub(r"[^a-z]", " ", title.lower()).split())
        slug_parts = slug.split("-")
        # Find where title words end and company begins
        company_parts = []
        title_matched = 0
        for i, part in enumerate(slug_parts):
            if part in title_words and title_matched < len(title_words):
                title_matched += 1
            else:
                company_parts = slug_parts[i:]
                break
        if company_parts:
            company = " ".join(company_parts).title()
            # Clean up known noise
            if company.lower() in {"remote", "global", "worldwide", "full", "time"}:
                company = ""
        if is_real_job(title, href) and not is_intern(title):
            jobs.append({"title": title, "company": company, "url": href,
                         "source": "EthereumJobBoard"})
    return jobs


def scrape_bitcoinerjobs() -> list[dict]:
    """Niceboard-powered — scrape HTML job listings directly."""
    jobs, seen_urls = [], set()
    # Try multiple Niceboard API endpoints
    for api_url in [
        "https://bitcoinerjobs.com/api/v1/jobs?per_page=50",
        "https://bitcoinerjobs.com/api/jobs",
        "https://niceboard.co/api/v1/boards/bitcoinerjobs/jobs",
    ]:
        r = get(api_url)
        if not r: continue
        try:
            data = r.json()
            items = data if isinstance(data, list) else data.get("jobs", data.get("data", []))
            if not items: continue
            for job in items:
                title = clean(str(job.get("title", "")))
                url = (job.get("url") or job.get("job_url") or
                       job.get("apply_url") or job.get("external_url", ""))
                if not url:
                    slug = job.get("slug", "")
                    url = f"https://bitcoinerjobs.com/jobs/{slug}" if slug else ""
                company = job.get("company", {})
                if isinstance(company, dict):
                    company = company.get("name", "")
                elif not isinstance(company, str):
                    company = ""
                norm = normalise_url(url)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, url):
                    jobs.append({"title": title, "company": company,
                                 "url": url, "source": "BitcoinerJobs"})
            if jobs: return jobs
        except Exception:
            continue
    # HTML fallback — scrape category pages which list actual job postings
    categories = ["engineering", "business-operations", "marketing",
                  "product", "other", "media-and-events"]
    for cat in categories:
        r = get(f"https://bitcoinerjobs.com/category/{cat}")
        if not r: continue
        s = soup(r)
        for a in s.select("a[href]"):
            href = a.get("href", "")
            if not href.startswith("http"):
                href = "https://bitcoinerjobs.com" + href
            if "bitcoinerjobs.com" not in href: continue
            # Job links look like /job-title-company or contain /jobs/
            path = href.replace("https://bitcoinerjobs.com", "").rstrip("/")
            # Skip known non-job pages
            skip = {"/", "/companies", "/post", "/categories", "/places",
                    "/tos", "/privacy", "/seeker/login", "/seeker/signup",
                    "/employer/login", "/employer/signup", "/job-alerts"}
            if path in skip: continue
            if path.startswith("/category"): continue
            if path.startswith("/company"): continue
            # Must look like a job slug - at least 2 hyphens
            if path.count("-") < 2: continue
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            title = clean(a.get_text())
            if not title or len(title) < 5: continue
            # Skip descriptions and metadata text
            if len(title) > 100: continue
            if any(x in title.lower() for x in ["jobs", "bitcoin company",
                                                  "bitcoin wealth", "mining industry",
                                                  "atomic economy"]): continue
            if is_real_job(title, href):
                jobs.append({"title": title, "company": "",
                             "url": href, "source": "BitcoinerJobs"})
        time.sleep(0.5)
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
        # Strip trailing digits left over from slug dedup e.g. "Senior Product Owner1"
        title = re.sub(r"\s*\d+$", "", title).strip()
        if is_real_job(title, href):
            jobs.append({"title": title, "company": company, "url": href,
                         "source": display, "location": loc_from_url(href)})
    return jobs

def scrape_safary()      -> list[dict]: return _getro("safary",     "Safary",      "https://jobs.safary.club/jobs")
def scrape_solana_jobs() -> list[dict]: return _getro("solana",     "SolanaJobs",  "https://jobs.solana.com/jobs")
def scrape_a16z_crypto() -> list[dict]:
    """a16z crypto — scrape their Ashby-powered jobs portal directly."""
    jobs, seen_urls = [], set()
    # a16z portfolio jobs are listed at jobs.ashbyhq.com/a16z
    # but the main accessible page is through their Getro board
    r = get("https://a16zcrypto.com/jobs/")
    if not r:
        return jobs
    s = soup(r)
    # Jobs load client-side but some may be in static HTML
    # Try to find any ashbyhq or job links
    for a in s.select("a[href]"):
        href = a.get("href", "")
        if not href.startswith("http"): continue
        if not any(x in href for x in ["ashbyhq", "greenhouse", "lever", "jobs."]):
            continue
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        title = clean(a.get_text())
        company = ""
        m = re.search(r"ashbyhq\.com/([^/]+)/", href)
        if m: company = clean_company(m.group(1))
        if is_real_job(title, href):
            jobs.append({"title": title, "company": company,
                         "url": href, "source": "a16z Crypto"})
    return jobs
def scrape_bitkraft()         -> list[dict]: return _getro("bitkraft",        "BITKRAFT VC",        "https://careers.bitkraft.vc/jobs")
def scrape_multicoin()        -> list[dict]: return _getro("multicoin",       "Multicoin Capital",  "https://jobs.multicoin.capital")
def scrape_delphi()           -> list[dict]: return _getro("delphi",          "Delphi Ventures",    "https://jobs.delphiventures.io/jobs")
def scrape_galaxy_vc()        -> list[dict]: return _getro("galaxy",          "Galaxy Ventures",    "https://venturecareers.galaxy.com/jobs")
def scrape_jump()             -> list[dict]: return _getro("jumpcrypto",      "Jump Crypto",        "https://jobs.jumpcrypto.com/jobs")
def scrape_polychain()        -> list[dict]: return _getro("polychain",       "Polychain",          "https://jobs.polychain.capital/jobs")
def scrape_framework()        -> list[dict]: return _getro("framework",       "Framework Ventures", "https://jobs.framework.ventures")
def scrape_coinfund()         -> list[dict]: return _getro("coinfund",        "CoinFund",           "https://jobs.coinfund.io/jobs")
def scrape_outlier()          -> list[dict]: return _getro("outlierventures", "Outlier Ventures",   "https://jobs.outlierventures.io")
def scrape_electric()         -> list[dict]: return _getro("electriccapital", "Electric Capital",   "https://jobs.electriccapital.com/jobs")
def scrape_variant()          -> list[dict]: return _getro("variant",         "Variant Fund",       "https://jobs.variant.fund")
def scrape_pantera()          -> list[dict]: return _getro("pantera",         "Pantera Capital",    "https://jobs.panteracapital.com/jobs")
def scrape_lemniscap()        -> list[dict]: return _getro("lemniscap",       "Lemniscap",          "https://careers.lemniscap.com/jobs")
def scrape_dragonfly()        -> list[dict]: return _getro("dragonfly",       "Dragonfly",          "https://jobs.dragonfly.xyz")
def scrape_avax()             -> list[dict]: return _getro("avax",            "Avalanche Ecosystem","https://jobs.avax.network")
def scrape_ton()              -> list[dict]: return _getro("ton",             "TON Ecosystem",      "https://jobs.ton.org")
def scrape_blockchain_assoc() -> list[dict]: return _getro("blockchainassociation", "Blockchain Association", "https://jobs.theblockchainassociation.org/jobs")
def scrape_fabric_vc()        -> list[dict]: return _getro("fabric",          "Fabric VC",          "https://careers.fabric.vc/jobs")
def scrape_octopus()          -> list[dict]: return _getro("octopusventures", "Octopus Ventures",   "https://talent.octopusventures.com/jobs")
def scrape_base_hirechain()   -> list[dict]: return _getro("basehirechain",   "Base Ecosystem",     "https://base.hirechain.io/")


def scrape_venturecapitalcareers() -> list[dict]:
    """VentureCapitalCareers.com - VC portfolio jobs.
    Real job URLs are /companies/{firm}/jobs/{slug}.
    Skip taxonomy pages (/jobs/skills/*, /jobs/category) which have the same selector."""
    jobs, seen_urls = [], set()
    r = get("https://venturecapitalcareers.com/jobs")
    if not r: return jobs
    for a in soup(r).select("a[href*='/companies/']"):
        href = a.get("href", "")
        # Must match /companies/{firm}/jobs/{slug} pattern
        if not re.search(r"/companies/[^/]+/jobs/[^/]+", href):
            continue
        if not href.startswith("http"):
            href = "https://venturecapitalcareers.com" + href
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        title = clean(a.get_text())
        if not title or len(title) < 5:
            # Title not on the link itself — derive from URL slug
            slug = href.rstrip("/").split("/jobs/")[-1]
            title = re.sub(r"[^a-z0-9]+", " ", slug).strip().title()
        if is_real_job(title, href) and not is_intern(title):
            jobs.append({"title": title, "company": "", "url": href, "source": "VCCareers"})
    return jobs


def scrape_defi_jobs_xyz() -> list[dict]:
    """DeFi-jobs.xyz niche board."""
    jobs, seen_urls = [], set()
    for feed_url in ["https://defi-jobs.xyz/feed/", "https://defi-jobs.xyz/rss"]:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            for e in feed.entries:
                title = clean(e.title)
                norm = normalise_url(e.link)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, e.link) and not is_intern(title):
                    jobs.append({"title": title, "company": "", "url": e.link, "source": "DeFiJobsXYZ"})
            if jobs: return jobs
    r = get("https://defi-jobs.xyz")
    if not r: return jobs
    for a in soup(r).select("a[href*='/job/'], a[href*='/jobs/']"):
        title = clean(a.get_text())
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://defi-jobs.xyz" + href
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        if is_real_job(title, href) and not is_intern(title):
            jobs.append({"title": title, "company": "", "url": href, "source": "DeFiJobsXYZ"})
    return jobs


def scrape_cryptojobs_com() -> list[dict]:
    """Cryptojobs.com aggregator - try RSS then HTML."""
    jobs, seen_urls = [], set()
    for feed_url in [
        "https://cryptojobs.com/feed/",
        "https://cryptojobs.com/rss",
        "https://www.cryptojobs.com/feed/",
        "https://cryptojobs.com/jobs.rss",
    ]:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            for e in feed.entries:
                title = clean(getattr(e, "title", ""))
                link = getattr(e, "link", "")
                if not link: continue
                norm = normalise_url(link)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, link) and not is_intern(title):
                    jobs.append({"title": title, "company": "", "url": link,
                                 "source": "CryptoJobs.com"})
            if jobs: return jobs
    for page_url in ["https://cryptojobs.com/jobs", "https://www.cryptojobs.com/jobs",
                     "https://cryptojobs.com/"]:
        r = get(page_url)
        if not r: continue
        for a in soup(r).select("a[href*='/job/'], a[href*='/jobs/']"):
            title = clean(a.get_text())
            href = a.get("href", "")
            if not href.startswith("http"):
                href = "https://cryptojobs.com" + href
            if "cryptojobs.com" not in href: continue
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            if is_real_job(title, href) and not is_intern(title):
                jobs.append({"title": title, "company": "", "url": href,
                             "source": "CryptoJobs.com"})
        if jobs: break
    return jobs


def scrape_crypto_jobs_ch() -> list[dict]:
    """Crypto-jobs.ch - Swiss/European crypto jobs."""
    jobs, seen_urls = [], set()
    for page_url in ["https://crypto-jobs.ch/search", "https://crypto-jobs.ch/jobs",
                     "https://crypto-jobs.ch/"]:
        r = get(page_url)
        if not r: continue
        for a in soup(r).select("a[href*='/jobs/'], a[href*='/job/'], a[href*='/position/']"):
            title = clean(a.get_text())
            href = a.get("href", "")
            if not href.startswith("http"):
                href = "https://crypto-jobs.ch" + href
            if "crypto-jobs.ch" not in href: continue
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            if is_real_job(title, href) and not is_intern(title):
                jobs.append({"title": title, "company": "", "url": href,
                             "source": "CryptoJobsCH"})
        if jobs: break
    return jobs


def scrape_remote3() -> list[dict]:
    """Remote3 - web3 remote jobs."""
    jobs, seen_urls = [], set()
    # Try RSS/sitemap first
    feed = feedparser.parse("https://www.remote3.co/rss.xml")
    if feed.entries:
        for e in feed.entries:
            title = clean(getattr(e, "title", ""))
            link = getattr(e, "link", "")
            if not link: continue
            norm = normalise_url(link)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            if is_real_job(title, link) and not is_intern(title):
                jobs.append({"title": title, "company": "",
                             "url": link, "source": "Remote3"})
        if jobs: return jobs
    # HTML fallback - look for actual job cards not category links
    for page_url in [
        "https://www.remote3.co/remote-web3-jobs",
        "https://www.remote3.co/",
    ]:
        r = get(page_url)
        if not r: continue
        s = soup(r)
        for a in s.select("a[href]"):
            href = a.get("href", "")
            if not href.startswith("http"):
                href = "https://www.remote3.co" + href
            # Only actual job pages - must have /web3-job/ in path
            if "/web3-job/" not in href: continue
            # Skip category pages ending in -jobs
            if re.search(r"-jobs/?$", href): continue
            title = clean(a.get_text())
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            if is_real_job(title, href) and not is_intern(title):
                jobs.append({"title": title, "company": "",
                             "url": href, "source": "Remote3"})
        if jobs: break
    return jobs


def scrape_web3career() -> list[dict]:
    """Web3.career - try their RSS feed first, then HTML."""
    jobs, seen_urls = [], set()
    # Try RSS first
    for feed_url in [
        "https://web3.career/rss",
        "https://web3.career/feed",
        "https://web3.career/remote-web3-jobs.rss",
    ]:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            for e in feed.entries:
                title = clean(getattr(e, "title", ""))
                link = getattr(e, "link", "")
                if not link: continue
                norm = normalise_url(link)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                company = clean(getattr(e, "author", ""))
                if is_real_job(title, link) and not is_intern(title):
                    jobs.append({"title": title, "company": company,
                                 "url": link, "source": "Web3.career"})
            if jobs:
                print(f"  Web3.career RSS: {len(jobs)} jobs", file=sys.stderr)
                return jobs
    # Fallback to HTML with better selectors
    r = get("https://web3.career/web3-jobs")
    if not r: return jobs
    s = soup(r)
    for row in s.select("tr[data-jobid], div[data-jobid]"):
        # Title lives in the <h2> inside .job-title-mobile; the first <a> often
        # has no visible text (it wraps a logo or icon), so grab title first.
        h2 = row.find("h2")
        title = clean(h2.get_text()) if h2 else ""
        if not title or len(title) < 5:
            continue
        a = row.find("a", href=True)
        if not a: continue
        href = a["href"]
        if not href.startswith("http"):
            href = "https://web3.career" + href
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        company = ""
        co_el = row.find(class_=re.compile(r"company|employer"))
        if co_el: company = clean(co_el.get_text())
        if is_real_job(title, href) and not is_intern(title):
            jobs.append({"title": title, "company": company,
                         "url": href, "source": "Web3.career"})
    return jobs


def scrape_cryptodotjobs() -> list[dict]:
    jobs, seen_urls = [], set()
    feed = feedparser.parse("https://crypto.jobs/feed")
    for e in feed.entries:
        title = clean(e.title)
        norm = normalise_url(e.link)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        if is_real_job(title, e.link) and not is_intern(title):
            jobs.append({"title": title, "company": "", "url": e.link, "source": "Crypto.jobs"})
    if jobs: return jobs
    r = get("https://crypto.jobs/jobs")
    if not r: return jobs
    for a in soup(r).select("a[href*='/jobs/']"):
        title = clean(a.get_text())
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://crypto.jobs" + href
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        if is_real_job(title, href) and not is_intern(title):
            jobs.append({"title": title, "company": "", "url": href, "source": "Crypto.jobs"})
    return jobs


def scrape_jobstash() -> list[dict]:
    """Jobstash public API - crypto/web3 focused."""
    jobs, seen_urls = [], set()
    # Try multiple API endpoints
    for api_url in [
        "https://api.jobstash.xyz/jobs?page=1&limit=100&tags=crypto,web3,blockchain,defi",
        "https://api.jobstash.xyz/jobs?page=1&limit=100",
        "https://jobstash.xyz/api/jobs?page=1",
    ]:
        r = get(api_url)
        if not r: continue
        try:
            data = r.json()
            # Handle different response shapes
            items = (data.get("data") or data.get("jobs") or
                     data.get("results") or
                     (data if isinstance(data, list) else []))
            if not items: continue
            for job in items:
                title = clean(str(job.get("title", "")))
                url = (job.get("url") or job.get("apply_url") or
                       job.get("applicationUrl") or "")
                org = job.get("organization") or job.get("company") or {}
                company = org.get("name", "") if isinstance(org, dict) else str(org)
                if not url or not title: continue
                norm = normalise_url(url)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, url) and not is_intern(title):
                    jobs.append({"title": title, "company": company,
                                 "url": url, "source": "Jobstash"})
            if jobs: return jobs
        except Exception as e:
            print(f"  Jobstash API error: {e}", file=sys.stderr)
            continue
    return jobs


def scrape_stablecoin_jobs() -> list[dict]:
    """Stablecoin-jobs.com - niche stablecoin jobs board."""
    jobs, seen_urls = [], set()
    # Try RSS first
    for feed_url in ["https://www.stablecoin-jobs.com/feed/",
                     "https://www.stablecoin-jobs.com/rss"]:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            for e in feed.entries:
                title = clean(getattr(e, "title", ""))
                link = getattr(e, "link", "")
                if not link: continue
                norm = normalise_url(link)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, link) and not is_intern(title):
                    jobs.append({"title": title, "company": "",
                                 "url": link, "source": "StablecoinJobs"})
            if jobs: return jobs
    r = get("https://www.stablecoin-jobs.com/")
    if not r: return jobs
    for a in soup(r).select("a[href*='/job/'], a[href*='/jobs/'], a[href*='/position/']"):
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://www.stablecoin-jobs.com" + href
        if "stablecoin-jobs.com" not in href: continue
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        title = clean(a.get_text())
        if is_real_job(title, href) and not is_intern(title):
            jobs.append({"title": title, "company": "", "url": href, "source": "StablecoinJobs"})
    return jobs


def scrape_beincrypto() -> list[dict]:
    """BeInCrypto jobs - try RSS then HTML."""
    jobs, seen_urls = [], set()
    for feed_url in [
        "https://beincrypto.com/jobs/feed/",
        "https://beincrypto.com/feed/",
    ]:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            for e in feed.entries:
                title = clean(getattr(e, "title", ""))
                link = getattr(e, "link", "")
                if not link or "beincrypto.com" not in link: continue
                if "/jobs/" not in link: continue
                norm = normalise_url(link)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, link) and not is_intern(title):
                    jobs.append({"title": title, "company": "",
                                 "url": link, "source": "BeInCrypto"})
            if jobs: return jobs
    r = get("https://beincrypto.com/jobs/")
    if not r: return jobs
    for a in soup(r).select("a[href*='/jobs/']"):
        title = clean(a.get_text())
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://beincrypto.com" + href
        if "beincrypto.com" not in href: continue
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        if is_real_job(title, href) and not is_intern(title):
            jobs.append({"title": title, "company": "", "url": href, "source": "BeInCrypto"})
    return jobs


def scrape_blockchainjobseurope() -> list[dict]:
    """BlockchainJobsEurope - it's actually a blog, skip it."""
    return []


def scrape_cryptojobshub() -> list[dict]:
    """CryptoJobsHub aggregator."""
    jobs, seen_urls = [], set()
    for feed_url in [
        "https://cryptojobshub.com/feed/",
        "https://cryptojobshub.com/rss/",
        "https://cryptojobshub.com/rss.xml",
        "https://www.cryptojobshub.com/feed/",
    ]:
        feed = feedparser.parse(feed_url)
        if feed.entries:
            for e in feed.entries:
                title = clean(getattr(e, "title", ""))
                link = getattr(e, "link", "")
                if not link: continue
                norm = normalise_url(link)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                if is_real_job(title, link) and not is_intern(title):
                    jobs.append({"title": title, "company": "", "url": link,
                                 "source": "CryptoJobsHub"})
            if jobs: return jobs
    for page_url in ["https://cryptojobshub.com", "https://cryptojobshub.com/jobs"]:
        r = get(page_url)
        if not r: continue
        for a in soup(r).select("a[href*='/job/'], a[href*='/jobs/'], a[href*='/position/']"):
            title = clean(a.get_text())
            href = a.get("href", "")
            if not href.startswith("http"):
                href = "https://cryptojobshub.com" + href
            if "cryptojobshub.com" not in href: continue
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            if is_real_job(title, href) and not is_intern(title):
                jobs.append({"title": title, "company": "", "url": href,
                             "source": "CryptoJobsHub"})
        if jobs: break
    return jobs


def scrape_blockchain_works() -> list[dict]:
    """Blockchain.works-hub job board."""
    jobs, seen_urls = [], set()
    r = get("https://blockchain.works-hub.com/jobs")
    if not r: return jobs
    for a in soup(r).select("a[href*='/jobs/']"):
        title = clean(a.get_text())
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://blockchain.works-hub.com" + href
        # Skip pagination and nav links
        if "page=" in href: continue
        if "search?" in href: continue
        if "hire.withgoogle" in href: continue
        # Must be a job slug (has alphanumeric ID at end)
        if not re.search(r"/jobs/[a-z0-9-]{5,}$", href): continue
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        # Skip titles that are nav items
        if not title or len(title) < 5: continue
        if title.lower() in {"careers", "show more jobs", "view all jobs"}: continue
        if is_real_job(title, href) and not is_intern(title):
            jobs.append({"title": title, "company": "", "url": href,
                         "source": "BlockchainWorks"})
    return jobs


def scrape_builtin_web3() -> list[dict]:
    """Built In web3 jobs section."""
    jobs, seen_urls = [], set()
    r = get("https://builtin.com/jobs/web3")
    if not r: return jobs
    for a in soup(r).select("a[href*='/job/']"):
        title = clean(a.get_text())
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://builtin.com" + href
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        if is_real_job(title, href) and not is_intern(title):
            # Try to get company from parent
            company = ""
            parent = a.find_parent(class_=re.compile(r"company|employer|org"))
            if parent:
                company = clean(parent.get_text())[:50]
            jobs.append({"title": title, "company": company, "url": href,
                         "source": "BuiltIn"})
    return jobs


# (duplicate scrape_crypto_jobs_ch removed)


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
    """Jobs are server-rendered in HTML under category paths like /engineering/slug/"""
    jobs, seen_urls = [], set()
    categories = ["engineering", "marketing", "sales", "operations",
                  "product", "design", "finance", "non-tech", "other"]
    for cat in categories:
        r = get(f"https://cryptocurrencyjobs.co/{cat}/")
        if not r: continue
        s = soup(r)
        for a in s.select("a[href]"):
            href = a.get("href", "")
            # Job links match /<category>/<company-job-slug>/
            if not re.match(r"^/" + cat + r"/.+/$", href):
                continue
            full_href = "https://cryptocurrencyjobs.co" + href
            norm = normalise_url(full_href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            # Title is in an h2 or h3 inside the card
            title = ""
            heading = a.find(["h2", "h3"])
            if heading:
                title = clean(heading.get_text())
            if not title:
                title = clean(a.get_text())
            # Company is usually in a sub-heading
            company = ""
            sub = a.find(["h3", "h4", "p"])
            if sub and sub != heading:
                company = clean(sub.get_text())
            if is_real_job(title, full_href):
                jobs.append({"title": title, "company": company,
                             "url": full_href, "source": "CryptocurrencyJobs"})
        time.sleep(0.5)
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
    """DeFi.jobs - Webflow site, job links contain /jobs/ with a slug."""
    jobs, seen_urls = [], set()
    r = get("https://www.defi.jobs/")
    if not r: return jobs
    s = soup(r)
    for a in s.select("a[href*='/jobs/']"):
        href = a.get("href", "")
        # Must be a real job slug, not just /jobs
        if not re.search(r"/jobs/[a-z0-9][a-z0-9-]{4,}", href):
            continue
        if not href.startswith("http"):
            href = "https://www.defi.jobs" + href
        # Check title BEFORE deduping — each card has two <a> tags for the same URL,
        # the first with no text. If we dedup on the empty one, the real title is lost.
        title = clean(a.get_text())
        if not title or len(title) < 5:
            continue
        norm = normalise_url(href)
        if norm in seen_urls: continue
        seen_urls.add(norm)
        # Strip trailing dedup numbers e.g. " 3", " 9"
        title = re.sub(r"\s+\d+$", "", title).strip()
        if is_real_job(title, href):
            jobs.append({"title": title, "company": "", "url": href,
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

        # Clean title: strip appended company names from HashtagWeb3
        raw = clean(a.get_text())
        title = raw

        # Strip appended domain names e.g. "moonshot.money"
        title = re.sub(r"\s+\w+\.\w+$", "", title).strip()

        # Strip number-prefixed company names like "3Box Labs"
        title = re.sub(r"\s*\d+[A-Z][A-Za-z\s]+$", "", title).strip()

        # Strip known company names appended directly (with or without space)
        # Use a broad pattern: if the title ends with a known company name, strip it
        company_suffixes = [
            "Uniswap", "UniSwap", "Anchorage Digital", "Anchorage",
            "Ashby", "Crossmint", "Coinbase", "Fireblocks", "Phantom",
            "Lido", "VALR", "Binance", "Ripple", "Circle", "Alchemy",
            "LayerZero", "Offchain Labs", "Consensys", "Eigen", "EigenLayer",
            "Gensyn", "Matrixport", "Nansen", "Range", "Veda", "Breeze",
            "Paradigm", "Chainalysis", "Polygon Labs", "Polygon", "BitGo",
            "Bitgo", "Lightspark", "Sky Mavis", "Aave", "Mysten Labs",
            "Solana Foundation", "Sui Foundation", "OP Labs", "OPLabs",
            "Spade", "Method", "Walrus Foundation", "Crux", "Avalabs",
            "Ava Labs", "Bastion", "OpenSea", "Opensea", "Sardine",
            "Jeeves", "LayerZero Labs", "Aptos Labs", "Aptoslabs",
            "Kast", "Helius", "Anza", "Wintermute", "Worldcoin", "World",
            "StreamingFast", "Streamingfast", "Kalshi", "Ondo Finance",
            "Talos", "Monad Foundation", "Nomic Foundation", "1inch Network",
            "1Inch Network", "Ripple", "Multicoin Capital", "Dragonfly",
            "Pantera Capital", "Shima Capital", "3Box Labs", "Ceramic",
            "BCB Group", "VALR", "Immutable", "Polygon", "Starkware",
            "Scroll", "Celestia", "Wormhole", "Axelar", "dYdX",
            "Bitso", "Morpho Labs", "Morpho", "Blackbird", "M^0 Labs",
            "Coinbase", "Kraken", "Gemini", "Galaxy", "Offchain Labs",
            "CoinSwitch Kuber", "CoinSwitch", "Sovrun", "Pixion Games",
            "Blockworks", "Magic Eden", "Sei Labs", "Li Fi", "Mesh",
            "Figment", "Temporal", "Lever", "Flow Blockchain",
            "OpenZeppelin", "Addressable",
        ]
        for suffix in company_suffixes:
            if title.endswith(suffix):
                title = title[:-len(suffix)].strip()
                break

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
    """BlockchainHeadhunter - try multiple approaches to get jobs."""
    jobs, seen_urls = [], set()
    skip_paths = {"/for-companies", "/news", "/about", "/contact",
                  "/blog", "/education", "/jobs", "/sponsored",
                  "/apply", "/submit-cv"}
    # Try sitemap variants
    for sitemap in ["https://blockchainheadhunter.com/sitemap.xml",
                     "https://blockchainheadhunter.com/sitemap_index.xml",
                     "https://blockchainheadhunter.com/page-sitemap.xml"]:
        r = get(sitemap)
        if not r: continue
        urls = re.findall(r"<loc>(https://blockchainheadhunter\.com/[^<]+)</loc>", r.text)
        if not urls: continue
        for href in urls:
            path = "/" + href.split("blockchainheadhunter.com/")[-1].rstrip("/")
            if any(path.startswith(s) for s in skip_paths): continue
            if href.count("/") < 4: continue  # must have a slug
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            slug = href.rstrip("/").split("/")[-1]
            title = re.sub(r"-\d+$", "", slug).replace("-", " ").title()
            if is_real_job(title, href):
                jobs.append({"title": title, "company": "", "url": href,
                             "source": "BlockchainHeadhunter"})
        if jobs: break
    # HTML fallback with broad selector but strict filtering
    if not jobs:
        r = get("https://blockchainheadhunter.com/jobs")
        if r:
            s = soup(r)
            for a in s.select("a[href]"):
                href = a.get("href", "")
                # Skip mailto, anchors, external links
                if href.startswith("mailto:") or href.startswith("#"): continue
                if not href.startswith("http"):
                    href = "https://blockchainheadhunter.com" + href
                if "blockchainheadhunter.com" not in href: continue
                # Skip legal/nav pages
                path = "/" + href.split("blockchainheadhunter.com/")[-1].rstrip("/")
                if any(path.startswith(sp) for sp in skip_paths): continue
                if "/legal/" in path: continue
                # Must look like a job slug: /word-word-word (at least 2 hyphens)
                if path.count("-") < 2: continue
                norm = normalise_url(href)
                if norm in seen_urls: continue
                seen_urls.add(norm)
                title = clean(a.get_text())
                # Skip nav-style short titles
                if not title or len(title) < 8: continue
                if title.lower() in {"submit your cv", "email", "terms and conditions",
                                     "privacy policy", "geobot", "post sponsored job"}:
                    continue
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
# Big companies - still shown but separated into their own section
# ---------------------------------------------------------------------------

BIG_COMPANIES = {
    "coinbase", "kraken", "binance", "gemini", "crypto.com", "crypto com",
    "okx", "bitfinex", "bitmex", "robinhood", "ripple", "circle",
    "galaxy", "galaxy digital", "jpmorgan", "jpmorgan chase co",
    "goldman sachs", "deloitte", "amazon", "amazon web services",
    "coindesk", "consensys", "chainalysis", "fireblocks", "anchorage",
    "anchorage digital", "bitgo", "ledger", "trezor", "polygon labs",
    "chainlink labs", "stellar development foundation", "ethereum foundation",
    "solana foundation", "sui foundation", "ava labs", "aptos labs",
    "animoca brands", "grayscale", "ark invest", "bitwise",
    "trm labs", "elliptic", "nansen", "messari",
}

# ---------------------------------------------------------------------------
# Direct company careers pages
# ---------------------------------------------------------------------------

CRYPTO_COMPANIES_GREENHOUSE = [
    # Only companies confirmed working via JSON API
    # (companies that returned 0 and are already on Ashby have been removed)
    ("Fireblocks",      "fireblocks"),
    ("Alchemy",         "alchemy"),
    ("BitGo",           "bitgo"),
    ("Gensyn",          "gensyn"),
    ("Aptos Labs",      "aptoslabs"),
    ("Ava Labs",        "avalabs"),
    ("LayerZero Labs",  "layerzerolabs"),
    ("Coinbase",        "coinbase"),
    ("Ripple",          "ripple"),
    ("Gemini",          "gemini"),
    ("Nansen",          "nansen"),
    ("M^0 Labs",        "m0dbathenextthingltd"),
    ("Kalshi",          "kalshi"),
]

CRYPTO_COMPANIES_ASHBY = [
    # Native Ashby boards
    ("Uniswap",             "uniswap"),
    ("Lightspark",          "lightspark"),
    ("Sky Mavis",           "skymavis"),
    ("OP Labs",             "oplabs"),
    ("Phantom",             "phantom"),
    ("Lido",                "lido.fi"),
    ("OpenSea",             "opensea"),
    ("EigenLayer",          "eigen-labs"),
    ("Mysten Labs",         "mystenlabs"),
    ("Bastion",             "bastion"),
    ("Sardine",             "sardine"),
    ("Method",              "method"),
    ("Walrus Foundation",   "walrus"),
    ("Solana Foundation",   "solana%20foundation"),
    ("Sui Foundation",      "sui%20foundation"),
    ("Chainalysis",         "chainalysis-careers"),
    ("Nomic Foundation",    "nomic.foundation"),
    ("Crux",                "cruxclimate"),
    ("Monad Foundation",    "monad.foundation"),
    ("Tools for Humanity",  "tools%20for%20humanity"),
    ("Paradigm",            "paradigm"),
    ("Talos",               "talos-trading"),
    ("Ondo Finance",        "ondo-finance"),
    ("Blackbird",           "blackbird-labs-inc"),
    ("Privy",               "privy"),
    ("Turnkey",             "turnkey"),
    ("Blockaid",            "blockaid"),
    # VC funds that moved from Getro to Ashby
    ("Multicoin Capital",   "multicoin-capital"),
    ("Pantera Capital",     "pantera"),
    ("Variant Fund",        "variant"),
    ("Framework Ventures",  "framework-ventures"),
    ("Outlier Ventures",    "outlierventures"),
    ("Lemniscap",           "lemniscap"),
    ("Fabric VC",           "fabric"),
    ("a16z Crypto",         "a16z-crypto"),
]


def scrape_company_greenhouse(company: str, slug: str) -> list[dict]:
    """Scrape a company's Greenhouse job board via JSON API."""
    jobs = []
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not r:
        return jobs
    try:
        data = r.json()
    except Exception:
        return jobs
    for job in data.get("jobs", []):
        title = clean(str(job.get("title", "")))
        url = job.get("absolute_url", "")
        loc = job.get("location", {})
        location = loc.get("name", "") if isinstance(loc, dict) else ""
        if not url or not is_real_job(title, url):
            continue
        if is_intern(title):
            continue
        jobs.append({
            "title": title,
            "company": company,
            "url": url,
            "location": location,
            "source": "Direct",
        })
    return jobs


def scrape_company_ashby(company: str, slug: str) -> list[dict]:
    """Scrape an Ashby job board via embedded window.__appData JSON."""
    import urllib.parse
    jobs = []
    r = get(f"https://jobs.ashbyhq.com/{slug}")
    if not r:
        return jobs
    # Ashby is React-rendered; job data is embedded in a <script> tag as
    # window.__appData = { ..., "jobBoard": { "jobPostings": [...] } }
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL)
    for script in scripts:
        if "jobPostings" not in script:
            continue
        try:
            # Extract the outermost JSON object by counting braces
            start = script.index("{")
            depth = 0
            end = start
            for i, c in enumerate(script[start:], start):
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            data = json.loads(script[start:end])
            postings = data.get("jobBoard", {}).get("jobPostings", [])
            decoded_slug = urllib.parse.unquote(slug)
            for job in postings:
                if not job.get("isListed", True):
                    continue
                title = clean(str(job.get("title", "")))
                job_id = job.get("id", "")
                if not title or not job_id:
                    continue
                if is_intern(title):
                    continue
                # Keep slug URL-safe: replace %20 (encoded spaces) with hyphens
                url_slug = slug.replace("%20", "-")
                url = f"https://jobs.ashbyhq.com/{url_slug}/{job_id}"
                location = job.get("locationName", "") or ""
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": url,
                    "location": location,
                    "source": "Direct",
                })
            break  # found the right script block
        except Exception:
            continue
    return jobs


def scrape_wellfound() -> list[dict]:
    """Wellfound - try role-specific crypto searches."""
    jobs, seen_urls = [], set()
    # Wellfound blocks generic scrapers but specific role searches sometimes work
    searches = [
        "https://wellfound.com/role/r/blockchain-engineer",
        "https://wellfound.com/role/r/web3",
        "https://wellfound.com/role/r/defi",
        "https://wellfound.com/role/r/crypto",
    ]
    for url in searches:
        r = get(url)
        if not r: continue
        s = soup(r)
        for a in s.select("a[href*='/jobs/']"):
            title = clean(a.get_text())
            href = a.get("href", "")
            if not href.startswith("http"):
                href = "https://wellfound.com" + href
            if "wellfound.com" not in href: continue
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            if is_real_job(title, href) and not is_intern(title):
                jobs.append({"title": title, "company": "",
                             "url": href, "source": "Wellfound"})
        time.sleep(1)
    return jobs


def scrape_workatastartup() -> list[dict]:
    """Y Combinator Work at a Startup - crypto/blockchain filter."""
    jobs, seen_urls = [], set()
    urls = [
        "https://www.workatastartup.com/jobs?industry=crypto",
        "https://www.workatastartup.com/jobs?industry=blockchain",
        "https://www.workatastartup.com/jobs?q=crypto",
        "https://www.workatastartup.com/jobs?q=web3",
    ]
    for page_url in urls:
        r = get(page_url)
        if not r: continue
        s = soup(r)
        for a in s.select("a[href*='/jobs/']"):
            title = clean(a.get_text())
            href = a.get("href", "")
            if not href.startswith("http"):
                href = "https://www.workatastartup.com" + href
            if "workatastartup.com" not in href: continue
            norm = normalise_url(href)
            if norm in seen_urls: continue
            seen_urls.add(norm)
            if is_real_job(title, href) and not is_intern(title):
                company = ""
                parent = a.find_parent()
                if parent:
                    for sib in parent.find_all(string=True):
                        t = sib.strip()
                        if t and t != title and 2 < len(t) < 60:
                            company = t
                            break
                jobs.append({"title": title, "company": company,
                             "url": href, "source": "YC Startups"})
        time.sleep(1)
    return jobs


def scrape_direct_companies() -> list[dict]:
    """Scrape all known crypto company job boards directly."""
    all_jobs = []
    print(f"→ Direct company boards ({len(CRYPTO_COMPANIES_GREENHOUSE + CRYPTO_COMPANIES_ASHBY)} companies)...", file=sys.stderr)

    for company, slug in CRYPTO_COMPANIES_GREENHOUSE:
        try:
            jobs = scrape_company_greenhouse(company, slug)
            all_jobs.extend(jobs)
        except Exception as e:
            print(f"  [WARN] {company}: {e}", file=sys.stderr)
        time.sleep(0.3)

    for company, slug in CRYPTO_COMPANIES_ASHBY:
        try:
            jobs = scrape_company_ashby(company, slug)
            all_jobs.extend(jobs)
        except Exception as e:
            print(f"  [WARN] {company}: {e}", file=sys.stderr)
        time.sleep(0.3)

    return all_jobs


def is_intern(title: str) -> bool:
    """Filter out intern roles."""
    t = title.lower()
    return any(x in t for x in [
        "intern", "internship", "co-op", "coop",
        "student", "graduate program", "apprentice",
        "fellowship", "phd fellowship",
    ])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SCRAPERS = [
    # Pure web3 job boards
    scrape_ethereumjobboard,
    scrape_talentweb3,
    scrape_myweb3jobs,
    scrape_defi_jobs,
    # scrape_hashtagweb3  — fully JS-rendered, 0 links in HTML, needs headless browser
    # VC portfolio boards (Getro-powered — confirmed working)
    scrape_safary,
    scrape_bitkraft,
    scrape_delphi,
    scrape_galaxy_vc,
    scrape_jump,
    scrape_polychain,
    scrape_coinfund,
    scrape_electric,
    scrape_dragonfly,
    scrape_blockchain_assoc,
    # Ecosystem boards (Getro-powered — confirmed working)
    scrape_solana_jobs,
    # Aggregators
    scrape_cryptojobslist,
    scrape_cryptocurrencyjobs,
    scrape_remote3,
    scrape_web3career,
    # Direct company boards (Greenhouse JSON API + Ashby appData)
    scrape_direct_companies,
    # Additional boards
    scrape_blockchain_works,
    scrape_builtin_web3,
    scrape_crypto_jobs_ch,
    scrape_venturecapitalcareers,
    scrape_defi_jobs_xyz,
    scrape_cryptojobs_com,
    # Removed (broken/blocked):
    # scrape_bitcoinerjobs   — API now requires key
    # scrape_bitcoinjobs     — silent 0, site restructured
    # scrape_stablecoin_jobs — domain offline
    # scrape_blockchainheadhunter — all sitemaps 404
    # scrape_jobstash        — DNS failure (domain gone)
    # scrape_cryptodotjobs   — 500 Internal Server Error
    # scrape_cryptojobshub   — 502 Bad Gateway
    # scrape_wellfound       — 403 Forbidden
    # scrape_beincrypto      — 403 Forbidden
    # scrape_workatastartup  — 0 results, JS-rendered
    # scrape_blockchainjobseurope — 0 results
    # scrape_multicoin/framework/outlier/variant/pantera/lemniscap/fabric_vc
    #   — Getro now blocks scraping; these funds now in CRYPTO_COMPANIES_ASHBY
    # scrape_avax/scrape_ton — Getro blocks; Ashby boards have 0 jobs currently
    # scrape_a16z_crypto     — moved to CRYPTO_COMPANIES_ASHBY
    # scrape_base_hirechain  — different platform (hirechain.io), no public API
]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(reset: bool = False) -> list[dict]:
    seen = set() if reset else load_seen()

    # Global URL dedup across all boards in this run
    this_run_urls: set[str] = set()

    all_new: list[dict] = []

    print(f"\n{'='*55}", file=sys.stderr)
    print(f"  Web3 Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    print(f"  Seen jobs on record: {len(seen)}", file=sys.stderr)
    print(f"{'='*55}\n", file=sys.stderr)

    for fn in SCRAPERS:
        name = fn.__name__.replace("scrape_", "")
        print(f"→ {name}...", file=sys.stderr)
        try:
            jobs = fn()
        except Exception as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            jobs = []

        new = []
        for job in jobs:
            norm = normalise_url(job.get("url", ""))
            jid  = make_seen_id(job["title"], job.get("company", ""), norm)

            # Filter out obvious non-web3 jobs
            if not is_web3_relevant(job):
                continue

            # Filter out intern roles
            if is_intern(job.get("title", "")):
                continue

            # Skip if seen in a previous run OR already seen in this run
            if jid in seen or norm in this_run_urls:
                continue

            seen.add(jid)
            this_run_urls.add(norm)
            new.append(job)

        print(f"  {len(jobs)} found, {len(new)} new (after global dedup)", file=sys.stderr)
        all_new.extend(new)
        time.sleep(REQUEST_DELAY)

    save_seen(seen)

    # ---------------------------------------------------------------------------
    # Slack output
    # ---------------------------------------------------------------------------

    if not all_new:
        print(f"<b>Web3 Jobs — {datetime.now().strftime('%d %b %Y')}</b>\nNo new jobs since last run.")
        return all_new

    def format_job_block(job: dict) -> list:
        title   = html.escape(job.get("title", "").strip())
        company = html.escape(apply_company_fixes(job.get("company", "").strip()))
        loc     = html.escape(clean_location(job.get("location", "")))
        sal     = html.escape(job.get("salary", "").strip())
        url     = job.get("url", "").strip()
        display_url = clean_display_url(url)
        if "linkedin.com" in display_url:
            m = re.search(r"(https://[a-z.]*linkedin\.com/jobs/view/[a-z0-9-]+-\d+)", url)
            if m:
                display_url = m.group(1)
        block = [f"<b>{title}</b>"]
        if company:
            block.append(f"🏢 {company}")
        if sal:
            block.append(f"💰 {sal}")
        if loc:
            block.append(f"📍 {loc}")
        block.append(f"🔗 {display_url}")
        block.append("")
        return block

    # Split into regular jobs and big company jobs
    regular_jobs, big_co_jobs = [], []
    for job in all_new:
        company_lower = apply_company_fixes(job.get("company", "")).lower().strip()
        if company_lower in BIG_COMPANIES:
            big_co_jobs.append(job)
        else:
            regular_jobs.append(job)

    # Output 1: Regular jobs (smaller / more targeted companies)
    if regular_jobs:
        lines = [
            f"🆕 <b>Web3 Jobs — {datetime.now().strftime('%d %b %Y %H:%M')}</b>",
            f"<i>{len(regular_jobs)} new jobs</i>",
            "",
        ]
        for job in regular_jobs:
            lines.extend(format_job_block(job))
        print("\n".join(lines))

    # Output 2: Big company jobs as a clearly labelled separate block
    if big_co_jobs:
        big_lines = [
            f"🏦 <b>Big Co Jobs — {datetime.now().strftime('%d %b %Y %H:%M')}</b>",
            f"<i>{len(big_co_jobs)} roles from larger companies</i>",
            "",
        ]
        for job in big_co_jobs:
            big_lines.extend(format_job_block(job))
        print("\n".join(big_lines))

    return all_new


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    run(reset=args.reset)
