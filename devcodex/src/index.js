const { gzip, gunzip } = require('node:zlib')
const { promisify } = require('node:util')
const { mkdir, readFile, stat, writeFile } = require('node:fs/promises')
const path = require('node:path')

const { listEntries, tokenize } = require('./search')

const gzipAsync = promisify(gzip)
const gunzipAsync = promisify(gunzip)
const INDEX_VERSION = 1
const INDEX_PATH = '.devcodex/index-v1.json.gz'
const DEFAULT_MAX_FILE_BYTES = 1024 * 1024
const MEMORY_INDEX = new Map()

function indexPath(root) {
  return path.join(root, INDEX_PATH)
}

async function loadWorkspaceIndex(root) {
  const cacheKey = path.resolve(root)
  if (MEMORY_INDEX.has(cacheKey)) return MEMORY_INDEX.get(cacheKey)
  try {
    const compressed = await readFile(indexPath(root))
    const parsed = JSON.parse((await gunzipAsync(compressed)).toString('utf8'))
    if (parsed.version !== INDEX_VERSION || !parsed.entries || typeof parsed.entries !== 'object') return null
    MEMORY_INDEX.set(cacheKey, parsed)
    return parsed
  } catch (error) {
    if (error.code === 'ENOENT') return null
    return null
  }
}

async function readIndexableFile(absolutePath, size, maxFileBytes) {
  if (size > maxFileBytes) return { searchable: false, content: null }
  const buffer = await readFile(absolutePath)
  if (buffer.includes(0)) return { searchable: false, content: null }
  return { searchable: true, content: buffer.toString('utf8') }
}

async function mapLimit(items, limit, worker) {
  const results = new Array(items.length)
  let cursor = 0

  async function consume() {
    while (cursor < items.length) {
      const index = cursor
      cursor += 1
      results[index] = await worker(items[index], index)
    }
  }

  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => consume()))
  return results
}

async function refreshWorkspaceIndex(root, options = {}) {
  const maxFileBytes = options.maxFileBytes || DEFAULT_MAX_FILE_BYTES
  const previous = await loadWorkspaceIndex(root)
  const previousEntries = previous ? previous.entries : {}
  const files = (await listEntries(root, { maxDepth: options.maxDepth || 20 }))
    .filter((entry) => entry.type === 'file')
    .sort((left, right) => left.path.localeCompare(right.path))

  let updated = 0
  let reused = 0
  let skipped = 0

  const records = await mapLimit(files, options.concurrency || 32, async (file) => {
    const absolutePath = path.join(root, file.path)
    const metadata = await stat(absolutePath)
    const prior = previousEntries[file.path]

    if (prior && prior.size === metadata.size && prior.mtimeMs === metadata.mtimeMs) {
      reused += 1
      return [file.path, prior]
    }

    const indexed = await readIndexableFile(absolutePath, metadata.size, maxFileBytes)
    updated += 1
    if (!indexed.searchable) skipped += 1
    return [file.path, {
      size: metadata.size,
      mtimeMs: metadata.mtimeMs,
      searchable: indexed.searchable,
      content: indexed.content
    }]
  })

  const now = new Date().toISOString()
  const index = {
    version: INDEX_VERSION,
    createdAt: previous ? previous.createdAt : now,
    updatedAt: now,
    entries: Object.fromEntries(records)
  }
  const compressed = await gzipAsync(Buffer.from(JSON.stringify(index)), { level: 6 })
  await mkdir(path.dirname(indexPath(root)), { recursive: true })
  await writeFile(indexPath(root), compressed)
  MEMORY_INDEX.set(path.resolve(root), index)

  return {
    files: files.length,
    updated,
    reused,
    skipped,
    compressedBytes: compressed.length,
    path: INDEX_PATH
  }
}

async function persistWorkspaceIndex(root, index) {
  const compressed = await gzipAsync(Buffer.from(JSON.stringify(index)), { level: 6 })
  await mkdir(path.dirname(indexPath(root)), { recursive: true })
  await writeFile(indexPath(root), compressed)
  MEMORY_INDEX.set(path.resolve(root), index)
  return compressed.length
}

