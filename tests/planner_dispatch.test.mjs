import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { planGuard, reservePlan, plannerPrompt } from "../scripts/planner-dispatch.mjs";

test("planner pin and assignment id are mandatory and minecraft-only", () => {
  const entry = { planner: { terminal: "term_a" } };
  const snapshot = { goal: { state: "ASSIGNING", assignment_id: "r1" } };
  assert.equal(planGuard("minecraft", entry, snapshot, "r1").ok, true);
  assert.equal(planGuard("other", entry, snapshot, "r1").ok, false);
  assert.equal(planGuard("minecraft", entry, snapshot, "r2").ok, false);
  assert.equal(planGuard("minecraft", {}, snapshot, "r1").ok, false);
});

test("planner request is reserved before sending and cannot be sent twice", () => {
  const dir = mkdtempSync(join(tmpdir(), "planner-once-"));
  try {
    assert.equal(reservePlan("r1", dir).ok, true);
    assert.equal(reservePlan("r1", dir).ok, false);
    assert.equal(reservePlan("../r1", dir).ok, false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("planner prompt includes bound output path, no user-selected target", () => {
  const prompt = plannerPrompt("r1", { goal: { state: "ASSIGNING" } }, { SPECTRE_PACKET_DIR: "/tmp/packets" });
  assert.match(prompt, /\/tmp\/packets\/r1.json/);
  assert.match(prompt, /request_id/);
  assert.throws(() => plannerPrompt("../x", {}));
});
