# Hourly append implementation record

**Goal:** Resume one MP3/CUE per clock period without suffixes or invented silence.
**Spec:** ../specs/2026-10-29-hourly-append-design.md
**Baseline:** cd16a7f local / 1d4569c remote optimized Python 0.6.1.

- [x] Reproduce session suffix inheritance and confirm FFmpeg file truncate=0 is not append.
- [x] Implement bounded MPEG frame parsing, current-file validation/append, partial-tail repair, old Xing duration updates and compatible-format checks; real FFmpeg matrix at five sample rates and mono/stereo.
- [x] Implement current-hour CUE recovery, preserved prefixes, per-capture-span audio offsets, bounded pending metadata and cap handling.
- [x] Keep FFmpeg continuous input and encoding; route complete reservoir-independent frames into hour files without input restart.
- [x] Deliver initial API playback snapshot independently from history after resume.
- [x] Migrate tests of superseded suffix/segment-muxer internals while preserving hour immutability, archive safety, metadata and concurrency coverage.
- [x] Independent review; reproduce and fix four findings: CUE/audio durability order, pending ICY title at boundary, rejected-frame seam mutation and orphan metadata when audio is absent.
- [x] Local full suite, actual encoder-session append and independent hour decode tests, compileall and diff check.
- [ ] Publish and verify Windows CI build.

Runtime input is still selected Windows audio; metadata remains independent. Archive-wide scans, intermediate audio files and whole-file reencoding are not introduced. Rate/channel changes, and bitrate changes in a headerless existing hour, fail safely. Physical Windows hardware qualification remains separate.
