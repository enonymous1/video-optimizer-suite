#!/usr/bin/env python3
"""One-command orchestrator: Inspect -> Import -> Process -> Verify -> Replace.

This is a thin driver, not a second converter: it shells out to the
real stages (in-process for import/convert, so state stays in one DB)
in order, and stops at the first stage that fails. It exists so a
headless Unraid/Docker run is one cron line instead of four commands
that can be run in the wrong order.

    video-pipeline --scan-dir /mnt/media --db state.db --encoder nvenc

Stages:
  1. Inspect -- runs the scanner bundled in this same folder
     (video_optimizer_scan.py, the copy supplied with the project).
     No separate scanner copy is needed anywhere else. Only if you
     deliberately pass --analyzer is a different scanner used; the
     old sibling ../video-meta-scan/ copy is a fallback, not a
     requirement. Skipped with --no-scan when you already have a
     fresh report.
     NOTE: the scanner still writes its JSON/CSV checklist; the DB is
     the pipeline's source of truth after import, the report is the
     human-readable export.
  2. Import -- state_manager.import_report() into state.db. Converted
     rows are preserved unless the file changed (size/mtime).
  2b. DVD ingest -- only with --dvd-dir: dvd_ingest.py scans for
     VIDEO_TS directories and imports one job per title set into the
     same DB. Skipped by --no-scan except for an existing dvd_report.
  2c. Camcorder ingest -- only with --camcorder-dir: camcorder_ingest.py
     scans for legacy camcorder files (.mod/.tod/.m2t and the
     camcorder formats the scanner already covers) and imports one
     job per file into the same DB, with its sidecars and field
     order. It is imported AFTER the main report, so a file both
     scanners found keeps the camcorder row. Skipped by --no-scan
     except for an existing camcorder_report.
  3. Process/Verify/Replace -- hevc_convert.main() with the same
     flags you would pass it directly (--priority, --replace, etc.).
     Verification is inside that stage, not a separate step you can
     forget.
  4. Report -- prints state_manager.summary().

Safety defaults for unattended runs:
  * --replace is OFF unless you pass it. A cron job that only converts
    to --out-dir can never delete an original.
  * --confirm-over-gb still applies; for cron, pass --yes explicitly
    (and think about whether you want it) or set --confirm-over-gb 0
    to disable the gate. Without a TTY, large files are skipped, not
    guessed at.
  * A lock file (<db>.lock) prevents two pipeline runs from racing
    over the same DB. A stale lock (PID dead) is reclaimed; a live
    lock exits with code 3 instead of double-encoding.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .state_manager import StateDB, load_report_file  # noqa: E402
from . import hevc_convert  # noqa: E402


def _acquire_lock(lock_path: Path) -> bool:
    """PID lockfile. Returns False if another live run holds it."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid = int(lock_path.read_text().strip())
            os.kill(pid, 0)  # raises if the PID is dead
            return False
        except (ValueError, OSError):
            lock_path.unlink(missing_ok=True)  # stale lock: reclaim
            return _acquire_lock(lock_path)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _module_env() -> dict:
    """Environment for child ``python -m video_optimizer.*`` calls.

    The package root (the suite folder) is prepended to PYTHONPATH
    so a child module resolves even when the driver was invoked
    from another working directory without an install. Without
    this, ``python -m`` in a child inherits only the caller's cwd
    and site-packages, and a non-installed run from elsewhere
    fails to find the package.
    """
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + prev if prev else "")
    return env


