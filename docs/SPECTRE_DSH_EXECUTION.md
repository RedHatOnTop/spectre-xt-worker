# DSH execution continuity

Status: formal native DSH placement and same-session native retries verified on
ClinePass and OpenCode Go. Unattended continuity activation remains open.
Runtime authority remains Orca.
The receipt adapter emits no DSH status events. The approved formal launcher
adds a version-pinned Orca registration overlay; it does not replace ownership.

## Boundary

A native Dispatch is the assignment authority. An execution receipt records
only a concrete OS execution of that Dispatch on its execution host. It never
allocates capacity, copies Task state, changes ownership, or treats shell exit 0
as Task success. Visible Orca panes remain the only worker launch surface.

The historical headless path used low-level dispatch without injection for its
expressiveness gap. Orca labels that resource external/unsupervised. The native
Task/Dispatch still owns the work; the execution adapter supplies exact process,
result and interruption evidence rather than invented native liveness.
The verified formal launcher below replaces that placement step without
changing receipt or recovery semantics. Use it for new supervised work.

## Subphases and gates

1. Execution receipt substrate: immutable native assignment binding, private
   write-ahead attempt files, exact boot/PID/start identity, atomic real exit
   recording, and duplicate launch refusal. Gates: real child exit and crash
   injection, PID-reuse and boot-change fixtures, private paths, source digest
   conflicts, concurrent attempts, and restart inspection; no provider call.
2. Native binding and executor: read current dispatch/preamble using the actual
   worker pane; validate exact Task/Dispatch/Run/pane/workspace before spawn;
   run the existing isolated ClinePass or Go profile explicitly. Gates: no
   credential/provider rewrite, actual native worker_done on bounded canaries
   for both routes, real artifact readback, and external resource accounting.
3. Completion and recovery: persist actual exit before publishing review-only
   execution context to the existing coordinator queue. Native worker_done,
   not process exit or model prose, settles Tasks. Gates: duplicate queue
   publication is a no-op, queue failure retains unsent execution evidence,
   failure retains native assignment and source input, no live/unknown retry.
4. Operational integration: coordinator instructions and operator-visible CLI
   use the same receipt for normal completion and recovery. Gates: source and
   installed hashes, complete repository verification, >=80% changed-module
   branch coverage, real provider-failure/turn-end/input-during-work readback,
   and controlled timer acceptance. Host reboot remains separately unverified.

## Receipt and process rules

- Commit one native-bound execution intent before spawning. Bind exact native
  Run, Task, Dispatch, terminal, pane, workspace, immutable prompt digest and
  selected explicit route. Store no API key or Dispatch capability in receipts.
- Each Dispatch gets at most one original OS execution. Refuse a second launch
  even if the original process is absent. A retry needs a new native attempt,
  explicit positive exit evidence, and the coordinator's native retry decision.
- Record boot ID, wrapper PID/start ticks, child PID/start ticks and actual
  wait status. Observation reads these on the execution host; it never signals
  the process or infers death from duration, a stale timestamp or missing hook.
- A missing/reused PID after a matching host boot is exit/interruption evidence,
  not Task completion. A changed host boot or unreadable process identity is
  reconciliation-required. No observer launches a replacement.
- Persist exit atomically and preserve full visible output. DSH turns and jobs
  are distinct: do not kill work merely because a model turn ends. The existing
  bounded packet's wall timeout remains an explicitly recorded execution limit,
  not a worker lease; it cannot become unattended permission to retry.
- A completion notification is review-only context. A failed transport leaves
  the execution receipt intact and unpublished; requeue the same immutable
  event ID, never rerun the worker to obtain another receipt.
- Original DSH sessions and isolated provider profiles are preserved. Direct
  Go uses the verified session-header adapter/home; no default route or funding
  fallback is permitted. Native `unverifiable` remains literal when Orca cannot
  attribute DSH status. OS evidence is reported separately.

## Historical external-dispatch acceptance

The original coordinator created exact native Tasks and external Dispatches in
the existing registered canary pane. The installed adapter reads current native
assignment/preamble itself, verifies packet equality and exact process
incarnation, commits intent, then runs the explicit route through the additive
`dsh-superagent-worker`. No terminal title or copied ID supplies authority.

```sh
spectre-dsh-execution run --task <native-task-id> --dispatch <native-dispatch-id> \
  --packet /absolute/private/task.txt --route clinepass
spectre-dsh-execution run --task <native-task-id> --dispatch <native-dispatch-id> \
  --packet /absolute/private/task.txt --route opencode-go
spectre-dsh-execution inspect --dispatch <native-dispatch-id>
```

Run only in the assigned registered Orca pane. Inherited environment overrides
cannot redirect the DSH binary/profile/key source. Go pins the reviewed existing
isolated home; this dated home is deployed version evidence, not a silently
upgradable global default. A new staged version needs fresh route acceptance.

