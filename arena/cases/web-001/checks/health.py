"""Usage: python health.py http://target:80; exit 0 = healthy, 1 = unhealthy."""
import sys
from urllib.error import URLError
from urllib.request import urlopen


def check(base_url):
    try:
        with urlopen(base_url.rstrip("/") + "/", timeout=3) as response:
            return response.status == 200 and response.read(100) == b"Cyber Range file service\n"
    except (OSError, URLError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if check(sys.argv[1]) else 1)
