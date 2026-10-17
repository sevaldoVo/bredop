/**
 * Board.tsx — патч интеграции с OSINT-бэкендом (ВИЛКА, ЭТАП 1 + ЭТАП 2).
 *
 * ВАЖНО: это референсная реализация под вашу текущую Board.tsx.
 * Структура (`c.nodes`, `c.links`, стейт через useReducer/useState) сохранена
 * так, как вы её описали. Вставьте помеченные блоки в свой файл:
 *
 *   [PATCH-1] импорты и константы — в шапку файла
 *   [PATCH-2] хук useEnrichment — внутрь компонента Board
 *   [PATCH-3] onDoubleClick + кнопка «Обогатить» — на ноду
 *   [PATCH-4] CSS-анимация появления — в ваш *.module.css / index.css
 *   [PATCH-5] (ЭТАП 2) сохранение кейса в IndexedDB после обогащения
 *
 * ЭТАП 2: FactLink использует поля `from`/`to` (как в types.ts и models.py).
 */

// ─── [PATCH-1] импорты ──────────────────────────────────────────────────────
import { useCallback, useRef, useState } from 'react';
import { enrichNode, type FactLink } from './api';
import type { FactNode } from './types';
// [PATCH-5] модуль персистентности из вашего проекта (idb-keyval):
import { saveCase } from './storage'; // ← путь под ваш проект

/** Радиус, на котором раскладываются новые ноды вокруг исходной. */
const ENRICH_RADIUS = 220;

interface EnrichState {
  pendingId: string | null; // id ноды, которую сейчас пробиваем
  error: string | null;
}

// ─── [PATCH-2] хук обогащения (внутри компонента <Board />) ─────────────────
//
// Допущение: у вас есть сеттер состояния графа вида
//   setGraph((g) => ({ ...g, nodes: [...], links: [...] }))
// Замените `setGraph` на свой способ мутации `c.nodes` / `c.links`.
//
// ЭТАП 2: добавлен коллбэк onSaved — вызывается ПОСЛЕ применения новых нод,
// с актуальным графом, чтобы Board мог сохранить кейс в IndexedDB.

function useEnrichment(
  setGraph: React.Dispatch<React.SetStateAction<{ nodes: FactNode[]; links: FactLink[] }>>,
  onSaved?: (graph: { nodes: FactNode[]; links: FactLink[] }) => void,
) {
  const [enrich, setEnrich] = useState<EnrichState>({ pendingId: null, error: null });
  // Ссылка на актуальные ноды, чтобы не тащить весь граф в deps.
  const nodesRef = useRef<FactNode[]>([]);

  const runEnrich = useCallback(
    async (node: FactNode) => {
      if (enrich.pendingId) return; // один пробив за раз
      setEnrich({ pendingId: node.id, error: null });
      try {
        const { nodes: fresh, links: freshLinks } = await enrichNode(node);

        // Раскладка новых нод по кругу вокруг исходной (если бэкенд не прислал x/y).
        const src = node;
        const placed: FactNode[] = fresh.map((n, i) => {
          if (n.x != null && n.y != null) return n;
          const angle = (2 * Math.PI * i) / Math.max(fresh.length, 1) - Math.PI / 2;
          return {
            ...n,
            x: (src.x ?? 0) + ENRICH_RADIUS * Math.cos(angle),
            y: (src.y ?? 0) + ENRICH_RADIUS * Math.sin(angle),
          };
        });

        let nextGraph: { nodes: FactNode[]; links: FactLink[] } | null = null;
        setGraph((g) => {
          // Идемпотентность: не добавляем ноду, которая уже есть на доске
          // (сверяем по kind+value, т.к. id у mock'а случайный).
          const seen = new Set(g.nodes.map((n) => `${n.kind}:${n.value.toLowerCase()}`));
          const addNodes = placed.filter((n) => !seen.has(`${n.kind}:${n.value.toLowerCase()}`));
          const addedIds = new Set(addNodes.map((n) => n.id));
          // Оставляем только связи, оба конца которых теперь живут на доске.
          const aliveIds = new Set([...g.nodes.map((n) => n.id), ...addedIds]);
          // ЭТАП 2: у FactLink поля from/to (не source/target).
          const addLinks = freshLinks.filter(
            (l) => (addedIds.has(l.from) || l.from === src.id) && aliveIds.has(l.to) && aliveIds.has(l.from),
          );
          nextGraph = { nodes: [...g.nodes, ...addNodes], links: [...g.links, ...addLinks] };
          return nextGraph;
        });

        // [PATCH-5] Персистентность: сохраняем обновлённый кейс в IndexedDB,
        // иначе пробив исчезнет при перезагрузке страницы.
        // `currentCaseId` — id активного кейса из вашего Board-стейта;
        // сигнатура saveCase(caseId, graph) повторяет ваш storage.ts.
        if (nextGraph && onSaved) onSaved(nextGraph);
      } catch (e) {
        setEnrich((s) => ({ ...s, error: e instanceof Error ? e.message : 'enrich failed' }));
      } finally {
        setEnrich((s) => ({ pendingId: null, error: s.error }));
      }
    },
    [enrich.pendingId, setGraph, onSaved],
  );

  return { enrich, runEnrich, nodesRef };
}

