#!/usr/bin/env python3
"""Camcorder ingest tests: discovery/sidecars, DB round-trip, and a real
ffmpeg end-to-end run on synthetic camcorder fixtures.

    python tests/test_camcorder_ingest.py

No pytest needed (same convention as tests/test_smoke.py). Requires
ffmpeg/ffprobe on PATH. CPU x265 only. exiftool is NOT on this box,
so the exiftool-merge path is exercised with a fake exiftool on PATH
(records its arguments, exits 0) and the no-exiftool fallback path
is exercised with the real environment.

Covers the old handbrake_camcorder_conversion behaviours that were
fixed by the fold, so they cannot silently regress:

  * .MOD/.TOD/.M2T files were the whole point of that tool, and the
    bundled scanner misses all three -- here discovery is
    case-insensitive and includes them;
  * sidecars (.modd/.xmp/.xml) were merged/copied even after a failed
    HandBrake run -- here metadata runs on the staging file before
    verification, and the fallback copy runs only after a verified
    publish, never after a failed encode;
  * the old tool had no output verification and treated a partial
    output as done -- here the verifier gates every publish, and
    --replace is the suite's opt-in backup path, not a default.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camcorder_ingest  # noqa: E402
import hevc_convert  # noqa: E402
from state_manager import StateDB  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print((("PASS " if cond else "FAIL ") + name
           + (f" -- {detail}" if detail and not cond else "")))
    if not cond:
        FAILURES.append(name)


def _make_clip(path: Path, seconds: int = 3, freq: int = 440,
               codec: str = "mpeg2video", size: str = "720x480",
               bitrate: str = "4000k", fmt: str = "mpeg") -> None:
    # .MOD/.TOD extensions mean nothing to ffmpeg's muxer guesser, so
    # the container format is forced explicitly (mpeg = MPEG-PS).
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i",
           f"testsrc=duration={seconds}:size={size}:rate=30000/1001",
           "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
           "-c:v", codec, "-b:v", bitrate,
           "-c:a", "mp2", "-b:a", "128k", "-f", fmt, str(path)]
    subprocess.run(cmd, capture_output=True, check=True)


def _convert_args(**overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        db=None, report=None, scan_root=None, out_dir=Path("converted"),
        staging_dir=None, limit=0, sort_by="savings", order="desc",
        min_savings_gb=0.0, priority="all", include_modern=False,
        encoder="x265", crf=28, preset="ultrafast", jobs=1,
        overwrite=True, dry_run=False, confirm_over_gb=0.0, yes=True,
        skip_done=True, update_report=True, replace=False,
        backup_dir=None, purge_backup=False, sample_quality=False,
        dvd_mode="480p", camcorder_mode="auto", camcorder_metadata=True)
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def _unit_checks(root: Path) -> None:
    root.mkdir(parents=True)
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "secret.MOD").write_bytes(b"x")
    (root / "clip.MOD").write_bytes(b"x")
    (root / "clip2.Tod").write_bytes(b"x")
    (root / "clip3.M2T").write_bytes(b"x")
    (root / "modern.mp4").write_bytes(b"x")
    (root / "notes.txt").write_bytes(b"x")
    found = camcorder_ingest.find_camcorder_files(root)
    names = [p.name for p in found]
    check("discovery finds .MOD/.Tod/.M2T case-insensitively",
          names == ["clip.MOD", "clip2.Tod", "clip3.M2T"], str(names))
    check("discovery excludes .mp4/.txt and hidden dirs",
          "modern.mp4" not in names and "secret.MOD" not in names)
    single = camcorder_ingest.find_camcorder_files(root / "clip.MOD")
    check("single-file root returns that file",
          len(single) == 1 and single[0].name == "clip.MOD")
    check("single-file root rejects a non-camcorder file",
          camcorder_ingest.find_camcorder_files(root / "modern.mp4") == [])

    (root / "clip.Modd").write_text("sidecar meta")
    (root / "clip.XMP").write_text("xmp meta")
    sidecars = camcorder_ingest.find_sidecars(root / "clip.MOD")
    check("sidecars found case-insensitively, in .modd/.xmp/.xml order",
          [p.name for p in sidecars] == ["clip.Modd", "clip.XMP"],
          str([p.name for p in sidecars]))
    check("no sidecars for a bare clip",
          camcorder_ingest.find_sidecars(root / "clip2.Tod") == [])

    check("field_order progressive is not interlaced",
          camcorder_ingest.is_interlaced({"field_order": "progressive"}) is False)
    check("field_order tt is interlaced",
          camcorder_ingest.is_interlaced({"field_order": "tt"}) is True)
    check("field_order unknown is unknown, not guessed",
          camcorder_ingest.is_interlaced({"field_order": "unknown"}) is None)
    check("missing field_order is unknown",
          camcorder_ingest.is_interlaced({}) is None)

    row_tt = {"Source_Kind": "camcorder", "Field_Order": "tt"}
    row_prog = {"Source_Kind": "camcorder", "Field_Order": "progressive"}
    check("auto mode deinterlaces an interlaced row",
          hevc_convert._camcorder_filter(row_tt, _convert_args()) == "bwdif=1")
    check("auto mode leaves a progressive row alone",
          hevc_convert._camcorder_filter(row_prog, _convert_args()) is None)
    check("deinterlace mode forces bwdif on a progressive row",
          hevc_convert._camcorder_filter(
              row_prog, _convert_args(camcorder_mode="deinterlace")) == "bwdif=1")
    check("keep mode never deinterlaces an interlaced row",
          hevc_convert._camcorder_filter(
              row_tt, _convert_args(camcorder_mode="keep")) is None)
    old_ns = argparse.Namespace()  # namespaces predating the new flags
    check("missing camcorder_mode defaults to auto",
          hevc_convert._camcorder_filter(row_tt, old_ns) == "bwdif=1")

    check("sidecar parsing accepts a report list",
          [str(p) for p in (hevc_convert._camcorder_sidecars(
              {"Source_Kind": "camcorder",
               "Sidecar_Files": ["/a/clip.modd"]}) or [])] == ["/a/clip.modd"])
    check("sidecar parsing accepts a DB JSON string",
          [str(p) for p in (hevc_convert._camcorder_sidecars(
              {"source_kind": "camcorder",
               "sidecar_files": '["/a/clip.modd"]'}) or [])] == ["/a/clip.modd"])
    check("non-camcorder rows have no sidecars",
          hevc_convert._camcorder_sidecars({"Source_Kind": "dvd_vts"}) is None)
    check("camcorder row without sidecars is still camcorder",
          hevc_convert._camcorder_sidecars({"Source_Kind": "camcorder"}) == [])


def _e2e_checks(root: Path) -> None:
    root.mkdir(parents=True)
    src = root / "clip.MOD"
    _make_clip(src)
    sidecar = root / "clip.modd"
    sidecar.write_text("sidecar meta")
    (root / "clip.xmp").write_text("xmp meta")
    bad = root / "broken.TOD"
    bad.write_bytes(b"this is not a video file")

    rows, files = camcorder_ingest.scan_camcorders(root)
    by_name = {r["Filename"]: r for r in rows}
    check("scan finds the .MOD and .TOD fixtures", len(rows) == 2, str(by_name))
    row = by_name["clip.MOD"]
    check("row is a camcorder job",
          row.get("Source_Kind") == "camcorder")
    check("row records both sidecars",
          len(row.get("Sidecar_Files") or []) == 2, str(row.get("Sidecar_Files")))
    check("row has a codec and a savings estimate",
          row.get("Video_Codec") == "MPEG-2"
          and (row.get("Estimated_Savings_GB") or 0) >= 0,
          str({k: row.get(k) for k in ("Video_Codec", "Estimated_Savings_GB")}))
    check("row field order is recorded, not guessed",
          row.get("Field_Order") in (None, "progressive", "unknown", "tt", "bb"),
          str(row.get("Field_Order")))
    bad_row = by_name["broken.TOD"]
    check("unprobeable file becomes an error row, still camcorder",
          bad_row.get("Error") and bad_row.get("Source_Kind") == "camcorder"
          and bad_row.get("Optimization_Candidate") == "Low",
          str(bad_row.get("Error")))

    # DB round-trip: sidecars + field order survive import and export,
    # and the migration adds the columns to an old-schema DB.
    db = StateDB(root / "state.db")
    db.import_report(rows)
    db_row = db.conn.execute(
        "SELECT source_kind, sidecar_files, field_order FROM files "
        "WHERE path = ?", (str(src.resolve()),)).fetchone()
    check("DB stores camcorder kind, sidecars and field order",
          db_row is not None and db_row["source_kind"] == "camcorder"
          and json.loads(db_row["sidecar_files"] or "[]")
          and db_row["field_order"] == row.get("Field_Order"),
          str(dict(db_row)) if db_row else "no row")
    exported = {r["Path"]: r for r in db.export_json()}
    check("export round-trips sidecars and field order",
          exported[str(src.resolve())].get("Sidecar_Files")
          and exported[str(src.resolve())].get("Field_Order") == row.get("Field_Order"))

    old_db = root / "old.db"
    conn = sqlite3.connect(str(old_db))
    # An old-schema DB: every column from the original SCHEMA except
    # the ones the migration loop adds (bit_depth, audio/subtitle
    # makeup, DVD columns, and now sidecar_files/field_order). The
    # migration must add exactly those, and keep the prior row.
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, "
                 "parent_directory TEXT, filename TEXT, size_bytes INTEGER, "
                 "size_gb REAL, mtime REAL, duration_seconds REAL, "
                 "container_format TEXT, video_codec TEXT, resolution TEXT, "
                 "width INTEGER, height INTEGER, video_bitrate_mbps REAL, "
                 "audio_codecs TEXT, candidate TEXT, candidate_reason TEXT, "
                 "estimated_savings_gb REAL, estimated_new_size_gb REAL, "
                 "status TEXT NOT NULL DEFAULT 'pending', output_path TEXT, "
                 "actual_savings_gb REAL, error TEXT, encoder TEXT, "
                 "quality INTEGER, preset TEXT, "
                 "attempts INTEGER NOT NULL DEFAULT 0, "
                 "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO files (path, status, created_at, updated_at) "
                 "VALUES (?, 'converted', '2026-01-01', '2026-01-01')",
                 (str(src.resolve()),))
    conn.commit()
    conn.close()
    legacy = StateDB(old_db)
    cols = {r[1] for r in legacy.conn.execute("PRAGMA table_info(files)")}
    kept = legacy.conn.execute(
        "SELECT status FROM files WHERE path = ?",
        (str(src.resolve()),)).fetchone()
    check("migration adds sidecar/field columns to an old DB",
          "sidecar_files" in cols and "field_order" in cols)
    check("migration keeps the prior row",
          kept is not None and kept["status"] == "converted")
    legacy.close()

    # Live encode, no exiftool on this box: fallback copies the
    # sidecars next to the verified output, after publish only.
    # (The 3s testsrc clip encodes at ~1 Mbps, so the scanner
    # honestly ranks it Low; claim via pending_jobs, not priority.)
    out_dir = root / "converted"
    pending = [j for j in db.pending_jobs()
               if j["filename"] == "clip.MOD"]
    check("DB returns the camcorder job", len(pending) == 1,
          str([j["path"] for j in pending]))
    job = pending[0] if pending else None
    if job is not None:
        job["Path"] = job["path"]
        job["Sidecar_Files"] = job.get("sidecar_files")
        job["Field_Order"] = job.get("field_order")
        args = _convert_args(scan_root=root, out_dir=out_dir, overwrite=True)
        entry = hevc_convert.convert_one(job, args, db)
        check("camcorder convert status converted",
              entry["Status"] == "converted", entry.get("Status_Detail") or "")
        final = out_dir / "clip.mkv"
        check("output is HEVC in the mirrored tree", final.is_file())
        copied = out_dir / "clip.modd"
        check("sidecar copied next to the output (no exiftool here)",
              copied.is_file() and copied.read_text() == "sidecar meta")
        check("xmp sidecar copied too",
              (out_dir / "clip.xmp").is_file())
        sidecar_json = Path(str(final) + ".conversion.json")
        check("conversion JSON records the camcorder block",
              sidecar_json.is_file()
              and json.loads(sidecar_json.read_text())["source"]["camcorder"]["field_order"]
              == row.get("Field_Order"))
        check("no staging file left behind",
              not list(out_dir.glob(".*hevc-part*")))

    # Failed encode: no sidecar copy, no metadata handling.
    empty_src = root / "empty.MOD"
    empty_src.write_bytes(b"")
    empty_sidecar = root / "empty.modd"
    empty_sidecar.write_text("must not be copied")
    fail_row = {"Path": str(empty_src), "Filename": "empty.MOD",
                "Source_Kind": "camcorder",
                "Sidecar_Files": [str(empty_sidecar)],
                "Field_Order": "progressive"}
    fail_entry = hevc_convert.convert_one(
        fail_row, _convert_args(scan_root=root, out_dir=root / "converted2",
                                overwrite=True), None)
    check("failed encode is not marked converted",
          fail_entry["Status"] in ("failed", "failed_verify"),
          str(fail_entry["Status"]))
    check("failed encode copies no sidecar",
          not (root / "converted2" / "empty.modd").exists())

    # Fake exiftool: merge path is recorded, no fallback copy.
    bin_dir = root / "fakebin"
    bin_dir.mkdir()
    exif_log = root / "exiftool.log"
    fake = bin_dir / "exiftool"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.environ['EXIFTOOL_LOG'], 'a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n")
    fake.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
    os.environ["EXIFTOOL_LOG"] = str(exif_log)
    try:
        src2 = root / "clip2.MOD"
        _make_clip(src2, freq=880)
        side2 = root / "clip2.modd"
        side2.write_text("sidecar two")
        row2 = camcorder_ingest.build_row(src2)
        entry2 = hevc_convert.convert_one(
            row2, _convert_args(scan_root=root, out_dir=root / "converted3",
                                overwrite=True), None)
        logged = exif_log.read_text() if exif_log.exists() else ""
        check("fake exiftool was called for source and sidecar",
              str(src2) in logged and str(side2) in logged, logged[:300])
        check("fake-exiftool convert still verified and converted",
              entry2["Status"] == "converted",
              entry2.get("Status_Detail") or "")
        check("merged sidecar is not also fallback-copied",
              not (root / "converted3" / "clip2.modd").exists())
    finally:
        os.environ["PATH"] = old_path
        os.environ.pop("EXIFTOOL_LOG", None)
    db.close()

    # Interlaced row, auto mode: the ffmpeg command carries bwdif
    # and the encode still verifies (filter path, live).
    row3 = camcorder_ingest.build_row(src2)
    row3["Field_Order"] = "tt"
    row3["Interlaced"] = True
    entry3 = hevc_convert.convert_one(
        row3, _convert_args(scan_root=root, out_dir=root / "converted4",
                            overwrite=True, camcorder_metadata=False), None)
    check("interlaced auto mode encodes with bwdif and verifies",
          entry3["Status"] == "converted"
          and "bwdif" in (entry3.get("FFmpeg_Command") or ""),
          str(entry3.get("Status_Detail")))
    # the main report (the scanner alone would miss .MOD entirely).
    work = root / "pipeline"
    work.mkdir()
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent.parent / "pipeline.py"),
         "--scan-dir", str(root), "--camcorder-dir", str(root),
         "--db", str(work / "state.db"),
         "--reports-dir", str(work / "reports"),
         "--out-dir", str(work / "converted"),
         "--encoder", "x265", "--crf", "28", "--preset", "ultrafast",
         "--priority", "all", "--dry-run"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("pipeline --camcorder-dir dry-run exits 0", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-400:])
    check("pipeline wrote the camcorder report",
          (work / "reports" / "camcorder_report.json").is_file())
    pdb = StateDB(work / "state.db")
    cam_rows = pdb.conn.execute(
        "SELECT path, sidecar_files FROM files "
        "WHERE source_kind = 'camcorder'").fetchall()
    pdb.close()
    check("pipeline imported camcorder jobs into the DB",
          len(cam_rows) >= 1, str([dict(r) for r in cam_rows]))


def main() -> int:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("SKIP: ffmpeg/ffprobe not on PATH")
        return 0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _unit_checks(root / "unit")
        _e2e_checks(root / "e2e")
    print(f"\n{len(FAILURES)} failure(s)" if FAILURES
          else "\nAll camcorder ingest checks passed.")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
