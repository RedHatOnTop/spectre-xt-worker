import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/;
const HERE = dirname(fileURLToPath(import.meta.url));

export function planGuard(worker, entry, snapshot, requestId) {
  if (worker !== "minecraft" || !entry.planner?.terminal || entry.tmux) {
    return { ok: false, detail: "plan requires the minecraft native planner pin" };
  }
  if (!ID.test(String(requestId || "")) || snapshot.goal?.state !== "ASSIGNING" ||
      snapshot.goal?.assignment_id !== requestId) {
    return { ok: false, detail: "plan requires the active assignment request_id" };
  }
  return { ok: true };
}

export function plannerPrompt(requestId, snapshot, env = process.env) {
  if (!ID.test(String(requestId || ""))) throw new Error("invalid request_id");
  const local = join(HERE, "../config/astra-plan-prompt.md");
  const file = env.SPECTRE_ASTRA_PROMPT || (existsSync(local) ? local : "/usr/local/share/remote-agent/astra-plan-prompt.md");
  const directory = env.SPECTRE_PACKET_DIR || join(homedir(), ".local/state/remote-agent/packets");
  const context = { request_id: requestId, worker: "minecraft", wake_reason: "completed",
    output_file: join(directory, `${requestId}.json`), snapshot };
  return `${readFileSync(file, "utf8").trim()}\nRequest data: ${JSON.stringify(context)}`;
}

export function reservePlan(requestId, directory) {
  if (!ID.test(String(requestId || ""))) return { ok: false, error: "invalid request_id" };
  try {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const fd = openSync(join(directory, `${requestId}.sent`), "wx", 0o600);
    try {
      writeFileSync(fd, "delivery reserved; never retry blindly\n");
      fsyncSync(fd);
    } finally { closeSync(fd); }
    const dir = openSync(directory, "r");
    try { fsyncSync(dir); } finally { closeSync(dir); }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error.code === "EEXIST" ? "request already reserved" : error.message };
  }
}
