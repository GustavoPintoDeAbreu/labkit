"""Scraping through the Evomi residential proxy: a plain HTTP fetcher, a real browser, a raw-page log.

Lifted from price-scout (``sources/fetch.py``, ``sources/browser.py``, ``sources/base.py``,
``raw.py``), where it reads Amazon.es, AliExpress and KuantoKusta every day on the Pi.

There is no unproxied fetcher: both fetchers need Evomi credentials, so a project never
scrapes a site from the home IP. Country and session targeting are password suffixes
(``_country-PT``, ``_session-<id>``), never URL parameters.

- ``ProxyFetcher``: httpx through the proxy (credentials in the proxy URL work with httpx).
- ``BrowserFetcher``: a **headed** Playwright Chromium through the proxy (credentials as
  separate fields: in the URL, Playwright's handshake times out). KuantoKusta's Akamai
  answers 403 to httpx and to headless Chromium but serves a headed one (verified
  2026-10-07), so run it under a virtual display (``xvfb-run``). Needs ``labkit[scrape]``
  plus ``playwright install chromium``; Playwright is imported only when the browser starts.
  ``block_resources`` drops images, fonts or media before they are fetched: the proxy bills
  per GB, and ``allow_hosts`` aborts every request to another host (third-party and CDN
  scripts: an Idealista page costs ~150 KB through the proxy instead of ~2 MB). ``engine="patchright"`` (``labkit[stealth]``, then ``patchright install chromium``)
  drives Patchright, Playwright with its automation tells patched out: Idealista's Cloudflare
  challenge never clears for Playwright's Chromium but lets Patchright's through (verified
  2026-10-11 through Evomi PT exits).
- Both count the bytes they pulled through the proxy (``bytes_used``), so a run can stop at
  a daily cap. httpx counts the bytes on the wire; the browser counts each finished
  request's encoded body and headers.
- ``RawLog``: a gzipped copy of every page fetched, for recording fixtures and diagnosing
  drift.

A fetch that is blocked (403/429/503), fails or times out raises ``SourceUnavailable``.
``SourceDrift`` is for parsers: a page whose shape is not recognised raises it instead of
returning nothing.
"""
from __future__ import annotations

import functools
import gzip
import json
import random
import re
import string
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

import httpx

EVOMI_PROXY_HOST = "rp.evomi.com:1000"
EVOMI_PROXY_SERVER = f"http://{EVOMI_PROXY_HOST}"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "pt-PT,pt;q=0.9,es;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

NAV_TIMEOUT_MS = 60_000
SETTLE_MS = 2_500            # after DOMContentLoaded, for the page's own scripts
READY_TIMEOUT_MS = 15_000

BLOCK_MARKERS = ("validatecaptcha", "captcha", "robot check", "are you a human")


class SourceDrift(Exception):
    """The page or response is not a shape we recognise."""


class SourceUnavailable(Exception):
    """Blocked, refused, timed out, 4xx/5xx."""


@runtime_checkable
class Fetcher(Protocol):
    def get(self, url: str, *, tag: str, platform: str = "page") -> str: ...


def looks_blocked(html: str) -> bool:
    """True when the page looks like a bot-check wall."""
    low = html.lower()
    return any(marker in low for marker in BLOCK_MARKERS)


class RawLog:
    """Gzipped copies of fetched pages under root/YYYY-MM-DD/, pruned after ``keep_days``."""

    def __init__(self, root: Path, enabled: bool = True, keep_days: int = 14):
        self.root = Path(root)
        self.enabled = enabled
        self.keep_days = keep_days

    def write(self, platform: str, tag: str, body: str | dict, now: datetime | None = None) -> Path | None:
        """Gzip the body to root/YYYY-MM-DD/HHMMSS_<platform>_<tag>.{html,json}.gz."""
        if not self.enabled:
            return None
        now = now or datetime.now()
        tag = re.sub(r"[^A-Za-z0-9_-]+", "-", tag)[:60]
        day_dir = self.root / now.date().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        suffix = "json" if isinstance(body, dict) else "html"
        path = day_dir / f"{now.strftime('%H%M%S')}_{platform}_{tag}.{suffix}.gz"
        data = json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(data)
        return path

    def prune(self, today: date) -> int:
        """Delete date directories older than keep_days; return how many."""
        removed = 0
        if not self.root.exists():
            return removed
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            try:
                day = date.fromisoformat(d.name)
            except ValueError:
                continue
            if (today - day).days > self.keep_days:
                for f in d.iterdir():
                    f.unlink(missing_ok=True)
                d.rmdir()
                removed += 1
        return removed


class _Paced:
    """A pause of delay_s to 2*delay_s between requests, like one person browsing."""

    def _pace(self) -> None:
        if self._last is not None:
            wait = self.delay_s + self.rng.uniform(0, self.delay_s) - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()


class ProxyFetcher(_Paced):
    def __init__(self, user: str, password: str, *, country: str = "PT", delay_s: float = 5.0,
                 timeout_s: float = 30, raw_log: RawLog | None = None,
                 client: httpx.Client | None = None, headers: dict | None = None,
                 sleep=time.sleep, clock=time.monotonic, rng: random.Random | None = None):
        self.user = user
        self.password = password
        self.country = country
        self.delay_s = delay_s
        self.raw_log = raw_log
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.bytes_used = 0
        self._last: float | None = None
        self._client = client or httpx.Client(proxy=self.proxy_url(), headers=headers or DEFAULT_HEADERS,
                                              timeout=timeout_s, follow_redirects=True)

    def proxy_url(self) -> str:
        pass_part = self.password + (f"_country-{self.country}" if self.country else "")
        return f"http://{quote(self.user, safe='')}:{quote(pass_part, safe='')}@{EVOMI_PROXY_HOST}"

    def get(self, url: str, *, tag: str, platform: str = "page") -> str:
        self._pace()
        try:
            r = self._client.get(url)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"fetch failed: {e}") from e
        # Bytes on the wire (compressed); a response built in memory has none, so its body counts.
        self.bytes_used += r.num_bytes_downloaded or len(r.content)
        if r.status_code in (403, 429, 503):
            raise SourceUnavailable(f"blocked {r.status_code}")
        if r.status_code >= 400:
            raise SourceUnavailable(f"http {r.status_code}")
        if self.raw_log is not None:
            self.raw_log.write(platform, tag, r.text)
        return r.text

    def close(self) -> None:
        self._client.close()


