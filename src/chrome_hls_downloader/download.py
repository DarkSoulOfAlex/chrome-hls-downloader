"""Find the stream the browser is playing and download all of its segments.

Credentials (token in the query string, cookies, Origin/Referer/Sec-Fetch-* headers) are
always taken from the most recent segment request the browser itself made for this
stream. When the server answers 401/403/404/410 the download pauses until the player
requests a segment with a fresh credential, then resumes with it.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import ts as tsmod
from .hls import Playlist, parse_playlist, path_of, replace_origin_and_query

AUTH_CODES = {401, 403, 404, 410}
RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}


class DownloadError(RuntimeError):
    pass


def log(msg=""):
    print(msg, flush=True)


def _opener_for(url):
    host = urlsplit(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def http_get(url, headers, timeout=60):
    """Return (status, body bytes or None)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener_for(url).open(req, timeout=timeout) as r:
            expected = r.headers.get("Content-Length")
            body = r.read()
            if expected is not None and len(body) != int(expected):
                return -1, None
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return -1, None


@dataclass
class Credential:
    url: str          # a URL the browser requested for this stream (carries host + query)
    headers: dict
    seq: int


# ---------------------------------------------------------------- stream discovery

def find_stream(browser, settle=6.0, timeout=None, status=log):
    """Wait until the browser loads a media playlist; return (Playlist, Request)."""
    status("Waiting for a video to play in the browser window (open the page and press play)...")
    deadline = None if timeout is None else time.time() + timeout
    hinted = False
    while True:
        first = browser.wait_for(lambda r: r.is_playlist and r.body, timeout=5)
        if first is not None:
            break
        if not hinted and browser.latest(lambda r: r.is_segment_like and "/" in r.path):
            status("Video data is flowing but its playlist was loaded before we started watching: "
                   "reload the page (F5) and press play again.")
            hinted = True
        if deadline is not None and time.time() > deadline:
            raise DownloadError("no HLS playlist seen in the browser")
    time.sleep(settle)  # give the player time to also load the variant playlist
    best = None
    masters = []
    for r in browser.requests():
        if not (r.is_playlist and r.body):
            continue
        try:
            pl = parse_playlist(r.body, r.url)
        except ValueError:
            continue
        if pl.is_media:
            key = (pl.total_duration, r.seq)
            if best is None or key > best[0]:
                best = (key, pl, r)
        elif pl.is_master:
            masters.append((pl, r))
    if best:
        return best[1], best[2]
    # only a master playlist: fetch the best variant ourselves with the browser's headers
    pl, r = masters[-1]
    variant = max(pl.variants, key=lambda v: v.bandwidth)
    code, body = http_get(variant.uri, r.replay_headers)
    if code != 200 or not body:
        raise DownloadError("could not load the variant playlist (HTTP %s)" % code)
    return parse_playlist(body.decode("utf-8", "replace"), variant.uri), r


def check_supported(pl):
    if pl.encryption:
        raise DownloadError("this stream is encrypted (EXT-X-KEY METHOD=%s): not supported" % pl.encryption)
    if pl.byterange:
        raise DownloadError("byte-range playlists (EXT-X-BYTERANGE) are not supported")
    if not pl.segments:
        raise DownloadError("the playlist has no segments")
    if not pl.endlist:
        log("Warning: the playlist has no EXT-X-ENDLIST (live stream?): only the segments listed now "
            "will be downloaded.")


# ---------------------------------------------------------------- downloader

