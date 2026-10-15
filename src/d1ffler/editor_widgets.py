"""Text editor, diff highlighting, and reusable path row widgets."""

from __future__ import annotations

from pathlib import Path
from PyQt6.QtCore import QEvent, QMimeData, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QPixmap, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QWidget
from d1ffler.app_assets import symbol_asset_path
from d1ffler.editor_history import DisplayDocument, EditorHistory, python_offset, utf16_length
from d1ffler.text_endings import join_text_endings, split_text_endings
from PyQt6.QtWidgets import QPlainTextDocumentLayout

class DiffHighlighter(QSyntaxHighlighter):
    def __init__(self, document, theme: str = "dark") -> None:
        super().__init__(document)
        self.set_theme(theme)

    def set_theme(self, theme: str) -> None:
        if theme == "light":
            self.formats = {
                "add": self._format("#155724", "#e2f3e5"),
                "remove": self._format("#842029", "#fbe3e5"),
                "header": self._format("#174f7b", "#e3f0fc"),
            }
        else:
            self.formats = {
                "add": self._format("#b7f0c2", "#12351c"),
                "remove": self._format("#ffb8b8", "#401717"),
                "header": self._format("#9ecbff", "#15283d"),
            }
        self.rehighlight()

    @staticmethod
    def _format(foreground: str, background: str) -> QTextCharFormat:
        result = QTextCharFormat()
        result.setForeground(QColor(foreground))
        result.setBackground(QColor(background))
        return result

    def highlightBlock(self, text: str) -> None:
        if text.startswith("+") and not text.startswith("+++"):
            self.setFormat(0, len(text), self.formats["add"])
        elif text.startswith("-") and not text.startswith("---"):
            self.setFormat(0, len(text), self.formats["remove"])
        elif text.startswith("@@") or text.startswith("---") or text.startswith("+++"):
            self.setFormat(0, len(text), self.formats["header"])



class LineNumberArea(QWidget):
    """The non-scrolling gutter displayed beside a LineNumberTextEdit."""

    def __init__(self, editor: "LineNumberTextEdit") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_line_number_area(event)



