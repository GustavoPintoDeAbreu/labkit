"""WhatsApp through a self-hosted WAHA container (the lab's is ``kaya-waha`` on the Pi).

A thin client and a mock twin that share one interface, so a whole app can be
developed and tested through ``MockNotifier`` without a phone:

- ``send_text(text)`` and ``send_voice(ogg_bytes, filename)``; ``send`` is an alias
  of ``send_text``.
- ``WahaNotifier`` retries a refused connection or a 5xx after 5, 20 and 60 s
  (long enough to ride out a WAHA restart), and raises at once on a 4xx, which
  will not fix itself (wrong key, bad chat id). After the last attempt it raises.
- Voice notes must be OGG/Opus; WAHA wants them base64-encoded with
  ``audio/ogg; codecs=opus``.

Merged from flight-deals' notifier and redditcast's voice-note sender.
"""
from __future__ import annotations

import base64
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send_text(self, text: str) -> None: ...

    @abstractmethod
    def send_voice(self, ogg: bytes, filename: str = "voice.ogg") -> None: ...

    def send(self, text: str) -> None:
        self.send_text(text)

    def send_voice_file(self, path: "str | Path") -> None:
        p = Path(path)
        self.send_voice(p.read_bytes(), p.name)


class MockNotifier(Notifier):
    """Captures instead of sending: ``outbox`` holds ``("text", str)`` and ``("voice", filename)``."""

    def __init__(self, echo: bool = True):
        self.echo = echo
        self.outbox: List[Tuple[str, object]] = []

    @property
    def texts(self) -> List[str]:
        return [str(p) for k, p in self.outbox if k == "text"]

    def send_text(self, text: str) -> None:
        self.outbox.append(("text", text))
        if self.echo:
            print("\n--- whatsapp (mock) ---\n" + text + "\n-----------------------\n")

    def send_voice(self, ogg: bytes, filename: str = "voice.ogg") -> None:
        self.outbox.append(("voice", filename))
        if self.echo:
            print(f"[whatsapp mock] voice note {filename} ({len(ogg) / 1e6:.1f} MB)")


class WahaNotifier(Notifier):
    # Seconds to wait before attempts 2, 3 and 4.
    RETRY_DELAYS = (5.0, 20.0, 60.0)

    def __init__(self, base_url: str, chat_id: str, session: str = "default",
                 api_key: Optional[str] = None, timeout_s: float = 60.0,
                 link_preview: Optional[bool] = None,
                 sleep: Optional[Callable[[float], None]] = None):
        if not base_url:
            raise ValueError("a WAHA base_url is required")
        if not chat_id:
            raise ValueError("a WhatsApp chat_id is required")
        self.chat_id = chat_id
        self.session = session
        self.link_preview = link_preview
        self._sleep = sleep            # None: time.sleep, looked up per call (tests patch it)
        headers = {"X-Api-Key": api_key} if api_key else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s)

    def _post(self, path: str, payload: dict) -> None:
        for attempt, delay in enumerate((*self.RETRY_DELAYS, None), start=1):
            try:
                self._client.post(path, json=payload).raise_for_status()
                return
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500 or delay is None:
                    raise
                err: Exception = e
            except httpx.TransportError as e:
                if delay is None:
                    raise
                err = e
            log.warning("WAHA %s failed (attempt %d): %s; retrying in %.0fs", path, attempt, err, delay)
            (self._sleep or time.sleep)(delay)

    def send_text(self, text: str) -> None:
        body = {"session": self.session, "chatId": self.chat_id, "text": text}
        if self.link_preview is None:
            self._post("/api/sendText", body)
            return
        try:
            self._post("/api/sendText", {**body, "linkPreview": self.link_preview})
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 422):
                raise
            log.warning("WAHA refused linkPreview (%d); sending without it", e.response.status_code)
            self._post("/api/sendText", body)

    def send_voice(self, ogg: bytes, filename: str = "voice.ogg") -> None:
        self._post("/api/sendVoice", {
            "session": self.session, "chatId": self.chat_id,
            "file": {"mimetype": "audio/ogg; codecs=opus", "filename": filename,
                     "data": base64.b64encode(ogg).decode("ascii")},
        })
