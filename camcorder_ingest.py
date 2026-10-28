#!/usr/bin/env python3
"""Camcorder ingest for the HEVC pipeline: legacy camcorder files -> jobs.

This is the front end the old ``handbrake_camcorder_conversion``
project needed, folded into the suite instead of becoming a second
encoder. It does ONLY the camcorder-specific work:

  * discover camcorder files under a root, including the formats the
    bundled scanner misses (``.mod``, ``.tod``, ``.m2t``) plus the
    camcorder formats it already covers (``.mts``, ``.m2ts``, ``.mpg``,
    ``.mpeg``, ``.vob``, ``.mov``, ``.avi``) -- so one camcorder root
    yields one kind of job, whichever scanner found a file first;
  * probe each file once with ffprobe and emit one scanner-compatible
    report row, so the existing flow works unchanged: state_manager
    import -> hevc_convert (ffmpeg HEVC encode) -> quality_check
    verification;
  * find the metadata sidecar files that sit next to a clip
    (``.modd``, ``.xmp``, ``.xml``, the set the old tool used) and
    record them on the row, so the converter can merge/copy them
    after a verified encode;
  * record the video stream's ``field_order`` so the converter can
    deinterlace only when ffprobe actually says the source is
    interlaced. Unknown field order means no deinterlace -- a guess
    either way would silently change progressive footage or leave
    interlaced footage combed.

What this module deliberately does NOT do:

  * encode anything (``encoders.py`` / ``hevc_convert.py`` own that).
    HandBrakeCLI is gone in this lane: camcorder files are encoded
    to HEVC by ffmpeg, like everything else in the suite, and the
    verifier's HEVC check applies unchanged;
  * carry over the old tool's YAML settings, HandBrake presets, RF
    quality, audio re-encode options, or ``additional_args``
    passthrough. Quality is the suite's ``--crf``/``--preset`` and
    audio is always copied, as for every other job;
  * claim a sidecar's contents. ``.moi`` index files are not sidecars
    here (the old tool did not carry them either) and are ignored.

A camcorder job's ``Path`` is the real file, unlike a DVD job's
synthetic title-set key, so the state DB fingerprints it by path +
size + mtime like any flat file, and ``--replace`` applies to it
like any flat file (opt-in, backup kept).

CLI:

    python camcorder_ingest.py scan "D:\\\\Camcorder" --out-dir reports

Dependencies: none (stdlib only). ffprobe must be on PATH for
``scan``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

__version__ = "1.0.0"

# Formats the bundled scanner (video_optimizer_scan.py) does NOT
# cover: .mod / .tod (JVC Everio MPEG-2) and .m2t (HDV transport
# stream). The rest of CAMCORDER_EXTENSIONS overlaps the scanner on
# purpose: a camcorder root is scanned as camcorder jobs, and the
# pipeline imports the camcorder report after the main report so
# the camcorder row (Source_Kind + sidecars + field order) wins for
# a file both scanners found. .mp4/.mkv are deliberately excluded:
# they are already-modern containers, not camcorder originals.
CAMCORDER_EXTENSIONS = {
    ".mod", ".tod", ".m2t",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".vob", ".mov", ".avi",
}

# Metadata sidecar extensions, exactly the set the old tool used.
SIDECAR_EXTENSIONS = ("modd", "xmp", "xml")

# ffprobe field_order values that mean interlaced fields, not frames.
# "progressive" and "unknown" (the common case for camcorder files
# whose container carries no field-order tag) mean no deinterlace.
INTERLACED_FIELD_ORDERS = {"interlaced", "tt", "bb", "tb", "bt"}

FFPROBE_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def find_camcorder_files(root: str | Path,
                         min_size_mb: float = 0.0) -> list[Path]:
    """Recursively collect camcorder files under *root*.

    The root itself may be a single camcorder file. Hidden
    directories are pruned. Matching is case-insensitive: files
    copied off a camera or an SD card often carry uppercase
    extensions (``.MOD``, ``.TOD``), and exact-case matching would
    silently find nothing there.
    """
    root = Path(root)
    if root.is_file():
        return [root.resolve()] if root.suffix.lower() in CAMCORDER_EXTENSIONS else []
    if not root.is_dir():
        return []
    min_bytes = min_size_mb * 1024 * 1024
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() not in CAMCORDER_EXTENSIONS:
                continue
            try:
                if p.stat().st_size < min_bytes:
                    continue
            except OSError:
                continue  # unreadable file: skip quietly, don't abort
            found.append(p.resolve())
    return sorted(found)


def find_sidecars(path: str | Path) -> list[Path]:
    """Metadata sidecars sitting next to *path*, in SIDECAR order.

    Matching is case-insensitive on the sidecar extension, so
    ``clip.MODD`` next to ``clip.MOD`` is found. A sidecar is only
    reported when the file actually exists -- nothing is guessed.
    """
    path = Path(path)
    parent = path.parent
    try:
        names = {n.lower(): n for n in os.listdir(parent)}
    except OSError:
        return []
    found: list[Path] = []
    for ext in SIDECAR_EXTENSIONS:
        wanted = f"{path.stem}.{ext}".lower()
        actual = names.get(wanted)
        if actual is not None:
            found.append(parent / actual)
    return found


# ---------------------------------------------------------------------------
# ffprobe (the scanner's pattern: stdlib, utf-8, timeout)
# ---------------------------------------------------------------------------
def _probe_file(path: Path) -> dict:
    """Run ffprobe on *path*, return parsed JSON. Raises RuntimeError."""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=FFPROBE_TIMEOUT_S)
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH (install ffmpeg)")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timed out after {FFPROBE_TIMEOUT_S}s")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200]
        raise RuntimeError(f"ffprobe failed (rc={proc.returncode}) {detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe emitted invalid JSON: {exc}")


_CODEC_LABELS = {
    "h264": "H.264", "avc": "H.264", "avc1": "H.264",
    "hevc": "HEVC", "h265": "HEVC", "hev1": "HEVC", "hvc1": "HEVC",
    "av1": "AV1", "av01": "AV1", "vp9": "VP9",
    "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1",
    "dvvideo": "DV", "mjpeg": "MJPEG",
    "wmv3": "WMV", "vc1": "VC-1",
    "aac": "AAC", "ac3": "AC3", "eac3": "E-AC3",
    "truehd": "TrueHD", "dts": "DTS", "dca": "DTS",
    "mp3": "MP3", "flac": "FLAC", "pcm_s16le": "PCM", "opus": "Opus",
    "subrip": "SubRip", "srt": "SubRip", "ass": "ASS", "ssa": "SSA",
    "mov_text": "mov_text", "tx3g": "mov_text",
    "hdmv_pgs_subtitle": "PGS", "dvd_subtitle": "DVD",
    "webvtt": "WebVTT", "dvb_subtitle": "DVB",
}


def _codec_label(codec_name: str | None) -> str | None:
    if not codec_name:
        return None
    return _CODEC_LABELS.get(codec_name.strip().lower(),
                             codec_name.strip().upper())


def _stream_language(stream: dict) -> str:
    tags = stream.get("tags") or {}
    return str(tags.get("language") or "?").strip() or "?"


def _stream_title(stream: dict) -> str | None:
    tags = stream.get("tags") or {}
    title = tags.get("title")
    return str(title).strip() if title and str(title).strip() else None


def _disposition_flags(stream: dict) -> list[str]:
    disp = stream.get("disposition") or {}
    order = ["default", "forced", "hearing_impaired", "visual_impaired",
             "comment", "original", "dub"]
    return [f for f in order if disp.get(f) == 1]


def _kbps_to_mbps(value: object) -> float | None:
    try:
        bps = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(bps / 1_000_000, 3) if bps > 0 else None


def _parse_fps(value: object) -> float | None:
    if value is None:
        return None
    text = str(value)
    if "/" in text:
        try:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _bit_depth(pix_fmt: str | None) -> int | None:
    if not pix_fmt:
        return None
    low = pix_fmt.lower()
    if "16" in low:
        return 16
    if "12" in low:
        return 12
    if "10" in low:
        return 10
    if "p" in low:  # yuv420p, yuv444p, ... -> 8-bit planar family
        return 8
    return None


def _aspect_ratio(stream: dict, width: int | None,
                  height: int | None) -> str | None:
    dar = stream.get("display_aspect_ratio")
    if dar and dar not in ("0:0", "N/A"):
        return str(dar)
    if width and height:
        g = math.gcd(width, height)
        return f"{width // g}:{height // g}"
    return None


def _fmt_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _describe_streams(streams: list[dict]) -> tuple[str | None, int,
                                                    str | None, int | None,
                                                    int, str | None,
                                                    str | None, float | None]:
    """Audio/subtitle display columns, mirroring the scanner's format."""
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    audio_descs: list[str] = []
    total_mbps = 0.0
    channels: list[int] = []
    for a in audio:
        codec = _codec_label(a.get("codec_name")) or "?"
        ch = a.get("channels")
        br = _kbps_to_mbps(a.get("bit_rate"))
        if br:
            total_mbps += br
        if isinstance(ch, int):
            channels.append(ch)
        title = _stream_title(a)
        ch_txt = f"{ch}ch" if isinstance(ch, int) else "?ch"
        sr = a.get("sample_rate")
        sr_txt = f"{sr} Hz" if sr else "? Hz"
        br_txt = f"{br:.2f} Mbps" if br else "bitrate?"
        title_txt = f", '{title}'" if title else ""
        audio_descs.append(
            f"{codec} ({ch_txt}, {sr_txt}, {_stream_language(a)}, "
            f"{br_txt}{title_txt})")
    sub_descs: list[str] = []
    for s in subs:
        codec = _codec_label(s.get("codec_name")) or "?"
        flags = _disposition_flags(s)
        flags_txt = f", {', '.join(flags)}" if flags else ""
        title = _stream_title(s)
        title_txt = f", '{title}'" if title else ""
        sub_descs.append(
            f"{codec} ({_stream_language(s)}{flags_txt}{title_txt})")
    return (
        " | ".join(audio_descs) if audio_descs else None,
        len(audio),
        " | ".join(_stream_language(a) for a in audio) if audio else None,
        max(channels) if channels else None,
        len(subs),
        " | ".join(sub_descs) if sub_descs else None,
        " | ".join(_stream_language(s) for s in subs) if subs else None,
        round(total_mbps, 3) if total_mbps > 0 else None,
    )


