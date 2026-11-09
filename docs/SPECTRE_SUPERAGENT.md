# Spectre continuous worker redesign

Status: implementation in progress. This is not a deployment receipt.
Baseline inspected on 2026-11-02. Runtime authority remains Orca-native.

## Objective and confirmed boundaries

Unfinished authorized work must survive coordinator turn completion, provider
outages, lost responses, host restarts, and input delivery failures. A connected
terminal is not proof of continued work. An outage is not proof of agent exit.

The operator confirmed:

- Preserve Orca ownership, visible terminals, and native Run/Task/Dispatch records.
- Prioritize existing authorized work and current intake paths. Do not introduce
  GitHub Issue/PR admission or new external publication permissions.
- Include DSH through existing OpenCode Go, alongside Agentrouter coordination,
  DSH ClinePass implementation, and verified-free auxiliary models.
- Use GPT-6.1 Sol for the existing Agentrouter coordinator instead of Astra.
- Send new Discord intake as review-only context, including records with no
  completion reviewer. Disable the legacy intake's own planner/implementation;
  reading, classification, and durable storage remain active.

Existing cost, production, publication, thermal, offload, backup, and player-data
approvals remain mandatory. OpenCode Go is a subscription route, not a free API.
No new funding or unknown-price fallback is authorized by selecting that route.

## Structural quality requirement

The operator's request for an exceptionally beautiful, clean, and efficient
result refers to architecture, mechanisms, and code itself, not UI. No dashboard
or UI redesign is part of this work.

- Every fact has one authority: Orca for execution, the event journal for
  delivery, source ledgers for intake, and operator mandates for permissions.
- State transitions are explicit and evidence-driven. No timeout invents exit,
  no receipt invents success, and no repeated observation invents permission.
- One small mechanism handles normal execution and recovery. Avoid a parallel
  recovery controller or a second occupancy ledger.
- Transport and persistence boundaries are visible in code. Pure decisions
  consume immutable snapshots; side effects commit their intention first.
- Work scales with changed events, not repeated history scans or repeated model
  invocations. Ordered delivery remains bounded without starving other Runs.
- Treat unexplained retry loops, incompatible state definitions, duplicate
  ownership, and hidden mutable configuration as design defects, not polish.

## Reference and inspected baseline

Reference: https://github.com/deepseek-ai/dsh-superagent-prototype
Pinned inspection commit: `6337fc823ce20251c8de8085948165c81aaa701e` (MIT).
The README explicitly calls this research code, not production-ready software.

Relevant upstream behavior:

| Upstream source | Adopted principle | Required adaptation |
| --- | --- | --- |
| `docs/001-domain.md` | Immutable Trigger; durable authorization separate from execution | Existing source identity and approvals authorize work; Orca owns execution attempts |
| `src/app/effects.ts` | Transactional effects, key deduplication, generation fencing, bounded backoff | A crash during a native mutation becomes reconciliation-required, not blind replay |
| `src/core/assignments.ts`, `src/executor/dsh.ts` | Durable assignment and recovery context | Bind to exact native Run/Task/Dispatch and execution-host facts |
| `src/executor/background.ts` | Background jobs retain capacity after a model turn ends | Preserve approved offload job handles; do not kill jobs at the prototype's 30-minute limit |
| `src/app/outbox.ts` | Durable, ordered replies and receipts | Preserve current communication/approval rules and do not publish unsolicited results |
| `WorkerSlots` in `src/executor/dsh.ts` | Bounded capacity and priority for human follow-ups | One coordinator; start with one implementation worker and one bounded auxiliary |
| `src/app/retry-health.ts` | Repeated transport failures remain visible | Route cooldown and attention are not completion or revocation of work |

The installed Spectre DSH reports `0.1.5-rc.1`; the prototype declares
`dsh: ^0.2.1-alpha.2`. Its plugin API cannot be presumed compatible. Stage a
separate pinned runtime and prove it before changing any existing session.

Read-only inspection found:

- Orca runtime reachable; coordinator process still present in its original pane.
- Native Run `run_5a60a8ea1424` bound to
  `term_b234127e-b0de-4b37-a35e-1a6d20f942b2`.
- Zero active Dispatches; `spectre-runtime` returned exit 1 and
  `required_workspace_unassigned` for `release-1-0-current`.
- The coordinator's last turn ended with pending release/capacity approvals.
  Its screen and OS process confirm an idle coordinator, not agent death.
- Existing ClinePass profile selects `cline-pass/deepseek-v4.1-flash-1017`.
- Existing DSH gateway profile selects `omni-gateway/glm-5.3-flash`; gateway
  default provider is `opencode-go`, endpoint
  `https://opencode.ai/inference/go/v1`.
- Native DSH lifecycle and reboot restoration are still unverified.

Private raw baseline evidence is under `.devcodex/evidence/superagent-baseline/`.
It must not be committed or published.

## Architecture

1. Existing intake adapters persist an immutable event before advancing their
   source cursor or acknowledging delivery. A repeated source/event ID with
   identical content is a no-op; conflicting content is an explicit error.
   The reviewed Discord patch commits original input and its channel cursor
   before classification, then verdict/TODO/settlement together. Failed
   classification retains the input and a bounded due retry, not a discard.
