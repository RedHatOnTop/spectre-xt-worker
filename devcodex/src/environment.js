const { execFile } = require('node:child_process')
const os = require('node:os')
const { promisify } = require('node:util')

const { loadConfig } = require('./config')
const { getChangeSummary } = require('./git')
const { getIndexStatus } = require('./index')
const { discoverSkills } = require('./skills')

const execFileAsync = promisify(execFile)

const TOOL_SPECS = Object.freeze({
  node: [process.execPath, ['--version']],
  npm: ['npm', ['--version']],
  git: ['git', ['--version']],
  python: ['python3', ['--version']],
  cargo: ['cargo', ['--version']],
  rustc: ['rustc', ['--version']],
  docker: ['docker', ['--version']]
})

async function inspectTool(command, args) {
  try {
    const result = await execFileAsync(command, args, { maxBuffer: 1024 * 1024 })
    const version = `${result.stdout || ''}${result.stderr || ''}`.trim().split(/\r?\n/)[0]
    return { available: true, command, version }
  } catch (error) {
    if (error.code === 'ENOENT') return { available: false, command, version: null }
    return {
      available: true,
      command,
      version: null,
      error: String(error.stderr || error.message || 'unknown error').trim().slice(0, 500)
    }
  }
}

async function inspectTools() {
  const entries = await Promise.all(Object.entries(TOOL_SPECS).map(async ([name, [command, args]]) => [
    name,
    await inspectTool(command, args)
  ]))
  return Object.fromEntries(entries)
}

async function buildEnvironmentSnapshot(root) {
  const [tools, config, git, index] = await Promise.all([
    inspectTools(),
    loadConfig(root),
    getChangeSummary(root),
    getIndexStatus(root)
  ])
  const skills = await discoverSkills(config.skillRoots)

  return {
    generatedAt: new Date().toISOString(),
    runtime: {
      platform: process.platform,
      arch: process.arch,
      node: process.version,
      hostname: os.hostname(),
      cpus: os.cpus().length,
      totalMemoryBytes: os.totalmem(),
      shell: process.env.SHELL || null
    },
    tools,
    project: {
      root,
      git: {
        branch: git.branch,
        head: git.head,
        clean: git.clean,
        changedFiles: git.files.length
      },
      index
    },
    capabilities: {
      qualityGatesConfigured: config.gates.length > 0,
      qualityGateCount: config.gates.length,
      hooksConfigured: Object.values(config.hooks || {}).some((hooks) => Array.isArray(hooks) && hooks.length > 0),
      skillsDiscovered: skills.length,
      dockerAvailable: tools.docker.available
    }
  }
}

module.exports = {
  TOOL_SPECS,
  buildEnvironmentSnapshot,
  inspectTool,
  inspectTools
}
