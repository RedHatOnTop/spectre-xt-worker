# Control-plane continuation — 2026-09-21

## Scope and gates

Continue Grok session `01a0be87-a8da-79d1-abdd-2fdcb7ff8859` against the actual tree, not its completed checklist.

1. Runtime safety: regression tests for cached GET, assignment ownership, notification failure, strict next-goal input, caps and duplicate prevention. Gate: real store/API and loop CLI tests.
2. Minecraft planning: provider probe/launch, correct planner pin, persistent assignment and packet queue. Gate: fake external executables plus real state daemon; no repeated planner send, sequential packets, failure recovery.
3. Machine hygiene: process-backed pin sync and reaper with dry-run units. Gate: fake proc/listener fixtures and a disposable live child, never a real worker kill.
4. Delivery: full `bash verify.sh`, repeated CLI integration, installed-layout smoke tests, documented Spectre commands. Live flags remain off until on-box gates pass.

## Initial findings

- HEAD `ecb5d7d` is clean. The prior session reported tests passing but missing runtime paths remain.
- `spectre-astra`, provider health, planner prompt/schema, reaper units are absent.
- Pin-sync prints a constant success; reaper without a fixture scans nothing.
- Plan dispatch selects the Efficient pin, bypasses claims, and never starts ASSIGNING.
- Loop lacks advance claims, planner responses, caps, and fingerprint protection. Notification failures are persisted as successes. Malformed next-goal JSON is accepted as prose.
- GET still reads SQLite under the writer lock instead of using the published snapshot.
- Spectre SSH requires Tailscale check approval. On-box behavior is unverified.

## Decisions

- Keep the single occupancy authority and single terminal writer. Add small runtime modules rather than another classifier.
- Preserve installed-off automation and dry-run reaping. Do not spend model quota during test runs.
- A planner cannot both use no tools and write a file. Permit only the exact result-file write in its prompt; no project edits, subprocesses, or follow-up messages.
