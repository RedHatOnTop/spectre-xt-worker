# Autonomous continuity, first safe migration — 2026-09-23

## Decision

The requested loop is not just provider fallback. It must preserve an initial
objective, select and verify successive goals, and recover from unintended
stops without an operator. Migrate one live component at a time; keep goal
dispatch disabled until the authority, pins, provider, and objective contract
are verified. A failed-closed state is not continuous operation, even when it
prevents a duplicate dispatch.

## Observed failure and recovery

`spectre-worker-state.service` was `active` for three days while its Unix
socket returned `[Errno 111] Connection refused`. Its old server unlinked a
live socket path on a second bind. Local regressions now cover active-bind
refusal, stale-socket recovery, and ownership-safe close. The first attempted
module replacement on Spectre failed because the installed package lacked
`dsh_jsonl`; the exact import error was reported, the old module restored,
and API health verified. A socket-only backport against the installed package
was then installed with the service stopped and restarted. On-box health and
Minecraft snapshot reads passed, and a second bind against the live path was
refused without losing API access.

A separate watchdog was installed and enabled. It probes every minute,
restarts after two consecutive failures, caps attempts at three per hour,
persists private state, and posts a fleet notice. A controlled live test
stopped the resolver: the first check observed failure, the second restarted
it, and the API returned healthy with a new PID. This does not type into any
worker terminal.

## Remaining gate

The Minecraft snapshot is `UNCONFIRMED`, with `can_dispatch_goal=false`.
Orca reports the registry-pinned terminal as `Gemini CLI`, but `/proc` shows
the foreground process in that terminal is Qoder Efficient with the matching
`ORCA_TERMINAL_HANDLE`; Orca's title/identity metadata is stale. The current
installed resolver 1.0.1 leaves this ambiguous delivery parked indefinitely.
A staged 1.2.0 resolver on a consistent clone of the live SQLite journal
classifies it as `FAILED/unconfirmed_timeout` and opens `can_dispatch_goal`.
That transition is not safe to deploy alone: the original terminal write may
have reached an agent despite missing structured acceptance evidence. A new
goal after a fixed timeout could duplicate active work. Resolver 1.2.1 keeps
ambiguous delivery `UNCONFIRMED` and raises an idle-SLO alert after the
timeout instead of authorizing another dispatch. The unmodified 1.2.0
poller also consumed 97.9% of one CPU during a 20-second on-box shadow run;
the box has only two cores. Keep the existing low-overhead resolver until
the ambiguity and scheduling rules are migrated together.

The repository does not yet hold a durable initial-objective contract or a verified
top-level completion criterion. Kimi's real account inference, live packet
handoff, and full control-plane migration are also unverified. No claim of
7x24 autonomous progress is justified yet.

## Resolver cutover

The 1.2.1 source now preserves `UNCONFIRMED` indefinitely until authoritative
evidence arrives, with an idle-SLO alert instead of dispatch permission.
The first on-box staged full-package test found an old assertion still
expecting `FAILED`; after updating that test, 97 worker-state tests passed.
The complete staged `bash verify.sh` passed on Spectre: bridge 64, Devcodex
77, Python 427. `shellcheck` was skipped because it is not installed.

The unmodified 1.2.0 poller consumed almost one core. Profiling found
`process._children` repeatedly walking all of `/proc`; reading each process's
per-thread kernel `children` files reduced warmed full polls from 3.6–4.2
seconds to 0.59–0.60 seconds on the cloned journal. Passive evidence is also
batched into one replay per worker. The first startup replay took about
12 seconds. Both code paths have on-box unit coverage.

The cutover stopped the bridge, state consumer timers, watchdog, and old
resolver; swapped the full package and service unit; waited for a real 1.2.1
snapshot; and restarted the consumers. Health returned seven workers. Live
Minecraft and qoder remained `UNCONFIRMED` with dispatch disabled. Bridge
dry-run returned `dispatch_refused` and
`worker is UNCONFIRMED (policy.can_dispatch_goal=false)`. Watcher and
continuity dry-runs completed. The prior package was retained for rollback.
`sudo spectre-doctor` reported `summary: 67 passed, 1 failed`; the failure
was `FAIL  codex third-party provider block present`. No goal/provider/proxy
timer was enabled.
