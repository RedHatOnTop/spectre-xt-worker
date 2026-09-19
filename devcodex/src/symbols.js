const { gzip, gunzip } = require('node:zlib')
const { promisify } = require('node:util')
const { mkdir, readFile, writeFile } = require('node:fs/promises')
const path = require('node:path')

const { ensureWorkspaceIndex, refreshWorkspaceIndex, searchWorkspaceIndex } = require('./index')

const gzipAsync = promisify(gzip)
const gunzipAsync = promisify(gunzip)
const SYMBOL_INDEX_VERSION = 1
const SYMBOL_INDEX_PATH = '.devcodex/symbols-v1.json.gz'
const MEMORY_SYMBOLS = new Map()

function symbolIndexPath(root) {
  return path.join(root, SYMBOL_INDEX_PATH)
}

function patternsForExtension(extension) {
  const commonJs = [
    { kind: 'function', regex: /^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/ },
    { kind: 'class', regex: /^(?:export\s+)?class\s+([A-Za-z_$][\w$]*)\b/ },
    { kind: 'interface', regex: /^(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)\b/ },
    { kind: 'type', regex: /^(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\b/ },
    { kind: 'enum', regex: /^(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)\b/ },
    { kind: 'variable', regex: /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b/ }
  ]
  if (['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'].includes(extension)) return commonJs
  if (extension === '.py') return [
    { kind: 'function', regex: /^(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\(/ },
    { kind: 'class', regex: /^class\s+([A-Za-z_][\w]*)\b/ }
  ]
  if (extension === '.rs') return [
    { kind: 'function', regex: /^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)\b/ },
    { kind: 'struct', regex: /^(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_][\w]*)\b/ },
    { kind: 'enum', regex: /^(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_][\w]*)\b/ },
    { kind: 'trait', regex: /^(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_][\w]*)\b/ }
  ]
  if (extension === '.go') return [
    { kind: 'function', regex: /^func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\(/ },
    { kind: 'type', regex: /^type\s+([A-Za-z_][\w]*)\b/ }
  ]
  if (['.java', '.kt', '.kts', '.cs'].includes(extension)) return [
    { kind: 'class', regex: /^(?:public\s+|private\s+|protected\s+|internal\s+|abstract\s+|final\s+|open\s+|sealed\s+)*(?:class)\s+([A-Za-z_][\w]*)\b/ },
    { kind: 'interface', regex: /^(?:public\s+|private\s+|protected\s+|internal\s+)*(?:interface)\s+([A-Za-z_][\w]*)\b/ },
    { kind: 'enum', regex: /^(?:public\s+|private\s+|protected\s+|internal\s+)*(?:enum)\s+([A-Za-z_][\w]*)\b/ }
  ]
  return []
}

function extractSymbols(filePath, content) {
  const patterns = patternsForExtension(path.extname(filePath).toLowerCase())
  if (patterns.length === 0 || typeof content !== 'string') return []
  const symbols = []
  for (const [index, rawLine] of content.split(/\r?\n/).entries()) {
    const line = rawLine.trim()
    if (!line || line.startsWith('//') || line.startsWith('#')) continue
    for (const pattern of patterns) {
      const match = line.match(pattern.regex)
      if (!match) continue
      symbols.push({
        name: match[1],
        kind: pattern.kind,
        path: filePath,
        line: index + 1,
        signature: line.slice(0, 240)
      })
      break
    }
  }
  return symbols
}

async function loadSymbolIndex(root) {
  const key = path.resolve(root)
  if (MEMORY_SYMBOLS.has(key)) return MEMORY_SYMBOLS.get(key)
  try {
    const parsed = JSON.parse((await gunzipAsync(await readFile(symbolIndexPath(root)))).toString('utf8'))
    if (parsed.version !== SYMBOL_INDEX_VERSION) return null
    MEMORY_SYMBOLS.set(key, parsed)
    return parsed
  } catch {
    return null
  }
}

async function buildSymbolIndex(root, options = {}) {
  if (options.refreshWorkspace !== false) await refreshWorkspaceIndex(root)
  const workspace = await ensureWorkspaceIndex(root)
  const existing = await loadSymbolIndex(root)
  if (existing && existing.sourceUpdatedAt === workspace.updatedAt) {
    return { symbols: existing.symbols.length, reused: true, path: SYMBOL_INDEX_PATH }
  }

  const symbols = Object.entries(workspace.entries)
    .flatMap(([filePath, record]) => record.searchable ? extractSymbols(filePath, record.content) : [])
    .sort((left, right) => left.path.localeCompare(right.path) || left.line - right.line || left.name.localeCompare(right.name))
  const index = {
    version: SYMBOL_INDEX_VERSION,
    sourceUpdatedAt: workspace.updatedAt,
    updatedAt: new Date().toISOString(),
    symbols
  }
  const compressed = await gzipAsync(Buffer.from(JSON.stringify(index)), { level: 6 })
  await mkdir(path.dirname(symbolIndexPath(root)), { recursive: true })
  await writeFile(symbolIndexPath(root), compressed)
  MEMORY_SYMBOLS.set(path.resolve(root), index)
  return { symbols: symbols.length, reused: false, compressedBytes: compressed.length, path: SYMBOL_INDEX_PATH }
}

async function ensureSymbolIndex(root) {
  const workspace = await ensureWorkspaceIndex(root)
  const existing = await loadSymbolIndex(root)
  if (existing && existing.sourceUpdatedAt === workspace.updatedAt) return existing
  await buildSymbolIndex(root, { refreshWorkspace: false })
  return await loadSymbolIndex(root)
}

async function searchSymbols(root, query, options = {}) {
  if (typeof query !== 'string' || query.trim() === '') throw new Error('Symbol query must be non-empty')
  const index = await ensureSymbolIndex(root)
  const needle = query.trim().toLowerCase()
  const limit = Number.isInteger(options.limit) ? options.limit : 50
  return index.symbols
    .map((symbol) => {
      const name = symbol.name.toLowerCase()
      const score = name === needle ? 100 : name.startsWith(needle) ? 50 : name.includes(needle) ? 20 : 0
      return { ...symbol, score }
    })
    .filter((symbol) => symbol.score > 0)
    .sort((left, right) => right.score - left.score || left.name.localeCompare(right.name) || left.path.localeCompare(right.path))
    .slice(0, limit)
}

async function outlineFile(root, filePath) {
  const index = await ensureSymbolIndex(root)
  return index.symbols.filter((symbol) => symbol.path === filePath)
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

async function findReferences(root, name, options = {}) {
  if (typeof name !== 'string' || !/^[A-Za-z_$][\w$]*$/.test(name)) throw new Error('Reference symbol must be a simple identifier')
  await refreshWorkspaceIndex(root)
  const [matches, symbols] = await Promise.all([
    searchWorkspaceIndex(root, `\\b${escapeRegex(name)}\\b`, { regex: true, caseSensitive: true, maxResults: options.limit || 200 }),
    ensureSymbolIndex(root)
  ])
  const definitions = new Set(symbols.symbols.filter((symbol) => symbol.name === name).map((symbol) => `${symbol.path}:${symbol.line}`))
  return matches.map((match) => ({ ...match, definition: definitions.has(`${match.path}:${match.line}`) }))
}

module.exports = {
  MEMORY_SYMBOLS,
  SYMBOL_INDEX_PATH,
  SYMBOL_INDEX_VERSION,
  buildSymbolIndex,
  ensureSymbolIndex,
  extractSymbols,
  findReferences,
  outlineFile,
  searchSymbols
}
