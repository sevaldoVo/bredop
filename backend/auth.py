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

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
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

async def get_user_from_token(token: str) -> User:
    """Валидация JWT + загрузка юзера. Используется HTTP и WebSocket-входом."""
    payload = decode_token(token)
    user_id = int(payload["sub"])
    async for session in session_scope():
        user = await session.get(User, user_id)
        break
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Юзер не найден/заблокирован")
    return user


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужен Bearer-токен",
                            headers={"WWW-Authenticate": "Bearer"})
    return await get_user_from_token(creds.credentials)


async def enforce_credit(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Списывает 1 кредит АТОМАРНО. Кредитов нет -> 402 Payment Required."""
    async for session in session_scope():
        result = await session.execute(
            update(User)
            .where(User.id == user.id, User.credits_balance >= ENRICH_COST)
            .values(credits_balance=User.credits_balance - ENRICH_COST)
        )
        if result.rowcount == 0:  # строка не обновилась => кредитов <= 0
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                f"Недостаточно кредитов (нужно {ENRICH_COST}). Пополните баланс.",
            )
        session.add(AuditLog(
            user_id=user.id, action="credit_spend",
            credits_delta=-ENRICH_COST, detail=f"cost={ENRICH_COST}",
        ))
        await session.commit()
        refreshed = await session.get(User, user.id)
        return refreshed or user


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
            hashed_password=hash_password(body.password),
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
        if user is None or not verify_password(body.password, user.hashed_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль")
        if not user.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Аккаунт заблокирован")
        session.add(AuditLog(user_id=user.id, action="login"))
        await session.commit()
        token, ttl = create_token(user)
        return TokenResponse(access_token=token, expires_in=ttl,
                             credits_balance=user.credits_balance)


async def whoami(user: Annotated[User, Depends(get_current_user)]) -> MeResponse:
    return MeResponse(id=user.id, email=user.email, role=user.role,
                      credits_balance=user.credits_balance)
