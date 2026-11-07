import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { open, lstat, realpath, link, unlink, readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { promisify } from 'node:util';

const SESSION_ID = /^session-[A-Za-z0-9][A-Za-z0-9_.:-]{0,200}$/;
const execute = promisify(execFile);

export async function authorize(env, run = execute) {
  if (!env.SPECTRE_DSH_SESSION_ID?.startsWith('session-native-')) return;
  const intent = env.SPECTRE_DSH_AUTHORITY_INTENT;
  if (!intent?.startsWith('/') || intent.includes('\0')) throw new Error('DSH native session requires an authority intent');
  const result = await run('/usr/local/bin/spectre-dsh-execution', ['authorize', '--intent', intent],
    { env, timeout: 30000, maxBuffer: 65536 });
  const receipt = JSON.parse(result.stdout);
  if (receipt.authorized !== true || receipt.session_id !== env.SPECTRE_DSH_SESSION_ID) {
    throw new Error('DSH native input authority does not match its session');
  }
}

async function syncDirectory(path) {
  const directory = await open(path, 'r');
  try {
    await directory.sync();
  } finally {
    await directory.close();
  }
}

async function runtimeIdentity() {
  const stat = await readFile(`/proc/${process.pid}/stat`, 'utf8');
  const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
  const bootId = (await readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim();
  return { pid: process.pid, boot_id: bootId, start_ticks: Number(fields[19]) };
}

export async function persistSession(path, value) {
  const body = `${JSON.stringify(value)}\n`;
  const temporary = join(dirname(path), `.session-${randomUUID()}.tmp`);
  const file = await open(temporary, 'wx', 0o600);
  try {
    try {
      await file.writeFile(body);
      await file.sync();
    } finally {
      await file.close();
    }
    await link(temporary, path);
  } finally {
    await unlink(temporary);
  }
  await syncDirectory(dirname(path));
}

function validateAgent(agent, sessionId, options) {
  if (agent.id !== sessionId || agent.session.id !== sessionId) {
    throw new Error('DSH returned another session identity');
  }
  if (agent.status !== 'idle') throw new Error('DSH worker requires an idle unpublished driver');
  if (agent.session.header.cwd !== options.meta.cwd) throw new Error('DSH session belongs to another workspace');
  if (['provider', 'model'].some((key) => agent.options[key] !== options.agentOptions[key])) {
    throw new Error('DSH session changed its approved route');
  }
}

export async function openAgent(agents, options, env) {
  await authorize(env);
  const sessionId = env.SPECTRE_DSH_SESSION_ID;
  const receipt = env.SPECTRE_DSH_SESSION_RECEIPT;
  if (!SESSION_ID.test(sessionId || '') || !receipt?.startsWith('/') || receipt.includes('\0')) {
    throw new Error('DSH worker requires an exact session ID and absolute receipt path');
  }
  if (!['0', '1'].includes(env.SPECTRE_DSH_SESSION_RESUME)) {
    throw new Error('DSH worker requires an explicit create or resume mode');
  }
  const directory = await lstat(dirname(receipt));
  if (resolve(receipt) !== receipt || await realpath(dirname(receipt)) !== dirname(receipt)
    || !directory.isDirectory() || directory.uid !== process.getuid() || (directory.mode & 0o777) !== 0o700) {
    throw new Error('DSH session receipt requires a private owned directory without symlinks');
  }
  try {
    await lstat(receipt);
    throw new Error('DSH session receipt already exists; inspect the original attempt');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const claim = await open(`${receipt}.claim`, 'wx', 0o600);
  try {
    await claim.writeFile(JSON.stringify({ session_id: sessionId }));
    await claim.sync();
  } finally {
    await claim.close();
  }
  await syncDirectory(dirname(receipt));
  const resumed = env.SPECTRE_DSH_SESSION_RESUME === '1';
  let pendingInputIds = [];
  const setup = async (ctx, agent) => {
    validateAgent(agent, sessionId, options);
    const commit = await options.setup?.(ctx, agent);
    validateAgent(agent, sessionId, options);
    if (resumed) {
      pendingInputIds = [...agent.inbox.nextStep, ...agent.inbox.nextTurn].map((message) => message.id);
      agent.inbox.clear();
    }
    return commit;
  };
  const handle = resumed
    ? await agents.resume({ resumeSessionId: sessionId, agentOptions: options.agentOptions, setup })
    : await agents.create({ ...options, sessionId, setup });
  validateAgent(handle.agent, sessionId, options);
  await persistSession(receipt, { session_id: sessionId, resumed, provider: options.agentOptions.provider,
    model: options.agentOptions.model, workspace_path: options.meta.cwd,
    pending_input_ids: pendingInputIds, first_seq: handle.agent.session.seq, runtime: await runtimeIdentity() });
  return handle;
}
