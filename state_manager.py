#!/usr/bin/env python3
"""SQLite state manager for the video optimization pipeline.

Why SQLite instead of only the scanner's JSON/CSV checklist?
The JSON report is a snapshot: every re-scan rewrites it, concurrent
writers can clobber each other, and "which files are still pending?"
is a full-file parse. SQLite gives the pipeline:

* one row per source file, keyed by absolute path (PRIMARY KEY);
* atomic job claiming, so two converter processes cannot pick up the
  same file (BEGIN IMMEDIATE + a single UPDATE ... WHERE status IN ...);
* a durable status history (attempts table) instead of one overwritable
  ``Conversion_Status`` cell;
* cheap queries for the dashboard (counts by status, total saved).

The JSON/CSV reports are NOT removed: ``import_report()`` feeds them
into the DB, and ``export_json()`` writes them back out, so the
scanner, Excel, and the dashboard can all read the same truth.

Statuses (the only values the converter/verifier may write):
    pending           -- seen by a scan, never attempted (or reset)
    queued            -- selected for this run, not yet started
    converting        -- an encode is in progress right now
    verified          -- encode passed verification, not yet replaced
    converted         -- replaced in place, subtitles copied
    converted_no_subs -- replaced in place, subtitles dropped
    failed            -- ffmpeg failed
    failed_verify     -- encode finished but verification failed
    skipped           -- user skipped, or modern codec / too small, etc.
    interrupted       -- process died / was killed mid-encode.
                          Never "converted": the next run retries it.

Fingerprint rule (import): a file is identified by path + size +
mtime. If a re-scan finds the same path with a different size/mtime,
the file changed since conversion, so a ``converted`` row is reset to
``pending`` -- otherwise the pipeline would keep skipping a file that
is no longer the file that was converted. If size/mtime match, an
existing ``converted`` row is never downgraded by a re-import.

DVD jobs are the exception: their path is a synthetic title-set key
(``<VIDEO_TS>/VTS_01``), not a file, so there is no mtime to compare
and the fingerprint is size-only (summed VOB bytes). A changed disc
with the same total size would be missed by the fingerprint -- the
same limitation the flat-file scanner has for in-place edits that
preserve size exactly.

Usage as a library:
    db = StateDB(Path("state.db"))
    db.import_report(rows_from_scan_json)
    job = db.claim_next(priority="High Priority")
    db.set_status(job["path"], "converting")

Usage as a CLI:
    python state_manager.py import --report reports/*.json --db state.db
    python state_manager.py list --db state.db --status pending --limit 20
    python state_manager.py summary --db state.db
    python state_manager.py export --db state.db --out reports/state.json
"""
from __future__ import annotations

import argparse
import csv
import functools
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    parent_directory TEXT,
    filename TEXT,
    size_bytes INTEGER,
    size_gb REAL,
    mtime REAL,
    duration_seconds REAL,
    container_format TEXT,
    video_codec TEXT,
    resolution TEXT,
    width INTEGER,
    height INTEGER,
    bit_depth INTEGER,
    pixel_format TEXT,
    audio_stream_count INTEGER,
    audio_languages TEXT,
    subtitle_stream_count INTEGER,
    subtitle_codecs TEXT,
    subtitle_languages TEXT,
    video_bitrate_mbps REAL,
    audio_codecs TEXT,
    candidate TEXT,
    candidate_reason TEXT,
    estimated_savings_gb REAL,
    estimated_new_size_gb REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    output_path TEXT,
    actual_savings_gb REAL,
    error TEXT,
    encoder TEXT,
    quality INTEGER,
    preset TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    output_path TEXT,
    elapsed_seconds REAL,
    FOREIGN KEY (path) REFERENCES files(path)
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_candidate ON files(candidate);
"""

CONVERTED = ("converted", "converted_no_subs")
RETRYABLE = ("pending", "queued", "failed", "failed_verify", "interrupted", "skipped")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _locked(fn):  # type: ignore[no-untyped-def]
    """Serialise all DB access through StateDB._lock (see __init__)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


