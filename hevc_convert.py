#!/usr/bin/env python3
"""Processor: transcode flagged videos to HEVC, verify, optionally replace.

Pipeline stage 2 (Process) + stage 3 (Verify) + stage 4 (Replace), in
one script so a file is never left half-processed between stages.

    Inspect  -- video_optimizer_scan.py (existing, unchanged)
    Import   -- state_manager.py import --report ... --db state.db
    Process  -- this script, --db state.db --priority high
    Verify   -- quality_check.py, called internally for every encode
    Replace  -- this script, --replace (off by default)
    View     -- dashboard.py, reads state.db

Defaults are deliberately conservative, because this script can delete
data if you ask it to:
  * WITHOUT --replace, originals are never touched. Outputs go to
    --out-dir (mirroring --scan-root, as in v1) and the original stays
    put. This is the v1 safety model, and it is still the default.
  * WITH --replace, the verified encode atomically replaces the
    original (os.replace, same filesystem), after the original has
    been moved to a backup (.bak alongside, or --backup-dir). Add
    --purge-backup only when you are sure you no longer want the
    safety net. The DB row is marked converted ONLY after the replace
    succeeds -- a failed replace leaves the original in place and the
    row as failed_verify / interrupted, never "converted".

Staging & interrupts:
  * Every encode writes to a staging file in the SAME directory (and
    therefore filesystem) as its final destination, named
    ``.<stem>.hevc-part<ext>``. Only after verification passes is it
    os.replace()d into place. A killed process therefore leaves at
    most a ``.hevc-part`` file, never a truncated "final" file, and
    never a converted DB row.
  * SIGINT / SIGTERM set a shutdown flag, terminate the running
    ffmpeg, delete the staging file, and mark the job "interrupted"
    in the DB. Re-running picks it up again.
  * --staging-dir is honoured ONLY if it is on the same filesystem
    as the destination; otherwise the converter warns and stages
    next to the destination anyway (cross-filesystem os.replace
    would fail at the last step, which is exactly when you do not
    want a surprise).

Encoder selection (see encoders.py for the full profiles):
    --encoder x265   CPU libx265, CRF (default, best compression)
    --encoder nvenc  NVIDIA hevc_nvenc, CQ, p1..p7 (RTX 3060)
    --encoder qsv    Intel hevc_qsv, global_quality (i9-10850K UHD 630)
  Swapping is one flag. There is no silent fallback: asking for nvenc
  or qsv without working hardware fails fast, before any encode.

Resources (see resources.py):
  Previously there was NO dynamic sizing: one x265 job used all cores
  (ffmpeg threads=auto), parallel jobs were manual --jobs only, and
  --jobs 3 meant three jobs each fighting for all cores. Now
  --auto-jobs sizes jobs from detected cores + RAM + encoder, and
  threads are always divided (cores // jobs) so total threads never
  exceed core count. Sizing is at startup only -- it does not watch
  load or thermals mid-run, and a second pipeline on the same machine
  is not accounted for.

Verification (quality_check.verify, same module the standalone CLI
uses, so the gate and the CLI can never drift apart):
  duration match, HEVC video stream, audio count/codec/channels match,
  non-empty, not suspiciously small, full decode with no ffmpeg errors.
  Subtitle loss is a warning, recorded as converted_no_subs.

DVD jobs (dvd_ingest.py, see --dvd-mode):
  A DVD title set has no single input file: the converter feeds the
  concat demuxer a list of the set's content VOBs, applies bwdif
  deinterlacing (plus an optional Lanczos upscale to 1080p), and
  verifies against a summed reference (total size/duration, first
  VOB's stream makeup) via quality_check.verify_reference -- the same
  hard checks, no duplicated gate. --replace is refused for DVD jobs:
  the original is a VIDEO_TS directory, so outputs always go to
  --out-dir and the disc folder is deleted by hand after verifying.

Examples:
    # Preview what High Priority would do (nothing encoded):
    python hevc_convert.py --db state.db --dry-run
    # Convert High Priority to a separate tree, originals untouched:
    python hevc_convert.py --db state.db --encoder nvenc --preset p7 --jobs 2
    # Same, but verified encodes replace originals (backup kept):
    python hevc_convert.py --db state.db --encoder x265 --replace --yes
    # JSON-report mode, no DB (v1-compatible input):
    python hevc_convert.py --report reports/video_optimization_report.json \\
        --scan-root /mnt/media --out-dir /mnt/converted --priority high
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dvd_ingest import write_concat_list  # noqa: E402
from encoders import (  # noqa: E402
    NVENC_PRESETS, QSV_PRESETS, X265_PRESETS, encoder_available,
    quality_label, resolve_nvenc_preset, video_args,
)
from quality_check import probe as probe_media  # noqa: E402
from quality_check import verify as verify_files  # noqa: E402
from quality_check import verify_reference  # noqa: E402
from resources import recommend as recommend_resources  # noqa: E402
from state_manager import StateDB, load_report_file  # noqa: E402

__version__ = "2.2.0"

try:
    from tqdm import tqdm
    _HAS_TQDM = True
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore
    _HAS_TQDM = False

MODERN_CODECS = {"HEVC", "AV1", "VP9"}
HEVC_CONTAINERS = {".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts", ".mts"}

LOG_COLUMNS = [
    "Input_Path", "Output_Path", "Status", "Status_Detail",
    "Original_GB", "New_GB", "Actual_Savings_GB", "Actual_Savings_Pct",
    "Elapsed_Seconds", "CRF", "Preset",
]

_print_lock = threading.Lock()
_SHUTDOWN = threading.Event()
_ACTIVE_PROCS: set[subprocess.Popen] = set()
_PROCS_LOCK = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{sign}{n:.1f} {unit}"
        n /= 1024
    return f"{sign}{n:.1f} TB"


def _fmt_seconds(s: float) -> str:
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _install_signal_handlers(on_interrupt: Any) -> None:
    """SIGINT/SIGTERM: stop cleanly instead of dying mid-encode."""
    def handler(signum: int, _frame: Any) -> None:
        _SHUTDOWN.set()
        with _PROCS_LOCK:
            for proc in list(_ACTIVE_PROCS):
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001 - shutdown must not crash
                    pass
        _log(f"\n[shutdown] signal {signum}: stopping after current cleanup...")
        if on_interrupt is not None:
            on_interrupt()
    # Only the main thread may install handlers; workers never call this.
    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except ValueError:
        pass  # not the main thread (tests) -- shutdown flag still works


# ---------------------------------------------------------------------------
# Selection (JSON-report mode; DB mode uses StateDB.pending/claim instead)
# ---------------------------------------------------------------------------
def select_files(rows: list[dict], args: argparse.Namespace) -> tuple[list[dict], int]:
    wanted = {"high": "High Priority", "medium": "Medium",
              "low": "Low"}.get(args.priority)
    eligible: list[dict] = []
    for r in rows:
        if r.get("Error"):
            continue
        if wanted and r.get("Optimization_Candidate") != wanted:
            continue
        if not args.include_modern and (r.get("Video_Codec") or "").upper() in MODERN_CODECS:
            continue
        if (r.get("Estimated_Savings_GB") or 0.0) < args.min_savings_gb:
            continue
        eligible.append(r)
    n_done = 0
    if args.skip_done:
        kept = []
        for r in eligible:
            if (r.get("Conversion_Status") or "") in ("converted", "converted_no_subs"):
                n_done += 1
            else:
                kept.append(r)
        eligible = kept
    sort_keys = {"size": "Size_GB", "savings": "Estimated_Savings_GB"}
    keys = [sort_keys[k.strip()] for k in args.sort_by.split(",")
            if k.strip() in sort_keys] or ["Estimated_Savings_GB"]
    reverse = args.order == "desc"
    eligible.sort(key=lambda r: tuple(
        -(r.get(k) or 0.0) if reverse else (r.get(k) or 0.0) for k in keys))
    if args.limit and args.limit > 0:
        eligible = eligible[: args.limit]
    return eligible, n_done


def confirm_large_files(work: list[dict], args: argparse.Namespace
                        ) -> tuple[list[dict], list[tuple[dict, str]]]:
    if args.confirm_over_gb <= 0:
        return work, []
    approved_all = args.yes
    final, skipped = [], []
    for r in work:
        size = r.get("Size_GB") or 0.0
        if size < args.confirm_over_gb or approved_all:
            final.append(r)
            continue
        if not sys.stdin.isatty():
            _log(f"[WARN] No TTY: skipping large file {r.get('Filename')} "
                 f"({size:.1f} GB) -- pass --yes to approve")
            skipped.append((r, "no_tty"))
            continue
        sav = r.get("Estimated_Savings_GB") or 0.0
        ans = input(
            f"\nLarge file: {r.get('Filename')} ({size:.1f} GB)\n"
            f"  {r.get('Video_Codec') or '?'} {r.get('Resolution') or ''} | "
            f"est. savings {sav:.1f} GB\n"
            f"  Convert? [y]es / [n]o / [a]ll remaining large: "
        ).strip().lower()
        if ans in ("a", "all"):
            approved_all = True
            final.append(r)
        elif ans in ("y", "yes"):
            final.append(r)
        else:
            skipped.append((r, "user"))
    return final, skipped


# ---------------------------------------------------------------------------
# Paths: final destination, staging (same filesystem, always)
# ---------------------------------------------------------------------------
def build_output_path(input_path: Path, scan_root: Path | None,
                      out_dir: Path) -> Path:
    if scan_root:
        try:
            rel = input_path.resolve().relative_to(scan_root.resolve())
            return out_dir / rel
        except ValueError:
            pass
    stem = f"{input_path.stem}.hevc"
    candidate = out_dir / f"{stem}{input_path.suffix}"
    n = 1
    while candidate.exists():
        n += 1
        candidate = out_dir / f"{stem}_{n}{input_path.suffix}"
    return candidate


def final_path_for(src: Path, args: argparse.Namespace) -> Path:
    """Where the verified file ends up: in place (--replace) or --out-dir."""
    if args.replace:
        return src
    dst = build_output_path(src, args.scan_root, args.out_dir)
    if dst.suffix.lower() not in HEVC_CONTAINERS:
        dst = dst.with_suffix(".mkv")
    return dst


def staging_path_for(final: Path, staging_dir: Path | None) -> Path:
    """Staging file on the SAME filesystem as *final* (os.replace needs it).

    If --staging-dir is on a different device, it is ignored with a
    warning and staging falls back to the destination directory.
    """
    if staging_dir is not None:
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            if os.stat(staging_dir).st_dev == os.stat(final.parent).st_dev:
                return staging_dir / f".{final.stem}.hevc-part{final.suffix}"
            _log(f"[WARN] --staging-dir {staging_dir} is on a different "
                 f"filesystem than {final.parent}; staging next to the "
                 f"destination instead (atomic replace requires same fs).")
        except OSError as exc:
            _log(f"[WARN] Cannot use --staging-dir ({exc}); staging in place.")
    final.parent.mkdir(parents=True, exist_ok=True)
    return final.parent / f".{final.stem}.hevc-part{final.suffix}"


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# ffmpeg encode (Popen, so a signal can terminate it)
# ---------------------------------------------------------------------------
def _encode(cmd: list[str]) -> tuple[bool, str]:
    """Run ffmpeg, returning (ok, stderr-tail). Registers the Popen so
    the signal handler can terminate it. A shutdown during the encode
    returns ok=False with a distinct message the caller maps to
    'interrupted', not 'failed'."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "ffmpeg not found on PATH"
    with _PROCS_LOCK:
        _ACTIVE_PROCS.add(proc)
    try:
        _, stderr = proc.communicate()
    finally:
        with _PROCS_LOCK:
            _ACTIVE_PROCS.discard(proc)
    if _SHUTDOWN.is_set():
        return False, "interrupted by signal"
    ok = proc.returncode == 0
    tail = "\n".join((stderr or "").strip().splitlines()[-8:])
    return ok, tail


