"""Real PTY worker launch consumes whole packets and keeps active children on input loss."""
import importlib.util
import contextlib
import io
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('worker_cli', ROOT / 'scripts/spectre-dsh-worker.py')
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class WorkerCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'runtime.py'
        self.script.write_text('import sys\nsys.path.insert(0, ' + repr(str(ROOT / 'scripts')) + ')\n'
            'import importlib.util\nspec=importlib.util.spec_from_file_location("cli", ' + repr(str(ROOT / 'scripts/spectre-dsh-worker.py')) + ')\n'
            'cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)\n'
            'raise SystemExit(cli.main())\n')
        self.orca = self.root / 'orca'
        self.orca.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
            'terminal={"handle":"term_1","worktreeId":"repo_1","worktreePath":os.getcwd(),'
            '"paneKey":"pane_1","connected":True,"writable":True,"incarnationId":"process_1",'
            '"agentIdentity":"jcode"}\n'
            'value={"terminal":terminal} if sys.argv[1:3]==["terminal","show"] else '
            '{"worktrees":[{"worktreeId":"repo_1","path":os.getcwd()}],"truncated":False}\n'
            'print(json.dumps({"ok":True,"result":value}))\n')
        self.orca.chmod(0o755)

    def test_actual_pty_ready_partial_frame_then_eof_does_not_spawn_or_hide_error(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        child = subprocess.Popen([sys.executable, str(self.script), '--orca', str(self.orca), '--route', 'clinepass',
                                  '--root', str(self.root / 'executions')], stdin=slave, stdout=slave, stderr=slave,
                                 env={**os.environ, 'ORCA_TERMINAL_HANDLE': 'term_1',
                                      'ORCA_PANE_KEY': 'pane_1', 'ORCA_WORKTREE_ID': 'repo_1'})
        os.close(slave)
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.terminate() if child.poll() is None else None)
        output = b''
        deadline = time.monotonic() + 5
        while b'"state": "ready"' not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
        self.assertIn(b'"state": "ready"', output)
        os.write(master, b'\x1b[200~incomplete\x00')
        child.wait(timeout=5)
        self.assertEqual(child.returncode, 2)
        self.assertFalse((self.root / 'executions').exists())

    def test_eof_with_truncated_utf8_is_an_explicit_input_failure(self):
        controller = CLI.worker.Worker(self.root / 'executions', '/exact/orca', 'clinepass',
            {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1', 'ORCA_WORKTREE_ID': 'repo_1'},
            output=io.StringIO())
        read, write = os.pipe()
        os.write(write, b'\xe2')
        os.close(write)
        with os.fdopen(read, 'rb') as source, patch.object(CLI.sys, 'stdin', source), \
                patch.object(CLI.signal, 'signal'):
            with self.assertRaises(UnicodeError):
                CLI.serve(controller)

    def test_pipe_eof_is_clean_and_all_stop_signals_are_registered(self):
        controller = CLI.worker.Worker(self.root / 'executions', '/exact/orca', 'opencode-go',
            {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1', 'ORCA_WORKTREE_ID': 'repo_1'},
            output=io.StringIO())
        read, write = os.pipe()
        os.close(write)
        with os.fdopen(read, 'rb') as source, patch.object(CLI.sys, 'stdin', source), \
                patch.object(CLI.signal, 'signal') as register:
            CLI.serve(controller)
        self.assertEqual({call.args[0] for call in register.call_args_list},
                         {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
        self.assertIn('"state": "ready"', controller.output.getvalue())

    def test_actual_tty_shutdown_waits_for_child_and_restores_terminal(self):
        controller = CLI.worker.Worker(self.root / 'executions', '/exact/orca', 'clinepass',
            {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1', 'ORCA_WORKTREE_ID': 'repo_1'},
            output=io.StringIO())
        from dataclasses import replace
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(0.05)'])
        self.addCleanup(child.wait)
        controller.state = replace(controller.state, child=child, active='ctx_1')
        master, slave = pty.openpty()
        original = CLI.termios.tcgetattr(slave)
        self.addCleanup(os.close, master)
        handlers = {}
        def register(number, handler):
            handlers[number] = handler
        def select_ready(selector, timeout):
            handlers[signal.SIGHUP](signal.SIGHUP, None)
            return []
        with os.fdopen(slave, 'rb') as source, patch.object(CLI.sys, 'stdin', source), \
                patch.object(CLI.signal, 'signal', side_effect=register), \
                patch.object(CLI.selectors.DefaultSelector, 'select', select_ready), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            CLI.serve(controller)
            self.assertEqual(CLI.termios.tcgetattr(source.fileno()), original)
        self.assertEqual(child.returncode, 0)
        self.assertIn('\x1b[?2004h', output.getvalue())
        self.assertIn('\x1b[?2004l', output.getvalue())

    def test_main_reports_foreign_environment_error_and_holds_private_lock_on_clean_exit(self):
        args = ['worker', '--orca', str(self.orca), '--route', 'clinepass', '--root', str(self.root / 'executions')]
        with patch.object(CLI.sys, 'argv', args), patch.dict(os.environ, {}, clear=True), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(CLI.main(), 2)
        self.assertIn('registered Orca pane', errors.getvalue())
        env = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1', 'ORCA_WORKTREE_ID': 'repo_1'}
        with patch.object(CLI.sys, 'argv', args), patch.dict(os.environ, env), \
                patch.object(CLI, 'serve') as serve:
            self.assertEqual(CLI.main(), 0)
        serve.assert_called_once()
        lock = self.root / 'worker-input/term_1/process.lock'
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_shutdown_rejects_readable_input_instead_of_starting_another_execution(self):
        controller = CLI.worker.Worker(self.root / 'executions', '/exact/orca', 'clinepass',
            {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1', 'ORCA_WORKTREE_ID': 'repo_1'},
            output=io.StringIO())
        read, write = os.pipe()
        os.write(write, b'\x1b[200~ignored\x1b[201~\r')
        os.close(write)
        handlers = {}
        def register(number, handler):
            handlers[number] = handler
        def select_ready(selector, timeout):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return [(None, None)]
        with os.fdopen(read, 'rb') as source, patch.object(CLI.sys, 'stdin', source), \
                patch.object(CLI.signal, 'signal', side_effect=register), \
                patch.object(CLI.selectors.DefaultSelector, 'select', select_ready), \
                patch.object(controller, 'submit') as submit:
            CLI.serve(controller)
        submit.assert_not_called()


    def test_signal_requests_graceful_wait_without_signalling_active_child(self):
        self.script.write_text(self.script.read_text().replace('raise SystemExit(cli.main())',
            'from superagent_queue import worker\n'
            'import subprocess\n'
            'def prepare(controller):\n'
            ' child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(1)"])\n'
            ' from dataclasses import replace\n'
            ' controller.state=replace(controller.state,child=child,active="ctx_1")\n'
            ' return child\n'
            'controller=worker.Worker(' + repr(str(self.root / 'executions')) + ',"/exact/orca", "clinepass", '
            '{"ORCA_TERMINAL_HANDLE":"term_1","ORCA_PANE_KEY":"pane_1","ORCA_WORKTREE_ID":"repo_1"}, sys.stdout)\n'
            'child=prepare(controller)\n'
            'cli.serve(controller)\n'
            'sys.stdout.write("CHILD_EXIT="+str(child.returncode)+"\\n")'))
        child = subprocess.Popen([sys.executable, str(self.script)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        line = child.stdout.readline()
        self.assertIn(b'"state": "ready"', line)
        child.terminate()
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertIn(b'CHILD_EXIT=0', stdout)
        self.assertIn(b'"state": "shutdown_requested"', stdout)

    def test_incomplete_input_while_child_active_preserves_child_before_failing(self):
        self.script.write_text(self.script.read_text().replace('raise SystemExit(cli.main())',
            'from superagent_queue import worker\n'
            'from dataclasses import replace\nimport subprocess\n'
            'controller=worker.Worker(' + repr(str(self.root / 'executions')) + ',"/exact/orca", "clinepass", '
            '{"ORCA_TERMINAL_HANDLE":"term_1","ORCA_PANE_KEY":"pane_1","ORCA_WORKTREE_ID":"repo_1"}, sys.stdout)\n'
            'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(0.4)"])\n'
            'controller.state=replace(controller.state,child=child,active="ctx_1")\n'
            'try: cli.serve(controller)\n'
            'except ValueError as exc:\n'
            ' sys.stdout.write("ERROR="+str(exc)+"\\nCHILD_EXIT="+str(child.poll())+"\\n")\n'))
        child = subprocess.Popen([sys.executable, str(self.script)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        output, error = child.communicate(b'\x1b[200~incomplete', timeout=5)
        self.assertEqual(child.returncode, 0, error)
        self.assertIn(b'CHILD_EXIT=0', output)
        self.assertIn(b'incomplete frame', output)


if __name__ == '__main__':
    unittest.main()
