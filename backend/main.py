"""ВИЛКА — OSINT Backend (FastAPI).

Запуск:
    uvicorn main:app --reload --port 8000

Эндпоинты:
    GET  /api/health   — проверка живости
    POST /api/enrich   — обогащение ноды (пробив)

CORS открыт для Vite-девсервера (http://localhost:5173) и любого
локального порта в режиме разработки (настраивается через VILKA_CORS_ORIGINS).
"""

from __future__ import annotations

import os
import time
from typing import Union

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

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


@app.post("/api/enrich", response_model=EnrichResponse)
async def api_enrich(req: EnrichRequest) -> EnrichResponse:
    """Пробивает исходную ноду и возвращает новые ноды + связи.

    Тело: {"source_node": {"kind": "phone", "value": "+79990000000"}}
    Допускается прислать полный FactNode — бэкенд возьмёт kind/value/id.
    """
    src: Union[FactNode, SourceNodeLite] = req.source_node
    source_id = getattr(src, "id", None) or f"req_{src.value[:16]}"

    t0 = time.perf_counter()
    try:
        result = await enrich(kind=src.kind, value=src.value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"enrichment failed: {exc}") from exc
    took_ms = int((time.perf_counter() - t0) * 1000)

    # Подменяем плейсхолдер исходной ноды на реальный id из запроса,
    # и выбрасываем ссылки на ещё неизвестные ноды.
    known_ids = {n.id for n in result.nodes} | {source_id}
    links: list[FactLink] = []
    for lk in result.links:
        source = source_id if lk.source == SOURCE_PLACEHOLDER else lk.source
        target = source_id if lk.target == SOURCE_PLACEHOLDER else lk.target
        if source in known_ids and target in known_ids and source != target:
            links.append(lk.model_copy(update={"source": source, "target": target}))

    return EnrichResponse(nodes=result.nodes, links=links, engine=result.engine, took_ms=took_ms)


@app.get("/api/kinds")
async def kinds() -> list[str]:
    """Список поддерживаемых типов нод (для тулзов/отладки)."""
    return [k.value for k in NodeKind]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
