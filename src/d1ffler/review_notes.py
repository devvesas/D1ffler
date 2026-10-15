"""Comparison-scoped review notes and content-safe report formatting."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from d1ffler.folder_archives import ArchiveIssue, ArchiveMember, is_zip_source, zip_inventory


STATES = ("Open", "Accepted", "Needs follow-up", "Ignored")


def source_key(sources: list[dict]) -> str:
    """Identify source roles without tying an editable branch to its moving HEAD."""
    normalized = []
    for source in sources:
        git = source.get("git") or {}
        identity = {
            "kind": source.get("kind"),
            "path": str(Path(str(source.get("path", ""))).resolve()),
            "git_repo": str(git.get("repo", "")),
            "git_kind": git.get("kind", ""),
            "git_name": git.get("name", ""),
            "git_commit": git.get("commit", "") if git.get("readonly") else "",
        }
        normalized.append(identity)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode("utf-8")).hexdigest()


def _safe_relative(relative: str) -> tuple[str, ...]:
    if "\\" in relative:
        raise ValueError(f"Unsafe relative path: {relative}")
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or any(part in {".", ".."} or ":" in part for part in parts):
        raise ValueError(f"Unsafe relative path: {relative}")
    return parts


def content_fingerprint(
    sources: list[dict], relative: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Hash the selected file's bytes in every panel, including missing markers."""
    parts = _safe_relative(relative) if relative is not None else None
    states = []
    for source in sources:
        if cancelled and cancelled():
            raise InterruptedError()
        path: Path | ArchiveMember | None = Path(str(source["path"]))
        if source["kind"] == "folder":
            if parts is None:
                raise ValueError("Choose a file in the folder comparison.")
            root = path
            if is_zip_source(root):
                entries, _excluded = zip_inventory(root, [], lambda _relative, _patterns: False, cancelled)
                if cancelled and cancelled():
                    raise InterruptedError()
                path = entries.get("/".join(parts))
                if isinstance(path, ArchiveIssue):
                    raise ValueError(f"Archive entry cannot be reviewed: {path.detail}")
            else:
                path = root.joinpath(*parts)
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"Review path escapes its folder: {relative}")
        elif source["kind"] != "file":
            raise ValueError("Review notes require file or folder sources.")
        if path is None or (isinstance(path, Path) and not path.exists()):
            states.append("missing")
            continue
        if isinstance(path, Path) and not path.is_file():
            raise ValueError(f"Review notes require a regular file: {path}")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if cancelled and cancelled():
                    raise InterruptedError()
                digest.update(chunk)
        after = path.stat()
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                  item.st_mtime_ns, item.st_ctime_ns)
        if signature(before) != signature(after):
            raise OSError(f"File changed while preparing review note: {path}")
        states.append(digest.hexdigest())
    return hashlib.sha256(json.dumps(states).encode("ascii")).hexdigest()


def hunk_anchor(aligned_rows: list[list[int | None]], all_lines: list[list[str]],
                hunk: tuple[int, int]) -> str:
    rows = []
    for row in aligned_rows[hunk[0]:hunk[1] + 1]:
        rows.append([all_lines[index][line] if line is not None else None
                     for index, line in enumerate(row)])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def note_is_stale(note: dict, current_key: str, fingerprint: str | None) -> bool:
    return (bool(note.get("hunk_stale") or note.get("buffer_stale")) or note.get("source_key") != current_key or
            fingerprint is None or note.get("fingerprint") != fingerprint)


