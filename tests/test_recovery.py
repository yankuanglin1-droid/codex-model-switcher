import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from codex_switcher import recovery, codexapp

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        idle = patch.object(codexapp, 'assert_history_idle')
        idle.start()
        self.addCleanup(idle.stop)

    def test_false_applescript_output_is_not_running(self):
        with patch.object(codexapp.subprocess, 'run', return_value=Mock(returncode=0, stdout='false\n')):
            self.assertFalse(codexapp.is_running())

    def test_failed_probe_is_not_treated_as_closed(self):
        with patch.object(codexapp.subprocess, 'run', return_value=Mock(returncode=1, stdout='')):
            with self.assertRaises(RuntimeError): codexapp.is_running()

    def test_binary_plist(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'Renamed.app'; (p/'Contents').mkdir(parents=True)
            (p/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':codexapp.BUNDLE_ID},fmt=plistlib.FMT_BINARY))
            with patch.object(codexapp.sys,'platform','darwin'), patch.object(codexapp,'CANDIDATES',[str(p)]):
                self.assertEqual(codexapp.find_app(),str(p))

    def test_running_host_never_triggers_repair_or_relaunch(self):
        recovery._LOCK.acquire()
        with patch.object(codexapp,'find_app',return_value='/fixture'), patch.object(codexapp,'is_running',return_value=True), patch.object(codexapp,'_quit',return_value=False), patch.object(codexapp,'_reopen') as reopen, patch.object(recovery.message_ids,'repair_file') as repair:
            recovery._run()
            repair.assert_not_called(); reopen.assert_not_called()
            self.assertEqual(recovery.status()['phase'],'online-readonly')

    def test_success_after_verified_closed_pipeline_and_reopen(self):
        recovery._set(checked=0,changed=0,skipped=0,failed=0)
        recovery._LOCK.acquire()
        with patch.object(codexapp,'find_app',return_value='/fixture'), patch.object(codexapp,'is_running',return_value=False), patch.object(codexapp,'_reopen',return_value=True), patch.object(recovery,'run_closed',return_value={'phase':'done'}) as repair:
            recovery._run()
            repair.assert_called_once()
            self.assertTrue(recovery.status()['reopened'])
            self.assertEqual(recovery.status()['phase'],'done')

    def test_partial_repair_does_not_report_done(self):
        recovery._LOCK.acquire()
        with patch.object(codexapp,'find_app',return_value='/fixture'), patch.object(codexapp,'is_running',return_value=False), patch.object(codexapp,'_reopen',return_value=True), patch.object(recovery,'run_closed',return_value={'phase':'partial'}):
            recovery._run()
            self.assertEqual(recovery.status()['phase'],'partial')

    def test_second_click_reuses_existing_job(self):
        recovery._LOCK.acquire()
        try:
            with patch.object(recovery.sys,'platform','darwin'),patch.object(recovery.threading,'Thread') as thread:
                recovery.start();thread.assert_not_called()
        finally: recovery._LOCK.release()

    def test_manual_wait_stays_active_until_host_exits_without_forcing_quit(self):
        recovery._LOCK.acquire()
        recovery._CANCEL_WAIT.clear()
        with patch.object(codexapp, 'find_app', return_value='/fixture'), \
                patch.object(codexapp, 'is_running', side_effect=[True, True, False, False]), \
                patch.object(codexapp, '_quit') as quit_host, \
                patch.object(codexapp, '_reopen', return_value=True), \
                patch.object(recovery._CANCEL_WAIT, 'wait', return_value=False) as wait, \
                patch.object(recovery, 'run_closed', return_value={'phase': 'done'}) as repair:
            recovery._run(wait_for_exit=True)
        self.assertEqual(wait.call_count, 2)
        quit_host.assert_not_called()
        repair.assert_called_once()
        self.assertEqual(recovery.status()['phase'], 'done')

    def test_manual_wait_cancel_does_not_write_or_reopen(self):
        recovery._LOCK.acquire()
        with patch.object(codexapp, 'find_app', return_value='/fixture'), \
                patch.object(codexapp, 'is_running', return_value=True), \
                patch.object(recovery._CANCEL_WAIT, 'wait', side_effect=lambda _: recovery.cancel_wait()['cancelled']), \
                patch.object(codexapp, '_reopen') as reopen, \
                patch.object(recovery, 'run_closed') as repair:
            recovery._run(wait_for_exit=True)
        repair.assert_not_called()
        reopen.assert_not_called()
        self.assertEqual(recovery.status()['phase'], 'cancelled')
        recovery._CANCEL_WAIT.clear()

    def test_cancel_is_refused_once_repairing(self):
        recovery._set(phase='repairing')
        self.assertFalse(recovery.cancel_wait()['cancelled'])
        recovery._set(phase='idle')

class AutomationAuditTests(unittest.TestCase):
    def test_fixed_model_and_missing_heartbeat_are_reported_without_writes(self):
        from codex_switcher import automation_audit, configfile
        if not configfile.toml_available(): self.skipTest('TOML parser unavailable')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name,content in [('cron','kind="cron"\nmodel="old-model"\n'),('heartbeat','kind="heartbeat"\ntarget_thread_id="missing"\n')]:
                p=root/'automations'/name/'automation.toml';p.parent.mkdir(parents=True);p.write_text(content)
            before={p:p.read_bytes() for p in root.rglob('*.toml')}
            with patch.object(automation_audit.paths,'codex_home',return_value=root),patch.object(automation_audit.threads,'list_threads',return_value=[]):
                result=automation_audit.inspect('example','new-model')
            self.assertEqual(result['needs_review'],2)
            self.assertFalse(result['trigger_verified'])
            self.assertEqual(before,{p:p.read_bytes() for p in before})

class HistoryWriteTests(unittest.TestCase):
    def test_clean_preserves_blank_lines_and_backs_up(self):
        from codex_switcher import history
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'history.jsonl'
            valid=json.dumps({'type':'response_item','payload':{'type':'message','content':[]}})
            orphan=json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':'test'}})
            original='\n'+valid+'\n\n'+orphan+'\n'
            p.write_text(original)
            with patch.object(history,'busy_reason',return_value=None):
                changed, stats=history.sanitize(p,False,Path(d)/'backup')
            self.assertFalse(changed)
            self.assertEqual(p.read_text(),original)
            self.assertEqual(stats['skipped'], 'destructive-cleanup-disabled')
            self.assertFalse((Path(d)/'backup').exists())

    def test_clean_wont_replace_host_open_file(self):
        from codex_switcher import history
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'history.jsonl'
            original=json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':'test'}})+'\n'
            p.write_text(original)
            with patch.object(history,'busy_reason',return_value='codex-open'):
                changed,_=history.sanitize(p,False,Path(d)/'backup')
            self.assertFalse(changed)
            self.assertEqual(p.read_text(),original)

class RuntimeVersionTests(unittest.TestCase):
    def test_old_gui_is_not_reused(self):
        from codex_switcher.webui import server
        from codex_switcher import platform_compat
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'gui.json';p.write_text(json.dumps({'pid':45678,'port':1234,'token':'dummy','version':'old'}))
            with patch.object(server.paths,'gui_state_file',return_value=p),patch.object(platform_compat,'process_command_line',return_value='python -m codex_switcher app --no-open'),patch('os.kill') as kill:
                self.assertIsNone(server.existing_url())
                kill.assert_called_once()

    def test_unknown_process_not_terminated(self):
        from codex_switcher.webui import server
        from codex_switcher import platform_compat
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'gui.json';p.write_text(json.dumps({'pid':45678,'version':'old'}))
            with patch.object(server.paths,'gui_state_file',return_value=p),patch.object(platform_compat,'process_command_line',return_value='unrelated process'),patch('os.kill') as kill:
                with self.assertRaises(RuntimeError):server.existing_url()
                kill.assert_not_called()
