# Video Optimization Pipeline — Inspect → Process → Verify → Replace

A complete, self-contained suite. Everything lives in this one folder
— including the Analyzer (`video_optimizer_scan.py`, bundled verbatim
from the copy supplied with the project, unchanged). No separate
scanner copy is needed anywhere else, and nothing in this suite
depends on the old `video-meta-scan/` folder.

```
video_optimizer_scan.py        (bundled, Analyzer)
        │  CSV / JSON report, High Priority flags
        ▼
state_manager.py import        (NEW, State Manager)
        │  SQLite: files + attempts, atomic job claiming
        ▼
hevc_convert.py                (v2, Processor + Verifier + Replacer)
        │  ffmpeg → staging (.hevc-part, same fs) → quality_check
        │  → os.replace() into place (only if verified) → DB update
        ▼
dashboard.py                   (NEW, Flask + Jinja2, read-only)
        │  queue, status counts, per-file attempt history
        ▼
pipeline.py                    (NEW, driver: all of the above, one command)
```

## What v1 already did (kept, not reinvented)

- Scanner heuristics: High Priority = legacy codec ≥1.5× the efficient
  bitrate target for its resolution, or GB/hr far above norm; dry-run
  savings estimates; CSV/JSON/Markdown reports; conversion checklist
  columns preserved across re-scans.
- Converter v1: libx265 CRF 20 / medium, NVENC CQ + p1–p7, 10-bit
  preservation, audio copy, subtitle-copy-with-fallback, HEVC container
  switch to MKV, `--skip-done`, large-file confirm gate, sidecar JSON.
- **v1 safety model is still the default here**: without `--replace`,
  originals are never touched.

## What is new in v2

| Piece | File | What it adds over v1 |
|---|---|---|
| State | `state_manager.py` | SQLite instead of only JSON checklist; atomic `claim_next()`; `interrupted` status; fingerprint (size+mtime) so a changed file is re-queued instead of skipped forever; `export` back to scanner-style JSON |
| Verify | `quality_check.py` | Standalone CLI + library: duration, HEVC codec, audio count/codec/channels, subtitle warning, full-decode corruption check, suspiciously-small tripwire, encode-log parsing, optional SSIM/VMAF sample (advisory only) |
| Process | `hevc_convert.py` (v2) | `--encoder x265/nvenc/qsv` via `encoders.py`; staging file on the same filesystem; SIGINT/SIGTERM cleanup → `interrupted`, never `converted`; `--replace` with backup + timestamp/permission preservation + rollback on failure |
| Driver | `pipeline.py` | One command, PID lockfile, scan → import → convert → summary |
| Dashboard | `dashboard.py` | Flask + Jinja2 app factory, HTMX summary polling, read-only (no delete/re-encode buttons on purpose) |
| DVD ingest (2026-10-27) | `dvd_ingest.py` + converter/DB hooks | VIDEO_TS → one job per title set (VTS grouping, IFO headers, summed ffprobe reference); concat-demuxer encode with `bwdif` (+ optional 1080p Lanczos); `--replace` refused for DVD jobs. See "DVD ingest". |
| Camcorder ingest (2026-10-28) | `camcorder_ingest.py` + converter/DB hooks | Legacy camcorder files (`.mod`/`.tod`/`.m2t` the scanner misses, plus `.mts`/`.m2ts`/`.mpg`/`.vob`/`.mov`/`.avi`) → ffmpeg HEVC jobs with sidecar metadata handling and field-order deinterlace. HandBrakeCLI is not used. See "Camcorder ingest". |

## Quick start

