#!/usr/bin/env python3
"""Machine-resource detection and encode sizing for hevc_convert.py.

What was true before this module (2026-10-25, honest version):
  * A single x265 encode already multithreads internally -- ffmpeg's
    default is threads=auto, so one job uses the machine's cores.
  * Parallel encodes were manual only (--jobs N), plus one hard-coded
    rule: NVENC raised --jobs 1 -> 2.
  * Nothing checked RAM, and nothing divided threads between jobs.
    So `--jobs 3` with x265 meant three encodes each trying to use
    ALL cores at once: oversubscription, cache thrashing, and often
    SLOWER total throughput than one job, not faster.

What this module adds (startup sizing, not mid-run throttling):
  * Detect logical cores and total RAM with stdlib only (psutil is
    used if it happens to be installed, never required). Windows uses
    GlobalMemoryStatusEx via ctypes; Linux reads /proc/meminfo;
    macOS uses sysctl hw.memsize. If RAM cannot be determined, it is
    None and the CPU rule alone decides -- never a guessed number.
  * `--auto-jobs` picks parallel jobs from cores + RAM + encoder:
      x265:  jobs = min(cores // 8, RAM // 2 GB, 4), at least 1.
             x265 is CPU- and RAM-hungry, and one slow-preset job
             already saturates ~8 threads on 1080p, so more jobs
             only help on many-core machines.
      nvenc/qsv: jobs = min(cores // 2, 3), at least 1.
             Hardware encodes are GPU-bound and CPU-light, so 2-3
             concurrent sessions is the sweet spot (the old NVENC
             rule), but on a 2-core box that correctly falls back
             to 1 instead of blindly forcing 2.
  * Threads per job are always divided, even for explicit --jobs:
    threads_per_job = cores // jobs (at least 1), unless
    --threads-per-job overrides it. Total encoder threads across
    jobs therefore never exceeds the core count by construction.
    An explicit --jobs that would need more RAM than ~80% of the
    machine gets a warning, not a silent refusal -- the user asked
    for it, so it runs, but with the risk stated.

What this module does NOT do (so it is not oversold):
  * It does not monitor load, thermals, or other processes during a
    run, and it does not resize jobs mid-run. Sizing is decided once
    at startup from the machine as it stands then. If you start a
    second pipeline on the same machine, each one sizes as if it
    were alone -- use explicit --jobs (or one pipeline at a time)
    for that case. The pipeline lockfile only stops two runs sharing
    one DB, not two runs sharing one CPU.
  * It cannot detect GPU VRAM or NVENC session limits without
    nvidia-smi / Intel tools, so the NVENC/QSV cap of 3 is a
    conservative rule, not a measured GPU limit.
"""
from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
from dataclasses import dataclass


def cpu_count() -> int:
    return max(1, os.cpu_count() or 1)


def memory_gb() -> float | None:
    """Total RAM in GiB, or None if it cannot be determined."""
    # psutil, when present, is the most portable answer.
    try:
        import psutil  # type: ignore
        total = psutil.virtual_memory().total
        return round(total / 1024 ** 3, 1) if total else None
    except Exception:  # noqa: BLE001 - fall through to platform probes
        pass
    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    m = re.match(r"MemTotal:\s+(\d+)\s+kB", line)
                    if m:
                        return round(int(m.group(1)) / 1024 ** 2, 1)
        elif system == "Windows":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.ullTotalPhys / 1024 ** 3, 1)
        elif system == "Darwin":
            proc = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                  capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                return round(int(proc.stdout.strip()) / 1024 ** 3, 1)
    except Exception:  # noqa: BLE001 - unknown RAM is reported as None
        pass
    return None


def memory_per_job_gb(encoder: str) -> float:
    """Conservative RAM budget per concurrent encode (GiB).

    x265 at slow preset on 1080p is the hungry case (~2 GiB); 4K
    roughly doubles it, but the auto rule already caps x265 jobs
    low, so a single 2 GiB budget with the RAM cap is the honest
    simplification -- it prevents a many-job run from swapping,
    which is the failure mode that actually kills throughput.
    """
    return 2.0 if encoder == "x265" else 0.5


@dataclass
class Plan:
    jobs: int
    threads_per_job: int
    cores: int
    memory_gb: float | None
    reason: str
    warnings: list[str]


def recommend(encoder: str, requested_jobs: int = 1,
              auto_jobs: bool = False,
              threads_per_job: int = 0) -> Plan:
    """Return a sizing plan. Explicit --jobs is respected; auto sizes.

    threads_per_job: 0 = divide cores evenly across jobs. A positive
    value overrides the division (use it only if you know why -- it
    can reintroduce oversubscription).
    """
    cores = cpu_count()
    mem = memory_gb()
    warnings: list[str] = []
    if auto_jobs:
        if encoder == "x265":
            jobs_by_cpu = max(1, cores // 8)
            jobs_by_mem = int(mem // memory_per_job_gb(encoder)) if mem else 99
            jobs = max(1, min(jobs_by_cpu, jobs_by_mem, 4))
            reason = (f"auto x265: cores//8={jobs_by_cpu}, "
                      f"RAM//2GB={jobs_by_mem if mem else 'unknown'}, cap 4")
        else:  # nvenc / qsv: GPU-bound, CPU-light, cap 3 sessions
            jobs_by_cpu = max(1, min(3, cores // 2))
            jobs = jobs_by_cpu
            reason = f"auto {encoder}: min(cores//2, 3)={jobs_by_cpu}"
    else:
        jobs = max(1, requested_jobs)
        reason = f"explicit --jobs {jobs}"
        if encoder == "nvenc" and requested_jobs == 1:
            # Preserve the old NVENC rule, but now with divided threads.
            jobs = 2
            reason = "nvenc: --jobs raised 1 -> 2 (legacy rule)"
        per_job = memory_per_job_gb(encoder)
        if mem is not None and jobs * per_job > 0.8 * mem:
            warnings.append(
                f"{jobs} x {encoder} job(s) at ~{per_job:.1f} GiB each "
                f"exceeds 80% of {mem:.1f} GiB RAM; expect swapping / "
                f"slower throughput -- lower --jobs or use --auto-jobs")
    if threads_per_job and threads_per_job > 0:
        t = threads_per_job
        if t * jobs > cores:
            warnings.append(
                f"--threads-per-job {t} x {jobs} jobs = {t * jobs} threads "
                f"on {cores} cores: oversubscribed by design")
    else:
        t = max(1, cores // jobs)
    return Plan(jobs=jobs, threads_per_job=t, cores=cores,
                memory_gb=mem, reason=reason, warnings=warnings)


if __name__ == "__main__":
    for enc in ("x265", "nvenc", "qsv"):
        print(enc, recommend(enc, auto_jobs=True))
