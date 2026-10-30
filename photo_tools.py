"""Local photo properties, non-destructive crop, and a compact visual guide."""
from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QVBoxLayout, QWidget)

from core import choose_target, fingerprint, matches
from layout_settings import read_settings, write_setting


def photo_properties(path):
    path = Path(path)
    stat = path.stat()
    values = [("ファイル名", path.name), ("保存場所", str(path.parent)),
              ("ファイルサイズ", f"{stat.st_size / 1024 / 1024:.2f} MB"),
              ("更新日時", datetime.fromtimestamp(stat.st_mtime).strftime("%Y/%m/%d %H:%M:%S"))]
    with Image.open(path) as img:
        exif = img.getexif()
        orientation = exif.get(274, 1)
        w, h = img.size
        if orientation in (5, 6, 7, 8): w, h = h, w
        values.insert(2, ("画像サイズ", f"{w:,} × {h:,} px"))
        values.insert(3, ("形式", img.format or path.suffix.upper().lstrip(".")))
        try:
            detail = exif.get_ifd(34665)
        except (AttributeError, KeyError, TypeError, ValueError, OSError):
            detail = {}
        def get(tag):
            return detail.get(tag) or exif.get(tag)
        def value(v):
            if isinstance(v, bytes): return v.decode("utf-8", "replace").strip("\x00")
            return str(v).strip("\x00")
        shot = get(36867) or get(36868) or exif.get(306)
        camera = " ".join(str(x).strip() for x in (exif.get(271), exif.get(272)) if x)
        if shot: values.append(("撮影日時", value(shot)))
        if camera: values.append(("カメラ", camera))
        if get(42036): values.append(("レンズ", value(get(42036))))
        if get(33437): values.append(("絞り", f"F{float(get(33437)):g}"))
        if get(33434):
            t = float(get(33434))
            values.append(("シャッター速度", f"1/{round(1 / t)} 秒" if 0 < t < 1 else f"{t:g} 秒"))
        if get(34855): values.append(("ISO", value(get(34855))))
        if get(37386): values.append(("焦点距離", f"{float(get(37386)):g} mm"))
    return values


def _screen_dialog(dialog, width, height):
    screen = dialog.screen() or QApplication.primaryScreen()
    if screen:
        rect = screen.availableGeometry()
        width, height = min(width, rect.width() - 30), min(height, rect.height() - 30)
    dialog.resize(max(300, width), max(250, height))


