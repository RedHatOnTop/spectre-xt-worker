const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { getChangeSummary, scopeStatus } = require('../src/git')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

test('getChangeSummary reports tracked and untracked changes', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-git-'))

  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await writeFile(path.join(root, 'tracked.txt'), 'one\n')
    await git(root, ['add', 'tracked.txt'])
    await git(root, ['commit', '-qm', 'initial'])
    await writeFile(path.join(root, 'tracked.txt'), 'one\ntwo\n')
    await writeFile(path.join(root, 'new.txt'), 'new\n')

    const summary = await getChangeSummary(root)

    assert.equal(summary.branch, 'master')
    assert.equal(summary.files.length, 2)
    assert.deepEqual(summary.files.map((file) => file.path).sort(), ['new.txt', 'tracked.txt'])
    assert.equal(summary.clean, false)
    assert.match(summary.fingerprint, /^[a-f0-9]{64}$/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('scopeStatus keeps only files below a nested project root', () => {
  const files = [
    { path: 'outside.js', status: ' M' },
    { path: 'nested/src/app.js', status: ' M' },
    { path: 'nested/new.js', status: '??' }
  ]

  assert.deepEqual(scopeStatus(files, 'nested/'), [
    { path: 'src/app.js', status: ' M' },
    { path: 'new.js', status: '??' }
  ])
})

test('getChangeSummary hides DevCodex runtime state from user change evidence', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-git-state-'))
  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await writeFile(path.join(root, 'tracked.txt'), 'one\n')
    await git(root, ['add', 'tracked.txt'])
    await git(root, ['commit', '-qm', 'initial'])
    await require('node:fs/promises').mkdir(path.join(root, '.devcodex'), { recursive: true })
    await writeFile(path.join(root, '.devcodex', 'journal.jsonl'), '{}\n')

    const summary = await getChangeSummary(root)
    assert.equal(summary.clean, true)
    assert.deepEqual(summary.files, [])
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
