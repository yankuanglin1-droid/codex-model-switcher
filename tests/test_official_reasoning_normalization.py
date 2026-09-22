"""Official reasoning adaptation never runs implicitly or deletes history items."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_switcher.message_ids import normalize_record, repair_file


def reasoning_item():
    return {
        "type": "reasoning",
        "id": "rs_fixture",
        "content": [{"type": "reasoning_text", "text": "synthetic reasoning"}],
        "encrypted_content": "synthetic-ciphertext",
        "summary": [{"type": "summary_text", "text": "synthetic summary"}],
        "status": "completed",
        "fixture_metadata": {"content": "preserve nested content"},
    }


class OfficialReasoningNormalizationTests(unittest.TestCase):
    def test_default_and_false_preserve_third_party_reasoning(self):
        for options in ({}, {"official": False}, {"official": "openai"}):
            with self.subTest(options=options):
                record = {"type": "response_item", "payload": reasoning_item()}
                before = copy.deepcopy(record)
                self.assertEqual(normalize_record(record, **options), 0)
                self.assertEqual(record, before)

    def test_explicit_official_response_item_removes_only_content(self):
        record = {"type": "response_item", "payload": reasoning_item(),
                  "timestamp": "synthetic timestamp"}
        expected = copy.deepcopy(record)
        del expected["payload"]["content"]
        self.assertEqual(normalize_record(record, official=True), 1)
        self.assertEqual(record, expected)
        self.assertEqual(normalize_record(record, official=True), 0)

    def test_both_compaction_histories_preserve_all_items_and_other_fields(self):
        for field in ("replacement_history", "guardian_history"):
            with self.subTest(field=field):
                record = {"type": "compacted", "payload": {field: [
                    reasoning_item(),
                    {"type": "message", "id": "msg_fixture", "role": "user",
                     "content": [{"type": "input_text", "text": "synthetic user text"}]},
                    {"type": "function_call", "id": "fc_fixture", "call_id": "pair",
                     "arguments": '{"type":"reasoning","content":["preserve"]}'},
                    {"type": "function_call_output", "call_id": "pair",
                     "output": {"type": "reasoning", "content": ["preserve output"]}},
                    {"type": "unknown", "content": ["preserve unknown"]},
                    None,
                ], "summary": "preserve compaction summary"}}
                expected = copy.deepcopy(record)
                del expected["payload"][field][0]["content"]
                self.assertEqual(normalize_record(record, official=True), 1)
                self.assertEqual(record, expected)

    def test_untyped_nested_content_and_event_records_are_untouched(self):
        for kind in ("event_msg", "session_meta", "turn_context", "unknown"):
            with self.subTest(kind=kind):
                record = {"type": kind, "payload": reasoning_item()}
                before = copy.deepcopy(record)
                self.assertEqual(normalize_record(record, official=True), 0)
                self.assertEqual(record, before)

    def test_empty_and_null_content_are_preserved(self):
        for content in ([], None):
            with self.subTest(content=content):
                item = reasoning_item()
                item["content"] = content
                record = {"type": "response_item", "payload": item}
                self.assertEqual(normalize_record(record, official=True), 0)
                self.assertEqual(item["content"], content)
                self.assertEqual(normalize_record(record, official=True), 0)

    def make_file(self, root):
        rows = [
            {"type": "session_meta", "payload": {"model_provider": "openai"}},
            {"type": "response_item", "payload": reasoning_item()},
            {"type": "compacted", "payload": {
                "replacement_history": [reasoning_item()],
                "guardian_history": [reasoning_item()]}},
        ]
        rows[1]["payload"]["id"] = "foreign_fixture"
        raw = (b"\r\n" + b"\r\n\r\n".join(
            json.dumps(row).encode() for row in rows))
        path = root / "fixture.jsonl"
        path.write_bytes(raw)
        return path, raw, rows

    def test_file_repair_counts_fields_backs_up_and_preserves_record_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, original, rows = self.make_file(root)
            with patch("codex_switcher.history.busy_reason", return_value=None):
                report = repair_file(path, root / "backups", official=True, host_closed=True)
                self.assertEqual(report["changed"], 4)
                self.assertEqual(report["normalized_item_ids"], 1)
                self.assertEqual(report["reasoning_content_removed"], 3)
                self.assertEqual(Path(report["backup"]).read_bytes(), original)
                repaired = path.read_bytes()
                self.assertTrue(repaired.startswith(b"\r\n"))
                self.assertEqual(repaired.count(b"\r\n"), original.count(b"\r\n"))
                self.assertFalse(repaired.endswith(b"\n"))
                result = [json.loads(line) for line in repaired.splitlines() if line.strip()]
                expected = copy.deepcopy(rows)
                del expected[1]["payload"]["content"]
                for field in ("replacement_history", "guardian_history"):
                    del expected[2]["payload"][field][0]["content"]
                result[1]["payload"]["id"] = expected[1]["payload"]["id"]
                self.assertEqual(result, expected)
                self.assertEqual(repair_file(path, root / "backups", official=True, host_closed=True), {
                    "changed": 0, "normalized_item_ids": 0, "reasoning_content_removed": 0})

    def test_dry_run_reports_changes_without_writes_or_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, original, _ = self.make_file(root)
            with patch("codex_switcher.history.busy_reason", return_value=None):
                report = repair_file(path, root / "backups", dry_run=True, official=True)
            self.assertEqual(report, {"changed": 0, "would_change": 4,
                                      "would_normalize_item_ids": 1,
                                      "would_remove_reasoning_content": 3})
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / "backups").exists())

    def test_default_file_repair_never_removes_reasoning_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, _, rows = self.make_file(root)
            with patch("codex_switcher.history.busy_reason", return_value=None):
                report = repair_file(path, root / "backups", host_closed=True)
            self.assertEqual(report["changed"], 1)
            result = [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]
            result[1]["payload"]["id"] = rows[1]["payload"]["id"]
            self.assertEqual(result, rows)


if __name__ == "__main__":
    unittest.main()
