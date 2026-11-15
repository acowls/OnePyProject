"""Resource-name completion for the assistant's plain-text prompt."""
from __future__ import annotations

import json

from PySide6.QtCore import QEvent, QStringListModel, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QCompleter, QPlainTextEdit

def _qt_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _python_offset(text: str, position: int) -> int:
    return len(text.encode("utf-16-le")[:position * 2].decode("utf-16-le", errors="ignore"))


def _mention(name: str) -> str:
    if all(character.isalnum() or character in "_-" for character in name):
        return "@" + name
    return "@" + json.dumps(name, ensure_ascii=False)


class ResourceMentionEdit(QPlainTextEdit):
    """Complete @mentions without changing the project's resource sheet."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self._inserting = False
        self._dismissed = None
        self._model = QStringListModel(self)
        self.completer = QCompleter(self._model, self)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.popup().setStyleSheet(
            "QAbstractItemView { color: #24354a; background: white; selection-color: #183753; "
            "selection-background-color: #d5e9fa; }")
        self.completer.popup().installEventFilter(self)
        self.completer.popup().viewport().installEventFilter(self)
        self.completer.activated[str].connect(self._insert_mention)
        self.textChanged.connect(self._schedule_refresh)
        self.cursorPositionChanged.connect(self._schedule_refresh)

    def _schedule_refresh(self):
        if not self._inserting:
            QTimer.singleShot(0, self._refresh_suggestions)

    def _context(self):
        cursor = self.textCursor()
        if cursor.hasSelection():
            return None
        block = cursor.block()
        text = block.text()
        position = _python_offset(text, cursor.positionInBlock())
        prefix = text[:position]
        at = prefix.rfind("@")
        if at < 0 or (at and (prefix[at - 1].isalnum() or prefix[at - 1] in "_@")):
            return None
        query = prefix[at + 1:]
        if '"' in query or "\t" in query or len(query) > 200:
            return None
        return block.position() + _qt_length(prefix[:at]), query, text[position:]

    def _resource_names(self):
        plan = self.window.ai_preview.after if self.window.ai_preview is not None else self.window.plan
        names = {resource.name for resource in plan.resources}
        known = {name.strip() for name in names}
        for task in plan.tasks:
            name = task.resource.strip()
            if name and name not in known:
                names.add(name)
                known.add(name)
        return sorted(names, key=str.casefold)

    def _refresh_suggestions(self):
        if self._inserting or not self.hasFocus():
            return
        signature = (self.toPlainText(), self.textCursor().position())
        if signature == self._dismissed:
            return
        context = self._context()
        if context is None:
            self.completer.popup().hide()
            return
        names = self._resource_names()
        query = context[1]
        if not any(name.casefold().startswith(query.casefold()) for name in names):
            self.completer.popup().hide()
            return
        self._model.setStringList(names)
        self.completer.setCompletionPrefix(query)
        rect = self.cursorRect()
        rect.setWidth(max(200, self.completer.popup().sizeHintForColumn(0) + 30))
        self.completer.complete(rect)
        if self.completer.completionCount():
            self.completer.popup().setCurrentIndex(self.completer.completionModel().index(0, 0))

    def _insert_mention(self, name):
        context = self._context()
        if context is None or name not in self._resource_names():
            self.completer.popup().hide()
            return
        start, query, following = context
        cursor = self.textCursor()
        end = cursor.position()
        # When editing in the middle of a name, replace its existing suffix too.
        if name[:len(query)].casefold() == query.casefold():
            suffix = name[len(query):]
            if suffix and following[:len(suffix)].casefold() == suffix.casefold():
                end += _qt_length(suffix)
        self._inserting = True
        try:
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(_mention(name))
            self.setTextCursor(cursor)
            self.completer.popup().hide()
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            self._dismissed = (self.toPlainText(), cursor.position())
        finally:
            self._inserting = False

    def _choose_current(self):
        index = self.completer.popup().currentIndex()
        if not index.isValid() and self.completer.completionCount():
            index = self.completer.completionModel().index(0, 0)
        if index.isValid():
            self._insert_mention(index.data())
            return True
        return False

    def _popup_key(self, event):
        if not self.completer.popup().isVisible() or event.type() != QEvent.Type.KeyPress:
            return False
        if event.key() == Qt.Key.Key_Escape:
            self._dismissed = (self.toPlainText(), self.textCursor().position())
            self.completer.popup().hide()
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            return self._choose_current()
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            popup = self.completer.popup()
            count = self.completer.completionCount()
            if count:
                current = popup.currentIndex()
                row = current.row() if current.isValid() else 0
                row = max(0, min(count - 1, row + (1 if event.key() == Qt.Key.Key_Down else -1)))
                popup.setCurrentIndex(self.completer.completionModel().index(row, 0))
                return True
        return False

    def keyPressEvent(self, event):
        if not self._popup_key(event):
            super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if watched in (self.completer.popup(), self.completer.popup().viewport()) and self._popup_key(event):
            return True
        return super().eventFilter(watched, event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self.completer.popup().hide()
