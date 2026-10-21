# Kazakhstan Black Swan Risk Dashboard

Репозиторий содержит готовый дашборд, сервер аналитического комментария и
обновления данных, исходные наборы, расчетные скрипты и комплект развертывания
во внутреннем контуре банка.

## Основные каталоги

- `dashboard/` — автономная HTML-страница с тремя индексами.
- `server/` — Node.js API и интеграция с OpenAI-совместимой моделью.
- `research/` — Excel-источники, расчетные скрипты, история и бэк-тесты.
- `deployment/` — Nginx, systemd, переменные окружения и эксплуатационные скрипты.

Полная инструкция: [`deployment/internal-deployment-guide.md`](deployment/internal-deployment-guide.md).

Быстрая локальная проверка:

```bash
cd server
OPENAI_MODEL=approved-model-name \
OPENAI_BASE_URL=https://ai-gateway.bank.local/v1 \
npm start
```

После запуска интерфейс доступен по `http://127.0.0.1:8080/`.

Публичная демонстрационная версия размещена на GitHub Pages. Статический хостинг
не запускает внутренние API; кнопки анализа и обновления данных работают только
через `server/server.mjs`. Обновление использует открытые источники и серверные
пути `BLOOMBERG_WORKBOOK`/`REOK_WORKBOOK`, поэтому Excel-файлы не передаются в
браузер.
