"""
ReviewLedger · Database Layer
Supports both PostgreSQL (production) and SQLite (local dev).
Set DATABASE_URL env var for PostgreSQL, otherwise falls back to SQLite.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES  = bool(DATABASE_URL)

# ── CONNECTION ────────────────────────────────────────────────────────────────

def get_conn():
    if USE_POSTGRES:
        import psycopg2
        import psycopg2.extras
        url = DATABASE_URL
        # Railway sometimes gives postgres:// but psycopg2 needs postgresql://
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url)
        conn.autocommit = False
        return conn
    else:
        import sqlite3
        db_path = Path(__file__).parent / "storage" / "reviewledger.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

def placeholder(n=1):
    """Return correct placeholder — %s for postgres, ? for sqlite."""
    p = "%s" if USE_POSTGRES else "?"
    return p if n == 1 else "(" + ",".join([p]*n) + ")"

def ph(n=1):
    return placeholder(n)

# ── SCHEMA ────────────────────────────────────────────────────────────────────

def init_db():
    if USE_POSTGRES:
        _init_postgres()
    else:
        _init_sqlite()

def _init_postgres():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS raw_reviews (
            review_id           TEXT PRIMARY KEY,
            platform            TEXT NOT NULL,
            competitor_name     TEXT NOT NULL,
            competitor_slug     TEXT NOT NULL,
            rating              REAL NOT NULL,
            title               TEXT,
            body                TEXT NOT NULL,
            author              TEXT,
            author_role         TEXT,
            author_company      TEXT,
            author_company_size TEXT,
            review_date         TEXT NOT NULL,
            platform_url        TEXT,
            scraped_at          TEXT NOT NULL,
            raw_html_hash       TEXT,
            classified          INTEGER DEFAULT 0
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_comp   ON raw_reviews(competitor_slug)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_class  ON raw_reviews(classified)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_date   ON raw_reviews(review_date)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS classified_reviews (
            review_id           TEXT PRIMARY KEY,
            platform            TEXT NOT NULL,
            competitor_name     TEXT NOT NULL,
            competitor_slug     TEXT NOT NULL,
            rating              REAL NOT NULL,
            title               TEXT,
            body                TEXT NOT NULL,
            author              TEXT,
            author_role         TEXT,
            author_company      TEXT,
            author_company_size TEXT,
            review_date         TEXT NOT NULL,
            platform_url        TEXT,
            scraped_at          TEXT NOT NULL,
            sentiment           TEXT NOT NULL,
            topics              TEXT NOT NULL,
            intensity_score     REAL NOT NULL,
            feature_requests    TEXT,
            key_phrases         TEXT,
            summary             TEXT,
            classified_at       TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cl_comp    ON classified_reviews(competitor_slug)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cl_sent    ON classified_reviews(sentiment)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cl_date    ON classified_reviews(review_date)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            signal_id       TEXT PRIMARY KEY,
            signal_type     TEXT NOT NULL,
            competitor_slug TEXT NOT NULL,
            competitor_name TEXT NOT NULL,
            topic           TEXT NOT NULL,
            headline        TEXT NOT NULL,
            body            TEXT NOT NULL,
            evidence        TEXT NOT NULL,
            intensity       REAL NOT NULL,
            review_count    INTEGER NOT NULL,
            period_days     INTEGER NOT NULL,
            generated_at    TEXT NOT NULL,
            is_alert        INTEGER DEFAULT 0,
            delivered       INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS review_hashes (
            hash        TEXT PRIMARY KEY,
            review_id   TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_projects (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            subject     TEXT NOT NULL,
            competitors TEXT NOT NULL,
            timeline    INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            status      TEXT DEFAULT 'pending',
            report_html TEXT,
            summary     TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS job_logs (
            id         SERIAL PRIMARY KEY,
            project_id TEXT NOT NULL,
            ts         TEXT NOT NULL,
            level      TEXT NOT NULL,
            msg        TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    logger.info("PostgreSQL schema ready")

def _init_sqlite():
    import sqlite3
    db_path = Path(__file__).parent / "storage" / "reviewledger.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS raw_reviews (
            review_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
            competitor_name TEXT NOT NULL, competitor_slug TEXT NOT NULL,
            rating REAL NOT NULL, title TEXT, body TEXT NOT NULL,
            author TEXT, author_role TEXT, author_company TEXT,
            author_company_size TEXT, review_date TEXT NOT NULL,
            platform_url TEXT, scraped_at TEXT NOT NULL,
            raw_html_hash TEXT, classified INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_raw_comp  ON raw_reviews(competitor_slug);
        CREATE INDEX IF NOT EXISTS idx_raw_class ON raw_reviews(classified);
        CREATE INDEX IF NOT EXISTS idx_raw_date  ON raw_reviews(review_date);

        CREATE TABLE IF NOT EXISTS classified_reviews (
            review_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
            competitor_name TEXT NOT NULL, competitor_slug TEXT NOT NULL,
            rating REAL NOT NULL, title TEXT, body TEXT NOT NULL,
            author TEXT, author_role TEXT, author_company TEXT,
            author_company_size TEXT, review_date TEXT NOT NULL,
            platform_url TEXT, scraped_at TEXT NOT NULL,
            sentiment TEXT NOT NULL, topics TEXT NOT NULL,
            intensity_score REAL NOT NULL, feature_requests TEXT,
            key_phrases TEXT, summary TEXT, classified_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cl_comp ON classified_reviews(competitor_slug);
        CREATE INDEX IF NOT EXISTS idx_cl_sent ON classified_reviews(sentiment);
        CREATE INDEX IF NOT EXISTS idx_cl_date ON classified_reviews(review_date);

        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY, signal_type TEXT NOT NULL,
            competitor_slug TEXT NOT NULL, competitor_name TEXT NOT NULL,
            topic TEXT NOT NULL, headline TEXT NOT NULL, body TEXT NOT NULL,
            evidence TEXT NOT NULL, intensity REAL NOT NULL,
            review_count INTEGER NOT NULL, period_days INTEGER NOT NULL,
            generated_at TEXT NOT NULL, is_alert INTEGER DEFAULT 0,
            delivered INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS review_hashes (
            hash TEXT PRIMARY KEY, review_id TEXT NOT NULL, created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL,
            competitors TEXT NOT NULL, timeline INTEGER NOT NULL,
            created_at TEXT NOT NULL, status TEXT DEFAULT 'pending',
            report_html TEXT, summary TEXT
        );

        CREATE TABLE IF NOT EXISTS job_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL, ts TEXT NOT NULL,
            level TEXT NOT NULL, msg TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    logger.info("SQLite schema ready")


# ── REVIEW OPERATIONS ─────────────────────────────────────────────────────────

from models import RawReview, ClassifiedReview, Signal, Platform, Sentiment, TopicCluster, SignalType

def insert_raw_review(review: RawReview) -> bool:
    P = "%s" if USE_POSTGRES else "?"
    conn = get_conn()
    try:
        cur = conn.cursor()
        if review.raw_html_hash:
            cur.execute(f"SELECT 1 FROM review_hashes WHERE hash={P}", (review.raw_html_hash,))
            if cur.fetchone():
                return False
        try:
            cur.execute(f"""
                INSERT INTO raw_reviews VALUES ({','.join([P]*16)})
            """, (
                review.review_id, review.platform.value,
                review.competitor_name, review.competitor_slug,
                review.rating, review.title, review.body, review.author,
                review.author_role, review.author_company, review.author_company_size,
                review.review_date.isoformat(), review.platform_url,
                review.scraped_at.isoformat(), review.raw_html_hash, 0,
            ))
            if review.raw_html_hash:
                cur.execute(f"INSERT INTO review_hashes VALUES ({P},{P},{P})",
                    (review.raw_html_hash, review.review_id, datetime.utcnow().isoformat()))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
    finally:
        conn.close()

def get_unclassified_reviews(limit: int = 2000) -> list:
    P = "%s" if USE_POSTGRES else "?"
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM raw_reviews WHERE classified=0 ORDER BY scraped_at ASC LIMIT {P}", (limit,))
        rows = cur.fetchall()
        return [_row_to_raw(r) for r in rows]
    finally:
        conn.close()

def insert_classified_review(review: ClassifiedReview):
    P = "%s" if USE_POSTGRES else "?"
    upsert = (
        f"INSERT INTO classified_reviews VALUES ({','.join([P]*21)}) ON CONFLICT(review_id) DO NOTHING"
        if USE_POSTGRES else
        f"INSERT OR REPLACE INTO classified_reviews VALUES ({','.join(['?']*21)})"
    )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(upsert, (
            review.review_id, review.platform.value,
            review.competitor_name, review.competitor_slug,
            review.rating, review.title, review.body, review.author,
            review.author_role, review.author_company, review.author_company_size,
            review.review_date.isoformat(), review.platform_url,
            review.scraped_at.isoformat(),
            review.sentiment.value,
            json.dumps([t.value for t in review.topics]),
            review.intensity_score,
            json.dumps(review.feature_requests),
            json.dumps(review.key_phrases),
            review.summary,
            review.classified_at.isoformat(),
        ))
        cur.execute(f"UPDATE raw_reviews SET classified=1 WHERE review_id={P}", (review.review_id,))
        conn.commit()
    finally:
        conn.close()

def get_classified_reviews(competitor_slug: str, sentiment=None, topic=None, days: int = 365) -> list:
    P = "%s" if USE_POSTGRES else "?"
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    query  = f"SELECT * FROM classified_reviews WHERE competitor_slug={P} AND review_date>={P}"
    params = [competitor_slug, cutoff]
    if sentiment:
        query += f" AND sentiment={P}"; params.append(sentiment.value)
    query += " ORDER BY review_date DESC"
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        results = [_row_to_classified(r) for r in cur.fetchall()]
    finally:
        conn.close()
    if topic:
        results = [r for r in results if topic in r.topics]
    return results

def insert_signal(signal: Signal):
    P = "%s" if USE_POSTGRES else "?"
    upsert = (
        f"INSERT INTO signals VALUES ({','.join([P]*14)}) ON CONFLICT(signal_id) DO NOTHING"
        if USE_POSTGRES else
        f"INSERT OR REPLACE INTO signals VALUES ({','.join(['?']*14)})"
    )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(upsert, (
            signal.signal_id, signal.signal_type.value,
            signal.competitor_slug, signal.competitor_name,
            signal.topic.value, signal.headline, signal.body,
            json.dumps(signal.evidence), signal.intensity,
            signal.review_count, signal.period_days,
            signal.generated_at.isoformat(), int(signal.is_alert), 0,
        ))
        conn.commit()
    finally:
        conn.close()

def get_pending_alerts() -> list:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM signals WHERE is_alert=1 AND delivered=0 ORDER BY generated_at DESC")
        return [_row_to_signal(r) for r in cur.fetchall()]
    finally:
        conn.close()

def get_scrape_stats() -> dict:
    conn = get_conn()
    try:
        cur = conn.cursor()
        def count(q): cur.execute(q); return cur.fetchone()[0]
        return {
            "total_raw":        count("SELECT COUNT(*) FROM raw_reviews"),
            "total_classified": count("SELECT COUNT(*) FROM classified_reviews"),
            "total_signals":    count("SELECT COUNT(*) FROM signals"),
            "pending_alerts":   count("SELECT COUNT(*) FROM signals WHERE is_alert=1 AND delivered=0"),
            "unclassified":     count("SELECT COUNT(*) FROM raw_reviews WHERE classified=0"),
        }
    finally:
        conn.close()


# ── PROJECT STORE (app-level, separate from review data) ─────────────────────

def save_project(proj: dict):
    P = "%s" if USE_POSTGRES else "?"
    upsert = (
        f"INSERT INTO app_projects VALUES ({','.join([P]*9)}) ON CONFLICT(id) DO UPDATE SET status=EXCLUDED.status, report_html=EXCLUDED.report_html, summary=EXCLUDED.summary"
        if USE_POSTGRES else
        "INSERT OR REPLACE INTO app_projects VALUES (?,?,?,?,?,?,?,?,?)"
    )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(upsert, (
            proj["id"], proj["name"], proj["subject"],
            json.dumps(proj["competitors"]), proj["timeline"],
            proj["created_at"], proj["status"],
            proj.get("report_html"), json.dumps(proj.get("summary", {})),
        ))
        conn.commit()
    finally:
        conn.close()

def load_projects() -> list:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM app_projects ORDER BY created_at DESC")
        rows = cur.fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        p = dict(zip([d[0] for d in cur.description], r)) if USE_POSTGRES else dict(r)
        p["competitors"] = json.loads(p["competitors"])
        p["summary"]     = json.loads(p["summary"] or "{}")
        result.append(p)
    return result

def load_project(pid: str) -> Optional[dict]:
    P = "%s" if USE_POSTGRES else "?"
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM app_projects WHERE id={P}", (pid,))
        row = cur.fetchone()
        if not row:
            return None
        p = dict(zip([d[0] for d in cur.description], row)) if USE_POSTGRES else dict(row)
        p["competitors"] = json.loads(p["competitors"])
        p["summary"]     = json.loads(p["summary"] or "{}")
        return p
    finally:
        conn.close()

def update_project_status(pid: str, status: str, report_html: str = None, summary: dict = None):
    P = "%s" if USE_POSTGRES else "?"
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE app_projects SET status={P}, report_html={P}, summary={P} WHERE id={P}",
            (status, report_html, json.dumps(summary or {}), pid)
        )
        conn.commit()
    finally:
        conn.close()

def log_job(project_id: str, level: str, msg: str):
    P = "%s" if USE_POSTGRES else "?"
    ts = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO job_logs(project_id,ts,level,msg) VALUES ({P},{P},{P},{P})",
                    (project_id, ts, level, msg))
        conn.commit()
    finally:
        conn.close()

def get_logs(project_id: str, since: int = 0) -> list:
    P = "%s" if USE_POSTGRES else "?"
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id,ts,level,msg FROM job_logs WHERE project_id={P} AND id>{P} ORDER BY id ASC",
            (project_id, since)
        )
        rows = cur.fetchall()
        return [{"id": r[0], "ts": r[1], "level": r[2], "msg": r[3]} for r in rows]
    finally:
        conn.close()


# ── ROW CONVERTERS ────────────────────────────────────────────────────────────

def _col(row, key):
    """Get column by name — works for both sqlite3.Row and psycopg2 tuple with description."""
    if isinstance(row, dict):
        return row[key]
    try:
        return row[key]
    except (TypeError, IndexError):
        return None

def _row_to_raw(row) -> RawReview:
    if USE_POSTGRES:
        row = dict(zip(
            ["review_id","platform","competitor_name","competitor_slug","rating","title","body",
             "author","author_role","author_company","author_company_size","review_date",
             "platform_url","scraped_at","raw_html_hash","classified"], row
        ))
    return RawReview(
        review_id=row["review_id"], platform=Platform(row["platform"]),
        competitor_name=row["competitor_name"], competitor_slug=row["competitor_slug"],
        rating=row["rating"], title=row["title"] or "",
        body=row["body"], author=row["author"] or "",
        author_role=row["author_role"], author_company=row["author_company"],
        author_company_size=row["author_company_size"],
        review_date=datetime.fromisoformat(row["review_date"]),
        platform_url=row["platform_url"] or "",
        scraped_at=datetime.fromisoformat(row["scraped_at"]),
        raw_html_hash=row["raw_html_hash"] or "",
    )

def _row_to_classified(row) -> ClassifiedReview:
    if USE_POSTGRES:
        cols = ["review_id","platform","competitor_name","competitor_slug","rating","title","body",
                "author","author_role","author_company","author_company_size","review_date",
                "platform_url","scraped_at","sentiment","topics","intensity_score",
                "feature_requests","key_phrases","summary","classified_at"]
        row = dict(zip(cols, row))
    return ClassifiedReview(
        review_id=row["review_id"], platform=Platform(row["platform"]),
        competitor_name=row["competitor_name"], competitor_slug=row["competitor_slug"],
        rating=row["rating"], title=row["title"] or "",
        body=row["body"], author=row["author"] or "",
        author_role=row["author_role"], author_company=row["author_company"],
        author_company_size=row["author_company_size"],
        review_date=datetime.fromisoformat(row["review_date"]),
        platform_url=row["platform_url"] or "",
        scraped_at=datetime.fromisoformat(row["scraped_at"]),
        sentiment=Sentiment(row["sentiment"]),
        topics=[TopicCluster(t) for t in json.loads(row["topics"])],
        intensity_score=row["intensity_score"],
        feature_requests=json.loads(row["feature_requests"] or "[]"),
        key_phrases=json.loads(row["key_phrases"] or "[]"),
        summary=row["summary"] or "",
        classified_at=datetime.fromisoformat(row["classified_at"]),
    )

def _row_to_signal(row) -> Signal:
    if USE_POSTGRES:
        cols = ["signal_id","signal_type","competitor_slug","competitor_name","topic",
                "headline","body","evidence","intensity","review_count","period_days",
                "generated_at","is_alert","delivered"]
        row = dict(zip(cols, row))
    return Signal(
        signal_id=row["signal_id"], signal_type=SignalType(row["signal_type"]),
        competitor_slug=row["competitor_slug"], competitor_name=row["competitor_name"],
        topic=TopicCluster(row["topic"]), headline=row["headline"], body=row["body"],
        evidence=json.loads(row["evidence"]),
        intensity=row["intensity"], review_count=row["review_count"],
        period_days=row["period_days"],
        generated_at=datetime.fromisoformat(row["generated_at"]),
        is_alert=bool(row["is_alert"]),
    )
