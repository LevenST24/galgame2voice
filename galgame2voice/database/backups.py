"""Consistent SQLite snapshots and explicit, offline database recovery."""

from contextlib import closing
from datetime import datetime
import logging
import os
from pathlib import Path
import sqlite3
import time
import uuid

logger = logging.getLogger(__name__)


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def _snapshot(source: Path, destination: Path) -> None:
    """Include committed WAL pages without copying or checkpointing live files."""
    deadline = time.monotonic() + 15

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("数据库正忙，请关闭其他使用数据库的程序后重试。")

    with closing(_open_readonly(source)) as reader, closing(sqlite3.connect(destination)) as writer:
        os.chmod(destination, 0o600)
        reader.backup(writer, pages=128, progress=progress, sleep=0.01)
        writer.execute("PRAGMA journal_mode=DELETE")
        if writer.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise sqlite3.DatabaseError("数据库校验失败，已保留原文件。")
    with destination.open("r+b") as handle:
        os.fsync(handle.fileno())


def create_database_backup(path: Path, *, required: bool = False) -> Path | None:
    """Publish a verified snapshot atomically; keep five snapshots per database.

    Normal startup takes at most one daily snapshot. Schema upgrades always take
    a fresh snapshot and must stop if it cannot be safely created.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return None
    backup_dir = path.parent / "backups"
    today = datetime.now().strftime("%Y%m%d")
    if not required:
        for candidate in backup_dir.glob(f"{path.name}.bak_{today}_*"):
            try:
                validate_backup(candidate)
                return None
            except (sqlite3.Error, ValueError, OSError):
                continue
    pending = backup_dir / f".pending_{uuid.uuid4().hex}"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        _snapshot(path, pending)
        backup = backup_dir / f"{path.name}.bak_{datetime.now():%Y%m%d_%H%M%S_%f}"
        os.replace(pending, backup)
        logger.info("Created verified database backup: %s", backup)
    except Exception as exc:
        if required:
            raise RuntimeError(
                "无法建立升级前的数据备份，已停止升级并保留原数据库。"
                "请检查解压目录的写入权限和磁盘剩余空间后重新启动。"
            ) from exc
        logger.warning("Daily database backup failed; original database retained: %s", exc)
        return None
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
    # Retention must never turn a completed, valid backup into a failed upgrade.
    try:
        snapshots = sorted(backup_dir.glob(f"{path.name}.bak_*"))
        for obsolete in snapshots[:-5]:
            obsolete.unlink()
    except OSError as exc:
        logger.warning("Could not prune older database backups: %s", exc)
    return backup


def validate_backup(path: Path) -> int:
    """Accept an intact G2V database, never an unrelated SQLite file."""
    with closing(_open_readonly(path)) as conn:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("备份校验失败，请选择另一份备份。")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"settings", "providers", "sessions", "messages"}.issubset(tables):
            raise ValueError("这不是完整的 Galgame2Voice 数据库备份。")
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def list_database_backups(database: Path) -> list[Path]:
    return sorted((database.parent / "backups").glob(f"{database.name}.bak_*"), reverse=True)


def restore_database(database: Path, selected: Path, *, max_schema_version: int) -> Path:
    """Caller must hold the desktop instance lock and get explicit confirmation.

    Stage and verify the replacement first. Move every original SQLite file into
    a separate recovery folder; roll those moves back if installation fails.
    Credential files are never changed, and preserved originals are not pruned.
    """
    database = database.resolve()
    selected = selected.resolve()
    if selected not in {item.resolve() for item in list_database_backups(database)}:
        raise ValueError("请选择本程序 data/backups 中的数据库备份。")
    if validate_backup(selected) > max_schema_version:
        raise ValueError("备份来自较新的程序版本，请先安装对应版本再恢复。")
    pending = database.parent / f".restore_{uuid.uuid4().hex}"
    preserved = database.parent / "backups" / f"before_restore_{datetime.now():%Y%m%d_%H%M%S_%f}"
    moved: list[tuple[Path, Path]] = []
    try:
        _snapshot(selected, pending)
        validate_backup(pending)
        preserved.mkdir(parents=True)
        for original in (database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")):
            if original.exists():
                target = preserved / original.name
                os.replace(original, target)
                moved.append((original, target))
        os.replace(pending, database)
    except BaseException:
        for original, target in reversed(moved):
            os.replace(target, original)
        raise
    finally:
        pending.unlink(missing_ok=True)
    return preserved
