const { createCheckpoint } = require('./checkpoint')
const { loadConfig } = require('./config')
const { buildContextPacket } = require('./context')
const { runQualityGates } = require('./gates')
const { getChangeSummary } = require('./git')
const { runHookSet } = require('./hooks')
const { buildRepoMap } = require('./repo-map')
const { reviewWorkspace } = require('./review')
const {
  activeTimeline,
  appendSessionEvent,
  completeSession,
  getLatestActiveSession,
  getSessionStatus,
  startSession
} = require('./session')
const { discoverSkills, rankSkills } = require('./skills')

function deriveNextActions(snapshot) {
  const actions = []

  if (snapshot.review.summary.high > 0) {
    actions.push('Resolve high-severity deterministic review findings before making further changes.')
  }
  if (snapshot.context.relevantFiles.length === 0) {
    actions.push('Broaden code search because no task-relevant files were identified.')
  } else {
    actions.push('Inspect the highest-ranked relevant files and resolve path-specific instructions before editing.')
  }
  if (snapshot.context.changes.clean) {
    actions.push('Establish the intended behavior with tests or a reproducible failure before implementation.')
  } else {
    actions.push('Review the existing change set before layering additional edits on top of it.')
  }
  if (snapshot.skills.some((skill) => skill.score > 0)) {
    actions.push('Read the highest-ranked applicable skill before starting the matching workflow.')
  }
  actions.push('Run configured quality gates and inspect the final diff before claiming completion.')

  return actions
}

function truncateInstruction(instruction) {
  const limit = 2400
  return {
    path: instruction.path,
    content: instruction.content.slice(0, limit),
    truncated: instruction.truncated || instruction.content.length > limit
  }
}

async function resolveBootstrapSession(root, options) {
  if (options.sessionId) return await getSessionStatus(root, options.sessionId)
  if (options.resumeLatest === true) {
    const latest = await getLatestActiveSession(root)
    if (latest) return await getSessionStatus(root, latest.id)
  }
  return null
}

async function buildTaskBootstrap(root, task, options = {}) {
  let sessionStatus = await resolveBootstrapSession(root, options)
  const normalizedTask = typeof task === 'string' && task.trim()
    ? task.trim()
    : sessionStatus?.session.task
  if (!normalizedTask) throw new Error('Task bootstrap requires a non-empty task or a recoverable task session')
  const config = await loadConfig(root)
  const [context, repoMap, review, discoveredSkills] = await Promise.all([
    buildContextPacket(root, normalizedTask),
    buildRepoMap(root),
    reviewWorkspace(root),
    discoverSkills(config.skillRoots)
  ])
  const skills = rankSkills(discoveredSkills, normalizedTask).filter((skill) => skill.score > 0).slice(0, 5)
  const snapshot = { context, repoMap, review, skills }
  if (!sessionStatus) {
    const created = await startSession(root, normalizedTask)
    sessionStatus = await getSessionStatus(root, created.id)
  }

  return {
    task: normalizedTask,
    generatedAt: new Date().toISOString(),
    repository: {
      name: repoMap.name,
      git: repoMap.git,
      fileCount: repoMap.fileCount,
      directoryCount: repoMap.directoryCount,
      languages: repoMap.languages,
      manifests: repoMap.manifests,
      testCount: repoMap.tests.length,
      scripts: repoMap.scripts,
      topLevel: repoMap.topLevel.slice(0, 30)
    },
    changes: {
      clean: context.changes.clean,
      fingerprint: context.changes.fingerprint,
      files: context.changes.files.slice(0, 30)
    },
    instructions: context.instructions.map(truncateInstruction),
    relevantFiles: context.relevantFiles.slice(0, 10),
    todos: context.todos.slice(0, 12),
    review: { ok: review.ok, summary: review.summary, findings: review.findings.slice(0, 8) },
    skills,
    session: sessionStatus ? {
      id: sessionStatus.session.id,
      task: sessionStatus.session.task,
      status: sessionStatus.session.status,
      revision: sessionStatus.session.revision,
      createdAt: sessionStatus.session.createdAt,
      updatedAt: sessionStatus.session.updatedAt,
      drifted: sessionStatus.drifted,
      baseline: sessionStatus.session.baseline,
      current: sessionStatus.current,
      timeline: activeTimeline(sessionStatus.session).slice(-12)
    } : null,
    nextActions: deriveNextActions(snapshot)
  }
}

async function runCompletionGate(root, options = {}) {
  const config = await loadConfig(root)
  const beforeVerify = await runHookSet(root, config.hooks, 'beforeVerify')
  const verification = beforeVerify.ok
    ? (config.gates.length > 0
        ? await runQualityGates(root, config.gates, { stopOnFailure: options.stopOnFailure === true })
        : { ok: true, reason: 'no-quality-gates-configured', results: [] })
    : { ok: false, reason: 'before-verify-hook-failed', results: [] }
  const afterVerify = beforeVerify.ok
    ? await runHookSet(root, config.hooks, 'afterVerify')
    : { hook: 'afterVerify', ok: false, skipped: true, results: [] }
  const review = await reviewWorkspace(root)
  const changes = await getChangeSummary(root, { includePatch: options.includePatch === true })
  const preCompletionOk = beforeVerify.ok && verification.ok && afterVerify.ok && review.ok
  const beforeCompletion = preCompletionOk
    ? await runHookSet(root, config.hooks, 'beforeCompletion')
    : { hook: 'beforeCompletion', ok: false, skipped: true, results: [] }
  const afterCompletion = preCompletionOk && beforeCompletion.ok
    ? await runHookSet(root, config.hooks, 'afterCompletion')
    : { hook: 'afterCompletion', ok: false, skipped: true, results: [] }
  const ok = preCompletionOk && beforeCompletion.ok && afterCompletion.ok
  const checkpoint = ok && options.checkpointName
    ? { created: true, checkpoint: await createCheckpoint(root, options.checkpointName, verification) }
    : null

  let sessionUpdated = false
  if (options.sessionId) {
    const message = ok
      ? `Completion gate passed${verification.reason === 'no-quality-gates-configured' ? ' (no quality gates configured)' : ''}${checkpoint ? `; checkpoint ${checkpoint.checkpoint.name} created` : ''}.`
      : `Completion gate failed: verification=${verification.ok}, review=${review.ok}.`
    if (ok) await completeSession(root, options.sessionId, message)
    else await appendSessionEvent(root, options.sessionId, { type: 'verification', message })
    sessionUpdated = true
  }

  return {
    ok,
    generatedAt: new Date().toISOString(),
    verification,
    hooks: { beforeVerify, afterVerify, beforeCompletion, afterCompletion },
    review,
    changes,
    checkpoint,
    sessionUpdated
  }
}

module.exports = {
  buildTaskBootstrap,
  deriveNextActions,
  runCompletionGate
}
