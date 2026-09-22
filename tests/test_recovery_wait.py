import json
from pathlib import Path
import tempfile
import unittest
from codex_switcher.recovery_wait import CompletionGate


class CompletionGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / 'main.jsonl'
        self.path.write_bytes(b'{}\n')
        self.sources = {'main': self.path}
        self.now = 0
        self.gate = CompletionGate(self.sources, ('main', 'turn'), clock=lambda: self.now)

    def event(self, kind, *, path=None, turn='turn'):
        with (path or self.path).open('ab') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': kind, 'turn_id': turn}}).encode() + b'\n')

    def test_waits_for_exact_completion_and_quiet_period(self):
        self.assertFalse(self.gate.poll(self.sources))
        self.event('task_complete', turn='unrelated')
        self.assertFalse(self.gate.poll(self.sources))
        self.event('task_complete')
        self.assertFalse(self.gate.poll(self.sources))
        self.now = 16
        self.assertTrue(self.gate.poll(self.sources))

    def test_aborted_turn_cancels(self):
        self.event('turn_aborted')
        with self.assertRaises(OSError): self.gate.poll(self.sources)

    def test_new_user_turn_cancels(self):
        self.event('task_complete')
        self.event('task_started', turn='new-turn')
        with self.assertRaises(OSError): self.gate.poll(self.sources)

    def test_goal_followups_keep_waiting_until_every_turn_finishes(self):
        gate = CompletionGate(self.sources, ('main', 'turn'), clock=lambda: self.now,
                              allow_followup_turns=True)
        self.event('task_complete')
        self.event('task_started', turn='followup')
        self.assertFalse(gate.poll(self.sources))
        self.now = 50
        self.assertFalse(gate.poll(self.sources))
        self.event('task_complete', turn='followup')
        self.assertFalse(gate.poll(self.sources))
        self.now = 66
        self.assertTrue(gate.poll(self.sources))
        self.event('task_started', turn='second-followup')
        self.assertFalse(gate.poll(self.sources))
        self.now = 100
        self.assertFalse(gate.poll(self.sources))
        self.event('task_complete', turn='second-followup')
        self.assertFalse(gate.poll(self.sources))
        self.now = 116
        self.assertTrue(gate.poll(self.sources))

    def test_goal_mode_does_not_allow_initial_interruption_or_source_replacement(self):
        for failure in ('abort', 'replace'):
            with self.subTest(failure=failure):
                gate = CompletionGate(self.sources, ('main', 'turn'), allow_followup_turns=True)
                if failure == 'abort':
                    self.event('turn_aborted')
                else:
                    temporary = self.home / 'new.jsonl'
                    temporary.write_bytes(b'{}\n')
                    temporary.replace(self.path)
                with self.assertRaises(OSError):
                    gate.poll(self.sources)

    def test_new_task_must_finish(self):
        self.event('task_complete')
        other = self.home / 'other.jsonl'
        self.event('task_started', path=other, turn='other-turn')
        self.sources['other'] = other
        self.assertFalse(self.gate.poll(self.sources))
        self.now = 16
        self.assertFalse(self.gate.poll(self.sources))
        self.event('task_complete', path=other, turn='other-turn')
        self.assertFalse(self.gate.poll(self.sources))
        self.now = 32
        self.assertTrue(self.gate.poll(self.sources))

    def test_truncation_never_authorizes_shutdown(self):
        self.path.write_bytes(b'')
        with self.assertRaises(OSError): self.gate.poll(self.sources)

    def test_replacement_never_authorizes_shutdown(self):
        old = self.path.with_suffix('.old')
        self.path.rename(old)
        self.path.write_bytes(b'{}\n')
        with self.assertRaises(OSError): self.gate.poll(self.sources)

    def test_archive_of_required_task_cancels(self):
        with self.assertRaises(OSError): self.gate.poll({})

    def test_partial_line_is_not_lost(self):
        data = json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn'}}).encode()
        with self.path.open('ab') as stream: stream.write(data[:20])
        gate = CompletionGate(self.sources, ('main', 'turn'), clock=lambda: self.now)
        with self.path.open('ab') as stream: stream.write(data[20:] + b'\n')
        self.assertFalse(gate.poll(self.sources))
        self.now = 16
        self.assertTrue(gate.poll(self.sources))
