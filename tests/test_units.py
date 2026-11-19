import os

from chrome_hls_downloader import ts
from chrome_hls_downloader.cdp import Request
from chrome_hls_downloader.download import Downloader
from chrome_hls_downloader.hls import parse_playlist, replace_origin_and_query

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:6.000,
seg-1-v1-a1.ts?t=aaa&e=1
#EXTINF:6.000,
seg-2-v1-a1.ts?t=aaa&e=1
#EXTINF:2.256,
seg-3-v1-a1.ts?t=aaa&e=1
#EXT-X-ENDLIST
"""

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION="1920x1080"
https://cdn.example/high/index.m3u8?sig=1
"""


def test_media_playlist():
    pl = parse_playlist(MEDIA, "https://cdn.example/hls/42/index-v1-a1.m3u8?t=aaa&e=1")
    assert pl.is_media and not pl.is_master and pl.endlist
    assert [s.index for s in pl.segments] == [1, 2, 3]
    assert pl.segments[0].uri == "https://cdn.example/hls/42/seg-1-v1-a1.ts?t=aaa&e=1"
    assert abs(pl.total_duration - 14.256) < 1e-9
    assert pl.encryption is None


def test_master_playlist():
    pl = parse_playlist(MASTER, "https://site.example/v/master.m3u8")
    assert pl.is_master and not pl.is_media
    assert pl.variants[0].uri == "https://site.example/v/low/index.m3u8"
    assert max(pl.variants, key=lambda v: v.bandwidth).resolution == "1920x1080"


def test_encrypted_and_map():
    text = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.bin"\n#EXT-X-MAP:URI="init.mp4"\n'
            '#EXTINF:4,\na.m4s\n')
    pl = parse_playlist(text, "https://x.example/p/i.m3u8")
    assert pl.encryption == "AES-128"
    assert pl.init_uri == "https://x.example/p/init.mp4"


def test_not_a_playlist():
    try:
        parse_playlist("<html>", "https://x.example/")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_transplant_keeps_path_takes_host_and_query():
    url = replace_origin_and_query("https://old.example/hls/42/seg-7-v1-a1.ts?t=old&e=1",
                                   "https://new.example:8443/hls/42/seg-300-v1-a1.ts?t=new&e=2")
    assert url == "https://new.example:8443/hls/42/seg-7-v1-a1.ts?t=new&e=2"


def _packets(n):
    return b"".join(bytes([0x47, 0x01, 0x00, 0x10]) + bytes(184) for _ in range(n))


def test_ts_completeness():
    assert ts.is_complete_ts(_packets(10))
    assert not ts.is_complete_ts(_packets(10)[:-5])              # truncated
    bad = bytearray(_packets(10))
    bad[188 * 4] = 0x00                                          # lost sync
    assert not ts.is_complete_ts(bytes(bad))
    assert not ts.is_complete_ts(b"<html>403</html>")


def test_corrupt_sample_keeps_headers():
    data = _packets(200)
    out, n = ts.corrupt_sample(data, packets=10, every=2)
    assert n == 10
    assert ts.is_complete_ts(bytes(out))                         # still demuxable
    assert out != bytearray(data)


class FakeBrowser:
    def __init__(self, reqs):
        self.reqs = reqs

    def latest(self, pred):
        cands = [r for r in self.reqs if r.url and pred(r)]
        return max(cands, key=lambda r: r.seq) if cands else None

    def requests(self):
        return list(self.reqs)


def _req(seq, url, status=200):
    return Request(seq=seq, url=url, session="s", request_id=str(seq),
                   basic_headers={"Referer": "https://player.example/", "Accept-Encoding": "gzip"},
                   extra_headers={"cookie": "a=b", ":authority": "cdn.example",
                                  "sec-fetch-mode": "cors"}, status=status)


def test_credentials_follow_the_newest_request_of_this_stream(tmp_path):
    pl_url = "https://cdn.example/hls/42/index-v1-a1.m3u8?t=aaa&e=1"
    pl = parse_playlist(MEDIA, pl_url)
    playlist_req = _req(1, pl_url)
    browser = FakeBrowser([
        playlist_req,
        _req(2, "https://cdn.example/hls/42/seg-1-v1-a1.ts?t=bbb&e=2"),
        _req(3, "https://ads.example/hls/99/seg-1-v1-a1.ts?t=zzz&e=9"),   # another stream
        _req(4, "https://cdn.example/hls/42/seg-2-v1-a1.ts?t=ccc&e=3", status=403),  # rejected
    ])
    d = Downloader(browser, pl, playlist_req, str(tmp_path))
    c = d.newer_credential()
    assert c.seq == 2 and "t=bbb" in c.url
    d._adopt(c)
    assert d._url_for(pl.segments[2].uri, d.cred) == "https://cdn.example/hls/42/seg-3-v1-a1.ts?t=bbb&e=2"
    h = c.headers
    assert h["cookie"] == "a=b" and h["sec-fetch-mode"] == "cors" and h["referer"]
    assert "accept-encoding" not in h and ":authority" not in h


def test_missing_detects_truncated_segments(tmp_path):
    pl = parse_playlist(MEDIA, "https://cdn.example/hls/42/index.m3u8")
    d = Downloader(FakeBrowser([]), pl, _req(1, pl.url), str(tmp_path))
    with open(d.seg_path(1), "wb") as f:
        f.write(_packets(5))
    with open(d.seg_path(2), "wb") as f:
        f.write(_packets(5)[:-10])
    assert [s.index for s in d.missing()] == [2, 3]
    assert os.path.basename(d.seg_path(3)) == "seg00003.ts"
