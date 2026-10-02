# TeamCTF

Код разделён на два сервиса:

- [`portal/`](portal/README.md) — backend, frontend, миграции и запуск доверенного Portal.
- [`arena/`](arena/README.md) — Agent, Docker runtime, проверки и игровые `cases/`.

`shared/` содержит общие контракты без дублирования; `tests/` — общие проверки.
База остаётся в `data/`. Корневые Compose/Alembic-конфиги сохраняют прежние команды.
После обновления структуры переустановите editable-пакет:
`.venv/bin/pip install -e '.[test,browser]'`.

Локальная Red vs Blue CTF-платформа. Реализованы backend, cases, Docker arenas,
SSH-доступ BLUE, проверки защиты, выдача ключа BLUE и веб-лобби.
Stage 10 добавляет автоматический подбор 2 RED / 2 BLUE и provisioning через ArenaProvider.

**Исходный и расширенный checkpoint 8A завершены.** Host-проверка: 167 passed,
1 skipped (Chromium ранее проверен локально).
Новый patch добавляет `/v1`, Portal-only firewall и ограниченный пул игровых портов.
Локально: 161 passed, 7 Docker skipped. Stage 8–9 сохранены. Развёртывание двух серверов,
TLS, firewall, новый async provider API и проверка описаны в [arena/README.md](arena/README.md).
Ниже разделы Stage 3–8 описывают исходное поведение; исполнение теперь находится
в Arena, а controller означает Portal, обращающийся через provider.

## Запуск

Нужны Docker Engine и Docker Compose:

```bash
docker compose up --build -d --wait
curl --fail http://localhost:8000/health
```

Ожидаемый ответ: `{"status":"ok"}`. При недоступности БД endpoint возвращает 503.
Миграции выполняются перед каждым запуском backend; ошибка миграции прерывает
запуск. SQLite сохраняется в `data/cyberrange.db` и переживает перезапуск.
Backend доступен только на localhost:8000.
Откройте http://localhost:8000/ в браузере — здесь находится лобби.

```bash
docker compose down
```

## Тесты и миграции

Полная проверка checkpoint 1 (запускает и затем останавливает Compose):

```bash
bash tests/checkpoint_01.sh
```

Если доступ к Docker есть только у root, выполнить через `sudo bash`.

```bash
docker compose run --rm --no-deps backend python -m pytest -q
docker compose run --rm --no-deps backend alembic upgrade head
```

Локальная разработка (Python 3.12+):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/alembic upgrade head
.venv/bin/uvicorn portal.app.main:app --reload
.venv/bin/python -m pytest -q
```

`DATABASE_URL` переопределяет путь к SQLite (по умолчанию
`sqlite:///data/cyberrange.db`); родительский каталог должен существовать.
Тесты используют временные БД и не изменяют данные приложения.

Новая миграция после изменения моделей:

```bash
.venv/bin/alembic revision --autogenerate -m 'describe change'
.venv/bin/alembic upgrade head
```

Переходы матча: `WAITING → MATCHMAKING → PROVISIONING → RUNNING → FINISHING → FINISHED`.
Из любого незавершённого состояния разрешён переход в `FAILED`.
Время событий генерируется backend и хранится в SQLite как UTC без timezone.

## Cases (Stage 2)

```bash
.venv/bin/cyberrange cases validate
.venv/bin/cyberrange cases validate --directory /path/to/cases
```

В Docker после пересборки образа:

```bash
docker compose run --build --rm --no-deps backend cyberrange cases validate
```

Команда печатает `READY` или `INVALID` для каждого каталога case; код выхода
0 означает непустой полностью валидный каталог, 1 — ошибку или пустой каталог.
`CaseCatalog.list()`, `get(id)` и `random(seed)` возвращают только валидные
cases. Неизвестный id вызывает `KeyError`, выбор из пустого каталога —
`CaseFormatError`. Каталог перечитывается при создании `CaseCatalog`;
порядок сортируется по id, выбор с seed не меняет глобальный random state.

Структура и YAML показаны в `arena/cases/web-001`. Поля обязательны, лишние поля
отклоняются; имена сервисов и пары port/protocol должны быть уникальны.
ID должен совпадать с именем каталога. Validator проверяет наличие rootfs,
базовый образ `ubuntu:20.04`, локальные пути и Python-синтаксис checkers.
Dockerfile в текущем формате использует буквальный `FROM ubuntu:20.04`
без alias, аргументов и подстановок.

