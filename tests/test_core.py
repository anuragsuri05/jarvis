"""Agent loop, approvals, tools, watcher, learner and reminders — with a scripted brain and mocked macOS."""
import asyncio
import datetime as dt
import io
import json
import time

import pytest
from PIL import Image

from friday import macos
from friday.llm import LLMError
from tests.conftest import run


class FakeWS:
    """Stands in for a browser tab: records events and answers approval prompts."""

    def __init__(self, friday, answer=None):
        self.f, self.answer, self.events = friday, answer, []

    async def send_text(self, data):
        msg = json.loads(data)
        self.events.append(msg)
        if msg["type"] == "confirm" and self.answer is not None:
            asyncio.get_running_loop().call_later(0.05, self.f.resolve_confirm, msg["id"], self.answer)

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]


@pytest.fixture(autouse=True)
def quiet_mac(monkeypatch):
    """Never pop real notifications or speak during tests."""
    async def noop(*a, **k):
        return None
    monkeypatch.setattr(macos, "notify", noop)
    monkeypatch.setattr(macos.Speaker, "say", noop)


def jpeg(color="white", box=None):
    img = Image.new("RGB", (320, 200), color)
    if box:
        img.paste((0, 0, 0), box)
    b = io.BytesIO()
    img.save(b, "JPEG")
    return b.getvalue()


# ================= agent =================
def test_direct_reply_and_context(friday):
    friday.memory.add_fact("The user is learning Rust", "learning")
    friday.memory.add_activity("Code", "lib.rs", "Writing a Rust parser")
    friday.watcher.app, friday.watcher.title = "Code", "lib.rs"
    friday.brain.push({"thought": "greet", "reply": "Hey! Rust parser going well?"})
    assert run(friday.agent.handle("hello")) == "Hey! Rust parser going well?"
    system = friday.brain.calls[0]["messages"][0]["content"]
    assert "The user is learning Rust" in system and "Writing a Rust parser" in system
    assert "RIGHT NOW: Code — lib.rs" in system and "- run_shell" in system and "[asks user approval]" in system
    assert [m["role"] for m in friday.memory.recent_messages()] == ["user", "assistant"]


def test_multistep_tool_use(friday):
    friday.brain.push(
        {"thought": "save", "tool": "remember", "args": {"fact": "The user prefers dark mode", "category": "preferences"}},
        {"thought": "done", "reply": "Got it — dark mode it is."},
    )
    ws = FakeWS(friday)
    friday.hub.clients.add(ws)
    assert run(friday.agent.handle("I prefer dark mode")) == "Got it — dark mode it is."
    assert friday.memory.facts()[0]["text"] == "The user prefers dark mode"
    followup = friday.brain.calls[1]["messages"][-1]["content"]
    assert followup.startswith("TOOL RESULT (remember)") and "Saved to memory" in followup
    assert ws.of("step")[0]["tool"] == "remember" and "Saved" in ws.of("step_result")[0]["result"]


def test_prose_unknown_tool_and_step_limit(friday):
    friday.brain.push("I'm just talking, no JSON here.")
    assert run(friday.agent.handle("hi")) == "I'm just talking, no JSON here."

    friday.brain.push({"tool": "teleport", "args": {}}, {"reply": "Can't teleport."})
    assert run(friday.agent.handle("teleport me")) == "Can't teleport."
    assert "Unknown tool 'teleport'" in friday.brain.calls[-1]["messages"][-1]["content"]

    friday.cfg["agent"]["max_steps"] = 3
    friday.brain.push(*[{"tool": "system_info", "args": {}}] * 5)
    n = len(friday.brain.calls)
    assert "ran out of steps" in run(friday.agent.handle("loop forever"))
    assert len(friday.brain.calls) - n == 3


