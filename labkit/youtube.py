"""A YouTube video as text for LLM prompts: metadata, chapters, transcript and screenshots.

``fetch_video(url, out_dir)`` reads one video through yt-dlp (``labkit[youtube]``;
``imageio-ffmpeg`` brings a bundled ffmpeg when none is on PATH): title, channel,
publish date, duration, description, chapters and the transcript from manual or
automatic captions. It saves at most ``MAX_SCREENSHOTS`` timestamped JPEGs in
``out_dir``, chosen at chapter starts, scene changes, then even spacing. yt-dlp's
disk cache is off (``cachedir=False``): nothing is written to ``~/.cache``.
Everything temporary lives in a ``.work-*`` folder inside ``out_dir`` and is
deleted afterwards; files already in ``out_dir`` are never touched.

Share links are reduced to the canonical watch URL, so a playlist is never fetched:
only ``youtube.com``, ``youtu.be`` and ``youtube-nocookie.com`` video links are read
(``si``, ``t``, ``list`` and ``feature`` are ignored). Live streams raise
VideoUnreadable.

Usage: ``python -m labkit.youtube URL [--out DIR] [--screenshots N]``.
"""
from __future__ import annotations

import argparse
import html
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qs

from labkit.reddit import _parse
from labkit.web import truncate

log = logging.getLogger(__name__)
MAX_SCREENSHOTS = 10
DEFAULT_SCREENSHOTS = 8
MAX_DURATION_S = 2 * 3600          # longer videos: transcript only
MAX_VIDEO_BYTES = 400_000_000
SCENE_THRESHOLD = 0.3
SCENE_TIMEOUT_S = 600
FRAME_TIMEOUT_S = 60
CHUNK_S = 30                       # transcript_text groups cues into ~30 s lines
DESCRIPTION_CHARS = 3000
TRANSCRIPT_CHARS = 40000
VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov")
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
_TAG = re.compile(r"<[^>]+>")
_PTS = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
BASE_OPTS = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
             "cachedir": False, "socket_timeout": 30, "retries": 2}


class YoutubeError(Exception):
    """Base: not a video link, or the video could not be read."""


class NotYoutubeLink(YoutubeError):
    """Not a youtube.com / youtu.be video link."""


class VideoUnreadable(YoutubeError):
    """yt-dlp could not read it (private, removed, age or bot check, live, network)."""


@dataclass
class Chapter:
    start: float
    end: float | None
    title: str


@dataclass
class Cue:
    start: float
    text: str


@dataclass
class Screenshot:
    time: float      # seconds, one decimal
    path: Path
    reason: str      # "chapter" | "scene" | "even"


