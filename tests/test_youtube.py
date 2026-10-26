import subprocess
import sys
from pathlib import Path

import pytest

import labkit.youtube as youtube
from labkit.youtube import (Cue, Chapter, Screenshot, Video, NotYoutubeLink, VideoUnreadable,
                            MAX_VIDEO_BYTES, fetch_video, is_youtube_url, main, pick_caption,
                            pick_times, parse_vtt, parse_scene_times, video_id, watch_url, clock,
                            YtDlp, ffmpeg_exe)

VID = "abcDEF12345"


def info(**over):
    d = {"id": VID, "title": "I built a site with Next.js", "channel": "Some Dev", "uploader": "somedev",
         "upload_date": "20260901", "duration": 600, "description": "Stack: Next.js, Tailwind\nLinks below",
         "chapters": [{"start_time": 0.0, "end_time": 120.0, "title": "Intro"},
                      {"start_time": 120.0, "end_time": 600.0, "title": "Build"}],
         "subtitles": {}, "automatic_captions": {"en": [{"ext": "vtt", "url": "x"}], "fr": [{"ext": "vtt", "url": "y"}]},
         "language": "en", "is_live": False}
    d.update(over)
    return d


VTT_AUTO = (
    "WEBVTT\n"
    "Kind: captions\n"
    "Language: en\n"
    "\n"
    "00:00:00.000 --> 00:00:02.000 align:start position:0%\n"
    "hello<00:00:00.500><c> world</c>\n"
    "\n"
    "00:00:02.000 --> 00:00:02.010 align:start position:0%\n"
    "hello world\n"
    "\n"
    "00:00:02.010 --> 00:00:05.000 align:start position:0%\n"
    "hello world\n"
    "this is<00:00:03.000><c> Next.js</c>\n"
    "\n"
    "00:00:35.000 --> 00:00:37.000\n"
    "and Tailwind &amp; shadcn\n"
)


class FakeDownloader:
    def __init__(self, info, vtt=VTT_AUTO, make_video=True):
        self.info_ = info
        self.vtt = vtt
        self.make_video = make_video
        self.calls = []

    def info(self, url):
        self.calls.append(("info", url))
        return self.info_

    def download(self, url, work, *, caption, video, max_height):
        self.calls.append(("download", url, caption, video, max_height))
        if caption and self.vtt:
            (Path(work) / f"media.{caption[0]}.vtt").write_text(self.vtt)
        if video and self.make_video:
            (Path(work) / "media.mp4").write_bytes(b"fake")


def fake_runner(scene_stderr="", fail_at=None):
    calls = []

    def run(cmd, timeout):
        calls.append(cmd)
        if "-frames:v" in cmd:
            if cmd[cmd.index("-ss") + 1] == fail_at:
                return subprocess.CompletedProcess(cmd, 1, "", "err")
            Path(cmd[-1]).write_bytes(b"\xff\xd8jpeg")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", scene_stderr)

    return run, calls


SCENES = ("[Parsed_showinfo_1 @ 0x55d0] n:   0 pts: 300500 pts_time:300.5   duration:1000\n"
          "[Parsed_showinfo_1 @ 0x55d0] n:   1 pts: 450000 pts_time:450     duration:1000\n")


def full_fetch(tmp_path):
    """The whole pipeline on fakes: returns (Video, FakeDownloader)."""
    dl = FakeDownloader(info())
    run, _calls = fake_runner(SCENES)
    v = fetch_video("https://youtu.be/abcDEF12345?si=zzz", tmp_path / "shots", screenshots=4,
                    downloader=dl, runner=run, ffmpeg="ffmpeg")
    return v, dl


# --- links --------------------------------------------------------------------

