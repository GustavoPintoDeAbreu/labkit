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


def test_complete_tools_returns_the_tool_calls():
    seen = []
    call = {"id": "c1", "type": "function", "function": {"name": "status", "arguments": "{\"product\": \"kaya\"}"}}

    def handler(req):
        seen.append(json.loads(req.content))
        if len(seen) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                                                                      "tool_calls": [call]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>x</think>All good."}}]})

    llm = broker.LLM("http://b/v1", "qwen-impl", client=httpx.Client(base_url="http://b/v1",
                                                                     transport=httpx.MockTransport(handler)))
    tools = [{"type": "function", "function": {"name": "status", "parameters": {"type": "object"}}}]
    first = llm.complete_tools([{"role": "user", "content": "is kaya up?"}], tools)
    assert first == {"role": "assistant", "content": "", "tool_calls": [call]}
    assert seen[0]["tools"] == tools and seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    second = llm.complete_tools([{"role": "user", "content": "?"}], tools)
    assert second == {"role": "assistant", "content": "All good.", "tool_calls": []}


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


def test_app_name_goes_in_the_user_agent():
    llm = broker.LLM.for_model("qwen-impl", url="http://b", app="subs-llm-translation")
    assert llm._client.headers["User-Agent"] == "subs-llm-translation"
    assert "python-httpx" in broker.LLM.for_model("qwen-impl", url="http://b")._client.headers["User-Agent"]


def test_follow_loaded_uses_a_loaded_capable_model_never_loads_one():
    from labkit.broker import FollowLoaded
    now = {"models": ["qwen-flash-strata-g1"]}
    f = FollowLoaded("qwen-impl", ["qwen-flash-strata-g1", "qwen-flash-g1"], loaded=lambda: now["models"])
    assert f.pick() == "qwen-flash-strata-g1"
    now["models"] = ["qwen-impl", "qwen-flash-g1"]
    assert f.pick() == "qwen-impl"                       # the default when it is there
    now["models"] = ["kaya", "big"]
    assert f.pick() == "qwen-impl"                       # nothing to follow: the default loads
    now["models"] = ["qwen-flash-g1"]
    assert f.llm().model == "qwen-flash-g1" and f.model == "qwen-flash-g1"