def _probe_duration(path: Path) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        return float(json.loads(proc.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001 - probe failure is a verify failure
        return None


# ---------------------------------------------------------------------------
# DVD jobs (dvd_ingest.py): a title set is many VOBs, not one file
# ---------------------------------------------------------------------------
def _dvd_source_files(row: dict) -> list[Path] | None:
    """Content VOBs for a DVD job, or None for a flat-file job.

    Accepts the report shape (a list under Source_Files) and the DB
    shape (a JSON string under source_files). A row that claims to be
    a DVD job but carries no usable list is a failure, not a flat file.
    """
    kind = row.get("Source_Kind") or row.get("source_kind")
    raw = row.get("Source_Files") or row.get("source_files")
    if raw is None:
        return None
    files: list[Path] = []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [raw]
        raw = parsed
    if isinstance(raw, (list, tuple)):
        files = [Path(str(f)) for f in raw if str(f)]
    if kind == "dvd_vts" or files:
        return files
    return None


def _is_dvd_job(row: dict) -> bool:
    return _dvd_source_files(row) is not None


def _dvd_filter(mode: str) -> str:
    """Deinterlace (+ optional upscale) filter for DVD (480i) sources.

    scale uses -2 (not -1) so the output width is always even, which
    yuv420p output requires. The old script's tune=animation is NOT
    carried over: encoder profiles live in encoders.py and apply to
    every source, not just DVDs.
    """
    if mode == "1080p":
        return "bwdif=1,scale=-2:1080:flags=lanczos"
    return "bwdif=1"


def _dvd_final_path(row: dict, args: argparse.Namespace) -> Path:
    """Output path for a DVD job: <out-dir>/<disc tree>/<movie>_vtsNN[.mkv].

    The disc tree mirrors --scan-root, so two discs with the same title
    do not collide. 1080p mode gains a _1080p suffix (the old script's
    naming); 480p mode has no suffix.
    """
    video_ts = Path(str(row.get("Video_TS_Dir")
                        or row.get("video_ts_dir") or ""))
    vts_num = str(row.get("VTS_Number") or row.get("vts_number") or "")
    if not video_ts.name or not vts_num:
        # Rows from an older DB/report may lack the DVD columns; the
        # synthetic job key <VIDEO_TS>/VTS_NN encodes both exactly.
        job_key = Path(str(row.get("Path") or row.get("path") or ""))
        if job_key.name.upper().startswith("VTS_"):
            video_ts = job_key.parent
            vts_num = job_key.name[4:]
    if not vts_num:
        vts_num = "01"
    movie = video_ts.parent.name if video_ts.parent.name else "dvd"
    stem = f"{movie}_vts{vts_num}"
    if getattr(args, "dvd_mode", "480p") == "1080p":
        stem += "_1080p"
    if args.scan_root:
        try:
            rel = video_ts.parent.resolve().relative_to(
                Path(args.scan_root).resolve())
            return Path(args.out_dir) / rel / f"{stem}.mkv"
        except ValueError:
            pass
    return Path(args.out_dir) / f"{stem}.mkv"


def _dvd_reference(files: list[Path]) -> tuple[dict | None, str]:
    """Summed verification reference for a title set.

    Returns (reference, "") or (None, reason). The probe is the first
    content VOB's ffprobe JSON; duration/size are summed across the
    set, re-probed now so a moved or changed VOB fails here instead
    of being verified against stale scan data.
    """
    try:
        first_probe = probe_media(files[0])
    except RuntimeError as exc:
        return None, str(exc)
    total_bytes = 0
    total_duration = 0.0
    for f in files:
        try:
            total_bytes += f.stat().st_size
        except OSError as exc:
            return None, f"source VOB missing: {f} ({exc})"
        dur = _probe_duration(f)
        if dur is None:
            return None, f"could not read duration from {f}"
        total_duration += dur
    return {"size_bytes": total_bytes,
            "duration_seconds": round(total_duration, 1),
            "probe": first_probe}, ""


# ---------------------------------------------------------------------------
# Camcorder jobs (camcorder_ingest.py): flat files, with sidecars and
# a field-order record. They encode like flat files (no concat, no
# synthetic key), plus two camcorder-only steps: an optional
# deinterlace filter when ffprobe says the source is interlaced,
# and an exiftool metadata/sidecar step on the staging file, before
# verification -- so a failed encode never gets metadata handling,
# the bug the old HandBrake tool had.
# ---------------------------------------------------------------------------
def _camcorder_sidecars(row: dict) -> list[Path] | None:
    """Metadata sidecars for a camcorder job, or None for other jobs.

    Accepts the report shape (a list under Sidecar_Files) and the DB
    shape (a JSON string under sidecar_files). A camcorder job with
    no sidecars returns [] (still a camcorder job); any other kind
    returns None.
    """
    kind = row.get("Source_Kind") or row.get("source_kind")
    if kind != "camcorder":
        return None
    raw = row.get("Sidecar_Files") or row.get("sidecar_files")
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [raw]
        raw = parsed
    if isinstance(raw, (list, tuple)):
        return [Path(str(f)) for f in raw if str(f)]
    return []


def _is_camcorder_job(row: dict) -> bool:
    return _camcorder_sidecars(row) is not None


def _camcorder_field_order(row: dict) -> str | None:
    """ffprobe field_order for a camcorder job, lowercased, or None.

    Older exports may carry only the derived Interlaced flag; that
    maps back to "interlaced"/"progressive" so the auto mode still
    decides the same way.
    """
    raw = row.get("Field_Order") or row.get("field_order")
    if raw:
        return str(raw).strip().lower() or None
    interlaced = row.get("Interlaced") if "Interlaced" in row else row.get("interlaced")
    if interlaced is True:
        return "interlaced"
    if interlaced is False:
        return "progressive"
    return None


def _camcorder_filter(row: dict, args: argparse.Namespace) -> str | None:
    """Deinterlace filter for a camcorder job, or None.

    auto (default): bwdif only when field_order says interlaced.
    Unknown or progressive field order means no filter -- guessing
    either way would change footage silently. deinterlace forces
    bwdif; keep forces no filter.
    """
    mode = getattr(args, "camcorder_mode", "auto")
    if mode == "deinterlace":
        return "bwdif=1"
    if mode == "keep":
        return None
    order = _camcorder_field_order(row)
    if order in ("interlaced", "tt", "bb", "tb", "bt"):
        return "bwdif=1"
    return None


def _apply_camcorder_metadata(src: Path, staging: Path,
                              sidecars: list[Path]) -> dict:
    """Copy metadata + merge sidecars into *staging* with exiftool.

    Runs only on the staging file, before verification, so the
    verified bytes are the ones published. Never raises: exiftool
    is best-effort here (ffmpeg already carried the stream metadata
    with -map 0), and a failure is recorded so the caller can fall
    back to copying the sidecar files next to the published output.
    Returns an outcome dict; ``failed`` lists sidecars that still
    need the fallback copy.
    """
    outcome: dict = {"exiftool_available": False,
                     "source_metadata": False,
                     "merged": [], "failed": [], "warnings": []}
    if shutil.which("exiftool") is None:
        outcome["warnings"].append("exiftool not on PATH; sidecars will "
                                   "be copied next to the output instead")
        outcome["failed"] = [str(p) for p in sidecars]
        return outcome
    outcome["exiftool_available"] = True

    def _run(cmd: list[str]) -> tuple[bool, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=300)
        except Exception as exc:  # noqa: BLE001 - metadata must not fail an encode
            return False, str(exc)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-3:])
            return False, tail or f"exiftool exited rc={proc.returncode}"
        return True, ""

    ok, detail = _run(["exiftool", "-TagsFromFile", str(src),
                       "-All:All", "-overwrite_original", str(staging)])
    outcome["source_metadata"] = ok
    if not ok:
        outcome["warnings"].append(
            f"exiftool metadata copy from source failed: {detail[:200]}")
    for sidecar in sidecars:
        if not sidecar.is_file():
            outcome["failed"].append(str(sidecar))
            outcome["warnings"].append(
                f"sidecar missing since scan, not copied: {sidecar}")
            continue
        ok, detail = _run(["exiftool", "-TagsFromFile", str(sidecar),
                           "-All:All", "-overwrite_original", str(staging)])
        if ok:
            outcome["merged"].append(str(sidecar))
        else:
            outcome["failed"].append(str(sidecar))
            outcome["warnings"].append(
                f"exiftool merge failed for {sidecar}: {detail[:200]}")
    return outcome


