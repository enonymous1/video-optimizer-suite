#!/usr/bin/env python3
"""CRF ladder test on a short clip -- find a size/quality trade fast.

Why this exists: the full-file CRF 18 test on 2026-10-25 took 73
minutes and came back LARGER than the original (2.482 GB vs 2.474 GB),
because CRF is a quality target, not a size target. Repeating that
for CRF 20, 22, 24 on whole files would burn a day. A 90-second clip
answers the same question in minutes.

How it works (all in --out-dir, original untouched):
  1. Cut one reference clip from the original, stream-copied
     (-c copy, so it is exactly the original's frames, just shorter).
     Cutting once, then encoding FROM the clip, means every CRF
     version shares the same frames -- the comparison is fair.
     Note the seek order matters: `-ss` AFTER `-i` with `-t`
     gives a clip of the requested length (keyframe rounding aside,
     and the probed actual length is what bitrates use). The
     seemingly equivalent `-ss` BEFORE `-i` with `-t` does NOT:
     on a test file it produced a 15s clip for a 10s request,
     because -t then counts from the original timeline.
  2. Encode that clip at each --crf with libx265 / --preset (default
     slow, the same preset as the full-file test), audio and
     subtitles copied, exactly like hevc_convert.py does.
  3. Report size, saving vs the reference clip, bitrate, and encode
     time per CRF, plus a ROUGH projection of what the whole file
     would become if it behaved like the clip.
  4. If stills.py sits next to this script, run a stills compare
     (original clip vs each CRF clip, same timestamps, side by side)
     into compare_crf<N>/, so you judge detail with your eyes, not
     just the byte counts. Pass --no-stills to skip that step.

Picking the clip matters more than anything else in this tool:
  - Pick a slice WITH miniature detail, texture, and movement --
    a close-up of painted models, not a static title card, a black
    fade, or a talking-head shot. A static slice will make every
    CRF look tiny and identical, and the projection will lie.
  - Default --start is 10 minutes in (600s) and --duration 90s.
    If that slice turns out unrepresentative, rerun with a different
    --start -- it costs minutes, not hours.
  - The projection is scene-dependent by nature: a clip cannot
    prove a whole file's saving. It tells you the ORDER of
    magnitude (e.g. CRF 22 saves ~25% on this kind of footage) and
    whether the detail survives -- that is enough to choose a CRF
    for one full-file test, not to batch all 19 files blind.

Example (the Goblins vs Gnomes file):
  crf-test --input "Blood Bowl. Goblins vs Gnomes.mkv" \\
      --out-dir crf-test-goblins --start 600 --duration 90 \\
      --crfs 20 22 24
  # then open crf-test-goblins/compare_crf22/side_by_side/ and look
  # at the miniature close-ups before you trust any number.

Dependencies: ffmpeg + ffprobe on PATH. Python 3.10+, stdlib only.
stills.py is optional (same folder) for step 4.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(value: str) -> float:
    s = str(value).strip().lower()
    if s.endswith("s"):
        s = s[:-1]
    m = _TIME_RE.match(s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"cannot parse time {value!r} (want seconds, 1:30, or 0:01:30)")
    h, mnt, sec = m.groups()
    total = float(sec)
    if mnt is not None:
        total += float(mnt) * 60
    if h is not None:
        total += float(h) * 3600
    return total


def fmt_hms(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def probe_duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: "
                           f"{proc.stderr.strip()[:200]}")
    try:
        duration = float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"could not read duration for {path}")
    if duration <= 0:
        raise RuntimeError(f"could not read duration for {path}")
    return duration


def run(cmd: list[str], what: str) -> float:
    """Run ffmpeg, return elapsed seconds. Raises on failure."""
    print(f"  $ {' '.join(cmd)}")
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=3600)
    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed: {proc.stderr.strip()[-300:]}")
    return elapsed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Encode a short clip at several CRFs and compare "
                    "sizes + stills, before committing to a full file.")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--start", type=parse_time, default=600.0,
                    help="Clip start (seconds, 1:30, or 0:01:30; default 600)")
    ap.add_argument("--duration", type=parse_time, default=90.0,
                    help="Clip length in seconds (default 90)")
    ap.add_argument("--crfs", type=int, nargs="+", default=[20, 22, 24],
                    help="CRF values to test (default 20 22 24; add 18 "
                         "for a visual baseline at the old setting)")
    ap.add_argument("--preset", default="slow",
                    help="x265 preset (default slow, as in the full test)")
    ap.add_argument("--stills-count", type=int, default=6,
                    help="Stills per compare run (default 6)")
    ap.add_argument("--side-width", type=int, default=960)
    ap.add_argument("--no-stills", action="store_true",
                    help="Skip the stills.py compare step")
    args = ap.parse_args(argv)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ERROR: ffmpeg/ffprobe not found on PATH.", file=sys.stderr)
        return 2
    if not args.input.is_file():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 2
    if args.duration <= 0:
        print("ERROR: --duration must be > 0.", file=sys.stderr)
        return 2
    for crf in args.crfs:
        if not 0 <= crf <= 51:
            print(f"ERROR: CRF out of range 0-51: {crf}", file=sys.stderr)
            return 2

    try:
        full_duration = probe_duration(args.input)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    start = max(0.0, min(args.start, full_duration - 5))
    clip_duration = min(args.duration, full_duration - start)
    if clip_duration < 10:
        print(f"ERROR: only {clip_duration:.0f}s left after start "
              f"{fmt_hms(start)} in a {fmt_hms(full_duration)} file; "
              f"pick an earlier --start.", file=sys.stderr)
        return 2
    if clip_duration < args.duration:
        print(f"[warn] clip shortened to {clip_duration:.0f}s (file ends "
              f"at {fmt_hms(full_duration)})")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    ref = args.out_dir / "reference.mkv"
    print(f"Cutting reference clip: {fmt_hms(start)} + {clip_duration:.0f}s "
          f"from {args.input.name}")
    try:
        # Seek AFTER -i on purpose: -ss before -i + -t gives a
        # clip of start+duration, not duration (verified 2026-10-25).
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(args.input), "-ss", str(start),
             "-t", str(clip_duration), "-map", "0", "-c", "copy",
             str(ref)], "reference clip cut")
        actual_clip = probe_duration(ref)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ref_size = ref.stat().st_size
    ref_mbps = ref_size * 8 / actual_clip / 1_000_000 if actual_clip else 0
    full_size = args.input.stat().st_size
    print(f"Reference clip: {ref_size / 1_000_000:.1f} MB, "
          f"{ref_mbps:.2f} Mbps, {actual_clip:.1f}s")

    # The stills step runs the package module (python -m), not a
    # sibling file: after the package migration there is no
    # stills.py next to this module, and a plain-script run of it
    # would fail on its relative import anyway.
    try:
        import video_optimizer.stills  # noqa: F401
        _has_stills = True
    except ImportError:
        _has_stills = False
    rows: list[dict] = []
    failed = 0
    for crf in args.crfs:
        out = args.out_dir / f"crf{crf}.mkv"
        print(f"\nEncoding CRF {crf} (preset {args.preset}) ...")
        try:
            elapsed = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                           "-i", str(ref), "-map", "0",
                           "-c:v", "libx265", "-preset", args.preset,
                           "-crf", str(crf), "-pix_fmt", "yuv420p",
                           "-c:a", "copy", "-c:s", "copy", str(out)],
                          f"CRF {crf} encode")
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += 1
            continue
        size = out.stat().st_size
        saved_pct = (1 - size / ref_size) * 100 if ref_size else 0
        mbps = size * 8 / actual_clip / 1_000_000 if actual_clip else 0
        projected_gb = full_size * (size / ref_size) / 1_000_000_000 if ref_size else 0
        row = {"crf": crf, "output": str(out), "size_mb": round(size / 1_000_000, 1),
               "saved_pct_vs_clip": round(saved_pct, 1),
               "bitrate_mbps": round(mbps, 2), "elapsed_s": round(elapsed, 1),
               "projected_full_file_gb": round(projected_gb, 3),
               "compare_dir": None}
        rows.append(row)
        print(f"  CRF {crf}: {row['size_mb']} MB, saved {saved_pct:.1f}% "
              f"vs clip, {mbps:.2f} Mbps, {elapsed:.0f}s; "
              f"rough full-file projection: {projected_gb:.2f} GB "
              f"(from {full_size / 1_000_000_000:.2f} GB)")
        if not args.no_stills and _has_stills:
            compare_dir = args.out_dir / f"compare_crf{crf}"
            print(f"  Stills compare -> {compare_dir}/side_by_side/")
            proc = subprocess.run(
                [sys.executable, "-m", "video_optimizer.stills", "compare",
                 "--original", str(ref), "--converted", str(out),
                 "--out-dir", str(compare_dir),
                 "--count", str(args.stills_count),
                 "--side-width", str(args.side_width)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=1200)
            if proc.returncode == 0:
                row["compare_dir"] = str(compare_dir)
            else:
                print(f"  [warn] stills compare failed for CRF {crf}; "
                      f"run it manually:\n"
                      f"    video-stills compare --original {ref} "
                      f"--converted {out} --out-dir {compare_dir} "
                      f"--count {args.stills_count}", file=sys.stderr)
        elif not args.no_stills:
            print(f"  [warn] video_optimizer.stills is not importable; "
                  f"compare manually with:\n"
                  f"    video-stills compare --original {ref} "
                  f"--converted {out} --out-dir "
                  f"{args.out_dir}/compare_crf{crf} --count {args.stills_count}",
                  file=sys.stderr)

    summary = {
        "input": str(args.input), "clip_start_s": round(start, 1),
        "clip_duration_s": round(actual_clip, 1),
        "reference": {"output": str(ref), "size_mb": round(ref_size / 1_000_000, 1),
                      "bitrate_mbps": round(ref_mbps, 2)},
        "full_file_gb": round(full_size / 1_000_000_000, 3),
        "preset": args.preset, "results": rows,
        "note": "Projections assume the whole file behaves like this clip. "
                "They are scene-dependent: a static or black clip "
                "understates the bitrate a detail-rich file needs.",
    }
    summary_path = args.out_dir / "crf_summary.json"
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    print("\n=== CRF ladder (clip) ===")
    print(f"{'CRF':>4}  {'Size':>8}  {'Saved':>7}  {'Mbps':>6}  {'Time':>6}  "
          f"{'Rough full-file':>15}")
    for r in rows:
        print(f"{r['crf']:>4}  {r['size_mb']:>7} MB  "
              f"{r['saved_pct_vs_clip']:>6}%  {r['bitrate_mbps']:>6}  "
              f"{r['elapsed_s']:>5}s  {r['projected_full_file_gb']:>14} GB")
    print(f"Summary: {summary_path}")
    print("Next step: open each compare_crf<N>/side_by_side/ folder and "
          "look at the miniature close-ups. Pick the HIGHEST CRF (most "
          "saving) whose stills you cannot tell from the reference -- "
          "then test that one CRF on ONE full file before any batch.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
