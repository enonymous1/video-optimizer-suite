#!/usr/bin/env python3
"""Encoder profiles for the HEVC pipeline.

Single place to swap CPU / NVIDIA / Intel encoding. The rest of the
pipeline (hevc_convert.py) never builds encoder flags by hand -- it asks
for a profile by name and gets back a validated argument list.

    x265  -- libx265, CRF. Best compression, slowest. The reference
             quality: everything else is judged against this.
    nvenc -- hevc_nvenc, CQ. ~5-10x faster than x265 on an RTX-class
             GPU, but ~20-30% larger at the same perceptual quality.
             Needs an NVIDIA GPU + driver. Presets p1..p7 (p7 best);
             x265 preset names are auto-mapped (medium -> p5).
    qsv   -- hevc_qsv, global_quality. Intel Quick Sync (i9-10850K has
             UHD 630). Quality sits between NVENC and x265 for most
             content, and it is far faster than CPU. Presets are the
             usual veryfast..veryslow names. Needs /dev/dri + the Intel
             media driver inside Docker (see README, Docker section).

Quality value meaning differs per encoder, so it is kept as one
``quality`` number and translated here:
    x265  -> -crf
    nvenc -> -cq
    qsv   -> -global_quality

No silent fallback: if you ask for nvenc/qsv and this ffmpeg build or
machine cannot do it, preflight() fails loudly before any file is
touched. A silent CPU fallback would change file sizes and encode
times without telling you.
"""
from __future__ import annotations

import subprocess

X265_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
]
NVENC_PRESETS = [f"p{i}" for i in range(1, 8)]
QSV_PRESETS = X265_PRESETS[:]  # QSV accepts the same names

# x265 preset name -> NVENC p1..p7 equivalent.
NVENC_PRESET_MAP = {
    "ultrafast": "p1", "superfast": "p2", "veryfast": "p3", "faster": "p4",
    "fast": "p5", "medium": "p5", "slow": "p6", "slower": "p7",
    "veryslow": "p7",
}

ENCODER_NAMES = ("x265", "nvenc", "qsv")
_FFMPEG_ENCODER = {"x265": "libx265", "nvenc": "hevc_nvenc", "qsv": "hevc_qsv"}


def resolve_nvenc_preset(preset: str) -> str:
    """Map an x265 preset name (or a literal p1..p7) to an NVENC preset."""
    p = preset.strip().lower()
    if p in NVENC_PRESETS:
        return p
    return NVENC_PRESET_MAP.get(p, "p7")


def pixel_format(bit_depth: int | None, encoder: str) -> str:
    """Output pixel format, preserving 10-bit sources to avoid banding."""
    if bit_depth == 10:
        # QSV wants p010le (its native 10-bit surface); the other two
        # accept yuv420p10le. NVENC also accepts p010le, but yuv420p10le
        # matches the v1 converter's behaviour.
        return "p010le" if encoder == "qsv" else "yuv420p10le"
    return "nv12" if encoder == "qsv" else "yuv420p"


def video_args(encoder: str, quality: int, preset: str,
               bit_depth: int | None) -> list[str]:
    """Return the ffmpeg -c:v argument list for *encoder*.

    Raises ValueError on an unknown encoder or preset, so a typo fails
    at argument validation, not halfway through a 2-hour encode.
    """
    if encoder not in ENCODER_NAMES:
        raise ValueError(f"unknown encoder {encoder!r} (want one of {ENCODER_NAMES})")
    pix_fmt = pixel_format(bit_depth, encoder)
    if encoder == "x265":
        if preset not in X265_PRESETS:
            raise ValueError(f"x265 preset {preset!r} not in {X265_PRESETS}")
        return ["-c:v", "libx265", "-preset", preset,
                "-crf", str(quality), "-pix_fmt", pix_fmt]
    if encoder == "nvenc":
        return ["-c:v", "hevc_nvenc",
                "-preset", resolve_nvenc_preset(preset),
                "-tune", "hq",
                "-cq", str(quality),
                "-rc-lookahead", "32",
                "-pix_fmt", pix_fmt]
    # qsv
    if preset not in QSV_PRESETS:
        raise ValueError(f"qsv preset {preset!r} not in {QSV_PRESETS}")
    return ["-c:v", "hevc_qsv",
            "-preset", preset,
            "-global_quality", str(quality),
            "-look_ahead", "1",
            "-pix_fmt", pix_fmt]


def encoder_available(encoder: str) -> tuple[bool, str]:
    """Preflight: does this ffmpeg build even know this encoder?

    This checks the build only. A ``True`` for nvenc/qsv does NOT prove
    a GPU is present -- that is only proven by a real encode. The
    converter therefore also treats the first encode failure as a
    hard stop with the ffmpeg stderr attached, rather than retrying
    every file against dead hardware.
    """
    name = _FFMPEG_ENCODER[encoder]
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", f"encoder={name}"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except FileNotFoundError:
        return False, "ffmpeg not found on PATH"
    except Exception as exc:  # noqa: BLE001 - preflight must not crash
        return False, str(exc)
    if proc.returncode == 0:
        return True, f"{name} present in this ffmpeg build"
    return False, f"{name} not available in this ffmpeg build"


def quality_label(encoder: str) -> str:
    return {"x265": "crf", "nvenc": "cq", "qsv": "global_quality"}[encoder]
