# Changelog

## Unreleased — staff review, 2026-10-31 (see REVIEW.md)

Safety and correctness fixes, all backwards-compatible in CLI
behaviour except where the old behaviour was the bug:

- `swap_originals.py`: publish rollback now uses the same
  cross-filesystem move helper as the backup step, so a rollback
  no longer fails with EXDEV when `--backup-dir` is on another
  drive; a shutdown during staging now refuses to publish
  (returns `interrupted`, original untouched); `--recover`
  now obeys the dry-run / `--yes` gates (a plan run writes no
  log and exits 0 / 2) and a real recovery returns 1 on failure;
  orphan staging cleanup is gated the same way; fast-probe
  tolerances are imported from `quality_check` instead of
  re-typed.
- `hevc_convert.py`: shutdown after verification now refuses to
  publish; backup numbering aligned with swap (first collision
  is `.1.bak`, not `.2.bak`); the backup path is recorded
  before the publish attempt and a failed rollback now reports
  where the original actually is instead of claiming "original
  untouched"; a failed publish never deletes a pre-existing
  final file.
- `encoders.py`: 12-bit sources keep 12-bit (was silently
  downconverted to 8-bit); unknown bit depths and unknown NVENC
  presets now raise instead of guessing.
- `video_optimizer_scan.py`: bit-depth parsing is suffix-based
  (NV12 is 8-bit, not 12-bit).
- `state_manager.py`: re-import no longer resets user-skipped
  or verified rows to pending; `CLAIMABLE` vs. `RETRYABLE`
  now documents that listing includes skipped jobs the claimer
  never takes.
- `dashboard.py`: Jinja autoescaping forced on (string
  templates were unescaped — a filename or `?q=` payload could
  render as live HTML); SQLite timeout raised to 30 s with a
  503 handler; LIKE wildcards escaped; queue page shows
  "Showing X of N".
- `common.py` (new): shared log / byte / duration formatting
  and atomic JSON writes, imported by the scanner, converter,
  and swap script.
- `.dockerignore` (new): `.git/`, reports, converted outputs,
  and `state.db` no longer bake into the image.
- Tests: new `tests/test_dashboard.py` (skips without Flask);
  new smoke checks for import preservation and claim-never-skips;
  new swap fixture for recover dry-run / no-`--yes`.
  CI now installs requirements and runs all five suites.
- `README.md`: rewritten — Description, Prerequisites,
  Installation, Local Development, Deployment, plus Guides,
  Troubleshooting, Security, and Contributing. Ten stale or
  wrong claims corrected (see REVIEW.md §5); the architecture
  diagram is `docs/assets/architecture.svg`.

## v2.3.0 — 2026-10-29

Swap already-converted files into place, without re-encoding:

- New `swap_originals.py`: pairs report `Output_Path` and/or
  conversion-log `Input_Path`/`Output_Path`/`New_Bytes` (never
  guesses from filenames; report/log disagreement skips the file),
  re-runs the full verify gate per file, stages a copy next to
  the original (same filesystem, so the publish is atomic even
  across drives), backs the original up (`.bak`, numbered on
  collision, or `--backup-dir`), atomically publishes, restores
  timestamps/permissions, copies the `.conversion.json` sidecar,
  rolls back on a failed publish, and offers `--recover` for
  interrupted swaps. Larger encodes are skipped unless
  `--allow-larger`; DVD jobs are refused. No `--yes`, no swap.
  Backups are never deleted by the script. New
  `tests/test_swap_originals.py` (23 checks, synthetic files).
- `hevc_convert.py`: the closing "re-run with --replace" message
  now points at `swap_originals.py` for already-converted files,
  because `--skip-done` would select nothing and `--no-skip-done`
  would re-encode everything. No encode behaviour changed.

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
