"""Thermal ceiling and compile-farm enforcement for the Spectre worker box.

Three independent rules, all from AGENTS.md ("the Spectre is an agent runtime,
not a compile farm"):

1. Build-class workloads never belong on this box. Any process whose argv is a
   compiler, build tool, JVM/Gradle daemon or project test runner is signalled
   and reported, at every temperature. Classification is argv-based on purpose:
   a `grep gradlew` or a packet file that merely mentions Gradle must not match.
2. CPU/memory-intensive work that is not agent tooling belongs in the Lightning
   Studio (`spectre-offload`, RUNBOOK 7.21). A process holding the RSS ceiling is
   stopped at once; a process only over the CPU ceiling needs a consecutive-sample
   streak, because one busy sample is normal for an agent. Build-class processes
   are owned by rule 1, unless the operator allowlisted their family.
3. Package temperature is a ceiling, not a target. Samples are appended to a
   JSONL log with the top CPU consumers so an audible-fan incident is
   diagnosable after the fact; at CRIT the guard stops the hottest process that
   is neither agent tooling nor core infrastructure.

The 2012 EC owns the fan and exposes no tachometer (hp hwmon pwm1 is N/A), so
temperature is the only available proxy. Measured on 2026-09-26 (see LESSONS):
4-thread load at max_perf_pct 25-55 sits at 56-60 C, so the frequency ceiling is
a weak lever and workload removal is the real control.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import time

WARN_C = 62.0
CRIT_C = 68.0
HOT_STREAK = 2
# Intensive non-agent work: an RSS ceiling that fires on one sample, a CPU
# ceiling measured per-process across all cores (see cpu_percent) that needs a
# consecutive-sample streak.
INTENSIVE_RSS_MIB = 1536
INTENSIVE_CPU_PCT = 90.0
INTENSIVE_STREAK = 3
NOTIFY_COOLDOWN_S = 1800
LOG_MAX_BYTES = 5 * 1024 * 1024
SAMPLE_INTERVAL_S = 60
DISPLAY_CONSUMERS = 5
STATE_DIR = Path.home() / '.local/state/remote-agent'

# Shells and launchers whose real payload is a later argument.
LAUNCHERS = frozenset({
    'sh', 'bash', 'dash', 'zsh', 'ksh', 'setsid', 'nohup', 'env', 'timeout',
    'nice', 'ionice', 'chrt', 'stdbuf', 'sudo', 'doas', 'xargs', 'systemd-run',
    'npx',
})
# argv[0] basename -> family. Compilers, build systems, package managers,
# project test runners.
BUILD_EXECUTABLES = {
    'gradle': 'gradle', 'gradlew': 'gradle', 'mvn': 'maven', 'mvnw': 'maven',
    'ant': 'ant', 'cargo': 'cargo', 'rustc': 'rustc', 'make': 'make',
    'gmake': 'make', 'ninja': 'ninja', 'bazel': 'bazel', 'msbuild': 'msbuild',
    'dotnet': 'dotnet', 'javac': 'javac', 'kotlinc': 'kotlinc',
    'scalac': 'scalac', 'groovyc': 'groovyc', 'gcc': 'cc', 'g++': 'cxx',
    'clang': 'cc', 'clang++': 'cxx', 'cc1': 'cc', 'cc1plus': 'cxx',
    'ld': 'ld', 'as': 'as', 'tsc': 'typescript', 'vite': 'vite',
    'webpack': 'webpack', 'esbuild': 'esbuild', 'rollup': 'rollup',
    'npm': 'npm', 'pnpm': 'pnpm', 'yarn': 'yarn', 'bun': 'bun',
    'pytest': 'pytest', 'tox': 'tox', 'nox': 'nox', 'playwright': 'playwright',
}
# Container engines only when the subcommand actually builds.
CONTAINER_ENGINES = frozenset({'podman', 'docker', 'buildah', 'nerdctl'})
CONTAINER_BUILD_SUBCOMMANDS = frozenset({'build', 'bud', 'buildx'})
# Interpreters that are only build-class for specific arguments.
JVM_MARKERS = ('org.gradle', 'GradleWorkerMain', 'GradleWrapperMain',
               'GradleDaemon', 'gradle-wrapper.jar', 'surefire',
               'kotlin-compiler', 'kotlin-daemon')
NODE_MARKERS = ('npm-cli.js', 'npx-cli.js', '/vite', 'webpack', 'esbuild',
                'rollup', 'typescript/lib/tsc', 'next/dist/bin/next',
                'node_modules/.bin/tsc', 'node_modules/.bin/vite')
PYTHON_TEST_MODULES = frozenset({'pytest', 'tox', 'nox'})
PYTHON_RE = re.compile(r'python3(\.\d+)?')

# Never signalled, at any temperature. Core infrastructure plus the agent
# tooling itself: the guard stops runaway work, not the harness that runs it.
CORE_PROTECTED = (
    'sshd', 'tailscaled', 'systemd', 'systemd-logind', 'dbus-daemon',
    'orca-ide', 'orca', 'spectre-state', 'spectre-worker-state',
    'spectre-slack-bridge', 'slack-bridge', 'spectre-thermal-guard',
    'spectre-reaper', 'spectre-loop', 'spectre-pin-sync', 'spectre-continuity',
    'spectre-healthcheck', 'codex-provider-health', 'devspace', 'Xorg',
    'xfce4', 'pulseaudio', 'tmux', 'dbus-broker', 'pipewire',
    'spectre-worker-state-watchdog',
)
# The CRIT fallback must not kill the agent harness or its shells; only the
# build rule may reach an agent's child processes. `obscura` is the agent
# browser (RUNBOOK 7.19): its CDP worker is agent tooling, not runaway work.
# Interpreters are deliberately NOT listed: `protected()` also matches the first
# two argv basenames, so `python3 /usr/local/bin/spectre-state` (and any
# `node /usr/local/bin/spectre-slack-bridge`) stays protected while a generic
# `python3 -c '…'` data crunch does not — which is exactly the work that must be
# offloaded to the Studio (RUNBOOK 7.21).
AGENT_PROTECTED = CORE_PROTECTED + (
    'dsh', 'deepseek-harness', 'codex', 'claude', 'qoder', 'mimo-clinepass',
    'obscura',
)


def read_temp_c(hwmon_root: Path | str = Path('/sys/class/hwmon'),
                thermal_root: Path | str = Path('/sys/class/thermal')) -> float | None:
    """Package temperature in C. coretemp 'Package id 0' first, then zones."""
    for hwmon in sorted(Path(hwmon_root).glob('hwmon*')):
        try:
            if hwmon.joinpath('name').read_text().strip() != 'coretemp':
                continue
        except OSError:
            continue
        for value in sorted(hwmon.glob('temp*_input')):
            try:
                label = value.with_name(value.name.replace('_input', '_label')).read_text().strip()
            except OSError:
                label = ''
            if label == 'Package id 0':
                return _milli_to_c(value)
    for name in ('x86_pkg_temp', 'acpitz'):
        for zone in sorted(Path(thermal_root).glob('thermal_zone*')):
            try:
                if zone.joinpath('type').read_text().strip() != name:
                    continue
            except OSError:
                continue
            temp = _milli_to_c(zone / 'temp')
            if temp is not None:
                return temp
    return None


def _milli_to_c(path: Path) -> float | None:
    try:
        raw = re.sub(r'[^0-9-]', '', path.read_text())
        return int(raw) / 1000.0 if raw else None
    except (OSError, ValueError):
        return None


def read_policy(pstate: Path | str = Path('/sys/devices/system/cpu/intel_pstate')) -> dict:
    """intel_pstate levers as ints; missing files are omitted, never zeroed."""
    out: dict[str, int] = {}
    for key in ('no_turbo', 'max_perf_pct', 'min_perf_pct', 'turbo_pct'):
        try:
            out[key] = int(Path(pstate).joinpath(key).read_text().strip())
        except (OSError, ValueError):
            continue
    return out


def classify(argv: list[str], depth: int = 0) -> str | None:
    """Build-class family for an argv, or None. Launchers are unwrapped."""
    if not argv or depth > 4:
        return None
    exe = Path(argv[0]).name
    if exe in LAUNCHERS:
        index = 1
        while index < len(argv):
            arg = argv[index]
            if arg == '-c' and index + 1 < len(argv):
                try:
                    return classify(shlex.split(argv[index + 1]), depth + 1)
                except ValueError:
                    return None
            if arg.startswith('-'):
                index += 1
                continue
            return classify(argv[index:], depth + 1)
        return None
    if exe in CONTAINER_ENGINES:
        return exe if any(arg in CONTAINER_BUILD_SUBCOMMANDS for arg in argv[1:]) else None
    if exe == 'python' or PYTHON_RE.fullmatch(exe):
        for index, arg in enumerate(argv[1:], start=1):
            if arg == '-m' and index + 1 < len(argv):
                return argv[index + 1] if argv[index + 1] in PYTHON_TEST_MODULES else None
        return None
    joined = ' '.join(argv[1:])
    if exe == 'java':
        return 'jvm-build' if any(marker in joined for marker in JVM_MARKERS) else None
    if exe == 'node':
        return 'node-build' if any(marker in joined for marker in NODE_MARKERS) else None
    return BUILD_EXECUTABLES.get(exe)


def protected(proc: dict, names: tuple[str, ...] = CORE_PROTECTED) -> bool:
    """Identity match only: comm, or the first two argv basenames.

    Never the whole command string: an agent's workspace path contains `orca`,
    and that must not protect the Gradle process running inside it.
    """
    comm = str(proc.get('comm') or '')
    if comm and any(name in comm for name in names):
        return True
    argv = [str(a) for a in (proc.get('argv') or [])]
    return bool({Path(a).name for a in argv[:2]} & set(names))


def build_workloads(procs: list[dict], allow: frozenset[str] = frozenset()) -> list[dict]:
    """Processes that make the box a compile farm, minus operator allowlists."""
    out = []
    for proc in procs:
        family = classify([str(a) for a in (proc.get('argv') or [])])
        if not family or family in allow or protected(proc):
            continue
        out = [*out, {**proc, 'family': family, 'reason': f'build_workload:{family}'}]
    return out


def next_streaks(prev: dict, cpu: dict[int, float],
                 threshold: float = INTENSIVE_CPU_PCT) -> dict[int, int]:
    """Consecutive-sample CPU streak per pid.

    A pid at or above `threshold` in this sample keeps its previous count and
    increments it; every other pid — below threshold, or gone from `cpu` — is
    dropped, so the count is consecutive by construction. `prev` is keyed by pid
    (JSON round-trips make them strings, so both forms are accepted).
    """
    counts = {int(pid): int(value) for pid, value in (prev or {}).items()}
    out: dict[int, int] = {}
    for pid, pct in cpu.items():
        key = int(pid)
        if float(pct) >= threshold:
            out[key] = counts.get(key, 0) + 1
    return out


def intensive_workloads(procs: list[dict], cpu: dict[int, float], streaks: dict[int, int],
                        allow: frozenset[str] = frozenset()) -> list[dict]:
    """Non-agent processes to offload to the Studio, hottest first.

    Rule 2 of the module docstring: not agent tooling or core infrastructure, not
    zombie, and not build-class (rule 1 owns those) unless the operator
    allowlisted the family. An RSS ceiling fires on a single sample; the CPU
    ceiling is per-process across all cores, so it needs INTENSIVE_STREAK
    consecutive samples.
    """
    rows = []
    for proc in procs:
        if protected(proc, AGENT_PROTECTED) or str(proc.get('process_state') or '') == 'Z':
            continue
        family = classify([str(a) for a in (proc.get('argv') or [])])
        if family and family not in allow:
            continue
        pid = int(proc['pid'])
        rss_mib = float(proc.get('rss_mib') or 0)
        cpu_pct = float(cpu.get(pid, 0.0))
        if rss_mib >= INTENSIVE_RSS_MIB:
            reason = 'intensive_workload:rss'
        elif cpu_pct >= INTENSIVE_CPU_PCT and int(streaks.get(pid, 0)) >= INTENSIVE_STREAK:
            reason = 'intensive_workload:cpu'
        else:
            continue
        rows = [*rows, {**proc, 'family': None, 'reason': reason,
                        'cpu_pct': round(cpu_pct, 1), 'rss_mib': round(rss_mib, 1)}]
    rows.sort(key=lambda p: (p['cpu_pct'], p['rss_mib']), reverse=True)
    return rows


def cpu_percent(prev: dict, procs: list[dict], now: float,
                uptime_s: float | None = None) -> dict[int, float]:
    """Per-pid CPU%% from tick deltas, falling back to a lifetime average.

    `prev` maps str(pid) -> [ticks, starttime, monotonic]. A pid seen for the
    first time has no delta, so its lifetime average is used: that still catches
    a build started between two samples.

    The value is a percentage of one core, so a multi-threaded process can
    exceed 100 (it is capped at 100 * cpu_count). INTENSIVE_CPU_PCT is therefore
    a per-process, all-cores threshold, not a share of the whole box.
    """
    out: dict[int, float] = {}
    hz = os.sysconf('SC_CLK_TCK') or 100
    for proc in procs:
        pid, ticks, start = int(proc['pid']), int(proc.get('ticks') or 0), int(proc.get('start') or 0)
        seen = prev.get(str(pid))
        measured = None
        if isinstance(seen, list) and len(seen) >= 3:
            prev_ticks, prev_start, prev_at = int(seen[0]), int(seen[1]), float(seen[2])
            elapsed = now - prev_at
            if prev_start == start and elapsed > 0:
                measured = 100.0 * ((ticks - prev_ticks) / hz) / elapsed
        if measured is None and uptime_s:
            life = uptime_s - (start / hz)
            if life > 1:
                measured = 100.0 * (ticks / hz) / life
        if measured is not None:
            out[pid] = max(0.0, min(measured, 100.0 * max(1, os.cpu_count() or 1)))
    return out


def level_for(temp_c: float | None, warn: float = WARN_C, crit: float = CRIT_C) -> str:
    if temp_c is None:
        return 'unknown'
    if temp_c >= crit:
        return 'crit'
    if temp_c >= warn:
        return 'warn'
    return 'ok'


def escalated(history: list[str], level: str, streak: int = HOT_STREAK) -> bool:
    """True when the last `streak` samples all reached at least `level`."""
    order = {'ok': 0, 'unknown': 0, 'warn': 1, 'crit': 2}
    if level in ('ok', 'unknown') or len(history) < streak:
        return False
    want = order[level]
    return all(order.get(item, 0) >= want for item in history[-streak:])


def crit_targets(procs: list[dict], cpu: dict[int, float], limit: int = 2,
                 allow: frozenset[str] = frozenset()) -> list[dict]:
    """Hottest processes the CRIT fallback may stop.

    Agent tooling and core services are never eligible. Build-class processes are
    owned by the build rule, except families the operator allowlisted: those fall
    back to the thermal rule so an allowed build can still be stopped at CRIT.
    """
    rows = []
    for proc in procs:
        if int(proc['pid']) not in cpu or protected(proc, AGENT_PROTECTED):
            continue
        if str(proc.get('process_state') or '') == 'Z':
            continue
        family = classify([str(a) for a in (proc.get('argv') or [])])
        if family and family not in allow:
            continue
        rows.append(proc)
    rows.sort(key=lambda p: cpu[int(p['pid'])], reverse=True)
    return [{**p, 'cpu_pct': round(cpu[int(p['pid'])], 1)} for p in rows[:limit]]


def consumers(procs: list[dict], cpu: dict[int, float], limit: int = DISPLAY_CONSUMERS) -> list[dict]:
    rows = sorted(procs, key=lambda p: cpu.get(int(p['pid']), 0.0), reverse=True)
    return [{'pid': int(p['pid']), 'comm': p.get('comm'), 'cpu_pct': round(cpu.get(int(p['pid']), 0.0), 1)}
            for p in rows[:limit] if cpu.get(int(p['pid']), 0.0) >= 1.0]


def read_allow(path: Path | str | None) -> frozenset[str]:
    """Operator allowlist: one family name per line, `#` comments."""
    if not path:
        return frozenset()
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines()
                     if line.strip() and not line.strip().startswith('#'))


def rotate(path: Path, max_bytes: int = LOG_MAX_BYTES) -> bool:
    """Keep the newest half when the sample log outgrows its cap."""
    try:
        if path.stat().st_size <= max_bytes:
            return False
        data = path.read_bytes()
        path.write_bytes(data[len(data) // 2:])
        return True
    except OSError:
        return False


def read_uptime_s(path: Path | str = Path('/proc/uptime')) -> float | None:
    try:
        return float(Path(path).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def load_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(f'{path}.tmp')
    tmp.write_text(json.dumps(state, sort_keys=True), encoding='utf-8')
    tmp.replace(path)


def due(last_ts: float | None, now: float, cooldown: float = NOTIFY_COOLDOWN_S) -> bool:
    return last_ts is None or (now - float(last_ts)) >= cooldown


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + '\n')


def now_iso(now: float | None = None) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(now if now else time.time()))
