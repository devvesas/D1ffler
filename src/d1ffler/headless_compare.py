"""Read-only command-line comparisons using the GUI comparison and export engines."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from collections import deque
from pathlib import Path

from d1ffler.comparison_export import folder_counts, folder_csv_chunks, text_unified_chunks, write_export
from d1ffler.folder_archives import ArchiveIssue, is_zip_source
from d1ffler.text_rules import RuleError, applicable_rules, validate_rule
from d1ffler.text_comparison import compare_text_sources
from d1ffler.background_tasks import BackgroundTask, job_messages


class CompareError(Exception):
    pass


class CommandParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message and file is None:
            raise CompareError("Console output is unavailable; use D1ffler-cli.exe compare")
        super()._print_message(message, file)


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = CommandParser(prog=prog,
                           description="Compare two files or folders without opening a window.")
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--format", choices=("json", "diff", "csv"), default="json")
    parser.add_argument("--output", type=Path, help="Write a report atomically; default: stdout")
    parser.add_argument("--progress", action="store_true", help="Print scan progress to stderr")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--ignore-whitespace", action="store_true")
    parser.add_argument("--ignore-line-endings", action="store_true")
    parser.add_argument("--ignore-gitignore", action="store_true")
    parser.add_argument("--follow-internal-links", action="store_true")
    parser.add_argument("--compare-metadata", action="store_true")
    parser.add_argument("--text-rules", type=Path, metavar="JSON_FILE",
                        help="JSON list of text masks (name, scope, pattern, mode, enabled)")
    return parser


def _rules(path: Path | None) -> list[dict]:
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompareError(f"Cannot read text rules: {exc}") from exc
    if not isinstance(data, list) or any(not isinstance(rule, dict) for rule in data):
        raise CompareError("Text rules must be a JSON list of rule objects")
    return [validate_rule(rule) for rule in data if rule.get("enabled") is True]


def _source_kind(path: Path) -> str:
    if path.is_dir():
        return "folder"
    if path.is_file():
        return "folder" if is_zip_source(path) else "file"
    raise CompareError(f"Input does not exist or is not a regular file or folder: {path}")


def _validate_output(path: Path | None, inputs: list[Path], kind: str) -> None:
    if path is None:
        return
    destination = path.resolve()
    for source in inputs:
        root = source.resolve()
        if destination == root or (kind == "folder" and source.is_dir() and root in destination.parents):
            raise CompareError("Output must be outside the compared inputs")
    if not path.parent.is_dir():
        raise CompareError(f"Output directory does not exist: {path.parent}")


def _report(chunks, output: Path | None) -> None:
    if output is None:
        if sys.stdout is None:
            raise CompareError("Standard output is unavailable; use D1ffler-cli.exe compare or --output FILE")
        # Assemble before writing so an export error cannot leave a partial stdout report.
        report = "".join(chunk for chunk, _units in chunks)
        try:
            sys.stdout.write(report)
            sys.stdout.flush()
        except (OSError, ValueError):
            # Avoid a second failed flush during interpreter shutdown overriding
            # the comparison failure code with Python's shutdown status (120).
            # Windows can report a closed pipe as EINVAL instead of EPIPE.
            sys.stdout = None
            raise
    else:
        write_export(output, chunks, lambda: False)


def _file_comparison(args, engine, rules: list[dict]) -> tuple[dict, list[tuple[str, int]] | None]:
    left_rules = applicable_rules(rules, args.left.name)
    right_rules = applicable_rules(rules, args.right.name)
    matched = [rule for rule in rules if rule in left_rules or rule in right_rules]
    status = engine._files_match_cancellable(args.left, args.right, args.ignore_whitespace,
                                             args.ignore_line_endings, lambda: False,
                                             matched if left_rules == right_rules else None)
    left_content = engine.inspect_file_content(args.left, preview_limit=not (left_rules != right_rules and matched))
    right_content = engine.inspect_file_content(args.right, preview_limit=not (left_rules != right_rules and matched))
    text_sources = None
    text_result = None
    if left_content.text is not None and right_content.text is not None:
        text_sources = [{"text": content.text, "scope": path.name, "encoding": content.encoding,
                         "bom": content.bom.hex()} for path, content in
                        ((args.left, left_content), (args.right, right_content))]
        text_result = compare_text_sources(*text_sources, args.ignore_whitespace, args.ignore_line_endings, rules)
        if status in {"modified", "ignored"}:
            status = text_result["status"]
    if status not in {"identical", "ignored", "modified"}:
        raise CompareError(f"File comparison did not complete: {status}")
    left_stat, right_stat = args.left.stat(), args.right.stat()
    sources = [{"name": "Left", "path": str(args.left.resolve()), "size": left_stat.st_size},
               {"name": "Right", "path": str(args.right.resolve()), "size": right_stat.st_size}]
    summary = {"schema_version": 1, "kind": "file", "complete": True, "status": status,
               "different": status == "modified", "sources": sources,
               "counts": {"identical": int(status == "identical"),
                          "ignored": int(status == "ignored"), "modified": int(status == "modified")}}
    if text_result is not None:
        summary["text_comparison"] = text_result
    if args.format != "diff":
        return summary, None
    left = engine.inspect_file_content(args.left, preview_limit=True)
    right = engine.inspect_file_content(args.right, preview_limit=True)
    if left.text is None or right.text is None:
        raise CompareError("Unified diff requires text files of at most 2 MiB each; use JSON for binary or larger files")
    export_sources = [{**text, "name": source["name"], "path": source["path"]}
                      for source, text in zip(sources, text_sources)]
    return summary, list(text_unified_chunks(export_sources, lambda: False, rules,
                                            args.ignore_whitespace, args.ignore_line_endings))


def _folder_comparison(args, engine, rules: list[dict]) -> dict:
    messages = queue.Queue()
    cancel = threading.Event()

    def strict_inventory(*parameters):
        return engine.folder_inventory(*parameters, strict_errors=True)

    worker = engine._FolderComparisonWorker(
        [args.left, args.right], args.exclude, args.ignore_whitespace,
        args.ignore_line_endings, args.ignore_gitignore, {}, cancel, messages,
        args.follow_internal_links, rules, args.compare_metadata,
        inventory=strict_inventory, files_match=engine._files_match_cancellable,
        detect_moves=engine.detect_exact_moves, hash_file=engine.file_sha256,
        max_move_hashes=engine.MAX_MOVE_HASHES)

    thread = BackgroundTask(None, worker, operation="Headless folder comparison")
    thread.start()
    rows, metadata, maps, links, moves = {}, {}, [], [], []
    errors = []
    finished = None
    pending = deque()
    while finished is None:
        if not pending:
            pending.extend(job_messages(thread, messages))
        if not pending:
            cancel.wait(0.01)
            continue
        message = pending.popleft()
        kind = message[0]
        if kind == "failed":
            raise CompareError(message[1].summary)
        if kind == "cancelled":
            raise CompareError("Folder comparison was cancelled")
        if kind == "batch":
            for relative, statuses in message[1]:
                if statuses is not None:
                    rows[relative] = statuses
            worker.permits.release()
        elif kind == "metadata":
            metadata.update(message[1])
        elif kind == "inventory":
            maps = message[1]
        elif kind == "links":
            links = message[1]
        elif kind == "moves":
            moves.extend(message[1])
            worker.permits.release()
        elif kind == "rule_error":
            errors.append(f"Text rule error in {message[1]}: {message[2]}")
        elif kind == "progress" and args.progress:
            print(f"Compared {message[1]}/{message[2]} paths", file=sys.stderr)
        elif kind == "move_progress" and args.progress:
            print(f"Checked {message[1]}/{message[2]} move candidates", file=sys.stderr)
        elif kind == "finished":
            finished = message
    thread.join()
    if finished[1] or finished[2]:
        raise CompareError(finished[2] or "Folder comparison was cancelled")
    if errors:
        raise CompareError(errors[0])
    if any("error" in statuses or "cancelled" in statuses for statuses in rows.values()):
        raise CompareError("One or more folder paths could not be compared")
    linked = {relative: [panel.get(relative) for panel in links]
              for relative in set().union(*(set(panel) for panel in links))} if links else {}
    move_records = [{"from_panel": move.from_panel, "from_path": move.from_path,
                     "to_panel": move.to_panel, "to_path": move.to_path, "sha256": move.sha256}
                    for move in moves]
    hidden = {path for move in moves for path in (move.from_path, move.to_path)}
    export_rows = [(relative, rows[relative]) for relative in sorted(rows, key=str.casefold)
                   if relative not in hidden]
    counts = folder_counts(export_rows, move_records, linked)
    different = any(status not in {"identical", "ignored", "present"}
                    for statuses in rows.values() for status in statuses)
    rules_summary = {"excluded_paths": args.exclude, "ignore_whitespace": args.ignore_whitespace,
                     "ignore_line_endings": args.ignore_line_endings,
                     "ignore_gitignore": args.ignore_gitignore,
                     "follow_internal_links": args.follow_internal_links,
                     "compare_metadata": args.compare_metadata, "text_masks": rules}
    sources = [{"name": name, "path": str(path.resolve()),
                "type": "ZIP archive" if is_zip_source(path) else "folder"}
               for name, path in (("Left", args.left), ("Right", args.right))]
    archive_issues = {relative: [entry.detail if isinstance(entry, ArchiveIssue) else ""
                                 for entry in (entries.get(relative) for entries in maps)]
                      for relative in rows if any(isinstance(entries.get(relative), ArchiveIssue)
                                                  for entries in maps)}
    snapshot = {"sources": sources, "rows": export_rows, "moves": move_records,
                "links": {relative: [link.as_dict() if link else None for link in entries]
                          for relative, entries in linked.items()},
                "metadata": metadata, "archive_issues": archive_issues,
                "include_paths": True, "include_metadata": args.compare_metadata,
                "scope": "All comparison rows", "discovered": len(rows),
                "excluded": worker.excluded_count, "partial": False, "stale": False,
                "rules": rules_summary}
    summary = {"schema_version": 1, "kind": "folder", "complete": True,
               "status": "different" if different else "identical", "different": different,
               "sources": sources, "rules": rules_summary, "counts": counts,
               "excluded_count": worker.excluded_count, "move_detection_limited": worker.move_detection_limited,
               "paths": [{"relative_path": relative, "statuses": statuses,
                          "metadata": metadata.get(relative),
                          "links": snapshot["links"].get(relative)}
                         for relative, statuses in sorted(rows.items(), key=lambda item: item[0].casefold())],
               "moves": move_records}
    return {"summary": summary, "snapshot": snapshot}


def run_compare(argv: list[str], engine, *, prog: str = "D1ffler.py compare") -> int:
    try:
        if sys.stderr is None:
            raise CompareError("Standard error is unavailable; use D1ffler-cli.exe compare")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
        try:
            args = _parser(prog).parse_args(argv)
        except SystemExit as exc:
            return int(exc.code)
        if args.output is None and sys.stdout is None:
            raise CompareError("Standard output is unavailable; use D1ffler-cli.exe compare or --output FILE")
        kind = _source_kind(args.left)
        if _source_kind(args.right) != kind:
            raise CompareError("Inputs must both be files or both be folders/ZIP archives")
        if kind == "file" and args.format == "csv":
            raise CompareError("CSV export is available only for folder comparisons")
        if kind == "folder" and args.format == "diff":
            raise CompareError("Unified diff is available only for file comparisons")
        if kind == "file" and (args.exclude or args.ignore_gitignore or args.follow_internal_links or args.compare_metadata):
            raise CompareError("Folder rules cannot be used for file comparisons")
        _validate_output(args.output, [args.left, args.right], kind)
        rules = _rules(args.text_rules)
        if kind == "file":
            summary, diff_chunks = _file_comparison(args, engine, rules)
            if args.progress:
                print("Compared 1/1 files", file=sys.stderr)
            summary["rules"] = {"ignore_whitespace": args.ignore_whitespace,
                                "ignore_line_endings": args.ignore_line_endings,
                                "text_masks": rules}
            chunks = diff_chunks if args.format == "diff" else [(json.dumps(summary, indent=2) + "\n", 0)]
        else:
            result = _folder_comparison(args, engine, rules)
            summary = result["summary"]
            chunks = (folder_csv_chunks(result["snapshot"], lambda: False) if args.format == "csv"
                      else [(json.dumps(summary, indent=2) + "\n", 0)])
        _report(chunks, args.output)
        return 1 if summary["different"] else 0
    except (CompareError, OSError, RuleError, ValueError) as exc:
        if sys.stderr is not None:
            try:
                print(f"Comparison failed: {exc}", file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass  # An unavailable diagnostic stream must still return the failure code.
        return 2
