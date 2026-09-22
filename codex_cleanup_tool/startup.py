import ctypes
import hashlib
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

from .version import APP_NAME_ZH, display_version
from .i18n import ENGLISH, Translator


WINDOW_TITLE = f"{APP_NAME_ZH} {display_version()}"
WINDOW_TITLES = (
    WINDOW_TITLE,
    f"{Translator(ENGLISH)(APP_NAME_ZH)} {display_version()}",
)


def mutex_name(user_home: Optional[Path] = None) -> str:
    home = Path(user_home) if user_home is not None else Path.home()
    identity = str(home.expanduser().resolve()).casefold().encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return f"Global\\CodexLocalCleanupTool-{digest}"


def user_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "CodexLocalCleanupTool"


class StartupLog:
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else user_data_dir()

    def write(self, message: str, error: Optional[BaseException] = None) -> Path:
        log_dir = self.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "startup.log"
        detail = "" if error is None else "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n{detail}\n")
        return path


def check_python_version(version=None) -> None:
    current = tuple(version or sys.version_info[:3])
    if current < (3, 10, 0):
        raise RuntimeError("需要 Python 3.10 或更高版本。")


def show_native_error(
    message: str,
    title: str = "Codex 本地数据清理工具启动失败",
) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)


def activate_existing_window(user32=None) -> bool:
    if user32 is None and os.name != "nt":
        return False
    user32 = user32 or ctypes.windll.user32
    handle = 0
    for title in WINDOW_TITLES:
        handle = user32.FindWindowW(None, title)
        if handle:
            break
    if not handle:
        return False
    user32.ShowWindow(handle, 9)
    user32.BringWindowToTop(handle)
    activated = bool(user32.SetForegroundWindow(handle))
    if not activated:
        user32.FlashWindow(handle, True)
    return True


class SingleInstanceLock:
    def __init__(self, name: Optional[str] = None):
        self.name = name or mutex_name()
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "无法创建单实例锁")
        return ctypes.get_last_error() != 183

    def close(self) -> None:
        if self.handle and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.CloseHandle(self.handle)
            self.handle = None