def test_approval_allow_deny_and_timeout(friday, tmp_path):
    target = tmp_path / "out.txt"
    for answer, exists in ((False, False), (True, True)):
        ws = FakeWS(friday, answer=answer)
        friday.hub.clients = {ws}
        friday.brain.push({"tool": "write_file", "args": {"path": str(target), "content": "hi"}}, {"reply": "ok"})
        run(friday.agent.handle("write it"))
        assert ws.of("confirm")[0]["tool"] == "write_file"
        assert target.exists() == exists
        result = friday.brain.calls[-1]["messages"][-1]["content"]
        assert ("DENIED" in result) != answer

    ws = FakeWS(friday, answer=None)
    friday.hub.clients = {ws}
    assert run(friday.confirm("run_shell", {"command": "ls"}, timeout=0.2)) is False
    assert ws.of("confirm_expired")

    friday.cfg["agent"]["confirm_actions"] = False  # approvals off → runs straight away
    assert "exit 0" in run(friday.tools.call("run_shell", {"command": "echo hi"}))


def test_chat_surfaces_brain_failure(friday):
    ws = FakeWS(friday)
    friday.hub.clients = {ws}
    friday.brain.push(LLMError("All brains failed — ollama: connection refused"))
    run(friday.chat("hello?"))
    assert "⚠️" in ws.of("reply")[-1]["text"]
    assert friday.busy is False and ws.of("status")[-1]["busy"] is False


# ================= tools (real, side-effect safe) =================
def test_file_shell_and_memory_tools(friday, tmp_path):
    friday.cfg["agent"]["confirm_actions"] = False
    call = lambda n, **a: run(friday.tools.call(n, a))  # noqa: E731
    p = tmp_path / "sub" / "note.md"
    assert "Wrote 5 chars" in call("write_file", path=str(p), content="hello")
    assert call("read_file", path=str(p)) == "hello"
    assert "📁 sub" in call("list_dir", path=str(tmp_path))
    out = call("run_shell", command="echo out; echo err >&2; exit 3")
    assert "exit 3" in out and "out" in out and "stderr: err" in out
    assert "Bad arguments" in call("read_file", nope="x")
    assert "Error in read_file" in call("read_file", path=str(tmp_path / "missing"))

    assert call("remember", fact="The user's dog is called Bruno", category="people") == "Saved to memory."
    friday.memory.add_activity("Chrome", "Bruno vet booking", "Booking a vet appointment for Bruno")
    rec = call("recall", query="dog Bruno")
    assert "dog is called Bruno" in rec and "vet appointment" in rec
    assert "vet appointment" in call("activity_log", hours=1)
    assert "No journal" in call("journal", date="2020-01-01")
    friday.memory.add_note("hourly", "Planned the week")
    assert "Planned the week" in call("journal")
    assert "Reminder set for" in call("set_reminder", text="stretch", minutes=10)
    assert friday.memory.pending_reminders()[0]["text"] == "stretch"


def test_system_and_clipboard_tools(friday):
    original = run(macos.clipboard_get())
    try:
        assert run(friday.tools.call("clipboard_set", {"text": "friday-test ✓"})) == "Copied to clipboard."
        assert run(friday.tools.call("clipboard_get", {})) == "friday-test ✓"
    finally:
        run(macos.clipboard_set(original))
    info = run(friday.tools.call("system_info", {}))
    assert "Battery" in info and "Disk" in info and "Now:" in info


def test_keyboard_and_open_tools_build_correct_commands(friday, monkeypatch):
    friday.cfg["agent"]["confirm_actions"] = False
    scripts, cmds = [], []

    async def fake_osa(s, timeout=20):
        scripts.append(s)
        return ""

    async def fake_run(*c, **k):
        cmds.append(c)
        return 0, "", ""

    monkeypatch.setattr(macos, "osascript", fake_osa)
    monkeypatch.setattr(macos, "run", fake_run)
    call = lambda n, **a: run(friday.tools.call(n, a))  # noqa: E731
    call("press_keys", keys="cmd+shift+t")
    call("press_keys", keys="Enter")
    call("press_keys", keys="ctrl + alt + left")
    assert scripts == [
        'tell application "System Events" to keystroke "t" using {command down, shift down}',
        'tell application "System Events" to key code 36',
        'tell application "System Events" to key code 123 using {control down, option down}',
    ]
    assert "exactly one" in call("press_keys", keys="cmd+c+v")
    call("open_url", url="github.com")
    call("open_url", url="mailto:a@b.c")
    call("open_app", name="Notes")
    assert cmds == [("open", "https://github.com"), ("open", "mailto:a@b.c"), ("open", "-a", "Notes")]
    call("set_volume", percent=250)
    assert scripts[-1] == "set volume output volume 100"


