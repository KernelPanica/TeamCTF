# Cyber Range MVP — Waterfall Roadmap

## 0. Цель проекта

Создать автоматизированную локальную Red vs Blue CTF-платформу без судьи, AI Game Master, AI-анализа, рейтинговых матчей и наблюдателей.

Основной цикл:

```text
Игроки
  ↓
Matchmaking
  ↓
RED / BLUE
  ↓
Случайный case
  ↓
Одноразовый Ubuntu 20.04 target
  ↓
RED атакует / BLUE защищает
  ↓
RED получает objective
ИЛИ
BLUE стабилизирует защищённый сервис
  ↓
KEY
  ↓
Submit
  ↓
Game Over
  ↓
Детерминированный отчёт
  ↓
Уничтожение arena
```

### RED

RED не имеет административных credentials.

Задача:

1. исследовать доступную поверхность;
2. обнаружить уязвимость;
3. эксплуатировать её;
4. получить `RED_KEY`;
5. отправить ключ через веб-интерфейс.

### BLUE

BLUE получает административные credentials от Ubuntu-контейнера после подготовки матча.

Задача:

1. исследовать сервер;
2. обнаружить источник угрозы;
3. сохранить работоспособность обязательных сервисов;
4. устранить эксплуатационный путь;
5. выдержать stabilization period;
6. получить `BLUE_KEY`;
7. отправить ключ через веб-интерфейс.

BLUE не должна искать `BLUE_KEY` внутри контейнера. Ключ выдаётся controller'ом только после выполнения условий защиты.

---

# Stage 1 — Project Skeleton

Создать минимальную структуру проекта.

```text
cyberrange/
├── backend/
│   └── app/
├── frontend/
├── cases/
├── runtime/
├── tests/
├── data/
├── docker/
├── README.md
├── ROADMAP.md
├── pyproject.toml
└── compose.yaml
```

Backend:

- Python;
- FastAPI;
- SQLite;
- SQLAlchemy или другой минимальный ORM;
- migrations.

Не добавлять Redis, Celery, Kafka, Kubernetes или другие распределённые компоненты.

Создать модели:

```text
Player
Match
MatchPlayer
Case
MatchEvent
```

Минимальные состояния матча:

```text
WAITING
MATCHMAKING
PROVISIONING
RUNNING
FINISHING
FINISHED
FAILED
```

## CHECKPOINT 1

**Статус: completed (2026-10-01).**

- `.venv/bin/python -m pytest -q`: 57 passed (Python 3.14, вне sandbox).
- `docker compose config --quiet`: успешно.
- Пользователь подтвердил `sudo bash tests/checkpoint_01.sh` результатом
  `CHECKPOINT 1 PASSED`: сборка и запуск, тесты внутри контейнера,
  HTTP 200, сохранение записи после restart, удаление тестовой записи
  и остановка Compose прошли успешно.

Должно работать:

```bash
docker compose up
```

После запуска:

```text
GET /health
→ 200 OK
```

SQLite автоматически создаётся/мигрируется.

Automated tests проверяют:

- запуск приложения;
- подключение БД;
- создание Player;
- создание Match;
- переходы основных состояний.

**Не переходить к Stage 2, пока CHECKPOINT 1 не проходит.**

---

# Stage 2 — Case Specification

Определить единый формат case.

Каждый case:

```text
cases/<case-id>/
├── case.yaml
├── Dockerfile
├── rootfs/
└── checks/
    ├── health.py
    └── exploit.py
```

Все игровые target images MVP обязаны наследоваться от:

```dockerfile
FROM ubuntu:20.04
```

`case.yaml` должен поддерживать минимум:

```yaml
id: web-001
name: Example Web Service

category: web
difficulty: medium

required_services:
  - name: web
    port: 80
    protocol: tcp

red:
  objective_path: /opt/objective/red-key

blue:
  stabilization_seconds: 60

checks:
  health: checks/health.py
  exploit: checks/exploit.py
```

Реализовать:

```text
CaseLoader
CaseValidator
CaseCatalog
```

Невалидный case не должен попадать в matchmaking.

## CHECKPOINT 2

**Статус: completed (2026-10-01).**

- Добавлены `CaseLoader`, `CaseValidator`, `CaseCatalog` и CLI
  `cyberrange cases validate`; невалидные cases исключаются из каталога.
