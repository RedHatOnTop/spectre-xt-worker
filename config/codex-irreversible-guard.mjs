#!/usr/bin/env node
// irreversible-guard — the Codex-shaped port of config/irreversible-guard.mjs.
//
// Codex runs with approval_policy = "never" and sandbox_mode = "danger-full-access",
// mirroring the Claude setup where no confirmation prompt stands between an instruction
// and an action. The only barrier left is this hook. `permissions`/sandbox policy is real
// but it is path-scoped, so it misses `sh -c "rm -rf x"`, misses indirection, and a sandbox
// in danger-full-access mode enforces nothing. Measured against a live session, not assumed.
//
// Codex tool names differ from Claude's, so the two guards are separate files: a Claude-shaped
// guard installed under `~/.codex` matches no Codex tool name and silently allows everything.
//   Claude Bash/PowerShell -> Codex exec_command / shell   (input: argv[] or command)
//   Claude Read            -> Codex read                    (input: path)
//   Claude Write/Edit      -> Codex write / edit / apply_patch (input: path / file)
//
// Two classes are refused:
//   1. actions whose damage cannot be undone: recursive force deletes, disk writes,
//      force-pushes, history rewrites, publishes.
//   2. actions that would disable this guard: writing Codex config, the hooks directory,
//      or re-entering Codex with its guard switched off.
//
// The override file opens a ten-minute window for class 1 only. Class 2 stays blocked even
// then, and configuration is never writable by the agent: a window exists to authorise one
// dangerous command, not to hand over the keys. An injection that waits for a window would
// otherwise disable the guard permanently. Config changes are a human action.
//
// HONEST LIMIT: a shell is a general-purpose machine. Anything that inspects command text is
// defeated by indirection — write a script, then run the script. This stops accidents and the
// direct forms of an injected instruction. It is not a sandbox. The real backstop for
// irreversibility is still git, and not pointing an agent at irreplaceable data.
//
// Codex PreToolUse contract: exit 2 blocks the call and returns stderr to the model. Exit 0
// allows. A crash must allow — a broken guard must never wedge every session.

import { readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

const HOME = process.env.USERPROFILE ?? process.env.HOME ?? '';
const CODEX_DIR = process.env.CODEX_HOME ?? join(HOME, '.codex');
const OVERRIDE_FILE = join(CODEX_DIR, '.allow-irreversible');
const OVERRIDE_TTL_MIN = 10;

const allow = () => process.exit(0);

function overrideActive() {
  try {
    return (Date.now() - statSync(OVERRIDE_FILE).mtimeMs) / 60_000 < OVERRIDE_TTL_MIN;
  } catch {
    return false;
  }
}

function block(reason, detail, overridable = true) {
  const escape = overridable
    ? `If the user genuinely wants it, they run it themselves, or they authorise a window by hand:\n\n` +
      `    touch "${OVERRIDE_FILE}"\n\n` +
      `That file expires after ${OVERRIDE_TTL_MIN} minutes. Do not create it yourself — writes there\n` +
      `are blocked by this same guard. Ask the user.\n`
    : `No override applies to this. Its effect would outlive the ten-minute window, so it is a\n` +
      `human action: the user edits the file, or runs the command, themselves.\n`;

  process.stderr.write(
    `irreversible-guard: refusing this call.\n\n  ${reason}\n  ${detail}\n\n` +
      `Approvals are off, so this is the only thing standing between a mistaken or injected\n` +
      `instruction and an action that cannot be undone.\n\n${escape}`
  );
  process.exit(2);
}

// Quoted text is only a command when something hands it to a shell. `sh -c "rm -rf x"` runs;
// `echo "rm - rf x"` and `git commit -m "drop the rm -rf"` do not. Stripping quotes
// unconditionally makes the guard fire on the mention, and blocking mentions blocks ordinary
// work — this rule caught its own author three times while writing the tests.
const SHELL_CARRIER =
  /\b(sh|bash|zsh|dash|ksh|pwsh|powershell|cmd|node|python\d?|perl|ruby)\b\s+-(c|e|command)\b|\b(eval|iex|invoke-expression)\b|\|\s*(sh|bash|zsh|pwsh|powershell|iex|invoke-expression)\b/;

const flatten = (s) => s.replace(/\\\s*\n/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();

/** Reveals quoted text. Correct when a shell will interpret it. */
const revealQuotes = (cmd) => flatten(cmd.replace(/[`'"]/g, ' '));

/** Replaces quoted spans with an opaque token. Correct when they are only arguments. */
const hideQuotes = (cmd) =>
  flatten(
    cmd
      .replace(/"(?:[^"\\]|\\.)*"/g, ' __str__ ')
      .replace(/'(?:[^'\\]|\\.)*'/g, ' __str__ ')
      .replace(/`(?:[^`\\]|\\.)*`/g, ' __str__ ')
  );

/** `cat ~/.codex/hooks/x; rm /tmp/a` is two statements, and neither writes the config. */
const statements = (cmd) => cmd.split(/;|&&|\|\||\||\n/).map((s) => s.trim()).filter(Boolean);

/** Run once quotes are opaque, so a `#` inside a string cannot swallow the rest of the line. */
const stripComments = (cmd) => cmd.replace(/(^|\s)#.*$/gm, ' ').trim();

// Deleting a build directory is not irreversible; it is one command from regenerated.
const REGENERABLE =
  /(node_modules|target|dist|build|\.next|\.turbo|__pycache__|\.pytest_cache|coverage|\.venv)\b/;

// Full profile (default): the desktop daily-driver. Anything here is
// treated as needing a human decision or an override window.
const DESTRUCTIVE = [
  [/\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*f|\brm\s+(-[a-z]*\s+)*-[a-z]*f[a-z]*r/, 'recursive force delete (rm)'],
  [/\brm\s+-r\b.*\s-f\b|\brm\s+-f\b.*\s-r\b/, 'recursive force delete (rm, split flags)'],
  [/\bremove-item\b(?=.*\brecurse\b)(?=.*\bforce\b)/, 'recursive force delete (Remove-Item)'],
  [/\bri\s+(?=.*-recurse)(?=.*-force)/, 'recursive force delete (ri alias)'],
  [/\brmdir\s+\/s|\bdel\s+.*\/s|\brd\s+\/s/, 'recursive delete (cmd)'],
  [/\bshred\b|\bsdelete\b/, 'secure erase'],

  [/\bdd\b.*\bof=\/dev\//, 'raw write to a block device'],
  [/\bmkfs(\.\w+)?\b|\bformat-volume\b|\bformat\s+[a-z]:/, 'filesystem format'],
  [/\bclear-disk\b|\binitialize-disk\b|\bdiskpart\b|\bset-disk\b/, 'disk reinitialisation'],
  [/>\s*\/dev\/(sd|nvme|hd)/, 'redirect over a block device'],

  [/\bgit\s+push\b(?=.*(--force(?!-with-lease)|\s-f\b))/, 'force push (use --force-with-lease)'],
  [/\bgit\s+push\b.*--delete|\bgit\s+push\b.*\s:\S/, 'deleting a remote ref'],
  [/\bgit\s+reset\s+--hard\b/, 'discards uncommitted work irrecoverably'],
  [/\bgit\s+clean\b.*-[a-z]*f/, 'deletes untracked files irrecoverably'],
  [/\bgit\s+branch\s+-d\b/, 'force-deletes a branch'],
  [/\bgit\s+filter-branch\b|\bgit\s+filter-repo\b/, 'rewrites history'],
  [/\bgit\s+reflog\s+expire\b|\bgit\s+gc\s+--prune=now\b/, 'destroys the recovery log'],

  [/\bnpm\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b|\bbun\s+publish\b/, 'publishes a package'],
  [/\bgh\s+release\s+delete\b|\bgh\s+repo\s+delete\b/, 'deletes a GitHub resource'],

  [/\breg\s+delete\b|\bremove-item(property)?\s+.*\bhk(lm|cu|cr|u|cc):/, 'registry deletion'],
  [/\bshutdown\b|\bstop-computer\b|\brestart-computer\b/, 'shuts the machine down'],
  [/\bdocker\s+system\s+prune\b.*-a|\bdocker\s+volume\s+rm\b/, 'destroys docker volumes'],

  [
    /\b(curl|wget|iwr|invoke-webrequest)\b[^|]*\|\s*(sh|bash|zsh|pwsh|powershell|iex|invoke-expression)\b/,
    'pipes a download into a shell',
  ],
];

// Box profile (SPECTRE_WORKER_PROFILE=box, set in /etc/environment on the Spectre):
// the worker box. A disposable, self-contained appliance — OS reinstallable from
// USB, data mirrored by warp and git — so local rm/mkfs/docker are not the risk
// there; the box git policy is: commits, pushes, PR creation/review, branch
// creation are normal work; merges, deletions, and anything that erases history
// are human-only.
const BOX_DESTRUCTIVE = [
  // git: no merge, no deletion, no history erasure — by policy, not
  // because they are always irreversible locally.
  [/\bgit\s+merge\b/, 'merging branches is a human decision on the box'],
  [/\bgit\s+pull\b.*\b(--rebase|--squash)\b/, 'history-altering pull'],
  [/\bgit\s+rebase\b/, 'rebasing rewrites commits'],
  [/\bgit\s+reset\s+--hard\b/, 'discards uncommitted work irrecoverably'],
  [/\bgit\s+clean\b.*-[a-z]*f/, 'deletes untracked files irrecoverably'],
  [/\bgit\s+(branch|tag)\s+-[dD]\b|\bgit\s+branch\s+--delete\b/, 'deleting a branch or tag'],
  [/\bgit\s+push\b(?=.*(--force(?!-with-lease)|\s-f\b))/, 'force push to a remote (use --force-with-lease)'],
  [/\bgit\s+push\b.*--delete|\bgit\s+push\b.*\s:refs?|\bgit\s+push\b.*\s:[^\s/]/, 'deleting a remote ref'],
  [/\bgit\s+filter-branch\b|\bgit\s+filter-repo\b/, 'rewrites history'],
  [/\bgit\s+reflog\s+expire\b|\bgit\s+gc\s+--prune=now\b/, 'destroys the recovery log'],
  [/\bgh\s+(pr|issue)\s+(close|delete)\b|\bgh\s+release\s+delete\b|\bgh\s+repo\s+delete\b/, 'closes or deletes a GitHub resource'],
  [/\bnpm\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b|\bbun\s+publish\b/, 'publishes a package'],

  [/\bdd\b.*\bof=\/dev\/(nvme\d+n\d+(?![a-z0-9])|sd[a-z](?!\d)|hd[a-z](?!\d))\b/, 'raw write over a whole disk device'],
  [/>\s*\/dev\/(sd|nvme|hd)/, 'redirect over a block device'],

  [/\b(shutdown|poweroff|halt|stop-computer)\b/, 'powers off the box — nobody there to turn it back on'],
  [/\bsystemctl\s+(reboot|poweroff|halt)\b/, 'restarts or powers off the box from systemd'],

  [
    /\b(curl|wget|iwr|invoke-webrequest)\b[^|]*\|\s*(sh|bash|zsh|pwsh|powershell|iex|invoke-expression)\b/,
    'pipes a download into a shell',
  ],
];

// These outlive the override window, so no window opens them.
const BYPASS = [
  [/--dangerously-bypass-approvals-and-sandbox/, 're-enters Codex with approvals and sandbox disabled'],
  [/-a\s+never\b|--ask-for-approval\s+never/, 're-enters Codex with approvals disabled'],
  [/-s\s+danger-full-access\b|--sandbox\s+danger-full-access/, 're-enters Codex with the sandbox disabled'],
  [/--dangerously-bypass-hook-trust/, 'runs hooks without persisted trust'],
  [/--dangerously-skip-permissions/, 're-enters Claude Code with permissions disabled'],
  [/\bclaude\b.*--settings\b/, 'starts Claude Code with a different permission set'],
  [/disableallhooks/, 'disables the hook system'],
  [/\bopencode\b.*(--config\b|opencode_config)/, 'starts opencode with a different config'],
  [/opencode_disable_project_config/, 'disables opencode project config discovery'],
];

// Every agent's configuration is protected from every agent: whichever
// session runs, the guard that constrains it is never writable from inside.
const PROTECTED_SHAPE =
  /\.codex[\\/](config|instructions)|\.codex\.toml|\.allow-irreversible|\.claude[\\/](settings|hooks)|\.claude\.json|\.config[\\/]opencode[\\/](opencode\.json|plugin)/;
const WRITE_VERB =
  /(^|\s|\|)(>{1,2}|(tee|set-content|out-file|add-content|cp|copy|copy-item|mv|move|move-item|rm|del|remove-item|ni|new-item|touch)\b|sed\s+-i)/;

// Normalize both sides to forward slashes. Normalizing one side to backslashes
// and then join()ing the other re-introduces '/' on Linux, so the comparison
// never matched and the write-tool protection was silently inert.
const norm = (p) => resolve(p).toLowerCase().replace(/\\/g, '/');

function isProtected(p) {
  if (!p) return false;
  const abs = norm(p);
  const cd = norm(CODEX_DIR);
  const claude = norm(join(HOME, '.claude'));
  const oc = norm(join(HOME, '.config', 'opencode'));
  return (
    abs === `${cd}/config.toml` ||
    abs === `${cd}/instructions.md` ||
    abs === norm(OVERRIDE_FILE) ||
    abs.startsWith(`${cd}/hooks`) ||
    abs === `${claude}/settings.json` ||
    abs === `${claude}/settings.local.json` ||
    abs.startsWith(`${claude}/hooks`) ||
    abs === norm(join(HOME, '.claude.json')) ||
    abs === `${oc}/opencode.json` ||
    abs.startsWith(`${oc}/plugin`)
  );
}

let payload;
try {
  payload = JSON.parse(readFileSync(0, 'utf8'));
} catch {
  allow();
}

const tool = payload?.tool_name;
const input = payload?.tool_input ?? {};

// Codex write-class tools. apply_patch carries a file path; write/edit carry path or file.
if (['write', 'edit', 'apply_patch'].includes(tool)) {
  const target = input.path ?? input.file ?? input.fileName;
  if (isProtected(target)) {
    block('This file decides what the guard blocks.', `${tool} -> ${target}`, false);
  }
  allow();
}

// Codex execution tools. exec_command carries argv[] (preferred) or a legacy command string;
// shell is the unified exec tool.
if (!['exec_command', 'shell'].includes(tool)) allow();

// Reconstruct a single command line from argv, or fall back to the command string.
const argv = Array.isArray(input.argv) ? input.argv : null;
const raw = argv ? argv.map((a) => (/\s/.test(String(a)) ? `"${a}"` : String(a))).join(' ') : input.command ?? '';

const revealed = revealQuotes(raw);
if (!revealed) allow();

// A shell carrier makes quoted text executable; otherwise quoted text is data, and a comment
// is never executed. Scanning the revealed form regardless is what made the guard block a
// script whose comment merely named a flag.
const scanned = SHELL_CARRIER.test(revealed) ? revealed : stripComments(hideQuotes(raw));

for (const [re, why] of BYPASS) if (re.test(scanned)) block(`Bypass attempt: ${why}.`, raw.slice(0, 200), false);

// The box profile only ever narrows the block list; the bypass and
// protected-config rules above are identical in both profiles, so an
// env var can never become a guard-off switch.
const destructive =
  /^box$/i.test(process.env.SPECTRE_WORKER_PROFILE ?? '') ? BOX_DESTRUCTIVE : DESTRUCTIVE;

// Paths are targets whether or not they are quoted, so this one reads the revealed form —
// but per statement, because a path in one command and a delete in another is not a config
// write. `cat ~/.codex/hooks/x; rm /tmp/a` used to trip this.
for (const stmt of statements(revealed)) {
  if (PROTECTED_SHAPE.test(stmt) && WRITE_VERB.test(stmt)) {
    block("Bypass attempt: writing an agent's own configuration through the shell.", raw.slice(0, 200), false);
  }
}

for (const [re, why] of destructive) {
  if (!re.test(scanned)) continue;
  if (/delete|rm|remove-item/.test(why) && REGENERABLE.test(scanned) && !/\s[/\\]\s|\s[a-z]:[/\\]?\s/.test(scanned)) continue;
  if (overrideActive()) break;
  block(`Irreversible: ${why}.`, `${tool}: ${raw.slice(0, 200)}`);
}

allow();
