const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const {
  getIndexStatus,
  refreshWorkspaceIndex,
  searchWorkspaceIndex
} = require('../src/index')

async function withWorkspace(run) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-index-'))
  try {
    await mkdir(path.join(root, 'src'))
    await writeFile(path.join(root, 'src', 'alpha.js'), 'const alpha = "needle"\n')
    await writeFile(path.join(root, 'src', 'beta.js'), 'const beta = 2\n')
    return await run(root)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
}

test('workspace index refresh reuses unchanged files and updates changed files', async () => {
  await withWorkspace(async (root) => {
    const first = await refreshWorkspaceIndex(root)
    assert.equal(first.files, 2)
    assert.equal(first.updated, 2)

    const second = await refreshWorkspaceIndex(root)
    assert.equal(second.updated, 0)
    assert.equal(second.reused, 2)

    await new Promise((resolve) => setTimeout(resolve, 10))
    await writeFile(path.join(root, 'src', 'beta.js'), 'const beta = "needle too"\n')
    const third = await refreshWorkspaceIndex(root)
    assert.equal(third.updated, 1)
    assert.equal(third.reused, 1)

    const matches = await searchWorkspaceIndex(root, 'needle')
    assert.deepEqual(matches.map((match) => match.path), ['src/alpha.js', 'src/beta.js'])
  })
})

test('index status exposes age and cache size without returning cached file contents', async () => {
  await withWorkspace(async (root) => {
    await refreshWorkspaceIndex(root)
    const status = await getIndexStatus(root)
    assert.equal(status.available, true)
    assert.equal(status.files, 2)
    assert.ok(status.compressedBytes > 0)
    assert.ok(status.ageMs >= 0)
    assert.equal(Object.hasOwn(status, 'entries'), false)
  })
})
