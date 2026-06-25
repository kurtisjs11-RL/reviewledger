"""
ReviewLedger · BBB (Better Business Bureau) Scraper

Scrapes customer reviews and complaints from BBB.org.
BBB is particularly valuable for:
- Financial services, home services, contractors
- Service businesses with formal complaint patterns
- Canadian businesses (bbb.org covers Canada too)
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

BBB_SEARCH = "https://www.bbb.org/search?find_text={query}&find_loc=Canada"
BBB_CA_SEARCH = "https://www.bbb.org/search?find_text={query}&find_loc=Canada&find_country=CAN"


def scrape_bbb(
    context,
    company_name: str,
    competitor_slug: str,
    aliases: list = None,
    max_reviews: int = 50,
) -> list[ScrapedReview]:
    """Scrape BBB reviews and complaints for a company."""
    reviews = []

    # Find BBB profile URL
    profile_url = _find_bbb_profile(context, company_name, aliases or [])
    if not profile_url:
        logger.info("[BBB] No profile found for '%s'", company_name)
        return []

    logger.info("[BBB] Found profile: %s", profile_url)

    # Scrape reviews tab
    reviews_url = profile_url.rstrip("/") + "/reviews-and-complaints"
    page = get_playwright_page(context, reviews_url, wait=1.5)
    if not page:
        # Try the profile page directly
        page = get_playwright_page(context, profile_url, wait=1.5)
    if not page:
        return []

    try:
        html = page.content()
        reviews = _parse_bbb_html(html, company_name, competitor_slug, reviews_url)

        # Try to get more pages
        page_num = 2
        while len(reviews) < max_reviews and page_num <= 5:
            next_url = f"{reviews_url}#page={page_num}"
            try:
                page.goto(next_url, wait_until="domcontentloaded", timeout=15000)
                time.sleep(random.uniform(0.8, 1.2))
                more = _parse_bbb_html(page.content(), company_name, competitor_slug, next_url)
                if not more:
                    break
                reviews.extend(more)
                page_num += 1
            except Exception:
                break

    except Exception as e:
        logger.error("[BBB] Scrape error: %s", e)
    finally:
        try:
            page.close()
        except Exception:
            pass

    # Dedup by review_id
    seen = set()
    unique = []
    for r in reviews:
        if r.review_id not in seen:
            seen.add(r.review_id)
            unique.append(r)

    logger.info("[BBB] Extracted %d reviews for '%s'", len(unique), company_name)
    return unique[:max_reviews]


def _find_bbb_profile(context, company_name: str, aliases: list) -> Optional[str]:
    """Search BBB for a company and return the profile URL."""
    search_terms = [company_name] + aliases[:2]

    for term in search_terms:
        url = BBB_CA_SEARCH.format(query=quote_plus(term))
        page = get_playwright_page(context, url, wait=1.5, scroll=False)
        if not page:
            continue

        try:
            # Find business profile links
            links = page.eval_on_selector_all(
                "a[href*='/profile/'], a[href*='/us/'], a[href*='/ca/']",
                "els => els.map(e => e.href)"
            )

            bbb_links = [
                l for l in links
                if "bbb.org" in l and ("/profile/" in l or "/us/" in l)
                and "search" not in l
            ]

            if bbb_links:
                # Score by name match
                best = _best_bbb_match(bbb_links, term)
                if best:
                    page.close()
                    return best

            # Also try raw HTML scan
            html = page.content()
            matches = re.findall(r'href="(https://www\.bbb\.org/(?:us|ca|canada)/[^"]+/[^"]+)"', html)
            if matches:
                page.close()
                return matches[0]

        except Exception as e:
            logger.debug("[BBB] Search error: %s", e)
        finally:
            try:
                page.close()
            except Exception:
                pass

        time.sleep(random.uniform(0.7, 1.0))

    return None


def _best_bbb_match(links: list, query: str) -> Optional[str]:
    query_words = set(re.sub(r'[^a-z0-9]', ' ', query.lower()).split())
    best_score, best_url = 0, None
    for link in links:
        slug_part = link.split("/")[-1] if "/" in link else link
        slug_clean = re.sub(r'[^a-z0-9]', ' ', slug_part.lower())
        score = sum(1 for w in query_words if w in slug_clean and len(w) > 2)
        if score > best_score:
            best_score, best_url = score, link
    return best_url or (links[0] if links else None)


def _parse_bbb_html(html: str, company_name: str, slug: str, url: str) -> list[ScrapedReview]:
    """Parse BBB HTML to extract reviews and complaints."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    reviews = []
    seen = set()

    # BBB review containers
    containers = (
        soup.find_all("div", class_=re.compile(r"review|complaint|customer-review")) or
        soup.find_all("article", class_=re.compile(r"review|complaint")) or
        soup.find_all("div", attrs={"data-review": True}) or
        []
    )

    # Also look for complaint sections
    complaint_sections = soup.find_all(
        string=re.compile(r"complaint|Complaint", re.I)
    )

    for container in containers:
        try:
            # Body
            body_el = (
                container.find(class_=re.compile(r"content|body|text|description")) or
                container.find("p")
            )
            body = clean_text(body_el.get_text()) if body_el else ""
            if not body or len(body) < 20:
                # Try getting all text in container
                body = clean_text(container.get_text())
            if not body or len(body) < 20:
                continue

            # Rating
            rating_el = container.find(class_=re.compile(r"star|rating|score"))
            if rating_el:
                rating = parse_rating(rating_el.get("aria-label", "") or rating_el.get_text())
            else:
                # BBB uses 1-5 star ratings
                stars = container.find_all(class_=re.compile(r"star-active|star-filled|filled"))
                rating = float(len(stars)) if stars else 3.0

            # Author
            author_el = container.find(class_=re.compile(r"author|reviewer|customer|name"))
            author = clean_text(author_el.get_text()) if author_el else "BBB Reviewer"

            # Date
            date_el = (
                container.find("time") or
                container.find(class_=re.compile(r"date|time|posted"))
            )
            date_str = (
                date_el.get("datetime", "") or clean_text(date_el.get_text())
            ) if date_el else ""
            review_date = parse_date(date_str) if date_str else datetime.utcnow()

            body_hash = make_hash(body[:100] + author)
            if body_hash in seen:
                continue
            seen.add(body_hash)

            # Classify complaint vs review — complaints are pain by default
            is_complaint = bool(container.find(string=re.compile(r"complaint", re.I)))
            if is_complaint and rating == 3.0:
                rating = 1.5  # Complaints skew negative

            reviews.append(ScrapedReview(
                review_id=make_review_id("bbb", slug, author, review_date.strftime("%Y-%m-%d"), body),
                platform="bbb", platform_url=url,
                competitor_name=company_name, competitor_slug=slug,
                rating=min(5.0, max(1.0, rating)), title="",
                body=body[:3000], author=author,
                author_role=None, author_company=None,
                review_date=review_date, raw_hash=body_hash,
            ))

        except Exception as e:
            logger.debug("[BBB] Container parse error: %s", e)

    return reviews
