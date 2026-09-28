import json

import httpx

from labkit import broker


def test_url_resolution(monkeypatch):
    monkeypatch.delenv("LAB_BROKER_URL", raising=False)
    assert broker.broker_url() == "http://127.0.0.1:8200"
    monkeypatch.setenv("LAB_BROKER_URL", "http://llm-broker:8080/")
    assert broker.broker_url() == "http://llm-broker:8080"
    assert broker.broker_url("http://x:1") == "http://x:1"
    assert broker.LLM.for_model("m")._client.base_url == httpx.URL("http://llm-broker:8080/v1/")


def test_chat_body_and_think_stripping():
    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>hmm</think>\n Hello "}}]})

    llm = broker.LLM("http://b/v1", "qwen-impl", client=httpx.Client(base_url="http://b/v1",
                                                                     transport=httpx.MockTransport(handler)))
    assert llm.chat("sys", "hi", temperature=0.2, max_tokens=5) == "Hello"
    path, body = seen[0]
    assert path == "/v1/chat/completions" and body["model"] == "qwen-impl"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["max_tokens"] == 5
    llm.complete([{"role": "user", "content": "x"}], thinking=True)
    assert "chat_template_kwargs" not in seen[1][1]


def test_running_and_unload(monkeypatch):
    calls = []

    def fake(method):
        def f(url, timeout):
            calls.append((method, url))
            return httpx.Response(200, json={"running": [{"model": "kaya"}]}, request=httpx.Request(method, url))
        return f

    monkeypatch.setattr(broker.httpx, "get", fake("GET"))
    monkeypatch.setattr(broker.httpx, "post", fake("POST"))
    assert broker.running("http://b") == [{"model": "kaya"}]
    broker.unload("redditcast-writer", "http://b")
    assert calls == [("GET", "http://b/running"), ("POST", "http://b/api/models/unload/redditcast-writer")]
