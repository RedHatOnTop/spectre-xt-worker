# Spectre runtime: Agentrouter coordinator and bounded workers

Current baseline: 2026-11-01. This document supersedes the historical Qoder
occupancy, Grok goal supervisor, terminal-pin, and transcript-classifier sections
of RUNBOOK.md. Their journals are audit records, not current execution authority.

## Roles and authority

- The coordinator is one visible Codex session on Agentrouter, launched through
  the direct profile `orca-agentrouter-b` (`https://agentrouter.org/v1`, key
  file `agentrouter-b`). The profile file itself sets the Codex-native idle
  timeout, retry cap, and compaction limit. It does not use the loopback proxy.
  The launch clears inherited ChatGPT-account routing and desktop client
  surfaces (apps, browser, computer use, in-app tools, and the extra MCP
  servers). It also bind-mounts `~/.codex/AGENTS.agentrouter-b.md` (source
  `config/codex-agents.agentrouter-b.md`) over `~/.codex/AGENTS.md` for that
  process only, so the global Korean-reply rule and browser CDP note are not
  part of the request. Never silently fall back
  to the OpenAI account or enable paid overages.
  The operator selected **GPT-6.1 Sol** on November 2. Existing Run, Task, Gate,
  and session identities remain authoritative; a model change creates no owner.
- The primary implementation/debugging worker is **DeepSeek Harness (DSH)** on
  the existing ClinePass DeepSeek V4.1 Flash route. Verified-free models are a
  separate auxiliary pool for suitable bounded work. OpenCode's free Flash is
  not a replacement for, or verification of, the DSH route.
- Refresh live availability and price evidence before selecting a free model.
  Unknown cost is not zero. The existing ClinePass/Go subscription routes are
  distinct from free APIs; using Flash does not authorize arbitrary paid fallback
  or additional funding.
- The coordinator owns task decomposition, dispatch, evidence review, and the
  next action. It does not spend a high-tier turn on routine worker progress.
- Orca's installed `orchestration` skill defines Run, Task, Dispatch, ownership,
  settlement, and recovery. Load that skill from the exact CLI on the execution
  host. Do not rebuild a scheduler around a second SQLite occupancy database.
- Existing work survives a provider outage. Continue already-authorized worker
  packets while safe; surface the coordinator outage instead of claiming success.

## Launch and worker selection

Start only inside a registered Orca workspace. Use `--worktree active` from its
CWD for custom launch commands, and confirm the returned workspace in
`orca-ide worktree ps --json`. Never use invisible tmux/nohup workers.

The coordinator launches through the direct Agentrouter profile:

```sh
orca-ide terminal create --worktree active --title 'Fullmoon coordinator' \
  --command '/usr/local/bin/spectre-coordinator resume <session-uuid>' --json
```

Wait for `terminal wait --for tui-idle` to report `satisfied: true` before sending
the assignment. `input_accepted` is not proof of a started model turn; require
`turn_started` and inspect actual output. Save the session/Run/Dispatch receipts.

For supervised work, the coordinator creates and binds a native Run and follows
the installed orchestration skill. Every worker packet names the target,
ownership, prohibitions, observable acceptance, and stop conditions. Independent
editors use separate registered worktrees.

Route choice, resume, and SOS are journal decisions in
`docs/SPECTRE_SUPERAGENT.md`. Native authority stays Orca. Do not launch a
second owner for a live or unverifiable worker. GPT-6.1 Sol through the direct `orca-agentrouter-b` profile stays the only
orchestrator. A new harness or a
temporary free endpoint is registered as data; it does not add a scheduler or
a proxy. SOS is one insight per recorded stuck episode inside five hours, with
the same owner and no new paid route. Unexpected faults append to the journal
`selection_fault` record shown by `spectre-superagent status`. The Spectre
user timer `spectre-superagent.timer` is enabled and active. Fedora is not a
substitute host for that unit.

### DSH implementation route

The November 2 approved native path is `worker-start --agent dsh-worker` for
ClinePass and `--agent dsh-worker-go` for the existing OpenCode Go subscription.
Use the current bound Run, exact registered workspace and bounded Task. Both
routes passed native placement, real tool execution, accepted completion,
actual OS exit and same-Task `--retry-of` history recovery. The formal launcher
and recovery contract are in `docs/SPECTRE_DSH_EXECUTION.md`. Keep its OS receipts
separate from Orca fleet `unverifiable / missing_status`; no model-turn support
is inferred from the readiness title. Do not repeat the historical migration
smokes, create a replacement Run, or remove pending approval Gates.