2. The accepted mandate records scope, source identity, approval references,
   allowed routes, and stop conditions. It points to a native Run and Tasks;
   it never copies occupancy or becomes a competing scheduler database.
3. A small local effect journal commits work before invoking Orca. It contains
   pending deliveries, wake requests, native request receipts, route cooldowns,
   and outgoing notifications, not authoritative worker lifecycle state.
4. The continuity driver reconciles current native facts before every effect.
   Exact native identity and an approved mandate are mandatory. Terminal input
   is an attention mechanism; native ownership remains unchanged.
5. Executors use visible registered Orca terminals. DSH ClinePass, DSH Go, and
   free auxiliaries keep isolated explicit profiles and route evidence. No
   global profile rewrite, billing fallback, or unregistered checkout is allowed.
6. The coordinator consumes FIFO questions and settlements, reviews artifacts,
   decides worker reuse/release, then acknowledges the native delivery. A
   model turn ending does not settle the overall mandate.

### Crash and delivery rules

- Commit event and effect together or neither. Execute only after commit.
- Persist an execution claim before a side effect. A response lost after claim
  is `reconciling`, never automatically changed to `pending` by a timer.
- Native mutation receipts supply operation replay identities. Reconcile with
  `request-show` or the exact returned recovery command before replaying.
- A terminal wake without a returned request ID cannot be retried blindly;
  inspect that exact terminal/process incarnation and surface uncertainty.
- A marked prompt in a native `working` snapshot can prove delivery. A matching
  native `done` snapshot can also prove it when it has a fresh completion time,
  a nonempty assistant response, and explicit non-interruption. This handles a
  response finishing before submission observation. Neither proves task success
  or changes the literal native receipt stages.
- Updating an effect while it runs increments its generation. An old response
  cannot delete or overwrite the newer delivery.
- Ordered deliveries do not overtake a cooling-down or unreconciled earlier
  delivery. Independent work may proceed in a different ordering lane.
- Transient retry uses bounded exponential backoff. Deferred capacity consumes
  no failure budget. Authentication, revoked authorization, unknown cost, and
  malformed inputs require explicit attention rather than broad fallback.
- Do not time out, kill, release, abandon, or duplicate a live/unverifiable
  worker to make the queue move. Operational queue claims are not agent leases.

### Route admission, profiles, and SOS

Native authority stays with Orca Run, Task, and Dispatch. The continuity
journal resumes the same mandate after a lost response, provider outage, or
process restart. Do not launch a second owner for a live or unverifiable
worker. GPT-6.1 Sol on the direct Agentrouter B profile
(`orca-agentrouter-b`, no local proxy) is the only orchestrator. Register
a harness or a temporary free endpoint as route data with adapter
`registered`; do not add a scheduler. SOS allows one insight for a recorded
stuck episode inside five hours, keeps the same owner, and opens no paid
route. A second SOS in that window, or an SOS without a stuck episode,
performs no dispatch. Append unexpected faults to the journal
`selection_fault` record printed by `spectre-superagent status`. Do not enable
`spectre-superagent.timer`, reboot, or start a live worker from this command.

`scripts/superagent_queue/selection.py` is the same text via
`instructions()`. `spectre-superagent tick --apply` is the service entry. It
reads `free-ledger.json` beside the journal, records one route choice, and
carries an unblocked select through the existing `worker-start` path once.
A live worker, an unverifiable worker, or an open delivery claim keeps the
same owner and is not launched again. When that blocker is gone, the next tick
stores the new route. No authorized work in the ledger means no launch. The
DSH cap stays two. Neither the tick nor `admit` opens a second proxy.

Registration is data. A new harness needs a route object (`route_id`,
`harness`, `adapter: registered`, `family`, `model`, `role: worker`, `price`,
`expires_at`, `usefulness`, `remaining`, `approved_paid`, `malformed`,
`exhausted`) and a bound mandate. Families are `cline`, `zen`, `qoder`,
`zcode-glm`, `stealth`, `temporary`, `clinepass`, and `opencode-go`.
OpenCode Go is a DSH subscription route, not a free pool. Free routes are
eligible only at a known zero price, at or above the recorded usefulness bar,
with remaining quota, and unexpired. Stealth also requires a `stealth/` model
id, numeric-zero price components, and a current or empty expiry. Priced,
expired, malformed, ordinary `:free`, and `openrouter/free` rows are not
chosen. Unknown price and unapproved paid routes are not fallbacks.

Agentrouter/Anyrouter budgets are the proxy health snapshot passed to
`budgets_from_proxy`. DSH ClinePass/OpenCode Go is the other subscription
family. New work is not assigned to a family marked exhausted. Suitable
authorized work prefers an eligible free route before either subscription.
Profiles are reduced from assistant `evidence:` lines in a preserved
transcript and are the text `admit` reads. They do not change harness policy.
The DSH cap remains two workers. On 2026-11-04 the user timer
`spectre-superagent.timer` was installed on Spectre, where `orca-serve` is
active. `systemctl --user is-enabled` prints `enabled` and `is-active` prints
`active`. Two installed `tick --apply` runs exited 0, printed no secrets, and
did not launch a worker. Fedora does not run the coordinator, so the timer was
not enabled there. A ledger with no authorized work does not create a task.

