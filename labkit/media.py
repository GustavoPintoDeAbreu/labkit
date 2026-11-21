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

Usage: ``python -m labkit.media URL`` prints what it read (no transcript of speech).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from labkit.reddit import _parse
from labkit.web import truncate
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


def read_media(url: str, *, transcribe: Callable[[Path], str] | None = None,
               max_transcribe_s: float = MAX_TRANSCRIBE_S, reader: Reader | None = None) -> Media:
    """Read one video or reel: who posted it, when, its caption and what is said in it.

    ``transcribe`` turns an audio file into text; without it a clip's speech is not read (YouTube's
    captions still are). Raises NotMediaLink and MediaUnreadable (see the module docstring)."""
    site = media_site(url)
    if not site:
        raise NotMediaLink(f"not a video or reel link: {url}")
    if site == "YouTube":
        try:
            v = fetch_video(url, None) if reader is None else None
        except VideoUnreadable as e:
            raise MediaUnreadable(str(e)) from e
        if v is not None:
            return Media(url=v.url, site=site, author=v.channel, title=v.title, caption=v.description,
                         published=v.published, duration=v.duration, transcript=v.transcript_text(),
                         transcript_source="captions" if v.transcript else "", warnings=list(v.warnings))
    target = clean_url(url)
    rd = reader or YtDlpMedia()
    info = rd.info(target)
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