@pytest.mark.parametrize("url,want", [
    ("https://youtu.be/HzL65tTeANs?si=NPoc30kQRrrogcSf", "HzL65tTeANs"),
    ("https://www.youtube.com/watch?v=z2K0nXa-dUg", "z2K0nXa-dUg"),
    ("youtube.com/watch?v=z2K0nXa-dUg&t=42s&list=PL123", "z2K0nXa-dUg"),
    ("https://m.youtube.com/watch?feature=share&v=z2K0nXa-dUg", "z2K0nXa-dUg"),
    ("https://music.youtube.com/watch?v=z2K0nXa-dUg", "z2K0nXa-dUg"),
    ("https://www.youtube.com/shorts/z2K0nXa-dUg?si=x", "z2K0nXa-dUg"),
    ("https://www.youtube.com/live/z2K0nXa-dUg", "z2K0nXa-dUg"),
    ("https://www.youtube.com/embed/z2K0nXa-dUg", "z2K0nXa-dUg"),
    ("https://www.youtube-nocookie.com/embed/z2K0nXa-dUg", "z2K0nXa-dUg"),
    ("  https://youtu.be/z2K0nXa-dUg  ", "z2K0nXa-dUg"),
])
def test_video_id_accepts(url, want):
    assert video_id(url) == want


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/@somechannel", "https://www.youtube.com/playlist?list=PL1",
    "https://www.youtube.com/watch?v=short", "https://youtu.be/",
    "https://notyoutube.com/watch?v=z2K0nXa-dUg", "https://evil.com/youtu.be/z2K0nXa-dUg",
    "https://www.reddit.com/r/x/comments/abc/", "ftp://youtu.be/z2K0nXa-dUg",
])
def test_video_id_rejects(url):
    assert video_id(url) is None
    assert is_youtube_url(url) is False


def test_watch_url_and_clock():
    assert watch_url(VID) == "https://www.youtube.com/watch?v=abcDEF12345"
    assert clock(0) == "0:00"
    assert clock(75) == "1:15"
    assert clock(3725) == "1:02:05"


# --- captions -----------------------------------------------------------------

def test_pick_caption_order():
    assert pick_caption({"subtitles": {"en-US": [1]}, "automatic_captions": {"en": [1]}}) == ("en-US", False)
    assert pick_caption({"subtitles": {"pt-PT": [1]}}) == ("pt-PT", False)
    assert pick_caption({"subtitles": {"live_chat": [1]}, "automatic_captions": {"en": [1]}}) == ("en", True)
    assert pick_caption({"subtitles": {"de": [1]}, "automatic_captions": {"en": [1]}}) == ("en", True)
    assert pick_caption({"subtitles": {"de": [1]}}) == ("de", False)
    assert pick_caption({"automatic_captions": {"pt-orig": [1], "en": [1]}, "language": "pt"}) == ("pt-orig", True)
    assert pick_caption({"automatic_captions": {"fr": [1], "pt": [1], "en": [1]}, "language": "pt"}) == ("pt", True)
    assert pick_caption({}) is None


def test_parse_vtt_dedupes_rolling_auto_captions():
    assert parse_vtt(VTT_AUTO) == [Cue(0.0, "hello world"), Cue(2.01, "this is Next.js"),
                                   Cue(35.0, "and Tailwind & shadcn")]


def test_parse_vtt_manual_with_ids_and_hours():
    text = ("WEBVTT\n\n1\n01:00:05.250 --> 01:00:07.000\nFirst line\n\n2\n"
            "01:00:08,000 --> 01:00:09.000\nSecond\n")
    assert parse_vtt(text) == [Cue(3605.25, "First line"), Cue(3608.0, "Second")]
    assert parse_vtt("WEBVTT\n") == []


def test_parse_scene_times():
    assert parse_scene_times(SCENES) == [300.5, 450.0]
    assert parse_scene_times("no frames") == []


# --- screenshot times ---------------------------------------------------------

def test_pick_times_even_only():
    assert pick_times(100, [], [], 4) == [(12.5, "even"), (37.5, "even"), (62.5, "even"), (87.5, "even")]


def test_pick_times_chapters():
    chapters = [Chapter(0, 30, "a"), Chapter(30, 60, "b"), Chapter(60, 100, "c")]
    assert pick_times(100, chapters, [], 3) == [(2.0, "chapter"), (32.0, "chapter"), (62.0, "chapter")]


def test_pick_times_spreads_many_chapters():
    starts = [0, 20, 40, 60, 80]
    ends = starts[1:] + [100]
    chapters = [Chapter(s, e, f"c{i}") for i, (s, e) in enumerate(zip(starts, ends))]
    assert pick_times(100, chapters, [], 3) == [(2.0, "chapter"), (42.0, "chapter"), (82.0, "chapter")]


def test_pick_times_scenes_fill_and_respect_gap():
    chapters = [Chapter(0, 50, "a"), Chapter(50, 100, "b")]
    assert pick_times(100, chapters, [3.0, 20.0, 51.0, 75.0], 4) == [
        (2.0, "chapter"), (20.0, "scene"), (52.0, "chapter"), (75.0, "scene")]


