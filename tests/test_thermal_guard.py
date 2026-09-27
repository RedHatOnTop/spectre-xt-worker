"""Thermal guard: compile-farm classification is argv-based, and the ceiling acts.

The classification tests are the important ones: a packet that merely mentions
`gradlew` (grep, a repo path, a shell heredoc) must never be signalled, while the
Gradle wrapper, its JVM children and a project test runner must be.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import thermal


def load_guard():
    spec = importlib.util.spec_from_file_location('spectre_thermal_guard',
                                                  ROOT / 'scripts/spectre-thermal-guard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_guard()


def proc(pid, argv, **fields):
    return {'pid': pid, 'start': 100, 'ticks': 1000, 'comm': Path(argv[0]).name,
            'argv': argv, 'cmd': ' '.join(argv), 'process_state': 'R', **fields}


class ClassifyTest(unittest.TestCase):
    def test_gradle_wrapper_through_a_shell(self):
        self.assertEqual(thermal.classify(['/bin/sh', './gradlew', 'cleanTest', 'test']), 'gradle')

    def test_gradle_inside_bash_dash_c(self):
        self.assertEqual(thermal.classify(['bash', '-c', 'sh gradlew integrationTest']), 'gradle')

    def test_gradle_daemon_jvm(self):
        self.assertEqual(thermal.classify([
            '/usr/lib/jvm/java-17/bin/java', '-Xmx2g',
            'org.gradle.launcher.daemon.bootstrap.GradleDaemon']), 'jvm-build')

    def test_gradle_worker_jvm(self):
        self.assertEqual(thermal.classify(['java', 'worker.org.gradle.process.internal.worker.GradleWorkerMain']),
                         'jvm-build')

    def test_grep_for_gradlew_is_not_a_build(self):
        self.assertIsNone(thermal.classify(['grep', '-rn', 'gradlew', '/home/person/orca/workspaces/x']))

    def test_shell_heredoc_mentioning_gradle_is_not_a_build(self):
        self.assertIsNone(thermal.classify(['bash', '-c', 'echo gradlew > note.txt']))

    def test_cargo_and_rustc(self):
        self.assertEqual(thermal.classify(['cargo', 'build', '--release']), 'cargo')
        self.assertEqual(thermal.classify(['/root/.rustup/toolchains/x/bin/rustc', 'src/lib.rs']), 'rustc')

    def test_compilers(self):
        self.assertEqual(thermal.classify(['gcc', '-c', 'a.c']), 'cc')
        self.assertEqual(thermal.classify(['cc1plus', '-quiet', 'a.cc']), 'cxx')

    def test_container_build_only(self):
        self.assertEqual(thermal.classify(['podman', 'build', '-t', 'x', '.']), 'podman')
        self.assertIsNone(thermal.classify(['podman', 'run', '-it', 'x', 'bash']))

    def test_python_test_module_only(self):
        self.assertEqual(thermal.classify(['python3', '-m', 'pytest', '-q']), 'pytest')
        self.assertIsNone(thermal.classify(['python3', '-m', 'unittest', 'discover', '-s', 'tests']))
        self.assertIsNone(thermal.classify(['python3', 'scripts/doctor.sh']))

    def test_node_package_manager_and_bundler(self):
        self.assertEqual(thermal.classify(['node', '/usr/lib/node_modules/npm/bin/npm-cli.js', 'ci']),
                         'node-build')
        self.assertEqual(thermal.classify(['npx', 'vite', 'build']), 'vite')

    def test_launcher_depth_is_bounded(self):
        argv = ['bash', '-c', 'bash -c "bash -c \'bash -c \\"cargo build\\"\'"']
        self.assertIsInstance(thermal.classify(argv), (str, type(None)))
        self.assertIsNone(thermal.classify(['bash', '-c']))

    def test_yes_is_not_build_class(self):
        self.assertIsNone(thermal.classify(['yes']))


class ProtectedTest(unittest.TestCase):
    def test_workspace_path_does_not_protect_a_build(self):
        row = proc(11, ['/bin/sh', './gradlew', '-p', '/home/person/orca/workspaces/casino'])
        self.assertFalse(thermal.protected(row))
        self.assertEqual([b['pid'] for b in thermal.build_workloads([row])], [11])

    def test_core_services_are_protected(self):
        for argv in (['/usr/sbin/sshd', '-D'], ['/usr/local/bin/orca-ide', 'serve'],
                     ['python3', '/usr/local/bin/spectre-state', 'health'],
                     ['/usr/bin/tailscaled', '--state=/var/lib/tailscale/tailscaled.state']):
            self.assertTrue(thermal.protected(proc(1, argv)), argv)

    def test_agent_harness_is_protected_from_the_crit_fallback_only(self):
        row = proc(7, ['/home/person/.local/share/deepseek-harness-venv/bin/python3',
                       '/usr/local/bin/dsh', '--profile', 'tui'])
        self.assertTrue(thermal.protected(row, thermal.AGENT_PROTECTED))
        self.assertEqual(thermal.crit_targets([row], {7: 99.0}), [])


class AllowlistTest(unittest.TestCase):
    def test_allowlisted_family_is_not_blocked(self):
        rows = [proc(1, ['cargo', 'build'])]
        self.assertEqual(thermal.build_workloads(rows, frozenset({'cargo'})), [])

    def test_allowlisted_family_is_still_eligible_at_crit(self):
        rows = [proc(1, ['cargo', 'build'])]
        self.assertEqual([t['pid'] for t in thermal.crit_targets(rows, {1: 90.0}, allow=frozenset({'cargo'}))], [1])

    def test_read_allow_ignores_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'allow'
            path.write_text('# note\npytest\n\n  gradle  \n')
            self.assertEqual(thermal.read_allow(path), frozenset({'pytest', 'gradle'}))
            self.assertEqual(thermal.read_allow(Path(tmp) / 'missing'), frozenset())


class IntensiveTest(unittest.TestCase):
    """Rule 2: intensive non-agent work is offloaded, never agent tooling."""

    def test_agent_tooling_is_never_selected(self):
        rows = []
        for index, argv in enumerate((['/usr/local/bin/dsh', '--profile', 'tui'],
                                      ['codex', 'exec', 'x'], ['claude', '-p', 'x'],
                                      ['qodercli', '--model', 'efficient'],
                                      ['python3', '/usr/local/bin/spectre-state', 'health'],
                                      ['node', '/usr/local/bin/spectre-slack-bridge'],
                                      ['obscura', 'fetch', 'https://example.com']), start=1):
            rows.append(proc(index, argv, rss_mib=3072))
        cpu = {int(row['pid']): 400.0 for row in rows}
        streaks = {int(row['pid']): 99 for row in rows}
        self.assertEqual(thermal.intensive_workloads(rows, cpu, streaks), [])

    def test_generic_python_and_node_hogs_are_selected(self):
        """Interpreters are not blanket-protected: a data crunch must be offloaded."""
        rows = [proc(1, ['python3', '-c', 'x=bytearray(2*1024**3)'], rss_mib=2048),
                proc(2, ['node', 'server.js'], rss_mib=2048)]
        out = thermal.intensive_workloads(rows, {1: 5.0, 2: 5.0}, {})
        self.assertEqual([r['pid'] for r in out], [1, 2])
        self.assertEqual({r['reason'] for r in out}, {'intensive_workload:rss'})

    def test_build_class_process_is_owned_by_the_build_rule(self):
        row = proc(1, ['cargo', 'build', '--release'], rss_mib=4096)
        self.assertEqual(thermal.intensive_workloads([row], {1: 400.0}, {1: 9}), [])
        self.assertEqual([b['family'] for b in thermal.build_workloads([row])], ['cargo'])
        allowed = thermal.intensive_workloads([row], {1: 400.0}, {1: 9}, frozenset({'cargo'}))
        self.assertEqual([r['pid'] for r in allowed], [1])

    def test_rss_ceiling_fires_on_the_first_sample(self):
        row = proc(1, ['ffmpeg', '-i', 'clip.mp4'], rss_mib=thermal.INTENSIVE_RSS_MIB)
        rows = thermal.intensive_workloads([row], {1: 0.0}, {})
        self.assertEqual([r['reason'] for r in rows], ['intensive_workload:rss'])
        self.assertEqual(rows[0]['rss_mib'], float(thermal.INTENSIVE_RSS_MIB))
        under = proc(2, ['ffmpeg'], rss_mib=thermal.INTENSIVE_RSS_MIB - 1)
        self.assertEqual(thermal.intensive_workloads([under], {2: 0.0}, {}), [])

    def test_cpu_ceiling_needs_a_consecutive_streak(self):
        row = proc(1, ['ffmpeg', '-i', 'clip.mp4'])
        cpu = {1: thermal.INTENSIVE_CPU_PCT}
        self.assertEqual(thermal.intensive_workloads([row], cpu, {1: thermal.INTENSIVE_STREAK - 1}), [])
        rows = thermal.intensive_workloads([row], cpu, {1: thermal.INTENSIVE_STREAK})
        self.assertEqual([r['reason'] for r in rows], ['intensive_workload:cpu'])
        self.assertEqual(rows[0]['cpu_pct'], thermal.INTENSIVE_CPU_PCT)

    def test_next_streaks_increments_resets_and_drops(self):
        cpu = {1: thermal.INTENSIVE_CPU_PCT, 2: thermal.INTENSIVE_CPU_PCT - 1}
        self.assertEqual(thermal.next_streaks({1: 2, 2: 5, 3: 4}, cpu), {1: 3})
        # A pid that is gone (absent from cpu) is dropped like one below threshold.
        self.assertEqual(thermal.next_streaks({'9': 3}, {}), {})
        self.assertEqual(thermal.next_streaks({}, cpu), {1: 1})

    def test_zombies_are_skipped_and_rows_rank_by_cpu(self):
        rows = [proc(1, ['ffmpeg'], rss_mib=1600),
                proc(2, ['ffmpeg'], rss_mib=2000, process_state='Z')]
        self.assertEqual([r['pid'] for r in thermal.intensive_workloads(rows, {1: 10.0, 2: 10.0}, {})], [1])
        ranked = thermal.intensive_workloads([proc(3, ['ffmpeg'], rss_mib=1600),
                                              proc(4, ['ffmpeg'], rss_mib=1600)],
                                             {3: 10.0, 4: 80.0}, {})
        self.assertEqual([r['pid'] for r in ranked], [4, 3])


class SensorTest(unittest.TestCase):
    def test_coretemp_package_is_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'hwmon'
            hwmon = root / 'hwmon3'
            hwmon.mkdir(parents=True)
            (hwmon / 'name').write_text('coretemp\n')
            (hwmon / 'temp1_label').write_text('Core 0\n')
            (hwmon / 'temp1_input').write_text('48000\n')
            (hwmon / 'temp2_label').write_text('Package id 0\n')
            (hwmon / 'temp2_input').write_text('52000\n')
            self.assertEqual(thermal.read_temp_c(hwmon_root=root, thermal_root=Path(tmp) / 'none'), 52.0)

    def test_thermal_zone_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            hwmon = Path(tmp) / 'hwmon'
            hwmon.mkdir()
            zones = Path(tmp) / 'thermal'
            zone = zones / 'thermal_zone3'
            zone.mkdir(parents=True)
            (zone / 'type').write_text('x86_pkg_temp\n')
            (zone / 'temp').write_text('61000\n')
            self.assertEqual(thermal.read_temp_c(hwmon_root=hwmon, thermal_root=zones), 61.0)

    def test_missing_sensors_return_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(thermal.read_temp_c(hwmon_root=Path(tmp) / 'a', thermal_root=Path(tmp) / 'b'))


class LevelTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(thermal.level_for(50.0), 'ok')
        self.assertEqual(thermal.level_for(thermal.WARN_C), 'warn')
        self.assertEqual(thermal.level_for(thermal.CRIT_C), 'crit')
        self.assertEqual(thermal.level_for(None), 'unknown')

    def test_streak_requires_consecutive_samples(self):
        self.assertFalse(thermal.escalated(['ok', 'crit'], 'crit'))
        self.assertTrue(thermal.escalated(['crit', 'crit'], 'crit'))
        self.assertTrue(thermal.escalated(['warn', 'crit'], 'warn'))
        self.assertFalse(thermal.escalated(['warn'], 'warn'))
        self.assertFalse(thermal.escalated([], 'crit'))

    def test_notify_cooldown(self):
        self.assertTrue(thermal.due(None, 100.0))
        self.assertFalse(thermal.due(100.0, 100.0 + thermal.NOTIFY_COOLDOWN_S - 1))
        self.assertTrue(thermal.due(100.0, 100.0 + thermal.NOTIFY_COOLDOWN_S))


class CpuPercentTest(unittest.TestCase):
    def test_delta_between_samples(self):
        hz = os.sysconf('SC_CLK_TCK')
        rows = [{'pid': 5, 'ticks': 100 + 2 * hz, 'start': 100}]
        cpu = thermal.cpu_percent({'5': [100, 100, 1000.0]}, rows, 1002.0)
        self.assertAlmostEqual(cpu[5], 100.0, places=1)

    def test_lifetime_fallback_on_first_sample(self):
        hz = os.sysconf('SC_CLK_TCK')
        rows = [{'pid': 5, 'ticks': 5 * hz, 'start': int(1 * hz)}]
        cpu = thermal.cpu_percent({}, rows, 1000.0, uptime_s=101.0)
        self.assertAlmostEqual(cpu[5], 5.0, places=1)

    def test_pid_reuse_is_not_measured(self):
        rows = [{'pid': 5, 'ticks': 999, 'start': 555}]
        self.assertEqual(thermal.cpu_percent({'5': [100, 100, 1000.0]}, rows, 1002.0), {})


class RotateTest(unittest.TestCase):
    def test_keeps_newest_half(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'log.jsonl'
            path.write_text(''.join(f'line{i}\n' for i in range(10)))
            self.assertTrue(thermal.rotate(path, max_bytes=10))
            self.assertLessEqual(len(path.read_text().splitlines()), 6)
            self.assertIn('line9', path.read_text())

    def test_no_rotation_below_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'log.jsonl'
            path.write_text('short\n')
            self.assertFalse(thermal.rotate(path, max_bytes=1000))
            self.assertEqual(path.read_text(), 'short\n')


class GuardRunTest(unittest.TestCase):
    """The enforcement path: builds are signalled, the ceiling is recorded."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state_dir = self.root / 'state'
        self.args = types.SimpleNamespace(
            apply=True, dry_run=False, notify=True, warn_c=62.0, crit_c=68.0, streak=2,
            allow_file=self.root / 'allow', state_dir=self.state_dir)

    def data(self, temp_c):
        rows = [proc(11, ['/bin/sh', './gradlew', 'cleanTest', 'test'], ticks=5000),
                proc(22, ['yes'], ticks=4000),
                proc(33, ['python3', '/usr/local/bin/spectre-state', 'health'], ticks=100)]
        return {'procs': rows, 'cpu': {11: 95.0, 22: 80.0, 33: 1.0}, 'temp_c': temp_c,
                'policy': {'max_perf_pct': 55, 'no_turbo': 1}, 'ticks': {}}

    def intensive_data(self):
        rows = [proc(44, ['ffmpeg', '-i', 'clip.mp4'], rss_mib=2048),
                proc(33, ['python3', '/usr/local/bin/spectre-state', 'health'], ticks=100)]
        return {'procs': rows, 'cpu': {44: 12.5, 33: 1.0}, 'temp_c': 50.0,
                'policy': {'max_perf_pct': 55, 'no_turbo': 1}, 'ticks': {}}

    def test_intensive_workload_is_stopped_reported_and_ledgered(self):
        kills = []

        def fake_terminate(row):
            kills.append(row)
            return {'pid': row['pid'], 'ok': True, 'reason': row.get('reason'),
                    'comm': row.get('comm'), 'family': row.get('family'),
                    'cpu_pct': row.get('cpu_pct'), 'rss_mib': row.get('rss_mib')}

        sent = []
        with mock.patch.object(guard, 'collect', return_value=self.intensive_data()), \
             mock.patch.object(guard, 'terminate', side_effect=fake_terminate), \
             mock.patch.object(guard, 'notify', side_effect=lambda *a, **k: sent.append(a) or True):
            state, code = guard.run_once(self.args, {'levels': ['ok'], 'notified': {}}, NOW + 5)

        self.assertEqual([k['pid'] for k in kills], [44])
        self.assertEqual(code, 1)
        self.assertEqual(state['level'], 'ok')
        self.assertEqual(sorted(state['notified']), ['intensive'])
        messages = ' '.join(str(call) for call in sent)
        self.assertIn('offload candidate stopped', messages)
        self.assertIn('spectre-offload, RUNBOOK 7.21', messages)
        self.assertIn('2048', messages)
        samples = [json.loads(line) for line in (self.state_dir / 'thermal.jsonl').read_text().splitlines()]
        self.assertEqual(samples[-1]['intensive_workloads'][0]['reason'], 'intensive_workload:rss')
        ledger = [json.loads(line) for line in (self.state_dir / 'thermal-kills.jsonl').read_text().splitlines()]
        self.assertEqual(ledger[-1]['actions'][0]['reason'], 'intensive_workload:rss')

    def test_check_mode_signals_nothing_for_an_intensive_process(self):
        self.args.apply = False
        self.args.notify = False
        sent = []
        with mock.patch.object(guard, 'collect', return_value=self.intensive_data()), \
             mock.patch.object(guard, 'terminate') as term, \
             mock.patch.object(guard, 'notify', side_effect=lambda *a, **k: sent.append(a) or True):
            state, code = guard.run_once(self.args, {'levels': ['ok'], 'notified': {}}, NOW + 6)
        term.assert_not_called()
        self.assertEqual(sent, [])
        self.assertEqual(code, 1)
        self.assertEqual(state['notified'], {})

    def test_cpu_intensive_needs_three_consecutive_samples(self):
        data = self.intensive_data()
        data['procs'] = [proc(44, ['ffmpeg', '-i', 'clip.mp4'], rss_mib=10)]
        data['cpu'] = {44: 95.0}
        kills = []

        def fake_terminate(row):
            kills.append(row)
            return {'pid': row['pid'], 'ok': True, 'reason': row.get('reason')}

        with mock.patch.object(guard, 'collect', return_value=data), \
             mock.patch.object(guard, 'terminate', side_effect=fake_terminate), \
             mock.patch.object(guard, 'notify', return_value=True):
            state, _ = guard.run_once(self.args, {'levels': ['ok'], 'notified': {}}, NOW + 7)
            self.assertEqual((kills, state['cpu_streaks']), ([], {'44': 1}))
            state, _ = guard.run_once(self.args, state, NOW + 8)
            self.assertEqual((kills, state['cpu_streaks']), ([], {'44': 2}))
            state, code = guard.run_once(self.args, state, NOW + 9)
        self.assertEqual([k['pid'] for k in kills], [44])
        self.assertEqual(kills[0]['reason'], 'intensive_workload:cpu')
        self.assertEqual(state['cpu_streaks'], {'44': 3})
        self.assertEqual(code, 1)

    def test_build_is_signalled_and_reported(self):
        kills = []

        def fake_terminate(row):
            kills.append(row)
            return {'pid': row['pid'], 'ok': True, 'reason': row.get('reason'),
                    'comm': row.get('comm'), 'family': row.get('family')}

        sent = []
        with mock.patch.object(guard, 'collect', return_value=self.data(50.0)), \
             mock.patch.object(guard, 'terminate', side_effect=fake_terminate), \
             mock.patch.object(guard, 'notify', side_effect=lambda *a, **k: sent.append(a) or True):
            state, code = guard.run_once(self.args, {'levels': ['ok'], 'notified': {}}, NOW)

        self.assertEqual([k['pid'] for k in kills], [11])
        self.assertEqual(code, 1)
        self.assertEqual(state['level'], 'ok')
        messages = ' '.join(str(call) for call in sent)
        self.assertIn('compile-farm block', messages)
        self.assertIn('gradle', messages)
        samples = [json.loads(line) for line in (self.state_dir / 'thermal.jsonl').read_text().splitlines()]
        self.assertEqual(samples[-1]['build_workloads'][0]['family'], 'gradle')
        self.assertEqual(samples[-1]['temp_c'], 50.0)
        # `now` is monotonic; the stored timestamp must still be wall-clock.
        self.assertGreater(int(samples[-1]['ts'][:4]), 2000)
        ledger = [json.loads(line) for line in (self.state_dir / 'thermal-kills.jsonl').read_text().splitlines()]
        self.assertEqual(ledger[-1]['actions'][0]['pid'], 11)

    def test_crit_stops_only_non_agent_hot_processes(self):
        kills = []

        def fake_terminate(row):
            kills.append(row)
            return {'pid': row['pid'], 'ok': True, 'reason': row.get('reason'), 'comm': row.get('comm'),
                    'family': row.get('family')}

        with mock.patch.object(guard, 'collect', return_value=self.data(70.0)), \
             mock.patch.object(guard, 'terminate', side_effect=fake_terminate), \
             mock.patch.object(guard, 'notify', return_value=True):
            state, code = guard.run_once(self.args, {'levels': ['crit'], 'notified': {}}, NOW + 2)

        self.assertEqual(code, 2)
        reasons = {k['pid']: k.get('reason') for k in kills}
        self.assertEqual(reasons.get(11), 'build_workload:gradle')
        self.assertEqual(reasons.get(22), 'thermal_crit')
        self.assertNotIn(33, reasons)
        self.assertEqual(state['level'], 'crit')

    def test_dry_run_signals_nothing(self):
        self.args.apply = False
        with mock.patch.object(guard, 'collect', return_value=self.data(70.0)), \
             mock.patch.object(guard, 'terminate') as term, \
             mock.patch.object(guard, 'notify', return_value=True):
            _, code = guard.run_once(self.args, {'levels': ['crit'], 'notified': {}}, NOW + 3)
        term.assert_not_called()
        self.assertEqual(code, 2)

    def test_recovery_notice_when_leaving_warn(self):
        sent = []
        with mock.patch.object(guard, 'collect', return_value=self.data(50.0)), \
             mock.patch.object(guard, 'terminate', return_value={'pid': 11, 'ok': True, 'reason': 'build_workload:gradle'}), \
             mock.patch.object(guard, 'notify', side_effect=lambda *a, **k: sent.append((a, k)) or True):
            guard.run_once(self.args, {'levels': ['warn'], 'notified': {}, 'level': 'warn'}, NOW + 4)
        self.assertTrue(any(call[1].get('recovery') for call in sent))


if __name__ == '__main__':
    unittest.main()
