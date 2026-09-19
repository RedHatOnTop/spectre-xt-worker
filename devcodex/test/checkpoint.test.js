const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { createCheckpoint, sanitizeName, verifyCheckpoint } = require('../src/checkpoint')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

async function initRepository() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-checkpoint-'))
  await git(root, ['init', '-q'])
  await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
  await git(root, ['config', 'user.name', 'DevCodex Test'])
  await writeFile(path.join(root, 'file.txt'), 'initial\n')
  await git(root, ['add', '.'])
  await git(root, ['commit', '-qm', 'initial'])
  return root
}

test('sanitizeName produces filesystem-safe checkpoint names', () => {
  assert.equal(sanitizeName('Before Big Refactor!'), 'before-big-refactor-')
  assert.throws(() => sanitizeName('---'), /alphanumeric/)
})

test('checkpoint matches until workspace state changes', async () => {
  const root = await initRepository()

  try {
    const created = await createCheckpoint(root, 'baseline')
    const before = await verifyCheckpoint(root, 'baseline')
    assert.equal(before.matches, true)
    assert.equal(created.clean, true)

    await writeFile(path.join(root, 'file.txt'), 'changed\n')
    const after = await verifyCheckpoint(root, 'baseline')
    assert.equal(after.matches, false)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
