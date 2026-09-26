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

## The offload path, verified the same day

The account turned out to exist already (`person414213-q1op6`,
teamspace `language-model`), but no Linux machine had a CLI, a key or a Studio —
the older `~/Projects/*/lightning_offload/run_chunky.sh` workflow was a script
pasted into the browser Studio, so nothing was automatable. What made it real:

1. `lightning-sdk` in a venv on fedora (`~/.local/share/lightning-cli/venv`) +
   `lightning login` (browser callback; the CLI's own `webbrowser.open` fails in
   a plain shell — a `BROWSER` shim that records the URL and calls `xdg-open`
   with the session's `WAYLAND_DISPLAY`/`XAUTHORITY`/`DBUS_SESSION_BUS_ADDRESS`
   worked). Credentials land in `~/.lightning/credentials.json`; the SDK writes
   it **0644**, so `chmod 600`.
2. The same venv shape on the Spectre via `scripts/install-lightning-offload.sh`
   (`/usr/local/lib/lightning-cli/venv`, `/usr/local/bin/lightning`), the
   credential **file** copied over stdout-free (mode 600), and the non-secret
   target in `~/.config/remote-agent/lightning.env`.
3. `lightning studio create --name buildbox`, `studio start --machine CPU`,
   `lightning ssh configure --name buildbox` → Host `buildbox`
   (`s_…@ssh.lightning.ai`, `~/.ssh/lightning_rsa`).

Ladder results (all on the box, in order): dry-run plan →
`-- cat hello.txt` rc 0 (rsync had to be installed *inside* the Studio; the
wrapper now does that once) → `./gradlew --version` (Gradle 9.4.0, JVM 21 from
`--setup openjdk-21-jdk-headless`) → `./gradlew compileJava` failed honestly on
the missing **JDK 25 toolchain** with exit 1 propagated → after a Temurin 25
`--setup`, **BUILD SUCCESSFUL in 59 s** in the Studio → final run without
`--keep` stopped the Studio (`studio list` → `Stopped`). Throughout, the box's
guard logged 54–60 °C: the build ran off-box. Measured Studio: Ubuntu 24.04.5,
4 vCPU, 15 GB RAM, 387 GB disk, `/teamspace/studios/this_studio`, no Java/rsync.

Two shell traps worth remembering: `sh -lc '…'` inside the Studio dies with
`Bad substitution` (dash + a non-POSIX login profile), and the Studio's ssh
login shell is zsh. The wrapper runs commands in the default shell.

## Storage is billed, so the offload leaves nothing behind

The operator's warning was right, and the billing FAQ confirms it: "the first
10 GB of data in the Drive are unbilled" (then **$0.10/GB/month, billed daily**;
Free caps the Drive at 50 GB) and "there is no cost for sleeping Studios, other
than the storage costs for the files on that Studio". A fat Studio also sleeps
slower (documented: sleep time scales with stored data).

What was actually in the Studio after a day of testing:

| Item | Size | Disposition |
| --- | --- | --- |
| uploaded `~/offload/coin-bridge` | 10.6 MB | **deleted by the wrapper**, verified `remote_cleaned: "yes"` |
| leftover trees from the killed test run | 14 MB | found by hand and removed (the wrapper now has an EXIT trap) |
| `~/.gradle` (wrapper dist + caches) | 590 MB → 443 MB | kept: it is why the repeat build ran `FROM-CACHE` in **7 s** |
| Temurin 25 in `$HOME/.jdks` | ≈300 MB | kept: persistent, Gradle auto-detects it |
| Studio home total | **894 MB ≈ 9 % of the free 10 GB** | audited with `spectre-offload --studio-report` |

Two findings changed the recipe. (1) **The root filesystem is not yours**: a
Temurin tarball extracted into `/usr/lib/jvm` was gone after the next Studio
start, while apt packages and everything under `$HOME` survived — so the JDK
now goes to `$HOME/.jdks`, which Gradle's toolchain detection scans. (2) **A
killed wrapper leaks**: when a local `timeout` killed the ssh client mid-run, the
uploaded tree stayed and the Studio kept running. The wrapper now traps
`EXIT/HUP/INT/TERM`, deletes the tree unless `--keep-remote`, and documents
`setsid nohup spectre-offload …` for builds that may outlive the caller.
`--prune-caches` drops the build caches too (slower next build); `--studio-report`
prints the audit fields (`home_used`, `free`, `offload_trees`, `cache_mb`,
`uptime_s`, `boot`, `lifecycle`).

