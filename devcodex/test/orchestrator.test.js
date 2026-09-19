const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { buildTaskBootstrap, runCompletionGate } = require('../src/orchestrator')
const { appendSessionEvent, getSessionStatus, startSession } = require('../src/session')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

async function createRepo() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-orchestrator-'))
  await git(root, ['init', '-q'])
  await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
  await git(root, ['config', 'user.name', 'DevCodex Test'])
  await mkdir(path.join(root, 'src'))
  await writeFile(path.join(root, 'AGENTS.md'), '# Rules\nRun tests.\n')
  await writeFile(path.join(root, 'src', 'math.js'), '// TODO: subtract\nfunction subtract(a, b) { return a - b }\n')
  await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({
    gates: [{ name: 'tests', command: 'node -e "process.exit(0)"' }],
    skillRoots: []
  }))
  await git(root, ['add', '.'])
  await git(root, ['commit', '-qm', 'initial'])
  return root
}

test('task bootstrap combines context, repo map, review, and next actions', async () => {
  const root = await createRepo()
  try {
    const snapshot = await buildTaskBootstrap(root, 'fix subtract implementation')

    assert.equal(snapshot.task, 'fix subtract implementation')
    assert.equal(snapshot.session.task, 'fix subtract implementation')
    assert.equal(snapshot.session.status, 'active')
    assert.equal(snapshot.session.timeline[0].type, 'start')
    assert.equal(snapshot.repository.git.clean, true)
    assert.equal(snapshot.instructions[0].path, 'AGENTS.md')
    assert.equal(snapshot.review.ok, true)
    assert.ok(snapshot.nextActions.length > 0)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('task bootstrap resumes the latest active session with durable task state', async () => {
  const root = await createRepo()
  try {
    const session = await startSession(root, 'finish subtraction refactor')
    await appendSessionEvent(root, session.id, { type: 'decision', message: 'Keep the public API stable.' })

    const resumed = await buildTaskBootstrap(root, undefined, { resumeLatest: true })
    assert.equal(resumed.task, 'finish subtraction refactor')
    assert.equal(resumed.session.id, session.id)
    assert.equal(resumed.session.status, 'active')
    assert.equal(resumed.session.timeline.at(-1).message, 'Keep the public API stable.')
    assert.equal(resumed.session.drifted, false)
    assert.equal(Object.hasOwn(resumed.session, 'goal'), false)
    assert.equal(Object.hasOwn(resumed.session, 'queue'), false)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('failed task bootstrap does not leave an orphan active session', async () => {
  const root = await createRepo()
  try {
    await writeFile(path.join(root, '.devcodex.json'), '{ malformed')
    await assert.rejects(
      () => buildTaskBootstrap(root, 'task that cannot bootstrap'),
      /Failed to read \.devcodex\.json/
    )

    const { listSessions } = require('../src/session')
    assert.equal((await listSessions(root, { status: 'active' })).length, 0)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('runCompletionGate returns verification evidence and creates a checkpoint on success', async () => {
  const root = await createRepo()
  try {
    await writeFile(path.join(root, 'src', 'math.js'), 'function subtract(a, b) { return a - b }\n')
    const result = await runCompletionGate(root, { checkpointName: 'ready' })

    assert.equal(result.ok, true)
    assert.equal(result.verification.ok, true)
    assert.equal(result.review.ok, true)
    assert.equal(result.checkpoint.created, true)
    assert.equal(result.checkpoint.checkpoint.name, 'ready')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('successful completion gate closes the attached task session', async () => {
  const root = await createRepo()
  try {
    const session = await startSession(root, 'finish task')
    const result = await runCompletionGate(root, { sessionId: session.id })

    assert.equal(result.ok, true)
    assert.equal(result.sessionUpdated, true)
    const status = await getSessionStatus(root, session.id)
    assert.equal(status.session.status, 'completed')
    assert.equal(status.session.events.at(-1).type, 'complete')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('completion gate passes and closes the session when no quality gates are configured', async () => {
  const root = await createRepo()
  try {
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({ skillRoots: [] }))
    const session = await startSession(root, 'close without gates')
    const result = await runCompletionGate(root, { sessionId: session.id })

    assert.equal(result.ok, true)
    assert.equal(result.verification.reason, 'no-quality-gates-configured')
    const status = await getSessionStatus(root, session.id)
    assert.equal(status.session.status, 'completed')
    assert.equal(status.session.events.at(-1).type, 'complete')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('runCompletionGate refuses completion when quality gates fail', async () => {
  const root = await createRepo()
  try {
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({
      gates: [{ name: 'tests', command: 'node -e "process.exit(9)"' }],
      skillRoots: []
    }))
    const result = await runCompletionGate(root, { checkpointName: 'must-not-create' })

    assert.equal(result.ok, false)
    assert.equal(result.verification.ok, false)
    assert.equal(result.checkpoint, null)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('runCompletionGate executes configured verification and completion hooks in order', async () => {
  const root = await createRepo()
  try {
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({
      gates: [{ name: 'tests', command: 'printf G >> lifecycle.log' }],
      skillRoots: [],
      hooks: {
        beforeVerify: [{ command: 'printf A >> lifecycle.log' }],
        afterVerify: [{ command: 'printf B >> lifecycle.log' }],
        beforeCompletion: [{ command: 'printf C >> lifecycle.log' }],
        afterCompletion: [{ command: 'printf D >> lifecycle.log' }]
      }
    }))

    const result = await runCompletionGate(root)
    assert.equal(result.ok, true)
    assert.equal(await require('node:fs/promises').readFile(path.join(root, 'lifecycle.log'), 'utf8'), 'AGBCD')
    assert.equal(result.hooks.beforeVerify.ok, true)
    assert.equal(result.hooks.afterCompletion.ok, true)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
