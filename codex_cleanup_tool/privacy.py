import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .history import (
    HistorySafetyError,
    _expand_descendant_ids,
    _index_without_ids,
    _replace_bytes,
    _validate_rollout_path,
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


def _load_matching_journal(
    root: Path, requested_ids: set[str]
) -> tuple[Path, dict] | None:
    matches = []
    for path in root.glob(".cleanup-privacy-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            journal_ids = {str(item) for item in payload.get("ids", ()) if item}
        except (OSError, ValueError, AttributeError) as exc:
            raise PrivacyPurgeError(f"隐私清除日志无效：{path}（{exc}）") from exc
        stored_request = payload.get("requested_ids")
        if stored_request is None:
            if journal_ids == requested_ids:
                matches.append((path, payload))
            elif requested_ids.issubset(journal_ids):
                raise PrivacyPurgeError("隐私清除日志与本次选择范围不一致")
            continue
        journal_request = {str(item) for item in stored_request if item}
        if journal_request != requested_ids:
            continue
        if path.resolve() != _journal_path(root, requested_ids).resolve():
            raise PrivacyPurgeError("隐私清除日志文件名与选择范围不一致")
        matches.append((path, payload))
    if len(matches) > 1:
        raise PrivacyPurgeError("发现多个匹配的隐私清除日志，无法安全继续")
    return matches[0] if matches else None


def _validate_journal_rollouts(
    root: Path, ids: set[str], values: list[object]
) -> tuple[Path, ...]:
    paths = []
    for value in values:
        raw_path = Path(str(value)).expanduser()
        matches = [
            item for item in ids if raw_path.name.endswith(f"{item}.jsonl")
        ]
        if len(matches) != 1:
            raise PrivacyPurgeError(f"隐私清除日志包含不安全路径：{raw_path}")
        try:
            paths.append(_validate_rollout_path(root, matches[0], raw_path))
        except HistorySafetyError as exc:
            raise PrivacyPurgeError(
                f"隐私清除日志包含不安全路径：{raw_path}"
            ) from exc
    return tuple(paths)


def _discover_rollout_paths(root: Path, ids: set[str]) -> tuple[Path, ...]:
    paths = []
    for area in ("sessions", "archived_sessions"):
        directory = root / area
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.jsonl"):
            matches = [item for item in ids if path.name.endswith(f"{item}.jsonl")]
            if len(matches) == 1:
                try:
                    paths.append(_validate_rollout_path(root, matches[0], path))
                except HistorySafetyError as exc:
                    raise PrivacyPurgeError(
                        f"隐私清除日志包含不安全路径：{path}"
                    ) from exc
    return tuple(sorted(set(paths), key=str))


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
    requested_ids = {str(item) for item in selected_ids if item}
    if not requested_ids:
        raise PrivacyPurgeError("没有选择需要永久清除的历史记录")
    if require_codex_closed and codex_running_check():
        raise PrivacyPurgeError("请完全退出 Codex 桌面程序后再永久清除历史记录。")

    database = root / "state_5.sqlite"
    existing_journal = _load_matching_journal(root, requested_ids)
    if existing_journal:
        journal, payload = existing_journal
        ids = {str(item) for item in payload.get("ids", ()) if item}
        if "rollout_paths" in payload:
            rollout_paths = _validate_journal_rollouts(
                root, ids, list(payload.get("rollout_paths", ()))
            )
        else:
            rollout_paths = _discover_rollout_paths(root, ids)
    else:
        ids = _expand_descendant_ids(database, requested_ids)
        journal = _journal_path(root, requested_ids)
        records = {
            record.id: record
            for record in scan_history_records(root, include_internal=True)
        }
        rollout_paths = tuple(
            records[item].rollout_path for item in sorted(ids) if item in records
        )
        payload = {
            "version": 2,
            "requested_ids": sorted(requested_ids),
            "ids": sorted(ids),
            "completed_steps": [],
        }

    protected = _protected_references(root, ids)
    if protected:
        raise PrivacyPurgeError(
            "受保护文件中存在任务引用：" + ", ".join(str(path) for path in protected)
        )

    index = root / "session_index.jsonl"
    rewritten_index = _index_without_ids(index, ids)
    if rewritten_index is not None and any(
        item.encode("utf-8") in rewritten_index for item in ids
    ):
        raise PrivacyPurgeError("任务索引包含无法安全移除的任务引用")

    registry = StorageRegistry(root)
    report = registry.inspect(ids, strict=True)
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
        if rewritten_index is not None:
            _replace_bytes(index, rewritten_index)
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

    remaining = registry.inspect(ids, strict=True)
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
