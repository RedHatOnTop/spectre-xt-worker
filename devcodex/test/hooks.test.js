const assert = require('node:assert/strict')
const { mkdtemp, readFile, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { runHooks } = require('../src/hooks')

test('hooks run configured lifecycle commands in order and capture evidence', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-hooks-'))
  try {
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({
      hooks: {
        beforeVerify: [
          { name: 'first', command: 'printf first >> hook.log' },
          { name: 'second', command: 'printf second >> hook.log' }
        ]
      }
    }))

    const report = await runHooks(root, 'beforeVerify')
    assert.equal(report.ok, true)
    assert.deepEqual(report.results.map((result) => result.name), ['first', 'second'])
    assert.equal(await readFile(path.join(root, 'hook.log'), 'utf8'), 'firstsecond')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('required hook failure stops the lifecycle while optional failure does not', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-hooks-'))
  try {
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({
      hooks: {
        beforeCompletion: [
          { name: 'optional', command: 'exit 4', required: false },
          { name: 'required', command: 'exit 7' },
          { name: 'never', command: 'printf nope > should-not-exist' }
        ]
      }
    }))

    const report = await runHooks(root, 'beforeCompletion')
    assert.equal(report.ok, false)
    assert.deepEqual(report.results.map((result) => result.name), ['optional', 'required'])
    assert.equal(report.results[0].required, false)
    assert.equal(report.results[1].exitCode, 7)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
