// End-to-end check of the deployed devcodex (RUNBOOK 7.13): the CLI task
// lifecycle in a scratch git workspace (bootstrap -> changes -> verify ->
// session-note -> write/edit/run -> complete) and an MCP stdio handshake
// that must list the 14 core tools. Node >= 20, no dependencies. Copy to the
// box and run as the worker user, with `devcodex` on PATH:
//
//   node devcodex-smoke.mjs
//
// Exit 0 only when every check passes.

import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync, appendFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const CORE_TOOLS = [
  'code_navigation',
  'completion_gate',
  'edit_file',
  'file_diff',
  'inspect_file',
  'note_task_session',
  'read_many',
  'result_page',
  'run_command',
  'search_context',
  'show_changes',
  'task_bootstrap',
  'verify_workspace',
  'write_file',
];

function run(args, cwd) {
  return execFileSync('devcodex', args, { cwd, encoding: 'utf8' });
}

function runJson(args, cwd) {
  return JSON.parse(run(args, cwd));
}

function git(args, cwd) {
  execFileSync('git', ['-c', 'user.email=smoke@local', '-c', 'user.name=smoke', ...args], {
    cwd,
    encoding: 'utf8',
  });
}

function check(description, ok, detail = '') {
  if (!ok) throw new Error(`${description}${detail ? ` — ${detail}` : ''}`);
  console.log(`PASS  ${description}`);
}

async function mcpHandshake(root) {
  const child = spawn('devcodex', ['mcp', '--root', root], { stdio: ['pipe', 'pipe', 'pipe'] });
  let buffer = '';
  const responses = new Map();
  const done = new Promise((resolve, reject) => {
    child.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      let index;
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (!line) continue;
        const message = JSON.parse(line);
        if (message.id !== undefined) responses.set(message.id, message);
        if (responses.has(1) && responses.has(2)) resolve();
      }
    });
    child.on('error', reject);
    setTimeout(() => reject(new Error('MCP handshake timed out after 15 s')), 15000).unref();
  });

  const send = (message) => child.stdin.write(`${JSON.stringify(message)}\n`);
  send({
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'smoke', version: '0' } },
  });
  send({ jsonrpc: '2.0', method: 'notifications/initialized' });
  send({ jsonrpc: '2.0', id: 2, method: 'tools/list' });

  await done;
  const exited = await new Promise((resolve) => {
    child.on('exit', (code) => resolve(code));
    child.stdin.end();
    setTimeout(() => {
      child.kill('SIGKILL');
      resolve(null);
    }, 5000).unref();
  });

  return { initialize: responses.get(1), tools: responses.get(2)?.result?.tools ?? [], exitCode: exited };
}

const workspace = mkdtempSync(path.join(tmpdir(), 'devcodex-smoke-'));
try {
  git(['init', '-q'], workspace);
  writeFileSync(path.join(workspace, 'README.md'), '# smoke\n');
  writeFileSync(
    path.join(workspace, '.devcodex.json'),
    `${JSON.stringify({ gates: [{ name: 'smoke-gate', command: 'true', timeoutMs: 30000 }] }, null, 2)}\n`
  );
  git(['add', '-A'], workspace);
  git(['commit', '-q', '-m', 'baseline'], workspace);

  const bootstrap = runJson(['bootstrap', 'smoke test', '--json'], workspace);
  const sessionId = bootstrap.session?.id;
  check('bootstrap starts a task session', Boolean(sessionId), JSON.stringify(bootstrap.session));
  check('bootstrap reports a baseline', Boolean(bootstrap.session?.baseline?.head));

  let changes = runJson(['changes', '--json'], workspace);
  check('changes reports a clean tree after the baseline commit', changes.clean === true);

  appendFileSync(path.join(workspace, 'README.md'), 'smoke change\n');
  changes = runJson(['changes', '--json'], workspace);
  check(
    'changes sees the edit',
    changes.clean === false && changes.files.some((file) => file.path === 'README.md'),
    JSON.stringify(changes.files)
  );

  const verify = runJson(['verify', '--json'], workspace);
  check('verify passes the configured gate', verify.ok === true, JSON.stringify(verify.results));

  runJson(['session-note', sessionId, 'verification', 'smoke note', '--json'], workspace);

  writeFileSync(path.join(workspace, 'local-src.txt'), 'written via devcodex\n');
  const written = runJson(['write', 'notes/smoke.txt', '--from', path.join(workspace, 'local-src.txt'), '--json'], workspace);
  check('write creates nested workspace files', written.path === 'notes/smoke.txt' && written.mode === 'created', JSON.stringify(written));

  const edited = runJson(['edit', 'notes/smoke.txt', '--old', 'via devcodex', '--new', 'via edit', '--json'], workspace);
  check('edit replaces exact text', edited.replaced === 1, JSON.stringify(edited));

  const ran = runJson(['run', 'printf', 'smoke-ran', '--json'], workspace);
  check('run executes in the workspace root', ran.ok === true && ran.stdout.includes('smoke-ran'), JSON.stringify(ran));

  const complete = runJson(['complete', '--session', sessionId, '--json'], workspace);
  check('completion gate passes and closes the session', complete.ok === true && complete.sessionUpdated === true);

  const handshake = await mcpHandshake(workspace);
  check('MCP initialize answers as devcodex', handshake.initialize?.result?.serverInfo?.name === 'devcodex');
  const names = handshake.tools.map((tool) => tool.name).sort();
  check(
    'MCP core profile lists exactly the 14 tools',
    JSON.stringify(names) === JSON.stringify(CORE_TOOLS),
    names.join(', ')
  );
  check('MCP server exits 0 on stdin close', handshake.exitCode === 0, `exit ${handshake.exitCode}`);

  console.log('devcodex smoke: all checks passed');
} catch (error) {
  console.error(`FAIL: ${error.message}`);
  process.exitCode = 1;
} finally {
  rmSync(workspace, { recursive: true, force: true });
}
