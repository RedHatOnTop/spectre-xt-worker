#!/usr/bin/env node

const { readFile } = require('node:fs/promises')
const path = require('node:path')

const { createCheckpoint, verifyCheckpoint } = require('./checkpoint')
const { loadConfig } = require('./config')
const { buildContextPacket } = require('./context')
const { getDaemonStatus, serveDaemonProxyStdio, startDaemonServer, stopDaemonServer } = require('./daemon')
const { buildEnvironmentSnapshot } = require('./environment')
const { editWorkspaceFile, runShellCommand, writeWorkspaceFile } = require('./fsops')
const { runQualityGates } = require('./gates')
const { getChangeSummary, getFileDiff } = require('./git')
const { runHooks } = require('./hooks')
const { getIndexStatus, refreshWorkspaceIndex, searchWorkspaceIndex } = require('./index')
const { findInstructionsForPath, inspectFile, readMany, readWorkspaceFile, searchContext } = require('./inspect')
const { readJournal } = require('./journal')
const { buildRuntimeMetrics } = require('./metrics')
const { serveStdio } = require('./mcp')
const { buildTaskBootstrap, runCompletionGate } = require('./orchestrator')
const { checkPermission } = require('./permission')
const { buildRepoMap } = require('./repo-map')
const { readResultPage } = require('./result-store')
const { createDetachedReview, reviewTarget, reviewWorkspace } = require('./review')
const { buildTree, searchWorkspace } = require('./search')
const { buildSymbolIndex, findReferences, outlineFile, searchSymbols } = require('./symbols')
const { readWorkspaceEvents } = require('./workspace-watch')
const { appendSessionEvent } = require('./session')
const { discoverSkills, rankSkills, readSkillByName } = require('./skills')

function parseArgs(argv) {
  const [command, ...rest] = argv
  const options = {}
  const positionals = []

  for (let index = 0; index < rest.length; index += 1) {
    const item = rest[index]
    if (!item.startsWith('--')) {
      positionals.push(item)
      continue
    }

    const [key, inlineValue] = item.slice(2).split('=', 2)
    if (inlineValue !== undefined) {
      options[key] = inlineValue
      continue
    }

    const next = rest[index + 1]
    if (next && !next.startsWith('--')) {
      options[key] = next
      index += 1
    } else {
      options[key] = true
    }
  }

  return { command, options, positionals }
}

function print(value, json = false) {
  if (json || typeof value !== 'string') {
    process.stdout.write(`${JSON.stringify(value, null, 2)}\n`)
    return
  }
  process.stdout.write(`${value}\n`)
}

async function readStdin() {
  if (process.stdin.isTTY) throw new Error('No content to write: pass --from FILE or pipe content on stdin')
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  return Buffer.concat(chunks).toString('utf8')
}

function usage() {
  return `DevCodex - agent-oriented workspace companion

Usage:
  devcodex tree [--depth N] [--json]
  devcodex search <query> [--regex] [--case-sensitive] [--json]
  devcodex search-context <query> [--matches N] [--context N] [--json]
  devcodex read <path> [--offset N] [--limit N] [--json]
  devcodex read-many <path...> [--limit N] [--json]
  devcodex write <path> [--from FILE] [--json]     (stdin when --from is absent)
  devcodex edit <path> --old <text> --new <text> [--all] [--json]
  devcodex run <command...> [--timeout-ms N] [--json]
  devcodex inspect <path> [--offset N] [--limit N] [--json]
  devcodex instructions <path> [--json]
  devcodex changes [--patch] [--json]
  devcodex diff-file <path> [--json]
  devcodex context <task...> [--json]
  devcodex bootstrap [task...] [--session ID] [--resume-latest] [--json]
  devcodex repo-map [--depth N] [--json]
  devcodex review [--json]
  devcodex review-target <uncommittedChanges|baseBranch|commit> [ref] [--detached] [--json]
  devcodex skills [task...] [--json]
  devcodex skill-read <name> [--json]
  devcodex doctor [--json]
  devcodex verify [--stop-on-failure] [--json]
  devcodex checkpoint <name> [--verify] [--json]
  devcodex checkpoint-check <name> [--json]
  devcodex session-note <id> <type> <message...> [--json]
  devcodex permission <action> [--profile NAME] [--json]
  devcodex journal [--type EVENT] [--limit N] [--json]
  devcodex result-page <handle> [--offset N] [--limit N] [--json]
  devcodex index-status [--json]
  devcodex index-refresh [--json]
  devcodex hook-run <beforeTask|afterTask|beforeVerify|afterVerify|beforeCompletion|afterCompletion> [--json]
  devcodex nav-symbols <query> [--limit N] [--json]
  devcodex nav-outline <path> [--json]
  devcodex nav-references <symbol> [--limit N] [--json]
  devcodex workspace-events [--since N] [--limit N] [--json]
  devcodex metrics [--limit N] [--json]
  devcodex complete [--checkpoint NAME] [--session ID] [--patch] [--stop-on-failure] [--json]
  devcodex daemon-serve [--root PATH] [--profile core|extended]
  devcodex daemon-status [--root PATH]
  devcodex mcp [--root PATH] [--profile core|extended] [--result-budget N] [--daemon]

Configuration:
  .devcodex.json defines quality gates, skill roots, and permission profiles.`
}

