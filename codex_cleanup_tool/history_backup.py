import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .path_detection import is_codex_home
from .scanner import _is_link_like
from .storage_registry import (
    StorageCompatibilityError,
    StorageRegistry,
    sqlite_reference_where,
)


class BackupSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class HistoryBackupResult:
    path: Path
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class HistoryRestoreResult:
    restored_ids: tuple[str, ...]
    restored_log_rows: int = 0


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_from_existing_ancestor(path: Path) -> Path:
    candidate = Path(path).expanduser()
    missing_parts = []
    while not candidate.exists() and candidate.parent != candidate:
        missing_parts.append(candidate.name)
        candidate = candidate.parent
    resolved = candidate.resolve()
    for part in reversed(missing_parts):
        resolved = resolved / part
    return resolved


def ensure_backup_root(path: Path, codex_root: Path, *, create: bool = False) -> Path:
    raw_target = Path(path).expanduser()
    if raw_target.exists() and _is_link_like(raw_target):
        raise BackupSafetyError("备份目录不能是符号链接或目录联接。")
    target = raw_target.resolve()
    codex = Path(codex_root).expanduser().resolve()
    if _inside(target, codex) or _inside(codex, target):
        raise BackupSafetyError("备份目录必须位于 Codex 数据目录之外。")
    if target.exists() and (_is_link_like(target) or not target.is_dir()):
        raise BackupSafetyError("备份目录不能是符号链接、目录联接或普通文件。")
    if not target.exists():
        if not create:
            raise BackupSafetyError("备份目录不存在，请先创建或重新选择。")
        target.mkdir(parents=True)
    marker = target / ".codex-history-backups"
    if not marker.exists():
        marker.write_text("1\n", encoding="ascii")
    return target


def _safe_relative_path(value: str) -> Path:
    relative = Path(value)
    if value.startswith(("/", "\\")) or relative.anchor or relative.drive or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise BackupSafetyError(f"备份清单包含不安全路径：{value}")
    return relative


def _safe_restore_destination(root: Path, relative: Path) -> Path:
    root = Path(root).resolve()
    relative = _safe_relative_path(Path(relative).as_posix())
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_like(current):
            raise BackupSafetyError(
                f"恢复路径包含符号链接或目录联接：{current}"
            )
    destination = (root / relative).resolve()
    if not _inside(destination, root):
        raise BackupSafetyError(f"恢复路径超出 Codex 数据目录：{destination}")
    return destination


def _verify_backup_folder(path: Path) -> None:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest.get("files", ()):
        candidate = path / "files" / _safe_relative_path(item["relative_path"])
        if not candidate.is_file() or _hash(candidate) != item["sha256"]:
            raise BackupSafetyError(f"备份校验失败：{path}")
    metadata_path = path / "metadata.sqlite"
    index_path = path / "index.jsonl"
    if _hash(metadata_path) != manifest.get("metadata_sha256") or _hash(index_path) != manifest.get("index_sha256"):
        raise BackupSafetyError(f"备份元数据或索引校验失败：{path}")
    connection = sqlite3.connect(path / "metadata.sqlite")
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupSafetyError(f"备份数据库校验失败：{path}")
        stored_ids = {row[0] for row in connection.execute("SELECT id FROM threads")}
        if stored_ids != set(manifest.get("record_ids", ())):
            raise BackupSafetyError(f"备份任务清单与数据库不一致：{path}")
    finally:
        connection.close()
    allowed_ids = set(manifest.get("record_ids", ()))
    for line in index_path.read_text(encoding="utf-8").splitlines():
        try:
            index_id = json.loads(line).get("id")
        except (ValueError, AttributeError) as exc:
            raise BackupSafetyError(f"备份索引格式无效：{path}") from exc
        if index_id not in allowed_ids:
            raise BackupSafetyError(f"备份索引包含清单之外的任务：{path}")
    logs_hash = manifest.get("logs_sha256")
    if logs_hash:
        logs_path = path / "logs.sqlite"
        if not logs_path.is_file() or _hash(logs_path) != logs_hash:
            raise BackupSafetyError(f"备份日志校验失败：{path}")
        logs = sqlite3.connect(logs_path)
        try:
            if logs.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupSafetyError(f"备份日志数据库完整性检查失败：{path}")
            columns = {row[1] for row in logs.execute("PRAGMA table_info(logs)")}
            where, parameters = sqlite_reference_where(
                "logs_2.sqlite", "logs", ("thread_id",), columns, allowed_ids
            )
            total = logs.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            selected = logs.execute(
                f"SELECT COUNT(*) FROM logs WHERE {where}", parameters
            ).fetchone()[0]
            if total != selected:
                raise BackupSafetyError(f"备份日志包含清单之外的任务：{path}")
        finally:
            logs.close()
    auxiliary = manifest.get("auxiliary")
    if auxiliary:
        try:
            StorageRegistry(path).verify_export(path / "auxiliary", auxiliary)
        except StorageCompatibilityError as exc:
            raise BackupSafetyError(str(exc)) from exc


