# AVS/qB state divergence during episodic download processing

## Status

Confirmed production recurrence; workflow fixed in AVS v1.5.3 and downloader-path isolation fixed in v1.5.6. The affected E02 source and hardlink have been recovered.

## Symptom

- qB registered an episode torrent and later reported it complete.
- AVS Watchlist did not record the episode/link because a later operation raised an upstream error.
- The naming job stored a completed rename plan before the source path was verified.
- Hardlink processing retried a planned path that did not exist.

## Root cause

The workflow couples multiple independently fallible operations into one success boundary:

1. Watchlist records a release only after `NamingService.add_download` returns.
2. `NamingService.add_download` immediately runs naming after adding the torrent.
3. Naming requires metadata but not completed file content, and stores a preview as a completed result without checking the realized source path.
4. Hardlink discovery assumes the planned renamed path exists and does not account for a qB save path that already includes the leading season directory.

An error after qB accepts the torrent therefore leaves Watchlist state empty, while later stages can trust paths that were never verified.

## Fix

- qB acceptance/enqueue is separated from scheduled naming work.
- Watchlist checkpoints each successfully accepted release, even if a later release in the same check fails.
- Naming waits for both torrent and selected-file completion, then verifies the realized normalized source path before recording completion.
- New downloads use normalized per-title roots beneath the configured media-type directory.
- Existing qB categories are reused without rewriting their save paths; only missing categories are created.
- Ambiguous add responses are reconciled with a read-only hash lookup.
- Hardlink discovery checks normalized, partially renamed, and original path variants while retaining the normalized target name; qB metadata size must match the source file.

## Production safety

Diagnosis was read-only. Do not use the affected E02 torrent as a live test; its source file is absent from both the download tree and media library.

## Verification

- `pytest -q -o addopts=''` → 97 passed
- `python3 -m compileall -q ainas` → passed
- `git diff --check` → passed
- The regression suite includes a temporary-filesystem chain from queued TV download through completion, rename verification, and same-inode hardlink creation.

## Admin progress recurrence (v1.5.4)

The management page could keep showing the progress captured on its initial load even after qB completed a task. A production read-only comparison found 438 matching hashes and zero field differences between AVS `/api/downloader/tasks` and qB `/api/v2/torrents/info`; the affected episode was `pausedUP / progress=1` in both APIs. The stale `0%` was therefore a browser refresh problem, not backend state corruption.

v1.5.4 refreshes visible download data every 15 seconds and exposes the last synchronization time plus a manual refresh button. Progress normalization uses independent completion signals but deliberately preserves qB `missingFiles` and `error` progress, so a task with historical completion metadata and missing source files is not shown as complete. Regression coverage is in `tests/unit/test_qbittorrent.py`; release commit: `a494d52`.

## Downloader path namespace and E02 recovery (v1.5.6)

The apparent E02 data loss was a namespace mismatch. qB mounted host `/volume1/video/Downloads` at container `/Downloads`, while AVS submitted the host-style save path `/volume1/video/Downloads/tv`. qB therefore wrote the complete 512,764,824-byte file into its private container layer and correctly reported 100%, but AVS's `/medialib` mount could not see it.

v1.5.6 adds `downloader_path` to each path rule and translates downloader paths back to NAS source paths before verification and hardlinking. New TV downloads now use `/Downloads/tv/<title>` in qB while AVS retains `/volume1/video/Downloads/tv` as the host source. Existing path-rule files migrate safely by initially treating a missing `downloader_path` as `source_path`; administrators then set the actual container mount.

Production recovery moved only the E02 torrent to `/Downloads/tv` through qB's own set-location operation. The old private-layer path no longer contains a duplicate. Retrying naming job `85fcb71086ca4c348414b086d2d66e56` completed the hardlink: download source and media-library E02 share inode `19520237` with link count `3`. qB remained at 438 tasks and all non-E02 task-state summaries were unchanged. Release commits: `94e0159`, `ce139a6`.
