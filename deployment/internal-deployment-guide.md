# Развертывание Black Swan Risk Dashboard в локальном контуре

## 1. Назначение документа

Документ описывает рекомендуемую промышленную архитектуру, первичную установку,
сетевые потоки, импорт списка событий из Word, обновление данных, мониторинг, резервное
копирование и откат. Базовый вариант рассчитан на Linux-сервер с Nginx и systemd.

## 2. Состав решения

| Слой | Технология | Назначение |
|---|---|---|
| Интерфейс | HTML5, CSS, JavaScript, SVG | Три индекса, исторические срезы, обновление и локальный импорт `.docx` |
| Веб/API | Node.js 20+ без внешних npm-зависимостей | Статика, `/api/data-refresh` и валидация состояния |
| Reverse proxy | Nginx 1.24+ или корпоративный аналог | TLS, SSO, ACL, лимиты и журнал доступа |
| Расчеты | Python 3.11+ | Обновление индексов и бэк-тестов |
| Python-пакеты | NumPy, pandas, scikit-learn, openpyxl | Excel, нормирование и метрики |
| Операционные данные | JSON и XLSX | История индексов и входные Bloomberg/РЭОК |
| Файл событий | Word `.docx`, не более 5 МБ | Готовый нумерованный список Black Swan событий |
| Хранилище списка | Web Storage браузера | Последний принятый текст для текущего профиля пользователя |

Сборка фронтенда не требуется: данные встроены в автономный HTML расчетными
скриптами. Word-документ разбирается локально и не передается на сервер.

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
   |
   v
HTML + JSON
   ^
   | контролируемый расчет по кнопке оператора
   |
Python ETL --> открытые источники + Bloomberg XLSX / РЭОК

Отдельный ETL/расчетный узел
   |
   +--> Bloomberg XLSX / РЭОК
   +--> разрешенные открытые источники
   +--> staging release -> проверка -> атомарная публикация
```

В комплекте реализован односерверный режим: кнопка отправляет асинхронный запрос,
Node.js запускает фиксированный `refresh-data.sh`, а браузер опрашивает статус и
перезагружает готовую страницу. POST-доступ следует дать только группе операторов
данных. В критичном production-контуре вместо прямого запуска укажите в
`DATA_REFRESH_SCRIPT` утвержденный job broker, который считает staging-копию и
публикует ее после контроля.

## 4. Рекомендуемые ресурсы

Для одного экземпляра и до 100 одновременных пользователей:

- 2 vCPU;
- 4 ГБ RAM;
- 20 ГБ системного диска;
- 20–50 ГБ для релизов, кэшей источников и журналов;
- Linux x86_64, Node.js 20/22 LTS, Python 3.11+, Nginx 1.24+.

GPU и AI-шлюз серверу дашборда не требуются.

## 5. Сетевые режимы

### 5.1. Импорт Word-документа

Кнопка `Обновить анализ` открывает системный выбор файла. Браузер принимает только
`.docx`, проверяет внутреннюю ZIP/XML-структуру и наличие списка минимум из трех
событий. Текст сохраняется в Web Storage текущего браузерного профиля. Содержимое
файла не проходит через reverse proxy или Node.js.

### 5.2. Доступ к открытым источникам

Исходящий HTTPS-доступ требуется только расчетному процессу обновления индексов.
Для Node.js/Python через proxy используются `NODE_USE_ENV_PROXY=1`, `HTTPS_PROXY`,
`NO_PROXY`; корпоративный корневой сертификат задается через
`NODE_EXTRA_CA_CERTS`.

## 6. Разрешенные соединения

### Интерактивный сервер

| Направление | Порт | Назначение |
|---|---:|---|
| Пользователи -> reverse proxy | 443/TCP | Интерфейс и API |
| Reverse proxy -> Node.js | 8080/TCP localhost | Внутренний upstream |
| Node.js/Python -> открытые источники | 443/TCP | Обновление индексов по кнопке |
| Мониторинг -> `/health`, `/ready` | 443/TCP | Liveness/readiness |

### Расчетный узел при обновлении из интернета

Разрешаются только необходимые FQDN по 443/TCP:

- `query1.finance.yahoo.com`;
- `data.gdeltproject.org`;
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
│   └── package.json
├── research/
│   ├── Blackswan.xlsx
│   ├── REOK.xlsx
│   ├── requirements.txt
│   ├── build_*.py
│   ├── gdelt-economic-narrative-history.json
│   ├── *-history.json
│   └── *-backtest.*
└── deployment/
    ├── black-swan.env.example
    ├── nginx/black-swan-dashboard.conf
    ├── systemd/black-swan-dashboard.service
    ├── build-transfer-package.sh
    ├── refresh-data.sh
    └── validate-release.sh
```

Каталоги `.cache`, `__pycache__`, локальные runtime-кэши, прежние AI-промпты и
реальные секреты в поставку не включаются. Проверенный архив для передачи и его
контрольную сумму можно сформировать командой:

