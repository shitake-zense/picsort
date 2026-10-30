"""Local photo properties, non-destructive crop, and a compact visual guide."""
from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QVBoxLayout, QWidget)

from core import choose_target, fingerprint, matches


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
        self.setMinimumSize(160, 130)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def image_rect(self):
        area = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        scale = min(area.width() / self.image.width(), area.height() / self.image.height())
        w, h = self.image.width() * scale, self.image.height() * scale
        return QRectF(area.center().x() - w / 2, area.center().y() - h / 2, w, h)

    def image_point(self, point):
        rect = self.image_rect()
        x = min(max(point.x(), rect.left()), rect.right())
        y = min(max(point.y(), rect.top()), rect.bottom())
        return QPointF((x - rect.left()) * self.image.width() / rect.width(),
                       (y - rect.top()) * self.image.height() / rect.height())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.image_rect().contains(event.position()):
            self.start = self.image_point(event.position()); self.selection = QRectF(); self.update()
            self.selection_changed.emit()

    def mouseMoveEvent(self, event):
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
        instruction = QLabel("元の写真はそのまま残し、同じフォルダに別ファイルを保存します。選び直すときはもう一度ドラッグ。")
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
        row.addWidget(self.size_label); row.addStretch(); layout.addLayout(row)
        self.canvas = CropCanvas(image); layout.addWidget(self.canvas, 1)
        self.canvas.selection_changed.connect(self.update_selection)
        self.ratio.currentIndexChanged.connect(self.change_ratio)
        note = QLabel("JPEGは再圧縮されます。一部の付加情報は引き継がれない場合があります。")
        note.setWordWrap(True); layout.addWidget(note)
        actions = QDialogButtonBox()
        actions.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        self.save_button = actions.addButton("別ファイルで保存", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setEnabled(False); actions.rejected.connect(self.reject); actions.accepted.connect(self.save)
        layout.addWidget(actions)

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
 ("切り抜き / Ctrl+Shift+X", "ドラッグで範囲を選び、元画像を残したまま別ファイルで保存します。")]),
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
