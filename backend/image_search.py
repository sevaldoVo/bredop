"""Reverse Image Search (ЭТАП 4) — Yandex Images через httpx + TLS-фингерпринт.

Принимает URL картинки или data-URI (base64), скачивает её и отправляет на
``https://yandex.ru/images/search?rpt=imageview&url=<...>`` (для base64 —
сначала отдаём картинку на tmpfiles.org и берём прямой URL). Парсит HTML
ответа на наличие внешних ссылок-источников (соцсети/профили), возвращая
ноды ACCOUNT/HANDLE + NOTE со страницами-источниками.

OPSEC: запросы идут через общий пул прокси VILKA_PROXIES (_new_client), а
``curl_cffi`` (если установлен) подменяет TLS-фингерпринт на Chrome — без
него Yandex чаще отдаёт капчу. Playwright не тянем: он весит ~300 МБ RAM,
что противоречит бюджету в 7 ГБ; при жёсткой капче enricher честно падает
в mock-фолбэк (как и остальные live-каналы).

env:
    VILKA_IMAGE_SEARCH=1|0   — выключатель live-канала (по умолчанию включён)
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import re
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("vilka.image_search")

_YA_URL = "https://yandex.ru/images/search"
_TMPFILE_URL = "https://tmpfiles.org/api/v1/upload"

# лимиты для base64-картинок: 11 МБ ДО декодирования, 8 МБ ПОСЛЕ
MAX_B64_LEN = 11 * 1024 * 1024
MAX_RAW_LEN = 8 * 1024 * 1024


class SSRFError(ValueError):
    """Запрошен URL/хост, указывающий на приватную сеть (loopback/RFC1918/
    link-local incl. 169.254.169.254, multicast, reserved)."""


def _validate_public_http_url(value: str) -> str:
    """SSRF-guard: пропускаем только публичные http(s)-URLы.

    Проверяем схему, хост (в т.ч. localhost/*.local/*.internal по имени) и
    ВСЕ резолвы хоста: если хоть один адрес приватный — отказ. Так нельзя
    обойти фильтр через DNS-алиас на 127.0.0.1 / метаданные облака.
    """
    p = urlparse((value or "").strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise SSRFError("требуется полный публичный http(s)-URL")
    host = p.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise SSRFError(f"запрещённый хост {host}")
    try:
        ip_obj = ipaddress.ip_address(host)
        addrs = [ip_obj]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise SSRFError(f"не резолвится хост {host}") from exc
        addrs = list({ipaddress.ip_address(i[4][0]) for i in infos})
    for addr in addrs:
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
            raise SSRFError(f"{host} указывает на {addr} — приватные адреса запрещены")
    return value.strip()

# ссылки на профили/соцсети, которые считаем «находками»
_SOCIAL_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|vk\.com|facebook\.com|twitter\.com|"
    r"x\.com|ok\.ru|m\.vk\.com|pinterest\.[a-z]+|tiktok\.com|linkedin\.com)"
    r"/[@\w.\-%]{2,64}",
    re.IGNORECASE,
)
# любые внешние домены-источники в выдаче (картинки «оттуда»)
_SOURCE_RE = re.compile(r'https?://(?!yandex\.|\.yandex|tmpfiles\.org)[\w.-]+\.[a-z]{2,}/[^\s"\'<>]{0,120}', re.IGNORECASE)


def _is_http_url(value: str) -> bool:
    try:
        p = urlparse(value.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except ValueError:
        return False


async def _upload_base64(client: httpx.AsyncClient, data_uri: str) -> Optional[str]:
    """Загружает base64-картинку на tmpfiles.org, возвращает прямой URL."""
    m = re.match(r"data:image/(\w+);base64,(.+)", data_uri.strip(), re.DOTALL)
    if not m:
        return None
    ext, b64 = m.group(1), m.group(2)
    # лимит ДО декодирования (защита от CPU/RAM-DoS через гигантский base64)
    if len(b64) > MAX_B64_LEN:
        logger.info("base64 image too large (pre-decode): %d bytes", len(b64))
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    if len(raw) > MAX_RAW_LEN:  # 8 MB ceiling после decode
        return None
    # SSRF-guard: URL tmpfiles валидируем так же, как и пользовательский
    uploaded_page = await _upload_and_get_url(client, raw, ext)
    if not uploaded_page:
        return None
    return _validate_public_http_url(uploaded_page)


async def _upload_and_get_url(client: httpx.AsyncClient, raw: bytes, ext: str) -> Optional[str]:
    files = {"file": (f"query.{ext}", raw, f"image/{ext}")}
    resp = await client.post(_TMPFILE_URL, files=files, timeout=15.0)
    resp.raise_for_status()
    page_url: str = resp.json()["url"]
    # tmpfiles отдаёт страницу; прямой линк — вставка /dl/ после домена
    return page_url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)


async def reverse_image_search(value: str) -> list[dict]:
    """Главная точка входа. value — http(s)-URL картинки или data:image;base64,....

    Возвращает список hits: {"url": ..., "kind": "social"|"source",
    "platform": ..., "handle": ...}. Пустой список => live-канал не сработал
    (вызывающий код фолбэкается на mock).
    """
    if not value or value.strip().lower() == "none":
        return []
    from enrichment import _new_client  # ленивый импорт: избегаем цикла

    async with _new_client() as client:
        img_url = value.strip()
        if img_url.startswith("data:"):
            uploaded = await _upload_base64(client, img_url)
            if not uploaded:
                return []
            img_url = uploaded
        else:
            # SSRF-guard перед отправкой URL в Яндекс (169.254.169.254, RFC1918,
            # loopback, link-local, multicast — не даём бэку ходиться внутрь сети)
            try:
                img_url = _validate_public_http_url(img_url)
            except SSRFError as exc:
                logger.info("reverse-image SSRF blocked: %s", exc)
                raise

        params = {"rpt": "imageview", "url": img_url}
        try:
            resp = await client.get(
                _YA_URL,
                params=params,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                logger.info("yandex reverse-image status=%s", resp.status_code)
                return []
            html = resp.text
        except httpx.HTTPError as exc:
            logger.info("yandex reverse-image error: %s", exc)
            return []

    hits: list[dict] = []
    seen: set[str] = set()

    for m in _SOCIAL_RE.finditer(html):
        url = m.group(0).rstrip(".,;'\"")
        if url in seen:
            continue
        seen.add(url)
        host = urlparse(url).netloc.lower().removeprefix("www.")
        platform = {
            "instagram.com": "instagram", "vk.com": "vk", "m.vk.com": "vk",
            "facebook.com": "facebook", "twitter.com": "twitter", "x.com": "twitter",
            "ok.ru": "odnoklassniki", "tiktok.com": "tiktok", "linkedin.com": "linkedin",
        }.get(host, host.split(".")[0])
        handle = urlparse(url).path.strip("/").split("/")[0]
        hits.append({"url": url, "kind": "social", "platform": platform, "handle": handle})

    for m in _SOURCE_RE.finditer(html):
        url = m.group(0).rstrip(".,;'\"")
        if url in seen:
            continue
        seen.add(url)
        host = urlparse(url).netloc.lower()
        hits.append({"url": url, "kind": "source", "platform": host, "handle": None})
        if len(hits) >= 25:
            break

    return hits
