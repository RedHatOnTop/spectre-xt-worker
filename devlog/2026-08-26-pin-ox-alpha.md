# 2026-08-26 — ox-alpha pinned as the only model on the Spectre

## Requirement

User: ZCode on the box must use ox-alpha, exclusively.

## What was done

Found the exact provider schema by inspecting fedora's working
`~/.zcode/v2/config.json`: custom providers are `kind:
"openai-compatible"`, uuid-keyed under `.provider`, with
`options.{apiKey, baseURL}` and a per-model map. Fedora already had a
"Hardened" entry (baseURL http://localhost:18088/v1, dummy key "a",
models glm-5.3 + ox-alpha) — proof the shape works end-to-end.

Applied to the Spectre's config.json via jq:

- new Hardened provider: baseURL http://127.0.0.1:18088/v1,
  enabled true, models = **ox-alpha only** (name/limit/modalities/
  zcode.modified copied from fedora's proven entry)
- every builtin provider flipped enabled=false — no other SKU exists
  in the picker, so the agent cannot hop models

Model id note: the proxy's /v1/models lists `stealth/ox-alpha`, but
fedora's working config registers plain `ox-alpha` and that is what has
been in use; kept `ox-alpha`.

Race handling (lesson from the doctor work): killed ZCode first, edited
config.json, restarted via the autostart wrapper. Verified the config
survives while ZCode runs (25s recheck): Hardened/enabled/[ox-alpha]
still the only active provider; proxy healthy (activeKeys=21).

Backup of pre-change config saved at
~/.zcode/v2/config.json.bak-model-pin on the box.

## Honest limits

- The UI-level selected-model key lives somewhere we did not identify;
  with exactly one enabled provider/model it should not matter, but if
  ZCode surfaces an empty-picker state after an update, check
  setting.json's modelProviderFamily* keys next.
- If the proxy later stops serving ox-alpha, ZCode has zero fallback by
  design — that is the requirement.
