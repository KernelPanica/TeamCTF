# Portal

Доверенный сервис: игроки, лобби, команды, игровые решения, ключи и история.
Матчи, timeline и итоговые отчёты сохраняются в SQLite; исполнение окружений
передаётся Arena через provider.

Для установки из candidate без checkout, включая native Portal без Docker,
см. [руководство релиза](../release/README.md). Stage 17 ещё не принят на двух
чистых серверах. Ниже описана разработка из исходников.

- `app/` — Python backend, модуль `portal.app.main:app`.
- `frontend/` — веб-интерфейс.
- `migrations/`, `alembic.ini` — схема SQLite.
- `Dockerfile`, `compose.yaml` — запуск Portal.
- `arena-ca.yaml` — дополнительный Compose-конфиг для private CA Arena.

Команды выполняются из корня репозитория:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test,browser]'
mkdir -p data
.venv/bin/cyberrange migrate
.venv/bin/uvicorn portal.app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Или `docker compose up --build -d --wait`: корневой Compose подключает
`portal/compose.yaml`. База по-прежнему находится в корневом `data/`,
имя Compose-сервиса `backend` сохранено для совместимости. По умолчанию порт
доступен только на localhost. Для доступа с другой машины задайте
`PORTAL_BIND=0.0.0.0` в `.env`, затем разрешите TCP/8000 в firewall или
используйте HTTPS reverse proxy.

Начните с `cp .env.example .env`. `ARENA_TOKEN` должен быть случайным и
одинаковым на Portal и Arena; реальный `.env` не коммитьте. Для подключения к
сертификату Arena задайте `ARENA_CA_FILE` и смонтируйте только CA-сертификат.

Portal работает без Docker при использовании RemoteArenaProvider.
По умолчанию выбран remote-режим. Без ARENA_URL/ARENA_TOKEN доступны лобби
и история; игровые окружения не создаются. Настройте ARENA_URL (HTTPS),
ARENA_TOKEN и ARENA_CA_FILE для private CA. `cyberrange arena-check` проверяет
готовность Agent и совпадение версии релиза/API без создания target.

`DATABASE_URL` задаёт постоянный путь к БД. Исходный Compose использует
`data/` и сервис `backend`; релизный — volume `portal-data` и сервис `portal`.
Не заменяйте конфиги между этими режимами без переноса настроек и данных.

С настроенным provider четыре игрока из очереди автоматически получают матч 2×2;
provisioning и Victory Engine работают в фоне. Используйте один worker Portal.
Общие контракты находятся в `shared/`; тесты двух сервисов — в `tests/`.
Настройка HTTPS и отдельного Arena-сервера: [arena/README.md](../arena/README.md).
Локальный режим и тесты: [корневой README](../README.md).
