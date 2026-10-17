"""Spider-движок «ВИЛКИ» (ЭТАП 3): рекурсивная раскрутка графа.

Модель:
  * Клиент подключается к WebSocket /ws/enrich и шлёт
    {"action": "spider", "graph": {nodes, links}, "start_node_id": "...", "depth": 2}
  * Бэкенд запускает asyncio.create_task, который в BFS-порядке пробивает
    ноды через enrich() и стримит прогресс в сокет:
        {"status":"spider_started","run_id":"...","frontier":N}
        {"status":"enriching","node_id":"...","kind":"phone","value":"+7...","depth":1}
        {"status":"found","node":{...}} / {"status":"found","link":{...}}
        {"status":"skipped","node_id":"...","reason":"already_seen"}
        {"status":"spider_done","run_id":"...","expanded":N,"new_nodes":M,"skipped":K}
        {"status":"error","detail":"..."}
  * Защита от циклов: множество visited по (kind, value.lower()) — ноду с
    уже известным значением не пробиваем повторно, даже если id разные.
  * Rate-limiting: задержка VILKA_SPIDER_DELAY секунд между enrich-вызовами
    (по умолчанию 0.8) + потолок VILKA_SPIDER_MAX_NODES (по умолчанию 60),
    чтобы не убить upstream API и не раздуть доску.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Optional

from models import FactLink, FactNode, NodeKind
from enrichment import enrich

log = logging.getLogger("vilka.spider")

SendFn = Callable[[dict[str, Any]], Awaitable[None]]

SOURCE_PLACEHOLDER = "__SOURCE__"


def _delay() -> float:
    try:
        return max(0.0, float(os.getenv("VILKA_SPIDER_DELAY", "0.8")))
    except ValueError:
        return 0.8


def _max_nodes() -> int:
    try:
        return max(1, int(os.getenv("VILKA_SPIDER_MAX_NODES", "60")))
    except ValueError:
        return 60


def _node_key(kind: NodeKind | str, value: str) -> str:
    return f"{str(kind)}:{value.strip().lower()}"


class SpiderRun:
    """Один запуск раскрутки; остановка — cancel() или client disconnect."""

    def __init__(self, send: SendFn) -> None:
        self.run_id = f"sp_{uuid.uuid4().hex[:10]}"
        self._send = send
        self._task: Optional[asyncio.Task[None]] = None
        self.expanded = 0
        self.new_nodes = 0
        self.skipped = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self, graph_nodes: list[FactNode], start_id: str, depth: int) -> None:
        self._task = asyncio.create_task(
            self._bfs(graph_nodes, start_id, depth), name=f"spider-{self.run_id}"
        )

    async def wait(self) -> None:
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # -- core ---------------------------------------------------------------
    async def _bfs(self, graph_nodes: list[FactNode], start_id: str, depth: int) -> None:
        by_id = {n.id: n for n in graph_nodes}
        if start_id not in by_id:
            await self._send({"status": "error",
                              "detail": f"start node '{start_id}' not found in graph"})
            return

        visited: set[str] = {_node_key(n.kind, n.value) for n in graph_nodes}
        # Старт всегда пробиваем, даже если его значение уже на доске:
        visited.discard(_node_key(by_id[start_id].kind, by_id[start_id].value))

        # frontier: (нода, текущая глубина, id «родителя» на доске для связей)
        frontier: list[tuple[FactNode, int, Optional[str]]] = [(by_id[start_id], 0, None)]
        cap = _max_nodes()
        delay = _delay()

        await self._send({"status": "spider_started", "run_id": self.run_id,
                          "frontier": len(frontier), "depth": depth})

        while frontier:
            node, d, parent_id = frontier.pop(0)
            if self.expanded >= cap:
                await self._send({"status": "spider_stopped", "run_id": self.run_id,
                                  "reason": "max_nodes reached", "expanded": self.expanded})
                return

            key = _node_key(node.kind, node.value)
            if key in visited:
                self.skipped += 1
                await self._send({"status": "skipped", "node_id": node.id,
                                  "reason": "already_seen"})
                continue
            visited.add(key)

            source_id = parent_id or node.id
            await self._send({"status": "enriching", "node_id": node.id,
                              "kind": node.kind.value, "value": node.value, "depth": d})
            try:
                result = await enrich(kind=node.kind, value=node.value)
            except Exception as exc:  # noqa: BLE001
                log.exception("spider enrich failed")
                await self._send({"status": "error",
                                  "detail": f"enrich({node.value}) failed: {exc}"})
                continue
            self.expanded += 1

            new_ids: list[str] = []
            for n in result.nodes:
                nkey = _node_key(n.kind, n.value)
                if nkey in visited:
                    continue  # дедуп: такая сущность уже есть/будет на доске
                visited.add(nkey)
                new_ids.append(n.id)
                self.new_nodes += 1
                await self._send({"status": "found", "node": n.model_dump(by_alias=True)})

            known = {n.id for n in graph_nodes} | {x.id for x in result.nodes} | {source_id}
            for lk in result.links:
                from_ = source_id if lk.from_ == SOURCE_PLACEHOLDER else lk.from_
                to_ = source_id if lk.to_ == SOURCE_PLACEHOLDER else lk.to_
                if from_ in known and to_ in known and from_ != to_:
                    payload = lk.model_copy(update={"from_": from_, "to_": to_})
                    await self._send({"status": "found",
                                      "link": payload.model_dump(by_alias=True)})

            # Следующий слой BFS: новые ноды, если ещё есть глубина
            if d < depth:
                for n in result.nodes:
                    if n.id in new_ids:
                        frontier.append((n, d + 1, source_id))

            await asyncio.sleep(delay)  # rate-limit между запросами к API

        await self._send({"status": "spider_done", "run_id": self.run_id,
                          "expanded": self.expanded, "new_nodes": self.new_nodes,
                          "skipped": self.skipped})
