import httpx
import pytest
from xml.sax.saxutils import escape

from labkit import reddit
from labkit.reddit import (Post, PostUnreadable, RedditBlocked, RedditRSS, fetch_post,
                           html_to_text, is_reddit_url, is_redirect_link, parse_comments,
                           parse_listing, parse_post, post_path)

PERMALINK = "https://www.reddit.com/r/LocalLLaMA/comments/abc123/tiny_agent/"
RSS = "https://www.reddit.com/r/LocalLLaMA/comments/abc123/.rss?sort=top&limit=13"


def entry(id, author, link, content, title="", sub="LocalLLaMA", published="2026-10-01T10:00:00+00:00"):
    return (f'<entry><author><name>/u/{author}</name></author>'
            f'<category term="{sub}" label="r/{sub}"/>'
            f'<content type="html">{escape(content)}</content><id>{id}</id>'
            f'<link href="{link}"/><updated>{published}</updated><published>{published}</published>'
            f'<title>{escape(title or author + " on post")}</title></entry>')


def feed(*entries):
    return ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            + "".join(entries) + "</feed>")


def md(inner):
    return f'<!-- SC_OFF --><div class="md">{inner} </div><!-- SC_ON -->'


POST_HTML = (
    f'<table> <tr><td> <a href="{PERMALINK}"> <img src="https://preview.redd.it/thumb.png?width=140&amp;height=60" alt="t" /> </a> </td><td> '
    + md('<p>Code is on <a href="https://github.com/example/tiny-agent">GitHub</a> and the write-up is '
         '<a href="https://blog.example.com/tiny-agent">here</a>.</p> '
         '<p><a href="https://preview.redd.it/shot.png?width=640&amp;format=png">https://preview.redd.it/shot.png?width=640&amp;format=png</a></p> '
         '<p>See also <a href="/r/MachineLearning">r/MachineLearning</a>.</p>')
    + ' &#32; submitted by &#32; <a href="https://www.reddit.com/user/alice"> /u/alice </a> <br/>'
    f' <span><a href="{PERMALINK}">[link]</a></span> &#32; <span><a href="{PERMALINK}">[comments]</a></span> </td></tr></table>')

COMMENTS = [
    entry("t1_c1", "AutoModerator", PERMALINK + "c1/", md('<p>Join us on <a href="https://discord.gg/example">Discord</a>.</p> <p><em>I am a bot, and this action was performed automatically.</em></p>')),
    entry("t1_c2", "bob", PERMALINK + "c2/", md('<p>The paper behind it: <a href="https://arxiv.org/abs/2601.00001">arxiv</a>, and the repo again <a href="https://github.com/example/tiny-agent">here</a>.</p>')),
    entry("t1_c3", "carol", PERMALINK + "c3/", md('<p>Works great on my 3090, thanks for sharing.</p>')),
    entry("t1_c4", "dave", PERMALINK + "c4/", md('<p>lol</p>')),
    entry("t1_c5", "erin", PERMALINK + "c5/", md('<p>Demo video: <a href="https://www.youtube.com/watch?v=abc">https://www.youtube.com/watch?v=abc</a> worth watching.</p>')),
    entry("t1_c6", "frank", PERMALINK + "c6/", md('<p>Older thread: <a href="https://www.reddit.com/r/LocalLLaMA/comments/zzz999/older/">here</a>, same idea.</p>')),
]
FEED = feed(entry("t3_abc123", "alice", PERMALINK, POST_HTML, title="I built a tiny agent"), *COMMENTS)

LINK_PERMALINK = "https://www.reddit.com/r/technology/comments/def456/story/"
LINK_HTML = ('<table> <tr><td> &#32; submitted by &#32; <a href="https://www.reddit.com/user/gina"> /u/gina </a> <br/>'
             ' <span><a href="https://example.org/news/story">[link]</a></span> &#32;'
             f' <span><a href="{LINK_PERMALINK}">[comments]</a></span> </td></tr></table>')
