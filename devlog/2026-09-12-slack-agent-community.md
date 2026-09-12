# 2026-09-12 — Slack agent community (workspace + bridge + ChatGPT intervention)

## Why

The box runs agents (Orca serve :6768, ZCode, claude CLI) but had no
shared surface where they report status and where the operator can act.
Alerting existed but was inert: `send_ntfy` in `scripts/healthcheck.py`
plus an empty `NTFY_TOPIC` meant today's orca-serve outage ran ~8 h
undetected. The request: give the agents a Slack "community" — not just
notifications, but a place where agents share issues and claude works
them in-thread, watchable — and tie ChatGPT in so the operator can
intervene when something breaks.

## What was built

Direction is outbound-only from the box (Socket Mode websocket; no
tunnel, no inbound firewall change).

- `scripts/slack-notify.py` -> `spectre-slack-notify`: stdlib-only CLI,
  posts via chat.postMessage with per-agent username/icon
  (`chat:write.customize`). Reads tokens from the 0600 env file at use
  time — no secret ever sits in a process environment. Unconfigured it
  prints `slack-notify: disabled` and exits 0; `--self-test` does
  shape-only checks and never prints token values.
- `scripts/slack-bridge.mjs` -> `slack-bridge.service` (systemd user
  unit, `Restart=always`): Socket Mode listener on Node's global
  WebSocket (zero dependencies). Per-channel policy:
  `#control` = allowlisted member @mention -> builtin or read-only
  qoder run; `#lobby` = registered agent issue -> qoder discussion
  reply in-thread, single-hop; `#alerts` = healthcheck alert ->
  optional triage (`SLACK_TRIAGE=1`); `#fleet` = posts only.
- `scripts/slack-brief.py` -> `spectre-slack-brief` + timer: daily
  09:00 deterministic brief to `#lobby` (no LLM).
- `config/slack-agents.json`: 7 identities (bridge, healthcheck, orca,
  zcode, claude, qoder, spectre).
- Executor: **qodercli, Efficient model**, via the box-local
  `qoder-efficient` wrapper (its `allow-start` cost gate refuses with
  exit 75 when the promo is no longer free; the bridge audits that as
  `cost_gate_refused`). Not claude — see the swap section below.
- Executor profile `config/slack-executor-settings.example.json`:
  allowlist (Read/Glob + narrow read-only Bash, no curl; Grep denied
  wholesale — its pattern semantics against secret files are
  unverifiable and `Grep(pattern)` returns matching lines, a token
  leak channel), secret read deny-list (`slack.env`, `health.env`,
  `env.json`, `http-token`, keys, `/proc/*/environ`,
  `/work/hardened-zai-proxy/**`); phase 1 is read-only. **No hooks
  section** — probed: qodercli 1.1.47 does not execute PreToolUse
  hooks delivered through `--settings`.
- `scripts/healthcheck.py`: new `probe_orca` (default on —
  `REQUIRE_ORCA=1`) and optional `send_slack` backend alongside ntfy.
- `scripts/doctor.sh`, `scripts/bootstrap.sh` (fresh installs only),
  `verify.sh` node section, `.gitignore` (`slack.env`), RUNBOOK §7.10,
  AGENTS.md rule.

Loop/cost guards: single-hop by construction, fail-closed on unknown
identity, dedupe by `event_id` + `(channel, ts)`, stale > 300 s
dropped, rate limit 6/10 min per user, per-thread 4 runs/h + daily cap
30 (persisted across restarts), one executor run at a time, 300 s
SIGKILL timeout, and the wrapper's cost gate (exit 75) as the dollar
bound — the Efficient model is 0-multiplier, so there is no per-run
dollar cap to spend.

### Executor swap: claude -> qodercli Efficient (same day)

Claude's upstream auth died mid-build ("Failed to authenticate: OAuth
session expired and could not be refreshed"), so the executor was
swapped to qodercli running the Efficient model — 0-multiplier, and
the box already has the `qoder-efficient` wrapper whose guard
hard-stops qodercli when the promo price moves. Consequences,
probed on the box against qodercli 1.1.47:

- The responder identity in Slack is `qoder` (human prefix in
  `#lobby` is `qoder:`), matching what actually answers.
- `--max-budget-usd` does not exist in qodercli; the cost bound is
  the wrapper's `allow-start` gate (exit 75 = refused, audited).
