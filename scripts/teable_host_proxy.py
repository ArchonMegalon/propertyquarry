#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import urllib.error
import urllib.request


DEFAULT_TARGET = "https://app.teable.ai"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Small host-side relay for Teable API calls from Docker containers.")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8787)
    parser.add_argument("--target-base", default=DEFAULT_TARGET)
    return parser.parse_args()


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "EATeableRelay/1.0"

    def _forward(self) -> None:
        if self.path == "/healthz":
            payload = json.dumps(
                {
                    "status": "ok",
                    "target_base": str(getattr(self.server, "target_base", DEFAULT_TARGET)).rstrip("/"),  # type: ignore[attr-defined]
                },
                ensure_ascii=True,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return
        if not self.path.startswith("/api/"):
            payload = b'{"error":"path_not_allowed"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return
        target_base = str(getattr(self.server, "target_base", DEFAULT_TARGET)).rstrip("/")  # type: ignore[attr-defined]
        url = f"{target_base}{self.path}"
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length > 0 else None
        forwarded_headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": DEFAULT_TARGET,
            "Referer": f"{DEFAULT_TARGET}/",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        }
        for key in ("Authorization", "Content-Type"):
            value = self.headers.get(key)
            if value:
                forwarded_headers[key] = value
        request = urllib.request.Request(
            url,
            data=body,
            headers=forwarded_headers,
            method=self.command,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                self.send_response(response.status)
                for header_name in ("Content-Type",):
                    header_value = response.headers.get(header_name)
                    if header_value:
                        self.send_header(header_name, header_value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            content_type = exc.headers.get("Content-Type") if exc.headers else ""
            if content_type:
                self.send_header("Content-Type", content_type)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except Exception as exc:
            payload = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_HEAD(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    args = parse_args()
    with _ThreadingServer((args.listen_host, args.listen_port), _ProxyHandler) as server:
        server.target_base = str(args.target_base or DEFAULT_TARGET).strip().rstrip("/")  # type: ignore[attr-defined]
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
