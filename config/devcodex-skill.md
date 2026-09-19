---
name: devcodex
description: >
  DevCodex workspace companion CLI (installed at /usr/local/bin/devcodex). Use it
  when a coding task should leave a durable session record or pass a project
  completion gate - bootstrap a session, record decisions, verify, complete.
  Trigger words: devcodex, session record, completion gate, verify before done.
---

# DevCodex

DevCodex is a local CLI that adds a durable task session and a completion gate on top of an ordinary Git worktree. It reads and writes only inside the fixed workspace root and runs commands there (including the project's own gate commands from `.devcodex.json`), so the host's native tools stay optional.

Source and docs: `/usr/local/share/devcodex` (README.md, docs/).

## When to use

- a multi-step coding task that may span turns or conversations
- the user asks for verification, a session record, or proof of completion
- the project has a `.devcodex.json` with quality gates

## Workflow

Run these through the shell tool with the working directory at the project root. Each command prints JSON with `--json`.

```bash
devcodex bootstrap "<task title>" --json      # repo map + new session; keep the returned session id
devcodex changes --json                       # current change evidence
devcodex verify --json                        # run the project gates without closing the session
devcodex session-note <id> decision "<text>"  # record a decision or important note in the session
devcodex complete --session <id> --json       # gates + deterministic diff review; closes the session
```

- Change and exercise the workspace through the same tool: `devcodex write <path> [--from <file>]` (stdin when `--from` is absent), `devcodex edit <path> --old "<text>" --new "<text>"` (add `--all` for repeated matches), `devcodex run "<command>"` — all run inside the fixed workspace root.
- Resume recent work: `devcodex bootstrap "<title>" --resume-latest --json`
- Gates come from `.devcodex.json` (`{"gates":[{"name":"tests","command":"npm test"}]}`). A project with no gates still completes — review-only evidence. Gate commands run through `/bin/sh -lc`, so use commands the project already runs.
- If `devcodex` is not on PATH, invoke `node /usr/local/share/devcodex/src/cli.js` with the same arguments.

## Rules

- `.devcodex/` is internal session state, not project output. Never edit it, and keep it out of commits (it is gitignored).
- The write/edit/run tools and the host's native tools act on the same workspace; whatever made the edit, use `devcodex changes` and `devcodex complete` for evidence.
- `complete` is the done-check: run it before claiming the task finished and report its gate results.
