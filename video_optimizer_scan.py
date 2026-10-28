#!/usr/bin/env python3
"""
video_optimizer_scan.py
=======================
Recursively scan a directory for video files, extract metadata with ffprobe,
and emit a CSV + JSON report flagging files that are prime candidates for
storage optimization (e.g. H.264 -> HEVC/H.265 transcodes).

Engine
------
* ffprobe via :mod:`subprocess`, JSON output for reliable parsing.
* :class:`concurrent.futures.ThreadPoolExecutor` for parallel probing.
* Results are aggregated in the main thread (as_completed) and written once
  at the end, so no thread-safe file appending is needed at all.

Resilience
----------
* Every subprocess call and every file open uses encoding="utf-8"
  (with errors="replace" on subprocess reads) to avoid cp1252
  UnicodeDecodeError crashes on Windows.
* Missing metadata fields are tolerated: the probe is marked with an
  ``Error`` column and the scan continues.
* Per-file timeouts + graceful handling of corrupt/empty files.

Usage
-----
    python video_optimizer_scan.py "D:\\Media\\Movies" --threads 8
    python video_optimizer_scan.py /mnt/nas/videos --out-dir reports --min-size-mb 50

Dependencies: none (stdlib only). Optional: tqdm for a progress bar.
ffprobe (part of ffmpeg) must be installed and on PATH.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional progress bar
# ---------------------------------------------------------------------------
try:  # tqdm is a nice-to-have, not a hard dependency
    from tqdm import tqdm

    _HAS_TQDM = True
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore
    _HAS_TQDM = False

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".3gp", ".ogv",
    ".asf", ".rmvb",
}

# Heuristic "efficient H.264 bitrate ceiling" per vertical resolution (Mbps).
# A file whose H.264 bitrate sits well above these is spending bits that a
# modern codec (HEVC/AV1) would not need -- a prime transcode candidate.
BITRATE_TARGETS_MBPS = {
    2160: 15.0,   # 4K UHD
    1440: 8.0,    # 1440p
    1080: 4.5,    # 1080p
    720: 3.0,     # 720p
    480: 1.5,     # 480p
    360: 1.0,     # 360p / SD
}

FFPROBE_TIMEOUT_S = 120  # per-file ceiling; stuck probes don't stall the scan

# Estimated efficient HEVC bitrates (Mbps) per vertical resolution, used for
# the dry-run savings estimate. CONSERVATIVE targets, patched 2026-10-25:
# roughly two-thirds of the H.264 targets (~33% saving), not half.
# The old ~50% targets (e.g. 1080p at 2.25 Mbps) overpromised savings and
# risked smearing fine detail (miniatures, texture, grain) if anyone
# treated the estimate as an encode bitrate. These targets are an
# estimate only -- the converter encodes at constant quality (CRF/CQ),
# never at a fixed bitrate, so actual output bitrate varies with content.
HEVC_TARGETS_MBPS = {
    2160: 10.0,   # 4K UHD (was 8.0)
    1440: 5.5,    # 1440p (was 4.0)
    1080: 3.0,    # 1080p (was 2.25)
    720: 2.0,     # 720p (was 1.5)
    480: 1.0,     # 480p (was 0.75)
    360: 0.7,     # 360p / SD (was 0.5)
}

# Codecs where an HEVC transcode is the expected win. Modern codecs still get
# an estimate (a bloated encode is bloated regardless of codec), but expect
# diminishing returns and a generation-loss tradeoff there.
LEGACY_VIDEO_CODECS = {"H.264", "MPEG-4", "MPEG-2", "MPEG-1", "WMV", "VC-1"}

CSV_COLUMNS = [
    "Path", "Parent_Directory", "Filename",
    "Size_MB", "Size_GB",
    "Duration", "Duration_Seconds",
    "Container_Format",
    "Video_Codec", "Resolution", "Width", "Height", "Aspect_Ratio",
    "FPS", "Pixel_Format", "Bit_Depth", "Video_Bitrate_Mbps",
    "Audio_Codecs", "Audio_Channels", "Audio_Bitrate_Mbps",
    "Audio_Stream_Count", "Audio_Languages",
    "Subtitle_Stream_Count", "Subtitle_Codecs", "Subtitle_Languages",
    "Overall_Bitrate_Mbps",
    "Optimization_Candidate", "Candidate_Reason",
    "Bloat_Flags",
    "Cropdetect_Crop", "Crop_Bars",
    "Crop_Top_Px", "Crop_Bottom_Px", "Crop_Left_Px", "Crop_Right_Px",
    "Crop_Confidence_Pct",
    "Estimated_HEVC_Bitrate_Mbps", "Estimated_New_Size_GB",
    "Estimated_Savings_GB", "Estimated_Savings_Pct",
    # Checklist columns, filled by hevc_convert.py and preserved across re-scans
    "Conversion_Status", "Converted_At_UTC", "Output_Path", "Actual_Savings_GB",
    "Error",
]

# Rows hevc_convert.py marks as done; the scanner preserves these per path.
CONVERTED_STATES = {"converted", "converted_no_subs"}

# Serialize console output so threaded progress logs never interleave.
_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# ffprobe plumbing
# ---------------------------------------------------------------------------
def probe_file(path: Path) -> dict:
    """Run ffprobe on *path* and return the parsed JSON dict.

    Raises RuntimeError with a human-readable message on any failure so the
    caller can record the error in the report instead of crashing.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",   # NEVER rely on locale default (cp1252 on Windows)
            errors="replace",   # replace undecodable bytes instead of raising
            timeout=FFPROBE_TIMEOUT_S,
        )
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


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------
def _parse_fps(value: object) -> float | None:
    """Parse ffprobe's avg_frame_rate ('30000/1001', '25/1', '0/0') to float."""
    if not value or not isinstance(value, str):
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            num, den = float(num), float(den)
            return num / den if den else None
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


def _aspect_ratio(stream: dict, width: int | None, height: int | None) -> str | None:
    """Prefer the container's display_aspect_ratio; fall back to reduced WxH."""
    dar = stream.get("display_aspect_ratio")
    if dar and dar not in ("0:0", "N/A"):
        return str(dar)
    if width and height:
        g = math.gcd(width, height)
        return f"{width // g}:{height // g}"
    return None


