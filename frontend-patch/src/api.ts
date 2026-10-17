/**
 * ВИЛКА — клиент OSINT-бэкенда.
 *
 * Типы ниже должны совпадать с src/types.ts (FactNode / FactLink).
 * Если в types.ts LinkKind уже объявлен — импортируй его оттуда,
 * а локальное объявление удали (см. комментарий в Board.tsx).
 */

import type { FactNode } from './types';

// ---------------------------------------------------------------------------
// Типы, зеркалящие backend/models.py
// ---------------------------------------------------------------------------

export type LinkKind =
  | 'belongs_to'
  | 'owned_by'
  | 'contact_of'
  | 'resolves_to'
  | 'hosted_at'
  | 'works_at'
  | 'located_in'
  | 'mentioned_in'
  | 'related_to'
  | 'leaked_with';

export interface FactLink {
  id: string;
  source: string; // id исходной ноды
  target: string; // id целевой ноды
  kind: LinkKind;
  weight: number; // 0..1
  note?: string | null;
  createdAt: string;
}

export interface EnrichResponse {
  nodes: FactNode[];
  links: FactLink[];
  engine: 'mock' | 'live';
  took_ms: number;
}

class EnrichError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = 'EnrichError';
  }
}

// ---------------------------------------------------------------------------
// Конфиг
// ---------------------------------------------------------------------------

/** Бэкенд задаётся через .env: VITE_VILKA_API=http://localhost:8000 */
const API_BASE: string = (() => {
  // import.meta.env есть только в Vite-сборке — защищаемся от type-check
  // чистого tsc и от запуска вне Vite (SSR/тесты).
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env;
  return env?.VITE_VILKA_API?.replace(/\/$/, '') ?? 'http://localhost:8000';
})();

const REQUEST_TIMEOUT_MS = 30_000;

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

/**
 * «Пробивает» ноду на бэкенде и возвращает найденные ноды + связи.
 * Связи уже содержат реальный id исходной ноды, поэтому их можно
 * класть в граф как есть.
 */
export async function enrichNode(node: FactNode): Promise<EnrichResponse> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const res = await fetch(`${API_BASE}/api/enrich`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_node: node }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new EnrichError(res.status, `enrich ${res.status}: ${detail || res.statusText}`);
    }

    const data = (await res.json()) as EnrichResponse;
    // Защита от дублей: бэкенд не должен возвращать саму исходную ноду,
    // но на всякий случай фильтруем.
    return {
      ...data,
      nodes: data.nodes.filter((n) => n.id !== node.id),
    };
  } finally {
    clearTimeout(timer);
  }
}

export { EnrichError };
