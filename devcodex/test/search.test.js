const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { buildTree, searchWorkspace } = require('../src/search')

async function withWorkspace(run) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-search-'))

  try {
    await mkdir(path.join(root, 'src'), { recursive: true })
    await mkdir(path.join(root, '.git'), { recursive: true })
    await mkdir(path.join(root, 'node_modules', 'ignored'), { recursive: true })
    await writeFile(path.join(root, 'src', 'math.js'), '// TODO: handle overflow\nfunction add(a, b) { return a + b }\n')
    await writeFile(path.join(root, 'README.md'), '# Example\n')
    await writeFile(path.join(root, '.git', 'secret'), 'TODO: never return this\n')
    await writeFile(path.join(root, 'node_modules', 'ignored', 'index.js'), 'TODO: never return this either\n')
    return await run(root)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
}

test('searchWorkspace finds matches and ignores noisy directories', async () => {
  await withWorkspace(async (root) => {
    const results = await searchWorkspace(root, 'TODO')

    assert.equal(results.length, 1)
    assert.equal(results[0].path, 'src/math.js')
    assert.equal(results[0].line, 1)
    assert.match(results[0].text, /handle overflow/)
  })
})

test('buildTree returns a stable compact tree', async () => {
  await withWorkspace(async (root) => {
    const entries = await buildTree(root, { maxDepth: 2 })

    assert.deepEqual(entries, [
      'README.md',
      'src/',
      '  math.js'
    ])
  })
})
