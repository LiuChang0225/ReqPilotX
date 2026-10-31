import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const sha = value => createHash('sha256').update(value).digest('hex');
const canonical = value => JSON.stringify(value, (_key, entry) => entry && typeof entry === 'object' && !Array.isArray(entry)
  ? Object.fromEntries(Object.entries(entry).sort(([a], [b]) => a.localeCompare(b))) : entry);
const helper = fileURLToPath(new URL('../host/src/task-workflow.py', import.meta.url));

test('workflow reader authenticates pinned bytes before importing substituted workspace code', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-workflow-guard-'));
  try {
    const workspace = join(root, 'workspace'), fixed = join(workspace, '.reqpilot');
    await mkdir(join(fixed, 'scripts'), { recursive: true });
    const original = 'def load_runtime():\n    return None\n';
    const requestValue = { task_id: 'task-' + 'a'.repeat(32),
      source_sha256: 'b'.repeat(64), workflow: { id: 'reqpilot-fixed-workflow', version: '1.0.0',
        definition_sha256: 'c'.repeat(64), brief_sha256: 'd'.repeat(64) } };
    const resultValue = { status: 'READY', task_id: requestValue.task_id,
      source: { sha256: requestValue.source_sha256 }, workflow: { workflow: requestValue.workflow } };
    const service = `import json\nclass TaskService:\n    def __init__(self, *args):\n        pass\n    def invoke(self, *args):\n        return json.loads(${JSON.stringify(JSON.stringify(resultValue))})\n`;
    await writeFile(join(fixed, 'scripts/reqpilot.py'), original);
    await writeFile(join(fixed, 'scripts/task_service.py'), service);
    const receipt = { schema_version: 'reqpilot-portable-build/v1',
      plugin_files: [{ path: 'scripts/reqpilot.py', sha256: sha(original), bytes: Buffer.byteLength(original) },
        { path: 'scripts/task_service.py', sha256: sha(service), bytes: Buffer.byteLength(service) }],
      runtime_files: [] };
    receipt.bundle_sha256 = sha(canonical(receipt));
    await writeFile(join(fixed, 'build-receipt.json'), JSON.stringify(receipt));
    const request = join(root, 'request.json');
    await writeFile(request, JSON.stringify({ ...requestValue, bundle_sha256: receipt.bundle_sha256 }));
    const clean = spawnSync('python3', ['-I', helper, request, workspace, join(root, 'clean-cache')], { encoding: 'utf8' });
    assert.equal(clean.status, 0, clean.stdout + clean.stderr);
    assert.equal(JSON.parse(clean.stdout).status, 'READY');
    const sentinel = join(root, 'workspace-code-executed');
    await writeFile(join(fixed, 'scripts/task_service.py'),
      `from pathlib import Path\nPath(${JSON.stringify(sentinel)}).write_text('executed')\n`);
    const result = spawnSync('python3', ['-I', helper, request, workspace, join(root, 'cache')], { encoding: 'utf8' });
    assert.notEqual(result.status, 0);
    assert.match(result.stdout, /TASK_WORKFLOW_PACKAGE_MISMATCH/);
    assert.equal(existsSync(sentinel), false);
    assert.equal(existsSync(join(root, 'cache')), false);
  } finally { await rm(root, { recursive: true, force: true }); }
});
