# Пакет для внутреннего сервера

Каталог содержит проверенную поставку дашборда для передачи сотруднику, который будет
разворачивать решение в локальном контуре банка.

## Файлы

- `black-swan-dashboard-internal-20261028.tar.gz` — полный архив приложения.
- `black-swan-dashboard-internal-20261028.tar.gz.sha256` — контрольная сумма SHA-256.

Ожидаемая SHA-256:
`bcece28ccd86bf48f6efc260631d7750a4dd5d07d7a16a965fef10b260957baf`.

Пакет собран из версии `83bbcd9`. Истории индексов обновлены по последнюю завершенную
неделю 25 октября 2026 года; файл `Blackswan.xlsx` содержит рыночные наблюдения по
27 октября 2026 года.

## Проверка и распаковка

```bash
sha256sum -c black-swan-dashboard-internal-20261028.tar.gz.sha256
mkdir black-swan-dashboard
tar -xzf black-swan-dashboard-internal-20261028.tar.gz -C black-swan-dashboard
cd black-swan-dashboard
deployment/validate-release.sh .
```

После проверки следуйте документу
`deployment/internal-deployment-guide.md` внутри архива. Он содержит требования к
Linux, Node.js, Python, Nginx, systemd, сетевому доступу, proxy/CA, обновлению данных,
мониторингу, резервному копированию и откату.

Секреты, рабочие `.env`, кэши источников и журналы в поставку не включены. Их должен
создать администратор внутреннего контура по шаблону
`deployment/black-swan.env.example`.
