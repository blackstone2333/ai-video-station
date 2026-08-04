# AVS/qB state divergence during episodic download processing

## Status

Confirmed production recurrence; fixed in AVS v1.5.3. Production deployment and recovery of the already-missing E02 source remain separate operations.

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

- `pytest -q -o addopts=''` → 91 passed
- `python3 -m compileall -q ainas` → passed
- `git diff --check` → passed
- The regression suite includes a temporary-filesystem chain from queued TV download through completion, rename verification, and same-inode hardlink creation.