async function main(argv = process.argv.slice(2), cwd = process.cwd()) {
  const { command, options, positionals } = parseArgs(argv)
  const json = options.json === true
  const root = path.resolve(options.root || cwd)

  switch (command) {
    case undefined:
    case 'help':
    case '--help':
      print(usage())
      return 0
    case 'tree': {
      const maxDepth = options.depth ? Number.parseInt(options.depth, 10) : 4
      const tree = await buildTree(root, { maxDepth })
      print(json ? tree : tree.join('\n'), json)
      return 0
    }
    case 'search': {
      const query = positionals.join(' ')
      await refreshWorkspaceIndex(root)
      const results = await searchWorkspaceIndex(root, query, {
        regex: options.regex === true,
        caseSensitive: options['case-sensitive'] === true
      })
      if (json) print(results, true)
      else print(results.map((item) => `${item.path}:${item.line}: ${item.text}`).join('\n'))
      return 0
    }
    case 'search-context': {
      print(await searchContext(root, positionals.join(' '), {
        regex: options.regex === true,
        caseSensitive: options['case-sensitive'] === true,
        maxMatches: options.matches ? Number.parseInt(options.matches, 10) : 8,
        contextLines: options.context ? Number.parseInt(options.context, 10) : 3
      }), true)
      return 0
    }
    case 'read': {
      const filePath = positionals[0]
      const result = await readWorkspaceFile(root, filePath, {
        offset: options.offset ? Number.parseInt(options.offset, 10) : 1,
        limit: options.limit ? Number.parseInt(options.limit, 10) : 200
      })
      print(json ? result : result.content, json)
      return 0
    }
    case 'read-many': {
      const limit = options.limit ? Number.parseInt(options.limit, 10) : 200
      print(await readMany(root, positionals.map((filePath) => ({ path: filePath, limit }))), true)
      return 0
    }
    case 'inspect': {
      print(await inspectFile(root, positionals[0], {
        offset: options.offset ? Number.parseInt(options.offset, 10) : 1,
        limit: options.limit ? Number.parseInt(options.limit, 10) : 240
      }), true)
      return 0
    }
    case 'instructions': {
      const result = await findInstructionsForPath(root, positionals[0])
      if (json) print(result, true)
      else print(result.map((item) => `--- ${item.path} ---\n${item.content}`).join('\n\n'))
      return 0
    }
    case 'changes': {
      const changes = await getChangeSummary(root, { includePatch: options.patch === true })
      if (json) print(changes, true)
      else {
        const lines = [
          `${changes.branch}@${changes.head.slice(0, 12)} ${changes.clean ? 'clean' : 'dirty'}`,
          `fingerprint ${changes.fingerprint}`,
          changes.stat || '(no tracked diff)',
          ...changes.files.map((file) => `${file.status} ${file.path}`)
        ]
        if (options.patch === true && changes.patch) lines.push('', changes.patch)
        print(lines.join('\n'))
      }
      return 0
    }
    case 'diff-file': {
      print(await getFileDiff(root, positionals[0]), true)
      return 0
    }
    case 'context': {
      const task = positionals.join(' ')
      const packet = await buildContextPacket(root, task)
      print(packet, true)
      return 0
    }
    case 'bootstrap': {
      const task = positionals.join(' ')
      print(await buildTaskBootstrap(root, task || undefined, {
        sessionId: typeof options.session === 'string' ? options.session : undefined,
        resumeLatest: options['resume-latest'] === true
      }), true)
      return 0
    }
    case 'repo-map': {
      const maxDepth = options.depth ? Number.parseInt(options.depth, 10) : 8
      const result = await buildRepoMap(root, { maxDepth })
      print(result, true)
      return 0
    }
    case 'review': {
      const result = await reviewWorkspace(root)
      print(result, true)
      return result.ok ? 0 : 2
    }
    case 'review-target': {
      const [type, ref] = positionals
      const target = type === 'baseBranch'
        ? { type, branch: ref }
        : type === 'commit'
          ? { type, sha: ref }
          : { type }
      const result = options.detached === true
        ? await createDetachedReview(root, target)
        : await reviewTarget(root, target)
      print(result, true)
      return result.review ? (result.review.ok ? 0 : 2) : (result.ok ? 0 : 2)
    }
    case 'skills': {
      const config = await loadConfig(root)
      const skills = await discoverSkills(config.skillRoots)
      const task = positionals.join(' ')
      const result = task ? rankSkills(skills, task) : skills
      print(json ? result : result.map((skill) => `${skill.score === undefined ? '' : `[${skill.score}] `}${skill.name} - ${skill.description}`).join('\n'), json)
      return 0
    }
    case 'skill-read': {
      const config = await loadConfig(root)
      const result = await readSkillByName(config.skillRoots, positionals[0])
      print(json ? result : result.content, json)
      return 0
    }
    case 'doctor': {
      print(await buildEnvironmentSnapshot(root), true)
      return 0
    }
    case 'write': {
      const target = positionals[0]
      if (!target) throw new Error('write requires a workspace-relative path')
      const content = typeof options.from === 'string' ? await readFile(path.resolve(options.from), 'utf8') : await readStdin()
      print(await writeWorkspaceFile(root, target, content), true)
      return 0
    }
    case 'edit': {
      const target = positionals[0]
      if (!target) throw new Error('edit requires a workspace-relative path')
      if (typeof options.old !== 'string' || typeof options.new !== 'string') throw new Error('edit requires --old and --new')
      print(await editWorkspaceFile(root, target, options.old, options.new, { replaceAll: options.all === true }), true)
      return 0
    }
    case 'run': {
      const timeoutMs = typeof options['timeout-ms'] === 'string' ? Number.parseInt(options['timeout-ms'], 10) : undefined
      const result = await runShellCommand(root, positionals.join(' '), { timeoutMs })
      print(result, true)
      return result.ok ? 0 : 1
    }
    case 'verify': {
      const config = await loadConfig(root)
      if (config.gates.length === 0) {
        print({ ok: true, reason: 'no-quality-gates-configured', results: [] }, true)
        return 0
      }
      const report = await runQualityGates(root, config.gates, { stopOnFailure: options['stop-on-failure'] === true })
      print(report, true)
      return report.ok ? 0 : 1
    }
    case 'checkpoint': {
      const name = positionals[0]
      let verification = null
      if (options.verify === true) {
        const config = await loadConfig(root)
        verification = config.gates.length > 0
          ? await runQualityGates(root, config.gates)
          : { ok: true, reason: 'no-quality-gates-configured', results: [] }
        if (!verification.ok) {
          print({ created: false, reason: 'quality-gates-failed', verification }, true)
          return 1
        }
      }
      const checkpoint = await createCheckpoint(root, name, verification)
      print(checkpoint, true)
      return 0
    }
    case 'checkpoint-check': {
      const result = await verifyCheckpoint(root, positionals[0])
      print(result, true)
      return result.matches ? 0 : 2
    }
    case 'session-note': {
      const [id, type, ...messageParts] = positionals
      const result = await appendSessionEvent(root, id, { type, message: messageParts.join(' ') })
      print(result, true)
      return 0
    }
    case 'permission': {
      print(await checkPermission(root, positionals[0], typeof options.profile === 'string' ? options.profile : undefined), true)
      return 0
    }
    case 'journal': {
      print(await readJournal(root, {
        type: typeof options.type === 'string' ? options.type : undefined,
        limit: typeof options.limit === 'string' ? Number.parseInt(options.limit, 10) : 100
      }), true)
      return 0
    }
    case 'result-page': {
      print(await readResultPage(root, positionals[0], {
        offset: options.offset ? Number.parseInt(options.offset, 10) : 0,
        limit: options.limit ? Number.parseInt(options.limit, 10) : 8000
      }), true)
      return 0
    }
    case 'index-status': {
      print(await getIndexStatus(root), true)
      return 0
    }
    case 'index-refresh': {
      print(await refreshWorkspaceIndex(root), true)
      return 0
    }
    case 'hook-run': {
      print(await runHooks(root, positionals[0]), true)
      return 0
    }
    case 'nav-symbols': {
      await buildSymbolIndex(root)
      print(await searchSymbols(root, positionals.join(' '), {
        limit: typeof options.limit === 'string' ? Number.parseInt(options.limit, 10) : 100
      }), true)
      return 0
    }
    case 'nav-outline': {
      await buildSymbolIndex(root)
      print(await outlineFile(root, positionals[0]), true)
      return 0
    }
    case 'nav-references': {
      await buildSymbolIndex(root)
      print(await findReferences(root, positionals[0], {
        limit: typeof options.limit === 'string' ? Number.parseInt(options.limit, 10) : 200
      }), true)
      return 0
    }
    case 'workspace-events': {
      print(await readWorkspaceEvents(root, {
        sinceSeq: typeof options.since === 'string' ? Number.parseInt(options.since, 10) : undefined,
        limit: typeof options.limit === 'string' ? Number.parseInt(options.limit, 10) : 100
      }), true)
      return 0
    }
    case 'metrics': {
      print(await buildRuntimeMetrics(root, {
        limit: typeof options.limit === 'string' ? Number.parseInt(options.limit, 10) : 10000
      }), true)
      return 0
    }
    case 'complete': {
      const result = await runCompletionGate(root, {
        checkpointName: typeof options.checkpoint === 'string' ? options.checkpoint : undefined,
        sessionId: typeof options.session === 'string' ? options.session : undefined,
        includePatch: options.patch === true,
        stopOnFailure: options['stop-on-failure'] === true
      })
      print(result, true)
      return result.ok ? 0 : 1
    }
    case 'daemon-status': {
      print(await getDaemonStatus(root), true)
      return 0
    }
    case 'daemon-serve': {
      const daemon = await startDaemonServer(root, {
        profile: typeof options.profile === 'string' ? options.profile : undefined
      })
      print({ status: 'running', host: daemon.host, port: daemon.port, profile: daemon.profile, pid: daemon.pid }, true)
      await new Promise((resolve) => {
        let closing = false
        const close = async () => {
          if (closing) return
          closing = true
          await stopDaemonServer(root, daemon)
          resolve()
        }
        process.once('SIGINT', close)
        process.once('SIGTERM', close)
      })
      return 0
    }
    case 'mcp':
      if (options.daemon === true) {
        await serveDaemonProxyStdio(root)
      } else {
        await serveStdio(root, {
          profile: typeof options.profile === 'string' ? options.profile : undefined,
          resultBudgetChars: typeof options['result-budget'] === 'string' ? Number.parseInt(options['result-budget'], 10) : undefined
        })
      }
      return 0
    default:
      throw new Error(`Unknown command: ${command}\n\n${usage()}`)
  }
}

if (require.main === module) {
  main().then((code) => {
    process.exitCode = code
  }).catch((error) => {
    process.stderr.write(`devcodex: ${error.message}\n`)
    process.exitCode = 1
  })
}

module.exports = {
  main,
  parseArgs,
  usage
}
