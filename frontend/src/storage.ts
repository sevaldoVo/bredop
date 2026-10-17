import { get, set, del } from 'idb-keyval';
import type { InvestigationCase } from './types';

const INDEX_KEY = 'vilka:index'; // list of case metas
const CASE_PREFIX = 'vilka:case:';

export interface CaseMeta {
  id: string;
  name: string;
  codename?: string;
  status: InvestigationCase['status'];
  createdAt: number;
  updatedAt: number;
  nodeCount: number;
  linkCount: number;
}

export async function loadIndex(): Promise<CaseMeta[]> {
  const idx = (await get(INDEX_KEY)) as CaseMeta[] | undefined;
  return Array.isArray(idx) ? idx : [];
}

async function saveIndex(list: CaseMeta[]) {
  await set(INDEX_KEY, list);
}

export async function loadCase(id: string): Promise<InvestigationCase | null> {
  return (await get(CASE_PREFIX + id)) as InvestigationCase | null;
}

export async function saveCase(c: InvestigationCase): Promise<void> {
  c.updatedAt = Date.now();
  await set(CASE_PREFIX + c.id, c);
  const idx = await loadIndex();
  const meta: CaseMeta = {
    id: c.id,
    name: c.name,
    codename: c.codename,
    status: c.status,
    createdAt: c.createdAt,
    updatedAt: c.updatedAt,
    nodeCount: Object.keys(c.nodes).length,
    linkCount: Object.keys(c.links).length,
  };
  const i = idx.findIndex((m) => m.id === c.id);
  if (i >= 0) idx[i] = meta;
  else idx.unshift(meta);
  await saveIndex(idx);
}

export async function deleteCase(id: string): Promise<void> {
  await del(CASE_PREFIX + id);
  const idx = await loadIndex();
  await saveIndex(idx.filter((m) => m.id !== id));
}

/* ---------- export / import ---------- */

export interface Bundle {
  app: 'vilka-board';
  version: 1;
  exportedAt: number;
  cases: InvestigationCase[];
}

export function exportCases(cases: InvestigationCase[]): string {
  const bundle: Bundle = { app: 'vilka-board', version: 1, exportedAt: Date.now(), cases };
  return JSON.stringify(bundle, null, 2);
}

export async function importCases(json: string): Promise<number> {
  const parsed = JSON.parse(json) as Bundle;
  if (!parsed || !Array.isArray(parsed.cases)) throw new Error('Файл не похож на выгрузку ВИЛКИ');
  for (const c of parsed.cases) {
    await saveCase({ ...c, updatedAt: Date.now() });
  }
  return parsed.cases.length;
}