Cases устанавливает оператор: это доверенный исполняемый код, а не пользовательские
загрузки. Статус READY подтверждает формат; сборка и поведение target проверяются
на следующих checkpoint. При валидации код cases не запускается.

Тестовый web-001 намеренно позволяет `/download?path=...` читать файлы target.
С Stage 8 objective генерируется controller'ом для каждого матча и записывается
при provisioning; статического ключа в образе нет. `/` должен возвращать
`Cyber Range file service` с переводом строки. Checkers запускаются снаружи:

```bash
python arena/cases/web-001/checks/health.py http://target:80
python arena/cases/web-001/checks/exploit.py http://target:80 < expected-key.txt
```

Для health код 0 означает здоровый сервис, остальные — неуспех проверки.
Для exploit с Stage 6: 0 — VULNERABLE, 10 — PATCHED, 11 — UNREACHABLE;
остальные коды означают ERROR.

## Docker Runtime (Stage 3)

`arena.app.runtime.docker.DockerRuntime` запускается на Arena Linux-хосте с доступом к
Docker CLI и daemon. Методы `prepare(match, case)`, `start(match)`,
`inspect(match)`, `destroy(match)` принимают `Match` с сохранённым положительным
id; `case` — спецификация из `CaseCatalog`. Runtime не меняет состояние матча
в БД: это задача будущего controller.

`prepare` заново проверяет case и собирает образ, затем создаёт отдельную
внутреннюю bridge network и новый остановленный контейнер. `start` запускает
его один раз; `inspect` возвращает Docker inspect dict либо `None`, если target
отсутствует. Повторное использование существующего контейнера запрещено.
`destroy` идемпотентно удаляет контейнеры, сети и volumes с обеими labels
`cyberrange=true` и `match_id=<id>`. Docker-ошибки не скрываются.

Limits: 1 CPU, 256 MiB RAM (без дополнительного swap), 128 PID. Target не получает
privileged, host network/PID, Docker socket или mounts; образы с VOLUME
отклоняются. Порты на хост не публикуются: target доступен по container IP с
Linux-хоста. Общий образ `range-case-<case-id>:latest` и build cache сохраняются
для следующих матчей; файловая система каждого контейнера создаётся заново.
Compose backend пока не подключён к runtime; Docker socket в него не добавлен.

Полная проверка checkpoint 3 на хосте (включая regression tests Stage 1–2):

```bash
sudo bash tests/checkpoint_03.sh
```

Нужна установленная `.venv` из раздела локальной разработки. Скрипт собирает
Ubuntu target, проверяет ограничения, Ubuntu 20.04, запись файла, уничтожение,
чистоту следующего target и отсутствие ресурсов после cleanup. Используется
временная БД; данные приложения не изменяются. При успехе печатает
`CHECKPOINT 3 PASSED`. Без `RUN_DOCKER=1` интеграционный тест явно пропускается.

## BLUE access (Stage 4)

В target добавлены OpenSSH, `blue`, sudo без дополнительного пароля и
`iptables` с явно выбранным nft backend для IPv4/IPv6. Capability `NET_ADMIN` разрешает менять firewall внутри namespace
матча; privileged и host namespaces не используются. Веб-сервис управляется
через `sudo service cyberrange-web start|stop|restart`; его лог находится в
`/var/log/cyberrange-web.log`. SSH остаётся доступен при остановке web-сервиса.
SSH host keys генерируются заново в каждом target при запуске.

Внутренняя async функция Portal `provision_arena(session, match, case, provider)`
принимает сохранённый матч в состоянии PROVISIONING, создаёт и запускает target,
генерирует пароль через `secrets.token_urlsafe(32)`, передаёт его `chpasswd`
через stdin, дожидается SSH и переводит матч в RUNNING. Пароля нет в образе,
Docker labels, environment или аргументах команды. Ошибка provisioning
переводит матч в FAILED и запускает cleanup созданного target.

`GET /matches/{id}/access` требует `Authorization: Bearer <player-token>`.
`issue_player_token(player)` выдаёт случайный токен и сохраняет только SHA-256
hash в модели Player; вызывающая сторона сохраняет Player в БД. Лобби выдаёт
токен при выборе nickname; публичного endpoint для выбора команды или
создания матча нет.

RED получает `{"host":"..."}`. BLUE получает
`{"host":"...","port":22,"username":"blue","password":"..."}`.
Команда определяется по MatchPlayer в БД, параметры запроса её не меняют.
Чужой матч возвращает 404, неверный токен — 401, неготовая или завершённая
arena — 409. Ответ с доступом имеет `Cache-Control: no-store`.

