# LESSONS.md

Durable lessons from work in this repository. This file is **not** a changelog and
not a session log: `devlog/` holds what happened, this holds what to do
differently next time. Add an entry when a mistake cost real time, when a
measurement contradicted an assumption, or when a tool behaved unlike its
documentation.

Each entry: the lesson, the evidence, and the rule to follow.

## Measuring a relay

**A relay's own surfaces are not evidence.** `/v1/models`, the dashboard and the
error text each disagreed with reality on anyrouter within one day:
`/v1/models` listed 15 models of which one token could call one; the dashboard's
prompt column showed `2` while billing a 40 k-token prefix; one error body
`429 {"error":{"message":"Service Unavailable"}}` was returned for a removed
model, for a nonsense model name, and for a too-short history.
*Rule:* pair a real client's request with the relay's answer to it, and treat
everything else as a hint.

**Reproduce before believing an error status.** `404` meant "no route" in one
body shape and "model not routed for this token" in another; `429` and `503` were
interchangeable for the same condition; `500 get_channel_failed` was
byte-identical for a real model and an invented one.
*Rule:* classify by body shape, and check whether a nonsense control gets the
same answer. If it does, the error carries no model information.

**A minimal hand-built request is not a valid probe.** A one-line body returned
the same generic error as a nonexistent model, while a captured 105 KB client
request succeeded.
*Rule:* capture a genuine request and replay it verbatim, changing one thing at a
time (`scripts/relay_probe/replay_probe.py`).

**Sample a verdict more than once.** Prefix lengths 2 and 10 messages returned
200 while 3, 6 and 9 returned 429, reproducibly 3/3 each. A neighbouring result
cannot be extrapolated, and a single sample cannot distinguish a rule from
intermittent shedding.
*Rule:* repeat each variant at least three times before drawing a conclusion.

**Size a probe to the question.** Reading a full stream to completion pays for
the whole generation to learn a status code. Probes that stopped at
`message_start` (which carries `usage`) gave the same answer far cheaper.
*Rule:* bound the read; never drain a stream just to see whether it opened.

**Latency is a property of the path, not the model.** Same model, same relay:
~35 s for a small prefix, ~250 s for 231 k tokens, and the relay buffered rather
than streamed. Generation settings (`thinking`, `effort`, `max_tokens`) had no
measurable effect on time to first byte.
*Rule:* measure time to first byte separately from generation, and attribute cost
and latency to different levers.

## Configuring clients against a relay

**A settings `env` block beats the shell.** `ANTHROPIC_BASE_URL=... claude` did
not redirect a configured client.
*Rule:* change the settings file, or pass `--settings`.

**`--settings <file>` replaces user settings instead of merging.** A flag-only
test silently skipped `disabledMcpServers`, and the measurement it produced was
invalid.
*Rule:* write the complete configuration you intend to test, and verify the effect
by capture rather than by assuming the flag took.

**A base URL is client-specific.** Claude Code appends `/v1/messages` itself, so a
base ending in `/v1` produced `/v1/v1/messages` → `404 Invalid URL`, which the CLI
rendered as "this model may not exist". Hours were spent treating a config typo as
an entitlement problem.
*Rule:* verify the effective request path before diagnosing anything else.

**A fast retry loop turns a slow relay into a wall of errors.** `MAX_RETRIES=300`
with `RETRY_WATCHDOG=1` retried with zero delay and produced `429 Service
Unavailable` instead of letting the first attempt finish.
*Rule:* few retries, generous timeouts; the amplifier is the bug.

**An interrupted turn can leave a state the relay refuses.** Interrupting a
Claude Code turn leaves the transcript ending on an assistant message, and a
request whose final message is `assistant` was rejected 3/3.
*Rule:* let turns finish, or end on a user message; never leave a critical
session parked mid-turn.

**Dependencies can ignore their own disable flags.** `enabled: false` in
`~/.config/1mcp/mcp.json` did not stop 1mcp serving those servers' tools; the
supported path was `1mcp mcp disable <name>` **followed by a service restart**
(the first restart ran before the change and showed nothing).
*Rule:* verify the effect through the interface the client actually sees, and
restart dependent services after changing their config.