class Downloader:
    def __init__(self, browser, playlist, playlist_request, workdir, parallel=6,
                 token_wait=600, status=log):
        self.browser = browser
        self.pl = playlist
        self.workdir = workdir
        self.parallel = max(1, parallel)
        self.token_wait = token_wait
        self.status = status
        self.paths = {path_of(s.uri) for s in playlist.segments}
        if playlist.init_uri:
            self.paths.add(path_of(playlist.init_uri))
        self.ext = os.path.splitext(path_of(playlist.segments[0].uri))[1].lower() or ".bin"
        self.is_ts = self.ext == ".ts"
        # starting credential: URIs as written in the playlist + headers of the playlist request
        self.cred = Credential(url=playlist.segments[0].uri,
                               headers=playlist_request.replay_headers,
                               seq=playlist_request.seq)
        self.transplant = False   # becomes True once we use a query seen in the browser
        if playlist_request.path in self.paths:   # resuming: we start from a segment request
            self._adopt(Credential(url=playlist_request.url, headers=playlist_request.replay_headers,
                                   seq=playlist_request.seq))
        self._stop = threading.Event()
        self._bytes = 0
        self._ok = 0
        self._lock = threading.Lock()
        os.makedirs(workdir, exist_ok=True)

    # ----- files
    def seg_path(self, index):
        return os.path.join(self.workdir, "seg%05d%s" % (index, self.ext))

    def init_path(self):
        return os.path.join(self.workdir, "init" + os.path.splitext(path_of(self.pl.init_uri))[1])

    def save_state(self):
        state = {"playlist_url": self.pl.url, "segments": [s.uri for s in self.pl.segments],
                 "durations": [s.duration for s in self.pl.segments], "init": self.pl.init_uri}
        with open(os.path.join(self.workdir, "state.json"), "w") as f:
            json.dump(state, f)

    def _complete(self, path):
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        return tsmod.is_complete_ts_file(path) if self.is_ts else True

    def missing(self):
        return [s for s in self.pl.segments if not self._complete(self.seg_path(s.index))]

    # ----- credentials
    def newer_credential(self):
        r = self.browser.latest(lambda r: r.seq > self.cred.seq and r.path in self.paths
                                and not (r.status >= 400))
        if r is None:
            return None
        return Credential(url=r.url, headers=r.replay_headers, seq=r.seq)

    def _url_for(self, uri, cred):
        return replace_origin_and_query(uri, cred.url) if self.transplant else uri

    def _adopt(self, cred):
        self.cred = cred
        self.transplant = True

    # ----- one segment
    def _fetch(self, seg_uri, dest):
        if self._stop.is_set():
            return "skipped"
        cred = self.cred
        code, body = http_get(self._url_for(seg_uri, cred), cred.headers)
        if code in AUTH_CODES:
            self._stop.set()
            return "auth"
        if code != 200 or body is None:
            return "retry" if (code in RETRY_CODES or code == -1) else "fatal:%s" % code
        if self.is_ts and not tsmod.is_complete_ts(body):
            return "retry"
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, dest)
        with self._lock:
            self._bytes += len(body)
            self._ok += 1
        return "ok"

    # ----- main loop
    def run(self):
        self.save_state()
        total = len(self.pl.segments)
        if self.pl.init_uri and not os.path.isfile(self.init_path()):
            self._get_init()
        stalls = 0
        start = time.time()
        bytes0 = self._bytes
        while True:
            todo = self.missing()
            if not todo:
                break
            newer = self.newer_credential()
            if newer:
                self._adopt(newer)
            self._stop.clear()
            done_before = total - len(todo)
            results = self._round(todo, total, start, bytes0)
            done_now = total - len(self.missing())
            fatal = [r for r in results if r.startswith("fatal")]
            if fatal and done_now == done_before:
                stalls += 1
            if "auth" in results:
                if done_now == done_before:
                    stalls += 1
                    # the query taken from the browser did not work for other segments
                    # (per-segment signatures?): reload the playlist with the fresh credential
                    if self.transplant and self._refresh_playlist():
                        continue
                self._wait_for_credential()
            elif "retry" in results:
                time.sleep(min(30, 2 ** min(stalls + 1, 5)))
                if done_now == done_before:
                    stalls += 1
            if done_now > done_before:
                stalls = 0
            if stalls >= 6:
                raise DownloadError("no progress after several attempts (last results: %s)"
                                    % ", ".join(sorted(set(results))))
        self._progress(total, total, start, bytes0, final=True)

    def _round(self, todo, total, start, bytes0):
        results = []
        with ThreadPoolExecutor(self.parallel) as ex:
            futs = {ex.submit(self._fetch, s.uri, self.seg_path(s.index)) for s in todo}
            last = 0
            while futs:
                try:
                    finished, futs = wait(futs, timeout=1.0, return_when=FIRST_COMPLETED)
                except KeyboardInterrupt:
                    self._stop.set()   # queued segments are skipped, running ones finish
                    raise
                for f in finished:
                    results.append(f.result())
                if not self._stop.is_set():
                    newer = self.newer_credential()     # switch early when the player rotates tokens
                    if newer:
                        self._adopt(newer)
                if time.time() - last >= 1:
                    self._progress(total - len(todo) + results.count("ok"), total, start, bytes0)
                    last = time.time()
        sys.stdout.write("\n")
        return results

    def _wait_for_credential(self):
        self.status("Token expired: waiting for the player to request a new segment "
                    "(keep the video playing)...")
        deadline = time.time() + self.token_wait
        while time.time() < deadline:
            newer = self.newer_credential()
            if newer:
                self._adopt(newer)
                return
            time.sleep(1)
        raise DownloadError("no fresh token from the browser within %d s: is the video still playing?"
                            % self.token_wait)

    def _refresh_playlist(self):
        cred = self.cred
        r = self.browser.latest(lambda r: r.is_playlist and r.path == path_of(self.pl.url)
                                and r.seq > cred.seq - 1 and not (r.status >= 400))
        url = r.url if r is not None else replace_origin_and_query(self.pl.url, cred.url)
        code, body = http_get(url, cred.headers)
        if code != 200 or not body:
            return False
        try:
            fresh = parse_playlist(body.decode("utf-8", "replace"), url)
        except ValueError:
            return False
        if len(fresh.segments) != len(self.pl.segments):
            return False
        for old, new in zip(self.pl.segments, fresh.segments):
            old.uri = new.uri
        if fresh.init_uri:
            self.pl.init_uri = fresh.init_uri
        self.transplant = False
        self.status("Reloaded the playlist with a fresh token.")
        return True

    def _get_init(self):
        while True:
            cred = self.cred
            code, body = http_get(self._url_for(self.pl.init_uri, cred), cred.headers)
            if code == 200 and body:
                with open(self.init_path(), "wb") as f:
                    f.write(body)
                return
            if code in AUTH_CODES:
                self._wait_for_credential()
            else:
                raise DownloadError("could not download the init segment (HTTP %s)" % code)

    def _progress(self, done, total, start, bytes0, final=False):
        elapsed = max(0.001, time.time() - start)
        rate = (self._bytes - bytes0) / elapsed
        eta = ""
        if self._ok and done < total:
            eta = "  ETA %s" % _hms(elapsed / self._ok * (total - done))
        sys.stdout.write("\r  %d/%d segments  %.1f MB/s%s      " % (done, total, rate / 1e6, eta))
        if final:
            sys.stdout.write("\n")
        sys.stdout.flush()

    # ----- merge
    def merge(self, output, ffmpeg=None):
        """Join the segments and remux to the container of ``output`` (mp4 by default)."""
        ffmpeg = ffmpeg or shutil.which("ffmpeg")
        joined = os.path.join(self.workdir, "joined" + (self.ext if self.is_ts else ".mp4"))
        with open(joined, "wb") as out:
            if self.pl.init_uri:
                with open(self.init_path(), "rb") as f:
                    shutil.copyfileobj(f, out)
            for s in self.pl.segments:
                with open(self.seg_path(s.index), "rb") as f:
                    shutil.copyfileobj(f, out)
        if not ffmpeg:
            fallback = os.path.splitext(output)[0] + os.path.splitext(joined)[1]
            os.replace(joined, fallback)
            log("ffmpeg not found: saved %s without remuxing" % fallback)
            return fallback
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", joined,
               "-map", "0:v?", "-map", "0:a?", "-c", "copy", output]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if p.returncode != 0:
            raise DownloadError("ffmpeg could not remux the segments:\n" + p.stderr[-2000:])
        os.remove(joined)
        return output


def load_state(workdir):
    path = os.path.join(workdir, "state.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        st = json.load(f)
    from .hls import Segment
    pl = Playlist(url=st["playlist_url"], endlist=True, init_uri=st.get("init"))
    pl.segments = [Segment(index=i + 1, uri=u, duration=d)
                   for i, (u, d) in enumerate(zip(st["segments"], st["durations"]))]
    return pl


def _hms(secs):
    secs = int(max(0, secs))
    return "%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)
