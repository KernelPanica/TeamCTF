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

Минимальная структура проекта (пути актуализированы после checkpoint 8A).

```text
cyberrange/
├── portal/
│   ├── app/
│   ├── frontend/
│   ├── migrations/
│   ├── Dockerfile
│   └── compose.yaml
├── arena/
│   ├── app/
│   ├── cases/
│   ├── Dockerfile
│   └── compose.yaml
├── shared/
├── tests/
├── data/
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
arena/cases/<case-id>/
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

**Статус: completed (2026-10-01).**

- В target добавлены SSH, пользователь blue, sudo, управление web-сервисом
  и firewall через NET_ADMIN без privileged. Host keys создаются при запуске.
- `provision_arena` генерирует случайный пароль и передаёт его через stdin;
  `destroy_arena` удаляет реквизиты из БД и уничтожает arena.
- `GET /matches/{id}/access` проверяет bearer token игрока и MatchPlayer:
  BLUE получает SSH credentials, RED — только адрес target.
- Добавлена миграция 0002 с проверкой сохранения существующих данных.
- В образе явно выбран iptables-nft/ip6tables-nft; backend проверяется тестом.
- Пользователь выполнил `sudo bash tests/checkpoint_04.sh`:
  **107 passed**, `CHECKPOINT 4 PASSED` (включая regression tests Stage 1–3).
- Подтверждены реальный SSH/sudo, firewall, остановка/восстановление web
  при доступном SSH, отсутствие credentials у RED, отзыв старого пароля
  и новый пароль следующего матча.

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

**Статус: completed (2026-10-01).**

- Добавлены `HealthChecker.poll` и `cyberrange health watch <match_id>`:
  внешние функциональные checks, timeout, статусы по каждому required service.
- События SERVICE_UP/DOWN/RESTORED сохраняются в MatchEvent только при смене
  состояния; downtime вычисляется по серверным timestamps и восстанавливается
  из БД после перезапуска checker. Provisioning сохраняет выбранный case.
- Пользователь выполнил `sudo bash tests/checkpoint_05.sh`:
  **116 passed**, `CHECKPOINT 5 PASSED` (включая regression tests Stage 1–4).
- Подтверждены остановка/восстановление настоящего web-сервиса, события
  без дублей, функциональный контракт и расчёт downtime.

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

**Статус: completed (2026-10-01).**

- `ExploitChecker.check` запускает внешний checker: VULNERABLE / PATCHED /
  UNREACHABLE / ERROR. Ожидаемый objective передаётся через stdin.
- `check_defense` объединяет exploit и функциональный health: остановка
  сервиса, timeout или ошибка checker не дают допуска BLUE.
- Проверяется результат эксплуатации; проверки версии/способа исправления нет.
- Пользователь выполнил `sudo bash tests/checkpoint_06.sh`:
  **126 passed**, `CHECKPOINT 6 PASSED` (включая regression tests Stage 1–5).
- Подтверждены исходный VULNERABLE, исправленный PATCHED + HEALTHY и
  остановленный UNREACHABLE + UNHEALTHY без допуска BLUE.

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

**Статус: completed (2026-10-01).**

- Добавлен изолированный ephemeral probe в network матча: functional health
  и exploit выполняются из эквивалентной RED-зоны.
- `check_red_defense` требует HEALTHY + RED surface AVAILABLE + PATCHED;
  остановка сервиса и блокировка RED-зоны не дают допуска BLUE.
- Probe read-only, без capabilities, без objective, с limits и labels матча;
  удаляется после проверки и обычным cleanup при orphan.
- Пользователь выполнил `sudo bash tests/checkpoint_07.sh`:
  **133 passed**, `CHECKPOINT 7 PASSED` (включая regression tests Stage 1–6).
- Подтверждены все сценарии A/B/C: остановка сервиса, блокировка RED subnet
  при доступном controller health и исправление с доступной поверхностью.

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

# CHECKPOINT 8A — Portal/Arena Architecture Split

**Статус: исходный и расширенный checkpoint completed (2026-10-02).**

Расширение реализовано: management API `/v1`, Portal-only source-IP firewall,
конфигурируемый пул игровых портов с исключением management/reserved/occupied ports,
защита параллельного выделения и освобождение после cleanup. Обновлены Compose,
deployment-инструкции и host-тесты запрета постороннего source IP, доступа target
к internal API и удаления опубликованных mappings. VM runtime не добавлялся.

Локальная регрессия: **161 passed, 7 Docker skipped**, включая Chromium.
Пользователь подтвердил host-проверку: **167 passed, 1 skipped**, 162.17 s,
`CHECKPOINT 8A PASSED`. Пропущен только opt-in Chromium, проверенный локально.
Compose-конфиги проверены. Повторная проверка: `sudo bash tests/checkpoint_08a.sh`
на изолированном тестовом Arena-хосте
без production management policy. Тест не перезаписывает существующую
`ARENA_MANAGEMENT` chain; подробности — `arena/README.md`.

Checkpoint 8A является границей между доверенным Portal и потенциально компрометируемой Arena.
Все уже работающие Stage 1–7 и Stage 8 должны быть сохранены. Изменения этого patch не должны
переписывать игровую механику, формат case, health/exploit проверки или Victory Engine без
необходимости, вызванной новой границей доверия.

## 8A.1 — Физическое разделение проекта

Проект должен быть физически разделён:

```text
cyberrange/
├── portal/
│   ├── app/
│   ├── frontend/
│   ├── migrations/
│   ├── Dockerfile
│   └── compose.yaml
├── arena/
│   ├── app/
│   │   ├── api/
│   │   ├── runtime/
│   │   ├── networking/
│   │   └── checks/
│   ├── cases/
│   ├── Dockerfile
│   └── compose.yaml
├── shared/
├── tests/
├── data/
├── README.md
├── ROADMAP.md
└── pyproject.toml
```

Ответственность:

```text
PORTAL
├── Web UI
├── players
├── matchmaking
├── team assignment
├── authoritative Match state
├── RED_KEY / BLUE_KEY
├── Victory Engine
├── key submission
├── reports
└── persistent database

