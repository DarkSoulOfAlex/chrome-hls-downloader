"""Launch a Chromium-based browser with remote debugging and watch its network traffic.

Every page, iframe (including cross-site ones running in another process) and worker
is attached through the Chrome DevTools Protocol, so we see each request exactly as
the browser sends it: URL with its current token, cookies, ``Origin``, ``Referer``,
``Sec-Fetch-*`` ... Playlist bodies are read with ``Network.getResponseBody``.
"""

import base64
import collections
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlsplit

import websocket

SEGMENT_EXTENSIONS = (".ts", ".m4s", ".m4v", ".m4a", ".aac", ".mp4", ".cmfv", ".cmfa")

# headers that must not be replayed (pseudo-headers, hop-by-hop, conditional/range, compression)
_DROP = {"host", "connection", "content-length", "accept-encoding", "range", "if-range",
         "if-none-match", "if-modified-since", "upgrade-insecure-requests", "priority"}

_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CdpError(RuntimeError):
    pass


@dataclass
class Request:
    seq: int
    url: str
    session: str
    request_id: str
    basic_headers: Dict[str, str] = field(default_factory=dict)
    extra_headers: Dict[str, str] = field(default_factory=dict)
    mime: str = ""
    status: int = 0
    body: Optional[str] = None

    @property
    def headers(self):
        """Headers as sent on the wire (extra info has cookies and sec-* headers)."""
        h = {k.lower(): v for k, v in self.basic_headers.items()}
        h.update({k.lower(): v for k, v in self.extra_headers.items()})
        return h

    @property
    def replay_headers(self):
        return {k: v for k, v in self.headers.items() if not k.startswith(":") and k not in _DROP}

    @property
    def path(self):
        return urlsplit(self.url).path

    @property
    def is_playlist(self):
        return self.path.lower().endswith(".m3u8") or "mpegurl" in self.mime.lower()

    @property
    def is_segment_like(self):
        return self.path.lower().endswith(SEGMENT_EXTENSIONS) or self.mime.lower().startswith(("video/", "audio/"))


def find_browser(explicit=None):
    """Path of a Chromium-based browser: explicit, $CHROME_PATH, or a well-known location."""
    for c in (explicit, os.environ.get("CHROME_PATH")):
        if c:
            found = c if os.path.isfile(c) else shutil.which(c)
            if found:
                return found
    cands = []
    if sys.platform.startswith("win"):
        roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rels = [r"Google\Chrome\Application\chrome.exe",
                r"Microsoft\Edge\Application\msedge.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
                r"Chromium\Application\chrome.exe",
                r"Vivaldi\Application\vivaldi.exe"]
        cands = [os.path.join(r, rel) for rel in rels for r in roots if r]
    elif sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium",
                 "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "brave-browser", "vivaldi"):
            p = shutil.which(name)
            if p:
                cands.append(p)
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def default_profile_dir():
    base = os.environ.get("LOCALAPPDATA") if sys.platform.startswith("win") else None
    base = base or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "chrome-hls-downloader", "profile")


