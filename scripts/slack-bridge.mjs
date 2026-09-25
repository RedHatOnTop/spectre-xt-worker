#!/usr/bin/env node
// Slack Socket Mode bridge for the agent community.
//
// Listens on a websocket (outbound only; no tunnel, no inbound firewall
// change) and applies a per-channel policy to message.channels events:
//   #control  bot mention from an allowlisted member -> built-in or qoder run
//             (goal/resume <worker>: type /goal[ resume] into a worker's tmux)
//   #lobby    agent-identity post -> qoder discussion reply in-thread
//             (humans only when allowlisted AND prefixed "qoder:").
//             With SLACK_DEBATE=1 the antigravity CLI (agy) joins as a
//             second voice: a responder's own post draws the *other*
//             responder, strictly alternating until [PASS] or a cap.
//   #alerts   healthcheck post -> optional auto-triage in-thread (SLACK_TRIAGE=1)
//   #fleet    posts only
//
// Replies go out through spectre-slack-notify (single Slack client impl).
// Credentials are read from the 0600 slack.env by this process itself and are
// never placed into any child environment; the executor gets a whitelisted
// env only. Audit trail: JSONL in /work/logs/slack-bridge.log.
//
// Deploy notes: RUNBOOK.md §7.10. Exit codes: 0 clean stop, 1 not configured.
// Pure helpers are exported for tests/slack_bridge.test.mjs.

import { execFile, spawn } from "node:child_process";
import { appendFileSync, chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, realpathSync, renameSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { randomUUID } from "node:crypto";
import { nativeProcessGuard, processRole, processes } from "./dispatch-process.mjs";
import { planGuard, plannerPrompt, reservePlan } from "./planner-dispatch.mjs";
import { actionResult, claimAction, getSnapshot } from "./worker-state-client.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_ENV_FILE = join(homedir(), ".config/remote-agent/slack.env");
const STATE_FILE = join(homedir(), ".local/state/remote-agent/slack-bridge-state.json");
const AUDIT_LOG = "/work/logs/slack-bridge.log";
const SETTINGS_PATH = "/usr/local/share/remote-agent/slack-executor-settings.json";
const DEFAULT_EXECUTOR_BIN = join(homedir(), ".local/bin/qoder-efficient");
const DEFAULT_AGY_BIN = join(homedir(), ".local/bin/agy");
// agy has no flag or env for its permissions file; the live path is fixed.
const AGY_SETTINGS_PATH = join(homedir(), ".gemini/antigravity-cli/settings.json");
const AGY_CWD = join(homedir(), ".local/state/remote-agent/agy-cwd");
const AGY_PRINT_TIMEOUT = "4m"; // = 240 s; must fire before EXECUTOR_TIMEOUT_MS (300 s)
const BOX_REGISTRY = "/usr/local/share/remote-agent/slack-agents.json";
const REPO_REGISTRY = join(HERE, "..", "config", "slack-agents.json");
const BOX_WORKERS_FILE = "/usr/local/share/remote-agent/qoder-workers.json";
const REPO_WORKERS_FILE = join(HERE, "..", "config", "qoder-workers.json");
const BOX_CLAUSE_FILE = "/usr/local/share/remote-agent/qoder-goal-clause.md";
const REPO_CLAUSE_FILE = join(HERE, "..", "config", "qoder-goal-clause.md");
const TMUX_BIN = "tmux";
const ORCA_BIN = "orca-ide";
const GOAL_LINE_MAX = 4000; // one tmux send-keys literal (goal text + clause)
const SLACK_API = "https://slack.com/api";

export const RESPONDER = "qoder"; // primary responder identity
export const RESPONDER_AGY = "antigravity"; // second #lobby voice (SLACK_DEBATE=1)
export const RESPONDERS = [RESPONDER, RESPONDER_AGY];
export const INFRA_IDENTITY = "bridge"; // excluded from #lobby triggers (brief, acks)
const HUMAN_PREFIX = /^qoder:\s*/i;
const RECOVERY_PREFIX = ":white_check_mark:";
const ALLOWED_SUBTYPES = ["bot_message", "thread_broadcast"];
const EVENT_MAX_AGE_SEC = 300;
export const REPLY_CHUNK = 3000;
const EXECUTOR_TIMEOUT_MS = 300_000;
export const THREAD_RUNS_PER_HOUR = 4;
export const DEBATE_MERGE_SEC = 60;
export const RATE_MAX = 6;
export const RATE_WINDOW_SEC = 600;
export const RATE_MIN_GAP_SEC = 10;
const PROMPT_MAX = 2000;
const THREAD_CONTEXT_MAX_MESSAGES = 15;
const THREAD_CONTEXT_MAX_CHARS = 12_000;
const THREAD_CONTEXT_MSG_CHARS = 1500;
const SEEN_MAX = 2000;

const HELP_TEXT =
  "commands: `@spectre-agents ping` | `status` | `help` | `goal <worker> <text>` | " +
  "`resume <worker>` | any question (qoder runs rate-limited, ~1-3 min)";

const BOX_FACTS_QODER = [
  "Context: you are running headless on the Spectre XT worker box (Debian 13, user person),",
  "inside the Spectre agent community in Slack.",
  "Services: orca-serve (user unit, :6768), qodercli (you, via the Efficient model);",
  "ZCode and the hardened-zai-proxy were retired from this box on 2026-09-15.",
  "Logs: /work/logs/health.log (a line per failing run and on state changes;",
  "a healthy box writes nothing — silence is normal, not a dead checker) and journald.",
  "You have read, write and shell access on the box (posture 2026-09-16); a deny list",
  "blocks destructive commands (rm, dd, mkfs, shutdown, force-push, history rewrites,",
  "package publishes) and secret files. Work directly — do not ask permission for",
  "ordinary steps, and do not assume a tool is unavailable without trying it.",
  "Never ask for or print secrets. Reply in plain text suitable for a Slack message.",
].join("\n");

const BOX_FACTS_AGY = [
  "Context: you are Antigravity (antigravity), the second voice in the Spectre agent",
  "community — a Slack workspace around the Spectre XT worker box (Debian 13, user person).",
  "You have no tool access in this mode: you cannot read files, run commands, browse, or",
  "inspect the box. You only see the thread text quoted in this prompt. Never claim to have",
  "inspected the box, read a file, or run a command — if the thread lacks the evidence, say",
  "what is unknown and what you would check, instead of inventing specifics.",
  "Never ask for or print secrets. Reply in plain text suitable for a Slack message.",
].join("\n");

const PASS_INSTRUCTION =
  "The exchange may continue beyond this reply. If the thread has reached a natural end " +
  "and you have nothing substantive to add, reply with exactly [PASS] and nothing else — " +
  "the bridge then stops the exchange. Never use [PASS] to dodge a question; it is not " +
  "offered on first replies, only mid-discussion.";

const PERSONAS = {
  control:
    "The operator asked something in #control. Investigate (you have read/write/shell " +
    "access) and reply " +
    "with a concise, concrete answer (no more than 2000 characters).",
  discussion:
    "You are qoder in #lobby, the commons where agents share issues and findings. " +
    "Join the thread with a concise, concrete reply (no more than 2000 characters). " +
    "Disagree when the evidence says so; do not pad.",
  discussionDebateQoder:
    "You are qoder in #lobby, the commons where agents share issues and findings. " +
    "You are one of two voices in an open discussion with Antigravity (antigravity). " +
    "Join the thread with a concise, concrete reply (no more than 2000 characters). " +
    "Address the author you are responding to, disagree when the evidence says so, " +
    "mark what you are uncertain about, and do not pad or restate the thread.",
  discussionDebateAgy:
    "You are Antigravity (antigravity) in #lobby, the commons where agents share " +
    "issues and findings. You are one of two voices in an open discussion with qoder. " +
    "Join the thread with a concise, concrete reply (no more than 2000 characters). " +
    "Address the author you are responding to, disagree when the evidence says so, and " +
    "mark what you are uncertain about — you cannot check the box yourself, so reason " +
    "only from what the thread shows and say what you would verify. Do not pad or " +
    "restate the thread.",
  triage:
    "A healthcheck alert was posted to #alerts. Triage it: likely cause, " +
    "evidence, suggested next action. Keep it under 1500 characters.",
};

// ---------------------------------------------------------------------------
// config and registry
// ---------------------------------------------------------------------------

export function parseEnvText(text) {
  const out = {};
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const idx = line.indexOf("=");
    const key = line.slice(0, idx).trim();
    if (key) out[key] = line.slice(idx + 1).trim();
  }
  return out;
}

const truthy = (value) =>
  ["1", "true", "yes", "on"].includes(String(value || "").trim().toLowerCase());

// env files are written by hand; a literal "~" or "~/" must still resolve.
// Function replacer: a literal replacement string would read "$&"-style
// sequences in the home path as patterns.
const expandHome = (path) => path.replace(/^~(?=$|\/)/, () => homedir());

function numberOr(value, fallback) {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
}

