const { createCheckpoint, verifyCheckpoint } = require('./checkpoint')
const { loadConfig } = require('./config')
const { buildContextPacket } = require('./context')
const { buildEnvironmentSnapshot } = require('./environment')
const { runQualityGates } = require('./gates')
const { getChangeSummary, getFileDiff } = require('./git')
const { runHooks } = require('./hooks')
const { editWorkspaceFile, runShellCommand, writeWorkspaceFile } = require('./fsops')
const { getIndexStatus, refreshWorkspaceIndex, searchWorkspaceIndex } = require('./index')
const { findInstructionsForPath, inspectFile, readMany, readWorkspaceFile, searchContext } = require('./inspect')
const { appendJournalEvent, readJournal } = require('./journal')
const { buildRuntimeMetrics } = require('./metrics')
const { buildTaskBootstrap, runCompletionGate } = require('./orchestrator')
const { checkPermission } = require('./permission')
const { buildRepoMap } = require('./repo-map')
const { boundResult, readResultPage } = require('./result-store')
const { createDetachedReview, reviewTarget, reviewWorkspace } = require('./review')
const { buildTree, searchWorkspace } = require('./search')
const { buildSymbolIndex, findReferences, outlineFile, searchSymbols } = require('./symbols')
const { readWorkspaceEvents } = require('./workspace-watch')
const { appendSessionEvent } = require('./session')
const { discoverSkills, rankSkills, readSkillByName } = require('./skills')

const objectSchema = (properties = {}, required = []) => ({
  type: 'object',
  properties,
  required,
  additionalProperties: false
})

const stringProperty = (description) => ({ type: 'string', minLength: 1, description })
const booleanProperty = (description) => ({ type: 'boolean', description })
const integerProperty = (description, minimum, maximum) => ({ type: 'integer', minimum, maximum, description })
const enumProperty = (description, values) => ({ type: 'string', enum: values, description })
const fileReadRequestsProperty = (description) => ({
  type: 'array',
  minItems: 1,
  maxItems: 20,
  description,
  items: objectSchema({
    path: stringProperty('Workspace-relative file path.'),
    offset: integerProperty('1-indexed starting line.', 1, 1000000000),
    limit: integerProperty('Maximum lines to return.', 1, 5000)
  }, ['path'])
})

const TOOL_PERMISSION_ACTIONS = Object.freeze({
  workspace_tree: 'read',
  search_code: 'search',
  search_context: 'search',
  read_file: 'read',
  write_file: 'fileWrite',
  edit_file: 'fileWrite',
  run_command: 'shell',
  read_many: 'read',
  inspect_file: 'read',
  instructions_for: 'read',
  show_changes: 'read',
  file_diff: 'read',
  build_context: 'read',
  task_bootstrap: 'sessionWrite',
  repo_map: 'read',
  recommend_skills: 'read',
  read_skill: 'read',
  review_changes: 'review',
  review_target: 'review',
  detached_review: 'review',
  verify_workspace: 'verify',
  create_checkpoint: 'checkpoint',
  check_checkpoint: 'read',
  note_task_session: 'sessionWrite',
  environment_snapshot: 'environment',
  permission_check: 'read',
  event_journal: 'read',
  result_page: 'read',
  code_navigation: 'search',
  index_status: 'read',
  index_refresh: 'search',
  run_hook: 'shell',
  workspace_events: 'read',
  runtime_metrics: 'read',
  completion_gate: 'verify'
})

const CORE_TOOL_NAMES = new Set([
  'search_context',
  'read_many',
  'inspect_file',
  'write_file',
  'edit_file',
  'run_command',
  'show_changes',
  'file_diff',
  'task_bootstrap',
  'note_task_session',
  'verify_workspace',
  'result_page',
  'code_navigation',
  'completion_gate'
])

function assertObject(value) {
  if (value === undefined) return {}
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Tool arguments must be an object')
  return value
}

function requireString(args, name) {
  if (typeof args[name] !== 'string' || args[name].trim() === '') throw new Error(`Missing required string argument: ${name}`)
  return args[name]
}

