# ВИЛКА — Backend (ЭТАП 1)

## Структура
```
backend/            FastAPI-бэкенд OSINT-обогащения
  main.py           роуты: GET /api/health, POST /api/enrich, GET /api/kinds
  models.py         Pydantic-модели = зеркало types.ts (FactNode/FactLink)
  enrichment.py     сервисы пробива: live (crt.sh, Cloudflare DoH, ip-api) + mock-фолбэк
frontend-patch/src/
  api.ts            enrichNode(node) — fetch-клиент к бэкенду
  Board.enrich.patch.tsx  патч Board.tsx: dblclick/кнопка, layout, анимация
```

## Запуск бэкенда
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Тест
```bash
curl -X POST http://localhost:8000/api/enrich \
  -H 'Content-Type: application/json' \
  -d '{"source_node": {"kind": "phone", "value": "+79990000000"}}'
```

## Фронтенд
1. Скопировать `frontend-patch/src/api.ts` в `src/` проекта.
2. В `Board.tsx` применить блоки [PATCH-1..4] из `Board.enrich.patch.tsx`.
3. `.env`: `VITE_VILKA_API=http://localhost:8000`

---

## 🐳 ЭТАП 4+: Docker-деплой (запуск за 1 минуту)

```bash
cp .env.example .env          # при желании впиши прокси/телеграм-ключи
mkdir -p data/breaches        # кинь сюда combolist'ы (.txt/.csv: email:pass и т.п.)
docker compose up -d --build
```

- API: http://localhost:8000 (docs: http://localhost:8000/docs)
- Web: http://localhost:5173 (nginx проксирует /api и /ws на backend)
- Volumes: `./data/breaches` (сливы), `./sessions` (Telethon-сессии)

Экспорт графа: `POST /api/export?format=graphml` (тело = снимок графа) → файл для Maltego/Gephi.

## 💳 ЭТАП 5: SaaS-слой (auth + кредиты + крипто-биллинг + аудит)

### Что появилось
| Модуль | Назначение |
|---|---|
| `backend/models_sql.py` | SQLAlchemy (async): таблицы `users`, `audit_logs`, `invoices`. Локально — aiosqlite (`./data/vilka.db`), в проде — `VILKA_DATABASE_URL=postgresql+asyncpg://...` |
| `backend/auth.py` | POST `/api/register`, POST `/api/login` (JWT HS256), GET `/api/me`; dependency `enforce_credit` — списывает 1 кредит на каждый POST `/api/enrich`, при нуле → **402 Payment Required** |
| `backend/billing.py` | POST `/api/billing/checkout` (NOWPayments/CryptoCloud, без ключа — мок-инвойс), POST `/api/webhook/crypto` — идемпотентное начисление **+1000 кредитов** по статусу paid, HMAC-SHA512 проверка подписи |
| `backend/bot_integrations.py` | Агрегаторы скрытых OSINT-ботов (Himera / Eye of God / Universal Search). Подключены в каскад `enrichment.py` как самый нижний фолбэк перед mock; парсят и JSON, и сырой текст отчётов |

### Инициализация БД и первый админ
Таблицы создаются автоматически при старте (`Base.metadata.create_all`, идемпотентно).
Для прода поверх можно навесить alembic:
```bash
cd backend
pip install alembic
alembic init migrations            # один раз
# sqlalchemy.url = cfg VILKA_DATABASE_URL; target_metadata из models_sql.Base
alembic revision --autogenerate -m "vilka saas tables"
alembic upgrade head
```

Первый админ — через env (создаётся/апгрейдится на startup):
```bash
export VILKA_JWT_SECRET="$(openssl rand -hex 32)"   # ОБЯЗАТЕЛЬНО в проде!
export VILKA_ADMIN_EMAIL=admin@vilka.io
export VILKA_ADMIN_PASSWORD='strong-password'
export VILKA_ADMIN_CREDITS=100000
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Ключевые env Этапа 5
```
VILKA_AUTH_ENFORCED=1        # 0 = enrich анонимный (режим Этапов 1–4)
VILKA_JWT_SECRET=...         # секрет JWT (без него — временный, токены сдохнут на рестарте)
VILKA_ENRICH_COST=1          # цена пробива в кредитах
VILKA_FREE_CREDITS=10         # приветственный бонус при регистрации
VILKA_DATABASE_URL=sqlite+aiosqlite:///./data/vilka.db   # или postgresql+asyncpg://...
VILKA_NP_API_KEY=...         # NOWPayments (без ключа checkout отдаёт мок-инвойс)
VILKA_NP_WEBHOOK_SECRET=...  # HMAC-секрет вебхука
VILKA_TOPUP_CREDITS=1000     # кредитов в пакете
VILKA_HIMERA_API_URL / VILKA_HIMERA_API_KEY
VILKA_EOG_API_URL    / VILKA_EOG_API_KEY
VILKA_USEARCH_API_URL/ VILKA_USEARCH_API_KEY
```

### Быстрый прогон (проверено)
```bash
TOKEN=$(curl -s -X POST localhost:8000/api/register -H 'content-type: application/json' \
  -d '{"email":"bob@test.io","password":"secret123"}' | jq -r .access_token)
curl -s -X POST localhost:8000/api/enrich -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"source_node":{"kind":"phone","value":"+79990000000"}}'
INV=$(curl -s -X POST localhost:8000/api/billing/checkout -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"amount_usd":49}' | jq -r .invoice_id)
curl -s -X POST localhost:8000/api/webhook/crypto -H 'content-type: application/json' \
  -d "{\"payment_id\":\"$INV\",\"payment_status\":\"finished\"}"   # -> credited 1000
```
