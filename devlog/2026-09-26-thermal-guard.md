# The fan incident became an enforced rule — 2026-09-26

## What the user reported

On 2026-09-25 the Spectre's fan became audible and the operator's remedy was
physical: the charger came off (battery mode quiets it). The box was reconnected
on 2026-09-26 with one requirement — it must not happen again. The user chose
"offload heavy work to a free Lightning.ai VM" over "lower the CPU cap", plus
"kill build workloads on detection and report to Slack".

## Root cause, from the box's own journal

`journalctl -b -1` on the Spectre (boot 2026-09-17 17:49 → 2026-09-26 01:02)
shows what ran: `dsh-subprocess-*` scopes executing

- `sh gradlew integrationTest --tests 'dev.personnya.coinbridge.casino.…'`
- `sh gradlew cleanTest cleanIntegrationTest test integrationTest`
- `sh gradlew integrationTest --tests '…JackpotDrawInvariantTest'`, …

in `~/wt/release-readiness-spectre`, `~/wt/casino-*`,
`~/orca/workspaces/release-readiness-spectre/*` and `/tmp/casino-base-*`, from
**04:32 to 15:57 on 09-25** — 11+ hours of JVM build/test load on a 2C/4T 17 W
2012 ultrabook. AGENTS.md has said since the first commit that the Spectre is
"an agent runtime, not a compile farm". The rule was documentation, and a packet
walked straight through it. (Those packets also printed a PostgreSQL password
from a repo workflow file into their logs; a separate session redacted it.)

## What actually drives this chassis's fan (measured, not assumed)

`scripts/thermal-calibrate.sh` (added here) sweeps `intel_pstate/max_perf_pct`
under a fixed 4-thread load and samples `coretemp` "Package id 0"; `turbostat`
cross-checks the effective clock.

| State | Package temp | Effective clock (`Bzy_MHz`) |
| --- | --- | --- |
| idle | 50–52 °C | 800 |
| 4 threads, 55 % cap (75 s) | 55.1 °C avg / 58 max | 800 |
| 4 threads, 45 / 35 / 30 / 25 % | 58.8 / 56.4 / 55.9 / 56.1 °C avg | 800 |
| 4 threads, 5–12 min soak | 55–57 °C plateau, 0 throttle events | 800 |

Two conclusions, both surprising:

1. **`CPU_MAX_PERF_ON_AC=55` is inert.** With
   `CPU_SCALING_GOVERNOR_ON_AC=powersave` and intel_pstate in **passive** mode
   (`status=passive`, driver `intel_cpufreq`), the actual frequency is pinned to
   `min_perf_pct` (26 % ≈ 800 MHz), so the box has been running at roughly half
   the clock the config implies. The 25→55 % sweep changes nothing measurable:
   the level-to-level differences track heat soak, not the setting.
2. **The frequency ceiling is not the control; workload duration is.** A
   4-thread load plateaus at 55–57 °C. Eleven hours of Gradle on top of that is
   what made the fan audible. So the guard's thresholds (62 warn / 68 crit) sit
   deliberately *above* the measured full-load plateau: they are a safety net
   for a blocked vent or a hotter ambient, not the primary control.

Not changed, deliberately: raising the clock back to 1.65 GHz means switching the
AC governor to `schedutil` and running hotter. That is an explicit operator
decision (documented as such in RUNBOOK §7.20), not a side effect of this work.

## What was built

| File | Role |
| --- | --- |
| `scripts/control_plane/thermal.py` | argv-based build classifier, sensor/policy readers, level + streak logic, CPU deltas, log rotation |
| `scripts/spectre-thermal-guard.py` | the guard: sample, enforce, notify, ledger (`--once`, `--check`, `--apply`, `--dry-run`) |
| `systemd/spectre-thermal-guard.{service,timer}` | user timer, 60 s, `--once --apply` |
| `scripts/install-thermal-guard.sh` | installs the package, binary and units; enables the timer |
| `tests/test_thermal_guard.py` | 34 tests: classification (including the false-positive case), sensors, streaks, CPU deltas, rotation, the full kill/notify/ledger path |
| `scripts/doctor.sh` | four new gates (installed, timer active, guard ok, sample fresh); runs `--check` as the agent user, not root |
| `scripts/thermal-calibrate.sh` | the measurement above, reproducible |
| `scripts/spectre-offload.sh`, `config/lightning.env.example` | heavy builds go to a free Lightning Studio instead (RUNBOOK §7.21) |
| RUNBOOK §7.20/§7.21, AGENTS.md rule, LESSONS × 3 | the rule, the measurements, the offload path |