function optionalInteger(args, name, fallback, minimum, maximum) {
  if (args[name] === undefined) return fallback
  if (!Number.isInteger(args[name]) || args[name] < minimum || args[name] > maximum) {
    throw new Error(`${name} must be an integer between ${minimum} and ${maximum}`)
  }
  return args[name]
}

function createToolRegistry(root, options = {}) {
  const tools = [
    {
      name: 'workspace_tree',
      description: 'Return a compact workspace tree while ignoring generated and dependency directories.',
      inputSchema: objectSchema({ depth: integerProperty('Maximum traversal depth.', 0, 20) }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await buildTree(root, { maxDepth: optionalInteger(args, 'depth', 4, 0, 20) })
      }
    },
    {
      name: 'search_code',
      description: 'Search text files in the workspace with optional regex and case-sensitive matching.',
      inputSchema: objectSchema({
        query: stringProperty('Text or regular expression to search for.'),
        regex: booleanProperty('Interpret query as a JavaScript regular expression.'),
        caseSensitive: booleanProperty('Use case-sensitive matching.'),
        maxResults: integerProperty('Maximum matches to return.', 1, 1000)
      }, ['query']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        if (options.liveIndex !== true) await refreshWorkspaceIndex(root)
        return await searchWorkspaceIndex(root, requireString(args, 'query'), {
          regex: args.regex === true,
          caseSensitive: args.caseSensitive === true,
          maxResults: optionalInteger(args, 'maxResults', 200, 1, 1000)
        })
      }
    },
    {
      name: 'search_context',
      description: 'Search code and return bounded surrounding source for the top matches in one call.',
      inputSchema: objectSchema({
        query: stringProperty('Text or regular expression to search for.'),
        regex: booleanProperty('Interpret query as a JavaScript regular expression.'),
        caseSensitive: booleanProperty('Use case-sensitive matching.'),
        maxMatches: integerProperty('Maximum matches with context to return.', 1, 50),
        contextLines: integerProperty('Lines of source context on each side.', 0, 50)
      }, ['query']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await searchContext(root, requireString(args, 'query'), {
          regex: args.regex === true,
          caseSensitive: args.caseSensitive === true,
          maxMatches: optionalInteger(args, 'maxMatches', 8, 1, 50),
          contextLines: optionalInteger(args, 'contextLines', 3, 0, 50)
        })
      }
    },
    {
      name: 'code_navigation',
      description: 'Navigate code structure using a cached multi-language symbol index: search definitions, outline a file, or find identifier references.',
      inputSchema: objectSchema({
        mode: enumProperty('Navigation operation.', ['symbols', 'outline', 'references']),
        query: stringProperty('Symbol-name query for symbols mode.'),
        path: stringProperty('Workspace-relative file path for outline mode.'),
        symbol: stringProperty('Exact simple identifier for references mode.'),
        limit: integerProperty('Maximum results.', 1, 500)
      }, ['mode']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        await buildSymbolIndex(root)
        const limit = optionalInteger(args, 'limit', 100, 1, 500)
        if (args.mode === 'symbols') return await searchSymbols(root, requireString(args, 'query'), { limit })
        if (args.mode === 'outline') return await outlineFile(root, requireString(args, 'path'))
        if (args.mode === 'references') return await findReferences(root, requireString(args, 'symbol'), { limit })
        throw new Error(`Unknown code navigation mode: ${args.mode}`)
      }
    },
    {
      name: 'read_file',
      description: 'Read a bounded line range from a text file inside the fixed workspace root.',
      inputSchema: objectSchema({
        path: stringProperty('Workspace-relative file path.'),
        offset: integerProperty('1-indexed starting line.', 1, 1000000000),
        limit: integerProperty('Maximum lines to return.', 1, 5000)
      }, ['path']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await readWorkspaceFile(root, requireString(args, 'path'), {
          offset: optionalInteger(args, 'offset', 1, 1, 1000000000),
          limit: optionalInteger(args, 'limit', 200, 1, 5000)
        })
      }
    },
    {
      name: 'write_file',
      description: 'Write a text file inside the fixed workspace root, creating parent directories as needed. The content replaces the file entirely.',
      inputSchema: objectSchema({
        path: stringProperty('Workspace-relative file path.'),
        content: { type: 'string', description: 'Full text content of the file. May be empty.' }
      }, ['path', 'content']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await writeWorkspaceFile(root, requireString(args, 'path'), args.content)
      }
    },
    {
      name: 'edit_file',
      description: 'Replace an exact text occurrence in a workspace file. Fails when oldText is absent; requires replaceAll when it occurs more than once.',
      inputSchema: objectSchema({
        path: stringProperty('Workspace-relative file path.'),
        oldText: stringProperty('Exact text to replace.'),
        newText: { type: 'string', description: 'Replacement text. May be empty to delete the match.' },
        replaceAll: booleanProperty('Replace every occurrence instead of requiring a unique match.')
      }, ['path', 'oldText', 'newText']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await editWorkspaceFile(root, requireString(args, 'path'), requireString(args, 'oldText'), args.newText, {
          replaceAll: args.replaceAll === true
        })
      }
    },
    {
      name: 'run_command',
      description: 'Run a shell command with /bin/sh -lc in the workspace root and return exit code, bounded output, and timing.',
      inputSchema: objectSchema({
        command: stringProperty('Shell command to run in the workspace root.'),
        timeoutMs: integerProperty('Optional timeout in milliseconds.', 1000, 3600000)
      }, ['command']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await runShellCommand(root, requireString(args, 'command'), {
          timeoutMs: optionalInteger(args, 'timeoutMs', undefined, 1000, 3600000)
        })
      }
    },
    {
      name: 'read_many',
      description: 'Read up to 20 bounded file windows in one call to reduce tool round trips.',
      inputSchema: objectSchema({ files: fileReadRequestsProperty('File windows to read.') }, ['files']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await readMany(root, args.files)
      }
    },
    {
      name: 'inspect_file',
      description: 'Read a bounded file window together with every AGENTS.md/CLAUDE.md instruction that applies to that path.',
      inputSchema: objectSchema({
        path: stringProperty('Workspace-relative file path.'),
        offset: integerProperty('1-indexed starting line.', 1, 1000000000),
        limit: integerProperty('Maximum lines to return.', 1, 5000)
      }, ['path']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await inspectFile(root, requireString(args, 'path'), {
          offset: optionalInteger(args, 'offset', 1, 1, 1000000000),
          limit: optionalInteger(args, 'limit', 240, 1, 5000)
        })
      }
    },
    {
      name: 'instructions_for',
      description: 'Resolve AGENTS.md and CLAUDE.md instructions that apply to a workspace-relative path, from root to leaf.',
      inputSchema: objectSchema({ path: stringProperty('Workspace-relative file or directory path.') }, ['path']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await findInstructionsForPath(root, requireString(args, 'path'))
      }
    },
    {
      name: 'show_changes',
      description: 'Return structured Git branch, HEAD, changed files, diff stats, fingerprint, and optionally the patch.',
      inputSchema: objectSchema({ includePatch: booleanProperty('Include the tracked unified diff.') }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await getChangeSummary(root, { includePatch: args.includePatch === true })
      }
    },
    {
      name: 'file_diff',
      description: 'Return the current Git diff for exactly one workspace-relative file, avoiding a repository-wide patch.',
      inputSchema: objectSchema({ path: stringProperty('Workspace-relative file path.') }, ['path']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await getFileDiff(root, requireString(args, 'path'))
      }
    },
    {
      name: 'build_context',
      description: 'Build an agent context packet from instructions, changes, TODO/FIXME markers, and task-relevant files.',
      inputSchema: objectSchema({ task: stringProperty('Task the coding agent is trying to complete.') }, ['task']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await buildContextPacket(root, requireString(args, 'task'))
      }
    },
    {
      name: 'task_bootstrap',
      description: 'Start a durable task session with compact workspace context, or recover an existing active task in a fresh context.',
      inputSchema: objectSchema({
        task: stringProperty('New or updated task description. May be omitted when recovering a session.'),
        sessionId: stringProperty('Specific durable task session to recover.'),
        resumeLatest: booleanProperty('Recover the most recently updated active task session.')
      }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await buildTaskBootstrap(root, args.task, {
          sessionId: args.sessionId,
          resumeLatest: args.resumeLatest === true
        })
      }
    },
    {
      name: 'repo_map',
      description: 'Summarize repository shape, languages, manifests, tests, package scripts, instructions, and Git state.',
      inputSchema: objectSchema({ maxDepth: integerProperty('Maximum traversal depth.', 1, 20) }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await buildRepoMap(root, { maxDepth: optionalInteger(args, 'maxDepth', 8, 1, 20) })
      }
    },
    {
      name: 'recommend_skills',
      description: 'Discover Codex/agent SKILL.md files and rank them against a coding task.',
      inputSchema: objectSchema({
        task: stringProperty('Task used for skill relevance ranking.'),
        limit: integerProperty('Maximum ranked skills to return.', 1, 100)
      }, ['task']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        const config = await loadConfig(root)
        const skills = await discoverSkills(config.skillRoots)
        return rankSkills(skills, requireString(args, 'task')).slice(0, optionalInteger(args, 'limit', 12, 1, 100))
      }
    },
    {
      name: 'read_skill',
      description: 'Read the full SKILL.md content for an exact discovered Codex/agent skill name.',
      inputSchema: objectSchema({ name: stringProperty('Exact discovered skill name.') }, ['name']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        const config = await loadConfig(root)
        return await readSkillByName(config.skillRoots, requireString(args, 'name'))
      }
    },
    {
      name: 'review_changes',
      description: 'Perform a deterministic preflight review of changed and untracked text for conflict markers, debug statements, TODOs, and obvious credential material.',
      inputSchema: objectSchema(),
      handler: async () => await reviewWorkspace(root)
    },
    {
      name: 'review_target',
      description: 'Run deterministic review against uncommitted changes, a base branch/ref, or a commit.',
      inputSchema: objectSchema({
        type: enumProperty('Review target type.', ['uncommittedChanges', 'baseBranch', 'commit']),
        branch: stringProperty('Base branch or ref for baseBranch review.'),
        sha: stringProperty('Commit SHA for commit review.'),
        title: stringProperty('Optional human-readable commit title.')
      }, ['type']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await reviewTarget(root, {
          type: requireString(args, 'type'),
          branch: args.branch,
          sha: args.sha,
          title: args.title
        })
      }
    },
    {
      name: 'detached_review',
      description: 'Run a deterministic review and persist the result as a detached review artifact without changing source files.',
      inputSchema: objectSchema({
        type: enumProperty('Review target type.', ['uncommittedChanges', 'baseBranch', 'commit']),
        branch: stringProperty('Base branch or ref for baseBranch review.'),
        sha: stringProperty('Commit SHA for commit review.'),
        title: stringProperty('Optional human-readable commit title.')
      }, ['type']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await createDetachedReview(root, {
          type: requireString(args, 'type'),
          branch: args.branch,
          sha: args.sha,
          title: args.title
        })
      }
    },
    {
      name: 'verify_workspace',
      description: 'Run configured quality gates and return evidence including exit codes, output, and timing.',
      inputSchema: objectSchema({ stopOnFailure: booleanProperty('Stop after the first required gate failure.') }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        const config = await loadConfig(root)
        if (config.gates.length === 0) return { ok: true, reason: 'no-quality-gates-configured', results: [] }
        return await runQualityGates(root, config.gates, { stopOnFailure: args.stopOnFailure === true })
      }
    },
    {
      name: 'create_checkpoint',
      description: 'Record HEAD and workspace fingerprint, optionally only after configured quality gates pass.',
      inputSchema: objectSchema({
        name: stringProperty('Checkpoint name.'),
        verify: booleanProperty('Require all configured quality gates to pass before creating the checkpoint.')
      }, ['name']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        let verification = null
        if (args.verify === true) {
          const config = await loadConfig(root)
          verification = config.gates.length > 0
            ? await runQualityGates(root, config.gates)
            : { ok: true, reason: 'no-quality-gates-configured', results: [] }
          if (!verification.ok) return { created: false, reason: 'quality-gates-failed', verification }
        }
        return { created: true, checkpoint: await createCheckpoint(root, requireString(args, 'name'), verification) }
      }
    },
    {
      name: 'check_checkpoint',
      description: 'Compare current HEAD and workspace fingerprint against a named checkpoint.',
      inputSchema: objectSchema({ name: stringProperty('Checkpoint name.') }, ['name']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await verifyCheckpoint(root, requireString(args, 'name'))
      }
    },
    {
      name: 'note_task_session',
      description: 'Append a typed note, finding, decision, or verification event to a task session.',
      inputSchema: objectSchema({
        id: stringProperty('Task session id.'),
        type: stringProperty('Event type such as note, finding, decision, or verification.'),
        message: stringProperty('Event message.')
      }, ['id', 'type', 'message']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await appendSessionEvent(root, requireString(args, 'id'), {
          type: requireString(args, 'type'),
          message: requireString(args, 'message')
        })
      }
    },
    {
      name: 'environment_snapshot',
      description: 'Inspect local runtime, toolchain availability, Git state, quality-gate configuration, and discovered skills.',
      inputSchema: objectSchema(),
      handler: async () => await buildEnvironmentSnapshot(root)
    },
    {
      name: 'index_status',
      description: 'Inspect the incremental compressed workspace search index without exposing cached file contents.',
      inputSchema: objectSchema(),
      handler: async () => await getIndexStatus(root)
    },
    {
      name: 'index_refresh',
      description: 'Refresh the incremental workspace index, rereading only files whose size or mtime changed.',
      inputSchema: objectSchema(),
      handler: async () => await refreshWorkspaceIndex(root)
    },
    {
      name: 'run_hook',
      description: 'Run one configured DevCodex lifecycle hook set. This is an extended shell-capable operation and is subject to permission policy.',
      inputSchema: objectSchema({
        hook: enumProperty('Lifecycle hook to execute.', ['beforeTask', 'afterTask', 'beforeVerify', 'afterVerify', 'beforeCompletion', 'afterCompletion'])
      }, ['hook']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await runHooks(root, requireString(args, 'hook'))
      }
    },
    {
      name: 'permission_check',
      description: 'Resolve an action through the active or named DevCodex permission profile.',
      inputSchema: objectSchema({
        action: stringProperty('Permission action such as read, shell, patch, fileDelete, or gitReset.'),
        profile: stringProperty('Optional named permission profile.')
      }, ['action']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await checkPermission(root, requireString(args, 'action'), args.profile)
      }
    },
    {
      name: 'event_journal',
      description: 'Read the append-only DevCodex runtime event journal. Tool arguments are intentionally not recorded in journal entries.',
      inputSchema: objectSchema({
        type: stringProperty('Optional exact event type filter.'),
        limit: integerProperty('Maximum most-recent events.', 1, 10000)
      }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await readJournal(root, { type: args.type, limit: optionalInteger(args, 'limit', 100, 1, 10000) })
      }
    },
    {
      name: 'workspace_events',
      description: 'Read recent daemon workspace filesystem events using a monotonic sequence cursor.',
      inputSchema: objectSchema({
        sinceSeq: integerProperty('Return events after this sequence number.', 0, 1000000000),
        limit: integerProperty('Maximum recent events.', 1, 1000)
      }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await readWorkspaceEvents(root, {
          sinceSeq: args.sinceSeq,
          limit: optionalInteger(args, 'limit', 100, 1, 1000)
        })
      }
    },
    {
      name: 'runtime_metrics',
      description: 'Aggregate DevCodex tool latency, failures, blocked calls, truncation, and result-size savings from the append-only journal.',
      inputSchema: objectSchema({
        limit: integerProperty('Maximum recent journal events to aggregate.', 1, 10000)
      }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await buildRuntimeMetrics(root, { limit: optionalInteger(args, 'limit', 10000, 1, 10000) })
      }
    },
    {
      name: 'result_page',
      description: 'Read a bounded character page from a large tool result that DevCodex stored behind a result handle.',
      inputSchema: objectSchema({
        handle: stringProperty('Result handle returned by a truncated tool response.'),
        offset: integerProperty('0-indexed character offset.', 0, 1000000000),
        limit: integerProperty('Maximum characters to return.', 1, 50000)
      }, ['handle']),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await readResultPage(root, requireString(args, 'handle'), {
          offset: optionalInteger(args, 'offset', 0, 0, 1000000000),
          limit: optionalInteger(args, 'limit', 8000, 1, 50000)
        })
      }
    },
    {
      name: 'completion_gate',
      description: 'Run quality gates, deterministic review, capture final changes, and optionally create a verified checkpoint and append evidence to a task session.',
      inputSchema: objectSchema({
        checkpointName: stringProperty('Optional checkpoint to create only when completion passes.'),
        sessionId: stringProperty('Optional task session to receive a verification event.'),
        includePatch: booleanProperty('Include the tracked final patch in the evidence bundle.'),
        stopOnFailure: booleanProperty('Stop verification on the first required quality-gate failure.')
      }),
      handler: async (rawArgs) => {
        const args = assertObject(rawArgs)
        return await runCompletionGate(root, {
          checkpointName: args.checkpointName,
          sessionId: args.sessionId,
          includePatch: args.includePatch === true,
          stopOnFailure: args.stopOnFailure === true
        })
      }
    }
  ]

  const profile = options.profile || 'extended'
  if (!['core', 'extended'].includes(profile)) throw new Error(`Unknown MCP tool profile: ${profile}`)
  const visibleTools = profile === 'core' ? tools.filter((tool) => CORE_TOOL_NAMES.has(tool.name)) : tools
  const byName = new Map(visibleTools.map((tool) => [tool.name, tool]))
  return Object.freeze({
    profile,
    list: () => visibleTools.map(({ handler, permissionAction, ...definition }) => ({ ...definition })),
    call: async (name, args) => {
      const tool = byName.get(name)
      if (!tool) throw new Error(`Unknown tool: ${name}`)
      const action = typeof tool.permissionAction === 'function'
        ? tool.permissionAction(args)
        : TOOL_PERMISSION_ACTIONS[name]
      if (!action) throw new Error(`Tool ${name} is missing a permission classification`)
      const permission = await checkPermission(root, action)
      if (permission.decision !== 'allow') {
        await appendJournalEvent(root, {
          type: 'tool.blocked',
          tool: name,
          permissionAction: action,
          permissionDecision: permission.decision
        })
        throw new Error(`Tool ${name} requires permission decision ${permission.decision} for action ${action}`)
      }

      const started = Date.now()
      await appendJournalEvent(root, { type: 'tool.started', tool: name, permissionAction: action })
      try {
        const result = await tool.handler(args)
        const bounded = await boundResult(root, result, { maxChars: options.resultBudgetChars })
        const returnedChars = JSON.stringify(bounded).length
        const resultChars = bounded && bounded.truncated === true && Number.isFinite(bounded.totalChars)
          ? bounded.totalChars
          : returnedChars
        await appendJournalEvent(root, {
          type: 'tool.completed',
          tool: name,
          permissionAction: action,
          durationMs: Date.now() - started,
          resultChars,
          returnedChars,
          truncated: bounded && bounded.truncated === true
        })
        return bounded
      } catch (error) {
        await appendJournalEvent(root, {
          type: 'tool.failed',
          tool: name,
          permissionAction: action,
          durationMs: Date.now() - started,
          error: error.message.slice(0, 500)
        })
        throw error
      }
    }
  })
}

module.exports = {
  assertObject,
  CORE_TOOL_NAMES,
  createToolRegistry,
  enumProperty,
  objectSchema,
  optionalInteger,
  requireString,
  TOOL_PERMISSION_ACTIONS
}
