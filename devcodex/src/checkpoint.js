const { mkdir, readFile, writeFile } = require('node:fs/promises')
const path = require('node:path')

const { getChangeSummary } = require('./git')

const CHECKPOINT_DIR = '.devcodex/checkpoints'

function sanitizeName(name) {
  const normalized = String(name || '').trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '-')
  if (!/[a-z0-9]/.test(normalized)) throw new Error('Checkpoint name must contain at least one alphanumeric character')
  return normalized
}

function checkpointPath(root, name) {
  return path.join(root, CHECKPOINT_DIR, `${sanitizeName(name)}.json`)
}

async function createCheckpoint(root, name, verification = null) {
  const changes = await getChangeSummary(root)
  const record = {
    name: sanitizeName(name),
    createdAt: new Date().toISOString(),
    branch: changes.branch,
    head: changes.head,
    fingerprint: changes.fingerprint,
    clean: changes.clean,
    files: changes.files,
    verification
  }

  const directory = path.join(root, CHECKPOINT_DIR)
  await mkdir(directory, { recursive: true })
  const target = checkpointPath(root, name)
  await writeFile(target, `${JSON.stringify(record, null, 2)}\n`)
  return { ...record, path: path.relative(root, target) }
}

async function verifyCheckpoint(root, name) {
  const target = checkpointPath(root, name)
  let record
  try {
    record = JSON.parse(await readFile(target, 'utf8'))
  } catch (error) {
    if (error.code === 'ENOENT') throw new Error(`Checkpoint not found: ${sanitizeName(name)}`)
    throw error
  }

  const changes = await getChangeSummary(root)
  return {
    matches: record.head === changes.head && record.fingerprint === changes.fingerprint,
    checkpoint: record,
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
  CHECKPOINT_DIR,
  checkpointPath,
  createCheckpoint,
  sanitizeName,
  verifyCheckpoint
}
