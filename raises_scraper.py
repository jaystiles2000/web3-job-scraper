#!/usr/bin/env python3
"""
Crypto Raises Scraper
======================
Monitors crypto news RSS feeds for funding round announcements.
Sends new raises to Telegram every 2 hours.

Requirements:
    pip install requests beautifulsoup4 feedparser lxml
"""

import json
import re
import time
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import feedparser

SEEN_RAISES_FILE = Path(__file__).parent / "seen_raises.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# Keywords that indicate a funding/raise article
# ---------------------------------------------------------------------------

# Must contain at least one of these SPECIFIC funding phrases
RAISE_KEYWORDS = [
    "raises $", "raised $", "secures $", "secured $",
    "closes $", "closed $", "funding round", "seed round",
    "series a", "series b", "series c", "series d",
    "pre-seed", "raises funding", "raised funding",
    "capital raise", "venture round", "investment round",
    "million round", "billion round", "led by", "co-led by",
    "raises million", "raises billion",
]

# Keywords that indicate it's NOT a raise
NOISE_KEYWORDS = [
    "lawsuit", "layoffs", "hack", "exploit", "scam", "rug pull",
    "price", "market cap", "trading volume", "exchange listing",
    "airdrop", "arrested", "fraud", "penalty", "fine", "sec charges",
]

# ---------------------------------------------------------------------------
# RSS Feed sources
# ---------------------------------------------------------------------------

FEEDS = [
    {
        "name": "The Block",
        "url": "https://www.theblock.co/rss.xml",
    },
    {
        "name": "Decrypt",
        "url": "https://decrypt.co/feed",
    },
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    },
    {
        "name": "DLNews",
        "url": "https://www.dlnews.com/rss/",
    },
    {
        "name": "Blockworks",
        "url": "https://blockworks.co/feed",
    },
    {
        "name": "The Defiant",
        "url": "https://thedefiant.io/api/feed",
    },
    {
        "name": "CryptoSlate",
        "url": "https://cryptoslate.com/feed/",
    },
    {
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss/tag/investments",
    },
]

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_RAISES_FILE.exists():
        with open(SEEN_RAISES_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_RAISES_FILE, "w") as f:
        json.dump(list(seen), f, indent=2)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_raise_article(title: str, summary: str = "") -> bool:
    """Check if an article is about a funding round."""
    text = (title + " " + summary).lower()

    # Must contain at least one raise keyword
    has_raise = any(kw in text for kw in RAISE_KEYWORDS)
    if not has_raise:
        return False

    # Skip if it's just price/market noise
    noise_count = sum(1 for kw in NOISE_KEYWORDS if kw in text)
    if noise_count >= 2:
        return False

    return True

def extract_amount(title: str, summary: str = "") -> str:
    """Try to extract the raise amount from the title/summary."""
    text = title + " " + summary
    # Look for patterns like "$10M", "$10 million", "10 million", "$10B"
    patterns = [
        r'\$[\d,.]+\s*(?:million|billion|M|B)\b',
        r'[\d,.]+\s*(?:million|billion)\s*(?:dollar|USD)',
        r'\$[\d,.]+[MB]\b',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return ""

def clean_title(t: str) -> str:
    t = re.sub(r"<[^>]+>", "", t)  # strip HTML
    t = re.sub(r"\s+", " ", t).strip()
    return t

# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def run(reset: bool = False) -> list[dict]:
    seen = set() if reset else load_seen()
    all_new = []

    print(f"\n{'='*55}", file=sys.stderr)
    print(f"  Raises Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    print(f"  Seen raises on record: {len(seen)}", file=sys.stderr)
    print(f"{'='*55}\n", file=sys.stderr)

    for feed_info in FEEDS:
        name = feed_info["name"]
        url = feed_info["url"]
        print(f"→ {name}...", file=sys.stderr)

        try:
            feed = feedparser.parse(url)
            entries = feed.entries
        except Exception as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            continue

        new_count = 0
        for entry in entries:
            title = clean_title(getattr(entry, "title", ""))
            link = getattr(entry, "link", "")
            summary = clean_title(getattr(entry, "summary", ""))

            if not title or not link:
                continue

            # Only process articles from the last 3 hours
            published = getattr(entry, "published_parsed", None)
            if published:
                pub_time = datetime(*published[:6], tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - pub_time
                if age > timedelta(hours=3):
                    continue

            if not is_raise_article(title, summary):
                continue

            # Dedup by URL
            entry_id = re.sub(r"[^a-z0-9]", "", link.lower())[:80]
            if entry_id in seen:
                continue

            seen.add(entry_id)
            amount = extract_amount(title, summary)

            all_new.append({
                "title": title,
                "source": name,
                "url": link,
                "amount": amount,
                "summary": summary[:200] if summary else "",
            })
            new_count += 1

        print(f"  {len(entries)} articles checked, {new_count} new raises", file=sys.stderr)
        time.sleep(0.5)

    save_seen(seen)

    # ---------------------------------------------------------------------------
    # Format output
    # ---------------------------------------------------------------------------

    if not all_new:
        print(f"💼 No new raises since last run.")
        return all_new

    lines = [
        f"💰 <b>Crypto Raises — {datetime.now().strftime('%d %b %Y %H:%M')}</b>",
        f"<i>{len(all_new)} new raises found</i>",
        "",
    ]

    for raise_ in all_new:
        # Clean URL - strip tracking params
        clean_url = raise_["url"].split("?utm_")[0].split("&utm_")[0]
        
        block = [f"<b>{raise_['title']}</b>"]
        if raise_["amount"]:
            block.append(f"💵 {raise_['amount']}")
        block.append(f"📰 {raise_['source']}")
        block.append(f"🔗 {clean_url}")
        block.append("")
        lines.extend(block)

    print("\n".join(lines))
    return all_new


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    run(reset=args.reset)
