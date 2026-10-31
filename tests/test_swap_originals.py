#!/usr/bin/env python3
"""Swap-script tests: pairing, verification gate, backup, rollback/recovery.

    python tests/test_swap_originals.py

No pytest needed (same convention as tests/test_smoke.py). Requires
ffmpeg/ffprobe on PATH. CPU x265 only. Synthetic 5s files only, so
the full verify gate (including the decode pass) runs in seconds.

Covers the failure modes that would make a swap dangerous:

  * no --yes / --dry-run changes nothing;
  * a converted file whose size disagrees with the conversion log
    is not swapped (wrong file / changed since encode);
  * report and log disagreeing on Output_Path -> skipped, never guessed;
  * a stale <stem>.hevc file is NOT swapped when the report points
    at <stem>.hevc_2 (the CRF 18 / CRF 24 collision from 2026-10-26);
  * a larger encode is skipped unless --allow-larger;
  * a duration-mismatched encode fails the verify gate, original
    untouched, no backup created;
  * a real swap: original path is HEVC, backup holds the original
    H.264 bytes, mtime preserved, sidecar copied, report updated,
    swap log written, no staging file left;
  * a pre-existing .bak is never overwritten (numbered backup);
  * --recover restores an original left missing by an interrupted
    swap and removes the orphan staging file;
  * DVD rows are never swapped.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
_ROOT = Path(__file__).resolve().parent.parent
os.environ["PYTHONPATH"] = (str(_ROOT) + os.pathsep
                              + os.environ.get("PYTHONPATH", ""))


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          check=check)


def _make_h264(path: Path, seconds: int = 5, bitrate: str | None = None) -> None:
    cmd = ["ffmpeg", "-y", "-f", "lavfi",
           f"-i", f"testsrc=duration={seconds}:size=320x240:rate=30",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if bitrate:
        cmd += ["-b:v", bitrate]
    cmd += ["-c:a", "aac", "-shortest", str(path)]
    _run(cmd, path.parent)


def _make_hevc(src: Path, dst: Path, crf: int = 28) -> None:
    # CRF, not -b:v: in CRF mode ffmpeg ignores -b:v, so a "larger"
    # fixture built with -b:v would silently not be larger at all.
    _run(["ffmpeg", "-y", "-i", str(src), "-map", "0",
          "-c:v", "libx265", "-preset", "ultrafast", "-crf", str(crf),
          "-c:a", "copy", str(dst)], src.parent)


def _codec(path: Path) -> str:
    out = _run(["ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", str(path)], path.parent, check=False)
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    return str(streams[0].get("codec_name")) if streams else ""


def _write_report(root: Path, rows: list[dict], name: str = "video_optimization_report.json") -> Path:
    # One report file per fixture: _write_report overwrites, and
    # fixtures that share a filename would clobber each other's rows
    # (the --recover fixture re-reads its own report last).
    report = root / "reports" / name
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return report


def _row(original: Path, converted: Path | None, status: str = "converted") -> dict:
    return {"Path": str(original), "Filename": original.name,
            "Conversion_Status": status,
            "Output_Path": str(converted) if converted else "",
            "Size_GB": original.stat().st_size / 1024 ** 3 if original.is_file() else 0}


def _log_entry(original: Path, converted: Path) -> dict:
    return {"Input_Path": str(original), "Output_Path": str(converted),
            "Status": "converted", "New_Bytes": converted.stat().st_size,
            "Original_Bytes": original.stat().st_size}


def main() -> int:
    # Fixtures are built with ffmpeg/ffprobe; without them, skip
    # honestly instead of crashing with FileNotFoundError (the
    # failure mode seen on GitHub runners 2026-10-31).
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("SKIP: ffmpeg/ffprobe not on PATH")
        return 0
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    SWAP_CMD = [sys.executable, "-m", "video_optimizer.swap_originals"]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        media, converted = root / "media", root / "converted"
        media.mkdir()
        converted.mkdir()

        # --- Fixture A: one clean pair -----------------------------------
        src_a = media / "Alpha.mkv"
        _make_h264(src_a)
        orig_a_bytes = src_a.read_bytes()
        orig_a_mtime = src_a.stat().st_mtime
        conv_a = converted / "Alpha.hevc.mkv"
        _make_hevc(src_a, conv_a)
        (converted / "Alpha.hevc.mkv.conversion.json").write_text(
            json.dumps({"tool": "test"}), encoding="utf-8")
        report_a = _write_report(root, [_row(src_a, conv_a)])
        log_a = root / "conversion_log.json"
        log_a.write_text(json.dumps([_log_entry(src_a, conv_a)]), encoding="utf-8")

        # Dry-run and no---yes change nothing.
        r = _run(SWAP_CMD + ["--report", str(report_a),
                  "--dry-run"], root, check=False)
        check("dry-run exits 0 and changes nothing",
              r.returncode == 0 and _codec(src_a) == "h264"
              and not (media / "Alpha.mkv.bak").exists())
        r = _run(SWAP_CMD + ["--report", str(report_a)],
                 root, check=False)
        check("no --yes exits 2 and changes nothing",
              r.returncode == 2 and _codec(src_a) == "h264")

        # Real swap, one file.
        r = _run(SWAP_CMD + ["--report", str(report_a),
                  "--log", str(log_a), "--yes"], root, check=False)
        check("swap exits 0", r.returncode == 0, r.stdout + r.stderr)
        check("original path is now HEVC", _codec(src_a) == "hevc")
        backup_a = media / "Alpha.mkv.bak"
        check("backup holds the original H.264 bytes",
              backup_a.is_file() and backup_a.read_bytes() == orig_a_bytes)
        check("mtime preserved", abs(src_a.stat().st_mtime - orig_a_mtime) < 1.0)
        check("sidecar copied alongside original",
              (media / "Alpha.mkv.conversion.json").is_file())
        check("converted source kept by default", conv_a.is_file())
        check("no staging file left behind", not list(media.glob(".*swap-part*")))
        rows = json.loads(report_a.read_text(encoding="utf-8"))
        check("report records swap + backup",
              rows[0].get("Swap_Status") == "swapped"
              and rows[0].get("Backup_Path") == str(backup_a))
        swap_log = json.loads((root / "swap_log.json").read_text(encoding="utf-8"))
        check("swap log records one swapped row",
              len(swap_log) == 1 and swap_log[0]["Status"] == "swapped")

        # --- Fixture B: report/log conflict -> skipped ------------------
        src_b = media / "Bravo.mkv"
        _make_h264(src_b)
        conv_b1 = converted / "Bravo.hevc.mkv"
        conv_b2 = converted / "Bravo.hevc_2.mkv"
        _make_hevc(src_b, conv_b1)
        _make_hevc(src_b, conv_b2)
        report_b = _write_report(root, [_row(src_b, conv_b1)], "report_b.json")
        log_b = root / "conversion_log_b.json"
        log_b.write_text(json.dumps([_log_entry(src_b, conv_b2)]), encoding="utf-8")
        r = _run(SWAP_CMD + ["--report", str(report_b),
                  "--log", str(log_b), "--yes"], root, check=False)
        check("conflicting report/log is skipped, original untouched",
              _codec(src_b) == "h264" and not (media / "Bravo.mkv.bak").exists(),
              r.stdout)

        # --- Fixture C: stale stem.hevc vs report-pointed _2 ------------
        src_c = media / "Charlie.mkv"
        _make_h264(src_c, bitrate="200k")
        stale_c = converted / "Charlie.hevc.mkv"
        _make_hevc(src_c, stale_c, crf=5)  # larger, stale
        good_c = converted / "Charlie.hevc_2.mkv"
        _make_hevc(src_c, good_c)
        check("stale fixture is larger, good fixture is smaller",
              stale_c.stat().st_size > src_c.stat().st_size
              and good_c.stat().st_size < src_c.stat().st_size)
        report_c = _write_report(root, [_row(src_c, good_c)], "report_c.json")
        log_c = root / "conversion_log_c.json"
        log_c.write_text(json.dumps([_log_entry(src_c, good_c)]), encoding="utf-8")
        r = _run(SWAP_CMD + ["--report", str(report_c),
                  "--log", str(log_c), "--yes"], root, check=False)
        check("report-pointed _2 file swapped, stale file untouched",
              r.returncode == 0 and _codec(src_c) == "hevc"
              and stale_c.is_file() and _codec(stale_c) == "hevc"
              and (media / "Charlie.mkv.bak").is_file(), r.stdout + r.stderr)

        # --- Fixture D: larger encode skipped, then allowed -------------
        src_d = media / "Delta.mkv"
        _make_h264(src_d, bitrate="200k")
        conv_d = converted / "Delta.hevc.mkv"
        _make_hevc(src_d, conv_d, crf=5)
        check("larger fixture really is larger",
              conv_d.stat().st_size > src_d.stat().st_size)
        report_d = _write_report(root, [_row(src_d, conv_d)], "report_d.json")
        log_d = root / "conversion_log_d.json"
        log_d.write_text(json.dumps([_log_entry(src_d, conv_d)]), encoding="utf-8")
        r = _run(SWAP_CMD + ["--report", str(report_d),
                  "--log", str(log_d), "--yes"], root, check=False)
        check("larger encode skipped by default",
              _codec(src_d) == "h264" and not (media / "Delta.mkv.bak").exists())
        r = _run(SWAP_CMD + ["--report", str(report_d),
                  "--log", str(log_d), "--yes", "--allow-larger",
                  "--verify-mode", "fast"], root, check=False)
        check("larger encode swaps with --allow-larger (fast verify)",
              _codec(src_d) == "hevc", r.stdout + r.stderr)

        # --- Fixture E: duration mismatch fails the gate ----------------
        src_e = media / "Echo.mkv"
        _make_h264(src_e, seconds=5)
        conv_e = converted / "Echo.hevc.mkv"
        _make_h264(src_e, seconds=5)  # rebuild source deterministically
        _make_hevc(src_e, conv_e)
        # Truncate the encode to a 2s file: probe/duration must fail.
        short_e = converted / "Echo_short.mkv"
        _run(["ffmpeg", "-y", "-i", str(src_e), "-t", "2", "-map", "0",
              "-c:v", "libx265", "-preset", "ultrafast", "-crf", "28",
              "-c:a", "copy", str(short_e)], root, check=False)
        report_e = _write_report(root, [_row(src_e, short_e)], "report_e.json")
        # No log for Echo: size cross-check is report-only (None), so
        # the verify gate itself must reject the 2s file.
        r = _run(SWAP_CMD + ["--report", str(report_e),
                  "--yes"], root, check=False)
        check("duration-mismatched encode fails gate, no backup made",
              _codec(src_e) == "h264" and not (media / "Echo.mkv.bak").exists(),
              r.stdout + r.stderr)

        # --- Fixture F: backup collision + size-mismatch skip -----------
        src_f = media / "Foxtrot.mkv"
        _make_h264(src_f)
        sentinel = media / "Foxtrot.mkv.bak"
        sentinel.write_bytes(b"pre-existing backup - do not overwrite")
        conv_f = converted / "Foxtrot.hevc.mkv"
        _make_hevc(src_f, conv_f)
        report_f = _write_report(root, [_row(src_f, conv_f)], "report_f.json")
        log_f = root / "conversion_log_f.json"
        log_f.write_text(json.dumps([_log_entry(src_f, conv_f)]), encoding="utf-8")
        r = _run(SWAP_CMD + ["--report", str(report_f),
                  "--log", str(log_f), "--yes"], root, check=False)
        check("pre-existing .bak untouched, numbered backup used",
              sentinel.read_bytes() == b"pre-existing backup - do not overwrite"
              and (media / "Foxtrot.mkv.1.bak").is_file()
              and _codec(src_f) == "hevc", r.stdout + r.stderr)

        src_g = media / "Golf.mkv"
        _make_h264(src_g)
        conv_g = converted / "Golf.hevc.mkv"
        _make_hevc(src_g, conv_g)
        report_g = _write_report(root, [_row(src_g, conv_g)], "report_g.json")
        log_g = root / "conversion_log_g.json"
        entry_g = _log_entry(src_g, conv_g)
        entry_g["New_Bytes"] = conv_g.stat().st_size + 12345  # stale log
        log_g.write_text(json.dumps([entry_g]), encoding="utf-8")
        r = _run(SWAP_CMD + ["--report", str(report_g),
                  "--log", str(log_g), "--yes"], root, check=False)
        check("size mismatch vs log is not swapped",
              _codec(src_g) == "h264" and not (media / "Golf.mkv.bak").exists())

        # --- Fixture H: DVD row never swapped ---------------------------
        src_h = media / "Hotel" / "VIDEO_TS" / "VTS_01"
        report_h = _write_report(root, [{
            "Path": str(src_h), "Filename": "VTS_01",
            "Conversion_Status": "converted",
            "Output_Path": str(converted / "Hotel_vts01.mkv"),
            "Source_Kind": "dvd",
            "Source_Files": [str(media / "Hotel" / "VIDEO_TS" / "VTS_01_1.VOB")],
        }], "report_h.json")
        r = _run(SWAP_CMD + ["--report", str(report_h),
                  "--yes"], root, check=False)
        check("DVD job skipped, nothing created for it",
              r.returncode == 0 and "DVD job" in r.stdout
              and not list((media / "Hotel").rglob("*.bak")), r.stdout)

        # --- Fixture I: --recover restores a missing original -----------
        # Alpha was swapped above: remove its (new) original, leave the
        # backup and an orphan staging file, then recover.
        staging_i = media / ".Alpha.swap-part.mkv"
        staging_i.write_bytes(b"orphan staging")
        src_a.unlink()
        check("interrupted-swap fixture: original missing, backup present",
              not src_a.exists() and backup_a.is_file())
        r = _run(SWAP_CMD + ["--report", str(report_a),
                  "--recover", "--yes"], root, check=False)
        check("recover restores original from backup, removes staging",
              src_a.is_file() and _codec(src_a) == "h264"
              and not staging_i.exists() and not backup_a.exists(),
              r.stdout + r.stderr)

        # --- Fixture J: --recover dry-run / no---yes change nothing -----
        # Regression: recover used to run before the dry-run guard, so
        # a "preview" recovery wrote swap_log and always exited 0.
        src_a.unlink()
        backup_a.write_bytes(orig_a_bytes)
        staging_i.write_bytes(b"orphan staging")
        log_before = (root / "swap_log.json").read_bytes()
        r = _run(SWAP_CMD + ["--report", str(report_a),
                  "--recover", "--dry-run"], root, check=False)
        check("recover dry-run changes nothing and exits 0",
              r.returncode == 0 and not src_a.exists()
              and staging_i.is_file()
              and (root / "swap_log.json").read_bytes() == log_before,
              r.stdout + r.stderr)
        r = _run(SWAP_CMD + ["--report", str(report_a),
                  "--recover"], root, check=False)
        check("recover without --yes changes nothing and exits 2",
              r.returncode == 2 and not src_a.exists()
              and staging_i.is_file()
              and (root / "swap_log.json").read_bytes() == log_before,
              r.stdout + r.stderr)
        r = _run(SWAP_CMD + ["--report", str(report_a),
                  "--recover", "--yes"], root, check=False)
        check("recover --yes restores after the plan runs",
              r.returncode == 0 and src_a.is_file()
              and _codec(src_a) == "h264" and not staging_i.exists(),
              r.stdout + r.stderr)

    print(f"\n{len(failures)} failure(s)" if failures else "\nAll swap checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
