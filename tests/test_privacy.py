import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cleanup_tool.privacy import (
    PrivacyPurgeError,
    privacy_purge_history,
)
from codex_cleanup_tool.storage_registry import StorageRegistry
from tests.test_history import add_record, create_codex_home
from tests.test_history_backup import create_auxiliary_history, create_logs


class PrivacyPurgeTests(unittest.TestCase):
    def test_privacy_purge_removes_all_known_local_references_without_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            selected = add_record(root, "thread-1", "selected")
            kept = add_record(root, "thread-2", "kept")
            create_logs(root, (("thread-1", "TRACE"), ("thread-2", "INFO")))
            create_auxiliary_history(root)
            (root / "session_index.jsonl").write_text(
                json.dumps({"id": "thread-1"})
                + "\n"
                + json.dumps({"id": "thread-2"})
                + "\n",
                encoding="utf-8",
            )
            (root / "thread-writer-locks").mkdir()
            (root / "thread-writer-locks" / "thread-1.lock").write_text(
                "", encoding="utf-8"
            )

            result = privacy_purge_history(
                root, {"thread-1"}, require_codex_closed=False
            )

            self.assertEqual(result.deleted_ids, ("thread-1",))
            self.assertFalse(selected.exists())
            self.assertTrue(kept.exists())
            self.assertEqual(StorageRegistry(root).inspect({"thread-1"}).total_references, 0)
            self.assertNotIn(
                "thread-1", (root / "session_index.jsonl").read_text(encoding="utf-8")
            )
            self.assertIn(
                "thread-2", (root / "session_index.jsonl").read_text(encoding="utf-8")
            )
            self.assertFalse(result.journal_path.exists())
            self.assertFalse(any(base.rglob("manifest.json")))

    def test_unknown_database_reference_blocks_before_any_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            rollout = add_record(root, "thread-1", "selected")
            future = sqlite3.connect(root / "future_1.sqlite")
            try:
                future.execute("CREATE TABLE future_threads (thread_id TEXT)")
                future.execute("INSERT INTO future_threads VALUES ('thread-1')")
                future.commit()
            finally:
                future.close()

            with self.assertRaisesRegex(PrivacyPurgeError, "未知任务引用"):
                privacy_purge_history(
                    root, {"thread-1"}, require_codex_closed=False
                )

            self.assertTrue(rollout.exists())
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM threads WHERE id='thread-1'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_protected_file_reference_blocks_privacy_purge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            add_record(root, "thread-1", "selected")
            (root / "config.toml").write_text(
                'note = "thread-1"', encoding="utf-8"
            )

            with self.assertRaisesRegex(PrivacyPurgeError, "受保护"):
                privacy_purge_history(
                    root, {"thread-1"}, require_codex_closed=False
                )

    def test_insufficient_vacuum_space_blocks_before_any_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            rollout = add_record(root, "thread-1", "selected")

            with (
                patch(
                    "shutil.disk_usage",
                    return_value=shutil._ntuple_diskusage(100, 100, 0),
                ),
                self.assertRaisesRegex(PrivacyPurgeError, "磁盘空间不足"),
            ):
                privacy_purge_history(
                    root, {"thread-1"}, require_codex_closed=False
                )

            self.assertTrue(rollout.exists())

    def test_unmanaged_text_reference_blocks_privacy_purge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            add_record(root, "thread-1", "selected")
            (root / "future-state.json").write_text(
                json.dumps({"thread_id": "thread-1"}), encoding="utf-8"
            )

            with self.assertRaisesRegex(PrivacyPurgeError, "未知任务引用"):
                privacy_purge_history(
                    root, {"thread-1"}, require_codex_closed=False
                )


if __name__ == "__main__":
    unittest.main()
