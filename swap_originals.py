#!/usr/bin/env python3
"""Swap already-encoded HEVC files into place, without re-encoding.

Why this script exists: ``hevc_convert.py --replace`` only replaces
while it encodes. Once a run has finished with ``replace=off`` (the
default), its outputs sit in ``--out-dir`` and the report checklist
says ``converted``. Re-running the converter ``--replace`` then does
the wrong thing either way:

  * with the default ``--skip-done`` it selects 0 files and does
    nothing (every row already says ``converted``);
  * with ``--no-skip-done`` it re-encodes everything from scratch --
    another ~10 hours for the 19-file Warhammer set that produced
    this script (2026-10-26).

This script does the swap the converter cannot: for each file the
report / conversion log says is already converted, it verifies the
existing encode again, backs the original up, and atomically swaps
the encode into the original's path. No ffmpeg encode runs here.

Safety model (mirrors the converter's ``--replace`` path):

  * Nothing swaps without ``--yes``. Without ``--yes`` the script
    prints the plan and exits 2; ``--dry-run`` prints the plan and
    changes nothing. Swap ONE file first (``--only <name>``), check
    it in Plex/Jellyfin, then run the rest.
  * Every pair is verified BEFORE any move, with the same hard
    gate as ``quality_check.verify`` (HEVC stream, duration,
    audio count/codec/channels, full decode, not suspiciously
    small). ``--verify-mode fast`` skips only the full-decode
    pass; ``--verify-mode none`` trusts the encode-time verify
    and checks existence + size only (not recommended).
  * The pairing is never guessed from filenames alone: it comes
    from the report's ``Output_Path`` (written by the converter)
    and/or the conversion log's ``Input_Path`` / ``Output_Path``.
    If the two sources disagree about a file's output, that file
    is skipped, never swapped. Filename inference
    (``<stem>.hevc<ext>`` in ``--converted-dir``) is off unless
    ``--allow-inferred`` is passed, and then only when exactly
    one candidate exists -- this is what keeps a stale CRF 18
    output from being swapped in over a CRF 24 ``_2`` file.
  * Larger encodes are skipped unless ``--allow-larger``: swapping
    in a file bigger than the original defeats the point and is
    how a wrong pairing would show up.
  * The swap itself stages a COPY of the encode next to the
    original (same filesystem, so the final ``os.replace`` is
    atomic even when the converted tree is on another drive --
    the Warhammer originals live on F: and the converted tree
    on C:). Then: original -> backup (``.bak`` alongside, or
    ``--backup-dir``), staged copy -> original path, original
    timestamps/permissions restored onto the new file. If the
    publish fails, the backup is rolled back and the job is
    reported failed -- the original is never left missing by a
    failed publish, and a staging file is never published
    unverified.
  * Backups are NEVER deleted by this script. Space is NOT freed
    by swapping: backups duplicate the originals until you
    delete the ``.bak`` files yourself, after checking the
    swapped files in your player. A free-space preflight aborts
    before any swap if the backup drive cannot hold all backups
    plus one staging copy.
  * ``--recover`` undoes an interrupted swap: if an original is
    missing but its backup exists, the backup is restored and
    any orphan staging file is removed. It never publishes
    anything.
  * DVD jobs are never swapped: their report ``Path`` is a
    synthetic VIDEO_TS key, not a file. Camcorder jobs are flat
    files and swap like any other; their exiftool sidecar copies
    next to the converted file are NOT moved (only the
    ``.conversion.json`` sidecar is copied alongside the
    original) -- check those by hand if they matter to you.

Library paths do not change (same filename, same folder), so
Plex/Jellyfin only needs a library scan to pick up the new
codec; nothing needs re-matching.

Usage:
    # Preview (changes nothing):
    python swap_originals.py --report reports/video_optimization_report.json --dry-run
    # Swap one file, then check it in your player:
    python swap_originals.py --report reports/video_optimization_report.json --only "Goblins" --yes
    # Swap the rest:
    python swap_originals.py --report reports/video_optimization_report.json --yes
    # Backups to another folder (must have room for all originals):
    python swap_originals.py --report reports/video_optimization_report.json --backup-dir "F:\\backups" --yes

Exit codes: 0 = every selected file swapped or cleanly skipped,
1 = at least one file failed verification/swap (others may have
swapped; see swap_log.json), 2 = usage / preflight / no --yes,
130 = interrupted (files already swapped stay swapped; re-run
the same command to resume -- swapped files are skipped because
their converted file is now the original path, and --recover
restores any file left mid-swap).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import atomic_write_json as _atomic_write_json  # noqa: E402
from common import fmt_bytes as _fmt_bytes  # noqa: E402
from common import log_msg as _log  # noqa: E402
from quality_check import DURATION_TOL_FRAC as _DURATION_TOL_FRAC  # noqa: E402
from quality_check import DURATION_TOL_S as _DURATION_TOL_S  # noqa: E402
from quality_check import MIN_SIZE_FRAC as _MIN_SIZE_FRAC  # noqa: E402
from quality_check import probe as probe_media  # noqa: E402
from quality_check import verify as verify_files  # noqa: E402
from state_manager import load_report_file  # noqa: E402

__version__ = "1.0.0"

CONVERTED_STATUSES = ("converted", "converted_no_subs")
SWAP_LOG_COLUMNS = [
    "Original_Path", "Converted_Path", "Backup_Path", "Status",
    "Status_Detail", "Original_Bytes", "Converted_Bytes",
    "Actual_Savings_GB",
]

_SHUTDOWN = threading.Event()


def _install_signal_handlers() -> None:
    def handler(signum: int, _frame: Any) -> None:
        _SHUTDOWN.set()
        _log(f"\n[shutdown] signal {signum}: stopping after the current file...")
    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except ValueError:
        pass  # not the main thread (tests)


def _key(path_str: str) -> str:
    # Dedup key: the path as written, normalised for case/separators
    # only -- resolving would fail for moved files and would hide a
    # genuine disagreement between report and log spellings.
    return os.path.normcase(os.path.normpath(str(path_str)))


def _is_dvd_row(row: dict) -> bool:
    if (row.get("Source_Kind") or row.get("source_kind")) == "dvd":
        return True
    if row.get("Source_Files") or row.get("source_files"):
        return True
    return False


# ---------------------------------------------------------------------------
# Pairing: report and/or conversion log. Never guess when sources disagree.
# ---------------------------------------------------------------------------
def _inferred_candidate(original: Path, converted_dir: Path,
                        scan_root: Path | None) -> Path | None:
    """Exactly-one ``<stem>.hevc`` candidate, or None (0 or >1 matches).

    Only used with --allow-inferred. Mirrors the converter's
    build_output_path flat form, and its --scan-root mirrored form.
    """
    candidates: list[Path] = []
    if scan_root is not None:
        try:
            rel = original.resolve().relative_to(scan_root.resolve())
            mirrored = converted_dir / rel.parent / f"{original.stem}.hevc{original.suffix}"
            if mirrored.is_file():
                candidates.append(mirrored)
        except (ValueError, OSError):
            pass
    flat = converted_dir / f"{original.stem}.hevc{original.suffix}"
    if flat.is_file() and flat not in candidates:
        candidates.append(flat)
    numbered = sorted(converted_dir.glob(f"{original.stem}.hevc_*{original.suffix}"))
    candidates.extend(p for p in numbered if p.is_file())
    if len(candidates) == 1:
        return candidates[0]
    return None


def load_pairs(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    """Return (pairs, skipped) where a pair is an original/converted mapping.

    Skipped entries are pairing-level rejections (not a file, DVD,
    not converted, no output recorded, sources disagree) -- they are
    reported, never swapped, and never silently dropped.
    """
    by_key: dict[str, dict] = {}
    order: list[str] = []
    skipped: list[dict] = []

    def note_skip(original: str, converted: str, detail: str) -> None:
        skipped.append({"Original_Path": original, "Converted_Path": converted,
                        "Backup_Path": "", "Status": "skipped",
                        "Status_Detail": detail, "Original_Bytes": None,
                        "Converted_Bytes": None, "Actual_Savings_GB": None})

    def add(source: str, original: str, converted: str,
            expected: int | None, status: str) -> None:
        key = _key(original)
        if key in by_key:
            prev = by_key[key]
            if _key(prev["converted"]) != _key(converted):
                # Sources disagree about which file is the encode:
                # do not guess. Remove the pair entirely.
                prev["conflict"] = (
                    f"report/log disagree on output: {prev['converted']} "
                    f"({prev['source']}) vs {converted} ({source})")
                prev["converted"] = ""
            else:
                prev["expected"] = expected if prev.get("expected") is None else prev["expected"]
                prev["source"] = f"{prev['source']}+{source}"
            return
        by_key[key] = {"original": original, "converted": converted,
                       "expected": expected, "source": source,
                       "status": status}
        order.append(key)

    if args.report is not None:
        try:
            rows = load_report_file(args.report)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: could not read report {args.report}: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        for row in rows:
            if not isinstance(row, dict):
                continue
            original = str(row.get("Path") or row.get("path") or "")
            if not original:
                continue
            if row.get("Error"):
                note_skip(original, "", f"scan error recorded: {row.get('Error')}")
                continue
            if _is_dvd_row(row):
                note_skip(original, "", "DVD job: original is a VIDEO_TS directory, not a file (swap is refused)")
                continue
            status = str(row.get("Conversion_Status") or "")
            output = str(row.get("Output_Path") or "")
            if status not in CONVERTED_STATUSES:
                continue  # not yet converted: not this script's work
            if not output:
                if args.allow_inferred:
                    cand = _inferred_candidate(Path(original), args.converted_dir, args.scan_root)
                    if cand is not None:
                        add("report(inferred)", original, str(cand), None, status)
                    else:
                        note_skip(original, "", "no Output_Path recorded and no unique <stem>.hevc candidate in --converted-dir (pass --log with the conversion log)")
                else:
                    note_skip(original, "", "no Output_Path recorded in report (pass --log with the conversion log, or --allow-inferred)")
                continue
            # The report does not record New_Bytes; the log does. Size
            # cross-checks happen from the log when both are given.
            add("report", original, output, None, status)

    if args.log is not None:
        if not args.log.is_file():
            print(f"ERROR: conversion log not found: {args.log}", file=sys.stderr)
            raise SystemExit(2)
        try:
            entries = json.loads(args.log.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: could not read conversion log {args.log}: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        if not isinstance(entries, list):
            print(f"ERROR: conversion log {args.log} is not a JSON list.", file=sys.stderr)
            raise SystemExit(2)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            original = str(entry.get("Input_Path") or "")
            output = str(entry.get("Output_Path") or "")
            status = str(entry.get("Status") or "")
            if not original or status not in CONVERTED_STATUSES or not output:
                continue
            expected = entry.get("New_Bytes")
            try:
                expected = int(expected) if expected not in (None, "") else None
            except (TypeError, ValueError):
                expected = None
            add("log", original, output, expected, status)

    pairs: list[dict] = []
    for key in order:
        pair = by_key[key]
        if pair.get("conflict"):
            note_skip(pair["original"], pair["converted"], pair["conflict"])
            continue
        if not pair["converted"]:
            note_skip(pair["original"], "", "no converted file recorded")
            continue
        pairs.append(pair)

    # Filters: --only matches filename or any substring of the
    # original path (case-insensitive); --exclude removes matches.
    def _matches(original: str, converted: str, patterns: list[str]) -> bool:
        hay = f"{original} {converted} {Path(original).name}".lower()
        return any(p.lower() in hay for p in patterns)

    if args.only:
        kept = [p for p in pairs if _matches(p["original"], p["converted"], args.only)]
        for p in pairs:
            if p not in kept:
                note_skip(p["original"], p["converted"], "excluded by --only filter")
        pairs = kept
    if args.exclude:
        kept = [p for p in pairs if not _matches(p["original"], p["converted"], args.exclude)]
        for p in pairs:
            if p not in kept:
                note_skip(p["original"], p["converted"], "excluded by --exclude filter")
        pairs = kept
    if args.limit and args.limit > 0:
        for p in pairs[args.limit:]:
            note_skip(p["original"], p["converted"], "beyond --limit")
        pairs = pairs[: args.limit]
    return pairs, skipped


# ---------------------------------------------------------------------------
# Verification (full gate by default; fast/none only on explicit request)
# ---------------------------------------------------------------------------
def _fast_probe_check(original: Path, converted: Path) -> list[str]:
    """ffprobe-only checks: HEVC, duration, audio makeup, size tripwire."""
    failures: list[str] = []
    try:
        o_info, n_info = probe_media(original), probe_media(converted)
    except RuntimeError as exc:
        return [str(exc)]
    o_v = [s for s in o_info.get("streams", []) if s.get("codec_type") == "video"]
    n_v = [s for s in n_info.get("streams", []) if s.get("codec_type") == "video"]
    if not n_v:
        failures.append("converted file has no video stream")
    elif str(n_v[0].get("codec_name") or "").lower() not in ("hevc", "h265"):
        failures.append(f"converted video codec is {n_v[0].get('codec_name')!r}, not HEVC")
    try:
        o_dur = float(o_info["format"]["duration"])
        n_dur = float(n_info["format"]["duration"])
        # Tolerances are imported from quality_check, not re-typed
        # here: a tolerance change must move in both gates at once.
        if abs(o_dur - n_dur) > max(_DURATION_TOL_S, _DURATION_TOL_FRAC * o_dur):
            failures.append(f"duration mismatch: original {o_dur:.1f}s vs converted {n_dur:.1f}s")
    except (KeyError, TypeError, ValueError):
        failures.append("could not read duration from original and/or converted file")
    o_a = [s for s in o_info.get("streams", []) if s.get("codec_type") == "audio"]
    n_a = [s for s in n_info.get("streams", []) if s.get("codec_type") == "audio"]
    if len(n_a) != len(o_a):
        failures.append(f"audio stream count changed: {len(o_a)} -> {len(n_a)}")
    else:
        for i, (oa, na) in enumerate(zip(o_a, n_a)):
            if str(oa.get("codec_name") or "").lower() != str(na.get("codec_name") or "").lower():
                failures.append(f"audio stream {i} codec changed: {oa.get('codec_name')} -> {na.get('codec_name')}")
            if oa.get("channels") != na.get("channels"):
                failures.append(f"audio stream {i} channel count changed: {oa.get('channels')} -> {na.get('channels')}")
    orig_size, new_size = original.stat().st_size, converted.stat().st_size
    if new_size == 0:
        failures.append("converted file is empty (0 bytes)")
    elif orig_size and new_size < _MIN_SIZE_FRAC * orig_size:
        failures.append(f"converted file suspiciously small: {new_size} bytes is < {_MIN_SIZE_FRAC:.0%} of original {orig_size} bytes")
    return failures


def verify_pair(original: Path, converted: Path, args: argparse.Namespace
                ) -> tuple[bool, list[str], list[str]]:
    """Return (passed, failures, warnings) for one pair, never raising."""
    if args.verify_mode == "none":
        return True, [], ["verify skipped (--verify-mode none): trusting the encode-time check"]
    if args.verify_mode == "fast":
        failures = _fast_probe_check(original, converted)
        return (not failures), failures, []
    result = verify_files(original, converted, strict=args.strict,
                          fail_if_larger=args.fail_if_larger)
    return result.passed, list(result.failures), list(result.warnings)


# ---------------------------------------------------------------------------
# Swap: stage a copy next to the original, backup, atomic publish, rollback
# ---------------------------------------------------------------------------
def _staging_path(original: Path) -> Path:
    return original.parent / f".{original.stem}.swap-part{original.suffix}"


def _backup_path(original: Path, backup_dir: Path | None) -> Path:
    if backup_dir is not None:
        backup = backup_dir / original.name
        n = 0
        while backup.exists():
            n += 1
            backup = backup_dir / f"{original.stem}_{n}{original.suffix}"
        return backup
    backup = original.with_suffix(original.suffix + ".bak")
    n = 0
    while backup.exists():
        n += 1
        backup = original.with_name(f"{original.name}.{n}.bak")
    return backup


def _move_file(src: Path, dst: Path) -> None:
    """Move a file, falling back to copy+delete across filesystems.

    ``os.replace`` is atomic but fails with EXDEV across drives — the
    exact case ``--backup-dir`` on another drive creates. Every move
    in this script (backup, publish rollback, recovery restore) must
    go through here, never a bare ``os.replace``, or the rollback
    fails in precisely the configuration the backup was meant to
    protect. The one exception is the publish itself, which is
    deliberately ``os.replace`` on a same-directory staging copy.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as exc:
        if getattr(exc, "errno", None) == 18:  # EXDEV: cross-filesystem
            shutil.move(str(src), str(dst))
        else:
            raise