- Flag-settings PreToolUse hooks are **not executed** (a trivial
  logging hook never ran), so the `irreversible-guard` hook is not
  part of this boundary. The executable boundary is the permission
  engine (allowlist + deny list), later probed in detail: a tool-name
  deny removes the tool from the executor's tool set entirely; Bash
  compound commands are split into segments and each segment is checked
  (see the fourth pass below).
- The bridge runs the executor with `cwd "/"` (every box path is
  read-visible to the allowlist) and parses the **last** JSON line of
  stdout — the wrapper prints its `allow-start` log line before the
  envelope.
- `slack-claude-settings.json` on the box was retired
  (`mv` -> `.retired`) in the same deploy; the bridge reads
  `slack-executor-settings.json`.

## Local verification (fedora, before deploy)

- `bash verify.sh` -> `verify: all gates passed` — shell `bash -n` on
  all scripts, shellcheck SKIP (not installed on fedora), py_compile,
  **Ran 50 tests … OK** (unittest), `node --check` + 19 node:test cases
  (`ok slack bridge tests`).
- Manual passes: `spectre-slack-notify --dry-run` / `--self-test` /
  disabled-path / unknown-agent error path; `spectre-slack-brief
  --dry-run` renders.
- Adversarial review pass (typescript-reviewer on the bridge,
  security-reviewer on the whole diff) before commit; fixed what it
  found: the `#lobby` human path now requires `SLACK_ALLOWED_USERS`
  (previously any workspace member could burn executor budget), the
  secret deny-list extensions above, `Grep` denied, hello/connect
  watchdog (half-open socket no longer sits "active but deaf"),
  settled-guard on replaced sockets, state-file shape validation,
  visible rate-limit/budget notices, SIGKILL to the executor's process
  group, post confirmation required from `spectre-slack-notify`
  (its disabled exit-0 no longer passes as success), clock-step
  clamps, `null`-payload guard, bounded stderr tail in failure posts,
  realpath `isMain`, doctor `claude`-presence check, bootstrap no
  longer clobbers locally hardened executor settings.
- Second review round on those fixes (same two reviewers) added:
  agent identity pinned to the app's own `bot_id` — without it a
  webhook or second app posting with username `orca`/`healthcheck`
  could impersonate an agent and reach the executor; executor deny
  list extended to `*.env`/`*token*`/`.qoder/**`/`.codexpro/**`/
  `hosts.yml` (Read is wholesale-allowed, so the deny list is the only
  file gate); executor child PATH pinned to
  `/usr/local/bin:/usr/bin:/bin`, never inherited (the guard hook
  resolves `node` through it); `--env-file` forwarded to
  `spectre-slack-notify`; failure posts show the newest stderr tail;
  the RUNBOOK deploy block no longer clobbers live settings on
  redeploy.
- Third pass (review of the fix commit): `~/.zcode/**` added to the
  deny floor — `v2/config.json` carries provider keys and
  `bot-state.v2.json` will hold the Telegram bot token (RUNBOOK 343,
  367); executor settings on redeploy now get the repo's deny list
  unioned in via jq (the no-clobber guard alone would have stranded
  new deny entries on an installed box); doctor's claude check now
  uses the bridge's pinned PATH instead of a login shell, so a
  `~/.local/bin`-only claude cannot mask a broken executor.

## On-box verification (2026-09-12, after the targeted deploy)

Deploy was targeted only (bridge/brief/doctor binaries, agents registry,
`slack-executor-settings.json`, units); `slack-claude-settings.json` and
`slack-guard.mjs` were retired in place as `.retired` (mv, not rm).

- Bridge up: `slack-bridge: starting (7 agents, triage=off)` ->
  `auth.test ok user=U0C1ARX2DGE bot=B0C16HEL6BV` -> `slack-bridge:
  socket connected`. Kill test: `systemctl --user kill -s SIGKILL` ->
  "Scheduled restart job, restart counter is at 1" -> active again.
- Executor under the exact bridge child env (`env -i`, minimal PATH,
  `cwd /`, SPECTRE_WORKER_PROFILE=box): `PROBE_OK`, exit 0,
  `total_cost_usd: 0`, `modelUsage ["efficient"]`. The wrapper's
  allow-start line lands on stdout before the envelope
  (`2026-09-12T07:00:01Z allow-start status=free price_factor=0.0
  source:pid:574580`); the bridge's last-JSON-line parser absorbs it.
