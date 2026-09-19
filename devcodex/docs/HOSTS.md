# Host integration

DevCodex is a workspace-pinned stdio MCP server. The host model remains the coding agent; DevCodex does not spawn or coordinate other models.

## Direct mode

Register the following command with an MCP-capable host, replacing both paths as needed:

```bash
node /path/to/devcodex/src/cli.js mcp --root /path/to/project
```

The project root is fixed when the server starts. MCP tools do not accept arbitrary roots, which keeps path validation simple and prevents the server from becoming a general filesystem tool.

The default profile is `core`. It is the recommended mode for normal coding work because the model sees only the daily-driver tools.

## Shared daemon mode

For repeated contexts against the same workspace, start one loopback daemon:

```bash
node /path/to/devcodex/src/cli.js daemon-serve --root /path/to/project
```

Then configure the host-side stdio process as:

```bash
node /path/to/devcodex/src/cli.js mcp --root /path/to/project --daemon
```

The daemon keeps the search index hot, maintains the filesystem event cursor, and shares durable DevCodex runtime state across reconnecting proxies.

## Recommended host flow

1. Call `task_bootstrap` with the task. This creates a durable task session and returns compact repository context.
2. Use `code_navigation`, `search_context`, `inspect_file`, and `read_many` to narrow the implementation surface.
3. Use `write_file` / `edit_file` / `run_command` (or the host's native tools) for actual workspace changes.
4. Add `note_task_session` events only for decisions or findings that matter after context loss.
5. Use `file_diff` / `show_changes` and `verify_workspace` during iteration.
6. Call `completion_gate` with the session id before claiming completion.
7. In a fresh context, call `task_bootstrap` with `resumeLatest=true` or an explicit `sessionId`.

This division of responsibility is intentional: DevCodex owns context, recovery, evidence, and workspace-scoped writes and commands; the host owns reasoning.