def test_see_screen(friday, monkeypatch):
    async def none(**k):
        return None
    monkeypatch.setattr(macos, "screenshot", none)
    assert "Screen Recording permission" in run(friday.tools.call("see_screen", {"question": "?"}))

    async def img(**k):
        return jpeg()
    monkeypatch.setattr(macos, "screenshot", img)
    friday.brain.push("A terminal with a failing test.")
    out = run(friday.tools.call("see_screen", {"question": "What's wrong?"}))
    assert out == "[seen by fake] A terminal with a failing test."
    assert friday.brain.calls[-1]["role"] == "vision" and friday.brain.calls[-1]["images"]


@pytest.mark.network
def test_web_tools(friday):
    res = run(friday.tools.call("web_search", {"query": "python programming language"}))
    assert "http" in res and "Error" not in res, res[:300]
    page = run(friday.tools.call("read_url", {"url": "https://example.com"}))
    assert "Example Domain" in page


# ================= watcher =================
def test_watcher_states_and_logging(friday, monkeypatch):
    w = friday.watcher
    env = {"idle": 0, "front": ("Code", "main.py"), "shot": jpeg(box=(0, 0, 160, 200)), "shots": 0}

    async def idle():
        return env["idle"]

    async def front():
        return env["front"]

    async def shot(**k):
        env["shots"] += 1
        return env["shot"]

    monkeypatch.setattr(macos, "idle_seconds", idle)
    monkeypatch.setattr(macos, "frontmost", front)
    monkeypatch.setattr(macos, "screenshot", shot)
    acts = friday.memory.recent_activity

    friday.brain.push("Editing main.py in VS Code.")
    run(w.tick())
    assert w.state == "watching" and acts()[-1]["description"] == "Editing main.py in VS Code."
    assert friday.brain.calls[-1]["role"] == "screen_vision"

    run(w.tick())  # nothing changed → no new description
    assert len(acts()) == 1 and len(friday.brain.calls) == 1

    env["front"] = ("Chrome", "Docs")  # switched, but <15s since last look → throttled
    run(w.tick())
    assert len(acts()) == 1

    w._last_describe -= 20  # switched + 20s later → describe
    env["front"] = ("Safari", "News")
    friday.brain.push(LLMError("vision down"))
    run(w.tick())
    assert acts()[-1]["app"] == "Safari" and acts()[-1]["description"] == ""  # still logged without vision

    w._last_describe -= 100  # same app, screen changed a lot, past min gap → describe
    env["shot"] = jpeg(box=(160, 0, 320, 100))
    friday.brain.push("Reading news.")
    run(w.tick())
    assert acts()[-1]["description"] == "Reading news."

    n = env["shots"]
    env["front"] = ("1Password", "Vault")
    run(w.tick())
    assert w.state == "private" and env["shots"] == n and w.title == "(private)"
    env["front"] = ("Chrome", "My Bank — login")
    run(w.tick())
    assert w.state == "private"

    env["idle"] = 999
    run(w.tick())
    assert w.state == "idle"
    env["idle"] = 0

    w.paused = True
    run(w.tick())
    assert w.state == "paused"
    w.paused = False

    env["shot"], env["front"] = None, ("Terminal", "zsh")
    run(w.tick())
    assert w.state == "no-permission" and acts()[-1]["app"] == "Terminal"


