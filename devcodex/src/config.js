const { readFile } = require('node:fs/promises')
const path = require('node:path')

const DEFAULT_CONFIG = Object.freeze({
  gates: [],
  skillRoots: [
    '~/.codex/skills',
    '~/.agents/skills'
  ],
  permissions: null,
  mcpProfile: 'core',
  resultBudgetChars: 12000,
  hooks: {}
})

function expandHome(input, homeDirectory = process.env.HOME) {
  if (!input.startsWith('~/')) return input
  if (!homeDirectory) throw new Error('Cannot expand ~ because HOME is not set')
  return path.join(homeDirectory, input.slice(2))
}

async function loadConfig(root) {
  const target = path.join(root, '.devcodex.json')
  let parsed = {}

  try {
    parsed = JSON.parse(await readFile(target, 'utf8'))
  } catch (error) {
    if (error.code !== 'ENOENT') throw new Error(`Failed to read .devcodex.json: ${error.message}`)
  }

  return {
    gates: Array.isArray(parsed.gates) ? parsed.gates.map((gate) => ({ ...gate })) : [],
    skillRoots: Array.isArray(parsed.skillRoots)
      ? parsed.skillRoots.map((item) => expandHome(item))
      : DEFAULT_CONFIG.skillRoots.map((item) => expandHome(item)),
    permissions: parsed.permissions && typeof parsed.permissions === 'object'
      ? JSON.parse(JSON.stringify(parsed.permissions))
      : null,
    mcpProfile: parsed.mcpProfile === 'extended' ? 'extended' : 'core',
    resultBudgetChars: Number.isInteger(parsed.resultBudgetChars) && parsed.resultBudgetChars >= 2000 && parsed.resultBudgetChars <= 100000
      ? parsed.resultBudgetChars
      : DEFAULT_CONFIG.resultBudgetChars,
    hooks: parsed.hooks && typeof parsed.hooks === 'object' && !Array.isArray(parsed.hooks)
      ? JSON.parse(JSON.stringify(parsed.hooks))
      : {}
  }
}

module.exports = {
  DEFAULT_CONFIG,
  expandHome,
  loadConfig
}
