const { readFile } = require('node:fs/promises')
const path = require('node:path')

const { getChangeSummary } = require('./git')
const { listEntries } = require('./search')

const LANGUAGE_BY_EXTENSION = Object.freeze({
  '.c': 'C',
  '.cc': 'C++',
  '.cpp': 'C++',
  '.cs': 'C#',
  '.css': 'CSS',
  '.go': 'Go',
  '.html': 'HTML',
  '.java': 'Java',
  '.js': 'JavaScript',
  '.jsx': 'JavaScript',
  '.kt': 'Kotlin',
  '.kts': 'Kotlin',
  '.lua': 'Lua',
  '.php': 'PHP',
  '.py': 'Python',
  '.rb': 'Ruby',
  '.rs': 'Rust',
  '.sh': 'Shell',
  '.sql': 'SQL',
  '.swift': 'Swift',
  '.ts': 'TypeScript',
  '.tsx': 'TypeScript',
  '.vue': 'Vue',
  '.wgsl': 'WGSL'
})

const MANIFEST_NAMES = new Set([
  'Cargo.toml', 'go.mod', 'package.json', 'pyproject.toml', 'requirements.txt',
  'pom.xml', 'build.gradle', 'build.gradle.kts', 'Gemfile', 'composer.json'
])

function isTestPath(filePath) {
  const normalized = filePath.replaceAll('\\', '/')
  const basename = path.basename(normalized)
  return normalized.includes('/test/') || normalized.includes('/tests/') || normalized.startsWith('test/') || normalized.startsWith('tests/') || /\.(test|spec)\.[^.]+$/.test(basename)
}

async function readPackageScripts(root, files) {
  if (!files.some((file) => file.path === 'package.json')) return {}

  try {
    const parsed = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'))
    const scripts = parsed.scripts && typeof parsed.scripts === 'object' ? parsed.scripts : {}
    return Object.fromEntries(Object.entries(scripts).sort(([left], [right]) => left.localeCompare(right)))
  } catch {
    return {}
  }
}

function summarizeLanguages(files) {
  const counts = files.reduce((result, file) => {
    const language = LANGUAGE_BY_EXTENSION[path.extname(file.path).toLowerCase()]
    if (!language) return result
    return { ...result, [language]: (result[language] || 0) + 1 }
  }, {})

  return Object.fromEntries(Object.entries(counts).sort(([left], [right]) => left.localeCompare(right)))
}

async function buildRepoMap(root, options = {}) {
  const [entries, changes] = await Promise.all([
    listEntries(root, { maxDepth: options.maxDepth || 8 }),
    getChangeSummary(root)
  ])
  const files = entries.filter((entry) => entry.type === 'file')
  const directories = entries.filter((entry) => entry.type === 'directory')
  const scripts = await readPackageScripts(root, files)

  const topLevel = entries
    .filter((entry) => entry.depth === 0)
    .map((entry) => `${entry.path}${entry.type === 'directory' ? '/' : ''}`)
    .sort()

  return {
    name: path.basename(path.resolve(root)),
    git: {
      branch: changes.branch,
      head: changes.head,
      clean: changes.clean,
      changedFiles: changes.files.length
    },
    fileCount: files.length,
    directoryCount: directories.length,
    topLevel,
    languages: summarizeLanguages(files),
    manifests: files.map((file) => file.path).filter((filePath) => MANIFEST_NAMES.has(path.basename(filePath))).sort(),
    instructions: files.map((file) => file.path).filter((filePath) => ['AGENTS.md', 'CLAUDE.md'].includes(path.basename(filePath))).sort(),
    tests: files.map((file) => file.path).filter(isTestPath).sort(),
    scripts
  }
}

module.exports = {
  LANGUAGE_BY_EXTENSION,
  MANIFEST_NAMES,
  buildRepoMap,
  isTestPath,
  summarizeLanguages
}
