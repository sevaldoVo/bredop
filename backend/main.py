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

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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

# ЭТАП 5: SaaS-слой (auth/billing/БД). Опционален: без sqlalchemy или
# VILKA_AUTH_ENFORCED=0 бэкенд живёт как раньше (анонимный enrich).
try:
    from models_sql import AuditLog, User, init_db, session_scope
    import auth
    from sqlalchemy import update as sa_update
    from auth import (
        LoginRequest,
        RegisterRequest,
        TokenResponse,
        MeResponse,
        EnrichUser,
        CurrentUser,
        enforce_credit,
        get_current_user,
        get_user_from_token,
        login_user,
        optional_paid_user,
        register_user,
        refund_credit,
        whoami,
    )
    from billing import CheckoutRequest, CheckoutResponse, WebhookAck, create_checkout, handle_crypto_webhook
    _HAS_SAAS = True
except ImportError:  # pragma: no cover
    _HAS_SAAS = False

# P0 FIX: флаг читается динамически в auth._auth_enforced() на каждый запрос —
# здесь только для информации/логирования.
# ЭТАП 7: флаг читается ЧЕРЕЗ auth._auth_enforced() (поддерживает и
# VILKA_AUTH_ENFORCED, и короткий AUTH_ENFORCED). Если SaaS-слой не импортирован,
# считаем по тем же двум именам напрямую.
def _flag_raw() -> str:
    return os.getenv("VILKA_AUTH_ENFORCED", os.getenv("AUTH_ENFORCED", ""))

if _HAS_SAAS:
    AUTH_ENFORCED = auth._auth_enforced()  # noqa: SLF001 — единый источник правды
else:
    AUTH_ENFORCED = (_flag_raw() or "0").strip().lower() not in ("0", "false", "off", "no")

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
    # БЛОК 3.3 (аудит GEMINI): без Authorization в allow_headers браузерный
    # CORS-preflight убивает все JWT-запросы с фронтенда на прод-домене.
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def _charge_id_middleware(request: Request, call_next):
    """БЛОК 2.2: каждому HTTP-запросу — уникальный charge_id.

    Зависимость optional_paid_user идемпотентно списывает кредит по этому id
    (а не по jti токена), поэтому один JWT можно использовать многократно,
    а refund при 422 остаётся точным (ровно за этот запрос)."""
    request.state.charge_id = uuid.uuid4().hex
    # БЛОК 2.4 (аудит GEMINI): rate-limit чувствительных ручек по IP.
    limit = _RATE_LIMITS.get((request.method, request.url.path))
    if limit is not None:
        allowed = _rate_allow(
            f"{request.client.host if request.client else 'unknown'}:{request.url.path}",
            limit[0], limit[1],
        )
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Слишком много запросов — попробуйте позже"},
                headers={"Retry-After": str(limit[1])},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# БЛОК 2.4 (аудит GEMINI): примитивный in-memory token-bucket rate limiter.
# Для одного инстанса достаточно; за горизонтальным масштабированием — Redis.
# register 3/час, login 10/мин, enrich 30/мин.
# ---------------------------------------------------------------------------

_RATE_BUCKETS: dict[str, list[float]] = {}
_RATE_LIMITS: dict[tuple[str, str], tuple[int, int]] = {
    ("POST", "/api/register"): (3, 3600),
    ("POST", "/api/login"): (10, 60),
    ("POST", "/api/enrich"): (30, 60),
}


def _rate_allow(key: str, max_calls: int, window_sec: int) -> bool:
    now = time.monotonic()
    bucket = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window_sec]
    if len(bucket) >= max_calls:
        _RATE_BUCKETS[key] = bucket
        return False
    bucket.append(now)
    _RATE_BUCKETS[key] = bucket
    return True


