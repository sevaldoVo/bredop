/**
 * ВИЛКА — клиент OSINT-бэкенда.
 *
 * Типы ниже должны совпадать с src/types.ts (FactNode / FactLink).
 * Если в types.ts LinkKind уже объявлен — импортируй его оттуда,
 * а локальное объявление удали (см. комментарий в Board.tsx).
 */

import type { FactNode, FactLink, NodeKind, LinkKind, SourceRef } from './types';

// ---------------------------------------------------------------------------
// Типы, зеркалящие backend/models.py
// ---------------------------------------------------------------------------

export type { LinkKind };

/** Wire-формат связи от бэкенда (models.py): from/to/kind/weight/ISO-date. */
export interface ApiFactLink {
  id: string;
  from: string; // ЭТАП 2: бэкенд отдаёт from/to, как в types.ts
  to: string;
  kind: LinkKind;
  weight: number; // 0..1
  note?: string | null;
  createdAt: string;
}

/** Wire-формат ноды от бэкенда (backend/enrichment отдаёт title/value/meta/sources:string[]). */
export interface ApiFactNode {
  id: string;
  kind: NodeKind;
  value: string;
  title?: string;
  notes?: string;
  conf: number;
  sources: string[];
  meta: Record<string, unknown>;
  x?: number | null;
  y?: number | null;
  createdAt: string;
}

/** Конвертация wire-ноды бэкенда в доменную FactNode доски (src/types.ts). */
export function apiNodeToFactNode(a: ApiFactNode): FactNode {
  const now = Date.now();
  return {
    id: a.id,
    kind: a.kind,
    title: a.title || a.value,
    value: a.value,
    notes: a.notes ?? undefined,
    tags: [],
    sources: (a.sources ?? []).map((s, i) => {
      const url = /^https?:\/\//i.test(s) ? s : undefined;
      return { id: `${a.id}-src${i}`, title: url ? new URL(url).hostname : s, ...(url ? { url } : {}) } as SourceRef;
    }),
    conf: Math.max(0, Math.min(3, Math.round(a.conf))) as FactNode['conf'],
    x: a.x ?? 0,
    y: a.y ?? 0,
    createdAt: Date.parse(a.createdAt) || now,
    updatedAt: now,
    // ЭТАП 7: движок из meta.__engine (проставляет stampEngine на границе API).
    engine: a.meta?.__engine === 'mock' || a.meta?.__engine === 'live' ? (a.meta.__engine as 'mock' | 'live') : undefined,
  };
}

/** Конвертация wire-связи бэкенда в доменную FactLink (relation ← kind, strength ← weight). */
export function apiLinkToFactLink(a: ApiFactLink): FactLink {
  return {
    id: a.id,
    from: a.from,
    to: a.to,
    relation: a.kind,
    kind: a.kind,
    weight: a.weight,
    strength: a.weight >= 0.75 ? 'strong' : a.weight >= 0.4 ? 'medium' : 'weak',
    note: a.note ?? undefined,
    createdAt: Date.parse(a.createdAt) || Date.now(),
  };
}

export interface EnrichResponse {
  nodes: ApiFactNode[];
  links: ApiFactLink[];
  engine: 'mock' | 'live';
  took_ms: number;
}

/** Нода с проставленным движком (для бейджа «СИМУЛЯЦИЯ» на доске). */
export function stampEngine(n: ApiFactNode, engine: 'mock' | 'live'): ApiFactNode {
  return { ...n, meta: { ...(n.meta ?? {}), __engine: engine } };
}

/** Сообщения стрима WebSocket /ws/enrich (ЭТАП 2). */
export type WsEnrichEvent =
  | { status: 'started'; node_id: string }
  | { status: 'found'; node?: ApiFactNode; link?: ApiFactLink }
  | { status: 'done'; engine: 'mock' | 'live'; took_ms: number; count: number }
  | { status: 'error'; detail: string }
  | { status: 'pong' };

