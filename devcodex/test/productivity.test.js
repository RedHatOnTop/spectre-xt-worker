const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { getFileDiff } = require('../src/git')
const { inspectFile, readMany, searchContext } = require('../src/inspect')
const { buildTaskBootstrap } = require('../src/orchestrator')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

async function withWorkspace(run) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-productivity-'))
  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await mkdir(path.join(root, 'src', 'feature'), { recursive: true })
    await writeFile(path.join(root, 'AGENTS.md'), '# Root\nRun tests.\n')
    await writeFile(path.join(root, 'src', 'feature', 'AGENTS.md'), '# Feature\nKeep API stable.\n')
    await writeFile(path.join(root, 'src', 'feature', 'math.js'), '// TODO: fix subtract\nfunction subtract(a, b) { return a + b }\n')
    await writeFile(path.join(root, 'src', 'other.js'), 'module.exports = 1\n')
    await writeFile(path.join(root, 'package.json'), JSON.stringify({ scripts: { test: 'node --test' } }))
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({ gates: [{ name: 'tests', command: 'node -e "process.exit(0)"' }] }))
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'initial'])
    return await run(root)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
}

test('inspectFile combines applicable instructions with a bounded read', async () => {
  await withWorkspace(async (root) => {
    const result = await inspectFile(root, 'src/feature/math.js', { limit: 20 })
    assert.equal(result.file.path, 'src/feature/math.js')
    assert.deepEqual(result.instructions.map((item) => item.path), ['AGENTS.md', 'src/feature/AGENTS.md'])
    assert.match(result.file.content, /subtract/)
  })
})

test('readMany batches file windows without requiring repeated tool calls', async () => {
  await withWorkspace(async (root) => {
    const result = await readMany(root, [
      { path: 'src/feature/math.js', offset: 1, limit: 2 },
      { path: 'src/other.js', offset: 1, limit: 1 }
    ])
    assert.equal(result.length, 2)
    assert.match(result[0].content, /TODO/)
    assert.match(result[1].content, /module\.exports/)
  })
})

test('getFileDiff scopes the patch to one file', async () => {
  await withWorkspace(async (root) => {
    await writeFile(path.join(root, 'src', 'feature', 'math.js'), 'function subtract(a, b) { return a - b }\n')
    await writeFile(path.join(root, 'src', 'other.js'), 'module.exports = 2\n')
    const result = await getFileDiff(root, 'src/feature/math.js')

    assert.match(result.patch, /subtract/)
    assert.doesNotMatch(result.patch, /module\.exports/)
  })
})

test('buildTaskBootstrap returns a compact first-turn context', async () => {
  await withWorkspace(async (root) => {
    const result = await buildTaskBootstrap(root, 'fix subtract bug without changing the API')
    assert.equal(result.task, 'fix subtract bug without changing the API')
    assert.equal(result.repository.name, path.basename(root))
    assert.equal(result.repository.testCount, 0)
    assert.ok(result.relevantFiles.some((file) => file.path === 'src/feature/math.js'))
    assert.ok(result.instructions.some((item) => item.path === 'AGENTS.md'))
    assert.ok(Array.isArray(result.nextActions))
    assert.equal(Object.hasOwn(result, 'repoMap'), false)
    assert.equal(Object.hasOwn(result, 'context'), false)
  })
})

test('searchContext returns surrounding source without a second read call', async () => {
  await withWorkspace(async (root) => {
    const result = await searchContext(root, 'subtract', { maxMatches: 4, contextLines: 1 })
    assert.equal(result.matches.length, 1)
    assert.equal(result.matches[0].path, 'src/feature/math.js')
    assert.match(result.matches[0].content, /TODO: fix subtract/)
    assert.match(result.matches[0].content, /function subtract/)
  })
})
