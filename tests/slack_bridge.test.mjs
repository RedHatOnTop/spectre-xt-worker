#!/usr/bin/env node
// Unit tests for the Slack Socket Mode bridge policy and guards.
import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  agentIdentity,
  budgetCheck,
  budgetRecord,
  buildAgyArgs,
  buildAgyPrompt,
  buildExecutorArgs,
  chunkText,
  classifyCommand,
  classifyMessage,
  controlDispatchArgs,
  dayKey,
  debateMerged,
  debateMergeRecord,
  astraPidVerdict,
  dispatchAction,
  dispatchAllowed,
  dispatchLine,
  flashSendLine,
  flashReady,
  freeProxyReady,
  flashPin,
  finalizeFlashInjection,
  injectionLine,
  ensurePacketPin,
  startPacketJob,
  packetSurface,
  packetShellTitle,
  packetJobTitle,
  applyPacketPin,
  pinTreeHasDsh,
  writeFlashPacket,
  formatThreadContext,
  isPassText,
  isRecoveryText,
  loadConfig,
  normalizeEvent,
  oneLine,
  otherResponder,
  paneRefusal,
  paneTarget,
  parseDispatchArgs,
  parseAgyResult,
  parseExecutorResult,
  pickNativeTerminal,
  rateCheck,
  rateRecord,
  shouldHandle,
  speakerReady,
  validateConfig,
} from "../scripts/slack-bridge.mjs";

const NOW = 1_800_000_000; // 2027-01-15T08:00:00Z

const CHANNELS = {
  alerts: "C01ALERTS0",
  fleet: "C01FLEET00",
  control: "C01CONTROL",
  lobby: "C01LOBBY00",
};

const ENV_TEXT = [
  "# comment",
  "SLACK_BOT_TOKEN=xoxb-aaaaaaaaaaaaaaaaaaaaaa",
  "SLACK_APP_TOKEN=xapp-bbbbbbbbbbbbbbbbbbbbbb",
  "SLACK_CHANNEL_ALERTS=C01ALERTS0",
  "SLACK_CHANNEL_FLEET=C01FLEET00",
  "SLACK_CHANNEL_CONTROL=C01CONTROL",
  "SLACK_CHANNEL_LOBBY=C01LOBBY00",
  "SLACK_ALLOWED_USERS=U0PERSON1,U0PERSON2",
  "SLACK_TRIAGE=1",
  "SLACK_MAX_RUNS_PER_DAY=7",
].join("\n");

function goodCfg(over = {}) {
  return {
    botToken: "xoxb-aaaaaaaaaaaaaaaaaaaaaa",
    appToken: "xapp-bbbbbbbbbbbbbbbbbbbbbb",
    channels: { ...CHANNELS },
    allowedUsers: ["U0PERSON1"],
    triage: true,
    debate: false,
    maxRunsPerDay: 30,
    threadRunsPerHour: 4,
    threadTurnsPerDay: 8,
    debateMaxRunsPerDay: 20,
    executorBin: "/home/person/.local/bin/qoder-efficient",
    executorModel: "efficient",
    agyBin: "/home/person/.local/bin/agy",
    ...over,
  };
}

const AGENTS = {
  bridge: {},
  healthcheck: {},
  orca: {},
  zcode: {},
  claude: {},
  qoder: {},
  spectre: {},
  antigravity: {},
};
const CTX = { botUserId: "U0BOT", botId: "B1", agents: AGENTS };

function message(over = {}) {
  return Object.assign(normalizeEvent({}), over);
}

function freshState() {
  return {
    daily: { date: "", runs: 0, debate: 0 },
    threads: {},
    threadDays: {},
    merge: {},
    rate: {},
    seen: {},
  };
}

test("loadConfig parses the env text and applies defaults", () => {
  const cfg = loadConfig(ENV_TEXT);
  assert.equal(cfg.botToken, "xoxb-aaaaaaaaaaaaaaaaaaaaaa");
  assert.deepEqual(cfg.allowedUsers, ["U0PERSON1", "U0PERSON2"]);
  assert.equal(cfg.triage, true);
  assert.equal(cfg.maxRunsPerDay, 7);
  assert.equal(cfg.executorModel, "efficient");
  assert.equal(cfg.executorBin, join(homedir(), ".local/bin/qoder-efficient"));
  assert.equal(loadConfig("SLACK_EXECUTOR_BIN=~/bin/x").executorBin, join(homedir(), "bin/x"));
  assert.equal(loadConfig("SLACK_EXECUTOR_BIN=~").executorBin, homedir());
  // "~user" and absolute paths must pass through untouched
  assert.equal(loadConfig("SLACK_EXECUTOR_BIN=~root/bin/x").executorBin, "~root/bin/x");
  assert.equal(loadConfig("SLACK_EXECUTOR_BIN=/usr/bin/true").executorBin, "/usr/bin/true");
  assert.equal(cfg.channels.lobby, "C01LOBBY00");
});

test("validateConfig reports every missing piece, accepts a full config", () => {
  assert.ok(validateConfig(loadConfig("")).length >= 6);
  assert.deepEqual(validateConfig(goodCfg()), []);
  const relative = validateConfig(goodCfg({ executorBin: "qoder-efficient" }));
  assert.ok(relative.some((problem) => problem.includes("SLACK_EXECUTOR_BIN")));
});

test("classifyMessage: #fleet never triggers anything", () => {
  const decision = classifyMessage(
    message({ channel: CHANNELS.fleet, ts: "1.1", botId: "B1", username: "orca", text: "hi" }),
    goodCfg(),
    CTX,
  );
  assert.deepEqual(decision, { kind: "ignore", reason: "fleet_posts_only" });
});

test("classifyMessage: #control allowlisted mention becomes a command", () => {
  const decision = classifyMessage(
    message({
      channel: CHANNELS.control,
      ts: "1.1",
      user: "U0PERSON1",
      text: "<@U0BOT> check the proxy",
    }),
    goodCfg(),
    CTX,
  );
  assert.equal(decision.kind, "control");
  assert.equal(decision.command, "check the proxy");
});

test("classifyMessage: #control accepts the |name mention form", () => {
  const decision = classifyMessage(
    message({
      channel: CHANNELS.control,
      ts: "1.1",
      user: "U0PERSON1",
      text: "<@U0BOT|spectre-agents> ping",
    }),
    goodCfg(),
    CTX,
  );
  assert.equal(decision.kind, "control");
  assert.equal(decision.command, "ping");
});

test("classifyMessage: #control rejects non-allowlisted, unmentioned, bot posts", () => {
  const cfg = goodCfg();
  assert.equal(
    classifyMessage(
      message({ channel: CHANNELS.control, ts: "1.1", user: "U0OTHER", text: "<@U0BOT> ls" }),
      cfg,
      CTX,
    ).reason,
    "control_not_allowed",
  );
  assert.equal(
    classifyMessage(
      message({ channel: CHANNELS.control, ts: "1.2", user: "U0PERSON1", text: "hello" }),
      cfg,
      CTX,
    ).reason,
    "control_no_mention",
  );
  assert.equal(
    classifyMessage(
      message({ channel: CHANNELS.control, ts: "1.3", botId: "B1", username: "orca", text: "<@U0BOT> ls" }),
      cfg,
      CTX,
    ).reason,
    "control_bot",
  );
});

test("classifyMessage: #alerts triages only healthcheck posts, never recoveries", () => {
  const cfg = goodCfg();
  const alert = message({
    channel: CHANNELS.alerts,
    ts: "2.1",
    botId: "B1",
    username: "healthcheck",
    subtype: "bot_message",
    text: "proxy_down",
  });
  assert.equal(classifyMessage(alert, cfg, CTX).kind, "triage");
  assert.equal(
    classifyMessage(
      message({ ...alert, text: ":white_check_mark: recovered" }),
      cfg,
      CTX,
    ).reason,
    "alerts_recovery",
  );
  assert.equal(
    classifyMessage(message({ ...alert, username: "orca" }), cfg, CTX).reason,
    "alerts_not_healthcheck",
  );
  assert.equal(
    classifyMessage(message({ ...alert, botId: "", username: "", user: "U0PERSON1" }), cfg, CTX).reason,
    "alerts_not_healthcheck",
  );
  assert.equal(
    classifyMessage(alert, goodCfg({ triage: false }), CTX).reason,
    "triage_disabled",
  );
});

test("classifyMessage: #lobby answers agent posts, not its own or unknown bots", () => {
  const cfg = goodCfg();
  const orca = message({
    channel: CHANNELS.lobby,
    ts: "3.1",
    botId: "B1",
    username: "orca",
    subtype: "bot_message",
    text: "two daemon restarts today — anyone know why?",
  });
  const decision = classifyMessage(orca, cfg, CTX);
  assert.equal(decision.kind, "discussion");
  assert.equal(decision.identity, "orca");

  assert.equal(
    classifyMessage(message({ ...orca, username: "qoder" }), cfg, CTX).reason,
    "lobby_self",
  );
  assert.equal(
    classifyMessage(message({ ...orca, username: "bridge" }), cfg, CTX).reason,
    "lobby_self",
  );
  assert.equal(
    classifyMessage(message({ ...orca, username: "karma-bot" }), cfg, CTX).reason,
    "lobby_unknown_identity",
  );
  assert.equal(
    classifyMessage(message({ ...orca, threadTs: "3.1", ts: "3.2" }), cfg, CTX).reason,
    "lobby_thread_reply",
  );
});