class EnrichError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = 'EnrichError';
    // Пробрасываем 401/402 наружу без проброса через весь UI-код.
    if (typeof window !== 'undefined' && (status === 401 || status === 402)) {
      window.dispatchEvent(new CustomEvent('vilka-api', { detail: { type: status === 401 ? 'unauthorized' : 'payment-required' } }));
    }
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
// ЭТАП 6: JWT-авторизация (токен в localStorage, ключ `vilka_jwt`)
// ---------------------------------------------------------------------------

const TOKEN_STORAGE_KEY = 'vilka_jwt';

export function loadToken(): string | null {
  try {
    return globalThis.localStorage?.getItem(TOKEN_STORAGE_KEY) ?? null;
  } catch {
    return null; // SSR/тесты без localStorage
  }
}

export function clearToken(): void {
  try {
    globalThis.localStorage?.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    /* noop */
  }
}

/** Заголовок Authorization, если токен есть. */
function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = loadToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

/** Разбор payload JWT (без верификации подписи — только для локальной проверки exp). */
export function decodeJwtExp(token: string): number | null {
  try {
    const payload = token.split('.')[1];
    if (!payload) return null;
    const json = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/'))) as { exp?: number };
    return typeof json.exp === 'number' ? json.exp : null;
  } catch {
    return null;
  }
}

