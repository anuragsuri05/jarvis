"""Everything FRIDAY can do on the computer. Risky tools require the user's approval in the UI."""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from . import macos


@dataclass
class Tool:
    name: str
    args: str
    desc: str
    fn: Callable[..., Awaitable[str]]
    confirm: bool = False


KEY_CODES = {"enter": 36, "return": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "esc": 53,
             "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119,
             "pageup": 116, "pagedown": 121, "f5": 96}
MODIFIERS = {"cmd": "command", "command": "command", "shift": "shift", "alt": "option", "option": "option",
             "opt": "option", "ctrl": "control", "control": "control"}


def _clip(text: str, n: int = 6000) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + f"\n…[{len(text) - n} more chars truncated]"


def html_to_text(raw: str) -> str:
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()
    return f"# {html.unescape(title.group(1).strip())}\n\n{text}" if title else text


class Tools:
    def __init__(self, friday):
        self.f = friday
        t = Tool
        self.tools = {x.name: x for x in [
            t("see_screen", '{"question": str}', "Take a fresh screenshot and ask a vision model about it.", self.see_screen),
            t("open_app", '{"name": str}', "Open or focus a Mac application by name.", self.open_app),
            t("open_url", '{"url": str}', "Open a URL in the default browser.", self.open_url),
            t("web_search", '{"query": str}', "Search the web; returns titles, links and snippets.", self.web_search),
            t("read_url", '{"url": str}', "Fetch a web page and return its readable text.", self.read_url),
            t("run_shell", '{"command": str}', "Run a zsh command on the Mac and return output.", self.run_shell, True),
            t("applescript", '{"script": str}', "Run AppleScript to control apps (Music, Finder, Mail, Calendar, Notes, System Events...).", self.applescript, True),
            t("read_file", '{"path": str}', "Read a text file.", self.read_file),
            t("list_dir", '{"path": str}', "List a directory.", self.list_dir),
            t("write_file", '{"path": str, "content": str}', "Create or overwrite a text file.", self.write_file, True),
            t("type_text", '{"text": str}', "Type text into the focused app (via paste).", self.type_text, True),
            t("press_keys", '{"keys": str}', 'Press a key combo in the focused app, e.g. "cmd+t", "enter", "cmd+shift+4".', self.press_keys, True),
            t("click", '{"x": int, "y": int}', "Click at screen coordinates (points). Last resort.", self.click, True),
            t("clipboard_get", "{}", "Read the clipboard.", self.clipboard_get),
            t("clipboard_set", '{"text": str}', "Put text on the clipboard.", self.clipboard_set),
            t("remember", '{"fact": str, "category": str}', "Save a durable fact about the user to long-term memory.", self.remember),
            t("recall", '{"query": str}', "Search long-term memory: facts about the user and past screen activity.", self.recall),
            t("activity_log", '{"hours": float}', "What the user did on the computer in the last N hours.", self.activity_log),
            t("journal", '{"date": "YYYY-MM-DD"}', "Daily journal/summary of a given day.", self.journal),
            t("set_reminder", '{"text": str, "minutes": float}', "Remind the user after N minutes (notification + voice).", self.set_reminder),
            t("notify", '{"text": str}', "Show a macOS notification.", self.notify),
            t("speak", '{"text": str}', "Say something out loud.", self.speak),
            t("set_volume", '{"percent": int}', "Set system output volume 0-100.", self.set_volume),
            t("system_info", "{}", "Date/time, battery, disk space, uptime, frontmost app.", self.system_info),
        ]}

    def describe(self) -> str:
        return "\n".join(f"- {t.name} {t.args}: {t.desc}{' [asks user approval]' if t.confirm else ''}"
                         for t in self.tools.values())

    async def call(self, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"Unknown tool '{name}'. Available: {', '.join(self.tools)}"
        if not isinstance(args, dict):
            return "Error: args must be a JSON object."
        if tool.confirm and self.f.cfg["agent"]["confirm_actions"]:
            if not await self.f.confirm(name, args):
                return "The user DENIED this action. Do not retry it; ask or propose an alternative."
        try:
            return _clip(str(await tool.fn(**args)))
        except TypeError as e:
            return f"Bad arguments for {name}: {e}. Expected {tool.args}"
        except Exception as e:
            return f"Error in {name}: {type(e).__name__}: {e}"

    # --- perception ---
    async def see_screen(self, question: str = "Describe what is on the screen in detail.") -> str:
        img = await macos.screenshot(max_width=1600)
        if img is None:
            return "Could not capture the screen — Screen Recording permission is missing."
        text, brain = await self.f.brain.complete(
            "vision", [{"role": "user", "content": f"This is the user's Mac screen. {question}"}], images=[img])
        return f"[seen by {brain}] {text}"

    # --- apps & web ---
    async def open_app(self, name: str) -> str:
        code, out, err = await macos.run("open", "-a", name)
        return f"Opened {name}." if code == 0 else f"Failed: {err.strip()}"

    async def open_url(self, url: str) -> str:
        if not re.match(r"^[a-z][a-z0-9+.-]*:", url):
            url = "https://" + url
        code, _, err = await macos.run("open", url)
        return f"Opened {url}." if code == 0 else f"Failed: {err.strip()}"

    async def web_search(self, query: str, max_results: int = 6) -> str:
        def _search():
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            return list(DDGS().text(query, max_results=max_results))

        results = await asyncio.to_thread(_search)
        if not results:
            return "No results."
        return "\n\n".join(f"{r.get('title')}\n{r.get('href')}\n{r.get('body')}" for r in results)

    async def read_url(self, url: str) -> str:
        if not url.startswith("http"):
            url = "https://" + url
        r = await self.f.brain.client.get(url, follow_redirects=True, timeout=30,
                                          headers={"User-Agent": "Mozilla/5.0 (Macintosh) FRIDAY/1.0"})
        if "html" in r.headers.get("content-type", ""):
            return _clip(html_to_text(r.text), 8000)
        return _clip(r.text, 8000)

    # --- system ---
    async def run_shell(self, command: str) -> str:
        code, out, err = await macos.run("/bin/zsh", "-lc", command, timeout=120)
        return f"exit {code}\n{_clip(out, 5000)}\n{('stderr: ' + _clip(err, 1500)) if err.strip() else ''}"

    async def applescript(self, script: str) -> str:
        return await macos.osascript(script, timeout=60) or "(done, no output)"

    async def read_file(self, path: str) -> str:
        p = Path(path).expanduser()
        return _clip(p.read_text(errors="replace"), 10000)

    async def list_dir(self, path: str = "~") -> str:
        p = Path(path).expanduser()
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]
        return "\n".join(f"{'📁' if i.is_dir() else '📄'} {i.name}" for i in items) or "(empty)"

    async def write_file(self, path: str, content: str) -> str:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} chars to {p}"

    async def type_text(self, text: str) -> str:
        old = await macos.clipboard_get()
        await macos.clipboard_set(text)
        await macos.osascript('tell application "System Events" to keystroke "v" using command down')
        await asyncio.sleep(0.4)
        await macos.clipboard_set(old)
        return f"Typed {len(text)} chars."

    async def press_keys(self, keys: str) -> str:
        parts = [p.strip().lower() for p in keys.replace(" ", "").split("+") if p.strip()]
        mods = [MODIFIERS[p] + " down" for p in parts if p in MODIFIERS]
        main = [p for p in parts if p not in MODIFIERS]
        if len(main) != 1:
            return "Give exactly one non-modifier key, e.g. cmd+shift+t"
        k = main[0]
        using = f" using {{{', '.join(mods)}}}" if mods else ""
        action = f"key code {KEY_CODES[k]}" if k in KEY_CODES else f'keystroke "{k}"'
        await macos.osascript(f'tell application "System Events" to {action}{using}')
        return f"Pressed {keys}."

    async def click(self, x: int, y: int) -> str:
        import pyautogui

        await asyncio.to_thread(pyautogui.click, int(x), int(y))
        return f"Clicked ({x}, {y})."

    async def clipboard_get(self) -> str:
        return await macos.clipboard_get() or "(clipboard empty)"

    async def clipboard_set(self, text: str) -> str:
        await macos.clipboard_set(text)
        return "Copied to clipboard."

    async def notify(self, text: str) -> str:
        await macos.notify(self.f.cfg["name"], text)
        return "Notification shown."

    async def speak(self, text: str) -> str:
        await self.f.speaker.say(text, self.f.cfg["agent"]["voice"])
        return "Spoken."

    async def set_volume(self, percent: int) -> str:
        await macos.osascript(f"set volume output volume {max(0, min(100, int(percent)))}")
        return f"Volume set to {percent}%."

    async def system_info(self) -> str:
        _, batt, _ = await macos.run("pmset", "-g", "batt")
        _, disk, _ = await macos.run("df", "-h", "/")
        _, up, _ = await macos.run("uptime")
        app, title = await macos.frontmost()
        return (f"Now: {dt.datetime.now():%A %d %B %Y, %H:%M}\nFrontmost: {app} — {title}\n"
                f"Battery: {batt.strip()}\nDisk:\n{disk.strip()}\nUptime: {up.strip()}")

    # --- memory ---
    async def remember(self, fact: str, category: str = "general") -> str:
        new = self.f.memory.add_fact(fact, category, "told")
        return "Saved to memory." if new else "Already knew that — reinforced it."

    async def recall(self, query: str) -> str:
        facts = self.f.memory.search_facts(query, 10)
        acts = self.f.memory.search_activity(query, 12)
        out = ["FACTS:"] + ([f"- {f['text']}" for f in facts] or ["- none"])
        out += ["", "PAST ACTIVITY:"] + [
            f"- [{dt.datetime.fromtimestamp(a['ts']):%Y-%m-%d %H:%M}] {a['app']} — {a['title'][:100]}: {a['description'][:200]}"
            for a in acts]
        return "\n".join(out)

    async def activity_log(self, hours: float = 2) -> str:
        acts = self.f.memory.activity_since(time.time() - float(hours) * 3600, 400)
        if not acts:
            return "No activity recorded in that window."
        step = max(1, len(acts) // 60)
        return "\n".join(f"[{dt.datetime.fromtimestamp(a['ts']):%H:%M}] {a['app']} — {a['title'][:90]}: {a['description'][:220]}"
                         for a in acts[::step])

    async def journal(self, date: str = "") -> str:
        from .learner import day_bounds

        day = dt.date.fromisoformat(date) if date else dt.date.today()
        start, end = day_bounds(day)
        notes = self.f.memory.notes_between(start, end + 6 * 3600)
        notes = [n for n in notes if (n["kind"] == "hourly" and n["ts"] < end)
                 or (n["kind"] == "daily" and n["text"].startswith(day.isoformat()))]
        if not notes:
            return f"No journal for {day} yet. Try activity_log instead."
        return "\n".join(f"[{n['kind']}] {n['text']}" for n in notes)

    async def set_reminder(self, text: str, minutes: float) -> str:
        due = time.time() + float(minutes) * 60
        self.f.memory.add_reminder(due, text)
        return f"Reminder set for {dt.datetime.fromtimestamp(due):%H:%M}: {text}"
