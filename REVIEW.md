# Staff-engineer review — video-optimizer-suite

**Scope:** the whole suite as of 2026-10-31, reviewed file by file
(scanner/state, converter/verifier/swap, driver/dashboard/tools)
with line-level evidence, then fixed where a fix was small, safe,
and test-covered. **This document is the review; the code changes
listed in §6 are already in this zip.** Everything in §7 is
proposed, not applied — it is the next maintainer's backlog, not
a silent change.

**Verdict, in one paragraph:** the safety core is genuinely good
— verify-before-publish, staged copies, atomic replaces, backups,
and a swap script that refuses to guess are the right design, and
they are implemented carefully. The debt is around that core, not
in it: duplicated helpers that had already drifted, a dashboard
that was one reflected filename away from XSS, a recovery tool
whose dry run wrote files, and a README that contradicted the
code in ten places. None of the P0s required a redesign; all of
them were one-helper or one-line fixes, which is why they are
fixed here rather than merely listed.

**How to read severities:** P0 = data loss, silent corruption,
or a security hole reachable from normal use. P1 = a real bug or
a maintenance trap that will bite at library scale or on the next
edit. P2 = a nit, a doc gap, or a hardening note.

---

## 1. The `--replace` question, settled from code

This was the review's first job, because the whole swap script
exists to answer it. Confirmed in `hevc_convert.py`:

- Report mode drops rows whose `Conversion_Status` is
  `converted` / `converted_no_subs` when `--skip-done` is on
  (the default), and `main()` then prints "No files match…
  (N already converted)". A re-run with `--replace` on an
  already-converted set therefore selects **nothing**.
- With `--no-skip-done`, the same rows flow into `convert_one()`,
  which always encodes before verifying — the only
  existing-output guards are `skipped_exists`, and the
  replace-mode guard only fires when `final != src`, which is
  false for a normal same-extension replace. The result is a
  **full re-encode** (~10 hours for the 19-file Warhammer set).
- `swap_originals.py` is the only path in the suite that moves an
  existing encode into place without encoding. **Do not "fix"
  this by making `--replace` imply `--no-skip-done`** — that
  would silently re-encode on a flag whose name promises a swap.

The converter's closing message and the rewritten README now say
exactly this. The remaining trap is that the *conversion log*
(`conversion_log.csv/.json`) is overwritten every run, so the
bulk run's log contains only the bulk 18 and the Valentines
pairing survives only in the report — the swap script's `--log`
source is inherently lossy (§3, P1).

---

## 2. Best practices & code quality

### P0 — fixed in this delivery

| # | Finding | Fix (file) |
|---|---|---|
| 1 | **Swap rollback broke on cross-filesystem backups.** `_move_to_backup()` fell back to `shutil.move` on EXDEV, but the publish-failure rollback used bare `os.replace`. With `--backup-dir` on another drive — the configuration a careful user is most likely to choose — the rollback itself raised EXDEV and the original sat at the backup with no automatic recovery. | `swap_originals.py`: new `_move_file()` helper (replace, EXDEV → copy+delete) used by backup, rollback, and recovery restore. |
| 2 | **Converter rollback could destroy the target and lied about it.** On publish failure it deleted `final` when `final != src`, then swallowed a failed restore in a bare `except OSError: pass` and re-raised with "original untouched" — false if the restore failed. `Backup_Path` was only set *after* a successful publish, so the failure record never said where the original was. | `hevc_convert.py`: `final` is never deleted on a failed publish (`os.replace` is atomic, so a failed publish leaves it untouched; deleting it could destroy a pre-existing `--overwrite` target); the restore uses a cross-filesystem fallback; `publish_backup` is recorded *before* the publish attempt and the failure message names it when the original is missing. |
| 3 | **Interrupts during verify/staging still published.** The converter checked `_SHUTDOWN` after the encode but not between `verify_files()` and publish; `swap_one()` checked it only on entry, so a SIGINT during `shutil.copyfile` was followed by an unconditional backup+publish. | Both tools now re-check the shutdown flag after verification (converter) and after staging (swap) and return `interrupted` with the original untouched. Residual note: the full-decode subprocess is a plain `subprocess.run`, not registered for termination, so a Ctrl-C during decode lets the decode finish — but its result is now never published after a shutdown request. Registering decode processes is a P2 follow-up. |
| 4 | **`--recover --dry-run` wrote files, and `--recover` always exited 0.** The recover branch ran before the dry-run guard, unconditionally wrote `swap_log`, and returned 0 even on a failed restore. A "preview" recovery mutated state and a failed one was invisible to scripts. | `swap_originals.py`: recover obeys the same gates as swap (dry-run / no `--yes` → plan only, no log write, exit 0 / 2), and a real recovery returns 1 on any failure. Orphan staging cleanup is also gated the same way — it previously deleted files even in a dry run. |
| 5 | **Dashboard XSS via string templates (P0, security).** Templates are rendered with `render_template_string`; Flask's autoescape selection is by filename extension, and string templates have no filename, so `{{ q }}`, `{{ r.filename }}`, `{{ r.path }}`, and attempt details were **unescaped**. A filename from disk or a `?q=` value containing HTML rendered live. | `dashboard.py`: `app.jinja_env.autoescape = True` in `create_app()` (the `|safe` summary fragment stays intentional — it is rendered by the same engine, so its own variables are escaped before insertion). Verified at runtime with Flask's test client (see §6). |
| 6 | **Dashboard bound to the LAN without auth in the quickstart.** `--host` defaults to loopback, but the old README quickstart overrode it to `0.0.0.0` with no warning, exposing `/api/queue` (`SELECT *`, full filesystem paths) and `/file` (whole DB row + attempts) to anyone on the LAN. Read-only, but a full library inventory. | New README binds loopback by default and carries the warning in its Security section; the dashboard module docstring says the same. Do not "fix" this by adding a delete button — read-only is the design. |

