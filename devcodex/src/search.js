const { readdir, readFile, stat } = require('node:fs/promises')
const path = require('node:path')

const DEFAULT_IGNORED_DIRECTORIES = new Set([
  '.git',
  '.devcodex',
  'coverage',
  'dist',
  'build',
  'node_modules',
  'target',
  '.next',
  '.cache'
])

const DEFAULT_MAX_FILE_BYTES = 1024 * 1024

function normalizeOptions(options = {}) {
  return {
    ignoredDirectories: new Set([
      ...DEFAULT_IGNORED_DIRECTORIES,
      ...(options.ignoredDirectories || [])
    ]),
    maxFileBytes: options.maxFileBytes || DEFAULT_MAX_FILE_BYTES,
    maxDepth: Number.isInteger(options.maxDepth) ? options.maxDepth : 8,
    maxResults: Number.isInteger(options.maxResults) ? options.maxResults : 200
  }
}

async function listEntries(root, options = {}) {
  const normalized = normalizeOptions(options)

  async function visit(directory, depth) {
    if (depth > normalized.maxDepth) return []

    const entries = await readdir(directory, { withFileTypes: true })
    const sorted = [...entries].sort((left, right) => left.name.localeCompare(right.name))
    const visible = sorted.filter((entry) => !normalized.ignoredDirectories.has(entry.name))
    const results = []

    for (const entry of visible) {
      const absolutePath = path.join(directory, entry.name)
      const relativePath = path.relative(root, absolutePath)

      if (entry.isDirectory()) {
        const children = await visit(absolutePath, depth + 1)
        results.push({ path: relativePath, type: 'directory', depth })
        results.push(...children)
      } else if (entry.isFile()) {
        results.push({ path: relativePath, type: 'file', depth })
      }
    }

    return results
  }

  return await visit(root, 0)
}

async function buildTree(root, options = {}) {
  const entries = await listEntries(root, options)
  return entries.map((entry) => `${'  '.repeat(entry.depth)}${entry.path.split(path.sep).at(-1)}${entry.type === 'directory' ? '/' : ''}`)
}

function makeMatcher(query, options = {}) {
  if (options.regex) {
    const expression = new RegExp(query, options.caseSensitive ? 'g' : 'gi')
    return (line) => expression.test(line)
  }

  const needle = options.caseSensitive ? query : query.toLowerCase()
  return (line) => {
    const haystack = options.caseSensitive ? line : line.toLowerCase()
    return haystack.includes(needle)
  }
}

async function readSearchableFile(absolutePath, maxFileBytes) {
  const metadata = await stat(absolutePath)
  if (metadata.size > maxFileBytes) return null

  const content = await readFile(absolutePath)
  if (content.includes(0)) return null
  return content.toString('utf8')
}

async function searchWorkspace(root, query, options = {}) {
  if (typeof query !== 'string' || query.length === 0) {
    throw new Error('Search query must be a non-empty string')
  }

  const normalized = normalizeOptions(options)
  const entries = await listEntries(root, normalized)
  const files = entries.filter((entry) => entry.type === 'file')
  const matcher = makeMatcher(query, options)
  const results = []

  for (const file of files) {
    if (results.length >= normalized.maxResults) break

    const absolutePath = path.join(root, file.path)
    const content = await readSearchableFile(absolutePath, normalized.maxFileBytes)
    if (content === null) continue

    const lines = content.split(/\r?\n/)
    for (const [index, line] of lines.entries()) {
      if (results.length >= normalized.maxResults) break
      if (!matcher(line)) continue

      results.push({
        path: file.path,
        line: index + 1,
        text: line.trimEnd()
      })
    }
  }

  return results
}

function tokenize(value) {
  return [...new Set(
    value
      .toLowerCase()
      .split(/[^a-z0-9_./-]+/)
      .map((token) => token.trim())
      .filter((token) => token.length >= 2)
  )]
}

async function rankRelevantFiles(root, task, options = {}) {
  const normalized = normalizeOptions(options)
  const taskTokens = tokenize(task)
  if (taskTokens.length === 0) return []

  const entries = await listEntries(root, normalized)
  const files = entries.filter((entry) => entry.type === 'file')
  const ranked = []

  for (const file of files) {
    const absolutePath = path.join(root, file.path)
    const content = await readSearchableFile(absolutePath, normalized.maxFileBytes)
    if (content === null) continue

    const lowerPath = file.path.toLowerCase()
    const lowerContent = content.toLowerCase()
    const score = taskTokens.reduce((total, token) => {
      const pathScore = lowerPath.includes(token) ? 4 : 0
      const contentScore = lowerContent.includes(token) ? 1 : 0
      return total + pathScore + contentScore
    }, 0)

    if (score > 0) {
      ranked.push({ path: file.path, score })
    }
  }

  return [...ranked]
    .sort((left, right) => right.score - left.score || left.path.localeCompare(right.path))
    .slice(0, options.limit || 12)
}

module.exports = {
  buildTree,
  listEntries,
  rankRelevantFiles,
  searchWorkspace,
  tokenize
}
