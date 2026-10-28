# FRIDAY — your personal, screen-aware AI assistant (macOS)

FRIDAY floats on your screen (always on top), watches what you do, learns who you are day by day,
and does work on your Mac when you ask — running on **free** LLMs.

```
┌───────────── floating window / orb (always on top) ─────────────┐
│  Chat  ·  Memory (what it learned)  ·  Brains (models & keys)   │
└───────────────┬──────────────────────────────────────────────────┘
                │ WebSocket (127.0.0.1 only)
┌───────────────▼──────────────── backend (Python) ───────────────┐
│ Watcher  → screenshot every 20s → vision model → 1-line summary │
│ Memory   → SQLite: activity, facts, chats, journal, reminders   │
│ Learner  → hourly: extract facts · daily: journal + profile.md  │
│ Agent    → plans with tools: shell, AppleScript, apps, web,     │
│            files, typing/keys, clipboard, reminders, see_screen │
│ Brain    → Gemini → OpenAI → Groq → OpenRouter → Ollama (auto   │
│            fallback; Ollama = local, free, private)             │
└─────────────────────────────────────────────────────────────────┘
```

## Start

```bash
cd "~/Downloads/100 projects/jarvis"
./run.sh              # floating window
./run.sh --headless   # no window; open http://127.0.0.1:8765 in a browser
./run.sh --no-watch   # don't watch the screen this session
```

First run creates `.venv` and installs everything. To start FRIDAY automatically at login:
`./scripts/install_autostart.sh`

## One-time macOS permissions (required for "seeing" and "doing")

System Settings → Privacy & Security, then enable the app you launch FRIDAY from
(**Terminal**, **iTerm**, or **Visual Studio Code**; with autostart, **Python**):

| Permission        | Why                                                  |
|-------------------|------------------------------------------------------|
| Screen Recording  | to see your screen (status says *Needs Screen Recording permission* until granted) |
| Accessibility     | window titles, typing, key presses, clicks          |
| Automation        | macOS will ask the first time FRIDAY controls an app |

Restart FRIDAY after granting.

## Brains (all free options first)

| Brain       | Cost | How to "log in"                                                    |
|-------------|------|--------------------------------------------------------------------|
| Ollama      | free, local, private | already installed — uses `qwen2.5:7b` (chat) + `gemma4:e2b` (vision) |
| **Gemini**  | free tier | sign in with Google at https://aistudio.google.com/apikey → copy key |
| Groq        | free tier | https://console.groq.com/keys                              |
| OpenRouter  | free models | https://openrouter.ai/keys                               |
| OpenAI/GPT  | paid (API) | https://platform.openai.com/api-keys                      |

Paste keys in the **Brains** tab (or in `.env`). FRIDAY tries them in order and falls back automatically
when one is rate-limited or offline.

> Why API keys and not your ChatGPT/Gemini website logins? Automating the consumer chat websites breaks
> their terms of service, can get your account banned, and breaks whenever the site changes. The Gemini API key
> *is* your Google login — just the official, stable door — and its free tier is generous.

**Speed tip:** on this Intel Mac the local models take ~30–60s per answer. Adding a free Gemini or Groq key
makes chat near-instant, while screen watching stays local.

## How it gets smarter

1. **Every ~20s**: if you switched apps or the screen changed, the local vision model writes one sentence about
   what you're doing. Only that text is stored; screenshots are deleted immediately.
2. **Every hour**: the Learner turns activity + chats into durable facts (projects, skills, habits, goals,
   preferences, people, tools) and rebuilds `data/profile.md`.
3. **Every day**: a journal entry for yesterday.
4. **Every chat**: FRIDAY reads your profile, the relevant facts, and what you're doing right now.

Review or delete anything it learned in the **Memory** tab. Press **Learn now** to trigger learning immediately.

## Privacy & safety

- Everything stays in `data/` on your Mac. The server listens on `127.0.0.1` only.
- Screen images go **only to Ollama (local)** by default (`brain.screen_vision` in config).
  The `see_screen` tool, which you trigger by asking, uses the `vision` chain, which may be a cloud model.
- Password managers, private/incognito windows, and banking titles are never observed (`watcher.exclude_*`).
- The eye button pauses watching; FRIDAY also pauses when you're idle for 5 min.
- Shell commands, AppleScript, file writes, typing, keys, and clicks show an **Allow / Deny** card first
  (turn it off in Brains → Behaviour at your own risk).

## Things to try

- "What am I working on?" · "Summarize my day" · "What do you know about me?"
- "Look at my screen and explain this error"
- "Open YouTube and search lofi beats" · "Set volume to 30"
- "Find the 5 biggest files in Downloads" · "Create a notes.md on my Desktop with today's plan"
- "Remind me to drink water in 45 minutes"
- "Research the best free vector databases and summarize"

## Customize

Create `config.yaml` (overrides the defaults in `friday/config.py`), e.g.:

```yaml
brain:
  chat: [gemini, groq, ollama]
  screen_vision: [ollama]        # add gemini here for better screen understanding (sends screenshots to Google)
watcher:
  interval_seconds: 30
  exclude_apps: [1Password, WhatsApp]
agent:
  speak_replies: true
  voice: Samantha                # `say -v ?` lists voices
```

## Tests

```bash
.venv/bin/python -m pytest tests -q                                  # 37 fast tests: memory, brain routing, agent, tools, watcher, learner, API, security
FRIDAY_HOME=$(mktemp -d) .venv/bin/python -m tests.ui_e2e /tmp/shots # drives the real UI in Chrome (29 checks + screenshots)
FRIDAY_HOME=$(mktemp -d) .venv/bin/python -m tests.live_scenarios    # real tasks against your real brain (slow on CPU)
```

`FRIDAY_HOME` points FRIDAY at a throwaway folder, so tests never touch your real memory or keys.

## Files

| Path | What |
|---|---|
| `friday/watcher.py` | screen observation |
| `friday/learner.py` | facts, journal, profile |
| `friday/agent.py` | reasoning loop + system prompt |
| `friday/tools.py` | everything it can do (add your own here) |
| `friday/llm.py` | multi-provider brain with fallback |
| `friday/memory.py` | SQLite memory |
| `friday/ui/index.html` | floating HUD |
| `data/` | your memory DB, profile, logs |
