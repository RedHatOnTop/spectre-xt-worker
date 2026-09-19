# 2026-09-12 — pugc-ade staged on the box (second Efficient worker session)

## What changed

- `pugc-ade` (fedora: `~/Projects/distribution-project/pugc-ade`) pushed to
  the Spectre at the **same absolute path** with the repo's `warp.sh push`
  (720 MB / 6,895 files; `node_modules` excluded by the warp excludes,
  gitignored `assets/` included by operator choice, `.git` included). One
  file could not be read on the sender: `assets/.scratch/fakesrc/dump/.../
  b.webp`, a mode-000 test fixture — nothing in the repo references
  `fakesrc`, so no gate is affected. rsync exited 23 on it and `set -e`
  aborted `cmd_push` before the ZCode-session export and `write_last`: all
  files except that one landed, and the warp `last.json` still shows the
  2026-09-05 DarwinInspection pull. Nothing was lost by the abort — a
  `warp_zcode.py export` for this directory reports 0 sessions.
- Box hardening before the worker started: `remote.reference.pushurl` on the
  box copy set to `DISABLED-fetch-only-oracle` (the upstream reference repo is
  the only remote there; a stray `git push` must never reach it).
- Environment on the box: `npm install`, then the migration gate
  `npm run verify` — typecheck (4 projects), eslint, vitest (13 files / 235
  tests / 40.5 s), MCP stdio smoke (`SMOKE PASS`, 11 tools) — all green. Log
  kept at `/work/logs/pugc-verify-2026-09-12.log`. Caveat: the box's npm
  (10.9.8, vs fedora 11.19.0) rewrote `package-lock.json` on install (drops
  42 `libc` fields); reverted on the box, and the brief now tells the worker
  to `git checkout -- package-lock.json` if an install rewrites it again.
- `sudo spectre-doctor` on the box after staging: **25 passed, 0 failed**
  (pre-existing WARNs only: proxy/zcode optional since the worker stack, ufw
  inactive, ssh password auth, fail2ban — `harden-network.sh` still unrun).
- This repo's own gate re-run after the brief/devlog/RUNBOOK edits:
  `bash verify.sh` → all gates passed (shell syntax, py_compile, 20 unit
  tests; shellcheck skipped, not installed on fedora).
- New tmux worker session **`pugc`** (created 00:40 KST, cwd the repo):
  `~/.local/bin/qodercli -m Efficient --dangerously-skip-permissions`,
  kickoff `Read TASK-SESSION-1.md and execute it now.` The brief continues
  the review-hardening workstream as rounds 13-16 — each defect proven by
  reproduction, each round gated on `npm run verify` with pasted output, one
  `Harden round N: ...` commit per round, no pushes anywhere. The orca-rust
  `qoder` session is untouched.
- Box commits so far: `e826e5d` (worker commits the brief), `760a4b4`
  (operator: correct the brief's box spec to 2c/4t / ~3 GB free and add the
  lockfile guard); the worker is then into round 13 (edits under
  `packages/core/src/commands/ops/`, `packages/core/src/doc/device-props.ts`,
  `packages/core/test/commands.test.ts`).

## Why

The Efficient tier on the box is the zero-credit worker; pugc-ade's
review-hardening rounds were fedora-only until now. Same-path staging keeps
the sync story simple: `warp pull` from the pugc-ade directory on fedora
brings back the worktree **and `.git`**, so the rounds' commits arrive
together with the files.

## Caveats / follow-ups

- The installed `~/.local/bin/warp` on fedora is older than
  `scripts/warp.sh` (no-argument `warp push`/`pull` missing — the push here
  ran the repo script directly). Refresh with `bash scripts/install-warp.sh`
  on fedora when convenient.
- doctor's worker-stack gate still checks only the `qoder` tmux session;
  extending it (and healthcheck) to `pugc` was deliberately not done — the
  session is a work queue, not a service.
- Playwright browsers are not installed on the box; `npm run e2e` stays a
  fedora-side gate.
- RAM context: besides the new worker, three long-running qodercli instances
  live in Orca ADE terminal sessions (~3 GB RSS together, one at 1.6 GB);
  ~3.3 GB was available at kickoff. Close idle Orca terminals if vitest ever
  gets OOM-killed.
