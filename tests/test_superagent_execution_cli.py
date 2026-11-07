"""Native execution CLI preserves visible wrapper flow and actual process receipts."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import execution
from superagent_queue import session_profile

SPEC = importlib.util.spec_from_file_location('spectre_dsh_execution', ROOT / 'scripts/spectre-dsh-execution.py')
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)
BINDING = {'run_id': 'run_1', 'task_id': 'task_1', 'dispatch_id': 'dispatch_1',
           'terminal_handle': 'term_1', 'pane_key': 'tab_1:leaf_1', 'worktree_id': 'repo::/work',
           'workspace_path': '/work', 'incarnation_id': 'pane_process_1',
           'route': 'clinepass', 'packet_sha256': 'a' * 64}


class ExecutionCliTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'private/executions'
        self.wrapper = Path(self.directory.name) / 'wrapper'
        self.wrapper.write_text('#!' + sys.executable + '\n'
            'import os,sys\nfrom pathlib import Path\n'
            'packet=Path(sys.argv[sys.argv.index("--file")+1])\n'
            'assert packet.read_text()=="Exact native preamble"\n'
            'assert packet.stat().st_mode & 0o777==0o600\n'
            'assert packet.parent==Path(os.environ["SPECTRE_PACKET_DIR"])\n'
            'packet.with_suffix(".exit").write_text("0\\n")\n')
        self.wrapper.chmod(0o755)
        self.packet = Path(self.directory.name) / 'task.txt'
        self.packet.write_text('Exact native Task')
        self.packet.chmod(0o600)
        self.args = SimpleNamespace(root=self.root, orca='/exact/orca-ide', task='task_1',
                                    dispatch='dispatch_1', packet=self.packet, route='clinepass')
        self.profile = patch.object(CLI.session_profile, 'verify', return_value={'profile': session_profile.PROFILE})
        self.profile.start()
        self.addCleanup(self.profile.stop)

    def test_actual_wrapper_process_exit_and_native_binding_are_durable(self):
        with patch.object(CLI, 'WRAPPER', self.wrapper), \
                patch.object(CLI.assignment, 'prepare', return_value=(BINDING, 'Exact native preamble', {})):
            result = CLI.execute(self.args)
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(execution.inspect(self.root, 'dispatch_1')['binding'], BINDING)
        self.assertEqual(result['session']['session_id'], CLI.session_execution.session_id(BINDING))
        self.assertEqual(result['session']['resume'], False)

    def test_go_explicit_home_does_not_use_a_shared_default_route(self):
        self.wrapper.write_text('#!' + sys.executable + '\n'
            'import os,sys\nassert sys.argv[sys.argv.index("--tier")+1]=="go"\n'
            'assert os.environ["SPECTRE_DSH_GO_HOME"]==' + repr(str(CLI.GO_HOME)) + '\n')
        args = SimpleNamespace(**{**vars(self.args), 'route': 'opencode-go'})
        with patch.object(CLI, 'WRAPPER', self.wrapper), \
                patch.object(CLI.assignment, 'prepare', return_value=({**BINDING, 'route': 'opencode-go'}, 'Go preamble', {})):
            result = CLI.execute(args)
        self.assertEqual(result['returncode'], 0)

    def test_inherited_worker_override_cannot_change_the_declared_route(self):
        self.wrapper.write_text('#!' + sys.executable + '\n'
            'import os\nassert os.environ["SPECTRE_DSH_HOME"]==' + repr(str(CLI.CLINE_HOME)) + '\n'
            'assert os.environ["SPECTRE_DSH_KEY"]==' + repr(str(CLI.CLINE_KEY)) + '\n'
            'assert os.environ["SPECTRE_DSH_BIN"]==' + repr(str(CLI.DSH_BIN)) + '\n'
            'assert os.environ["SPECTRE_DSH_PROFILE"]==' + repr(session_profile.PROFILE) + '\n'
            'assert os.environ["SPECTRE_DSH_SESSION_RESUME"]=="0"\n'
            'assert os.environ["SPECTRE_DSH_SESSION_ID"].startswith("session-native-")\n')
        overrides = {'SPECTRE_DSH_HOME': '/unknown-profile', 'SPECTRE_DSH_KEY': '/unknown-paid-key',
                     'SPECTRE_DSH_BIN': '/unknown-executable', 'SPECTRE_DSH_PROFILE': 'unknown-profile'}
        with patch.dict(os.environ, overrides), patch.object(CLI, 'WRAPPER', self.wrapper), \
                patch.object(CLI.assignment, 'prepare', return_value=(BINDING, 'Exact native preamble', {})):
            result = CLI.execute(self.args)
        self.assertEqual(result['returncode'], 0)

    def test_duplicate_execution_refuses_packet_overwrite_and_process_spawn(self):
        with patch.object(CLI, 'WRAPPER', self.wrapper), \
                patch.object(CLI.assignment, 'prepare', return_value=(BINDING, 'Exact native preamble', {})):
            CLI.execute(self.args)
            with self.assertRaises((ValueError, FileExistsError)), patch.object(execution.subprocess, 'Popen') as spawn:
                CLI.execute(self.args)
            spawn.assert_not_called()

    def test_native_validation_failure_does_not_write_packet_or_receipt(self):
        with patch.object(CLI.assignment, 'prepare', side_effect=ValueError('native owner changed')), \
                self.assertRaisesRegex(ValueError, 'native owner changed'):
            CLI.execute(self.args)
        self.assertFalse(self.root.parent.exists())

    def test_cli_parses_explicit_run_and_inspect_without_default_route(self):
        for words, action in ((['inspect', '--dispatch', 'dispatch_1'], 'inspect'),
                              (['run', '--task', 'task_1', '--dispatch', 'dispatch_1',
                                '--packet', '/private/task.txt', '--route', 'clinepass'], 'run')):
            with patch.object(sys, 'argv', ['spectre-dsh-execution', *words]):
                args = CLI.arguments()
            self.assertEqual(args.action, action)
            self.assertEqual(args.dispatch, 'dispatch_1')

    def test_cli_reports_actual_signal_exit_and_verbatim_validation_error(self):
        with patch.object(CLI, 'arguments', return_value=SimpleNamespace(**{**vars(self.args), 'action': 'run'})), \
                patch.object(CLI, 'execute', return_value={'returncode': -15}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(CLI.main(), 143)
            self.assertEqual(json.loads(output.getvalue())['returncode'], -15)
        with patch.object(CLI, 'arguments', return_value=SimpleNamespace(**{**vars(self.args), 'action': 'inspect'})), \
                patch.object(execution, 'inspect', side_effect=ValueError('exact native retry link unavailable')), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(CLI.main(), 2)
            self.assertEqual(output.getvalue(), 'spectre-dsh-execution: exact native retry link unavailable\n')

    def test_preinput_guard_rejects_absent_child_and_session_mismatch(self):
        from superagent_queue.settings import read_private
        with patch.object(CLI, 'WRAPPER', self.wrapper), \
                patch.object(CLI.assignment, 'prepare', return_value=(BINDING, 'Exact native preamble', {})):
            CLI.execute(self.args)
        intent = self.root / 'dispatch_1/intent.json'
        args = SimpleNamespace(intent=intent, orca='/exact/orca')
        with self.assertRaisesRegex(ValueError, 'live wrapper'):
            CLI.authorize(args)
        recorded = execution.inspect(self.root, 'dispatch_1')
        session = read_private(intent)['session']['session_id']
        with patch.object(execution, 'inspect', return_value={**recorded, 'observation': 'live'}), \
                patch.object(CLI.assignment, 'confirm'), \
                patch.object(CLI.assignment, 'current_dispatch', return_value=({}, 'Exact native preamble')), \
                patch.dict(os.environ, {'SPECTRE_DSH_SESSION_ID': session}):
            self.assertEqual(CLI.authorize(args)['authorized'], True)
        with patch.object(execution, 'inspect', return_value={**recorded, 'observation': 'live'}), \
                patch.object(CLI.assignment, 'confirm'), \
                patch.object(CLI.assignment, 'current_dispatch', return_value=({}, 'Exact native preamble')), \
                patch.dict(os.environ, {'SPECTRE_DSH_SESSION_ID': 'session-native-other'}), \
                self.assertRaisesRegex(ValueError, 'session does not match'):
            CLI.authorize(args)

    def test_authorize_action_returns_exact_live_receipt_through_cli(self):
        args = SimpleNamespace(action='authorize', intent=Path('/private/intent.json'))
        with patch.object(CLI, 'arguments', return_value=args), \
                patch.object(CLI, 'authorize', return_value={'authorized': True, 'session_id': 'session-native-task'}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(CLI.main(), 0)
            self.assertTrue(json.loads(output.getvalue())['authorized'])


if __name__ == '__main__':
    unittest.main()
