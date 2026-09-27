COMPLETION PROTOCOL (box standard, do not skip): work the goal as one unit, then hand off.
(1) Run the unit's verification command and capture its output.
(2) Commit the work. Never push unless the goal itself says so.
(3) Post a completion report to Slack #lobby under your own identity with: what changed
    (commit hashes), the verification result, and the suggested next goal. Run:
    /usr/local/bin/spectre-slack-notify --agent qoder --channel lobby --text '<report>'
(4) Only AFTER the report is posted, call update_goal(status="complete").
Never mark the goal complete before the report exists. If you are blocked, post the blocker
to Slack and stop without marking complete. Do not wait for a human to resume you: finishing
the unit and reporting IS the handoff.

HEAVY WORK IS OFFLOADED (box standard, no exceptions, 2026-09-26): the Spectre is an
agent runtime, not a compile farm, and spectre-thermal-guard SIGTERMs build-class work
at every temperature (and stops memory/CPU-intensive non-agent processes). Never run a
build, compile, test suite, container build or dataset job on this box. Run it in the
Lightning Studio instead, as part of this goal:
  spectre-offload --repo <dir> [--setup '<one-time cmd>'] [--artifact <path>] -- <command>
(RUNBOOK 7.21; the wrapper syncs the repo, runs the command remotely, copies artifacts
back, deletes the uploaded tree and stops the Studio). Local work stays I/O-shaped:
reading, searching, editing, git, API calls, small scripts. Delegating a heavy step is
not optional and working around the guard (nice, backgrounding, another worktree) is a
protocol violation. Record studio footprint with `spectre-offload --studio-report`.
