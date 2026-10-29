#!/usr/bin/env node
'use strict';

const { spawnSync } = require('node:child_process');
const path = require('node:path');
const { version } = require('../package.json');

const HELP = `copilot-codex-companion ${version}

Usage: copilot-codex-companion <command> [options]

Commands:
  install    Install into LOCALAPPDATA\\CodexCopilotCompat
  update     Update an existing installation and restart its bridge
  restart    Restart the installed bridge
  check      Verify configuration, authentication and the model catalog
  uninstall  Restore the original connection and stop the bridge

Options:
  --dest PATH          Existing/custom installation directory
  --hostname HOST      Persist the authentication host (install/update)
  --force-config       Rebuild the connection using gh auth token (install)
  --config PATH        Codex config.toml (install)
  --port PORT          Local port (install)
  --codex PATH         Matching Codex executable (install)
  --automatic-context Remove manual context limits (install)
  --live               Send a synthetic test request (check)
  --help               Show help
  --version            Show package version

Requires Windows and Python 3.11+ with pythonw.exe, plus an existing
working Codex/Copilot Enterprise connection. Python is not bundled.
The runtime is copied into --dest; the npm cache is not used as a service.
`;

const PROBE = [
  'import json, pathlib, sys',
  "ok = sys.version_info >= (3, 11) and pathlib.Path(sys.executable).with_name('pythonw.exe').is_file()",
  'print(json.dumps(sys.executable) if ok else "")',
  'sys.exit(0 if ok else 1)',
].join('; ');

function findPython(spawn = spawnSync) {
  for (const [command, prefix] of [['py', ['-3']], ['python', []], ['python3', []]]) {
    const result = spawn(command, [...prefix, '-c', PROBE], {
      encoding: 'utf8', windowsHide: true, shell: false, timeout: 10000,
    });
    if (result.error || result.status !== 0) continue;
    try {
      const executable = JSON.parse(result.stdout.trim());
      if (typeof executable === 'string' && path.isAbsolute(executable)) return executable;
    } catch { /* Skip launchers that did not return a usable interpreter. */ }
  }
  throw new Error('Python 3.11+ with pythonw.exe was not found. Install standard Windows Python and make py or python available on PATH.');
}

function main(args = process.argv.slice(2), options = {}) {
  const spawn = options.spawn || spawnSync;
  const stdout = options.stdout || process.stdout;
  const stderr = options.stderr || process.stderr;
  const platform = options.platform || process.platform;
  if (!args.length || args[0] === '--help' || args[0] === '-h') {
    stdout.write(HELP);
    return 0;
  }
  if (args[0] === '--version' || args[0] === '-v') {
    stdout.write(version + '\n');
    return 0;
  }
  if (!['install', 'update', 'restart', 'check', 'uninstall'].includes(args[0])) {
    stderr.write('Unknown command. Run copilot-codex-companion --help.\n');
    return 2;
  }
  if (platform !== 'win32') {
    stderr.write('This package supports Windows only.\n');
    return 1;
  }
  try {
    const python = findPython(spawn);
    const result = spawn(python, [path.join(__dirname, '..', 'manage.py'), ...args], {
      stdio: 'inherit', windowsHide: true, shell: false,
    });
    if (result.error) throw new Error('Could not launch the Python management command.');
    if (result.signal) throw new Error('The management command was interrupted.');
    return result.status ?? 1;
  } catch (error) {
    stderr.write(`Copilot compatibility: ${error.message}\n`);
    return 1;
  }
}

if (require.main === module) process.exitCode = main();
module.exports = { main, findPython };
