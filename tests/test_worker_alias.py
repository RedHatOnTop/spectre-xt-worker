"""Spare-agent alias selection stays off agents that are open or installed."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from orca_runtime.worker_alias import plan


class WorkerAliasTest(unittest.TestCase):
    def setUp(self):
        self.bin_dir = Path('/home/person/.local/bin')
        self.script_dir = Path('/home/person/.local/lib/spectre-superagent/scripts')

    def bindings(self, **kwargs):
        values = {
            'live_agents': ['codex', 'grok', 'dsh'],
            'overrides': {},
            'disabled': [],
            'default_agent': None,
            'installed_names': set(),
            'configured_agents': set(),
        }
        values.update(kwargs)
        return plan(self.bin_dir, self.script_dir, **values)

    def test_open_codex_grok_and_dsh_leave_two_absent_carriers(self):
        bindings = self.bindings()
        self.assertEqual([item['agent'] for item in bindings], ['jcode', 'freebuff'])
        self.assertEqual([item['alias'] for item in bindings], ['dsh-worker', 'dsh-worker-go'])
        self.assertTrue(bindings[0]['script'].endswith('/scripts/jcode.py'))
        self.assertEqual(bindings[0]['command'], '/home/person/.local/bin/dsh-worker')

    def test_live_or_installed_or_customized_carrier_is_skipped(self):
        bindings = self.bindings(live_agents=['codex', 'jcode'], installed_names={'freebuff'},
                                 overrides={'crush': 'crush --verbose'})
        self.assertEqual([item['agent'] for item in bindings], ['codebuff', 'goose'])

    def test_protected_agent_cannot_carry_a_worker_name(self):
        with self.assertRaisesRegex(ValueError, 'dsh-worker'):
            self.bindings(overrides={'codex': '/home/person/.local/bin/dsh-worker'})

    def test_saved_environment_is_not_a_carrier(self):
        bindings = self.bindings(configured_agents={'jcode'})
        self.assertEqual([item['agent'] for item in bindings], ['freebuff', 'crush'])

    def test_existing_exact_binding_is_kept(self):
        bindings = self.bindings(overrides={
            'freebuff': '/home/person/.local/bin/dsh-worker',
            'crush': '/home/person/.local/bin/dsh-worker-go',
        })
        self.assertEqual([(item['alias'], item['agent']) for item in bindings],
                         [('dsh-worker', 'freebuff'), ('dsh-worker-go', 'crush')])


if __name__ == '__main__':
    unittest.main()
