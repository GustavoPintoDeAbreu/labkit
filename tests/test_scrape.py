"""labkit.scrape: ProxyFetcher with a mock transport, BrowserFetcher with a fake Playwright page."""
import gzip
import random
from datetime import date, datetime

import httpx
import pytest

from labkit.scrape import (BrowserFetcher, ProxyFetcher, RawLog, SourceUnavailable,
                           looks_blocked)


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- ProxyFetcher (price-scout tests/test_fetch.py) ---

def test_proxy_url_encodes_and_country():
    f = ProxyFetcher("user", "p@ss", client=httpx.Client())
    url = f.proxy_url()
    assert "p%40ss_country-PT" in url
    assert url.startswith("http://")
    assert url.endswith("@rp.evomi.com:1000")


def test_proxy_url_no_country():
    f = ProxyFetcher("user", "pass", country="", client=httpx.Client())
    assert "pass@rp.evomi.com:1000" in f.proxy_url()
    assert "_country" not in f.proxy_url()


def test_delay_between_calls():
    sleeps = []

    class _ZeroRng:
        def uniform(self, a, b):
            return 0

    f = ProxyFetcher("u", "p", delay_s=5.0, client=mock_client(lambda r: httpx.Response(200, text="ok")),
                     sleep=sleeps.append, clock=lambda: 0.0, rng=_ZeroRng())
    f.get("https://x.test/1", tag="t1")
    f.get("https://x.test/2", tag="t2")
    assert sleeps == [5.0]


def test_403_blocked():
    f = ProxyFetcher("u", "p", client=mock_client(lambda r: httpx.Response(403)))
    with pytest.raises(SourceUnavailable, match="blocked 403"):
        f.get("https://x.test", tag="t")


def test_500_unavailable():
    f = ProxyFetcher("u", "p", client=mock_client(lambda r: httpx.Response(500)))
    with pytest.raises(SourceUnavailable, match="http 500"):
        f.get("https://x.test", tag="t")


def test_connection_error_unavailable():
    def refuse(request):
        raise httpx.ConnectError("refused")
    f = ProxyFetcher("u", "p", client=mock_client(refuse))
    with pytest.raises(SourceUnavailable, match="fetch failed"):
        f.get("https://x.test", tag="t")


def test_200_writes_raw_with_platform(tmp_path):
    raw = RawLog(tmp_path / "raw")
    f = ProxyFetcher("u", "p", raw_log=raw,
                     client=mock_client(lambda r: httpx.Response(200, text="<html>hi</html>")))
    assert f.get("https://x.test/page", tag="search-amazon", platform="amazon") == "<html>hi</html>"
    files = list((tmp_path / "raw").rglob("*.gz"))
    assert len(files) == 1 and "_amazon_search-amazon" in files[0].name
    with gzip.open(files[0], "rt", encoding="utf-8") as fh:
        assert fh.read() == "<html>hi</html>"


def test_bytes_used_counts_refused_pages_too():
    pages = {"/a": httpx.Response(200, text="x" * 100), "/b": httpx.Response(403, text="y" * 50)}
    f = ProxyFetcher("u", "p", delay_s=0, client=mock_client(lambda r: pages[r.url.path]))
    f.get("https://x.test/a", tag="a")
    with pytest.raises(SourceUnavailable):
        f.get("https://x.test/b", tag="b")
    assert f.bytes_used == 150


# --- BrowserFetcher (price-scout tests/test_browser.py) ---

class FakeResponse:
    def __init__(self, status, body=""):
        self.status, self.body = status, body

    def text(self):
        return self.body


class FakeRequest:
    def __init__(self, url, resource_type="document", body=0, headers=0, text=""):
        self.url, self.resource_type, self._sizes, self.text_body = url, resource_type, (body, headers), text

    def sizes(self):
        return {"responseBodySize": self._sizes[0], "responseHeadersSize": self._sizes[1]}


class FakeRoute:
    def __init__(self, request):
        self.request, self.result = request, None

    def abort(self):
        self.result = "abort"

    def continue_(self):
        self.result = "continue"