def run_scan(analyzer: Path | None, scan_dir: Path, out_dir: Path,
             threads: int, min_size_mb: float) -> Path:
    """Run the scanner as a subprocess; return the JSON report.

    ``analyzer`` is None for the bundled package module
    (``python -m video_optimizer.video_optimizer_scan``) or a Path
    to an external scanner file (the ``--analyzer`` override).
    CLI compatibility: an older scanner copy takes the scan
    directory as a positional argument, while the bundled copy
    takes --dir (and prompts if it is missing). Both are always
    supplied here, so the scanner never blocks on stdin; the form
    is chosen from the scanner's own --help output.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = ([sys.executable, "-m", "video_optimizer.video_optimizer_scan"]
              if analyzer is None else [sys.executable, str(analyzer)])
    help_proc = subprocess.run(
        prefix + ["--help"], env=_module_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    help_text = help_proc.stdout + help_proc.stderr
    if "--dir" in help_text:
        dir_args = ["--dir", str(scan_dir)]
    else:
        dir_args = [str(scan_dir)]
    cmd = prefix + dir_args + [
           "--threads", str(threads), "--out-dir", str(out_dir),
           "--min-size-mb", str(min_size_mb)]
    proc = subprocess.run(cmd, env=_module_env(), text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"scanner exited rc={proc.returncode}")
    report = out_dir / "video_optimization_report.json"
    if not report.is_file():
        raise RuntimeError(f"scanner did not produce {report}")
    return report


def run_dvd_scan(dvd_dir: Path, out_dir: Path) -> Path | None:
    """Run the dvd_ingest module's scan as a subprocess; return its JSON report.

    Returns None when no VIDEO_TS directory is found (dvd_ingest exits
    2 with no report written) -- that is a normal empty result, not a
    failure. Any other non-zero exit raises.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "video_optimizer.dvd_ingest", "scan",
           str(dvd_dir), "--out-dir", str(out_dir), "--stem", "dvd_report"]
    proc = subprocess.run(cmd, env=_module_env(), text=True,
                          encoding="utf-8", errors="replace")
    report = out_dir / "dvd_report.json"
    if proc.returncode == 2 and not report.is_file():
        return None
    if proc.returncode != 0:
        raise RuntimeError(f"dvd_ingest scan exited rc={proc.returncode}")
    if not report.is_file():
        raise RuntimeError(f"dvd_ingest did not produce {report}")
    return report


