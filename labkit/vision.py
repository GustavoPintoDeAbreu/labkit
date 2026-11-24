"""Describe pictures with the lab's local vision model, through the GPU broker (``labkit[vision]``).

For projects whose own model cannot see images: the idea pipeline's text-only planners (local
Flash-Next, DeepSeek, GLM) get a written description of the photos he sent instead (2026-11-24).
The model is Qwen3-VL 8B behind the broker (``photoshop-vision``, GPU0, llama-server + mmproj).
The image encoding and the message shape are Local Photoshop's prompt helper's
(``backend/app/prompt_helper.py``, in production since 2026-10): a JPEG data URL, long side at most
1024 px, the image first and the text after it. Local Photoshop keeps its own client, which also
places the model on a card and handles evictions.

    describe([Path("photo.jpg")], "What is on this whiteboard?")   # -> text
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import List, Optional, Sequence, Union

import httpx

from labkit.broker import broker_url

MODEL = "photoshop-vision"
MAX_SIDE = 1024
MAX_TOKENS = 900
SYSTEM = ("You describe pictures for someone who cannot see them and will act on your words. Say plainly "
          "what is in the picture: any text exactly as written (code, error messages, names, numbers, "
          "URLs), what it shows (a screen, a diagram, a product, a person's setup), and anything that "
          "looks like the point of sending it. No opinions, no guesses presented as facts.")


class VisionError(Exception):
    """The vision model could not be asked or gave nothing back."""


def data_url(image: Union[Path, bytes], max_side: int = MAX_SIDE) -> str:
    """The picture as a JPEG data URL, long side at most ``max_side`` (needs Pillow)."""
    from PIL import Image
    img = Image.open(io.BytesIO(image) if isinstance(image, bytes) else image)
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def messages(images: Sequence[Union[Path, bytes]], question: str, system: str = SYSTEM) -> List[dict]:
    content: List[dict] = [{"type": "image_url", "image_url": {"url": data_url(i)}} for i in images]
    content.append({"type": "text", "text": question})
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def describe(images: Sequence[Union[Path, bytes]], question: str = "Describe this picture.", *,
             model: str = MODEL, url: Optional[str] = None, app: str = "labkit-vision",
             timeout_s: float = 900, client: Optional[httpx.Client] = None) -> str:
    """The vision model's answer about ``images`` (one request; the broker loads the model)."""
    body = {"model": model, "messages": messages(images, question), "max_tokens": MAX_TOKENS,
            "temperature": 0.2, "stream": False}
    http = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=600), headers={"User-Agent": app})
    try:
        r = http.post(f"{broker_url(url)}/v1/chat/completions", json=body)
        if r.status_code != 200:
            raise VisionError(f"the vision model failed ({r.status_code}): {r.text[:300]}")
        text = (r.json()["choices"][0]["message"].get("content") or "").strip()
    except httpx.HTTPError as e:
        raise VisionError(f"the broker could not be reached: {e}") from e
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise VisionError(f"unexpected reply from the vision model: {e}") from e
    finally:
        if client is None:
            http.close()
    if not text:
        raise VisionError("the vision model gave an empty answer")
    return text
