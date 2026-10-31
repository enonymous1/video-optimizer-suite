#!/usr/bin/env python3
"""Lightweight dashboard for the pipeline's SQLite state.

Stack choice, deliberately: Flask + Jinja2 (app factory), not FastHTML.
That is the stack you have already built with (ss-sms-br viewer,
208-scan dashboard), and it keeps this dashboard a ~200-line file
instead of a new framework to learn for a queue view. HTMX is used
only for one thing -- polling the summary/queue fragments -- and it
is loaded from CDN with a plain-refresh fallback, so the page still
works with JavaScript disabled (the whole page is server-rendered).

Run:
    pip install flask
    video-dashboard --db state.db --port 5000
    # then open http://localhost:5000  (bind 0.0.0.0 in Docker)

Routes:
    GET /                 queue table + summary cards, filterable by
                          ?status=pending&candidate=High+Priority&source_kind=camcorder&q=...
    GET /api/summary      JSON summary (also the HTMX poll target)
    GET /api/queue        JSON queue rows (same filters as /)
    GET /file?path=...    one file's detail + attempt history

No actions are exposed: this dashboard is read-only by design. A web
"delete originals" or "re-run encode" button on a headless server is
how libraries get lost. Conversions are started from the CLI / cron,
where --replace and --yes are explicit decisions, not clicks.

Security note, read before binding: there is NO authentication,
TLS, or rate limiting here, and /api/queue returns full filesystem
paths — a library inventory. Bind loopback (127.0.0.1, the
default) for local use; expose it on a LAN only behind a reverse
proxy with auth (or a firewall rule), never by bare --host 0.0.0.0.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

BASE_TEMPLATE = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HEVC Pipeline Queue</title>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<style>
body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #ddd;padding:.4rem;text-align:left}
.cards{display:flex;gap:1rem;flex-wrap:wrap}.card{border:1px solid #ddd;border-radius:8px;padding:.8rem 1rem}
form{margin:1rem 0}input,select{padding:.3rem}
</style></head><body>
<h1>HEVC Pipeline Queue</h1>
<div id="summary" hx-get="/api/summary-html" hx-trigger="every 30s" hx-swap="innerHTML">
{{ summary_html|safe }}</div>
<form method="get">
  <input name="q" value="{{ q }}" placeholder="Search filename/path">
  <select name="status"><option value="">All statuses</option>
    {% for s in statuses %}<option value="{{ s }}" {{ 'selected' if s==status else '' }}>{{ s }}</option>{% endfor %}</select>
  <select name="candidate"><option value="">All candidates</option>
    {% for c in candidates %}<option value="{{ c }}" {{ 'selected' if c==candidate else '' }}>{{ c }}</option>{% endfor %}</select>
  <select name="source_kind"><option value="">All sources</option>
    {% for k in source_kinds %}<option value="{{ k }}" {{ 'selected' if k==source_kind else '' }}>{{ k }}</option>{% endfor %}</select>
  <button>Filter</button>
</form>
<p>Showing {{ rows|length }} of {{ s.total_files }} file(s) &middot; <a href="/api/queue?status={{ status|urlencode }}&amp;candidate={{ candidate|urlencode }}&amp;source_kind={{ source_kind|urlencode }}&amp;q={{ q|urlencode }}">JSON</a></p>
<table><tr><th>Status</th><th>File</th><th>Source</th><th>Size</th><th>Codec</th><th>Res</th><th>Est. save</th><th>Actual save</th></tr>
{% for r in rows %}<tr>
<td>{{ r.status }}</td>
<td><a href="/file?path={{ r.path|urlencode }}">{{ r.filename or r.path }}</a></td>
<td>{{ r.source_kind or '—' }}</td>
<td>{{ '%.2f'|format(r.size_gb or 0) }} GB</td>
<td>{{ r.video_codec or '?' }}</td><td>{{ r.resolution or '?' }}</td>
<td>{{ '%.2f'|format(r.estimated_savings_gb or 0) }} GB</td>
<td>{{ ('%.2f GB'|format(r.actual_savings_gb)) if r.actual_savings_gb is not none else '—' }}</td>
</tr>{% endfor %}</table>
</body></html>
"""

SUMMARY_TEMPLATE = """
<div class="cards">
<div class="card"><b>{{ s.total_files }}</b> files<br>{{ s.total_gb }} GB scanned</div>
<div class="card"><b>{{ s.total_saved_gb }} GB</b> actually saved</div>
{% for st, v in s.by_status.items() %}<div class="card"><b>{{ v.files }}</b> {{ st }}<br>{{ v.size_gb }} GB</div>{% endfor %}
</div>
"""

DETAIL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><title>File detail</title>
<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}pre{background:#f4f4f4;padding:1rem;overflow:auto}</style>
</head><body><p><a href="/">← Queue</a></p><h1>{{ row.filename or row.path }}</h1>
<pre>{{ row|tojson(indent=2) }}</pre><h2>Attempts</h2><ul>
{% for a in attempts %}<li>{{ a.finished_at }} — <b>{{ a.status }}</b> {{ a.detail or '' }}</li>{% else %}<li>No attempts yet.</li>{% endfor %}</ul>
</body></html>
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    # 30 s, not SQLite's 5 s default: the converter holds write locks
    # for whole encodes, and a dashboard query timing out mid-encode
    # is the most common 500 this view produces. A busy database
    # still surfaces as a 503 (see the error handler in create_app),
    # not a stack trace.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) n, COALESCE(SUM(size_gb),0) gb,"
        " COALESCE(SUM(actual_savings_gb),0) saved FROM files"
        " GROUP BY status").fetchall()
    total = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size_gb),0) gb,"
        " COALESCE(SUM(actual_savings_gb),0) saved FROM files").fetchone()
    return {"by_status": {r["status"]: {"files": r["n"],
                                        "size_gb": round(r["gb"], 2),
                                        "saved_gb": round(r["saved"], 2)}
                          for r in rows},
            "total_files": total["n"], "total_gb": round(total["gb"], 2),
            "total_saved_gb": round(total["saved"], 2)}


