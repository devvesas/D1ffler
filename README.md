# D1ffler

D1ffler is free software under **GPL-3.0-or-later**. Commercial use and redistribution are allowed under that license. See [LICENSE](LICENSE), [copyright and artwork provenance](COPYRIGHT.md), [third-party notices](THIRD_PARTY_NOTICES.md), and [corresponding source and rebuilding](SOURCE_ACCESS.md). The information dialog's **Licenses** tab includes these documents offline.

This project distributes source code and build scripts only. Run D1ffler as a Python application or build your own executables. Building, testing, and signing executables are the user's responsibility. Public contributions and pull requests are not reviewed; clone the source to maintain your own copy.

A modern PyQt6 desktop diff tool inspired by Beyond Compare:

- two to four comparison panels with baseline and custom names;
- direct text entry and clipboard paste into panels;
- files and folders added through each panel's right-click menu;
- mixed file/text comparison with multi-panel side-by-side output;
- baseline-aligned text hunks, so insertions and deletions do not cascade into later lines;
- previous/next change navigation (`F7` / `Shift+F7`) and cross-panel text search and replace (`Ctrl+F` / `Ctrl+H`);
- folder path search, per-status filters and counts, and changed-file navigation during scans;
- bounded background search inside discovered folder files, with per-panel hits and direct line navigation;
- exact-content detection of renamed or moved files, with a linked move row and direct file comparison;
- reviewed folder sync plans with per-path directions, dry run, conflict policies, optional backups, and reports;
- right-click a changed hunk to apply it to another editable panel; use `Ctrl+S`, Save As, or confirmed Save All for writes.
- use Undo/Redo in the toolbar or pane menu (`Ctrl+Z`, `Ctrl+Y` / `Ctrl+Shift+Z`); edits, applied hunks, selections, and merge choices remain reversible through comparison refreshes.
- three-way text merging with Base, Local, Incoming, and an editable Result; independent edits merge automatically and overlapping edits show conflict blocks.
- Git revision sources for folder comparison panels, using linked worktrees without changing the baseline checkout;
- optional review notes on text hunks and folder file entries, with stale-content warnings and Markdown or HTML reports;
- headless comparison of two files or folders with JSON, unified diff, or folder CSV output and script-friendly exit codes;
- reviewed import and application of unified text patches to editable buffers or folder targets;
- binary inputs are read-only and show sizes, SHA-256 hashes, and first differing byte offsets instead of a misleading text diff; text files detect BOM/UTF-8/Windows-1252 encodings.
- supported images open in a read-only visual view with linked zoom and pan, overlay, and pixel difference modes; the byte summary remains available.
- folder comparison with missing-file messages using panel names;
- background folder scans with incremental results, progress, cancellation, and Refresh;
- dark, native desktop interface with multiple color themes.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe D1ffler.py
```

Application modules live in `src/d1ffler/`. The root `D1ffler.py` and `D1ffler_cli.py` launchers load that package directly. Assets remain in `icons/`, `symbols/`, and `logo/`; build and maintenance tools live in `scripts/`, including `scripts/generate_test_data.py`.

Run the tests from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

## Headless comparison

Use **`python D1ffler_cli.py compare`** to run without opening a window. Two files can produce JSON or a unified text diff; two folders or ZIP archives can produce JSON or the same folder CSV manifest used by the GUI. JSON is the default and writes to stdout unless `--output` is given. If you build executables, `D1ffler-cli.exe` provides the same command and `D1ffler.exe` launches the desktop app.

Run these examples from the project root after installing the runtime dependencies:

```powershell
.\.venv\Scripts\python.exe D1ffler_cli.py compare "C:\My files\left café.txt" "C:\My files\right 日本語.txt" --format diff --output "C:\Reports\changes.diff"
.\.venv\Scripts\python.exe D1ffler_cli.py compare left-folder right-folder --ignore-whitespace --compare-metadata --output results.json
.\.venv\Scripts\python.exe D1ffler_cli.py compare left-folder right-folder --format csv --progress
.\.venv\Scripts\python.exe D1ffler_cli.py compare --help
```

Quote paths containing spaces. Relative paths resolve from the current working directory, including the report path. The report directory must already exist. From source, use `python D1ffler_cli.py compare ...` or `python D1ffler.py compare ...` with the same options.

Exit code `0` means equivalent under the selected rules, `1` means different, and `2` means an invalid, incomplete, or failed comparison. Help goes to stdout; errors, usage errors, and optional progress go to stderr. Reports and diagnostics use UTF-8. A failed comparison does not write a report or replace an existing `--output` file. Reports written with `--output` are replaced atomically, and an output path inside either compared source is refused. A stdout write failure returns `2`; a pipe or redirected file may already contain partial bytes if its write fails. Missing required console streams return `2`, never a successful discarded report.

Capture default JSON through a PowerShell pipeline, or use redirection. Save `$LASTEXITCODE` immediately after invoking the command:

```powershell
$report = .\.venv\Scripts\python.exe D1ffler_cli.py compare "left folder" "right folder" | ConvertFrom-Json
$comparisonExitCode = $LASTEXITCODE
.\.venv\Scripts\python.exe D1ffler_cli.py compare left.txt right.txt > report.json
$comparisonExitCode = $LASTEXITCODE
```

`--output report.json` writes UTF-8 directly, independent of the shell's redirection encoding. Windows PowerShell 5.1 decodes native output using `[Console]::OutputEncoding` and writes `>` text as UTF-16; set `[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()` when consuming Unicode through its pipeline, and use `--output` for a UTF-8 report file. See [PowerShell character encoding](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-5.1) and [redirection](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_Redirection).

Options include repeated `--exclude PATTERN`, `--ignore-whitespace`, `--ignore-line-endings`, `--ignore-gitignore`, `--follow-internal-links`, `--compare-metadata`, and `--text-rules FILE`. The text rules file is a JSON list of rule objects with `name`, `scope`, `pattern`, `mode` (`match` or `line`), and `enabled`. Folder-only options are rejected for file comparisons. Unified diff requires text files of at most 2 MiB each; JSON supports binary files. The JSON report has `schema_version: 1`, `complete: true`, sources, active rules, status, counts, and (for folders) relative paths, per-panel statuses, metadata, and detected moves. Incomplete scans and unreadable entries exit with code `2` and produce no report.

Right-click a panel to add a file, folder, or ZIP archive, or paste text directly into it. Use the `+` button to add up to four panels and rename panels from their headers. Adding a folder or ZIP archive locks the workspace to folder comparison; adding a text file or pasted text locks it to file/text comparison.

Double-click a folder file to open its comparison, then use **Save** or `Ctrl+S` to save the active editable preview. Save is disabled while viewing folder trees or read-only previews; its tooltip explains why. Saving preserves the file's encoding and asks before overwriting external changes.

Use **Apply patch** in the toolbar to open a unified `.patch` or `.diff` file. The review dialog shows the patch source, target, selected files and hunks, match status, and a result preview. A matching hunk applies at its stated line; a unique displaced match is labeled **offset**. Ambiguous or failed hunks must be deselected or resolved before Apply. Applying to an editable text panel changes its buffer as one undoable edit; use the normal Save action to write it. Applying to a folder shows creates, overwrites, deletes, and renames before a separate confirmation, checks files again before writing, and can back up changed source files outside the target folder. Folder operations already completed are not rolled back if a later one fails. Read-only Git revisions, ZIP entries, binary patches, unsafe paths, and unsupported mode or copy changes cannot be targets.

Click the **ⓘ** button in the top-right corner for in-app instructions. Its tabs cover getting started, text comparison and merge, folders and sync, Git, review notes and reports, and shortcuts.

Use **Three-way merge** in the toolbar to switch to four fixed roles. Add text to Base, Local, and Incoming; Result updates automatically until you edit it or resolve a conflict. Pick a conflict by its Base line range and choose Local, Incoming, or Base, or edit its markers in Result and click **Mark Manual**. If you later change an input after editing Result, click **Rebuild merge** to replace the old result. In merge mode, **Save**, **Save As**, and `Ctrl+S` write only Result.

For Git comparisons, add a repository root as the baseline folder, then right-click another folder panel and choose **Choose revision…**. The picker lists local branches, tags, and the 200 most recent local commits with their short hashes and subjects. Choose **Enter commit hash…** for an older commit. D1ffler creates a linked worktree at the path you approve, or offers to reuse an existing worktree when a branch is already checked out. Branch panels are editable. Tag and commit panels are read-only; right-click one and choose **Create branch from this revision…** to make it editable. Panel labels show the revision, worktree path, and whether it has local changes. A branch worktree with local changes shows its current files rather than a pristine commit snapshot.

Right-click any panel and choose **Manage D1ffler worktrees…** to keep a created worktree or remove it after clearing its panel. Removal is refused while the worktree has uncommitted, untracked, or ignored files. Closing D1ffler leaves worktrees in place. Git is optional; file/text comparison still accepts pasted content next to a tracked file.

Saving an edited Git file writes it to the worktree. To record it in Git, right-click an editable branch panel and choose **Create commit…**. Select files, choose **Review selected patch**, inspect the saved disk content that will be committed, enter a message, then create the commit. The preview includes deletions and binary changes. D1ffler excludes unrelated staged files and requires open editor changes in that worktree to be saved or discarded first. Git status, preview, and commit run in the background; if files or staging change after review, refresh the list and review the patch again.

## Review notes and reports

Right-click a text hunk or a file entry in a folder comparison to add a review note and state. Notes record a review decision; they do not change files. Use **Review notes** to edit, delete, or explicitly reattach a stale note after the sources or file contents change. Notes are saved with recent and named workspaces. They are most useful for comparisons without a Git commit message.

Use **Review report** to export selected notes as Markdown or HTML. Reports include source paths, active rules, and comparison counts. File contents are excluded unless you select the unified excerpt option for current text hunks. Reports label stale notes and partial folder results.

Use **Export comparison** to share the comparison itself without review notes. Text views export a unified diff or an HTML report with side-by-side line numbers. Folder views export a CSV or HTML manifest with per-panel statuses, linked-path targets and decisions, move links, rules, and counts. Choose all rows or the current path/status filter. Incomplete and stale folder results are labeled; absolute source paths and absolute link targets are omitted unless you opt in. Large exports run with progress and can be cancelled without replacing an existing report.

Text views and HTML reports distinguish identical text and format, equivalence under active rules, content differences, and format-only differences. The result notice identifies final-newline, CR/LF/CRLF, encoding and BOM distinctions even when there are no content hunks. Clean file buffers with identical text and encoding/BOM are labeled byte-identical. Edited folder previews compare unsaved buffers; folder rows still classify the saved files. File CLI JSON reports include the same explanation in `text_comparison` for text within the preview limit.

Text masks and the whitespace/line-ending settings apply consistently to text views, files, folder previews, CLI results and HTML reports without changing source text. **Ignore whitespace** removes all whitespace, including line breaks and the final newline. **Ignore line endings** treats CR, LF and CRLF alike while still comparing whether a final newline is present. Encoding and BOM always remain significant, including when ignore rules are enabled. Binary files use byte comparison.

Unified patch exports contain raw decoded content changes, including changes ignored by comparison rules; they never substitute masked or whitespace-normalized text. Their headers state the active rules and result. CR/LF/CRLF use LF patch transport and final-newline markers are retained. Encoding/BOM and line-ending-only changes are described in the header rather than encoded as patch operations; format-only exports may contain no hunks. Applying a patch preserves the target's line-ending style; use Save with encoding for encoding/BOM conversion.

Folder exports can also include per-source file size, modification time, permissions, and underlying content result. Select **Include file size, modification time, and permissions** in the export dialog. The active metadata comparison rule and metadata-only path count appear in reports even when those detail columns are omitted.

## Folder sync

In a folder comparison, right-click a folder panel and choose **Plan folder sync…**. Choose two folder panels, then set an action for each changed file or apply one action to a folder subtree. The plan shows creates, overwrites, deletions, metadata changes, skipped paths, binary files, and excluded paths. Choose **Modified time**, **Permissions**, or **Time and permissions** in either direction to sync only those fields on existing files without rewriting their bytes. The metadata preview shows the current target and source values, including exact timestamps in nanoseconds. Metadata actions always require an explicit choice, even when the metadata comparison rule is enabled. Right-click a file or folder in the tree to start a plan with that path set to copy to a chosen panel. Existing **Apply to panel** actions still edit open files in memory and use the usual Save flow.

Symbolic links, junctions, and other reparse points appear as linked rows with their targets and internal, external, broken, or inaccessible state. By default, comparison checks link metadata and does not read linked files or descend into linked folders. **Follow linked paths within each selected folder** in Folder rules enables comparison-only traversal of internal linked folders and content comparison of internal linked files; cycles stop at the repeated directory. External links are never followed. Folder sync lists linked paths as unavailable with a reason and does not write through them.

ZIP archives can be compared with other ZIPs or ordinary folders without extraction. Add one with **Add ZIP as folder…**, drop a `.zip` file, or pass it on the command line. Archive entries appear in the same folder tree and can be previewed read-only; folder search, filters, move detection, and comparison exports also include them. ZIP entry names are normalized, and unsafe paths, duplicates, encrypted entries, corrupt members, and size-limit violations appear as errors. Archive reads are limited to 64 MiB per entry and 512 MiB of declared content per archive; entries above a 200:1 expansion ratio are rejected. `.gitignore` rules are not applied inside ZIPs. Folder sync involving a ZIP source is unavailable.

Image files use the visual view in direct comparisons and when opened from a folder or ZIP comparison. Use **Side by side**, **Overlay**, or **Pixel difference** above the panels; overlay opacity and the comparison target can be changed there. Zoom buttons or `Ctrl` plus mouse wheel adjust all visible images, and dragging pans them together. **Show byte summary** returns to hashes and byte offsets. Pixel difference requires matching dimensions. Images are never edited by these controls; files over 32 MiB, images above 8 million pixels, and files Qt cannot decode fall back to the byte summary with a reason.

Use **Dry run** to check the plan without changing the compared folders. **Apply plan…** asks for confirmation before writing. Choose whether changed targets are skipped, stop the plan, or are overwritten. An optional backup directory stores copies of overwritten, deleted, and metadata target files, preserving their previous metadata. Each file is reported separately; metadata reports include each field's before/after values and completion state. Bytes and supported metadata are rechecked immediately before each metadata write. Cancellation stops between files or metadata fields and leaves completed operations in place. Changes detected during an operation stop its remaining fields. Windows permissions mean writable/read-only state; POSIX permissions use mode bits. Unavailable fields disable the action with a reason; unsupported writes or values a filesystem cannot retain are reported as unavailable. Dry run validates reviewed values and conflicts but cannot test filesystem write support. Read-only tag and commit panels cannot be targets. Reports can be exported as JSON and are included when you save the workspace; reopen a workspace and use **View sync reports…** from a panel's right-click menu.

To undo completed sync actions, open a sync report that used backups and choose **Review restore…**. The restore plan shows the current target, saved backup, and proposed action for each completed operation. Overwrites and deletions restore from their backup; metadata actions restore only their recorded fields without rewriting bytes; files created by the sync can be removed. Changed targets default to skip and require an explicit overwrite policy. Older reports without a post-sync fingerprint also require explicit overwrite. You can dry run or cancel the restore. A separate restore report records the outcomes and can be reopened with **View restore reports…**; the original sync report and backups remain in place.

## Failures and diagnostics

Unexpected background errors mark the owning comparison **failed/incomplete**, stop its progress, and retain unsaved buffers. Folder rows already received remain visibly partial. **Refresh** retries the current comparison, including unchanged binary and image sources. Other tabs continue independently. Cancelled, superseded, and closed-tab tasks still record unexpected failures, without applying their results to another comparison. Sync/restore planning, content search, Git operations, and comparison exports use the same task boundary; failed plans remain unavailable for Apply, and failed exports retain the existing report.

Choose **Diagnostics…** to inspect incident records, see their local directory, or export a JSON diagnostic report. Logs live in the per-user D1ffler data directory's `diagnostics` folder (on Windows, normally `%LOCALAPPDATA%\D1ffler\diagnostics`). Storage rotates across three files of at most 256 KiB each, with locks shared by running instances. Records contain incident IDs, operation types, the application version, Python/Qt versions, OS version, exception types, and traceback module filenames, function names, and line numbers. They omit file contents, exception messages, source-code lines, local variables, and absolute paths. Source paths and the local log location are included in an export only when you check the sharing option. Review exported diagnostics before sharing them.

Unexpected GUI callback errors show a failure notice and attempt a recovery snapshot while leaving open edits available. Review an interrupted action's results before retrying it; disk changes already completed are not rolled back. If recovery or logging cannot write, the notice reports that failure. Normal save and close prompts still protect unsaved edits. Native crashes and forced termination cannot be handled by Python's exception hooks.

## Settings

Use the gear button in the top-right corner to switch between Dark, Light, Midnight, Dracula, Nord, and Solarized themes. Settings and saved workspaces live in `%LOCALAPPDATA%\D1ffler\appsettings.json` on Windows, independently of the executable location. Source runs use the same user location. On macOS the directory is `~/Library/Application Support/D1ffler`; on Linux it is `$XDG_STATE_HOME/D1ffler`, defaulting to `~/.local/state/D1ffler`. The settings dialog shows the actual settings path. Missing settings use built-in defaults; unreadable or invalid settings produce a warning and are retained for repair.

When upgrading from an older version, close older D1ffler instances before launching the upgrade. If per-user settings do not yet exist, an `appsettings.json` beside the executable (or beside `D1ffler.py` for source runs) is imported automatically. Existing `D1ffler-recovery` snapshots in that location are copied once into the user directory. Original settings and recovery files are retained. Discarding an imported snapshot does not cause the unchanged legacy copy to be offered again, even if the old portable directory moves.

Settings writes use a process lock and atomic replacement. Independent workspace additions and worktree registrations from different instances are merged; unchanged preferences retain external updates, while explicit preference changes take precedence. Conflicting edits to the same named workspace are refused, including when recent history has refreshed in the meantime. Keep the comparison open and use **Save workspace** with a different name to preserve both versions. Failed named saves report the error, retain the pending comparison, and offer Retry; the **Workspaces** menu also provides a retry action. They cannot be saved indirectly by a later history update. Closing a comparison with a pending named save offers Save, Discard, or Cancel, including when its file buffers are clean.

The **Folder rules** settings tab accepts excluded relative paths one per line or separated with semicolons (for example `.git/;__pycache__/` or `build/;out/`). **Ignore files listed in .gitignore** applies Git's nested ignore rules and exceptions to each folder source, including folders outside a repository. This option requires Git on PATH. Ignored directories are skipped before their contents are scanned. The tab can also ignore whitespace-only or line-ending-only changes for text files; binary files continue to use byte-for-byte comparison.

Enable **Compare modification time and supported permissions** to show same-content files with different metadata as **Metadata only** in folder results and the Changed filter. Selecting a file shows size, local modification time with UTC offset, and permissions in each panel. On Windows, permission comparison uses the read-only or writable state; on POSIX it uses permission bits. ZIP entry modification time and permissions are unavailable and never create a metadata difference. This rule is saved with workspaces and affects comparison results only; folder sync metadata actions are selected explicitly and independently of the rule.

Drop one or more files or folders onto a comparison panel to fill it and any empty panels to its right. Choose **+ New comparison** or press `Ctrl+T` to keep another comparison open. Each tab retains its source panels, unsaved edits, active panel, search, folder filters, selection, scroll position, review notes, and sync/restore reports. Tabs use source names or the saved workspace name and show `*` for unsaved buffers. Switching tabs does not save, discard, or reload edits. Close a tab with its close button or `Ctrl+W`; its own unsaved buffers require Save, Discard, or Cancel. Closing the window checks all tabs and preserves unfinished pasted text through recovery.

Up to eight comparison tabs can stay open. A shared pool runs at most two background jobs across folder scans, text matching, binary/image reads, content search, sync planning, and restore planning. Other jobs wait for a worker; **Cancel work** applies only to the current comparison, and closing a tab cancels its jobs. Completed and late results always belong to their original tab.

Commit, folder sync, folder patch, and restore check affected open files across every tab in the same window, including standalone files and nested folder previews. Unsaved edits require Save, Discard, or Cancel; the prompt names each owning tab and file. Cancel retains the buffers and disk contents. Apply checks again for edits made since review. Saving those edits requires a new review; when several edited buffers refer to the same file, save the desired version from its tab before resolving the others. During a write, affected editors are temporarily read-only and overlapping writes are blocked until it finishes. Completed or partially completed writes refresh changed clean previews and mark affected folder results stale. Dirty buffers retain their edits and disk-change warnings; manually edited merge Results remain intact and become stale when a source changes.

Use **Save workspace** to explicitly save the active tab's setup, filters, rules, review notes, and reports. **Workspaces** opens saved and recent comparisons in a new tab. Opening, switching, or updating a tab never overwrites a named workspace; use **Save workspace** again to update it. Named workspaces store source references and review state, including the selected folder file or preview, expanded tree paths, text cursors/selections, selected hunk, and scroll position. Reopening recomputes the comparison from current sources. Removed paths and changed content reset invalid positions with a visible notice; notes are revalidated against current file bytes and hunk anchors. Unsaved editor text is kept separately in recovery, and saving a named workspace neither saves nor discards those buffers. D1ffler can open up to four paths per comparison from the command line, for example `D1ffler.exe left-folder right-folder`. Separate source sets with `--new-tab`, for example `D1ffler.exe left-folder right-folder --new-tab before.txt after.txt`. Command-line comparisons also open beside restored recovery tabs.

Accepted source paths are stored as absolute locations, so workspaces and recovery continue to use the original files when D1ffler starts from another directory. Older workspaces with relative paths ask you to select their original working folder; use **Save workspace** to retain the located sources. Older recovery offers to locate the original folder or restore edits as text with no assigned file. Saving that text asks you to choose a destination. Cancelling retains the recovery. Report cleanup pauses while saved or recovery sources have unresolved locations.

D1ffler keeps a separate local recovery snapshot for each tab's unsaved file edits, pasted text, and manually edited merge Results after a short idle period and when the window closes. Snapshots live in the user directory's `D1ffler-recovery` folder, separately from settings. On the next launch, recovery dialogs let you inspect recovered text beside the current disk file, restore each comparison into its own tab, discard it, or decide later. Recovery also restores the workspace name, workspace and application text rules, metadata options, folder filters, search settings, review notes, retained report references, and valid review positions. Restoring does not write to source files; changed disk files still require the normal save confirmation. Notes attached to unsaved or changed content remain stale until reviewed; recovered merge results are marked stale if their disk inputs changed. Existing version-1 snapshots remain readable. Each snapshot is limited to 24 MiB; pruning normally keeps five sessions and protects every currently open tab, up to eight snapshots when all eight tabs have unfinished text. Running instances retain ownership of their snapshots, so other instances cannot offer, restore, discard, or prune their live buffers. Ownership is released on close and can be reclaimed after a process exits unexpectedly.

New workspace and recovery snapshots refer to sync/restore reports stored once in the user directory's `D1ffler-reports` folder. Retained reports are historical records of earlier operations, never evidence of a current completed comparison. Missing or altered report files are labeled unavailable; their references are kept. Each snapshot retains up to ten references of each report type, and each report has a 24 MiB limit. Older embedded report history is still supported.

Open **Workspaces → Local history and storage…** to see local locations and sizes, saved/recent comparisons, recoveries, reports and diagnostics. Inspect abandoned recovery without restarting and restore into a new comparison tab; live snapshots remain owned by their running instance. Selected history removal deletes its setup, rules, notes and report references, while recovery discard deletes the selected recovered text. Reports stay until a separate cleanup. Diagnostic clearing removes only local logs and their rotations; exports stay intact. These actions preserve source files, Git worktrees, sync backups and open edits.

**Preview report cleanup…** lists unreferenced report copies older than the selected age and lets you uncheck files to keep. Cleanup checks saved/recent workspaces, all recovery snapshots, and live references from other instances again before deletion. Reports loaded or retained by open comparisons stay protected until all comparisons using that state directory close. Unreadable, corrupt or unsupported reference state blocks report cleanup, and a deletion failure stops cleanup with an error. Retention is disabled by default; the optional saved rule runs on startup and removes only unreferenced reports older than its age threshold. It does not prune workspaces, recovery or diagnostics.

To back up complete user state, close **all** D1ffler instances and copy the whole local state directory shown in the management view to a private backup. Include `appsettings.json`, `D1ffler-recovery`, `D1ffler-reports`, `diagnostics`, `local-data-policy.json` if present, and recovery migration metadata. Before restoring, close all instances, preserve a copy of the existing directory, and replace it with the complete backup at the same user-state location. Restore settings, recovery and reports together so their references match. This backup contains recovered source text, paths, review notes and report details; keep it private and review any exported recovery or diagnostic bundle before sharing. User-state backups do not contain comparison sources, Git worktrees or sync backup contents; back those up separately.

Large folder comparisons show pending files while scanning and keep the window interactive. **Cancel work** leaves the results visibly partial; **Refresh** starts a new scan and checks files again. Opening a file does not wait for the rest of the folder scan. Binary summaries and larger text line matches run in the background. Editor previews are limited to 2 MiB per file; larger files show metadata instead. Building a folder sync plan also runs in the background and enables its actions only after the plan is ready.

In folder view, type part of a relative path in the search field, or press `Ctrl+F` to focus it. Search matches paths as they are discovered. The status menu filters added, removed, modified, metadata-only, identical, error, pending, moved, and linked results and shows counts for each; **Changed** remains a quick filter. `F7` and `Shift+F7` move through visible changed files, and selecting a path reveals it in every panel, including panels where that file is missing. Refresh keeps the search, filter, selection, expanded folders, and scroll position when those results still exist. Open a file to use `Ctrl+F` for text search in its editors.

Use **Search contents…** beside the folder path field to find literal text across selected folder panels. Choose all discovered paths or only the current path/status filter; results show the relative path, panel, line, column, and excerpt. Double-click a hit to open that file at the match. The search is separate from folder path search and text Find. It reads supported ZIP entries, runs independently of the folder scan, and can be cancelled. Each file is limited to 2 MiB, each search to 128 MiB and 5,000 displayed hits. The result summary counts skipped binary, oversized, linked, unreadable, missing, and pending entries, and labels partial or stale results. Refresh the folder comparison and search again before opening a stale hit.

In text comparison, `Ctrl+F` opens the search bar and `Ctrl+H` focuses its replacement field. `F3` and `Shift+F3` visit matches across panels in either direction, with a wrap notice. The bar shows total and active-panel match counts and offers case-sensitive and whole-word matching. Replace Next and Replace All change only the active editable panel; Replace All confirms its panel and match count first. The changes stay in the editor until saved and can be undone. Read-only Git revisions and ZIP entries cannot be replaced. Opening another file from a folder keeps the text query; folder path search remains separate.

After a folder scan, D1ffler hashes unmatched paths in the background and links uniquely matching content under **Detected moves**. Double-click a move to compare its old and new paths directly. In two-panel comparisons the linked row replaces the separate addition and removal in the tree; three- and four-panel comparisons show each panel pair explicitly. Ambiguous or edited moves remain separate. Detection never changes files, and sync plans still review the underlying paths and conflicts independently. The move search hashes at most 5,000 candidate files per scan; the status says when that limit was reached.

Example:

```json
{
  "theme": "light"
}
```

## Build an executable

Executable builds are optional and performed by users. The supplied build profile requires **Windows x64, 64-bit Python 3.10.7**, and an interactive desktop. Put that interpreter on `PATH`, then run from the project root:

```powershell
.\build.ps1 -Release
```

The command creates a fresh virtual environment, installs the exact runtime/build/test locks in `requirements/`, runs source tests, builds the GUI and CLI with the version from `src/d1ffler/app_version.py`, and checks both executables. It downloads and verifies dependency source archives listed in `licensing/inventory.json`; an internet connection is needed for uncached dependencies and archives. Git is optional when building an exported source snapshot.

Outputs are printed at completion and placed in a unique `dist/candidates/D1ffler-<version>-windows-x64-<build-id>/` directory. The portable `D1ffler-<version>-windows-x64.zip` contains `D1ffler.exe`, `D1ffler-cli.exe`, instructions, and notices. The matching `D1ffler-<version>-corresponding-source.zip`, checksums, build record, and verification logs are beside it. Extract the portable ZIP before launching either executable. Failed builds retain logs and never reuse old executables.

The scripts produce **unsigned executables** and do not publish them. You are responsible for testing your builds and obtaining and applying any required code signatures. Signing changes executable bytes; regenerate archive checksums if you repackage signed files. If you redistribute an executable, retain its notices and provide the matching application and dependency sources as described in [SOURCE_ACCESS.md](SOURCE_ACCESS.md).

For local builds without the source suite, use `.\build.ps1`. To use an environment that already contains the exact build-lock versions:

```powershell
.\build.ps1 -SkipInstall
```

Run that command with the prepared environment's Python on `PATH`; for example, activate a virtual environment after installing `requirements/build.lock.txt`. The builder validates the exact inventoried Python version, dependencies, and license texts. Changing those inputs requires updating and reviewing the locks, notices, and `licensing/inventory.json` using `scripts/update_licenses.py`. Project artwork provenance is recorded in [COPYRIGHT.md](COPYRIGHT.md).

Both executables bundle Python, PyQt6, and the shared comparison/report engines. Neither needs Python installed on the target machine. Extract the whole ZIP before launching; the intended target is Windows 10 1809+ or Windows 11, x64. An optional `appsettings.json` beside `D1ffler.exe` supplies initial settings when per-user settings do not yet exist. Settings and recovery writes use the user's data directory; extracting an upgrade to a new folder retains that state.

The packaged driver copies only the executables into an ACL-protected installation directory with Unicode names, removes Python/Git from PATH, and isolates user state. It covers CLI codes/streams/formats/atomic output, metadata, normal GUI startup, bundled assets/help, file/folder/ZIP/image loading, editing/saving, 100/150/200% scaling, and recovery/settings across restarts and executable locations. Run it separately against an extracted candidate:

```powershell
python scripts/smoke_packaged.py --dist-dir "path\to\extracted\D1ffler-1.0.0-windows-x64" --report packaged-tests.json
```

Local `-SkipSmokeTests` records skipped executable verification; `-Release` refuses both skip switches. Automated checks have limits; test your executables on their intended machines and displays. [FIRST_RUN.txt](FIRST_RUN.txt) covers running locally built executables and retaining user data during upgrades.

## Current limits

Three-way merge works on text inputs. Binary files and folders remain available in normal comparison mode.

Save preserves the detected text encoding and BOM, along with line endings and the final newline. The panel shows its current encoding. Right-click for **Save with encoding…** to convert after confirmation, or **Reopen with encoding…** to reinterpret a clean file. With unsaved edits, Reopen shows a disk preview and keeps the edited buffer. Characters that cannot be represented in the current encoding stop the save and offer UTF-8 conversion or a UTF-8 copy. D1ffler watches open files and shows a reload action when their disk content changes. For local edits, the notice offers a disk comparison, Save as copy, or discard and reload. Saving over changed disk content requires confirmation naming the path. Saves use a temporary file and replace the destination only after the write succeeds. Folder results show a stale notice when a watched root changes; use **Refresh** to rescan.
