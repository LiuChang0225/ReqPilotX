/** Test driver for the documented subprocess handle API, using real OS children. */
import { spawn, execFileSync } from 'node:child_process';
import { access } from 'node:fs/promises';
import { constants } from 'node:fs';
import { isAbsolute } from 'node:path';
export const nativeSubprocess = {
  async resolveExecutable(name) {
    const path = isAbsolute(name) ? name : execFileSync('which', [name], {encoding:'utf8'}).trim();
    await access(path, constants.X_OK); return path;
  },
  spawn({argv,cwd,env,signal}) {
    const base = Object.fromEntries(['PATH','HOME','LANG','TMPDIR','SYSTEMROOT'].filter(k=>process.env[k]).map(k=>[k,process.env[k]]));
    const child=spawn(argv[0],argv.slice(1),{cwd,env:{...base,...env},stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',finished=false;
    child.stdout.on('data', chunk=>{stdout=(stdout+chunk).slice(-65536);});
    child.stderr.on('data', chunk=>{stderr=(stderr+chunk).slice(-65536);});
    const terminate=()=>{if(!finished)child.kill('SIGTERM');};
    signal?.addEventListener('abort',terminate,{once:true});
    const done=new Promise((ok,fail)=>{
      child.once('error',fail);
      child.once('close',(exitCode)=>{finished=true;signal?.removeEventListener('abort',terminate);ok({exitCode});});
    });
    return {done,terminate,collected:{stdout:{readFrom:()=>({text:stdout})},stderr:{readFrom:()=>({text:stderr})}},
      async waitForExit(abort) {
        if(finished)return true;
        return new Promise(resolve=>{const stop=()=>resolve(false);abort.addEventListener('abort',stop,{once:true});done.then(()=>{abort.removeEventListener('abort',stop);resolve(true);});});
      }};
  },
};
