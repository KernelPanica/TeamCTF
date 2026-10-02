"""Host prerequisite for published arenas. Run setup explicitly as root.

Requires Docker's iptables backend (including iptables-nft), not its native
nftables backend. Agent verifies these dedicated chains before each create.
"""
import subprocess
import os
import ipaddress


RULES = {
    "ARENA_INPUT": [
        ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ["-j", "DROP"],
    ],
    "ARENA_FORWARD": [
        ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"],
        ["-m", "physdev", "--physdev-is-bridged", "-j", "RETURN"],
        ["-j", "DROP"],
    ],
}
PARENTS = {"INPUT": "ARENA_INPUT", "DOCKER-USER": "ARENA_FORWARD"}


def command(*args):
    return subprocess.run(["iptables", "--wait", "5", *args], check=True,
                          capture_output=True, text=True).stdout


def management_policy():
    host = ipaddress.IPv4Address(os.environ["ARENA_MANAGEMENT_IP"])
    portal = ipaddress.IPv4Address(os.environ["ARENA_PORTAL_IP"])
    port = int(os.environ.get("ARENA_MANAGEMENT_PORT", "8443"))
    if host.is_unspecified or portal.is_unspecified or not 1 <= port <= 65535:
        raise ValueError("explicit management/Portal IP and valid management port required")
    jump = ["-d", f"{host}/32", "-p", "tcp", "-m", "tcp", "--dport", str(port), "-j", "ARENA_MANAGEMENT"]
    rules = [["-s", f"{portal}/32", "-j", "ACCEPT"], ["-j", "DROP"]]
    return jump, rules


def verify(require_management=False):
    for parent, chain in PARENTS.items():
        rules = [line for line in command("-S", parent).splitlines() if line.startswith("-A ")]
        if not rules or rules[0] != f"-A {parent} -i crarena+ -j {chain}":
            raise RuntimeError("Arena firewall jump missing or not first")
        actual = [line for line in command("-S", chain).splitlines() if line.startswith("-A ")]
        expected = [f"-A {chain} " + " ".join(rule) for rule in RULES[chain]]
        if actual != expected:
            raise RuntimeError("Arena firewall chain differs from policy")
    if require_management:
        jump, policy = management_policy()
        rules = [line for line in command("-S", "INPUT").splitlines() if line.startswith("-A ")]
        if len(rules) < 2 or rules[1] != "-A INPUT " + " ".join(jump):
            raise RuntimeError("Portal-only management firewall jump missing or shadowed")
        actual = [line for line in command("-S", "ARENA_MANAGEMENT").splitlines() if line.startswith("-A ")]
        if actual != ["-A ARENA_MANAGEMENT " + " ".join(rule) for rule in policy]:
            raise RuntimeError("Portal-only management policy differs")


def setup():
    # Populate before attaching; never open a hole by flushing a live chain.
    for chain, rules in RULES.items():
        try:
            command("-N", chain)
        except subprocess.CalledProcessError:
            actual = [line for line in command("-S", chain).splitlines() if line.startswith("-A ")]
            expected = [f"-A {chain} " + " ".join(rule) for rule in rules]
            if actual != expected:
                raise RuntimeError("existing Arena chain differs; stop arenas before changing host policy")
        else:
            for rule in rules:
                command("-A", chain, *rule)
    for parent, chain in PARENTS.items():
        rules = [line for line in command("-S", parent).splitlines() if line.startswith("-A ")]
        if not rules or rules[0] != f"-A {parent} -i crarena+ -j {chain}":
            command("-I", parent, "1", "-i", "crarena+", "-j", chain)
    verify()
    if "ARENA_MANAGEMENT_IP" in os.environ:
        jump, policy = management_policy()
        try:
            command("-N", "ARENA_MANAGEMENT")
        except subprocess.CalledProcessError:
            actual = [line for line in command("-S", "ARENA_MANAGEMENT").splitlines() if line.startswith("-A ")]
            if actual != ["-A ARENA_MANAGEMENT " + " ".join(rule) for rule in policy]:
                raise RuntimeError("stop Agent before replacing its management policy")
        else:
            for rule in policy:
                command("-A", "ARENA_MANAGEMENT", *rule)
        rules = [line for line in command("-S", "INPUT").splitlines() if line.startswith("-A ")]
        if len(rules) < 2 or rules[1] != "-A INPUT " + " ".join(jump):
            command("-I", "INPUT", "2", *jump)
        verify(require_management=True)


if __name__ == "__main__":
    setup()