Both routes executed real tools, created/read the exact receipt artifact,
reported native worker_done and settled completed/succeeded. Actual OS exits
were 0. Native liveness is still unverifiable/unsupervised_settled. Exact native
FIFO completion messages were reviewed before metadata-only cleanup and ack;
both left the original pane, release Gates and coordinator identity intact.

Immutable exit-source pages then entered the original continuation journal.
They convey review evidence, not worker_done or new approval. Live attempts
do not generate repetitive model turns; pending or failed deliveries preserve
source receipts/cursors. A standard service restart retained both execution
receipts and nine delivered effects without duplicate sends.

At this checkpoint, native retry placement was still open; the formal acceptance
below closes it. Automatic native-mail obligation/ack acceptance, offload job
completion events, efficient polling and staged timer activation remain open.
Headless DSH has no resume option; persistent-session recovery uses
the verified core API profile below, not invented argv. Host reboot remains
outside this run at the operator's request.

## Durable session substrate and actual interruption acceptance

`spectre-worker-session-v2` adds a pinned runner without replacing headless,
existing TUI profiles or providers. Its source is the exact upstream
`@deepseek-ai/dsh-headless@0.1.5-rc.1` plus the public core create/resume helper.
Installed package exports, runner, helper, LICENSE and provider overlay must
match reviewed bytes before execution. Stage each approved home explicitly:

```sh
dsh-superagent-stage-session --home /home/person/.local/share/fullmoon-dsh
dsh-superagent-stage-session --home /home/person/.local/share/fullmoon-dsh-go-verified-20261102
```

The exact Run/Task/workspace/route yields one stable session ID. A new Dispatch
cannot recreate an existing Task session without native retry lineage. Each
Dispatch still has one OS intent and one exclusive session claim. Receipts
preserve workspace, selected provider/model, runtime PID/boot/start ticks and
the session sequence before current input. OS exit remains separate from native
Task outcome. A retry requires the exact `retry_of_dispatch_id`, unchanged
scope/packet/route, actual recorded exit, matching host boot, absent original
wrapper and runtime PIDs, and a verified original session receipt. Live,
unreadable and reused PIDs fail closed.

Pinned AgentLoop constructs an idle driver, runs setup, flushes setup events,
then publishes it; publication does not wake the durable inbox. Resume setup
clears pending old input through the public inbox API before publication and
records discarded input IDs. Original events remain in the durable log. The
current Dispatch's preamble is new input, never authority inherited from old
messages. This does not resolve unknown side effects: interrupted tools keep
the upstream `TOOL_OUTCOME_UNKNOWN` result, not fabricated success or retry.

Actual Spectre acceptance used an independent Run `run_2b2ed77486fb` with shell
coordinator `term_3603d7e3-d29f-4e43-a9d8-d139b92ab57b`, registered in the
existing remote-agent workspace. Task `task_35117b4c6839` / Dispatch
`ctx_d07b18d2dccd` created and reread the bounded artifact, emitted one native
`worker_done`, settled `completed / succeeded`, and recorded OS exit 0 with the
stable session receipt. Completion `msg_06148ca47691` was reviewed against the
artifact and Dispatch; release returned `retained / no_owned_resource /
processAction: none`; FIFO `delivery_4fab520c9237` was acknowledged and no
reclaimable rows remained. The production coordinator was not used or restarted.

A separate visible direct canary wrote and reread a nonce, then entered one
`sleep 180` tool. Its explicit 65-second wrapper limit produced exit 124; the
original runtime PID was absent before resume. The same session
`session-interrupted-v2-20261102` resumed in a new process, recovered the nonce
from history without that nonce in the new prompt, and used only `write` and
`read` on the new artifact before exit 0. Actual log readback contains
`interrupted -> completed` and the unresolved old tool outcome. This verifies
history recovery, not native retry placement, automatic inbox processing, host
reboot restoration or an offload job's outcome.

## Approved formal DSH worker path

The operator explicitly chose to extend the formal execution path rather than
leave native retries unsupported. Orca has `--retry-of` only on `worker-start`;
low-level `dispatch` records no retry link. Before the approved overlay, installed
1.4.212 agent configuration did not register DSH. Never bridge this gap
with database edits, another agent's identity, fake status/title evidence,
unrelated Tasks, or a fresh Run for an existing assignment.

On 2026-11-05 the operator accepted one narrower exception. A running Orca
that only exposes the single-executable alias resolver may bind `dsh-worker`
and `dsh-worker-go` to spare stdin-after-start agents whose programs are not
installed and whose terminals are not open. That avoids a session-dropping
runtime replacement. The binding still refuses an open or installed agent, a
database edit, a fake status title, another Run, and any weakening of
coordinator ownership. `scripts/install-dsh-worker-alias.py` performs it and
does not call worker-start.

