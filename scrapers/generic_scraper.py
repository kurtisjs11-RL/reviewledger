"""
ReviewLedger · Generic Playwright Scraper

For review platforms that don't have a dedicated scraper.
Uses heuristics to find and extract review content from any page.
Works for: Yelp, Glassdoor, SiteJabber, Houzz, DealerRater, etc.
"""

import re
import json
import time
import random
import logging
from datetime import datetime
from typing import Optional

from scrapers.base_scraper import (
    ScrapedReview, make_review_id, make_hash,
    clean_text, parse_rating, parse_date, get_playwright_page
)

logger = logging.getLogger(__name__)

# Minimum body length to count as a real review
MIN_BODY_LEN = 25

# CSS patterns that commonly wrap reviews across platforms
REVIEW_CONTAINER_PATTERNS = [
    "[data-testid*='review']",
    "[class*='review-container']",
    "[class*='ReviewContainer']",
    "[class*='reviewCard']",
    "[class*='review-card']",
    "article[class*='review']",
    "li[class*='review']",
    "div[class*='reviewItem']",
    "div[itemprop='review']",
    "[class*='rating-item']",
    "[class*='user-review']",
]

BODY_PATTERNS = [
    "[class*='review-text']",
    "[class*='reviewText']",
    "[class*='review-content']",
    "[class*='reviewContent']",
    "[class*='review-body']",
    "[class*='comment-text']",
    "[itemprop='reviewBody']",
    "p[class*='review']",
]

RATING_PATTERNS = [
    "[class*='star-rating']",
    "[class*='starRating']",
    "[aria-label*='star']",
    "[class*='rating-score']",
    "[itemprop='ratingValue']",
]

AUTHOR_PATTERNS = [
    "[class*='reviewer-name']",
    "[class*='authorName']",
    "[class*='author-name']",
    "[itemprop='author']",
    "[class*='user-name']",
    "[class*='reviewer']",
]

DATE_PATTERNS = [
    "time",
    "[class*='review-date']",
    "[class*='date-posted']",
    "[class*='posted-date']",
    "[datetime]",
]


def scrape_generic(
    context,
    url: str,
    platform: str,
    company_name: str,
    competitor_slug: str,
    max_reviews: int = 50,
    max_pages: int = 5,
) -> list[ScrapedReview]:
    """
    Scrape reviews from any review platform using heuristic extraction.
    """
    all_reviews = []
    seen_hashes = set()

    for page_num in range(1, max_pages + 1):
        # Build paginated URL
        page_url = _paginate_url(url, page_num)
        if page_num > 1 and page_url == url:
            break  # Can't paginate this URL

        page = get_playwright_page(context, page_url, wait=1.5)
        if not page:
            break

        try:
            html = page.content()

            # Try JSON-LD first (most reliable)
            batch = _extract_jsonld_reviews(html, platform, company_name, competitor_slug)

            # Fall back to HTML heuristics
            if not batch:
                batch = _extract_html_reviews(html, page, platform, company_name, competitor_slug)

            # Dedup
            new_reviews = []
            for r in batch:
                if r.raw_hash not in seen_hashes:
                    seen_hashes.add(r.raw_hash)
                    new_reviews.append(r)

            if not new_reviews and page_num > 1:
                break  # Pagination exhausted

            all_reviews.extend(new_reviews)
            logger.info("[Generic:%s] Page %d: %d reviews", platform, page_num, len(new_reviews))

            if len(all_reviews) >= max_reviews:
                break

        except Exception as e:
            logger.warning("[Generic:%s] Page %d error: %s", platform, page_num, e)
            break
        finally:
            try:
                page.close()
            except Exception:
                pass

        time.sleep(random.uniform(0.7, 1.0))

    logger.info("[Generic:%s] Total: %d reviews for '%s'", platform, len(all_reviews), company_name)
    return all_reviews[:max_reviews]


