const { appendFile, mkdir, readFile } = require('node:fs/promises')
const path = require('node:path')

const JOURNAL_PATH = '.devcodex/journal.jsonl'

async function appendJournalEvent(root, event) {
  if (!event || typeof event.type !== 'string' || event.type.trim() === '') {
    throw new Error('Journal event requires a type')
  }
  const record = {
    id: `${Date.now()}-${process.pid}-${Math.random().toString(16).slice(2, 10)}`,
    at: new Date().toISOString(),
    ...event,
    type: event.type.trim()
  }
  const target = path.join(root, JOURNAL_PATH)
  await mkdir(path.dirname(target), { recursive: true })
  await appendFile(target, `${JSON.stringify(record)}\n`)
  return record
}

async function readJournal(root, options = {}) {
  const target = path.join(root, JOURNAL_PATH)
  let content
  try {
    content = await readFile(target, 'utf8')
  } catch (error) {
    if (error.code === 'ENOENT') return []
    throw error
  }
  const records = content
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((record) => !options.type || record.type === options.type)
  const limit = Number.isInteger(options.limit) && options.limit > 0 ? options.limit : 100
  return records.slice(-limit)
}

module.exports = {
  JOURNAL_PATH,
  appendJournalEvent,
  readJournal
}
