-- spectre-worker-state schema v1
-- snapshot rows are a cache; evidence_journal is the source of truth.

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE evidence_journal (
  journal_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  source_timestamp TEXT,
  received_at TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  goal_id TEXT,
  turn_id TEXT,
  dispatch_id TEXT,
  attempt_id INTEGER,
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  authority INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  ignored INTEGER NOT NULL DEFAULT 0,
  ignore_reason TEXT
);
CREATE INDEX idx_evidence_worker_seq ON evidence_journal (worker_id, journal_seq);

CREATE TABLE worker_snapshots (
  worker_id TEXT PRIMARY KEY,
  snapshot_version INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL,
  journal_seq INTEGER NOT NULL,
  resolver_version TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE action_claims (
  action_id TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL,
  action TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  expected_snapshot_version INTEGER NOT NULL,
  snapshot_version_at_claim INTEGER NOT NULL,
  state TEXT NOT NULL,
  delivery_state TEXT,
  goal_id TEXT,
  turn_id TEXT,
  dispatch_id TEXT,
  attempt_id INTEGER,
  claimed_at TEXT NOT NULL,
  result_at TEXT,
  result_json TEXT
);
CREATE INDEX idx_claims_worker_state ON action_claims (worker_id, state);

CREATE TABLE goal_bindings (
  worker_id TEXT NOT NULL,
  goal_id TEXT NOT NULL,
  turn_id TEXT,
  dispatch_id TEXT NOT NULL,
  attempt_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  terminal_state TEXT,
  PRIMARY KEY (worker_id, goal_id, attempt_id)
);

CREATE TABLE completion_claims (
  worker_id TEXT NOT NULL,
  goal_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  attempt_id INTEGER NOT NULL,
  completion_identity TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  journal_seq INTEGER NOT NULL,
  PRIMARY KEY (worker_id, goal_id, turn_id, attempt_id, completion_identity)
);

CREATE TABLE decision_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id TEXT NOT NULL,
  journal_seq INTEGER,
  snapshot_version INTEGER,
  snapshot_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  resolver_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_audit_worker ON decision_audit (worker_id, audit_id);

CREATE TABLE shadow_divergence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  old_json TEXT NOT NULL,
  new_json TEXT NOT NULL,
  kind TEXT NOT NULL
);
CREATE INDEX idx_shadow_worker ON shadow_divergence (worker_id, id);
