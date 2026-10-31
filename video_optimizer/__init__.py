"""video_optimizer — HEVC library optimization pipeline.

Installable package (see pyproject.toml): the CLI entry points are
console scripts (``video-scan``, ``hevc-convert``, ``swap-originals``,
``video-pipeline``, ``video-dashboard``, …), and every tool also runs
as ``python -m video_optimizer.<module>`` from the suite folder
without an install. The package layout replaces the pre-2.4.0 flat
scripts in the repository root; there are no root scripts anymore,
and the old ``sys.path`` hacks are gone — modules import each other
with relative imports.
"""
