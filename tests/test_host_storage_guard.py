import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from codex_switcher import codexapp


class HostStorageGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / 'state_5.sqlite').touch()
        for context in (patch.object(codexapp.sys, 'platform', 'darwin'),
                        patch.object(codexapp, 'find_app', return_value='/Applications/Fixture.app')):
            context.start()
            self.addCleanup(context.stop)

    def run_check(self, inventory, holders='', code=0, error=''):
        outputs = [Mock(returncode=0, stdout=inventory),
                   Mock(returncode=code, stdout=holders, stderr=error)]
        with patch.object(codexapp.subprocess, 'run', side_effect=outputs):
            codexapp.assert_history_idle(self.home)

    def test_idle_and_no_handles(self):
        self.run_check('10 1 /usr/bin/unrelated\n', code=1)

    def test_lingering_app_server_refuses(self):
        with self.assertRaises(OSError):
            self.run_check('10 1 /Applications/Fixture.app/Contents/Resources/codex app-server\n')

    def test_owned_reader_is_allowed(self):
        pid = os.getpid()
        child = pid + 100000
        self.run_check(f'{pid} 1 python\n{child} {pid} /Applications/Fixture.app/Contents/Resources/codex app-server\n',
                       f'{pid}\n{child}\n')

    def test_external_database_holder_refuses(self):
        with self.assertRaises(OSError):
            self.run_check('10 1 another-python\n', '10\n')

    def test_failed_handle_probe_refuses(self):
        with self.assertRaises(OSError):
            self.run_check('10 1 python\n', code=1, error='permission denied')

    def test_unexpected_inventory_refuses(self):
        with self.assertRaises(OSError):
            self.run_check('invalid inventory')

    def test_disappearing_sqlite_sidecar_requires_a_fresh_clean_probe(self):
        sidecar = self.home / 'state_5.sqlite-shm'
        sidecar.touch()
        calls = []
        def run(command, **kwargs):
            calls.append(command[0])
            if command[0] == '/bin/ps':
                return Mock(returncode=0, stdout='10 1 unrelated\n')
            if sidecar.exists():
                sidecar.unlink()
                return Mock(returncode=1, stdout='', stderr='cannot stat missing sidecar')
            return Mock(returncode=1, stdout='', stderr='')
        with patch.object(codexapp.subprocess, 'run', side_effect=run):
            codexapp.assert_history_idle(self.home)
        self.assertEqual(calls, ['/bin/ps', '/usr/sbin/lsof'] * 2)

    def test_sidecar_retry_does_not_allow_external_holder(self):
        sidecar = self.home / 'state_5.sqlite-shm'
        sidecar.touch()
        def run(command, **kwargs):
            if command[0] == '/bin/ps':
                return Mock(returncode=0, stdout='10 1 unrelated\n')
            if sidecar.exists():
                sidecar.unlink()
                return Mock(returncode=1, stdout='', stderr='missing file')
            return Mock(returncode=0, stdout='10\n', stderr='')
        with patch.object(codexapp.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(OSError, 'Another process'):
                codexapp.assert_history_idle(self.home)

    def test_sidecar_retry_does_not_ignore_persistent_probe_errors(self):
        sidecar = self.home / 'state_5.sqlite-shm'
        sidecar.touch()
        def run(command, **kwargs):
            if command[0] == '/bin/ps':
                return Mock(returncode=0, stdout='10 1 unrelated\n')
            sidecar.unlink(missing_ok=True)
            return Mock(returncode=1, stdout='', stderr='permission denied')
        with patch.object(codexapp.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(OSError, 'Cannot verify history storage handles'):
                codexapp.assert_history_idle(self.home)

    def test_constant_sidecar_churn_refuses_after_three_probes(self):
        sidecar = self.home / 'state_5.sqlite-shm'
        probes = []
        def run(command, **kwargs):
            if command[0] == '/bin/ps':
                return Mock(returncode=0, stdout='10 1 unrelated\n')
            probes.append(command)
            if sidecar.exists():
                sidecar.unlink()
            else:
                sidecar.touch()
            return Mock(returncode=1, stdout='', stderr='')
        with patch.object(codexapp.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(OSError, 'History storage changed'):
                codexapp.assert_history_idle(self.home)
        self.assertEqual(len(probes), 3)

    def test_main_database_disappearing_is_not_a_retryable_sidecar_race(self):
        def run(command, **kwargs):
            if command[0] == '/bin/ps':
                return Mock(returncode=0, stdout='10 1 unrelated\n')
            (self.home / 'state_5.sqlite').unlink()
            return Mock(returncode=1, stdout='', stderr='missing database')
        with patch.object(codexapp.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(OSError, 'History database changed'):
                codexapp.assert_history_idle(self.home)
