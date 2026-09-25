"""Process-backed hygiene and launcher regression tests."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import inventory, pins, astra
from control_plane.io import write_json
import importlib.util
spec = importlib.util.spec_from_file_location('reaper', ROOT / 'scripts/spectre-reaper.py')
reaper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reaper)


class ReaperTest(unittest.TestCase):
    def test_duplicate_and_untitled_require_observed_identity_and_grace(self):
        args = {'listen_ports': set(), 'pins': set(), 'flash_done_age': None}
        self.assertEqual(reaper.decide({'cmd': 'qodercli -m Efficient', 'duplicate': True,
                                       'age': 301}, **args), 'term')
        self.assertEqual(reaper.decide({'cmd': 'qodercli -m Efficient', 'duplicate': True,
                                       'age': 5}, **args), 'keep')
        self.assertEqual(reaper.decide({'cmd': 'bash', 'untitled': True, 'age': 301}, **args), 'term')
        self.assertEqual(reaper.decide({'cmd': 'dsh --profile headless task',
                                       'unpinned_flash_done_age': 301}, **args), 'term')

    def test_descendant_listener_protects_parent_tree(self):
        rows = [{'pid': 20, 'ppid': 1}, {'pid': 21, 'ppid': 20}]
        self.assertEqual(reaper.tree_listeners(rows, {21: {6768}})[20], {6768})

    def test_headless_command_in_pinned_shell_is_not_headless_process(self):
        self.assertEqual(reaper.decide({'cmd': 'bash -c "dsh --profile headless task"',
            'handle': 'term_flash'}, listen_ports=set(), pins={'term_flash'}, flash_done_age=301), 'keep')

    def test_no_listener_heavy_is_selected_but_astra_is_kept(self):
        args = {'listen_ports': set(), 'pins': set(), 'flash_done_age': None}
        self.assertEqual(reaper.decide({'cmd': 'java', 'rss_mib': 300}, **args), 'term')
        self.assertEqual(reaper.decide({'cmd': 'codex -m gpt-6-astra', 'rss_mib': 300}, **args), 'keep')

    def test_allowlisted_listener_kept_before_named_rules(self):
        self.assertEqual(reaper.decide({'cmd': 'java -jar paper.jar', 'listen': [9091]},
            listen_ports={9091}, pins=set(), flash_done_age=None), 'keep')

    def test_live_child_signal_checks_identity(self):
        child = subprocess.Popen(['sleep', '30'])
        try:
            observed = next(row for row in inventory.scan() if row['pid'] == child.pid)
            with self.assertRaises(ValueError):
                reaper.terminate({**observed, 'start': observed['start'] + 1})
            self.assertIsNone(child.poll())
            reaper.terminate(observed)
            self.assertEqual(child.wait(timeout=3), -15)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)

    def test_listener_parser(self):
        text = 'LISTEN 0 128 127.0.0.1:6768 0.0.0.0:* users:(("orca",pid=42,fd=7))\n'
        self.assertEqual(inventory.listeners(text), {42: {6768}})

    def test_only_this_uids_unattributed_listeners_count(self):
        text = ('LISTEN 0 128 0.0.0.0:22 0.0.0.0:* ino:1 sk:1 cgroup:/system.slice/ssh.service <->\n'
                'LISTEN 0 511 0.0.0.0:6768 0.0.0.0:* users:(("orca",pid=42,fd=7)) uid:1000 ino:2 sk:2 <->\n'
                'LISTEN 0 128 127.0.0.1:8080 0.0.0.0:* uid:1000 ino:3 sk:3 <->\n'
                'LISTEN 0 128 127.0.0.1:9000 0.0.0.0:* uid:1001 ino:4 sk:4 <->\n'
                'garbled\n\n')
        self.assertEqual(inventory.unattributed(text, 1000), ['127.0.0.1:8080', 'garbled'])
        self.assertEqual(inventory.unattributed(text, 0), ['0.0.0.0:22', 'garbled'])
        self.assertEqual(inventory.listeners(text), {42: {6768}})


class LiveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.packet_dir = root / 'packets'
        (self.packet_dir / 'tabs').mkdir(parents=True)
        self.record = self.packet_dir / 'tabs' / 'term_gone.json'
        self.record.write_text(json.dumps({'handle': 'term_gone', 'role': 'flash', 'kind': 'shell',
                                           'dispatch_id': None, 'created_at': 0}) + '\n')
        write_json(root / 'workers.json', {'workers': {'minecraft': {'cwd': '/work/mc'}}})
        self.args = argparse.Namespace(apply=False, workers_file=root / 'workers.json',
                                       state=root / 'reaper.json')

    def live(self, listener_uid, observe):
        row = f'LISTEN 0 128 127.0.0.1:8080 0.0.0.0:* uid:{listener_uid} ino:3 sk:3 <->\n'

        def fake_ss(argv, **kwargs):
            self.assertEqual(argv, ['ss', '-ltnpeH'])
            return subprocess.CompletedProcess(argv, 0, row, '')

        listing = {'ok': True, 'parsed': {'result': {'terminals': [], 'truncated': False}}}
        with patch.dict(os.environ, {'SPECTRE_PACKET_DIR': str(self.packet_dir)}), \
                patch.object(reaper.subprocess, 'run', side_effect=fake_ss), \
                patch.object(reaper, 'run', return_value=listing), \
                patch.object(reaper, 'observe', side_effect=observe):
            return reaper.live(self.args)

    def test_another_uids_unattributed_listener_does_not_block(self):
        out = self.live(os.getuid() + 1, lambda *args: ([], {}))
        self.assertTrue(out['ok'])
        self.assertNotIn('error', out)
        self.assertEqual([row['reason'] for row in out['tab_records']], ['tab_gone'])

    def test_own_unattributed_listener_refuses_processes_but_sweeps_tabs(self):
        def refuse_observe(*args):
            raise AssertionError('observe ran despite an unattributed listener')

        out = self.live(os.getuid(), refuse_observe)
        self.assertFalse(out['ok'])
        self.assertEqual(out['error'],
                         'listener ownership incomplete: 127.0.0.1:8080; refusing process reap')
        self.assertEqual(out['decisions'], [])
        self.assertEqual([(row['handle'], row['reason'], row['dry_run']) for row in out['tab_records']],
                         [('term_gone', 'tab_gone', True)])
        self.assertTrue(self.record.exists())


class PinTest(unittest.TestCase):
    def test_title_is_never_model_evidence(self):
        terminals = [{'handle': 'term_a', 'worktreePath': '/work/a', 'connected': True,
                      'writable': True, 'title': 'Qoder Efficient'}]
        self.assertFalse(pins.pick(terminals, [], '/work/a', 'efficient')['ok'])
        processes = [{'handle': 'term_a', 'cwd': '/work/a', 'model': 'efficient'}]
        self.assertEqual(pins.pick(terminals, processes, '/work/a', 'efficient')['handle'], 'term_a')

    def test_ambiguous_pins_preserved(self):
        entry = {'cwd': '/work/a', 'terminal': 'term_old'}
        updated, changes = pins.sync({'w': entry}, [], [])
        self.assertEqual(updated['w'], entry)
        self.assertFalse(changes[0]['ok'])


class AstraTest(unittest.TestCase):
    def test_existing_astra_never_creates_or_switches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('control_plane.astra.inventory.scan', return_value=[{'pid': 41, 'model': 'astra'}]):
                called = []
                out = astra.launch(root / 'workers', root / 'modes', root / 'provider.json',
                                   run=lambda cmd, **kw: called.append(cmd))
                self.assertFalse(out['ok'])
                self.assertEqual(called, [])

    def test_failed_switch_does_not_write_active_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / 'workers', {'workers': {'minecraft': {'cwd': '/work/mc'}}})
            write_json(root / 'provider.json', {'ok': True, 'id': 'anyrouter', 'checked_at': 100})
            with patch('control_plane.astra.inventory.scan', return_value=[]):
                out = astra.launch(root / 'workers', root, root / 'provider.json', now=100,
                                   run=lambda cmd, **kw: {'ok': False})
            self.assertEqual(out['error'], 'codex_mode_failed')
            self.assertFalse((root / 'active-provider').exists())


if __name__ == '__main__':
    unittest.main()

class LaunchFlowTest(unittest.TestCase):
    def test_success_creates_only_orca_terminal_and_preserves_other_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / 'workers', {'workers': {'minecraft': {'cwd': '/work/mc',
                'targets': {'flash': {'terminal': 'term_flash'}, 'efficient': {'terminal': 'old'}}}}})
            write_json(root / 'provider.json', {'ok': True, 'id': 'anyrouter', 'checked_at': 100})
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs.get('cwd')))
                if argv[1:3] == ['terminal', 'list']:
                    return {'ok': True, 'parsed': {'result': {'terminals': [{'handle': 'term_eff',
                        'worktreePath': '/work/mc', 'connected': True, 'writable': True}]}}}
                if argv[1:3] == ['worktree', 'ps']:
                    return {'ok': True, 'parsed': {'result': {'worktrees': [
                        {'worktreeId': 'wt-mc::/work/mc', 'path': '/work/mc'}]}}}
                if argv[1:3] == ['terminal', 'create']:
                    return {'ok': True, 'parsed': {'result': {'terminal': {
                        'handle': 'term_astra', 'worktreeId': 'wt-mc::/work/mc'}}}}
                return {'ok': True}
            processes = [{'pid': 7, 'model': 'efficient', 'handle': 'term_eff', 'cwd': '/work/mc'}]
            with patch('control_plane.astra.inventory.scan', return_value=processes):
                output = astra.launch(root / 'workers', root, root / 'provider.json', run=run, now=100)
            self.assertTrue(output['ok'])
            self.assertEqual(output['worktree_id'], 'wt-mc::/work/mc')
            self.assertEqual(calls[0][0], ['codex-mode', 'api', 'anyrouter', 'gpt-6-astra'])
            create, cwd = calls[-1]
            self.assertEqual(create[:6], ['orca-ide', 'terminal', 'create', '--worktree', 'active',
                                          '--title'])
            self.assertEqual(cwd, '/work/mc')
            self.assertEqual((root / 'active-provider').read_text(), 'anyrouter\n')
            updated = json.loads((root / 'workers').read_text())['workers']['minecraft']
            self.assertEqual(updated['planner']['terminal'], 'term_astra')
            self.assertEqual(updated['planner']['harness'], 'codex')
            self.assertEqual(updated['targets']['efficient']['terminal'], 'term_eff')
            self.assertEqual(updated['targets']['flash']['terminal'], 'term_flash')
            self.assertNotIn('model_provider=openai', ' '.join(create))

    def test_pin_sync_updates_all_efficient_aliases_and_planner(self):
        workers = {'minecraft': {'cwd': '/work/mc', 'terminal': 'old', 'planner': {'terminal': 'old_a'},
                                 'targets': {'efficient': {'terminal': 'old'}}}}
        terminals = [{'handle': h, 'worktreePath': '/work/mc', 'connected': True, 'writable': True}
                     for h in ('new_e', 'new_a')]
        procs = [{'handle': 'new_e', 'cwd': '/work/mc', 'model': 'efficient'},
                 {'handle': 'new_a', 'cwd': '/work/mc', 'model': 'astra'}]
        updated, changes = pins.sync(workers, terminals, procs)
        self.assertEqual(updated['minecraft']['targets']['efficient']['terminal'], 'new_e')
        self.assertEqual(updated['minecraft']['planner']['terminal'], 'new_a')
        self.assertEqual(workers['minecraft']['terminal'], 'old')

    def test_pin_sync_follows_the_planner_harness(self):
        terminals = [{'handle': h, 'worktreePath': '/work/mc', 'connected': True, 'writable': True}
                     for h in ('new_e', 'new_a', 'new_c')]
        procs = [{'handle': 'new_e', 'cwd': '/work/mc', 'model': 'efficient'},
                 {'handle': 'new_a', 'cwd': '/work/mc', 'model': 'astra'},
                 {'handle': 'new_c', 'cwd': '/work/mc', 'model': 'claude'}]
        workers = {'minecraft': {'cwd': '/work/mc',
                                 'planner': {'terminal': 'old', 'harness': 'claude'}}}
        updated, changes = pins.sync(workers, terminals, procs)
        self.assertEqual(updated['minecraft']['planner'], {'terminal': 'new_c', 'harness': 'claude'})
        self.assertEqual(changes[-1]['role'], 'claude')
        unknown = {'minecraft': {'cwd': '/work/mc',
                                 'planner': {'terminal': 'old', 'harness': 'gemini'}}}
        updated, changes = pins.sync(unknown, terminals, procs)
        self.assertEqual(updated['minecraft']['planner']['terminal'], 'old')
        self.assertFalse(changes[-1]['ok'])
        self.assertIn('unknown planner harness', changes[-1]['error'])

class FlashPinTest(unittest.TestCase):
    def test_flash_binding_requires_an_idle_shell_in_minecraft(self):
        workers = {'minecraft': {'cwd': '/work/mc', 'targets': {'flash': {'model': 'flash'}}}}
        term = {'handle': 'term_f', 'worktreePath': '/work/mc', 'connected': True, 'writable': True}
        procs = [{'handle': 'term_f', 'cwd': '/work/mc', 'model': None, 'comm': 'bash'}]
        updated = pins.bind_flash(workers, [term], procs, 'term_f')
        self.assertEqual(updated['minecraft']['targets']['flash']['terminal'], 'term_f')
        self.assertNotIn('terminal', workers['minecraft']['targets']['flash'])
        with self.assertRaises(ValueError):
            pins.bind_flash(workers, [term], [{**procs[0], 'model': 'efficient'}], 'term_f')
        with self.assertRaises(ValueError):
            pins.bind_flash(workers, [{**term, 'worktreePath': '/elsewhere'}], procs, 'term_f')
