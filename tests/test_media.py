"""labkit.media: which links are videos or reels, and what a read returns (a fake yt-dlp)."""
from pathlib import Path

import pytest

from labkit import media
from labkit.media import Media, MediaUnreadable, NotMediaLink, clean_url, media_site, read_media


@pytest.mark.parametrize("url,site", [
    ("https://www.instagram.com/reel/Df-xh3CvWPd/?fqnt=MTcxZTltZW43eWpscw==", "Instagram"),
    ("https://instagram.com/p/Cabc123/", "Instagram"),
    ("https://www.tiktok.com/@someone/video/7312345678901234567?_t=8abc", "TikTok"),
    ("https://vm.tiktok.com/ZMabcdef/", "TikTok"),
    ("https://x.com/someone/status/1790000000000000000?s=46", "X"),
    ("https://twitter.com/someone/status/1790000000000000000", "X"),
    ("https://www.facebook.com/reel/123456789", "Facebook"),
    ("https://fb.watch/abcDEF/", "Facebook"),
    ("https://vimeo.com/76979871", "Vimeo"),
    ("https://youtu.be/vMiyYb5JcpQ?is=6wjn3RliUkeJL6sv", "YouTube"),
    ("https://www.youtube.com/shorts/vMiyYb5JcpQ", "YouTube"),
])
def test_media_links(url, site):
    assert media_site(url) == site


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/someone/",          # a profile, not a post
    "https://x.com/someone",
    "https://notinstagram.com/reel/abc/",
    "https://www.bbc.com/news/article",
    "ftp://instagram.com/reel/abc/",
    "https://vimeo.com/channels/staffpicks",
])
def test_not_media_links(url):
    assert media_site(url) == ""
    with pytest.raises(NotMediaLink):
        read_media(url, reader=FakeReader({}))


def test_clean_url_drops_share_tracking():
    assert clean_url("https://www.instagram.com/reel/Df-xh3CvWPd/?fqnt=MTcx==") == "https://www.instagram.com/reel/Df-xh3CvWPd/"
    assert clean_url("https://youtu.be/vMiyYb5JcpQ?is=6wj") == "https://www.youtube.com/watch?v=vMiyYb5JcpQ"
    assert clean_url("https://www.facebook.com/watch?v=123") == "https://www.facebook.com/watch?v=123"


class FakeReader:
    def __init__(self, info: dict, audio: bytes | None = b"OggS", fail_audio: bool = False):
        self._info, self._audio, self._fail = info, audio, fail_audio
        self.urls: list[str] = []

    def info(self, url: str) -> dict:
        self.urls.append(url)
        if self._info is None:
            raise MediaUnreadable("private")
        return self._info

    def audio(self, url: str, work: Path) -> Path | None:
        if self._fail:
            raise MediaUnreadable("403")
        if self._audio is None:
            return None
        p = work / "audio.m4a"
        p.write_bytes(self._audio)
        return p


REEL = {"title": "Video by aniljonezclips", "description": "BRABUS Bodo Cabrio jetzt schon in GTA6 #gta6",
        "uploader": "AnilJonez", "timestamp": 1795215234, "duration": 31.5, "like_count": 19622}


def test_reel_caption_and_speech():
    seen = []

    def transcribe(path: Path) -> str:
        seen.append(path.read_bytes())
        return "look at this car, it is already in the game"

    rd = FakeReader(REEL)
    m = read_media("https://www.instagram.com/reel/Df-xh3CvWPd/?fqnt=x", transcribe=transcribe, reader=rd)
    assert rd.urls == ["https://www.instagram.com/reel/Df-xh3CvWPd/"]
    assert (m.site, m.author, m.published, m.likes) == ("Instagram", "AnilJonez", "2026-11-20", 19622)
    assert m.transcript_source == "speech" and "already in the game" in m.transcript
    assert seen == [b"OggS"]
    text = m.to_text()
    assert "Instagram · by AnilJonez" in text and "BRABUS Bodo Cabrio" in text
    assert "Video by" not in text                  # the generic title says nothing
    assert "speech, transcribed" in text


def test_no_transcriber_reads_caption_only():
    m = read_media("https://www.instagram.com/reel/abc/", reader=FakeReader(REEL))
    assert m.transcript == "" and "What is said: (nothing read)" in m.to_text()


def test_long_clip_and_silence_and_audio_failure_are_warnings():
    long_one = read_media("https://vimeo.com/1", transcribe=lambda p: "x", reader=FakeReader({**REEL, "duration": 900}))
    assert "longer than 180 s" in long_one.warnings[0]
    quiet = read_media("https://www.instagram.com/reel/abc/", transcribe=lambda p: "  ", reader=FakeReader(REEL))
    assert quiet.warnings == ["no speech (music or silence)"]
    blocked = read_media("https://www.instagram.com/reel/abc/", transcribe=lambda p: "x",
                         reader=FakeReader(REEL, fail_audio=True))
    assert blocked.warnings[0].startswith("audio not downloaded")

    def boom(_):
        raise RuntimeError("whisper")
    broken = read_media("https://www.instagram.com/reel/abc/", transcribe=boom, reader=FakeReader(REEL))
    assert broken.warnings == ["speech not transcribed (RuntimeError)"]


def test_unreadable_post_raises():
    with pytest.raises(MediaUnreadable):
        read_media("https://www.instagram.com/reel/abc/", reader=FakeReader(None))


def test_carousel_takes_its_first_video():
    info = {"_type": "playlist", "description": "two clips", "entries": [{"duration": 12, "uploader": "a"}]}
    m = read_media("https://www.instagram.com/p/abc/", reader=FakeReader(info))
    assert (m.duration, m.author, m.caption) == (12, "a", "two clips")


def test_to_text_cuts_long_parts():
    m = Media(url="u", site="TikTok", author="", title="", caption="c" * 5000, transcript="t" * 9000,
              transcript_source="captions")
    text = m.to_text(caption_chars=100, transcript_chars=200)
    assert len(text) < 600 and "its captions" in text


def test_cli_error(capsys):
    assert media.main(["https://example.com/page"]) == 1
    assert "not a video or reel link" in capsys.readouterr().err
