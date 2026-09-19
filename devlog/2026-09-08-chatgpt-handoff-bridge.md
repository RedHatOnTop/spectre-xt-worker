# 2026-09-08 — ChatGPT handoff bridge: CodexPro chosen, DevSpace fork rejected

## Requirement

User wanted ChatGPT web to work the Spectre like Codex, specifically
keeping an Antigravity CLI worker busy with plans it writes itself.
Which bridge to deploy — and whether to port CodexPro features into
DevSpace instead — was left open.

## What was done

Cloned both candidates into `references/` (gitignored) and read the
implementations, not the marketing.

- DevSpace: strongest auth story (OAuth + Owner-password approval page)
  and a faithful Codex tool surface, but its subagent daemon rejects
  custom providers by design ("Unsupported or custom providers are
  rejected"; "Current non-goals: Custom or arbitrary CLI-backed
  agents"). Driving agy would mean a fork with per-provider SDK
  adapters against fast upstream churn — or handing ChatGPT an
  unrestricted `exec_command` behind a public tunnel. Both rejected.
- CodexPro: handoff mode never advertises generic write tools
  (ChatGPT writes `.ai-bridge/` plans only), bash defaults to a safe
  allowlist, public tunnels fail closed without a >=24-byte token, and
  execute/watch/loop-handoff accept `--agent custom --command`
  templates ({{plan_file}}, {{root}}) with machine-readable
  handoff-run-state.json that ChatGPT polls via `wait_for_handoff`.
  That is the requested loop, already built.

Decision: deploy CodexPro as-is in handoff mode. The `.ai-bridge` file
protocol becomes the bridge-agnostic contract, so the executor can be
swapped (agy today, qodercli tomorrow) by changing one `--command`
flag. DevSpace fork: rejected — revisit if upstream ships custom/ACP
provider support, agy speaks ACP, or a direct-edit ChatGPT surface is
wanted on the Zenbook (never on the box).

Antigravity CLI headless verified against current Google docs: `agy -p`,
`--output-format json/stream-json`, `--print-timeout`, cached
credentials after one interactive login, and deny-by-default
permissions in headless mode. That soft-deny is the worker-side guard
that replaces irreversible-guard coverage for this path.

Delivered in the repo:

- `scripts/agy-handoff.sh` — executor wrapper: response text to stdout
  (CodexPro collects it into agent-status.md), JSONL envelope to
  `/work/logs/agy-handoff.log`, non-zero exit on any non-SUCCESS status
  so handoff-run-state.json flips to failed. agy print timeout (9m)
  stays under CodexPro's 600000 ms execute timeout on purpose.
- `systemd/codexpro-handoff.service` — user unit: handoff mode,
  `--no-bash` first, cloudflare-named tunnel, both token files, the
  §7.8 `UnsetEnvironment` gotcha carried over; Phase 2 `watch-handoff`
  unit included as a comment block, disabled until Phase 1 is trusted.
- `config/codexpro.env.example`, `config/agy-settings.example.json`
  (agy `permissions.allow` scoped to git read/commit, package test
  scripts, `write_file(src/)`).
- `scripts/doctor.sh` — "chatgpt handoff bridge" gate section
  (unit active, codexpro on PATH, 401 fail-closed probe on :8787,
  cloudflared alive, token file present). No-op when not installed.
- `scripts/healthcheck.py` + tests — `REQUIRE_BRIDGE` (default off)
  with `bridge_down` / `bridge_auth_http=<code>` /
  `bridge_tunnel_missing` bits; `config/health.env.example` updated.
- `RUNBOOK.md` §7.9 + verification-gate lines.

## Honest limits

- Nothing here has run on the Spectre yet; per policy this stays a
  plan until the §7.9 gates pass on the box.
- Pilot repo, tunnel hostname, and tunnel creation are user inputs
  still to be chosen.
- ChatGPT Developer mode/Plugins is a beta surface; the connection
  recipe may drift.
- agy quota and terms are the user's account responsibility; long
  tasks are handled by splitting plans, not by raising
  `--print-timeout`.
- codexpro is young (0.30.x); pin the installed version and re-read
  its SECURITY.md on upgrades.
- references/devspace and references/codexpro are shallow clones kept
  out of git via `.gitignore`.