def test_pick_times_even_fill():
    assert pick_times(100, [], [50.0], 3) == [(16.7, "even"), (50.0, "scene"), (83.3, "even")]


def test_pick_times_clamps():
    times = pick_times(1000, [], [], 50)
    assert len(times) == 10
    assert times[0] == (50.0, "even") and times[-1] == (950.0, "even")
    assert pick_times(100, [], [], 0) == []
    assert pick_times(0, [], [], 5) == []
    chapters = [Chapter(0, 99, "a"), Chapter(99, 100, "b")]
    assert pick_times(100, chapters, [], 2) == [(2.0, "chapter"), (99.0, "chapter")]


# --- fetch_video --------------------------------------------------------------

def test_fetch_video_full(tmp_path):
    v, dl = full_fetch(tmp_path)
    assert v.title == "I built a site with Next.js"
    assert v.channel == "Some Dev"
    assert v.published == "2026-09-01"
    assert v.duration == 600.0
    assert v.chapters[0] == Chapter(0.0, 120.0, "Intro")
    assert v.transcript_source == "auto"
    assert v.transcript_lang == "en"
    assert v.transcript == [Cue(0.0, "hello world"), Cue(2.01, "this is Next.js"),
                            Cue(35.0, "and Tailwind & shadcn")]
    assert ([(s.time, s.reason, s.path.name) for s in v.screenshots] ==
            [(2.0, "chapter", "shot_01_00002s.jpg"), (122.0, "chapter", "shot_02_00122s.jpg"),
             (300.5, "scene", "shot_03_00300s.jpg"), (450.0, "scene", "shot_04_00450s.jpg")])
    assert all(s.path.is_file() for s in v.screenshots)
    assert not [p for p in (tmp_path / "shots").iterdir() if p.name.startswith(".work-")]
    canonical = "https://www.youtube.com/watch?v=abcDEF12345"
    assert dl.calls == [("info", canonical), ("download", canonical, ("en", True), True, 720)]
    assert v.warnings == []


def test_fetch_video_transcript_only_without_out_dir(tmp_path):
    run, calls = fake_runner(SCENES)
    dl = FakeDownloader(info())
    v = fetch_video("https://youtu.be/abcDEF12345", None, screenshots=4, downloader=dl, runner=run,
                    ffmpeg="ffmpeg")
    assert [c for c in dl.calls if c[0] == "download"][0][3] is False
    assert calls == []
    assert v.screenshots == []
    assert len(v.transcript) == 3


def test_fetch_video_zero_screenshots(tmp_path):
    dl = FakeDownloader(info())
    run, _calls = fake_runner(SCENES)
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", screenshots=0,
                    downloader=dl, runner=run, ffmpeg="ffmpeg")
    assert [c for c in dl.calls if c[0] == "download"][0][3] is False
    assert v.screenshots == []


def test_fetch_video_without_ffmpeg_keeps_transcript(monkeypatch, tmp_path):
    monkeypatch.setattr("labkit.youtube.ffmpeg_exe", lambda: None)
    dl = FakeDownloader(info())
    run, _calls = fake_runner(SCENES)
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", downloader=dl, runner=run)
    assert any("ffmpeg" in w for w in v.warnings)
    assert [c for c in dl.calls if c[0] == "download"][0][3] is False
    assert v.screenshots == []
    assert len(v.transcript) == 3


def test_fetch_video_too_long_skips_video(tmp_path):
    run, _calls = fake_runner(SCENES)
    dl = FakeDownloader(info(duration=10000))
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", downloader=dl, runner=run,
                    ffmpeg="ffmpeg")
    assert any("longer than 2:00:00" in w for w in v.warnings)
    assert [c for c in dl.calls if c[0] == "download"][0][3] is False


def test_fetch_video_no_captions(tmp_path):
    run, _calls = fake_runner(SCENES)
    dl = FakeDownloader(info(automatic_captions={}))
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", screenshots=4, downloader=dl, runner=run,
                    ffmpeg="ffmpeg")
    assert v.transcript == []
    assert v.transcript_source == ""
    assert "no captions on this video" in v.warnings
    assert [c for c in dl.calls if c[0] == "download"][0][2] is None
    assert len(v.screenshots) == 4


def test_fetch_video_no_captions_no_out_dir_skips_download(tmp_path):
    dl = FakeDownloader(info(automatic_captions={}))
    v = fetch_video("https://youtu.be/abcDEF12345", None, downloader=dl)
    assert [c[0] for c in dl.calls] == ["info"]


