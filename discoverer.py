"""
ReviewLedger · Trustpilot URL Discoverer

Uses Playwright (headless browser) to search Trustpilot for any company
in any industry and return the correct review page URL.

Three strategies in order:
1. Playwright-rendered Trustpilot search (most reliable)
2. Playwright-rendered direct URL validation (brute-force guesses)
3. Playwright-rendered Google search as last resort
"""

import re
import time
import random
import logging
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


def find_trustpilot_url(company_name: str, aliases: list = None) -> Optional[str]:
    """
    Find Trustpilot review URL for any company using Playwright.
    Tries company name and all aliases across multiple strategies.
    """
    aliases = aliases or []
    all_names = _dedupe([company_name] + aliases)

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
                viewport={"width": 1280, "height": 800},
                locale="en-CA",
            )
            # Block images/fonts to speed up page loads
            context.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot}",
                lambda r: r.abort()
            )

            # ── STRATEGY 1: Trustpilot search for each name variant ──
            for name in all_names:
                logger.info("[Discoverer] Trustpilot search: '%s'", name)
                url = _search_trustpilot_playwright(context, name, company_name)
                if url:
                    logger.info("[Discoverer] ✓ Found via search: %s", url)
                    browser.close()
                    return url
                time.sleep(random.uniform(0.5, 0.9))

            # ── STRATEGY 2: Direct URL guesses validated live ──
            candidates = _build_url_candidates(company_name, aliases)
            logger.info("[Discoverer] Trying %d direct URL candidates", len(candidates))
            page = context.new_page()
            for domain in candidates[:30]:  # Cap at 30 attempts
                full_url = f"https://www.trustpilot.com/review/{domain}"
                try:
                    resp = page.goto(full_url, wait_until="domcontentloaded", timeout=8000)
                    if resp and resp.status == 200:
                        final_url = page.url
                        # Confirm it landed on a real review page with reviews
                        content = page.content()
                        if (
                            "/review/" in final_url
                            and any(kw in content for kw in [
                                "TrustScore", "reviewCount", "ratingValue",
                                "stars", "out of 5", "reviews"
                            ])
                            and "search" not in final_url
                        ):
                            logger.info("[Discoverer] ✓ Found via direct URL: %s", final_url)
                            page.close()
                            browser.close()
                            return final_url.split("?")[0].rstrip("/")
                except Exception:
                    pass
                time.sleep(0.15)
            page.close()

            # ── STRATEGY 3: Google search ──
            for name in all_names[:2]:
                logger.info("[Discoverer] Google search for: '%s'", name)
                url = _google_search_playwright(context, name, company_name)
                if url:
                    logger.info("[Discoverer] ✓ Found via Google: %s", url)
                    browser.close()
                    return url
                time.sleep(random.uniform(0.8, 1.2))

            browser.close()

    except ImportError:
        logger.error("[Discoverer] Playwright not installed")
    except Exception as e:
        logger.error("[Discoverer] Error: %s", e)

    logger.warning("[Discoverer] ✗ No Trustpilot page found for '%s'", company_name)
    return None


def _search_trustpilot_playwright(context, query: str, company_name: str) -> Optional[str]:
    """Search Trustpilot using a real browser and extract the best result."""
    page = context.new_page()
    try:
        search_url = f"https://www.trustpilot.com/search?query={quote_plus(query)}"
        page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(random.uniform(0.8, 1.2))

        # Extract all /review/ links from the rendered page
        links = page.eval_on_selector_all(
            "a[href*='/review/']",
            "els => els.map(e => e.getAttribute('href'))"
        )

        # Also scan raw HTML for review links
        html = page.content()
        html_matches = re.findall(r'href=["\'](/review/[a-z0-9\-\.]+)["\']', html)
        links += html_matches

        # Extract domain portion
        domains = []
        for link in links:
            m = re.match(r'/?review/([a-z0-9][a-z0-9\-\.]+\.[a-z]{2,})', link)
            if m:
                domains.append(m.group(1))

        domains = _dedupe(domains)

        if domains:
            best = _score_and_pick(domains, company_name, query)
            if best:
                page.close()
                return f"https://www.trustpilot.com/review/{best}"

    except Exception as e:
        logger.debug("[Discoverer] Trustpilot search error: %s", e)
    finally:
        try:
            page.close()
        except Exception:
            pass

    return None


