# Cyber Range 0.1.0 — release candidate

**Не опубликованный/не принятый MVP.** Для завершения acceptance нужны два чистых
Linux x86_64 сервера. Скрипт игровых сценариев сам по себе не закрывает Stage 17.
Поддержку других архитектур не заявляем. Требуются Python >=3.12 для native Portal,
Docker Engine с iptables backend (iptables-nft допустим), Compose v2 на Arena,
OpenSSH client на машине acceptance. Buildx, setuptools и wheel нужны сборщику,
skopeo — оператору registry, не игрокам.

## Сборка оператором релиза

Из исходного checkout (только на машине сборки):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test,browser]'
.venv/bin/python tools/release.py prepare --output dist/candidate
# Или полная сборка трёх OCI-образов и архивов с реальными digest:
bash tools/build_release.sh dist/candidate-images REGISTRY/PROJECT
```

Используйте новый пустой каталог. Результат полной сборки: wheel Portal, отдельные минимальные
build contexts, три OCI archives (Portal, Arena, Ubuntu target), deployment tar.gz, release.json, SHA256SUMS и
accept_release.py и native_smoke.py. Команда `prepare` создаёт wheel и контексты,
но не OCI-образы или deployment-архивы: для них нужна полная сборка через Buildx.
В wheel нет Arena runtime; каталог case включён как статические
данные для существующего валидатора. Arena context не содержит Portal или его БД.
release.json фиксирует commit и dirty flag: перед финальной сборкой нужен чистый
commit. Подготовленный candidate нельзя объявлять принятым релизом.

Скрипт не публикует образы. Для испытаний загрузите OCI в закрытый staging registry
с сохранением digest, например `skopeo copy --all --preserve-digests
oci-archive:portal-v0.1.0.oci.tar docker://REGISTRY/PROJECT/portal:0.1.0` (аналогично
Arena и target-web-001). Используйте registry prefix из сборки. Перед запуском проверьте digest в
registry против release.json; Compose использует именно эти digest, не tag/latest.
Публикация финального release допускается только после acceptance.

`dist/`, локальные `.env`, TLS-файлы и базы исключены из Git. Шаблоны
`.env.example` входят в исходники и deployment-архивы. Артефакты распространяются
отдельно от Git checkout. Уже собранные candidates не обновляются при изменении
исходников или README: собирайте новый каталог и проверяйте его SHA256SUMS.

## Общая подготовка серверов

1. Проверьте `sha256sum -c SHA256SUMS` в каталоге артефактов и доверенный источник
   самого manifest. Распакуйте portal-v0.1.0.tar.gz только на Portal, arena-v0.1.0.tar.gz
   только на Arena. Checkout, соседние папки исходников и сборка на серверах не нужны.
2. Выделите management IPv4 Arena и game IPv4, доступный игрокам. Они могут
   совпадать, если в `.env` явно задано `ARENA_SINGLE_IP=1`: management port
   `8443` резервируется и не используется игровыми сервисами.
   Если private/VPN сети нет, допустим второй публичный адрес, но firewall должен
   разрешать management только фактическому source IP Portal с учётом NAT. Не
   открывайте management port всему Интернету.
3. Выпустите TLS certificate Arena с SAN management DNS/IP от вашей CA. Поместите
   tls.crt/tls.key в `arena/tls/`; private key доступен только оператору/Agent.
   На Portal передайте **только CA certificate**, как `portal/arena-ca.pem`.
4. Создайте отдельный случайный API token (`openssl rand -hex 32`) и сохраните его
   в локальных .env с правами 0600 на обоих серверах. В архиве секретов нет.
   Пароли/ключи Portal, его БД и SSH private keys на Arena не копируются.

## Arena

В распакованном каталоге arena скопируйте .env.example в .env, задайте права
`chmod 600 .env` и заполните значения. Все команды этого раздела выполняются
из этого каталога. ARENA_OWNER
должен быть стабильным и уникальным. Релизный Agent использует встроенный manifest
case-images.json с закреплённым digest target: сборки на сервере нет. Для private
registry выполните docker login и docker pull для case_images.web-001 из release.json
на Arena-хосте заранее; Agent использует уже загруженный daemon image без передачи
registry credentials в target. Если образ публичный, Agent может скачать его по digest.
Диапазон игровых портов не пересекается с
management/зарезервированными портами. Docker должен быть запущен.
Примените firewall на **выделенном** Arena-хосте с теми же environment variables:

```bash
set -a
. ./.env
set +a
sudo --preserve-env=ARENA_MANAGEMENT_IP,ARENA_MANAGEMENT_PORT,ARENA_PORTAL_IP python3 firewall.py
docker compose up -d
docker compose logs agent
```

На системе с sudo, удаляющим переменные, явно передайте эти три значения через
sudo env. Скрипт использует iptables и DOCKER-USER; правила должны применяться
после старта Docker и при reboot **до** Agent. Оформите эту последовательность
в systemd вашей ОС. Agent проверяет правила и откажется стартовать без них.
Target не получает docker.sock или token. Agent имеет Docker socket и NET_ADMIN;
его management plane доступен только Portal. Не открывайте 8443 всему Интернету.