**A relay can refuse a perfectly healthy request for shape reasons.** On
anyrouter, replaying a captured session body showed a reproducible pattern: a
request whose **final message is `assistant`** returns the generic
`429 Service Unavailable` (3/3 at several prefix lengths), while `user`- and
`system`-final bodies return 200. This is the state an **interrupted turn**
leaves behind — pressing Esc mid-turn can therefore wedge a session: every
subsequent request is refused until the history changes.
*Rule:* let turns finish; if a session starts returning 429, check the final
message role of its history before blaming the provider.

**Deep histories also start being refused.** A sweep over the same captured
body found 429 at 15, 30, 60 and 80 messages while 2, 5, 10, 20, 25, 35, 40, 50
and 70 returned 200 — with a 303 KB/80-message body refused although a 629 KB
body built from a short history succeeded. So size alone is not the rule;
role plus accumulated conversation shape is.
*Rule:* keep sessions bounded (compact or hand off to a fresh primed session
before the history gets very deep) rather than pushing one session for days.

**Retry settings decide whether a refusal is recoverable.** With
`CLAUDE_CODE_MAX_RETRIES=300` and `CLAUDE_CODE_RETRY_WATCHDOG=1` the client
retried with no delay and displayed `attempt 11/300` indefinitely; the turn never
ended, the transcript stopped being written, and the session looked hung.
*Rule:* few retries and generous timeouts, so a refusal surfaces as a clear
failure instead of a silent stall, and a session started before a settings change
must be restarted before the change can help it.

## Orca

**`--worktree path:<dir>` creates a terminal the ADE UI never shows.**
`orca terminal create --worktree path:/home/person/wt/release-readiness-spectre`
returned `ok: true` with `surface: "background"` and bound the terminal to
**`bbc15fac-…::/home/person/wt/release-readiness-spectre`** — the minecraft repo's
id glued to the release path. `orca worktree ps` lists the real record for that
path as **`00bbf03b-…::/home/person/wt/release-readiness-spectre`**. The UI renders
registered worktrees, so every terminal created the `path:` way stayed invisible
even though `orca terminal list` showed it as connected and writable.
*Rule:* create from **inside** the worktree with the active selector
(`cd <worktree> && orca terminal create --worktree active …`). Treat a returned
`worktreeId` that is not in `orca worktree ps` as a failure — the terminal will
not be visible.

**The invisible-terminal bug was in the documented convention.** `AGENTS.md` and
RUNBOOK §7.8 told agents to use `--worktree path:<dir>`, so every agent that
followed the instruction produced tabs the operator could not see — which is how
a fleet of "LIVE" Flash sessions accumulated with nobody able to watch them.
*Rule:* when a documented command produces a silent partial success, fix the
document, not just the instance.

**A successful exit code is not proof of the outcome.** `terminal create`
returned `ok: true` while failing to make the terminal discoverable; `terminal
switch` returned `ok: true` with `"navigated": false`; `terminal list` reported
`connected: true, writable: true` for terminals the UI never rendered.
*Rule:* check the field that describes the outcome (`surface`, `navigated`, the
`worktreeId`), not the status.

**`orca is` / `wait` / `screenshot` drive the built-in browser, not terminal
tabs.** They answer `browser_no_tab` for a worktree with no browser page, so they
cannot be used to verify that a terminal tab is visible.
*Rule:* for terminal visibility, verify the worktree binding instead; do not
claim a tab renders without operator confirmation.

**A headless runtime reports a closed desktop window that does not matter.**
`orca status` said `desktopWindowStatus: openable` and `orca open` timed out on
the box, while the operator was watching the ADE over Tailscale. Trying to open a
local window was wasted effort.
*Rule:* check how the operator actually reaches the UI (the Tailscale connection
to the runtime port) before diagnosing a display problem.

