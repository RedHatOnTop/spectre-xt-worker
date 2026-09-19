# 2026-09-15 — Grokbot: the goal supervisor that closes the qoder handoff loop

## Report

Yesterday's park watcher (`devlog/2026-09-14-qoder-goal-parks-and-dispatch.md`)
left one piece manual: a parked or finished `/goal` unit was reported to Slack,
and a human read the notice and typed the next goal in `#control`. On a box that
is supposed to run unattended, the human *is* the failure mode — and `ssh
spectre` cannot replace them, because Tailscale SSH in check mode needs a browser
re-approval every 24 h (AGENTS.md rule), so any host-side loop that drives the
box over SSH dies daily.

This adds the piece that was missing: the **goal supervisor** (Grokbot). It runs
on the box, rides the same outbound-only Slack path, reviews with the grok CLI in
headless mode, and dispatches the next goal through the bridge's guard chain.

```
stopped /goal unit
  -> probe      spectre-qoder-goal-watch --probe <worker> --json
  -> review     grok -p <evidence prompt> --output-format json --json-schema
                (read-only: --tools Read + deny floor, no shell, isolated home)
  -> validate   decision + evidence + goal shape — discard on any doubt
  -> dispatch   spectre-slack-bridge --dispatch goal|resume <worker> <text>
  -> report     #lobby as `grok`
```

The reviewer answers a schema-constrained `{decision, goal, rationale,
evidence}`: `resume`, `goal`, `stop`, or `escalate`. It never acts on its own
word — the bridge re-applies registry → probe → tmux pane → cwd before a byte is
typed, exactly as for a human `#control` dispatch.

## What was built

- `scripts/goal-supervisor.py` → `spectre-goal-supervisor` (stdlib). `--scan`
  default; `--probe <worker>` classifies and prints the plan (no model call);
  `--dry-run` plans only (no call, no post, no state); `--worker` narrows a scan.
- `config/goal-supervisor-prompt.md` — the review prompt, with placeholders the
  script refuses to run without.
- `config/grok-supervisor/config.toml` — the reviewer's isolated `GROK_HOME`
  (vendor MCP/config scanning off).
- `scripts/slack-bridge.mjs` — `--dispatch` CLI mode (runs before the config
  check, so it needs no `slack.env`), sharing `dispatchLine` with the `#control`
  path; new audit events `dispatch_dry_run` and `origin`.
- `systemd/goal-supervisor.{service,timer}` — 180 s oneshot, just past the
  watcher's 120 s tick.
- `grok` identity (`:crystal_ball:`) in `config/slack-agents.json`; the notify
  self-test now reports nine agents (tests updated).
- `scripts/bootstrap.sh` / `scripts/doctor.sh` / `RUNBOOK.md` §7.16 / AGENTS.md.

## Safety rails (and why each exists)

- **Kill switch** `SPECTRE_GOAL_SUPERVISOR=1` (unit sets it; installed off — the
  timer is enabled only when grok and its profile exist).
- **Caps**: 8/worker/day, 20/day, 900 s cooldown, $1.00/day — the cost cap is
  read from grok's own `total_cost_usd`.
- **Dedup** by `reason` + `turn_id`; **repeat guard** on the goal fingerprint, so
  the same objective is never typed twice in a row.
- **No evidence, no dispatch**; a malformed decision is settled rather than
  retried (it would just buy the same misbehaviour again) — but a *failed* call
  (402/auth) is deliberately not settled, so it retries when the cause clears.
- **plan_gate is never typed into**: a blind Enter at an ExitPlanMode dialog
  could select an option; the supervisor escalates instead.
- The reviewer's environment is whitelisted from nothing, so no secret in the
  unit environment can reach the model call; the prompt carries evidence as
  text, and the reviewer runs in a neutral cwd so the worker's own `AGENTS.md`
  is never loaded as *its* rules.
## Evidence gathered before writing any of it (daily driver, 2026-09-15)

