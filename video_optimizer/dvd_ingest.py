#!/usr/bin/env python3
"""DVD ingest for the HEVC pipeline: VIDEO_TS -> scanner-compatible jobs.

This is the front end the old ``ss-py-ffmpeg-conversions`` scripts
needed, folded into the suite instead of becoming a second HEVC
encoder. It does ONLY the DVD-specific work:

  * discover ``VIDEO_TS`` directories under a root (case-insensitive);
  * group files into Video Title Sets, skipping the ``_0`` menu VOBs
    for encoding while keeping them as menu-preview candidates;
  * read IFO headers in pure Python (no MediaInfo dependency -- the
    old script's MediaInfo chapter count was wrong: it counted the
    keys of the menu track's ``to_data()`` dict, not chapters);
  * probe each content VOB with ffprobe, sum durations/sizes, and
    emit one scanner-compatible report row per title set, so the
    existing flow works unchanged: state_manager import -> hevc_convert
    (which gains the DVD encode path) -> quality_check verification.

What this module deliberately does NOT do:

  * encode anything (``encoders.py`` / ``hevc_convert.py`` own that);
  * claim a chapter count (IFO chapter tables are not parsed, so
    chapters are reported as unknown, never guessed);
  * touch the originals. ``--replace`` is refused for DVD jobs in
    ``hevc_convert.py`` because the original is a directory, not a
    file -- outputs always go to ``--out-dir``.

A DVD job's ``Path`` is a synthetic key, ``<VIDEO_TS>/VTS_01`` and
friends: it identifies the title set in the report and the state DB,
it is not a file on disk. The real inputs live in ``Source_Files``.
The state DB fingerprints DVD jobs by summed size only (there is no
single file mtime to compare), which is documented in
``state_manager.py``.

CLI:

    dvd-ingest scan "D:\\Media\\DVDs" --out-dir reports
    dvd-ingest info "D:\\Media\\Movie\\VIDEO_TS"
    dvd-ingest previews -i "D:\\Media\\Movie\\VIDEO_TS" -o menu_previews

Dependencies: none (stdlib only). ffprobe must be on PATH for
``scan``; ffmpeg must be on PATH for ``previews``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pprint
import re
import shutil
import subprocess
import sys
from pathlib import Path

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# DVD structure
# ---------------------------------------------------------------------------
# VTS_01_0.IFO / VTS_01_1.VOB / VTS_02_0.BUP ... -- part 0 is the menu,
# parts 1..9 are content. Matching is case-insensitive: discs ripped
# on Linux often carry lowercase extensions, and the old script's
# exact-case matching silently found nothing there.
_VTS_RE = re.compile(r"VTS_(\d{2})_(\d)\.(IFO|BUP|VOB)$", re.IGNORECASE)
_ROOT_RE = re.compile(r"VIDEO_TS\.(IFO|BUP|VOB)$", re.IGNORECASE)

IFO_TYPES = {
    "DVDVIDEO-VMG": "Video Manager (Root Menu/Navigation)",
    "DVDVIDEO-VTS": "Video Title Set (Feature/Bonus Content)",
}


def parse_ifo_header(ifo_path: Path) -> dict:
    """Read an IFO file's 12-byte header id. Never raises for bad data."""
    info: dict = {"header_id": None, "type": "Unknown",
                  "size_bytes": None, "chapters": None}
    try:
        info["size_bytes"] = ifo_path.stat().st_size
        with open(ifo_path, "rb") as fh:
            header = fh.read(12).decode(errors="ignore")
    except OSError as exc:
        info["error"] = str(exc)
        return info
    info["header_id"] = header
    info["type"] = IFO_TYPES.get(header, "Unknown")
    # Chapters are NOT parsed: the chapter tables live deeper in the
    # IFO (VTS_PGCIT) and the old script's MediaInfo shortcut counted
    # dict keys instead. Unknown beats a confident wrong number.
    return info


