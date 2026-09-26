# Spectre's Claude Code moved from anyrouter to the Max 20x subscription — 2026-09-26

The operator reactivated the Claude Max 20x subscription and asked for every
Claude Code session on the Spectre to run on it instead of the anyrouter relay.
Fedora's `/login` had already succeeded; the box needed its own login, a config
switch, and a restart of the two live sessions without losing their history.

## Why the box needed its own login

Spectre's `~/.claude/.credentials.json` was a dead OAuth record from 2026-09-05
(`expiresAt: 0`); a plain `claude` on it answered
`Failed to authenticate: OAuth session expired and could not be refreshed`.
Copying fedora's fresh credential over was ruled out: the refresh token rotates
on use, so two machines sharing one would log each other out.

The login was done in an **isolated config dir**, `~/.cache/claude-max-login`
(mode 700), via `CLAUDE_CONFIG_DIR`, so nothing under `~/.claude` changed until a
working credential existed. That dir isolates both settings and credentials,
which also kept the relay env from `~/.claude/settings.json` out of the test.

## The OAuth code that kept failing with 400

`claude auth login --claudeai` prints an authorize URL and waits for a pasted
`code#state` string. Sending the operator's code into the Orca terminal with
`orca-ide terminal send` failed twice:

```
Login failed: Request failed with status code 400
```

A byte test explains it. Orca classifies a terminal whose foreground process is
`claude` as `provider: claude` and wraps every `send` into it in bracketed paste.
The login prompt is not the TUI's paste-aware input — it keeps the markers as
part of the code:

```
033 [ 2 0 0 ~ A B # … 033 [ 2 0 1 ~
```

A shell terminal (`provider: unsupported`) receives the same send without the
markers. The fix was to take Orca out of the path: a small Python pty driver
(`~/.cache/claude-max-login/drive.py`, run as a transient systemd user unit)
spawns `claude auth login --claudeai`, writes the URL to `url.txt`, and writes
whatever lands in `code.txt` to the pty as raw bytes followed by `\r`. Its
deadline is three hours, because the operator was away from the desk and each
earlier URL had gone stale before the code came back.

Result:

```
Paste code here if prompted > Login successful.
{ "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x", ... }
```

and in the isolated dir `claude -p "reply with exactly: ok"` → `ok` in 5.9 s,
`claude auth status --text` → `Login method: Claude Max account`.

## The switch itself is an operator action