### P1 — fixed in this delivery

| # | Finding | Fix |
|---|---|---|
| 7 | **Backup numbering diverged.** Converter's first collision was `.2.bak` (`n=1`, then `n+=1` before naming); swap's was `.1.bak`. A user restoring by hand had to guess which scheme produced the backup in front of them. | Converter now starts at `n=0` in both branches: first collision is `.1.bak`, matching swap. |
| 8 | **Silent 12-bit → 8-bit downconvert, fed by a scanner bug.** `encoders.pixel_format()` returned 8-bit for any depth other than exactly 10, and the scanner's `_bit_depth()` substring-matched (`"12" in "nv12"` is true, so 8-bit NV12 was reported as 12-bit). A true 12-bit source encoded as 8-bit with no warning. | `encoders.py`: 12-bit maps to `yuv420p12le` / `p012le`; depths outside {None, 8, 10, 12} raise instead of guessing. `video_optimizer_scan.py`: suffix-based depth parsing (explicit `p10/p12/p16` suffixes; `nv12`/`nv21` → 8). |
| 9 | **Silent NVENC preset fallback.** `resolve_nvenc_preset()` mapped any unknown preset to `p7`, while x265/QSV raised — a typo like `slwo` silently became best-quality NVENC. | Now raises `ValueError`, like the other two encoders. |
| 10 | **Verify tolerances duplicated and drifting.** Swap's `_fast_probe_check()` re-coded the duration/size checks with literals (`2.0`, `0.01`, `0.05`) that `quality_check.py` defines as named constants, and dropped the resolution check the full gate at least warns on. | Swap now imports `DURATION_TOL_S`, `DURATION_TOL_FRAC`, `MIN_SIZE_FRAC` from `quality_check`. (The full single-gate refactor is §7, item 1.) |
| 11 | **Re-import clobbered user decisions.** `state_manager.import_report()` reset *every* non-`converting`, non-converted row to `pending` — including user-`skipped` and `verified` rows — despite its comment saying it reset "retryable/failed" rows. A routine re-import silently un-skipped files. | New `RESET_ON_IMPORT = CLAIMABLE + ("queued",)`; only those statuses move. `skipped`/`verified` keep their status while metadata refreshes. |
| 12 | **Retryable sets disagreed, undocumented.** `RETRYABLE` included `skipped`/`queued`, `pending_jobs` listed `skipped`, `claim_next` claimed neither. `list` showed jobs the converter would never claim. | New `CLAIMABLE` constant; `claim_next()` (SELECT and UPDATE guard) uses it, `pending_jobs()` uses `RETRYABLE`, and both docstrings state the contract: listing includes skipped/queued for visibility, claiming never takes a skipped job behind the user's back. Also corrected: the claim docstring claimed `BEGIN IMMEDIATE`; the transaction is deferred, and correctness rests on the rowcount guard plus SQLite locking. |
| 13 | **Dashboard: 500s under a converter lock, wrong counts, LIKE bypass.** Default 5 s SQLite timeout vs. whole-encode write locks; the page printed `N file(s)` from a `LIMIT 500` query while the summary counted all files; `q` wrapped in `%…%` without escaping `%`/`_`, so `%` matched everything. | `_connect(timeout=30)` + a 503 handler for `OperationalError`; the page now prints "Showing X of N"; LIKE wildcards (`\`, `%`, `_`) are escaped with `ESCAPE '\'`. |
| 14 | **Docker image baked in the library.** No `.dockerignore` existed, so `COPY . /app/` included `.git/`, `reports/`, `converted/`, `state.db`, and `*.bak`. `.gitignore` does not apply to Docker builds. | New `.dockerignore` (plus a loopback-only, commented dashboard service in compose — compose as shipped runs the pipeline only and previously exposed no port at all). The image still runs as root and its `CMD` is `pipeline.py --help`; both are noted, not changed, in §7. |
| 15 | **Duplicated helpers, three modules.** `_log` (scanner, converter, swap) and `_fmt_bytes` (converter, swap) were byte-identical copies — the drift in finding 7 is what that buys you. | New `common.py` (`log_msg`, `fmt_bytes`, `fmt_seconds`, `atomic_write_json`); all three modules import it and keep private aliases so call sites do not change. The scanner keeps a plain-print fallback so a lone copied scanner still runs. |

### P1 — not fixed (backlog, §7)

- **Verify gate re-implemented, not reused, in swap** (finding 10
  is the tolerance half; the structural half remains): swap's
  fast probe is still a separate code path. One canonical
  verifier with full/fast/probe-only modes, shared by converter,
  swap, and CLI, eliminates the remaining drift.
- **Conversion log overwritten every run** (`hevc_convert.py`
  `write_log()` opens with `"w"`): append, or write per-run
  timestamped logs plus a merged index. Until then, the report
  is the only pairing source that survives a second run.
- **Report write-back not atomic in the converter**
  (`update_report_json()` uses plain `open(..., "w")`): a kill
  mid-write corrupts the checklist. Swap already uses temp +
  `os.replace`; the converter should use `common.atomic_write_json`.
- **Swap always copies, always sequentially.** Even on the same
  filesystem, swap `copyfile`s the whole encode next to the
  original (~12 GB of gratuitous I/O for the 19-file set) where
  `os.replace` would be free, and it loops files one at a time
  while the converter parallelises. Fix: `os.replace` when
  `st_dev` matches, copy only across filesystems; parallelise
  verification (read-only) even if publishing stays serial.
  4 ffprobe invocations + 1 decode per file in full mode, with
  no result caching, is part of the same item.
- **Scanner-unavailable fallbacks contradict each other:**
  `dvd_ingest.py` labels the row High Priority on import
  failure, `camcorder_ingest.py` labels it Low, for the
  identical situation; both catch only `ImportError`, so a
  signature-drift `TypeError` from the scanner call propagates
  uncaught. Pick one fallback policy and catch the call, not
  just the import.
- **Pipeline is a stringly-typed argv reconstruction**
  (`pipeline.py` builds a `convert_argv` list for
  `hevc_convert.main()`): every new converter flag needs a
  pipeline change, and drift is silent (pipeline `--threads` is
  passed only to the scanner, never to the converter;
  `--scan-root` is always the required `--scan-dir`, even under
  `--no-scan`). A `run(options)` API or a typed dataclass is
  the fix; the argv round-trip is the single largest coupling
  in the driver.
- **Claim loop is O(n² log n) at library scale:**
  `claim_next()` re-scans and sorts by estimated savings per
  job, and there is no composite index on
  `(status, estimated_savings_gb)`. Fine at 19 files, a real
  cost at 10,000. Add the index or claim in batches.
- **Unvalidated CLI inputs → tracebacks:** `--threads 0/-1`
  reaches `ThreadPoolExecutor` and raises; `parse_args()` uses
  bare `input()` for missing values, which raises `EOFError`
  under cron/Docker with closed stdin. Validate `--threads >= 1`
  and fail with exit 2 instead of prompting when stdin is not a
  TTY (also: split `parse_args` from `prompt_for_missing` so
  the scanner module is importable and drivable
  programmatically).
- **Corrupt prior report silently drops the checklist:**
  `_load_prior_conversions()` returns `{}` on `OSError` /
  `JSONDecodeError`, so a truncated previous report wipes all
  `Conversion_Status` progress on re-scan with no warning. Warn
  loudly and refuse to overwrite, or back up the prior file
  first. Same shape: a JSON report that is a dict, not a list,
  returns `[]` silently in `state_manager`.
- **Fingerprint broken by the rounded-GB fallback:**
  `state_manager` falls back to `int(Size_GB * 1024**3)` when a
  file has moved, but `Size_GB` was rounded to 3 decimals at
  scan time, so it almost never equals the stored exact
  `size_bytes` — a genuinely unchanged converted row resets to
  `pending` and re-encodes. Compare with a tolerance (±1 MiB)
  or store exact bytes in the report.
- **Unvalidated statuses and encoders:** `set_status()` accepts
  any string (a typo creates a phantom bucket in `summary()`);
  `resources.memory_per_job_gb()` returns 0.5 for any
  non-`x265` string, so a typo'd encoder gets GPU sizing
  silently and then `KeyError`s in `encoders.py`. Validate
  against one shared `ENCODER_NAMES` / status set at each
  boundary.
- **MPEG-1 falls through the candidate rule:** `LEGACY_VIDEO_CODECS`
  includes MPEG-1 but `classify_candidate()` tests only
  H.264/MPEG-4/MPEG-2/WMV/VC-1, so a bloated MPEG-1 file is Low
  unless the GB/hr rule fires. Use the shared set in both places.

### P2 — nits and doc gaps (selected)

- `convert_one()` (~300 lines) and `swap_one()` (~170 lines)
  are multi-concern orchestrators driven by an
  `argparse.Namespace` plus a free-form row dict (14 `getattr`
  fallbacks in the converter alone). Split into
  `verify_pair() → stage() → backup() → publish()` units over
  a typed `Job` dataclass; see §7 item 2 for the shape.
- `convert_one.finish()` catches bare `Exception` around the DB
  update and only warns, so DB, report, and log can permanently
  disagree after a "successful" convert. Make DB-update failure
  a distinct, surfaced status. `swap_one()`'s "never raises"
  docstring is not enforced (`stat()` calls are unguarded) —
  catch `OSError` around them and return `failed`, as promised.
- `quality_check._verify_against()` says "never raises for a bad
  file", but the sample-quality path can propagate
  `subprocess.TimeoutExpired` (only `RuntimeError` is caught
  around the probe). Fix the catch or the docstring.
- Hardcoded gates with no single source of truth:
  `confirm_over_gb` 3.0, the 5% tripwire, SSIM/VMAF thresholds,
  bitrate tables, `VIDEO_EXTENSIONS`, `FFPROBE_TIMEOUT_S` 120,
  `resources.py` budgets (2.0/0.5 GiB, `cores//8`, caps 4/3, 80%
  warning). The bitrate tables are the product's core tuning
  and deserve a config file or CLI overrides, not edits.
- `stills.py` batch subfolders truncate stems to 120 chars, so
  two files sharing a stem collide into one folder; include a
  short hash or the parent dir. Its compare `zip()` silently
  truncates on a failed frame — assert equal lengths.
- `crf_test.py` forces `-pix_fmt yuv420p`, dropping 10-bit, so
  the ladder does not represent 10-bit sources even though the
  suite elsewhere preserves bit depth.
- `dvd_ingest.py` menu frames hardcode `scale=854:480`
  (stretches 4:3 menus) and hardcoded seeks, with no
  width/height option; the DVD lane's always-`bwdif` choice is
  never justified next to the camcorder lane's field-order
  vocabulary.
- `pipeline.py`'s `--dry-run` still runs scan + import and only
  dry-runs stage 3; rename it `--convert-dry-run` or gate the
  earlier stages. Its lockfile trusts PID liveness, so a
  recycled PID makes a dead run look live forever — store PID +
  start time, or use `fcntl.flock` on Linux.
- CI runs only `compileall` + the test scripts (now five, and
  now after `pip install -r requirements.txt`), with no ffmpeg
  install step, no lint, no type check, no timeout, no pip
  cache, and no pinned action SHAs. `requirements.txt` pins
  floors only (`flask>=3.0`, `tqdm>=4.65`) — no lockfile, no
  upper bounds, no dev dependencies. Python matrix 3.10–3.12
  vs. Dockerfile pinning 3.12 is minor drift worth one line.

---

## 3. Commenting & in-code documentation

The suite's "why" commenting is unusually good where it matters
— the swap module docstring, the staging/rollback comments,
the converter's staging/verify/publish comments, the
cropdetect aggregation, and the camcorder field-order reasoning
are all models. The gaps are specific, and most were filled in
this delivery alongside the fixes they explain:

- **Backup numbering** now states its first-collision name in
  code (converter `.1.bak`, matching swap) — the exact thing a
  user restoring by hand needs to know.
- **Shutdown re-checks** are commented at the check, not just
  at the signal handler, so the next reader sees *why* a
  verified file can still be refused after Ctrl-C.
- **Container switch** now explains in code that Plex keys by
  path, so a switched file appears as a *new* file — the README
  explained it, the code did not.
- **Claim vs. list, and import-reset sets** are now documented
  at the constants (`CLAIMABLE`, `RESET_ON_IMPORT`), because
  the disagreement was previously discoverable only by reading
  two SQL strings.
- **Cross-filesystem moves** are documented once, in
  `_move_file()`'s docstring, instead of being re-derived at
  each call site.

Remaining doc gaps (not fixed, cheap to close):

- `convert_one()` and `swap_one()` both still claim "never
  raises" without being structured to guarantee it (see P2
  above) — enforce it or change the docstring to "raises only
  on …".
