import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

const root = process.env.PAIR_DSH_CONTRACT_ROOT;
const skip = root ? false : 'Set PAIR_DSH_CONTRACT_ROOT to the copied current DSH public contract tree.';
const read = path => readFile(join(root, path), 'utf8');

test('client navigation depends on the actual Workspace/Session public contracts', { skip }, async () => {
  const navigation = await read('packages/client/ui-workspace/src/client/navigation.ts');
  const sessions = await read('packages/api/session-controller/src/client/contract/sessions.ts');
  const workspace = await read('packages/api/workspace-controller/src/index.ts');
  const locale = await read('packages/client/locale/src/client/index.ts');
  const metadata = JSON.parse(await read('packages/client/ui-workspace/package.json'));
  const pairMetadata = JSON.parse(await readFile(new URL('../client/package.json', import.meta.url), 'utf8'));
  assert.match(navigation, /openSession\(target: SessionTarget\): void/);
  assert.match(navigation, /uiWorkspace: UiWorkspace/);
  assert.match(sessions, /retain\(target: SessionTarget/);
  assert.match(sessions, /using<T>\(target: SessionTarget/);
  assert.match(sessions, /scope\(id: SessionId\): AgentContext \| undefined/);
  assert.match(workspace, /@Remote\('create'\)/);
  assert.match(workspace, /@Remote\('rename'\)/);
  assert.match(workspace, /rename\(request: WorkspaceRenameRequest\): Promise<WorkspaceValue>/);
  assert.match(locale, /getSnapshot\(\): LocaleSnapshot/);
  assert.match(locale, /subscribe\(fn: \(\) => void\): \(\) => void/);
  assert.match(locale, /bind\(ns: string\): Translate/);
  assert.ok(metadata['dsh'].client.inject.includes('@deepseek-ai/dsh-client-ui-conversation'));
  const inject = pairMetadata['dsh'].client.inject;
  assert.ok(inject.includes('@deepseek-ai/dsh-client-ui-workspace'));
  assert.ok(inject.includes('@deepseek-ai/dsh-api-workspace-controller'));
});

test('adapter source uses only those owned capabilities', async () => {
  const client = await readFile(new URL('../client/src/client.cjs', import.meta.url), 'utf8');
  const host = await readFile(new URL('../host/src/task-launch.js', import.meta.url), 'utf8');
  assert.match(client, /ctx\.uiWorkspace\.openSession\(sessionId\)/);
  const app = await readFile(new URL('../client/src/catalog.cjs', import.meta.url), 'utf8');
  const sharedApp = await readFile(new URL('../../../packages/workbench-ui/src/host/ReqPilotXApplication.tsx', import.meta.url), 'utf8');
  assert.match(client, /ctx\.sessions\.using\(sessionId/);
  assert.match(client, /reference\.binding\.ctx/);
  assert.doesNotMatch(client, /ctx\.sessions\.open\(/);
  assert.match(client, /ReqPilotXReadonlyPanel, \{ selection: saved\.selection, documentName: saved\.documentName, host, active: true, t \}/);
  assert.match(app, /HostedApplication, \{ key: config\.applicationId \+ ':' \+ config\.serviceOrigin, config, openSession, t \}/);
  assert.match(app, /ReqPilotXApplication, \{ host, theme, t \}/);
  assert.match(sharedApp, /HostLocaleProvider t=\{t\}/);
  assert.match(host, /services\.workspaces\.create\(\{ path: workspace \}\)/);
  assert.match(host, /services\.workspaces\.rename\(\{ workspaceId: record\.workspaceId, title \}\)/);
});