def _video_bitrate(info: dict, streams: list[dict],
                   overall_mbps: float | None) -> float | None:
    video = [s for s in streams if s.get("codec_type") == "video"]
    v = video[0] if video else None
    vbr = _kbps_to_mbps(v.get("bit_rate")) if v else None
    if vbr is None and overall_mbps:
        audio_total = sum(
            float(a.get("bit_rate") or 0)
            for a in streams
            if a.get("codec_type") == "audio" and a.get("bit_rate")
        ) / 1_000_000
        vbr = round(overall_mbps - audio_total, 3)
    return vbr if (vbr and vbr > 0) else None


def field_order(stream: dict | None) -> str | None:
    """ffprobe field_order for a video stream, lowercased, or None."""
    if not stream:
        return None
    value = stream.get("field_order")
    return str(value).strip().lower() if value else None


def is_interlaced(stream: dict | None) -> bool | None:
    """True/False when field_order says so; None when it says nothing.

    ``unknown`` is common for camcorder containers and is reported as
    None (unknown), never guessed as either progressive or interlaced.
    """
    order = field_order(stream)
    if order is None or order == "unknown":
        return None
    if order == "progressive":
        return False
    return order in INTERLACED_FIELD_ORDERS


# ---------------------------------------------------------------------------
# Report rows: one per file, scanner-compatible
# ---------------------------------------------------------------------------
def _error_row(path: Path, error: str,
               size_bytes: int | None = None) -> dict:
    return {
        "Path": str(path.resolve()),
        "Parent_Directory": str(path.parent.resolve()),
        "Filename": path.name,
        "Size_MB": round(size_bytes / (1024 ** 2), 2) if size_bytes else None,
        "Size_GB": round(size_bytes / (1024 ** 3), 3) if size_bytes else None,
        "Source_Kind": "camcorder",
        "Sidecar_Files": [str(p) for p in find_sidecars(path)],
        "Field_Order": None,
        "Interlaced": None,
        "Error": error,
        "Optimization_Candidate": "Low",
    }


