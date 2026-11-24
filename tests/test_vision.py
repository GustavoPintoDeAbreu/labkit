import base64
import io
import json

import httpx
import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from labkit import vision  # noqa: E402


def _png(w=3000, h=1500):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_data_url_is_a_jpeg_at_most_1024_on_the_long_side():
    url = vision.data_url(_png())
    assert url.startswith("data:image/jpeg;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert img.size == (1024, 512)


def test_describe_sends_image_then_text_to_the_vision_model():
    seen = {}
    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": " A red square. "}}]})
    out = vision.describe([_png(10, 10)], "What is this?", url="http://broker",
                          client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out == "A red square."
    assert seen["model"] == "photoshop-vision"
    parts = seen["messages"][1]["content"]
    assert parts[0]["type"] == "image_url" and parts[-1] == {"type": "text", "text": "What is this?"}


def test_errors_are_vision_errors():
    def handler(request):
        return httpx.Response(503, text="loading")
    with pytest.raises(vision.VisionError, match="503"):
        vision.describe([_png(10, 10)], url="http://broker", client=httpx.Client(transport=httpx.MockTransport(handler)))
