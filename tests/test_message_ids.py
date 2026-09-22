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
        self.assertEqual(normalize_record(record), 2)
        self.assertEqual(normalize_record(record), 0)
        items = record['payload']['replacement_history']
        self.assertTrue(items[0]['id'].startswith('msg_'))
        self.assertTrue(items[1]['id'].startswith('fc_'))
        items[0]['id'] = 'foreign_msg'
        items[1]['id'] = 'foreign_fc'
        self.assertEqual(record, before)

    def test_backup_blank_lines_and_final_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'rollout.jsonl'
            raw = b'\n'+json.dumps({'type':'response_item','payload':{'type':'message','id':'foreign_msg','content':[]}}).encode()+b'\n\n'
            p.write_bytes(raw)
            with patch('codex_switcher.history.busy_reason', return_value=None):
                result = repair_file(p, Path(d)/'backups', host_closed=True)
                self.assertEqual(result['changed'], 1)
                self.assertEqual(Path(result['backup']).read_bytes(), raw)
                self.assertTrue(p.read_bytes().startswith(b'\n'))
                self.assertTrue(p.read_bytes().endswith(b'\n\n'))
                self.assertEqual(repair_file(p, Path(d)/'backups', host_closed=True)['changed'], 0)

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

    def test_typed_ids_in_both_compaction_histories(self):
        for field in ('replacement_history', 'guardian_history'):
            for kind, prefix in (('message', 'msg'), ('function_call', 'fc'), ('reasoning', 'rs')):
                item = {'type': kind, 'id': 'foreign_' + prefix, 'call_id': 'foreign_fc',
                        'content': [{'text': 'foreign_fc'}], 'summary': [], 'arguments': '{}'}
                before = copy.deepcopy(item)
                record = {'type': 'compacted', 'payload': {field: [item]}}
                self.assertEqual(normalize_record(record), 1)
                self.assertTrue(item['id'].startswith(prefix + '_'))
                self.assertEqual(normalize_record(record), 0)
                item['id'] = before['id']
                self.assertEqual(item, before)

    def test_official_and_unknown_ids_are_preserved(self):
        for kind, value in (('function_call', 'fc_valid'), ('reasoning', 'rs_valid'),
                            ('function_call_output', 'fco_valid'), ('unknown', 'foreign')):
            record = {'type': 'response_item', 'payload': {'type': kind, 'id': value}}
            before = copy.deepcopy(record)
            self.assertEqual(normalize_record(record), 0)
            self.assertEqual(record, before)

    def test_function_call_file_repair_preserves_pair(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'rollout.jsonl'
            records = [
                {'type': 'response_item', 'payload': {'type': 'function_call',
                 'id': 'foreign_fc_0', 'call_id': 'shared_call', 'name': 'test', 'arguments': '{}'}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output',
                 'call_id': 'shared_call', 'output': 'foreign_fc_0'}}]
            original = b'\n'.join(json.dumps(r).encode() for r in records)
            p.write_bytes(original)
            with patch('codex_switcher.history.busy_reason', return_value=None):
                result = repair_file(p, Path(d)/'backups', host_closed=True)
            self.assertEqual(result['changed'], 1)
            self.assertEqual(Path(result['backup']).read_bytes(), original)
            actual = [json.loads(l) for l in p.read_bytes().splitlines()]
            actual[0]['payload']['id'] = 'foreign_fc_0'
            self.assertEqual(actual, records)

    def test_switch_to_official_normalizes_compacted_history(self):
        from codex_switcher import threads
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'rollout.jsonl'
            rows = [
                {'type': 'session_meta', 'payload': {'model_provider': 'example'}},
                {'type': 'compacted', 'payload': {'replacement_history': [
                    {'type': 'function_call', 'id': 'foreign_fc', 'call_id': 'pair'},
                    {'type': 'function_call_output', 'call_id': 'pair', 'output': 'retained'}]}}]
            p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            with patch('codex_switcher.history.busy_reason', return_value=None):
                threads._rewrite_session_file(p, 'example', 'openai', Path(d)/'backups', host_closed=True)
            result = [json.loads(l) for l in p.read_text().splitlines()]
            items = result[1]['payload']['replacement_history']
            self.assertTrue(items[0]['id'].startswith('fc_'))
            self.assertEqual(items[0]['call_id'], items[1]['call_id'])
            self.assertEqual(items[1]['output'], 'retained')

    def test_sweep_dry_run_never_rewrites_ids(self):
        from codex_switcher import history
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'rollout.jsonl'
            original = json.dumps({'type': 'response_item', 'payload': {
                'type': 'function_call', 'id': 'foreign_fc', 'call_id': 'pair'}}).encode()
            p.write_bytes(original)
            with patch.object(history, 'recent_rollouts', return_value=[p]), \
                 patch.object(history, 'busy_reason', return_value=None), \
                 patch.object(history, '_backup_root', return_value=Path(d)/'backups'), \
                 patch.object(history, '_load_sweep_ledger', return_value={}), \
                 patch.object(history, '_save_sweep_ledger'):
                report = history.sweep_all(dry_run=True)
                self.assertEqual(report["would_normalize_ids"], 1)
            self.assertEqual(p.read_bytes(), original)
            self.assertFalse((Path(d)/'backups').exists())
