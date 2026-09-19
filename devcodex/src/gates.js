const { spawn } = require('node:child_process')

function runCommand(root, gate) {
  return new Promise((resolve) => {
    const startedAt = new Date().toISOString()
    const startedMs = Date.now()
    const required = gate.required !== false
    const child = spawn('/bin/sh', ['-lc', gate.command], {
      cwd: root,
      env: { ...process.env, ...(gate.env || {}) },
      stdio: ['ignore', 'pipe', 'pipe']
    })

    let stdout = ''
    let stderr = ''
    let timedOut = false

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString()
    })
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString()
    })

    const timeout = gate.timeoutMs
      ? setTimeout(() => {
        timedOut = true
        child.kill('SIGTERM')
      }, gate.timeoutMs)
      : null

    child.on('error', (error) => {
      if (timeout) clearTimeout(timeout)
      resolve({
        name: gate.name,
        command: gate.command,
        required,
        ok: false,
        exitCode: null,
        signal: null,
        timedOut,
        stdout: stdout.trimEnd(),
        stderr: `${stderr}${stderr ? '\n' : ''}${error.message}`.trimEnd(),
        startedAt,
        durationMs: Date.now() - startedMs
      })
    })

    child.on('close', (code, signal) => {
      if (timeout) clearTimeout(timeout)
      resolve({
        name: gate.name,
        command: gate.command,
        required,
        ok: code === 0 && !timedOut,
        exitCode: code,
        signal,
        timedOut,
        stdout: stdout.trimEnd(),
        stderr: stderr.trimEnd(),
        startedAt,
        durationMs: Date.now() - startedMs
      })
    })
  })
}

async function runQualityGates(root, gates, options = {}) {
  const normalized = (gates || []).map((gate, index) => ({
    name: gate.name || `gate-${index + 1}`,
    command: gate.command,
    required: gate.required !== false,
    timeoutMs: gate.timeoutMs,
    env: gate.env
  }))

  for (const gate of normalized) {
    if (!gate.command || typeof gate.command !== 'string') {
      throw new Error(`Quality gate ${gate.name} is missing a command`)
    }
  }

  const results = []
  for (const gate of normalized) {
    const result = await runCommand(root, gate)
    results.push(result)
    if (options.stopOnFailure && result.required && !result.ok) break
  }

  return {
    ok: results.every((result) => !result.required || result.ok),
    results
  }
}

module.exports = {
  runCommand,
  runQualityGates
}
