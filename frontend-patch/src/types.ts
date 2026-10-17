// Минимальная заглушка для type-check патча.
// В реальном проекте НЕ переносите — у вас уже есть свой types.ts.
export type NodeKind = 'target'|'person'|'account'|'handle'|'phone'|'email'|'domain'|'ip'|'geo'|'org'|'event'|'note';
export interface FactNode {
  id: string; kind: NodeKind; value: string; label?: string | null;
  conf: number; sources: string[]; meta: Record<string, unknown>;
  x?: number | null; y?: number | null; createdAt: string;
}
