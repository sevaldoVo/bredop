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
