"""Manual review of local similar-photo suggestions."""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QColorSpace, QIcon, QImageReader, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from shiboken6 import isValid
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QStyledItemDelegate, QVBoxLayout, QWidget)

from similar import find_similar
from photo_tools import PropertyDialog


class SimilarCards(QListWidget):
    """Click to check a card; Shift-click checks an inclusive range."""
    def __init__(self):
        super().__init__()
        self.anchor_path = ""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item:
                path = item.data(Qt.ItemDataRole.UserRole)
                anchor = next((i for i in range(self.count())
                               if self.item(i).data(Qt.ItemDataRole.UserRole) == self.anchor_path), -1)
                row = self.row(item)
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and anchor >= 0:
                    for index in range(min(anchor, row), max(anchor, row) + 1):
                        self.item(index).setCheckState(Qt.CheckState.Checked)
                else:
                    item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)
                    self.anchor_path = path
                self.setCurrentItem(item)
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and self.currentItem():
            item = self.currentItem()
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)
            self.anchor_path = item.data(Qt.ItemDataRole.UserRole)
            event.accept()
        else:
            super().keyPressEvent(event)


class SimilarCardDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        # Qt's native Windows style otherwise caches a 20px text-only height.
        size = self.parent().gridSize()
        return QSize(max(1, size.width() - 2), max(1, size.height() - 2))

    def paint(self, painter, option, index):
        painter.save(); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(3, 3, -3, -3)
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
        current = self.parent().currentIndex() == index
        painter.setBrush(QColor("#293b2a" if checked else "#20262e"))
        painter.setPen(QPen(QColor("#a9dfb7" if checked else ("#8ab7ef" if current else "#39424f")), 2 if checked or current else 1))
        painter.drawRoundedRect(rect, 7, 7)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon): icon.paint(painter, rect.adjusted(7, 7, -7, -43), Qt.AlignmentFlag.AlignCenter)
        square = QRect(rect.left() + 9, rect.top() + 9, 21, 21)
        painter.setBrush(QColor("#a9dfb7" if checked else "#141a21"))
        painter.setPen(QPen(QColor("#a9dfb7" if checked else "#ccd6e0"), 1.5)); painter.drawRoundedRect(square, 4, 4)
        if checked:
            painter.setPen(QPen(QColor("#152218"), 2.5))
            painter.drawLine(square.left()+5, square.top()+10, square.left()+9, square.top()+14)
            painter.drawLine(square.left()+9, square.top()+14, square.left()+16, square.top()+6)
        painter.setPen(QColor("#e1e7ee"))
        name_rect = QRect(rect.left()+7, rect.bottom()-39, rect.width()-14, 19)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(index.data() or "", Qt.TextElideMode.ElideMiddle, name_rect.width()))
        status = index.data(Qt.ItemDataRole.UserRole+2) or ""
        action = index.data(Qt.ItemDataRole.UserRole+3)
        painter.setPen(QColor({"trash":"#ffb9c3", "file":"#a9dfb7", "hold":"#f0cf8d"}.get(action,"#929daa")))
        status_rect = QRect(rect.left()+7, rect.bottom()-21, rect.width()-14, 17)
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(status, Qt.TextElideMode.ElideRight, status_rect.width()))
        painter.restore()


