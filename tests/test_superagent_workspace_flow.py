"""Installed execution preflight expands bounded native inventory in a real process."""
import json
import subprocess
import sys

from superagent_flow_support import InstalledFlow


class WorkspaceFlowTest(InstalledFlow):
    def inspect_pane(self):
        source = ('import json,sys\nfrom pathlib import Path\n'
            f'root=Path({str(self.installed)!r})\n'
            'sys.path.insert(0,str(root/"usr/local/lib/spectre-superagent"))\n'
            'from superagent_queue import assignment\n'
            'assert root in Path(assignment.__file__).parents\n'
            f'print(json.dumps(assignment.current_terminal({str(self.fixture)!r},'
            '{"ORCA_TERMINAL_HANDLE":"term_1","ORCA_PANE_KEY":"pane_1","ORCA_WORKTREE_ID":"repo::/work"},"/work")))\n')
        return subprocess.run([sys.executable, '-I', '-B', '-c', source], env=self.environment(),
                              text=True, capture_output=True, check=False, timeout=10)

    def test_registered_pane_outside_first_page_is_confirmed_without_new_terminal(self):
        pages = [{'worktrees': [], 'truncated': True, 'totalCount': 101},
                 {'worktrees': [{'worktreeId': 'repo::/work', 'path': '/work'}], 'truncated': False}]
        self.change(workspace_pages=pages)
        result = self.inspect_pane()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['handle'], 'term_1')
        calls = self.state()['calls']
        self.assertEqual(calls, [['terminal', 'show', '--terminal', 'term_1', '--json'],
                                ['worktree', 'ps', '--json'], ['worktree', 'ps', '--limit', '101', '--json']])
        self.assertEqual(self.state()['submissions'], 0)

    def test_incomplete_native_expansion_refuses_even_matching_registered_metadata(self):
        page = {'worktrees': [{'worktreeId': 'repo::/work', 'path': '/work'}],
                'truncated': True, 'totalCount': 101}
        self.change(workspace_pages=[page, page])
        result = self.inspect_pane()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Orca workspace inventory remains incomplete after expansion', result.stderr)
        self.assertEqual(self.state()['submissions'], 0)