LINK_FEED = feed(
    entry("t3_def456", "gina", LINK_PERMALINK, LINK_HTML, title="Big story", sub="technology"),
    entry("t1_d1", "hank", LINK_PERMALINK + "d1/", md('<p>Mirror of <a href="https://example.org/news/story">the story</a> plus <a href="https://archive.example.net/story">an archive</a>.</p>'), sub="technology"))


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def client(responses, clock=None, seen=None):
    """RedditRSS whose HTTP answers come from ``responses`` in order: (status, text, headers),
    or an Exception instance to raise."""
    it = iter(responses)

    def handler(req):
        if seen is not None:
            seen.append(str(req.url))
        item = next(it)
        if isinstance(item, Exception):
            raise item
        status, text, headers = item
        return httpx.Response(status, text=text, headers=headers)

    clock = clock or Clock()
    http = httpx.Client(base_url="https://www.reddit.com", transport=httpx.MockTransport(handler))
    return RedditRSS("test-ua", 65, client=http, sleep=clock.sleep, monotonic=clock.now), clock


# --- lifted from redditcast -------------------------------------------------

def test_requests_are_spaced():
    r, clock = client([(200, FEED, {})] * 3)
    for _ in range(3):
        r._get("/r/x/top/.rss")
    starts = [t for t, _, _ in r.requests]
    for a, b in zip(starts, starts[1:]):
        assert b - a >= 65
    assert clock.sleeps == [65, 65]


def test_429_waits_for_reset_then_succeeds():
    r, clock = client([(429, "", {"x-ratelimit-reset": "30"}), (200, FEED, {})])
    assert r._get("/r/x/top/.rss") == FEED
    assert 35 in clock.sleeps


def test_429_forever_raises():
    r, _ = client([(429, "", {"x-ratelimit-reset": "1"})] * 3)
    with pytest.raises(RedditBlocked) as e:
        r._get("/r/x/top/.rss")
    assert e.value.status == 429


def test_403_raises_at_once():
    r, _ = client([(403, "blocked", {})])
    with pytest.raises(RedditBlocked):
        r._get("/r/x/top/.rss")
    assert len(r.requests) == 1


def test_404_says_private():
    r, _ = client([(404, "", {})])
    with pytest.raises(RedditBlocked, match="private"):
        r._get("/r/x/top/.rss")


def test_5xx_retried_once():
    r, _ = client([(502, "", {}), (200, FEED, {})])
    assert r._get("/r/x/top/.rss") == FEED


def test_top_urls():
    assert RedditRSS.top_url({"multi": {"user": "someone", "name": "ai"}}) == "/user/someone/m/ai/top/.rss?t=week"
    assert RedditRSS.top_url({"subreddits": ["a", "b"]}, "month") == "/r/a+b/top/.rss?t=month"
    with pytest.raises(ValueError):
        RedditRSS.top_url({"name": "x"})


def test_comments_path():
    seen = []
    r, _ = client([(200, FEED, {})], seen=seen)
    out = r.comments(parse_post(FEED), 5)
    assert [c.author for c in out] == ["bob", "carol", "erin", "frank"]
    assert seen[0].endswith("/r/LocalLLaMA/comments/abc123/tiny_agent/.rss?sort=top&limit=10")


def test_html_to_text_handles_double_escape():
    assert "It's a test" in html_to_text("<p>It&amp;#39;s a test</p>")


def test_parse_comments_filters_bots_and_short():
    assert [c.author for c in parse_comments(FEED, 10)] == ["bob", "carol", "erin", "frank"]
    assert not any("i am a bot" in c.body for c in parse_comments(FEED, 10))
    assert len(parse_comments(FEED, 2)) == 2


# --- parsing (new) ----------------------------------------------------------

def test_parse_listing_ranks_kinds_links():
    posts = parse_listing(feed(
        entry("t3_abc123", "alice", PERMALINK, POST_HTML, title="I built a tiny agent"),
        entry("t3_def456", "gina", LINK_PERMALINK, LINK_HTML, title="Big story", sub="technology")))
    assert [p.rank for p in posts] == [1, 2]
    assert [p.kind for p in posts] == ["self", "link"]
    assert posts[0].links == ["https://github.com/example/tiny-agent",
                              "https://blog.example.com/tiny-agent",
                              "https://preview.redd.it/shot.png?width=640&format=png"]