## The 4-hour cap: documented, and staged for measurement

The only statement found is the pricing footnote: "Free Studios run 24/7 but
require restart every 4 hours. No restrictions in Pro or higher." The API does
not expose it (`V1CloudSpace.max_run_duration = 0`, `operating_cost` empty). So
the platform's behaviour is being observed rather than assumed: the Studio now
carries `~/.lightning_studio/on_start.sh` (appends a line at every launch) plus a
bounded 6 h heartbeat that records `uptime -s` every 5 minutes, and the box has a
one-off `systemd-run --user --on-active=4h15m` check writing
`~/.local/state/remote-agent/studio-cap-check.log`. A new `boot=` line, a gap in
the heartbeat, or a `Running` status with a small `uptime_s` answers
auto-vs-manual relaunch. Result goes into RUNBOOK §7.21.

## Not verified, and what is left

- **Lightning:** whether the free Studio actually accrues no credits (check the
  billing UI), the 4-hour cap's behaviour against an in-flight build (staged
  above), and `sandbox`/`job` billing. The full integration suite that cost the
  box 11 h has not been run in the Studio, so its duration there is unknown.
- **Battery/EC heat** is outside the guard's view: no charge-threshold node on
  this EC (100 % on AC) and no fan tachometer, so a mechanical fan fault or
  battery heat would look normal. Physical checks stay the operator's.
- **Repo state**: the working tree already carried uncommitted sections from the
  2026-09-26 control-plane session (`RUNBOOK.md`, `AGENTS.md`, untracked
  `LESSONS.md`). This change's new files and `scripts/doctor.sh` are committed;
  the three doc files are left in the working tree so their earlier sections
  commit as their author intended.

## Operator follow-ups

1. Decide how far to push offloads: the wrapper is manual by design (kill +
   notify, then a human or agent runs `spectre-offload`). Automating the handoff
   from the guard's kill message is the next step if the current split proves
   annoying.
2. Check the BIOS for a battery-health/charge-limit setting; it is the only fix
   for the 100 %-on-AC heat the guard cannot see.
3. Decide the speed/silence tradeoff: keep `powersave` (≈800 MHz, coolest) or
   switch the AC governor to `schedutil` with `CPU_MAX_PERF_ON_AC=55`.

## A failure after the start stops the Studio too — 2026-09-27

What happened: two offloads ran back to back. The second one's `studio start`
got `INFO - Studio buildbox is already running`, and SSH answered. The rsync
check then failed with `Error: We are still setting things up for you, please
try again after the progress bar at the top of the Studio disappears.`, so the
run ended with `could not install rsync in buildbox` (exit 6). The EXIT trap
only removed the uploaded tree, and only the end of a normal run stopped the
Studio. So every `die` after the start left the Studio running. The ones
affected were the SSH wait, the rsync install and the rsync push. The Studio
was found running two minutes later with `spectre-offload --studio-report`
(`uptime_s: 130.57`). It was stopped by the next normal run.

The fix: the run sets `STARTED=1` before `studio start`. `stop_studio()` stops
the Studio only when this run started it and `--keep` was not given, and only
once. It runs from the EXIT/HUP/INT/TERM trap after `cleanup_remote`, and at
the normal place before the JSON verdict. `--studio-report` never starts the
Studio, so it never stops one either.

`tests/test_spectre_offload.py` runs the script against a fake `lightning`,
`ssh` and `rsync` on `PATH`. A failed remote rsync install exits 6 and stops
the Studio exactly once. A normal run stops it once. `--keep` and
`--studio-report` stop nothing.

The same investigation looked at why `home_used` climbed from 1.2G to 2.7G
over the day's runs. A read-only report found none of it came from the
control-plane runs, whose trees were all removed (`remote_cleaned: yes`). The
growth is other sessions' data:

- `~/.gradle`, 1.9G. Of that, `caches` is 1.6G: fabric-loom Minecraft jars and
  the Gradle 9.4.0 and 9.6.1 API jars.
- `~/.jdks`, 303M.
- Trees left in `~/offload`: `tutorial-residuals` 244M,
  `fmc-panorama-20260926T151219Z-269661` 53M, `rb-before` 23M,
  `dashboard-runtime` 3.1M and two small ones.

Nothing was deleted. `--prune-caches` would drop the Gradle caches at the cost
of the next build's download, and the `~/offload` trees belong to whoever left
them.

The installed `/usr/local/bin/spectre-offload` is identical to the previous
revision. Installing this one is the operator's step.

Verified the same way as the control-plane slices. `verify.sh` ran through
`spectre-offload` for `git archive 347f62e` and for the same tree plus the
changed script and the new test. Both trees gave the same gate results, apart
from unit tests going from 541 to 544 run, with the same one known Studio
failure (`test_session_name_falls_back_to_home`). Against the HEAD script, the
failure-path test fails
(`Lists differ: [] != ['lightning studio stop --name box --teamspace team']`),
and the other two pass.

## The shared Studio: stop only what an offload started, with the last lease — 2026-09-27

One Studio, `buildbox`, serves every session on this box. Until now every run
stopped it at the end, and `8f91eb2` made a failed run stop it as well. On
2026-09-27 that went wrong. A run from this session found `INFO - Studio
buildbox is already running`, hit `We are still setting things up for you` on
its first remote command, exited 6, and stopped the Studio. Another session
had just started that Studio for its own offload (`lightning studio list`
showed `Pending` minutes later), so this run most likely cut that offload off.
Two changes follow.

**Readiness is an exact echo.** A freshly started Studio accepts SSH before it
is set up and answers commands with the setup message. The wait loop used
`ssh … true`, which passed too early. It now waits until `ssh … 'echo
spectre-ready'` prints exactly `spectre-ready`. Both of the day's rsync
failures were this.

**Leases, and a start marker.** Each run takes a lease before `studio start`.
The lease is a file named by the run's pid in
`~/.local/state/remote-agent/offload-leases`, or in `LIGHTNING_LEASE_DIR`.
While taking it, the run reads the Studio's status. Unless the status is
`Running` or `Pending`, it writes `.started`, meaning an offload lease started
this Studio. At exit, from the trap or the normal path, the run drops its lease
and every lease whose pid is gone. It stops the Studio only when no live lease
remains and `.started` is present, and then it removes the marker. Otherwise
it notes `leaving Studio <name> running for <n> other offload(s)` or `leaving
Studio <name> running: it was already up when this offload began`. Taking,
dropping and counting run under one `flock`. The status call is bounded by
`timeout 60`, and `--studio-report` uses the same helper.

This protects a run only from runs that use this script. Until
`/usr/local/bin/spectre-offload` is reinstalled, other sessions neither take
leases nor honour them. What the Lightning CLI does with a start that arrives
during another run's stop has not been observed.

### Verification

This slice's Studio run used the working-tree `scripts/spectre-offload.sh`.
The first attempt was the failure described above. On the second, the lease
directory held only the run's pid (`355086`) and `.lock`, with no `.started`,
because the Studio was already up for another session.

At the end the run noted `leaving Studio buildbox running: it was already up
when this offload began`, and the lease directory held only `.lock`. The other
session's offload was still running, and the Studio was `Running`.

`verify.sh` ran on `git archive 553e4ac` and on the same tree plus the changed
script and test. Both trees gave the same gate results, apart from unit tests
going from 546 to 550 run, with the same one known Studio failure. Against the
HEAD script, the four new behaviours fail and the three earlier tests pass:

- a dead lease gives `['13145'] != ['.lock']`;
- a live other lease gives no `leaving … for 1 other offload(s)`;
- a Studio found `Running` or `Pending` gives no `already up`;
- the setup message is not waited out.

All seven pass on the changed script.
