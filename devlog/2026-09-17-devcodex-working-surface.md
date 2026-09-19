# 2026-09-17 — devcodex on the Spectre gets a working surface

## Requirement

User: "이거 지금 있잖아, 스펙터에 있는 devcodex의 안전가드가 여전히 너무
빡빡함" — after the 2026-09-16 relaxation the remaining friction was inside
devcodex itself, not the Codex hook. A scope question (Codex hook git
policy / multi-agent sandbox / devcodex itself / other) was answered
**devcodex itself**; a follow-up offered three moves and the user picked
all three: complete-gate relaxation, permission full allow, and
file-write/exec tools.

Evidence gathered on the box before touching anything:

- Every devcodex session on the box is stuck `active` (flagship-games
  rev 15, release-readiness-spectre rev 23, two adverse sessions at
  rev 1), and a Codex session log (09-15) records "`devcodex complete`
  was not run. Session notes retain the evidence instead." The cause:
  those workspaces have no `.devcodex.json` gates, and `complete`
  refused to close a session with `no-quality-gates-configured`.
- `devcodex permission write|delete|complete` returned `deny` (unnamed
  actions) — structural, since every shipped tool is mapped to a named
  action and ran regardless.
- The Codex hook guard (`irreversible-guard.mjs`) has zero refusals in
  the box's session logs; the git policy inside it was not the friction.

## What was done (repo — verify.sh green)

- **complete without gates.** `devcodex/src/orchestrator.js`: with no
  gates, verification is `{ ok: true, reason: 'no-quality-gates-configured' }`
  and the completion message records it. The deterministic review still
  gates completion (conflict markers, key material → high). CLI `verify`,
  CLI `checkpoint --verify`, MCP `verify_workspace` and `create_checkpoint`
  follow the same rule.
- **permission full allow.** `devcodex/src/permission.js`: named actions
  keep explicit rules (plus the new `fileWrite`); unnamed actions fall to
  the same allow. `explicit` still reports whether a rule was spelled out,
  so the journal keeps the distinction.
- **working surface.** New `devcodex/src/fsops.js`: `write_file`,
  `edit_file`, `run_command` resolve through the same workspace-root check
  as the read tools (writes cannot leave the pinned root); commands run
  `/bin/sh -lc` in the root with optional timeout and bounded output.
  They join the core MCP profile (11 → 14 tools) and the CLI
  (`devcodex write` / `edit` / `run`, RUNBOOK §7.13 table).
- **docs/tests.** README, docs/HOSTS.md, `config/devcodex-skill.md`,
  `config/codex-global-AGENTS.md`, RUNBOOK §7.13; tests: permission, mcp,
  orchestrator (no-gates completion), cli-surface updated; new
  `devcodex/test/fsops.test.js`; `tests/devcodex-smoke.mjs` now exercises
  write/edit/run and expects 14 tools (12 checks). Tree pin moved
  `b9f50632…` → `df491a0e…` in `scripts/install-devcodex.sh`.

## Evidence

Repo, on the daily driver: `verify.sh` — all gates green (devcodex suite
77/77 including the six new fsops tests; 181 unit tests; bridge tests).

**Redeployed on the box 2026-09-17 23:40–23:44 KST** (user-approved after
the hold). The hold happened because the box was actively running a Codex
session (`codex resume 01a0a49c… --yolo`, PID 2213244) with a devcodex
session updated minutes earlier; the user asked to stop and be told before
any restart. What the restart actually is became the second lesson (below).

Tree shipped to `~/remote-agent-deploy/remote-agent`, installed via
`install-devcodex.sh`, skill notes refreshed
(`~/.agents/skills/devcodex/SKILL.md`, `~/.codex/AGENTS.md`), ChatGPT
connector restarted (`systemctl --user restart devspace`; back to 401 on
`/mcp` ~48 s later). Observed, not inferred:

- smoke: "all checks passed" (12 checks; write/edit/run exercised; core
  profile lists 14 tools);
- vendored suite on the box: 77/77 with `SPECTRE_MOTD_DONE=1`;
- `devcodex permission fileDelete --json` → `{"decision":"allow","explicit":false}`;
- `devcodex mcp` core profile length: 14;
- `devcodex daemon-status` → `unconfigured` (no daemon on this box);
- `devspace` active, listening on 7676, `GET /mcp` → 401.

**What "devcodex" is on this box (established the hard way).** devcodex is
**ChatGPT-only**: ChatGPT reaches the box through the DevSpace connector
(Funnel → 7676) and runs the `devcodex` CLI with its `bash` tool; the
terminal agents (codex resume sessions, gemini, qoder workers, the
Flash/Efficient control plane under `wt/release-readiness-spectre/.devcodex/`)
keep their own tooling and never call devcodex. So "install devcodex, then
restart it" means: install the tree, refresh the two skill/AGENTS notes, and
restart **devspace** — there is no devcodex process, daemon, or unit to
restart, and the codex terminals are not part of the reload. Recorded in
RUNBOOK §7.13 and the root AGENTS.md.

## Honest limits

- devcodex's read-only posture was a deliberate design ("DevSpace remains
  the execution layer"); this inverts it for the box. The destructive
  barrier stays with the agent guard hook, but that hook watches the
  agent's own tool calls — `devcodex run` is a path around it. The pinned
  workspace root bounds where writes and commands start, not what a
  command can reach.
- The installer's `~/.claude/CLAUDE.md` block is append-only; the box's
  already-installed copy of that note is not rewritten by a redeploy.
- Upstream (`~/Projects/devspace-demo/devcodex`) is untouched — the trees
  now differ on permission.js, orchestrator.js, tool-registry.js, cli.js,
  mcp.js plus the new fsops.js; re-vendoring must re-apply and re-pin.

## Follow-ups

1. Done 2026-09-17 23:44 KST (see Evidence). The reload path is documented
   in RUNBOOK §7.13.
2. Optional: add `.devcodex.json` gates to flagship-games /
   release-readiness-spectre for real verification evidence — no longer
   required for completion.
3. The `~/.codex/skills/devcodex/` copy does not exist and was not created;
   the DevSpace-advertised path is `~/.agents/skills/devcodex/SKILL.md`.
