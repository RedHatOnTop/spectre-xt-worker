# orca serve forked as a patch series — 2026-09-26

## Why

A large share of the box's incidents trace back to `orca serve`: it is an
Electron desktop app run headless, and it assumes a desktop session exists
around it. Observed failure modes (all in this repo's devlogs):

- **Env hang** (2026-09-08): the lightdm/XFCE user manager leaks
  `DISPLAY`/`DESKTOP_SESSION`/`XDG_*` into every user unit; serve attached to
  the real desktop session and hung at 0 CPU, never binding :6768. Worked
  around with `UnsetEnvironment=` in the unit.
- **Silent exit 0** (2026-09-12): serve self-exited cleanly after 75 minutes
  and stayed dead 8 hours; the journal had no trace of why.
- **Buffered readiness line** (2026-09-08): `orca_server_ready` only reached
  journald at process exit, so a hung serve looked like it printed nothing.
- **Silent serve signal handlers** (found today; see "Deploy" below for how):
  upstream's `registerServeSignalHandlers` never receives a signal, so the
  first SIGTERM quits through Electron's own one-shot handler and a second one,
  after a vetoed quit, kills serve with status 143 and no teardown. The first
  reading of the source ("a packaged serve installs no signal handler") was
  wrong.

Plus the PTY round (same session, second directive — PTY 자체 + PTY 스트리밍):

- **Invisible terminal while alive** (2026-09-15): zzbrush's Orca terminal was
  absent from `terminal list` for 3+ days while its qodercli process lived —
  upstream issue #11342's class: previous daemon generations survive with their
  PTYs, and `terminal list` only talks to the current generation.
- **Streaming stalls leave no evidence**: upstream's stream-backlog probe is
  env-gated (`ORCA_DAEMON_STREAM_BACKLOG_FILE`); an unattended box can't set it
  after the fact, so multi-second echo lag was never attributable.

Upstream is `github.com/stablyai/orca` (MIT, very active — v1.4.212 today; the
box runs 1.4.198). Decision: keep the stock desktop upstream, and carry the
headless delta as a **patch series** that follows upstream tags, in a separate
directory so it never mixes with this repo.

## What landed

`~/Projects/orca-serve-fork/` (own git repo, no remote; 81bdcea..2384ac7):

- `upstream.lock` pins `v1.4.212` (`d937d22f49`); `upstream/` is a blobless
  clone; `build/` is a worktree with branch `serve-fork`.
- `patches/` — seven patches, serve/daemon paths only. Headless serve round:
  1. scrub inherited desktop-session env in serve mode before any display/GPU
     decision (`ORCA_SERVE_KEEP_DISPLAY=1` restores upstream behavior; new
     module + 10 vitest cases). Makes the unit's `UnsetEnvironment=` list
     belt-and-suspenders instead of load-bearing.
  2. after app ready, drop and re-add every SIGINT/SIGTERM/SIGHUP listener so
     upstream's serve handlers actually receive signals, and log
     `[serve] <SIG> received; quitting` for each (final form, see "Deploy").
  3. readiness line written with `writeSync` (EINTR-safe), so it lands in
     journald the moment serve is ready.
  4. serve-only breadcrumbs: `before-quit (trigger)` + final
     `exiting with code N` — the next silent death names itself.
  PTY / streaming round:
  5. daemon startup reports live stale-generation daemons
     (`daemon-v<N>.pid` scan + liveness probe → `stale-generation-daemon` /
     `stale-generation-summary` in daemon.log). Report-only — reaping policy
     stays upstream (#11342, PRs #22343/#17522/#12740).
  6. `pty-session-exit` logged per reaped session — the daemon.log now answers
     "when did this PTY die" (it previously held nothing between startup and
     shutdown).
  7. stream-backlog pacing events (heldWriteThrough, stall events) land in
     daemon.log even without the env gate, rate-limited 30 s per event with a
     suppressed count; the deep 250 ms sampling stays env-gated upstream-only.
- `scripts/apply-patches.sh` (pristine tag + series → `git am`),
  `export-patches.sh` (zero-commit, no-signature → idempotent re-export),
  `update-upstream.sh v<new>` (rebase, auto-drops upstreamed patches,
  refreshes lock + patches). `Makefile`: `bootstrap apply export update TAG=
  test typecheck build clean`.
- Workflow documented in the fork README, including the failure→patch catalog
  and the **scope boundary**: upstream has open PRs for orphan reaping
  (#22343/#22348/#17522), close-on-exec fds (#8855), stranded PTY rows
  (#22085), stale generations (#12740) — the fork adds detection/evidence,
  not duplicates of those.

## Verification (fedora, not the box)

- `git am` round-trip from pristine `v1.4.212`: clean; re-export byte-stable;
  the am-built tree is commit-identical to the edit tree.
- New suites: `serve-headless-env` 10/10, `daemon-stale-generation-report`
  7/7, `daemon-stream-backlog-probe` 4/4; neighbors `serve-readiness` 10/10,
  `daemon-server` 34/34. The `typecheck:tsc:node` "clean" first recorded here
  was wrong: patch 5 typed `readdirSync`'s result as the Buffer overload and
  failed with three errors (fixed to `Dirent[]`). Final: `make test` 68/68
  (6 files), `make typecheck` clean.

## Deploy (Spectre)

Built on fedora with `make deb` (Debian 13 container, committed series →
`dist/orca-ide_1.4.212_serve-<sha>_amd64.deb`), installed with
`scripts/deploy-spectre.sh` (stop, `dpkg -i`, start, wait for the readiness
line). Three packages went out, one per patch 2 attempt:

| deb | patch 2 form | `SIGTERM received` on restart |
| --- | --- | --- |
| `serve-95cb49b62cde` | own early `process.on` listener in preflight | never |
| `serve-f04de78c561f` | log line inside upstream's handlers | never |
| `serve-f76493817e35` | re-arm after app ready + log line | **yes** |

Why the first two stayed silent: `index.js` requires proper-lockfile at top
level, and loading it calls signal-exit's `onExit()`, which adds
SIGINT/SIGTERM/SIGHUP listeners before app ready. Node opens one libuv signal
handle per signal on the first listener, and libuv installs its sigaction only
then. Electron 43.7.0's `PostCreateMainMessageLoop` then installs its own
one-shot handler (`Browser::Quit`, reset to SIG_DFL). Every later
`process.on` joins the existing handle and never fires. Reproduced on fedora
with the packaged Electron and the bundled proper-lockfile: a late listener is
silent, and a vetoed quit plus a second SIGTERM ends in wait status 143.
Removing all listeners closes the handle (libuv resets to SIG_DFL). Re-adding
them opens a new handle under libuv's sigaction, and then every signal
reaches the handlers and the retried quit exits 0. Stock upstream has the same
bug.

`f76493817e35` on the box (installed, serving):

- Restart: `[serve] SIGTERM received; quitting` → `before-quit` ×2 →
  `exiting with code 0`; readiness ~7 s after start; web-index 200; one
  :6768 listener; `NRestarts=0`.
- Daemon pid 2796 (protocol 36) warm-reattached every time ("Preserving
  daemon … owns 11 live sessions"); `terminal list` 11/11 connected. The 13 →
  11 drop between snapshots was two remote-agent shells opened at 18:43–18:56
  and closed again. The 11 terminals from 18:38 survived all three deploys.
- Isolated probe (`scripts/probe-spectre-serve.sh` in the fork: second serve on
  :6769, scratch profile, desktop env injected; the live serve is untouched):
  patch 1 logs `[serve] headless env guard: scrubbed inherited desktop session
  env (DISPLAY=:0, XAUTHORITY=…, DESKTOP_SESSION=xfce, GDMSESSION=…, …)` and
  reaches readiness. `STOP_MODE=double-term` (two SIGTERMs 50 ms apart to the
  Electron main) → two `SIGTERM received` lines, exit 0.
- Patches 5–7 are NOT exercised yet: they are daemon code, and the running
  daemon predates the install and is kept while it owns sessions. They start
  at the next daemon generation (reboot or daemon exit). Check `daemon.log`
  for `startup` + `stale-generation-*` then.

The unit's `UnsetEnvironment=` and `Restart=always` stay in place.

## Findings on the way

- **Journal attribution.** The Electron main moves itself into
  `app-orca-<pid>.scope` ~5 s after start, so readiness and every `[serve]`
  line after that are logged under the scope. Querying only
  `-u orca-serve.service` hides them. Use
  `journalctl --user -u orca-serve.service -u 'app-orca-*.scope'`.
- **`KillMode=mixed` is load-bearing.** Same build, probe stopped under the
  default `control-group` mode: systemd SIGTERMs Chromium's zygote and GPU
  children directly, then `Failed to send GetTerminationStatus message to
  zygote` → `FATAL:…gpu_data_manager_impl_private.cc:416] GPU process isn't
  usable. Goodbye.` → CLI reports `Orca serve exited via SIGILL.`, status 1.
  Under `mixed` it exits 0.
- **The user manager still carries the desktop env.** `systemctl --user
  show-environment` lists 11 session vars (`DISPLAY=:0`, `XAUTHORITY`,
  `DESKTOP_SESSION`, `GDMSESSION`, `XDG_SESSION_*`, …). The unit's
  `UnsetEnvironment=` strips them for the live serve, so patch 1 is the second
  line of defense, not dead code.
- **Unit main process is the CLI wrapper.** `MainPID` is
  `out/cli/index.js serve`, and the Electron main is its child. A signal to the
  wrapper is forwarded once, so signal tests must target the child.
- **2026-09-12 hypothesis (unverified).** Electron on Linux calls
  `ExitOnSessionLoss` → exit code 0 when the display/D-Bus session goes away.
  That fits a clean self-exit with nothing in the journal. With patches 2 and 4
  live, a recurrence now shows whether a signal started it.

## Notes

- Upstream fixed many things in 1.4.199–212, and the deploy upgraded the box
  14 versions in one step. The `--worktree path:<dir>` gotcha was re-tested on
  1.4.212 and **still reproduces**: run from `~/Projects/remote-agent`,
  `terminal create --worktree path:/home/person/wt/release-readiness-spectre`
  returned `ok: true`, `surface: background`, `worktreeId
  bbc15fac-…::/home/person/wt/release-readiness-spectre` (the minecraft repo
  id again, not the cwd's repo), while `worktree ps` lists `00bbf03b-…` for
  that path. The test terminal was closed. AGENTS.md / RUNBOOK §7.8 guidance
  stands. A resolver fix is a candidate patch 9.
- Rollback package: `~/pkgs/orca/orca-ide_1.4.198_amd64.deb` on the box.
- The fork does not touch GUI code paths; every patch is gated on serve mode
  or lives inside the daemon's own log path.
- `terminal list` CLI cost (7.8 s CPU / 180 MB per tick under load, devlog
  2026-09-19) was NOT patched: the control-plane already routes around it
  (`/proc/<pid>/environ` handles, one list per tick max, §7.10), and the cost
  looks dominated by Electron-as-node startup, not daemon RPC. Revisit only
  if a post-deploy profile says otherwise.

## Patch 8: remote terminal stream drops (evening)

Symptom (operator report): a terminal viewed from the Orca ADE client sometimes
streamed slowly, or broke and kept remnants of an older screen on top of the
new one. Not keystroke echo lag.

Code read of 1.4.212 (no incident evidence exists yet, so this is mechanism,
not diagnosis). Output can be dropped at two hops, each followed by a
snapshot meant to repair the viewer:

- daemon → serve: sessions serve marks background get keep-tail thinning
  (`daemon-stream-keep-tail-drop.ts`, 512K chars per session shrinking to 64K,
  2M global); the kept tail may start mid-escape-sequence by design, and a
  `dataGap` replaces the dropped bytes. Logged by patch 7 once the daemon
  generation turns over.
- serve → remote client: per-stream ACK queue over 256 KB drops its oldest
  chunks and sends a recovery snapshot; if no snapshot can be made the stream
  ends `unverifiable`. That snapshot waits up to 8 s for the daemon and then
  falls back to whatever mirror is available, and upstream's own test
  (`bounds a hung authoritative provider acquisition and reuses its fallback`)
  shows the fallback is reused until the hung daemon call settles.

A TUI (Claude Code, qodercli) repaints by moving the cursor and overwriting
changed cells, so a dropped span or a lagging repair image leaves old cells
on screen until the next full redraw. That is consistent with the report but
unproven.

Patch 8 (`e7617156a7`) logs the serve → client hop. Serve installs a main-
process sink (patch 7's sink is daemon-only) that writes `[serve]
stream-backlog <event> <json>` to the journal, one line per event per 30 s:
`remoteAckOverflow`, `remotePendingOverflow`, `remoteRecoverySnapshot`
(source, seq, elapsedMs), `remoteRecoverySnapshotFailed`,
`remoteStreamUnverifiable`, `remoteSnapshotUnavailable`,
`remoteInitialSnapshotTruncated`, `authoritativeSnapshotFallback`
(providerWaitMs, source). Upstream's existing `mainBackgroundSync` entries
land there too. How to read them: RUNBOOK §7.8.

Verification:

- fedora: `make test` 71/71 plus the runtime fragment 2/2; `make typecheck`
  clean; oxlint and oxfmt clean on the changed files. Red check: with the
  source hunks stashed, 3 of the 4 new tests fail (the "stays quiet" case
  passes either way, as it should).
- Pre-existing failures seen on the unpatched parent too, unrelated:
  `configure-process.test.ts` (2 GPU-flag tests, host-dependent) and flaky
  lineage / worktree-scan tests in `orca-runtime.test.ts` (a different set
  each run), which is why `make test` filters the runtime entry to the fork's
  fragment.
- Package `orca-ide_1.4.212_serve-e7617156a7b5_amd64.deb`: all new event
  names present in `app.asar`.
- Spectre deploy 21:33 KST: clean stop (`SIGTERM received` → `before-quit` →
  `exiting with code 0`), daemon pid 2796 preserved with 16 live sessions,
  16/16 terminals listed after, `NRestarts=0`, web-index 200. A throwaway
  terminal (created and closed) produced
  `[serve] stream-backlog mainBackgroundSync {"sessionIdSuffix":"@@00b5e9ab","background":true,"caller":"spawn","known":false,"visible":false}`,
  so the production sink is live.
- Not seen live: the `remote*` and fallback events. Inducing an ACK overflow
  needs a client that pairs over E2EE and speaks the binary multiplex
  protocol; they are covered by unit tests only and will show up on the next
  real overflow.

Finding on the way: a CLI-created terminal starts with thinning **on**
(`background:true`, caller `spawn`). A remote viewer is supposed to flip it
off (`caller:"remote-view"`, `background:false`). When a stale screen next
appears, first check whether that flip was logged for the session; if not,
the daemon was thinning a stream someone was watching. Caveat: the rate limit
is per event name, so a flip within 30 s of another sync shows only as a
`suppressedSinceLastEmit` count (fixed by patch 9 below).

## Patch 9: PTY resize logging (2026-09-27, after midnight)

Why: serve has no renderer process (checked on the box: zygotes, GPU process,
Xvfb `:99` and the network service, no `--type=renderer`), so nothing measures
a pane. `orca-runtime-create-terminal.ts` spawns CLI terminals at a fixed
120×40, and the size changes only when a remote viewer claims its viewport
(`RemoteDesktopTerminalFloor`) and again on the host reclaim when the last
viewer leaves, back to the pre-claim size since serve has no renderer size to
restore. Each change is a full TUI repaint at a new width, and none was
recorded. The other renderer gaps found in the same read (lazy tab mount
waits 10 s for a renderer that does not exist, then `no_connected_pty`; no
renderer serializer as a snapshot source) are recorded here, not patched.

Patch 9 (`9e4c765cc6`): `applyLayout`, the only resize path in serve, logs
`ptyResize` with `from`/`to`, `kind` (`remote-desktop` claim, `desktop`
reclaim, `phone`), `owner` (`multiplex:<connection>:<stream>`, the same
stream id as the patch 8 `remote*` lines), `seq`, and `failed:true` when the
provider refuses. The sink's rate limit now takes an optional key.
`ptyResize` is keyed per kind and session, and `mainBackgroundSync` per
direction and session. The box journal had already shown the need: between the
patch 8 and patch 9 deploys, per-event-name keying folded session flips into
`suppressedSinceLastEmit: 1` and `: 4`. Past 256 keys, closed windows with
nothing suppressed are dropped.

Real data from that window (patch 8 live, 21:33 → 00:34): 7 lines, all
`mainBackgroundSync`. Five `spawn` → `background:true` for new CLI terminals,
and two `remote-view` → `background:false` (`@@d92e03c2` at 21:51,
`@@253b6fb0` at 00:21) when the ADE client (100.64.11.53) opened a terminal.
So the flip-off works when a viewer attaches. No `remote*` overflow line
appeared.

Verification:

- fedora: `make test` 77/77 plus the runtime fragment 2/2; typecheck, oxlint,
  oxfmt clean. The `src/main/ipc/pty` suite plus the layout suites (89 files,
  849 tests) pass. Red check: without the source hunks, 4 of the new resize
  and probe tests fail, and the producer-sync test fails.
- Pre-existing, unrelated: 7 bash-wrapper tests
  (`bash-prompt-command-composition`, `shell-ready-bash-wrapper`) fail on
  fedora's bash 5.3.9 with and without the patch, with identical names. An
  8th in the wide run was a load flake.
- Package `orca-ide_1.4.212_serve-9e4c765cc6b5_amd64.deb`: `ptyResize:` and
  `mainBackgroundSync:` keys present in `app.asar`.
- Spectre deploy 00:34 KST: clean stop (`SIGTERM received` → `exiting with
  code 0`), daemon 2796 preserved with 26 live sessions, 26/26 terminals
  after, `NRestarts=0`, web-index 200, the ADE client reconnected.

### First live ptyResize data: tab switches shrink agent PTYs to 80x24

Within minutes of the operator reconnecting (00:35-00:44 KST), the journal
showed the same pattern on every terminal the ADE client opened and then left:

```
ptyResize {"sessionIdSuffix":"@@3f4d4deb","from":null,"to":"149x55","kind":"remote-desktop","owner":"multiplex:953506ff08500055:5","seq":1}
ptyResize {"sessionIdSuffix":"@@3f4d4deb","from":"149x55","to":"80x24","kind":"desktop","seq":2}
```

Five sessions were claimed to the client's 149x55 and then reclaimed to
**80x24** 1-7 s later, when the viewer moved on. Mechanism, from the code:

- `getTerminalSize` is `ptyController.getSize`, which serve only knows for
  PTYs it spawned itself (`@@Omj_DvqQ`, spawned at 00:38, logged
  `from:"120x40"`). For sessions re-attached from the preserved daemon after
  a serve restart it is `null` (`from:null` above).
- On a first claim, `RemoteDesktopTerminalFloor` records the host reclaim
  target via `resolveDesktopRestoreTarget`: mobile baseline (none) → last
  renderer size (no renderer in serve) → current size (`null`) → the hard
  default `{80,24}`, which upstream's comment says is "reached only under
  bug". In serve it is reached for every re-attached PTY.
- When the last viewer leaves, the floor resizes the PTY to that target.

So every visit to an agent tab costs two full TUI reflows (149 → 80 → 149).
The time at 80 columns also leaves output wrapped at 80 in scrollback. The
80x24 `qodercli` PTYs seen on the box earlier fit this. This is the strongest
candidate so far for the "stale screen remnants" report: Ink-style TUIs erase
their previous frame by line count, and a width change rewraps that frame.
It is not proven to be the cause, but the resize churn itself is now
observed, not inferred.

Candidate patch 10 (not written): in serve there is no host display to give
width back to, so when the last remote viewer leaves, keep the PTY at the
viewer's size instead of reclaiming. Alternative: seed the re-attached
PTY's real size from the daemon so the reclaim target is at least not 80x24.
