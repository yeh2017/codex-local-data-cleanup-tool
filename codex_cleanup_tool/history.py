import csv
import html
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .path_detection import is_codex_home
from .recycle_bin import RecycleBinClient
from .scanner import _is_link_like
from .storage_registry import StorageCompatibilityError, StorageRegistry


class HistorySafetyError(ValueError):
    pass


@dataclass(frozen=True)
class HistoryRecord:
    id: str
    title: str
    updated_at: str | int | float
    archived: bool
    rollout_path: Path
    total_bytes: int
    related_sizes: tuple[tuple[str, int], ...] = ()
    missing_rollout: bool = False


@dataclass(frozen=True)
class HistoryDeleteResult:
    deleted_ids: tuple[str, ...]
    recycled_paths: tuple[Path, ...]
    backup_path: Path | None = None
    deleted_log_rows: int = 0
    deleted_auxiliary_references: int = 0


def _plain_windows_path(path: str | Path) -> Path:
    text = str(path)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text).expanduser().resolve()


def _validate_rollout_path(root: Path, record_id: str, raw_path: str | Path) -> Path:
    path = _plain_windows_path(raw_path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise HistorySafetyError(f"历史记录文件位于 Codex 目录之外：{path}") from exc
    if not relative.parts or relative.parts[0] not in {"sessions", "archived_sessions"}:
        raise HistorySafetyError(f"历史记录文件不属于允许的会话目录：{path}")
    if (
        path.suffix.casefold() != ".jsonl"
        or not path.stem.endswith(f"-{record_id}")
    ):
        raise HistorySafetyError(f"历史记录 ID 与文件名不匹配：{path}")
    if _is_link_like(path):
        raise HistorySafetyError(f"禁止处理符号链接或目录联接：{path}")
    return path


def _is_internal_thread_source(source: object) -> bool:
    if not isinstance(source, str) or not source.startswith("{"):
        return False
    try:
        value = json.loads(source)
    except ValueError:
        return False
    return isinstance(value, dict) and "subagent" in value


def _clean_history_title(*candidates: object) -> str:
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        text = html.unescape(candidate).strip()
        marker = "## My request:"
        if marker in text:
            text = text.rsplit(marker, 1)[1].strip()
        text = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if text:
            return text[:240]
    return ""


def scan_history_records(
    root: Path, *, include_internal: bool = False
) -> tuple[HistoryRecord, ...]:
    root = Path(root).expanduser().resolve()
    if not is_codex_home(root):
        raise HistorySafetyError(f"不是有效的 Codex 数据目录：{root}")
    database = root / "state_5.sqlite"
    if not database.is_file() or _is_link_like(database):
        raise HistorySafetyError(f"未找到可用的任务状态数据库：{database}")

    uri = f"file:{database.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            """
            SELECT
                id,
                name,
                title,
                first_user_message,
                COALESCE(updated_at, ''),
                COALESCE(archived, 0),
                rollout_path,
                source
            FROM threads
            WHERE rollout_path IS NOT NULL AND rollout_path <> ''
            ORDER BY COALESCE(recency_at_ms, updated_at_ms, 0) DESC
            """
        ).fetchall()
        edges = connection.execute(
            "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
        ).fetchall()
    finally:
        connection.close()

    records: list[tuple[HistoryRecord, object]] = []
    for record_id, name, legacy_title, first_message, updated_at, archived, raw_path, source in rows:
        path = _validate_rollout_path(root, record_id, raw_path)
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        records.append(
            (
                HistoryRecord(
                id=record_id,
                title=_clean_history_title(name, legacy_title, first_message, record_id),
                updated_at=updated_at,
                archived=bool(archived),
                rollout_path=path,
                total_bytes=size,
                missing_rollout=not path.is_file(),
                ),
                source,
            )
        )
    if include_internal:
        return tuple(
            replace(record, related_sizes=((record.id, record.total_bytes),))
            for record, _ in records
        )

    by_id = {record.id: record for record, _ in records}
    children: dict[str, set[str]] = {}
    for parent_id, child_id in edges:
        if parent_id and child_id:
            children.setdefault(parent_id, set()).add(child_id)

    def related_sizes(record_id: str) -> tuple[tuple[str, int], ...]:
        pending = [record_id]
        visited: set[str] = set()
        sizes: list[tuple[str, int]] = []
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            record = by_id.get(current)
            if record is not None:
                sizes.append((record.id, record.total_bytes))
            pending.extend(children.get(current, ()))
        return tuple(sorted(sizes))

    return tuple(
        replace(
            record,
            total_bytes=sum(size for _, size in related_sizes(record.id)),
            related_sizes=related_sizes(record.id),
        )
        for record, source in records
        if not _is_internal_thread_source(source)
    )


def _expand_descendant_ids(database: Path, selected_ids: set[str]) -> set[str]:
    expanded = set(selected_ids)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        while True:
            placeholders = ",".join("?" for _ in expanded)
            children = {
                row[0]
                for row in connection.execute(
                    f"SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id IN ({placeholders})",
                    tuple(sorted(expanded)),
                )
                if row[0]
            }
            new_ids = children.difference(expanded)
            if not new_ids:
                return expanded
            expanded.update(new_ids)
    finally:
        connection.close()


def is_codex_running() -> bool:
    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if completed.returncode != 0:
        return True
    blocked = {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}
    return any(
        row and row[0].strip().casefold() in blocked
        for row in csv.reader(completed.stdout.splitlines())
    )


def _index_without_ids(index: Path, selected_ids: set[str]) -> bytes | None:
    if not index.is_file():
        return None
    kept: list[str] = []
    for line in index.read_text(encoding="utf-8").splitlines():
        try:
            record_id = json.loads(line).get("id")
        except (ValueError, AttributeError):
            record_id = None
        if record_id not in selected_ids:
            kept.append(line)
    text = "\n".join(kept) + ("\n" if kept else "")
    return text.encode("utf-8")


def _replace_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + ".cleanup.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _restore_database_backup(
    backup_path: Path,
    connection: sqlite3.Connection,
) -> None:
    backup_connection = sqlite3.connect(backup_path)
    try:
        backup_connection.backup(connection)
    finally:
        backup_connection.close()


def delete_history_records(
    root: Path,
    selected_ids: set[str],
    *,
    recycle_client: RecycleBinClient | None = None,
    require_codex_closed: bool = True,
    codex_running_check: Callable[[], bool] = is_codex_running,
    backup_root: Path | None = None,
) -> HistoryDeleteResult:
    root = Path(root).expanduser().resolve()
    selected_ids = {str(item) for item in selected_ids if item}
    if not selected_ids:
        raise HistorySafetyError("没有选择历史记录")
    if require_codex_closed and codex_running_check():
        raise HistorySafetyError("请完全退出 Codex 桌面程序后再删除历史记录。")

    database = root / "state_5.sqlite"
    selected_ids = _expand_descendant_ids(database, selected_ids)
    records = {
        record.id: record
        for record in scan_history_records(root, include_internal=True)
    }
    missing = selected_ids.difference(records)
    if missing:
        raise HistorySafetyError(f"找不到所选历史记录：{', '.join(sorted(missing))}")
    selected = tuple(records[record_id] for record_id in sorted(selected_ids))
    for record in selected:
        if not record.rollout_path.is_file():
            raise HistorySafetyError(f"历史记录文件已不存在：{record.rollout_path}")

    registry = StorageRegistry(root)
    compatibility = registry.inspect(selected_ids, strict=True)
    if compatibility.unknown_references:
        details = ", ".join(
            f"{item.path} ({item.detail or item.store})"
            for item in compatibility.unknown_references
        )
        raise HistorySafetyError(f"发现无法安全处理的任务引用：{details}")
    managed_paths = registry.managed_paths(selected_ids) | {
        record.rollout_path.resolve() for record in selected
    }
    unmanaged = registry.unmanaged_references(selected_ids, managed_paths)
    if unmanaged:
        details = ", ".join(str(item.path) for item in unmanaged)
        raise HistorySafetyError(f"发现无法安全处理的任务引用：{details}")

    backup_path = None
    operation_id = uuid.uuid4().hex
    auxiliary_rollback = root / f".cleanup-auxiliary-{operation_id}"
    auxiliary_root: Path
    auxiliary_metadata: dict
    if backup_root is not None:
        from .history_backup import create_history_backup

        backup_path = create_history_backup(
            root, selected_ids, backup_root, require_codex_closed=False
        ).path
        manifest = json.loads((backup_path / "manifest.json").read_text(encoding="utf-8"))
        auxiliary_root = backup_path / "auxiliary"
        auxiliary_metadata = manifest.get("auxiliary", {})
    else:
        auxiliary_metadata = registry.export_selected(
            selected_ids, auxiliary_rollback
        )
        auxiliary_root = auxiliary_rollback

    client = recycle_client or RecycleBinClient()
    index = root / "session_index.jsonl"
    original_index = index.read_bytes() if index.is_file() else None
    rewritten_index = _index_without_ids(index, selected_ids)
    if rewritten_index is not None and any(
        item.encode("utf-8") in rewritten_index for item in selected_ids
    ):
        raise HistorySafetyError("任务索引包含无法安全移除的任务引用")
    placeholders = ",".join("?" for _ in selected_ids)
    parameters = tuple(sorted(selected_ids))
    staging = root / f".cleanup-history-{operation_id}"
    database_backup = root / f".cleanup-state-{operation_id}.sqlite"
    logs_database = root / "logs_2.sqlite"
    logs_database_backup = root / f".cleanup-logs-{operation_id}.sqlite"
    staged: list[tuple[Path, Path]] = []
    deleted_log_rows = 0
    deleted_auxiliary_references = 0
    auxiliary_delete_started = False
    logs_connection: sqlite3.Connection | None = None
    operation_succeeded = False
    recovery_succeeded = False

    connection = sqlite3.connect(database, timeout=30)
    try:
        backup_connection = sqlite3.connect(database_backup)
        try:
            connection.backup(backup_connection)
        finally:
            backup_connection.close()
        if logs_database.is_file() and not _is_link_like(logs_database):
            logs_connection = sqlite3.connect(logs_database, timeout=30)
            log_columns = {
                row[1] for row in logs_connection.execute("PRAGMA table_info(logs)")
            }
            if "thread_id" in log_columns:
                backup_connection = sqlite3.connect(logs_database_backup)
                try:
                    logs_connection.backup(backup_connection)
                finally:
                    backup_connection.close()
                logs_connection.execute("BEGIN IMMEDIATE")
            else:
                logs_connection.close()
                logs_connection = None
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"DELETE FROM thread_spawn_edges WHERE parent_thread_id IN ({placeholders}) OR child_thread_id IN ({placeholders})",
            parameters + parameters,
        )
        connection.execute(
            f"DELETE FROM thread_dynamic_tools WHERE thread_id IN ({placeholders})",
            parameters,
        )
        if connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='thread_attachments'"
        ).fetchone():
            connection.execute(
                f"DELETE FROM thread_attachments "
                f"WHERE thread_id IN ({placeholders})",
                parameters,
            )
        connection.execute(
            f"DELETE FROM threads WHERE id IN ({placeholders})",
            parameters,
        )
        if logs_connection is not None:
            cursor = logs_connection.execute(
                f"DELETE FROM logs WHERE thread_id IN ({placeholders})",
                parameters,
            )
            deleted_log_rows = max(0, cursor.rowcount)
        if rewritten_index is not None:
            _replace_bytes(index, rewritten_index)
        staging.mkdir()
        for record in selected:
            staged_path = staging / record.rollout_path.name
            os.replace(record.rollout_path, staged_path)
            staged.append((record.rollout_path, staged_path))
        if logs_connection is not None:
            logs_connection.commit()
        connection.commit()
        auxiliary_delete_started = True
        deleted_auxiliary_references = registry.delete_additional(
            selected_ids
        ).deleted_references
        recycle_result = client.recycle((staging,))
        if recycle_result.failed:
            _restore_database_backup(database_backup, connection)
            if logs_connection is not None and logs_database_backup.is_file():
                _restore_database_backup(logs_database_backup, logs_connection)
            if not staging.exists() and backup_path is not None:
                for record in selected:
                    source = backup_path / "files" / record.rollout_path.relative_to(root)
                    record.rollout_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, record.rollout_path)
            details = "；".join(
                f"{path}：{reason}" for path, reason in recycle_result.failed
            )
            raise HistorySafetyError(f"历史文件未能移入回收站：{details}")
        operation_succeeded = True
    except Exception as original_error:
        connection.rollback()
        if logs_connection is not None:
            logs_connection.rollback()
        try:
            if database_backup.is_file():
                _restore_database_backup(database_backup, connection)
            if logs_connection is not None and logs_database_backup.is_file():
                _restore_database_backup(logs_database_backup, logs_connection)
            if original_index is not None:
                _replace_bytes(index, original_index)
            if staging.exists():
                for original, staged_path in reversed(staged):
                    if staged_path.exists():
                        original.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(staged_path, original)
                try:
                    staging.rmdir()
                except OSError:
                    pass
            if auxiliary_delete_started:
                registry.restore_selected(
                    selected_ids, auxiliary_root, auxiliary_metadata
                )
            recovery_succeeded = True
        except Exception as recovery_error:
            snapshots = [
                str(path)
                for path in (database_backup, logs_database_backup)
                if path.is_file()
            ]
            raise HistorySafetyError(
                "历史删除失败且自动回滚失败；救援快照已保留："
                + ", ".join(snapshots)
                + f"。回滚错误：{recovery_error}"
            ) from original_error
        raise
    finally:
        if logs_connection is not None:
            logs_connection.close()
        connection.close()
        if operation_succeeded or recovery_succeeded:
            database_backup.unlink(missing_ok=True)
            logs_database_backup.unlink(missing_ok=True)
            if auxiliary_rollback.is_dir():
                shutil.rmtree(auxiliary_rollback, ignore_errors=True)

    return HistoryDeleteResult(
        parameters,
        tuple(record.rollout_path for record in selected),
        backup_path,
        deleted_log_rows,
        deleted_auxiliary_references,
    )
