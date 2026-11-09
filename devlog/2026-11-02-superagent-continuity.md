# Superagent continuity redesign — 2026-11-02

- Inspected upstream commit `6337fc823ce20251c8de8085948165c81aaa701e` and
  current source, native runtime, coordinator pane, OS process, and private routes.
- Tailscale SSH required a user check; surfaced the URL and waited for approval.
  Authentication then passed. No authentication workaround was attempted.
- Initial observer invocation failed with
  `spectre-runtime: error: unrecognized arguments: status --json`; used its
  actual argument-free JSON command afterward. `run-show` takes `--id`, not
  `--run`. Read actual help rather than assuming shared flags.
- Connector reads returned `ENOENT` for files confirmed present on the local
  execution host. Used the local DevCodex CLI for bounded source inspection;
  remote connector workspace registration is not filesystem equivalence.
- Found zero active Dispatches, a connected idle coordinator, retained pending
  gates, and `required_workspace_unassigned`. Did not infer process exit from
  stale activity or from `terminal wait` returning `timeout`.
- Operator selected Orca authority plus DeepSeek structure, existing intake
  first, and inclusion of DSH OpenCode Go. Confirmed Go provider/model separately
  from ClinePass and free API routes, without printing private keys.
- Prototype needs DSH `^0.2.1-alpha.2`; installed runtime is `0.1.5-rc.1`.
  Adopt durable concepts first; do not replace the working runtime blindly.
- Baseline `bash verify.sh`: 113 bridge tests, 132 DevCodex tests, 662 Python
  tests passed; shellcheck was skipped because it is not installed. This is a
  baseline, not verification of the redesign or complete lint evidence.
- Wrote `docs/SPECTRE_SUPERAGENT.md` with the preserved full scope and failing
  acceptance gates. First implementation slice is the crash-safe operational
  queue, not a replacement worker-occupancy database.
- Wrote journal tests red, then implemented transactional ingest, strict event
  identity, exclusive claims, ordered lanes, bounded retry, attention, receipt
  retention, and uncertain-claim reconciliation. A real subprocess exits 23
  immediately after claim; reopen retains the event and prevents re-execution.
- Review exposed two defects before acceptance: an updated effect lost its
  original attempt payload on restart, and an old attention result lost its
  reason. Added failing regressions and retained original attempt payload and
  explicit attention reason.
- Implemented a local native wake adapter against the inspected envelope
  `result.send.prompt`. It requires exact Run/pane/workspace/incarnation,
  confirms idle state, retains partial delivery, and uses the same native
  request ID for observation instead of resending. It is not deployed.
- Sent one read-only checkpoint to the original Fullmoon coordinator. Initial
  receipt and idempotent replay both remain `input_accepted`; screen readback
  shows the exact submitted request and `Working`. Retained request
  `d6e09262-c1a1-4ab1-91a2-5278a0a72fc8` as reconciling, with no duplicate owner.
- Installed ShellCheck 0.11.0 in a disposable local venv because neither
  shellcheck nor uv was available. Full lint now exposes pre-existing SC2088,
  SC2329, SC2016 in spectre-offload and SC2094 in thermal-calibrate. Full gate
  fails; no suppression or threshold reduction was introduced.
- Earlier progress commentary accidentally said 705 Python tests; the actual
  corresponding full output was `Ran 704 tests` (662 baseline plus 42 new).
  Command output is authoritative. Subsequent tests must report fresh counts.
- Latest focused gate:
  `python3 -m coverage run --data-file=.devcodex/evidence/superagent.coverage --branch --source=scripts/superagent_queue -m unittest discover -s tests -p 'test_superagent*.py' -v`
  returned `Ran 47 tests`, `OK`, exit 0. Coverage report returned 97% combined
  statement/branch coverage, exit 0. Native behavior remains mocked in these
  adapter tests; actual same-pane wake has the unresolved receipt gap above.
- Latest full gate:
  `PATH=/home/person/.cache/spectre-superagent/lint/bin:$PATH bash verify.sh`
  returned bridge `113 pass; 0 fail`, DevCodex `132 pass; 0 fail`, Python
  `Ran 709 tests`, but `verify: FAILED`, exit 1 due to literal ShellCheck
  diagnostics preserved in `.devcodex/evidence/superagent-verify-current.log`.
  Python compile and `git diff --check` separately exited 0. No stage is closed.
- Graph AST updated with `graphify update .`. Semantic documentation refresh
  was not run; graphify warned about missing `tree_sitter_sql` and changed
  community labels. These warnings remain recorded, not a complete graph claim.
- No commit, push, Spectre installer run, service replacement, route switch,
  or host reboot occurred. Next work: original native request reconciliation,
  complete operational driver/intake integration, DSH route staging, and
  behavior-backed correction of the existing lint failures.
