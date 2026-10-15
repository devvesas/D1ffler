"""Streaming, read-only exports of text and folder comparison snapshots."""

from __future__ import annotations

import csv
import difflib
import html
import io
import ntpath
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path
from typing import Callable, Iterable, Iterator
from d1ffler.text_comparison import compare_text_sources, comparison_options_summary, line_key, source_rules


class ExportCancelled(Exception):
    pass


def _check(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise ExportCancelled()


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _label(source: dict) -> str:
    name = str(source.get("name") or "Source").replace("\r", " ").replace("\n", " ")
    path = source.get("path")
    return f"{name} ({path})" if path else name


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)


def text_unified_chunks(sources: list[dict], cancelled: Callable[[], bool],
                        text_rules: list[dict] | None = None, ignore_whitespace=False,
                        ignore_line_endings=False) -> Iterator[tuple[str, int]]:
    if len(sources) < 2:
        raise ValueError("Choose at least two text sources.")
    yield f"# D1ffler text comparison · exported {_stamp()}\n", 0
    yield "# " + comparison_options_summary(ignore_whitespace, ignore_line_endings) + "\n", 0
    yield ("# Patch scope: raw decoded content, including differences ignored by rules. "
           "CR/LF/CRLF use LF transport; final newline is retained. Encoding, BOM and line-ending-only "
           "changes are described below and are not patch operations.\n"), 0
    for rule in text_rules or []:
        yield f"# Active text mask: {rule['name']} ({rule['scope']}: {rule['pattern']})\n", 0
    baseline = _lines(str(sources[0]["text"]))
    for source in sources[1:]:
        _check(cancelled)
        target = _lines(str(source["text"]))
        yield f"# {_label(sources[0])} → {_label(source)}\n", 0
        comparison = compare_text_sources(sources[0], source, ignore_whitespace, ignore_line_endings, text_rules or [])
        yield "# Result: " + comparison["summary"].replace("\n", " ").replace("\r", " ") + "\n", 0
        patch_label = lambda item: str(item.get("scope") or Path(item.get("path") or "").name or item.get("name") or "Source").replace("\n", " ").replace("\r", " ")
        for line in difflib.unified_diff(baseline, target, fromfile=patch_label(sources[0]),
                                         tofile=patch_label(source), lineterm="\n"):
            _check(cancelled)
            if line.endswith("\n"):
                yield line, 1
            else:
                yield line + "\n\\ No newline at end of file\n", 1


