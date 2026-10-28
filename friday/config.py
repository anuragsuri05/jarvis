"""Configuration: defaults, config.yaml overrides, and .env secrets."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
# FRIDAY_HOME relocates all user state (memory, keys, config) — used for tests or multiple profiles.
HOME = Path(os.environ.get("FRIDAY_HOME", ROOT)).expanduser()
DATA = HOME / "data"
UI_DIR = Path(__file__).resolve().parent / "ui"
ENV_FILE = HOME / ".env"
CONFIG_FILE = HOME / "config.yaml"

DEFAULTS: dict = {
    "name": "FRIDAY",
    # Every provider speaks the OpenAI chat format except Ollama, which uses its native API.
    "providers": {
        "ollama": {
            "kind": "ollama",
            "base_url": "http://localhost:11434",
            "model": "qwen2.5:7b",
            "vision_model": "gemma4:e2b",
        },
        "gemini": {
            "kind": "openai",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "key_env": "GEMINI_API_KEY",
            "model": "gemini-flash-latest",
            "vision_model": "gemini-flash-latest",
        },
        "groq": {
            "kind": "openai",
            "base_url": "https://api.groq.com/openai/v1",
            "key_env": "GROQ_API_KEY",
            "model": "llama-3.3-70b-versatile",
            "vision_model": "meta-llama/llama-4-scout-17b-16e-instruct",
        },
        "openrouter": {
            "kind": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_API_KEY",
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "vision_model": None,
        },
        "openai": {
            "kind": "openai",
            "base_url": "https://api.openai.com/v1",
            "key_env": "OPENAI_API_KEY",
            "model": "gpt-4.1-mini",
            "vision_model": "gpt-4.1-mini",
        },
    },
    # Each role tries providers in order and falls through on errors / rate limits.
    "brain": {
        "chat": ["gemini", "openai", "groq", "openrouter", "ollama"],
        "fast": ["groq", "gemini", "openrouter", "openai", "ollama"],
        "vision": ["gemini", "openai", "groq", "ollama"],
        # What looks at your screen continuously. Local-only by default for privacy.
        "screen_vision": ["ollama"],
    },
    "watcher": {
        "enabled": True,
        "interval_seconds": 20,
        "describe_min_seconds": 90,
        "idle_pause_seconds": 300,
        "exclude_apps": ["1Password", "1Password 7", "Keychain Access", "Bitwarden", "LastPass", "Passwords"],
        "exclude_title_keywords": ["password", "private browsing", "incognito", "bank"],
    },
    "learner": {"every_minutes": 60},
    "agent": {
        "max_steps": 8,
        "confirm_actions": True,
        "speak_replies": False,
        "voice": "Samantha",
    },
    "server": {"host": "127.0.0.1", "port": 8765},
    "window": {"width": 400, "height": 640},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def save_env(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def _user_config() -> dict:
    if CONFIG_FILE.exists():
        return yaml.safe_load(CONFIG_FILE.read_text()) or {}
    return {}


def save_user_config(patch: dict) -> dict:
    """Merge `patch` into config.yaml and return the full effective config."""
    merged = _merge(_user_config(), patch)
    CONFIG_FILE.write_text(yaml.safe_dump(merged, sort_keys=False))
    return load_config()


def load_config() -> dict:
    load_env()
    DATA.mkdir(parents=True, exist_ok=True)
    return _merge(DEFAULTS, _user_config())
