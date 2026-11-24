"""A stdio MCP server that reads one Reddit post for coding agents (``labkit[mcp]``).

    labkit-reddit-mcp        # Claude Code: claude mcp add --scope user reddit -- labkit-reddit-mcp

One tool, ``read_reddit_post(url, comments, linked_pages, image)``: the post, its top
comments and the text of the pages it links to (a GitHub repo gives its README), as
Markdown. Agents need it because their own web fetchers are refused by Reddit.

It goes through ``labkit.reddit`` (public RSS only) and keeps its rules across every agent
on the machine:

- one pacing file (``fcntl`` lock) spaces requests 65 s apart for all processes, since each
  agent session starts its own server;
- a result is cached on disk for an hour, so reading the same link again costs no request;
- if Reddit says no (403/429), the tool says so and does not retry over RSS.

Since about 2026-11-17 Reddit refuses every logged-out read from the lab (RSS, JSON, even the
owner's private feed key: it redirects to its login page), and new API keys need Reddit's
approval. So a refusal falls back to **lab-browser** (2026-11-24, the owner's choice): his own
logged-in Chrome, driven read-only by the lab's browser agent (``browse`` on its loopback MCP,
``LAB_BROWSER_MCP``; ``0`` turns the fallback off). It needs his desktop session with Chrome open;
when that is not there, the tool says so. It is never a way around a block on someone else's
behalf: it reads as him, in his browser, what he could read himself.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from labkit import __version__
from labkit.reddit import IMAGE_EXT, IMAGE_HOSTS, Post, RedditError, RedditRSS

USER_AGENT = f"labkit-reddit-mcp/{__version__} (homelab coding agents)"
CACHE_DIR = Path(os.environ.get("LABKIT_REDDIT_CACHE") or Path.home() / ".cache/labkit/reddit")
CACHE_TTL_S = 3600
BODY_CHARS, COMMENT_CHARS, PAGE_CHARS, PAGES_CHARS = 8000, 1500, 6000, 20000
MAX_COMMENTS, MAX_PAGES = 25, 5
IMAGE_BYTES = 8_000_000


class SharedPaceRSS(RedditRSS):
    """RedditRSS whose 65 s spacing holds across processes: the time of the last request
    lives in ``pace_file``, read and written under an exclusive lock (waiters queue on it)."""

    def __init__(self, user_agent: str, pace_file: Path, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, **kw):
        super().__init__(user_agent, sleep=sleep, monotonic=wall, **kw)
        self.pace_file = Path(pace_file)

    def _pace(self) -> None:
        self.pace_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.pace_file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                try:
                    last = float(f.read().strip() or 0)
                except ValueError:
                    last = 0.0
                wait = self.min_interval_s - (self._now() - last)
                if wait > 0:
                    self._sleep(wait)
                self._last = self._now()
                f.seek(0)
                f.truncate()
                f.write(f"{self._last:.3f}")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _cap(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + " […]"


def image_url(post: Post) -> str | None:
    """The picture of an image post on a known image host (galleries and videos give None)."""
    if post.kind != "image" or not post.link_url:
        return None
    u = urlparse(post.link_url)
    host, path = (u.hostname or "").lower(), u.path.lower()
    return post.link_url if host in IMAGE_HOSTS and path.endswith(IMAGE_EXT) else None


def render(url: str, post: Post, pages: list[tuple[str, str]]) -> str:
    """The post as Markdown: header, body, top comments, then each linked page's text."""
    lines = [f"# {post.title}", "",
             f"r/{post.subreddit} · u/{post.author} · {post.published[:10]} · {post.permalink or url}"]
    if post.link_url:
        lines.append(f"The post's {'image' if post.kind == 'image' else post.kind}: {post.link_url}")
    lines += ["", "## Post", "", _cap(post.selftext, BODY_CHARS) or "(no text)"]
    links = post.outbound_links()
    if links:
        lines += ["", "Links in the post and comments:"] + [f"- {link}" for link in links]
    if post.comments:
        lines += ["", "## Top comments", ""]
        lines += ["- u/" + c.author + ": " + _cap(c.body, COMMENT_CHARS).replace("\n", "\n  ")
                  for c in post.comments]
    budget = PAGES_CHARS
    for page_url, text in pages:
        if budget <= 0:
            break
        text = _cap(text, min(PAGE_CHARS, budget))
        budget -= len(text)
        lines += ["", f"## Linked page: {page_url}", "", text]
    lines += ["", "(Read through Reddit's public RSS: no scores, no nested replies.)"]
    return "\n".join(lines)


