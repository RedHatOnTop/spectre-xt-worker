# 2026-09-14 — DevCodex on the Spectre: ChatGPT's workspace runtime, vendored

## Requirement

User: "ChatGPT가 이 머신에 구현한 DevCodex라는게 있는데 아예 그거를
스펙터에 넣자" — the DevCodex that ChatGPT built on the daily driver
(developed 09-09 → 09-10 against DevSpace, sitting in
`~/Projects/devspace-demo/devcodex` as v1.0.0-rc.1) should run on the
box. This came out of the earlier "can ChatGPT notice new #lobby
reports" question: instead of picking one of the two proposed paths
(deploy §7.9 CodexPro, or a box-side auto-proposer), the user chose
their own ChatGPT-built artifact.

## What DevCodex is

A compact single-agent workspace runtime: ~55 files, zero runtime
dependencies, Node >= 20. Two forms — a CLI and a workspace-pinned stdio
MCP server (`devcodex mcp --root <path>`, 11 core tools). It is the
memory-and-evidence layer, not an execution layer: no file-write or
process tools by design (DevSpace/host owns edits and execution), so it
does not compete with the box's guards. Core value for this box:
`bootstrap` creates or recovers a durable task session (`.devcodex/`
state), `verify` runs the workspace's `.devcodex.json` gates, and
`complete` treats completion as a verified state (gates + deterministic
diff review + session close) rather than a model declaration. Upstream
tests: 70, green.

## What was done

- Vendored the tree into this repo as `devcodex/` (src, docs, test,
  package.json, README.md; runtime state and the devspace-demo workspace
  config excluded). `verify.sh` now runs the vendored suite.
- `scripts/install-devcodex.sh`: idempotent, staged swap to
  `/usr/local/share/devcodex`, `/usr/local/bin/devcodex` wrapper, a
  managed block in `~/.claude/CLAUDE.md`, and a user smoke (bootstrap in
  a scratch git workspace). No network, no daemon, no service.
- `scripts/bootstrap.sh` calls the installer after obscura;
  `scripts/doctor.sh` gained a devcodex section (CLI on PATH + MCP
  handshake); `config/codex-global-AGENTS.md` gained a "Workspace
  companion" section (also appended to the box's `~/.codex/AGENTS.md`
  once, so Codex sees it now rather than next codex reinstall).
- `tests/devcodex-smoke.mjs`: CLI lifecycle (bootstrap → changes →
  verify → session-note → complete) plus an MCP stdio handshake
  expecting exactly the 11 core tools. 9 checks.
- RUNBOOK §7.13, README line.

## Deployed and verified on the box (2026-09-14 ~20:05–20:15 KST)

- installer green: tree at `/usr/local/share/devcodex`, wrapper on PATH,
  smoke `bootstrap: ok`.
- `node devcodex-smoke.mjs` — 9/9 PASS on the box.
- vendored suite on the box — 70/70 with `SPECTRE_MOTD_DONE=1`.
- `devcodex tree` / `devcodex changes` against the live orca-rust
  checkout — reads and reports as expected.
- `sudo spectre-doctor` — 43 PASS, 0 FAIL, devcodex section included.

## Three things the box taught us

- **Locale hashing.** The installer's tree pin was first computed with a
  plain `sort`; the same tree hashed differently on the box (POSIX
  collation) than on the daily driver (UTF-8). The pin check caught it
  before install — working as designed — and now uses a C-collated hash.
- **`bootstrap` needs a git repo.** In a non-git scratch dir it hard-fails
  on `git diff HEAD`; the installer smoke now creates a one-commit repo
  first.
- **The SSH MOTD banner leaks into gate stdout.** Over SSH,
  `/etc/profile.d/spectre-motd.sh` prints on login, and devcodex runs
  gates via `/bin/sh -lc`, so the banner lands in every gate's captured
  stdout. Harmless for exit codes, but it fails the vendored suite's
  exact-stdout gate test; run the suite with `SPECTRE_MOTD_DONE=1` (the
  banner's own guard).

## Honest limits

- **No MCP registration by default.** DevCodex pins one workspace per
  server; registering it globally would put 11 tools × every workspace
  into every agent's tool surface. RUNBOOK §7.13 carries the one-liner
  per workspace (`claude mcp add -s user devcodex-<ws> -- ...`).
- **DevSpace itself is not deployed.** DevCodex was built to pair with
  DevSpace (the ChatGPT-facing execution layer). Putting DevSpace on the
  box — OAuth, workspace roots, possibly a tunnel — is a separately
  reviewed security decision; §7.9 remains the reviewed design for
  ChatGPT-driven work on the box. So this does not yet give ChatGPT
  direct box access.
- **No agent has used it yet.** Discoverability is the CLAUDE.md block,
  the Codex AGENTS.md section, and RUNBOOK. First real use is pending;
  one natural hook is the goal-completion protocol clause, which is not
  changed here.
- **The vendored copy is a snapshot.** ChatGPT keeps evolving the project
  in devspace-demo; re-vendoring is manual (update the tree and
  `DEVDCODEX_TREE_SHA256` together). The embedded `serverInfo` version
  string says 0.7.0 while the package says 1.0.0-rc.1 — upstream
  cosmetic, left alone.
- Not committed: like the rest of the 09-13/09-14 work, this sits in the
  working tree (repo has been uncommitted since 09-12 18:00).

## Follow-ups

1. First real use by a box agent; consider pointing the goal-completion
   clause at `devcodex verify` / `complete` where a repo has gates.
2. Decide whether DevSpace joins the box (the ChatGPT-direct path).
3. Re-vendor when upstream changes land.
