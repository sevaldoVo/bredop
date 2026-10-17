"""Telethon / MTProto-движок «ВИЛКИ» (ЭТАП 3).

Реальный пробив через Telegram MTProto: глобальный поиск сущности
(телефон или @username) + парсинг последних сообщений из публичных
чатов/каналов цели.

Конфигурация через переменные окружения (.env):
    VILKA_TG_API_ID     — api_id из https://my.telegram.org
    VILKA_TG_API_HASH   — api_hash
    VILKA_TG_SESSION    — путь к session-файлу (создаётся при первом запуске;
                         для первого запуска нужен интерактивный ввод кода,
                         поэтому в проде сессию обычно генерят отдельно)
    VILKA_TELETHON_ENABLED  — "1" включает live-канал (по умолчанию выкл,
                         чтобы бэк стартовал без telethon-зависимостей)

Дизайн:
  * Движок ленивый: Telethon импортируется только при включённом флаге и
    реальном вызове — без установленной библиотеки модуль не роняет бэкенд.
  * Одна разделяемая сессия клиента на процесс (singleton под asyncio.Lock).
  * Любая ошибка (нет сети / нет сессии / flood-wait) -> None, и
    enrichment уходит в mock-фолбэк. Никогда не бросаем наружу.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover — только для type-checker
    from telethon import TelegramClient

log = logging.getLogger("vilka.telethon")

_PHONE_RE = re.compile(r"^\+?\d{7,15}$")
_MAX_MESSAGES = 5  # по ТЗ: последние 5 сообщений


def is_enabled() -> bool:
    """Telethon-канал активен? (флаг + ключи; сам telethon проверим лениво)."""
    return (
        os.getenv("VILKA_TELETHON_ENABLED", "").strip() == "1"
        and bool(os.getenv("VILKA_TG_API_ID"))
        and bool(os.getenv("VILKA_TG_API_HASH"))
    )


# ---------------------------------------------------------------------------
# Singleton клиента
# ---------------------------------------------------------------------------

_client: Optional["TelegramClient"] = None
_connect_lock = asyncio.Lock()


async def _get_client() -> Optional["TelegramClient"]:
    """Возвращает готового TelegramClient или None (не настроен / ошибка)."""
    global _client
    if _client is not None and _client.is_user_authorized():
        return _client

    async with _connect_lock:
        if _client is not None and _client.is_user_authorized():
            return _client
        try:
            from telethon import TelegramClient  # ленивый импорт
        except ImportError:
            log.warning("telethon не установлен — MTProto-канал пропущен")
            return None

        api_id = os.getenv("VILKA_TG_API_ID", "").strip()
        api_hash = os.getenv("VILKA_TG_API_HASH", "").strip()
        session = os.getenv("VILKA_TG_SESSION", "vilka_tg.session").strip()
        try:
            client = TelegramClient(session, int(api_id), api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                log.warning("telethon: сессия %s не авторизована (нужен код SMS)", session)
                await client.disconnect()
                return None
            _client = client
            return client
        except Exception as exc:  # noqa: BLE001 — best-effort OSINT-канал
            log.info("telethon недоступен: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Публичный API движка
# ---------------------------------------------------------------------------

async def mtproto_lookup(query: str) -> Optional[dict[str, Any]]:
    """Глобальный поиск сущности в Telegram по телефону или @username.

    Возвращает dict:
        {"tg_id": str, "username": str|None, "name": str|None,
         "phone": str|None}
    или None, если не найдено / канал недоступен.
    """
    query = query.strip()
    if not query:
        return None
    client = await _get_client()
    if client is None:
        return None

    try:
        if _PHONE_RE.match(query):
            entity = await client.get_entity(query)
        else:
            username = query.lstrip("@")
            # resolve_username даёт точное совпадение по юзернейму
            entity = await client.get_entity(username)

        tg_id = str(getattr(entity, "id", "") or "")
        if not tg_id:
            return None
        return {
            "tg_id": tg_id,
            "username": getattr(entity, "username", None),
            "name": " ".join(
                x for x in (getattr(entity, "first_name", None),
                            getattr(entity, "last_name", None)) if x
            ) or None,
            "phone": getattr(entity, "phone", None),
        }
    except Exception as exc:  # noqa: BLE001 — UsernameNoP2PError, FloodWaitError…
        log.info("mtproto lookup(%s) failed: %s", query, exc)
        return None


async def mtproto_recent_messages(tg_id: str, limit: int = _MAX_MESSAGES) -> list[dict[str, Any]]:
    """Последние `limit` публичных сообщений аккаунта (через его публичные чаты).

    Возвращает список dict:
        {"text": str, "date": iso str, "chat": str, "link": url|None}
    Ограничение MTProto: видно только то, что доступно аккаунту-агенту
    (публичные каналы/группы, где цель оставила след).
    """
    client = await _get_client()
    if client is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        entity = await client.get_entity(int(tg_id))
        dialogs = await client.get_dialogs()
        # Только публичные чаты/каналы, где участвует цель:
        for dlg in dialogs:
            if len(out) >= limit:
                break
            chat = dlg.entity
            if not (getattr(chat, "public", False) or hasattr(chat, "username")):
                continue
            try:
                async for msg in client.iter_messages(chat, from_user=entity, limit=1):
                    text = (msg.message or "").strip()
                    if not text:
                        continue
                    link = None
                    if getattr(chat, "username", None):
                        link = f"https://t.me/{chat.username}/{msg.id}"
                    out.append({
                        "text": text[:500],
                        "date": msg.date.isoformat(timespec="seconds") if msg.date else "",
                        "chat": getattr(chat, "title", None) or getattr(chat, "username", "?"),
                        "link": link,
                    })
            except Exception:  # noqa: BLE001 — пропускаем недоступный чат
                continue
    except Exception as exc:  # noqa: BLE001
        log.info("mtproto messages(%s) failed: %s", tg_id, exc)
    return out


async def shutdown() -> None:
    """Закрывает сессию (вызывается из lifespan FastAPI при остановке)."""
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _client = None
