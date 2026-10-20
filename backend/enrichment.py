"""Сервисы OSINT-обогащения («пробива») для платформы ВИЛКА.

Стратегия:
  1. LIVE-источники (реальные открытые API через httpx) — по одному на kind.
     Сейчас подключены бесплатные и не требующие ключей:
       * phone   -> numverify (нужен бесплатный ключ, опционально)
       * domain  -> crt.sh (сертификаты) + Cloudflare DNS-over-HTTPS (A-записи)
       * ip      -> ip-api.com (гео по IP)
  2. Если live-источник недоступен/не настроен — фолбэк на детерминированный
     MOCK-генератор (засеян по значению ноды, чтобы повторные пробивы
     давали одинаковый результат — удобно для отладки фронтенда).

Каждый enricher возвращает пару (nodes, links), где links содержат связи
как между новыми нодами, так и к исходной ноде (по её id из запроса).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx

from models import EnrichResponse, FactLink, FactNode, LinkKind, NodeKind

logger = logging.getLogger("vilka.enrichment")

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_UA = "VILKA-OSINT/0.1 (+https://localhost; investigative tool)"


# ---------------------------------------------------------------------------
# OPSEC: пул прокси (VILKA_PROXIES="socks5://h:p,http://user:pass@h:p,...")
# ---------------------------------------------------------------------------

def _proxy_pool() -> list[str]:
    """Читает пул прокси из окружения (задаётся через запятую).

    Читается при каждом вызове, а не один раз при импорте — чтобы смена
    VILKA_PROXIES не требовала перезапуска процесса (например, в --reload).
    """
    raw = os.getenv("VILKA_PROXIES", "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _pick_proxy(rng: Optional[random.Random] = None) -> Optional[str]:
    """Случайный прокси из пула; пустой пул = прямой выход (None)."""
    pool = _proxy_pool()
    if not pool:
        return None
    pick = rng or random
    return pick.choice(pool)


def _new_client() -> httpx.AsyncClient:
    """AsyncClient с одним случайным прокси из пула на каждый запрос-сесссию.

    Ротация на уровне клиента => каждый live-вызов (ip-api, crt.sh, DoH, TG)
    уходит с нового исходящего IP — меньше 429 и не палим IP сервера.
    Для SOCKS5 нужен пакет ``httpx[socks]`` (добавлен в requirements.txt).
    """
    proxy = _pick_proxy()
    kwargs: dict = {"timeout": _HTTP_TIMEOUT, "headers": {"user-agent": _UA}}
    if proxy:
        # httpx >= 0.26 использует keyword `proxy`; старые версии — `proxies`.
        try:
            return httpx.AsyncClient(proxy=proxy, **kwargs)
        except TypeError:  # pragma: no cover — fallback для httpx < 0.26
            return httpx.AsyncClient(proxies=proxy, **kwargs)
    return httpx.AsyncClient(**kwargs)


def _seeded_rng(value: str) -> random.Random:
    """Детерминированный RNG: одно значение -> один и тот же «слив»."""
    digest = hashlib.sha256(value.lower().strip().encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    if not digits.startswith("+") and digits:
        digits = "+" + digits
    return digits


def _node(
    kind: NodeKind,
    value: str,
    *,
    label: Optional[str] = None,
    conf: int = 1,
    sources: Optional[list[str]] = None,
    meta: Optional[dict] = None,
) -> FactNode:
    return FactNode(
        kind=kind,
        value=value,
        label=label,
        conf=conf,
        sources=sources or ["mock"],
        meta=meta or {},
    )


def _link(
    from_id: str,
    to_id: str,
    kind: LinkKind,
    *,
    weight: float = 0.5,
    note: Optional[str] = None,
) -> FactLink:
    # JSON-алиасы: from_ -> "from", to_ -> "to" (см. models.FactLink)
    return FactLink(from_=from_id, to_=to_id, kind=kind, weight=weight, note=note)


@dataclass
class EnrichResult:
    nodes: list[FactNode] = field(default_factory=list)
    links: list[FactLink] = field(default_factory=list)
    engine: str = "mock"


# ---------------------------------------------------------------------------
# LIVE: домен -> сертификаты (crt.sh) + A-записи (Cloudflare DoH)
# ---------------------------------------------------------------------------

async def _live_domain(client: httpx.AsyncClient, domain: str) -> Optional[EnrichResult]:
    """crt.sh отдаёт субдены и email'ы из SAN; DoH — A-записи. Без ключей."""
    res = EnrichResult(engine="live")
    try:
        r = await client.get(
            "https://crt.sh/", params={"q": f"%.{domain}", "output": "json"}
        )
        if r.status_code == 200 and r.text.strip():
            rows = json.loads(r.text)
            subs: set[str] = {domain}
            emails: set[str] = set()
            for row in rows[:200]:
                for name in re.split(r"[,;\n]", row.get("name_value", "")):
                    name = name.strip().lstrip("*.").lower()
                    if "@" in name:
                        emails.add(name)
                    elif name.endswith(domain) and "." not in name.removesuffix(domain).removesuffix("."):
                        subs.add(name)
            for sub in sorted(subs)[:8]:
                node = _node(NodeKind.DOMAIN, sub, conf=3, sources=["crt.sh"])
                res.nodes.append(node)
                if sub != domain:
                    # субдомен — часть исходного домена
                    res.links.append(_link(node.id, "__SOURCE__", LinkKind.RELATED_TO, weight=0.7, note="субдомен"))
            for em in sorted(emails)[:5]:
                res.nodes.append(_node(NodeKind.EMAIL, em, conf=2, sources=["crt.sh cert SAN"]))
    except (httpx.HTTPError, json.JSONDecodeError):
        pass

    try:
        r = await client.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": domain, "type": "A"},
            headers={"accept": "application/dns-json", "user-agent": _UA},
        )
        if r.status_code == 200:
            for ans in r.json().get("Answer", []):
                if ans.get("type") == 1:
                    res.nodes.append(
                        _node(NodeKind.IP, ans["data"], conf=3, sources=["cloudflare-doh"])
                    )
    except (httpx.HTTPError, ValueError):
        pass

    return res if res.nodes else None


