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


# -- the post's preview when yt-dlp cannot read the video (0.19.0) -----------------------------------
import httpx  # noqa: E402

from labkit.media import post_preview  # noqa: E402


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_unreadable_video_falls_back_to_the_preview():
    seen = []

    def preview(site, url):
        seen.append((site, url))
        return Media(url=url, site=site, author="someone", title="", caption="the post's words")

    m = read_media("https://x.com/someone/status/123?s=46", reader=FakeReader(None), preview=preview)
    assert seen == [("X", "https://x.com/someone/status/123")]
    assert m.caption == "the post's words" and "could not be read" in m.warnings[0]
    assert "the post's words" in m.to_text()


def test_no_preview_still_raises():
    with pytest.raises(MediaUnreadable):
        read_media("https://x.com/someone/status/123", reader=FakeReader(None), preview=lambda s, u: None)


def test_x_post_without_video_reads_its_text():
    def handler(request):
        assert request.url.host == "cdn.syndication.twimg.com" and request.url.params["id"] == "20"
        return httpx.Response(200, json={"__typename": "Tweet", "text": "just setting up my twttr",
                                         "created_at": "2006-03-21T20:50:14.000Z", "favorite_count": 5,
                                         "user": {"name": "jack", "screen_name": "jack"},
                                         "mediaDetails": [{"type": "photo"}]})
    m = post_preview("X", "https://x.com/jack/status/20?s=20", _client(handler))
    assert (m.author, m.published, m.likes) == ("jack", "2006-03-21", 5)
    assert m.caption.startswith("just setting up my twttr") and "(attached: photo)" in m.caption


def test_x_deleted_post_is_none():
    m = post_preview("X", "https://x.com/a/status/9", _client(
        lambda r: httpx.Response(200, json={"__typename": "TweetTombstone"})))
    assert m is None


def test_tiktok_oembed_title_is_the_caption():
    def handler(request):
        assert request.url.host == "www.tiktok.com" and request.url.path == "/oembed"
        return httpx.Response(200, json={"title": "GTA VI is out #gta6", "author_name": "rockstargames"})
    m = post_preview("TikTok", "https://www.tiktok.com/@r/video/77?_t=x", _client(handler))
    assert (m.caption, m.author, m.title) == ("GTA VI is out #gta6", "rockstargames", "")


def test_youtube_oembed_404_is_none():
    # a made-up video id (2026-11-22: the model invented one)
    m = post_preview("YouTube", "https://youtu.be/24Z58zY7v9k", _client(lambda r: httpx.Response(404)))
    assert m is None


def test_instagram_preview_from_crawler_meta(monkeypatch):
    monkeypatch.setattr("labkit.web.public_url", lambda url: True)
    html = ('<html><head><meta property="og:title" content="Lontizzle on Instagram: &quot;#gta6 secret&quot;" />'
            '<meta property="og:description" content="91K likes, 666 comments - lontizzle_yaps on November 21, '
            '2026: &quot;Where to fight Satan #gta6&quot;. " /></head></html>')

    def handler(request):
        assert request.headers["user-agent"] == media.CRAWLER_UA
        return httpx.Response(200, html=html)
    m = post_preview("Instagram", "https://www.instagram.com/reel/DgB/?igsh=x", _client(handler))
    assert (m.author, m.published, m.caption) == ("lontizzle_yaps", "2026-11-21", "Where to fight Satan #gta6")


def test_instagram_login_page_is_none(monkeypatch):
    monkeypatch.setattr("labkit.web.public_url", lambda url: True)
    m = post_preview("Instagram", "https://www.instagram.com/p/x/", _client(
        lambda r: httpx.Response(200, html="<html><head><title>Instagram</title></head></html>")))
    assert m is None