ARENA
├── Arena Agent
├── runtime
├── case catalog/runtime data
├── temporary BLUE credentials
├── health checker
├── exploit checker
├── RED-zone checker
├── runtime event observations
├── temporary game networking
└── disposable targets
```

Общие DTO/API-контракты могут находиться в `shared/`, но `shared/` не должен становиться местом
для общей бизнес-логики Portal и Arena.

Portal не должен импортировать `DockerRuntime` или зависеть от установленного Docker.

---

## 8A.2 — Trust Model

Зафиксировать модель доверия:

```text
Portal              TRUSTED
   │
   ▼
Arena Agent         PRIVILEGED, MANAGEMENT-ONLY
   │
   ▼
Arena Runtime       PRIVILEGED
   │
   ▼
Game environment    DISPOSABLE / UNTRUSTED
   │
   ▼
Target              HOSTILE
```

Target считается заведомо враждебным.

Arena host считается потенциально компрометируемым и не должен обладать секретами,
позволяющими получить административный доступ к Portal.

Компрометация target не должна автоматически означать компрометацию Portal.

---

## 8A.3 — ArenaProvider

Portal работает только через общий async-интерфейс:

```python
class ArenaProvider:
    async def create_match(self, ...): ...
    async def get_match(self, match_id): ...
    async def get_events(self, match_id, ...): ...
    async def destroy_match(self, match_id): ...
```

Реализации:

```text
LocalArenaProvider
RemoteArenaProvider
```

Development:

```text
Portal
  ↓
LocalArenaProvider
  ↓
Arena Runtime
```

Production:

```text
Portal
  ↓
RemoteArenaProvider
  ↓
authenticated management channel
  ↓
Arena Agent
  ↓
