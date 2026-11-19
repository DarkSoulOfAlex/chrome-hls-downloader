"""Minimal HLS (m3u8) playlist parsing."""

import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^",]*)')


def parse_attributes(text):
    return {k: v.strip('"') for k, v in _ATTR.findall(text)}


@dataclass
class Segment:
    index: int          # 1-based position in the playlist
    uri: str            # absolute URL as written in the playlist
    duration: float


@dataclass
class Variant:
    uri: str
    bandwidth: int = 0
    resolution: Optional[str] = None


@dataclass
class Playlist:
    url: str
    is_master: bool = False
    variants: List[Variant] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)
    init_uri: Optional[str] = None
    encryption: Optional[str] = None   # METHOD of EXT-X-KEY when not NONE
    byterange: bool = False
    endlist: bool = False

    @property
    def total_duration(self):
        return sum(s.duration for s in self.segments)

    @property
    def is_media(self):
        return bool(self.segments)


def parse_playlist(text, url):
    pl = Playlist(url=url)
    if not text.lstrip("﻿").startswith("#EXTM3U"):
        raise ValueError("not an m3u8 playlist")
    pending_inf = None
    pending_variant = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            tag, _, value = line.partition(":")
            if tag == "#EXTINF":
                try:
                    pending_inf = float(value.split(",", 1)[0])
                except ValueError:
                    pending_inf = 0.0
            elif tag == "#EXT-X-STREAM-INF":
                a = parse_attributes(value)
                pending_variant = Variant(uri="", bandwidth=int(a.get("BANDWIDTH", "0") or 0),
                                          resolution=a.get("RESOLUTION"))
            elif tag == "#EXT-X-KEY":
                method = parse_attributes(value).get("METHOD", "NONE")
                if method.upper() != "NONE":
                    pl.encryption = method
            elif tag == "#EXT-X-MAP":
                uri = parse_attributes(value).get("URI")
                if uri:
                    pl.init_uri = urljoin(url, uri)
            elif tag == "#EXT-X-BYTERANGE":
                pl.byterange = True
            elif tag == "#EXT-X-ENDLIST":
                pl.endlist = True
            continue
        if pending_variant is not None:
            pending_variant.uri = urljoin(url, line)
            pl.variants.append(pending_variant)
            pl.is_master = True
            pending_variant = None
        elif pending_inf is not None:
            pl.segments.append(Segment(index=len(pl.segments) + 1, uri=urljoin(url, line),
                                       duration=pending_inf))
            pending_inf = None
    return pl


def replace_origin_and_query(url, like):
    """Keep the path of ``url`` but take scheme, host and query from ``like``."""
    u, l = urlsplit(url), urlsplit(like)
    return urlunsplit((l.scheme, l.netloc, u.path, l.query, ""))


def path_of(url):
    return urlsplit(url).path