def _bit_depth(pix_fmt: str | None) -> int | None:
    """Infer bit depth from pixel format name ('yuv420p10le' -> 10)."""
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


def _codec_label(codec_name: str | None) -> str | None:
    """Normalize codec names to the labels the heuristic understands."""
    if not codec_name:
        return None
    c = codec_name.strip().lower()
    return {
        "h264": "H.264", "avc": "H.264", "avc1": "H.264",
        "hevc": "HEVC", "h265": "HEVC", "hev1": "HEVC", "hvc1": "HEVC",
        "av1": "AV1", "av01": "AV1",
        "vp9": "VP9",
        "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1",
        "wmv3": "WMV", "vc1": "VC-1",
        "aac": "AAC", "ac3": "AC3", "eac3": "E-AC3",
        "truehd": "TrueHD", "dts": "DTS", "dca": "DTS",
        "mp3": "MP3", "flac": "FLAC", "pcm_s16le": "PCM", "opus": "Opus",
        "subrip": "SubRip", "srt": "SubRip",
        "ass": "ASS", "ssa": "SSA",
        "mov_text": "mov_text", "tx3g": "mov_text",
        "hdmv_pgs_subtitle": "PGS", "dvd_subtitle": "DVD",
        "webvtt": "WebVTT", "dvb_subtitle": "DVB",
    }.get(c, codec_name.upper())


def _stream_language(stream: dict) -> str:
    """ISO language tag from stream tags, or '?' when untagged.

    ffprobe reports e.g. 'eng', 'und' (undetermined). '?' is used
    instead of guessing when the container carries no tag at all --
    which is common for AAC tracks in MKV, as in the 2026-10-25
    Warhammer report where every audio track showed 'bitrate?' and
    no language.
    """
    tags = stream.get("tags") or {}
    return str(tags.get("language") or "?").strip() or "?"


def _stream_title(stream: dict) -> str | None:
    tags = stream.get("tags") or {}
    title = tags.get("title")
    return str(title).strip() if title and str(title).strip() else None


def _disposition_flags(stream: dict) -> list[str]:
    """Disposition flags that matter for replacement decisions.

    'default' = player picks this track by default; 'forced' = forced
    subtitles (e.g. alien dialogue only); 'hearing_impaired' /
    'visual_impaired' = accessibility tracks. Only flags set to 1
    are listed, in a stable order.
    """
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


LOSSLESS_AUDIO_CODECS = {"flac", "alac", "truehd", "ape", "wavpack"}


def _is_lossless_audio(stream: dict) -> bool:
    """True for uncompressed/lossless audio: PCM, FLAC, ALAC, TrueHD,
    DTS-HD (profile carries the HD marker; plain DTS is lossy)."""
    raw = (stream.get("codec_name") or "").strip().lower()
    if raw.startswith("pcm") or raw in LOSSLESS_AUDIO_CODECS:
        return True
    if raw in ("dts", "dca"):
        profile = (stream.get("profile") or "").upper()
        return "HD" in profile  # DTS-HD MA / DTS-HD HRA
    return False


def compute_bloat_flags(row: dict, audio_streams: list[dict]) -> list[str]:
    """Bloat flags for one row, from ffprobe stream data (never guessed).

    Values are "none" in the report when no flag applies; rows from
    older reports (no Bloat_Flags column at all) are unknown, not none.
    Baked-in black bars ARE flagged here, but only when cropdetect
    ran (opt-in --cropdetect): without it, Crop_Bars is None and no
    token is added. Deliberately NOT flagged, because this scan
    cannot establish it: CBR vs VBR (ffprobe reports no reliable
    bitrate mode for most containers).
    A flag is a candidate for review, not an instruction to delete:
    a 7.1 track or a second language may be exactly what the owner
    wants, and the converter still copies audio unchanged -- these
    flags do not change any encoding, only the report.
    """
    flags: list[str] = []

    # ---- video: bitrate far above the target ---------------------------
    # Deliberately NOT a legacy-codec flag: every H.264 file would
    # then be "flagged", drowning the audio/bitrate signal, and the
    # candidate column already covers legacy codecs. Bloat flags are
    # for the extra waste on top of the codec choice.
    codec = (row.get("Video_Codec") or "").upper()
    vbr = row.get("Video_Bitrate_Mbps")
    height = row.get("Height")
    target = _target_for_height(height if isinstance(height, int) else None)
    if codec in LEGACY_VIDEO_CODECS:
        if vbr and target and vbr >= 1.5 * target:
            flags.append(f"high-video-bitrate:{vbr:.1f}Mbps-vs-{target:.1f}Mbps-target")
    elif codec in ("HEVC", "VP9", "AV1"):
        if vbr and target and vbr >= 2.0 * target:
            flags.append(f"high-video-bitrate:{vbr:.1f}Mbps-vs-{target:.1f}Mbps-target")

    # ---- audio: per-track ------------------------------------------------
    for a in audio_streams:
        label = _codec_label(a.get("codec_name")) or "?"
        lossless = _is_lossless_audio(a)
        if lossless:
            flags.append(f"lossless-audio:{label}")
        try:
            sr = int(a.get("sample_rate") or 0)
        except (TypeError, ValueError):
            sr = 0
        if sr > 48000:
            flags.append(f"high-sample-rate:{sr}Hz")
        ch = a.get("channels")
        if isinstance(ch, int) and ch >= 8:  # 7.1 and up
            flags.append(f"surround-{ch}ch")
        # High lossy bitrate only: lossless tracks are already flagged
        # above, and their bitrate is inherent, not a separate surprise.
        br = _kbps_to_mbps(a.get("bit_rate"))
        if br and br > 1.0 and not lossless:
            flags.append(f"high-audio-bitrate:{br:.2f}Mbps")

    # ---- baked-in black bars (only when cropdetect ran) ----------------
    if row.get("Crop_Bars") in ("letterbox", "pillarbox", "both"):
        flags.append(f"baked-in-bars:{row.get('Cropdetect_Crop') or row['Crop_Bars']}")

    # ---- audio: whole-file ------------------------------------------------
    if len(audio_streams) >= 3:
        flags.append(f"multi-audio:{len(audio_streams)}-tracks")
    langs = sorted({_stream_language(a) for a in audio_streams
                    if _stream_language(a) not in ("?", "und", "")})
    if len(langs) >= 2:
        flags.append(f"multi-language:{'/'.join(langs)}")

    # Stable order, no duplicates (two PCM tracks -> one flag each is
    # fine, but two identical sample-rate flags are noise).
    seen: set[str] = set()
    out: list[str] = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _fmt_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Cropdetect (baked-in black bars) -- opt-in, sampled, never whole-file