def run_camcorder_scan(camcorder_dir: Path, out_dir: Path) -> Path | None:
    """Run the camcorder_ingest module's scan as a subprocess; return its report.

    Returns None when no camcorder file is found (camcorder_ingest
    exits 2 with no report written) -- a normal empty result, not a
    failure. Any other non-zero exit raises.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "video_optimizer.camcorder_ingest", "scan",
           str(camcorder_dir), "--out-dir", str(out_dir),
           "--stem", "camcorder_report"]
    proc = subprocess.run(cmd, env=_module_env(), text=True,
                          encoding="utf-8", errors="replace")
    report = out_dir / "camcorder_report.json"
    if proc.returncode == 2 and not report.is_file():
        return None
    if proc.returncode != 0:
        raise RuntimeError(
            f"camcorder_ingest scan exited rc={proc.returncode}")
    if not report.is_file():
        raise RuntimeError("camcorder_ingest did not produce {report}")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="End-to-end HEVC pipeline driver.")
    ap.add_argument("--scan-dir", type=Path, required=True)
    ap.add_argument("--dvd-dir", type=Path, default=None,
                    help="Also scan this root for VIDEO_TS directories "
                         "(dvd_ingest.py) and import those title sets "
                         "into the same DB. May be the same as --scan-dir.")
    ap.add_argument("--dvd-mode", choices=["480p", "1080p"], default="480p",
                    help="DVD title sets only: 480p (deinterlace, keep "
                         "480p) or 1080p (deinterlace + Lanczos upscale)")
    ap.add_argument("--camcorder-dir", type=Path, default=None,
                    help="Also scan this root for camcorder files "
                         "(camcorder_ingest.py) and import those jobs "
                         "into the same DB. May be the same as --scan-dir.")
    ap.add_argument("--camcorder-mode", choices=["auto", "deinterlace", "keep"],
                    default="auto",
                    help="Camcorder jobs only: auto (bwdif only when "
                         "field_order says interlaced), deinterlace, keep")
    ap.add_argument("--camcorder-metadata",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="Camcorder jobs only: exiftool metadata/sidecar "
                         "handling (see hevc_convert.py)")
    ap.add_argument("--db", type=Path, default=Path("state.db"))
    ap.add_argument("--reports-dir", type=Path, default=Path("reports"))
    ap.add_argument("--analyzer", type=Path, default=None,
                    help="Scanner file path, only if you want to override "
                         "the bundled video_optimizer.video_optimizer_scan "
                         "module")
    ap.add_argument("--no-scan", action="store_true",
                    help="Skip stage 1; use the existing report in --reports-dir")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--min-size-mb", type=float, default=0)
    ap.add_argument("--out-dir", type=Path, default=Path("converted"))
    ap.add_argument("--encoder", choices=["x265", "nvenc", "qsv"], default="x265")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--auto-jobs", action="store_true",
                    help="Size parallel jobs from detected cores/RAM/encoder")
    ap.add_argument("--threads-per-job", type=int, default=0,
                    help="ffmpeg threads per encode (0 = cores // jobs)")
    ap.add_argument("--priority", choices=["high", "medium", "low", "all"],
                    default="high")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-savings-gb", type=float, default=0)
    ap.add_argument("--include-modern", action="store_true")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--backup-dir", type=Path, default=None)
    ap.add_argument("--purge-backup", action="store_true")
    ap.add_argument("--confirm-over-gb", type=float, default=3.0)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    lock_path = args.db.with_suffix(".lock")
    if not _acquire_lock(lock_path):
        print(f"ERROR: another pipeline run holds {lock_path}; exiting.",
              file=sys.stderr)
        return 3
    try:
        report: Path
        if args.no_scan:
            report = args.reports_dir / "video_optimization_report.json"
            if not report.is_file():
                print(f"ERROR: --no-scan but no report at {report}",
                      file=sys.stderr)
                return 2
        else:
            # Default is the bundled package module (run_scan passes
            # analyzer=None for the -m form). The old sibling-folder
            # fallback is gone with the flat layout: if this driver
            # runs, the package is importable, so the module is too.
            # Only an explicit --analyzer override is a file path,
            # and it must exist.
            analyzer = args.analyzer
            if analyzer is not None and not analyzer.is_file():
                print(f"ERROR: --analyzer file not found: {analyzer}",
                      file=sys.stderr)
                return 2
            print(f"[1/4] Inspect: scanning {args.scan_dir}")
            report = run_scan(analyzer, args.scan_dir, args.reports_dir,
                              args.threads, args.min_size_mb)

        print("[2/4] Import: report -> SQLite")
        db = StateDB(args.db)
        try:
            print("      ", db.import_report(load_report_file(report)))
        finally:
            db.close()

        if args.dvd_dir is not None:
            print("[2/4] DVD ingest: VIDEO_TS -> title sets")
            db = StateDB(args.db)
            try:
                if args.no_scan:
                    dvd_report = args.reports_dir / "dvd_report.json"
                    if dvd_report.is_file():
                        print("      ", db.import_report(
                            load_report_file(dvd_report)))
                    else:
                        print(f"       no existing {dvd_report}; "
                              f"skipping DVD import")
                else:
                    dvd_report = run_dvd_scan(args.dvd_dir, args.reports_dir)
                    if dvd_report is None:
                        print("       no VIDEO_TS directories found; "
                              "nothing to import")
                    else:
                        print("      ", db.import_report(
                            load_report_file(dvd_report)))
            finally:
                db.close()

        if args.camcorder_dir is not None:
            print("[2/4] Camcorder ingest: camcorder files -> jobs")
            db = StateDB(args.db)
            try:
                if args.no_scan:
                    cam_report = args.reports_dir / "camcorder_report.json"
                    if cam_report.is_file():
                        print("      ", db.import_report(
                            load_report_file(cam_report)))
                    else:
                        print(f"       no existing {cam_report}; "
                              f"skipping camcorder import")
                else:
                    cam_report = run_camcorder_scan(args.camcorder_dir,
                                                    args.reports_dir)
                    if cam_report is None:
                        print("       no camcorder files found; "
                              "nothing to import")
                    else:
                        print("      ", db.import_report(
                            load_report_file(cam_report)))
            finally:
                db.close()

        print(f"[3/4] Process/Verify{'/Replace' if args.replace else ''}: "
              f"{args.encoder}, priority={args.priority}")
        convert_argv = [
            "--db", str(args.db),
            "--scan-root", str(args.scan_dir),
            "--out-dir", str(args.out_dir),
            "--encoder", args.encoder, "--crf", str(args.crf),
            "--preset", args.preset, "--jobs", str(args.jobs),
            "--threads-per-job", str(args.threads_per_job),
            "--dvd-mode", args.dvd_mode,
            "--camcorder-mode", args.camcorder_mode,
            "--priority", args.priority,
            "--limit", str(args.limit),
            "--min-savings-gb", str(args.min_savings_gb),
            "--confirm-over-gb", str(args.confirm_over_gb),
        ]
        if args.auto_jobs:
            convert_argv.append("--auto-jobs")
        convert_argv.append("--camcorder-metadata" if args.camcorder_metadata
                            else "--no-camcorder-metadata")
        if args.include_modern:
            convert_argv.append("--include-modern")
        if args.replace:
            convert_argv.append("--replace")
        if args.backup_dir is not None:
            convert_argv += ["--backup-dir", str(args.backup_dir)]
        if args.purge_backup:
            convert_argv.append("--purge-backup")
        if args.yes:
            convert_argv.append("--yes")
        if args.dry_run:
            convert_argv.append("--dry-run")
        rc = hevc_convert.main(convert_argv)
        if rc not in (0, 130):
            print(f"[3/4] converter exited rc={rc}", file=sys.stderr)
            return rc

        print("[4/4] Summary:")
        db = StateDB(args.db)
        try:
            print(json.dumps(db.summary(), indent=2))
        finally:
            db.close()
        return rc
    finally:
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
