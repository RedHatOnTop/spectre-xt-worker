const { readdir, readFile } = require('node:fs/promises')
const path = require('node:path')

const { tokenize } = require('./search')

const SKILL_STOPWORDS = new Set([
  'agent', 'and', 'are', 'code', 'coding', 'for', 'from', 'into', 'is', 'it',
  'project', 'task', 'the', 'this', 'use', 'using', 'when', 'with', 'work'
])

const SKILL_DESCRIPTION_STOPWORDS = new Set([
  ...SKILL_STOPWORDS,
  'codex', 'ready', 'review', 'self', 'verify'
])

function skillTokens(value) {
  const normalized = String(value || '').replace(/[-_./]+/g, ' ')
  return tokenize(normalized).filter((token) => token.length >= 3 && !SKILL_STOPWORDS.has(token))
}

function tokenMatches(candidate, token) {
  if (candidate === token) return true
  if (token.length < 4 || candidate.length < 4) return false
  return candidate.startsWith(token) || token.startsWith(candidate)
}

function parseFrontmatter(content) {
  const lines = content.split(/\r?\n/)
  if (lines[0] !== '---') return {}

  const end = lines.indexOf('---', 1)
  if (end === -1) return {}

  return lines.slice(1, end).reduce((result, line) => {
    const separator = line.indexOf(':')
    if (separator === -1) return result

    const key = line.slice(0, separator).trim()
    const value = line.slice(separator + 1).trim().replace(/^['"]|['"]$/g, '')
    return key ? { ...result, [key]: value } : result
  }, {})
}

async function discoverSkills(roots) {
  const discovered = await Promise.all((roots || []).map(async (root) => {
    let entries
    try {
      entries = await readdir(root, { withFileTypes: true })
    } catch (error) {
      if (error.code === 'ENOENT') return []
      throw error
    }

    const directories = entries.filter((entry) => entry.isDirectory())
    const skills = await Promise.all(directories.map(async (entry) => {
      const skillPath = path.join(root, entry.name, 'SKILL.md')
      try {
        const content = await readFile(skillPath, 'utf8')
        const metadata = parseFrontmatter(content)
        return {
          name: metadata.name || entry.name,
          description: metadata.description || '',
          path: skillPath
        }
      } catch (error) {
        if (error.code === 'ENOENT') return null
        throw error
      }
    }))

    return skills.filter(Boolean)
  }))

  return discovered.flat().sort((left, right) => left.name.localeCompare(right.name))
}

async function readSkillByName(roots, name) {
  const normalized = String(name || '').trim()
  if (!normalized) throw new Error('Skill name must be non-empty')

  const skills = await discoverSkills(roots)
  const skill = skills.find((candidate) => candidate.name === normalized)
  if (!skill) throw new Error(`Skill not found: ${normalized}`)

  return {
    ...skill,
    content: await readFile(skill.path, 'utf8')
  }
}

function rankSkills(skills, task) {
  const taskTokens = skillTokens(task)

  return skills
    .map((skill) => {
      const nameTokens = skillTokens(skill.name)
      const descriptionTokens = skillTokens(skill.description)
      const score = taskTokens.reduce((total, token) => {
        const nameScore = nameTokens.some((candidate) => tokenMatches(candidate, token)) ? 3 : 0
        const descriptionScore = !SKILL_DESCRIPTION_STOPWORDS.has(token) && descriptionTokens.some((candidate) => tokenMatches(candidate, token)) ? 1 : 0
        return total + nameScore + descriptionScore
      }, 0)
      return { ...skill, score }
    })
    .sort((left, right) => right.score - left.score || left.name.localeCompare(right.name))
}

module.exports = {
  SKILL_DESCRIPTION_STOPWORDS,
  SKILL_STOPWORDS,
  discoverSkills,
  parseFrontmatter,
  rankSkills,
  readSkillByName,
  skillTokens,
  tokenMatches
}