test("classifyMessage: #lobby humans need the qoder: prefix and the allowlist", () => {
  const cfg = goodCfg();
  const human = message({
    channel: CHANNELS.lobby,
    ts: "3.3",
    user: "U0PERSON1",
    text: "just chatting",
  });
  assert.equal(classifyMessage(human, cfg, CTX).reason, "lobby_human_unprefixed");
  const prefixed = classifyMessage(message({ ...human, text: "Qoder: why is proxy down?" }), cfg, CTX);
  assert.equal(prefixed.kind, "discussion");
  assert.equal(prefixed.identity, "human");
  assert.equal(prefixed.prompt, "why is proxy down?");
  // A member outside SLACK_ALLOWED_USERS must not drive the executor.
  assert.equal(
    classifyMessage(
      message({ ...human, user: "U0OTHER", text: "qoder: read the proxy env" }),
      cfg,
      CTX,
    ).reason,
    "lobby_human_not_allowed",
  );
});

test("shouldHandle drops stale, duplicate, and edited events", () => {
  const cfg = goodCfg();
  const orca = message({
    channel: CHANNELS.lobby,
    ts: "4.1",
    botId: "B1",
    username: "orca",
    text: "hi",
    eventId: "Ev1",
    eventTime: 1000,
  });
  assert.equal(shouldHandle(orca, cfg, CTX, 1000 + 400, new Set()).reason, "stale");
  assert.equal(shouldHandle(orca, cfg, CTX, 1000, new Set(["e:Ev1"])).reason, "duplicate");
  assert.equal(
    shouldHandle(orca, cfg, CTX, 1000, new Set([`m:${CHANNELS.lobby}:4.1`])).reason,
    "duplicate",
  );
  const edited = message({ ...orca, subtype: "message_changed" });
  assert.equal(shouldHandle(edited, cfg, CTX, 1000, new Set()).reason, "subtype:message_changed");
  assert.equal(shouldHandle(message({ ...orca, ts: "" }), cfg, CTX, 1000, new Set()).reason, "malformed");
});

test("shouldHandle keeps customized bot posts (subtype bot_message survives)", () => {
  const alert = message({
    channel: CHANNELS.alerts,
    ts: "5.1",
    botId: "B1",
    username: "healthcheck",
    subtype: "bot_message",
    text: "proxy_down",
    eventTime: 1000,
  });
  assert.equal(shouldHandle(alert, goodCfg(), CTX, 1000, new Set()).kind, "triage");
});

test("classifyCommand: builtins only when the command is bare", () => {
  assert.equal(classifyCommand("ping").builtin, "ping");
  assert.equal(classifyCommand("STATUS").builtin, "status");
  assert.equal(classifyCommand("help").builtin, "help");
  assert.equal(classifyCommand("status now").builtin, null);
  assert.equal(classifyCommand("why is the proxy down?").prompt, "why is the proxy down?");
  assert.equal(classifyCommand("x".repeat(3000)).prompt.length, 2000);
});

test("classifyCommand: ChatGPT attribution trailers are stripped", () => {
  // Synced from the box's live bridge 2026-09-16; three observed forms.
  assert.equal(classifyCommand("*Sent using ChatGPT*").prompt, "");
  assert.equal(classifyCommand("ping *Sent using ChatGPT*").builtin, "ping");
  assert.equal(
    classifyCommand("help *Sent using* <@U0C0XE1NCLF|ChatGPT>").builtin,
    "help",
  );
  assert.equal(
    classifyCommand("status _Sent using_ <@U0C0XE1NCLF|ChatGPT>").builtin,
    "status",
  );
  // Mid-message mentions are real text, not a trailer.
  assert.equal(
    classifyCommand("was this Sent using ChatGPT or a person?").prompt,
    "was this Sent using ChatGPT or a person?",
  );
});

test("classifyCommand: goal/resume dispatch parsing", () => {
  assert.deepEqual(classifyCommand("goal qoder fix the parser"), {
    builtin: "goal",
    worker: "qoder",
    prompt: "fix the parser",
  });
  const upper = classifyCommand("GOAL Qoder Fix The Parser");
  assert.equal(upper.worker, "qoder");
  assert.equal(upper.prompt, "Fix The Parser");
  assert.deepEqual(classifyCommand("goal"), { builtin: "goal", worker: null, prompt: "" });
  assert.deepEqual(classifyCommand("goal qoder"), { builtin: "goal", worker: "qoder", prompt: "" });
  assert.deepEqual(classifyCommand("resume pugc"), { builtin: "resume", worker: "pugc", prompt: "" });
  assert.deepEqual(classifyCommand("RESUME Pugc"), { builtin: "resume", worker: "pugc", prompt: "" });
  assert.equal(classifyCommand("goal w " + "x".repeat(3000)).prompt.length, 2000);
  // Over-long resume tails are a usage error, not a qoder question.
  assert.deepEqual(classifyCommand("resume a b"), { builtin: "resume", worker: null, prompt: "" });
  assert.deepEqual(classifyCommand("resume"), { builtin: "resume", worker: null, prompt: "" });
  // A hyphenated word does not trigger dispatch; it stays a qoder question.
  assert.equal(classifyCommand("goal-setting advice for the team").builtin, null);
  // Trailing words after the worker are a usage error, never a qoder question.
  assert.equal(classifyCommand("resume the investigation").worker, null);
});

test("classifyCommand: strips the ChatGPT Slack attribution trailer", () => {
  assert.equal(classifyCommand("help\n*Sent using ChatGPT*").builtin, "help");
  assert.equal(classifyCommand("ping *Sent using ChatGPT*").builtin, "ping");
  assert.equal(classifyCommand("resume zzbrush\n*Sent using ChatGPT*").worker, "zzbrush");
  const goal = classifyCommand("goal qoder fix the parser\n*Sent using ChatGPT*");
  assert.equal(goal.worker, "qoder");
  assert.equal(goal.prompt, "fix the parser");
});

test("controlDispatchArgs: Slack dispatch carries the classified worker", () => {
  // The message decision has no worker/prompt fields of its own; reading the
  // dispatch target from it (instead of classifyCommand) silently broke every
  // Slack goal/resume — usage reply for any worker.
  assert.deepEqual(controlDispatchArgs({ kind: "control", command: "resume zzbrush" }), {
    builtin: "resume",
    worker: "zzbrush",
    prompt: "",
  });
  assert.deepEqual(controlDispatchArgs({ kind: "control", command: "goal pugc fix the tests" }), {
    builtin: "goal",
    worker: "pugc",
    prompt: "fix the tests",
  });
  assert.equal(controlDispatchArgs({ kind: "control", command: "why is the proxy down?" }).builtin, null);
});

test("dispatchAction: plan is not a goal inject", () => {
  assert.equal(dispatchAction("plan"), "plan");
  assert.equal(dispatchAction("goal"), "dispatch_goal");
  assert.equal(dispatchAction("resume"), "resume");
});

test("dispatchAllowed: policy flags only, fail-closed without policy", () => {
  const closed = dispatchAllowed({ state: "idle" });
  assert.equal(closed.ok, false);
  assert.match(closed.detail, /no policy/);
  assert.equal(dispatchAllowed(undefined).ok, false);

  const running = dispatchAllowed({
    goal: { state: "RUNNING" },
    policy: { can_dispatch_goal: false, can_resume: false },
  });
  assert.equal(running.ok, false);

  const idle = dispatchAllowed({
    goal: { state: "IDLE" },
    policy: { can_dispatch_goal: true, can_resume: false },
  }, "dispatch_goal");
  assert.equal(idle.ok, true);
  assert.equal(
    dispatchAllowed({
      goal: { state: "IDLE" },
      policy: { can_dispatch_goal: true, can_resume: false },
    }, "resume").ok,
    false,
  );

  const budget = dispatchAllowed({
    goal: { state: "PARKED", park_reason: "goal_budget" },
    policy: { can_dispatch_goal: false, can_resume: true },
  }, "resume");
  assert.equal(budget.ok, true);
  assert.equal(
    dispatchAllowed({
      goal: { state: "PARKED", park_reason: "goal_budget" },
      policy: { can_dispatch_goal: false, can_resume: true },
    }, "dispatch_goal").ok,
    false,
  );

  const planOk = dispatchAllowed({
    goal: { state: "COMPLETED" },
    policy: { grokbot_may_advance: true, can_dispatch_goal: true },
  }, "plan");
  assert.equal(planOk.ok, true);
  assert.equal(
    dispatchAllowed({
      goal: { state: "RUNNING" },
      policy: { grokbot_may_advance: false, can_dispatch_goal: false },
    }, "plan").ok,
    false,
  );

  const gate = dispatchAllowed({
    goal: { state: "PARKED", park_reason: "plan_gate" },
    policy: { can_dispatch_goal: false, can_resume: false },
  }, "resume");
  assert.equal(gate.ok, false);
  assert.match(gate.detail, /ExitPlanMode/);
});

