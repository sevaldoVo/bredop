import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  KINDS, CONF, STRENGTH_META, STATUS_META,
  newNode, newLink, uid,
  type InvestigationCase, type FactNode, type FactLink,
  type NodeKind, type LinkStrength, type ConfLevel, type CaseStatus,
} from '../types';
import { saveCase } from '../storage';
import { Modal, KindPicker, Seg, relTime, fmtDateTime } from '../ui';
// --- [PATCH-1] VILKA OSINT integration (STAGE 1..6): backend api client ----
import {
  enrichNode, spiderRun, findPath, exportGraph, EnrichError,
  apiNodeToFactNode, apiLinkToFactLink, stampEngine,
  type ApiFactNode, type ApiFactLink, type SpiderEvent, type WsState,
} from '../api';
import { THEMES, useTheme } from '../theme';

/* ЭТАП 9 коммит 4: inline SVG-иконки тул-рейла (stroke 1.5, без эмодзи). */
const ICONS = {
  plus: 'M12 5v14M5 12h14',
  link: 'M10 13a5 5 0 0 0 7.07 0l2.5-2.5a5 5 0 0 0-7.07-7.07L11 5M14 11a5 5 0 0 0-7.07 0L4.43 13.5a5 5 0 0 0 7.07 7.07L13 19',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-4.35-4.35',
  frame: 'M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4',
  spider: 'M12 8a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3M12 19v3M2 12h3M19 12h3M5 5l2.5 2.5M16.5 16.5L19 19M19 5l-2.5 2.5M7.5 16.5L5 19',
  chain: 'M9 12a3 3 0 0 1 3-3h4a3 3 0 0 1 0 6h-1M15 12a3 3 0 0 1-3 3H8a3 3 0 0 1 0-6h1',
  download: 'M12 3v12M7 10l5 5 5-5M4 19h16',
  bolt: 'M13 2L4 14h6l-1 8 9-12h-6l1-8z',
} as const;
function Icon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d={d} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Radius of circular layout for fresh nodes around the enriched one. */
const ENRICH_RADIUS = 240;

const NODE_W = 208;
const NODE_H = 96; // rough height for edge math

interface View { x: number; y: number; k: number }

type Selection = { type: 'node'; id: string } | { type: 'link'; id: string } | null;

