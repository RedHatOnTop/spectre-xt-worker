#!/usr/bin/env node
// Unit tests for the Slack Socket Mode bridge policy and guards.
import assert from "node:assert/strict";
import { homedir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  agentIdentity,
  budgetCheck,
  budgetRecord,
  buildExecutorArgs,
  chunkText,
  classifyCommand,
  classifyMessage,
  dayKey,
  formatThreadContext,
  isRecoveryText,
  loadConfig,
  normalizeEvent,
  parseExecutorResult,
  rateCheck,
  rateRecord,
  shouldHandle,
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
    maxRunsPerDay: 30,
    executorBin: "/home/person/.local/bin/qoder-efficient",
    executorModel: "efficient",
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
};
const CTX = { botUserId: "U0BOT", botId: "B1", agents: AGENTS };

function message(over = {}) {
  return Object.assign(normalizeEvent({}), over);
}

function freshState() {
  return { daily: { date: "", runs: 0 }, threads: {}, rate: {}, seen: {} };
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

test("buildExecutorArgs: read-only, isolated, never skips permissions", () => {
  const args = buildExecutorArgs({
    prompt: "hello",
    systemPrompt: "facts",
    settingsPath: "/usr/local/share/remote-agent/slack-executor-settings.json",
    model: "efficient",
  });
  assert.equal(args[args.indexOf("-p") + 1], "hello");
  assert.equal(args[args.indexOf("--permission-mode") + 1], "default");
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
    "2026-09-12T06:53:58Z allow-start status=free price_factor=0.0 source:pid:574580\n" +
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
