# ВИЛКА — Backend (ЭТАП 1)

## Структура
```
backend/            FastAPI-бэкенд OSINT-обогащения
  main.py           роуты: GET /api/health, POST /api/enrich, GET /api/kinds
  models.py         Pydantic-модели = зеркало types.ts (FactNode/FactLink)
  enrichment.py     сервисы пробива: live (crt.sh, Cloudflare DoH, ip-api) + mock-фолбэк
frontend/src/
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
1. Фронтенд целиком лежит в `frontend/` (api.ts и все патчи PATCH-1..6 уже применены к Board.tsx).
2. Сборка: `cd frontend && npm install && npm run build` (или через docker compose — см. ниже).
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

## 🚀 ЭТАП 6: Production-деплой (VPS + TLS + CI/CD)

### Состав
- `nginx/nginx.conf` — reverse proxy: HTTPS (vilka.app), `/api/*` и `/ws/*` → backend:8000, SPA → web; HSTS/X-Frame-Options/CSP/gzip, WS-upgrade.
- `docker-compose.prod.yml` — **PostgreSQL 15** (volume `pgdata`) вместо SQLite, backend/web без публичных портов (весь трафик через nginx), **certbot** с автопродлением каждые 12ч.
- `.github/workflows/deploy.yml` — CI/CD: push в master → SSH на VPS → `git reset --hard` → init_db (миграции) → `docker compose up -d --build` → healthcheck с авто-rollback.

### Первый запуск на сервере (за 1 минуту)
```bash
git clone https://github.com/sevaldoVo/bredop.git vilka && cd vilka
cp .env.example .env   # заполнить: VILKA_PG_PASSWORD, VILKA_JWT_SECRET, VILKA_ADMIN_*
# DNS A-запись vilka.app -> IP сервера; порты 80/443 открыты
docker compose -f docker-compose.prod.yml up -d --build
# самоподписанный cert, чтобы nginx поднялся до первого выпуска LE:
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
  -keyout ./nginx/ssl/privkey.pem -out ./nginx/ssl/fullchain.pem -subj "/CN=vilka.app" 2>/dev/null || true
docker compose -f docker-compose.prod.yml exec certbot certonly \
  --webroot -w /var/www/certbot -d vilka.app -d www.vilka.app
docker compose -f docker-compose.prod.yml exec nginx nginx -s reload
```

### Настройка GitHub Secrets (для автодеплоя)
В репо: Settings → Secrets and variables → Actions:
`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` (приватный ключ), `VPS_PATH` (путь к клону).
Ключ генерится на сервере: `ssh-keygen -t ed25519 -f vilka_deploy`, публичную часть — в `~/.ssh/authorized_keys`.

### Миграции БД
Схема поднимается идемпотентным `create_all` (startup бэкенда + шаг CI). При появлении breaking-изменений подключить alembic:
`docker compose -f docker-compose.prod.yml exec backend alembic init migrations && alembic revision --autogenerate && alembic upgrade head`.

### Auth на фронте (ЭТАП 6)
- `frontend/src/useAuth.ts` — хук `useAuth()`: login/register/logout/checkout/refresh, JWT хранится в localStorage (`vilka_jwt`), клиентская проверка `exp`.
- `src/api.ts`: все запросы (enrich/spider/path/export) автоматически несут `Authorization: Bearer`; WebSocket получает токен query-параметром `?token=` (бэк валидирует его и рвёт сессию кодом 4401 при мусорном токене).
- Режим SaaS включён по умолчанию (`VILKA_AUTH_ENFORCED=1`): анонимный enrich → 401, пустой баланс → 402 Payment Required.
