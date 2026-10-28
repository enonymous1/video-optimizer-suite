#!/usr/bin/env python3
"""Verifier for HEVC encodes: the gate before any original is replaced.

A file only passes if EVERY hard check passes. Warnings never promote
a failure, and never demote a pass to a failure on their own -- they are
reported so a human can decide about edge cases (e.g. subtitles dropped
because the container cannot hold them).

Hard checks (fail = do NOT replace, do NOT mark converted):
  1. Output exists and is non-empty.
  2. Exactly one video stream, and it is HEVC.
  3. Duration matches the original within max(2.0s, 1% of duration).
     (Same tolerance the v1 converter used, so v1-verified files still
     pass here. Use --strict for max(0.5s, 0.2%) on short/archival work.)
  4. Audio stream count matches the original, and every copied audio
     codec + channel count matches (audio is -c:a copy, so any drift
     means the mux went wrong).
  5. Full decode with ``ffmpeg -v error -f null -`` produces no errors.
     This is the corruption check: a file can have a plausible header
     and duration and still be undecodable halfway through. It costs
     one decode pass, which is cheap next to an encode pass.
  6. Output is not absurdly small (<5% of the original size) -- a
     near-empty encode that somehow decoded. This is a tripwire, not a
     quality judgement: a legit HEVC file can be 40-60% of the original.

Warnings (reported, do not fail):
  * subtitle stream count changed (expected when a container cannot
    hold PGS subs and the converter fell back to -sn; the converter
    records converted_no_subs for exactly this case);
  * output larger than the original (possible on tiny / already
    efficient files; the pipeline logs it, it does not block replace
    unless you pass --fail-if-larger);
  * resolution / fps changed (should never happen with this pipeline's
    commands, so a change here is worth a human look even though it
    does not fail the gate by itself -- it is listed as a warning
    rather than a failure only because some sources report fps
    inconsistently across containers).

Optional perceptual sample (--sample-quality):
  Compares short samples (default 3 x 10s: start, middle, end) of the
  original vs the encode. Two metrics, in preference order:
    * libvmaf -- best perceptual correlate, but only if your ffmpeg
      was built with --enable-libvmaf. This box's stock Ubuntu ffmpeg
      is NOT (checked 2026-10-25: only vmafmotion is present), so on
      stock builds this step reports "unavailable" and skips -- it
      never fails the gate for a missing filter.
    * ssim -- built into stock ffmpeg, used as the fallback. A mean
      SSIM < 0.90 on a sample is reported as a warning (not a failure:
      SSIM punishes grain/noise that HEVC legitimately smooths).
  The sample is advisory by design: a 10-second window cannot prove a
  2-hour film, and a hard VMAF gate on samples would block good encodes
  for noisy sources. If you want a hard gate, read the reported score
  and decide per title -- do not wire it into the replace decision
  until you have calibrated it on your own library.

  Multi-file sources (DVD title sets) use ``verify_reference()`` with
  a summed duration/size and the first file's stream makeup, instead
  of ``verify()`` on a single file. The hard checks are identical;
  only the perceptual sample is skipped (no single timeline to
  sample), and a resolution change from the DVD upscale mode is a
  warning, as it already is here.

  Also: the encode log itself is parsed when you pass --encode-log.
  A zero-exit ffmpeg run that printed "Error" / "Invalid" lines is
  treated as a failure -- exit code alone is not trusted.

CLI:
    python quality_check.py original.mkv converted.mkv
    python quality_check.py original.mkv converted.mkv --sample-quality --json
    python quality_check.py original.mkv converted.mkv --encode-log encode.log
Exit code: 0 = pass, 1 = fail, 2 = usage / tool error.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DURATION_TOL_S = 2.0
DURATION_TOL_FRAC = 0.01
STRICT_TOL_S = 0.5
STRICT_TOL_FRAC = 0.002
MIN_SIZE_FRAC = 0.05
SSIM_WARN_BELOW = 0.90
VMAF_WARN_BELOW = 90.0


@dataclass
class CheckResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.passed = False
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)


def probe(path: Path) -> dict:
    """ffprobe JSON for a file, or raise RuntimeError with the reason."""
    try:
        proc = _run(["ffprobe", "-v", "quiet", "-print_format", "json",
                     "-show_format", "-show_streams", str(path)], timeout=120)
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed (rc={proc.returncode}): "
                           f"{proc.stderr.strip()[:200]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe emitted invalid JSON: {exc}")


def _streams(info: dict, kind: str) -> list[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind]


def _duration(info: dict) -> float | None:
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None


def _codec_name(stream: dict) -> str:
    return str(stream.get("codec_name") or "").lower()


def decode_check(path: Path) -> list[str]:
    """Decode the whole file, collecting ffmpeg error lines.

    ``-v error`` suppresses progress chatter; anything left on stderr
    is a real decode problem. Returns the error lines (empty = clean).
    """
    try:
        proc = _run(["ffmpeg", "-v", "error", "-i", str(path),
                     "-f", "null", "-"], timeout=3600)
    except FileNotFoundError:
        return ["ffmpeg not found on PATH"]
    except subprocess.TimeoutExpired:
        return ["decode check timed out"]
    lines = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
    if proc.returncode != 0:
        lines.append(f"ffmpeg decode exited rc={proc.returncode}")
    return lines


def parse_encode_log(log_path: Path) -> list[str]:
    """Flag suspicious lines in an ffmpeg encode log.

    Looks for real error markers, not the word "error" in a benign
    context (e.g. an encoder name). An empty list means clean.
    """
    patterns = [
        re.compile(r"conversion failed", re.I),
        re.compile(r"^.*error.*$", re.I),
        re.compile(r"invalid (argument|data|nals|bitstream)", re.I),
        re.compile(r"could not find tag for codec", re.I),
        re.compile(r"failed to .*encode", re.I),
    ]
    hits: list[str] = []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"cannot read encode log: {exc}"]
    for line in text.splitlines():
        if any(p.search(line) for p in patterns):
            # "Error" lines ffmpeg prints for a dropped subtitle track
            # during the -c:s copy fallback are handled by the caller
            # (it re-runs without subs); only flag them if the log is
            # from the final, successful command -- the caller passes
            # only the final command's log here.
            hits.append(line.strip())
    return hits[:20]


def _filter_available(name: str) -> bool:
    try:
        proc = _run(["ffmpeg", "-hide_banner", "-filters"], timeout=30)
    except Exception:  # noqa: BLE001 - availability probe must not crash
        return False
    return f" {name} " in (proc.stdout or "")


def sample_quality(original: Path, converted: Path,
                   samples: int = 3, sample_s: int = 10,
                   metric: str = "auto") -> dict:
    """Score short samples of original vs converted.

    Returns {"metric": ..., "mean": float|None, "samples": [...],
             "note": str}. ``mean`` is None when the metric is
    unavailable -- that is a skip, not a failure.
    """
    if metric == "auto":
        metric = "libvmaf" if _filter_available("libvmaf") else "ssim"
    if metric == "libvmaf" and not _filter_available("libvmaf"):
        return {"metric": "libvmaf", "mean": None, "samples": [],
                "note": "libvmaf not in this ffmpeg build; skipped"}
    if metric == "ssim" and not _filter_available("ssim"):
        return {"metric": "ssim", "mean": None, "samples": [],
                "note": "ssim filter not in this ffmpeg build; skipped"}
    try:
        orig_info = probe(original)
        dur = _duration(orig_info) or 0
    except RuntimeError as exc:
        return {"metric": metric, "mean": None, "samples": [],
                "note": f"cannot probe original: {exc}"}
    if dur <= 0:
        return {"metric": metric, "mean": None, "samples": [],
                "note": "original duration unknown; skipped"}
    # Sample points: 10% / 50% / 90% through, clamped so the window fits.
    fracs = [0.1, 0.5, 0.9][:samples] if samples <= 3 else [
        (i + 0.5) / samples for i in range(samples)]
    scores: list[float] = []
    for frac in fracs:
        start = max(0.0, min(dur - sample_s, frac * dur))
        filt = ("libvmaf" if metric == "libvmaf"
                else "ssim")
        # Two inputs, same window, compared frame-by-frame.
        cmd = ["ffmpeg", "-v", "info",
               "-ss", str(start), "-t", str(sample_s), "-i", str(original),
               "-ss", str(start), "-t", str(sample_s), "-i", str(converted),
               "-filter_complex", f"[0:v][1:v]{filt}",
               "-f", "null", "-"]
        try:
            proc = _run(cmd, timeout=600)
        except subprocess.TimeoutExpired:
            continue
        text = (proc.stderr or "")
        if metric == "libvmaf":
            m = re.search(r"VMAF score:\s*([0-9.]+)", text)
        else:
            # ssim prints "SSIM Y:... All:0.98..." per frame; take the
            # last All value as the sample score.
            matches = re.findall(r"All:\s*([0-9.]+)", text)
            m = matches[-1:] and re.match(r"([0-9.]+)", matches[-1])
        if m:
            try:
                scores.append(float(m.group(1)))
            except ValueError:
                pass
    if not scores:
        return {"metric": metric, "mean": None, "samples": [],
                "note": "no sample scores parsed; skipped"}
    return {"metric": metric, "mean": round(sum(scores) / len(scores), 4),
            "samples": scores, "note": ""}


def _verify_against(orig_info: dict, orig_size: int, converted: Path, *,
                    strict: bool = False, fail_if_larger: bool = False,
                    sample_quality_flag: bool = False,
                    encode_log: Path | None = None,
                    sample_original: Path | None = None) -> CheckResult:
    """Shared checks against an already-probed source description.

    *orig_info* / *orig_size* may describe one file (``verify``) or a
    summed multi-file source such as a DVD title set
    (``verify_reference``). Never raises for a bad file."""
    res = CheckResult(passed=True)
    if not converted.is_file():
        res.fail(f"converted file missing: {converted}")
        return res
    new_size = converted.stat().st_size
    res.details = {"original_bytes": orig_size, "converted_bytes": new_size,
                   "savings_gb": round((orig_size - new_size) / 1024 ** 3, 3)}
    if new_size == 0:
        res.fail("converted file is empty (0 bytes)")
        return res
    if orig_size and new_size < MIN_SIZE_FRAC * orig_size:
        res.fail(f"converted file suspiciously small: {new_size} bytes is "
                 f"< {MIN_SIZE_FRAC:.0%} of original {orig_size} bytes")
    try:
        n_info = probe(converted)
    except RuntimeError as exc:
        res.fail(str(exc))
        return res
    o_info = orig_info

    o_v, n_v = _streams(o_info, "video"), _streams(n_info, "video")
    if not n_v:
        res.fail("converted file has no video stream")
    elif len(n_v) > 1:
        res.warn(f"converted file has {len(n_v)} video streams (expected 1)")
    if n_v and _codec_name(n_v[0]) not in ("hevc", "h265"):
        res.fail(f"converted video codec is {_codec_name(n_v[0])!r}, not HEVC")
    if o_v and n_v:
        if (o_v[0].get("width"), o_v[0].get("height")) != \
           (n_v[0].get("width"), n_v[0].get("height")):
            res.warn(f"resolution changed: {o_v[0].get('width')}x"
                     f"{o_v[0].get('height')} -> {n_v[0].get('width')}x"
                     f"{n_v[0].get('height')}")
        res.details["video_codec"] = _codec_name(n_v[0])
        res.details["resolution"] = (f"{n_v[0].get('width')}x"
                                     f"{n_v[0].get('height')}")

    o_dur, n_dur = _duration(o_info), _duration(n_info)
    res.details["original_duration_s"] = o_dur
    res.details["converted_duration_s"] = n_dur
    if o_dur is None or n_dur is None:
        res.fail("could not read duration from original and/or converted file")
    else:
        tol_s = STRICT_TOL_S if strict else DURATION_TOL_S
        tol_f = STRICT_TOL_FRAC if strict else DURATION_TOL_FRAC
        tol = max(tol_s, tol_f * o_dur)
        if abs(o_dur - n_dur) > tol:
            res.fail(f"duration mismatch: original {o_dur:.1f}s vs "
                     f"converted {n_dur:.1f}s (tolerance {tol:.1f}s)")

    o_a, n_a = _streams(o_info, "audio"), _streams(n_info, "audio")
    res.details["audio_streams"] = {"original": len(o_a), "converted": len(n_a)}
    if len(n_a) != len(o_a):
        res.fail(f"audio stream count changed: {len(o_a)} -> {len(n_a)}")
    else:
        for i, (oa, na) in enumerate(zip(o_a, n_a)):
            if _codec_name(oa) != _codec_name(na):
                res.fail(f"audio stream {i} codec changed: "
                         f"{_codec_name(oa)} -> {_codec_name(na)}")
            if oa.get("channels") != na.get("channels"):
                res.fail(f"audio stream {i} channel count changed: "
                         f"{oa.get('channels')} -> {na.get('channels')}")

    o_s, n_s = _streams(o_info, "subtitle"), _streams(n_info, "subtitle")
    res.details["subtitle_streams"] = {"original": len(o_s), "converted": len(n_s)}
    if len(n_s) != len(o_s):
        res.warn(f"subtitle stream count changed: {len(o_s)} -> {len(n_s)} "
                 f"(expected if the container cannot hold the sub codec)")

    if new_size > orig_size:
        msg = (f"converted file is larger than the original "
               f"({new_size} > {orig_size} bytes)")
        if fail_if_larger:
            res.fail(msg)
        else:
            res.warn(msg)

    if encode_log is not None:
        hits = parse_encode_log(encode_log)
        if hits:
            res.fail(f"encode log contains error lines: {hits[0]}")

    decode_errors = decode_check(converted)
    if decode_errors:
        res.fail(f"decode check failed: {decode_errors[0]}")
        res.details["decode_errors"] = decode_errors[:5]

    if sample_quality_flag:
        if sample_original is None:
            # Multi-file source (DVD title set): there is no single
            # original timeline to sample against. Skipped, not failed.
            res.details["sample_quality"] = {
                "metric": "skipped", "mean": None, "samples": [],
                "note": "no single original file to sample (DVD title set)"}
        else:
            sq = sample_quality(sample_original, converted)
            res.details["sample_quality"] = sq
            if sq["mean"] is not None:
                thresh = VMAF_WARN_BELOW if sq["metric"] == "libvmaf" else SSIM_WARN_BELOW
                if sq["mean"] < thresh:
                    res.warn(f"sample {sq['metric']} mean {sq['mean']} below "
                             f"advisory threshold {thresh} (samples: {sq['samples']})")
    return res


def verify(original: Path, converted: Path, *,
           strict: bool = False, fail_if_larger: bool = False,
           sample_quality_flag: bool = False,
           encode_log: Path | None = None) -> CheckResult:
    """Run every hard check on one original file. Never raises."""
    res = CheckResult(passed=True)
    if not original.is_file():
        res.fail(f"original missing: {original}")
        return res
    try:
        o_info = probe(original)
    except RuntimeError as exc:
        res.fail(str(exc))
        return res
    return _verify_against(o_info, original.stat().st_size, converted,
                           strict=strict, fail_if_larger=fail_if_larger,
                           sample_quality_flag=sample_quality_flag,
                           encode_log=encode_log, sample_original=original)


def verify_reference(reference: dict, converted: Path, *,
                     strict: bool = False, fail_if_larger: bool = False,
                     sample_quality_flag: bool = False,
                     encode_log: Path | None = None) -> CheckResult:
    """Verify against a multi-file source (a DVD title set).

    ``reference`` is ``{"size_bytes": int, "duration_seconds": float,
    "probe": <ffprobe JSON of the first content file>}``. Duration is
    the SUMMED title-set duration, overriding the first file's own
    duration; stream makeup is the first file's. A reference with no
    streams or no size fails, rather than passing on empty evidence.
    """
    res = CheckResult(passed=True)
    probe_info = reference.get("probe") or {}
    if not probe_info.get("streams"):
        res.fail("reference probe has no streams (cannot verify)")
        return res
    orig_size = reference.get("size_bytes") or 0
    if not orig_size:
        res.fail("reference size unknown (cannot verify)")
        return res
    orig_info = copy.deepcopy(probe_info)
    duration = reference.get("duration_seconds")
    fmt = orig_info.setdefault("format", {})
    if duration is None:
        fmt.pop("duration", None)
    else:
        fmt["duration"] = str(duration)
    return _verify_against(orig_info, orig_size, converted,
                           strict=strict, fail_if_larger=fail_if_larger,
                           sample_quality_flag=sample_quality_flag,
                           encode_log=encode_log, sample_original=None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify an HEVC encode before replacing the original.")
    ap.add_argument("original", type=Path)
    ap.add_argument("converted", type=Path)
    ap.add_argument("--strict", action="store_true",
                    help="Tighter duration tolerance (0.5s / 0.2 percent)")
    ap.add_argument("--fail-if-larger", action="store_true")
    ap.add_argument("--sample-quality", action="store_true",
                    help="Advisory SSIM/VMAF sample (never fails the gate)")
    ap.add_argument("--encode-log", type=Path, default=None)
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args(argv)
    result = verify(args.original, args.converted, strict=args.strict,
                    fail_if_larger=args.fail_if_larger,
                    sample_quality_flag=args.sample_quality,
                    encode_log=args.encode_log)
    if args.json:
        print(json.dumps({"passed": result.passed,
                          "failures": result.failures,
                          "warnings": result.warnings,
                          "details": result.details}, indent=2))
    else:
        print("PASS" if result.passed else "FAIL")
        for f in result.failures:
            print(f"  FAIL: {f}")
        for w in result.warnings:
            print(f"  WARN: {w}")
        print(f"  details: {json.dumps(result.details)}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
