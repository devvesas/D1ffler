"""Line-based three-way merge independent of the Qt interface."""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Conflict:
    number: int
    start: int  # zero-based, end-exclusive base range
    end: int
    base: str
    local: str
    incoming: str
    marker: str


@dataclass(frozen=True)
class MergeResult:
    text: str
    automatic_merges: int
    conflicts: tuple[Conflict, ...]


def _edits(base: list[str], changed: list[str]) -> list[Edit]:
    return [
        Edit(start, end, tuple(changed[other_start:other_end]))
        for tag, start, end, other_start, other_end in
        difflib.SequenceMatcher(None, base, changed, autojunk=False).get_opcodes()
        if tag != "equal"
    ]


def _touches(left: Edit, right: Edit) -> bool:
    if left.start == left.end and right.start == right.end:
        return left.start == right.start
    if left.start == left.end:
        return right.start < left.start < right.end
    if right.start == right.end:
        return left.start < right.start < left.end
    return left.start < right.end and right.start < left.end


def _apply(base: list[str], start: int, end: int, edits: list[Edit]) -> list[str]:
    output: list[str] = []
    position = start
    for edit in edits:
        output.extend(base[position:edit.start])
        output.extend(edit.lines)
        position = edit.end
    output.extend(base[position:end])
    return output


def merge_text(base_text: str, local_text: str, incoming_text: str) -> MergeResult:
    base = base_text.splitlines(keepends=True)
    sides = [(_edits(base, local_text.splitlines(keepends=True)), 0),
             (_edits(base, incoming_text.splitlines(keepends=True)), 1)]
    tagged = sorted(((edit, side) for edits, side in sides for edit in edits),
                    key=lambda pair: (pair[0].start, pair[0].end, pair[1]))
    groups: list[list[tuple[Edit, int]]] = []
    for item in tagged:
        touching = [group for group in groups if any(_touches(item[0], other[0]) for other in group)]
        if touching:
            target = touching[0]
            target.append(item)
            for group in touching[1:]:
                target.extend(group)
                groups.remove(group)
        else:
            groups.append([item])
    groups.sort(key=lambda group: min(edit.start for edit, _ in group))

    output: list[str] = []
    conflicts: list[Conflict] = []
    automatic = 0
    position = 0
    for group in groups:
        start = min(edit.start for edit, _ in group)
        end = max(edit.end for edit, _ in group)
        output.extend(base[position:start])
        local_edits = sorted((edit for edit, side in group if side == 0), key=lambda e: (e.start, e.end))
        incoming_edits = sorted((edit for edit, side in group if side == 1), key=lambda e: (e.start, e.end))
        local = _apply(base, start, end, local_edits)
        incoming = _apply(base, start, end, incoming_edits)
        base_part = base[start:end]
        if not local_edits:
            output.extend(incoming)
            automatic += len(incoming_edits)
        elif not incoming_edits:
            output.extend(local)
            automatic += len(local_edits)
        elif local == incoming:
            output.extend(local)
            automatic += 1
        else:
            number = len(conflicts) + 1
            # Markers are deliberately visible in saved text until resolved.
            def marked(lines: list[str]) -> str:
                content = "".join(lines)
                return content + ("\n" if content and not content.endswith(("\n", "\r")) else "")

            marker = (f"<<<<<<< LOCAL (conflict {number})\n" + marked(local) +
                      "||||||| BASE\n" + marked(base_part) +
                      "=======\n" + marked(incoming) +
                      f">>>>>>> INCOMING (conflict {number})\n")
            conflicts.append(Conflict(number, start, end, "".join(base_part),
                                      "".join(local), "".join(incoming), marker))
            output.append(marker)
        position = end
    output.extend(base[position:])
    return MergeResult("".join(output), automatic, tuple(conflicts))
