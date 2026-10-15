# Release notes

## Unreleased

- Distribute sources and build scripts only; users run from Python or build, test, and sign their own executables.

- Anchor accepted source, editable, Git and backup paths so workspace/recovery reopening from another working directory keeps the original targets. Older relative sources require locating their original folder or restoring edits as text with an explicitly chosen Save target.
- Count actual recovery snapshots during pruning so empty/clean tabs do not evict unfinished edits. Preserve live owners across processes and retain changed or uncertain recoveries when pruning cannot safely remove them.
- Preserve external edits during folder patch application by rechecking file states and path ancestry before replacement/deletion. Verify original backups and report completed operations and partial renames when application stops.
- Protect retained reports used as comparison sources through relative paths, `..`, case variants, junctions/symlinks and hard links. Cleanup and automatic retention preserve uncertain identities and recheck saved, recovery and live references.
- Preserve literal Unicode paragraph/line separators through loading, editing, Undo/Redo, clipboard transfers, saves and recovery. Stop saves and recovery writes if text and ending metadata disagree.
- Preserve dirty buffers, recovery and pending named workspaces when a window or tab close is cancelled after Discard. Explicit saves remain durable; discards apply only after all close checks pass.
- Manage local history and storage from Workspaces: inspect or restore abandoned recovery, remove selected saved/recent history, clear diagnostics, and preview unreferenced report cleanup.
- Offer optional age-based report retention, with shared process coordination and protection for workspace, recovery and live comparison references. Unreadable state blocks cleanup; source files, worktrees, backups, exports and open edits stay intact.

## 1.0.0

Stable version metadata for source releases and locally built executables.

- Include version metadata, dependency notices, and corresponding source with locally built executable archives.
- Preserve editor undo through comparison edits and align text comparison rules with format-only diagnostics across GUI, folders, CLI, and reports.

- Restore comparison rules, workspace names, search/filter settings, notes, and review positions alongside recovered buffers. Named workspaces resume valid folder selections/previews, expanded paths, cursors, hunks, and scrolling without storing unsaved text.
- Revalidate restored notes and hunk anchors against current sources; reset stale view positions and label retained sync/restore reports as historical. Store reports once in local user state and keep only references in new snapshots. Legacy workspaces and version-1 recovery snapshots remain readable; the recovery limit remains 24 MiB.
- Complete the D1ffler console-entry filename rename so source CLI commands and release builds resolve the documented entry point.

## 1.0.0-rc.1

Initial release candidate. The source repository is licensed for public distribution.

- Compare and edit text across two to four panels, with three-way merge, reviewed patches, and explicit saves.
- Compare folders, ZIP archives, binary files, and images; review sync plans and backup restore operations.
- Keep multiple comparisons, named workspaces, review notes, and unfinished edits in per-user state.
- Use the standalone console companion for UTF-8 JSON, unified diff, and folder CSV with exit codes 0/1/2.
- Surface background failures with retry controls and bounded, redacted diagnostics.
- Build portable versioned candidates with locked dependencies, executable metadata, checksums, and verification records.
- License D1ffler under GPL-3.0-or-later, record third-party notices and grouped ChatGPT artwork provenance, show offline licensing documents, and build matching corresponding-source companions with release checks for dependency and notice changes.

Known limits: Git features require a separate Git installation. ZIPs, images, binary inputs, and pinned Git revisions are read-only. Completed disk operations are not rolled back after cancellation or failure. Native crashes cannot be intercepted by Python hooks. See README.md for format, size, and comparison limits.