export default function Board({
  initial, onExit, user, onPaymentRequired, onTopUp, onLogout, autoSpider,
}: {
  initial: InvestigationCase;
  onExit: () => void;
  /** ЭТАП 6: текущий юзер (null = гость, enrichment работает в mock-режиме). */
  user?: import('../api').AuthUser | null;
  onPaymentRequired?: (u: import('../api').AuthUser) => void;
  /** ЭТАП 7: «Пополнить» — крипто-инвойс через /api/billing/checkout. */
  onTopUp?: () => void;
  /** ЭТАП 7: «Выйти» — чистит JWT из localStorage. */
  onLogout?: () => void;
  /** ЭТАП10 коммит10, п.3: id ноды для автозапуска spider при открытии доски (quick-seed). */
  autoSpider?: string | null;
}) {
  const [c, setC] = useState<InvestigationCase>(initial);
  const cRef = useRef(c);
  cRef.current = c;

  const wrapRef = useRef<HTMLDivElement>(null);
  const boardRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const [sel, setSel] = useState<Selection>(null);
  const [linkFrom, setLinkFrom] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [query, setQuery] = useState('');
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [caseModal, setCaseModal] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  // --- [PATCH-2] OSINT state: enrichment / spider / path -------------------
  const [enrichingId, setEnrichingId] = useState<string | null>(null);
  const [spiderActive, setSpiderActive] = useState(false);
  const [pathMode, setPathMode] = useState<'off' | 'pickA' | 'pickB'>('off');
  const [pathA, setPathA] = useState<string | null>(null);
  const [pathIds, setPathIds] = useState<{ nodes: string[]; links: string[] }>({ nodes: [], links: [] });
  const [toast, setToast] = useState<string | null>(null);
  // ЭТАП 9 коммит 4: данные статус-бара + тем-свитчер
  const { theme, setTheme } = useTheme();
  const [themeMenuOpen, setThemeMenuOpen] = useState(false);
  const [wsState, setWsState] = useState<WsState>('closed');
  const [lastProbe, setLastProbe] = useState<{ engine: 'mock' | 'live'; ms: number }>({ engine: 'mock', ms: 0 });
  const spawnedRef = useRef<Set<string>>(new Set()); // ids created by backend (spawn animation)
  // КОММИТ 11, п.3: каскад found-событий — очередь с минимальным интервалом CASCADE_MS
  const CASCADE_MS = 150;
  const streamRef = useRef<{ last: number; timer: number | null }>({ last: 0, timer: null });
  const toastTimer = useRef<number | null>(null);

  const showToast = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4200);
  }, []);
  const [zoomPct, setZoomPct] = useState<number>(() => Math.round(c.view.k * 100));

  const viewRef = useRef<View>(c.view);
  const worldRef = useRef<HTMLDivElement>(null);

  /* ---------- persistence (debounced) ---------- */
  useEffect(() => {
    const t = window.setTimeout(() => {
      const cur = cRef.current;
      saveCase({ ...cur, view: viewRef.current });
    }, 450);
    return () => window.clearTimeout(t);
  }, [c]);

  // flush on unload / route change
  useEffect(() => {
    const flush = () => saveCase({ ...cRef.current, view: viewRef.current });
    window.addEventListener('beforeunload', flush);
    return () => {
      window.removeEventListener('beforeunload', flush);
      flush();
    };
  }, []);

  /* ---------- transform helpers ---------- */
  const applyTransform = useCallback((v: View) => {
    viewRef.current = v;
    if (worldRef.current)
      worldRef.current.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`;
    if (svgRef.current)
      svgRef.current.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`;
    setZoomPct(Math.round(v.k * 100));
  }, []);

  // restore saved viewport on open; first-ever open fits the board
  useLayoutEffect(() => {
    const v = cRef.current.view;
    if (v && Number.isFinite(v.k) && v.k !== 1) applyTransform(v);
    else fitAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ЭТАП10 коммит10, п.3: автозапуск spider после монтирования (quick-seed из Home). */
  useEffect(() => {
    if (!autoSpider) return;
    const t = window.setTimeout(() => { void runSpiderRef.current?.(autoSpider); }, 400);
    return () => window.clearTimeout(t);
  }, [autoSpider]);

  const screenToWorld = useCallback((sx: number, sy: number) => {
    const r = boardRef.current!.getBoundingClientRect();
    const v = viewRef.current;
    return { x: (sx - r.left - v.x) / v.k, y: (sy - r.top - v.y) / v.k };
  }, []);

  const zoomAt = useCallback((factor: number, sx?: number, sy?: number) => {
    const v = viewRef.current;
    const r = boardRef.current!.getBoundingClientRect();
    const px = sx !== undefined ? sx - r.left : r.width / 2;
    const py = sy !== undefined ? sy - r.top : r.height / 2;
    const k = Math.min(2.5, Math.max(0.25, v.k * factor));
    const wx = (px - v.x) / v.k;
    const wy = (py - v.y) / v.k;
    applyTransform({ k, x: px - wx * k, y: py - wy * k });
  }, [applyTransform]);

  const centerOn = useCallback((wx: number, wy: number) => {
    const r = boardRef.current?.getBoundingClientRect();
    if (!r) return;
    const v = viewRef.current;
    applyTransform({ ...v, x: r.width / 2 - wx * v.k, y: r.height / 2 - wy * v.k });
  }, [applyTransform]);

  const fitAll = useCallback(() => {
    const nodes = Object.values(cRef.current.nodes);
    const r = boardRef.current?.getBoundingClientRect();
    if (!r) return;
    if (!nodes.length) { applyTransform({ x: r.width / 2, y: r.height / 2, k: 1 }); return; }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + NODE_W); maxY = Math.max(maxY, n.y + NODE_H);
    }
    const pad = 60;
    const w = maxX - minX + pad * 2, h = maxY - minY + pad * 2;
    const k = Math.min(2, Math.max(0.25, Math.min(r.width / w, r.height / h)));
    applyTransform({
      k,
      x: (r.width - (maxX + minX) * k) / 2,
      y: (r.height - (maxY + minY) * k) / 2,
    });
  }, [applyTransform]);

  /* ---------- pointer interaction on the board ---------- */
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const panStart = useRef<{ vx: number; vy: number; px: number; py: number } | null>(null);
  const pinchDist = useRef<number | null>(null);
  const dragNode = useRef<{ id: string; ox: number; oy: number; moved: boolean } | null>(null);
  const threadTarget = useRef<string | null>(null);
  const [ghostPt, setGhostPt] = useState<{ x: number; y: number } | null>(null);

  const onPointerDownBoard = (e: React.PointerEvent) => {
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });

    const nodeEl = (e.target as HTMLElement).closest('.node') as HTMLElement | null;

    if (nodeEl && linkFrom) {
      // finishing a pulled thread: release over another card connects them
      const id = nodeEl.dataset.id!;
      if (id !== linkFrom) tapNode(id);
      else { setLinkFrom(null); setHint(null); }
      threadTarget.current = null;
      return;
    }
    if (nodeEl && !linkFrom) {
      const id = nodeEl.dataset.id!;
      const n = cRef.current.nodes[id];
      const w = screenToWorld(e.clientX, e.clientY);
      dragNode.current = { id, ox: w.x - n.x, oy: w.y - n.y, moved: false };
      nodeEl.classList.add('dragging');
      return;
    }
    // background → pan (or cancel an in-progress thread)
    if (linkFrom) { threadTarget.current = '__cancel__'; }
    panStart.current = { vx: viewRef.current.x, vy: viewRef.current.y, px: e.clientX, py: e.clientY };
    boardRef.current?.classList.add('panning');
  };

  const onPointerMoveBoard = (e: React.PointerEvent) => {
    if (pointers.current.has(e.pointerId))
      pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });

    // pinch zoom (two fingers)
    if (pointers.current.size >= 2) {
      const pts = [...pointers.current.values()];
      const d = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
      const midX = (pts[0].x + pts[1].x) / 2;
      const midY = (pts[0].y + pts[1].y) / 2;
      if (pinchDist.current != null) {
        zoomAt(d / pinchDist.current, midX, midY);
      }
      pinchDist.current = d;
      panStart.current = null;
      dragNode.current = null;
      return;
    }

    if (dragNode.current) {
      const w = screenToWorld(e.clientX, e.clientY);
      const d = dragNode.current;
      const nx = Math.round(w.x - d.ox);
      const ny = Math.round(w.y - d.oy);
      if (!d.moved && Math.hypot(nx - cRef.current.nodes[d.id].x, ny - cRef.current.nodes[d.id].y) > 2) {
        d.moved = true;
        setSel({ type: 'node', id: d.id });
      }
      setC((prev) => {
        const n = prev.nodes[d.id];
        if (!n || (n.x === nx && n.y === ny)) return prev;
        return { ...prev, nodes: { ...prev.nodes, [d.id]: { ...n, x: nx, y: ny } } };
      });
      return;
    }
    if (linkFrom) {
      const el = (document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null)?.closest('.node') as HTMLElement | null;
      threadTarget.current = el?.dataset.id ?? null;
    }

    if (panStart.current) {
      const p = panStart.current;
      applyTransform({ ...viewRef.current, x: p.vx + (e.clientX - p.px), y: p.vy + (e.clientY - p.py) });
    }
  };

  const endPointer = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinchDist.current = null;
    boardRef.current?.classList.remove('panning');

    // finishing a pulled red thread
    if (linkFrom) {
      const t = threadTarget.current;
      threadTarget.current = null;
      panStart.current = null;
      if (t === '__cancel__') { setLinkFrom(null); setHint(null); return; }
      if (pathMode !== 'off') { if (t && t !== '__cancel__') void handlePathTap(t); setHint(pathMode === 'pickA' ? 'Путь: ткни ПЕРВУЮ карточку' : null); return; } // [PATCH-3b]
      if (t && t !== linkFrom) tapNode(t);
      return;
    }
    threadTarget.current = null;

    if (dragNode.current) {
      const d = dragNode.current;
      dragNode.current = null;
      const el = boardRef.current?.querySelector(`.node[data-id="${d.id}"]`);
      el?.classList.remove('dragging');
      if (!d.moved) {
        if (pathMode !== 'off') { void handlePathTap(d.id); return; } // [PATCH-3b] path picking mode
        if (sel?.type === 'node' && sel.id === d.id && inspectorOpen) setInspectorOpen(false);
        else { setSel({ type: 'node', id: d.id }); setInspectorOpen(true); }
      }
    } else if (panStart.current) {
      const moved = Math.hypot(e.clientX - panStart.current.px, e.clientY - panStart.current.py);
      if (moved < 5) { setSel(null); setInspectorOpen(false); }
    }
    panStart.current = null;
  };

  // native listener: React attaches wheel as passive, which blocks preventDefault
  useEffect(() => {
    const el = boardRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const factor = Math.pow(1.0015, -e.deltaY);
      zoomAt(factor, e.clientX, e.clientY);
    };
    el.addEventListener('wheel', handler, { passive: false });
    return () => el.removeEventListener('wheel', handler);
  }, [zoomAt]);

  /* ---------- node tap in link mode ---------- */
  const tapNode = (id: string) => {
    if (!linkFrom) {
      setLinkFrom(id);
      setHint('Тяни красную нить: выбери вторую карточку');
      return;
    }
    if (linkFrom === id) { setLinkFrom(null); setHint(null); return; }
    const a = cRef.current.nodes[linkFrom];
    const b = cRef.current.nodes[id];
    const dup = Object.values(cRef.current.links).find(
      (l) => (l.from === a.id && l.to === b.id) || (l.from === b.id && l.to === a.id),
    );
    if (dup) { setSel({ type: 'link', id: dup.id }); setInspectorOpen(true); setLinkFrom(id); setHint('Такая нить уже есть — открыл её. Ткни другую карточку для новой'); return; }
    const link = newLink({ from: a.id, to: b.id, relation: '' });
    setC((prev) => ({ ...prev, links: { ...prev.links, [link.id]: link } }));
    setSel({ type: 'link', id: link.id });
    setInspectorOpen(true);
    // stay in thread mode, anchored on the card we just connected to — chains feel faster
    setLinkFrom(b.id);
    setHint('Нить цела. Ткни следующую карточку или Esc, чтобы закончить');
  };

  /* ---------- mutations ---------- */
  const addNode = (kind: NodeKind, title: string, value?: string) => {
    const center = screenToWorld(
      (boardRef.current!.clientWidth) / 2,
      (boardRef.current!.clientHeight) / 2,
    );
    const jitter = () => (Math.random() - 0.5) * 160;
    const n = newNode({ kind, title, value, x: Math.round(center.x + jitter()) - NODE_W / 2, y: Math.round(center.y + jitter()) - NODE_H / 2 });
    setC((prev) => ({ ...prev, nodes: { ...prev.nodes, [n.id]: n } }));
    setSel({ type: 'node', id: n.id });
    setInspectorOpen(true);
  };

  const updateNode = (id: string, patch: Partial<FactNode>) =>
    setC((prev) => {
      const n = prev.nodes[id];
      if (!n) return prev;
      return { ...prev, nodes: { ...prev.nodes, [id]: { ...n, ...patch, updatedAt: Date.now() } } };
    });

  const deleteNode = (id: string) =>
    setC((prev) => {
      const nodes = { ...prev.nodes }; delete nodes[id];
      const links = { ...prev.links };
      for (const l of Object.values(links)) if (l.from === id || l.to === id) delete links[l.id];
      return { ...prev, nodes, links };
    });

  const updateLink = (id: string, patch: Partial<FactLink>) =>
    setC((prev) => {
      const l = prev.links[id];
      if (!l) return prev;
      return { ...prev, links: { ...prev.links, [id]: { ...l, ...patch } } };
    });

  const deleteLink = (id: string) =>
    setC((prev) => {
      const links = { ...prev.links }; delete links[id];
      return { ...prev, links };
    });

  /* ---------- [PATCH-2b] applying backend results to the board ----------- */
  /** Insert wire-nodes/links into the case + immediate saveCase (PATCH-5). */
  /** КОММИТ 11, п.1: нормализованный ключ сущности kind:value для entity resolution. */
  const normalizeKey = (kind: string, value: string): string => `${kind}:${value.toLowerCase().trim()}`;

  /** КОММИТ 11, п.3: каскад found-событий — применяем не быстрее, чем раз в 150ms (граф растёт визуально). */
  const cascadeGate = useCallback((): Promise<void> => {
    const st = streamRef.current;
    const wait = Math.max(0, st.last + CASCADE_MS - Date.now());
    st.last = Date.now() + wait;
    if (wait > 0) setWsState('streaming');
    return wait > 0 ? new Promise((res) => { window.setTimeout(res, wait); }) : Promise.resolve();
  }, [CASCADE_MS]);

  const applyApiResults = useCallback(
    async (freshNodes: ApiFactNode[], freshLinks: ApiFactLink[]) => {
      const cur = cRef.current;
      // КОММИТ 11, п.1: карта нормализованных ключей -> id существующих нод (без дублей на доске)
      const byKey = new Map<string, string>();
      for (const n of Object.values(cur.nodes)) {
        if (n.mergedInto) continue;                    // каноническая нода — это не она
        byKey.set(normalizeKey(n.kind, n.value ?? n.title), n.id);
      }
      let cx = 0, cy = 0, cnt = 0;
      for (const l of freshLinks) {
        const a = cur.nodes[l.from], b = cur.nodes[l.to];
        if (a) { cx += a.x; cy += a.y; cnt++; } else if (b) { cx += b.x; cy += b.y; cnt++; }
      }
      const center = cnt ? { x: cx / cnt, y: cy / cnt } : { x: 0, y: 0 };

      const addedNodes: FactNode[] = [];
      const mergedNodes: FactNode[] = [];              // дубли, ставшие alias'ами
      const retarget = new Map<string, string>();      // raw id -> канонический id на доске
      freshNodes.forEach((raw, i) => {
        if (cur.nodes[raw.id]) { retarget.set(raw.id, raw.id); return; } // уже на доске
        const key = normalizeKey(raw.kind, raw.value ?? '');
        const canonId = key ? byKey.get(key) : undefined;
        if (canonId) {
          // КОММИТ 11, п.1: дубль НЕ создаём; притягиваем нить к существующей ноде, помечаем ПЕРЕСЕЧЕНИЕ
          retarget.set(raw.id, canonId);
          const canon = cur.nodes[canonId];
          if (canon && !canon.intersection) {
            mergedNodes.push({ ...canon, intersection: true, updatedAt: Date.now() });
          }
          return;
        }
        if (raw.id) byKey.set(key, raw.id);            // будущая нода займёт ключ
        const n = apiNodeToFactNode(raw);
        if (!raw.x && !raw.y) {
          const angle = (2 * Math.PI * i) / Math.max(freshNodes.length, 1) - Math.PI / 2;
          n.x = Math.round(center.x + Math.cos(angle) * ENRICH_RADIUS) - NODE_W / 2;
          n.y = Math.round(center.y + Math.sin(angle) * ENRICH_RADIUS) - NODE_H / 2;
        }
        spawnedRef.current.add(n.id);                  // CSS spawn animation
        addedNodes.push(n);
      });
      const resolve = (id: string) => retarget.get(id) ?? id;
      const validIds = new Set([
        ...Object.keys(cur.nodes),
        ...addedNodes.map((n) => n.id),
        ...mergedNodes.map((n) => n.id),
      ]);
      const addedLinks: FactLink[] = freshLinks
        .map((l) => ({ ...l, from: resolve(l.from), to: resolve(l.to) }))
        .filter((l) => validIds.has(l.from) && validIds.has(l.to) && l.from !== l.to && !cur.links[l.id])
        .map(apiLinkToFactLink);

      if (!addedNodes.length && !addedLinks.length && !mergedNodes.length) return false;
      setC((prev) => {
        const nodes = { ...prev.nodes };
        for (const n of addedNodes) nodes[n.id] = n;
        for (const n of mergedNodes) nodes[n.id] = n;  // чип ПЕРЕСЕЧЕНИЕ на канонической ноде
        const links = { ...prev.links };
        for (const l of addedLinks) links[l.id] = l;
        return { ...prev, nodes, links, updatedAt: Date.now() };
      });
      // КОММИТ 11, п.2: пересечение двух источников на одной ноде — тост
      for (const n of mergedNodes) {
        showToast(`ПЕРЕСЕЧЕНИЕ: ${n.title} — данные из второго источника`);
      }
      // [PATCH-5] persist to IndexedDB right away so F5 does not eat the probe
      const next: InvestigationCase = {
        ...cRef.current,
        nodes: { ...cRef.current.nodes },
        links: { ...cRef.current.links },
      };
      for (const n of addedNodes) next.nodes[n.id] = n;
      for (const n of mergedNodes) next.nodes[n.id] = n;
      for (const l of addedLinks) next.links[l.id] = l;
      try { await saveCase({ ...next, view: viewRef.current }); } catch { /* noop */ }
      return true;
    },
    [],
  );

  /** Double-click on a card / "Enrich" button — HTTP probe of one node. */
  const runEnrich = useCallback(
    async (nodeId: string) => {
      if (enrichingId) return;
      const node = cRef.current.nodes[nodeId];
      if (!node) return;
      setEnrichingId(nodeId);
      try {
        const res = await enrichNode(node);
        setLastProbe({ engine: res.engine, ms: res.took_ms }); // статус-бар: LAST/engine
        // ЭТАП 7: проставляем движок каждой ноде — для бейджа «СИМУЛЯЦИЯ» при engine=mock.
        const applied = await applyApiResults(res.nodes.map((n) => stampEngine(n, res.engine)), res.links);
        showToast(applied
          ? `Пробив ${KINDS[node.kind].short}: +${res.nodes.length} фактов (${res.engine})`
          : 'Новых фактов не найдено');
      } catch (e) {
        const err = e as EnrichError;
        if (err.status === 402 && user && onPaymentRequired) onPaymentRequired(user);
        showToast(err.status === 402
          ? 'Кредиты закончились — пополните баланс'
          : err.status === 401
            ? 'Нужна авторизация (войдите в аккаунт)'
            : `Ошибка пробива: ${String(err.message).slice(0, 80)}`);
      } finally {
        setEnrichingId(null);
      }
    },
    [enrichingId, applyApiResults, showToast, user, onPaymentRequired],
  );

  /** Recursive graph expansion over WebSocket (STAGE 3 spider). */
  const runSpider = useCallback(
    async (startNodeId: string, depth = 2) => {
      if (spiderActive) return;
      setSpiderActive(true);
      setHint('Спайдер раскручивает граф…');
      const graph = {
        nodes: Object.values(cRef.current.nodes),
        links: Object.values(cRef.current.links),
      };
      let found = 0;
      setWsState('connecting');
      // ЭТАП 7: движок последнего done-события — для бейджа «СИМУЛЯЦИЯ» на спайдер-нодах.
      let lastEngine: 'mock' | 'live' = 'live';
      try {
        await spiderRun(graph, startNodeId, depth, async (ev: SpiderEvent) => {
          if (ev.status === 'spider_started' || ev.status === 'enriching') setWsState('open');
          if (ev.status === 'found' && (ev.node || ev.link)) {
            await cascadeGate();                       // КОММИТ 11, п.3: визуальный каскад 150ms
            const nodes = ev.node ? [stampEngine(ev.node, lastEngine)] : [];
            const links = ev.link ? [ev.link] : [];
            if (await applyApiResults(nodes, links)) found++;
          } else if (ev.status === 'enriching' && ev.value) {
            setHint(`Спайдер [${ev.depth}]: ${ev.value}`);
          } else if (ev.status === 'done' || ev.status === 'spider_done') {
            setWsState((ws) => (ws === 'streaming' ? 'open' : ws)); // КОММИТ 11: конец каскада
            if (ev.engine) { lastEngine = ev.engine; setLastProbe((p) => ({ ...p, engine: ev.engine as 'mock' | 'live' })); }
          }
        });
        setHint(null);
        showToast(`Раскрутка завершена: применено пакетов: ${found}`);
      } catch (e) {
        setHint(null);
        showToast(`Спайдер остановлен: ${(e as Error).message.slice(0, 60)}`);
      } finally {
        setWsState('closed');
        setSpiderActive(false);
      }
    },
    [spiderActive, applyApiResults, showToast],
  );

  /* ref на актуальную runSpider — для автозапуска из useEffect (quick-seed) без циклов зависимостей */
  const runSpiderRef = useRef<typeof runSpider | null>(null);
  runSpiderRef.current = runSpider;

  /** Shortest path between two cards -> networkx on backend -> red highlight. */
  const handlePathTap = useCallback(
    async (id: string) => {
      if (pathMode === 'pickA') { setPathA(id); setPathMode('pickB'); setHint('Путь: ткни КОНЕЧНУЮ карточку'); return; }
      if (pathMode === 'pickB' && pathA && id !== pathA) {
        setPathMode('off'); setHint(null);
        try {
          const graph = { nodes: Object.values(cRef.current.nodes), links: Object.values(cRef.current.links) };
          const r = await findPath(graph, pathA, id);
          if (r.found) {
            setPathIds({ nodes: r.path_nodes.map((n) => n.id), links: r.path_links.map((l) => l.id) });
            showToast(`Путь найден: ${r.hops} переходов`);
          } else { showToast('Путь между карточками не найден'); setPathIds({ nodes: [], links: [] }); }
        } catch (e) {
          showToast(`Путь: ${String((e as Error).message).slice(0, 60)}`);
        }
      }
    },
    [pathMode, pathA, showToast],
  );

  const downloadExport = useCallback(async () => {
    try {
      const blob = await exportGraph({ nodes: Object.values(cRef.current.nodes), links: Object.values(cRef.current.links) }, 'graphml');
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'vilka_graph.graphml'; a.click();
      URL.revokeObjectURL(url);
      showToast('GraphML выгружен (Maltego/Gephi)');
    } catch (e) { showToast(`Экспорт: ${(e as Error).message.slice(0, 60)}`); }
  }, [showToast]);

  /* ---------- keyboard shortcuts ---------- */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (e.key === 'n' || e.key === 'т') { e.preventDefault(); setShowAdd(true); }
      if (e.key === 'f' || e.key === 'а') { e.preventDefault(); setShowSearch(true); }
      if (e.key === 'l' || e.key === 'д') {
        e.preventDefault();
        if (sel?.type === 'node') { setLinkFrom(sel.id); setHint('Тяни красную нить: выбери вторую карточку'); }
      }
      if (e.key === 'Escape') { setLinkFrom(null); setHint(null); setShowSearch(false); setInspectorOpen(false); }
      if ((e.key === 'Delete' || e.key === 'Backspace') && sel) {
        e.preventDefault();
        if (sel.type === 'node') deleteNode(sel.id); else deleteLink(sel.id);
        setSel(null); setInspectorOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [sel]);

  /* ---------- search results ---------- */
  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return Object.values(c.nodes)
      .filter((n) =>
        n.title.toLowerCase().includes(q) ||
        (n.value ?? '').toLowerCase().includes(q) ||
        (n.notes ?? '').toLowerCase().includes(q) ||
        n.tags.some((t) => t.toLowerCase().includes(q)),
      )
      .slice(0, 30);
  }, [query, c.nodes]);

  /* ---------- derived selection data ---------- */
  const selNode = sel?.type === 'node' ? c.nodes[sel.id] : undefined;
  const selLink = sel?.type === 'link' ? c.links[sel.id] : undefined;

  const nodeLinks = useMemo(() => {
    if (!selNode) return [];
    return Object.values(c.links).filter((l) => l.from === selNode.id || l.to === selNode.id);
  }, [c.links, selNode]);

  const timeline = useMemo(() => {
    const items = Object.values(c.nodes)
      .filter((n) => n.kind === 'event' || n.conf >= 2)
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, 40);
    return items;
  }, [c.nodes]);

  const stats = useMemo(() => {
    const nodes = Object.values(c.nodes);
    const confirmed = nodes.filter((n) => n.conf >= 2).length;
    const withSource = nodes.filter((n) => n.sources.length > 0).length;
    return { total: nodes.length, links: Object.keys(c.links).length, confirmed, withSource };
  }, [c]);

  /* ---------- thread geometry ---------- */
  const nodeCenter = (n: FactNode) => ({ x: n.x + NODE_W / 2, y: n.y + NODE_H / 2 });

  const threads = Object.values(c.links).map((l) => {
    const a = c.nodes[l.from], b = c.nodes[l.to];
    if (!a || !b) return null;
    const p1 = nodeCenter(a), p2 = nodeCenter(b);
    const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
    const dx = p2.x - p1.x, dy = p2.y - p1.y;
    const len = Math.hypot(dx, dy) || 1;
    // sag like a real thread between two pins
    const sag = Math.min(46, len * 0.14);
    const cx = mx - (dy / len) * 0, cy = my + sag;
    const d = `M ${p1.x} ${p1.y} Q ${cx} ${cy} ${p2.x} ${p2.y}`;
    return { l, d, lx: cx, ly: (my + cy) / 2 };
  }).filter(Boolean) as { l: FactLink; d: string; lx: number; ly: number }[];

  const statusOptions = (Object.keys(STATUS_META) as CaseStatus[]).map((s) => ({ v: s, label: STATUS_META[s].label }));

  /* live red-thread preview while dragging between cards */
  const [mouseWorld, setMouseWorld] = useState<{ x: number; y: number } | null>(null);
  useEffect(() => {
    if (!linkFrom) { setMouseWorld(null); setGhostPt(null); return; }
    const move = (e: PointerEvent) => {
      const w = screenToWorld(e.clientX, e.clientY);
      setMouseWorld(w);
      setGhostPt(w);
    };
    window.addEventListener('pointermove', move);
    return () => window.removeEventListener('pointermove', move);
  }, [linkFrom, screenToWorld]);

  let liveThread: { d: string; ex: number; ey: number } | null = null;
  if (linkFrom && ghostPt) {
    const src = c.nodes[linkFrom];
    if (src) {
      const p1 = nodeCenter(src);
      const tgtId = threadTarget.current;
      const tgt = tgtId && tgtId !== '__cancel__' ? c.nodes[tgtId] : null;
      const p2 = tgt ? nodeCenter(tgt) : ghostPt;
      const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
      const len = Math.hypot(p2.x - p1.x, p2.y - p1.y) || 1;
      const sag = Math.min(40, len * 0.12);
      liveThread = { d: `M ${p1.x} ${p1.y} Q ${mx} ${my + sag} ${p2.x} ${p2.y}`, ex: p2.x, ey: p2.y };
    }
  }

  return (
    <div className="app">
      <div className="topbar">
        <button className="btn ghost small" onClick={() => { saveCase({ ...cRef.current, view: viewRef.current }); onExit(); }}>← Дела</button>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 15, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {c.name}{c.codename ? <span className="mono muted"> «{c.codename}»</span> : null}
          </div>
        </div>
        <div className="spacer" />
        <div className="chip mono">{stats.total} узлов · {stats.links} нитей</div>
        {/* ЭТАП 6/7: баланс кредитов + «Пополнить» + «Выйти» */}
        {user != null && (
          <>
            <div className="chip mono" title={`вход: ${user.email}`}><Icon d={ICONS.bolt} /> CR: {user.credits_balance}</div>
            {onTopUp && (
              <button className="btn small" onClick={onTopUp} title="Создать крипто-инвойс (+1000 кредитов)">Пополнить</button>
            )}
            {onLogout && (
              <button className="btn ghost small" onClick={onLogout} title="Выйти из аккаунта">Выйти</button>
            )}
          </>
        )}
        {/* ЭТАП 9 коммит 4: тем-свитчер */}
        <div className="theme-switch">
          <button className="chip mono" style={{ cursor: 'pointer', background: 'transparent' }}
            onClick={() => setThemeMenuOpen((o) => !o)} title="Сменить тему">
            THEME: {theme.toUpperCase()}
          </button>
          {themeMenuOpen && (
            <div className="theme-menu">
              {THEMES.map((t) => (
                <button key={t} className={t === theme ? 'on' : ''} onClick={() => { setTheme(t); setThemeMenuOpen(false); }}>
                  {t.toUpperCase()}
                </button>
              ))}
            </div>
          )}
        </div>
        <button className="btn small" onClick={() => setCaseModal(true)}>Дело</button>
      </div>

      <div className="board-wrap" ref={wrapRef}>
        <div
          className={`board${linkFrom ? ' linking' : ''}`}
          ref={boardRef}
          onPointerDown={onPointerDownBoard}
          onPointerMove={onPointerMoveBoard}
          onPointerUp={endPointer}
          onPointerCancel={endPointer}
          onDoubleClick={(e) => {
            const t = e.target as HTMLElement;
            const nd = t.closest('.node') as HTMLElement | null;
            if (nd?.dataset.id) runEnrich(nd.dataset.id); // [PATCH-3] dblclick = probe
            else setShowAdd(true);
          }}
        >
          <svg className="threads" ref={svgRef} width="1" height="1" style={{ position: 'absolute', left: 0, top: 0 }}>
            <g>
              {threads.map(({ l, d, lx, ly }) => (
                <g key={l.id}>
                  <path
                    className="thread-hit"
                    d={d}
                    onPointerDown={(e) => { e.stopPropagation(); setSel({ type: 'link', id: l.id }); setInspectorOpen(true); }}
                  />
                  <path className={`thread ${l.strength}${sel?.type === 'link' && sel.id === l.id ? ' selected' : ''}${pathIds.links.includes(l.id) ? ' path-red' : ''}`} d={d} />
                  {l.relation && l.strength !== 'weak' && (
                    <text className="thread-label" x={lx} y={ly} textAnchor="middle">{l.relation}</text>
                  )}
                  <circle className="pin-dot" cx={nodeCenter(c.nodes[l.from]).x} cy={nodeCenter(c.nodes[l.from]).y} r={2.5} />
                  <circle className="pin-dot" cx={nodeCenter(c.nodes[l.to]).x} cy={nodeCenter(c.nodes[l.to]).y} r={2.5} />
                </g>
              ))}
            </g>
          </svg>

          {linkFrom && liveThread && (
            <svg className="threads" style={{ position: 'absolute', left: 0, top: 0 }} width="1" height="1">
              <path className="thread strong" strokeDasharray="6 4" opacity={0.85} d={liveThread.d} />
              <circle className="pin-dot" cx={liveThread.ex} cy={liveThread.ey} r={3} />
            </svg>
          )}

          <div className="world" ref={worldRef}>
            {Object.values(c.nodes).map((n) => (
              <div
                key={n.id}
                data-id={n.id}
                className={`node kind-${n.kind}${sel?.type === 'node' && sel.id === n.id ? ' selected' : ''}${n.intersection ? ' intersected' : ''}${linkFrom === n.id ? ' linksource' : ''}${spawnedRef.current.delete(n.id) ? ' node-spawn' : ''}${enrichingId === n.id ? ' enriching' : ''}${pathIds.nodes.includes(n.id) ? ' on-path' : ''}`}
                style={{ left: n.x, top: n.y }}
              >
                <div className="head">
                  <span className="glyph">{KINDS[n.kind].glyph}</span>
                  <span className="kind">{KINDS[n.kind].short}</span>
                  {n.engine === 'mock' && (
                    <span className="sim-badge" title="Данные сгенерированы мок-генератором, а не реальным источником">СИМУЛЯЦИЯ</span>
                  )}
                  {/* КОММИТ 11, п.1-2: пересечение двух источников на одной сущности */}
                  {n.intersection && (
                    <span className="int-badge" title="На эту сущность указывают данные из двух и более источников">ПЕРЕСЕЧЕНИЕ</span>
                  )}
                </div>
                <div className="title">{n.title}</div>
                {/* ЭТАП10 коммит10, п.1: title === value -> не дублируем строку value, рендерим мета-строку */}
                {n.value && n.value.trim().toLowerCase() !== n.title.trim().toLowerCase()
                  ? <div className="value">{n.value}</div>
                  : n.value
                    ? <div className="value mono-meta">{KINDS[n.kind].label.toLowerCase()} · {new Date(n.createdAt).toISOString().slice(0, 10)}</div>
                    : null}
                <div className="foot">
                  {n.sources.length > 0 && <span className="src-count">SRC:{n.sources.length}</span>}
                  {n.tags.slice(0, 1).map((t) => <span key={t} className="tag">#{t}</span>)}
                  <span className="conf-bar" title={`достоверность: ${CONF[n.conf].label}`}>
                    {[0, 1, 2, 3].map((i) => (
                      <span key={i} className={`conf-cell${i <= n.conf ? ' filled' : ''}`} />
                    ))}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* tools */}
        <div className="board-tools">
          <button className="tool" title="Новая карточка (N)" onClick={() => setShowAdd(true)}><Icon d={ICONS.plus} /></button>
          <button
            className={`tool${linkFrom ? ' on' : ''}`}
            title="Красная нить (L)"
            onClick={() => {
              if (linkFrom) { setLinkFrom(null); setHint(null); }
              else if (sel?.type === 'node') { setLinkFrom(sel.id); setHint('Тяни красную нить: выбери вторую карточку'); }
              else setHint('Сначала выбери карточку, потом жми нить');
            }}
          ><Icon d={ICONS.link} /></button>
          <button className={`tool${showSearch ? ' on' : ''}`} title="Поиск по делу (F)" onClick={() => setShowSearch((s) => !s)}><Icon d={ICONS.search} /></button>
          <button className="tool" title="Всё дело на экране" onClick={fitAll}><Icon d={ICONS.frame} /></button>
          {/* [PATCH-6] OSINT panel: spider, path, export */}
          <button
            className={`tool${spiderActive ? ' on' : ''}`}
            disabled={spiderActive}
            title="Раскрутить граф от выбранной карточки (BFS)"
            onClick={() => {
              if (sel?.type === 'node') void runSpider(sel.id, 2);
              else showToast('Сначала выбери стартовую карточку');
            }}
          ><Icon d={ICONS.spider} /></button>
          <button
            className={`tool${pathMode !== 'off' ? ' on' : ''}`}
            title="Найти путь между двумя карточками"
            onClick={() => {
              if (pathMode === 'off') { setPathMode('pickA'); setPathIds({ nodes: [], links: [] }); setHint('Путь: ткни ПЕРВУЮ карточку'); }
              else { setPathMode('off'); setHint(null); }
            }}
          ><Icon d={ICONS.chain} /></button>
          <button className="tool" title="Экспорт графа в GraphML (Maltego/Gephi)" onClick={() => void downloadExport()}><Icon d={ICONS.download} /></button>
        </div>

        {showSearch && (
          <div className="search-panel">
            <input
              autoFocus
              placeholder="ник, почта, тег, заметка…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') setShowSearch(false); }}
            />
            {results.map((n) => (
              <div
                key={n.id}
                className="result"
                onClick={() => {
                  setSel({ type: 'node', id: n.id });
                  setInspectorOpen(true);
                  centerOn(n.x + NODE_W / 2, n.y + NODE_H / 2);
                  setShowSearch(false);
                }}
              >
                <div className="t">{KINDS[n.kind].glyph} {n.title}</div>
                <div className="k">{KINDS[n.kind].label}{n.value ? ' · ' + n.value : ''}</div>
              </div>
            ))}
            {query && !results.length && <div className="muted" style={{ padding: 8 }}>Пусто.</div>}
          </div>
        )}

        {hint && <div className="hint-banner">{hint}</div>}

        {/* ЭТАП10 коммит10, п.2: zoom-dock ВНУТРИ контейнера холста (absolute), не наезжает на инспектор */}
        <div className="zoom-dock">
          <button className="btn ghost small" onClick={() => zoomAt(1 / 1.2)}>−</button>
          <span className="mono zoom-val">{zoomPct}%</span>
          <button className="btn ghost small" onClick={() => zoomAt(1.2)}>+</button>
        </div>

        {/* inspector */}
        <aside className={`inspector${inspectorOpen && sel ? ' open' : ''}`}>
          {selNode && <NodeInspector
            node={selNode}
            links={nodeLinks}
            other={(id) => c.nodes[id]}
            onClose={() => setInspectorOpen(false)}
            onUpdate={(p) => updateNode(selNode.id, p)}
            onDelete={() => { deleteNode(selNode.id); setSel(null); setInspectorOpen(false); }}
            onSelectLink={(id) => setSel({ type: 'link', id })}
            onStartLink={() => { setLinkFrom(selNode.id); setHint('Тяни красную нить: выбери вторую карточку'); }}
            onEnrich={() => void runEnrich(selNode.id)}
            onSpider={() => void runSpider(selNode.id, 2)}
            enrichBusy={enrichingId === selNode.id}
            spiderBusy={spiderActive}
            onGoto={(id) => { const n2 = c.nodes[id]; if (n2) { setSel({ type: 'node', id }); centerOn(n2.x + NODE_W / 2, n2.y + NODE_H / 2); } }}
          />}
          {selLink && <LinkInspector
            link={selLink}
            a={c.nodes[selLink.from]}
            b={c.nodes[selLink.to]}
            onClose={() => setInspectorOpen(false)}
            onUpdate={(p) => updateLink(selLink.id, p)}
            onDelete={() => { deleteLink(selLink.id); setSel(null); setInspectorOpen(false); }}
            onSwap={() => updateLink(selLink.id, { from: selLink.to, to: selLink.from })}
          />}
          {!sel && !selLink && c.id && <BoardInspector
            c={c} timeline={timeline} stats={stats}
            onClose={() => setInspectorOpen(false)}
            onGotoNode={(id) => { const n = c.nodes[id]; setSel({ type: 'node', id }); centerOn(n.x + NODE_W / 2, n.y + NODE_H / 2); }}
          />}
        </aside>
      </div>

      {/* ЭТАП 9 коммит 4: статус-бар — WS/engine/latency/credits/theme */}
      <div className="board-status">
        <span>WS: {wsState === 'open' ? <b>CONNECTED</b> : wsState === 'streaming' ? <b className="amber">STREAMING</b> : wsState === 'connecting' ? 'CONNECTING' : 'IDLE'} <span className={`dot${wsState === 'open' ? '' : wsState === 'streaming' ? ' amber-dot' : ' off'}`}>●</span></span>
        <span>ENGINE: <b>{lastProbe.engine.toUpperCase()}</b></span>
        <span>LAST: <b>{lastProbe.ms || '—'}ms</b></span>
        <span>CR: <b>{user ? user.credits_balance : '—'}</b></span>
        <span>THEME: <b>{theme.toUpperCase()}</b></span>
        <span style={{ marginLeft: 'auto' }}>v0.9</span>
      </div>

      {showAdd && (
        <AddNodeModal onClose={() => setShowAdd(false)} onPick={(kind) => setShowAdd(false)} onCommit={addNode} />
      )}

      {toast && <div className="toast">{toast}</div>}

      {caseModal && (
        <Modal title="Дело" sub="метаданные расследования" onClose={() => setCaseModal(false)}>
          <div className="field"><label>Название</label>
            <input value={c.name} onChange={(e) => setC((p) => ({ ...p, name: e.target.value }))} /></div>
          <div className="field"><label>Позывной</label>
            <input value={c.codename ?? ''} placeholder="напр. СЕРОКРЫЛ" onChange={(e) => setC((p) => ({ ...p, codename: e.target.value }))} /></div>
          <div className="field"><label>Статус</label>
            <Seg value={c.status} options={statusOptions} onChange={(s) => setC((p) => ({ ...p, status: s }))} alt /></div>
          <div className="field"><label>Суть дела</label>
            <textarea value={c.summary ?? ''} placeholder="что ищем, по какой причине, рамки допустимого"
              onChange={(e) => setC((p) => ({ ...p, summary: e.target.value }))} /></div>
          <div className="actions">
            <span className="stamp" style={{ marginRight: 'auto' }}>изменения сохраняются сами</span>
            <button className="btn primary" onClick={() => setCaseModal(false)}>Готово</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ================= Add node modal ================= */
function AddNodeModal({
  onCommit, onClose,
}: {
  onPick: (k: NodeKind) => void;
  onClose: () => void;
  onCommit: (kind: NodeKind, title: string, value?: string) => void;
}) {
  const [kind, setKind] = useState<NodeKind>('person');
  const [title, setTitle] = useState('');
  const [value, setValue] = useState('');

  const commit = () => {
    if (!title.trim()) return;
    onCommit(kind, title.trim(), value.trim() || undefined);
    onClose();
  };

  return (
    <Modal title="Новая карточка" sub="факт, зацепка или сущность на доске" onClose={onClose}>
      <label>Тип</label>
      <KindPicker onPick={(k) => setKind(k)} />
      <div className="row" style={{ marginTop: 10 }}>
        <span className="chip on">{KINDS[kind].glyph} {KINDS[kind].label}</span>
        <span className="muted" style={{ fontSize: 12 }}>{KINDS[kind].hint}</span>
      </div>
      <div className="field"><label>Заголовок *</label>
        <input autoFocus value={title} placeholder={KINDS[kind].label} onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') commit(); }} /></div>
      <div className="field"><label>Значение (ник, номер, адрес…)</label>
        <input value={value} onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') commit(); }} /></div>
      <div className="actions">
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn primary" onClick={commit} disabled={!title.trim()}>Приколоть к доске</button>
      </div>
    </Modal>
  );
}

/* ================= Node inspector ================= */
function NodeInspector({
  node, links, other, onUpdate, onDelete, onClose, onSelectLink, onStartLink, onGoto,
  onEnrich, onSpider, enrichBusy, spiderBusy,
}: {
  node: FactNode;
  links: FactLink[];
  other: (id: string) => FactNode | undefined;
  onUpdate: (p: Partial<FactNode>) => void;
  onDelete: () => void;
  onClose: () => void;
  onSelectLink: (id: string) => void;
  onStartLink: () => void;
  onGoto: (id: string) => void;
  onEnrich?: () => void;
  onSpider?: () => void;
  enrichBusy?: boolean;
  spiderBusy?: boolean;
}) {
  const [tagDraft, setTagDraft] = useState('');
  const [srcTitle, setSrcTitle] = useState('');
  const [srcUrl, setSrcUrl] = useState('');

  const addTag = () => {
    const t = tagDraft.trim().replace(/^#/, '').toLowerCase();
    if (!t || node.tags.includes(t)) return;
    onUpdate({ tags: [...node.tags, t] });
    setTagDraft('');
  };

  const addSource = () => {
    if (!srcTitle.trim() && !srcUrl.trim()) return;
    onUpdate({
      sources: [...node.sources, {
        id: uid(),
        title: srcTitle.trim() || srcUrl.trim(),
        url: srcUrl.trim() || undefined,
        retrievedAt: Date.now(),
      }],
    });
    setSrcTitle(''); setSrcUrl('');
  };

  return (
    <>
      <header>
        <span style={{ color: 'var(--vermilion)', fontSize: 20 }}>{KINDS[node.kind].glyph}</span>
        <h3>{node.title}</h3>
        <button className="close-x" onClick={onClose}>×</button>
      </header>
      <div className="body">
        <div className="field"><label>Тип карточки</label>
          <select value={node.kind} onChange={(e) => onUpdate({ kind: e.target.value as NodeKind })}>
            {(Object.keys(KINDS) as NodeKind[]).map((k) => (
              <option key={k} value={k}>{KINDS[k].glyph}  {KINDS[k].label}</option>
            ))}
          </select>
        </div>
        <div className="field"><label>Заголовок</label>
          <input value={node.title} onChange={(e) => onUpdate({ title: e.target.value })} /></div>
        <div className="field"><label>Значение</label>
          <input className="mono" value={node.value ?? ''} placeholder="@handle, 79xx…, domain.tld"
            onChange={(e) => onUpdate({ value: e.target.value })} /></div>
        <div className="field"><label>Что это / комментарии</label>
          <textarea value={node.notes ?? ''} placeholder="откуда взялось, что проверено, что настораживает"
            onChange={(e) => onUpdate({ notes: e.target.value })} /></div>

        <div className="field"><label>Достоверность</label>
          <Seg<string>
            value={String(node.conf)}
            options={[0, 1, 2, 3].map((i) => ({ v: String(i), label: CONF[i as ConfLevel].label }))}
            onChange={(v) => onUpdate({ conf: Number(v) as ConfLevel })}
          />
        </div>

        <div className="field"><label>Теги</label>
          <div className="row">
            <input value={tagDraft} placeholder="tg-канал" onChange={(e) => setTagDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTag(); } }} />
            <button className="btn small" onClick={addTag}>+</button>
          </div>
          <div className="row" style={{ flexWrap: 'wrap', marginTop: 8 }}>
            {node.tags.map((t) => (
              <span key={t} className="chip on" style={{ cursor: 'pointer' }}
                onClick={() => onUpdate({ tags: node.tags.filter((x) => x !== t) })}
                title="убрать">#{t} ×</span>
            ))}
          </div>
        </div>

        <div className="field"><label>Источники (откуда это)</label>
          {node.sources.map((s) => (
            <div className="source-item" key={s.id}>
              <div className="grow">
                <div>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : s.title}</div>
                <div className="u">{s.retrievedAt ? fmtDateTime(s.retrievedAt) : ''}</div>
              </div>
              <button className="btn ghost small" title="удалить"
                onClick={() => onUpdate({ sources: node.sources.filter((x) => x.id !== s.id) })}>×</button>
            </div>
          ))}
          <input placeholder="название (скрин поста, база…)" value={srcTitle} onChange={(e) => setSrcTitle(e.target.value)} />
          <div className="row" style={{ marginTop: 6 }}>
            <input className="mono" placeholder="https://ссылка" value={srcUrl} onChange={(e) => setSrcUrl(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addSource(); } }} />
            <button className="btn small" onClick={addSource}>＋</button>
          </div>
        </div>

        <div className="field"><label>Нити ({links.length})</label>
          {links.map((l) => {
            const oid = l.from === node.id ? l.to : l.from;
            const o = other(oid);
            if (!o) return null;
            return (
              <div className="link-item" key={l.id} onClick={() => onSelectLink(l.id)}>
                <span className="rel">{l.from === node.id ? '→' : '←'} {l.relation || STRENGTH_META[l.strength].label}</span>
                <span className="lt">{KINDS[o.kind].glyph} {o.title}</span>
                <button className="btn ghost small" onClick={(e) => { e.stopPropagation(); onGoto(o.id); }}>→</button>
              </div>
            );
          })}
          {!links.length && <div className="muted" style={{ fontSize: 13 }}>Пока ни с чем не связано. Нажми «нить» на панели слева и ткни вторую карточку.</div>}
        </div>

        <div className="row" style={{ marginTop: 18 }}>
          <button className="btn" onClick={onStartLink}>НИТЬ ОТ СЮДА</button>
          <button className="btn danger" style={{ marginLeft: 'auto' }} onClick={onDelete}>Удалить</button>
        </div>
        {/* [PATCH-3] OSINT buttons: enrich & spider */}
        {(onEnrich || onSpider) && (
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn primary" onClick={onEnrich} disabled={enrichBusy}>
              {enrichBusy ? 'ПРОБИВАЕМ…' : 'ОБОГАТИТЬ'}
            </button>
            <button className="btn" style={{ marginLeft: 8 }} onClick={onSpider} disabled={spiderBusy || enrichBusy}>
              {spiderBusy ? 'РАСКРУТКА…' : 'РАСКРУТИТЬ (SPIDER)'}
            </button>
          </div>
        )}
        <div className="muted" style={{ fontSize: 11, marginTop: 14 }}>
          обновили {relTime(node.updatedAt)}
        </div>
      </div>
    </>
  );
}

/* ================= Link inspector ================= */
function LinkInspector({
  link, a, b, onUpdate, onDelete, onClose, onSwap,
}: {
  link: FactLink; a?: FactNode; b?: FactNode;
  onUpdate: (p: Partial<FactLink>) => void;
  onDelete: () => void; onClose: () => void; onSwap: () => void;
}) {
  return (
    <>
      <header>
        <span style={{ color: 'var(--red)', fontSize: 16, fontFamily: 'var(--font-mono)' }}>--</span>
        <h3>Красная нить</h3>
        <button className="close-x" onClick={onClose}>×</button>
      </header>
      <div className="body">
        <div className="row" style={{ margin: '12px 0' }}>
          <span className="chip">{a ? `${KINDS[a.kind].glyph} ${a.title}` : '?'}</span>
          <span className="muted">→</span>
          <span className="chip">{b ? `${KINDS[b.kind].glyph} ${b.title}` : '?'}</span>
          <button className="btn ghost small" onClick={onSwap} title="развернуть">⇄</button>
        </div>
        <div className="field"><label>Как связаны</label>
          <input value={link.relation} placeholder="владеет, переписывался, зарегистрирован на, один EXIF…"
            onChange={(e) => onUpdate({ relation: e.target.value })} /></div>
        <div className="field"><label>Сила связи</label>
          <Seg<LinkStrength>
            value={link.strength}
            options={(Object.keys(STRENGTH_META) as LinkStrength[]).map((s) => ({ v: s, label: STRENGTH_META[s].label }))}
            onChange={(s) => onUpdate({ strength: s })}
          />
        </div>
        <div className="field"><label>Почему так считаешь</label>
          <textarea value={link.note ?? ''} onChange={(e) => onUpdate({ note: e.target.value })} /></div>
        <div className="row" style={{ marginTop: 18 }}>
          <button className="btn danger" onClick={onDelete}>Перерезать нить</button>
        </div>
      </div>
    </>
  );
}

/* ================= Board overview (nothing selected) ================= */
function BoardInspector({
  c, timeline, stats, onClose, onGotoNode,
}: {
  c: InvestigationCase;
  timeline: FactNode[];
  stats: { total: number; links: number; confirmed: number; withSource: number };
  onClose: () => void;
  onGotoNode: (id: string) => void;
}) {
  return (
    <>
      <header>
        <h3>Обзор дела</h3>
        <button className="close-x" onClick={onClose}>×</button>
      </header>
      <div className="body">
        <div className="row" style={{ flexWrap: 'wrap', gap: 8, margin: '14px 0' }}>
          <span className="chip">карточек: {stats.total}</span>
          <span className="chip">нитей: {stats.links}</span>
          <span className="chip">подтверждено: {stats.confirmed}</span>
          <span className="chip">с источниками: {stats.withSource}</span>
        </div>
        {c.summary && <p className="dim">{c.summary}</p>}
        <div className="field"><label>Хронология (события и подтверждённое)</label></div>
        {timeline.map((n) => (
          <div className="timeline-item" key={n.id} style={{ cursor: 'pointer' }} onClick={() => onGotoNode(n.id)}>
            <span className="dot" />
            <div>
              <div>{KINDS[n.kind].glyph} {n.title}</div>
              <div className="muted" style={{ fontSize: 11 }}>{fmtDateTime(n.updatedAt)} · {CONF[n.conf].label}</div>
            </div>
          </div>
        ))}
        {!timeline.length && <div className="muted" style={{ fontSize: 13 }}>Пока пусто. Добавь карточку типа «Событие» — она попадёт сюда.</div>}
      </div>
    </>
  );
}