class Browser:
    """A browser instance (launched by us or already running) on a debugging port."""

    def __init__(self, port=9222, browser_path=None, profile_dir=None, headless=False,
                 start_url=None, max_requests=5000):
        self.port = port
        self.browser_path = browser_path
        self.profile_dir = profile_dir or default_profile_dir()
        self.headless = headless
        self.start_url = start_url
        self.process = None
        self.ws = None
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._requests = {}                    # (session, requestId) -> Request
        self._recent = collections.deque(maxlen=max_requests)
        self._running = False
        self.launched = False

    # ---------- lifecycle ----------
    def _version(self):
        with _LOCAL.open("http://127.0.0.1:%d/json/version" % self.port, timeout=2) as r:
            return json.load(r)

    def alive(self):
        try:
            self._version()
            return True
        except Exception:
            return False

    def launch(self):
        path = find_browser(self.browser_path)
        if not path:
            raise CdpError("no Chromium-based browser found (Chrome, Edge, Brave, Chromium, Vivaldi); "
                           "pass --browser PATH")
        os.makedirs(self.profile_dir, exist_ok=True)
        args = [path, "--remote-debugging-port=%d" % self.port,
                "--user-data-dir=%s" % self.profile_dir,
                "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required"]
        if self.headless:
            args.append("--headless=new")
        if os.environ.get("CHROME_HLS_NO_SANDBOX") or (
                sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0):
            args.append("--no-sandbox")   # root / CI containers
        args.append("about:blank")   # the start page is opened once we are watching
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.launched = True
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.alive():
                return path
            if self.process.poll() is not None:
                break
            time.sleep(0.3)
        raise CdpError("the browser did not open its debugging port %d. If it was already running "
                       "with the same profile, close it and retry." % self.port)

    def start(self):
        if not self.alive():
            self.launch()
        info = self._version()
        self.ws = websocket.create_connection(info["webSocketDebuggerUrl"], suppress_origin=True,
                                              enable_multithread=True)
        self._running = True
        threading.Thread(target=self._reader, daemon=True).start()
        # attach to every page (existing and future); new ones start paused until the
        # network monitoring is enabled, so not even their first request is missed
        self.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True,
                                           "flatten": True})
        if self.start_url:
            self.post("Target.createTarget", {"url": self.start_url})
        return info.get("Browser", "browser")

    def close(self, kill=None):
        self._running = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        if kill is None:
            kill = self.launched and self.headless
        if kill and self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except Exception:
                self.process.kill()

    # ---------- protocol ----------
    def _send(self, msg):
        with self._send_lock:
            self.ws.send(json.dumps(msg))

    def post(self, method, params=None, session=None, callback=None):
        mid = next(self._ids)
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        if callback:
            with self._lock:
                self._pending[mid] = callback
        self._send(msg)
        return mid

    def call(self, method, params=None, session=None, timeout=15):
        done = threading.Event()
        box = {}

        def cb(msg):
            box["msg"] = msg
            done.set()

        self.post(method, params, session, cb)
        if not done.wait(timeout):
            raise CdpError("timeout waiting for %s" % method)
        msg = box["msg"]
        if "error" in msg:
            raise CdpError("%s: %s" % (method, msg["error"].get("message")))
        return msg.get("result", {})

    def _reader(self):
        while self._running:
            try:
                raw = self.ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" in msg:
                with self._lock:
                    cb = self._pending.pop(msg["id"], None)
                if cb:
                    try:
                        cb(msg)
                    except Exception:
                        pass
            else:
                try:
                    self._on_event(msg.get("method", ""), msg.get("params", {}), msg.get("sessionId"))
                except Exception:
                    pass
        self._running = False

    # ---------- events ----------
    def _on_event(self, method, p, session):
        if method == "Target.attachedToTarget":
            sid = p["sessionId"]
            self.post("Network.enable", {}, sid)
            if p.get("targetInfo", {}).get("type") in ("page", "iframe"):
                # iframes (also cross-site ones in other processes) and workers of this page
                self.post("Target.setAutoAttach",
                          {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, sid)
            if p.get("waitingForDebugger"):
                self.post("Runtime.runIfWaitingForDebugger", {}, sid)
        elif method == "Network.requestWillBeSent":
            req = self._get(session, p["requestId"], p["request"]["url"])
            req.url = p["request"]["url"]          # also follows redirects
            req.basic_headers = p["request"].get("headers", {})
        elif method == "Network.requestWillBeSentExtraInfo":
            req = self._get(session, p["requestId"])
            req.extra_headers = p.get("headers", {})
        elif method == "Network.responseReceived":
            req = self._get(session, p["requestId"], p["response"]["url"])
            req.mime = p["response"].get("mimeType", "")
            req.status = p["response"].get("status", 0)
        elif method == "Network.loadingFinished":
            with self._lock:
                req = self._requests.get((session, p["requestId"]))
            if req and req.is_playlist and 200 <= req.status < 300:
                self.post("Network.getResponseBody", {"requestId": p["requestId"]}, session,
                          callback=lambda m, r=req: self._store_body(r, m))

    def _get(self, session, request_id, url=""):
        key = (session, request_id)
        with self._lock:
            req = self._requests.get(key)
            if req is None:
                req = Request(seq=next(self._seq), url=url, session=session, request_id=request_id)
                self._requests[key] = req
                self._recent.append(req)
                if len(self._requests) > self._recent.maxlen:
                    alive = {(r.session, r.request_id) for r in self._recent}
                    self._requests = {k: v for k, v in self._requests.items() if k in alive}
            return req

    def _store_body(self, req, msg):
        res = msg.get("result")
        if not res:
            return
        body = res.get("body", "")
        if res.get("base64Encoded"):
            body = base64.b64decode(body).decode("utf-8", "replace")
        req.body = body

    # ---------- queries ----------
    def requests(self):
        with self._lock:
            return list(self._recent)

    def latest(self, predicate):
        best = None
        for r in self.requests():
            if r.url and predicate(r) and (best is None or r.seq > best.seq):
                best = r
        return best

    def wait_for(self, predicate, timeout=None, poll=0.5):
        deadline = None if timeout is None else time.time() + timeout
        while deadline is None or time.time() < deadline:
            r = self.latest(predicate)
            if r is not None:
                return r
            if not self._running:
                raise CdpError("lost the connection to the browser (was it closed?)")
            time.sleep(poll)
        return None
