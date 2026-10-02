"""Bounded host port reservations shared by all executions in one Agent."""
import socket
from threading import Lock


class PortPool:
    def __init__(self, host, first=30000, last=39999, reserved=()):
        if not 1024 <= first <= last <= 65535:
            raise ValueError("game port range must be within 1024..65535")
        self.host, self.first, self.last = host, first, last
        self.reserved = set(reserved)
        if any(not 1 <= port <= 65535 for port in self.reserved):
            raise ValueError("invalid reserved host port")
        self.allocations = {}
        self.lock = Lock()

    def allocate(self, match_id, services):
        with self.lock:
            if match_id in self.allocations:
                raise ValueError("match already owns game ports")
            used = self.reserved | {p for mapping in self.allocations.values() for p in mapping.values()}
            result = {}
            for service in sorted(set(services)):
                protocol = service[1]
                if protocol not in ("tcp", "udp"):
                    raise ValueError("unsupported protocol")
                for port in range(self.first, self.last + 1):
                    if port in used:
                        continue
                    # Check both protocols: a number belongs to one game endpoint.
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                            tcp.bind((self.host, port))
                            udp.bind((self.host, port))
                    except OSError:
                        continue
                    result[service] = port
                    used.add(port)
                    break
                else:
                    raise RuntimeError("game port range exhausted")
            # Docker's bind remains authoritative against external processes racing
            # after our check; a bind failure fails provisioning and triggers cleanup.
            self.allocations[match_id] = result
            return result.copy()

    def release(self, match_id):
        with self.lock:
            self.allocations.pop(match_id, None)
