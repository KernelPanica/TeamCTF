# Portal

Доверенный сервис: игроки, лобби, команды, игровые решения, ключи и история.

- `app/` — Python backend, модуль `portal.app.main:app`.
- `frontend/` — веб-интерфейс.
- `migrations/`, `alembic.ini` — схема SQLite.
- `Dockerfile`, `compose.yaml` — запуск Portal.
- `arena-ca.yaml` — дополнительный Compose-конфиг для private CA Arena.

Команды выполняются из корня репозитория:

```bash
.venv/bin/pip install -e '.[test,browser]'
.venv/bin/alembic -c portal/alembic.ini upgrade head
.venv/bin/uvicorn portal.app.main:app --reload
```

Или `docker compose up --build -d --wait`: корневой Compose подключает
`portal/compose.yaml`. База по-прежнему находится в корневом `data/`,
имя Compose-сервиса `backend` сохранено для совместимости.

Portal работает без Docker при использовании RemoteArenaProvider.
Общие контракты находятся в `shared/`; тесты двух сервисов — в `tests/`.
Настройка HTTPS и отдельного Arena-сервера: [arena/README.md](../arena/README.md).
