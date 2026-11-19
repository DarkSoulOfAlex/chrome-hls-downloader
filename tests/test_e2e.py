"""End-to-end: real headless browser + fake streaming site with rotating tokens.

The CDN only accepts requests carrying the browser's CORS headers and a token that is
valid for a few seconds; the server slows our downloader down so that tokens expire
mid-download. The download must still complete, the verification must pass, and the
silent audio tail must be detected and trimmed.
"""

import os
import shutil

import pytest

from chrome_hls_downloader.cdp import find_browser
from chrome_hls_downloader.cli import main

import fakesite

pytestmark = pytest.mark.e2e

needs = pytest.mark.skipif(not (find_browser() and shutil.which("ffmpeg") and shutil.which("ffprobe")),
                           reason="needs a Chromium-based browser and ffmpeg")


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return fakesite.make_media(str(tmp_path_factory.mktemp("media")))


def _run(site, tmp_path, port):
    out = tmp_path / "video.mp4"
    code = main(["-o", str(out), "--url", site.top_url, "--headless", "--port", str(port),
                 "--profile", str(tmp_path / "profile"), "-j", "2", "--wait", "60",
                 "--token-wait", "60"])
    return code, out


def _check_output(site, tmp_path, code, out):
    assert code == 0
    assert out.is_file()
    assert (tmp_path / "video.trimmed.mp4").is_file(), "the silent audio tail should have been trimmed"
    assert site.count("downloader", 403, "headers") == 0, "the browser's headers must be replayed"
    assert site.count("downloader", 200) >= 15
    assert not os.path.exists(str(out) + ".parts"), "segments folder should be removed after success"


@needs
def test_switches_to_fresh_tokens_early(media, tmp_path):
    """Tokens overlap: the downloader should follow the player's rotation."""
    site = fakesite.FakeSite(media, window=4.0, overlap=1.5, delay=0.6)
    try:
        code, out = _run(site, tmp_path, 9339)
        _check_output(site, tmp_path, code, out)
        assert len(site.tokens_used) >= 2, "the download should span several token rotations"
    finally:
        site.close()


@needs
def test_recovers_after_expired_token(media, tmp_path):
    """No overlap: requests in flight hit expired tokens and must be retried with fresh ones."""
    site = fakesite.FakeSite(media, window=4.0, overlap=0.0, delay=0.9)
    try:
        code, out = _run(site, tmp_path, 9340)
        _check_output(site, tmp_path, code, out)
        assert site.count("downloader", 403, "token") >= 1
        assert len(site.tokens_used) >= 2
    finally:
        site.close()


@needs
def test_verify_detects_corruption(media, tmp_path):
    """Corrupt a few packets in a real segment: the verification must fail."""
    import subprocess
    from chrome_hls_downloader import ts
    from chrome_hls_downloader.verify import verify

    joined = tmp_path / "joined.ts"
    with open(joined, "wb") as out:
        for i in range(1, 16):
            with open(os.path.join(media, "seg-%d-v1-a1.ts" % i), "rb") as f:
                data = f.read()
            if i == 8:
                data, n = ts.corrupt_sample(data, packets=30, every=3, start_fraction=0.2, seed=3)
                assert n > 0
            out.write(data)
    mp4 = tmp_path / "damaged.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(joined), "-map", "0:v",
                    "-map", "0:a", "-c", "copy", str(mp4)], check=True)
    rep = verify(str(mp4), trim_silent_tail=False, progress=False)
    assert rep.self_test_ok
    assert not rep.ok
    assert rep.reports, "ffmpeg should have reported the damaged frames"