Arena Runtime
```

Обе реализации должны соблюдать один контракт. Игровая логика между ними не дублируется.

---

## 8A.4 — Arena Management Plane

Arena должна предоставлять ровно один management listener для связи с Portal.

Пример:

```text
Arena host

management:
    :8443 → Arena Agent

game plane:
    dynamic ports → game environments
```

Management listener:

- не является игровым портом;
- не публикуется target-контейнерам;
- недоступен из game networks;
- разрешает соединения только от Portal;
- использует TLS;
- использует отдельную аутентификацию Portal → Arena;
- не используется игроками.

Host firewall должен реализовывать как минимум:

```text
PORTAL_IP → MANAGEMENT_PORT    ACCEPT
GAME_NET  → MANAGEMENT_PORT    DROP
OTHER     → MANAGEMENT_PORT    DROP
```

Если инфраструктура не позволяет надёжно ограничить source IP, должен использоваться
эквивалентный защищённый management channel с взаимной аутентификацией.

Arena Agent является единственной разрешённой точкой привилегированного управления Arena со
стороны Portal.

---

## 8A.5 — Не предоставлять Portal raw Docker API

Portal не должен получать прямой доступ к:

```text
/var/run/docker.sock
Docker Remote API
SSH root shell Arena
generic command execution
```

в штатном игровом control path.

Запрещены API endpoints вида:

```text
POST /exec
POST /shell
POST /command
POST /docker
```

и любые аналоги, позволяющие Portal передавать произвольную shell-команду или произвольные
Docker options.

Минимальный management API:

```text
POST   /v1/matches
GET    /v1/matches/{id}
GET    /v1/matches/{id}/events
DELETE /v1/matches/{id}
```

Portal передаёт декларативное намерение:

```json
{
  "match_id": "abc123",
  "case_id": "web-017",
  "seed": 58391
}
```

Arena самостоятельно определяет разрешённые:

- image;
- network;
- capabilities;
- resource limits;
- port mappings;
- temporary credentials;
- volumes;
- checker configuration.

Portal не может запросить `privileged=true`, host mounts, host network или произвольный image
через management API.

---

## 8A.6 — Management Plane и Game Plane

Сети должны быть разделены логически и firewall-политикой:

```text
                         INTERNET

                 ┌──────────┴──────────┐
                 │                     │
              PORTAL                 ARENA
                 │                     │
                 │ management channel  │
                 └────────────────────►│ Arena Agent
                                       │
                                       ├── management plane
                                       │      X
                                       │      X no route/access
                                       │      X
                                       └── game plane
                                              │
                                        game environments
                                              │
                                          RED / BLUE
```

Game environment не должен иметь прямого доступа к:

- Arena Agent;
- management port;
- Portal internal API;
- Portal database;
- infrastructure credentials.

Публичный Portal UI/API, необходимый игрокам для игрового интерфейса и submit KEY, может быть
доступен через обычный публичный endpoint, но это не должно открывать внутренний management API.

---

## 8A.7 — Runtime Layers

Не использовать privileged Docker-in-Docker как security boundary.

### MVP runtime

Для текущего MVP разрешена схема:

```text
Arena Host OS
    ↓
Arena Runtime / Docker
    ↓
disposable Ubuntu 20.04 target
    ↓
vulnerable service
```

Target:

- Ubuntu 20.04;
- disposable;
- без `privileged`;
- без Docker socket;
- без host network;
- без host PID namespace;
- без произвольных host mounts;
- с ограниченными capabilities;
- с CPU/RAM/PID limits.

Уязвимость case находится внутри disposable target/environment, а не на Arena host.

### Future hardened runtime

Архитектура должна допускать будущую реализацию:

```text
Arena Host
    ↓
Disposable Ubuntu VM
    ↓
Docker
    ↓