```bash
deployment/build-transfer-package.sh /tmp/black-swan-dashboard-transfer.tar.gz
```

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
sudo install -d -o black-swan -g black-swan -m 0750 \
  /var/lib/black-swan-dashboard/source-cache
sudo install -d -o data-operator -g black-swan -m 0750 \
  /var/lib/black-swan-dashboard/input
sudo install -d -o root -g black-swan -m 0750 /etc/black-swan-dashboard
```

### 8.2. Установка релиза

Передавайте архив через утвержденный внутренний репозиторий артефактов и
проверяйте SHA-256. Не клонируйте публичный GitHub непосредственно из production.

```bash
RELEASE=2026-10-22.1
TRANSFER=/approved/inbox/black-swan-dashboard-transfer.tar.gz
(cd "$(dirname "$TRANSFER")" && sha256sum -c "$(basename "$TRANSFER").sha256")
sudo install -d -o root -g root -m 0755 \
  "/opt/black-swan-dashboard/releases/$RELEASE"
sudo tar -xzf "$TRANSFER" -C "/opt/black-swan-dashboard/releases/$RELEASE"
sudo chown -R root:root "/opt/black-swan-dashboard/releases/$RELEASE"
sudo find "/opt/black-swan-dashboard/releases/$RELEASE" -type d -exec chmod 0755 {} \;
sudo find "/opt/black-swan-dashboard/releases/$RELEASE" -type f -exec chmod 0644 {} \;
sudo chmod 0755 "/opt/black-swan-dashboard/releases/$RELEASE/deployment/"*.sh
sudo chgrp black-swan \
  "/opt/black-swan-dashboard/releases/$RELEASE/dashboard/black-swan-dashboard.html" \
  "/opt/black-swan-dashboard/releases/$RELEASE/research/"*.json \
  "/opt/black-swan-dashboard/releases/$RELEASE/research/"*.md
sudo chmod 0664 \
  "/opt/black-swan-dashboard/releases/$RELEASE/dashboard/black-swan-dashboard.html" \
  "/opt/black-swan-dashboard/releases/$RELEASE/research/"*.json \
  "/opt/black-swan-dashboard/releases/$RELEASE/research/"*.md
sudo ln -sfn "/opt/black-swan-dashboard/releases/$RELEASE" \
  /opt/black-swan-dashboard/current
```

Перед запуском загрузите проверенные файлы `Blackswan.xlsx` и `REOK.xlsx` в
`/var/lib/black-swan-dashboard/input/`, назначьте владельца
`data-operator:black-swan` и права `0640`.

### 8.3. Python-окружение для расчетного узла

При обновлении по кнопке Python устанавливается на том же сервере:

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

- `DATA_REFRESH_ENABLED=true` — включает серверное обновление по кнопке;
- `DATA_REFRESH_SCRIPT` и `DATA_REFRESH_ROOT` — фиксированный сценарий и корень поставки;
- `BLOOMBERG_WORKBOOK` и `REOK_WORKBOOK` — серверные пути к проверенным XLSX;
- `PYTHON` и `CACHE_ROOT` — Python-окружение и рабочий кэш источников.

Не размещайте пароли к источникам и иные секреты в публичном unit-файле.

### 8.5. systemd

```bash
sudo cp deployment/systemd/black-swan-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now black-swan-dashboard
sudo systemctl status black-swan-dashboard
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/ready
```

`/health` подтверждает работу процесса. `/ready` возвращает HTTP 200 при
доступности HTML.

### 8.6. TLS, DNS и SSO

1. Создайте внутреннюю DNS-запись, например `black-swan.bank.local`.
2. Выпустите сертификат корпоративного УЦ.
3. Скорректируйте `server_name` и пути сертификатов в Nginx-конфигурации.
4. Подключите утвержденный SSO/OIDC/Kerberos-модуль на reverse proxy.
5. Ограничьте просмотр группой аналитиков и руководителей, а `POST /api/data-refresh` отдельной ролью оператора данных.
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
4. Кнопка `Обновить анализ` принимает корректный `.docx` с нумерованным списком и отклоняет `.doc`, `.txt`, поврежденный или неподходящий документ.
5. После перезагрузки показывается локально сохраненный список и имя исходного файла.
6. Кнопка в шапке обновляет открытые источники, использует серверный Bloomberg XLSX и после завершения показывает новый срез.
7. Пользователь без роли оператора не может выполнить `POST /api/data-refresh`.
8. Неавторизованный пользователь получает отказ на reverse proxy.
9. Word-документ не появляется в сетевых запросах браузера; в HTML и DevTools отсутствуют XLSX и секреты источников.

## 10. Обновление данных

Кнопка в шапке вызывает `POST /api/data-refresh`. Сервер немедленно возвращает
HTTP 202, выполняет один расчетный процесс и публикует состояние через
`GET /api/data-refresh`. Повторный POST во время расчета возвращает HTTP 409.

В односерверном режиме установленный сценарий запускается так:

```bash
sudo -u black-swan \
  PYTHON=/opt/black-swan-dashboard/venv/bin/python \
  CACHE_ROOT=/var/lib/black-swan-dashboard/source-cache \
  BLOOMBERG_WORKBOOK=/var/lib/black-swan-dashboard/input/Blackswan.xlsx \
  REOK_WORKBOOK=/var/lib/black-swan-dashboard/input/REOK.xlsx \
  /opt/black-swan-dashboard/current/deployment/refresh-data.sh \
  /opt/black-swan-dashboard/current