def _has_column(conn: sqlite3.Connection, column: str) -> bool:
    """True when *column* exists on files (old DBs predate source_kind)."""
    return any(r[1] == column
               for r in conn.execute("PRAGMA table_info(files)"))


def _queue(conn: sqlite3.Connection, status: str, candidate: str,
           q: str, limit: int = 500, source_kind: str = "") -> list[dict]:
    sql = "SELECT * FROM files WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if candidate:
        sql += " AND candidate = ?"
        params.append(candidate)
    if source_kind and _has_column(conn, "source_kind"):
        sql += " AND source_kind = ?"
        params.append(source_kind)
    if q:
        # Escape LIKE wildcards in the user's term: without this, a
        # search for "%" matches every row (filter bypass, not
        # injection — the parameters themselves are already safe).
        q_esc = (q.replace("\\", "\\\\").replace("%", "\\%")
                  .replace("_", "\\_"))
        sql += " AND (filename LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
        params += [f"%{q_esc}%", f"%{q_esc}%"]
    sql += " ORDER BY COALESCE(estimated_savings_gb,0) DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def create_app(db_path: Path) -> Flask:
    app = Flask(__name__)
    # Force Jinja autoescaping ON. These templates are string
    # templates (render_template_string), which have no filename, so
    # Flask's default extension-based autoescape selection leaves
    # them UNESCAPED: a filename like '"><svg onload=x>' from disk,
    # or a ?q= value reflected into the search box, would render as
    # live HTML. With autoescape forced on, {{ summary_html|safe }}
    # stays intentional (that fragment is rendered by this same
    # engine, so its own variables are escaped before insertion).
    # If this ever moves to templates/*.html files, this line becomes
    # redundant — do not delete it until then.
    app.jinja_env.autoescape = True
    app.config["DB_PATH"] = Path(db_path)

    @app.errorhandler(sqlite3.OperationalError)
    def _db_busy(_exc: Exception) -> tuple[str, int]:
        return ("Database is busy (the converter holds the write lock); "
                "wait a moment and refresh."), 503

    @app.get("/")
    def index() -> str:
        status = request.args.get("status", "")
        candidate = request.args.get("candidate", "")
        source_kind = request.args.get("source_kind", "")
        q = request.args.get("q", "")
        conn = _connect(app.config["DB_PATH"])
        try:
            statuses = [r[0] for r in conn.execute(
                "SELECT DISTINCT status FROM files ORDER BY status")]
            candidates = [r[0] for r in conn.execute(
                "SELECT DISTINCT candidate FROM files WHERE candidate IS NOT NULL"
                " ORDER BY candidate")]
            source_kinds = ([r[0] for r in conn.execute(
                "SELECT DISTINCT source_kind FROM files "
                "WHERE source_kind IS NOT NULL ORDER BY source_kind")]
                if _has_column(conn, "source_kind") else [])
            s = _summary(conn)
            rows = _queue(conn, status, candidate, q, source_kind=source_kind)
            summary_html = render_template_string(SUMMARY_TEMPLATE, s=s)
            return render_template_string(
                BASE_TEMPLATE, summary_html=summary_html, rows=rows, s=s,
                statuses=statuses, candidates=candidates,
                source_kinds=source_kinds,
                status=status, candidate=candidate,
                source_kind=source_kind, q=q)
        finally:
            conn.close()

    @app.get("/api/summary")
    def api_summary() -> object:
        conn = _connect(app.config["DB_PATH"])
        try:
            return jsonify(_summary(conn))
        finally:
            conn.close()

    @app.get("/api/summary-html")
    def api_summary_html() -> str:
        conn = _connect(app.config["DB_PATH"])
        try:
            return render_template_string(SUMMARY_TEMPLATE, s=_summary(conn))
        finally:
            conn.close()

    @app.get("/api/queue")
    def api_queue() -> object:
        conn = _connect(app.config["DB_PATH"])
        try:
            return jsonify(_queue(conn, request.args.get("status", ""),
                                  request.args.get("candidate", ""),
                                  request.args.get("q", ""),
                                  source_kind=request.args.get("source_kind", "")))
        finally:
            conn.close()

    @app.get("/file")
    def file_detail() -> str:
        path = request.args.get("path", "")
        conn = _connect(app.config["DB_PATH"])
        try:
            row = conn.execute("SELECT * FROM files WHERE path = ?",
                               (path,)).fetchone()
            if row is None:
                return "File not in state DB", 404
            attempts = [dict(r) for r in conn.execute(
                "SELECT * FROM attempts WHERE path = ?"
                " ORDER BY id DESC LIMIT 50", (path,))]
            return render_template_string(DETAIL_TEMPLATE, row=dict(row),
                                          attempts=attempts)
        finally:
            conn.close()

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="HEVC pipeline dashboard.")
    ap.add_argument("--db", type=Path, default=Path("state.db"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    if not args.db.is_file():
        print(f"NOTE: state DB not found yet: {args.db} "
              f"(run video-pipeline or video-state import first; "
              f"the dashboard will show empty tables until then.)")
    app = create_app(args.db)
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
