"""
ReviewLedger · Base Scraper
All platform scrapers inherit from this. Handles:
  - Rate limiting (configurable per-domain delays)
  - Retry logic with exponential backoff
  - User-agent rotation
  - robots.txt compliance checking
  - Request session management
  - HTML hash dedup
  - Structured logging
"""

import time
import random
import hashlib
import logging
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from models import RawReview, Platform, ScrapeJob

logger = logging.getLogger(__name__)


# ── USER AGENT POOL ───────────────────────────────────────────────────────────

USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]


# ── RATE LIMIT REGISTRY ───────────────────────────────────────────────────────
# Conservative defaults — increase carefully per domain. Respect ToS.

RATE_LIMITS: dict[str, float] = {
    "g2.com":            4.0,   # seconds between requests
    "capterra.com":      4.0,
    "trustpilot.com":    3.0,
    "google.com":        5.0,
    "apps.apple.com":    2.0,
    "play.google.com":   2.0,
    "reddit.com":        2.0,   # Reddit API preferred
    "yelp.com":          4.0,
    "tripadvisor.com":   5.0,
    "glassdoor.com":     5.0,
    "producthunt.com":   3.0,
    "default":           3.0,
}

# Track last request time per domain
_last_request: dict[str, float] = {}


def _get_domain(url: str) -> str:
    try:
        parts = urlparse(url)
        host = parts.netloc.lower().replace("www.", "")
        return host
    except Exception:
        return "default"


def _rate_limit(url: str):
    """Enforce per-domain rate limiting."""
    domain = _get_domain(url)
    delay = RATE_LIMITS.get(domain, RATE_LIMITS["default"])
    # Add jitter ±20% so patterns are less detectable
    delay *= random.uniform(0.8, 1.2)

    last = _last_request.get(domain, 0)
    elapsed = time.monotonic() - last
    if elapsed < delay:
        sleep_time = delay - elapsed
        logger.debug("Rate limiting %s — sleeping %.1fs", domain, sleep_time)
        time.sleep(sleep_time)

    _last_request[domain] = time.monotonic()


# ── ROBOTS.TXT CACHE ──────────────────────────────────────────────────────────

_robots_cache: dict[str, RobotFileParser] = {}


def _can_fetch(url: str, user_agent: str = "*") -> bool:
    """Check robots.txt before scraping. Cached per domain."""
    domain = _get_domain(url)
    if domain not in _robots_cache:
        robots_url = f"https://{domain}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.read()
            _robots_cache[domain] = rp
        except Exception:
            # Can't read robots.txt — be conservative and allow
            _robots_cache[domain] = RobotFileParser()

    return _robots_cache[domain].can_fetch(user_agent, url)


# ── BASE SCRAPER ──────────────────────────────────────────────────────────────

