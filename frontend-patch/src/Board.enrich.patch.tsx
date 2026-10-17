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