def test_fetch_video_video_not_downloaded(tmp_path):
    run, _calls = fake_runner(SCENES)
    dl = FakeDownloader(info(), make_video=False)
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", downloader=dl, runner=run,
                    ffmpeg="ffmpeg")
    assert v.screenshots == []
    assert any("video could not be downloaded" in w for w in v.warnings)
    assert len(v.transcript) == 3


def test_fetch_video_frame_failure_skipped(tmp_path):
    run, _calls = fake_runner(SCENES, fail_at="122.0")
    dl = FakeDownloader(info())
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", screenshots=4,
                    downloader=dl, runner=run, ffmpeg="ffmpeg")
    assert [(s.time, s.reason) for s in v.screenshots] == [(2.0, "chapter"), (300.5, "scene"), (450.0, "scene")]
    assert "frame at 2:02 failed" in v.warnings


def test_scene_pass_skipped_when_chapters_cover(tmp_path):
    chapters = [{"start_time": float(i * 75), "end_time": float((i + 1) * 75), "title": f"c{i}"}
                for i in range(8)]
    run, calls = fake_runner(SCENES)
    dl = FakeDownloader(info(chapters=chapters))
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", screenshots=4,
                    downloader=dl, runner=run, ffmpeg="ffmpeg")
    assert not any("null" in cmd for cmd in calls)
    assert len(v.screenshots) == 4
    assert all(s.reason == "chapter" for s in v.screenshots)


def test_scene_detection_failure_falls_back(tmp_path):
    def run(cmd, timeout):
        if "null" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        Path(cmd[-1]).write_bytes(b"\xff\xd8jpeg")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    dl = FakeDownloader(info())
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", screenshots=4,
                    downloader=dl, runner=run, ffmpeg="ffmpeg")
    assert any("scene detection failed" in w for w in v.warnings)
    reasons = [s.reason for s in v.screenshots]
    assert set(reasons) <= {"chapter", "even"}


def test_fetch_video_screenshots_capped_at_10(tmp_path):
    run, _calls = fake_runner("")
    dl = FakeDownloader(info(chapters=[]))
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", screenshots=50,
                    downloader=dl, runner=run, ffmpeg="ffmpeg")
    assert len(v.screenshots) == 10
    assert v.screenshots[0].time == 30.0 and v.screenshots[-1].time == 570.0
    assert all(s.reason == "even" for s in v.screenshots)


def test_fetch_video_rejects_non_youtube():
    dl = FakeDownloader(info())
    with pytest.raises(NotYoutubeLink):
        fetch_video("https://example.com/watch?v=abcDEF12345", downloader=dl)
    assert dl.calls == []


def test_fetch_video_live_raises():
    dl = FakeDownloader(info(is_live=True))
    with pytest.raises(VideoUnreadable):
        fetch_video("https://youtu.be/abcDEF12345", downloader=dl)


def test_fetch_video_download_error_is_a_warning(tmp_path):
    class Boom(FakeDownloader):
        def download(self, url, work, *, caption, video, max_height):
            raise VideoUnreadable("HTTP 403")

    run, _calls = fake_runner(SCENES)
    dl = Boom(info())
    v = fetch_video("https://youtu.be/abcDEF12345", tmp_path / "shots", downloader=dl, runner=run,
                    ffmpeg="ffmpeg")
    assert any("download failed: HTTP 403" in w for w in v.warnings)
    assert v.transcript == []
    assert "captions could not be downloaded" in v.warnings


# --- rendering ------------------------------------------------------------------

def test_to_markdown(tmp_path):
    v, _dl = full_fetch(tmp_path)
    md = v.to_markdown()
    assert md.startswith("# I built a site with Next.js")
    assert "Some Dev · 2026-09-01 · 10:00 · https://www.youtube.com/watch?v=abcDEF12345" in md
    assert "- 0:00 Intro" in md
    assert "- 2:00 Build" in md
    assert "- 0:02 shot_01_00002s.jpg (chapter, Intro)" in md
    assert "- 5:00 shot_03_00300s.jpg (scene, Build)" in md
    assert "## Transcript (auto captions, en)" in md
    assert "[0:00] hello world this is Next.js" in md
    assert "[0:35] and Tailwind & shadcn" in md