def _pages(post: Post, linked_pages: int, fetch_page: Callable[[str], str | None]) -> list[tuple[str, str]]:
    candidates = ([post.link_url] if post.kind == "link" and post.link_url else []) + post.outbound_links()
    pages: list[tuple[str, str]] = []
    for link in candidates[:linked_pages * 2]:              # a failed page doesn't cost a slot, up to a point
        if len(pages) >= linked_pages:
            break
        text = fetch_page(link)
        if text:
            pages.append((link, text))
    return pages


def _default_fetch_page(link: str) -> str | None:
    from labkit.web import fetch_page
    return fetch_page(link, max_chars=PAGE_CHARS, user_agent=USER_AGENT)


def read_post(url: str, comments: int = 8, linked_pages: int = 3, *, rss: RedditRSS | None = None,
              fetch_page: Callable[[str], str | None] | None = None, cache_dir: Path | None = None,
              now: Callable[[], float] = time.time) -> dict:
    """``{"text": markdown, "image_url": str | None}`` for one post, from the cache when it is
    fresh. Raises ``RedditError`` (``RedditBlocked`` / ``PostUnreadable``)."""
    comments = max(0, min(int(comments), MAX_COMMENTS))
    linked_pages = max(0, min(int(linked_pages), MAX_PAGES))
    cache_dir = Path(cache_dir or CACHE_DIR)
    key = hashlib.sha1(f"{url.strip()}|{comments}|{linked_pages}".encode()).hexdigest()
    cached = cache_dir / f"{key}.json"
    try:
        hit = json.loads(cached.read_text())
        if now() - hit["at"] < CACHE_TTL_S:
            return {"text": hit["text"], "image_url": hit.get("image_url")}
    except (OSError, ValueError, KeyError):
        pass
    rss = rss or SharedPaceRSS(USER_AGENT, cache_dir / "last")
    post = rss.post(url, comments)
    result = {"text": render(url, post, _pages(post, linked_pages, fetch_page or _default_fetch_page)),
              "image_url": image_url(post)}
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"at": now(), **result}))
    except OSError:
        pass
    return result


LISTING_TTL_S = 600


def read_listing(source: str, sort: str = "hot", limit: int = 15, *, rss: RedditRSS | None = None,
                 cache_dir: Path | None = None, now: Callable[[], float] = time.time) -> str:
    """A feed's posts as markdown (rank, title, subreddit, author, kind, link), cached 10 min."""
    from labkit.reddit import source_of
    src = source_of(source)
    limit = max(1, min(int(limit), 50))
    cache_dir = Path(cache_dir or CACHE_DIR)
    key = hashlib.sha1(f"listing|{json.dumps(src, sort_keys=True)}|{sort}|{limit}".encode()).hexdigest()
    cached = cache_dir / f"{key}.json"
    try:
        hit = json.loads(cached.read_text())
        if now() - hit["at"] < LISTING_TTL_S:
            return hit["text"]
    except (OSError, ValueError, KeyError):
        pass
    rss = rss or SharedPaceRSS(USER_AGENT, cache_dir / "last")
    posts = rss.listing(src, sort=sort, limit=limit)[:limit]
    name = (f"/user/{src['multi']['user']}/m/{src['multi']['name']}" if "multi" in src
            else "r/" + "+".join(src["subreddits"]))
    lines = [f"# {name} — {sort} ({len(posts)} posts)"]
    for p in posts:
        lines.append(f"{p.rank}. {p.title} — r/{p.subreddit}, u/{p.author}, {p.kind}, {p.published[:10]}\n   {p.permalink}")
    text = "\n".join(lines)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"at": now(), "text": text}))
    except OSError:
        pass
    return text


def fetch_image(url: str, client: httpx.Client | None = None) -> tuple[bytes, str] | None:
    """(bytes, format) of a post's picture, at most IMAGE_BYTES; None if it can't be had."""
    # No redirects: image_url() only names known image hosts, and a hop could lead anywhere.
    http = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)
    try:
        with http.stream("GET", url) as r:
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or not ctype.startswith("image/"):
                return None
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > IMAGE_BYTES:
                    return None
            return data, ctype.split("/", 1)[1].split(";")[0]
    except httpx.HTTPError:
        return None
    finally:
        if client is None:
            http.close()


