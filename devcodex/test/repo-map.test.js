const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { buildRepoMap } = require('../src/repo-map')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

test('buildRepoMap summarizes languages, manifests, tests, scripts, and git state', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-map-'))
  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await mkdir(path.join(root, 'src'))
    await mkdir(path.join(root, 'test'))
    await writeFile(path.join(root, 'package.json'), JSON.stringify({ scripts: { test: 'node --test', lint: 'echo lint' } }))
    await writeFile(path.join(root, 'AGENTS.md'), '# rules\n')
    await writeFile(path.join(root, 'src', 'index.js'), 'module.exports = 1\n')
    await writeFile(path.join(root, 'test', 'index.test.js'), 'module.exports = 1\n')
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'initial'])

    const result = await buildRepoMap(root)

    assert.equal(result.git.clean, true)
    assert.equal(result.languages.JavaScript, 2)
    assert.deepEqual(result.manifests, ['package.json'])
    assert.deepEqual(result.instructions, ['AGENTS.md'])
    assert.deepEqual(result.tests, ['test/index.test.js'])
    assert.deepEqual(result.scripts, { lint: 'echo lint', test: 'node --test' })
    assert.ok(result.fileCount >= 4)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
