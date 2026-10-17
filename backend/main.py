"""ВИЛКА — OSINT Backend (FastAPI).

Запуск:
    uvicorn main:app --reload --port 8000

Эндпоинты:
    GET       /api/health   — проверка живости
    POST      /api/enrich   — обогащение ноды (пробив)
    WebSocket /ws/enrich    — стрим статусов обогащения (ЭТАП 2, задел под батчи)

CORS открыт для Vite-девсервера (http://localhost:5173) и любого
локального порта в режиме разработки (настраивается через VILKA_CORS_ORIGINS).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, Optional, Union

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enrichment import enrich
from models import (
    EnrichRequest,
    EnrichResponse,
    FactLink,
    FactNode,
    NodeKind,
    SourceNodeLite,
)
from spider import SpiderRun

try:  # networkx опционален: без него /api/path вернёт 503, остальное живёт
    import networkx as nx
    _HAS_NX = True
except ImportError:  # pragma: no cover
    nx = None  # type: ignore[assignment]
    _HAS_NX = False

try:
    from telethon_engine import shutdown as tg_shutdown
except ImportError:  # pragma: no cover — telethon не установлен
    async def tg_shutdown() -> None: ...  # type: ignore[misc]

SOURCE_PLACEHOLDER = "__SOURCE__"

app = FastAPI(
    title="ВИЛКА — OSINT Enrichment API",
    version="0.1.0",
    description="Бэкенд автоматического сбора данных для следственной доски.",
)

_default_origins = "http://localhost:5173,http://127.0.0.1:5173"
_origins = [o.strip() for o in os.getenv("VILKA_CORS_ORIGINS", _default_origins).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "vilka-backend"}


def _resolve_source_id(src: Union[FactNode, SourceNodeLite]) -> str:
    return getattr(src, "id", None) or f"req_{src.value[:16]}"


def _finalize(result, source_id: str) -> tuple[list[FactNode], list[FactLink]]:
    """Заменяет плейсхолдер исходной ноды на реальный id и отсекает висячие рёбра."""
    known_ids = {n.id for n in result.nodes} | {source_id}
    links: list[FactLink] = []
    for lk in result.links:
        from_ = source_id if lk.from_ == SOURCE_PLACEHOLDER else lk.from_
        to_ = source_id if lk.to_ == SOURCE_PLACEHOLDER else lk.to_
        if from_ in known_ids and to_ in known_ids and from_ != to_:
            links.append(lk.model_copy(update={"from_": from_, "to_": to_}))
    return result.nodes, links


@app.post("/api/enrich", response_model=EnrichResponse)
async def api_enrich(req: EnrichRequest) -> EnrichResponse:
    """Пробивает исходную ноду и возвращает новые ноды + связи.

    Тело: {"source_node": {"kind": "phone", "value": "+79990000000"}}
    Допускается прислать полный FactNode — бэкенд возьмёт kind/value/id.
    """
    src: Union[FactNode, SourceNodeLite] = req.source_node
    source_id = _resolve_source_id(src)

    t0 = time.perf_counter()
    try:
        result = await enrich(kind=src.kind, value=src.value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"enrichment failed: {exc}") from exc
    took_ms = int((time.perf_counter() - t0) * 1000)

    nodes, links = _finalize(result, source_id)
    return EnrichResponse(nodes=nodes, links=links, engine=result.engine, took_ms=took_ms)


# ---------------------------------------------------------------------------
# WebSocket /ws/enrich — стрим статусов (ЭТАП 2, архитектурный задел под батчи)
# ---------------------------------------------------------------------------
#
# Клиент шлёт:   {"action": "enrich", "node": {"kind": "phone", "value": "+7..."}}
# Сервер стримит:
#   {"status": "started", "node_id": "..."}
#   {"status": "found",   "node": {FactNode...}}          — по одной ноде за раз
#   {"status": "found",   "link": {FactLink...}}          — затем все связи
#   {"status": "done",    "engine": "live|mock", "took_ms": N, "count": N}
# Ошибки:        {"status": "error", "detail": "..."}
#
# Протокол спроектирован так, чтобы фронтенд мог применять инкрементально,
# а пробив 1000 сущностей не упирался в HTTP-таймауты (в будущем сюда же
# встанет очередь задач с event'ами progress).

async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_json(payload)


async def _handle_enrich_message(ws: WebSocket, msg: dict[str, Any]) -> None:
    node_raw = msg.get("node") or msg.get("source_node") or {}
    try:
        src: Union[FactNode, SourceNodeLite] = (
            FactNode.model_validate(node_raw)
            if ("id" in node_raw or "createdAt" in node_raw)
            else SourceNodeLite.model_validate(node_raw)
        )
    except ValidationError as exc:
        await _send(ws, {"status": "error", "detail": f"invalid node: {exc.errors()}"})
        return

    source_id = _resolve_source_id(src)
    await _send(ws, {"status": "started", "node_id": source_id})

    t0 = time.perf_counter()
    try:
        result = await enrich(kind=src.kind, value=src.value)
    except Exception as exc:  # noqa: BLE001
        await _send(ws, {"status": "error", "detail": f"enrichment failed: {exc}"})
        return
    took_ms = int((time.perf_counter() - t0) * 1000)

    nodes, links = _finalize(result, source_id)
    for n in nodes:
        await _send(ws, {"status": "found", "node": n.model_dump(by_alias=True)})
    for lk in links:
        await _send(ws, {"status": "found", "link": lk.model_dump(by_alias=True)})
    await _send(
        ws,
        {"status": "done", "engine": result.engine, "took_ms": took_ms, "count": len(nodes)},
    )


@app.websocket("/ws/enrich")
async def ws_enrich(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                await _send(ws, {"status": "error", "detail": f"invalid JSON: {exc.msg}"})
                continue
            if not isinstance(msg, dict):
                await _send(ws, {"status": "error", "detail": "expected JSON object"})
                continue
            action = msg.get("action")
            if action == "enrich":
                await _handle_enrich_message(ws, msg)
            elif action == "ping":
                await _send(ws, {"status": "pong"})
            elif action == "spider":
                await _handle_spider_message(ws, msg)
            else:
                await _send(ws, {"status": "error", "detail": f"unknown action: {action!r}"})
    except WebSocketDisconnect:
        return


# ---------------------------------------------------------------------------
# ЭТАП 3: Spider — рекурсивная раскрутка графа (стрим в /ws/enrich)
# ---------------------------------------------------------------------------
#
# Клиент шлёт:
#   {"action":"spider","start_node_id":"n_xxx","depth":2,
#    "graph":{"nodes":[FactNode...],"links":[...]}}   — снимок доски
# Сервер стримит spider_started / enriching / found(node|link) / skipped /
# spider_done. Фоновая задача — asyncio.create_task; при дисконнекте — cancel.

async def _handle_spider_message(ws: WebSocket, msg: dict[str, Any]) -> None:
    graph_raw = msg.get("graph") or {}
    try:
        nodes = [FactNode.model_validate(n) for n in graph_raw.get("nodes", [])]
    except ValidationError as exc:
        await _send(ws, {"status": "error", "detail": "invalid graph"})
        return

    start_id = str(msg.get("start_node_id") or "")
    depth_raw = msg.get("depth", 2)
    try:
        depth = max(1, min(int(depth_raw), 5))  # жёсткий потолок глубины
    except (TypeError, ValueError):
        depth = 2

    run = SpiderRun(send=lambda p: ws.send_json(p))
    run.start(nodes, start_id, depth)
    try:
        await run.wait()
    except WebSocketDisconnect:
        if run._task is not None:
            run._task.cancel()
        raise


# ---------------------------------------------------------------------------
# ЭТАП 3: HTTP-фасад спайдера — POST /api/spider (background task + job status)
# ---------------------------------------------------------------------------

class GraphSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[FactNode] = Field(default_factory=list)
    links: list[FactLink] = Field(default_factory=list)


class SpiderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_node_id: str
    depth: int = Field(default=2, ge=1, le=5)
    graph: GraphSnapshot


class JobInfo(BaseModel):
    job_id: str
    status: str          # queued | running | done | error
    expanded: int = 0
    new_nodes: int = 0
    skipped: int = 0
    detail: Optional[str] = None


_jobs: dict[str, JobInfo] = {}
_job_results: dict[str, EnrichResponse] = {}


async def _run_spider_job(job_id: str, nodes: list[FactNode], start_id: str, depth: int) -> None:
    """Фоновый spider без сокета: собирает результат в память (GET /api/spider/{job_id})."""
    info = _jobs[job_id]
    info.status = "running"
    collected_nodes: dict[str, FactNode] = {}
    collected_links: dict[str, FactLink] = {}

    async def fake_send(payload: dict[str, Any]) -> None:
        if payload.get("node") is not None:
            n = FactNode.model_validate(payload["node"])
            collected_nodes[n.id] = n
            info.new_nodes = len(collected_nodes)
        elif payload.get("link") is not None:
            lk = FactLink.model_validate(payload["link"])
            collected_links[lk.id] = lk
        elif payload.get("status") == "skipped":
            info.skipped += 1
        elif payload.get("status") in ("spider_done", "spider_stopped"):
            info.expanded = payload.get("expanded", info.expanded)
        elif payload.get("status") == "error":
            info.detail = payload.get("detail")

    run = SpiderRun(send=fake_send)
    run.start(nodes, start_id, depth)
    await run.wait()
    _job_results[job_id] = EnrichResponse(
        nodes=list(collected_nodes.values()),
        links=list(collected_links.values()),
        engine="spider",
    )
    info.status = "error" if info.detail else "done"


@app.post("/api/spider", status_code=202)
async def api_spider(req: SpiderRequest) -> dict[str, str]:
    """Запускает рекурсивную раскрутку фоновой задачей.

    Живой стрим прогресса — через WS `/ws/enrich` (action:"spider");
    поллинг результата для HTTP-клиентов — GET /api/spider/{job_id}.
    """
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _jobs[job_id] = JobInfo(job_id=job_id, status="queued")
    asyncio.create_task(_run_spider_job(job_id, req.graph.nodes, req.start_node_id, req.depth))
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/spider/{job_id}")
async def api_spider_status(job_id: str) -> dict[str, Any]:
    info = _jobs.get(job_id)
    if info is None:
        raise HTTPException(status_code=404, detail="job not found")
    out: dict[str, Any] = info.model_dump()
    if info.status == "done" and job_id in _job_results:
        out["result"] = _job_results[job_id].model_dump(by_alias=True)
    return out


# ---------------------------------------------------------------------------
# ЭТАП 3: аналитика графа — кратчайший путь между двумя нодами (networkx)
# ---------------------------------------------------------------------------

_server_graph: dict[str, tuple[list[FactNode], list[FactLink]]] = {}


def _shortest_path(nodes: list[FactNode], links: list[FactLink],
                   from_id: str, to_id: str) -> dict[str, Any]:
    if not _HAS_NX:
        raise HTTPException(status_code=503, detail="networkx not installed")
    by_id = {n.id: n for n in nodes}
    if from_id not in by_id or to_id not in by_id:
        raise HTTPException(status_code=404, detail="node(s) not found in graph")
    g = nx.Graph()
    for n in nodes:
        g.add_node(n.id, kind=n.kind.value, value=n.value)
    for lk in links:
        # вес: обратный к доверию связи — «надёжные» ниточки короче
        if lk.from_ in by_id and lk.to_ in by_id:
            g.add_edge(lk.from_, lk.to_, id=lk.id, kind=lk.kind.value,
                       weight=1.0 - 0.9 * lk.weight)
    try:
        ids = nx.shortest_path(g, from_id, to_id, weight="weight")
    except nx.NetworkXNoPath:
        return {"found": False, "hops": 0, "path_nodes": [], "path_links": []}
    path_links = []
    for a, b in zip(ids, ids[1:]):
        e = g.get_edge_data(a, b)
        if e:
            path_links.append({"id": e["id"], "from": a, "to": b, "kind": e["kind"]})
    return {
        "found": True,
        "hops": len(ids) - 1,
        "path_nodes": [{"id": i, "kind": by_id[i].kind.value, "value": by_id[i].value}
                       for i in ids],
        "path_links": path_links,
    }


@app.get("/api/path")
async def api_path(from_id: str, to_id: str) -> dict[str, Any]:
    """Кратчайший путь по серверному снимку доски (последний POST /api/path)."""
    graph = _server_graph.get("current")
    if graph is None:
        raise HTTPException(status_code=404,
                            detail="no server-side graph; POST your board to /api/path")
    return _shortest_path(graph[0], graph[1], from_id, to_id)


@app.post("/api/path")
async def api_path_body(body: GraphSnapshot, from_id: str, to_id: str) -> dict[str, Any]:
    """Поиск пути, где граф приходит в теле (фронт шлёт свой снимок).
    Заодно кеширует снимок как серверное состояние для GET-варианта."""
    _server_graph["current"] = (body.nodes, body.links)
    return _shortest_path(body.nodes, body.links, from_id, to_id)


@app.get("/api/kinds")
async def kinds() -> list[str]:
    """Список поддерживаемых типов нод (для тулзов/отладки)."""
    return [k.value for k in NodeKind]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
