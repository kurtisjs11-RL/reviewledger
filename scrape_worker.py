"""
ReviewLedger · Scrape Worker (consolidated deploy version)
Runs as subprocess, all companies in parallel.
"""

import sys
import os
import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("worker")


def main():
    if len(sys.argv) < 3:
        sys.exit(1)

    job     = json.loads(Path(sys.argv[1]).read_text())
    out     = Path(sys.argv[2])
    entities      = job["entities"]
    platform_list = job["platform_list"]
    db_url        = job.get("db_url", "")
    db_path       = job.get("db_path", "")

    # Set DATABASE_URL so database.py uses the right backend
    if db_url:
        os.environ["DATABASE_URL"] = db_url

    import database as db
    if db_path and not db_url:
        db.DB_PATH = Path(db_path)
    db.init_db()

    from database import insert_raw_review
    from models import RawReview, Platform
    from scrapers.industry_sources import build_platform_urls
    from scrapers.google_reviews   import scrape_google_reviews
    from scrapers.bbb_scraper      import scrape_bbb
    from scrapers.generic_scraper  import scrape_generic
    from scrapers.trustpilot_capterra import TrustpilotScraper
    from scrapers.reddit_scraper      import RedditScraper

    log_lines = []
    log_lock  = threading.Lock()
    db_lock   = threading.Lock()

    def log(msg, level="info"):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        with log_lock:
            log_lines.append({"ts": ts, "level": level, "msg": msg})
        getattr(logger, level if level in ("info","warning","error") else "info")(msg)

    def safe_insert(r):
        with db_lock:
            return insert_raw_review(r)

    def to_raw(sr, slug, name):
        try: plat = Platform(sr.platform)
        except: plat = Platform.G2
        return RawReview(
            review_id=sr.review_id, platform=plat,
            competitor_name=name, competitor_slug=slug,
            rating=sr.rating, title=sr.title or "", body=sr.body,
            author=sr.author, author_role=sr.author_role,
            author_company=sr.author_company, author_company_size=None,
            review_date=sr.review_date, platform_url=sr.platform_url,
            raw_html_hash=sr.raw_hash,
        )

    def scrape_company(comp, pw):
        name    = comp["name"]
        slug    = comp["slug"]
        aliases = comp.get("aliases", [name])
        tp_url  = comp["platforms"].get("trustpilot")
        new     = 0
        log(f"Scraping: {name}")

        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}, locale="en-CA",
        )
        ctx.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,eot}", lambda r: r.abort())

        platform_urls = build_platform_urls(platform_list, name, None, tp_url)

        for pid, purl in platform_urls.items():
            try:
                if pid == "trustpilot":
                    if not tp_url:
                        log(f"  trustpilot: no URL", "warning"); continue
                    scraper = TrustpilotScraper()
                    def _tp_fetch(url, _c=ctx):
                        from scrapers.base_scraper import get_playwright_page
                        from bs4 import BeautifulSoup
                        page = get_playwright_page(_c, url, wait=1.5)
                        if not page: return None
                        html = page.content(); page.close()
                        return BeautifulSoup(html, "html.parser")
                    scraper._fetch_with_playwright = _tp_fetch
                    raw = scraper.scrape_competitor(competitor_slug=slug, competitor_name=name, start_url=tp_url, max_pages=10)
                    n = sum(1 for r in raw if safe_insert(r))
                    new += n; log(f"  trustpilot: {len(raw)} found, {n} new")

                elif pid == "reddit":
                    scraper = RedditScraper()
                    all_rd = []
                    for term in aliases[:3]:
                        rd = scraper.scrape_competitor(competitor_slug=slug, competitor_name=name, start_url=term, max_pages=3)
                        all_rd.extend(rd)
                    n = sum(1 for r in all_rd if safe_insert(r))
                    new += n; log(f"  reddit: {len(all_rd)} found, {n} new")

                elif pid == "google":
                    scraped = scrape_google_reviews(ctx, name, slug, aliases, max_reviews=60)
                    n = sum(1 for s in scraped if safe_insert(to_raw(s, slug, name)))
                    new += n; log(f"  google: {len(scraped)} found, {n} new")

                elif pid == "bbb":
                    scraped = scrape_bbb(ctx, name, slug, aliases, max_reviews=40)
                    n = sum(1 for s in scraped if safe_insert(to_raw(s, slug, name)))
                    new += n; log(f"  bbb: {len(scraped)} found, {n} new")

                else:
                    scraped = scrape_generic(ctx, purl, pid, name, slug, max_reviews=40)
                    n = sum(1 for s in scraped if safe_insert(to_raw(s, slug, name)))
                    new += n
                    log(f"  {pid}: {len(scraped)} found, {n} new" if scraped else f"  {pid}: 0 found", "warning" if not scraped else "info")

            except Exception as e:
                log(f"  {pid} error: {str(e)[:100]}", "warning")

        browser.close()
        return new

    from playwright.sync_api import sync_playwright

    total_new = 0
    max_par = min(3, len(entities))
    log(f"Running {len(entities)} companies in parallel (max {max_par})...")

    def run_one(comp):
        with sync_playwright() as pw:
            return scrape_company(comp, pw)

    with ThreadPoolExecutor(max_workers=max_par) as ex:
        futures = {ex.submit(run_one, c): c["name"] for c in entities}
        for f in as_completed(futures):
            try:
                total_new += f.result()
            except Exception as e:
                log(f"Company failed [{futures[f]}]: {e}", "error")

    log(f"Scraping complete — {total_new} new reviews")
    out.write_text(json.dumps({"total_new": total_new, "logs": log_lines}))


if __name__ == "__main__":
    main()
