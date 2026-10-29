# Python optimization implementation plan

> **For agentic workers:** Use test-first implementation and independent final review.

**Goal:** Reduce blocking work without changing recording behavior.
**Architecture:** Retain Recorder and Tk event queue; serialize blocking commands outside Tk. Isolate cleanup worker and temporary naming index.
**Tech Stack:** Python standard library, existing Tkinter/sounddevice/FFmpeg.
**Spec:** docs/superpowers/specs/2026-10-29-python-optimization-design.md

## Global constraints
- Preserve existing Tkinter layout, MP3/CUE formats, schedule/manual pause, current-hour metadata only and saved settings.
- No additional dependencies, no DSP changes, no change to FFmpeg segmentation or normal shutdown protocol.
- Python 3.12 Windows compatibility.

## Review focus
- Start immediately followed by Stop/close: no abandoned encoder or metadata worker.
- Settings change during startup: preserve newly saved retention settings.
- Disk failure: watch cannot die while claiming it is running.
- Large cleanup: metadata and segmentation remain responsive; active files never deleted.
- Case collisions/symlinks: preserve original exists behavior and suffix safety.

### Task 1: Engine and filesystem
Files: engine.py; tests/test_engine_optimization.py.
- [x] Add failing tests for watch exceptions, blocked cleanup concurrent metadata, duplicate cleanup prevention, naming call counts/case/collisions, overflow reporting.
- [x] Run tests, verify intended failures, implement minimal changes without changing public APIs.
- [x] Run python -m unittest discover -s tests -q; commit implementation and tests.

### Task 2: GUI commands
Files: app.py; optional recording_control.py; tests/test_async_ui.py.
- [x] Test blocked start/stop responsiveness, pending-start cancellation/close, scheduler behavior, metadata lifecycle and settings changes during start.
- [x] Implement serialized background commands; Tk handles completion through existing queue.
- [x] Run full suite; commit implementation and tests.

### Task 3: Integration and delivery
- [x] Independent review of combined diff; fix material findings with regression tests.
- [x] Run complete suite, compile check, compare naming output and operation counts to baseline.
- [ ] Prepare patch/source ZIP and publish isolated branch and PR via GitHub connector. Never merge main automatically.

## Verification record

Baseline: 128 tests passed. Final integration: 158 tests passed; compileall and git diff --check clean. Engine regressions observed failing before fixes, including watcher failure, blocking cleanup, stale retention policy, startup retention save, overflow reporting, timeout ownership and termination failure fallback. GUI regressions observed failing before serialized lifecycle/archive loading. Independent review found one termination fallback issue, fixed with two red-to-green tests. A real FFmpeg synthetic 2-second input produced a valid 2.04-second MP3 and CUE. Twenty naming scenarios matched the baseline. Windows hardware was not validated.
