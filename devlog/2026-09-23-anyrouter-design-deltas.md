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