- Write probe: `DENIED`, num_turns=2 (the write was attempted and
  refused), `/tmp/slack-probe.txt` absent.
- Secret-read probe (forced attempt): `DENIED`, num_turns=2, no `xoxb-`
  anywhere in stdout. Note: qodercli 1.1.47 leaves
  `permission_denials` empty even when a tool is refused — the turn
  count is the evidence, not that array.
- Cost gate: `qoder-efficient-guard check` -> `status=free
  price_factor=0.0`, exit 0.
- `spectre-slack-notify --self-test` -> `OK (4 channels, 7 agents)`.
- `spectre-slack-brief --dry-run` renders (health green, 24h log, box
  block).
- `spectre-doctor` -> `summary: 34 passed, 0 failed`, including the new
  executor gates (`qoder-efficient wrapper present`, `executor settings
  installed`).
- **Blocked on user action**: the bot is in no channel yet —
  `error=not_in_channel` on all four. `/invite @spectreagents` in
  `#alerts`, `#fleet`, `#control`, `#lobby`. Until then no
  `message.channels` events reach the bridge, so the #lobby discussion,
  #alerts triage, and #control reply legs stay unverified (PENDING).
- ChatGPT-Plus connector test (step D): PENDING (user).

### Review round on the executor swap

Adversarial review (typescript-reviewer on the bridge delta,
security-reviewer on the settings/deploy delta) against the swap
commit; findings fixed and redeployed the same day:

- `parseExecutorResult` accepted *any* JSON object as the envelope — a
  stray JSON log line could be read as a result. Now the last
  stdout JSON object must carry a `result` or `is_error` key
  (`num_turns` alone is not enough).
- `expandHome` only handled a bare `~/` prefix; now `~` at end of
  string and `~/…` are both expanded (`SLACK_EXECUTOR_BIN=~` no longer
  silently resolves relative).
- `validateConfig` never checked `executorBin`; it now requires an
  absolute path (a relative bin would resolve against `cwd /`).
- Deny list extended with backup variants and adjacent secrets:
  `**/.env.*` (`.env.bak` etc.), `**/.gemini/**`, `**/.claude.json`,
  `**/.bash_history`; `/proc/*/environ` widened to `Read(/proc/**)`.
- `Bash(journalctl:*)` was an allow entry — a wildcard that bypassed
  the file deny list (journalctl can read anything journald writes).
  Removed from allow, added to deny. `pgrep:*` removed and
  `df:*`/`free:*` tightened to `df -h:*`/`free -h`.
- `bootstrap.sh` merged only the *deny* list, so allow-list
  tightenings never landed on an installed box. The merge is now
  `$new`-based: allow follows the repo, deny is unioned with whatever
  is on the box.
- `doctor.sh` now also checks the cost-gate snapshot
  (`qoder-efficient-rate.json` → `is_free == true`, WARN otherwise).