test("paneRefusal: shells and foreign cwds are refused", () => {
  assert.match(paneRefusal("bash", "/home/person/Projects/orca-rust", "/home/person/Projects/orca-rust"), /shell/);
  assert.match(paneRefusal("-zsh", "/x", "/x"), /shell/);
  assert.equal(paneRefusal("node", "/home/person/Projects/orca-rust", "/home/person/Projects/orca-rust"), null);
  assert.match(paneRefusal("node", "/home/person/Projects/other", "/home/person/Projects/orca-rust"), /pane cwd/);
  // Unknown pane path: the shell check still applies, the cwd check abstains.
  assert.match(paneRefusal("node", "", "/home/person/Projects/orca-rust"), /missing/);
  assert.equal(paneRefusal("node", undefined, undefined), null);
});

test("paneTarget: bare names become session targets, explicit targets pass through", () => {
  // Bare "qoder" hit the pugc pane (window name "qodercli" prefix match).
  assert.equal(paneTarget("qoder"), "qoder:");
  assert.equal(paneTarget("pugc"), "pugc:");
  assert.equal(paneTarget("qoder:0.0"), "qoder:0.0");
  assert.equal(paneTarget("sess:1"), "sess:1");
  assert.equal(paneTarget("%6"), "%6");
  assert.equal(paneTarget("@1"), "@1");
  assert.equal(paneTarget(""), "");
  assert.equal(paneTarget(undefined), "");
});

test("oneLine: collapses whitespace so the payload is one send-keys literal", () => {
  assert.equal(oneLine("line one\nline two\t indented"), "line one line two indented");
  assert.equal(oneLine("  padded  "), "padded");
  assert.equal(oneLine(undefined), "");
});

test("buildExecutorArgs: isolated, never skips permissions, deny list governs", () => {
  const args = buildExecutorArgs({
    prompt: "hello",
    systemPrompt: "facts",
    settingsPath: "/usr/local/share/remote-agent/slack-executor-settings.json",
    model: "efficient",
  });
  assert.equal(args[args.indexOf("-p") + 1], "hello");
  // acceptEdits (2026-09-16), not default: default keeps the headless write
  // gate shut and the Write/Edit tools refuse. Still not a skip-permissions
  // mode — the assertions below pin that.
  assert.equal(args[args.indexOf("--permission-mode") + 1], "acceptEdits");
  assert.equal(args[args.indexOf("--setting-sources") + 1], "");
  assert.equal(
    args[args.indexOf("--settings") + 1],
    "/usr/local/share/remote-agent/slack-executor-settings.json",
  );
  assert.equal(args[args.indexOf("--model") + 1], "efficient");
  assert.equal(args[args.indexOf("--output-format") + 1], "json");
  assert.equal(args[args.indexOf("--append-system-prompt") + 1], "facts");
  assert.ok(args.includes("--no-session-persistence"));
  assert.equal(args.filter((arg) => arg === "-p").length, 1);
  assert.equal(args.filter((arg) => arg === "--model").length, 1);
  assert.ok(!args.includes("--max-budget-usd"));
  assert.ok(!args.includes("--dangerously-skip-permissions"));
  assert.ok(!args.includes("--yolo"));
  assert.ok(!args.some((arg) => /bypass|dangerously/i.test(arg)));
});

test("chunkText respects the limit and keeps content", () => {
  assert.deepEqual(chunkText("hello", 3000), ["hello"]);
  const text = "line of words\n".repeat(900);
  const chunks = chunkText(text, 3000);
  assert.ok(chunks.length > 1);
  for (const chunk of chunks) assert.ok(chunk.length <= 3000);
  assert.equal(chunks.join("").replace(/\s+/g, ""), text.replace(/\s+/g, ""));
});

test("budgets: four runs per thread per hour, daily cap, new-day reset", () => {
  const cfg = goodCfg();
  const where = { channel: CHANNELS.lobby, rootTs: "6.1" };
  const state = freshState();
  for (let i = 0; i < 4; i += 1) {
    assert.equal(budgetCheck(state, where, NOW + i, cfg).ok, true);
    budgetRecord(state, where, NOW + i);
  }
  assert.deepEqual(budgetCheck(state, where, NOW + 10, cfg), { ok: false, reason: "thread_cap" });
  assert.equal(budgetCheck(state, { channel: CHANNELS.lobby, rootTs: "6.2" }, NOW + 10, cfg).ok, true);

  const capped = freshState();
  capped.daily = { date: dayKey(NOW), runs: 30 };
  assert.equal(budgetCheck(capped, where, NOW, cfg).reason, "daily_cap");
  capped.daily = { date: "2000-01-01", runs: 30 };
  assert.equal(budgetCheck(capped, where, NOW, cfg).ok, true);
  budgetRecord(capped, where, NOW);
  assert.equal(capped.daily.runs, 1);
});

test("rate limit: six per ten minutes, ten seconds apart", () => {
  const state = freshState();
  rateRecord(state, "U0PERSON1", NOW);
  assert.equal(rateCheck(state, "U0PERSON1", NOW + 5).reason, "rate_gap");
  assert.equal(rateCheck(state, "U0PERSON1", NOW + 11).ok, true);
  for (let i = 1; i < 6; i += 1) rateRecord(state, "U0PERSON1", NOW + i * 30);
  assert.equal(rateCheck(state, "U0PERSON1", NOW + 181).reason, "rate_window");
  assert.equal(rateCheck(state, "U0PERSON1", NOW + 601).ok, true);
});

test("clock steps backward never wedge the rate/budget windows", () => {
  const state = freshState();
  rateRecord(state, "U0PERSON1", NOW + 500); // future-stamped entry
  assert.equal(rateCheck(state, "U0PERSON1", NOW).ok, true);
  const where = { channel: CHANNELS.lobby, rootTs: "9.1" };
  budgetRecord(state, where, NOW + 500);
  assert.equal(budgetCheck(state, where, NOW, goodCfg()).ok, true);
});

test("parseExecutorResult reads the last JSON envelope (wrapper logs pollute stdout)", () => {
  assert.deepEqual(parseExecutorResult('{"type":"result","result":"ok text","is_error":false}', 0), {
    ok: true,
    text: "ok text",
  });
  const wrapped =
    "2026-09-12T06:53:58Z allow-start status=free price_factor=0.0 source=pid:574580\n" +
    '{"type":"result","subtype":"success","is_error":false,"result":"wrapped ok"}\n';
  assert.deepEqual(parseExecutorResult(wrapped, 0), { ok: true, text: "wrapped ok" });
  assert.equal(parseExecutorResult('{"type":"result","result":"","is_error":true,"subtype":"error_max_turns"}', 1).ok, false);
  assert.equal(parseExecutorResult('{"type":"result","result":"","is_error":true,"subtype":"error_max_turns"}', 1).error, "error_max_turns");
  assert.equal(parseExecutorResult("not json", 0).ok, false);
  assert.equal(parseExecutorResult("", 1).ok, false);
  assert.equal(parseExecutorResult("null", 0).ok, false);
  assert.equal(parseExecutorResult('"text"', 0).ok, false);
  assert.equal(parseExecutorResult("[1,2]", 0).ok, false);
  // a JSON object without type:"result" is not the envelope, even if it
  // carries a result key — a keyed log line must not mask the real result
  assert.equal(parseExecutorResult('{"result":"ok text","is_error":false}', 0).ok, false);
  const keyedLog = '{"type":"result","is_error":false,"result":"real"}\n{"level":"info","result":"fake"}\n';
  assert.deepEqual(parseExecutorResult(keyedLog, 0), { ok: true, text: "real" });
  const trailingLog = '{"type":"result","is_error":false,"result":"real"}\n{"level":"info","msg":"bye"}\n';
  assert.deepEqual(parseExecutorResult(trailingLog, 0), { ok: true, text: "real" });
  // exit 75 is the qoder-efficient wrapper's cost-gate refusal
  assert.deepEqual(parseExecutorResult("", 75), { ok: false, error: "cost_gate_refused" });
});

test("formatThreadContext caps size and keeps the newest messages", () => {
  const messages = Array.from({ length: 30 }, (_, i) => ({
    user: "U1",
    text: `${i}:${"a".repeat(1400)}`,
  }));
  const formatted = formatThreadContext(messages);
  assert.ok(formatted.length <= 12000);
  assert.ok(formatted.includes("29:"));
  assert.ok(!formatted.includes("0:"));
});

