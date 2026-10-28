"""The brain: a router over free/paid LLM providers with automatic fallback."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger("friday.llm")


class LLMError(Exception):
    pass


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def extract_json(text: str):
    """Pull the first JSON object out of a model response (handles fences and chatter)."""
    text = strip_think(text)
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


class Provider:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.kind = cfg.get("kind", "openai")
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg.get("model")
        self.vision_model = cfg.get("vision_model")
        self.key_env = cfg.get("key_env")
        self.cooldown_until = 0.0
        self.last_error: str | None = None
        self.last_used: float | None = None

    @property
    def api_key(self) -> str:
        return os.environ.get(self.key_env, "") if self.key_env else ""

    @property
    def configured(self) -> bool:
        return bool(self.model) and (not self.key_env or bool(self.api_key))

    def available(self, vision: bool) -> bool:
        if vision and not self.vision_model:
            return False
        return self.configured and time.time() >= self.cooldown_until

    async def chat(self, client, messages, images=None, json_mode=False, temperature=0.4) -> str:
        model = self.vision_model if images else self.model
        if self.kind == "ollama":
            return await self._ollama(client, model, messages, images, json_mode, temperature)
        return await self._openai(client, model, messages, images, temperature)

    async def _ollama(self, client, model, messages, images, json_mode, temperature):
        msgs = [dict(m) for m in messages]
        if images:
            msgs[-1]["images"] = [base64.b64encode(i).decode() for i in images]
        payload = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "think": False,
            # Keep the chat model warm, but free the vision model quickly: two resident models swap on 16 GB Macs.
            "keep_alive": "2m" if images else "30m",
            "options": {"temperature": temperature, "num_ctx": 8192},
        }
        if json_mode:
            payload["format"] = "json"
        r = await client.post(f"{self.base_url}/api/chat", json=payload, timeout=600)
        if r.status_code >= 400:
            raise LLMError(f"ollama {r.status_code}: {r.text[:200]}")
        return strip_think(r.json()["message"]["content"])

    async def _openai(self, client, model, messages, images, temperature):
        msgs = [dict(m) for m in messages]
        if images:
            parts = [{"type": "text", "text": msgs[-1]["content"]}]
            for img in images:
                b64 = base64.b64encode(img).decode()
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            msgs[-1]["content"] = parts
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.name == "openrouter":
            headers |= {"HTTP-Referer": "http://localhost", "X-Title": "FRIDAY"}
        payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": 4000}
        url = f"{self.base_url}/chat/completions"
        r = await client.post(url, json=payload, headers=headers, timeout=180)
        if r.status_code == 400 and ("temperature" in r.text or "max_tokens" in r.text):
            # Reasoning models (e.g. gpt-5 family) reject these sampling params.
            payload.pop("temperature", None)
            payload.pop("max_tokens", None)
            r = await client.post(url, json=payload, headers=headers, timeout=180)
        if r.status_code == 429:
            self.cooldown_until = time.time() + 60
            raise LLMError(f"{self.name} rate limited")
        if r.status_code >= 400:
            raise LLMError(f"{self.name} {r.status_code}: {r.text[:200]}")
        content = r.json()["choices"][0]["message"].get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return strip_think(content)


class Brain:
    def __init__(self, cfg: dict):
        self.client = httpx.AsyncClient()
        self.reload(cfg)

    def reload(self, cfg: dict) -> None:
        self.providers = {n: Provider(n, c) for n, c in cfg["providers"].items()}
        self.chains = cfg["brain"]

    def status(self) -> list[dict]:
        now = time.time()
        return [
            {
                "name": p.name,
                "model": p.model,
                "vision_model": p.vision_model,
                "key_env": p.key_env,
                "configured": p.configured,
                "cooling": p.cooldown_until > now,
                "last_error": p.last_error,
                "last_used": p.last_used,
            }
            for p in self.providers.values()
        ]

    async def complete(self, role: str, messages: list[dict], images=None, json_mode=False, temperature=0.4):
        """Return (text, provider_name). Tries every provider in the role's chain."""
        chain = self.chains.get(role) or self.chains["chat"]
        errors, cooling = [], []
        for name in chain:
            p = self.providers.get(name)
            if p and p.configured and p.cooldown_until > time.time() and (p.vision_model or not images):
                cooling.append(f"{name} ({p.last_error or 'cooling down'})")
            if not p or not p.available(vision=bool(images)):
                continue
            try:
                try:
                    text = await p.chat(self.client, messages, images, json_mode, temperature)
                except httpx.TransportError:
                    await asyncio.sleep(1.5)  # one retry for dropped connections / restarting servers
                    text = await p.chat(self.client, messages, images, json_mode, temperature)
                p.last_error, p.last_used = None, time.time()
                return text, name
            except Exception as e:
                msg = str(e) or type(e).__name__
                if p.kind == "ollama" and isinstance(e, httpx.ConnectError):
                    msg = "Ollama isn't running — open the Ollama app (or run `ollama serve`), or add a free Gemini key"
                p.last_error = msg[:300]
                if not p.cooldown_until > time.time():
                    p.cooldown_until = time.time() + 20
                errors.append(f"{name}: {msg[:120]}")
                log.warning("brain %s failed: %s", name, msg[:300])
        if not errors and cooling:
            raise LLMError("Brains are cooling down after errors — " + " | ".join(cooling))
        if not errors:
            raise LLMError(f"No brain configured for '{role}'. Add a key in the Brains tab or start Ollama.")
        raise LLMError("All brains failed — " + " | ".join(errors))