def _launch_playwright(proxy: dict, engine: str = "playwright"):
    """Start Playwright (or Patchright) and a headed Chromium. Returns (playwright, browser, page)."""
    if engine == "patchright":
        from patchright.sync_api import sync_playwright  # labkit[stealth]
    else:
        from playwright.sync_api import sync_playwright  # labkit[scrape]; heavy, so only here

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=False, proxy=proxy)
        context = browser.new_context(locale="pt-PT", timezone_id="Europe/Lisbon",
                                      viewport={"width": 1366, "height": 900})
        return pw, browser, context.new_page()
    except Exception:
        pw.stop()
        raise


class BrowserFetcher(_Paced):
    """One headed browser and one Evomi session (one exit IP) per fetcher, started on the first get."""

    def __init__(self, user: str, password: str, *, country: str = "PT", delay_s: float = 5.0,
                 raw_log: RawLog | None = None, ready_selector: str | None = None,
                 block_resources: tuple[str, ...] = (), allow_hosts: tuple[str, ...] = (),
                 engine: str = "playwright",
                 launch: Callable[..., tuple] = _launch_playwright,
                 sleep=time.sleep, clock=time.monotonic, rng: random.Random | None = None):
        self.user = user
        self.password = password
        self.country = country
        self.delay_s = delay_s
        self.raw_log = raw_log
        self.ready_selector = ready_selector
        self.block_resources = tuple(block_resources)
        self.allow_hosts = tuple(h.lower() for h in allow_hosts)
        if engine not in ("playwright", "patchright"):
            raise ValueError(f"unknown browser engine {engine!r}")
        self.engine = engine
        # A custom launcher (tests, price-scout's) takes just the proxy unless an engine is asked for.
        self.launch = launch if engine == "playwright" else functools.partial(launch, engine=engine)
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.session = "".join(self.rng.choices(string.ascii_lowercase + string.digits, k=10))
        self.bytes_used = 0
        self._finished: list = []
        self._last: float | None = None
        self._pw = self._browser = self._page = None

    def proxy(self) -> dict:
        suffix = (f"_country-{self.country}" if self.country else "") + f"_session-{self.session}"
        return {"server": EVOMI_PROXY_SERVER, "username": self.user, "password": self.password + suffix}

    def _ensure(self):
        if self._page is None:
            try:
                self._pw, self._browser, self._page = self.launch(self.proxy())
                if self.block_resources or self.allow_hosts:
                    self._page.route("**/*", lambda route: route.continue_() if self.wanted(route.request)
                                     else route.abort())
                self._page.on("requestfinished", lambda request: self._finished.append(request))  # Playwright wraps Python callables only
            except Exception as e:  # noqa: BLE001 - no browser means the source is unavailable
                self.close()
                raise SourceUnavailable(f"browser did not start: {e}") from e
        return self._page

    def wanted(self, request) -> bool:
        """False for a blocked resource type, or a host outside ``allow_hosts`` (subdomains count)."""
        if request.resource_type in self.block_resources:
            return False
        if not self.allow_hosts:
            return True
        host = (urlsplit(request.url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.allow_hosts)

    def _count(self) -> None:
        """Add the finished requests' sizes; Playwright API calls are not made inside its event handler."""
        finished, self._finished[:] = list(self._finished), []
        for request in finished:
            try:
                sizes = request.sizes()
                self.bytes_used += max(0, sizes.get("responseBodySize", 0)) + max(0, sizes.get("responseHeadersSize", 0))
            except Exception:  # noqa: BLE001 - a request without sizes counts as zero
                pass

    def get(self, url: str, *, tag: str, platform: str = "page", raw: bool = False) -> str:
        """The rendered page (DOM after its scripts ran); ``raw=True`` gives the body as served
        instead, with no waiting: for a JSON API that only a browser can reach."""
        self._pace()
        page = self._ensure()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = response.status if response is not None else 0
            if status in (403, 429, 503):
                raise SourceUnavailable(f"blocked {status}")
            if status >= 400:
                raise SourceUnavailable(f"http {status}")
            if raw:
                html = response.text()
            else:
                if self.ready_selector:
                    try:
                        page.wait_for_selector(self.ready_selector, state="attached", timeout=READY_TIMEOUT_MS)
                    except Exception:  # noqa: BLE001 - the parser reports a page without its data
                        pass
                page.wait_for_timeout(SETTLE_MS)
                html = page.content()
        except SourceUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - timeouts, proxy errors, a crashed tab
            raise SourceUnavailable(f"browser fetch failed: {type(e).__name__}: {str(e)[:200]}") from e
        finally:
            self._count()
        if self.raw_log is not None:
            self.raw_log.write(platform, tag, html)
        return html

    def close(self) -> None:
        for closer in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001 - closing must never fail a run
                    pass
        self._pw = self._browser = self._page = None
