"""Exercise installed continuity CLI and real SQLite against a fake native host."""
import json
from contextlib import closing
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import settings

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Continue authorized fixture work; no external writes.'}


class ContinuityCliTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / 'settings.json'
        self.config.write_text(json.dumps({'schema_version': 1, 'coordinators': [TARGET]}))
        self.config.chmod(0o600)
        self.dispositions = self.root / 'dispositions.json'
        self.journal = self.root / 'private/journal.sqlite3'
        self.events = self.root / 'calls.jsonl'
        self.cli = self.root / 'fake-orca'
        self.cli.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
with Path(os.environ['FAKE_CALLS']).open('a') as output:
    output.write(json.dumps(sys.argv[1:])+'\\n')
command=sys.argv[1:3]
data={}
if command[0]=='status': data={'runtime':{'reachable':True,'state':'ready'}}
elif command==['worktree','ps']:
    data={'worktrees':[{'worktreeId':'repo::/work','agents':[{'paneKey':'pane_1','state':'done','stateStartedAt':1000}]}]}
elif command==['terminal','list']:
    data={'terminals':[{'handle':'term_1','incarnationId':'process_1','worktreeId':'repo::/work','paneKey':'pane_1','connected':True,'writable':True}]}
elif command==['orchestration','run-list']:
    data={'runs':[{'id':'run_1','coordinator_handle':'term_1','legacy':0}],'nextCursor':None}
elif command==['orchestration','run-show']:
    data={'run':{'id':'run_1','coordinator_handle':'term_1','legacy':0}}
elif command==['orchestration','worker-list']:
    data={'workers':[],'page':{'hasMore':False},'scope':{'run':'run_1'}}
elif command==['orchestration','check']:
    data={'runId':'run_1','messages':[]}
elif command==['orchestration','gate-list']:
    data={'runId':'run_1','gates':[{'id':'gate_1','status':'pending'}]}
elif command==['terminal','send']:
    data={'send':{'handle':'term_1','accepted':True,'prompt':{'requestId':'request_1','processIncarnation':'process_1','stages':['turn_started']}}}
