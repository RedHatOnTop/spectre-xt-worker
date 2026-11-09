"""Installed harness control bounds real subprocess native fixture launches."""
import json
from pathlib import Path
import subprocess
import sys

from superagent_flow_support import InstalledFlow


class HarnessWorkerFlowTest(InstalledFlow):
    def setUp(self):
        super().setUp()
        self.policy = self.root / 'policy.json'
        self.write(self.policy, {'schema_version': 1, 'max_workers': 2, 'agents': ['dsh-worker', 'dsh-worker-go']})
        self.change(workspace_path=str(self.root), workspace_pages=[{
            'worktrees': [{'worktreeId': 'repo::/work', 'path': str(self.root)},
                          {'worktreeId': 'repo::/worker_1', 'path': '/worker_1'},
                          {'worktreeId': 'repo::/worker_2', 'path': '/worker_2'},
                          {'worktreeId': 'repo::/worker_3', 'path': '/worker_3'}]}, {}],
                    tasks=[{'id': f'pool_task_{index}', 'run_id': 'run_1', 'status': 'ready'} for index in (1, 2, 3)])
        self.runner = self.root / 'pool-cli.py'
        self.runner.write_text('import importlib.machinery,importlib.util,sys\nfrom pathlib import Path\n'
            f'root=Path({str(self.installed)!r})\n'
            'sys.path.insert(0,str(root/"usr/local/lib/spectre-superagent"))\n'
            'path=root/"usr/local/bin/harness-workers"\n'
            'spec=importlib.util.spec_from_file_location("pool",path,loader=importlib.machinery.SourceFileLoader("pool",str(path)))\n'
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
            'assert root in Path(module.worker_pool.__file__).parents\nraise SystemExit(module.main())\n')

    def start(self, index, apply=True):
        request = {'launch_id': f'pool_launch_{index}', 'run_id': 'run_1', 'task_id': f'pool_task_{index}',
                   'agent': 'dsh-worker' if index % 2 else 'dsh-worker-go', 'worktree_id': f'repo::/worker_{index}'}
        words = ['--apply'] if apply else []
        return subprocess.run([sys.executable, '-I', '-B', str(self.runner), '--policy', str(self.policy),
            '--state', str(self.root / 'pool-state'), '--orca', str(self.fixture), 'start', *words],
            input=json.dumps(request), env={**self.environment(owner=True), 'ORCA_WORKTREE_ID': 'repo::/work'}, cwd=self.root,
            capture_output=True, text=True, timeout=20, check=False)

    def test_two_designated_harnesses_launch_and_third_waits_without_another_model(self):
        first, second, third = self.start(1), self.start(2), self.start(3)
        for result in (first, second, third):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(first.stdout)['state'], 'ready')
        self.assertEqual(json.loads(second.stdout)['state'], 'ready')
        self.assertEqual(json.loads(third.stdout)['state'], 'capacity_wait')
        replay = self.start(1)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertTrue(json.loads(replay.stdout)['replayed'])
        self.assertEqual(self.state()['worker_starts'], 2)
        starts = [call for call in self.state()['calls'] if call[:2] == ['orchestration', 'worker-start']]
        self.assertEqual([call[call.index('--agent') + 1] for call in starts], ['dsh-worker', 'dsh-worker-go'])

    def test_ambiguous_first_native_launch_blocks_new_worker_and_retains_result(self):
        self.change(faults={'worker_start_lost_output': True})
        first = self.start(1)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertIn('native worker launch accepted but response lost', json.loads(first.stdout)['stderr'])
        self.change(faults={})
        replay, second = self.start(1), self.start(2)
        self.assertEqual(replay.returncode, 1, replay.stdout + replay.stderr)
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        self.assertEqual(json.loads(second.stdout)['state'], 'reconciliation_required')
        self.assertEqual(self.state()['worker_starts'], 1)

    def test_same_native_launch_request_reconciliation_does_not_create_another_worker(self):
        self.change(faults={'worker_start_lost_output': True})
        first = self.start(1)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.change(faults={})
        request_id = next(iter(self.state()['mutations']))
        args = [sys.executable, '-I', '-B', str(self.runner), '--policy', str(self.policy),
                '--state', str(self.root / 'pool-state'), '--orca', str(self.fixture),
                'reconcile', '--run', 'run_1', '--launch', 'pool_launch_1', '--request', request_id]
        original = (self.root / 'pool-state/run_1/pool_launch_1/receipt.json').read_bytes()
        result = subprocess.run(args, env={**self.environment(owner=True), 'ORCA_WORKTREE_ID': 'repo::/work'},
            cwd=self.root, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'], 'ready')
        self.assertEqual((self.root / 'pool-state/run_1/pool_launch_1/receipt.json').read_bytes(), original)
        self.assertEqual(self.start(1).returncode, 0)
        self.assertEqual(self.start(2).returncode, 0)
        self.assertEqual(self.state()['worker_starts'], 2)

    def test_dry_run_reads_current_coordinator_without_creating_native_worker(self):
        result = self.start(1, apply=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['native_start'])
        self.assertNotIn('worker_starts', self.state())