# ---------------------------------------------------------------------------
_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def sample_cropdetect(path: Path, duration: float, samples: int,
                      limit: int, frames_per_sample: int = 5) -> list[tuple[int, int, int, int]]:
    """Run ffmpeg cropdetect at *samples* evenly spaced timestamps.

    Fast seek (-ss BEFORE -i) on purpose: bars are geometry, not a
    moment, so a keyframe offset of a few seconds is irrelevant, and
    accurate seek would decode from the start for every sample.
    cropdetect emits nothing for the first frame it sees, hence
    frames_per_sample > 1. Returns every (w, h, x, y) line seen,
    across all samples, in order.
    """
    crops: list[tuple[int, int, int, int]] = []
    for i in range(samples):
        t = duration * (i + 0.5) / samples  # midpoints, like stills --count
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y",
               "-ss", str(round(t, 3)), "-i", str(path),
               "-vf", f"cropdetect=limit={limit}:round=2:reset=0",
               "-frames:v", str(frames_per_sample), "-f", "null", "-"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        for m in _CROP_RE.finditer(proc.stderr or ""):
            crops.append(tuple(int(g) for g in m.groups()))  # type: ignore[arg-type]
    return crops


def aggregate_cropdetect(crops: list[tuple[int, int, int, int]],
                         width: int, height: int) -> dict:
    """Aggregate sampled crop rectangles into one verdict.

    Rules, all chosen from the 2026-10-25 synthetic tests:
    - Discard crops covering <10% of the frame: a fade, a black
      frame, or a single bright dot is not a bar measurement.
      (First version used <50% here, which wrongly discarded a
      legitimate 800px-wide pillarbox -- 42% of a 1920x1080 frame.)
    - Take the LARGEST-area surviving crop as the content rectangle.
      A single frame can under-report (dark content at the edges
      shrank a letterboxed testsrc to 1680px wide at one timestamp),
      but the true rectangle is the union over time, not the
      intersection -- bright frames eventually fill the edges.
    - Require >=3 valid crops and the largest crop reached (within
      2% of its area) by >=2 of them; otherwise "inconsistent".
      That keeps a one-off dark scene from being reported as bars.
    - Flag bars only when the missing dimension is >=8% of the
      stored dimension, so a few pixels of rounding don't flag.
    Returns Crop_* values (Cropdetect_Crop is None when inconsistent).
    """
    full_area = width * height
    valid = [(w, h, x, y) for (w, h, x, y) in crops
             if 0 < w <= width and 0 < h <= height and x + w <= width
             and y + h <= height and w * h >= 0.10 * full_area]
    out: dict = {"Cropdetect_Crop": None, "Crop_Bars": "inconsistent",
                 "Crop_Top_Px": None, "Crop_Bottom_Px": None,
                 "Crop_Left_Px": None, "Crop_Right_Px": None,
                 "Crop_Confidence_Pct": None}
    if not valid:
        return out
    largest = max(valid, key=lambda c: c[0] * c[1])
    largest_area = largest[0] * largest[1]
    agreeing = sum(1 for c in valid if c[0] * c[1] >= 0.98 * largest_area)
    out["Crop_Confidence_Pct"] = round(100 * agreeing / len(valid))
    if len(valid) < 3 or agreeing < 2:
        return out
    w, h, x, y = largest
    top, bottom = y, height - (y + h)
    left, right = x, width - (x + w)
    out.update({"Cropdetect_Crop": f"{w}x{h}+{x}+{y}",
                "Crop_Top_Px": top, "Crop_Bottom_Px": bottom,
                "Crop_Left_Px": left, "Crop_Right_Px": right})
    letterbox = (top + bottom) >= 0.08 * height
    pillarbox = (left + right) >= 0.08 * width
    out["Crop_Bars"] = ("both" if letterbox and pillarbox else
                        "letterbox" if letterbox else
                        "pillarbox" if pillarbox else "none")
    return out