- RUNBOOK honesty fixes: the executor is *not* "allowlist-only" for
  reads (Read/Glob are wholesale-allowed; the deny list is the file
  gate) and §7.10 now carries a real `Grep` probe (the "Grep is
  denied" claim previously had no probe re-checking it).

Post-fix redeploy (targeted `install`, then `daemon-reload` + restart):
the settings refresh landed as designed — allow replaced (journalctl,
pgrep gone; `df -h:*`, `free -h`), deny **22 → 30** entries. Re-probes
under the bridge child env:

- Secret-read probe (forced attempt, tightened settings):
  `{"num_turns":2,"result":"DENIED"}`, no token in stdout.
- The Grep probe returned `{"num_turns":1,"result":"DENIED"}` — which,
  on the fourth pass below, turned out to be *no evidence at all*: the
  engine never saw a tool call.

Local `bash verify.sh` green after the fixes (50 python + 20 node
tests).

### Fourth pass: tool-set audit — the subagent write hole

The follow-up review flagged that the Grep probe proved nothing
(`num_turns=1` = the model declined without attempting) and that
"Read/Glob filtered by the deny list" was asserted without a probe.
Went after the whole tool surface instead of patching the two claims:

- `--output-format stream-json` emits an init message with the full
  tool list (plain stream-json; `--verbose` is not a known option).
  1.1.47 ships **28 tools**; there is **no Grep tool** — the deny entry
  is a floor only, and the xoxb- probe can never show an attempted call
  (its real check is "no token in stdout").
- Per-tool probing of the unlisted tools found a **real hole**: the
  `Agent` tool spawned a subagent whose Bash *write* gate did not hold
  — `touch /tmp/p4-agent.txt` succeeded there while the same write is
  denied headless in the main thread (read denials and the Bash
  allowlist did hold inside the subagent). `CronCreate` also ran
  (session-only, no file). `WebFetch`/`WebSearch`/`ImageSearch`/
  `ImageGen`/`Monitor` were refused.
- Bash semantics re-probed properly: the engine splits `;`/`&&`/`|`
  into segments and checks each — `uptime; uptime` and `uptime | wc -l`
  **ran** (both segments allowlisted), `uptime && journalctl …` and
  `uptime && touch …` were denied whole (no file), `echo $(uptime)`
  denied. The earlier "compound commands are denied" line was wrong as
  written and is corrected here.
- Glob *is* gated by the deny list (Glob over a deny-listed directory
  refused; over `/tmp` allowed) — so the original claim holds, now with
  a probe.

Fixes deployed the same day:

- Tool-name denies added to the executor profile: escalation (`Agent`,
  `Workflow`, `CronCreate/List/Delete`, `ScheduleWakeup`,
  `EnterWorktree`, `ExitWorktree`), writes (`Edit`, `Write`,
  `NotebookEdit`), egress (`WebFetch`, `WebSearch`, `ImageSearch`,
  `ImageGen`, `Monitor`). Probed mechanism: a tool-name deny **removes
  the tool from the tool set** — live dump went **28 → 12** (Bash,
  Read, Glob, Skill, Task*/Goal metadata). That was the verified fix
  for the subagent hole (`Agent` gone = no subagent exists to write).
- Deny list also gained `.netrc`, `.git-credentials`, `.aws/**`,
  `.zsh_history*`, `.bash_history*` (was exact-match).
- `parseExecutorResult` now also requires `type:"result"` (verified
  present in the 1.1.47 envelope) — a *keyed* JSON log line can no
  longer mask the real result; `expandHome` uses a function replacer
  (a `$&` in a home path cannot be mangled); `bootstrap.sh`/RUNBOOK
  deploy merge now writes tmp + `mv` (atomic — a truncated live
  settings file would strand the deny floor) and warns when jq is
  missing; new `tests/test_slack_executor_settings.py` guards the
  profile against wildcard-allows and missing denies.
- Post-fix probes on the box: subagent `DENIED`, no file; write
  `DENIED`, no file; egress `DENIED`; `uptime` (positive) ran; secret
  read `DENIED`, no token; live settings allow 13 / deny 51.

## Honest limits

- The executor boundary is a permission engine (allowlist + tool/path
  deny lists) on qodercli 1.1.47 — semantics that are not contractual.
  Re-run the full probe list in RUNBOOK §7.10 (write, secret, subagent,
  egress, compound, tool-set dump) after every qodercli upgrade: the
  subagent hole was exactly the class of thing a version bump can
  reopen, and a *new* tool in the set is invisible to the current
  probes. Same-user isolation is imperfect; the executor runs as the
  box user.
- Flag-settings hooks do not execute (probed), so there is no
  irreversible-guard backstop on this path. With the tool-name denies,
  the executor's tool set is Bash/Read/Glob/Skill + Task/Goal
  metadata; Bash is allowlist-only and file paths are deny-filtered.
  Anything that widens the allowlist or the tool set also widens this.
- Bot-post event shape (does `message.channels` carry username/subtype
  for customized posts?) is unverified until the first real post; the
  policy fails closed if identity is unknown, so it cannot loop — but
  `#lobby` automation stays silent in that case until fixed.
- The Efficient promo is a 0-multiplier *promo*: the wrapper refuses
  (exit 75) when the price changes, but a change between the price
  scan and the run is not defendable — check the guard's state file
  if runs start failing.
- Anything holding the user's Slack account can drive the read-only
  executor — intentional and bounded.
- One executor job at a time on a 2C/4T box; no boot-time services
  added.

## Follow-ups

Write-enabled executor phase 2, thread->session continuity, Slack
interactive buttons, multi-hop threads (v1 is single-hop — humans
continue them), Orca/zcode as autonomous responders (interactive apps —
they post via the CLI, they do not answer), mirroring box-only
`qoder-efficient-guard`/`qoder-nudge` into the repo, removing the
now-unused claude identity from the registry if claude never comes
back.
