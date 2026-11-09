# Fedora worker bootstrap — 2026-11-06

## Scope and gates

- The operator approved session-preserving integration repair, then explicitly
  permitted direct coordinator implementation for this bootstrap only. GNOME
  appearance implementation remains delegated to the bounded worker path.
- Preserve the original GNOME Run, Task, failed native request and launch files.
  No new funding, paid fallback, credential changes, journal reset, global cap
  increase, Orca restart or unrelated process cancellation is authorized.
- Gates: exact failed-request reconciliation; full compile/lint/nonzero tests;
  at least 80% changed-module coverage; immutable installed closure; native
  worker execution and artifact/completion readback; session preservation.

## Observations and changes

- Managed launch `gnome-hybrid-20261106-1136-01` failed with
  `error: start_failed: agent_readiness: timeout`. Native request
  `61769037-b8c7-4ffa-8fea-ee1965c886bf` completed with a failed outcome.
- Reconciliation returned
  `harness-workers: native launch reconciliation does not match its original operation`.
  The adapter only accepted ready receipts and compared the requested alias
  directly to its canonical carrier. It also requested a concise start view.
- The initial reader later emitted
  `spectre-dsh-worker: worker input requires a complete native bracketed-paste frame`
  and returned to its shell. Its shell had no children. Only that exact settled
  failed-start terminal was released natively; output was captured. Other
  retained readers and user sessions were left alone.
- Fedora lacks the legacy `/usr/local/bin/spectre-dsh-execution` deployment.
  Its existing OpenCode CLI, Go credential entry and Go model catalog are
  present. A user-scope Go executor now uses that existing account and the
  existing `opencode-go/glm-5.3-flash` selection without changing profiles or keys.
  Missing ClinePass execution is explicit; there is no provider fallback.
- Native reader lifecycle events use the existing OSC status protocol and the
  exact validated pane identity. Idle means no active child, never native Task
  success or a model-turn claim. Recognized terminal query replies are consumed
  only outside paste frames; arbitrary input and embedded control data still fail.
- Go attempts retain actual child identities, exit statuses and CLI session IDs.
  Retries require native lineage, the original scope and actual prior exits.
  Storage failures do not abandon a live child. Dispatch capabilities are
  redacted from forwarded output and failure diagnostics.
- Reader rollout is a digest-checked, backed-up pointer to a complete immutable
  snapshot. Existing Python readers keep their loaded code. No runtime restart
  or live carrier rebinding is involved.
- The DevCodex connector returned stale source and
  `ENOENT: no such file or directory, access '/home/person/Projects/distribution-project/remote-agent/tests/test_harness_worker_pool.py'`
  while that file existed locally. No connector write occurred. Local DevCodex
  session `20261106115631-cdaa781f` owns the actual Fedora source work.

## Evidence so far

- Red tests reproduced missing bootstrap modules, the failed receipt contract,
  the absent reader-update digest argument and rejected renderer reply frames.
- Focused coverage run: 77 tests passed; 94% aggregate branch-aware coverage.
  New Go execution and status modules exceeded 80% individually.
- First full verification: 123 Node, 132 vendored DevCodex and 1341 Python tests
  passed. ShellCheck was initially unavailable, so that first lint gate was
  explicitly skipped. A checksum-verified ShellCheck 0.11.0 binary subsequently
  ran the whole shell gate with exit 0 and no findings.
- Final full verification, installed behavior and GNOME application are still
  open. No desktop settings have changed yet.

## Installed execution and preserved reader reuse

- Final bootstrap gate: `bash verify.sh` with the checksum-verified ShellCheck
  directory on PATH completed with `verify_exit_code=0`: ShellCheck clean,
  123 Slack bridge tests, 132 DevCodex tests and 1342 Python tests passed.
  The command ran inside `dsh-offload/throttled.sh`. The earlier SC2164 failure
  was fixed with an explicit directory-change guard; the cancellation fixture
  now establishes its signal behavior before publishing its PID marker.
- Digest-checked installation promoted snapshot `3db70a8` without a restart.
  All 25 original native terminal handles, PTYs and incarnations matched before
  and after. Reader PIDs 70189/start248207 and 566034/start2043465 remained alive.
  Their stable library link stayed on `fa81c65`; the old reader was backed up.
