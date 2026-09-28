"""The lab's GPU broker (llama-swap): one OpenAI-style URL for every local model.

The broker loads a model on its first request, queues concurrent requests and
unloads by rule, so callers never manage GPUs. The first request after a load can
take minutes, hence the long default timeout.

The URL is resolved as: the ``url`` argument, then ``$LAB_BROKER_URL``, then
``http://127.0.0.1:8200`` (the host's view). From a container on the broker's
docker network it is ``http://llm-broker:8080``.

Never call ``/upstream/<model>/...`` just to look: it loads the model.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_URL = "http://127.0.0.1:8200"
_THINK = re.compile(r"<think>.*?</think>", re.S)


def broker_url(url: Optional[str] = None) -> str:
    return (url or os.environ.get("LAB_BROKER_URL") or DEFAULT_URL).rstrip("/")


class LLM:
    """Chat with one broker model. ``base_url`` is the OpenAI root, e.g. ``http://127.0.0.1:8200/v1``."""

    def __init__(self, base_url: str, model: str, timeout_s: float = 900.0,
                 client: Optional[httpx.Client] = None):
        self.model = model
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)

    @classmethod
    def for_model(cls, model: str, url: Optional[str] = None, **kw: Any) -> "LLM":
        return cls(broker_url(url) + "/v1", model, **kw)

    def complete(self, messages: List[Dict[str, str]], *, temperature: float = 0.7,
                 max_tokens: int = 4096, thinking: bool = False, **extra: Any) -> str:
        body = {"model": self.model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens, **extra}
        if not thinking:
            # Qwen3.x thinks by default on llama.cpp; hidden reasoning eats the token budget.
            body["chat_template_kwargs"] = {"enable_thinking": False}
        r = self._client.post("/chat/completions", json=body)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
        return _THINK.sub("", text).strip()

    def chat(self, system: str, user: str, **kw: Any) -> str:
        return self.complete([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)


def running(url: Optional[str] = None, timeout_s: float = 10) -> List[Dict[str, Any]]:
    """What is loaded now (``GET /running``); never loads anything."""
    r = httpx.get(broker_url(url) + "/running", timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    return data.get("running", data) if isinstance(data, dict) else data


def unload(model: str, url: Optional[str] = None, timeout_s: float = 60) -> None:
    """Free one model's VRAM now. Only unload your own model; never evict someone else's."""
    httpx.post(f"{broker_url(url)}/api/models/unload/{model}", timeout=timeout_s).raise_for_status()