## Portal в контейнере

В распакованном каталоге portal скопируйте .env.example в .env, задайте права
`chmod 600 .env`, заполните его и добавьте arena-ca.pem. Из этого каталога выполните:

```bash
docker compose up -d --wait
docker compose exec portal cyberrange arena-check
```

Portal не получает Docker socket. SQLite находится в именованном volume portal-data;
не выполняйте `docker compose down -v`. Backend слушает localhost:8000 по умолчанию.
Опубликуйте его через HTTPS reverse proxy вашей инфраструктуры; game/management
адреса Arena не должны совпадать с private адресами внутренних сервисов Portal.

## Native Portal без Docker и без checkout

На Portal установите wheel из этого же релиза в venv. Интернет нужен для Python
зависимостей; для offline установки оператор заранее готовит wheelhouse для ОС/Python.

```bash
python3 -m venv /opt/cyberrange/venv
/opt/cyberrange/venv/bin/pip install ./cyberrange-0.1.0-py3-none-any.whl
mkdir -p /var/lib/cyberrange
export DATABASE_URL=sqlite:////var/lib/cyberrange/cyberrange.db
export ARENA_PROVIDER=remote
export ARENA_URL=https://ARENA-MANAGEMENT:8443
export ARENA_CA_FILE=/etc/cyberrange/arena-ca.pem
# ARENA_TOKEN задайте из защищённого environment file, не из истории shell.
/opt/cyberrange/venv/bin/cyberrange migrate
/opt/cyberrange/venv/bin/cyberrange cases validate
/opt/cyberrange/venv/bin/cyberrange arena-check
/opt/cyberrange/venv/bin/uvicorn portal.app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Запускайте от отдельного непривилегированного пользователя с доступом только к
своей БД и CA, через ваш service manager. Браузеру нужен HTTPS reverse proxy.
Wheel содержит frontend, migrations, shared contracts и catalog; установка shared
отдельно не нужна. Для LocalArenaProvider нужен checkout с Arena runtime;
релизный wheel предназначен для RemoteArenaProvider.

Автоматическая проверка установленного wheel вне checkout, с недоступным Docker:
`python3 native_smoke.py --python /opt/cyberrange/venv/bin/python`.

## Совместимость и проверка

API: `/v1`. Перед каждым созданием Portal проверяет аутентифицированный `/v1/info`:
API version 1, release version 0.1.0 и готовность Agent. Иная версия отклоняется
до POST /v1/matches. TLS verification никогда не отключается. Обновляйте обе стороны
вместе. `cyberrange arena-check` проверяет связь без создания игрового окружения.

С машины, которой доступны Portal и игровые адреса Arena:

```bash
python3 accept_release.py --portal-url https://PORTAL --ca-file portal-ca.pem --output acceptance.json
```

Скрипт создаёт четыре временные сессии, проводит RED victory через уязвимый download,
затем BLUE victory через SSH patch и стабилизацию, проверяет одинаковые reports и
недоступность target после cleanup. В evidence нет токенов/паролей/игровых ключей.
Запускайте на выделенной acceptance установке без других игроков. При прерывании
не публикуйте candidate; сохраните диагностику и очистите оставшийся тестовый match
через обычное администрирование Arena/Portal.

Обязательная дополнительная acceptance-проверка оператором: оба сервера чистые,
исходников нет; native Portal запущен без Docker; после игр на Arena нет ресурсов
с labels cyberrange=true и arena_owner=<owner>; target не достигает management
или внутренних API; после restart/обновления отчёты и история сохранены. Эти
проверки нельзя заменить успешным запуском build или двумя локальными контейнерами.

## Обновление и rollback

1. Остановите приём новых игроков на reverse proxy, дождитесь завершения матчей
   и cleanup. Остановите Portal, затем Arena. Не запускайте старый и новый Agent
   одновременно с одним ARENA_OWNER.
2. Скопируйте остановленную SQLite БД (или backup через SQLite backup API), .env,
   CA и конфигурацию. Проверьте backup. Не включайте его в release artifacts.
3. Проверьте SHA256SUMS нового релиза, сохраните старые compose/env/image digests.
   Обновите Arena, затем Portal той же версии; Portal применяет миграции перед
   запуском. Для native Portal сначала установите новый wheel, затем migrate.
4. Выполните arena-check, health и acceptance smoke, после чего верните игроков.
   История находится в прежнем volume/пути БД, а не в контейнере/venv.
5. Rollback: остановите оба сервиса, восстановите согласованный backup БД и старые
   версии **обоих** сервисов/config. Простая замена image tag не гарантирует rollback
   миграций. Сохраняйте старые артефакты, digests и backup до завершения проверки.

Release notes: checkpoints 1–16, Portal/Arena split, Docker isolation, role access,
RED/BLUE victory, persistent timeline/report, restart recovery. Stage 17 acceptance
остаётся незавершённым до документированной проверки установленных артефактов.
