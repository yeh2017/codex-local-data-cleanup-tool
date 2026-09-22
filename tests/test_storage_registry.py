import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_cleanup_tool.storage_registry import (
    CompatibilityStatus,
    StorageCompatibilityError,
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

    def test_unknown_sqlite_json_reference_is_detected(self):
        create_database(
            self.root / "future_1.sqlite",
            (
                "CREATE TABLE future_events (payload TEXT)",
                f'''INSERT INTO future_events VALUES ('{{"thread_id":"{THREAD_ID}"}}')''',
            ),
        )

        report = StorageRegistry(self.root).inspect({THREAD_ID})

        self.assertEqual(report.status, CompatibilityStatus.UNSUPPORTED)
        self.assertEqual(report.unknown_references[0].detail, "future_events.payload")

    def test_new_table_in_known_database_is_detected(self):
        self.create_supported_stores()
        connection = sqlite3.connect(self.root / "queue_1.sqlite")
        try:
            connection.execute("CREATE TABLE future_links (payload TEXT)")
            connection.execute(
                "INSERT INTO future_links VALUES (?)",
                (json.dumps({"thread_id": THREAD_ID}),),
            )
            connection.commit()
        finally:
            connection.close()

        report = StorageRegistry(self.root).inspect({THREAD_ID})

        self.assertEqual(report.status, CompatibilityStatus.UNSUPPORTED)
        self.assertTrue(
            any(item.detail == "future_links.payload" for item in report.unknown_references)
        )

    def test_strict_inspection_detects_reference_in_retained_known_row_payload(self):
        self.create_supported_stores()
        connection = sqlite3.connect(self.root / "queue_1.sqlite")
        try:
            connection.execute(
                "INSERT INTO queued_items VALUES (?, ?, ?)",
                ("cross-reference", "kept-thread", json.dumps({"target": THREAD_ID})),
            )
            connection.commit()
        finally:
            connection.close()

        report = StorageRegistry(self.root).inspect({THREAD_ID}, strict=True)

        self.assertEqual(report.status, CompatibilityStatus.UNSUPPORTED)
        self.assertTrue(
            any(
                item.detail == "queued_items.payload_json"
                for item in report.unknown_references
            )
        )

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

    def test_unrelated_codex_coordination_files_are_not_treated_as_unknown(self):
        coordination = self.root / "thread-writer-locks" / ".coordination.lock"
        provisioning = self.root / ".codex-provisioning-test.guard"
        coordination.write_text("", encoding="utf-8")
        provisioning.write_text("", encoding="utf-8")
        registry = StorageRegistry(self.root)
        managed = registry.managed_paths({THREAD_ID})

        references = registry.unmanaged_references({THREAD_ID}, managed)

        self.assertIn(coordination.resolve(), managed)
        self.assertIn(provisioning.resolve(), managed)
        self.assertEqual(references, ())

    def test_invalid_stale_global_state_temp_is_backed_up_deleted_and_restored(self):
        stale = self.root / "..codex-global-state.json.tmp-stale"
        original = ('{"thread":"' + THREAD_ID).encode("utf-8")
        stale.write_bytes(original)
        registry = StorageRegistry(self.root)

        report = registry.inspect({THREAD_ID})

        self.assertEqual(report.status, CompatibilityStatus.SUPPORTED)
        self.assertEqual(report.unknown_references, ())
        with tempfile.TemporaryDirectory() as temporary:
            backup = Path(temporary)
            metadata = registry.export_selected({THREAD_ID}, backup)
            registry.verify_export(backup, metadata)
            registry.delete_additional({THREAD_ID})
            self.assertFalse(stale.exists())

            registry.restore_selected({THREAD_ID}, backup, metadata)

        self.assertEqual(stale.read_bytes(), original)

    def test_invalid_stale_temp_without_selected_reference_does_not_reduce_support(self):
        (self.root / "..codex-global-state.json.tmp-empty").write_bytes(b"")

        report = StorageRegistry(self.root).inspect({THREAD_ID})

        self.assertEqual(report.status, CompatibilityStatus.SUPPORTED)

    def test_restore_rejects_global_state_target_outside_codex_root(self):
        victim = self.root.parent / "victim.json"
        victim.write_text('{"safe":true}', encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            backup = Path(temporary)
            states = backup / "global_state_fragments.json"
            states.write_text(
                json.dumps(
                    [
                        {
                            "name": "../victim.json",
                            "fragments": [
                                {
                                    "kind": "dict",
                                    "path": [],
                                    "key": "thread",
                                    "value": THREAD_ID,
                                }
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            metadata = {
                "databases": [],
                "global_state": {
                    "name": states.name,
                    "sha256": hashlib.sha256(states.read_bytes()).hexdigest(),
                },
            }

            with self.assertRaisesRegex(StorageCompatibilityError, "路径|文件名"):
                StorageRegistry(self.root).restore_selected(
                    {THREAD_ID}, backup, metadata
                )

        self.assertEqual(victim.read_text(encoding="utf-8"), '{"safe":true}')


if __name__ == "__main__":
    unittest.main()
