const { readFile } = require('node:fs/promises')
const path = require('node:path')

const { getChangeSummary } = require('./git')
const { rankRelevantFilesFromIndex, refreshWorkspaceIndex, searchWorkspaceIndex } = require('./index')
const { listEntries } = require('./search')

const INSTRUCTION_FILES = new Set(['AGENTS.md', 'CLAUDE.md'])

async function readInstructions(root, options = {}) {
  const entries = await listEntries(root, { maxDepth: options.maxDepth || 4 })
  const instructionEntries = entries.filter((entry) => entry.type === 'file' && INSTRUCTION_FILES.has(path.basename(entry.path)))

  return await Promise.all(instructionEntries.map(async (entry) => {
    const content = await readFile(path.join(root, entry.path), 'utf8')
    const limit = options.maxInstructionChars || 12000
    return {
      path: entry.path,
      content: content.slice(0, limit),
      truncated: content.length > limit
    }
  }))
}

async function collectTodos(root, options = {}) {
  const maxResults = options.maxTodos || 80
  const [todos, fixmes] = await Promise.all([
    searchWorkspaceIndex(root, 'TODO', { maxResults }),
    searchWorkspaceIndex(root, 'FIXME', { maxResults })
  ])

  const byLocation = new Map()
  for (const item of [...todos, ...fixmes]) {
    byLocation.set(`${item.path}:${item.line}:${item.text}`, item)
  }
  return [...byLocation.values()]
    .sort((left, right) => left.path.localeCompare(right.path) || left.line - right.line)
    .slice(0, maxResults)
}

async function buildContextPacket(root, task, options = {}) {
  if (typeof task !== 'string' || task.trim() === '') {
    throw new Error('Task must be a non-empty string')
  }

  await refreshWorkspaceIndex(root, { maxDepth: options.indexMaxDepth || 20 })
  const [instructions, changes, todos, relevantFiles] = await Promise.all([
    readInstructions(root, options),
    getChangeSummary(root),
    collectTodos(root, options),
    rankRelevantFilesFromIndex(root, task, { limit: options.maxRelevantFiles || 12 })
  ])

  return {
    task,
    generatedAt: new Date().toISOString(),
    instructions,
    changes,
    todos,
    relevantFiles
  }
}

module.exports = {
  buildContextPacket,
  collectTodos,
  readInstructions
}
