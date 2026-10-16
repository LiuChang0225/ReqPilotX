/** Bind actual Web output bytes to the source candidate observed at build time. */
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { lstatSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, relative, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(fileURLToPath(new URL('..', import.meta.url)));
const filename = 'reqpilotx-build.json';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');

export function stampWebBuild(output, sourceRoot = repo) {
  const git = args => execFileSync('git', args, { cwd: sourceRoot, encoding: 'utf8' }).trim();
  const commit = git(['rev-parse', 'HEAD']);
  const tree = git(['rev-parse', 'HEAD^{tree}']);
  const dirty = git(['status', '--porcelain=v1', '--untracked-files=all', '--',
    'apps/web', 'packages/workbench-ui', 'package.json', 'package-lock.json',
    'scripts/web-build-identity.mjs']);
  const declared = git(['ls-tree', '-r', '-z', 'HEAD', '--', 'apps/web', 'packages/workbench-ui',
    'package.json', 'package-lock.json', 'scripts/web-build-identity.mjs']);
  let exactSources = true;
  for (const entry of declared.split('\0').filter(Boolean)) {
    const match = /^(\d+) blob ([0-9a-f]+)\t(.+)$/s.exec(entry);
    try {
      if (!match || !['100644', '100755'].includes(match[1])) { exactSources = false; continue; }
      const path = join(sourceRoot, match[3]), stat = lstatSync(path);
      if (!stat.isFile() || stat.isSymbolicLink()) { exactSources = false; continue; }
      const bytes = readFileSync(path);
      const blob = createHash('sha1').update('blob ' + bytes.length + '\0').update(bytes).digest('hex');
      if (blob !== match[2]) exactSources = false;
    } catch { exactSources = false; }
  }
  const root = resolve(output), files = [];
  function walk(directory) {
    for (const name of readdirSync(directory).sort()) {
      const path = join(directory, name), stat = lstatSync(path);
      if (stat.isSymbolicLink()) throw Error('Web build output contains a symlink');
      if (stat.isDirectory()) walk(path);
      else if (stat.isFile()) {
        const pathName = relative(root, path).split('\\').join('/');
        if (pathName !== filename) files.push({ path: pathName, sha256: sha(readFileSync(path)), bytes: stat.size });
      } else throw Error('Web build output is not a regular file');
    }
  }
  walk(root);
  if (!files.some(file => file.path === 'index.html')) throw Error('Web build entry is missing');
  const result = { schema_version: 'reqpilotx-web-build/v1', source_commit: commit,
    source_tree: tree, source_inputs_clean: dirty === '' && exactSources, files };
  writeFileSync(join(root, filename), JSON.stringify(result, null, 2) + '\n');
  return result;
}
