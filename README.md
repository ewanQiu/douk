# douk

**English** | [简体中文](README.zh-CN.md)

Batch-archive complete TikTok **drama series** (短剧) — plus playlists/mixes,
collections, and whole profiles. Collection and download are separate stages,
so runs are resumable and incremental.

**Why this exists:** yt-dlp has no extractor for TikTok drama series. Its
`tiktok:collection` matches only `/@user/collection/...` (saved collections),
not drama series and not playlists. If you want an entire 30-, 50-, or 80-episode
series with the episodes in the right order, you have to build the listing step
yourself. That's what douk does.

```powershell
.\setup.bat                                            # one-time setup
.\douk sync "https://www.tiktok.com/@user/video/123"   # grab the whole series
```

## Features

- **Whole series in one command.** Point it at *any* episode's URL; it finds the
  series and pulls every episode.
- **Correct episode numbers.** Taken from the API's `EpisodeNumber`, not from
  upload time — a series' episodes are often published in one batch, so sorting
  by timestamp gives you a shuffled mess.
- **Resumable and incremental.** Progress lives in SQLite. Re-running only fetches
  what's new; interrupted downloads pick up where they left off.
- **Covers and metadata.** Series title, description, genre tags, episode count,
  and total runtime are saved as `cover.jpg` + `drama.json` next to the videos.
- **Codec choice.** H.264 for universal playback, H.265 for 1080p, or download
  1080p H.265 and transcode to H.264 to get both.
- **Subtitles, thumbnails, and per-episode metadata** via yt-dlp.
- **`doctor` command** that checks egress IP, proxy/VPN flags, TikTok
  reachability, browser, ffmpeg, and login state before you waste time debugging.

## Requirements

| | |
|---|---|
| Python | **3.11+** (uses `tomllib`) |
| Chrome | Used via Playwright's `channel="chrome"`. Alternatively install Playwright's bundled Chromium and set `channel = ""`. |
| ffmpeg | Optional — only needed for transcoding and thumbnail conversion |
| Network | Must be able to reach TikTok. Set `proxy` in `config.toml` if yours can't. |

Windows is the tested platform. The Python code is cross-platform, but the
`setup` / `package` helper scripts are PowerShell.

## Install

```powershell
.\setup.bat
```

Finds Python (installs it via winget if missing), creates a virtualenv, installs
dependencies, checks Chrome and ffmpeg, then runs a self-check.

> Run `setup.bat`, not `setup.ps1` — Windows blocks `.ps1` execution by default.
> If you prefer the `.ps1`: `powershell -ExecutionPolicy Bypass -File .\setup.ps1`