vulnerable target
```

Это предназначено для кейсов, где container escape, kernel exploitation или иная атака на
container boundary является реалистичной частью угрозы.

Не реализовывать VM runtime в MVP.

Допустимая абстракция:

```text
ArenaRuntime
├── DockerRuntime      # MVP
└── VMRuntime          # future, not implemented
```

Не создавать фиктивную `VMRuntime` реализацию, пустые сервисы или преждевременную
виртуализационную инфраструктуру.

---

## 8A.8 — Disposable State и Reset

Arena должна считаться disposable execution host.

Каждый матч:

```text
CREATE
  ↓
fresh game environment
  ↓
RUNNING
  ↓
DESTROY
  ↓
container/network/volumes/NAT state removed
```

Нормальное завершение матча обязано удалять:

- target containers;
- match-specific networks;
- temporary volumes;
- temporary credentials;
- dynamic port mappings;
- match-specific firewall/NAT state;
- checker/probe resources.

При старте Arena Agent выполняется reconciliation.

Он должен найти runtime resources, принадлежащие Cyber Range, по labels/owner metadata:

```text
cyberrange=true
owner=<arena-instance-id>
match_id=<id>
```

и обработать orphaned state.

После перезапуска Arena старое игровое состояние не должно автоматически становиться
действующим матчем.

Portal остаётся authoritative источником состояния матча.

Если Arena потеряла runtime после reboot:

```text
Arena restart
    ↓
reconciliation
    ↓
old runtime unavailable
    ↓
Portal marks affected arena/match failed or lost
    ↓
cleanup/recovery
```

Нельзя молча создавать новый target и продолжать старый матч как будто ничего не произошло.

---

## 8A.9 — Game Ports

Требование «один порт Arena» относится только к management plane.

Игровые endpoints являются отдельным game plane и могут занимать динамический диапазон портов,
который Arena выделяет контейнерам.

Пример:

```text
ARENA PUBLIC IP

8443/tcp
    → Arena Agent
    → только Portal

30000-39999/tcp
    → dynamic game mappings
    → targets
    → RED / BLUE
