const assert = require('node:assert/strict')
const { mkdtemp, rm } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { runQualityGates } = require('../src/gates')

test('runQualityGates records success, failure, and aggregate status', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-gates-'))

  try {
    const report = await runQualityGates(root, [
      { name: 'pass', command: 'node -e "process.stdout.write(\'ok\')"' },
      { name: 'fail', command: 'node -e "process.stderr.write(\'bad\'); process.exit(7)"' }
    ])

    assert.equal(report.ok, false)
    assert.equal(report.results.length, 2)
    assert.equal(report.results[0].exitCode, 0)
    assert.equal(report.results[0].stdout, 'ok')
    assert.equal(report.results[1].exitCode, 7)
    assert.equal(report.results[1].stderr, 'bad')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('optional quality gates do not fail the aggregate report', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-gates-'))

  try {
    const report = await runQualityGates(root, [
      { name: 'optional-fail', command: 'node -e "process.exit(2)"', required: false }
    ])

    assert.equal(report.ok, true)
    assert.equal(report.results[0].ok, false)
    assert.equal(report.results[0].required, false)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
