# chrome-hls-downloader

Download the HLS video that is playing in your browser, even when the site protects
its stream with short-lived tokens and header checks, and then verify every frame of
the result.

[Italiano](README.it.md)

```
chrome-hls-dl -o movie.mp4
```

A browser window opens. Go to the page, press play, and leave it playing: the tool
finds the stream, downloads all segments in parallel with the browser's own tokens and
headers, joins them into an MP4 and checks that every video and audio frame decodes.

## Why

Many players fetch HLS segments with URLs like `seg-12.ts?t=…&e=…`, where the token
expires after a couple of minutes, and the CDN rejects requests that lack the browser's
`Origin`, `Referer` or `Sec-Fetch-*` headers. Copying a URL into a downloader works for
a minute and then fails with `403`.

This tool doesn't guess any of that. It watches the browser through the
[Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) and
replays requests **exactly as the browser sends them**. When the player rotates its
token, the download switches to the new one, and after a `403` it waits for the next
one.

## Features

- **No site-specific code.** Playlist, tokens, cookies and headers are read from the
  browser's real requests, in every frame. That includes cross-site iframes running in
  other processes, and workers.
- **Rotating tokens.** Switches to a fresh token as soon as the player uses one; on
  `401/403/404/410` it pauses until the player provides the next one. If the token
  turns out to be per-segment, it reloads the playlist instead.
- **Resumable.** Segments are kept in `<output>.parts/`; run the same command again to
  continue.
- **Completeness checks.** Each segment is checked against its `Content-Length`, and TS
  segments must consist of whole 188-byte packets with valid sync bytes.
- **Frame-level verification** (needs ffmpeg), described below.

## Verification

Run automatically after a download, or by hand with `chrome-hls-dl verify FILE`:

1. **Self-test.** A copy of the first 30 seconds is deliberately corrupted, and it must
   produce more ffmpeg reports than the original. If ffmpeg on your machine wouldn't
   reveal damage, the verification stops instead of reporting a false "OK".
2. **Full decode** of every video and audio frame, at `-v warning`, because ffmpeg
   reports concealed or corrupt frames as warnings, not errors. Each report is noted
   with its timestamp.
3. **Checks:**
   - decoded frames equal the frames in the file;
   - no holes in the video and audio timelines;
   - audio and video start together and end together.
4. **Silent tail.** Some sources pad the end with audio-only digital silence. When that
   is found, it is reported as padding, not damage, and a `*.trimmed.mp4` copy without
   it is written.

## Install

You need Python ≥ 3.10, a Chromium-based browser and, for MP4 output and verification,
[ffmpeg](https://ffmpeg.org/) on your `PATH`.

```
pip install git+https://github.com/DarkSoulOfAlex/chrome-hls-downloader
```

On Windows, ffmpeg can be installed with `winget install -e --id Gyan.FFmpeg`.

## Usage

```
chrome-hls-dl [-o OUTPUT] [options]
chrome-hls-dl verify FILE [--no-trim]
```

| option | |
|---|---|
| `-o, --output` | output file (default `video.mp4`) |
| `-j, --parallel` | parallel downloads (default 6). More is not always faster: many servers throttle per IP |
| `--browser` | browser executable (default: Chrome, then Edge, Brave, Chromium, Vivaldi) |
| `--port` | remote debugging port (default 9222) |
| `--profile` | browser profile folder (a separate profile is used, not your everyday one) |
| `--url` | page to open in the browser |
| `--workdir` | segments folder (default `<output>.parts`) |
| `--wait` | give up if no video starts within N seconds |
| `--token-wait` | how long to wait for a fresh token (default 600 s) |
| `--no-verify`, `--no-trim`, `--keep-segments` | |

Tips:

- **Keep the video playing**, and don't minimize the window. The player only fetches
  fresh tokens while it plays, and browsers throttle hidden, muted pages.
- **If the video was already playing** when the tool started, the playlist may have
  been loaded before it was watching. Reload the page (F5) and press play.
- **The browser runs with a separate profile.** Recent Chrome versions refuse remote
  debugging on the default profile, so your everyday browser stays untouched. Log in
  inside the new window if the site needs it.

## Browser support

Chrome, Edge, Brave, Chromium, Vivaldi, Opera: any Chromium-based browser. Firefox and
Safari are not supported, because they don't offer the Chrome DevTools Protocol.

## Limitations

- **HLS only:** no DASH and no progressive MP4.
- **Not supported:** encrypted streams (`EXT-X-KEY` AES-128 / SAMPLE-AES), DRM
  (Widevine, FairPlay, PlayReady) and byte-range playlists. The tool does not circumvent
  encryption or DRM.
- **Live streams:** only the segments listed in the playlist when it was read are
  downloaded.
- **Quality:** the output is a bit-exact copy of what the site serves. Compression
  artifacts in the source (blocky flames, explosions, …) stay in the copy.

## Legal

Use this tool only for content you own, content in the public domain, or content you
have permission to download. You are responsible for respecting copyright and the terms
of service of the sites you use it on.

## Development

```
pip install -e .[test]
pytest                    # unit tests + end-to-end test (needs a Chromium browser and ffmpeg)
pytest -m "not e2e"       # unit tests only
```

The end-to-end test (`tests/test_e2e.py`) starts a fake streaming site with three
origins, a cross-site player iframe, tokens that expire every few seconds, header
checks, and media whose audio outlasts the video. It then runs the real tool against it
in a headless browser.

## License

MIT
