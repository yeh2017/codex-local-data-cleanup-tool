import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .history import is_codex_running
from .path_detection import is_codex_home
from .scanner import _is_link_like


class LogSafetyError(ValueError):
    pass


class LogGrowthCancelled(Exception):
    pass


@dataclass(frozen=True)
class LogDiagnostics:
    database_path: Path | None
    database_bytes: int
    wal_bytes: int
    shm_bytes: int
    row_count: int
    trace_count: int
    free_bytes: int
    oldest_ts: int | None
    newest_ts: int | None
    max_id: int
    integrity_ok: bool

    @property
    def total_bytes(self) -> int:
        return self.database_bytes + self.wal_bytes + self.shm_bytes

    @property
    def trace_ratio(self) -> float:
        return self.trace_count / self.row_count if self.row_count else 0.0


@dataclass(frozen=True)
class LogGrowth:
    interval_seconds: float
    bytes_delta: int
    rows_delta: int
    new_rows: int
    new_trace_rows: int
    max_id_delta: int
    wal_bytes_delta: int

    @property
    def rows_per_minute(self) -> float:
        return self.new_rows * 60 / self.interval_seconds

    @property
    def trace_rows_per_minute(self) -> float:
        return self.new_trace_rows * 60 / self.interval_seconds

    @property
    def bytes_per_minute(self) -> float:
        return max(0, self.bytes_delta) * 60 / self.interval_seconds

    @property
    def new_trace_ratio(self) -> float:
        return self.new_trace_rows / self.new_rows if self.new_rows else 0.0


def classify_log_growth(growth: LogGrowth) -> str:
    if (
        growth.rows_per_minute > 6000
        or growth.bytes_per_minute > 10 * 1024 * 1024
        or (
            growth.new_trace_ratio >= 0.9
            and growth.trace_rows_per_minute > 600
        )
    ):
        return "high"
    if growth.new_rows or growth.max_id_delta or growth.bytes_delta > 0:
        return "active"
    return "idle"


@dataclass(frozen=True)
class LogOptimizeResult:
    before: LogDiagnostics
    after: LogDiagnostics
    deleted_rows: int

    @property
    def released_bytes(self) -> int:
        return max(0, self.before.total_bytes - self.after.total_bytes)


@dataclass(frozen=True)
class LogCleanupPreview:
    retention_days: int
    cutoff_ts: int
    expired_rows: int
    estimated_bytes: int


def find_recovery_backups(root: Path) -> tuple[Path, ...]:
    root = Path(root).expanduser()
    return tuple(sorted(path for path in root.glob(".cleanup-logs-*.sqlite") if path.is_file()))


def _database_path(root: Path) -> tuple[Path, Path]:
    root = Path(root).expanduser().resolve()
    if not is_codex_home(root):
        raise LogSafetyError(f"不是有效的 Codex 数据目录：{root}")
    database = root / "logs_2.sqlite"
    if not database.is_file() or _is_link_like(database):
        raise LogSafetyError(f"未找到可用的日志数据库：{database}")
    return root, database


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def inspect_logs(root: Path) -> LogDiagnostics:
    _, database = _database_path(root)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        row_count, trace_count, oldest_ts, newest_ts, max_id = connection.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN UPPER(level) = 'TRACE' THEN 1 ELSE 0 END), 0),
                   MIN(ts), MAX(ts), COALESCE(MAX(id), 0)
            FROM logs
            """
        ).fetchone()
        quick_check = connection.execute("PRAGMA quick_check").fetchall()
        integrity_ok = (
            len(quick_check) == 1
            and str(quick_check[0][0]).casefold() == "ok"
        )
    except sqlite3.Error as exc:
        raise LogSafetyError(f"无法读取日志数据库：{exc}") from exc
    finally:
        connection.close()
    return LogDiagnostics(
        database_path=database,
        database_bytes=_file_size(database),
        wal_bytes=_file_size(database.with_name(database.name + "-wal")),
        shm_bytes=_file_size(database.with_name(database.name + "-shm")),
        row_count=int(row_count),
        trace_count=int(trace_count),
        free_bytes=page_size * free_pages,
        oldest_ts=int(oldest_ts) if oldest_ts is not None else None,
        newest_ts=int(newest_ts) if newest_ts is not None else None,
        max_id=int(max_id),
        integrity_ok=integrity_ok,
    )


def preview_log_cleanup(
    root: Path,
    *,
    retention_days: int = 30,
    now: Callable[[], float] = time.time,
) -> LogCleanupPreview:
    if retention_days < 1:
        raise LogSafetyError("日志保留天数必须大于 0")
    _, database = _database_path(root)
    cutoff = int(now()) - retention_days * 24 * 60 * 60
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(logs)")}
        if not {"ts"}.issubset(columns):
            raise LogSafetyError("日志数据库缺少时间字段，无法预览清理。")
        estimated_expression = (
            "COALESCE(SUM(estimated_bytes), 0)"
            if "estimated_bytes" in columns
            else "0"
        )
        expired_rows, estimated_bytes = connection.execute(
            f"SELECT COUNT(*), {estimated_expression} FROM logs WHERE ts < ?",
            (cutoff,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise LogSafetyError(f"无法读取日志数据库：{exc}") from exc
    finally:
        connection.close()
    return LogCleanupPreview(
        retention_days,
        cutoff,
        int(expired_rows),
        max(0, int(estimated_bytes or 0)),
    )


def _count_rows_after_id(database: Path, last_id: int) -> tuple[int, int]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN UPPER(level) = 'TRACE' THEN 1 ELSE 0 END), 0)
            FROM logs WHERE id > ?
            """,
            (last_id,),
        ).fetchone()
        return int(row[0]), int(row[1])
    finally:
        connection.close()


