"""FRIDAY's central object: owns memory, brain, watcher, learner, tools and the UI event hub."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging

from . import macos
from .agent import Agent
from .config import DATA
from .learner import Learner
from .llm import Brain
from .memory import Memory
from .tools import Tools
from .watcher import Watcher

log = logging.getLogger("friday")


class Hub:
    """Fan-out of events to every connected UI (floating window, browser tabs)."""

    def __init__(self):
        self.clients: set = set()

    async def emit(self, msg: dict) -> None:
        data = json.dumps(msg, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)


class Friday:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.memory = Memory(DATA / "friday.db")
        self.brain = Brain(cfg)
        self.hub = Hub()
        self.speaker = macos.Speaker()
        self.watcher = Watcher(self)
        self.learner = Learner(self)
        self.tools = Tools(self)
        self.agent = Agent(self)
        self.busy = False
        self.busy_label = ""
        self._chat_lock = asyncio.Lock()
        self._confirms: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [asyncio.create_task(c) for c in (self.watcher.run(), self.learner.run(), self._reminders())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self.speaker.stop()
        await self.brain.client.aclose()

    def status(self) -> dict:
        return {
            "type": "status",
            "name": self.cfg["name"],
            "busy": self.busy,
            "busy_label": self.busy_label,
            "watcher": self.watcher.snapshot(),
            "stats": self.memory.stats(),
            "toggles": {
                "watch": not self.watcher.paused and self.cfg["watcher"]["enabled"],
                "speak": self.cfg["agent"]["speak_replies"],
                "confirm": self.cfg["agent"]["confirm_actions"],
            },
            "brains": [b["name"] for b in self.brain.status() if b["configured"]],
        }

    async def push_status(self) -> None:
        await self.hub.emit(self.status())

    async def set_busy(self, busy: bool, label: str = "") -> None:
        self.busy, self.busy_label = busy, label
        await self.push_status()

    async def confirm(self, tool: str, args: dict, timeout: float = 180) -> bool:
        cid = str(next(self._ids))
        fut = asyncio.get_running_loop().create_future()
        self._confirms[cid] = fut
        await self.hub.emit({"type": "confirm", "id": cid, "tool": tool, "args": args})
        await macos.notify(self.cfg["name"], f"Needs your approval: {tool}")
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            await self.hub.emit({"type": "confirm_expired", "id": cid})
            return False
        finally:
            self._confirms.pop(cid, None)

    def resolve_confirm(self, cid: str, ok: bool) -> None:
        fut = self._confirms.get(cid)
        if fut and not fut.done():
            fut.set_result(ok)

    async def chat(self, text: str) -> None:
        async with self._chat_lock:
            await self.set_busy(True, "thinking")
            try:
                reply = await self.agent.handle(text)
            except Exception as e:
                log.exception("chat failed")
                reply = f"⚠️ {e}"
            await self.hub.emit({"type": "reply", "text": reply})
            await self.set_busy(False)
            if self.cfg["agent"]["speak_replies"]:
                await self.speaker.say(reply, self.cfg["agent"]["voice"])

    async def _reminders(self) -> None:
        while True:
            try:
                for r in self.memory.due_reminders():
                    self.memory.finish_reminder(r["id"])
                    await macos.notify(f"{self.cfg['name']} reminder", r["text"])
                    await self.hub.emit({"type": "reply", "text": f"⏰ **Reminder:** {r['text']}"})
                    await self.speaker.say(f"Reminder: {r['text']}", self.cfg["agent"]["voice"])
            except Exception:
                log.exception("reminder loop failed")
            await asyncio.sleep(15)