class LineNumberTextEdit(QPlainTextEdit):
    """A plain-text editor with a line-number gutter that tracks scrolling."""

    ALIGNMENT_PLACEHOLDER = "\u2063"
    historyChanged = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.history = EditorHistory(self)
        display = DisplayDocument(self)
        display.setDocumentLayout(QPlainTextDocumentLayout(display))
        self.setDocument(display)
        display.setUndoRedoEnabled(False)
        self._line_number_area = LineNumberArea(self)
        self._gutter_background = QColor("#111821")
        self._gutter_foreground = QColor("#8d98aa")
        self._line_endings: list[str] = []
        self._document_text = ""
        self._display_markers = set()
        self._setting_text = False
        self._typing_edit = False
        self._inserted_endings = None
        self._show_line_endings = False
        self._applied_gutter_width = -1
        self._updating_gutter_width = False
        self.drop_paths_callback = None
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self._line_ending_icons = {
            "\r": QPixmap(str(symbol_asset_path("cr.svg"))),
            "\n": QPixmap(str(symbol_asset_path("lf.svg"))),
        }
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.document().contentsChange.connect(self._track_line_endings)
        self._update_line_number_area_width(0)

    def setPlainText(self, text: str) -> None:
        normalized, self._line_endings = split_text_endings(text)
        self.history.reset(normalized, self._line_endings.copy())
        self._display_markers = set()
        self._setting_text = True
        super().setPlainText(normalized)
        self._setting_text = False
        self._document_text = self.toPlainText()
        self._sync_line_endings()
        self._notify_history()

    def logical_text(self):
        # CR/LF styles remain normalized for comparison operations. Literal
        # Unicode/control separators retain their identities outside Qt.
        endings = ["\n" if e in {"\r\n", "\r", "\n"} else e
                   for e in self.history.state()["endings"]]
        return join_text_endings(self.history.text(), endings)

    def toPlainText(self):
        return self.document().toRawText().replace("\u2029", "\n")

    def logical_text_with_endings(self):
        return join_text_endings(self.history.text(), self.history.state()["endings"])

    def logical_text_slice(self, start, end):
        """Recover original endings for a selection in logical UTF-16 positions."""
        text = self.history.text()
        a, b = python_offset(text, start), python_offset(text, end)
        endings = self.history.state()["endings"]
        ending_start = text[:a].count("\n")
        return join_text_endings(text[a:b], endings[ending_start:ending_start + text[a:b].count("\n")])

    def insertPlainText(self, text):
        if self.isReadOnly():
            return
        normalized, endings = split_text_endings(text)
        self._inserted_endings = endings
        try:
            super().insertPlainText(normalized)
        finally:
            self._inserted_endings = None

    def insertFromMimeData(self, source):
        if source.hasText():
            self.insertPlainText(source.text())

    def createMimeDataFromSelection(self):
        cursor = self.textCursor()
        mime = QMimeData()
        mime.setText(self.logical_text_slice(self._logical_position(cursor.selectionStart()),
                                             self._logical_position(cursor.selectionEnd())))
        return mime

    def _logical_position(self, position):
        block = self.document().findBlock(position)
        lines = self.toPlainText().split("\n")[:block.blockNumber()]
        prefix_parts = []
        offset = 0
        for line in lines:
            marked = {j for j, char in enumerate(line)
                      if char == self.ALIGNMENT_PLACEHOLDER and offset + j in self._display_markers}
            if not (line == self.ALIGNMENT_PLACEHOLDER and marked):
                prefix_parts.append("".join(char for j, char in enumerate(line) if j not in marked) + "\n")
            offset += len(line) + 1
        prefix = "".join(prefix_parts)
        column = position - block.position()
        column -= sum(utf16_length(block.text()[:p - offset]) < column
                      for p in self._display_markers if offset <= p < offset + len(block.text()))
        return min(utf16_length(prefix) + column, self.history.document.characterCount() - 1)

    def _display_position(self, position):
        logical_block = self.history.document.findBlock(position)
        line = logical_block.blockNumber()
        block = self.document().firstBlock()
        while block.isValid():
            if block.text() != self.ALIGNMENT_PLACEHOLDER:
                if line == 0:
                    return block.position() + min(position - logical_block.position(), block.length() - 1)
                line -= 1
            block = block.next()
        return self.document().characterCount() - 1

    def set_alignment(self, rows, lines):
        """Replace presentation only, retaining logical history, caret and selection."""
        marker = self.ALIGNMENT_PLACEHOLDER
        text = "\n".join(marker if row is None else lines[row] for row in rows)
        if self.history.text().endswith("\n"):
            text += "\n"
        if text == self.toPlainText():
            return
        cursor = self.textCursor()
        position, anchor = self._logical_position(cursor.position()), self._logical_position(cursor.anchor())
        self._project(text, rows)
        cursor = self.textCursor()
        cursor.setPosition(self._display_position(anchor))
        cursor.setPosition(self._display_position(position), QTextCursor.MoveMode.KeepAnchor)
        blocked = self.blockSignals(True)
        try:
            self.setTextCursor(cursor)
        finally:
            self.blockSignals(blocked)

    def _project(self, text, rows=None):
        scroll = self.verticalScrollBar().value(), self.horizontalScrollBar().value()
        blocked = self.blockSignals(True)
        self._setting_text = True
        try:
            super().setPlainText(text)
            endings = self.history.state()["endings"]
            if rows is None:
                self._line_endings = endings.copy()
            else:
                self._line_endings = ["\n" if row is None else endings[row] if row < len(endings) else "\n"
                                      for row in rows][:self.blockCount() - 1]
            self._document_text = self.toPlainText()
            self._display_markers = set()
            if rows is not None:
                offset = 0
                for row, line in zip(rows, self._document_text.split("\n")):
                    if row is None:
                        self._display_markers.add(offset)
                    offset += len(line) + 1
            self._sync_line_endings()
            self.verticalScrollBar().setValue(scroll[0])
            self.horizontalScrollBar().setValue(scroll[1])
        finally:
            self._setting_text = False
            self.blockSignals(blocked)
        self._update_line_number_area_width(0)
        self.viewport().update()
        self._line_number_area.update()

    def replace_logical_text(self, text, metadata=None):
        """An unsaved, undoable comparison operation, including its metadata."""
        if self.isReadOnly():
            return
        normalized, endings = split_text_endings(text)
        cursor = self.textCursor()
        position, anchor = self._logical_position(cursor.position()), self._logical_position(cursor.anchor())
        if self.history.edit(normalized, endings, metadata):
            self._project(normalized)
            cursor = self.textCursor()
            cursor.setPosition(min(anchor, self.history.document.characterCount() - 1))
            cursor.setPosition(min(position, self.history.document.characterCount() - 1), QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            self._notify_history()
            self.textChanged.emit()

    def _notify_history(self):
        self.undoAvailable.emit(self.history.document.isUndoAvailable())
        self.redoAvailable.emit(self.history.document.isRedoAvailable())
        self.historyChanged.emit()

    def _travel_history(self, redo):
        if self.isReadOnly():
            return
        document = self.history.document
        if not (document.isRedoAvailable() if redo else document.isUndoAvailable()):
            return
        cursor = QTextCursor(document)
        self.history.break_typing()
        (document.redo if redo else document.undo)(cursor)
        position = cursor.position()
        self._project(self.history.text())
        self._notify_history()
        self.textChanged.emit()
        cursor = self.textCursor()
        cursor.setPosition(self._display_position(position))
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def undo(self):
        self._travel_history(False)

    def redo(self):
        self._travel_history(True)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo()
        elif event.matches(QKeySequence.StandardKey.Redo):
            self.redo()
        elif any(e not in {"\r\n", "\r", "\n"} for e in split_text_endings(event.text())[1]):
            self.insertPlainText(event.text())
            event.accept()
        else:
            self._typing_edit = bool(event.text() and "\n" not in event.text() and "\r" not in event.text()
                                     and not event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier))
            try:
                super().keyPressEvent(event)
            finally:
                self._typing_edit = False

    def mousePressEvent(self, event):
        self.history.break_typing()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        if self.drop_paths_callback is None:
            event.ignore()
            return
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.drop_paths_callback(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Type.DragEnter:
            self.dragEnterEvent(event)
            return event.isAccepted()
        if event.type() == QEvent.Type.Drop:
            self.dropEvent(event)
            return event.isAccepted()
        return super().viewportEvent(event)

    def set_show_line_endings(self, show: bool) -> None:
        self._show_line_endings = show
        self._update_line_number_area_width(0)
        self._line_number_area.update()

    def text_with_line_endings(self) -> str:
        lines = self.toPlainText().split("\n")
        return "".join(
            line + (self._line_endings[index] if index < len(self._line_endings) else "\n")
            for index, line in enumerate(lines[:-1])
        ) + lines[-1]

    def _track_line_endings(self, position: int, chars_removed: int, chars_added: int) -> None:
        if self._setting_text:
            return
        old_text = self._document_text
        new_text = self.toPlainText()
        start = python_offset(old_text, position)
        old_end = python_offset(old_text, position + chars_removed)
        new_start = python_offset(new_text, position)
        new_end = python_offset(new_text, position + chars_added)
        start_line = old_text[:start].count("\n")
        removed_lines = old_text[start:old_end].count("\n")
        added_lines = new_text[new_start:new_end].count("\n")
        added_endings = self._inserted_endings if self._inserted_endings is not None else ["\n"] * added_lines
        self._line_endings[start_line:start_line + removed_lines] = added_endings
        self._document_text = new_text
        self._sync_line_endings()
        # A typed character in a spacer becomes real text, but the synthetic
        # marker itself must never enter the logical buffer or its history.
        delta = new_end - new_start - (old_end - start)
        self._display_markers = {offset if offset < start else offset + delta
                                 for offset in self._display_markers if offset < start or offset >= old_end}
        lines = new_text.split("\n")
        logical_lines, endings = [], []
        offset = 0
        for i, line in enumerate(lines):
            marked = {j for j, char in enumerate(line)
                      if char == self.ALIGNMENT_PLACEHOLDER and offset + j in self._display_markers}
            if not (line == self.ALIGNMENT_PLACEHOLDER and marked):
                logical_lines.append("".join(char for j, char in enumerate(line) if j not in marked))
                if i < len(self._line_endings):
                    endings.append(self._line_endings[i])
            offset += len(line) + 1
        logical = "\n".join(logical_lines)
        endings = endings[:logical.count("\n")]
        if self.history.edit(logical, endings, typing=self._typing_edit):
            if self._typing_edit:
                self.history.typing_position(self._logical_position(position + chars_added))
            self._notify_history()

    def _sync_line_endings(self) -> None:
        expected_count = max(0, self.blockCount() - 1)
        self._line_endings = (self._line_endings + ["\n"] * expected_count)[:expected_count]

    def set_line_number_theme(self, theme: str) -> None:
        colors = {
            "light": ("#edf1f6", "#647184"),
            "midnight": ("#060912", "#8d98aa"),
            "dracula": ("#21222c", "#b8b8c8"),
            "nord": ("#242933", "#a7b0c0"),
            "solarized": ("#00212b", "#93a1a1"),
            "tokyo_night": ("#16161e", "#9aa5ce"),
            "catppuccin_mocha": ("#181825", "#a6adc8"),
            "gruvbox": ("#1d2021", "#a89984"),
            "one_dark": ("#21252b", "#828997"),
            "monokai": ("#1e1f1c", "#a6a69c"),
            "rose_pine": ("#13111c", "#908caa"),
            "everforest": ("#232a2e", "#9da9a0"),
            "ayu_dark": ("#0b0f14", "#8a9199"),
            "material_ocean": ("#090b10", "#717cb4"),
            "github_dark": ("#090c10", "#8b949e"),
        }
        background, foreground = colors.get(theme, ("#111821", "#8d98aa"))
        self._gutter_background = QColor(background)
        self._gutter_foreground = QColor(foreground)
        self._line_number_area.update()

    def line_number_area_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_line_number_area_width(self, _block_count: int) -> None:
        if self._updating_gutter_width:
            return
        self._updating_gutter_width = True
        width = self.line_number_area_width()
        try:
            if width != self._applied_gutter_width:
                self._applied_gutter_width = width
                self.setViewportMargins(width, 0, 0, 0)
        finally:
            self._updating_gutter_width = False

    def _update_line_number_area(self, rect, dy: int) -> None:
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(0, rect.y(), self._line_number_area.width(), rect.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        contents = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(contents.left(), contents.top(), self.line_number_area_width(), contents.height())
        )

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._show_line_endings:
            return

        painter = QPainter(self.viewport())
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        while block.isValid():
            block_rect = self.blockBoundingGeometry(block).translated(self.contentOffset())
            if block_rect.top() > self.viewport().height():
                break
            if block.isVisible() and block_number < len(self._line_endings):
                layout = block.layout()
                if layout and layout.lineCount():
                    line = layout.lineAt(layout.lineCount() - 1)
                    x = int(block_rect.left() + line.x() + line.naturalTextWidth() + 3)
                    icon_height = min(16, int(line.height()))
                    y = int(block_rect.top() + line.y() + (line.height() - icon_height) / 2)
                    for character in self._line_endings[block_number]:
                        icon = self._line_ending_icons[character]
                        if not icon.isNull():
                            scaled_icon = icon.scaledToHeight(icon_height, Qt.TransformationMode.SmoothTransformation)
                            painter.drawPixmap(x, y, scaled_icon)
                            x += scaled_icon.width() + 2
            block = block.next()
            block_number += 1

    def paint_line_number_area(self, event) -> None:
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), self._gutter_background)
        painter.setPen(self._gutter_foreground)

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        width = self._line_number_area.width() - 6

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(0, top, width, self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(block_number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1



class PathRow(QFrame):
    def __init__(self, label: str, on_browse) -> None:
        super().__init__()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(f"Select {label.lower()}…")
        browse = QPushButton("Browse")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(on_browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        caption = QLabel(label)
        caption.setObjectName("pathLabel")
        layout.addWidget(caption)
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)