def _extract_jsonld_reviews(html: str, platform: str, company_name: str, slug: str) -> list[ScrapedReview]:
    """Extract reviews from JSON-LD structured data."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    reviews = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            items = data if isinstance(data, list) else [data]
            for item in items:
                review_list = (
                    item.get("review", []) +
                    item.get("reviews", []) +
                    ([] if item.get("@type") != "Review" else [item])
                )
                for rev in review_list:
                    r = _jsonld_to_review(rev, platform, company_name, slug)
                    if r:
                        reviews.append(r)
        except Exception:
            pass

    return reviews


def _jsonld_to_review(data: dict, platform: str, company_name: str, slug: str) -> Optional[ScrapedReview]:
    body = data.get("reviewBody", "") or data.get("description", "")
    if not body or len(body) < MIN_BODY_LEN:
        return None

    rating_data = data.get("reviewRating", {})
    rating = float(rating_data.get("ratingValue", 3.0)) if rating_data else 3.0

    author_data = data.get("author", {})
    author = (
        author_data.get("name", "Reviewer")
        if isinstance(author_data, dict)
        else str(author_data)
    )

    date_str = data.get("datePublished", "") or data.get("dateCreated", "")
    review_date = parse_date(date_str) if date_str else datetime.utcnow()
    body = clean_text(body)
    body_hash = make_hash(body[:100] + author)

    return ScrapedReview(
        review_id=make_review_id(platform, slug, author, review_date.strftime("%Y-%m-%d"), body),
        platform=platform, platform_url="",
        competitor_name=company_name, competitor_slug=slug,
        rating=min(5.0, max(1.0, rating)),
        title=data.get("name", "")[:200],
        body=body[:3000], author=author,
        author_role=None, author_company=None,
        review_date=review_date, raw_hash=body_hash,
    )


def _extract_html_reviews(
    html: str, page, platform: str, company_name: str, slug: str
) -> list[ScrapedReview]:
    """Extract reviews using HTML heuristics."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    reviews = []
    seen = set()

    # Find review containers using multiple patterns
    containers = []
    for pattern in REVIEW_CONTAINER_PATTERNS:
        try:
            found = page.eval_on_selector_all(
                pattern,
                "els => els.map(e => e.outerHTML)"
            )
            if found:
                sub_soup = BeautifulSoup("<div>" + "".join(found) + "</div>", "html.parser")
                containers = sub_soup.find_all("div", recursive=False)
                if len(containers) >= 2:
                    break
        except Exception:
            pass

    # Fall back to BeautifulSoup patterns
    if not containers:
        for pattern_class in ["review", "Review", "testimonial", "comment", "rating"]:
            found = soup.find_all(attrs={"class": re.compile(pattern_class)})
            if len(found) >= 2:
                containers = found
                break

    for container in containers[:100]:
        try:
            # Body
            body_el = None
            for bp in BODY_PATTERNS:
                body_el = container.select_one(bp)
                if body_el:
                    break
            if not body_el:
                # Try all paragraphs
                paras = container.find_all("p")
                body_el = max(paras, key=lambda p: len(p.get_text()), default=None)
            if not body_el:
                body_el = container  # Use entire container text as fallback

            body = clean_text(body_el.get_text())
            if not body or len(body) < MIN_BODY_LEN:
                continue
            # Skip navigation text
            if any(skip in body.lower() for skip in ["cookie", "privacy policy", "sign in", "log in"]):
                continue

            # Rating
            rating = 3.0
            for rp in RATING_PATTERNS:
                rel = container.select_one(rp)
                if rel:
                    aria = rel.get("aria-label", "")
                    content = rel.get("content", "")
                    text = aria or content or rel.get_text()
                    if text:
                        rating = parse_rating(text)
                        break
            if rating == 3.0:
                # Count filled stars
                filled = container.find_all(class_=re.compile(r"fill|active|selected|check"))
                if filled:
                    rating = min(5.0, float(len(filled)))

            # Author
            author = "Reviewer"
            for ap in AUTHOR_PATTERNS:
                ael = container.select_one(ap)
                if ael:
                    author = clean_text(ael.get_text())
                    break

            # Date
            review_date = datetime.utcnow()
            for dp in DATE_PATTERNS:
                del_ = container.select_one(dp)
                if del_:
                    date_str = del_.get("datetime", "") or clean_text(del_.get_text())
                    if date_str:
                        review_date = parse_date(date_str)
                        break

            body_hash = make_hash(body[:100] + author)
            if body_hash in seen:
                continue
            seen.add(body_hash)

            reviews.append(ScrapedReview(
                review_id=make_review_id(platform, slug, author, review_date.strftime("%Y-%m-%d"), body),
                platform=platform, platform_url="",
                competitor_name=company_name, competitor_slug=slug,
                rating=min(5.0, max(1.0, rating)), title="",
                body=body[:3000], author=author,
                author_role=None, author_company=None,
                review_date=review_date, raw_hash=body_hash,
            ))

        except Exception as e:
            logger.debug("[Generic] Container parse error: %s", e)

    return reviews


def _paginate_url(url: str, page_num: int) -> str:
    """Try to build a paginated URL for common pagination patterns."""
    if page_num == 1:
        return url
    if "?" in url:
        return f"{url}&page={page_num}"
    return f"{url}?page={page_num}"