def _google_search_playwright(context, query: str, company_name: str) -> Optional[str]:
    """Search Google for the Trustpilot page of a company."""
    page = context.new_page()
    try:
        search_q = f'trustpilot.com/review "{query}"'
        url = f"https://www.google.com/search?q={quote_plus(search_q)}&num=10"
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(random.uniform(0.8, 1.2))

        html = page.content()
        matches = re.findall(r'trustpilot\.com/review/([a-z0-9\-\.]+)', html)
        matches = _dedupe(matches)

        if matches:
            # Filter out pagination (e.g. domain.com?page=2)
            clean = [m.split("?")[0].split("#")[0] for m in matches]
            clean = [m for m in clean if "." in m and len(m) > 4]
            clean = _dedupe(clean)
            best = _score_and_pick(clean, company_name, query)
            if best:
                page.close()
                return f"https://www.trustpilot.com/review/{best}"

    except Exception as e:
        logger.debug("[Discoverer] Google search error: %s", e)
    finally:
        try:
            page.close()
        except Exception:
            pass

    return None


def _build_url_candidates(company_name: str, aliases: list) -> list:
    """
    Generate a comprehensive list of plausible Trustpilot domain slugs
    for any company in any industry.
    """
    all_names = _dedupe([company_name] + aliases)
    candidates = []

    # Common suffixes to strip for cleaner slug generation
    strip_words = (
        r'\b(company|corporation|corp|incorporated|inc|limited|ltd|'
        r'group|holdings|holding|financial|finance|bank|banking|trust|'
        r'mortgage|mortgages|capital|services|service|solutions|solution|'
        r'technologies|technology|tech|digital|online|global|international|'
        r'national|canada|canadian|lp|llp|llc|co\.?|and)\b'
    )

    slugs = set()
    for name in all_names:
        variants = [name]

        # Also add version with common words stripped
        stripped = re.sub(strip_words, ' ', name.lower(), flags=re.I)
        stripped = re.sub(r'\s+', ' ', stripped).strip()
        if stripped and stripped != name.lower():
            variants.append(stripped)

        for variant in variants:
            v = variant.lower().strip()
            if not v:
                continue

            # Generate slug styles
            hyphen = re.sub(r'[^a-z0-9]+', '-', v).strip('-')
            nospace = re.sub(r'[^a-z0-9]+', '', v)
            dotted  = re.sub(r'[^a-z0-9]+', '.', v).strip('.')

            for slug in [hyphen, nospace, dotted]:
                if not slug or len(slug) < 2:
                    continue
                # Try .ca first (prioritise Canadian), then .com, then no TLD
                for tld in ['.ca', '.com', '.ca']:
                    full = slug + tld
                    if len(full) > 4:
                        slugs.add(full)
                # Also try without TLD (some companies use subdomains)
                slugs.add(slug)

    # Sort: .ca first, then .com, then others, shorter slugs first
    def sort_key(s):
        tld_score = 0 if s.endswith('.ca') else (1 if s.endswith('.com') else 2)
        return (tld_score, len(s), s)

    return sorted(slugs, key=sort_key)


