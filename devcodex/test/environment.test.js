const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { buildEnvironmentSnapshot } = require('../src/environment')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

test('environment snapshot reports runtime, tools, project state, and capabilities', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-env-'))
  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await writeFile(path.join(root, 'package.json'), '{"scripts":{"test":"node --test"}}\n')
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'initial'])

    const snapshot = await buildEnvironmentSnapshot(root)
    assert.equal(snapshot.runtime.platform, process.platform)
    assert.equal(snapshot.runtime.arch, process.arch)
    assert.equal(snapshot.tools.node.available, true)
    assert.equal(snapshot.tools.git.available, true)
    assert.equal(snapshot.project.git.branch, 'master')
    assert.equal(typeof snapshot.project.index.available, 'boolean')
    assert.equal(Object.hasOwn(snapshot.project, 'backgroundProcesses'), false)
    assert.equal(typeof snapshot.capabilities.qualityGatesConfigured, 'boolean')
    assert.equal(Object.hasOwn(snapshot.tools, 'codex'), false)
    assert.equal(Object.hasOwn(snapshot.tools, 'claude'), false)
    assert.equal(Object.hasOwn(snapshot.capabilities, 'codexAvailable'), false)
    assert.equal(Object.hasOwn(snapshot.capabilities, 'claudeAvailable'), false)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
