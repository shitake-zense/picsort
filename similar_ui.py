"""Manual review of local similar-photo suggestions."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QStyledItemDelegate, QVBoxLayout, QWidget)

from similar import find_similar


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
        self.setWindowTitle("似た写真を探す")
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            self.resize(min(1050, available.width()-30), min(720, available.height()-30))
        else:
            self.resize(960, 650)
        layout = QVBoxLayout(self)
        title = QLabel("似た写真を探す")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        layout.addWidget(title)
        note = QLabel("表示中の写真を小さくして構図を比較します。候補を確認し、残す・削除する写真は自分で選びます。")
        note.setWordWrap(True); layout.addWidget(note)
        filters = QHBoxLayout()
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
        filters.addStretch(); layout.addLayout(filters)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.status = QLabel(f"表示中の {len(self.paths):,} 枚が対象です。条件を選んで［探す］を押してください。")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.progress = QProgressBar(); self.progress.setRange(0, len(self.paths)); self.progress.hide()
        self.progress.setStyleSheet("QProgressBar {background: #232932; border: 1px solid #596675; border-radius: 5px; text-align: center;} QProgressBar::chunk {background: #668e70; border-radius: 4px;}")
        layout.addWidget(self.progress)
        main = QHBoxLayout()
        left = QVBoxLayout(); left.addWidget(QLabel("候補グループ"))
        self.group_list = QListWidget(); self.group_list.setMinimumWidth(100); self.group_list.setMaximumWidth(190)
        self.group_list.currentRowChanged.connect(self.group_selected); left.addWidget(self.group_list, 1); main.addLayout(left, 1)
        center = QVBoxLayout(); self.count_label = QLabel("写真をクリックしてチェック")
        center.addWidget(self.count_label)
        self.items = SimilarCards(); self.items.setViewMode(QListView.ViewMode.IconMode)
        self.items.setResizeMode(QListView.ResizeMode.Adjust); self.items.setMovement(QListView.Movement.Static)
        self.items.setIconSize(QSize(132, 80)); self.items.setGridSize(QSize(150, 135))
        self.items.setSpacing(2); self.items.setUniformItemSizes(True)
        self.items.setItemDelegate(SimilarCardDelegate(self.items))
        self.items.itemChanged.connect(self.item_checked)
        self.items.currentItemChanged.connect(self.preview_selected)
        center.addWidget(self.items, 1); main.addLayout(center, 3)
        right = QVBoxLayout(); self.preview_info = QLabel("写真を選択してください")
        self.preview_info.setWordWrap(True); self.preview_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.preview_info.setMaximumHeight(60); right.addWidget(self.preview_info)
        self.preview = QLabel("写真を選択してください")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview.setMinimumSize(100, 100)
        self.preview.setStyleSheet("background: #101318; border: 1px solid #39424f; color: #adb9c1;")
        right.addWidget(self.preview, 1); main.addLayout(right, 2)
        layout.addLayout(main, 1)
        actions = QHBoxLayout()
        self.open_button = QPushButton("選んだ写真を表示"); self.open_button.clicked.connect(self.open_photo)
        self.compare_button = QPushButton("チェック2枚を比較"); self.compare_button.clicked.connect(self.compare_photos)
        self.clear_checks_button = QPushButton("チェックを解除"); self.clear_checks_button.clicked.connect(self.clear_checks)
        actions.addWidget(self.open_button); actions.addWidget(self.compare_button); actions.addWidget(self.clear_checks_button)
        actions.addStretch(); layout.addLayout(actions)
        actions2 = QHBoxLayout()
        self.clear_button = QPushButton("チェックした候補を解除"); self.clear_button.clicked.connect(self.clear_candidates)
        self.trash_button = QPushButton("チェックした写真を削除候補に")
        self.trash_button.setObjectName("danger"); self.trash_button.clicked.connect(self.mark_trash)
        actions2.addWidget(self.clear_button); actions2.addWidget(self.trash_button); actions2.addStretch()
        close = QPushButton("閉じる"); close.clicked.connect(self.reject); actions2.addWidget(close)
        layout.addLayout(actions2)
        self.preview_image = None
        self.update_actions()

    def mode_changed(self):
        self.seconds.setVisible(self.mode.currentData() == "time")
        self.seconds.setEnabled(self.mode.currentData() == "time" and not self.running)

    def start_search(self):
        if self.running: return
        self.cancel_event = threading.Event(); self.running = True
        self.groups = []; self.checked.clear(); self.group_list.clear(); self.items.clear()
        self.preview_image = None; self.preview.setText("写真を選択してください")
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
        self.thumb_generation += 1; generation = self.thumb_generation
        self.items.blockSignals(True); self.items.clear(); self.items.anchor_path = ""
        if row < 0 or row >= len(self.groups):
            self.items.blockSignals(False); self.update_actions(); return
        for path in self.groups[row]:
            item = QListWidgetItem(Path(path).name); item.setData(Qt.ItemDataRole.UserRole, path)
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
        self.compare_button.setEnabled(enabled and n == 2)
        self.clear_checks_button.setEnabled(enabled and n > 0)
        self.clear_button.setEnabled(enabled and n > 0)
        self.trash_button.setEnabled(enabled and n > 0)

    def preview_selected(self, item, _previous):
        if not item:
            self.preview_image = None; self.preview.setText("写真を選択してください"); self.update_actions(); return
        path = item.data(Qt.ItemDataRole.UserRole)
        self.preview_info.setText(Path(path).name + self.window.decision_suffix(path)); self.preview_info.setToolTip(self.window.tooltip_for(path))
        reader = QImageReader(path); reader.setAutoTransform(True); size = reader.size()
        if size.isValid(): reader.setScaledSize(size.scaled(1200, 1200, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read(); self.preview_image = image if not image.isNull() else None
        if self.preview_image is None: self.preview.setText("写真を表示できません。")
        self.update_preview(); self.update_actions()

    def update_preview(self):
        if self.preview_image is not None:
            pix = QPixmap.fromImage(self.preview_image)
            self.preview.setPixmap(pix.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "preview_image"): self.update_preview()

    def selected_paths(self):
        # Keep the order of the current photo list when comparing two frames.
        return [p for p in self.paths if p in self.checked]

    def open_photo(self):
        item = self.items.currentItem()
        if item: self.chosen_paths = [item.data(Qt.ItemDataRole.UserRole)]; self.accept()

    def compare_photos(self):
        chosen = self.selected_paths()
        if len(chosen) == 2: self.chosen_paths = chosen; self.accept()

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
