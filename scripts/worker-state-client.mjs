// UDS HTTP client for spectre-worker-state. No resolver copy.
import http from "node:http";
import { join } from "node:path";

export const FAIL_CLOSED_POLICY = {
  can_dispatch_goal: false,
  can_resume: false,
  continuity_eligible: false,
  continuity_recovery_allowed: false,
  grokbot_may_advance: false,
};

export function defaultSocketPath() {
  const override = String(process.env.SPECTRE_WORKER_STATE_SOCK || "").trim();
  if (override) return override;
  const runtime = String(process.env.XDG_RUNTIME_DIR || "").trim();
  if (runtime) return join(runtime, "spectre-worker-state.sock");
  return `/run/user/${process.getuid()}/spectre-worker-state.sock`;
}

export function failClosedSnapshot(worker, error) {
  return {
    ok: false,
    schema_version: 1,
    snapshot_version: 0,
    worker,
    goal: {
      goal_id: null,
      turn_id: null,
      dispatch_id: null,
      attempt_id: null,
      state: "UNKNOWN",
      park_reason: null,
    },
    transport: { kind: null, state: "UNKNOWN" },
    observation: { state: "UNKNOWN" },
    execution: { progress_seq: 0, current_operation: null, stalled: false, stall_reason: null },
    policy: { ...FAIL_CLOSED_POLICY },
    evidence: { kind: null, source: null, journal_seq: 0 },
    reason: `state api unavailable: ${error}`,
    debug: { unavailable: true },
  };
}

export function request(method, path, body, socketPath, timeoutMs = 5000) {
  const sock = socketPath || defaultSocketPath();
  const payload = body == null ? null : Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        socketPath: sock,
        path,
        method,
        timeout: timeoutMs,
        headers: {
          Host: "localhost",
          Accept: "application/json",
          ...(payload
            ? { "Content-Type": "application/json", "Content-Length": String(payload.length) }
            : {}),
        },
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const raw = Buffer.concat(chunks).toString("utf8");
          let parsed = {};
          try {
            parsed = JSON.parse(raw || "{}");
          } catch {
            parsed = { ok: false, error: "bad_response", detail: raw.slice(0, 200) };
          }
          resolve({ status: res.statusCode || 0, payload: parsed });
        });
      },
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("timeout"));
    });
    if (payload) req.write(payload);
    req.end();
  });
}

export async function getSnapshot(worker, socketPath) {
  try {
    const { status, payload } = await request("GET", `/v1/workers/${worker}/snapshot`, null, socketPath);
    if (status !== 200 || !payload || typeof payload !== "object") {
      return failClosedSnapshot(worker, (payload && (payload.detail || payload.error)) || String(status));
    }
    return payload;
  } catch (err) {
    return failClosedSnapshot(worker, String((err && err.message) || err));
  }
}

export async function claimAction(body, socketPath) {
  return request("POST", "/v1/actions/claim", body, socketPath);
}

export async function actionResult(actionId, ok, error, socketPath) {
  const body = { ok: Boolean(ok) };
  if (error) body.error = error;
  return request("POST", `/v1/actions/${actionId}/result`, body, socketPath);
}
