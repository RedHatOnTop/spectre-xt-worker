const { readFile, stat } = require('node:fs/promises')
const path = require('node:path')

const { refreshWorkspaceIndex, searchWorkspaceIndex } = require('./index')

const INSTRUCTION_NAMES = ['AGENTS.md', 'CLAUDE.md']
const DEFAULT_MAX_BYTES = 1024 * 1024

function resolveWorkspacePath(root, requestedPath) {
  if (typeof requestedPath !== 'string' || requestedPath.trim() === '') {
    throw new Error('Path must be a non-empty relative path')
  }
  if (path.isAbsolute(requestedPath)) {
    throw new Error('Path must be a relative path inside the workspace')
  }

  const absoluteRoot = path.resolve(root)
  const target = path.resolve(absoluteRoot, requestedPath)
  if (target !== absoluteRoot && !target.startsWith(`${absoluteRoot}${path.sep}`)) {
    throw new Error('Path resolves outside workspace root')
  }
  return target
}

async function readWorkspaceFile(root, requestedPath, options = {}) {
  const target = resolveWorkspacePath(root, requestedPath)
  const metadata = await stat(target)
  if (!metadata.isFile()) throw new Error(`Not a regular file: ${requestedPath}`)

  const maxBytes = options.maxBytes || DEFAULT_MAX_BYTES
  if (metadata.size > maxBytes) {
    throw new Error(`File exceeds read limit of ${maxBytes} bytes: ${requestedPath}`)
  }

  const buffer = await readFile(target)
  if (buffer.includes(0)) throw new Error(`Binary file cannot be read as text: ${requestedPath}`)

  const lines = buffer.toString('utf8').split(/\r?\n/)
  const offset = Number.isInteger(options.offset) && options.offset > 0 ? options.offset : 1
  const limit = Number.isInteger(options.limit) && options.limit > 0 ? options.limit : 200
  const startIndex = Math.min(offset - 1, lines.length)
  const endIndex = Math.min(startIndex + limit, lines.length)

  return {
    path: path.relative(path.resolve(root), target),
    startLine: startIndex + 1,
    endLine: endIndex,
    totalLines: lines.length,
    truncated: startIndex > 0 || endIndex < lines.length,
    content: lines.slice(startIndex, endIndex).join('\n')
  }
}

async function maybeReadInstruction(root, relativePath) {
  try {
    const result = await readWorkspaceFile(root, relativePath, { limit: 1000, maxBytes: 256 * 1024 })
    return { path: result.path, content: result.content, truncated: result.truncated }
  } catch (error) {
    if (error.code === 'ENOENT') return null
    throw error
  }
}

async function findInstructionsForPath(root, requestedPath) {
  const target = resolveWorkspacePath(root, requestedPath)
  let directory = target
  try {
    const metadata = await stat(target)
    if (metadata.isFile()) directory = path.dirname(target)
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
    directory = path.dirname(target)
  }

  const absoluteRoot = path.resolve(root)
  const relativeDirectory = path.relative(absoluteRoot, directory)
  const segments = relativeDirectory ? relativeDirectory.split(path.sep) : []
  const levels = [[], ...segments.map((_, index) => segments.slice(0, index + 1))]
  const candidates = levels.flatMap((parts) => INSTRUCTION_NAMES.map((name) => path.join(...parts, name)))
  const instructions = await Promise.all(candidates.map((candidate) => maybeReadInstruction(root, candidate)))
  return instructions.filter(Boolean)
}

async function readMany(root, requests) {
  if (!Array.isArray(requests) || requests.length === 0) throw new Error('files must be a non-empty array')
  if (requests.length > 20) throw new Error('readMany accepts at most 20 files per call')

  return await Promise.all(requests.map((request) => {
    if (!request || typeof request !== 'object' || Array.isArray(request)) throw new Error('Each file request must be an object')
    return readWorkspaceFile(root, request.path, {
      offset: request.offset,
      limit: request.limit,
      maxBytes: request.maxBytes
    })
  }))
}

async function inspectFile(root, requestedPath, options = {}) {
  const [file, instructions] = await Promise.all([
    readWorkspaceFile(root, requestedPath, options),
    findInstructionsForPath(root, requestedPath)
  ])
  return { file, instructions }
}

function mergeSearchWindows(matches, contextLines) {
  return matches.reduce((windows, match) => {
    const start = Math.max(1, match.line - contextLines)
    const end = match.line + contextLines
    const previous = windows.at(-1)
    if (previous && previous.path === match.path && start <= previous.end + 1) {
      const merged = {
        ...previous,
        end: Math.max(previous.end, end),
        matchLines: [...previous.matchLines, match.line]
      }
      return [...windows.slice(0, -1), merged]
    }
    return [...windows, { path: match.path, start, end, line: match.line, text: match.text, matchLines: [match.line] }]
  }, [])
}

async function searchContext(root, query, options = {}) {
  const maxMatches = Number.isInteger(options.maxMatches) && options.maxMatches > 0
    ? Math.min(options.maxMatches, 50)
    : 8
  const contextLines = Number.isInteger(options.contextLines) && options.contextLines >= 0
    ? Math.min(options.contextLines, 50)
    : 3
  await refreshWorkspaceIndex(root)
  const rawMatches = await searchWorkspaceIndex(root, query, {
    regex: options.regex === true,
    caseSensitive: options.caseSensitive === true,
    maxResults: maxMatches
  })

  const windows = mergeSearchWindows(rawMatches, contextLines)
  const matches = await Promise.all(windows.map(async (window) => {
    const file = await readWorkspaceFile(root, window.path, {
      offset: window.start,
      limit: window.end - window.start + 1
    })
    return {
      path: window.path,
      line: window.line,
      text: window.text,
      matchLines: window.matchLines,
      startLine: file.startLine,
      endLine: file.endLine,
      content: file.content
    }
  }))

  return { query, matches }
}

module.exports = {
  DEFAULT_MAX_BYTES,
  INSTRUCTION_NAMES,
  findInstructionsForPath,
  inspectFile,
  mergeSearchWindows,
  readMany,
  readWorkspaceFile,
  resolveWorkspacePath,
  searchContext
}