# ---------------------------------------------------------------------------
# LIVE: IP -> гео (ip-api.com, бесплатно, 45 req/min)
# ---------------------------------------------------------------------------

async def _live_ip(client: httpx.AsyncClient, ip: str) -> Optional[EnrichResult]:
    # SSRF-guard: валидируем IP ДО подстановки в URL ip-api.com (приватные/
    # loopback/link-local/multicast/reserved адреса не пробиваем наружу)
    try:
        ip_obj = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
            or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified):
        logger.info("ip-enrich skipped: %s is non-public address", ip)
        return None
    try:
        r = await client.get(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,lat,lon,isp,org")
        data = r.json()
        if data.get("status") != "success":
            return None
        res = EnrichResult(engine="live")
        geo_val = ", ".join(x for x in (data.get("city"), data.get("regionName"), data.get("country")) if x)
        geo = _node(
            NodeKind.GEO, geo_val or ip, conf=3, sources=["ip-api.com"],
            meta={"lat": data.get("lat"), "lon": data.get("lon")},
        )
        res.nodes.append(geo)
        res.links.append(_link("__SOURCE__", geo.id, LinkKind.HOSTED_AT, weight=0.9))
        org_name = data.get("org") or data.get("isp")
        if org_name:
            org = _node(NodeKind.ORG, org_name, conf=2, sources=["ip-api.com"])
            res.nodes.append(org)
            res.links.append(_link("__SOURCE__", org.id, LinkKind.HOSTED_AT, weight=0.6))
        return res
    except (httpx.HTTPError, ValueError):
        return None


# ---------------------------------------------------------------------------
# LIVE: телефон -> Telegram (MTProto-вектор через внешний TG-OSINT API)
# ---------------------------------------------------------------------------

# Настройка через окружение:
#   VILKA_TG_API_URL  — эндпоинт, принимающий JSON {"phone": "+7..."} и
#                       отвечающий {"found": bool, "tg_id": str, "username": str}
#                       (например, локальный бот-прослущик «Глаз Бога» / Eye of God
#                        self-hosted инстанс или telegram-phone-checker).
#   VILKA_TG_API_KEY  — необязательный Bearer-ключ.
# Если переменные не заданы или API недоступно — фолбэк на детерминированный mock.

async def _live_phone_telethon(phone: str) -> Optional[EnrichResult]:
    """ЭТАП 3: MTProto-пробив телефона напрямую через Telethon.

    Ищет аккаунт по номеру, затем парсит до 5 последних публичных
    сообщений цели -> ноды ACCOUNT + NOTE (с ссылкой на источник).
    Возвращает None, если канал выключен/недоступен (тогда работает
    HTTP VILKA_TG_API_URL, а затем mock).
    """
    from telethon_engine import is_enabled, mtproto_lookup, mtproto_recent_messages

    if not is_enabled():
        return None
    norm = _norm_phone(phone)
    found = await mtproto_lookup(norm)
    if not found:
        return None

    res = EnrichResult(engine="live")
    tg_id = found["tg_id"]
    username = found.get("username") or ""
    value = f"tg:{tg_id}"
    account = _node(
        NodeKind.ACCOUNT, value,
        label=f"@{username.lstrip('@')}" if username else value,
        conf=3, sources=["live:telethon-mtproto"],
        meta={"platform": "telegram", "tg_id": tg_id, "username": username,
              "name": found.get("name")},
    )
    res.nodes.append(account)
    res.links.append(
        _link(account.id, "__SOURCE__", LinkKind.BELONGS_TO, weight=0.95,
              note="MTProto: номер привязан к аккаунту")
    )
    # Последние сообщения -> NOTE-ноды с источниками
    for msg in await mtproto_recent_messages(tg_id):
        note = _node(
            NodeKind.NOTE, msg["text"],
            label=f"msg @{username or tg_id} ({msg['chat']})",
            conf=2, sources=[f"t.me/{msg['chat']}"],
            meta={"tg_id": tg_id, "date": msg["date"], "link": msg["link"]},
        )
        res.nodes.append(note)
        res.links.append(
            _link(account.id, note.id, LinkKind.MENTIONED_IN, weight=0.8,
                  note=msg["link"])
        )
    return res


async def _live_handle_telethon(handle: str) -> Optional[EnrichResult]:
    """ЭТАП 3: MTProto-поиск @username; при находке — те же ACCOUNT+NOTE."""
    from telethon_engine import is_enabled, mtproto_lookup, mtproto_recent_messages

    if not is_enabled():
        return None
    found = await mtproto_lookup(handle)
    if not found:
        return None

    res = EnrichResult(engine="live")
    tg_id = found["tg_id"]
    username = (found.get("username") or handle.lstrip("@")).lstrip("@")
    account = _node(
        NodeKind.ACCOUNT, f"tg:{tg_id}",
        label=f"@{username}", conf=3, sources=["live:telethon-mtproto"],
        meta={"platform": "telegram", "tg_id": tg_id, "username": username,
              "name": found.get("name")},
    )
    res.nodes.append(account)
    res.links.append(
        _link(account.id, "__SOURCE__", LinkKind.BELONGS_TO, weight=0.9,
              note="MTProto: username найден")
    )
    for msg in await mtproto_recent_messages(tg_id):
        note = _node(
            NodeKind.NOTE, msg["text"],
            label=f"msg @{username} ({msg['chat']})",
            conf=2, sources=[f"t.me/{msg['chat']}"],
            meta={"tg_id": tg_id, "date": msg["date"], "link": msg["link"]},
        )
        res.nodes.append(note)
        res.links.append(
            _link(account.id, note.id, LinkKind.MENTIONED_IN, weight=0.8,
                  note=msg["link"])
        )
    return res


async def _live_phone_tg(client: httpx.AsyncClient, phone: str) -> Optional[EnrichResult]:
    """Проверяет привязку номера к Telegram через внешний TG-OSINT API."""
    api_url = os.getenv("VILKA_TG_API_URL", "").strip()
    if not api_url:
        return None  # live-канал не настроен -> enrichment пойдёт через mock

    norm = _norm_phone(phone)
    headers = {"content-type": "application/json"}
    api_key = os.getenv("VILKA_TG_API_KEY", "").strip()
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    try:
        r = await client.post(api_url, json={"phone": norm}, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None

    if not data.get("found"):
        return None

    tg_id = str(data.get("tg_id") or "").strip()
    username = str(data.get("username") or "").strip()
    if not tg_id and not username:
        return None

    res = EnrichResult(engine="live")
    value = f"tg:{tg_id}" if tg_id else f"tg:@{username.lstrip('@')}"
    account = _node(
        NodeKind.ACCOUNT,
        value,
        label=f"@{username.lstrip('@')}" if username else value,
        conf=3,
        sources=["live:tg-phone-checker"],
        meta={"platform": "telegram", "tg_id": tg_id, "username": username},
    )
    res.nodes.append(account)
    # ACCOUNT belongs_to исходный PHONE (направление: аккаунт -> телефон).
    res.links.append(
        _link(account.id, "__SOURCE__", LinkKind.BELONGS_TO, weight=0.9,
              note="номер привязан к аккаунту Telegram")
    )
    return res


# ---------------------------------------------------------------------------
# LIVE: локальные базы утечек (ЭТАП 4) — синхронный SQLite, не нужен client
# ---------------------------------------------------------------------------

def _live_breaches(kind: NodeKind, value: str) -> Optional[EnrichResult]:
    """Ищет email/phone в локальной БД ./data/breaches/ (SQLite).

    Находка => ноды EVENT («Утечка <имя>») + NOTE с деталями и связи
    LEAKED_WITH / MENTIONED_IN к исходной ноде.
    """
    if kind not in (NodeKind.EMAIL, NodeKind.PHONE):
        return None
    try:
        from breaches import lookup  # ленивый импорт: модуль опционален
        found = lookup(value)
    except Exception:  # noqa: BLE001 — локальная БД best-effort
        return None
    if not found:
        return None
    res = EnrichResult(engine="live")
    for rec in found[:5]:
        breach = rec["breach"]
        ev = _node(
            NodeKind.EVENT, f"Утечка {breach}",
            conf=3, sources=["local:breach-db"],
            meta={"has_credential": rec.get("has_credential", False)},
        )
        res.nodes.append(ev)
        if kind is NodeKind.EMAIL:
            res.links.append(_link("__SOURCE__", ev.id, LinkKind.LEAKED_WITH, weight=0.9,
                                   note="email найден в локальном сливе"))
        else:
            res.links.append(_link("__SOURCE__", ev.id, LinkKind.MENTIONED_IN, weight=0.9,
                                   note="телефон найден в локальном сливе"))
        if rec.get("has_credential"):
            note = _node(
                NodeKind.NOTE, f"В сливе «{breach}» для {value} есть пароль",
                conf=2, sources=["local:breach-db"],
            )
            res.nodes.append(note)
            res.links.append(_link(note.id, ev.id, LinkKind.RELATED_TO, weight=0.5))
    return res


# ---------------------------------------------------------------------------
# LIVE: Reverse Image Search (ЭТАП 4) — Yandex Images, URL или base64 data-URI
# ---------------------------------------------------------------------------

async def _live_image(client: httpx.AsyncClient, value: str) -> Optional[EnrichResult]:
    """Картинка -> ACCOUNT/HANDLE (соцсети с профилями) + NOTE (страницы-источники)."""
    if os.getenv("VILKA_IMAGE_SEARCH", "1").strip() == "0":
        return None
    # SSRF-guard ДО сетевых операций: приватные/loopback/link-local адреса — 400.
    # data-URI (base64) здесь пропускаем — он валидируется после загрузки на tmpfiles.
    from image_search import _validate_public_http_url, SSRFError as _SSRFE
    if not value.strip().startswith("data:"):
        try:
            _validate_public_http_url(value)
        except _SSRFE as exc:
            raise ValueError(f"приватный/запрещённый URL запрещён: {exc}") from exc
    try:
        from image_search import reverse_image_search  # ленивый импорт
        hits = await reverse_image_search(value)
    except Exception:  # noqa: BLE001 — live best-effort
        return None
    if not hits:
        return None
    res = EnrichResult(engine="live")
    seen_handles: set[str] = set()
    social_count = 0
    source_notes = 0
    for h in hits:
        if h["kind"] == "social" and social_count < 8:
            handle = (h.get("handle") or "").lstrip("@")
            key = f"{h['platform']}:{handle.lower()}"
            if not handle or key in seen_handles:
                continue
            seen_handles.add(key)
            social_count += 1
            acc = _node(
                NodeKind.ACCOUNT, f"{h['platform']}:{handle}",
                label=f"@{handle} ({h['platform']})",
                conf=2, sources=["live:yandex-reverse-image"],
                meta={"platform": h["platform"], "url": h["url"], "from_image": True},
            )
            res.nodes.append(acc)
            res.links.append(_link(acc.id, "__SOURCE__", LinkKind.BELONGS_TO, weight=0.7,
                                   note="профиль найден по обратному поиску картинки"))
            hd = _node(
                NodeKind.HANDLE, f"@{handle}",
                conf=1, sources=["live:yandex-reverse-image"],
                meta={"platform": h["platform"]},
            )
            res.nodes.append(hd)
            res.links.append(_link(hd.id, acc.id, LinkKind.RELATED_TO, weight=0.6))
        elif h["kind"] == "source" and source_notes < 6:
            source_notes += 1
            note = _node(
                NodeKind.NOTE, f"Источник картинки: {h['url'][:120]}",
                conf=1, sources=["live:yandex-reverse-image"],
                meta={"url": h["url"], "platform": h.get("platform")},
            )
            res.nodes.append(note)
            res.links.append(_link(note.id, "__SOURCE__", LinkKind.MENTIONED_IN, weight=0.4))
    return res if res.nodes else None


def _mock_image(value: str) -> EnrichResult:
    """Детерминированный фолбэк reverse-image (для отладки без Yandex)."""
    rng = _seeded_rng(value)
    res = EnrichResult(engine="mock")
    plat = rng.choice(["instagram", "vk", "pinterest"])
    handle = f"{rng.choice(_LAST).lower().rstrip('ov')}{rng.randint(10, 99)}"
    acc = _node(NodeKind.ACCOUNT, f"{plat}:{handle}", label=f"@{handle} ({plat})",
                conf=1, sources=["mock:reverse-image"], meta={"platform": plat})
    dom = _node(NodeKind.DOMAIN, f"{rng.choice(['pic', 'img', 'cdn'])}.{rng.choice(_TLDS)}",
                conf=1, sources=["mock:exif-guess"])
    res.nodes = [acc, dom]
    res.links = [
        _link(acc.id, "__SOURCE__", LinkKind.BELONGS_TO, weight=0.4),
        _link("__SOURCE__", dom.id, LinkKind.HOSTED_AT, weight=0.3, note="предполагаемый хостинг"),
    ]
    return res


# ---------------------------------------------------------------------------
# MOCK-генераторы (детерминированные, для теста и фолбэка)
# ---------------------------------------------------------------------------

_FIRST_M = ["Александр", "Дмитрий", "Максим", "Сергей", "Андрей", "Игорь", "Никита"]
_FIRST_F = ["Анна", "Мария", "Елена", "Ольга", "Наталья", "Екатерина", "Дарья"]
_LAST = ["Иванов", "Петров", "Сидоров", "Кузнецов", "Соколов", "Морозов", "Волков", "Козлов"]
# Отчества строго из одного гендерного набора с именем (см. _mock_fio):
# мужское имя -> -ович/-евич, женское -> -овна/-евна. Никаких «Петров Дарья Сергеевич».
_PATRON_M = ["Андреевич", "Сергеевич", "Игоревич", "Александрович", "Викторович"]
_PATRON_F = ["Андреевна", "Сергеевна", "Игоревна", "Александровна", "Викторовна"]


def _mock_fio(rng: random.Random) -> str:
    """ФИО из СОГЛАСОВАННОГО гендерного набора: имя+фамилия+отчество одного пола."""
    last = rng.choice(_LAST)
    if rng.random() > 0.5:
        first, patron = rng.choice(_FIRST_M), rng.choice(_PATRON_M)
    else:
        first, patron = rng.choice(_FIRST_F), rng.choice(_PATRON_F)
        last = last + "а"  # женская форма фамилии: Иванов -> Иванова
    return f"{last} {first} {patron}"
_ORGS = ["ООО «Вектор»", "АО «ГлобалТрейд»", "ИП Смирнов А.В.", "ООО «НеоСистемы»", "ПАО «Руслинк»"]
_TLDS = ["mail.ru", "gmail.com", "yandex.ru", "outlook.com", "proton.me"]
_PLATFORMS = ["telegram", "instagram", "vk", "ok", "github"]


def _mock_phone(value: str) -> EnrichResult:
    """Телефон -> person (ФИО), email, аккаунты, geo, org + связи."""
    rng = _seeded_rng(value)
    phone = _norm_phone(value)
    res = EnrichResult(engine="mock")

    last = rng.choice(_LAST)
    fio = _mock_fio(rng)
    first = fio.split()[1]
    patron = fio.split()[2]
    person = _node(
        NodeKind.PERSON, fio,
        label=f"{last} {first[0]}. {patron[0]}.",
        conf=rng.randint(1, 2),
        sources=["mock:GetContact-like", "mock:truecaller-like"],
        meta={"age_range": f"{rng.randint(22, 55)}-{rng.randint(56, 70)}"},
    )

    email_user = f"{last.lower()}{rng.randint(1970, 2005)}"
    email = _node(
        NodeKind.EMAIL, f"{email_user}@{rng.choice(_TLDS)}",
        conf=2, sources=["mock:leak-db-2021"],
    )

    tg_id = f"{rng.randint(100_000_000, 999_999_999)}"
    tg = _node(
        NodeKind.ACCOUNT, f"tg:{tg_id}",
        label=f"@{last.lower()}_{tg_id[-3:]}",
        conf=2, sources=["mock:telegram-phone-sync"],
        meta={"platform": "telegram", "tg_id": tg_id},
    )

    handle = _node(
        NodeKind.HANDLE, f"@{last.lower()}{rng.randint(10, 99)}",
        conf=1, sources=["mock:social-crawler"],
        meta={"platform": rng.choice(_PLATFORMS)},
    )

    city = rng.choice(["Москва", "Санкт-Петербург", "Казань", "Екатеринбург", "Новосибирск"])
    geo = _node(NodeKind.GEO, city, conf=1, sources=["mock:operator-region"])

    org = _node(NodeKind.ORG, rng.choice(_ORGS), conf=1, sources=["mock:employer-guess"])

    res.nodes = [person, email, tg, handle, geo, org]
    # Связи НОВЫХ нод с ИСХОДНОЙ (плейсхолдер __SOURCE__ заменит main.py):
    res.links = [
        _link("__SOURCE__", person.id, LinkKind.OWNED_BY, weight=0.8, note="номер зарегистрирован на"),
        _link(person.id, email.id, LinkKind.LEAKED_WITH, weight=0.6, note="совместная утечка 2021"),
        _link(person.id, tg.id, LinkKind.BELONGS_TO, weight=0.7, note="привязан к аккаунту"),
        _link(person.id, handle.id, LinkKind.BELONGS_TO, weight=0.4),
        _link(person.id, geo.id, LinkKind.LOCATED_IN, weight=0.3),
        _link(person.id, org.id, LinkKind.WORKS_AT, weight=0.3),
        _link(tg.id, handle.id, LinkKind.RELATED_TO, weight=0.2),
    ]
    return res


def _mock_email(value: str) -> EnrichResult:
    rng = _seeded_rng(value)
    res = EnrichResult(engine="mock")
    domain = value.split("@")[-1]
    fio_m = f"{rng.choice(_LAST)} {rng.choice(_FIRST_M)}"  # муж. фамилия + муж. имя — согласовано
    person = _node(
        NodeKind.PERSON, fio_m,
        conf=1, sources=["mock:breach-corpus"],
    )
    phone = _node(
        NodeKind.PHONE, f"+79{rng.randint(100000000, 999999999)}",
        conf=1, sources=["mock:breach-corpus"],
    )
    dom = _node(NodeKind.DOMAIN, domain, conf=3, sources=["rfc5322"])
    event = _node(
        NodeKind.EVENT, f"утечка {domain} ({rng.randint(2018, 2024)})",
        conf=2, sources=["mock:leak-archive"],
    )
    res.nodes = [person, phone, dom, event]
    res.links = [
        _link("__SOURCE__", person.id, LinkKind.OWNED_BY, weight=0.5),
        _link(person.id, phone.id, LinkKind.OWNED_BY, weight=0.4),
        _link("__SOURCE__", dom.id, LinkKind.HOSTED_AT, weight=0.9),
        _link("__SOURCE__", event.id, LinkKind.MENTIONED_IN, weight=0.7),
        _link(event.id, person.id, LinkKind.MENTIONED_IN, weight=0.6),
    ]
    return res


def _mock_domain(value: str) -> EnrichResult:
    rng = _seeded_rng(value)
    res = EnrichResult(engine="mock")
    ips = [_node(NodeKind.IP, f"95.{rng.randint(100,180)}.{rng.randint(1,254)}.{rng.randint(1,254)}",
                 conf=2, sources=["mock:dns"]) for _ in range(2)]
    whois_email = _node(NodeKind.EMAIL, f"admin@{value}", conf=2, sources=["mock:whois"])
    org = _node(NodeKind.ORG, rng.choice(_ORGS), conf=1, sources=["mock:whois-registrant"])
    geo = _node(NodeKind.GEO, rng.choice(["Москва, Россия", "Amsterdam, NL", "Frankfurt, DE"]),
                conf=1, sources=["mock:geoip"])
    res.nodes = [*ips, whois_email, org, geo]
    res.links = [
        *[_link("__SOURCE__", ip.id, LinkKind.RESOLVES_TO, weight=0.8) for ip in ips],
        _link("__SOURCE__", whois_email.id, LinkKind.CONTACT_OF, weight=0.6),
        _link(whois_email.id, org.id, LinkKind.BELONGS_TO, weight=0.5),
        _link(ips[0].id, geo.id, LinkKind.HOSTED_AT, weight=0.4),
    ]
    return res


def _mock_handle(value: str) -> EnrichResult:
    rng = _seeded_rng(value)
    res = EnrichResult(engine="mock")
    clean = value.lstrip("@")
    cross = [_node(NodeKind.HANDLE, f"@{clean}_{p}", conf=1,
                   sources=["mock:name-similarity"], meta={"platform": p})
             for p in rng.sample(_PLATFORMS, 2)]
    fio_f = f"{rng.choice(_LAST)}а {rng.choice(_FIRST_F)}"  # жен. фамилия + жен. имя — согласовано
    person = _node(NodeKind.PERSON, fio_f,
                   conf=1, sources=["mock:bio-parser"])
    res.nodes = [*cross, person]
    res.links = [
        *[_link("__SOURCE__", h.id, LinkKind.RELATED_TO, weight=0.3) for h in cross],
        _link("__SOURCE__", person.id, LinkKind.BELONGS_TO, weight=0.4),
    ]
    return res


def _mock_generic(value: str) -> EnrichResult:
    rng = _seeded_rng(value)
    res = EnrichResult(engine="mock")
    note = _node(NodeKind.NOTE, f"Контекст по {value}: найдено совпадений — {rng.randint(1, 40)}",
                 conf=1, sources=["mock:index"])
    event = _node(NodeKind.EVENT, f"Упоминание в сводке {rng.randint(1, 12)}/2025",
                  conf=1, sources=["mock:monitoring"])
    res.nodes = [note, event]
    res.links = [
        _link("__SOURCE__", note.id, LinkKind.MENTIONED_IN, weight=0.3),
        _link("__SOURCE__", event.id, LinkKind.MENTIONED_IN, weight=0.3),
    ]
    return res


# ---------------------------------------------------------------------------
# Диспетчер
# ---------------------------------------------------------------------------

MockFn = Callable[[str], EnrichResult]
LiveFn = Callable[[httpx.AsyncClient, str], Awaitable[Optional[EnrichResult]]]

_MOCK_DISPATCH: dict[NodeKind, MockFn] = {
    NodeKind.PHONE: _mock_phone,
    NodeKind.EMAIL: _mock_email,
    NodeKind.DOMAIN: _mock_domain,
    NodeKind.HANDLE: _mock_handle,
    NodeKind.IMAGE: _mock_image,
}

# Телефон: сначала пробуем live-TG (если VILKA_TG_API_URL задан), при неудаче — mock.
_LIVE_DISPATCH: dict[NodeKind, list[LiveFn]] = {
    NodeKind.DOMAIN: [_live_domain],
    NodeKind.IP: [_live_ip],
    NodeKind.PHONE: [_live_phone_tg],
    NodeKind.IMAGE: [_live_image],
}

# ЭТАП 4: синхронный локальный live-канал (SQLite ./data/breaches).
# Проверяется раньше сетевых enricher'ов и mock — локальный слив точнее.
_LOCAL_LIVE_KINDS = (NodeKind.EMAIL, NodeKind.PHONE)


# ---------------------------------------------------------------------------
# ЭТАП 5: АГРЕГАТОРЫ скрытых OSINT-ботов (Himera / Eye of God / Universal Search)
# Самый нижний уровень фолбэка перед mock. Каждый адаптер получает клиент
# (с уже назначенным случайным прокси) и возвращает нормализованный AggHit.
# ---------------------------------------------------------------------------

try:
    from bot_integrations import AGGREGATORS, AggHit
except ImportError:  # pragma: no cover — модуль опционален
    AGGREGATORS = []

    class AggHit(dict):  # type: ignore[no-redef]
        pass


def _agg_to_result(hit: "AggHit", value: str) -> Optional[EnrichResult]:
    """Конвертирует находку агрегатора в ноды/связи FactNode/FactLink."""
    res = EnrichResult(engine="live")
    src_name = hit.get("name")
    person: Optional[FactNode] = None
    if src_name:
        person = _node(NodeKind.PERSON, src_name, conf=2,
                       sources=list(hit.get("sources", ["aggregator"])),
                       meta={"birth_date": hit.get("birth_date")} if hit.get("birth_date") else {})
        res.nodes.append(person)

    anchor_id = "__SOURCE__"  # всё вешаем на исходную ноду доски
    for phone in hit.get("phones", [])[:5]:
        if phone == value:
            continue
        n = _node(NodeKind.PHONE, phone, conf=2, sources=list(hit.get("sources", ["aggregator"])))
        res.nodes.append(n)
        res.links.append(_link(anchor_id, n.id, LinkKind.OWNED_BY, weight=0.7,
                               note="найден через OSINT-агрегатор"))
        if person is not None:
            res.links.append(_link(n.id, person.id, LinkKind.OWNED_BY, weight=0.6))
    for email in hit.get("emails", [])[:5]:
        if email == value:
            continue
        n = _node(NodeKind.EMAIL, email, conf=2, sources=list(hit.get("sources", ["aggregator"])))
        res.nodes.append(n)
        res.links.append(_link(anchor_id, n.id, LinkKind.OWNED_BY, weight=0.7,
                               note="найден через OSINT-агрегатор"))
        if person is not None:
            res.links.append(_link(n.id, person.id, LinkKind.OWNED_BY, weight=0.6))
    for uname in hit.get("usernames", [])[:5]:
        n = _node(NodeKind.HANDLE, f"@{uname.lstrip('@')}", conf=2,
                  sources=list(hit.get("sources", ["aggregator"])))
        res.nodes.append(n)
        res.links.append(_link(anchor_id, n.id, LinkKind.BELONGS_TO, weight=0.7,
                               note="tg-username из агрегатора"))
    if hit.get("geo"):
        g = _node(NodeKind.GEO, str(hit["geo"]), conf=1,
                  sources=list(hit.get("sources", ["aggregator"])))
        res.nodes.append(g)
        res.links.append(_link(anchor_id, g.id, LinkKind.LOCATED_IN, weight=0.5))
    return res if res.nodes else None


async def _run_aggregators(client: httpx.AsyncClient, kind: NodeKind, value: str) -> Optional[EnrichResult]:
    """Прогоняет значение через все настроенные агрегаторы, объединяет находки."""
    merged: Optional[EnrichResult] = None
    seen_keys: set[tuple[str, str]] = set()
    for agg in AGGREGATORS:
        try:
            hit = await agg(client, value)
        except Exception:  # noqa: BLE001 — best-effort
            continue
        if not hit:
            continue
        part = _agg_to_result(hit, value)
        if part is None:
            continue
        if merged is None:
            merged = part
        else:
            for n in part.nodes:
                key = (n.kind.value, n.value.lower())
                if key not in seen_keys:
                    merged.nodes.append(n)
            merged.links.extend(part.links)
        for n in (merged.nodes if merged else []):
            seen_keys.add((n.kind.value, n.value.lower()))
    return merged


# Telethon-обёртки не берут httpx-клиент; диспетчер различает их по сигнатуре.
MtLiveFn = Callable[[str], Awaitable[Optional[EnrichResult]]]

_MTPROTO_DISPATCH: dict[NodeKind, MtLiveFn] = {
    NodeKind.PHONE: _live_phone_telethon,
    NodeKind.HANDLE: _live_handle_telethon,
}


async def enrich(kind: NodeKind, value: str) -> EnrichResult:
    """Пробивает значение: локальные утечки -> MTProto -> live HTTP -> mock.

    Порядок каскада (ЭТАП 4):
      0. Локальная БД утечек ./data/breaches (для email/phone) — самый точный
         источник, бьёт всё остальное;
      1. Telethon/MTProto (для phone/handle, если VILKA_TELETHON_ENABLED=1);
      2. HTTP live-источники — каждый через СЛУЧАЙНЫЙ прокси из пула
         VILKA_PROXIES (см. _new_client) — ротация на запрос, anti-429/OPSEC;
      3. Детерминированный mock-фолбэк.
    """
    if kind in _LOCAL_LIVE_KINDS:
        local = _live_breaches(kind, value)
        if local is not None:
            # generic-связи «осиротевших» локальных нод к источнику
            linked = ({lk.from_, lk.to_} for lk in local.links)
            for n in local.nodes:
                if not any(n.id in pair for pair in linked):
                    local.links.append(_link("__SOURCE__", n.id, LinkKind.RELATED_TO, weight=0.5))
    else:
        local = None

    def _merge_local(result: EnrichResult) -> EnrichResult:
        """Дописывает находки локальной БД утечек к live/mock-результату."""
        if local is not None:
            result.nodes.extend(local.nodes)
            result.links.extend(local.links)
            result.engine = "live"  # локальный слив — достоверный источник
        return result

    mt_fn = _MTPROTO_DISPATCH.get(kind)
    if mt_fn is not None:
        try:
            result = await mt_fn(value)
        except Exception:  # noqa: BLE001 — enrichment best-effort
            result = None
        if result is not None:
            linked = ({lk.from_, lk.to_} for lk in result.links)
            for n in result.nodes:
                if not any(n.id in pair for pair in linked):
                    result.links.append(_link("__SOURCE__", n.id, LinkKind.RELATED_TO, weight=0.5))
            return _merge_local(result)

    lives = _LIVE_DISPATCH.get(kind, [])
    for live in lives:
        async with _new_client() as client:
            try:
                result = await live(client, value)
            except ValueError:  # SSRF-guard — валидированный отказ, пробрасываем наверх (HTTP 400)
                raise
            except Exception:  # noqa: BLE001 — enrichment best-effort
                result = None
        if result is not None:
            # Добавляем НЕДОСТАЮЩИЕ generic-связи live-нод к источнику
            # (enricher сам решает направление/тип; здесь — страховка, чтобы
            #  ни одна найденная нода не осталась «островом» на доске).
            linked = ({lk.from_, lk.to_} for lk in result.links)
            for n in result.nodes:
                if not any(n.id in pair for pair in linked):
                    result.links.append(_link("__SOURCE__", n.id, LinkKind.RELATED_TO, weight=0.5))
            return _merge_local(result)

    mock_fn = _MOCK_DISPATCH.get(kind, _mock_generic)
    # ЭТАП 5: перед mock — каскад агрегаторов скрытых OSINT-ботов
    # (Himera / Eye of God / Universal Search). Работает только для kinds,
    # которые умеют пробивать боты, и только если настроены env-ключи.
    if AGGREGATORS and kind in (NodeKind.PHONE, NodeKind.EMAIL, NodeKind.HANDLE):
        async with _new_client() as client:
            agg_res = await _run_aggregators(client, kind, value)
        if agg_res is not None:
            linked = ({lk.from_, lk.to_} for lk in agg_res.links)
            for n in agg_res.nodes:
                if not any(n.id in pair for pair in linked):
                    agg_res.links.append(_link("__SOURCE__", n.id, LinkKind.RELATED_TO, weight=0.5))
            return _merge_local(agg_res)

    # Небольшая искусственная задержка имитирует сетевые вызовы
    # и даёт фронтенду показать spinner.
    await asyncio.sleep(0.4)
    return _merge_local(mock_fn(value))
