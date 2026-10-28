"""Local web server (127.0.0.1 only): serves the UI and a WebSocket for live chat/events."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .config import UI_DIR, load_config, save_env, save_user_config
from .core import Friday
from .learner import day_bounds


def create_app(friday: Friday) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        friday.start()
        yield
        await friday.stop()

    app = FastAPI(lifespan=lifespan)

    # Any website you visit can reach 127.0.0.1, so only trust FRIDAY's own page:
    # the Host header must be ours (blocks DNS rebinding) and, when a browser sends an Origin, it must be ours too.
    port = friday.cfg["server"]["port"]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    def trusted(headers, need_origin_check: bool) -> bool:
        if headers.get("host") not in hosts:
            return False
        origin = headers.get("origin")
        return not (need_origin_check and origin is not None and origin not in origins)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not trusted(request.headers, request.method not in ("GET", "HEAD")):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    async def index():
        return FileResponse(UI_DIR / "index.html")

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        if not trusted(sock.headers, True):
            await sock.close(code=1008)
            return
        await sock.accept()
        friday.hub.clients.add(sock)
        await sock.send_text(json.dumps(friday.status(), default=str))
        history = [{"role": m["role"], "text": m["content"], "ts": m["ts"]} for m in friday.memory.recent_messages(30)]
        await sock.send_text(json.dumps({"type": "history", "messages": history}, default=str))
        try:
            while True:
                msg = json.loads(await sock.receive_text())
                kind = msg.get("type")
                if kind == "chat" and msg.get("text", "").strip():
                    asyncio.create_task(friday.chat(msg["text"].strip()))
                elif kind == "confirm":
                    friday.resolve_confirm(str(msg["id"]), bool(msg.get("ok")))
                elif kind == "stop_speaking":
                    friday.speaker.stop()
        except WebSocketDisconnect:
            pass
        finally:
            friday.hub.clients.discard(sock)

    @app.get("/api/status")
    async def status():
        return friday.status()

    @app.get("/api/memory")
    async def memory():
        start, end = day_bounds(dt.date.today())
        acts = friday.memory.activity_between(start, end)
        return {
            "profile": friday.learner.profile(),
            "facts": friday.memory.facts(500),
            "today": [a for a in acts if a["description"]][-80:],
            "notes": friday.memory.notes_between(start - 7 * 86400, end + 1)[-20:],
            "reminders": friday.memory.pending_reminders(),
            "stats": friday.memory.stats(),
        }

    @app.delete("/api/facts/{fact_id}")
    async def delete_fact(fact_id: int):
        friday.memory.delete_fact(fact_id)
        return {"ok": True}

    @app.post("/api/learn")
    async def learn():
        added = await friday.learner.learn()
        if not added:
            await friday.learner.rebuild_profile()
        return {"added": added}

    @app.post("/api/toggle")
    async def toggle(body: dict):
        key, value = body.get("key"), bool(body.get("value"))
        if key == "watch":
            friday.watcher.paused = not value
            # reflect the change now rather than at the next watcher tick (up to 20s later)
            friday.watcher.state = "starting" if value else "paused"
            if value and not friday.cfg["watcher"]["enabled"]:
                friday.cfg = save_user_config({"watcher": {"enabled": True}})
            if value:
                asyncio.create_task(friday.watcher.tick())
        elif key in ("speak", "confirm"):
            field = {"speak": "speak_replies", "confirm": "confirm_actions"}[key]
            friday.cfg = save_user_config({"agent": {field: value}})
            if key == "speak" and not value:
                friday.speaker.stop()
        await friday.push_status()
        return friday.status()

    @app.get("/api/settings")
    async def get_settings():
        return {"providers": friday.brain.status(), "chains": friday.cfg["brain"]}

    @app.post("/api/settings")
    async def set_settings(body: dict):
        keys = {k: v.strip() for k, v in (body.get("keys") or {}).items() if k.endswith("_API_KEY") and v is not None}
        if keys:
            save_env(keys)
        patch = {}
        if body.get("models"):
            patch["providers"] = {n: {k: v for k, v in m.items() if k in ("model", "vision_model")}
                                  for n, m in body["models"].items() if n in friday.cfg["providers"]}
        if body.get("chains"):
            patch["brain"] = {k: v for k, v in body["chains"].items() if isinstance(v, list)}
        friday.cfg = save_user_config(patch) if patch else load_config()
        friday.brain.reload(friday.cfg)
        await friday.push_status()
        return await get_settings()

    @app.post("/api/test_brain")
    async def test_brain(body: dict):
        p = friday.brain.providers.get(body.get("name"))
        if not p:
            return {"ok": False, "error": "unknown provider"}
        try:
            text = await p.chat(friday.brain.client, [{"role": "user", "content": "Reply with just: online"}])
            p.last_error = None
            return {"ok": True, "text": text[:100]}
        except Exception as e:
            p.last_error = str(e)[:300]
            return {"ok": False, "error": str(e)[:300]}

    return app
