# 2026-11-04 — Sol instruction against stalling

The coordinator transcript `01a196ee-87bc-76c0-a62f-b6fafbc544c9` last spoke at
15:14 KST with a plan to resume release work. Later turns hit the same
`capacity_wait` (`occupied` 7, `limit` 2) again, including one at 20:58 KST,
and the earlier reply had asked for a new operator approval instead of
identifying settled occupants. The installed instructions only covered tone,
evidence, and anti-slop.

`config/codex-instructions.md` now has a "Do not stall" section: finish the
authorized step in the turn, treat `capacity_wait` as data, keep unknown or
live owners, and stop only for a named gate. The same bytes were copied to
`~/.codex/instructions.md` on Fedora and Spectre. The Spectre profile
`orca-agentrouter-portable.config.toml` now sets `model_instructions_file` to
that path. The Fullmoon coordinator process started 2026-11-02 19:50 KST was
left running, so this text is not in that process yet.