def migrate_backup_root(old_path: Path, new_path: Path, codex_root: Path) -> Path:
    old = ensure_backup_root(old_path, codex_root)
    new = ensure_backup_root(new_path, codex_root, create=True)
    if old == new:
        return new
    backups = [child for child in old.iterdir() if child.is_dir() and (child / "manifest.json").is_file()]
    copied: list[tuple[Path, Path]] = []
    for source in backups:
        destination = new / source.name
        if destination.exists():
            raise BackupSafetyError(f"新目录中已有同名备份：{destination.name}")
        temporary = new / f".migrating-{source.name}"
        shutil.copytree(source, temporary)
        _verify_backup_folder(temporary)
        os.replace(temporary, destination)
        copied.append((source, destination))
    for source, _ in copied:
        shutil.rmtree(source)
    return new


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_table(source: sqlite3.Connection, target: sqlite3.Connection, table: str, where: str, params: tuple) -> None:
    create_sql = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    target.execute(create_sql)
    columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
    rows = source.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()
    if rows:
        target.executemany(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            rows,
        )


def _copy_optional_table(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    where: str,
    params: tuple,
) -> None:
    exists = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists:
        _copy_table(source, target, table, where, params)


def create_history_backup(
    root: Path,
    selected_ids: set[str],
    backup_root: Path,
    *,
    require_codex_closed: bool = True,
    codex_running_check=None,
) -> HistoryBackupResult:
    from .history import _expand_descendant_ids, is_codex_running, scan_history_records

    running_check = codex_running_check or is_codex_running
    if require_codex_closed and running_check():
        raise BackupSafetyError("请完全退出 Codex 桌面程序后再备份历史记录。")

    root = Path(root).expanduser().resolve()
    backup_root = ensure_backup_root(backup_root, root)
    database = root / "state_5.sqlite"
    ids = tuple(sorted(_expand_descendant_ids(database, {str(i) for i in selected_ids if i})))
    if not ids:
        raise BackupSafetyError("没有选择需要备份的历史记录。")
    records = {record.id: record for record in scan_history_records(root, include_internal=True)}
    if set(ids).difference(records):
        raise BackupSafetyError("部分历史记录已经不存在，无法备份。")
    operation = uuid.uuid4().hex
    temporary = backup_root / f".creating-{operation}"
    final = backup_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{operation[:8]}"
    temporary.mkdir()
    try:
        files = []
        for record_id in ids:
            source_file = records[record_id].rollout_path
            relative = source_file.relative_to(root)
            destination = temporary / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
            files.append({"relative_path": relative.as_posix(), "size": destination.stat().st_size, "sha256": _hash(destination)})
        source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        metadata = sqlite3.connect(temporary / "metadata.sqlite")
        try:
            placeholders = ",".join("?" for _ in ids)
            _copy_table(source, metadata, "threads", f"id IN ({placeholders})", ids)
            _copy_table(source, metadata, "thread_dynamic_tools", f"thread_id IN ({placeholders})", ids)
            _copy_optional_table(
                source,
                metadata,
                "thread_attachments",
                f"thread_id IN ({placeholders})",
                ids,
            )
            _copy_table(source, metadata, "thread_spawn_edges", f"parent_thread_id IN ({placeholders}) OR child_thread_id IN ({placeholders})", ids + ids)
            metadata.commit()
            if metadata.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupSafetyError("备份元数据完整性检查失败。")
        finally:
            metadata.close()
            source.close()
        index_rows = []
        index = root / "session_index.jsonl"
        if index.is_file():
            for line in index.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("id") in ids:
                        index_rows.append(line)
                except (ValueError, AttributeError):
                    pass
        index_backup = temporary / "index.jsonl"
        index_backup.write_text("\n".join(index_rows) + ("\n" if index_rows else ""), encoding="utf-8")
        logs_hash = None
        logs_database = root / "logs_2.sqlite"
        if logs_database.is_file() and not _is_link_like(logs_database):
            logs_source = sqlite3.connect(
                f"file:{logs_database.as_posix()}?mode=ro", uri=True
            )
            logs_backup = sqlite3.connect(temporary / "logs.sqlite")
            copied_logs = False
            try:
                columns = {
                    row[1] for row in logs_source.execute("PRAGMA table_info(logs)")
                }
                if "thread_id" in columns:
                    where, parameters = sqlite_reference_where(
                        "logs_2.sqlite", "logs", ("thread_id",), columns, set(ids)
                    )
                    _copy_table(
                        logs_source,
                        logs_backup,
                        "logs",
                        where,
                        parameters,
                    )
                    logs_backup.commit()
                    copied_logs = True
            finally:
                logs_backup.close()
                logs_source.close()
            if copied_logs:
                logs_hash = _hash(temporary / "logs.sqlite")
            else:
                (temporary / "logs.sqlite").unlink(missing_ok=True)
        auxiliary = StorageRegistry(root).export_selected(
            set(ids), temporary / "auxiliary"
        )
        manifest = {"version": 3, "created_at": datetime.now(timezone.utc).isoformat(), "source_root": str(root), "installation_id": (root / "installation_id").read_text(encoding="utf-8").strip(), "record_ids": ids, "files": files, "metadata_sha256": _hash(temporary / "metadata.sqlite"), "index_sha256": _hash(index_backup), "auxiliary": auxiliary}
        if logs_hash:
            manifest["logs_sha256"] = logs_hash
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, final)
        return HistoryBackupResult(final, ids)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _insert_table(source: sqlite3.Connection, target: sqlite3.Connection, table: str) -> None:
    source_columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
    target_info = target.execute(f"PRAGMA table_info({table})").fetchall()
    target_columns = {row[1] for row in target_info}
    missing_required = [
        row[1]
        for row in target_info
        if row[1] not in source_columns
        and bool(row[3])
        and row[4] is None
        and not bool(row[5])
    ]
    if missing_required:
        raise BackupSafetyError(
            f"当前数据库表 {table} 新增了备份中不存在的必填字段："
            + ", ".join(missing_required)
        )
    columns = [column for column in source_columns if column in target_columns]
    rows = source.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
    if rows:
        target.executemany(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            rows,
        )


