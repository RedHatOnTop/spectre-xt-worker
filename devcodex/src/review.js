const { randomUUID } = require('node:crypto')
const { mkdir, writeFile } = require('node:fs/promises')
const path = require('node:path')

const { readWorkspaceFile } = require('./inspect')
const { getChangeSummary, runGit } = require('./git')

const REVIEW_DIR = '.devcodex/reviews'

const RULES = Object.freeze([
  {
    name: 'merge-conflict-marker',
    severity: 'high',
    pattern: /^(?:<{7}|={7}|>{7})/,
    message: 'Unresolved merge-conflict marker is present.'
  },
  {
    name: 'private-key-material',
    severity: 'high',
    pattern: /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b/,
    message: 'Potential credential or private key material is present.'
  },
  {
    name: 'debug-statement',
    severity: 'medium',
    pattern: /\bconsole\.log\s*\(|\bdebugger\s*;?/,
    message: 'Debug statement was added to the change set.',
    sanitizeStrings: true
  },
  {
    name: 'todo-added',
    severity: 'low',
    pattern: /(?:\/\/|#|\/\*|\*|<!--)\s*(?:TODO|FIXME)\b/,
    message: 'TODO/FIXME marker was added to the change set.',
    sanitizeStrings: true
  }
])

function stripQuotedLiterals(input) {
  let quote = null
  let escaped = false
  let output = ''

  for (const character of input) {
    if (quote) {
      if (escaped) {
        escaped = false
        continue
      }
      if (character === '\\') {
        escaped = true
        continue
      }
      if (character === quote) {
        quote = null
        output += character
      }
      continue
    }

    if (character === '\'' || character === '"' || character === '`') {
      quote = character
      output += character
      continue
    }
    output += character
  }

  return output
}

function extractAddedLines(patch) {
  const results = []
  let currentPath = null
  let newLine = 0

  for (const line of patch.split(/\r?\n/)) {
    if (line.startsWith('+++ ')) {
      const marker = line.slice(4)
      currentPath = marker === '/dev/null' ? null : marker.replace(/^b\//, '')
      continue
    }
    if (line.startsWith('@@')) {
      const match = line.match(/\+(\d+)(?:,(\d+))?/) 
      if (match) newLine = Number.parseInt(match[1], 10)
      continue
    }
    if (!currentPath) continue
    if (line.startsWith('+') && !line.startsWith('+++')) {
      results.push({ path: currentPath, line: newLine, text: line.slice(1) })
      newLine += 1
      continue
    }
    if (line.startsWith('-') && !line.startsWith('---')) continue
    if (!line.startsWith('\\')) newLine += 1
  }

  return results
}

async function collectUntrackedLines(root, files) {
  const untracked = files.filter((file) => file.untracked)
  const chunks = await Promise.all(untracked.map(async (file) => {
    try {
      const result = await readWorkspaceFile(root, file.path, { limit: 10000, maxBytes: 1024 * 1024 })
      return result.content.split(/\r?\n/).map((text, index) => ({ path: file.path, line: index + 1, text }))
    } catch {
      return []
    }
  }))
  return chunks.flat()
}

function analyzeLines(lines) {
  return lines.flatMap((line) => RULES
    .filter((rule) => rule.pattern.test(rule.sanitizeStrings ? stripQuotedLiterals(line.text) : line.text))
    .map((rule) => ({
      severity: rule.severity,
      rule: rule.name,
      path: line.path,
      line: line.line,
      message: rule.message,
      excerpt: line.text.trim().slice(0, 240)
    })))
}

function summarizeFindings(findings) {
  return ['high', 'medium', 'low'].reduce((result, severity) => ({
    ...result,
    [severity]: findings.filter((finding) => finding.severity === severity).length
  }), {})
}

async function reviewWorkspace(root) {
  return await reviewTarget(root, { type: 'uncommittedChanges' })
}

function validateReviewTarget(target) {
  if (!target || typeof target !== 'object') throw new Error('Review target must be an object')
  if (target.type === 'uncommittedChanges') return { type: target.type }
  if (target.type === 'baseBranch') {
    if (typeof target.branch !== 'string' || target.branch.trim() === '') throw new Error('baseBranch review requires branch')
    return { type: target.type, branch: target.branch.trim() }
  }
  if (target.type === 'commit') {
    if (typeof target.sha !== 'string' || target.sha.trim() === '') throw new Error('commit review requires sha')
    return { type: target.type, sha: target.sha.trim(), title: target.title ? String(target.title) : null }
  }
  throw new Error(`Unsupported deterministic review target: ${target.type}`)
}

async function patchForTarget(root, target) {
  if (target.type === 'uncommittedChanges') {
    const changes = await getChangeSummary(root, { includePatch: true })
    return {
      patch: changes.patch || '',
      files: changes.files,
      untrackedLines: await collectUntrackedLines(root, changes.files)
    }
  }
  if (target.type === 'baseBranch') {
    const patch = await runGit(root, ['diff', '--binary', `${target.branch}...HEAD`, '--', '.'])
    return { patch, files: [], untrackedLines: [] }
  }
  const patch = await runGit(root, ['show', '--format=', '--binary', target.sha, '--', '.'])
  return { patch, files: [], untrackedLines: [] }
}

async function reviewTarget(root, rawTarget) {
  const target = validateReviewTarget(rawTarget)
  const changeSet = await patchForTarget(root, target)
  const trackedAddedLines = extractAddedLines(changeSet.patch)
  const untrackedLines = changeSet.untrackedLines
  const findings = analyzeLines([...trackedAddedLines, ...untrackedLines])
    .sort((left, right) => left.path.localeCompare(right.path) || left.line - right.line || left.rule.localeCompare(right.rule))
  const summary = summarizeFindings(findings)

  return {
    target,
    ok: summary.high === 0,
    reviewedFiles: new Set([...trackedAddedLines.map((line) => line.path), ...untrackedLines.map((line) => line.path)]).size,
    summary,
    findings
  }
}

async function createDetachedReview(root, target) {
  const review = await reviewTarget(root, target)
  const createdAt = new Date().toISOString()
  const id = `${createdAt.replace(/[-:.TZ]/g, '').slice(0, 14)}-${randomUUID().slice(0, 8)}`
  const record = { id, delivery: 'detached', createdAt, review }
  const directory = path.join(root, REVIEW_DIR)
  const targetPath = path.join(directory, `${id}.json`)
  await mkdir(directory, { recursive: true })
  await writeFile(targetPath, `${JSON.stringify(record, null, 2)}\n`)
  return { ...record, path: path.relative(root, targetPath) }
}

module.exports = {
  REVIEW_DIR,
  RULES,
  analyzeLines,
  createDetachedReview,
  extractAddedLines,
  patchForTarget,
  reviewTarget,
  reviewWorkspace,
  stripQuotedLiterals,
  summarizeFindings,
  validateReviewTarget
}
