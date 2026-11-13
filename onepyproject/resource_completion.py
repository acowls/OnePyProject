"""Autocomplete resource names within a comma-separated assignment list."""
from __future__ import annotations

import csv
import io
import re

from PySide6.QtCore import QEvent, SIGNAL, QTimer, Qt
from PySide6.QtWidgets import QCompleter, QLineEdit


_ALLOC_START = re.compile(r"\s*\[(?=[+-]?(?:\d|\.|$))", re.IGNORECASE)


def _token_bounds(text: str, cursor: int) -> tuple[int, int]:
    """Find the CSV field under the cursor, ignoring commas inside quotes."""
    delimiters = []
    quoted = False
    i = 0
    while i < len(text):
        if text[i] == '"':
            if quoted and i + 1 < len(text) and text[i + 1] == '"':
                i += 2
                continue
            quoted = not quoted
        elif text[i] == "," and not quoted:
            delimiters.append(i)
        i += 1
    start = max((i + 1 for i in delimiters if i < cursor), default=0)
    end = min((i for i in delimiters if i >= cursor), default=len(text))
    return start, end


def _decode_field(field: str) -> str:
    if not field.strip():
        return ""
    try:
        return next(csv.reader([field.strip()], skipinitialspace=True))[0]
    except (csv.Error, StopIteration, IndexError):
        return field.strip().strip('"').replace('""', '"')


def _encode_field(field: str) -> str:
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="").writerow([field])
    return stream.getvalue()


class ResourceCompleter(QCompleter):
    def __init__(self, editor: QLineEdit, names: list[str]):
        super().__init__(sorted(names, key=str.casefold), editor)
        self.editor = editor
        self.names = set(names)
        self.accepting = False
        self.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        self.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.popup().setStyleSheet(
            "QAbstractItemView { color: #24354a; background: white; selection-color: #183753; "
            "selection-background-color: #d5e9fa; }"
        )
        self.popup().installEventFilter(self)
        self.popup().viewport().installEventFilter(self)
        editor.textEdited.connect(lambda _text: QTimer.singleShot(0, self._highlight_first))

    def disable_inline_preview(self):
        # Qt attaches QLineEdit's highlight handler after the editor is shown.
        try:
            if self.receivers(SIGNAL("highlighted(QString)")):
                self.highlighted[str].disconnect()
        except (RuntimeError, TypeError):
            pass

    def _highlight_first(self):
        if self.popup().isVisible() and not self.popup().currentIndex().isValid() and self.completionCount():
            self.popup().setCurrentIndex(self.completionModel().index(0, 0))

    def splitPath(self, path: str) -> list[str]:
        cursor = min(self.editor.cursorPosition(), len(path))
        start, _ = _token_bounds(path, cursor)
        prefix = _decode_field(path[start:cursor])
        # Don't interfere with editing an allocation percentage.
        if _ALLOC_START.search(prefix):
            return ["\0"]
        return [prefix]

    def _replacement(self, index) -> tuple[str, int]:
        selected = index.data()
        if selected is None:
            return self.editor.text(), self.editor.cursorPosition()
        text = self.editor.text()
        start, end = _token_bounds(text, self.editor.cursorPosition())
        raw = text[start:end]
        leading = raw[:len(raw) - len(raw.lstrip())]
        trailing = raw[len(raw.rstrip()):] if raw.strip() else ""
        decoded = _decode_field(raw)
        allocation = ""
        if decoded not in self.names:
            match = _ALLOC_START.search(decoded)
            if match:
                allocation = decoded[match.start():]
        field = _encode_field(str(selected) + allocation)
        return (text[:start] + leading + field + trailing + text[end:], start + len(leading) + len(field))

    def pathFromIndex(self, index) -> str:
        return self._replacement(index)[0]

    def accept_index(self, index) -> bool:
        if not index.isValid():
            return False
        text, cursor = self._replacement(index)
        self.accepting = True
        self.editor.setText(text)
        self.editor.setCursorPosition(cursor)
        self.popup().hide()
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, lambda: setattr(self, "accepting", False))
        return True

    def dismiss(self):
        self.accepting = True
        self.popup().hide()
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, lambda: setattr(self, "accepting", False))

    def accept_current(self) -> bool:
        index = self.popup().currentIndex()
        if not index.isValid() and self.completionCount():
            index = self.completionModel().index(0, 0)
        return self.accept_index(index)

    def move_selection(self, step: int) -> bool:
        count = self.completionCount()
        if not count:
            return False
        current = self.popup().currentIndex()
        row = current.row() if current.isValid() else (0 if step > 0 else count - 1)
        if current.isValid():
            row = max(0, min(count - 1, row + step))
        index = self.completionModel().index(row, 0)
        self.popup().setCurrentIndex(index)
        self.popup().scrollTo(index)
        return True

    def eventFilter(self, watched, event):
        if watched in (self.popup(), self.popup().viewport()) and self.popup().isVisible():
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.dismiss()
                return True
            if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                if self.move_selection(1 if event.key() == Qt.Key.Key_Down else -1):
                    return True
            if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                if self.accept_current():
                    return True
            if (watched is self.popup().viewport() and event.type() == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton):
                if self.accept_index(self.popup().indexAt(event.position().toPoint())):
                    return True
        return super().eventFilter(watched, event)
