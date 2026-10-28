"""Watches the screen, describes what the user is doing, and logs it to memory."""
from __future__ import annotations

import asyncio
import logging
import time

from . import macos

log = logging.getLogger("friday.watcher")

DESCRIBE_PROMPT = """You are FRIDAY, quietly observing your user's Mac screen so you can learn about them and help later.
Active app: {app}
Window: {title}

In 1-2 short sentences, describe what the user is doing right now: the task, the topic, project or file names,
website, and what they seem to be working towards. Be specific and factual.
Never copy passwords, API keys, card numbers, or private message contents."""


class Watcher:
    def __init__(self, friday):
        self.f = friday
        self.paused = False
        self.state = "starting"  # watching | idle | paused | private | no-permission | off
        self.app = ""
        self.title = ""
        self.description = ""
        self._last_key = None
        self._last_hash = None
        self._last_describe = 0.0
        self._lock = asyncio.Lock()  # the loop and an on-demand tick (resume button) must not overlap

    @property
    def cfg(self):
        return self.f.cfg["watcher"]

    def snapshot(self) -> dict:
        return {"state": self.state, "app": self.app, "title": self.title, "description": self.description}

    def _excluded(self, app: str, title: str) -> bool:
        if app in self.cfg["exclude_apps"]:
            return True
        low = title.lower()
        return any(k.lower() in low for k in self.cfg["exclude_title_keywords"])

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("watcher tick failed")
            await asyncio.sleep(self.cfg["interval_seconds"])

    async def _set_state(self, state: str):
        if state != self.state:
            self.state = state
            await self.f.push_status()

    async def tick(self):
        async with self._lock:
            await self._tick()

    async def _tick(self):
        if not self.cfg["enabled"]:
            return await self._set_state("off")
        if self.paused:
            return await self._set_state("paused")
        if await macos.idle_seconds() > self.cfg["idle_pause_seconds"]:
            return await self._set_state("idle")

        app, title = await macos.frontmost()
        if self._excluded(app, title):
            self.app, self.title, self.description = app, "(private)", "Private app — not observed."
            return await self._set_state("private")

        key = (app, title)
        switched = key != self._last_key
        self._last_key = key
        self.app, self.title = app, title

        img = await macos.screenshot()
        if img is None:
            if switched:
                self.f.memory.add_activity(app, title, "")
            return await self._set_state("no-permission")
        await self._set_state("watching")

        h = macos.dhash(img)
        changed = self._last_hash is None or macos.hamming(h, self._last_hash) > 12
        self._last_hash = h
        now = time.time()
        due = now - self._last_describe >= self.cfg["describe_min_seconds"]
        if not (switched and now - self._last_describe > 15) and not (changed and due):
            return

        self._last_describe = now
        try:
            desc, _ = await self.f.brain.complete(
                "screen_vision",
                [{"role": "user", "content": DESCRIBE_PROMPT.format(app=app, title=title or "(none)")}],
                images=[img],
                temperature=0.2,
            )
        except Exception as e:
            log.info("no screen description: %s", e)
            desc = ""
        self.description = desc.strip()[:600]
        self.f.memory.add_activity(app, title, self.description)
        await self.f.hub.emit({"type": "activity", "ts": now, "app": app, "title": title, "description": self.description})
        await self.f.push_status()
