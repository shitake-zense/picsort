from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import (QMimeData, QObject, QPoint, QPointF, QRect, QRectF, QRunnable,
                            QSize, Qt, QThreadPool, QTimer, Signal, QLockFile)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from shiboken6 import isValid
from PySide6.QtGui import (QColor, QColorSpace, QDrag, QFont, QIcon, QImage, QImageReader, QIntValidator,
                          QKeySequence, QPainter, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QStyledItemDelegate, QTabWidget, QVBoxLayout, QWidget)

from core import SUPPORTED, Store, apply_decision, fingerprint, is_within
from photo_tools import PropertyDialog, CropDialog, HelpDialog

VERSION = "0.13.0"
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"


def application_directory():
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent.parent
        return executable.parent
    return Path(__file__).resolve().parent


def default_data_directory(platform=None, home=None):
    # macOS applications are often installed in the shared /Applications folder,
    # which ordinary users cannot write to. Keep each user's work in their library.
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return Path(home or Path.home()) / "Library" / "Application Support" / "PixSort"
    return application_directory() / "PixSortData"


DATA_DIR = Path(os.environ.get("PIXSORT_DATA_DIR") or os.environ.get("KOMA_DATA_DIR") or str(default_data_directory()))


def migrate_previous_work(data_dir, previous_dir):
    """Copy an older session once, using SQLite's consistent backup API."""
    data_dir, previous_dir = Path(data_dir), Path(previous_dir)
    destination = data_dir / "sessions.sqlite3"
    source = previous_dir / "sessions.sqlite3"
    if destination.exists() or not source.is_file():
        return False
    data_dir.mkdir(parents=True, exist_ok=True)
    temp = data_dir / f"sessions-migration-{os.getpid()}.sqlite3"
    temp_settings = data_dir / f"settings-migration-{os.getpid()}.json"
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as original:
            with closing(sqlite3.connect(temp, timeout=30)) as copy:
                original.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise OSError("旧版の作業記録を読み取れませんでした。")
        if (previous_dir / "settings.json").is_file():
            shutil.copy2(previous_dir / "settings.json", temp_settings)
        if destination.exists():
            return False
        if temp_settings.exists():
            temp_settings.replace(data_dir / "settings.json")
        temp.replace(destination)
        return True
    finally:
        temp.unlink(missing_ok=True)
        temp_settings.unlink(missing_ok=True)

def scan_current_folder(root, data_dir, legacy_paths):
    """Read the current folder without traversing links or touching photos."""
    children = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                       and not p.name.startswith(".") and not is_within(p, data_dir)),
                      key=lambda p: p.name.casefold())
    photos = sorted((p for p in root.iterdir() if p.is_file() and not p.is_symlink()
                     and p.suffix.lower() in SUPPORTED), key=lambda p: p.name.casefold())
    paths = [str(p) for p in photos]
    paths.extend(p for p in legacy_paths if p not in paths and Path(p).is_file())
    return {"paths": paths, "children": [str(p) for p in children],
            "legacy_count": sum(Path(p).parent != root for p in paths)}


STYLE = """
QWidget { color: #f0f2f5; font-family: 'Yu Gothic UI', 'Hiragino Sans', 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog { background: #15191d; }
QWidget#sideContent, QFrame#sideActions { background: #1c2227; }
QFrame#sidebar { background: #1c2227; border-right: 1px solid #333c43; }
QFrame#header, QFrame#footer { background: #1c2227; }
QFrame#header { border-bottom: 1px solid #2b3038; }
QFrame#footer { border-top: 1px solid #2b3038; }
QLabel { background: transparent; }
QLabel#muted { color: #adb9c1; }
QLabel#eyebrow { color: #a9b5bd; font-size: 11px; font-weight: 600; }
QLabel#brand { color: #eaf6ed; font-size: 24px; font-weight: 700; letter-spacing: 0px; }
QPushButton { background: #282e37; border: 1px solid #363e49; border-radius: 9px;
              padding: 8px 14px; font-weight: 600; }
QPushButton:hover { background: #343c48; border-color: #69788c; }
QPushButton:pressed { background: #414b59; }
QPushButton:focus { border-color: #a9dfb7; }
QPushButton:disabled { background: #272d32; color: #89959c; border-color: #394148; }
QPushButton#primary { background: #a9dfb7; border-color: #a9dfb7; color: #14271c; }
QPushButton#primary:hover { background: #c6eed0; }
QPushButton#primary:disabled { background: #303c30; color: #7e9674; border-color: #303c30; }
QPushButton#quiet { background: transparent; border: 1px solid transparent; color: #aeb8c6; }
QPushButton#quiet:hover { background: #2c333d; color: #fff; }
QPushButton#category { text-align: left; background: #242a33; border-color: #323b46; padding: 8px 10px; }
QPushButton#category:hover { border-color: #a9dfb7; background: #303c33; }
QPushButton#segment { padding: 7px 12px; background: #242b30; border-color: #39434a; }
QPushButton#segment:checked { background: #3a5042; color: #e9fff0; border-color: #8fbb9b; }
QPushButton:checked { color: #c8f49f; background: #30412d; border-color: #718c58; }
QPushButton#danger { background: #3b242a; border-color: #71404b; color: #ffb9c3; }
QPushButton#danger:hover { background: #583039; }
QPushButton#danger:disabled { color: #8d6970; background: #292126; border-color: #493039; }
QComboBox { background: #232932; border: 1px solid #39424f; border-radius: 6px; padding: 6px 12px; min-height: 20px; }
QComboBox QAbstractItemView { background: #262d37; selection-background-color: #40523b; }
QListWidget { background: #101318; border: none; outline: 0; }
QListWidget::item { border: 1px solid transparent; border-radius: 5px; color: #a7b1c0; }
QListWidget::item:selected { border: 1px solid #a9dfb7; background: #283629; color: #e4f4d9; }
QScrollBar:horizontal { background: #171b21; height: 9px; }
QScrollBar:vertical { background: #171b21; width: 9px; }
QScrollBar::handle { background: #414b59; border-radius: 4px; min-width: 25px; min-height: 25px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollArea { border: 0; background: transparent; }
QLineEdit { background: #232932; color: #f2f5f8; border: 1px solid #697989; border-radius: 5px;
            padding: 6px 8px; selection-background-color: #91c76f; selection-color: #142018; }
QLineEdit:focus { border-color: #a9dfb7; }
QLineEdit#positionEdit { padding: 4px 6px; min-height: 22px; border-radius: 7px; font-weight: 600; }
QLineEdit:disabled { color: #95a1ae; background: #303740; }
QPlainTextEdit { background: #101318; border: 1px solid #343c48; border-radius: 6px; padding: 12px; }
QTabWidget::pane { border: 1px solid #39424f; background: #101318; }
QTabBar::tab { background: #28303a; color: #c4cdd8; border: 1px solid #39424f;
               padding: 7px 12px; min-width: 50px; }
QTabBar::tab:selected { background: #40543b; color: #f2ffe8; border-color: #92bb7a; }
QTabBar::tab:hover { background: #36444b; }
QTabBar QToolButton { background: #303b40; color: #f4f9f4; border: 1px solid #65747b; border-radius: 3px; }
QTabBar QToolButton:hover { background: #46565b; }
QTabBar QToolButton:disabled { background: #272e32; color: #9ca9af; border-color: #414c52; }
QMenu { background: #242b34; color: #edf2f7; border: 1px solid #596675; padding: 5px; }
QMenu::item { background: transparent; color: #edf2f7; padding: 8px 20px; }
QMenu::item:selected { background: #475a43; color: #ffffff; }
QMenu::item:disabled { color: #8793a2; }
QListWidget#folders { background: #11161b; border: 1px solid #39424f; border-radius: 6px; padding: 3px; }
QListWidget#folders::item { padding: 6px 5px; color: #d9e2ea; }
QListWidget#folders::item:selected { background: #354a35; color: #ffffff; border-color: #94c67e; }
QToolTip { background: #303944; color: #eff4f9; border: 1px solid #5b6978; padding: 6px; }
"""


def label(text, name=None):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    return item


def button(text, callback, name=None):
    item = QPushButton(text)
    if name:
        item.setObjectName(name)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    item.clicked.connect(callback)
    return item


class Signals(QObject):
    result = Signal(object)
    progress = Signal(object)


class Task(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = Signals()

    def run(self):
        try:
            self.signals.result.emit(self.function(self.signals.progress.emit))
        except Exception as error:
            self.signals.result.emit({"error": str(error)})


class PositionEdit(QLineEdit):
    """An editable photo number with shortcuts suspended while typing."""
    editing_started = Signal()
    editing_ended = Signal()
    cancel_requested = Signal()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.editing_started.emit()
        self.selectAll()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.editing_ended.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_requested.emit()
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)