`spectre-superagent status` prints the retained work id, the one GPT-6.1 Sol
Agentrouter/Anyrouter orchestrator, the admitted routes, and the
`selection_fault` location. It does not print secrets and it does not launch
work.

### Continuation and blockers

The coordinator records structured `working`, `waiting_job`, `waiting_provider`,
`waiting_approval`, `ready_for_review`, or `finished` dispositions with exact
native/job/gate references. Free-text classification is prohibited. A blocker
holds only the affected work; independent authorized tasks remain eligible.

Wait on positively live handles. Recheck a blocked dependency when its state
changes or a bounded retry becomes due, not by repeatedly asking the model to
repeat an outage report. An idle coordinator with pending eligible work gets
one durable wake, followed by submission/readback verification. Unknown
submission remains visible and retains the event.

Only evidence-backed completion or an operator stop closes the mandate and
removes the required-workspace expectation. A human approval wait is explicit,
visible, and retained; it is not a successful release.

## Phases and quality gates

### P1: durable input and effect substrate

Goal: no accepted input disappears on crash or concurrent update.
Tasks: transactional event/effect journal, deduplication, generation-fenced
claims, ordered lanes, transient/deferred backoff, reconciliation state, private
storage, schema validation, and restart integration tests.
Gates: tests written red before implementation; real SQLite close/reopen and
process-restart flows; at least 80% changed-module coverage; repository
`bash verify.sh`; no external mutation or runtime replacement.

### P2: native continuity driver and existing intake integration

Goal: existing authorized work resumes without new operator prompts or duplicate
owners. Tasks: native identity resolution, durable source cursor integration,
structured continuation dispositions, safe wake/request reconciliation, blocker
isolation, queue/attention CLI, and systemd lifecycle.
Gates: fake-runtime fault injection for lost response and incarnation changes;
real same-pane wake with receipt/readback; one event causes at most one turn;
service restart preserves pending events; live/unverifiable work never retries.

### P3: DSH execution and route integration

Goal: DSH ClinePass and DSH Go execute bounded packets with durable recovery and
accurate native lifecycle evidence. Tasks: isolated version-compatible staging,
explicit profile/model/cost evidence, current Orca compatibility audit, a design
review before introducing DSH status hooks, and visible registered execution.
Gates: real tool-read/write receipts for both routes; exact exit and session
evidence; native Dispatch settlement proven or explicitly external-accounted;
provider outage recovery without paid fallback; unchanged existing profiles.

### P4: deploy and failure acceptance

Goal: the 24/7 system works without relying on a perpetually active model turn.
Tasks: reviewed additive deployment, source/install hash verification, startup
reconciliation, operator-visible status, and acceptance reports.
Gates: full tests/build/lint on an approved non-Spectre execution host; real
service restart, coordinator turn-end, provider failure, input-during-job,
duplicate event, lost response, and host restoration flows; retained work and
approvals verified after every fault; exact rollback procedure demonstrated.
Host reboot requires a separate maintenance confirmation and must not interrupt
unrelated work.

No phase closes merely because its plan, static compilation, service enablement,
or a test with mocked behavior looks plausible. Full goal completion requires
the deployed end-to-end P4 evidence, not just P1.

### Verified artifact return and resource retention

November 3 adds `offload-artifacts.py`, a standalone remote/local verifier.
Selection, exact job/command identity, every regular file digest/size and the
directory tree enter a bounded manifest. Descriptor-relative reads reject
symlinks, nonregular files and concurrent replacements. Private per-job
destinations preserve write-once intent and verification receipts. Canonical
JSON comparison distinguishes booleans from integer file sizes.

The revised wrapper verifies returned bytes before non-native cleanup. Any
manifest/copy/verification failure retains the remote tree, Studio and capacity;
it never prunes shared caches. A failed command keeps its actual exit separate
from artifact failure. Native jobs retain resources even after verified return,
pending an exact coordinator review and resource decision. No unattended
cleanup or release implementation is implied by retention.

The source is privately staged on Spectre, not installed as the production
wrapper. Actual local rsync and file readback passed in the existing isolated
coordinator pane, including empty directories and corrupt-copy rejection with
unchanged source/verification bytes. This proves local I/O, not Lightning
execution. Automatic artifact-review publication, late artifact retrieval,
receipt-backed cleanup and successful native remote acceptance remain open.

### Durable artifact publication and late retrieval

The November 3 follow-on binds returned artifacts to the original native packet,
command digest, immutable wrapper exit and exact remote completion. A write-once
`artifact-exit.json` snapshots the verified manifest and proof. It enters the
same transactional source cursor as a distinct `offload-artifact` review-only
event; publishing it never modifies an earlier execution/reconciliation event.
After snapshot publication, later local artifact removal does not erase the
historical evidence. Completion/resource decisions remain native authority.

