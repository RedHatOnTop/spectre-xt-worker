const { randomBytes } = require('node:crypto')
const net = require('node:net')
const readline = require('node:readline')
const { mkdir, readFile, writeFile } = require('node:fs/promises')
const path = require('node:path')

const { handlePayload } = require('./mcp')
const { createToolRegistry } = require('./tool-registry')
const { startWorkspaceWatcher } = require('./workspace-watch')

const DAEMON_STATE_PATH = '.devcodex/daemon.json'

function statePath(root) {
  return path.join(root, DAEMON_STATE_PATH)
}

async function writeDaemonState(root, state) {
  await mkdir(path.dirname(statePath(root)), { recursive: true })
  await writeFile(statePath(root), `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 })
  return state
}

async function readDaemonState(root) {
  try {
    return JSON.parse(await readFile(statePath(root), 'utf8'))
  } catch (error) {
    if (error.code === 'ENOENT') return null
    throw error
  }
}

function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

async function getDaemonStatus(root) {
  const state = await readDaemonState(root)
  if (!state) return { status: 'unconfigured', path: DAEMON_STATE_PATH }
  if (state.status === 'running' && !processAlive(state.pid)) {
    return { ...state, status: 'stale', path: DAEMON_STATE_PATH }
  }
  const { token, ...safe } = state
  return { ...safe, path: DAEMON_STATE_PATH }
}

async function startDaemonServer(root, options = {}) {
  const existing = await readDaemonState(root)
  if (existing && existing.status === 'running' && processAlive(existing.pid)) {
    throw new Error(`DevCodex daemon is already running on ${existing.host}:${existing.port}`)
  }

  const profile = options.profile || 'core'
  const registry = options.registry || createToolRegistry(root, { profile, liveIndex: true })
  const token = randomBytes(32).toString('hex')
  const host = options.host || '127.0.0.1'
  const server = net.createServer((socket) => {
    let authenticated = false
    const lines = readline.createInterface({ input: socket, crlfDelay: Infinity })
    lines.on('line', async (line) => {
      let message
      try {
        message = JSON.parse(line)
      } catch {
        socket.write(`${JSON.stringify({ ok: false, error: 'invalid-json' })}\n`)
        return
      }

      if (!authenticated) {
        if (!message || message.devcodexAuth !== token) {
          socket.write(`${JSON.stringify({ ok: false, error: 'authentication-failed' })}\n`)
          socket.end()
          return
        }
        authenticated = true
        socket.write(`${JSON.stringify({ ok: true, server: 'devcodex-daemon' })}\n`)
        return
      }

      try {
        const response = await handlePayload(message, { registry })
        if (response !== null) socket.write(`${JSON.stringify(response)}\n`)
      } catch (error) {
        socket.write(`${JSON.stringify({ jsonrpc: '2.0', id: message && message.id !== undefined ? message.id : null, error: { code: -32603, message: error.message } })}\n`)
      }
    })
  })

  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(options.port || 0, host, resolve)
  })
  const address = server.address()
  const state = {
    status: 'running',
    pid: process.pid,
    host,
    port: address.port,
    profile,
    token,
    startedAt: new Date().toISOString()
  }
  await writeDaemonState(root, state)
  const watcher = options.watch === false ? null : await startWorkspaceWatcher(root, {
    onError: options.onWatchError
  })
  return { server, watcher, ...state }
}

async function stopDaemonServer(root, daemon) {
  if (daemon && daemon.watcher) await daemon.watcher.close()
  if (daemon && daemon.server && daemon.server.listening) {
    await new Promise((resolve) => daemon.server.close(resolve))
  }
  const current = await readDaemonState(root)
  if (!current) return { status: 'stopped', stoppedAt: new Date().toISOString() }
  return await writeDaemonState(root, {
    ...current,
    status: 'stopped',
    stoppedAt: new Date().toISOString()
  })
}

async function connectDaemon(root, options = {}) {
  const state = await readDaemonState(root)
  if (!state || state.status !== 'running') throw new Error('DevCodex daemon is not running')
  const token = options.token || state.token
  const socket = net.createConnection({ host: state.host, port: state.port })
  const lines = readline.createInterface({ input: socket, crlfDelay: Infinity })
  const iterator = lines[Symbol.asyncIterator]()
  await new Promise((resolve, reject) => {
    socket.once('connect', resolve)
    socket.once('error', reject)
  })
  socket.write(`${JSON.stringify({ devcodexAuth: token })}\n`)
  const authLine = await iterator.next()
  if (authLine.done) throw new Error('Daemon authentication failed: connection closed')
  const auth = JSON.parse(authLine.value)
  if (!auth.ok) {
    socket.destroy()
    throw new Error(`Daemon authentication failed: ${auth.error || 'unknown error'}`)
  }

  return {
    state: { host: state.host, port: state.port, profile: state.profile },
    request: async (payload) => {
      const message = payload && typeof payload === 'object' ? payload : {}
      socket.write(`${JSON.stringify(message)}\n`)
      if (message.id === undefined) return null
      const line = await iterator.next()
      if (line.done) throw new Error('Daemon connection closed while awaiting response')
      return JSON.parse(line.value)
    },
    close: async () => {
      socket.end()
      lines.close()
    }
  }
}

async function serveDaemonProxyStdio(root, options = {}) {
  const input = options.input || process.stdin
  const output = options.output || process.stdout
  const errorOutput = options.errorOutput || process.stderr
  const client = await connectDaemon(root)
  const lines = readline.createInterface({ input, crlfDelay: Infinity })

  try {
    for await (const line of lines) {
      if (line.trim() === '') continue
      let payload
      try {
        payload = JSON.parse(line)
      } catch (error) {
        output.write(`${JSON.stringify({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error', data: error.message } })}\n`)
        continue
      }

      try {
        const response = await client.request(payload)
        if (response !== null) output.write(`${JSON.stringify(response)}\n`)
      } catch (error) {
        errorOutput.write(`devcodex daemon proxy error: ${error.message}\n`)
        const id = payload && !Array.isArray(payload) && payload.id !== undefined ? payload.id : null
        output.write(`${JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32603, message: 'Daemon proxy error' } })}\n`)
      }
    }
  } finally {
    await client.close()
  }
}

module.exports = {
  DAEMON_STATE_PATH,
  connectDaemon,
  getDaemonStatus,
  processAlive,
  readDaemonState,
  serveDaemonProxyStdio,
  startDaemonServer,
  stopDaemonServer,
  writeDaemonState
}
