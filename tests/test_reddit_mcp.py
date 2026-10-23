import re

import httpx

from labkit import reddit_mcp
from labkit.reddit import RedditBlocked
from labkit.reddit_mcp import SharedPaceRSS, fetch_image, image_url, read_post

from test_reddit import FEED, LINK_FEED, LINK_PERMALINK, PERMALINK, entry, feed, md

IMAGE_PERMALINK = "https://www.reddit.com/r/comfyui/comments/img789/bf16/"
IMAGE_HTML = ('<table> <tr><td> ' + md('<p>Repo: <a href="https://github.com/example/unlock">github</a></p>')
              + ' <span><a href="https://i.redd.it/shot.png">[link]</a></span> &#32;'
              f' <span><a href="{IMAGE_PERMALINK}">[comments]</a></span> </td></tr></table>')
IMAGE_FEED = feed(entry("t3_img789", "furkan", IMAGE_PERMALINK, IMAGE_HTML, title="BF16 2x", sub="comfyui"))


class Wall:
    def __init__(self):
        self.t, self.sleeps = 1_000_000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def rss(tmp_path, responses, wall=None, seen=None):
    it = iter(responses)

    def handler(req):
        if seen is not None:
            seen.append(str(req.url))
        status, text = next(it)
        return httpx.Response(status, text=text)

    wall = wall or Wall()
    http = httpx.Client(base_url="https://www.reddit.com", transport=httpx.MockTransport(handler))
    return SharedPaceRSS("test-ua", tmp_path / "last", wall=wall.now, sleep=wall.sleep, client=http), wall


def test_markdown_has_post_comments_and_linked_pages(tmp_path):
    r, _ = rss(tmp_path, [(200, FEED)])
    fetched = []

    def page(url):
        fetched.append(url)
        return f"text of {url}"

    out = read_post(PERMALINK, 8, 2, rss=r, fetch_page=page, cache_dir=tmp_path)
    text = out["text"]
    assert text.startswith("# I built a tiny agent")
    assert "r/LocalLLaMA · u/alice · 2026-10-01" in text
    assert "Code is on GitHub" in text
    assert "- u/bob: The paper behind it" in text and "AutoModerator" not in text
    assert "- https://github.com/example/tiny-agent" in text
    assert fetched == ["https://github.com/example/tiny-agent", "https://blog.example.com/tiny-agent"]
    assert "## Linked page: https://github.com/example/tiny-agent\n\ntext of https://github.com" in text
    assert out["image_url"] is None


def test_link_post_reads_its_target_first_and_failed_pages_cost_no_slot(tmp_path):
    r, _ = rss(tmp_path, [(200, LINK_FEED)])
    out = read_post(LINK_PERMALINK, 8, 1, rss=r, cache_dir=tmp_path,
                    fetch_page=lambda u: None if "example.org" in u else "archive text")
    assert "The post's link: https://example.org/news/story" in out["text"]
    assert "## Linked page: https://archive.example.net/story" in out["text"]


def test_linked_pages_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(reddit_mcp, "PAGE_CHARS", 100)
    monkeypatch.setattr(reddit_mcp, "PAGES_CHARS", 150)
    r, _ = rss(tmp_path, [(200, FEED)])
    out = read_post(PERMALINK, 8, 3, rss=r, cache_dir=tmp_path, fetch_page=lambda u: "x" * 500)
    pages = out["text"].split("## Linked page: ")[1:]
    assert len(pages) == 2                                  # the budget runs out after two
    first, second = [len(max(re.findall("x{5,}", p), key=len)) for p in pages]
    assert first == 100 and second < 50                    # the rest of the 150-char budget


def test_image_post_gives_its_picture_url(tmp_path):
    r, _ = rss(tmp_path, [(200, IMAGE_FEED)])
    out = read_post(IMAGE_PERMALINK, 8, 0, rss=r, cache_dir=tmp_path, fetch_page=lambda u: None)
    assert out["image_url"] == "https://i.redd.it/shot.png"
    assert "The post's image: https://i.redd.it/shot.png" in out["text"]


def test_refusal_raises_and_is_not_cached(tmp_path):
    r, _ = rss(tmp_path, [(403, "")])
    try:
        read_post(PERMALINK, rss=r, cache_dir=tmp_path, fetch_page=lambda u: None)
        assert False, "expected RedditBlocked"
    except RedditBlocked as e:
        assert e.status == 403
    assert not list(tmp_path.glob("*.json"))


def test_second_read_comes_from_the_cache(tmp_path):
    seen = []
    r, wall = rss(tmp_path, [(200, FEED)], seen=seen)
    first = read_post(PERMALINK, rss=r, cache_dir=tmp_path, fetch_page=lambda u: None, now=wall.now)
    again = read_post(PERMALINK, rss=r, cache_dir=tmp_path, fetch_page=lambda u: None, now=wall.now)
    assert first == again and len(seen) == 1
    wall.t += 3601                                         # stale: would ask Reddit again
    r2, _ = rss(tmp_path, [(200, FEED)], wall=wall, seen=seen)
    read_post(PERMALINK, rss=r2, cache_dir=tmp_path, fetch_page=lambda u: None, now=wall.now)
    assert len(seen) == 2


def test_pacing_is_shared_between_instances(tmp_path):
    wall = Wall()
    a, _ = rss(tmp_path, [(200, FEED)], wall=wall)
    b, _ = rss(tmp_path, [(200, FEED)], wall=wall)          # another agent's server, same pace file
    a._get("/r/x/comments/abc123/.rss")
    wall.t += 20
    b._get("/r/x/comments/abc123/.rss")
    assert wall.sleeps == [45]
    assert float((tmp_path / "last").read_text()) == wall.t


def test_image_url_only_for_known_image_hosts():
    from labkit.reddit import Post
    base = dict(id="t3_x", rank=1, subreddit="s", title="t", author="a", permalink="p", selftext="", published="")
    assert image_url(Post(kind="image", link_url="https://i.redd.it/a.png", **base))
    assert image_url(Post(kind="image", link_url="https://www.reddit.com/gallery/abc", **base)) is None
    assert image_url(Post(kind="link", link_url="https://example.org/a.png", **base)) is None


def test_fetch_image_caps_and_checks_type(monkeypatch):
    def client(status, ctype, body):
        return httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(status, headers={"content-type": ctype}, content=body)))

    assert fetch_image("https://i.redd.it/a.png", client(200, "image/png", b"PNG")) == (b"PNG", "png")
    assert fetch_image("https://i.redd.it/a.png", client(200, "text/html", b"<html>")) is None
    assert fetch_image("https://i.redd.it/a.png", client(302, "image/png", b"")) is None
    monkeypatch.setattr(reddit_mcp, "IMAGE_BYTES", 2)
    assert fetch_image("https://i.redd.it/a.png", client(200, "image/png", b"PNG")) is None
