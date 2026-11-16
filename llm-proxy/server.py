"""llm-proxy: holds the real DeepSeek API key so the agent container never sees it.

The agent talks to http://llm-proxy:8080/v1 with a dummy key. This server
forwards only the chat-completions and model-list calls to DeepSeek, swaps in
the real key, and applies a simple per-minute rate limit. Everything else gets
a 404, so the key can't be used for anything but model calls.

Standard library only, so the container that holds the key installs no
third-party packages.
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("UPSTREAM_BASE_URL", "https://api.deepseek.com").rstrip("/")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(8 * 1024 * 1024)))
# Longest wait for DeepSeek to connect or send the next bytes of a reply.
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "600"))

# Method -> allowed upstream paths (after stripping an optional /v1 prefix).
ALLOWED = {
    "POST": {"/chat/completions"},
    "GET": {"/models"},
}
KEY_CONFIGURED = bool(API_KEY) and not API_KEY.startswith("replace-me")


class RateLimiter:
    """Admits at most `per_minute` requests in any 60-second window."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.recent = deque()  # monotonic times of admitted requests
        self.lock = threading.Lock()

    def allow(self):
        now = time.monotonic()
        with self.lock:
            while self.recent and now - self.recent[0] > 60:
                self.recent.popleft()
            if len(self.recent) >= self.per_minute:
                return False
            self.recent.append(now)
            return True


limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)


class ClientError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "llm-proxy"
    sys_version = ""

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        started = time.monotonic()
        self.status = 0
        url_path = urllib.parse.urlsplit(self.path).path
        try:
            self.route(url_path)
        except ClientError as err:
            self.send_error_json(err.status, str(err))
        except (BrokenPipeError, ConnectionResetError):
            self.status = self.status or 499  # the agent hung up
        finally:
            if url_path != "/healthz":
                stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                ms = round((time.monotonic() - started) * 1000)
                print(f"{stamp} {self.command} {url_path} -> {self.status} {ms}ms", flush=True)

    def route(self, url_path):
        if self.command == "GET" and url_path == "/healthz":
            self.status = 200
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        path = url_path[3:] if url_path.startswith("/v1/") else url_path
        if path not in ALLOWED.get(self.command, ()):
            raise ClientError(404, "This proxy only forwards chat completions and the model list.")
        if not KEY_CONFIGURED:
            raise ClientError(503, "The proxy has no DeepSeek API key configured.")
        if not limiter.allow():
            raise ClientError(429, f"Rate limit of {RATE_LIMIT_PER_MINUTE} requests per minute reached. Try again shortly.")

        body = self.read_body() if self.command == "POST" else None
        self.forward(path, body)

    def read_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = bytearray()
            while True:
                try:
                    size = int(self.rfile.readline(1024).split(b";")[0].strip(), 16)
                except ValueError:
                    raise ClientError(400, "Malformed chunked request body.")
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass  # skip trailers
                    return bytes(body)
                if len(body) + size > MAX_BODY_BYTES:
                    raise ClientError(413, "request body too large")
                body += self.rfile.read(size)
                self.rfile.readline(1024)  # CRLF after each chunk
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ClientError(400, "Invalid Content-Length.")
        if length > MAX_BODY_BYTES:
            raise ClientError(413, "request body too large")
        return self.rfile.read(length)

    def forward(self, path, body):
        request = urllib.request.Request(
            UPSTREAM + path,
            data=body,
            method=self.command,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Accept": self.headers.get("Accept", "application/json"),
            },
        )
        try:
            upstream = urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as err:
            upstream = err  # DeepSeek's own error response: pass it through
        except OSError as err:  # connection failure or timeout
            print(f"llm-proxy: upstream error: {err}", file=sys.stderr, flush=True)
            raise ClientError(502, "Upstream request failed.")

        with upstream:
            self.status = upstream.status
            self.send_response(upstream.status)
            self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
            if upstream.headers.get("Cache-Control"):
                self.send_header("Cache-Control", upstream.headers["Cache-Control"])
            self.send_header("Connection", "close")
            self.end_headers()
            # read1 returns whatever has arrived, so streamed replies pass
            # through as they come instead of waiting for a full buffer.
            read = getattr(upstream, "read1", upstream.read)
            try:
                while chunk := read(64 * 1024):
                    self.wfile.write(chunk)
            except TimeoutError:
                print("llm-proxy: upstream stalled mid-reply", file=sys.stderr, flush=True)

    def send_error_json(self, status, message):
        self.status = status
        data = json.dumps({"error": {"message": message, "type": "llm_proxy"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_request(self, code="-", size="-"):
        pass  # handle_request prints one line per request instead

    def log_message(self, format, *args):
        print(f"llm-proxy: {format % args}", file=sys.stderr, flush=True)


def main():
    if not KEY_CONFIGURED:
        print("llm-proxy: DEEPSEEK_API_KEY is not set; model calls will fail with 503 until it is.", file=sys.stderr, flush=True)
    ThreadingHTTPServer.request_queue_size = 64
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # As PID 1 the process would otherwise ignore SIGTERM and `docker stop`
    # would wait 10 seconds before killing it.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"llm-proxy listening on :{PORT}, forwarding to {UPSTREAM}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
