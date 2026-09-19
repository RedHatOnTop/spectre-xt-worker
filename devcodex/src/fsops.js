const { readFile, mkdir, stat, writeFile } = require('node:fs/promises')
const path = require('node:path')

const { runCommand } = require('./gates')
const { resolveWorkspacePath } = require('./inspect')

// Workspace-scoped write and command tools (box posture, 2026-09-17 user
// decision). DevCodex no longer stays read-only on this box: the host agent
// gets one working surface here instead of a second execution layer, and the
// destructive-command barrier remains the agent guard hook outside this
// runtime. Paths resolve through the same workspace-root check the read tools
// use, so a write cannot leave the fixed root.

async function writeWorkspaceFile(root, requestedPath, content) {
  if (typeof content !== 'string') throw new Error('content must be a string')
  const absoluteRoot = path.resolve(root)
  const target = resolveWorkspacePath(root, requestedPath)
  let existed = true
  try {
    await stat(target)
  } catch {
    existed = false
  }
  await mkdir(path.dirname(target), { recursive: true })
  await writeFile(target, content)
  return {
    path: path.relative(absoluteRoot, target),
    bytes: Buffer.byteLength(content, 'utf8'),
    mode: existed ? 'replaced' : 'created'
  }
}

async function editWorkspaceFile(root, requestedPath, oldText, newText, options = {}) {
  if (typeof oldText !== 'string' || oldText === '') throw new Error('oldText must be a non-empty string')
  if (typeof newText !== 'string') throw new Error('newText must be a string')
  const target = resolveWorkspacePath(root, requestedPath)
  const buffer = await readFile(target)
  if (buffer.includes(0)) throw new Error(`Binary file cannot be edited as text: ${requestedPath}`)
  const current = buffer.toString('utf8')
  const occurrences = current.split(oldText).length - 1
  if (occurrences === 0) throw new Error(`oldText was not found in ${requestedPath}`)
  const replaceAll = options.replaceAll === true
  if (occurrences > 1 && !replaceAll) {
    throw new Error(`oldText occurs ${occurrences} times in ${requestedPath} - pass replaceAll to replace every occurrence`)
  }
  const next = replaceAll ? current.split(oldText).join(newText) : current.replace(oldText, newText)
  await writeFile(target, next)
  return {
    path: path.relative(path.resolve(root), target),
    replaced: replaceAll ? occurrences : 1
  }
}

async function runShellCommand(root, command, options = {}) {
  if (typeof command !== 'string' || command.trim() === '') throw new Error('command must be a non-empty string')
  const normalized = command.trim()
  const result = await runCommand(root, { name: 'command', command: normalized, timeoutMs: options.timeoutMs })
  return {
    command: normalized,
    ok: result.ok,
    exitCode: result.exitCode,
    signal: result.signal,
    timedOut: result.timedOut,
    stdout: result.stdout,
    stderr: result.stderr,
    startedAt: result.startedAt,
    durationMs: result.durationMs
  }
}

module.exports = {
  editWorkspaceFile,
  runShellCommand,
  writeWorkspaceFile
}
