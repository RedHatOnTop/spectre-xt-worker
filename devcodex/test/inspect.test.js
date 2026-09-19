const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { findInstructionsForPath, readWorkspaceFile, resolveWorkspacePath } = require('../src/inspect')

test('resolveWorkspacePath rejects traversal outside the workspace', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-inspect-'))
  try {
    assert.equal(resolveWorkspacePath(root, 'src/index.js'), path.join(root, 'src/index.js'))
    assert.throws(() => resolveWorkspacePath(root, '../secret.txt'), /outside workspace root/)
    assert.throws(() => resolveWorkspacePath(root, '/etc/passwd'), /relative path/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('readWorkspaceFile returns a bounded line window', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-inspect-'))
  try {
    await writeFile(path.join(root, 'notes.txt'), 'one\ntwo\nthree\nfour\n')
    const result = await readWorkspaceFile(root, 'notes.txt', { offset: 2, limit: 2 })

    assert.equal(result.startLine, 2)
    assert.equal(result.endLine, 3)
    assert.equal(result.totalLines, 5)
    assert.equal(result.truncated, true)
    assert.equal(result.content, 'two\nthree')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('findInstructionsForPath returns root-to-leaf instruction hierarchy', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-inspect-'))
  try {
    await mkdir(path.join(root, 'src', 'feature'), { recursive: true })
    await writeFile(path.join(root, 'AGENTS.md'), '# root agent\n')
    await writeFile(path.join(root, 'src', 'CLAUDE.md'), '# src claude\n')
    await writeFile(path.join(root, 'src', 'feature', 'AGENTS.md'), '# feature agent\n')
    await writeFile(path.join(root, 'src', 'feature', 'index.js'), 'module.exports = 1\n')

    const result = await findInstructionsForPath(root, 'src/feature/index.js')

    assert.deepEqual(result.map((item) => item.path), [
      'AGENTS.md',
      'src/CLAUDE.md',
      'src/feature/AGENTS.md'
    ])
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