test("agentIdentity and isRecoveryText fail closed", () => {
  assert.equal(agentIdentity({ botId: "B1", username: "orca" }, AGENTS, "B1"), "orca");
  assert.equal(agentIdentity({ botId: "B1", username: "karma-bot" }, AGENTS, "B1"), null);
  assert.equal(agentIdentity({ botId: "", username: "orca" }, AGENTS, "B1"), null);
  // impersonation: another app/webhook posting with a registered username
  assert.equal(agentIdentity({ botId: "B9", username: "orca" }, AGENTS, "B1"), null);
  assert.equal(agentIdentity({ botId: "B1", username: "orca" }, AGENTS, ""), null);
  assert.equal(isRecoveryText(":white_check_mark: recovered"), true);
  assert.equal(isRecoveryText("proxy_down"), false);
});

// ---------------------------------------------------------------------------
// #lobby debate (SLACK_DEBATE=1) — second responder = antigravity (agy)
// ---------------------------------------------------------------------------

test("loadConfig parses the debate knobs and the agy bin", () => {
  const cfg = loadConfig(
    [
      "SLACK_DEBATE=1",
      "SLACK_THREAD_RUNS_PER_HOUR=2",
      "SLACK_THREAD_TURNS_PER_DAY=5",
      "SLACK_DEBATE_MAX_RUNS_PER_DAY=9",
      "SLACK_AGY_BIN=~/bin/agy",
    ].join("\n"),
  );
  assert.equal(cfg.debate, true);
  assert.equal(cfg.threadRunsPerHour, 2);
  assert.equal(cfg.threadTurnsPerDay, 5);
  assert.equal(cfg.debateMaxRunsPerDay, 9);
  assert.equal(cfg.agyBin, join(homedir(), "bin/agy"));

  const defaults = loadConfig("");
  assert.equal(defaults.debate, false);
  assert.equal(defaults.threadRunsPerHour, 4);
  assert.equal(defaults.threadTurnsPerDay, 8);
  assert.equal(defaults.debateMaxRunsPerDay, 20);
  assert.equal(defaults.agyBin, join(homedir(), ".local/bin/agy"));
});

test("validateConfig requires an absolute agy bin only when debate is on", () => {
  assert.deepEqual(validateConfig(goodCfg({ debate: true })), []);
  assert.ok(
    validateConfig(goodCfg({ debate: true, agyBin: "agy" })).some((problem) =>
      problem.includes("SLACK_AGY_BIN"),
    ),
  );
  assert.deepEqual(validateConfig(goodCfg({ debate: false, agyBin: "agy" })), []);
});

test("classifyMessage: debate off keeps the single-responder contract", () => {
  const cfg = goodCfg(); // debate: false
  const orca = message({
    channel: CHANNELS.lobby,
    ts: "7.1",
    botId: "B1",
    username: "orca",
    text: "two daemon restarts today",
  });
  const decision = classifyMessage(orca, cfg, CTX);
  assert.equal(decision.kind, "discussion");
  assert.equal(decision.speaker, "qoder");
  assert.equal(decision.continuation, false);
  // the responder's own post — even in-thread — stays ignored
  assert.equal(
    classifyMessage(message({ ...orca, username: "qoder", threadTs: "7.1", ts: "7.2" }), cfg, CTX)
      .reason,
    "lobby_self",
  );
  assert.equal(
    classifyMessage(message({ ...orca, threadTs: "7.1", ts: "7.3" }), cfg, CTX).reason,
    "lobby_thread_reply",
  );
});

test("classifyMessage: debate on alternates responders on their own posts", () => {
  const cfg = goodCfg({ debate: true });
  const orca = message({
    channel: CHANNELS.lobby,
    ts: "8.1",
    botId: "B1",
    username: "orca",
    text: "two daemon restarts today",
  });
  const first = classifyMessage(orca, cfg, CTX);
  assert.equal(first.kind, "discussion");
  assert.equal(first.identity, "orca");
  assert.equal(first.speaker, "qoder");
  assert.equal(first.continuation, false);

  const qoderReply = message({
    ...orca,
    username: "qoder",
    threadTs: "8.1",
    ts: "8.2",
    text: "my take",
  });
  const second = classifyMessage(qoderReply, cfg, CTX);
  assert.equal(second.kind, "discussion");
  assert.equal(second.identity, "qoder");
  assert.equal(second.speaker, "antigravity");
  assert.equal(second.continuation, true);

  const agyReply = message({
    ...orca,
    username: "antigravity",
    threadTs: "8.1",
    ts: "8.3",
    text: "counterpoint",
  });
  const third = classifyMessage(agyReply, cfg, CTX);
  assert.equal(third.speaker, "qoder");
  assert.equal(third.continuation, true);

  // a responder's top-level post also continues the exchange
  assert.equal(
    classifyMessage(message({ ...orca, username: "antigravity", ts: "8.9" }), cfg, CTX).speaker,
    "qoder",
  );
  // bridge acks/briefs never trigger; unknown identities fail closed;
  // third-party agent replies stay ignored
  assert.equal(
    classifyMessage(message({ ...orca, username: "bridge", threadTs: "8.1", ts: "8.4" }), cfg, CTX)
      .reason,
    "lobby_self",
  );
  assert.equal(
    classifyMessage(message({ ...orca, username: "karma-bot" }), cfg, CTX).reason,
    "lobby_unknown_identity",
  );
  assert.equal(
    classifyMessage(message({ ...orca, threadTs: "8.1", ts: "8.5" }), cfg, CTX).reason,
    "lobby_thread_reply",
  );
  // a human follow-up mid-thread is always a first-turn qoder answer:
  // never answerable with [PASS] silence
  const human = classifyMessage(
    message({
      channel: CHANNELS.lobby,
      ts: "8.6",
      threadTs: "8.1",
      user: "U0PERSON1",
      text: "qoder: what about the proxy?",
    }),
    cfg,
    CTX,
  );
  assert.equal(human.kind, "discussion");
  assert.equal(human.speaker, "qoder");
  assert.equal(human.continuation, false);
});

test("isPassText and otherResponder are strict", () => {
  assert.equal(isPassText("[PASS]"), true);
  assert.equal(isPassText("  [pass] \n"), true);
  assert.equal(isPassText("[PASS] but here is more"), false);
  assert.equal(isPassText("I will pass on that"), false);
  assert.equal(otherResponder("qoder"), "antigravity");
  assert.equal(otherResponder("antigravity"), "qoder");
});

test("speakerReady: qoder always; agy needs bin and settings file", () => {
  const cfg = goodCfg({ debate: true });
  assert.equal(speakerReady("qoder", cfg), true);
  // settings file missing (only the bin exists)
  assert.equal(speakerReady("antigravity", cfg, (path) => path === cfg.agyBin), false);
  assert.equal(speakerReady("antigravity", cfg, () => true), true);
  assert.equal(speakerReady("antigravity", { ...cfg, agyBin: "agy" }, () => true), false);
});

test("debate merge window collapses chunked responder posts", () => {
  const merge = {};
  const where = { channel: CHANNELS.lobby, rootTs: "9.1" };
  assert.equal(debateMerged(merge, where, "qoder", NOW), false);
  debateMergeRecord(merge, where, "qoder", NOW);
  assert.equal(debateMerged(merge, where, "qoder", NOW + 30), true);
  // recording slides the window forward (a chunk stream stays merged)
  debateMergeRecord(merge, where, "qoder", NOW + 30);
  assert.equal(debateMerged(merge, where, "qoder", NOW + 70), true);
  assert.equal(debateMerged(merge, where, "qoder", NOW + 91), false);
  // a different author or thread is never merged
  assert.equal(debateMerged(merge, where, "antigravity", NOW + 31), false);
  assert.equal(debateMerged(merge, { ...where, rootTs: "9.2" }, "qoder", NOW + 31), false);
  // clock steps backward: future stamps never merge
  debateMergeRecord(merge, where, "qoder", NOW + 500);
  assert.equal(debateMerged(merge, where, "qoder", NOW), false);
});

test("budgets: debate day cap and non-renewing per-thread day cap", () => {
  const cfg = goodCfg({ debate: true });
  const where = { channel: CHANNELS.lobby, rootTs: "10.1", debate: true };

  const state = freshState();
  state.daily = { date: dayKey(NOW), runs: 1, debate: 20 };
  assert.equal(budgetCheck(state, where, NOW, cfg).reason, "debate_day_cap");
  // non-lobby channels are not blocked by the debate counter
  assert.equal(
    budgetCheck(state, { channel: CHANNELS.control, rootTs: "10.9" }, NOW, cfg).ok,
    true,
  );

  const thread = freshState();
  thread.threadDays[`${CHANNELS.lobby}:10.1`] = { date: dayKey(NOW), turns: 8 };
  assert.equal(budgetCheck(thread, where, NOW, cfg).reason, "thread_day_cap");
  // non-renewing: the same thread is fine again the next day
  assert.equal(budgetCheck(thread, where, NOW + 86_400, cfg).ok, true);

  budgetRecord(thread, where, NOW);
  assert.equal(thread.daily.debate, 1);
  assert.equal(thread.daily.runs, 1);
  assert.equal(thread.threadDays[`${CHANNELS.lobby}:10.1`].turns, 9);
  // control runs never consume the debate counter
  budgetRecord(thread, { channel: CHANNELS.control, rootTs: "10.9" }, NOW);
  assert.equal(thread.daily.debate, 1);
  assert.equal(thread.daily.runs, 2);

  // stale threadDays entries are pruned on the next record
  const stale = freshState();
  stale.threadDays["C01LOBBY00:old"] = { date: "2000-01-01", turns: 3 };
  budgetRecord(stale, where, NOW);
  assert.equal(stale.threadDays["C01LOBBY00:old"], undefined);

  // debate off: the per-thread day cap must not exist (exact old behavior)…
  const single = freshState();
  single.threadDays[`${CHANNELS.lobby}:10.1`] = { date: dayKey(NOW), turns: 8 };
  assert.equal(
    budgetCheck(single, { ...where, debate: false }, NOW, goodCfg()).ok,
    true,
  );
  // …and non-debate runs never touch the threadDays bookkeeping
  budgetRecord(single, { channel: CHANNELS.control, rootTs: "10.9" }, NOW);
  assert.equal(single.threadDays[`${CHANNELS.control}:10.9`], undefined);
});

