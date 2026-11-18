import sqlite3
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import settings as app_settings


class SettingsValidationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def tearDown(self):
        self.conn.close()

    def test_valid_values_round_trip(self):
        written = app_settings.update_settings(self.conn, {
            "songrec_enabled": False,
            "whisper_model_size": "medium",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "scan_workers": 12,
        })
        self.assertEqual(len(written), 5)
        values = app_settings.get_settings(self.conn)
        self.assertFalse(values["songrec_enabled"])
        self.assertEqual(values["whisper_model_size"], "medium")
        self.assertEqual(values["scan_workers"], 12)

    def test_invalid_updates_do_not_partially_write(self):
        with self.assertRaises(ValueError):
            app_settings.update_settings(self.conn, {
                "scan_workers": 4,
                "whisper_device": "pretend-gpu",
            })
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0], 0)

    def test_worker_bounds_and_bool_rejected_as_integer(self):
        for value in (-1, 33, True, "8", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app_settings.validate_updates({"scan_workers": value})

    def test_unknown_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown setting"):
            app_settings.validate_updates({"debug": True})

    def test_bad_persisted_value_falls_back_to_default(self):
        self.conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)", ("whisper_device", "shell"))
        self.conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)", ("scan_workers", "999"))
        values = app_settings.get_settings(self.conn)
        self.assertEqual(values["whisper_device"], "cpu")
        self.assertEqual(values["scan_workers"], 0)


if __name__ == "__main__":
    unittest.main()
