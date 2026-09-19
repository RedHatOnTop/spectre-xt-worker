const { createHash } = require('node:crypto')
const { execFile } = require('node:child_process')
const { readFile } = require('node:fs/promises')
const path = require('node:path')
const { promisify } = require('node:util')

const execFileAsync = promisify(execFile)
const INTERNAL_STATE_PREFIX = '.devcodex/'

async function runGit(root, args, options = {}) {
  try {
    const result = await execFileAsync('git', args, {
      cwd: root,
      maxBuffer: options.maxBuffer || 8 * 1024 * 1024
    })
    return result.stdout.trimEnd()
  } catch (error) {
    const detail = error.stderr ? `: ${error.stderr.trim()}` : ''
    throw new Error(`git ${args.join(' ')} failed${detail}`)
  }
}

async function getGitPrefix(root) {
  return await runGit(root, ['rev-parse', '--show-prefix'])
}

function scopeStatus(files, prefix) {
  if (!prefix) return files
  return files
    .filter((file) => file.path.startsWith(prefix))
    .map((file) => ({ ...file, path: file.path.slice(prefix.length) }))
}

function parseStatusLine(line) {
  const status = line.slice(0, 2)
  const rawPath = line.slice(3)
  const filePath = rawPath.includes(' -> ') ? rawPath.split(' -> ').at(-1) : rawPath
  return {
    path: filePath,
    status,
    staged: status[0] !== ' ' && status[0] !== '?',
    unstaged: status[1] !== ' ' && status[1] !== '?',
    untracked: status === '??'
  }
}

async function fingerprintChanges(root, status, patch) {
  const relevantStatus = status.filter((file) => !file.path.startsWith(INTERNAL_STATE_PREFIX))
  const untracked = relevantStatus.filter((file) => file.untracked).sort((left, right) => left.path.localeCompare(right.path))
  const untrackedContent = await Promise.all(untracked.map(async (file) => {
    try {
      const content = await readFile(path.join(root, file.path))
      return `${file.path}\0${createHash('sha256').update(content).digest('hex')}`
    } catch (error) {
      return `${file.path}\0unreadable:${error.code || 'unknown'}`
    }
  }))

  return createHash('sha256')
    .update(JSON.stringify(relevantStatus))
    .update('\0')
    .update(patch)
    .update('\0')
    .update(untrackedContent.join('\n'))
    .digest('hex')
}

async function getChangeSummary(root, options = {}) {
  const [branch, head, prefix, rawStatus, patch, stat] = await Promise.all([
    runGit(root, ['branch', '--show-current']),
    runGit(root, ['rev-parse', 'HEAD']),
    getGitPrefix(root),
    runGit(root, ['status', '--porcelain=v1', '--untracked-files=all']),
    runGit(root, ['diff', '--binary', 'HEAD', '--', '.'], { maxBuffer: options.maxBuffer }),
    runGit(root, ['diff', '--stat', 'HEAD', '--', '.'])
  ])

  const parsedFiles = rawStatus ? rawStatus.split('\n').filter(Boolean).map(parseStatusLine) : []
  const files = scopeStatus(parsedFiles, prefix)
    .filter((file) => !file.path.startsWith(INTERNAL_STATE_PREFIX))
  const fingerprint = await fingerprintChanges(root, files, patch)

  return {
    branch,
    head,
    clean: files.length === 0,
    files,
    stat,
    patch: options.includePatch ? patch : undefined,
    fingerprint
  }
}

async function getFileDiff(root, filePath) {
  if (typeof filePath !== 'string' || filePath.trim() === '' || path.isAbsolute(filePath) || filePath.includes('..')) {
    throw new Error('File diff requires a safe workspace-relative path')
  }

  const [patch, status] = await Promise.all([
    runGit(root, ['diff', '--binary', 'HEAD', '--', filePath]),
    runGit(root, ['status', '--porcelain=v1', '--untracked-files=all', '--', filePath])
  ])
  const untracked = status.startsWith('??')
  let untrackedContent
  if (untracked) {
    try {
      untrackedContent = (await readFile(path.join(root, filePath), 'utf8')).slice(0, 1024 * 1024)
    } catch {
      untrackedContent = undefined
    }
  }

  return { path: filePath, status: status.slice(0, 2), untracked, patch, untrackedContent }
}

module.exports = {
  getFileDiff,
  getGitPrefix,
  getChangeSummary,
  parseStatusLine,
  runGit,
  scopeStatus
}
