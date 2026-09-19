# DevCodex

**Current version: 1.0.0-rc.1**

DevCodex is a compact single-agent workspace runtime for DevSpace. The host model owns reasoning. DevCodex supplies the context, durable task state, code navigation, workspace-scoped writes and command execution, verification, and completion evidence that help one coding agent stay effective across long sessions.

The project intentionally avoids becoming a multi-agent orchestrator. DevCodex keeps workspace writes and command execution inside the same pinned root and permission envelope as its memory and evidence tools, so one host agent works through a single surface.

## Design goals

- Keep the default MCP surface small enough that tool choice stays obvious.
- Persist only state that materially helps a coding agent resume work.
- Prefer composite operations over chains of low-level round trips.
- Keep source modification and process control inside the fixed workspace root and its permission envelope.
- Treat completion as a verified state, not a model declaration.
- Add no runtime dependencies.

## Core MCP surface

The default `core` profile exposes fourteen tools:

- `task_bootstrap` — start a durable task session, or recover one by id or latest-active state, while returning compact repository/task context.
- `note_task_session` — persist an important note, finding, decision, or verification event.
- `search_context` — search and return bounded surrounding source in one call.
- `code_navigation` — search symbols, outline a file, or find exact-identifier references from the cached symbol index.
- `read_many` — read several bounded file windows in one call.
- `inspect_file` — read a file window together with applicable `AGENTS.md` / `CLAUDE.md` instructions.
- `write_file` — write a text file inside the fixed workspace root, creating parent directories; the content replaces the file entirely.
- `edit_file` — replace an exact text occurrence, failing on a missing match and requiring `replaceAll` when the text occurs more than once.
- `run_command` — run a shell command with `/bin/sh -lc` in the workspace root and return exit code, bounded output, and timing.
- `show_changes` — return structured Git state, changed files, diff stats, and workspace fingerprint.
- `file_diff` — return the diff for one file instead of flooding context with a repository-wide patch.
- `verify_workspace` — run configured quality gates with captured evidence.
- `result_page` — page through an oversized result stored behind a handle.
- `completion_gate` — run verification, deterministic review, lifecycle hooks, optional checkpointing, and close the attached task session on success.

The `extended` profile exposes diagnostic and maintenance operations such as raw tree/search/read helpers, repository maps, skill inspection, review targets, checkpoints, environment/index inspection, hooks, permissions, journal events, workspace events, and runtime metrics. It is not intended for routine agent work.

## Normal agent lifecycle

Start a task with `task_bootstrap`. A new task automatically receives a durable session id:

```bash
node src/cli.js bootstrap "refactor authentication without changing the public API"
```

Persist only decisions worth recovering later:

```bash
node src/cli.js session-note SESSION_ID decision "Keep the public API stable"
```

Make workspace changes through `write_file` / `edit_file` / `run_command` (or the host's native tools — both work on the same pinned root). Use DevCodex search/navigation/diff tools to keep model context focused. Run verification while iterating, then finish with evidence:

```bash
node src/cli.js verify
node src/cli.js complete --session SESSION_ID --checkpoint ready
```

Successful completion marks the session `completed`, so it is no longer returned by latest-active recovery.

If a chat or model context is lost, recover the most recently updated active task with one call:

```bash
node src/cli.js bootstrap --resume-latest
```

Or recover an explicit session:

```bash
node src/cli.js bootstrap --session SESSION_ID
```

The bootstrap response includes the recent session timeline, original baseline, current workspace state, and whether the workspace drifted since the task began.

## Configuration

Create `.devcodex.json` in the workspace root:

```json
{
  "gates": [
    {
      "name": "tests",
      "command": "npm test",
      "timeoutMs": 120000
    },
    {
      "name": "lint",
      "command": "npm run lint",
      "required": false
    }
  ],
  "skillRoots": [
    "~/.codex/skills",
    "~/.agents/skills"
  ],
  "permissions": {
    "defaultProfile": "development",
    "profiles": {
      "development": {
        "read": "allow",
        "search": "allow",
        "review": "allow",
        "verify": "allow",
        "checkpoint": "allow",
        "sessionWrite": "allow",
        "environment": "allow",
        "shell": "allow",
        "fileWrite": "allow"
      }
    }
  },
  "mcpProfile": "core",
  "resultBudgetChars": 12000
}
```

The built-in default allows every action — the named actions (`read`, `search`,
`review`, `verify`, `checkpoint`, `sessionWrite`, `environment`, `shell`,
`fileWrite`) and any unnamed action through the same allow fallback. A repo
that wants a stricter posture names the profile in `.devcodex.json` and lowers
individual actions to `ask` or `deny`.

Required quality gates determine aggregate verification success. Optional gates are still reported but do not block completion.

## MCP server

Run one server per workspace so the allowed root is fixed when the process starts:

```bash
node src/cli.js mcp --root /absolute/path/to/project
```

Use `--profile extended` only for explicit diagnostics or maintenance.

For repeated sessions against the same workspace, a shared loopback daemon can keep the compressed search index hot and preserve the workspace event stream:

```bash
node src/cli.js daemon-serve --root /absolute/path/to/project
node src/cli.js mcp --root /absolute/path/to/project --daemon
```

The daemon authenticates local proxies with a random token stored under `.devcodex/`. Runtime state is excluded from workspace change evidence.

See [`docs/HOSTS.md`](docs/HOSTS.md) for the host boundary and registration model.

## Result bounding

Every MCP result is bounded by `resultBudgetChars` (12,000 characters by default). Oversized results are stored under `.devcodex/results/` and replaced by a handle plus preview. `result_page` retrieves only the slice the agent actually needs.

## Verification and release checks

```bash
npm test
npm run test:coverage
npm run check
```

The release candidate targets Node.js 20 or newer and has no runtime dependencies.

## Integration boundary

DevCodex exposes workspace-scoped write and command tools (`write_file`,
`edit_file`, `run_command`) alongside its context and evidence tools. The
workspace root is fixed when the server starts and every path resolves inside
it, so the tools cannot reach outside the pinned workspace. Persistent
processes and optional worktree isolation stay with DevSpace and the host
agent.

Runtime state lives under `.devcodex/`; source configuration lives in `.devcodex.json`.

## Design acknowledgements

Some runtime ideas were informed by public concepts in OpenAI Codex CLI and its app-server ecosystem. DevCodex is independently implemented, does not vendor Codex source, is not an OpenAI product, and is not affiliated with or endorsed by OpenAI.

See [`docs/CODEX-INSPIRATION.md`](docs/CODEX-INSPIRATION.md) for the current attribution and the pre-1.0 concepts that were intentionally removed.