Пароль и target IP хранятся в SQLite только на время arena, чтобы BLUE сохраняла
доступ после перезапуска backend. Для cleanup controller должен использовать
`await destroy_arena(session, match, provider)`: сначала реквизиты удаляются из БД,
затем уничтожаются Docker resources. Ошибка Docker не возвращает API-доступ;
её необходимо устранить и повторить cleanup. Прямой `runtime.destroy` —
низкоуровневый метод и не изменяет БД. Финализация результата матча появится
в последующих этапах. На internal network нет доступа в Интернет; исправления
вносятся средствами уже установленного окружения.

После обновления схемы выполните `.venv/bin/alembic upgrade head`
(Compose выполняет миграции автоматически). Полная проверка Stage 1–4:

```bash
sudo bash tests/checkpoint_04.sh
```

На хосте нужен клиент `ssh`. Тест проверяет реальный password login, `sudo`,
управление сервисом, firewall, отсутствие credentials в RED response,
недействительность старого пароля и новые credentials следующего матча.
При успехе печатает `CHECKPOINT 4 PASSED`. Матчи теста используют временную БД
и удаляются после проверки. UI и matchmaking в этот этап не входят.

## Health Checker (Stage 5)

На Linux-хосте controller можно запустить для RUNNING-матча:

```bash
.venv/bin/cyberrange health watch 42
```

`DATABASE_URL` должен указывать на БД controller. `--interval` задаёт паузу
между poll (по умолчанию 2 секунды), `--directory` — каталог cases.
Команда выводит JSON со статусом HEALTHY/UNHEALTHY и результатами по сервисам;
завершается при выходе матча из RUNNING или очистке target. На матч запускается
один watcher. Для автоматически созданных матчей Stage 10 Portal сам запускает
Victory Engine, который также сохраняет health-наблюдения; второй CLI watcher не нужен.
Provisioning теперь сохраняет связь матча с выбранным case.

`HealthChecker.poll(session, match, case)` выполняет локальный case-checker
отдельным Python-процессом с таймаутом 5 секунд для каждого required service.
Аргументы: URL `http://<target-ip>:<port>`, имя сервиса и транспорт `tcp`/`udp`.
Первый аргумент совместим с исходным HTTP-checker; для других протоколов автор
case использует адрес/порт из URL и переданный transport, реализуя собственную
функциональную проверку. Checker исполняется на controller, не в target.
Exit 0 означает HEALTHY; любой другой exit, ошибка запуска или timeout —
UNHEALTHY. Вывод checker не попадает в timeline. IP берётся только из матча.

События SERVICE_UP / SERVICE_DOWN / SERVICE_RESTORED записываются в MatchEvent
по каждому сервису только при смене состояния. Первая неуспешная проверка
создаёт SERVICE_DOWN. Metadata содержит service, status, reason и накопленный
закрытый downtime_seconds. Результат poll включает также текущий открытый
простой. После рестарта состояние восстанавливается из событий в SQLite;
новая таблица и миграция не нужны. Время событий задаётся controller в UTC,
точность downtime ограничена частотой polling. Завершённые матчи не проверяются.

Полная проверка этапов 1–5 на хосте:

```bash
sudo bash tests/checkpoint_05.sh
```

Тест останавливает и восстанавливает `cyberrange-web` в настоящем target,
проверяет события, отсутствие дублей и вычисление downtime. Дополнительно
локальные тесты проверяют неверный HTTP body, timeout, сбой checker, несколько
сервисов и повторное создание checker. Ожидаемый результат — `CHECKPOINT 5 PASSED`.

## Exploit Checker (Stage 6)

`ExploitChecker.check(match, case, expected_key)` запускает локальный
`checks.exploit` на controller отдельным Python-процессом (timeout 5 секунд).
Первый required service служит сетевой точкой входа exploit этого case.
URL передаётся аргументом, ожидаемый objective — через stdin, без вывода в
логи или аргументы процесса. Значение objective передаёт controller из
`Match.red_key`, созданного при provisioning.

Exit-контракт exploit scripts: 0 = VULNERABLE, 10 = PATCHED,
11 = UNREACHABLE, остальные = ERROR. Timeout и ошибка запуска также дают
ERROR: обычный Python crash с exit 1 не считается исправлением.
Тестовый checker пытается прочитать objective по intended path; не проверяет
версию пакета, конфигурацию или конкретный способ ремонта. Ответы 403/404/410
и отсутствие ожидаемого objective означают PATCHED; HTTP 5xx — ERROR.