```

Конкретный диапазон должен быть конфигурируемым.

Arena Agent обязан предотвращать:

- конфликт port allocation;
- выдачу management port;
- выдачу зарезервированных host ports;
- сохранение port mapping после destroy;
- использование case произвольного host port вне разрешённого диапазона.

Portal получает уже готовые endpoints от Arena и не выбирает host ports самостоятельно.

---

## 8A.10 — Keys и Authority

Portal остаётся authoritative владельцем:

```text
RED_KEY
BLUE_KEY
winner
match state
```

`BLUE_KEY` никогда не передаётся Arena заранее.

Arena сообщает факты:

```text
health = HEALTHY
exploit = PATCHED
red_surface = AVAILABLE
stabilization = PASSED
```

Portal принимает игровое решение:

```text
BLUE_SECURED_TARGET
→ BLUE_KEY may be revealed
```

Arena не может самостоятельно объявить победителя.

RED objective передаётся в target только в объёме, необходимом конкретному матчу.

Ключи не должны появляться в Arena events, logs или management API responses без необходимости.

---

## 8A.11 — Secrets

На Arena запрещено хранить:

- Portal DB;
- Portal session secrets;
- Portal signing secrets;
- SSH private keys для административного доступа к Portal;
- BLUE_KEY;
- полный persistent match history.

Credential Portal → Arena должен иметь только права Arena Agent API.

Credential не должен предоставлять shell или доступ к Portal.

Все временные BLUE credentials уничтожаются вместе с матчем.

---

## 8A.12 — Failure Model

### Target compromised

Ожидаемое состояние. Матч продолжается.

### Target container escape suspected

Arena должна считаться потенциально скомпрометированной.

Новые матчи на ней не запускаются до административного восстановления/reimage.

### Arena unavailable

Portal:

```text
marks Arena OFFLINE
stops assigning new matches
marks affected provisioning/running matches appropriately
preserves persistent history
```

### Portal unavailable

Arena не принимает команды от игроков вместо Portal.

Существующие временные environments могут быть очищены по recovery/reconciliation policy.

Portal после восстановления сверяет authoritative state с Arena.

---

## 8A.13 — Existing Implementation Migration

Не удалять и не переписывать работающий код Stage 1–8 без необходимости.

После patch:

- `portal/` сохраняет существующий backend, frontend, migrations и DB;
- `arena/` содержит Agent, runtime, cases и checks;
- `shared/` содержит только необходимые контракты;
- существующий `DockerRuntime` становится реализацией MVP Arena Runtime;
- существующие health/exploit/RED-zone checks выполняются на Arena;
- существующий Victory Engine остаётся на Portal;
- существующие Local/Remote ArenaProvider сохраняются;
- Stage 8 продолжает использовать факты Arena, но решение о BLUE_KEY остаётся Portal.

---

## CHECKPOINT 8A

Checkpoint считается пройденным только если выполняются все условия.

### Project boundary

1. `portal/`, `arena/`, `shared/` физически разделены.
2. Portal не импортирует DockerRuntime.
3. Portal запускается на машине без Docker.
4. Arena Agent запускается независимо.
5. Arena не импортирует Portal ORM/DB implementation.

### Management boundary

6. Arena имеет один management listener.
7. Management listener доступен Portal и недоступен game network.
8. Portal → Arena requests аутентифицированы и защищены TLS.
9. Нет generic exec/shell/command endpoint.
10. Portal не имеет raw Docker API/socket access.
11. API принимает только декларативные операции над матчами.

### Runtime

12. Portal может создать матч через RemoteArenaProvider.
13. Arena создаёт fresh Ubuntu 20.04 target.
14. Target не privileged.
15. Target не получает Docker socket/host network/host PID.
16. Health checker работает через новую границу.
17. Exploit checker работает через новую границу.
18. RED-zone checker работает через новую границу.
19. BLUE credentials доступны только через предусмотренный Portal flow.

### Networking

20. Arena Agent management port не маршрутизируется в target/game network.
21. Game ports выделяются только из разрешённого диапазона.
22. Management/reserved host ports не могут быть выделены target.
23. После destroy game port mappings исчезают.
24. Target не может обращаться к Portal internal management API.

### Authority

25. RED_KEY/BLUE_KEY/winner остаются authoritative данными Portal.
26. BLUE_KEY не передаётся Arena.
27. Arena events содержат наблюдения, а не самостоятельное решение о победителе.
28. Victory Engine продолжает работать на Portal.

### Cleanup / restart

29. Normal destroy удаляет container/network/volumes/probes/temporary networking.
30. Arena Agent startup reconciliation обнаруживает orphaned resources.
31. Reboot Arena не продолжает старый матч на новом target молча.
32. Portal корректно обрабатывает потерянную Arena.
33. История завершённых матчей сохраняется независимо от Arena.

### Regression

34. Все Stage 1–7 tests проходят.
35. Все уже реализованные Stage 8 tests проходят.
36. LocalArenaProvider и RemoteArenaProvider проходят одинаковые contract tests.
37. Существующий web/lobby код не ломается.
38. Host-level checkpoint проверяет Docker, HTTPS/auth boundary, firewall policy,
    game port allocation и cleanup.

После выполнения:

```text
checkpoint-08a: harden portal arena boundary
```

Если исходный `checkpoint-08a: split portal and arena runtime` уже существует, создать новый commit,
не переписывая историю:

```text
checkpoint-08a: harden arena isolation and management plane
```

**Не продолжать новые продуктовые Stage, пока расширенный CHECKPOINT 8A не проходит.**

### История выполнения до расширения patch

На момент расширения 8A уже было выполнено физическое разделение:

- `portal/` содержит app, frontend, migrations и deployment Portal;
- `arena/` содержит Agent, runtime, проверки и `cases/`;
- общие контракты находятся в `shared/`;
- Python entrypoint Portal: `portal.app.main:app`;
- корневые Compose/Alembic entrypoints и существующая БД `data/` сохранены;
- Local/Remote ArenaProvider уже введены;
- Remote использует HTTPS + отдельный Bearer token;
- management был вынесен на private IP;
- generic exec/shell endpoint отсутствует;
- BLUE_KEY не передаётся Arena;
- host-проверка ранее завершилась результатом **161 passed, 1 skipped**.

Новый patch не отменяет эти результаты. Он формализует management/game plane, запрещает raw Docker
control, закрепляет disposable/reconciliation model и вводит будущую VM boundary без реализации VM
в MVP.

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

**Статус: completed (2026-10-01).**

- Миграция 0003 добавляет ключи матча, начало SECURING и время выдачи BLUE_KEY.
  RED_KEY генерируется и записывается в target при provisioning через stdin;
  BLUE_KEY остаётся в backend. Ошибка provisioning и cleanup удаляют ключи.
- `VictoryEngine.poll` / `cyberrange victory watch` используют все проверки
  Stage 7, монотонный отсчёт, отмену при неуспехе и повторную стабилизацию
  после restart или перерыва более 30 секунд. События сохраняются без ключей.
- API выдаёт `blue_key` только участникам BLUE после завершения стабилизации.
  Выдача не завершает матч; key submission относится к Stage 12.
- Пользователь выполнил `sudo bash tests/checkpoint_08.sh`:
  **141 passed**, `CHECKPOINT 8 PASSED` (включая regression tests Stage 1–7).
- Подтверждены реальные 60 секунд защиты, отмена при возврате exploit
  и firewall block, генерация ключей и доступ к BLUE_KEY только для BLUE.

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

**Статус: completed (2026-10-02).**

- Реализованы веб-лобби, Bearer-сессии, уникальные активные nickname и очередь в SQLite.
- Локальная регрессия: 145 passed, 7 skipped (6 Docker и 1 browser).
- Отдельный запуск Stage 9 с Chromium: 11 passed; проверены четыре независимые
  сессии, очередь, перезагрузка страницы, выход и запрет дубликатов.
- Миграция проверена с существующими участниками матча; связи сохранены.
- Полная проверка: `sudo bash tests/checkpoint_09.sh`; подготовка Chromium — в README.
- После Portal/Arena split браузерный сценарий повторно прошёл в локальной
  регрессии (161 passed, 7 Docker skipped); host-регрессия подтверждена пользователем
  (167 passed, 1 browser skipped, CHECKPOINT 8A PASSED). В совокупности все проверки
  Stage 9 и предыдущих этапов выполнены, включая четыре независимых браузера.

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

**Статус: completed (2026-10-02).**

- Атомарное назначение четырёх активных заявок в SQLite, seed в миграции 0006,
  случайный READY case и команды 2 RED / 2 BLUE без повторного назначения игрока.
- Фоновый provisioning через provider, повтор неопределённого запроса с тем же run ID,
  возобновление после restart Portal и автоматический запуск существующего Victory Engine.
- Лобби показывает свою команду, союзника, case и PROVISIONING/RUNNING/FAILED;
  secrets не выдаются. Выход/смена nickname запрещены после активного назначения.
- Тесты покрывают гонку claims, выход до назначения, expired sessions, пустой
  каталог, потерю связи, повтор после FAILED, restart и четыре Chromium-сессии.
- Итоговая локальная регрессия: **166 passed, 8 Docker skipped**, включая два browser tests.
- Полная проверка: `sudo bash tests/checkpoint_10.sh` на изолированном Docker-хосте
  с Chromium и без production management policy.
- Пользователь подтвердил **174 passed**, `CHECKPOINT 10 PASSED`, включая Docker,
  HTTPS Arena и Chromium.

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

При падении Portal или перезапуске Arena необходимо согласовать authoritative состояние Portal
с фактическим runtime Arena.

Arena Agent выполняет локальный reconciliation своих disposable resources, а Portal выполняет
recovery через `ArenaProvider`.

Реализовать:

```text
arena reconcile
portal arena recovery
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