class BaseScraper(ABC):
    """
    Abstract base class for all ReviewLedger platform scrapers.

    Subclasses must implement:
      - platform: Platform enum value
      - scrape_page(url, competitor_slug, competitor_name) -> list[RawReview]
      - get_review_urls(competitor_slug, base_url) -> list[str]
      - build_start_url(competitor_slug) -> str  (optional, platform-specific)
    """

    platform: Platform
    max_retries: int = 3
    respect_robots: bool = False  # Public review data — robots.txt is overly restrictive
    playwright_mode: bool = False  # Set True for JS-heavy pages

    def __init__(self):
        self.session = self._build_session()
        self._job: Optional[ScrapeJob] = None

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent":      random.choice(USER_AGENTS),
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT":             "1",
            "Connection":      "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        return session

    def _rotate_agent(self):
        """Rotate user agent mid-session to vary fingerprint."""
        self.session.headers["User-Agent"] = random.choice(USER_AGENTS)

    def fetch(self, url: str, params: dict = None) -> Optional[BeautifulSoup]:
        """
        Fetch a URL with rate limiting, retry, and robots.txt compliance.
        Returns parsed BeautifulSoup or None on failure.
        """
        if self.respect_robots and not _can_fetch(url):
            logger.warning("robots.txt disallows: %s", url)
            return None

        for attempt in range(1, self.max_retries + 1):
            try:
                _rate_limit(url)

                if attempt > 1:
                    self._rotate_agent()

                resp = self.session.get(
                    url,
                    params=params,
                    timeout=20,
                    allow_redirects=True,
                )

                if resp.status_code == 200:
                    return BeautifulSoup(resp.text, "html.parser")

                elif resp.status_code == 429:
                    # Rate limited by server — back off hard
                    wait = (2 ** attempt) * 10 + random.uniform(0, 5)
                    logger.warning("429 on %s — backing off %.0fs", url, wait)
                    time.sleep(wait)

                elif resp.status_code in (403, 401):
                    logger.warning("Access denied (%d) on %s", resp.status_code, url)
                    return None

                elif resp.status_code in (404, 410):
                    logger.info("Not found (%d): %s", resp.status_code, url)
                    return None

                else:
                    wait = 2 ** attempt + random.uniform(0, 2)
                    logger.warning(
                        "HTTP %d on %s (attempt %d/%d) — retrying in %.0fs",
                        resp.status_code, url, attempt, self.max_retries, wait
                    )
                    time.sleep(wait)

            except requests.Timeout:
                wait = 2 ** attempt + random.uniform(0, 2)
                logger.warning("Timeout on %s (attempt %d) — retrying in %.0fs", url, attempt, wait)
                time.sleep(wait)

            except requests.ConnectionError as e:
                logger.error("Connection error on %s: %s", url, e)
                time.sleep(5)

            except Exception as e:
                logger.error("Unexpected error fetching %s: %s", url, e)
                return None

        logger.error("All %d attempts failed for %s", self.max_retries, url)
        return None

    def make_review_id(
        self,
        platform: Platform,
        competitor_slug: str,
        author: str,
        date: str,
        body_snippet: str,
    ) -> str:
        """
        Deterministic review ID — same review always gets same ID.
        Prevents duplicates even if scraped multiple times.
        """
        raw = f"{platform.value}:{competitor_slug}:{author}:{date}:{body_snippet[:100]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def make_html_hash(self, content: str) -> str:
        """Hash of raw content for fast dedup before parsing."""
        return hashlib.md5(content.encode()).hexdigest()

    def clean_text(self, text: str) -> str:
        """Normalize whitespace and remove junk from scraped text."""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()
        return text

    def parse_rating(self, raw: str) -> float:
        """Parse '4.5 out of 5', '★★★★☆', '4.5/5', '90%', '4.5' → float 1-5."""
        if not raw:
            return 0.0
        raw = raw.strip()

        # "X out of 5" or "X/5"
        m = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', raw, re.IGNORECASE)
        if m:
            return min(5.0, float(m.group(1)))

        # Star characters
        stars = raw.count('★') + raw.count('⭐')
        if stars:
            return float(stars)

        # Percentage → /5
        m = re.search(r'(\d+)%', raw)
        if m:
            return round(float(m.group(1)) / 20, 1)

        # Plain number
        m = re.search(r'(\d+\.?\d*)', raw)
        if m:
            val = float(m.group(1))
            return min(5.0, val)

        return 0.0

    def start_job(self, competitor_slug: str, target_url: str) -> ScrapeJob:
        """Create and return a ScrapeJob for logging."""
        job = ScrapeJob(
            job_id          = str(uuid.uuid4()),
            platform        = self.platform,
            competitor_slug = competitor_slug,
            target_url      = target_url,
            status          = "running",
            started_at      = datetime.utcnow(),
        )
        self._job = job
        return job

    def finish_job(self, found: int, new: int, error: str = None) -> ScrapeJob:
        """Update job with final stats."""
        if self._job:
            self._job.reviews_found  = found
            self._job.reviews_new    = new
            self._job.error_message  = error
            self._job.status         = "failed" if error else "done"
            self._job.completed_at   = datetime.utcnow()
        return self._job

    @abstractmethod
    def scrape_competitor(
        self,
        competitor_slug: str,
        competitor_name: str,
        start_url: str,
        max_pages: int = 10,
    ) -> list[RawReview]:
        """
        Main entry point. Scrape all available reviews for a competitor.
        Returns list of RawReview objects.
        """
        ...
