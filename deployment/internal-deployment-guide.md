# Развертывание Black Swan Risk Dashboard в локальном контуре

## 1. Назначение документа

Документ описывает рекомендуемую промышленную архитектуру, первичную установку,
сетевые потоки, подключение AI, обновление данных, мониторинг, резервное
копирование и откат. Базовый вариант рассчитан на Linux-сервер с Nginx и systemd.

## 2. Состав решения

| Слой | Технология | Назначение |
|---|---|---|
| Интерфейс | HTML5, CSS, JavaScript, SVG | Три индекса, исторические срезы и AI-комментарий |
| Веб/API | Node.js 20+ без внешних npm-зависимостей | Статика, `/api/ai-analysis`, валидация и кэш |
| Reverse proxy | Nginx 1.24+ или корпоративный аналог | TLS, SSO, ACL, лимиты и журнал доступа |
| Расчеты | Python 3.11+ | Обновление индексов и бэк-тестов |
| Python-пакеты | NumPy, pandas, scikit-learn, openpyxl | Excel, нормирование и метрики |
| Операционные данные | JSON и XLSX | История индексов и входные Bloomberg/РЭОК |
| AI | OpenAI Responses API или совместимый внутренний шлюз | Аналитический комментарий по фиксированному промпту |
| Хранилище ответа | JSON-файл с правами `0600` | Последний сформированный комментарий |

Сборка фронтенда не требуется: данные встроены в автономный HTML расчетными
скриптами. В браузер не передаются AI-токен, системный промпт и адрес поставщика.

## 3. Целевая архитектура

```text
Рабочая станция сотрудника
        |
        | HTTPS + корпоративный SSO
        v
Внутренний reverse proxy / WAF
        |
        | HTTP 127.0.0.1:8080
        v
Node.js dashboard service
   |                 |
   | read-only       | HTTPS, только сервер-сервер
   v                 v
HTML + JSON       Внутренний AI gateway
                     |
                     +--> утвержденная LLM
                     +--> утвержденный веб-поиск

Отдельный ETL/расчетный узел
   |
   +--> Bloomberg XLSX / РЭОК
   +--> разрешенные открытые источники
   +--> staging release -> проверка -> атомарная публикация
```

Интерактивный сервис и расчетный процесс следует разделять. Расчеты выполняются
в staging-копии, после проверки создается новый неизменяемый релиз. Продуктивный
процесс не должен изменять формулы или данные во время пользовательского запроса.

## 4. Рекомендуемые ресурсы

Для одного экземпляра и до 100 одновременных пользователей:

- 2 vCPU;
- 4 ГБ RAM;
- 20 ГБ системного диска;
- 20–50 ГБ для релизов, кэшей источников и журналов;
- Linux x86_64, Node.js 20/22 LTS, Python 3.11+, Nginx 1.24+.

AI-вычисления выполняются внешним или внутренним шлюзом, поэтому GPU на сервере
дашборда не нужен. Для локальной LLM ресурсы рассчитываются отдельно ее владельцем.

## 5. Сетевые режимы

### 5.1. Рекомендуемый: внутренний AI gateway

`OPENAI_BASE_URL` указывает на OpenAI-совместимый шлюз банка. Шлюз отвечает за
идентификацию сервиса, DLP, журналирование, квоты, выбор модели и поиск. Сервер
дашборда не имеет прямого доступа в интернет.

### 5.2. Контролируемый внешний доступ

Сервер обращается к утвержденному внешнему API через корпоративный HTTPS proxy.
Необходимо согласовать передачу среза индексов и текста промпта за периметр.
Ключ хранится в Vault/секрет-хранилище или в защищенном EnvironmentFile, но не в
HTML, Git и журнале команд.

Для Node.js через proxy используются `NODE_USE_ENV_PROXY=1`, `HTTPS_PROXY`,
`NO_PROXY`. Корпоративный корневой сертификат задается через
`NODE_EXTRA_CA_CERTS`.

### 5.3. Полностью изолированный контур

Нужен внутренний OpenAI-совместимый endpoint. Если промпт должен выполнять
актуальный поиск, шлюз обязан поддерживать `web_search` либо внутренний RAG/search
service. При `AI_ENABLE_WEB_SEARCH=false` кнопка технически работает, но требование
промпта об актуальном поиске не выполняется; это должно быть явно отражено в
регламенте.

