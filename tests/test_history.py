import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cleanup_tool.history import (
    HistorySafetyError,
    delete_history_records,
    scan_history_records,
)
from codex_cleanup_tool.recycle_bin import RecycleBinClient


def create_codex_home(base: Path) -> Path:
    root = base / ".codex"
    root.mkdir()
    (root / "installation_id").write_text("test", encoding="utf-8")
    database = root / "state_5.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT,
                title TEXT,
                name TEXT,
                first_user_message TEXT,
                updated_at TEXT,
                updated_at_ms INTEGER,
                recency_at_ms INTEGER,
                archived INTEGER DEFAULT 0,
                source TEXT
            );
            CREATE TABLE thread_dynamic_tools (
                thread_id TEXT,
                name TEXT
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT,
                child_thread_id TEXT
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    return root


def add_record(
    root: Path,
    record_id: str,
    title: str,
    archived: bool = False,
    source: str = "vscode",
) -> Path:
    area = "archived_sessions" if archived else "sessions"
    rollout = root / area / "2026" / "08" / "07" / f"rollout-{record_id}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    connection = sqlite3.connect(root / "state_5.sqlite")
    try:
        connection.execute(
            """
            INSERT INTO threads (
                id, rollout_path, title, updated_at, updated_at_ms,
                recency_at_ms, archived, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                str(rollout),
                title,
                "2026-08-07T12:00:00Z",
                1,
                1,
                int(archived),
                source,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return rollout


class HistoryTests(unittest.TestCase):
    def test_scan_prefers_codex_name_over_legacy_raw_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            add_record(root, "thread-1", "# Files mentioned by the user:\nraw")
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute(
                    "UPDATE threads SET name = ? WHERE id = ?",
                    ("Codex 整理后的标题", "thread-1"),
                )
                connection.commit()
            finally:
                connection.close()

            records = scan_history_records(root)

            self.assertEqual(records[0].title, "Codex 整理后的标题")

    def test_scan_cleans_html_and_attachment_preamble_from_fallback_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            add_record(
                root,
                "thread-1",
                "# Files mentioned by the user:\n\n## file.png\n\n"
                "## My request:\nLivePortrait &#x4E0E; Google Flow",
            )

            records = scan_history_records(root)

            self.assertEqual(records[0].title, "LivePortrait 与 Google Flow")

    def test_scan_marks_record_with_missing_rollout_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            rollout = add_record(root, "thread-1", "孤儿记录")
            rollout.unlink()

            records = scan_history_records(root)

            self.assertTrue(records[0].missing_rollout)
            self.assertEqual(records[0].total_bytes, 0)

    def test_visible_parent_size_includes_hidden_descendant_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            parent = add_record(root, "parent", "侧边栏任务")
            child = add_record(
                root,
                "child",
                "内部审查任务",
                source=json.dumps({"subagent": {"thread_spawn": {"depth": 1}}}),
            )
            child.write_text(child.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES (?, ?)",
                    ("parent", "child"),
                )
                connection.commit()
            finally:
                connection.close()

            records = scan_history_records(root)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].id, "parent")
            self.assertEqual(
                records[0].total_bytes,
                parent.stat().st_size + child.stat().st_size,
            )

    def test_scan_hides_internal_subagent_records_like_codex_sidebar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            add_record(root, "parent", "侧边栏任务")
            add_record(
                root,
                "child",
                "内部审查任务",
                source=json.dumps({"subagent": {"thread_spawn": {"depth": 1}}}),
            )

            records = scan_history_records(root)

            self.assertEqual([record.id for record in records], ["parent"])

    def test_deleting_parent_also_deletes_hidden_subagent_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            parent = add_record(root, "parent", "侧边栏任务")
            child = add_record(
                root,
                "child",
                "内部审查任务",
                source=json.dumps({"subagent": {"thread_spawn": {"depth": 1}}}),
            )
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES (?, ?)",
                    ("parent", "child"),
                )
                connection.commit()
            finally:
                connection.close()

            def recycle_staging(paths):
                shutil.rmtree(paths[0])
                return 0, False

            result = delete_history_records(
                root,
                {"parent"},
                recycle_client=RecycleBinClient(recycle_staging),
                require_codex_closed=False,
            )

            self.assertEqual(set(result.deleted_ids), {"parent", "child"})
            self.assertFalse(parent.exists())
            self.assertFalse(child.exists())
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)
            finally:
                connection.close()

    def test_record_id_must_match_filename_suffix_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            rollout = add_record(root, "thread-10", "另一条记录")
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute(
                    "INSERT INTO threads (id, rollout_path, title) VALUES (?, ?, ?)",
                    ("thread-1", str(rollout), "错误映射"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(HistorySafetyError, "ID 与文件名不匹配"):
                scan_history_records(root)

    def test_scan_maps_database_title_to_rollout_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            rollout = add_record(root, "thread-1", "菜单图片优化")

            records = scan_history_records(root)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].id, "thread-1")
            self.assertEqual(records[0].title, "菜单图片优化")
            self.assertEqual(records[0].rollout_path, rollout.resolve())
            self.assertEqual(records[0].total_bytes, rollout.stat().st_size)
            self.assertFalse(records[0].archived)

    def test_delete_removes_selected_file_database_row_and_duplicate_index_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            selected = add_record(root, "thread-1", "删除我")
            kept = add_record(root, "thread-2", "保留我")
            index = root / "session_index.jsonl"
            index.write_text(
                "\n".join(
                    json.dumps(row, ensure_ascii=False)
                    for row in (
                        {"id": "thread-1", "thread_name": "删除我"},
                        {"id": "thread-1", "thread_name": "重复索引"},
                        {"id": "thread-2", "thread_name": "保留我"},
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            def recycle_one(paths):
                shutil.rmtree(paths[0])
                return 0, False

            result = delete_history_records(
                root,
                {"thread-1"},
                recycle_client=RecycleBinClient(recycle_one),
                require_codex_closed=False,
            )

            self.assertEqual(result.deleted_ids, ("thread-1",))
            self.assertFalse(selected.exists())
            self.assertTrue(kept.exists())
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                ids = [row[0] for row in connection.execute("SELECT id FROM threads")]
            finally:
                connection.close()
            self.assertEqual(ids, ["thread-2"])
            remaining = [json.loads(line)["id"] for line in index.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(remaining, ["thread-2"])

    def test_batch_delete_uses_one_staging_target_and_removes_related_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            first = add_record(root, "thread-1", "第一条")
            second = add_record(root, "thread-2", "第二条")
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                connection.execute(
                    "INSERT INTO thread_dynamic_tools VALUES (?, ?)",
                    ("thread-1", "tool"),
                )
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES (?, ?)",
                    ("thread-1", "thread-2"),
                )
                connection.commit()
            finally:
                connection.close()
            recycled = []

            def recycle_staging(paths):
                recycled.append(paths)
                shutil.rmtree(paths[0])
                return 0, False

            delete_history_records(
                root,
                {"thread-1", "thread-2"},
                recycle_client=RecycleBinClient(recycle_staging),
                require_codex_closed=False,
            )

            self.assertEqual(len(recycled), 1)
            self.assertEqual(len(recycled[0]), 1)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM thread_dynamic_tools").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM thread_spawn_edges").fetchone()[0], 0)
            finally:
                connection.close()

    def test_recycle_failure_restores_files_and_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            rollout = add_record(root, "thread-1", "保留我")

            def fail_recycle(_paths):
                raise RuntimeError("模拟回收站失败")

            with self.assertRaisesRegex(HistorySafetyError, "未能移入回收站"):
                delete_history_records(
                    root,
                    {"thread-1"},
                    recycle_client=RecycleBinClient(fail_recycle),
                    require_codex_closed=False,
                )

            self.assertTrue(rollout.exists())
            connection = sqlite3.connect(root / "state_5.sqlite")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 1)
            finally:
                connection.close()

    def test_safe_delete_blocks_unknown_file_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            rollout = add_record(root, "thread-1", "unknown reference")
            (root / "future-state.json").write_text(
                json.dumps({"thread_id": "thread-1"}), encoding="utf-8"
            )

            with self.assertRaisesRegex(HistorySafetyError, "无法安全处理"):
                delete_history_records(
                    root,
                    {"thread-1"},
                    require_codex_closed=False,
                )

            self.assertTrue(rollout.exists())

    def test_failed_rollback_keeps_rescue_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            add_record(root, "thread-1", "rollback failure")

            def fail_recycle(_paths):
                raise RuntimeError("recycle failed")

            with (
                patch(
                    "codex_cleanup_tool.history._restore_database_backup",
                    side_effect=OSError("database restore failed"),
                    create=True,
                ),
                self.assertRaisesRegex(HistorySafetyError, "救援快照"),
            ):
                delete_history_records(
                    root,
                    {"thread-1"},
                    recycle_client=RecycleBinClient(fail_recycle),
                    require_codex_closed=False,
                )

            self.assertTrue(any(root.glob(".cleanup-state-*.sqlite")))

    def test_delete_is_blocked_while_codex_is_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = create_codex_home(Path(temporary))
            add_record(root, "thread-1", "运行中的任务")

            with self.assertRaisesRegex(HistorySafetyError, "退出 Codex"):
                delete_history_records(
                    root,
                    {"thread-1"},
                    codex_running_check=lambda: True,
                )


if __name__ == "__main__":
    unittest.main()