class FakePage:
    """pages: url -> (status or exception, html, [subresource FakeRequests])."""

    def __init__(self, pages):
        self.pages, self.visited, self.waited = pages, [], []
        self.router, self.listeners, self.routes = None, {}, []

    def route(self, pattern, handler):
        self.router = handler

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def goto(self, url, **kw):
        self.visited.append(url)
        status, _, subs = self.pages[url]
        if isinstance(status, Exception):
            raise status
        for req in [FakeRequest(url, body=1000, headers=200)] + subs:
            route = FakeRoute(req)
            if self.router is not None:
                self.router(route)
                self.routes.append(route)
            if route.result != "abort":
                for handler in self.listeners.get("requestfinished", []):
                    handler(req)
                for handler in self.listeners.get("response", []):
                    response = FakeResponse(200, req.text_body)
                    response.url = req.url
                    handler(response)
        return FakeResponse(status, f"raw:{url}")

    def wait_for_selector(self, selector, **kw):
        self.waited.append(selector)

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self.pages[self.visited[-1]][1]


class Closer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def stop(self):
        self.closed = True


def make(pages, launches, **kw):
    page, browser, pw = FakePage(pages), Closer(), Closer()

    def launch(proxy):
        launches.append(proxy)
        return pw, browser, page
    f = BrowserFetcher("user", "secret", launch=launch, sleep=lambda s: None, rng=random.Random(1), **kw)
    return f, page, browser, pw


def test_starts_once_headed_behind_the_proxy_and_returns_the_page():
    launches = []
    f, page, browser, pw = make({"https://a/1": (200, "<html>one</html>", []), "https://a/2": (200, "two", [])},
                                launches)
    assert f.get("https://a/1", tag="t") == "<html>one</html>"
    assert f.get("https://a/2", tag="t") == "two"
    assert len(launches) == 1
    proxy = launches[0]
    assert proxy["server"] == "http://rp.evomi.com:1000" and proxy["username"] == "user"
    assert proxy["password"].startswith("secret_country-PT_session-") and "@" not in proxy["server"]
    assert page.waited == []        # no ready_selector given
    f.close()
    assert browser.closed and pw.closed


def test_waits_for_the_ready_selector():
    f, page, *_ = make({"https://a/1": (200, "x", [])}, [], ready_selector="script#__NEXT_DATA__")
    f.get("https://a/1", tag="t")
    assert page.waited == ["script#__NEXT_DATA__"]


def test_blocks_and_failures_are_unavailable():
    f, *_ = make({"https://a/403": (403, "Access Denied", []), "https://a/x": (TimeoutError("slow"), "", [])}, [])
    with pytest.raises(SourceUnavailable, match="blocked 403"):
        f.get("https://a/403", tag="t")
    with pytest.raises(SourceUnavailable, match="TimeoutError"):
        f.get("https://a/x", tag="t")


def test_a_browser_that_will_not_start_is_unavailable():
    def launch(proxy):
        raise RuntimeError("no display")
    f = BrowserFetcher("u", "p", launch=launch, sleep=lambda s: None)
    with pytest.raises(SourceUnavailable, match="browser did not start"):
        f.get("https://a/1", tag="t")
    f.close()


def test_blocked_resource_types_are_aborted_and_not_counted():
    subs = [FakeRequest("https://a/i.jpg", "image", body=50_000), FakeRequest("https://a/f.woff2", "font", body=20_000),
            FakeRequest("https://a/app.js", "script", body=3_000, headers=100)]
    f, page, *_ = make({"https://a/1": (200, "x", subs)}, [], block_resources=("image", "font", "media"))
    f.get("https://a/1", tag="t")
    results = {r.request.url: r.result for r in page.routes}
    assert results == {"https://a/1": "continue", "https://a/i.jpg": "abort", "https://a/f.woff2": "abort",
                       "https://a/app.js": "continue"}
    assert f.bytes_used == 1200 + 3100


def test_without_blocking_every_request_counts():
    subs = [FakeRequest("https://a/i.jpg", "image", body=50_000)]
    f, page, *_ = make({"https://a/1": (200, "x", subs)}, [])
    f.get("https://a/1", tag="t")
    assert page.router is None
    assert f.bytes_used == 1200 + 50_000


# --- RawLog and looks_blocked ---

def test_rawlog_prunes_old_days(tmp_path):
    raw = RawLog(tmp_path, keep_days=14)
    raw.write("p", "old", "a", now=datetime(2026, 9, 1, 10))
    raw.write("p", "new", {"k": 1}, now=datetime(2026, 10, 10, 10))
    assert raw.prune(date(2026, 10, 11)) == 1
    assert [p.name for p in tmp_path.rglob("*.gz")] == ["100000_p_new.json.gz"]


