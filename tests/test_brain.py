"""Brain router against mocked HTTP: fallback order, rate limits, vision routing, payload shapes."""
import json

import httpx

from friday.llm import Brain, LLMError
from tests.conftest import run

CFG = {
    "providers": {
        "a": {"kind": "openai", "base_url": "http://a", "key_env": "A_KEY", "model": "ma", "vision_model": None},
        "b": {"kind": "openai", "base_url": "http://b", "key_env": "B_KEY", "model": "mb", "vision_model": "vb"},
        "nokey": {"kind": "openai", "base_url": "http://n", "key_env": "MISSING_KEY", "model": "mn"},
        "local": {"kind": "ollama", "base_url": "http://local", "model": "ml", "vision_model": "vl"},
    },
    "brain": {"chat": ["nokey", "a", "b", "local"], "vision": ["a", "b", "local"], "screen_vision": ["local"]},
}


def make(monkeypatch, handler):
    monkeypatch.setenv("A_KEY", "ka")
    monkeypatch.setenv("B_KEY", "kb")
    monkeypatch.delenv("MISSING_KEY", raising=False)
    br = Brain(CFG)
    br.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return br


def oai(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_falls_back_past_errors_and_rate_limits(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req.url.host)
        if req.url.host == "a":
            return httpx.Response(429, text="slow down")
        if req.url.host == "b":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"message": {"content": "<think>hmm</think>local answer"}})

    br = make(monkeypatch, handler)
    text, name = run(br.complete("chat", [{"role": "user", "content": "hi"}]))
    assert (text, name) == ("local answer", "local")
    assert seen == ["a", "b", "local"]  # 'nokey' skipped: no API key
    st = {s["name"]: s for s in br.status()}
    assert st["a"]["cooling"] and "rate limited" in st["a"]["last_error"]
    assert not st["nokey"]["configured"]

    # cooled-down providers are skipped on the next call
    seen.clear()
    run(br.complete("chat", [{"role": "user", "content": "again"}]))
    assert seen == ["local"]


def test_all_fail_and_none_configured(monkeypatch):
    br = make(monkeypatch, lambda req: httpx.Response(503, text="down"))
    try:
        run(br.complete("chat", [{"role": "user", "content": "x"}]))
        raise AssertionError("expected LLMError")
    except LLMError as e:
        assert "All brains failed" in str(e) and "a:" in str(e) and "local:" in str(e)

    br2 = make(monkeypatch, lambda req: oai("x"))
    br2.chains = {"chat": ["nokey"]}
    try:
        run(br2.complete("chat", [{"role": "user", "content": "x"}]))
        raise AssertionError("expected LLMError")
    except LLMError as e:
        assert "No brain configured" in str(e)


def test_vision_skips_text_only_and_shapes_payloads(monkeypatch):
    bodies = {}

    def handler(req):
        bodies[req.url.host] = json.loads(req.content)
        return oai("I see code") if req.url.host == "b" else httpx.Response(200, json={"message": {"content": "local"}})

    br = make(monkeypatch, handler)
    text, name = run(br.complete("vision", [{"role": "user", "content": "what?"}], images=[b"\xff\xd8jpeg"]))
    assert name == "b" and "a" not in bodies  # 'a' has no vision model
    body = bodies["b"]
    assert body["model"] == "vb"
    parts = body["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "what?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    run(br.complete("screen_vision", [{"role": "user", "content": "s"}], images=[b"img"], json_mode=True))
    lb = bodies["local"]
    assert lb["model"] == "vl" and lb["messages"][-1]["images"]
    assert lb["format"] == "json" and lb["think"] is False


def test_retries_without_sampling_params(monkeypatch):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if "temperature" in body:
            return httpx.Response(400, text="Unsupported parameter: 'temperature'")
        return oai("ok")

    br = make(monkeypatch, handler)
    br.chains = {"chat": ["a"]}
    assert run(br.complete("chat", [{"role": "user", "content": "x"}]))[0] == "ok"
    assert len(calls) == 2 and "max_tokens" not in calls[1]


def test_list_content_and_auth_header(monkeypatch):
    def handler(req):
        assert req.headers["authorization"] == "Bearer ka"
        return httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "par"}, {"type": "text", "text": "ts"}]}}]})

    br = make(monkeypatch, handler)
    br.chains = {"chat": ["a"]}
    assert run(br.complete("chat", [{"role": "user", "content": "x"}]))[0] == "parts"


def test_retries_dropped_connection_once(monkeypatch):
    n = {"calls": 0}

    def handler(req):
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"message": {"content": "back"}})

    br = make(monkeypatch, handler)
    br.chains = {"chat": ["local"]}
    assert run(br.complete("chat", [{"role": "user", "content": "x"}])) == ("back", "local")
    assert n["calls"] == 2


def test_ollama_down_gives_actionable_errors(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("Connection refused")

    br = make(monkeypatch, handler)
    br.chains = {"chat": ["local"]}
    for expected in ("Ollama isn't running", "cooling down after errors"):
        try:
            run(br.complete("chat", [{"role": "user", "content": "x"}]))
            raise AssertionError("expected LLMError")
        except LLMError as e:
            assert expected in str(e), str(e)
