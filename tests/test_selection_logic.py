import unittest
import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

from codex_cleanup_tool.gui import (
    CleanupApp,
    can_delete_history,
    calculate_space_totals,
    compatibility_status_text,
    format_history_updated_at,
    is_category_deletable,
    summarize_history_selection,
    summarize_selection,
)
from codex_cleanup_tool.history import HistoryRecord
from codex_cleanup_tool.i18n import ENGLISH, LANGUAGE_LABELS, SIMPLIFIED_CHINESE, Translator
from codex_cleanup_tool.models import ScanItem, ScanSummary, format_size
from codex_cleanup_tool.storage_registry import (
    CompatibilityReport,
    CompatibilityStatus,
)


def make_item(key: str, size: int, files: int = 1) -> ScanItem:
    return ScanItem(
        key=key,
        label=key,
        description="",
        paths=(Path(key),),
        file_count=files,
        folder_count=0,
        total_bytes=size,
    )


class SelectionLogicTests(unittest.TestCase):
    def test_privacy_mode_does_not_require_backup_directory(self):
        self.assertTrue(can_delete_history(1, False, False, "privacy"))
        self.assertFalse(can_delete_history(1, False, False, "safe"))

    def test_compatibility_status_text_explains_partial_support(self):
        report = CompatibilityReport(CompatibilityStatus.PARTIAL, (), ())

        self.assertEqual(
            compatibility_status_text(report),
            "兼容性：部分支持（检测到结构差异，删除前会再次校验）",
        )

    def test_language_change_is_saved_and_rebuilds_interface(self):
        app = CleanupApp.__new__(CleanupApp)
        app.busy = False
        app.language = SIMPLIFIED_CHINESE
        app.translator = Translator(SIMPLIFIED_CHINESE)
        app.root = MagicMock()
        app.language_var = MagicMock()
        app.language_var.get.return_value = LANGUAGE_LABELS[ENGLISH]
        app.settings = {}
        app.settings_path = Path("settings.json")
        app.selected_keys = {"cache"}
        app.selected_history_ids = {"thread-id"}
        app._rebuild_localized_ui = MagicMock()

        with patch("codex_cleanup_tool.gui.save_settings") as save:
            CleanupApp._on_language_changed(app)

        self.assertEqual(app.language, ENGLISH)
        self.assertEqual(app.settings["language"], ENGLISH)
        self.assertEqual(app.translator("开始扫描"), "Scan")
        self.assertEqual(app.selected_keys, set())
        self.assertEqual(app.selected_history_ids, set())
        save.assert_called_once_with(app.settings_path, app.settings)
        app._rebuild_localized_ui.assert_called_once_with()

    def test_language_change_rolls_back_when_settings_cannot_be_saved(self):
        app = CleanupApp.__new__(CleanupApp)
        app.busy = False
        app.language = SIMPLIFIED_CHINESE
        app.translator = Translator(SIMPLIFIED_CHINESE)
        app.root = MagicMock()
        app.language_var = MagicMock()
        app.language_var.get.return_value = LANGUAGE_LABELS[ENGLISH]
        app.settings = {}
        app.settings_path = Path("settings.json")
        app.selected_keys = {"cache"}
        app.selected_history_ids = {"thread-id"}
        app._rebuild_localized_ui = MagicMock()

        with (
            patch(
                "codex_cleanup_tool.gui.save_settings",
                side_effect=PermissionError("不可写"),
            ),
            patch("codex_cleanup_tool.gui.messagebox.showerror") as show_error,
        ):
            CleanupApp._on_language_changed(app)

        self.assertEqual(app.language, SIMPLIFIED_CHINESE)
        self.assertNotIn("language", app.settings)
        self.assertEqual(app.selected_keys, {"cache"})
        self.assertEqual(app.selected_history_ids, {"thread-id"})
        app.language_var.set.assert_called_once_with(
            LANGUAGE_LABELS[SIMPLIFIED_CHINESE]
        )
        show_error.assert_called_once()
        app._rebuild_localized_ui.assert_not_called()

    def test_language_change_is_blocked_while_operation_is_running(self):
        app = CleanupApp.__new__(CleanupApp)
        app.busy = True
        app.language = SIMPLIFIED_CHINESE
        app.translator = Translator(SIMPLIFIED_CHINESE)
        app.root = MagicMock()
        app.language_var = MagicMock()
        app.language_var.get.return_value = LANGUAGE_LABELS[ENGLISH]
        app.settings = {}
        app.settings_path = Path("settings.json")
        app._rebuild_localized_ui = MagicMock()

        with patch("codex_cleanup_tool.gui.messagebox.showwarning") as warning:
            CleanupApp._on_language_changed(app)

        self.assertEqual(app.language, SIMPLIFIED_CHINESE)
        app.language_var.set.assert_called_once_with(
            LANGUAGE_LABELS[SIMPLIFIED_CHINESE]
        )
        warning.assert_called_once()
        app._rebuild_localized_ui.assert_not_called()

    def test_session_categories_must_be_deleted_from_history_tab(self):
        self.assertFalse(is_category_deletable("sessions"))
        self.assertFalse(is_category_deletable("archived_sessions"))
        self.assertFalse(is_category_deletable("logs"))
        self.assertFalse(is_category_deletable("session_index"))

    def test_busy_window_cannot_be_closed_during_write_operation(self):
        app = CleanupApp.__new__(CleanupApp)
        app.busy = True
        app.root = MagicMock()

        with patch("codex_cleanup_tool.gui.messagebox.showwarning") as warning:
            CleanupApp._on_close(app)

        warning.assert_called_once()
        app.root.destroy.assert_not_called()

    def test_idle_window_can_be_closed(self):
        app = CleanupApp.__new__(CleanupApp)
        app.busy = False
        app.root = MagicMock()

        CleanupApp._on_close(app)

        app.root.destroy.assert_called_once_with()

    def test_history_timestamp_accepts_unix_seconds_and_iso_text(self):
        self.assertRegex(format_history_updated_at(1786108538), r"^20\d\d-\d\d-\d\d ")
        self.assertEqual(
            format_history_updated_at("2026-08-07T21:15:38Z"),
            "2026-08-07 21:15:38",
        )

    def test_history_selection_counts_only_existing_records(self):
        records = (
            HistoryRecord("one", "第一条", "", False, Path("one.jsonl"), 100),
            HistoryRecord("two", "第二条", "", True, Path("two.jsonl"), 40),
        )

        count, size = summarize_history_selection(records, {"two", "missing"})

        self.assertEqual((count, size), (1, 40))

    def test_history_selection_deduplicates_parent_and_visible_child(self):
        parent = HistoryRecord(
            "parent",
            "父任务",
            "",
            False,
            Path("parent.jsonl"),
            160,
            (("parent", 100), ("child", 60)),
        )
        child = HistoryRecord(
            "child",
            "子任务",
            "",
            False,
            Path("child.jsonl"),
            60,
            (("child", 60),),
        )

        count, size = summarize_history_selection(
            (parent, child), {"parent", "child"}
        )

        self.assertEqual((count, size), (2, 160))

    def test_selection_total_contains_only_selected_items(self):
        items = (make_item("sessions", 100), make_item("logs", 40, files=3))

        categories, files, size = summarize_selection(items, {"logs"})

        self.assertEqual((categories, files, size), (1, 3, 40))

    def test_space_totals_distinguish_root_reclaimable_and_selected(self):
        items = (make_item("sessions", 100), make_item("cache", 40))
        summary = ScanSummary(Path(".codex"), items, root_total_bytes=500)
        records = (
            HistoryRecord("one", "第一条", "", False, Path("one.jsonl"), 60),
        )

        result = calculate_space_totals(summary, {"cache"}, records, {"one"})

        self.assertEqual(result, (500, 140, 100))

    def test_reclaimable_space_uses_only_log_internal_free_bytes(self):
        items = (make_item("logs", 90), make_item("cache", 40))
        summary = ScanSummary(Path(".codex"), items, root_total_bytes=500)
        diagnostics = MagicMock(free_bytes=20)

        result = calculate_space_totals(
            summary, set(), (), set(), log_diagnostics=diagnostics
        )

        self.assertEqual(result, (500, 60, 0))

    def test_missing_or_empty_items_are_not_counted(self):
        empty = ScanItem("cache", "cache", "", (), 0, 0, 0)

        result = summarize_selection((empty,), {"cache", "missing"})

        self.assertEqual(result, (0, 0, 0))

    def test_format_size_uses_readable_units(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(1536), "1.50 KB")
        self.assertEqual(format_size(2 * 1024 * 1024), "2.00 MB")

    def test_start_scan_clears_old_summary_before_background_scan(self):
        app = CleanupApp.__new__(CleanupApp)
        app.busy = False
        app.path_var = MagicMock()
        app.path_var.get.return_value = r"C:\temp\.codex"
        app.selected_keys = {"sessions"}
        app._clear_table = MagicMock()
        app._set_busy = MagicMock()
        app._scan_worker = MagicMock()

        with patch("codex_cleanup_tool.gui.threading.Thread") as thread:
            CleanupApp.start_scan(app)

        app._clear_table.assert_called_once_with()
        thread.assert_called_once()

    def test_using_detected_path_waits_for_manual_scan(self):
        app = CleanupApp.__new__(CleanupApp)
        app.path_var = MagicMock()
        app.status_var = MagicMock()
        app.settings = {}
        app.settings_path = Path("settings.json")
        app._clear_table = MagicMock()
        app.start_scan = MagicMock()
        detected = Path(r"C:\Users\Example\.codex")

        with patch("codex_cleanup_tool.gui.save_settings"):
            CleanupApp._use_path(app, detected)

        app.path_var.set.assert_called_once_with(str(detected))
        app._clear_table.assert_called_once_with()
        app.status_var.set.assert_called_once_with("目录已就绪，请点击“开始扫描”。")
        app.start_scan.assert_not_called()

    def test_scan_worker_returns_categories_history_and_log_diagnostics(self):
        app = CleanupApp.__new__(CleanupApp)
        app.events = queue.Queue()
        summary = MagicMock()
        history = (MagicMock(),)
        all_history = history + (MagicMock(),)
        diagnostics = MagicMock()
        compatibility = MagicMock()

        with (
            patch("codex_cleanup_tool.gui.scan_codex_home", return_value=summary),
            patch(
                "codex_cleanup_tool.gui.scan_history_records",
                side_effect=(history, all_history),
            ),
            patch("codex_cleanup_tool.gui.inspect_logs", return_value=diagnostics),
            patch("codex_cleanup_tool.gui.StorageRegistry") as registry,
        ):
            registry.return_value.inspect.return_value = compatibility
            registry.return_value.managed_paths.return_value = set()
            registry.return_value.unmanaged_references.return_value = ()
            CleanupApp._scan_worker(app, Path(r"C:\Users\Example\.codex"))

        event, payload = app.events.get_nowait()
        self.assertEqual(event, "scan_ok")
        self.assertEqual(payload, (summary, history, diagnostics, None, compatibility))
        registry.return_value.inspect.assert_called_once_with(
            {record.id for record in all_history}
        )

    def test_scan_worker_preserves_log_diagnostic_error_message(self):
        app = CleanupApp.__new__(CleanupApp)
        app.events = queue.Queue()

        with (
            patch("codex_cleanup_tool.gui.scan_codex_home", return_value=MagicMock()),
            patch("codex_cleanup_tool.gui.scan_history_records", side_effect=((), ())),
            patch(
                "codex_cleanup_tool.gui.inspect_logs",
                side_effect=ValueError("数据库损坏"),
            ),
            patch("codex_cleanup_tool.gui.StorageRegistry") as registry,
        ):
            registry.return_value.inspect.return_value = MagicMock()
            registry.return_value.managed_paths.return_value = set()
            registry.return_value.unmanaged_references.return_value = ()
            CleanupApp._scan_worker(app, Path(r"C:\Users\Example\.codex"))

        event, payload = app.events.get_nowait()
        self.assertEqual(event, "scan_ok")
        self.assertIsNone(payload[2])
        self.assertIn("数据库损坏", payload[3])

    def test_privacy_delete_worker_uses_permanent_purge(self):
        app = CleanupApp.__new__(CleanupApp)
        app.events = queue.Queue()
        app.selected_history_ids = {"thread-1"}
        result = MagicMock(
            deleted_ids=("thread-1",),
            deleted_references=8,
        )

        with patch(
            "codex_cleanup_tool.gui.privacy_purge_history", return_value=result
        ) as purge:
            CleanupApp._history_delete_worker(
                app, Path(r"C:\Users\Example\.codex"), {"thread-1"}, "privacy"
            )

        purge.assert_called_once()
        event, message = app.events.get_nowait()
        self.assertEqual(event, "history_delete_ok")
        self.assertIn("永久清除", message)
        self.assertIn("不会创建备份", message)

    def test_log_optimization_worker_is_not_daemonized(self):
        app = CleanupApp.__new__(CleanupApp)
        app.busy = False
        app.log_diagnostics = MagicMock()
        app.retention_var = MagicMock()
        app.retention_var.get.return_value = "30"
        app.path_var = MagicMock()
        app.path_var.get.return_value = r"C:\Users\Example\.codex"
        app._set_busy = MagicMock()
        app._log_optimize_worker = MagicMock()

        with (
            patch("codex_cleanup_tool.gui.messagebox.askyesno", return_value=True),
            patch("codex_cleanup_tool.gui.threading.Thread") as thread,
        ):
            CleanupApp.confirm_log_optimization(app)

        self.assertFalse(thread.call_args.kwargs["daemon"])

    def test_log_growth_check_uses_selected_interval(self):
        app = CleanupApp.__new__(CleanupApp)
        app.busy = False
        app.log_diagnostics = MagicMock()
        app.log_interval_var = MagicMock()
        app.log_interval_var.get.return_value = "30"
        app.path_var = MagicMock()
        app.path_var.get.return_value = r"C:\Users\Example\.codex"
        app.log_growth_var = MagicMock()
        app.root = MagicMock()
        app._set_busy = MagicMock()
        app._log_growth_worker = MagicMock()

        with patch("codex_cleanup_tool.gui.threading.Thread") as thread:
            CleanupApp.start_log_growth_check(app)

        self.assertEqual(thread.call_args.kwargs["args"][1], 30)
        self.assertIn("30", app.log_growth_var.set.call_args.args[0])
        self.assertEqual(len(thread.call_args.kwargs["args"]), 3)
        self.assertFalse(thread.call_args.kwargs["args"][2].is_set())

    def test_log_growth_cancel_sets_worker_event(self):
        app = CleanupApp.__new__(CleanupApp)
        app.log_growth_cancel_event = __import__("threading").Event()
        app.log_growth_cancel_button = MagicMock()
        app.log_growth_var = MagicMock()

        CleanupApp.cancel_log_growth_check(app)

        self.assertTrue(app.log_growth_cancel_event.is_set())
        app.log_growth_cancel_button.configure.assert_called_once_with(state="disabled")


if __name__ == "__main__":
    unittest.main()
