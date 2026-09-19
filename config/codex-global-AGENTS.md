# Global Agent Rules

## NO AI SLOP (CRITICAL)
When designing or building UI/UX:
- NO emojis in the UI.
- NO "AI Slop" purple colors.
- NO excessive use of neon colors or gradients.
- Keep the UI design clean, professional, and utilitarian.

## ENGLISH DOCUMENTATION ONLY (CRITICAL)
- All code comments, docstrings, README files, commit messages, and markdown documentation MUST be written strictly in English.

## Immutability (CRITICAL)
ALWAYS create new objects, NEVER mutate:
```javascript
// WRONG: Mutation
function updateUser(user, name) {
  user.name = name  // MUTATION!
  return user
}

// CORRECT: Immutability
function updateUser(user, name) {
  return { ...user, name }
}
```

## File Organization
MANY SMALL FILES > FEW LARGE FILES:
- High cohesion, low coupling
- 200-400 lines typical, 800 max
- Extract utilities from large components
- Organize by feature/domain, not by type

## Error Handling
ALWAYS handle errors comprehensively:
```typescript
try {
  const result = await riskyOperation()
  return result
} catch (error) {
  console.error('Operation failed:', error)
  throw new Error('Detailed user-friendly message')
}
```

## Input Validation
ALWAYS validate user input:
```typescript
import { z } from 'zod'
const schema = z.object({
  email: z.string().email(),
  age: z.number().int().min(0).max(150)
})
```

## Code Quality Checklist
Before marking work complete:
- [ ] Code is readable and well-named
- [ ] Functions are small (<50 lines)
- [ ] Files are focused (<800 lines)
- [ ] No deep nesting (>4 levels)
- [ ] Proper error handling
- [ ] No console.log statements
- [ ] No hardcoded values
- [ ] No mutation (immutable patterns used)

## Browser (spectre box)

`obscura` — a headless browser, no Chromium — is on PATH: `obscura fetch <url> --dump text`,
`--eval "<js>"`, `-s page.png` for screenshots, `--allow-private-network` for local dev servers.
It is also registered as the `obscura` MCP server, and a CDP endpoint for Playwright/Puppeteer
scripts listens on `127.0.0.1:9222`.

## Workspace companion (spectre box)

`devcodex` is on PATH — durable task sessions, code navigation, workspace-scoped writes
and commands, and evidence-backed completion. Run it from the repo you are working in
(or pass `--root <path>`): `devcodex bootstrap "<task>"` starts a recoverable task
session, `devcodex write` / `edit` / `run` change and exercise the workspace,
`devcodex verify` runs the quality gates configured in the repo's `.devcodex.json`, and
`devcodex complete --session <id>` closes the task once review passes (gates run when
the repo configures them). Runtime state lives under `.devcodex/` in the workspace.
MCP form: `devcodex mcp --root <path>` (one server per workspace).

## Session handoff (spectre box)

`codex-handoff` moves a Codex CLI session between this box and the daily
driver: `codex-handoff push` (from the project directory) hands the newest
session for that directory over, `codex-handoff pull` brings one back, and
`codex-handoff list` / `status` show what is where. The receiver resumes
with `codex resume <uuid>`. Handoffs are additive — an identical session on
the peer is a no-op, a differing one is refused unless `--replace` is given,
and nothing is ever deleted. See RUNBOOK §7.15.

When the user asks for a session to be migrated or created on this box
(handoff, warp, or any long-running agent session), it must end up
**visible and controllable in Orca**: bring it up as an Orca-managed
terminal in the project's worktree
(`orca-ide terminal create --worktree path:<dir> --title <t> --command
<cmd>`), not a bare tmux session or an invisible background process.
The operator checks and controls work remotely through the Orca ADE
client. A worktree may hold several live terminals (a handoff session
beside the qoder worker); the Slack bridge dispatches unambiguously
through the worker's `terminal` pin in `config/qoder-workers.json`, so
when a second live terminal appears in a worker's worktree, pin the
worker terminal.
