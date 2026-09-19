const { loadConfig } = require('./config')

const DECISIONS = Object.freeze(['allow', 'ask', 'deny'])
// Box posture (2026-09-16, extended 2026-09-17 — user decisions): every action
// resolves to allow. Named actions keep an explicit rule; unnamed actions fall
// to the same allow through the resolver, so no future tool parks work behind a
// deny on this box. The destructive-command barrier is the agent guard hook
// beside this runtime, not devcodex.
const DEFAULT_RULES = Object.freeze({
  read: 'allow',
  search: 'allow',
  review: 'allow',
  verify: 'allow',
  checkpoint: 'allow',
  sessionWrite: 'allow',
  environment: 'allow',
  shell: 'allow',
  fileWrite: 'allow'
})

function validateDecision(value, action) {
  if (!DECISIONS.includes(value)) throw new Error(`Invalid permission decision for ${action}: ${value}`)
  return value
}

async function resolvePermissionProfile(root, requestedName) {
  const config = await loadConfig(root)
  const permissionConfig = config.permissions || {}
  const profiles = permissionConfig.profiles && typeof permissionConfig.profiles === 'object'
    ? permissionConfig.profiles
    : {}
  const name = requestedName || permissionConfig.defaultProfile || 'default'
  const configured = name === 'default' ? {} : profiles[name]
  if (name !== 'default' && (!configured || typeof configured !== 'object')) {
    throw new Error(`Permission profile not found: ${name}`)
  }
  const overrides = configured || {}
  const rules = Object.entries(overrides).reduce((result, [action, decision]) => ({
    ...result,
    [action]: validateDecision(decision, action)
  }), { ...DEFAULT_RULES })
  return { name, rules }
}

async function checkPermission(root, action, profileName) {
  if (typeof action !== 'string' || action.trim() === '') throw new Error('Permission action must be non-empty')
  const profile = await resolvePermissionProfile(root, profileName)
  const normalizedAction = action.trim()
  return {
    profile: profile.name,
    action: normalizedAction,
    decision: profile.rules[normalizedAction] || 'allow',
    explicit: Object.prototype.hasOwnProperty.call(profile.rules, normalizedAction)
  }
}

module.exports = {
  DECISIONS,
  DEFAULT_RULES,
  checkPermission,
  resolvePermissionProfile,
  validateDecision
}
