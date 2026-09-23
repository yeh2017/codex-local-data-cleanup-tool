import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class StorageCompatibilityError(ValueError):
    pass


class CompatibilityStatus(str, Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class StorageReference:
    path: Path
    store: str
    count: int
    detail: str = ""


@dataclass(frozen=True)
class CompatibilityReport:
    status: CompatibilityStatus
    references: tuple[StorageReference, ...]
    unknown_references: tuple[StorageReference, ...]

    @property
    def total_references(self) -> int:
        return sum(item.count for item in self.references) + sum(
            item.count for item in self.unknown_references
        )


@dataclass(frozen=True)
class StorageDeleteResult:
    deleted_references: int


SQLITE_STORES = {
    "state_5.sqlite": {
        "thread_spawn_edges": ("parent_thread_id", "child_thread_id"),
        "thread_dynamic_tools": ("thread_id",),
        "thread_attachments": ("thread_id",),
        "threads": ("id",),
    },
    "logs_2.sqlite": {"logs": ("thread_id",)},
    "thread_history_1.sqlite": {
        "thread_items": ("thread_id",),
        "thread_realtime_items": ("thread_id",),
        "thread_turns": ("thread_id",),
        "thread_history_projection_state": ("thread_id",),
    },
    "queue_1.sqlite": {
        "queued_items": ("thread_id",),
        "queued_thread_revisions": ("thread_id",),
    },
}

SUBSTRING_SQLITE_REFERENCES = {
    "logs_2.sqlite": {"logs": ("feedback_log_body",)},
}

AUXILIARY_SQLITE_STORES = {
    name: tables
    for name, tables in SQLITE_STORES.items()
    if name in {"thread_history_1.sqlite", "queue_1.sqlite"}
}

NULLABLE_SQLITE_REFERENCES = {
    "state_5.sqlite": {
        "rollout_migration_state": ("last_checked_thread_id",),
    }
}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _placeholders(values: set[str]) -> str:
    return ",".join("?" for _ in values)


def sqlite_reference_where(
    database_name: str,
    table: str,
    exact_columns: tuple[str, ...],
    available_columns: set[str],
    ids: set[str],
) -> tuple[str, tuple[str, ...]]:
    ordered_ids = tuple(sorted(ids))
    clauses = []
    parameters: list[str] = []
    for column in exact_columns:
        if column not in available_columns:
            continue
        clauses.append(f"{_quote(column)} IN ({_placeholders(ids)})")
        parameters.extend(ordered_ids)
    for column in SUBSTRING_SQLITE_REFERENCES.get(database_name, {}).get(table, ()):
        if column not in available_columns:
            continue
        clauses.append(
            "(" + " OR ".join(
                f"instr(CAST({_quote(column)} AS TEXT), ?) > 0" for _ in ids
            ) + ")"
        )
        parameters.extend(ordered_ids)
    return " OR ".join(clauses), tuple(parameters)


def _contains_id(value: object, ids: set[str]) -> bool:
    return isinstance(value, str) and any(item in value for item in ids)


def _count_json_references(value: object, ids: set[str]) -> int:
    if isinstance(value, dict):
        return sum(
            int(_contains_id(str(key), ids)) + _count_json_references(child, ids)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return sum(_count_json_references(child, ids) for child in value)
    return int(_contains_id(value, ids))


def _purge_json_references(value: object, ids: set[str]) -> object:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if _contains_id(str(key), ids) or _contains_id(child, ids):
                continue
            result[key] = _purge_json_references(child, ids)
        return result
    if isinstance(value, list):
        return [
            _purge_json_references(child, ids)
            for child in value
            if not _count_json_references(child, ids)
        ]
    return value


def _collect_json_fragments(
    value: object, ids: set[str], path: tuple[object, ...] = ()
) -> list[dict]:
    fragments: list[dict] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if _contains_id(str(key), ids) or _contains_id(child, ids):
                fragments.append(
                    {"kind": "dict", "path": list(path), "key": key, "value": child}
                )
            else:
                fragments.extend(_collect_json_fragments(child, ids, path + (key,)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if _count_json_references(child, ids):
                fragments.append(
                    {"kind": "list", "path": list(path), "value": child}
                )
            else:
                fragments.extend(
                    _collect_json_fragments(child, ids, path + (index,))
                )
    return fragments


def _navigate_json(value: object, path: list[object]) -> object:
    current = value
    for component in path:
        if isinstance(component, int) and isinstance(current, list):
            current = current[component]
        elif isinstance(component, str) and isinstance(current, dict):
            current = current.setdefault(component, {})
        else:
            raise StorageCompatibilityError("全局状态备份路径已经不兼容")
    return current


def _restore_json_fragments(value: object, fragments: list[dict]) -> object:
    for fragment in fragments:
        parent = _navigate_json(value, fragment["path"])
        if fragment["kind"] == "dict" and isinstance(parent, dict):
            key = fragment["key"]
            if key in parent and parent[key] != fragment["value"]:
                raise StorageCompatibilityError(f"全局状态中已存在冲突字段：{key}")
            parent[key] = fragment["value"]
        elif fragment["kind"] == "list" and isinstance(parent, list):
            if fragment["value"] not in parent:
                parent.append(fragment["value"])
        else:
            raise StorageCompatibilityError("全局状态备份片段无效")
    return value


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_stale_global_state_temp(path: Path) -> bool:
    return path.name.startswith("..codex-global-state.json.tmp-")


def _count_raw_references(path: Path, ids: set[str]) -> int:
    raw = path.read_bytes()
    return sum(raw.count(item.encode("utf-8")) for item in ids)


def _safe_backup_path(root: Path, value: object) -> Path:
    relative = Path(str(value))
    if (
        relative.anchor
        or relative.drive
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise StorageCompatibilityError(f"辅助备份包含不安全路径：{value}")
    destination = (Path(root).resolve() / relative).resolve()
    if not destination.is_relative_to(Path(root).resolve()):
        raise StorageCompatibilityError(f"辅助备份包含不安全路径：{value}")
    return destination


def _validate_global_state_name(value: object) -> str:
    name = str(value)
    allowed = name in {
        ".codex-global-state.json",
        ".codex-global-state.json.bak",
    } or name.startswith("..codex-global-state.json.tmp-")
    if not allowed or Path(name).name != name:
        raise StorageCompatibilityError(f"全局状态备份文件名无效：{name}")
    return name


class StorageRegistry:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    def _global_state_paths(self) -> tuple[Path, ...]:
        paths = {
            self.root / ".codex-global-state.json",
            self.root / ".codex-global-state.json.bak",
            *self.root.glob("..codex-global-state.json.tmp-*"),
        }
        return tuple(sorted((path for path in paths if path.is_file()), key=str))

    def unmanaged_references(
        self, ids: set[str], managed_paths: set[Path]
    ) -> tuple[StorageReference, ...]:
        ids = {str(item) for item in ids if item}
        managed = {Path(path).resolve() for path in managed_paths}
        encoded = tuple(item.encode("utf-8") for item in ids)
        references = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if resolved in managed or path.suffix == ".sqlite":
                continue
            count = 0
            try:
                with path.open("rb") as stream:
                    carry = b""
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        data = carry + chunk
                        count += sum(data.count(item) for item in encoded)
                        carry = data[-64:]
            except OSError as exc:
                references.append(
                    StorageReference(path, "unreadable_file", 1, str(exc))
                )
                continue
            if count:
                references.append(
                    StorageReference(path, "unknown_file", count)
                )
        return tuple(references)

    def managed_paths(self, ids: set[str]) -> set[Path]:
        paths: set[Path] = set(self._global_state_paths())
        for name in SQLITE_STORES:
            database = self.root / name
            paths.update(
                {
                    database,
                    database.with_name(database.name + "-wal"),
                    database.with_name(database.name + "-shm"),
                }
            )
        paths.add(self.root / "session_index.jsonl")
        paths.add(self.root / "thread-writer-locks" / ".coordination.lock")
        paths.update(self.root.glob(".codex-provisioning-*.guard"))
        paths.update(
            self.root / "thread-writer-locks" / f"{item}.lock" for item in ids
        )
        return {path.resolve() for path in paths}

    def _inspect_known_sqlite(
        self, path: Path, tables: dict[str, tuple[str, ...]], ids: set[str]
    ) -> tuple[list[StorageReference], bool]:
        references: list[StorageReference] = []
        schema_supported = True
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table, columns in tables.items():
                if table not in existing_tables:
                    schema_supported = False
                    continue
                existing_columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({_quote(table)})"
                    )
                }
                if any(column not in existing_columns for column in columns):
                    schema_supported = False
                    continue
                where, parameters = sqlite_reference_where(
                    path.name, table, columns, existing_columns, ids
                )
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {_quote(table)} WHERE {where}", parameters
                ).fetchone()[0]
                if count:
                    references.append(
                        StorageReference(path, path.name, int(count), table)
                    )
        finally:
            connection.close()
        return references, schema_supported

    def _inspect_nullable_references(
        self, path: Path, ids: set[str]
    ) -> list[StorageReference]:
        references = []
        if not ids:
            return references
        tables = NULLABLE_SQLITE_REFERENCES.get(path.name, {})
        if not tables:
            return references
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table, columns in tables.items():
                if table not in existing:
                    continue
                for column in columns:
                    count = connection.execute(
                        f"SELECT COUNT(*) FROM {_quote(table)} "
                        f"WHERE {_quote(column)} IN ({_placeholders(ids)})",
                        tuple(sorted(ids)),
                    ).fetchone()[0]
                    if count:
                        references.append(
                            StorageReference(
                                path,
                                "nullable_sqlite_reference",
                                int(count),
                                f"{table}.{column}",
                            )
                        )
        finally:
            connection.close()
        return references

    def _inspect_unknown_sqlite(
        self,
        path: Path,
        ids: set[str],
        excluded_tables: set[str] | None = None,
    ) -> list[StorageReference]:
        references: list[StorageReference] = []
        if not ids:
            return references
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                if excluded_tables and table in excluded_tables:
                    continue
                for column in connection.execute(f"PRAGMA table_info({_quote(table)})"):
                    column_name = column[1]
                    try:
                        where = " OR ".join(
                            f"instr(CAST({_quote(column_name)} AS TEXT), ?) > 0"
                            for _ in ids
                        )
                        count = connection.execute(
                            f"SELECT COUNT(*) FROM {_quote(table)} WHERE {where}",
                            tuple(sorted(ids)),
                        ).fetchone()[0]
                    except sqlite3.Error:
                        continue
                    if count:
                        references.append(
                            StorageReference(
                                path,
                                "unknown_sqlite",
                                int(count),
                                f"{table}.{column_name}",
                            )
                        )
        finally:
            connection.close()
        return references

    def _inspect_retained_known_rows(
        self,
        path: Path,
        tables: dict[str, tuple[str, ...]],
        ids: set[str],
    ) -> list[StorageReference]:
        if not ids:
            return []
        references = []
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table, identifier_columns in tables.items():
                if table not in existing_tables:
                    continue
                columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({_quote(table)})"
                    )
                }
                handled = [
                    column for column in identifier_columns if column in columns
                ]
                if not handled:
                    continue
                selected, selected_parameters = sqlite_reference_where(
                    path.name, table, identifier_columns, columns, ids
                )
                substring_columns = set(
                    SUBSTRING_SQLITE_REFERENCES.get(path.name, {}).get(table, ())
                )
                for column in sorted(columns.difference(handled).difference(substring_columns)):
                    contains = " OR ".join(
                        f"instr(CAST({_quote(column)} AS TEXT), ?) > 0"
                        for _ in ids
                    )
                    count = connection.execute(
                        f"SELECT COUNT(*) FROM {_quote(table)} "
                        f"WHERE NOT ({selected}) AND ({contains})",
                        selected_parameters + tuple(sorted(ids)),
                    ).fetchone()[0]
                    if count:
                        references.append(
                            StorageReference(
                                path,
                                "unknown_known_sqlite",
                                int(count),
                                f"{table}.{column}",
                            )
                        )
        finally:
            connection.close()
        return references

    def inspect(
        self, ids: set[str], *, strict: bool = False
    ) -> CompatibilityReport:
        ids = {str(item) for item in ids if item}
        references: list[StorageReference] = []
        unknown: list[StorageReference] = []
        partial = False

        for name, tables in SQLITE_STORES.items():
            path = self.root / name
            if not path.is_file():
                continue
            try:
                found, supported = self._inspect_known_sqlite(path, tables, ids)
            except sqlite3.Error as exc:
                unknown.append(
                    StorageReference(path, "unreadable_sqlite", 1, str(exc))
                )
                continue
            references.extend(found)
            references.extend(self._inspect_nullable_references(path, ids))
            if strict:
                unknown.extend(
                    self._inspect_retained_known_rows(path, tables, ids)
                )
            partial = partial or not supported
            try:
                excluded = set(tables) | set(
                    NULLABLE_SQLITE_REFERENCES.get(name, {})
                )
                unknown.extend(
                    self._inspect_unknown_sqlite(path, ids, excluded)
                )
            except sqlite3.Error as exc:
                partial = True
                unknown.append(
                    StorageReference(path, "unreadable_sqlite", 1, str(exc))
                )

        for path in sorted(self.root.glob("*.sqlite")):
            if path.name in SQLITE_STORES:
                continue
            try:
                unknown.extend(self._inspect_unknown_sqlite(path, ids))
            except sqlite3.Error as exc:
                partial = True
                unknown.append(
                    StorageReference(path, "unreadable_sqlite", 1, str(exc))
                )

        for path in self._global_state_paths():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                count = _count_raw_references(path, ids) if path.is_file() else 0
                if count:
                    reference = StorageReference(
                        path,
                        "stale_global_state_temp"
                        if _is_stale_global_state_temp(path)
                        else "invalid_global_state",
                        count,
                        str(exc),
                    )
                    if _is_stale_global_state_temp(path):
                        references.append(reference)
                    else:
                        unknown.append(reference)
                elif not _is_stale_global_state_temp(path):
                    partial = True
                continue
            count = _count_json_references(value, ids)
            if count:
                references.append(
                    StorageReference(path, "global_state", count)
                )

        lock_root = self.root / "thread-writer-locks"
        for item in ids:
            path = lock_root / f"{item}.lock"
            if path.is_file():
                references.append(StorageReference(path, "thread_lock", 1))

        if unknown:
            status = CompatibilityStatus.UNSUPPORTED
        elif partial:
            status = CompatibilityStatus.PARTIAL
        else:
            status = CompatibilityStatus.SUPPORTED
        return CompatibilityReport(status, tuple(references), tuple(unknown))

    def export_selected(self, ids: set[str], destination: Path) -> dict:
        ids = {str(item) for item in ids if item}
        target_root = Path(destination)
        target_root.mkdir(parents=True, exist_ok=True)
        databases = []
        for name, tables in AUXILIARY_SQLITE_STORES.items():
            source_path = self.root / name
            if not source_path.is_file():
                continue
            target_path = target_root / name
            source = sqlite3.connect(
                f"file:{source_path.as_posix()}?mode=ro", uri=True
            )
            target = sqlite3.connect(target_path)
            try:
                for table, columns in tables.items():
                    schema_row = source.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                    if schema_row is None:
                        raise StorageCompatibilityError(
                            f"不支持的数据表结构：{name}/{table}"
                        )
                    target.execute(schema_row[0])
                    source_columns = [
                        row[1]
                        for row in source.execute(f"PRAGMA table_info({_quote(table)})")
                    ]
                    where = " OR ".join(
                        f"{_quote(column)} IN ({_placeholders(ids)})"
                        for column in columns
                    )
                    parameters = tuple(sorted(ids)) * len(columns)
                    rows = source.execute(
                        f"SELECT * FROM {_quote(table)} WHERE {where}", parameters
                    ).fetchall()
                    if rows:
                        target.executemany(
                            f"INSERT INTO {_quote(table)} "
                            f"({','.join(_quote(column) for column in source_columns)}) "
                            f"VALUES ({','.join('?' for _ in source_columns)})",
                            rows,
                        )
                target.commit()
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StorageCompatibilityError(
                        f"辅助历史备份完整性检查失败：{name}"
                    )
            finally:
                target.close()
                source.close()
            databases.append({"name": name, "sha256": _hash(target_path)})

        states = []
        raw_states = []
        for path in self._global_state_paths():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if (
                    _is_stale_global_state_temp(path)
                    and _count_raw_references(path, ids)
                ):
                    raw_root = target_root / "raw-global-state"
                    raw_root.mkdir(exist_ok=True)
                    target = raw_root / path.name
                    shutil.copy2(path, target)
                    raw_states.append(
                        {
                            "name": path.name,
                            "relative_path": target.relative_to(target_root).as_posix(),
                            "sha256": _hash(target),
                        }
                    )
                continue
            fragments = _collect_json_fragments(value, ids)
            if fragments:
                states.append({"name": path.name, "fragments": fragments})
        state_path = target_root / "global_state_fragments.json"
        state_path.write_text(
            json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        nullable_rows = []
        state_database = self.root / "state_5.sqlite"
        if state_database.is_file():
            connection = sqlite3.connect(
                f"file:{state_database.as_posix()}?mode=ro", uri=True
            )
            try:
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='rollout_migration_state'"
                ).fetchone()
                if table_exists:
                    nullable_rows = [
                        {
                            "migration_id": row[0],
                            "last_checked_thread_created_at": row[1],
                            "last_checked_thread_id": row[2],
                            "updated_at": row[3],
                        }
                        for row in connection.execute(
                            "SELECT migration_id, last_checked_thread_created_at, "
                            "last_checked_thread_id, updated_at "
                            "FROM rollout_migration_state "
                            f"WHERE last_checked_thread_id IN ({_placeholders(ids)})",
                            tuple(sorted(ids)),
                        )
                    ]
            finally:
                connection.close()
        nullable_path = target_root / "nullable_references.json"
        nullable_path.write_text(
            json.dumps(nullable_rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "databases": databases,
            "global_state": {
                "name": state_path.name,
                "sha256": _hash(state_path),
            },
            "raw_global_state": raw_states,
            "nullable_references": {
                "name": nullable_path.name,
                "sha256": _hash(nullable_path),
            },
        }

    def verify_export(self, backup_root: Path, metadata: dict) -> None:
        backup_root = Path(backup_root)
        for item in metadata.get("databases", ()):
            if item.get("name") not in AUXILIARY_SQLITE_STORES:
                raise StorageCompatibilityError(
                    f"辅助备份包含不安全路径：{item.get('name')}"
                )
            path = _safe_backup_path(backup_root, item["name"])
            if not path.is_file() or _hash(path) != item["sha256"]:
                raise StorageCompatibilityError(
                    f"辅助历史备份校验失败：{item['name']}"
                )
            connection = sqlite3.connect(path)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StorageCompatibilityError(
                        f"辅助历史备份数据库损坏：{item['name']}"
                    )
            finally:
                connection.close()
        state = metadata.get("global_state")
        if state:
            if state.get("name") != "global_state_fragments.json":
                raise StorageCompatibilityError("全局状态备份文件名无效")
            path = _safe_backup_path(backup_root, state["name"])
            if not path.is_file() or _hash(path) != state["sha256"]:
                raise StorageCompatibilityError("全局状态备份校验失败")
        for item in metadata.get("raw_global_state", ()):
            name = _validate_global_state_name(item.get("name"))
            expected = (Path("raw-global-state") / name).as_posix()
            if Path(str(item.get("relative_path"))).as_posix() != expected:
                raise StorageCompatibilityError(
                    f"辅助备份包含不安全路径：{item.get('relative_path')}"
                )
            path = _safe_backup_path(backup_root, item["relative_path"])
            if not path.is_file() or _hash(path) != item["sha256"]:
                raise StorageCompatibilityError("全局状态备份校验失败")
        nullable = metadata.get("nullable_references")
        if nullable:
            if nullable.get("name") != "nullable_references.json":
                raise StorageCompatibilityError("可空引用备份文件名无效")
            path = _safe_backup_path(backup_root, nullable["name"])
            if not path.is_file() or _hash(path) != nullable["sha256"]:
                raise StorageCompatibilityError("可空引用备份校验失败")

    def restore_selected(self, ids: set[str], backup_root: Path, metadata: dict) -> int:
        ids = {str(item) for item in ids if item}
        backup_root = Path(backup_root)
        self.verify_export(backup_root, metadata)
        restored = 0
        for item in metadata.get("databases", ()):
            name = item["name"]
            target_path = self.root / name
            if not target_path.is_file():
                raise StorageCompatibilityError(f"缺少目标数据库：{target_path}")
            source = sqlite3.connect(backup_root / name)
            target = sqlite3.connect(target_path, timeout=30)
            try:
                target.execute("BEGIN IMMEDIATE")
                for table in AUXILIARY_SQLITE_STORES[name]:
                    source_columns = [
                        row[1]
                        for row in source.execute(f"PRAGMA table_info({_quote(table)})")
                    ]
                    rows = source.execute(f"SELECT * FROM {_quote(table)}").fetchall()
                    if not rows:
                        continue
                    target_columns = {
                        row[1]
                        for row in target.execute(f"PRAGMA table_info({_quote(table)})")
                    }
                    if any(column not in target_columns for column in source_columns):
                        raise StorageCompatibilityError(
                            f"目标数据库结构不兼容：{name}/{table}"
                        )
                    target.executemany(
                        f"INSERT INTO {_quote(table)} "
                        f"({','.join(_quote(column) for column in source_columns)}) "
                        f"VALUES ({','.join('?' for _ in source_columns)})",
                        rows,
                    )
                    restored += len(rows)
                target.commit()
            except Exception:
                target.rollback()
                raise
            finally:
                target.close()
                source.close()

        state_metadata = metadata.get("global_state")
        if state_metadata:
            states = json.loads(
                _safe_backup_path(backup_root, state_metadata["name"]).read_text(
                    encoding="utf-8"
                )
            )
            for item in states:
                path = self.root / _validate_global_state_name(item.get("name"))
                current = (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.is_file()
                    else {}
                )
                updated = _restore_json_fragments(current, item["fragments"])
                temporary = path.with_name(path.name + ".restore.tmp")
                temporary.write_text(
                    json.dumps(updated, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(temporary, path)
                restored += len(item["fragments"])
        for item in metadata.get("raw_global_state", ()):
            source = _safe_backup_path(backup_root, item["relative_path"])
            target = self.root / _validate_global_state_name(item.get("name"))
            if target.is_file():
                if _hash(target) != item["sha256"]:
                    raise StorageCompatibilityError(
                        f"全局状态中已存在冲突文件：{target}"
                    )
                continue
            shutil.copy2(source, target)
            restored += 1
        nullable = metadata.get("nullable_references")
        if nullable:
            rows = json.loads(
                _safe_backup_path(backup_root, nullable["name"]).read_text(
                    encoding="utf-8"
                )
            )
            if rows:
                database = sqlite3.connect(self.root / "state_5.sqlite", timeout=30)
                try:
                    database.execute("BEGIN IMMEDIATE")
                    for row in rows:
                        current = database.execute(
                            "SELECT last_checked_thread_id "
                            "FROM rollout_migration_state WHERE migration_id=?",
                            (row["migration_id"],),
                        ).fetchone()
                        if current and current[0] not in (
                            None,
                            row["last_checked_thread_id"],
                        ):
                            raise StorageCompatibilityError(
                                "迁移状态中已存在冲突任务引用"
                            )
                        if current:
                            database.execute(
                                "UPDATE rollout_migration_state SET "
                                "last_checked_thread_created_at=?, "
                                "last_checked_thread_id=?, updated_at=? "
                                "WHERE migration_id=?",
                                (
                                    row["last_checked_thread_created_at"],
                                    row["last_checked_thread_id"],
                                    row["updated_at"],
                                    row["migration_id"],
                                ),
                            )
                        else:
                            database.execute(
                                "INSERT INTO rollout_migration_state "
                                "(migration_id, last_checked_thread_created_at, "
                                "last_checked_thread_id, updated_at) VALUES (?, ?, ?, ?)",
                                (
                                    row["migration_id"],
                                    row["last_checked_thread_created_at"],
                                    row["last_checked_thread_id"],
                                    row["updated_at"],
                                ),
                            )
                        restored += 1
                    database.commit()
                except Exception:
                    database.rollback()
                    raise
                finally:
                    database.close()
        return restored

    def _clear_nullable_references(
        self, ids: set[str], *, secure: bool = False
    ) -> int:
        path = self.root / "state_5.sqlite"
        if not path.is_file():
            return 0
        connection = sqlite3.connect(path, timeout=30)
        try:
            if secure:
                connection.execute("PRAGMA secure_delete = ON")
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='rollout_migration_state'"
            ).fetchone()
            if not table_exists:
                return 0
            cursor = connection.execute(
                "UPDATE rollout_migration_state SET "
                "last_checked_thread_created_at=NULL, last_checked_thread_id=NULL "
                f"WHERE last_checked_thread_id IN ({_placeholders(ids)})",
                tuple(sorted(ids)),
            )
            connection.commit()
            if secure:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            return max(0, cursor.rowcount)
        finally:
            connection.close()

    def _delete_global_states(self, ids: set[str]) -> int:
        deleted = 0
        for path in self._global_state_paths():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                count = _count_raw_references(path, ids)
                if count and _is_stale_global_state_temp(path):
                    path.unlink()
                    deleted += count
                elif count:
                    raise StorageCompatibilityError(
                        f"发现未知任务引用：{path}"
                    )
                continue
            count = _count_json_references(value, ids)
            if not count:
                continue
            content = json.dumps(
                _purge_json_references(value, ids),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            temporary = path.with_name(path.name + ".cleanup.tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
            deleted += count
        return deleted

    def _delete_sqlite(
        self,
        path: Path,
        tables: dict[str, tuple[str, ...]],
        ids: set[str],
        secure: bool,
    ) -> int:
        deleted = 0
        connection = sqlite3.connect(path, timeout=30)
        try:
            if secure:
                connection.execute("PRAGMA secure_delete = ON")
            connection.execute("BEGIN IMMEDIATE")
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table, columns in tables.items():
                if table not in existing_tables:
                    continue
                existing_columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({_quote(table)})"
                    )
                }
                if any(column not in existing_columns for column in columns):
                    raise StorageCompatibilityError(
                        f"不支持的数据表结构：{path.name}/{table}"
                    )
                where, parameters = sqlite_reference_where(
                    path.name, table, columns, existing_columns, ids
                )
                cursor = connection.execute(
                    f"DELETE FROM {_quote(table)} WHERE {where}", parameters
                )
                deleted += max(0, cursor.rowcount)
            connection.commit()
            if secure:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StorageCompatibilityError(
                        f"数据库完整性检查失败：{path}"
                    )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return deleted

    def delete_known(self, ids: set[str], *, secure: bool = False) -> StorageDeleteResult:
        ids = {str(item) for item in ids if item}
        before = self.inspect(ids, strict=True)
        if before.unknown_references:
            names = ", ".join(str(item.path) for item in before.unknown_references)
            raise StorageCompatibilityError(f"发现未知任务引用：{names}")

        deleted = 0
        for name, tables in SQLITE_STORES.items():
            path = self.root / name
            if path.is_file():
                deleted += self._delete_sqlite(path, tables, ids, secure)

        deleted += self._clear_nullable_references(ids, secure=secure)

        deleted += self._delete_global_states(ids)

        lock_root = self.root / "thread-writer-locks"
        for item in ids:
            path = lock_root / f"{item}.lock"
            if path.is_file():
                path.unlink()
                deleted += 1

        return StorageDeleteResult(deleted)

    def delete_additional(
        self, ids: set[str], *, secure: bool = False
    ) -> StorageDeleteResult:
        ids = {str(item) for item in ids if item}
        deleted = 0
        for name, tables in AUXILIARY_SQLITE_STORES.items():
            path = self.root / name
            if path.is_file():
                deleted += self._delete_sqlite(path, tables, ids, secure)
        deleted += self._clear_nullable_references(ids, secure=secure)
        deleted += self._delete_global_states(ids)
        lock_root = self.root / "thread-writer-locks"
        for item in ids:
            path = lock_root / f"{item}.lock"
            if path.is_file():
                path.unlink()
                deleted += 1
        return StorageDeleteResult(deleted)
