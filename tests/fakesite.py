"""A tiny fake streaming site used by the end-to-end test.

It reproduces the things that make real-world HLS downloads hard:

* three origins: a top page (``localhost``) that embeds the player in a
  cross-site iframe (``127.0.0.1``), and a CDN on another port;
* short-lived tokens in the query string (``?t=...&e=...``) that rotate every
  few seconds, handed out to the player by a ``/token`` endpoint;
* the CDN rejects requests without the browser's CORS headers
  (``Origin``, ``Referer``, ``Sec-Fetch-Mode: cors``);
* an optional per-request delay, so that tokens expire mid-download;
* media whose audio track continues for a few seconds of silence after the
  video ends.
"""

import hashlib
import hmac
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.parse

SECRET = b"fake-site-secret"

PLAYER_HTML = """<!doctype html><html><body>player<script>
const CDN = "%CDN%";
let tok = null;
async function token() {
  if (!tok || Date.now() > tok.e - 1500) tok = await (await fetch('/token')).json();
  return tok;
}
(async () => {
  let t = await token();
  const pl = await (await fetch(CDN + 'index-v1-a1.m3u8?t=' + t.t + '&e=' + t.e)).text();
  const segs = pl.split('\\n').filter(l => l && !l.startsWith('#')).map(l => l.split('?')[0]);
  let i = 0;
  while (true) {
    t = await token();
    try { await (await fetch(CDN + segs[i % segs.length] + '?t=' + t.t + '&e=' + t.e)).arrayBuffer(); } catch (e) {}
    i++;
    await new Promise(r => setTimeout(r, 700));
  }
})();
</script></body></html>"""

TOP_HTML = """<!doctype html><html><body>top page
<iframe src="%PLAYER%/" width="400" height="300"></iframe></body></html>"""


def make_media(out_dir, video_seconds=30, silent_tail=6):
    """Encode a small HLS VOD (2 s segments) whose audio outlasts the video."""
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(os.path.join(out_dir, "index.m3u8")):
        return out_dir
    total = video_seconds + silent_tail
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=%d" % video_seconds,
        "-f", "lavfi", "-i",
        "aevalsrc='if(lt(t,%d),0.3*sin(2*PI*440*t),0)':s=48000:d=%d" % (video_seconds, total),
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-keyint_min", "48",
        "-sc_threshold", "0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod", "-start_number", "1",
        "-hls_segment_filename", os.path.join(out_dir, "seg-%d-v1-a1.ts"),
        os.path.join(out_dir, "index.m3u8"),
    ]
    subprocess.run(cmd, check=True)
    return out_dir


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeSite:
    def __init__(self, media_dir, window=5.0, overlap=2.0, delay=0.0):
        self.media_dir = media_dir
        self.window = window
        self.overlap = overlap
        self.delay = delay
        self.log = []  # (who, path, status, reason)
        self.tokens_used = set()  # tokens of successful downloader requests
        self._lock = threading.Lock()
        self._servers = []
        self.cdn_port = self._serve(self._cdn_handler())
        self.player_port = self._serve(self._player_handler())
        self.top_port = self._serve(self._top_handler())
        self.cdn = "http://127.0.0.1:%d/hls/42/" % self.cdn_port
        self.player_origin = "http://127.0.0.1:%d" % self.player_port
        self.top_url = "http://localhost:%d/" % self.top_port

    # ---------- tokens ----------
    def _token_for(self, e_ms):
        return hmac.new(SECRET, str(e_ms).encode(), hashlib.sha256).hexdigest()[:16]

    def current_token(self):
        now = time.time()
        w = int(now // self.window)
        e_ms = int(((w + 1) * self.window + self.overlap) * 1000)
        return {"t": self._token_for(e_ms), "e": e_ms}

    def _token_ok(self, query):
        q = urllib.parse.parse_qs(query)
        try:
            t, e = q["t"][0], int(q["e"][0])
        except (KeyError, ValueError):
            return False
        return e > time.time() * 1000 and hmac.compare_digest(t, self._token_for(e))

    def _record(self, who, path, status, reason=""):
        with self._lock:
            self.log.append((who, path, status, reason))

    def count(self, who=None, status=None, reason=None):
        with self._lock:
            return sum(1 for w, _, s, r in self.log
                       if (who is None or w == who) and (status is None or s == status)
                       and (reason is None or r == reason))

    # ---------- servers ----------
    def _serve(self, handler):
        srv = _Server(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._servers.append(srv)
        return srv.server_address[1]

    def close(self):
        for s in self._servers:
            s.shutdown()
            s.server_close()

    def _cdn_handler(self):
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", ctype="text/plain"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", site.player_origin)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                # urllib (our downloader) sends "Accept-Encoding: identity", browsers don't
                who = "downloader" if self.headers.get("Accept-Encoding", "") == "identity" else "browser"
                h = {k.lower(): v for k, v in self.headers.items()}
                if (h.get("origin") != site.player_origin or h.get("sec-fetch-mode") != "cors"
                        or not h.get("referer", "").startswith(site.player_origin)):
                    site._record(who, url.path, 403, "headers")
                    return self._send(403, b"bad headers")
                if not site._token_ok(url.query):
                    site._record(who, url.path, 403, "token")
                    return self._send(403, b"bad token")
                name = url.path.rsplit("/", 1)[-1]
                if name == "index-v1-a1.m3u8":
                    lines = []
                    with open(os.path.join(site.media_dir, "index.m3u8")) as f:
                        for line in f.read().splitlines():
                            if line and not line.startswith("#"):
                                line = line + "?" + url.query
                            lines.append(line)
                    site._record(who, url.path, 200)
                    return self._send(200, ("\n".join(lines) + "\n").encode(),
                                      "application/vnd.apple.mpegurl")
                fp = os.path.join(site.media_dir, name)
                if not name.startswith("seg-") or not os.path.isfile(fp):
                    site._record(who, url.path, 404)
                    return self._send(404, b"not found")
                if site.delay and who == "downloader":
                    time.sleep(site.delay)
                with open(fp, "rb") as f:
                    body = f.read()
                site._record(who, url.path, 200)
                if who == "downloader":
                    with site._lock:
                        site.tokens_used.add(urllib.parse.parse_qs(url.query)["t"][0])
                self._send(200, body, "video/MP2T")

        return H

    def _player_handler(self):
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/token"):
                    body = json.dumps(site.current_token()).encode()
                    ctype = "application/json"
                else:
                    body = PLAYER_HTML.replace("%CDN%", site.cdn).encode()
                    ctype = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return H

    def _top_handler(self):
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = TOP_HTML.replace("%PLAYER%", site.player_origin).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return H


if __name__ == "__main__":
    import sys
    media = make_media(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake-media")
    site = FakeSite(media, delay=0.5)
    print("open:", site.top_url)
    threading.Event().wait()