**A terminal in an *unregistered* worktree cannot be made visible at all.**
Orca renders only worktrees it has registered, and a `worktreeId` is a
per-worktree instance id (`<instance>::<path>`), not something derived from the
path. Measured on 2026-09-25:

| worktree | registered? | `--worktree active` result |
| --- | --- | --- |
| `/home/person/wt/release-readiness-spectre` | yes (`00bbf03b-…`) | terminal created, `worktreeId` registered → **visible** |
| `/home/person/wt/casino-cap-locks` | no | **creation fails** (no handle) |
| either, with `--worktree path:<dir>` | — | created, but `worktreeId` is a synthesized `<repoId>::<path>` → **invisible** |

A plain `git worktree add` is not enough; a worktree becomes renderable by being
Orca-managed (`orca worktree create …`) or already registered.
*Rule:* run visible work in a registered worktree, and create genuinely new
worktrees with `orca worktree create` rather than `git worktree add`. Before
trusting any create, check the returned `worktreeId` against
`orca-ide worktree ps`.

**Verify a terminal is visible, do not assume it.** `dsh-clinepass-orca` now
compares the created `worktreeId` against `orca-ide worktree ps` and prints a
loud warning naming the fix when it is a ghost, because the previous silent
success hid a whole fleet of Flash sessions.

**Orca resurrects agent sessions from per-pane records — including ones you
killed.** Orca records, per pane,
`{providerSession: {key: 'session_id', id: '<agent session id>'}}` in
`~/.config/orca/agent-hooks/last-status.json`, and on pane or worktree restore it
rebuilds the launch command from that record
(`daemon-ready-identity`: `case 'claude': return ['claude', '--resume', id]`)
adding the agent's default flags (`tui-agent-permissions`: claude →
`--dangerously-skip-permissions`).

Observed 2026-09-25: a Claude session that had been **killed** (`pkill`, leaving
its pane record at `state: done`) was resurrected at 13:52 as
`claude --dangerously-skip-permissions --resume 9011601c-…` in the same worktree,
while the current session was still running — two agents in one git worktree,
and the resurrected one carried a 4.5 MB history of exactly the shape this relay
refuses with its fake 429. The daemon log showed it plainly:

```
startup-command-delivery  …@@48e9e973  commandLength: 89, hasCommand: true
```

89 is the shlex-quoted form of that command, so the length alone identifies it;
`orca-ide terminal list` then matched the new terminal's `tabId` to the stale
pane record. Nothing in any agent transcript mentioned it, because no agent ran
it — Orca did.
*Rule:* retire an agent pane **through Orca** (`orca terminal close`), never by
killing the process: closing clears the record, killing only marks it `done` and
leaves it resumable. Verify with
`grep <session-id> ~/.config/orca/agent-hooks/last-status.json` (0 matches means
it is gone). Never leave two agent sessions in one worktree.

**Latency follows the request's BYTES, and images inflate bytes without inflating
tokens.** A session failed with `524` on every attempt at ~97 k prompt tokens and
~1.43 MB of body. Token counts looked fine and so did three images, which is why
"the context got too big" looked wrong at first. The real driver is bytes, and
images are the byte-inflation vector: image tokens are charged by pixel
dimensions (~533 tokens for 800x500) while the base64 payload is hundreds of KB,
and the client re-sends every image on every turn. Measured on this relay:

| request | prompt tokens | body | result |
| --- | --- | --- | --- |
| captured body, unchanged | 37,861 | 107 KB | 200 in 65.5 s |
| + 3 images | 39,446 | 584 KB | 200 in 46.7 s |
| + 230,000 chars of real text | 125,632 | 344 KB | 200 in 100.5 s |
| the failing session | ~97,000 | **~1.43 MB** | **524 at ~301 s x 6** |

