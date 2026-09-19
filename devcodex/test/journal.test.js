const assert = require('node:assert/strict')
const { mkdtemp, rm } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { appendJournalEvent, readJournal } = require('../src/journal')

test('journal is append-only JSONL and supports bounded filtered reads', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-journal-'))
  try {
    await appendJournalEvent(root, { type: 'tool.started', tool: 'search_code' })
    await appendJournalEvent(root, { type: 'tool.completed', tool: 'search_code', durationMs: 4 })
    await appendJournalEvent(root, { type: 'tool.completed', tool: 'repo_map', durationMs: 8 })

    const all = await readJournal(root, { limit: 2 })
    assert.equal(all.length, 2)
    assert.equal(all[0].tool, 'search_code')
    assert.equal(all[1].tool, 'repo_map')

    const filtered = await readJournal(root, { type: 'tool.started', limit: 10 })
    assert.equal(filtered.length, 1)
    assert.equal(filtered[0].tool, 'search_code')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
