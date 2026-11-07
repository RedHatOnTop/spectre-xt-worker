"""Drive an isolated installed CLI closure with real subprocesses and SQLite."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue.store import Journal

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Continue only existing authorized fixture work.'}
CALLER = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}


class InstalledFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install_directory = tempfile.TemporaryDirectory()
        cls.installed = Path(cls.install_directory.name)
        result = subprocess.run(['bash', str(ROOT / 'scripts/install-superagent.sh')],
            env={**os.environ, 'DESTDIR': str(cls.installed), 'PERSON_HOME': '/home/fixture'},
            text=True, capture_output=True, check=False, timeout=20)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.install_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config, self.dispositions = self.root / 'config.json', self.root / 'dispositions.json'
        self.journal = self.root / 'private/journal.sqlite3'
        self.state_path = self.root / 'native-state.json'
        self.write(self.config, {'schema_version': 1, 'coordinators': [TARGET]})
        self.write(self.dispositions, {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}})
        self.write(self.state_path, {'run': {'id': 'run_1', 'coordinator_handle': 'term_1', 'legacy': 0},
            'agent_state': 'done', 'since': 1000, 'prompt': 'Previous fixture turn', 'response': 'Previous answer',
            'incarnation': 'process_1', 'workers': [], 'messages': [], 'delivery': None,
            'dispatches': [], 'gates': [{'id': 'gate_1', 'status': 'pending'}],
            'calls': [], 'submissions': 0, 'requests': {}, 'mutations': {}, 'faults': {}})
        self.fixture = ROOT / 'tests/fixtures/superagent-native.py'
        self.runner = self.root / 'installed-cli.py'
        self.runner.write_text('import importlib.machinery,importlib.util,sys\nfrom pathlib import Path\n'
            f'root=Path({str(self.installed)!r})\n'
            'sys.path.insert(0,str(root/"usr/local/lib/spectre-superagent"))\n'
            'path=root/"usr/local/bin/spectre-superagent"\n'
            'spec=importlib.util.spec_from_file_location("installed",path,loader=importlib.machinery.SourceFileLoader("installed",str(path)))\n'
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
            'assert root in Path(module.driver.__file__).parents\nraise SystemExit(module.main())\n')

    def write(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def state(self):
        return json.loads(self.state_path.read_text())

    def change(self, **values):
        self.write(self.state_path, {**self.state(), **values})

    def run_cli(self, action, *extra, value=None, owner=False):
        result = subprocess.run(self.command(action, *extra), input=json.dumps(value) if value is not None else None,
            env=self.environment(owner), text=True, capture_output=True, check=False, timeout=20)
        return result

    def command(self, action, *extra):
        return [sys.executable, '-I', '-B', str(self.runner), '--config', str(self.config),
            '--dispositions', str(self.dispositions), '--journal', str(self.journal), '--orca', str(self.fixture),
            action, *extra]

    def environment(self, owner=False):
        return {**os.environ, 'NATIVE_FIXTURE_STATE': str(self.state_path), **(CALLER if owner else {})}

    def report(self, action, *extra, value=None, owner=False, expected=0):
        result = self.run_cli(action, *extra, value=value, owner=owner)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def tick(self, expected=0):
        return self.report('tick', '--apply', expected=expected)

    def native(self, *args):
        result = subprocess.run([str(self.fixture), *args, '--json'],
            env={**os.environ, 'NATIVE_FIXTURE_STATE': str(self.state_path)},
            text=True, capture_output=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)['result']

    def question(self, message_id='question_1'):
        return {'id': message_id, 'type': 'question', 'subject': 'Fixture question',
                'body': 'Is this READ_ONLY_FIXTURE?', 'payload': None, 'sender_handle': 'worker_1'}

    def end_turn(self):
        self.change(agent_state='done', response='Completed fixture response')

    def review(self, checked, rows):
        acked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1',
                            '--ack', checked['deliveryId'])
        value = {'check_request_id': checked['mutation']['requestId'],
                 'ack_request_id': acked['mutation']['requestId'], 'reviews': rows}
        return self.report('mail-reviewed', '--run', 'run_1', value=value, owner=True)

    def sql(self, statement):
        with Journal(self.journal) as journal:
            return [dict(row) for row in journal.connection.execute(statement)]