The exact current coordinator may run:

```sh
spectre-superagent offload-artifacts --run <run-id> --job <job-id>
spectre-superagent offload-artifacts --run <run-id> --job <job-id> --fetch
```

Fetch requires recorded actual wrapper and remote command exits, the unchanged
native packet and the approved existing Studio matching the original transport.
It sends the pinned read-only manifest helper over SSH, copies declared paths
with rsync, and rereads actual returned bytes. It never starts the Studio,
replays the command, deletes remote data, mutates Tasks or releases capacity.
Each transfer attempt is private and append-only; failures preserve literal
output. A per-job lock prevents concurrent transfers, and a verified attempt
surviving a crash is reused without another network request. Missing/invalid
original evidence remains an error, not permission to recreate execution.

Local SSH-shaped acceptance executes the pinned helper and real rsync. Actual
Spectre acceptance in the existing isolated coordinator pane confirms current
native owner, outcome events 2 -> artifact event 1 -> replay 0 and unchanged
wrapper history. The job/result inputs in that acceptance are explicitly staged
local fixtures; this is not a positive native Lightning launch. Automatic
coordinator result review, receipt-backed resource cleanup and unattended
fault acceptance still block timer activation.

### Receipt-backed remote tree cleanup

The current coordinator can request cleanup only with the exact reviewed native
completion message and artifact snapshot digest:

```sh
printf '%s\n' '{"message_id":"<completion-id>","artifact_sha256":"<snapshot-digest>"}' \
  | spectre-superagent offload-cleanup --run <run-id> --job <job-id>
```

It rereads current ownership, original packet and returned artifact bytes, then
checks accepted Task/Dispatch outcome, exact whole Delivery check/ack receipts
and native release/retain/reuse decision. A command exit 0 is not Task success;
native failure may reject a command that exited 0. Native success cannot conceal
a failed remote command. Missing review or uncertain receipts authorize nothing.

The adapter commits cleanup intent before SSH. The pinned remote helper checks
exact command/result identity, original boot, absent original PID and no live
members of the original process group. It moves only the owned original tree
into a private cleanup tombstone and removes it using descriptor-relative,
symlink-safe filesystem operations. Its write-once claim and result live outside
the removed tree. A recreated original path is never removed by old replay.

Lost response requires the same command with `--reconcile`. Remote inspection
alone can report pending/absent; neither frees resources. Only an exact existing
claim with verified same-boot tree identity and an unlocked operation advertises
safe replay of that cleanup ID. A pending live/unknown operation is preserved.
This does not replay the original remote build or mutate native ownership.

This slice removes the verified remote tree only. Shared Studio stopping and
capacity reservation release are still deliberately unimplemented, so cleanup
reports `capacity_released: false`. Real local I/O and actual current-owner
negative acceptance passed; authorized Lightning cleanup and overall unattended
activation remain unverified. The production wrapper and timer are unchanged.

### Exact capacity release and shared Studio ownership

The follow-on source binds native jobs to the existing shared lease directory,
not a new worker scheduler. The wrapper requires private native lease storage,
records the exact reservation inode/digest, keeps a hard-link provenance anchor,
and records wrapper PID/boot/start ticks, original transport and remote boot.
An empty legacy `.started` remains unverified ownership. A native start instead
has a durable operation marker and a distinct confirmed provider-start receipt.

After exact cleanup completion, only that job's original reservation is archived
and removed. Missing/recreated/conflicting reservation or live/reused wrapper
PID fails closed. Other numeric or unknown leases are preserved, even when their
PID is absent; an absent wrapper does not prove its remote work stopped.

The Studio is stopped only when no other lease remains, start/confirmation
markers match the original Studio/teamspace, the current remote boot matches
the accepted command receipt, and current private configuration still names
that CPU Studio. Stop intent is shared and durable before the provider call;
new wrapper starts are blocked while it remains unresolved. Lost stop responses
are observed, not blindly retried. Verified stopped state archives only matching
markers and survives a crash before final resource receipt publication.

Cleanup and final resource release are separate immutable `offload-cleanup` and
`offload-resource` source events. Their publication preserves earlier execution,
late-result and artifact events. Actual local command/cleanup/reservation I/O and
Spectre staged exact reservation release passed; Studio start/stop uses provider
fixtures in those tests, not positive Lightning acceptance. Production lease
storage is currently mode775 and must clear a reviewed private preflight before
native activation. No production permission change or wrapper install occurred.

## Current verification and open findings

Updated November 2 after actual Spectre execution. P2/P3/P4 remain open.

### Obsolete continuation and finished-Run review delivery

November 3 counterexamples exposed two FIFO stalls: a deferred automatic resume
could block later native questions, and finished could block a first review
context. The local/staged repair resolves only exact generated, proven-unsent
generation-1 resume intents with unchanged event identity and current native
owner evidence. It retains an explicit no-input supersession receipt. A lost
response, receipt-bearing claim or replacement generation is never discarded.