`check_defense(session, match, case, expected_key, cases_directory)` возвращает
health, exploit и предварительный `blue_eligible`: true только при RUNNING,
HEALTHY и PATCHED. Это проверка допуска Stage 6, не выдача ключа или победа:
проверка из RED-зоны и stabilization реализованы отдельно в Stage 7–8.

Пример ручной проверки с ожидаемым ключом без перевода строки в файле
(код 10 для PATCHED — нормальный результат):

```bash
python arena/cases/web-001/checks/exploit.py http://target:80 < expected-key.txt
```

Полная проверка этапов 1–6 на хосте:

```bash
sudo bash tests/checkpoint_06.sh
```

Тест подтверждает VULNERABLE на исходном target, удаляет небезопасный маршрут
`/download` (контракт `/` сохраняется), получает PATCHED + HEALTHY, затем
останавливает сервис и проверяет отсутствие допуска BLUE. Ожидаемый итог —
`CHECKPOINT 6 PASSED`. Локальные тесты дополнительно проверяют другие способы
блокировки, возврат уязвимости, HTTP 5xx, crash и timeout checker.

## RED-zone checks (Stage 7)

`check_red_defense(...)` сохраняет обычный controller health-check и запускает
functional health + intended exploit из краткоживущего probe-контейнера в той
же internal network, что и target. Допуск BLUE требует одновременно HEALTHY,
AVAILABLE из RED-зоны и PATCHED. Остановка сервиса или firewall-блокировка
RED-зоны всегда дают `blue_eligible: false`.

Probe строится из точного image target, но отдельным слоем получает только
case checkers; `/opt/objective` удаляется. Он запускается read-only, без Linux
capabilities, с `no-new-privileges`, 64 MiB RAM и 32 PID. После каждой проверки
probe удаляется; orphaned probe имеет labels матча и удаляется обычным
`DockerRuntime.destroy`. Target не содержит checker-файлы.

Полная проверка этапов 1–7 на хосте:

```bash
sudo bash tests/checkpoint_07.sh
```

Тест проверяет три сценария: остановленный сервис; controller health доступен,
но RED subnet заблокирован firewall; сервис доступен из RED-зоны после
устранения unsafe route. Ожидаемый итог — `CHECKPOINT 7 PASSED`.

## Victory Engine (Stage 8)

При provisioning controller генерирует независимые RED_KEY и BLUE_KEY с
256 битами случайности. RED_KEY передаётся через stdin и записывается по
`red.objective_path` только в текущем target. BLUE_KEY остаётся в SQLite;
в image, target, Docker metadata и timeline его нет. Cleanup и ошибка
provisioning удаляют оба ключа из записи матча.

Для RUNNING-матча на controller-хосте:

```bash
.venv/bin/cyberrange victory watch 42
```

Команда каждые 2 секунды после завершения предыдущего poll проверяет controller
health, RED surface и exploit через `check_red_defense`. Первый успешный poll
начинает SECURING. После `blue.stabilization_seconds` (60 для web-001) и
успешного завершающего poll выдаётся BLUE_KEY. Любой неуспех или ошибка
checker отменяет попытку; следующий успех начинает полный отсчёт заново.

На матч нужен один экземпляр watcher. Отсчёт идёт по монотонным часам;
перезапуск controller, перерыв между успешными проверками более 30 секунд
или probe длительностью более 30 секунд сбрасывают попытку. Пропущенный
интервал не засчитывается. `--interval` меняет паузу между poll (0 < interval < 30).
Наблюдение дискретное: изменения между poll нельзя обнаружить без следующей
проверки. После выдачи ключ сохраняет силу до окончания/cleanup матча.

Состояние и выдача сохраняются в БД; timeline получает BLUE_SECURING_STARTED,
BLUE_SECURING_CANCELLED, BLUE_SECURED_TARGET и BLUE_KEY_ISSUED без значений ключей.
`GET /matches/{id}/access` добавляет поле `blue_key` только для BLUE и только
после выдачи. RED не получает ни один ключ через этот endpoint. CLI выводит
статус и оставшееся время, без ключей. Выдача не завершает матч; submit и
определение победителя относятся к Stage 12.

Примените миграции через `.venv/bin/alembic upgrade head` или перезапуск
пересобранного Compose backend. Старые активные arenas без ключей нужно
очистить и создать заново: engine не выдаёт им ключ задним числом.
Полная проверка этапов 1–8:

```bash
sudo bash tests/checkpoint_08.sh
```

