import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

from superagent_queue import free_tier


def private(path, body='x'):
    path.write_text(body)
    path.chmod(0o600)
    return path


class KindTests(unittest.TestCase):
    def test_reads_the_declared_kind_from_the_packet_header(self):
        self.assertEqual(free_tier.kind_of('Kind: mechanical\n\nDo the thing'), 'mechanical')
        self.assertEqual(free_tier.kind_of('kind: Implement'), 'implement')

    def test_absent_or_unknown_kind_is_not_a_free_candidate(self):
        self.assertIsNone(free_tier.kind_of('Do the thing'))
        self.assertIsNone(free_tier.kind_of('Kind: whatever\nDo the thing'))
        self.assertIsNone(free_tier.kind_of(''))

    def test_marker_after_the_header_region_is_ignored(self):
        text = '\n'.join(f'line {index}' for index in range(12)) + '\nKind: mechanical'
        self.assertIsNone(free_tier.kind_of(text))


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.key = self.root / 'client_key'

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_profile_is_unprovisioned(self):
        result = free_tier.readiness(self.home, self.key)
        self.assertEqual(result, {**result, 'ok': False, 'reason': 'free_unprovisioned'})

    def test_public_profile_is_not_a_safe_free_route(self):
        profile = private(self.home / 'settings.yaml')
        profile.chmod(0o644)
        private(self.key)
        result = free_tier.readiness(self.home, self.key)
        self.assertEqual(result['reason'], 'free_unprovisioned')

    def test_missing_key_is_visible(self):
        private(self.home / 'settings.yaml')
        result = free_tier.readiness(self.home, self.key)
        self.assertEqual(result['reason'], 'free_key_missing')

    def test_private_profile_and_key_are_ready(self):
        private(self.home / 'settings.yaml')
        private(self.key)
        self.assertEqual(free_tier.readiness(self.home, self.key)['ok'], True)


class DecisionTests(unittest.TestCase):
    READY = {'ok': True, 'reason': 'ready'}
    ABSENT = {'ok': False, 'reason': 'free_unprovisioned'}

    def test_unknown_kind_stays_paid(self):
        self.assertEqual(free_tier.decide(None, self.READY)['tier'], 'paid')
        self.assertEqual(free_tier.decide(None, self.READY)['reason'], 'default_unknown_kind')

    def test_judgment_work_never_rides_on_free(self):
        for kind in ('review', 'blocker'):
            decision = free_tier.decide(kind, self.READY)
            self.assertEqual(decision['tier'], 'paid')
            self.assertEqual(decision['reason'], f'reliable_{kind}')

    def test_eligible_work_goes_free_first(self):
        for kind in ('implement', 'mechanical'):
            decision = free_tier.decide(kind, self.READY)
            self.assertEqual(decision, {'kind': kind, 'tier': 'free', 'reason': 'free_first'})

    def test_unprovisioned_free_route_falls_back_with_a_reason(self):
        decision = free_tier.decide('mechanical', self.ABSENT)
        self.assertEqual(decision['tier'], 'paid')
        self.assertEqual(decision['reason'], 'free_unprovisioned')


class LedgerTests(unittest.TestCase):
    def test_decision_is_appended_to_a_private_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = free_tier.record({'kind': 'mechanical', 'tier': 'free', 'reason': 'free_first'},
                                    root=directory, now=100.0)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            row = json.loads(path.read_text())
            self.assertEqual(row['tier'], 'free')
            self.assertEqual(row['at'], 100.0)
            free_tier.record({'kind': 'review', 'tier': 'paid', 'reason': 'reliable_review'},
                             root=directory, now=101.0)
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_ledger_failure_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'ledger'
            target.write_text('not a directory')
            target.chmod(0o600)
            self.assertIsNone(free_tier.record({'tier': 'paid'}, root=target))


if __name__ == '__main__':
    unittest.main()