def sample_log_growth(
    root: Path,
    *,
    interval_seconds: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    cancel_event=None,
) -> LogGrowth:
    before = inspect_logs(root)
    if cancel_event is not None:
        if cancel_event.wait(interval_seconds):
            raise LogGrowthCancelled()
        if cancel_event.is_set():
            raise LogGrowthCancelled()
    else:
        sleep(interval_seconds)
    after = inspect_logs(root)
    if cancel_event is not None and cancel_event.is_set():
        raise LogGrowthCancelled()
    new_rows, new_trace_rows = _count_rows_after_id(after.database_path, before.max_id)
    if cancel_event is not None and cancel_event.is_set():
        raise LogGrowthCancelled()
    return LogGrowth(
        interval_seconds=interval_seconds,
        bytes_delta=after.total_bytes - before.total_bytes,
        rows_delta=after.row_count - before.row_count,
        new_rows=new_rows,
        new_trace_rows=new_trace_rows,
        max_id_delta=max(0, after.max_id - before.max_id),
        wal_bytes_delta=after.wal_bytes - before.wal_bytes,
    )


def _require_integrity(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or str(result[0]).casefold() != "ok":
        raise LogSafetyError(f"日志数据库完整性检查失败：{result}")


def _backup_database(source: Path, backup: Path) -> None:
    source_connection = sqlite3.connect(source, timeout=30)
    backup_connection = sqlite3.connect(backup)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()


def _restore_database(backup: Path, destination: Path) -> None:
    backup_connection = sqlite3.connect(backup)
    destination_connection = sqlite3.connect(destination, timeout=30)
    try:
        backup_connection.backup(destination_connection)
        checkpoint = destination_connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            raise LogSafetyError("恢复后的日志数据库 WAL 仍被占用。")
        _require_integrity(destination_connection)
    finally:
        destination_connection.close()
        backup_connection.close()


def optimize_logs(
    root: Path,
    *,
    retention_days: int = 30,
    now: Callable[[], float] = time.time,
    codex_running_check: Callable[[], bool] = is_codex_running,
) -> LogOptimizeResult:
    if retention_days < 1:
        raise LogSafetyError("日志保留天数必须大于 0")
    if codex_running_check():
        raise LogSafetyError("请完全退出 Codex 桌面程序后再优化日志。")
    root, database = _database_path(root)
    before = inspect_logs(root)
    required_space = before.total_bytes * 2 + 64 * 1024 * 1024
    available_space = shutil.disk_usage(root).free
    if available_space < required_space:
        raise LogSafetyError(
            f"磁盘空间不足：日志优化至少需要 {required_space} 字节，"
            f"当前可用 {available_space} 字节"
        )
    handle, backup_name = tempfile.mkstemp(prefix=".cleanup-logs-", suffix=".sqlite", dir=root)
    os.close(handle)
    Path(backup_name).unlink(missing_ok=True)
    backup = Path(backup_name)
    restored_or_succeeded = False
    connection: sqlite3.Connection | None = None
    try:
        _backup_database(database, backup)
        connection = sqlite3.connect(database, timeout=30)
        _require_integrity(connection)
        cutoff = int(now()) - retention_days * 24 * 60 * 60
        cursor = connection.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        deleted_rows = max(0, cursor.rowcount)
        connection.commit()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            raise LogSafetyError("日志数据库正在被占用，无法截断 WAL。")
        connection.execute("VACUUM")
        final_checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if final_checkpoint and int(final_checkpoint[0]) != 0:
            raise LogSafetyError("日志数据库正在被占用，无法完成最终 WAL 截断。")
        _require_integrity(connection)
        connection.close()
        connection = None
        after = inspect_logs(root)
        restored_or_succeeded = True
        return LogOptimizeResult(before, after, deleted_rows)
    except Exception as exc:
        if connection is not None:
            connection.close()
            connection = None
        try:
            if backup.is_file():
                _restore_database(backup, database)
                restored_or_succeeded = True
        except Exception as restore_exc:
            raise LogSafetyError(
                f"日志优化失败且自动恢复失败；备份保留在 {backup}：{restore_exc}"
            ) from exc
        raise
    finally:
        if connection is not None:
            connection.close()
        if restored_or_succeeded:
            backup.unlink(missing_ok=True)