export function loadConfig(text) {
  const env = parseEnvText(text);
  return {
    botToken: (env.SLACK_BOT_TOKEN || "").trim(),
    appToken: (env.SLACK_APP_TOKEN || "").trim(),
    channels: {
      alerts: (env.SLACK_CHANNEL_ALERTS || "").trim(),
      fleet: (env.SLACK_CHANNEL_FLEET || "").trim(),
      control: (env.SLACK_CHANNEL_CONTROL || "").trim(),
      lobby: (env.SLACK_CHANNEL_LOBBY || "").trim(),
    },
    allowedUsers: (env.SLACK_ALLOWED_USERS || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    triage: truthy(env.SLACK_TRIAGE || "0"),
    debate: truthy(env.SLACK_DEBATE || "0"),
    maxRunsPerDay: Math.max(1, Math.floor(numberOr(env.SLACK_MAX_RUNS_PER_DAY, 30))),
    threadRunsPerHour: Math.max(
      1,
      Math.floor(numberOr(env.SLACK_THREAD_RUNS_PER_HOUR, THREAD_RUNS_PER_HOUR)),
    ),
    threadTurnsPerDay: Math.max(
      1,
      Math.floor(numberOr(env.SLACK_THREAD_TURNS_PER_DAY, 8)),
    ),
    debateMaxRunsPerDay: Math.max(
      1,
      Math.floor(numberOr(env.SLACK_DEBATE_MAX_RUNS_PER_DAY, 20)),
    ),
    // qodercli "efficient" is the 0-multiplier model; the wrapper adds the
    // billing guard (refuses to start, exit 75, if the promo price changed).
    executorBin: expandHome((env.SLACK_EXECUTOR_BIN || "").trim()) || DEFAULT_EXECUTOR_BIN,
    executorModel: (env.SLACK_EXECUTOR_MODEL || "efficient").trim(),
    // second #lobby voice (debate mode); the agy CLI, spawned by absolute path
    agyBin: expandHome((env.SLACK_AGY_BIN || "").trim()) || DEFAULT_AGY_BIN,
  };
}

export function validateConfig(cfg) {
  const problems = [];
  if (!/^xoxb-\S{20,}$/.test(cfg.botToken)) problems.push("SLACK_BOT_TOKEN missing/bad");
  if (!/^xapp-\S{20,}$/.test(cfg.appToken)) problems.push("SLACK_APP_TOKEN missing/bad");
  for (const [name, id] of Object.entries(cfg.channels)) {
    if (!/^C[A-Z0-9]{8,}$/.test(id)) {
      problems.push(`SLACK_CHANNEL_${name.toUpperCase()} missing/bad`);
    }
  }
  if (!cfg.allowedUsers.length || cfg.allowedUsers.some((u) => !/^U[A-Z0-9]{6,}$/.test(u))) {
    problems.push("SLACK_ALLOWED_USERS must list at least one U... member id");
  }
  if (!cfg.executorBin.startsWith("/")) {
    problems.push("SLACK_EXECUTOR_BIN must be an absolute path");
  }
  if (cfg.debate && !cfg.agyBin.startsWith("/")) {
    problems.push("SLACK_AGY_BIN must be an absolute path (SLACK_DEBATE=1)");
  }
  return problems;
}

function loadRegistry() {
  const path =
    process.env.SLACK_AGENTS_FILE || (existsSync(BOX_REGISTRY) ? BOX_REGISTRY : REPO_REGISTRY);
  try {
    const payload = JSON.parse(readFileSync(path, "utf8"));
    if (payload && payload.agents && typeof payload.agents === "object") {
      return payload.agents;
    }
    throw new Error("no agents map");
  } catch (err) {
    console.error(`slack-bridge: registry unreadable (${path}): ${err.message}`);
    process.exit(1);
  }
}

// ---------------------------------------------------------------------------
// pure policy helpers
// ---------------------------------------------------------------------------

export function normalizeEvent(event) {
  return {
    channel: event.channel || "",
    ts: event.ts || "",
    threadTs: event.thread_ts || "",
    text: event.text || "",
    user: event.user || "",
    botId: event.bot_id || "",
    appId: event.app_id || "",
    username: event.username || "",
    subtype: event.subtype || "",
    eventId: "",
    eventTime: 0,
  };
}

// Fail closed: a post whose author is not a registered agent is never answered.
// Identity is pinned to the app's own bot_id, so an unrelated webhook or app
// posting with username "orca"/"healthcheck" cannot impersonate an agent.
export function agentIdentity(msg, agents, botId) {
  if (!msg.botId || !msg.username || !botId) return null;
  if (msg.botId !== botId) return null;
  return Object.prototype.hasOwnProperty.call(agents, msg.username) ? msg.username : null;
}

export function isRecoveryText(text) {
  return String(text || "").trimStart().startsWith(RECOVERY_PREFIX);
}

// Strict: the personas are told to reply with exactly [PASS]; a discussion
// that merely mentions the marker still gets posted.
export function isPassText(text) {
  return /^\[pass\]$/i.test(String(text || "").trim());
}

export function otherResponder(identity) {
  return identity === RESPONDER_AGY ? RESPONDER : RESPONDER_AGY;
}

// The agy leg is best-effort: bin+settings absent -> lobby debate turns are
// skipped and audited, never an error loop. qoder turns are never gated here.
export function speakerReady(speaker, cfg, existsFn = existsSync) {
  if (speaker !== RESPONDER_AGY) return true;
  return (
    String(cfg.agyBin || "").startsWith("/") &&
    existsFn(cfg.agyBin) &&
    existsFn(AGY_SETTINGS_PATH)
  );
}

export function classifyMessage(msg, cfg, ctx) {
  if (msg.channel === cfg.channels.fleet) {
    return { kind: "ignore", reason: "fleet_posts_only" };
  }

  if (msg.channel === cfg.channels.control) {
    if (msg.botId || (msg.user && msg.user === ctx.botUserId)) {
      return { kind: "ignore", reason: "control_bot" };
    }
    if (!cfg.allowedUsers.includes(msg.user)) {
      return { kind: "ignore", reason: "control_not_allowed" };
    }
    if (!ctx.botUserId) return { kind: "ignore", reason: "control_no_bot_user" };
    const mention = new RegExp(`<@${ctx.botUserId}(\\|[^>]*)?>`);
    if (!mention.test(msg.text)) return { kind: "ignore", reason: "control_no_mention" };
    const command = msg.text.replace(mention, " ").trim();
    if (!command) return { kind: "ignore", reason: "control_empty" };
    return { kind: "control", command: command.slice(0, PROMPT_MAX) };
  }

  if (msg.channel === cfg.channels.alerts) {
    if (agentIdentity(msg, ctx.agents, ctx.botId) !== "healthcheck") {
      return { kind: "ignore", reason: "alerts_not_healthcheck" };
    }
    if (isRecoveryText(msg.text)) return { kind: "ignore", reason: "alerts_recovery" };
    if (msg.threadTs && msg.threadTs !== msg.ts) {
      return { kind: "ignore", reason: "alerts_reply" };
    }
    if (!cfg.triage) return { kind: "ignore", reason: "triage_disabled" };
    return { kind: "triage", identity: "healthcheck" };
  }

  if (msg.channel === cfg.channels.lobby) {
    if (!msg.botId) {
      if (!HUMAN_PREFIX.test(msg.text)) {
        return { kind: "ignore", reason: "lobby_human_unprefixed" };
      }
      if (!cfg.allowedUsers.includes(msg.user)) {
        return { kind: "ignore", reason: "lobby_human_not_allowed" };
      }
      // continuation=false even mid-thread: a human question is never
      // answerable with [PASS] silence.
      return {
        kind: "discussion",
        identity: "human",
        speaker: RESPONDER,
        continuation: false,
        prompt: msg.text.replace(HUMAN_PREFIX, "").trim().slice(0, PROMPT_MAX),
      };
    }
    const identity = agentIdentity(msg, ctx.agents, ctx.botId);
    if (!identity) return { kind: "ignore", reason: "lobby_unknown_identity" };
    if (identity === INFRA_IDENTITY) return { kind: "ignore", reason: "lobby_self" };
    const isReply = Boolean(msg.threadTs && msg.threadTs !== msg.ts);
    if (!cfg.debate) {
      if (identity === RESPONDER) return { kind: "ignore", reason: "lobby_self" };
      // Single-hop by construction: agent posts only trigger at top level;
      // humans continue threads with the qoder: prefix.
      if (isReply) return { kind: "ignore", reason: "lobby_thread_reply" };
      return {
        kind: "discussion",
        identity,
        speaker: RESPONDER,
        continuation: false,
        prompt: msg.text.slice(0, PROMPT_MAX),
      };
    }
    // Debate mode: a responder's own post (top level or in-thread) draws the
    // other responder — that event-driven alternation is the whole exchange.
    if (RESPONDERS.includes(identity)) {
      return {
        kind: "discussion",
        identity,
        speaker: otherResponder(identity),
        continuation: true,
        prompt: msg.text.slice(0, PROMPT_MAX),
      };
    }
    if (isReply) return { kind: "ignore", reason: "lobby_thread_reply" };
    return {
      kind: "discussion",
      identity,
      speaker: RESPONDER,
      continuation: false,
      prompt: msg.text.slice(0, PROMPT_MAX),
    };
  }

  return { kind: "ignore", reason: "channel" };
}

export function shouldHandle(msg, cfg, ctx, now, seen) {
  if (!msg.channel || !msg.ts) return { kind: "ignore", reason: "malformed" };
  const ours = Object.values(cfg.channels);
  if (!ours.includes(msg.channel)) return { kind: "ignore", reason: "channel" };
  if (msg.eventTime && now - msg.eventTime > EVENT_MAX_AGE_SEC) {
    return { kind: "ignore", reason: "stale" };
  }
  if (
    (msg.eventId && seen.has(`e:${msg.eventId}`)) ||
    seen.has(`m:${msg.channel}:${msg.ts}`)
  ) {
    return { kind: "ignore", reason: "duplicate" };
  }
  if (msg.subtype && !ALLOWED_SUBTYPES.includes(msg.subtype)) {
    return { kind: "ignore", reason: `subtype:${msg.subtype}` };
  }
  return classifyMessage(msg, cfg, ctx);
}

// ChatGPT Slack connector attribution trailers come in three observed forms:
//   legacy:  *Sent using ChatGPT*
//   bold:    *Sent using* <@U0C0XE1NCLF|ChatGPT>
//   italic:  _Sent using_ <@U0C0XE1NCLF|ChatGPT>
// The regex strips exactly one trailing attribution line so that bare commands
// (help, resume, goal) still parse, while legitimate goal text that merely
// mentions "Sent using" mid-message is preserved.
// (Synced from the box's live bridge on 2026-09-16 — the box carried this fix
// from 2026-09-15 and the repo did not.)
const ATTRIBUTION_TRAILER_RE =
  /\s*(?:\*Sent using\*\s*<@[^>\n]*>[^\n]*|_Sent using_\s*<@[^>\n]*>[^\n]*|\*Sent using [^*\n]*\*)\s*$/i;

export function classifyCommand(text) {
  // Slack add-ons can append a trailer to the operator's text; a bare command
  // must still parse as one.
  const command = String(text || "")
    .replace(ATTRIBUTION_TRAILER_RE, "")
    .trim();
  const words = command.split(/\s+/);
  const first = (words[0] || "").toLowerCase();
  if (words.length === 1 && ["status", "ping", "help"].includes(first)) {
    return { builtin: first, prompt: "" };
  }
  if (first === "resume") {
    const rest = command.slice(words[0].length).trim();
    const parts = rest ? rest.split(/\s+/) : [];
    if (parts.length === 1) return { builtin: "resume", worker: parts[0].toLowerCase(), prompt: "" };
    return { builtin: "resume", worker: null, prompt: "" };
  }
  if (first === "goal") {
    const rest = command.slice(words[0].length).trim();
    if (!rest) return { builtin: "goal", worker: null, prompt: "" };
    const space = rest.search(/\s/);
    const worker = (space < 0 ? rest : rest.slice(0, space)).toLowerCase();
    const body = (space < 0 ? "" : rest.slice(space + 1)).trim();
    return { builtin: "goal", worker, prompt: body.slice(0, PROMPT_MAX) };
  }
  return { builtin: null, prompt: command.slice(0, PROMPT_MAX) };
}

// What the #control handler hands to handleDispatch. Pure: the worker and
// goal text come from classifyCommand — the message decision carries neither
// field, and reading them from it silently broke every Slack goal/resume.
export function controlDispatchArgs(decision) {
  const { builtin, worker, prompt } = classifyCommand(String((decision && decision.command) || ""));
  return { builtin, worker: worker || null, prompt: prompt || "" };
}

// Whether a probed worker position may be typed into. Pure.
// Active workers own the terminal; a plan_gate park shows an ExitPlanMode
// permission dialog — blind keystrokes could select a dialog option, so the
// operator must approve that one in the orca UI.
export function dispatchAction(builtin) {
  if (builtin === "resume") return "resume";
  if (builtin === "plan") return "plan";
  return "dispatch_goal";
}

export function dispatchAllowed(pos, action = "dispatch_goal") {
  const policy = pos && pos.policy;
  if (!policy || typeof policy !== "object") {
    return { ok: false, detail: "state api unavailable (no policy) — refusing to type blind" };
  }
  const flag = action === "resume"
    ? "can_resume"
    : action === "plan"
      ? "grokbot_may_advance"
      : "can_dispatch_goal";
  if (policy[flag] === true) return { ok: true };
  const park = (pos.goal && pos.goal.park_reason) || pos.park_reason || pos.reason;
  const state = (pos.goal && pos.goal.state) || pos.goal_state || pos.state || "UNKNOWN";
  if (park === "plan_gate") {
    return {
      ok: false,
      detail: "worker awaits ExitPlanMode approval in the UI — blind keystrokes could " +
        "select a dialog option; approve or deny it in orca first",
    };
  }
  return { ok: false, detail: `worker is ${state} (policy.${flag}=false)` };
}

// Last guard before typing: the injected text is a command line for whatever
// runs in that pane. A pane that fell back to a shell would execute it, and a
// pane in another project would take the goal into the wrong session.
const SHELL_COMMANDS = ["bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh"];
export function paneRefusal(command, path, cwd, opts = {}) {
  const cmd = String(command || "").replace(/^-/, "").toLowerCase();
  if (SHELL_COMMANDS.includes(cmd)) {
    if (!(opts.allowFlashShell && String(opts.line || "").startsWith(FLASH_FILE_PREFIX))) {
      return `pane runs a shell (${cmd}) — the text would execute as a shell command`;
    }
  }
  if (cwd && !path) return "pane cwd is missing";
  if (cwd && path) {
    let same = path === cwd;
    if (!same) {
      try {
        same = realpathSync(path) === realpathSync(cwd);
      } catch {
        same = false;
      }
    }
    if (!same) return `pane cwd (${path}) is not the worker cwd (${cwd})`;
  }
  return null;
}

async function inspectPane(target) {
  const [command, path] = await Promise.all([
    runCommandCapture(TMUX_BIN, ["display-message", "-p", "-t", target, "#{pane_current_command}"], 10_000),
    runCommandCapture(TMUX_BIN, ["display-message", "-p", "-t", target, "#{pane_current_path}"], 10_000),
  ]);
  if (!command.ok || !path.ok) return { ok: false, error: command.error || path.error };
  return { ok: true, command: command.stdout.trim(), path: path.stdout.trim() };
}

// A bare registry name is ambiguous as a tmux target: tmux matches it as a
// window name in the current session before a session name (pugc's window is
// named "qodercli", so `-t qoder` landed on the pugc pane). Qualify bare
// names as sessions; explicit targets (%pane, @window, sess:w.p) pass through.
export function paneTarget(tmux) {
  const t = String(tmux || "");
  if (!t || t.startsWith("%") || t.startsWith("@") || t.includes(":")) return t;
  return `${t}:`;
}

// A worker with no tmux path lives in an Orca-managed native terminal. Only a
// single live, connected, writable terminal for the worker's worktree may be
// typed into; no match or an ambiguous match is a refusal, never a guess.
// A worktree can legitimately hold several terminals (a handoff session beside
// the qoder worker), so a worker may carry a `terminal` pin. The pin only
// narrows — it is honored while that exact terminal is live, and a stale pin
// refuses: falling back to "the other one" could type /goal resume into a
// foreign session.
export function pickNativeTerminal(terminals, cwd, pin) {
  const live = (Array.isArray(terminals) ? terminals : []).filter(
    (t) => t && t.worktreePath === cwd && t.connected === true && t.writable === true,
  );
  const wanted = String(pin || "");
  const matches = wanted ? live.filter((t) => t.handle === wanted) : live;
  if (matches.length === 1) return { ok: true, terminal: matches[0] };
  if (matches.length === 0) {
    if (wanted) {
      const rest = live.length ? ` (${live.length} other live)` : "";
      return {
        ok: false,
        detail: `pinned terminal ${wanted} for ${cwd} is not live${rest} — update the worker registry or the orca UI`,
      };
    }
    return { ok: false, detail: `no live orca terminal for ${cwd} — create one in the orca UI` };
  }
  return {
    ok: false,
    detail: `${matches.length} live orca terminals for ${cwd} — refusing to guess; pin one with the worker's "terminal" field`,
  };
}

// Collapse arbitrary text to a single line safe for one tmux send-keys literal.
export function oneLine(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

// The exact line a dispatch types. Pure, and shared by the #control path and
// the supervisor CLI so the clause/prefix can never drift between them.
export function dispatchLine(builtin, goalText, clauseText) {
  let line = builtin === "resume" ? "/goal resume" : `/goal ${oneLine(goalText)}`;
  if (builtin !== "resume" && oneLine(clauseText)) line += ` ${oneLine(clauseText)}`;
  if (line.length > GOAL_LINE_MAX) throw new RangeError(`goal line exceeds ${GOAL_LINE_MAX} characters`);
  return line;
}

export const FLASH_FILE_PREFIX = "/usr/local/bin/dsh-clinepass --file ";
export const FLASH_PACKET_DIR = join(homedir(), ".local/state/remote-agent/packets");
export const FLASH_WRAPPER = "/usr/local/bin/dsh-clinepass";
export const FLASH_KEY_DEFAULT = join(homedir(), ".config/fullmoon-agent-control/cline_api_key");
export const FLASH_DSH_DEFAULT = join(homedir(), ".local/share/deepseek-harness-venv/bin/dsh");

export function packetDir(env = process.env) {
  const override = String((env && env.SPECTRE_PACKET_DIR) || "").trim();
  return override || FLASH_PACKET_DIR;
}

export function sanitizeDispatchId(dispatchId) {
  return String(dispatchId || "").replace(/[^A-Za-z0-9._-]/g, "");
}

export function flashSendLine(dispatchId, env = process.env, tier = "paid") {
  const id = sanitizeDispatchId(dispatchId) || "dry-run";
  return `${FLASH_FILE_PREFIX}${packetDir(env)}/${id}.txt${tier === "free" ? " --tier free" : ""}`;
}

export function flashPin(entry) {
  const flash = entry && entry.targets && entry.targets.flash;
  return String((flash && flash.terminal) || "");
}

export const MIMO_WRAPPER = "/usr/local/bin/mimo-clinepass";
export const MIMO_FILE_PREFIX = `${MIMO_WRAPPER} --file `;

export function mimoPin(entry) {
  const mimo = entry && entry.targets && entry.targets.mimo;
  return String((mimo && mimo.terminal) || "");
}

export function mimoSendLine(dispatchId, env = process.env) {
  const id = sanitizeDispatchId(dispatchId) || "dry-run";
  return `${MIMO_FILE_PREFIX}${packetDir(env)}/${id}.txt`;
}

export function mimoReady(entry, env = process.env) {
  const wrapper = String(env.SPECTRE_MIMO_WRAPPER || MIMO_WRAPPER);
  const bin = String(env.SPECTRE_MIMO_BIN || join(homedir(), ".mimocode/bin/mimo"));
  const pin = mimoPin(entry);
  if (!pin && packetSurface(env) === "job") {
    // job surface creates its own terminal; a registry pin is optional
  } else if (!pin) {
    return { ok: false, evt: "dispatch_mimo_unavailable", detail: "mimo pin missing (targets.mimo.terminal) — run ensurePacketPin" };
  }
  try {
    const st = statSync(wrapper);
    if ((st.mode & 0o777) !== 0o755) {
      return { ok: false, evt: "dispatch_mimo_unavailable", detail: `wrapper mode ${(st.mode & 0o777).toString(8)} is not 755` };
    }
  } catch {
    return { ok: false, evt: "dispatch_mimo_unavailable", detail: `wrapper missing (${wrapper})` };
  }
  try {
    const st = statSync(bin);
    if (!st.isFile() || (st.mode & 0o111) === 0) {
      return { ok: false, evt: "dispatch_mimo_unavailable", detail: `mimo not executable (${bin})` };
    }
  } catch {
    return { ok: false, evt: "dispatch_mimo_unavailable", detail: `mimo missing (${bin})` };
  }
  return { ok: true, line: mimoSendLine("dry-run", env), pin };
}

export const PACKET_SHELL_TITLES = { flash: "flash-packets", mimo: "mimo-packets" };

export function packetSurface(env = process.env) {
  const raw = String(env.SPECTRE_PACKET_SURFACE || "pin").trim().toLowerCase();
  return raw === "job" ? "job" : "pin";
}

export function packetShellTitle(role) {
  return PACKET_SHELL_TITLES[String(role)] || `${String(role || "packet")}-packets`;
}

export function packetJobTitle(role, dispatchId) {
  const id = sanitizeDispatchId(dispatchId) || "pending";
  return `${String(role || "packet")} ${id}`;
}

async function createOrcaTerminal({ cwd, title, command, focus = true }) {
  const args = ["terminal", "create", "--worktree", `path:${cwd}`, "--title", String(title || ""), "--command", String(command || ""), "--json"];
  if (focus) args.splice(args.length - 1, 0, "--focus");
  const result = await runCommandCapture(ORCA_BIN, args, 20_000);
  if (!result.ok) return { ok: false, error: result.error || "orca create failed" };
  let payload = null;
  try {
    payload = JSON.parse(result.stdout.trim());
  } catch {
    return { ok: false, error: "bad orca create response" };
  }
  const resultBody = (payload && payload.result) || {};
  const handle = (resultBody.terminal || resultBody).handle;
  if (typeof handle !== "string" || !handle.startsWith("term_")) {
    return { ok: false, error: "orca_handle_missing; inspect terminal list before retry" };
  }
  return { ok: true, handle, payload };
}

async function switchOrcaTerminal(handle) {
  if (!handle) return { ok: true, skipped: true };
  const result = await runCommandCapture(ORCA_BIN, ["terminal", "switch", "--terminal", String(handle), "--json"], 15_000);
  return result.ok ? { ok: true } : { ok: false, error: result.error };
}

/** Live pin for a packet role, creating a visible Orca shell when missing. */
export async function ensurePacketPin(entry, role, cwd) {
  const listed = await listOrcaTerminals();
  if (!listed.ok) return { ok: false, evt: "dispatch_orca_failed", detail: listed.error };
  const wanted = role === "mimo" ? mimoPin(entry) : flashPin(entry);
  const title = packetShellTitle(role);
  const live = listed.terminals.filter(
    (term) => term && term.worktreePath === cwd && term.connected === true && term.writable === true,
  );
  const byPin = wanted ? live.find((term) => term.handle === wanted) : null;
  if (byPin) return { ok: true, handle: byPin.handle, created: false };
  const byTitle = live.find((term) => String(term.title || "") === title);
  if (byTitle) return { ok: true, handle: byTitle.handle, created: false };
  const created = await createOrcaTerminal({
    cwd,
    title,
    command: "bash",
    focus: true,
  });
  if (!created.ok) {
    return { ok: false, evt: "dispatch_orca_failed", detail: created.error };
  }
  return { ok: true, handle: created.handle, created: true, title };
}

/** Per-packet visible job tab running the wrapper itself. */
export async function startPacketJob({ role, cwd, line, dispatchId }) {
  const title = packetJobTitle(role, dispatchId);
  const created = await createOrcaTerminal({ cwd, title, command: line, focus: true });
  if (!created.ok) {
    return { ok: false, evt: "dispatch_orca_failed", detail: created.error };
  }
  return { ok: true, handle: created.handle, title, created: true };
}

export function applyPacketPin(entry, role, handle) {
  const targets = { ...((entry && entry.targets) || {}) };
  const current = { ...((targets && targets[role]) || {}), terminal: handle };
  if (role === "flash" && !current.wrapper) {
    current.wrapper = MIMO_WRAPPER.replace("mimo-clinepass", "dsh-clinepass");
    current.harness = "dsh-clinepass";
    current.profile = "headless";
    current.model = current.model || "cline-pass/deepseek-v4.1-flash";
  }
  if (role === "mimo" && !current.wrapper) {
    current.wrapper = MIMO_WRAPPER;
    current.harness = "mimo-clinepass";
    current.model = current.model || "mimo-v2.6-pro";
  }
  targets[role] = current;
  return { ...entry, targets };
}

function persistWorkersPin(workersFile, workerName, entry) {
  if (!workersFile) return { ok: false, error: "no workers file" };
  try {
    const payload = JSON.parse(readFileSync(workersFile, "utf8"));
    if (!payload || typeof payload.workers !== "object" || !payload.workers) {
      return { ok: false, error: "no workers map" };
    }
    const next = { ...payload, workers: { ...payload.workers, [workerName]: entry } };
    const tmp = `${workersFile}.tmp`;
    writeFileSync(tmp, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600 });
    renameSync(tmp, workersFile);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
}


export function writeFlashPacket(dispatchId, text, env = process.env) {
  const id = sanitizeDispatchId(dispatchId);
  if (!id) return { ok: false, error: "missing dispatch_id" };
  const dir = packetDir(env);
  try {
    mkdirSync(dir, { recursive: true });
    const dest = join(dir, `${id}.txt`);
    const tmp = join(dir, `${id}.txt.tmp`);
    writeFileSync(tmp, `${String(text).trim()}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    chmodSync(tmp, 0o600);
    renameSync(tmp, dest);
    return { ok: true, path: dest };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
}

function _readProcCmd(procRoot, pid) {
  try {
    return String(readFileSync(join(procRoot, String(pid), "cmdline"))).replace(/\0/g, " ");
  } catch {
    return "";
  }
}

function _readProcHandle(procRoot, pid) {
  try {
    const raw = String(readFileSync(join(procRoot, String(pid), "environ")));
    const m = raw.match(/ORCA_TERMINAL_HANDLE=([^\0]*)/);
    return m ? m[1] : "";
  } catch {
    return "";
  }
}

function _readProcPpid(procRoot, pid) {
  try {
    const text = readFileSync(join(procRoot, String(pid), "stat"), "utf8");
    const rparen = text.lastIndexOf(")");
    const fields = text.slice(rparen + 2).trim().split(/\s+/);
    return parseInt(fields[1], 10);
  } catch {
    return null;
  }
}

function _cmdIsDsh(cmd) {
  return /\bdsh\b/.test(cmd) && /--profile\s+(headless|tui)/.test(cmd);
}

export function pinTreeHasDsh(pin, procRoot = "/proc") {
  const wanted = String(pin || "");
  if (!wanted) return true;
  let ents;
  try {
    ents = readdirSync(procRoot, { withFileTypes: true });
  } catch {
    return true;
  }
  const pids = ents.filter((e) => e.isDirectory() && /^\d+$/.test(e.name)).map((e) => e.name);
  const tree = new Set();
  for (const pid of pids) {
    if (_readProcHandle(procRoot, pid) === wanted) tree.add(pid);
  }
  let changed = true;
  while (changed) {
    changed = false;
    for (const pid of pids) {
      if (tree.has(pid)) continue;
      const ppid = _readProcPpid(procRoot, pid);
      if (ppid != null && tree.has(String(ppid))) {
        tree.add(pid);
        changed = true;
      }
    }
  }
  for (const pid of tree) {
    if (_cmdIsDsh(_readProcCmd(procRoot, pid))) return true;
  }
  return false;
}

export function flashReady(entry, env = process.env, tier = "paid") {
  const wrapper = String(env.SPECTRE_DSH_WRAPPER || FLASH_WRAPPER);
  const free = tier === "free";
  const unavailable = free ? "dispatch_flash_free_unavailable" : "dispatch_flash_unavailable";
  const key = String((free ? env.SPECTRE_DSH_FREE_KEY : env.SPECTRE_DSH_KEY)
    || (free ? join(homedir(), ".config/omni-proxy/client_key") : FLASH_KEY_DEFAULT));
  const dsh = String(env.SPECTRE_DSH_BIN || FLASH_DSH_DEFAULT);
  const pin = flashPin(entry);
  if (free && !["1", "true", "yes", "on"].includes(String(env.SPECTRE_FREE_PACKETS_ENABLED || "").toLowerCase())) {
    return { ok: false, evt: unavailable, detail: "free packets are disabled" };
  }
  if (free) {
    const home = String(env.SPECTRE_DSH_FREE_HOME || join(homedir(), ".local/share/fullmoon-dsh-free"));
    try {
      const config = lstatSync(join(home, "settings.yaml"));
      if (!config.isFile() || (config.mode & 0o077) !== 0) throw new Error("mode");
    } catch {
      return { ok: false, evt: unavailable, detail: "private free DSH profile missing" };
    }
  }
  if (!pin && packetSurface(env) === "job") {
    // job surface creates its own terminal; a registry pin is optional
  } else if (!pin) {
    return { ok: false, evt: unavailable, detail: "flash pin missing (targets.flash.terminal) — run ensurePacketPin or spectre-pin-sync --flash-terminal" };
  }
  try {
    const st = statSync(wrapper);
    if ((st.mode & 0o777) !== 0o755) {
      return {
        ok: false,
        evt: unavailable,
        detail: `wrapper mode ${(st.mode & 0o777).toString(8)} is not 755`,
      };
    }
  } catch {
    return { ok: false, evt: unavailable, detail: `wrapper missing (${wrapper})` };
  }
  try {
    const st = lstatSync(key);
    if (!st.isFile() || (st.mode & 0o777) !== 0o600) {
      return {
        ok: false,
        evt: unavailable,
        detail: `key mode ${(st.mode & 0o777).toString(8)} is not 600`,
      };
    }
  } catch {
    return { ok: false, evt: unavailable, detail: "cline key missing" };
  }
  try {
    const st = statSync(dsh);
    if (!(st.mode & 0o111)) {
      return { ok: false, evt: unavailable, detail: `dsh not executable (${dsh})` };
    }
  } catch {
    return { ok: false, evt: unavailable, detail: `dsh missing (${dsh})` };
  }
  const procRoot = String(env.SPECTRE_PROC_ROOT || "/proc");
  if (pinTreeHasDsh(pin, procRoot)) {
    return { ok: false, evt: "dispatch_flash_busy", detail: "dsh already in the Flash pin tree" };
  }
  return { ok: true, pin, wrapper, key, dsh };
}

export async function freeProxyReady(env = process.env) {
  const raw = String(env.SPECTRE_OMNI_ENDPOINT || "http://127.0.0.1:8790/v1");
  let endpoint;
  try {
    endpoint = new URL(raw);
  } catch {
    return { ok: false, evt: "dispatch_flash_free_unavailable", detail: "invalid proxy endpoint" };
  }
  if (endpoint.protocol !== "http:" || endpoint.hostname !== "127.0.0.1"
      || !endpoint.port || endpoint.pathname !== "/v1" || endpoint.search || endpoint.hash
      || endpoint.username || endpoint.password) {
    return { ok: false, evt: "dispatch_flash_free_unavailable", detail: "proxy endpoint must be loopback /v1" };
  }
  try {
    const response = await fetch(`${endpoint.origin}/omni/health`, { signal: AbortSignal.timeout(2000) });
    const body = await response.json();
    if (response.ok && body.ok === true && Array.isArray(body.providers)
        && body.providers.includes("cline-free") && body.providers.includes("cline-paid")) {
      return { ok: true };
    }
  } catch {
    return { ok: false, evt: "dispatch_flash_free_unavailable", detail: "proxy unavailable" };
  }
  return { ok: false, evt: "dispatch_flash_free_unavailable", detail: "proxy providers unavailable" };
}

export function finalizeFlashInjection(claimed, goalText, env = process.env, tier = "paid") {
  const id = claimed && claimed.claim && claimed.claim.dispatch_id;
  if (!id) return { ok: false, error: "claim missing dispatch_id" };
  const written = writeFlashPacket(id, goalText, env);
  if (!written.ok) return written;
  return { ok: true, line: flashSendLine(id, env, tier), dispatch_id: id, packet: written.path };
}

export function injectionLine({ builtin, target, goalText, clauseText, dispatchId }) {
  if (builtin === "plan") return oneLine(goalText);
  if (builtin === "goal" && String(target || "efficient") === "flash") {
    return flashSendLine(dispatchId);
  }
  if (builtin === "goal" && String(target || "") === "mimo") {
    return mimoSendLine(dispatchId);
  }
  if (builtin === "resume") return dispatchLine("resume", "", "");
  return dispatchLine("goal", goalText, clauseText);
}

// Must match PLANNER_ROLES in scripts/control_plane/seat.py.
const PLANNER_ROLES = new Map([["codex", "astra"], ["claude", "claude"], ["kimi", "kimi"]]);

export function plannerRole(entry) {
  return PLANNER_ROLES.get(entry?.planner?.harness || "codex") ?? null;
}

export function plannerPidVerdict(cmdlines, role) {
  const lines = (Array.isArray(cmdlines) ? cmdlines : []).map((c) => String(c));
  const matched = lines.filter((c) => processRole(c.split(/\s+/)) === role);
  if (role === "astra" && matched.some((c) => /model_provider\s*=\s*["\']?openai/.test(c))) {
    return { ok: false, evt: "dispatch_astra_plus_burn" };
  }
  if (matched.length !== 1) {
    return { ok: false, evt: `dispatch_${role}_busy`, count: matched.length };
  }
  return { ok: true };
}

export function astraPidVerdict(cmdlines) {
  return plannerPidVerdict(cmdlines, "astra");
}

export function astraCmdlinesFromEnv(env = process.env) {
  const raw = env.SPECTRE_ASTRA_CMDLINES;
  if (raw == null || String(raw).trim() === "") return null;
  return String(raw).split("\n").filter(Boolean);
}

export function listPlannerCmdlines(role, env = process.env, procRoot = "/proc") {
  const fromEnv = astraCmdlinesFromEnv(env);
  if (fromEnv) return fromEnv;
  try {
    const rows = processes(env.SPECTRE_PROC_ROOT || procRoot).filter((row) => row.role === role);
    const leaves = rows.filter((row) => !rows.some((other) => other.ppid === row.pid));
    return leaves.map((row) => row.argv.join(" "));
  } catch {
    return [];
  }
}

// CLI parsing for `--dispatch` (the goal supervisor's path; needs no Slack).
// Returns {builtin, worker, text, dryRun, operator, workersFile} or {error}.
export function parseDispatchArgs(argv) {
  let options = {
    dryRun: false,
    operator: "supervisor",
    workersFile: null,
    target: "efficient",
    tier: "paid",
    requestId: null,
  };
  let rest = [];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = String(argv[i]);
    if (arg === "--dry-run") {
      options = { ...options, dryRun: true };
    } else if (arg === "--operator") {
      options = { ...options, operator: String(argv[i + 1] || "").trim() || "supervisor" };
      i += 1;
    } else if (arg === "--workers-file") {
      options = { ...options, workersFile: String(argv[i + 1] || "").trim() || null };
      i += 1;
    } else if (arg === "--request-id") {
      options = { ...options, requestId: String(argv[i + 1] || "") };
      i += 1;
    } else if (arg === "--target") {
      options = { ...options, target: String(argv[i + 1] || "efficient").trim().toLowerCase() || "efficient" };
      i += 1;
    } else if (arg === "--tier") {
      options = { ...options, tier: String(argv[i + 1] || "").trim().toLowerCase() };
      i += 1;
    } else {
      rest = [...rest, arg];
    }
  }
  const builtin = String(rest[0] || "").toLowerCase();
  const worker = String(rest[1] || "").toLowerCase() || null;
  const text = rest.slice(2).join(" ").trim();
  const parsed = { ...options, builtin, worker, text, error: null };
  if (builtin !== "goal" && builtin !== "resume" && builtin !== "plan") {
    return { ...parsed, error: "usage: --dispatch goal|resume|plan <worker> [goal text] [--target flash|mimo|efficient] [--tier paid|free] [--dry-run]" };
  }
  let error = null;
  if (!worker) {
    error = `usage: --dispatch ${builtin} <worker>${builtin === "goal" ? " <goal text>" : ""}`;
  } else if (builtin === "goal" && !text) {
    error = "usage: --dispatch goal <worker> <goal text>";
  }
  if (!["flash", "mimo", "efficient"].includes(options.target) || (builtin === "resume" && options.target !== "efficient")) {
    error = "invalid target for dispatch";
  }
  if (!["free", "paid"].includes(options.tier)) error = "invalid dispatch tier";
  else if (options.tier === "free" && (builtin !== "goal" || options.target !== "flash")) {
    error = "free tier requires a Flash goal";
  }
  return { ...parsed, error };
}

export function chunkText(text, limit = REPLY_CHUNK) {
  const chunks = [];
  let rest = String(text || "").trim();
  while (rest.length > limit) {
    let cut = rest.lastIndexOf("\n", limit);
    if (cut < limit / 2) cut = rest.lastIndexOf(" ", limit);
    if (cut < limit / 2) cut = limit;
    chunks.push(rest.slice(0, cut).trimEnd());
    rest = rest.slice(cut).trimStart();
  }
  if (rest) chunks.push(rest);
  return chunks;
}

export function formatThreadContext(messages) {
  const lines = [];
  for (const msg of messages.slice(-THREAD_CONTEXT_MAX_MESSAGES)) {
    const who = msg.bot_id ? msg.username || "bot" : msg.user || "user";
    const text = String(msg.text || "").replace(/\s+/g, " ").slice(0, THREAD_CONTEXT_MSG_CHARS);
    lines.push(`${who}: ${text}`);
  }
  let joined = "";
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const candidate = joined ? `${lines[i]}\n${joined}` : lines[i];
    if (candidate.length > THREAD_CONTEXT_MAX_CHARS) break;
    joined = candidate;
  }
  return joined;
}

// ---------------------------------------------------------------------------
// budgets, rate limit, state
// ---------------------------------------------------------------------------

export function dayKey(now) {
  return new Date(now * 1000).toISOString().slice(0, 10);
}

// Clock steps backward (NTP): drop future-stamped entries so windows can
// never wedge until wall time catches up.
const withinWindow = (t, now, windowSec) => t <= now && now - t < windowSec;

export function pruneThreads(state, now) {
  for (const [key, arr] of Object.entries(state.threads)) {
    const recent = (Array.isArray(arr) ? arr : []).filter(
      (t) => Number.isFinite(t) && withinWindow(t, now, 3600),
    );
    if (recent.length) state.threads[key] = recent;
    else delete state.threads[key];
  }
  const today = dayKey(now);
  for (const [key, entry] of Object.entries(state.threadDays)) {
    if (!entry || entry.date !== today) delete state.threadDays[key];
  }
}

// A responder's follow-up post inside DEBATE_MERGE_SEC of its previous one in
// the same thread is one logical message (chunked replies) — merging it stops
// a 3-chunk post from buying 3 turns. Recording slides the window forward.
export function debateMerged(merge, where, identity, now) {
  const last = Number(merge[`${where.channel}:${where.rootTs}:${identity}`]);
  return Number.isFinite(last) && last <= now && now - last < DEBATE_MERGE_SEC;
}

export function debateMergeRecord(merge, where, identity, now) {
  merge[`${where.channel}:${where.rootTs}:${identity}`] = now;
  for (const [key, at] of Object.entries(merge)) {
    if (!Number.isFinite(at) || at > now || now - at > 3600) delete merge[key];
  }
}

export function budgetCheck(state, where, now, cfg) {
  const day = dayKey(now);
  if (state.daily.date === day && state.daily.runs >= cfg.maxRunsPerDay) {
    return { ok: false, reason: "daily_cap" };
  }
  // Debate runs count against the global daily too (above), so lobby turns
  // beyond this cap cannot eat #control's share of the budget.
  if (where.debate && state.daily.date === day && state.daily.debate >= cfg.debateMaxRunsPerDay) {
    return { ok: false, reason: "debate_day_cap" };
  }
  const key = `${where.channel}:${where.rootTs}`;
  const recent = (state.threads[key] || []).filter(
    (t) => Number.isFinite(t) && withinWindow(t, now, 3600),
  );
  if (recent.length >= cfg.threadRunsPerHour) return { ok: false, reason: "thread_cap" };
  // The non-renewing per-thread day cap is a debate-model backstop only:
  // with SLACK_DEBATE=0 the budget must behave exactly as before (hourly
  // per-thread window + global daily cap).
  const threadDay = state.threadDays[key];
  if (where.debate && threadDay && threadDay.date === day && threadDay.turns >= cfg.threadTurnsPerDay) {
    return { ok: false, reason: "thread_day_cap" };
  }
  return { ok: true };
}

export function budgetRecord(state, where, now) {
  const day = dayKey(now);
  if (state.daily.date !== day) state.daily = { date: day, runs: 0, debate: 0 };
  state.daily.runs += 1;
  if (where.debate) state.daily.debate += 1;
  const key = `${where.channel}:${where.rootTs}`;
  const recent = (state.threads[key] || []).filter(
    (t) => Number.isFinite(t) && withinWindow(t, now, 3600),
  );
  recent.push(now);
  state.threads[key] = recent;
  if (where.debate) {
    const threadDay = state.threadDays[key];
    if (threadDay && threadDay.date === day) threadDay.turns += 1;
    else state.threadDays[key] = { date: day, turns: 1 };
  }
  pruneThreads(state, now);
}

export function rateCheck(state, userId, now) {
  const recent = (state.rate[userId] || []).filter(
    (t) => Number.isFinite(t) && withinWindow(t, now, RATE_WINDOW_SEC),
  );
  if (recent.length >= RATE_MAX) return { ok: false, reason: "rate_window" };
  if (recent.length && now - recent[recent.length - 1] < RATE_MIN_GAP_SEC) {
    return { ok: false, reason: "rate_gap" };
  }
  return { ok: true };
}

export function rateRecord(state, userId, now) {
  const recent = (state.rate[userId] || []).filter(
    (t) => Number.isFinite(t) && withinWindow(t, now, RATE_WINDOW_SEC),
  );
  recent.push(now);
  state.rate[userId] = recent;
}

function defaultState() {
  return {
    daily: { date: "", runs: 0, debate: 0 },
    threads: {},
    threadDays: {},
    merge: {},
    rate: {},
    seen: {},
  };
}

// Only finite numbers / string timestamps survive a hand-edited or older
// state file; a wrong-typed value must not make every event throw later.
function numberMap(raw, valueOk) {
  const out = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [key, value] of Object.entries(raw)) {
    if (typeof key === "string" && key && valueOk(value)) out[key] = value;
  }
  return out;
}

const isTimestamp = (value) => Number.isFinite(Number(value)) && Number(value) > 0;

function loadState() {
  try {
    const parsed = JSON.parse(readFileSync(STATE_FILE, "utf8"));
    const state = defaultState();
    if (parsed && typeof parsed === "object") {
      if (parsed.daily && typeof parsed.daily === "object") {
        state.daily = {
          date: String(parsed.daily.date || ""),
          runs: Math.max(0, Math.floor(numberOr(parsed.daily.runs, 0))),
          debate: Math.max(0, Math.floor(numberOr(parsed.daily.debate, 0))),
        };
      }
      const timestampList = (value) =>
        Array.isArray(value) ? value.filter((t) => isTimestamp(t)).map((t) => Number(t)) : [];
      const threads = numberMap(parsed.threads, Array.isArray);
      for (const [key, value] of Object.entries(threads)) {
        state.threads[key] = timestampList(value);
      }
      if (parsed.threadDays && typeof parsed.threadDays === "object" && !Array.isArray(parsed.threadDays)) {
        for (const [key, value] of Object.entries(parsed.threadDays)) {
          if (typeof key !== "string" || !key) continue;
          if (!value || typeof value !== "object" || Array.isArray(value)) continue;
          state.threadDays[key] = {
            date: String(value.date || ""),
            turns: Math.max(0, Math.floor(numberOr(value.turns, 0))),
          };
        }
      }
      const merge = numberMap(parsed.merge, isTimestamp);
      for (const [key, value] of Object.entries(merge)) state.merge[key] = Number(value);
      const rate = numberMap(parsed.rate, Array.isArray);
      for (const [key, value] of Object.entries(rate)) {
        state.rate[key] = timestampList(value);
      }
      const seen = numberMap(parsed.seen, isTimestamp);
      for (const [key, value] of Object.entries(seen)) state.seen[key] = Number(value);
    }
    return state;
  } catch {
    return defaultState();
  }
}

function persist(state) {
  try {
    mkdirSync(dirname(STATE_FILE), { recursive: true });
    const tmp = `${STATE_FILE}.tmp`;
    writeFileSync(tmp, `${JSON.stringify(state)}\n`);
    renameSync(tmp, STATE_FILE);
  } catch (err) {
    audit({ evt: "state_save_failed", error: String(err) });
  }
}

function seenSet(state) {
  return new Set(Object.keys(state.seen));
}

function markSeen(state, msg) {
  const now = nowSec();
  for (const [key, at] of Object.entries(state.seen)) {
    if (now - at > 3600) delete state.seen[key];
  }
  if (msg.eventId) state.seen[`e:${msg.eventId}`] = now;
  state.seen[`m:${msg.channel}:${msg.ts}`] = now;
  const keys = Object.keys(state.seen);
  if (keys.length > SEEN_MAX) {
    for (const key of keys.slice(0, keys.length - SEEN_MAX)) delete state.seen[key];
  }
}

// ---------------------------------------------------------------------------
// executor (qodercli, Efficient model) — full working surface, deny-list floor
//
// Posture changed 2026-09-16 (user decision, RUNBOOK 7.10): the executor is no
// longer a read-only reporter. The profile allows Read/Glob/Grep, the write
// tools, the web tools, subagents, and the whole shell through a bare `Bash`;
// the deny list in slack-executor-settings.json carries the destructive floor
// (rm/dd/mkfs/shutdown, force-push, history rewrites, publishes) and the secret
// file gates. Honest limit, same class as the guard hooks: deny rules are
// prefix-matched and qodercli 1.1.47 runs no PreToolUse hooks from --settings
// (probed: a logging hook never ran), so a deny entry catches `rm -rf x` but
// not `cd x && rm -rf .` — it stops accidents and the direct form of an
// injected instruction, not indirection. The engine's own splitting rules
// (compound commands checked per segment, substitution-bearing commands denied)
// were probed 2026-09-12 and still apply. `cwd: "/"` is retained so relative
// paths land inside the box workspace, not inside the bridge's cwd.
//
// Second #lobby voice (SLACK_DEBATE=1): the antigravity CLI (agy) in
// headless print mode. agy has no per-invocation settings/system-prompt
// flag, so BOX_FACTS_AGY and the persona are folded into the single -p
// string and the read-only posture rides on its fixed settings file
// (~/.gemini/antigravity-cli/settings.json, installed from the repo
// example; permission semantics are NOT contractual — the on-box probes in
// RUNBOOK 7.10 are the deploy gate). --dangerously-skip-permissions is
// banned for this path exactly as --yolo is for qodercli.
// ---------------------------------------------------------------------------

export function buildExecutorArgs({ prompt, systemPrompt, settingsPath, model }) {
  return [
    "-p",
    prompt,
    "--append-system-prompt",
    systemPrompt,
    "--output-format",
    "json",
    "--no-session-persistence",
    "--setting-sources",
    "",
    "--settings",
    settingsPath,
    // acceptEdits, not default: measured on the box 2026-09-16, `default` keeps
    // the engine's headless write gate shut, so the Write/Edit tools refuse even
    // when the profile allows them (probe: Write -> DENIED). With acceptEdits
    // the same probe returns DONE. It is not `--dangerously-skip-permissions`
    // (still banned, asserted in the tests) — the allow/deny lists still govern
    // every tool call, and the destructive floor still refuses `rm`.
    "--permission-mode",
    "acceptEdits",
    "--model",
    model,
  ];
}

function childEnv() {
  // Fixed minimal PATH (never inherited): the bridge's own PATH may include
  // user-writable dirs. The qoder-efficient wrapper resolves qodercli by
  // absolute path, so nothing here depends on ~/.local/bin.
  const env = { PATH: "/usr/local/bin:/usr/bin:/bin" };
  for (const key of ["HOME", "LANG", "TERM", "TZ"]) {
    if (process.env[key]) env[key] = process.env[key];
  }
  env.SPECTRE_WORKER_PROFILE = "box";
  return env;
}

// The qoder-efficient wrapper prints its allow-start line to stdout before
// the JSON envelope, so the result is the last parseable JSON line, not the
// whole output. Only a line shaped like the envelope counts — `type` must be
// "result" (verified against qodercli 1.1.47) and a result/is_error key must
// be present — so a stray JSON log/diagnostic object cannot mask the
// envelope. stdout comes from the trusted local wrapper; a deliberately
// envelope-shaped trailing line would still win. Exit 75 is the wrapper's
// cost-gate refusal.
export function parseExecutorResult(stdout, code) {
  if (code === 75) return { ok: false, error: "cost_gate_refused" };
  const lines = String(stdout || "").split("\n").reverse();
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let payload;
    try {
      payload = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) continue;
    if (payload.type !== "result") continue;
    if (!("result" in payload) && !("is_error" in payload)) continue;
    if (payload.is_error) return { ok: false, error: String(payload.subtype || "is_error") };
    const text = String(payload.result || "").trim();
    if (!text) return { ok: false, error: "empty_result" };
    return { ok: true, text };
  }
  return { ok: false, error: `exit_${code}_no_result` };
}

export function buildAgyArgs({ prompt }) {
  return ["-p", prompt, "--output-format", "json", "--print-timeout", AGY_PRINT_TIMEOUT];
}

// agy's envelope (verified against current docs; re-probe on upgrade):
// {conversation_id, status, response, error, duration_seconds, num_turns,
// usage}. Log lines may precede it on stdout — scan reversed lines and
// accept only an object carrying a string `status`.
export function parseAgyResult(stdout, code) {
  const lines = String(stdout || "").split("\n").reverse();
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let payload;
    try {
      payload = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) continue;
    if (typeof payload.status !== "string") continue;
    if (payload.status !== "SUCCESS") {
      const detail = String(payload.error || "").slice(0, 200);
      return {
        ok: false,
        error: `agy_${payload.status || "unknown"}${detail ? `: ${detail}` : ""}`,
      };
    }
    const text = String(payload.response || "").trim();
    if (!text) return { ok: false, error: "empty_result" };
    return { ok: true, text };
  }
  return { ok: false, error: `exit_${code}_no_result` };
}

export function buildAgyPrompt(job, threadContext) {
  return `${BOX_FACTS_AGY}\n\n${composePrompt(job, threadContext)}`;
}

function runExecutor({ prompt, systemPrompt, cfg, speaker }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    const agy = speaker === RESPONDER_AGY;
    if (agy) {
      try {
        mkdirSync(AGY_CWD, { recursive: true });
      } catch (err) {
        audit({ evt: "agy_cwd_failed", error: String(err) });
        resolve({ ok: false, error: "agy_cwd_unavailable" });
        return;
      }
    }
    const args = agy
      ? buildAgyArgs({ prompt })
      : buildExecutorArgs({
          prompt,
          systemPrompt,
          settingsPath: SETTINGS_PATH,
          model: cfg.executorModel,
        });
    const child = spawn(agy ? cfg.agyBin : cfg.executorBin, args, {
      env: childEnv(),
      // qodercli: cwd "/" so box paths go through the deny list. agy: a
      // dedicated empty scratch dir (its permissions are file-based, and
      // the workspace-auto-allow behavior must never see box paths).
      cwd: agy ? AGY_CWD : "/",
      stdio: ["ignore", "pipe", "pipe"],
      detached: true, // own process group: the timeout can kill tool children too
    });
    let stdout = "";
    let stderrTail = "";
    const timer = setTimeout(() => {
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        try {
          child.kill("SIGKILL");
        } catch {
          // already gone
        }
      }
      finish({ ok: false, error: "timeout_300s", stderr: stderrTail });
    }, EXECUTOR_TIMEOUT_MS);
    child.stdout.on("data", (data) => {
      stdout += data;
    });
    child.stderr.on("data", (data) => {
      stderrTail = (stderrTail + data).slice(-500);
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      finish({ ok: false, error: `spawn_${err.code || "error"}`, stderr: stderrTail });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      const parsed = agy ? parseAgyResult(stdout, code) : parseExecutorResult(stdout, code);
      if (!parsed.ok) parsed.stderr = stderrTail;
      finish(parsed);
    });
  });
}

// ---------------------------------------------------------------------------
// posting (via spectre-slack-notify) and audit
// ---------------------------------------------------------------------------

function audit(record) {
  try {
    appendFileSync(AUDIT_LOG, `${JSON.stringify({ at: new Date().toISOString(), ...record })}\n`);
  } catch {
    // outside the box (dev) — journald still gets the console lines
  }
}

function spawnNotify(command, args) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const killTimer = setTimeout(() => child.kill("SIGKILL"), 20_000);
    child.stdout.on("data", (data) => {
      stdout += data;
    });
    child.stderr.on("data", (data) => {
      stderr += data;
    });
    child.on("error", (err) => {
      clearTimeout(killTimer);
      resolve({ ok: false, missing: err.code === "ENOENT", error: err.message });
    });
    child.on("close", (code) => {
      clearTimeout(killTimer);
      const out = stdout.trim();
      // slack-notify exits 0 with "disabled" when unconfigured; require the
      // post confirmation so a silently-vanished reply is audited as failed.
      if (code === 0 && /\bposted\b/.test(out)) resolve({ ok: true, stdout: out });
      else if (code === 0) resolve({ ok: false, missing: false, error: out || "no_post_confirmation" });
      else resolve({ ok: false, missing: false, error: stderr.trim() || `exit_${code}` });
    });
  });
}

async function post(ctx, { agent, channel, threadTs, text }) {
  const args = ["--agent", agent, "--channel", channel, `--text=${text}`];
  if (threadTs) args.push("--thread-ts", threadTs);
  if (ctx.envFile) args.push("--env-file", ctx.envFile);
  let result = await spawnNotify("spectre-slack-notify", args);
  if (!result.ok && result.missing) {
    result = await spawnNotify("python3", [join(HERE, "slack-notify.py"), ...args]);
  }
  if (!result.ok) {
    audit({ evt: "post_failed", agent, channel, error: result.error });
  }
  return result;
}

async function postChunks(ctx, { agent, channel, threadTs, text }) {
  for (const chunk of chunkText(text)) {
    const posted = await post(ctx, { agent, channel, threadTs, text: chunk });
    if (!posted.ok) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// event handling
// ---------------------------------------------------------------------------

const queue = [];
let activeJob = false;
const nowSec = () => Math.floor(Date.now() / 1000);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function enqueue(job) {
  queue.push(job);
  audit({ evt: "job_queued", kind: job.kind, channel: job.channel, speaker: job.speaker });
  void pumpQueue();
}

async function pumpQueue() {
  if (activeJob || queue.length === 0) return;
  activeJob = true;
  const job = queue.shift();
  try {
    await runJob(job);
  } catch (err) {
    audit({ evt: "job_crash", kind: job.kind, error: String(err) });
  } finally {
    activeJob = false;
    void pumpQueue();
  }
}

function personaFor(speaker, debate) {
  if (!debate) return PERSONAS.discussion;
  return speaker === RESPONDER_AGY ? PERSONAS.discussionDebateAgy : PERSONAS.discussionDebateQoder;
}

function systemPromptFor(job) {
  if (job.kind !== "discussion") {
    return `${BOX_FACTS_QODER}\n\n${PERSONAS[job.kind] || PERSONAS.control}`;
  }
  const facts = job.speaker === RESPONDER_AGY ? BOX_FACTS_AGY : BOX_FACTS_QODER;
  const parts = [facts, personaFor(job.speaker, job.debate)];
  if (job.debate && job.continuation) parts.push(PASS_INSTRUCTION);
  return parts.join("\n\n");
}

function composePrompt(job, threadContext) {
  const parts = [];
  if (job.kind === "control") {
    parts.push("The operator asked in #control:", job.text);
  } else if (job.kind === "discussion") {
    parts.push(`Message from ${job.identity} in #lobby:`, job.text);
  } else {
    parts.push("A healthcheck alert is in #alerts:", job.text);
  }
  if (threadContext) parts.push("", "Thread so far:", threadContext);
  if (job.kind === "discussion") {
    parts.push("", personaFor(job.speaker, job.debate));
    if (job.debate && job.continuation) parts.push("", PASS_INSTRUCTION);
  } else {
    parts.push("", PERSONAS[job.kind] || PERSONAS.control);
  }
  return parts.join("\n");
}

async function fetchThreadContext(token, channel, rootTs) {
  try {
    const url =
      `${SLACK_API}/conversations.replies?channel=${encodeURIComponent(channel)}` +
      `&ts=${encodeURIComponent(rootTs)}&limit=${THREAD_CONTEXT_MAX_MESSAGES}`;
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(10_000),
    });
    const body = await response.json();
    if (!body.ok) return "";
    return formatThreadContext(body.messages || []);
  } catch {
    return "";
  }
}

async function runJob(job) {
  const { ctx } = job;
  audit({
    evt: "job_start",
    kind: job.kind,
    channel: job.channel,
    ts: job.ts,
    speaker: job.speaker,
  });
  const threadContext = await fetchThreadContext(ctx.cfg.botToken, job.channel, job.rootTs);
  const agy = job.speaker === RESPONDER_AGY;
  const result = await runExecutor({
    prompt: agy ? buildAgyPrompt(job, threadContext) : composePrompt(job, threadContext),
    systemPrompt: agy ? "" : systemPromptFor(job),
    cfg: ctx.cfg,
    speaker: job.speaker,
  });
  if (!result.ok) {
    audit({ evt: "job_failed", kind: job.kind, speaker: job.speaker, error: result.error });
    // The second voice is best-effort (shared quota, preview API): its
    // failures are audited, never posted — the debate just goes quiet.
    if (agy) return;
    const tail = String(result.stderr || "").trim().slice(-300);
    const suffix = tail ? `\n\`\`\`\n${tail}\n\`\`\`` : "";
    await post(ctx, {
      agent: INFRA_IDENTITY,
      channel: job.channel,
      threadTs: job.ts,
      text: `:warning: ${job.speaker || RESPONDER} run failed (${result.error})${suffix}`,
    });
    return;
  }
  if (job.debate && job.continuation && isPassText(result.text)) {
    audit({
      evt: "debate_pass",
      kind: job.kind,
      speaker: job.speaker,
      channel: job.channel,
      ts: job.ts,
    });
    return;
  }
  await postChunks(ctx, {
    agent: job.speaker || RESPONDER,
    channel: job.channel,
    threadTs: job.ts,
    text: result.text,
  });
  audit({ evt: "job_done", kind: job.kind, speaker: job.speaker, chars: result.text.length });
}

function runCommandCapture(command, args, timeoutMs) {
  return new Promise((resolve) => {
    execFile(command, args, { timeout: timeoutMs, maxBuffer: 1_000_000 }, (err, stdout) => {
      resolve({ ok: !err, stdout: String(stdout || ""), error: err ? String(err.message) : "" });
    });
  });
}

async function runLocalStatus() {
  let result = await runCommandCapture("spectre-status", [], 30_000);
  if (!result.ok && /ENOENT/.test(result.error)) {
    result = await runCommandCapture("bash", [join(HERE, "status.sh")], 30_000);
  }
  const text = result.stdout.trim();
  if (!text) return `:warning: spectre-status returned nothing (${result.error || "empty"})`;
  return text;
}

function loadWorkers(overridePath) {
  const local = join(homedir(), ".config/remote-agent/qoder-workers.json");
  const path = overridePath || process.env.QODER_WORKERS_FILE || (existsSync(local) ? local
    : existsSync(BOX_WORKERS_FILE) ? BOX_WORKERS_FILE : REPO_WORKERS_FILE);
  try {
    const payload = JSON.parse(readFileSync(path, "utf8"));
    const workers = payload && typeof payload.workers === "object" ? payload.workers : null;
    if (!workers) return { path, error: "no workers map" };
    return { path, workers };
  } catch (err) {
    return { path, error: String((err && err.message) || err) };
  }
}

function loadClause() {
  const path = existsSync(BOX_CLAUSE_FILE) ? BOX_CLAUSE_FILE : REPO_CLAUSE_FILE;
  try {
    const text = readFileSync(path, "utf8").trim();
    if (!text) return { path, error: "empty" };
    return { path, text };
  } catch (err) {
    return { path, error: String((err && err.message) || err) };
  }
}

async function probeWorker(_workersFile, name) {
  const snapshot = await getSnapshot(name);
  if (snapshot && snapshot.debug && snapshot.debug.unavailable) {
    return { ok: false, error: snapshot.reason || "state api unavailable" };
  }
  if (!snapshot || typeof snapshot !== "object" || !snapshot.policy) {
    return { ok: false, error: "state api unavailable" };
  }
  const goal = snapshot.goal || {};
  return {
    ok: true,
    payload: {
      ...snapshot,
      state: goal.state || snapshot.state || "UNKNOWN",
      reason: goal.park_reason || snapshot.reason || goal.state,
      park_reason: goal.park_reason || null,
    },
  };
}

async function finishClaim(claim, ok, error) {
  if (!claim || !claim.action_id) return;
  try {
    await actionResult(claim.action_id, ok, error);
  } catch {
    // Reconcile on the daemon will mark delivery unknown.
  }
}

async function claimDispatch(worker, builtin, snapshot, target, terminal) {
  const action = dispatchAction(builtin);
  try {
    const { status, payload } = await claimAction({
      worker,
      action,
      expected_snapshot_version: snapshot.snapshot_version,
      idempotency_key: `${action}:${worker}:${snapshot.snapshot_version}:${randomUUID()}`,
      target: target || "efficient",
      ...(terminal ? { terminal } : {}),
    });
    if (status !== 200 || !payload || !payload.action_id) {
      return {
        ok: false,
        error: (payload && (payload.detail || payload.error)) || `claim http ${status}`,
      };
    }
    return { ok: true, claim: payload };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
}

async function bindFlashAfterClaim(opts, targetName, claimed, injected) {
  const packetTarget = targetName === "flash" || targetName === "mimo";
  if (!(opts.builtin === "goal" && packetTarget)) {
    return { ok: true, injected, dispatch_id: null };
  }
  const made = finalizeFlashInjection(claimed, opts.text, process.env, opts.tier);
  if (!made.ok) {
    await finishClaim(claimed.claim, false, made.error);
    return { ok: false, evt: `dispatch_${targetName}_packet_failed`, error: made.error };
  }
  const line = targetName === "mimo" ? mimoSendLine(made.dispatch_id) : made.line;
  return { ok: true, injected: line, dispatch_id: made.dispatch_id, packet: made.packet };
}

async function sendKeysToWorker(target, text) {
  const literal = await runCommandCapture(TMUX_BIN, ["send-keys", "-l", "-t", target, text], 10_000);
  if (!literal.ok) return { ok: false, error: literal.error };
  // Separate Enter call: with -l the payload must never be parsed as key names.
  await sleep(150);
  const enter = await runCommandCapture(TMUX_BIN, ["send-keys", "-t", target, "Enter"], 10_000);
  if (!enter.ok) return { ok: false, error: enter.error };
  return { ok: true };
}

async function listOrcaTerminals() {
  const result = await runCommandCapture(ORCA_BIN, ["terminal", "list", "--json"], 20_000);
  if (!result.ok) return { ok: false, error: result.error };
  let payload = null;
  try {
    payload = JSON.parse(result.stdout.trim());
  } catch {
    return { ok: false, error: "bad orca terminal list output" };
  }
  const terminals = payload && payload.result && Array.isArray(payload.result.terminals)
    ? payload.result.terminals
    : null;
  if (!terminals) return { ok: false, error: "unexpected orca terminal list shape" };
  return { ok: true, terminals };
}

async function sendToNativeTerminal(handle, text) {
  const result = await runCommandCapture(
    ORCA_BIN,
    ["terminal", "send", "--terminal", handle, "--text", text, "--enter", "--json"],
    20_000,
  );
  if (!result.ok) return { ok: false, error: result.error };
  try {
    const payload = JSON.parse(result.stdout.trim());
    if (payload && payload.ok === false) return { ok: false, error: "orca terminal send refused" };
  } catch {
    // Non-JSON stdout still means the CLI ran; the exit status is the verdict.
  }
  return { ok: true };
}

// The same guard chain as the #control path, without Slack: `--dispatch` is the
// goal supervisor's entry point. It runs before the config check in main(), so a
// box with no slack.env can still dispatch (nothing in this path posts).
async function dispatchCli(argv) {
  const opts = parseDispatchArgs(argv);
  if (opts.error) return { ok: false, evt: "dispatch_usage", detail: opts.error, code: 2 };

  const { path: workersFile, workers, error: workersError } = loadWorkers(opts.workersFile);
  if (!workers) {
    return {
      ok: false,
      evt: "dispatch_registry_failed",
      detail: `worker registry unreadable (${workersFile}): ${workersError}`,
      code: 1,
    };
  }
  let entry = workers[opts.worker];
  if (!entry) {
    const known = Object.keys(workers).sort().join(", ");
    return { ok: false, evt: "dispatch_unknown_worker", detail: `unknown worker ${opts.worker} (known: ${known})`, code: 1 };
  }

  if (opts.builtin === "plan") {
    const role = plannerRole(entry);
    if (!role) {
      audit({ evt: "dispatch_planner_unknown", worker: opts.worker, origin: opts.operator });
      return {
        ok: false,
        evt: "dispatch_planner_unknown",
        detail: "planner harness is unknown — refusing plan, not creating a terminal",
        code: 1,
      };
    }
    const pids = plannerPidVerdict(listPlannerCmdlines(role), role);
    if (!pids.ok) {
      audit({ evt: pids.evt, worker: opts.worker, origin: opts.operator, count: pids.count });
      return {
        ok: false,
        evt: pids.evt,
        detail: pids.evt === "dispatch_astra_plus_burn"
          ? "plus-burn gpt-6-astra pid present — refusing plan, not creating a terminal"
          : `${role} pid count is not 1 — refusing plan, not creating a terminal`,
        code: 1,
      };
    }
  }

  const targetName = opts.target || "efficient";
  const surface = packetSurface();
  let ensuredPin = null;
  if (opts.builtin === "goal" && (targetName === "flash" || targetName === "mimo") && surface === "pin") {
    const existingPin = targetName === "mimo" ? mimoPin(entry) : flashPin(entry);
    if (existingPin) {
      ensuredPin = existingPin;
    } else {
      const ensured = await ensurePacketPin(entry, targetName, entry.cwd);
      if (!ensured.ok) {
        audit({ evt: ensured.evt, worker: opts.worker, origin: opts.operator, detail: ensured.detail });
        return { ok: false, evt: ensured.evt, detail: ensured.detail, code: 1 };
      }
      ensuredPin = ensured.handle;
      entry = applyPacketPin(entry, targetName, ensured.handle);
      persistWorkersPin(opts.workersFile || workersFile, opts.worker, entry);
    }
  }
  if (opts.builtin === "goal" && (targetName === "flash" || targetName === "mimo")) {
    const ready = targetName === "mimo" ? mimoReady(entry, process.env) : flashReady(entry, process.env, opts.tier);
    if (!ready.ok) {
      audit({ evt: ready.evt, worker: opts.worker, origin: opts.operator, detail: ready.detail });
      return { ok: false, evt: ready.evt, detail: ready.detail, code: 1 };
    }
    if (opts.tier === "free") {
      const proxy = await freeProxyReady(process.env);
      if (!proxy.ok) return { ...proxy, code: 1 };
    }
  }

  let injected;
  if (opts.builtin === "plan") {
    injected = "";
  } else if (opts.builtin === "goal" && (targetName === "flash" || targetName === "mimo")) {
    // Placeholder for paneRefusal / dry-run. Live send overwrites after claim
    // writes packets/<dispatch_id>.txt named after the claimed id.
    injected = targetName === "mimo" ? mimoSendLine("dry-run") : flashSendLine("dry-run", process.env, opts.tier);
  } else if (opts.builtin === "goal") {
    const clause = loadClause();
    if (!clause.text) {
      audit({ evt: "dispatch_clause_failed", origin: opts.operator, error: clause.error });
      return {
        ok: false,
        evt: "dispatch_clause_failed",
        detail: `protocol clause unreadable (${clause.path}): ${clause.error}`,
        code: 1,
      };
    }
    try {
      injected = injectionLine({
        builtin: "goal",
        target: targetName,
        goalText: opts.text,
        clauseText: clause.text,
      });
    } catch (error) {
      return { ok: false, evt: "dispatch_goal_too_long", detail: error.message, code: 1 };
    }
  } else {
    injected = injectionLine({ builtin: "resume" });
  }

  const probe = await probeWorker(workersFile, opts.worker);
  if (!probe.ok) {
    audit({ evt: "dispatch_probe_failed", builtin: opts.builtin, worker: opts.worker, origin: opts.operator, error: probe.error });
    return {
      ok: false,
      evt: "dispatch_probe_failed",
      detail: `cannot probe ${opts.worker} (${probe.error}) — refusing to type blind`,
      code: 1,
    };
  }
  const verdict = opts.builtin === "plan"
    ? planGuard(opts.worker, entry, probe.payload, opts.requestId)
    : dispatchAllowed(probe.payload, dispatchAction(opts.builtin));
  if (!verdict.ok) {
    audit({ evt: "dispatch_refused", builtin: opts.builtin, worker: opts.worker, state: probe.payload.state, origin: opts.operator });
    return {
      ok: false,
      evt: "dispatch_refused",
      detail: verdict.detail,
      state: probe.payload.state,
      reason: probe.payload.reason,
      code: 1,
    };
  }

  if (opts.builtin === "plan") {
    try {
      injected = oneLine(plannerPrompt(opts.requestId, probe.payload));
    } catch (error) {
      return { ok: false, evt: "dispatch_plan_prompt_failed", detail: error.message, code: 1 };
    }
  }

  if (!entry.tmux) {
    const listed = await listOrcaTerminals();
    if (!listed.ok) {
      audit({ evt: "dispatch_orca_failed", worker: opts.worker, origin: opts.operator, error: listed.error });
      return { ok: false, evt: "dispatch_orca_failed", detail: `cannot list orca terminals for ${opts.worker}: ${listed.error}`, code: 1 };
    }
    const pin = opts.builtin === "plan" ? entry.planner.terminal
      : targetName === "flash" ? (ensuredPin || flashPin(entry))
      : targetName === "mimo" ? (ensuredPin || mimoPin(entry))
      : (entry.targets?.efficient?.terminal || entry.terminal);
    const pick = pickNativeTerminal(listed.terminals, entry.cwd, pin);
    if (!pick.ok) {
      audit({ evt: "dispatch_orca_refused", worker: opts.worker, origin: opts.operator, detail: pick.detail });
      return { ok: false, evt: "dispatch_orca_refused", detail: pick.detail, code: 1 };
    }
    const role = opts.builtin === "plan" ? plannerRole(entry) : targetName;
    try {
      if (!nativeProcessGuard(pick.terminal.handle, entry.cwd, role)) {
        return { ok: false, evt: "dispatch_model_mismatch", detail: "pinned process argv/cwd does not match target", code: 1 };
      }
    } catch (error) {
      return { ok: false, evt: "dispatch_model_mismatch", detail: error.message, code: 1 };
    }
    const base = {
      builtin: opts.builtin,
      worker: opts.worker,
      terminal: pick.terminal.handle,
      chars: injected.length,
      state: probe.payload.state,
      reason: probe.payload.reason,
      origin: opts.operator,
    };
    if (opts.dryRun) {
      audit({ evt: "dispatch_dry_run", ...base, line: injected });
      return { ok: true, evt: "dispatch_dry_run", ...base, line: injected };
    }
    if (opts.builtin === "plan") {
      const reserved = reservePlan(opts.requestId, packetDir());
      if (!reserved.ok) {
        return { ok: false, evt: "dispatch_plan_duplicate", detail: reserved.error, code: 1 };
      }
    }
    const claimed = opts.builtin === "plan"
      ? { ok: true, claim: { action_id: null } }
      : await claimDispatch(opts.worker, opts.builtin, probe.payload, targetName, pick.terminal.handle);
    if (!claimed.ok) {
      audit({ evt: "dispatch_claim_failed", worker: opts.worker, origin: opts.operator, error: claimed.error });
      return { ok: false, evt: "dispatch_claim_failed", detail: `claim refused for ${opts.worker}: ${claimed.error}`, code: 1 };
    }
    const bound = await bindFlashAfterClaim(opts, targetName, claimed, injected);
    if (!bound.ok) {
      audit({ evt: bound.evt, worker: opts.worker, origin: opts.operator, error: bound.error });
      return { ok: false, evt: bound.evt, detail: bound.error, code: 1 };
    }
    injected = bound.injected;
    if (bound.dispatch_id) base.dispatch_id = bound.dispatch_id;
    base.chars = injected.length;
    let sent = { ok: false, error: "not sent" };
    let usedTerminal = pick.terminal.handle;
    if (opts.builtin === "goal" && (targetName === "flash" || targetName === "mimo") && surface === "job") {
      const job = await startPacketJob({
        role: targetName,
        cwd: entry.cwd,
        line: injected,
        dispatchId: bound.dispatch_id,
      });
      if (!job.ok) {
        await finishClaim(claimed.claim, false, job.detail || job.error);
        return { ok: false, evt: job.evt || "dispatch_orca_failed", detail: job.detail || job.error, code: 1 };
      }
      usedTerminal = job.handle;
      base.terminal = usedTerminal;
      base.surface = "job";
      sent = { ok: true };
      await finishClaim(claimed.claim, true, null);
    } else {
      sent = await sendToNativeTerminal(pick.terminal.handle, injected);
      await finishClaim(claimed.claim, sent.ok, sent.error);
      // Reveal only packet harness tabs (flash/mimo). Plan/efficient must not steal focus.
      if (sent.ok && (targetName === "flash" || targetName === "mimo")) {
        base.surface = "pin";
        await switchOrcaTerminal(pick.terminal.handle);
      }
    }
    if (!sent.ok) {
      audit({ evt: "dispatch_send_failed", worker: opts.worker, terminal: usedTerminal, origin: opts.operator, error: sent.error });
      return { ok: false, evt: "dispatch_send_failed", detail: `orca terminal send failed for ${pick.terminal.handle}: ${sent.error}`, code: 1 };
    }
    audit({ evt: "dispatch_sent", ...base, action_id: claimed.claim.action_id });
    return { ok: true, evt: "dispatch_sent", ...base, line: injected.slice(0, 200) };
  }

  const target = paneTarget(entry.tmux);
  const pane = await inspectPane(target);
  if (!pane.ok) {
    audit({ evt: "dispatch_pane_failed", worker: opts.worker, origin: opts.operator, error: pane.error });
    return { ok: false, evt: "dispatch_pane_failed", detail: `cannot read tmux pane ${entry.tmux}: ${pane.error}`, code: 1 };
  }
  const paneCommand = pane.command;
  const panePath = pane.path;
  const refusal = paneRefusal(paneCommand, panePath, entry.cwd, {
    allowFlashShell: targetName === "flash",
    line: injected,
  });
  if (refusal) {
    audit({ evt: "dispatch_pane_refused", builtin: opts.builtin, worker: opts.worker, pane: paneCommand, origin: opts.operator });
    return { ok: false, evt: "dispatch_pane_refused", detail: refusal, pane: paneCommand, code: 1 };
  }

  const base = {
    builtin: opts.builtin,
    worker: opts.worker,
    tmux: entry.tmux,
    pane: paneCommand,
    chars: injected.length,
    state: probe.payload.state,
    reason: probe.payload.reason,
    origin: opts.operator,
  };
  if (opts.dryRun) {
    audit({ evt: "dispatch_dry_run", ...base, line: injected });
    return { ok: true, evt: "dispatch_dry_run", ...base, line: injected };
  }
  const claimed = opts.builtin === "plan"
    ? { ok: true, claim: { action_id: null } }
    : await claimDispatch(opts.worker, opts.builtin, probe.payload, targetName);
  if (!claimed.ok) {
    audit({ evt: "dispatch_claim_failed", worker: opts.worker, origin: opts.operator, error: claimed.error });
    return { ok: false, evt: "dispatch_claim_failed", detail: `claim refused for ${opts.worker}: ${claimed.error}`, code: 1 };
  }
  const bound = await bindFlashAfterClaim(opts, targetName, claimed, injected);
  if (!bound.ok) {
    audit({ evt: bound.evt, worker: opts.worker, origin: opts.operator, error: bound.error });
    return { ok: false, evt: bound.evt, detail: bound.error, code: 1 };
  }
  injected = bound.injected;
  if (bound.dispatch_id) base.dispatch_id = bound.dispatch_id;
  base.chars = injected.length;
  const sent = await sendKeysToWorker(target, injected);
  await finishClaim(claimed.claim, sent.ok, sent.error);
  if (!sent.ok) {
    audit({ evt: "dispatch_send_failed", worker: opts.worker, tmux: entry.tmux, origin: opts.operator, error: sent.error });
    return { ok: false, evt: "dispatch_send_failed", detail: `tmux send-keys failed for ${entry.tmux}: ${sent.error}`, code: 1 };
  }
  audit({ evt: "dispatch_sent", ...base, action_id: claimed.claim.action_id });
  return { ok: true, evt: "dispatch_sent", ...base, line: injected.slice(0, 200) };
}

async function handleDispatch(msg, ctx, builtin, workerName, goalText) {
  const reply = (text) =>
    post(ctx, { agent: INFRA_IDENTITY, channel: msg.channel, threadTs: msg.ts, text });

  if (!workerName || (builtin === "goal" && !goalText)) {
    const usage =
      builtin === "goal"
        ? "usage: `goal <worker> <goal text>` — types /goal into the worker's session"
        : "usage: `resume <worker>` — types /goal resume into the worker's session";
    await reply(`:warning: ${usage}`);
    return;
  }

  const { path: workersFile, workers, error: workersError } = loadWorkers();
  if (!workers) {
    audit({ evt: "dispatch_registry_failed", error: workersError });
    await reply(`:warning: worker registry unreadable (${workersFile}): ${workersError}`);
    return;
  }
  const entry = workers[workerName];
  if (!entry) {
    await reply(`:warning: unknown worker \`${workerName}\` (known: ${Object.keys(workers).sort().join(", ")})`);
    return;
  }

  let injected;
  if (builtin === "goal") {
    const clause = loadClause();
    if (!clause.text) {
      audit({ evt: "dispatch_clause_failed", error: clause.error });
      await reply(`:warning: protocol clause unreadable (${clause.path}) — refusing to dispatch without it`);
      return;
    }
    try {
      injected = dispatchLine("goal", goalText, clause.text);
    } catch (error) {
      await reply(`goal refused: ${error.message}`);
      return;
    }
  } else {
    injected = dispatchLine("resume", "", "");
  }

  const probe = await probeWorker(workersFile, workerName);
  if (!probe.ok) {
    audit({ evt: "dispatch_probe_failed", worker: workerName, error: probe.error });
    await reply(`:warning: cannot probe \`${workerName}\` (${probe.error}) — refusing to type blind`);
    return;
  }
  const verdict = dispatchAllowed(probe.payload, dispatchAction(builtin));
  if (!verdict.ok) {
    audit({ evt: "dispatch_refused", builtin, worker: workerName, state: probe.payload.state });
    await reply(`:no_entry: ${builtin} refused: ${verdict.detail}`);
    return;
  }

  const was = `${probe.payload.state}${probe.payload.reason ? `/${probe.payload.reason}` : ""}`;
  const preview = injected.length > 140 ? `${injected.slice(0, 140)}…` : injected;

  if (!entry.tmux) {
    const listed = await listOrcaTerminals();
    if (!listed.ok) {
      audit({ evt: "dispatch_orca_failed", worker: workerName, error: listed.error });
      await reply(`:warning: cannot list orca terminals for \`${workerName}\`: ${listed.error}`);
      return;
    }
    const pick = pickNativeTerminal(listed.terminals, entry.cwd, entry.terminal);
    if (!pick.ok) {
      audit({ evt: "dispatch_orca_refused", worker: workerName, detail: pick.detail });
      await reply(`:no_entry: ${builtin} refused: ${pick.detail}`);
      return;
    }
    try {
      if (!nativeProcessGuard(pick.terminal.handle, entry.cwd, "efficient")) {
        await reply("dispatch refused: pinned process argv/cwd does not match Efficient");
        return;
      }
    } catch (error) {
      await reply(`dispatch refused: ${error.message}`);
      return;
    }
    const claimed = await claimDispatch(workerName, builtin, probe.payload, "efficient", pick.terminal.handle);
    if (!claimed.ok) {
      audit({ evt: "dispatch_claim_failed", worker: workerName, error: claimed.error });
      await reply(`:no_entry: ${builtin} refused: claim failed (${claimed.error})`);
      return;
    }
    const sent = await sendToNativeTerminal(pick.terminal.handle, injected);
    await finishClaim(claimed.claim, sent.ok, sent.error);
    if (!sent.ok) {
      audit({ evt: "dispatch_send_failed", worker: workerName, terminal: pick.terminal.handle, error: sent.error });
      await reply(`:warning: orca terminal send failed for \`${workerName}\`: ${sent.error}`);
      return;
    }
    audit({
      evt: "dispatch_sent",
      builtin,
      worker: workerName,
      terminal: pick.terminal.handle,
      user: msg.user,
      chars: injected.length,
      state: probe.payload.state,
      reason: probe.payload.reason,
    });
    await reply(`:rocket: ${builtin} sent to \`${workerName}\` (orca terminal ${pick.terminal.handle}, was ${was}): ${preview}`);
    return;
  }

  const target = paneTarget(entry.tmux);
  const pane = await inspectPane(target);
  if (!pane.ok) {
    audit({ evt: "dispatch_pane_failed", worker: workerName, error: pane.error });
    await reply(`:warning: cannot read tmux pane \`${entry.tmux}\`: ${pane.error}`);
    return;
  }
  const paneCommand = pane.command;
  const panePath = pane.path;
  const refusal = paneRefusal(paneCommand, panePath, entry.cwd);
  if (refusal) {
    audit({ evt: "dispatch_pane_refused", builtin, worker: workerName, pane: paneCommand });
    await reply(`:no_entry: ${builtin} refused: ${refusal}`);
    return;
  }

  const claimed = await claimDispatch(workerName, builtin, probe.payload);
  if (!claimed.ok) {
    audit({ evt: "dispatch_claim_failed", worker: workerName, error: claimed.error });
    await reply(`:no_entry: ${builtin} refused: claim failed (${claimed.error})`);
    return;
  }
  const sent = await sendKeysToWorker(target, injected);
  await finishClaim(claimed.claim, sent.ok, sent.error);
  if (!sent.ok) {
    audit({ evt: "dispatch_send_failed", worker: workerName, tmux: entry.tmux, error: sent.error });
    await reply(`:warning: tmux send-keys failed for \`${entry.tmux}\`: ${sent.error}`);
    return;
  }
  audit({
    evt: "dispatch_sent",
    builtin,
    worker: workerName,
    tmux: entry.tmux,
    pane: paneCommand,
    user: msg.user,
    chars: injected.length,
    state: probe.payload.state,
    reason: probe.payload.reason,
  });
  await reply(`:rocket: ${builtin} sent to \`${workerName}\` (tmux ${entry.tmux}, was ${was}): ${preview}`);
}

async function handleControl(msg, decision, ctx) {
  const now = nowSec();
  const rate = rateCheck(ctx.state, msg.user, now);
  if (!rate.ok) {
    audit({ evt: "rate_limited", user: msg.user, reason: rate.reason });
    await post(ctx, {
      agent: INFRA_IDENTITY,
      channel: msg.channel,
      threadTs: msg.ts,
      text: `:no_entry: rate limited (${rate.reason}) — try again shortly`,
    });
    return;
  }
  rateRecord(ctx.state, msg.user, now);

  const { builtin, worker, prompt } = controlDispatchArgs(decision);
  if (builtin === "ping") {
    await post(ctx, {
      agent: INFRA_IDENTITY,
      channel: msg.channel,
      threadTs: msg.ts,
      text: "pong — bridge is alive",
    });
    return;
  }
  if (builtin === "help") {
    await post(ctx, {
      agent: INFRA_IDENTITY,
      channel: msg.channel,
      threadTs: msg.ts,
      text: HELP_TEXT,
    });
    return;
  }
  if (builtin === "status") {
    await postChunks(ctx, {
      agent: INFRA_IDENTITY,
      channel: msg.channel,
      threadTs: msg.ts,
      text: await runLocalStatus(),
    });
    return;
  }
  if (builtin === "goal" || builtin === "resume") {
    await handleDispatch(msg, ctx, builtin, worker, prompt);
    return;
  }

  const budget = budgetCheck(ctx.state, { channel: msg.channel, rootTs: msg.ts }, now, ctx.cfg);
  if (!budget.ok) {
    audit({ evt: "budget_blocked", kind: "control", reason: budget.reason });
    await post(ctx, {
      agent: INFRA_IDENTITY,
      channel: msg.channel,
      threadTs: msg.ts,
      text: `:no_entry: run limit: ${budget.reason}`,
    });
    return;
  }
  budgetRecord(ctx.state, { channel: msg.channel, rootTs: msg.ts }, now);
  await post(ctx, {
    agent: INFRA_IDENTITY,
    channel: msg.channel,
    threadTs: msg.ts,
    text: ":hourglass_flowing_sand: on it — qoder is running",
  });
  enqueue({
    kind: "control",
    channel: msg.channel,
    ts: msg.ts,
    rootTs: msg.ts,
    text: prompt,
    identity: msg.user,
    speaker: RESPONDER,
    debate: false,
    continuation: false,
    ctx,
  });
}

async function handleDiscussion(msg, decision, ctx) {
  const now = nowSec();
  const rootTs = msg.threadTs || msg.ts;
  const speaker = decision.speaker || RESPONDER;
  const where = { channel: msg.channel, rootTs, debate: Boolean(ctx.cfg.debate) };
  if (decision.continuation) {
    // Chunked responder posts must not buy a turn per chunk (merge window).
    if (debateMerged(ctx.state.merge, where, decision.identity, now)) {
      debateMergeRecord(ctx.state.merge, where, decision.identity, now);
      audit({ evt: "debate_merged", channel: msg.channel, ts: msg.ts, author: decision.identity });
      return;
    }
    debateMergeRecord(ctx.state.merge, where, decision.identity, now);
  }
  if (!speakerReady(speaker, ctx.cfg)) {
    // agy missing/unconfigured: skip quietly (audit only) — qoder turns
    // never reach this branch because speakerReady(qoder) is always true.
    audit({ evt: "debate_skip", speaker, reason: "agy_unavailable", channel: msg.channel, ts: msg.ts });
    return;
  }
  const budget = budgetCheck(ctx.state, where, now, ctx.cfg);
  if (!budget.ok) {
    audit({ evt: "budget_blocked", kind: "discussion", speaker, reason: budget.reason });
    // A continuation that hits a cap just ends the exchange silently; only a
    // fresh (human/third-party) trigger gets the visible refusal.
    if (!decision.continuation) {
      await post(ctx, {
        agent: INFRA_IDENTITY,
        channel: msg.channel,
        threadTs: msg.ts,
        text: `:no_entry: run limit: ${budget.reason}`,
      });
    }
    return;
  }
  budgetRecord(ctx.state, where, now);
  await post(ctx, {
    agent: INFRA_IDENTITY,
    channel: msg.channel,
    threadTs: msg.ts,
    text: `:hourglass_flowing_sand: ${speaker} is reading the thread`,
  });
  enqueue({
    kind: "discussion",
    channel: msg.channel,
    ts: msg.ts,
    rootTs,
    text: decision.prompt,
    identity: decision.identity,
    speaker,
    debate: where.debate,
    continuation: Boolean(decision.continuation),
    ctx,
  });
}

async function handleTriage(msg, decision, ctx) {
  const now = nowSec();
  const budget = budgetCheck(ctx.state, { channel: msg.channel, rootTs: msg.ts }, now, ctx.cfg);
  if (!budget.ok) {
    audit({ evt: "budget_blocked", kind: "triage", reason: budget.reason });
    await post(ctx, {
      agent: INFRA_IDENTITY,
      channel: msg.channel,
      threadTs: msg.ts,
      text: `:no_entry: triage run limit: ${budget.reason}`,
    });
    return;
  }
  budgetRecord(ctx.state, { channel: msg.channel, rootTs: msg.ts }, now);
  enqueue({
    kind: "triage",
    channel: msg.channel,
    ts: msg.ts,
    rootTs: msg.ts,
    text: msg.text,
    identity: decision.identity,
    speaker: RESPONDER,
    debate: false,
    continuation: false,
    ctx,
  });
}

async function handleEventsApi(envelope, ctx) {
  const payload = envelope.payload || {};
  if (payload.type !== "event_callback") return;
  const msg = normalizeEvent(payload.event || {});
  msg.eventId = payload.event_id || "";
  msg.eventTime = Number(payload.event_time) || 0;

  const decision = shouldHandle(msg, ctx.cfg, ctx, nowSec(), seenSet(ctx.state));
  if (decision.kind === "ignore") {
    if (!["channel", "duplicate", "stale"].includes(decision.reason)) {
      audit({ evt: "ignored", reason: decision.reason, channel: msg.channel, ts: msg.ts });
    }
    return;
  }
  markSeen(ctx.state, msg);
  if (decision.kind === "control") await handleControl(msg, decision, ctx);
  else if (decision.kind === "discussion") await handleDiscussion(msg, decision, ctx);
  else if (decision.kind === "triage") await handleTriage(msg, decision, ctx);
  persist(ctx.state);
}

// ---------------------------------------------------------------------------
// socket mode session
// ---------------------------------------------------------------------------

async function slackCall(token, method, params) {
  try {
    const response = await fetch(`${SLACK_API}/${method}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams(params || {}),
      signal: AbortSignal.timeout(15_000),
    });
    return await response.json();
  } catch (err) {
    return { ok: false, error: `network: ${err.message || err}` };
  }
}

function runSocketSession(url, ctx, onHealthy) {
  return new Promise((resolve) => {
    const ws = new WebSocket(url);
    let settled = false;
    let refreshTimer = null;
    let helloTimer = null;
    const clearTimers = () => {
      if (refreshTimer) clearTimeout(refreshTimer);
      if (helloTimer) clearTimeout(helloTimer);
      refreshTimer = null;
      helloTimer = null;
    };
    const finish = (reason) => {
      if (settled) return;
      settled = true;
      clearTimers();
      try {
        ws.close();
      } catch {
        // already closed
      }
      resolve({ reason });
    };
    // Slack sends `hello` right after connect; a half-open socket that never
    // delivers one would otherwise stay "active but deaf" (no close event).
    helloTimer = setTimeout(() => finish("no_hello"), 30_000);

    ws.addEventListener("open", () => {
      console.log("slack-bridge: socket connected");
      audit({ evt: "socket_open" });
    });
    ws.addEventListener("message", (event) => {
      if (settled) return; // late envelopes from a socket being replaced
      let envelope;
      try {
        envelope = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (envelope.type === "hello") {
        if (helloTimer) clearTimeout(helloTimer);
        helloTimer = null;
        onHealthy();
        const connectionTime = Number(envelope.approximate_connection_time) || 600;
        audit({ evt: "hello", connection_time: connectionTime });
        if (refreshTimer) clearTimeout(refreshTimer);
        refreshTimer = setTimeout(
          () => finish("proactive"),
          Math.max(60, Math.floor(connectionTime * 0.9)) * 1000,
        );
        return;
      }
      if (envelope.type === "disconnect") {
        audit({ evt: "slack_disconnect", reason: envelope.reason || "" });
        finish("slack_disconnect");
        return;
      }
      if (envelope.type === "events_api") {
        if (envelope.envelope_id) {
          try {
            ws.send(JSON.stringify({ envelope_id: envelope.envelope_id }));
          } catch (err) {
            audit({ evt: "ack_failed", error: String(err) });
          }
        }
        handleEventsApi(envelope, ctx).catch((err) => {
          audit({ evt: "handler_crash", error: String(err) });
        });
        return;
      }
      // interactive/slash_commands are not subscribed; ackless types are dropped
    });
    ws.addEventListener("close", () => finish("closed"));
    ws.addEventListener("error", () => finish("error"));
  });
}

function readTextFile(path) {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

async function main() {
  // The goal supervisor's path: guarded dispatch with no Slack config required
  // (it never posts). Handled first so a box without slack.env can still drive
  // a worker, and so it can never fall into the socket loop.
  const argv = process.argv.slice(2);
  const dispatchAt = argv.indexOf("--dispatch");
  if (dispatchAt > -1) {
    const result = await dispatchCli(argv.slice(dispatchAt + 1));
    console.log(JSON.stringify(result));
    process.exitCode = result.ok ? 0 : Math.max(1, Number(result.code) || 1);
    return;
  }

  const flagIndex = process.argv.indexOf("--env-file");
  const envFile = flagIndex > -1 ? process.argv[flagIndex + 1] : DEFAULT_ENV_FILE;

  const problems = validateConfig(loadConfig(readTextFile(envFile)));
  if (problems.length) {
    console.error(`slack-bridge: not configured (${envFile}): ${problems.join("; ")}`);
    process.exitCode = 1;
    return;
  }

  const agents = loadRegistry();
  const state = loadState();
  const ctx = { cfg: null, agents, botUserId: "", botId: "", envFile, state };
  const cfg0 = loadConfig(readTextFile(envFile));
  console.log(
    `slack-bridge: starting (${Object.keys(agents).length} agents, triage=${cfg0.triage ? "on" : "off"}, debate=${cfg0.debate ? "on" : "off"})`,
  );
  const me = await slackCall(cfg0.botToken, "auth.test", {});
  if (!me.ok) {
    console.error(`slack-bridge: auth.test failed: ${me.error}`);
    process.exitCode = 1;
    return;
  }
  ctx.botUserId = me.user_id || "";
  ctx.botId = me.bot_id || "";
  console.log(`slack-bridge: auth.test ok user=${me.user_id} bot=${me.bot_id || "-"}`);
  if (!ctx.botId) {
    console.error(
      "slack-bridge: auth.test returned no bot_id — agent-identity checks fail closed",
    );
  }

  for (const signal of ["SIGTERM", "SIGINT"]) {
    process.on(signal, () => {
      audit({ evt: "stop", signal });
      process.exit(0);
    });
  }

  let backoff = 1000;
  for (;;) {
    let cfg;
    try {
      cfg = loadConfig(readTextFile(envFile));
      const reloadProblems = validateConfig(cfg);
      if (reloadProblems.length) throw new Error(reloadProblems.join("; "));
    } catch (err) {
      console.error(`slack-bridge: env reload failed: ${err.message}`);
      await sleep(backoff);
      backoff = Math.min(backoff * 2, 60_000);
      continue;
    }
    ctx.cfg = cfg;
    try {
      const opened = await slackCall(cfg.appToken, "apps.connections.open", {});
      if (!opened.ok || !opened.url) {
        throw new Error(`connections.open: ${opened.error || "no url"}`);
      }
      const session = await runSocketSession(opened.url, ctx, () => {
        backoff = 1000;
      });
      console.log(`slack-bridge: session ended (${session.reason})`);
      if (session.reason === "proactive") continue;
      if (session.reason === "slack_disconnect") {
        await sleep(1000);
        continue;
      }
      throw new Error(`socket_${session.reason}`);
    } catch (err) {
      audit({ evt: "reconnect", error: String(err.message || err), backoff_ms: backoff });
      await sleep(backoff);
      backoff = Math.min(backoff * 2, 60_000);
    }
  }
}

const isMain = (() => {
  if (!process.argv[1]) return false;
  try {
    // realpath: under a symlinked deploy argv[1] differs from import.meta.url
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
})();

if (isMain) {
  main().catch((err) => {
    console.error(`slack-bridge: fatal: ${err && err.stack ? err.stack : err}`);
    process.exit(1);
  });
}
