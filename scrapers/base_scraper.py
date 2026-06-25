"""
ReviewLedger · Universal Base Scraper
All platform scrapers inherit from this.
Uses Playwright for full JS rendering — handles any modern web page.
"""

import re
import time
import random
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


@dataclass
class ScrapedReview:
    """Universal review object — platform-agnostic."""
    review_id:       str
    platform:        str        # e.g. "google", "bbb", "yelp"
    platform_url:    str
    competitor_name: str
    competitor_slug: str
    rating:          float      # 1.0 – 5.0
    title:           str
    body:            str
    author:          str
    author_role:     Optional[str]
    author_company:  Optional[str]
    review_date:     datetime
    scraped_at:      datetime = field(default_factory=datetime.utcnow)
    raw_hash:        str = ""


def make_review_id(platform: str, slug: str, author: str, date: str, body: str) -> str:
    raw = f"{platform}:{slug}:{author}:{date}:{body[:80]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def make_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()


def parse_rating(raw: str) -> float:
    if not raw:
        return 0.0
    raw = str(raw).strip()
    m = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', raw, re.I)
    if m:
        return min(5.0, float(m.group(1)))
    stars = raw.count('★') + raw.count('⭐')
    if stars:
        return float(stars)
    m = re.search(r'(\d+)%', raw)
    if m:
        return round(float(m.group(1)) / 20, 1)
    m = re.search(r'(\d+\.?\d*)', raw)
    if m:
        val = float(m.group(1))
        return min(5.0, val) if val <= 5 else min(5.0, val / 2)
    return 0.0


def parse_date(raw: str) -> datetime:
    raw = clean_text(str(raw))
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
        "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y",
        "%d %B %Y", "%d/%m/%Y",
    ]:
        try:
            return datetime.strptime(raw[:26], fmt)
        except ValueError:
            continue
    # Relative: "2 weeks ago", "3 months ago"
    m = re.search(r'(\d+)\s+(day|week|month|year)s?\s+ago', raw, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=n * 30),
            "year": timedelta(days=n * 365),
        }.get(unit, timedelta(0))
        return datetime.utcnow() - delta
    return datetime.utcnow()


def get_playwright_page(context, url: str, wait: float = 2.5, scroll: bool = True):
    """Open a URL in Playwright, wait for render, optionally scroll."""
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(random.uniform(wait * 0.6, wait * 0.9))
        if scroll:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            time.sleep(0.4)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.4)
        return page
    except Exception as e:
        logger.warning("Page load error for %s: %s", url, e)
        try:
            page.close()
        except Exception:
            pass
        return None