def test_parse_post_self_post():
    p = parse_post(FEED)
    assert p.id == "t3_abc123"
    assert p.subreddit == "LocalLLaMA"
    assert p.title == "I built a tiny agent"
    assert p.author == "alice"
    assert p.permalink == PERMALINK
    assert p.kind == "self"
    assert p.link_url is None
    assert p.rank == 1
    assert p.published == "2026-10-01T10:00:00+00:00"
    assert p.selftext.startswith("Code is on GitHub and the write-up is here.")
    assert "See also r/MachineLearning." in p.selftext
    assert "submitted by" not in p.selftext
    assert [c.author for c in p.comments] == ["bob", "carol", "erin", "frank"]
    assert p.comments[0].links == ["https://arxiv.org/abs/2601.00001", "https://github.com/example/tiny-agent"]


def test_outbound_links_dedupe_and_drop_reddit():
    assert parse_post(FEED).outbound_links() == [
        "https://github.com/example/tiny-agent",
        "https://blog.example.com/tiny-agent",
        "https://arxiv.org/abs/2601.00001",
        "https://www.youtube.com/watch?v=abc"]


def test_link_post_target_not_in_outbound():
    p = parse_post(LINK_FEED)
    assert p.kind == "link"
    assert p.link_url == "https://example.org/news/story"
    assert p.selftext == ""
    assert p.outbound_links() == ["https://archive.example.net/story"]


def test_parse_post_comment_limit():
    assert [c.author for c in parse_post(FEED, comments=2).comments] == ["bob", "carol"]


def test_parse_post_unreadable():
    with pytest.raises(PostUnreadable):
        parse_post(feed(*COMMENTS))
    with pytest.raises(PostUnreadable):
        parse_post("not xml")


# --- URL helpers (new) ------------------------------------------------------

@pytest.mark.parametrize("url", [
    PERMALINK,
    "https://old.reddit.com/r/LocalLLaMA/comments/abc123/tiny_agent/",
    "https://m.reddit.com/r/LocalLLaMA/comments/abc123/tiny_agent/?utm_source=share&utm_medium=android_app",
    "https://reddit.com/r/LocalLLaMA/comments/abc123",
    "www.reddit.com/r/LocalLLaMA/comments/abc123/tiny_agent/",
    "  https://new.reddit.com/r/LocalLLaMA/comments/abc123/tiny_agent/def456/?context=3 ",
])
def test_post_path_accepts_permalink_variants(url):
    assert post_path(url) == "/r/LocalLLaMA/comments/abc123"


@pytest.mark.parametrize("url", [
    "https://redd.it/abc123",
    "https://www.reddit.com/r/LocalLLaMA/s/AbC12xY",
    "https://www.reddit.com/r/LocalLLaMA/",
    "https://example.com/r/x/comments/abc123/",
    "https://i.redd.it/x.png",
    "",
])
def test_post_path_rejects(url):
    assert post_path(url) is None


def test_post_path_takes_a_bare_comments_page():
    assert post_path("https://www.reddit.com/comments/abc123/") == "/comments/abc123"


def test_is_redirect_link():
    assert is_redirect_link("https://www.reddit.com/r/LocalLLaMA/s/AbC12xY")
    assert is_redirect_link("https://redd.it/abc123")
    assert not is_redirect_link(PERMALINK)
    assert not is_redirect_link("https://i.redd.it/x.png")
    assert not is_redirect_link("https://example.com/s/x")


def test_is_reddit_url():
    assert is_reddit_url(PERMALINK)
    assert is_reddit_url("https://preview.redd.it/a.png")
    assert is_reddit_url("https://redd.it/x")
    assert is_reddit_url("old.reddit.com/r/x")
    assert not is_reddit_url("https://github.com/a/b")
    assert not is_reddit_url("https://notreddit.com/x")


# --- RedditRSS.post / fetch_post (new) -------------------------------------