// ─── [PATCH-5] wiring в компоненте Board (рядом с объявлением хука) ─────────
/*
const caseIdRef = useRef<string>(activeCase.id); // ваш активный кейс

const { enrich, runEnrich } = useEnrichment(setGraph, (graph) => {
  // fire-and-forget: saveCase уже async внутри (idb-keyval set)
  void saveCase(caseIdRef.current, { ...activeCase, nodes: graph.nodes, links: graph.links })
    .catch((err) => console.warn('[vilka] saveCase failed:', err));
});
*/

// ─── [PATCH-3] вешаем на ноду внутри JSX рендера нод ────────────────────────
/*
{c.nodes.map((node) => (
  <div
    key={node.id}
    className={`vilka-node appear ${enrich.pendingId === node.id ? 'pulse' : ''}`}
    style={{ left: node.x, top: node.y }}
    // Двойной клик = быстрый пробив:
    onDoubleClick={() => void runEnrich(node)}
  >
    <span className="vilka-node__label">{node.label ?? node.value}</span>

    // Явная кнопка — надёжнее двойного клика (не конфликтует с drag):
    <button
      className="vilka-node__enrich"
      title="Обогатить (OSINT-пробив)"
      disabled={enrich.pendingId !== null}
      onClick={(e) => {
        e.stopPropagation();
        void runEnrich(node);
      }}
    >
      {enrich.pendingId === node.id ? '⏳' : '🔍'}
    </button>
  </div>
))}

{enrich.error && (
  <div className="vilka-toast vilka-toast--error" role="alert">
    {enrich.error}
  </div>
)}
*/

// ─── [PATCH-4] анимация появления новых нод (CSS) ───────────────────────────
/*
@keyframes vilka-node-in {
  0%   { opacity: 0; transform: scale(0.6); filter: blur(4px); }
  60%  { opacity: 1; transform: scale(1.06); filter: blur(0); }
  100% { opacity: 1; transform: scale(1); }
}
.vilka-node.appear {
  animation: vilka-node-in 420ms cubic-bezier(0.2, 0.9, 0.3, 1.2) both;
}
@keyframes vilka-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(255, 107, 107, 0.5); }
  50%      { box-shadow: 0 0 0 10px rgba(255, 107, 107, 0); }
}
.vilka-node.pulse { animation: vilka-pulse 1s ease-in-out infinite; }
*/

export { useEnrichment };

// ===========================================================================
// ЭТАП 3 — [PATCH-6]: Spider и поиск пути
// (добавлять в тот же Board.tsx, что и PATCH-1..5)
// ===========================================================================

// --- [PATCH-6a] доп. импорты (расширить строку импорта из './api') ---------
// import { enrichNode, spiderRun, findPath } from './api';
// import type { SpiderEvent, PathResult } from './api';