_HTML_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>D1ffler comparison export</title><style>
body{font:14px system-ui,sans-serif;margin:24px;color:#17202b}table{border-collapse:collapse;width:100%;margin:16px 0 32px;table-layout:fixed}
th,td{border:1px solid #cbd5e1;padding:5px 8px;vertical-align:top}th{background:#e7edf4;text-align:left}
td.line{width:4em;color:#647184;text-align:right}td.code{font:12px ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
tr.change{background:#fff0dc}tr.add{background:#e5f7e9}tr.remove{background:#fde8e8}
.flag{font-weight:700;padding:5px 9px;background:#fff0dc}.stale{background:#fde8e8}
</style></head><body>"""


def text_html_chunks(sources: list[dict], cancelled: Callable[[], bool],
                     text_rules: list[dict] | None = None, ignore_whitespace=False,
                     ignore_line_endings=False) -> Iterator[tuple[str, int]]:
    if len(sources) < 2:
        raise ValueError("Choose at least two text sources.")
    yield _HTML_HEAD + f"<h1>Text comparison</h1><p>Exported: {html.escape(_stamp())}</p>\n", 0
    yield "<p>" + html.escape(comparison_options_summary(ignore_whitespace, ignore_line_endings)) + "</p>\n", 0
    if text_rules:
        yield "<p>Active text masks: " + "; ".join(
            html.escape(f"{r['name']} ({r['scope']}: {r['pattern']})") for r in text_rules) + "</p>\n", 0
    baseline = str(sources[0]["text"]).replace("\r\n", "\n").replace("\r", "\n").splitlines()
    for source in sources[1:]:
        _check(cancelled)
        target = str(source["text"]).replace("\r\n", "\n").replace("\r", "\n").splitlines()
        comparison = compare_text_sources(sources[0], source, ignore_whitespace, ignore_line_endings, text_rules or [])
        yield '<p class="flag">' + html.escape(comparison["summary"]) + "</p>\n", 0
        left_label, right_label = html.escape(_label(sources[0])), html.escape(_label(source))
        yield f"<h2>{left_label} → {right_label}</h2><table><thead><tr><th colspan=2>{left_label}</th><th colspan=2>{right_label}</th></tr></thead><tbody>\n", 0
        keys = [[line_key(line, source_rules(item, text_rules or []), ignore_whitespace)[0] for line in lines]
                for item, lines in ((sources[0], baseline), (source, target))]
        matcher = difflib.SequenceMatcher(None, *keys, autojunk=False)
        for tag, a0, a1, b0, b1 in matcher.get_opcodes():
            _check(cancelled)
            left = list(enumerate(baseline[a0:a1], a0 + 1))
            right = list(enumerate(target[b0:b1], b0 + 1))
            css = {"equal": "", "replace": "change", "insert": "add", "delete": "remove"}[tag]
            if comparison["kind"] != "content":
                css = ""
            for old, new in zip_longest(left, right):
                _check(cancelled)
                old_number, old_text = old if old is not None else ("", "")
                new_number, new_text = new if new is not None else ("", "")
                yield (f'<tr class="{css}"><td class="line">{old_number}</td>'
                       f'<td class="code">{html.escape(old_text)}</td>'
                       f'<td class="line">{new_number}</td>'
                       f'<td class="code">{html.escape(new_text)}</td></tr>\n'), 1
        yield "</tbody></table>\n", 0
    yield "</body></html>\n", 0


def _csv_row(values: list[object]) -> str:
    stream = io.StringIO(newline="")
    csv.writer(stream).writerow("'" + str(value) if str(value).startswith(("=", "+", "-", "@", "\t", "\r"))
                                else value for value in values)
    return stream.getvalue()


def folder_statuses(statuses: list[str] | None, panel_count: int) -> list[str]:
    if statuses is None:
        return ["pending"] * panel_count
    baseline = "present" if statuses[0] != "removed" else "missing"
    return [baseline] + list(statuses[1:panel_count])


def folder_row_category(statuses: list[str] | None) -> str:
    if statuses is None:
        return "pending"
    if all(value == "identical" for value in statuses):
        return "identical"
    categories = set(statuses[1:])
    if statuses[0] == "error":
        categories.add("error")
    return ", ".join(sorted(categories))


def folder_counts(rows: list[tuple[str, list[str] | None]], moves: list[dict],
                  links: dict | None = None) -> dict[str, int]:
    counts = Counter()
    for _relative, statuses in rows:
        if statuses is None:
            counts["pending"] += 1
            continue
        categories = set(statuses[1:])
        if statuses[0] == "error":
            categories.add("error")
        if all(value == "identical" for value in statuses):
            categories.add("identical")
        else:
            categories.discard("identical")
        for category in categories:
            counts[category] += 1
    counts["moved"] = len(moves)
    counts["linked"] = sum(relative in (links or {}) for relative, _statuses in rows)
    return {name: counts[name] for name in
            ("added", "removed", "modified", "metadata", "ignored", "identical", "error", "pending", "moved", "linked")}


def _folder_metadata(snapshot: dict, rows: list, moves: list) -> list[tuple[str, str]]:
    counts = folder_counts(rows, moves, snapshot.get("links"))
    state = "PARTIAL" if snapshot.get("partial") else "Complete"
    if snapshot.get("stale"):
        state += " · STALE"
    result = [("Exported", _stamp()), ("Comparison state", state),
              ("Scope", str(snapshot.get("scope", "All rows"))),
              ("Files discovered", str(snapshot.get("discovered", 0))),
              ("Files in export", str(len(rows))), ("Move links in export", str(len(moves))),
              ("Excluded paths", str(snapshot.get("excluded", 0)))]
    result.extend((f"{name.title()} paths", str(value)) for name, value in counts.items())
    result.extend((f"Rule: {name}", str(value)) for name, value in snapshot.get("rules", {}).items())
    for index, source in enumerate(snapshot["sources"]):
        result.append((f"Panel {index + 1}", _label(source)))
        if source.get("type"):
            result.append((f"Panel {index + 1} type", str(source["type"])))
    return result


def _move_cells(match: dict, panel_count: int) -> list[str]:
    statuses = ["—"] * panel_count
    statuses[match["from_panel"]] = "moved from"
    statuses[match["to_panel"]] = "moved to"
    return statuses


def _link_cells(snapshot: dict, relative: str, panel_count: int) -> list[str]:
    entries = snapshot.get("links", {}).get(relative, [])
    cells = []
    for entry in (entries + [None] * panel_count)[:panel_count]:
        if not entry:
            cells.append("")
            continue
        target = str(entry["target"])
        if not snapshot.get("include_paths") and (os.path.isabs(target) or ntpath.isabs(target)
                                                   or target.startswith("\\??\\")):
            target = "[absolute target hidden]"
        cells.append(f"{entry['kind']} → {target} ({entry['state']})")
    return cells


def _archive_issue_cells(snapshot: dict, relative: str, panel_count: int) -> list[str]:
    entries = snapshot.get("archive_issues", {}).get(relative, [])
    return (entries + [""] * panel_count)[:panel_count]


def _file_metadata_cells(snapshot: dict, relative: str, panel_count: int) -> list[str]:
    record = snapshot.get("metadata", {}).get(relative, {})
    panels = record.get("panels", [])
    content = record.get("content_statuses", [])
    cells = []
    for index in range(panel_count):
        value = panels[index] if index < len(panels) else {}
        cells.extend((str(value.get("size")) if value.get("size") is not None else "",
                      str(value.get("modified") or value.get("reason") or ""),
                      str(value.get("permissions") or value.get("reason") or ""),
                      str(content[index]) if index < len(content) else ""))
    return cells


def folder_csv_chunks(snapshot: dict, cancelled: Callable[[], bool]) -> Iterator[tuple[str, int]]:
    sources, rows, moves = snapshot["sources"], snapshot["rows"], snapshot["moves"]
    panel_count = len(sources)
    include_metadata = bool(snapshot.get("include_metadata"))
    header = ["Record", "Relative path", "Status", *(_label(source) for source in sources),
              *(f"Link in {_label(source)}" for source in sources),
              *(f"Archive issue in {_label(source)}" for source in sources)]
    if include_metadata:
        header.extend(f"{field} in {_label(source)}" for source in sources
                      for field in ("Size (bytes)", "Modified", "Permissions", "Content result"))
    header.extend(("Move from", "Move to", "SHA-256"))
    yield _csv_row(header), 0
    for key, value in _folder_metadata(snapshot, rows, moves):
        _check(cancelled)
        yield _csv_row(["meta", key, value, *([""] * ((3 + 4 * include_metadata) * panel_count + 3))]), 0
    for relative, statuses in rows:
        _check(cancelled)
        values = folder_statuses(statuses, panel_count)
        category = folder_row_category(statuses)
        yield _csv_row(["file", relative, category, *values,
                        *_link_cells(snapshot, relative, panel_count),
                        *_archive_issue_cells(snapshot, relative, panel_count),
                        *(_file_metadata_cells(snapshot, relative, panel_count) if include_metadata else []),
                        "", "", ""]), 1
    for match in moves:
        _check(cancelled)
        yield _csv_row(["move", f"{match['from_path']} → {match['to_path']}", "moved",
                        *_move_cells(match, panel_count), *([""] * ((2 + 4 * include_metadata) * panel_count)),
                        match["from_path"], match["to_path"],
                        match["sha256"]]), 1


def folder_html_chunks(snapshot: dict, cancelled: Callable[[], bool]) -> Iterator[tuple[str, int]]:
    sources, rows, moves = snapshot["sources"], snapshot["rows"], snapshot["moves"]
    panel_count = len(sources)
    include_metadata = bool(snapshot.get("include_metadata"))
    state = "PARTIAL" if snapshot.get("partial") else "Complete"
    if snapshot.get("stale"):
        state += " · STALE"
    css = "stale" if snapshot.get("stale") else ""
    yield (_HTML_HEAD + f'<h1>Folder comparison</h1><p class="flag {css}">{html.escape(state)}</p>'
           '<h2>Sources, rules, and counts</h2><table><tbody>\n'), 0
    for key, value in _folder_metadata(snapshot, rows, moves):
        _check(cancelled)
        yield f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>\n", 0
    yield "</tbody></table><h2>Paths</h2><table><thead><tr><th>Relative path</th><th>Status</th>", 0
    for source in sources:
        yield f"<th>{html.escape(_label(source))}</th>", 0
    for source in sources:
        yield f"<th>Link in {html.escape(_label(source))}</th>", 0
    for source in sources:
        yield f"<th>Archive issue in {html.escape(_label(source))}</th>", 0
    if include_metadata:
        for source in sources:
            for field in ("Size (bytes)", "Modified", "Permissions", "Content result"):
                yield f"<th>{field} in {html.escape(_label(source))}</th>", 0
    yield "<th>Move link</th></tr></thead><tbody>\n", 0
    for relative, statuses in rows:
        _check(cancelled)
        values = folder_statuses(statuses, panel_count)
        category = folder_row_category(statuses)
        row = f"<tr><td>{html.escape(relative)}</td><td>{html.escape(category)}</td>"
        row += "".join(f"<td>{html.escape(value)}</td>" for value in values)
        row += "".join(f"<td>{html.escape(value)}</td>" for value in _link_cells(snapshot, relative, panel_count))
        row += "".join(f"<td>{html.escape(value)}</td>" for value in _archive_issue_cells(snapshot, relative, panel_count))
        if include_metadata:
            row += "".join(f"<td>{html.escape(value)}</td>" for value in _file_metadata_cells(snapshot, relative, panel_count))
        yield row + "<td></td></tr>\n", 1
    for match in moves:
        _check(cancelled)
        cells = _move_cells(match, panel_count)
        detail = f"{match['from_path']} → {match['to_path']} (SHA-256 {match['sha256']})"
        row = f"<tr class=\"change\"><td>{html.escape(match['from_path'])} → {html.escape(match['to_path'])}</td><td>moved</td>"
        row += "".join(f"<td>{html.escape(value)}</td>" for value in cells)
        row += "<td></td>" * ((2 + 4 * include_metadata) * panel_count)
        yield row + f"<td>{html.escape(detail)}</td></tr>\n", 1
    yield "</tbody></table></body></html>\n", 0


def write_export(path: Path, chunks: Iterable[tuple[str, int]], cancelled: Callable[[], bool],
                 progress: Callable[[int], None] | None = None) -> int:
    """Stream a report into a sibling temporary file, replacing the target only on success."""
    temporary = None
    completed = 0
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=".D1ffler-export-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            for chunk, units in chunks:
                _check(cancelled)
                stream.write(chunk)
                completed += units
                if progress and completed % 100 == 0 and units:
                    progress(completed)
            stream.flush()
            os.fsync(stream.fileno())
        _check(cancelled)
        os.replace(temporary, path)
        temporary = None
        if progress:
            progress(completed)
        return completed
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
