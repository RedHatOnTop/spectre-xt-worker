"""Retirement is reversible and cannot signal an agent or erase its journal."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from orca_runtime import retire, watch


class ReceiptTest(unittest.TestCase):
    def test_timestamp_churn_does_not_bypass_alert_spacing(self):
        with tempfile.TemporaryDirectory() as d, patch('orca_runtime.watch.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as send:
            f = Path(d) / 'receipt.json'
            first = {'issues': [{'code': 'agent_not_proven_live', 'dispatch_id': 'd1', 'liveness': {'verdict': 'unverifiable', 'observedAt': 1000}}], 'status': 'attention_required'}
            second = {**first, 'issues': [{**first['issues'][0], 'liveness': {'verdict': 'unverifiable', 'observedAt': 1001}}]}
            watch.notify(first, f, 1000)
            self.assertEqual(watch.notify(second, f, 1001), 'throttled')
            self.assertEqual(send.call_count, 1)

    def test_config_rejects_nonabsolute_workspaces(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'config.json'
            f.write_text('{"schema_version":1,"required_workspaces":["relative"]}')
            with self.assertRaises(ValueError):
                watch.config(f)

    def test_config_rejects_wrong_schema(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'config.json'
            f.write_text('{}')
            with self.assertRaises(ValueError):
                watch.config(f)

    def test_config_reads_valid_paths(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'config.json'
            f.write_text('{"schema_version":1,"required_workspaces":[]}')
            self.assertEqual(watch.config(f)['required_workspaces'], [])

    def test_missing_worker_alert_is_throttled_but_not_cleared(self):
        with tempfile.TemporaryDirectory() as d, patch('orca_runtime.watch.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as send:
            f = Path(d) / 'receipt.json'
            report = {'issues': [{'code': 'required_workspace_unassigned'}], 'status': 'attention_required'}
            self.assertEqual(watch.notify(report, f, 1000), 'sent')
            self.assertEqual(watch.notify(report, f, 1001), 'throttled')
            self.assertEqual(send.call_count, 1)
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            self.assertEqual(watch.notify(report, f, 2000), 'sent')

    def test_failed_notification_is_not_acknowledged(self):
        with tempfile.TemporaryDirectory() as d, patch('orca_runtime.watch.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'notify failed verbatim')):
            f = Path(d) / 'receipt.json'
            with self.assertRaisesRegex(RuntimeError, 'notify failed verbatim'):
                watch.notify({'issues': [{'code': 'missing'}], 'status': 'attention_required'}, f, 1000)
            self.assertFalse(f.exists())

    def test_clear_records_idle_without_a_completion_claim(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'receipt.json'
            self.assertEqual(watch.notify({'issues': [], 'status': 'idle'}, f), 'clear')
            self.assertEqual(json.loads(f.read_text())['status'], 'idle')

    def test_corrupted_receipt_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'receipt.json'
            f.write_text('[]')
            with self.assertRaises(ValueError):
                watch.read_receipt(f)


class RetirementTest(unittest.TestCase):
    def test_plan_includes_legacy_authorities_not_cost_or_game_guards(self):
        self.assertIn('spectre-worker-state.service', retire.LEGACY_UNITS)
        self.assertIn('slack-bridge.service', retire.LEGACY_UNITS)
        for name in ['qoder-efficient-guard.timer', 'fullmoon-prod-watch.service', 'orca-serve.service', 'spectre-thermal-guard.timer', 'fullmoon-discord-intake.timer']:
            self.assertNotIn(name, retire.LEGACY_UNITS)

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            units = home / '.config/systemd/user'
            units.mkdir(parents=True)
            old = units / 'spectre-continuity.timer'
            old.write_text('old unit')
            result = retire.plan(home)
            self.assertIn(str(old), result['files'])
            self.assertEqual(old.read_text(), 'old unit')
            self.assertFalse((home / '.local/state/remote-agent/retired-runtime').exists())

    def test_apply_masks_units_and_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as d, patch('orca_runtime.retire.systemctl') as control:
            home = Path(d)
            units = home / '.config/systemd/user'
            units.mkdir(parents=True)
            f = units / 'spectre-continuity.timer'
            f.write_text('original')
            result = retire.apply(home, stamp='test')
            self.assertEqual(f.readlink(), Path('/dev/null'))
            backup = Path(result['backup'])
            self.assertEqual((backup / 'units/spectre-continuity.timer').read_text(), 'original')
            self.assertFalse(any('kill' in str(c) for c in control.call_args_list))

    def test_apply_preserves_journal_and_cost_sentinel(self):
        with tempfile.TemporaryDirectory() as d, patch('orca_runtime.retire.systemctl'):
            home = Path(d)
            state = home / '.local/state/remote-agent'
            state.mkdir(parents=True)
            for name in ['worker-state.sqlite', 'qoder-efficient-billed']:
                (state / name).write_text('preserve')
            retire.apply(home, stamp='test')
            self.assertEqual((state / 'worker-state.sqlite').read_text(), 'preserve')
            self.assertEqual((state / 'qoder-efficient-billed').read_text(), 'preserve')

    def test_reapplying_does_not_replace_an_existing_archive(self):
        with tempfile.TemporaryDirectory() as d, patch('orca_runtime.retire.systemctl'):
            home = Path(d)
            result = retire.apply(home, stamp='same')
            with self.assertRaises(FileExistsError):
                retire.apply(home, stamp='same')
            self.assertTrue(Path(result['backup']).exists())

    def test_systemctl_failure_aborts_before_archiving(self):
        with tempfile.TemporaryDirectory() as d, patch('orca_runtime.retire.systemctl', side_effect=RuntimeError('stop refused verbatim')):
            with self.assertRaisesRegex(RuntimeError, 'stop refused verbatim'):
                retire.apply(Path(d), stamp='test')


if __name__ == '__main__':
    unittest.main()
