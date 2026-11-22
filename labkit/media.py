"""What a video or reel link is about, as text: Instagram, TikTok, X, Facebook, Vimeo and YouTube.

``read_media(url)`` reads one post through yt-dlp (``labkit[youtube]``) without logging in: the
site, its author, the date, the caption (the words the poster wrote) and, when it can, what is
said in it: YouTube's own captions (``labkit.youtube``), else the speech of a short clip turned
into text by the caller's ``transcribe(audio_path) -> str`` (Whisper in lab-assistant). Only the
audio of a clip up to ``max_transcribe_s`` long is downloaded, into a temporary folder that is
deleted afterwards; yt-dlp's disk cache is off.

Share links lose their tracking parameters (Instagram's ``?igsh=``, TikTok's ``?_t=``, X's ``?s=``)
before they are read. A link that is not one of these sites raises NotMediaLink; a post yt-dlp
cannot read (private, deleted, a login wall) raises MediaUnreadable. Everything else (no speech,
no ffmpeg, a long clip) becomes a warning on the returned Media.

When yt-dlp cannot read a post, its caption is still taken from what the site gives anyone
(``post_preview``): X's public embed data (a post with no video is text, a photo or a poll), TikTok's,
YouTube's and Vimeo's oEmbed, and the link preview Instagram and Facebook give link-preview
crawlers (an age-restricted reel, a photo post). The Media then says so in its warnings, and
MediaUnreadable is raised only when even that is empty (a deleted post, a made-up link).
TikTok needs yt-dlp's browser impersonation (``curl-cffi``, in ``labkit[youtube]``).

Usage: ``python -m labkit.media URL`` prints what it read (no transcript of speech).
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import httpx

from labkit.reddit import _parse
from labkit.web import page_meta, truncate
from labkit.youtube import BASE_OPTS, VideoUnreadable, fetch_video, is_youtube_url, watch_url, video_id

CAPTION_CHARS = 2000
TRANSCRIPT_CHARS = 6000
MAX_TRANSCRIBE_S = 180            # longer clips: caption only (Whisper on the CPU is ~real time / 4)
MAX_AUDIO_BYTES = 40_000_000

# host (or its parent) -> the site's name; a post's path must also look like one post (``_is_post``).
SITES = {"instagram.com": "Instagram", "tiktok.com": "TikTok", "x.com": "X", "twitter.com": "X",
         "facebook.com": "Facebook", "fb.watch": "Facebook", "vimeo.com": "Vimeo"}


class MediaError(Exception):
    """Base: not a media link, or the post could not be read."""


class NotMediaLink(MediaError):
    """Not a link to one video or reel on a site this module reads."""


class MediaUnreadable(MediaError):
    """yt-dlp could not read the post (private, deleted, behind a login)."""


@dataclass
class Media:
    url: str                    # the link as read (tracking parameters dropped)
    site: str                   # "Instagram", "YouTube", ...
    author: str
    title: str                  # the site's title; generic on Instagram ("Video by <user>")
    caption: str                # what the poster wrote (description)
    published: str = ""         # "YYYY-MM-DD" or ""
    duration: float = 0.0       # seconds, 0.0 if unknown
    likes: int | None = None
    transcript: str = ""
    transcript_source: str = ""     # "captions" (the site's) | "speech" (transcribed) | ""
    warnings: list[str] = field(default_factory=list)

    def to_text(self, caption_chars: int = CAPTION_CHARS, transcript_chars: int = TRANSCRIPT_CHARS) -> str:
        """For a prompt: one header line, the caption, then what is said."""
        facts = [self.site, self.author and f"by {self.author}", self.published,
                 self.duration and f"{int(self.duration // 60)}:{int(self.duration % 60):02d}",
                 self.likes is not None and f"{self.likes} likes"]
        lines = [" · ".join(str(f) for f in facts if f), self.url]
        if self.title and not self.title.lower().startswith(("video by ", "post by ", "reel by ")):
            lines += ["", f"Title: {self.title}"]
        lines += ["", "Caption:", truncate(self.caption.strip(), caption_chars) or "(none)"]
        if self.transcript:
            label = "its captions" if self.transcript_source == "captions" else "speech, transcribed"
            lines += ["", f"What is said ({label}):", truncate(self.transcript.strip(), transcript_chars)]
        else:
            lines += ["", "What is said: (nothing read)"]
        if self.warnings:
            lines += ["", "Notes: " + "; ".join(self.warnings)]
        return "\n".join(lines)


def _site(host: str) -> str:
    host = host.lower().removeprefix("www.").removeprefix("m.")
    for known, name in SITES.items():
        if host == known or host.endswith("." + known):
            return name
    return ""


def _is_post(site: str, host: str, parts: list[str]) -> bool:
    if site == "Instagram":
        return len(parts) >= 2 and parts[0] in ("reel", "reels", "p", "tv")
    if site == "TikTok":
        return host.startswith(("vm.", "vt.")) and len(parts) == 1 or "video" in parts or parts[:1] == ["t"]
    if site == "X":
        return "status" in parts
    if site == "Facebook":
        return host == "fb.watch" or bool(parts) and parts[0] in ("reel", "watch", "share") or "videos" in parts
    if site == "Vimeo":
        return bool(parts) and parts[-1].isdigit()
    return False


def media_site(url: str) -> str:
    """The site's name when ``url`` is one video or reel this module reads, else ""."""
    if is_youtube_url(url):
        return "YouTube"
    u = _parse(url)
    if u.scheme not in ("http", "https"):
        return ""
    host = (u.hostname or "").lower()
    site = _site(host)
    parts = [p for p in u.path.split("/") if p]
    return site if site and _is_post(site, host.removeprefix("www.").removeprefix("m."), parts) else ""


