const { randomUUID } = require('node:crypto')
const { mkdir, readFile, readdir, writeFile } = require('node:fs/promises')
const path = require('node:path')

const { getChangeSummary } = require('./git')

const SESSION_DIR = '.devcodex/sessions'

function makeEvent(type, message, parentId = null, at = new Date().toISOString()) {
  return {
    id: randomUUID(),
    parentId,
    type,
    at,
    message
  }
}

function normalizeSessionRecord(record) {
  const normalizedEvents = (record.events || []).reduce((events, event, index) => {
    const previous = events.at(-1)
    return [
      ...events,
      {
        ...event,
        id: event.id || `legacy-${index + 1}`,
        parentId: event.parentId === undefined ? (previous ? previous.id : null) : event.parentId
      }
    ]
  }, [])
  return {
    ...record,
    schemaVersion: 2,
    revision: Number.isInteger(record.revision) ? record.revision : Math.max(normalizedEvents.length, 1),
    events: normalizedEvents,
    headEventId: record.headEventId || normalizedEvents.at(-1)?.id || null,
    operations: Array.isArray(record.operations) ? record.operations.map((operation) => ({ ...operation })) : []
  }
}

function activeTimeline(record) {
  const normalized = normalizeSessionRecord(record)
  const byId = new Map(normalized.events.map((event) => [event.id, event]))
  const reversed = []
  let currentId = normalized.headEventId
  const visited = new Set()

  while (currentId) {
    if (visited.has(currentId)) throw new Error('Session event graph contains a cycle')
    visited.add(currentId)
    const event = byId.get(currentId)
    if (!event) throw new Error(`Session head references unknown event: ${currentId}`)
    reversed.push(event)
    currentId = event.parentId
  }

  return reversed.reverse()
}

function normalizeSessionId(id) {
  const value = String(id || '').trim()
  if (!/^[a-zA-Z0-9._-]+$/.test(value)) throw new Error('Invalid task session id')
  return value
}

function sessionPath(root, id) {
  return path.join(root, SESSION_DIR, `${normalizeSessionId(id)}.json`)
}

async function writeSession(root, record) {
  await mkdir(path.join(root, SESSION_DIR), { recursive: true })
  await writeFile(sessionPath(root, record.id), `${JSON.stringify(record, null, 2)}\n`)
  return record
}

async function readSession(root, id) {
  try {
    return normalizeSessionRecord(JSON.parse(await readFile(sessionPath(root, id), 'utf8')))
  } catch (error) {
    if (error.code === 'ENOENT') throw new Error(`Task session not found: ${normalizeSessionId(id)}`)
    throw error
  }
}

async function startSession(root, task) {
  if (typeof task !== 'string' || task.trim() === '') throw new Error('Task session requires a non-empty task')
  const changes = await getChangeSummary(root)
  const createdAt = new Date().toISOString()
  const id = `${createdAt.replace(/[-:.TZ]/g, '').slice(0, 14)}-${randomUUID().slice(0, 8)}`
  const startEvent = makeEvent('start', task.trim(), null, createdAt)
  const record = {
    schemaVersion: 2,
    id,
    task: task.trim(),
    status: 'active',
    revision: 1,
    createdAt,
    updatedAt: createdAt,
    parentSessionId: null,
    baseline: {
      branch: changes.branch,
      head: changes.head,
      fingerprint: changes.fingerprint
    },
    events: [startEvent],
    headEventId: startEvent.id,
    operations: []
  }
  await writeSession(root, record)
  return record
}

async function appendSessionEvent(root, id, event) {
  if (!event || typeof event.type !== 'string' || event.type.trim() === '') throw new Error('Session event requires a type')
  if (typeof event.message !== 'string' || event.message.trim() === '') throw new Error('Session event requires a message')
  const current = await readSession(root, id)
  const at = new Date().toISOString()
  const nextEvent = makeEvent(event.type.trim(), event.message.trim(), current.headEventId, at)
  const next = {
    ...current,
    revision: current.revision + 1,
    updatedAt: at,
    events: [...current.events, nextEvent],
    headEventId: nextEvent.id
  }
  await writeSession(root, next)
  return next
}

async function completeSession(root, id, message = 'Task completed.') {
  const current = await readSession(root, id)
  if (current.status === 'completed') return current
  const at = new Date().toISOString()
  const completionEvent = makeEvent('complete', String(message || 'Task completed.').trim(), current.headEventId, at)
  const next = {
    ...current,
    status: 'completed',
    revision: current.revision + 1,
    updatedAt: at,
    events: [...current.events, completionEvent],
    headEventId: completionEvent.id,
    operations: [...current.operations, { type: 'complete', at, revision: current.revision + 1, eventId: completionEvent.id }]
  }
  return await writeSession(root, next)
}

async function listSessions(root, options = {}) {
  let entries
  try {
    entries = await readdir(path.join(root, SESSION_DIR))
  } catch (error) {
    if (error.code === 'ENOENT') return []
    throw error
  }
  const records = await Promise.all(entries
    .filter((name) => name.endsWith('.json'))
    .map((name) => readSession(root, name.slice(0, -5))))
  const status = typeof options.status === 'string' ? options.status : null
  const limit = Number.isInteger(options.limit) ? Math.max(1, options.limit) : 100
  return records
    .filter((record) => !status || record.status === status)
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
    .slice(0, limit)
}

async function getLatestActiveSession(root) {
  return (await listSessions(root, { status: 'active', limit: 1 }))[0] || null
}

async function getActiveTimeline(root, id) {
  return activeTimeline(await readSession(root, id))
}

async function getSessionStatus(root, id) {
  const [session, changes] = await Promise.all([
    readSession(root, id),
    getChangeSummary(root)
  ])
  return {
    session,
    drifted: session.baseline.head !== changes.head || session.baseline.fingerprint !== changes.fingerprint,
    current: {
      branch: changes.branch,
      head: changes.head,
      fingerprint: changes.fingerprint,
      clean: changes.clean,
      files: changes.files
    }
  }
}

module.exports = {
  SESSION_DIR,
  activeTimeline,
  appendSessionEvent,
  completeSession,
  getActiveTimeline,
  getLatestActiveSession,
  getSessionStatus,
  listSessions,
  makeEvent,
  normalizeSessionId,
  normalizeSessionRecord,
  readSession,
  sessionPath,
  startSession
}
