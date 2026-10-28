"""The thinking loop: builds context from memory, lets the brain pick tools, and replies."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from .llm import extract_json

log = logging.getLogger("friday.agent")

# Phrases that claim an action happened. If one appears while no tool ran this turn, the brain is bluffing.
CLAIMS = re.compile(
    r"TOOL RESULT|reminder (?:is |has been )?set|i(?:'ve| have)? (?:set|searched|looked up|opened|created|saved|"
    r"deleted|written|wrote|sent|launched|scheduled|added|copied|run|ran|checked)\b|according to (?:my|the) "
    r"(?:web )?search|search results? (?:show|say)",
    re.I,
)
NUDGE = ("SYSTEM CHECK: your reply claims something was done or looked up, but you called NO tool this turn, so "
         "nothing actually happened. If the request needs an action or live information, respond now with a JSON "
         "tool call. If no tool is needed, reply honestly without claiming actions.")

SYSTEM = """You are {name} — a personal AI assistant in the spirit of FRIDAY/JARVIS from Iron Man — living on the
user's Mac. You are loyal, sharp, proactive, concise and a little witty. You watch the user's screen activity
(with their consent), learn who they are day by day, and act on the computer with tools to get work done.

Current time: {now}

## Who the user is (your learned profile)
{profile}

## Relevant memories
{facts}

## What the user is doing now / recently
{activity}

## Tools
{tools}

## How to respond
Reply with exactly ONE JSON object and nothing else.
Use a tool:   {{"thought": "brief reasoning", "tool": "<name>", "args": {{...}}}}
Answer user:  {{"thought": "brief reasoning", "reply": "<message to the user, markdown allowed>"}}

Rules:
- Use tools for real data and real actions; never pretend you did something.
- After a TOOL RESULT, keep going until the task is done, then reply with a short summary.
- If the user shares something about themselves or a preference, call `remember`.
- Use your knowledge of the user and their current screen to give personal, specific help.
- Prefer open_app / open_url / applescript over pixel clicking. On macOS use zsh commands.
- If a tool is denied or fails twice, stop and explain.
- You cannot set reminders, search, open, create, or check ANYTHING without calling the tool. Claiming you did
  without a tool call is lying. Only the system writes "TOOL RESULT" messages — never write one yourself.
- Keep replies brief unless asked for depth. Address the user naturally.

Examples:
User: remind me to drink water in 20 minutes
You: {{"thought": "needs a real reminder", "tool": "set_reminder", "args": {{"text": "drink water", "minutes": 20}}}}
User: what's the latest iPhone?
You: {{"thought": "live info, must search", "tool": "web_search", "args": {{"query": "latest iPhone model"}}}}
User: thanks!
You: {{"thought": "no action needed", "reply": "Anytime!"}}"""


class Agent:
    def __init__(self, friday):
        self.f = friday

    def _context(self, text: str) -> str:
        mem = self.f.memory
        profile = self.f.learner.profile().strip()[:3000] or "(No profile yet — you are still learning about the user.)"
        seen, facts = set(), []
        for fct in mem.search_facts(text, 8) + mem.facts(12):
            if fct["id"] not in seen:
                seen.add(fct["id"])
                facts.append(f"- ({fct['category']}) {fct['text']}")
        acts = mem.recent_activity(8)
        activity = "\n".join(
            f"[{dt.datetime.fromtimestamp(a['ts']):%H:%M}] {a['app']} — {a['title'][:100]}: {a['description'][:250]}"
            for a in acts) or "(nothing observed yet)"
        w = self.f.watcher.snapshot()
        if w["app"]:
            activity += f"\nRIGHT NOW: {w['app']} — {w['title'][:150]}"
        return SYSTEM.format(
            name=self.f.cfg["name"],
            now=dt.datetime.now().strftime("%A %d %B %Y, %H:%M"),
            profile=profile,
            facts="\n".join(facts) or "(none yet)",
            activity=activity,
            tools=self.f.tools.describe(),
        )

    async def handle(self, text: str) -> str:
        mem = self.f.memory
        history = mem.recent_messages(12)
        mem.add_message("user", text)
        msgs = [{"role": "system", "content": self._context(text)}]
        msgs += [{"role": m["role"], "content": m["content"]} for m in history]
        msgs.append({"role": "user", "content": text})

        reply, used_tool, nudged = None, False, False
        for _ in range(self.f.cfg["agent"]["max_steps"]):
            await self.f.set_busy(True, "thinking")
            raw, brain = await self.f.brain.complete("chat", msgs, json_mode=True, temperature=0.3)
            action = extract_json(raw)
            tool = action.get("tool") if isinstance(action, dict) else None
            if not tool:
                answer = str(action["reply"]) if isinstance(action, dict) and action.get("reply") is not None else raw.strip()
                if not used_tool and not nudged and CLAIMS.search(answer):
                    log.info("brain claimed an action without a tool; nudging: %s", answer[:120])
                    nudged = True
                    msgs += [{"role": "assistant", "content": raw}, {"role": "user", "content": NUDGE}]
                    continue
                reply = answer
                break
            args = action.get("args") or {}
            used_tool = True
            await self.f.hub.emit({"type": "step", "tool": tool, "args": args,
                                   "thought": action.get("thought", ""), "brain": brain})
            await self.f.set_busy(True, f"using {tool}")
            result = await self.f.tools.call(tool, args)
            await self.f.hub.emit({"type": "step_result", "tool": tool, "result": result[:600]})
            msgs.append({"role": "assistant", "content": json.dumps(action)})
            msgs.append({"role": "user", "content": f"TOOL RESULT ({tool}):\n{result}\n\nContinue. One JSON object."})
        if reply is None:
            reply = "I ran out of steps on that one. Here's where I got to — tell me how to continue."
        mem.add_message("assistant", reply)
        return reply