- `.venv/bin/cyberrange cases validate`: `web-001: READY`, exit 0.
- Тесты повреждённого YAML подтверждают `INVALID`, exit 1; также проверены
  обязательные поля, пути, структура, Ubuntu base и детерминированный seed.
- `.venv/bin/python -m pytest -q`: 82 passed, включая все тесты Stage 1
  и локальную проверку HTTP-контракта тестового сервиса и его checkers.
- Сборка target и Docker isolation проверяются в Stage 3; READY на этом
  этапе означает соответствие формату case, а не runtime-проверку.

Создать один тестовый case.

Команда:

```bash
cyberrange cases validate
```

должна выводить его как `READY`.

Повреждённый `case.yaml` должен выводиться как `INVALID`.

CaseCatalog должен уметь:

```text
list()
get(id)
random(seed)
```

Одинаковый seed должен давать одинаковый результат при неизменном каталоге.

**Не переходить к Stage 3, пока CHECKPOINT 2 не проходит.**

---

# Stage 3 — Docker Runtime

Реализовать `DockerRuntime`.

Интерфейс:

```text
prepare(match, case)
start(match)
inspect(match)
destroy(match)
```

Для каждого матча создаётся новый disposable container.

Запрещено повторно использовать контейнер между матчами.

Container:

- Ubuntu 20.04;
- отдельная Docker network;
- без `privileged`;
- без `docker.sock`;
- без host network;
- без host PID namespace;
- без произвольных host mounts;
- с CPU limit;
- с memory limit;
- с PID limit.

Имена ресурсов должны включать `match_id`.

Например:

```text
range-match-42-target
range-match-42-net
```

## CHECKPOINT 3

**Статус: completed (2026-10-01).**

- Реализован `DockerRuntime.prepare/start/inspect/destroy`: отдельная internal
  bridge network, новый target, limits 1 CPU / 256 MiB / 128 PID, labels
  `cyberrange=true` и `match_id`; host mounts и volumes образа запрещены.
- Пользователь выполнил `sudo bash tests/checkpoint_03.sh`:
  **95 passed**, `CHECKPOINT 3 PASSED`.
- Подтверждены Ubuntu 20.04, ограничения, файл в первом target, чистый второй
  target, идемпотентный destroy и отсутствие ресурсов матча после cleanup.

Тест должен:

1. создать match;
2. поднять target;
3. подтвердить Ubuntu 20.04;
4. записать файл внутрь контейнера;
5. уничтожить match;
6. создать новый match;
7. убедиться, что файла больше нет.

После `destroy()` не должно оставаться:

- контейнеров;
- сетей;
- volumes данного матча.

**Не переходить к Stage 4, пока CHECKPOINT 3 не проходит.**

---

# Stage 4 — BLUE Access

При provisioning создать случайные credentials для BLUE.

Например:

```text
username: blue
password: cryptographically-random
```

BLUE должна иметь административный доступ, достаточный для:

- просмотра процессов;
- просмотра логов;
- редактирования конфигураций;
- управления сервисами;
- установки исправлений, предусмотренных окружением;
- изменения firewall.

RED credentials не получает.

Credentials хранятся только в рамках текущего матча.

## CHECKPOINT 4

После provisioning backend должен вернуть BLUE:

```json
{
  "host": "...",
  "port": 22,
  "username": "blue",
  "password": "..."
}
```

Проверить:

```bash
ssh blue@target
sudo ...
```

BLUE credentials не должны присутствовать в RED API response.

После уничтожения arena старые credentials перестают работать.

**Не переходить к Stage 5, пока CHECKPOINT 4 не проходит.**

---

# Stage 5 — Health Checker

Реализовать независимый checker обязательных сервисов.

Health checker запускается controller'ом, а не внутри target.

Он должен проверять функциональность, а не только открытый TCP port.

Например:

```text
GET /
→ HTTP 200
→ ожидаемый response
```

События:

```text
SERVICE_UP
SERVICE_DOWN
SERVICE_RESTORED
```

Не создавать одинаковое событие на каждом poll.

## CHECKPOINT 5

При работающем сервисе:

```text
HEALTHY
```

После:

```bash
systemctl stop <service>
```

должно фиксироваться:

```text
SERVICE_DOWN
```