export interface AuthUser {
  id: number;
  email: string;
  credits_balance: number;
  is_admin: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface CheckoutResult {
  invoice_id: string;
  pay_url: string;
  amount_usd: number;
  credits: number;
  status: string;
}

/** POST /api/register — регистрация нового аккаунта (даёт стартовые кредиты). */
export async function register(email: string, password: string): Promise<TokenResponse> {
  const r = await fetch(`${API_BASE}/api/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) throw new EnrichError(r.status, `register ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()) as TokenResponse;
}

/** POST /api/login — возвращает JWT. */
export async function login(email: string, password: string): Promise<TokenResponse> {
  const r = await fetch(`${API_BASE}/api/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) throw new EnrichError(r.status, `login ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()) as TokenResponse;
}

export interface HealthInfo {
  status: string;
  service: string;
  auth_enforced?: boolean;
}

/** GET /api/health — живость бэкенда + режим авторизации (для демо-входа в AuthGate). */
export async function fetchHealth(): Promise<HealthInfo> {
  const r = await fetch(`${API_BASE}/api/health`);
  if (!r.ok) throw new EnrichError(r.status, `health ${r.status}`);
  return (await r.json()) as HealthInfo;
}

/** GET /api/me — профиль и баланс кредитов. */
export async function fetchMe(): Promise<AuthUser> {
  const r = await fetch(`${API_BASE}/api/me`, { headers: authHeaders() });
  if (!r.ok) throw new EnrichError(r.status, `me ${r.status}`);
  return (await r.json()) as AuthUser;
}

/** POST /api/billing/checkout — крипто-инвойс на пополнение кредитов. */
export async function createCheckout(amountUsd: number): Promise<CheckoutResult> {
  const r = await fetch(`${API_BASE}/api/billing/checkout`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ amount_usd: amountUsd }),
  });
  if (!r.ok) throw new EnrichError(r.status, `checkout ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return (await r.json()) as CheckoutResult;
}


// ---------------------------------------------------------------------------
// Конвертация доменной модели доски -> wire-формат бэкенда (models.py)
//
// P0 FIX (ЭТАП 6): backend/models.py живёт с extra="forbid". Доска хранит
// расширенную FactNode (notes/tags/sources: SourceRef[]/updatedAt/pinned), а
// связь — свободную метку relation/strength. Голый spread ноды в тело запроса
// давал 422 extra_forbidden. Поэтому на границе — строгие мапперы:
// отправляем РОВНО те поля, которые ждут FactNode/FactLink из models.py.
// ---------------------------------------------------------------------------

/** Маппинг русской UI-метки связи -> машинный LinkKind бэкенда. */
const REL: Record<string, LinkKind> = {
  владеет: 'owned_by',
  принадлежит: 'belongs_to',
  состоит: 'belongs_to',
  проживает: 'located_in',
  работает: 'works_at',
  упоминается: 'mentioned_in',
  слив: 'leaked_with',
  резолвится: 'resolves_to',
  хостится: 'hosted_at',
  контакт: 'contact_of',
};

/** Нормализация kind связи: уже валидный kind -> как есть; метка -> через REL; иначе related_to. */
function linkKindOf(l: FactLink): LinkKind {
  if (l.kind) return l.kind;
  const rel = (l.relation ?? '').trim().toLowerCase();
  return REL[rel] ?? 'related_to';
}

/** Вес связи: явный weight бэкенда -> как есть; иначе из strength. */
function linkWeightOf(l: FactLink): number {
  if (typeof l.weight === 'number') return Math.min(1, Math.max(0, l.weight));
  return l.strength === 'strong' ? 0.9 : l.strength === 'weak' ? 0.2 : 0.5;
}

/** Wire-нода FactNode (models.py): без notes/tags/updatedAt/pinned. */
export interface WireFactNode {
  id: string;
  kind: NodeKind;
  value: string;
  label: string;
  conf: number;
  sources: string[];
  meta: Record<string, unknown>;
  x: number;
  y: number;
  createdAt: string;
}

/** FactNode (доска) -> wire-объект, принимаемый моделями бэкенда (extra="forbid"). */
export function toWire(n: FactNode): WireFactNode {
  return {
    id: n.id,
    kind: n.kind,
    value: n.value ?? n.title,
    label: n.title,
    conf: n.conf,
    // SourceRef[] -> string[]: сначала URL, иначе заголовок источника.
    sources: (n.sources ?? []).map((s) => (typeof s === 'string' ? s : s.url || s.title || 'source')),
    meta: {},
    x: n.x,
    y: n.y,
    createdAt: new Date(n.createdAt).toISOString(),
  };
}

/** Wire-связь FactLink (models.py): from/to/kind/weight вместо relation/strength. */
export interface WireFactLink {
  id: string;
  from: string;
  to: string;
  kind: LinkKind;
  weight: number;
  note: string | null;
  createdAt: string;
}

/** FactLink (доска) -> wire-объект бэкенда. */
export function toWireLink(l: FactLink): WireFactLink {
  return {
    id: l.id,
    from: l.from,
    to: l.to,
    kind: linkKindOf(l),
    weight: linkWeightOf(l),
    note: l.note ?? null,
    createdAt: new Date(l.createdAt).toISOString(),
  };
}

/** Снимок доски -> wire-граф для /api/spider, /api/path, /api/export. */
export function toWireGraph(g: { nodes: FactNode[]; links: FactLink[] }): { nodes: WireFactNode[]; links: WireFactLink[] } {
  return {
    nodes: g.nodes.map(toWire),
    links: g.links.map(toWireLink),
  };
}

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
      // ЭТАП 6: JWT подставляется автоматически (402 Payment Required, если кредиты кончились)
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ source_node: toWire(node) }),
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
    // БЛОК 2.6: JWT субпротоколом ['bearer', token] (токен не в URL). Фолбэк ?token= на бэке.
    const token = loadToken();
    const ws = new WebSocket(`${WS_BASE}/ws/enrich`, token ? ['bearer', token] : undefined);
    let settled = false;

    ws.onopen = () => ws.send(JSON.stringify({ action: 'enrich', node: toWire(node) }));
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

// ===========================================================================
// ЭТАП 3: Spider (рекурсивная раскрутка) + поиск пути (networkx на бэке)
// ===========================================================================

export interface SpiderEvent {
  status:
    | 'spider_started' | 'enriching' | 'found' | 'skipped' | 'done'
    | 'spider_done' | 'spider_stopped' | 'error';
  run_id?: string;
  node?: ApiFactNode;
  link?: ApiFactLink;
  node_id?: string;
  kind?: NodeKind;
  value?: string;
  depth?: number;
  reason?: string;
  detail?: string;
  expanded?: number;
  new_nodes?: number;
  skipped?: number;
  /** ЭТАП 7: движок последнего enrich-цикла (для бейджа «СИМУЛЯЦИЯ»). */
  engine?: 'mock' | 'live';
}

export interface PathResult {
  found: boolean;
  hops: number;
  path_nodes: Array<{ id: string; kind: NodeKind; value: string }>;
  path_links: Array<{ id: string; from: string; to: string; kind: string }>;
}

/** Рекурсивная раскрутка графа через WebSocket — стрим событий в реальном
 *  времени. Бэкенд сам следит за циклами (dedup по kind+value) и rate-limit. */
export async function spiderRun(
  graph: { nodes: FactNode[]; links: FactLink[] },
  startNodeId: string,
  depth: number,
  onEvent: (ev: SpiderEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    // БЛОК 2.6: JWT субпротоколом ['bearer', token] (токен не в URL). Фолбэк ?token= на бэке.
    const token = loadToken();
    const ws = new WebSocket(`${WS_BASE}/ws/enrich`, token ? ['bearer', token] : undefined);
    const finish = (fn: () => void) => { try { ws.close(); } catch { /* noop */ } fn(); };
    signal?.addEventListener('abort', () => finish(() => reject(new Error('aborted'))));
    ws.onopen = () =>
      ws.send(JSON.stringify({ action: 'spider', start_node_id: startNodeId, depth, graph: toWireGraph(graph) }));
    ws.onmessage = (e) => {
      const ev = JSON.parse(e.data as string) as SpiderEvent;
      onEvent(ev);
      if (ev.status === 'spider_done' || ev.status === 'spider_stopped') finish(resolve);
      else if (ev.status === 'error') finish(() => reject(new Error(ev.detail ?? 'spider error')));
    };
    ws.onerror = () => finish(() => reject(new Error('websocket error')));
  });
}

/** HTTP-вариант спайдера: fire-and-forget джоба (для батчей без сокета). */
export async function spiderStart(
  graph: { nodes: FactNode[]; links: FactLink[] },
  startNodeId: string,
  depth = 2,
): Promise<string> {
  const r = await fetch(`${API_BASE}/api/spider`, {
    method: 'POST',
    headers: authHeaders({ 'content-type': 'application/json' }),
    body: JSON.stringify({ start_node_id: startNodeId, depth, graph: toWireGraph(graph) }),
  });
  if (!r.ok) throw new Error(`spider start failed: ${r.status}`);
  return ((await r.json()) as { job_id: string }).job_id;
}

export async function spiderJobStatus(jobId: string): Promise<Record<string, unknown>> {
  const r = await fetch(`${API_BASE}/api/spider/${encodeURIComponent(jobId)}`);
  if (!r.ok) throw new Error(`spider status failed: ${r.status}`);
  return (await r.json()) as Record<string, unknown>;
}

/** Кратчайший путь между двумя нодами: фронт шлёт снимок доски, бэк считает networkx. */
export async function findPath(
  graph: { nodes: FactNode[]; links: FactLink[] },
  fromId: string,
  toId: string,
): Promise<PathResult> {
  const r = await fetch(
    `${API_BASE}/api/path?from_id=${encodeURIComponent(fromId)}&to_id=${encodeURIComponent(toId)}`,
    {
      method: 'POST',
      headers: authHeaders({ 'content-type': 'application/json' }),
      body: JSON.stringify(toWireGraph(graph)),
    },
  );
  if (!r.ok) throw new Error(`path query failed: ${r.status} ${(await r.text()).slice(0, 200)}`);
  return (await r.json()) as PathResult;
}

// ---------------------------------------------------------------------------
// ЭТАП 6: экспорт графа (GraphML для Maltego/Gephi) — скачивание файла
// ---------------------------------------------------------------------------

/** Скачивает GraphML-снимок доски. Возвращает Blob — фронт делает download. */
export async function exportGraph(
  graph: { nodes: FactNode[]; links: FactLink[] },
  format: 'graphml' | 'json' = 'graphml',
): Promise<Blob> {
  const r = await fetch(`${API_BASE}/api/export?format=${format}`, {
    method: 'POST',
    headers: authHeaders({ 'content-type': 'application/json' }),
    body: JSON.stringify(toWireGraph(graph)),
  });
  if (!r.ok) throw new EnrichError(r.status, `export ${r.status}`);
  return await r.blob();
}
