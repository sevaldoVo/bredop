// Стаб вашего storage.ts (idb-keyval) — только для type-check патча.
// В реальном проекте НЕ переносите: используйте существующий модуль.
import type { FactNode } from './types';
import type { FactLink } from './api';

export interface CaseGraph {
  id: string;
  nodes: FactNode[];
  links: FactLink[];
}

export async function saveCase(caseId: string, graph: CaseGraph): Promise<void> {
  // в оригинале: await set(`case:${caseId}`, graph) из idb-keyval
  void caseId; void graph;
}
