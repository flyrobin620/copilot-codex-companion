'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { main, findPython } = require('../bin/cli.cjs');
const python = path.resolve('test interpreter', 'python.exe');
const goodProbe = () => ({ status: 0, stdout: JSON.stringify(python) + '\n' });

function output() {
  return { text: '', write(value) { this.text += value; } };
}

test('help and version work without Python or installation state', () => {
  const stdout = output();
  const spawn = () => assert.fail('help must not spawn any process');
  assert.equal(main(['--help'], { stdout, spawn }), 0);
  assert.match(stdout.text, /--hostname/);
  assert.equal(main(['--version'], { stdout, spawn }), 0);
  assert.ok(stdout.text.endsWith(require('../package.json').version + '\n'));
});

test('Python discovery falls back past missing or unsuitable interpreters', () => {
  const calls = [];
  const spawn = (command, args, options) => {
    calls.push(command);
    assert.equal(options.shell, false);
    assert.equal(options.windowsHide, true);
    assert.equal(options.timeout, 10000);
    assert.match(args.at(-1), /pythonw\.exe/);
    if (command === 'py') return { error: new Error('not found') };
    if (command === 'python') return { status: 1, stdout: '' };
    return goodProbe();
  };
  assert.equal(findPython(spawn), python);
  assert.deepEqual(calls, ['py', 'python', 'python3']);
});

test('missing Python produces an actionable error without running a command', () => {
  const stderr = output();
  assert.equal(main(['update'], { platform: 'win32', stderr, spawn: () => ({ status: 1 }) }), 1);
  assert.match(stderr.text, /Python 3\.11\+ with pythonw\.exe/);
});

test('rejects malformed probe output instead of treating it as a command', () => {
  assert.throws(() => findPython(() => ({ status: 0, stdout: '"relative-python.exe"' })), /not found/);
});

test('forwards every command and arguments literally, preserving its exit code', () => {
  for (const action of ['install', 'update', 'restart', 'check', 'uninstall']) {
    const args = [action, '--dest', 'D:\\Tools\\桥 & bridge', '--hostname', 'custom.example.test'];
    let launched = false;
    const spawn = (command, passed, options) => {
      if (command === 'py') return goodProbe();
      launched = true;
      assert.equal(command, python);
      assert.equal(passed[0], path.resolve(__dirname, '..', 'manage.py'));
      assert.deepEqual(passed.slice(1), args);
      assert.equal(options.shell, false);
      assert.equal(options.stdio, 'inherit');
      assert.equal(options.windowsHide, true);
      assert.equal(options.timeout, undefined);
      return { status: 7 };
    };
    assert.equal(main(args, { spawn, platform: 'win32' }), 7);
    assert.equal(launched, true);
  }
});

test('rejects unsupported platforms and unknown commands before launching', () => {
  const stderr = output();
  const spawn = () => assert.fail('must not launch');
  assert.equal(main(['install'], { platform: 'linux', stderr, spawn }), 1);
  assert.equal(main(['unknown'], { platform: 'win32', stderr, spawn }), 2);
});

test('reports launch failures and interrupted children as failures', () => {
  for (const result of [{ error: new Error('launch failed') }, { signal: 'SIGTERM' }, { status: null }]) {
    const stderr = output();
    const spawn = command => command === 'py' ? goodProbe() : result;
    assert.equal(main(['restart'], { spawn, platform: 'win32', stderr }), 1);
  }
});
