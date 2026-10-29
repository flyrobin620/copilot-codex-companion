'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const root = path.join(__dirname, '..');
const pkg = require('../package.json');
const manager = fs.readFileSync(path.join(root, 'manage.py'), 'utf8');
const version = manager.match(/^VERSION = '([^']+)'/m)?.[1];
if (pkg.version !== version) throw new Error('package.json and manage.py versions must match.');
const runtime = [...manager.match(/^FILES = \((.+)\)$/m)[1].matchAll(/'([^']+)'/g)].map(match => match[1]);
for (const name of runtime) {
  if (!pkg.files.includes(name)) throw new Error(`Missing runtime file in npm allowlist: ${name}`);
}
const files = [...new Set(['package.json', ...pkg.files])].filter(name => name !== 'SHA256SUMS.txt').sort();
const sums = files.map(name => {
  // Keep the published package limited to individual, explicitly selected files.
  if (name.includes('..') || /[*!?]/.test(name) || path.isAbsolute(name)) {
    throw new Error(`Package files must be explicit relative paths: ${name}`);
  }
  const bytes = fs.readFileSync(path.join(root, name));
  return `${createHash('sha256').update(bytes).digest('hex')}  ${name}\n`;
}).join('');
fs.writeFileSync(path.join(root, 'SHA256SUMS.txt'), sums);
console.log(`Verified runtime completeness and generated checksums for ${files.length} package files.`);
