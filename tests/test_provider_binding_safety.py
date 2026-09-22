import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path


class ProviderBindingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.previous = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.home)
        database = sqlite3.connect(str(self.home / "state_5.sqlite"))
        database.execute("CREATE TABLE threads (id TEXT, title TEXT, model TEXT, model_provider TEXT, rollout_path TEXT, updated_at REAL)")
        self.rollout = self.home / "task.jsonl"
        self.original = (json.dumps({"type": "session_meta", "payload": {"model_provider": "source"}}) + "\n" +
                         json.dumps({"type": "response_item", "payload": {"type": "message", "id": "foreign_msg", "content": [{"type": "output_text", "text": "keep"}]}}) + "\n").encode()
        self.rollout.write_bytes(self.original)
        database.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                         ("task-1", "task", "target-model", "source", str(self.rollout), time.time()))
        database.commit()
        database.close()
        from codex_switcher import state
        state.save({"schema_version": 3, "providers": {"target": {"id": "target", "models": {"target-model": {}}}}})

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.previous
        self.temp.cleanup()

    def test_repair_only_reports_and_preserves_raw_and_database(self):
        from codex_switcher import threads
        report = threads.repair()
        self.assertEqual(report["fixed"], 0)
        self.assertEqual(report["continuation_required"], 1)
        self.assertTrue(report["history_preserved"])
        self.assertEqual(self.rollout.read_bytes(), self.original)
        database = sqlite3.connect(str(self.home / "state_5.sqlite"))
        self.assertEqual(database.execute("SELECT model_provider FROM threads").fetchone()[0], "source")
        database.close()

    def test_follow_switch_never_rewrites_existing_task(self):
        from codex_switcher import threads
        report = threads.follow_switch("source", "target", "target-model")
        self.assertEqual(report["moved"], 0)
        self.assertEqual(report["continuation_required"], 1)
        self.assertEqual(self.rollout.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
