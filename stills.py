#!/usr/bin/env python3
"""Grab stills (screenshots) from videos, for visual quality checks.

This is the missing eyeball step for the pipeline: the verifier
(quality_check.py) proves a file decodes and its duration/audio
match, but it cannot tell you whether miniature detail smeared.
Stills can. Use them before you --replace, especially on the first
file of a batch.

Three modes, one tool:

  single   -- one video -> stills in --out-dir
  batch    -- many videos -> one subfolder per video in --out-dir
  compare  -- original + converted -> the SAME timestamps from both,
              plus a side-by-side image per timestamp, so you can
              judge the encode frame-for-frame instead of from memory

Timing / cadence (exactly one of these per run; default --count 12):
  --count N          N evenly spaced stills, at the midpoints of N
                     equal slices (avoids a black first/last frame).
  --interval SEC     a still every SEC seconds, starting at --start.
  --timestamps T...  exact times, e.g. 10 90 1:30 0:05:00 (also
                     accepts "90s"). Times outside [--start, --end]
                     are dropped with a warning, not an error.
  --percents P...    positions as % of the (clipped) duration,
                     e.g. 10 50 90.

  --start SEC / --end SEC  clip the range stills are taken from.
                     --end may be negative to mean "that many seconds
                     before the end" (e.g. --end -10 skips credits).

Seek accuracy (this matters for compare):
  --seek accurate (default) -- `-ss` AFTER `-i`: ffmpeg decodes to
                     the exact timestamp. Slower (one decode pass per
                     still), but original and converted frames are
                     taken at the same moment, which is the whole
                     point of a comparison.
  --seek fast        -- `-ss` BEFORE `-i`: seeks to the nearest
                     keyframe first. Much faster, but the frame can
                     be seconds off the requested time, and original
                     vs converted can land on different moments if
                     their GOP structures differ. Fine for a quick
                     preview, wrong for a quality verdict.

Other flags:
  --width PX / --height PX   resize stills (one dimension only
                     preserves aspect; both together scales exactly
                     and may stretch -- use only if you mean it).
  --quality N        JPEG quality, 1 (best) to 31 (worst), default 2.
                     Ignored for PNG.
  --format jpg|png   default jpg.
  --dry-run          print the timestamps that WOULD be grabbed,
                     extract nothing.
  --overwrite        re-extract existing stills (default: skip
                     existing files, so a re-run resumes).

Output naming always carries the timestamp, e.g.
  Goblins_vs_Gnomes_003_t1845s.jpg
and every run writes stills_manifest.json in --out-dir with the
exact timestamp, source path, and output path per still, so a
still can always be traced back to its moment in the video.

Batch sources (exactly one):
  --inputs a.mkv b.mkv ...   explicit files
  --glob "media/**/*.mkv"    glob, recursive with **
  --report report.json       scanner report, with --priority
                             (high/medium/low/all, default all),
                             --limit, --sort-by size|savings,
                             --order asc|desc

Compare output layout (in --out-dir):
  original/<stem>_001_t120s.jpg
  converted/<stem>_001_t120s.jpg
  side_by_side/compare_001_t120s.jpg   (left=original, right=converted)
  stills_manifest.json

Side-by-side frames are scaled to --side-width pixels wide per side
(default 960) with aspect preserved and padded to a common height,
so hstack never fails on a 1-2px rounding difference between the
two sources. The individual frames are left at full (or --width)
size for close inspection -- the side-by-side is the preview, not
the evidence.

Dependencies: ffmpeg + ffprobe on PATH. Python 3.10+, stdlib only
(optional tqdm for a progress bar).
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from tqdm import tqdm
    _HAS_TQDM = True
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore
    _HAS_TQDM = False


# ---------------------------------------------------------------------------
# Time parsing / formatting
# ---------------------------------------------------------------------------
_TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(value: str) -> float:
    """Parse '90', '90s', '1:30', '0:01:30', '1:30.5' to seconds."""
    s = str(value).strip().lower()
    if s.endswith("s"):
        s = s[:-1]
    m = _TIME_RE.match(s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"cannot parse timestamp {value!r} (want seconds, 1:30, or 0:01:30)")
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


def fmt_filename_ts(seconds: float) -> str:
    """Timestamp for a filename: whole seconds, e.g. t1845s."""
    return f"t{int(round(seconds))}s"


def sanitize_stem(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return cleaned[:120] or "video"


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def probe_video(path: Path) -> dict:
    """Return {duration_s, width, height} for *path* via ffprobe."""
    if not path.is_file():
        raise FileNotFoundError(f"video not found: {path}")
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: "
                           f"{proc.stderr.strip()[:200]}")
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe emitted invalid JSON for {path}: {exc}")
    duration: float | None = None
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        pass
    width = height = None
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            width, height = stream.get("width"), stream.get("height")
            break
    if not duration or duration <= 0:
        raise RuntimeError(f"could not read duration for {path}")
    return {"duration_s": duration, "width": width, "height": height}


# ---------------------------------------------------------------------------
# Timestamp resolution
# ---------------------------------------------------------------------------
def resolve_timestamps(duration: float, args: argparse.Namespace) -> list[float]:
    """Return sorted, deduplicated timestamps inside [--start, --end].

    Exactly one of --count / --interval / --timestamps / --percents
    may be given; with none given, --count 12 is the default.
    """
    given = [args.count is not None, args.interval is not None,
             args.timestamps is not None, args.percents is not None]
    if sum(given) > 1:
        raise ValueError("pass exactly one of --count, --interval, "
                         "--timestamps, --percents")
    start = args.start or 0.0
    end = args.end if args.end is not None else duration
    if end < 0:  # e.g. --end -10 = 10s before the end
        end = duration + end
    start = max(0.0, min(start, duration))
    end = max(0.0, min(end, duration))
    if start >= end:
        raise ValueError(f"--start {start}s must be before --end {end}s "
                         f"(duration {duration:.1f}s)")

    if args.timestamps is not None:
        out: list[float] = []
        for t in args.timestamps:
            if start <= t <= end:
                out.append(t)
            else:
                print(f"[warn] dropping timestamp {t}s outside "
                      f"[{start:.0f}s, {end:.0f}s]", file=sys.stderr)
        times = out
    elif args.percents is not None:
        times = [start + (end - start) * p / 100.0 for p in args.percents
                 if 0 <= p <= 100]
        bad = [p for p in args.percents if not 0 <= p <= 100]
        if bad:
            raise ValueError(f"percents out of range 0-100: {bad}")
    elif args.interval is not None:
        if args.interval <= 0:
            raise ValueError("--interval must be > 0")
        times, t = [], start
        while t < end:
            times.append(t)
            t += args.interval
    else:
        count = args.count if args.count is not None else 12
        if count <= 0:
            raise ValueError("--count must be > 0")
        span = end - start
        times = [start + span * (i + 0.5) / count for i in range(count)]
    # Deduplicate (e.g. tiny interval on a short clip) and sort.
    return sorted({round(t, 3) for t in times})


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _scale_filter(args: argparse.Namespace) -> list[str]:
    if args.width and args.height:
        return ["-vf", f"scale={args.width}:{args.height}"]
    if args.width:
        return ["-vf", f"scale={args.width}:-2"]
    if args.height:
        return ["-vf", f"scale=-2:{args.height}"]
    return []


def extract_frame(video: Path, timestamp: float, out_path: Path,
                  args: argparse.Namespace) -> bool:
    """Extract one frame. Returns True on success."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not args.overwrite:
        return True  # resume: already have this still
    fmt = args.format
    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if args.seek == "fast":
        cmd += ["-ss", str(timestamp), "-i", str(video)]
    else:  # accurate: seek after input
        cmd += ["-i", str(video), "-ss", str(timestamp)]
    cmd += ["-frames:v", "1"] + _scale_filter(args)
    if fmt == "jpg":
        cmd += ["-q:v", str(args.quality)]
    cmd += [str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
    except FileNotFoundError:
        print("ERROR: ffmpeg not found on PATH", file=sys.stderr)
        return False
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        print(f"[fail] {video} @ {timestamp:.1f}s: "
              f"{proc.stderr.strip()[:200]}", file=sys.stderr)
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def extract_for_video(video: Path, out_dir: Path, args: argparse.Namespace,
                      manifest: list[dict], label: str = "") -> tuple[int, int]:
    """Extract all resolved stills for one video. Returns (ok, failed)."""
    try:
        info = probe_video(video)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 0, 1
    try:
        times = resolve_timestamps(info["duration_s"], args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 0, 1
    if args.dry_run:
        for i, t in enumerate(times, 1):
            print(f"  [dry-run] {video} @ {fmt_hms(t)} ({t:.1f}s)")
        return 0, 0
    stem = sanitize_stem(video.stem)
    prefix = f"{label}_" if label else ""
    ok = failed = 0
    bar = None
    if _HAS_TQDM and len(times) > 1:
        bar = tqdm(total=len(times), desc=stem, unit="still", leave=False)
    for i, t in enumerate(times, 1):
        name = f"{prefix}{stem}_{i:03d}_{fmt_filename_ts(t)}.{args.format}"
        out_path = out_dir / name
        if extract_frame(video, t, out_path, args):
            ok += 1
            manifest.append({
                "source": str(video), "label": label or None,
                "timestamp_s": round(t, 3), "timestamp_hms": fmt_hms(t),
                "output": str(out_path),
                "duration_s": round(info["duration_s"], 1),
            })
        else:
            failed += 1
        if bar is not None:
            bar.update(1)
    if bar is not None:
        bar.close()
    print(f"{video.name}: {ok} still(s), {failed} failed "
          f"-> {out_dir} (duration {fmt_hms(info['duration_s'])})")
    return ok, failed


def write_manifest(out_dir: Path, manifest: list[dict]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "stills_manifest.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return path


# ---------------------------------------------------------------------------
# Compare: side-by-side composites
# ---------------------------------------------------------------------------
def make_side_by_side(orig_frame: Path, conv_frame: Path, out_path: Path,
                      orig_info: dict, conv_info: dict,
                      args: argparse.Namespace) -> bool:
    """Composite two frames side by side (left=original, right=converted).

    Both frames are scaled to --side-width wide, aspect preserved,
    then padded to a common height so hstack cannot fail on a 1px
    rounding difference. No drawtext labels: the font dependency
    is not worth it -- left/right is documented in the manifest
    and in the side_by_side/ folder name, and the individual frames
    live alongside for close inspection.
    """
    side_w = args.side_width
    # Scaled heights at side_w wide, preserving aspect.
    def scaled_h(info: dict) -> int:
        w, h = info.get("width"), info.get("height")
        if w and h:
            return max(2, int(round(h * side_w / w)) // 2 * 2)
        return side_w  # unknown aspect: square fallback, still common
    common_h = max(scaled_h(orig_info), scaled_h(conv_info))
    filt = (
        f"[0:v]scale={side_w}:{common_h}:force_original_aspect_ratio=decrease,"
        f"pad={side_w}:{common_h}:(ow-iw)/2:(oh-ih)/2[v0];"
        f"[1:v]scale={side_w}:{common_h}:force_original_aspect_ratio=decrease,"
        f"pad={side_w}:{common_h}:(ow-iw)/2:(oh-ih)/2[v1];"
        f"[v0][v1]hstack[vout]"
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(orig_frame), "-i", str(conv_frame),
           "-filter_complex", filt, "-map", "[vout]", "-frames:v", "1"]
    if args.format == "jpg":
        cmd += ["-q:v", str(args.quality)]
    cmd += [str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
    except FileNotFoundError:
        return False
    if proc.returncode != 0 or not out_path.is_file():
        print(f"[fail] side-by-side {out_path}: "
              f"{proc.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def add_timing_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--count", type=int, default=None,
                   help="Evenly spaced stills (default 12 if no other "
                        "timing flag is given)")
    p.add_argument("--interval", type=float, default=None,
                   help="Seconds between stills, starting at --start")
    p.add_argument("--timestamps", type=parse_time, nargs="+", default=None,
                   help="Exact times, e.g. 10 90 1:30")
    p.add_argument("--percents", type=float, nargs="+", default=None,
                   help="Positions as %% of duration, e.g. 10 50 90")
    p.add_argument("--start", type=parse_time, default=0.0,
                   help="Clip start (default 0)")
    p.add_argument("--end", type=parse_time, default=None,
                   help="Clip end; negative = seconds before the end "
                        "(parse with = : --end=-10)")
    p.add_argument("--seek", choices=["accurate", "fast"], default="accurate",
                   help="accurate (default, exact frame) or fast (keyframe)")


def add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--quality", type=int, default=2,
                   help="JPEG quality 1 (best) - 31 (worst), default 2")
    p.add_argument("--format", choices=["jpg", "png"], default="jpg")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="List timestamps, extract nothing")


def _load_report_inputs(report: Path, args: argparse.Namespace) -> list[Path]:
    """Load scanner-report paths, filtered like the converter's selection."""
    try:
        from state_manager import load_report_file
        rows = load_report_file(report)
    except Exception:  # noqa: BLE001 - fall back to plain JSON
        with open(report, encoding="utf-8") as fh:
            rows = json.load(fh)
    wanted = {"high": "High Priority", "medium": "Medium",
              "low": "Low"}.get(args.priority)
    out: list[Path] = []
    for r in rows:
        if r.get("Error"):
            continue
        if wanted and r.get("Optimization_Candidate") != wanted:
            continue
        path = r.get("Path")
        if path:
            out.append(Path(path))
    key = "Size_GB" if args.sort_by == "size" else "Estimated_Savings_GB"
    reverse = args.order == "desc"
    by_path = {str(r.get("Path")): r for r in rows}
    def _val(p: Path) -> float:
        try:
            return float(by_path.get(str(p), {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
    out.sort(key=_val, reverse=reverse)
    if args.limit and args.limit > 0:
        out = out[: args.limit]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Grab stills from videos, singly, in batch, or "
                    "side-by-side original vs converted.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("single", help="Stills from one video")
    p.add_argument("--input", type=Path, required=True)
    add_timing_args(p)
    add_output_args(p)

    p = sub.add_parser("batch", help="Stills from many videos, one folder each")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--inputs", type=Path, nargs="+")
    src.add_argument("--glob", type=str,
                     help='e.g. "media/**/*.mkv" (recursive with **)')
    src.add_argument("--report", type=Path, help="Scanner report JSON/CSV")
    p.add_argument("--priority", choices=["high", "medium", "low", "all"],
                   default="all")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--sort-by", choices=["size", "savings"], default="savings")
    p.add_argument("--order", choices=["asc", "desc"], default="desc")
    add_timing_args(p)
    add_output_args(p)

    p = sub.add_parser("compare",
                       help="Same timestamps from original + converted, "
                            "side by side")
    p.add_argument("--original", type=Path, required=True)
    p.add_argument("--converted", type=Path, required=True)
    p.add_argument("--side-width", type=int, default=960,
                   help="Width per side in side-by-side images (default 960)")
    p.add_argument("--keep-individuals", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="Also keep original/ and converted/ stills "
                        "(default on; --no-keep-individuals for composites only)")
    add_timing_args(p)
    add_output_args(p)

    args = ap.parse_args(argv)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ERROR: ffmpeg/ffprobe not found on PATH.", file=sys.stderr)
        return 2

    manifest: list[dict] = []
    total_ok = total_failed = 0

    if args.cmd == "single":
        if not args.input.is_file():
            print(f"ERROR: video not found: {args.input}", file=sys.stderr)
            return 2
        ok, failed = extract_for_video(args.input, args.out_dir, args, manifest)
        total_ok, total_failed = ok, failed

    elif args.cmd == "batch":
        if args.inputs is not None:
            videos = list(args.inputs)
        elif args.glob is not None:
            videos = [Path(m) for m in sorted(globmod.glob(args.glob, recursive=True))]
        else:
            if not args.report.is_file():
                print(f"ERROR: report not found: {args.report}", file=sys.stderr)
                return 2
            videos = _load_report_inputs(args.report, args)
        # Deduplicate, preserving order; drop missing files with a warning.
        seen: set[str] = set()
        uniq: list[Path] = []
        for v in videos:
            if str(v) in seen:
                continue
            seen.add(str(v))
            if not v.is_file():
                print(f"[warn] skipping missing file: {v}", file=sys.stderr)
                continue
            uniq.append(v)
        if not uniq:
            print("ERROR: no input videos found.", file=sys.stderr)
            return 2
        for v in uniq:
            sub = args.out_dir / sanitize_stem(v.stem)
            ok, failed = extract_for_video(v, sub, args, manifest)
            total_ok += ok
            total_failed += failed

    elif args.cmd == "compare":
        for label, path in (("original", args.original),
                            ("converted", args.converted)):
            if not path.is_file():
                print(f"ERROR: {label} video not found: {path}", file=sys.stderr)
                return 2
        try:
            orig_info = probe_video(args.original)
            conv_info = probe_video(args.converted)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        # Same timestamps for both: clip to the shorter video so a
        # timestamp can never exist in one and not the other.
        shorter = min(orig_info["duration_s"], conv_info["duration_s"])
        if abs(orig_info["duration_s"] - conv_info["duration_s"]) > max(2.0, 0.01 * orig_info["duration_s"]):
            print(f"[warn] durations differ: original "
                  f"{orig_info['duration_s']:.1f}s vs converted "
                  f"{conv_info['duration_s']:.1f}s; timestamps clipped to "
                  f"the shorter video", file=sys.stderr)
        # Temporarily cap --end at the shorter duration for resolution.
        saved_end = args.end
        if args.end is None or args.end > shorter:
            args.end = shorter
        try:
            times = resolve_timestamps(orig_info["duration_s"], args)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        finally:
            args.end = saved_end
        if args.dry_run:
            for t in times:
                print(f"  [dry-run] compare @ {fmt_hms(t)} ({t:.1f}s)")
            return 0
        # Reuse extract_for_video's naming by extracting with a
        # per-video args copy that pins the resolved timestamps.
        # Simplest correct route: extract each side into its folder
        # with --timestamps set to the resolved list.
        args.timestamps = times
        args.count = args.interval = args.percents = None
        stem = sanitize_stem(args.original.stem)
        orig_dir = args.out_dir / "original"
        conv_dir = args.out_dir / "converted"
        if args.keep_individuals:
            o_ok, o_fail = extract_for_video(args.original, orig_dir, args, manifest, label="original")
            c_ok, c_fail = extract_for_video(args.converted, conv_dir, args, manifest, label="converted")
        else:
            # Still need frames to composite from: extract to temp
            # folders inside out_dir, then remove them afterwards.
            # out_dir must exist first: mkdtemp does NOT create its
            # parent, and this was a live crash (2026-10-25) when
            # --no-keep-individuals was used with a fresh out-dir.
            args.out_dir.mkdir(parents=True, exist_ok=True)
            import tempfile
            tmp = Path(tempfile.mkdtemp(prefix="stills_compare_", dir=str(args.out_dir)))
            orig_dir, conv_dir = tmp / "original", tmp / "converted"
            o_ok, o_fail = extract_for_video(args.original, orig_dir, args, manifest, label="original")
            c_ok, c_fail = extract_for_video(args.converted, conv_dir, args, manifest, label="converted")
        total_ok, total_failed = o_ok + c_ok, o_fail + c_fail
        # Pair up by manifest order, NOT by filename glob: the
        # converted file usually has a different stem (e.g. .hevc),
        # so its frames would not match an original-stem glob.
        # Both sides used the same timestamp list, in the same order.
        side_dir = args.out_dir / "side_by_side"
        side_dir.mkdir(parents=True, exist_ok=True)
        orig_entries = [m for m in manifest if m.get("label") == "original"]
        conv_entries = [m for m in manifest if m.get("label") == "converted"]
        for i, (oe, ce) in enumerate(zip(orig_entries, conv_entries)):
            of, cf = Path(oe["output"]), Path(ce["output"])
            t = oe["timestamp_s"]
            side_path = side_dir / f"compare_{i+1:03d}_{fmt_filename_ts(t)}.{args.format}"
            if make_side_by_side(of, cf, side_path, orig_info, conv_info, args):
                manifest.append({
                    "source": "side_by_side", "label": "compare",
                    "timestamp_s": round(t, 3), "timestamp_hms": fmt_hms(t),
                    "output": str(side_path),
                    "left": str(of), "right": str(cf),
                })
            else:
                total_failed += 1
        if not args.keep_individuals:
            shutil.rmtree(tmp, ignore_errors=True)
            # Keep only side-by-side entries, and drop their
            # left/right pointers (those temp frames are gone).
            manifest = [m for m in manifest if m.get("label") == "compare"]
            for m in manifest:
                m.pop("left", None)
                m.pop("right", None)
        print(f"Compare: {len(list(side_dir.glob(f'*.{args.format}')))} "
              f"side-by-side image(s) in {side_dir}")

    if not args.dry_run:
        manifest_path = write_manifest(args.out_dir, manifest)
        print(f"Manifest: {manifest_path} ({len(manifest)} entries)")
    print(f"Done: {total_ok} still(s) extracted, {total_failed} failed.")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
