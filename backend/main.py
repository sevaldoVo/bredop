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

import json
import os
import time
from typing import Any, Optional, Union

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from enrichment import enrich
from models import (
    EnrichRequest,
    EnrichResponse,
    FactLink,
    FactNode,
    NodeKind,
    SourceNodeLite,
)

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
            else:
                await _send(ws, {"status": "error", "detail": f"unknown action: {action!r}"})
    except WebSocketDisconnect:
        return


@app.get("/api/kinds")
async def kinds() -> list[str]:
    """Список поддерживаемых типов нод (для тулзов/отладки)."""
    return [k.value for k in NodeKind]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
