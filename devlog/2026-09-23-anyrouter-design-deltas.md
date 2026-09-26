# anyrouter findings force design deltas in the continuity loop — 2026-09-23

Revises the design recorded in `2026-09-23-autonomous-continuity.md` and
`2026-09-23-omni-loop.md`, based on the anyrouter evidence in
`2026-09-23-anyrouter-404-entitlement.md`. Nothing here is deployed yet; the
loop gate is still open, which is why these are cheap to fix now.

## Why the design must change

The loop's stated requirement is that an unintended stop costs wall-clock time
and must not happen. Three of the facts measured today break that requirement in
ways the current design cannot see, and one more is a correctness trap that
already cost operator hours.

## D1 — a configuration failure must not be able to park the chain

`providers.choose()` computes `configured` across the probe results and, when any
relay reports `configured: false`, returns
`{ok: false, reason: 'probe_configuration'}` **before** consulting `kimi_free` or
Plus (scripts/control_plane/providers.py:115-121). Measured on the box, both
relays are `configured: false` — anyrouter because its `base_url` is
`http://127.0.0.1:9995/v1` and agentrouter because neither entry has a
`probe_model` — so the chain dead-ends on `probe_configuration` and never
reaches the free or paid fallbacks. That is a failed-closed stop, the exact
outcome the design forbids, and it is indistinguishable from "all relays are
down".

Change: a missing `probe_model` or an unprobeable relay is recorded per relay and
**alerts**, but cannot withhold dispatch. `configured: false` downgrades to a
soft signal; the chain continues to the next candidate.

## D2 — allow loopback http for the local shim

`probe_request()` requires `https` for any base URL
(scripts/control_plane/providers.py:36-38). anyrouter is pinned to the loopback
shim, so anyrouter is structurally unprobeable. Change: permit `http` only when
the host resolves to loopback and the URL carries no credentials; keep the https
requirement for remote hosts.

## D3 — classify relay failures by body shape, per (provider, wire_api, model)

The probe currently maps every non-2xx to "down", special-casing only 402
(providers.py:82-88). Measured shapes, all from the same host and key:

| shape | meaning |
| --- | --- |
| flat `404 当前 API 不支持所选模型` | slug not offered for this token on this wire API |
| `400 1m 上下文…` | offered, but `anthropic-beta: context-1m-2025-08-07` missing |
| `429`/`503 Service Unavailable`, `500 get_channel_failed` | offered, no channel serving it right now |
| `404 Invalid URL (POST …)` | wrong path or base URL — a config bug |

The last three are not interchangeable. A bogus model name receives the third
shape byte-identically, so it carries no model information and must never be
read as a rate limit or a transient overload. `Invalid URL` must raise a
configuration alert and never be retried. Change: parse the body into
`not_offered` / `needs_beta` / `no_serving_channel` / `bad_route` with distinct
retry policies.

## D4 — catalog reconciliation is the authoritative existence check