The failing session had accumulated **1,234,904 B of base64 images** inside its
post-compaction window - 93 % of that window's content, 8+ images re-sent every
turn. Three images were below the edge's capacity; 1.2 MB of them is not. Note the
trap: the last *recorded* usage was 97 k tokens from before the accumulation, and
every request after it failed, so no usage figure ever showed the growth.
*Rule:* measure body bytes, not tokens, and check for accumulated base64 images
when a long session starts timing out. `/compact` or a fresh session drops them.

**A repeatable timeout is a rate, not a cause.** The same session failed with
`524 status code (no body)` four times, 30-36 minutes apart. The interval is the
tell: the relay edge gives up at ~301 s and the client retried five times, so
`301 s x 6 = ~30 min` per reported failure. Measuring the interval identified the
retry structure before any payload was inspected.
*Rule:* divide the observed interval by the known timeout to get the retry count;
when it is close to a whole number, the failure is one condition repeating rather
than several different faults.

**A `/goal` Stop hook turns one failure into an unattended retry loop.** That
session had `/goal` active, so every 524 was followed by an automatic
`Goal check-in: continuing after the last turn ended early` turn, which failed the
same way - roughly every 30 minutes for hours, with nobody watching.
*Rule:* when a session fails repeatedly and unattended, check for a goal/Stop-hook
loop first; it converts a single transient fault into a long burn.

## Working practice

**Do not use `pkill -f <pattern>` when the pattern matches your own command
line.** It killed the shell running the command, three times in one session,
silently discarding the output being collected.
*Rule:* use `pkill -x <exact-name>`, or a bracketed pattern like `'[c]ap2.py'`.

**Never print a raw config dict.** Masking by top-level key name missed secrets
nested under `env`, `args` and `headers`, and printed four credentials across two
sessions.
*Rule:* mask recursively before printing, walk known secret-bearing containers,
and prefer printing key names and presence rather than values.

**Do not name a script after a standard-library module.** `/tmp/bisect.py`
shadowed `bisect`, and Python failed to import `urllib` with a circular-import
error that pointed nowhere near the cause.
*Rule:* give scratch scripts unambiguous names.

**A long staged verification has environment coupling.** The gate failed on
Spectre because the staged `slack-notify` self-test picked up the box's installed
registry (9 agents) instead of the staged one (10), and the login MOTD leaked into
vendored shell tests.
*Rule:* run staged gates with `SLACK_AGENTS_FILE` pointed at the staged tree and
`SPECTRE_MOTD_DONE=1`.

**The local DSH runtime exports a `PYTHONHOME` that breaks `python3`.**
`verify.sh` died with `ModuleNotFoundError: No module named 'encodings'` inside
the sandbox, which looks like a repo failure and is not one.
*Rule:* run Python gates as `env -u PYTHONHOME -u PYTHONPATH python3 ...`.

**Keep tools where they can be found again.** Every probe written in `/tmp` was
lost when the directory was cleared mid-session, and had to be rewritten.
*Rule:* reusable tools live under `scripts/relay_probe/` (see its README), and
secrets-bearing scratch files are deleted as soon as the measurement is done.

**A stalled agent may be quota-dead, not busy.** A Codex Tier-0 process had been
alive for six days with 0 s of accumulated CPU and a terminal reading "You've hit
your usage limit".
*Rule:* before assuming work is in progress, check accumulated CPU time and the
mtime of the session file.

## Cost

**Cache fields decide the real cost.** `input_tokens` was `2` while the prompt
sat in `cache_creation_input_tokens`/`cache_read_input_tokens`; write costs 1.25×
input and read 0.1×, so an unstable prefix re-buys the whole prompt every turn at
a 12× premium.
*Rule:* sum all three fields for size, keep the prefix stable, and treat the
dashboard's prompt column as meaningless.

**A prompt the relay "dropped" may just be cached.** A single-digit prompt column
alongside real completion tokens looked like the relay discarding input; burying
an unguessable marker and reading it back proved the prompt arrived intact.
*Rule:* test for data loss with a recall marker rather than inferring it from
counters.

## ZCode on the Spectre

