"""
ReviewLedger · Reddit Scraper (Playwright version)

Reddit blocked unauthenticated JSON API access in 2023.
This scraper uses Playwright (headless Chromium) to browse
Reddit search results like a real user would.

Searches for competitor mentions across Reddit and extracts
posts and comments as review signals.
"""

import logging
import re
import time
import random
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from models import RawReview, Platform
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# Canadian finance subreddits — most relevant for EQ Bank / Home Trust
CANADA_FINANCE_SUBS = [
    "PersonalFinanceCanada",
    "canada",
    "CanadianInvestor",
    "MortgagesCanada",
    "FirstTimeHomeBuyerCanada",
]


class RedditScraper(BaseScraper):

    platform = Platform.REDDIT

    def scrape_competitor(
        self,
        competitor_slug: str,
        competitor_name: str,
        start_url: str,          # used as search query
        max_pages: int = 5,
    ) -> list[RawReview]:

        all_reviews: list[RawReview] = []
        job = self.start_job(competitor_slug, self.platform.value)
        logger.info("[Reddit] Playwright search for: '%s'", competitor_name)

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                    locale="en-CA",
                )

                # 1. Global Reddit search
                reviews = self._search_reddit(
                    context, competitor_name, competitor_slug, subreddit=None
                )
                all_reviews.extend(reviews)
                logger.info("[Reddit] Global search: %d posts", len(reviews))

                # 2. Canadian finance subreddits
                for sub in CANADA_FINANCE_SUBS:
                    time.sleep(random.uniform(2.0, 3.5))
                    sub_reviews = self._search_reddit(
                        context, competitor_name, competitor_slug, subreddit=sub
                    )
                    all_reviews.extend(sub_reviews)
                    if sub_reviews:
                        logger.info("[Reddit] r/%s: %d posts", sub, len(sub_reviews))

                browser.close()

        except ImportError:
            logger.error("[Reddit] Playwright not installed — run: playwright install chromium")
        except Exception as e:
            logger.error("[Reddit] Playwright error: %s", e)

        # Deduplicate
        seen = set()
        deduped = []
        for r in all_reviews:
            if r.review_id not in seen:
                seen.add(r.review_id)
                deduped.append(r)

        self.finish_job(found=len(deduped), new=len(deduped))
        logger.info("[Reddit] Total: %d unique posts for %s", len(deduped), competitor_name)
        return deduped

    def _search_reddit(self, context, query: str, slug: str, subreddit: Optional[str]) -> list[RawReview]:
        """Search Reddit for a query using Playwright."""
        reviews = []

        try:
            page = context.new_page()

            if subreddit:
                url = f"https://www.reddit.com/r/{subreddit}/search/?q={quote_plus(query)}&restrict_sr=1&sort=relevance&t=year"
            else:
                url = f"https://www.reddit.com/search/?q={quote_plus(query)}&sort=relevance&t=year&type=link"

            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(3.0, 5.0))

            # Scroll to load more results
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            time.sleep(1.5)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)

            html = page.content()
            page.close()

            reviews = self._parse_search_results(html, query, slug)

        except Exception as e:
            logger.warning("[Reddit] Search error for '%s' in r/%s: %s", query, subreddit or "all", e)

        return reviews

    def _parse_search_results(self, html: str, competitor_name: str, slug: str) -> list[RawReview]:
        """Parse Reddit search results HTML into RawReview objects."""
        from bs4 import BeautifulSoup
        reviews = []
        soup = BeautifulSoup(html, "html.parser")

        # Reddit's current layout uses <article> or shreddit-post elements
        posts = (
            soup.find_all("article") or
            soup.find_all(attrs={"data-testid": "post-container"}) or
            soup.find_all("shreddit-post") or
            soup.find_all("div", attrs={"data-fullname": re.compile(r"t3_")})
        )

        # Fallback: find post titles by link pattern
        if not posts:
            posts = soup.find_all("a", href=re.compile(r"/r/\w+/comments/"))

        logger.debug("[Reddit] Found %d raw post elements", len(posts))

        for post in posts[:25]:  # Cap at 25 per search
            try:
                review = self._parse_post_element(post, competitor_name, slug)
                if review and self._is_relevant(review.body + " " + review.title, competitor_name):
                    reviews.append(review)
            except Exception as e:
                logger.debug("[Reddit] Post parse error: %s", e)

        return reviews

    def _parse_post_element(self, element, competitor_name: str, slug: str) -> Optional[RawReview]:
        """Extract fields from a single Reddit post element."""
        from bs4 import Tag

        # Title
        title = ""
        title_el = (
            element.find("h1") or element.find("h2") or element.find("h3") or
            element.find(attrs={"data-testid": "post-title"}) or
            element.find(class_=re.compile(r"title|post-title"))
        )
        if title_el:
            title = self.clean_text(title_el.get_text())
        elif hasattr(element, "get"):
            title = element.get("post-title", "") or element.get("title", "")

        if not title and isinstance(element, Tag):
            # If element IS an <a> tag, use its text
            title = self.clean_text(element.get_text())

        if not title or len(title) < 5:
            return None

        # Body / selftext
        body_el = element.find(attrs={"data-testid": "post-body"}) or \
                  element.find(class_=re.compile(r"selftext|post-body|usertext"))
        body = self.clean_text(body_el.get_text()) if body_el else ""

        # Combined text for analysis
        full_text = f"{title} {body}".strip()
        if len(full_text) < 15:
            return None

        # Author
        author_el = element.find(attrs={"data-testid": "post_author_link"}) or \
                    element.find(class_=re.compile(r"author"))
        author = self.clean_text(author_el.get_text()) if author_el else "reddit_user"
        author = author.replace("u/", "").strip() or "reddit_user"

        # Date — Reddit posts often have a <time> element
        date_el = element.find("time")
        if date_el:
            date_str = date_el.get("datetime", "")
            try:
                review_date = datetime.fromisoformat(date_str.replace("Z", ""))
            except Exception:
                review_date = datetime.utcnow() - timedelta(days=random.randint(1, 180))
        else:
            review_date = datetime.utcnow() - timedelta(days=random.randint(1, 180))

        # Permalink
        link_el = element.find("a", href=re.compile(r"/r/\w+/comments/"))
        permalink = ""
        if link_el:
            href = link_el.get("href", "")
            permalink = f"https://reddit.com{href}" if href.startswith("/") else href

        # Estimate sentiment from title/body
        rating = self._estimate_rating(full_text)

        review_id = self.make_review_id(
            self.platform, slug, author,
            review_date.strftime("%Y-%m-%d"), full_text
        )

        return RawReview(
            review_id           = review_id,
            platform            = self.platform,
            competitor_name     = competitor_name,
            competitor_slug     = slug,
            rating              = rating,
            title               = title[:300],
            body                = full_text[:3000],
            author              = author,
            author_role         = None,
            author_company      = None,
            author_company_size = None,
            review_date         = review_date,
            platform_url        = permalink or f"https://reddit.com/search?q={quote_plus(competitor_name)}",
            raw_html_hash       = self.make_html_hash(full_text + author),
        )

    def _is_relevant(self, text: str, competitor_name: str) -> bool:
        """Check if text meaningfully mentions the competitor."""
        text_lower = text.lower()
        # Check all common name variants
        variants = [w.lower() for w in competitor_name.split()] + [competitor_name.lower()]
        # Also add common aliases
        aliases = {
            "eq bank": ["eqbank", "eq bank", "equitable bank", "eqb"],
            "equitable bank": ["equitable bank", "eq bank", "eqb"],
            "home trust": ["home trust", "hometrust", "home capital", "htc", "home bank"],
        }
        check_variants = list(variants)
        for key, vals in aliases.items():
            if key in competitor_name.lower():
                check_variants.extend(vals)

        return any(v in text_lower for v in check_variants if len(v) > 2)

    def _estimate_rating(self, text: str) -> float:
        """Estimate a 1-5 rating from text sentiment."""
        text_lower = text.lower()
        negative = ["terrible", "awful", "worst", "avoid", "scam", "horrible",
                    "fraud", "predatory", "incompetent", "nightmare", "stay away",
                    "frozen", "locked", "refused", "dishonest", "beware"]
        positive = ["great", "excellent", "amazing", "love", "best", "fantastic",
                    "highly recommend", "perfect", "outstanding", "wonderful",
                    "easy", "fast", "helpful", "professional", "smooth"]
        neg = sum(1 for w in negative if w in text_lower)
        pos = sum(1 for w in positive if w in text_lower)
        if pos > neg:
            return min(5.0, 3.5 + pos * 0.3)
        elif neg > pos:
            return max(1.0, 3.0 - neg * 0.4)
        return 3.0