## 6. Разрешенные соединения

### Интерактивный сервер

| Направление | Порт | Назначение |
|---|---:|---|
| Пользователи -> reverse proxy | 443/TCP | Интерфейс и API |
| Reverse proxy -> Node.js | 8080/TCP localhost | Внутренний upstream |
| Node.js -> AI gateway | 443/TCP | Генерация комментария |
| Мониторинг -> `/health`, `/ready` | 443/TCP | Liveness/readiness |

### Расчетный узел при обновлении из интернета

Разрешаются только необходимые FQDN по 443/TCP:

- `query1.finance.yahoo.com`;
- `news.google.com`;
- `www.federalreserve.gov`;
- `www.ecb.europa.eu`;
- `www.who.int`;
- `xmart-api-public.who.int`;
- `flood-api.open-meteo.com`.

Если открытые источники зеркалируются внутри банка, URL в скриптах следует
заменить внутренними endpoint, а прямой интернет-доступ отключить.

## 7. Структура поставки

```text
black-swan-risk-dashboard/
├── dashboard/black-swan-dashboard.html
├── server/
│   ├── server.mjs
│   ├── package.json
│   └── prompts/analysis-prompt.txt
├── research/
│   ├── Blackswan.xlsx
│   ├── REOK.xlsx
│   ├── requirements.txt
│   ├── build_*.py
│   ├── *-history.json
│   └── *-backtest.*
└── deployment/
    ├── black-swan.env.example
    ├── nginx/black-swan-dashboard.conf
    ├── systemd/black-swan-dashboard.service
    ├── refresh-data.sh
    └── validate-release.sh
```

Каталоги `.cache`, `__pycache__`, локальный AI-кэш и реальные секреты в поставку
не включаются.

## 8. Первичная установка

Ниже используется пользователь `black-swan`, каталог релизов
`/opt/black-swan-dashboard` и состояние `/var/lib/black-swan-dashboard`.

### 8.1. Системный пользователь и каталоги

```bash
sudo groupadd --system black-swan
sudo useradd --system --gid black-swan \
  --home /var/lib/black-swan-dashboard/runtime \
  --shell /usr/sbin/nologin black-swan
sudo useradd --system --gid black-swan \
  --home /var/lib/black-swan-dashboard/source-cache \
  --shell /usr/sbin/nologin data-operator
sudo install -d -o root -g root -m 0755 /opt/black-swan-dashboard/releases
sudo install -d -o root -g black-swan -m 0750 /var/lib/black-swan-dashboard
sudo install -d -o black-swan -g black-swan -m 0700 \
  /var/lib/black-swan-dashboard/runtime
sudo install -d -o data-operator -g black-swan -m 0750 \
  /var/lib/black-swan-dashboard/source-cache
sudo install -d -o root -g black-swan -m 0750 /etc/black-swan-dashboard
```

### 8.2. Установка релиза

Передавайте архив через утвержденный внутренний репозиторий артефактов и
проверяйте SHA-256. Не клонируйте публичный GitHub непосредственно из production.

```bash
RELEASE=2026-10-21.1
sudo install -d -o root -g root -m 0755 \
  "/opt/black-swan-dashboard/releases/$RELEASE"
sudo cp -a dashboard server research deployment \
  "/opt/black-swan-dashboard/releases/$RELEASE/"
sudo chown -R root:root "/opt/black-swan-dashboard/releases/$RELEASE"
sudo find "/opt/black-swan-dashboard/releases/$RELEASE" -type d -exec chmod 0755 {} \;
sudo find "/opt/black-swan-dashboard/releases/$RELEASE" -type f -exec chmod 0644 {} \;
sudo chmod 0755 "/opt/black-swan-dashboard/releases/$RELEASE/deployment/"*.sh
sudo ln -sfn "/opt/black-swan-dashboard/releases/$RELEASE" \
  /opt/black-swan-dashboard/current
```

### 8.3. Python-окружение для расчетного узла

На интерактивном сервере Python не обязателен. На расчетном узле:

```bash
sudo python3 -m venv /opt/black-swan-dashboard/venv
sudo /opt/black-swan-dashboard/venv/bin/pip install --no-index \
  --find-links /path/to/internal/wheelhouse \
  -r "/opt/black-swan-dashboard/releases/$RELEASE/research/requirements.txt"
```

