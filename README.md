# TeamCTF

Локальная Red vs Blue CTF-платформа. Реализован Stage 1: backend, SQLite,
миграции и модели. Stage 2 добавляет формат cases, validator, каталог и CLI.
Игровой функционал появится в следующих checkpoint.

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
.venv/bin/uvicorn backend.app.main:app --reload
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

Структура и YAML показаны в `cases/web-001`. Поля обязательны, лишние поля
отклоняются; имена сервисов и пары port/protocol должны быть уникальны.
ID должен совпадать с именем каталога. Validator проверяет наличие rootfs,
базовый образ `ubuntu:20.04`, локальные пути и Python-синтаксис checkers.
Dockerfile в текущем формате использует буквальный `FROM ubuntu:20.04`
без alias, аргументов и подстановок.

Cases устанавливает оператор: это доверенный исполняемый код, а не пользовательские
загрузки. Статус READY подтверждает формат; сборка и поведение target проверяются
на следующих checkpoint. При валидации код cases не запускается.

Тестовый web-001 намеренно позволяет `/download?path=...` читать файлы target.
Его статический objective `stage-2-test-objective` служит только fixture Stage 2;
ключи матча будут генерироваться controller'ом в Stage 8. `/` должен возвращать
`Cyber Range file service` с переводом строки. Checkers запускаются снаружи:

```bash
python cases/web-001/checks/health.py http://target:80
python cases/web-001/checks/exploit.py http://target:80 stage-2-test-objective
```

Код 0 означает здоровый сервис / доступный exploit, код 1 — неуспех проверки.
Различение причин неуспеха и оркестрация проверок относятся к Stage 5–8.

## Docker Runtime (Stage 3)

`backend.app.runtime.DockerRuntime` запускается на Linux-хосте с доступом к
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

Внутренняя функция controller `provision_arena(session, match, case, runtime)`
принимает сохранённый матч в состоянии PROVISIONING, создаёт и запускает target,
генерирует пароль через `secrets.token_urlsafe(32)`, передаёт его `chpasswd`
через stdin, дожидается SSH и переводит матч в RUNNING. Пароля нет в образе,
Docker labels, environment или аргументах команды. Ошибка provisioning
переводит матч в FAILED и запускает cleanup созданного target.

`GET /matches/{id}/access` требует `Authorization: Bearer <player-token>`.
`issue_player_token(player)` выдаёт случайный токен и сохраняет только SHA-256
hash в модели Player; вызывающая сторона сохраняет Player в БД. Выдача токена
через lobby появится в Stage 9; сейчас identity создают controller/tests,
публичного endpoint для выбора команды или создания матча нет.

RED получает `{"host":"..."}`. BLUE получает
`{"host":"...","port":22,"username":"blue","password":"..."}`.
Команда определяется по MatchPlayer в БД, параметры запроса её не меняют.
Чужой матч возвращает 404, неверный токен — 401, неготовая или завершённая
arena — 409. Ответ с доступом имеет `Cache-Control: no-store`.

Пароль и target IP хранятся в SQLite только на время arena, чтобы BLUE сохраняла
доступ после перезапуска backend. Для cleanup controller должен использовать
`destroy_arena(session, match, runtime)`: сначала реквизиты удаляются из БД,
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
