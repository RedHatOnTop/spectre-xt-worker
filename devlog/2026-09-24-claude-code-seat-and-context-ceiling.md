# Claude Code as seat 1, and anyrouter's real context ceiling — 2026-09-24

Seat 1 of `SEAT_LADDER` is Claude Opus 5.5 on anyrouter through Claude Code
(`2026-09-23-anyrouter-design-deltas.md`, D8). This records the client
configuration that makes that seat work and the measurement of how much context
it can actually take.

## Applied configuration

`~/.claude/settings.json`, `env` block. A pre-change copy is kept at
`~/.claude/settings.json.bak-20260924-013244` (mode 600, contains the previous
relay key, so it is a rollback point and not a file to copy around).

| key | value | why |
| --- | --- | --- |
| `ANTHROPIC_BASE_URL` | `https://anyrouter.top` | Claude Code appends `/v1/messages` itself; a base ending in `/v1` produces `/v1/v1/messages` and a 404 that the CLI reports as "model may not exist" |
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` | anyrouter key | seat 1 credential |
| `ANTHROPIC_BETAS` | `context-1m-2025-08-07` | without it anyrouter answers 400 `1m 上下文已经全量可用，请启用 1m 上下文后重试` |
| `model` | `claude-opus-5-5` | the only Claude id anyrouter serves today (`claude-fable-*` was removed) |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `180000` | derived from the measurement below |

The file previously pointed at `https://api.justwoker.icu` with model
`claude-opus-5`, which serves only `claude-opus-4-8`. That is why the change is
reversible from the backup rather than edited in place.

`ANTHROPIC_CUSTOM_HEADERS` is deliberately **not** used to inject the beta: the
CLI already sets `anthropic-beta` itself and the custom header does not override
it (measured).

Verification, with no environment override so the real settings file drives it:

```
$ env -u PYTHONHOME -u PYTHONPATH claude -p "reply with exactly: ok"
[claude-code:unrecognized_model] {"model":"claude-opus-5-5","query_source":"sdk"}
ok
exit=0 elapsed=83s
```

The `unrecognized_model` line is cosmetic: the bundled CLI does not carry this
id in its own registry, but it dispatches the request anyway. A one-word answer
takes ~78–83 s at `effort: xhigh`.

## Measuring the ceiling

Measuring this relay needs care; two naive methods give wrong answers.

- **Do not append a message.** The authentic request carries two messages, the
  second with `role: system`. Appending a third returns the generic
  `429 {"error":{"message":"Service Unavailable"}}` — at 200 filler tokens and at
  8,000 real-corpus tokens alike. The body content is irrelevant; the extra
  message is the trigger.
- **Do not fill with generated text.** Repeated numbered lines were rejected the
  same way, so the corpus must be real text. A 27 MB corpus of this workspace's
  own sources and docs was used.
- **Do not overwrite the last block.** Replacing it destroys the captured
  environment text and changes the baseline. Inserting one extra text block into
  the existing first user message is the mutation that works.
- **Read only until `message_start`.** The usage object arrives there, so the
  probe measures input acceptance instead of generation time.
- **Read all three token fields.** Claude Code sends `cache_control`, so
  `input_tokens` is a stub (`2`) and the prompt size lives in
  `cache_creation_input_tokens` + `cache_read_input_tokens`.

Procedure: capture the genuine request once (a logging proxy in front of
anyrouter, replaying the client's own body and headers verbatim), then insert a
corpus block of increasing size.

## Result — the wall is a timeout, not a context window

| corpus | body | status | time to `message_start` | prompt tokens |
| --- | --- | --- | --- | --- |
| 0 | 115 KB | 200 | 42.5 s | **40,726** |
| 500,000 chars | 629 KB | 200 | **249.3 s** | **231,408** |
| 1,000,000 chars | 1.15 MB | **524** | 301.3 s | — |
| 2,000,000 chars | 2.26 MB | **524** | 301.0 s | — |

`524` is the relay edge giving up at ~301 s; the same 301 s ceiling appeared for
an 8 MB body. So the relay does not enforce a token limit it will tell you
about — it accepts the body, starts work, and dies at the edge timeout.

Fitting the two successes (40.7k→42.5 s, 231k→249 s) gives ~1.08 s per 1k prompt
tokens and puts 300 s at roughly **278k tokens**. The practical ceiling is
therefore ~230–280k prompt tokens, and the 1M window the beta advertises is not
reachable through this relay at any prompt size that fits the timeout.

Two caveats worth keeping: the request body itself is ~115 KB / 40.7 k tokens
before any user content (system prompt, 48–50 tool definitions, reminders), and
the 231k run reported 34,239 cache-read tokens, so its 249 s already benefits
from a partially warm prompt cache.

## The relay log's single-digit prompt is `cache_control`, not a dropped prompt

The relay's request log shows many requests with a prompt in the single digits
and real completion tokens. That is the same cache split as above, not the relay
discarding the prompt: `input_tokens` holds only the non-cached remainder while
the bulk sits in `cache_creation_input_tokens` / `cache_read_input_tokens`.

Verified by burying an unguessable marker in the inserted block and asking for it
back:

| inserted | status | time | usage | answer |
| --- | --- | --- | --- | --- |
| 2,000 chars, marker at the end | 200 | 74.3 s | input 2, cache_creation 7,276, cache_read 34,239 | `VAULT-ALPHA-7731` |
| 200,000 chars, marker at the end | 200 | 165.2 s | input 2, cache_creation 82,080, cache_read 34,239 | `VAULT-BETA-9042` |

Both markers came back verbatim, so the prompt reaches the model at both sizes
and the log column is a display artifact. Note the constant 34,239 cache-read
tokens: the ~40.7 k-token floor (system prompt, tool definitions, reminders) is a
warm cached prefix, which is exactly why the logged non-cached input looks
degenerate. A real usage figure is the sum of all three fields.

## Consequence for the client

`CLAUDE_CODE_AUTO_COMPACT_WINDOW` was 240,000 — essentially the failure edge
(231k already took 249 s of a 300 s budget). It is now 180,000, which the same
fit puts at ~191 s, leaving roughly a 40% latency margin. This is the value to
revisit if the relay's edge timeout or throughput changes.

## Open items

1. `CLAUDE_CODE_MAX_RETRIES = 300` with `CLAUDE_CODE_RETRY_WATCHDOG = 1` is
   inherited from the previous relay. Against a 429 that means "no serving
   channel" rather than a rate limit (D3), that combination may retry a dead seat
   far longer than is useful. Not changed here; the retry policy follows the D3
   work.
2. The seat is configured by hand in a user-level file. Once P2 lands the harness
   dispatch, the ladder should own these values so the box cannot drift from the
   recorded policy.
