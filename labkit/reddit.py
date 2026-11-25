"""Reddit through its public RSS (Atom) feeds, the one door open to a logged-out client.

Lifted from redditcast. Probed 2026-09-27: ``.json`` answers 403, RSS answers 200 at
about 1 request per 60 s. So:

- send an honest User-Agent naming your project (required, there is no default);
- requests from one ``RedditRSS`` are spaced ``min_interval_s`` (65 s) apart;
- a 429 waits for the advertised reset (twice at most); a 403/404 stops at once;
- if Reddit says no, the caller says so. No identity rotation, header games,
  headless browsers or CAPTCHA solving.

``fetch_post(url, user_agent=...)`` reads one post and its top comments from a single
``/r/<sub>/comments/<id>/.rss`` request. App share links (``/r/<sub>/s/<code>``) and
``redd.it/<id>`` shortlinks cost one more paced request per redirect hop.
Reuse one ``RedditRSS`` for several posts so the pacing holds across them.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)
ATOM = {"a": "http://www.w3.org/2005/Atom"}
BASE_URL = "https://www.reddit.com"

IMAGE_HOSTS = ("i.redd.it", "preview.redd.it", "i.imgur.com", "imgur.com")
VIDEO_HOSTS = ("v.redd.it", "youtube.com", "www.youtube.com", "youtu.be")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")
BOT_AUTHORS = {"automoderator", "[deleted]"}
REDDIT_HOSTS = ("reddit.com", "redd.it")          # NEW: subdomains count too
MAX_REDIRECT_HOPS = 3                             # NEW
SORTS = ("hot", "new", "rising", "top", "controversial")
_MULTI = re.compile(r"/?(?:user|u)/([\w-]+)/m/([\w-]+)", re.I)
_SUBS = re.compile(r"/?r/([\w+]+)", re.I)


def source_of(text: str) -> dict:
    """A feed from what a user or agent writes: a multireddit link or path
    (reddit.com/user/X/m/Y, /u/X/m/Y) or subreddits (r/A, r/A+B, "A, B")."""
    t = (text or "").strip()
    m = _MULTI.search(t)
    if m:
        return {"multi": {"user": m.group(1), "name": m.group(2)}}
    m = _SUBS.search(t)
    if m:
        return {"subreddits": [x for x in m.group(1).split("+") if x]}
    subs = [x.strip().lstrip("r/") for x in re.split(r"[,+\s]+", t) if x.strip()]
    if subs and all(re.fullmatch(r"[\w-]+", x) for x in subs):
        return {"subreddits": subs}
    raise ValueError(f"not a subreddit or multireddit: {text!r}")

_PERMALINK = re.compile(r"^/r/([A-Za-z0-9_]+)/comments/([A-Za-z0-9]+)(?:/|$)")   # NEW
_SHARE = re.compile(r"^/r/[A-Za-z0-9_]+/s/[A-Za-z0-9_-]+/?$")                    # NEW
_SHORT = re.compile(r"^/[A-Za-z0-9]+/?$")                                         # NEW
# Where redd.it/<id> lands: a post page with no subreddit in the path. Its feed is
# /comments/<id>/.rss (checked live 2026-10-06; it answers 200, not a redirect).
_BARE = re.compile(r"^/comments/([A-Za-z0-9]+)(?:/|$)")


@dataclass
class Comment:
    author: str                                        # without "/u/"
    body: str                                          # plain text
    links: list[str] = field(default_factory=list)     # absolute http(s) hrefs in the body, in order, no repeats


@dataclass
class Post:
    id: str               # "t3_xxxx"
    rank: int             # 1-based position in the feed (1 for fetch_post)
    subreddit: str        # "LocalLLaMA" (no "r/")
    title: str
    author: str
    permalink: str        # https://www.reddit.com/r/.../comments/<id>/<slug>/
    kind: str             # "self" | "link" | "image" | "video"
    link_url: str | None  # the post's target for link/image/video posts, else None
    selftext: str         # plain text of the post body ("" if none)
    published: str        # ISO timestamp from <published> or <updated>
    links: list[str] = field(default_factory=list)        # absolute http(s) hrefs in the body
    comments: list[Comment] = field(default_factory=list)

    def outbound_links(self) -> list[str]:
        """Links found in the body, then in each comment, in order: no repeats,
        no Reddit links (reddit.com, redd.it and their subdomains), and never ``link_url``."""
        seen = {self.link_url} if self.link_url else set()
        out: list[str] = []
        for url in self.links + [u for c in self.comments for u in c.links]:
            if url not in seen and not is_reddit_url(url):
                seen.add(url)
                out.append(url)
        return out


def _parse(url: str):
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    return urlparse(url)


def _is_reddit_host(host: str) -> bool:
    return host == "reddit.com" or host.endswith(".reddit.com")


def is_reddit_url(url: str) -> bool:
    """True for reddit.com, redd.it and any subdomain (www, old, m, i.redd.it, preview.redd.it...)."""
    host = (_parse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in REDDIT_HOSTS)


def post_path(url: str) -> str | None:
    """``/r/<sub>/comments/<id>`` for a post or comment permalink on any reddit.com host
    (www, old, new, m, np, bare), ignoring slug, query and fragment; None otherwise."""
    u = _parse(url)
    if not _is_reddit_host((u.hostname or "").lower()):
        return None
    m = _PERMALINK.match(u.path)
    if m:
        return f"/r/{m.group(1)}/comments/{m.group(2)}"
    m = _BARE.match(u.path)
    return f"/comments/{m.group(1)}" if m else None


def _short_path(url: str) -> str | None:
    """``/comments/<id>`` for a ``redd.it/<id>`` shortlink, with no request; None otherwise."""
    u = _parse(url)
    if (u.hostname or "").lower() != "redd.it" or not _SHORT.match(u.path):
        return None
    return f"/comments/{u.path.strip('/')}"


def is_redirect_link(url: str) -> bool:
    """An app share link (``reddit.com/r/<sub>/s/<code>``) or a ``redd.it/<id>`` shortlink."""
    u = _parse(url)
    host = (u.hostname or "").lower()
    if host == "redd.it":
        return bool(_SHORT.match(u.path))
    return _is_reddit_host(host) and bool(_SHARE.match(u.path))


class _Content(HTMLParser):
    """One pass over an entry's content HTML.

    Collects the text of ``<div class="md">`` (the post or comment body, with
    paragraph breaks), the absolute hrefs of anchors inside it, and the href of
    the anchor whose text is ``[link]``. Everything outside the md div is
    Reddit's "submitted by" trailer.
    """

    BREAKS = {"p", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"}

    def __init__(self, md_only: bool = True):
        super().__init__(convert_charrefs=True)
        self.md_only = md_only
        self.depth = 0            # >0 while inside the md div
        self.parts: list[str] = []
        self.link: str | None = None
        self.links: list[str] = []
        self._href: str | None = None
        self._anchor: list[str] = []

    def _inside(self) -> bool:
        return self.depth > 0 or not self.md_only

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "md" in (a.get("class") or "").split():
                self.depth = 1
        if tag == "a":
            self._href, self._anchor = a.get("href"), []
            href = self._href or ""
            if self.depth > 0 and href.startswith(("http://", "https://")) and href not in self.links:
                self.links.append(href)
        if tag in self.BREAKS and self._inside():
            self.parts.append("\n\n" if tag in ("p", "blockquote", "pre") else "\n")

    def handle_endtag(self, tag):
        if tag == "div" and self.depth:
            self.depth -= 1
        if tag == "a":
            if "".join(self._anchor).strip() == "[link]":
                self.link = self._href
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._anchor.append(data)
        if self._inside():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        if "&#" in raw or "&amp;" in raw or "&quot;" in raw:   # doubly escaped bodies
            raw = html.unescape(raw)
        lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html_text: str) -> str:
    """An HTML fragment as plain text with paragraph breaks."""
    p = _Content(md_only=False)
    p.feed(html_text)
    p.close()
    return p.text()


def _parse_content(content_html: str) -> tuple[str, str | None, list[str]]:
    p = _Content()
    p.feed(content_html)
    p.close()
    return p.text(), p.link, p.links


def _kind(target: str | None, permalink: str) -> tuple[str, str | None]:
    if not target or target.rstrip("/") == permalink.rstrip("/"):
        return "self", None
    u = urlparse(target)
    host, path = (u.hostname or "").lower(), u.path.lower()
    if host in VIDEO_HOSTS:
        return "video", target
    if host in IMAGE_HOSTS or path.endswith(IMAGE_EXT):
        return "image", target
    if host.endswith("reddit.com") and "/gallery/" in path:
        return "image", target
    return "link", target


def _entries(xml: str) -> list[ET.Element]:
    return ET.fromstring(xml).findall("a:entry", ATOM)


def _field(e: ET.Element, path: str) -> str:
    el = e.find(path, ATOM)
    return (el.text or "").strip() if el is not None else ""


def _author(e: ET.Element) -> str:
    return _field(e, "a:author/a:name").removeprefix("/u/")


def _post(e: ET.Element, rank: int) -> Post:
    cat = e.find("a:category", ATOM)
    link = e.find("a:link", ATOM)
    permalink = link.get("href", "") if link is not None else ""
    body, target, links = _parse_content(_field(e, "a:content"))
    kind, link_url = _kind(target, permalink)
    return Post(
        id=_field(e, "a:id"), rank=rank,
        subreddit=cat.get("term", "") if cat is not None else "",
        title=html.unescape(_field(e, "a:title")), author=_author(e),
        permalink=permalink, kind=kind, link_url=link_url, selftext=body,
        published=_field(e, "a:published") or _field(e, "a:updated"), links=links)


def parse_listing(xml: str) -> list[Post]:
    return [_post(e, rank) for rank, e in enumerate(_entries(xml), start=1)]


def parse_comments(xml: str, limit: int) -> list[Comment]:
    out = []
    for e in _entries(xml):
        if not _field(e, "a:id").startswith("t1_"):
            continue                                  # the post itself (t3_)
        author = _author(e)
        body, _, links = _parse_content(_field(e, "a:content"))
        words = re.sub(r"https?://\S+", "", body).split()
        if (author.lower() in BOT_AUTHORS or body in ("[deleted]", "[removed]")
                or "i am a bot" in body.lower() or len(words) < 3):
            continue                                  # bots, removed, bare gif/image links
        out.append(Comment(author, body, links))
        if len(out) >= limit:
            break
    return out


def parse_post(xml: str, comments: int = 8) -> Post:            # NEW
    """The post (first ``t3_`` entry) of a ``/comments/<id>/.rss`` feed, with up to
    ``comments`` top comments. Raises PostUnreadable on bad XML or a feed without the post."""
    try:
        entries = _entries(xml)
    except ET.ParseError as e:
        raise PostUnreadable(f"the feed is not valid XML: {e}") from e
    for e in entries:
        if _field(e, "a:id").startswith("t3_"):
            post = _post(e, rank=1)
            post.comments = parse_comments(xml, comments)
            return post
    raise PostUnreadable("the feed has no post entry")


@dataclass
class UserComment:
    subreddit: str        # "LocalLLaMA"
    post_title: str       # the post the comment answers
    text: str             # plain text
    permalink: str
    published: str


def parse_user_comments(xml: str) -> list[UserComment]:
    """A user's comments feed (``/user/<name>/comments/.rss``): one entry per comment."""
    out = []
    for e in _entries(xml):
        body, _, _ = _parse_content(_field(e, "a:content"))
        title = html.unescape(_field(e, "a:title"))
        cat = e.find("a:category", ATOM)
        link = e.find("a:link", ATOM)
        out.append(UserComment(subreddit=cat.get("term", "") if cat is not None else "",
                               post_title=re.sub(r"^/u/\S+ on ", "", title), text=body.strip(),
                               permalink=link.get("href", "") if link is not None else "",
                               published=_field(e, "a:updated") or _field(e, "a:published")))
    return out