# ================= learner =================
def test_learner_hourly_profile_and_daily(friday):
    mem, learner = friday.memory, friday.learner
    mem.add_activity("Code", "agent.py", "Building an AI assistant called FRIDAY in Python")
    mid = mem.add_message("user", "I'm a CS student and I want to get an ML internship")
    friday.brain.push(
        {"facts": [{"category": "projects", "text": "The user is building an AI assistant called FRIDAY"},
                   {"category": "goals", "text": "The user wants an ML internship"},
                   {"category": "goals", "text": "The user wants an ML internship."},  # dupe
                   "garbage"],
         "summary": "Worked on FRIDAY."},
        "## Who they are\nA CS student building FRIDAY.",
    )
    assert run(learner.learn()) == 2
    assert friday.brain.calls[0]["json_mode"] is True and friday.brain.calls[0]["role"] == "fast"
    assert "Building an AI assistant" in friday.brain.calls[0]["messages"][0]["content"]
    assert mem.unlearned_messages() == [] and mid
    assert "CS student" in learner.profile()
    assert mem.notes_between(0, time.time() + 1, "hourly")[0]["text"] == "Worked on FRIDAY."

    n = len(friday.brain.calls)
    assert run(learner.learn()) == 0 and len(friday.brain.calls) == n  # nothing new → no LLM call

    # daily journal for yesterday, written once
    y = dt.date.today() - dt.timedelta(days=1)
    ts = dt.datetime.combine(y, dt.time(14)).timestamp()
    mem._x("INSERT INTO activity(ts,app,title,description) VALUES(?,?,?,?)", (ts, "Code", "x", "Debugged the watcher"))
    friday.brain.push("Spent the afternoon debugging the watcher.", "## Profile v2")
    assert "debugging" in run(learner.daily(y))
    assert mem.notes_between(0, time.time() + 1, "daily")[0]["text"].startswith(y.isoformat())
    assert run(learner.daily(y)) is None


def test_learner_tolerates_bad_json(friday):
    friday.memory.add_message("user", "hello")
    friday.brain.push("Sorry, I can't produce JSON today.")
    assert run(friday.learner.learn()) == 0
    assert friday.memory.unlearned_messages() == []


# ================= reminders =================
def test_reminder_fires_once(friday):
    ws = FakeWS(friday)
    friday.hub.clients = {ws}
    friday.memory.add_reminder(time.time() - 1, "drink water")

    async def go():
        t = asyncio.create_task(friday._reminders())
        await asyncio.sleep(0.3)
        t.cancel()

    run(go())
    assert [e["text"] for e in ws.of("reply")] == ["⏰ **Reminder:** drink water"]
    assert friday.memory.pending_reminders() == []


def test_honesty_check_catches_bluffed_actions(friday):
    # bluff → nudged → real tool call → honest reply
    friday.brain.push(
        {"reply": "Reminder set: call mom in 2 minutes."},
        {"tool": "set_reminder", "args": {"text": "call mom", "minutes": 2}},
        {"reply": "Reminder set for call mom."},
    )
    assert run(friday.agent.handle("remind me to call mom in 2 min")) == "Reminder set for call mom."
    assert friday.memory.pending_reminders()[0]["text"] == "call mom"
    assert "SYSTEM CHECK" in friday.brain.calls[1]["messages"][-1]["content"]
    assert all(c["json_mode"] for c in friday.brain.calls)

    # fake TOOL RESULT in prose is caught too; a second bluff is not looped on forever
    n = len(friday.brain.calls)
    friday.brain.push("TOOL RESULT: Python 3.10.12 is the latest.", {"reply": "I can't check right now."})
    assert run(friday.agent.handle("latest python?")) == "I can't check right now."
    assert len(friday.brain.calls) - n == 2

    # after a real tool ran, claiming the action is legitimate — no nudge
    n = len(friday.brain.calls)
    friday.brain.push({"tool": "system_info", "args": {}}, {"reply": "I've checked: battery is fine."})
    assert run(friday.agent.handle("battery?")) == "I've checked: battery is fine."
    assert len(friday.brain.calls) - n == 2


def test_profile_failure_does_not_lose_learning(friday):
    friday.memory.add_message("user", "I run every morning at 6")
    friday.brain.push({"facts": [{"category": "habits", "text": "The user runs every morning at 6"}], "summary": "s"},
                      LLMError("ollama: Server disconnected"))
    assert run(friday.learner.learn()) == 1  # facts kept even though the profile rebuild failed
    assert friday.memory.get("profile_stale") == "1" and not friday.learner.profile()
    friday.brain.push("## Habits\nRuns every morning at 6.")
    assert run(friday.learner.learn()) == 0  # nothing new, but the stale profile is retried
    assert "Runs every morning" in friday.learner.profile() and friday.memory.get("profile_stale") == "0"
