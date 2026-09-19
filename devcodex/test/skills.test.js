const assert = require('node:assert/strict')
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { discoverSkills, rankSkills, readSkillByName } = require('../src/skills')

async function withSkills(run) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'devcodex-skills-'))

  try {
    const tdd = path.join(root, 'tdd-workflow')
    const browser = path.join(root, 'playwright')
    await mkdir(tdd, { recursive: true })
    await mkdir(browser, { recursive: true })
    await writeFile(path.join(tdd, 'SKILL.md'), '---\nname: tdd-workflow\ndescription: Test-driven development with coverage gates.\n---\n# TDD\n')
    await writeFile(path.join(browser, 'SKILL.md'), '---\nname: playwright\ndescription: Browser automation and UI debugging.\n---\n# Browser\n')
    return await run(root)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
}

test('discoverSkills reads Codex-style SKILL.md frontmatter', async () => {
  await withSkills(async (root) => {
    const skills = await discoverSkills([root])

    assert.equal(skills.length, 2)
    assert.equal(skills[0].name, 'playwright')
    assert.match(skills[1].description, /coverage gates/)
  })
})

test('rankSkills ranks descriptions against a task', async () => {
  await withSkills(async (root) => {
    const skills = await discoverSkills([root])
    const ranked = rankSkills(skills, 'fix this bug with test driven development and coverage')

    assert.equal(ranked[0].name, 'tdd-workflow')
    assert.ok(ranked[0].score > ranked[1].score)
  })
})

test('readSkillByName returns the exact discovered skill content', async () => {
  await withSkills(async (root) => {
    const skill = await readSkillByName([root], 'playwright')

    assert.equal(skill.name, 'playwright')
    assert.match(skill.content, /# Browser/)
    await assert.rejects(() => readSkillByName([root], '../outside'), /Skill not found/)
  })
})

test('rankSkills does not inflate generic short-token overlap', async () => {
  await withSkills(async (root) => {
    const skills = await discoverSkills([root])
    const ranked = rankSkills(skills, 'self review mcp integration verify agent use')

    assert.equal(ranked[0].score, 0)
    assert.equal(ranked[1].score, 0)
  })
})

test('rankSkills prefers domain overlap over generic agent wording', () => {
  const ranked = rankSkills([
    { name: 'generic', description: 'Use when an agent is ready to review code and work on a task.', path: '/generic' },
    { name: 'mcp-integration', description: 'MCP server integration and protocol workflows.', path: '/mcp' }
  ], 'verify MCP integration for agent use')

  assert.equal(ranked[0].name, 'mcp-integration')
  assert.ok(ranked[0].score > ranked[1].score)
})
