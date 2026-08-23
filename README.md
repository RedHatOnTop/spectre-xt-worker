# Spectre XT agent worker

Turn the idle HP Spectre XT into a lid-closed, AC-powered ZCode worker that
hammers free/rotating endpoints through `hardened-zai-proxy`, steered from
a phone (Z Fold 7) over Tailscale.

**Read [RUNBOOK.md](RUNBOOK.md).** That is the install and ops document.

```
Spectre (Debian 13 + XFCE, auto-login)
  glm-proxy     :18088   Tailscale only
  ZCode GUI     pinned model via Hardened provider
  Cockpit       :9090    Tailscale only
  health timer  ntfy + log
Phone
  Tailscale + Termius (tmux)
  ZCode Bot Channel (Telegram)   durable
  ZCode Remote Control           visual
  Cockpit PWA                    reboot / units / logs
```
