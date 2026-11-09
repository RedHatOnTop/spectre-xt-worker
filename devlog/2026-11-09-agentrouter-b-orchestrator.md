# 2026-11-09 Agentrouter B orchestrator

Spectre coordinators were still entering through `codex-agentrouter-portable`,
which starts the loopback proxy on port 18789 and the original Agentrouter key.
The live Fullmoon session `01a196ee-87bc-76c0-a62f-b6fafbc544c9` and the
acceptance coordinator were both on that profile. Their base Codex config also
inherited ChatGPT auth, `service_tier`, desktop features (apps, browser,
computer use, in-app tools), and extra MCP servers.

`spectre-coordinator` now execs `codex --no-daemon --profile orca-agentrouter-b`
with those inherited surfaces overridden off, API login forced, and the
credential store set to ephemeral so the ChatGPT account is not the client
user. The profile itself is direct `https://agentrouter.org/v1` and reads the
`agentrouter-b` key. No proxy is on this path.

The stock Codex multi-agent role paragraph stayed in `debug prompt-input` even
with `features.multi_agent=false`. That text is the CLI's own developer
message, so it was left in place rather than rewritten by a proxy.

`project_doc_max_bytes=0` kept the Hangul project `AGENTS.md` out of the
prompt. The global `~/.codex/AGENTS.md` is exempt from that budget and still
told the model to reply in Korean and described a browser CDP listener on
`127.0.0.1:9222`. The coordinator bind-mounts an English overlay over that
file inside a user mount namespace. Other Codex processes still see the
original file. A prompt-input from the Fullmoon workspace under that mount
had 0 Hangul and no loopback, browser, or ChatGPT account fields.

A native `codex exec --ephemeral` on that profile, reasoning low, returned
the single word `pong` (exit 0, 8367 input tokens, 42 output tokens). The
proxy journal for the same window had only large HTTP 403 forwards and no
small body, so that probe did not go through port 18789. Empty `service_tier`
was omitted from the request with a warning, which is the intended result.

The Fullmoon coordinator was then stopped and resumed in its existing Orca
terminal (`term_b234127e-b0de-4b37-a35e-1a6d20f942b2`) as
`codex --profile orca-agentrouter-b` on session
`01a196ee-87bc-76c0-a62f-b6fafbc544c9`. The process mount namespace binds the
English overlay. The TUI came back at the idle prompt after the interrupt.
The session file contains 261222 Hangul characters, 194 of them in the last
300000 characters. No follow-up turn was sent, so whether that history is
included in the next upstream request is unverified. The acceptance
coordinator had no rollout file and was left on the portable profile. The
macOS research session was not an orchestrator and was left untouched.