def _move_to_backup(original: Path, backup: Path) -> None:
    _move_file(original, backup)


def swap_one(pair: dict, args: argparse.Namespace) -> dict:
    """Swap one verified pair. Never raises; returns a swap-log entry."""
    original = Path(pair["original"])
    converted = Path(pair["converted"])
    entry: dict = {"Original_Path": str(original), "Converted_Path": str(converted),
                   "Backup_Path": "", "Status": "failed", "Status_Detail": "",
                   "Original_Bytes": None, "Converted_Bytes": None,
                   "Actual_Savings_GB": None}

    def finish(status: str, detail: str, **extra: Any) -> dict:
        entry["Status"] = status
        entry["Status_Detail"] = detail
        entry.update(extra)
        return entry

    if _SHUTDOWN.is_set():
        return finish("interrupted", "shutdown before start")
    if not original.is_file():
        return finish("failed", f"original missing: {original}")
    if not converted.is_file():
        return finish("failed", f"converted file missing: {converted}")
    try:
        if original.resolve() == converted.resolve():
            return finish("skipped", "converted file is already at the original path (already swapped)")
    except OSError:
        pass
    orig_stat = original.stat()
    orig_bytes, conv_bytes = orig_stat.st_size, converted.stat().st_size
    entry["Original_Bytes"] = orig_bytes
    entry["Converted_Bytes"] = conv_bytes
    entry["Actual_Savings_GB"] = round((orig_bytes - conv_bytes) / 1024 ** 3, 3)
    expected = pair.get("expected")
    if expected is not None and conv_bytes != expected:
        return finish("failed",
                      f"converted size {conv_bytes} != size recorded at encode time "
                      f"{expected} (file changed, or wrong file -- not swapping)")
    if conv_bytes > orig_bytes and not args.allow_larger:
        return finish("skipped_larger",
                      f"converted file is larger than the original "
                      f"({_fmt_bytes(conv_bytes)} > {_fmt_bytes(orig_bytes)}); "
                      f"pass --allow-larger to swap it anyway")

    passed, failures, warnings = verify_pair(original, converted, args)
    if not passed:
        return finish("failed_verify", "; ".join(failures))
    detail = "; ".join(warnings) if warnings else "verified"
    if args.verify_mode == "full":
        detail = ("verified (full gate incl. decode)" +
                  (f"; warnings: {'; '.join(warnings)}" if warnings else ""))

    staging = _staging_path(original)
    backup = _backup_path(original, args.backup_dir)
    # Backup_Path is only recorded once the backup actually exists
    # (below); a failed swap must not claim a backup that was never made.
    # Stage a copy next to the original: the publish is then a same-
    # filesystem os.replace even when converted/ lives on another
    # drive. A stale staging file from a killed run is removed --
    # it was never published, and the original is still in place.
    try:
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            staging.unlink()
        except FileNotFoundError:
            pass
        shutil.copyfile(converted, staging)
        if staging.stat().st_size != conv_bytes:
            staging.unlink(missing_ok=True)
            return finish("failed", "staging copy size does not match the converted file (copy aborted, original untouched)")
        # A truncated copy would pass a size check only if it were
        # exactly the right size; probe the staged bytes too, so a
        # bad copy cannot be published on size alone.
        staged_failures = _fast_probe_check(original, staging)
        if staged_failures:
            staging.unlink(missing_ok=True)
            return finish("failed", f"staged copy failed probe: {staged_failures[0]} (original untouched)")
    except OSError as exc:
        try:
            staging.unlink()
        except OSError:
            pass
        return finish("failed", f"could not stage copy next to original: {exc} (original untouched)")

    # A signal received during staging must not publish: the user
    # asked to stop, and the original is still untouched at this
    # point, so stopping here is free. (The same re-check exists in
    # the converter between verification and publish.)
    if _SHUTDOWN.is_set():
        staging.unlink(missing_ok=True)
        return finish("interrupted", "shutdown during staging (original untouched)")
    # Publish: backup, then atomic replace. Roll back on any failure
    # between the two replaces so the original path is never left
    # missing by this script. The rollback goes through _move_file,
    # not a bare os.replace: a cross-filesystem backup would make
    # a bare replace fail with EXDEV and strand the original at
    # the backup path.
    try:
        _move_to_backup(original, backup)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        return finish("failed", f"could not move original to backup {backup}: {exc} (original untouched)")
    entry["Backup_Path"] = str(backup)
    try:
        os.replace(staging, original)
    except OSError as exc:
        try:
            _move_file(backup, original)
            staging.unlink(missing_ok=True)
            return finish("failed", f"publish failed ({exc}); original rolled back from backup")
        except OSError as rollback_exc:
            staging.unlink(missing_ok=True)
            return finish("failed",
                          f"publish failed ({exc}) AND rollback failed ({rollback_exc}): "
                          f"original is at backup {backup} -- "
                          f"restore it by hand or run --recover")
    # Preserve the original's timestamps/permissions on the new file,
    # as the converter's --replace path does. A failure here warns
    # only: the swap itself already succeeded.
    try:
        os.chmod(original, orig_stat.st_mode)
        os.utime(original, (orig_stat.st_atime, orig_stat.st_mtime))
    except OSError as exc:
        detail += f"; [WARN] could not restore timestamps/permissions: {exc}"

    # Sidecar provenance: copy <converted>.conversion.json alongside
    # the original as <original>.conversion.json. Never overwrite an
    # existing sidecar at the destination.
    sidecar_src = converted.with_name(converted.name + ".conversion.json")
    if sidecar_src.is_file():
        sidecar_dst = original.parent / f"{original.name}.conversion.json"
        try:
            if not sidecar_dst.exists():
                shutil.copy2(sidecar_src, sidecar_dst)
                detail += "; sidecar copied"
            else:
                detail += "; sidecar NOT copied (one already exists at the destination)"
        except OSError as exc:
            detail += f"; [WARN] sidecar copy failed: {exc}"
        if args.remove_converted:
            try:
                sidecar_src.unlink()
            except OSError:
                pass
    if args.remove_converted:
        try:
            converted.unlink()
            detail += "; converted source removed (--remove-converted)"
        except OSError as exc:
            detail += f"; [WARN] could not remove converted source: {exc}"
    return finish("swapped", detail)


