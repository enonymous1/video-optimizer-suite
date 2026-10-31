#!/usr/bin/env python3
"""Dashboard tests: escaping, queue counts, LIKE wildcards.

    python tests/test_dashboard.py

Skips (exit 0) when Flask is not installed — the dashboard is the
only Flask-dependent part of the suite, and CI does not install
requirements on every runner. When Flask is present, uses its test
client against a synthetic state DB; no server is started and no
real media is needed.

Covers the defects fixed in the 2026-10-31 staff review:
  * string templates (render_template_string) have no filename, so
    Flask's extension-based autoescape left them unescaped: a
    filename or ?q= value containing HTML rendered live. Fixed by
    forcing app.jinja_env.autoescape = True in create_app().
  * the queue page printed "N file(s)" from the capped LIMIT 500
    query while the summary counted all files, so the two numbers
    disagreed on libraries over 500 files. The page now prints
    "Showing X of N".
  * a ?q= value of "%" matched every row, because LIKE wildcards
    were not escaped. _queue() now escapes \\, %, and _.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask not installed (dashboard tests need it).")
    raise SystemExit(0)

from dashboard import create_app  # noqa: E402


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "state.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE files (path TEXT, filename TEXT, status TEXT,"
                     " candidate TEXT, source_kind TEXT, size_gb REAL,"
                     " video_codec TEXT, resolution TEXT,"
                     " estimated_savings_gb REAL, actual_savings_gb REAL)")
        conn.execute("CREATE TABLE attempts (id INTEGER PRIMARY KEY, path TEXT,"
                     " finished_at TEXT, status TEXT, detail TEXT)")
        evil = '"><svg onload=alert(1)>.mkv'
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (f"/media/{evil}", evil, "pending", "High Priority",
                      "flat", 1.0, "H.264", "1920x1080", 0.5, None))
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("/media/plain.mkv", "plain.mkv", "converted",
                      "Medium", "flat", 2.0, "HEVC", "1920x1080", 1.0, 1.0))
        conn.execute("INSERT INTO attempts VALUES (1,?,?,?,?)",
                     (f"/media/{evil}", "2026-01-01", "failed",
                      "<script>alert(2)</script>"))
        conn.commit()
        conn.close()

        app = create_app(db)
        app.testing = True
        client = app.test_client()

        body = client.get("/", query_string={"q": '"><svg onload=alert(9)>'}
                          ).get_data(as_text=True)
        check("reflected ?q= payload is escaped, not live HTML",
              "<svg onload" not in body and "&lt;svg" in body)
        check("queue count shows shown-of-total",
              "Showing 0 of 2" in body)
        body = client.get("/", query_string={"q": "svg"}
                          ).get_data(as_text=True)
        check("matching filter shows 1 of 2",
              "Showing 1 of 2" in body)
        body = client.get("/", query_string={"q": "%"}
                          ).get_data(as_text=True)
        check("LIKE wildcard % does not match every row",
              "Showing 0 of 2" in body)
        detail = client.get("/file", query_string={"path": f"/media/{evil}"}
                            ).get_data(as_text=True)
        check("attempt detail HTML is escaped",
              "<script>alert(2)</script>" not in detail
              and "&lt;script&gt;" in detail)
        check("unknown file is 404, not a crash",
              client.get("/file", query_string={"path": "/nope.mkv"}
                         ).status_code == 404)

    print(f"\n{len(failures)} failure(s)" if failures else "\nAll dashboard checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
