const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { expandHome, loadConfig } = require('../src/config')

test('expandHome only expands a leading home marker', () => {
  assert.equal(expandHome('~/.codex/skills', '/home/tester'), '/home/tester/.codex/skills')
  assert.equal(expandHome('/tmp/~literal', '/home/tester'), '/tmp/~literal')
})

test('loadConfig reads immutable gate copies and skill roots', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-config-'))
  try {
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({
      gates: [{ name: 'test', command: 'npm test' }],
      skillRoots: ['/tmp/skills'],
      mcpProfile: 'extended',
      resultBudgetChars: 8000
    }))
    const config = await loadConfig(root)
    assert.deepEqual(config.gates, [{ name: 'test', command: 'npm test' }])
    assert.deepEqual(config.skillRoots, ['/tmp/skills'])
    assert.equal(config.mcpProfile, 'extended')
    assert.equal(config.resultBudgetChars, 8000)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('loadConfig ignores obsolete external agent provider configuration', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-config-obsolete-provider-'))
  try {
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({
      agentProviders: {
        legacy: { command: '/tmp/legacy-agent', args: ['{prompt}'] }
      }
    }))
    const config = await loadConfig(root)
    assert.equal(Object.hasOwn(config, 'agentProviders'), false)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
