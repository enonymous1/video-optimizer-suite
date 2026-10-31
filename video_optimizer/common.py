#!/usr/bin/env python3
"""Shared helpers for the video-optimizer scripts.

This module exists because the same three helpers were copy-pasted
into ``video_optimizer_scan.py``, ``hevc_convert.py`` and
``swap_originals.py`` — and the copies had already started to drift
(the converter's backup numbering and the swap script's disagreed
about the first collision name). New code should import from here
instead of re-implementing; the old modules keep private aliases
(``_log = log_msg`` etc.) so existing call sites do not change.

Deliberately stdlib-only and side-effect-free at import time, so
every script — and every test — can import it without ffmpeg,
a database, or a working directory.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_print_lock = threading.Lock()


def log_msg(msg: str) -> None:
    """Print one line, serialised so threaded logs never interleave."""
    with _print_lock:
        print(msg, flush=True)


def fmt_bytes(n: float) -> str:
    """Human size, binary units: 1536 -> '1.5 KB', -2048 -> '-2.0 KB'."""
    n = float(n or 0)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{sign}{n:.1f} {unit}"
        n /= 1024
    return f"{sign}{n:.1f} TB"


def fmt_seconds(s: float) -> str:
    """Duration as '19m05s' or '2h03m' (no seconds once hours appear)."""
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def atomic_write_json(path: Path, rows: list | dict) -> None:
    """Write JSON atomically: temp file in the same directory, then
    ``os.replace``. A kill mid-write leaves the previous file intact
    instead of a truncated report/log — the failure mode the
    converter's plain ``open(path, 'w')`` report write-back had.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
