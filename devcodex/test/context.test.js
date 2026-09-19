const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { buildContextPacket } = require('../src/context')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

test('buildContextPacket combines instructions, changes, TODOs, and relevant files', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-context-'))

  try {
    await git(root, ['init', '-q'])
    await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
    await git(root, ['config', 'user.name', 'DevCodex Test'])
    await mkdir(path.join(root, 'src'))
    await writeFile(path.join(root, 'AGENTS.md'), '# Rules\nRun tests.\n')
    await writeFile(path.join(root, 'src', 'math.js'), '// TODO: fix subtraction\nfunction subtract(a, b) { return a + b }\n')
    await git(root, ['add', '.'])
    await git(root, ['commit', '-qm', 'initial'])

    const packet = await buildContextPacket(root, 'fix subtract bug')

    assert.equal(packet.task, 'fix subtract bug')
    assert.equal(packet.instructions[0].path, 'AGENTS.md')
    assert.equal(packet.todos[0].path, 'src/math.js')
    assert.equal(packet.relevantFiles[0].path, 'src/math.js')
    assert.equal(packet.changes.clean, true)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
