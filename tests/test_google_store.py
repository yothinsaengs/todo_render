import unittest

from app.google_store import StoreError, _normalize_due_at


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


if __name__ == "__main__":
    unittest.main()