```bash
# 0. ffmpeg/ffprobe on PATH. Python 3.10+. No packages needed except
#    Flask (dashboard only) and tqdm (optional progress bars).
pip install -r requirements.txt   # flask only; tqdm optional

# 1. Inspect (bundled scanner, in this folder)
#    Bundled copy takes --dir; it prompts if --dir/--out-dir are omitted,
#    so always pass both in scripts.
python video_optimizer_scan.py --dir /mnt/media \
    --threads 8 --out-dir reports

# 2. Import into SQLite
#    NOTE: use the full path the scanner actually wrote, e.g.
#    reports/video_optimization_report.json -- a bare
#    video_optimization_report.json only works if that file is in
#    your current directory, otherwise state_manager now prints a
#    clean ERROR (with a reports/ suggestion) instead of a traceback.
python state_manager.py import --report reports/video_optimization_report.json \
    --db state.db
python state_manager.py summary --db state.db

# 3. Preview, then convert High Priority (originals untouched)
python hevc_convert.py --db state.db --dry-run
python hevc_convert.py --db state.db --encoder x265 --jobs 1

# 4. When you trust it: verified replaces, backups kept
python hevc_convert.py --db state.db --encoder x265 --replace --yes
# Backups are <original>.bak alongside (or --backup-dir <dir>).
# Delete them only after spot-checking in Plex/Jellyfin:
#   python hevc_convert.py --db state.db --replace --purge-backup   # next run only
#   # or: find /mnt/media -name '*.bak' -delete   (after you verify)

# 5. One-command version (cron-friendly, lockfile-protected)
python pipeline.py --scan-dir /mnt/media --db state.db --encoder nvenc --preset p7
python pipeline.py --scan-dir /mnt/media --db state.db --encoder nvenc --preset p7 --replace --yes

# 6. Dashboard (read-only)
python dashboard.py --db state.db --host 0.0.0.0 --port 5000
```

## DVD ingest (`dvd_ingest.py`, 2026-10-27)

DVDs folded in from the old `ss-py-ffmpeg-conversions` scripts: one
job per title set, encoded by the same converter/verifier/encoder
profiles as flat files. Nothing here encodes anything itself.

```bash
# Scan a folder of discs -> one report row per title set
python dvd_ingest.py scan "D:\Media\DVDs" --out-dir reports
python state_manager.py import --report reports/dvd_report.json --db state.db
python hevc_convert.py --db state.db --encoder x265 --dvd-mode 480p

# Or one command, with the flat-file scan in the same DB:
python pipeline.py --scan-dir /mnt/media --dvd-dir /mnt/media --db state.db --encoder nvenc --dvd-mode 1080p

# Inspect one disc, or grab menu preview frames:
python dvd_ingest.py info "D:\Media\Movie\VIDEO_TS"
python dvd_ingest.py previews -i "D:\Media\Movie\VIDEO_TS" -o menu_previews
```

How a title set becomes a job: `VIDEO_TS` directories are found
recursively (case-insensitive, so lowercase `.vob`/`.ifo` rips work);
files group by VTS; the `_0` menu VOB is excluded from encoding;
IFO headers are read in pure Python (no MediaInfo); every content
VOB is probed and durations/sizes are summed, with codec and
audio/subtitle makeup taken from the first VOB. A job's `Path` is a
synthetic key (`<VIDEO_TS>/VTS_01`), not a file; the real inputs are
in `Source_Files`. Candidate ranking and savings estimates use the
scanner's rules, so DVD jobs sort with flat files.

Encoding: the converter feeds the VOBs to ffmpeg's concat demuxer
(via a temp list file next to the staging file, deleted after the
encode), deinterlaces with `bwdif`, and in `--dvd-mode 1080p`
upscales with Lanczos to 1080p (`scale=-2:1080`, even width).
`--dvd-mode 480p` (default) keeps 480p for clean/AI-prep output.
Audio is copied (`-map 0`, subtitle copy with the usual `-sn`
fallback). Verification uses the same hard gate via
`quality_check.verify_reference` against the summed size/duration
and first-VOB makeup; `--sample-quality` is skipped for DVD jobs
(no single timeline to sample). The old script's `tune=animation`
is not carried over — encoder profiles in `encoders.py` apply to
every source. IFO chapter counts are reported as unknown: the
chapter tables are not parsed, and the old MediaInfo shortcut
counted the wrong thing.