def analyze_ifo_files(video_ts_dir: str | Path) -> dict:
    """Map IFO filename -> header info for a VIDEO_TS directory.

    Returns {} (not an error) when the directory does not exist, matching
    the old script's behaviour for callers that probe before deciding.
    """
    video_ts_dir = Path(video_ts_dir)
    ifo_data: dict = {}
    if not video_ts_dir.is_dir():
        return ifo_data
    for name in sorted(os.listdir(video_ts_dir)):
        if name.upper().endswith(".IFO"):
            ifo_data[name] = parse_ifo_header(video_ts_dir / name)
    return ifo_data


def read_video_ts_directory(video_ts_dir: str | Path,
                            vts_only: bool = False):
    """Map a VIDEO_TS directory's contents into VTS groups.

    Returns a dict by default; with ``vts_only=True`` returns a sorted
    list of unique 2-digit VTS numbers (e.g. ['01', '02']). Returns
    [] / {} when the directory does not exist.
    """
    video_ts_dir = Path(video_ts_dir)
    if not video_ts_dir.is_dir():
        return [] if vts_only else {}

    contents: dict = {
        "vts_sets": set(),
        "vts_files": {},
        "root_files": [],
        "all_files": [],
    }
    try:
        names = os.listdir(video_ts_dir)
    except OSError:
        return [] if vts_only else contents
    for name in names:
        contents["all_files"].append(name)
        match = _VTS_RE.match(name)
        if match:
            vts_num = match.group(1)
            contents["vts_sets"].add(vts_num)
            contents["vts_files"].setdefault(vts_num, []).append(name)
        else:
            contents["root_files"].append(name)

    contents["vts_sets"] = sorted(contents["vts_sets"])
    for vts_num in contents["vts_files"]:
        contents["vts_files"][vts_num] = sorted(
            contents["vts_files"][vts_num], key=str.upper)
    contents["root_files"] = sorted(contents["root_files"], key=str.upper)
    contents["all_files"] = sorted(contents["all_files"], key=str.upper)
    if vts_only:
        return contents["vts_sets"]
    return contents


def content_vobs(video_ts_dir: str | Path, vts_num: str) -> list[Path]:
    """Content VOBs (parts 1..9) for one VTS, sorted. Menu (_0) excluded."""
    video_ts_dir = Path(video_ts_dir)
    found: list[Path] = []
    if not video_ts_dir.is_dir():
        return found
    try:
        names = os.listdir(video_ts_dir)
    except OSError:
        return found
    for name in names:
        match = _VTS_RE.match(name)
        if (match and match.group(1) == vts_num
                and match.group(3).upper() == "VOB"
                and match.group(2) != "0"):
            found.append(video_ts_dir / name)
    return sorted(found, key=lambda p: p.name.upper())