def _score_and_pick(domains: list, company_name: str, query: str) -> Optional[str]:
    """
    Score domain candidates and pick the best match for the company name.
    Uses brand-word separation to avoid matching generic industry terms.
    """
    import re

    if not domains:
        return None

    GENERIC_INDUSTRY = {
        'plumbing','rooter','plumber','electric','electrical','hvac','heating','cooling',
        'roofing','roofer','cleaning','cleaner','repair','service','services','solutions',
        'restaurant','restaurants','food','burger','kitchen','grill','pizza','cafe',
        'hotel','resort','lodge','inn','motel','travel','tours','tourism',
        'dental','medical','clinic','health','care','pharmacy','doctor',
        'law','legal','lawyer','attorney','accounting','consulting',
        'tech','technology','software','digital','online','web','app',
        'bank','banking','financial','finance','capital','investment','insurance',
        'realty','real','estate','homes','property','housing',
    }

    NOISE = {
        'the','a','an','of','and','or','for','in','at','by','to','company',
        'corp','inc','ltd','group','lp','llc','co','national','canada',
        'canadian','limited','corporation',
    }

    WRONG_TLDS = {
        '.dk','.de','.fr','.nl','.se','.no','.fi','.pl','.it','.es',
        '.pt','.ru','.cn','.jp','.kr','.au','.nz','.in','.br','.mx',
    }

    def _valid(domain, score):
        # Reject wrong-country TLDs
        if any(domain.lower().endswith(t) for t in WRONG_TLDS):
            return False

        # All meaningful words — include names like "national", "first", "home"
        NOISE_HARD = {'the','a','an','of','and','or','for','in','at','by','to',
                      'corp','inc','ltd','group','lp','llc','co',
                      'canada','canadian','limited','corporation','company'}
        all_words   = [w for w in re.split(r'[^a-z0-9]', company_name.lower())
                       if w and w not in NOISE_HARD and len(w) >= 2]
        brand_words = [w for w in all_words if w not in GENERIC_INDUSTRY]
        domain_slug = re.sub(r'[^a-z0-9]', '', domain.lower().split('.')[0])

        if not brand_words:
            return domain_slug.startswith(all_words[0]) if all_words else False

        primary = brand_words[0]

        # Domain must START with the primary brand word
        starts = domain_slug.startswith(primary)
        if not starts and len(brand_words) >= 2:
            starts = domain_slug.startswith(''.join(brand_words[:2]))
        if not starts:
            return False

        # Pollution check: company words should "explain" the domain
        # If >25% of the domain is unexplained characters, it's a false match
        remaining = domain_slug
        for w in sorted(set(all_words), key=len, reverse=True):
            remaining = remaining.replace(w, '', 1)
        for tld in ['com', 'ca', 'net', 'org', 'inc', 'co']:
            remaining = remaining.replace(tld, '')
        pollution = len(remaining) / max(len(domain_slug), 1)
        if pollution > 0.25:
            return False

        return True

    # Score all domains
    query_words = set(re.sub(r'[^a-z0-9]', ' ', query.lower()).split())
    name_words  = set(re.sub(r'[^a-z0-9]', ' ', company_name.lower()).split())

    scored = []
    for domain in domains:
        d = re.sub(r'[^a-z0-9]', '', domain.lower())
        score = 0
        name_slug = re.sub(r'[^a-z0-9]', '', company_name.lower())
        if name_slug and name_slug in d:
            score += 15
        for word in name_words:
            if len(word) > 2 and word in d:
                score += 4
        for word in query_words:
            if len(word) > 2 and word in d:
                score += 2
        if domain.endswith('.ca'):
            score += 3
        elif domain.endswith('.com'):
            score += 1
        scored.append((score, domain))

    scored.sort(key=lambda x: (-x[0], len(x[1])))
    logger.debug("[Discoverer] Top candidates: %s", scored[:5])

    # Return first valid match
    for score, domain in scored:
        if _valid(domain, score):
            return domain

    return None


def _dedupe(lst: list) -> list:
    """Deduplicate list preserving order, case-insensitive."""
    seen = set()
    result = []
    for item in lst:
        key = str(item).lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def build_reddit_queries(company_name: str, aliases: list) -> list:
    """Build deduplicated Reddit search terms from name + aliases."""
    return _dedupe([company_name] + (aliases or []))