**The bundled CLI assumes the desktop ran first.** `node
/opt/ZCode/resources/glm/zcode.cjs login` (app 3.14.3, CLI 0.16.9) died with
「无法定位 CLI ZCode Built-in Provider Config」 while `--version` and `doctor`
ran fine: the desktop materializes that config into
`~/.zcode/v2/runtime/provider/<arch>/<ver>/endpoint-*/zcode-builtin.json` and
hands the CLI `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`; the lookalike env
`ZCODE_BUILTIN_PROVIDER_BUNDLED_CONFIG_FILE` exists in the bundle but is not
honoured on this path, and neither cwd nor the deb layout satisfies the lookup.
*Rule:* on a desktopless box, launch the CLI through a wrapper that pins
`ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=/opt/ZCode/resources/config/provider/zcode-builtin.json`
(the deb ships it there); the box has `~/.local/bin/zcode` for this since
2026-09-25.

**A standalone CLI can only use the plan its login minted.** The provider
registry contains a plan provider only if the credential store holds
`account-provider:<id>:identity` + `…:api-key`; `zcode login` mints exactly
the account's server-side active plan (here: individual, empty balance → every
`-p` run dies with provider error 1113), and other plans like
`account:zai-start-plan` are not registered at all. Ruled out by experiment:
`provider_config.json` `defaultModelSelection` edits, `ZCODE_AGENT_PROVIDER`
and desktop-equivalent env sets, and the app-server protocol (NDJSON
`{id,method,params}` — no `jsonrpc` key; `session/create` waits 15 s for a
`session/requestRuntimePreferences` callback that only a desktop host
answers; the `session/setModel` handler forwards the model object
unconverted and the registry rejects it). `zcode tui` is dead in every
desktop bundle (`Cannot find package '@zcode/tui'`), on both machines.
*Rule:* to move a box onto a different plan, mint from a desktop host or
re-login after the server-side active plan changes; do not burn an afternoon
on client-side selection paths.

**The credential store is decryptable offline, but that only proves the wall.**
Credential values (`enc:v1:iv.tag.ct`, base64url) are AES-256-GCM under
`sha256("zcode-credential-fallback:" + os.platform() + ":" + os.homedir() + ":"
+ username)` unless `ZCODE_CREDENTIAL_SECRET` is set. Decrypting them on the
box confirmed: the individual api-key is valid at `api.z.ai/api/anthropic`
(429/1113 = recognized, empty plan), while the Start-Plan gateway
(`zcode.z.ai/api/v1/zcode-plan/anthropic`) returns 401 for that key and
**3007 "captcha verify failed"** for the ZCode JWT — the gateway only accepts
the official client's captcha-cleared session. Also: `zcode login` hardcodes
exactly one "Individual Coding Plan Provider" per family (vHo), so re-login
can never mint a different plan.
*Rule:* plan-gateway auth is official-client territory — do not try to
replay, mint or spoof past the captcha boundary; use the desktop host for
those plans or get a proper plan key from the vendor.

## Blender renders on the Spectre

**Headless WORKBENCH/EEVEE needs a display server even with `--background`.**
`blender --background --python render.py` died with
`EGL Error (0x3009): EGL_BAD_MATCH` before writing a single PNG, while pure
geometry builds (`--background --python builder.py`) worked fine — only the
render engines ask for a GL context. Xvfb was already installed.
*Rule:* wrap render invocations in `xvfb-run -a -s "-screen 0 1280x720x24"`;
geometry builds need no wrapper. (2026-09-25, korea-metro-twin CC tunnel probes.)

**`build_tunnel_manifest.py` merges, it does not rebuild.** Removing defs from
`tunnel_defs` does not remove records: `main()` preserves any existing
`tunnel_segment` record whose id is not in the new defs, so index.jsonl keeps
stale segments after a def rollback.
*Rule:* to un-build a batch, filter its `segment_id`s out of index.jsonl before
regenerating, or the manifest will disagree with the defs.

## Splitting a flag's value across shell lines silently truncates the command

