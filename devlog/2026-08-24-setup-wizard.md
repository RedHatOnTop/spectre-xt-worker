# 2026-08-24 — setup-wizard: fresh Debian 13 to full operation in one command

## Goal

User asked to finish the tooling so that a genuinely fresh Debian 13 +
XFCE box can go from bare install to full worker operation (through
warp) without consulting scattered docs.

## Audit findings

Walked bootstrap end-to-end assuming a blank box:

- `doctor.sh` was never installed by bootstrap — the final echo told the
  user to run `spectre-doctor`, which did not exist on PATH. Fixed:
  bootstrap installs it.
- `spectre-status` was human-text only; the planned mobile management
  app needs machine-readable facts. Added `--json` mode (same probes,
  one jq object; smoke-tested on fedora — proxy keys, temp, heartbeat
  age all populated).
- Everything else (warp install path, pin, health timer) was already
  wired from the earlier passes.

## The wizard

`scripts/setup-wizard.sh` — interactive stage runner, 10 stages:
bootstrap, tailscale, keys, harden, cockpit, proxy, health-env,
zcode-pin, stealth-test, verify. Design choices:

- **Re-run safe**: `/var/lib/spectre-wizard/stages.done` records finished
  stages; completed ones print "already done, skipping". A failed stage
  never means starting over.
- **Refuses to reorder**: bootstrap and tailscale are hard prerequisites;
  harden re-checks keys-on-disk itself (lockout guard), and the wizard
  stops if keys were not pulled.
- **Secrets stay manual**: proxy rsync excludes env.json/.env; the
  wizard pauses and asks the user to hand-copy env.json before enabling
  the unit. Same rule as RUNBOOK: keys never travel by script.
- **Interactive gates are explicit**: tailscale prints the login URL and
  waits; stealth stage runs `spectre-stealth closed`, then asks the user
  to confirm the fedora-side ping+health checks passed with the lid
  closed before recording the stage.
- Proxy stage pulls from fedora over tailscale with the same excludes as
  warp (node_modules, target, logs), chowns to the user, enables the
  user unit, and health-checks :18088 before marking done.
- bootstrap's final message now leads with the wizard; the manual
  command list stays as the alternative.

## Verification

- `bash -n` + `shellcheck -S style` clean on wizard, status, bootstrap.
- `spectre-status --json` smoke test on fedora: valid JSON, proxy ok
  activeKeys=20, tailscale Running, ac on.
- Full repo gate: `bash verify.sh` → all gates passed (17 scripts lint,
  py_compile, 16 unit tests OK).
- Wizard itself is Debian-targeted (systemd/tailscale/spectre-*)
  and cannot be executed on fedora — its correctness gate is the
  on-Spectre run, still pending like the rest of the install.
