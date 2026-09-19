# 2026-09-19 — Authoritative worker-state resolver (shadow)

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
