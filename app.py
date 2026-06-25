"""
ReviewLedger · Web Application
Consolidated single-repo version for deployment.
Uses PostgreSQL in production (DATABASE_URL env var), SQLite locally.
"""

import os
import sys
import json
import uuid
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template, request, jsonify, send_file
from jinja2 import Environment, FileSystemLoader

# ── PATH (single repo — no hacks needed) ─────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

import database as db
from database import log_job, get_logs, save_project, load_projects, load_project, update_project_status

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

# ── JOB TRACKING ─────────────────────────────────────────────────────────────
_jobs: dict = {}
_jobs_lock  = threading.Lock()


# ── PIPELINE ─────────────────────────────────────────────────────────────────

def run_pipeline(project_id: str, subject: str, competitors: list, timeline: int, subject_aliases: list = None):
    """Runs in a background thread."""

    def log(msg, level="info"):
        log_job(project_id, level, msg)
        with _jobs_lock:
            if project_id in _jobs:
                _jobs[project_id]["logs"].append({"ts": datetime.utcnow().isoformat(), "level": level, "msg": msg})

    try:
        with _jobs_lock:
            _jobs[project_id] = {"status": "running", "progress": 0, "logs": [], "summary": {}}

        update_project_status(project_id, "running")

        log(f"Starting pipeline for project: {subject}")
        log(f"Competitors: {', '.join(c['name'] for c in competitors)}")
        log(f"Timeline: last {timeline} days")

        # ── STEP 1: DISCOVER TRUSTPILOT URLS IN PARALLEL ──────────────────────
        log("Discovering Trustpilot pages...")

        from discoverer import find_trustpilot_url, build_reddit_queries
        from scrapers.industry_sources import detect_industry, get_platforms_for_industry, build_platform_urls

        subject_aliases = subject_aliases or [subject]
        subject_slug    = subject.lower().replace(" ", "-").replace("'", "")

        all_to_discover = [(subject, subject_aliases, True)] + [
            (c["name"], c.get("aliases", [c["name"]]), False) for c in competitors
        ]

        log(f"Searching {len(all_to_discover)} companies in parallel...")

        def _discover(args):
            name, aliases, is_subj = args
            return name, aliases, is_subj, find_trustpilot_url(name, aliases), build_reddit_queries(name, aliases)

        discovery = {}
        with ThreadPoolExecutor(max_workers=min(4, len(all_to_discover))) as ex:
            futures = {ex.submit(_discover, item): item[0] for item in all_to_discover}
            for f in as_completed(futures):
                name, aliases, is_subj, tp_url, reddit_terms = f.result()
                label = "Subject" if is_subj else name
                log(f"  {'✓' if tp_url else '✗'} {label}: {tp_url or 'no Trustpilot found'}")
                if reddit_terms:
                    log(f"  Reddit terms for {name}: {', '.join(reddit_terms[:3])}")
                discovery[name] = (aliases, is_subj, tp_url, reddit_terms)

        # Build configs
        subj_aliases, _, subj_tp, subj_rd = discovery[subject]
        subject_config = {
            "name": subject, "slug": subject_slug, "aliases": subj_aliases,
            "platforms": {**({"trustpilot": subj_tp} if subj_tp else {}),
                          "reddit": subj_rd[0] if subj_rd else subject},
            "max_pages": 10, "is_subject": True,
        }

        comp_config = []
        for c in competitors:
            name = c["name"]
            aliases, _, tp_url, reddit_terms = discovery[name]
            slug = name.lower().replace(" ", "-").replace("'", "")
            comp_config.append({
                "name": name, "slug": slug, "aliases": aliases,
                "platforms": {**({"trustpilot": tp_url} if tp_url else {}),
                              "reddit": reddit_terms[0] if reddit_terms else name},
                "max_pages": 10, "is_subject": False,
            })

        all_entities = [subject_config] + comp_config

        with _jobs_lock:
            _jobs[project_id]["progress"] = 15

        # ── STEP 2: DETECT INDUSTRY ────────────────────────────────────────────
        log("Detecting industry...")
        industry      = detect_industry(subject, subject_aliases)
        platform_list = get_platforms_for_industry(industry)
        log(f"Industry: {industry} → sources: {', '.join(p[1] for p in platform_list)}")

        with _jobs_lock:
            _jobs[project_id]["progress"] = 20

        # ── STEP 3: SCRAPE (subprocess) ────────────────────────────────────────
        log("Starting multi-source scraping...")

        import subprocess, tempfile

        job_payload = {
            "entities": all_entities,
            "industry": industry,
            "platform_list": platform_list,
            "db_url": os.environ.get("DATABASE_URL", ""),
            "db_path": str(BASE_DIR / "storage" / "reviewledger.db"),
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as jf:
            json.dump(job_payload, jf)
            job_file = jf.name

        out_file    = job_file.replace(".json", "_out.json")
        worker_path = BASE_DIR / "scrape_worker.py"

        proc = subprocess.run(
            [sys.executable, str(worker_path), job_file, out_file],
            capture_output=True, text=True, timeout=1800
        )

        total_new = 0
        if Path(out_file).exists():
            result = json.loads(Path(out_file).read_text())
            total_new = result.get("total_new", 0)
            for wlog in result.get("logs", []):
                log(wlog["msg"], wlog.get("level", "info"))
        else:
            for line in (proc.stderr or "").splitlines()[-20:]:
                if line.strip():
                    log(f"[worker] {line}", "warning")

        for f in [job_file, out_file]:
            try: os.unlink(f)
            except: pass

        log(f"Scraping complete — {total_new} new reviews")

        with _jobs_lock:
            _jobs[project_id]["progress"] = 65

        # ── STEP 4: CLASSIFY ───────────────────────────────────────────────────
        log("Classifying reviews with AI...")
        db.init_db()

        from pipeline.classifier import batch_classify, get_cost_estimate
        pending = db.get_unclassified_reviews(limit=2000)

        if pending:
            log(f"Classifying {len(pending)} new reviews...")
            classified = batch_classify(pending, batch_size=25)
            saved = 0
            for r in classified:
                try:
                    db.insert_classified_review(r)
                    saved += 1
                except Exception:
                    pass
            cost = get_cost_estimate()
            log(f"Classified {saved} reviews | Est. cost: ${cost['est_cost_usd']:.4f}")
        else:
            log("No new reviews to classify")

        with _jobs_lock:
            _jobs[project_id]["progress"] = 80

        # ── STEP 5: GENERATE SIGNALS ────────────────────────────────────────────
        log("Generating intelligence signals...")
        from pipeline.classifier import generate_signal
        from models import TopicCluster, Sentiment
        import threading as _t

        signals_generated = 0
        all_signals       = []
        sig_lock          = _t.Lock()

        # Pre-fetch all reviews
        review_cache = {}
        for comp in all_entities:
            slug = comp["slug"]
            for topic in TopicCluster:
                reviews = db.get_classified_reviews(competitor_slug=slug, topic=topic, days=timeline)
                if reviews:
                    review_cache[(slug, topic)] = reviews

        def _gen(comp, topic):
            slug    = comp["slug"]
            name    = comp["name"]
            reviews = review_cache.get((slug, topic), [])
            if len(reviews) < 3:
                return None
            pain   = [r for r in reviews if r.sentiment == Sentiment.PAIN]
            praise = [r for r in reviews if r.sentiment == Sentiment.PRAISE]
            if len(pain) < 2 and len(praise) < 2:
                return None
            return generate_signal(
                competitor_name=name, competitor_slug=slug,
                topic=topic, reviews=reviews, days=timeline,
            )

        work = [(comp, topic) for comp in all_entities for topic in TopicCluster]
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_gen, c, t): (c, t) for c, t in work}
            for fut in as_completed(futures):
                signal = fut.result()
                if signal:
                    with sig_lock:
                        db.insert_signal(signal)
                        signals_generated += 1
                        all_signals.append(signal)
                    if signal.is_alert:
                        log(f"🚨 ALERT: [{signal.competitor_name}] {signal.headline}", "alert")

        log(f"Generated {signals_generated} signals")

        with _jobs_lock:
            _jobs[project_id]["progress"] = 90

        # ── STEP 6: BUILD REPORT ────────────────────────────────────────────────
        log("Building report...")
        from collections import Counter

        report_data = {"subject": subject, "subject_slug": subject_slug, "companies": []}

        for comp in all_entities:
            slug       = comp["slug"]
            is_subject = comp.get("is_subject", False)
            reviews    = db.get_classified_reviews(competitor_slug=slug, days=timeline)
            pain_count    = sum(1 for r in reviews if r.sentiment.value == "pain")
            praise_count  = sum(1 for r in reviews if r.sentiment.value == "praise")
            total         = len(reviews) or 1

            pain_topics   = Counter()
            praise_topics = Counter()
            for r in reviews:
                for t in r.topics:
                    if r.sentiment.value == "pain":
                        pain_topics[t.value] += 1
                    else:
                        praise_topics[t.value] += 1

            comp_sigs  = [s for s in all_signals if s.competitor_slug == slug]
            alerts     = [s for s in comp_sigs if s.is_alert]

            report_data["companies"].append({
                "name":           comp["name"],
                "slug":           slug,
                "is_subject":     is_subject,
                "total_reviews":  len(reviews),
                "pain_count":     pain_count,
                "praise_count":   praise_count,
                "neutral_count":  len(reviews) - pain_count - praise_count,
                "pain_rate":      round(pain_count / total * 100),
                "praise_rate":    round(praise_count / total * 100),
                "top_complaints": pain_topics.most_common(5),
                "top_praises":    praise_topics.most_common(5),
                "signals":        len(comp_sigs),
                "alerts":         len(alerts),
                "signal_details": [
                    {"type": s.signal_type.value, "topic": s.topic.value,
                     "headline": s.headline, "body": s.body,
                     "intensity": s.intensity, "is_alert": s.is_alert}
                    for s in comp_sigs
                ],
            })

        report_data["subject_data"]  = next((c for c in report_data["companies"] if c["is_subject"]), None)
        report_data["competitors"]   = [c for c in report_data["companies"] if not c["is_subject"]]

        # Render report without Flask context
        jinja_env   = Environment(loader=FileSystemLoader(str(BASE_DIR / "templates")))
        template    = jinja_env.get_template("report.html")
        report_html = template.render(
            data=report_data,
            generated_at=datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC"),
            timeline=timeline,
        )

        total_reviews = sum(c["total_reviews"] for c in report_data["companies"])
        total_alerts  = sum(c["alerts"] for c in report_data["companies"])
        summary = {
            "total_reviews": total_reviews,
            "total_signals": signals_generated,
            "total_alerts":  total_alerts,
            "competitors":   len(all_entities),
        }

        update_project_status(project_id, "done", report_html=report_html, summary=summary)

        with _jobs_lock:
            _jobs[project_id]["status"]   = "done"
            _jobs[project_id]["progress"] = 100
            _jobs[project_id]["summary"]  = summary

        log(f"✓ Pipeline complete — {total_reviews} reviews, {signals_generated} signals, {total_alerts} alerts")

    except Exception as e:
        import traceback
        log(f"Pipeline error: {e}", "error")
        log(traceback.format_exc(), "error")
        with _jobs_lock:
            if project_id in _jobs:
                _jobs[project_id]["status"] = "error"
        update_project_status(project_id, "error")


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    projects = load_projects()
    return render_template("index.html", projects=projects)

