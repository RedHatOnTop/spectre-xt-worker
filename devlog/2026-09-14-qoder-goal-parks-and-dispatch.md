# 2026-09-14 — Why qoder goals freeze silently, and the report-then-dispatch handoff

## Report

orca-served qodercli sessions on the Spectre stop mid-goal: the session
stays alive, the goal stops, no error appears anywhere, and only a manual
`/goal resume` in the TUI restarts it. Observed repeatedly over 3 days.

## Root cause, from the session transcripts

The only durable trace is the session jsonl
(`~/.qoder/logs/sessions/<cwd slashes→dashes>/<session>/segments/<ts>-p<pid>.jsonl`).
Two distinct park mechanisms, both silent:

1. **goal_budget.** `/goal` created without `--turns` defaults to 1000
   loop iterations. When it exhausts, the turn ends
   `turn.finished reason=max_turns num_turns=1000` and the goal is
   force-paused ("only the user can grant more turns"). Measured rate:
   ~2.3 iterations/min active → 1000 iterations ≈ 5-9 h of work. The
   counter really was rising slowly — the budget was genuinely exhausted.
   Evidence: 6 `max_turns` events ↔ 6 manual `/goal resume` inputs in
   exactly two workers (zzbrush, minecraft) over 09-11 → 09-14, the
   longest gap 18 h ending with `input.prompt.received "/goal resume"`
   `query_source:"tui"`.
2. **plan_gate.** The model called `ExitPlanMode` and the CLI waits on
   the approval prompt; no record is written while it waits. Observed
   6.1 h: `hook.finished PreToolUse:ExitPlanMode` 02:36:29 →
   `permission.resolved source:"user", allowed:true` 08:41:35. No
   `/goal resume` clears this — only a TUI answer.

Why nothing else saw it: these parks are TUI-state only. No log line, no
Slack notice, no service state change. Hence the fix below reads the same
jsonl the evidence came from.

## Decision (user's proposal)

Retire the standing "Do not mark the goal as complete" instruction — it
keeps a session hostage to a human. Instead: a worker finishes its unit,
posts a completion report to `#lobby` (what changed, commit hashes,
verification result, suggested next goal), *then* calls
`update_goal(status="complete")`. Whoever reads the report — the user,
ChatGPT via the Slack connector, another qoder session — sets the next
goal by dispatch. Finishing and reporting **is** the handoff.

## What was built

- `config/qoder-goal-clause.md` — the completion-protocol clause appended
  to every dispatched goal.
- `config/qoder-workers.json` — worker registry (name → `cwd` + `tmux`;
  `tmux: null` marks orca native terminals with no injection path:
  zzbrush, minecraft, korea-metro-twin; tmux workers: qoder, pugc).
- `scripts/qoder-goal-watch.py` — park detector + Slack notifier.
  `--scan` posts one `#fleet` notice per park event (dedup key = reason +
  turn_id, persisted) and one recovery line on movement; `--probe NAME
  --json` is the same classifier for the dispatcher. Stdlib only; notify
  goes through `spectre-slack-notify`.
- `systemd/qoder-goal-watch.{service,timer}` — 120 s oneshot scan.
- `scripts/slack-bridge.mjs` — `#control` builtins `goal <worker> <text>`
  and `resume <worker>`. Guard chain before anything is typed: registry
  lookup, watcher probe (`active` and `plan_gate` are refused — blind
  Enter at a permission dialog could select an option), then a tmux pane
  check (refuse if the pane runs a shell or its cwd is not the registry
  cwd). Injection is one `send-keys -l` literal (newlines flattened,
  4000-char cap) plus a separate Enter. Dispatch does not consume the
  qoder-run budget; no model call is made.
- Tests: `tests/test_qoder_goal_watch.py` (35), bridge additions for
  `classifyCommand`/`dispatchAllowed`/`paneRefusal`/`paneTarget`/`oneLine`.
  Full gate green: 95 python + 36 node tests, `bash verify.sh` all pass.

## Verified on the box (offline, read-only)

Dry-run scan against live session data classified all five workers as
expected — minecraft parked/goal_budget (num_turns 1000, age ~34 h),
zzbrush parked/goal_budget (~4.3 h), korea-metro-twin active, pugc and
qoder idle — and emitted exactly the two `notify_park` actions. That run
created no state and posted nothing.

## Deployed and verified on the box (2026-09-14 19:49–19:52 KST)

- Installed per RUNBOOK §7.10: `spectre-qoder-goal-watch`, the registry
  and clause under `/usr/local/share/remote-agent/`, the timer units, and
  the dispatch-capable bridge (previous build kept at
  `/usr/local/bin/spectre-slack-bridge.2026-09-12.bak`).
- Watcher first tick delivered the three live park notices to #fleet:
  zzbrush, minecraft, and korea-metro-twin, which hit its own
  goal_budget at 19:24 that evening — a fresh live example of the
  reported failure, and being orca-native its notice points at the orca
  UI. All `delivered=True` with Slack ts; the next tick was silent
  (state-file dedup).
- Probes and dry-run re-ran on the installed binary; positions carry a
  `tmux` flag that shapes the resume hint.
- Pre-dispatch verification caught a real bug: a bare registry name is an
  ambiguous tmux target — `-t qoder` resolved to the **pugc** pane (its
  window is named `qodercli` and tmux prefix-matched the window name in
  the current session). The cwd guard would have refused it safely, but
  the qoder worker would have been unreachable; fixed with `paneTarget()`
  (bare name → `name:`), tested, redeployed.
- Registry `tmux` values stay hand-maintained; drift is caught at
  dispatch time by the pane/cwd guard, not before.
- Still unverified: the end-to-end `#control` dispatch itself (takes a
  human Slack message), and whether `/goal` accepts the full one-line
  description with the clause appended.

## Follow-ups

1. Run the first real `#control` dispatch (`goal pugc <text>`) — it is
   the last unverified line in RUNBOOK §7.10 and also answers the
   long-one-line `/goal` question.
2. A third park shape is visible in the data but deliberately not in v1:
   a `model.request.started` record with no matching completion (possible
   upstream hang). Too easy to false-positive on a slow model call — it
   needs its own probe before it ships.
3. Orca native-terminal workers (3 of 5) remain UI-only for dispatch; if
   orca grows a CLI keystroke path, wire it into the same guard chain.