Тест включает реальные 60 секунд непрерывной защиты, отмену при возврате
exploit и блокировке RED subnet, доступность ключа только BLUE, а также
regression tests Stage 1–7. Ожидаемый итог — `CHECKPOINT 8 PASSED`.

## Player/Lobby (Stage 9)

После `docker compose up --build -d --wait` откройте http://localhost:8000/.
Выберите nickname и нажмите «НАЧАТЬ ИГРУ»: появится `SEARCHING FOR MATCH`
и количество ожидающих игроков. «Выйти из очереди» отменяет поиск,
«Сменить nickname» завершает сессию. При настроенном ArenaProvider четыре игрока
автоматически получают матч (Stage 10); после назначения выход из очереди и смена
nickname блокируются до завершения/ошибки матча.

Nickname содержит 2–24 буквы, цифры, `_` или `-`. Активные имена уникальны
без учёта регистра и после Unicode-нормализации. Сессия живёт 15 минут после
последнего обращения к лобби; открытая страница обновляет её каждые 5 секунд.
После выхода или истечения сессии имя освобождается, история игрока сохраняется.
Токен хранится в sessionStorage вкладки, в БД — только его hash. Перезагрузка
страницы сохраняет сессию. Для независимых игроков используйте отдельные
профили браузера. Очередь хранится в SQLite и переживает перезапуск backend.

API: `POST /lobby/session` принимает `{"nickname":"pilot"}` и выдаёт токен;
`GET /lobby/session` возвращает статус и продлевает сессию;
`DELETE /lobby/session` завершает её; `POST`/`DELETE /lobby/queue` входят
в очередь и выходят из неё. Все запросы после создания сессии требуют Bearer token.

Полная проверка этапов 1–9 включает Docker и четыре независимые сессии Chromium.
Однократная подготовка (браузер устанавливается для пользователя, запускающего тесты):

```bash
.venv/bin/pip install -e '.[test,browser]'
sudo .venv/bin/python -m playwright install chromium
sudo bash tests/checkpoint_09.sh
```

Ожидаемый итог — `CHECKPOINT 9 PASSED`. Только браузерная проверка без Docker:

```bash
.venv/bin/python -m playwright install chromium
RUN_BROWSER=1 .venv/bin/python -m pytest tests/test_checkpoint_09.py -q -p no:cacheprovider
```

## Matchmaking (Stage 10)

Portal с настроенным RemoteArenaProvider или LocalArenaProvider раз в секунду
забирает четыре самые ранние активные заявки. Назначение выполняется одной
SQLite-транзакцией: игрок не может одновременно попасть в два матча, выход из
очереди до назначения учитывается. Истёкшие сессии не назначаются.

Portal сохраняет случайный seed, выбирает READY case из `PORTAL_CASES` (по умолчанию
`arena/cases`), перемешивает игроков и назначает 2 RED / 2 BLUE. На Arena должен
быть установлен тот же case. Матч сразу виден всем участникам: номер, case,
своя команда, союзник и `PROVISIONING`, затем `RUNNING` или `FAILED`.
Лобби не возвращает пароли, ключи и токены других игроков.

Provisioning выполняется в фоне. После RUNNING автоматически запускается
существующий Victory Engine; выдача BLUE_KEY по-прежнему не завершает матч.
После перезапуска Portal незавершённый provisioning использует сохранённый
run ID и ключи, а наблюдение RUNNING-матча возобновляется со сбросом стабилизации.
При неопределённом сетевом результате новые назначения приостанавливаются до
восстановления связи. После FAILED игрок может снова нажать «НАЧАТЬ ИГРУ».

Запускайте **один worker Portal**: DB защищает назначение очереди, а фоновые задачи
provisioning/наблюдения принадлежат одному процессу. Compose это фиксирует;
для native-запуска используйте `uvicorn portal.app.main:app --workers 1`.
Без ArenaProvider очередь работает как лобби и матч не создаётся.

Примените миграцию 0006 через `.venv/bin/alembic upgrade head` или пересборку Compose.
Проверка на изолированном Docker-хосте с подготовленным Chromium:

```bash
sudo bash tests/checkpoint_10.sh
```

Скрипт включает предыдущие проверки, HTTPS/firewall 8A, четыре браузера и реальное
автоматическое создание target через LocalArenaProvider. Тесты используют временные
БД. На тестовом хосте не должно быть активных local arenas или production management
policy: startup reconciliation и host-проверки намеренно очищают свои ресурсы.
Ожидаемый итог — `CHECKPOINT 10 PASSED`.
