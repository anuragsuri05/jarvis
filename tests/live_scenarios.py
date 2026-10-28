"""Live end-to-end scenarios against the REAL brain (local Ollama by default). Not part of pytest — slow.

    FRIDAY_HOME=$(mktemp -d) .venv/bin/python -m tests.live_scenarios
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

assert os.environ.get("FRIDAY_HOME"), "set FRIDAY_HOME to a temp dir so real memory is untouched"

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from friday import macos  # noqa: E402
from friday.config import load_config  # noqa: E402
from friday.core import Friday  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="friday_live_"))
results = []


class Recorder:
    def __init__(self, f):
        self.f, self.events, self.answer = f, [], True

    async def send_text(self, data):
        m = json.loads(data)
        self.events.append(m)
        if m["type"] == "confirm":
            print(f"      ↳ approval asked for {m['tool']} → {'ALLOW' if self.answer else 'DENY'}", flush=True)
            asyncio.get_running_loop().call_later(0.05, self.f.resolve_confirm, m["id"], self.answer)
        elif m["type"] == "step":
            print(f"      ↳ step: {m['tool']} {json.dumps(m['args'])[:120]}", flush=True)


async def scenario(f, rec, name, prompt, check, answer=True):
    rec.events.clear()
    rec.answer = answer
    print(f"\n▶ {name}\n   you: {prompt}", flush=True)
    t = time.time()
    await f.chat(prompt)
    took = time.time() - t
    reply = next((e["text"] for e in reversed(rec.events) if e["type"] == "reply"), "")
    tools = [e["tool"] for e in rec.events if e["type"] == "step"]
    try:
        ok, why = check(reply, tools)
    except Exception as e:
        ok, why = False, f"check crashed: {e}"
    print(f"   friday ({took:.0f}s): {reply[:300]}\n   tools={tools}  → {'PASS' if ok else 'FAIL'} {why}", flush=True)
    results.append((name, ok, took, why))


def screen(lines, title):
    img = Image.new("RGB", (1280, 800), "#1e1e1e")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 28)
    d.rectangle([0, 0, 1280, 50], fill="#333")
    d.text((20, 10), title, fill="white", font=font)
    d.multiline_text((40, 90), "\n".join(lines), fill="#d4d4d4", font=font, spacing=12)
    b = io.BytesIO()
    img.save(b, "JPEG")
    return b.getvalue()


async def main():
    async def noop(*a, **k):
        return None
    macos.notify = noop  # don't spam real notifications

    f = Friday(load_config())
    rec = Recorder(f)
    f.hub.clients.add(rec)
    print("brains:", [b["name"] for b in f.brain.status() if b["configured"]], "| work dir:", WORK)

    lower = str.lower
    await scenario(f, rec, "1. small talk (no tools needed)", "Hey Friday, how are you today?",
                   lambda r, t: (bool(r) and "⚠️" not in r and not t, "answered without tools" if not t else f"used {t}"))
    await scenario(f, rec, "2. learns a preference", "Remember this: my favourite programming language is Rust.",
                   lambda r, t: ("remember" in t and any("rust" in lower(x["text"]) for x in f.memory.facts()),
                                 "fact saved" if "remember" in t else "did not call remember"))
    await scenario(f, rec, "3. recalls it later", "What's my favourite programming language?",
                   lambda r, t: ("rust" in lower(r), "mentions Rust"))
    todo = WORK / "todo.txt"
    await scenario(f, rec, "4. multi-step file task (approved)",
                   f"Create a file at {todo} containing exactly the text 'buy milk', then read it back to confirm.",
                   lambda r, t: (todo.exists() and "buy milk" in todo.read_text() and "write_file" in t,
                                 f"file={'ok' if todo.exists() else 'missing'}, read_back={'read_file' in t}"))
    junk = WORK / "junk"
    junk.mkdir()
    await scenario(f, rec, "5. risky action DENIED", f"Use the shell to delete the folder {junk}",
                   lambda r, t: (junk.exists() and bool(t), "folder survived; FRIDAY respected the denial"),
                   answer=False)
    await scenario(f, rec, "6. system question", "How much battery do I have left?",
                   lambda r, t: ("%" in r and bool(t), f"tools {t}"))
    await scenario(f, rec, "7. web research", "Search the web: what is the latest stable version of Python?",
                   lambda r, t: ("web_search" in t and "3." in r, "searched + gave a version"))
    await scenario(f, rec, "8. reminder", "Remind me to call mom in 2 minutes.",
                   lambda r, t: (any("mom" in lower(x["text"]) for x in f.memory.pending_reminders()), "reminder stored"))

    # --- watcher: real vision model on a synthetic screen ---
    print("\n▶ 9. watcher sees & describes the screen (real vision model)", flush=True)
    shot = screen(["def train(model, data):", "    for epoch in range(20):", "        loss = model.fit(data)",
                   "", "# TODO: add early stopping"], "Visual Studio Code — train_cifar.py — ml-internship-prep")

    async def fake_shot(**k):
        return shot

    async def fake_front():
        return "Code", "train_cifar.py — ml-internship-prep"

    async def zero():
        return 0
    macos.screenshot, macos.frontmost, macos.idle_seconds = fake_shot, fake_front, zero
    t = time.time()
    await f.watcher.tick()
    desc = f.memory.recent_activity(1)[-1]["description"]
    ok = any(k in lower(desc) for k in ("train", "cifar", "model", "epoch"))
    print(f"   description ({time.time()-t:.0f}s): {desc}\n   → {'PASS' if ok else 'FAIL'}", flush=True)
    results.append(("9. watcher vision", ok, time.time() - t, desc[:80]))

    # --- learner: turns activity into grounded facts + profile ---
    print("\n▶ 10. learner extracts facts and writes a grounded profile", flush=True)
    for app, title, d in [
        ("Code", "train_cifar.py", "Training a CNN on CIFAR-10 with PyTorch for an ML internship prep project"),
        ("Chrome", "LeetCode — Two Sum", "Solving LeetCode array problems in Python"),
        ("Chrome", "Internshala — ML internships", "Browsing machine learning internship listings"),
        ("Notion", "Study plan", "Writing a weekly study plan: DSA mornings, ML evenings"),
    ]:
        f.memory.add_activity(app, title, d)
    t = time.time()
    added = await f.learner.learn()
    facts = [x["text"] for x in f.memory.facts()]
    profile = f.learner.profile()
    print(f"   +{added} facts ({time.time()-t:.0f}s):", *[f"\n     - {x}" for x in facts])
    print("   profile:\n" + "\n".join("     " + ln for ln in profile.splitlines()), flush=True)
    invented = [w for w in ("jupyter", "tensorflow", "kaggle", "java ", "r programming", "weekend") if w in lower(profile)]
    ok = added > 0 and bool(profile) and not invented
    results.append(("10. learner + profile", ok, time.time() - t, f"invented={invented}" if invented else "grounded"))

    print("\n================ SUMMARY ================")
    for name, ok, took, why in results:
        print(f"{'PASS' if ok else 'FAIL'}  {took:5.0f}s  {name}  — {why}")
    print(f"{sum(r[1] for r in results)}/{len(results)} passed")
    await f.stop()
    return all(r[1] for r in results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
