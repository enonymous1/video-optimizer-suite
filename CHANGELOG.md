# Changelog

## v2.2.0 — 2026-10-28

Camcorder ingest, folded in from `handbrake_camcorder_conversion`
as an ffmpeg HEVC lane (HandBrakeCLI dropped):

- New `camcorder_ingest.py`: `.mod` / `.tod` / `.m2t` plus the
  camcorder formats the scanner already covers, one
  scanner-compatible row per file, with metadata sidecars
  (`.modd`, `.xmp`, `.xml`) and ffprobe `field_order` recorded.
- Converter: `--camcorder-mode auto` (default) applies `bwdif`
  only when `field_order` says interlaced; `deinterlace` forces
  it, `keep` never applies it. `--camcorder-metadata` merges
  metadata/sidecars with exiftool on the staging file before
  verification, falling back to sidecar copies only after a
  verified publish.
- State DB: `sidecar_files` / `field_order` columns via migration;
  pipeline `--camcorder-dir`; dashboard source-kind filter.
- New `tests/test_camcorder_ingest.py`.

## v2.1.0 — 2026-10-27

DVD ingest, folded in from `ss-py-ffmpeg-conversions`:

- New `dvd_ingest.py`: VIDEO_TS discovery, VTS grouping, IFO
  headers, one job per title set, menu preview frames.
- Converter: concat-demuxer encode with `bwdif` deinterlace,
  `--dvd-mode 480p` / `1080p` (Lanczos upscale), summed-reference
  verification; `--replace` refused for DVD jobs.
- State DB: `source_kind` / `source_files` / `video_ts_dir` /
  `vts_number` columns via migration; pipeline `--dvd-dir`.

## v2.0.0 — 2026-10-25

Initial suite: bundled ffprobe scanner (`video_optimizer_scan.py`),
SQLite state manager with atomic job claiming, HEVC converter
(x265 / NVENC / QSV) with staging + verify-before-publish +
opt-in `--replace` with `.bak` backup, standalone verifier
(`quality_check.py`), resource sizing (`resources.py`), Flask
dashboard, and the `pipeline.py` driver.