### Portal crash

После restart Portal сверяет authoritative match state с Arena через `ArenaProvider` и не создаёт
дубликаты environments.

### Arena restart/crash

Arena startup reconciliation обнаруживает orphaned resources своего owner. Потерянный runtime
не должен молча заменяться новым target для уже идущего матча.

История завершённых матчей при этом сохраняется на Portal.

**Не переходить к Stage 17, пока CHECKPOINT 16 не проходит.**

---

# Stage 17 — Release Installation and End-to-End MVP Test

## 17.1 — Установка из релиза

**Статус: planned.** Релизная сборка и установка пока не реализованы.
Выполнять этот этап после предыдущих checkpoint, включая расширенный 8A.

Пользователь устанавливает Portal и Arena на два отдельных сервера из одного
версионированного релиза, без клонирования репозитория и сборки образов на серверах.

В релиз `v0.1.0` включить:

- отдельные готовые OCI-образы Portal и Arena с одинаковым version tag;
- `portal-v0.1.0.tar.gz`: Compose-конфиг, `.env.example`, инструкции Portal;
- `arena-v0.1.0.tar.gz`: Compose-конфиг, `.env.example`, настройку host firewall
  и инструкции Arena;
- Python wheel Portal с frontend, миграциями и необходимыми общими контрактами,
  а также инструкцию native-запуска для сервера без Docker;
