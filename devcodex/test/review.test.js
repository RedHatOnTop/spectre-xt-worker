const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { analyzeLines, reviewWorkspace } = require('../src/review')

const execFileAsync = promisify(execFile)

async function git(root, args) {
  return await execFileAsync('git', args, { cwd: root })
}

async function createRepo() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-review-'))
  await git(root, ['init', '-q'])
  await git(root, ['config', 'user.email', 'devcodex@example.invalid'])
  await git(root, ['config', 'user.name', 'DevCodex Test'])
  await writeFile(path.join(root, 'app.js'), 'function run() { return 1 }\n')
  await git(root, ['add', '.'])
  await git(root, ['commit', '-qm', 'initial'])
  return root
}

test('reviewWorkspace flags conflict markers, debug statements, and TODO additions', async () => {
  const root = await createRepo()
  try {
    await writeFile(path.join(root, 'app.js'), [
      'function run() {',
      '  console.log("debug")',
      '  // TODO: remove workaround',
      '<<<<<<< HEAD',
      '  return 2',
      '=======',
      '  return 3',
      '>>>>>>> branch',
      '}',
      ''
    ].join('\n'))

    const report = await reviewWorkspace(root)
    const rules = report.findings.map((finding) => finding.rule)

    assert.equal(report.ok, false)
    assert.ok(rules.includes('merge-conflict-marker'))
    assert.ok(rules.includes('debug-statement'))
    assert.ok(rules.includes('todo-added'))
    assert.equal(report.summary.high >= 1, true)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('reviewWorkspace scans untracked text files', async () => {
  const root = await createRepo()
  try {
    await writeFile(path.join(root, 'new.js'), 'debugger;\n')
    const report = await reviewWorkspace(root)
    const finding = report.findings.find((item) => item.path === 'new.js' && item.rule === 'debug-statement')
    assert.ok(finding)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('analyzeLines ignores debug and TODO text inside quoted fixtures or prose', () => {
  const findings = analyzeLines([
    { path: 'fixture.js', line: 1, text: 'const snippet = \'console.log("debug")\'' },
    { path: 'fixture.js', line: 2, text: 'const snippet = \'// TODO: later\'' },
    { path: 'README.md', line: 3, text: 'This section explains TODO/FIXME review behavior.' }
  ])

  assert.deepEqual(findings, [])
})
