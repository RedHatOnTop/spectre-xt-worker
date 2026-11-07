"""A persistent native worker records input before execution and never overlaps attempts."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import worker

PROMPT = ('You are working inside Orca, a multi-agent IDE. You are a dispatched worker.\n'
          'Your task ID is: task_1\n\n=== CLI COMMANDS ===\n'
          'orca orchestration send --from term_1 --type worker_done '
          '--task-id task_1 --dispatch-id ctx_1 --outcome succeeded\n'
          '\n=== TASK ===\nRead approved fixture only.\n')
CALLER = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'tab_1:leaf_1', 'ORCA_WORKTREE_ID': 'repo::/work'}


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'executions'
        self.controller = worker.Worker(self.root, '/exact/orca-ide', 'clinepass', CALLER,
                                        output=io.StringIO())

    def test_input_is_private_and_immutable_before_native_check_or_spawn(self):
        with patch.object(worker.assignment, 'prepare', side_effect=ValueError('native owner changed')):
            self.controller.submit(PROMPT)
        records = list(self.controller.spool.glob('*.json'))
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)
        self.assertIn('native owner changed', self.controller.output.getvalue())
        self.assertIsNone(self.controller.child)

    def test_duplicate_preamble_never_spawns_second_attempt(self):
        with patch.object(worker.assignment, 'prepare', return_value=({}, PROMPT, {})), \
                patch.object(worker.subprocess, 'Popen') as spawn:
            spawn.return_value.pid = 123
            self.controller.submit(PROMPT)
            self.controller.submit(PROMPT)
            self.assertEqual(spawn.call_count, 1)

    def test_input_during_work_stays_recorded_without_concurrent_spawn(self):
        with patch.object(worker.assignment, 'prepare', return_value=({}, PROMPT, {})), \
                patch.object(worker.subprocess, 'Popen') as spawn:
            spawn.return_value.pid = 123
            self.controller.submit(PROMPT)
            self.controller.submit(PROMPT.replace('task_1', 'task_2').replace('ctx_1', 'ctx_2'))
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(len(list(self.controller.spool.glob('*.json'))), 2)
            self.assertEqual(len(self.controller.pending), 1)
            self.assertEqual(self.controller.active, 'ctx_1')

    def test_exact_current_preamble_mismatch_and_invalid_input_are_not_run(self):
        with patch.object(worker.assignment, 'prepare', return_value=({}, PROMPT + 'changed', {})), \
                patch.object(worker.subprocess, 'Popen') as spawn:
            self.controller.submit(PROMPT)
            spawn.assert_not_called()
        self.assertIn('current native preamble', self.controller.output.getvalue())
        with self.assertRaises(ValueError):
            self.controller.submit('echo hello')

    def test_actual_child_exit_records_terminal_observation_then_drains_pending(self):
        script = Path(self.temp.name) / 'executor'
        script.write_text('#!' + sys.executable + '\nraise SystemExit(7)\n')
        script.chmod(0o755)
        with patch.object(worker.assignment, 'prepare', return_value=({}, PROMPT, {})), \
                patch.object(worker, 'EXECUTOR', script):
            self.controller.submit(PROMPT)
            self.controller.child.wait(timeout=5)
            self.controller.settle()
        self.assertIsNone(self.controller.child)
        self.assertEqual(self.controller.active, None)
        self.assertIn('"returncode": 7', self.controller.output.getvalue())
        self.assertEqual(len(list(self.controller.spool.glob('*.exit.json'))), 1)

    def test_foreign_shell_without_registered_environment_fails_before_spool(self):
        with self.assertRaises(ValueError):
            worker.Worker(self.root, '/exact/orca-ide', 'clinepass', {}, output=io.StringIO())

    def test_ready_and_running_titles_come_from_actual_reader_and_child_transitions(self):
        self.controller.report({'state': 'ready'})
        self.assertIn('\x1b]0;DSH Worker ready\x07', self.controller.output.getvalue())
        with patch.object(worker.assignment, 'prepare', return_value=({}, PROMPT, {})), \
                patch.object(worker.subprocess, 'Popen') as spawn:
            spawn.return_value.pid = 123
            self.controller.submit(PROMPT)
        self.assertIn('\x1b]0;DSH Worker working\x07', self.controller.output.getvalue())
        self.assertEqual(self.controller.active, 'ctx_1')


if __name__ == '__main__':
    unittest.main()
