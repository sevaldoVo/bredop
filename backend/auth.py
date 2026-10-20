"""ВИЛКА ЭТАП 5 — AUTH: регистрация, логин (JWT), списание кредитов.

Использование в роутах FastAPI:

    @app.post("/api/enrich")
    async def api_enrich(req: EnrichRequest, user: User = Depends(enforce_credit)):
        ...  # вызывается только если JWT валиден и кредит >= 1

Зависимости:
    get_current_user — Bearer-JWT -> User (или 401)
    enforce_credit   — get_current_user + атомарное списание 1 кредита (или 402)
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models_sql import AuditLog, User, session_scope

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

JWT_SECRET = os.getenv("VILKA_JWT_SECRET", "")          # обязательно задать в проде!
JWT_ALG = "HS256"
JWT_TTL_HOURS = int(os.getenv("VILKA_JWT_TTL_HOURS", "24"))
ENRICH_COST = int(os.getenv("VILKA_ENRICH_COST", "1"))  # кредитов за один пробив
FREE_CREDITS_ON_REGISTER = int(os.getenv("VILKA_FREE_CREDITS", "10"))

_bearer = HTTPBearer(auto_error=False)


def _auth_enforced() -> bool:
    """P0 FIX: динамический флаг (env читается на каждый запрос, а не при импорте).

    ЭТАП 7 (рассинхрон имён): код исторически читает VILKA_AUTH_ENFORCED, а доки/
    compose/.env.example — AUTH_ENFORCED. Поддерживаем ОБА имени: приоритет у
    префиксного VILKA_*, фолбэк — короткий AUTH_ENFORCED, дефолт — включено.
    """
    val = os.getenv("VILKA_AUTH_ENFORCED", os.getenv("AUTH_ENFORCED", "1"))
    return val.strip().lower() not in ("0", "false", "off", "no")


async def refund_credit(user_id: int, amount: int = None) -> None:
    """ЭТАП 7 P0: возврат списанного кредита (например, при 422 валидации тела).

    Идемпотентность обеспечивает вызывающий (RequestValidationError-хендлер),
    который знает jti токена и помечает его как refunded."""
    cost = ENRICH_COST if amount is None else amount
    async for session in session_scope():
        await session.execute(
            update(User).where(User.id == user_id).values(
                credits_balance=User.credits_balance + cost
            )
        )
        session.add(AuditLog(
            user_id=user_id, action="credit_refund", credits_delta=cost,
            detail=f"refund cost={cost}",
        ))
        await session.commit()


async def optional_paid_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User | None:
    """Единый паттерн для ВСЕХ защищённых ручек (/api/enrich, POST /api/spider,
    /api/path, WS /ws/enrich):
      - AUTH_ENFORCED=0 -> всегда None (анонимный демо-режим, без 422/401);
      - AUTH_ENFORCED=1 -> валидирует Bearer-JWT и списывает 1 кредит (401/402).
    Никаких «голых» параметров user в сигнатурах хэндлеров.

    БЛОК 2.2: идемпотентность списания — по charge_id запроса (middleware в
    main.py кладёт uuid в request.state.charge_id), а НЕ по jti токена. Один
    JWT теперь можно использовать многократно до истечения TTL."""
    if not _auth_enforced():
        return None
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужен Bearer-токен",
                            headers={"WWW-Authenticate": "Bearer"})
    user, charged = await get_user_from_token(creds.credentials,
                                              request_charge_id(request))
    if charged:
        # ЭТАП 7 P0: помечаем юзера «заряженным» — RequestValidationError-хендлер в
        # main.py вернёт кредит, если FastAPI завернёт тело запроса (422) ПОСЛЕ
        # того, как зависимость уже списала его. Успешный 200 сбрасывает флаг.
        user._vilka_charged = True  # type: ignore[attr-defined]
    return user


EnrichUser = Annotated[User | None, Depends(optional_paid_user)]


def _secret() -> str:
    global JWT_SECRET
    if not JWT_SECRET:
        # Не падаем в dev: генерим временный секрет (токены умрут после рестарта).
        JWT_SECRET = uuid.uuid4().hex
        print("[vilka.auth] VILKA_JWT_SECRET не задан — сгенерирован временный. "
              "Для прода задай его в .env!")
    return JWT_SECRET


# ---------------------------------------------------------------------------
# Pydantic-схемы
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    credits_balance: int


class MeResponse(BaseModel):
    id: int
    email: str
    role: str
    credits_balance: int


# ---------------------------------------------------------------------------
# Пароли / токены
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# БЛОК 2.3 (аудит Gemini): bcrypt — CPU-блокирующая операция (~250 мс при
# rounds=12). В async-хендлерах вызываем через asyncio.to_thread, чтобы не
# вешать event loop под нагрузкой (register/login брутфорс).
# ---------------------------------------------------------------------------

async def hash_password_async(plain: str) -> str:
    return await asyncio.to_thread(hash_password, plain)


async def verify_password_async(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(verify_password, plain, hashed)


def create_token(user: User) -> tuple[str, int]:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=JWT_TTL_HOURS)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "exp": expires_at,
        "iat": datetime.now(timezone.utc),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, _secret(), algorithm=JWT_ALG)
    return token, int((expires_at - datetime.now(timezone.utc)).total_seconds())


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, _secret(), algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Токен истёк") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Невалидный токен") from None


# ---------------------------------------------------------------------------
# Зависимости FastAPI
# ---------------------------------------------------------------------------

async def load_user_from_token(token: str) -> User:
    """Только валидация JWT + загрузка юзера (без списания). Для /api/me, /api/admin/*."""
    payload = decode_token(token)
    user_id = int(payload["sub"])
    async for session in session_scope():
        user = await session.get(User, user_id)
        break
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Юзер не найден/заблокирован")
    return user


async def get_user_from_token(token: str, charge_id: str | None = None) -> tuple[User, bool]:
    """Валидация JWT + загрузка юзера. Используется HTTP и WebSocket-входом.

    Возвращает (user, charged): charged=True, если кредит списан на этот запрос
    (см. _charge_token; БЛОК 2.2 — ключ идемпотентности charge_id запроса) —
    это сигнал для refund-хендлера при 422."""
    user = await load_user_from_token(token)
    async for session in session_scope():
        charged = await _charge_token(session, token, user, charge_id)
        break
    return user, charged


def request_charge_id(request: Request | None) -> str:
    """БЛОК 2.2 (аудит GEMINI): идемпотентность списания ПО ЗАПРОСУ, а не по jti.

    Старая схема (charge по jti) делала токен одноразовым: клиент падал бы на
    402 "Токен уже использован" на втором же enrich/spider/path/export подряд.
    Правильный ключ — уникальный id HTTP-запроса: middleware кладёт его в
    request.state.charge_id; для WS/фоновых вызовов генерируем uuid на месте.
    Повторная обработка того же запроса (например, refund-хендлер после 422)
    идемпотентна, а разные запросы одним токеном списывают каждый свой кредит.
    """
    rid = getattr(request.state, "charge_id", None) if request is not None else None
    return f"req:{rid}" if rid else f"req:{uuid.uuid4().hex}"


async def _charge_token(session: AsyncSession, token: str, user: User,
                        charge_id: str | None = None) -> bool:
    """ЭТАП 7 P0 + БЛОК 2.2: атомарное списание кредита на уровне ЗАПРОСА.

    Ключ таблицы used_tokens — charge_id (id HTTP-запроса), а не jti токена:
    один JWT живёт до истечения TTL и платит по кредиту за каждый запрос.
    Если тело потом не пройдёт валидацию (422), RequestValidationError-хендлер
    в main.py сделает refund (DELETE строки по тому же charge_id + возврат
    баланса), и баланс останется нетронутым.
    """
    if not _auth_enforced():
        return False
    payload = decode_token(token)
    jti = payload.get("jti", "")
    key = charge_id or f"req:{jti}"
    try:
        from models_sql import UsedToken  # noqa: WPS433
    except ImportError:
        # Fallback без таблицы used_tokens: списываем напрямую (старое поведение).
        result = await session.execute(
            update(User)
            .where(User.id == user.id, User.credits_balance >= ENRICH_COST)
            .values(credits_balance=User.credits_balance - ENRICH_COST)
        )
        if result.rowcount == 0:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                f"Недостаточно кредитов (нужно {ENRICH_COST}). Пополните баланс.",
            )
        session.add(AuditLog(
            user_id=user.id, action="credit_spend",
            credits_delta=-ENRICH_COST, detail=f"cost={ENRICH_COST}",
        ))
        await session.commit()
        return True

    from sqlalchemy.exc import IntegrityError
    try:
        # БЛОК 2.2: ключ идемпотентности — charge_id ЗАПРОСА (col jti), не jti токена.
        session.add(UsedToken(jti=key, user_id=user.id))
        await session.flush()
    except IntegrityError:
        await session.rollback()
        # Тот же самый запрос обрабатывается повторно (например, refund-хендлер) —
        # второй раз НЕ списываем; charged=False => refund не нужен.
        return False
    result = await session.execute(
        update(User)
        .where(User.id == user.id, User.credits_balance >= ENRICH_COST)
        .values(credits_balance=User.credits_balance - ENRICH_COST)
    )
    if result.rowcount == 0:
        await session.rollback()
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"Недостаточно кредитов (нужно {ENRICH_COST}). Пополните баланс.",
        )
    session.add(AuditLog(
        user_id=user.id, action="credit_spend",
        credits_delta=-ENRICH_COST, detail=f"cost={ENRICH_COST} jti={jti[:8]}",
    ))
    await session.commit()
    return True


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Только аутентификация (для /api/me, /api/admin/*) — БЕЗ списания кредита."""
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужен Bearer-токен",
                            headers={"WWW-Authenticate": "Bearer"})
    user = await load_user_from_token(creds.credentials)
    return user


async def enforce_credit(user: Annotated[User, Depends(get_current_user)]) -> User:
    """Совместимость API Этапа 5/6: кредит уже списан в get_user_from_token.

    Возвращает свежий объект юзера с обновлённым балансом; если на объекте стоит
    флаг _vilka_charged (выставлен optional_paid_user), сбрасываем его — успешный
    ответ не должен триггерить refund в RequestValidationError-хендлере."""
    async for session in session_scope():
        refreshed = await session.get(User, user.id)
        break
    result = refreshed or user
    if getattr(result, "_vilka_charged", False):
        try:
            del result._vilka_charged  # type: ignore[attr-defined]
        except AttributeError:
            pass
    return result


# ---------------------------------------------------------------------------
# Хэндлеры (роуты навешивает main.py)
# ---------------------------------------------------------------------------

async def register_user(body: RegisterRequest) -> TokenResponse:
    email = body.email.lower().strip()
    async for session in session_scope():
        exists = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Email уже зарегистрирован")
        user = User(
            email=email,
            hashed_password=await hash_password_async(body.password),
            credits_balance=FREE_CREDITS_ON_REGISTER,
        )
        session.add(user)
        await session.flush()
        session.add(AuditLog(user_id=user.id, action="register",
                             credits_delta=FREE_CREDITS_ON_REGISTER,
                             detail=f"welcome bonus={FREE_CREDITS_ON_REGISTER}"))
        await session.commit()
        token, ttl = create_token(user)
        return TokenResponse(access_token=token, expires_in=ttl,
                             credits_balance=user.credits_balance)


async def login_user(body: LoginRequest) -> TokenResponse:
    email = body.email.lower().strip()
    async for session in session_scope():
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None or not await verify_password_async(body.password, user.hashed_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль")
        if not user.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Аккаунт заблокирован")
        session.add(AuditLog(user_id=user.id, action="login"))
        await session.commit()
        token, ttl = create_token(user)
        return TokenResponse(access_token=token, expires_in=ttl,
                             credits_balance=user.credits_balance)


# P0 FIX (ЭТАП 6): аннотации НЕ должны быть строками. В модуле с
# `from __future__ import annotations` FastAPI резолвит forward-ref только через
# globals() функции — локальные импорты User/Depends там отсутствуют, и параметр
# молча превращался в обязательный query (422 "query, user"). Поэтому все
# зависимости-хелперы живут в auth.py (импортированы на уровне модуля), а main.py
# использует их как значения (не строки).
CurrentUser = Annotated[User, Depends(get_current_user)]


async def whoami(user: CurrentUser) -> MeResponse:
    return MeResponse(id=user.id, email=user.email, role=user.role,
                      credits_balance=user.credits_balance)