- Canary Task `task_d173acdf5b49`, Dispatch `ctx_4d814526d716` ran through
  `harness-workers start --apply` in the original Run. Actual write, `od -c`,
  `sha256sum`, accepted native completion and the Go CLI/executor exits were read.
  The artifact is exactly 52 bytes, SHA256
  `c5456d16ab352799dd1c847a73cfe2fcd23f91c0e9749a0968ee63ee0893b3f1`.
  The model's 48-byte prose claim was incorrect; artifact bytes are authoritative.
- The canary reader remains live and was retained natively. Starting another
  writer in that workspace correctly returned `workspace_wait`, `native_start:
  false`. It was not killed, released or moved aside to evade the guard.
- Added bounded native reader reuse: exact terminal/incarnation/previous
  Dispatch, successful native settlement, unchanged carrier and original OS
  exits are required, then revalidated immediately before native input. Capacity
  remains occupied; foreign/busy resources and new same-workspace writers block.
  Reviewed readiness failures retain their original Task completion timestamps
  only with the exact released failed-start lineage, never a prior result or
  circuit-broken Dispatch. No records are cleared.
- Latest focused gate: 95 tests passed, 92% aggregate branch-aware coverage;
  every changed module exceeded 80%. Full reuse gate and actual same-reader
  GNOME Dispatch acceptance remain open. GNOME is still unchanged.

## Native retry state correction

- The full reuse gate completed: ShellCheck clean, Node 123/132 tests and Python
  1351 tests passed, `verify_exit_code=0`. Snapshot `f6b01d7` was installed with
  all 25 original terminal identities and both unrelated reader processes intact.
- The first reuse call returned
  `Task task_22dfde5567ab cannot retry from Dispatch ctx_d8dada961378. Orchestration mutation request ID: 46c725b2-13b5-4226-9b9f-eb3eb717b9dc.`
  I had changed the Task to ready; native retry requires failed/blocked instead.
  No new Dispatch appeared. Native request readback was `absent`, which alone
  is not proof of no effects. The original intent and response are preserved.
- Restored the original Task to failed without clearing its old Dispatch or
  failure count. Managed retry validation now inspects the latest exact failed
  Dispatch rather than treating a reopened Task timestamp as new work.
- Added original-request recovery for this exact recorded validation refusal:
  native readback, full original-command equality, original caller identity,
  proven exited launcher, retained-reader custody and current capacity are
  checked before replaying only the same durable request ID. Recovery intent and
  output are append-only; pending work cannot be duplicated.
- Latest focused run: 102 tests passed, 91% aggregate branch-aware coverage,
  every changed module above 80%. Full recovery gate and native replay are open.

## Recovery accepted

- `verify-recovery.log`: ShellCheck clean, Node 123/132 tests and Python 1357
  tests passed, `verify: all gates passed`, `verify_exit_code=0`.
- Installed source closure `5b1e725` without restarting anything. All 25
  original native terminals and both unrelated reader processes still matched.
  The original stable library remains `fa81c65`.
- Managed `recover` replayed request
  `46c725b2-13b5-4226-9b9f-eb3eb717b9dc` and accepted Dispatch
  `ctx_73477c88d0ce` on the original GNOME Task/Run, reusing the exact canary
  terminal and incarnation. No new terminal, Run or replacement Task was made.
  The original failed response and launch intent were not overwritten.
- Native startup reported `input_accepted`, `turnStart: unsupported`. The Go
  reader's actual execution receipts and initial output were inspected separately;
  the desktop application and visual acceptance remain the worker's open Task.

## Failure escalation and native authority

- Orca escalated `ctx_73477c88d0ce`: the Go model exited 0 after an automatically
  rejected `external_directory` call without reporting completion; the helper
  exited 2 with `native failure report requires one exact current Dispatch capability`.
  The Dispatch remains open. Both real child/executor exits and the retained
  reader were inspected; no process was killed or treated as released.
- Current native contract is version 1 with an explicit null capability hash.
  Both the received and native preambles contain zero capabilities and match
  exactly. The native CLI requires exact pane authority in this case, not an
  invented token. Failure reporting now preserves that contract; required,
  forged, revoked and unknown capabilities still fail closed after full native
  binding confirmation. A settled failure is never reported a second time.
