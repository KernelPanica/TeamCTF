import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from arena.app.networking.ports import PortPool
from arena.app.runtime import firewall


def test_port_range_reservations_exhaustion_and_reuse():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        pool = PortPool("127.0.0.1", port, port + 4, reserved={port + 1})
        first = pool.allocate("one", {(80, "tcp"), (22, "tcp")})
        assert len(set(first.values())) == 2
        assert not {port, port + 1} & set(first.values())
        with pytest.raises(RuntimeError, match="exhausted"):
            pool.allocate("two", {(80, "tcp"), (22, "tcp")})
        assert "two" not in pool.allocations
        with pytest.raises(ValueError, match="already"):
            pool.allocate("one", {(80, "tcp")})
        pool.release("one")
        assert pool.allocate("two", {(80, "tcp"), (22, "tcp")}) == first


def test_parallel_allocations_do_not_share_ports():
    pool = PortPool("127.0.0.1", 31000, 31999, reserved={31000, 31001})
    with ThreadPoolExecutor(max_workers=8) as workers:
        mappings = list(workers.map(lambda i: pool.allocate(str(i), {(80, "tcp"), (22, "tcp")}), range(8)))
    ports = [p for mapping in mappings for p in mapping.values()]
    assert len(ports) == len(set(ports)) == 16
    assert all(31002 <= p <= 31999 for p in ports)


@pytest.mark.parametrize("first,last", [(0, 65535), (30000, 29999), (30000, 65536)])
def test_invalid_ranges(first, last):
    with pytest.raises(ValueError):
        PortPool("127.0.0.1", first, last)


def test_management_policy_is_portal_only_and_cannot_be_shadowed(monkeypatch):
    monkeypatch.setenv("ARENA_MANAGEMENT_IP", "192.168.10.2")
    monkeypatch.setenv("ARENA_PORTAL_IP", "192.168.10.1")
    monkeypatch.setenv("ARENA_MANAGEMENT_PORT", "8443")
    jump, rules = firewall.management_policy()
    assert rules == [["-s", "192.168.10.1/32", "-j", "ACCEPT"], ["-j", "DROP"]]
    outputs = {chain: "\n".join(f"-A {chain} " + " ".join(rule) for rule in policy)
               for chain, policy in firewall.RULES.items()}
    outputs["ARENA_MANAGEMENT"] = "\n".join("-A ARENA_MANAGEMENT " + " ".join(rule) for rule in rules)
    outputs["INPUT"] = "-A INPUT -i crarena+ -j ARENA_INPUT\n-A INPUT " + " ".join(jump)
    outputs["DOCKER-USER"] = "-A DOCKER-USER -i crarena+ -j ARENA_FORWARD"
    monkeypatch.setattr(firewall, "command", lambda op, chain: outputs[chain])
    firewall.verify(require_management=True)
    outputs["INPUT"] = outputs["INPUT"].replace("\n", "\n-A INPUT -j ACCEPT\n", 1)
    with pytest.raises(RuntimeError, match="shadowed"):
        firewall.verify(require_management=True)