@app.route("/new")
def new_project():
    return render_template("new_project.html")

@app.route("/api/project", methods=["POST"])
def create_project():
    data        = request.json
    pid         = str(uuid.uuid4())[:8]
    competitors = data.get("competitors", [])
    subject_aliases = data.get("subject_aliases", [data.get("subject", "")])

    project = {
        "id":          pid,
        "name":        data.get("name", f"Project {pid}"),
        "subject":     data.get("subject", ""),
        "competitors": competitors,
        "timeline":    int(data.get("timeline", 365)),
        "created_at":  datetime.utcnow().isoformat(),
        "status":      "running",
        "report_html": None,
        "summary":     {},
    }
    save_project(project)

    t = threading.Thread(
        target=run_pipeline,
        args=(pid, project["subject"], competitors, project["timeline"], subject_aliases),
        daemon=True,
    )
    t.start()

    return jsonify({"project_id": pid})

@app.route("/project/<pid>")
def project_view(pid):
    project = load_project(pid)
    if not project:
        return "Project not found", 404
    return render_template("project.html", project=project)

@app.route("/api/project/<pid>/status")
def project_status(pid):
    since = int(request.args.get("since", 0))
    logs  = get_logs(pid, since=since)

    with _jobs_lock:
        job = _jobs.get(pid, {})

    project  = load_project(pid)
    status   = project["status"] if project else job.get("status", "unknown")
    progress = job.get("progress", 100 if status == "done" else 0)

    return jsonify({
        "status":   status,
        "progress": progress,
        "logs":     logs,
        "summary":  job.get("summary", project.get("summary", {}) if project else {}),
    })

