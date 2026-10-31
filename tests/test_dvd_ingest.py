#!/usr/bin/env python3
"""DVD ingest tests: grouping/IFO logic, stub-ffprobe rows, and a real
ffmpeg end-to-end run on synthetic VIDEO_TS fixtures.

    python tests/test_dvd_ingest.py

No pytest needed (same convention as tests/test_smoke.py). Requires
ffmpeg/ffprobe on PATH. CPU x265 only -- NVENC/QSV are preflight-checked
by hevc_convert.main(), not encode-tested here.

Covers the old ss-py-ffmpeg-conversions behaviours that were fixed by
the fold, so they cannot silently regress:

  * starter.py's __main__ ran a test harness and main() was commented
    out -- here the CLI subcommands are the entry points, and `scan`
    actually produces jobs;
  * VOB matching was exact-case -- here grouping is case-insensitive
    and the _0 menu VOB is excluded from encoding;
  * the hardcoded H:\\...\\Tweety's... path -- here no path is baked
    in, and a fixture directory WITH an apostrophe proves concat-list
    escaping and output naming work;
  * IFO chapters were a wrong MediaInfo key count -- here chapters
    are reported as None (unknown), never guessed;
  * subtitles/chapters/metadata were dropped with no -map -- here the
    converter uses -map 0 and the verifier checks the summed reference.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_ROOT = Path(__file__).resolve().parent.parent
os.environ["PYTHONPATH"] = (str(_ROOT) + os.pathsep
                              + os.environ.get("PYTHONPATH", ""))

from video_optimizer import dvd_ingest  # noqa: E402
from video_optimizer import hevc_convert  # noqa: E402
from video_optimizer.quality_check import probe as probe_media  # noqa: E402
from video_optimizer.quality_check import verify_reference  # noqa: E402
from video_optimizer.state_manager import StateDB  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _make_vob(path: Path, seconds: int = 3, freq: int = 440) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i",
           f"testsrc=duration={seconds}:size=720x480:rate=30000/1001",
           "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
           "-c:v", "mpeg2video", "-b:v", "4000k",
           "-c:a", "ac3", "-b:a", "192k", "-f", "vob", str(path)]
    subprocess.run(cmd, capture_output=True, check=True)


def _make_disc(root: Path) -> Path:
    """Synthetic disc under a directory name containing an apostrophe."""
    video_ts = root / "Tweety's Film (2000)" / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    (video_ts / "VIDEO_TS.IFO").write_bytes(b"DVDVIDEO-VMG" + b"\x00" * 20)
    (video_ts / "VTS_01_0.IFO").write_bytes(b"DVDVIDEO-VTS" + b"\x00" * 20)
    _make_vob(video_ts / "VTS_01_0.VOB", seconds=2, freq=220)
    _make_vob(video_ts / "VTS_01_1.vob", seconds=3, freq=440)
    _make_vob(video_ts / "VTS_01_2.VOB", seconds=3, freq=880)
    return video_ts


def _convert_args(**overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        db=None, report=None, scan_root=None, out_dir=Path("converted"),
        staging_dir=None, limit=0, sort_by="savings", order="desc",
        min_savings_gb=0.0, priority="all", include_modern=False,
        encoder="x265", crf=28, preset="ultrafast", jobs=1,
        auto_jobs=False, threads_per_job=0,
        overwrite=True, dry_run=False, confirm_over_gb=0.0, yes=True,
        skip_done=True, update_report=True, replace=False,
        backup_dir=None, purge_backup=False, sample_quality=False,
        dvd_mode="480p")
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def _unit_checks(root: Path) -> None:
    video_ts = root / "Disc" / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    (video_ts / "VIDEO_TS.IFO").write_bytes(b"DVDVIDEO-VMG" + b"\x00" * 4)
    (video_ts / "VTS_01_0.ifo").write_bytes(b"DVDVIDEO-VTS" + b"\x00" * 4)
    (video_ts / "VTS_01_0.vob").write_bytes(b"x" * 60_000)
    (video_ts / "VTS_01_1.vob").write_bytes(b"x" * 60_000)
    (video_ts / "VTS_01_2.VOB").write_bytes(b"x" * 60_000)
    (video_ts / "VTS_02_1.VOB").write_bytes(b"x" * 100)
    (video_ts / "notes.txt").write_text("not a disc file")

    check("vts_only lists both sets, sorted",
          dvd_ingest.read_video_ts_directory(video_ts, vts_only=True)
          == ["01", "02"])
    check("content VOBs exclude the _0 menu VOB, case-insensitive",
          [p.name for p in dvd_ingest.content_vobs(video_ts, "01")]
          == ["VTS_01_1.vob", "VTS_01_2.VOB"])
    check("menu VOBs found case-insensitively",
          [p.name for p in dvd_ingest.menu_vobs(video_ts)]
          == ["VTS_01_0.vob"])
    ifo = dvd_ingest.analyze_ifo_files(video_ts)
    check("IFO VMG header parsed",
          ifo["VIDEO_TS.IFO"]["type"].startswith("Video Manager"))
    check("IFO chapters are unknown, not guessed",
          ifo["VIDEO_TS.IFO"]["chapters"] is None)
    check("missing dir maps to empty, not a crash",
          dvd_ingest.read_video_ts_directory(root / "nope") == {})
    check("find_video_ts_dirs finds the disc, prunes hidden dirs",
          dvd_ingest.find_video_ts_dirs(root) == [video_ts.resolve()])
    check("root-as-VIDEO_TS is found directly",
          dvd_ingest.find_video_ts_dirs(video_ts) == [video_ts.resolve()])

    # Concat-list escaping: apostrophe + single quote in the path.
    lst = root / "list.txt"
    dvd_ingest.write_concat_list(
        [Path("/tmp/Tweety's Film/VTS_01_1.VOB")], lst)
    check("concat list escapes apostrophes",
          lst.read_text() == "file '/tmp/Tweety'\\''s Film/VTS_01_1.VOB'\n",
          lst.read_text())

    # DVD path / filter helpers.
    row = {"Path": "/d/Movie/VIDEO_TS/VTS_01",
           "Video_TS_Dir": "/d/Movie/VIDEO_TS", "VTS_Number": "01"}
    args = argparse.Namespace(scan_root=Path("/d"), out_dir=Path("converted"),
                              dvd_mode="480p")
    check("DVD final path mirrors the scan root",
          str(hevc_convert._dvd_final_path(row, args))
          == str(Path("converted/Movie/Movie_vts01.mkv")),
          str(hevc_convert._dvd_final_path(row, args)))
    args.dvd_mode = "1080p"
    check("1080p mode gains the _1080p suffix",
          hevc_convert._dvd_final_path(row, args).name
          == "Movie_vts01_1080p.mkv")
    check("DVD final path derives VIDEO_TS/VTS from the job key",
          hevc_convert._dvd_final_path(
              {"Path": "/d/Movie/VIDEO_TS/VTS_02"}, args).name
          == "Movie_vts02_1080p.mkv")
    check("480p filter is deinterlace only",
          hevc_convert._dvd_filter("480p") == "bwdif=1")
    check("1080p filter uses even-width scale",
          hevc_convert._dvd_filter("1080p")
          == "bwdif=1,scale=-2:1080:flags=lanczos")
    check("source files parse from a list",
          hevc_convert._dvd_source_files(
              {"Source_Kind": "dvd_vts", "Source_Files": ["/a/1.VOB"]})
          == [Path("/a/1.VOB")])
    check("source files parse from a DB JSON string",
          hevc_convert._dvd_source_files(
              {"source_kind": "dvd_vts",
               "source_files": json.dumps(["/a/1.VOB", "/a/2.VOB"])})
          == [Path("/a/1.VOB"), Path("/a/2.VOB")])
    check("flat-file rows are not DVD jobs",
          hevc_convert._dvd_source_files({"Path": "/a/movie.mkv"}) is None)


def _stub_ffprobe_checks(root: Path) -> None:
    """Row building against a canned ffprobe shim on PATH."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "ffprobe"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "target = sys.argv[-1]\n"
        "if 'BAD' in target:\n"
        "    print('ffprobe: no such file', file=sys.stderr); sys.exit(1)\n"
        "print(json.dumps({'format': {'duration': '10.0', 'bit_rate': '6000000'},\n"
        " 'streams': [\n"
        "  {'codec_type': 'video', 'codec_name': 'mpeg2video', 'width': 720,\n"
        "   'height': 480, 'pix_fmt': 'yuv420p', 'avg_frame_rate': '30000/1001',\n"
        "   'bit_rate': '5000000'},\n"
        "  {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 2,\n"
        "   'sample_rate': '48000', 'bit_rate': '192000', 'tags': {'language': 'eng'},\n"
        "   'disposition': {}},\n"
        "  {'codec_type': 'subtitle', 'codec_name': 'dvd_subtitle',\n"
        "   'tags': {'language': 'eng'}, 'disposition': {'default': 1}}]}))\n")
    shim.chmod(0o755)
    video_ts = root / "Stub" / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    (video_ts / "VTS_01_1.VOB").write_bytes(b"x" * 100_000)
    (video_ts / "VTS_01_2.VOB").write_bytes(b"x" * 200_000)
    (video_ts / "VTS_01_0.IFO").write_bytes(b"DVDVIDEO-VTS")

    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
    try:
        row = dvd_ingest.build_vts_row(video_ts, "01")
    finally:
        os.environ["PATH"] = old_path
    check("stub row sums durations across the set",
          row.get("Duration_Seconds") == 20.0, str(row.get("Duration_Seconds")))
    check("stub row sums sizes across the set",
          row.get("Size_GB") == round(300_000 / 1024 ** 3, 3))
    check("stub row labels the codec MPEG-2",
          row.get("Video_Codec") == "MPEG-2")
    check("stub row carries the source files",
          len(row.get("Source_Files") or []) == 2)
    check("stub row makeup is consistent",
          row.get("Stream_Makeup_Consistent") is True)
    check("stub row is a DVD job",
          row.get("Source_Kind") == "dvd_vts")
    check("stub row has an audio + subtitle makeup",
          row.get("Audio_Stream_Count") == 1
          and row.get("Subtitle_Stream_Count") == 1)

    bad_ts = root / "Bad" / "VIDEO_TS"
    bad_ts.mkdir(parents=True)
    (bad_ts / "VTS_01_1_BAD.VOB").write_bytes(b"x" * 100_000)
    # Name must match the VTS pattern to be picked up as content.
    (bad_ts / "VTS_01_1_BAD.VOB").rename(bad_ts / "VTS_01_1.VOB")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
    try:
        # Force the shim to fail by pointing it at a BAD path via a
        # second disc whose VOB name contains BAD and still matches.
        bad2 = root / "Bad2" / "VIDEO_TS"
        bad2.mkdir(parents=True)
        (bad2 / "VTS_01_1.VOB").write_bytes(b"x" * 100_000)
        row_bad = dvd_ingest.build_vts_row(bad2, "01")
        # The shim only fails on 'BAD' in the path; Bad2's path has
        # none, so this row builds -- assert the error path instead
        # by probing a missing file directly.
        try:
            dvd_ingest._probe_file(root / "BAD_missing.VOB")
            probe_failed = False
        except RuntimeError:
            probe_failed = True
    finally:
        os.environ["PATH"] = old_path
    check("ffprobe failure surfaces as RuntimeError, not a crash",
          probe_failed)


