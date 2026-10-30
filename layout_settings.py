"""Per-user, local splitter and display preferences."""
from __future__ import annotations
import json
from pathlib import Path
from PySide6.QtCore import QTimer
from shiboken6 import isValid


def read_settings(directory):
    try:
        settings = json.loads((Path(directory) / "settings.json").read_text(encoding="utf-8"))
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


def write_setting(directory, key, value):
    path = Path(directory) / "settings.json"
    settings = read_settings(directory); settings[key] = value
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError:
        # Resizing remains usable even if preferences cannot be saved.
        pass


def restore_splitter(splitter, directory, key, defaults):
    values = read_settings(directory).get(key, defaults)
    if not isinstance(values, list) or len(values) != len(defaults) or any(type(v) is not int or v < 1 or v > 100000 for v in values):
        values = defaults
    if isValid(splitter): splitter.setSizes(values)


def configure_splitter(splitter, directory, key, defaults):
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(8)
    splitter.setOpaqueResize(True)
    splitter.setToolTip("境界をドラッグして幅を調整")
    def save(*_):
        if isValid(splitter):
            sizes = splitter.sizes()
            if len(sizes) == len(defaults) and all(size > 0 for size in sizes):
                write_setting(directory, key, sizes)
    splitter.splitterMoved.connect(save)
    QTimer.singleShot(0, lambda: restore_splitter(splitter, directory, key, defaults) if isValid(splitter) else None)
