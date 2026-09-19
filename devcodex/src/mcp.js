const readline = require('node:readline')

const { loadConfig } = require('./config')
const { createToolRegistry } = require('./tool-registry')

const MODERN_PROTOCOL_VERSION = '2026-07-28'
const LEGACY_PROTOCOL_VERSION = '2025-11-25'
const SERVER_INFO = Object.freeze({
  name: 'devcodex',
  version: '0.7.0',
  title: 'DevCodex'
})
const SERVER_INSTRUCTIONS = 'DevCodex exposes a fixed local workspace for evidence-backed coding-agent work. Start with task_bootstrap, prefer search_context/inspect_file/read_many over repeated small calls, make workspace changes with write_file/edit_file/run_command, use file_diff after edits, and verify before claiming completion. Large results return a handle for result_page instead of flooding the client.'

function serverMeta() {
  return { 'io.modelcontextprotocol/serverInfo': { ...SERVER_INFO } }
}

function success(id, result) {
  return { jsonrpc: '2.0', id, result }
}

function failure(id, code, message, data) {
  return {
    jsonrpc: '2.0',
    id,
    error: data === undefined ? { code, message } : { code, message, data }
  }
}

function toolResult(value) {
  return {
    content: [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    isError: false,
    _meta: serverMeta()
  }
}

function toolError(error) {
  return {
    content: [{ type: 'text', text: error instanceof Error ? error.message : String(error) }],
    isError: true,
    _meta: serverMeta()
  }
}

function discoverResult() {
  return {
    supportedVersions: [MODERN_PROTOCOL_VERSION],
    capabilities: { tools: { listChanged: false } },
    instructions: SERVER_INSTRUCTIONS,
    ttlMs: 300000,
    cacheScope: 'private',
    _meta: serverMeta()
  }
}

function initializeResult(params = {}) {
  const proposed = params.protocolVersion
  return {
    protocolVersion: proposed === LEGACY_PROTOCOL_VERSION ? proposed : LEGACY_PROTOCOL_VERSION,
    capabilities: { tools: { listChanged: false } },
    serverInfo: { ...SERVER_INFO },
    instructions: SERVER_INSTRUCTIONS
  }
}

async function handleMessage(message, context) {
  if (!message || typeof message !== 'object' || Array.isArray(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string') {
    return failure(message && message.id !== undefined ? message.id : null, -32600, 'Invalid Request')
  }

  const isNotification = message.id === undefined
  if (isNotification) {
    if (message.method === 'notifications/initialized' || message.method === 'notifications/cancelled') return null
    return null
  }

  switch (message.method) {
    case 'server/discover':
      return success(message.id, discoverResult())
    case 'initialize':
      return success(message.id, initializeResult(message.params))
    case 'ping':
      return success(message.id, {})
    case 'tools/list':
      return success(message.id, {
        tools: context.registry.list(),
        ttlMs: 300000,
        cacheScope: 'private',
        _meta: serverMeta()
      })
    case 'tools/call': {
      const params = message.params && typeof message.params === 'object' ? message.params : {}
      try {
        const value = await context.registry.call(params.name, params.arguments || {})
        return success(message.id, toolResult(value))
      } catch (error) {
        return success(message.id, toolError(error))
      }
    }
    default:
      return failure(message.id, -32601, `Method not found: ${message.method}`)
  }
}

async function handlePayload(payload, context) {
  if (Array.isArray(payload)) {
    if (payload.length === 0) return failure(null, -32600, 'Invalid Request')
    const responses = await Promise.all(payload.map((message) => handleMessage(message, context)))
    const visible = responses.filter((response) => response !== null)
    return visible.length > 0 ? visible : null
  }
  return await handleMessage(payload, context)
}

function writeProtocolMessage(output, message) {
  output.write(`${JSON.stringify(message)}\n`)
}

async function serveStdio(root, options = {}) {
  const input = options.input || process.stdin
  const output = options.output || process.stdout
  const errorOutput = options.errorOutput || process.stderr
  const config = await loadConfig(root)
  const registry = options.registry || createToolRegistry(root, {
    profile: options.profile || config.mcpProfile,
    resultBudgetChars: options.resultBudgetChars || config.resultBudgetChars
  })
  const lines = readline.createInterface({ input, crlfDelay: Infinity })

  for await (const line of lines) {
    if (line.trim() === '') continue
    let payload
    try {
      payload = JSON.parse(line)
    } catch (error) {
      writeProtocolMessage(output, failure(null, -32700, 'Parse error', error.message))
      continue
    }

    try {
      const response = await handlePayload(payload, { registry })
      if (response !== null) writeProtocolMessage(output, response)
    } catch (error) {
      errorOutput.write(`devcodex mcp internal error: ${error.message}\n`)
      const id = payload && !Array.isArray(payload) && payload.id !== undefined ? payload.id : null
      writeProtocolMessage(output, failure(id, -32603, 'Internal error'))
    }
  }
}

module.exports = {
  LEGACY_PROTOCOL_VERSION,
  MODERN_PROTOCOL_VERSION,
  SERVER_INFO,
  discoverResult,
  handleMessage,
  handlePayload,
  initializeResult,
  serveStdio,
  toolError,
  toolResult,
  writeProtocolMessage
}
