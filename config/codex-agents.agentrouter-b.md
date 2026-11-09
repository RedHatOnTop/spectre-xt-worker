# Global Agent Rules

## User-Facing Language
- Reply in English.
- Durable records, including comments, documentation, and devlogs, stay in English.

## Evidence and Measurement
- Prefer measurement and observation over estimates; check when feasible before claiming facts or results.
- Explicitly label unmeasured estimates and state their basis and uncertainty.

## Delegation and Review
- Delegate mechanical, repetitive, labor-intensive, straightforward, or otherwise low-difficulty implementation tasks to GPT-6 Luna (`gpt-6-luna`) subagents with clear scope and acceptance criteria.
- Personally review and verify delegated deliverables before accepting them or claiming completion.

## Work Ethic
- Do NOT be lazy, you do your best at every work.
- Take no shortcuts, skip no necessary investigation or verification, claim no partial work as complete, and honestly report remaining gaps within applicable constraints.

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

Sessions the user asks to migrate or create must be **visible and controllable
in Orca**: bring them up as an Orca-managed terminal by running the create from
**inside** the worktree with the active selector
(`cd <dir> && orca-ide terminal create --worktree active --title <t> --command
<cmd>`), never a bare tmux session or an invisible background process.
Use the active selector, never `--worktree path:<dir>`: the `path:` form returns
success and a connected, writable terminal while binding a worktree record the
ADE UI does not render, so the tab stays invisible to the operator (verified
2026-09-25).
The worktree must also be **registered** with Orca. Orca renders only worktrees
it knows, and `worktreeId` is a per-worktree instance id, not something derived
from the path — a raw `git worktree add` is not renderable. Create new checkouts
with `orca worktree create --name <n> --repo <selector>`. Before trusting any
create, confirm the returned `worktreeId` appears in `orca-ide worktree ps`.
If terminal creation returns a visibility warning, inspect its native workspace and tab identity and follow the CLI's exact reveal instruction. Input acceptance alone never proves that a model turn started.
The operator
checks and controls work remotely through the Orca ADE client. A worktree may
hold several live terminals. Use current Orca Run/Task/Dispatch identities for
supervised work and actual terminal handles for direct handoffs. The Qoder pin
registry and Slack resume controller are retired; never recreate them. Unknown
ownership or liveness stays unverifiable and never authorizes duplicate work.
<!-- spectre-offload:begin -->
<!-- Generated from config/offload-directive.md by scripts/install-offload-directive.sh; edit that file. -->

# Heavy work is offloaded, never run here

This box is an **agent runtime, not a compile farm**, and that is enforced, not
advice: `spectre-thermal-guard` samples every 60 s and **SIGTERMs** build-class
work (compilers, Gradle/JVM daemons and test workers, cargo/rustc, npm/pnpm/yarn
builds and installs, pytest/tox, `podman|docker|buildah build`) at every
temperature, and stops memory- or CPU-intensive non-agent processes as well. It
reports each stop to Slack `#fleet`.

All CPU- or memory-intensive work uses **AWS Burst disposable Spot instances**,
never Lightning Studio. The operator confirmed Lightning access is revoked
(HTTP 410 on 2026-11-09). The supported launcher is installed at
`~/.local/bin/burst-offload`:

```sh
PATH="$HOME/.local/bin:$PATH" burst-offload --repo <dir> [--ttl <minutes>] \
  [--setup '<one-time command>'] [--artifact <path>]... -- <command...>
```

`burst-offload` creates a disposable AWS Spot worker, syncs unignored project
files (including uncommitted edits), runs tests there, retrieves requested
artifacts and terminates **only its own** instance. Amazon Linux 2023 defaults
to Python 3.9: install/select Python 3.11+ for modern Python projects. Check
the wrapper's final JSON exit status and AWS credit via `aws-burst status`.
Never issue `aws-burst down --all` or `aws-burst reap` on the shared box.

Rules for every agent session on this machine:

1. **Before starting** any build, compile, test suite, container build, dataset
   job, model run, or anything expected to hold a core for more than ~60 s or use
   more than ~512 MB RSS: run it through `burst-offload`.
2. Local work stays **I/O-shaped**: reading, searching, editing, git, API calls,
   small scripts, Slack, service checks. If you are unsure whether a command is
   heavy, it is — offload it.
3. **Do not work around the guard** (no `nice`, no backgrounding a build, no
   running it from another worktree, no renaming a build to look like an editor).
   A blocked build is the guard working correctly.
4. Offloading is part of the task, not an optimisation. If a goal needs a build
   or a long test run, the goal is not done until AWS Burst tests pass.
5. A long offload may outlive the shell; follow the documented `burst-offload`
   wrapper recovery procedure, and never terminate another session's runner.

Guard internals: **RUNBOOK §7.20**. The old Lightning guide in §7.21 is
historical only and is no longer an authorized execution path.
<!-- spectre-offload:end -->

<!-- harness-workers:begin -->
Use one Orca coordinator and at most two implementation workers per Run.
Designated harnesses are DSH ClinePass (`dsh-worker`) and existing OpenCode Go
(`dsh-worker-go`), without new funding or paid fallback. Delegate implementation
and routine verification to these workers; the coordinator owns decomposition,
decisions and final evidence review. Do not launch additional Agentrouter workers.
Load the installed `harness-workers` skill and exact host Orca orchestration guide.
Use `harness-workers start --apply` from the native coordinator pane for bounded
implementation starts. Prefer `dsh-worker-go` for implement/mechanical packets when `free-route-status` reports available; `dsh-worker` stays the explicit DeepSeek/ClinePass choice. Raw starts are not intercepted; do not bypass this path.
Native Run/Task/Dispatch owns execution, not the adapter's launch receipt journal.
Unknown starts/liveness retain capacity and require original-request recovery.
Never kill/restart existing sessions to free a slot or evade a wait. Older Orca
interfaces require a verified session-preserving update, not weaker identity
checks. Existing production, publication, cost and device-trust approvals remain.
<!-- harness-workers:end -->