def _copy_sidecar_fallback(sidecar: Path, final: Path) -> Path | None:
    """Copy *sidecar* next to *final* (same stem, sidecar suffix).

    Returns the destination, or None when there is nothing to copy
    (already adjacent, or the copy failed). Never raises.
    """
    dest = final.parent / (final.stem + sidecar.suffix)
    try:
        if dest.resolve() == sidecar.resolve():
            return dest  # already sitting next to the output
    except OSError:
        pass
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sidecar, dest)
    except OSError:
        return None
    return dest


def convert_one(row: dict, args: argparse.Namespace,
                db: StateDB | None = None) -> dict:
    """Convert, verify, and (if --replace) swap one file. Never raises."""
    log: dict = {c: None for c in LOG_COLUMNS}
    log["CRF"], log["Preset"] = args.crf, args.preset
    src = Path(str(row.get("Path") or row.get("path") or ""))
    log["Input_Path"] = str(src)

    def finish(status: str, detail: str, **extra: Any) -> dict:
        log["Status"] = status
        log["Status_Detail"] = detail
        log.update(extra)
        if db is not None and src:
            try:
                db.set_status(str(src), status, detail,
                              output_path=log.get("Output_Path"),
                              actual_savings_gb=log.get("Actual_Savings_GB"),
                              encoder=args.encoder, quality=args.crf,
                              preset=args.preset,
                              elapsed=log.get("Elapsed_Seconds"))
            except Exception as exc:  # noqa: BLE001 - DB loss must not
                # rewrite an encode result: the conversion_log still
                # records the true status, and the next import can
                # recover it. Never turn a published encode into a
                # phantom "failed" by raising here.
                _log(f"[WARN] DB update failed for {src} ({exc}); "
                     f"status {status} is in the conversion log.")
        return log

    if _SHUTDOWN.is_set():
        return finish("interrupted", "shutdown before start")
    dvd_files = _dvd_source_files(row)
    is_dvd = dvd_files is not None
    camcorder_sidecars = _camcorder_sidecars(row)
    is_camcorder = camcorder_sidecars is not None
    if is_camcorder:
        log["Source_Kind"] = "camcorder"
        log["Sidecar_Files"] = [str(p) for p in camcorder_sidecars]
    if is_dvd:
        # --replace cannot apply: the original is a VIDEO_TS directory,
        # and the replace path (os.replace + .bak) only knows files.
        if getattr(args, "replace", False):
            return finish(
                "failed", "--replace is not supported for DVD jobs: the "
                "original is a VIDEO_TS directory, not a file. Re-run "
                "without --replace; outputs go to --out-dir.")
        if not dvd_files:
            return finish("failed", "DVD job has no Source_Files "
                          "(re-run dvd_ingest.py scan)")
        missing = [f for f in dvd_files if not f.is_file()]
        if missing:
            return finish("failed",
                          f"DVD source VOB missing (moved since scan?): "
                          f"{missing[0]}")
        src_stat = None
        src_bytes = sum(f.stat().st_size for f in dvd_files)
        log["Source_Files"] = [str(f) for f in dvd_files]
    else:
        if not src.is_file():
            return finish("failed", "input file missing (moved since scan?)")
        src_stat = src.stat()
        src_bytes = src_stat.st_size
    log["Original_GB"] = round(src_bytes / 1024 ** 3, 3)
    log["Original_Bytes"] = src_bytes

    final = _dvd_final_path(row, args) if is_dvd else final_path_for(src, args)
    # Container switch only matters for --replace: the final path's
    # extension changes (avi -> mkv), so the "replace" is a move + a
    # delete of the old path, not an os.replace onto the same path.
    container_switched = None
    if args.replace and final.suffix.lower() not in HEVC_CONTAINERS:
        container_switched = (final.suffix.lower(), ".mkv")
        final = final.with_suffix(".mkv")
    log["Output_Path"] = str(final)
    if not args.replace and final.exists() and not args.overwrite:
        return finish("skipped_exists",
                      "output already exists (use --overwrite to redo)")
    if args.replace and not args.overwrite and final != src and final.exists():
        return finish("skipped_exists",
                      "replacement target already exists (use --overwrite)")

    staging = staging_path_for(final, args.staging_dir)
    _cleanup(staging)
    log["Staging_Path"] = str(staging)

    # DVD jobs encode from a concat-demuxer list, not a single file.
    # The list lives next to the staging file and is deleted once the
    # encode finishes (it is only ever read by ffmpeg).
    concat_list: Path | None = None
    if is_dvd:
        assert dvd_files is not None
        concat_list = final.parent / f".{final.stem}.concat-list.txt"
        try:
            write_concat_list(dvd_files, concat_list)
        except OSError as exc:
            return finish("failed", f"could not write concat list: {exc}")
        log["Concat_List"] = str(concat_list)

    def _drop_concat() -> None:
        if concat_list is not None:
            _cleanup(concat_list)

    bit_depth = row.get("Bit_Depth") or row.get("bit_depth")
    try:
        v_args = video_args(args.encoder, args.crf, args.preset,
                            int(bit_depth) if bit_depth is not None else None)
    except ValueError as exc:
        _drop_concat()
        return finish("failed", str(exc))
    # Threads are divided across parallel jobs (see resources.py):
    # without this, every job tries to use all cores. getattr() keeps
    # old test namespaces that lack the new fields working.
    threads = getattr(args, "threads_per_job", 0) or 0
    thread_args = ["-threads", str(threads)] if threads > 0 else []
    ffmpeg_head = (["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
                   + thread_args)
    if is_dvd:
        assert concat_list is not None
        dvd_mode = getattr(args, "dvd_mode", "480p")
        base = (ffmpeg_head
                + ["-f", "concat", "-safe", "0", "-i", str(concat_list),
                   "-map", "0", "-vf", _dvd_filter(dvd_mode)]
                + v_args + ["-c:a", "copy"])
    else:
        camcorder_vf = _camcorder_filter(row, args) if is_camcorder else None
        log["Camcorder_Filter"] = camcorder_vf
        base = (ffmpeg_head
                + ["-i", str(src), "-map", "0"]
                + (["-vf", camcorder_vf] if camcorder_vf else [])
                + v_args + ["-c:a", "copy"])
    if final.suffix.lower() in (".mp4", ".mov"):
        base += ["-tag:v", "hvc1"]  # Apple/QuickTime-friendly HEVC tag

    t0 = time.time()
    started_utc = datetime.now(timezone.utc).isoformat()
    # Attempt 1: subtitles copied. Attempt 2: dropped (some containers
    # cannot hold PGS subs in MP4, etc.). Never claim success on a
    # failed attempt-1 without a successful attempt-2 + verification.
    ok, err = _encode(base + ["-c:s", "copy", str(staging)])
    used_cmd, used_subs = base + ["-c:s", "copy", str(staging)], True
    if not ok and not _SHUTDOWN.is_set():
        _cleanup(staging)
        ok, err = _encode(base + ["-sn", str(staging)])
        used_cmd, used_subs = base + ["-sn", str(staging)], False
    elapsed = round(time.time() - t0, 1)
    log["Elapsed_Seconds"] = elapsed
    log["FFmpeg_Command"] = subprocess.list2cmdline(used_cmd)
    log["Started_UTC"] = started_utc
    log["Finished_UTC"] = datetime.now(timezone.utc).isoformat()
    if _SHUTDOWN.is_set() or err == "interrupted by signal":
        _cleanup(staging)
        _drop_concat()
        return finish("interrupted", "interrupted during encode")
    if not ok:
        _cleanup(staging)
        _drop_concat()
        return finish("failed", f"ffmpeg error: {err[:300]}")
    if not staging.is_file() or staging.stat().st_size == 0:
        _cleanup(staging)
        _drop_concat()
        return finish("failed_verify", "staging output missing/empty")
    _drop_concat()

    # Camcorder metadata/sidecars, on the staging file BEFORE
    # verification, so the verified bytes are the published bytes.
    # A metadata failure never fails the encode; the sidecars fall
    # back to plain copies next to the published output below.
    metadata: dict | None = None
    if is_camcorder and getattr(args, "camcorder_metadata", True):
        assert camcorder_sidecars is not None
        metadata = _apply_camcorder_metadata(src, staging, camcorder_sidecars)
        log["Camcorder_Metadata"] = metadata
        for warning in metadata["warnings"]:
            _log(f"[WARN] {warning} ({src})")

    # ---- verify BEFORE anything is replaced or marked done ---------------
    if is_dvd:
        assert dvd_files is not None
        reference, ref_err = _dvd_reference(dvd_files)
        if reference is None:
            _cleanup(staging)
            return finish("failed_verify",
                          f"could not build DVD reference: {ref_err}")
        result = verify_reference(reference, staging,
                                  sample_quality_flag=args.sample_quality)
    else:
        result = verify_files(src, staging,
                              sample_quality_flag=args.sample_quality)
    if not result.passed:
        _cleanup(staging)
        return finish("failed_verify", "; ".join(result.failures))
    new_bytes = staging.stat().st_size
    new_gb = new_bytes / 1024 ** 3
    saved = (log["Original_GB"] or 0) - new_gb
    log["New_GB"] = round(new_gb, 3)
    log["New_Bytes"] = new_bytes
    log["Actual_Savings_GB"] = round(saved, 3)
    log["Actual_Savings_Pct"] = round(100 * saved / log["Original_GB"], 1) \
        if log["Original_GB"] else 0.0
    detail = ("subtitles copied" if used_subs
              else "subtitles dropped (container incompatible)")
    if is_dvd:
        detail += (f"; dvd_mode={getattr(args, 'dvd_mode', '480p')} "
                   f"(filter {_dvd_filter(getattr(args, 'dvd_mode', '480p'))})")
    if is_camcorder:
        order = _camcorder_field_order(row)
        detail += (f"; camcorder_mode={getattr(args, 'camcorder_mode', 'auto')} "
                   f"(field_order={order or 'unknown'}, "
                   f"filter={log.get('Camcorder_Filter') or 'none'})")
        if metadata is not None:
            merged = len(metadata["merged"])
            failed = len(metadata["failed"])
            detail += (f"; metadata: exiftool="
                       f"{'yes' if metadata['exiftool_available'] else 'no'}, "
                       f"sidecars merged={merged}, fallback={failed}")
    if container_switched:
        detail += (f"; container {container_switched[0]} -> "
                   f"{container_switched[1]} (HEVC unsupported in "
                   f"{container_switched[0]})")
    if result.warnings:
        detail += "; warnings: " + "; ".join(result.warnings)
    if saved < 0:
        detail += f"; WARNING: output larger by {_fmt_bytes(-saved * 1024 ** 3)}"
    status = "converted" if used_subs else "converted_no_subs"

    # ---- replace / publish ------------------------------------------------
    try:
        if args.replace:
            backup: Path | None = None
            if args.backup_dir is not None:
                args.backup_dir.mkdir(parents=True, exist_ok=True)
                backup = args.backup_dir / src.name
                n = 1
                while backup.exists():
                    n += 1
                    backup = args.backup_dir / f"{src.stem}_{n}{src.suffix}"
                shutil.move(str(src), str(backup))
            else:
                backup = src.with_suffix(src.suffix + ".bak")
                n = 1
                while backup.exists():
                    n += 1
                    backup = src.with_name(f"{src.name}.{n}.bak")
                os.replace(src, backup)
            try:
                os.replace(staging, final)
            except OSError:
                # Roll back: put the original back, staging is deleted
                # below, and the job is NOT marked converted.
                try:
                    if final != src:
                        _cleanup(final)
                    os.replace(backup, src)
                except OSError:
                    pass
                raise
            # Preserve timestamps + permissions from the original.
            # (Linux birth/creation time cannot be set via stdlib; mtime
            # and atime are what Plex/Jellyfin and most tools actually
            # display, and they are what is preserved here.)
            try:
                os.chmod(final, src_stat.st_mode)
                os.utime(final, (src_stat.st_atime, src_stat.st_mtime))
            except OSError as exc:
                _log(f"[WARN] Could not restore timestamps on {final}: {exc}")
            if args.replace and final != src:
                # Container switch: the old path is gone by design
                # (backup holds it); nothing else to delete.
                pass
            log["Backup_Path"] = str(backup)
            if args.purge_backup and backup is not None:
                try:
                    backup.unlink()
                    log["Backup_Path"] = None
                except OSError as exc:
                    _log(f"[WARN] Could not purge backup {backup}: {exc}")
            log["Output_Path"] = str(final)
        else:
            os.replace(staging, final)
    except OSError as exc:
        _cleanup(staging)
        return finish("failed", f"publish/replace failed, original untouched: {exc}")

    # Sidecars exiftool could not merge (or no exiftool at all) are
    # copied next to the published output. This runs only after a
    # verified publish, never after a failed encode.
    if is_camcorder and metadata is not None and metadata["failed"]:
        copied: list[str] = []
        for failed in metadata["failed"]:
            dest = _copy_sidecar_fallback(Path(failed), final)
            if dest is not None:
                copied.append(str(dest))
        if copied:
            metadata["copied"] = copied
            log["Camcorder_Metadata"] = metadata

    try:
        log["Sidecar_Path"] = str(write_sidecar(log, row, args, used_subs))
    except OSError as exc:
        _log(f"[WARN] Could not write sidecar for {final}: {exc}")
    return finish(status, detail)


def write_sidecar(entry: dict, row: dict, args: argparse.Namespace,
                  used_subs: bool) -> Path:
    dst = Path(str(entry["Output_Path"]))
    src = Path(str(entry["Input_Path"]))
    sidecar = {
        "sidecar_version": 1,
        "generated_at_local": datetime.now().astimezone().isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tool": {"name": "hevc_convert.py", "version": __version__},
        "source": {
            "path": str(src), "filename": src.name,
            "size_bytes": entry.get("Original_Bytes"),
            "container": row.get("Container_Format") or row.get("container_format"),
            "duration_seconds": row.get("Duration_Seconds") or row.get("duration_seconds"),
            "video": {
                "codec": row.get("Video_Codec") or row.get("video_codec"),
                "resolution": row.get("Resolution") or row.get("resolution"),
                "bit_depth": row.get("Bit_Depth") or row.get("bit_depth"),
                "bitrate_mbps": row.get("Video_Bitrate_Mbps") or row.get("video_bitrate_mbps"),
            },
            "audio": row.get("Audio_Codecs") or row.get("audio_codecs"),
            "optimization_candidate": row.get("Optimization_Candidate") or row.get("candidate"),
            "estimated_savings_gb": row.get("Estimated_Savings_GB") or row.get("estimated_savings_gb"),
        },
        "transformation": {
            "started_utc": entry.get("Started_UTC"),
            "finished_utc": entry.get("Finished_UTC"),
            "elapsed_seconds": entry.get("Elapsed_Seconds"),
            "ffmpeg_command": entry.get("FFmpeg_Command"),
            "video_encoder": args.encoder,
            "quality_mode": quality_label(args.encoder),
            "quality_value": entry.get("CRF"),
            "preset": entry.get("Preset"),
            "threads_per_job": getattr(args, "threads_per_job", None),
            "audio": "copy (untouched)",
            "subtitles": "copied" if used_subs else "dropped (container incompatible)",
            "replace": bool(args.replace),
            "backup_path": entry.get("Backup_Path"),
        },
        "result": {
            "path": str(dst), "size_bytes": entry.get("New_Bytes"),
            "actual_savings_gb": entry.get("Actual_Savings_GB"),
            "actual_savings_pct": entry.get("Actual_Savings_Pct"),
            "verified": True,
            "verification": "quality_check: duration, HEVC codec, audio "
                            "count/codec/channels, full decode, non-empty",
        },
    }
    if _is_dvd_job(row):
        dvd_mode = getattr(args, "dvd_mode", "480p")
        raw_files = row.get("Source_Files") or row.get("source_files")
        if isinstance(raw_files, str):
            try:
                raw_files = json.loads(raw_files)
            except json.JSONDecodeError:
                pass
        sidecar["source"]["dvd"] = {
            "video_ts_dir": row.get("Video_TS_Dir") or row.get("video_ts_dir"),
            "vts_number": row.get("VTS_Number") or row.get("vts_number"),
            "source_files": raw_files,
            "ifo": row.get("IFO"),
            "job_key": str(src),
        }
        sidecar["transformation"]["dvd_mode"] = dvd_mode
        sidecar["transformation"]["video_filter"] = _dvd_filter(dvd_mode)
    if _is_camcorder_job(row):
        _order = _camcorder_field_order(row)
        sidecar["source"]["camcorder"] = {
            "original_extension": row.get("Original_Extension"),
            "sidecar_files": [str(p) for p in (_camcorder_sidecars(row) or [])],
            "field_order": _order,
            "interlaced": _order in ("interlaced", "tt", "bb", "tb", "bt"),
        }
        sidecar["transformation"]["camcorder_mode"] = getattr(
            args, "camcorder_mode", "auto")
        sidecar["transformation"]["video_filter"] = entry.get("Camcorder_Filter")
        sidecar["transformation"]["metadata"] = entry.get("Camcorder_Metadata")
    sidecar_path = dst.parent / f"{dst.name}.conversion.json"
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2, ensure_ascii=False)
    return sidecar_path


