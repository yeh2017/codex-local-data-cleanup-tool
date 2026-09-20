import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .history import (
    _expand_descendant_ids,
    _index_without_ids,
    _replace_bytes,
    is_codex_running,
    scan_history_records,
)
from .path_detection import is_codex_home
from .storage_registry import StorageRegistry


class PrivacyPurgeError(ValueError):
    pass


@dataclass(frozen=True)
class PrivacyPurgeResult:
    deleted_ids: tuple[str, ...]
    deleted_references: int
    journal_path: Path


PROTECTED_NAMES = {
    "auth.json",
    "config.toml",
    "plugins",
    "skills",
    "vendor_imports",
    "installation_id",
}


def _contains_any(path: Path, ids: set[str]) -> int:
    count = 0
    encoded = tuple(item.encode("utf-8") for item in ids)
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
        raise PrivacyPurgeError(f"无法检查受保护文件：{path}（{exc}）") from exc
    return count


def _protected_references(root: Path, ids: set[str]) -> tuple[Path, ...]:
    matches = []
    for name in PROTECTED_NAMES:
        path = root / name
        if path.is_file() and _contains_any(path, ids):
            matches.append(path)
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and _contains_any(child, ids):
                    matches.append(child)
    return tuple(matches)


def _journal_path(root: Path, ids: set[str]) -> Path:
    digest = hashlib.sha256("\0".join(sorted(ids)).encode("utf-8")).hexdigest()[:16]
    return root / f".cleanup-privacy-{digest}.json"


def _write_journal(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def privacy_purge_history(
    root: Path,
    selected_ids: set[str],
    *,
    require_codex_closed: bool = True,
    codex_running_check=is_codex_running,
) -> PrivacyPurgeResult:
    root = Path(root).expanduser().resolve()
    if not is_codex_home(root):
        raise PrivacyPurgeError(f"不是有效的 Codex 数据目录：{root}")
    ids = {str(item) for item in selected_ids if item}
    if not ids:
        raise PrivacyPurgeError("没有选择需要永久清除的历史记录")
    if require_codex_closed and codex_running_check():
        raise PrivacyPurgeError("请完全退出 Codex 桌面程序后再永久清除历史记录。")

    database = root / "state_5.sqlite"
    ids = _expand_descendant_ids(database, ids)
    journal = _journal_path(root, ids)
    if journal.is_file():
        payload = json.loads(journal.read_text(encoding="utf-8"))
        rollout_paths = tuple(Path(item) for item in payload.get("rollout_paths", ()))
    else:
        records = {
            record.id: record
            for record in scan_history_records(root, include_internal=True)
        }
        rollout_paths = tuple(
            records[item].rollout_path for item in sorted(ids) if item in records
        )
        payload = {
            "version": 1,
            "ids": sorted(ids),
            "rollout_paths": [str(path) for path in rollout_paths],
            "completed_steps": [],
        }

    protected = _protected_references(root, ids)
    if protected:
        raise PrivacyPurgeError(
            "受保护文件中存在任务引用：" + ", ".join(str(path) for path in protected)
        )

    registry = StorageRegistry(root)
    report = registry.inspect(ids)
    if report.unknown_references:
        details = ", ".join(
            f"{item.path} ({item.detail or item.store})"
            for item in report.unknown_references
        )
        raise PrivacyPurgeError(f"发现未知任务引用：{details}")
    managed_paths = registry.managed_paths(ids) | set(rollout_paths) | {
        journal,
        journal.with_name(journal.name + ".tmp"),
    }
    unmanaged = registry.unmanaged_references(ids, managed_paths)
    if unmanaged:
        details = ", ".join(str(item.path) for item in unmanaged)
        raise PrivacyPurgeError(f"发现未知任务引用：{details}")

    database_sizes = [
        path.stat().st_size
        for path in root.glob("*.sqlite")
        if path.is_file()
    ]
    required_space = (max(database_sizes) if database_sizes else 0) + 64 * 1024 * 1024
    available_space = shutil.disk_usage(root).free
    if available_space < required_space:
        raise PrivacyPurgeError(
            f"磁盘空间不足：至少需要 {required_space} 字节，当前可用 {available_space} 字节"
        )

    _write_journal(journal, payload)
    deleted = 0
    completed = set(payload.get("completed_steps", ()))
    if "databases" not in completed:
        deleted += registry.delete_known(ids, secure=True).deleted_references
        completed.add("databases")
        payload["completed_steps"] = sorted(completed)
        _write_journal(journal, payload)

    if "index" not in completed:
        index = root / "session_index.jsonl"
        rewritten = _index_without_ids(index, ids)
        if rewritten is not None:
            _replace_bytes(index, rewritten)
        completed.add("index")
        payload["completed_steps"] = sorted(completed)
        _write_journal(journal, payload)

    if "rollouts" not in completed:
        for path in rollout_paths:
            if path.is_file():
                path.unlink()
                deleted += 1
        completed.add("rollouts")
        payload["completed_steps"] = sorted(completed)
        _write_journal(journal, payload)

    remaining = registry.inspect(ids)
    unmanaged_remaining = registry.unmanaged_references(ids, managed_paths)
    index = root / "session_index.jsonl"
    index_has_ids = bool(
        index.is_file()
        and any(item in index.read_text(encoding="utf-8", errors="ignore") for item in ids)
    )
    remaining_rollouts = tuple(path for path in rollout_paths if path.exists())
    if (
        remaining.total_references
        or unmanaged_remaining
        or index_has_ids
        or remaining_rollouts
    ):
        raise PrivacyPurgeError("永久清除后的验证失败，仍存在本地任务引用")

    journal.unlink(missing_ok=True)
    return PrivacyPurgeResult(tuple(sorted(ids)), deleted, journal)
