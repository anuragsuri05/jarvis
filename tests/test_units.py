"""Pure units: JSON parsing, memory store, config, image hashing, html cleanup."""
import io
import time

from PIL import Image

from friday import config, macos
from friday.llm import extract_json, strip_think
from friday.memory import Memory, fts_query
from friday.tools import html_to_text


# ---------- extract_json ----------
def test_extract_plain():
    assert extract_json('{"reply": "hi"}') == {"reply": "hi"}


def test_extract_fenced_and_chatter():
    raw = 'Sure! Here you go:\n```json\n{"tool": "open_app", "args": {"name": "Safari"}}\n```\nDone.'
    assert extract_json(raw) == {"tool": "open_app", "args": {"name": "Safari"}}


def test_extract_with_think_and_braces_in_strings():
    raw = '<think>maybe {not json}</think> ok {"reply": "use {braces} and \\"quotes\\""} trailing'
    assert extract_json(raw) == {"reply": 'use {braces} and "quotes"'}


def test_extract_skips_invalid_first_object():
    assert extract_json('{bad json} then {"a": 1}') == {"a": 1}


def test_extract_none_on_prose():
    assert extract_json("Just a normal sentence.") is None
    assert strip_think("<think>x</think>Hello") == "Hello"


# ---------- memory ----------
def test_memory_facts_dedupe_search_delete(tmp_path):
    m = Memory(tmp_path / "m.db")
    assert m.add_fact("The user codes mostly in Python", "skills")
    assert not m.add_fact("The user codes mostly in Python.", "skills")  # near-dupe → strengthened
    assert m.add_fact("The user lives in Delhi", "identity")
    assert not m.add_fact("ab")  # too short
    facts = m.facts()
    assert len(facts) == 2 and facts[0]["strength"] == 2
    hits = m.search_facts("which language does he code in? python")
    assert hits and "Python" in hits[0]["text"]
    m.delete_fact(hits[0]["id"])
    assert m.search_facts("python") == []  # FTS index updated on delete
    assert m.stats()["facts"] == 1


def test_memory_activity_messages_notes_reminders(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.add_activity("Code", "main.py", "Editing the FRIDAY watcher module")
    m.add_activity("Chrome", "YouTube", "Watching a cricket highlights video")
    assert [a["app"] for a in m.recent_activity(5)] == ["Code", "Chrome"]
    assert m.search_activity("cricket")[0]["app"] == "Chrome"
    assert len(m.activity_since(time.time() - 60)) == 2

    a = m.add_message("user", "hello")
    m.add_message("assistant", "hi")
    assert [x["role"] for x in m.recent_messages(5)] == ["user", "assistant"]
    m.mark_learned([a])
    assert len(m.unlearned_messages()) == 1

    m.add_note("hourly", "Worked on FRIDAY")
    assert m.notes_between(0, time.time() + 1, "hourly")[0]["text"] == "Worked on FRIDAY"

    m.add_reminder(time.time() - 1, "past")
    m.add_reminder(time.time() + 3600, "future")
    due = m.due_reminders()
    assert [r["text"] for r in due] == ["past"]
    m.finish_reminder(due[0]["id"])
    assert m.due_reminders() == [] and len(m.pending_reminders()) == 1

    m.set("k", 5)
    assert m.get("k") == "5" and m.get("missing", "d") == "d"


def test_fts_query_sanitizes():
    assert fts_query('what is "DROP TABLE"; my*project?') == '"drop" OR "table" OR "project"'
    assert fts_query("the and of") == ""


# ---------- config ----------
def test_config_overrides_and_env(home):
    cfg = config.save_user_config({"watcher": {"interval_seconds": 99}, "brain": {"chat": ["ollama"]}})
    assert cfg["watcher"]["interval_seconds"] == 99
    assert cfg["watcher"]["idle_pause_seconds"] == 300  # untouched defaults survive the merge
    assert cfg["brain"]["chat"] == ["ollama"]
    config.save_env({"GROQ_API_KEY": "abc"})
    config.save_env({"GROQ_API_KEY": "xyz", "GEMINI_API_KEY": "g"})
    text = config.ENV_FILE.read_text()
    assert text.count("GROQ_API_KEY") == 1 and "GROQ_API_KEY=xyz" in text
    assert oct(config.ENV_FILE.stat().st_mode)[-3:] == "600"
    config.save_env({"GROQ_API_KEY": "", "GEMINI_API_KEY": ""})
    assert str(config.HOME) != str(config.ROOT)  # tests never touch the real home


# ---------- image hashing ----------
def _jpeg(color, box=None):
    img = Image.new("RGB", (400, 300), color)
    if box:
        Image.Image.paste(img, (0, 0, 0), box)
    b = io.BytesIO()
    img.save(b, "JPEG")
    return b.getvalue()


def test_dhash_detects_change():
    a = macos.dhash(_jpeg("white", (0, 0, 200, 300)))
    b = macos.dhash(_jpeg("white", (0, 0, 200, 300)))
    c = macos.dhash(_jpeg("white", (200, 0, 400, 150)))
    assert macos.hamming(a, b) == 0
    assert macos.hamming(a, c) > 12


def test_html_to_text():
    raw = "<html><head><title>x</title><style>.a{}</style></head><body><h1>Hello</h1><script>evil()</script>" \
          "<p>World &amp; more</p></body></html>"
    text = html_to_text(raw)
    assert text.startswith("# x") and "Hello" in text and "World & more" in text and "evil" not in text and ".a{}" not in text
