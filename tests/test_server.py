"""HTTP + WebSocket API, end to end through FastAPI (scripted brain, real memory/config on a temp home)."""
import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from friday import config, macos
from friday.llm import Brain
from friday.server import create_app


@pytest.fixture
def client(friday, monkeypatch):
    async def noop(*a, **k):
        return None
    monkeypatch.setattr(macos, "notify", noop)
    monkeypatch.setattr(macos.Speaker, "say", noop)

    async def zero():
        return 0

    async def front():
        return "Code", "x.py"
    monkeypatch.setattr(macos, "idle_seconds", zero)
    monkeypatch.setattr(macos, "frontmost", front)
    monkeypatch.setattr(macos, "screenshot", noop)
    friday.cfg =config.save_user_config({"watcher": {"enabled": False}})  # no real screen access from tests
    with TestClient(create_app(friday), base_url="http://127.0.0.1:8765") as c:
        yield c, friday


LOCAL = {"host": "127.0.0.1:8765"}  # TestClient's websockets ignore base_url and send host=testserver


def recv_until(ws, kind):
    seen = []
    while True:
        m = json.loads(ws.receive_text())
        seen.append(m)
        if m["type"] == kind:
            return m, seen


def test_ui_and_status(client):
    c, f = client
    r = c.get("/")
    assert r.status_code == 200 and "FRIDAY" in r.text and "/ws" in r.text
    s = c.get("/api/status").json()
    assert s["type"] == "status" and s["toggles"]["confirm"] is True and s["busy"] is False


def test_websocket_chat_with_tool_approval(client, tmp_path):
    c, f = client
    target = tmp_path / "plan.md"
    f.brain.push(
        {"thought": "write", "tool": "write_file", "args": {"path": str(target), "content": "# Plan"}},
        {"reply": "Created your plan."},
    )
    with c.websocket_connect("/ws", headers=LOCAL) as ws:
        assert json.loads(ws.receive_text())["type"] == "status"
        assert json.loads(ws.receive_text())["type"] == "history"
        ws.send_text(json.dumps({"type": "chat", "text": "make a plan file"}))
        step, _ = recv_until(ws, "step")
        assert step["tool"] == "write_file"
        conf, _ = recv_until(ws, "confirm")
        assert not target.exists()  # nothing happens before approval
        ws.send_text(json.dumps({"type": "confirm", "id": conf["id"], "ok": True}))
        reply, seen = recv_until(ws, "reply")
        assert reply["text"] == "Created your plan." and target.read_text() == "# Plan"
        assert any(m["type"] == "step_result" and "Wrote" in m["result"] for m in seen)

    # history is replayed to a newly opened window
    with c.websocket_connect("/ws", headers=LOCAL) as ws:
        ws.receive_text()
        hist = json.loads(ws.receive_text())["messages"]
        assert [(m["role"], m["text"]) for m in hist] == [("user", "make a plan file"), ("assistant", "Created your plan.")]


def test_memory_endpoints(client):
    c, f = client
    f.memory.add_fact("The user plays guitar", "interests")
    f.memory.add_activity("Code", "x.py", "Writing tests for FRIDAY")
    f.memory.add_reminder(9e9, "far future")
    m = c.get("/api/memory").json()
    assert m["facts"][0]["text"] == "The user plays guitar"
    assert m["today"][0]["description"] == "Writing tests for FRIDAY"
    assert m["reminders"][0]["text"] == "far future" and m["stats"]["facts"] == 1
    assert c.delete(f"/api/facts/{m['facts'][0]['id']}").json() == {"ok": True}
    assert c.get("/api/memory").json()["facts"] == []

    f.brain.push({"facts": [{"category": "projects", "text": "The user is testing FRIDAY"}], "summary": "s"}, "## Profile")
    assert c.post("/api/learn").json() == {"added": 1}
    assert c.get("/api/memory").json()["profile"].startswith("## Profile")


def test_toggles_persist(client):
    c, f = client
    s = c.post("/api/toggle", json={"key": "watch", "value": False}).json()
    assert f.watcher.paused and s["toggles"]["watch"] is False
    c.post("/api/toggle", json={"key": "speak", "value": True})
    c.post("/api/toggle", json={"key": "confirm", "value": False})
    saved = yaml.safe_load(config.CONFIG_FILE.read_text())
    assert saved["agent"] == {"speak_replies": True, "confirm_actions": False}
    assert f.cfg["agent"]["confirm_actions"] is False
    c.post("/api/toggle", json={"key": "watch", "value": True})
    assert not f.watcher.paused and yaml.safe_load(config.CONFIG_FILE.read_text())["watcher"]["enabled"] is True


def test_settings_keys_models_and_brain_test(client, monkeypatch):
    c, f = client
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    f.brain = Brain(f.cfg)

    def handler(req):
        if req.url.host == "api.groq.com":
            assert req.headers["authorization"] == "Bearer gsk_test"
            return httpx.Response(200, json={"choices": [{"message": {"content": "online"}}]})
        return httpx.Response(401, text="bad key")

    f.brain.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    groq = lambda s: next(p for p in s["providers"] if p["name"] == "groq")  # noqa: E731
    assert groq(c.get("/api/settings").json())["configured"] is False

    s = c.post("/api/settings", json={"keys": {"GROQ_API_KEY": " gsk_test ", "EVIL": "x"},
                                      "models": {"groq": {"model": "llama-test", "vision_model": None, "base_url": "http://evil"},
                                                 "nope": {"model": "x"}}}).json()
    g = groq(s)
    assert g["configured"] is True and g["model"] == "llama-test" and g["vision_model"] is None
    env = config.ENV_FILE.read_text()
    assert "GROQ_API_KEY=gsk_test" in env and "EVIL" not in env
    saved = yaml.safe_load(config.CONFIG_FILE.read_text())
    assert saved["providers"] == {"groq": {"model": "llama-test", "vision_model": None}}  # base_url not writable
    assert "groq" in c.get("/api/status").json()["brains"]

    assert c.post("/api/test_brain", json={"name": "groq"}).json() == {"ok": True, "text": "online"}
    monkeypatch.setenv("GEMINI_API_KEY", "bad")
    r = c.post("/api/test_brain", json={"name": "gemini"}).json()
    assert r["ok"] is False and "401" in r["error"]
    assert c.post("/api/test_brain", json={"name": "zzz"}).json()["ok"] is False
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


# ---------- security: other websites must not be able to drive FRIDAY ----------
EVIL = {"origin": "https://evil.example", **LOCAL}


def test_cross_site_websocket_rejected(client):
    c, f = client
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers=EVIL) as ws:
            ws.receive_text()


def test_cross_site_writes_and_dns_rebinding_rejected(client):
    c, f = client
    assert c.post("/api/toggle", json={"key": "confirm", "value": False}, headers=EVIL).status_code == 403
    assert f.cfg["agent"]["confirm_actions"] is True
    assert c.get("/api/memory", headers={"host": "attacker.example:8765"}).status_code == 403
    ok = {"origin": "http://127.0.0.1:8765", **LOCAL}
    assert c.post("/api/toggle", json={"key": "speak", "value": False}, headers=ok).status_code == 200
    with c.websocket_connect("/ws", headers=ok) as ws:
        assert json.loads(ws.receive_text())["type"] == "status"
