/** Pair-owned local runtime. Uses DSH subprocess handles; never adopts an unrelated service. */
import { mkdir, readFile, writeFile, open, unlink, realpath, readdir, stat } from 'node:fs/promises';
import { resolve, dirname, join } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { randomBytes, randomUUID, createHash } from 'node:crypto';
import { createServer } from 'node:net';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
export async function freePort() {
  const server = createServer(); await new Promise((ok, fail) => { server.once('error', fail); server.listen(0, '127.0.0.1', ok); });
  const port = server.address().port; await new Promise(ok => server.close(ok)); return port;
}
export function ownerAlive(pid) {
  if (!Number.isInteger(pid) || pid < 2) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code !== 'ESRCH'; }
}
export class ManagedRuntime {
  constructor(subprocess, config = {}) {
    this.subprocess = subprocess;
    this.appRoot = resolve(config.appRoot || fileURLToPath(new URL('../../../../prototype', import.meta.url)));
    this.dataRoot = resolve(config.dataRoot || join(homedir(), '.local/share/reqpilotx/pair'));
    this.python = config.python || join(this.appRoot, '.venv/bin/python');
    this.binDir = config.postgresBinDir;
    this.token = randomUUID(); this.handles = []; this.cancel = new AbortController();
    this.snapshot = { mode: 'managed', status: 'STARTING', stage: 'dependencies', message: '正在准备 ReqPilotX 服务', owned: true };
  }
  async privateWrite(name, value) { await writeFile(join(this.dataRoot, name), JSON.stringify(value, null, 2) + '\n', { mode: 0o600 }); }
  async claim() {
    await mkdir(this.dataRoot, { recursive: true, mode: 0o700 });
    const marker = join(this.dataRoot, 'owner.json');
    let owner;
    try { owner = JSON.parse(await readFile(marker, 'utf8')); }
    catch (e) {
      if (e.code !== 'ENOENT') throw e;
      if ((await readdir(this.dataRoot)).length) throw Error('Data directory is not an empty ReqPilotX-owned directory.');
      owner = { application: 'reqpilotx-pair', version: 1, id: randomUUID() }; await this.privateWrite('owner.json', owner);
    }
    if (owner.application !== 'reqpilotx-pair' || owner.version !== 1) throw Error('Data directory ownership does not match.');
    this.ownerId = owner.id;
    const lock = join(this.dataRoot, 'runtime.lock');
    try { const prior = JSON.parse(await readFile(lock, 'utf8')); if (ownerAlive(prior.pid)) throw Error('This ReqPilotX data directory is already in use by another Pair process.'); await unlink(lock); }
    catch (e) { if (e.code !== 'ENOENT') throw e; }
    const lease = await open(lock, 'wx', 0o600);
    try { await lease.writeFile(JSON.stringify({ pid: process.pid, token: this.token })); } finally { await lease.close(); }
    this.claimed = true;
  }
  spawn(argv, env = {}, collect = true) {
    this.cancel.signal.throwIfAborted();
    const handle = this.subprocess.spawn({ argv, cwd: this.appRoot, env, signal: this.cancel.signal,
      stdio: { stdin: 'ignore', stdout: { maxBytes: collect ? 65536 : 16384 }, stderr: { maxBytes: 65536 } }, graceMs: 15000 });
    this.handles.push(handle); return handle;
  }
  async command(argv, env = {}) {
    const handle = this.spawn(argv, env); const result = await handle.done;
    if (result.exitCode !== 0) {
      const detail = handle.collected.stderr?.readFrom(0).text || handle.collected.stdout?.readFrom(0).text || '';
      throw Error('Runtime preparation command failed: ' + dirname(argv[0]).split('/').pop() + '/' + argv[0].split('/').pop() + ': ' + detail.slice(-1600));
    }
    return handle.collected.stdout?.readFrom(0).text || '';
  }
  stage(stage, message) { this.snapshot = { ...this.snapshot, status: 'STARTING', stage, message }; }
  start() {
    if (!this.starting) this.starting = this.startInner().catch(async error => {
      const message = String(error.message).split(this.secret || '\u0000').join('[redacted]');
      this.snapshot = { mode: 'managed', status: 'ERROR', stage: this.snapshot.stage, message, owned: true };
      await this.cleanup();
      if (this.claimed) await this.release();
    });
    return this.starting;
  }
  async startInner() {
    await this.claim();
    this.python = await this.subprocess.resolveExecutable(this.python);
    await stat(join(this.appRoot, 'app/main.py'));
    const initdb = this.binDir ? join(this.binDir, 'initdb') : await this.subprocess.resolveExecutable('initdb');
    this.binDir = dirname(await realpath(initdb));
    for (const name of ['postgres', 'psql', 'createdb', 'pg_isready']) await this.subprocess.resolveExecutable(join(this.binDir, name));
    const pgData = join(this.dataRoot, 'postgres');
    await mkdir(join(this.dataRoot, 'storage'), { recursive: true, mode: 0o700 });
    try { this.secret = (await readFile(join(this.dataRoot, 'database-password'), 'utf8')).trim(); }
    catch (e) { if (e.code !== 'ENOENT') throw e; this.secret = randomBytes(32).toString('hex'); await writeFile(join(this.dataRoot, 'database-password'), this.secret + '\n', { mode: 0o600 }); }
    if (!/^[a-f0-9]{64}$/.test(this.secret)) throw Error('Invalid private database credential file.');
    try { await stat(join(pgData, 'PG_VERSION')); }
    catch (e) {
      if (e.code !== 'ENOENT') throw e;
      this.stage('storage', '正在初始化应用数据目录');
      await this.command([initdb, '-D', pgData, '--username=reqpilotx', '--auth-local=trust', '--auth-host=scram-sha-256', '--encoding=UTF8', '--locale=C', '--pwfile=' + join(this.dataRoot, 'database-password')]);
    }
    // An unexplained live cluster is never signalled or adopted by this new owner.
    try { const pid = Number((await readFile(join(pgData, 'postmaster.pid'), 'utf8')).split('\n')[0]); if (ownerAlive(pid)) throw Error('The owned data directory still has a live database process; it was not started by this Pair instance.'); }
    catch (e) { if (e.code !== 'ENOENT') throw e; }
    const pgPort = await freePort();
    this.stage('database', '正在启动应用数据库');
    const pg = this.spawn([join(this.binDir, 'postgres'), '-D', pgData, '-h', '127.0.0.1', '-p', String(pgPort), '-k', ''], {}, false);
    const dbEnv = { PGPASSWORD: this.secret, PGHOST: '127.0.0.1', PGPORT: String(pgPort), PGUSER: 'reqpilotx', PGDATABASE: 'postgres' };
    let pgExit; pg.done.then(result => { pgExit = result; }, error => { pgExit = { error: error.message }; });
    let ready = false;
    for (let i = 0; i < 100; i++) {
      if (pgExit) throw Error('Managed database stopped before readiness.');
      const check = this.spawn([join(this.binDir, 'pg_isready'), '-q'], dbEnv);
      if ((await check.done).exitCode === 0) { ready = true; break; }
      await delay(100);
    }
    if (!ready) throw Error('Managed database did not become ready.');
    const exists = await this.command([join(this.binDir, 'psql'), '-Atqc', "SELECT 1 FROM pg_database WHERE datname='reqpilotx'"], dbEnv);
    if (exists.trim() !== '1') await this.command([join(this.binDir, 'createdb'), 'reqpilotx'], dbEnv);
    const env = { REQPILOT_ENV: 'local', REQPILOT_PROCESS_ROLE: 'api', REQPILOT_AGENT_RUNTIME: 'none', REQPILOT_DATABASE_URL: 'postgresql://reqpilotx:' + this.secret + '@127.0.0.1:' + pgPort + '/reqpilotx', REQPILOT_OBJECT_ROOT: join(this.dataRoot, 'storage'), REQPILOT_SCRATCH_ROOT: join(this.dataRoot, 'scratch'), REQPILOT_ALLOWED_HOSTS: '127.0.0.1,localhost', REQPILOT_MANAGED_INSTANCE: this.token };
    this.stage('migrations', '正在恢复项目与文档存储');
    await this.command([this.python, join(this.appRoot, 'scripts/runtime_admin.py'), 'migrate'], env);
    this.stage('api', '正在启动 ReqPilotX');
    const descriptor = join(this.dataRoot, 'api-instance.json');
    try { await unlink(descriptor); } catch (e) { if (e.code !== 'ENOENT') throw e; }
    const api = this.spawn([this.python, fileURLToPath(new URL('./managed_api.py', import.meta.url)), this.appRoot, descriptor], env, false);
    let apiExit; api.done.then(result => { apiExit = result; }, error => { apiExit = { error: error.message }; });
    let info;
    for (let i = 0; i < 200; i++) {
      this.cancel.signal.throwIfAborted();
      if (apiExit) throw Error('Managed API stopped before readiness: ' + (api.collected.stderr?.readFrom(0).text || '').slice(-1200));
      try {
        info = JSON.parse(await readFile(descriptor, 'utf8'));
        if (info.instanceId !== this.token) throw Error('Managed API identity mismatch.');
        const response = await fetch(info.origin + '/api/projects', { signal: AbortSignal.timeout(1500) });
        await response.body?.cancel(); if (response.ok) break;
      } catch { info = null; }
      info = null; await delay(150);
    }
    if (!info) throw Error('Managed API did not become ready.');
    const pgPid = Number((await readFile(join(pgData, 'postmaster.pid'), 'utf8')).split('\n')[0]);
    this.snapshot = { mode: 'managed', status: 'READY', stage: 'ready', message: 'ReqPilotX 已就绪', owned: true, serviceOrigin: info.origin, instanceId: this.token };
    await this.privateWrite('runtime-receipt.json', { ownerId: this.ownerId, ownerPid: process.pid, instanceId: this.token, apiPid: info.pid, apiOrigin: info.origin, postgresPid: pgPid, postgresPort: pgPort, appRoot: this.appRoot, responseMatrixSha256: createHash('sha256').update(await readFile(join(this.appRoot, 'app/services/response_matrix.py'))).digest('hex'), workerStarted: false });
    const exited = () => { if (!this.stopping && this.snapshot.status === 'READY') { this.snapshot = { mode: 'managed', status: 'ERROR', stage: 'runtime', message: 'ReqPilotX 服务已停止，请重新启用插件恢复。', owned: true }; void this.cleanup().catch(() => { this.snapshot = { ...this.snapshot, message: '自有进程尚未完全停止，已保留所有权记录。' }; }); } };
    api.done.then(exited, exited); pg.done.then(exited, exited);
  }
  async cleanup() {
    // Sequential termination lets the API close its pool before PostgreSQL checkpoints and stops.
    for (const handle of [...this.handles].reverse()) {
      handle.terminate(); if (!await handle.waitForExit(AbortSignal.timeout(20000))) throw Error('Owned process did not stop; ownership lock retained.');
    }
    this.handles = [];
  }
  async release() {
    const path = join(this.dataRoot, 'runtime.lock');
    try { const lock = JSON.parse(await readFile(path, 'utf8')); if (lock.token === this.token) await unlink(path); } catch (e) { if (e.code !== 'ENOENT') throw e; }
    this.claimed = false;
  }
  stop() {
    if (!this.stopping) this.stopping = (async () => { if (this.snapshot.status !== 'READY') this.cancel.abort(); await this.starting; await this.cleanup(); this.cancel.abort(); if (this.claimed) await this.release(); this.snapshot = { mode: 'managed', status: 'STOPPED', message: 'ReqPilotX 已停止，项目数据已保留', owned: true }; })();
    return this.stopping;
  }
}
