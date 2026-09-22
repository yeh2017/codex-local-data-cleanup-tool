import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_cleanup_tool import main as main_module
from codex_cleanup_tool.i18n import ENGLISH, Translator
from codex_cleanup_tool.startup import (
    StartupLog,
    activate_existing_window,
    check_python_version,
    mutex_name,
)


class StartupTests(unittest.TestCase):
    def test_startup_translator_uses_saved_language(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary)
            settings = local / "CodexLocalCleanupTool" / "settings.json"
            settings.parent.mkdir()
            settings.write_text('{"language": "en"}', encoding="utf-8")

            translator = main_module.startup_translator(
                {"LOCALAPPDATA": str(local)}, Path(temporary)
            )

        self.assertEqual(translator.language, ENGLISH)
        self.assertEqual(translator("正在检查运行环境并加载界面..."), "Checking the runtime and loading the interface...")

    def test_existing_english_window_can_be_activated(self):
        searched = []

        def find_window(_class, title):
            searched.append(title)
            return 42 if title == "Codex Local Data Cleanup Tool v1.1.0" else 0

        user32 = SimpleNamespace(
            FindWindowW=find_window,
            ShowWindow=lambda *_args: None,
            BringWindowToTop=lambda *_args: None,
            SetForegroundWindow=lambda *_args: True,
            FlashWindow=lambda *_args: None,
        )

        self.assertTrue(activate_existing_window(user32))
        self.assertIn("Codex Local Data Cleanup Tool v1.1.0", searched)

    def test_startup_error_is_written_to_user_log_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = StartupLog(Path(temporary))

            path = log.write("启动失败", RuntimeError("模拟错误"))

            text = path.read_text(encoding="utf-8")
            self.assertIn("启动失败", text)
            self.assertIn("模拟错误", text)
            self.assertEqual(path.parent, Path(temporary) / "logs")

    def test_python_version_check_rejects_old_interpreter(self):
        with self.assertRaisesRegex(RuntimeError, "Python 3.10"):
            check_python_version((3, 9, 9))

        check_python_version((3, 10, 0))

    def test_startup_module_can_be_parsed_by_python_39(self):
        source = (
            Path(__file__).resolve().parents[1] / "codex_cleanup_tool" / "startup.py"
        ).read_text(encoding="utf-8")

        ast.parse(source, feature_version=(3, 9))
        self.assertNotIn(" | None", source)

    def test_native_error_is_shown_even_when_log_write_fails(self):
        with (
            patch.object(main_module, "check_python_version", side_effect=RuntimeError("旧版本")),
            patch.object(main_module.StartupLog, "write", side_effect=PermissionError("不可写")),
            patch.object(main_module, "show_native_error") as show_error,
        ):
            self.assertEqual(main_module.main(), 1)

        show_error.assert_called_once()
        self.assertIn("旧版本", show_error.call_args.args[0])

    def test_startup_error_title_is_translated_in_english_mode(self):
        with (
            patch.object(main_module, "startup_translator", return_value=Translator(ENGLISH)),
            patch.object(main_module, "check_python_version", side_effect=RuntimeError("失败")),
            patch.object(main_module.StartupLog, "write", return_value=None),
            patch.object(main_module, "show_native_error") as show_error,
        ):
            self.assertEqual(main_module.main(), 1)

        self.assertEqual(
            show_error.call_args.args[1],
            "Codex Local Data Cleanup Tool Startup Failed",
        )

    def test_failed_foreground_activation_flashes_existing_window(self):
        calls = []
        user32 = SimpleNamespace(
            FindWindowW=lambda _class, _title: 42,
            ShowWindow=lambda handle, mode: calls.append(("show", handle, mode)),
            BringWindowToTop=lambda handle: calls.append(("top", handle)),
            SetForegroundWindow=lambda handle: False,
            FlashWindow=lambda handle, invert: calls.append(("flash", handle, invert)),
        )

        self.assertTrue(activate_existing_window(user32))
        self.assertIn(("flash", 42, True), calls)

    def test_frozen_application_directory_is_executable_parent(self):
        executable = Path(r"C:\Portable\CodexCleanup\CodexLocalCleanupTool.exe")

        result = main_module.application_directory(
            module_file=Path(r"C:\Portable\CodexCleanup\_internal\codex_cleanup_tool\main.py"),
            executable=executable,
            frozen=True,
        )

        self.assertEqual(result, executable.parent)

    def test_mutex_name_is_global_and_scoped_to_user_profile(self):
        first = mutex_name(Path(r"C:\Users\First"))
        second = mutex_name(Path(r"C:\Users\Second"))

        self.assertTrue(first.startswith("Global\\CodexLocalCleanupTool-"))
        self.assertNotEqual(first, second)

    def test_other_session_instance_failure_is_reported(self):
        lock = SimpleNamespace(acquire=lambda: False, close=lambda: None)
        with (
            patch.object(main_module, "SingleInstanceLock", return_value=lock),
            patch.object(main_module, "activate_existing_window", return_value=False),
            patch.object(main_module, "show_native_error") as show_error,
            patch.object(main_module.time, "sleep"),
        ):
            result = main_module.main()

        self.assertEqual(result, 2)
        show_error.assert_called_once()
        self.assertIn("其他 Windows 会话", show_error.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
