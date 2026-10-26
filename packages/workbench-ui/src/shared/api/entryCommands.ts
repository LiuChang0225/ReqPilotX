/** Pending entry intentions contain identity/metadata only, never source bytes. */
import { saasStorageKey } from './saasSession';
export interface EntryIntent {
  commandId: string;
  kind: 'project.create' | 'document.import';
  scope: string;
  payload: Record<string, string | number>;
}

export interface EntryCommandOptions {
  namespace?: string | (() => string | undefined);
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'>;
}

export function entryCommandStore(options: EntryCommandOptions) {
  // Pair supplies its stable application ID, independent of service port.
  // The standalone single-owner service uses its browser origin.
  // Importing/initializing a read-only host must not require browser state.
  const prefix = () => saasStorageKey('reqpilotx:entry:v1:' + ((typeof options.namespace === 'function' ? options.namespace() : options.namespace) ?? window.location.origin) + ':');
  const retainedKeys = new WeakMap<EntryIntent, string>();
  const storage = () => options.storage ?? localStorage;
  const key = (kind: EntryIntent['kind'], scope: string, payload: EntryIntent['payload']) =>
    prefix() + JSON.stringify([kind, scope, payload]);
  const pending = (): EntryIntent[] => {
    const target = storage(), entries: EntryIntent[] = [];
    for (let i = 0; i < target.length; i++) {
      const itemKey = target.key(i);
      if (!itemKey?.startsWith(prefix())) continue;
      const item = JSON.parse(target.getItem(itemKey)!);
      if (typeof item?.commandId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(item.commandId)
          || !['project.create', 'document.import'].includes(item.kind)
          || typeof item.scope !== 'string' || !item.payload
          || key(item.kind, item.scope, item.payload) !== itemKey) {
        throw new Error('待恢复的提交记录无效，请检查本地记录后再提交。');
      }
      retainedKeys.set(item as EntryIntent, itemKey);
      entries.push(item as EntryIntent);
    }
    return entries;
  };
  return {
    pending,
    acquire(kind: EntryIntent['kind'], scope: string, payload: EntryIntent['payload']) {
      const prior = pending().find(item => key(item.kind, item.scope, item.payload) === key(kind, scope, payload));
      if (prior) return { intent: prior, retry: true };
      const intent: EntryIntent = { commandId: crypto.randomUUID(), kind, scope, payload };
      // A durable ID must be saved before the first write. Storage denial fails
      // before submission, so refresh cannot silently create a second command.
      storage().setItem(key(kind, scope, payload), JSON.stringify(intent));
      retainedKeys.set(intent, key(kind, scope, payload));
      return { intent, retry: false };
    },
    complete(intent: EntryIntent) {
      const target = storage(), itemKey = retainedKeys.get(intent) ?? key(intent.kind, intent.scope, intent.payload);
      const current = target.getItem(itemKey);
      // A late query may finish after another client has already resolved this
      // command and started a new, intentional submission with the same payload.
      if (current && JSON.parse(current).commandId === intent.commandId) target.removeItem(itemKey);
    },
  };
}

export async function fileSha256(file: File): Promise<string> {
  const bytes = typeof file.arrayBuffer === 'function' ? await file.arrayBuffer() : await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(file);
  });
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), byte => byte.toString(16).padStart(2, '0')).join('');
}