def build_row(path: str | Path) -> dict:
    """Build one scanner-compatible row for a camcorder file.

    Probes the file once. Candidate + savings estimate come from the
    scanner, so a camcorder file is ranked by the same rules as a
    flat file. If the scanner is unavailable, the row is honestly
    unranked (Low, no estimate) rather than inventing one.
    """
    path = Path(path)
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        return _error_row(path, f"Cannot stat file: {exc}")
    sidecars = find_sidecars(path)
    try:
        info = _probe_file(path)
    except RuntimeError as exc:
        return _error_row(path, str(exc), size_bytes=size_bytes)

    fmt = info.get("format", {}) or {}
    streams = info.get("streams", []) or []
    try:
        dur = float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
    except (TypeError, ValueError):
        dur = None
    container = fmt.get("format_name", "")
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    v = video_streams[0] if video_streams else None
    width = v.get("width") if v and isinstance(v.get("width"), int) else None
    height = v.get("height") if v and isinstance(v.get("height"), int) else None
    overall_mbps = _kbps_to_mbps(fmt.get("bit_rate"))
    (audio_descs, audio_count, audio_langs, audio_channels,
     sub_count, sub_descs, sub_langs, audio_mbps) = _describe_streams(streams)
    fps = (_parse_fps(v.get("avg_frame_rate"))
           or _parse_fps(v.get("r_frame_rate"))) if v else None

    row: dict = {
        "Path": str(path.resolve()),
        "Parent_Directory": str(path.parent.resolve()),
        "Filename": path.name,
        "Size_MB": round(size_bytes / (1024 ** 2), 2),
        "Size_GB": round(size_bytes / (1024 ** 3), 3),
        "Duration": _fmt_duration(dur),
        "Duration_Seconds": round(dur, 1) if dur else None,
        "Container_Format": container.split(",")[0].upper() if container else None,
        "Video_Codec": _codec_label(v.get("codec_name")) if v else None,
        "Resolution": f"{width}x{height}" if width and height else None,
        "Width": width,
        "Height": height,
        "Aspect_Ratio": _aspect_ratio(v, width, height) if v else None,
        "FPS": round(fps, 3) if fps else None,
        "Pixel_Format": v.get("pix_fmt") if v else None,
        "Bit_Depth": _bit_depth(v.get("pix_fmt")) if v else None,
        "Video_Bitrate_Mbps": _video_bitrate(info, streams, overall_mbps),
        "Audio_Codecs": audio_descs,
        "Audio_Channels": audio_channels,
        "Audio_Bitrate_Mbps": audio_mbps,
        "Audio_Stream_Count": audio_count,
        "Audio_Languages": audio_langs,
        "Subtitle_Stream_Count": sub_count,
        "Subtitle_Codecs": sub_descs,
        "Subtitle_Languages": sub_langs,
        "Overall_Bitrate_Mbps": overall_mbps,
        "Bloat_Flags": "camcorder",
        "Source_Kind": "camcorder",
        "Original_Extension": path.suffix.lower(),
        "Sidecar_Files": [str(p) for p in sidecars],
        "Field_Order": field_order(v),
        "Interlaced": is_interlaced(v),
    }

    try:
        from video_optimizer_scan import (  # noqa: E402
            classify_candidate, compute_bloat_flags, estimate_hevc_savings)
        candidate, reason = classify_candidate(row)
        row["Optimization_Candidate"] = candidate
        row["Candidate_Reason"] = f"Camcorder: {reason}"
        row.update(estimate_hevc_savings(row))
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        bloat = compute_bloat_flags(row, audio_streams)
        row["Bloat_Flags"] = " | ".join(["camcorder"] + bloat) if bloat else "camcorder"
    except ImportError:
        row["Optimization_Candidate"] = "Low"
        row["Candidate_Reason"] = ("Camcorder file (scanner unavailable; "
                                   "no savings estimate)")
    return row


