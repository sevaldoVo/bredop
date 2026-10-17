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
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("vilka.image_search")

_YA_URL = "https://yandex.ru/images/search"
_TMPFILE_URL = "https://tmpfiles.org/api/v1/upload"

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
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    if len(raw) > 8 * 1024 * 1024:  # 8 MB ceiling
        return None
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
