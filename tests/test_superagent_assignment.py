"""Only a current native Dispatch in its exact pane can become an execution."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import assignment

CALLER = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'tab_1:leaf_1',
          'ORCA_WORKTREE_ID': 'repo::/work'}
DISPATCH = {'id': 'dispatch_1', 'task_id': 'task_1', 'run_id': 'run_1', 'status': 'dispatched',
            'contract_version': 1, 'assignee_handle': 'term_1', 'assignee_pane_key': 'tab_1:leaf_1',
            'capability_revoked_at': None, 'process_incarnation': 'repo::/work@@pty_1:process_1'}
TERMINAL = {'handle': 'term_1', 'incarnationId': 'process_1', 'worktreeId': 'repo::/work',
            'worktreePath': '/work', 'tabId': 'tab_1', 'leafId': 'leaf_1',
            'ptyId': 'repo::/work@@pty_1', 'connected': True, 'writable': True}
PREAMBLE = 'Exact native worker instructions.\n=== TASK ===\nRead approved fixture only.'


class NativeAssignmentTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.packet = Path(self.directory.name) / 'task.txt'
        self.packet.write_text('Read approved fixture only.')
        self.packet.chmod(0o600)

    def query(self, cli, args):
        if args[:2] == ['orchestration', 'dispatch-show']:
            return {'dispatch': DISPATCH, 'preamble': PREAMBLE}
        if args[:2] == ['orchestration', 'run-show']:
            return {'run': {'id': 'run_1', 'coordinator_handle': 'coordinator_1', 'legacy': 0}}
        if args[:2] == ['terminal', 'show']:
            return {'terminal': TERMINAL}
        if args[:2] == ['worktree', 'ps']:
            return {'worktrees': [{'worktreeId': 'repo::/work', 'path': '/work'}]}
        raise AssertionError(args)

    def prepare(self, caller=CALLER):
        return assignment.prepare('/exact/orca-ide', 'task_1', 'dispatch_1', self.packet,
                                  'clinepass', caller, cwd='/work')

    def test_exact_preamble_and_native_identities_are_retained(self):
        with patch('superagent_queue.assignment.observe.query', side_effect=self.query):
            binding, prompt, dispatch = self.prepare()
        self.assertEqual(binding['dispatch_id'], 'dispatch_1')
        self.assertEqual(binding['run_id'], 'run_1')
        self.assertEqual(binding['incarnation_id'], 'process_1')
        self.assertEqual(binding['packet_sha256'], hashlib.sha256(self.packet.read_bytes()).hexdigest())
        self.assertEqual(prompt, PREAMBLE)
        self.assertEqual(dispatch, DISPATCH)
        self.assertNotIn('capability', binding)

    def test_worker_preamble_uses_the_run_coordinator_not_the_worker_as_coordinator(self):
        with patch('superagent_queue.assignment.observe.query', side_effect=self.query) as query:
            self.prepare()
        preamble_calls = [call.args[1] for call in query.call_args_list if '--preamble' in call.args[1]]
        self.assertEqual(len(preamble_calls), 1)
        args = preamble_calls[0]
        self.assertEqual(args[args.index('--from') + 1], 'coordinator_1')

    def test_read_only_inspection_requires_the_exact_registered_runtime_root(self):
        def query(cli, args):
            if args[:2] == ['worktree', 'ps']:
                return {'worktrees': []}
            return self.query(cli, args)
        with patch('superagent_queue.assignment.observe.query', side_effect=query), self.assertRaises(ValueError):
            self.prepare()

    def test_truncated_inventory_expands_before_confirming_registered_execution_pane(self):
        def query(cli, args):
            if args[:2] == ['worktree', 'ps']:
                if '--limit' not in args:
                    return {'worktrees': [], 'truncated': True, 'totalCount': 101}
                self.assertEqual(args, ['worktree', 'ps', '--limit', '101'])
                return {'worktrees': [{'worktreeId': 'repo::/work', 'path': '/work'}], 'truncated': False}
            return self.query(cli, args)
        with patch('superagent_queue.assignment.observe.query', side_effect=query):
            binding, _, _ = self.prepare()
        self.assertEqual(binding['worktree_id'], 'repo::/work')

    def test_partial_inventory_cannot_confirm_a_worker_even_if_its_row_is_present(self):
        for value in ({'truncated': True, 'totalCount': None}, {'truncated': True, 'totalCount': True},
                      {'truncated': True, 'totalCount': 10001}, {'truncated': True, 'totalCount': -1}):
            def query(cli, args):
                if args[:2] == ['worktree', 'ps']:
                    return {**value, 'worktrees': [{'worktreeId': 'repo::/work', 'path': '/work'}]}
                return self.query(cli, args)
            with self.subTest(value=value), patch('superagent_queue.assignment.observe.query', side_effect=query), \
                    self.assertRaisesRegex(ValueError, 'incomplete'):
                self.prepare()

    def test_still_truncated_expansion_never_confirms_execution(self):
        def query(cli, args):
            if args[:2] == ['worktree', 'ps']:
                return {'worktrees': [{'worktreeId': 'repo::/work', 'path': '/work'}],
                        'truncated': True, 'totalCount': 101}
            return self.query(cli, args)
        with patch('superagent_queue.assignment.observe.query', side_effect=query), \
                self.assertRaisesRegex(ValueError, 'remains incomplete'):
            self.prepare()

    def test_expanded_inventory_failure_preserves_native_error_without_fallback(self):
        def query(cli, args):
            if args[:2] == ['worktree', 'ps']:
                if '--limit' in args:
                    raise RuntimeError('native expanded workspace host unavailable verbatim')
                return {'worktrees': [], 'truncated': True, 'totalCount': 101}
            return self.query(cli, args)
        with patch('superagent_queue.assignment.observe.query', side_effect=query), \
                self.assertRaisesRegex(RuntimeError, 'native expanded workspace host unavailable verbatim'):
            self.prepare()

    def test_source_packet_cannot_expand_the_native_task(self):
        self.packet.write_text('Read approved fixture and change production.')
        with patch('superagent_queue.assignment.observe.query', side_effect=self.query), \
                self.assertRaisesRegex(ValueError, 'packet does not match the native Task'):
            self.prepare()

    def test_shell_without_native_pane_environment_never_queries_or_spawns(self):
        with patch('superagent_queue.assignment.observe.query') as query, self.assertRaises(ValueError):
            self.prepare({})
        query.assert_not_called()

    def test_other_dispatch_settled_attempt_and_missing_preamble_are_refused(self):
        for change in ({'id': 'other_dispatch'}, {'task_id': 'other_task'}, {'status': 'completed'},
                       {'assignee_handle': 'other_terminal'}, {'assignee_pane_key': 'other_pane'},
                       {'capability_revoked_at': '2026-11-02'}, {'contract_version': 0}):
            def query(cli, args):
                result = self.query(cli, args)
                return {**result, 'dispatch': {**DISPATCH, **change}} if 'dispatch' in result else result
            with self.subTest(change=change), patch('superagent_queue.assignment.observe.query', side_effect=query), \
                    self.assertRaises(ValueError):
                self.prepare()

    def test_folder_terminal_uses_the_registered_workspace_path(self):
        folder = {**TERMINAL, 'worktreePath': '', 'paneKey': None}

        def query(cli, args):
            return {'terminal': folder} if args[:2] == ['terminal', 'show'] else self.query(cli, args)

        with patch('superagent_queue.assignment.observe.query', side_effect=query):
            binding, _, _ = self.prepare()
        self.assertEqual(binding['workspace_path'], '/work')
        missing = {key: value for key, value in folder.items() if key != 'worktreePath'}

        def without_path(cli, args):
            return {'terminal': missing} if args[:2] == ['terminal', 'show'] else self.query(cli, args)

        with patch('superagent_queue.assignment.observe.query', side_effect=without_path), \
                self.assertRaisesRegex(ValueError, 'workspace'):
            self.prepare()

    def test_other_cwd_disconnected_or_unregistered_terminal_is_refused(self):
        for terminal in ({**TERMINAL, 'connected': False}, {**TERMINAL, 'worktreePath': '/other'},
                         {**TERMINAL, 'worktreeId': 'other_repo::/work'}, {**TERMINAL, 'incarnationId': ''}):
            def query(cli, args):
                return {'terminal': terminal} if args[:2] == ['terminal', 'show'] else self.query(cli, args)
            with self.subTest(terminal=terminal), patch('superagent_queue.assignment.observe.query', side_effect=query), \
                    self.assertRaises(ValueError):
                self.prepare()

    def test_replaced_process_in_same_pane_cannot_execute_the_old_dispatch(self):
        def query(cli, args):
            result = self.query(cli, args)
            return {**result, 'terminal': {**TERMINAL, 'incarnationId': 'replacement'}} if 'terminal' in result else result
        with patch('superagent_queue.assignment.observe.query', side_effect=query), self.assertRaisesRegex(ValueError, 'incarnation'):
            self.prepare()

    def test_packet_symlink_or_nonprivate_file_is_refused(self):
        self.packet.chmod(0o644)
        with patch('superagent_queue.assignment.observe.query', side_effect=self.query), self.assertRaises(ValueError):
            self.prepare()

    def test_preinput_confirmation_reads_current_native_scope_and_exact_task_digest(self):
        with patch('superagent_queue.assignment.observe.query', side_effect=self.query):
            binding, _, _ = self.prepare()
            self.assertEqual(assignment.confirm('/exact/orca-ide', binding, self.packet, CALLER, cwd='/work')['id'], 'dispatch_1')
        for change in ({'run_id': 'run_other'}, {'packet_sha256': 'b' * 64}, {'incarnation_id': 'replaced'}):
            with patch('superagent_queue.assignment.observe.query', side_effect=self.query), \
                    self.assertRaises(ValueError):
                assignment.confirm('/exact/orca-ide', {**binding, **change}, self.packet, CALLER, cwd='/work')

    def test_native_capability_preamble_is_checked_against_dispatch_hash_not_preview(self):
        prompt = PREAMBLE.replace('Exact native worker instructions.', 'Exact native worker instructions. --dispatch-capability exact_token')
        preview = prompt.replace(' --dispatch-capability exact_token', '')
        dispatch = {**DISPATCH, 'capability_hash': hashlib.sha256(b'exact_token').hexdigest()}
        self.assertEqual(assignment.received_preamble(prompt, preview, dispatch), prompt)
        for value in (prompt.replace('exact_token', 'other_token'), prompt + 'different task',
                      prompt.replace(' --dispatch-capability exact_token', '')):
            with self.assertRaises(ValueError):
                assignment.received_preamble(value, preview, dispatch)


if __name__ == '__main__':
    unittest.main()
