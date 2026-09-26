# Power loss, session resume, and the offload directive — 2026-09-26

## The outage

The previous boot ended **mid-command at 01:02:18** — the journal stops while
`worker-health.service` was finishing normally, with no shutdown sequence — and
everything agent-shaped died with it: 2 Claude TUIs, 3 Orca worker terminals
(zzbrush, korea-metro-twin, minecraft), the Astra planner, the flash-packets
shell, and the two legacy tmux workers (`qoder`, `pugc`). The operator had
removed the charger the day before (fan noise), so this was a battery death; they
asked for three things: resume everything, measure the battery runtime, and put a
strong offload directive into every agent.

## The battery cannot answer "how long did it last"

- UPower's charge history for the previous 7 days has **zero `discharging`
  rows** (1089 `fully-charged`, 8 `charging`, 1 `unknown`).
- The gauge reads `energy 43.3344 Wh == energy-full-design`, `capacity 100 %`,
  `energy-rate 0.0148 W`, `time to empty 122 days`, `charge-cycles: N/A`, vendor
  `23-17`, model `PW04048` — an aftermarket pack whose fuel gauge never updates
  on discharge.
- No AC transition is logged either (`tlp` and the kernel emit nothing on this
  EC), so the unplug moment is not recoverable from the journal.
- Consequence: the runtime is **unmeasurable from history**, there is no
  low-battery warning, and the battery must be treated as an emergency-only cell.
  Sessions have to be restartable, not gracefully suspended. A real number would
  need a deliberate test (known idle load + `upower -m`) and would still be blind
  if the gauge stays at 100 %. Written up in RUNBOOK §7.20.

## Resume

Rebuilt from the registry (`qoder-workers.json`) plus transcript mtimes (the two
newest Claude transcripts were last written at 01:02, i.e. at the death moment):

| Session | How | Handle |
| --- | --- | --- |
| Claude (remote-agent), `5522c1fa…` | `claude --permission-mode bypassPermissions --resume` in the worktree | agent-managed, `/goal active` |
| Claude (release-readiness), `40068146…` | same, after seeding `hasTrustDialogAccepted` for `/home/person/wt/release-readiness-spectre` (§7.19) | one writer kept |
| zzbrush | Orca terminal, `/home/person/.local/bin/qoder-efficient` | `term_af0901a3-…` |
| korea-metro-twin | same, `/work/korea-metro-twin` | `term_3a8cf98d-…` |
| minecraft | same, `~/Projects/minecraft-server-project` | `term_7a7f3859-…` |
| qoder, pugc | `tmux new-session -d -s <w> -c <cwd>` + qoder-efficient | tmux |

`spectre-pin-sync --apply` recorded the three new worker handles; the registry now
points at live terminals again. The Astra planner was **skipped on the operator's
word** ("Astra has been stopped for a while and Claude took over"); the launcher
also needs `~/.local/bin` on `PATH` (`codex-mode`) and refused with
`efficient_pin_required_before_second_terminal` anyway. The flash-packets shell is
bridge-provisioned, so it returns on the next flash dispatch.

Traps found while doing this: `qoder-efficient` refuses a cold start while the
cost gate cannot read a price factor (`allow-start status=unknown` → exit 75), so
two worker terminals sat at the prompt until the gate re-read it — fixed by
re-sending the command into the *same* terminal, not by opening another. And the
resumed Claude sessions immediately began creating terminals of their own (that
is why the terminal count jumped from 3 to 12); one duplicate writer on session
`40068146` was closed so a single TUI owns each transcript.

## The offload directive, in every agent

Added `config/offload-directive.md` + `scripts/install-offload-directive.sh`
(marker-delimited, idempotent, backup kept) and installed it into
`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`; appended the hard clause to
`config/qoder-goal-clause.md` (every dispatched goal) and the execution constraint
to `config/astra-plan-prompt.md` (the planner may not plan a local build);
strengthened the AGENTS.md rule. The box copies under
`/usr/local/share/remote-agent/` were refreshed and `slack-bridge` restarted.

## Guard rule 3: CPU/memory-intensive work

The operator's "offload CPU/Memory intensive work" needed enforcement, not just
prose: `spectre-thermal-guard` now also stops non-agent work at
**RSS ≥ 1536 MiB** or **≥ 90 % CPU for 3 consecutive samples**
(`INTENSIVE_RSS_MIB`, `INTENSIVE_CPU_PCT`, `INTENSIVE_STREAK`), with the Slack
message `offload candidate stopped … (spectre-offload, RUNBOOK 7.21)` and the
kills ledger. Implemented with tests (43 → 44 after the refinement below).

One hole was found and closed before installing: `AGENT_PROTECTED` listed
`python3`/`python`/`node`/`electron`, which protected exactly the data crunches
the rule exists to catch. Interpreters are no longer blanket-protected;
`protected()` matches the executable identity and the first two argv basenames, so
`python3 /usr/local/bin/spectre-state` and `node …/spectre-slack-bridge` stay
safe while `python3 -c '…'` does not. Verified live: a synthetic 1.7 GiB python
hog was SIGTERM'd (`reason: intensive_workload:rss`), the ledger line written and
the Slack message posted; a dry-run against the running fleet selected **zero**
candidates (agents sit at ~270 MB RSS and <10 % CPU).

## Still open

- The 4-hour Studio cap observation: one-off check scheduled at 17:48:31 KST
  (`~/.local/state/remote-agent/studio-cap-check.log`, `~/studio-lifecycle.log`
  in the Studio).
- Whether the free Studio accrues any credits, and `sandbox`/`job` billing.
- A deliberate battery endurance test, if the operator wants a number.
