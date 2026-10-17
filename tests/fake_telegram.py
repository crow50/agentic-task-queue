"""Local stand-in for the Telegram Bot API (stdlib only).

    telegram = FakeTelegram()
    telegram.start()            # serves on 127.0.0.1:<free port>
    telegram.url                # point TELEGRAM_API_BASE here
    telegram.calls("sendMessage")
    telegram.stop()

Every request is recorded. By default each method answers {"ok": true, ...};
queue_response() makes the next call to a method fail or return something else,
e.g. a 400 to exercise the HTML-to-plain-text fallback.

With telegram.strict = True it also refuses what the real Bot API refuses, with the real
wording: text that is empty or longer than 4096 characters (counted as Python characters; whether Telegram counts UTF-16 units is unverified,
after HTML parsing), and HTML with an unsupported or unbalanced tag. Rejected requests are
recorded in telegram.rejected as (method, description).
"""

import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEXT_LIMIT = 4096
HTML_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote",
             "tg-spoiler", "tg-emoji", "span"}


class _HtmlCheck(HTMLParser):
    """Telegram's HTML subset: allowed tags only, each closed in order. .text is what the reader sees."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.error = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag not in HTML_TAGS:
            self.error = self.error or f"Unsupported start tag \"{tag}\""
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.error = self.error or f"Unmatched end tag \"{tag}\""
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.parts.append(data)

    @property
    def text(self):
        return "".join(self.parts)


def strict_error(request):
    """Why the real Bot API would answer 400 to this request, or None."""
    if request.method not in ("sendMessage", "editMessageText"):
        return None
    text = request.params.get("text", "")
    if request.params.get("parse_mode") == "HTML":
        check = _HtmlCheck()
        check.feed(text)
        check.close()
        if check.error or check.stack:
            return "can't parse entities: " + (check.error or f"Can't find end tag for \"{check.stack[-1]}\"")
        text = check.text
    if not text.strip():
        return "message text is empty"
    if len(text) > TEXT_LIMIT:
        return "message is too long"
    return None


@dataclass
class Request:
    method: str  # Telegram method, e.g. "sendMessage"
    token: str
    content_type: str
    params: dict  # decoded form / JSON / query parameters
    body: bytes  # raw body, for multipart uploads
    at: float = field(default_factory=time.monotonic)  # when it arrived, for rate-limit assertions
    accepted: bool = True  # False when strict mode refused it (or a queued response failed it)


class FakeTelegram:
    def __init__(self):
        self.requests = []
        self.rejected = []  # (method, description) refused by strict mode
        self.strict = False
        self.updates = []  # served (once) by getUpdates
        self.files = {}  # file_id -> bytes served by getFile and the /file/ download; unknown ids get b"OggS-fake"
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

    def voices(self):
        """The sendVoice requests, in order."""
        return self.calls("sendVoice")

    def edits(self):
        """The editMessageText requests, in order."""
        return self.calls("editMessageText")

    def texts(self):
        return [r.params.get("text") for r in self.calls("sendMessage")]

    def answers(self):
        """The answerCallbackQuery requests, in order."""
        return self.calls("answerCallbackQuery")

    def keyboards(self):
        """The inline keyboards sent with sendMessage or editMessageText, as lists of rows of (label, callback_data)."""
        found = []
        for request in self.calls("sendMessage") + self.calls("editMessageText"):
            markup = request.params.get("reply_markup")
            if markup:
                rows = json.loads(markup)["inline_keyboard"]
                found.append([[(b["text"], b["callback_data"]) for b in row] for row in rows])
        return found

    @staticmethod
    def callback_update(data, sender=4242, chat=4242, message_id=100, update_id=900, query_id="cb1"):
        """What Telegram sends when a button is tapped."""
        return {"update_id": update_id, "callback_query": {
            "id": query_id, "from": {"id": sender}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}}}}

    def tap(self, data, **kwargs):
        """Queue a button tap for the next getUpdates."""
        self.updates.append(self.callback_update(data, **kwargs))

    def respond(self, request):
        with self._lock:
            if self.strict and (problem := strict_error(request)):
                request.accepted = False
                self.rejected.append((request.method, problem))
                return 400, {"ok": False, "error_code": 400, "description": f"Bad Request: {problem}"}
            queued = self._queued.get(request.method)
            if queued:
                return queued.pop(0)
            if request.method == "getUpdates":
                updates, self.updates = self.updates, []
                return 200, {"ok": True, "result": updates}
            if request.method == "getMe":
                return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "fake_bot"}}
            if request.method == "getFile":
                file_id = request.params.get("file_id", "")
                return 200, {"ok": True, "result": {"file_id": file_id, "file_path": f"voice/{file_id}.oga"}}
            if request.method in ("sendMessage", "sendDocument", "sendVoice", "editMessageText"):
                self._next_message_id += 1
                return 200, {"ok": True, "result": {"message_id": self._next_message_id}}
            return 200, {"ok": True, "result": True}


class _Handler(BaseHTTPRequestHandler):
    def handle_request(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = urllib.parse.urlsplit(self.path)
        download = re.fullmatch(r"/file/bot([^/]+)/voice/(.+)\.oga", url.path)
        if download:  # a file download, as the Bot API serves it after getFile
            data = self.server.fake.files.get(urllib.parse.unquote(download.group(2)), b"OggS-fake")
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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
