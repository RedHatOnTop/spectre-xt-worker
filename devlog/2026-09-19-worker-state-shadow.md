# 2026-09-19 — Authoritative worker-state resolver (shadow → cutover)

## Why

Slack `/goal`, Grokbot, and the qoder goal watcher each classified
active/idle from the last session-jsonl record. A `model.request.started`
older than 600 s looked idle while work continued. The design
(`Spectre Authoritative Worker-State System`) puts one resolver behind a
Unix-socket JSON API.

## What landed (this slice)

Phase A + qoder jsonl adapter + shadow. No consumer cutover.

- `scripts/worker_state/` — journal, resolver, policy, UDS HTTP, CLI
- `scripts/spectre-state.py` — `serve` / `get` / `health` / `reconcile` / `ingest`
- `systemd/spectre-worker-state.service`
- Design holes closed in code: API-down UNKNOWN is fail-closed; an unseen
  worker is IDLE (first `/goal` allowed); PARKED carries `park_reason`
  (`goal_budget` vs `plan_gate`); pane/cwd targeting stays in the bridge.

## Cutover on the box (2026-09-19 20:24 KST)

Live probes before cutover: qoder/zzbrush active; pugc idle by last-record
(`hook.finished` 2.6 h) while tmux still ran `qodercli` — the false idle
this system exists to stop. Shadow logged `old_false_idle` for pugc, zero
`dangerous_false_idle`. Daemon RSS ~29 MB on a load-26 box.

Then Slack `--dispatch` and `qoder-goal-watch --probe` were pointed at
`snapshot.policy`. Dry-run: resume pugc refused (RUNNING), goal qoder
refused (RUNNING), goal minecraft allowed (IDLE, pinned orca terminal).
`qoder-nudge.timer` (TUI pane scrape) disabled; `spectre-continuity.timer`
enabled (resume only when `continuity_recovery_allowed`).

## Verify

`bash verify.sh` on this host: 235 python tests, slack bridge tests green.
On Spectre: `systemctl --user is-active spectre-worker-state.service`
and `slack-bridge.service`; `spectre-state health`; dry-run dispatch as
above. Grokbot binary installed, timer still off.

## Completion: the units, not just the binaries (2026-09-19 21:40 KST)

The first cutover installed the new binaries but left the systemd units pointing
at the older flat deploy tree (`~/remote-agent-deploy/remote-agent/`), and it
enabled `spectre-continuity.timer` without starting it (enabled + dead, so
nothing ran). The box therefore still classified occupancy in four places:

- `qoder-goal-watch.service` ran the deploy-dir watcher (session-jsonl probe)
- `qoder-continuity.timer` ran the idle-budget failover and was still firing
  `resume` at workers the resolver reports as RUNNING
- `codex-goal-healer.timer` scraped `orca-ide` pane text; every scan failed on a
  20 s `terminal list` timeout (CPU 7.8 s, peak 180 MB per 60 s tick)
- `grokbot-goal-event.timer` classified `UpdateGoal`/`goal_complete` from
  session jsonl and published independently — the §21 guard violation

Done: installed this repo's `qoder-goal-watch.{service,timer}` (unit now runs
`/usr/local/bin/spectre-qoder-goal-watch --scan`), started
`spectre-continuity.timer`, and disabled `qoder-continuity`, `qoder-nudge`,
`codex-goal-healer` and `grokbot-goal-event`. Unit files are backed up under
`~/.config/systemd/user/legacy-backup-20260919T213903/` and `…T214033/`; the
deploy tree itself was not touched. `bootstrap.sh` now installs the goal-watch
units and disables the four legacy timers, so a rebuild cannot reintroduce them.

Verified on Spectre: `spectre-state health` → `ok=true`, 5 workers, WAL;
`spectre-qoder-goal-watch --scan --dry-run` → positions + `actions: []`;
`spectre-continuity --dry-run` → all five workers `skip, reason=policy`
(pugc/zzbrush RUNNING). Still unverified: reboot persistence, and the first real
continuity resume when a genuine stall appears. Grokbot remains installed-off
by decision; the old publisher is gone, so the box now has no Grokbot action
path at all.

## The cutover gate was not actually green: resolver 1.0.1

The §23 gate says cutover needs a live shadow with no dangerous false positive.
The first cutover read only the top of the log and asserted "zero
`dangerous_false_idle`". Re-reading the whole file found **21** of them, all
`zzbrush` (20) and `qoder` (1), the last one minutes before the cutover — the
gate was passed on a false claim.

Cause, from the journal and the raw segment:

- The adapter mapped `session.phase.finished` to `turn.ended`. Every observed
  `session.phase.finished` carries `phase: "input.attachments.collect"` — a
  sub-phase *inside* a live `/goal` loop, not a turn boundary. The real stream is
  `loop.iteration.started → model.request.started → tool.requested →
  hook.finished → session.phase.finished → loop.iteration.finished → …`
- So the resolver idled a worker mid-loop. For ~10–15 s, `can_dispatch_goal`
  was true while the goal loop was still running — the dangerous direction
  (`old_state=active` from the probe, `new_occupancy=idle`). The old classifier
  was slow, not wrong. `turn.finished` is the actual boundary and was already
  mapped.

Two fixes (`RESOLVER_VERSION` 1.0.0 → 1.0.1, deployed to Spectre at 13:24 UTC):

1. `session.phase.finished` maps to nothing. A sub-phase is not evidence about
   goal lifecycle.
2. `hook.finished` no longer raises IDLE to RUNNING (removed from
   `RUNNING_HINT_KINDS`). A hook is corroboration, not a rising edge (I3); it
   still updates structured-evidence health. Without this, fix 1 would have left
   `korea-metro-twin` RUNNING forever on a 13 h old `PreToolUse` hook whose
   session has no `turn.finished` at all, refusing every later `/goal`.

After the fix: qoder/minecraft/korea-metro-twin IDLE (dispatch allowed),
pugc/zzbrush RUNNING (refused), and the zzbrush loop no longer produces a
dangerous row. Note the journal is append-only, so the 21 mis-mapped
`turn.ended` rows already ingested stay; later evidence supersedes them and the
resolved state is correct, but a replay is not byte-identical to a journal built
with the fixed adapter.
