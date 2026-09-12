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
  engine + the narrow read-only allowlist: writes always require
  confirmation and are denied headless; compound commands (`;`) and
  command substitution (`$()`) are denied; the deny list lands as a
  flagSettings rule (slack.env read denied).
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

## On-box verification (PENDING — filled at deploy)

- ChatGPT-Plus connector test (step D): _pending_.
- Executor boundary probe: _pending_ (expect `DENIED`, no
  `/tmp/slack-probe.txt`).
- Secret-read probe (`~/.config/remote-agent/slack.env`): _pending_
  (expect `DENIED`).
- `auth.test` ok / socket connected / one reconnect cycle: _pending_.
- orca stop -> Slack alert -> restart clears the streak: _pending_.
- First `#lobby` agent discussion + first `#alerts` triage thread:
  _pending_.

## Honest limits

- The executor boundary is a permission engine plus a narrow read-only
  allowlist on qodercli 1.1.47 — semantics that are not contractual.
  Re-run the write/secret probes in RUNBOOK §7.10 after every qodercli
  upgrade. Same-user isolation is imperfect; the executor runs as the
  box user.
- Flag-settings hooks do not execute (probed), so there is no
  irreversible-guard backstop on this path — the read-only allowlist
  is the whole Bash boundary. Anything that widens the allowlist also
  widens this.
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
