#!/usr/bin/env node
// read-guard — refuse to pull a large file into the context window whole, and refuse to
// pull secrets in at all.
//
// Ported from the Claude Code original for Codex CLI. Claude uses the `Read` tool with
// `file_path`; Codex uses the `read` tool with `path`. Codex also exposes an `apply_patch`
// / `write` / `edit` path argument, but those are writes and the irreversible-guard owns them.
//
// Two jobs:
//   1. Size guard. Every token a read adds is re-read on every later turn, so a big read is
//      not a one-off cost — it is a tax on the rest of the session. The threshold blocks
//      almost nothing and recovers the long tail.
//   2. Secret guard. Mirrors the Claude deny list (Read(**/.env), .credentials.json, id_rsa,
//      id_ed25519). Codex's path-based permissions are awkward to express this as, and a hook
//      fires before the bytes enter context — which is the point.
//
// Codex PreToolUse contract: exit 2 blocks the call and hands stderr back to the model, exit
// 0 allows. Any failure inside this guard must allow; a broken guard must never wedge a
// session.

import { readFileSync, statSync } from 'node:fs';

const MAX_BYTES = Number(process.env.READ_GUARD_MAX_BYTES ?? 20_000);
const MAX_LINES = Number(process.env.READ_GUARD_MAX_LINES ?? 800);
// Below this a file cannot hold MAX_LINES of anything worth reading, so skip the line count.
const LINE_CHECK_FLOOR = 8_000;

// Read renders these rather than dumping text; image tools are bounded differently.
const BINARY = /\.(png|jpe?g|gif|webp|svg|pdf|ipynb)$/i;

// Mirrors the Claude permissions.deny list for secret material.
const SECRET =
  /(^|[\\/])\.env(\.|$)|(^|[\\/])\.credentials\.json$|(^|[\\/])(id_rsa|id_ed25519|id_ecdsa|id_dsa)(\.pub)?$|(^|[\\/])\.npmrc$|(^|[\\/])\.pypirc$/i;

const allow = () => process.exit(0);

let payload;
try {
  payload = JSON.parse(readFileSync(0, 'utf8'));
} catch {
  allow();
}

const input = payload?.tool_input ?? {};
const path = input.path ?? input.file_path;

// A bounded read is exactly the behaviour this guard exists to produce.
if (!path || input.limit || input.offset || input.pages) allow();

// Secret check first — size is irrelevant when the file must never enter context.
if (SECRET.test(path)) {
  process.stderr.write(
    `read-guard: refusing to read ${path} — it looks like a secret or credential file.\n\n` +
      `It is on the deny list (mirrors the Claude Code permissions.deny entries). If the user\n` +
      `genuinely needs this read, they run it themselves.\n`
  );
  process.exit(2);
}

if (BINARY.test(path)) allow();

let size;
let lines = 0;
try {
  size = statSync(path).size;
} catch {
  allow(); // missing file or permission error — let the tool surface the real message
}

if (size <= MAX_BYTES) {
  if (size < LINE_CHECK_FLOOR) allow();
  try {
    lines = readFileSync(path, 'utf8').split('\n').length;
  } catch {
    allow();
  }
  if (lines <= MAX_LINES) allow();
}

const approxTokens = Math.round(size / 3.6);
const why =
  lines > MAX_LINES
    ? `${lines.toLocaleString()} lines`
    : `${size.toLocaleString()} bytes, roughly ${approxTokens.toLocaleString()} tokens`;

process.stderr.write(
  `read-guard: refusing to read ${path} whole — ${why}.\n\n` +
    `It would stay in the context window for the rest of the session and be re-read on every ` +
    `later turn. Pick one:\n\n` +
    `  - Read with offset and limit, when you know roughly where to look.\n` +
    `  - Grep the symbol, with -n and -C for surrounding lines.\n\n` +
    `If the whole file truly has to be in context, re-run with READ_GUARD_MAX_BYTES raised and ` +
    `tell the user why.\n`
);
process.exit(2);