После восстановления:

```text
SERVICE_RESTORED
```

Время downtime должно вычисляться автоматически.

**Не переходить к Stage 6, пока CHECKPOINT 5 не проходит.**

---

# Stage 6 — Exploit Checker

Каждый case должен предоставлять checker, который определяет:

> Можно ли всё ещё выполнить intended exploitation path?

Checker не должен проверять конкретный способ исправления.

Нельзя делать:

```text
nginx version >= X → fixed
```

Нужно проверять результат:

```text
intended exploit
      ↓
SUCCESS / FAILED
```

BLUE может исправить проблему любым способом, пока сохраняется service contract.

## CHECKPOINT 6

Для исходного target:

```text
exploit_check()
→ VULNERABLE
```

После предусмотренного исправления:

```text
exploit_check()
→ PATCHED
```

При этом:

```text
health_check()
→ HEALTHY
```

Если сервис просто остановлен:

```text
exploit_check()
→ PATCHED/UNREACHABLE

health_check()
→ UNHEALTHY

BLUE victory eligibility
→ FALSE
```

**Не переходить к Stage 7, пока CHECKPOINT 6 не проходит.**

---

# Stage 7 — Anti-Firewall Bypass

BLUE разрешено использовать firewall.

Но BLUE не должна выигрывать простым:

```bash
ufw deny 80
```

или блокировкой RED.

Для каждого case определить `required_services`.

Attack checker должен проверять доступность обязательной поверхности из эквивалентной RED сетевой зоны.

Условие BLUE:

```text
exploit blocked
AND
required services reachable
AND
functional healthcheck passes
```

## CHECKPOINT 7

Проверить три сценария.

### A. BLUE останавливает сервис

```text
BLUE WIN = false
```

### B. BLUE firewall'ом отрезает attack network

```text
BLUE WIN = false
```

### C. BLUE оставляет сервис доступным, но устраняет exploit

```text
BLUE WIN ELIGIBLE = true
```

**Не переходить к Stage 8, пока CHECKPOINT 7 не проходит.**

---

# Stage 8 — Victory Engine

При provisioning генерировать:

```text
RED_KEY
BLUE_KEY
```

Оба криптографически случайные и уникальные для матча.

### RED_KEY

Доступен внутри target через intended exploitation/objective path.

### BLUE_KEY

Никогда заранее не помещается внутрь target.

Controller выдаёт его только после:

```text
exploit_check == PATCHED
AND
health_check == HEALTHY
AND
required attack surface == AVAILABLE
```

После этого начинается:

```text
SECURING
```

Например:

```text
60 seconds
```

В течение stabilization period регулярно повторять проверки.

Любой failure:

```text
SECURING_CANCELLED
```

При успешном завершении:

```text
BLUE_SECURED_TARGET
BLUE_KEY_ISSUED
```

## CHECKPOINT 8

Проверить:

```text
правильный patch
→ SECURING
→ 60 секунд
→ BLUE_KEY
```

Проверить:

```text
patch
→ SECURING
→ exploit снова работает
→ SECURING_CANCELLED
```

Проверить:

```text
firewall block
→ BLUE_KEY не выдаётся
```

**Не переходить к Stage 9, пока CHECKPOINT 8 не проходит.**

---

# Stage 9 — Player/Lobby

Реализовать простой web UI.

Игрок вводит уникальный nickname.

Главная страница:

```text
CYBER RANGE

Nickname: kernelpanica

[ НАЧАТЬ ИГРУ ]
```

После нажатия:

```text
SEARCHING FOR MATCH
```

Игрок может выйти из очереди.

Полноценные accounts, email, OAuth и passwords в MVP не нужны.

## CHECKPOINT 9

Четыре браузерные сессии могут:

1. выбрать разные nickname;
2. войти в очередь;
3. видеть статус matchmaking;
4. выйти из очереди до создания матча.

Одинаковые активные nickname запрещены.

**Не переходить к Stage 10, пока CHECKPOINT 9 не проходит.**

---

# Stage 10 — Matchmaking

MVP использует только:

```text
2 RED
vs
2 BLUE
```

При четырёх ожидающих игроках:

```text
shuffle(players)
```

затем:

```text
2 → RED
2 → BLUE
```

Выбрать случайный READY case.

