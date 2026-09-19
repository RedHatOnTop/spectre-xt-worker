const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { refreshWorkspaceIndex, searchWorkspaceIndex } = require('../src/index')
const { readWorkspaceEvents, startWorkspaceWatcher } = require('../src/workspace-watch')

async function waitFor(predicate, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) return true
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  return false
}

test('workspace watcher records changes and incrementally refreshes the hot index', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-watch-'))
  let watcher
  try {
    await mkdir(path.join(root, 'src'))
    await writeFile(path.join(root, 'src', 'app.js'), 'const value = "old"\n')
    await refreshWorkspaceIndex(root)
    watcher = await startWorkspaceWatcher(root)

    await writeFile(path.join(root, 'src', 'app.js'), 'const value = "newNeedle"\n')
    const updated = await waitFor(async () => (await searchWorkspaceIndex(root, 'newNeedle')).length === 1)
    assert.equal(updated, true)

    const events = await readWorkspaceEvents(root, { limit: 20 })
    assert.ok(events.some((event) => event.path === 'src/app.js'))
    assert.ok(events.every((event) => !event.path.startsWith('.devcodex/')))
  } finally {
    if (watcher) await watcher.close()
    await rm(root, { recursive: true, force: true })
  }
})