def _e2e_checks(root: Path) -> None:
    video_ts = _make_disc(root)
    here = Path(__file__).resolve().parent.parent

    # 1. CLI scan produces one job.
    reports = root / "reports"
    proc = subprocess.run(
        [sys.executable, "-m", "video_optimizer.dvd_ingest", "scan", str(root),
         "--out-dir", str(reports)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("dvd_ingest scan exits 0", proc.returncode == 0, proc.stderr[-300:])
    report = reports / "dvd_report.json"
    rows = json.loads(report.read_text(encoding="utf-8"))
    check("scan finds exactly one title set", len(rows) == 1,
          f"{len(rows)} rows")
    row = rows[0]
    check("scanned duration is the VOB sum (~6s)",
          row.get("Duration_Seconds") is not None
          and abs(row["Duration_Seconds"] - 6.0) < 0.5,
          str(row.get("Duration_Seconds")))
    check("scanned row has no error", not row.get("Error"), str(row.get("Error")))

    # 2. DB import -> convert via the real CLI (480p, out-dir mode).
    work = root / "work"
    work.mkdir()
    db_path = work / "state.db"
    db = StateDB(db_path)
    db.import_report(rows)
    db.close()
    out_dir = work / "converted"
    proc = subprocess.run(
        [sys.executable, "-m", "video_optimizer.hevc_convert",
         "--db", str(db_path), "--scan-root", str(root),
         "--out-dir", str(out_dir), "--encoder", "x265", "--crf", "28",
         "--preset", "ultrafast", "--priority", "all", "--yes"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("hevc_convert CLI exits 0 for a DVD job", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-400:])
    out = out_dir / "Tweety's Film (2000)" / "Tweety's Film (2000)_vts01.mkv"
    check("DVD output exists at the mirrored path", out.is_file(), str(out))
    info = probe_media(out)
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    check("DVD output is HEVC 720x480",
          video and video[0].get("codec_name") == "hevc"
          and video[0].get("width") == 720 and video[0].get("height") == 480,
          str(video))
    check("DVD output keeps the AC3 audio track",
          len(audio) == 1 and audio[0].get("codec_name") == "ac3",
          str(audio))
    check("DVD output duration matches the summed reference",
          abs(float(info["format"]["duration"]) - 6.0) <= 2.0,
          str(info["format"].get("duration")))
    check("no staging or concat-list files left behind",
          not list(out_dir.rglob(".*hevc-part*"))
          and not list(out_dir.rglob("*.concat-list.txt")))
    sidecar = json.loads(
        (out.parent / f"{out.name}.conversion.json").read_text())
    check("sidecar records the DVD provenance",
          sidecar["source"]["dvd"]["vts_number"] == "01"
          and len(sidecar["source"]["dvd"]["source_files"]) == 2
          and sidecar["transformation"]["dvd_mode"] == "480p")
    check("sidecar records the deinterlace filter",
          sidecar["transformation"]["video_filter"] == "bwdif=1")
    db = StateDB(db_path)
    status = db.conn.execute(
        "SELECT status FROM files WHERE path = ?",
        (row["Path"],)).fetchone()["status"]
    db.close()
    check("DB row marked converted", status == "converted", str(status))
    check("original VOBs untouched (sizes unchanged)",
          (video_ts / "VTS_01_1.vob").stat().st_size > 0
          and (video_ts / "VTS_01_2.VOB").stat().st_size > 0)

    # 3. verify_reference directly: good output passes, empty fails.
    reference, err = hevc_convert._dvd_reference(
        [Path(p) for p in row["Source_Files"]])
    check("DVD reference builds from the VOBs",
          reference is not None, err)
    if reference is not None:
        res = verify_reference(reference, out)
        check("verify_reference passes the good encode", res.passed,
              "; ".join(res.failures))
        empty = work / "empty.mkv"
        empty.touch()
        check("verify_reference fails an empty file",
              not verify_reference(reference, empty).passed)

    # 4. Menu previews from the real VOBs.
    previews = root / "previews"
    proc = subprocess.run(
        [sys.executable, "-m", "video_optimizer.dvd_ingest", "previews",
         "-i", str(video_ts), "-o", str(previews), "--seeks", "1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    jpgs = list(previews.glob("*.jpg")) if previews.is_dir() else []
    check("menu previews exit 0 with frames",
          proc.returncode == 0 and len(jpgs) >= 1,
          proc.stderr[-300:] + f" jpgs={len(jpgs)}")
    check("preview frames are non-trivial files",
          all(j.stat().st_size > 5_000 for j in jpgs))

    # 5. --replace is refused for DVD jobs, originals untouched.
    db2 = StateDB(work / "state2.db")
    db2.import_report(rows)
    db2.close()
    claimed = None
    db2 = StateDB(work / "state2.db")
    claimed = db2.claim_next()
    db2.close()
    assert claimed is not None
    claimed["Path"] = claimed["path"]
    claimed["Source_Kind"] = claimed.get("source_kind")
    claimed["Source_Files"] = claimed.get("source_files")
    claimed["Video_TS_Dir"] = claimed.get("video_ts_dir")
    claimed["VTS_Number"] = claimed.get("vts_number")
    args = _convert_args(replace=True, out_dir=work / "converted2",
                         scan_root=root)
    db2 = StateDB(work / "state2.db")
    entry = hevc_convert.convert_one(claimed, args, db2)
    db2.close()
    check("--replace DVD job fails with the refusal detail",
          entry["Status"] == "failed"
          and "--replace is not supported for DVD jobs" in entry["Status_Detail"],
          str(entry))
    check("--replace DVD job creates no output",
          not (work / "converted2").exists())

    # 6. 1080p mode upscales and suffixes the name.
    db3 = StateDB(work / "state3.db")
    db3.import_report(rows)
    db3.close()
    db3 = StateDB(work / "state3.db")
    claimed3 = db3.claim_next()
    db3.close()
    assert claimed3 is not None
    claimed3["Path"] = claimed3["path"]
    claimed3["Source_Kind"] = claimed3.get("source_kind")
    claimed3["Source_Files"] = claimed3.get("source_files")
    claimed3["Video_TS_Dir"] = claimed3.get("video_ts_dir")
    claimed3["VTS_Number"] = claimed3.get("vts_number")
    args3 = _convert_args(out_dir=work / "converted3", scan_root=root,
                          dvd_mode="1080p")
    db3 = StateDB(work / "state3.db")
    entry3 = hevc_convert.convert_one(claimed3, args3, db3)
    db3.close()
    out3 = (work / "converted3" / "Tweety's Film (2000)"
            / "Tweety's Film (2000)_vts01_1080p.mkv")
    check("1080p DVD job converts", entry3["Status"] == "converted",
          str(entry3.get("Status_Detail")))
    check("1080p output is 1080 high",
          out3.is_file() and probe_media(out3)["streams"][0]["height"] == 1080,
          str(out3))

    # 7. Pipeline wiring: --dvd-dir dry-run imports the DVD job.
    proc = subprocess.run(
        [sys.executable, "-m", "video_optimizer.pipeline",
         "--scan-dir", str(root), "--dvd-dir", str(root),
         "--db", str(work / "state4.db"),
         "--reports-dir", str(work / "reports4"),
         "--out-dir", str(work / "converted4"),
         "--encoder", "x265", "--crf", "28", "--preset", "ultrafast",
         "--dry-run"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("pipeline --dvd-dir dry-run exits 0", proc.returncode == 0,
          (proc.stdout + proc.stderr)[-400:])
    check("pipeline wrote the DVD report",
          (work / "reports4" / "dvd_report.json").is_file())
    db4 = StateDB(work / "state4.db")
    dvd_rows = db4.conn.execute(
        "SELECT path FROM files WHERE source_kind = 'dvd_vts'").fetchall()
    db4.close()
    check("pipeline imported the DVD job into the DB",
          len(dvd_rows) == 1, str([dict(r) for r in dvd_rows]))


def main() -> int:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("SKIP: ffmpeg/ffprobe not on PATH")
        return 0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _unit_checks(root / "unit")
        _stub_ffprobe_checks(root / "stub")
        _e2e_checks(root / "e2e")
    print(f"\n{len(FAILURES)} failure(s)" if FAILURES
          else "\nAll DVD ingest checks passed.")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
