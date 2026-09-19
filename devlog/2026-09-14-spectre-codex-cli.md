# 2026-09-14 — Codex CLI on the Spectre, third-party API included

## Why

The box had Claude Code, Qoder and ZCode, but not Codex. The ask: install
it on the Spectre, copy the daily-driver settings, and carry the
third-party API setup over too — the box should not need an OpenAI login.

Three scope calls were made up front:

- **Active provider only.** The Zenbook's `codex-mode` registry holds seven
  providers and seven keys; only `anyrouter` (the active one, `gpt-6-astra`)
  is transferred. More providers are one `codex-mode add` away on the box.
- **Search/doc MCP only.** exa, context7, sequential-thinking. `github` has
  no token on the box, `filesystem` points at a desktop path, and
  `node_repl` needs the ChatGPT desktop runtime.
- **No irreversible-guard for Codex on the box** (user call). Codex runs
  with approvals off and full sandbox access, so on the box it carries the
  read-guard only. The box-profile irreversible-guard stays Claude-side.
  This is a deliberate asymmetry, recorded in RUNBOOK 7.11, not an
  oversight.

## What was copied, and what was dropped

`~/.codex/config.toml` is box-adapted, not a byte copy. Dropped because
they belong to the ChatGPT desktop app and would be dead weight or broken
on a headless box: `[desktop]`, `[plugins]`, `[marketplaces]`,
`mcp_servers.node_repl` (paths under `/usr/lib/chatgpt/`), `[windows]`,
and the `hooks.state` trust records. Project trust lists only paths that
exist on the box.

Kept from the daily driver: model + provider wiring, `instructions.md`
(Korean-engineer response style), the global `AGENTS.md`,
`model_reasoning_effort = "xhigh"`, and the read-guard.

## Guard files are per-agent ports

The daily config ran into this once already: a hook keyed to the wrong
tool-name set is silently inert. Codex names differ (`shell` /
`exec_command` with `argv[]` or `command`, `apply_patch`), so the read-guard
installed under `~/.codex` is the Codex port, and the first attempt at a
Codex-shaped irreversible-guard was dropped when the box decision landed.
The Claude-shaped `config/irreversible-guard.mjs` must not be installed
under `~/.codex` — it would match nothing and allow everything.

## Bug found while wiring the tests

Extending `config/test-irreversible-guard.sh` with write-class payloads
(`Write` + `file_path`) exposed a real defect in the shipped Claude guard:
`isProtected()` normalized the candidate path to backslashes and then
compared it against `join()` results, which re-introduce `/` on Linux. The
comparison never matched, so `Write`/`Edit` against `~/.claude/settings.json`
or the hooks directory was **allowed** while the shell path was blocked.
The test failed on the first run; the fix normalizes both sides to forward
slashes (`norm()`), and the suite passes. The shell-based protection was
never affected.

## Repo artifacts

- `config/spectre-codex-config.toml` — box config template; MCP keys are
  placeholders substituted at first install from the environment.
- `config/read-guard.mjs`, `config/codex-instructions.md`,
  `config/codex-global-AGENTS.md` — copied verbatim from the daily driver.
- `scripts/codex-mode` — vendored provider switcher (same file the Zenbook
  runs; python3 only, no jq/curl).
- `scripts/install-codex.sh` — pinned npm install, config/hooks/tool
  install, env.sh + `.bashrc` wiring. Called from `bootstrap.sh`.
- RUNBOOK 7.11 — install, one-time key transfer, behavior notes, verify,
  rollback.

The key never enters the repo: it is piped daily-driver → box over
Tailscale SSH into `~/.codex/modes/keys/anyrouter` (0600) and reaches Codex
as `CODEX_THIRDPARTY_API_KEY` via `~/.codex/modes/env.sh`.

## Verification (on the Spectre, 2026-09-14)

```
codex --version                        # codex-cli 0.154.0
codex-mode status                      # third-party API, Anyrouter, gpt-6-astra, key stored
codex exec "reply with exactly: ok"    # -> ok (gpt-6-astra via anyrouter, 7,728 tokens)
printf '{"tool_name":"read","tool_input":{"path":"/home/person/.env"}}' \
  | node ~/.codex/hooks/read-guard.mjs # exit 2, refusal message
```

One measurement worth recording: a session asked to read a `.env` printed
it, no refusal. The tool dump from that session carries no path-based read
tool — reads go through `exec_command`, so a guard keyed on
`tool_input.path` never sees them. read-guard is correct in isolation and
dormant in practice on this codex-cli; both facts are stated in RUNBOOK
7.11 rather than papered over. Same measurement applies to the daily
driver, which runs the same CLI version and file.