- Three new regression tests pass. Focused gate: 82 tests, 89% branch-aware
  aggregate coverage, every touched module above 80%; full gate is running.
- The operator was asked to approve fencing only the ended GNOME attempt and
  project-scoped external-directory permissions. No permission, profile, key
  or session change was made while awaiting that gate. OpenCode 1.18.33 and its
  versioned documentation were inspected; `--auto` is not used.
- Registered a separate empty `native-failure-probe` workspace and native Task
  `task_e5e7bea50b86` in the same Run. It will verify the installed missing
  ClinePass path reports an actual native failure and exit 2 without a model
  request or fallback. It is not a ClinePass implementation-route success claim.
- Peer message `5946c5a67ae81d4bc2fd063a12235dcf` was acknowledged. Their retained
  readers and all unrelated sessions remain unchanged; their history is not
  authorization to implement GNOME directly.

## Retained custody accounting

- Full pane-authority gate passed: ShellCheck clean, Node 123/132 and Python
  1360 tests, `verify_exit_code=0`. The new immutable closure was promoted;
  no existing reader restarted or changed its loaded code.
- Probe start returned `capacity_wait`, occupied 2, limit 2, native_start false.
  I identified both reported occupants before taking any other start action.
  They are the current GNOME Dispatch and its completed canary ancestor on
  the same retained native terminal. The ancestor has resource null, while the
  current native resource explicitly records the ancestor as origin and the
  GNOME Dispatch as owner. Neither was released or killed.
- Accounting now resolves only this exact native origin/owner/terminal/host/
  workspace/endpoint-incarnation transfer. It retains one live occupied slot,
  rather than treating resource absence as release. Foreign, ambiguous, active
  or malformed history remains separate unknown capacity. No journal or native
  metadata is rewritten and the worker limit stays 2.
- Red tests reproduced the duplicate slot and malformed projection failure;
  focused tests now pass. Read-only live inventory confirms one physical retained
  slot with the exact native custody chain. The full gate and actual failure
  probe are still open; operator permission and reader-replacement gates remain.

## Authorized recovery — 2026-11-07

- The operator approved project-scoped desktop paths, fencing only the ended
  attempt, and replacing only its idle reader. The original 25 terminals and
  Go conversation remain protected; Orca and GNOME must not restart.
- Actual Go/executor absence was reconfirmed before native `worker-stop`.
  Native stop closed only `term_5253997d-f1c4-4a16-9430-b98b58b8901a`;
  PID 1197533 was then absent. The preserved stream records shutdown_requested.
  Native `worker-release` completed with processAction none and captured archive.
  The old Dispatch is failed/stopped, count 1, and the same Task is blocked.
- Full custody verification failed in the existing offload artifact fixture:
  `subprocess.TimeoutExpired: Command '['bash', '/home/person/Projects/distribution-project/remote-agent/scripts/spectre-offload.sh', '--repo', '/tmp/tmpkk3y_rmx', '--artifact', 'results', '--', 'true']' timed out after 10 seconds`.
  The identical test under the same cgroup caps then passed in 7.491 seconds.
  No timeout, assertion or resource limit was relaxed. Full gate is being rerun.
- Origin accounting now follows an exact completed native release as well as
  retained custody. An absent old row never frees capacity on its own. Released
  origin/workspace/host/incarnation and native exited/resource_release evidence
  are required. Live or malformed release evidence remains unknown capacity.
- A stopped execution retry requires unchanged Task bytes, route and workspace,
  actual old model/executor exits and the exact preserved Go session. Read-only
  production preflight proves zero remaining physical slots and the original
  session is resumable. No Task, journal or failure count was reset.
- Created only workspace-local `opencode.json`, SHA256
  `fb2968c0259e7b0f29e0cb7b74ce6f382dc5b9a0336cc653ff026988ce64864b`.
  Native `opencode debug config` exit 0 confirmed the resolved permission rules.
  Other external paths still ask; credential/session directories and system
  asset edits are denied. No global profile or auto-approval was changed.
- Workspace AGENTS.md records the authorized runtime boundary, current native
  report contract and the already-verified throttle command without requiring
  a source read. Desktop implementation is still delegated, not implemented here.