Native refresh reads workers for eligible automatic continuation and restores
that observation if a held disposition becomes eligible while reading. New
digest-bound review contexts may wake finished without reopening execution.
Prompt delivery still leaves native question/completion review obligations open.
FIFO lanes and dispositions remain Run-scoped; same-Run independent-task
eligibility and sustained unattended operation still require further acceptance.

Actual Spectre native reads plus staged SQLite intents verified supersession,
later review eligibility and reopen retention without terminal input or native
mutation. A partial installed-module update then exposed a missing staged-only
offload dependency on actual import. The prior installed files were immediately
restored and import/status verified. These fixes therefore remain local/staged,
not deployed. Source import preflight now rejects incomplete installer input
before existing files change. Timer stays disabled; automatic mailbox trust and
native offload terminal topology remain pending operator decisions.

### Receipt-backed mail obligations and pending followup cancellation

Journal schema 3 records currently observed native mail separately from its
transport effects. Prompt delivery never settles a question or completion.
The exact current owner publishes original check, reply/resource-decision and
whole-Delivery acknowledgment request IDs. Installed native receipts must match
the complete batch and accepted Task/Dispatch outcome before review is stored.
The driver does not answer, acknowledge or release on the owner's behalf.

November 3 fault-flow acceptance adds exact original message comparison before
storing native review. ID reuse with changed body/type/payload cannot close the
old obligation; current native read metadata is not part of its immutable
identity. Malformed native rows are explicit validation failures. Structured
provider cooldown holds automatic mailbox followups in discovery, pending
selection and the refreshed pre-send guard; the original review remains open.
Actual installed-CLI process fixtures cover lost output, observer SIGKILL,
concurrent new input, post-send observation failure, source/storage rollback,
early turn-end and whole-batch receipt replay. These are fixture protocol flows,
not real unattended model acceptance. Actual original Spectre receipts for
status/question/completion were also revalidated by staged code from the same
isolated pane without issuing native mutations. The repairs remain staged.

An unsent followup is unnecessary when all its original messages have
receipt-backed reviews. The driver resolves it without model input, preserving
`delivery_sent: false` and review proof hashes. The native adapter checks again
after refreshing ownership. Later mail does not block cancellation of an
already processed original batch. A newer generation or ambiguous/receipted
transmission remains for reconciliation.

November 3 installed acceptance used real check/ack receipts in the existing
isolated coordinator pane: two pending effects resolved, no new native mutation
or model input, `FOLLOWUP_VERIFICATION_EXIT=0`. Full local gate: Node 123,
DevCodex 132, Python 938 passing tests; changed-module branch coverage 94%.
This proves cancellation and receipt validation, not unattended model handling.
Its automatic canary awaits operator Folder access trust approval. The
continuity timer was still disabled at that acceptance. The 2026-11-04
Spectre deploy later enabled the user timer.

### Held-Run polling and existing offload safeguards

Continuity ticks retain current native owner, mailbox, Gate and source reads.
Route selection always re-reads the Orca worker inventory before it chooses,
including when the disposition is finished, waiting on a job, waiting on
approval, or waiting on a provider. A live or unverifiable worker in that
inventory is retained and is not launched again. An eligible disposition
published during source collection restores worker observation before resume
discovery. Unknown or failed observations never substitute an empty worker
inventory. Mail processing remains eligible during approval waits.

November 3 source collection against the actual production Run, using an
isolated journal and no model input, took 4.905 seconds and discovered no
unsolicited resumes. Full local verification reported Node 123, DevCodex 132
and Python 955 passing tests. This does not prove full polling efficiency over
time or automatic offload completion. The user timer was still disabled
then; the 2026-11-04 Spectre deploy enabled it.

The repository offload wrapper now retains the installed shared-Studio lease
and capacity safeguards, rejects unknown provider status, separates default
remote directories, and keeps start/stop under the lease lock. It is staged,
not installed as the production wrapper. Real Lightning status currently fails
with HTTP 521; the staged wrapper preserves that error and exits 6 without
changing the Studio. Native-bound job terminal selection, durable offload
completion and successful real remote acceptance remain open.

### Durable offload completion substrate

`offload_receipt.py` preserves one write-once local wrapper attempt and the
original native references and command digest. Wrapper exit and remote command
exit are separate facts. A missing, public, malformed or mismatched remote
receipt keeps the remote observation unverifiable even when the wrapper exits 0.
`offload_source.py` admits verified receipt facts through bounded transactional
pages as review-only context. It never settles Tasks or releases worker resources.

The standalone `offload-command.py` verifies argv identity, claims before remote
execution, preserves actual output and wait status, and does not kill the command
on observer signals. Job-mode wrapper collects this receipt before cleanup.
Without verified remote completion, it retains the tree, Studio and unknown
capacity reservation. That reservation precedes execution so wrapper SIGKILL
does not make capacity appear free. No retry or unknown-capacity release is
automatically authorized by this mechanism.

This substrate is locally implemented and staged on Spectre, not an activated
production job launch route. Actual staged acceptance recorded Lightning status
failure with wrapper exit 6 and remote unverifiable, and read back durable
review-only source facts. No remote command ran in that acceptance. Successful
remote execution, native launch authority, later receipt reconciliation and
unattended completion wakes remain acceptance gates. The operator's pending
terminal choice and Folder access trust question are not implicitly resolved.

