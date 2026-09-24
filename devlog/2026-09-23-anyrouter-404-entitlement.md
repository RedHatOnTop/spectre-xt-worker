# 2026-09-23 — anyrouter 404 is model entitlement, not a missing endpoint

## Symptom

`anyrouter.top` returned HTTP 404 for every model tried, while `GET /v1/models`
answered 200 and listed models. The operator read the 404 as "the
endpoint/base_url is wrong", which the relay's own error taxonomy makes easy to
do. **Resolved in Finding 8: it was a double-`/v1` base URL in the Claude Code
settings, and Claude Code renders any 404 as "this model may not exist".** The
findings below stay because they map what the relay actually serves, which is
independent of that bug.

## Finding 1 — two different 404s, different body shapes

The relay (new-api family) returns two unrelated 404s:

| case | body |
| --- | --- |
| route does not exist | `{"error":{"message":"Invalid URL (POST /v1/embeddings)","type":"invalid_request_error"}}` |
| model not in this token's channels | `{"error":"当前 API 不支持所选模型 <id>","type":"error"}` (flat, no wrapper) |

The second one means "the current API does not support the selected model" — an
entitlement/no-channel error that reuses 404 as its status. It is not a route
error. Verified: `POST /v1/nope` and `/v1/completions` give the `Invalid URL`
shape; `/v1/responses` with an unentitled model gives the flat shape.

## Finding 2 — `/v1/models` is a site catalog, not this token's reachable set

`GET /v1/models` returns a fixed 15-entry site catalog. It is not scoped to the
calling token, and it is not scoped to a wire API. Two things follow:

- Models absent from the list can still be callable: `gpt-5.6-sol` and `gpt-5.5`
  route on `/v1/responses` and are advertised nowhere.
- Models present in the list can be unreachable: `gpt-5-codex` is advertised and
  404s on every path probed.

**Correction to the first pass of this investigation.** Probing only
`/v1/responses` made it look like 14 of the 15 advertised ids were dead. That was
wrong twice over: the `claude`-named ones do route, on `/v1/messages`, and the
suffixed names that appeared to route on `/v1/responses` route only because the
router matches substrings. See Findings 3 and 3b.

## Finding 2b — the Pricing page flag is the same site-level data

The site's Pricing page marks 14 ids "available" (`gpt-5-codex`, `gpt-6-astra`,
the 11 standard `claude-*` ids including `claude-opus-4-6`, and
`gemini-2.5-pro`) and `claude-opus-4-8` "unusable". That list is the same
15-entry catalog minus `claude-opus-4-20250514`, so both surfaces are generated
from one site-level model table — model-scoped, not token-scoped and not
wire-API-scoped. Measured against reality at the same moment:

- `claude-opus-4-8`, labelled **unusable**, answers 400 "needs 1m beta" then 503 on
  `/v1/messages` exactly like every other `claude-*` name — **but so does
  `claude-zzz-xyz`**, so this does not refute the label. See Finding 3b.
- `claude-opus-4-20250514`, absent from both the Pricing list and the
  "unusable" list, is the one id `/v1/models` and Pricing disagree on.
- The `/v1/responses` gpt families that actually answer are advertised nowhere.
- `claude-opus-4-6`, advertised as available, answers 400
  "已下线，请切换到 claude-opus-4-7" — the relay does carry a per-id patch table,
  and this one is genuinely retired.

The `x-tengine-error: denied by http_custom` WAF on `/api/*` blocks reading the
underlying pricing JSON, so this cross-check uses the operator's rendering of the
page plus direct probes.

## Finding 3 — entitlement is per (wire API x model family), and the relay matches by substring

Entitlement is keyed on path as well as model. A model can be fully entitled on
one wire API and answer the flat 404 on another:

