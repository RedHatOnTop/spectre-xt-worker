const { runCommand } = require('./gates')
const { loadConfig } = require('./config')

const HOOK_NAMES = new Set([
  'beforeTask',
  'afterTask',
  'beforeVerify',
  'afterVerify',
  'beforeCompletion',
  'afterCompletion'
])

function normalizeHooks(hooks, hookName) {
  if (!HOOK_NAMES.has(hookName)) throw new Error(`Unknown hook lifecycle: ${hookName}`)
  const configured = hooks && Array.isArray(hooks[hookName]) ? hooks[hookName] : []
  return configured.map((hook, index) => {
    if (typeof hook === 'string') return { name: `${hookName}-${index + 1}`, command: hook, required: true }
    if (!hook || typeof hook !== 'object' || typeof hook.command !== 'string' || hook.command.trim() === '') {
      throw new Error(`Invalid ${hookName} hook at index ${index}`)
    }
    return {
      name: typeof hook.name === 'string' && hook.name.trim() ? hook.name.trim() : `${hookName}-${index + 1}`,
      command: hook.command,
      required: hook.required !== false,
      timeoutMs: hook.timeoutMs,
      env: hook.env
    }
  })
}

async function runHooks(root, hookName) {
  const config = await loadConfig(root)
  return await runHookSet(root, config.hooks, hookName)
}

async function runHookSet(root, hooksConfig, hookName) {
  const hooks = normalizeHooks(hooksConfig, hookName)
  const results = []

  for (const hook of hooks) {
    const result = await runCommand(root, hook)
    results.push(result)
    if (result.required && !result.ok) break
  }

  return {
    hook: hookName,
    ok: results.every((result) => !result.required || result.ok),
    results
  }
}

module.exports = {
  HOOK_NAMES,
  normalizeHooks,
  runHookSet,
  runHooks
}