def write_log(logs: list[dict], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = out_dir / "conversion_log.csv", out_dir / "conversion_log.json"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for l in logs:
            w.writerow({c: ("" if l.get(c) is None else l.get(c)) for c in LOG_COLUMNS})
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump([{k: (v if v is not None else "") for k, v in l.items()}
                   for l in logs], fh, indent=2, ensure_ascii=False)
    return csv_path, json_path


def update_report_json(report: Path, logs: list[dict]) -> None:
    """Backwards-compatible checklist write-back into the scan report."""
    try:
        rows = load_report_file(report)
    except (OSError, json.JSONDecodeError) as exc:
        _log(f"[WARN] Could not update report ({exc}); checklist not saved.")
        return
    by_path = {r.get("Path"): r for r in rows if isinstance(r, dict)}
    now = datetime.now(timezone.utc).isoformat()
    touched = 0
    for l in logs:
        r = by_path.get(l.get("Input_Path") or "")
        if r is None:
            continue
        if l.get("Status") in ("converted", "converted_no_subs",
                               "failed", "failed_verify", "interrupted"):
            r["Conversion_Status"] = l["Status"]
            r["Converted_At_UTC"] = now
            r["Output_Path"] = l.get("Output_Path") or ""
            r["Actual_Savings_GB"] = l.get("Actual_Savings_GB")
            touched += 1
    if not touched:
        return
    if report.suffix.lower() == ".json":
        with open(report, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        _log(f"Report checklist updated: {report}")
    else:
        cols = sorted({k for r in rows for k in r})
        with open(report, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
        _log(f"Report checklist updated: {report}")


def print_summary(logs: list[dict]) -> None:
    done = [l for l in logs if l["Status"] in ("converted", "converted_no_subs")]
    failed = [l for l in logs if l["Status"] in ("failed", "failed_verify")]
    inter = [l for l in logs if l["Status"] == "interrupted"]
    skipped = [l for l in logs if str(l["Status"]).startswith("skipped")]
    orig_b = sum(l.get("Original_Bytes") or 0 for l in done)
    new_b = sum(l.get("New_Bytes") or 0 for l in done)
    saved_b = orig_b - new_b
    secs = sum(l.get("Elapsed_Seconds") or 0 for l in done)
    print("\n==== Conversion summary ====")
    print(f"Converted: {len(done)}   Skipped: {len(skipped)}   "
          f"Failed: {len(failed)}   Interrupted: {len(inter)}")
    if done:
        print(f"Original size:  {_fmt_bytes(orig_b)}")
        print(f"New size:       {_fmt_bytes(new_b)}")
        pct = f" ({100 * saved_b / orig_b:.0f}% of converted)" if orig_b else ""
        print(f"Actual saved:   {_fmt_bytes(saved_b)}{pct}")
        print(f"Encode time:    {_fmt_seconds(secs)}")
    for l in failed + inter:
        print(f"  {l['Status'].upper()}: {l['Input_Path']}\n"
              f"          {l['Status_Detail']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Convert flagged videos to HEVC, verify, optionally replace.")
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--db", type=Path, default=None,
                     help="SQLite state DB (preferred; see state_manager.py)")
    src.add_argument("--report", type=Path, default=None,
                     help="Scan report JSON/CSV (no-DB mode)")
    ap.add_argument("--scan-root", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=Path("converted"),
                    help="Output tree when NOT using --replace")
    ap.add_argument("--staging-dir", type=Path, default=None,
                    help="Staging dir (must share a filesystem with the "
                         "destination or it is ignored with a warning)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sort-by", default="savings")
    ap.add_argument("--order", choices=["asc", "desc"], default="desc")
    ap.add_argument("--min-savings-gb", type=float, default=0)
    ap.add_argument("--priority", choices=["high", "medium", "low", "all"],
                    default="high",
                    help="Default high: only High Priority jobs (v1 default "
                         "was all; pass --priority all to reproduce it)")
    ap.add_argument("--include-modern", action="store_true")
    ap.add_argument("--encoder", choices=["x265", "nvenc", "qsv"], default="x265")
    ap.add_argument("--crf", type=int, default=20,
                    help="Quality: CRF (x265) / CQ (nvenc) / global_quality (qsv)")
    ap.add_argument("--preset", default="medium",
                    help="x265/qsv: ultrafast..veryslow; nvenc: p1..p7 "
                         "(x265 names auto-mapped for nvenc)")
    ap.add_argument("--dvd-mode", choices=["480p", "1080p"], default="480p",
                    help="DVD title sets only (ignored for flat files): "
                         "480p = deinterlace, keep 480p; 1080p = "
                         "deinterlace + Lanczos upscale to 1080p")
    ap.add_argument("--camcorder-mode", choices=["auto", "deinterlace", "keep"],
                    default="auto",
                    help="Camcorder jobs only (ignored for other files): "
                         "auto = bwdif only when ffprobe field_order says "
                         "interlaced; deinterlace = always bwdif; "
                         "keep = never deinterlace")
    ap.add_argument("--camcorder-metadata",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="Camcorder jobs only: copy metadata and merge "
                         "sidecars with exiftool on the staging file "
                         "(fallback: copy sidecars next to the output)")
    ap.add_argument("--jobs", type=int, default=1,
                    help="Parallel conversions (default: 1; x265 already "
                         "multithreads, so raise cautiously or use "
                         "--auto-jobs)")
    ap.add_argument("--auto-jobs", action="store_true",
                    help="Size --jobs from detected cores + RAM + encoder "
                         "(see resources.py) instead of using --jobs")
    ap.add_argument("--threads-per-job", type=int, default=0,
                    help="ffmpeg threads per encode (0 = auto: cores // jobs, "
                         "so total threads never exceed core count)")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm-over-gb", type=float, default=3.0)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--skip-done", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--update-report", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--replace", action="store_true",
                    help="Replace originals with verified encodes (backup kept)")
    ap.add_argument("--backup-dir", type=Path, default=None,
                    help="Where replaced originals go; default is a .bak "
                         "file alongside the original")
    ap.add_argument("--purge-backup", action="store_true",
                    help="Delete the backup after a successful replace")
    ap.add_argument("--sample-quality", action="store_true",
                    help="Advisory SSIM/VMAF sample in verification (slow)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ERROR: ffmpeg/ffprobe not found on PATH.", file=sys.stderr)
        return 2
    if args.db is None and args.report is None:
        print("ERROR: pass --db state.db (preferred) or --report <scan json>.",
              file=sys.stderr)
        return 2
    if args.encoder == "nvenc" and args.preset not in NVENC_PRESETS + X265_PRESETS:
        print(f"ERROR: nvenc preset must be p1..p7 or an x265 name, not {args.preset!r}",
              file=sys.stderr)
        return 2
    ok, msg = encoder_available(args.encoder)
    if not ok:
        print(f"ERROR: encoder {args.encoder} unavailable: {msg}", file=sys.stderr)
        return 2
    if args.encoder in ("nvenc", "qsv"):
        _log(f"[note] {args.encoder}: {msg}. A build-level check cannot prove "
             f"a GPU is present; the first encode failure stops the run.")

    db: StateDB | None = None
    if args.db is not None:
        db = StateDB(args.db)
        recovered = db.mark_all_converting_interrupted()
        if recovered:
            _log(f"[recover] {recovered} job(s) left 'converting' by a "
                 f"killed run were marked interrupted and are retryable.")
        _install_signal_handlers(
            lambda: db.mark_all_converting_interrupted() if db else None)
    else:
        _install_signal_handlers(None)

    # ---- selection -------------------------------------------------------
    if db is not None:
        priority = {"high": "High Priority", "medium": "Medium",
                    "low": "Low"}.get(args.priority)
        # Claim up front (atomic per job in StateDB); workers then run
        # only claimed jobs, so a second process cannot claim them.
        work: list[dict] = []
        while True:
            if args.limit and len(work) >= args.limit:
                break
            job = db.claim_next(priority=priority,
                                min_savings_gb=args.min_savings_gb,
                                include_modern=args.include_modern)
            if job is None:
                break
            # Normalise DB column names to the report-row shape convert_one
            # already understands (it checks both, this is belt-and-braces).
            job["Path"] = job["path"]
            job["Filename"] = job["filename"]
            job["Video_Codec"] = job["video_codec"]
            job["Resolution"] = job["resolution"]
            job["Bit_Depth"] = job.get("bit_depth")
            job["Duration_Seconds"] = job["duration_seconds"]
            job["Size_GB"] = job["size_gb"]
            job["Estimated_Savings_GB"] = job["estimated_savings_gb"]
            job["Optimization_Candidate"] = job["candidate"]
            job["Source_Kind"] = job.get("source_kind")
            job["Source_Files"] = job.get("source_files")
            job["Video_TS_Dir"] = job.get("video_ts_dir")
            job["VTS_Number"] = job.get("vts_number")
            job["Sidecar_Files"] = job.get("sidecar_files")
            job["Field_Order"] = job.get("field_order")
            work.append(job)
        n_done = 0
    else:
        try:
            rows = load_report_file(args.report)  # type: ignore[arg-type]
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            if db is not None:
                db.close()
            return 2
        work, n_done = select_files(rows, args)

    # Resource sizing, decided once at startup (see resources.py).
    # This replaces the old hard-coded NVENC 1 -> 2 rule with a
    # detected plan, and it always divides threads across jobs.
    plan = recommend_resources(args.encoder, requested_jobs=args.jobs,
                               auto_jobs=getattr(args, "auto_jobs", False),
                               threads_per_job=getattr(args, "threads_per_job", 0))
    args.jobs = plan.jobs
    args.threads_per_job = plan.threads_per_job
    mem_txt = f"{plan.memory_gb:.1f} GiB RAM" if plan.memory_gb else "RAM unknown"
    _log(f"[resources] {plan.cores} cores, {mem_txt}, encoder {args.encoder}: "
         f"{plan.jobs} job(s) x {plan.threads_per_job} thread(s) "
         f"({plan.reason})")
    for w in plan.warnings:
        _log(f"[resources] WARNING: {w}")
    if not work:
        print("No files match the selection criteria. Nothing to do."
              + (f" ({n_done} already converted)" if n_done else ""))
        if db is not None:
            db.close()
        return 0

    est_total = sum(r.get("Estimated_Savings_GB") or 0.0 for r in work)
    print(f"Selected {len(work)} file(s), est. total savings "
          f"{_fmt_bytes(est_total * 1024 ** 3)} "
          f"(encoder {args.encoder}, {quality_label(args.encoder)} {args.crf}, "
          f"preset {args.preset}, replace={'on' if args.replace else 'off'})")
    if args.dry_run:
        for i, r in enumerate(work, 1):
            print(f"  {i:>3}. {_fmt_bytes((r.get('Estimated_Savings_GB') or 0) * 1024 ** 3):>10} est. save | "
                  f"{_fmt_bytes((r.get('Size_GB') or 0) * 1024 ** 3):>10} | "
                  f"{r.get('Video_Codec') or '?'} {r.get('Resolution') or ''} | "
                  f"{r.get('Filename') or r.get('Path')}")
        if db is not None:
            # Dry run claimed jobs; release them back to pending.
            for r in work:
                db.set_status(str(r["Path"]), "pending", "dry-run (released)")
            db.close()
        return 0

    work, user_skipped = confirm_large_files(work, args)
    logs: list[dict] = []
    for r, reason in user_skipped:
        entry = {c: None for c in LOG_COLUMNS}
        entry.update({
            "Input_Path": str(r.get("Path")),
            "Status": "skipped_user" if reason == "user" else "skipped_no_tty",
            "Status_Detail": "skipped at size-confirm prompt" if reason == "user"
                             else "large file, no TTY (pass --yes)",
        })
        logs.append(entry)
        if db is not None:
            db.set_status(str(r["Path"]), entry["Status"], entry["Status_Detail"])

    if work:
        bar = tqdm(total=len(work), desc="Converting", unit="file") if _HAS_TQDM else None
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futs = {pool.submit(convert_one, r, args, db): r for r in work}
            for fut in as_completed(futs):
                if _SHUTDOWN.is_set() and done == 0:
                    pass  # still drain already-running jobs' cleanups
                try:
                    entry = fut.result()
                except Exception as exc:  # last-resort guard
                    r = futs[fut]
                    entry = {c: None for c in LOG_COLUMNS}
                    entry.update({"Input_Path": str(r.get("Path")),
                                  "Status": "failed",
                                  "Status_Detail": f"unexpected: {exc}"})
                    if db is not None:
                        db.set_status(str(r["Path"]), "failed", str(exc))
                logs.append(entry)
                done += 1
                if bar is not None:
                    bar.update(1)
                else:
                    s = entry["Status"]
                    extra = ""
                    if s in ("converted", "converted_no_subs"):
                        saved_b = ((entry.get("Original_Bytes") or 0)
                                   - (entry.get("New_Bytes") or 0))
                        extra = (f" saved {_fmt_bytes(saved_b)} "
                                 f"({entry['Actual_Savings_Pct']}%) in "
                                 f"{_fmt_seconds(entry['Elapsed_Seconds'] or 0)}")
                    _log(f"[{done}/{len(work)}] {s}{extra} :: {entry['Input_Path']}")
                if _SHUTDOWN.is_set():
                    # Do not start new work; as_completed still drains
                    # jobs already running so their staging is cleaned.
                    pass
        if bar is not None:
            bar.close()

    log_dir = args.out_dir if not args.replace else Path("logs")
    if logs:
        csv_path, json_path = write_log(logs, log_dir)
        _log(f"\nConversion log CSV:  {csv_path}")
        _log(f"Conversion log JSON: {json_path}")
    if args.report is not None and args.update_report:
        update_report_json(args.report, logs)
    print_summary(logs)
    if not args.replace:
        print("\nOriginals were left untouched. Verify a few outputs, then "
              "re-run with --replace (backups kept) when you are happy.")
    if db is not None:
        db.close()
    return 0 if not _SHUTDOWN.is_set() else 130


if __name__ == "__main__":
    sys.exit(main())