def _insert_optional_table(
    source: sqlite3.Connection, target: sqlite3.Connection, table: str
) -> None:
    source_exists = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not source_exists:
        return
    target_exists = target.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not target_exists:
        raise BackupSafetyError(f"当前数据库缺少备份所需的数据表：{table}")
    _insert_table(source, target, table)


def _snapshot_connection(connection: sqlite3.Connection, path: Path) -> None:
    snapshot = sqlite3.connect(path)
    try:
        connection.backup(snapshot)
    finally:
        snapshot.close()


def _restore_snapshot(path: Path, connection: sqlite3.Connection) -> None:
    snapshot = sqlite3.connect(path)
    try:
        snapshot.backup(connection)
    finally:
        snapshot.close()


def _validate_spawn_edge_endpoints(
    metadata: sqlite3.Connection,
    target: sqlite3.Connection,
) -> None:
    restored_ids = {
        str(row[0]) for row in metadata.execute("SELECT id FROM threads")
    }
    existing_ids = {
        str(row[0]) for row in target.execute("SELECT id FROM threads")
    }
    available_ids = restored_ids | existing_ids
    missing = {
        str(endpoint)
        for parent_id, child_id in metadata.execute(
            "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
        )
        for endpoint in (parent_id, child_id)
        if endpoint is not None and str(endpoint) not in available_ids
    }
    if missing:
        raise BackupSafetyError(
            "备份关联任务不存在，已拒绝恢复：" + ", ".join(sorted(missing))
        )