Пакеты следует получать из внутреннего PyPI/wheelhouse после проверки ИБ.

### 8.4. Переменные и секреты

```bash
sudo cp deployment/black-swan.env.example \
  /etc/black-swan-dashboard/black-swan.env
sudo chown root:black-swan /etc/black-swan-dashboard/black-swan.env
sudo chmod 0640 /etc/black-swan-dashboard/black-swan.env
sudoedit /etc/black-swan-dashboard/black-swan.env
```

Обязательные параметры:

- `OPENAI_BASE_URL` — разрешенный endpoint;
- `OPENAI_MODEL` — утвержденная модель;
- `OPENAI_API_KEY` — токен, если его требует шлюз;
- `AI_API_MODE` — `responses` либо `chat-completions`;
- `AI_ENABLE_WEB_SEARCH` — `true`, если шлюз поддерживает поиск.

Не передавайте ключ через `Environment=` в публичном unit-файле. Предпочтительно
получать его из Vault при запуске; EnvironmentFile является запасным вариантом.

### 8.5. systemd

```bash
sudo cp deployment/systemd/black-swan-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now black-swan-dashboard
sudo systemctl status black-swan-dashboard
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/ready
```

`/health` подтверждает работу процесса. `/ready` возвращает HTTP 200 только при
доступности HTML, непустого промпта и заданного `OPENAI_MODEL`. Endpoint не делает
платный запрос модели.

### 8.6. TLS, DNS и SSO

1. Создайте внутреннюю DNS-запись, например `black-swan.bank.local`.
2. Выпустите сертификат корпоративного УЦ.
3. Скорректируйте `server_name` и пути сертификатов в Nginx-конфигурации.
4. Подключите утвержденный SSO/OIDC/Kerberos-модуль на reverse proxy.
5. Ограничьте доступ группой аналитиков и руководителей.
6. До подключения SSO не публикуйте сервис за пределы административной подсети.

```bash
sudo cp deployment/nginx/black-swan-dashboard.conf /etc/nginx/conf.d/
sudo nginx -t
sudo systemctl reload nginx
```

Node.js слушает только `127.0.0.1`; пользователи не должны обращаться к порту
8080 напрямую.

## 9. Проверка после установки

```bash
curl -fsS https://black-swan.bank.local/health
curl -fsS https://black-swan.bank.local/ready
curl -fsS -o /dev/null -w '%{http_code}\n' \
  https://black-swan.bank.local/
```

Функциональная приемка:

1. Открываются все три блока индексов.
2. Каждый ползунок достигает первой и последней даты.
3. Композит пересчитывается при движении ползунка.
4. Кнопка AI возвращает только нумерованный список.
5. После перезагрузки показывается сохраненный ответ.
6. Неавторизованный пользователь получает отказ на reverse proxy.
7. В HTML и DevTools отсутствуют API-токен и системный промпт.

## 10. Обновление данных

Расчет выполняется не в `current`, а в новой staging-копии:

```bash
RELEASE="$(date -u +%Y%m%d-%H%M%S)"
STAGE="/opt/black-swan-dashboard/releases/$RELEASE"
sudo install -d -o root -g root -m 0755 "$STAGE"
sudo cp -aL /opt/black-swan-dashboard/current/. "$STAGE/"
sudo chown -R data-operator:black-swan "$STAGE"
```

Затем в `research/` заменяются утвержденные Bloomberg `Blackswan.xlsx` и РЭОК
`REOK.xlsx`, после чего запускается:

```bash
sudo -u data-operator \
  PYTHON=/opt/black-swan-dashboard/venv/bin/python \
  CACHE_ROOT=/var/lib/black-swan-dashboard/source-cache \
  "$STAGE/deployment/refresh-data.sh" "$STAGE"
```

Порядок расчета зафиксирован в скрипте:

1. экономические новостные нарративы;
2. коммуникации ФРС и ЕЦБ;
3. экономический индекс и его бэк-тест;
4. рыночный модуль Россия–Украина;
5. политический индекс;
6. природно-биологический индекс и его бэк-тест;
7. валидация релиза.

После аналитического контроля владельцем методологии:

```bash
sudo chown -R root:root "$STAGE"
sudo ln -sfn "$STAGE" /opt/black-swan-dashboard/current
sudo systemctl restart black-swan-dashboard
curl -fsS http://127.0.0.1:8080/ready
```

