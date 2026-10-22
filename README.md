# Kazakhstan Black Swan Risk Dashboard

Репозиторий содержит готовый дашборд, импорт списка Black Swan событий из Word,
сервер обновления данных, исходные наборы, расчетные скрипты и комплект развертывания
во внутреннем контуре банка.

## Основные каталоги

- `dashboard/` — автономная HTML-страница с тремя индексами.
- `server/` — Node.js API для страницы и обновления расчетных данных.
- `research/` — Excel-источники, расчетные скрипты, история и бэк-тесты.
- `deployment/` — Nginx, systemd, переменные окружения и эксплуатационные скрипты.

Полная инструкция: [`deployment/internal-deployment-guide.md`](deployment/internal-deployment-guide.md).

Архив для передачи во внутренний контур вместе с SHA-256 формируется так:

```bash
deployment/build-transfer-package.sh /tmp/black-swan-dashboard-transfer.tar.gz
```

Быстрая локальная проверка:

```bash
cd server
npm start
```

После запуска интерфейс доступен по `http://127.0.0.1:8080/`.

Публичная версия размещена на GitHub Pages. Импорт `.docx` работает локально в
браузере и не передает документ на сервер. Серверное обновление индексов доступно
только через `server/server.mjs` и использует пути
`BLOOMBERG_WORKBOOK`/`REOK_WORKBOOK`, поэтому Excel-файлы не передаются в браузер.
