"""ВИЛКА ЭТАП 5 — SQLAlchemy-модели (SaaS: юзеры, кредиты, аудит).

Таблицы:
    users       — аккаунты (email, hashed_password, credits_balance, role)
    audit_logs  — кто/что/когда пробивал + списания/начисления кредитов
    invoices    — привязка крипто-инвойса к юзеру (для вебхука биллинга)

Движок асинхронный: aiosqlite для локалки, asyncpg в проде
(VILKA_DATABASE_URL=postgresql+asyncpg://user:pass@host/vilka).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    credits_balance: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(64))  # enrich / spider / credit_spend / credit_topup / login ...
    target_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    credits_delta: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Invoice(Base):
    """Крипто-инвойс, выданный процессингу. Нужен, чтобы вебхук понял,
    какому юзеру начислять кредиты по invoice_id."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    credits: Mapped[int] = mapped_column(Integer, default=1000)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|paid|failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Движок / сессии
# ---------------------------------------------------------------------------

_DEFAULT_SQLITE = "sqlite+aiosqlite:///./data/vilka.db"


def _database_url() -> str:
    return os.getenv("VILKA_DATABASE_URL", _DEFAULT_SQLITE)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = _database_url()
        kwargs: dict[str, object] = {"echo": False}
        if url.startswith("postgresql"):
            kwargs["pool_size"] = int(os.getenv("VILKA_DB_POOL_SIZE", "10"))
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def session_scope() -> AsyncIterator[AsyncSession]:
    """FastAPI-dependency: одна транзакция на запрос."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def init_db() -> None:
    """Создаёт таблицы (идемпотентно). Для прода можно поверх повесить alembic."""
    if _database_url().startswith("sqlite"):
        db_path = _database_url().split("///")[-1]
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
