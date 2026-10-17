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
  from: string; // id исходной ноды (ЭТАП 2: бэкенд отдаёт from/to, как в types.ts)
  to: string;   // id целевой ноды
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

/** Сообщения стрима WebSocket /ws/enrich (ЭТАП 2). */
export type WsEnrichEvent =
  | { status: 'started'; node_id: string }
  | { status: 'found'; node: FactNode }
  | { status: 'found'; link: FactLink }
  | { status: 'done'; engine: 'mock' | 'live'; took_ms: number; count: number }
  | { status: 'error'; detail: string }
  | { status: 'pong' };

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

// ---------------------------------------------------------------------------
// WebSocket /ws/enrich — стримовый пробив (ЭТАП 2, задел под батчи)
// ---------------------------------------------------------------------------

const WS_BASE: string = (() => {
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env;
  const http = env?.VITE_VILKA_API?.replace(/\/$/, '') ?? 'http://localhost:8000';
  return http.replace(/^http/, 'ws'); // http->ws, https->wss
})();

/**
 * Стримовый обогащение одной ноды. Коллбэк получает каждое сообщение сервера
 * (started -> found* -> done). Промис резолвится на 'done', реджектится на
 * 'error' или закрытии сокета до завершения.
 */
export function enrichNodeStream(
  node: FactNode,
  onEvent: (ev: WsEnrichEvent) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${WS_BASE}/ws/enrich`);
    let settled = false;

    ws.onopen = () => ws.send(JSON.stringify({ action: 'enrich', node }));
    ws.onmessage = (ev: MessageEvent<string>) => {
      let msg: WsEnrichEvent;
      try {
        msg = JSON.parse(ev.data) as WsEnrichEvent;
      } catch {
        return;
      }
      onEvent(msg);
      if (msg.status === 'done') {
        settled = true;
        ws.close();
        resolve();
      } else if (msg.status === 'error') {
        settled = true;
        ws.close();
        reject(new EnrichError(-1, msg.detail));
      }
    };
    ws.onerror = () => {
      if (!settled) reject(new EnrichError(-1, 'websocket error'));
    };
    ws.onclose = () => {
      if (!settled) reject(new EnrichError(-1, 'websocket closed before done'));
    };
  });
}
