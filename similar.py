"""Local, low-resolution composition matching. It never changes photo files."""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat


@dataclass(frozen=True)
class Signature:
    path: str
    timestamp: float | None
    ratio: float
    values: tuple[float, ...]
    gradient: int
    color: tuple[float, float, float]


def _capture_time(exif):
    try:
        details = exif.get_ifd(34665)
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        details = {}
    raw = details.get(36867) or exif.get(36867) or details.get(36868) or exif.get(36868)
    if not raw:
        return None
    try:
        if isinstance(raw, bytes): raw = raw.decode("ascii", "ignore")
        dt = datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
        subseconds = details.get(37521) or exif.get(37521) or ""
        if isinstance(subseconds, bytes): subseconds = subseconds.decode("ascii", "ignore")
        digits = "".join(c for c in str(subseconds) if c.isdigit())[:6]
        seconds = (dt.toordinal() * 86400 + dt.hour * 3600 + dt.minute * 60 + dt.second)
        return seconds + (int(digits) / (10 ** len(digits)) if digits else 0)
    except (TypeError, ValueError, OverflowError):
        return None


def signature_for(path):
    # JPEG draft avoids decoding a full-resolution copy when possible.
    with Image.open(path) as source:
        exif = source.getexif()
        captured = _capture_time(exif)
        source.draft("RGB", (128, 128))
        image = ImageOps.exif_transpose(source)
        image.thumbnail((96, 96), Image.Resampling.LANCZOS)
        rgb = image.convert("RGB")
        ratio = rgb.width / rgb.height
        rgb = rgb.resize((16, 16), Image.Resampling.LANCZOS)
        rgb = rgb.filter(ImageFilter.GaussianBlur(0.55))
        gray = rgb.convert("L")
        pixels = tuple(gray.get_flattened_data())
        mean = sum(pixels) / len(pixels)
        contrast = max(20.0, math.sqrt(sum((v - mean) ** 2 for v in pixels) / len(pixels)))
        values = tuple(max(-2.5, min(2.5, (v - mean) / contrast)) for v in pixels)
        edge = gray.resize((9, 8), Image.Resampling.LANCZOS)
        data = tuple(edge.get_flattened_data())
        bits = 0
        for y in range(8):
            for x in range(8):
                bits = (bits << 1) | int(data[y * 9 + x] > data[y * 9 + x + 1])
        color_mean = ImageStat.Stat(rgb).mean
        color_total = max(1, sum(color_mean))
        color = tuple(v / color_total for v in color_mean)
    return Signature(str(path), captured, ratio, values, bits, color)


def similarity(a, b):
    # A different portrait/landscape framing should not be stretched into a match.
    if abs(math.log(a.ratio / b.ratio)) > 0.16:
        return 0.0
    diff = (a.gradient ^ b.gradient).bit_count()
    if diff > 28:
        return 0.0
    structure = min(1.0, sum(abs(x - y) for x, y in zip(a.values, b.values)) / len(a.values) / 1.4)
    edge = diff / 64.0
    color = min(1.0, sum(abs(x - y) for x, y in zip(a.color, b.color)) / 0.6)
    return max(0.0, 1.0 - 0.65 * structure - 0.25 * edge - 0.10 * color)


def find_similar(paths, time_window=3.0, threshold=0.83, cancelled=lambda: False, progress=lambda data: None):
    """Group only against each group's first photo, avoiding transitive chaining.

    time_window=None compares composition alone. No shooting time in time mode is
    skipped rather than silently treating file modification time as capture time.
    """
    groups = []
    # Index anchor times so a long event does not compare every earlier photo.
    time_index = []
    missing_time = unreadable = 0
    total = len(paths)
    for index, path in enumerate(paths):
        if cancelled(): return {"cancelled": True}
        if index % 8 == 0:
            progress({"done": index, "total": total})
        try:
            sig = signature_for(path)
        except Exception:
            # A corrupt photo or metadata should not abort the whole folder.
            unreadable += 1
            continue
        if time_window is not None and sig.timestamp is None:
            missing_time += 1
            continue
        best_group, best_score = None, threshold
        if time_window is None:
            candidates = range(len(groups))
        else:
            lo = bisect_left(time_index, (sig.timestamp - time_window, -1))
            hi = bisect_right(time_index, (sig.timestamp + time_window, len(groups)))
            candidates = (item[1] for item in time_index[lo:hi])
        for checked, group_index in enumerate(candidates):
            if checked % 128 == 0 and cancelled(): return {"cancelled": True}
            anchor, members = groups[group_index]
            score = similarity(anchor, sig)
            if score >= best_score:
                best_group, best_score = group_index, score
        if best_group is None:
            if time_window is not None:
                insort(time_index, (sig.timestamp, len(groups)))
            groups.append((sig, [sig.path]))
        else:
            groups[best_group][1].append(sig.path)
    progress({"done": total, "total": total})
    matches = [members for _, members in groups if len(members) >= 2]
    return {"groups": matches, "missing_time": missing_time, "unreadable": unreadable, "total": total}
