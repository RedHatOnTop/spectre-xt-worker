import { readFileSync, readdirSync, readlinkSync } from "node:fs";
import { basename, join } from "node:path";

function option(argv, names) {
  for (let i = 0; i < argv.length; i += 1) {
    // A wrapper such as `claude bg-pty-host ... -- /usr/bin/claude --model X`
    // carries its child's argv; counting it would double the planner.
    if (argv[i] === "--") return null;
    if (names.includes(argv[i])) return argv[i + 1];
    for (const name of names) if (argv[i].startsWith(`${name}=`)) return argv[i].slice(name.length + 1);
  }
  return null;
}

export function processRole(argv) {
  let exe = basename(argv[0] || "");
  if (/^(node|python)/.test(exe)) exe = basename(argv[1] || "");
  const model = option(argv, ["-m", "--model"]);
  if (["codex", "codex-cli", "codex.js"].includes(exe) && model === "gpt-6-astra") return "astra";
  if (exe === "claude" && model === "claude-opus-5-5") return "claude";
  if (exe === "kimi" && model === "cline/kimi-k3") return "kimi";
  if (["qodercli", "qoder", "qoder-efficient"].includes(exe) && String(model).toLowerCase() === "efficient") return "efficient";
  if (exe === "dsh-clinepass" || (exe === "dsh" && ["headless", "tui", "minimal"].includes(option(argv, ["--profile"])))) return "flash";
  if (["mimo-clinepass", "mimo", "mimocode"].includes(exe)) return "mimo";
  if (["bash", "sh", "zsh", "fish", "dash"].includes(exe)) return "shell";
  return null;
}

export function processes(root = process.env.SPECTRE_PROC_ROOT || "/proc") {
  const rows = readdirSync(root).filter((name) => /^\d+$/.test(name)).flatMap((pid) => {
    try {
      const path = join(root, pid);
      const argv = readFileSync(join(path, "cmdline"), "utf8").split("\0").filter(Boolean);
      const stat = readFileSync(join(path, "stat"), "utf8").split(")").slice(1).join(")").trim().split(/\s+/);
      const env = readFileSync(join(path, "environ"), "utf8").split("\0");
      const handle = env.find((value) => value.startsWith("ORCA_TERMINAL_HANDLE="))?.split("=")[1] || "";
      return [{ pid, ppid: stat[1], argv, handle, cwd: readlinkSync(join(path, "cwd")), role: processRole(argv) }];
    } catch { return []; }
  });
  const byPid = new Map(rows.map((row) => [row.pid, row]));
  return rows.map((row) => {
    let ancestor = row;
    const seen = new Set();
    while (ancestor && !ancestor.handle && !seen.has(ancestor.pid)) {
      seen.add(ancestor.pid);
      ancestor = byPid.get(ancestor.ppid);
    }
    return { ...row, handle: ancestor?.handle || "" };
  });
}

export function nativeProcessGuard(pin, cwd, target, rows = processes()) {
  const tree = rows.filter((row) => row.handle === pin);
  const packetTargets = ["flash", "mimo"];
  const wanted = packetTargets.includes(target) ? "shell" : target;
  const matched = tree.some((row) => row.role === wanted && row.cwd === cwd);
  const wrongAgent = tree.some((row) => ["astra", "claude", "kimi", "efficient", "flash", "mimo"].includes(row.role) && row.role !== wanted);
  return matched && !wrongAgent;
}
