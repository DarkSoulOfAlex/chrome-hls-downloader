"""Verify a downloaded video with ffmpeg.

1. Self-test: a deliberately corrupted copy of the first 30 s must produce more ffmpeg
   reports than the original. If not, the decoder on this machine would not reveal damage
   and the verification stops instead of passing.
2. Decode every video and audio frame (at ``-v warning``: ffmpeg reports corrupt frames
   as warnings, not errors) and note the time of every report.
3. Check decoded frames == frames in the file, no holes in the video/audio timelines,
   audio and video start together and end together.
4. If audio continues after the video only as digital silence (padding found in some
   sources), write a copy without that tail.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from . import ts as tsmod

SILENCE_DB = -70.0


def find_tools():
    return shutil.which("ffmpeg"), shutil.which("ffprobe")


def _hms(secs):
    secs = int(max(0, secs))
    return "%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


@dataclass
class DecodeResult:
    reports: List[str] = field(default_factory=list)   # "hh:mm:ss  message"
    frames: int = 0
    returncode: int = 0


def decode(ffmpeg, path, total=0.0, progress=None):
    """Decode all video and audio; ``progress(seconds)`` is called about once per second."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "warning", "-progress", "pipe:1",
           "-stats_period", "1", "-i", path, "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"]
    res = DecodeResult()
    cur = [0.0]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, errors="replace")

    def read_err():
        for line in p.stderr:
            line = line.strip()
            if line:
                res.reports.append("%s  %s" % (_hms(cur[0]), line))

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    for line in p.stdout:
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and value.isdigit():
            cur[0] = int(value) / 1e6
        elif key == "frame" and value.isdigit():
            res.frames = int(value)
        elif key == "progress" and progress:
            progress(cur[0], len(res.reports))
    p.wait()
    t.join()
    res.returncode = p.returncode
    return res


@dataclass
class StreamInfo:
    has_video: bool = False
    has_audio: bool = False
    vcodec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vstart: float = 0.0
    vdur: float = 0.0
    vframes: int = 0
    acodec: str = ""
    rate: float = 0.0
    channels: int = 0
    astart: float = 0.0
    adur: float = 0.0
    aframes: int = 0


def probe(ffprobe, path):
    cmd = [ffprobe, "-v", "error", "-show_entries",
           "stream=codec_type,codec_name,width,height,start_time,duration,nb_frames,avg_frame_rate,"
           "sample_rate,channels", "-of", "json", path]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True).stdout
    info = StreamInfo()
    for s in json.loads(out or "{}").get("streams", []):
        if s.get("codec_type") == "video" and not info.has_video:
            info.has_video = True
            info.vcodec = s.get("codec_name", "")
            info.width, info.height = int(s.get("width", 0)), int(s.get("height", 0))
            num, _, den = str(s.get("avg_frame_rate", "0/0")).partition("/")
            info.fps = _num(num) / _num(den) if _num(den) else 0.0
            info.vstart, info.vdur = _num(s.get("start_time")), _num(s.get("duration"))
            info.vframes = int(_num(s.get("nb_frames")))
        elif s.get("codec_type") == "audio" and not info.has_audio:
            info.has_audio = True
            info.acodec = s.get("codec_name", "")
            info.rate, info.channels = _num(s.get("sample_rate")), int(s.get("channels", 0))
            info.astart, info.adur = _num(s.get("start_time")), _num(s.get("duration"))
            info.aframes = int(_num(s.get("nb_frames")))
    return info


def self_test(ffmpeg, path, seconds=30):
    """Return (passed, clean_reports, corrupted_reports, clean_frames)."""
    tmp = tempfile.mkdtemp(prefix="chls-selftest-")
    try:
        clean = os.path.join(tmp, "clean.ts")
        bad = os.path.join(tmp, "corrupted.ts")
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", path, "-t", str(seconds),
                        "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "mpegts", clean],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.isfile(clean):
            return False, ["could not extract a sample"], [], 0
        with open(clean, "rb") as f:
            data, n = tsmod.corrupt_sample(f.read())
        with open(bad, "wb") as f:
            f.write(data)
        c = decode(ffmpeg, clean)
        b = decode(ffmpeg, bad)
        # detection works if the corrupted copy produces more reports than the original
        # (the original may itself be damaged: that is reported by the full decode)
        ok = n > 0 and c.frames > 0 and len(b.reports) > len(c.reports)
        return ok, c.reports, b.reports, c.frames
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def max_volume(ffmpeg, path, start):
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-ss", "%.3f" % start, "-i", path,
           "-map", "0:a", "-af", "volumedetect", "-f", "null", "-"]
    err = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         universal_newlines=True, errors="replace").stderr
    vol = None
    for line in err.splitlines():
        if "max_volume:" in line:
            v = line.split("max_volume:", 1)[1].strip().split()[0]
            vol = -999.0 if v == "-inf" else _num(v, None)
    return vol