class PhotoCanvas(QWidget):
    navigate_requested = Signal(int)
    trash_requested = Signal(str)
    clear_requested = Signal(str)
    activated = Signal(object)
    zoom_requested = Signal(object)
    fullscreen_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumSize(180, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.image = QImage()
        self.path = ""
        self.caption = ""
        self.zoomed = False
        self.zoom_scale = 1.0
        self.actual_requested = False
        self.offset = QPointF()
        self._press = None
        self._dragged = False
        self._repeated = False
        self.repeat_timer = QTimer(self)
        self.repeat_timer.setInterval(120)
        self.repeat_timer.timeout.connect(self.repeat_navigation)
        self.hold_timer = QTimer(self)
        self.hold_timer.setSingleShot(True)
        self.hold_timer.setInterval(400)
        self.hold_timer.timeout.connect(self.start_repeating)
        self.loading = False
        self.active = False
        self.overlay_status = ""
        self.setMouseTracking(True)

    def set_photo(self, path):
        if path != self.path:
            self.image = QImage()
            self.zoomed = False
            self.zoom_scale = 1.0
            self.actual_requested = False
            self.offset = QPointF()
        self.path = path
        self.loading = bool(path)
        self.caption = ""
        self.update()

    def image_rect(self):
        if self.image.isNull():
            return QRectF()
        area = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        fit = min(area.width() / self.image.width(), area.height() / self.image.height())
        width = self.image.width() * fit * self.zoom_scale
        height = self.image.height() * fit * self.zoom_scale
        return QRectF(area.center().x() - width / 2 + self.offset.x(),
                      area.center().y() - height / 2 + self.offset.y(), width, height)

    def fit_image(self):
        self.zoomed = False
        self.zoom_scale = 1.0
        self.actual_requested = False
        self.offset = QPointF()
        self.zoom_requested.emit(self)
        self.update()

    def set_actual(self):
        if self.image.isNull():
            return
        area = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        fit = min(area.width() / self.image.width(), area.height() / self.image.height())
        self.zoom_scale = 1.0 / (self.devicePixelRatioF() * fit)
        self.zoomed = True
        self.offset = QPointF()
        self.actual_requested = False
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0d1014"))
        if not self.image.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not self.zoomed)
            painter.drawImage(self.image_rect(), self.image)
        else:
            painter.setPen(QColor("#9ba7b6"))
            font = painter.font()
            font.setPointSize(16)
            painter.setFont(font)
            text = "読み込み中…" if self.loading else "フォルダを開いて、写真整理をはじめよう。"
            if self.caption:
                text = self.caption
            painter.drawText(self.rect().adjusted(24, 12, -24, -12),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
        if self.overlay_status and self.path:
            # A translucent, non-interactive label keeps the photo controls usable.
            margin, padding = 12, 10
            available = max(0, self.width() - margin * 2)
            if available > padding * 2:
                font = painter.font()
                font.setPointSize(10)
                painter.setFont(font)
                metrics = painter.fontMetrics()
                lines = [Path(self.path).name, self.overlay_status]
                width = min(available, max(metrics.horizontalAdvance(line) for line in lines) + padding * 2)
                line_height = metrics.height()
                panel = QRectF(margin, margin, width, line_height * 2 + padding * 2)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 220))
                painter.drawRoundedRect(panel, 8, 8)
                painter.setPen(QColor("#20282b"))
                for index, line in enumerate(lines):
                    text = metrics.elidedText(line, Qt.TextElideMode.ElideMiddle, int(width - padding * 2))
                    rect = QRectF(panel.left() + padding, panel.top() + padding + index * line_height,
                                  width - padding * 2, line_height)
                    painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        if self.active:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#a9dfb7"), 3))
            painter.drawRect(self.rect().adjusted(2, 2, -3, -3))
        painter.end()

    def enterEvent(self, event):
        self.activated.emit(self)
        super().enterEvent(event)

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self.activated.emit(self)
            self._press = event.position().toPoint()
            self._press_path = self.path
            self._press_button = event.button()
            self._dragged = False
            self._pan_start = QPointF(self.offset)
            self._repeated = False
            if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton) and not event.modifiers():
                self.hold_timer.start()

    def start_repeating(self):
        if self._press is not None and not self._dragged:
            self._repeated = True
            self.repeat_navigation()
            self.repeat_timer.start()

    def repeat_navigation(self):
        if self._press is not None and not self._dragged:
            self.navigate_requested.emit(-1 if self._press_button == Qt.MouseButton.LeftButton else 1)

    def mouseMoveEvent(self, event):
        if self._press is None or not self.path or self._press_button != Qt.MouseButton.LeftButton:
            return
        delta = event.position().toPoint() - self._press
        if delta.manhattanLength() < QApplication.startDragDistance():
            return
        self.hold_timer.stop()
        self.repeat_timer.stop()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and not self._dragged:
            self._dragged = True
            mime = QMimeData()
            mime.setData("application/x-koma-photo", self.path.encode("utf-8"))
            drag = QDrag(self)
            drag.setMimeData(mime)
            if not self.image.isNull():
                pixmap = QPixmap.fromImage(self.image.scaled(160, 110, Qt.AspectRatioMode.KeepAspectRatio,
                                                            Qt.TransformationMode.SmoothTransformation))
                drag.setPixmap(pixmap)
                drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
            drag.exec(Qt.DropAction.MoveAction)
            self._press = None
        elif self.zoomed:
            self._dragged = True
            self.offset = self._pan_start + QPointF(delta)
            self.update()

    def mouseReleaseEvent(self, event):
        self.hold_timer.stop()
        self.repeat_timer.stop()
        if self._press is not None and not self._repeated:
            distance = (event.position().toPoint() - self._press).manhattanLength()
            if (distance < QApplication.startDragDistance() and not self._dragged and self.path
                and self._press_path == self.path):
                if (self._press_button == Qt.MouseButton.LeftButton and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier and
                    not event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self.clear_requested.emit(self.path)
                elif (self._press_button == Qt.MouseButton.LeftButton and
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier and
                    not event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                    self.trash_requested.emit(self.path)
                elif self._press_button == Qt.MouseButton.LeftButton:
                    self.navigate_requested.emit(-1)
                elif self._press_button == Qt.MouseButton.RightButton:
                    self.navigate_requested.emit(1)
                elif self._press_button == Qt.MouseButton.MiddleButton:
                    self.fullscreen_requested.emit()
        self._press = None

    def wheelEvent(self, event):
        if self.image.isNull() or not self.path:
            return
        self.activated.emit(self)
        old = self.image_rect()
        if old.isEmpty():
            return
        position = event.position()
        image_x = (position.x() - old.left()) / old.width()
        image_y = (position.y() - old.top()) / old.height()
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if not delta:
            return
        factor = 1.25 ** (delta / 120.0 if event.angleDelta().y() else delta / 100.0)
        self.zoom_scale = max(1.0, min(12.0, self.zoom_scale * factor))
        self.zoomed = self.zoom_scale > 1.0001
        self.actual_requested = False
        if self.zoomed:
            current = self.image_rect()
            self.offset += QPointF(position.x() - (current.left() + image_x * current.width()),
                                   position.y() - (current.top() + image_y * current.height()))
        else:
            self.offset = QPointF()
        self.zoom_requested.emit(self)
        self.update()
        event.accept()


class TrashDrop(QFrame):
    dropped = Signal(str)
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(58)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)
        self.title = label("⌫   削除候補", None)
        self.title.setStyleSheet("color: #ffc0c8; font-size: 14px; font-weight: 700")
        self.subtitle = label("Shift ＋ 左クリック  /  X")
        self.subtitle.setStyleSheet("color: #b28c95; font-size: 11px; line-height: 1.6")
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.subtitle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        self.highlight(False)

    def highlight(self, active):
        color = "#61323d" if active else "#302127"
        border = "#ffadb9" if active else "#75434f"
        self.setStyleSheet(f"TrashDrop {{ background: {color}; border: 1px dashed {border}; border-radius: 9px; }}")

    def dragEnterEvent(self, event):
        if self.isEnabled() and event.mimeData().hasFormat("application/x-koma-photo"):
            self.highlight(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.highlight(False)

    def dropEvent(self, event):
        self.highlight(False)
        if self.isEnabled() and event.mimeData().hasFormat("application/x-koma-photo"):
            self.dropped.emit(bytes(event.mimeData().data("application/x-koma-photo")).decode("utf-8"))
            event.acceptProposedAction()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class CheckGrid(QListWidget):
    """Click toggles a card; Shift-click checks the inclusive range from the anchor."""
    clear_requested = Signal(str)
    def __init__(self):
        super().__init__()
        self.anchor_path = ""

    def reset_anchor(self):
        self.anchor_path = ""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item:
                path = item.data(Qt.ItemDataRole.UserRole)
                if (event.modifiers() & Qt.KeyboardModifier.ControlModifier and
                    not event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                    self.clear_requested.emit(path)
                    event.accept()
                    return
                row = self.row(item)
                anchor = next((i for i in range(self.count())
                               if self.item(i).data(Qt.ItemDataRole.UserRole) == self.anchor_path), -1)
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and anchor >= 0:
                    for index in range(min(anchor, row), max(anchor, row) + 1):
                        self.item(index).setCheckState(Qt.CheckState.Checked)
                else:
                    item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                                       else Qt.CheckState.Checked)
                    self.anchor_path = path
                self.setCurrentItem(item)
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and self.currentItem():
            item = self.currentItem()
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                               else Qt.CheckState.Checked)
            self.anchor_path = item.data(Qt.ItemDataRole.UserRole)
            event.accept()
        else:
            super().keyPressEvent(event)


class CardDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(3, 3, -3, -3)
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
        painter.setBrush(QColor("#293b2a" if checked else "#20262e"))
        grid = self.parent()
        focused = grid.hasFocus() and grid.currentIndex() == index
        painter.setPen(QPen(QColor("#a9dfb7" if checked else ("#8ab7ef" if focused else "#303945")),
                            2 if checked or focused else 1))
        painter.drawRoundedRect(rect, 8, 8)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        image_rect = rect.adjusted(7, 7, -7, -54)
        if isinstance(icon, QIcon):
            icon.paint(painter, image_rect, Qt.AlignmentFlag.AlignCenter)
        square = QRect(rect.left() + 10, rect.top() + 10, 21, 21)
        painter.setBrush(QColor("#a9dfb7" if checked else "#141a21"))
        painter.setPen(QPen(QColor("#a9dfb7" if checked else "#b5c0cc"), 1.5))
        painter.drawRoundedRect(square, 4, 4)
        if checked:
            painter.setPen(QPen(QColor("#152218"), 2.5))
            painter.drawLine(square.left() + 5, square.top() + 10, square.left() + 9, square.top() + 14)
            painter.drawLine(square.left() + 9, square.top() + 14, square.left() + 16, square.top() + 6)
        painter.setPen(QColor("#e1e7ee"))
        name_rect = QRect(rect.left() + 9, rect.bottom() - 48, rect.width() - 18, 20)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(index.data(), Qt.TextElideMode.ElideMiddle, name_rect.width()))
        status = index.data(Qt.ItemDataRole.UserRole + 2) or ""
        action = index.data(Qt.ItemDataRole.UserRole + 3)
        painter.setPen(QColor({"trash": "#ffb9c3", "file": "#a9dfb7", "hold": "#f0cf8d"}.get(action, "#929daa")))
        status_rect = QRect(rect.left() + 9, rect.bottom() - 27, rect.width() - 18, 18)
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(status, Qt.TextElideMode.ElideRight, status_rect.width()))
        painter.restore()


