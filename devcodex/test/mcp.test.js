const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { createToolRegistry, TOOL_PERMISSION_ACTIONS } = require('../src/tool-registry')
const { handleMessage, handlePayload, MODERN_PROTOCOL_VERSION } = require('../src/mcp')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

async function createWorkspace() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-mcp-'))
  await git(root, ['init', '-q'])
  await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
  await git(root, ['config', 'user.name', 'DevCodex Test'])
  await mkdir(path.join(root, 'src'))
  await writeFile(path.join(root, 'src', 'app.js'), '// TODO: improve\nmodule.exports = 1\n')
  await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({ gates: [{ name: 'tests', command: 'node -e "process.exit(0)"' }] }))
  await git(root, ['add', '.'])
  await git(root, ['commit', '-qm', 'initial'])
  return root
}

test('tool registry exposes agent-oriented operations and executes search_code', async () => {
  const root = await createWorkspace()
  try {
    const registry = createToolRegistry(root)
    const names = registry.list().map((tool) => tool.name)
    assert.ok(names.includes('search_code'))
    assert.ok(names.includes('repo_map'))
    assert.ok(names.includes('review_changes'))
    assert.ok(names.includes('note_task_session'))
    assert.ok(names.includes('task_bootstrap'))
    assert.ok(names.includes('read_many'))
    assert.ok(names.includes('inspect_file'))
    assert.ok(names.includes('search_context'))
    assert.ok(names.includes('file_diff'))
    assert.ok(names.includes('result_page'))
    assert.ok(names.includes('code_navigation'))

    const result = await registry.call('search_code', { query: 'TODO' })
    assert.equal(result[0].path, 'src/app.js')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('core MCP profile keeps the default tool surface compact', async () => {
  const root = await createWorkspace()
  try {
    const registry = createToolRegistry(root, { profile: 'core' })
    const names = registry.list().map((tool) => tool.name)
    const expected = [
      'search_context',
      'code_navigation',
      'read_many',
      'inspect_file',
      'write_file',
      'edit_file',
      'run_command',
      'show_changes',
      'file_diff',
      'task_bootstrap',
      'note_task_session',
      'verify_workspace',
      'result_page',
      'completion_gate'
    ]

    assert.equal(registry.profile, 'core')
    assert.equal(names.length, 14)
    assert.deepEqual([...names].sort(), [...expected].sort())
    assert.equal(names.includes('handoff'), false)
    assert.equal(names.includes('repo_map'), false)
    assert.equal(names.includes('workspace_tree'), false)
    assert.equal(names.includes('review_changes'), false)
    assert.equal(names.includes('queue_add'), false)
    await assert.rejects(() => registry.call('queue_add', {}), /Unknown tool/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('task_bootstrap accepts session recovery without adding another core tool', async () => {
  const root = await createWorkspace()
  try {
    const registry = createToolRegistry(root, { profile: 'core' })
    const tool = registry.list().find((item) => item.name === 'task_bootstrap')
    assert.ok(tool)
    assert.equal(tool.inputSchema.required.includes('task'), false)
    assert.ok(tool.inputSchema.properties.sessionId)
    assert.ok(tool.inputSchema.properties.resumeLatest)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('unknown MCP profiles fail instead of expanding the tool surface', async () => {
  const root = await createWorkspace()
  try {
    assert.throws(() => createToolRegistry(root, { profile: 'cor' }), /Unknown MCP tool profile/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('every advertised MCP tool has an explicit permission classification', async () => {
  const root = await createWorkspace()
  try {
    const registry = createToolRegistry(root, { profile: 'extended' })
    for (const tool of registry.list()) {
      assert.equal(typeof TOOL_PERMISSION_ACTIONS[tool.name], 'string', `missing permission classification for ${tool.name}`)
    }
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('MCP profiles exclude retired orchestration controls', async () => {
  const root = await createWorkspace()
  try {
    const core = createToolRegistry(root, { profile: 'core' })
    const extended = createToolRegistry(root, { profile: 'extended' })
    const coreNames = core.list().map((tool) => tool.name)
    const extendedNames = extended.list().map((tool) => tool.name)
    assert.equal(coreNames.includes('agent_delegate'), false)
    assert.equal(coreNames.includes('agent_providers'), false)
    assert.equal(extendedNames.includes('agent_providers'), false)
    assert.equal(extendedNames.includes('agent_delegate'), false)
    assert.equal(extendedNames.includes('handoff'), false)
    assert.equal(extendedNames.includes('agent_snapshot'), false)
    for (const retired of [
      'start_task_session',
      'task_session_status',
      'task_session_timeline',
      'steer_task_session',
      'fork_task_session',
      'rollback_task_session',
      'set_goal',
      'get_goal',
      'update_goal',
      'consume_goal_budget',
      'queue_add',
      'queue_list',
      'queue_reorder',
      'queue_start_next',
      'queue_cancel',
      'background_process_start',
      'background_process_poll',
      'background_process_list',
      'background_process_stop'
    ]) assert.equal(extendedNames.includes(retired), false)
    await assert.rejects(() => extended.call('agent_delegate', {}), /Unknown tool/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('large MCP tool results are replaced by retrievable handles', async () => {
  const root = await createWorkspace()
  try {
    const lines = Array.from({ length: 350 }, (_, index) => `// TODO: item ${index} ${'x'.repeat(50)}`).join('\n')
    await writeFile(path.join(root, 'src', 'large.js'), `${lines}\n`)
    const registry = createToolRegistry(root)

    const large = await registry.call('search_code', { query: 'TODO', maxResults: 350 })
    assert.equal(large.truncated, true)
    assert.match(large.handle, /^result-/)

    const page = await registry.call('result_page', { handle: large.handle, limit: 2000 })
    assert.equal(page.offset, 0)
    assert.ok(page.content.length > 0)
    assert.ok(page.totalChars > page.content.length)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('MCP handler supports modern discovery and tool listing', async () => {
  const root = await createWorkspace()
  try {
    const registry = createToolRegistry(root)
    const discover = await handleMessage({
      jsonrpc: '2.0', id: 1, method: 'server/discover', params: { _meta: { 'io.modelcontextprotocol/protocolVersion': MODERN_PROTOCOL_VERSION } }
    }, { registry })

    assert.equal(discover.id, 1)
    assert.deepEqual(discover.result.supportedVersions, [MODERN_PROTOCOL_VERSION])
    assert.ok(discover.result.capabilities.tools)

    const list = await handleMessage({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} }, { registry })
    assert.ok(list.result.tools.some((tool) => tool.name === 'build_context'))
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('MCP handler supports legacy initialize and tool calls', async () => {
  const root = await createWorkspace()
  try {
    const registry = createToolRegistry(root)
    const initialized = await handleMessage({
      jsonrpc: '2.0', id: 3, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'test', version: '1' } }
    }, { registry })
    assert.equal(initialized.result.protocolVersion, '2025-11-25')

    const called = await handleMessage({
      jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'search_code', arguments: { query: 'TODO' } }
    }, { registry })
    assert.equal(called.result.isError, false)
    assert.match(called.result.content[0].text, /src\/app.js/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('MCP handler returns JSON-RPC errors for unknown tools', async () => {
  const root = await createWorkspace()
  try {
    const registry = createToolRegistry(root)
    const response = await handleMessage({
      jsonrpc: '2.0', id: 5, method: 'tools/call', params: { name: 'does_not_exist', arguments: {} }
    }, { registry })
    assert.equal(response.result.isError, true)
    assert.match(response.result.content[0].text, /Unknown tool/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('MCP handler supports JSON-RPC batches and suppresses notification responses', async () => {
  const root = await createWorkspace()
  try {
    const registry = createToolRegistry(root)
    const response = await handlePayload([
      { jsonrpc: '2.0', id: 20, method: 'ping', params: {} },
      { jsonrpc: '2.0', method: 'notifications/initialized', params: {} },
      { jsonrpc: '2.0', id: 21, method: 'tools/list', params: {} }
    ], { registry })

    assert.equal(response.length, 2)
    assert.equal(response[0].id, 20)
    assert.equal(response[1].id, 21)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
