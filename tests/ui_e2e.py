"""Drive the real UI in Chrome (Playwright) against a live server with a scripted brain.

    FRIDAY_HOME=$(mktemp -d) .venv/bin/python -m tests.ui_e2e [screenshot_dir]
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

assert os.environ.get("FRIDAY_HOME"), "set FRIDAY_HOME to a temp dir"

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from friday import config, macos  # noqa: E402
from friday.core import Friday  # noqa: E402
from friday.llm import Brain  # noqa: E402
from friday.server import create_app  # noqa: E402
from tests.conftest import FakeBrain  # noqa: E402

SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else "ui_shots")
SHOTS.mkdir(parents=True, exist_ok=True)
PORT = 8799
checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}", flush=True)


async def _noop(*a, **k):
    return None


async def _zero():
    return 0


async def _front():
    return "Code", "train.py"


macos.notify = _noop
macos.Speaker.say = _noop
macos.idle_seconds, macos.frontmost, macos.screenshot = _zero, _front, _noop  # fake screen, never the real one

cfg = config.save_user_config({"watcher": {"enabled": True}, "server": {"port": PORT}})
friday = Friday(cfg)
real = Brain(cfg)
real.client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda r: httpx.Response(200, json={"message": {"content": "online"}})))
fake = FakeBrain()
fake.providers, fake.status, fake.client, fake.reload = real.providers, real.status, real.client, real.reload
friday.brain = fake
friday.memory.add_fact("The user is a CS student", "identity")
friday.memory.add_fact("The user is preparing for ML internships", "goals")
friday.memory.add_fact("The user enjoys cricket", "interests")
friday.memory.add_activity("Code", "train.py", "Training a CNN on CIFAR-10 in PyTorch")
friday.memory.add_activity("Chrome", "LeetCode", "Solving a two-pointer problem in Python")
work = Path(config.HOME) / "work"
work.mkdir()

server = uvicorn.Server(uvicorn.Config(create_app(friday), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.05)

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 400, "height": 640}, device_scale_factor=2)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(f"http://127.0.0.1:{PORT}/")

    sub = page.locator("#sub")
    expect(sub).not_to_have_text("Connecting…", timeout=5000)
    check("connects & shows status", "Connecting" not in sub.inner_text(), f"→ '{sub.inner_text()}'")
    expect(sub).to_have_text("Needs Screen Recording permission", timeout=5000)
    check("watcher state reaches the UI", True)
    check("hello screen with suggestion chips", page.locator(".chip").count() == 5)
    page.screenshot(path=SHOTS / "1_hello.png")

    # --- chip → markdown reply ---
    fake.push({"reply": "Here's what I know:\n- You're a **CS student**\n- Prepping for *ML internships*\n\n"
                        "```python\nprint('hi')\n```\nRun `pytest` next."})
    page.locator(".chip", has_text="What do you know about me?").click()
    expect(page.locator(".msg.assistant")).to_have_count(1, timeout=5000)
    a = page.locator(".msg.assistant .b").last
    check("user bubble shows sent text", page.locator(".msg.user").last.inner_text() == "What do you know about me?")
    check("markdown: list/bold/italic/code", a.locator("li").count() == 2 and a.locator("b").count() == 1
          and a.locator("i").count() == 1 and a.locator("pre code").count() == 1 and a.locator("p code").count() == 1)
    check("typing indicator cleared", page.locator(".typing").count() == 0)

    # --- tool step + approval: ALLOW ---
    target = work / "plan.md"
    fake.push({"thought": "write it", "tool": "write_file", "args": {"path": str(target), "content": "# Week plan"}},
              {"reply": "Done — saved your plan."})
    page.fill("#input", "Write my week plan to a file")
    page.keyboard.press("Enter")
    card = page.locator(".confirm").last
    expect(card).to_be_visible(timeout=5000)
    check("approval card shows tool + content", "write_file" in card.inner_text() and "# Week plan" in card.inner_text())
    check("file NOT written before approval", not target.exists())
    page.screenshot(path=SHOTS / "2_approval.png")
    card.get_by_text("Allow").click()
    expect(page.locator(".msg.assistant").last).to_have_text("Done — saved your plan.", timeout=5000)
    check("file written after Allow", target.exists() and target.read_text() == "# Week plan")
    check("card marked allowed", "Allowed" in card.inner_text())
    step = page.locator(".step").last
    step.click()
    check("step chip expands to show result", "Wrote" in step.locator(".r").inner_text())

    # --- approval: DENY ---
    victim = work / "keep"
    victim.mkdir()
    fake.push({"tool": "run_shell", "args": {"command": f"rm -rf {victim}"}}, {"reply": "Okay, I won't delete it."})
    page.fill("#input", "delete that folder")
    page.locator("#send").click()
    card = page.locator(".confirm").last
    expect(card.get_by_text("Deny")).to_be_visible(timeout=5000)
    card.get_by_text("Deny").click()
    expect(page.locator(".msg.assistant").last).to_have_text("Okay, I won't delete it.", timeout=5000)
    check("Deny keeps the folder", victim.exists())
    check("brain was told it was denied", "DENIED" in fake.calls[-1]["messages"][-1]["content"])

    # --- XSS safety ---
    fake.push({"reply": "<img src=x onerror=\"document.body.dataset.pwned=1\"> [x](javascript:alert(1)) <script>alert(1)</script>"})
    page.fill("#input", "<b>bold?</b>")
    page.keyboard.press("Enter")
    expect(page.locator(".msg.assistant")).to_have_count(4, timeout=5000)
    last = page.locator(".msg.assistant .b").last
    check("model HTML is escaped (no img/script/js link)", last.locator("img, script, a").count() == 0
          and page.evaluate("document.body.dataset.pwned") is None)
    check("user HTML is escaped", page.locator(".msg.user .b").last.inner_text() == "<b>bold?</b>")

    # --- brain error is shown, UI recovers ---
    fake.push(RuntimeError("All brains failed — ollama: connection refused"))
    page.fill("#input", "are you there?")
    page.keyboard.press("Enter")
    expect(page.locator(".msg.assistant").last).to_contain_text("All brains failed", timeout=5000)
    check("error shown and not stuck busy", page.locator(".typing").count() == 0)
    page.screenshot(path=SHOTS / "3_chat.png")

    # --- header toggles ---
    page.locator("#tWatch").click()
    expect(sub).to_have_text("Not watching", timeout=3000)
    check("eye button pauses watching", friday.watcher.paused)
    page.locator("#tWatch").click()
    expect(page.locator("#tWatch")).to_have_class("icon on", timeout=3000)
    expect(sub).not_to_have_text("Not watching", timeout=3000)
    check("eye button resumes", not friday.watcher.paused)

    # --- memory tab ---
    page.get_by_role("button", name="Memory").click()
    expect(page.locator(".fact")).to_have_count(3, timeout=5000)
    check("memory: stats + facts + today", page.locator(".stat b").first.inner_text() == "3"
          and "CIFAR-10" in page.locator("#mem").inner_text())
    page.screenshot(path=SHOTS / "4_memory.png")
    page.locator(".fact", has_text="cricket").hover()
    page.locator(".fact", has_text="cricket").locator("button").click()
    expect(page.locator(".fact")).to_have_count(2, timeout=3000)
    check("forget a fact (×)", all("cricket" not in f["text"] for f in friday.memory.facts()))
    fake.push({"facts": [{"category": "projects", "text": "The user is building FRIDAY"}], "summary": "s"},
              "## Who they are\nA CS student building **FRIDAY**.")
    page.locator("#learnNow").click()
    expect(page.locator("#mem")).to_contain_text("A CS student building FRIDAY", timeout=8000)
    check("Learn now → new fact + profile", page.locator(".fact").count() == 3)

    # --- brains tab ---
    page.get_by_role("button", name="Brains").click()
    expect(page.locator(".prov")).to_have_count(5, timeout=5000)
    check("brains: 5 providers listed", True)
    ol = page.locator('.prov[data-name="ollama"]')
    ol.locator(".test").click()
    expect(ol).to_contain_text("✓ online", timeout=5000)
    check("Test button reaches a brain", True)
    page.screenshot(path=SHOTS / "5_brains.png")
    page.locator('input[data-key="GROQ_API_KEY"]').fill("gsk_ui_test")
    page.locator('.prov[data-name="groq"] input[data-f="model"]').fill("llama-ui-model")
    page.locator("#saveBrains").click()
    expect(page.locator('.prov[data-name="groq"] .dot')).to_have_class("dot ok", timeout=5000)
    check("Save stores key + model", "GROQ_API_KEY=gsk_ui_test" in config.ENV_FILE.read_text()
          and friday.cfg["providers"]["groq"]["model"] == "llama-ui-model")
    page.locator('.sw[data-k="speak"]').click()
    page.wait_for_timeout(400)
    check("speak switch persists", friday.cfg["agent"]["speak_replies"] is True)

    # --- orb mode ---
    page.get_by_role("button", name="Chat").click()
    page.locator("#bMin").click()
    check("minimise → orb", page.evaluate("document.body.classList.contains('mini')") and page.locator("#bigorb").is_visible())
    page.screenshot(path=SHOTS / "6_orb.png")
    page.locator("#bigorb").click(force=True)  # it pulses, so never "stable"
    check("click orb → panel", not page.evaluate("document.body.classList.contains('mini')"))

    # --- reload: history replayed ---
    page.reload()
    expect(page.locator(".msg")).not_to_have_count(0, timeout=5000)
    check("history restored after reload", page.locator(".msg.user").count() == 5)
    check("no JS errors", not errors, f"{errors[:3]}")
    browser.close()

server.should_exit = True
os.environ.pop("GROQ_API_KEY", None)
passed = sum(c[1] for c in checks)
print(f"\n{passed}/{len(checks)} UI checks passed; screenshots in {SHOTS}")
sys.exit(0 if passed == len(checks) else 1)
