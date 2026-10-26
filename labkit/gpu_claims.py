"""GPU claims: a project says while it uses a card, and releases it when it is done.

The broker queues its own models, but some work holds a card around it: redditcast's text
to speech runs on GPU0 next to its writer model, for example. Such a project writes a claim
while it works, and a project that would start long GPU work (idea-pipeline's builds) waits
while a claim covers the card it needs. Nothing is ever evicted: a claim only keeps new
work from starting.

A claim is one JSON file per owner in ``$LAB_GPU_CLAIMS`` (default
``~/.local/state/lab/gpu-claims``)::

    {"owner": "redditcast", "gpus": ["gpu0"], "reason": "making the weekly podcast",
     "since": 1793001458.6, "until": 1793006858.6}

``since`` may lie ahead: a booking for scheduled work, so nothing long starts just before
it. ``until`` caps a claim whose owner died without releasing it. Both are epoch seconds.

    from labkit.gpu_claims import claim, release, claims, holding

    with holding("redditcast", reason="making the weekly podcast", ttl_s=3 * 3600):
        ...                                            # released on exit, even on errors
    claims(gpus=["gpu0"])                              # the live ones on GPU0
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional

_OWNER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def claims_dir() -> Path:
    return Path(os.environ.get("LAB_GPU_CLAIMS") or Path.home() / ".local" / "state" / "lab" / "gpu-claims")


def _path(owner: str, directory: Optional[Path]) -> Path:
    if not _OWNER.match(owner):
        raise ValueError(f"bad claim owner {owner!r}: lowercase letters, digits, '.', '_' or '-'")
    return (directory or claims_dir()) / f"{owner}.json"


def claim(owner: str, *, gpus: Iterable[str] = ("gpu0",), reason: str = "", ttl_s: float = 3600,
          since: Optional[float] = None, directory: Optional[Path] = None,
          now: Callable[[], float] = time.time) -> Path:
    """Write (or replace) ``owner``'s claim. It counts from ``since`` (default now) for
    ``ttl_s`` seconds, unless released first."""
    start = now() if since is None else since
    path = _path(owner, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"owner": owner, "gpus": sorted(set(gpus)), "reason": reason,
              "since": start, "until": start + ttl_s}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(path)
    return path


def release(owner: str, *, directory: Optional[Path] = None) -> None:
    """Remove ``owner``'s claim; nothing happens if there is none."""
    _path(owner, directory).unlink(missing_ok=True)


def claims(*, gpus: Optional[Iterable[str]] = None, directory: Optional[Path] = None,
           now: Callable[[], float] = time.time) -> List[dict]:
    """The claims in force now (started, not expired), optionally only those on ``gpus``.
    Unreadable files are skipped, never deleted: they belong to their owners."""
    folder = directory or claims_dir()
    wanted = set(gpus) if gpus is not None else None
    moment = now()
    found = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            record = json.loads(path.read_text())
            live = float(record["since"]) <= moment < float(record["until"])
            cards = set(record["gpus"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if live and (wanted is None or cards & wanted):
            found.append(record)
    return found


@contextmanager
def holding(owner: str, *, gpus: Iterable[str] = ("gpu0",), reason: str = "", ttl_s: float = 3600,
            directory: Optional[Path] = None) -> Iterator[Path]:
    """A claim for the duration of a ``with`` block."""
    path = claim(owner, gpus=gpus, reason=reason, ttl_s=ttl_s, directory=directory)
    try:
        yield path
    finally:
        release(owner, directory=directory)
