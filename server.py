#!/usr/bin/env python3
"""Local/Render server for the Virtual Lover demo.

Serves the static files in this folder and proxies POST /api/chat to the
DeepSeek API, so the browser never has to deal with CORS. The API key is
sent per request in the X-DeepSeek-Key header and is never stored here.

Usage:
    python server.py            # http://127.0.0.1:8000
    python server.py 9000       # custom port

On a PaaS (Render, Railway, Fly.io, ...) the platform injects PORT and expects
the app to listen on 0.0.0.0, which this script does automatically.

Requires Python 3.8+. No third-party packages.
"""

import hmac
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = "https://api.deepseek.com/chat/completions"

# Optional server-side key: set this on Render (or in your shell) and the page
# works on every device without anyone typing a key. Never commit it.
SERVER_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
# Shared code that gates the whole site, so strangers cannot spend the server
# key. Defaults to "suzanya"; override with the ACCESS_CODE env var, or set it
# to an empty string to disable the gate.
ACCESS_CODE = os.environ.get("ACCESS_CODE", "suzanya").strip()


class Handler(SimpleHTTPRequestHandler):
    server_version = "VirtualLoverDemo/1.0"

    # ---- GET /api/config -----------------------------------------------
    def do_GET(self):
        if self.path.split("?")[0] == "/api/config":
            # Booleans only: the key itself never leaves the server.
            self._send_json(200, {
                "hasServerKey": bool(SERVER_API_KEY),
                "requiresCode": bool(ACCESS_CODE),
            })
            return
        super().do_GET()

    # ---- POST /api/chat and /api/verify --------------------------------
    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/chat", "/api/verify"):
            self.send_error(404, "Not found")
            return

        if ACCESS_CODE:
            provided = (self.headers.get("X-Access-Code") or "").strip()
            if not hmac.compare_digest(provided, ACCESS_CODE):
                self._send_json(401, {"error": {"message": "Wrong access code."}})
                return

        if path == "/api/verify":
            self._send_json(200, {"ok": True})
            return

        key = (self.headers.get("X-DeepSeek-Key") or "").strip() or SERVER_API_KEY
        if not key:
            self._send_json(400, {
                "error": {
                    "message": "No API key. Add one in Settings, or set DEEPSEEK_API_KEY "
                    "on the server."
                }
            })
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""

        upstream_request = urlrequest.Request(
            UPSTREAM,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "Accept": "text/event-stream, application/json",
            },
            method="POST",
        )

        try:
            with urlrequest.urlopen(upstream_request, timeout=180) as upstream:
                self.send_response(200)
                content_type = upstream.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # Pass the bytes through untouched: works for both JSON and SSE.
                while True:
                    chunk = upstream.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urlerror.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001 - demo code, report and move on
            self._send_json(502, {"error": {"message": "Proxy error: %s" % exc}})

    # ---- helpers -------------------------------------------------------
    def _send_json(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))


def main():
    env_port = os.environ.get("PORT")
    # Locally we stay on loopback; on Render (PORT is set) we must bind 0.0.0.0.
    host = os.environ.get("HOST") or ("0.0.0.0" if env_port else "127.0.0.1")

    port = 8000
    if env_port:
        try:
            port = int(env_port)
        except ValueError:
            print("PORT must be a number, got %r" % env_port)
            return 1
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("Port must be a number, e.g. python server.py 8000")
            return 1

    os.chdir(ROOT)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("Virtual Lover demo running at http://%s:%d" % (host, port))
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
