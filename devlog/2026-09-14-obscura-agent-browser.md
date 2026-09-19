# 2026-09-14 — Obscura: one headless browser for every agent

## Why

The ask: install obscura on the Spectre and make it usable by every agent
there. The box is a dedicated agent machine — guards are close to
irrelevant on it — so the bar for adding a capability is "does it let the
agents do more", not "does it need a new guard".

Obscura fits the box in a way a browser stack would not: Rust + V8, no
Chromium, ~220 MB on disk, ~3 MB idle RSS. The box is a 2012 Ivy Bridge
with 12 GB shared between Claude, Qoder, Codex, Orca and a Slack bridge;
a real Chrome would have been a resource decision.

## What was installed

- Release `v0.2.2`, asset `obscura-x86_64-linux-stealth.tar.gz`, pinned by
  sha256 in `scripts/install-obscura.sh`. The stealth build carries the
  rendering engine plus TLS impersonation and tracker blocking.
- `/usr/local/bin/obscura` + `obscura-worker` (the worker must sit next to
  the binary for `obscura scrape`).
- `obscura-cdp.service` — user unit, CDP on `127.0.0.1:9222`.
- `obscura mcp` registered as the `obscura` MCP server in Claude Code
  (user scope), Qoder CLI (user scope) and Codex (`config.toml`).
- A short browser note in the box sessions' instruction files
  (`~/.claude/CLAUDE.md` appended in place; Codex's global `AGENTS.md` now
  carries it in `config/codex-global-AGENTS.md`).

Three doors, deliberately: the CLI (`obscura fetch`) for one-off reads,
per-agent stdio MCP instances for tool-driven browsing, and the CDP
endpoint for Playwright/Puppeteer scripts. Per-agent stdio means nothing
is shared and nothing races on a port; the CDP service is the only long
lived process.

## Flags, and why these

- `--stealth` everywhere: the box browses real sites on the agents'
  behalf, and fingerprint consistency plus tracker blocking costs nothing
  here.
- `--allow-private-network` is ON. Obscura blocks loopback/RFC1918 by
  default (post-SSRF-fix), which would break the most common box use —
  an agent checking a local dev server it just started.
- `--allow-file-access` is OFF. It would let a CDP client navigate to
  `file://` and read local files; no agent needs that and turning it on
  would quietly defeat the read guard.
- The CDP listener stays on loopback; only the fetch side is widened.

## Decisions worth recording

- **No ZCode registration.** ZCode keeps its MCP config inside the desktop
  app, and ZCode is on the way out on this box anyway. ZCode sessions
  still get the CLI on PATH.
- **Guards unchanged.** A browser is not an irreversible action, and the
  box posture (RUNBOOK 7.11) already records that Codex runs unguarded
  here. Nothing in this change needed a guard entry.
- **The sha256 pin is a review pin, not a provenance claim.** Upstream
  publishes no checksums and the build is a from-source Rust binary, so
  the pin means "the artifact reviewed on 2026-09-14" and nothing more.
  Moving versions means updating `OBSCURA_VERSION` and `OBSCURA_SHA256`
  together, which keeps a silent swap from happening by accident.
- **It is not Chromium.** The engine is independent (v0.2.2) and the
  project says long-tail CSS and some Web APIs differ. Agents should treat
  a rendering difference as expected, not as a bug in the page.

## Repo artifacts

- `scripts/install-obscura.sh` — pinned download + sha256 check, both
  binaries, unit install/enable, MCP registration for three agents, the
  Claude-session note (marker-guarded), smoke test. Called from
  `bootstrap.sh` next to `install-codex.sh`; a CDN failure warns and
  continues.
- `systemd/obscura-cdp.service` — the user unit.
- `tests/obscura-cdp-smoke.mjs` — dependency-free CDP end-to-end (Node ≥22
  global WebSocket): connect, new tab, navigate, read the title back.
- `config/codex-global-AGENTS.md` — gained the browser section, so the
  Codex installer keeps it in place across re-runs.
- `scripts/doctor.sh` — six gates: CLI present, unit active, CDP answers,
  MCP entry present in claude/qoder/codex.
- RUNBOOK 7.12 — install, the three access paths, notes, verify, rollback.

## Verification (on the Spectre, 2026-09-14)

```
obscura --version                                    # obscura 0.2.2
curl -s http://127.0.0.1:9222/json/version | jq -r .Browser   # Chrome/145.0.0.0
obscura fetch https://example.com --eval "document.title"     # Example Domain
MCP tools/list over stdio                            # 37 browser_* tools
claude mcp get obscura / qodercli mcp get obscura    # ✔ Connected / ✓ Connected
codex mcp list | grep obscura                        # enabled
node tests/obscura-cdp-smoke.mjs                     # PASS over the WebSocket
sudo spectre-doctor                                  # 41 PASS, 0 FAIL
```

The CDP check is a real navigation over `ws://127.0.0.1:9222`, not a port
probe: `Target.createTarget` → `attachToTarget` → `Page.navigate` →
`Runtime.evaluate document.title` → `"Example Domain"`.

One doc mismatch found and not repeated here: the project README lists 13
MCP tools; the v0.2.2 binary exposes 37 (`browser_markdown`, tabs, storage
state, form detection among them). The binary is the truth; RUNBOOK 7.12
records the measured count.
