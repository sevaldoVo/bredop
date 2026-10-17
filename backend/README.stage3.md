# ЭТАП 3 — Spider, MTProto, аналитика графа

## Новые env-переменные
VILKA_TELETHON_ENABLED=1        # включить MTProto-канал (нужны ключи ниже)
VILKA_TG_API_ID / VILKA_TG_API_HASH   # из my.telegram.org
VILKA_TG_SESSION=vilka_tg.session      # сессия Telethon
VILKA_SPIDER_DELAY=0.8          # пауза между enrich-вызовами спайдера (rate-limit)
VILKA_SPIDER_MAX_NODES=60       # потолок нод на одну раскрутку

## Эндпоинты
POST /api/spider {start_node_id, depth<=5, graph:{nodes,links}} -> 202 {job_id}
GET  /api/spider/{job_id}      -> статус + результат джобы
GET/POST /api/path?from_id&to_id -> кратчайший путь (networkx; POST принимает снимок доски)
WS   /ws/enrich {"action":"spider",...} -> стрим spider_started/enriching/found/skipped/spider_done
