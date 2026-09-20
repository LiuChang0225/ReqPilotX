/** An adapter consumes a built shared artifact, not another editable business implementation. */
import { readFile, realpath } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
export async function loadSharedArtifacts(directory, required) {
  const root = await realpath(directory);
  const contract = JSON.parse(await readFile(resolve(root, 'host-contract.json'), 'utf8'));
  if (contract.schema !== required.sharedUiSchema || contract.major !== required.sharedUiMajor) throw Error('Shared UI contract is incompatible with this Pair adapter.');
  const missing = required.requiredExports.filter(name => !contract.exports?.includes(name));
  if (missing.length) throw Error('Shared UI is missing required exports: ' + missing.join(', '));
  const panel = await readFile(resolve(root, 'reqpilotx-readonly.cjs'), 'utf8');
  const css = await readFile(resolve(root, 'reqpilotx-readonly.css'), 'utf8');
  const requests = [...new Set([...panel.matchAll(/require\(["']([^"']+)["']\)/g)].map(x => x[1]))];
  if (requests.some(x => !['react', 'react/jsx-runtime', 'react-dom'].includes(x))) throw Error('Unexpected shared UI external: ' + requests.join(', '));
  const sha = value => createHash('sha256').update(value).digest('hex');
  return { panel, css, requests, contract, identity: sha(JSON.stringify({ contract, panel: sha(panel), css: sha(css) })) };
}
