"""Reddit through his Chrome (labkit.reddit: parse_post_page, read_page_via_chrome, fetch_post's order).

Since ~2026-11-17 Reddit refuses every logged-out feed read from the lab; idea-pipeline's Reddit
ideas (idea 164) and every other fetch_post caller stopped. The page below is synthetic."""
import json

import httpx
import pytest

from labkit import reddit
from labkit.reddit import PostUnreadable, RedditBlocked, fetch_post, parse_post_page

PAGE = """<html><body>
<div class="thing promoted link" data-type="link" data-fullname="t3_ad"><a class="title">An ad</a></div>
<div class=" thing id-t3_abc123 link self" data-fullname="t3_abc123" data-type="link" data-subreddit="LocalLLM"
     data-author="alice" data-permalink="/r/LocalLLM/comments/abc123/kv_cache_in_ram/" data-timestamp="1795600000000"
     data-url="/r/LocalLLM/comments/abc123/kv_cache_in_ram/">
  <p class="title"><a class="title may-blank" href="/r/LocalLLM/comments/abc123/">Why not put the KV cache &amp; weights in RAM?</a></p>
  <div class="expando"><form><div class="usertext-body"><div class="md">
    <p>I tried it on a 4x3090 rig.</p><p>Code: <a href="https://github.com/example/kv-offload">kv-offload</a></p>
  </div></div></form></div>
</div>
<div class="commentarea">
  <div class="thing comment" data-type="comment" data-author="bob"><div class="entry"><div class="md">
    <p>Strata does this with <code>--kv-resident 32768</code>, the rest stays in RAM.</p></div></div>
    <div class="child"><div class="thing comment" data-type="comment" data-author="carol"><div class="md">
      <p>A reply that is not top-level and is skipped.</p></div></div></div>
  </div>
  <div class="thing comment" data-type="comment" data-author="AutoModerator"><div class="md"><p>Please follow the rules here.</p></div></div>
  <div class="thing comment" data-type="comment" data-author="dave"><div class="md"><p>[deleted]</p></div></div>
  <div class="thing comment" data-type="comment" data-author="erin"><div class="md">
    <p>See <a href="https://github.com/example/adaptive-kv">adaptive kv streaming</a> for llama.cpp.</p></div></div>
</div></body></html>"""


def test_a_post_page_parses_like_the_feed():
    post = parse_post_page(PAGE, comments=8)
    assert post.id == "t3_abc123" and post.subreddit == "LocalLLM" and post.author == "alice"
    assert post.title == "Why not put the KV cache & weights in RAM?"
    assert post.permalink == "https://www.reddit.com/r/LocalLLM/comments/abc123/kv_cache_in_ram/"
    assert post.kind == "self" and post.link_url is None
    assert post.selftext == "I tried it on a 4x3090 rig.\n\nCode: kv-offload"
    assert post.links == ["https://github.com/example/kv-offload"]
    assert [c.author for c in post.comments] == ["bob", "erin"], "top-level only, bots and deleted out"
    assert "--kv-resident 32768" in post.comments[0].body
    assert post.outbound_links() == ["https://github.com/example/kv-offload",
                                     "https://github.com/example/adaptive-kv"]
    assert post.published.startswith("2026-11-")


def test_a_page_without_a_post_is_unreadable():
    with pytest.raises(PostUnreadable, match="no Reddit post"):
        parse_post_page("<html><body>Log in to Reddit</body></html>")