Рекомендуемая частота — еженедельно после закрытия недели. Месячные показатели
переносятся вперед внутри месяца по текущей методологии публикационных лагов.

## 11. Обновление приложения и откат

Каждая версия хранится отдельным неизменяемым каталогом. Для отката:

```bash
sudo ln -sfn /opt/black-swan-dashboard/releases/PREVIOUS_RELEASE \
  /opt/black-swan-dashboard/current
sudo systemctl restart black-swan-dashboard
curl -fsS http://127.0.0.1:8080/ready
```

Храните минимум три последних принятых релиза. Не изменяйте старый релиз после
публикации.

## 12. Резервное копирование

Резервируются:

- `/etc/black-swan-dashboard/` без выгрузки секрета в обычный backup;
- `/var/lib/black-swan-dashboard/latest-analysis.json`;
- принятые XLSX и итоговые JSON/HTML каждого релиза;
- журнал версий формул и результаты бэк-тестов;
- конфигурация reverse proxy и идентификаторы сертификатов.

Кэши открытых источников можно восстанавливать повторной загрузкой, но для
аудита желательно хранить их согласно политике банка.

## 13. Мониторинг и журналы

Минимальные проверки:

- `/health`: каждые 30 секунд, тревога после трех ошибок;
- `/ready`: каждую минуту;
- время и код ответа `POST /api/ai-analysis`;
- свободное место `/var/lib/black-swan-dashboard`;
- возраст последнего принятого релиза;
- срок действия TLS-сертификата;
- число HTTP 429/5xx на reverse proxy.

Логи процесса доступны через:

```bash
journalctl -u black-swan-dashboard --since today
```

Не журналируйте API-токен, полный системный промпт, содержимое AI-запроса и ответ,
если для этого нет отдельного утвержденного защищенного аудита.

## 14. Контроль безопасности

- Доступ только по HTTPS и через корпоративный SSO.
- Node.js не публикуется напрямую в сеть.
- Токен находится в Vault либо файле `0640`, никогда в Git/HTML.
- Исходящий доступ ограничен AI gateway и утвержденными источниками.
- Reverse proxy ограничивает размер запроса и частоту AI-вызовов.
- Релиз принадлежит `root` и доступен сервису только на чтение.
- Состояние сервиса записывается только в `/var/lib/black-swan-dashboard`.
- Обновления Node.js, Python и библиотек проходят внутренний vulnerability scan.
- XLSX и JSON перед публикацией проходят антивирусную и содержательную проверку.
- AI-ответ является аналитическим материалом, а не автоматическим решением.

## 15. Ограничения текущей версии

Перед признанием системы критичной следует учесть:

1. Приложение не содержит собственной аутентификации; она обязательна на proxy.
2. Последний AI-ответ хранится в локальном JSON, что рассчитано на один экземпляр.
   Для HA нужен PostgreSQL/Redis или общее защищенное хранилище.
3. Срез индексов передается браузером. Сервер проверяет структуру и диапазоны, но
   не сверяет значения с серверным источником. Для строгой целостности следует
   формировать AI-контекст на сервере по выбранным датам.
4. Источники Google News/Yahoo не имеют банковского SLA. Для промышленного режима
   предпочтительны лицензированные или внутренне архивируемые источники.
5. Веб-поиск зависит от возможностей AI gateway. Без него требование промпта об
   актуальности не гарантируется.
6. Текущий AI-кэш хранит только последний ответ и не является полноценным
   аудиторским журналом. Для аудита нужны request ID, дата среза, версия промпта,
   модель, хэш входных данных и защищенное хранение результата.

## 16. Критерии готовности к production

- Архитектура и потоки данных согласованы ИБ и владельцем данных.
- Определен режим AI и правовое основание передачи данных.
- Настроены DNS, TLS, SSO, RBAC и сетевые ACL.
- Секрет выдается через утвержденное хранилище.
- Проверены `health`, `ready`, лимиты и тайм-ауты.
- Выполнена приемка формул и контрольных дат владельцем методологии.
- Настроен еженедельный staging-процесс и четырехглазный контроль публикации.
- Проведены тест восстановления и отката.
- Назначены владельцы приложения, данных, модели и инфраструктуры.