class ApplyPreviewDialog(QDialog):
    """Review queued actions by destination; clicking toggles candidate cancellation."""
    def __init__(self, window, pending):
        super().__init__(window)
        self.setWindowTitle("整理内容を確認")
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1080, 650)
        self.resize(min(1080, available.width() - 30), min(650, available.height() - 30))
        self.window = window
        self.pending = dict(pending)
        self.cancelled_paths = set()
        self.thumbnail_pending = set()
        self.preview_path = ""
        self.preview_image = QImage()
        self.lists = []
        self.groups = []
        self.items = None
        layout = QVBoxLayout(self)
        self.title = label("")
        self.title.setStyleSheet("font-size: 18px; font-weight: 700; padding: 6px")
        layout.addWidget(self.title)
        mode = window.state.get("mode", "move")
        self.verb = "コピー" if mode == "copy" else "移動"
        message = label("写真をクリックすると候補を解除します。もう一度クリックすると元に戻ります。\n"
                        "選んだ写真を右側に表示します。解除は［戻る］でも保存されます。", "muted")
        message.setWordWrap(True)
        layout.addWidget(message)
        content = QHBoxLayout()
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(200)
        self.tabs.setMaximumWidth(530)
        self.tabs.setUsesScrollButtons(True)
        content.addWidget(self.tabs, 2)
        right = QVBoxLayout()
        self.detail = label("写真を選択してください", "muted")
        self.detail.setWordWrap(True)
        self.detail.setMaximumHeight(85)
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        right.addWidget(self.detail)
        self.preview = QLabel("写真を選択してください")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(150, 120)
        self.preview.setStyleSheet("background: #101318; color: #ccd4df; border: 1px solid #39424f;")
        right.addWidget(self.preview, 1)
        content.addLayout(right, 3)
        layout.addLayout(content, 1)

        groups = [("削除", "OSのごみ箱", [(p, d) for p, d in pending.items() if d.get("action") == "trash"])]
        # Keep a tab for every configured destination, plus destinations retained
        # in older pending decisions after their configuration was removed.
        destinations = {d.get("path", ""): [] for d in window.state.get("destinations", [])}
        for path, decision in pending.items():
            if decision.get("action") == "file":
                destinations.setdefault(decision.get("destination", ""), []).append((path, decision))
        used_names = {}
        for target, entries in destinations.items():
            name = next((d.get("label", "") for d in window.state.get("destinations", []) if d.get("path") == target), "")
            name = name or Path(target).name or target or "行き先未設定"
            used_names[name] = used_names.get(name, 0) + 1
            display = name if used_names[name] == 1 else f"{name} {used_names[name]}"
            groups.append((display, target, entries))
        for group_name, target, entries in groups:
            photo_list = QListWidget()
            photo_list.setViewMode(QListView.ViewMode.IconMode)
            photo_list.setResizeMode(QListView.ResizeMode.Adjust)
            photo_list.setMovement(QListView.Movement.Static)
            photo_list.setWrapping(True)
            photo_list.setIconSize(QSize(115, 72))
            photo_list.setGridSize(QSize(138, 110))
            photo_list.setSpacing(2)
            photo_list.setUniformItemSizes(True)
            photo_list.verticalScrollBar().valueChanged.connect(self.request_preview_thumbnails)
            photo_list.currentItemChanged.connect(self.preview_selected)
            photo_list.itemClicked.connect(self.toggle_candidate)
            for path, decision in entries:
                item = QListWidgetItem(Path(path).name)
                item.setData(Qt.ItemDataRole.UserRole, (path, decision))
                item.setSizeHint(QSize(136, 108))
                item.setToolTip(path + "\n行き先: " + target)
                photo_list.addItem(item)
            if photo_list.count():
                photo_list.blockSignals(True)
                photo_list.setCurrentRow(0)
                photo_list.blockSignals(False)
            self.lists.append(photo_list)
            self.groups.append((group_name, target))
            index = self.tabs.addTab(photo_list, group_name)
            self.tabs.setTabToolTip(index, target)
        self.tabs.currentChanged.connect(self.tab_changed)
        if self.lists and not self.lists[0].count():
            first_nonempty = next((i for i, photo_list in enumerate(self.lists) if photo_list.count()), 0)
            self.tabs.setCurrentIndex(first_nonempty)
        self.tab_changed(self.tabs.currentIndex())

        buttons = QDialogButtonBox()
        self.back_button = buttons.addButton("戻る", QDialogButtonBox.ButtonRole.RejectRole)
        self.back_button.clicked.connect(self.reject)
        self.go_button = buttons.addButton("この内容で実行", QDialogButtonBox.ButtonRole.AcceptRole)
        self.go_button.setObjectName("primary")
        self.go_button.clicked.connect(self.accept)
        layout.addWidget(buttons)
        self.update_counts()
        QTimer.singleShot(0, self.request_preview_thumbnails)

    def update_counts(self):
        remaining = len(self.pending) - len(self.cancelled_paths)
        self.title.setText(f"反映予定 {remaining}枚  /  候補解除 {len(self.cancelled_paths)}枚")
        for index, photo_list in enumerate(self.lists):
            count = sum(photo_list.item(i).data(Qt.ItemDataRole.UserRole)[0] not in self.cancelled_paths
                        for i in range(photo_list.count()))
            self.tabs.setTabText(index, f"{self.groups[index][0]} ({count})")
        self.go_button.setEnabled(remaining > 0)
        self.back_button.setText("解除を保存して戻る" if self.cancelled_paths else "戻る")

    def tab_changed(self, index):
        if index < 0 or index >= len(self.lists):
            return
        self.items = self.lists[index]
        current = self.items.currentItem()
        if current:
            self.preview_selected(current, None)
        else:
            self.preview_path = ""
            self.preview_image = QImage()
            self.preview.setText("このタブに写真はありません。")
            self.detail.setText("写真を選択してください")
        QTimer.singleShot(0, self.request_preview_thumbnails)

    def set_detail(self, path, decision):
        target = "OSのごみ箱" if decision.get("action") == "trash" else decision.get("destination", "")
        status = "候補解除（反映対象外）" if path in self.cancelled_paths else ("削除候補" if decision.get("action") == "trash" else self.verb + "予定")
        self.detail.setText(status + "\n" + path + "\n→ " + target)
        self.detail.setToolTip(path + "\n→ " + target)

    def toggle_candidate(self, item):
        path, decision = item.data(Qt.ItemDataRole.UserRole)
        if path in self.cancelled_paths:
            self.cancelled_paths.remove(path)
            item.setText(Path(path).name)
            item.setBackground(QColor("transparent"))
        else:
            self.cancelled_paths.add(path)
            item.setText("解除済  " + Path(path).name)
            item.setBackground(QColor("#573036"))
        self.set_detail(path, decision)
        self.update_counts()

    def preview_selected(self, item, _previous):
        if not item:
            return
        path, decision = item.data(Qt.ItemDataRole.UserRole)
        self.preview_path = path
        self.set_detail(path, decision)
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > 1600:
            reader.setScaledSize(size.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio))
        self.preview_image = reader.read()
        self.update_preview()

    def request_preview_thumbnails(self, *_):
        if not isValid(self) or not self.isVisible() or not self.items or not isValid(self.items):
            return
        top = max(0, self.items.indexAt(QPoint(8, 8)).row())
        columns = max(1, self.items.viewport().width() // 138)
        rows = self.items.viewport().height() // 110 + 3
        for row in range(max(0, top - columns), min(self.items.count(), top + columns * rows)):
            item = self.items.item(row)
            path, _ = item.data(Qt.ItemDataRole.UserRole)
            cached = self.window.images.get((path, "thumb"))
            if cached is not None and not cached.isNull():
                item.setIcon(QIcon(QPixmap.fromImage(cached)))
                continue
            if not item.icon().isNull() or path in self.thumbnail_pending:
                continue
            self.thumbnail_pending.add(path)
            def read(_progress, source=path):
                reader = QImageReader(source)
                reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid() and max(size.width(), size.height()) > 220:
                    reader.setScaledSize(size.scaled(220, 220, Qt.AspectRatioMode.KeepAspectRatio))
                return {"path": source, "image": reader.read()}
            self.window.submit(read, self.preview_thumbnail_ready, priority=-1)

    def preview_thumbnail_ready(self, result):
        if not isValid(self) or not self.isVisible():
            return
        path, image = result.get("path"), result.get("image")
        self.thumbnail_pending.discard(path)
        if not path or image is None or image.isNull():
            return
        for photo_list in self.lists:
            if not isValid(photo_list):
                continue
            for row in range(photo_list.count()):
                item = photo_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole)[0] == path:
                    item.setIcon(QIcon(QPixmap.fromImage(image)))
                    return

    def update_preview(self):
        if self.preview_image.isNull():
            self.preview.setText("写真を表示できません。ファイルの場所を確認してください。")
        else:
            pixmap = QPixmap.fromImage(self.preview_image)
            self.preview.setPixmap(pixmap.scaled(QSize(max(1, self.preview.width() - 12), max(1, self.preview.height() - 12)),
                                   Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "preview") and not self.preview_image.isNull():
            self.update_preview()
        if hasattr(self, "items"):
            QTimer.singleShot(0, self.request_preview_thumbnails)


class KeyCaptureDialog(QDialog):
    """Capture the actual key press and keep the dialog open on conflicts."""
    def __init__(self, window, slot):
        super().__init__(window)
        self.window = window
        self.slot = slot
        self.value = None
        self.setWindowTitle("振り分けキーを決める")
        self.resize(450, 190)
        layout = QVBoxLayout(self)
        title = label(f"［{window.state['destinations'][slot]['label']}］に使うキーを押してください。")
        title.setWordWrap(True)
        layout.addWidget(title)
        self.message = label("例：A、1、F2、Ctrl+G。Escでキャンセル", "muted")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addStretch()
        layout.addWidget(button("キャンセル", self.reject, "quiet"))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        if event.isAutoRepeat() or event.key() in (Qt.Key.Key_Shift, Qt.Key.Key_Control,
                                                   Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_unknown):
            return
        sequence = QKeySequence(event.keyCombination()).toString(QKeySequence.SequenceFormat.PortableText)
        reason = self.window.shortcut_conflict(sequence, self.slot)
        if reason:
            self.message.setStyleSheet("color: #ffb9c3;")
            self.message.setText(reason + "\n別のキーを押してください。")
        else:
            self.value = sequence
            self.accept()


class Window(QMainWindow):
    def __init__(self, data_dir=DATA_DIR):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.store = Store(self.data_dir)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.jobs = set()
        self.images = OrderedDict()
        self.image_pending = set()
        self.root = ""
        self.state = {}
        self.entries = []
        self.visible = []
        self.position = 0
        self.generation = 0
        self.scanning = False
        self.applying = False
        self.closing = False
        self.undo_stack = []
        self.checked = set()
        self.recovered_previous = False
        self.shortcuts = []
        self.destination_shortcuts = []
        self.key_migrations = []
        self.compare_mode = False
        self.pinned_path = ""
        self.independent_paths = None
        self.active_side = 0
        self.photo_fullscreen = False
        self.app_fullscreen = False
        self.photo_return_fullscreen = False
        self.was_maximized = False
        self.film_items = {}
        self.setWindowTitle(f"ピクソート — 写真整理  {VERSION}")
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1320, 880)
        self.resize(min(1320, available.width()), min(880, available.height()))
        self.setMinimumSize(740, 450)
        if (ASSETS / "koma.ico").exists():
            self.setWindowIcon(QIcon(str(ASSETS / "koma.ico")))
        self.build_ui()
        self.bind_keys()
        self.update_footer()

    def build_ui(self):
        container = QWidget()
        self.setCentralWidget(container)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QFrame()
        self.header_frame = header
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 8, 18, 8)
        brand = label("ピクソート", "brand")
        header_layout.addWidget(brand)
        header_layout.addSpacing(16)
        self.brand_tagline = label("写真を見ながら、迷わず整理。", "muted")
        header_layout.addWidget(self.brand_tagline)
        header_layout.addStretch()
        self.fullscreen_button = button("全画面  F11", self.toggle_app_fullscreen)
        self.refresh_button = button("↻  更新", self.refresh_folder)
        self.refresh_button.setToolTip("現在のフォルダの写真とサブフォルダを更新（F5）")
        self.open_button = button("＋  フォルダを開く", self.choose_root, "primary")
        header_layout.addWidget(self.fullscreen_button)
        header_layout.addWidget(self.refresh_button)
        header_layout.addWidget(self.open_button)
        outer.addWidget(header)
        body = QHBoxLayout()
        body.setSpacing(0)
        sidebar = QFrame()
        self.sidebar_frame = sidebar
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(240)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        side_scroll = QScrollArea()
        self.side_scroll = side_scroll
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_layout.addWidget(side_scroll, 1)
        side_content = QWidget()
        self.side_content = side_content
        side_content.setObjectName("sideContent")
        side_scroll.setWidget(side_content)
        side = QVBoxLayout(side_content)
        side.setContentsMargins(12, 10, 12, 8)
        side.setSpacing(5)
        side.addWidget(label("開いているフォルダ", "eyebrow"))
        self.folder_label = label("写真フォルダ未選択")
        self.folder_label.setStyleSheet("font-size: 17px; font-weight: 700")
        side.addWidget(self.folder_label)
        self.folder_info = label("JPEG / PNG · 開いているフォルダ", "muted")
        self.folder_info.setWordWrap(True)
        self.folder_info.setStyleSheet("font-size: 11px; color: #929daa")
        side.addWidget(self.folder_info)
        self.folder_info.hide()
        self.parent_button = button("↑  親フォルダへ", self.go_parent)
        side.addWidget(self.parent_button)
        side.addWidget(label("フォルダ  /  ダブルクリックで開く", "eyebrow"))
        self.folder_list = QListWidget()
        self.folder_list.setObjectName("folders")
        self.folder_list.setFixedHeight(80)
        self.folder_list.itemActivated.connect(self.enter_folder_item)
        self.folder_list.currentItemChanged.connect(self.select_folder_item)
        self.folder_list.itemClicked.connect(self.select_folder_item)
        side.addWidget(self.folder_list)
        folder_actions = QHBoxLayout()
        folder_actions.setSpacing(5)
        self.enter_button = button("開く", self.enter_selected_folder)
        self.use_folder_button = button("行き先に追加", self.use_selected_folder)
        self.enter_button.setStyleSheet("padding: 7px 5px;")
        self.use_folder_button.setStyleSheet("padding: 7px 5px;")
        self.use_folder_button.setToolTip("選択したフォルダを振り分け先に追加")
        folder_actions.addWidget(self.enter_button)
        folder_actions.addWidget(self.use_folder_button)
        side.addLayout(folder_actions)
        self.create_folder_button = button("＋  フォルダを作成", self.create_folder)
        side.addWidget(self.create_folder_button)
        side.addSpacing(2)
        side.addWidget(label("振り分け先  /  キーは「設定」で変更", "eyebrow"))
        self.dest_box = QVBoxLayout()
        self.dest_box.setSpacing(5)
        side.addLayout(self.dest_box)
        self.add_destination_button = button("＋  移動先フォルダを追加", self.add_destination, "quiet")
        side.addWidget(self.add_destination_button)
        self.parent_destination_button = button("↑  親を振り分け先に追加", self.add_parent_destination, "quiet")
        side.addWidget(self.parent_destination_button)
        side.addStretch()
        actions_frame = QFrame()
        actions_frame.setObjectName("sideActions")
        actions = QVBoxLayout(actions_frame)
        actions.setContentsMargins(12, 5, 12, 6)
        actions.setSpacing(2)
        sidebar_layout.addWidget(actions_frame)
        self.trash_zone = TrashDrop()
        self.trash_zone.dropped.connect(self.trash_dropped)
        self.trash_zone.clicked.connect(lambda: self.mark_current("trash"))
        actions.addWidget(self.trash_zone)
        self.hold_button = button("H   保留にして次へ", lambda: self.mark_current("hold"), "quiet")
        actions.addWidget(self.hold_button)
        utility = QHBoxLayout()
        utility.setSpacing(2)
        utility.addWidget(button("操作ガイド", self.show_help, "quiet"))
        utility.addWidget(button("操作履歴", self.show_history, "quiet"))
        actions.addLayout(utility)
        actions.addWidget(label("●  オフラインで動作中", "eyebrow"))
        # Long captions or destination names must not force the scroll area wider
        # than its viewport, which otherwise clips the entire left edge.
        for child in side_content.findChildren(QWidget):
            if isinstance(child, (QLabel, QPushButton, QListWidget, TrashDrop)):
                child.setSizePolicy(QSizePolicy.Policy.Ignored, child.sizePolicy().verticalPolicy())
        body.addWidget(sidebar)
        content = QVBoxLayout()
        self.content_layout = content
        content.setContentsMargins(10, 8, 10, 6)
        content.setSpacing(6)
        toolbar = QHBoxLayout()
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["すべての写真", "未確認", "削除候補", "振り分け予定", "保留"])
        self.filter_combo.currentIndexChanged.connect(self.rebuild_views)
        toolbar.addWidget(self.filter_combo)
        self.single_button = button("1枚", self.show_single)
        self.compare_button = button("2枚比較", self.show_compare)
        self.grid_button = button("▦  チェック一覧", lambda: self.set_view(True))
        for item in (self.single_button, self.compare_button, self.grid_button):
            item.setObjectName("segment")
            item.setCheckable(True)
            toolbar.addWidget(item)
        self.single_button.setChecked(True)
        self.pin_button = button("左を固定", self.toggle_pin, "quiet")
        self.pin_button.setVisible(False)
        toolbar.addWidget(self.pin_button)
        toolbar.addStretch()
        content.addLayout(toolbar)
        tools_row = QHBoxLayout()
        self.property_button = button("ⓘ  プロパティ", self.show_properties)
        self.crop_button = button("切り抜き", self.show_crop)
        self.property_button.setToolTip("操作中の写真の情報を表示")
        self.crop_button.setToolTip("操作中の写真を別ファイルに切り抜く")
        tools_row.addWidget(self.property_button)
        tools_row.addWidget(self.crop_button)
        tools_row.addStretch()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["確定時：移動", "確定時：コピー"])
        self.mode_combo.currentIndexChanged.connect(self.mode_changed)
        tools_row.addWidget(self.mode_combo)
        self.toolbar_widgets = [self.filter_combo, self.single_button, self.compare_button,
                                self.grid_button, self.pin_button, self.property_button, self.crop_button, self.mode_combo]
        content.addLayout(tools_row)
        info = QHBoxLayout()
        self.file_label = label("フォルダを開いて写真を選びましょう")
        self.file_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.file_label.setStyleSheet("font-size: 16px; font-weight: 600")
        info.addWidget(self.file_label, 1)
        self.position_edit = PositionEdit()
        self.position_edit.setObjectName("positionEdit")
        self.position_edit.setToolTip("写真番号を入力して Enter で移動します（現在の絞り込み内）")
        self.position_edit.setAccessibleName("写真番号")
        self.position_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position_edit.setValidator(QIntValidator(0, 999999999, self.position_edit))
        self.position_edit.returnPressed.connect(self.jump_to_entered_position)
        self.position_edit.editing_started.connect(lambda: self.set_photo_shortcuts_enabled(False))
        self.position_edit.editing_ended.connect(self.finish_position_edit)
        self.position_edit.cancel_requested.connect(self.finish_position_edit)
        self.position_edit.hide()
        info.addWidget(self.position_edit)
        self.counter_label = label("", "muted")
        self.counter_label.setToolTip("写真の番号を入力して Enter で移動できます")
        info.addWidget(self.counter_label)
        self.zoom_button = button("原寸で確認", self.toggle_zoom, "quiet")
        self.zoom_button.setToolTip("写真を原寸で表示してピントを確認（Z）")
        info.addWidget(self.zoom_button)
        self.previous_button = button("← 前の写真", lambda: self.navigate(-1), "quiet")
        self.next_button = button("次の写真 →", lambda: self.navigate(1), "quiet")
        self.first_button = button("↺ 最初の写真へ", self.go_first, "primary")
        self.first_button.hide()
        info.addWidget(self.previous_button)
        info.addWidget(self.next_button)
        info.addWidget(self.first_button)
        self.info_widgets = [self.file_label, self.position_edit, self.counter_label, self.zoom_button,
                             self.previous_button, self.next_button, self.first_button]
        content.addLayout(info)
        self.stack = QStackedWidget()
        self.photo_page = QWidget()
        photo_layout = QHBoxLayout(self.photo_page)
        photo_layout.setContentsMargins(0, 0, 0, 0)
        photo_layout.setSpacing(5)
        self.canvas = PhotoCanvas()
        self.compare_canvas = PhotoCanvas()
        self.pane_wrappers = []
        self.pane_labels = []
        for canvas in (self.canvas, self.compare_canvas):
            pane = QWidget()
            pane_layout = QVBoxLayout(pane)
            pane_layout.setContentsMargins(0, 0, 0, 0)
            pane_layout.setSpacing(3)
            pane_label = label("", "muted")
            pane_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            pane_label.hide()
            pane_layout.addWidget(pane_label)
            pane_layout.addWidget(canvas, 1)
            canvas.navigate_requested.connect(self.navigate)
            canvas.trash_requested.connect(self.mark_path_for_trash)
            canvas.clear_requested.connect(self.clear_clicked_path)
            canvas.activated.connect(self.leave_position_editor)
            canvas.activated.connect(self.activate_canvas)
            canvas.zoom_requested.connect(self.canvas_zoom_changed)
            canvas.fullscreen_requested.connect(self.toggle_photo_fullscreen)
            photo_layout.addWidget(pane, 1)
            self.pane_wrappers.append(pane)
            self.pane_labels.append(pane_label)
        self.pane_wrappers[1].hide()
        self.first_overlay = button("↺ 最初の写真へ", self.go_first, "primary")
        self.first_overlay.setParent(self.photo_page)
        self.first_overlay.setToolTip("現在の絞り込み内の最初へ戻る（Home）")
        self.first_overlay.hide()
        self.stack.addWidget(self.photo_page)
        grid_page = QWidget()
        grid_layout = QVBoxLayout(grid_page)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(10)
        batch = QHBoxLayout()
        self.checked_label = label("0枚チェック", "muted")
        batch.addWidget(self.checked_label)
        batch.addSpacing(16)
        batch.addWidget(label("画像サイズ", "muted"))
        self.grid_size_combo = QComboBox()
        self.grid_size_combo.addItems(["小", "中", "大", "特大"])
        batch.addWidget(self.grid_size_combo)
        batch.addStretch()
        batch.addWidget(button("表示中をすべてチェック", self.check_all, "quiet"))
        batch.addWidget(button("解除", self.uncheck_all, "quiet"))
        grid_layout.addLayout(batch)
        actions = QHBoxLayout()
        actions.addStretch()
        self.batch_clear_trash_button = button("チェックした削除候補を解除", self.clear_checked_trash)
        actions.addWidget(self.batch_clear_trash_button)
        self.batch_trash_button = button("チェックした写真を削除候補に", self.batch_trash, "danger")
        actions.addWidget(self.batch_trash_button)
        grid_layout.addLayout(actions)
        self.grid = CheckGrid()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.grid.setGridSize(QSize(164, 158))
        self.grid.setIconSize(QSize(144, 97))
        self.grid.setSpacing(3)
        self.grid.setUniformItemSizes(True)
        self.grid_size_combo.currentIndexChanged.connect(self.grid_size_changed)
        try:
            settings = json.loads((self.data_dir / "settings.json").read_text(encoding="utf-8"))
            size_index = int(settings.get("grid_size", 1))
        except (OSError, ValueError, TypeError, AttributeError):
            size_index = 1
        self.grid_size_combo.blockSignals(True)
        self.grid_size_combo.setCurrentIndex(size_index if 0 <= size_index < 4 else 1)
        self.grid_size_combo.blockSignals(False)
        self.apply_grid_size()
        self.grid.setItemDelegate(CardDelegate(self.grid))
        self.grid.itemChanged.connect(self.checked_changed)
        self.grid.clear_requested.connect(self.clear_clicked_path)
        self.grid.verticalScrollBar().valueChanged.connect(self.request_thumbnails)
        grid_layout.addWidget(self.grid)
        self.stack.addWidget(grid_page)
        content.addWidget(self.stack, 1)
        self.metadata = label("左クリック：前へ  ·  右クリック：次へ  ·  Shift＋左クリック：削除候補", "muted")
        self.metadata.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.metadata.setStyleSheet("font-size: 11px; color: #929daa")
        content.addWidget(self.metadata)
        self.film = QListWidget()
        self.film.setViewMode(QListView.ViewMode.IconMode)
        self.film.setFlow(QListView.Flow.LeftToRight)
        self.film.setWrapping(False)
        self.film.setMovement(QListView.Movement.Static)
        self.film.setIconSize(QSize(82, 48))
        self.film.setGridSize(QSize(96, 70))
        self.film.setUniformItemSizes(True)
        self.film.setFixedHeight(75)
        self.film.setMaximumWidth(7 * 96)
        self.film.setMinimumWidth(0)
        self.film.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.film.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.film.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.film.itemClicked.connect(self.film_clicked)
        self.film.horizontalScrollBar().valueChanged.connect(self.request_thumbnails)
        film_row = QHBoxLayout()
        film_row.addStretch()
        film_row.addWidget(self.film)
        film_row.addStretch()
        content.addLayout(film_row)
        body.addLayout(content, 1)
        outer.addLayout(body, 1)
        footer = QFrame()
        self.footer_frame = footer
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(14, 7, 14, 7)
        self.status_label = label("フォルダを開いて始めましょう。", "muted")
        self.status_label.setWordWrap(False)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        foot.addWidget(self.status_label, 1)
        self.undo_button = button("取り消す", self.undo, "quiet")
        foot.addWidget(self.undo_button)
        self.apply_button = button("整理を反映", self.confirm_apply, "primary")
        self.apply_button.setMinimumWidth(110)
        foot.addWidget(self.apply_button)
        outer.addWidget(footer)
        self.rebuild_destinations()

    def fixed_bindings(self):
        return [("Right", "次の写真", lambda: self.keyboard_step(1)),
                ("Left", "前の写真", lambda: self.keyboard_step(-1)),
                ("X", "削除候補", lambda: self.mark_current("trash")),
                ("Delete", "削除候補", lambda: self.mark_current("trash")),
                ("H", "保留", lambda: self.mark_current("hold")),
                ("0", "候補の解除", self.clear_current),
                ("Home", "最初の写真へ", self.go_first),
                ("Z", "原寸と全体表示を切り替え", self.toggle_zoom),
                ("Escape", "全画面を終了", self.exit_fullscreen),
                ("F11", "アプリの全画面", self.toggle_app_fullscreen),
                ("F5", "フォルダを更新", self.refresh_folder),
                ("Alt+Enter", "プロパティ", self.show_properties),
                ("Ctrl+Shift+X", "切り抜き", self.show_crop),
                ("Alt+1", "1枚表示", self.show_single),
                ("Alt+2", "2枚比較", self.show_compare),
                ("Alt+3", "チェック一覧", lambda: self.set_view(True)),
                ("Alt+P", "左の写真を固定", self.toggle_pin),
                ("Ctrl+F", "写真の全画面", self.toggle_photo_fullscreen),
                ("Ctrl+O", "フォルダを開く", self.choose_root),
                ("Ctrl+N", "フォルダを作成", self.create_folder),
                ("Alt+Up", "親フォルダへ", self.go_parent),
                ("Alt+Shift+Up", "親フォルダを振り分け先に追加", self.add_parent_destination),
                ("Ctrl+Return", "整理を反映", self.confirm_apply),
                ("Ctrl+A", "表示中をすべてチェック", lambda: self.check_all() if self.stack.currentIndex() == 1 else None),
                ("Ctrl+Shift+A", "チェックを解除", lambda: self.uncheck_all() if self.stack.currentIndex() == 1 else None),
                ("Shift+Left", "片側だけ前へ", lambda: self.navigate_independent(-1)),
                ("Shift+Right", "片側だけ次へ", lambda: self.navigate_independent(1)),
                ("Ctrl+Left", "左の写真を操作", lambda: self.select_side(0)),
                ("Ctrl+Right", "右の写真を操作", lambda: self.select_side(1)),
                ("Ctrl++", "拡大", lambda: self.keyboard_zoom(1.25)),
                ("Ctrl+Shift++", "拡大", lambda: self.keyboard_zoom(1.25)),
                ("Ctrl+=", "拡大", lambda: self.keyboard_zoom(1.25)),
                ("Ctrl+Up", "拡大", lambda: self.keyboard_zoom(1.25)),
                ("Ctrl+Down", "縮小", lambda: self.keyboard_zoom(0.8)),
                ("Ctrl+-", "縮小", lambda: self.keyboard_zoom(0.8)),
                ("Ctrl+Shift+Left", "拡大位置を左へ", lambda: self.keyboard_pan(-1, 0)),
                ("Ctrl+Shift+Right", "拡大位置を右へ", lambda: self.keyboard_pan(1, 0)),
                ("Ctrl+Shift+Up", "拡大位置を上へ", lambda: self.keyboard_pan(0, -1)),
                ("Ctrl+Shift+Down", "拡大位置を下へ", lambda: self.keyboard_pan(0, 1)),
                (QKeySequence.StandardKey.Undo, "取り消し", self.undo)]

    def bind_keys(self):
        bindings = [(sequence, callback) for sequence, _, callback in self.fixed_bindings()]
        for sequence, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setAutoRepeat(QKeySequence(sequence).toString(QKeySequence.SequenceFormat.PortableText)
                                   in {"Left", "Right", "Shift+Left", "Shift+Right", "Ctrl+Shift+Left",
                                       "Ctrl+Shift+Right", "Ctrl+Shift+Up", "Ctrl+Shift+Down"})
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    def shortcut_conflict(self, key, slot=None):
        sequence = QKeySequence(key)
        if sequence.isEmpty() or sequence.count() != 1:
            return "そのキーは設定できません。"
        for reserved, purpose, _ in self.fixed_bindings():
            if sequence.matches(QKeySequence(reserved)) != QKeySequence.SequenceMatch.NoMatch:
                return f"このキーは「{purpose}」で使われています。"
        for reserved in ("Esc", "Tab", "Backtab", "Return", "Enter", "Space"):
            if sequence.matches(QKeySequence(reserved)) != QKeySequence.SequenceMatch.NoMatch:
                return "このキーは画面の操作に使われています。"
        for index, destination in enumerate(self.state.get("destinations", [])):
            if index != slot and destination.get("key") and sequence.matches(QKeySequence(destination["key"])) != QKeySequence.SequenceMatch.NoMatch:
                return f"このキーは「{destination['label']}」で使われています。"
        return ""

    def ensure_destination_keys(self):
        # Preserve operations when a former destination key becomes a built-in shortcut.
        for slot, destination in enumerate(self.state.get("destinations", [])):
            key = QKeySequence(destination.get("key", ""))
            if not key.isEmpty() and any(key.matches(QKeySequence(reserved)) != QKeySequence.SequenceMatch.NoMatch
                                         for reserved, _, _ in self.fixed_bindings()):
                destination["key"] = ""
                self.key_migrations.append(destination.get("label", "移動先"))
            if not destination.get("key"):
                for key in [str(slot + 1)] + [str(i) for i in range(1, 10)]:
                    if not self.shortcut_conflict(key, slot):
                        destination["key"] = key
                        break

    def bind_destination_keys(self):
        for shortcut in self.destination_shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.destination_shortcuts.clear()
        for slot, destination in enumerate(self.state.get("destinations", [])):
            if not destination.get("key"):
                continue
            shortcut = QShortcut(QKeySequence(destination["key"]), self)
            shortcut.setAutoRepeat(False)
            shortcut.setEnabled(not self.position_edit.hasFocus())
            shortcut.activated.connect(lambda n=slot: self.assign(n))
            self.destination_shortcuts.append(shortcut)

    def set_destination_key(self, slot, key):
        reason = self.shortcut_conflict(key, slot)
        if reason:
            return reason
        self.state["destinations"][slot]["key"] = QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)
        self.save()
        self.rebuild_destinations()
        return ""

    def submit(self, function, callback, progress=None, priority=0):
        if self.closing:
            return
        job = Task(function)
        self.jobs.add(job)
        job.signals.result.connect(lambda result, task=job: self.task_result(task, callback, result))
        if progress:
            job.signals.progress.connect(progress)
        self.pool.start(job, priority)

    def task_result(self, job, callback, result):
        try:
            if not self.closing:
                callback(result)
        except Exception as error:
            self.show_error(str(error))
        finally:
            self.jobs.discard(job)

    def save(self):
        if self.root and self.state:
            self.store.save(self.root, self.state)

    def current_path(self):
        return self.visible[self.position] if self.visible and 0 <= self.position < len(self.visible) else ""

    def ready(self):
        return bool(self.root) and not self.scanning and not self.applying

    def choose_root(self):
        folder = QFileDialog.getExistingDirectory(self, "イベントの写真フォルダを選択")
        if folder:
            self.open_folder(folder)

    def open_folder(self, folder, select_path=""):
        if self.applying:
            return
        self.save()
        self.generation += 1
        generation = self.generation
        self.root = str(Path(folder).resolve())
        self.state = self.store.load(self.root)
        if select_path:
            self.filter_combo.blockSignals(True)
            self.filter_combo.setCurrentIndex(0)
            self.filter_combo.blockSignals(False)
        self.key_migrations = []
        self.ensure_destination_keys()
        self.entries = []
        self.visible = []
        self.images.clear()
        self.image_pending.clear()
        self.undo_stack.clear()
        self.checked.clear()
        self.grid.reset_anchor()
        self.pinned_path = ""
        self.independent_paths = None
        self.active_side = 0
        self.scanning = True
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(1 if self.state.get("mode") == "copy" else 0)
        self.mode_combo.blockSignals(False)
        self.folder_label.setText(self.folder_label.fontMetrics().elidedText(Path(self.root).name or self.root, Qt.TextElideMode.ElideMiddle, 180))
        self.folder_label.setToolTip(self.root)
        self.folder_info.setText("JPEG / PNG · 開いているフォルダ")
        self.folder_list.clear()
        self.canvas.set_photo("")
        self.compare_canvas.set_photo("")
        self.canvas.loading = True
        self.canvas.update()
        self.film_items.clear()
        self.film.clear()
        self.grid.clear()
        self.rebuild_destinations()
        self.update_footer("写真フォルダを読み込んでいます…")
        root = Path(self.root)
        # Keep unfinished decisions from recursive scans in older releases visible.
        legacy_paths = [p for p, d in self.state.get("decisions", {}).items()
                        if d.get("action") in ("trash", "file") and Path(p).parent != root]

        def scan(progress):
            return scan_current_folder(root, self.data_dir, legacy_paths)

        def finished(result):
            if generation != self.generation:
                return
            self.scanning = False
            if "error" in result:
                self.update_footer("読み込みに失敗しました。")
                self.show_error(result["error"])
                return
            self.entries = result["paths"]
            if select_path and str(Path(select_path).resolve()) in self.entries:
                self.state["cursor"] = str(Path(select_path).resolve())
                self.position = self.entries.index(self.state["cursor"])
            self.refresh_folder_list(result["children"])
            self.rebuild_destinations()
            self.rebuild_views()
            self.save()
            notice = "旧版の作業記録を引き継ぎました。" if self.recovered_previous else "左クリックで前、右クリックで次へ。"
            self.recovered_previous = False
            legacy = f"  旧版で指定したサブフォルダ内の候補 {result['legacy_count']}枚を含みます。" if result["legacy_count"] else ""
            key_notice = ("  新しい操作キーとの重複を避けるため、移動先「" + "、".join(self.key_migrations) + "」のキーを変更しました。") if self.key_migrations else ""
            self.update_footer(f"{len(self.entries):,}枚を読み込みました。{notice}{legacy}{key_notice}")

        self.submit(scan, finished)
        self.remember_root()

    def refresh_folder(self):
        if not self.ready():
            return
        self.save()
        self.generation += 1
        generation = self.generation
        root = Path(self.root)
        selected_item = self.folder_list.currentItem()
        selected_folder = selected_item.data(Qt.ItemDataRole.UserRole) if selected_item else ""
        legacy_paths = [p for p, d in self.state.get("decisions", {}).items()
                        if d.get("action") in ("trash", "file") and Path(p).parent != root]
        self.scanning = True
        self.images.clear()
        self.image_pending.clear()
        self.canvas.set_photo("")
        self.compare_canvas.set_photo("")
        self.canvas.loading = True
        self.canvas.update()
        self.rebuild_destinations()
        self.update_footer("フォルダを更新しています…")

        def scan(progress):
            return scan_current_folder(root, self.data_dir, legacy_paths)

        def finished(result):
            if generation != self.generation:
                return
            self.scanning = False
            if "error" in result:
                self.rebuild_destinations()
                self.rebuild_views()
                self.update_footer("更新できませんでした。以前の一覧を表示しています。")
                self.show_error(result["error"])
                return
            self.entries = result["paths"]
            existing = set(self.entries)
            self.checked.intersection_update(existing)
            if self.pinned_path and self.pinned_path not in existing:
                self.pinned_path = ""
            if self.independent_paths and any(p not in existing for p in self.independent_paths):
                self.independent_paths = None
            # A moved/trashed file that reappears is a new or restored photo.
            # Copies remain completed because their original intentionally remains.
            completed = self.state.get("completed", {})
            for path in existing.intersection(completed):
                if completed[path].get("action") in ("move", "trash"):
                    completed.pop(path, None)
            self.refresh_folder_list(result["children"])
            if selected_folder:
                for index in range(self.folder_list.count()):
                    item = self.folder_list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == selected_folder:
                        self.folder_list.setCurrentItem(item)
                        break
            self.rebuild_destinations()
            self.rebuild_views()
            self.save()
            missing = sum(d.get("action") in ("trash", "file") and p not in existing
                          for p, d in self.state.get("decisions", {}).items())
            notice = (f"  見つからない候補 {missing}件は記録を保持しています。" if missing else "")
            self.update_footer(f"フォルダを更新しました。写真 {len(self.entries):,}枚。" + notice)

        self.submit(scan, finished)

    def refresh_folder_list(self, paths=None):
        self.folder_list.clear()
        if not self.root:
            return
        try:
            if paths is None:
                paths = [str(p) for p in sorted(Path(self.root).iterdir(), key=lambda p: p.name.casefold())
                         if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
                         and not is_within(p, self.data_dir)]
            for path in paths:
                item = QListWidgetItem("›  " + Path(path).name)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(path)
                self.folder_list.addItem(item)
        except OSError as error:
            self.show_error("フォルダ一覧を読み込めませんでした。\n" + str(error))
        self.select_folder_item()

    def select_folder_item(self, *_):
        selected = self.folder_list.currentItem() is not None
        self.enter_button.setEnabled(self.ready() and selected)
        self.use_folder_button.setEnabled(self.ready() and selected and len(self.state.get("destinations", [])) < 9)

    def enter_folder_item(self, item):
        if self.ready():
            self.open_folder(item.data(Qt.ItemDataRole.UserRole))

    def enter_selected_folder(self):
        item = self.folder_list.currentItem()
        if item:
            self.enter_folder_item(item)

    def go_parent(self):
        if self.ready():
            parent = Path(self.root).parent
            if parent != Path(self.root):
                self.open_folder(str(parent))

    def add_parent_destination(self):
        if self.ready() and Path(self.root).parent != Path(self.root):
            self.register_destination(str(Path(self.root).parent))

    def use_selected_folder(self):
        item = self.folder_list.currentItem()
        if item and self.ready():
            self.register_destination(item.data(Qt.ItemDataRole.UserRole))

    def register_destination(self, folder):
        target = str(Path(folder).resolve())
        if Path(self.root).resolve() == Path(target):
            self.show_error("現在開いているフォルダ自身は振り分け先にできません。")
            return
        if any(Path(d["path"]).resolve() == Path(target) for d in self.state["destinations"]):
            self.update_footer("このフォルダは既に振り分け先に追加されています。")
            return
        if len(self.state["destinations"]) >= 9:
            self.show_error("振り分け先は9件までです。")
            return
        self.state["destinations"].append({"label": Path(target).name, "path": target})
        self.ensure_destination_keys()
        self.save()
        self.rebuild_destinations()
        self.update_footer("振り分け先を追加しました。")

    def create_folder(self):
        if not self.ready():
            return
        name, accepted = QInputDialog.getText(self, "フォルダを作成", "このフォルダの中に作る名前:")
        name = name.strip()
        if not accepted or not name:
            return
        if name in (".", "..") or any(c in name for c in '/\\'):
            self.show_error("フォルダ名だけを入力してください。")
            return
        try:
            (Path(self.root) / name).mkdir()
        except OSError as error:
            self.show_error("フォルダを作成できませんでした。\n" + str(error))
            return
        self.refresh_folder_list()
        for index in range(self.folder_list.count()):
            item = self.folder_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == str(Path(self.root) / name):
                self.folder_list.setCurrentItem(item)
                break
        self.select_folder_item()
        if len(self.state.get("destinations", [])) < 9:
            self.register_destination(str(Path(self.root) / name))
            self.update_footer("フォルダを作成し、振り分け先に追加しました。")
        else:
            self.update_footer("フォルダを作成しました。振り分け先は9件の上限に達しているため追加していません。")

    def remember_root(self):
        settings_path = self.data_dir / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        if self.root:
            settings["last_root"] = self.root
        settings["grid_size"] = self.grid_size_combo.currentIndex()
        temp = settings_path.with_suffix(".tmp")
        temp.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        temp.replace(settings_path)

    def apply_grid_size(self):
        dimensions = [(134, 138, 114, 77), (164, 158, 144, 97),
                      (240, 209, 220, 148), (320, 263, 300, 202)]
        index = self.grid_size_combo.currentIndex()
        width, height, icon_width, icon_height = dimensions[index if 0 <= index < len(dimensions) else 1]
        self.grid.setGridSize(QSize(width, height))
        self.grid.setIconSize(QSize(icon_width, icon_height))
        for row in range(self.grid.count()):
            self.grid.item(row).setSizeHint(QSize(width - 2, height - 2))
        self.grid.doItemsLayout()
        QTimer.singleShot(0, self.request_thumbnails)

    def grid_size_changed(self, *_):
        self.apply_grid_size()
        self.remember_root()

    def rebuild_destinations(self):
        self.ensure_destination_keys()
        while self.dest_box.count():
            item = self.dest_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for slot, destination in enumerate(self.state.get("destinations", [])):
            row = QWidget()
            row.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(3)
            key_name = QKeySequence(destination.get("key", "")).toString(QKeySequence.SequenceFormat.NativeText)
            full_label = f"{key_name}  [{self.destination_marker(destination['path'])}]  {destination['label']}"
            assign = button(full_label, lambda checked=False, n=slot: self.assign(n), "category")
            assign.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            assign.setText(assign.fontMetrics().elidedText(full_label, Qt.TextElideMode.ElideRight, 145))
            assign.setToolTip(destination["label"] + "\n" + destination["path"] + "\nチェック一覧ではチェックした写真をまとめて指定")
            assign.setEnabled(self.ready())
            layout.addWidget(assign, 1)
            edit = button("設定", lambda checked=False, n=slot: self.destination_menu(n))
            edit.setFixedWidth(44)
            edit.setStyleSheet("padding: 5px")
            edit.setEnabled(self.ready())
            layout.addWidget(edit)
            self.dest_box.addWidget(row)
        self.add_destination_button.setEnabled(self.ready() and len(self.state.get("destinations", [])) < 9)
        self.parent_destination_button.setEnabled(self.ready() and Path(self.root).parent != Path(self.root) and len(self.state.get("destinations", [])) < 9)
        self.select_folder_item()
        self.bind_destination_keys()
        if self.visible and not self.photo_fullscreen and self.stack.currentIndex() == 0:
            self.update_film()

    def add_destination(self):
        if not self.ready():
            return
        folder = QFileDialog.getExistingDirectory(self, "移動・コピー先のフォルダを選択（新規作成もできます）", self.root)
        if folder:
            self.register_destination(folder)

    def destination_menu(self, slot):
        destination = self.state["destinations"][slot]
        menu = QMenu(self)
        choose_key = menu.addAction("キーを決める")
        rename = menu.addAction("表示名を変更")
        change = menu.addAction("移動先フォルダを変更")
        remove = menu.addAction("この移動先を一覧から外す")
        action = menu.exec(self.cursor().pos())
        if action == choose_key:
            capture = KeyCaptureDialog(self, slot)
            if capture.exec() == QDialog.DialogCode.Accepted and capture.value:
                self.set_destination_key(slot, capture.value)
            return
        elif action == rename:
            name, accepted = QInputDialog.getText(self, "表示名", "移動先の表示名", text=destination["label"])
            if accepted and name.strip():
                destination["label"] = name.strip()[:30]
        elif action == change:
            folder = QFileDialog.getExistingDirectory(self, "新しい移動先（既に付けた候補の行き先は変わりません）", destination["path"])
            if folder:
                if Path(self.root).resolve() == Path(folder).resolve():
                    self.show_error("現在開いているフォルダ自身は振り分け先にできません。")
                    return
                destination["path"] = str(Path(folder).resolve())
        elif action == remove:
            self.state["destinations"].pop(slot)
        self.save()
        self.rebuild_destinations()
        self.rebuild_views()

    def mode_changed(self):
        if self.state:
            self.state["mode"] = "copy" if self.mode_combo.currentIndex() else "move"
            self.save()
            self.update_footer()

    def set_view(self, grid):
        if grid and self.photo_fullscreen:
            self.exit_photo_fullscreen()
        self.stack.setCurrentIndex(1 if grid else 0)
        self.grid_button.setChecked(grid)
        self.single_button.setChecked(not grid and not self.compare_mode)
        self.compare_button.setChecked(not grid and self.compare_mode)
        self.pin_button.setVisible(not grid and self.compare_mode and not self.photo_fullscreen)
        self.pane_wrappers[1].setVisible(self.compare_mode)
        for pane_label in self.pane_labels:
            pane_label.setVisible(self.compare_mode and not self.photo_fullscreen)
        self.film.setVisible(not grid and not self.photo_fullscreen)
        self.zoom_button.setVisible(not grid and not self.photo_fullscreen)
        self.previous_button.setVisible(not grid and not self.photo_fullscreen)
        self.next_button.setVisible(not grid and not self.photo_fullscreen)
        self.refresh_current()
        QTimer.singleShot(50, self.request_thumbnails)

    def toggle_app_fullscreen(self):
        if self.photo_fullscreen:
            self.exit_photo_fullscreen()
        if self.app_fullscreen:
            self.app_fullscreen = False
            self.fullscreen_button.setText("全画面  F11")
            if self.was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self.was_maximized = self.isMaximized()
            self.app_fullscreen = True
            self.fullscreen_button.setText("全画面を終了  F11")
            self.showFullScreen()
        self.update_active_display()

    def exit_fullscreen(self):
        if self.photo_fullscreen:
            self.exit_photo_fullscreen()
        elif self.app_fullscreen:
            self.toggle_app_fullscreen()

    def toggle_photo_fullscreen(self):
        if self.photo_fullscreen:
            self.exit_photo_fullscreen()
            return
        if not self.ready() or self.stack.currentIndex() != 0 or not self.active_path():
            return
        self.photo_return_fullscreen = self.app_fullscreen
        if not self.app_fullscreen:
            self.was_maximized = self.isMaximized()
        self.photo_fullscreen = True
        for widget in (self.header_frame, self.sidebar_frame, self.footer_frame,
                       self.metadata, self.film, *self.toolbar_widgets, *self.info_widgets,
                       *self.pane_labels):
            widget.hide()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        self.showFullScreen()
        for canvas in (self.canvas, self.compare_canvas):
            if canvas.path and (canvas is self.canvas or self.compare_mode):
                self.request_image(canvas.path, "full" if canvas.zoomed else "preview", priority=1)
        self.prefetch_previews()
        self.update_active_display()
        self.update_first_controls()

    def exit_photo_fullscreen(self):
        if not self.photo_fullscreen:
            return
        self.photo_fullscreen = False
        self.first_overlay.hide()
        self.update_active_display()
        self.content_layout.setContentsMargins(10, 8, 10, 6)
        self.content_layout.setSpacing(6)
        for widget in (self.header_frame, self.sidebar_frame, self.footer_frame, self.metadata,
                       *self.toolbar_widgets, *self.info_widgets):
            widget.show()
        if self.photo_return_fullscreen:
            self.showFullScreen()
        elif self.was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.set_view(False)

    def show_single(self):
        if self.compare_mode and self.active_path() in self.visible:
            self.position = self.visible.index(self.active_path())
        self.compare_mode = False
        self.pinned_path = ""
        self.independent_paths = None
        self.active_side = 0
        self.set_view(False)

    def show_compare(self):
        self.compare_mode = True
        self.independent_paths = None
        self.active_side = 0
        if len(self.visible) > 1 and self.position >= len(self.visible) - 1:
            self.position = len(self.visible) - 2
            self.active_side = 1
        self.set_view(False)

    def toggle_pin(self):
        if not self.ready() or not self.compare_mode or not self.canvas.path:
            return
        if self.independent_paths:
            self.position = self.visible.index(self.canvas.path) if self.canvas.path in self.visible else self.position
            self.independent_paths = None
        if self.pinned_path:
            self.pinned_path = ""
            self.active_side = 0
        else:
            self.pinned_path = self.canvas.path
            if self.position + 1 < len(self.visible):
                self.position += 1
            self.active_side = 1
        self.refresh_current()

    def active_canvas(self):
        return self.compare_canvas if self.compare_mode and self.active_side == 1 else self.canvas

    def active_path(self):
        if self.stack.currentIndex() == 0:
            return self.active_canvas().path
        return self.current_path()

    def activate_canvas(self, canvas):
        if self.stack.currentIndex() != 0 or not self.compare_mode or not canvas.path:
            return
        side = 1 if canvas is self.compare_canvas else 0
        if side != self.active_side:
            self.active_side = side
            if self.independent_paths and canvas.path in self.visible:
                self.position = self.visible.index(canvas.path)
            self.update_active_display()
            self.update_film()

    def update_active_display(self):
        for index, canvas in enumerate((self.canvas, self.compare_canvas)):
            canvas.active = self.compare_mode and self.stack.currentIndex() == 0 and index == self.active_side
            path = canvas.path
            status = self.decision_description(path) if path and (self.photo_fullscreen or self.app_fullscreen) else ""
            canvas.overlay_status = "削除予定" if status == "削除候補" else status
            canvas.update()
            prefix = ("左・固定" if self.pinned_path and index == 0 else "左") if index == 0 else "右"
            self.pane_labels[index].setText(f"{prefix}  {'● 操作対象  ' if canvas.active else ''}" +
                                            (Path(path).name + self.decision_suffix(path) if path else "写真なし"))
            self.pane_labels[index].setToolTip(self.tooltip_for(path) if path else "")
        active = self.active_canvas()
        self.file_label.setText(Path(active.path).name + self.decision_suffix(active.path) if active.path else "写真を選んでください")
        self.file_label.setToolTip(self.tooltip_for(active.path) if active.path else "")
        self.zoom_button.setText("画面に合わせる" if active.zoomed else "原寸で確認")
        self.zoom_button.setToolTip("写真全体が見える表示に戻す（Z）" if active.zoomed
                                    else "写真を原寸で表示してピントを確認（Z）")
        self.pin_button.setText("固定を解除" if self.pinned_path else "左を固定")
        self.update_position_display()

    def filtered(self, path):
        if path in self.state.get("completed", {}):
            return False
        action = self.state.get("decisions", {}).get(path, {}).get("action")
        mode = self.filter_combo.currentIndex()
        return (mode == 0 or (mode == 1 and path not in self.reviewed_lookup)
                or (mode == 2 and action == "trash") or (mode == 3 and action == "file")
                or (mode == 4 and action == "hold"))

    def decision_description(self, path):
        decision = self.state.get("decisions", {}).get(path, {})
        action = decision.get("action")
        if action == "trash":
            return "削除候補"
        if action == "hold":
            return "保留"
        if action == "file":
            target = decision.get("destination", "")
            label = next((d["label"] for d in self.state.get("destinations", [])
                          if d.get("path") == target), Path(target).name or target)
            return "振り分け予定：" + label
        return ""

    def decision_suffix(self, path):
        decision = self.state.get("decisions", {}).get(path, {})
        if decision.get("action") == "trash":
            return "  ⌫"
        if decision.get("action") == "hold":
            return "  H"
        if decision.get("action") == "file":
            return "  → " + self.decision_description(path).removeprefix("振り分け予定：") + "（予定）"
        return ""

    def rebuild_views(self, *_):
        current = self.current_path() or self.state.get("cursor", "")
        self.reviewed_lookup = set(self.state.get("reviewed", []))
        self.visible = [p for p in self.entries if self.filtered(p)]
        self.position = self.visible.index(current) if current in self.visible else min(self.position, max(0, len(self.visible) - 1))
        self.grid.blockSignals(True)
        self.grid.setUpdatesEnabled(False)
        self.grid.clear()
        for path in self.visible:
            name = Path(path).name
            grid_item = QListWidgetItem(name)
            size = self.grid.gridSize()
            grid_item.setSizeHint(QSize(size.width() - 2, size.height() - 2))
            grid_item.setData(Qt.ItemDataRole.UserRole + 2, self.decision_description(path))
            grid_item.setData(Qt.ItemDataRole.UserRole + 3, self.state.get("decisions", {}).get(path, {}).get("action", ""))
            grid_item.setData(Qt.ItemDataRole.UserRole, path)
            grid_item.setFlags(grid_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            grid_item.setCheckState(Qt.CheckState.Checked if path in self.checked else Qt.CheckState.Unchecked)
            grid_item.setToolTip(self.tooltip_for(path))
            self.grid.addItem(grid_item)
        self.grid.setUpdatesEnabled(True)
        self.grid.blockSignals(False)
        self.refresh_current()
        self.update_checked_bar()
        self.update_footer()
        QTimer.singleShot(30, self.request_thumbnails)

    def tooltip_for(self, path):
        decision = self.state.get("decisions", {}).get(path, {})
        extra = {"trash": "削除候補", "hold": "保留", "file": "振り分け先: " + decision.get("destination", "")}.get(decision.get("action"), "")
        return path + ("\n" + extra if extra else "")

    def refresh_item(self, path):
        if path in self.visible:
            row = self.visible.index(path)
            grid_item = self.grid.item(row)
            grid_item.setText(Path(path).name)
            grid_item.setData(Qt.ItemDataRole.UserRole + 2, self.decision_description(path))
            grid_item.setData(Qt.ItemDataRole.UserRole + 3, self.state.get("decisions", {}).get(path, {}).get("action", ""))
            grid_item.setToolTip(self.tooltip_for(path))
            film_item = self.film_item(path)
            if film_item:
                delta = film_item.data(Qt.ItemDataRole.UserRole + 1)
                film_item.setText(self.film_label(delta) + self.film_decision_suffix(path))
                film_item.setToolTip(self.tooltip_for(path))

    def destination_marker(self, target):
        destinations = self.state.get("destinations", [])
        targets = list(dict.fromkeys([d.get("path", "") for d in destinations] +
                         [d.get("destination", "") for d in self.state.get("decisions", {}).values()
                          if d.get("action") == "file"]))
        def display_name(value):
            return next((d.get("label", "") for d in destinations if d.get("path") == value),
                        Path(value).name or value).strip() or "?"
        first = display_name(target)[0]
        matching = [value for value in targets if display_name(value)[0].casefold() == first.casefold()]
        return first + (str(matching.index(target) + 1) if len(matching) > 1 and target in matching else "")

    def film_decision_suffix(self, path):
        decision = self.state.get("decisions", {}).get(path, {})
        action = decision.get("action")
        if action == "trash":
            return "  ⌫"
        if action == "hold":
            return "  H"
        if action == "file":
            return "  [" + self.destination_marker(decision.get("destination", "")) + "]"
        return ""

    def film_item(self, path):
        # Store rows, never QListWidgetItem wrappers: Qt destroys wrappers on clear().
        row = self.film_items.get(path)
        item = self.film.item(row) if row is not None else None
        return item if item and item.data(Qt.ItemDataRole.UserRole) == path else None

    @staticmethod
    def film_label(delta):
        if delta == 0:
            return "● 現在"
        if delta == -1:
            return "← 前"
        if delta == 1:
            return "次 →"
        return f"{abs(delta)}枚前" if delta < 0 else f"{delta}枚後"

    def update_film(self):
        self.film.blockSignals(True)
        self.film_items.clear()
        self.film.clear()
        if self.visible:
            active_path = self.active_path()
            center = self.visible.index(active_path) if active_path in self.visible else self.position
            for delta in range(-3, 4):
                index = center + delta
                path = self.visible[index] if 0 <= index < len(self.visible) else ""
                item = QListWidgetItem(self.film_label(delta) + self.film_decision_suffix(path) if path else "")
                item.setSizeHint(QSize(94, 68))
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setData(Qt.ItemDataRole.UserRole + 1, delta)
                if path:
                    item.setToolTip(self.tooltip_for(path))
                else:
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                self.film.addItem(item)
                if path:
                    self.film_items[path] = self.film.count() - 1
            self.film.setCurrentRow(3)
        self.film.blockSignals(False)
        def center_film():
            item = self.film.currentItem()
            if item:
                self.film.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
        QTimer.singleShot(0, center_film)
        QTimer.singleShot(0, self.request_thumbnails)

    def refresh_current(self):
        path = self.current_path()
        grid = self.stack.currentIndex() == 1
        self.position_edit.setVisible(bool(not grid and self.visible and not self.photo_fullscreen))
        self.counter_label.setText(f"{len(self.visible):,}枚表示" if grid else
                                   (f"/ {len(self.visible):,}" if path else "0枚"))
        self.file_label.setText("チェックで、まとめて整理。" if grid else (Path(path).name if path else "写真を選んでください"))
        if grid:
            self.first_button.hide()
            self.first_overlay.hide()
            self.metadata.setText("クリックでチェック  ·  Shift＋クリックで範囲チェック  ·  振り分けキーでまとめて指定")
            return
        if self.independent_paths and any(p not in self.visible for p in self.independent_paths):
            self.independent_paths = None
        left_path = self.pinned_path if self.compare_mode and self.pinned_path else path
        right_path = ""
        if self.compare_mode:
            if self.independent_paths:
                left_path, right_path = self.independent_paths
            elif self.pinned_path:
                right_path = path if path != self.pinned_path else (self.visible[self.position + 1] if self.position + 1 < len(self.visible) else "")
            elif self.position + 1 < len(self.visible):
                right_path = self.visible[self.position + 1]
        for canvas, display_path in ((self.canvas, left_path), (self.compare_canvas, right_path)):
            if canvas.path != display_path:
                canvas.set_photo(display_path)
            if display_path:
                # Cached screen-sized preview displays immediately. Full resolution is only
                # needed for zoom, and never has to block normal fullscreen navigation.
                self.request_image(display_path, "preview", priority=1)
                if canvas.zoomed:
                    self.request_image(display_path, "full", priority=1)
            else:
                canvas.loading = False
                canvas.caption = "比較する写真がありません。" if self.compare_mode else ""
                canvas.update()
        self.update_active_display()
        self.update_position_display()
        self.update_first_controls()
        if path:
            self.state["cursor"] = path
            if not self.photo_fullscreen:
                self.update_film()
            self.metadata.setText("左クリック：前  ·  右クリック：次  ·  Shift＋左クリック：削除候補  ·  Ctrl＋左クリック：候補解除  ·  ホイール：拡大 / 押込：全画面")
            self.prefetch_previews()
        else:
            self.canvas.loading = False
            self.canvas.caption = "この条件に合う写真はありません。" if self.entries else "このフォルダに写真はありません。左のフォルダ一覧から中に入れます。"
            self.canvas.update()
            self.metadata.setText("JPEG・PNG対応 / 作業状況は自動保存されます")
            self.update_film()
        self.save()

    def leave_position_editor(self, *_):
        if self.position_edit.hasFocus():
            self.position_edit.clearFocus()

    def set_photo_shortcuts_enabled(self, enabled):
        for shortcut in self.shortcuts + self.destination_shortcuts:
            if isValid(shortcut):
                shortcut.setEnabled(enabled)

    def update_position_display(self, force=False):
        if not hasattr(self, "position_edit"):
            return
        if self.stack.currentIndex() != 0 or not self.visible:
            if force:
                self.position_edit.clear()
            return
        active = self.active_path()
        index = self.visible.index(active) if active in self.visible else self.position
        if force or not self.position_edit.hasFocus():
            self.position_edit.setText(str(index + 1))
        width = self.position_edit.fontMetrics().horizontalAdvance(str(len(self.visible))) + 24
        self.position_edit.setFixedWidth(max(46, min(110, width)))

    def finish_position_edit(self):
        self.set_photo_shortcuts_enabled(True)
        self.position_edit.setStyleSheet("")
        self.update_position_display(force=True)

    def jump_to_entered_position(self):
        if not self.ready() or self.stack.currentIndex() != 0 or not self.visible:
            return
        text = self.position_edit.text().strip()
        if not text.isdigit() or not 1 <= int(text) <= len(self.visible):
            self.position_edit.setStyleSheet("border: 1px solid #ffb9c3;")
            self.position_edit.selectAll()
            self.update_footer(f"写真番号は 1 から {len(self.visible):,} の範囲で入力してください。")
            return
        index = int(text) - 1
        target = self.visible[index]
        if self.compare_mode and self.canvas.path and self.compare_canvas.path:
            # Change the selected side only and preserve the existing pin when possible.
            if self.pinned_path and self.active_side == 1 and target != self.pinned_path:
                self.independent_paths = None
                self.position = index
            elif self.pinned_path and self.active_side == 0 and target != self.compare_canvas.path:
                self.pinned_path = target
                self.independent_paths = None
                self.position = self.visible.index(self.compare_canvas.path)
            else:
                self.independent_paths = [self.canvas.path, self.compare_canvas.path]
                self.independent_paths[self.active_side] = target
                self.pinned_path = ""
                self.position = index
        else:
            self.position = index
        self.refresh_current()
        self.position_edit.clearFocus()
        self.finish_position_edit()
        self.update_footer(f"{index + 1:,}枚目に移動しました。")

    def update_first_controls(self):
        if not hasattr(self, "first_overlay"):
            return
        at_end = (self.stack.currentIndex() == 0 and len(self.visible) > 1 and
                  self.visible[-1] in (self.canvas.path, self.compare_canvas.path if self.compare_mode else ""))
        self.first_button.setVisible(bool(at_end and not self.photo_fullscreen))
        self.first_overlay.setVisible(bool(at_end and self.photo_fullscreen))
        if at_end and self.photo_fullscreen:
            self.position_first_overlay()

    def position_first_overlay(self):
        if not hasattr(self, "first_overlay"):
            return
        self.first_overlay.adjustSize()
        self.first_overlay.move(max(8, self.photo_page.width() - self.first_overlay.width() - 16),
                                max(8, self.photo_page.height() - self.first_overlay.height() - 16))
        self.first_overlay.raise_()

    def prefetch_previews(self):
        if not self.visible or self.stack.currentIndex() != 0:
            return
        # Forward navigation is most common; comparison already displays the next photo.
        offsets = (2, -1, 3, 1) if self.compare_mode and not self.pinned_path else (1, -1, 2, 3)
        for offset in offsets:
            position = self.position + offset
            if 0 <= position < len(self.visible):
                self.request_image(self.visible[position], "preview", priority=-1)

    def go_first(self):
        if not self.ready() or not self.visible:
            return
        if self.stack.currentIndex() == 1:
            self.grid.setCurrentRow(0)
            self.grid.scrollToItem(self.grid.item(0))
            return
        self.position = 0
        self.pinned_path = ""
        self.independent_paths = None
        self.active_side = 0
        self.refresh_current()
        self.update_footer("最初の写真に戻りました。")

    def keyboard_step(self, direction):
        if not self.ready():
            return
        if self.stack.currentIndex() == 1:
            if self.grid.count():
                row = self.grid.currentRow()
                row = 0 if row < 0 else max(0, min(row + direction, self.grid.count() - 1))
                self.grid.setCurrentRow(row)
                self.grid.scrollToItem(self.grid.item(row))
                self.grid.setFocus()
        else:
            self.navigate(direction)

    def select_side(self, side):
        if self.compare_mode and self.stack.currentIndex() == 0:
            canvas = self.canvas if side == 0 else self.compare_canvas
            if canvas.path:
                self.active_side = side
                if self.independent_paths and canvas.path in self.visible:
                    self.position = self.visible.index(canvas.path)
                self.update_active_display()
                if not self.photo_fullscreen:
                    self.update_film()

    def navigate_independent(self, direction):
        if not self.ready() or not self.compare_mode or self.stack.currentIndex() != 0:
            return
        if not self.canvas.path or not self.compare_canvas.path:
            return
        if not self.independent_paths:
            self.independent_paths = [self.canvas.path, self.compare_canvas.path]
            self.pinned_path = ""
        path = self.independent_paths[self.active_side]
        if direction > 0 and path not in self.state.setdefault("reviewed", []):
            self.state["reviewed"].append(path)
        if path not in self.visible:
            self.independent_paths = None
            return
        index = max(0, min(self.visible.index(path) + direction, len(self.visible) - 1))
        self.independent_paths[self.active_side] = self.visible[index]
        self.position = index
        if self.filter_combo.currentIndex() == 1 and direction > 0:
            self.rebuild_views()
        else:
            self.refresh_current()

    def keyboard_zoom(self, factor):
        if not self.ready() or self.stack.currentIndex() != 0 or not self.active_path():
            return
        canvas = self.active_canvas()
        canvas.zoom_scale = max(1.0, min(12.0, canvas.zoom_scale * factor))
        canvas.zoomed = canvas.zoom_scale > 1.0001
        canvas.actual_requested = False
        if not canvas.zoomed:
            canvas.offset = QPointF()
        self.canvas_zoom_changed(canvas)
        canvas.update()

    def keyboard_pan(self, x, y):
        if self.ready() and self.stack.currentIndex() == 0:
            canvas = self.active_canvas()
            if canvas.zoomed:
                canvas.offset += QPointF(-50 * x, -50 * y)
                canvas.update()

    def navigate(self, direction):
        if not self.ready() or not self.visible or self.stack.currentIndex() != 0:
            return
        path = self.active_path() or self.current_path()
        if self.compare_mode and self.independent_paths:
            if direction > 0 and path not in self.state.setdefault("reviewed", []):
                self.state["reviewed"].append(path)
            indices = [self.visible.index(p) for p in self.independent_paths if p in self.visible]
            if len(indices) == 2 and all(0 <= i + direction < len(self.visible) for i in indices):
                self.independent_paths = [self.visible[i + direction] for i in indices]
                self.position = indices[self.active_side] + direction
                if self.filter_combo.currentIndex() == 1 and direction > 0:
                    self.rebuild_views()
                else:
                    self.refresh_current()
            return
        if direction > 0:
            reviewed = self.state.setdefault("reviewed", [])
            if path not in reviewed:
                reviewed.append(path)
        old_visible = list(self.visible)
        if self.filter_combo.currentIndex() == 1 and direction > 0:
            self.rebuild_views()
        else:
            last_position = len(self.visible) - 1
            if self.compare_mode and not self.pinned_path and len(self.visible) > 1:
                last_position -= 1
            self.position = max(0, min(self.position + direction, last_position))
            self.refresh_current()
        self.save()
        if old_visible and path == old_visible[-1] and direction > 0:
            self.update_footer("最後の写真です。候補を確認して［整理を反映］できます。")

    def film_clicked(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        modifiers = QApplication.keyboardModifiers()
        if (modifiers & Qt.KeyboardModifier.ControlModifier and
            not modifiers & Qt.KeyboardModifier.ShiftModifier):
            self.clear_clicked_path(path)
            return
        if self.ready() and path in self.visible:
            self.position = self.visible.index(path)
            if self.compare_mode and self.independent_paths:
                self.independent_paths[self.active_side] = path
                self.refresh_current()
                return
            if self.compare_mode and not self.pinned_path and len(self.visible) > 1 and self.position == len(self.visible) - 1:
                self.position -= 1
                self.active_side = 1
            self.refresh_current()

    def request_image(self, path, kind, priority=0):
        key = (path, kind)
        if key in self.images:
            self.images.move_to_end(key)
            self.image_ready({"key": key, "image": self.images[key]})
            return
        if key in getattr(self, "image_pending", set()):
            return
        if not hasattr(self, "image_pending"):
            self.image_pending = set()
        self.image_pending.add(key)

        generation = self.generation
        # Use actual display pixels for a crisp fit on high-DPI and 4K screens.
        screen = self.screen() or QApplication.primaryScreen()
        preview_limit = 2400
        if screen:
            size = screen.size()
            preview_limit = min(5120, max(2400, round(max(size.width(), size.height()) * screen.devicePixelRatio())))

        def read(progress):
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            size = reader.size()
            maximum = 500 if kind == "thumb" else (preview_limit if kind == "preview" else 0)
            if maximum and size.isValid() and max(size.width(), size.height()) > maximum:
                reader.setScaledSize(size.scaled(maximum, maximum, Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if image.isNull():
                return {"key": key, "image": image, "error": reader.errorString(), "generation": generation}
            if image.colorSpace().isValid():
                image = image.convertedToColorSpace(QColorSpace.NamedColorSpace.SRgb)
            return {"key": key, "image": image, "generation": generation}

        self.submit(read, self.image_ready, priority=priority)

    def image_ready(self, result):
        if "key" not in result:
            return
        if result.get("generation", self.generation) != self.generation:
            return
        key = result["key"]
        self.image_pending.discard(key)
        image = result["image"]
        if key not in self.images and not image.isNull():
            self.images[key] = image
            while sum(i.sizeInBytes() for i in self.images.values()) > 180 * 1024 * 1024 and len(self.images) > 1:
                self.images.popitem(last=False)
        path, kind = key
        if kind == "thumb":
            if path in self.visible:
                index = self.visible.index(path)
                icon = QIcon(QPixmap.fromImage(image))
                # Updating a thumbnail emits itemChanged; it is not a check change.
                blocked = self.grid.blockSignals(True)
                try:
                    self.grid.item(index).setIcon(icon)
                finally:
                    self.grid.blockSignals(blocked)
                film_item = self.film_item(path)
                if film_item:
                    film_item.setIcon(icon)
        else:
            for canvas in (self.canvas, self.compare_canvas):
                if canvas.path != path:
                    continue
                if image.isNull():
                    if canvas.image.isNull():
                        canvas.loading = False
                        canvas.caption = "この写真を表示できません。\n" + result.get("error", "")
                elif (kind == "full" and canvas.zoomed) or (kind == "preview" and (not canvas.zoomed or canvas.image.isNull())):
                    canvas.image = image
                    canvas.loading = False
                    if kind == "full" and canvas.actual_requested:
                        canvas.set_actual()
                canvas.update()

    def request_thumbnails(self, *_):
        if self.photo_fullscreen and self.stack.currentIndex() == 0:
            return
        view = self.grid if self.stack.currentIndex() == 1 else self.film
        if not view.count():
            return
        top = view.indexAt(QPoint(8, 8)).row()
        if top < 0:
            top = 0
        grid_size = self.grid.gridSize()
        count = ((view.viewport().width() // max(1, grid_size.width()) + 2) *
                 (view.viewport().height() // max(1, grid_size.height()) + 2)) if view is self.grid else 16
        for index in range(max(0, top - 2), min(view.count(), top + count)):
            path = view.item(index).data(Qt.ItemDataRole.UserRole)
            if path:
                self.request_image(path, "thumb")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "brand_tagline"):
            self.brand_tagline.setVisible(self.width() >= 980 and not self.photo_fullscreen)
        if hasattr(self, "first_overlay") and self.photo_fullscreen:
            QTimer.singleShot(0, self.position_first_overlay)
        QTimer.singleShot(80, self.request_thumbnails)

    def toggle_zoom(self):
        if not self.ready() or not self.active_path() or self.stack.currentIndex() != 0:
            return
        canvas = self.active_canvas()
        if canvas.zoomed:
            canvas.fit_image()
        else:
            canvas.zoomed = True
            canvas.actual_requested = True
            self.request_image(canvas.path, "full")
        self.update_active_display()

    def canvas_zoom_changed(self, canvas):
        if canvas.path:
            self.request_image(canvas.path, "full" if canvas.zoomed else "preview", priority=1)
        self.update_active_display()

    def checked_changed(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.checked.add(path)
        else:
            self.checked.discard(path)
        self.update_checked_bar()

    def update_checked_bar(self):
        self.checked_label.setText(f"{len(self.checked):,}枚チェック")
        self.batch_trash_button.setEnabled(bool(self.checked) and self.ready())
        has_trash = any(self.state.get("decisions", {}).get(p, {}).get("action") == "trash" for p in self.checked)
        self.batch_clear_trash_button.setEnabled(has_trash and self.ready())

    def check_all(self):
        if self.ready():
            for index in range(self.grid.count()):
                self.grid.item(index).setCheckState(Qt.CheckState.Checked)

    def uncheck_all(self):
        self.checked.clear()
        self.grid.reset_anchor()
        self.grid.blockSignals(True)
        for index in range(self.grid.count()):
            self.grid.item(index).setCheckState(Qt.CheckState.Unchecked)
        self.grid.blockSignals(False)
        self.update_checked_bar()

    def make_decisions(self, paths, action, destination=""):
        if not self.ready():
            return False
        # Prepare the whole command before changing any state.
        try:
            prepared = {path: {"action": action, "destination": destination,
                               "fingerprint": fingerprint(Path(path))} for path in paths}
        except OSError as error:
            self.show_error(str(error))
            return False
        decisions = self.state.setdefault("decisions", {})
        previously_reviewed = set(self.state.get("reviewed", []))
        self.undo_stack.append({"previous": {p: decisions.get(p) for p in paths}, "cursor": self.current_path(),
                                "reviewed_before": {p: p in previously_reviewed for p in paths}})
        for path, decision in prepared.items():
            decisions[path] = decision
            if path not in self.state.setdefault("reviewed", []):
                self.state["reviewed"].append(path)
            self.refresh_item(path)
        self.save()
        self.update_footer()
        return True

    def mark_current(self, action):
        if self.stack.currentIndex() == 1:
            if action == "trash":
                self.batch_trash()
            elif action == "hold" and self.checked:
                self.mark_batch("hold")
            return
        path = self.active_path()
        self.mark_path(path, action)

    def mark_path_for_trash(self, path):
        self.mark_path(path, "trash")

    def mark_path(self, path, action):
        if path and self.make_decisions([path], action):
            if self.filter_combo.currentIndex() != 0:
                self.rebuild_views()
            else:
                self.navigate(1)
            self.update_footer("削除候補にしました。確定までは取り消せます。" if action == "trash" else "保留にしました。")

    def trash_dropped(self, path):
        if path in (self.canvas.path, self.compare_canvas.path):
            self.mark_path(path, "trash")

    def assign(self, slot):
        if not self.ready() or slot >= len(self.state.get("destinations", [])):
            return
        destination = self.state["destinations"][slot]
        if self.stack.currentIndex() == 1:
            if self.checked:
                self.mark_batch("file", destination["path"])
            else:
                self.update_footer("まずカードをチェックしてください。チェックした写真をまとめて振り分けます。")
            return
        path = self.active_path()
        if path and self.make_decisions([path], "file", destination["path"]):
            if self.filter_combo.currentIndex() != 0:
                self.rebuild_views()
            else:
                self.navigate(1)
            self.update_footer(f"［{destination['label']}］に振り分け予定。最後にまとめて反映できます。")

    def mark_batch(self, action, destination=""):
        paths = [p for p in self.entries if p in self.checked]
        if paths and self.make_decisions(paths, action, destination):
            self.uncheck_all()
            self.rebuild_views()
            self.update_footer(f"{len(paths)}枚をまとめて" + ("削除候補にしました。" if action == "trash" else "指定しました。"))

    def batch_trash(self):
        self.mark_batch("trash")

    def clear_checked_trash(self):
        if not self.ready() or self.stack.currentIndex() != 1:
            return
        decisions = self.state.get("decisions", {})
        paths = [p for p in self.entries if p in self.checked and decisions.get(p, {}).get("action") == "trash"]
        if not paths:
            return
        reviewed = set(self.state.get("reviewed", []))
        self.undo_stack.append({"previous": {p: decisions[p] for p in paths}, "cursor": self.current_path(),
                                "reviewed_before": {p: p in reviewed for p in paths}})
        for path in paths:
            decisions.pop(path)
        self.checked.difference_update(paths)
        self.save()
        self.rebuild_views()
        self.update_footer(f"{len(paths)}枚の削除候補を解除しました。取り消すと元に戻せます。")

    def clear_clicked_path(self, path):
        if not self.ready() or not path:
            return
        if self.state.get("decisions", {}).get(path, {}).get("action") not in ("trash", "file"):
            return
        self.clear_decisions([path])

    def clear_decisions(self, paths):
        decisions = self.state.get("decisions", {})
        paths = [p for p in dict.fromkeys(paths) if p in decisions]
        if not paths:
            return 0
        reviewed = set(self.state.get("reviewed", []))
        self.undo_stack.append({"previous": {p: decisions[p] for p in paths}, "cursor": self.current_path(),
                                "reviewed_before": {p: p in reviewed for p in paths}})
        for path in paths:
            decisions.pop(path)
        self.checked.difference_update(paths)
        self.save()
        self.rebuild_views()
        self.update_footer(f"{len(paths)}枚の候補を解除しました。取り消すと元に戻せます。")
        return len(paths)

    def clear_current(self):
        if not self.ready():
            return
        if self.stack.currentIndex() == 1:
            self.clear_checked_trash()
            return
        path = self.active_path()
        if not path:
            return
        self.clear_decisions([path])

    def undo(self):
        if not self.ready() or not self.undo_stack:
            return
        command = self.undo_stack.pop()
        decisions = self.state["decisions"]
        for path, previous in command["previous"].items():
            if previous is None:
                decisions.pop(path, None)
            else:
                decisions[path] = previous
        reviewed = set(self.state.get("reviewed", []))
        for path, was_reviewed in command["reviewed_before"].items():
            if was_reviewed:
                reviewed.add(path)
            else:
                reviewed.discard(path)
        self.state["reviewed"] = [p for p in self.entries if p in reviewed]
        self.state["cursor"] = command["cursor"]
        self.filter_combo.blockSignals(True)
        self.filter_combo.setCurrentIndex(0)
        self.filter_combo.blockSignals(False)
        self.visible = []  # Restore the command's cursor, not the next photo's.
        self.rebuild_views()
        self.save()
        self.update_footer("直前の指定を取り消しました。まとめて付けた候補も一度で戻せます。")

    def pending(self):
        entries = set(self.entries)
        return {p: d for p, d in self.state.get("decisions", {}).items()
                if p in entries and d["action"] in ("trash", "file")}

    def update_footer(self, message=None):
        pending = self.pending()
        trash = sum(d["action"] == "trash" for d in pending.values())
        files = len(pending) - trash
        if message is None:
            message = f"削除候補 {trash}枚  ·  振り分け予定 {files}枚  ·  作業状況は自動保存"
        self.status_label.setText(message)
        self.apply_button.setText(f"整理を反映  ({len(pending)})" if pending else "整理を反映")
        self.apply_button.setEnabled(bool(pending) and self.ready())
        self.undo_button.setEnabled(bool(self.undo_stack) and self.ready())
        self.open_button.setEnabled(not self.applying)
        self.refresh_button.setEnabled(self.ready())
        self.property_button.setEnabled(self.ready())
        self.crop_button.setEnabled(self.ready())
        self.parent_button.setEnabled(self.ready() and Path(self.root).parent != Path(self.root))
        self.folder_list.setEnabled(self.ready())
        self.create_folder_button.setEnabled(self.ready())
        self.select_folder_item()
        self.mode_combo.setEnabled(self.ready())
        self.filter_combo.setEnabled(self.ready())
        self.trash_zone.setEnabled(self.ready())
        self.hold_button.setEnabled(self.ready())
        self.canvas.setEnabled(not self.applying and not self.scanning)
        self.compare_canvas.setEnabled(not self.applying and not self.scanning)
        self.grid.setEnabled(not self.applying and not self.scanning)
        self.update_checked_bar()

    def confirm_apply(self):
        pending = self.pending()
        if not pending or not self.ready():
            return
        dialog = ApplyPreviewDialog(self, pending)
        result = dialog.exec()
        # Candidate cancellations are retained even when returning without applying.
        self.clear_decisions(dialog.cancelled_paths)
        if result == QDialog.DialogCode.Accepted:
            remaining = self.pending()
            if remaining:
                self.start_apply(remaining)

    def start_apply(self, pending):
        self.applying = True
        self.save()
        self.update_footer("ファイルを整理しています。アプリを閉じずにお待ちください…")
        self.rebuild_destinations()
        mode = self.state.get("mode", "move")
        directory = self.data_dir

        def execute(progress):
            store = Store(directory)
            results = []
            try:
                for index, (path, decision) in enumerate(pending.items()):
                    result = apply_decision(store, path, decision, mode)
                    results.append(result)
                    progress({"done": index + 1, "total": len(pending)})
            finally:
                store.close()
            return {"results": results}

        def finished(result):
            self.applying = False
            successes = []
            errors = []
            for item in result.get("results", []):
                if item["success"]:
                    path = item["source"]
                    successes.append(path)
                    self.state["decisions"].pop(path, None)
                    self.state.setdefault("completed", {})[path] = {"target": item["target"], "action": item["action"]}
                else:
                    errors.append(item["source"] + "\n" + item["error"])
            if "error" in result:
                errors.append(result["error"])
            self.undo_stack.clear()
            if self.pinned_path in successes:
                self.pinned_path = ""
                self.active_side = 0
            self.checked.difference_update(successes)
            self.save()
            self.refresh_folder_list()
            self.rebuild_destinations()
            self.rebuild_views()
            self.update_footer(f"{len(successes)}枚を反映しました。" + (f"  {len(errors)}件は未完了です。" if errors else "  操作履歴から行き先を確認できます。"))
            if errors:
                self.show_error(f"{len(errors)}件を処理できませんでした。未完了の指定は候補に残しています。\n"
                                "アクセス拒否の場合は、アプリの実行環境と写真・移動先フォルダの権限を確認してください。\n"
                                "ごみ箱を使えない場合は、元の写真を保持します。\n\n" + "\n\n".join(errors))

        self.submit(execute, finished, lambda p: self.status_label.setText(f"整理中  {p['done']} / {p['total']}枚…"))

    def show_history(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("ファイル操作の履歴")
        dialog.resize(850, 540)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("ファイル操作の履歴（最新300件）"))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        statuses = {"done": "完了", "error": "失敗", "started": "中断・確認が必要", "copied": "コピー済み・確認が必要"}
        actions = {"trash": "ごみ箱へ", "move": "移動", "copy": "コピー"}
        rows = []
        for created, action, status, source, target, error in self.store.history():
            rows.append(f"{created}  [{statuses.get(status, status)}]  {actions.get(action, action)}\n{source}\n→ {target or '履歴に行き先がありません'}" + (f"\n{error}" if error else ""))
        text.setPlainText("\n\n".join(rows) or "まだファイル操作は実行していません。")
        layout.addWidget(text)
        layout.addWidget(button("閉じる", dialog.accept))
        dialog.exec()

    def selected_photo_path(self):
        if self.stack.currentIndex() == 0:
            return self.active_path()
        item = self.grid.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def show_properties(self):
        path = self.selected_photo_path()
        if self.ready() and path:
            PropertyDialog(path, self).exec()
        else:
            self.update_footer("写真を選んでからプロパティを開いてください。")

    def show_crop(self):
        path = self.selected_photo_path()
        if not self.ready() or not path:
            self.update_footer("写真を選んでから切り抜きを開いてください。")
            return
        dialog = CropDialog(path, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved_path:
            self.update_footer("切り抜いた写真を別ファイルで保存しました：" + Path(dialog.saved_path).name)
            self.refresh_folder()

    def show_help(self):
        HelpDialog(self).exec()

    def show_error(self, message):
        if len(message) < 500:
            QMessageBox.warning(self, "ピクソート — 確認してください", message)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("ピクソート — 処理結果の確認")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        heading = label("未完了の写真があります")
        heading.setStyleSheet("font-size: 18px; font-weight: 700")
        layout.addWidget(heading)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(message)
        layout.addWidget(details, 1)
        layout.addWidget(button("閉じる", dialog.accept, "primary"))
        dialog.exec()

    def closeEvent(self, event):
        if self.applying:
            self.update_footer("ファイルの整理が終わってから閉じてください。")
            event.ignore()
            return
        self.closing = True
        self.save()
        # Tasks never access widgets directly. Await readers before destroying their signals.
        self.pool.waitForDone()
        self.store.close()
        event.accept()


class LocalInstance(QObject):
    received = Signal(object)

    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = QLockFile(str(self.data_dir / "instance.lock"))
        self.lock.setStaleLockTime(0)
        digest = hashlib.sha256(str(self.data_dir).casefold().encode("utf-8")).hexdigest()[:24]
        self.name = "pixsort-" + digest
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.clients = set()
        self.server.newConnection.connect(self.accept_clients)

    def start(self, payload):
        if not self.lock.tryLock(0):
            # Another instance may still be starting its local server.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                client = QLocalSocket()
                client.connectToServer(self.name)
                if client.waitForConnected(250):
                    client.write((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))
                    client.flush()
                    client.waitForBytesWritten(500)
                    if client.waitForReadyRead(2500) and bytes(client.readAll()).startswith(b"ok"):
                        client.disconnectFromServer()
                        return "forwarded"
                    client.abort()
                    return "unavailable"
                time.sleep(.1)
            return "unavailable"
        if not self.server.listen(self.name):
            self.lock.unlock()
            return "unavailable"
        return "primary"

    def accept_clients(self):
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            self.clients.add(client)
            client.readyRead.connect(lambda c=client: self.read_client(c))
            client.disconnected.connect(lambda c=client: self.finish_client(c))
            if client.bytesAvailable():
                self.read_client(client)

    def read_client(self, client):
        if not client.canReadLine():
            return
        try:
            message = json.loads(bytes(client.readLine()).decode("utf-8"))
            if isinstance(message, dict):
                self.received.emit(message)
                client.write(b"ok\n")
                client.flush()
        except (UnicodeError, ValueError):
            client.disconnectFromServer()

    def finish_client(self, client):
        self.clients.discard(client)
        if isValid(client):
            client.deleteLater()

    def close(self):
        self.server.close()
        self.lock.unlock()


def register_open_with(executable, registry=None):
    """Register PixSort as an optional per-user Windows Open With handler."""
    if registry is None:
        import winreg as registry
    executable = str(Path(executable).resolve())
    exe_name = Path(executable).name
    progid = "KOMA.Photo.0.8"
    entries = [
        (f"Software\\Classes\\{progid}", "", registry.REG_SZ, "ピクソート"),
        (f"Software\\Classes\\{progid}\\DefaultIcon", "", registry.REG_SZ, f'"{executable}",0'),
        (f"Software\\Classes\\{progid}\\shell\\open\\command", "", registry.REG_SZ, f'"{executable}" "%1"'),
        (f"Software\\Classes\\Applications\\{exe_name}", "FriendlyAppName", registry.REG_SZ, "ピクソート"),
        (f"Software\\Classes\\Applications\\{exe_name}\\shell\\open\\command", "", registry.REG_SZ, f'"{executable}" "%1"'),
    ]
    for ext in sorted(SUPPORTED):
        entries.append((f"Software\\Classes\\{ext}\\OpenWithProgids", progid, registry.REG_NONE, b""))
        entries.append((f"Software\\Classes\\Applications\\{exe_name}\\SupportedTypes", ext, registry.REG_SZ, ""))
    for path, name, value_type, value in entries:
        with registry.CreateKey(registry.HKEY_CURRENT_USER, path) as key:
            registry.SetValueEx(key, name, 0, value_type, value)
    if getattr(registry, "__name__", "") == "winreg":
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("photo", nargs="?", help="開く写真ファイル")
    parser.add_argument("--folder")
    parser.add_argument("--register-open-with", action="store_true")
    parser.add_argument("--capture", help=argparse.SUPPRESS)
    parser.add_argument("--grid", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--compare", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fullscreen", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    QApplication.setApplicationName("PixSort")
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Yu Gothic UI", 10))
    if args.register_open_with:
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            QMessageBox.warning(None, "ピクソート", "登録はWindowsの配布版から実行してください。")
            return 1
        try:
            register_open_with(sys.executable)
        except OSError as error:
            QMessageBox.warning(None, "ピクソート", "登録できませんでした。\n" + str(error))
            return 1
        QMessageBox.information(None, "ピクソート", "ピクソートを『プログラムから開く』の候補に登録しました。標準アプリは変更していません。")
        return 0
    if args.photo and (not Path(args.photo).is_file() or Path(args.photo).suffix.lower() not in SUPPORTED):
        QMessageBox.warning(None, "ピクソート", "JPEGまたはPNGの写真を指定してください。")
        return 1
    try:
        instance = LocalInstance(DATA_DIR)
        instance_status = instance.start({"photo": args.photo or "", "folder": args.folder or ""})
    except OSError as error:
        QMessageBox.critical(None, "ピクソートを起動できません", "作業記録の保存先にアクセスできません。\n" + str(error))
        return 1
    if instance_status == "forwarded":
        return 0
    if instance_status != "primary":
        QMessageBox.warning(None, "ピクソート", "別のピクソートが起動中ですが、操作を引き継げませんでした。開いているピクソートを確認してください。")
        return 1
    try:
        migrated = False
        if (getattr(sys, "frozen", False) and "PIXSORT_DATA_DIR" not in os.environ and "KOMA_DATA_DIR" not in os.environ):
            parent = application_directory().parent
            for previous in (parent / "PixSort-v0.12-Windows" / "PixSortData",
                             parent / "PixSort-v0.11-Windows" / "PixSortData",
                             parent / "Koma-v0.10-Windows" / "KomaData"):
                if migrate_previous_work(DATA_DIR, previous):
                    migrated = True
                    break
        window = Window()
        window.recovered_previous = migrated
    except Exception as error:
        QMessageBox.critical(None, "ピクソートを起動できません", f"作業記録の保存・引き継ぎを確認してください。旧版の記録は保持されています。\n\n{error}")
        instance.close()
        return 1

    def open_external(message):
        if window.closing:
            return
        if window.applying or app.activeModalWidget():
            QTimer.singleShot(500, lambda: open_external(message))
            return
        photo, folder = message.get("photo", ""), message.get("folder", "")
        if photo and Path(photo).is_file() and Path(photo).suffix.lower() in SUPPORTED:
            window.open_folder(str(Path(photo).resolve().parent), photo)
        elif folder and Path(folder).is_dir():
            window.open_folder(folder)
        window.showNormal() if window.isMinimized() else None
        window.raise_()
        window.activateWindow()

    instance.received.connect(open_external)
    window.show()
    if args.photo:
        QTimer.singleShot(60, lambda: window.open_folder(str(Path(args.photo).resolve().parent), args.photo))
    elif args.folder:
        QTimer.singleShot(60, lambda: window.open_folder(args.folder))
    else:
        settings = DATA_DIR / "settings.json"
        if settings.exists():
            try:
                previous = json.loads(settings.read_text(encoding="utf-8")).get("last_root", "")
                if previous and Path(previous).is_dir():
                    QTimer.singleShot(60, lambda: window.open_folder(previous))
            except (OSError, ValueError):
                pass
    if args.grid:
        QTimer.singleShot(1500, lambda: window.set_view(True))
    if args.compare:
        QTimer.singleShot(1500, window.show_compare)
    if args.fullscreen:
        QTimer.singleShot(2200, window.toggle_photo_fullscreen)
    if args.capture:
        def capture():
            window.grab().save(args.capture)
            window.close()
        QTimer.singleShot(3500, capture)
    result = app.exec()
    instance.close()
    return result


if __name__ == "__main__":
    sys.exit(main())
