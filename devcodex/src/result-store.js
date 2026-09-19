const { randomUUID } = require('node:crypto')
const { mkdir, readFile, writeFile } = require('node:fs/promises')
const path = require('node:path')

const RESULT_DIR = '.devcodex/results'
const DEFAULT_MAX_CHARS = 12000
const DEFAULT_PREVIEW_CHARS = 1600
const DEFAULT_PAGE_CHARS = 8000

function normalizeHandle(handle) {
  const value = String(handle || '')
  if (!/^result-[a-zA-Z0-9-]+$/.test(value)) throw new Error('Invalid result handle')
  return value
}

function resultPath(root, handle) {
  return path.join(root, RESULT_DIR, `${normalizeHandle(handle)}.json`)
}

async function storeResult(root, serialized) {
  const handle = `result-${randomUUID()}`
  await mkdir(path.join(root, RESULT_DIR), { recursive: true })
  await writeFile(resultPath(root, handle), serialized)
  return handle
}

async function boundResult(root, value, options = {}) {
  const serialized = JSON.stringify(value, null, 2)
  const maxChars = options.maxChars || DEFAULT_MAX_CHARS
  if (serialized.length <= maxChars) return value

  const handle = await storeResult(root, serialized)
  const previewChars = Math.min(options.previewChars || DEFAULT_PREVIEW_CHARS, maxChars)
  return {
    truncated: true,
    handle,
    totalChars: serialized.length,
    preview: serialized.slice(0, previewChars),
    hint: 'Use result_page with this handle to retrieve more of the stored JSON result.'
  }
}

async function readResultPage(root, handle, options = {}) {
  const serialized = await readFile(resultPath(root, handle), 'utf8')
  const offset = Number.isInteger(options.offset) && options.offset >= 0 ? options.offset : 0
  const limit = Number.isInteger(options.limit) && options.limit > 0
    ? Math.min(options.limit, 50000)
    : DEFAULT_PAGE_CHARS
  const end = Math.min(offset + limit, serialized.length)

  return {
    handle: normalizeHandle(handle),
    offset,
    totalChars: serialized.length,
    content: serialized.slice(offset, end),
    nextOffset: end < serialized.length ? end : null
  }
}

module.exports = {
  DEFAULT_MAX_CHARS,
  DEFAULT_PAGE_CHARS,
  DEFAULT_PREVIEW_CHARS,
  RESULT_DIR,
  boundResult,
  normalizeHandle,
  readResultPage,
  resultPath,
  storeResult
}