LAB_BROWSER_MCP = os.environ.get("LAB_BROWSER_MCP", "http://127.0.0.1:8305/mcp")


def via_browser(task: str, start_url: str, *, client: httpx.Client | None = None) -> str | None:
    """Ask lab-browser to read a Reddit page in his Chrome. Its answer, or None when the fallback is
    off or lab-browser does not answer at all. A browse that could not run (no desktop session)
    comes back as text saying so."""
    if LAB_BROWSER_MCP in ("", "0"):
        return None
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "browse", "arguments": {"task": task, "start_url": start_url, "sites": ["reddit.com"]}}}
    http = client or httpx.Client(timeout=900)
    try:
        r = http.post(LAB_BROWSER_MCP, json=body,
                      headers={"accept": "application/json, text/event-stream"})
        res = (r.json().get("result") or {}).get("structuredContent") or {}
        res = res.get("result", res)
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if client is None:
            http.close()
    answer = str(res.get("answer") or "").strip()
    if res.get("status") in ("done", "ok", "answered") and answer:
        return f"(Read through lab-browser, in his Chrome: Reddit refuses the lab's logged-out reads.)\n\n{answer}"
    return ("Reddit refuses the lab's logged-out reads, and lab-browser (his Chrome) could not read it either"
            + (f": {answer}" if answer else "") + ". It needs his desktop session with Chrome open on the PC.")


TOOL_DESCRIPTION = """Read one Reddit post: title, body, top comments, and the text of the pages it links to
(a GitHub repo link gives the repo's README). Use it for any reddit.com or redd.it link the user
gives you; generic web fetchers are refused by Reddit.

url: a post or comment link (www/old/m.reddit.com, redd.it/<id>, or an app share link /r/<sub>/s/<code>).
comments: how many top comments (0-25, default 8).
linked_pages: how many linked pages to read and include (0-5, default 3).
image: true to also get the post's picture when it is an image post (only if you can view images).

Reddit allows about one request a minute, so a call may wait up to ~65 s (share links take two
requests). The same link is served from a cache for an hour. If Reddit refuses (it refuses the lab's
logged-out reads since 2026-11), the tool asks lab-browser to read it in his Chrome, which takes
a few minutes and needs his desktop session; otherwise ask the user to paste the post. Don't try
to reach Reddit another way."""


def main() -> None:
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("reddit", version=__version__,
                       instructions="Reddit posts for coding agents, through Reddit's public RSS.")

    @server.tool(name="read_reddit_post", description=TOOL_DESCRIPTION,
                 annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=True))
    def read_reddit_post(url: str, comments: int = 8, linked_pages: int = 3, image: bool = False):
        try:
            result = read_post(url, comments, linked_pages)
        except RedditError as e:
            fallback = via_browser(
                f"Read this Reddit post and report its title, author, subreddit, the full post text and the top "
                f"{comments} comments (author and text, as written; do not summarise). Post: {url}", url)
            return fallback or (f"Could not read {url}: {e}. Ask the user to paste the post's text or a screenshot.")
        if not (image and result["image_url"]):
            return result["text"]
        picture = fetch_image(result["image_url"])
        if picture is None:
            return result["text"] + f"\n\n(Could not download the image: {result['image_url']})"
        return [result["text"], Image(data=picture[0], format=picture[1])]

    @server.tool(name="read_reddit_listing",
                 description="A subreddit's, several subreddits' or a multireddit's (Custom Feed's) posts: rank, "
                             "title, subreddit, author, kind and link. source: r/LocalLLaMA, r/A+B, or a multireddit "
                             "link/path such as reddit.com/user/NAME/m/FEED (public feeds only). sort: hot, new, "
                             "rising, top. limit: 1-50 (default 15). One paced request (~65 s at worst); cached 10 min. "
                             "Then read_reddit_post for a post's body and comments.",
                 annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=True))
    def read_reddit_listing(source: str, sort: str = "hot", limit: int = 15):
        try:
            return read_listing(source, sort, limit)
        except RedditError as e:
            src = source.strip().lstrip("/")
            url = src if src.startswith("http") else f"https://www.reddit.com/{src}/{sort}/"
            fallback = via_browser(
                f"List the first {limit} posts on this Reddit page ({sort}): for each, the rank, title, subreddit, "
                f"author and the post's link. Page: {url}", url)
            return fallback or f"Could not read {source}: {e}."
        except ValueError as e:
            return f"Could not read {source}: {e}."

    server.run("stdio")


if __name__ == "__main__":
    main()
