"""
ReviewLedger · Google Reviews Scraper

Scrapes Google Maps / Google Business reviews for any company.
Uses Playwright to render the full page including lazy-loaded reviews.

Strategy: Search Google Maps for the company name, find the business listing,
extract all reviews by scrolling the review panel.
"""

import re
import time
import random
import logging
from datetime import datetime
from urllib.parse import quote_plus
from typing import Optional

from scrapers.base_scraper import (
    ScrapedReview, make_review_id, make_hash,
    clean_text, parse_rating, parse_date, get_playwright_page
)

logger = logging.getLogger(__name__)


def scrape_google_reviews(
    context,
    company_name: str,
    competitor_slug: str,
    aliases: list = None,
    max_reviews: int = 100,
) -> list[ScrapedReview]:
    """Scrape Google Reviews for a company."""
    reviews = []

    # Step 1: Find the Google Maps listing
    maps_url = _find_maps_url(context, company_name, aliases or [])
    if not maps_url:
        logger.warning("[Google] No Maps listing found for '%s'", company_name)
        return []

    logger.info("[Google] Found listing: %s", maps_url[:80])

    # Step 2: Navigate to the reviews tab
    page = get_playwright_page(context, maps_url, wait=1.5, scroll=False)
    if not page:
        return []

    try:
        # Click "Reviews" tab if visible
        try:
            review_btn = page.locator("button[aria-label*='Review'], [data-tab-index='1'], button:has-text('Reviews')").first
            if review_btn:
                review_btn.click(timeout=3000)
                time.sleep(0.6)
        except Exception:
            pass

        # Sort by "Newest" for most recent reviews
        try:
            sort_btn = page.locator("button[aria-label*='Sort'], [data-value='Sort']").first
            if sort_btn:
                sort_btn.click(timeout=3000)
                time.sleep(0.6)
                newest = page.locator("li[role='menuitemradio']:has-text('Newest'), [data-index='1']").first
                if newest:
                    newest.click(timeout=3000)
                    time.sleep(1.0)
        except Exception:
            pass

        # Scroll to load reviews
        reviews_loaded = 0
        scroll_attempts = 0
        max_scrolls = max(10, max_reviews // 8)

        while reviews_loaded < max_reviews and scroll_attempts < max_scrolls:
            # Scroll the review panel (not the page)
            try:
                page.evaluate("""
                    const panels = document.querySelectorAll('[role="feed"], .section-listbox, div[jslog*="reviews"]');
                    if (panels.length > 0) {
                        panels[panels.length-1].scrollTop += 2000;
                    } else {
                        window.scrollTo(0, document.body.scrollHeight);
                    }
                """)
            except Exception:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

            time.sleep(random.uniform(0.8, 1.2))

            # Extract reviews from current state
            html = page.content()
            batch = _parse_google_reviews_html(html, company_name, competitor_slug)
            if len(batch) > reviews_loaded:
                reviews_loaded = len(batch)
                logger.debug("[Google] Loaded %d reviews so far", reviews_loaded)
            else:
                break  # No new reviews loaded
            scroll_attempts += 1

        # Final extraction
        html = page.content()
        reviews = _parse_google_reviews_html(html, company_name, competitor_slug)

    except Exception as e:
        logger.error("[Google] Scrape error: %s", e)
    finally:
        try:
            page.close()
        except Exception:
            pass

    logger.info("[Google] Extracted %d reviews for '%s'", len(reviews), company_name)
    return reviews[:max_reviews]


def _find_maps_url(context, company_name: str, aliases: list) -> Optional[str]:
    """
    Search Google for the company's review page.
    For large chains: searches Google for brand-level review content.
    For local businesses: finds the Google Maps listing directly.
    """
    # Clean name for searching — strip legal suffixes
    import re as _re
    clean_name = _re.sub(
        r'(restaurants?|of canada|limited|ltd|inc\.?|corporation|corp\.?|llc|lp)',
        '', company_name, flags=_re.I
    ).strip().strip(',').strip()

    search_terms = [clean_name] + [
        a for a in aliases[:3]
        if a.lower() != company_name.lower()
    ]

    for term in search_terms:
        # Try Google Maps direct search first
        query = f"{term} Canada"
        search_url = f"https://www.google.com/maps/search/{quote_plus(query)}"

        page = get_playwright_page(context, search_url, wait=1.5, scroll=False)
        if not page:
            continue

        try:
            time.sleep(1.0)
            current_url = page.url

            # Direct redirect to a place page
            if "place" in current_url or "@" in current_url:
                page.close()
                return current_url

            # Find listing links
            links = page.eval_on_selector_all(
                "a[href*='/maps/place/']",
                "els => els.map(e => e.href)"
            )
            place_links = [l for l in links if "/maps/place/" in l]
            if place_links:
                page.close()
                return place_links[0]

        except Exception as e:
            logger.debug("[Google] Maps search error: %s", e)
        finally:
            try:
                page.close()
            except Exception:
                pass

        time.sleep(random.uniform(0.6, 1.0))

        # Fallback: search Google web for "[brand] reviews site:google.com/maps"
        # This helps for large chains where Maps returns many locations
        try:
            web_query = f'"{term}" reviews canada site:google.com/maps'
            web_url = f"https://www.google.com/search?q={quote_plus(web_query)}"
            page2 = get_playwright_page(context, web_url, wait=1.5, scroll=False)
            if page2:
                html = page2.content()
                page2.close()
                maps_links = _re.findall(r'google\.com/maps/place/([^"&\s]+)', html)
                if maps_links:
                    return f"https://www.google.com/maps/place/{maps_links[0]}"
        except Exception:
            pass

        time.sleep(random.uniform(0.5, 0.8))

    return None


def _parse_google_reviews_html(html: str, company_name: str, slug: str) -> list[ScrapedReview]:
    """Parse Google Maps HTML to extract reviews."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    reviews = []
    seen = set()

    # Google reviews are in various container structures
    # Try multiple selectors
    review_containers = (
        soup.find_all("div", class_=re.compile(r"jftiEf|WMbnJf|MyEned")) or
        soup.find_all("div", attrs={"data-review-id": True}) or
        soup.find_all("div", class_=re.compile(r"review|Review")) or
        []
    )

    for container in review_containers:
        try:
            # Body text
            body_el = (
                container.find(class_=re.compile(r"wiI7pd|MyEned|review-full-text")) or
                container.find(attrs={"jslog": re.compile(r"review")}) or
                container.find("span", class_=re.compile(r"review"))
            )
            if not body_el:
                # Try any span with meaningful text
                spans = container.find_all("span")
                body_el = next((s for s in spans if len(clean_text(s.get_text())) > 30), None)

            body = clean_text(body_el.get_text()) if body_el else ""
            if not body or len(body) < 15:
                continue

            # Rating
            rating_el = container.find(attrs={"aria-label": re.compile(r"\d.*star", re.I)})
            if rating_el:
                rating_raw = rating_el.get("aria-label", "")
                m = re.search(r'(\d+\.?\d*)', rating_raw)
                rating = float(m.group(1)) if m else 3.0
            else:
                stars = container.find_all(class_=re.compile(r"star|Star|hCCjke|kvMYJc"))
                rating = float(len([s for s in stars if "active" in str(s.get("class", "")).lower()])) or 3.0

            # Author
            author_el = container.find(class_=re.compile(r"d4r55|reviewer|author|WNxzHc"))
            author = clean_text(author_el.get_text()) if author_el else "Google Reviewer"

            # Date
            date_el = container.find(class_=re.compile(r"rsqaWe|dehysf|date|time")) or \
                      container.find("span", string=re.compile(r"ago|month|year|week", re.I))
            date_str = clean_text(date_el.get_text()) if date_el else ""
            review_date = parse_date(date_str) if date_str else datetime.utcnow()

            # Dedup
            body_hash = make_hash(body + author)
            if body_hash in seen:
                continue
            seen.add(body_hash)

            review_id = make_review_id("google", slug, author, review_date.strftime("%Y-%m-%d"), body)

            reviews.append(ScrapedReview(
                review_id=review_id, platform="google",
                platform_url="https://maps.google.com",
                competitor_name=company_name, competitor_slug=slug,
                rating=min(5.0, max(1.0, rating)), title="",
                body=body[:3000], author=author,
                author_role=None, author_company=None,
                review_date=review_date,
                raw_hash=body_hash,
            ))

        except Exception as e:
            logger.debug("[Google] Review parse error: %s", e)

    return reviews