Manual install:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# only if you have no Chrome and want Playwright's bundled browser
.\.venv\Scripts\python.exe -m playwright install chromium
```

Then copy `config.example.toml` to `config.toml` and set `out_dir`.

## Getting a session

**This step is mandatory.** Without a login, TikTok's list endpoints return
nothing and `playAddr` comes back as an empty string — which is the real reason
yt-dlp reports `No video formats found` on these URLs.

Everything reduces to one file: **`data/cookies.txt`**.

### Option A — browser extension (easiest)

1. Install *Get cookies.txt LOCALLY* (open source) from the Chrome Web Store
2. Open **tiktok.com while logged in**
3. Click the extension, confirm **Export Format is `Netscape`**, click **Export**
   — not *Export All Cookies*, which dumps every site you use
4. Save the downloaded file as `data/cookies.txt`

### Option B — copy the Cookie request header

The `Cookie:` request header contains every cookie, HttpOnly ones included.

1. On tiktok.com, press F12 → **Network** → reload
2. Click any request to tiktok.com → **Headers** → **Request Headers** → right-click
   the `Cookie:` line → **Copy value**
3. Save it to `data/header.txt`, then:

```powershell
.\douk cookies --from-header data\header.txt
```

> It must be `Cookie:` from **Request** Headers, not `Set-Cookie:` from the
> response. The converter checks for `sessionid` and fails loudly if you got the
> wrong one.

### Verify

```powershell
.\douk verify
```

Prints which account TikTok recognizes. Cookies last weeks to months; when
`verify` starts failing, export again.

> `data/cookies.txt` is equivalent to your account password. Never commit or
> share it — the bundled `.gitignore` excludes it.

**Why manual?** The `login`, `cookies --from`, and `import-profile` commands
exist but none of them worked on the test machine: in-browser login is rejected
because the automated environment is detected; Chrome 127+ encrypts its cookie
store in a way that can't be read externally; and copying the browser profile
fails because the key is bound to the original profile path. Manual export takes
30 seconds and always works.

## Usage

```powershell
.\douk doctor                  # check environment first
.\douk dramas "<profile url>"  # list a creator's series
.\douk sync "<any episode url>"   # resolve -> collect -> download
.\douk status                  # what's in the library
```

Accepted URLs:

| Input | Behavior |
|---|---|
| `/@user/video/123` | Finds which series the video belongs to, grabs all of it. Falls back to the whole profile if it isn't part of one. |
| `/@user/playlist/xxx-123` | The whole playlist/mix |
| `/@user/collection/xxx-123` | The whole collection |
| `/@user` | Every video on the profile |

Useful flags:

```powershell
.\douk sync "<url>" --collect-only        # index without downloading
.\douk download -t <id> --limit 3         # try a few episodes first
.\douk sync "<url>" --codec transcode     # 1080p, transcoded to H.264
.\douk sync "<url>" --headful             # watch the browser work
```

Other commands: `collect`, `download`, `convert` (re-encode what you already
have), `relocate` (rename/move files after changing naming settings),
`info` (show series metadata).

## How series are stored on TikTok

There are four different containers, each with its own endpoint. douk handles all
four:

| Container | Endpoint | Notes |
|---|---|---|
| **Drama series** | `/api/drama/episode/item_list/` | The dominant format today. Rendered as `creator-drama-card` wrapped in a `<button>`, **not an `<a href>`** — so there is no link on the page to follow; the series ID has to come from the API. |
| Mix / playlist | `/api/mix/item_list/` | Older format, URLs look like `/playlist/name-{mixId}` |
| Collection | `/api/collection/item_list/` | User-curated saved items |
| Profile posts | `/api/post/item_list/` | Everything a creator posted |

Three things about drama collection that took real debugging:

- **Pagination advances by cursor inside the page context; the UI is never
  driven.** Both UI approaches proved unreliable in testing. *Scrolling* moves
  the video feed to the next video and swaps the whole page out, so you drift
  away from the series you wanted. *Clicking the `1-24 / 25-48 / 49-72` range
  tabs* is worse than it looks — those tabs are rendered dynamically, and the
  same series can render two tabs on one load and none on the next, so you
  reliably come up short by the last few episodes.
- **Responses must be filtered by `dramaID`.** Read past the end of one series
  and the API starts handing you the next one; without filtering, two series get
  interleaved in your library.
- **Episode numbers come from `EpisodeNumber`, never from `createTime`.**

## Codec and playback

TikTok offers two encodings of the same video, and it's a genuine tradeoff:

| Codec | Max resolution | Playback |
|---|---|---|
| H.264 | 576x1024 | Plays everywhere |
| H.265 | **1080x1920** | Windows' built-in player needs a paid HEVC extension |

Set `video_codec` in `config.toml` (or `--codec` per run):

- **`h264`** (default) — 576x1024, no transcoding, plays anywhere.
- **`h265`** — 1080p. The easy fix for playback is a different player: VLC,
  PotPlayer, and MPV all handle HEVC natively, with no transcode and no quality loss.
- **`transcode`** — download 1080p H.265, re-encode to H.264 with ffmpeg.

Already downloaded? `.\douk convert -t <id>` re-encodes in place. It's idempotent
and resumable.

**Transcoding always increases file size** — H.264 is simply less efficient than
H.265. Measured on a 30-episode 1080p series (663 kbps source):

| CRF | vs. source | Time/episode |
|---|---|---|
| 23 | 3.1x | 26s |
| **26** (default) | **2.3x** | **23s** |
| 28 | 1.9x | 21s |

`preset slow` was only 5% smaller than `medium` while taking 30% longer, so
`medium` is fixed. The full series went from 131 MB to 323 MB.

## Output layout

```
data/
├─ douk.db                    # SQLite: target + aweme tables
├─ cookies.txt                # your session (gitignored)
├─ browser/                   # Playwright runtime cache, safe to delete
└─ videos/{series}/
     ├─ 第1集.mp4  第1集.srt  第1集.jpg  第1集.info.json
     ├─ cover.jpg             # series cover art
     └─ drama.json            # title / description / genres / runtime
```

Filenames follow `filename_template` in `config.toml` (default `第{seq}集`).
Placeholders: `{seq}` `{seq2}` (zero-padded) `{seq3}` `{id}` `{title}` `{drama}`.
Use `第{seq2}集` if you want filename sort order to match episode order.

## Troubleshooting

**Run `.\douk doctor` first.** It reports egress IP and geolocation, whether that
IP is flagged as a proxy, Cloudflare WARP status, TikTok reachability, browser
launch, ffmpeg, output directory writability, and login state.

| Symptom | Fix |
|---|---|
| `No video formats found` | No session, or it expired — re-export `cookies.txt` and run `douk verify` |
| "collected nothing" | Same as above, or a captcha — try `--headful` and clear it by hand |
| `get region err` on login | The automated environment was detected. Don't log in through douk; export cookies manually. If your normal browser can't log in either, it's your IP (datacenter and VPN ranges get flagged). |
| Rate limited | Wait 30–60 minutes, change IP, delete `data/browser/` |
| Downloads are slow or throttled | Lower `concurrency`, raise `delay_min`/`delay_max` |
| Video won't play | It's H.265 — install VLC, or run `douk convert` |
| Only got N of M episodes | Network was slow mid-pagination; just run `collect` again |

## Moving to another machine

```powershell
.\package.bat     # produces douk-package.zip, ~0.05 MB
```

Source and config only. `.venv` (164 MB) hardcodes absolute paths and
`data/browser` (231 MB) is machine-local state — both must be rebuilt on the
target, so copying them is pointless. Unzip and run `setup.bat`.

## License and disclaimer

Source code is MIT — see [LICENSE](LICENSE).

**The MIT license covers this repository's code only and grants no rights to any
downloaded content.** Please read [DISCLAIMER.md](DISCLAIMER.md) before using
this tool: intended use, copyright ownership, terms-of-service responsibility,
and credential safety.

This tool is for archiving content you already have access to, for personal
offline viewing. Downloaded material belongs to its creators. Keep request rates
modest — `concurrency`, `delay_min`, and `delay_max` exist for that reason.