Limits, read before running: `--replace` is refused for DVD jobs
(the original is a directory; the converter only knows files), so
DVD outputs always land in `--out-dir` mirroring `--scan-root`
(`<movie>_vts01.mkv`, `_1080p` suffix in 1080p mode) and the disc
folder is deleted by hand after verifying. If the VOBs in a set do
not share one stream makeup, the row carries
`Stream_Makeup_Note` and verification uses the first VOB's makeup.
The state DB fingerprints DVD jobs by summed size only (no single
file mtime); a disc changed without changing total size will not
be re-detected by import alone.

## Camcorder ingest (`camcorder_ingest.py`, 2026-10-28)

Legacy camcorder files folded in from the old
`handbrake_camcorder_conversion` project — as an ffmpeg HEVC lane,
not as a second encoder. HandBrakeCLI, its YAML settings, presets,
and RF quality are gone; what survives is the camcorder-specific
work: finding `.mod`/`.tod`/`.m2t` files the bundled scanner misses,
and carrying their metadata sidecars through a verified encode.

```bash
# Scan a camcorder folder -> one report row per file
python camcorder_ingest.py scan "D:\Camcorder" --out-dir reports
python state_manager.py import --report reports/camcorder_report.json --db state.db
python hevc_convert.py --db state.db --encoder x265

# Or one command, with the flat-file scan in the same DB:
python pipeline.py --scan-dir /mnt/media --camcorder-dir /mnt/media --db state.db --encoder nvenc
```