- `SHA256SUMS`, release notes, требования к ОС/архитектуре, Docker/Compose
  и описание совместимости Portal/Arena API.

Deployment-архивы должны быть самодостаточными: без ссылок на соседние каталоги
исходного репозитория. Общие контракты упаковываются при сборке; отдельная
установка `shared/` пользователем не требуется. Первый релиз поддерживает
Linux x86_64; другие архитектуры не объявлять поддержанными без проверки.

Compose использует готовые образы с закреплёнными digest, без `build:` и `latest`.
По умолчанию устанавливаются Portal и Arena одной версии. Несовместимые версии
management API отклоняются с понятной ошибкой до создания матча.

Инструкция первичной установки должна провести пользователя через:

1. Проверку системных требований и подготовку двух серверов.
2. Настройку постоянного хранилища Portal и применение миграций.
3. Настройку management/game addresses, разрешённых игровых портов и firewall Arena.
4. Выпуск/установку TLS-сертификата, настройку доверия CA и отдельного Arena API token.
5. Запуск сервисов и проверку связи Portal → Arena без отключения TLS verification.

Релизы не содержат готовых секретов, private keys, БД или данных матчей.
Обновление описывает backup БД, завершение активных матчей, порядок обновления
сервисов и миграций. Если rollback требует восстановления backup, это указано явно.
Данные Portal сохраняются вне заменяемых контейнеров/пакетов.

Добавить автоматизированную сборку и проверку релизных артефактов. Публиковать
только прошедшую acceptance версию; наличие образов само по себе не закрывает MVP.

## 17.2 — End-to-End из релизных артефактов

Перед игровыми сценариями установить сервисы на двух чистых серверах из
подготовленного релиза, без доступа к checkout исходников. Отдельно проверить
native-установку Portal без Docker. Далее проверить оба игровых сценария через
RemoteArenaProvider; LocalArenaProvider остаётся режимом разработки.

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

Дополнительно обязательны:

- установка Portal/Arena из релизных артефактов на два чистых сервера;
- native-запуск Portal без Docker и без исходного checkout;
- HTTPS/auth, недоступность management API из target и отсутствие секретов в артефактах;
- сохранение БД/истории после перезапуска и проверка документированного обновления;
- корректные checksums и соответствие образов, архивов и пакета одной версии;
- успешные RED/BLUE end-to-end сценарии именно на установленной релизной сборке.

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

Оператор должен иметь возможность установить эту версию на отдельные Portal/Arena
серверы из опубликованного релиза по инструкции, без клонирования исходников
и локальной сборки образов. Первичная настройка серверов выполняется оператором;
проведение последующих матчей не требует ручного администрирования.

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