The Spectre user timer is enabled. Older native-mail, offload-completion, and
polling-efficiency gates are not closed by that enablement. Maintenance did
not reboot the host or terminate the coordinator.

The retained bounded wrapper is `/usr/local/bin/dsh-clinepass`. Its existing
private profile is `~/.local/share/fullmoon-dsh`, with provider `clinepass`,
endpoint `https://api.cline.bot/api/v1`, and model
`cline-pass/deepseek-v4.1-flash-1017` on the November 1 snapshot. The wrapper
loads the existing private key itself and writes an atomic `.exit` sidecar.

Write a bounded packet under `~/.local/state/remote-agent/packets/` and launch
the wrapper in a registered Orca terminal from the packet's target workspace:

```sh
/usr/local/bin/dsh-clinepass --tier paid \
  --file /home/person/.local/state/remote-agent/packets/<packet>.txt
```

`paid` is this existing wrapper's tier name for ClinePass, not permission to buy
credit or enable overages. Do not switch it to `--tier free` and call that the
same route. Do not use an unqualified `dsh-task` as an alias: its separate global
profile selected `omni-gateway/glm-5.3-flash` when inspected on November 1.
Preserve that other profile instead of changing it globally.

Require actual tool output, artifact readback and the wrapper's real exit code.
Do not invent native DSH liveness or `worker_done` support when this host has
not proved it. The DSH model route, Orca resume integration, and native lifecycle
status are separate acceptance gates. A provider/session-only hook is not an
agent-status feed. Never resurrect the old Qoder occupancy database to fill
that gap.

### Verified-free auxiliary pool

OpenCode's existing global default is not necessarily free. Pin each worker's
model explicitly, for example `opencode --pure --model opencode/<verified-id>`.
For a custom model launch, create the visible terminal in its exact workspace,
wait for readiness, and use native `worker-start --terminal <handle>` to bind
the assignment. Do not pass unsupported OpenCode `--model` flags to Orca's
`worker-start`; that command uses OpenCode's own launch configuration.

The November 1 live Zen catalog showed zero prices for `beam-free`,
`deepseek-v4.1-flash-free`, `big-pickle`, `flint-40b-free`,
`mimo-v2.6-flash-free`, `nemotron-3.5-super-vl-free`,
`nemotron-3.5-lightning-free`, and `glm-5.3-flash-free`. This observation is not
a permanent allowlist or proof of tool-use success. Never enable a contributor
or training/privacy concession to obtain a free route.

### Native free tier for harness workers

Native worker executions read one exact `Kind:` line from the packet header and
choose a burn tier. `mechanical` and `implement` go free first when
`~/.local/share/fullmoon-dsh-free/settings.yaml` and the omni gateway client key
are provisioned; `review` and `blocker` always stay on paid ClinePass, and a
missing or unknown kind stays paid. The free profile pins zero-price Cline
models through the local gateway (`reflection/beam-free`); it never enables a
contributor or training route.

Every decision and its reason is appended to
`~/.local/state/remote-agent/free-tier-decisions.jsonl`, and the executor
exports `SPECTRE_DSH_KIND`, `SPECTRE_DSH_TIER` and `SPECTRE_DSH_TIER_REASON`.
An unprovisioned free route is a visible paid fallback, never a silent one, and
a free result is bounded review evidence like any other worker result.

## Observation is not scheduling

The Discord intake operator-approved mode is read/classify/store only
(`INTAKE_IMPLEMENT=0`). It no longer plans or launches implementation. Current
TODO revisions reach the native coordinator as review-only context through
`spectre-superagent`; `accepted_by` is the completion reviewer, never permission
to execute. The original native Run/Task/Dispatch remains execution authority.

`spectre-runtime` reads only native status, workspace/terminal inventory,
paginated Runs, and `orchestration worker-list --include-remote`. A coordinator
counts as working only when a current native Run binds its connected terminal
to a matching native agent with fresh working evidence (at most 120 seconds).
Live Dispatches in that Run also count while the coordinator waits. It preserves native
`live`, `unverifiable`, and `exited` verdicts and literal recovery instructions.
It never sends terminal input, starts an agent, infers death from a timeout,
acknowledges mail, or resets Task state.

Private configuration at `~/.config/remote-agent/orca-runtime.json` declares
which workspaces currently require an active assignment:

```json
{
  "schema_version": 1,
  "required_workspaces": ["/absolute/registered/workspace"]
}
```

This is an operator expectation, not a second task ledger or terminal registry.
Remove an expectation only after evidence-backed completion or an explicit
operator stop. A shell, an empty intake queue, or an active systemd service is
not an active assignment. An idle runtime is reported as `idle`, never released.

