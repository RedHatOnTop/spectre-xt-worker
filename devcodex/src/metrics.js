const { readJournal } = require('./journal')

function percentile(values, fraction) {
  if (values.length === 0) return null
  const sorted = [...values].sort((left, right) => left - right)
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(sorted.length * fraction) - 1))
  return sorted[index]
}

function round(value, digits = 2) {
  const factor = 10 ** digits
  return Math.round(value * factor) / factor
}

async function buildRuntimeMetrics(root, options = {}) {
  const events = await readJournal(root, { limit: options.limit || 10000 })
  const completed = events.filter((event) => event.type === 'tool.completed')
  const failed = events.filter((event) => event.type === 'tool.failed')
  const blocked = events.filter((event) => event.type === 'tool.blocked')
  const tools = {}

  for (const event of completed) {
    const current = tools[event.tool] || { calls: 0, durations: [], resultChars: 0, returnedChars: 0, truncatedCalls: 0 }
    current.calls += 1
    if (Number.isFinite(event.durationMs)) current.durations.push(event.durationMs)
    current.resultChars += Number.isFinite(event.resultChars) ? event.resultChars : 0
    current.returnedChars += Number.isFinite(event.returnedChars) ? event.returnedChars : 0
    if (event.truncated === true) current.truncatedCalls += 1
    tools[event.tool] = current
  }

  const summarizedTools = Object.fromEntries(Object.entries(tools)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, value]) => {
      const durationTotal = value.durations.reduce((sum, duration) => sum + duration, 0)
      return [name, {
        calls: value.calls,
        avgMs: value.durations.length ? round(durationTotal / value.durations.length) : null,
        p50Ms: percentile(value.durations, 0.5),
        p95Ms: percentile(value.durations, 0.95),
        maxMs: value.durations.length ? Math.max(...value.durations) : null,
        resultChars: value.resultChars,
        returnedChars: value.returnedChars,
        savedChars: Math.max(0, value.resultChars - value.returnedChars),
        truncatedCalls: value.truncatedCalls
      }]
    }))

  const totalResultChars = completed.reduce((sum, event) => sum + (Number.isFinite(event.resultChars) ? event.resultChars : 0), 0)
  const totalReturnedChars = completed.reduce((sum, event) => sum + (Number.isFinite(event.returnedChars) ? event.returnedChars : 0), 0)

  return {
    sampledEvents: events.length,
    completedCalls: completed.length,
    failedCalls: failed.length,
    blockedCalls: blocked.length,
    output: {
      resultChars: totalResultChars,
      returnedChars: totalReturnedChars,
      savedChars: Math.max(0, totalResultChars - totalReturnedChars),
      truncatedCalls: completed.filter((event) => event.truncated === true).length
    },
    tools: summarizedTools
  }
}

module.exports = {
  buildRuntimeMetrics,
  percentile
}
