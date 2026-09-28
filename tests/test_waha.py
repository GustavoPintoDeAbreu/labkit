import base64
import json

import httpx
import pytest

from labkit.waha import MockNotifier, WahaNotifier


def waha(responses, **kw):
    """A WahaNotifier whose transport replays ``responses`` (status codes or exception classes)."""
    calls, slept = [], []
    it = iter(responses)

    def handler(request):
        calls.append(json.loads(request.content))
        r = next(it)
        if isinstance(r, type) and issubclass(r, Exception):
            raise r("boom", request=request)
        return httpx.Response(r, json={})

    n = WahaNotifier("http://waha.test", chat_id="1@c.us", api_key="k", sleep=slept.append, **kw)
    n._client = httpx.Client(base_url="http://waha.test", transport=httpx.MockTransport(handler))
    return n, calls, slept


def test_retries_a_refused_connection_then_succeeds():
    n, calls, slept = waha([httpx.ConnectError, 201])
    n.send("hi")
    assert slept == [5.0] and calls[-1] == {"session": "default", "chatId": "1@c.us", "text": "hi"}


def test_gives_up_after_the_last_5xx():
    n, calls, slept = waha([502, 502, 502, 502])
    with pytest.raises(httpx.HTTPStatusError):
        n.send_text("hi")
    assert slept == list(WahaNotifier.RETRY_DELAYS) and len(calls) == 4


def test_4xx_raises_at_once():
    n, calls, slept = waha([401])
    with pytest.raises(httpx.HTTPStatusError):
        n.send_text("hi")
    assert slept == [] and len(calls) == 1


def test_link_preview_and_its_fallback():
    n, calls, _ = waha([422, 201], link_preview=False)
    n.send_text("card")
    assert calls[0]["linkPreview"] is False and "linkPreview" not in calls[1]


def test_voice_body():
    n, calls, _ = waha([201])
    n.send_voice(b"OggS-data", "ai.ogg")
    f = calls[0]["file"]
    assert f["mimetype"] == "audio/ogg; codecs=opus" and f["filename"] == "ai.ogg"
    assert base64.b64decode(f["data"]) == b"OggS-data"


def test_default_sleep_is_time_sleep_looked_up_per_call(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    n, _, _ = waha([503, 201])
    n._sleep = None
    n.send_text("x")
    assert slept == [5.0]


def test_mock_and_validation(tmp_path, capsys):
    m = MockNotifier()
    m.send("a")
    (tmp_path / "v.ogg").write_bytes(b"x")
    m.send_voice_file(tmp_path / "v.ogg")
    assert m.outbox == [("text", "a"), ("voice", "v.ogg")] and m.texts == ["a"]
    assert "a" in capsys.readouterr().out
    with pytest.raises(ValueError):
        WahaNotifier("http://w", chat_id="")