def _browser(status="done", text=PAGE, seen=None):
    def handler(request):
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body["params"]["arguments"])
        return httpx.Response(200, json={"result": {"structuredContent": {"result": {
            "status": status, "text": text, "answer": "" if status == "done" else "no desktop session"}}}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_read_page_asks_lab_browser_for_reddit_only_html(monkeypatch):
    monkeypatch.setattr(reddit, "LAB_BROWSER_MCP", "http://lab-browser.test/mcp")
    seen = []
    assert reddit.read_page_via_chrome("https://old.reddit.com/r/x/comments/abc123/?sort=top",
                                       client=_browser(seen=seen)) == PAGE
    assert seen == [{"url": "https://old.reddit.com/r/x/comments/abc123/?sort=top",
                     "sites": ["reddit.com"], "html": True}]
    with pytest.raises(PostUnreadable, match="no desktop session"):
        reddit.read_page_via_chrome("https://old.reddit.com/r/x/", client=_browser(status="failed", text=""))


def test_old_reddit_urls():
    assert reddit._old_reddit_url("/r/x/comments/abc123/.rss") == "https://old.reddit.com/r/x/comments/abc123/?sort=top"
    assert reddit._old_reddit_url("https://www.reddit.com/r/x/comments/abc123/slug/") == \
        "https://old.reddit.com/r/x/comments/abc123/slug/?sort=top"


def test_fetch_post_reads_chrome_first_and_never_touches_the_feeds(monkeypatch):
    monkeypatch.setattr(reddit, "LAB_BROWSER_MCP", "http://lab-browser.test/mcp")
    asked = []
    monkeypatch.setattr(reddit, "read_page_via_chrome", lambda url, **kw: asked.append(url) or PAGE)
    feeds = httpx.Client(base_url="https://www.reddit.com",
                         transport=httpx.MockTransport(lambda req: pytest.fail(f"feed read: {req.url}")))
    post = fetch_post("https://www.reddit.com/r/LocalLLM/comments/abc123/slug/", user_agent="t", client=feeds)
    assert post.id == "t3_abc123"
    assert asked == ["https://old.reddit.com/r/LocalLLM/comments/abc123/?sort=top"]


def test_a_share_link_is_resolved_before_chrome(monkeypatch):
    monkeypatch.setattr(reddit, "LAB_BROWSER_MCP", "http://lab-browser.test/mcp")
    asked = []
    monkeypatch.setattr(reddit, "read_page_via_chrome", lambda url, **kw: asked.append(url) or PAGE)

    def handler(request):
        assert request.url.path == "/r/LocalLLM/s/AbCdEf"
        return httpx.Response(301, headers={"location": "https://www.reddit.com/r/LocalLLM/comments/abc123/slug/"})
    resolver = httpx.Client(base_url="https://www.reddit.com", transport=httpx.MockTransport(handler))
    fetch_post("https://www.reddit.com/r/LocalLLM/s/AbCdEf", user_agent="t", client=resolver)
    assert asked == ["https://old.reddit.com/r/LocalLLM/comments/abc123/?sort=top"]


def test_chrome_down_falls_back_to_the_feeds_and_says_both(monkeypatch):
    monkeypatch.setattr(reddit, "LAB_BROWSER_MCP", "http://lab-browser.test/mcp")

    def down(url, **kw):
        raise PostUnreadable("lab-browser did not answer (ConnectError)")
    monkeypatch.setattr(reddit, "read_page_via_chrome", down)
    empty = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    feeds = httpx.Client(base_url="https://www.reddit.com",
                         transport=httpx.MockTransport(lambda req: httpx.Response(404, text=empty)))
    with pytest.raises(RedditBlocked) as e:
        fetch_post("https://www.reddit.com/r/LocalLLM/comments/abc123/", user_agent="t", client=feeds)
    assert str(e.value).startswith("HTTP 404") and "his Chrome could not read it either: lab-browser did not answer" in str(e.value)


def test_browser_false_and_non_reddit_links_skip_chrome(monkeypatch):
    monkeypatch.setattr(reddit, "LAB_BROWSER_MCP", "http://lab-browser.test/mcp")
    monkeypatch.setattr(reddit, "read_page_via_chrome", lambda url, **kw: pytest.fail("chrome asked"))
    feed = httpx.Client(base_url="https://www.reddit.com",
                        transport=httpx.MockTransport(lambda req: httpx.Response(403)))
    with pytest.raises(RedditBlocked):
        fetch_post("https://www.reddit.com/r/x/comments/abc123/", user_agent="t", client=feed, browser=False)
    with pytest.raises(PostUnreadable):
        fetch_post("https://example.com/not-reddit", user_agent="t", client=feed)
