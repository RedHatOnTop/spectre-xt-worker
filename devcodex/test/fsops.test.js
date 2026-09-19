const assert = require('node:assert/strict')
const { mkdtemp, readFile, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { editWorkspaceFile, runShellCommand, writeWorkspaceFile } = require('../src/fsops')

async function createRoot() {
  return await mkdtemp(path.join(os.tmpdir(), 'devcodex-fsops-'))
}

test('writeWorkspaceFile creates parent directories and replaces existing content', async () => {
  const root = await createRoot()
  try {
    const created = await writeWorkspaceFile(root, 'nested/dir/file.txt', 'hello\n')
    assert.equal(created.path, 'nested/dir/file.txt')
    assert.equal(created.mode, 'created')
    assert.equal(created.bytes, 6)
    assert.equal(await readFile(path.join(root, 'nested/dir/file.txt'), 'utf8'), 'hello\n')

    const replaced = await writeWorkspaceFile(root, 'nested/dir/file.txt', 'world\n')
    assert.equal(replaced.mode, 'replaced')
    assert.equal(await readFile(path.join(root, 'nested/dir/file.txt'), 'utf8'), 'world\n')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('writeWorkspaceFile refuses paths outside the workspace root', async () => {
  const root = await createRoot()
  try {
    await assert.rejects(() => writeWorkspaceFile(root, '../escape.txt', 'x'), /outside workspace root/)
    await assert.rejects(() => writeWorkspaceFile(root, '/abs/path.txt', 'x'), /relative path/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('editWorkspaceFile replaces a unique match and reports the count', async () => {
  const root = await createRoot()
  try {
    await writeFile(path.join(root, 'app.js'), 'const a = 1\nconst b = 1\n')
    const result = await editWorkspaceFile(root, 'app.js', 'const a = 1', 'const a = 2')
    assert.equal(result.replaced, 1)
    assert.equal(await readFile(path.join(root, 'app.js'), 'utf8'), 'const a = 2\nconst b = 1\n')

    await assert.rejects(() => editWorkspaceFile(root, 'app.js', 'missing text', 'x'), /not found/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('editWorkspaceFile requires replaceAll for repeated matches', async () => {
  const root = await createRoot()
  try {
    await writeFile(path.join(root, 'multi.txt'), 'x = 1\ny = 1\n')
    await assert.rejects(() => editWorkspaceFile(root, 'multi.txt', '= 1', '= 9'), /occurs 2 times/)
    const all = await editWorkspaceFile(root, 'multi.txt', '= 1', '= 9', { replaceAll: true })
    assert.equal(all.replaced, 2)
    assert.equal(await readFile(path.join(root, 'multi.txt'), 'utf8'), 'x = 9\ny = 9\n')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('runShellCommand runs in the workspace root and reports exit state', async () => {
  const root = await createRoot()
  try {
    await writeFile(path.join(root, 'marker.txt'), 'ok\n')
    const okRun = await runShellCommand(root, 'cat marker.txt')
    assert.equal(okRun.ok, true)
    assert.equal(okRun.exitCode, 0)
    assert.match(okRun.stdout, /ok/)

    const failing = await runShellCommand(root, 'exit 3')
    assert.equal(failing.ok, false)
    assert.equal(failing.exitCode, 3)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('runShellCommand honors the timeout', async () => {
  const root = await createRoot()
  try {
    const result = await runShellCommand(root, 'sleep 5', { timeoutMs: 1000 })
    assert.equal(result.timedOut, true)
    assert.equal(result.ok, false)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