### Late remote result reconciliation

Late receipts append `remote-exit.json` with the original wrapper-exit digest
and exact remote job/command result. Original wrapper observation is immutable.
The journal receives a separate `offload-reconciliation` event, leaving earlier
delivery receipts intact. Duplicate observations are idempotent; conflicting
results, missing receipt or unknown wrapper exit remain unresolved.

The current coordinator can use `offload-reconcile --run <run> --job <job>`.
It requires that exact current native pane and original Run. Optional `--fetch`
also requires the Run's explicitly configured existing Studio and the original
recorded transport. Fetch reads only a bounded receipt from the recorded job
directory. No Studio start, remote command retry, cleanup, ownership change or
capacity release occurs. Remote errors retain full diagnostic output.

Actual staged Spectre I/O acceptance verified late helper exit 7 after wrapper
exit 0, unchanged original wrapper bytes, two distinct evidence events and
coordinator-owned CLI replay with no command re-execution. This was local I/O,
not successful Lightning execution. Existing Lightning availability still
fails with HTTP 521. Production wrapper/config remain unchanged; continuous
activation and the operator's unresolved topology/trust choices remain open.

### Fresh native authority for offload execution

The formal `spectre-offload-execution` adapter validates a bounded schema-1
argv/artifact packet against the current exact native Task, Dispatch, capability
preamble, registered pane and process incarnation. Its Run must retain the
approved coordinator and existing Studio. Packet rereads must retain the
native-bound digest. DSH route validation is unchanged.

Each binding has one stable offload job, original private packet/preamble and
configuration snapshots. Inherited provider/funding/guard overrides are removed.
Reviewed wrapper/helper/authority modules are hash-pinned. The actual child
identity is persisted before its startup signal is released; missing receipt
cannot race into an authority check or remote command.

The same native guard runs before Studio access and after capacity/upload waits,
immediately before command execution. It uses the exact original CLI and checks
the live wrapper, fresh original authority and unchanged current configuration.
Unknown startup, revoked approval or mixed runtime refuses execution.

This path is implemented and staged, not production-activated. Actual Spectre
negative acceptance rejected a completed Dispatch from the coordinator pane
with no job or remote mutation. Full local gate reports 1051 Python, 123 Node
and 132 DevCodex passing tests. Authorized remote success, selected visible job
topology, resource cleanup and automatic continuous operation remain open.

### Durable source and disposition acceptance

Journal schema 2 adds operational source cursors and append-only versioned
continuation dispositions. Schema 1 upgrades transactionally without clearing
events, effects, attempts, receipts, or native records. Source pages freeze a
maximum ID per sweep, read at most 101 rows, and atomically ingest up to 100
rows with the next cursor. The next sweep catches lower-ID inserts and
backdated revisions; new growth cannot postpone wrap indefinitely. This is
current-revision polling, not a claim to recover overwritten upstream revisions.

The source `accepted_by` is a completion reviewer, not a work authorization.
The operator confirmed review-only admission of queued/planned/deferred source
records and disabled `INTAKE_IMPLEMENT`. The actual scheduled intake cycle
reported `implement: false`, `planned: 0`, `delegated: 0`, exit 0 while continuing
to read its configured channels. No credentials or other env lines changed.

The original native coordinator published `waiting_approval` revision 1 through
the installed CLI from its exact pane. A later process read the same revision.
Publication checks current native Run/pane/workspace/incarnation plus an
expected revision; it mutates neither Gate nor Task. Existing JSON is only a
baseline for Runs with no published record. Publish through:

```sh
spectre-superagent disposition --run <native-run-id> --expect-revision <n> < disposition.json
spectre-superagent status
```

On Spectre the real source ledger produced two review events, then zero new
events after reopen. Both reached the original coordinator once, one per tick;
readback explicitly retained review-only scope and pending approvals. The final
journal showed schema 2, two events, two delivered effects, two attempts, and
two distinct native request IDs. Further reopen reported `deliveries: []`.
Run/Task/Gate result objects and the original source bytes compared unchanged.

`patches/discord-intake-continuity/` retains a reviewed source patch, digest
manifest, and installer with the original intake cycle lock and private backup.
It also restores historical failed classifications or relevant messages missing
their TODO from retained history. The installed source's hashes match; its next
scheduled cycle created the additive `received` table and exited 0 with own
implementation still disabled. Existing source rows/cursors were not reset.

Latest gates for this slice: `bash verify.sh` with pinned ShellCheck reports
115 Node / 132 DevCodex passes, `Ran 817 tests`, `verify: all gates passed`,
exit 0. The reproduced patched intake's complete suite reports `Ran 36 tests`,
`OK`, exit 0; changed executable-line coverage is 94.4%. Its whole historical
module coverage is 59%, not 94.4%. Superagent module coverage is 95%. All suites
ran on Fedora; no test/build suite ran on Spectre. The full historical suite
still emits `ResourceWarning: unclosed database` from retired qoder-goal-watch;
the focused superagent suite is warning-clean. No old controller was re-enabled.

