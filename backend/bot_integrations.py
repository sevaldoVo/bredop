"""ВИЛКА ЭТАП 5 — Агрегатор скрытых OSINT API (боты Himera / Eye of God / Universal Search).

Самый НИЖНИЙ уровень фолбэка в каскаде enrichment.py:
    локальные сливы -> MTProto -> HTTP live -> [АГРЕГАТОРЫ] -> mock

Каждый адаптер получает httpx.AsyncClient (уже с случайным прокси из пула
VILKA_PROXIES) и сырое значение, а возвращает EnrichResult-совместимый dict
или None. Формат ответа ботов разный (JSON/text), поэтому каждый адаптер
парсит «своё» и нормализует к единому виду:

    {"name": str|None, "phones": [...], "emails": [...],
     "usernames": [...], "geo": str|None, "birth_date": str|None,
     "sources": [str, ...]}

Env-ключи агрегаторов (без ключа адаптер молча выключается):
    VILKA_HIMERA_API_URL / VILKA_HIMERA_API_KEY
    VILKA_EOG_API_URL    / VILKA_EOG_API_KEY      (Eye of God self-hosted)
    VILKA_USEARCH_API_URL / VILKA_USEARCH_API_KEY (Universal Search)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Awaitable, Callable, Optional

import httpx

log = logging.getLogger("vilka.aggregators")

_TIMEOUT = 20.0


class AggHit(dict[str, Any]):
    """Нормализованная находка от агрегатора (dict-подобно, для гибкости)."""


# ---------------------------------------------------------------------------
# Парсинг «сырого текста» ботов (Himera/EOG часто отдают text, не JSON)
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,14}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_USERNAME_RE = re.compile(r"(?:@|t\.me/)([A-Za-z]\w{3,31})")
# Метки ботов заканчиваются двоеточием — обязательны; значение до конца строки.
_NAME_RE = re.compile(r"(?:ФИО|Имя|Full\s*name|Name)[:\s]+([А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z .'-]*[А-Яа-яЁёA-Za-z])(?=\n|$)")
_DATE_RE = re.compile(r"(?:ДР|Birth\s*date|Date\s*of\s*birth|День\s*рождения)[:\s]+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})")


def parse_raw_bot_text(text: str) -> AggHit:
    hit = AggHit(phones=[], emails=[], usernames=[], name=None, geo=None,
                 birth_date=None, sources=["raw_text"])
    seen_p: set[str] = set()
    for m in _PHONE_RE.findall(text):
        p = re.sub(r"[\s\-()]|\.(?=\D)", "", m)
        if len(p) >= 10 and not p.startswith("+"):
            p = "+" + p
        if p not in seen_p:
            seen_p.add(p)
            hit["phones"].append(p)
    hit["emails"] = sorted(set(_EMAIL_RE.findall(text)))[:10]
    hit["usernames"] = sorted(set(_USERNAME_RE.findall(text)))[:10]
    nm = _NAME_RE.search(text)
    if nm:
        hit["name"] = nm.group(1).strip()
    bd = _DATE_RE.search(text)
    if bd:
        hit["birth_date"] = bd.group(1)
    geo_m = re.search(r"(?:Город|Geo|Location)[:\s]+([\wёА-Яа-я .,-]{3,60})", text)
    if geo_m:
        hit["geo"] = geo_m.group(1).strip()
    return hit


def _normalize_json(data: dict[str, Any]) -> AggHit:
    """Приводит типичный JSON-ответ бота к AggHit."""
    def _list(key: str) -> list[str]:
        v = data.get(key) or []
        return [str(x) for x in v][:20] if isinstance(v, list) else [str(v)]

    phones = _list("phones") or _list("phone_numbers")
    if p := data.get("phone"):
        phones.append(str(p))
    emails = _list("emails") or _list("mails")
    if e := data.get("email"):
        emails.append(str(e))
    usernames = _list("usernames") or _list("tg_usernames")
    if u := (data.get("username") or data.get("tg_username")):
        usernames.append(str(u))
    name = data.get("name") or data.get("full_name") or data.get("fio")
    geo = data.get("geo") or data.get("city") or data.get("location")
    return AggHit(
        phones=list(dict.fromkeys(phones)),
        emails=list(dict.fromkeys(emails)),
        usernames=list(dict.fromkeys(usernames)),
        name=str(name) if name else None,
        geo=str(geo) if geo else None,
        birth_date=data.get("birth_date") or data.get("bd"),
        sources=[str(s) for s in (data.get("sources") or ["aggregator_json"])][:10],
    )


# ---------------------------------------------------------------------------
# Адаптеры конкретных ботов (заглушки под реальные эндпоинты)
# ---------------------------------------------------------------------------

async def himera_check(client: httpx.AsyncClient, value: str) -> Optional[AggHit]:
    """Himera-osint API: POST {url}/v1/check {phone|email} + Bearer key."""
    base, key = os.getenv("VILKA_HIMERA_API_URL", ""), os.getenv("VILKA_HIMERA_API_KEY", "")
    if not base or not key:
        return None
    try:
        r = await client.post(f"{base.rstrip('/')}/v1/check",
                              json={"query": value},
                              headers={"Authorization": f"Bearer {key}"},
                              timeout=_TIMEOUT)
        if r.status_code != 200:
            log.debug("himera http %s", r.status_code)
            return None
        ct = r.headers.get("content-type", "")
        data = r.json() if "json" in ct else {"raw": r.text}
        hit = _normalize_json(data) if "json" in ct else parse_raw_bot_text(r.text)
        hit["sources"].append("himera")
        return hit if any(hit[k] for k in ("phones", "emails", "usernames", "name")) else None
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("himera failed: %s", exc)
        return None


async def eye_of_god_check(client: httpx.AsyncClient, value: str) -> Optional[AggHit]:
    """Eye-of-God self-hosted: POST /api/search {type:'phone'|'email', value}."""
    base, key = os.getenv("VILKA_EOG_API_URL", ""), os.getenv("VILKA_EOG_API_KEY", "")
    if not base or not key:
        return None
    kind = "email" if "@" in value else "phone"
    try:
        r = await client.post(f"{base.rstrip('/')}/api/search",
                              json={"type": kind, "value": value},
                              headers={"X-Api-Key": key},
                              timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        hit = _normalize_json(r.json())
        hit["sources"].append("eye_of_god")
        return hit if any(hit[k] for k in ("phones", "emails", "usernames", "name")) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("eog failed: %s", exc)
        return None


async def universal_search_check(client: httpx.AsyncClient, value: str) -> Optional[AggHit]:
    """Universal Search (us1.fi-группа): POST /search, ответ — текстовый дамп."""
    base, key = os.getenv("VILKA_USEARCH_API_URL", ""), os.getenv("VILKA_USEARCH_API_KEY", "")
    if not base or not key:
        return None
    try:
        r = await client.post(f"{base.rstrip('/')}/search",
                              json={"token": key, "query": value},
                              timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        try:
            hit = _normalize_json(json.loads(r.text))
        except (json.JSONDecodeError, TypeError):
            hit = parse_raw_bot_text(r.text)
        hit["sources"].append("universal_search")
        return hit if any(hit[k] for k in ("phones", "emails", "usernames", "name")) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("usearch failed: %s", exc)
        return None


AGGREGATORS: list[Callable[[httpx.AsyncClient, str], Awaitable[Optional[AggHit]]]] = [
    himera_check,
    eye_of_god_check,
    universal_search_check,
]
