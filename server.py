#!/usr/bin/env python3
"""Local/Render server for the Virtual Lover demo.

Serves the static files in this folder and proxies POST /api/chat to the
DeepSeek API, so the browser never has to deal with CORS.

The API key can live on the server so that every visitor shares it:
  * DEEPSEEK_API_KEY (environment) wins if set, and survives restarts;
  * otherwise POST /api/key saves one to .secrets/api_key.json, guarded by the
    access code, until the next restart/redeploy.
A key typed in the browser is only a per-device fallback.

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
# Code required to CHANGE the shared key (not to open the site). Defaults to
# "suzanya"; override with ACCESS_CODE, or set it empty to disable the check.
ACCESS_CODE = os.environ.get("ACCESS_CODE", "suzanya").strip()

# Key saved from the website, guarded by ACCESS_CODE. Kept out of git.
SECRET_DIR = os.path.join(ROOT, ".secrets")
SECRET_FILE = os.path.join(SECRET_DIR, "api_key.json")


def read_saved_key():
    """Return the key saved through the website, or "" if there is none."""
    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("apiKey", "")).strip()
    except Exception:
        return ""


def write_saved_key(key):
    """Persist the shared key atomically, readable only by this user."""
    os.makedirs(SECRET_DIR, exist_ok=True)
    tmp = SECRET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"apiKey": key}, fh)
    os.replace(tmp, SECRET_FILE)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass


def effective_key():
    """Environment key first, then the one saved from the site."""
    return SERVER_API_KEY or read_saved_key()


class Handler(SimpleHTTPRequestHandler):
    server_version = "VirtualLoverDemo/1.0"

    # ---- never serve the saved secret ----------------------------------
    def send_head(self):
        if self.path.split("?")[0].startswith("/.secrets"):
            self.send_error(404, "Not found")
            return None
        return super().send_head()

    # ---- GET /api/config -----------------------------------------------
    def do_GET(self):
        if self.path.split("?")[0] == "/api/config":
            # Booleans only: the key itself never leaves the server.
            self._send_json(200, {
                "hasServerKey": bool(effective_key()),
                "requiresCode": bool(ACCESS_CODE),
            })
            return
        super().do_GET()

    # ---- POST /api/chat, /api/key, /api/verify --------------------------
    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/chat", "/api/key", "/api/verify"):
            self.send_error(404, "Not found")
            return

        provided = (self.headers.get("X-Access-Code") or "").strip()
        code_ok = bool(ACCESS_CODE) and hmac.compare_digest(provided, ACCESS_CODE)

        if path == "/api/verify":
            if ACCESS_CODE and not code_ok:
                self._send_json(401, {"error": {"message": "Wrong access code."}})
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/api/key":
            if ACCESS_CODE and not code_ok:
                self._send_json(401, {"error": {"message": "Wrong access code."}})
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                new_key = str(json.loads(raw.decode("utf-8")).get("apiKey", "")).strip()
            except Exception:
                self._send_json(400, {"error": {"message": 'Send {"apiKey": "sk-..."}'}})
                return

            if not new_key:
                self._send_json(400, {"error": {"message": "The API key is empty."}})
                return

            try:
                write_saved_key(new_key)
            except Exception as exc:  # noqa: BLE001 - report and continue
                self._send_json(500, {"error": {"message": "Could not save the key: %s" % exc}})
                return
            self._send_json(200, {"ok": True, "hasServerKey": True})
            return

        key = (self.headers.get("X-DeepSeek-Key") or "").strip() or effective_key()
        if not key:
            self._send_json(400, {
                "error": {
                    "message": "No shared key yet. Open Settings, paste a key and press "
                    "\"Save key for everyone\"."
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
