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
  source=pid:574580`); the bridge's last-JSON-line parser absorbs it.
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
  installed`) — 35 in the post-invite run below, once every slack gate
  is installed and active.
- **Resolved (user, same day):** the bot is invited in all four channels
  (the `not_in_channel` block is gone), and the ChatGPT connector test
  works on the user's Plus plan. The connector path (official Slack app
  connector vs custom `mcp.slack.com` endpoint) is to be recorded on the
  next check.

### End-to-end verification (2026-09-12, after invite + connector test)

Real threads, real executor runs (guard fast path restored, see the
PrivateTmp section below):

- **#lobby discussion**: `spectre-slack-notify --agent orca --channel
  lobby` post -> audit `job_queued/job_start kind=discussion`
  08:44:54Z, `job_done chars=340` 08:45:41Z. Thread, via
  `conversations.replies`: `orca` issue -> `bridge` ack
  (`:hourglass_flowing_sand: qoder is reading the thread`) -> `qoder`
  reply (340 chars) that actually read the health log: "three memory
  pressure events today around 02:04-02:12 UTC — mem_avail dropped to
  778M, then 645M, then 424M…suggests OOM or systemd memory limits".
  The bridge's own ack/reply were ignored (`lobby_self`) — loop guard
  holds on live events.
- **#alerts triage**: `systemctl --user stop orca-serve.service` ->
  health log `NOTIFY_NEW orca_down` 08:47:00Z -> Slack alert as
  identity `healthcheck` -> audit `job_queued/job_start kind=triage`
  08:47:01Z -> `qoder` triage reply in-thread (1290 chars: likely
  cause, evidence from the log, next actions). The triage's own reply
  was ignored (`alerts_not_healthcheck`). Two `STILL orca_down` lines
  in the interim were `log_only` — by design they never post. Orca
  restarted 08:49:16Z; `NOTIFY_RECOVER recovered` 08:50:10Z posted and
  was ignored by the bridge (`alerts_recovery`).
- Triage quality note: the reply treated the quiet stretch between the
  last memory event (02:12:35Z) and the 08:47Z alert as "the log
  stopped updating … the healthcheck stopped" — but the box was green
  in between, and a healthy box writes no lines (during a failing
  streak the checker appends a `STILL`/`RENOTIFY` line every ~60 s
  run, which is the liveness signal). Fixed with a corrected one-line
  `BOX_FACTS` addition in the bridge: "a line per failing run and on
  state changes; a healthy box writes nothing — silence is normal, not
  a dead checker".
- **#control**: negative probe (a bot-posted mention) -> audit
  `ignored reason=control_bot` — the authorization gate is
  fail-closed. The positive leg (a mention from the user's member ID)
  still needs the user to type it once.
- `spectre-slack-brief --dry-run` renders (health green, 24h counts,
  box block). `sudo spectre-doctor` -> `summary: 35 passed, 0 failed`
  (all Slack gates PASS: env 600, token shapes, node, wrapper,
  settings, cost gate, unit active, timer, self-test).

### Cost gate: bridge runs refused (`cost_gate_refused`) — root cause

First live runs failed: audit `job_failed cost_gate_refused`, Slack
`:warning: qoder run failed (cost_gate_refused)`; guard log for those
allow-starts showed `status=unknown price_factor=None source=` after
~11 s, while every other context (ssh, `check` timer, transient
systemd-run) saw `free source=pid:574580` in <1.5 s. The 11 s = the
guard's fallback path: spawn `qodercli --list-models` and scan its
memory for the Efficient blob.

Bisect (transient units reading `/proc/<pid>/exe` of a live qodercli,
probed on the box):

| unit props | `readlink /proc/<pid>/exe` |
|---|---|
| none | OK — `/home/person/.qoder/bin/qodercli/qodercli-1.1.47` |
| `PrivateTmp=true` | **EACCES** |
| `NoNewPrivileges=true` | OK |
| both | EACCES |

`comm`/`cmdline` stayed readable in all cases; only `exe` broke.
Root cause: `PrivateTmp=true` puts the unit in its own mount
namespace, and the guard's `_is_our_qodercli()` needs `readlink
/proc/<pid>/exe` (exe basename must start with `qodercli`). With the
live-pid scan blind, allow-start fell back to the ~11 s memory scan;
in the observed bridge runs that fallback did not confirm the factor
— `status=unknown`, exit 75. (Other namespace-creating unit properties
were not probed.)

Fix: `PrivateTmp=true` removed from `systemd/slack-bridge.service`
(repo + box; a comment in the unit records why it must not come back).
Verification after redeploy: transient unit with the unit's exact
props -> `allow-start status=free price_factor=0.0 source=pid:574580`
in 1.4 s; both live e2e runs above then hit the fast path (`guard
log: allow-start status=free price_factor=0.0 source=pid:574580`).
Chose this over patching the guard: the guard is box-only (mirroring
is out of scope) and its pid scan is the load-bearing check; the
PrivateTmp was defense-in-depth only.

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
  into segments and checks each — `uptime; uptime` ran;
  `uptime | wc -l` **also ran**, which exposed that there is a second
  gate besides the allowlist: an *internal read-only safe list*
  (confirmed members: `wc`, `ls`; nothing in the profile allowlists
  them). `uptime && journalctl …` and `uptime && touch …` were denied
  whole (no file), `echo $(uptime)` denied. The earlier "compound
  commands are denied" line was wrong as written and is corrected here.
- The safe list made the content readers the next question: `cat`,
  `head`, `tail`, `sed`, `awk`, `cut`, `sort`, `od`, `nl` on the
  0600 slack.env were all refused (attempted, `num_turns=2`, no
  content; `strings` self-refused) — they are *not* on the safe list
  and not allowlisted, so the headless confirmation default denies
  them. They are now deny-listed as a floor too; `ls` on the
  deny-listed directory prints file *names* (and `-l` metadata) only.
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
- Deny floor extended again after the coherence review: the content
  readers (`cat`/`head`/`tail`/`sed`/`awk`/`cut`/`sort`/`od`/`nl`/
  `strings`/`base64`, plus `journalctl`) and the missing path/history
  keys — live settings ended at allow 13 / deny 68 (30 entering the tool
  audit; 22 before the first review round). `wc`/`ls` remain runnable via
  the engine's internal read-only safe list; that is documented, not
  closed.
- Post-fix probes on the box: subagent `DENIED`, no file; write
  `DENIED`, no file; egress `DENIED`; content readers `DENIED`, no
  token; `uptime` and `spectre-status` (positive) ran; secret read via
  Read `DENIED`, no token; tool set 12. After the final deny merge
  (allow 13 / deny 68), `nl` on slack.env was re-probed: `DENIED`,
  `num_turns=2`, no token value in the transcript. `ls -la` on the
  deny-listed directory returned names and `-l` metadata only, no
  contents; Glob over `/tmp` returned a listing (the allow side). The
  live profile is a superset of the repo example: three box-local
  extras (`Read(**/.bash_history)`, `Read(/proc/*/environ)`,
  `Read(/proc/**/environ)`) are all subsumed by wider repo rules, so
  the union merge does not drop them and the repo file stays the
  source of truth. Substitution-bearing commands were re-probed inside
  an otherwise-runnable command: `uptime $(uptime)` and `uptime ${x}`
  both `DENIED` (`num_turns=2`), while the non-substitution control
  `uptime -p` ran; `git log -n 1` was `DENIED`, so bare allow rules
  stop matching when given arguments. The honest statement is the
  operational one: no command containing `$(...)`/`${...}` has run in
  any probe — whether that is a structural check or simply that
  `$`-bearing strings match no rule is **not** isolated. The earlier
  forms each carried an unallowlisted side (`echo $(uptime)`,
  `uptime $(echo hi)`), which is why they did not count.

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
  metadata; Bash is the allowlist *plus the engine's internal read-only
  safe list* (everything else falls to the headless confirmation
  default) and file paths are deny-filtered.
  Note the split: the *file* write tools are rule-denied (gone from the
  set), but a mutating **Bash** command (`mv`, `tee`, redirects) is
  blocked only by the headless confirmation default — no rule names it.
  That default is what the write and compound-smuggling probes
  re-check. Anything that widens the allowlist or the tool set also
  widens this.
- Bot-post event shape: **verified on the first real posts** —
  `message.channels` carries `bot_id` + the customized `username` for
  our posts, so identity resolves (audit reasons on live events:
  `lobby_self`, `alerts_not_healthcheck`, `alerts_recovery`,
  `control_bot`). An unresolvable identity still fails closed (no run).
- The cost gate needs cross-process `/proc` visibility: `PrivateTmp=`
  (probed; other namespace-creating unit properties untested) blinds
  the guard's live-pid scan (readlink `/proc/<pid>/exe` -> EACCES;
  comm/cmdline stay readable), allow-start falls back to the ~11 s
  memory scan, and runs can be refused with `cost_gate_refused`. The
  guard log's `source=pid:<n>` vs `status=unknown … source=` is the
  canary; `slack-bridge.service` stays without `PrivateTmp` (comment
  in the unit, RUNBOOK §7.10).
- Dropping `PrivateTmp` (the fix above) returns the shared `/tmp` to
  the executor's Glob/Read (the `/tmp` allow-side probe lands there;
  no `/tmp`-specific rule exists) — same-user trust domain, accepted
  rather than closed.
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