@app.route("/project/<pid>/report")
def project_report(pid):
    project = load_project(pid)
    if not project or not project.get("report_html"):
        return "Report not ready", 404
    return project["report_html"]

@app.route("/project/<pid>/export")
def export_report(pid):
    project = load_project(pid)
    if not project or not project.get("report_html"):
        return "Report not ready", 404
    from io import BytesIO
    buf = BytesIO(project["report_html"].encode())
    buf.seek(0)
    fname = f"reviewledger-{project['name'].lower().replace(' ', '-')}-{pid}.html"
    return send_file(buf, mimetype="text/html", as_attachment=True, download_name=fname)

@app.route("/project/<pid>/delete", methods=["POST"])
def delete_project(pid):
    import database as _db2
    P = "%s" if _db2.USE_POSTGRES else "?"
    conn = _db2.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM app_projects WHERE id={P}", (pid,))
        cur.execute(f"DELETE FROM job_logs WHERE project_id={P}", (pid,))
        conn.commit()
    finally:
        conn.close()
    with _jobs_lock:
        _jobs.pop(pid, None)
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


# ── STARTUP ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", 8080))
    print(f"\n{'═'*50}")
    print(f"  ReviewLedger · {'Production' if db.USE_POSTGRES else 'Local Dev'}")
    print(f"  http://localhost:{port}")
    print(f"{'═'*50}\n")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
