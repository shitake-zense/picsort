"""Local session storage and conservative, auditable file operations."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

SUPPORTED = {".jpg", ".jpeg", ".jpe", ".png"}


def fingerprint(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime": st.st_mtime_ns}


def matches(path: Path, expected: dict) -> bool:
    try:
        return not path.is_symlink() and fingerprint(path) == expected
    except OSError:
        return False


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def scan_images(root: Path, excluded: list[Path] = (), data_dir: Path | None = None):
    """No symlink traversal; stable relative-path ordering, not filename identity."""
    excluded = [p.resolve() for p in excluded]
    if data_dir:
        excluded.append(data_dir.resolve())
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".")
            and not (Path(directory) / d).is_symlink()
            and not any(is_within(Path(directory) / d, e) for e in excluded)
        )
        for name in sorted(files, key=str.casefold):
            path = Path(directory) / name
            if path.suffix.lower() in SUPPORTED and not path.is_symlink():
                yield str(path)


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "sessions.sqlite3"
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS projects(root TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(
                id INTEGER PRIMARY KEY, created TEXT NOT NULL,
                source TEXT NOT NULL, target TEXT, action TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT
            );
        """)
        self.connection.commit()

    def load(self, root: str) -> dict:
        row = self.connection.execute("SELECT state FROM projects WHERE root=?", (root,)).fetchone()
        if row:
            return json.loads(row[0])
        return {"decisions": {}, "reviewed": [], "cursor": "", "mode": "move", "destinations": []}

    def save(self, root: str, state: dict):
        with self.connection:
            self.connection.execute(
                "INSERT INTO projects(root,state) VALUES(?,?) ON CONFLICT(root) DO UPDATE SET state=excluded.state",
                (root, json.dumps(state, ensure_ascii=False)),
            )

    def begin_operation(self, source: str, target: str, action: str) -> int:
        with self.connection:
            result = self.connection.execute(
                "INSERT INTO operations(created,source,target,action,status) VALUES(?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), source, target, action, "started"),
            )
        return result.lastrowid

    def finish_operation(self, op_id: int, status: str, target: str = "", error: str = ""):
        with self.connection:
            self.connection.execute(
                "UPDATE operations SET status=?,target=?,error=? WHERE id=?", (status, target, error, op_id)
            )

    def history(self, limit=300) -> list:
        return self.connection.execute(
            "SELECT created,action,status,source,target,error FROM operations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def incomplete_sources(self) -> set[str]:
        return {row[0] for row in self.connection.execute(
            "SELECT source FROM operations WHERE status IN ('started','copied')"
        )}

    def close(self):
        self.connection.close()


def choose_target(source: Path, destination: Path):
    """Exclusive creation prevents overwriting even if a filename races us."""
    for number in range(100000):
        name = source.name if number == 0 else f"{source.stem} ({number}){source.suffix}"
        target = destination / name
        try:
            return target, target.open("xb")
        except FileExistsError:
            continue
    raise OSError("同名ファイルが多すぎるため、移動先の名前を決められませんでした。")


def verified_transfer(source: Path, destination: Path, expected: dict, move: bool,
                      copied_callback=lambda target: None) -> Path:
    """Copy + fsync + checksum + source recheck; only then remove the source."""
    if not matches(source, expected):
        raise OSError("写真が変更されたか、見つかりません。候補を付け直してください。")
    if source.parent.resolve() == destination.resolve():
        raise OSError("移動・コピー先が元のフォルダと同じです。別のフォルダを指定してください。")
    destination.mkdir(parents=True, exist_ok=True)
    target, stream = choose_target(source, destination)
    complete = False
    try:
        digest = hashlib.sha256()
        with source.open("rb") as original, stream:
            while block := original.read(1024 * 1024):
                digest.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        with target.open("rb") as result:
            copied_digest = hashlib.file_digest(result, "sha256").digest()
        if digest.digest() != copied_digest or not matches(source, expected):
            raise OSError("コピーの検証に失敗しました。元の写真は保持されています。")
        shutil.copystat(source, target)
        complete = True
        copied_callback(str(target))
        if move:
            if not matches(source, expected):
                raise OSError("コピー中に写真が変更されました。移動先と元の写真を確認してください。")
            source.unlink()
        return target
    except BaseException:
        if not stream.closed:
            stream.close()
        # Only our own incomplete output is removed. A verified copy remains recoverable.
        if not complete:
            target.unlink(missing_ok=True)
        raise


def safe_trash(source: str) -> str:
    """Use Qt's native Recycle Bin / NSFileManager trash API, with no delete fallback."""
    from PySide6.QtCore import QFile
    if not QFile.supportsMoveToTrash():
        raise OSError("この環境ではごみ箱が利用できません。写真はそのまま保持します。")
    file = QFile(Path(source).as_posix())
    if not file.moveToTrash():
        raise OSError("ごみ箱へ移動できません。写真はそのまま保持します。\n" + file.errorString())
    return file.fileName() or "OSのごみ箱"


def apply_decision(store: Store, source_text: str, decision: dict, mode: str,
                   trash_function=None) -> dict:
    source = Path(source_text)
    action = decision["action"]
    if action not in ("trash", "file"):
        return {"source": source_text, "success": False, "error": "実行対象外です。"}
    if source_text in store.incomplete_sources():
        return {"source": source_text, "success": False,
                "error": "前回の処理が中断されています。操作履歴で元と移動先を確認してください。"}
    if not matches(source, decision["fingerprint"]):
        return {"source": source_text, "success": False,
                "error": "写真が変更されたか、見つかりません。候補を付け直してください。"}
    if action == "file" and mode not in ("move", "copy"):
        raise ValueError("Unknown transfer mode")
    operation = "trash" if action == "trash" else mode
    destination = decision.get("destination", "")
    operation_id = store.begin_operation(source_text, destination, operation)
    copied_target = ""

    def copied(target):
        nonlocal copied_target
        copied_target = target
        store.finish_operation(operation_id, "copied", target)

    try:
        if action == "trash":
            if trash_function is None:
                trash_function = safe_trash
            target = trash_function(source_text) or "OSのごみ箱"
        else:
            target = str(verified_transfer(source, Path(destination), decision["fingerprint"],
                                           mode == "move", copied))
        store.finish_operation(operation_id, "done", target)
        return {"source": source_text, "success": True, "target": target, "action": operation}
    except Exception as error:
        message = str(error)
        # A completed copy whose source could not be removed requires manual review.
        status = "copied" if copied_target else "error"
        store.finish_operation(operation_id, status, copied_target or destination, message)
        return {"source": source_text, "success": False, "error": message, "target": copied_target}
