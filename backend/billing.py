"""ВИЛКА ЭТАП 5 — BILLING: крипто-оплата (NOWPayments / CryptoCloud).

Схема работы:
  1. Фронт зовёт POST /api/billing/checkout -> бэк создаёт инвойс у процессинга
     (или генерит мок-инвойс, если ключ не задан) и кладёт строку в таблицу invoices.
  2. Юзер платит крипту.
  3. Процессинг шлёт POST /api/webhook/crypto {invoice_id, payment_status}.
  4. Мы сверяем подпись (HMAC для NOWPayments), находим invoice -> user_id
     и начисляем кредиты + пишем аудит. Идемпотентно: повторный webhook
     со статусом paid на уже оплаченный инвойс ничего не начислит.

Env:
  VILKA_NP_API_KEY        — API-ключ NOWPayments (для реального checkout)
  VILKA_NP_WEBHOOK_SECRET — секрет подписи вебхука (X-API-HMAC-SHA256-Digest)
  VILKA_PAYMENTS_URL      — база API (по умолчанию https://api.nowpayments.io/v1)
  VILKA_TOPUP_CREDITS     — сколько кредитов даёт пакет (по умолчанию 1000)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from models_sql import AuditLog, Invoice, User, session_scope

log = logging.getLogger("vilka.billing")

PAYMENTS_URL = os.getenv("VILKA_PAYMENTS_URL", "https://api.nowpayments.io/v1")
NP_API_KEY = os.getenv("VILKA_NP_API_KEY", "")
NP_WEBHOOK_SECRET = os.getenv("VILKA_NP_WEBHOOK_SECRET", "")
TOPUP_CREDITS = int(os.getenv("VILKA_TOPUP_CREDITS", "1000"))
PRICE_USD = float(os.getenv("VILKA_TOPUP_PRICE_USD", "49"))


class CheckoutRequest(BaseModel):
    amount_usd: float = Field(default=PRICE_USD, gt=0, le=100_000)
    currency: str = Field(default="usd")


class CheckoutResponse(BaseModel):
    invoice_id: str
    pay_url: str
    credits: int
    mock: bool


class WebhookAck(BaseModel):
    ok: bool = True
    detail: str = ""


# ---------------------------------------------------------------------------
# POST /api/billing/checkout — создать счёт на оплату
# ---------------------------------------------------------------------------

async def create_checkout(user: User, body: CheckoutRequest) -> CheckoutResponse:
    credits = TOPUP_CREDITS  # фиксированный пакет; тарификация — на проде через прайс
    if NP_API_KEY:
        payload = {
            "price_amount": body.amount_usd,
            "price_currency": body.currency.lower(),
            "order_id": f"vilka-{user.id}-{secrets.token_hex(4)}",
            "order_description": f"ВИЛКА: {credits} кредитов для {user.email}",
            "success_url": os.getenv("VILKA_SUCCESS_URL", "https://vilka.app/paid"),
            "cancel_url": os.getenv("VILKA_CANCEL_URL", "https://vilka.app/pricing"),
            "status_callback": os.getenv("VILKA_WEBHOOK_URL", "http://localhost:8000/api/webhook/crypto"),
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{PAYMENTS_URL}/payment",
                json=payload,
                headers={"x-api-key": NP_API_KEY, "content-type": "application/json"},
            )
        if r.status_code >= 400:
            log.error("nowpayments checkout failed: %s %s", r.status_code, r.text[:400])
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Процессинг недоступен")
        data = r.json()
        invoice_id = str(data.get("id") or data.get("payment_id"))
        pay_url = str(data.get("pay_url") or data.get("invoice_url") or "")
    else:
        # Мок-режим (нет ключа): локальный тест вебхука без живого процессинга.
        invoice_id = f"mock_{secrets.token_hex(8)}"
        pay_url = f"http://localhost:8000/mock-pay/{invoice_id}"
        log.warning("[billing] VILKA_NP_API_KEY не задан — мок-инвойс %s", invoice_id)

    async for session in session_scope():
        session.add(Invoice(invoice_id=invoice_id, user_id=user.id,
                            amount_usd=body.amount_usd, credits=credits))
        await session.commit()

    return CheckoutResponse(invoice_id=invoice_id, pay_url=pay_url,
                            credits=credits, mock=not NP_API_KEY)


# ---------------------------------------------------------------------------
# POST /api/webhook/crypto — уведомление об оплате
# ---------------------------------------------------------------------------

def _verify_nowpayments_signature(request_body: bytes, digest_header: str) -> bool:
    """NOWPayments: HMAC-SHA512 от сырого тела, hex-строка в X-API-HMAC-SHA512-Digest."""
    if not NP_WEBHOOK_SECRET:
        return True  # секрет не настроен (dev/мок) — пропускаем, но предупредим
    expected = hmac.new(NP_WEBHOOK_SECRET.encode(), request_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, digest_header)


async def handle_crypto_webhook(request: Request) -> WebhookAck:
    raw = await request.body()
    digest = request.headers.get("X-API-HMAC-SHA512-Digest", "")
    if NP_WEBHOOK_SECRET and not _verify_nowpayments_signature(raw, digest):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Битая подпись вебхука")

    try:
        body: dict[str, Any] = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не JSON") from None

    # NOWPayments присылает {"payment_id":..,"payment_status":"finished",...};
    # CryptoCloud — {"payment_id":..,"status":"success",...}. Нормализуем.
    invoice_id = str(body.get("invoice_id") or body.get("payment_id") or body.get("id") or "")
    st = str(body.get("payment_status") or body.get("status") or "").lower()
    paid = st in {"finished", "confirmed", "paid", "success"}

    if not invoice_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нет invoice_id/payment_id")

    async for session in session_scope():
        inv = (await session.execute(
            select(Invoice).where(Invoice.invoice_id == invoice_id)
        )).scalar_one_or_none()
        if inv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Инвойс {invoice_id} не найден")
        if inv.status == "paid":
            return WebhookAck(detail="already_credited (идемпотентно)")
        if not paid:
            inv.status = "failed" if st in {"canceled", "expired", "fail", "declined"} else st
            await session.commit()
            return WebhookAck(detail=f"status={st}, кредиты не начислены")

        user = await session.get(User, inv.user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Юзер инвойса пропал")
        user.credits_balance += inv.credits
        inv.status = "paid"
        inv.paid_at = datetime.now(timezone.utc)
        session.add(AuditLog(user_id=user.id, action="credit_topup",
                             credits_delta=inv.credits,
                             detail=f"invoice={invoice_id} via crypto"))
        await session.commit()
        log.info("[billing] +%d credits to user=%s invoice=%s",
                 inv.credits, user.id, invoice_id)
        return WebhookAck(detail=f"credited {inv.credits}")
