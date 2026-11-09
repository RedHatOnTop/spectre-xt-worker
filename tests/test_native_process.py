"""Native Efficient occupancy must follow its pinned process, not its UI title."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from worker_state.process import evidence_for_worker


class NativeProcessTest(unittest.TestCase):
    def test_pinned_efficient_pid_is_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / '42'
            proc.mkdir()
            (proc / 'cmdline').write_bytes(b'qodercli\0-m\0Efficient\0')
            (proc / 'environ').write_bytes(b'ORCA_TERMINAL_HANDLE=term_e\0')
            (proc / 'cwd').symlink_to(root)
            fields = ['S', '1'] + ['0'] * 22
            (proc / 'stat').write_text('42 (qodercli) ' + ' '.join(fields))
            (proc / 'cgroup').write_text('0::/user.slice\n')
            events = evidence_for_worker('native-fixture', {'cwd': tmp, 'terminal': 'term_e'},
                                         1800000000, proc_root=root)
            samples = [event['payload'] for event in events if event['kind'] == 'process.sample']
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]['pid'], 42)
            self.assertTrue(samples[0]['alive'])
