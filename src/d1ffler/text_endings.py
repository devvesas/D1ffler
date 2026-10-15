"""Lossless text boundaries shared by logical editor documents and their metadata."""

import re


TEXT_ENDING = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


class TextRepresentationError(ValueError):
    pass


def split_text_endings(text):
    """Use one Qt paragraph per Python text line, retaining the original boundary."""
    endings = TEXT_ENDING.findall(text)
    return TEXT_ENDING.sub("\n", text), endings


def join_text_endings(text, endings):
    lines = text.split("\n")
    if len(endings) != len(lines) - 1 or any(TEXT_ENDING.fullmatch(e) is None for e in endings):
        raise TextRepresentationError(
            "Text and line-ending metadata disagree; save was stopped to preserve the complete buffer."
        )
    return "".join(line + endings[i] for i, line in enumerate(lines[:-1])) + lines[-1]
