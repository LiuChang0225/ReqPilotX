import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { stampWebBuild } from './web-build-identity.mjs';

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'reqpilot-web-identity-'));
  const git = args => execFileSync('git', args, { cwd: root, stdio: 'pipe' });
  git(['init', '--quiet']); git(['config', 'user.name', 'Synthetic Fixture']);
  git(['config', 'user.email', 'fixture@example.invalid']);
  mkdirSync(join(root, 'apps/web/src'), { recursive: true }); mkdirSync(join(root, 'dist'));
  writeFileSync(join(root, '.gitignore'), 'dist/\n');
  writeFileSync(join(root, 'apps/web/src/App.tsx'), '// invented source\n');
  writeFileSync(join(root, 'dist/index.html'), '<p>invented output</p>');
  git(['add', '.']); git(['commit', '--quiet', '-m', 'synthetic']);
  return root;
}

test('Web build identity records exact bytes and distinguishes clean from dirty sources', () => {
  const root = fixture();
  try {
    const before = stampWebBuild(join(root, 'dist'), root);
    assert.equal(before.source_inputs_clean, true); assert.equal(before.files.length, 1);
    writeFileSync(join(root, 'apps/web/src/App.tsx'), '// changed\n');
    const after = stampWebBuild(join(root, 'dist'), root);
    assert.equal(after.source_commit, before.source_commit); assert.equal(after.source_inputs_clean, false);
    execFileSync('git', ['update-index', '--assume-unchanged', 'apps/web/src/App.tsx'], { cwd: root });
    assert.equal(stampWebBuild(join(root, 'dist'), root).source_inputs_clean, false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('Web build rejects linked output instead of declaring its payload local', () => {
  const root = fixture();
  try {
    symlinkSync(join(root, 'apps/web/src/App.tsx'), join(root, 'dist/linked.js'));
    assert.throws(() => stampWebBuild(join(root, 'dist'), root), /symlink/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
