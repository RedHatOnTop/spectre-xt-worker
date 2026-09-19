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
- `systemd/spectre-worker-state.service` (RUNBOOK §7.6 on this branch)
- Design holes closed in code: API-down UNKNOWN is fail-closed; an unseen
  worker is IDLE (first `/goal` allowed); PARKED carries `park_reason`
  (`goal_budget` vs `plan_gate`); pane/cwd targeting stays in the bridge.

## Not in this slice

Slack `dispatchAllowed`, Grokbot, qoder-nudge, codex-goal-healer. Those
still call the old probe. Architecture guard allowlists them until cutover.

## Verify

`python3 -m unittest discover -s tests -p 'test_worker_state*.py'` and
`bash verify.sh` on this host. Spectre enablement is unverified until
`systemctl --user enable --now spectre-worker-state.service` is run on
the box and `spectre-state health` returns ok.
