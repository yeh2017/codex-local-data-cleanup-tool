import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cleanup_tool.history import delete_history_records, scan_history_records
from codex_cleanup_tool.history_backup import (
    BackupSafetyError,
    _insert_table,
    _safe_restore_destination,
    create_history_backup,
    ensure_backup_root,
    migrate_backup_root,
    restore_history_backup,
)
from codex_cleanup_tool.recycle_bin import RecycleBinClient
from codex_cleanup_tool.storage_registry import StorageRegistry
from tests.test_history import add_record, create_codex_home


def create_logs(root: Path, rows: tuple[tuple[str, str], ...]) -> None:
    connection = sqlite3.connect(root / "logs_2.sqlite")
    try:
        connection.execute(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY, thread_id TEXT, level TEXT)"
        )
        connection.executemany(
            "INSERT INTO logs (thread_id, level) VALUES (?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()


def create_auxiliary_history(root: Path) -> None:
    history = sqlite3.connect(root / "thread_history_1.sqlite")
    try:
        history.execute("CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT)")
        history.execute("CREATE TABLE thread_items (thread_id TEXT, item_json TEXT)")
        history.execute("CREATE TABLE thread_realtime_items (thread_id TEXT, item_json TEXT)")
        history.execute(
            "CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY)"
        )
        for thread_id in ("thread-1", "thread-2"):
            history.execute("INSERT INTO thread_turns VALUES (?, 'turn')", (thread_id,))
            history.execute("INSERT INTO thread_items VALUES (?, '{}')", (thread_id,))
            history.execute("INSERT INTO thread_realtime_items VALUES (?, '{}')", (thread_id,))
            history.execute(
                "INSERT INTO thread_history_projection_state VALUES (?)", (thread_id,)
            )
        history.commit()
    finally:
        history.close()
    queue = sqlite3.connect(root / "queue_1.sqlite")
    try:
        queue.execute(
            "CREATE TABLE queued_items (id TEXT, thread_id TEXT, payload_json TEXT)"
        )
        queue.execute(
            "CREATE TABLE queued_thread_revisions (revision INTEGER, thread_id TEXT)"
        )
        queue.execute("INSERT INTO queued_items VALUES ('one', 'thread-1', '{}')")
        queue.execute("INSERT INTO queued_items VALUES ('two', 'thread-2', '{}')")
        queue.execute("INSERT INTO queued_thread_revisions VALUES (1, 'thread-1')")
        queue.commit()
    finally:
        queue.close()
    (root / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "thread-workspace-root-hints": {
                    "thread-1": "C:/one",
                    "thread-2": "C:/two",
                },
                "projectless-thread-ids": ["thread-1", "thread-2"],
            }
        ),
        encoding="utf-8",
    )


