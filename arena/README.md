# Arena Agent

This guide covers development from a checkout and the Portal/Arena trust boundary.
For candidate deployment without building on the server, see the
[release installation guide](../release/README.md). Checkpoints 1–16, including
8A, have passed; Stage 17 release acceptance on two clean hosts remains pending.

Portal owns SQLite, players, teams, game state, RED_KEY/BLUE_KEY, decisions and
history. Arena owns disposable Docker environments and checker execution.
Agent never imports Portal code or connects to its database. It receives only
the RED objective needed by the case, never BLUE_KEY or Portal session tokens.

The management token authorizes requests **to Arena only**. Portal accepts no
callbacks from Arena. Arena can falsify observations if compromised; this split
protects Portal credentials, not the integrity of an already compromised host.

## Portal without Docker

Install the project on the trusted Portal machine, then:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
mkdir -p data
export DATABASE_URL=sqlite:///data/cyberrange.db
export ARENA_PROVIDER=remote
export ARENA_URL=https://arena-management.example:8443
# Load ARENA_TOKEN from a protected environment file (at least 32 characters).
export ARENA_CA_FILE=/etc/cyberrange/arena-ca.crt
.venv/bin/cyberrange migrate
.venv/bin/cyberrange arena-check
.venv/bin/uvicorn portal.app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Omit `ARENA_CA_FILE` for a certificate trusted by the system CA store. HTTPS,
hostname verification and certificate verification cannot be disabled. Redirects
are not followed. The Portal starts and serves lobby/history without any Arena
configuration; provisioning requires a configured provider.

Existing Portal Compose supports `ARENA_URL` and `ARENA_TOKEN`. For a private CA:

```bash
docker compose -f compose.yaml -f portal/arena-ca.yaml up --build -d --wait
```

Only the public CA certificate is mounted into Portal. Arena's TLS private key
stays on Arena. There is no Docker socket mount in Portal Compose.

## Dedicated Arena machine

Linux, Docker Engine with its **iptables firewall backend** (iptables-nft is
supported), OpenSSH client for tests, and two separate assigned IPv4 addresses
are required. Docker's native nftables backend is not supported by this first
version: absence of `DOCKER-USER` causes a closed failure. IPv6 publication is
not enabled. Do not run multiple Agent workers or replicas against one owner.

Before migration, destroy old active environments using the old controller.
Historical SQLite rows are retained; old live arenas are not adopted.

Set a stable unique `ARENA_OWNER`, a separate `ARENA_GAME_IP`, dedicated
`ARENA_MANAGEMENT_IP`, the allowed source `ARENA_PORTAL_IP`, player-reachable `ARENA_PUBLIC_HOST`, `ARENA_TOKEN`, and
`ARENA_CERT_DIR` containing `tls.crt` and `tls.key`. The certificate SAN must
match `ARENA_URL`. Keep these variables on Arena; do not copy Portal `.env`,
SQLite, session secrets or SSH private keys there.

Management and game may share one IP when ports are distinct. Set
`ARENA_SINGLE_IP=1` explicitly in that case; the management port is reserved
from the game pool and remains restricted to `ARENA_PORTAL_IP` by firewall.

Install host policy before starting Agent (from the repository with its venv):

```bash
sudo -E .venv/bin/python -m arena.app.runtime.firewall
docker compose -f arena/compose.yaml up --build -d
```

The policy creates `ARENA_INPUT`, `ARENA_FORWARD` and `ARENA_MANAGEMENT` chains and places
their jumps first in INPUT/DOCKER-USER for `crarena*` bridges. It does not flush
host or Docker chains, or flush a live Arena chain. Existing conflicting Arena
policy requires stopping arenas and correcting it before startup. The management
rule follows the game-network deny rule and permits only `ARENA_PORTAL_IP` to
`ARENA_MANAGEMENT_IP:ARENA_MANAGEMENT_PORT` (default 8443); every other source is
dropped. Preserve these exported configuration variables when using sudo.

Management listens only on the configured management address. If no private/VPN
network is available, a second public IPv4 is supported, but the host policy
must allow the port only from `ARENA_PORTAL_IP`; never expose it to the Internet.
Host policy denies
new target-to-host connections, including management and host gateway, and new
connections outside the target's bridge. Responses to player/controller traffic
and same-bridge RED probes remain possible. Bind management behind a private
network/firewall accessible only to Portal; a private management network remains
preferable.

Remote targets publish SSH and required service ports on `ARENA_GAME_IP`; Docker
binds explicitly allocated host ports. `ARENA_GAME_PORT_MIN`/`ARENA_GAME_PORT_MAX`
bound the pool (defaults 30000–39999). `ARENA_RESERVED_PORTS` is a comma-separated
denylist (defaults 22,80,443); the management port is always excluded, even when
inside the game range. Allocations are serialized across active matches, occupied
host ports are skipped and exhaustion fails provisioning. An external bind race
fails safely in Docker and triggers cleanup. Agent returns the actual port mapping. Remote bridges use
host filtering instead of Docker `--internal`, since players need published
ports. Resource limits, capabilities, service behavior and checker contracts
remain unchanged. Agent verifies host policy on startup and before each create.
Recheck policy after Docker/firewall restarts; do not operate arenas with a
firewall manager that removes these rules.