Создать seed.

Запустить provisioning.

## CHECKPOINT 10

Четыре игрока автоматически получают матч.

Должно выполняться:

```text
RED count == 2
BLUE count == 2
```

Каждый игрок видит:

- свою команду;
- союзников;
- статус provisioning.

Case выбирается автоматически.

Администратор ничего не делает.

**Не переходить к Stage 11, пока CHECKPOINT 10 не проходит.**

---

# Stage 11 — Game UI

После `RUNNING` показать RED:

```text
RED TEAM

Players:
...

TARGET
10.x.x.x

TIME
00:03:42

KEY
[________________]

[ SUBMIT ]
```

BLUE дополнительно видит:

```text
ADMIN ACCESS

SSH: blue@...
Password: ...
```

RED этих данных не видит.

Таймер отображает фактическую продолжительность матча.

Таймер сам по себе матч не завершает.

## CHECKPOINT 11

RED и BLUE одновременно видят актуальное состояние одного матча.

BLUE видит credentials.

RED credentials не получает.

Refresh страницы не ломает состояние.

**Не переходить к Stage 12, пока CHECKPOINT 11 не проходит.**

---

# Stage 12 — KEY Submission

Создать endpoint:

```text
POST /matches/{id}/submit
```

Проверять KEY только backend'ом.

Возможные результаты:

```text
INVALID
RED_WIN
BLUE_WIN
MATCH_ALREADY_FINISHED
```

Победа должна фиксироваться атомарно.

Первый правильный KEY выигрывает.

## CHECKPOINT 12

Проверить:

```text
wrong key
→ матч продолжается
```

```text
RED_KEY
→ RED wins
```

```text
BLUE_KEY
→ BLUE wins
```

Одновременный RED_KEY и BLUE_KEY:

```text
ровно один winner
```

После FINISHED изменить winner невозможно.

**Не переходить к Stage 13, пока CHECKPOINT 12 не проходит.**

---

# Stage 13 — Event Timeline

Все значимые события сохранять как:

```text
MatchEvent

id
match_id
type
timestamp
metadata
```

Минимальные события:

```text
MATCH_CREATED
MATCH_STARTED

TARGET_STARTED
TARGET_HEALTHY

BLUE_FIRST_LOGIN

SERVICE_DOWN
SERVICE_RESTORED

EXPLOIT_AVAILABLE
EXPLOIT_BLOCKED
EXPLOIT_RESTORED

BLUE_SECURING_STARTED
BLUE_SECURING_CANCELLED
BLUE_KEY_ISSUED

KEY_SUBMITTED_INVALID
KEY_SUBMITTED_RED
KEY_SUBMITTED_BLUE

MATCH_FINISHED
ARENA_DESTROYED
```

## CHECKPOINT 13

После тестового матча timeline должен полностью восстанавливать основные события игры в хронологическом порядке.

Все timestamps должны рассчитываться server-side.

**Не переходить к Stage 14, пока CHECKPOINT 13 не проходит.**

---

# Stage 14 — Deterministic Match Analyzer

Никакого AI.

Analyzer получает исключительно:

```text
Match
+
MatchEvents
+
health history
+
exploit history
```

Рассчитать:

```text
match duration
winner

service uptime
total downtime
longest downtime

time to exploit blocked
number of securing attempts
number of invalid key submissions
time between important events
```

Формулировать замечания только на основании измеримых событий.

Пример:

```text
BLUE

• Сервис был недоступен 37 секунд.
• Первая попытка защиты не устранила exploitation path.
• Уязвимость была окончательно устранена через 06:31.
• После исправления target оставался защищённым 60 секунд.

RED

• RED получила objective через 05:14.
• До завершения матча было отправлено 2 неверных ключа.
```

Не генерировать субъективные советы.

## CHECKPOINT 14

Один и тот же event log всегда должен генерировать один и тот же report.

Analyzer не обращается ни к каким LLM/API.

**Не переходить к Stage 15, пока CHECKPOINT 14 не проходит.**

---

# Stage 15 — Post-Match UI

После завершения показать:

```text
MATCH #42

WINNER
BLUE

DURATION
07:31
```

Далее:

```text
RED
BLUE
SERVICE
```

и timeline:

```text
00:00 Match started
00:43 Blue login
02:14 Red exploitation detected
03:51 Service down
04:03 Service restored
05:27 Blue securing started
06:02 Securing cancelled
06:18 Blue securing started
07:18 Blue key issued
07:31 Blue key submitted
```

После отчёта:

```text
[ НАЧАТЬ НОВУЮ ИГРУ ]
```

## CHECKPOINT 15

Все четыре игрока после завершения автоматически получают один и тот же authoritative report.

После refresh report остаётся доступен.

**Не переходить к Stage 16, пока CHECKPOINT 15 не проходит.**

---

# Stage 16 — Cleanup and Failure Recovery

После FINISHED:

```text
save result
save events
generate report
destroy arena
```

При ошибке provisioning:

```text
FAILED
→ cleanup
```

При падении backend после restart необходимо найти orphaned resources.

Реализовать reconciliation:

```text
runtime reconcile
```

Ресурсы идентифицировать labels:

```text
cyberrange=true
match_id=<id>
```

## CHECKPOINT 16

Проверить:

### Normal match

После завершения нет игровых Docker resources.

### Failed provisioning

Нет оставшихся resources.

### Backend crash

После restart reconciliation обнаруживает и корректно обрабатывает orphaned arena.

История завершённых матчей при этом сохраняется.

**Не переходить к Stage 17, пока CHECKPOINT 16 не проходит.**

---

# Stage 17 — End-to-End MVP Test

Провести полностью автоматизированный тест.

```text
4 players
    ↓
queue
    ↓
matchmaking
    ↓
2 RED / 2 BLUE
    ↓
random case
    ↓
Ubuntu 20.04 arena
    ↓
BLUE credentials
    ↓
RUNNING
```

Проверить RED victory:

```text
RED obtains RED_KEY
→ submit
→ RED WIN
→ report
→ cleanup
```

Запустить второй матч.

Проверить BLUE victory:

```text
BLUE investigates
→ patches vulnerability
→ service remains functional
→ exploit unavailable
→ stabilization
→ BLUE_KEY
→ submit
→ BLUE WIN
→ report
→ cleanup
```

## CHECKPOINT 17 — MVP COMPLETE

MVP считается готовым только если оба сценария проходят без:

- ручного создания контейнера;
- ручного назначения команд;
- ручного выбора case;
- ручной выдачи credentials;
- ручной проверки победителя;
- ручного создания отчёта;
- ручного удаления arena.

---

# Definition of Done

Версия `0.1.0` считается завершённой, когда четыре человека могут открыть веб-приложение и провести несколько последовательных Red vs Blue матчей без участия администратора.

Основной контракт игры:

```text
RED
────────────────────
discover
exploit
obtain RED_KEY
submit
         │
         ├──── first valid KEY wins
         │
BLUE
────────────────────
investigate
maintain service
block exploitation
stabilize
receive BLUE_KEY
submit
```

После каждого матча:

```text
result persisted
+
timeline persisted
+
report generated
+
arena destroyed
```

Следующий матч всегда получает чистый target, построенный на Ubuntu 20.04 LTS.

---

# Explicit Non-Goals

Не реализовывать:

```text
AI Game Master
AI-generated cases
AI match analysis

ranked games
ELO
MMR
seasons

spectators
spectator mode

Kubernetes
multi-node orchestration

tournaments
achievements

OAuth
email accounts

terminal replay
packet replay UI
```

Не добавлять эти функции «на будущее» в виде преждевременных abstractions.

Сначала закончить `0.1.0`.

---

# Правило работы Codex

После каждого Stage:

1. реализовать только scope текущего Stage;
2. написать/обновить automated tests;
3. запустить tests;
4. выполнить соответствующий CHECKPOINT;
5. исправить найденные проблемы;
6. обновить `ROADMAP.md`;
7. отметить checkpoint как completed;
8. сделать отдельный commit;
9. только после этого переходить к следующему Stage.

Формат commit:

```text
checkpoint-01: project skeleton
checkpoint-02: case specification
checkpoint-03: docker runtime
...
checkpoint-17: mvp complete
```

Запрещено пропускать checkpoint из-за того, что последующий функционал уже частично реализован.

При обнаружении архитектурной проблемы вернуться к checkpoint, в котором нарушенный контракт был введён, исправить его и повторно прогнать все последующие automated tests.