def test_permalink_is_one_request():
    seen = []
    r, clock = client([(200, FEED, {})], seen=seen)
    p = r.post("https://old.reddit.com/r/LocalLLaMA/comments/abc123/tiny_agent/")
    assert p.id == "t3_abc123"
    assert seen == [RSS]
    assert clock.sleeps == []


def test_share_link_resolves_then_reads():
    seen = []
    r, clock = client([(301, "", {"location": PERMALINK + "?share_id=x&utm_medium=android_app"}),
                       (200, FEED, {})], seen=seen)
    p = r.post("https://www.reddit.com/r/LocalLLaMA/s/AbC12xY")
    assert p.id == "t3_abc123"
    assert seen == ["https://www.reddit.com/r/LocalLLaMA/s/AbC12xY", RSS]
    assert clock.sleeps == [65]


def test_redd_it_reads_the_bare_comments_feed():
    # redd.it/<id> lands on www.reddit.com/comments/<id>, which answers 200 (live, 2026-10-06):
    # the feed is read straight from /comments/<id>/.rss, with no redirect request.
    seen = []
    r, clock = client([(200, FEED, {})], seen=seen)
    p = r.post("https://redd.it/abc123")
    assert p.id == "t3_abc123"
    assert seen == ["https://www.reddit.com/comments/abc123/.rss?sort=top&limit=13"]
    assert clock.sleeps == []


def test_a_share_link_landing_on_a_bare_comments_path_resolves():
    seen = []
    r, clock = client([(301, "", {"location": "https://www.reddit.com/comments/abc123/"}),
                       (200, FEED, {})], seen=seen)
    p = r.post("https://www.reddit.com/r/LocalLLaMA/s/AbC12xY")
    assert p.id == "t3_abc123"
    assert seen == ["https://www.reddit.com/r/LocalLLaMA/s/AbC12xY",
                    "https://www.reddit.com/comments/abc123/.rss?sort=top&limit=13"]


def test_share_link_without_redirect_is_unreadable():
    r, _ = client([(200, "<html></html>", {})])
    with pytest.raises(PostUnreadable):
        r.post("https://www.reddit.com/r/LocalLLaMA/s/AbC12xY")


def test_share_link_redirecting_elsewhere_is_unreadable():
    r, _ = client([(302, "", {"location": "https://example.com/"})])
    with pytest.raises(PostUnreadable):
        r.post("https://www.reddit.com/r/LocalLLaMA/s/AbC12xY")


def test_share_link_403_is_blocked():
    r, _ = client([(403, "", {})])
    with pytest.raises(RedditBlocked):
        r.post("https://www.reddit.com/r/LocalLLaMA/s/AbC12xY")


def test_feed_403_is_blocked():
    r, _ = client([(403, "blocked", {})])
    with pytest.raises(RedditBlocked) as e:
        r.post(PERMALINK)
    assert e.value.status == 403


def test_not_a_post_link_makes_no_request():
    seen = []
    r, _ = client([], seen=seen)
    with pytest.raises(PostUnreadable):
        r.post("https://example.com/x")
    assert seen == []


def test_transport_error_is_unreadable():
    r, _ = client([httpx.ConnectError("boom"), httpx.ConnectError("boom")])
    with pytest.raises(PostUnreadable):
        r.post(PERMALINK)


def test_reddit_error_is_the_base():
    assert issubclass(RedditBlocked, reddit.RedditError)
    assert issubclass(PostUnreadable, reddit.RedditError)


def test_fetch_post_module_function():
    http = httpx.Client(base_url="https://www.reddit.com",
                        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=FEED)))
    assert fetch_post(PERMALINK, user_agent="t", client=http).title == "I built a tiny agent"


def test_default_client_and_user_agent():
    r = RedditRSS("labkit-test/1.0")
    assert r._client.headers["User-Agent"] == "labkit-test/1.0"
    assert str(r._client.base_url).rstrip("/") == "https://www.reddit.com"
    assert r._client.follow_redirects is True
    with pytest.raises(ValueError):
        RedditRSS("  ")
