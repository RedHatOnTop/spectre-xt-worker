"""Formal offload execution keeps exact native authority across capacity waits."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import execution, offload_receipt
from test_superagent_offload_receipt import BINDING
from test_superagent_offload_assignment import SPEC, CONFIG

spec = importlib.util.spec_from_file_location('spectre_offload_execution', ROOT / 'scripts/spectre-offload-execution.py')
CLI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CLI)


class OffloadExecutionCliTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / 'private/offloads'
        self.packet = self.base / 'packet.json'
        self.packet.write_text(json.dumps(SPEC))
        self.packet.chmod(0o600)
        self.preamble = self.base / 'preamble.txt'
        self.preamble.write_text('Exact current preamble')
        self.preamble.chmod(0o600)
        self.config = self.base / 'config.json'
        self.config.write_text(json.dumps(CONFIG))
        self.config.chmod(0o600)
        self.args = SimpleNamespace(root=self.root, orca='/exact/orca', task='task_1', dispatch='dispatch_1',
                                    packet=self.packet, preamble=self.preamble, config=self.config)
        self.wrapper = self.base / 'wrapper'
        self.wrapper.write_text('#!' + sys.executable + '\nimport os\n'
            'assert os.environ["SPECTRE_OFFLOAD_EXPECTED_STUDIO"]=="existing-studio"\n'
            'assert os.environ["SPECTRE_OFFLOAD_AUTHORITY_INTENT"].endswith("/intent.json")\n')
        self.wrapper.write_text(self.wrapper.read_text() + 'assert os.environ["SPECTRE_OFFLOAD_ORCA"]=="/exact/orca"\n')
        self.wrapper.chmod(0o755)
        self.binding = {**BINDING, 'packet_sha256': hashlib.sha256(self.packet.read_bytes()).hexdigest()}
        self.addCleanup(patch.stopall)
        patch.object(CLI.settings, 'config', return_value=CONFIG).start()
        self.verify_runtime = CLI.verify_runtime
        patch.object(CLI, 'verify_runtime', return_value=None).start()

    def test_exact_task_argv_profile_and_intent_are_preserved(self):
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper):
            result = CLI.execute(self.args)
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['command'], SPEC['command'])
        self.assertEqual(result['binding'], self.binding)
        self.assertEqual(result['remote_observation'], 'unverifiable')

    def test_wrapper_waits_for_the_durable_started_receipt_before_authority_check(self):
        self.wrapper.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\n'
            'assert sys.stdin.readline()=="native-offload-started\\n"\n'
            'intent=Path(os.environ["SPECTRE_OFFLOAD_AUTHORITY_INTENT"])\n'
            'started=json.loads(intent.with_name("started.json").read_text())\n'
            'assert started["child"]["pid"]==os.getpid()\n')
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper):
            result = CLI.execute(self.args)
        self.assertEqual(result['returncode'], 0)

    def test_started_receipt_failure_closes_handshake_without_running_remote_work(self):
        marker = self.base / 'wrapper-exited.txt'
        self.wrapper.write_text('#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\n'
            'signal=sys.stdin.readline()\n'
            f'Path({str(marker)!r}).write_text("executed" if signal else "refused")\n')
        persist = execution.persist

        def fail_started(directory, name, value):
            if name == 'started.json':
                raise OSError('started receipt unavailable')
            return persist(directory, name, value)

        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper), patch.object(execution, 'persist', side_effect=fail_started), \
                self.assertRaisesRegex(OSError, 'started receipt unavailable'):
            CLI.execute(self.args)
        import time
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(.01)
        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text(), 'refused')

    def test_native_validation_failure_leaves_no_job_intent_or_wrapper(self):
        with patch.object(CLI.offload_assignment, 'prepare', side_effect=ValueError('native capability changed')), \
                patch.object(offload_receipt.subprocess, 'Popen') as spawn, \
                self.assertRaisesRegex(ValueError, 'capability changed'):
            CLI.execute(self.args)
        spawn.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_exact_dispatch_has_one_stable_job_id_and_cannot_be_relaunched(self):
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper):
            first = CLI.execute(self.args)
            with self.assertRaisesRegex(ValueError, 'intent'), patch.object(offload_receipt.subprocess, 'Popen') as spawn:
                CLI.execute(self.args)
            spawn.assert_not_called()
        self.assertEqual(first['job_id'], CLI.job_id(self.binding))

    def test_inherited_provider_funding_and_guard_overrides_are_scrubbed(self):
        overrides = {'LIGHTNING_ENV_FILE': '/foreign-credentials', 'LIGHTNING_MACHINE': 'PAID_GPU',
                     'LIGHTNING_STUDIO': 'foreign-studio', 'LIGHTNING_API_KEY': 'fixture-do-not-use',
                     'SPECTRE_OFFLOAD_AUTHORITY_INTENT': '/copied-intent'}
        self.wrapper.write_text(self.wrapper.read_text() +
            'assert os.environ.get("LIGHTNING_MACHINE") is None\n'
            'assert os.environ.get("LIGHTNING_API_KEY") is None\n'
            'assert os.environ.get("LIGHTNING_STUDIO") is None\n'
            'assert os.environ["LIGHTNING_ENV_FILE"]!=' + repr(overrides['LIGHTNING_ENV_FILE']) + '\n')
        with patch.dict(os.environ, overrides), patch.object(CLI.offload_assignment, 'prepare',
                return_value=(self.binding, SPEC, 'existing-studio')), patch.object(CLI, 'WRAPPER', self.wrapper):
            self.assertEqual(CLI.execute(self.args)['returncode'], 0)

    def test_guard_requires_actual_live_wrapper_and_exact_original_packet(self):
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper):
            result = CLI.execute(self.args)
        args = SimpleNamespace(intent=self.root / result['job_id'] / 'intent.json', orca='/exact/orca')
        with self.assertRaisesRegex(ValueError, 'live wrapper'):
            CLI.authorize(args)
        observed = offload_receipt.inspect(self.root, result['job_id'])
        with patch.object(offload_receipt, 'inspect', return_value={**observed, 'observation': 'live'}), \
                patch.object(CLI.offload_assignment, 'confirm', return_value='existing-studio') as confirm:
            report = CLI.authorize(args)
        self.assertTrue(report['authorized'])
        self.assertEqual(report['studio'], 'existing-studio')
        self.assertEqual(confirm.call_args.args[1], self.binding)

    def test_guard_refuses_copied_or_changed_command_before_network(self):
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper):
            result = CLI.execute(self.args)
        args = SimpleNamespace(intent=self.root / result['job_id'] / 'intent.json', orca='/exact/orca')
        observed = offload_receipt.inspect(self.root, result['job_id'])
        with patch.object(offload_receipt, 'inspect', return_value={**observed, 'observation': 'live'}), \
                patch.object(CLI.offload_assignment, 'confirm', side_effect=ValueError('native binding changed')), \
                self.assertRaisesRegex(ValueError, 'binding changed'):
            CLI.authorize(args)

    def test_guard_refuses_revoked_config_or_changed_cli_during_capacity_wait(self):
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper):
            result = CLI.execute(self.args)
        args = SimpleNamespace(intent=self.root / result['job_id'] / 'intent.json', orca='/exact/orca')
        observed = offload_receipt.inspect(self.root, result['job_id'])
        for current in ({**CONFIG, 'offload': []}, {**CONFIG, 'coordinators': []}):
            with self.subTest(current=current), patch.object(offload_receipt, 'inspect',
                    return_value={**observed, 'observation': 'live'}), \
                    patch.object(CLI.settings, 'config', side_effect=[CONFIG, current]), \
                    patch.object(CLI.offload_assignment, 'confirm', return_value='existing-studio'), \
                    self.assertRaisesRegex(ValueError, 'configuration changed'):
                CLI.authorize(args)

    def test_unpinned_or_changed_runtime_never_passes_validation(self):
        path = self.base / 'pinned-helper.py'
        path.write_text('original reviewed runtime')
        with patch.object(CLI, 'RUNTIME_HASHES', {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}):
            self.verify_runtime()
            path.write_text('changed executable')
            with self.assertRaisesRegex(ValueError, 'reviewed bytes'):
                self.verify_runtime()
        with patch.object(CLI, 'RUNTIME_HASHES', {}), self.assertRaisesRegex(ValueError, 'not pinned'):
            self.verify_runtime()

    def test_runtime_pins_cover_the_executed_native_authority_modules(self):
        paths = {Path(path).name for path in CLI.RUNTIME_HASHES}
        self.assertTrue({'assignment.py', 'offload_assignment.py', 'offload_receipt.py', 'execution.py',
                         'settings.py', 'observe.py', 'offload-command.py', 'offload-artifacts.py',
                         'offload-capacity.py', 'offload_capacity.py', 'spectre-offload',
                         'native.py', 'disposition.py', 'reconcile.py', 'continuation.py'} <= paths)

    def test_exact_artifact_helper_and_private_job_destination_override_global_config(self):
        self.wrapper.write_text(self.wrapper.read_text() +
            'assert os.environ["SPECTRE_OFFLOAD_ARTIFACT_HELPER"].endswith("offload-artifacts.py")\n'
            'assert os.environ["SPECTRE_OFFLOAD_ARTIFACT_ROOT"].endswith("/offload-artifacts")\n')
        self.wrapper.write_text(self.wrapper.read_text() + 'assert os.environ["SPECTRE_OFFLOAD_CAPACITY_HELPER"].endswith("offload-capacity.py")\n')
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper):
            self.assertEqual(CLI.execute(self.args)['returncode'], 0)

    def test_wrapper_exit_before_handshake_retains_its_actual_exit_and_closes_the_pipe(self):
        from unittest.mock import MagicMock
        child = MagicMock()
        child.pid = os.getpid()
        child.stdin.flush.side_effect = BrokenPipeError('wrapper exited')
        child.stdin.close.side_effect = BrokenPipeError('wrapper pipe closed')
        child.wait.return_value = 7
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper), patch.object(offload_receipt.subprocess, 'Popen', return_value=child):
            result = CLI.execute(self.args)
        self.assertEqual(result['returncode'], 7)
        self.assertEqual(result['error'], 'wrapper exited before durable start signal')
        self.assertEqual(offload_receipt.inspect(self.root, result['job_id'])['returncode'], 7)

    def test_cli_keeps_signal_returncode_and_literal_validation_error(self):
        with patch.object(CLI, 'arguments', return_value=SimpleNamespace(action='run')), \
                patch.object(CLI, 'execute', return_value={'returncode': -15}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(CLI.main(), 143)
            self.assertEqual(json.loads(output.getvalue())['returncode'], -15)
        with patch.object(CLI, 'arguments', return_value=SimpleNamespace(action='authorize')), \
                patch.object(CLI, 'authorize', side_effect=ValueError('exact native owner unavailable')), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(CLI.main(), 2)
            self.assertEqual(output.getvalue(), 'spectre-offload-execution: exact native owner unavailable\n')

    def test_explicit_cli_run_authorize_and_inspect_require_exact_identifiers(self):
        for words, action in ((['inspect', '--job', 'job_1'], 'inspect'),
                (['authorize', '--intent', '/private/intent.json'], 'authorize'),
                (['run', '--task', 'task_1', '--dispatch', 'dispatch_1', '--packet', '/private/packet.json',
                  '--preamble', '/private/preamble.txt'], 'run')):
            with self.subTest(words=words), patch.object(sys, 'argv', ['spectre-offload-execution', *words]):
                self.assertEqual(CLI.arguments().action, action)
        with patch.object(CLI, 'arguments', return_value=SimpleNamespace(action='inspect', root=self.root, job='job_1')), \
                patch.object(offload_receipt, 'inspect', return_value={'returncode': 7}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CLI.main(), 7)

    def test_guard_rejects_another_intent_path_or_modified_record(self):
        with patch.object(CLI.offload_assignment, 'prepare', return_value=(self.binding, SPEC, 'existing-studio')), \
                patch.object(CLI, 'WRAPPER', self.wrapper):
            result = CLI.execute(self.args)
        intent = self.root / result['job_id'] / 'intent.json'
        data = json.loads(intent.read_text())
        with patch.object(CLI.settings, 'read_private', return_value=data), self.assertRaisesRegex(ValueError, 'original job intent'):
            CLI.authorize(SimpleNamespace(intent=self.root / 'other/intent.json', orca='/exact/orca'))
        observed = offload_receipt.inspect(self.root, result['job_id'])
        for authority in ({}, {**data['authority'], 'orca': '/different/orca'}):
            intent.write_text(json.dumps({**data, 'authority': authority}))
            with patch.object(offload_receipt, 'inspect', return_value={**observed, 'observation': 'live'}), \
                    patch.object(CLI.offload_assignment, 'confirm', return_value='existing-studio'), self.assertRaises(ValueError):
                CLI.authorize(SimpleNamespace(intent=intent, orca='/exact/orca'))


if __name__ == '__main__':
    unittest.main()
