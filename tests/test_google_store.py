import json
import unittest
from threading import RLock

from app.google_store import (
    MERGE_LOG_HEADERS,
    TODO_HEADERS,
    GoogleStore,
    StoreError,
    _normalize_due_at,
)


def task(task_id, **changes):
    value = {
        "id": task_id,
        "title": f"Ticket {task_id}",
        "details": "",
        "status": "inbox",
        "priority": "P3",
        "due_date": "",
        "tags_json": "[]",
        "created_at": "2026-06-30T00:00:00Z",
        "updated_at": "2026-06-30T00:00:00Z",
        "completed_at": "",
        "version": 1,
        "due_at": "",
        "merged_into_id": "",
        "merge_id": "",
        "merged_from_status": "",
        "merged_at": "",
    }
    value.update(changes)
    return value


class DueTimeTests(unittest.TestCase):
    def test_naive_due_time_is_bangkok_time(self):
        self.assertEqual(
            _normalize_due_at("2026-07-01T14:30"), "2026-07-01T14:30+07:00"
        )

    def test_utc_due_time_is_converted_to_bangkok(self):
        self.assertEqual(
            _normalize_due_at("2026-07-01T07:30:00Z"),
            "2026-07-01T14:30+07:00",
        )

    def test_invalid_due_time_is_rejected(self):
        with self.assertRaises(StoreError):
            _normalize_due_at("tomorrow afternoon")


class RemovalTests(unittest.TestCase):
    def test_remove_changes_status_without_deleting_row(self):
        store = GoogleStore.__new__(GoogleStore)
        store._lock = RLock()
        original = task("task-1", title="Ticket")
        writes = []
        store._find_task = lambda task_id: (2, original)
        store._update_values = lambda sheet, row, values: writes.append(
            (sheet, row, values)
        )
        store._bump_database_version = lambda updated_at: 7
        store._log_activity = lambda action, task, version: None

        result = store.remove_task({"id": "task-1", "version": 1})

        self.assertEqual(result["item"]["status"], "removed")
        self.assertEqual(result["databaseVersion"], 7)
        self.assertEqual(writes[0][0:2], ("todos", 2))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.store = GoogleStore.__new__(GoogleStore)
        self.store._lock = RLock()
        self.target = task(
            "target",
            title="Main ticket",
            details="Keep this context",
            priority="P3",
            due_date="2026-07-10",
            tags_json='["main"]',
            version=4,
        )
        self.source_one = task(
            "source-1",
            title="Urgent source",
            details="Important source detail",
            status="blocked",
            priority="P1",
            due_date="2026-07-04",
            due_at="2026-07-04T09:00+07:00",
            tags_json='["api", "main"]',
            version=2,
        )
        self.source_two = task(
            "source-2",
            title="Earlier source",
            status="planned",
            priority="P2",
            due_date="2026-07-02",
            tags_json='["customer"]',
            version=7,
        )
        self.records = {
            "target": (2, self.target),
            "source-1": (3, self.source_one),
            "source-2": (4, self.source_two),
        }
        self.writes = []
        self.store._task_record_map = lambda: self.records
        self.store._values = lambda sheet: [["header"]]
        self.store._batch_write = self._capture_writes

    def _capture_writes(self, updates, updated_at):
        self.writes.extend(updates)
        return 31

    def test_merge_uses_smart_rules_and_soft_removes_sources(self):
        result = self.store.merge_tasks(
            {
                "targetId": "target",
                "targetVersion": 4,
                "sources": [
                    {"id": "source-1", "version": 2},
                    {"id": "source-2", "version": 7},
                ],
            }
        )

        self.assertEqual(result["item"]["title"], "Main ticket")
        self.assertEqual(result["item"]["status"], "inbox")
        self.assertEqual(result["item"]["priority"], "P1")
        self.assertEqual(result["item"]["dueDate"], "2026-07-02")
        self.assertEqual(result["item"]["tags"], ["main", "api", "customer"])
        self.assertIn("Urgent source", result["item"]["details"])
        self.assertEqual(result["removedIds"], ["source-1", "source-2"])
        source_values = dict(zip(TODO_HEADERS, self.writes[1][2]))
        self.assertEqual(source_values["status"], "removed")
        self.assertEqual(source_values["merged_into_id"], "target")
        self.assertEqual(source_values["merged_from_status"], "blocked")
        self.assertEqual(source_values["merge_id"], result["mergeId"])
        self.assertEqual(self.writes[-1][0], "merge_log")

    def test_merge_rejects_duplicate_sources(self):
        with self.assertRaisesRegex(StoreError, "unique"):
            self.store.merge_tasks(
                {
                    "targetId": "target",
                    "targetVersion": 4,
                    "sources": [
                        {"id": "source-1", "version": 2},
                        {"id": "source-1", "version": 2},
                    ],
                }
            )

    def test_bulk_move_updates_all_tasks_in_one_batch(self):
        result = self.store.bulk_update_status(
            {
                "status": "done",
                "tasks": [
                    {"id": "source-1", "version": 2},
                    {"id": "source-2", "version": 7},
                ],
            }
        )

        self.assertEqual([item["status"] for item in result["items"]], ["done", "done"])
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(result["databaseVersion"], 31)

    def test_undo_restores_target_and_source_statuses(self):
        merge_id = "merge-1"
        merged_target = task("target", title="Main ticket", version=5)
        merged_source = task(
            "source-1",
            status="removed",
            version=3,
            merge_id=merge_id,
            merged_into_id="target",
            merged_from_status="blocked",
        )
        log = dict(
            zip(
                MERGE_LOG_HEADERS,
                [
                    merge_id,
                    "target",
                    json.dumps(self.target),
                    json.dumps(["source-1"]),
                    json.dumps({"target": 5, "source-1": 3}),
                    "2026-06-30T01:00:00Z",
                    "",
                ],
            )
        )
        self.store._task_record_map = lambda: {
            "target": (2, merged_target),
            "source-1": (3, merged_source),
        }
        self.store._find_merge_log = lambda requested: (2, log)

        result = self.store.undo_merge({"mergeId": merge_id})

        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual(by_id["target"]["details"], "Keep this context")
        self.assertEqual(by_id["source-1"]["status"], "blocked")
        restored_source = dict(zip(TODO_HEADERS, self.writes[1][2]))
        self.assertEqual(restored_source["merge_id"], "")
        self.assertTrue(self.writes[2][2][-1])

    def test_undo_rejects_changes_made_after_merge(self):
        merge_id = "merge-1"
        log = {
            "merge_id": merge_id,
            "target_id": "target",
            "target_before_json": json.dumps(self.target),
            "source_ids_json": json.dumps(["source-1"]),
            "post_versions_json": json.dumps({"target": 5, "source-1": 3}),
            "created_at": "2026-06-30T01:00:00Z",
            "undone_at": "",
        }
        self.store._find_merge_log = lambda requested: (2, log)
        self.store._task_record_map = lambda: {
            "target": (2, task("target", version=6)),
            "source-1": (3, task("source-1", version=3)),
        }
        with self.assertRaisesRegex(StoreError, "VERSION_CONFLICT"):
            self.store.undo_merge({"mergeId": merge_id})


if __name__ == "__main__":
    unittest.main()
