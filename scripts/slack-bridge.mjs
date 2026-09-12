#!/usr/bin/env node
// Slack Socket Mode bridge for the agent community.
//
// Listens on a websocket (outbound only; no tunnel, no inbound firewall
// change) and applies a per-channel policy to message.channels events:
//   #control  bot mention from an allowlisted member -> built-in or qoder run
//   #lobby    agent-identity post -> qoder discussion reply in-thread
//             (humans only when allowlisted AND prefixed "qoder:")
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
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_ENV_FILE = join(homedir(), ".config/remote-agent/slack.env");
const STATE_FILE = join(homedir(), ".local/state/remote-agent/slack-bridge-state.json");
const AUDIT_LOG = "/work/logs/slack-bridge.log";
const SETTINGS_PATH = "/usr/local/share/remote-agent/slack-executor-settings.json";
const DEFAULT_EXECUTOR_BIN = join(homedir(), ".local/bin/qoder-efficient");
const BOX_REGISTRY = "/usr/local/share/remote-agent/slack-agents.json";
const REPO_REGISTRY = join(HERE, "..", "config", "slack-agents.json");
const SLACK_API = "https://slack.com/api";

export const RESPONDER = "qoder"; // identity used for replies
export const INFRA_IDENTITY = "bridge"; // excluded from #lobby triggers (brief, acks)
const HUMAN_PREFIX = /^qoder:\s*/i;
const RECOVERY_PREFIX = ":white_check_mark:";
const ALLOWED_SUBTYPES = ["bot_message", "thread_broadcast"];
const EVENT_MAX_AGE_SEC = 300;
export const REPLY_CHUNK = 3000;
const EXECUTOR_TIMEOUT_MS = 300_000;
export const THREAD_RUNS_PER_HOUR = 4;
export const RATE_MAX = 6;
export const RATE_WINDOW_SEC = 600;
export const RATE_MIN_GAP_SEC = 10;
const PROMPT_MAX = 2000;
const THREAD_CONTEXT_MAX_MESSAGES = 15;
const THREAD_CONTEXT_MAX_CHARS = 12_000;
const THREAD_CONTEXT_MSG_CHARS = 1500;
const SEEN_MAX = 2000;

const HELP_TEXT =
  "commands: `@spectre-agents ping` | `status` | `help` | any question " +
  "(qoder runs read-only, rate-limited, ~1-3 min)";

const BOX_FACTS = [
  "Context: you are running headless on the Spectre XT worker box (Debian 13, user person),",
  "inside the Spectre agent community in Slack.",
  "Services: orca-serve (user unit, :6768), hardened-zai-proxy (:18088, optional),",
  "ZCode (Electron GUI app, cannot run headless), qodercli (you, via the Efficient model).",
  "Logs: /work/logs/health.log and journald.",
  "You have a read-only tool allowlist; unlisted tools fail closed.",
  "Never ask for or print secrets. Reply in plain text suitable for a Slack message.",
].join("\n");

const PERSONAS = {
  control:
    "The operator asked something in #control. Investigate read-only and reply " +
    "with a concise, concrete answer (no more than 2000 characters).",
  discussion:
    "You are qoder in #lobby, the commons where agents share issues and findings. " +
    "Join the thread with a concise, concrete reply (no more than 2000 characters). " +
    "Disagree when the evidence says so; do not pad.",
  triage:
    "A healthcheck alert was posted to #alerts. Triage it read-only: likely cause, " +
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
    maxRunsPerDay: Math.max(1, Math.floor(numberOr(env.SLACK_MAX_RUNS_PER_DAY, 30))),
    // qodercli "efficient" is the 0-multiplier model; the wrapper adds the
    // billing guard (refuses to start, exit 75, if the promo price changed).
    executorBin: expandHome((env.SLACK_EXECUTOR_BIN || "").trim()) || DEFAULT_EXECUTOR_BIN,
    executorModel: (env.SLACK_EXECUTOR_MODEL || "efficient").trim(),
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
      return {
        kind: "discussion",
        identity: "human",
        prompt: msg.text.replace(HUMAN_PREFIX, "").trim().slice(0, PROMPT_MAX),
      };
    }
    const identity = agentIdentity(msg, ctx.agents, ctx.botId);
    if (!identity) return { kind: "ignore", reason: "lobby_unknown_identity" };
    if (identity === RESPONDER || identity === INFRA_IDENTITY) {
      return { kind: "ignore", reason: "lobby_self" };
    }
    // Single-hop by construction: agent posts only trigger at top level;
    // humans continue threads with the qoder: prefix.
    if (msg.threadTs && msg.threadTs !== msg.ts) {
      return { kind: "ignore", reason: "lobby_thread_reply" };
    }
    return { kind: "discussion", identity, prompt: msg.text.slice(0, PROMPT_MAX) };
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

export function classifyCommand(text) {
  const command = String(text || "").trim();
  const words = command.split(/\s+/);
  const first = (words[0] || "").toLowerCase();
  if (words.length === 1 && ["status", "ping", "help"].includes(first)) {
    return { builtin: first, prompt: "" };
  }
  return { builtin: null, prompt: command.slice(0, PROMPT_MAX) };
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
}