def test_rawlog_disabled_writes_nothing(tmp_path):
    assert RawLog(tmp_path, enabled=False).write("p", "t", "x") is None
    assert list(tmp_path.iterdir()) == []


def test_looks_blocked():
    assert looks_blocked("<title>Robot Check</title>")
    assert not looks_blocked("<html>T2 em Alvalade</html>")


def test_patchright_engine_is_passed_to_the_launcher():
    seen = []

    def launch(proxy, engine="playwright"):
        seen.append(engine)
        return Closer(), Closer(), FakePage({"https://a/1": (200, "x", [])})
    f = BrowserFetcher("u", "p", engine="patchright", launch=launch, sleep=lambda s: None)
    f.get("https://a/1", tag="t")
    assert seen == ["patchright"]


def test_unknown_engine_is_refused():
    with pytest.raises(ValueError):
        BrowserFetcher("u", "p", engine="selenium")


def test_allow_hosts_aborts_other_hosts_but_keeps_subdomains():
    subs = [FakeRequest("https://dd.site.pt/tags.js", "script", body=100),
            FakeRequest("https://cdn.other.com/big.js", "script", body=90_000),
            FakeRequest("https://challenges.cloudflare.com/t.js", "script", body=10)]
    f, page, *_ = make({"https://www.site.pt/1": (200, "x", subs)}, [],
                       allow_hosts=("site.pt", "challenges.cloudflare.com"))
    f.get("https://www.site.pt/1", tag="t")
    results = {r.request.url: r.result for r in page.routes}
    assert results["https://cdn.other.com/big.js"] == "abort"
    assert [u for u, r in results.items() if r == "continue"] == [
        "https://www.site.pt/1", "https://dd.site.pt/tags.js", "https://challenges.cloudflare.com/t.js"]
    assert f.bytes_used == 1200 + 100 + 10


def test_raw_returns_the_body_as_served_without_waiting():
    f, page, *_ = make({"https://a/api/x": (200, "<pre>rendered</pre>", [])}, [], ready_selector="#x")
    assert f.get("https://a/api/x", tag="t", raw=True) == "raw:https://a/api/x"
    assert page.waited == []


def test_capture_returns_the_pages_own_data_call():
    """A booking engine renders its fares from an XHR after the page loads; the body
    of that call is what the caller wants, not the page shell."""
    data = FakeRequest("https://b/bfm/rest/booking/availability/search", "xhr", 300, 10, text='{"data": 1}')
    other = FakeRequest("https://b/assets/footer.json", "xhr", 10, 10, text="{}")
    f, page, *_ = make({"https://b/deeplink": (200, "<app-root></app-root>", [other, data])}, [],
                       capture="booking/availability/search")
    assert f.get("https://b/deeplink", tag="t") == '{"data": 1}'


def test_capture_falls_back_to_the_page_when_the_call_never_comes():
    f, page, *_ = make({"https://b/deeplink": (200, "<app-root></app-root>", [])}, [],
                       capture="booking/availability/search")
    assert f.get("https://b/deeplink", tag="t") == "<app-root></app-root>"


def test_capture_is_per_get():
    data = FakeRequest("https://b/avail?1", "xhr", 1, 1, text="first")
    f, page, *_ = make({"https://b/1": (200, "one", [data]), "https://b/2": (200, "two", [])}, [], capture="avail")
    assert f.get("https://b/1", tag="t") == "first"
    assert f.get("https://b/2", tag="t") == "two"


def test_screenshot_after_the_page_settles_with_a_prepare_step():
    f, page, *_ = make({"https://a/1": (200, "<html>story</html>", []), "https://a/x": (403, "no", [])}, [])
    shots, prepared = [], []
    page.screenshot = lambda **kw: shots.append(kw) or b"PNG"
    png, html = f.screenshot("https://a/1", tag="t", prepare=lambda p: prepared.append(p) or 1 / 0)
    assert (png, html) == (b"PNG", "<html>story</html>") and prepared == [page]   # prepare's error ignored
    assert shots == [{"type": "png", "full_page": False}] and f.bytes_used == 1200
    with pytest.raises(SourceUnavailable, match="blocked 403"):
        f.screenshot("https://a/x", tag="t")

    def broken(**kw):
        raise TimeoutError("slow")
    page.screenshot = broken
    with pytest.raises(SourceUnavailable, match="screenshot failed: TimeoutError"):
        f.screenshot("https://a/1", tag="t")