- `swap_originals.py` `recover_pairs` backup-candidate search
  (including the backup-dir-only `Foo_1.mkv` glob) needs a
  why-comment covering the multi-candidate refusal, which is
  the actual safety behaviour; today only the glob itself has a
  one-liner.
- Missing docstrings: scanner `_stream_title`, `_kbps_to_mbps`;
  `state_manager.close`, `summary`, `export_json`, `main`;
  `encoders.quality_label`; `pipeline.py` argv reconstruction
  (why strings, not a namespace); `dashboard._queue` (the 500
  cap, LIKE behaviour, `source_kind` guard).
- Magic thresholds with no derivation: candidate ratios
  (1.5×, 1.0×, 2.0×, 1.75×, `target * 0.45` GB/hr, 60-second
  clip cutoff) and bloat thresholds (48 kHz, 1.0 Mbps, 8
  channels, 3 tracks) document the *what* but not *why these
  values* or what would justify changing them. The 1.0 s mtime
  tolerance in fingerprinting likewise never states its
  rationale (FAT/exFAT 2 s granularity vs. ext4/NTFS).
- Misleading docstrings still open: `find_video_files` says
  "smallest-first skipped" but sorts by path; `state_manager`
  docstring shows `import --report reports/*.json` but the CLI
  takes a single `--report` path (a glob with 2+ files errors);
  `dashboard.py` docstring says HTMX polls "the summary/queue
  fragments" but only the summary polls — the table goes stale
  while the cards refresh.