@dataclass
class Report:
    checks: List[tuple] = field(default_factory=list)   # (ok, message)
    notes: List[str] = field(default_factory=list)
    reports: List[str] = field(default_factory=list)
    trimmed: Optional[str] = None
    self_test_ok: bool = False

    @property
    def ok(self):
        return self.self_test_ok and all(ok for ok, _ in self.checks)


def verify(path, trim_silent_tail=True, out=print, progress=True):
    ffmpeg, ffprobe = find_tools()
    rep = Report()
    if not ffmpeg or not ffprobe:
        rep.checks.append((False, "ffmpeg/ffprobe not found: install ffmpeg to verify"))
        return rep

    def check(ok, msg):
        rep.checks.append((ok, msg))
        out("   %s  %s" % ("OK" if ok else "KO", msg))

    out("Verifying %s" % path)
    out(" 1/3 self-test (a deliberately corrupted sample must be detected)...")
    ok, clean_r, bad_r, frames = self_test(ffmpeg, path)
    rep.self_test_ok = ok
    if not ok:
        out("   KO  self-test failed (original sample: %d reports, %d frames; corrupted copy: %d reports)"
            % (len(clean_r), frames, len(bad_r)))
        out("       the verification would not be reliable on this machine; stopping")
        rep.reports = clean_r + bad_r
        return rep
    out("   OK  original sample: %d reports, corrupted copy: %d reports" % (len(clean_r), len(bad_r)))

    info = probe(ffprobe, path)
    total = max(info.vdur, info.adur)
    out(" 2/3 decoding every video and audio frame...")

    def show(sec, nrep):
        if progress:
            print("\r     %5.1f%%  %s / %s  reports: %d   "
                  % (min(100.0, 100.0 * sec / max(1.0, total)), _hms(sec), _hms(total), nrep),
                  end="", flush=True)

    dec = decode(ffmpeg, path, total, show)
    if progress:
        print()
    rep.reports = dec.reports
    out(" 3/3 checks")
    if info.has_video:
        out("     video: %s %dx%d %.3f fps, %d frames, %s"
            % (info.vcodec, info.width, info.height, info.fps, info.vframes, _hms(info.vdur)))
    if info.has_audio:
        out("     audio: %s %d Hz, %d ch, %s" % (info.acodec, info.rate, info.channels, _hms(info.adur)))
    check(dec.returncode == 0 and not dec.reports,
          "all video and audio frames decode cleanly (%d reports)" % len(dec.reports))
    if info.has_video:
        check(abs(dec.frames - info.vframes) <= 2,
              "decoded frames %d of %d in the file" % (dec.frames, info.vframes))
        hole = info.vdur - info.vframes / info.fps if info.fps else 0.0
        check(abs(hole) < 0.5, "video timeline has no holes (gap %.3f s)" % hole)
    if info.has_audio and info.acodec == "aac" and info.rate:
        hole = info.adur - info.aframes * 1024 / info.rate
        check(abs(hole) < 0.5, "audio timeline has no holes (gap %.3f s)" % hole)
    if info.has_video and info.has_audio:
        check(abs(info.vstart - info.astart) < 0.5,
              "audio and video start together (offset %.3f s)" % (info.vstart - info.astart))
        vend, aend = info.vstart + info.vdur, info.astart + info.adur
        tail = aend - vend
        if abs(tail) < 0.5:
            check(True, "audio and video end together (offset %.3f s)" % tail)
        elif tail > 0:
            # skip the first half second: lossy audio codecs smear the last sound a little past the cut
            vol = max_volume(ffmpeg, path, vend + min(0.5, tail / 2))
            if vol is not None and vol <= SILENCE_DB:
                note = ("after the video ends there are %.1f s of silent audio only (max %.1f dB): "
                        "padding in the source, not damage" % (tail, vol))
                rep.notes.append(note)
                out("     NOTE  " + note)
                if trim_silent_tail and all(ok for ok, _ in rep.checks):
                    rep.trimmed = _trim(ffmpeg, ffprobe, path, vend - min(info.vstart, info.astart),
                                        info, check)
            else:
                check(False, "audio continues %.1f s after the video and is NOT silent "
                             "(max %s dB): video is missing" % (tail, vol))
        else:
            check(False, "video continues %.1f s after the audio: audio is missing" % -tail)
    return rep


def _trim(ffmpeg, ffprobe, path, duration, info, check):
    root, ext = os.path.splitext(path)
    out_path = root + ".trimmed" + ext
    p = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", path,
                        "-map", "0:v", "-map", "0:a", "-c", "copy", "-t", "%.3f" % duration, out_path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if p.returncode != 0:
        check(False, "could not write the trimmed copy: %s" % p.stderr.strip()[-300:])
        return None
    t = probe(ffprobe, out_path)
    check(abs(t.vframes - info.vframes) <= 2,
          "%s: all frames kept (%d of %d)" % (os.path.basename(out_path), t.vframes, info.vframes))
    end_gap = (t.astart + t.adur) - (t.vstart + t.vdur)
    check(abs(end_gap) < 0.5, "%s: audio and video end together (offset %.3f s), duration %s"
          % (os.path.basename(out_path), end_gap, _hms(t.vdur)))
    return out_path