class RedditError(Exception):                                     # NEW
    """Base: Reddit refused, or the post could not be read."""


class RedditBlocked(RedditError):                                 # lifted (base class changed)
    """Reddit refused us. Stop; do not work around it."""

    def __init__(self, url: str, status: int, why: str = ""):
        self.url, self.status = url, status
        super().__init__(f"HTTP {status} for {url}" + (f" ({why})" if why else ""))


class PostUnreadable(RedditError):                                # NEW
    """Not a Reddit post link, a link that does not resolve to a post, a feed
    without the post, or a network failure."""


REFUSED_404 = ("Reddit answered with an empty feed. Since about 2026-11-17 it does this to every "
               "feed read without a Reddit login from the lab, so the subreddit or post most likely "
               "exists: Reddit is refusing us, not saying it is missing")


def _why_404(r: httpx.Response) -> str:
    """A 404 with an empty Atom feed and rate-limit headers is Reddit refusing unauthenticated feed
    readers (seen on every read from 2026-11-17, even of subreddits that exist), not a missing page."""
    body = r.text or ""
    if "<feed" in body and "<entry" not in body and r.headers.get("x-ratelimit-used") is not None:
        return REFUSED_404
    return "not found: a private or missing post, multi or subreddit"


class RedditRSS:
    """Paced RSS client. A custom ``client`` must have ``base_url="https://www.reddit.com"``."""

    def __init__(self, user_agent: str, min_interval_s: float = 65.0,
                 client: httpx.Client | None = None, sleep=time.sleep,
                 monotonic=time.monotonic, max_429_retries: int = 2):
        if not (user_agent or "").strip():                                     # NEW
            raise ValueError("Reddit needs an honest User-Agent naming your project")
        self._client = client or httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": user_agent},
            timeout=30, follow_redirects=True)
        self.min_interval_s = min_interval_s
        self._sleep, self._now = sleep, monotonic
        self.max_429_retries = max_429_retries
        self._last: float | None = None
        # (start time, url, status) per attempt; start times are what _pace spaces.
        self.requests: list[tuple[float, str, int]] = []

    def _pace(self) -> None:
        if self._last is not None:
            wait = self.min_interval_s - (self._now() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._now()

    def _get(self, url: str) -> str:
        retried_5xx = False
        for attempt in range(self.max_429_retries + 1):
            self._pace()
            try:
                r = self._client.get(url)
            except httpx.TransportError as e:
                self.requests.append((self._last, url, 0))
                if retried_5xx:
                    raise
                retried_5xx = True
                log.warning("reddit transport error on %s: %s; retrying once", url, e)
                continue
            self.requests.append((self._last, url, r.status_code))
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                raise RedditBlocked(url, 404, _why_404(r))
            if r.status_code == 403:
                raise RedditBlocked(url, 403, "blocked by Reddit")
            if r.status_code == 429:
                if attempt >= self.max_429_retries:
                    break
                reset = float(r.headers.get("x-ratelimit-reset") or 60)
                log.warning("reddit 429 on %s; waiting %.0fs", url, reset + 5)
                self._sleep(reset + 5)
                continue
            if r.status_code >= 500 and not retried_5xx:
                retried_5xx = True
                log.warning("reddit %d on %s; retrying once", r.status_code, url)
                continue
            raise RedditBlocked(url, r.status_code)
        raise RedditBlocked(url, 429, "rate limited after retries")

    @staticmethod
    def listing_url(source: dict, sort: str = "hot", timeframe: str = "week", limit: int = 25) -> str:
        """A feed's RSS path: ``source`` is ``{"multi": {"user", "name"}}`` (a multireddit /
        Custom Feed; public ones only, RSS is logged out) or ``{"subreddits": [...]}``."""
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {SORTS}")
        m = source.get("multi")
        if m:
            base = f"/user/{m['user']}/m/{m['name']}"
        elif source.get("subreddits"):
            base = f"/r/{'+'.join(source['subreddits'])}"
        else:
            raise ValueError(f"multi {source.get('name')!r} has neither 'multi' nor 'subreddits'")
        query = f"?limit={max(1, min(int(limit), 100))}" + (f"&t={timeframe}" if sort in ("top", "controversial") else "")
        return f"{base}/{sort}/.rss{query}"

    @staticmethod
    def top_url(source: dict, timeframe: str = "week") -> str:
        m = source.get("multi")
        if m:
            return f"/user/{m['user']}/m/{m['name']}/top/.rss?t={timeframe}"
        subs = source.get("subreddits")
        if subs:
            return f"/r/{'+'.join(subs)}/top/.rss?t={timeframe}"
        raise ValueError(f"multi {source.get('name')!r} has neither 'multi' nor 'subreddits'")

    def top(self, source: dict, timeframe: str = "week") -> list[Post]:
        return parse_listing(self._get(self.top_url(source, timeframe)))

    def user_comments(self, user: str, limit: int = 25) -> list[UserComment]:
        """A user's latest public comments (one paced request)."""
        return parse_user_comments(self._get(f"/user/{user}/comments/.rss?limit={max(1, min(int(limit), 100))}"))

    def listing(self, source: dict, sort: str = "hot", timeframe: str = "week", limit: int = 25) -> list[Post]:
        """One paced request: a subreddit's, several subreddits' or a multireddit's posts."""
        return parse_listing(self._get(self.listing_url(source, sort, timeframe, limit)))

    def comments(self, post: Post, limit: int) -> list[Comment]:
        path = urlparse(post.permalink).path.rstrip("/")
        return parse_comments(self._get(f"{path}/.rss?sort=top&limit={limit + 5}"), limit)

    def _resolve(self, url: str) -> str:                                        # NEW
        """Follow a share link or redd.it shortlink, one paced request per hop and without
        following redirects automatically, until the Location is a post permalink."""
        start = url = _parse(url).geturl()
        for _ in range(MAX_REDIRECT_HOPS):
            self._pace()
            r = self._client.get(url, follow_redirects=False)
            self.requests.append((self._last, url, r.status_code))
            if r.status_code == 403:
                raise RedditBlocked(url, 403, "blocked by Reddit")
            if r.status_code == 429:
                raise RedditBlocked(url, 429, "rate limited")
            location = r.headers.get("location")
            if not (300 <= r.status_code < 400 and location):
                raise PostUnreadable(f"{url} did not redirect to a post (HTTP {r.status_code})")
            url = urljoin(url, location)
            path = post_path(url)
            if path:
                return path
            if not is_reddit_url(url):
                raise PostUnreadable(f"{start} redirects outside Reddit: {url}")
        raise PostUnreadable(f"too many redirects from {start}")

    def post(self, url: str, comments: int = 8) -> Post:                       # NEW
        """One post with up to ``comments`` top comments. Raises RedditBlocked when
        Reddit refuses and PostUnreadable for anything else; never a bare httpx error."""
        try:
            path = post_path(url) or _short_path(url)
            if path is None:
                if not is_redirect_link(url):
                    raise PostUnreadable(f"not a Reddit post link: {url}")
                path = self._resolve(url)
            xml = self._get(f"{path}/.rss?sort=top&limit={comments + 5}")
        except httpx.TransportError as e:
            raise PostUnreadable(f"network error reading {url}: {e}") from e
        return parse_post(xml, comments)


def fetch_post(url: str, *, user_agent: str, comments: int = 8,                 # NEW
               client: httpx.Client | None = None, browser: bool = True,
               rss: "RedditRSS | None" = None) -> Post:
    """Read one Reddit post (title, subreddit, body, link_url, top comments, links).
    Accepts www/old/new/m/np permalinks, comment permalinks, redd.it/<id> and app share
    links /r/<sub>/s/<code>. See ``RedditRSS.post`` for errors.

    His logged-in Chrome comes first (``read_post_via_chrome``): Reddit refuses every logged-out
    feed read from the lab since ~2026-11-17, and waiting out its 429s cost over a minute per post
    before giving up. Only when Chrome cannot read it (lab-browser down: no desktop session, or
    not on the PC) are the public feeds tried, as before. ``browser=False`` or
    ``LAB_BROWSER_MCP=0`` skips Chrome. A feed refusal then carries why Chrome could not help.
    ``rss`` reuses a reader (its pacing, e.g. reddit_mcp's shared one) for the share-link
    resolve and the feed fallback.

    Each call builds its own client (closed when garbage-collected) and its own pacing;
    to read several posts, keep one ``RedditRSS`` so the 65 s spacing holds across them."""
    chrome_why: RedditError | None = None
    rss = rss or RedditRSS(user_agent, client=client)
    if browser and LAB_BROWSER_MCP not in ("", "0") and is_reddit_url(url):
        try:
            # old.reddit does not follow app share codes (/r/x/s/<code>): resolve them first, a
            # plain redirect Reddit still answers logged-out, so Chrome opens the permalink.
            path = post_path(url) or _short_path(url)
            if path is None and is_redirect_link(url):
                path = rss._resolve(url)
            return read_post_via_chrome(path or url, comments)
        except RedditError as why:
            chrome_why = why
            log.info("reddit: Chrome could not read %s (%s); trying the feeds", url, why)
    try:
        return rss.post(url, comments)
    except RedditBlocked as blocked:
        if chrome_why is not None:
            blocked.args = (f"{blocked.args[0]}; his Chrome could not read it either: {chrome_why}",)
        raise


# ── Reddit through his Chrome (2026-11-25) ───────────────────────────────────
# Reddit refuses every logged-out read from the lab since ~2026-11-17 (RSS, JSON, his private
# feed key). His logged-in Chrome on the PC still reads ordinary pages, through lab-browser's
# read_page tool (no model: it opens the page in its own tab and returns the HTML; sites limited
# to reddit.com). redditcast and game-alerts did this on their own; this is the shared one, so
# every fetch_post caller (idea-pipeline, lab-assistant, lab-notes…) gets it. It only answers on
# the PC, with his desktop session and Chrome up (lab-infra desktop auto-login).
import os as _os                                                    # noqa: E402

LAB_BROWSER_MCP = _os.environ.get("LAB_BROWSER_MCP", "http://127.0.0.1:8305/mcp")
CHROME_TIMEOUT_S = 180
OLD_REDDIT = "https://old.reddit.com"


def _old_reddit_url(url: str) -> str:
    """``url`` (a feed, permalink or share link) as its old.reddit page, top comments first."""
    parts = urlparse(url if "://" in url else "https://www.reddit.com" + url)
    path = re.sub(r"/\.rss$|\.rss$", "", parts.path.rstrip("/")) or "/"
    return f"{OLD_REDDIT}{path}/?sort=top"


class _PostPage(HTMLParser):
    """An old.reddit post page: the post's ``thing`` (id, subreddit, author, permalink, time,
    target, title), its body (the md in its expando) and its top-level comments (first md of
    each), each body kept as an HTML fragment for ``_parse_content``."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.meta: dict | None = None
        self.title = ""
        self.body = ""
        self.comments: list[tuple[str, str]] = []
        self._in_title = False
        self._depth = 0
        self._post_depth = 0
        self._comment_depths: list[int] = []
        self._author = ""
        self._took = False
        self._expando = 0
        self._md = 0
        self._for = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if self._md:
            self._buf.append(self.get_starttag_text() or "")
        if tag == "a" and self.meta is not None and "title" in cls and not self.title and not self._comment_depths:
            self._in_title = True
        if tag != "div":
            return
        self._depth += 1
        if "thing" in cls and self.meta is None and a.get("data-type") == "link" and "promoted" not in cls \
                and (a.get("data-fullname") or "").startswith("t3_"):
            self.meta = {k: a.get(f"data-{k}") or "" for k in
                         ("fullname", "subreddit", "author", "permalink", "timestamp", "url")}
            self._post_depth = self._depth
        elif "thing" in cls and a.get("data-type") == "comment":
            self._comment_depths.append(self._depth)
            self._author, self._took = a.get("data-author") or "", False
        elif "expando" in cls and self._post_depth and not self._comment_depths and not self.body:
            self._expando = self._depth
        elif "md" in cls and not self._md:
            if self._expando and not self.body:
                self._md, self._for, self._buf = self._depth, "post", []
            elif len(self._comment_depths) == 1 and not self._took:
                self._md, self._for, self._buf = self._depth, "comment", []

    def handle_startendtag(self, tag, attrs):
        if self._md:
            self._buf.append(self.get_starttag_text() or "")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._md:
            self._buf.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")

    def handle_endtag(self, tag):
        if tag == "a" and self._in_title:
            self._in_title = False
        if self._md and not (tag == "div" and self._depth == self._md):
            self._buf.append(f"</{tag}>")
        if tag != "div":
            return
        if self._md and self._depth == self._md:
            fragment = '<div class="md">' + "".join(self._buf) + "</div>"
            if self._for == "post":
                self.body = fragment
            else:
                self.comments.append((self._author, fragment))
                self._took = True
            self._md = 0
        if self._expando and self._depth == self._expando:
            self._expando = 0
        if self._comment_depths and self._depth == self._comment_depths[-1]:
            self._comment_depths.pop()
        self._depth -= 1


def parse_post_page(page_html: str, comments: int = 8) -> Post:
    """One old.reddit post page as a ``Post``, its comments filtered like the RSS reader's.
    Raises ``PostUnreadable`` when the page holds no post (a login wall, an error page)."""
    p = _PostPage()
    p.feed(page_html)
    p.close()
    if p.meta is None:
        raise PostUnreadable("the page has no Reddit post on it (a login or error page?)")
    meta = p.meta
    permalink = "https://www.reddit.com" + meta["permalink"]
    target = ("https://www.reddit.com" + meta["url"]) if meta["url"].startswith("/") else meta["url"]
    kind, link_url = _kind(target, permalink)
    body, _, links = _parse_content(p.body) if p.body else ("", None, [])
    try:
        published = dt.datetime.fromtimestamp(int(meta["timestamp"]) / 1000, dt.timezone.utc).isoformat()
    except ValueError:
        published = ""
    kept: list[Comment] = []
    for author, fragment in p.comments:
        text, _, comment_links = _parse_content(fragment)
        words = re.sub(r"https?://\S+", "", text).split()
        if (author.lower() in BOT_AUTHORS or text in ("[deleted]", "[removed]")
                or "i am a bot" in text.lower() or len(words) < 3):
            continue
        kept.append(Comment(author, text, comment_links))
        if len(kept) >= comments:
            break
    return Post(id=meta["fullname"], rank=1, subreddit=meta["subreddit"],
                title=" ".join(html.unescape(p.title).split()), author=meta["author"],
                permalink=permalink, kind=kind, link_url=link_url, selftext=body,
                published=published, links=links, comments=kept)


def read_page_via_chrome(url: str, *, client: httpx.Client | None = None,
                         timeout: float = CHROME_TIMEOUT_S) -> str:
    """One reddit.com page's HTML, opened in his Chrome by lab-browser's ``read_page``.
    Raises ``PostUnreadable`` when lab-browser does not answer or could not read it."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_page",
                       "arguments": {"url": url, "sites": ["reddit.com"], "html": True}}}
    http = client or httpx.Client(timeout=timeout)
    try:
        r = http.post(LAB_BROWSER_MCP, json=body, timeout=timeout,
                      headers={"accept": "application/json, text/event-stream"})
        r.raise_for_status()
        result = (r.json().get("result") or {})
    except (httpx.HTTPError, ValueError) as e:
        raise PostUnreadable(f"lab-browser did not answer ({type(e).__name__}): his desktop session "
                             "and Chrome must be up on the PC") from e
    finally:
        if client is None:
            http.close()
    out = (result.get("structuredContent") or {}).get("result") or {}
    if out.get("status") != "done" or not out.get("text"):
        raise PostUnreadable(f"lab-browser could not read the page: "
                             f"{out.get('answer') or out.get('status') or 'no answer'}")
    return out["text"]


def read_post_via_chrome(url: str, comments: int = 8, *, client: httpx.Client | None = None) -> Post:
    """``fetch_post`` through his Chrome: ``url`` as its old.reddit page, parsed exactly, no model."""
    return parse_post_page(read_page_via_chrome(_old_reddit_url(url), client=client), comments)