Within one day the relay removed `claude-fable-5.1` and added
`claude-opus-5-5`. The removal surfaces as the **fake 429**, not as a 404, while
`GET /v1/models` already showed fable absent. Any design that hardcodes a slug
(`probe_model`, a pinned `model`, the standby seat's model) therefore breaks
silently and, if the failure is treated as transient, retries a removed model
forever.

Change: one periodic `GET /v1/models` per provider, diffed against the configured
slugs. A configured slug missing from the catalog is marked retired — no retry,
an alert, and operator action — instead of being retried as congestion. The
catalog is an existence check only: it is not token-scoped and must not be read
as entitlement.

## D5 — a probe is a lower bound, never an entitlement proof

The same Claude id that answers 400/503 to a small synthetic request returns
**200** to the genuine client request (105 KB, 46 tools, `stream: true`,
captured and replayed with only the model name changed). So a probe failure is
inconclusive. Change: a probe success stays conclusive; a probe failure must not
mark an account dead, must not retire a slug on its own (see D4), and must not be
the sole basis for a decision that spends the Plus budget. Where the decision is
expensive, require the genuine-client path or an operator-visible alert.

## D6 — the wire API and the base URL are per client, not per provider

Measured: Codex appends `/responses`, Claude Code appends `/v1/messages`; this
relay serves the gpt family only on `/v1/responses`, the Claude family only on
`/v1/messages` (plus the 1M beta), and effectively nothing on
`/v1/chat/completions`. A base URL that already ends in `/v1` and is handed to
Claude Code yields `/v1/v1/messages` → `404 Invalid URL`, which Claude Code
renders as "this model may not exist", i.e. a config bug that masquerades as an
entitlement failure.

`omni-proxy` already has the surfaces for this (`ResponsesNative`,
`MessagesNative` in internal/provider/provider.go:169-179), so the delta is in
configuration and in the tester, not in new plumbing:

- record the base URL per client: Codex wants `…/v1`, Claude Code wants the host
  without `/v1`;
- the anyrouter profile's capabilities must say `/chat/completions` is not
  available and route by model family instead of translating through chat;
- `tester.UpstreamEndpoints` (internal/tester/tester.go:667) lists
  `/v1/responses` for the OpenAI kind but has no `/v1/messages` entry, so an
  anyrouter-shaped profile can never test the surface its Claude models live on.
  Add the Anthropic surface where the upstream exposes it, and mark each entry
  as family-specific rather than "upstream dependent".

Also give the failing doctor check a correct target. It currently only asserts
that some provider block exists — `scripts/doctor.sh:385` greps for
`^\[model_providers\.` — and the box fails it today. A block existing is not the
same as the block being usable. Add: the Claude-Code base URL must not end in
`/v1`, and each relay must declare its `wire_api`.

## D7 — the shim is load-bearing and unmanaged

anyrouter's `base_url` points at `127.0.0.1:9995`, served by
`anyrouter-sse-shim.py`, which is a bare `python3` process with no unit, up for
six days, and whose source lives only in `remote-agent-deploy/` on the box. If it
dies, anyrouter silently becomes unprobeable, which then triggers D1's dead end.
Change: either give the shim a systemd user unit with its source in the repo, or
drop it and point at the relay directly.

## D8 — the routing policy is a seat ladder, and each seat has its own harness

Operator-locked order, replacing `RELAYS = ('agentrouter', 'anyrouter')`:

| rank | model | harness | upstream | wire API |
| --- | --- | --- | --- | --- |
| 1 | `claude-opus-5-5` | Claude Code | anyrouter | `/v1/messages` + `anthropic-beta: context-1m-2025-08-07` |
| 2 | `gpt-6-astra` | Codex | agentrouter | `/v1/responses` |
| 3 | `cline-free/kimi-k3` | Kimi (cline-free proxy) | `http://127.0.0.1:8790/v1` | OpenAI chat |
| 4 | `gpt-6-astra` | Codex, ChatGPT account | Plus subscription | `/v1/responses` |

The unit of ranking is therefore `(harness, provider, model, wire_api)`, not a
provider id. Three things block this today:

- `providers.rank()` means "best healthy astra relay" and returns a provider id
  (scripts/control_plane/providers.py:91-97).
- `probe_request()` has `responses` and `chat` branches only — there is no
  `messages` branch, so seat 1 cannot be probed at all
  (scripts/control_plane/providers.py:42-49). D6 stops being optional.
- The dispatch path hardcodes both the harness and the model:
  `['codex-mode', 'chatgpt'] if ident == 'openai' else
  ['codex-mode', 'api', ident, 'gpt-6-astra']`
  (scripts/control_plane/astra.py:47). Under the new ladder this would ask
  anyrouter for astra on the Responses wire — the path that is currently broken —
  instead of opus-5-5 on the Messages wire.

`config/qoder-workers.json` already carries a per-target `harness` field
(`targets.flash.harness = "dsh-clinepass"`), so the configuration shape exists;
the `planner` block is what needs the ladder.

Also note `Routing.FallbackOn429` in omni-proxy defaults to true
(internal/config/config.go:90-120). That default is right for anyrouter — its 429
is the generic "no serving channel" — but wrong for a vendor 429 that carries a
reset countdown, which must reach the client verbatim per omni-proxy's own rule.
The two are distinguishable by body shape, so the flag should not be the only
control.

## D9 — continuity across harnesses: one session record, per-harness resume

Requirement: one top-level session that continues when the seat moves between
harnesses. `seat.py` currently documents the opposite contract — "Handoff is a
brief-based warm start, never a cross-harness resume"
(scripts/control_plane/seat.py:4-5) — with `MAX_HANDOFFS_PER_DAY = 2`.

A hard limit has to be stated first, because promising more would be a lie:
**native transcript resume across harnesses is impossible.** Codex stores
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` keyed by a Codex session id;
Claude Code stores `~/.claude/projects/<munged-cwd>/<uuid>.jsonl` keyed by cwd and
its own uuid; the Kimi path has no session store at all
(scripts/control_plane/kimi.py has no resume handling). The three namespaces are
unrelated formats.

What is achievable, and what the design should specify:

1. **A control-plane-owned session record** is the single source of truth and
   carries the stable, operator-visible top-level session id: objective,
   completion criterion, decision log, open threads, current goal, next action.
   Harness session ids are attributes of it, never the identity.
2. **Per-harness resume pointers.** While a seat runs under harness H, record H's
   native session id. Returning to H later is a **true native resume**
   (`codex resume <id>`, `claude --resume <id>`); the top-level worktree is fixed
   (minecraft-server-project) and Claude Code keys transcripts by cwd, so
   `-home-person-Projects-minecraft-server-project` is the right resume scope and
   already exists.
3. **Primed continuation at a harness boundary.** Crossing to a different harness
   opens a new native session in that harness, primed from the record through
   that harness's own surface (Codex instructions/`AGENTS.md`, Claude Code
   `CLAUDE.md`, the Kimi prompt). Continuity here is semantic, not transcript
   level.
4. **Seats 2 and 4 share the Codex harness**, so that switch keeps one native
   session and needs no priming at all. The priming machinery is only needed on
   the 1↔2 and 2↔3 edges.
5. **Kimi has no resume**, so a Kimi tenure is primed-only in both directions and
   must be the shortest-lived seat; the record has to be rewritten on entry.
6. `MAX_HANDOFFS_PER_DAY = 2` was sized for a two-provider world and now guards a
   four-seat ladder. The cap must become per-edge (the expensive edges are 1↔2 and
   2↔3) so ordinary 2→4 failover is not throttled by the flip-flop guard.

## P1 landed — the seat ladder is the declared policy

`scripts/control_plane/seat.py` now declares the policy once, as an ordered
ladder of `(harness, provider, model, wire_api)` seats, and exposes
`ladder()`, `seat_for_rank()`, `seats_for_owner()`, `owner_rank()` and
`validate_ladder()`. `OWNER_CLAUDE = 'claude'` is added; `OWNER_ASTRA` and
`OWNER_KIMI` are unchanged so no consumer moves yet.

Deliberately additive: `owner`, `model` and `provider_chain` are still written
as before, and `providers.rank()`/`astra.py` dispatch are untouched. The runtime
therefore behaves exactly as it did; the flip happens once the harness dispatch
exists (P2), so no phase leaves the box unable to start a seat.

The shipped ladder is validated by test rather than at import time: an import
failure would take the control plane down for a typo, which is the unintended
stop this work exists to prevent.

Gate, run on Spectre against a staged tree (`tar` of this repo with `.git`,
`graphify-out/`, `zcode-remote-app/` and `references/` excluded, extracted to
`/tmp/rv-stage`, removed afterwards):

```
$ env -u PYTHONHOME -u PYTHONPATH SLACK_AGENTS_FILE=/tmp/rv-stage/config/slack-agents.json \
    SPECTRE_MOTD_DONE=1 bash verify.sh
ok    devcodex tests      # 77 pass, 0 fail
ok    Ran 432 tests
verify: all gates passed
STAGED_EXIT=0
```

Python went 427 -> 432 with the five new ladder tests. Two environment traps are
worth keeping in mind for the next staged run:

- the local DSH runtime exports a `PYTHONHOME`/`PYTHONPATH` pointing into its
  AppImage, which makes `python3` die with
  `ModuleNotFoundError: No module named 'encodings'` inside `verify.sh`; run it
  under `env -u PYTHONHOME -u PYTHONPATH`.
- without `SLACK_AGENTS_FILE` the staged `slack-notify` self-test picks up the
  box's installed registry (9 agents) instead of the staged one (10) and fails
  on a count mismatch. `SPECTRE_MOTD_DONE=1` suppresses the login MOTD leaking
  into the vendored Devcodex shell tests.

## Effect on the recorded gates

- The claim "the loop is not yet closed" stays true, and these deltas explain two
  of the reasons: the chain can park on `probe_configuration` (D1), and a retired
  slug can absorb retries forever (D4).
- The `spectre-doctor` failure `codex third-party provider block present` should
  not be "fixed" by merely writing a block. The block must carry the base URL and
  wire API that D6 requires, or it will reproduce the double-`/v1` failure.
- No deployment: the goal/provider/proxy timers remain disabled.

## D1, D2, D3 and D5 landed — 2026-09-24

`providers.choose()` no longer has a `probe_configuration` exit. A relay that
cannot be probed returns `failure: 'configuration'` with a reason code, and every
failure in `CONFIG_FAILURES` becomes a `<relay>:<reason>` alert while the chain
moves on to `kimi_free` and then Plus. The reasons produced before any request
is sent:

| reason | cause |
| --- | --- |
| `relay_missing` | no registry row for the relay |
| `relay_unknown` | the row id is not a known relay |
| `key_unusable` | key file missing, a symlink, or not mode 600 |
| `base_url_invalid` | not https (http only on `127.0.0.1`/`::1`), credentials in the URL, or unparsable |
| `probe_model_missing` / `probe_model_astra` | no cheap probe model, or an Astra one |
| `wire_api_unsupported` | `wire_api` is neither `responses` nor `chat` |

D2 accepts the literal loopback addresses only. `localhost` is not accepted,
because honouring it would mean trusting a name lookup, and the box registry
already uses `127.0.0.1`.

D3 reads at most 4 KiB of an error body, JSON-decodes it so escaped CJK text
matches, and checks the measured shapes before the status: `Invalid URL (` is
`bad_route`, `不支持所选模型` is `not_offered`, `1m 上下文` is `needs_beta`, and
`已下线` is `retired` (the `claude-opus-4-6` deprecation body from the 404
entitlement log). Only then does the status decide: 402 `upstream_quota`,
401/403 `unauthorized`, 429 and 5xx `no_serving_channel`, anything else
`rejected`. The body itself is never stored; only the class is. The
`no_serving_channel` shape carries no model information (see D3 above), so it
never alerts and never names the configured model.

D5: selecting Plus always carries a `plus_fallback` alert. `cli.alerted()` posts
`provider: <alerts>` to the lobby once per changed alert set; a failed post keeps
the previous `alerts_sent`, so the next five-minute health run retries.
`astra.launch()` also calls `choose()` but does not post; `alerts_sent` is carried
through `selected()`, so the next health run announces whatever the launcher saw.
Only `no_serving_channel`, `upstream_quota` and `unreachable` stay silent. Any
other 4xx (`rejected`) and a 200 that is not a completion (`invalid_response`)
alert too, because a retry cannot fix either, and a relay skipped in silence
would leave the chain on a lower seat with nobody told. The one silent
structural case left is the shim dying: that is `unreachable`, and it waits on
D7.

The responses probe now asks for 16 output tokens, not 1. It has never run
against either relay, because the box registry has no `probe_model`, and the
OpenAI Responses API rejects `max_output_tokens` below 16 with
`400 Invalid 'max_output_tokens': integer below minimum value. Expected a value
>= 16, but got 1 instead.` (quoted from pi issue #6265; Azure's Responses
migration samples state the same minimum). A relay that forwards that check
would have failed every probe, and the relays could never have been selected.
16 is the smallest body the wire accepts, which is what the "responses-API
equivalent" of a one-token ping in `2026-09-20-spectre-control-plane.md` has to
mean. The chat probe keeps one token, and so does the Kimi probe; neither has
been measured against a live upstream either.

Two robustness gaps closed on the way. `http.client.HTTPException` (for example
`IncompleteRead`) escaped `providers.probe()`, `error_body()` and `kimi.probe()`
and would have crashed the health unit; it is now `unreachable` or
`kimi_probe_unavailable`. A 200 whose body is not a response or chat completion
used to fall into the same bucket as a dead socket; it is now its own class,
`invalid_response`.

Local gate:

```
$ env -u PYTHONHOME -u PYTHONPATH bash verify.sh
ok    slack bridge tests    # 64 pass, 0 fail
ok    devcodex tests        # 77 pass, 0 fail
ok    Ran 436 tests
verify: all gates passed
```

Python went 432 -> 436. The staged run on Spectre was skipped: ssh stopped at
the Tailscale check-mode prompt.

Effect once deployed, with the registry measured on the box: anyrouter's
`http://127.0.0.1:9995/v1` now passes D2, but neither relay has a `probe_model`,
so health reports `agentrouter:probe_model_missing` and
`anyrouter:probe_model_missing`. The health unit does not set
`SPECTRE_KIMI_ENABLED`, so the chain then selects Plus with `plus_fallback`,
which is visible in the lobby rather than silent. The operator fix is a cheap
non-Astra `probe_model` on both entries of `~/.codex/modes/providers.json`.
Still not deployed.

## P2 first slice — seat-aware planner dispatch — 2026-09-25

D8. The planner registry entry now names its harness: `planner.harness` is
`codex`, `claude` or `kimi`, and a missing field means `codex`, so every
existing registry keeps its meaning. `seat.PLANNER_ROLES` maps the seat owner
to the process role that must be running behind the pin (`codex` -> `astra`,
`claude` -> `claude`, `kimi` -> `kimi`). The same mapping lives in
`inventory.model()`, `dispatch-process.mjs` and the bridge's `PLANNER_ROLES`,
and the four have to agree.

`runtime.start_plan()` reads the seat before anything else. A codex seat runs
the gates it always had (budget refusal, provider health, the Kimi handoff
request, provider restart) and additionally refuses a pin whose harness is not
codex (`planner_harness_mismatch`). Any other owner plans only through a pin of
its own harness and skips the codex gates, because Plus caps and relay health
say nothing about a Claude or Kimi seat; the ledger records the owner's best
ladder provider (`anyrouter` for claude, `cline-free` for kimi). A pin of the
wrong harness escalates `seat_owned_by_<owner>` without typing, and a pin with
no terminal escalates `planner_terminal_missing`. The assignment timeout is now
per owner and stored on the request: Claude gets 900 s, because one anyrouter
edge window ends in a 524 at about 301 s and a planning turn can span several;
everyone else keeps 300 s.

Identity: `claude --model claude-opus-5-5` (also through `node`) is `claude`,
`kimi -m cline/kimi-k3` is `kimi`, and option parsing stops at a bare `--`.
That last rule keeps the Fedora `claude bg-pty-host` wrapper, whose argv
carries its child's argv after `--`, from counting as a second Claude planner;
the child itself still counts once. Pin sync picks the planner terminal by the
pinned harness and refuses an unknown one without touching the pin.
`kimi.confirm()` rewrites the planner pin to the Kimi terminal
(`harness: kimi`, `provider: kimi_free`), `kimi.release()` rewrites it to
`{terminal: null, harness: codex}` and returns `next: launch_astra`, and a
registry write that fails after the seat commit is reported as
`registry_update_failed` with `seat_committed: true`. The loop then fails
closed on the mismatch instead of planning through the wrong harness. Kimi
launch and release now refuse while any planner process, not only Astra, runs
in the worktree.

The bridge counts the pinned harness's planner: `plannerPidVerdict()` keeps the
plus-burn refusal for Astra only and otherwise demands exactly one process
(`dispatch_claude_busy`, `dispatch_kimi_busy`); an unknown harness is
`dispatch_planner_unknown`. Both refuse before any terminal work. The native
process guard treats a Claude or Kimi process in a packet pin as the wrong
agent.

Local gate (`d81121c` + `620be83`):

```
$ env -u PYTHONHOME -u PYTHONPATH bash verify.sh
ok    slack bridge tests    # 70 pass, 0 fail
ok    devcodex tests        # 77 pass, 0 fail
ok    Ran 464 tests
verify: all gates passed
```

The local count includes 18 tests from `tests/test_relay_probe.py`, which is
another workstream's untracked file. Staged on Spectre from
`git archive 620be83` into `/tmp` and removed afterwards:

```
VERIFY_EXIT=0 rev=620be83 host=spectre
SKIP  shellcheck not installed (apt/dnf install shellcheck)
ok    slack bridge tests    # 70 pass, 0 fail
ok    devcodex tests        # 77 pass, 0 fail
ok    Ran 446 tests
verify: all gates passed
```

Python went 436 -> 446 and the bridge suite 64 -> 70.

Still open after this slice:

- Nothing moves the seat to Claude yet. There is no Claude launcher, and
  `seat.transition(to='claude')` is reachable only by hand. A Claude planner
  must carry `--model claude-opus-5-5` on argv (plain `claude` has no identity)
  and needs write access to the packet directory. Unverified on Spectre:
  Claude process identity, whether a daemon-hosted Claude inherits
  `ORCA_TERMINAL_HANDLE`, and whether the Claude TUI submits on
  `orca-ide terminal send --enter`.
- The planner pid count is box-wide. A second
  `claude --model claude-opus-5-5` anywhere on the box, an operator session
  included, makes `plan` refuse with `dispatch_claude_busy`.
- `ASTRA_ENABLED` still gates planning for every seat, and `dry_action()` still
  reports `plan` without looking at the seat.
- Seat transfer stays operator-confirmed. A non-codex seat has no relay
  recovery: nothing hands it back to codex when the relays return. A Kimi seat
  whose free quota is gone is not detected either; each plan times out after
  300 s, is retried 300 s later, and every timeout posts its own
  `assignment_timeout:<rid>` alert.
- After `kimi.release()` the loop escalates `planner_terminal_missing` until
  the Astra launcher or pin sync sets a terminal.
- `cli.loop_main()` reads the registry before the tick takes the loop lock, so
  a tick racing `kimi.confirm()` or `kimi.release()` can raise one spurious
  `seat_owned_by_kimi` or `planner_harness_mismatch`. It fails closed, and the
  next tick reads the new registry.
- `astra.launch()` still looks only for a running Astra in the worktree.
- `astra.create_terminal()` and `kimi.launch()` still create terminals with
  `--worktree path:<dir>`, which binds an unregistered worktree record and
  leaves the tab invisible in Orca. That is the next slice.
- `config/astra-plan-prompt.md` is harness-generic except for the
  `requires_astra_review` schema field.

Still not deployed.

## Orca terminals bind to a registered worktree — 2026-09-25

This closes the `path:` item left open by the P2 first slice. Every terminal
the control plane creates now goes through one rule: `orca.create()` for
`astra.create_terminal()` and `kimi.launch()`, and `createOrcaTerminal()` for
the bridge's `ensurePacketPin()` and `startPacketJob()`. Each first reads
`orca-ide worktree ps --json` and needs exactly one unarchived row whose `path`
is the target cwd. It then runs `orca-ide terminal create --worktree active`
with that cwd as the process working directory, so the active selector resolves
to the registered record, and compares the `worktreeId` that comes back with
the registered one. `--worktree path:<dir>` is gone. It returned a connected,
writable terminal bound to a worktree record the ADE does not render.

Nothing is created when the listing fails (`orca_ps_failed`), has an unknown
shape (`orca_ps_invalid`), is truncated without the row (`orca_ps_truncated`),
or holds zero or several matching rows (`orca_worktree_unregistered`, with the
row count); a truncated listing is not proof of absence. The bridge reports the
same cases with the cwd in the message. A terminal that comes back bound
anywhere else is closed with `terminal close --tab` and reported as
`orca_terminal_invisible` with both ids and whether the close worked. A create
that timed out or returned no `term_` handle is not retried: the tab may exist,
so the error says to inspect `terminal list` first. Astra and Kimi record
nothing after a failed create, so neither the planner pin nor the pending Kimi
handoff can point at an invisible tab. `io.run()` gained a `cwd` argument for
this.

Smoke on Spectre from the staged tree, against the live Orca: `orca.create()`
on the minecraft worktree with the title `cp-smoke` returned `ok`, a `term_`
handle, the registered id
(`bbc15fac-9ef1-426c-a4ba-81a9e6346afd::/home/person/Projects/minecraft-server-project`)
and `surface: background`. `terminal list` showed the tab bound to that id,
connected and writable; `tidy.close()` closed it, and the next listing no
longer had it. `surface` reads `background` for a correctly bound tab, so it
says nothing about visibility. The ADE client itself was not looked at; the
evidence is the id match.

The same smoke showed that a title is not an identity. Within 3 s the shell's
OSC title had replaced `cp-smoke` with
`person@spectre: ~/Projects/minecraft-server-project`, and agents set their own
(qodercli shows "◇ Gemini CLI", codex shows its task). Of the 47 terminals
listed on Spectre, 35 carry the `person@spectre: <cwd>` form and none carries a
control-plane title. Everything keyed on titles is therefore inert live:

- `tidy.select()` picks `flash <id>`, `mimo <id>`, `flash-packets` and
  `mimo-packets` tabs by title, so the reaper's tab tidy has selected nothing.
- `ensurePacketPin()` reuse by title never matches; while no pin is recorded,
  each dispatch creates another shell.
- `packetJobTitle()` job tabs cannot be recognized after creation.
- The reaper's `untitled` rule matches `''`, `bash`, `person@spectre` and
  `(person@spectre shell, untitled)…`, never `person@spectre: <cwd>`, so it
  terminates nothing. That is the safe failure and it stays: widening the rule
  would terminate idle operator shells.

Live state on 2026-09-25: the minecraft worktree holds 24 idle `bash` shells
started by Orca, with no agent identity and no pin. All are bound to the
registered id, so the operator sees them. They were started between 2026-09-18
16:07 and 2026-09-19 00:53 UTC, a median 16 minutes apart, and all last printed
between 2026-09-20 01:11 and 01:16 UTC. No journal entry records who created
them. A bridge that creates a packet shell per dispatch and cannot record the
pin would produce this pattern, and the live registry is `root:root 0644`, not
writable by the bridge user, holding only the three top-level pins (zzbrush,
minecraft, korea-metro-twin). That is unverified, so the shells were left
open: the operator sees them, their origin is unknown, and a close cannot be
undone.

Local gate (`f155dd9`):

```
$ env -u PYTHONHOME -u PYTHONPATH bash verify.sh
ok    slack bridge tests    # 72 pass, 0 fail
ok    devcodex tests        # 77 pass, 0 fail
ok    Ran 473 tests
verify: all gates passed
```

The local count again includes the 18 tests of `tests/test_relay_probe.py`.
Staged on Spectre from `git archive f155dd9` into `/tmp/rv-stage-f155dd9`;
this time the tree and its log were left in `/tmp`:

```
VERIFY_EXIT=0 rev=f155dd9 host=spectre
SKIP  shellcheck not installed (apt/dnf install shellcheck)
ok    slack bridge tests    # 72 pass, 0 fail
ok    devcodex tests        # 77 pass, 0 fail
ok    Ran 455 tests
verify: all gates passed
```

Python went 446 -> 455 (eight `orca` tests, one Kimi test) and the bridge
suite 70 -> 72.

Still open after this slice:

- Titles carry no identity, so tab tidy, title reuse and job-tab recognition
  are inert. Next slice: the creators write an ownership record per handle at
  creation, and tidy closes only recorded tabs.
- The 24 idle minecraft shells stay open until the operator decides.
- A truncated `worktree ps` listing without the row refuses creation; nothing
  pages through it.
- The other open items of the P2 first slice stand.

Still not deployed.

## Tab records, the listener guard and null titles — 2026-09-25/26

Three commits close the title item above and the first two things that stopped
the reaper from producing any output on Spectre.

**`2afa98d` — a tab is owned by its create-time record.** Every tab the bridge
creates is written as `<packet_dir>/tabs/<handle>.json` (role, kind,
`dispatch_id`, cwd, `worktree_id`, `created_at`, mode 600), and that record is
the only ownership evidence tidy accepts. `ensurePacketPin()` reuses the newest
live recorded shell for the same role and cwd, so a missing registry pin no
longer creates a shell per dispatch. The record directory is checked before the
create, and a record that cannot be saved closes the tab it describes. A failed
`persistWorkersPin()` is audited as `dispatch_pin_persist_failed` instead of
being dropped. The reaper closes recorded job tabs whose `.exit` sidecar is past
the grace window, and retires records whose tab has left a non-truncated
listing. A failed close now carries an error, so `ok` reports it.

Smoke on Spectre against the live Orca, packet dir `/tmp/tabrec-smoke-6a6k6p`:

```
first {"ok":true,"created":true,"handle":"term_7cec49c2-…"}
record {"role":"flash","kind":"shell","dispatch_id":null,
        "cwd":"/home/person/Projects/minecraft-server-project",
        "worktree_id":"bbc15fac-…::/home/person/Projects/minecraft-server-project",…} mode 600
row {"connected":true,"writable":true,"titleStillFlashPackets":false}
second {"ok":true,"created":false,"sameHandle":true}
closed true
listed_after_close false
```

The second `ensurePacketPin()` reused the recorded shell even though its title
had already been replaced. A `tidy.sweep()` dry-run over the same listing
(47 of 47 tabs, `truncated: false`) saw the one record and selected nothing for
closing at the default grace.

**`827c65f` — root's listeners no longer block the reaper.** Run as a user
unit, `ss -ltnpH` cannot name root's listeners: 10 of Spectre's 21 (sshd,
tailscaled, cups, cockpit) lack `pid=`, so every run refused with
`listener ownership incomplete; refusing reap` before any decision or sweep.
The reaper now reads `ss -ltnpeH`, which prints no `uid:` for root. Only this
uid's unattributed listeners refuse. A refusal skips process reaping alone: the
tab sweep still runs, and the error names the listeners.

**`d7f43e9` — an unreported title is no evidence.** Spectre's Orca lists 46 of
47 tabs with `title: null`. `terminal.get('title', '')` returned `None` for
those, and `startswith` raised `AttributeError`, so the first live run past the
listener guard died without printing JSON. Only a reported string title can
mark a shell untitled now; a null or missing one keeps the shell.

Gates, local and then from `git archive <rev>` staged on Spectre:

| Rev | Local | Spectre staged |
| --- | --- | --- |
| `2afa98d` | bridge 77, devcodex 77, `Ran 481 tests` | bridge 77, devcodex 77, `Ran 463 tests` |
| `827c65f` | `Ran 484 tests` | bridge 77, devcodex 77, `Ran 466 tests` |
| `d7f43e9` | not run | bridge 77, devcodex 77, `Ran 467 tests` |

Each staged run ended `verify: all gates passed` with `SKIP shellcheck not
installed`. The local count again includes `tests/test_relay_probe.py`.

### The first full dry-run shows the reaper is not safe to enable

With both guards fixed, a dry-run from an ssh shell on Spectre (`d7f43e9`,
`--state` in `/tmp/reaper-smoke-oOFCqn`) produced the first complete decision
set: 185 processes, 172 `keep` and **13 `term`**. Every one of the 13 was
something the operator wants running:

| Class | Processes | Rule that fired |
| --- | --- | --- |
| Live agent sessions | `cline`, `codex` ×2, `claude`, `qodercli` | RSS ≥ 256 MiB or `duplicate` |
| Game servers | `java` on 42571; `sh` + `java` on 25566 | listening port |
| Podman forwarders | `pasta` on 5432 and 15433, no Orca handle | listening port |
| A dev preview | `npm`/`sh`/`vite` on 5180, no Orca handle | listening port |

The rules are generic hygiene heuristics (a listening port, RSS above a limit,
a second agent in a registered cwd), and on a box where people run things they
match whatever is running. Apart from the fixed allow-lists, the guard meant
to exempt live work is `protected`, and it points the wrong way.

`protected` walks each process's ancestry to `roots = {getpid(), getppid()}`
plus the registered tmux panes. From an ssh shell, `getppid()` is that shell,
which protects almost nothing. Under the transient user unit the timer would
use, `getppid()` is the user manager (`systemd --user`, pid 1408), which is an
ancestor of nearly every user process. A check of the 13 candidates inside
such a unit found 10 alive under 1408, protected, the two `pasta`
forwarders outside it, so still `term`, and `cline` already gone. The same guard therefore protects
nearly everything in one context and nearly nothing in the other, and in
neither does it track what the control plane actually owns.

The unit run itself (`/tmp/reaper-unit-QhVKAC`) ended before any decision:

```
UNIT_EXIT=1
{"ok": false, "error": "Orca inventory unavailable; refusing reap"}
```

It printed nothing on stderr. `orca-ide terminal list --json` run directly in
a unit with the same properties succeeded three times, so the cause is still
unknown. The message drops `run()`'s own error, which is the first thing to
fix.

### Deployment state and what it means for this slice

`devlog/2026-09-26-control-plane-deploy.md` records the first deploy of the
control plane to Spectre, at `d7f43e9`, which replaces the "Still not deployed"
lines above. For the reaper this means:

- `/usr/local/bin/spectre-reaper` is byte-identical to `scripts/spectre-reaper.py`
  at `d7f43e9`, the unscoped version described here.
- `spectre-reaper.timer` (`OnBootSec=180s`, `OnUnitActiveSec=5min`) is
  installed and **disabled**, as are the loop, pin-sync and provider-health
  timers.
- Enabling it, or running the binary with `--apply` from a shell, would
  terminate the processes in the table above. Both stay off until the reaper
  terminates only what the control plane owns.

Still open after this slice:

- Ownership scoping (next slice): `term` only for a process whose Orca handle
  has a tab record or a registry pin; everything else is kept regardless of
  port, RSS or duplicate. `protected` becomes the reaper's own ancestor chain
  plus the tmux panes instead of the `getppid()` subtree.
- The Orca-unavailable refusal must carry `run()`'s error.
- Processes started by a tab whose record was retired lose their owner and are
  kept. That is the intended failure direction.
- The 24 idle minecraft shells remain open, and no creator other than the bridge
  (Astra, Kimi) writes tab records yet.

## The reaper cleans up only recorded tabs — 2026-09-26

This slice makes the reaper safe to run against the box as it is. It does not
enable the timer or reinstall the binary.

**Ownership gate.** `decide()` takes a required `owned` set, which `live()`
builds from the tab records in `<packet_dir>/tabs/`. The order of the checks is:

1. Allow-listed ports, `protected`, unknown listeners, `NEVER_KILL` and Astra
   are kept, as before.
2. A registry pin keeps everything except a headless Flash run that finished
   more than `FLASH_GRACE` ago, as before.
3. **New:** a process without an Orca handle, or whose handle has no tab record,
   is kept.
4. The generic rules (Minecraft names, listening port, RSS, CPU, duplicate,
   untitled, unpinned finished Flash) now see only processes in recorded,
   unpinned tabs, which are the bridge's job tabs.

A process left behind by a tab whose record was retired loses its owner and is
kept. So are tabs opened by Astra and Kimi, which do not write records yet.

**Point protection for the parent.** `observe()` protected the subtrees of
`getpid()` and `getppid()`. Under a user unit `getppid()` is `systemd --user`,
so everything was protected. Now the reaper's own ancestor chain and
`getppid()` are protected only as individual processes. The subtrees still
protected are the reaper's own and the registered tmux panes'.

**The Orca refusal names its cause.** It now reads
`Orca inventory unavailable (<run() error>); refusing reap`.

### Dry-runs on Spectre

Two dry-runs of the working-tree script, without `--apply`, each with its own
`--state` file:

| Context | Result |
| --- | --- |
| ssh shell | `ok: true`, 102 decisions, 102 `keep` |
| transient user unit (`NoNewPrivileges`, `Nice=10`, unit `PATH`) | `ok: true`, 100 decisions, 100 `keep` |

The unit run no longer fails with `Orca inventory unavailable`. The earlier
failure did not recur, so its cause is still unknown. If it recurs, the new
message will name it.

Keeping every process is expected when nothing is owned. The one tab record on
the box (`term_e25e81fa…`, the `flash-packets` shell from the deploy) points at
a tab that is no longer listed. Both runs report it as `tab_gone`, dry-run. The
registry's Flash pin and the Astra planner pin (`term_08fdfa68…`) point at
tabs that are also gone.

A counterfactual run gave ownership to every observed handle (11 tabs) to check
that the gate, and not over-protection, is what keeps the processes:

| Context | `protected` | `term` with every handle owned |
| --- | --- | --- |
| ssh shell | 12 | 1: a `claude` at 279 MiB RSS |
| user unit | 7 | 2: the same `claude` and this session's `claude` at 381 MiB |

The unit no longer protects the whole user session. The rules that remain
would still select an interactive Claude session by RSS alone, so a launcher
that records a tab for a long-lived agent must pin it as well.

### Gates in the Lightning Studio

`verify.sh` ran through `spectre-offload` twice in one Studio session: once for
`git archive HEAD` (`7dcf243`) and once for the same tree plus the three changed
files. Both used Node 22.23.2 (under `$HOME/.node22`) and `/usr/bin/python3`
3.12.3:

| Gate | HEAD `7dcf243` | This slice |
| --- | --- | --- |
| `bash -n` | 32 ok | 32 ok |
| shellcheck | SKIP, not installed | SKIP, not installed |
| `py_compile` | ok | ok |
| slack bridge, `node --test` | 77 pass | 77 pass |
| devcodex, vendored | 69 pass, 8 fail | 69 pass, the same 8 fail |
| unit tests | 512 run, 1 failure | 517 run, the same 1 failure |

`verify.sh` exits 1 for both trees, for the same two reasons. Both come from
the Studio, not from the code:

- All 8 devcodex failures (`runShellCommand`, `runQualityGates`, the lifecycle
  hooks, `runCompletionGate`) spawn `/bin/sh -lc`. The Studio's login profile
  breaks dash: `/bin/sh -lc true` prints `/bin/sh: 31: Bad substitution` and
  exits 2.
- `test_session_name_falls_back_to_home` expects `codex-$USER`, but
  `session_name` takes the basename of `$HOME`. In the Studio `$HOME` is
  `/teamspace/studios/this_studio`, so the result is
  `'codex-this_studio' != 'codex-person414213'`.

The 5 new tests pass. No test that passes at HEAD fails with this slice.

The first attempt used the Studio's defaults, and those are not a usable
toolchain:

- apt's Node 18.19.1 printed the bridge summary (`# pass 77`, `# fail 0`) and
  then did not exit. It sat in `ep_poll` for more than 1000 s until the runner
  was killed.
- The login shell's `python3` has no `os.pidfd_open`, so
  `test_live_child_signal_checks_identity` fails with an `AttributeError` in
  `terminate()`. `/usr/bin/python3` has `os.pidfd_open`.

Still open:

- Reinstalling `/usr/local/bin/spectre-reaper` from this revision is a deploy
  step for the operator. The timer stays disabled until then.
- The stale Flash and planner pins, and the record of the gone `flash-packets`
  tab, are left as they are. The next Flash dispatch provisions a new shell, and
  an `--apply` sweep would retire the record.
- Astra and Kimi do not write tab records yet. `terminal list` truncation,
  exceptions that escape `main()`, and the tab sweep running outside the lock
  are also still open.

## The reaper holds its lock for the whole run — 2026-09-26

This slice closes two of the reaper items above. The third turned out to be
closed already.

**The tab sweep runs under the lock.** `reap_processes()` held `reaper.lock`
only around `observe()` and the kills. `tidy.sweep()` ran after the lock was
released, and on the listener-refusal path it ran with no lock at all. Now
`live()` takes the lock before it reads `ss`, the Orca listing and the tab
records, and releases it after the sweep. A run that waited for another run
therefore cannot close or retire tabs from a listing taken before the other run
acted. When another run holds the lock, the reaper exits 1 before it reads any
inventory. The error is `[Errno 11] Resource temporarily unavailable`, which
does not name the lock. `io.locked()` has eight other callers, so the message
is left as it is.

**`main()` always prints a verdict.** An exception outside `OSError`,
`ValueError` and `SubprocessError` used to escape as a bare traceback, with
nothing on stdout. Now it is reported as `{"ok": false, "error": "<Type>:
<message>"}` with exit 1. The traceback still goes to stderr, where the journal
keeps it. The test case is a malformed Orca listing whose `result` is a list:
`AttributeError: 'list' object has no attribute 'get'`.

**`terminal list` truncation was already fail-safe.** `sweep()` skips
`stale()` when the listing is truncated, and `select()` closes only tabs it
sees. On the process side, a tab missing from the listing cannot make its shell
`untitled`, and `duplicate` does not read the listing. A truncated listing can
only turn a `term` into a `keep`.

### Verification

Dry-runs of the working-tree script on Spectre from an ssh shell, without
`--apply`, with a scratch `--state`:

| Run | Result |
| --- | --- |
| lock free | `ok: true`, 101 decisions, 101 `keep`; `term_e25e81fa…` `tab_gone`, dry-run |
| `flock -n` holding the scratch `reaper.lock` | `{"error": "[Errno 11] Resource temporarily unavailable", "ok": false}`, rc 1 |
| lock released again | `ok: true`, 102 decisions |

`verify.sh` ran through `spectre-offload` for `git archive HEAD` (`8e8be50`)
and for the same tree plus the two changed files, with the toolchain of the
previous slice (Node 22.23.2, `/usr/bin/python3` 3.12.3):

| Gate | HEAD `8e8be50` | This slice |
| --- | --- | --- |
| `bash -n` | 32 ok | 32 ok |
| shellcheck | SKIP, not installed | SKIP, not installed |
| `py_compile` | ok | ok |
| slack bridge, `node --test` | 77 pass | 77 pass |
| devcodex, vendored | 69 pass, 8 fail | 69 pass, the same 8 fail |
| unit tests | 517 run, 1 failure | 519 run, the same 1 failure |

The failures are the two Studio issues recorded for the previous slice. The
same run copied the new test file onto the HEAD tree and ran the two new tests
against the HEAD reaper. Both fail there:
`test_a_held_lock_stops_inventory_and_sweep` fails because `ss` ran while the
lock was held (`AssertionError: reaper read inventory or swept while another run
held the lock`), and `test_unexpected_error_still_prints_a_json_verdict` errors
with the escaped `AttributeError: 'list' object has no attribute 'get'`.

Still open:

- Reinstalling `/usr/local/bin/spectre-reaper` is still the operator's deploy
  step, and the timer stays disabled.
- Lock contention reports `[Errno 11] Resource temporarily unavailable`
  without naming the lock.
- Astra and Kimi do not write tab records yet. A launcher that records a
  long-lived agent tab must pin it too, or the RSS rule selects it.

## The loop reads the registry under its own lock — 2026-09-26

This slice closes the `cli.loop_main()` registry race listed under P2 and gives
the loop the same always-a-verdict exit as the reaper.

**The tick reads the registry after it takes the loop lock.** `spectre-kimi
confirm` and `release` hold `spectre-loop.lock` while they commit the seat and
rewrite the minecraft planner pin. `loop_main()` read `workers.json` before
`runtime.tick()` took that lock. A tick that read the registry just before a
`confirm` and took the lock just after it therefore paired the new Kimi seat
with the Astra pin it had read earlier and escalated `seat_owned_by_kimi`. The
same window around a `release` escalated `planner_harness_mismatch`. (A tick
that finds the lock held does not wait; it exits 1 with `[Errno 11]`.) Now
`tick()` takes a `load` callable.
When one is given, it reads the registry again after taking the lock, refuses
an empty one, and validates it before `live_tick()` sees it. `loop_main()`
passes one that re-reads `--workers-file`. The first read stays, because it
validates the registry early and is all that `--dry-run` uses. A dry run takes
no lock.

The Astra launcher and `native-worker-pin-sync` write the registry under the
registry lock only. Neither can pair a pin with a different seat. The launcher
sets the codex planner pin after `release()` has already moved the seat to
codex, and pin sync does not touch `planner`. A tick that reads just before
either of them acts on the registry as it was a moment earlier.

**`loop_main()` always prints a verdict.** An exception outside `OSError`,
`ValueError` and `RuntimeError` used to escape as a bare traceback, with
nothing on stdout. Now it is reported as `{"ok": false, "error": "<Type>:
<message>"}` with exit 1, and the traceback goes to stderr. The test case is a
loop state whose `workers` field is a list. `read_json()` accepts it because
the top level is an object, and `progress()` fails with `AttributeError:
'list' object has no attribute 'get'`. The loop lock is released afterwards.

### Verification

A dry run of the working-tree `scripts/spectre-loop.py --dry-run` on Spectre,
with `SPECTRE_LOOP=1` and a scratch `SPECTRE_LOOP_STATE`, exited 0 with five
decisions (`goal` for `pugc`, `skip` for the other four) and an empty stderr. It
wrote neither a lock nor a state file. The live path was not run on Spectre,
because a live tick dispatches to real terminals. `spectre-loop.timer` is
disabled, and the installed `runtime.py` and `cli.py` are identical to HEAD.

`verify.sh` ran through `spectre-offload` for `git archive HEAD` (`6f399e2`)
and for the same tree plus the four changed files, with the toolchain of the
previous slices (Node 22.23.2, `/usr/bin/python3` 3.12.3):

| Gate | HEAD `6f399e2` | This slice |
| --- | --- | --- |
| `bash -n` | 32 ok | 32 ok |
| shellcheck | SKIP, not installed | SKIP, not installed |
| `py_compile` | ok | ok |
| slack bridge, `node --test` | 77 pass | 77 pass |
| devcodex, vendored | 69 pass, 8 fail | 69 pass, the same 8 fail |
| unit tests | 519 run, 1 failure | 523 run, the same 1 failure |

The failures are the two Studio issues recorded for the earlier slices. The
same run copied the two changed test files onto the HEAD tree and ran the four
new tests there. All four fail. Three fail only because HEAD has no `load`
parameter (`TypeError: tick() got an unexpected keyword argument 'load'`, and
the fake tick in the `loop_main()` test is called without one). The catch-all
test errors with the escaped `AttributeError: 'list' object has no attribute
'get'`. The stale pairing itself is already covered at HEAD: the seat case table
turns a Kimi seat with the Astra pin into `seat_owned_by_kimi`.

Still open:

- Reinstalling the control plane is the operator's deploy step, and the loop
  timer stays disabled.
- `ASTRA_ENABLED` still gates planning for every seat, and `dry_action()` still
  reports `plan` without looking at the seat.