Agent needs Docker socket access and host NET_ADMIN to verify policy; those
privileges belong only on the disposable Arena server. Target containers never
receive the socket, management token or TLS key. Image COPY operations include
only Arena, shared contracts and cases; no Portal database or code is copied.

For a native Agent process, set the same variables plus `ARENA_TLS_CERT`,
`ARENA_TLS_KEY` and `ARENA_CASES`, then run `python -m arena.app.serve` with
Docker/firewall permissions. The entrypoint requires TLS and a dedicated
non-loopback IPv4 bind IP.

## Operations and recovery

`ArenaProvider` exposes async `create_match(CreateMatch)`, `get_match(id)`,
`get_events(id, after=0)` and `destroy_match(id)`. Portal provisioning functions
are async and accept a provider; they no longer accept DockerRuntime.

Agent exposes authenticated GET `/v1/info`, POST `/v1/matches`, GET `/v1/matches/{id}`, GET
`/v1/matches/{id}/events?after=N` and DELETE `/v1/matches/{id}`. Unversioned routes
are not supported; update Portal and Agent together after stopping old matches. No shell/exec/Docker API is
available. Cases are installed by the Arena operator, not uploaded through API.
Before creating a match, Portal checks `/v1/info` for readiness, API version 1
and matching release version 0.1.0. `cyberrange arena-check` performs this check
without provisioning. Update both services together.

Source development builds the case target through Docker. Release Agent instead
loads `ARENA_CASE_IMAGES`, a JSON case-ID-to-image-digest manifest built into its
image, and uses prebuilt targets. For a private registry, pre-pull the target
on the Arena host; do not pass registry credentials to target containers.

Creation returns 202 with an execution state. Poll until READY or FAILED.
Repeating a create with the same ID/run/parameters reuses the operation; different
parameters conflict. A deleted run remains a non-secret tombstone until restart.
Use a new Portal match ID for a new environment. DELETE waits for in-flight
creation/checks, then removes containers, networks and volumes. Cleanup failure
revokes API credentials immediately and is retriable.

Active events live in a bounded in-memory journal (1,000 entries, pages of 100).
Portal persists observations with a cursor transaction and records downtime and
game decisions itself. Cursor gaps are explicit failures and cancel securing;
they never count as successful defense. Run one game observer per match.
No fresh observation does not grant a key; a gap longer than 30 seconds cancels
the defense stabilization countdown.

On Agent restart, startup removes resources carrying its owner label before
accepting creates. No full match database or credentials are recovered. Portal
reconciles known running executions every five seconds, marks lost arenas FAILED,
revokes credentials and retries pending cleanup. Network timeouts alone do not
mean a target was destroyed. A provisioning call with an uncertain POST result
can be retried using its persisted run ID and original keys.

Match access retains `host`; BLUE retains `username`, `password` and `port` (now
the published SSH port). New `services` entries contain name/host/port/protocol.
RED never receives SSH credentials. Legacy history without endpoints still reads.

## Local development

Set `ARENA_PROVIDER=local` and optionally `ARENA_CASES=arena/cases` for a **single Portal
process** with Docker access. LocalArenaProvider calls the same ArenaService and
Executor without HTTP; targets retain internal bridge/IP access. Do not launch
another local-provider process or CLI against the same active owner: execution
state is process-local and startup cleanup is intentional. To exercise separate
Portal/CLI processes on one machine, run Agent separately with RemoteArenaProvider.

## Acceptance

Prepare the venv and run on the Linux Docker host as root:

```bash
.venv/bin/pip install -e '.[test,browser]'
sudo .venv/bin/python -m playwright install chromium
sudo bash tests/checkpoint_17.sh
```

This explicitly installs/verifies the dedicated host firewall chains and runs
the current source, browser and packaging regression, including 8A integration.
The real integration starts a temporary Agent with a trusted test TLS certificate,
creates Ubuntu 20.04 through RemoteArenaProvider, verifies published HTTP/SSH,
checks health/exploit, attempts management access from the hostile target,
deletes resources and tests abrupt Agent restart cleanup. It uses a unique owner
and temporary Portal databases. It needs a private non-loopback host IPv4 address;
set `ARENA_TEST_MANAGEMENT_IP` to override automatic route-based selection.

Run host acceptance on an isolated test Arena without an installed
`ARENA_MANAGEMENT` chain. The test installs its own temporary Portal-source policy
before starting Agent and removes it after stopping Agent; it refuses to overwrite
an existing production policy. It also checks that a different source IP cannot
open TCP to management, internal host APIs are unreachable from target, published
ports are in the permitted range, reserved ports are excluded and mappings close
after deletion. Base game-network chains remain installed.

Chromium regression remains independently available with `RUN_BROWSER=1`.
Install Chromium as the user running the tests. The Stage 17 script reports
source/artifact test success, not MVP completion. Acceptance of installed release
artifacts on two clean hosts is a separate requirement in the release guide.
