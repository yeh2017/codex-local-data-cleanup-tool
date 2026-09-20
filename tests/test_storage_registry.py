import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_cleanup_tool.storage_registry import (
    CompatibilityStatus,
    StorageRegistry,
)


THREAD_ID = "01a05d04-cf47-78f2-b69e-7cda7c4e222c"


def create_database(path: Path, statements: tuple[str, ...]) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


class StorageRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / ".codex"
        self.root.mkdir()
        (self.root / "thread-writer-locks").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def create_supported_stores(self):
        create_database(
            self.root / "thread_history_1.sqlite",
            (
                "CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT)",
                "CREATE TABLE thread_items (thread_id TEXT, item_json TEXT)",
                "CREATE TABLE thread_realtime_items (thread_id TEXT, item_json TEXT)",
                "CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY)",
                f"INSERT INTO thread_turns VALUES ('{THREAD_ID}', 'turn')",
                f"INSERT INTO thread_items VALUES ('{THREAD_ID}', '{{}}')",
                f"INSERT INTO thread_realtime_items VALUES ('{THREAD_ID}', '{{}}')",
                f"INSERT INTO thread_history_projection_state VALUES ('{THREAD_ID}')",
            ),
        )
        create_database(
            self.root / "queue_1.sqlite",
            (
                "CREATE TABLE queued_items (id TEXT, thread_id TEXT, payload_json TEXT)",
                "CREATE TABLE queued_thread_revisions (revision INTEGER, thread_id TEXT)",
                f"INSERT INTO queued_items VALUES ('item', '{THREAD_ID}', '{{}}')",
                f"INSERT INTO queued_thread_revisions VALUES (1, '{THREAD_ID}')",
            ),
        )
        state = {
            "thread-workspace-root-hints": {THREAD_ID: "C:/workspace"},
            "projectless-thread-ids": [THREAD_ID, "keep"],
        }
        for name in (
            ".codex-global-state.json",
            ".codex-global-state.json.bak",
            "..codex-global-state.json.tmp-old",
        ):
            (self.root / name).write_text(json.dumps(state), encoding="utf-8")
        (self.root / "thread-writer-locks" / f"{THREAD_ID}.lock").write_text(
            "", encoding="utf-8"
        )

    def test_inspect_counts_all_supported_reference_locations(self):
        self.create_supported_stores()

        report = StorageRegistry(self.root).inspect({THREAD_ID})

        self.assertEqual(report.status, CompatibilityStatus.SUPPORTED)
        self.assertEqual(report.total_references, 13)
        self.assertEqual(report.unknown_references, ())

    def test_unknown_sqlite_reference_blocks_destructive_operations(self):
        self.create_supported_stores()
        create_database(
            self.root / "future_1.sqlite",
            (
                "CREATE TABLE future_threads (owner TEXT, payload BLOB)",
                f"INSERT INTO future_threads VALUES ('{THREAD_ID}', X'00')",
            ),
        )

        report = StorageRegistry(self.root).inspect({THREAD_ID})

        self.assertEqual(report.status, CompatibilityStatus.UNSUPPORTED)
        self.assertEqual(len(report.unknown_references), 1)
        self.assertEqual(report.unknown_references[0].path.name, "future_1.sqlite")

    def test_delete_known_references_removes_database_json_and_lock_entries(self):
        self.create_supported_stores()
        registry = StorageRegistry(self.root)

        result = registry.delete_known({THREAD_ID}, secure=True)
        report = registry.inspect({THREAD_ID})

        self.assertEqual(result.deleted_references, 13)
        self.assertEqual(report.total_references, 0)
        self.assertFalse(
            (self.root / "thread-writer-locks" / f"{THREAD_ID}.lock").exists()
        )


if __name__ == "__main__":
    unittest.main()
