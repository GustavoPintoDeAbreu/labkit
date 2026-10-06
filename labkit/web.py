"""Readable text from a web page, for LLM prompts and digests.

Lifted from redditcast (articles.py). ``fetch_page`` never raises on a bad page:
a network error, a non-200, a non-HTML content type, a skipped host (Reddit itself,
image and video hosts) or an unreadable page all give ``None``. Bodies are read up to
``max_bytes``; text is cut to ``max_chars`` at a sentence or paragraph boundary.

A GitHub repository link (``github.com/<owner>/<repo>``) returns the repo's README as
raw Markdown from the GitHub REST API (``GET /repos/{owner}/{repo}/readme`` with
``Accept: application/vnd.github.raw+json``, unauthenticated); if that fails the
page itself is read.

Extraction uses trafilatura, an optional dependency: install ``labkit[web]``. Without
it ``extract_text`` raises ImportError, the one exception ``fetch_page`` lets through.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx

# Domains whose pages are not articles (media, or Reddit itself). Subdomains
# count too: www.reddit.com, m.youtube.com, i.imgur.com.
SKIP_HOSTS = ("reddit.com", "redd.it", "imgur.com", "youtube.com", "youtu.be")
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "application/vnd.github.raw+json"
# First path segments on github.com that are not repository owners.
_GITHUB_NOT_OWNERS = {"orgs", "users", "settings", "topics", "features", "marketplace",
                      "sponsors", "explore", "collections", "trending", "about", "pricing",
                      "login", "search", "notifications", "apps", "enterprise", "security", "site"}


def skipped_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    text = text[:max_chars]
    for delimiter in ("\n\n", ". ", "! ", "? ", "\n", "."):
        idx = text.rfind(delimiter)
        if idx > max_chars * 0.5:  # at least halfway
            return text[:idx].rstrip()
    return text


def extract_text(html_text: str, max_chars: int = 6000) -> str | None:
    import trafilatura  # optional: labkit[web]

    text = trafilatura.extract(html_text, include_comments=False, include_tables=False)
    if text is None or len(text) < 200:
        return None
    return truncate(text, max_chars)


def github_repo(url: str) -> tuple[str, str] | None:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) != 2 or parts[0].lower() in _GITHUB_NOT_OWNERS:
        return None
    repo = parts[1].removesuffix(".git")
    return (parts[0], repo) if repo else None


def _client(user_agent: str | None = None, timeout_s: float = 20.0) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": user_agent} if user_agent else None,
                        timeout=timeout_s, follow_redirects=True)


def _get_capped(http: httpx.Client, url: str, headers: dict | None, timeout_s: float,
                max_bytes: int, html_only: bool) -> tuple[int, str, str]:
    with http.stream("GET", url, headers=headers, timeout=timeout_s) as r:
        ctype = r.headers.get("content-type", "")
        chunks, size = [], 0
        if r.status_code == 200 and (not html_only or "html" in ctype):
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
        codec = r.charset_encoding or "utf-8"
        status = r.status_code
    raw = b"".join(chunks)[:max_bytes]
    try:
        text = raw.decode(codec, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return status, ctype, text


def fetch_page(url: str, client: httpx.Client | None = None, max_chars: int = 6000, *,
               user_agent: str | None = None, timeout_s: float = 20.0,
               max_bytes: int = 2_000_000) -> str | None:
    try:
        if urlparse(url).scheme not in ("http", "https") or skipped_host(url):
            return None
        http = client or _client(user_agent, timeout_s)
        try:
            repo = github_repo(url)
            if repo:
                status, _, text = _get_capped(http, f"{GITHUB_API}/repos/{repo[0]}/{repo[1]}/readme",
                                              {"Accept": GITHUB_RAW}, timeout_s, max_bytes, html_only=False)
                if status == 200 and text.strip():
                    return truncate(text.strip(), max_chars)
            status, ctype, text = _get_capped(http, url, None, timeout_s, max_bytes, html_only=True)
            if status != 200 or "html" not in ctype:
                return None
            return extract_text(text, max_chars)
        finally:
            if client is None:
                http.close()
    except ImportError:
        raise
    except Exception:
        return None