def is_media_url(url: str) -> bool:
    return bool(media_site(url))


def clean_url(url: str) -> str:
    """The link without its query or fragment (share tracking); YouTube's canonical watch URL."""
    vid = video_id(url)
    if vid:
        return watch_url(vid)
    u = _parse(url)
    if (u.hostname or "").lower().endswith("facebook.com") and u.path.rstrip("/") == "/watch":
        return u._replace(fragment="").geturl()        # /watch?v=<id> needs its query
    return u._replace(query="", fragment="").geturl()


class Reader(Protocol):
    def info(self, url: str) -> dict: ...

    def audio(self, url: str, work: Path) -> Path | None: ...


class YtDlpMedia:
    """yt-dlp behind two seams (``info``, ``audio``), so tests never need it."""

    def __init__(self):
        try:
            import yt_dlp  # optional: labkit[youtube]
        except ImportError as e:
            raise ImportError("labkit.media needs yt-dlp: pip install 'labkit[youtube]'") from e
        self._yt_dlp = yt_dlp

    def info(self, url: str) -> dict:
        try:
            with self._yt_dlp.YoutubeDL(dict(BASE_OPTS)) as ydl:
                return ydl.sanitize_info(ydl.extract_info(url, download=False))
        except self._yt_dlp.utils.YoutubeDLError as e:
            raise MediaUnreadable(f"yt-dlp could not read {url}: {e}") from e

    def audio(self, url: str, work: Path) -> Path | None:
        opts = {**BASE_OPTS, "outtmpl": str(work / "audio.%(ext)s"), "format": "bestaudio/best",
                "max_filesize": MAX_AUDIO_BYTES}
        try:
            with self._yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
        except self._yt_dlp.utils.YoutubeDLError as e:
            raise MediaUnreadable(f"yt-dlp could not download {url}: {e}") from e
        files = [p for p in work.iterdir() if p.name.startswith("audio.") and p.stat().st_size > 0]
        return files[0] if files else None


