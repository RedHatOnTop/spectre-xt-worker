const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const {
  buildSymbolIndex,
  findReferences,
  outlineFile,
  searchSymbols
} = require('../src/symbols')

async function withWorkspace(run) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-symbols-'))
  try {
    await mkdir(path.join(root, 'src'))
    await writeFile(path.join(root, 'src', 'math.js'), [
      'export function subtract(a, b) { return a - b }',
      'export class Calculator {',
      '  run() { return subtract(3, 1) }',
      '}',
      'const helper = () => subtract(4, 2)',
      ''
    ].join('\n'))
    await writeFile(path.join(root, 'src', 'worker.py'), [
      'def process_item(value):',
      '    return value',
      '',
      'class Worker:',
      '    pass',
      ''
    ].join('\n'))
    await writeFile(path.join(root, 'src', 'lib.rs'), [
      'pub struct Engine {}',
      'pub fn start_engine() {}',
      ''
    ].join('\n'))
    return await run(root)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
}

test('symbol index extracts common definitions across languages', async () => {
  await withWorkspace(async (root) => {
    const built = await buildSymbolIndex(root)
    assert.ok(built.symbols >= 7)

    const calculator = await searchSymbols(root, 'Calculator')
    assert.equal(calculator[0].kind, 'class')
    assert.equal(calculator[0].path, 'src/math.js')

    const engine = await searchSymbols(root, 'Engine')
    assert.equal(engine[0].path, 'src/lib.rs')
  })
})

test('outline and references provide compact code navigation', async () => {
  await withWorkspace(async (root) => {
    await buildSymbolIndex(root)
    const outline = await outlineFile(root, 'src/math.js')
    assert.ok(outline.some((symbol) => symbol.name === 'subtract'))
    assert.ok(outline.some((symbol) => symbol.name === 'Calculator'))

    const references = await findReferences(root, 'subtract')
    assert.equal(references.length, 3)
    assert.equal(references.filter((reference) => reference.definition).length, 1)
  })
})
