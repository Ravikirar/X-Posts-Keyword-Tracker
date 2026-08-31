import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tracker


class FakeHeaders(dict):
    pass


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        tracker.DB_PATH = Path(self.temp_dir.name) / "tracker.db"
        tracker.initialize_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_saves_metadata_but_not_post_text(self):
        payload = {
            "data": [{
                "id": "12345",
                "author_id": "7",
                "created_at": "2026-08-31T10:00:00Z",
                "text": "SENSITIVE_CONTENT_MUST_NOT_BE_STORED",
            }],
            "includes": {"users": [{"id": "7", "username": "example"}]},
            "meta": {"newest_id": "12345"},
        }
        with patch.object(tracker, "_request_json", return_value=(payload, FakeHeaders())):
            result = tracker.fetch_once("test-token")
        self.assertEqual(result.saved, 1)
        posts = tracker.list_posts()
        self.assertEqual(posts[0]["post_url"], "https://x.com/example/status/12345")
        raw_db = tracker.DB_PATH.read_bytes()
        self.assertNotIn(b"SENSITIVE_CONTENT_MUST_NOT_BE_STORED", raw_db)

    def test_deduplicates_post_id(self):
        payload = {
            "data": [{"id": "9", "author_id": "2", "created_at": "2026-08-31T10:00:00Z"}],
            "includes": {"users": [{"id": "2", "username": "author"}]},
            "meta": {"newest_id": "9"},
        }
        with patch.object(tracker, "_request_json", return_value=(payload, FakeHeaders())):
            self.assertEqual(tracker.fetch_once("token").saved, 1)
            self.assertEqual(tracker.fetch_once("token").saved, 0)
        self.assertEqual(len(tracker.list_posts()), 1)


if __name__ == "__main__":
    unittest.main()
