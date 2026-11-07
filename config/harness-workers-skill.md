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

Start every packet with one exact kind line; the execution router uses it
to choose the burn tier:

    Kind: mechanical|implement|review|blocker

`mechanical` and `implement` run free first when the verified-free DSH route is
provisioned; `review` and `blocker` always stay paid ClinePass. A missing or
unknown kind stays paid. Free-route gaps are recorded reasons
(`free_unprovisioned`, `free_key_missing`, `free_profile_unsafe`) that fall back
to paid, never a silent paid launch. Read
`~/.local/state/remote-agent/free-tier-decisions.jsonl` before claiming which
tier actually ran.

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

To reuse a settled successful reader, include `terminal`, `reuse_of` (its last
completed Dispatch) and `terminal_incarnation` in the managed request. Reuse
requires matching native custody, the approved carrier and actual exited model
and executor processes. It preserves the reader, its slot and native retry
history; it does not relax capacity or create a second writer in the workspace.

Keep a Task failed or blocked when using `retry_of`; Orca reopens it natively.
For a recorded `task_not_startable` refusal with its exact original command and
request ID, `harness-workers recover --run <run> --launch <launch> --request <id>`
reads native state and recovers only that operation with `--retry-request`.
It never replaces the original launch/response files or permits another start
on an absent receipt alone. Pending native work and foreign requests stay owned.

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
