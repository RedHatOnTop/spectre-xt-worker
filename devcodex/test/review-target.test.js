const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { createDetachedReview, reviewTarget } = require('../src/review')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

test('review target supports commit and base-branch scopes', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-review-target-'))
  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await writeFile(path.join(root, 'app.js'), 'module.exports = 1\n')
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'initial'])
    const base = (await git(root, ['rev-parse', 'HEAD'])).stdout.trim()
    await writeFile(path.join(root, 'app.js'), 'console.log("debug")\nmodule.exports = 2\n')
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'debug commit'])
    const commit = (await git(root, ['rev-parse', 'HEAD'])).stdout.trim()

    const commitReview = await reviewTarget(root, { type: 'commit', sha: commit })
    assert.equal(commitReview.target.type, 'commit')
    assert.equal(commitReview.summary.medium, 1)

    const branchReview = await reviewTarget(root, { type: 'baseBranch', branch: base })
    assert.equal(branchReview.target.type, 'baseBranch')
    assert.equal(branchReview.summary.medium, 1)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('detached review persists a review artifact without modifying source files', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-review-detached-'))
  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await writeFile(path.join(root, 'app.js'), 'module.exports = 1\n')
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'initial'])
    await writeFile(path.join(root, 'app.js'), 'debugger;\nmodule.exports = 1\n')

    const detached = await createDetachedReview(root, { type: 'uncommittedChanges' })
    assert.equal(detached.delivery, 'detached')
    assert.match(detached.path, /^\.devcodex\/reviews\//)
    assert.equal(detached.review.summary.medium, 1)
    assert.equal(await require('node:fs/promises').readFile(path.join(root, 'app.js'), 'utf8'), 'debugger;\nmodule.exports = 1\n')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