```

Перед приемкой кнопки проверьте статус напрямую:

```bash
curl -fsS http://127.0.0.1:8080/api/data-refresh
```

Порядок расчета зафиксирован в скрипте:

1. экономические новостные нарративы: архивный GDELT-прокси до июля 2024 года и Google News RSS после;
2. коммуникации ФРС и ЕЦБ;
3. экономический индекс и его бэк-тест;
4. рыночный модуль Россия–Украина;
5. политический индекс;
6. природно-биологический индекс и его бэк-тест;
7. валидация релиза.

Для production с обязательным четырехглазным контролем замените
`DATA_REFRESH_SCRIPT` на утвержденный исполняемый broker без пользовательских
аргументов. Он должен создать staging-релиз, выполнить те же расчеты и проверки,
атомарно опубликовать результат; Node.js не принимает путь или команду из HTTP.

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
- `/var/lib/black-swan-dashboard/runtime/data-refresh.json`;
- принятые XLSX и итоговые JSON/HTML каждого релиза;
- журнал версий формул и результаты бэк-тестов;
- конфигурация reverse proxy и идентификаторы сертификатов.

Кэши открытых источников можно восстанавливать повторной загрузкой, но для
аудита желательно хранить их согласно политике банка.

## 13. Мониторинг и журналы

Минимальные проверки:

- `/health`: каждые 30 секунд, тревога после трех ошибок;
- `/ready`: каждую минуту;
- статус и длительность `POST /api/data-refresh`, возраст `latestDataDate`;
- свободное место `/var/lib/black-swan-dashboard`;
- возраст последнего принятого релиза;
- срок действия TLS-сертификата;
- число HTTP 429/5xx на reverse proxy.

Логи процесса доступны через:

```bash
journalctl -u black-swan-dashboard --since today
```

Word-документ обрабатывается локально, поэтому его содержимое не должно появляться
в серверных журналах.

## 14. Контроль безопасности

- Доступ только по HTTPS и через корпоративный SSO.
- Node.js не публикуется напрямую в сеть.
- Исходящий доступ ограничен утвержденными источниками расчетных данных.
- Reverse proxy ограничивает размер запросов и частоту запуска обновлений.
- Формулы и исполняемый код релиза принадлежат `root`; сервис может изменять только расчетные JSON/MD и HTML.
- Входные XLSX хранятся вне Git в `/var/lib/black-swan-dashboard/input` и доступны сервису только на чтение.
- POST обновления разрешен только роли оператора данных и запускает фиксированный серверный сценарий.
- Обновления Node.js, Python и библиотек проходят внутренний vulnerability scan.
- XLSX и JSON перед публикацией проходят антивирусную и содержательную проверку.
- Импортированный список является аналитическим материалом, а не автоматическим решением.

## 15. Ограничения текущей версии

Перед признанием системы критичной следует учесть:

1. Приложение не содержит собственной аутентификации; она обязательна на proxy.
2. Последний импортированный список хранится в Web Storage конкретного браузерного
   профиля и не синхронизируется между рабочими местами.
3. Браузер поддерживает только современный формат `.docx`; старый бинарный `.doc`
   необходимо предварительно пересохранить в Word как `.docx`.
4. Источники Google News/Yahoo не имеют банковского SLA. Для промышленного режима
   предпочтительны лицензированные или внутренне архивируемые источники.
5. Web Storage не является аудиторским журналом. Для централизованного хранения
   утвержденных списков потребуется отдельный серверный реестр документов.
7. Базовый односерверный режим обновляет расчетные файлы текущего релиза. Если
   политика банка требует неизменяемые релизы и предварительное утверждение,
   подключите staging/job broker через `DATA_REFRESH_SCRIPT`.

## 16. Критерии готовности к production

- Архитектура и потоки данных согласованы ИБ и владельцем данных.
- Настроены DNS, TLS, SSO, RBAC и сетевые ACL.
- Проверены `health`, `ready`, лимиты и тайм-ауты.
- Выполнена приемка формул и контрольных дат владельцем методологии.
- Настроено обновление по кнопке либо staging-процесс с четырехглазным контролем публикации.
- Проведены тест восстановления и отката.
- Назначены владельцы приложения, данных и инфраструктуры.