def scan_camcorders(root: str | Path,
                    min_size_mb: float = 0.0) -> tuple[list[dict], list[Path]]:
    """Scan *root* for camcorder files. Returns (rows, files)."""
    files = find_camcorder_files(root, min_size_mb)
    return [build_row(p) for p in files], files


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _write_report(rows: list[dict], out_dir: Path, stem: str) -> tuple[Path, Path]:
    """Write scanner-style CSV + JSON via the scanner when available.

    Falls back to a plain JSON write when the scanner module cannot be
    imported; the JSON is what state_manager imports, the CSV is the
    human-readable copy.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        from video_optimizer_scan import write_reports  # noqa: E402
        return write_reports(rows, out_dir, stem)
    except ImportError:
        json_path = out_dir / f"{stem}.json"
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        return json_path, json_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Camcorder ingest for the HEVC pipeline "
                    "(camcorder files -> jobs).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="Find camcorder files and write a job report")
    p.add_argument("root", type=Path,
                   help="Root to search, or a single camcorder file")
    p.add_argument("--out-dir", type=Path, default=Path("reports"))
    p.add_argument("--stem", default="camcorder_report",
                   help="Report filename stem (default: camcorder_report)")
    p.add_argument("--min-size-mb", type=float, default=0.0,
                   help="Skip files smaller than this (default: 0)")
    args = ap.parse_args(argv)

    if not args.root.exists():
        print(f"ERROR: root not found: {args.root}", file=sys.stderr)
        return 2
    if shutil.which("ffprobe") is None:
        print("ERROR: ffprobe not found on PATH (install ffmpeg).",
              file=sys.stderr)
        return 2
    rows, files = scan_camcorders(args.root, args.min_size_mb)
    if not files:
        print(f"No camcorder files found under {args.root}; "
              f"no report written.", file=sys.stderr)
        return 2
    csv_path, json_path = _write_report(rows, args.out_dir, args.stem)
    n_err = sum(1 for r in rows if r.get("Error"))
    n_side = sum(len(r.get("Sidecar_Files") or []) for r in rows)
    print(f"Found {len(files)} camcorder file(s) ({n_err} with errors), "
          f"{n_side} sidecar file(s).")
    print(f"Report CSV:  {csv_path}")
    print(f"Report JSON: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