**Docstring convention for new code:** explain the *why* and
the *failure mode*, not the *what*. The model comment in this
codebase is the camcorder metadata one ("merged before
verification, so the verified bytes are the published bytes").
A docstring that restates the function name is worse than none
— it costs a reader a line to learn nothing.

---

## 4. Future-proofing & architecture

### The coupling map, worst first

1. **Report JSON is the de-facto database for swap** (no `--db`
   support), so the swap path and the DB path can never be
   reconciled — the very divergence that created the `--replace`
   trap. Any future state work must start here.
2. **`hevc_convert.py` imports five modules at top and threads
   one `Namespace` through all of them.** Swapping an encoder,
   verifier, or state backend requires touching `convert_one`.
3. **The dashboard knows the DB schema by `SELECT *`**, so any
   schema migration silently changes the API payload.
4. **Untyped dict rows are the shared schema, maintained twice.**
   The scanner's `CSV_COLUMNS` is the schema, but
   `state_manager` hand-maps ~35 keys on import and hand-maps
   them back on export — and the two mappings have already
   drifted: `export_json()` omits `Size_MB`, `Bit_Depth`,
   `Pixel_Format`, `Aspect_Ratio`, `FPS`, `Audio_Channels`,
   `Audio_Bitrate_Mbps`, `Overall_Bitrate_Mbps`, `Bloat_Flags`,
   and all `Crop_*` columns, so a DB → JSON → DB round-trip
   silently loses data. A single `TypedDict`/dataclass row type
   plus one field-mapping table used by both directions is the
   fix (the new README already says export is a subset, so the
   docs no longer overclaim).
5. **No packaging.** No `pyproject.toml`, `setup.py`, or
   `__init__.py`; scripts import each other as top-level
   modules and depend on callers inserting the script dir into
   `sys.path`. That blocks `pip install`, import from anywhere,
   and test isolation. This is the main blocker to installing,
   testing in isolation, and scaling the suite.
6. **Migrations are ad-hoc.** `state_manager`'s `ALTER TABLE`
   loop has no schema-version tracking; the next fold (DVD →
   camcorder → ?) appends another tuple to the same list. A
   `schema_version` table + named migrations makes the history
   auditable.

### Concrete modularisation (proposed, in order)

```python
# 1. One canonical verifier — converter, swap, and CLI share it.
#    quality_check.py already owns the constants; expose modes
#    instead of letting swap re-implement the fast path.
class VerifyMode(Enum):
    FULL = "full"      # probe + full decode (the publish gate)
    FAST = "fast"      # ffprobe only (pre-swap triage)
    PROBE_ONLY = "probe"  # parsed ProbeResult, no judgement

def verify_pair(original, converted, mode=VerifyMode.FULL) -> VerifyResult:
    """Single gate. Tolerances come from quality_check constants
    only; callers never re-type 2.0s / 1% / 5%."""

# 2. One publish engine — the stage/backup/publish/rollback
#    sequence that today lives twice (converter, swap) with
#    different bugs each time. Injectable filesystem + verifier
#    fakes make the P0 paths unit-testable without 2 GB files.
class PublishEngine:
    def __init__(self, fs, verifier): ...
    def stage(self, converted, original) -> Path: ...
    def backup(self, original, backup_dir=None) -> Path: ...
    def publish(self, staging, original) -> None: ...
    def rollback(self, backup, original) -> None: ...  # via _move_file

# 3. One typed job — replaces Namespace + row-dict plumbing.
@dataclass
class Job:
    path: Path
    source_kind: str = "flat"          # flat | dvd | camcorder
    encoder: str = "x265"
    quality: int = 20
    preset: str = "slow"
    replace: bool = False
    backup_dir: Path | None = None
    # ... one field per flag, validated at construction,
    # instead of 14 getattr(args, ...) fallbacks in convert_one.

# 4. A package, last (it depends on 1–3 being importable):
#    video_optimizer/ with probe.py, media.py (shared ffprobe /
#    codec / bit-depth helpers — the ingest lanes' copies merge
#    here), ingest.py, verify.py, publish.py, and console-script
#    entry points (hevc-convert, swap-originals, video-scan).
#    Keep the CLI scripts as thin __main__ shims so existing
#    `python script.py` invocations keep working.
```

### What blocks scaling, honestly

Sequential swap, per-file full decode with no caching, no
packaging, and the log-overwrite (§2, P1 backlog). None of
these matters at 19 files; all of them matter at 1,000. The
scanner's `--threads` exists; the ingest lanes have none
(DVD VOB probing and camcorder row-building are sequential
loops). `find_video_files` materialises the whole library
(`rglob` + `stat` per file + `sorted()`, then a second `stat`
in `extract_row`); stream it for very large libraries. The
cropdetect samples (8 ffmpeg invocations per file, sequential)
are independent — a small worker pool, or one invocation with
multiple `-ss` outputs, cuts the 1–3 s/file cost substantially.

### Config, as a design rule going forward

Today there is no config file, env-var, or constants module:
dashboard port 5000, pipeline `--threads 8`,
`--confirm-over-gb 3.0`, CRF defaults (start 600, duration 90,
CRFs 20/22/24), DVD menu seeks (1, 5, 15, 30) and
`scale=854:480`, stills `--side-width 960`, queue `LIMIT 500`.
None is wrong individually; collectively, tuning one run means
editing code or re-typing flags. The rule for new constants:
if a user might reasonably want a different value, it is a
flag or a config key, not a literal — and the NVENC/QSV magic
numbers (`-tune hq`, `-rc-lookahead 32`, `-look_ahead 1`, the
`fast`/`medium` → `p5` map collision) get a why-comment at
minimum, since they are the next person's silent-behaviour
change.

---

## 5. README modernization

The old README was ~610 lines with 19 sections, no table of
contents, and a chronological v1/v2 history section as its
organising principle. It has been rewritten (see `README.md`),
not patched, around the five sections requested:

- **Description** — what the suite is, the four-stage pipeline,
  the safety model stated before any command, the architecture
  diagram, and a contents list.
- **Prerequisites** — Python, ffmpeg/ffprobe, disk-space budget,
  optional Flask/tqdm/exiftool, GPU/driver notes, OS notes.
- **Installation** — venv + `pip install -r requirements.txt`,
  with the stdlib-only core called out, and a toolchain check
  before any real run.
- **Local development** — layout table (one row per file, no
  duplicates, no stale `(NEW)` labels), test commands, CI
  description, and the "do not copy probe helpers" rule.
- **Deployment** — Docker (with `.dockerignore` and the
  loopback-only dashboard note), cron/systemd, backup retention.

Everything else (scanning rules, encoders, verify gate, stills,
swap, DVD/camcorder, Plex notes) moved under **Guides**, and the
top three user errors in this project's history — bare report
filename, the `--replace` re-run trap, DB-vs-report disagreement
— got their own **Troubleshooting** table instead of living
deep inside other sections. A **Security** section carries the
dashboard auth warning that the old quickstart omitted.

### Stale/wrong claims corrected in the rewrite

1. Header said the bundled scanner was "verbatim … unchanged";
   the Files table said it was patched 2026-10-25 to
   conservative targets. The patch is real — the new README
   states it in the layout table and drops the header claim.
2. `(NEW, …)` labels on files dating from v2.0.0 in a v2.3.0
   suite — dropped.
3. "Nothing depends on the old `video-meta-scan/` folder" vs.
   `pipeline.py` falling back to `../video-meta-scan/...` when
   the bundled copy is missing. New wording: the bundled
   scanner is the default; the sibling folder is a fallback
   only if the bundled copy is missing.
4. Bloat section said black bars were "not flagged, because
   this scan cannot establish them" — and the next section
   documented `--cropdetect`, which does add
   `baked-in-bars:<crop>`. The Bloat section predated
   cropdetect and was never updated. Fixed, and the flag list
   now includes `baked-in-bars`.
5. Purge-backup command (`--replace --purge-backup` standalone)
   does nothing with default `--skip-done`. New README states
   purge happens in the *same* successful replace run.
6. "pip install -r requirements.txt # flask only; tqdm optional"
   — the file installs both; tqdm is optional *at runtime*,
   not at install time. Stated correctly now.
7. ASCII diagram ended `dashboard.py → pipeline.py`, implying
   the dashboard feeds the driver. It doesn't — the diagram
   and the text now show the dashboard as a read-only view.
8. State DB schema summary omitted ~20 columns (including all
   audio/subtitle count/language/codec columns and the
   DVD/camcorder migration columns) and called migration
   columns part of the `CREATE TABLE`. New README labels the
   layout honestly and notes the export subset.
9. Environment-specific claims presented as general docs ("test
   file on this box (2 cores, 7.7 GiB)", "1–3 seconds per file",
   "stock Ubuntu ffmpeg 8.1 has ssim/psnr but no libvmaf").
   Kept only where the measurement is the point (SSIM vs.
   VMAF availability, which is a stock-build fact), otherwise
   dropped or softened.
10. NVENC "~5-10x faster, ~20-30% larger" repeated from
    `encoders.py` with no benchmark, hardware, or CRF cited
    anywhere in the repo. Softened everywhere to "typically
    faster and larger — measure on your hardware".
11. "Threads never exceed core count" — true only for the
    default path; `--threads-per-job` explicitly allows
    oversubscription with a warning. New wording: "by default".
12. "Exactly one video stream" as a hard check — the code fails
    only on *zero* video streams and warns on >1, checking only
    the first stream's codec. New README: "at least one HEVC
    video stream (more than one is a warning)".
13. "Error lines in the encode log" as a converter hard failure
    — the converter never passes an encode log to the verifier;
    encode-log parsing exists only in the standalone CLI. The
    gate description now says so.
14. "The report gains `Swap_Status`…" without qualification —
    true only for JSON reports with `--update-report` on. Now
    qualified; CSV reports get no write-back.
15. Duplicated Files-table rows (`camcorder_ingest.py` and
    `test_camcorder_ingest.py` twice each, `test_smoke.py`
    twice, unsorted, and missing `requirements.txt` /
    `Dockerfile` / `docker-compose.yml` rows) — replaced by a
    single sorted layout table that includes them.

---

## 6. What was fixed in this delivery, and how it was verified

**Code changes (all in this zip, all backwards-compatible in
CLI behaviour except where the old behaviour was the bug):**

- `common.py` (new): shared `log_msg` / `fmt_bytes` /
  `fmt_seconds` / `atomic_write_json`.
- `swap_originals.py`: `_move_file()` helper; shutdown re-check
  after staging; recover gate ordering + exit codes + gated
  orphan cleanup; fast-probe tolerances imported from
  `quality_check`; docstring comments for the rollback and
  recovery invariants.
- `hevc_convert.py`: shutdown re-check after verification;
  backup numbering `.1.bak` first; `publish_backup` recorded
  before publish + honest failure detail; no deletion of a
  pre-existing `final` on failed publish; cross-filesystem
  rollback fallback; container-switch Plex comment; removed
  unused `resolve_nvenc_preset` import; helpers imported from
  `common.py`.
- `encoders.py`: 12-bit formats; unknown depths raise; unknown
  NVENC presets raise.
- `video_optimizer_scan.py`: suffix-based `_bit_depth`;
  shared `_log` from `common.py` (with a lone-copy fallback).
- `state_manager.py`: `CLAIMABLE` / `RESET_ON_IMPORT`
  constants; `claim_next()` and `pending_jobs()` use them;
  corrected claim-transaction docstring; import preserves
  `skipped`/`verified`.
- `dashboard.py`: forced Jinja autoescaping; 30 s SQLite
  timeout + 503 handler; LIKE wildcard escaping; "Showing X
  of N" count; urlencoded JSON link; auth warning in the
  module docstring.
- `.dockerignore` (new); `docker-compose.yml` loopback-only
  commented dashboard service.
- `tests/test_dashboard.py` (new, skips without Flask);
  `tests/test_smoke.py` section 4 (import preservation +
  claim-never-skips); `tests/test_swap_originals.py` fixture J
  (recover dry-run / no-`--yes` change nothing).
- `.github/workflows/ci.yml`: installs requirements, runs all
  five suites.
- `README.md`: rewritten (see §5). `CHANGELOG.md`: entry for
  this review. `docs/assets/architecture.svg`: the diagram the
  README links (see §8, asset 1 — this one exists).

**Verification run (this box, 2026-10-31):**

- `python -m py_compile` over all scripts and tests: clean.
- All five suites pass: smoke (including the two new checks),
  DVD ingest, camcorder ingest, swap (including fixture J),
  dashboard under a Flask 3.1.3 venv (6 checks: reflected
  `?q=` escaped, count 0-of-2 and 1-of-2, `%` wildcard
  contained, attempt detail escaped, unknown file 404).
  The dashboard suite exits 0 with a SKIP message when Flask
  is absent.
- Targeted unit checks: 12-bit formats per encoder, NVENC
  unknown-preset and 16-bit-depth raises, `_bit_depth` for
  `nv12` (8), `yuv420p10le` (10), `yuv420p12le` (12).
- **Not verified here (needs the maintainer's hardware):**
  NVENC/QSV encodes (no GPU on this box — preflight only),
  exiftool merge on a real camcorder file, the first CI run
  after push, and any visual asset captured from a real run
  (§8, assets 2–5). The README and this review mark each of
  those as unverified rather than asserting them.

---

## 7. Backlog for the next maintainer (proposed, not applied)

In the order a maintainer should take them, with the effort
each roughly is:

1. **Single verify gate** (§4, item 1) — delete swap's fast
   probe, expose `verify_pair(mode=…)`. Medium: one module,
   two call sites, existing tests.
2. **Publish engine + typed `Job`** (§4, items 2–3) — the
   structural fix for `convert_one`/`swap_one`. Large, but it
   is the change that makes the P0 paths unit-testable without
   2 GB fixtures; do not attempt it without the engine's own
   test suite first.
3. **Append, don't overwrite, the conversion log** + atomic
   report write-back via `common.atomic_write_json`. Small.
4. **Swap: `os.replace` when `st_dev` matches; parallelise
   verification.** Small–medium; measure before/after on the
   19-file set, since the current sequential loop is also the
   thing that makes interrupt behaviour easy to reason about.
5. **Packaging**: `pyproject.toml` + `video_optimizer/`
   package + console entry points, scripts as `__main__` shims.
   Medium; do it after items 1–2, not before, or the package
   just re-exports the same coupling.
6. **One row type + one field-mapping table** for scanner ↔
   DB ↔ export, so the export subset stops being a silent
   loss. Medium; the mapping drift list in §4 item 4 is the
   test oracle.
7. **Schema-version table + named migrations; composite claim
   index; fingerprint tolerance.** Three small, independent
   items; the index and the tolerance are one-line each.
8. **Dashboard to file templates** (`templates/*.html`, which
   get real `.html` autoescaping) and drop the forced-autoescape
   line only once that lands; add token auth or a documented
   reverse-proxy example before any LAN use. Small–medium.
9. **Docker hardening**: non-root `USER`, `EXPOSE 5000`,
   `restart:` policy, pinned requirements (lockfile), and an
   ffmpeg install step in CI so the encode suites actually run
   there instead of skipping. Small, except the non-root user,
   which needs volume-permission testing on Unraid.
10. **CLI validation pass**: `--threads >= 1`, non-TTY stdin
    fails exit 2 instead of prompting, corrupt-prior-report
    warning, `set_status`/encoder validation, MPEG-1 candidate
    fix, stills stem-collision hash, CRF 10-bit preservation,
    DVD menu aspect fix. Each is a one-function change; batch
    them as a single "validation" PR with tests, not drive-bys.

**Deliberately not recommended:** making `--replace` imply
`--no-skip-done` (§1); adding a delete/re-encode button to the
dashboard (read-only is the design); actual cropping in the
converter (destructive, subtitle-risky — flag only); audio
downmixing (lossy, irreversible, needs a verifier change);
AV1 output (a new encoder profile, not a review fix); a
`--purge-backup`-as-cleanup-command (it is a same-run flag,
and the README now says so).

---

## 8. Visual asset strategy

One rule governs this section, and it is the suite's own:
**a referenced-but-missing image is left out rather than
shipped broken.** The old README had no visuals at all (its
only "diagram" was ASCII). The new README links exactly one
asset that exists in this zip; everything else below is a
capture plan with exact filenames, exact content, and exact
insertion points — to be captured from real runs, never
fabricated, and linked only once the file exists.

### Asset 1 — Architecture diagram (exists, linked)

- **File:** `docs/assets/architecture.svg` (in this zip).
- **Depicts:** the four-stage pipeline exactly as the code
  runs it — driver (`pipeline.py`) over scan/import/convert;
  scanner + ingest lanes → report → `state.db`; converter +
  encoders + verify gate → `converted/` tree; swap (verify →
  stage copy → `.bak` → atomic publish → rollback) → library;
  dashboard as a read-only reader of `state.db`, labelled
  "no auth · bind loopback".
- **Placement:** README **Description** section, directly
  after the ASCII pipeline, as
  `![Pipeline architecture](docs/assets/architecture.svg)`.
  One diagram, not two: the ASCII pipeline stays as the
  plain-text fallback (it renders in terminals and diffs),
  and the SVG is the visual version of the same flow. Do not
  add a Mermaid copy — three versions of one diagram is how
  they drift apart.

### Asset 2 — Dashboard screenshot (to capture)

- **Depicts:** the queue page on a real (small, synthetic or
  demo) state DB: summary cards, the filter bar, and at least
  one row in each of `pending`, `converted`, and `interrupted`,
  with the "Showing X of N" count visible. Redact or use
  synthetic paths — a real library inventory must not ship in
  a public README.
- **File:** `docs/assets/dashboard-queue.png` (capture at
  ~1400 px wide, PNG, no browser chrome).
- **Placement:** new subsection **"What the dashboard looks
  like"** at the end of README **Guides → Plex / Jellyfin
  notes**, inserted as
  `![HEVC pipeline dashboard queue](docs/assets/dashboard-queue.png)`
  followed by one sentence: "Read-only: it shows queue state
  and summary cards; conversions start from the CLI."
- **Capture command (from the suite folder, after the
  dashboard test DB exists):**
  `python dashboard.py --db state.db --port 5000` then
  screenshot `http://localhost:5000` with the filters cleared.

### Asset 3 — Stills side-by-side (to capture)

- **Depicts:** one frame from a real original vs. its CRF 20
  HEVC encode, side by side, on content with miniature detail
  (the suite's hardest case) — the kind of frame a reader
  would otherwise have to take on faith from a size percentage.
  Generate it with the suite itself, so the asset is also a
  proof the tool works:
  `python stills.py compare --original "Movie.mkv" --converted
  "converted/Movie.hevc.mkv" --out-dir stills/movie-full
  --count 12`, then pick the sharpest frame pair from
  `stills/movie-full/side_by_side/`.
- **File:** `docs/assets/stills-compare.png` (the chosen
  side-by-side frame, not the whole folder).
- **Placement:** README **Guides → Stills and the CRF ladder**,
  directly after the `stills.py compare` command block, as
  `![Original (left) vs CRF 20 HEVC (right), same frame](docs/assets/stills-compare.png)`
  with a caption naming the source file, CRF, and preset.
  If the two frames are indistinguishable at README size, say
  so in the caption — that is the point of CRF 20, and
  claiming a visible difference that is not there would be
  the same fabrication the old "demo GIF" rule forbids.

### Asset 4 — Terminal walkthrough GIF (to capture)

- **Depicts:** the first-run happy path in a terminal, in
  real time, no edits: `scan` (last lines only) → `import` +
  `summary` → `--dry-run` convert → one `--only` swap with
  `--yes` → the swap log line. 60–90 seconds, recorded with
  asciinema or a terminal recorder, exported as GIF.
- **File:** `docs/assets/quickstart.gif`.
- **Placement:** README **Quickstart**, directly after the
  five-step command block and before "One-command version",
  as `![Quickstart: scan, import, convert, swap](docs/assets/quickstart.gif)`.
  The GIF must match the commands printed above it — if a
  flag changes, re-record; a stale GIF is worse than none,
  because a new user copies what they see.
- **Do not record:** a full library conversion (hours long),
  any real library path, or a `--replace` run — the GIF must
  teach the swap path, not the trap.

### Asset 5 — CRF ladder chart (to capture, optional)

- **Depicts:** size vs. CRF for one real file, from
  `crf_test.py`'s `crf_summary.json` (CRFs 20/22/24, preset
  slow), plotted as a simple line/bar chart with the file's
  duration and content type in the title. This is the only
  asset that may be a chart rather than a screenshot, and it
  must be generated from a real `crf_summary.json`, not
  hand-drawn.
- **File:** `docs/assets/crf-ladder.png`.
- **Placement:** README **Guides → Stills and the CRF ladder**,
  after asset 3's caption, as
  `![File size vs CRF for one 90-second clip (preset slow)](docs/assets/crf-ladder.png)`
  with the caption: "Projections are scene-dependent; this
  chart is one file, not a promise for yours."
- **Priority:** lowest of the five. If only one asset gets
  captured after the diagram, it should be asset 2 (the
  dashboard), because it is the only part of the suite a new
  user cannot already picture from the command line.

### Assets explicitly not recommended

- **A demo GIF of a conversion "in progress"** with a fake
  progress bar or a sped-up encode: the suite's own rule
  against faked demos applies, and an encode's duration is
  hardware-dependent anyway.
- **A logo or wordmark:** the suite is a CLI toolset; a logo
  adds nothing a user needs and one more file to maintain.
- **Screenshots of Plex/Jellyfin themselves:** third-party UIs
  change, and the suite's claim is about files, not about
  Plex's rendering of them. The Plex note stays text.