**A wrapped `-f` value ended the yt-dlp invocation early, and nothing reported
it.** Patching `scripts/fetch_videos.sh` turned a single `-f "selector" \` line
into a quoted first half with *no* trailing backslash plus an indented second
half. The shell therefore terminated the command at the first half: yt-dlp got
`--download-archive` and `-f` but never `-o`, `--write-info-json`, `--no-playlist`
or the retry flags. It ran for minutes looking healthy, using yt-dlp's *default*
output template, and wrote a 337 MB `.part` file into the project root named after
the video's Korean title instead of `media/videos/<id> - <title> [1080p].mp4`.
`bash -n` passed, and the orphaned continuation line failed as a separate command
whose error landed in the same `tee` log as the progress output.
*Rule:* never split a quoted argument value across a line break, even with a
continuation. After editing anything that builds an argv, verify the argv itself —
point the tool variable at a stub that prints `"$@"` — instead of trusting
`bash -n`, a live run, or an exit status. (2026-09-25, korea-metro-twin video fetch.)

## A 403 from a model API is not always a bad key

**The Spectre's ClinePass key authenticated perfectly while being unable to run a
single completion.** `/api/v1/users/me` returned 200 for the account that had been
working all day (its `/usages` record even showed a successful
`cline-pass/deepseek-v4.1-flash` call minutes earlier), yet every `cline-pass/*`
request returned `403 ENTITLEMENT_ERROR` and `/users/me/plan` returned
`404 no plan history found for user`. The obvious reading — "the key went stale,
rotate it" — would have replaced a working credential with another working
credential and left the box exactly as broken, because the *account* had lost its
subscription. A second trap: a probe with `max_tokens: 1` answers
`500 {"error":"empty response content"}` even on a healthy key, which looks like
an upstream outage rather than a probe artifact.
*Rule:* separate authentication from entitlement before touching a credential.
"Who is this key?" (`/users/me`) and "is this account entitled?"
(plan/usage endpoint) are different questions, and only the second explains a
403. Finish with a real completion at a normal token budget, then the consuming
path end to end. (2026-09-25, Spectre ClinePass rotation.)

## `pkill -f <pattern>` matches the shell that is running it

Running `ssh spectre 'pkill -f "fetch_videos.sh"; ...'` killed its own remote
shell, because the pattern appears in that shell's own command line, so the
follow-up diagnostics never printed and the command returned empty output that
looked like a dead connection.
*Rule:* bracket one character of the pattern (`pkill -f "[f]etch_videos.sh"`) so
the pattern cannot match its own invocation. (2026-09-25, korea-metro-twin video fetch.)

## A resumed Claude Code session stalls on the workspace-trust dialog, and `bypassPermissions` does not skip it

Migrating a Claude Code session to the Spectre meant copying the transcript and
running `claude --permission-mode bypassPermissions --resume <uuid>`. The TUI
came up on the box's CLI v2.1.281 and stopped at

```
 Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
```

`bypassPermissions` reads like "answer yes to everything", and the box's own
`~/.claude/settings.json` already sets `permissions.defaultMode:
bypassPermissions` — neither has any effect here. The gate is **workspace
trust**, keyed on `projects["<cwd>"].hasTrustDialogAccepted` in `~/.claude.json`,
and it sits upstream of the permission system. The CLI carries no flag to
pre-accept it (`Yes, I trust this folder` is the only affirmative in the
binary), so a fresh path — which is what a migrated session always has, since
the box's repo lives at a different directory than the sender's — always hits
it. The failure mode is a hang with a plausible-looking UI, not an error, so it
reads as "the relay is slow".
*Rule:* a Claude session is three files **and one trust flag**; seed
`hasTrustDialogAccepted: true` (merging into mode-600 `~/.claude.json`, never
rewriting it — it holds relay keys) before creating the terminal. Copy the entry
shape from an already-trusted project, and check it first whenever a resume
produces no output. (2026-09-26, control-plane session handoff, RUNBOOK §7.19.)

## A Claude session's transcript slug is derived from its cwd, and the cwd in the transcript does not move with it

The same session id lands under a different `~/.claude/projects/<slug>/`
directory on each machine, because `<slug>` is the recorded working directory
with `/` replaced by `-` (`/home/person/Projects/distribution-project/remote-agent`
→ `-home-person-Projects-distribution-project-remote-agent`, but
`-home-person-Projects-remote-agent` on the box). The session id is stable; the
directory is not.
*Rule:* compute the receiving slug from the receiving machine's path and copy the
transcript there. The `cwd` field *inside* the transcript keeps the sender's
path — it is the address, not a statement about the machine — so it neither
proves nor causes anything, but a resumed session that trusts its own history
will write sender paths on the receiver. Say which paths are real in the first
message after the move. (2026-09-26, same handoff.)

## Orca wraps input to a `claude` terminal in bracketed paste, and `claude auth login` keeps the markers

Pasting an OAuth `code#state` into `claude auth login --claudeai` with
`orca-ide terminal send` failed twice with `Login failed: Request failed with
status code 400`. Orca classifies a terminal whose foreground process is
`claude` as `provider: claude` and wraps every send in `ESC[200~ … ESC[201~`;
the login prompt is not the TUI's paste-aware input and submits the markers as
part of the code. The same send into a shell terminal arrives clean.
*Rule:* feed a code to `claude auth login` through a pty you control (write the
bytes, then `\r`), not through `terminal send`. When a pasted value is
"rejected", dump what the process actually received (`od -c`) before blaming
the value. (2026-09-26, Spectre Max switch.)

## `claude -p` inside `ssh host 'bash -s' <<EOF` eats the rest of the script

`bash -s` reads the script from stdin, and `claude -p` also reads stdin, so the
first `claude -p` swallowed every line after it: the command "succeeded" and
the checks that followed never ran.
*Rule:* give every `claude` in a stdin-fed script `</dev/null`.
(2026-09-26, Spectre Max switch.)

## A `--fork-session` preflight of a `/goal` session runs the goal

Forking a session with `claude --resume <uuid> --fork-session -p "reply ok"` to
check that its history is accepted by a new endpoint answered `ok` — and then
the session's `/goal` Stop hook, which is session state and travels with the
fork, drove about 25 more turns in the real repo cwd. It could not write only
because `-p` without a bypass flag refuses writes.
*Rule:* check a transcript for an active `/goal` before any `--resume -p`, never
give a preflight `--dangerously-skip-permissions` or `bypassPermissions`, and run
it under an isolated `CLAUDE_CONFIG_DIR` so the fork's transcript stays out of
the live session list. (2026-09-26, Spectre Max switch.)

## An agent cannot switch its own auth; ship the switch as an operator script

Installing a credential and deleting `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`
from `~/.claude/settings.json` through the shell is refused by the
irreversible-guard hook with no override — its effect outlives any approval
window, so a person has to run it.
*Rule:* plan an auth or settings change as a reviewed script with its own
backups from the start (`ssh spectre 'bash -s' < script`, run by the operator
via `!`), and do everything around it — login, preflight, restart — in the
agent's own lane. (2026-09-26, Spectre Max switch.)

## `terminal send --enter` into a Claude TUI submits whatever was already typed

To restart an idle Tier-0 session, `orca-ide terminal send --text "/exit"
--enter` was sent into its prompt. A `2` typed earlier to answer a one-time
dialog was still in the input box, so the TUI submitted `2/exit`. Tier-0 was
waiting for the operator to pick among numbered options, read it as "do item
2", and wrote a memory and ran `git fetch` before it was interrupted.
*Rule:* do not type into a Claude TUI to stop it. `SIGTERM` the `claude` pid
(it exits in seconds and prints its `--resume` line), then send the launch
command into the tab's shell after a raw Ctrl+U. When text must go into a TUI,
read the screen first, and clear the box with a raw `\x15` if it is not empty.
If a stray input is submitted anyway, tell the session plainly that it was not
from the operator. (2026-09-26, Spectre Max switch.)