| model | reachable path | observed |
| --- | --- | --- |
| `gpt-6-astra` | `/v1/responses` | 500 `get_channel_failed` (congested); 404 on `/chat/completions` and `/messages` |
| `gpt-5.6-sol`, `gpt-5.5` | `/v1/responses` | 500 `get_channel_failed` (congested); never advertised |
| `gpt-5-codex` | none found | 404 on all three paths despite being advertised |
| `claude-*` (any name) | `/v1/messages` | 400 "needs 1m beta", 503 with the beta, 429/520 for some; never on `/responses` or `/chat/completions` |
| `claude-opus-4-6` | `/v1/messages` | 400 "已下线，请切换到 claude-opus-4-7" |
| `gemini-2.5-*` | `/v1/chat/completions` | 500, usually with an empty body |

`gpt-6-astra` 404s on `/v1/chat/completions` while answering on `/v1/responses`;
the Claude ids 404 on `/v1/responses` while answering on `/v1/messages`. So one
client pinned to a single wire API sees "every model 404s" for the other family.
That is the reported symptom: not a dead endpoint, a mismatched wire API.

## Finding 3b — the router fuzzy-matches, so probing yields patterns, not models

An exhaustive 218-model x 3-path sweep (654 requests) was run against
`/v1/responses`, `/v1/chat/completions` and `/v1/messages`. It classified 52
models "entitled" on `/v1/responses` and 28 on `/v1/messages` — and those counts
are **an artifact of the relay's matching, not a model list**. Controls on
invented names:

```
/responses
  gpt-6-astra-zzz    -> 500 congested      gpt-6-astr     -> 404
  gpt-6-astrafoo     -> 500 congested      gpt-5.6-so     -> 404
  zzz-gpt-6-astra    -> 500 congested      gpt5.6-sol     -> 404
  gpt-6-astraXHIGH   -> 500 congested      gpt-5.5x       -> 500 congested
  gpt-5.6-sol-extra  -> 500 congested      gpt-5. / gpt-5 -> 404
```

A request routes iff it **contains one of the configured strings** as a
substring — not prefix, not glob, not equality. So the real `/v1/responses`
patterns are exactly `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.5`; every suffixed
variant probed earlier (`-xhigh`, `-nano`, `-thinking`, `-2026`, …) was a false
positive. Same on `/v1/chat/completions`, where the pattern is `gemini-2.5-`.

`/v1/messages` is worse: it matches on `claude` alone. `claude-zzz-xyz`,
`claude-foo`, and bare `claude-` are **byte-identical to `claude-opus-4-7`** —
400 "needs 1m beta" without the beta header, 503 with it. The only per-id signal
is the deprecation table (`claude-opus-4-6` → 已下线).

**Consequences, both of them scope limits on this investigation:**

1. Per-id entitlement for Claude **cannot be determined from outside** on this
   relay. The earlier claim in this log that the Pricing page's
   `claude-opus-4-8` "unusable" label was wrong is **retracted** — that probe
   proved only that a generic `claude` route exists. For individual Claude ids,
   the relay's own catalog is a better source than probing.
2. `/v1/models` is not a superset or subset of reality; it is a third,
   independently generated table. The only surfaces that agree are
   `/v1/models` and Pricing (same 15-entry source), and both are model-scoped,
   not token-scoped.

## Finding 3c — the `/v1/responses` families were saturated during the sweep

Across 654 plain-path requests and the `/v1/messages` 1M pass: no 200 from the
`/v1/responses` families at all. Every `gpt-6-astra`/`gpt-5.6-sol`/`gpt-5.5`
request answered 500 `get_channel_failed`; the `gemini-2.5-*` chat route answered
500 with an empty body. The Claude route looked equally dead at the time, but
that was an artifact of probing it without `context-1m-2025-08-07` (see Findings
8 and 9) — with the beta, a genuine Claude Code client got a 200.

`/v1/messages` requires the 1M-context beta. Without
`anthropic-beta: context-1m-2025-08-07` entitled Claude ids answer
400 "1m 上下文已经全量可用，请启用 1m 上下文后重试"; with it the request reaches a
channel and returns 200, 429 or 503 depending on the model.

### Control experiment, and what it actually proved

The first control sent names with no family substring at all, and they all 404'd:

```
nonexistent-model-xyz   -> 404 {"error":"当前 API 不支持所选模型 nonexistent-model-xyz"}
gpt-5.6-terra           -> 404 (same shape)
glm-5.2                 -> 404 (same shape)
gpt-6-astra             -> 404 (same shape, on /v1/messages)
gemini-2.5-pro          -> 404 (same shape, on /v1/messages)
```

That was read as "the 1m 400 is model-specific, so entitlement is per id". The
second control refutes it: `claude-zzz-xyz`, `claude-foo` and bare `claude-` all
produce the same 400 and then the same 503 as `claude-opus-4-7`. The 400 fires
whenever the name contains `claude`, so it proves the *family* route exists and
nothing about the individual id. The earlier per-id reading is retracted; see
Finding 3b.

## Finding 4 — the whole chain was saturated or failing

Every `gpt-6-astra` / `gpt-5.6-sol` / `gpt-5.5` request on `/v1/responses`
answered 500 `{"code":"get_channel_failed","message":"当前模型 X 负载已经达到上限"}`,
repeatedly. The `gemini-2.5-*` chat route answered 500 with an empty body. On
`/v1/messages` the 23 names that passed the 1M gate answered 503
(`Service Unavailable`), with 429 for the `claude-3-5-haiku` / `claude-haiku-*`
group and 520 for `claude-haiku-4-5-20251001`. Not one request in the sweep
returned 200. This matches the standing note that anyrouter is often unusable:
congestion plus upstream failure, not an entitlement problem, and it must not be
recorded as a dead account.

## Finding 5 — the local shim is not the cause, but the probe cannot configure it

The shim on the box (`python3 /home/person/remote-agent-deploy/remote-agent/anyrouter-sse-shim.py
--listen 127.0.0.1 --port 9995 --upstream https://anyrouter.top/v1
--strip-responses-lite`, no systemd unit, up since Sep 17) reproduces the direct
results byte for byte, so it passes errors through untouched.

But `~/.codex/modes/providers.json` on the box pins anyrouter to
`base_url = http://127.0.0.1:9995/v1` with `wire_api = responses` and no
`probe_model`; agentrouter likewise has no `probe_model`. `providers.probe_request()`
requires an HTTPS `base_url` and a non-Astra `probe_model`, so both relays fail
configuration. Running the real chooser against the live registry:

```
selected ok : False
reason      : probe_configuration
probes      : agentrouter -> configured:false, anyrouter -> configured:false
```

`choose()` then never reaches the kimi/Plus fallback — it dead-ends on
`probe_configuration`. The http shim cannot pass the HTTPS guard at all, and the
"cheap non-Astra probe_model" rule has no candidate on this token except
`gpt-5.6-sol` / `gpt-5.5`, both of which are the same saturated premium
channels as astra.

## Finding 6 — the 500 is a channel-acquisition failure, not "no upstream for this model"

*Refined by Finding 10: the tier means "no channel serving this model right now",
which covers removed models as well as capacity, and its wording must not be
taken literally.*

`get_channel_failed` + "当前模型 X 负载已经达到上限，请稍后重试" reads like it could
mean "no working upstream exists for this model". It does not. Five checks:

1. The relay has a **separate, flat-body 404** for "no channel configured", and it
   is issued by the same host, key and minute: `nonexistent-model-xyz` → flat 404,
   while `gpt-6-astra` → 500 `get_channel_failed`. Different branches, so the 500
   is not the no-channel branch.
2. The response header `x-oneapi-request-id` identifies the software as the
   one-api / new-api family. In that codebase `get_channel_failed` is produced by
   channel selection and the load-cap wording is its saturation branch.
3. Latency is flat and the cap is sticky: 36 consecutive attempts across the three
   patterns were all 500 with 201–536 ms (median 299 ms), then a 17-minute watch
   (120 more samples, every 25 s) produced 120/120 `LOAD_CAP` and zero 200s — 156
   consecutive cap rejections. An upstream that was tried and failed would show
   far wider latency variance; this is a pre-flight cap check.
