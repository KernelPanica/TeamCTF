"""Intentionally vulnerable file service for the isolated training target."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/":
            body = b"Cyber Range file service\n"
        elif url.path == "/download":
            # Intended exploit: user-controlled paths can read the objective file.
            path = parse_qs(url.query).get("path", [""])[0]
            try:
                body = Path(path).read_bytes()
            except (OSError, ValueError):
                self.send_error(404)
                return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 80), Handler).serve_forever()