# ---------------------------------------------------------------------------
# Row extraction
# ---------------------------------------------------------------------------
def extract_row(path: Path, crop_opts: dict | None = None) -> dict:
    """Build one report row for *path*; errors become an Error column value."""
    row: dict = {col: None for col in CSV_COLUMNS}
    row["Path"] = str(path.resolve())
    row["Parent_Directory"] = str(path.parent.resolve())
    row["Filename"] = path.name

    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        row["Error"] = f"Cannot stat file: {exc}"
        return row
    row["Size_MB"] = round(size_bytes / (1024 ** 2), 2)
    row["Size_GB"] = round(size_bytes / (1024 ** 3), 3)

    try:
        info = probe_file(path)
    except RuntimeError as exc:
        row["Error"] = str(exc)
        return row

    fmt = info.get("format", {}) or {}
    streams = info.get("streams", []) or []

    # ---- container / duration -------------------------------------------
    container = fmt.get("format_name", "")
    row["Container_Format"] = container.split(",")[0].upper() if container else None
    try:
        dur = float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
    except (TypeError, ValueError):
        dur = None
    row["Duration_Seconds"] = round(dur, 1) if dur else None
    row["Duration"] = _fmt_duration(dur)
    row["Overall_Bitrate_Mbps"] = _kbps_to_mbps(fmt.get("bit_rate"))

    # ---- video stream (first/main video stream) --------------------------
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    v = video_streams[0] if video_streams else None
    if v:
        row["Video_Codec"] = _codec_label(v.get("codec_name"))
        w = v.get("width")
        h = v.get("height")
        row["Width"] = w if isinstance(w, int) else None
        row["Height"] = h if isinstance(h, int) else None
        if isinstance(w, int) and isinstance(h, int):
            row["Resolution"] = f"{w}x{h}"
        row["Aspect_Ratio"] = _aspect_ratio(
            v, row["Width"], row["Height"]
        )
        fps = _parse_fps(v.get("avg_frame_rate")) or _parse_fps(v.get("r_frame_rate"))
        row["FPS"] = round(fps, 3) if fps else None
        row["Pixel_Format"] = v.get("pix_fmt")
        row["Bit_Depth"] = _bit_depth(v.get("pix_fmt"))

        # Video bitrate: prefer the stream value; else derive from the
        # container total minus the audio streams (common when the muxer
        # only writes a global bit_rate).
        vbr = _kbps_to_mbps(v.get("bit_rate"))
        if vbr is None and row["Overall_Bitrate_Mbps"]:
            audio_total = sum(
                float(a.get("bit_rate") or 0)
                for a in streams
                if a.get("codec_type") == "audio" and a.get("bit_rate")
            ) / 1_000_000
            vbr = round(row["Overall_Bitrate_Mbps"] - audio_total, 3)
        row["Video_Bitrate_Mbps"] = vbr if (vbr and vbr > 0) else None

    # ---- audio streams ----------------------------------------------------
    # Full per-stream detail, not just codec names: language, sample
    # rate and title are what tell you whether a file has e.g. a
    # commentary track, a second language, or just one stereo track.
    # Bitrate is often absent for AAC in MKV (ffprobe reports no
    # stream bit_rate) -- that stays 'bitrate?' rather than a guess.
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    row["Audio_Stream_Count"] = len(audio_streams)
    if audio_streams:
        descs = []
        total_mbps = 0.0
        channels_seen: list[int] = []
        for a in audio_streams:
            codec = _codec_label(a.get("codec_name")) or "?"
            ch = a.get("channels")
            br = _kbps_to_mbps(a.get("bit_rate"))
            if br:
                total_mbps += br
            if isinstance(ch, int):
                channels_seen.append(ch)
            ch_txt = f"{ch}ch" if isinstance(ch, int) else "?"
            sr = a.get("sample_rate")
            sr_txt = f"{sr} Hz" if sr else "?"
            lang = _stream_language(a)
            br_txt = f"{br:.2f} Mbps" if br else "bitrate?"
            title = _stream_title(a)
            title_txt = f", '{title}'" if title else ""
            descs.append(f"{codec} ({ch_txt}, {sr_txt}, {lang}, "
                         f"{br_txt}{title_txt})")
        row["Audio_Codecs"] = " | ".join(descs)
        row["Audio_Channels"] = max(channels_seen) if channels_seen else None
        row["Audio_Bitrate_Mbps"] = round(total_mbps, 3) if total_mbps > 0 else None
        row["Audio_Languages"] = " | ".join(
            _stream_language(a) for a in audio_streams)
    else:
        row["Audio_Languages"] = None

    # ---- subtitle streams -------------------------------------------------
    # Previously invisible in the report -- yet subtitles are the
    # stream most likely to be lost in a conversion (a container that
    # cannot hold PGS subs forces the converter's -sn fallback, logged
    # as converted_no_subs). Report codec + language + disposition
    # per track so a replace decision can see exactly what is at stake.
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    row["Subtitle_Stream_Count"] = len(subtitle_streams)
    if subtitle_streams:
        sub_descs = []
        for s in subtitle_streams:
            codec = _codec_label(s.get("codec_name")) or "?"
            lang = _stream_language(s)
            flags = _disposition_flags(s)
            flags_txt = f", {', '.join(flags)}" if flags else ""
            title = _stream_title(s)
            title_txt = f", '{title}'" if title else ""
            sub_descs.append(f"{codec} ({lang}{flags_txt}{title_txt})")
        row["Subtitle_Codecs"] = " | ".join(sub_descs)
        row["Subtitle_Languages"] = " | ".join(
            _stream_language(s) for s in subtitle_streams)
    else:
        row["Subtitle_Codecs"] = None
        row["Subtitle_Languages"] = None

    # ---- cropdetect (opt-in only) --------------------------------------
    # Off by default: this decodes frames, unlike the ffprobe-only
    # scan. When enabled, columns are filled; when disabled, or when
    # duration/resolution is unknown, they stay None (unknown).
    # Computed BEFORE the bloat flags so a flagged crop can add a
    # baked-in-bars token there.
    if crop_opts and v and row["Width"] and row["Height"] and dur:
        try:
            crops = sample_cropdetect(path, dur, crop_opts["samples"],
                                      crop_opts["limit"])
            row.update(aggregate_cropdetect(crops, row["Width"], row["Height"]))
        except Exception:  # noqa: BLE001 - a failed crop run must not kill the row
            row["Crop_Bars"] = "inconsistent"

    # ---- heuristic ---------------------------------------------------------
    candidate, reason = classify_candidate(row)
    row["Optimization_Candidate"] = candidate
    row["Candidate_Reason"] = reason or None

    # ---- bloat flags -------------------------------------------------------
    # "none" = scanned and nothing flagged. Error rows return before
    # this point, so their Bloat_Flags stays None (unknown), and rows
    # from older reports have no column at all (also unknown, not none).
    bloat = compute_bloat_flags(row, audio_streams)
    row["Bloat_Flags"] = " | ".join(bloat) if bloat else "none"

    # ---- dry-run savings estimate -----------------------------------------
    row.update(estimate_hevc_savings(row))
    return row


