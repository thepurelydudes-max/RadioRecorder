# Conservative Python optimization

The user authorized the optimizations identified in the code review, conditional on preserving existing functionality. Keep Tkinter window sizes/widgets/layout, capture and FFmpeg segmentation, CUE serialization/closed-hour immutability, settings defaults/storage, scheduler/manual pause, metadata independence, startup and single-instance protection.

Use targeted changes and regression tests rather than a rewrite. Move blocking recorder commands off Tk with serialized start/stop/close and immutable config snapshots. Move daily cleanup off the recorder watch thread; protect active files with short locks and serialize cleanup runs. Reduce name-selection filesystem calls with a temporary directory index while preserving suffix selection and platform case behavior. Guarantee watch failure cleanup. Report PortAudio overflows without changing PCM or DSP.

Alternatives: a full recorder state-machine/FFmpeg shutdown rewrite has higher compatibility risk and is deferred; disabling EBU filters or batching CUE fsync would change behavior and is excluded. Archive listing may move to bounded background loading while retaining layout, if the lifecycle can be tested safely.

Verify the complete existing suite plus deterministic concurrency tests. Hardware qualification, Windows audio latency and long-duration recording cannot be established by Linux unit tests. Deliver a reviewable GitHub branch/PR, preserving main and existing ZIP archives.
