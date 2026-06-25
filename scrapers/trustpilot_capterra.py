"""
ReviewLedger · Trustpilot Scraper
Scrapes reviews from Trustpilot business pages using Playwright
for full JS rendering, bypassing bot detection.
"""

import json
import logging
import re
import time
import random
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from models import RawReview, Platform
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class TrustpilotScraper(BaseScraper):

    platform = Platform.TRUSTPILOT
    BASE_URL = "https://www.trustpilot.com/review/{domain}"

    def _fetch_with_playwright(self, url: str) -> Optional[BeautifulSoup]:
        """Use Playwright headless browser to fetch JS-rendered pages."""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800},
                    locale="en-CA",
                )
                page = context.new_page()
                # Block images/fonts to speed up loading
                page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}", lambda r: r.abort())
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(2.0, 3.5))  # Let JS render
                html = page.content()
                browser.close()
                return BeautifulSoup(html, "html.parser")
        except Exception as e:
            logger.warning("[Trustpilot] Playwright failed: %s — falling back to requests", e)
            return self.fetch(url)

    def scrape_competitor(
        self,
        competitor_slug: str,
        competitor_name: str,
        start_url: str,
        max_pages: int = 10,
    ) -> list[RawReview]:

        all_reviews: list[RawReview] = []
        job = self.start_job(competitor_slug, start_url)

        logger.info("[Trustpilot] Starting: %s", competitor_name)

        for page_num in range(1, max_pages + 1):
            url = start_url if page_num == 1 else f"{start_url}?page={page_num}"
            logger.info("[Trustpilot] Fetching page %d", page_num)

            # Try Playwright first, fall back to requests
            soup = self._fetch_with_playwright(url)
            if not soup:
                break

            # PRIMARY: extract from JSON-LD structured data
            reviews = self._parse_jsonld(soup, competitor_slug, competitor_name, url)

            # FALLBACK: parse HTML directly
            if not reviews:
                reviews = self._parse_html(soup, competitor_slug, competitor_name, url)

            if not reviews:
                logger.info("[Trustpilot] Empty page %d — done", page_num)
                break

            all_reviews.extend(reviews)
            logger.info("[Trustpilot] Page %d: %d reviews (total: %d)",
                        page_num, len(reviews), len(all_reviews))

            if not self._has_next_page(soup):
                break

        self.finish_job(found=len(all_reviews), new=len(all_reviews))
        return all_reviews

    def _parse_jsonld(
        self, soup: BeautifulSoup, slug: str, name: str, url: str
    ) -> list[RawReview]:
        """Extract reviews from JSON-LD structured data blocks."""
        reviews = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except json.JSONDecodeError:
                continue

            # Handle both single object and array
            items = data if isinstance(data, list) else [data]

            for item in items:
                # Look for Product or LocalBusiness with reviews
                if item.get("@type") in ("Product", "LocalBusiness", "Organization"):
                    for review_data in item.get("review", []) + item.get("reviews", []):
                        r = self._jsonld_to_review(review_data, slug, name, url)
                        if r:
                            reviews.append(r)

                # Direct Review type
                elif item.get("@type") == "Review":
                    r = self._jsonld_to_review(item, slug, name, url)
                    if r:
                        reviews.append(r)

        return reviews

    def _jsonld_to_review(self, data: dict, slug: str, name: str, url: str) -> Optional[RawReview]:
        """Convert a JSON-LD review object to RawReview."""
        body = data.get("reviewBody", "") or data.get("description", "")
        if not body or len(body) < 20:
            return None

        rating_data = data.get("reviewRating", {})
        rating = float(rating_data.get("ratingValue", 3.0))

        author_data = data.get("author", {})
        author = author_data.get("name", "Anonymous") if isinstance(author_data, dict) else str(author_data)

        date_str = data.get("datePublished", "") or data.get("dateCreated", "")
        review_date = self._parse_date(date_str)

        review_id = self.make_review_id(self.platform, slug, author, review_date.strftime("%Y-%m-%d"), body)

        return RawReview(
            review_id           = review_id,
            platform            = self.platform,
            competitor_name     = name,
            competitor_slug     = slug,
            rating              = min(5.0, rating),
            title               = data.get("name", "")[:200],
            body                = self.clean_text(body),
            author              = author,
            author_role         = None,
            author_company      = None,
            author_company_size = None,
            review_date         = review_date,
            platform_url        = url,
            raw_html_hash       = self.make_html_hash(body + author),
        )

    def _parse_html(self, soup: BeautifulSoup, slug: str, name: str, url: str) -> list[RawReview]:
        """Fallback HTML parser for Trustpilot."""
        reviews = []

        cards = (
            soup.find_all("div", attrs={"data-service-review-id": True}) or
            soup.find_all("article", class_=re.compile(r"review")) or
            soup.find_all("div", class_=re.compile(r"reviewCard|review-card"))
        )

        for card in cards:
            try:
                body_el = card.find(attrs={"data-service-review-text-typography": True}) or \
                          card.find(class_=re.compile(r"reviewContent|review-content"))
                body = self.clean_text(body_el.get_text()) if body_el else ""
                if not body or len(body) < 20:
                    continue

                rating_el = card.find(class_=re.compile(r"starRating|star-rating"))
                rating = 3.0
                if rating_el:
                    img = rating_el.find("img")
                    if img and img.get("alt"):
                        rating = self.parse_rating(img["alt"])

                author_el = card.find(attrs={"data-consumer-name-typography": True}) or \
                            card.find(class_=re.compile(r"consumerName|reviewer-name"))
                author = self.clean_text(author_el.get_text()) if author_el else "Anonymous"

                date_el = card.find("time")
                date_str = date_el.get("datetime", "") if date_el else ""
                review_date = self._parse_date(date_str)

                review_id = self.make_review_id(self.platform, slug, author, review_date.strftime("%Y-%m-%d"), body)

                reviews.append(RawReview(
                    review_id=review_id, platform=self.platform,
                    competitor_name=name, competitor_slug=slug,
                    rating=rating, title="", body=body, author=author,
                    author_role=None, author_company=None, author_company_size=None,
                    review_date=review_date, platform_url=url,
                    raw_html_hash=self.make_html_hash(body + author),
                ))
            except Exception as e:
                logger.debug("[Trustpilot] Card parse error: %s", e)

        return reviews

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        return bool(
            soup.find("a", attrs={"name": "pagination-button-next"}) or
            soup.find("a", class_=re.compile(r"pagination.*next|next.*page"))
        )

    def _parse_date(self, raw: str) -> datetime:
        for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"]:
            try:
                return datetime.strptime(raw[:26], fmt)
            except ValueError:
                continue
        return datetime.utcnow()

    @staticmethod
    def build_url(domain: str) -> str:
        return TrustpilotScraper.BASE_URL.format(domain=domain)


