const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const {
  appendSessionEvent,
  completeSession,
  getLatestActiveSession,
  getSessionStatus,
  startSession
} = require('../src/session')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

test('task session records notes and detects workspace drift', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-session-'))
  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await writeFile(path.join(root, 'app.js'), 'module.exports = 1\n')
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'initial'])

    const session = await startSession(root, 'refactor app')
    await appendSessionEvent(root, session.id, { type: 'note', message: 'Mapped dependencies.' })

    const before = await getSessionStatus(root, session.id)
    assert.equal(before.drifted, false)
    assert.equal(before.session.events.at(-1).message, 'Mapped dependencies.')

    await writeFile(path.join(root, 'app.js'), 'module.exports = 2\n')
    const after = await getSessionStatus(root, session.id)
    assert.equal(after.drifted, true)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('completed sessions leave the active recovery set', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-session-complete-'))
  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await writeFile(path.join(root, 'app.js'), 'module.exports = 1\n')
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'initial'])

    const older = await startSession(root, 'older task')
    const newer = await startSession(root, 'newer task')
    assert.equal((await getLatestActiveSession(root)).id, newer.id)

    const completed = await completeSession(root, newer.id, 'All checks passed.')
    assert.equal(completed.status, 'completed')
    assert.equal(completed.events.at(-1).type, 'complete')
    assert.equal(completed.events.at(-1).message, 'All checks passed.')
    assert.equal((await getLatestActiveSession(root)).id, older.id)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