def estimate_hevc_savings(row: dict) -> dict:
    """Dry-run estimate: space saved by re-encoding video to efficient HEVC.

    Nothing is transcoded. The estimate assumes a CRF-based HEVC re-encode at
    equivalent perceptual quality at the conservative HEVC_TARGETS_MBPS
    rate for the resolution (~two-thirds of the H.264 target, ~33%
    saving; see HEVC_TARGETS_MBPS). Audio is left untouched, so savings
    come from the video stream only.

    Returns the four Estimated_* columns; all None when the estimate can't be
    made (missing bitrate or duration). Never negative -- an already-efficient
    file simply estimates ~0 savings.
    """
    out = {
        "Estimated_HEVC_Bitrate_Mbps": None,
        "Estimated_New_Size_GB": None,
        "Estimated_Savings_GB": None,
        "Estimated_Savings_Pct": None,
    }
    vbr = row.get("Video_Bitrate_Mbps")      # Mbps
    dur = row.get("Duration_Seconds")        # seconds
    size_gb = row.get("Size_GB") or 0.0
    if not vbr or not dur or not size_gb:
        return out

    height = row.get("Height")
    target = _target_for_height(
        height if isinstance(height, int) else None,
        table=HEVC_TARGETS_MBPS, default=3.0,  # unknown res -> 1080p target
    )
    out["Estimated_HEVC_Bitrate_Mbps"] = target

    video_bytes = vbr * 1_000_000 * dur / 8
    new_video_bytes = target * 1_000_000 * dur / 8
    savings_bytes = max(0.0, video_bytes - new_video_bytes)

    size_bytes = size_gb * 1024 ** 3
    # Sanity clamp: you can't save more than the non-audio portion of the
    # file. This guards against inconsistent container metadata (e.g. a wrong
    # header bitrate on a VBR file) producing >100% "savings".
    audio_bytes = (row.get("Audio_Bitrate_Mbps") or 0.0) * 1_000_000 * dur / 8
    savings_bytes = min(savings_bytes, max(0.0, size_bytes - audio_bytes))

    out["Estimated_New_Size_GB"] = round(max(0.0, size_gb - savings_bytes / 1024 ** 3), 3)
    out["Estimated_Savings_GB"] = round(savings_bytes / 1024 ** 3, 3)
    out["Estimated_Savings_Pct"] = round(100 * savings_bytes / size_bytes, 1)
    return out


# ---------------------------------------------------------------------------
# Optimization heuristic
# ---------------------------------------------------------------------------
def _target_for_height(height: int | None,
                       table: dict[int, float] = BITRATE_TARGETS_MBPS,
                       default: float = 4.5) -> float:
    """Efficient-bitrate target (Mbps) for a given frame height and table."""
    if not height:
        return default  # unknown resolution -> treat as 1080p target
    for res, target in sorted(table.items()):
        if height <= res:
            return target
    return table[max(table)]


def classify_candidate(row: dict) -> tuple[str, str]:
    """Return (priority, reason) for the Optimization_Candidate column.

    Priorities: "High Priority" | "Medium" | "Low".
    Files with no usable video metadata are "Low" with an explanatory reason.
    """
    if row.get("Error"):
        return "Low", f"Probe error: {row['Error']}"

    codec = (row.get("Video_Codec") or "").upper()
    vbr = row.get("Video_Bitrate_Mbps")  # Mbps
    dur_s = row.get("Duration_Seconds")  # seconds
    size_gb = row.get("Size_GB") or 0.0
    height = row.get("Height")
    target = _target_for_height(height if isinstance(height, int) else None)

    if not codec or vbr is None:
        return "Low", "Insufficient metadata to evaluate"

    reasons: list[str] = []
    priority = "Low"

    def _bump(level: str, reason: str) -> None:
        nonlocal priority
        order = {"Low": 0, "Medium": 1, "High Priority": 2}
        if order[level] > order[priority]:
            priority = level
        reasons.append(reason)

    # 1) Legacy codec at an inefficient bitrate -> the classic HEVC win.
    if codec in ("H.264", "MPEG-4", "MPEG-2", "WMV", "VC-1"):
        ratio = vbr / target if target else 0
        if ratio >= 1.5:
            _bump("High Priority",
                  f"{codec} at {vbr:.1f} Mbps is "
                  f"{ratio:.1f}x the ~{target:.1f} Mbps efficiency target "
                  f"for {height or '?'}p")
        elif ratio >= 1.0:
            _bump("Medium",
                  f"{codec} at {vbr:.1f} Mbps meets/exceeds the "
                  f"~{target:.1f} Mbps efficiency target for {height or '?'}p")

    # 2) Modern codec but still bloated -> may be worth a re-encode pass.
    elif codec in ("HEVC", "VP9", "AV1"):
        if vbr >= 2.0 * target:
            _bump("Medium",
                  f"{codec} at {vbr:.1f} Mbps is unusually high "
                  f"(2x+ the ~{target:.1f} Mbps target for {height or '?'}p)")

    # 3) Size relative to runtime: GB-per-hour exposes bloated encodes
    #    regardless of codec label. The ceiling is derived from the same
    #    efficiency target above: target_mbps * 3600s / 8bits / 1024 ~= Mbps*0.45.
    if dur_s and dur_s > 60:  # ignore clips under a minute
        gb_per_hour = size_gb / (dur_s / 3600)
        ceiling = target * 0.45
        if gb_per_hour >= 1.75 * ceiling:
            _bump("High Priority",
                  f"{gb_per_hour:.1f} GB/hr is far above the ~{ceiling:.1f} GB/hr "
                  f"norm for {codec or 'this codec'}")
        elif gb_per_hour >= ceiling:
            _bump("Medium",
                  f"{gb_per_hour:.1f} GB/hr exceeds the ~{ceiling:.1f} GB/hr "
                  f"norm for {codec or 'this codec'}")

    if not reasons:
        reasons.append(
            f"Efficient encode ({codec}"
            f"{f', {vbr:.1f} Mbps' if vbr else ''}"
            f"{f', {height}p' if height else ''})"
        )
    return priority, "; ".join(reasons)


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------
def find_video_files(root: Path, min_size_mb: float) -> list[Path]:
    """Recursively collect video files under *root*, smallest-first skipped."""
    min_bytes = min_size_mb * 1024 * 1024
    found: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            if p.stat().st_size < min_bytes:
                continue
        except OSError:
            continue  # unreadable file: skip quietly, don't abort the scan
        found.append(p)
    return sorted(found)