The formal slice provides a dedicated native DSH worker launcher and exact
Orca agent registration, retaining the existing TUI's separate restore-only
hook. It must use the same execution/session receipt, accept only a complete
native preamble, verify the current Dispatch immediately before model input,
and expose real readiness/process/exit evidence without a competing scheduler.
Both subscription routes remain explicit; neither receives a funding fallback.

Formal acceptance gates:

1. Version-matched Orca source/installed archive audit; reproducible reviewed
   build with no changes to other agents, mixed-version behavior or ownership.
2. Launcher tests for framed input, native identity changes during startup,
   duplicate preambles, receipt failures, EOF, live child and actual exit.
3. Isolated native `worker-start`, exact failed Task `--retry-of`, same durable
   session history, fresh authority, real tools, completion review/cleanup/ack.
4. Full compile/lint/test gates and actual installed path readback. No green
   typecheck or fake process declaration satisfies native acceptance.
5. Any running Orca replacement needs separate maintenance approval. The operator
   approved the verified switch and required preservation or resumption of all
   sessions interrupted by it. Host reboot and forced coordinator termination
   remain excluded.

Offload completion events and polling efficiency remain separate open slices.
The Spectre user timer is enabled; those slices are not closed by it.

## Formal launcher acceptance and maintenance audit

The approved overlay targets the exact installed Orca 1.4.212 source digests.
It registers `dsh-worker` and `dsh-worker-go`, including process recognition,
launch commands, agent kind, telemetry enums, and exact readiness titles. The
small compiled foreground launcher executes the pinned Python protocol, forwards
stop signals only to that reader, and returns its real wait status. The reader
waits for an existing execution without signalling it. It rejects new input
after shutdown and validates UTF-8 completion at EOF. This is an actual native
process, not an argv rename, another agent's alias, or a synthetic status hook.

Launch through the native path from the Run's coordinator context:

```sh
orca-ide orchestration worker-start --spec '<bounded task>' \
  --worktree current --agent dsh-worker --json
orca-ide orchestration worker-start --spec '<bounded task>' \
  --worktree current --agent dsh-worker-go --json
orca-ide orchestration worker-start --task <same-task> --retry-of <failed-dispatch> \
  --worktree <exact-workspace> --terminal <proven-worker-handle> --json
```

A failed or stopped attempt still needs a recorded OS exit and exact native
retry lineage. A pre-publication failure may create the same stable session
only when the original wrapper and executor are absent and no session claim
exists. A claim without its receipt remains unverifiable; it never permits
recreation. Session profiles also preserve the Go route's local adapter package
and verify its files before execution instead of copying only the runner.

Actual acceptance in Run `run_2b2ed77486fb`:

- ClinePass Task `task_6a8cce40cbbd`: deliberate failed Dispatch
  `ctx_2743d1cb6fd6`, native retry `ctx_2ac778b18d79`, same durable session,
  session sequence 3 -> 49, exact random nonce recovered solely from history.
- Go Task `task_cc398863fe44`: an initial loader failure exited 1 before any
  session claim. After dependency repair, native retry `ctx_a0551c9d3260`
  created the stable session and deliberately reported failure. Native retry
  `ctx_f5057f5206a8` resumed it at sequence 49 and recovered its exact nonce.
- Both history retries ran existence probe, write, read-result, coordinator
  check, and exactly one current-capability completion. Neither read the nonce
  file, regenerated it, or inspected transcripts. Host comparison verified
  both artifacts. Both succeeded natively and exited 0. Exact FIFO completions
  were reviewed, owned terminals released with captured output, and deliveries
  acknowledged. Reclaimable worker count returned 0.

Orca restart preserved all 26 pre-switch terminals and 37 processes with the
same PTY/process incarnation. No session was interrupted, so none required
relaunch. After explicit cleanup of the earlier failed-readiness canary, the
final audit retained the remaining 25 terminals and 35 processes unchanged.
The production Run, 14 Tasks, two approval Gates, and 13 Dispatch records compare
equal to the pre-switch SQLite backup. That backup is evidence only; no database
was modified directly. The original Sol coordinator and its provider remain.

Native prompt receipts still report `turnStart: unsupported`; fleet liveness
can remain `unverifiable / missing_status`. Actual tool events, native accepted
completion, and OS receipts are separate verified facts. Do not promote a ready
title or a live PTY into model-turn or fleet-liveness proof.

The Spectre user timer is enabled. Automatic native-mail obligations,
offload-completion events, polling efficiency, and host reboot restoration are
not accepted by these canaries. Raw evidence stays private in
`.devcodex/evidence/formal-dsh-worker/` and on the Spectre maintenance stage.
