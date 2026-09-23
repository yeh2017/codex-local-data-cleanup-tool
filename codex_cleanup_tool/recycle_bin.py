import base64
import os
import subprocess
from pathlib import Path
from typing import Callable

from .models import RecycleResult
from .path_detection import is_codex_home
from .scanner import CATEGORY_SPECS, _is_link_like


POWERSHELL_RECYCLE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName Microsoft.VisualBasic
$Target = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($env:CODEX_RECYCLE_TARGET_B64)
)
$ui = [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs
$recycle = [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
$cancel = [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException
if ([System.IO.Directory]::Exists($Target)) {
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($Target, $ui, $recycle, $cancel)
} elseif ([System.IO.File]::Exists($Target)) {
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($Target, $ui, $recycle, $cancel)
} else {
    throw "Target does not exist: $Target"
}
"""


class SafetyError(ValueError):
    pass


def build_windows_path_list(paths: tuple[Path, ...]) -> str:
    return "\0".join(str(path) for path in paths) + "\0\0"


def _category_patterns(category_key: str) -> tuple[str, ...]:
    for spec in CATEGORY_SPECS:
        if spec.key == category_key:
            return spec.patterns
    raise SafetyError(f"未知的清理类别：{category_key}")


def validate_targets(
    root: Path, category_key: str, paths: tuple[Path, ...]
) -> tuple[Path, ...]:
    root = Path(root).expanduser().resolve()
    if not is_codex_home(root):
        raise SafetyError(f"Codex 数据目录身份验证失败：{root}")
    patterns = _category_patterns(category_key)
    allowed: dict[str, Path] = {}
    for pattern in patterns:
        for match in root.glob(pattern):
            if _is_link_like(match):
                continue
            resolved = match.resolve()
            allowed[os.path.normcase(str(resolved))] = resolved

    validated: list[Path] = []
    for raw_path in paths:
        raw = Path(os.path.abspath(Path(raw_path).expanduser()))
        if _is_link_like(raw):
            raise SafetyError(f"禁止处理符号链接或目录联接：{raw}")
        path = raw.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SafetyError(f"目标位于 Codex 目录之外：{path}") from exc
        if path == root:
            raise SafetyError("禁止处理 Codex 根目录")
        key = os.path.normcase(str(path))
        if key not in allowed:
            raise SafetyError(f"目标不属于类别“{category_key}”的白名单：{path}")
        if not path.exists():
            raise SafetyError(f"目标已不存在：{path}")
        validated.append(path)
    if not validated:
        raise SafetyError("没有可处理的目标")
    return tuple(dict.fromkeys(validated))


def _windows_recycle_operation(paths: tuple[Path, ...]) -> tuple[int, bool]:
    if os.name != "nt":
        raise RuntimeError("回收站功能仅支持 Windows")
    if len(paths) != 1:
        raise ValueError("回收站接口每次只处理一个目标")
    path = paths[0]
    command = "& { " + POWERSHELL_RECYCLE_SCRIPT + " }"
    environment = os.environ.copy()
    environment["CODEX_RECYCLE_TARGET_B64"] = base64.b64encode(
        str(path).encode("utf-8")
    ).decode("ascii")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=600,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(detail or f"PowerShell 返回错误代码 {completed.returncode}")
    if path.exists():
        raise RuntimeError("回收站操作结束后目标仍然存在")
    return 0, False


class RecycleBinClient:
    def __init__(
        self,
        operation: Callable[[tuple[Path, ...]], tuple[int, bool]] | None = None,
    ):
        self._operation = operation or _windows_recycle_operation

    def recycle(self, paths: tuple[Path, ...]) -> RecycleResult:
        if not paths:
            raise ValueError("没有选择要移入回收站的目标")
        paths = tuple(Path(path) for path in paths)
        succeeded: list[Path] = []
        failed: list[tuple[Path, str]] = []
        any_aborted = False
        for path in paths:
            try:
                result_code, aborted = self._operation((path,))
            except Exception as exc:
                failed.append((path, f"回收站接口调用失败：{exc}"))
                continue
            any_aborted = any_aborted or aborted
            if result_code == 0 and not aborted:
                succeeded.append(path)
            else:
                reason = (
                    "用户或系统中止了操作"
                    if aborted
                    else f"Windows Shell 返回错误代码 {result_code}"
                )
                failed.append((path, reason))
        return RecycleResult(tuple(succeeded), tuple(failed), any_aborted)