- `grok -p … --output-format json --json-schema …` returns `structuredOutput`
  (the docs say `structured_output` — the live envelope is camelCase), plus
  `sessionId`, `num_turns`, `stopReason`, `usage` and `total_cost_usd`, so one
  call answers both "what next" and "what did it cost".
- `--effort` menu on grok-4.6 is `xhigh|high|medium|low`; `minimal` is rejected.
- Read-only floor: `--always-approve --tools Read,Grep --deny Write --deny Edit
  --deny Bash` refused a requested write (`wrote_file: false`, no file created) —
  and the model also tried `gateway__filesystem_1mcp_write_file`, an MCP
  meta-tool that `--tools` does **not** restrict. Hence the isolated `GROK_HOME`
  profile as the primary control.
- Cost floor: ~$0.015 and ~22 k input tokens for a one-word answer; $0.024 for a
  4-turn read-only review. The org's grok balance has answered HTTP 402 before
  (`ade-workbench`, 2026-09-05), so quota is classified and alerted.
- The box was not reachable for this work: `ssh spectre` is waiting on the
  Tailscale re-approval (`login.tailscale.com/a/l14eaff7236e4bb`). Everything
  below was verified off-box.

## Verified (off-box, 2026-09-15)

- `python3 -m unittest tests.test_goal_supervisor` — 33 tests: gates, caps,
  decision validation, prompt/argv/env construction, envelope parsing, post
  formatting, log redaction, cost/dispatch accounting, plus an offline
  end-to-end (fake watcher/grok/bridge/notify) that asserts one dispatch per
  stop, dedup on the second tick, the exact bridge argv, alert-once-then-retry
  for a 402, and that a decoy `SLACK_BOT_TOKEN` in the environment never reaches
  the reviewer.
- `node --test tests/slack_bridge.test.mjs` — 42 tests, including the new
  `dispatchLine`/`parseDispatchArgs` cases and a **real tmux pane** dry run:
  `dispatch_dry_run` against a matching pane, `dispatch_pane_refused` when the
  registry cwd does not match, and `dispatch_no_injection_path` for a worker
  with no tmux.
