import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_switcher.message_ids import normalize_record, repair_file


class MessageIDs(unittest.TestCase):
    def test_normalization_preserves_content_and_tool_pairing(self):
        record = {'type': 'compacted', 'payload': {'replacement_history': [
            {'type': 'message', 'id': 'foreign_msg', 'content': [{'text': 'foreign_msg'}]},
            {'type': 'function_call', 'id': 'foreign_fc', 'call_id': 'tool-1'},
            {'type': 'function_call_output', 'call_id': 'tool-1', 'output': 'result'},
            {'type': 'message', 'id': 'msg_official', 'content': []}]}}
        before = copy.deepcopy(record)
        self.assertEqual(normalize_record(record), 1)
        self.assertEqual(normalize_record(record), 0)
        items = record['payload']['replacement_history']
        self.assertTrue(items[0]['id'].startswith('msg_'))
        items[0]['id'] = 'foreign_msg'
        self.assertEqual(record, before)

    def test_backup_blank_lines_and_final_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'rollout.jsonl'
            raw = b'\n'+json.dumps({'type':'response_item','payload':{'type':'message','id':'foreign_msg','content':[]}}).encode()+b'\n\n'
            p.write_bytes(raw)
            with patch('codex_switcher.history.busy_reason', return_value=None):
                result = repair_file(p, Path(d)/'backups')
                self.assertEqual(result['changed'], 1)
                self.assertEqual(Path(result['backup']).read_bytes(), raw)
                self.assertTrue(p.read_bytes().startswith(b'\n'))
                self.assertTrue(p.read_bytes().endswith(b'\n\n'))
                self.assertEqual(repair_file(p, Path(d)/'backups')['changed'], 0)

    def test_busy_file_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'rollout.jsonl'; p.write_bytes(b'private history')
            with patch('codex_switcher.history.busy_reason', return_value='codex-open'):
                self.assertEqual(repair_file(p, Path(d)/'backups')['changed'], 0)
            self.assertEqual(p.read_bytes(), b'private history')

    def test_malformed_history_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'rollout.jsonl'; p.write_bytes(b'{bad json')
            with patch('codex_switcher.history.busy_reason', return_value=None):
                with self.assertRaises(ValueError): repair_file(p, Path(d)/'backups')
            self.assertEqual(p.read_bytes(), b'{bad json')
