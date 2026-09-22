import sqlite3
import tempfile
import threading
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from codex_cleanup_tool.log_maintenance import (
    LogGrowth,
    LogGrowthCancelled,
    LogSafetyError,
    classify_log_growth,
    find_recovery_backups,
    inspect_logs,
    optimize_logs,
    preview_log_cleanup,
    sample_log_growth,
)


def create_log_home(base: Path) -> Path:
    root = base / ".codex"
    root.mkdir()
    (root / "installation_id").write_text("test", encoding="utf-8")
    (root / "state_5.sqlite").write_bytes(b"")
    database = root / "logs_2.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                level TEXT NOT NULL,
                message TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
            (
                (100, "TRACE", "old trace"),
                (200, "INFO", "old info"),
                (900_000, "TRACE", "recent trace"),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return root


class LogMaintenanceTests(unittest.TestCase):
    def test_growth_rates_are_normalized_per_minute(self):
        growth = LogGrowth(10, 1024 * 1024, 20, 20, 10, 20, 0)

        self.assertEqual(growth.rows_per_minute, 120)
        self.assertEqual(growth.trace_rows_per_minute, 60)
        self.assertEqual(growth.bytes_per_minute, 6 * 1024 * 1024)
        self.assertEqual(growth.new_trace_ratio, 0.5)
        self.assertEqual(classify_log_growth(growth), "active")

    def test_growth_classification_detects_idle_and_high_frequency(self):
        idle = LogGrowth(10, 0, 0, 0, 0, 0, 0)
        high_rows = LogGrowth(10, 0, 1001, 1001, 0, 1001, 0)
        high_bytes = LogGrowth(10, 2 * 1024 * 1024, 1, 1, 0, 1, 0)
        high_trace = LogGrowth(10, 0, 120, 120, 110, 120, 0)

        self.assertEqual(classify_log_growth(idle), "idle")
        self.assertEqual(classify_log_growth(high_rows), "high")
        self.assertEqual(classify_log_growth(high_bytes), "high")
        self.assertEqual(classify_log_growth(high_trace), "high")

    def test_inspect_reports_rows_trace_ratio_and_sidecar_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))

            result = inspect_logs(root)

            self.assertEqual(result.row_count, 3)
            self.assertEqual(result.trace_count, 2)
            self.assertAlmostEqual(result.trace_ratio, 2 / 3)
            self.assertEqual(result.oldest_ts, 100)
            self.assertEqual(result.newest_ts, 900_000)
            self.assertGreater(result.total_bytes, 0)
            self.assertGreaterEqual(result.free_bytes, 0)

    def test_cleanup_preview_counts_expired_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))

            result = preview_log_cleanup(
                root, retention_days=5, now=lambda: 1_000_000
            )

            self.assertEqual(result.expired_rows, 2)
            self.assertEqual(result.estimated_bytes, 0)

    def test_recovery_backups_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))
            backup = root / ".cleanup-logs-interrupted.sqlite"
            backup.write_bytes(b"backup")

            self.assertEqual(find_recovery_backups(root), (backup,))

    def test_growth_sample_compares_database_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))

            def write_during_sample(_seconds: float):
                connection = sqlite3.connect(root / "logs_2.sqlite")
                try:
                    connection.execute(
                        "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
                        (900_001, "TRACE", "new"),
                    )
                    connection.commit()
                finally:
                    connection.close()

            growth = sample_log_growth(root, interval_seconds=0.1, sleep=write_during_sample)

            self.assertEqual(growth.rows_delta, 1)
            self.assertEqual(growth.new_rows, 1)
            self.assertEqual(growth.new_trace_rows, 1)
            self.assertEqual(growth.interval_seconds, 0.1)

    def test_growth_sample_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))
            cancelled = threading.Event()
            cancelled.set()

            with self.assertRaises(LogGrowthCancelled):
                sample_log_growth(root, interval_seconds=30, cancel_event=cancelled)

    def test_growth_detects_new_rows_when_old_rows_are_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))

            def replace_during_sample(_seconds: float):
                connection = sqlite3.connect(root / "logs_2.sqlite")
                try:
                    connection.execute("DELETE FROM logs WHERE id = 1")
                    connection.execute(
                        "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
                        (900_002, "TRACE", "replacement"),
                    )
                    connection.commit()
                finally:
                    connection.close()

            growth = sample_log_growth(root, sleep=replace_during_sample)

            self.assertEqual(growth.rows_delta, 0)
            self.assertEqual(growth.new_rows, 1)
            self.assertEqual(growth.new_trace_rows, 1)
            self.assertGreater(growth.max_id_delta, 0)

    def test_optimize_retains_recent_rows_and_does_not_create_triggers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))

            result = optimize_logs(
                root,
                retention_days=5,
                now=lambda: 1_000_000,
                codex_running_check=lambda: False,
            )

            connection = sqlite3.connect(root / "logs_2.sqlite")
            try:
                rows = connection.execute("SELECT ts FROM logs ORDER BY ts").fetchall()
                triggers = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(rows, [(900_000,)])
            self.assertEqual(triggers, [])
            self.assertEqual(result.deleted_rows, 2)
            self.assertGreaterEqual(result.released_bytes, 0)

    def test_optimize_is_blocked_while_codex_is_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))

            with self.assertRaisesRegex(LogSafetyError, "退出 Codex"):
                optimize_logs(root, codex_running_check=lambda: True)

    def test_optimize_checks_free_disk_space_before_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))
            Usage = namedtuple("Usage", "total used free")

            with patch(
                "codex_cleanup_tool.log_maintenance.shutil.disk_usage",
                return_value=Usage(100, 100, 0),
            ):
                with self.assertRaisesRegex(LogSafetyError, "磁盘空间不足"):
                    optimize_logs(root, codex_running_check=lambda: False)

    def test_failed_optimize_restores_database_from_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))
            original = inspect_logs(root).row_count

            with patch(
                "codex_cleanup_tool.log_maintenance._require_integrity",
                side_effect=(None, LogSafetyError("模拟优化后校验失败"), None),
            ):
                with self.assertRaisesRegex(LogSafetyError, "模拟优化后校验失败"):
                    optimize_logs(
                        root,
                        retention_days=5,
                        now=lambda: 1_000_000,
                        codex_running_check=lambda: False,
                    )

            self.assertEqual(inspect_logs(root).row_count, original)

    def test_failed_restore_integrity_keeps_temporary_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_log_home(Path(temporary))

            with patch(
                "codex_cleanup_tool.log_maintenance._require_integrity",
                side_effect=(
                    None,
                    LogSafetyError("模拟优化失败"),
                    LogSafetyError("模拟恢复校验失败"),
                ),
            ):
                with self.assertRaisesRegex(LogSafetyError, "备份保留"):
                    optimize_logs(
                        root,
                        retention_days=5,
                        now=lambda: 1_000_000,
                        codex_running_check=lambda: False,
                    )

            self.assertEqual(len(list(root.glob(".cleanup-logs-*.sqlite"))), 1)


if __name__ == "__main__":
    unittest.main()
