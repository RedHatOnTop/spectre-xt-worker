const { watch } = require('node:fs')
const { appendFile, mkdir, readFile } = require('node:fs/promises')
const path = require('node:path')

const { refreshWorkspaceIndexPaths } = require('./index')

const EVENT_PATH = '.devcodex/workspace-events.jsonl'
const IGNORED_PREFIXES = [
  '.devcodex/', '.git/', 'node_modules/', 'target/', 'dist/', 'build/', 'coverage/', '.next/', '.cache/'
]

function normalizeEventPath(filename) {
  return String(filename || '').replaceAll('\\', '/').replace(/^\.\//, '')
}

function ignoredEventPath(filePath) {
  return !filePath || IGNORED_PREFIXES.some((prefix) => filePath === prefix.slice(0, -1) || filePath.startsWith(prefix))
}

async function appendEvents(root, events) {
  if (events.length === 0) return
  const target = path.join(root, EVENT_PATH)
  await mkdir(path.dirname(target), { recursive: true })
  await appendFile(target, `${events.map((event) => JSON.stringify(event)).join('\n')}\n`)
}

async function readWorkspaceEvents(root, options = {}) {
  const target = path.join(root, EVENT_PATH)
  let content
  try {
    content = await readFile(target, 'utf8')
  } catch (error) {
    if (error.code === 'ENOENT') return []
    throw error
  }
  const sinceSeq = Number.isInteger(options.sinceSeq) ? options.sinceSeq : -1
  const limit = Number.isInteger(options.limit) ? options.limit : 100
  return content.split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((event) => event.seq > sinceSeq)
    .slice(-limit)
}

async function startWorkspaceWatcher(root, options = {}) {
  let seq = 0
  const previous = await readWorkspaceEvents(root, { limit: 1 })
  if (previous.length > 0) seq = previous[0].seq
  let pending = new Map()
  let timer = null
  let closed = false

  const flush = async () => {
    if (closed || pending.size === 0) return
    const batch = [...pending.values()]
    pending = new Map()
    await appendEvents(root, batch)
    await refreshWorkspaceIndexPaths(root, batch.map((event) => event.path))
    if (typeof options.onBatch === 'function') await options.onBatch(batch)
  }

  const schedule = () => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      flush().catch((error) => {
        if (typeof options.onError === 'function') options.onError(error)
      })
    }, Number.isInteger(options.debounceMs) ? options.debounceMs : 50)
  }

  const watcher = watch(root, { recursive: true }, (eventType, filename) => {
    const filePath = normalizeEventPath(filename)
    if (ignoredEventPath(filePath)) return
    seq += 1
    pending.set(filePath, {
      seq,
      at: new Date().toISOString(),
      type: eventType,
      path: filePath
    })
    schedule()
  })

  return {
    get sequence() { return seq },
    close: async () => {
      if (closed) return
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
      await flush()
      closed = true
      watcher.close()
    }
  }
}

module.exports = {
  EVENT_PATH,
  IGNORED_PREFIXES,
  ignoredEventPath,
  normalizeEventPath,
  readWorkspaceEvents,
  startWorkspaceWatcher
}
