"""Command line interface.

    chrome-hls-dl [-o video.mp4] [options]     download the video playing in the browser
    chrome-hls-dl verify FILE [--no-trim]      verify an existing file
"""

import argparse
import os
import shutil
import sys

from . import __version__
from .cdp import Browser, CdpError, default_profile_dir
from .download import DownloadError, Downloader, check_supported, find_stream, load_state, log
from .hls import path_of
from .verify import verify


def _download_parser():
    p = argparse.ArgumentParser(
        prog="chrome-hls-dl",
        description="Download the HLS video that is playing in a Chromium-based browser "
                    "(Chrome, Edge, Brave, Chromium, Vivaldi), using the browser's own tokens "
                    "and headers. Run 'chrome-hls-dl verify FILE' to verify a file.")
    p.add_argument("-o", "--output", default="video.mp4", help="output file (default: video.mp4)")
    p.add_argument("--workdir", help="folder for the segments (default: <output>.parts)")
    p.add_argument("-j", "--parallel", type=int, default=6, help="parallel downloads (default: 6)")
    p.add_argument("--browser", help="path or name of the browser executable")
    p.add_argument("--port", type=int, default=9222, help="remote debugging port (default: 9222)")
    p.add_argument("--profile", default=None,
                   help="browser profile folder (default: %s)" % default_profile_dir())
    p.add_argument("--url", help="page to open in the browser")
    p.add_argument("--headless", action="store_true", help="run the browser without a window")
    p.add_argument("--wait", type=float, default=None,
                   help="give up if no video starts within this many seconds (default: wait forever)")
    p.add_argument("--token-wait", type=float, default=600,
                   help="seconds to wait for a fresh token from the player (default: 600)")
    p.add_argument("--no-verify", action="store_true", help="skip the verification")
    p.add_argument("--no-trim", action="store_true", help="do not write a copy without a silent audio tail")
    p.add_argument("--keep-segments", action="store_true", help="keep the segments folder")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return p


def _verify_parser():
    p = argparse.ArgumentParser(prog="chrome-hls-dl verify",
                                description="Decode every frame of a video and check its integrity.")
    p.add_argument("file")
    p.add_argument("--no-trim", action="store_true", help="do not write a copy without a silent audio tail")
    return p


def _print_result(rep):
    if rep.ok:
        log("ALL OK: every frame decodes cleanly, no holes, audio and video in sync.")
        if rep.trimmed:
            log("The file to keep is %s (same video without the silent tail)." % rep.trimmed)
    else:
        log("Verification FAILED: %d check(s) did not pass." % sum(1 for ok, _ in rep.checks if not ok))
        for line in rep.reports[:20]:
            log("   " + line)


def cmd_verify(argv):
    a = _verify_parser().parse_args(argv)
    if not os.path.isfile(a.file):
        log("file not found: %s" % a.file)
        return 2
    rep = verify(a.file, trim_silent_tail=not a.no_trim)
    _print_result(rep)
    return 0 if rep.ok else 1


def cmd_download(argv):
    a = _download_parser().parse_args(argv)
    output = os.path.abspath(a.output)
    workdir = os.path.abspath(a.workdir or output + ".parts")
    browser = Browser(port=a.port, browser_path=a.browser, profile_dir=a.profile,
                      headless=a.headless, start_url=a.url)
    try:
        name = browser.start()
        log("Connected to %s on port %d%s." % (name, a.port, " (separate profile)" if browser.launched else ""))
        resumed = load_state(workdir)
        if resumed:
            pl = resumed
            log("Resuming %s: play the same video in the browser so it provides fresh tokens." % workdir)
            paths = {path_of(seg.uri) for seg in pl.segments}
            req = browser.wait_for(lambda r: r.path in paths and not r.status >= 400, timeout=a.wait)
            if req is None:
                raise DownloadError("no request for this video seen in the browser")
        else:
            pl, req = find_stream(browser, timeout=a.wait)
        check_supported(pl)
        log("Stream found: %d segments, %s, %s" % (len(pl.segments), _hms(pl.total_duration), pl.url.split("?")[0]))
        dl = Downloader(browser, pl, req, workdir, parallel=a.parallel, token_wait=a.token_wait)
        done = len(pl.segments) - len(dl.missing())
        if done:
            log("%d segments already downloaded." % done)
        log("Downloading (keep the video playing in the browser so tokens stay fresh)...")
        dl.run()
        out = dl.merge(output)
        log("Saved %s" % out)
    except KeyboardInterrupt:
        log("\nInterrupted: segments are kept in %s; run the same command again to resume." % workdir)
        return 130
    except (DownloadError, CdpError) as e:
        log("Error: %s" % e)
        return 1
    finally:
        browser.close()
    ok = True
    if not a.no_verify:
        rep = verify(out, trim_silent_tail=not a.no_trim)
        _print_result(rep)
        ok = rep.ok
    if ok and not a.keep_segments:
        shutil.rmtree(workdir, ignore_errors=True)
    elif not ok:
        log("Segments kept in %s." % workdir)
    if browser.launched and not a.headless:
        log("You can close the browser window.")
    return 0 if ok else 1


def _hms(secs):
    secs = int(max(0, secs))
    return "%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "verify":
        return cmd_verify(argv[1:])
    return cmd_download(argv)