The event journal, native adapter, CLI, and systemd units are installed
additively. That November 3 install left the timer disabled. The 2026-11-04
Spectre deploy enabled `spectre-superagent.timer`. Existing ClinePass, global
DSH, coordinator ownership, approval gates, and production services were not
replaced.
Twenty-five staged files and twenty-one installed runtime files were hash
verified, followed by matching hashes for the two fast-completion corrections.

Queue decisions use one fleet snapshot per tick and one delivery per lane.
An unproven send adds one post-send snapshot solely for delivery evidence;
normal discovery does not rescan per effect.
Reconciliation is bounded in SQL before materialization; native delivery reads
one current claim instead of scanning retained effect/attempt history. A
reconciliation backlog reserves capacity for independent pending lanes. Current
approval/finished dispositions also hold already-pending automatic wake effects.
Read-only inbox and review-only intake reads do not consume or approve source work.

The original coordinator retained session
`01a196ee-87bc-76c0-a62f-b6fafbc544c9`, Run `run_5a60a8ea1424`, and terminal
`term_b234127e-b0de-4b37-a35e-1a6d20f942b2`. Model/provider readback showed
`GPT-6.1-Sol xhigh` / `orca_agentrouter`. Automatic previous-turn compaction
still used Astra; explicit `/compact` used Sol and completed. A subsequent real
shell `orca-ide orchestration run-show --id run_5a60a8ea1424 --json` produced
the original coordinator handle and a native completed response. Run, Task, and
Gate result objects compared identical before and after.

A private driver acceptance event reached the same pane exactly once. The
native mutation `e9d61d07-543d-49a0-a23e-a0a554dea059` returned only
`input_accepted`; exact-request observation replayed the receipt, not the prompt.
The response completed before the observer could see `working`. A failing
regression led to native `done` correlation using the unique prompt marker,
exact identity, completion time, assistant response, and non-interruption.
The journal then reported one retained event, one delivered effect, and no
reconciling effects; reopening produced no deliveries. Native stages remained
`input_accepted`, and this is delivery evidence, not release/task completion.
The attempt journal contains a no-send deferral followed by one delivered
attempt, with one native request ID. Two claims are not two model submissions.

The reproducible isolated Go adapter pins source digest
`1f787eb5cd3d0308e7a2563cdb2b8cc2c1fc153fe06b55d39439fb2446059483` and
uses actual DSH `options.sessionId` for `x-opencode-session`. Its embedded
imports are pinned to the inspected DSH runtime, not npm package guesses. A new
isolated home `~/.local/share/fullmoon-dsh-go-verified-20261102` executed actual
`read`, `bash`, `write`, `read` calls and ended with wrapper/sidecar exit 0.
Readback was the README first line, numeric sum 5, and route
`opencode-go/glm-5.3-flash`. Its persisted session independently contains all
four tool results and `turn/end: completed`. Subsequent native-bound canaries
verified Dispatch settlement for both routes through the execution adapter
(documented below). Native DSH liveness and reboot restoration remain unverified. A fresh November 2 ClinePass canary used
the unchanged existing profile and original wrapper. Its persisted session
records only `clinepass/cline-pass/deepseek-v4.1-flash-1017`, six real tool calls
and results, and `turn/end: completed`. The receipt matches the actual README
first line and numeric sum 5; wrapper and sidecar both exit 0. Neither route
canary proves native Dispatch settlement.

Latest Fedora gates:

```sh
PATH=/home/person/.cache/spectre-superagent/lint/bin:$PATH bash verify.sh
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_superagent*.py'
python3 -m coverage run --branch --data-file=.devcodex/evidence/standard-service/full-final.coverage \
  --source=scripts/superagent_queue -m unittest discover -s tests
python3 -m coverage report --data-file=.devcodex/evidence/standard-service/full-final.coverage \
  --fail-under=80
```

Output: ShellCheck clean; Python compile succeeds; Node `115 pass; 0 fail` and
DevCodex `132 pass; 0 fail`; Python `Ran 823 tests`, `OK`;
`verify: all gates passed`, exit 0. Focused suite: `Ran 134 tests`, `OK`.
Full-suite branch-inclusive coverage: 95% across the complete superagent module
directory; native delivery 96%, driver 95%, routes 81%, Go adapter 100%.
The full suite was
not run on Spectre because its offload policy prohibits it. Focused continuity
tests also pass without the earlier unclosed-database ResourceWarnings.

Next gates: provider/job failure and recovery acceptance;
controlled timer activation and host restoration when separately authorized.
Do not enable the timer while those execution/recovery gates remain
open. No host reboot, release deployment, public post, commit, or push occurred.
The read-only runtime observer still returns exit 1 and
`required_workspace_unassigned` after the bounded acceptance turns finish.
It correctly refuses to call an idle coordinator an active release assignment;
the required-workspace expectation was not removed to make that check green.

### Standard service and nonblocking delivery acceptance

