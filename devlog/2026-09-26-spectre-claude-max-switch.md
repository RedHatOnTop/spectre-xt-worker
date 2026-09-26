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

<!-- filled in after the restart -->
