---
name: harness-workers
description: Use for supervised implementation with one Orca coordinator and a bounded DSH ClinePass/OpenCode Go worker pool, including Fedora and Spectre. No direct coordinator implementation or alternate agent-spawn path.
---

# Bounded implementation harnesses

One orchestration session has one coordinator and at most two implementation
workers. Approved native agents are `dsh-worker` (existing ClinePass) and
`dsh-worker-go` (existing OpenCode Go). No new funding or paid fallback.

The coordinator owns scope, decomposition, native Task creation, questions and
final evidence review. It delegates source edits, routine investigation and
verification to the designated implementation harnesses. It does not implement
the packets itself or launch extra Agentrouter sessions. Batch independent work
into bounded packets; do not spend a high-tier turn reporting unchanged progress.

Load the exact host's Orca `orchestration` and `orca-cli` guides. Native ownership
and approval rules remain authoritative. Bind the existing Run, never create a
replacement Run to evade a wait or capacity limit. Existing live/unverifiable
workers and settled-but-unreleased terminals occupy capacity. A timeout does
not free a slot. Give settled resources a native retain/reuse/release decision
after evidence review; never kill unknown work to launch a third worker.

Create each implementation Task natively with exact ownership, target paths,
prohibitions, observable acceptance and stop conditions. Use a separate registered
workspace for each independent writer; do not run implementation in the
coordinator's checkout. A ready Task with result/completed_at needs review first,
not another execution. Use existing explicit subscription profiles unchanged.

From the current registered coordinator pane, pass the exact request on stdin:

```sh
printf '%s\n' '{"launch_id":"<unique operation id>","run_id":"<existing run>","task_id":"<ready task>","agent":"dsh-worker","worktree_id":"<registered id>"}' \
  | harness-workers start --apply
```

On a running Orca that has no native `dsh-worker` entry, the host may bind
these names to spare unused single-executable command overrides. That binding
does not restart Orca, does not retarget an agent that is open or installed,
and does not change coordinator ownership. Starts still go only through
`harness-workers start --apply`.

Without `--apply`, no native start occurs. The policy is private at
`~/.config/remote-agent/harness-workers.json`; `harness-workers policy` reads it
without touching native state. `launch_id` identifies one durable native start,
not a new Task. Reuse the same launch ID only for readback of that exact request.
Unknown native response blocks additional managed starts. Preserve its original
stdout/stderr/request receipt and follow the exact native recovery operation;
never generate another ID as a retry workaround.

When an exact completed native workerStart request is known, reconcile it with
`harness-workers reconcile --run <run-id> --launch <original-launch-id> --request <native-request-id>`.
This reads the native receipt, appends reconciliation evidence and starts
nothing. A foreign Run/Task/agent/request cannot clear the original uncertainty.

The managed start path reads current native inventory and uses a per-Run process
lock; its journal stores launch intent/response, not worker occupancy. Raw Orca
commands are not intercepted by this adapter. Do not bypass it for implementation
starts. Refuse older native interfaces instead of weakening ownership checks.

Use native FIFO check/reply/worker_done contracts and process complete Deliveries
before acknowledgment. For repeated unchanged waits, retain structured dependency
state and use deterministic observation/event delivery rather than repeatedly
calling Agentrouter. Where the continuity timer is not accepted, report that
explicitly; a stopped coordinator turn is not completed work.

Host reboot, runtime replacement and production/publication operations retain
their separate approvals. Device synchronization is not implemented: credentials,
session capabilities and device paths must never be copied as policy sync data.
