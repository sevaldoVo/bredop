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
import json
import random
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx

from models import EnrichResponse, FactLink, FactNode, LinkKind, NodeKind

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_UA = "VILKA-OSINT/0.1 (+https://localhost; investigative tool)"


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
    source_id: str,
    target_id: str,
    kind: LinkKind,
    *,
    weight: float = 0.5,
    note: Optional[str] = None,
) -> FactLink:
    return FactLink(source=source_id, target=target_id, kind=kind, weight=weight, note=note)


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
# MOCK-генераторы (детерминированные, для теста и фолбэка)
# ---------------------------------------------------------------------------

_FIRST_M = ["Александр", "Дмитрий", "Максим", "Сергей", "Андрей", "Игорь", "Никита"]
_FIRST_F = ["Анна", "Мария", "Елена", "Ольга", "Наталья", "Екатерина", "Дарья"]
_LAST = ["Иванов", "Петров", "Сидоров", "Кузнецов", "Соколов", "Морозов", "Волков", "Козлов"]
_ORGS = ["ООО «Вектор»", "АО «ГлобалТрейд»", "ИП Смирнов А.В.", "ООО «НеоСистемы»", "ПАО «Руслинк»"]
_TLDS = ["mail.ru", "gmail.com", "yandex.ru", "outlook.com", "proton.me"]
_PLATFORMS = ["telegram", "instagram", "vk", "ok", "github"]


def _mock_phone(value: str) -> EnrichResult:
    """Телефон -> person (ФИО), email, аккаунты, geo, org + связи."""
    rng = _seeded_rng(value)
    phone = _norm_phone(value)
    res = EnrichResult(engine="mock")

    last = rng.choice(_LAST)
    first = rng.choice(_FIRST_M if rng.random() > 0.5 else _FIRST_F)
    patron = rng.choice(["Андреевич", "Сергеевич", "Игоревич", "Александрович", "Викторович"])
    person = _node(
        NodeKind.PERSON, f"{last} {first} {patron}",
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
    person = _node(
        NodeKind.PERSON, f"{rng.choice(_LAST)} {rng.choice(_FIRST_M)}",
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
    person = _node(NodeKind.PERSON, f"{rng.choice(_LAST)} {rng.choice(_FIRST_F)}",
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
}

_LIVE_DISPATCH: dict[NodeKind, LiveFn] = {
    NodeKind.DOMAIN: _live_domain,
    NodeKind.IP: _live_ip,
}


async def enrich(kind: NodeKind, value: str) -> EnrichResult:
    """Пробивает значение: сначала live-источник (если есть), иначе mock."""
    live = _LIVE_DISPATCH.get(kind)
    if live is not None:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers={"user-agent": _UA}) as client:
            try:
                result = await live(client, value)
            except Exception:  # noqa: BLE001 — enrichment best-effort
                result = None
        if result is not None:
            # Развесим связи live-нод к источнику
            for n in result.nodes:
                result.links.append(_link("__SOURCE__", n.id, LinkKind.RELATED_TO, weight=0.5))
            return result

    mock_fn = _MOCK_DISPATCH.get(kind, _mock_generic)
    # Небольшая искусственная задержка имитирует сетевые вызовы
    # и даёт фронтенду показать spinner.
    await asyncio.sleep(0.4)
    return mock_fn(value)
