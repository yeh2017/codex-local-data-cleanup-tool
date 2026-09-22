import os
import sys
import time
from pathlib import Path

from .i18n import Translator, resolve_language
from .path_detection import load_settings, stable_settings_path
from .startup import (
    SingleInstanceLock,
    StartupLog,
    WINDOW_TITLE,
    activate_existing_window,
    check_python_version,
    show_native_error,
)
from .version import APP_NAME_ZH, display_version


def application_directory(
    module_file=None,
    executable=None,
    frozen=None,
) -> Path:
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return Path(executable or sys.executable).resolve().parent
    return Path(module_file or __file__).resolve().parent.parent


def startup_translator(env=None, user_home=None) -> Translator:
    environment = os.environ if env is None else env
    home = Path.home() if user_home is None else Path(user_home)
    settings = load_settings(stable_settings_path(environment, home))
    return Translator(resolve_language(settings.get("language")))


def run_startup_check() -> int:
    check_python_version()
    import tkinter as tk
    from .gui import CleanupApp

    if CleanupApp is None:
        raise RuntimeError("主界面模块加载失败。")

    root = tk.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
    finally:
        root.destroy()
    return 0


def main() -> int:
    translator = startup_translator()
    log = StartupLog()
    lock = SingleInstanceLock()
    try:
        check_python_version()
        if "--startup-check" in sys.argv:
            return run_startup_check()
        if not lock.acquire():
            for attempt in range(10):
                if activate_existing_window():
                    return 0
                if attempt < 9:
                    time.sleep(0.2)
            show_native_error(
                translator("工具已经在其他 Windows 会话中运行，请先关闭该实例后再试。"),
                translator("Codex 本地数据清理工具启动失败"),
            )
            return 2

        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.withdraw()
        splash = tk.Toplevel(root)
        splash.title(translator(WINDOW_TITLE))
        splash.geometry("520x112")
        splash.resizable(False, False)
        ttk.Label(
            splash,
            text=f"{translator(APP_NAME_ZH)} {display_version()}",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(pady=(22, 6))
        ttk.Label(
            splash, text=translator("正在检查运行环境并加载界面...")
        ).pack()
        splash.update_idletasks()
        x = (splash.winfo_screenwidth() - splash.winfo_width()) // 2
        y = (splash.winfo_screenheight() - splash.winfo_height()) // 2
        splash.geometry(f"+{x}+{y}")
        splash.update()

        from .gui import CleanupApp

        CleanupApp(root, application_directory())

        def show_main_window():
            if splash.winfo_exists():
                splash.destroy()
            root.deiconify()
            root.lift()

        root.after(350, show_main_window)
        root.mainloop()
        return 0
    except Exception as exc:
        log_path = None
        try:
            log_path = log.write("启动失败", exc)
        except Exception:
            pass
        message = translator("程序启动失败。\n\n") + translator(str(exc))
        if log_path:
            message += f"\n\n{translator('日志：')}{log_path}"
        try:
            show_native_error(
                message,
                translator("Codex 本地数据清理工具启动失败"),
            )
        except Exception:
            pass
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
