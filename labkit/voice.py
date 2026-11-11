"""How Gustavo actually writes: his recent public Reddit comments as style samples, so any agent
drafting a comment, reply or post in his name can match his voice (short, first person,
opinionated, setup only when it fits) instead of a house style.

    from labkit.voice import samples
    for s in samples("someone", n=5): print(s["subreddit"], s["text"])

The comments are read from Reddit's public RSS (labkit.reddit, paced) and cached locally
(``~/.cache/labkit/voice/<user>.json``, ``LABKIT_VOICE_CACHE``), refreshed when older than a week.
Nothing leaves the machine; the samples are his own public words.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, List, Optional

from labkit.reddit import RedditError, RedditRSS

CACHE_DIR = Path(os.environ.get("LABKIT_VOICE_CACHE") or Path.home() / ".cache/labkit/voice")
MAX_AGE_S = 7 * 86400
USER_AGENT = "labkit-voice/0.12 (homelab writing assistant)"


def _keep(text: str) -> bool:
    """Comments worth imitating: a full sentence or more, not a link or a one-word reply."""
    t = text.strip()
    return len(t) >= 40 and not t.startswith("http")


def samples(user: str, n: int = 20, *, rss: Optional[RedditRSS] = None, cache_dir: Optional[Path] = None,
            now: Callable[[], float] = time.time, max_age_s: float = MAX_AGE_S) -> List[dict]:
    """Up to ``n`` of his recent comments: [{subreddit, post, text}], newest first. A stale or
    missing cache is refreshed with one RSS request; if Reddit refuses, the old cache is used."""
    cache_dir = Path(cache_dir or CACHE_DIR)
    path = cache_dir / f"{user.lower()}.json"
    cached = None
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    if cached is None or now() - cached.get("at", 0) > max_age_s:
        try:
            got = (rss or RedditRSS(USER_AGENT, 65)).user_comments(user, 50)
            items = [{"subreddit": c.subreddit, "post": c.post_title, "text": c.text[:600]}
                     for c in got if _keep(c.text)]
            cached = {"at": now(), "items": items}
            cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached, ensure_ascii=False))
        except (RedditError, OSError):
            if cached is None:
                return []
    return list(cached.get("items", []))[:n]
