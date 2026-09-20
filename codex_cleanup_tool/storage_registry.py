import json
import os
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
        "threads": ("id",),
        "thread_dynamic_tools": ("thread_id",),
        "thread_spawn_edges": ("parent_thread_id", "child_thread_id"),
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


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _placeholders(values: set[str]) -> str:
    return ",".join("?" for _ in values)


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
            if not _contains_id(child, ids)
        ]
    return value


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
                where = " OR ".join(
                    f"{_quote(column)} IN ({_placeholders(ids)})"
                    for column in columns
                )
                parameters = tuple(sorted(ids)) * len(columns)
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

    def _inspect_unknown_sqlite(
        self, path: Path, ids: set[str]
    ) -> list[StorageReference]:
        references: list[StorageReference] = []
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
                for column in connection.execute(f"PRAGMA table_info({_quote(table)})"):
                    column_name = column[1]
                    try:
                        count = connection.execute(
                            f"SELECT COUNT(*) FROM {_quote(table)} "
                            f"WHERE CAST({_quote(column_name)} AS TEXT) "
                            f"IN ({_placeholders(ids)})",
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

    def inspect(self, ids: set[str]) -> CompatibilityReport:
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
            partial = partial or not supported

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
                raw = path.read_bytes() if path.is_file() else b""
                count = sum(raw.count(item.encode("utf-8")) for item in ids)
                if count:
                    unknown.append(
                        StorageReference(path, "invalid_global_state", count, str(exc))
                    )
                else:
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
                where = " OR ".join(
                    f"{_quote(column)} IN ({_placeholders(ids)})"
                    for column in columns
                )
                parameters = tuple(sorted(ids)) * len(columns)
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
        before = self.inspect(ids)
        if before.unknown_references:
            names = ", ".join(str(item.path) for item in before.unknown_references)
            raise StorageCompatibilityError(f"发现未知任务引用：{names}")

        deleted = 0
        for name, tables in SQLITE_STORES.items():
            path = self.root / name
            if path.is_file():
                deleted += self._delete_sqlite(path, tables, ids, secure)

        for path in self._global_state_paths():
            value = json.loads(path.read_text(encoding="utf-8"))
            count = _count_json_references(value, ids)
            if not count:
                continue
            content = json.dumps(
                _purge_json_references(value, ids), ensure_ascii=False, separators=(",", ":")
            )
            temporary = path.with_name(path.name + ".cleanup.tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
            deleted += count

        lock_root = self.root / "thread-writer-locks"
        for item in ids:
            path = lock_root / f"{item}.lock"
            if path.is_file():
                path.unlink()
                deleted += 1

        return StorageDeleteResult(deleted)
