# TeamCTF

Red vs Blue CTF-платформа: четыре игрока получают команды 2×2 и отдельный
Ubuntu 20.04 target. RED добывает ключ через уязвимость, BLUE сохраняет работу
сервиса и блокирует эксплуатацию. После успешной стабилизации BLUE получает
свой ключ. Первая корректная отправка ключа определяет победителя; результат,
хронология и отчёт сохраняются, игровое окружение удаляется.

**Состояние проекта:** checkpoints 1–16 и архитектурный checkpoint 8A пройдены.
Stage 17 — упаковка и установка релиза `0.1.0` — в работе. Сборочные инструменты
подготовлены, но опубликованного принятого релиза пока нет. Полная приёмка
на двух чистых Linux x86_64 серверах ещё не выполнена. План: [ROADMAP.md](ROADMAP.md).

## Архитектура

| Каталог | Назначение |
| --- | --- |
| [portal/](portal/README.md) | Доверенный backend, frontend, игроки, команды, ключи, победитель, SQLite и история |
| [arena/](arena/README.md) | Отдельный Agent, Docker runtime, disposable targets, health/exploit checks |
| `shared/` | Общие контракты Portal/Arena и формат cases |
| `tests/` | Модульные, браузерные и host integration проверки |
| [release/](release/README.md) | Шаблоны релизной установки и инструкции оператора |
| `tools/` | Упаковка релиза и проверка установленного candidate |
| `data/` | Локальная БД разработки; не включается в Git или релиз |

В production Portal обращается к Arena по HTTPS через `RemoteArenaProvider`.
Portal не требует Docker и не передаёт Arena свою БД, сессии или BLUE_KEY.
Arena принимает только игровые операции; generic shell/exec endpoint отсутствует.
Host firewall изолирует target от management API и внутренних сервисов.
Для разработки на одной машине доступен `LocalArenaProvider` с тем же runtime.

## Быстрый старт из исходников

Python 3.12+, команды из корня репозитория:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test,browser]'
mkdir -p data
.venv/bin/cyberrange migrate
.venv/bin/cyberrange cases validate
.venv/bin/uvicorn portal.app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Откройте <http://127.0.0.1:8000/>. `/health` возвращает `{"status":"ok"}`,
`/version` — версии релиза и Arena API. Без настроенной Arena доступны
лобби и история; для запуска матчей нужен provider.

Для одиночной Linux-машины с доступом текущего пользователя к Docker CLI/daemon
перед запуском Portal задайте:

```bash
export ARENA_PROVIDER=local
export ARENA_CASES=arena/cases
```

Используйте один процесс Portal. Не запускайте параллельно другой local controller:
состояние Arena находится в памяти, а старт provider очищает прежние окружения.
Local targets доступны через Docker bridge; этот режим предназначен для разработки.

Для отдельных серверов настройте HTTPS Agent по [инструкции Arena](arena/README.md),
затем `ARENA_PROVIDER=remote`, `ARENA_URL`, `ARENA_TOKEN` и при необходимости
`ARENA_CA_FILE` на Portal. Проверка связи: `.venv/bin/cyberrange arena-check`.
Токен задавайте через защищённое окружение, не сохраняйте в репозитории.

## Portal через Docker Compose из исходников

```bash
docker compose up --build -d --wait
curl --fail http://127.0.0.1:8000/health
docker compose logs backend
```

Корневой Compose подключает `portal/compose.yaml`; сервис называется `backend`.
Он запускает только Portal в remote-режиме, без Docker socket. Для матчей
передайте настройки отдельной Arena. Private CA подключается через
[portal/arena-ca.yaml](portal/arena-ca.yaml). SQLite хранится в `data/cyberrange.db`,
миграции применяются перед запуском. Остановка: `docker compose down`.

## Cases и игровой цикл

Cases — доверенный код оператора, не пользовательские загрузки. Пример:
[`arena/cases/web-001/`](arena/cases/web-001/). Валидатор проверяет YAML, пути,
Ubuntu 20.04 base и синтаксис checkers без исполнения case. Для другого каталога:

```bash
.venv/bin/cyberrange cases validate --directory /path/to/cases
```

Четыре участника очереди автоматически получают матч. RED видит адреса сервисов;
BLUE дополнительно получает временный SSH-доступ с sudo. Health и exploit checks
выполняются на Arena, решение о выдаче BLUE_KEY и победителе принимает Portal.
После матча все участники видят сохранённый отчёт и timeline и могут встать в
очередь снова. Каждый новый матч получает чистый target и новые ключи.

## Проверки

Быстрая регрессия без Docker и браузера (host-тесты будут пропущены):

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Браузерная регрессия с Chromium:

```bash
.venv/bin/python -m playwright install chromium
RUN_BROWSER=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Полная проверка текущих исходников и упаковки — только на изолированном Linux
Docker-хосте с OpenSSH, OpenSSL и iptables backend Docker:

```bash
sudo .venv/bin/python -m playwright install chromium
sudo bash tests/checkpoint_17.sh
```

Chromium устанавливается для того пользователя, который запускает тесты.
Host-проверки устанавливают firewall и создают реальные targets. Требуется
доступный non-loopback IPv4; `ARENA_TEST_MANAGEMENT_IP` переопределяет его выбор.
Не запускайте на production Arena с действующей `ARENA_MANAGEMENT` policy.
Подробности: [Arena acceptance](arena/README.md#acceptance).

`checkpoint_17.sh` не объявляет MVP завершённым: установка готовых артефактов
на двух чистых серверах проверяется отдельно. Последняя локальная регрессия:
199 passed, 10 host checks skipped; native wheel smoke также прошёл.

## Данные и восстановление

По умолчанию `DATABASE_URL=sqlite:///data/cyberrange.db`. Для native установки
выберите постоянный абсолютный путь. Используйте один worker Portal.
Победитель, события, итоговый отчёт и намерение cleanup сохраняются транзакционно.
Portal повторяет неудавшееся удаление; потерянный Arena instance переводит матч
в FAILED без подмены новым target. Restart Agent удаляет ресурсы своего owner.

Для ручного восстановления сначала остановите соответствующий controller.
На Arena, с прежним `ARENA_OWNER` и доступом к Docker:

```bash
.venv/bin/python -m arena.app.cli reconcile
```

На Portal, с настроенным remote provider:

```bash
.venv/bin/cyberrange portal arena recovery
```

Ненулевой код выхода требует устранения причины и повторной попытки.
Сохраните backup SQLite перед обновлением. Инструкции миграции, обновления и
rollback находятся в [руководстве релиза](release/README.md).

## Подготовка релиза

Без Docker можно собрать wheel Portal и build contexts:

```bash
.venv/bin/python tools/release.py prepare --output dist/candidate
```

Для сборки трёх OCI-образов и deployment-архивов нужен Docker Buildx:

```bash
bash tools/build_release.sh dist/candidate-images REGISTRY/PROJECT
```

В обоих случаях используйте новый пустой каталог. Скрипт не публикует образы.
Deployment Compose закрепляет реальные image digest; релизная Arena использует
готовый target без сборки на сервере. Native wheel содержит frontend, миграции
и контракты, но не Arena runtime. Установка, TLS, firewall, checksums и приёмка:
[release/README.md](release/README.md).
