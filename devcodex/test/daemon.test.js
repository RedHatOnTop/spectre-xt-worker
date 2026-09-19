const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { Readable, Writable } = require('node:stream')
const test = require('node:test')

const {
  connectDaemon,
  getDaemonStatus,
  serveDaemonProxyStdio,
  startDaemonServer,
  stopDaemonServer
} = require('../src/daemon')

test('shared daemon serves the MCP registry over authenticated loopback JSONL', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-daemon-'))
  let daemon
  try {
    await writeFile(path.join(root, 'sample.js'), 'const sample = 1\n')
    daemon = await startDaemonServer(root, { profile: 'core' })
    const status = await getDaemonStatus(root)
    assert.equal(status.status, 'running')
    assert.equal(status.port, daemon.port)

    const client = await connectDaemon(root)
    const ping = await client.request({ jsonrpc: '2.0', id: 1, method: 'ping', params: {} })
    assert.equal(ping.result && typeof ping.result, 'object')

    const tools = await client.request({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} })
    assert.ok(tools.result.tools.some((tool) => tool.name === 'task_bootstrap'))
    assert.equal(tools.result.tools.some((tool) => tool.name === 'queue_add'), false)
    await client.close()
  } finally {
    if (daemon) await stopDaemonServer(root, daemon)
    const status = await getDaemonStatus(root)
    assert.equal(status.status, 'stopped')
    await rm(root, { recursive: true, force: true })
  }
})

test('daemon rejects an invalid authentication token', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-daemon-'))
  let daemon
  try {
    daemon = await startDaemonServer(root)
    await assert.rejects(
      connectDaemon(root, { token: 'definitely-wrong' }),
      /authentication failed/i
    )
  } finally {
    if (daemon) await stopDaemonServer(root, daemon)
    await rm(root, { recursive: true, force: true })
  }
})

test('stdio proxy forwards MCP JSON-RPC to the shared daemon', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-daemon-'))
  let daemon
  try {
    daemon = await startDaemonServer(root, { profile: 'core' })
    const input = Readable.from([
      `${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'ping', params: {} })}\n`,
      `${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} })}\n`,
      `${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} })}\n`
    ])
    let outputText = ''
    const output = new Writable({
      write(chunk, encoding, callback) {
        outputText += chunk.toString()
        callback()
      }
    })
    const errorOutput = new Writable({ write(chunk, encoding, callback) { callback() } })

    await serveDaemonProxyStdio(root, { input, output, errorOutput })
    const responses = outputText.trim().split('\n').map((line) => JSON.parse(line))
    assert.deepEqual(responses.map((response) => response.id), [1, 2])
    assert.ok(responses[1].result.tools.some((tool) => tool.name === 'search_context'))
  } finally {
    if (daemon) await stopDaemonServer(root, daemon)
    await rm(root, { recursive: true, force: true })
  }
})