// --- [PATCH-6b] состояние ---------------------------------------------------
// const [spideringId, setSpideringId] = useState<string | null>(null);
// const [pathSel, setPathSel] = useState<string[]>([]);   // id выбранных нод
// const [pathIds, setPathIds] = useState<{ nodes: Set<string>; links: Set<string> }>({ nodes: new Set(), links: new Set() });

// --- [PATCH-6c] обработчики (рядом с runEnrich) ----------------------------

async function runSpider(node: FactNode) {
  if (spideringId) return;                      // одна раскрутка за раз
  setSpideringId(node.id);
  const freshNodes: Map<string, FactNode> = new Map();
  const freshLinks: Map<string, FactLink> = new Map();
  try {
    await spiderRun(
      { nodes: c.nodes, links: c.links },
      node.id,
      2,                                          // depth по ТЗ
      (ev) => {
        if (ev.status === 'found' && ev.node) freshNodes.set(ev.node.id, ev.node);
        if (ev.status === 'found' && ev.link) freshLinks.set(ev.link.id, ev.link);
        // каждое событие — инкрементально на доску (анимация как у enrich):
        if (freshNodes.size || freshLinks.size) {
          setGraph((g) => ({
            ...g,
            nodes: applyLayout([...g.nodes, ...[...freshNodes.values()].filter((n) => !g.nodes.some((m) => m.id === n.id))], g),
            links: [...g.links, ...[...freshLinks.values()].filter((l) => !g.links.some((m) => m.id === l.id))],
          }));
          freshNodes.clear();
          freshLinks.clear();
        }
      },
    );
    onSaved?.({ nodes: c.nodes, links: c.links }); // [PATCH-5] persistence в IndexedDB
  } catch (e) {
    console.error('spider failed', e);
  } finally {
    setSpideringId(null);
  }
}

async function runPathSearch() {
  if (pathSel.length !== 2) return;
  try {
    const res: PathResult = await findPath({ nodes: c.nodes, links: c.links }, pathSel[0], pathSel[1]);
    if (!res.found) { alert('Путь не найден'); setPathIds({ nodes: new Set(), links: new Set() }); return; }
    setPathIds({
      nodes: new Set(res.path_nodes.map((n) => n.id)),
      links: new Set(res.path_links.map((l) => l.id)),
    });
  } catch (e) {
    console.error('path failed', e);
  } finally {
    setPathSel([]);
  }
}

// --- [PATCH-6d] контекстное меню ноды: добавить две кнопки рядом с «🔍 Обогатить» --
// <button onClick={() => void runSpider(node)} disabled={!!spideringId}>
//   {spideringId === node.id ? '🕷 Раскручиваю…' : '🕷 Раскрутить (Spider)'}
// </button>
// <button onClick={() => setPathSel((s) => s.includes(node.id) ? s.filter((x) => x !== node.id) : [...s, node.id].slice(-2))}>
//   🔗 Выбрать для пути ({pathSel.includes(node.id) ? '✓' : `${pathSel.length}/2`})
// </button>
// Когда выбрано ровно 2 — показать плавающую кнопку:
// {pathSel.length === 2 && <button className="vilka-path-go" onClick={() => void runPathSearch()}>🔗 Найти путь</button>}

// --- [PATCH-6e] отрисовка связей: подсветка пути красным --------------------
// В рендере каждой нити (SVG line/path):
//   const onPath = pathIds.links.has(link.id);
//   stroke={onPath ? '#ff2d55' : '#8884f2'}
//   strokeWidth={onPath ? 3 : 1.5}
//   style={{ transition: 'stroke .3s, filter .3s', filter: onPath ? 'drop-shadow(0 0 6px #ff2d55)' : undefined }}
// И ноды на пути чуть увеличиваем/подсвечиваем рамкой:
//   className={`vilka-node ${pathIds.nodes.has(node.id) ? 'vilka-node--path' : ''}`}

// --- [PATCH-6f] CSS (добавить в index.css / стили доски) --------------------
/*
.vilka-node--path { outline: 2px solid #ff2d55; outline-offset: 2px; box-shadow: 0 0 14px rgba(255,45,85,.7); }
.vilka-path-go { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); z-index: 50; }
*/
