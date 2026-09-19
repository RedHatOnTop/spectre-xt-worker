#!/usr/bin/env node
// End-to-end check for the DevSpace -> ChatGPT -> devcodex bridge.
//
// Drives the real DevSpace HTTP MCP endpoint the way the ChatGPT connector
// does: dynamic client registration, owner-token authorization with PKCE,
// then open_workspace / exec_command against a scratch Git workspace, running
// the devcodex CLI through the shell tool ChatGPT is told to use.
//
// Usage:
//   node tests/devspace-devcodex-e2e.mjs [--base http://127.0.0.1:7676]
//
// Reads ~/.devspace/auth.json (owner token) and ~/.devspace/config.json
// (publicBaseUrl, allowedRoots). Never prints the token.
//
// Two of the checks pin the advertised skill path: the $HOME-expanded form
// must read, and the tilde form DevSpace actually advertises must read too
// (that one needs scripts/patch-devspace-tilde.sh — RUNBOOK 7.14).

import { execFileSync } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { appendFile, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

const args = process.argv.slice(2)
const baseIndex = args.indexOf('--base')
const BASE = (baseIndex >= 0 ? args[baseIndex + 1] : 'http://127.0.0.1:7676').replace(/\/+$/, '')
const PROTOCOL_VERSION = '2025-11-25'
const REDIRECT_URI = 'http://127.0.0.1:8765/callback'

let failures = 0

function check(ok, label, detail) {
  const suffix = detail ? ` (${detail})` : ''
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label}${suffix}`)
  if (!ok) failures += 1
}

function httpRequest(urlString, { method = 'GET', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request(new URL(urlString), { method, headers }, (response) => {
      const chunks = []
      response.on('data', (chunk) => chunks.push(chunk))
      response.on('end', () => resolve({
        status: response.statusCode,
        headers: response.headers,
        text: Buffer.concat(chunks).toString('utf8')
      }))
    })
    request.on('error', reject)
    if (body !== undefined) request.write(body)
    request.end()
  })
}

function parseBody(text) {
  const trimmed = (text || '').trim()
  if (!trimmed) return undefined
  const candidates = [trimmed]
  for (const line of trimmed.split('\n')) {
    if (line.startsWith('data:')) candidates.push(line.slice(5).trim())
  }
  for (const candidate of candidates) {
    if (!candidate.startsWith('{') && !candidate.startsWith('[')) continue
    try {
      return JSON.parse(candidate)
    } catch {}
  }
  return { raw: trimmed }
}

async function oauthAccessToken(base, ownerToken, resource) {
  const registration = await httpRequest(`${base}/register`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      client_name: 'devcodex-e2e',
      redirect_uris: [REDIRECT_URI],
      token_endpoint_auth_method: 'none',
      grant_types: ['authorization_code'],
      response_types: ['code'],
      scope: 'devspace'
    })
  })
  if (registration.status !== 201 && registration.status !== 200) {
    throw new Error(`registration failed: ${registration.status} ${registration.text.slice(0, 200)}`)
  }
  const clientId = parseBody(registration.text)?.client_id
  if (!clientId) throw new Error('registration returned no client_id')

  const verifier = randomBytes(32).toString('base64url')
  const challenge = createHash('sha256').update(verifier).digest('base64url')
  const authorize = await httpRequest(`${base}/authorize`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      response_type: 'code',
      client_id: clientId,
      redirect_uri: REDIRECT_URI,
      code_challenge: challenge,
      code_challenge_method: 'S256',
      scope: 'devspace',
      state: 'devcodex-e2e',
      resource,
      owner_token: ownerToken
    }).toString()
  })
  if (authorize.status !== 302) {
    throw new Error(`authorize expected 302, got ${authorize.status} ${authorize.text.slice(0, 200)}`)
  }
  const code = new URL(authorize.headers.location).searchParams.get('code')
  if (!code) throw new Error('authorize redirect carried no code')

  const token = await httpRequest(`${base}/token`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      redirect_uri: REDIRECT_URI,
      client_id: clientId,
      code_verifier: verifier
    }).toString()
  })
  if (token.status !== 200) throw new Error(`token exchange failed: ${token.status} ${token.text.slice(0, 200)}`)
  const accessToken = parseBody(token.text)?.access_token
  if (!accessToken) throw new Error('token response had no access_token')
  return accessToken
}

function createMcpClient(base, accessToken) {
  let sessionId
  let protocolVersion = PROTOCOL_VERSION
  let nextId = 1

  async function call(method, params, { notification = false } = {}) {
    const payload = notification
      ? { jsonrpc: '2.0', method, params }
      : { jsonrpc: '2.0', id: nextId++, method, params }
    const response = await httpRequest(`${base}/mcp`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
        authorization: `Bearer ${accessToken}`,
        ...(sessionId ? { 'mcp-session-id': sessionId, 'mcp-protocol-version': protocolVersion } : {})
      },
      body: JSON.stringify(payload)
    })
    if (response.headers['mcp-session-id']) sessionId = response.headers['mcp-session-id']
    if (response.status >= 400) {
      throw new Error(`${method} failed: ${response.status} ${response.text.slice(0, 300)}`)
    }
    const json = parseBody(response.text)
    if (json?.result?.protocolVersion) protocolVersion = json.result.protocolVersion
    if (json?.error) throw new Error(`${method} returned JSON-RPC error ${json.error.code}: ${json.error.message}`)
    return json
  }

  return { call }
}

function toolOutput(response) {
  const structured = response?.result?.structuredContent || {}
  const parts = (response?.result?.content || []).map((block) => block.text || '')
  const text = parts.length > 0 ? parts.join('\n') : (typeof structured.result === 'string' ? structured.result : '')
  const start = text.indexOf('{')
  const end = text.lastIndexOf('}')
  let json
  if (start !== -1 && end > start) {
    try {
      json = JSON.parse(text.slice(start, end + 1))
    } catch {}
  }
  return { text, json, exitCode: response?.result?.structuredContent?.exitCode }
}

async function main() {
  const authFile = path.join(os.homedir(), '.devspace', 'auth.json')
  const configFile = path.join(os.homedir(), '.devspace', 'config.json')
  const ownerToken = JSON.parse(await readFile(authFile, 'utf8')).ownerToken
  const config = JSON.parse(await readFile(configFile, 'utf8'))
  const resource = `${config.publicBaseUrl.replace(/\/+$/, '')}/mcp`
  const allowedRoot = config.allowedRoots[0].replace(/^~(?=\/|$)/, os.homedir())

  console.log(`DevSpace base: ${BASE}`)
  console.log(`OAuth resource: ${resource}`)

  const accessToken = await oauthAccessToken(BASE, ownerToken, resource)
  check(Boolean(accessToken), 'OAuth: registered client, authorized with owner token, exchanged access token')

  const mcp = createMcpClient(BASE, accessToken)
  const initialized = await mcp.call('initialize', {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: {},
    clientInfo: { name: 'devcodex-e2e', version: '1.0.0' }
  })
  check(initialized?.result?.serverInfo?.name === 'devspace', 'MCP: initialize answered', initialized?.result?.serverInfo?.name)
  await mcp.call('notifications/initialized', {}, { notification: true })

  const toolsResponse = await mcp.call('tools/list', {})
  const toolNames = (toolsResponse?.result?.tools || []).map((tool) => tool.name)
  const shellTool = toolNames.includes('bash') ? 'bash' : toolNames.includes('exec_command') ? 'exec_command' : undefined
  check(toolNames.includes('open_workspace') && Boolean(shellTool), 'MCP: tool surface includes open_workspace + a shell tool', `${toolNames.length} tools, shell=${shellTool}`)
  check(!toolNames.some((name) => name.startsWith('devcodex_')), 'MCP: devcodex tools are NOT aggregated (expected: shell-driven)', toolNames.filter((name) => name.includes('devcodex')).join(',') || 'none')

  const scratch = await mkdtemp(path.join(allowedRoot, '.e2e-devcodex-'))
  try {
    execFileSync('git', ['init', '-q'], { cwd: scratch })
    await writeFile(path.join(scratch, 'README.md'), '# devcodex e2e scratch\n')
    await writeFile(path.join(scratch, '.devcodex.json'), `${JSON.stringify({ gates: [{ name: 'e2e-gate', command: 'true', timeoutMs: 30000 }] }, null, 2)}\n`)
    execFileSync('git', ['add', '-A'], { cwd: scratch })
    execFileSync('git', ['-c', 'user.email=e2e@local', '-c', 'user.name=e2e', 'commit', '-q', '-m', 'scratch'], { cwd: scratch })

    const opened = await mcp.call('tools/call', { name: 'open_workspace', arguments: { path: scratch } })
    const workspaceId = opened?.result?.structuredContent?.workspaceId
    check(Boolean(workspaceId), 'open_workspace returned a workspaceId')
    const skills = opened?.result?.structuredContent?.skills || []
    const devcodexSkill = skills.find((skill) => skill.name === 'devcodex')
    check(Boolean(devcodexSkill), 'open_workspace advertises the devcodex skill', skills.map((skill) => skill.name).join(','))
    if (devcodexSkill) check(devcodexSkill.path.endsWith('skills/devcodex/SKILL.md'), 'skill path is the SKILL.md ChatGPT will read', devcodexSkill.path)
    if (devcodexSkill) {
      const absoluteSkillPath = devcodexSkill.path.replace(/^~(?=\/|$)/, os.homedir())
      const advertisedRead = await mcp.call('tools/call', { name: 'read', arguments: { workspaceId, path: devcodexSkill.path } })
      const absoluteRead = await mcp.call('tools/call', { name: 'read', arguments: { workspaceId, path: absoluteSkillPath } })
      const advertisedText = (advertisedRead?.result?.content || []).map((block) => block.text || '').join('\n')
      const absoluteText = (absoluteRead?.result?.content || []).map((block) => block.text || '').join('\n')
      check(!absoluteRead?.result?.isError && absoluteText.includes('devcodex bootstrap'), 'read: skill file is readable via the $HOME-expanded path', absoluteSkillPath)
      // Required, not a note: DevSpace advertises the tilde form and tells the
      // model to read exactly that path, so a build that resolves "~" against
      // the workspace turns the skill into an ENOENT the model has to guess
      // around. scripts/patch-devspace-tilde.sh fixes it (RUNBOOK 7.14).
      check(!advertisedRead?.result?.isError && advertisedText.includes('devcodex bootstrap'), 'read: the advertised "~" skill path resolves to $HOME (needs scripts/patch-devspace-tilde.sh)', devcodexSkill.path)
    }

    async function shell(command) {
      const response = await mcp.call('tools/call', {
        name: shellTool,
        arguments: shellTool === 'exec_command'
          ? { workspaceId, cmd: command, yieldTimeMs: 30000, maxOutputTokens: 50000 }
          : { workspaceId, command, timeout: 120 }
      })
      const output = toolOutput(response)
      output.isError = Boolean(response?.result?.isError)
      if (output.exitCode === undefined && output.isError) output.exitCode = 1
      return output
    }

    const bootstrap = await shell('devcodex bootstrap "devspace e2e" --json')
    const sessionId = bootstrap.json?.session?.id
    check(!bootstrap.isError && Boolean(sessionId), 'shell: devcodex bootstrap created a session', sessionId)

    await appendFile(path.join(scratch, 'README.md'), 'changed by e2e\n')
    const changes = await shell('devcodex changes --json')
    const changedFiles = (changes.json?.files || []).map((file) => file.path)
    check(!changes.isError && changedFiles.includes('README.md'), 'shell: devcodex changes sees the edit', changedFiles.join(','))

    const verify = await shell('devcodex verify --json')
    check(!verify.isError && verify.json?.ok === true, 'shell: devcodex verify passes the gate')

    const note = await shell(`devcodex session-note '${sessionId}' decision "e2e exercised the ChatGPT shell path" --json`)
    check(!note.isError && Boolean(note.json), 'shell: devcodex session-note recorded a decision')

    const complete = await shell(`devcodex complete --session '${sessionId}' --json`)
    check(!complete.isError && complete.json?.ok === true && complete.json?.sessionUpdated === true, 'shell: devcodex complete closes the session with gates green')
  } finally {
    await rm(scratch, { recursive: true, force: true })
  }
}

main().then(() => {
  console.log(failures === 0 ? '\nE2E PASS' : `\nE2E FAIL (${failures} failing checks)`)
  process.exitCode = failures === 0 ? 0 : 1
}).catch((error) => {
  console.error(`E2E ERROR: ${error.message}`)
  process.exitCode = 1
})