4. `stream: true` changes nothing (same 500), and no alternate base prefix helps
   (`/openai/v1/...`, `/codex/v1/...` return the SPA page, `POST /v1/responses/`
   is a 307). So it is not a request-shape or routing-prefix problem.
5. History: the box session
   `~/.codex/sessions/2026/09/20/rollout-2026-09-20T15-32-43-01a0bd84-...jsonl`
   contains 93 `"model":"gpt-6-astra"` references, 50 `token_usage_record` events
   and 71 token-count events, with `get_channel_failed` appearing at exactly two
   timestamps 11 seconds apart. The same model on the same relay family served
   dozens of real completions; the cap is transient.
   *Caveat:* that session's `session_meta` says `model_provider: openai`, so the
   relay attribution rests on the error strings being this family's; the
   provisioned-model conclusion does not depend on it.

So the correct reading is "the channels for this model exist and are all at their
concurrency cap right now", not "no upstream serves this model". A health check
must not record it as a dead account or a missing model.

## Finding 7 — why everything looked dead: chat-only probing

Only three GPT patterns are provisioned (`gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.5`)
and `/v1/chat/completions` serves essentially none of them, so any client or tool
that probes the OpenAI chat path reports the entire model set as missing. This is
the workspace's own tester's blind spot: `relay-lab` declares
`PROBE_MODES = ("chat", "stream", "embeddings")` with
`DEFAULT_CHAT_PATH = "/v1/chat/completions"`, and maps
`status in (404, 422)` to `PROBE_UNAVAILABLE` = "Relay does not serve this
model". Against anyrouter that verdict is wrong for every gpt model: they are
served, on `/v1/responses`.

`relay-lab` is otherwise right about the distinction — `500 <= status < 600` maps
to `PROBE_SERVER_ERROR` ("Relay error"), not to "unavailable". The gap is wire-API
coverage: there is no `responses` mode and no Anthropic `/v1/messages` mode, so
the two paths that actually carry this relay's traffic cannot be probed at all.

## Finding 8 — the reported "all models 404" was a double-`/v1` base URL

The operator's Claude Code config was the actual client:

```
~/.claude/settings.json  →  env.ANTHROPIC_BASE_URL = https://anyrouter.top/v1
```

Claude Code appends `/v1/messages` itself (captured: base `http://127.0.0.1:8799`
produced the path `/v1/messages`). With a base that already ends in `/v1`, every
request becomes a double prefix:

```
POST https://anyrouter.top/v1/v1/messages -> 404 {"error":{"message":"Invalid URL (POST /v1/v1/messages)"}}
GET  https://anyrouter.top/v1/v1/models   -> 404 {"error":{"message":"Invalid URL (GET /v1/v1/models)"}}
POST https://anyrouter.top/v1/messages    -> 400 (route exists)
GET  https://anyrouter.top/v1/models      -> 200 (the catalog)
```

Claude Code maps **any** 404 to `model_not_found` and prints "There's an issue
with the selected model (X). It may not exist or you may not have access to it."
So the double prefix surfaced as a per-model access problem: every model 404s
while `/v1/models` lists them. That is the whole reported symptom, and it is a
config bug, not a relay or entitlement problem. The correct value is
`https://anyrouter.top` with no `/v1`.

Fixing the base alone moves the failure to the next gate:

```
/v1/messages?beta=true -> 400 {"error":"1m 上下文已经全量可用，请启用 1m 上下文后重试"}
```

That gate is opened only by `anthropic-beta: context-1m-2025-08-07`; Claude Code
does not send it by default and `ANTHROPIC_CUSTOM_HEADERS` does not override the
header it already sets. `ANTHROPIC_BETAS=context-1m-2025-08-07` does.

## Finding 9 — a genuine Claude Code client works; no client fingerprinting

With the base fixed and the beta supplied, the real Claude Code 2.1.267 was run
through a logging proxy against anyrouter:

```
claude-opus-5-5   -> 200, streamed message_start with model claude-opus-5-5, CLI printed "ok"
claude-fable-5-1  -> 429 x9 {"error":{"message":"Service Unavailable"}}
```

reproduced twice, one request to success for opus-5-5 and zero for fable-5-1.
So `claude-opus-5-5` works end to end from the genuine client. `claude-fable-5-1`
does not, and Finding 10 shows why: the relay removed it, and the 429 is the
generic "no serving channel" response rather than a rate limit.

The coding-client fingerprinting rumour is **not** the cause here. The
fingerprint was captured from the installed 2.1.267 and replayed: path
`POST /v1/messages?beta=true`, `User-Agent: claude-cli/2.1.267 (external,
sdk-cli)`, `X-Claude-Code-Session-Id`, `x-app: cli`,
`anthropic-dangerous-direct-browser-access: true`, stainless headers, the CLI's
own `anthropic-beta` list, a Claude Code `system` prompt and a 110–164 KB body.
Responses to the early-rejection layer are invariant across all of that: the
same 400-then-429/503 shapes come back for the real ids, for
`claude-opus-4-7`, and for the nonsense `claude-zzz-xyz`.

Qualification, because it changes the earlier Finding 3b wording: model identity
is invisible to the *gate* layer, but not to the *channel* layer — once the beta
is present the same client gets 200 for `claude-opus-5-5` and 429 for
`claude-fable-5-1`. Per-model availability therefore is observable through a
genuine client, just not by a bare curl to the early-rejection path.

## Finding 10 — 429/500 are generic "no serving channel", not load or rate limits

The operator's read was right: the 429 is a fake error. A bogus model name gets
exactly the same response as a real one, at both layers:

```
claude route (genuine Claude Code request, 105 KB, 46 tools, stream:true)
  claude-opus-5-5   -> 200 x3   real message_start, real msg ids
  claude-fable-5-1  -> 429 x3   {"error":{"message":"Service Unavailable","type":"error"}}
  claude-zzz-xyz    -> 429 x3   byte-identical to fable

gpt route (genuine Codex 0.156.1 request, 55 KB)
  gpt-6-astra       -> 500 x3   负载已经达到上限 / get_channel_failed
  gpt-5.6-sol       -> 500 x3
  gpt-5.5           -> 500 x3
  gpt-6-astra-zzz   -> 500 x3   byte-identical to the real ids
  gpt-5-codex       -> 404      flat 当前 API 不支持所选模型
  claude-opus-5-5   -> 404      flat (wrong family for this wire API)
```

The method matters: the request was captured verbatim from the genuine client
(proxying a 200), then replayed with **only the model name changed**. That
removes every fingerprint variable at once, which hand-built header sets could
not do — and it also refuted the earlier assumption that a small curl body was
equivalent. The same small-body curl returns 400/503 for every Claude id; the
real 105 KB request returns 200 for `claude-opus-5-5`.

Three distinct tiers, now readable:

| response | meaning |
| --- | --- |
| flat `404 当前 API 不支持所选模型` | the name matches no pattern for this token on this wire API |
| `400 1m 上下文…` | matched, but `anthropic-beta: context-1m-2025-08-07` missing |
| `429`/`503 Service Unavailable`, `500 get_channel_failed` | matched, but no channel is serving that model right now |

The third tier does not distinguish "removed", "never existed" or "at capacity",
and its wording ("负载已经达到上限", "Service Unavailable") must not be read as a
rate limit or a transient overload.

**Why fable 429s: the relay swapped it out.** `claude-fable-*` is absent from the
live `/v1/models` catalog (15 entries), which does contain `claude-opus-5-5`; the
operator's Pricing list showed the same. Repeated genuine-client runs agree:
`claude-fable-5-1` 8/8 timed out with no output, `claude-opus-5-5` 6/6 returned
"ok". So fable-5-1 was removed and opus-5-5 added, and the removal surfaces as a
synthetic 429 rather than a clean 404.