Classification is by **argv**, never substring: `gradlew`/`gradle`, `maven`,
`ant`, `cargo`/`rustc`, `make`/`ninja`/`bazel`, `javac`/`kotlinc`, `gcc`/`clang`/
`cc1*`, `tsc`/`vite`/`webpack`/`esbuild`, `npm`/`pnpm`/`yarn`/`bun`, `pytest`/
`tox`/`nox`, `podman|docker|buildah … build`, plus JVM/Gradle daemon and test
worker argument markers. A packet that only *mentions* Gradle — `grep -rn
gradlew`, `echo gradlew > note.txt`, a worktree path containing `orca` — is not
touched. That distinction is tested.

## Verified on the box

```text
systemctl --user is-enabled spectre-thermal-guard.timer   → enabled
systemctl --user is-active  spectre-thermal-guard.timer   → active
spectre-thermal-guard --check                            → {"ok":true,"temp_c":52,"level":"ok", policy:{no_turbo:1,max_perf_pct:55,min_perf_pct:26}}
timer-written sample (apply:true)                        → {"ts":"2026-09-26T13:00:xx+0900","temp_c":54,"level":"ok"}
false-positive test: `bash -c "grep -rn gradlew …; sleep 90"` → SURVIVED
synthetic /tmp/…/gradlew                                  → SIGTERM, guard_rc=1,
  ledger {"family":"gradle","reason":"build_workload:gradle","ok":true}
real Slack post                                           → #fleet "compile-farm block", notified:["build"]
warn/crit transitions (isolated state dir, dry-run)       → warn rc=1, crit rc=2 after 2 samples, recovery rc=0
sudo spectre-doctor                                       → 4/4 thermal gates PASS (pre-existing FAILs: qoder tmux, obscura MCP in claude.json, codex provider block)
verify.sh (fedora)                                        → 526 python tests, 91 bridge, 77 devcodex, all gates passed
```

One bug found and fixed during verification: the sample timestamp was built from
`time.monotonic()`, so it read `1970-01-01`; `ts` is now wall-clock (`at` keeps
the monotonic value used for CPU deltas). A test asserts it.

## Not verified, and what is left

- **Lightning offload is UNVERIFIED**: no account existed. Only the refusal path
  (exit 3 with no env file) and `--dry-run` were exercised. Signup needs a
  non-virtual phone number; the free tier is one 4-vCPU Studio, 24/7, with a
  4-hour session cap and a Free-undisable 10-minute idle sleep. The verification
  ladder is in RUNBOOK §7.21.
- **Battery/EC heat** is outside the guard's view: no charge-threshold node on
  this EC (100 % on AC) and no fan tachometer, so a mechanical fan fault or
  battery heat would look normal. Physical checks stay the operator's.
- **Repo state**: the working tree already carried uncommitted sections from the
  2026-09-26 control-plane session (`RUNBOOK.md`, `AGENTS.md`, untracked
  `LESSONS.md`). This change's new files and `scripts/doctor.sh` are committed;
  the three doc files are left in the working tree so their earlier sections
  commit as their author intended.

## Operator follow-ups

1. Create the Lightning account, then run the §7.21 ladder (dry-run → `sleep 5`
   → `./gradlew --version` → a real suite) and replace the UNVERIFIED paragraph.
2. Check the BIOS for a battery-health/charge-limit setting; it is the only fix
   for the 100 %-on-AC heat the guard cannot see.
3. Decide the speed/silence tradeoff: keep `powersave` (≈800 MHz, coolest) or
   switch the AC governor to `schedutil` with `CPU_MAX_PERF_ON_AC=55`.