class SimilarDialog(QDialog):
    def __init__(self, window, paths):
        super().__init__(window)
        self.window = window
        self.paths = list(paths)
        self.groups = []
        self.checked = set()
        self.chosen_paths = []
        self.cancel_event = threading.Event()
        self.running = False
        self.closed = False
        self.thumb_generation = 0
        self.photo_only = False
        self.dialog_fullscreen = False
        self.return_fullscreen = False
        self.compare_mode = False
        self.active_canvas = None
        self.image_cache = OrderedDict()
        self.image_loading = set()
        self.shortcuts = []
        self.setWindowTitle("似た写真を探す")
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            self.resize(min(1050, available.width()-30), min(720, available.height()-30))
        else:
            self.resize(960, 650)
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("似た写真を探す")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        self.title_label = title; heading.addWidget(title); heading.addStretch()
        self.fullscreen_button = QPushButton("全画面  F11")
        self.fullscreen_button.clicked.connect(self.toggle_dialog_fullscreen)
        heading.addWidget(self.fullscreen_button); layout.addLayout(heading)
        note = QLabel("表示中の写真を小さくして構図を比較します。候補を確認し、残す・削除する写真は自分で選びます。")
        note.setWordWrap(True); self.note = note; layout.addWidget(note)
        self.filter_widget = QWidget(); filters = QHBoxLayout(self.filter_widget)
        filters.setContentsMargins(0, 0, 0, 0)
        self.mode = QComboBox()
        self.mode.addItem("撮影時刻 ＋ 構図", "time")
        self.mode.addItem("構図のみ", "visual")
        self.mode.setToolTip("撮影時刻がない写真は『構図のみ』で探せます。")
        filters.addWidget(self.mode)
        self.seconds = QComboBox()
        for n in (1, 3, 5, 10, 30): self.seconds.addItem(f"{n}秒以内", n)
        self.seconds.setCurrentIndex(1); filters.addWidget(self.seconds)
        self.threshold = QComboBox()
        self.threshold.addItem("似ている度：厳しめ", 0.90)
        self.threshold.addItem("似ている度：標準", 0.83)
        self.threshold.addItem("似ている度：広め", 0.76)
        self.threshold.setCurrentIndex(1); filters.addWidget(self.threshold)
        self.search_button = QPushButton("探す")
        self.search_button.setObjectName("primary"); self.search_button.clicked.connect(self.start_search)
        filters.addWidget(self.search_button)
        self.stop_button = QPushButton("中断"); self.stop_button.clicked.connect(self.stop_search)
        self.stop_button.hide(); filters.addWidget(self.stop_button)
        filters.addStretch(); layout.addWidget(self.filter_widget)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.status = QLabel(f"表示中の {len(self.paths):,} 枚が対象です。条件を選んで［探す］を押してください。")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.progress = QProgressBar(); self.progress.setRange(0, len(self.paths)); self.progress.hide()
        self.progress.setStyleSheet("QProgressBar {background: #232932; border: 1px solid #596675; border-radius: 5px; text-align: center;} QProgressBar::chunk {background: #668e70; border-radius: 4px;}")
        layout.addWidget(self.progress)
        main = QHBoxLayout()
        self.left_panel = QWidget(); left = QVBoxLayout(self.left_panel)
        left.setContentsMargins(0, 0, 0, 0); left.addWidget(QLabel("候補グループ"))
        self.group_list = QListWidget(); self.group_list.setMinimumWidth(100); self.group_list.setMaximumWidth(190)
        self.group_list.currentRowChanged.connect(self.group_selected); left.addWidget(self.group_list, 1); main.addWidget(self.left_panel, 1)
        self.center_panel = QWidget(); center = QVBoxLayout(self.center_panel)
        center.setContentsMargins(0, 0, 0, 0); self.count_label = QLabel("写真をクリックしてチェック")
        center.addWidget(self.count_label)
        self.items = SimilarCards(); self.items.setViewMode(QListView.ViewMode.IconMode)
        self.items.setResizeMode(QListView.ResizeMode.Adjust); self.items.setMovement(QListView.Movement.Static)
        self.items.setIconSize(QSize(132, 80)); self.items.setGridSize(QSize(150, 135))
        self.items.setSpacing(2); self.items.setUniformItemSizes(True)
        self.items.setItemDelegate(SimilarCardDelegate(self.items))
        self.items.itemChanged.connect(self.item_checked)
        self.items.currentItemChanged.connect(self.preview_selected)
        center.addWidget(self.items, 1); main.addWidget(self.center_panel, 3)
        self.right_panel = QWidget(); right = QVBoxLayout(self.right_panel)
        right.setContentsMargins(0, 0, 0, 0); self.preview_info = QLabel("写真を選択してください")
        self.preview_info.setWordWrap(True); self.preview_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.preview_info.setMaximumHeight(60); right.addWidget(self.preview_info)
        self.preview = type(window.canvas)()
        self.compare_canvas = type(window.canvas)()
        self.preview.setMinimumSize(80, 80); self.compare_canvas.setMinimumSize(80, 80)
        self.preview.setFocusPolicy(Qt.FocusPolicy.StrongFocus); self.compare_canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas_row = QHBoxLayout(); self.canvas_row.setContentsMargins(0, 0, 0, 0); self.canvas_row.setSpacing(4)
        self.canvas_row.addWidget(self.preview, 1); self.canvas_row.addWidget(self.compare_canvas, 1)
        self.compare_canvas.hide(); right.addLayout(self.canvas_row, 1)
        self.active_canvas = self.preview
        for canvas in (self.preview, self.compare_canvas):
            canvas.activated.connect(self.activate_preview)
            canvas.navigate_requested.connect(self.navigate_preview)
            canvas.trash_requested.connect(lambda path: self.mark_one(path, "trash", advance=True))
            canvas.clear_requested.connect(self.clear_one)
            canvas.zoom_requested.connect(self.preview_zoom_changed)
            canvas.fullscreen_requested.connect(self.toggle_photo_fullscreen)
        self.exit_photo_button = QPushButton("全画面を終了  Esc", self.preview)
        self.exit_photo_button.clicked.connect(self.exit_photo_fullscreen); self.exit_photo_button.hide()
        self.preview.installEventFilter(self)
        self.zoom_controls = QWidget(); zoom_row = QHBoxLayout(self.zoom_controls)
        zoom_row.setContentsMargins(0, 0, 0, 0)
        self.prev_button = QPushButton("←"); self.prev_button.setToolTip("前の写真へ")
        self.prev_button.clicked.connect(lambda: self.navigate_preview(-1))
        self.next_button = QPushButton("→"); self.next_button.setToolTip("次の写真へ")
        self.next_button.clicked.connect(lambda: self.navigate_preview(1))
        self.zoom_out_button = QPushButton("−"); self.zoom_out_button.clicked.connect(lambda: self.zoom_active(.8))
        self.zoom_in_button = QPushButton("＋"); self.zoom_in_button.clicked.connect(lambda: self.zoom_active(1.25))
        self.actual_button = QPushButton("原寸"); self.actual_button.clicked.connect(self.toggle_actual)
        self.actual_button.setToolTip("原寸でピントを確認 / 画面に合わせる（Z）")
        self.properties_button = QPushButton("情報"); self.properties_button.clicked.connect(self.show_properties)
        self.properties_button.setToolTip("写真のプロパティ（Alt+Enter）")
        for control in (self.prev_button, self.next_button, self.zoom_out_button, self.zoom_in_button, self.actual_button, self.properties_button):
            control.setStyleSheet("padding: 5px 3px; font-size: 11px;")
            control.setMinimumWidth(28)
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            zoom_row.addWidget(control, 1)
        right.addWidget(self.zoom_controls)
        hint = QLabel("写真：左 前 / 右 次 / Shift＋左 削除候補 / Ctrl＋左 解除 / ホイール 拡大 / 押込 全画面")
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        hint.setToolTip("写真にマウスを置くと、X・H・0・振り分け先キーも使えます。Ctrl＋Fで写真全画面。")
        self.preview_hint = hint; right.addWidget(hint)
        main.addWidget(self.right_panel, 2)
        layout.addLayout(main, 1)
        self.actions_widget = QWidget(); actions = QHBoxLayout(self.actions_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        self.open_button = QPushButton("選んだ写真を表示"); self.open_button.clicked.connect(self.open_photo)
        self.compare_button = QPushButton("チェック2枚を比較"); self.compare_button.clicked.connect(self.compare_photos)
        self.clear_checks_button = QPushButton("チェックを解除"); self.clear_checks_button.clicked.connect(self.clear_checks)
        actions.addWidget(self.open_button); actions.addWidget(self.compare_button); actions.addWidget(self.clear_checks_button)
        actions.addStretch(); layout.addWidget(self.actions_widget)
        self.actions2_widget = QWidget(); actions2 = QHBoxLayout(self.actions2_widget)
        actions2.setContentsMargins(0, 0, 0, 0)
        self.clear_button = QPushButton("チェックした候補を解除"); self.clear_button.clicked.connect(self.clear_candidates)
        self.trash_button = QPushButton("チェックした写真を削除候補に")
        self.trash_button.setObjectName("danger"); self.trash_button.clicked.connect(self.mark_trash)
        self.assign_button = QPushButton("振り分け先")
        self.assign_menu = QMenu(self.assign_button)
        for slot, destination in enumerate(self.window.state.get("destinations", [])):
            action = self.assign_menu.addAction(f"{destination.get('key', '')}  {destination.get('label', '')}")
            action.triggered.connect(lambda _checked=False, n=slot: self.assign_checked(n))
        self.assign_button.setMenu(self.assign_menu)
        self.assign_button.setToolTip("チェックした写真を選んだフォルダの候補に指定")
        actions2.addWidget(self.assign_button); actions2.addWidget(self.clear_button); actions2.addWidget(self.trash_button); actions2.addStretch()
        close = QPushButton("閉じる"); close.clicked.connect(self.reject); actions2.addWidget(close)
        layout.addWidget(self.actions2_widget)
        self.bind_shortcuts()
        self.update_actions()

    def mode_changed(self):
        self.seconds.setVisible(self.mode.currentData() == "time")
        self.seconds.setEnabled(self.mode.currentData() == "time" and not self.running)

    def start_search(self):
        if self.running: return
        self.cancel_event = threading.Event(); self.running = True
        self.groups = []; self.checked.clear(); self.group_list.clear(); self.items.clear()
        self.exit_compare()
        self.preview.set_photo(""); self.preview.loading = False; self.preview.caption = "写真を選択してください。"; self.preview.update()
        self.preview_info.setText("写真を選択してください"); self.thumb_generation += 1
        self.search_button.setEnabled(False); self.mode.setEnabled(False); self.seconds.setEnabled(False); self.threshold.setEnabled(False)
        self.stop_button.show(); self.progress.setValue(0); self.progress.show(); self.update_actions()
        time_window = self.seconds.currentData() if self.mode.currentData() == "time" else None
        threshold = self.threshold.currentData(); event = self.cancel_event
        self.status.setText("写真をローカルで読み取り、似た構図を探しています…")
        def work(progress):
            return find_similar(self.paths, time_window, threshold, event.is_set, progress)
        self.window.submit(work, self.search_finished, self.search_progress)

    def search_progress(self, data):
        if self.closed or not self.running: return
        self.progress.setValue(data["done"])
        self.status.setText(f"写真を比較中  {data['done']:,} / {data['total']:,} 枚")

    def stop_search(self):
        if self.running:
            self.cancel_event.set(); self.status.setText("中断しています…"); self.stop_button.setEnabled(False)

    def search_finished(self, result):
        if self.closed: return
        self.running = False; self.search_button.setEnabled(True); self.mode.setEnabled(True)
        self.seconds.setEnabled(self.mode.currentData() == "time"); self.threshold.setEnabled(True)
        self.stop_button.hide(); self.stop_button.setEnabled(True); self.progress.hide()
        if result.get("cancelled"):
            self.status.setText("検索を中断しました。写真は変更されていません。")
        elif "error" in result:
            self.status.setText("検索できませんでした：" + result["error"])
        else:
            self.groups = result["groups"]
            for index, group in enumerate(self.groups):
                name = Path(group[0]).name
                item = QListWidgetItem(f"{index + 1}  ·  {len(group)}枚\n{name}")
                item.setToolTip(" / ".join(Path(p).name for p in group)); self.group_list.addItem(item)
            summary = f"{len(self.groups):,} グループ、{sum(map(len, self.groups)):,} 枚の候補が見つかりました。"
            if result["missing_time"]:
                summary += f" 撮影時刻なし {result['missing_time']:,} 枚は対象外（構図のみなら比較可）。"
            if result["unreadable"]: summary += f" 読めなかった写真 {result['unreadable']:,} 枚。"
            if not self.groups: summary += " 条件を変えて再検索できます。"
            self.status.setText(summary)
            if self.groups: self.group_list.setCurrentRow(0)
        self.update_actions()

    def group_selected(self, row):
        self.exit_compare()
        self.thumb_generation += 1; generation = self.thumb_generation
        self.items.blockSignals(True); self.items.clear(); self.items.anchor_path = ""
        if row < 0 or row >= len(self.groups):
            self.items.blockSignals(False); self.update_actions(); return
        for path in self.groups[row]:
            item = QListWidgetItem(Path(path).name); item.setSizeHint(QSize(148, 133))
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setData(Qt.ItemDataRole.UserRole + 2, self.window.decision_description(path))
            item.setData(Qt.ItemDataRole.UserRole + 3, self.window.state.get("decisions", {}).get(path, {}).get("action", ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if path in self.checked else Qt.CheckState.Unchecked)
            item.setToolTip(self.window.tooltip_for(path)); self.items.addItem(item)
        self.items.setCurrentRow(0); self.items.blockSignals(False)
        self.preview_selected(self.items.currentItem(), None)
        self.load_thumbnails(generation, 0); self.update_actions()

    def load_thumbnails(self, generation, start):
        if self.closed or generation != self.thumb_generation: return
        end = min(start + 8, self.items.count())
        self.items.blockSignals(True)
        try:
            for index in range(start, end):
                item = self.items.item(index); path = item.data(Qt.ItemDataRole.UserRole)
                reader = QImageReader(path); reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid(): reader.setScaledSize(size.scaled(160, 100, Qt.AspectRatioMode.KeepAspectRatio))
                image = reader.read()
                if not image.isNull(): item.setIcon(QIcon(QPixmap.fromImage(image)))
        finally: self.items.blockSignals(False)
        if end < self.items.count(): QTimer.singleShot(0, lambda: self.load_thumbnails(generation, end))

    def item_checked(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked: self.checked.add(path)
        else: self.checked.discard(path)
        self.update_actions()

    def update_actions(self):
        n = len(self.checked)
        self.count_label.setText(f"{n:,} 枚チェック（他グループを含む）" if n else "クリックでチェック / Shift＋クリックで範囲選択")
        enabled = bool(self.groups) and not self.running
        self.open_button.setEnabled(enabled and self.items.currentItem() is not None)
        self.compare_button.setEnabled(enabled and (n == 2 or self.compare_mode))
        self.clear_checks_button.setEnabled(enabled and n > 0)
        self.clear_button.setEnabled(enabled and n > 0)
        self.trash_button.setEnabled(enabled and n > 0)
        self.assign_button.setEnabled(enabled and n > 0 and bool(self.window.state.get("destinations", [])))

    def preview_selected(self, item, _previous):
        if not item:
            self.preview.set_photo(""); self.preview.loading = False
            self.preview.caption = "写真を選択してください。"; self.preview.update(); self.update_actions(); return
        if self.compare_mode:
            self.exit_compare()
        path = item.data(Qt.ItemDataRole.UserRole)
        self.preview.set_photo(path); self.active_canvas = self.preview
        self.update_preview_info(); self.request_image(self.preview, "preview"); self.update_actions()

    def request_image(self, canvas, kind):
        path = canvas.path
        if not path: return
        key = (path, kind)
        if key in self.image_cache:
            self.image_cache.move_to_end(key)
            self.image_loaded(canvas, path, kind, {"image": self.image_cache[key]})
            return
        if key in self.image_loading: return
        self.image_loading.add(key)
        def work(progress):
            reader = QImageReader(path); reader.setAutoTransform(True)
            size = reader.size()
            if kind == "preview" and size.isValid() and max(size.width(), size.height()) > 2400:
                reader.setScaledSize(size.scaled(2400, 2400, Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if not image.isNull() and image.colorSpace().isValid(): image = image.convertedToColorSpace(QColorSpace.NamedColorSpace.SRgb)
            return {"image": image, "error": reader.errorString() if image.isNull() else ""}
        self.window.submit(work, lambda result: self.image_loaded(canvas, path, kind, result), priority=1)

    def image_loaded(self, canvas, path, kind, result):
        self.image_loading.discard((path, kind))
        if self.closed or not isValid(canvas): return
        image = result.get("image")
        if image is not None and not image.isNull():
            self.image_cache[(path, kind)] = image; self.image_cache.move_to_end((path, kind))
            while sum(i.sizeInBytes() for i in self.image_cache.values()) > 160 * 1024 * 1024 and len(self.image_cache) > 1:
                self.image_cache.popitem(last=False)
            # Both panes may display the same path; update both after a shared read.
            for target in (self.preview, self.compare_canvas):
                if target.path == path and ((kind == "full" and target.zoomed) or (kind == "preview" and (not target.zoomed or target.image.isNull()))):
                    target.image = image; target.loading = False
                    if kind == "full" and target.actual_requested: target.set_actual()
                    target.update()
        elif canvas.path == path and canvas.image.isNull():
            canvas.loading = False; canvas.caption = "この写真を表示できません。"; canvas.update()

    def update_preview_info(self):
        canvas = self.active_canvas or self.preview; path = canvas.path
        if path:
            self.preview_info.setText(("操作中：" if self.compare_mode else "") + Path(path).name + self.window.decision_suffix(path))
            self.preview_info.setToolTip(self.window.tooltip_for(path))
        else: self.preview_info.setText("写真を選択してください")
        for target in (self.preview, self.compare_canvas):
            target.active = bool(self.compare_mode and target is self.active_canvas)
            status = self.window.decision_description(target.path) if target.path and self.photo_only else ""
            target.overlay_status = "削除予定" if status == "削除候補" else status
            target.update()
        self.actual_button.setText("全体" if canvas.zoomed else "原寸")
        self.actual_button.setToolTip("画面に合わせる（Z）" if canvas.zoomed else "原寸でピントを確認（Z）")

    def activate_preview(self, canvas):
        if canvas.path:
            self.active_canvas = canvas; canvas.setFocus(); self.update_preview_info()

    def preview_zoom_changed(self, canvas):
        self.request_image(canvas, "full" if canvas.zoomed else "preview")
        self.update_preview_info()

    def zoom_active(self, factor):
        canvas = self.active_canvas
        if not canvas or not canvas.path or canvas.image.isNull(): return
        canvas.zoom_scale = max(1.0, min(12.0, canvas.zoom_scale * factor))
        canvas.zoomed = canvas.zoom_scale > 1.0001; canvas.actual_requested = False
        if not canvas.zoomed: canvas.offset = QPointF()
        self.preview_zoom_changed(canvas); canvas.update()

    def pan_active(self, x, y):
        canvas = self.active_canvas
        if canvas and canvas.path and canvas.zoomed:
            canvas.offset += QPointF(-50 * x, -50 * y); canvas.update()

    def toggle_actual(self):
        canvas = self.active_canvas
        if not canvas or not canvas.path: return
        if canvas.zoomed: canvas.fit_image()
        else:
            canvas.zoomed = True; canvas.actual_requested = True
            self.request_image(canvas, "full")
        self.update_preview_info()

    def navigate_preview(self, direction, independent=None):
        if self.running or not self.groups: return
        if self.compare_mode:
            paths = [self.preview.path, self.compare_canvas.path]
            row = self.group_list.currentRow()
            group = self.groups[row] if 0 <= row < len(self.groups) else []
            population = group if all(path in group for path in paths) else self.paths
            if any(path not in population for path in paths): return
            indices = [population.index(path) for path in paths]
            if independent is None:
                independent = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
            targets = [0 if self.active_canvas is self.preview else 1] if independent else [0, 1]
            if not all(0 <= indices[i] + direction < len(population) for i in targets): return
            for i in targets:
                canvas = (self.preview, self.compare_canvas)[i]
                canvas.set_photo(population[indices[i] + direction]); self.request_image(canvas, "preview")
            self.update_preview_info(); return
        row = self.items.currentRow()
        if row < 0: return
        target = max(0, min(self.items.count()-1, row+direction))
        self.items.setCurrentRow(target); self.items.scrollToItem(self.items.item(target))

    def select_compare_side(self, index):
        canvas = (self.preview, self.compare_canvas)[index]
        if self.compare_mode and canvas.path: self.activate_preview(canvas)

    def refresh_statuses(self):
        self.items.blockSignals(True)
        for index in range(self.items.count()):
            item = self.items.item(index); path = item.data(Qt.ItemDataRole.UserRole)
            item.setData(Qt.ItemDataRole.UserRole+2, self.window.decision_description(path))
            item.setData(Qt.ItemDataRole.UserRole+3, self.window.state.get("decisions", {}).get(path, {}).get("action", ""))
            item.setToolTip(self.window.tooltip_for(path))
        self.items.blockSignals(False); self.items.viewport().update(); self.update_preview_info()

    def mark_one(self, path, action, destination="", advance=False):
        if not path or self.running: return
        old = self.window.state.get("decisions", {}).get(path, {})
        if action == "trash" and old.get("action") in ("file", "hold"):
            if QMessageBox.question(self, "候補の変更", "この写真の振り分け・保留を削除候補に変更しますか？") != QMessageBox.StandardButton.Yes: return
        if self.window.make_decisions([path], action, destination):
            self.window.rebuild_views(); self.refresh_statuses()
            self.status.setText("候補を指定しました。整理を反映するまで元の写真は変更されません。")
            if advance: self.navigate_preview(1)

    def clear_one(self, path):
        if path and not self.running:
            self.window.clear_decisions([path]); self.refresh_statuses()
            self.status.setText("候補を解除しました。元の写真は変更されていません。")

    def assign_active(self, slot):
        destinations = self.window.state.get("destinations", [])
        if slot < len(destinations) and self.active_canvas:
            self.mark_one(self.active_canvas.path, "file", destinations[slot]["path"], advance=True)

    def undo(self):
        if self.running: return
        if not self.window.undo_stack:
            self.status.setText("取り消す操作はありません。")
            return
        self.window.undo(); self.refresh_statuses(); self.status.setText("直前の候補指定を取り消しました。")

    def show_properties(self):
        if self.active_canvas and self.active_canvas.path: PropertyDialog(self.active_canvas.path, self).exec()

    def bind_shortcuts(self):
        def bind(key, callback, target=None):
            shortcut = QShortcut(QKeySequence(key), target or self)
            if target: shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback); self.shortcuts.append(shortcut)
        for key, callback in (("F11", self.toggle_dialog_fullscreen), ("Ctrl+F", self.toggle_photo_fullscreen),
                              ("Ctrl+Z", self.undo), ("Alt+Enter", self.show_properties)):
            bind(key, callback)
        for canvas in (self.preview, self.compare_canvas):
            for key, callback in (("Left", lambda: self.navigate_preview(-1)), ("Right", lambda: self.navigate_preview(1)),
                                  ("Shift+Left", lambda: self.navigate_independent(-1)), ("Shift+Right", lambda: self.navigate_independent(1)),
                                  ("X", lambda: self.mark_one(self.active_canvas.path, "trash", advance=True)),
                                  ("Delete", lambda: self.mark_one(self.active_canvas.path, "trash", advance=True)),
                                  ("H", lambda: self.mark_one(self.active_canvas.path, "hold", advance=True)),
                                  ("0", lambda: self.clear_one(self.active_canvas.path)),
                                  ("Z", self.toggle_actual), ("Ctrl+Up", lambda: self.zoom_active(1.25)),
                                  ("Ctrl++", lambda: self.zoom_active(1.25)), ("Ctrl+Shift++", lambda: self.zoom_active(1.25)),
                                  ("Ctrl+=", lambda: self.zoom_active(1.25)), ("Ctrl+-", lambda: self.zoom_active(.8)),
                                  ("Ctrl+Down", lambda: self.zoom_active(.8)),
                                  ("Ctrl+Shift+Left", lambda: self.pan_active(-1, 0)), ("Ctrl+Shift+Right", lambda: self.pan_active(1, 0)),
                                  ("Ctrl+Shift+Up", lambda: self.pan_active(0, -1)), ("Ctrl+Shift+Down", lambda: self.pan_active(0, 1)),
                                  ("Ctrl+Left", lambda: self.select_compare_side(0)),
                                  ("Ctrl+Right", lambda: self.select_compare_side(1))):
                bind(key, callback, canvas)
            for slot, destination in enumerate(self.window.state.get("destinations", [])):
                if destination.get("key"): bind(destination["key"], lambda n=slot: self.assign_active(n), canvas)

    def navigate_independent(self, direction):
        if not self.compare_mode: self.navigate_preview(direction); return
        canvas = self.active_canvas
        row = self.group_list.currentRow()
        group = self.groups[row] if 0 <= row < len(self.groups) else []
        population = group if canvas.path in group else self.paths
        if canvas.path not in population: return
        index = population.index(canvas.path) + direction
        if 0 <= index < len(population):
            canvas.set_photo(population[index]); self.request_image(canvas, "preview"); self.update_preview_info()

    def toggle_dialog_fullscreen(self):
        if self.photo_only: self.exit_photo_fullscreen()
        if self.dialog_fullscreen:
            self.dialog_fullscreen = False; self.showNormal(); self.fullscreen_button.setText("全画面  F11")
        else:
            self.dialog_fullscreen = True; self.showFullScreen(); self.fullscreen_button.setText("全画面を終了  F11")

    def toggle_photo_fullscreen(self):
        if self.photo_only: self.exit_photo_fullscreen(); return
        if not self.active_canvas or not self.active_canvas.path: return
        self.return_fullscreen = self.dialog_fullscreen; self.photo_only = True
        for widget in (self.title_label, self.fullscreen_button, self.note, self.filter_widget, self.status,
                       self.progress, self.left_panel, self.center_panel, self.preview_info, self.zoom_controls,
                       self.preview_hint, self.actions_widget, self.actions2_widget): widget.hide()
        self.layout().setContentsMargins(0, 0, 0, 0); self.showFullScreen()
        self.exit_photo_button.show(); self.position_exit_button(); self.update_preview_info()

    def exit_photo_fullscreen(self):
        if not self.photo_only: return
        self.photo_only = False; self.exit_photo_button.hide(); self.layout().setContentsMargins(9, 9, 9, 9)
        for widget in (self.title_label, self.fullscreen_button, self.note, self.filter_widget, self.status,
                       self.left_panel, self.center_panel, self.preview_info, self.zoom_controls,
                       self.preview_hint, self.actions_widget, self.actions2_widget): widget.show()
        self.progress.setVisible(self.running)
        if self.return_fullscreen: self.showFullScreen()
        else: self.showNormal()
        self.mode_changed(); self.update_preview_info()

    def position_exit_button(self):
        self.exit_photo_button.adjustSize()
        self.exit_photo_button.move(max(4, self.preview.width()-self.exit_photo_button.width()-12), 12)
        self.exit_photo_button.raise_()

    def eventFilter(self, obj, event):
        if obj is self.preview and event.type() == QEvent.Type.Resize and self.photo_only:
            QTimer.singleShot(0, self.position_exit_button)
        return super().eventFilter(obj, event)

    def reject(self):
        if self.photo_only: self.exit_photo_fullscreen()
        elif self.dialog_fullscreen: self.toggle_dialog_fullscreen()
        else: super().reject()

    def selected_paths(self):
        # Keep the order of the current photo list when comparing two frames.
        return [p for p in self.paths if p in self.checked]

    def open_photo(self):
        path = self.active_canvas.path if self.active_canvas else ""
        if path: self.chosen_paths = [path]; self.accept()

    def compare_photos(self):
        if self.compare_mode:
            self.exit_compare(); return
        chosen = self.selected_paths()
        if len(chosen) != 2: return
        self.compare_mode = True; self.active_canvas = self.preview
        self.preview.set_photo(chosen[0]); self.compare_canvas.set_photo(chosen[1])
        self.compare_canvas.show(); self.compare_button.setText("比較を終了")
        self.request_image(self.preview, "preview"); self.request_image(self.compare_canvas, "preview")
        self.update_preview_info(); self.update_actions()

    def exit_compare(self):
        if not self.compare_mode: return
        self.compare_mode = False; self.compare_canvas.hide(); self.compare_canvas.set_photo("")
        self.compare_button.setText("チェック2枚を比較"); self.active_canvas = self.preview
        item = self.items.currentItem()
        if item:
            self.preview.set_photo(item.data(Qt.ItemDataRole.UserRole)); self.request_image(self.preview, "preview")
        self.update_preview_info(); self.update_actions()

    def clear_checks(self):
        self.checked.clear(); self.items.anchor_path = ""; self.items.blockSignals(True)
        for i in range(self.items.count()): self.items.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.items.blockSignals(False); self.update_actions()

    def refresh_group(self):
        row = self.group_list.currentRow(); self.group_selected(row)

    def clear_candidates(self):
        paths = self.selected_paths()
        count = self.window.clear_decisions(paths)
        self.checked.clear(); self.refresh_group()
        self.status.setText(f"{count:,} 枚の候補を解除しました。元の写真は変更されていません。")

    def assign_checked(self, slot):
        paths = self.selected_paths(); destinations = self.window.state.get("destinations", [])
        if not paths or slot >= len(destinations): return
        destination = destinations[slot]; decisions = self.window.state.get("decisions", {})
        changed = sum(bool(decisions.get(p)) and (decisions[p].get("action") != "file" or decisions[p].get("destination") != destination["path"]) for p in paths)
        if changed and QMessageBox.question(self, "候補の変更", f"{changed} 枚の既存の候補を変更しますか？") != QMessageBox.StandardButton.Yes: return
        if self.window.make_decisions(paths, "file", destination["path"]):
            count = len(paths); self.checked.clear(); self.window.rebuild_views(); self.refresh_group()
            self.status.setText(f"{count:,} 枚を［{destination['label']}］へ振り分け予定にしました。")

    def mark_trash(self):
        paths = self.selected_paths()
        if not paths: return
        decisions = self.window.state.get("decisions", {})
        overwritten = sum(decisions.get(p, {}).get("action") in ("file", "hold") for p in paths)
        if overwritten:
            choice = QMessageBox.question(self, "候補の変更", f"{overwritten} 枚には振り分け・保留が指定されています。削除候補に変更しますか？")
            if choice != QMessageBox.StandardButton.Yes: return
        if self.window.make_decisions(paths, "trash"):
            count = len(paths); self.checked.clear(); self.window.rebuild_views(); self.refresh_group()
            self.status.setText(f"{count:,} 枚を削除候補にしました。実際の削除は整理を反映するまで行われません。")

    def done(self, result):
        self.closed = True; self.cancel_event.set(); self.thumb_generation += 1
        super().done(result)
