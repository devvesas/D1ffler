"""Qt undo history for logical text, independent of the aligned display document.

The display is a projection with spacer paragraphs. Only content edits enter this
document; line endings and merge decisions live in its undoable frame format.
"""

import json
import time

from PyQt6.QtGui import QTextCursor, QTextDocument, QTextFormat
from d1ffler.text_endings import join_text_endings


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def python_offset(text, position):
    return len(text.encode("utf-16-le")[:position * 2].decode("utf-16-le"))


class DisplayDocument(QTextDocument):
    """Expose the editor's logical availability to Python document clients."""

    def isUndoAvailable(self):
        return self.parent().history.document.isUndoAvailable()

    def isRedoAvailable(self):
        return self.parent().history.document.isRedoAvailable()

    def undo(self, *_args):
        self.parent().undo()

    def redo(self, *_args):
        self.parent().redo()


class EditorHistory:
    STATE_PROPERTY = int(QTextFormat.Property.UserProperty) + 39

    def __init__(self, editor):
        self.document = QTextDocument(editor)
        self.reset("", [])

    def reset(self, text, endings, metadata=None):
        join_text_endings(text, endings)
        self._typing_end = None
        self.document.setUndoRedoEnabled(False)
        self.document.setPlainText(text)
        self._set_state(endings, metadata or {})
        self.document.setUndoRedoEnabled(True)

    def state(self):
        value = self.document.rootFrame().frameFormat().property(self.STATE_PROPERTY)
        return json.loads(value)

    def text(self):
        # toPlainText substitutes ordinary spaces for NBSP. Preserve the raw
        # characters and translate Qt's structural paragraph separators to LF.
        # Original boundaries, including literal U+2029, live in the undo state.
        return self.document.toRawText().replace("\u2029", "\n")

    def _set_state(self, endings, metadata):
        value = json.dumps({"endings": endings, "metadata": metadata}, sort_keys=True)
        frame = self.document.rootFrame()
        fmt = frame.frameFormat()
        if fmt.property(self.STATE_PROPERTY) != value:
            fmt.setProperty(self.STATE_PROPERTY, value)
            frame.setFrameFormat(fmt)

    def break_typing(self):
        self._typing_end = None

    def typing_position(self, position):
        if self._typing_end is not None:
            self._typing_end = python_offset(self.text(), position), self._typing_end[1]

    def edit(self, text, endings, metadata=None, typing=False):
        """Record one content/format transaction, using a minimal text replacement."""
        join_text_endings(text, endings)
        old = self.text()
        state = self.state()
        if metadata is None:
            metadata = state["metadata"]
        if old == text and state == {"endings": endings, "metadata": metadata}:
            return False
        start = 0
        limit = min(len(old), len(text))
        while start < limit and old[start] == text[start]:
            start += 1
        end = 0
        while end < limit - start and old[len(old) - end - 1] == text[len(text) - end - 1]:
            end += 1
        cursor = QTextCursor(self.document)
        now = time.monotonic()
        insertion = old != text and len(old) - end == start
        join = (typing and insertion and self._typing_end is not None
                and self._typing_end[0] == start and now - self._typing_end[1] < 2
                and state == {"endings": endings, "metadata": metadata})
        if join:
            cursor.joinPreviousEditBlock()
        else:
            cursor.beginEditBlock()
        if old != text:
            cursor.setPosition(utf16_length(old[:start]))
            cursor.setPosition(utf16_length(old[:len(old) - end]), QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(text[start:len(text) - end])
        self._set_state(endings, metadata)
        cursor.endEditBlock()
        self._typing_end = (len(text) - end, now) if typing and insertion else None
        return True
