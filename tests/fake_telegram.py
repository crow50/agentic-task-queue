"""Local stand-in for the Telegram Bot API (stdlib only).

    telegram = FakeTelegram()
    telegram.start()            # serves on 127.0.0.1:<free port>
    telegram.url                # point TELEGRAM_API_BASE here
    telegram.calls("sendMessage")
    telegram.stop()

Every request is recorded. By default each method answers {"ok": true, ...};
queue_response() makes the next call to a method fail or return something else,
e.g. a 400 to exercise the HTML-to-plain-text fallback.
"""

import json
import re
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Request:
    method: str  # Telegram method, e.g. "sendMessage"
    token: str
    content_type: str
    params: dict  # decoded form / JSON / query parameters
    body: bytes  # raw body, for multipart uploads


class FakeTelegram:
    def __init__(self):
        self.requests = []
        self.updates = []  # served (once) by getUpdates
        self._queued = {}
        self._next_message_id = 100
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self.url

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None

    def queue_response(self, method, status=200, body=None):
        """Answer the next call to `method` with this status and JSON body."""
        if body is None and status != 200:
            body = {"ok": False, "error_code": status,
                    "description": "Bad Request: can't parse entities" if status == 400
                    else f"error {status}"}
        with self._lock:
            self._queued.setdefault(method, []).append((status, body))

    def calls(self, method=None):
        return [r for r in self.requests if method is None or r.method == method]

    def texts(self):
        return [r.params.get("text") for r in self.calls("sendMessage")]

    def respond(self, request):
        with self._lock:
            queued = self._queued.get(request.method)
            if queued:
                return queued.pop(0)
            if request.method == "getUpdates":
                updates, self.updates = self.updates, []
                return 200, {"ok": True, "result": updates}
            if request.method == "getMe":
                return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "fake_bot"}}
            if request.method in ("sendMessage", "sendDocument", "editMessageText"):
                self._next_message_id += 1
                return 200, {"ok": True, "result": {"message_id": self._next_message_id}}
            return 200, {"ok": True, "result": True}


class _Handler(BaseHTTPRequestHandler):
    def handle_request(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = urllib.parse.urlsplit(self.path)
        match = re.fullmatch(r"/bot([^/]+)/(\w+)", url.path)
        if not match:
            return self.reply(404, {"ok": False, "error_code": 404, "description": "Not Found"})

        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("application/x-www-form-urlencoded"):
            params = dict(urllib.parse.parse_qsl(body.decode(), keep_blank_values=True))
        elif content_type.startswith("application/json"):
            params = json.loads(body or b"{}")
        else:
            params = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))

        fake = self.server.fake
        request = Request(match.group(2), match.group(1), content_type, params, body)
        fake.requests.append(request)
        self.reply(*fake.respond(request))

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = handle_request

    def log_message(self, *args):
        pass
