"""Bounded, comparison-only text masks."""

from __future__ import annotations

import fnmatch

import regex


class RuleError(ValueError):
    pass


def validate_rule(rule: dict) -> dict:
    name = str(rule.get("name", "")).strip()
    scope = str(rule.get("scope", "*")).strip() or "*"
    pattern = str(rule.get("pattern", ""))
    mode = str(rule.get("mode", "match"))
    if not name or not pattern or mode not in {"match", "line"}:
        raise RuleError("Give the rule a name, a regular expression, and a match mode.")
    if len(pattern) > 256 or len(scope) > 256:
        raise RuleError("The pattern or filename scope is too long (256 character limit).")
    try:
        compiled = regex.compile(pattern)
    except regex.error as exc:
        raise RuleError(f"Invalid regular expression: {exc}") from exc
    try:
        if compiled.search("", timeout=0.02) is not None:
            raise RuleError("A pattern matching empty text cannot be used.")
    except TimeoutError as exc:
        raise RuleError("The pattern exceeded the 20 ms match limit.") from exc
    return {"name": name, "scope": scope, "pattern": pattern,
            "mode": mode, "enabled": rule.get("enabled") is True}


def applicable_rules(rules: list[dict], path: str) -> list[dict]:
    return [rule for rule in rules if rule.get("enabled") and
            fnmatch.fnmatchcase(path.casefold(), str(rule.get("scope", "*")).casefold())]


def normalize_line(line: str, rules: list[dict]) -> tuple[str, list[tuple[int, int]]]:
    """Return a comparison key and exact original spans masked by rules."""
    if not rules:
        return line, []
    if len(line) > 16384:
        raise RuleError("A line exceeds the 16 KiB text rule limit.")
    spans: list[tuple[int, int, str]] = []
    for index, rule in enumerate(rules):
        if "pattern" not in rule or rule.get("mode") not in {"match", "line"}:
            raise RuleError("An enabled text rule is incomplete or has an invalid mode.")
        try:
            found = list(regex.finditer(str(rule["pattern"]), line, timeout=0.02))
        except TimeoutError as exc:
            raise RuleError(f"Rule '{rule.get('name', 'Unnamed')}' exceeded the 20 ms match limit.") from exc
        except regex.error as exc:
            raise RuleError(f"Invalid rule '{rule.get('name', 'Unnamed')}': {exc}") from exc
        if rule["mode"] == "line" and found:
            return f"\u0000ignored-line:{index}", [(0, len(line))]
        spans.extend((match.start(), match.end(), str(index)) for match in found if match.end() > match.start())
    spans.sort(key=lambda item: (item[0], -item[1]))
    output: list[str] = []
    masked: list[tuple[int, int]] = []
    end = 0
    for start, stop, marker in spans:
        if start < end:
            continue
        output.extend((line[end:start], f"\u0000ignored:{marker}\u0000"))
        masked.append((start, stop))
        end = stop
    output.append(line[end:])
    return "".join(output), masked


def normalize_text(text: str, rules: list[dict]) -> str:
    if len(text) > 8 * 1024 * 1024:
        raise RuleError("Text exceeds the 8 MiB text rule limit.")
    output = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        output.append(normalize_line(body, rules)[0] + line[len(body):])
    return "".join(output)