def test_to_markdown_caps_and_empty():
    cues = [Cue(float(i), "word word word.") for i in range(200)]
    v = Video(id=VID, url=watch_url(VID), title="T", channel="C", published="", duration=200.0,
              description="", transcript=cues, transcript_source="auto", transcript_lang="en")
    assert "(transcript cut at 300 characters)" in v.to_markdown(max_transcript_chars=300)

    empty = Video(id=VID, url=watch_url(VID), title="T", channel="C", published="", duration=0.0,
                  description="")
    md = empty.to_markdown()
    assert "(no captions)" in md
    assert "(no description)" in md
    assert "## Chapters" not in md
    assert "## Screenshots" not in md


# --- seams ----------------------------------------------------------------------

def test_ytdlp_missing_raises_helpful_importerror(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    with pytest.raises(ImportError, match=r"labkit\[youtube\]"):
        YtDlp()


def test_ytdlp_options(monkeypatch, tmp_path):
    import types

    class FakeErr(Exception):
        pass

    class YoutubeDL:
        seen: list = []
        raise_with = None

        def __init__(self, opts):
            self.opts = opts
            self.url = None
            self.download = None
            YoutubeDL.seen.append(self)

        def extract_info(self, url, download=False):
            if YoutubeDL.raise_with is not None:
                raise FakeErr(YoutubeDL.raise_with)
            self.url, self.download = url, download
            return {"id": VID}

        def sanitize_info(self, i):
            return i

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YoutubeDL
    mod.utils = types.SimpleNamespace(YoutubeDLError=FakeErr)
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)

    dl = YtDlp()
    canonical = watch_url(VID)

    dl.info(canonical)
    inst = YoutubeDL.seen[-1]
    assert inst.opts["cachedir"] is False and inst.opts["noplaylist"] is True and inst.opts["quiet"] is True
    assert inst.download is False

    dl.download(canonical, tmp_path, caption=("en", True), video=True, max_height=720)
    inst = YoutubeDL.seen[-1]
    assert inst.opts["writeautomaticsub"] is True
    assert inst.opts["writesubtitles"] is False
    assert inst.opts["subtitleslangs"] == ["en"]
    assert inst.opts["subtitlesformat"] == "vtt"
    assert inst.opts["skip_download"] is False
    assert inst.opts["format"] == "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/b"
    assert inst.opts["outtmpl"] == str(tmp_path / "media.%(ext)s")
    assert inst.opts["max_filesize"] == MAX_VIDEO_BYTES
    assert inst.opts["cachedir"] is False

    dl.download(canonical, tmp_path, caption=None, video=False, max_height=720)
    inst = YoutubeDL.seen[-1]
    assert inst.opts["skip_download"] is True
    assert inst.opts["writesubtitles"] is False and inst.opts["writeautomaticsub"] is False

    YoutubeDL.raise_with = "Sign in to confirm"
    with pytest.raises(VideoUnreadable, match="Sign in"):
        dl.info(canonical)


def test_ffmpeg_exe_none_when_missing(monkeypatch):
    monkeypatch.setattr("labkit.youtube.shutil.which", lambda name: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    assert ffmpeg_exe() is None


# --- cli ------------------------------------------------------------------------

def test_cli_prints_summary(monkeypatch, tmp_path, capsys):
    v, _dl = full_fetch(tmp_path)
    monkeypatch.setattr(youtube, "fetch_video", lambda *a, **k: v)
    rc = main(["https://youtu.be/abcDEF12345", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "video: I built a site with Next.js" in out
    assert "transcript: auto en, 3 cues" in out
    assert "screenshots: 4" in out
    assert "[0:00] hello world" in out


def test_cli_errors(monkeypatch, capsys):
    def boom(url, *a, **k):
        raise VideoUnreadable("Sign in")

    monkeypatch.setattr(youtube, "fetch_video", boom)
    rc = main(["https://youtu.be/abcDEF12345"])
    err = capsys.readouterr().err
    assert rc == 1 and "could not read" in err

    def boom_import(url, *a, **k):
        raise ImportError("labkit.youtube needs yt-dlp: pip install 'labkit[youtube]'")

    monkeypatch.setattr(youtube, "fetch_video", boom_import)
    assert main(["https://youtu.be/abcDEF12345"]) == 2

    quiet = Video(id=VID, url=watch_url(VID), title="T", channel="C", published="", duration=0.0,
                  description="d")
    monkeypatch.setattr(youtube, "fetch_video", lambda *a, **k: quiet)
    assert main(["https://youtu.be/abcDEF12345"]) == 1
    assert "nothing read beyond metadata" in capsys.readouterr().err