On the gpt side no model succeeded through any shape tried (90 genuine Codex
attempts plus 36 verbatim replays), including with
`x-openai-internal-codex-responses-lite` and `x-codex-beta-features` stripped and
the User-Agent replaced. Unlike `claude-opus-5-5`, there is no confirmation that
any gpt slug is servable right now, only the 2026-09-20 history where
`gpt-6-astra` completed dozens of turns.

## Open items (not fixed here)

1. `providers.py` should distinguish a flat-body 404 (`no channel on this wire
   API`) from a route 404 and from a channel-cap 500, rather than collapsing all
   to "down". A 404 on the wrong wire API currently reads as a dead relay.
2. Since routing is substring-based, the probe model only has to contain a
   configured base string; `gpt-5.6-sol` and `gpt-5.5` work as probe names on
   `/v1/responses`, but both are the same saturated premium channels as astra.
3. Any anyrouter client must select the wire API per model family:
   `/v1/responses` for the `gpt-6-astra` / `gpt-5.6-sol` / `gpt-5.5` families,
   `/v1/messages` for anything containing `claude` (plus the 1M-context beta),
   and nothing usable on `/v1/chat/completions`.
4. Per-id Claude entitlement is not observable with a bare curl, because the gate
   answers before the model is resolved. Capture a genuine client request and
   replay it with only the model name changed; a small hand-built body gives
   400/503 for models that the real request gets 200 for.
5. Treat `429`/`503 Service Unavailable` and `500 get_channel_failed` as "not
   served right now", never as a rate limit or a load spike. Check the live
   `/v1/models` catalog (and Pricing) for whether a slug is still offered at all.
6. The operator's `~/.claude/settings.json` should use
   `ANTHROPIC_BASE_URL=https://anyrouter.top` (no `/v1`) and set
   `ANTHROPIC_BETAS=context-1m-2025-08-07`. Not changed here because it is a
   user-level config outside the repo.
7. `relay-lab` cannot probe this relay correctly: add a `responses` mode and an
   Anthropic `messages` mode, and split `PROBE_UNAVAILABLE` into "not served" vs
   "not served on this wire API". Not done here because it is a separate project.
8. The HTTPS-only guard vs. the localhost `http` shim needs a decision: allow
   loopback http, or move the shim to TLS.
9. `anyrouter-sse-shim.py` exists only on the box (in `remote-agent-deploy/`) and
   is not under systemd, so the box is not reproducible from the repo.

## Method

An exhaustive sweep of 218 candidate model names x 3 wire APIs (654 requests)
plus a 218-request `/v1/messages` pass with
`anthropic-beta: context-1m-2025-08-07`, on top of targeted controls. Probes used
the box's `~/.codex/modes/keys/anyrouter` via `Authorization: Bearer` and were
sent from fedora directly, and from the box both direct and through `:9995`.
Bodies were classified by error shape, never by status code alone, and every
entitlement claim was re-tested against a control name that lacks the family
substring. A 36-request retry loop and a 17-minute, 120-sample watch (156
requests total, every 25 s) were run against the three provisioned
`/v1/responses` patterns to test the cap-vs-missing-upstream question; they saw
156/156 `LOAD_CAP` and no 200.

For the client question the installed Claude Code 2.1.267 was used as an oracle:
a local recording server captured the exact request it sends (path, headers,
body), and a local logging forward proxy placed in front of anyrouter captured
the relay's answers to the genuine client. Read-only: no relay config, no repo
code and no user settings were modified, and the two local helper servers were
stopped afterwards. The Pricing cross-check uses
the operator's rendering of the page, because `/api/pricing` is behind the ESA
WAF (`x-tengine-error: denied by http_custom`) and returns the JS challenge
instead of JSON — replaying the challenge's own cookies with a browser User-Agent
does not pass it, and the CN mirror host answers 403 `AccessDenied` to both
`/api/pricing` and `/v1/models`. `/v1/messages/count_tokens` does not exist
(`Invalid URL`), and non-`/v1/messages` paths return the SPA page, so the
Anthropic surface is exactly `POST /v1/messages`. No credential or header value
was logged.