def _coerce_float(value: Any) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class StateDB:
    """Thin wrapper around the SQLite file; one connection per instance."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The converter runs workers in threads that all report status
        # through this one connection. sqlite3 defaults to
        # check_same_thread=True, which makes every worker-thread write
        # fail with "SQLite objects created in a thread can only be used
        # in that same thread" (caught live 2026-10-25). Disabling that
        # check is safe ONLY because every write below is serialised
        # through self._lock; never use this connection without it.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path),
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        # Lightweight migration for DBs created before these columns
        # existed (bit_depth, then audio/subtitle makeup, 2026-10-25).
        existing_cols = {r[1] for r in
                         self.conn.execute("PRAGMA table_info(files)")}
        for col, decl in (("bit_depth", "INTEGER"),
                          ("pixel_format", "TEXT"),
                          ("audio_stream_count", "INTEGER"),
                          ("audio_languages", "TEXT"),
                          ("subtitle_stream_count", "INTEGER"),
                          ("subtitle_codecs", "TEXT"),
                          ("subtitle_languages", "TEXT"),
                          ("source_kind", "TEXT"),
                          ("source_files", "TEXT"),
                          ("video_ts_dir", "TEXT"),
                          ("vts_number", "TEXT"),
                          ("sidecar_files", "TEXT"),
                          ("field_order", "TEXT")):
            if col not in existing_cols:
                self.conn.execute(
                    f"ALTER TABLE files ADD COLUMN {col} {decl}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- import -----------------------------------------------------------
    @_locked
    def import_report(self, rows: list[dict]) -> dict[str, int]:
        """Upsert scan rows. Returns counts: inserted / updated / kept_converted.

        ``kept_converted`` counts rows where the DB already said converted
        and the fingerprint still matches, so the import deliberately did
        NOT reset them to pending.
        """
        inserted = updated = kept = 0
        now = _now()
        for r in rows:
            path = str(r.get("Path") or "")
            if not path:
                continue
            p = Path(path)
            size_bytes: int | None = None
            mtime: float | None = None
            try:
                st = p.stat()
                size_bytes, mtime = st.st_size, st.st_mtime
            except OSError:
                pass  # file moved since scan; keep report size if present
            if size_bytes is None:
                gb = _coerce_float(r.get("Size_GB"))
                size_bytes = int(gb * 1024 ** 3) if gb else None
            existing = self.conn.execute(
                "SELECT status, size_bytes, mtime FROM files WHERE path = ?",
                (path,)).fetchone()
            if existing and existing["status"] in CONVERTED:
                same = (existing["size_bytes"] == size_bytes
                        and (existing["mtime"] is None or mtime is None
                             or abs(existing["mtime"] - mtime) < 1.0))
                if same:
                    kept += 1
                    continue  # fingerprint matches: keep the converted row
                # File changed since conversion -> fall through and reset.
            # DVD jobs carry their real inputs in Source_Files (the Path
            # is a synthetic title-set key, not a file). Store them as
            # JSON so a claimed job can be encoded without re-scanning.
            raw_files = r.get("Source_Files") or r.get("source_files")
            if isinstance(raw_files, list):
                source_files_json = json.dumps([str(f) for f in raw_files])
            elif isinstance(raw_files, str) and raw_files:
                source_files_json = raw_files
            else:
                source_files_json = None
            # Camcorder jobs carry their metadata sidecars the same
            # way DVD jobs carry Source_Files: a JSON list, so a
            # claimed job can find them without re-scanning.
            raw_sidecars = r.get("Sidecar_Files") or r.get("sidecar_files")
            if isinstance(raw_sidecars, list):
                sidecar_files_json = json.dumps([str(f) for f in raw_sidecars])
            elif isinstance(raw_sidecars, str) and raw_sidecars:
                sidecar_files_json = raw_sidecars
            else:
                sidecar_files_json = None
            cols = dict(
                path=path,
                parent_directory=r.get("Parent_Directory"),
                filename=r.get("Filename"),
                size_bytes=size_bytes,
                size_gb=_coerce_float(r.get("Size_GB")),
                mtime=mtime,
                duration_seconds=_coerce_float(r.get("Duration_Seconds")),
                container_format=r.get("Container_Format"),
                video_codec=r.get("Video_Codec"),
                resolution=r.get("Resolution"),
                width=_coerce_int(r.get("Width")),
                height=_coerce_int(r.get("Height")),
                bit_depth=_coerce_int(r.get("Bit_Depth")),
                pixel_format=r.get("Pixel_Format"),
                video_bitrate_mbps=_coerce_float(r.get("Video_Bitrate_Mbps")),
                audio_codecs=r.get("Audio_Codecs"),
                audio_stream_count=_coerce_int(r.get("Audio_Stream_Count")),
                audio_languages=r.get("Audio_Languages"),
                subtitle_stream_count=_coerce_int(r.get("Subtitle_Stream_Count")),
                subtitle_codecs=r.get("Subtitle_Codecs"),
                subtitle_languages=r.get("Subtitle_Languages"),
                candidate=r.get("Optimization_Candidate"),
                candidate_reason=r.get("Candidate_Reason"),
                estimated_savings_gb=_coerce_float(r.get("Estimated_Savings_GB")),
                estimated_new_size_gb=_coerce_float(r.get("Estimated_New_Size_GB")),
                source_kind=r.get("Source_Kind") or r.get("source_kind"),
                source_files=source_files_json,
                video_ts_dir=r.get("Video_TS_Dir") or r.get("video_ts_dir"),
                vts_number=r.get("VTS_Number") or r.get("vts_number"),
                sidecar_files=sidecar_files_json,
                field_order=r.get("Field_Order") or r.get("field_order"),
                updated_at=now,
            )
            if existing is None:
                cols["status"] = "pending"
                cols["created_at"] = now
                self.conn.execute(
                    f"INSERT INTO files ({','.join(cols)}) "  # noqa: S608
                    f"VALUES ({','.join('?' * len(cols))})",
                    tuple(cols.values()))
                inserted += 1
            else:
                # Re-scan of a non-converted file: refresh metadata, and
                # reset retryable/failed rows to pending so they are
                # eligible again. A row mid-flight (converting) is left
                # alone -- an import must never steal a running job.
                if existing["status"] != "converting":
                    cols["status"] = "pending"
                set_clause = ",".join(f"{k}=?" for k in cols if k != "path")
                self.conn.execute(
                    f"UPDATE files SET {set_clause} WHERE path = ?",  # noqa: S608
                    tuple(v for k, v in cols.items() if k != "path") + (path,))
                updated += 1
        self.conn.commit()
        return {"inserted": inserted, "updated": updated, "kept_converted": kept}

    # -- selection / claiming ---------------------------------------------
    @_locked
    def pending_jobs(self, priority: str | None = None,
                     min_savings_gb: float = 0.0,
                     limit: int = 0,
                     include_modern: bool = False) -> list[dict]:
        """List retryable jobs, biggest estimated saving first.

        This is a read: it does not claim anything. The converter calls
        claim_next() (or claim_batch()) to take ownership.
        """
        sql = ("SELECT * FROM files WHERE status IN "
               "('pending','failed','failed_verify','interrupted','skipped')")
        params: list[Any] = []
        if priority:
            sql += " AND candidate = ?"
            params.append(priority)
        if min_savings_gb:
            sql += " AND COALESCE(estimated_savings_gb, 0) >= ?"
            params.append(min_savings_gb)
        if not include_modern:
            sql += " AND COALESCE(video_codec, '') NOT IN ('HEVC','AV1','VP9')"
        sql += " ORDER BY COALESCE(estimated_savings_gb, 0) DESC, size_gb DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    @_locked
    def claim_next(self, priority: str | None = None,
                   min_savings_gb: float = 0.0,
                   include_modern: bool = False) -> dict | None:
        """Atomically claim the single best pending job.

        BEGIN IMMEDIATE serialises claimers: two processes racing here
        cannot both get the same row, because the UPDATE only matches
        rows still in a retryable status.
        """
        with self.conn:  # transaction: BEGIN IMMEDIATE on first write
            row = self.conn.execute(
                "SELECT * FROM files WHERE status IN "
                "('pending','failed','failed_verify','interrupted') "
                + ("AND candidate = ? " if priority else "")
                + ("AND COALESCE(estimated_savings_gb, 0) >= ? " if min_savings_gb else "")
                + ("AND COALESCE(video_codec, '') NOT IN ('HEVC','AV1','VP9') "
                   if not include_modern else "")
                + "ORDER BY COALESCE(estimated_savings_gb, 0) DESC LIMIT 1",
                tuple(v for v in (priority, min_savings_gb)
                      if v is not None and v != 0.0 and v is not False),
            ).fetchone()
            if row is None:
                return None
            cur = self.conn.execute(
                "UPDATE files SET status='converting', attempts=attempts+1,"
                " updated_at=? WHERE path=? AND status IN"
                " ('pending','failed','failed_verify','interrupted')",
                (_now(), row["path"]))
            if cur.rowcount != 1:
                return None  # lost the race; caller tries the next job
            return dict(row)

    @_locked
    def set_status(self, path: str, status: str, detail: str = "",
                   output_path: str | None = None,
                   actual_savings_gb: float | None = None,
                   encoder: str | None = None,
                   quality: int | None = None, preset: str | None = None,
                   elapsed: float | None = None) -> None:
        """Record a terminal (or progress) status + one attempts row."""
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE files SET status=?, error=?, output_path="
                "COALESCE(?, output_path),"
                " actual_savings_gb=COALESCE(?, actual_savings_gb),"
                " encoder=COALESCE(?, encoder), quality=COALESCE(?, quality),"
                " preset=COALESCE(?, preset), updated_at=? WHERE path=?",
                (status, detail or None, output_path, actual_savings_gb,
                 encoder, quality, preset, now, path))
            self.conn.execute(
                "INSERT INTO attempts (path, started_at, finished_at, status,"
                " detail, output_path, elapsed_seconds) VALUES (?,?,?,?,?,?,?)",
                (path, now, now, status, detail or None, output_path, elapsed))

    @_locked
    def mark_interrupted(self, path: str) -> None:
        """Called on shutdown / signal: a converting job is NOT done."""
        with self.conn:
            self.conn.execute(
                "UPDATE files SET status='interrupted',"
                " error='interrupted (process stopped mid-encode)',"
                " updated_at=? WHERE path=? AND status='converting'",
                (_now(), path))

    @_locked
    def mark_all_converting_interrupted(self) -> int:
        """Startup recovery: jobs left 'converting' by a killed run."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE files SET status='interrupted',"
                " error='previous run was killed while converting',"
                " updated_at=? WHERE status='converting'", (_now(),))
            return cur.rowcount

    # -- reporting ----------------------------------------------------------
    @_locked
    def summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n, COALESCE(SUM(size_gb),0) gb,"
            " COALESCE(SUM(actual_savings_gb),0) saved FROM files"
            " GROUP BY status").fetchall()
        by_status = {r["status"]: {"files": r["n"], "size_gb": round(r["gb"], 2),
                                   "saved_gb": round(r["saved"], 2)}
                     for r in rows}
        total = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size_gb),0) gb,"
            " COALESCE(SUM(actual_savings_gb),0) saved FROM files").fetchone()
        return {"by_status": by_status,
                "total_files": total["n"],
                "total_gb": round(total["gb"], 2),
                "total_saved_gb": round(total["saved"], 2)}

    @_locked
    def export_json(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM files ORDER BY path").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                source_files = json.loads(d.get("source_files") or "null")
            except json.JSONDecodeError:
                source_files = None
            try:
                sidecar_files = json.loads(d.get("sidecar_files") or "null")
            except json.JSONDecodeError:
                sidecar_files = None
            field_order = d.get("field_order")
            interlaced = (True if field_order in ("interlaced", "tt", "bb", "tb", "bt")
                          else False if field_order == "progressive"
                          else None)
            out.append({
                "Path": d["path"], "Parent_Directory": d["parent_directory"],
                "Filename": d["filename"], "Size_GB": d["size_gb"],
                "Duration_Seconds": d["duration_seconds"],
                "Container_Format": d["container_format"],
                "Video_Codec": d["video_codec"], "Resolution": d["resolution"],
                "Width": d["width"], "Height": d["height"],
                "Video_Bitrate_Mbps": d["video_bitrate_mbps"],
                "Audio_Codecs": d["audio_codecs"],
                "Audio_Stream_Count": d.get("audio_stream_count"),
                "Audio_Languages": d.get("audio_languages"),
                "Subtitle_Stream_Count": d.get("subtitle_stream_count"),
                "Subtitle_Codecs": d.get("subtitle_codecs"),
                "Subtitle_Languages": d.get("subtitle_languages"),
                "Optimization_Candidate": d["candidate"],
                "Candidate_Reason": d["candidate_reason"],
                "Estimated_Savings_GB": d["estimated_savings_gb"],
                "Estimated_New_Size_GB": d["estimated_new_size_gb"],
                "Source_Kind": d.get("source_kind"),
                "Source_Files": source_files,
                "Video_TS_Dir": d.get("video_ts_dir"),
                "VTS_Number": d.get("vts_number"),
                "Sidecar_Files": sidecar_files,
                "Field_Order": field_order,
                "Interlaced": interlaced,
                "Conversion_Status": d["status"],
                "Output_Path": d["output_path"] or "",
                "Actual_Savings_GB": d["actual_savings_gb"],
                "Error": d["error"] or "",
            })
        return out


def load_report_file(path: Path) -> list[dict]:
    """Load a scanner report, with a human error when the path is wrong.

    A bare relative name like 'video_optimization_report.json' only
    works when your current directory actually contains that file --
    it is NOT searched for next to the script, in a reports/ folder,
    or on your Desktop. That was the 2026-10-25 traceback: the file
    simply was not in the folder the command was run from. Raising
    here with the resolved path + cwd (instead of a bare Errno 2)
    makes that obvious instead of looking like a code bug.
    """
    if not path.is_file():
        tried = path if path.is_absolute() else Path.cwd() / path
        hint = ""
        alt = Path.cwd() / "reports" / path.name
        if alt.is_file():
            hint = (f"\n  Did you mean: {alt} ?\n"
                    f"  (run with --report \"{alt}\" instead)")
        elif path.name == "video_optimization_report.json":
            hint = ("\n  The scanner writes this to your --out-dir, e.g. "
                    "reports/video_optimization_report.json -- pass that "
                    "full path, or cd to the folder that contains it first.")
        raise FileNotFoundError(
            f"Report not found: {tried} (current directory: {Path.cwd()}){hint}")
    if path.suffix.lower() == ".json":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    numeric = {"Size_GB", "Duration_Seconds", "Video_Bitrate_Mbps",
               "Estimated_Savings_GB", "Estimated_New_Size_GB"}
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            for col in numeric:
                if r.get(col) not in (None, ""):
                    try:
                        r[col] = float(r[col])  # type: ignore[arg-type]
                    except ValueError:
                        pass
            rows.append(r)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SQLite state for the HEVC pipeline.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="Import a scanner JSON/CSV report into the DB")
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--db", type=Path, default=Path("state.db"))
    p = sub.add_parser("list", help="List jobs by status")
    p.add_argument("--db", type=Path, default=Path("state.db"))
    p.add_argument("--status", default=None)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("summary", help="Counts by status")
    p.add_argument("--db", type=Path, default=Path("state.db"))
    p = sub.add_parser("export", help="Write the DB back out as scanner-style JSON")
    p.add_argument("--db", type=Path, default=Path("state.db"))
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("recover", help="Mark jobs left 'converting' as interrupted")
    p.add_argument("--db", type=Path, default=Path("state.db"))
    args = ap.parse_args(argv)

    # Check the report BEFORE creating state.db, so a mistyped path
    # does not leave an empty DB behind and then a traceback.
    if args.cmd == "import":
        try:
            rows = load_report_file(args.report)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    db = StateDB(args.db)
    try:
        if args.cmd == "import":
            print(db.import_report(rows))
        elif args.cmd == "list":
            jobs = db.pending_jobs() if args.status in (None, "pending") else [
                dict(r) for r in db.conn.execute(
                    "SELECT * FROM files WHERE status=? ORDER BY"
                    " COALESCE(estimated_savings_gb,0) DESC LIMIT ?",
                    (args.status, args.limit))]
            for j in jobs[: args.limit]:
                print(f"{j['status']:<18} {j.get('size_gb') or 0:>7.2f} GB  "
                      f"{j.get('video_codec') or '?':<6} {j['path']}")
        elif args.cmd == "summary":
            print(json.dumps(db.summary(), indent=2))
        elif args.cmd == "export":
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(db.export_json(), fh, indent=2, ensure_ascii=False)
            print(f"Wrote {args.out}")
        elif args.cmd == "recover":
            print(f"Recovered {db.mark_all_converting_interrupted()} interrupted job(s)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