def _date(info: dict) -> str:
    raw = str(info.get("upload_date") or "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    ts = info.get("timestamp")
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%Y-%m-%d") if ts else ""


CRAWLER_UA = "facebookexternalhit/1.1"     # Instagram and Facebook give their link preview to this one
X_EMBED = "https://cdn.syndication.twimg.com/tweet-result"
OEMBED = {"TikTok": "https://www.tiktok.com/oembed", "YouTube": "https://www.youtube.com/oembed",
          "Vimeo": "https://vimeo.com/api/oembed.json"}
_IG_TITLE = re.compile(r"^(?P<author>.+?) on Instagram: [\"“](?P<caption>.*)[\"”]\s*$", re.S)
_IG_DESC = re.compile(r"^(?:[\d.,KMkm]+ likes?, [\d.,KMkm]+ comments? - )?(?P<author>[\w.]+) on "
                      r"(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4}): [\"“](?P<caption>.*)[\"”]\.?\s*$", re.S)


def _x_post(url: str, http: httpx.Client) -> Media | None:
    parts = [p for p in _parse(url).path.split("/") if p]
    post_id = parts[parts.index("status") + 1] if "status" in parts and parts.index("status") + 1 < len(parts) else ""
    if not post_id.isdigit():
        return None
    r = http.get(X_EMBED, params={"id": post_id, "token": "a", "lang": "en"})
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
        return None
    data = r.json()
    if data.get("__typename") == "TweetTombstone" or not (data.get("text") or "").strip():
        return None
    user = data.get("user") or {}
    kinds = sorted({str(m.get("type")) for m in data.get("mediaDetails") or [] if m.get("type")})
    caption = str(data["text"])
    quoted = (data.get("quoted_tweet") or {}).get("text")
    if quoted:
        caption += f"\n\n(quoting: {quoted})"
    if kinds:
        caption += f"\n\n(attached: {', '.join(kinds)})"
    likes = data.get("favorite_count")
    return Media(url=clean_url(url), site="X", author=str(user.get("name") or user.get("screen_name") or ""),
                 title="", caption=caption, published=str(data.get("created_at") or "")[:10],
                 likes=int(likes) if isinstance(likes, (int, float)) else None)


def _oembed(site: str, url: str, http: httpx.Client) -> Media | None:
    r = http.get(OEMBED[site], params={"url": url, "format": "json"})
    if r.status_code != 200:
        return None
    data = r.json()
    title = str(data.get("title") or "").strip()
    if not title:                                   # TikTok answers a removed video with no title
        return None
    # TikTok's title is the poster's caption; YouTube's and Vimeo's is the video's title.
    return Media(url=url, site=site, author=str(data.get("author_name") or ""),
                 title="" if site == "TikTok" else title, caption=title if site == "TikTok" else "")


def _preview(site: str, url: str, http: httpx.Client) -> Media | None:
    meta = page_meta(url, http, user_agent=CRAWLER_UA)
    title, desc = meta.get("title", ""), meta.get("description", "")
    if not (title or desc) or title.strip().lower() in ("instagram", "facebook", "log in or sign up to view"):
        return None
    author, caption, published = "", desc, ""
    found = _IG_DESC.match(desc) if site == "Instagram" else None
    if found:
        author, caption = found["author"], found["caption"]
        try:
            published = dt.datetime.strptime(found["date"], "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    elif site == "Instagram" and (t := _IG_TITLE.match(title)):
        author, caption = t["author"], t["caption"]
    return Media(url=url, site=site, author=author, title=title, caption=caption, published=published)


def post_preview(site: str, url: str, client: httpx.Client | None = None) -> Media | None:
    """What anyone is shown of one post (see the module docstring), or None. Never raises."""
    http = client or httpx.Client(timeout=20, follow_redirects=True,
                                  headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    target = watch_url(video_id(url)) if site == "YouTube" and video_id(url) else clean_url(url)
    try:
        if site == "X":
            return _x_post(target, http)
        if site in OEMBED:
            return _oembed(site, target, http)
        return _preview(site, target, http)
    except Exception:
        return None
    finally:
        if client is None:
            http.close()


def _from_preview(site: str, url: str, error: Exception, preview) -> Media:
    """The post's preview in place of an unreadable video, or the error again when there is none."""
    media = preview(site, url) if preview is not None else None
    if media is None:
        raise error if isinstance(error, MediaUnreadable) else MediaUnreadable(str(error))
    why = str(error).split("ERROR:")[-1].strip()[:160]
    media.warnings.append(f"the video itself could not be read ({why}): this is the post's text only")
    return media


def read_media(url: str, *, transcribe: Callable[[Path], str] | None = None,
               max_transcribe_s: float = MAX_TRANSCRIBE_S, reader: Reader | None = None,
               preview: Callable[[str, str], Media | None] | None = None) -> Media:
    """Read one video or reel: who posted it, when, its caption and what is said in it.

    ``transcribe`` turns an audio file into text; without it a clip's speech is not read (YouTube's
    captions still are). ``preview(site, url)`` is what a post yt-dlp cannot read falls back to
    (``post_preview`` by default; with a test ``reader`` there is none unless given). Raises
    NotMediaLink and MediaUnreadable (see the module docstring)."""
    site = media_site(url)
    if not site:
        raise NotMediaLink(f"not a video or reel link: {url}")
    if preview is None and reader is None:
        preview = post_preview
    if site == "YouTube":
        try:
            v = fetch_video(url, None) if reader is None else None
        except VideoUnreadable as e:
            return _from_preview(site, url, e, preview)
        if v is not None:
            return Media(url=v.url, site=site, author=v.channel, title=v.title, caption=v.description,
                         published=v.published, duration=v.duration, transcript=v.transcript_text(),
                         transcript_source="captions" if v.transcript else "", warnings=list(v.warnings))
    target = clean_url(url)
    rd = reader or YtDlpMedia()
    try:
        info = rd.info(target)
    except MediaUnreadable as e:
        return _from_preview(site, target, e, preview)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = {**info, **(info["entries"][0] or {})}            # a carousel post: its first video
    duration = float(info.get("duration") or 0)
    likes = info.get("like_count")
    media = Media(url=target, site=site,
                  author=str(info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""),
                  title=str(info.get("title") or ""), caption=str(info.get("description") or ""),
                  published=_date(info), duration=duration, likes=int(likes) if isinstance(likes, (int, float)) else None)
    if transcribe is None:
        return media
    if duration <= 0 and site != "Instagram":
        media.warnings.append("unknown length: speech not read")
        return media
    if duration > max_transcribe_s:
        media.warnings.append(f"longer than {int(max_transcribe_s)} s: speech not read")
        return media
    with tempfile.TemporaryDirectory(prefix="labkit-media-") as td:
        try:
            audio = rd.audio(target, Path(td))
        except MediaUnreadable as e:
            media.warnings.append(f"audio not downloaded ({e})")
            return media
        if audio is None:
            media.warnings.append("no audio in this post")
            return media
        try:
            text = (transcribe(audio) or "").strip()
        except Exception as e:                                   # a failed transcription is not fatal
            media.warnings.append(f"speech not transcribed ({e.__class__.__name__})")
            return media
    if text:
        media.transcript, media.transcript_source = text, "speech"
    else:
        media.warnings.append("no speech (music or silence)")
    return media


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m labkit.media", description=__doc__.splitlines()[0])
    ap.add_argument("url")
    args = ap.parse_args(argv)
    try:
        print(read_media(args.url).to_text())
    except MediaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