class HistoryBackupTests(unittest.TestCase):
    def test_v3_backup_delete_and_restore_covers_auxiliary_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "selected")
            add_record(root, "thread-2", "kept")
            create_auxiliary_history(root)
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.executemany(
                    "INSERT INTO thread_attachments VALUES (?, ?, ?)",
                    (
                        ("attachment-1", "thread-1", "selected"),
                        ("attachment-2", "thread-2", "kept"),
                    ),
                )
                connection.execute(
                    "INSERT INTO rollout_migration_state VALUES (?, ?, ?, ?)",
                    ("rollout", 100, "thread-1", 200),
                )
                connection.commit()
            finally:
                connection.close()
            backup_root = ensure_backup_root(base / "backups", root, create=True)

            def recycle(paths):
                shutil.rmtree(paths[0])
                return 0, False

            result = delete_history_records(
                root,
                {"thread-1"},
                backup_root=backup_root,
                recycle_client=RecycleBinClient(recycle),
                require_codex_closed=False,
            )

            manifest = json.loads(
                (result.backup_path / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["version"], 3)
            self.assertEqual(StorageRegistry(root).inspect({"thread-1"}).total_references, 0)
            self.assertGreater(StorageRegistry(root).inspect({"thread-2"}).total_references, 0)
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT id FROM thread_attachments ORDER BY id"
                    ).fetchall(),
                    [("attachment-2",)],
                )
            finally:
                connection.close()
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertIsNone(
                    connection.execute(
                        "SELECT last_checked_thread_id FROM rollout_migration_state"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            restore_history_backup(
                result.backup_path, root, require_codex_closed=False
            )

            self.assertGreater(StorageRegistry(root).inspect({"thread-1"}).total_references, 0)
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT id FROM thread_attachments ORDER BY id"
                    ).fetchall(),
                    [("attachment-1",), ("attachment-2",)],
                )
            finally:
                connection.close()
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT last_checked_thread_id FROM rollout_migration_state"
                    ).fetchone()[0],
                    "thread-1",
                )
            finally:
                connection.close()

    def test_restore_rewrites_rollout_path_after_codex_home_moves(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "source").mkdir()
            source_root = create_codex_home(base / "source")
            add_record(source_root, "thread-1", "moved")
            backup_root = ensure_backup_root(base / "backups", source_root, create=True)
            backup = create_history_backup(
                source_root, {"thread-1"}, backup_root, require_codex_closed=False
            )
            (base / "target").mkdir()
            target_root = create_codex_home(base / "target")

            restore_history_backup(
                backup.path, target_root, require_codex_closed=False
            )

            restored = scan_history_records(target_root)[0]
            self.assertTrue(restored.rollout_path.is_relative_to(target_root))
            self.assertNotIn(str(source_root), str(restored.rollout_path))

    def test_restore_rejects_missing_required_target_column(self):
        source = sqlite3.connect(":memory:")
        target = sqlite3.connect(":memory:")
        try:
            source.execute("CREATE TABLE sample (id TEXT)")
            source.execute("INSERT INTO sample VALUES ('one')")
            target.execute(
                "CREATE TABLE sample (id TEXT, required_value TEXT NOT NULL)"
            )

            with self.assertRaisesRegex(BackupSafetyError, "必填字段"):
                _insert_table(source, target, "sample")
        finally:
            source.close()
            target.close()

    def test_restore_destination_rejects_linked_parent_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sessions = root / "sessions"
            sessions.mkdir()

            with patch(
                "codex_cleanup_tool.history_backup._is_link_like",
                side_effect=lambda path: Path(path) == sessions,
            ):
                with self.assertRaisesRegex(BackupSafetyError, "目录联接"):
                    _safe_restore_destination(
                        root, Path("sessions/2026/rollout-thread-1.jsonl")
                    )

    def test_restore_destination_rejects_dangling_final_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            final = root / "sessions" / "rollout-thread-1.jsonl"

            with patch(
                "codex_cleanup_tool.history_backup._is_link_like",
                side_effect=lambda path: Path(path) == final,
            ):
                with self.assertRaisesRegex(BackupSafetyError, "目录联接"):
                    _safe_restore_destination(
                        root, Path("sessions/rollout-thread-1.jsonl")
                    )

    def test_restore_rejects_spawn_edge_when_other_endpoint_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            parent = add_record(root, "parent", "parent")
            child = add_record(root, "child", "child")
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES (?, ?)",
                    ("parent", "child"),
                )
                connection.commit()
            finally:
                connection.close()
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            backup = create_history_backup(
                root, {"child"}, backup_root, require_codex_closed=False
            )
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute("DELETE FROM thread_spawn_edges")
                connection.execute("DELETE FROM threads")
                connection.commit()
            finally:
                connection.close()
            parent.unlink()
            child.unlink()

            with self.assertRaisesRegex(BackupSafetyError, "关联任务"):
                restore_history_backup(
                    backup.path, root, require_codex_closed=False
                )

    def test_failed_snapshot_restore_keeps_rescue_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            rollout = add_record(root, "thread-1", "restore failure")
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            backup = create_history_backup(
                root, {"thread-1"}, backup_root, require_codex_closed=False
            )
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute("DELETE FROM threads")
                connection.commit()
            finally:
                connection.close()
            rollout.unlink()

            with (
                patch(
                    "codex_cleanup_tool.history_backup.shutil.copy2",
                    side_effect=OSError("copy failed"),
                ),
                patch(
                    "codex_cleanup_tool.history_backup._restore_snapshot",
                    side_effect=OSError("snapshot restore failed"),
                ),
                self.assertRaisesRegex(BackupSafetyError, "救援快照"),
            ):
                restore_history_backup(
                    backup.path, root, require_codex_closed=False
                )

            self.assertTrue(any(root.glob(".cleanup-restore-state-*.sqlite")))

    def test_partial_auxiliary_restore_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            rollout = add_record(root, "thread-1", "partial auxiliary")
            create_auxiliary_history(root)
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            backup = create_history_backup(
                root, {"thread-1"}, backup_root, require_codex_closed=False
            )
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute("DELETE FROM threads WHERE id='thread-1'")
                connection.commit()
            finally:
                connection.close()
            rollout.unlink()
            StorageRegistry(root).delete_additional({"thread-1"})

            with (
                patch.object(
                    StorageRegistry,
                    "restore_selected",
                    side_effect=RuntimeError("partial auxiliary restore"),
                ),
                patch.object(StorageRegistry, "delete_additional") as cleanup,
                self.assertRaisesRegex(RuntimeError, "partial auxiliary restore"),
            ):
                restore_history_backup(
                    backup.path, root, require_codex_closed=False
                )

            cleanup.assert_called_once_with({"thread-1"})

    def test_restore_rejects_preexisting_auxiliary_reference_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            rollout = add_record(root, "thread-1", "preexisting auxiliary")
            create_auxiliary_history(root)
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            backup = create_history_backup(
                root, {"thread-1"}, backup_root, require_codex_closed=False
            )
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute("DELETE FROM threads WHERE id='thread-1'")
                connection.commit()
            finally:
                connection.close()
            rollout.unlink()

            with self.assertRaisesRegex(BackupSafetyError, "辅助|引用"):
                restore_history_backup(
                    backup.path, root, require_codex_closed=False
                )

            connection = sqlite3.connect(root / "thread_history_1.sqlite")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM thread_turns WHERE thread_id='thread-1'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_delete_and_restore_include_only_selected_thread_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "selected")
            add_record(root, "thread-2", "kept")
            create_logs(
                root,
                (("thread-1", "TRACE"), ("thread-1", "INFO"), ("thread-2", "INFO")),
            )
            backup_root = ensure_backup_root(base / "backups", root, create=True)

            def recycle(paths):
                shutil.rmtree(paths[0])
                return 0, False

            result = delete_history_records(
                root,
                {"thread-1"},
                backup_root=backup_root,
                recycle_client=RecycleBinClient(recycle),
                require_codex_closed=False,
            )

            self.assertEqual(result.deleted_log_rows, 2)
            connection = sqlite3.connect(root / "logs_2.sqlite")
            try:
                self.assertEqual(
                    connection.execute("SELECT thread_id FROM logs").fetchall(),
                    [("thread-2",)],
                )
            finally:
                connection.close()
            self.assertTrue((result.backup_path / "logs.sqlite").is_file())

            restore_history_backup(result.backup_path, root, require_codex_closed=False)

            connection = sqlite3.connect(root / "logs_2.sqlite")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT thread_id, level FROM logs ORDER BY id"
                    ).fetchall(),
                    [
                        ("thread-1", "TRACE"),
                        ("thread-1", "INFO"),
                        ("thread-2", "INFO"),
                    ],
                )
            finally:
                connection.close()

    def test_recycle_failure_restores_selected_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "selected")
            create_logs(root, (("thread-1", "TRACE"),))
            backup_root = ensure_backup_root(base / "backups", root, create=True)

            def fail(_paths):
                raise RuntimeError("recycle failed")

            with self.assertRaises(Exception):
                delete_history_records(
                    root,
                    {"thread-1"},
                    backup_root=backup_root,
                    recycle_client=RecycleBinClient(fail),
                    require_codex_closed=False,
                )

            connection = sqlite3.connect(root / "logs_2.sqlite")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 1)
            finally:
                connection.close()

    def test_partial_auxiliary_delete_is_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "selected")
            create_auxiliary_history(root)
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            original_delete = StorageRegistry.delete_additional

            def delete_then_fail(registry, ids, *, secure=False):
                original_delete(registry, ids, secure=secure)
                raise RuntimeError("partial auxiliary delete")

            with (
                patch.object(
                    StorageRegistry,
                    "delete_additional",
                    delete_then_fail,
                ),
                self.assertRaisesRegex(RuntimeError, "partial auxiliary delete"),
            ):
                delete_history_records(
                    root,
                    {"thread-1"},
                    backup_root=backup_root,
                    recycle_client=RecycleBinClient(lambda paths: (0, False)),
                    require_codex_closed=False,
                )

            connection = sqlite3.connect(root / "thread_history_1.sqlite")
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM thread_turns WHERE thread_id='thread-1'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_parent_delete_also_removes_child_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "parent", "parent")
            add_record(root, "child", "child")
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES (?, ?)",
                    ("parent", "child"),
                )
                connection.commit()
            finally:
                connection.close()
            create_logs(
                root,
                (("parent", "INFO"), ("child", "TRACE"), ("kept", "INFO")),
            )
            backup_root = ensure_backup_root(base / "backups", root, create=True)

            def recycle(paths):
                shutil.rmtree(paths[0])
                return 0, False

            result = delete_history_records(
                root,
                {"parent"},
                backup_root=backup_root,
                recycle_client=RecycleBinClient(recycle),
                require_codex_closed=False,
            )

            self.assertEqual(result.deleted_log_rows, 2)
            connection = sqlite3.connect(root / "logs_2.sqlite")
            try:
                self.assertEqual(
                    connection.execute("SELECT thread_id FROM logs").fetchall(),
                    [("kept",)],
                )
            finally:
                connection.close()

    def test_tampered_log_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "selected")
            create_logs(root, (("thread-1", "TRACE"),))
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            backup = create_history_backup(
                root, {"thread-1"}, backup_root, require_codex_closed=False
            )
            with (backup.path / "logs.sqlite").open("ab") as stream:
                stream.write(b"tampered")

            with self.assertRaisesRegex(BackupSafetyError, "日志校验失败"):
                restore_history_backup(
                    backup.path, root, require_codex_closed=False
                )
    def test_backup_root_must_be_outside_codex_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            with self.assertRaisesRegex(BackupSafetyError, "Codex 数据目录之外"):
                ensure_backup_root(root / "backups", root, create=True)

    def test_delete_creates_persistent_backup_and_restore_recovers_all_relations(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            rollout = add_record(root, "thread-1", "需要恢复")
            index = root / "session_index.jsonl"
            index.write_text(
                json.dumps({"id": "thread-1", "thread_name": "需要恢复"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute("INSERT INTO thread_dynamic_tools VALUES (?, ?)", ("thread-1", "tool"))
                connection.commit()
            finally:
                connection.close()
            backup_root = ensure_backup_root(base / "backups", root, create=True)

            def recycle(paths):
                shutil.rmtree(paths[0])
                return 0, False

            result = delete_history_records(
                root,
                {"thread-1"},
                backup_root=backup_root,
                recycle_client=RecycleBinClient(recycle),
                require_codex_closed=False,
            )
            self.assertTrue(result.backup_path.is_dir())
            self.assertFalse(rollout.exists())

            restored = restore_history_backup(
                result.backup_path,
                root,
                require_codex_closed=False,
            )

            self.assertEqual(restored.restored_ids, ("thread-1",))
            self.assertTrue(rollout.exists())
            self.assertEqual([r.id for r in scan_history_records(root)], ["thread-1"])
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM thread_dynamic_tools").fetchone()[0], 1)
            finally:
                connection.close()
            self.assertIn('"id": "thread-1"', index.read_text(encoding="utf-8"))
            self.assertTrue(result.backup_path.is_dir())

    def test_restore_refuses_existing_thread_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "原任务")
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            backup = create_history_backup(root, {"thread-1"}, backup_root, require_codex_closed=False)

            with self.assertRaisesRegex(BackupSafetyError, "已存在"):
                restore_history_backup(backup.path, root, require_codex_closed=False)

    def test_restore_rejects_manifest_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "安全校验")
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            backup = create_history_backup(root, {"thread-1"}, backup_root, require_codex_closed=False)
            rollout = scan_history_records(root)[0].rollout_path
            rollout.unlink()
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute("DELETE FROM threads WHERE id = 'thread-1'")
                connection.commit()
            finally:
                connection.close()
            manifest_path = backup.path / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["relative_path"] = "../escaped.jsonl"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(BackupSafetyError, "路径"):
                restore_history_backup(backup.path, root, require_codex_closed=False)

    def test_restore_rejects_windows_rooted_manifest_path(self):
        self._assert_manifest_path_rejected(r"\escaped.jsonl")

    def _assert_manifest_path_rejected(self, unsafe_path: str):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "安全校验")
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            backup = create_history_backup(root, {"thread-1"}, backup_root, require_codex_closed=False)
            manifest_path = backup.path / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["relative_path"] = unsafe_path
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(BackupSafetyError, "路径"):
                restore_history_backup(backup.path, root, require_codex_closed=False)

    def test_manual_backup_is_blocked_while_codex_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "运行中")
            backup_root = ensure_backup_root(base / "backups", root, create=True)
            with self.assertRaisesRegex(BackupSafetyError, "退出 Codex"):
                create_history_backup(root, {"thread-1"}, backup_root, codex_running_check=lambda: True)

    def test_recycle_reports_failure_after_moving_staging_restores_from_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            rollout = add_record(root, "thread-1", "恢复测试")
            backup_root = ensure_backup_root(base / "backups", root, create=True)

            def move_then_fail(paths):
                shutil.rmtree(paths[0])
                return 1, False

            with self.assertRaisesRegex(Exception, "未能移入回收站"):
                delete_history_records(
                    root,
                    {"thread-1"},
                    backup_root=backup_root,
                    recycle_client=RecycleBinClient(move_then_fail),
                    require_codex_closed=False,
                )

            self.assertTrue(rollout.is_file())
            self.assertEqual([record.id for record in scan_history_records(root)], ["thread-1"])

    def test_backup_directory_migration_copies_verifies_then_removes_old_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = create_codex_home(base)
            add_record(root, "thread-1", "迁移任务")
            old = ensure_backup_root(base / "old", root, create=True)
            backup = create_history_backup(root, {"thread-1"}, old, require_codex_closed=False)
            new = base / "new"

            migrate_backup_root(old, new, root)

            self.assertTrue((new / backup.path.name / "manifest.json").is_file())
            self.assertFalse(backup.path.exists())


if __name__ == "__main__":
    unittest.main()
