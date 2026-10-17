"""Pydantic-модели «ВИЛКИ».

Зеркалят типы фронтенда из ``src/types.ts`` (FactNode / FactLink),
чтобы бэкенд и доска говорили на одном языке без ручного маппинга.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class NodeKind(str, Enum):
    """Тип сущности на следственной доске — копия ``NodeKind`` из types.ts."""

    TARGET = "target"
    PERSON = "person"
    ACCOUNT = "account"
    HANDLE = "handle"
    PHONE = "phone"
    EMAIL = "email"
    DOMAIN = "domain"
    IP = "ip"
    GEO = "geo"
    ORG = "org"
    EVENT = "event"
    NOTE = "note"
    IMAGE = "image"  # ЭТАП 4: URL/base64 картинки для reverse-search


class LinkKind(str, Enum):
    """Тип связи между нодами — копия ``LinkKind`` из types.ts."""

    BELONGS_TO = "belongs_to"      # account -> person
    OWNED_BY = "owned_by"          # phone/email -> person
    CONTACT_OF = "contact_of"      # phone/email <-> target
    RESOLVES_TO = "resolves_to"    # domain -> ip
    HOSTED_AT = "hosted_at"        # domain/ip -> geo
    WORKS_AT = "works_at"          # person -> org
    LOCATED_IN = "located_in"      # person/ip -> geo
    MENTIONED_IN = "mentioned_in"  # anything -> event/note
    RELATED_TO = "related_to"      # универсальная слабая связь
    LEAKED_WITH = "leaked_with"    # person x email (утечка)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class FactNode(BaseModel):
    """Узел графа. Полностью совместим с ``FactNode`` из types.ts.

    Координаты (x, y) не обязательны: если бэкенд их не присылает,
    фронтенд раскладывает новые ноды сам (см. layout в Board.tsx).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: _new_id("n"))
    kind: NodeKind
    value: str = Field(..., description="Основное значение: телефон, домен, ФИО…")
    label: Optional[str] = Field(
        default=None, description="Подпись на доске; по умолчанию = value"
    )
    conf: int = Field(
        default=1, ge=0, le=3, description="Уровень достоверности 0–3"
    )
    sources: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    x: Optional[float] = None
    y: Optional[float] = None
    createdAt: str = Field(default_factory=_now_iso)


class FactLink(BaseModel):
    """Ребро графа. Совместимо с ``FactLink`` из types.ts.

    ЭТАП 2: поля переименованы в ``from_`` / ``to_`` (зарезервированные
    слова Python), но в JSON сериализуются как ``from`` / ``to`` — ровно
    как ожидает фронтенд (alias_generator не используем точечно, чтобы
    не сломать camelCase-поле ``createdAt``).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("l"))
    from_: str = Field(..., alias="from", description="id исходной ноды")
    to_: str = Field(..., alias="to", description="id целевой ноды")
    kind: LinkKind = LinkKind.RELATED_TO
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    note: Optional[str] = None
    createdAt: str = Field(default_factory=_now_iso)


class SourceNodeLite(BaseModel):
    """Упрощённая нода для тела запроса: {"kind": "phone", "value": "+7..."}.
    """

    model_config = ConfigDict(extra="forbid")

    kind: NodeKind
    value: str


class EnrichRequest(BaseModel):
    """Тело POST /api/enrich.

    Принимает либо целиком FactNode (фронтенд шлёт свою ноду как есть),
    либо упрощённый {"kind": "...", "value": "..."} — оба варианта валидны.
    """

    model_config = ConfigDict(extra="forbid")

    source_node: FactNode | SourceNodeLite


class EnrichResponse(BaseModel):
    """Результат обогащения: новые ноды + связи (в т.ч. к исходной ноде)."""

    nodes: list[FactNode] = Field(default_factory=list)
    links: list[FactLink] = Field(default_factory=list)
    engine: str = Field(default="mock", description="mock | live")
    took_ms: int = 0

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        # Фронтенд ждёт from/to — всегда сериализуем по alias.
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(**kwargs)