def _rollout_path_updates(
    metadata: sqlite3.Connection,
    manifest: dict,
    root: Path,
) -> list[tuple[str, str]]:
    source_root_value = manifest.get("source_root")
    if not isinstance(source_root_value, str) or not source_root_value:
        raise BackupSafetyError("备份缺少原始数据目录信息，无法安全恢复。")
    source_root = _resolve_from_existing_ancestor(Path(source_root_value))
    backed_up_files = {
        _safe_relative_path(item["relative_path"]).as_posix()
        for item in manifest.get("files", ())
    }
    updates = []
    for record_id, rollout_path in metadata.execute(
        "SELECT id, rollout_path FROM threads"
    ):
        try:
            relative = _resolve_from_existing_ancestor(
                Path(str(rollout_path))
            ).relative_to(source_root)
        except ValueError as exc:
            raise BackupSafetyError(
                f"备份任务路径不属于原始数据目录：{rollout_path}"
            ) from exc
        relative = _safe_relative_path(relative.as_posix())
        if relative.as_posix() not in backed_up_files:
            raise BackupSafetyError(
                f"备份任务路径缺少对应文件：{relative.as_posix()}"
            )
        destination = _safe_restore_destination(root, relative)
        updates.append((str(destination), str(record_id)))
    return updates


