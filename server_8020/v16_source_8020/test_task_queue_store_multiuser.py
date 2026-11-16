import gc
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from task_queue_store import PersistentTaskQueue


LEGACY_SCHEMA = """
CREATE TABLE task_queue (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL UNIQUE,
    submission_key TEXT,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    stage TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    history_hidden INTEGER NOT NULL DEFAULT 0,
    billing_state TEXT NOT NULL DEFAULT 'not_started',
    control_state TEXT NOT NULL DEFAULT 'active',
    control_error TEXT NOT NULL DEFAULT '',
    control_changed_at TEXT
);
CREATE UNIQUE INDEX idx_task_queue_submission_key
ON task_queue(submission_key) WHERE submission_key IS NOT NULL;
"""


class PersistentTaskQueueMultiUserTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "queue.sqlite3"

    def tearDown(self):
        # sqlite3 context managers commit/rollback but do not themselves close;
        # collect short-lived connection objects before Windows removes the DB.
        gc.collect()
        self.temporary_directory.cleanup()

    def test_legacy_migration_keeps_existing_rows_unassigned(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(LEGACY_SCHEMA)
            connection.execute(
                """
                INSERT INTO task_queue (
                    task_id, submission_key, status, title, config_json,
                    created_at, finished_at, stage, billing_state
                ) VALUES (?, ?, 'succeeded', ?, '{}', ?, ?, ?, 'confirmed')
                """,
                (
                    "legacy-task",
                    "legacy-key",
                    "legacy title",
                    "2026-08-01 10:00:00",
                    "2026-08-01 10:01:00",
                    "done",
                ),
            )

        queue = PersistentTaskQueue(self.database_path)
        queue.ensure_ready()

        legacy = queue.get_internal("legacy-task")
        self.assertTrue(legacy["legacy_unassigned"])
        self.assertIsNone(legacy["owner_user_id"])
        self.assertIsNone(legacy["tenant_id"])
        self.assertIsNone(queue.get_owned("alice", "legacy-task"))
        self.assertEqual(queue.snapshot_owned("alice")["total"], 0)

        with sqlite3.connect(self.database_path) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(task_queue)")
            }
            indexes = {
                row[1]: row[2]
                for row in connection.execute("PRAGMA index_list(task_queue)")
            }
        self.assertIn("owner_user_id", columns)
        self.assertIn("tenant_id", columns)
        self.assertNotIn("idx_task_queue_submission_key", indexes)
        self.assertEqual(indexes["idx_task_queue_owner_submission_key"], 1)

    def test_owner_is_required_and_idempotency_is_owner_scoped(self):
        queue = PersistentTaskQueue(self.database_path)

        with self.assertRaises(TypeError):
            queue.enqueue("missing", {})
        with self.assertRaises(ValueError):
            queue.enqueue("blank", {}, owner_user_id="  ")

        alice = queue.enqueue(
            "alice one",
            {"task_name": "alice"},
            submission_key="same-key",
            owner_user_id="alice",
            tenant_id="tenant-a",
        )
        alice_repeat = queue.enqueue(
            "alice duplicate",
            {"task_name": "should not replace"},
            submission_key="same-key",
            owner_user_id="alice",
            tenant_id="tenant-a",
        )
        bob = queue.enqueue(
            "bob one",
            {"task_name": "bob"},
            submission_key="same-key",
            owner_user_id="bob",
            tenant_id="tenant-a",
        )

        self.assertEqual(alice_repeat["task_id"], alice["task_id"])
        self.assertNotEqual(bob["task_id"], alice["task_id"])
        self.assertEqual(
            queue.get_by_submission_key_owned("alice", "same-key")["task_id"],
            alice["task_id"],
        )
        self.assertEqual(
            queue.get_by_submission_key_owned("bob", "same-key")["task_id"],
            bob["task_id"],
        )
        self.assertIsNone(queue.get_by_submission_key("same-key"))

    def test_owned_lookup_and_snapshot_hide_foreign_nonexistent_and_legacy(self):
        queue = PersistentTaskQueue(self.database_path)
        alice = queue.enqueue(
            "alice",
            {},
            owner_user_id="alice",
            tenant_id="tenant-a",
        )
        bob = queue.enqueue(
            "bob",
            {},
            owner_user_id="bob",
            tenant_id="tenant-a",
        )
        queue.enqueue_legacy_internal("legacy", {})

        self.assertIsNone(queue.get_owned("alice", bob["task_id"]))
        self.assertIsNone(queue.get_owned("alice", "does-not-exist"))
        self.assertIsNone(
            queue.get_owned(
                "alice",
                alice["task_id"],
                tenant_id="wrong-tenant",
            )
        )
        owned = queue.get_owned(
            "alice",
            alice["task_id"],
            tenant_id="tenant-a",
        )
        self.assertEqual(owned["task_id"], alice["task_id"])

        alice_snapshot = queue.snapshot_owned("alice", tenant_id="tenant-a")
        self.assertEqual(alice_snapshot["total"], 1)
        self.assertEqual(
            [item["task_id"] for item in alice_snapshot["active"]],
            [alice["task_id"]],
        )
        internal_ids = {
            item["task_id"] for item in queue.snapshot_internal()["active"]
        }
        self.assertEqual(len(internal_ids), 3)

    def test_asset_references_include_finished_at_for_cleanup_cutoff(self):
        queue = PersistentTaskQueue(self.database_path)
        asset_set_id = "0123456789abcdef0123456789abcdef"
        task = queue.enqueue(
            "cleanup",
            {"config_version": 2, "asset_set_id": asset_set_id},
            owner_user_id="alice",
        )
        self.assertTrue(queue.cancel_waiting(task["task_id"]))

        references = queue.asset_set_references(asset_set_id)
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["task_id"], task["task_id"])
        self.assertIsNotNone(references[0]["finished_at"])

    def test_history_clear_and_hide_are_owner_scoped(self):
        queue = PersistentTaskQueue(self.database_path)
        alice = queue.enqueue("alice", {}, owner_user_id="alice")
        bob = queue.enqueue("bob", {}, owner_user_id="bob")
        self.assertTrue(queue.cancel_waiting(alice["task_id"]))
        self.assertTrue(queue.cancel_waiting(bob["task_id"]))

        self.assertEqual(
            queue.hide_finished_owned("alice", [bob["task_id"]]),
            0,
        )
        self.assertEqual(queue.clear_finished_owned("alice"), 1)
        self.assertEqual(queue.snapshot_owned("alice")["total"], 0)
        self.assertEqual(queue.snapshot_owned("bob")["total"], 1)
        self.assertEqual(
            queue.hide_finished_owned("bob", [bob["task_id"]]),
            1,
        )
        self.assertEqual(queue.snapshot_owned("bob")["total"], 0)

    def test_normal_worker_parks_legacy_unassigned_waiting_rows(self):
        queue = PersistentTaskQueue(self.database_path)
        legacy = queue.enqueue_legacy_internal("legacy", {})

        self.assertIsNone(queue.claim_next())
        claimed = queue.claim_next_legacy_internal()
        self.assertEqual(claimed["task_id"], legacy["task_id"])
        self.assertTrue(claimed["legacy_unassigned"])

    def test_two_workers_cannot_claim_the_same_task(self):
        first_queue = PersistentTaskQueue(self.database_path)
        second_queue = PersistentTaskQueue(self.database_path)
        task_ids = {
            first_queue.enqueue(
                f"task-{index}",
                {},
                owner_user_id=f"user-{index}",
            )["task_id"]
            for index in range(2)
        }
        barrier = threading.Barrier(3)

        def claim(queue):
            barrier.wait()
            return queue.claim_next()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(claim, first_queue),
                executor.submit(claim, second_queue),
            ]
            barrier.wait()
            claimed = [future.result(timeout=5) for future in futures]

        claimed_ids = {item["task_id"] for item in claimed if item is not None}
        self.assertEqual(claimed_ids, task_ids)
        self.assertEqual(len(claimed_ids), 2)
        self.assertEqual(first_queue.snapshot_internal()["running"], 2)

    def test_concurrent_same_owner_submission_is_idempotent(self):
        first_queue = PersistentTaskQueue(self.database_path)
        second_queue = PersistentTaskQueue(self.database_path)
        barrier = threading.Barrier(3)

        def enqueue(queue, title):
            barrier.wait()
            return queue.enqueue(
                title,
                {"task_name": title},
                submission_key="one-click",
                owner_user_id="alice",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(enqueue, first_queue, "first"),
                executor.submit(enqueue, second_queue, "second"),
            ]
            barrier.wait()
            results = [future.result(timeout=5) for future in futures]

        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.assertEqual(first_queue.snapshot_owned("alice")["total"], 1)


if __name__ == "__main__":
    unittest.main()
