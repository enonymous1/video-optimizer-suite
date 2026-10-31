#!/usr/bin/env python3
"""End-to-end smoke test (no pytest needed): synthetic H.264 -> HEVC.

    python tests/test_smoke.py

Creates a 5s H.264 + AAC test file with ffmpeg lavfi, then runs the
real pipeline code paths (not reimplementations of them):
  1. quality_check.verify() on a good encode -> pass
  2. quality_check.verify() on an empty file -> fail
  3. hevc_convert.convert_one() in --replace mode, CPU x265,
     preset ultrafast -> original path is now HEVC, backup kept,
     mtime preserved, DB row marked converted
  4. state_manager claim/recover: a 'converting' row left behind is
     recovered to 'interrupted' on next open

Requires ffmpeg/ffprobe on PATH. CPU x265 only -- NVENC/QSV are
preflight-checked, not encode-tested, because this box has no GPU.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quality_check import verify  # noqa: E402
from state_manager import StateDB  # noqa: E402
import hevc_convert  # noqa: E402


def _make_source(path: Path) -> None:
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(cmd, capture_output=True, check=True)


def _convert_args(**overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        db=None, report=None, scan_root=None, out_dir=Path("converted"),
        staging_dir=None, limit=0, sort_by="savings", order="desc",
        min_savings_gb=0.0, priority="high", include_modern=False,
        encoder="x265", crf=28, preset="ultrafast", jobs=1,
        overwrite=True, dry_run=False, confirm_over_gb=0.0, yes=True,
        skip_done=True, update_report=True, replace=False,
        backup_dir=None, purge_backup=False, sample_quality=False)
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "sample.mkv"
        _make_source(src)
        orig_mtime = src.stat().st_mtime
        orig_mode = src.stat().st_mode

        # 1. Verifier: good encode passes, empty file fails.
        good = root / "good.mkv"
        subprocess.run(["ffmpeg", "-y", "-i", str(src), "-map", "0",
                        "-c:v", "libx265", "-preset", "ultrafast", "-crf", "28",
                        "-c:a", "copy", str(good)],
                       capture_output=True, check=True)
        res = verify(src, good)
        check("verifier passes good HEVC encode", res.passed, "; ".join(res.failures))
        empty = root / "empty.mkv"
        empty.touch()
        res2 = verify(src, empty)
        check("verifier fails empty file", not res2.passed)

        # 2. Replace-mode convert through the real convert_one().
        db = StateDB(root / "state.db")
        db.import_report([{
            "Path": str(src), "Parent_Directory": str(root),
            "Filename": src.name, "Size_GB": src.stat().st_size / 1024 ** 3,
            "Duration_Seconds": 5.0, "Container_Format": "MATROSKA",
            "Video_Codec": "H.264", "Resolution": "320x240",
            "Width": 320, "Height": 240, "Video_Bitrate_Mbps": 1.0,
            "Audio_Codecs": "AAC (2ch)", "Optimization_Candidate": "High Priority",
            "Candidate_Reason": "test", "Estimated_Savings_GB": 0.001,
            "Bit_Depth": 8, "Pixel_Format": "yuv420p",
        }])
        row = db.claim_next(priority="High Priority")
        check("DB claim returns the job", row is not None)
        if row is not None:
            row["Path"] = row["path"]
            args = _convert_args(replace=True)
            entry = hevc_convert.convert_one(row, args, db)
            check("replace convert status converted",
                  entry["Status"] == "converted", entry.get("Status_Detail") or "")
            check("original path is now HEVC",
                  "hevc" in json.dumps(__import__("subprocess").run(
                      ["ffprobe", "-v", "quiet", "-print_format", "json",
                       "-show_streams", str(src)], capture_output=True,
                      text=True).stdout).lower())
            backup = root / "sample.mkv.bak"
            check("backup kept alongside original", backup.is_file())
            check("mtime preserved",
                  abs(src.stat().st_mtime - orig_mtime) < 1.0,
                  f"{src.stat().st_mtime} vs {orig_mtime}")
            check("permissions preserved",
                  (src.stat().st_mode & 0o777) == (orig_mode & 0o777))
            check("no staging file left behind",
                  not list(root.glob(".*hevc-part*")))
            db_row = db.conn.execute(
                "SELECT status FROM files WHERE path = ?", (str(src),)).fetchone()
            check("DB row marked converted",
                  db_row is not None and db_row["status"] == "converted",
                  str(dict(db_row)) if db_row else "no row")

        # 3. Recovery: a row stuck in 'converting' is recovered.
        db.conn.execute(
            "UPDATE files SET status='converting' WHERE path = ?", (str(src),))
        db.conn.commit()
        n = db.mark_all_converting_interrupted()
        check("stuck converting row recovered to interrupted", n == 1)

        # 4. Import preserves user decisions; claim never takes a skip.
        # (Regression for the old behaviour where any re-import reset
        # skipped/verified rows to pending.)
        db2_path = root / "state2.db"
        db2 = StateDB(db2_path)
        row_a = {"Path": str(root / "keep_pending.mkv"), "Filename": "keep_pending.mkv",
                 "Size_GB": 0.001, "Duration_Seconds": 5.0,
                 "Video_Codec": "H.264", "Resolution": "320x240",
                 "Optimization_Candidate": "High Priority",
                 "Estimated_Savings_GB": 0.001}
        row_b = dict(row_a, Path=str(root / "user_skipped.mkv"),
                     Filename="user_skipped.mkv")
        row_c = dict(row_a, Path=str(root / "user_verified.mkv"),
                     Filename="user_verified.mkv")
        db2.import_report([row_a, row_b, row_c])
        db2.set_status(row_b["Path"], "skipped", "user skipped")
        db2.set_status(row_c["Path"], "verified", "user verified")
        db2.import_report([row_a, row_b, row_c])  # re-import must not reset them
        st = {r["path"]: r["status"] for r in db2.conn.execute(
            "SELECT path, status FROM files")}
        check("re-import preserves skipped", st[row_b["Path"]] == "skipped", str(st))
        check("re-import preserves verified", st[row_c["Path"]] == "verified", str(st))
        claimed = db2.claim_next(priority="High Priority")
        check("claim_next never claims a skipped row",
              claimed is not None and claimed["path"] == row_a["Path"],
              str(claimed))
        db2.close()
        db.close()

    print(f"\n{len(failures)} failure(s)" if failures else "\nAll smoke checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