test("buildAgyArgs: headless print mode, no permission bypass", () => {
  const args = buildAgyArgs({ prompt: "hello" });
  assert.deepEqual(args, ["-p", "hello", "--output-format", "json", "--print-timeout", "4m"]);
  assert.ok(!args.some((arg) => /dangerously|bypass|yolo/i.test(arg)));
  // the print timeout must fire before the bridge's 300 s SIGKILL
  const minutes = Number(/^(\d+)m$/.exec(args[args.indexOf("--print-timeout") + 1])[1]);
  assert.ok(minutes * 60 < 300);
});

test("parseAgyResult reads the agy envelope, fails closed otherwise", () => {
  const envelope = JSON.stringify({
    conversation_id: "c1",
    status: "SUCCESS",
    response: "ok from agy",
    error: "",
    duration_seconds: 3,
    num_turns: 1,
    usage: {},
  });
  assert.deepEqual(parseAgyResult(envelope, 0), { ok: true, text: "ok from agy" });
  // log lines before the envelope are skipped (reversed scan)
  assert.deepEqual(parseAgyResult(`booting...\n${envelope}\n`, 0), { ok: true, text: "ok from agy" });
  // non-SUCCESS carries the status and the error detail
  const failed = JSON.stringify({ status: "ERROR", error: "quota exhausted" });
  assert.deepEqual(parseAgyResult(failed, 1), { ok: false, error: "agy_ERROR: quota exhausted" });
  assert.equal(parseAgyResult(JSON.stringify({ status: "CANCELED" }), 130).error, "agy_CANCELED");
  // SUCCESS with an empty response is not a reply
  assert.equal(
    parseAgyResult(JSON.stringify({ status: "SUCCESS", response: "  " }), 0).error,
    "empty_result",
  );
  // no envelope at all, or non-envelope JSON
  assert.equal(parseAgyResult("plain text", 1).error, "exit_1_no_result");
  assert.equal(parseAgyResult("", 0).error, "exit_0_no_result");
  assert.equal(parseAgyResult('{"result":"qoder-shaped"}', 0).error, "exit_0_no_result");
  assert.equal(parseAgyResult('{"status":42}', 0).error, "exit_0_no_result");
  // error detail is bounded
  const long = parseAgyResult(JSON.stringify({ status: "ERROR", error: "x".repeat(500) }), 1);
  assert.ok(long.error.length < 260);
});

test("buildAgyPrompt: agy facts and persona, never qoder executor text", () => {
  const job = {
    kind: "discussion",
    identity: "qoder",
    speaker: "antigravity",
    debate: true,
    continuation: true,
    text: "my take on the proxy",
  };
  const prompt = buildAgyPrompt(job, "orca: issue\nqoder: my take on the proxy");
  assert.ok(prompt.includes("my take on the proxy"));
  assert.ok(prompt.includes("Thread so far:"));
  assert.ok(prompt.includes("[PASS]"));
  assert.ok(prompt.includes("Antigravity"));
  // the agy leg must never inherit qoder's executor facts
  assert.ok(!prompt.includes("qodercli"));
  assert.ok(!prompt.includes("Efficient model"));
  // first turns never carry the [PASS] offer
  const first = buildAgyPrompt({ ...job, continuation: false }, "");
  assert.ok(!first.includes("[PASS]"));
});

test("dispatchLine: goal carries the clause on one line, resume is the bare command", () => {
  const clause = "COMPLETION PROTOCOL:\n  commit, then post to #lobby";
  const line = dispatchLine("goal", "fix   the parser\nnext", clause);
  assert.equal(
    line,
    "/goal fix the parser next COMPLETION PROTOCOL: commit, then post to #lobby",
  );
  assert.equal(dispatchLine("resume", "ignored", clause), "/goal resume");
  assert.equal(dispatchLine("goal", "no clause yet", ""), "/goal no clause yet");
  assert.equal(dispatchLine("goal", "x", "   "), "/goal x");
  assert.throws(() => dispatchLine("goal", "y".repeat(5000), "clause"), /goal line exceeds 4000/);
});

test("parseDispatchArgs: supervisor CLI shape", () => {
  const goal = parseDispatchArgs(["goal", "qoder", "shrink", "the", "parser"]);
  assert.deepEqual(goal, {
    builtin: "goal",
    worker: "qoder",
    text: "shrink the parser",
    dryRun: false,
    operator: "supervisor",
    workersFile: null,
    target: "efficient",
    tier: "paid",
    requestId: null,
    error: null,
  });

  const resume = parseDispatchArgs(["resume", "Pugc", "--dry-run"]);
  assert.equal(resume.builtin, "resume");
  assert.equal(resume.worker, "pugc");
  assert.equal(resume.text, "");
  assert.equal(resume.dryRun, true);

  const full = parseDispatchArgs([
    "goal",
    "qoder",
    "text",
    "--operator",
    "grok-supervisor",
    "--workers-file",
    "/tmp/workers.json",
  ]);
  assert.equal(full.operator, "grok-supervisor");
  assert.equal(full.workersFile, "/tmp/workers.json");
  assert.equal(full.text, "text");
  assert.equal(parseDispatchArgs(["goal", "q", "task", "--target", "flash", "--tier", "free"]).tier, "free");
  assert.equal(parseDispatchArgs(["goal", "q", "task", "--tier", "free"]).error, "free tier requires a Flash goal");
  assert.equal(parseDispatchArgs(["goal", "q", "task", "--target", "flash", "--tier", "unknown"]).error, "invalid dispatch tier");

  // A hyphenated or multi-word goal stays one literal; only flags are consumed.
  assert.equal(parseDispatchArgs(["goal", "q", "fix --dry-run bug"]).text, "fix --dry-run bug");
});

test("packetSurface: pin by default, job opt-in", () => {
  assert.equal(packetSurface({}), "pin");
  assert.equal(packetSurface({ SPECTRE_PACKET_SURFACE: "job" }), "job");
  assert.equal(packetSurface({ SPECTRE_PACKET_SURFACE: "nope" }), "pin");
  assert.equal(packetShellTitle("flash"), "flash-packets");
  assert.equal(packetShellTitle("mimo"), "mimo-packets");
  assert.equal(packetJobTitle("mimo", "d-1"), "mimo d-1");
  const entry = applyPacketPin({ targets: {} }, "mimo", "term_9");
  assert.equal(entry.targets.mimo.terminal, "term_9");
  assert.equal(entry.targets.mimo.wrapper, "/usr/local/bin/mimo-clinepass");
});