async function refreshWorkspaceIndexPaths(root, changedPaths, options = {}) {
  if (!Array.isArray(changedPaths) || changedPaths.length === 0) return await getIndexStatus(root)
  let existing = await loadWorkspaceIndex(root)
  if (!existing) {
    await refreshWorkspaceIndex(root, options)
    existing = await loadWorkspaceIndex(root)
  }
  const maxFileBytes = options.maxFileBytes || DEFAULT_MAX_FILE_BYTES
  const entries = { ...existing.entries }
  let updated = 0
  let removed = 0
  let requiresFullRefresh = false

  for (const requestedPath of [...new Set(changedPaths)].sort()) {
    if (typeof requestedPath !== 'string' || requestedPath === '' || requestedPath.startsWith('.devcodex/')) continue
    const absolutePath = path.resolve(root, requestedPath)
    const absoluteRoot = path.resolve(root)
    if (absolutePath !== absoluteRoot && !absolutePath.startsWith(`${absoluteRoot}${path.sep}`)) continue
    try {
      const metadata = await stat(absolutePath)
      if (metadata.isDirectory()) {
        requiresFullRefresh = true
        continue
      }
      if (!metadata.isFile()) continue
      const indexed = await readIndexableFile(absolutePath, metadata.size, maxFileBytes)
      entries[requestedPath] = {
        size: metadata.size,
        mtimeMs: metadata.mtimeMs,
        searchable: indexed.searchable,
        content: indexed.content
      }
      updated += 1
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
      if (Object.hasOwn(entries, requestedPath)) {
        delete entries[requestedPath]
        removed += 1
      }
    }
  }

  if (requiresFullRefresh) return await refreshWorkspaceIndex(root, options)
  const index = { ...existing, updatedAt: new Date().toISOString(), entries }
  const compressedBytes = await persistWorkspaceIndex(root, index)
  return { files: Object.keys(entries).length, updated, removed, compressedBytes, partial: true, path: INDEX_PATH }
}

async function ensureWorkspaceIndex(root, options = {}) {
  const existing = await loadWorkspaceIndex(root)
  if (!existing) {
    await refreshWorkspaceIndex(root, options)
    return await loadWorkspaceIndex(root)
  }

  if (Number.isInteger(options.maxAgeMs) && options.maxAgeMs >= 0) {
    const ageMs = Date.now() - Date.parse(existing.updatedAt)
    if (ageMs > options.maxAgeMs) {
      await refreshWorkspaceIndex(root, options)
      return await loadWorkspaceIndex(root)
    }
  }
  return existing
}

function makeMatcher(query, options = {}) {
  if (options.regex) {
    const expression = new RegExp(query, options.caseSensitive ? '' : 'i')
    return (line) => expression.test(line)
  }
  const needle = options.caseSensitive ? query : query.toLowerCase()
  return (line) => (options.caseSensitive ? line : line.toLowerCase()).includes(needle)
}

async function searchWorkspaceIndex(root, query, options = {}) {
  if (typeof query !== 'string' || query.length === 0) throw new Error('Search query must be a non-empty string')
  const index = await ensureWorkspaceIndex(root, options)
  const matcher = makeMatcher(query, options)
  const maxResults = Number.isInteger(options.maxResults) ? options.maxResults : 200
  const results = []

  for (const [filePath, record] of Object.entries(index.entries).sort(([left], [right]) => left.localeCompare(right))) {
    if (results.length >= maxResults) break
    if (!record.searchable || typeof record.content !== 'string') continue
    const lines = record.content.split(/\r?\n/)
    for (const [lineIndex, line] of lines.entries()) {
      if (results.length >= maxResults) break
      if (matcher(line)) results.push({ path: filePath, line: lineIndex + 1, text: line.trimEnd() })
    }
  }
  return results
}

async function rankRelevantFilesFromIndex(root, task, options = {}) {
  const index = await ensureWorkspaceIndex(root, options)
  const tokens = tokenize(task)
  if (tokens.length === 0) return []
  const ranked = []

  for (const [filePath, record] of Object.entries(index.entries)) {
    if (!record.searchable || typeof record.content !== 'string') continue
    const lowerPath = filePath.toLowerCase()
    const lowerContent = record.content.toLowerCase()
    const score = tokens.reduce((total, token) => total + (lowerPath.includes(token) ? 4 : 0) + (lowerContent.includes(token) ? 1 : 0), 0)
    if (score > 0) ranked.push({ path: filePath, score })
  }

  return ranked
    .sort((left, right) => right.score - left.score || left.path.localeCompare(right.path))
    .slice(0, options.limit || 12)
}

async function getIndexStatus(root) {
  const existing = await loadWorkspaceIndex(root)
  if (!existing) return { available: false, path: INDEX_PATH }
  const metadata = await stat(indexPath(root))
  return {
    available: true,
    version: existing.version,
    files: Object.keys(existing.entries).length,
    updatedAt: existing.updatedAt,
    ageMs: Math.max(0, Date.now() - Date.parse(existing.updatedAt)),
    compressedBytes: metadata.size,
    path: INDEX_PATH
  }
}

module.exports = {
  DEFAULT_MAX_FILE_BYTES,
  INDEX_PATH,
  INDEX_VERSION,
  MEMORY_INDEX,
  ensureWorkspaceIndex,
  getIndexStatus,
  loadWorkspaceIndex,
  rankRelevantFilesFromIndex,
  refreshWorkspaceIndex,
  refreshWorkspaceIndexPaths,
  searchWorkspaceIndex
}