class PropertyDialog(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("写真のプロパティ")
        _screen_dialog(self, 650, 560)
        layout = QVBoxLayout(self)
        title = QLabel("写真の情報")
        title.setStyleSheet("font-size: 19px; font-weight: 700;")
        layout.addWidget(title)
        area = QScrollArea(); area.setWidgetResizable(True)
        body = QWidget(); body.setObjectName("dialogBody")
        body.setStyleSheet("QWidget#dialogBody { background: #15191d; }")
        form = QFormLayout(body)
        form.setSpacing(12); form.setContentsMargins(15, 15, 15, 15)
        try:
            properties = photo_properties(path)
        except (OSError, ValueError, TypeError, OverflowError) as error:
            properties = [("読み込みエラー", str(error))]
        for name, value in properties:
            widget = QLabel(value); widget.setWordWrap(True)
            widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(name, widget)
        note = QLabel("撮影情報は写真に記録されている項目のみ表示します。")
        note.setWordWrap(True); form.addRow(note)
        area.setWidget(body); layout.addWidget(area, 1)
        box = QDialogButtonBox()
        box.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        box.rejected.connect(self.reject); layout.addWidget(box)


class CropCanvas(QWidget):
    selection_changed = Signal()
    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.image = image; self.start = None; self.selection = QRectF(); self.ratio = 0
        self.zoom_scale = 1.0; self.offset = QPointF(); self.grid_enabled = True
        self.move_start = None; self.move_selection = QRectF()
        self.pan_start = None; self.pan_offset = QPointF()
        self.setMinimumSize(160, 130)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def image_rect(self):
        area = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        scale = min(area.width() / self.image.width(), area.height() / self.image.height()) * self.zoom_scale
        w, h = self.image.width() * scale, self.image.height() * scale
        return QRectF(area.center().x() - w / 2 + self.offset.x(),
                      area.center().y() - h / 2 + self.offset.y(), w, h)

    def image_point(self, point):
        rect = self.image_rect()
        x = min(max(point.x(), rect.left()), rect.right())
        y = min(max(point.y(), rect.top()), rect.bottom())
        return QPointF((x - rect.left()) * self.image.width() / rect.width(),
                       (y - rect.top()) * self.image.height() / rect.height())

    def mousePressEvent(self, event):
        if not self.image_rect().contains(event.position()): return
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = self.image_point(event.position()); self.selection = QRectF(); self.update()
            self.selection_changed.emit()
        elif event.button() == Qt.MouseButton.RightButton and not self.selection.isEmpty():
            self.move_start = self.image_point(event.position()); self.move_selection = QRectF(self.selection)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        elif event.button() == Qt.MouseButton.MiddleButton and self.zoom_scale > 1:
            self.pan_start = QPointF(event.position()); self.pan_offset = QPointF(self.offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.pan_start is not None:
            self.offset = self.pan_offset + event.position() - self.pan_start
            self.clamp_offset(); self.update(); return
        if self.move_start is not None:
            delta = self.image_point(event.position()) - self.move_start
            rect = QRectF(self.move_selection)
            x = max(0.0, min(self.image.width() - rect.width(), rect.left() + delta.x()))
            y = max(0.0, min(self.image.height() - rect.height(), rect.top() + delta.y()))
            rect.moveTopLeft(QPointF(x, y)); self.selection = rect
            self.update(); self.selection_changed.emit(); return
        if self.start is None: return
        end = self.image_point(event.position())
        dx, dy = end.x() - self.start.x(), end.y() - self.start.y()
        if self.ratio and dx and dy:
            width = min(abs(dx), abs(dy) * self.ratio)
            height = width / self.ratio
            dx = math.copysign(width, dx); dy = math.copysign(height, dy)
        self.selection = QRectF(self.start, self.start + QPointF(dx, dy)).normalized()
        self.update(); self.selection_changed.emit()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.start is not None:
            self.mouseMoveEvent(event); self.start = None
        elif event.button() == Qt.MouseButton.RightButton and self.move_start is not None:
            self.mouseMoveEvent(event); self.move_start = None; self.setCursor(Qt.CursorShape.CrossCursor)
        elif event.button() == Qt.MouseButton.MiddleButton and self.pan_start is not None:
            self.mouseMoveEvent(event); self.pan_start = None; self.setCursor(Qt.CursorShape.CrossCursor)

    def clamp_offset(self):
        area = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        image = self.image_rect()
        max_x = max(0.0, (image.width() - area.width()) / 2)
        max_y = max(0.0, (image.height() - area.height()) / 2)
        self.offset = QPointF(max(-max_x, min(max_x, self.offset.x())),
                              max(-max_y, min(max_y, self.offset.y())))

    def zoom(self, factor, position=None):
        old = self.image_rect()
        position = position if position is not None else QRectF(self.rect()).center()
        if old.isEmpty(): return
        x = (position.x() - old.left()) / old.width()
        y = (position.y() - old.top()) / old.height()
        self.zoom_scale = max(1.0, min(12.0, self.zoom_scale * factor))
        if self.zoom_scale <= 1.0001:
            self.zoom_scale = 1.0; self.offset = QPointF()
        else:
            current = self.image_rect()
            self.offset += QPointF(position.x() - (current.left() + x * current.width()),
                                   position.y() - (current.top() + y * current.height()))
            self.clamp_offset()
        self.update()

    def fit_image(self):
        self.zoom_scale = 1.0; self.offset = QPointF(); self.update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.zoom(1.25 ** (delta / 120.0 if event.angleDelta().y() else delta / 100.0), event.position())
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.clamp_offset()

    def selected_pixels(self):
        if self.selection.isEmpty(): return None
        left = max(0, math.floor(self.selection.left())); top = max(0, math.floor(self.selection.top()))
        right = min(self.image.width(), math.ceil(self.selection.right()))
        bottom = min(self.image.height(), math.ceil(self.selection.bottom()))
        return (left, top, right, bottom) if right > left and bottom > top else None

    def paintEvent(self, event):
        painter = QPainter(self); painter.fillRect(self.rect(), QColor("#0d1014"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.image_rect(); painter.drawImage(rect, self.image)
        if not self.selection.isEmpty():
            sx, sy = rect.width() / self.image.width(), rect.height() / self.image.height()
            selected = QRectF(rect.left() + self.selection.left() * sx, rect.top() + self.selection.top() * sy,
                              self.selection.width() * sx, self.selection.height() * sy)
            painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), max(0, selected.top()-rect.top())), QColor(0,0,0,150))
            painter.fillRect(QRectF(rect.left(), selected.bottom(), rect.width(), max(0, rect.bottom()-selected.bottom())), QColor(0,0,0,150))
            painter.fillRect(QRectF(rect.left(), selected.top(), max(0, selected.left()-rect.left()), selected.height()), QColor(0,0,0,150))
            painter.fillRect(QRectF(selected.right(), selected.top(), max(0, rect.right()-selected.right()), selected.height()), QColor(0,0,0,150))
            if self.grid_enabled:
                pen = QPen(QColor(255, 255, 255, 115), 1); pen.setCosmetic(True); painter.setPen(pen)
                for fraction in (1/3, 2/3):
                    x = selected.left() + selected.width() * fraction
                    y = selected.top() + selected.height() * fraction
                    painter.drawLine(QPointF(x, selected.top()), QPointF(x, selected.bottom()))
                    painter.drawLine(QPointF(selected.left(), y), QPointF(selected.right(), y))
            painter.setPen(QPen(QColor("#d7ffe1"), 2)); painter.setBrush(Qt.BrushStyle.NoBrush); painter.drawRect(selected)
        painter.end()


def save_crop(source, selection, expected):
    source = Path(source)
    if not matches(source, expected):
        raise OSError("元の写真が変更されたか見つかりません。開き直してください。")
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        image.load()
        if not matches(source, expected):
            raise OSError("読み込み中に元の写真が変更されました。開き直してください。")
        left, top, right, bottom = selection
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ValueError("切り抜き範囲を確認してください。")
        result = image.crop(selection)
        options = {}
        exif = image.getexif()
        if exif:
            exif[256], exif[257] = result.size
            try:
                sub = exif.get_ifd(34665)
                if 40962 in sub: sub[40962] = result.width
                if 40963 in sub: sub[40963] = result.height
            except (AttributeError, KeyError, TypeError, ValueError): pass
            options["exif"] = exif.tobytes()
        if image.info.get("icc_profile"): options["icc_profile"] = image.info["icc_profile"]
        fmt = "PNG" if source.suffix.lower() == ".png" else "JPEG"
        if fmt == "JPEG":
            if result.mode not in ("RGB", "L"): result = result.convert("RGB")
            options.update(quality=95, subsampling=0)
        requested = source.with_name(source.stem + "_切り抜き" + (".png" if fmt == "PNG" else ".jpg"))
        target, stream = choose_target(requested, source.parent)
        try:
            with stream:
                result.save(stream, format=fmt, **options)
                stream.flush(); os.fsync(stream.fileno())
        except BaseException:
            if not stream.closed: stream.close()
            target.unlink(missing_ok=True)
            raise
    return str(target)


class CropDialog(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path; self.saved_path = ""
        self.setWindowTitle("写真を切り抜く")
        _screen_dialog(self, 980, 720)
        layout = QVBoxLayout(self)
        header = QLabel("残したい範囲をドラッグして選択")
        header.setStyleSheet("font-size: 18px; font-weight: 700;"); layout.addWidget(header)
        instruction = QLabel("左ドラッグ：範囲を選択 / 右ドラッグ：範囲を移動 / ホイール：拡大 / 押し込みドラッグ：表示位置を移動")
        instruction.setWordWrap(True); layout.addWidget(instruction)
        self.expected = None
        try:
            self.expected = fingerprint(Path(path))
            reader = QImageReader(path); reader.setAutoTransform(True); image = reader.read()
            if image.isNull(): raise OSError(reader.errorString())
            if not matches(Path(path), self.expected): raise OSError("読み込み中に元の写真が変更されました。")
        except (OSError, ValueError) as error:
            layout.addWidget(QLabel("写真を読み込めませんでした：" + str(error)))
            box = QDialogButtonBox(); box.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
            box.rejected.connect(self.reject); layout.addWidget(box)
            return
        row = QHBoxLayout(); row.addWidget(QLabel("比率"))
        self.ratio = QComboBox()
        for title, ratio in (("自由",0), ("1 : 1",1), ("4 : 3",4/3), ("3 : 2",3/2), ("16 : 9",16/9)):
            self.ratio.addItem(title, ratio)
        row.addWidget(self.ratio); self.size_label = QLabel("範囲を選択してください")
        row.addWidget(self.size_label); row.addStretch()
        self.zoom_out = QPushButton("−"); self.zoom_in = QPushButton("＋"); self.fit_button = QPushButton("全体")
        for button in (self.zoom_out, self.zoom_in, self.fit_button):
            button.setStyleSheet("padding: 5px 9px;"); row.addWidget(button)
        self.grid_button = QPushButton("グリッド ON"); self.grid_button.setCheckable(True)
        self.grid_button.setToolTip("選択範囲に三分割のガイド線を表示")
        row.addWidget(self.grid_button); layout.addLayout(row)
        self.canvas = CropCanvas(image); layout.addWidget(self.canvas, 1)
        self.data_dir = getattr(parent, "data_dir", None)
        grid_enabled = read_settings(self.data_dir).get("crop_grid", True) if self.data_dir else True
        self.grid_button.setChecked(grid_enabled if isinstance(grid_enabled, bool) else True)
        self.set_grid(self.grid_button.isChecked(), save=False)
        self.grid_button.toggled.connect(self.set_grid)
        self.zoom_out.clicked.connect(lambda: self.canvas.zoom(.8))
        self.zoom_in.clicked.connect(lambda: self.canvas.zoom(1.25))
        self.fit_button.clicked.connect(self.canvas.fit_image)
        self.zoom_shortcuts = []
        for sequence, factor in (("Ctrl+Up", 1.25), ("Ctrl+Down", .8), ("Ctrl++", 1.25), ("Ctrl+-", .8)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(lambda n=factor: self.canvas.zoom(n)); self.zoom_shortcuts.append(shortcut)
        self.canvas.selection_changed.connect(self.update_selection)
        self.ratio.currentIndexChanged.connect(self.change_ratio)
        note = QLabel("JPEGは再圧縮されます。一部の付加情報は引き継がれない場合があります。")
        note.setWordWrap(True); layout.addWidget(note)
        actions = QDialogButtonBox()
        actions.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        self.save_button = actions.addButton("別ファイルで保存", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setEnabled(False); actions.rejected.connect(self.reject); actions.accepted.connect(self.save)
        layout.addWidget(actions)

    def set_grid(self, enabled, save=True):
        self.canvas.grid_enabled = enabled
        self.grid_button.setText("グリッド ON" if enabled else "グリッド OFF")
        self.canvas.update()
        if save and self.data_dir: write_setting(self.data_dir, "crop_grid", bool(enabled))

    def change_ratio(self):
        self.canvas.ratio = self.ratio.currentData(); self.canvas.selection = QRectF()
        self.canvas.update(); self.update_selection()

    def update_selection(self):
        selection = self.canvas.selected_pixels(); self.save_button.setEnabled(bool(selection))
        self.size_label.setText(f"{selection[2]-selection[0]:,} × {selection[3]-selection[1]:,} px" if selection else "範囲を選択してください")

    def save(self):
        selection = self.canvas.selected_pixels()
        if not selection: return
        try: self.saved_path = save_crop(self.path, selection, self.expected)
        except (OSError, ValueError, TypeError) as error:
            QMessageBox.warning(self, "保存できません", str(error)); return
        self.accept()


HELP = [
 ("はじめに", [("1  フォルダを開く", "右上から写真フォルダを選びます。左の一覧からサブフォルダへ移動できます。"),
 ("2  行き先を作る", "左のフォルダ一覧から追加するか、新しいフォルダを作成します。キーは各行の設定から変更できます。"),
 ("3  写真を選ぶ", "行き先のキー、または削除候補を指定します。指定しただけでは元の写真は変更されません。"),
 ("4  整理を反映", "下のボタンで一覧を確認し、実行します。削除候補はごみ箱へ移動します。"),
 ("更新  /  F5", "外部で追加・変更した写真やフォルダを読み直します。"),
 ("Ctrl+O / Ctrl+N", "写真フォルダを開く / 現在のフォルダに新しいフォルダを作ります。"),
 ("Alt+↑", "親フォルダに戻ります。"), ("Alt+Shift+↑", "親フォルダを振り分け先に追加します。")]),
 ("写真を見る", [("左クリック / ←", "前の写真へ。長押しすると連続で送ります。"), ("右クリック / →", "次の写真へ。"),
 ("ホイール", "写真を拡大・縮小します。拡大中は左ドラッグで位置を動かせます。"),
 ("ホイール押込 / Ctrl+F", "写真だけを全画面にします。Escで戻ります。"),
 ("原寸で確認 / Z", "ピントを原寸で確認します。もう一度押すと全体表示に戻ります。"),
 ("写真番号 → Enter", "入力した番号の写真に移動します。番号は現在の一覧内です。"),
 ("Home", "一覧の最初の写真へ戻ります。"),
 ("Ctrl＋↑ / ↓", "キーボードで拡大・縮小します。Ctrl＋＋ / －も使えます。"),
 ("Ctrl+Shift＋矢印", "拡大中の表示位置を移動します。"),
 ("プロパティ / Alt+Enter", "画像サイズ、撮影日時、カメラ設定など、記録されている情報を表示します。"),
 ("切り抜き / Ctrl+Shift+X", "左ドラッグで選択、右ドラッグで範囲を移動。ホイールで拡大し、押し込みドラッグで表示位置を移動します。グリッドは切り替えられます。元画像を残して別ファイルに保存します。"),
 ("似た写真を探す", "撮影時刻と構図、または構図だけで候補を探します。Shift＋右クリックで次のグループに進みます。削除の判断は自分で行います。"),
 ("画面幅の調整", "画面の縦の境界線をドラッグすると、一覧やプレビューなどの幅を変えられます。")]),
 ("整理する", [("行き先のキー", "写真を指定したフォルダへ振り分ける候補にして次へ進みます。"),
 ("Shift＋左クリック / X", "削除候補にして次へ進みます。"), ("Ctrl＋左クリック / 0", "その写真の削除・振り分け候補を解除します。"),
 ("H", "保留にして次へ進みます。"), ("Ctrl+Z / 取り消す", "直前の候補指定を戻します。"),
 ("チェック一覧", "複数枚をチェックしてまとめて指定・解除します。Shift＋クリックで範囲を選べます。"),
 ("一覧で Space / Ctrl+A", "キーボードでチェック / 表示中の写真をすべてチェック。Ctrl+Shift+Aで解除します。"),
 ("整理を反映 / Ctrl+Enter", "削除・行き先ごとのタブで確認します。クリックすると候補を解除できます。")]),
 ("2枚比較・その他", [("Alt+1 / Alt+2 / Alt+3", "1枚表示・2枚比較・チェック一覧を切り替えます。"),
 ("Shift＋左右矢印", "2枚比較で、操作中の側だけを移動します。"), ("Ctrl＋左右矢印", "2枚比較で、操作する側を切り替えます。"),
 ("左を固定", "左の写真を残したまま右側を送ります。"), ("F11 / Esc", "アプリ全体の全画面を切り替えます。"),
 ("ローカル保存", "作業状況はこのパソコンに自動保存されます。アプリは外部通信しません。")])]


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent); self.setWindowTitle("ピクソート 操作ガイド"); _screen_dialog(self, 760, 650)
        layout = QVBoxLayout(self)
        title = QLabel("操作ガイド"); title.setStyleSheet("font-size: 20px; font-weight: 700;"); layout.addWidget(title)
        intro = QLabel("目的に合わせてタブを選んでください。")
        layout.addWidget(intro)
        tabs = QTabWidget()
        for name, rows in HELP:
            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            body = QWidget(); body.setObjectName("guideBody")
            body.setStyleSheet("QWidget#guideBody { background: #15191d; }")
            entries = QVBoxLayout(body); entries.setContentsMargins(18,18,18,18); entries.setSpacing(14)
            for key, description in rows:
                key_label = QLabel(key); key_label.setStyleSheet("font-weight: 700; color: #c8f2d1;")
                text = QLabel(description); text.setWordWrap(True)
                text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                entries.addWidget(key_label); entries.addWidget(text)
            entries.addStretch()
            scroll.setWidget(body); tabs.addTab(scroll, name)
        layout.addWidget(tabs, 1)
        box = QDialogButtonBox(); box.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        box.rejected.connect(self.reject); layout.addWidget(box)
