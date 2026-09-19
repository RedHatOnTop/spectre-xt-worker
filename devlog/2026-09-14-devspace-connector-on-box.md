# 2026-09-14 — ChatGPT connector moved onto the box (DevSpace on the Spectre)

## What was asked

The user noticed the ChatGPT-facing stack ran on the Zenbook, not the 24/7
box, and asked to move it. A detour: "not devspace, move the devcodex that's
on this machine" — the box already ran the vendored devcodex (§7.13) with all
gates passing, so that part was verified rather than re-done. Then: "move the
connector too — or just build a fresh one and tell me how to set it up."

## What was built

- `@waishnav/devspace` 1.0.8 installed system-wide on the box
  (`/usr/bin/devspace`); node v22.23.2 satisfies the package's
  `>=22.19 <27`.
- Fresh `~/.devspace/config.json` + `auth.json` (new 32-byte owner token,
  0600; dir 0700). `publicBaseUrl` = `https://spectre.tail1fa7c9.ts.net`.
- Roots (user answer to a direct question): `~/Projects` + `~/.agents`.
  Narrow because the box holds fleet credentials; upstream also recommends
  narrow roots. `~/.agents` exists in the list solely so the advertised
  devcodex skill's SKILL.md is readable (the tilde-path read quirk from the
  Zenbook work applies unchanged).
- Box skill `~/.agents/skills/devcodex/SKILL.md` (paths adapted to
  `/usr/local/bin/devcodex`); repo copy at `config/devcodex-skill.md`.
- `systemd/devspace.service` user unit (`Restart=on-failure`); bootstrap
  installs the unit and enables it only when the binary exists.
- `sudo tailscale funnel --bg --https=443 http://127.0.0.1:7676`.
- doctor.sh gains a "devspace connector" section (unit active / CLI on PATH /
  401 fail-closed probe / funnel proxy present).

## Diagnosis worth remembering

The first systemd start looked hung: no listener at +17 s, empty journal.
It was not hung. `strace` caught ~10k `statx`/s hammering
`pi-coding-agent/node_modules/typebox/**/package.json` — the CLI statically
imports `pi-coding-agent` (cli.js pulls `getShellConfig`), and that module
graph takes ~18 s to load on this 2012 CPU (measured: `pi-tools.js` 8.1 s,
`server.js` 13.5 s). DevSpace logs nothing until init finishes. Rule for
next time: give a restart ~20 s before calling it broken; `timeout 10
devspace serve` will always look dead.

## Verification (box, 2026-09-14 ~23:55 KST)

- `devspace doctor`: roots `/home/person/Projects, /home/person/.agents`,
  allowed hosts include `spectre.tail1fa7c9.ts.net`, SQLite native ok.
- Local `/mcp` → 401 + `resource_metadata`; public via funnel → 401 (HTTP/2);
  both well-known OAuth endpoints answer.
- `tests/devspace-devcodex-e2e.mjs` run on the box: 13/13 PASS (DCR +
  owner-token authorize + PKCE + MCP handshake + skill advertised & readable
  + devcodex bootstrap/changes/verify/note/complete through the bash tool).
- `spectre-doctor`: devcodex 2 PASS; devspace section deployed with this
  change.

## Limits / notes

- The Zenbook connector (`fedora.tail1fa7c9.ts.net`) still runs (nohup, no
  unit) until the user retires it in the ChatGPT UI. Two connectors can
  coexist; the box one is the 24/7 one.
- No installer script, on purpose: the owner token is a live credential, and
  a script that regenerates it would silently break an approved connector.
  RUNBOOK §7.14 is the record.
- Roots bound file tools only; `bash` is full user access by upstream design
  — the OAuth owner approval is the real gate.
- Startup ~18 s means a freshly restarted unit is unreachable for that long;
  ChatGPT retries are fine.
