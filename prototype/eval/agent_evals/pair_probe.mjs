// Runs only as a plugin inside the supported `dsh --profile ... --patch` host.
// Never calls a model or reads credentials; only inspects registered metadata.
import { createHash } from 'node:crypto';

export const name = 'reqpilot-discovery-probe';
export const inject = ['skills', 'llm', 'agentDefaultModel'];

export function apply(ctx) {
  const exit = ctx.get('appExit');
  void (async () => {
    await ctx.get('loader')?.await();
    const selection = ctx.agentDefaultModel.currentSelection();
    const models = await ctx.llm.listModels(selection.provider);
    const model = models.find(row => row.id === selection.model);
    const skills = [];
    for (const row of await ctx.skills.list({ cwd: process.cwd() })) {
      if (!row.name.startsWith('reqpilot')) continue;
      const loaded = await ctx.skills.get(row.name, { cwd: process.cwd() });
      skills.push({ name: row.name, path: row.path, provider: row.provider,
        resourceBase: loaded?.resourceBase, invocation: row.invocation,
        contentSha256: createHash('sha256').update(loaded?.content ?? '').digest('hex') });
    }
    process.stdout.write(JSON.stringify({ modelCalls: 0, selection,
      model: model && { id: model.id, inputModalities: model.inputModalities,
        contextWindow: model.contextWindow, reasoningEfforts: model.reasoningEfforts }, skills }) + '\n');
    exit(skills.length === 5 && model ? 0 : 1);
  })().catch(error => {
    process.stderr.write('ReqPilot discovery failed: ' + error.message + '\n');
    exit(1);
  });
}