How a clip becomes a job: files are found recursively
(case-insensitive, hidden directories pruned), including `.mts`,
`.m2ts`, `.mpg`, `.mpeg`, `.vob`, `.mov`, and `.avi` — so a
camcorder root scanned with both scanners yields one kind of job.
The pipeline imports the camcorder report after the main report,
so a file both scanners found keeps the camcorder row
(`Source_Kind`, sidecars, field order). Each file is probed once;
candidate ranking and savings estimates use the scanner's rules.
Sidecars (`.modd`, `.xmp`, `.xml`, the old tool's set) sitting
next to a clip are recorded on the row; `.moi` index files are
ignored, as they were before.

Encoding: a camcorder job encodes like a flat file (same encoder
profiles, staging, verification, and `--replace` rules — unlike
DVD jobs, the original is a file, so opt-in `--replace` with a
backup applies). Two camcorder-only steps run in the converter.
Deinterlace: `--camcorder-mode auto` (default) applies `bwdif`
only when ffprobe's `field_order` says the source is interlaced;
`unknown` field order — common on camcorder containers — means no
filter, never a guess. `deinterlace` forces `bwdif`, `keep` never
applies it. Metadata: with exiftool on PATH (and
`--camcorder-metadata`, on by default), the converter copies the
source file's metadata and merges each sidecar into the staging
file with `exiftool -TagsFromFile … -All:All`, before
verification, so the verified bytes are the published bytes. If
exiftool is absent or a merge fails, the sidecar is copied next
to the published output instead — after a verified publish only,
never after a failed encode (the old tool's bug). Without
exiftool, install it for the merge path; the copy fallback is
what runs on a stock ffmpeg-only box. The conversion JSON
sidecar records the camcorder block (sidecars, field order,
filter, metadata outcome), and the dashboard can filter by
source kind.

Limits: audio is always copied and quality is the suite's
`--crf`/`--preset`; the old tool's audio re-encode options,
sample/random-sample modes, and `additional_args` passthrough
were not carried over.

## Stills / visual check (`stills.py`)

The verifier proves a file decodes — it cannot judge whether
miniature detail smeared. Grab stills before you `--replace`,
especially on the first file of a batch.

```bash
# One video, 12 evenly spaced stills (default)
python stills.py single --input "Blood Bowl. Goblins vs Gnomes.mkv" --out-dir stills/goblins --count 12

# Cadence options (exactly one per run): every 60s, exact times, or % positions
python stills.py single --input a.mkv --out-dir stills/a --interval 60
python stills.py single --input a.mkv --out-dir stills/a --timestamps 10 90 1:30 0:05:00
python stills.py single --input a.mkv --out-dir stills/a --percents 10 50 90 --start 30 --end -10

# Many videos: one subfolder per video, from a glob or a scanner report
python stills.py batch --glob "media/**/*.mkv" --out-dir stills/batch --count 8
python stills.py batch --report reports/video_optimization_report.json --priority medium --limit 5 --out-dir stills/batch --count 8

# Original vs converted, SAME timestamps, side by side (left=original, right=converted)
python stills.py compare --original orig.mkv --converted orig.hevc.mkv --out-dir stills/compare --count 12
# Layout: compare/original/, compare/converted/, compare/side_by_side/compare_001_t120s.jpg
# --no-keep-individuals keeps only the side-by-side images
```

Notes: `--seek accurate` (default) decodes to the exact timestamp —
slower, but original and converted land on the same moment, which
is the whole point of a comparison; `--seek fast` seeks to the
nearest keyframe and can be seconds off, and differently off for
each file. `--width 960` resizes stills, `--quality 2` is JPEG
quality (1 best – 31 worst, PNG ignores it), `--dry-run` lists
timestamps without extracting, and every run writes
`stills_manifest.json` (source, exact timestamp, output path) in
the output folder. The progress bar counts stills, not frames
inside a still — same caveat as the converter.

## Bloat flags (scanner, 2026-10-25)

The report now carries a `Bloat_Flags` column per file, plus a
"Bloat flags" section in the Markdown summary (files flagged, total
size, counts by flag). Flags, from ffprobe stream data only, never
guessed:

- `lossless-audio:<codec>` — PCM, FLAC, ALAC, TrueHD, DTS-HD
- `high-sample-rate:<Hz>` — any audio track above 48,000 Hz
- `surround-<N>ch` — 8 channels and up (7.1+)
- `high-audio-bitrate:<Mbps>` — a single lossy track above 1.0 Mbps
  (lossless tracks are already flagged above, so they don't double-count)
- `multi-audio:<N>-tracks` — 3 or more audio tracks
- `multi-language:<langs>` — 2 or more known audio languages
- `high-video-bitrate:<Mbps>-vs-<target>Mbps-target` — legacy codec
  at 1.5x+ the per-resolution target, or HEVC/VP9/AV1 at 2x+

No flags means `Bloat_Flags` is `none`. Rows from older reports have
no column at all — those count as unknown, not none, so a re-scan is
needed to populate them. A legacy codec alone is deliberately NOT a
bloat flag (every H.264 file would then be flagged, and the candidate
column already covers it). Not flagged, because this scan cannot
establish them: CBR vs VBR (ffprobe reports no reliable bitrate
mode) and baked-in black bars (needs sampled cropdetect analysis).
Flags change nothing about encoding — the converter still copies
audio — they only say "look here first". Filter the CSV/JSON on
`Bloat_Flags` to find the audio-bloated files in a library.

## Baked-in black bars (scanner `--cropdetect`, 2026-10-25)

ffprobe alone cannot see bars — it reports the stored resolution,
not where the picture ends inside it. Opt in with:

```bash
python video_optimizer_scan.py --dir /mnt/media --out-dir reports --cropdetect
# optional tuning: --crop-samples 8 (default) --crop-limit 24 (default, 0-255)
```

How it works: ffmpeg `cropdetect` at 8 evenly spaced timestamps per
file, 5 frames each, fast seek (`-ss` before `-i` — bars are
geometry, not a moment, so a keyframe offset doesn't matter).
Aggregation is largest-area, not mode: a single frame can
under-report when content edges are dark (a letterboxed testsrc
wandered between 1680 and 1754px wide at one timestamp), so crops
under 10% of the frame are discarded, the largest surviving crop is
the content rectangle, and it must be reached (within 2% of its
area) by at least 2 samples with at least 3 valid crops, or the
result is `inconsistent`. Bars are flagged only when the missing
dimension is at least 8% of the stored dimension.

New columns: `Cropdetect_Crop` (e.g. `1920x800+0+140`),
`Crop_Bars` (`letterbox` / `pillarbox` / `both` / `none` /
`inconsistent`), top/bottom/left/right bar pixels, and
`Crop_Confidence_Pct`. A flagged crop also adds a
`baked-in-bars:<crop>` token to `Bloat_Flags`, and the summary
gains a "Baked-in black bars" section. Without `--cropdetect`,
`Crop_Bars` is unknown, not none — re-scan with the flag to
populate it. Off by default because this decodes frames, unlike
the ffprobe-only scan (roughly 1–3 seconds per file in testing).
A crop flag does not promise a saving from cropping (flat black is
already cheap at CRF), and the converter does not crop — cropping
is a separate, destructive step that can cut subtitles positioned
in the bars.

## CRF ladder test (`crf_test.py`)

After the full-file CRF 18 test came back larger than the original,
do NOT test other CRFs on whole files. Test them on a clip first:

```bash
python crf_test.py --input "Blood Bowl. Goblins vs Gnomes.mkv" --out-dir crf-test-goblins --start 600 --duration 90 --crfs 20 22 24
```

This cuts one 90s reference clip (stream-copied, original untouched),
encodes it at each CRF with libx265 / preset slow (audio + subtitles
copied, as in the converter), prints size / saving / bitrate / time
per CRF plus a rough full-file projection, writes `crf_summary.json`,
and runs a `stills.py` compare per CRF into `compare_crf<N>/side_by_side/`.
Pick the highest CRF whose stills you cannot tell from the reference,
then test that one CRF on ONE full file before any batch.

Picking the clip matters: use a slice with miniature detail, texture,
and movement, not a title card, black fade, or talking head — a static
slice makes every CRF look tiny and identical, and the projection will
lie. Projections are scene-dependent by nature; they give the order of
magnitude, not a promise for the whole file. Add `18` to `--crfs` if you
want a visual baseline at the old setting. Seek-order gotcha, verified
2026-10-25: the clip cut uses `-ss` AFTER `-i` with `-t`, because `-ss`
before `-i` with `-t` produced a 15s clip for a 10s request.

## Speed / resource use (`resources.py`)

Before this change there was **no** dynamic sizing: one x265 job used
all cores (ffmpeg threads=auto), parallel jobs were manual `--jobs`
only, and `--jobs 3` meant three jobs each fighting for all cores.
Now:

```bash
# Let the machine decide: cores + RAM + encoder -> jobs + threads/job
python hevc_convert.py --db state.db --encoder x265 --auto-jobs
python pipeline.py --scan-dir /mnt/media --db state.db --auto-jobs
```

Rules: x265 auto = min(cores//8, RAM//2 GiB, 4) jobs; NVENC/QSV auto =
min(cores//2, 3) jobs. Threads are always divided (cores // jobs), so
total threads never exceed core count, and an explicit `--jobs` that
would exceed ~80% of RAM gets a warning. `--threads-per-job N` overrides
the division. Sizing happens once at startup — it does not watch load,
thermals, or a second pipeline on the same machine mid-run. For the
test file on this box (2 cores, 7.7 GiB), auto x265 correctly chose
1 job x 2 threads, and the ffmpeg command in the sidecar shows
`-threads 2`.

## Encoder swap (the whole point of `encoders.py`)

```bash
# CPU, best compression (default)
python hevc_convert.py --db state.db --encoder x265 --crf 20 --preset medium

# NVIDIA RTX 3060, ~5-10x faster, ~20-30% larger than x265 at same quality
python hevc_convert.py --db state.db --encoder nvenc --crf 20 --preset p7 --jobs 2

# Intel Quick Sync, i9-10850K (UHD 630)
python hevc_convert.py --db state.db --encoder qsv --crf 20 --preset medium
```

Quality numbers are per-encoder: CRF (x265), CQ (nvenc),
`global_quality` (qsv) — all default 20, all via the single `--crf`
flag so scripts do not need rewriting when you swap hardware.
There is **no silent fallback**: `--encoder nvenc` on a machine
without NVIDIA fails fast before encoding anything. QSV additionally
needs `/dev/dri` and the Intel media driver in Docker (see below).

## Verify gate — what blocks a replace

Hard failures (no replace, row = `failed_verify`): missing/empty
output, no HEVC video stream, duration off by more than
max(2.0s, 1%), audio count/codec/channels changed, any ffmpeg decode
error on a full decode pass, output <5% of original size, error lines
in the encode log. Warnings (logged, do not block): subtitle count
changed (→ `converted_no_subs`), output larger than original,
resolution/fps drift.

Perceptual sample (`--sample-quality` on the converter,
`quality_check.py --sample-quality` standalone): 3×10s windows
(start/middle/end), libvmaf if your ffmpeg has it, SSIM otherwise.
**Advisory only, by design** — it never fails the gate. Notes from
checking this box's ffmpeg (2026-10-25): stock Ubuntu ffmpeg 8.1 has
`ssim`/`psnr` but **no `libvmaf`**, so on stock builds the VMAF step
reports "unavailable" and SSIM is used. For a hard VMAF gate you need
a ffmpeg built with `--enable-libvmaf`, and you should calibrate the
threshold on your own library first (noisy/grainy sources score lower
for reasons that are not encode defects).

## Replace safety, step by step

1. Encode to `.<stem>.hevc-part<ext>` in the destination directory
   (same filesystem — required for atomic `os.replace`).
2. `quality_check.verify()` on the staging file. Fail → staging
   deleted, original untouched, DB = `failed_verify`.
3. Move the original to backup (`.bak` alongside, or `--backup-dir`).
4. `os.replace(staging, final)`. If this fails, the original is moved
   back from backup and the job is `failed`, never `converted`.
5. Restore the original's mtime/atime and permissions on the new file.
   Linux birth/creation time cannot be set from Python's stdlib, so it
   is **not** claimed to be preserved — mtime/atime are what
   Plex/Jellyfin sort/display on, and those are preserved.
6. Only now is the DB row marked `converted`/`converted_no_subs`, and
   a `<file>.conversion.json` sidecar is written next to it.
7. Container switch (e.g. `.avi` → `.mkv`, because AVI cannot hold
   HEVC): the final path changes extension, the old path is gone by
   design, and the backup holds the original. Plex/Jellyfin will see
   this as a new file path — rescan after a container-switching run.

Interrupts: SIGINT/SIGTERM terminate the running ffmpeg, delete the
staging file, and mark the job `interrupted`. On the next run,
`mark_all_converting_interrupted()` recovers any job a *killed*
(no-cleanup) run left in `converting`. Nothing is ever marked
`converted` unless steps 2–5 all succeeded.

## Plex / Jellyfin notes

- In-place replace keeps the library path stable, so no library
  rescan mapping breaks — except container switches (above).
- While a run is active, Plex may try to play a file being replaced;
  `os.replace` is atomic, so a player sees either the old file or the
  new file, never a half-written one. Still, run replaces during
  off-hours for large libraries.
- After a replace run, trigger a library scan (Plex: Scan Library
  Files; Jellyfin: Scan Library) so durations/sizes refresh.

## Headless / Docker (Unraid)

```yaml
# docker-compose.yml (example; adjust device/volume paths)
services:
  hevc-pipeline:
    build: .
    volumes:
      - /mnt/user/media:/mnt/media:rw
      - ./state:/app/state
    # NVIDIA (NVENC): uncomment the next two lines + install the
    # NVIDIA Container Toolkit on the host.
    # deploy:
    #   resources:
    #     reservations:
    #       devices: [{capabilities: [gpu]}]
    # Intel QSV: uncomment the device line below.
    # devices: [/dev/dri:/dev/dri]
    command: >
      python pipeline.py --scan-dir /mnt/media --db /app/state/state.db
      --encoder nvenc --preset p7 --jobs 2
```

Notes: the container user must own/write `/mnt/media` (replace needs
rename permission in the directory, not just write to the file);
`--staging-dir` must be on the same filesystem as the media or it is
ignored with a warning; keep `state.db` on a persistent volume, not
in the container layer.

## State DB schema (summary)

- `files(path PK, size_bytes, mtime, duration_seconds, video_codec,
  resolution, bit_depth, candidate, estimated_savings_gb, status,
  output_path, actual_savings_gb, error, encoder, quality, preset,
  attempts, created_at, updated_at, source_kind, source_files,
  video_ts_dir, vts_number, sidecar_files, field_order)` — the last
  six are DVD/camcorder columns (DVD 2026-10-27, camcorder
  2026-10-28; added by migration to older DBs). For flat files they
  are NULL; for DVD jobs `path` is the synthetic title-set key.
- `attempts(id, path FK, started_at, finished_at, status, detail,
  output_path, elapsed_seconds)` — one row per status change, so the
  dashboard's per-file history is an audit trail, not a single cell.

## Tests

```bash
python tests/test_smoke.py
# CPU x265 only (no GPU on a test box): synthetic H.264 → HEVC,
# verifier pass/fail, replace + backup + mtime, DB status, recovery.
# NVENC/QSV are preflight-checked (encoder present in the ffmpeg
# build), not encode-tested here — they need real NVIDIA/Intel
# hardware, which the smoke box does not have.

python tests/test_dvd_ingest.py
# DVD fold (2026-10-27): VTS grouping / IFO headers / concat-list
# escaping / DVD output naming (unit + stub-ffprobe rows), then a
# real-ffmpeg run on synthetic MPEG-2/AC3 VOBs: scan -> DB import ->
# convert (480p and 1080p) -> summed-reference verification ->
# sidecar provenance, menu previews, --replace refusal, and the
# pipeline --dvd-dir dry-run. CPU x265 only.

python tests/test_camcorder_ingest.py
# Camcorder fold (2026-10-28): case-insensitive .MOD/.TOD/.M2T
# discovery, sidecar finding, field-order/deinterlace decisions,
# DB import/export/migration, then a real-ffmpeg run on synthetic
# MPEG-2 .MOD fixtures: scan -> DB import -> convert -> verified
# HEVC + sidecar fallback copy, failed-encode copies nothing,
# fake-exiftool merge path, interlaced bwdif path, and the pipeline
# --camcorder-dir dry-run. CPU x265 only; exiftool is not on the
# test box, so the merge path uses a fake exiftool on PATH and
# the real-exiftool run is a Windows-side gate.
```

## Files

| File | Role |
|---|---|
| `video_optimizer_scan.py` | Analyzer (bundled) — ffprobe scan + High Priority flags + CSV/JSON/Markdown reports. Note: its HEVC savings estimate was patched 2026-10-25 to conservative targets (1080p: 3.0 Mbps, not the original 2.25) — see the comment above `HEVC_TARGETS_MBPS` in the file. The H.264 candidate thresholds and the converter's CRF encoding are unchanged; only the `Estimated_*` columns change. Since 2026-10-25 it also reports full audio/subtitle makeup: `Audio_Stream_Count`, `Audio_Languages`, enriched `Audio_Codecs` (channels, sample rate, language, title, bitrate when ffprobe has it), and `Subtitle_Stream_Count` / `Subtitle_Codecs` / `Subtitle_Languages` (codec, language, default/forced/accessibility flags, title). Old reports without those columns import with subtitle/audio counts as unknown, not zero — re-scan to populate them. Since 2026-10-25 it also reports `Bloat_Flags` per file (lossless audio, >48 kHz, 7.1+, high lossy audio bitrate, multi-audio, multi-language, high video bitrate) — see "Bloat flags". Since 2026-10-25 it also supports opt-in `--cropdetect` baked-in black-bar scanning (`Cropdetect_Crop`, `Crop_Bars`, bar pixels, confidence, plus a `baked-in-bars` bloat token) — see "Baked-in black bars". |
| `resources.py` | Machine-resource sizing (2026-10-25) — detects cores + RAM (stdlib only, psutil optional) and sizes `--auto-jobs` + threads per job. See "Speed / resource use" below. |
| `encoders.py` | Encoder profiles + preflight (the swap point) |
| `state_manager.py` | SQLite state + CLI (import/list/summary/export/recover) |
| `quality_check.py` | Verifier library + CLI |
| `stills.py` | Still grabber (2026-10-25) — single / batch / original-vs-converted side-by-side, with count / interval / timestamps / percents, accurate or fast seek, and a manifest per run. See "Stills / visual check". |
| `crf_test.py` | CRF ladder on a short clip (2026-10-25) — reference clip + libx265 encodes at each CRF + size/bitrate table + rough full-file projection + stills compare per CRF. See "CRF ladder test". |
| `hevc_convert.py` | Processor + verifier + replacer (v2; v2.1.0 adds the DVD title-set path: concat-demuxer input, `bwdif` filter, `--dvd-mode`, summed-reference verification, `--replace` refused for DVD jobs; v2.2.0 adds the camcorder path: field-order deinterlace, exiftool metadata/sidecar step on staging, fallback sidecar copies after verified publish) |
| `pipeline.py` | End-to-end driver with lockfile (2026-10-27: `--dvd-dir` / `--dvd-mode` import DVD title sets into the same DB; 2026-10-28: `--camcorder-dir` / `--camcorder-mode` import camcorder jobs into the same DB) |
| `camcorder_ingest.py` | Camcorder ingest (2026-10-28) — camcorder discovery (`.mod`/`.tod`/`.m2t` + camcorder formats), ffprobe rows, sidecar + field-order recording. See "Camcorder ingest". |
| `tests/test_camcorder_ingest.py` | Camcorder ingest tests, stdlib only (2026-10-28) |
| `dvd_ingest.py` | DVD ingest (2026-10-27) — VIDEO_TS discovery, VTS grouping, IFO headers, per-title-set report rows, menu preview frames. See "DVD ingest". |
| `camcorder_ingest.py` | Camcorder ingest (2026-10-28) — camcorder discovery (`.mod`/`.tod`/`.m2t` + camcorder formats), ffprobe rows, sidecar + field-order recording. See "Camcorder ingest". |
| `tests/test_camcorder_ingest.py` | Camcorder ingest tests, stdlib only (2026-10-28) |
| `LICENSE` | MIT (enonymous1) |
| `CHANGELOG.md` | Version history (v2.0.0 → v2.2.0) |
| `.gitignore` / `.gitattributes` | Runtime outputs (DBs, reports, `converted/`, logs, backups, staging files) are never committed; LF endings on all text files |
| `.github/workflows/ci.yml` | CI: compile + the three test suites on Ubuntu/Windows, Python 3.10–3.12 |
| `tests/test_smoke.py` | End-to-end smoke test, stdlib only |
| `tests/test_dvd_ingest.py` | DVD ingest tests, stdlib only (2026-10-27) |
| `dashboard.py` | Flask + Jinja2 read-only dashboard |
| `tests/test_smoke.py` | End-to-end smoke test, stdlib only |