@app.on_event("startup")
async def _startup_breaches() -> None:
    """ЭТАП 4: при старте индексируем локальные сливы ./data/breaches (best-effort)."""
    try:
        from breaches import rescan
        n = rescan()
        print(f"[vilka] breach-db indexed records={n} dir={os.getenv('VILKA_BREACH_DIR', './data/breaches')}")
    except Exception as exc:  # noqa: BLE001 — бэкенд живёт и без утечек
        print(f"[vilka] breach-db init skipped: {exc}")


@app.on_event("startup")
async def _startup_saas_db() -> None:
    """ЭТАП 5: создаём таблицы users/audit_logs/invoices (create_all идемпотентен).

    БЛОК 3/коммит 3 (аудит GEMINI): если VILKA_ADMIN_EMAIL + валидный
    VILKA_ADMIN_PASSWORD заданы — при КАЖДОМ старте создаём админа либо
    ПЕРЕЗАПИСЫВАЕМ hashed_password существующего юзера с таким email
    (защита от захвата роли через регистрацию до старта). Дефолтные пароли
    ("change-me", "change-me-first-login") игнорируются полностью: админ не
    создаётся и не обновляется (защита от дефолтного пароля в проде).
    """
    if not _HAS_SAAS:
        return
    try:
        await init_db()
        admin_email = os.getenv("VILKA_ADMIN_EMAIL", "").strip().lower()
        admin_password = os.getenv("VILKA_ADMIN_PASSWORD", "")
        _DEFAULT_PW = ("change-me", "change-me-first-login")
        if admin_email and admin_password and admin_password not in _DEFAULT_PW:
            from auth import hash_password
            async for session in session_scope():
                from sqlalchemy import select
                u = (await session.execute(
                    select(User).where(User.email == admin_email)
                )).scalar_one_or_none()
                if u is None:
                    u = User(email=admin_email,
                             hashed_password=hash_password(admin_password),
                             credits_balance=int(os.getenv("VILKA_ADMIN_CREDITS", "100000")),
                             role="admin")
                    session.add(u)
                    print(f"[vilka] admin created: {admin_email}")
                else:
                    # принудительный сброс пароля админа на каждый старт
                    u.role = "admin"
                    u.hashed_password = hash_password(admin_password)
                    # микро-коммит: подтягиваем баланс до VILKA_ADMIN_CREDITS, если меньше
                    _admin_credits = int(os.getenv("VILKA_ADMIN_CREDITS", "100000"))
                    if (u.credits_balance or 0) < _admin_credits:
                        u.credits_balance = _admin_credits
                    print(f"[vilka] admin password reset on startup: {admin_email}")
                await session.commit()
        elif admin_email:
            print("[vilka] ADMIN INIT SKIPPED: VILKA_ADMIN_PASSWORD пустой или дефолтный "
                  "(запрещено в проде)")
    except Exception as exc:  # noqa: BLE001 — БД не должна ронять API
        print(f"[vilka] SaaS DB init skipped: {exc}")


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


async def _audit_enrich(user_id: Optional[int], kind: str, value: str,
                        engine: str, found: int) -> None:
    """ЭТАП 5: пишет каждый пробив в audit_logs (user_id=None в демо-режиме)."""
    if not _HAS_SAAS:
        return
    try:
        async for session in session_scope():
            session.add(AuditLog(
                user_id=user_id, action="enrich", credits_delta=0,
                detail=f"{kind}={value[:64]} engine={engine} found={found}",
            ))
            await session.commit()
    except Exception:  # noqa: BLE001 — аудит не должен ронять API
        pass