- Real typing, not just a dry run: a `cat > file` pane captured the actual
  `/goal PROBE LINE ONE COMPLETION PROTOCOL …` line (785 chars) after
  `--dispatch goal`, i.e. the `send-keys -l` + Enter round trip. A `bash` pane
  was refused first ("pane runs a shell — the text would execute as a shell
  command"), which is the guard doing its job.
- The supervisor against the daily driver's own qoder sessions: `--dry-run`
  classified all five registry workers (`minecraft` idle with a real segment, the
  rest unknown for lack of local sessions) and planned no action; the kill switch
  printed `disabled` and exited 0.

## Verified with the real model (off-box, 2026-09-15)

A harness (fake watcher + fake bridge + fake notify, the *real* grok CLI headless
with the same flags the box will use) ran the whole review path:

- The reviewer read the evidence and chose `escalate`, not a goal: the harness
  deliberately fed inconsistent evidence (probe said one worker/cwd while the
  session tail belonged to another project, `turn.finished reason=abort`, a dirty
  tree, no completion report). It cited the mismatched turn ids and cwd and told
  the human to reconcile first. Nothing was dispatched — the designed outcome for
  thin/contradictory evidence.
- Cost landed in the state file (`cost_usd: 0.0102`, date stamped) and the report
  went to `#lobby` as `grok`.
- Two real bugs surfaced only because a live run was done:
  1. **`--deny NotebookEdit` aborted every run** (`unsupported tool prefix`,
     exit 1, *before* any model call — so no cost, no decision, and the stop
     stayed unhandled with a `#fleet` alert; the retry path behaved correctly).
     `EnterWorktree` too. Probed the tool vocabulary for free with an
     `--effort bogus` sentinel (also invalid, also pre-call) and rebuilt the deny
     floor from accepted names only. A reviewer deny list that kills the run is
     worse than no deny list — `--tools Read` is the real restriction.
  2. **The cost cap was dead code.** An insertion mishap had left
     `record_cost`'s body truncated to `day = day_key(now)` — the unit tests did
     not notice because the *dispatch* path also booked spend, so the numbers
     looked right. Fixing `record_cost` then exposed double counting
     (`record_cost` + `record_dispatch` both added the same call), which the
     suite caught as a failure. Spend is now booked exactly once, in
     `record_cost`, with a test pinning it.

The lesson is the repo's own rule: an off-box suite can be green while the thing
is broken. The live run cost ~$0.03 and found both.

## The credential question: browser login is the default, key is the alternative

The reviewer needs *a credential*, not a human login — and the chosen default is
the **browser** one, because a device-code approval is a one-time cost with no
key to manage:

- **Default (shipped profile): device-code login.** `grok login
  --device-code|--device-auth` prints a URL and a code; approve once from any
  browser (phone is fine — the box needs no browser and no display). grok writes
  `auth.json` in the isolated home (0600) and refreshes the token itself. One
  approval, not a daily one — unlike Tailscale's 24 h re-approval, which is
  exactly why the supervisor lives on the box. The profile therefore ships **no
  `[auth]` pin**, so the session token is used as-is.
- **Alternative: `XAI_API_KEY` (no login at all).** Documented as the no-browser
  path, and it took **two steps** to make it true on grok 1.0.30 (both probed,
  both free — they fail before any billable call):
  1. The key alone is not enough in an isolated home: with a well-formed
     `XAI_API_KEY` and no `auth.json`, the CLI still exits 1 with "Not signed in
     … Alternatively, set the XAI_API_KEY environment variable" — the fallback
     the docs describe was not taken.
  2. Uncommenting `[auth] preferred_method = "api_key"` in the profile made grok
     *use* the key: a shape-valid dummy reached the API and the server answered
     `400 Bad Request … Incorrect API key provided`, which is proof the
     credential traveled.

  The key path stays in the repo as a commented pin plus
  `~/.config/remote-agent/grok.env` (0600, never committed, names only, example in
  `config/grok.env.example`). The supervisor takes **only** `XAI_API_KEY`/
  `GROK_CODE_XAI_API_KEY` out of that file and passes it into the reviewer's
  whitelisted environment — never the whole file, never the unit's environment,
  never a log or a Slack post (all three are pinned by tests). It is a pin with
  no fallthrough, so the box must not also hold a session token;
  `spectre-doctor` warns for a key without the pin, a token with the pin, and
  both present.
- Copying `auth.json` from another machine is the option to avoid — the grok docs
  say not to move that file, and a rotation elsewhere kills the box.

What happens with no credential at all (probed 2026-09-15, free — it fails before
any model call): `Error: Not signed in. To authenticate without a browser, run:
grok login --device-code …`, classified `auth_failed` → one `#fleet` alert per
hour, the stop stays unhandled (not settled), and the loop resumes by itself once
a credential appears. Nothing is lost and nothing is retried in a tight loop.

Also probed, because it would have broken the deploy: an empty `GROK_HOME`
*self-initializes* on the first grok run and writes its own 2-line stub
`config.toml`, which would have shadowed the reviewer profile that
`bootstrap.sh` used to install "only if absent". The install is now
marker-based (`^disabled_mcp_servers`), with a timestamped backup of whatever it
replaces, and `spectre-doctor` reports a profile that went missing.

## Not verified (needs the box)

grok installed and logged in on the Spectre (`grok login --device-auth` with the
isolated `GROK_HOME`); a live review call from the box (network, quota, and the
`--json-schema` round trip on that CPU); `GROK_HOME` isolation actually hiding
the box's vendor config; a real park → review → dispatch cycle; and the
"goal complete" trigger — `qoder-goal-watch` still does not classify a completion
record (the supervisor already accepts `goal_complete`/`goal_done`/
`update_goal_complete`, so the watcher can start emitting them next). RUNBOOK
§7.16 lists the exact commands.