def restore_history_backup(backup_path: Path, root: Path, *, require_codex_closed: bool = True) -> HistoryRestoreResult:
    from .history import is_codex_running

    raw_backup = Path(backup_path).expanduser()
    if raw_backup.exists() and _is_link_like(raw_backup):
        raise BackupSafetyError("备份目录不能是符号链接或目录联接。")
    backup = raw_backup.resolve()
    root = Path(root).expanduser().resolve()
    if require_codex_closed and is_codex_running():
        raise BackupSafetyError("请完全退出 Codex 桌面程序后再恢复历史记录。")
    if not is_codex_home(root) or _is_link_like(backup):
        raise BackupSafetyError("恢复路径无效。")
    try:
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupSafetyError("所选目录不是有效的任务备份。") from exc
    installation = (root / "installation_id").read_text(encoding="utf-8").strip()
    if manifest.get("installation_id") != installation:
        raise BackupSafetyError("该备份不属于当前 Codex 数据目录。")
    ids = tuple(str(i) for i in manifest.get("record_ids", ()))
    _verify_backup_folder(backup)
    database = root / "state_5.sqlite"
    logs_backup = backup / "logs.sqlite"
    active_logs = root / "logs_2.sqlite"
    if logs_backup.is_file() and (
        not active_logs.is_file() or _is_link_like(active_logs)
    ):
        raise BackupSafetyError("当前 Codex 日志数据库不存在，无法完整恢复任务。")
    target = sqlite3.connect(database, timeout=30)
    metadata = sqlite3.connect(backup / "metadata.sqlite")
    logs_source = None
    logs_target = None
    restored_log_rows = 0
    if logs_backup.is_file():
        logs_source = sqlite3.connect(logs_backup)
        logs_target = sqlite3.connect(active_logs, timeout=30)
    operation = uuid.uuid4().hex
    state_snapshot = root / f".cleanup-restore-state-{operation}.sqlite"
    logs_snapshot = root / f".cleanup-restore-logs-{operation}.sqlite"
    moved: list[Path] = []
    index = root / "session_index.jsonl"
    original_index = index.read_bytes() if index.exists() else None
    operation_succeeded = False
    recovery_succeeded = False
    auxiliary_restore_started = False
    try:
        placeholders = ",".join("?" for _ in ids)
        if target.execute(f"SELECT COUNT(*) FROM threads WHERE id IN ({placeholders})", ids).fetchone()[0]:
            raise BackupSafetyError("当前数据中已存在同 ID 的历史记录，已拒绝覆盖。")
        existing_lines = index.read_text(encoding="utf-8").splitlines() if index.exists() else []
        for line in existing_lines:
            try:
                existing_id = json.loads(line).get("id")
            except (ValueError, AttributeError):
                continue
            if existing_id in ids:
                raise BackupSafetyError("任务索引中已存在同 ID 记录，已拒绝覆盖。")
        for item in manifest["files"]:
            relative = _safe_relative_path(item["relative_path"])
            source_file = backup / "files" / relative
            destination = _safe_restore_destination(root, relative)
            if destination.exists():
                raise BackupSafetyError(f"目标文件已存在，已拒绝覆盖：{destination}")
            if _hash(source_file) != item["sha256"]:
                raise BackupSafetyError("备份文件校验失败，未执行恢复。")
        if logs_target is not None:
            log_columns = {
                row[1] for row in logs_target.execute("PRAGMA table_info(logs)")
            }
            where, log_parameters = sqlite_reference_where(
                "logs_2.sqlite", "logs", ("thread_id",), log_columns, set(ids)
            )
            log_conflicts = logs_target.execute(
                f"SELECT COUNT(*) FROM logs WHERE {where}", log_parameters
            ).fetchone()[0]
            if log_conflicts:
                raise BackupSafetyError("当前日志中已存在相同任务 ID，已拒绝重复恢复。")
        auxiliary = manifest.get("auxiliary")
        if auxiliary and StorageRegistry(root).inspect(set(ids)).total_references:
            raise BackupSafetyError(
                "当前辅助存储中已存在相同任务 ID 的引用，已拒绝覆盖。"
            )
        _validate_spawn_edge_endpoints(metadata, target)
        rollout_updates = _rollout_path_updates(metadata, manifest, root)
        _snapshot_connection(target, state_snapshot)
        if logs_target is not None:
            _snapshot_connection(logs_target, logs_snapshot)
        target.execute("PRAGMA foreign_keys = ON")
        target.execute("BEGIN IMMEDIATE")
        if logs_target is not None:
            logs_target.execute("BEGIN IMMEDIATE")
        for table in ("threads", "thread_dynamic_tools", "thread_spawn_edges"):
            _insert_table(metadata, target, table)
        _insert_optional_table(metadata, target, "thread_attachments")
        target.executemany(
            "UPDATE threads SET rollout_path = ? WHERE id = ?",
            rollout_updates,
        )
        foreign_key_errors = target.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise BackupSafetyError("恢复后的数据库关系校验失败，已取消恢复。")
        if logs_source is not None and logs_target is not None:
            restored_log_rows = logs_source.execute(
                "SELECT COUNT(*) FROM logs"
            ).fetchone()[0]
            _insert_table(logs_source, logs_target, "logs")
        for item in manifest["files"]:
            relative = _safe_relative_path(item["relative_path"])
            source_file = backup / "files" / relative
            destination = _safe_restore_destination(root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
            moved.append(destination)
        additions = (backup / "index.jsonl").read_text(encoding="utf-8").splitlines()
        text = "\n".join(existing_lines + additions) + ("\n" if existing_lines or additions else "")
        temporary_index = index.with_suffix(index.suffix + ".restore.tmp")
        temporary_index.write_text(text, encoding="utf-8")
        os.replace(temporary_index, index)
        target.commit()
        if logs_target is not None:
            logs_target.commit()
        if auxiliary:
            auxiliary_restore_started = True
            StorageRegistry(root).restore_selected(
                set(ids), backup / "auxiliary", auxiliary
            )
        operation_succeeded = True
        return HistoryRestoreResult(ids, int(restored_log_rows))
    except Exception as original_error:
        target.rollback()
        if logs_target is not None:
            logs_target.rollback()
        try:
            if state_snapshot.is_file():
                _restore_snapshot(state_snapshot, target)
            if logs_target is not None and logs_snapshot.is_file():
                _restore_snapshot(logs_snapshot, logs_target)
            for path in moved:
                path.unlink(missing_ok=True)
            if original_index is None:
                index.unlink(missing_ok=True)
            else:
                index.write_bytes(original_index)
            if auxiliary_restore_started:
                StorageRegistry(root).delete_additional(set(ids))
            recovery_succeeded = True
        except Exception as recovery_error:
            snapshots = [
                str(path)
                for path in (state_snapshot, logs_snapshot)
                if path.is_file()
            ]
            raise BackupSafetyError(
                "任务恢复失败且自动回滚失败；救援快照已保留："
                + ", ".join(snapshots)
                + f"。回滚错误：{recovery_error}"
            ) from original_error
        raise
    finally:
        if logs_source is not None:
            logs_source.close()
        if logs_target is not None:
            logs_target.close()
        metadata.close()
        target.close()
        if operation_succeeded or recovery_succeeded:
            state_snapshot.unlink(missing_ok=True)
            logs_snapshot.unlink(missing_ok=True)
