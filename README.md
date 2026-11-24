# labkit

Small shared pieces for the projects in Gustavo's homelab (a GPU PC and a Raspberry Pi 5),
so each project stops carrying its own copy.

| Piece | What |
|---|---|
| `labkit.waha` | WhatsApp through a self-hosted [WAHA](https://waha.devlike.pro): text and voice notes, retry on 5xx, fail fast on 4xx, and a `MockNotifier` twin for development |
| `labkit.broker` | The local LLM broker (llama-swap): `LLM.for_model(name).chat(...)`, `running()`, `unload()`, `FollowLoaded(default, follow)` (use a capable model already loaded instead of forcing a swap) |
| `labkit.gpu_claims` | Say "I am using GPU0" while working outside the broker's queue and release it when done; `claims()` for whoever is about to start long GPU work |
| `labkit.reddit` | Reddit through its public RSS feeds: `fetch_post(url, user_agent=...)` (permalinks, `redd.it`, app share links) with top comments and outbound links; `RedditRSS.listing(source, sort)` for a subreddit, several (`r/A+B`) or a public multireddit / Custom Feed (`source_of("reddit.com/user/X/m/Y")`), hot/new/rising/top; one request per 65 s, stops on 403/429 |
| `labkit.web` | `fetch_page(url)`: readable text of a page (trafilatura, needs `labkit[web]`), size-capped, never raises; GitHub repo links give the README. Public addresses only: a host or redirect hop resolving to a private, loopback, link-local or Tailscale address gives `None` (`allow_private=True` to opt out). `page_meta(url, user_agent=None)`: a page's own link preview (title, description, site) for pages with too little text or drawn by JavaScript |
| `labkit.scrape` | Scraping through the Evomi residential proxy, never the home IP: `ProxyFetcher` (httpx), `BrowserFetcher` (a headed Chromium under Xvfb, needs `labkit[scrape]`; can block images and fonts), `allow_hosts` keeps only the site's own requests, `engine="patchright"` (`labkit[stealth]`) passes Cloudflare challenges that stock Playwright does not, `raw=True` returns a JSON API's body; `capture="<url part>"` (or a compiled regex) returns the body of the page's own data call (a single-page app's XHR) instead of its HTML; `screenshot(url, prepare=)` returns a PNG of the settled page and its HTML (prepare dismisses a cookie banner); both paced and counting proxy bytes; `RawLog` keeps gzipped copies of the pages; `SourceUnavailable` / `SourceDrift` (a block names its wall when the refusal shows it: `blocked 403 (cloudflare)`, `(datadome)`, `block_vendor()`); `ProxyFetcher.post(url, json=, headers=)` for an authenticated JSON API (e.g. an MCP server) through the proxy |
| `labkit.voice` | How Gustavo writes: `samples(user, n)` returns his recent public Reddit comments (subreddit, post, text) as style examples for any agent drafting in his name; one paced RSS request, cached a week in `~/.cache/labkit/voice` (`LABKIT_VOICE_CACHE`). `RedditRSS.user_comments(user)` underneath |
| `labkit-reddit-mcp` | A stdio MCP server (`labkit[mcp]`) so coding agents can read a Reddit link: `read_reddit_post(url, comments=8, linked_pages=3, image=false)` returns the post, top comments and the text of linked pages (GitHub repos give their README) as Markdown, plus the picture of an image post on request; `read_reddit_listing(source, sort=hot, limit=15)` lists a subreddit's or a public multireddit's posts (cached 10 min). The 65 s pacing is shared by every agent process through a lock file, results are cached for an hour (`~/.cache/labkit/reddit`, `LABKIT_REDDIT_CACHE`), and a refusal is never retried over RSS: since 2026-11-17 Reddit refuses every logged-out read from the lab, so the tool falls back to lab-browser (his logged-in Chrome, read-only; needs his desktop session; `LAB_BROWSER_MCP=0` turns it off) |
| `labkit.youtube` | `fetch_video(url, out_dir, screenshots=8)` (`labkit[youtube]`): title, channel, description, chapters and the transcript (manual or auto captions, through yt-dlp), plus at most 10 timestamped screenshots (chapter starts, scene changes, then evenly spaced) saved in `out_dir`; `to_markdown()`; `python -m labkit.youtube URL --out DIR` prints what it read. ffmpeg comes from PATH or the bundled `imageio-ffmpeg` |
| `labkit.media` | `read_media(url, transcribe=None)` (`labkit[youtube]`): what a video or reel link is about, no login: Instagram, TikTok, X, Facebook, Vimeo (share tracking dropped) and YouTube (through `labkit.youtube`): site, author, date, likes, the caption and what is said (YouTube's captions, or a short clip's speech through the caller's `transcribe(audio_path)`); `to_text()` for prompts; `python -m labkit.media URL`. When yt-dlp cannot read the video (an X post with no video, an age-restricted reel, a photo post) `post_preview` gives the post's own text from X's embed data, TikTok/YouTube/Vimeo oEmbed or Instagram/Facebook's link preview, with a warning; TikTok needs `curl-cffi` (in the extra) |
| `labkit.repovet` | `vet(url, ref=None)` → a `Report` with `verdict` (`ok` / `caution` / `red`) and the signs of a fake GitHub project: automated commits that change nothing, a long README over almost no code, releases shipping executables, base64-hidden links or commands, link shorteners and file hosts, "turn off the antivirus / SmartScreen" instructions, a young look-alike of a bigger repository. Reads through the GitHub API only (never clones or runs anything); `scan_tree(path)` runs the content checks on a clone already on disk; `repo_refs(text)` and `is_pinned(ref)` find the repositories a requirements file or script points at. CLI: `labkit vet <url>` |
| `labkit.access` | `AccessGuard` (`labkit[access]`), ASGI middleware for a web app published through a Cloudflare Tunnel behind Cloudflare Access: a request that carries Cloudflare headers, or comes from a non-private address, needs a valid Access token (`Cf-Access-Jwt-Assertion`, RS256 against the team keys, the app's AUD tag, the team as issuer); the LAN, the tailnet and the host pass as before. Settings `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD`; unset fails closed. The email is in `request.state.access_email` |
| `labkit deploy-pi` | Ship a repo's `deploy/pi` compose project to the Pi: clean-tree guard, version stamp, rsync, build and restart |
| `labkit init` | Join a repo to the lab: `AGENTS.md` (with the Lab block), `CLAUDE.md` → `@AGENTS.md`, and a `lab.yaml` skeleton |
| `labkit install-skills` | Agent skills (`send-whatsapp`, `use-llm-broker`, `deploy-to-pi`, `new-lab-project`) into `~/.claude/skills` |

## Install

Pinned to a release tarball, so slim Docker images need no `git`:

```
labkit @ https://github.com/GustavoPintoDeAbreu/labkit/archive/refs/tags/v0.20.0.tar.gz
labkit[web] @ https://github.com/GustavoPintoDeAbreu/labkit/archive/refs/tags/v0.20.0.tar.gz
labkit[youtube] @ https://github.com/GustavoPintoDeAbreu/labkit/archive/refs/tags/v0.20.0.tar.gz
```

As an agent tool (one install for every agent on the machine):

```
uv tool install "labkit[mcp] @ https://github.com/GustavoPintoDeAbreu/labkit/archive/refs/tags/v0.20.0.tar.gz"
claude mcp add --scope user reddit -- ~/.local/bin/labkit-reddit-mcp
```

(add `[web]` to use `labkit.web`, `[scrape]` for `labkit.scrape`'s browser; then `playwright install --with-deps chromium`)

## Develop

```bash
uv venv && uv pip install -e ".[test]"
.venv/bin/pytest -q
```

The venv is made by uv and has no pip: add packages with `uv pip install ...`.

A release is a tag (`vX.Y.Z`, matching `pyproject.toml` and `labkit/__init__.py`). Projects
bump their pin on purpose; nothing follows `main`.

Addresses, chat ids and keys never live here: they come from each project's `.env`,
arguments, or environment variables (`LAB_BROKER_URL`, `LAB_HUB_URL`, `PI_HOST`).