Installing the credential and removing the relay keys from
`~/.claude/settings.json` from an agent's shell was refused by the
irreversible-guard hook ("writing an agent's own configuration through the
shell … the user edits the file, or runs the command, themselves"), with no
override. The change was therefore packaged as a script the operator ran from
fedora with `ssh spectre 'bash -s' < switch-to-max.sh`. It:

- copies each file it replaces to `<file>.bak-max-<ts>` (mode 600);
- installs the new `.credentials.json` (mode 600);
- merges only `oauthAccount` into `~/.claude.json` with `jq`;
- deletes only `env.ANTHROPIC_BASE_URL` and `env.ANTHROPIC_AUTH_TOKEN` from
  `settings.json` and tightens it from 664 to 600. `model`, `effortLevel`, the
  output style, the timeouts and the hooks are untouched.

Nothing else on the box injects the relay: no `ANTHROPIC_*` in shell rc files,
`environment.d`, the systemd user environment, the Tier-0 launcher, or either
live process's environment. The relay reached the sessions only through the
settings `env` block.

## Preflighting the two histories

A resumed session replays its history to the new endpoint, and that history was
produced through the relay. Each was forked and resumed once in the isolated dir
before touching the live sessions (`claude --resume <uuid> --fork-session -p
--model claude-opus-5-5 …`, transcript copied into the isolated `projects/`):

| Session | cwd | Result |
| --- | --- | --- |
| `9fc50e14-7c5f-4bad-9fb3-2e0c73759586` (Tier-0) | `~/wt/release-readiness-spectre` | `ok`, 15 s |
| `5522c1fa-6111-416a-99ec-a3888035bfa2` (control plane) | `~/Projects/remote-agent` | `ok`, cache creation 129 436 tokens |

Neither history was refused. The control-plane fork, however, did not stop at
`ok`: the session's `/goal control plane 구현 계속해줘` is session state, it
re-armed in the fork, and the Stop hook drove about 25 more turns over 2½
minutes. The fork ran in the real repo cwd, so its tool calls were real. It ran
in `-p` mode without a bypass flag, so every write was refused (two `Edit`s and
three `Bash` writes, e.g. `Claude requested permissions to write to …
devlog/2026-09-23-anyrouter-design-deltas.md, but you haven't granted it yet.`);
only two reads succeeded. The repo was not changed.

## Restarting the live sessions

Settings `env` is read at process start, so both sessions kept talking to the
relay until restarted. Each was restarted in its own Orca terminal so the
operator's tabs stay where they were:

| Session | Terminal | Restarted with |
| --- | --- | --- |
| control plane `5522c1fa` | `term_0858170c-…` | `/exit`, then `claude --dangerously-skip-permissions --resume 5522c1fa-…` |
| Tier-0 `9fc50e14` | `term_49b16cd1-…` | `/exit`, then `bash …/start-tier0-claude.sh --resume 9fc50e14-…` |

The first launch on the Max login stops at a one-time dialog, *Make auto mode
your default permission mode?*, with **Yes** preselected. Taking the default
would have rewritten `permissions.defaultMode` away from `bypassPermissions`;
both were answered **No, keep bypass permissions**, and the settings file was
checked unchanged afterwards. In the Tier-0 terminal a plain `2` was enough. In
the control-plane terminal it was not, and the dialog was driven with raw sends
— `orca-ide terminal send --text "$(printf '\033[B')"`, then
`--text "$(printf '\r')"`, both without `--enter` — because `--enter` routes
through the agent-prompt path and its bracketed paste.

Evidence that both now run on the subscription:

- the status line shows the plan windows, e.g. `5h 8% · 7d 2%`, which the relay
  never reported;
- both processes hold connections only to `160.79.104.10:443` (Anthropic), none
  to anyrouter (`163.181.252.138`);
- the control-plane session continued its `/goal` on Max and auto-compacted
  from 146 k to 106 k context without error.

The control-plane session was told in one message why it had been restarted
and that the relay premises (request-size guard, 429 avoidance) no longer
hold. Tier-0 was left idle at its prompt: it is waiting for the operator's
answer about a scoped release, and nothing was sent to it.

## Lifting the relay-era limits

With the relay gone, the settings that existed only for it stopped protecting
anything and started capping the sessions. The clearest case:
`claude -p --output-format json` on the Spectre reported `contextWindow
1000000` for `claude-opus-5-5`, while `/context` showed an auto-compact window
of `180k` — the relay's `CLAUDE_CODE_AUTO_COMPACT_WINDOW=180000`, sized for its
~300 s edge timeout, was cutting a 1M window to 180 k.

Fedora was lifted first, by a parallel session (`lift-relay-limits.sh`,
2026-09-26 20:42): the whole settings `env` block, the `request-budget.mjs` Read
hook, and `autoCompactWindow` pinned to `1000000`; a fresh session there showed
`/context` `… / 1m` without `ANTHROPIC_BETAS`. The Spectre got the same change
plus its own relay trims:

| Removed | Why it existed |
| --- | --- |
| env `CLAUDE_CODE_AUTO_COMPACT_WINDOW=180000` (and top-level `autoCompactWindow: 180000`, now `1000000`) | relay edge timeout at ~1 s per 1 k prompt tokens |
| env `ANTHROPIC_BETAS=context-1m-2025-08-07` | anyrouter answered 400 without it |
| env `CLAUDE_CODE_MAX_RETRIES=5` | relay's fake 429s |
| env `API_TIMEOUT_MS` and four stream/first-byte timeouts, all `600000` | relay buffered the whole prefix before the first byte |
| env `CLAUDE_CODE_DISABLE_CRON=1` | −3 tools of relay prefix |
| `disabledMcpServers`, and the `obscura` MCP re-registered | −37 tools of relay prefix; obscura is the box's standard browser (RUNBOOK 7.12) |
| PreToolUse `Read` hook `request-budget.mjs` | 800 KB request-body cap for the relay edge |

Not restored: the Spectre's `gateway` MCP. It pointed at a 1mcp endpoint
(`127.0.0.1:3050`) that already answered `ECONNREFUSED` on 2026-09-25, and 1mcp
is not installed on the box. Kept on fedora: 1mcp's `filesystem` and
`sequential-thinking` stay disabled, because they duplicate built-in tools, not
because of the relay.

The Tier-0 briefs carried the relay as rules for the model — "Relay
constraints measured on this box (obey these)": no concurrent requests, never
leave a turn interrupted, a quiet terminal is not a hang. Those sections now
describe the Max harness instead, and the launcher's "only resume a shallow
history" comment is gone (backups `*.bak-unrelay-20260926-204412` beside each
file).

<!-- unrelay run and restart results go here -->
