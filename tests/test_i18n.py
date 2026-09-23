import ast
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cleanup_tool.i18n import (
    ENGLISH,
    SIMPLIFIED_CHINESE,
    Translator,
    detect_system_language,
    resolve_language,
)


class I18nTests(unittest.TestCase):
    def test_windows_display_language_takes_priority_over_region_format(self):
        class Kernel32:
            def GetUserDefaultLocaleName(self, buffer, _size):
                buffer.value = "zh-CN"
                return 1

            def GetUserPreferredUILanguages(
                self, _flags, language_count, buffer, buffer_size
            ):
                language_count._obj.value = 1
                if buffer is None:
                    buffer_size._obj.value = len("en-US\0\0")
                    return 0
                else:
                    buffer.value = "en-US"
                return 1

        with (
            patch("codex_cleanup_tool.i18n.os.name", "nt"),
            patch("codex_cleanup_tool.i18n.ctypes.windll.kernel32", Kernel32()),
        ):
            self.assertEqual(detect_system_language(), ENGLISH)

    def test_all_user_interface_literals_have_english_text(self):
        project_root = Path(__file__).resolve().parents[1]
        translator = Translator(ENGLISH)
        untranslated = []
        for relative in (
            "codex_cleanup_tool/gui.py",
            "codex_cleanup_tool/history.py",
            "codex_cleanup_tool/history_backup.py",
            "codex_cleanup_tool/log_maintenance.py",
            "codex_cleanup_tool/main.py",
            "codex_cleanup_tool/privacy.py",
            "codex_cleanup_tool/recycle_bin.py",
            "codex_cleanup_tool/scanner.py",
            "codex_cleanup_tool/startup.py",
            "codex_cleanup_tool/storage_registry.py",
        ):
            source = (project_root / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if re.search(r"[\u4e00-\u9fff]", node.value) and re.search(
                    r"[\u4e00-\u9fff]", translator(node.value)
                ):
                    untranslated.append((relative, node.lineno, node.value))

        self.assertEqual(untranslated, [])

    def test_saved_language_has_priority(self):
        self.assertEqual(resolve_language(ENGLISH, SIMPLIFIED_CHINESE), ENGLISH)

    def test_unsupported_saved_language_falls_back_to_system_language(self):
        self.assertEqual(resolve_language("fr", SIMPLIFIED_CHINESE), SIMPLIFIED_CHINESE)

    def test_non_chinese_windows_language_defaults_to_english(self):
        with patch(
            "codex_cleanup_tool.i18n._windows_ui_language_name",
            return_value="en-US",
        ):
            self.assertEqual(detect_system_language(), ENGLISH)

    def test_chinese_windows_language_uses_chinese(self):
        with patch(
            "codex_cleanup_tool.i18n._windows_ui_language_name",
            return_value="zh-CN",
        ):
            self.assertEqual(detect_system_language(), SIMPLIFIED_CHINESE)

    def test_english_translator_handles_static_and_formatted_text(self):
        translator = Translator(ENGLISH)

        self.assertEqual(translator("开始扫描"), "Scan")
        self.assertEqual(
            translator("扫描完成：找到 {count} 条历史记录", count=3),
            "Scan complete: found 3 history records",
        )

    def test_chinese_translator_preserves_original_text(self):
        translator = Translator(SIMPLIFIED_CHINESE)

        self.assertEqual(translator("开始扫描"), "开始扫描")
        self.assertEqual(
            translator("已选择 {count} 条历史记录", count=2),
            "已选择 2 条历史记录",
        )


if __name__ == "__main__":
    unittest.main()
