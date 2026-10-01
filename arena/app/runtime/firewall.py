"""Host prerequisite for published arenas. Run setup explicitly as root.

Requires Docker's iptables backend (including iptables-nft), not its native
nftables backend. Agent verifies these dedicated chains before each create.
"""
import subprocess


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


def verify():
    for parent, chain in PARENTS.items():
        rules = [line for line in command("-S", parent).splitlines() if line.startswith("-A ")]
        if not rules or rules[0] != f"-A {parent} -i crarena+ -j {chain}":
            raise RuntimeError("Arena firewall jump missing or not first")
        actual = [line for line in command("-S", chain).splitlines() if line.startswith("-A ")]
        expected = [f"-A {chain} " + " ".join(rule) for rule in RULES[chain]]
        if actual != expected:
            raise RuntimeError("Arena firewall chain differs from policy")


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


if __name__ == "__main__":
    setup()
