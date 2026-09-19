const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { checkPermission, resolvePermissionProfile } = require('../src/permission')

test('permission profiles allow every action, named or unnamed', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-permission-'))
  try {
    const profile = await resolvePermissionProfile(root)
    assert.equal(profile.name, 'default')
    assert.equal((await checkPermission(root, 'read')).decision, 'allow')
    assert.equal((await checkPermission(root, 'shell')).decision, 'allow')
    assert.equal((await checkPermission(root, 'verify')).decision, 'allow')
    assert.equal((await checkPermission(root, 'fileWrite')).decision, 'allow')
    // Unnamed actions resolve through the same allow fallback (box posture
    // 2026-09-17): nothing parks work behind an implicit deny. The explicit
    // marker still reports whether a rule was spelled out.
    assert.equal((await checkPermission(root, 'fileDelete')).decision, 'allow')
    assert.equal((await checkPermission(root, 'fileDelete')).explicit, false)
    assert.equal((await checkPermission(root, 'futureUnknownAction')).decision, 'allow')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('project permission profiles override defaults and inherit the allow fallback', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-permission-'))
  try {
    await writeFile(path.join(root, '.devcodex.json'), JSON.stringify({
      permissions: {
        defaultProfile: 'strict',
        profiles: {
          strict: {
            shell: 'deny',
            checkpoint: 'ask'
          }
        }
      }
    }))
    assert.equal((await checkPermission(root, 'shell')).decision, 'deny')
    assert.equal((await checkPermission(root, 'checkpoint')).decision, 'ask')
    assert.equal((await checkPermission(root, 'fileDelete')).decision, 'allow')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