# ─────────────────────────────────────────────────────────────────────────────


class CapterraScraper(BaseScraper):
    """
    ReviewLedger · Capterra Scraper

    URL pattern:
      https://www.capterra.com/p/{id}/{slug}/#reviews

    Capterra embeds review data in both JSON-LD and
    structured HTML. We try JSON-LD first.
    """

    platform = Platform.CAPTERRA
    BASE_URL = "https://www.capterra.com/p/{product_id}/{slug}/"

    def scrape_competitor(
        self,
        competitor_slug: str,
        competitor_name: str,
        start_url: str,
        max_pages: int = 10,
    ) -> list[RawReview]:

        all_reviews: list[RawReview] = []
        self.start_job(competitor_slug, start_url)
        logger.info("[Capterra] Starting: %s", competitor_name)

        for page_num in range(1, max_pages + 1):
            # Capterra uses ?page= for pagination
            url = start_url if page_num == 1 else f"{start_url}?page={page_num}"
            soup = self.fetch(url)
            if not soup:
                break

            reviews = self._parse_page(soup, competitor_slug, competitor_name, url)
            if not reviews:
                break

            all_reviews.extend(reviews)
            logger.info("[Capterra] Page %d: %d reviews", page_num, len(reviews))

            if not self._has_next_page(soup):
                break

        self.finish_job(found=len(all_reviews), new=len(all_reviews))
        return all_reviews

    def _parse_page(self, soup: BeautifulSoup, slug: str, name: str, url: str) -> list[RawReview]:
        reviews = []

        # Try JSON-LD first
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                for review_item in data.get("review", []) if isinstance(data, dict) else []:
                    body = review_item.get("reviewBody", "")
                    if not body or len(body) < 20:
                        continue
                    rating_data = review_item.get("reviewRating", {})
                    rating = float(rating_data.get("ratingValue", 3.0))
                    author_data = review_item.get("author", {})
                    author = author_data.get("name", "Anonymous") if isinstance(author_data, dict) else "Anonymous"
                    date_str = review_item.get("datePublished", "")
                    try:
                        review_date = datetime.fromisoformat(date_str.replace("Z",""))
                    except Exception:
                        review_date = datetime.utcnow()

                    review_id = self.make_review_id(self.platform, slug, author, review_date.strftime("%Y-%m-%d"), body)
                    reviews.append(RawReview(
                        review_id=review_id, platform=self.platform,
                        competitor_name=name, competitor_slug=slug,
                        rating=min(5.0, rating), title=review_item.get("name",""),
                        body=self.clean_text(body), author=author,
                        author_role=None, author_company=None, author_company_size=None,
                        review_date=review_date, platform_url=url,
                        raw_html_hash=self.make_html_hash(body+author),
                    ))
            except Exception:
                pass

        if reviews:
            return reviews

        # HTML fallback
        cards = (
            soup.find_all("div", class_=re.compile(r"review-card|reviewCard|review_card")) or
            soup.find_all("article", class_=re.compile(r"review")) or
            soup.find_all("li", class_=re.compile(r"review"))
        )

        for card in cards:
            try:
                body_el = (
                    card.find(class_=re.compile(r"review.*content|review.*body|review.*text")) or
                    card.find("div", class_=re.compile(r"body|content|text"))
                )
                body = self.clean_text(body_el.get_text()) if body_el else ""
                if not body or len(body) < 20:
                    continue

                stars = card.find_all(class_=re.compile(r"star.*full|full.*star|star-filled"))
                rating = float(len(stars)) if stars else 3.0

                author_el = card.find(class_=re.compile(r"reviewer|author|user.*name"))
                author = self.clean_text(author_el.get_text()) if author_el else "Anonymous"

                role_el = card.find(class_=re.compile(r"reviewer.*role|job.*title|user.*role"))
                author_role = self.clean_text(role_el.get_text()) if role_el else None

                company_el = card.find(class_=re.compile(r"company.*size|industry|employee"))
                author_company_size = self.clean_text(company_el.get_text()) if company_el else None

                date_el = card.find("time") or card.find(class_=re.compile(r"date|posted"))
                date_str = (date_el.get("datetime") or date_el.get_text()) if date_el else ""
                try:
                    review_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
                except Exception:
                    review_date = datetime.utcnow()

                review_id = self.make_review_id(self.platform, slug, author, review_date.strftime("%Y-%m-%d"), body)
                reviews.append(RawReview(
                    review_id=review_id, platform=self.platform,
                    competitor_name=name, competitor_slug=slug,
                    rating=rating, title="", body=body, author=author,
                    author_role=author_role, author_company=None,
                    author_company_size=author_company_size,
                    review_date=review_date, platform_url=url,
                    raw_html_hash=self.make_html_hash(body+author),
                ))
            except Exception as e:
                logger.debug("[Capterra] Card error: %s", e)

        return reviews

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        return bool(soup.find("a", class_=re.compile(r"next|pagination.*next")))