def report_text(context: dict, notes: list[dict], *, html_format: bool = False,
                excerpts: dict[str, str] | None = None) -> str:
    """Format only the explicitly selected notes and optional excerpts."""
    excerpts = excerpts or {}
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources = context.get("sources", [])
    rules = context.get("rules", {})
    moves = context.get("moves", [])
    counts = dict(context.get("counts", {}))
    counts.update({f"Selected notes: {state}": sum(note.get("state") == state for note in notes)
                   for state in STATES})
    if html_format:
        lines = ["<!doctype html>", "<html><head><meta charset=\"utf-8\"><title>D1ffler review report</title></head><body>",
                 "<h1>D1ffler review report</h1>", f"<p>Exported: {html.escape(timestamp)}</p>"]
        lines.append("<h2>Sources</h2><ul>")
        for source in sources:
            lines.append(f"<li>{html.escape(str(source.get('name', 'Source')))}: "
                         f"{html.escape(str(source.get('path', '')))}</li>")
        lines.append("</ul><h2>Rules and status</h2><ul>")
        for name, value in {**rules, **counts}.items():
            lines.append(f"<li>{html.escape(str(name))}: {html.escape(str(value))}</li>")
        if moves:
            lines.append("</ul><h2>Detected moves</h2><ul>")
            for move in moves:
                description = (f"{move['from_panel']}: {move['from_path']} → "
                               f"{move['to_panel']}: {move['to_path']} (exact SHA-256)")
                lines.append(f"<li>{html.escape(description)}</li>")
        lines.append("</ul><h2>Review notes</h2>")
        for note in notes:
            title = f"{note.get('path', '') or 'File comparison'} · {note.get('state', 'Open')}"
            if note.get("stale"):
                title += " · STALE"
            lines.append(f"<section><h3>{html.escape(title)}</h3><p>{html.escape(str(note.get('text', ''))).replace(chr(10), '<br>')}</p>")
            excerpt = excerpts.get(str(note.get("id", "")))
            if excerpt:
                lines.append(f"<pre>{html.escape(excerpt)}</pre>")
            lines.append("</section>")
        return "\n".join(lines + ["</body></html>", ""])
    def markdown_inline(value: object) -> str:
        return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1",
                      html.escape(str(value).replace("\r", " ").replace("\n", " ")))

    lines = ["# D1ffler review report", "", f"Exported: {timestamp}", "", "## Sources", ""]
    for source in sources:
        lines.append(f"- {markdown_inline(source.get('name', 'Source'))}: {markdown_inline(source.get('path', ''))}")
    lines.extend(["", "## Rules and status", ""])
    for name, value in {**rules, **counts}.items():
        lines.append(f"- {markdown_inline(name)}: {markdown_inline(value)}")
    if moves:
        lines.extend(["", "## Detected moves", ""])
        for move in moves:
            lines.append(f"- {markdown_inline(move['from_panel'])}: {markdown_inline(move['from_path'])} → "
                         f"{markdown_inline(move['to_panel'])}: {markdown_inline(move['to_path'])} (exact SHA-256)")
    lines.extend(["", "## Review notes", ""])
    for note in notes:
        title = f"{note.get('path', '') or 'File comparison'} · {note.get('state', 'Open')}"
        if note.get("stale"):
            title += " · STALE"
        lines.extend([f"### {markdown_inline(title)}", ""])
        lines.extend(f"> {markdown_inline(line)}" for line in str(note.get("text", "")).splitlines() or [""])
        lines.append("")
        excerpt = excerpts.get(str(note.get("id", "")))
        if excerpt:
            fence = "`" * max(3, max((len(match.group()) for match in re.finditer(r"`+", excerpt)), default=0) + 1)
            lines.extend([f"{fence}diff", excerpt.rstrip(), fence, ""])
    return "\n".join(lines)


def unified_excerpt(left: Path, right: Path, base_start: int, max_lines: int = 100) -> str:
    """Return the unified hunk nearest a reviewed baseline line."""
    import difflib

    def lines(path: Path) -> list[str]:
        data = path.read_bytes()
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Excerpt unavailable for files above the editor preview limit.")
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return data.decode(encoding).splitlines(keepends=True)
            except UnicodeDecodeError:
                continue
        raise ValueError("Excerpt unavailable for this encoding.")

    diff = list(difflib.unified_diff(lines(left), lines(right),
                                     fromfile=left.name, tofile=right.name, n=3))
    groups = []
    current = []
    for line in diff[2:]:
        if line.startswith("@@ ") and current:
            groups.append(current)
            current = []
        current.append(line)
    if current:
        groups.append(current)
    if not groups:
        return ""
    def distance(group: list[str]) -> int:
        match = re.match(r"@@ -(\d+)", group[0])
        return abs(int(match.group(1)) - base_start - 1) if match else 10**9
    selected = min(groups, key=distance)
    return "".join(diff[:2] + selected[:max_lines])
