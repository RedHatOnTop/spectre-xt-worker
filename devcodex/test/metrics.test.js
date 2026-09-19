const assert = require('node:assert/strict')
const { mkdtemp, rm } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { appendJournalEvent } = require('../src/journal')
const { buildRuntimeMetrics } = require('../src/metrics')

test('runtime metrics aggregate latency, failures, truncation, and output savings', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-metrics-'))
  try {
    await appendJournalEvent(root, { type: 'tool.completed', tool: 'search_code', durationMs: 10, resultChars: 1000, returnedChars: 1000, truncated: false })
    await appendJournalEvent(root, { type: 'tool.completed', tool: 'search_code', durationMs: 30, resultChars: 20000, returnedChars: 1800, truncated: true })
    await appendJournalEvent(root, { type: 'tool.failed', tool: 'search_code', durationMs: 5, error: 'boom' })
    await appendJournalEvent(root, { type: 'tool.blocked', tool: 'run_hook', permissionDecision: 'ask' })

    const metrics = await buildRuntimeMetrics(root)
    assert.equal(metrics.completedCalls, 2)
    assert.equal(metrics.failedCalls, 1)
    assert.equal(metrics.blockedCalls, 1)
    assert.equal(metrics.output.savedChars, 18200)
    assert.equal(metrics.output.truncatedCalls, 1)
    assert.equal(metrics.tools.search_code.calls, 2)
    assert.equal(metrics.tools.search_code.p95Ms, 30)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
