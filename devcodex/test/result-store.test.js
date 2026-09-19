const assert = require('node:assert/strict')
const { mkdtemp, rm } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { boundResult, readResultPage } = require('../src/result-store')

test('boundResult keeps small payloads inline', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-results-'))
  try {
    const value = { ok: true, items: [1, 2, 3] }
    const result = await boundResult(root, value, { maxChars: 1000 })

    assert.deepEqual(result, value)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('boundResult stores large payloads and exposes bounded pages', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-results-'))
  try {
    const value = { items: Array.from({ length: 100 }, (_, index) => ({ index, text: 'x'.repeat(80) })) }
    const bounded = await boundResult(root, value, { maxChars: 500, previewChars: 180 })

    assert.equal(bounded.truncated, true)
    assert.match(bounded.handle, /^result-/)
    assert.ok(bounded.totalChars > 500)
    assert.ok(bounded.preview.length <= 180)

    const first = await readResultPage(root, bounded.handle, { offset: 0, limit: 240 })
    const second = await readResultPage(root, bounded.handle, { offset: first.nextOffset, limit: 240 })
    assert.equal(first.offset, 0)
    assert.equal(first.content.length, 240)
    assert.equal(second.offset, 240)
    assert.ok(first.totalChars > first.content.length)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
