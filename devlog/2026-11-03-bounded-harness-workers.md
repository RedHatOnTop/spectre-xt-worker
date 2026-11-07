# Bounded implementation harnesses across hosts — 2026-11-03

## Confirmed scope

- One orchestration coordinator per session; delegate implementation to at most
  two DSH workers, using existing ClinePass and OpenCode Go only. Coordinator
  spends its high-tier quota on decomposition, decisions and final review.
- Apply the same policy to Fedora and Spectre without creating another worker
  occupancy authority. Orca Run/Task/Dispatch and current terminal resources
  remain authoritative. Existing sessions are not terminated to free slots.
- A unified cross-device harness and synchronization of user-owned harness
  improvements are future direction only, not an implemented feature here.
- Fedora currently runs Orca 1.4.183: no formal dsh-worker registration and no
  current paginated worker-list interface. Asked for session-preserving runtime
  transition approval. Do not patch or restart its running archive without it.

## Subphases and gates

1. Build a portable reviewed policy and native start adapter. Gate: red tests
   for two-worker cap, exact coordinator identity, permitted routes, complete
   inventory, concurrent start and uncertain native response. No duplicated
   occupancy DB or coordinator pretending to implement work.
2. Add durable native launch intent and bounded coordinator instructions. Gate:
   actual subprocess fake-native acceptance and replay after process restart;
   ambiguous starts block a new launch rather than duplicating workers.
3. Full lint/compile/test and at least 80% changed-module coverage. Prove current
   native read-only preflight on both hosts. Actual DSH launch on Fedora follows
   only verified registration/version transition and session-preservation audit.

## Previous subphase retained evidence

- Native workspace correction uses existing collect_workspaces rather than a
  raw capped worktree ps. Red15 tests: five failures/two errors; green15, native
  offload9 and real installed-process workspace2 tests passed. Full1243 Python,
  123 Node,132 DevCodex exit0; focused457, assignment branch90%.
- Staged actual Spectre existing coordinator pane confirmed 131 complete
  workspace records and original identity; no new worker or native mutation.
  This current read was not truncated; injected process tests prove expansion.
- Same-Run task review policy question is pending. Ready task has prior evidence
  and cannot be relaunched solely because status is ready. Existing production
  timer remains disabled and original runtime files remain restored.

## Implementation and acceptance

- `worker_pool.py` and `worker_launch.py` enforce the selected maximum on their
  managed native start path. Occupancy is read from current scoped native
  resources, including live/unverifiable and settled-unreleased terminals.
  Same-terminal retry history is deduplicated by endpoint/handle. Unknown
  workspace ownership blocks a new writer; separate registered workspaces are
  required. No worker lifecycle or slot ledger is copied into SQLite.
- Per-Run start lock serializes managed launches. Write-once intent and response
  keep exact command/coordinator/Task/agent/workspace. Unknown response blocks
  repeated and later managed starts. An exact completed native workerStart
  request appends reconciliation proof without changing the original response
  or starting another worker; crash without response receipt is recoverable
  the same way. Raw Orca starts are not intercepted and must not be claimed to
  be globally constrained by this adapter.
- User installer stages a complete immutable hash-named import closure, verifies
  reused bytes, writes private policy/skill, preserves existing Codex rules with
  a backup, and atomically promotes only its owned command symlink. Broken
  source, conflicting policy, malformed memory markers and symlinked memory
  fail before policy/global-rule changes. It never restarts an agent/runtime.
- Tests: 26 pool cases, six user installer cases and four real installed-process
  flows. Native protocol fixture proves first ClinePass + second Go launch,
  third capacity wait, no-op original response replay, lost response blocking,
  exact native reconciliation and dry-run. This is not actual DSH execution.
- Branch coverage: worker launch 89%, pool 91%, installer 86%, aggregate 89%.
  Focused `test_harness*.py`: 36 tests, OK. Full current gate remains in progress.
- Fedora transition approved with preservation/resumption of existing sessions.
  Initial exact inventory: 15 terminals and 49 registered workspaces; no
  truncation. No runtime stop, archive replacement or worker launch yet.
- Downloaded official Orca v1.4.212 Linux AppImage through the bounded disk-I/O
  wrapper. Official SHA256 cfa84bde134b3108db32b3bf3bb60be9b69edbe54fb5aa06e5c902d82b3f3df7
  matches. Extracted into private transition stage, not the installation.
- Official 1.4.212 shares six existing registration module pins but not main
  digest or the Spectre chunk path. Exact read failed with
  `KeyError: 'out/main/chunks/daemon-cgroup-scope-BJEqXHBB.js'`; current official
  file is daemon-cgroup-scope-TzNKxEon.js and the old title anchor is absent.
  Do not copy the modified Spectre archive or loosen anchors. Derive a separate
  reviewed official-build registration/readiness adaptation before transition.
- Long-term cross-device synchronization remains reference only. No credential,
  capability, profile or session record synchronization was implemented.