def scan_directory(
    root: Path, workers: int, min_size_mb: float,
    crop_opts: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """Probe all video files under *root* in parallel. Returns (rows, errors)."""
    files = find_video_files(root, min_size_mb)
    if not files:
        return [], []

    rows: list[dict] = []
    errors: list[str] = []
    done = 0

    progress = None
    if _HAS_TQDM:
        progress = tqdm(total=len(files), desc="Probing", unit="file")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_path = {pool.submit(extract_row, p, crop_opts): p for p in files}
        for fut in as_completed(future_to_path):
            p = future_to_path[fut]
            try:
                rows.append(fut.result())
            except Exception as exc:  # last-resort guard; loop must not die
                _log(f"[ERROR] Unexpected failure on {p}: {exc}")
                errors.append(f"{p}: {exc}")
            done += 1
            if progress is not None:
                progress.update(1)
            elif done % 25 == 0 or done == len(files):
                _log(f"  ... {done}/{len(files)} probed")
    if progress is not None:
        progress.close()
    return rows, errors


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------
def _jsonable(row: dict) -> dict:
    return {k: (v if v is not None else "") for k, v in row.items()}


def _load_prior_conversions(json_path: Path) -> dict[str, dict]:
    """Read Conversion_* checklist state from a previous report, keyed by Path."""
    if not json_path.is_file():
        return {}
    try:
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    cols = ["Conversion_Status", "Converted_At_UTC", "Output_Path",
            "Actual_Savings_GB"]
    out: dict[str, dict] = {}
    for r in data:
        if not isinstance(r, dict) or not r.get("Path"):
            continue
        kept = {c: r[c] for c in cols if r.get(c) not in (None, "")}
        if kept:
            out[r["Path"]] = kept
    return out


def write_reports(rows: list[dict], out_dir: Path, stem: str) -> tuple[Path, Path]:
    """Write CSV (utf-8-sig for Excel) and JSON (utf-8) reports. Returns paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"

    # Preserve the conversion checklist from a previous report so re-scans
    # don't wipe hevc_convert.py's progress (matched by absolute path).
    prior = _load_prior_conversions(json_path)
    if prior:
        for r in rows:
            conv = prior.get(r.get("Path") or "")
            if conv:
                r.update(conv)

    ordered = sorted(
        rows,
        key=lambda r: (r.get("Optimization_Candidate") != "High Priority",
                       -(r.get("Size_GB") or 0)),
    )

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in ordered:
            writer.writerow({c: ("" if r.get(c) is None else r.get(c))
                             for c in CSV_COLUMNS})

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump([_jsonable(r) for r in ordered], fh,
                  indent=2, ensure_ascii=False)

    return csv_path, json_path


def _res_bucket(height: object) -> str:
    if not isinstance(height, int):
        return "Unknown"
    if height >= 2000:
        return "4K UHD"
    if height >= 1000:
        return "1080p"
    if height >= 600:
        return "720p"
    return "SD"


def write_summary_report(rows: list[dict], out_dir: Path, stem: str,
                         scan_root: Path) -> Path:
    """Write a human-readable Markdown summary of the scan. Returns the path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}_summary.md"

    def md_table(headers: list[str], data: list[tuple]) -> str:
        lines = ["| " + " | ".join(headers) + " |",
                 "|" + "|".join(["---"] * len(headers)) + "|"]
        lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in data]
        return "\n".join(lines)

    ok = [r for r in rows if not r.get("Error")]
    errs = [r for r in rows if r.get("Error")]
    tot_gb = sum(r.get("Size_GB") or 0 for r in ok)
    tot_sav = sum(r.get("Estimated_Savings_GB") or 0 for r in ok)

    L: list[str] = []
    L.append("# Video Optimization Report — Summary\n")
    L.append(f"- Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    L.append(f"- Scan root: `{scan_root}`")
    L.append(f"- Files scanned: {len(rows)} ({len(errs)} probe errors)")
    L.append(f"- Total size: {tot_gb:.1f} GB")
    if tot_gb:
        L.append(f"- Estimated HEVC savings: {tot_sav:.1f} GB "
                 f"({100 * tot_sav / tot_gb:.0f}% of scanned)")

    L.append("\n## By candidate flag\n")
    flag_rows = []
    for flag in ("High Priority", "Medium", "Low"):
        grp = [r for r in ok if r.get("Optimization_Candidate") == flag]
        if grp:
            flag_rows.append((
                flag, len(grp),
                f"{sum(r.get('Size_GB') or 0 for r in grp):.1f} GB",
                f"{sum(r.get('Estimated_Savings_GB') or 0 for r in grp):.1f} GB",
            ))
    L.append(md_table(["Flag", "Files", "Size", "Est. savings"], flag_rows))

    L.append("\n## By video codec (top 10 by size)\n")
    by_codec: dict[str, list[dict]] = {}
    for r in ok:
        by_codec.setdefault(r.get("Video_Codec") or "Unknown", []).append(r)
    codec_rows = sorted(
        ((c, len(g), f"{sum(x.get('Size_GB') or 0 for x in g):.1f} GB",
          f"{sum(x.get('Estimated_Savings_GB') or 0 for x in g):.1f} GB")
         for c, g in by_codec.items()),
        key=lambda t: float(t[2].split()[0]), reverse=True)[:10]
    L.append(md_table(["Codec", "Files", "Size", "Est. savings"], codec_rows))

    L.append("\n## By resolution\n")
    by_res: dict[str, list[dict]] = {}
    for r in ok:
        by_res.setdefault(_res_bucket(r.get("Height")), []).append(r)
    res_rows = [
        (b, len(by_res[b]),
         f"{sum(x.get('Size_GB') or 0 for x in by_res[b]):.1f} GB",
         f"{sum(x.get('Estimated_Savings_GB') or 0 for x in by_res[b]):.1f} GB")
        for b in ("4K UHD", "1080p", "720p", "SD", "Unknown") if b in by_res
    ]
    L.append(md_table(["Resolution", "Files", "Size", "Est. savings"], res_rows))

    # Audio / subtitle makeup: the streams most likely to be lost or
    # changed by a conversion, summarised so the report alone shows
    # what a file is made of. Old reports (pre-2026-10-25) have no
    # Subtitle_* columns; those rows count as 'unknown', not 'none'.
    L.append("\n## Audio / subtitle makeup\n")
    with_subs = [r for r in ok if (r.get("Subtitle_Stream_Count") or 0) > 0]
    no_subs = [r for r in ok if r.get("Subtitle_Stream_Count") == 0]
    unknown_subs = [r for r in ok if r.get("Subtitle_Stream_Count") is None]
    L.append(f"- With subtitles: {len(with_subs)} files; "
             f"without: {len(no_subs)}; unknown (old report): {len(unknown_subs)}")
    # Subtitle codecs that cannot be copied into every container
    # (notably PGS into MP4) are the ones that force a -sn fallback.
    sub_codec_counts: dict[str, int] = {}
    for r in with_subs:
        for part in str(r.get("Subtitle_Codecs") or "").split(" | "):
            codec = part.split(" (")[0].strip()
            if codec:
                sub_codec_counts[codec] = sub_codec_counts.get(codec, 0) + 1
    if sub_codec_counts:
        L.append("- Subtitle codecs (tracks, not files): " +
                 ", ".join(f"{c} x{n}" for c, n in
                           sorted(sub_codec_counts.items(),
                                  key=lambda kv: -kv[1])))
    audio_lang_counts: dict[str, int] = {}
    for r in ok:
        for lang in str(r.get("Audio_Languages") or "").split(" | "):
            lang = lang.strip()
            if lang:
                audio_lang_counts[lang] = audio_lang_counts.get(lang, 0) + 1
    if audio_lang_counts:
        L.append("- Audio languages (tracks, not files): " +
                 ", ".join(f"{c} x{n}" for c, n in
                           sorted(audio_lang_counts.items(),
                                  key=lambda kv: -kv[1])))
    multi_audio = [r for r in ok if (r.get("Audio_Stream_Count") or 0) > 1]
    L.append(f"- Files with 2+ audio tracks: {len(multi_audio)}")

    # Bloat flags: counted by base flag (before the ":" parameter, so
    # e.g. high-sample-rate:96000Hz and :192000Hz count together).
    # "none" = scanned, nothing flagged; None / missing column = an
    # older report, unknown, not none. Flags change nothing about
    # encoding -- the converter still copies audio -- they only say
    # "look here first".
    L.append("\n## Bloat flags\n")
    flagged = [r for r in ok
               if r.get("Bloat_Flags") and r.get("Bloat_Flags") != "none"]
    unknown_bloat = [r for r in ok if r.get("Bloat_Flags") is None]
    L.append(f"- Files with at least one bloat flag: {len(flagged)} "
             f"({sum(r.get('Size_GB') or 0 for r in flagged):.1f} GB); "
             f"no flags: {len(ok) - len(flagged) - len(unknown_bloat)}; "
             f"unknown (old report): {len(unknown_bloat)}")
    flag_counts: dict[str, int] = {}
    for r in flagged:
        for token in str(r.get("Bloat_Flags") or "").split(" | "):
            base = token.split(":", 1)[0].strip()
            if base:
                flag_counts[base] = flag_counts.get(base, 0) + 1
    if flag_counts:
        L.append("- Flags (files, not tracks): " +
                 ", ".join(f"{c} x{n}" for c, n in
                           sorted(flag_counts.items(), key=lambda kv: -kv[1])))
    L.append("- Not flagged by this scan: CBR vs VBR (ffprobe reports no "
             "reliable bitrate mode). Baked-in black bars are flagged "
             "only when the scan ran with --cropdetect; otherwise "
             "Crop_Bars is unknown, not none.")

    # Cropdetect: only meaningful when the scan ran with --cropdetect.
    # Unknown = no crop run (flag off, or unprobable file), not "no bars".
    L.append("\n## Baked-in black bars (cropdetect)\n")
    crop_known = [r for r in ok if r.get("Crop_Bars") is not None]
    if crop_known:
        for bars in ("letterbox", "pillarbox", "both", "none", "inconsistent"):
            grp = [r for r in crop_known if r.get("Crop_Bars") == bars]
            if grp:
                L.append(f"- {bars}: {len(grp)} files "
                         f"({sum(r.get('Size_GB') or 0 for r in grp):.1f} GB)")
        L.append("- A crop flag says the bars are baked in; it does not "
                 "promise a saving from cropping them (flat black is "
                 "already cheap at CRF), and cropping is not done by "
                 "the converter -- it is a separate, destructive step "
                 "that can cut subtitles positioned in the bars.")
    else:
        L.append("- Not run: re-scan with --cropdetect to populate "
                 "Crop_* columns (Crop_Bars is unknown, not none).")

    done = [r for r in ok
            if (r.get("Conversion_Status") or "") in CONVERTED_STATES]
    if done:
        L.append("\n## Conversion progress\n")
        L.append(f"- Converted: {len(done)} files, "
                 f"{sum(r.get('Actual_Savings_GB') or 0 for r in done):.1f} GB "
                 f"actually saved")

    L.append("\n## Top 15 by estimated savings\n")
    top_sav = sorted(ok, key=lambda r: -(r.get("Estimated_Savings_GB") or 0))[:15]
    L.append(md_table(
        ["#", "Est. save", "Size", "Codec", "Res", "File"],
        [(i + 1, f"{r.get('Estimated_Savings_GB') or 0:.2f} GB",
          f"{r.get('Size_GB') or 0:.2f} GB",
          r.get("Video_Codec") or "?", r.get("Resolution") or "?",
          r.get("Filename") or "")
         for i, r in enumerate(top_sav)]))

    L.append("\n## Top 15 by size\n")
    top_size = sorted(ok, key=lambda r: -(r.get("Size_GB") or 0))[:15]
    L.append(md_table(
        ["#", "Size", "Codec", "Res", "Candidate", "File"],
        [(i + 1, f"{r.get('Size_GB') or 0:.2f} GB",
          r.get("Video_Codec") or "?", r.get("Resolution") or "?",
          r.get("Optimization_Candidate") or "?",
          r.get("Filename") or "")
         for i, r in enumerate(top_size)]))

    if errs:
        L.append("\n## Probe errors (first 20)\n")
        L.append(md_table(
            ["File", "Error"],
            [(r.get("Filename") or "?", str(r.get("Error"))[:100])
             for r in errs[:20]]))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return path


def print_summary(rows: list[dict]) -> None:
    total = len(rows)
    high = sum(1 for r in rows if r.get("Optimization_Candidate") == "High Priority")
    med = sum(1 for r in rows if r.get("Optimization_Candidate") == "Medium")
    errs = sum(1 for r in rows if r.get("Error"))
    gb_total = sum(r.get("Size_GB") or 0 for r in rows)
    gb_high = sum(r.get("Size_GB") or 0 for r in rows
                  if r.get("Optimization_Candidate") == "High Priority")
    sav_total = sum(r.get("Estimated_Savings_GB") or 0 for r in rows)
    sav_high = sum(r.get("Estimated_Savings_GB") or 0 for r in rows
                   if r.get("Optimization_Candidate") == "High Priority")
    done_conv = [r for r in rows
                 if (r.get("Conversion_Status") or "") in CONVERTED_STATES]
    sav_actual = sum(r.get("Actual_Savings_GB") or 0 for r in done_conv)
    print("\n==== Scan summary ====")
    print(f"Files scanned:            {total}")
    print(f"Total size:               {gb_total:.1f} GB")
    print(f"High Priority candidates: {high}  ({gb_high:.1f} GB)")
    print(f"Medium candidates:        {med}")
    flagged_n = sum(1 for r in rows
                    if r.get("Bloat_Flags") and r.get("Bloat_Flags") != "none")
    if flagged_n:
        print(f"Files with bloat flags:   {flagged_n}")
    barred = [r for r in rows if r.get("Crop_Bars") in ("letterbox", "pillarbox", "both")]
    if barred:
        print(f"Files with baked-in bars: {len(barred)}")
    print(f"Probe errors:             {errs}")
    if gb_total:
        print(f"Est. HEVC savings total:  {sav_total:.1f} GB "
              f"({sav_total / gb_total * 100:.0f}% of scanned)")
    print(f"Est. savings (High Pri.): {sav_high:.1f} GB")
    if done_conv:
        print(f"Already converted:        {len(done_conv)} files, "
              f"{sav_actual:.1f} GB actually saved")
    if high:
        print("\nTop High Priority files:")
        top = sorted(
            (r for r in rows if r.get("Optimization_Candidate") == "High Priority"),
            key=lambda r: -(r.get("Size_GB") or 0),
        )[:10]
        for r in top:
            print(f"  {r['Size_GB']:>8.2f} GB  {r['Video_Codec'] or '?':<6} "
                  f"{r['Resolution'] or '?':<10} {r['Filename']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Scan videos with ffprobe and flag transcode candidates."
    )
    # Made optional so the script can prompt the user if omitted
    ap.add_argument("--dir", dest="directory", type=Path, default=None,
                    help="Root directory to scan recursively (prompts if omitted)")
    ap.add_argument("--threads", type=int, default=8,
                    help="Parallel probe workers (default: 8)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Where to write the CSV/JSON (prompts if omitted)")
    ap.add_argument("--stem", default="video_optimization_report",
                    help="Report filename stem (default: video_optimization_report)")
    ap.add_argument("--min-size-mb", type=float, default=0,
                    help="Skip files smaller than this many MB (default: 0)")
    ap.add_argument("--cropdetect", action="store_true",
                    help="Also run sampled ffmpeg cropdetect per file to "
                         "detect baked-in black bars (decodes frames, so "
                         "slower than the ffprobe-only scan; off by default)")
    ap.add_argument("--crop-samples", type=int, default=8,
                    help="Cropdetect sample timestamps per file (default: 8)")
    ap.add_argument("--crop-limit", type=int, default=24,
                    help="Cropdetect black threshold 0-255 (default: 24)")

    args = ap.parse_args(argv)

    # 1. Interactive prompt for Input Directory
    while not args.directory or not args.directory.is_dir():
        if args.directory is not None:
            print(f"[WARN] Directory not found: {args.directory}")
        user_in = input(r"Enter the input directory to scan (e.g., G:\data\media\tv): ").strip()
        
        # Strip quotes in case of terminal drag-and-drop, expand '~' for Unix paths
        clean_path = user_in.strip("\"'")
        if clean_path:
            args.directory = Path(clean_path).expanduser().resolve()

    # 2. Interactive prompt for Output Directory
    if not args.out_dir:
        user_out = input(r"Enter the output directory for reports [default: reports]: ").strip()
        clean_out = user_out.strip("\"'")
        if clean_out:
            args.out_dir = Path(clean_out).expanduser().resolve()
        else:
            args.out_dir = Path("reports").resolve()

    return args


def main(argv: list[str] | None = None) -> int:
    if not shutil.which("ffprobe"):
        print("ERROR: ffprobe not found on PATH. Install ffmpeg first:",
              file=sys.stderr)
        print("  Windows:  winget install Gyan.FFmpeg  (or choco install ffmpeg)",
              file=sys.stderr)
        print("  macOS:    brew install ffmpeg", file=sys.stderr)
        print("  Linux:    sudo apt install ffmpeg", file=sys.stderr)
        return 2

    # Parse args (will prompt here if directories weren't passed via CLI)
    args = parse_args(argv)

    crop_opts = None
    if args.cropdetect:
        if not shutil.which("ffmpeg"):
            print("ERROR: --cropdetect needs ffmpeg on PATH.", file=sys.stderr)
            return 2
        if args.crop_samples < 1 or not 0 <= args.crop_limit <= 255:
            print("ERROR: --crop-samples must be >= 1 and --crop-limit "
                  "in 0-255.", file=sys.stderr)
            return 2
        crop_opts = {"samples": args.crop_samples, "limit": args.crop_limit}

    _log(f"\nScanning {args.directory} with {args.threads} workers ...")
    rows, fatal_errors = scan_directory(args.directory, args.threads,
                                        args.min_size_mb, crop_opts)
    if fatal_errors:
        _log(f"[WARN] {len(fatal_errors)} files failed unexpectedly "
             "(see console output).")

    if not rows:
        _log("No video files found. Nothing to report.")
        return 0

    csv_path, json_path = write_reports(rows, args.out_dir, args.stem)
    summary_path = write_summary_report(rows, args.out_dir, args.stem,
                                        args.directory)
    _log(f"\nCSV report:  {csv_path}")
    _log(f"JSON report: {json_path}")
    _log(f"Summary:     {summary_path}")
    print_summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())