@dataclass
class Video:
    id: str
    url: str                 # canonical watch_url(id)
    title: str
    channel: str
    published: str           # "YYYY-MM-DD" or ""
    duration: float          # seconds, 0.0 if unknown
    description: str
    chapters: list[Chapter] = field(default_factory=list)
    transcript: list[Cue] = field(default_factory=list)
    transcript_source: str = ""      # "manual" | "auto" | ""
    transcript_lang: str = ""
    screenshots: list[Screenshot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def chapter_at(self, t: float) -> str:
        """The title of the last chapter starting at or before ``t`` ("" if none)."""
        title = ""
        for c in self.chapters:
            if c.start <= t:
                title = c.title
            else:
                break
        return title

    def transcript_text(self) -> str:
        """The cues as lines of ~CHUNK_S seconds each, tagged with their start time."""
        if not self.transcript:
            return ""
        lines: list[str] = []
        texts: list[str] = []
        line_start: float | None = None
        for c in self.transcript:
            if line_start is None or c.start - line_start >= CHUNK_S:
                if texts:
                    lines.append(f"[{clock(line_start)}] " + " ".join(texts))
                line_start, texts = c.start, [c.text]
            else:
                texts.append(c.text)
        if texts:
            lines.append(f"[{clock(line_start)}] " + " ".join(texts))
        return "\n".join(lines)

    def to_markdown(self, max_transcript_chars: int = TRANSCRIPT_CHARS) -> str:
        parts = [f"# {self.title}", "",
                 f"{self.channel} · {self.published or 'date unknown'} · {clock(self.duration)} · {self.url}",
                 "", "## Description", "",
                 truncate(self.description.strip(), DESCRIPTION_CHARS) or "(no description)"]
        if self.chapters:
            parts += ["", "## Chapters", ""] + [f"- {clock(c.start)} {c.title}" for c in self.chapters]
        if self.screenshots:
            parts += ["", "## Screenshots", ""]
            for s in self.screenshots:
                ch = self.chapter_at(s.time)
                reason = f"{s.reason}, {ch}" if ch else s.reason
                parts.append(f"- {clock(s.time)} {s.path.name} ({reason})")
        if self.transcript:
            parts += ["", f"## Transcript ({self.transcript_source} captions, {self.transcript_lang})", ""]
            full = self.transcript_text()
            text = truncate(full, max_transcript_chars)
            parts.append(text)
            if len(text) < len(full):
                parts.append(f"(transcript cut at {max_transcript_chars} characters)")
        else:
            parts += ["", "## Transcript", "", "(no captions)"]
        return "\n".join(parts)


def video_id(url: str) -> str | None:
    """The 11-character video id of a youtube.com / youtu.be / youtube-nocookie video link.

    Accepts watch URLs (``?v=``, share/playlist/t parameters ignored), ``shorts``,
    ``live``, ``embed`` and ``v`` paths, ``youtu.be/<id>`` and ``youtube-nocookie.com``
    embeds. ``notyoutube.com`` does not match."""
    u = _parse(url)
    if u.scheme not in ("http", "https"):
        return None
    host = (u.hostname or "").lower()
    candidate = ""
    if host in ("youtu.be", "www.youtu.be"):
        parts = [p for p in u.path.split("/") if p]
        candidate = parts[0] if parts else ""
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        path = u.path.rstrip("/")
        if path == "/watch":
            candidate = parse_qs(u.query).get("v", [""])[0]
        else:
            parts = [p for p in u.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
                candidate = parts[1]
    elif host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com"):
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "embed":
            candidate = parts[1]
    return candidate if _ID.match(candidate) else None


def is_youtube_url(url: str) -> bool:
    return video_id(url) is not None


def watch_url(vid: str) -> str:
    """The canonical watch URL for a video id — the only URL yt-dlp is given, so
    ``si``/``list`` parameters cannot pull a playlist in."""
    return f"https://www.youtube.com/watch?v={vid}"


def clock(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _lang_match(keys, lang: str) -> str | None:
    """The first key equal to ``lang``, else the first starting with ``lang + "-"``."""
    if not lang:
        return None
    for k in keys:
        if k == lang:
            return k
    for k in keys:
        if k.startswith(lang + "-"):
            return k
    return None


def pick_caption(info: dict, langs: tuple[str, ...] = ("en", "pt")) -> tuple[str, bool] | None:
    """``(track key, is_auto)`` for the caption track to fetch, or None.

    Manual tracks in ``langs`` first; then the original-language automatic track
    (a key ending in ``-orig``); then automatic tracks matching the video's own
    language; then automatic tracks in ``langs``; then any manual track."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    for lang in langs:
        k = _lang_match(manual, lang)
        if k:
            return (k, False)
    for k in auto:
        if k.endswith("-orig"):
            return (k, True)
    k = _lang_match(auto, info.get("language") or "")
    if k:
        return (k, True)
    for lang in langs:
        if lang in auto:
            return (lang, True)
    for k in manual:
        return (k, False)
    return None


def parse_vtt(text: str) -> list[Cue]:
    """A WebVTT file as cues. Tags and entities are removed, consecutive repeats
    dropped (YouTube's rolling automatic captions repeat the previous line)."""
    cues: list[Cue] = []
    start: float | None = None
    last = ""
    for line in text.splitlines():
        s = line.strip()
        if "-->" in s:
            m = _TIME.match(s.split("-->")[0])
            if m:
                h, mi, sec, ms = m.groups()
                start = (int(h) * 3600 if h else 0) + int(mi) * 60 + int(sec) + int(ms) / 1000
            else:
                start = None
        elif s == "":
            start = None
        elif start is not None:
            t = html.unescape(_TAG.sub("", s))
            t = " ".join(t.split())
            if t and t != last:
                cues.append(Cue(round(start, 3), t))
                last = t
    return cues


def _spread(items: list[float], k: int) -> list[float]:
    """At most ``k`` of ``items``, evenly by index (the middle one when k == 1)."""
    if len(items) <= k:
        return items
    if k == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]


def pick_times(duration: float, chapters: list[Chapter], scenes: list[float], n: int) -> list[tuple[float, str]]:
    """Screenshot times and reasons: chapter starts first, then scene changes,
    then even spacing; shots at least ``gap`` apart, at most ``n``. May return
    fewer than ``n`` when the video is too short to space them."""
    n = max(0, min(n, MAX_SCREENSHOTS))
    if n == 0 or duration <= 0:
        return []
    gap = max(2.0, duration / (n * 3))
    end = max(0.0, duration - 1.0)
    chosen: list[tuple[float, str]] = []

    def add(t: float, reason: str) -> None:
        t = round(min(max(t, 0.0), end), 1)
        if len(chosen) < n and all(abs(t - c) >= gap for c, _ in chosen):
            chosen.append((t, reason))

    chapter_times = [min(c.start + 2.0, (c.start + (c.end if c.end is not None else duration)) / 2)
                     for c in chapters]
    for t in _spread(chapter_times, n):
        add(t, "chapter")
    for t in _spread(sorted(scenes), n):
        add(t, "scene")
    for i in range(n):
        add(duration * (i + 0.5) / n, "even")
    return sorted(chosen)


Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def ffmpeg_exe() -> str | None:
    """ffmpeg from PATH, or the one bundled in ``imageio-ffmpeg`` (``labkit[youtube]``)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # optional: labkit[youtube]
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def parse_scene_times(stderr: str) -> list[float]:
    """The ``pts_time`` values ffmpeg's showinfo filter printed, in order."""
    return [float(m) for m in _PTS.findall(stderr)]


def detect_scenes(ffmpeg: str, video: Path, runner: Runner = _run) -> list[float] | None:
    """Scene-change times (seconds) from ffmpeg's scene filter; None on failure."""
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", str(video), "-an",
           "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo", "-f", "null", "-"]
    try:
        r = runner(cmd, SCENE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return parse_scene_times(r.stderr or "")


def grab_frame(ffmpeg: str, video: Path, t: float, path: Path, runner: Runner = _run) -> bool:
    """Save the frame at ``t`` as one JPEG; True only when a non-empty file appeared."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.1f}",
           "-i", str(video), "-frames:v", "1", "-q:v", "3", str(path)]
    try:
        r = runner(cmd, FRAME_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0 and path.is_file() and path.stat().st_size > 0


class Downloader(Protocol):
    def info(self, url: str) -> dict: ...

    def download(self, url: str, work: Path, *, caption: tuple[str, bool] | None,
                 video: bool, max_height: int) -> None: ...


class YtDlp:
    """yt-dlp behind two seams (``info``, ``download``), so tests never need it."""

    def __init__(self):
        try:
            import yt_dlp  # optional: labkit[youtube]
        except ImportError as e:
            raise ImportError("labkit.youtube needs yt-dlp: pip install 'labkit[youtube]'") from e
        self._yt_dlp = yt_dlp

    def info(self, url: str) -> dict:
        try:
            with self._yt_dlp.YoutubeDL(dict(BASE_OPTS)) as ydl:
                return ydl.sanitize_info(ydl.extract_info(url, download=False))
        except self._yt_dlp.utils.YoutubeDLError as e:
            raise VideoUnreadable(f"yt-dlp could not read {url}: {e}") from e

    def download(self, url: str, work: Path, *, caption: tuple[str, bool] | None,
                 video: bool, max_height: int) -> None:
        opts = {**BASE_OPTS, "outtmpl": str(work / "media.%(ext)s"), "skip_download": not video,
                "format": f"bv*[height<={max_height}][ext=mp4]/bv*[height<={max_height}]/b[height<={max_height}]/b",
                "max_filesize": MAX_VIDEO_BYTES,
                "writesubtitles": bool(caption) and not caption[1],
                "writeautomaticsub": bool(caption) and caption[1],
                "subtitleslangs": [caption[0]] if caption else [],
                "subtitlesformat": "vtt"}
        try:
            with self._yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
        except self._yt_dlp.utils.YoutubeDLError as e:
            raise VideoUnreadable(f"yt-dlp could not read {url}: {e}") from e


def fetch_video(url: str, out_dir: str | Path | None = None, *, screenshots: int = DEFAULT_SCREENSHOTS,
                langs: tuple[str, ...] = ("en", "pt"), max_height: int = 720,
                max_duration_s: float = MAX_DURATION_S, downloader: Downloader | None = None,
                runner: Runner | None = None, ffmpeg: str | None = None) -> Video:
    """Read one YouTube video: metadata, chapters, transcript and up to ``screenshots`` JPEGs.

    Raises NotYoutubeLink for links that are not videos and VideoUnreadable when yt-dlp
    cannot read the video itself; every other problem (no captions, no ffmpeg, a too-long
    or live video, a failed download) becomes a warning on the returned Video.
    ``out_dir=None`` means transcript only."""
    vid = video_id(url)
    if vid is None:
        raise NotYoutubeLink(f"not a YouTube video link: {url}")
    canonical = watch_url(vid)

    warnings: list[str] = []
    dl = downloader or YtDlp()   # an ImportError propagates: the caller must install the extra
    info = dl.info(canonical)
    if info.get("is_live"):
        raise VideoUnreadable(f"{canonical} is a live stream")

    title = info.get("title") or ""
    channel = info.get("channel") or info.get("uploader") or ""
    raw_date = str(info.get("upload_date") or "")
    published = (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
                 if len(raw_date) == 8 and raw_date.isdigit() else "")
    duration = float(info.get("duration") or 0)
    description = info.get("description") or ""
    chapters = [Chapter(float(c.get("start_time") or 0),
                        (float(c["end_time"]) if c.get("end_time") is not None else None),
                        c.get("title") or "") for c in info.get("chapters") or []]

    n = max(0, min(int(screenshots), MAX_SCREENSHOTS))
    want_video = out_dir is not None and n > 0
    exe: str | None = None
    if want_video:
        if duration <= 0:
            warnings.append("unknown duration: no screenshots")
            want_video = False
        elif duration > max_duration_s:
            warnings.append(f"video is longer than {clock(max_duration_s)}: no screenshots")
            want_video = False
        else:
            exe = ffmpeg or ffmpeg_exe()
            if exe is None:
                warnings.append("ffmpeg not found (install labkit[youtube] or put ffmpeg on PATH): no screenshots")
                want_video = False

    caption = pick_caption(info, langs)
    if caption is None:
        warnings.append("no captions on this video")

    video = Video(id=vid, url=canonical, title=title, channel=channel, published=published,
                  duration=duration, description=description, chapters=chapters)

    if caption is not None or want_video:
        if out_dir is not None:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.TemporaryDirectory(prefix=".work-", dir=out)
        else:
            tmp = tempfile.TemporaryDirectory(prefix="labkit-youtube-")
        with tmp as td_name:
            work = Path(td_name)

            try:
                dl.download(canonical, work, caption=caption, video=want_video, max_height=max_height)
            except VideoUnreadable as e:
                warnings.append(f"download failed: {e}")

            vtts = sorted(work.glob("*.vtt"))
            if caption and not vtts:
                warnings.append("captions could not be downloaded")
            elif vtts:
                cues = parse_vtt(vtts[0].read_text(encoding="utf-8", errors="replace"))
                if cues:
                    video.transcript = cues
                    video.transcript_source = "auto" if caption[1] else "manual"
                    video.transcript_lang = caption[0]
                else:
                    warnings.append("captions were empty")

            if want_video:
                video_file = None
                for p in sorted(work.iterdir()):
                    if p.is_file() and p.suffix.lower() in VIDEO_EXT and p.stat().st_size > 0:
                        video_file = p
                        break
                if video_file is None:
                    warnings.append("video could not be downloaded: no screenshots")
                else:
                    scenes: list[float] = []
                    if len(chapters) < n:
                        got = detect_scenes(exe, video_file, runner or _run)
                        if got is None:
                            warnings.append("scene detection failed: using chapters and even spacing")
                        else:
                            scenes = got
                    for i, (t, reason) in enumerate(pick_times(duration, chapters, scenes, n), 1):
                        path = Path(out_dir) / f"shot_{i:02d}_{int(t):05d}s.jpg"
                        if grab_frame(exe, video_file, t, path, runner or _run):
                            video.screenshots.append(Screenshot(t, path, reason))
                        else:
                            warnings.append(f"frame at {clock(t)} failed")

    video.warnings = warnings
    for w in warnings:
        log.warning("%s", w)
    return video


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m labkit.youtube")
    p.add_argument("url")
    p.add_argument("--out", default=None, help="folder for the screenshots (without it: transcript only)")
    p.add_argument("--screenshots", type=int, default=DEFAULT_SCREENSHOTS)
    p.add_argument("--lang", action="append", default=None, help="caption language to prefer (repeatable)")
    p.add_argument("--max-height", type=int, default=720)
    args = p.parse_args(argv)
    try:
        v = fetch_video(args.url, args.out, screenshots=args.screenshots,
                        langs=tuple(args.lang or ("en", "pt")), max_height=args.max_height)
    except ImportError as e:
        print(e, file=sys.stderr)
        return 2
    except YoutubeError as e:
        print(f"could not read {args.url}: {e}", file=sys.stderr)
        return 1

    print(f"video: {v.title}")
    print(f"channel: {v.channel} · published {v.published or '?'} · duration {clock(v.duration)}")
    print(f"url: {v.url}")
    print(f"description: {len(v.description)} chars")
    for line in [ln for ln in v.description.splitlines() if ln.strip()][:3]:
        print(f"  {line}")
    print(f"chapters: {len(v.chapters)}")
    for c in v.chapters[:5]:
        print(f"  {clock(c.start)} {c.title}")
    if v.transcript:
        text = v.transcript_text()
        print(f"transcript: {v.transcript_source} {v.transcript_lang}, {len(v.transcript)} cues, {len(text)} chars")
        for line in text.splitlines()[:5]:
            print(f"  {line}")
    else:
        print("transcript: none")
    print(f"screenshots: {len(v.screenshots)} in {args.out}")
    for s in v.screenshots:
        size = s.path.stat().st_size / 1024 if s.path.is_file() else 0.0
        print(f"  {clock(s.time)} {s.path.name} {size:.1f} KB ({s.reason})")
    print(f"warnings: {len(v.warnings)}")
    for w in v.warnings:
        print(f"  - {w}")

    if v.transcript or v.screenshots:
        return 0
    print("nothing read beyond metadata", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
