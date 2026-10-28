"""Turns raw activity + conversations into durable knowledge about the user, every hour and every day."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from .config import DATA
from .llm import extract_json

log = logging.getLogger("friday.learner")

CATEGORIES = "identity, work, projects, skills, learning, interests, habits, schedule, preferences, people, goals, tools"

EXTRACT_PROMPT = """You are the learning module of FRIDAY, a personal AI assistant. Study the user's recent computer
activity and conversation, and extract DURABLE facts about the user: who they are, their work and projects, skills
they use or are learning, interests, daily habits and schedule, preferences, goals, people, and tools they rely on.

Rules:
- Only facts that will likely still be true next week. Skip one-off trivia.
- Each fact is one short, specific sentence about the user ("The user is building a Jarvis-style assistant in Python").
- Do NOT repeat anything in KNOWN FACTS.
- Never record passwords, keys, financial numbers, or private message contents.
- Categories: {categories}

KNOWN FACTS:
{known}

RECENT ACTIVITY:
{activity}

RECENT CONVERSATION:
{messages}

Respond with JSON only:
{{"facts": [{{"category": "...", "text": "..."}}], "summary": "2-3 sentence summary of what the user did in this period"}}"""

PROFILE_PROMPT = """Write FRIDAY's internal profile of its user from the facts and recent journal below.
Use markdown with short sections chosen from: Who they are, Work & projects, Skills & learning, Habits & schedule,
Preferences, Goals, How FRIDAY can help most. Be concise (max ~300 words) and specific.

STRICT RULE: every statement must come directly from the facts or journal. Do NOT guess or add plausible
details (tools, languages, schedules, skill levels) that are not written below. Omit any section you have no
facts for. A short, accurate profile is far better than a long, invented one.

FACTS (strongest first):
{facts}

RECENT JOURNAL:
{journal}"""

JOURNAL_PROMPT = """Write a short journal entry (max 150 words) summarizing the user's day on {day} from these hourly
notes and activity samples. Mention main projects, time sinks, and anything worth following up tomorrow.

NOTES:
{notes}

ACTIVITY SAMPLES:
{activity}"""


def fmt_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def day_bounds(day: dt.date) -> tuple[float, float]:
    start = dt.datetime.combine(day, dt.time())
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


class Learner:
    def __init__(self, friday):
        self.f = friday
        self.profile_path = DATA / "profile.md"
        self._lock = asyncio.Lock()

    def profile(self) -> str:
        return self.profile_path.read_text() if self.profile_path.exists() else ""

    async def run(self):
        await asyncio.sleep(30)
        while True:
            try:
                mem = self.f.memory
                last = float(mem.get("last_learn_ts", 0))
                if time.time() - last >= self.f.cfg["learner"]["every_minutes"] * 60:
                    await self.learn()
                today = dt.date.today().isoformat()
                if mem.get("last_journal_day") != today:
                    await self.daily(dt.date.today() - dt.timedelta(days=1))
                    mem.set("last_journal_day", today)
            except Exception:
                log.exception("learner loop failed")
            await asyncio.sleep(120)

    async def learn(self) -> int:
        async with self._lock:
            mem = self.f.memory
            since = float(mem.get("last_learn_ts", 0))
            acts = [a for a in mem.activity_since(since, 400) if a["description"] or a["title"]]
            msgs = mem.unlearned_messages(80)
            if not acts and not msgs:
                mem.set("last_learn_ts", time.time())
                await self._refresh_profile(False)
                return 0
            step = max(1, len(acts) // 120)
            activity = "\n".join(
                f"[{fmt_time(a['ts'])}] {a['app']} — {a['title'][:120]}: {a['description'][:300]}" for a in acts[::step]
            ) or "(none)"
            messages = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in msgs) or "(none)"
            known = "\n".join(f"- {f['text']}" for f in mem.facts(120)) or "(none yet)"
            text, brain = await self.f.brain.complete(
                "fast",
                [{"role": "user", "content": EXTRACT_PROMPT.format(
                    categories=CATEGORIES, known=known, activity=activity[:14000], messages=messages[:6000])}],
                json_mode=True,
                temperature=0.2,
            )
            data = extract_json(text) or {}
            added = 0
            for item in data.get("facts", [])[:25]:
                if isinstance(item, dict) and item.get("text"):
                    added += mem.add_fact(item["text"], item.get("category", "general"), "learned")
            if data.get("summary"):
                mem.add_note("hourly", data["summary"])
            mem.mark_learned([m["id"] for m in msgs])
            mem.set("last_learn_ts", time.time())
            log.info("learned %d new facts via %s", added, brain)
            if added:
                await self.f.hub.emit({"type": "notice", "text": f"🧠 Learned {added} new thing(s) about you."})
            await self._refresh_profile(added > 0)
            return added

    async def _refresh_profile(self, changed: bool) -> None:
        """Rebuild the profile when facts changed or a previous rebuild failed; never fail the caller."""
        mem = self.f.memory
        if not (changed or mem.get("profile_stale") == "1" or not self.profile_path.exists()):
            return
        try:
            await self.rebuild_profile()
            mem.set("profile_stale", 0)
        except Exception as e:
            mem.set("profile_stale", 1)
            log.warning("profile rebuild failed, will retry next cycle: %s", e)

    async def rebuild_profile(self) -> str:
        mem = self.f.memory
        facts = mem.facts(150)
        if not facts:
            return ""
        week_ago = time.time() - 7 * 86400
        journal = mem.notes_between(week_ago, time.time() + 1, "daily")[-5:]
        text, _ = await self.f.brain.complete(
            "fast",
            [{"role": "user", "content": PROFILE_PROMPT.format(
                facts="\n".join(f"- ({f['category']}) {f['text']}" for f in facts),
                journal="\n".join(n["text"] for n in journal) or "(none yet)")}],
            temperature=0.3,
        )
        self.profile_path.write_text(text.strip() + "\n")
        await self.f.hub.emit({"type": "profile"})
        return text

    async def daily(self, day: dt.date) -> str | None:
        start, end = day_bounds(day)
        mem = self.f.memory
        if mem.notes_between(start, end + 6 * 3600, "daily"):
            return None
        acts = mem.activity_between(start, end)
        if not acts:
            return None
        await self.learn()
        notes = mem.notes_between(start, end + 3600, "hourly")
        step = max(1, len(acts) // 60)
        text, _ = await self.f.brain.complete(
            "fast",
            [{"role": "user", "content": JOURNAL_PROMPT.format(
                day=day.isoformat(),
                notes="\n".join(n["text"] for n in notes) or "(none)",
                activity="\n".join(f"[{fmt_time(a['ts'])}] {a['app']}: {a['description'][:200]}" for a in acts[::step]))}],
            temperature=0.3,
        )
        mem.add_note("daily", f"{day.isoformat()}: {text.strip()}")
        await self.rebuild_profile()
        return text