# ---------------------------------------------------------------------------
# ЭТАП 5: SaaS-роуты (auth / billing). P0 FIX: единая зависимость
# optional_paid_user для всех защищённых ручек — при VILKA_AUTH_ENFORCED=0
# она возвращает None (анонимный демо-режим), при =1 — JWT + списание кредита.
# Никаких «голых» параметров user и условных Depends(...) if ... else None
# (FastAPI трактовал такой параметр как обязательный query -> 422).
# ---------------------------------------------------------------------------
if _HAS_SAAS:

    @app.post("/api/register", response_model=TokenResponse)
    async def api_register(body: RegisterRequest) -> TokenResponse:
        return await register_user(body)

    @app.post("/api/login", response_model=TokenResponse)
    async def api_login(body: LoginRequest) -> TokenResponse:
        return await login_user(body)

    @app.get("/api/me", response_model=MeResponse)
    async def api_me(user: CurrentUser) -> MeResponse:
        return await whoami(user)

    @app.post("/api/billing/checkout", response_model=CheckoutResponse)
    async def api_checkout(
        body: CheckoutRequest,
        user: CurrentUser,
    ) -> CheckoutResponse:
        return await create_checkout(user, body)

    # Вебхук — ПУБЛИЧНЫЙ эндпоинт (его зовёт процессинг), защита HMAC-подписью.
    @app.post("/api/webhook/crypto", response_model=WebhookAck)
    async def api_webhook_crypto(request: Request) -> WebhookAck:
        return await handle_crypto_webhook(request)

    # -----------------------------------------------------------------
    # ЭТАП 7 (админка): список юзеров + аудит-лог. Доступ только role=admin.
    # -----------------------------------------------------------------
    @app.get("/api/admin/users")
    async def api_admin_users(user: CurrentUser, limit: int = 100) -> dict[str, Any]:
        if getattr(user, "role", "") != "admin":
            raise HTTPException(status_code=403, detail="Только для администраторов")
        from sqlalchemy import select as sa_select
        async for session in session_scope():
            rows = (await session.execute(
                sa_select(User).order_by(User.id.desc()).limit(min(limit, 500))
            )).scalars().all()
            break
        return {"users": [
            {"id": u.id, "email": u.email, "role": u.role,
             "credits_balance": u.credits_balance, "is_active": u.is_active,
             "created_at": u.created_at.isoformat() if u.created_at else None}
            for u in rows
        ]}

    @app.get("/api/admin/audit")
    async def api_admin_audit(
        user: CurrentUser, limit: int = 200, user_id: Optional[int] = None,
    ) -> dict[str, Any]:
        if getattr(user, "role", "") != "admin":
            raise HTTPException(status_code=403, detail="Только для администраторов")
        from sqlalchemy import select as sa_select
        stmt = sa_select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)
        async for session in session_scope():
            rows = (await session.execute(stmt)).scalars().all()
            break
        return {"audit_logs": [
            {"id": a.id, "user_id": a.user_id, "action": a.action,
             "target_kind": a.target_kind, "target_value": a.target_value,
             "detail": a.detail, "credits_delta": a.credits_delta,
             "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in rows
        ]}


# ---------------------------------------------------------------------------
# ЭТАП 7 P0: возврат кредита при 422. Списание происходит в зависимости
# optional_paid_user (до валидации тела), поэтому битое тело раньше «съедало»
# кредит (10->8 на два 422). Хендлер ловит RequestValidationError и делает
# refund (+1, audit 'credit_refund'), если на юзере стоит флаг _vilka_charged
# и запрос шёл на платную ручку. Идемпотентность — по jti токена.
# ---------------------------------------------------------------------------
_PAID_ROUTES = {"/api/enrich", "/api/spider", "/api/path", "/api/export"}


@app.exception_handler(RequestValidationError)
async def _validation_refund_handler(request: Request, exc: RequestValidationError) -> Response:
    body = {"detail": exc.errors()}
    if _HAS_SAAS and request.url.path in _PAID_ROUTES:
        auth_header = request.headers.get("authorization", "")
        token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
        if token:
            try:
                payload = auth.decode_token(token)
                uid = int(payload.get("sub", 0))
                # БЛОК 2.2: refund ровно за ЭТОТ запрос — по charge_id из
                # middleware (строка used_tokens с ключом "req:<charge_id>").
                cid = getattr(request.state, "charge_id", None)
                key = f"req:{cid}" if cid else ""
                refunded = await _refund_once(uid, key) if key else False
                if refunded:
                    body["credit_refunded"] = True
            except Exception:  # noqa: BLE001 — refund best-effort, 422 отдаём в любом случае
                pass
    return JSONResponse(status_code=422, content=body)


async def _refund_once(user_id: int, jti: str) -> bool:
    """Возврат ровно одного кредита за токен: атомарно удаляем строку used_tokens;
    если её нет (уже возвращали / не списывали) — False."""
    from models_sql import UsedToken
    from sqlalchemy import delete as sa_delete
    async for session in session_scope():
        res = await session.execute(sa_delete(UsedToken).where(UsedToken.jti == jti))
        if res.rowcount == 0:
            return False
        await session.execute(
            sa_update(User).where(User.id == user_id).values(
                credits_balance=User.credits_balance + 1
            )
        )
        session.add(AuditLog(user_id=user_id, action="credit_refund",
                             credits_delta=1, detail=f"422 refund jti={jti[:8]}"))
        await session.commit()
        return True


@app.post("/api/enrich", response_model=EnrichResponse)
async def api_enrich(
    req: EnrichRequest,
    user: EnrichUser,
) -> EnrichResponse:
    """Пробивает исходную ноду и возвращает новые ноды + связи.

    Тело: {"source_node": {"kind": "phone", "value": "+79990000000"}}
    Допускается прислать полный FactNode — бэкенд возьмёт kind/value/id.

    ЭТАП 5: при VILKA_AUTH_ENFORCED=1 требует Bearer-JWT и списывает 1 кредит
    (402 Payment Required при нулевом балансе). Каждый пробив пишется в audit_logs.
    """
    src: Union[FactNode, SourceNodeLite] = req.source_node
    source_id = _resolve_source_id(src)

    t0 = time.perf_counter()
    try:
        result = await enrich(kind=src.kind, value=src.value)
    except ValueError as exc:  # SSRFError — валидированный отказ (приватные адреса)
        raise HTTPException(status_code=400, detail=f"SSRF guard: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"enrichment failed: {exc}") from exc
    took_ms = int((time.perf_counter() - t0) * 1000)

    nodes, links = _finalize(result, source_id)
    await _audit_enrich(getattr(user, "id", None), src.kind.value, src.value,
                        result.engine, len(nodes))
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


async def _handle_enrich_message(ws: WebSocket, msg: dict[str, Any],
                                 user_id: Optional[int] = None) -> None:
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
    except ValueError as exc:  # SSRFError — валидированный отказ (приватные адреса)
        await _send(ws, {"status": "error", "detail": f"SSRF guard: {exc}"})
        return
    except Exception as exc:  # noqa: BLE001
        await _send(ws, {"status": "error", "detail": f"enrichment failed: {exc}"})
        return
    took_ms = int((time.perf_counter() - t0) * 1000)

    nodes, links = _finalize(result, source_id)
    for n in nodes:
        await _send(ws, {"status": "found", "node": n.model_dump(by_alias=True)})
    for lk in links:
        await _send(ws, {"status": "found", "link": lk.model_dump(by_alias=True)})
    await _audit_enrich(user_id, src.kind.value, src.value, result.engine, len(nodes))
    await _send(
        ws,
        {"status": "done", "engine": result.engine, "took_ms": took_ms, "count": len(nodes)},
    )


@app.websocket("/ws/enrich")
async def ws_enrich(ws: WebSocket) -> None:
    # ЭТАП 6 + P0 FIX: WS не умеет заголовки — JWT приходит query-параметром ?token=.
    # При VILKA_AUTH_ENFORCED=1 токен ОБЯЗАТЕЛ (close 4401 без него); при =0 —
    # токен не требуем (демо/локалка), но если прислан — всё равно валидируем.
    # БЛОК 2.5 (аудит GEMINI): браузер не умеет заголовки на WS — фронт шлёт
    # токен субпротоколом new WebSocket(url, ["bearer", token]); фолбэк ?token=.
    token = ""
    subs = ws.scope.get("subprotocols", [])
    # БЛОК 2.5: браузер шлёт new WebSocket(url, ["bearer", <jwt>]). uvicorn
    # сам согласует субпротокол из этого списка (обычно "bearer") — нам нужно
    # лишь достать токен (последний элемент, отличный от "bearer").
    for sub in reversed(subs):
        if sub and sub != "bearer":
            token = sub
            break
    if not token:
        token = ws.query_params.get("token") or ""
    # ЭТАП 7: флаг перечитываем динамически (AUTH_ENFORCED/VILKA_AUTH_ENFORCED),
    # чтобы не зависеть от значения на момент импорта модуля.
    enforced = auth._auth_enforced() if _HAS_SAAS else AUTH_ENFORCED  # noqa: SLF001
    if enforced and _HAS_SAAS and not token:
        await ws.close(code=4401, reason="authorization required")
        return
    user_ws = None
    if token:
        try:
            user_ws, charged_ws = await auth.get_user_from_token(token)
        except HTTPException as exc:
            await ws.close(code=4401, reason=str(exc.detail)[:120])
            return
        except Exception:
            await ws.close(code=4401, reason="invalid token")
            return
        # WS-пробивы списывают кредит так же, как HTTP; id юзера — для аудита.
        ws_user_id: Optional[int] = user_ws.id if charged_ws else None
    # БЛОК 2.5 фикс handshake: если клиент прислал субпротоколы ["bearer", <jwt>]
    # (два элемента), обязательно эхо-подтверждаем "bearer" в accept — иначе
    # браузер/клиент сам закрывает соединение (протокол не согласован) до того,
    # как мы успеем что-либо отправить. Одиночный субпротокол / его отсутствие —
    # accept без subprotocol (как раньше).
    _scope_subs = ws.scope.get("subprotocols", []) or []
    await ws.accept(subprotocol="bearer" if len(_scope_subs) == 2 and _scope_subs[0].lower() == "bearer" else None)
    # БЛОК 2.1 (аудит GEMINI): состояние соединения для пер-запросного списания.
    ws.state_user = user_ws if (token and _HAS_SAAS) else None  # type: ignore[attr-defined]
    ws.state_uid = user_ws.id if (token and _HAS_SAAS) else None  # type: ignore[attr-defined]
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
            if action in ("enrich", "spider"):
                # БЛОК 2.1: кредит списывается атомарно НА КАЖДЫЙ enrich/spider
                # внутри live-соединения (ключ идемпотентности — uuid сообщения),
                # а не один раз на handshake. Без токена/в демо — бесплатно.
                uid = None
                if _HAS_SAAS and auth._auth_enforced():  # noqa: SLF001
                    if getattr(ws, "state_user", None) is None:
                        await _send(ws, {"status": "error", "detail": "authorization required"})
                        continue
                    tok = token
                    cid = f"ws:{uuid.uuid4().hex}"
                    try:
                        u2, charged2 = await auth.get_user_from_token(tok, cid)
                        ws.state_user = u2
                    except HTTPException as exc:
                        await _send(ws, {"status": "error", "detail": str(exc.detail)})
                        continue
                    if not charged2:
                        await _send(ws, {"status": "error", "detail": "credit charge failed"})
                        continue
                    uid = u2.id
                uid = uid or getattr(ws, "state_uid", None)
                if action == "enrich":
                    await _handle_enrich_message(ws, msg, uid)
                else:
                    await _handle_spider_message(ws, msg, uid)
            elif action == "ping":
                await _send(ws, {"status": "pong"})
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

async def _handle_spider_message(ws: WebSocket, msg: dict[str, Any],
                                 user_id: Optional[int] = None) -> None:
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
# Аудит P0 (БЛОК 1.5): владелец job_id -> user.id. В демо-режиме (без JWT) id=None.
_job_owners: dict[str, Optional[int]] = {}


def _check_job_owner(job_id: str, user: Optional[User]) -> None:
    owner = _job_owners.get(job_id)
    if AUTH_ENFORCED and (owner is None or owner != getattr(user, "id", None)):
        raise HTTPException(status_code=404, detail="job not found")


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
async def api_spider(req: SpiderRequest, user: EnrichUser) -> dict[str, str]:
    """Запускает рекурсивную раскрутку фоновой задачей.

    ЭТАП 9 аудит (БЛОК 1.3): ручка защищена — при AUTH_ENFORCED=1 требует JWT и
    списывает 1 кредит; job привязывается к владельцу (_job_owners).

    Живой стрим прогресса — через WS `/ws/enrich` (action:"spider");
    поллинг результата для HTTP-клиентов — GET /api/spider/{job_id}.
    """
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    _jobs[job_id] = JobInfo(job_id=job_id, status="queued")
    _job_owners[job_id] = getattr(user, "id", None) if user is not None else None
    asyncio.create_task(_run_spider_job(job_id, req.graph.nodes, req.start_node_id, req.depth))
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/spider/{job_id}")
async def api_spider_status(job_id: str, user: EnrichUser) -> dict[str, Any]:
    info = _jobs.get(job_id)
    if info is None:
        raise HTTPException(status_code=404, detail="job not found")
    # БЛОК 1.5: чужой job_id не отдаём (404 вместо 403 — не светуем существование)
    _check_job_owner(job_id, user)
    out: dict[str, Any] = info.model_dump()
    if info.status == "done" and job_id in _job_results:
        out["result"] = _job_results[job_id].model_dump(by_alias=True)
    return out


# ---------------------------------------------------------------------------
# ЭТАП 3: аналитика графа — кратчайший путь между двумя нодами (networkx)
# ---------------------------------------------------------------------------

# Аудит P0 (БЛОК 1.4): снимок доски привязан к владельцу. Ключ — user.id, в
# демо-режиме (AUTH_ENFORCED=0 / без JWT) — sentinel 0. Чужой граф не виден.
_server_graph: dict[int, tuple[list[FactNode], list[FactLink]]] = {}


def _graph_key(user: Optional[User]) -> int:
    return getattr(user, "id", None) or 0


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
async def api_path(from_id: str, to_id: str, user: EnrichUser) -> dict[str, Any]:
    """Кратчайший путь по серверному снимку доски (последний POST /api/path).

    БЛОК 1.3/1.4: снимок хранится per-user — чужой граф через эту ручку не достать.
    """
    graph = _server_graph.get(_graph_key(user))
    if graph is None:
        raise HTTPException(status_code=404,
                            detail="no server-side graph; POST your board to /api/path")
    return _shortest_path(graph[0], graph[1], from_id, to_id)


@app.post("/api/path")
async def api_path_body(body: GraphSnapshot, from_id: str, to_id: str,
                        user: EnrichUser) -> dict[str, Any]:
    """Поиск пути, где граф приходит в теле (фронт шлёт свой снимок).
    Заодно кеширует снимок как серверное состояние для GET-варианта (per-user)."""
    _server_graph[_graph_key(user)] = (body.nodes, body.links)
    return _shortest_path(body.nodes, body.links, from_id, to_id)


@app.get("/api/kinds")
async def kinds() -> list[str]:
    """Список поддерживаемых типов нод (для тулзов/отладки)."""
    return [k.value for k in NodeKind]


# ---------------------------------------------------------------------------
# ЭТАП 4: экспорт графа (GraphML — Maltego/Gephi/offline-анализ)
# (снимок доски — уже объявленный GraphSnapshot из блока /api/path)
# ---------------------------------------------------------------------------


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _graphml(nodes: list[FactNode], links: list[FactLink]) -> str:
    """Конвертирует граф в GraphML (схемы declare'ов фиксированы, как любит Gephi)."""
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns"',
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '         xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns'
        ' http://graphml.graphdrawing.org/xmlns/1.1/graphml.xsd">',
        '  <key id="kind" for="node" attr.name="kind" attr.type="string"/>',
        '  <key id="value" for="node" attr.name="value" attr.type="string"/>',
        '  <key id="label" for="node" attr.name="label" attr.type="string"/>',
        '  <key id="conf" for="node" attr.name="conf" attr.type="int"/>',
        '  <key id="sources" for="node" attr.name="sources" attr.type="string"/>',
        '  <key id="created" for="node" attr.name="createdAt" attr.type="string"/>',
        '  <key id="lkind" for="edge" attr.name="kind" attr.type="string"/>',
        '  <key id="weight" for="edge" attr.name="weight" attr.type="double"/>',
        '  <key id="lnote" for="edge" attr.name="note" attr.type="string"/>',
        f'  <graph id="vilka" edgedefault="directed">',
    ]
    for n in nodes:
        lines.append(f'    <node id="{_xml_escape(n.id)}">')
        lines.append(f'      <data key="kind">{n.kind.value}</data>')
        lines.append(f'      <data key="value">{_xml_escape(n.value)}</data>')
        if n.label:
            lines.append(f'      <data key="label">{_xml_escape(n.label)}</data>')
        lines.append(f'      <data key="conf">{n.conf}</data>')
        if n.sources:
            lines.append(f'      <data key="sources">{_xml_escape(",".join(n.sources))}</data>')
        lines.append(f'      <data key="created">{n.createdAt}</data>')
        lines.append('    </node>')
    by_id = {n.id for n in nodes}
    for lk in links:
        if lk.from_ not in by_id or lk.to_ not in by_id:
            continue  # не пишем висячие рёбра — Maltego их не любит
        lines.append(f'    <edge id="{_xml_escape(lk.id)}" source="{_xml_escape(lk.from_)}" target="{_xml_escape(lk.to_)}">')
        lines.append(f'      <data key="lkind">{lk.kind.value}</data>')
        lines.append(f'      <data key="weight">{lk.weight}</data>')
        if lk.note:
            lines.append(f'      <data key="lnote">{_xml_escape(lk.note)}</data>')
        lines.append('    </edge>')
    lines += ['  </graph>', '</graphml>']
    return "\n".join(lines)


@app.post("/api/export")
async def api_export_post(body: GraphSnapshot, user: EnrichUser,
                          format: str = "graphml") -> Any:
    """Экспорт снимка доски (фронт присылает свой граф). Формат: graphml.

    БЛОК 1.3/1.4: ручка под auth-зависимостью; серверный кеш — per-user."""
    if format != "graphml":
        raise HTTPException(status_code=400, detail="supported formats: graphml")
    _server_graph[_graph_key(user)] = (body.nodes, body.links)  # заодно обновим серверный кеш
    xml = _graphml(body.nodes, body.links)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"content-disposition": 'attachment; filename="vilka-board.graphml"'},
    )


@app.get("/api/export")
async def api_export(user: EnrichUser, format: str = "graphml") -> Any:
    """Экспорт серверного снимка (последний POST /api/path или /api/export).

    Пример: GET /api/export?format=graphml -> vilka-board.graphml (Maltego/Gephi).
    БЛОК 1.3/1.4: отдаёт только снимок текущего пользователя (или демо-слот 0).
    """
    if format != "graphml":
        raise HTTPException(status_code=400, detail="supported formats: graphml")
    graph = _server_graph.get(_graph_key(user))
    if graph is None:
        raise HTTPException(status_code=404,
                            detail="no server-side graph; POST your board to /api/export")
    xml = _graphml(graph[0], graph[1])
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"content-disposition": 'attachment; filename="vilka-board.graphml"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