The standard private settings and journal now exist. A SQLite backup promoted
the verified journal without clearing or rewriting its five tables; integrity
check returned `ok` and every retained row compared equal. The original
acceptance journal remains preserved. The default `spectre-superagent status`
and standard `spectre-superagent.service` use this operational journal.

Actual service start and restart returned `Result=success`, `ExecMainStatus=0`.
They retained delivered effects and `waiting_approval` without sending again.
A subsequent pending maintenance input survived restart, reached the original
pane, and retained its native request identity. A scheduled new Discord source
record also reached that pane as `review_only`; its response refused the public
channel operation and preserved the pending Gates.

The driver process lock now covers effect-executing ticks only. SQLite
transactions serialize input and disposition writes; enqueue, status, and
exact-owner disposition publication remain available while a tick waits on
transport. The driver refreshes published dispositions after source collection
and before each pending delivery. Red regressions exposed both the lock-induced
exit 75 and a stale disposition allowing a wake; the corresponding tests pass.
Actual original-pane publication reached revision 2 while the driver lock was
held. Native Gate and Task mutations remained false.

Unproven sends receive a bounded post-send native observation. Exact marked
working/done evidence can settle delivery in that tick, without inventing
`turn_started` or sending again. Post-send observation failure retains the
original receipt and reconciliation state. A real seven-second response
settled this way with literal stages still `input_accepted`.
Receipt identity is validated before persistence: a mismatched replay response
cannot overwrite the original native request and redirect reconciliation.

One earlier source delivery required explicit review of a preserved native
terminal screen because a later direct maintenance turn replaced its current
prompt. The saved screen contained the exact marker, source record, completed
response, and original pane. Current owner and original native request were
rechecked before recording a manual delivery-only receipt. This was not an
automatic replay or a native Task settlement. Final readback reports five
events, five delivered effects, and no pending/running/reconciling/attention
effects, with `waiting_approval` revision 2 retained.

The standard timer is still disabled. The operator selected verification
without reboot; host restoration is unverified. No coordinator restart, new
worker ownership, public post, production restart, commit, or push was made.

### Native-bound DSH execution acceptance

The operator selected an execution adapter instead of extending Orca DSH status
hooks. `docs/SPECTRE_DSH_EXECUTION.md` defines the current boundary. The additive
`spectre-dsh-execution` command binds one OS attempt to the exact current native
Run/Task/Dispatch/pane/workspace/incarnation and checks the packet against the
native Task block. It retains the original preamble rather than reconstructing
lifecycle commands. The native Run coordinator supplies preamble identity.

Atomic private intent/started/exit receipts preserve real process boot ID, PID,
start ticks and wait status. Each Dispatch may execute only once; observer
restart, absent/reused PID or unknown native liveness never triggers a second
launch. Actual exit is execution evidence, not Task success. Receipts contain
no provider credential or Dispatch capability. Native low-level assignment is
truthfully external/unsupervised; no status hook or Orca bundle was modified.

ClinePass Task `task_7ef0b565c62b`, Dispatch `ctx_91902d9cae8d` completed through
real worker_done with succeeded outcome. The adapter recorded actual exit 0,
artifact readback matches README/sum, and the original coordinator independently
verified native settlement. Cleanup returned retained/no_owned_resource with
processAction none. Exact FIFO Delivery `delivery_7a97f3147945` was acknowledged
only after review; subsequent inbox count was 0 and the original pane remained.

Go Task `task_a2f1db6bfab3`, Dispatch `ctx_abb939c60272` also completed through
real worker_done with succeeded outcome and adapter exit 0. Persisted DSH
session records eight real tool calls/results on opencode-go/glm-5.3-flash and
completed turn. Model-added guessed artifact timestamps are not authoritative;
use exact session and host exit timestamps. Native liveness remains literal
unverifiable/unsupervised_settled, separate from the execution-host OS evidence.
The original coordinator independently reviewed it, applied metadata-only
cleanup and acknowledged exact Delivery `delivery_bb9321cb4b94`. Inbox count
returned 0; the original shell and pending release Gates remained unchanged.

The driver ingests immutable execution exits through bounded fixed-boundary
pages and the existing transactional cursor/event/effect journal. Completion
and failures are review-only events, not fake worker_done signals. A queue write
failure preserves the original exit and cursor for later retry. Live executions
cause no repetitive model wakes. Unsettled external native dispatched work
holds automatic resume even without a supervised resource row.
Native-mail wakes explicitly retain existing coordinator settlement and FIFO
acknowledgment obligations; they grant no new execution/publication permission.
Review-only source intake retains its separate permission boundary. Replaying
an existing event after changing renderer text preserves its original prompt
and request identity, while changed source content or native binding is refused.

Current evidence: full `bash verify.sh` with pinned ShellCheck returned Node
115 / DevCodex 132 / Python `Ran 866 tests`, exit 0. Focused continuity suite
returned `Ran 177 tests`, `OK`, with ResourceWarning treated as an error.
Full-suite branch-inclusive superagent module coverage is 94%.
Installed source hashes match and service restart retains native-bound exit
receipts and delivered events. Provider interruption recovery, offload job-end
integration, efficient native polling and controlled timer activation remain
open. No reboot or production restart occurred.