print(json.dumps({'ok':True,'result':data}))
''')
        self.cli.chmod(0o755)

    def run_cli(self, action, extra=(), stdin=None, executable=None, caller=None):
        return subprocess.run([str(executable or ROOT / 'scripts/spectre-superagent.py'),
                               '--config', str(self.config), '--dispositions', str(self.dispositions),
                               '--journal', str(self.journal), '--orca', str(self.cli), action, *extra],
                              input=stdin, text=True, capture_output=True, timeout=10,
                              env={**os.environ, 'FAKE_CALLS': str(self.events), **(caller or {})}, check=False)

    def calls(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def test_status_never_sends_input_or_acknowledges_native_mail(self):
        result = self.run_cli('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['queue']['events'], 0)
        self.assertEqual(self.calls(), [])

    def offload_fixture(self, run_id='run_1'):
        from superagent_queue import offload_receipt
        from test_superagent_offload_receipt import BINDING, COMMAND, remote_result
        self.journal.parent.mkdir(mode=0o700)
        root = self.journal.parent / 'offloads'
        offload_receipt.run(root, 'job_1', {**BINDING, 'run_id': run_id}, COMMAND, ['/bin/true'])
        path = root / 'job_1/command-result.json'
        path.write_text(json.dumps(remote_result()))
        path.chmod(0o600)
        return root

    def test_offload_reconcile_cli_observes_late_receipt_without_remote_execution(self):
        root = self.offload_fixture()
        before = (root / 'job_1/exit.json').read_bytes()
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        result = self.run_cli('offload-reconcile', ['--run', 'run_1', '--job', 'job_1'], caller=caller)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['queued'], 2)
        self.assertFalse(report['remote_execution'])
        self.assertFalse(report['capacity_released'])
        self.assertEqual((root / 'job_1/exit.json').read_bytes(), before)
        self.assertFalse(any(call[:2] in (['terminal', 'send'], ['orchestration', 'worker-start']) for call in self.calls()))

    def test_offload_reconcile_cli_refuses_foreign_caller_and_run(self):
        root = self.offload_fixture()
        result = self.run_cli('offload-reconcile', ['--run', 'run_1', '--job', 'job_1'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('exact current coordinator pane', result.stderr)
        self.assertFalse((root / 'job_1/remote-exit.json').exists())

    def test_offload_reconcile_cli_refuses_another_runs_receipt(self):
        self.offload_fixture(run_id='other_run')
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        result = self.run_cli('offload-reconcile', ['--run', 'run_1', '--job', 'job_1'], caller=caller)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('another native Run', result.stderr)

    def test_fetch_requires_explicit_existing_studio_binding_before_network(self):
        root = self.offload_fixture()
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        result = self.run_cli('offload-reconcile', ['--run', 'run_1', '--job', 'job_1', '--fetch'], caller=caller)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('approved existing Studio', result.stderr)
        self.assertFalse((root / 'job_1/remote-exit.json').exists())

    def test_actual_cli_fetch_reconciles_configured_receipt_without_replaying_commands(self):
        from superagent_queue import execution, offload_receipt
        from test_superagent_offload_receipt import remote_result
        root = self.offload_fixture()
        (root / 'job_1/command-result.json').unlink()
        original = offload_receipt.inspect(root, 'job_1')
        execution.persist(root / 'job_1', 'transport.json', {'schema_version': 1, 'job_id': 'job_1',
            'command_sha256': original['command_sha256'], 'studio': 'fixture-studio',
            'remote_dir': 'offload/job_1', 'recorded_at': original['created_at']})
        self.config.write_text(json.dumps({'schema_version': 1, 'coordinators': [TARGET],
                                          'offload': [{'run_id': 'run_1', 'studio': 'fixture-studio'}]}))
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        ssh = bin_dir / 'ssh'
        ssh.write_text('#!' + sys.executable + '\nimport json,sys\n'
            'assert sys.argv[-2]=="fixture-studio"\n'
            'assert sys.argv[-1]==\'head -c 65537 -- "$HOME"/offload/job_1/.spectre-offload/command-result.json\'\n'
            f'print({json.dumps(remote_result())!r})\n')
        ssh.chmod(0o755)
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1',
                  'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
        for expected in (2, 0):
            result = self.run_cli('offload-reconcile', ['--run', 'run_1', '--job', 'job_1', '--fetch'], caller=caller)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['queued'], expected)
            self.assertFalse(report['capacity_released'])
            self.assertFalse(report['remote_execution'])
        self.assertEqual(offload_receipt.inspect(root, 'job_1')['remote_observation'], 'unverifiable')

    def artifact_fixture(self):
        import hashlib
        import importlib.util
        from superagent_queue import execution, offload_receipt
        from test_superagent_offload_receipt import BINDING, COMMAND, remote_result
        self.journal.parent.mkdir(mode=0o700)
        packet = self.journal.parent / 'packet.json'
        packet.write_text(json.dumps({'schema_version': 1, 'command': COMMAND, 'artifacts': ['result.json']}))
        packet.chmod(0o600)
        root = self.journal.parent / 'offloads'
        binding = {**BINDING, 'packet_sha256': hashlib.sha256(packet.read_bytes()).hexdigest()}
        record = offload_receipt.run(root, 'job_1', binding, COMMAND, ['/bin/true'],
                                    authority={'packet_path': str(packet)})
        execution.persist(root / 'job_1', 'command-result.json', remote_result())
        offload_receipt.reconcile(root, 'job_1')
        spec = importlib.util.spec_from_file_location('artifact_helper', ROOT / 'scripts/offload-artifacts.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        target = helper.prepare(self.journal.parent / 'offload-artifacts', 'job_1', record['command_sha256'], ['result.json'])
        (target / 'result.json').write_text('actual artifact')
        helper.persist(target / 'manifest.json', helper.manifest(target, 'job_1', record['command_sha256'], ['result.json']))
        helper.verify(target, 'job_1', record['command_sha256'], ['result.json'])
        return root

    def test_artifact_cli_publishes_independent_review_context_and_refuses_foreign_caller(self):
        root = self.artifact_fixture()
        result = self.run_cli('offload-artifacts', ['--run', 'run_1', '--job', 'job_1'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('current coordinator pane', result.stderr)
        self.assertFalse((root / 'job_1/artifact-exit.json').exists())
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        for expected in (3, 0):
            result = self.run_cli('offload-artifacts', ['--run', 'run_1', '--job', 'job_1'], caller=caller)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['queued'], expected)
            self.assertFalse(json.loads(result.stdout)['capacity_released'])
        self.assertFalse(any(call[:2] in (['terminal', 'send'], ['orchestration', 'worker-start']) for call in self.calls()))

    def test_artifact_cli_fetch_requires_approved_studio_before_network(self):
        root = self.artifact_fixture()
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        result = self.run_cli('offload-artifacts', ['--run', 'run_1', '--job', 'job_1', '--fetch'], caller=caller)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('approved existing Studio', result.stderr)
        self.assertFalse((root / 'job_1/artifact-exit.json').exists())

    def test_offload_cleanup_cli_requires_approved_studio_and_does_not_infer_native_review(self):
        root = self.artifact_fixture()
        caller = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
        request = json.dumps({'message_id': 'missing', 'artifact_sha256': 'a' * 64})
        result = self.run_cli('offload-cleanup', ['--run', 'run_1', '--job', 'job_1'], stdin=request, caller=caller)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('approved existing Studio', result.stderr)
        self.assertFalse((root / 'job_1/cleanup/intent.json').exists())

    def test_offload_cleanup_cli_exposes_reconciliation_without_native_mutation(self):
        from unittest.mock import patch
        import importlib.util
        spec = importlib.util.spec_from_file_location('continuity_cleanup_cli', ROOT / 'scripts/spectre-superagent.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(sys, 'argv', ['spectre-superagent', 'offload-cleanup', '--run', 'run_1', '--job', 'job_1', '--reconcile']):
            args = module.arguments()
        self.assertTrue(args.reconcile)
        self.assertEqual(args.action, 'offload-cleanup')

    def test_tick_is_observation_only_without_apply(self):
        result = self.run_cli('tick')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['applied'])
        self.assertFalse(any(call[:2] == ['terminal', 'send'] for call in self.calls()))

    def test_two_real_ticks_deliver_one_event_then_reopen_without_duplicate_send(self):
        for _ in range(2):
            result = self.run_cli('tick', ['--apply'])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['queue']['events'], 1)
        self.assertEqual(report['queue']['effects']['delivered'], 1)
        self.assertEqual(sum(call[:2] == ['terminal', 'send'] for call in self.calls()), 1)

    def test_private_structured_approval_wait_prevents_tick_wake(self):
        self.dispositions.write_text(json.dumps({'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}))
        self.dispositions.chmod(0o600)
        result = self.run_cli('tick', ['--apply'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['queue']['events'], 0)
        self.assertFalse(any(call[:2] == ['terminal', 'send'] for call in self.calls()))

    def test_enqueue_persists_untrusted_context_before_any_delivery(self):
        event = {'source': 'operator', 'event_id': 'input_1', 'content': {'text': 'fixture question'}}
        result = self.run_cli('enqueue', ['--run', 'run_1'], json.dumps(event))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['queue']['effects']['pending'], 1)
        self.assertEqual(self.calls(), [])

    def test_tick_lock_does_not_block_input_status_or_owner_disposition(self):
        event = {'source': 'operator', 'event_id': 'input_1', 'content': {'text': 'fixture question'}}
        with settings.lock(self.journal.with_suffix('.lock')):
            result = self.run_cli('enqueue', ['--run', 'run_1'], json.dumps(event))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_cli('status')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['queue']['effects']['pending'], 1)
            result = self.run_cli('disposition', ['--run', 'run_1', '--expect-revision', '0'],
                                  json.dumps({'state': 'waiting_approval', 'gate_ids': ['gate_1']}),
                                  caller={'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['published']['revision'], 1)
        self.assertFalse(any(call[:2] == ['terminal', 'send'] for call in self.calls()))

    def test_tick_lock_still_prevents_concurrent_effect_execution(self):
        with settings.lock(self.journal.with_suffix('.lock')):
            result = self.run_cli('tick', ['--apply'])
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertIn('another continuity tick owns the operational lock', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_nonprivate_config_is_an_error_not_default_automation(self):
        self.config.chmod(0o644)
        result = self.run_cli('tick', ['--apply'])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('private', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_invalid_input_has_no_effect_and_reports_exact_failure(self):
        result = self.run_cli('enqueue', ['--run', 'run_1'], '{broken')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('Expecting property name', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_native_owner_publishes_disposition_and_next_process_holds_automatic_resume(self):
        result = self.run_cli('disposition', ['--run', 'run_1', '--expect-revision', '0'],
                              json.dumps({'state': 'waiting_approval', 'gate_ids': ['gate_1']}),
                              caller={'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['published']['revision'], 1)
        result = self.run_cli('tick', ['--apply'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['queue']['events'], 0)
        result = self.run_cli('status')
        self.assertEqual(json.loads(result.stdout)['published_dispositions']['run_1']['revision'], 1)
        self.assertFalse(any(call[:2] == ['terminal', 'send'] for call in self.calls()))

    def test_disposition_from_unbound_process_cannot_create_a_hold(self):
        result = self.run_cli('disposition', ['--run', 'run_1', '--expect-revision', '0'],
                              json.dumps({'state': 'finished'}),
                              caller={'ORCA_TERMINAL_HANDLE': '', 'ORCA_PANE_KEY': ''})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('native owner', result.stderr)
        self.assertEqual(json.loads(self.run_cli('status').stdout)['published_dispositions'], {})

    def test_existing_intake_pages_survive_real_cli_process_restart(self):
        source = self.root / 'source.sqlite'
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute('''CREATE TABLE todos (id TEXT PRIMARY KEY, message_id TEXT, state TEXT,
                kind TEXT, title TEXT, summary TEXT, accepted_by TEXT, updated_at TEXT)''')
            connection.executemany('INSERT INTO todos VALUES (?,?,?,?,?,?,?,?)', [
                (f'todo_{index:04}', f'msg_{index}', 'queued', 'bug', 'Fixture', 'Approved scope', 'operator', '2026-11-02')
                for index in range(205)])
        source.chmod(0o600)
        config = {'schema_version': 1, 'coordinators': [TARGET], 'intake': [{'run_id': 'run_1', 'path': str(source)}]}
        self.config.write_text(json.dumps(config))
        self.dispositions.write_text(json.dumps({'run_1': {'state': 'finished'}}))
        self.dispositions.chmod(0o600)
        before = source.read_bytes()
        for expected in (100, 200, 205, 205):
            result = self.run_cli('tick', ['--apply'])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['queue']['events'], expected)
        self.assertEqual(source.read_bytes(), before)
        sends = [call for call in self.calls() if call[:2] == ['terminal', 'send']]
        self.assertEqual(len(sends), 4)
        for call in sends:
            self.assertIn('Context only; this event grants no new execution or publication permission.',
                          call[call.index('--text') + 1])
        status = json.loads(self.run_cli('status').stdout)
        self.assertEqual(status['queue']['effects']['delivered'], 4)
        self.assertEqual(status['queue']['effects']['pending'], 201)
        self.assertEqual(status['dispositions']['run_1']['state'], 'finished')
        self.assertFalse(any(call[:2] in (['orchestration', 'worker-start'], ['orchestration', 'task-create'])
                             for call in self.calls()))

    def test_install_adds_cli_and_units_without_changing_running_services(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/install-superagent.sh')],
                                env={**os.environ, 'DESTDIR': str(self.root / 'install'), 'PERSON_HOME': '/home/fixture'},
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed = self.root / 'install/usr/local/bin/spectre-superagent'
        result = self.run_cli('status', executable=installed)
        self.assertEqual(result.returncode, 0, result.stderr)
        service = self.root / 'install/home/fixture/.config/systemd/user/spectre-superagent.service'
        self.assertTrue(service.is_file())
        self.assertNotIn('Restart=always', service.read_text())
        self.assertTrue((self.root / 'install/usr/local/lib/spectre-superagent/dsh-go-session.mjs').is_file())
        self.assertTrue((self.root / 'install/usr/local/lib/spectre-superagent/offload-command.py').is_file())
        self.assertEqual((self.root / 'install/usr/local/lib/spectre-superagent/offload-artifacts.py').read_bytes(),
                         (ROOT / 'scripts/offload-artifacts.py').read_bytes())
        self.assertTrue((self.root / 'install/usr/local/bin/spectre-offload-execution').is_file())
        self.assertTrue((self.root / 'install/usr/local/bin/harness-workers').is_file())
        self.assertTrue((self.root / 'install/usr/local/bin/spectre-dsh-execution').is_file())
        stage = self.root / 'install/usr/local/bin/dsh-superagent-stage-session'
        self.assertTrue(stage.is_file())
        base = self.root / 'dsh-home/profiles/headless'
        base.mkdir(parents=True)
        (base / 'cordis.patch.yml').write_text('- id: original-provider\n')
        (base / 'package.json').write_text('{}')
        result = subprocess.run([str(stage), '--home', str(base.parent.parent)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('spectre-worker-session-v3', result.stdout)
        self.assertTrue((self.root / 'install/usr/local/lib/spectre-superagent/dsh-worker-session/LICENSE').is_file())


if __name__ == '__main__':
    unittest.main()