test("ensurePacketPin: reuses a live pin and creates flash-packets when missing", async () => {
  const dir = mkdtempSync(join(tmpdir(), "spectre-pin-ensure-"));
  const bin = join(dir, "bin");
  mkdirSync(bin);
  const log = join(dir, "orca.log");
  const created = join(dir, "created");
  const script = [
    "#!/bin/sh",
    'printf "%s\\n" "$*" >> ' + JSON.stringify(log),
    'if [ "$1" = "terminal" ] && [ "$2" = "list" ]; then',
    '  if [ -f ' + JSON.stringify(created) + ' ]; then',
    "    printf '%s' " + JSON.stringify(JSON.stringify({ ok: true, result: { terminals: [{ handle: "term_new", worktreePath: dir, connected: true, writable: true, title: "flash-packets" }] } })),
    "  else",
    "    printf '%s' " + JSON.stringify(JSON.stringify({ ok: true, result: { terminals: [] } })),
    "  fi",
    'elif [ "$1" = "terminal" ] && [ "$2" = "create" ]; then',
    '  touch ' + JSON.stringify(created),
    "  printf '%s' " + JSON.stringify(JSON.stringify({ ok: true, result: { terminal: { handle: "term_new" } } })),
    "else",
    "  printf '%s' '{\"ok\":true}'",
    "fi",
  ].join("\n");
  writeFileSync(join(bin, "orca-ide"), script, { mode: 0o755 });
  const prev = process.env.PATH;
  process.env.PATH = `${bin}:${prev}`;
  try {
    const first = await ensurePacketPin({}, "flash", dir);
    assert.equal(first.ok, true, JSON.stringify(first));
    assert.equal(first.created, true);
    assert.equal(first.handle, "term_new");
    const second = await ensurePacketPin({ targets: { flash: { terminal: "term_new" } } }, "flash", dir);
    assert.equal(second.ok, true, JSON.stringify(second));
    assert.equal(second.created, false);
  } finally {
    process.env.PATH = prev;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("injectionLine: mimo never types /goal and targets mimo-clinepass", () => {
  const mimo = injectionLine({ builtin: "goal", target: "mimo", dispatchId: "m1" });
  assert.ok(mimo.startsWith("/usr/local/bin/mimo-clinepass --file "));
  assert.ok(!mimo.includes("/goal"));
  assert.equal(parseDispatchArgs(["goal", "minecraft", "hi", "--target", "mimo"]).target, "mimo");
  assert.ok(parseDispatchArgs(["goal", "minecraft", "hi", "--target", "gemini"]).error);
});

test("injectionLine: flash never types /goal; efficient does", () => {
  const flash = injectionLine({ builtin: "goal", target: "flash", dispatchId: "abc" });
  assert.equal(flash, flashSendLine("abc"));
  assert.ok(flash.startsWith("/usr/local/bin/dsh-clinepass --file "));
  assert.ok(!flash.includes("/goal"));
  const efficient = injectionLine({
    builtin: "goal",
    target: "efficient",
    goalText: "fix parser",
    clauseText: "PROTOCOL",
  });
  assert.ok(efficient.startsWith("/goal "));
  assert.ok(efficient.includes("fix parser"));
});

test("writeFlashPacket: names the file after the claimed dispatch_id", () => {
  const dir = mkdtempSync(join(tmpdir(), "spectre-packet-"));
  try {
    const env = { SPECTRE_PACKET_DIR: dir };
    const written = writeFlashPacket("d-claimed1", "fix   the nether\nnext", env);
    assert.equal(written.ok, true);
    assert.equal(written.path, join(dir, "d-claimed1.txt"));
    assert.equal(readFileSync(written.path, "utf8"), "fix   the nether\nnext\n");
    assert.equal(statSync(written.path).mode & 0o777, 0o600);
    const made = finalizeFlashInjection({ claim: { dispatch_id: "d-claimed1" } }, "fix the nether", env);
    assert.equal(made.ok, true);
    assert.equal(made.dispatch_id, "d-claimed1");
    assert.equal(made.line, flashSendLine("d-claimed1", env));
    assert.equal(flashSendLine("d-claimed1", env, "free"), `${made.line} --tier free`);
    assert.equal(finalizeFlashInjection({ claim: { dispatch_id: "d-claimed2" } }, "free work", env, "free").line,
      flashSendLine("d-claimed2", env, "free"));
    assert.ok(!made.line.includes("fix the nether"));
    assert.equal(finalizeFlashInjection({ claim: {} }, "x", env).ok, false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("flashReady: fail-closes missing wrapper/key/dsh/pin and busy pin-tree dsh", () => {
  const dir = mkdtempSync(join(tmpdir(), "spectre-flash-ready-"));
  try {
    const wrapper = join(dir, "dsh-clinepass");
    const key = join(dir, "key");
    const dsh = join(dir, "dsh");
    const proc = join(dir, "proc");
    mkdirSync(proc);
    writeFileSync(wrapper, "#!/bin/sh\n", { mode: 0o644 });
    chmodSync(wrapper, 0o644);
    writeFileSync(key, "secret\n", { mode: 0o600 });
    chmodSync(key, 0o600);
    writeFileSync(dsh, "#!/bin/sh\n", { mode: 0o755 });
    chmodSync(dsh, 0o755);
    const env = {
      SPECTRE_DSH_WRAPPER: wrapper,
      SPECTRE_DSH_KEY: key,
      SPECTRE_DSH_BIN: dsh,
      SPECTRE_PROC_ROOT: proc,
    };
    assert.equal(flashReady({}, env).evt, "dispatch_flash_unavailable");
    const entry = { targets: { flash: { terminal: "term_flash" } } };
    assert.match(flashReady(entry, env).detail, /755/);
    chmodSync(wrapper, 0o755);
    assert.equal(flashReady(entry, env).ok, true);
    assert.equal(flashPin(entry), "term_flash");

    mkdirSync(join(proc, "42"));
    writeFileSync(join(proc, "42", "cmdline"), "dsh\0--profile\0headless\0task");
    writeFileSync(join(proc, "42", "environ"), "ORCA_TERMINAL_HANDLE=term_flash\0");
    writeFileSync(join(proc, "42", "stat"), "42 (dsh) S 1 0 0 0 0 0 0 0 0 0 10 5 0 0 0 0 0\n");
    assert.equal(pinTreeHasDsh("term_flash", proc), true);
    assert.equal(flashReady(entry, env).evt, "dispatch_flash_busy");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("flashReady: free tier requires flag, private profile and proxy client key", () => {
  const dir = mkdtempSync(join(tmpdir(), "spectre-free-ready-"));
  try {
    const wrapper = join(dir, "dsh-clinepass");
    const key = join(dir, "client_key");
    const dsh = join(dir, "dsh");
    const home = join(dir, "free-home");
    mkdirSync(home);
    writeFileSync(wrapper, "#!/bin/sh\n", { mode: 0o755 });
    chmodSync(wrapper, 0o755);
    writeFileSync(dsh, "#!/bin/sh\n", { mode: 0o755 });
    chmodSync(dsh, 0o755);
    writeFileSync(key, "client-key\n", { mode: 0o600 });
    chmodSync(key, 0o600);
    const entry = { targets: { flash: { terminal: "term_free" } } };
    const env = { SPECTRE_DSH_WRAPPER: wrapper, SPECTRE_DSH_BIN: dsh,
      SPECTRE_DSH_FREE_KEY: key, SPECTRE_DSH_FREE_HOME: home };
    assert.equal(flashReady(entry, env, "free").evt, "dispatch_flash_free_unavailable");
    env.SPECTRE_FREE_PACKETS_ENABLED = "1";
    assert.equal(flashReady(entry, env, "free").evt, "dispatch_flash_free_unavailable");
    writeFileSync(join(home, "settings.yaml"), "free profile\n", { mode: 0o600 });
    chmodSync(join(home, "settings.yaml"), 0o600);
    assert.equal(flashReady(entry, env, "free").ok, true);
    rmSync(join(home, "settings.yaml"));
    symlinkSync(key, join(home, "settings.yaml"));
    assert.equal(flashReady(entry, env, "free").evt, "dispatch_flash_free_unavailable");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("freeProxyReady: only a live loopback proxy with both providers is accepted", async () => {
  assert.equal((await freeProxyReady({ SPECTRE_OMNI_ENDPOINT: "http://example.com/v1" })).ok, false);
  const server = createServer((_req, response) => {
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({ ok: true, providers: ["cline-free", "cline-paid"] }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const endpoint = `http://127.0.0.1:${server.address().port}/v1`;
    assert.equal((await freeProxyReady({ SPECTRE_OMNI_ENDPOINT: endpoint })).ok, true);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("astraPidVerdict: plus-burn and busy", () => {
  assert.equal(
    astraPidVerdict(["codex -m gpt-6-astra -c model_provider=openai"]).evt,
    "dispatch_astra_plus_burn",
  );
  assert.equal(astraPidVerdict(["codex -m gpt-6-astra", "codex -m gpt-6-astra"]).evt, "dispatch_astra_busy");
  assert.equal(astraPidVerdict(["codex -m gpt-6-astra"]).ok, true);
});

test("paneRefusal: flash wrapper prefix may run in a shell", () => {
  const line = flashSendLine("x");
  assert.equal(
    paneRefusal("bash", "/home/person/Projects/minecraft-server-project", "/home/person/Projects/minecraft-server-project", {
      allowFlashShell: true,
      line,
    }),
    null,
  );
  assert.ok(
    paneRefusal("bash", "/home/person/Projects/minecraft-server-project", "/home/person/Projects/minecraft-server-project"),
  );
});

test("parseDispatchArgs: refusals never become a bare goal", () => {
  assert.ok(parseDispatchArgs([]).error);
  assert.ok(parseDispatchArgs(["scan"]).error);
  assert.ok(parseDispatchArgs(["goal", "qoder"]).error); // no goal text
  assert.ok(parseDispatchArgs(["goal"]).error); // no worker
  assert.ok(parseDispatchArgs(["resume"]).error);
  assert.equal(parseDispatchArgs(["resume", "qoder", "extra"]).error, null);
});

const BRIDGE_SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "..", "scripts", "slack-bridge.mjs");
const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

function fakeProcess(root, pid, argv, handle, cwd) {
  const path = join(root, String(pid));
  mkdirSync(path, { recursive: true });
  writeFileSync(join(path, "cmdline"), argv.join("\0") + "\0");
  writeFileSync(join(path, "environ"), `ORCA_TERMINAL_HANDLE=${handle}\0`);
  writeFileSync(join(path, "stat"), `${pid} (test) S 1 0 0 0`);
  symlinkSync(cwd, join(path, "cwd"));
}

function startStateServer(dir, ingestKind) {
  const proc = join(dir, "identity-proc");
  fakeProcess(proc, 991, ["qodercli", "-m", "Efficient"], "term_test", dir);
  fakeProcess(proc, 992, ["qodercli", "-m", "Efficient"], "term_pin", dir);
  const sock = join(dir, "state.sock");
  const db = join(dir, "state.sqlite");
  const pyPath = join(dir, "seed.py");
  const scripts = join(REPO_ROOT, "scripts");
  writeFileSync(
    pyPath,
    [
      "import sys",
      `sys.path.insert(0, ${JSON.stringify(scripts)})`,
      "from worker_state.store import Store",
      `store = Store(${JSON.stringify(db)})`,
      "now = 1800000000.0",
      ingestKind === "parked"
        ? "store.ingest({\"event_id\":\"park-1\",\"worker\":\"native\",\"kind\":\"goal.parked\",\"source\":\"session_jsonl\",\"turn_id\":\"t1\",\"source_timestamp\":\"2027-01-15T08:00:00+00:00\",\"payload\":{\"park_reason\":\"goal_budget\"}}, now)"
        : "for worker in ('idle', 'minecraft', 'faux'): store.snapshot(worker, now, rebuild=True)",
      "store.close()",
    ].join("\n"),
  );
  execFileSync("python3", [pyPath], { encoding: "utf8" });
  const child = spawn(
    "python3",
    [join(REPO_ROOT, "scripts/spectre-state.py"), "--socket", sock, "--db", db, "serve", "--poll-interval", "0", "--no-shadow"],
    { stdio: ["ignore", "ignore", "inherit"] },
  );
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    const health = spawnSync(
      "python3",
      [join(REPO_ROOT, "scripts/spectre-state.py"), "--socket", sock, "health"],
      { encoding: "utf8" },
    );
    if (health.status === 0) break;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50);
  }
  return {
    env: { SPECTRE_WORKER_STATE_SOCK: sock, SPECTRE_PROC_ROOT: proc },
    db,
    stop() {
      child.kill("SIGTERM");
    },
  };
}

function seedParked(db, worker, eventId, turnId) {
  const script = [
    "import sys",
    `sys.path.insert(0, ${JSON.stringify(join(REPO_ROOT, "scripts"))})`,
    "from worker_state.client import StateClient",
    `client = StateClient(${JSON.stringify(join(dirname(db), "state.sock"))})`,
    `status, payload = client.evidence({"event_id":${JSON.stringify(eventId)},"worker":${JSON.stringify(worker)},"kind":"goal.parked","source":"session_jsonl","turn_id":${JSON.stringify(turnId)},"source_timestamp":"2027-01-15T08:00:01+00:00","payload":{"park_reason":"goal_budget"}})`,
    "assert status == 200, payload",
  ].join("\n");
  execFileSync("python3", ["-c", script]);
}

function runBridge(args, env) {
  const proc = spawnSync("node", [BRIDGE_SCRIPT, ...args], {
    encoding: "utf8",
    env: env ? { ...process.env, ...env } : process.env,
  });
  return { code: proc.status, stdout: proc.stdout || "", stderr: proc.stderr || "" };
}

function writeFlashBits(dir) {
  const wrapper = join(dir, "dsh-clinepass");
  const key = join(dir, "cline_api_key");
  const dsh = join(dir, "dsh");
  const proc = join(dir, "proc");
  const packets = join(dir, "packets");
  mkdirSync(proc, { recursive: true });
  fakeProcess(proc, 998, ["bash"], "term_flash", dir);
  mkdirSync(packets, { recursive: true });
  writeFileSync(wrapper, "#!/bin/sh\nexit 0\n");
  chmodSync(wrapper, 0o755);
  writeFileSync(key, "not-a-real-key\n");
  chmodSync(key, 0o600);
  writeFileSync(dsh, "#!/bin/sh\nexit 0\n");
  chmodSync(dsh, 0o755);
  return {
    wrapper,
    key,
    dsh,
    proc,
    packets,
    env: {
      SPECTRE_DSH_WRAPPER: wrapper,
      SPECTRE_DSH_KEY: key,
      SPECTRE_DSH_BIN: dsh,
      SPECTRE_PROC_ROOT: proc,
      SPECTRE_PACKET_DIR: packets,
    },
  };
}

test("dispatch CLI: flash dry-run refuses missing wrapper and never types /goal", () => {
  const dir = mkdtempSync(join(tmpdir(), "spectre-flash-dry-"));
  const state = startStateServer(dir, "");
  try {
    const workers = join(dir, "workers.json");
    writeFileSync(
      workers,
      JSON.stringify({
        workers: {
          minecraft: {
            cwd: dir,
            tmux: null,
            terminal: "term_eff",
            targets: { flash: { terminal: "term_flash" } },
          },
        },
      }),
    );
    const missing = runBridge(
      ["--dispatch", "goal", "minecraft", "fix the nether", "--target", "flash", "--dry-run", "--workers-file", workers],
      { ...state.env, SPECTRE_PROC_ROOT: join(dir, "no-proc") },
    );
    assert.equal(missing.code, 1, missing.stdout);
    const verdict = JSON.parse(missing.stdout.trim());
    assert.equal(verdict.evt, "dispatch_flash_unavailable");
    assert.ok(!String(verdict.line || "").includes("/goal"));
  } finally {
    state.stop();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("dispatch CLI: flash claims first, writes packets/<dispatch_id>.txt, sends that line", () => {
  const dir = mkdtempSync(join(tmpdir(), "spectre-flash-send-"));
  const bin = join(dir, "bin");
  const state = startStateServer(dir, "");
  const bits = writeFlashBits(dir);
  try {
    mkdirSync(bin, { recursive: true });
    const workers = join(dir, "workers.json");
    writeFileSync(
      workers,
      JSON.stringify({
        workers: {
          minecraft: {
            cwd: dir,
            tmux: null,
            terminal: "term_eff",
            targets: { flash: { terminal: "term_flash" } },
          },
        },
      }),
    );
    const sentLog = join(dir, "sent.log");
    writeFileSync(
      join(bin, "orca-ide"),
      "#!/bin/sh\n" +
        'if [ "$1" = "terminal" ] && [ "$2" = "list" ]; then\n' +
        `  printf '%s' '{"ok":true,"result":{"terminals":[{"handle":"term_flash","worktreePath":"${dir}","connected":true,"writable":true},{"handle":"term_eff","worktreePath":"${dir}","connected":true,"writable":true}]}}'\n` +
        "else\n" +
        `  printf '%s\\n' "$*" >> "${sentLog}"\n` +
        "  printf '%s' '{\"ok\":true}'\n" +
        "fi\n",
      { mode: 0o755 },
    );
    const env = { PATH: `${bin}:${process.env.PATH}`, ...state.env, ...bits.env };
    const ok = runBridge(
      ["--dispatch", "goal", "minecraft", "fix the nether", "--target", "flash", "--workers-file", workers],
      env,
    );
    assert.equal(ok.code, 0, `${ok.stderr}${ok.stdout}`);
    const verdict = JSON.parse(ok.stdout.trim());
    assert.equal(verdict.evt, "dispatch_sent");
    assert.equal(verdict.terminal, "term_flash");
    assert.ok(verdict.dispatch_id, verdict);
    assert.match(verdict.line, /^\/usr\/local\/bin\/dsh-clinepass --file /);
    assert.ok(!verdict.line.includes("/goal"));
    assert.ok(!verdict.line.includes("fix the nether"));
    const packet = join(bits.packets, `${verdict.dispatch_id}.txt`);
    assert.equal(readFileSync(packet, "utf8").trim(), "fix the nether");
    const sent = readFileSync(sentLog, "utf8");
    assert.match(sent, new RegExp(`--terminal term_flash`));
    assert.match(sent, new RegExp(`${verdict.dispatch_id}\\.txt`));
    assert.equal(sent.includes("/goal"), false);
    const names = readdirSync(bits.packets).filter((n) => n.endsWith(".txt"));
    assert.deepEqual(names, [`${verdict.dispatch_id}.txt`]);
  } finally {
    state.stop();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("dispatch CLI: plan plus-burn refuses without terminal create", () => {
  const dir = mkdtempSync(join(tmpdir(), "spectre-plan-"));
  try {
    const workers = join(dir, "workers.json");
    writeFileSync(
      workers,
      JSON.stringify({ workers: { minecraft: { cwd: dir, tmux: null, terminal: "term_a" } } }),
    );
    const src = readFileSync(BRIDGE_SCRIPT, "utf8");
    assert.equal(src.includes("astra-planner"), false);
    assert.equal(src.includes("orca-ide terminal create"), false);
    const plus = runBridge(
      ["--dispatch", "plan", "minecraft", "next packet", "--dry-run", "--workers-file", workers],
      { SPECTRE_ASTRA_CMDLINES: "codex -m gpt-6-astra -c model_provider=openai" },
    );
    assert.equal(plus.code, 1, plus.stdout);
    const verdict = JSON.parse(plus.stdout.trim());
    assert.equal(verdict.evt, "dispatch_astra_plus_burn");
    assert.match(verdict.detail, /not creating a terminal/);
    assert.equal(src.includes("orca-ide terminal create"), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("dispatch CLI: usage and unknown workers exit non-zero with a JSON verdict", () => {
  const usage = runBridge(["--dispatch"]);
  assert.equal(usage.code, 2, usage.stderr);
  assert.equal(JSON.parse(usage.stdout.trim()).evt, "dispatch_usage");

  const unknown = runBridge(["--dispatch", "goal", "nope", "do the thing"]);
  assert.equal(unknown.code, 1);
  const verdict = JSON.parse(unknown.stdout.trim());
  assert.equal(verdict.evt, "dispatch_unknown_worker");
  assert.match(verdict.detail, /known: /);
});

test("pickNativeTerminal: exactly one live terminal for the worktree, or refuse", () => {
  const cwd = "/home/person/Projects/demo";
  const term = { handle: "term_1", worktreePath: cwd, connected: true, writable: true };
  const picked = pickNativeTerminal([term], cwd);
  assert.equal(picked.ok, true);
  assert.equal(picked.terminal.handle, "term_1");
  assert.equal(pickNativeTerminal([], cwd).ok, false);
  assert.equal(pickNativeTerminal([{ ...term, connected: false }], cwd).ok, false);
  assert.equal(pickNativeTerminal([{ ...term, writable: false }], cwd).ok, false);
  assert.equal(pickNativeTerminal([{ ...term, worktreePath: "/elsewhere" }], cwd).ok, false);
  const two = pickNativeTerminal([term, { ...term, handle: "term_2" }], cwd);
  assert.equal(two.ok, false);
  assert.match(two.detail, /refusing to guess/);
});

test("pickNativeTerminal: a terminal pin disambiguates several live terminals", () => {
  const cwd = "/home/person/Projects/demo";
  const worker = { handle: "term_worker", worktreePath: cwd, connected: true, writable: true };
  const handoff = { handle: "term_handoff", worktreePath: cwd, connected: true, writable: true };

  const pinned = pickNativeTerminal([worker, handoff], cwd, "term_worker");
  assert.equal(pinned.ok, true);
  assert.equal(pinned.terminal.handle, "term_worker");

  // A stale pin refuses — it never falls back to the other live terminal.
  const stale = pickNativeTerminal([handoff], cwd, "term_worker");
  assert.equal(stale.ok, false);
  assert.match(stale.detail, /pinned terminal term_worker/);
  assert.match(stale.detail, /not live/);

  // A pin cannot widen the match: a terminal in another worktree never counts.
  const elsewhere = pickNativeTerminal([{ ...worker, worktreePath: "/elsewhere" }], cwd, "term_worker");
  assert.equal(elsewhere.ok, false);

  // The pinned terminal must itself be live and writable.
  const dead = pickNativeTerminal([{ ...worker, writable: false }, handoff], cwd, "term_worker");
  assert.equal(dead.ok, false);
  assert.match(dead.detail, /not live/);
});

test("dispatch CLI: a native worker needs exactly one live orca terminal", () => {
  const dir = mkdtempSync(join(tmpdir(), "spectre-dispatch-"));
  const bin = join(dir, "bin");
  const state = startStateServer(dir, "parked");
  try {
    mkdirSync(bin, { recursive: true });
    const workers = join(dir, "workers.json");
    writeFileSync(workers, JSON.stringify({ workers: { native: { cwd: dir, tmux: null } } }));
    const env = { PATH: `${bin}:${process.env.PATH}`, ...state.env };

    // No connected terminal for the worktree: refused, nothing typed.
    writeFileSync(
      join(bin, "orca-ide"),
      "#!/bin/sh\nprintf '%s' '{\"ok\":true,\"result\":{\"terminals\":[]}}'\n",
      { mode: 0o755 },
    );
    const none = runBridge(["--dispatch", "resume", "native", "--workers-file", workers], env);
    assert.equal(none.code, 1, none.stderr);
    assert.equal(JSON.parse(none.stdout.trim()).evt, "dispatch_orca_refused");

    // Exactly one live terminal: the dispatch is typed into it with Enter.
    const sentLog = join(dir, "sent.log");
    writeFileSync(
      join(bin, "orca-ide"),
      "#!/bin/sh\n" +
        'if [ "$1" = "terminal" ] && [ "$2" = "list" ]; then\n' +
        `  printf '%s' '{"ok":true,"result":{"terminals":[{"handle":"term_test","worktreePath":"${dir}","connected":true,"writable":true}]}}'\n` +
        "else\n" +
        `  printf '%s\\n' "$*" >> "${sentLog}"\n` +
        "  printf '%s' '{\"ok\":true}'\n" +
        "fi\n",
      { mode: 0o755 },
    );
    const ok = runBridge(["--dispatch", "resume", "native", "--workers-file", workers], env);
    assert.equal(ok.code, 0, `${ok.stderr}${ok.stdout}`);
    const verdict = JSON.parse(ok.stdout.trim());
    assert.equal(verdict.evt, "dispatch_sent");
    assert.equal(verdict.terminal, "term_test");
    assert.match(readFileSync(sentLog, "utf8"), /--text \/goal resume --enter/);

    // Two live terminals (handoff beside the worker) with a registry pin: the
    // pinned handle is chosen, never the first row the list happens to return.
    writeFileSync(
      workers,
      JSON.stringify({ workers: { native: { cwd: dir, tmux: null, terminal: "term_pin" } } }),
    );
    writeFileSync(
      join(bin, "orca-ide"),
      "#!/bin/sh\n" +
        'if [ "$1" = "terminal" ] && [ "$2" = "list" ]; then\n' +
        `  printf '%s' '{"ok":true,"result":{"terminals":[{"handle":"term_other","worktreePath":"${dir}","connected":true,"writable":true},{"handle":"term_pin","worktreePath":"${dir}","connected":true,"writable":true}]}}'\n` +
        "else\n" +
        `  printf '%s\\n' "$*" >> "${sentLog}"\n` +
        "  printf '%s' '{\"ok\":true}'\n" +
        "fi\n",
      { mode: 0o755 },
    );
    seedParked(state.db, "native", "park-2", "t2");
    const pinned = runBridge(["--dispatch", "resume", "native", "--workers-file", workers], env);
    assert.equal(pinned.code, 0, `${pinned.stderr}${pinned.stdout}`);
    const pinnedVerdict = JSON.parse(pinned.stdout.trim());
    assert.equal(pinnedVerdict.evt, "dispatch_sent");
    assert.equal(pinnedVerdict.terminal, "term_pin");
    assert.match(readFileSync(sentLog, "utf8"), /--terminal term_pin/);
  } finally {
    state.stop();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("dispatch CLI: guards pass in a dry run against a real tmux pane", (t) => {
  const probe = spawnSync("tmux", ["-V"], { encoding: "utf8" });
  if (probe.status !== 0) {
    t.skip("tmux not installed");
    return;
  }
  const dir = mkdtempSync(join(tmpdir(), "spectre-dispatch-"));
  const session = `spectre-dispatch-test-${process.pid}`;
  const state = startStateServer(dir, "");
  try {
    execFileSync("tmux", ["new-session", "-d", "-s", session, "-c", dir, "sleep 300"], {
      stdio: "ignore",
    });
  } catch (err) {
    state.stop();
    rmSync(dir, { recursive: true, force: true });
    t.skip(`cannot start a tmux session: ${err.message}`);
    return;
  }
  try {
    const workers = join(dir, "workers.json");
    const write = (cwd) =>
      writeFileSync(workers, JSON.stringify({ workers: { faux: { cwd, tmux: session } } }));
    write(dir);

    // Dry run: every guard runs (registry -> probe -> pane -> cwd) and nothing is typed.
    const dry = runBridge(
      ["--dispatch", "goal", "faux", "do a thing", "--dry-run", "--workers-file", workers],
      state.env,
    );
    assert.equal(dry.code, 0, `${dry.stdout}${dry.stderr}`);
    const verdict = JSON.parse(dry.stdout.trim());
    assert.equal(verdict.evt, "dispatch_dry_run");
    assert.equal(verdict.worker, "faux");
    assert.equal(verdict.origin, "supervisor");
    assert.ok(verdict.chars > 0);

    // A registry cwd that does not match the pane is refused, not typed into.
    const elsewhere = join(dir, "elsewhere");
    mkdirSync(elsewhere);
    write(elsewhere);
    const refused = runBridge(
      ["--dispatch", "goal", "faux", "wrong cwd", "--workers-file", workers],
      state.env,
    );
    assert.equal(refused.code, 1);
    const refusal = JSON.parse(refused.stdout.trim());
    assert.equal(refusal.evt, "dispatch_pane_refused");
    assert.match(refusal.detail, /cwd/);
  } finally {
    try {
      execFileSync("tmux", ["kill-session", "-t", session], { stdio: "ignore" });
    } catch {
      // session already gone
    }
    state.stop();
    rmSync(dir, { recursive: true, force: true });
  }
});
