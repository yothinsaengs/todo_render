import unittest
from threading import RLock

from app.google_store import GoogleStore, StoreError, _normalize_due_at


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
        original = {
            "id": "task-1",
            "title": "Ticket",
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
        }
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


if __name__ == "__main__":
    unittest.main()