export function budgetCheck(state, where, now, cfg) {
  if (state.daily.date === dayKey(now) && state.daily.runs >= cfg.maxRunsPerDay) {
    return { ok: false, reason: "daily_cap" };
  }
  const key = `${where.channel}:${where.rootTs}`;
  const recent = (state.threads[key] || []).filter(
    (t) => Number.isFinite(t) && withinWindow(t, now, 3600),
  );
  if (recent.length >= THREAD_RUNS_PER_HOUR) return { ok: false, reason: "thread_cap" };
  return { ok: true };
}

export function budgetRecord(state, where, now) {
  const day = dayKey(now);
  if (state.daily.date !== day) state.daily = { date: day, runs: 0 };
  state.daily.runs += 1;
  const key = `${where.channel}:${where.rootTs}`;
  const recent = (state.threads[key] || []).filter(
    (t) => Number.isFinite(t) && withinWindow(t, now, 3600),
  );
  recent.push(now);
  state.threads[key] = recent;
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
  return { daily: { date: "", runs: 0 }, threads: {}, rate: {}, seen: {} };
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
        };
      }
      const timestampList = (value) =>
        Array.isArray(value) ? value.filter((t) => isTimestamp(t)).map((t) => Number(t)) : [];
      const threads = numberMap(parsed.threads, Array.isArray);
      for (const [key, value] of Object.entries(threads)) {
        state.threads[key] = timestampList(value);
      }
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
// executor (qodercli, Efficient model) — read-only by construction
//
// Verified against qodercli 1.1.47 on the box: writes always require
// confirmation and are denied headless; compound commands (`;`, `&&`, `|`)
// are split into segments and each is checked — a denied or write segment
// denies the whole command; command substitution `$(...)` is denied
// outright. `cwd: "/"` puts every read inside the workspace root so the
// read/Glob deny list applies to box paths. Flag-level PreToolUse
// hooks are NOT executed by qodercli (probed: a logging hook never ran),
// so the executable boundary is the permission engine + the narrow
// allowlist in slack-executor-settings.json (plus the engine's internal
// read-only safe list, e.g. wc/ls), not the guard hook.
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
    "--permission-mode",
    "default",
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

function runExecutor({ prompt, systemPrompt, cfg }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    const args = buildExecutorArgs({
      prompt,
      systemPrompt,
      settingsPath: SETTINGS_PATH,
      model: cfg.executorModel,
    });
    const child = spawn(cfg.executorBin, args, {
      env: childEnv(),
      cwd: "/", // in-workspace reads: box paths filtered by the deny list
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
      const parsed = parseExecutorResult(stdout, code);
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
  audit({ evt: "job_queued", kind: job.kind, channel: job.channel });
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

function systemPromptFor(kind) {
  return `${BOX_FACTS}\n\n${PERSONAS[kind] || PERSONAS.control}`;
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
  parts.push("", PERSONAS[job.kind] || PERSONAS.control);
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
  audit({ evt: "job_start", kind: job.kind, channel: job.channel, ts: job.ts });
  const threadContext = await fetchThreadContext(ctx.cfg.botToken, job.channel, job.rootTs);
  const result = await runExecutor({
    prompt: composePrompt(job, threadContext),
    systemPrompt: systemPromptFor(job.kind),
    cfg: ctx.cfg,
  });
  if (!result.ok) {
    audit({ evt: "job_failed", kind: job.kind, error: result.error });
    const tail = String(result.stderr || "").trim().slice(-300);
    const suffix = tail ? `\n\`\`\`\n${tail}\n\`\`\`` : "";
    await post(ctx, {
      agent: INFRA_IDENTITY,
      channel: job.channel,
      threadTs: job.ts,
      text: `:warning: qoder run failed (${result.error})${suffix}`,
    });
    return;
  }
  await postChunks(ctx, {
    agent: RESPONDER,
    channel: job.channel,
    threadTs: job.ts,
    text: result.text,
  });
  audit({ evt: "job_done", kind: job.kind, chars: result.text.length });
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

  const { builtin, prompt } = classifyCommand(decision.command);
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
    text: ":hourglass_flowing_sand: on it — qoder is running (read-only)",
  });
  enqueue({
    kind: "control",
    channel: msg.channel,
    ts: msg.ts,
    rootTs: msg.ts,
    text: prompt,
    identity: msg.user,
    ctx,
  });
}

async function handleDiscussion(msg, decision, ctx) {
  const now = nowSec();
  const rootTs = msg.threadTs || msg.ts;
  const budget = budgetCheck(ctx.state, { channel: msg.channel, rootTs }, now, ctx.cfg);
  if (!budget.ok) {
    audit({ evt: "budget_blocked", kind: "discussion", reason: budget.reason });
    await post(ctx, {
      agent: INFRA_IDENTITY,
      channel: msg.channel,
      threadTs: msg.ts,
      text: `:no_entry: run limit: ${budget.reason}`,
    });
    return;
  }
  budgetRecord(ctx.state, { channel: msg.channel, rootTs }, now);
  await post(ctx, {
    agent: INFRA_IDENTITY,
    channel: msg.channel,
    threadTs: msg.ts,
    text: ":hourglass_flowing_sand: qoder is reading the thread",
  });
  enqueue({
    kind: "discussion",
    channel: msg.channel,
    ts: msg.ts,
    rootTs,
    text: decision.prompt,
    identity: decision.identity,
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
    `slack-bridge: starting (${Object.keys(agents).length} agents, triage=${cfg0.triage ? "on" : "off"})`,
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