# ---------------------------------------------------------------------------
# Recovery: restore backups for originals left missing by an interrupted run
# ---------------------------------------------------------------------------
def _backup_candidates(original: Path, backup_dir: Path | None) -> list[Path]:
    cands: list[Path] = []
    dirs = [original.parent]
    if backup_dir is not None and backup_dir not in dirs:
        dirs.append(backup_dir)
    for d in dirs:
        base = d / (original.name + ".bak")
        if base.is_file():
            cands.append(base)
        n = 1
        while True:
            numbered = d / f"{original.name}.{n}.bak"
            if numbered.is_file():
                cands.append(numbered)
                n += 1
            else:
                break
        # The backup-dir numbered form (Foo_1.mkv) is only looked for
        # in --backup-dir: in the original's own folder a Foo_1.mkv
        # could be a different, legitimate file.
        if backup_dir is not None and d == backup_dir:
            stemmed = sorted(d.glob(f"{original.stem}_*{original.suffix}"))
            cands.extend(p for p in stemmed if p.is_file())
    # De-duplicate, preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for c in cands:
        if str(c) not in seen:
            seen.add(str(c))
            out.append(c)
    return out


def recover_pairs(pairs: list[dict], args: argparse.Namespace) -> list[dict]:
    results: list[dict] = []
    for pair in pairs:
        original = Path(pair["original"])
        staging = _staging_path(original)
        entry = {"Original_Path": str(original), "Converted_Path": pair["converted"],
                 "Backup_Path": "", "Status": "skipped",
                 "Status_Detail": "", "Original_Bytes": None,
                 "Converted_Bytes": None, "Actual_Savings_GB": None}
        if original.is_file():
            if staging.is_file():
                # A plan run must not delete anything, even an orphan:
                # staging files are only removed by a real recovery.
                if args.dry_run or not args.yes:
                    entry["Status_Detail"] = "original present; orphan staging file would be removed"
                else:
                    try:
                        staging.unlink()
                        entry["Status_Detail"] = "original present; orphan staging file removed"
                    except OSError as exc:
                        entry["Status_Detail"] = f"original present; could not remove orphan staging: {exc}"
            else:
                entry["Status_Detail"] = "original present; nothing to recover"
            results.append(entry)
            continue
        cands = _backup_candidates(original, args.backup_dir)
        if not cands:
            entry["Status_Detail"] = "original missing and no backup found -- nothing to restore (converted file still in --converted-dir; re-run the swap)"
            results.append(entry)
            continue
        if len(cands) > 1:
            entry["Status_Detail"] = ("original missing with multiple backups; refusing to guess: "
                                      + ", ".join(str(c) for c in cands)
                                      + " -- restore one by hand or re-run with --only")
            results.append(entry)
            continue
        backup = cands[0]
        entry["Backup_Path"] = str(backup)
        if args.dry_run:
            entry["Status"] = "recover_dry_run"
            entry["Status_Detail"] = f"would restore {backup} -> {original}" + (" and remove staging" if staging.is_file() else "")
            results.append(entry)
            continue
        if not args.yes:
            entry["Status_Detail"] = f"would restore {backup} -> {original} (pass --yes to recover)"
            results.append(entry)
            continue
        try:
            _move_file(backup, original)
            staging.unlink(missing_ok=True)
            entry["Status"] = "recovered"
            entry["Status_Detail"] = f"restored from backup {backup}"
        except OSError as exc:
            entry["Status"] = "failed"
            entry["Status_Detail"] = f"restore failed: {exc} (backup still at {backup})"
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Logs and report write-back (atomic: temp file + os.replace in place)
# ---------------------------------------------------------------------------
def write_swap_log(logs: list[dict], out_path: Path) -> Path:
    """Write the CSV log to *out_path* and a JSON twin alongside it.

    If the user names a .json file, the CSV goes to the .csv twin
    instead, so the two formats never overwrite each other.
    Returns the JSON path.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".json":
        csv_path, json_path = out_path.with_suffix(".csv"), out_path
    else:
        csv_path, json_path = out_path, out_path.with_suffix(".json")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=SWAP_LOG_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for l in logs:
            w.writerow({c: ("" if l.get(c) is None else l.get(c)) for c in SWAP_LOG_COLUMNS})
    _atomic_write_json(json_path, [{k: (v if v is not None else "") for k, v in l.items()} for l in logs])
    return json_path


def update_report(report: Path, logs: list[dict]) -> None:
    """Record Swap_Status / backup on the rows that swapped. JSON only."""
    if report.suffix.lower() != ".json":
        _log(f"[WARN] Report {report} is not JSON; swap results not written back "
             f"(see swap_log.json).")
        return
    try:
        rows = load_report_file(report)
    except (OSError, json.JSONDecodeError) as exc:
        _log(f"[WARN] Could not update report ({exc}); swap results are in swap_log.json.")
        return
    by_path = {r.get("Path"): r for r in rows if isinstance(r, dict)}
    now = datetime.now(timezone.utc).isoformat()
    touched = 0
    for l in logs:
        if l.get("Status") != "swapped":
            continue
        r = by_path.get(l.get("Original_Path") or "")
        if r is None:
            continue
        r["Swap_Status"] = "swapped"
        r["Swapped_At_UTC"] = now
        r["Backup_Path"] = l.get("Backup_Path") or ""
        touched += 1
    if not touched:
        return
    _atomic_write_json(report, rows)
    _log(f"Report swap checklist updated: {report}")


def print_summary(logs: list[dict]) -> None:
    swapped = [l for l in logs if l["Status"] == "swapped"]
    recovered = [l for l in logs if l["Status"] == "recovered"]
    failed = [l for l in logs if l["Status"] in ("failed", "failed_verify", "interrupted")]
    skipped = [l for l in logs if str(l["Status"]).startswith("skipped")]
    orig_b = sum(l.get("Original_Bytes") or 0 for l in swapped)
    new_b = sum(l.get("Converted_Bytes") or 0 for l in swapped)
    saved_b = orig_b - new_b
    print("\n==== Swap summary ====")
    line = f"Swapped: {len(swapped)}   Skipped: {len(skipped)}   Failed: {len(failed)}"
    if recovered:
        line += f"   Recovered: {len(recovered)}"
    print(line)
    if swapped:
        print(f"Original size:  {_fmt_bytes(orig_b)}")
        print(f"New size:       {_fmt_bytes(new_b)}")
        pct = f" ({100 * saved_b / orig_b:.0f}% saved)" if orig_b else ""
        print(f"Size difference:{_fmt_bytes(saved_b)}{pct}")
        print("Backups kept: no space is freed until you delete the .bak "
              "files yourself, after checking the swapped files in "
              "Plex/Jellyfin (then trigger a library scan).")
    for l in failed:
        print(f"  {l['Status'].upper()}: {l['Original_Path']}\n"
              f"          {l['Status_Detail']}")
    for l in skipped:
        if l["Status"] != "skipped" or "already" in str(l["Status_Detail"]):
            print(f"  SKIPPED: {l['Original_Path']} -- {l['Status_Detail']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Swap already-encoded HEVC files into place (no re-encoding).")
    ap.add_argument("--report", type=Path, default=None,
                    help="Scan report JSON (converter checklist: Conversion_Status + Output_Path)")
    ap.add_argument("--log", type=Path, default=None,
                    help="Conversion log JSON from hevc_convert.py (Input_Path/Output_Path/New_Bytes)")
    ap.add_argument("--converted-dir", type=Path, default=Path("converted"),
                    help="Where converted files live (only used for --allow-inferred pairing)")
    ap.add_argument("--scan-root", type=Path, default=None,
                    help="Scan root the converter mirrored into --converted-dir (inference only)")
    ap.add_argument("--backup-dir", type=Path, default=None,
                    help="Where backups go; default is a .bak file alongside each original")
    ap.add_argument("--swap-log", type=Path, default=Path("swap_log.csv"),
                    help="Swap log CSV (a .json twin is written alongside)")
    ap.add_argument("--allow-inferred", action="store_true",
                    help="Allow <stem>.hevc pairing when no Output_Path is recorded, "
                         "only if exactly one candidate exists")
    ap.add_argument("--allow-larger", action="store_true",
                    help="Swap in encodes larger than their original (default: skip them)")
    ap.add_argument("--verify-mode", choices=["full", "fast", "none"], default="full",
                    help="full = same hard gate incl. decode (default); fast = ffprobe only; "
                         "none = trust the encode-time check")
    ap.add_argument("--strict", action="store_true",
                    help="Tighter duration tolerance in the full verify gate (0.5s / 0.2%%)")
    ap.add_argument("--fail-if-larger", action="store_true",
                    help="Turn a larger encode into a verify failure instead of a skip "
                         "(only with --allow-larger; otherwise larger files are skipped first)")
    ap.add_argument("--remove-converted", action="store_true",
                    help="Delete the converted source file after a successful swap "
                         "(default: keep it as a second copy)")
    ap.add_argument("--only", action="append", default=[],
                    help="Only swap files whose name/path contains this text (repeatable)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="Skip files whose name/path contains this text (repeatable)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan; change nothing")
    ap.add_argument("--yes", action="store_true",
                    help="Actually swap (required; without it, only the plan is printed)")
    ap.add_argument("--update-report", action=argparse.BooleanOptionalAction, default=True,
                    help="Write Swap_Status/Backup_Path back into a JSON report")
    ap.add_argument("--recover", action="store_true",
                    help="Restore originals left missing by an interrupted swap; never publishes")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.report is None and args.log is None:
        print("ERROR: pass --report <scan json> and/or --log <conversion_log.json>.",
              file=sys.stderr)
        return 2
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ERROR: ffmpeg/ffprobe not found on PATH (verification needs them).",
              file=sys.stderr)
        return 2
    _install_signal_handlers()

    pairs, skipped = load_pairs(args)
    if not pairs and not skipped:
        print("No converted files found in the report/log. Nothing to do.")
        return 0
    if not pairs:
        print("No files eligible to swap; all pairings were skipped:")
        for s in skipped:
            print(f"  {s['Original_Path']} -- {s['Status_Detail']}")
        return 0

    # Free-space preflight (real runs and recoveries that move files):
    # backups duplicate every original, plus one staging copy at a
    # time. Abort before ANY swap if the backup drive cannot hold it.
    if not args.dry_run:
        backup_target = args.backup_dir or Path(pairs[0]["original"]).parent
        if args.backup_dir is not None:
            try:
                args.backup_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"ERROR: cannot create --backup-dir {args.backup_dir}: {exc}",
                      file=sys.stderr)
                return 2
        try:
            total_orig = sum(Path(p["original"]).stat().st_size
                             for p in pairs if Path(p["original"]).is_file())
            largest = max((Path(p["converted"]).stat().st_size
                           for p in pairs if Path(p["converted"]).is_file()),
                          default=0)
            free = shutil.disk_usage(backup_target).free
            needed = total_orig + largest
            if free < needed:
                print(f"ERROR: not enough free space for backups on {backup_target}: "
                      f"need {_fmt_bytes(needed)} (all originals as backups + one staging copy), "
                      f"have {_fmt_bytes(free)}. Free space or pass --backup-dir on a roomier drive. "
                      f"No files were swapped.", file=sys.stderr)
                return 2
        except OSError as exc:
            print(f"ERROR: free-space preflight failed: {exc}", file=sys.stderr)
            return 2

    print(f"Selected {len(pairs)} file(s) to swap "
          f"(verify-mode={args.verify_mode}, "
          f"backup={'--backup-dir ' + str(args.backup_dir) if args.backup_dir else '.bak alongside original'}, "
          f"remove-converted={'on' if args.remove_converted else 'off'})")
    for i, p in enumerate(pairs, 1):
        print(f"  {i:>3}. {p['original']}  <-  {p['converted']}  ({p['source']})")
    if skipped:
        print(f"  ({len(skipped)} pairing(s) skipped:)")
        for s in skipped:
            print(f"    - {s['Original_Path']} -- {s['Status_Detail']}")

    # Recovery obeys the same gates as swapping: a dry run (or a run
    # without --yes) prints the plan and writes NO log file, and a
    # real recovery reports failure with exit 1 instead of hiding
    # it behind exit 0. Previously this branch ran before the
    # dry-run guard, so `--recover --dry-run` mutated swap_log and
    # `--recover` always exited 0 even when a restore failed.
    if args.recover:
        logs = recover_pairs(pairs, args) + skipped
        for l in logs:
            print(f"[{l['Status']}] {l['Original_Path']} -- {l['Status_Detail']}")
        print_summary(logs)
        if args.dry_run or not args.yes:
            print("\n[dry-run] No files were changed." if args.dry_run
                  else "\nNo files were changed: pass --yes to recover.")
            return 0 if args.dry_run else 2
        write_swap_log(logs, args.swap_log)
        failed = [l for l in logs if l["Status"] in ("failed", "failed_verify")]
        return 1 if failed else 0

    if args.dry_run:
        print("\n[dry-run] No files were changed. Pass --yes (without --dry-run) to swap.")
        return 0
    if not args.yes:
        print("\nNo files were changed: pass --yes to swap, or --dry-run to preview only.")
        return 2

    logs: list[dict] = list(skipped)
    for pair in pairs:
        if _SHUTDOWN.is_set():
            logs.append({"Original_Path": pair["original"], "Converted_Path": pair["converted"],
                         "Backup_Path": "", "Status": "interrupted",
                         "Status_Detail": "shutdown before start",
                         "Original_Bytes": None, "Converted_Bytes": None,
                         "Actual_Savings_GB": None})
            continue
        entry = swap_one(pair, args)
        _log(f"[{entry['Status']}] {pair['original']} -- {entry['Status_Detail']}")
        logs.append(entry)
    json_log = write_swap_log(logs, args.swap_log)
    csv_log = json_log.with_suffix(".csv") if args.swap_log.suffix.lower() == ".json" else args.swap_log
    _log(f"\nSwap log CSV:  {csv_log}")
    _log(f"Swap log JSON:  {json_log}")
    if args.report is not None and args.update_report:
        update_report(args.report, logs)
    print_summary(logs)
    failed = [l for l in logs if l["Status"] in ("failed", "failed_verify")]
    if _SHUTDOWN.is_set():
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
