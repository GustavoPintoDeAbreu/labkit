import httpx
import pytest

from labkit import web
from labkit.web import extract_text, fetch_page, github_repo, skipped_host, truncate

HTML = {"content-type": "text/html; charset=utf-8"}


def mock(handler, seen=None):
    def h(req):
        if seen is not None:
            seen.append(str(req.url))
        return handler(req)
    return httpx.Client(transport=httpx.MockTransport(h))


def _inline_html(text):
    return f"<html><body><article><p>{text}</p></article></body></html>"


# --- lifted from redditcast (need trafilatura) ------------------------------

def test_extract_text_returns_text():
    pytest.importorskip("trafilatura")
    out = extract_text(_inline_html("The quick brown fox jumps over the lazy dog. " * 40))
    assert out is not None
    assert len(out) >= 200


def test_extract_text_short_html():
    pytest.importorskip("trafilatura")
    assert extract_text(_inline_html("Hi there.")) is None


def test_extract_text_boilerplate():
    pytest.importorskip("trafilatura")
    assert extract_text(_inline_html("Copyright 2024 My Site. All rights reserved.")) is None


# --- no trafilatura needed --------------------------------------------------

def test_truncate_keeps_short_text():
    assert truncate("abc", 10) == "abc"


def test_truncate_cuts_at_sentence():
    t = "Sentence one. " * 100
    out = truncate(t, 300)
    assert 150 < len(out) <= 300
    assert out.startswith("Sentence one.")
    assert not out.endswith(" ")


def test_fetch_page_404_is_none():
    c = mock(lambda req: httpx.Response(404))
    assert fetch_page("https://example.com/a", c) is None


@pytest.mark.parametrize("url", [
    "https://reddit.com/skip",
    "https://www.reddit.com/r/x",
    "https://i.redd.it/a.png",
    "https://youtu.be/abc",
    "https://m.youtube.com/watch?v=a",
    "https://i.imgur.com/a.png",
])
def test_fetch_page_skipped_hosts(url):
    seen = []
    c = mock(lambda req: httpx.Response(200, text="<html></html>", headers=HTML), seen=seen)
    assert fetch_page(url, c) is None
    assert seen == []
    assert skipped_host("https://github.com/a/b") is False


@pytest.mark.parametrize("url", ["ftp://example.com/a", "mailto:a@example.com"])
def test_fetch_page_non_http_scheme(url):
    seen = []
    c = mock(lambda req: httpx.Response(200, text="<html></html>", headers=HTML), seen=seen)
    assert fetch_page(url, c) is None
    assert seen == []


def test_fetch_page_non_html_is_none():
    c = mock(lambda req: httpx.Response(200, content=b"%PDF",
                                        headers={"content-type": "application/pdf"}))
    assert fetch_page("https://example.com/a", c) is None


def test_fetch_page_never_raises(monkeypatch):
    c = mock(lambda req: (_ for _ in ()).throw(httpx.ConnectError("boom")))
    assert fetch_page("https://example.com/a", c) is None
    monkeypatch.setattr(web, "extract_text", lambda h, m: (_ for _ in ()).throw(RuntimeError("x")))
    c = mock(lambda req: httpx.Response(200, text="<html><body>x</body></html>", headers=HTML))
    assert fetch_page("https://example.com/a", c) is None


def test_fetch_page_caps_bytes(monkeypatch):
    monkeypatch.setattr(web, "extract_text", lambda h, m: h)
    c = mock(lambda req: httpx.Response(200, content=b"<html>" + b"a" * 5000, headers=HTML))
    assert len(fetch_page("https://example.com/a", c, max_bytes=1000)) == 1000


def test_fetch_page_passes_html_to_extract(monkeypatch):
    monkeypatch.setattr(web, "extract_text", lambda h, m: f"{m}:{h}")
    c = mock(lambda req: httpx.Response(200, content=b"<p>hi</p>", headers=HTML))
    assert fetch_page("https://example.com/a", c, max_chars=77) == "77:<p>hi</p>"


def test_github_repo_parsing():
    assert github_repo("https://github.com/example/tiny-agent") == ("example", "tiny-agent")
    assert github_repo("https://www.github.com/example/tiny-agent/") == ("example", "tiny-agent")
    assert github_repo("https://github.com/example/tiny-agent.git") == ("example", "tiny-agent")
    assert github_repo("https://github.com/example/tiny-agent#readme") == ("example", "tiny-agent")
    assert github_repo("https://github.com/example/tiny-agent/issues/3") is None
    assert github_repo("https://github.com/example") is None
    assert github_repo("https://github.com/orgs/example") is None
    assert github_repo("https://gist.github.com/a/b") is None


def test_fetch_page_github_returns_readme():
    def handler(req):
        if (req.url.host == "api.github.com" and req.url.path == "/repos/example/tiny-agent/readme"
                and req.headers["accept"] == "application/vnd.github.raw+json"):
            return httpx.Response(200, content=b"# tiny-agent\n\nA small agent.\n",
                                  headers={"content-type": "application/vnd.github.raw+json; charset=utf-8"})
        return httpx.Response(500)
    seen = []
    c = mock(handler, seen=seen)
    assert fetch_page("https://github.com/example/tiny-agent", c) == "# tiny-agent\n\nA small agent."
    assert seen == ["https://api.github.com/repos/example/tiny-agent/readme"]


def test_fetch_page_github_readme_capped():
    def handler(req):
        if req.url.host == "api.github.com":
            return httpx.Response(200, content=("word " * 3000).encode(),
                                  headers={"content-type": "application/vnd.github.raw+json"})
        return httpx.Response(500)
    c = mock(handler)
    assert len(fetch_page("https://github.com/example/tiny-agent", c, max_chars=500)) <= 500


def test_fetch_page_github_falls_back_to_page(monkeypatch):
    monkeypatch.setattr(web, "extract_text", lambda h, m: "page text")
    def handler(req):
        if req.url.host == "api.github.com":
            return httpx.Response(403)
        return httpx.Response(200, text="<html><body>page</body></html>", headers=HTML)
    seen = []
    c = mock(handler, seen=seen)
    assert fetch_page("https://github.com/example/tiny-agent", c) == "page text"
    assert [u.split("/")[2] for u in seen] == ["api.github.com", "github.com"]


def test_own_client_sets_user_agent():
    c = web._client("labkit-test/1.0", 20)
    assert c.headers["User-Agent"] == "labkit-test/1.0"
    assert c.follow_redirects is True
