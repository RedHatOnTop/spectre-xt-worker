# DevCodex for ChatGPT — DevSpace skill + shell path

Date: 2026-09-14
Related: `2026-09-14-devcodex-on-spectre.md` (box deployment), RUNBOOK §7.9 / §7.13

## Goal

The user asked to connect DevCodex to ChatGPT, reusing the existing
DevSpace↔ChatGPT authentication on this machine (the Zenbook) if possible,
and to document the method if not.

## What the investigation found

- The Zenbook's DevSpace (`@waishnav/devspace` 1.0.8) *is* the ChatGPT
  connector: HTTP MCP at `https://fedora.tail1fa7c9.ts.net/mcp` (Tailscale
  Funnel → `127.0.0.1:7676`), OAuth with an owner token.
- DevSpace does **not** aggregate or proxy external MCP servers. There is no
  upstream/proxy machinery in `dist/`; the server only serves its own tool
  set. DevCodex's 11 MCP tools therefore cannot appear in ChatGPT's tool list
  through the existing connector — that would need a new ChatGPT connector
  (user action) plus its own auth story.
- What ChatGPT does get through DevSpace is a small fixed surface. In the
  default `minimal` tool mode: `open_workspace`, `read`, `write`, `edit`,
  `bash`. Two extension channels ride on top of it:
  - workspace instruction files (`AGENTS.md`/`CLAUDE.md`) — loaded and
    returned by `open_workspace`;
  - skills (`~/.agents/skills`, `~/.codex/skills`, workspace `.agents/skills`)
    — advertised by `open_workspace`, and the model is told to `read` a
    matching skill's `SKILL.md` before proceeding.
- The DevSpace server was **not running** (Funnel up, nothing on 7676).
  ChatGPT had been effectively disconnected until this work.

## Decision

Reuse the existing connector end to end: ChatGPT drives the DevCodex CLI
through its `bash` tool, discovery goes through a skill plus a pointer in the
workspace `AGENTS.md`. No new connector, no ChatGPT UI change, no new
credential, no changes to the OAuth setup.

For the Spectre this reuse is impossible — DevSpace is a filesystem-local
server. ChatGPT reaching box-side devcodex would need a separate DevSpace
deployment and a new connector; that stays out of scope (RUNBOOK §7.9 remains
the reviewed design for ChatGPT-driven box work).

## Roots widened to the whole home directory (user decision)

`allowedRoots` was `["~/Projects/devspace-demo"]`; the user chose to lift the
workspace restriction entirely, so it is now `["~"]`. DevSpace has no
deny-list, so this deliberately grants ChatGPT (via `open_workspace` +
`read` + `bash`) access to everything under `/home/person` — including
`~/.devspace/auth.json` (DevSpace's own owner token), `~/.ssh`,
`~/.qoder`, and agent credential dirs. The tradeoff was surfaced before the
change and accepted; the previous config is backed up at
`~/.devspace/config.json.bak-20260914`. Rollback: restore that file and
restart `devspace serve`.

## What was built (Zenbook)

1. `~/.agents/skills/devcodex/SKILL.md` — when to use, exact CLI commands
   (`bootstrap` → `changes`/`session-note` → `verify` → `complete`), and
   boundaries (no edit/exec tools; `.devcodex/` is internal state).
2. `~/.local/bin/devcodex` — wrapper:
   `exec node "$HOME/Projects/devspace-demo/devcodex/src/cli.js" "$@"`.
3. `~/Projects/devspace-demo` workspace support:
   - `.devcodex.json` with one gate (`npm test`, verified green: 84 pass);
   - `.gitignore` with `.devcodex/` so session state never shows up as a
     change;
   - an `AGENTS.md` section pointing multi-step tasks at the skill.
4. `tests/devspace-devcodex-e2e.mjs` — drives the real MCP endpoint exactly
   like the ChatGPT connector does: dynamic client registration, owner-token
   authorization with PKCE, then `open_workspace` / `read` / `bash` running
   the devcodex lifecycle in a scratch Git workspace.
5. `devspace serve` restarted and left running (`~/.devspace/serve.log`).

## Verification (run on the Zenbook, 2026-09-14)

- `node tests/devspace-devcodex-e2e.mjs --base http://127.0.0.1:7677`
  (scratch server, dummy state dir): **13/13 PASS**.
- `node tests/devspace-devcodex-e2e.mjs` (real server and config):
  **13/13 PASS** — skill advertised, skill readable via the `$HOME`-expanded
  path, and `bootstrap → changes → verify → session-note → complete` green
  through the `bash` tool.
- Re-run after widening `allowedRoots` to `~` (server restarted, log shows
  `allowed roots: /home/person`): **13/13 PASS**, with the scratch workspace
  created directly under the home directory — which is itself the proof that
  the old `~/Projects/devspace-demo` boundary is gone.
- Public path intact: `POST https://fedora.tail1fa7c9.ts.net/mcp` → `401`
  with `www-authenticate → resource_metadata=…/oauth-protected-resource/mcp`.

## Quirks and lessons

- DevSpace advertises skill paths tilde-formatted (`~/.agents/...`), but this
  build's `read` resolves `~` as workspace-relative and fails with ENOENT.
  The model retries with the expanded absolute path, which works (verified);
  still worth reporting upstream.
- Skill frontmatter must be valid YAML. A colon+space inside a plain
  `description:` scalar made the skill load fail silently — a folded block
  (`description: >`) works.
- Default tool mode is `minimal`: the shell tool is named `bash` (with
  `command`/`timeout` arguments), not `exec_command` (codex mode). The E2E
  handles both.
- The ChatGPT connection only exists while `devspace serve` runs. It is up
  now, ad hoc; a systemd user unit would make it durable (follow-up).
- Ops lesson: while stopping the scratch server, a too-greedy `sed` pulled
  the wrong PID and killed `ulauncher` (pid 5163). It was relaunched
  immediately with its autostart command (`ulauncher --hide-window`). Match
  the listener line before killing anything.

## Limits

- ChatGPT does not gain devcodex *tools*; it depends on the model reading the
  skill (or the AGENTS.md pointer) and running the CLI through `bash`.
- The demo workspace now has a real gate: `npm test` at the root recurses
  into `devcodex/test` (70 tests) and `artificial-development` (84 total,
  ~2 s).
- Nothing on the ChatGPT side was changed, so there is nothing to configure
  there; the OAuth store persists across server restarts, so no re-auth is
  needed.

## Follow-ups

- First real ChatGPT session: ask it to bootstrap in `devspace-demo` and
  complete; confirm `.devcodex/` appears in the workspace.
- Optional: systemd user unit for `devspace serve` (prefer units over ad-hoc
  background processes).
- Optional: report the tilde-path `read` bug upstream.