def menu_vobs(video_ts_dir: str | Path) -> list[Path]:
    """Menu VOBs: VIDEO_TS.VOB plus every VTS _0.VOB, sorted, deduped."""
    video_ts_dir = Path(video_ts_dir)
    found: list[Path] = []
    if not video_ts_dir.is_dir():
        return found
    try:
        names = os.listdir(video_ts_dir)
    except OSError:
        return found
    for name in names:
        upper = name.upper()
        if _ROOT_RE.match(name) and upper.endswith(".VOB"):
            found.append(video_ts_dir / name)
            continue
        match = _VTS_RE.match(name)
        if (match and match.group(3).upper() == "VOB"
                and match.group(2) == "0"):
            found.append(video_ts_dir / name)
    seen: set[str] = set()
    unique: list[Path] = []
    for p in sorted(found, key=lambda p: p.name.upper()):
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def find_video_ts_dirs(root: str | Path) -> list[Path]:
    """Recursively find VIDEO_TS directories under *root*.

    The root itself may be a VIDEO_TS directory. Hidden directories
    are pruned, and a found VIDEO_TS is not descended into.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    if root.name.upper() == "VIDEO_TS":
        return [root.resolve()]
    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if Path(dirpath).name.upper() == "VIDEO_TS":
            found.append(Path(dirpath).resolve())
            dirnames[:] = []  # do not descend into a disc's VIDEO_TS
    return sorted(found)


# ---------------------------------------------------------------------------
# ffprobe (local copy of the scanner's pattern: stdlib, utf-8, timeout)
# ---------------------------------------------------------------------------
FFPROBE_TIMEOUT_S = 120


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
    "h264": "H.264", "avc": "H.264", "mpeg2video": "MPEG-2",
    "mpeg1video": "MPEG-1", "mpeg4": "MPEG-4", "hevc": "HEVC",
    "h265": "HEVC", "vp9": "VP9", "av1": "AV1", "aac": "AAC",
    "ac3": "AC3", "eac3": "E-AC3", "dts": "DTS", "truehd": "TrueHD",
    "mp3": "MP3", "flac": "FLAC", "pcm_s16le": "PCM", "opus": "Opus",
    "subrip": "SubRip", "dvd_subtitle": "DVD",
    "hdmv_pgs_subtitle": "PGS", "ass": "ASS", "mov_text": "mov_text",
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
    if "10le" in pix_fmt or pix_fmt.endswith("10"):
        return 10
    if "12le" in pix_fmt or pix_fmt.endswith("12"):
        return 12
    return 8


def _aspect_ratio(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    g = math.gcd(width, height)
    return f"{width // g}:{height // g}"


def _stream_signature(streams: list[dict]) -> tuple:
    """Stream makeup, for checking every VOB in a set matches the first."""
    sig: list[tuple] = []
    for s in streams:
        kind = s.get("codec_type")
        if kind == "video":
            sig.append((kind, s.get("codec_name"), s.get("width"),
                        s.get("height")))
        elif kind == "audio":
            sig.append((kind, s.get("codec_name"), s.get("channels")))
        elif kind == "subtitle":
            sig.append((kind, s.get("codec_name")))
    return tuple(sig)


def _describe_streams(streams: list[dict]) -> tuple[str | None, int,
                                                    str | None, int,
                                                    str | None, str | None,
                                                    float | None]:
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


# ---------------------------------------------------------------------------
# Report rows: one per title set, scanner-compatible
# ---------------------------------------------------------------------------
def _error_row(video_ts_dir: Path, vts_num: str, error: str,
               size_bytes: int | None = None) -> dict:
    movie_stem = video_ts_dir.parent.name or "dvd"
    return {
        "Path": str((video_ts_dir / f"VTS_{vts_num}").resolve()),
        "Parent_Directory": str(video_ts_dir.parent.resolve()),
        "Filename": f"{movie_stem}_vts{vts_num}",
        "Size_GB": round(size_bytes / 1024 ** 3, 3) if size_bytes else None,
        "Source_Kind": "dvd_vts",
        "Video_TS_Dir": str(video_ts_dir.resolve()),
        "VTS_Number": vts_num,
        "Error": error,
        "Optimization_Candidate": "Low",
    }


def build_vts_row(video_ts_dir: str | Path, vts_num: str) -> dict:
    """Build one scanner-compatible row for a title set.

    Probes every content VOB. Duration and size are summed across the
    set; codec/resolution/audio/subtitle makeup come from the first
    content VOB, and a makeup mismatch across the set is recorded in
    Stream_Makeup_Note (verification then uses the first VOB's makeup,
    so the note is there for a human to check before replacing
    anything -- which, for DVD jobs, is always manual anyway).
    """
    video_ts_dir = Path(video_ts_dir)
    vobs = content_vobs(video_ts_dir, vts_num)
    if not vobs:
        return _error_row(video_ts_dir, vts_num,
                          f"No content VOB files found for VTS_{vts_num}")
    total_bytes = 0
    for vob in vobs:
        try:
            total_bytes += vob.stat().st_size
        except OSError:
            pass

    probes: list[dict] = []
    for vob in vobs:
        try:
            probes.append(_probe_file(vob))
        except RuntimeError as exc:
            return _error_row(video_ts_dir, vts_num, str(exc),
                              size_bytes=total_bytes or None)

    first = probes[0]
    fmt = first.get("format", {}) or {}
    streams = first.get("streams", []) or []
    durations: list[float] = []
    for info in probes:
        f = info.get("format", {}) or {}
        try:
            durations.append(float(f["duration"]))
        except (KeyError, TypeError, ValueError):
            return _error_row(
                video_ts_dir, vts_num,
                "A content VOB has no readable duration; cannot sum "
                "the title-set duration", size_bytes=total_bytes or None)
    duration = round(sum(durations), 1)

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    v = video_streams[0] if video_streams else None
    width = v.get("width") if v and isinstance(v.get("width"), int) else None
    height = v.get("height") if v and isinstance(v.get("height"), int) else None
    overall_mbps = _kbps_to_mbps(fmt.get("bit_rate"))
    (audio_descs, audio_count, audio_langs, sub_count, sub_descs,
     sub_langs, audio_mbps) = _describe_streams(streams)

    signatures = [_stream_signature(
        (info.get("streams", []) or [])) for info in probes]
    makeup_ok = all(s == signatures[0] for s in signatures)
    makeup_note = (None if makeup_ok else
                   "Content VOBs in this title set do not share one "
                   "stream makeup; verification uses the first VOB's "
                   "streams. Check the set before trusting the output.")

    movie_stem = video_ts_dir.parent.name or "dvd"
    row: dict = {
        "Path": str((video_ts_dir / f"VTS_{vts_num}").resolve()),
        "Parent_Directory": str(video_ts_dir.parent.resolve()),
        "Filename": f"{movie_stem}_vts{vts_num}",
        "Size_MB": round(total_bytes / (1024 ** 2), 2),
        "Size_GB": round(total_bytes / (1024 ** 3), 3),
        "Duration_Seconds": duration,
        "Container_Format": "VOB",
        "Video_Codec": _codec_label(v.get("codec_name")) if v else None,
        "Resolution": f"{width}x{height}" if width and height else None,
        "Width": width,
        "Height": height,
        "Aspect_Ratio": _aspect_ratio(width, height),
        "FPS": (_parse_fps(v.get("avg_frame_rate"))
                or _parse_fps(v.get("r_frame_rate"))) if v else None,
        "Pixel_Format": v.get("pix_fmt") if v else None,
        "Bit_Depth": _bit_depth(v.get("pix_fmt")) if v else None,
        "Video_Bitrate_Mbps": _video_bitrate(first, streams, overall_mbps),
        "Audio_Codecs": audio_descs,
        "Audio_Stream_Count": audio_count,
        "Audio_Languages": audio_langs,
        "Audio_Bitrate_Mbps": audio_mbps,
        "Subtitle_Stream_Count": sub_count,
        "Subtitle_Codecs": sub_descs,
        "Subtitle_Languages": sub_langs,
        "Overall_Bitrate_Mbps": overall_mbps,
        "Bloat_Flags": ("dvd_vts" if makeup_ok
                        else "dvd_vts | stream_makeup_differs"),
        "Source_Kind": "dvd_vts",
        "Video_TS_Dir": str(video_ts_dir.resolve()),
        "VTS_Number": vts_num,
        "Source_Files": [str(v.resolve()) for v in vobs],
        "Menu_Files": [str(m.resolve()) for m in menu_vobs(video_ts_dir)],
        "IFO": analyze_ifo_files(video_ts_dir),
        "Stream_Makeup_Consistent": makeup_ok,
        "Stream_Makeup_Note": makeup_note,
    }

    # Candidate + savings estimate from the scanner, so a DVD title
    # set is ranked by the same rules as a flat file. If the scanner
    # is unavailable, fall back to an honest unranked row rather than
    # inventing an estimate.
    try:
        from .video_optimizer_scan import (  # noqa: E402
            classify_candidate, estimate_hevc_savings)
        candidate, reason = classify_candidate(row)
        row["Optimization_Candidate"] = candidate
        row["Candidate_Reason"] = f"DVD title set: {reason}" + (
            f" {makeup_note}" if makeup_note else "")
        row.update(estimate_hevc_savings(row))
    except ImportError:
        row["Optimization_Candidate"] = "High Priority"
        row["Candidate_Reason"] = ("DVD title set (scanner unavailable; "
                                   "no savings estimate)")
    return row


def build_rows_for_disc(video_ts_dir: str | Path) -> list[dict]:
    """One row per title set in a VIDEO_TS directory."""
    video_ts_dir = Path(video_ts_dir)
    rows: list[dict] = []
    for vts_num in read_video_ts_directory(video_ts_dir, vts_only=True):
        rows.append(build_vts_row(video_ts_dir, vts_num))
    if not rows:
        movie_stem = video_ts_dir.parent.name or "dvd"
        rows.append({
            "Path": str(video_ts_dir.resolve()),
            "Parent_Directory": str(video_ts_dir.parent.resolve()),
            "Filename": movie_stem,
            "Source_Kind": "dvd_vts",
            "Video_TS_Dir": str(video_ts_dir.resolve()),
            "Error": "No VTS files found in this VIDEO_TS directory",
            "Optimization_Candidate": "Low",
        })
    return rows


def scan_dvds(root: str | Path) -> tuple[list[dict], list[Path]]:
    """Scan *root* for VIDEO_TS directories. Returns (rows, discs)."""
    discs = find_video_ts_dirs(root)
    rows: list[dict] = []
    for disc in discs:
        rows.extend(build_rows_for_disc(disc))
    return rows, discs


# ---------------------------------------------------------------------------
# Concat list (ffmpeg concat demuxer) -- written by the converter, but the
# escaping rule lives here next to the VOB grouping it serves.
# ---------------------------------------------------------------------------
def write_concat_list(files: list[Path], dest: Path) -> Path:
    """Write an ffmpeg concat-demuxer list file for *files*.

    Paths use forward slashes (safe on Windows and Linux) and single
    quotes are escaped the way the demuxer requires, so a title like
    ``Tweety's High-Flying Adventure`` does not break the list.
    """
    lines = []
    for f in files:
        escaped = str(f).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return dest


# ---------------------------------------------------------------------------
# Menu previews (the old dvd_menu_extractor.py, fixed)
# ---------------------------------------------------------------------------
def extract_menu_frames(video_ts_dir: str | Path, output_folder: str | Path,
                        seek_points: tuple[float, ...] = (1.0, 5.0, 15.0, 30.0),
                        min_vob_bytes: int = 50_000,
                        min_jpg_bytes: int = 5_000) -> list[dict]:
    """Extract preview frames from menu/first-content VOBs.

    Returns one entry per attempted frame: {vob, seek_seconds, output,
    ok, detail}. Failures are recorded in the entry (ffmpeg stderr,
    last line) instead of being silently dropped, which is what the
    old script did by capturing stderr and never reading it. Raises
    FileNotFoundError for a missing input directory and RuntimeError
    when ffmpeg is not on PATH.
    """
    video_ts_dir = Path(video_ts_dir)
    if not video_ts_dir.is_dir():
        raise FileNotFoundError(
            f"DVD directory not found: {video_ts_dir}")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH (install ffmpeg)")
    output_folder = Path(output_folder).resolve()
    output_folder.mkdir(parents=True, exist_ok=True)

    candidates = menu_vobs(video_ts_dir)
    # First-content VOBs (_1) are also preview candidates, as before.
    for vts_num in read_video_ts_directory(video_ts_dir, vts_only=True):
        for vob in content_vobs(video_ts_dir, vts_num)[:1]:
            if vob not in candidates:
                candidates.append(vob)
    candidates = sorted(candidates, key=lambda p: p.name.upper())
    print(f"--- DVD Menu Frame Extractor ---")
    print(f"Analyzing {len(candidates)} VOB file(s)...")
    print(f"Saving frames to: {output_folder}\n")

    results: list[dict] = []
    for vob_path in candidates:
        try:
            size = vob_path.stat().st_size
        except OSError as exc:
            results.append({"vob": vob_path.name, "seek_seconds": None,
                            "output": None, "ok": False,
                            "detail": f"cannot stat: {exc}"})
            continue
        if size < min_vob_bytes:
            print(f"Skipping tiny file: {vob_path.name} ({size} bytes)")
            continue
        print(f"Processing: {vob_path.name}")
        for seek in seek_points:
            slug = f"t{seek:g}s"
            out_path = output_folder / f"{vob_path.stem}_{slug}.jpg"
            cmd = ["ffmpeg", "-y", "-ss", str(seek), "-i", str(vob_path),
                   "-frames:v", "1", "-an", "-vf", "bwdif,scale=854:480",
                   "-q:v", "3", str(out_path)]
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True,
                                  encoding="utf-8", errors="replace")
            ok = (proc.returncode == 0 and out_path.is_file()
                  and out_path.stat().st_size > min_jpg_bytes)
            detail = ""
            if not ok:
                detail = "\n".join(
                    (proc.stderr or "").strip().splitlines()[-1:])[:200]
                if proc.returncode != 0 and not detail:
                    detail = f"ffmpeg exited rc={proc.returncode}"
                elif proc.returncode == 0:
                    detail = (f"frame file missing or <= {min_jpg_bytes} "
                              f"bytes (blank/black frame?)")
            entry = {"vob": vob_path.name, "seek_seconds": seek,
                     "output": str(out_path), "ok": ok, "detail": detail}
            results.append(entry)
            if ok:
                print(f"  Captured frame at {seek:g}s")
            else:
                print(f"  Failed frame at {seek:g}s: {detail}")
        print()
    return results


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
        from .video_optimizer_scan import write_reports  # noqa: E402
        return write_reports(rows, out_dir, stem)
    except ImportError:
        json_path = out_dir / f"{stem}.json"
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        return json_path, json_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="DVD ingest for the HEVC pipeline (VIDEO_TS -> jobs).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="Find VIDEO_TS dirs and write a job report")
    p.add_argument("root", type=Path,
                   help="Root to search, or a VIDEO_TS directory itself")
    p.add_argument("--out-dir", type=Path, default=Path("reports"))
    p.add_argument("--stem", default="dvd_report",
                   help="Report filename stem (default: dvd_report)")

    p = sub.add_parser("info", help="Show a VIDEO_TS directory map + IFO info")
    p.add_argument("video_ts_dir", type=Path)

    p = sub.add_parser("previews", help="Extract menu preview frames")
    p.add_argument("-i", "--input", type=Path, required=True,
                   help="Path to the VIDEO_TS directory")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Folder for frames (default: ./menu_previews)")
    p.add_argument("--seeks", type=float, nargs="+",
                   default=[1.0, 5.0, 15.0, 30.0],
                   help="Seek points in seconds (default: 1 5 15 30)")

    args = ap.parse_args(argv)

    if args.cmd == "info":
        if not args.video_ts_dir.is_dir():
            print(f"ERROR: DVD directory not found: {args.video_ts_dir}",
                  file=sys.stderr)
            return 2
        print(f"Directory mapping for: {args.video_ts_dir}\n")
        pprint.pprint(read_video_ts_directory(args.video_ts_dir))
        print(f"\nIFO analysis for: {args.video_ts_dir}\n")
        pprint.pprint(analyze_ifo_files(args.video_ts_dir))
        return 0

    if args.cmd == "previews":
        out_dir = args.output or (Path.cwd() / "menu_previews")
        try:
            results = extract_menu_frames(args.input, out_dir,
                                          seek_points=tuple(args.seeks))
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        captured = sum(1 for r in results if r["ok"])
        failed = len(results) - captured
        print(f"Captured {captured} frame(s), {failed} failed.")
        return 0 if captured else 1

    # scan
    if not args.root.is_dir():
        print(f"ERROR: root directory not found: {args.root}", file=sys.stderr)
        return 2
    if shutil.which("ffprobe") is None:
        print("ERROR: ffprobe not found on PATH (install ffmpeg).",
              file=sys.stderr)
        return 2
    rows, discs = scan_dvds(args.root)
    if not discs:
        print(f"No VIDEO_TS directories found under {args.root}; "
              f"no report written.", file=sys.stderr)
        return 2
    csv_path, json_path = _write_report(rows, args.out_dir, args.stem)
    n_err = sum(1 for r in rows if r.get("Error"))
    print(f"Found {len(discs)} VIDEO_TS director(ies), "
          f"{len(rows)} title set(s) ({n_err} with errors).")
    print(f"Report CSV:  {csv_path}")
    print(f"Report JSON: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
