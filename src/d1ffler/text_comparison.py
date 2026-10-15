"""Comparison-only text equivalence and format diagnostics shared by all views."""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from d1ffler.text_rules import applicable_rules, normalize_line, normalize_text


def comparison_key(text, rules=(), ignore_whitespace=False, ignore_line_endings=False):
    text = normalize_text(text, rules) if rules else text
    if ignore_line_endings:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if ignore_whitespace:
        text = re.sub(r"\s+", "", text)
    return text


def line_key(line, rules=(), ignore_whitespace=False):
    key, spans = normalize_line(line, rules)
    if ignore_whitespace:
        key = re.sub(r"\s+", "", key)
        spans += [(match.start(), match.end()) for match in re.finditer(r"\s+", line)]
    return key, spans


def source_rules(source, rules):
    scope = source.get("scope")
    if scope is None:
        scope = Path(source.get("path") or "").name
    return applicable_rules(rules, scope)


def _codec(source):
    return codecs.lookup(source.get("encoding") or "utf-8").name


def _bom(source):
    value = source.get("bom", "")
    return value.hex() if isinstance(value, bytes) else value


def _ending_name(ending):
    return {"\r\n": "CRLF", "\n": "LF", "\r": "CR", "": "none"}[ending]


def _endings(text):
    return re.findall(r"\r\n|\n|\r", text)


def _final_ending(text):
    return "\r\n" if text.endswith("\r\n") else text[-1:] if text.endswith(("\r", "\n")) else ""


def compare_text_sources(left, right, ignore_whitespace=False, ignore_line_endings=False, rules=()):
    """Return stable file statuses plus a finer explanation for text and formats.

    Encoding/BOM always count. Endings and final newline follow the ignore rules;
    whitespace includes line breaks, preserving the existing script contract.
    """
    a, b = left["text"], right["text"]
    left_rules, right_rules = source_rules(left, rules), source_rules(right, rules)
    key_a = comparison_key(a, left_rules, ignore_whitespace, ignore_line_endings)
    key_b = comparison_key(b, right_rules, ignore_whitespace, ignore_line_endings)
    encoding_equal = (_codec(left), _bom(left)) == (_codec(right), _bom(right))
    details = []

    def detail(name, before, after, ignored=False):
        if before != after:
            details.append({"name": name, "left": before, "right": after, "ignored": ignored})

    final_a, final_b = _final_ending(a), _final_ending(b)
    detail("Final newline", _ending_name(final_a), _ending_name(final_b),
           ignore_whitespace or (ignore_line_endings and bool(final_a) == bool(final_b)))
    ends_a, ends_b = _endings(a), _endings(b)
    styles = lambda endings: ", ".join(sorted({_ending_name(e) for e in endings})) or "none"
    detail("Line endings", styles(ends_a), styles(ends_b), ignore_whitespace or ignore_line_endings)
    if ends_a != ends_b and styles(ends_a) == styles(ends_b) and a.splitlines() == b.splitlines():
        details.append({"name": "Line-ending sequence", "left": "original sequence", "right": "different sequence",
                        "ignored": ignore_whitespace or ignore_line_endings})
    detail("Encoding", _codec(left), _codec(right))
    detail("BOM", _bom(left) or "none", _bom(right) or "none")
    if a == b and encoding_equal:
        status, kind, label = "identical", "identical", "Identical text and format"
    elif key_a == key_b and encoding_equal:
        status, kind, label = "ignored", "equivalent", "Equivalent under active rules"
    else:
        # Content excludes paragraph terminators, while the comparison key above
        # retains their significance unless an active rule ignores them.
        body_key = lambda text, active: ("" if ignore_whitespace else "\n").join(
            line_key(line, active, ignore_whitespace)[0] for line in text.splitlines())
        content_equal = body_key(a, left_rules) == body_key(b, right_rules)
        status, kind = "modified", "format" if content_equal else "content"
        label = "Format-only differences" if content_equal else "Content differences"
    explanation = "; ".join(f"{d['name']}: {d['left']} → {d['right']}" +
                            (" (ignored)" if d["ignored"] else "") for d in details)
    return {"status": status, "kind": kind, "summary": label + (" · " + explanation if explanation else ""),
            "format_differences": details}


def comparison_options_summary(ignore_whitespace=False, ignore_line_endings=False):
    return (f"Ignore whitespace: {ignore_whitespace}; Ignore line endings: {ignore_line_endings}; "
            "encoding and BOM are compared; final newline is compared unless whitespace is ignored")


def build_text_layout(sources, rules, options, build_rows):
    """Build GUI/review alignment and statuses using the same whole-text result.

    Line alignment remains useful when whitespace changes paragraph boundaries;
    its edits are ignored if the whole buffers are equivalent under active rules.
    """
    lines = [source["text"].splitlines() for source in sources]
    keys = [[line_key(line, source_rules(source, rules), options.get("ignore_whitespace", False))[0]
             for line in panel_lines] for source, panel_lines in zip(sources, lines)]
    rows, initial_statuses, initial_pairs = build_rows(lines, keys)
    if len(sources) == 2:
        comparison = compare_text_sources(*sources, rules=rules, **options)
        if comparison["kind"] != "content":
            initial_statuses = [["ignored" if status != "equal" else "equal" for status in panel]
                                for panel in initial_statuses]
            initial_pairs = [[], []]
        return (rows, initial_statuses, initial_pairs), [comparison]
    statuses = [["equal"] * len(panel_lines) for panel_lines in lines]
    pairs = [[] for _ in lines]
    comparisons = []
    priority = {"equal": 0, "ignored": 1, "modified": 2, "added": 3, "removed": 3}
    for index, source in enumerate(sources[1:], 1):
        result = compare_text_sources(sources[0], source, rules=rules, **options)
        comparisons.append(result)
        _rows, pair_statuses, pair_inline = build_rows([lines[0], lines[index]], [keys[0], keys[index]])
        if result["kind"] != "content":
            pair_statuses = [["ignored" if status != "equal" else "equal" for status in panel]
                             for panel in pair_statuses]
            pair_inline = [[], []]
        for panel_index, values in ((0, pair_statuses[0]), (index, pair_statuses[1])):
            for line, status in enumerate(values):
                if priority[status] > priority[statuses[panel_index][line]]:
                    statuses[panel_index][line] = status
        for _other, base, target in pair_inline[0]:
            pairs[0].append((index, base, target))
            pairs[index].append((index, base, target))
    return (rows, statuses, pairs), comparisons
