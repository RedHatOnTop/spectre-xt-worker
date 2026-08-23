# remote-agent

24/7 ZCode worker box for the idle HP Spectre XT TouchSmart (13-2000,
2012, i7-3517U, 12 GB, 256 GB + 120 GB SSD). This is not an HP ZBook
and not the 15-inch ENVY Spectre XT. The daily driver is the ASUS
Zenbook Duo (`fedora`).
This directory is its own git repository. Do not mix commits with sibling
projects under `distribution-project/`.

## Role

The Spectre is an **agent runtime**, not a compile farm. One ZCode window,
one pinned model, `hardened-zai-proxy` on `:18088`. Rust debug builds, Docker
Desktop, GNOME, and a second Electron app do not belong here.

## Rules

- Documentation is English. Replies to the user may be Korean.
- Never commit `env.json`, `.env`, API keys, `credentials.json`, or Tailscale
  auth keys. Reference secrets by name only.
- Prefer systemd user units over shell loops and `nohup`.
- A change is done only when the verification command in `RUNBOOK.md` was
  actually run on the Spectre (or explicitly marked unverified).
