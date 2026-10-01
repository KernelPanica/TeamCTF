"""Execute unchanged case health scripts without Portal persistence."""
import subprocess
import sys
from pathlib import Path

from .common import checked_case_directory, target_url


class HealthChecker:
    def __init__(self, cases_directory=Path("cases"), timeout=5):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.cases_directory = Path(cases_directory).resolve()
        self.timeout = timeout

    def check(self, match, case):
        directory = checked_case_directory(match, case, self.cases_directory)
        results = {}
        for service in case.required_services:
            try:
                result = subprocess.run(
                    [sys.executable, str(directory / case.checks.health),
                     target_url(match.target_host, service.port), service.name, service.protocol],
                    cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=self.timeout,
                )
                reason = "OK" if result.returncode == 0 else "CHECK_FAILED" if result.returncode == 1 else "CHECK_ERROR"
            except subprocess.TimeoutExpired:
                reason = "TIMEOUT"
            except OSError:
                reason = "CHECK_ERROR"
            results[service.name] = {"status": "HEALTHY" if reason == "OK" else "UNHEALTHY", "reason": reason}
        return results
