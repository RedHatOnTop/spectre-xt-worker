import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { openAgent, persistSession, authorize } from '../scripts/dsh-worker-session.mjs';

const options = { sessionId: 'random-session', meta: { cwd: '/work' },
  agentOptions: { provider: 'clinepass', model: 'approved-model' }, setup: () => {} };

function agent(id) {
  return { agent: { id, status: 'idle', options: options.agentOptions,
    session: { id, header: { cwd: '/work' }, seq: 3 },
    inbox: { nextTurn: [], nextStep: [], clear: () => {} } } };
}

test('create and resume retain exact session and immutable model options', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-session-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const env = { SPECTRE_DSH_SESSION_ID: 'session-task_1', SPECTRE_DSH_SESSION_RECEIPT: join(root, 'create.json'),
    SPECTRE_DSH_SESSION_RESUME: '0' };
  const original = structuredClone({ ...options, setup: undefined });
  const calls = [];
  const agents = { create: async (value) => { calls.push(['create', value]); return agent(value.sessionId); },
    resume: async (value) => { calls.push(['resume', value]); return agent(value.resumeSessionId); } };
  await openAgent(agents, options, env);
  await openAgent(agents, options, { ...env, SPECTRE_DSH_SESSION_RESUME: '1',
    SPECTRE_DSH_SESSION_RECEIPT: join(root, 'resume.json') });
  assert.equal(calls[0][1].sessionId, 'session-task_1');
  assert.equal(calls[1][1].resumeSessionId, 'session-task_1');
  assert.equal(calls[1][1].agentOptions, options.agentOptions);
  assert.deepEqual({ ...options, setup: undefined }, original);
  const receipt = JSON.parse(await readFile(join(root, 'resume.json')));
  assert.equal(receipt.runtime.pid, process.pid);
  assert.ok(receipt.runtime.start_ticks > 0);
  assert.equal(typeof receipt.runtime.boot_id, 'string');
  assert.deepEqual({ ...receipt, runtime: undefined }, {
    session_id: 'session-task_1', resumed: true, provider: 'clinepass', model: 'approved-model',
    workspace_path: '/work', pending_input_ids: [], first_seq: 3, runtime: undefined });
  assert.equal((await stat(join(root, 'resume.json'))).mode & 0o777, 0o600);
});

test('resume fences old pending input before publication and preserves its identities', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-session-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const handle = agent('session-task');
  const pending = [{ id: 'old-turn' }];
  const step = [{ id: 'old-step' }];
  const subject = { ...handle.agent, inbox: { nextTurn: pending, nextStep: step,
    clear: () => { assert.ok(setupFinished); cleared = true; } } };
  let setupFinished = false;
  let cleared = false;
  const selected = { ...options, setup: () => { setupFinished = true; } };
  const path = join(root, 'resume.json');
  await openAgent({ resume: async (value) => {
    await value.setup({}, subject);
    assert.equal(cleared, true);
    return { agent: subject };
  } }, selected, { SPECTRE_DSH_SESSION_ID: 'session-task', SPECTRE_DSH_SESSION_RECEIPT: path,
    SPECTRE_DSH_SESSION_RESUME: '1' });
  assert.deepEqual(JSON.parse(await readFile(path)).pending_input_ids, ['old-step', 'old-turn']);
  assert.deepEqual(pending, [{ id: 'old-turn' }]);
});

test('runtime workspace, route and active-driver mismatch fail before receipt or input', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-session-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const changes = [{ status: 'running' }, { session: { id: 'session-task', header: { cwd: '/other' } } },
    { options: { provider: 'another', model: 'approved-model' } }];
  for (const [index, change] of changes.entries()) {
    const path = join(root, `attempt-${index}.json`);
    const subject = { ...agent('session-task').agent, ...change };
    await assert.rejects(openAgent({ create: async (value) => {
      await value.setup({}, subject);
      return { agent: subject };
    } }, options, { SPECTRE_DSH_SESSION_ID: 'session-task', SPECTRE_DSH_SESSION_RECEIPT: path,
      SPECTRE_DSH_SESSION_RESUME: '0' }), /identity|workspace|route|idle/);
    await assert.rejects(readFile(path), { code: 'ENOENT' });
  }
});

test('concurrent claim allows only one runtime open and failed claim cannot be reused', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-session-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const env = { SPECTRE_DSH_SESSION_ID: 'session-task', SPECTRE_DSH_SESSION_RECEIPT: join(root, 'attempt.json'),
    SPECTRE_DSH_SESSION_RESUME: '0' };
  let calls = 0;
  const agents = { create: async () => { calls += 1; throw new Error('provider setup failure'); } };
  const result = await Promise.allSettled([openAgent(agents, options, env), openAgent(agents, options, env)]);
  assert.equal(calls, 1);
  assert.equal(result.filter((value) => value.status === 'rejected').length, 2);
  await assert.rejects(openAgent(agents, options, env), { code: 'EEXIST' });
  assert.equal(calls, 1);
});

test('invalid session receipt or mode never starts an agent', async () => {
  let called = false;
  const agents = { create: async () => { called = true; } };
  for (const env of [{}, { SPECTRE_DSH_SESSION_ID: '../escape' },
    { SPECTRE_DSH_SESSION_ID: 'session-task', SPECTRE_DSH_SESSION_RECEIPT: '/private/x', SPECTRE_DSH_SESSION_RESUME: 'unknown' }]) {
    await assert.rejects(openAgent(agents, options, env), /requires/);
  }
  assert.equal(called, false);
});

test('mismatched runtime identity never publishes session evidence', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-session-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const path = join(root, 'receipt.json');
  await assert.rejects(openAgent({ create: async () => agent('other-session') }, options,
    { SPECTRE_DSH_SESSION_ID: 'session-task', SPECTRE_DSH_SESSION_RECEIPT: path, SPECTRE_DSH_SESSION_RESUME: '0' }),
  /another session/);
  await assert.rejects(readFile(path), { code: 'ENOENT' });
});

test('existing receipt prevents duplicate agent launch before runtime work', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-session-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const path = join(root, 'receipt.json');
  await persistSession(path, { session_id: 'session-task' });
  let called = false;
  await assert.rejects(openAgent({ create: async () => { called = true; return agent('session-task'); } }, options,
    { SPECTRE_DSH_SESSION_ID: 'session-task', SPECTRE_DSH_SESSION_RECEIPT: path, SPECTRE_DSH_SESSION_RESUME: '0' }),
  /receipt already exists/);
  assert.equal(called, false);
});

test('native session cannot open without exact current authority; explicit guard rejects stale scope', async () => {
  await assert.rejects(authorize({ SPECTRE_DSH_SESSION_ID: 'session-native-task' }), /authority intent/);
  const env = { SPECTRE_DSH_SESSION_ID: 'session-native-task', SPECTRE_DSH_AUTHORITY_INTENT: '/private/dispatch/intent.json' };
  const calls = [];
  await authorize(env, async (command, args) => {
    calls.push([command, args]);
    return { stdout: '{"authorized":true,"session_id":"session-native-task"}' };
  });
  assert.deepEqual(calls, [['/usr/local/bin/spectre-dsh-execution', ['authorize', '--intent', '/private/dispatch/intent.json']]]);
  await assert.rejects(authorize(env, async () => ({ stdout: '{"authorized":true,"session_id":"another"}' })), /authority/);
  await assert.rejects(authorize(env, async () => { throw new Error('native owner changed'); }), /native owner changed/);
});