```sh
spectre-runtime
systemctl --user enable --now spectre-runtime-watch.timer
journalctl --user -u spectre-runtime-watch.service -n 30 --no-pager
```

A missing expected assignment or incomplete inventory returns exit 1. The
minute timer repeats the check and sends a bounded Slack fleet alert; identical
alerts are spaced 15 minutes apart. Notification failure remains an error and
does not consume the retry receipt. The observer is not an unattended coordinator.
The coordinator must remain in its native supervised loop until settlement,
explicit approval, or a reported external blocker.

## Retire old control paths

Install from the reviewed tree, then inspect the read-only retirement plan:

```sh
sudo PERSON_USER=person PERSON_HOME=/home/person bash scripts/install-orca-runtime.sh
spectre-retire-legacy
spectre-retire-legacy --apply
```

Retirement refuses an unreachable native Orca runtime. It disables old controller
units, moves their original files and the user Qoder pin registry into a private
timestamped archive, and masks retired names against accidental restarts.
It does not kill agents, delete worktrees, clear locks/journals, change credentials,
or clear the Qoder billing sentinel. Cost/thermal guards, backup, production
watch, Discord intake, and the Agentrouter proxy remain independent services.
Shared root-owned legacy registries/binaries require a separately audited archive;
do not delete an entire library still used by a supported harness.

`install-control-plane.sh` now refuses execution. `bootstrap.sh` installs only
the native observer. The previous bootstrap and installer are retained as
non-runnable audit text in `docs/archive/`; source history is not a migration
instruction. Restoring a historical installer is not a supported rollback.

`spectre-coordinator /absolute/prompt.md` must run inside an Orca terminal. It
holds one workspace-scoped process lock, uses the existing Agentrouter launcher,
and passes the initial assignment as a CLI argument rather than typing before
TUI readiness. Startup update prompts are disabled for this managed launch;
software updates remain an explicit maintenance action. The launcher never
silently retries or switches to a different provider.
The startup flag is documented in the official
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

To resume an exact existing session, use the same workspace-scoped lock:

```sh
spectre-coordinator resume <session-uuid>
```

This pins `gpt-6.1-sol` through the existing Agentrouter wrapper and refuses
ambiguous selectors such as `--last`. Run it in the existing registered pane
only after its previous Codex process has exited. Do not close its terminal or
create a new Run merely to change models. Read `/status`, the actual provider
request evidence, and native inventory before declaring recovery.

The November 2 migration exposed a Codex compaction boundary: changing `/model`
and even resuming with `--model` did not change the model used to compact the
previous Astra turn automatically. The proxy's model-specific counters showed
continued Astra requests. Interrupting that compaction and invoking `/compact`
explicitly used Sol and completed. Full local history was retained; no remote
opaque compaction, transcript edit, or provider rewrite was used. Subsequent
Sol shell execution and native `done` were verified in the original pane.

For rollback, inspect the private archive manifest, remove only the matching
`/dev/null` masks, and restore the saved unit files. Review original enablement
and current process ownership before enabling any unit. Never force the old
Minecraft journal to IDLE or delete `.sent` records to obtain progress.

## Fullmoon release boundary

The active release scope is `docs/RELEASE_1_0.md` in the current Minecraft
checkout, frozen on October 30. September release matrices and old Tier-0
handoffs do not expand it. Accept only direct test/gameplay evidence.
Production deployment, real user data, irreversible operations, and public
Discord writes keep their approval gates. A monitor being installed is not
proof that Fullmoon 1.0 shipped.

## Verified migration corrections

The live November 1 test exposed two additional interruption paths. OpenCode was
missing from the thermal guard's agent-tooling classification and was killed
with `intensive_workload:cpu`. It is now protected as an agent harness; build
children remain subject to offload enforcement and no threshold was relaxed.

The Agentrouter proxy rejected image-bearing history with
`413 Payload Too Large: Request exceeds the portable proxy size limit.` Its
bounded request/decompression limit is now 64 MiB instead of 8 MiB. Framing,
duplicate-field, depth, and compression validation remain enabled. A real
10,780,327-byte request subsequently received upstream HTTP 200 on Spectre.
The historical failure and initial external-terminal smoke reports are retained,
not rewritten as native-supervision passes.

The installed Codex Orca launcher preserves an existing session's Agentrouter
provider on resume. The actual session metadata and resolved resume argv were
checked without starting a duplicate coordinator. A full machine reboot and
end-to-end session restoration after reboot have not been performed.
