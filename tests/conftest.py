"""Test setup: isolate all FRIDAY state in a temp home, and provide a scripted fake brain."""
import asyncio
import json
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="friday_test_")
os.environ["FRIDAY_HOME"] = _HOME  # must happen before importing friday

import httpx  # noqa: E402
import pytest  # noqa: E402

from friday import config  # noqa: E402
from friday.config import load_config  # noqa: E402
from friday.core import Friday  # noqa: E402


class FakeBrain:
    """Replays scripted responses; records every call. Items can be str, dict (→JSON) or Exception."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []
        self.client = httpx.AsyncClient()
        self.providers = {}
        self.chains = {}

    def push(self, *items):
        self.script.extend(items)

    async def complete(self, role, messages, images=None, json_mode=False, temperature=0.4):
        self.calls.append({"role": role, "messages": list(messages), "images": images, "json_mode": json_mode})
        item = self.script.pop(0) if self.script else {"reply": "(fake default)"}
        if isinstance(item, Exception):
            raise item
        return (json.dumps(item) if isinstance(item, dict) else item), "fake"

    def status(self):
        return []

    def reload(self, cfg):
        pass


@pytest.fixture
def home():
    """Fresh, empty FRIDAY home per test."""
    for name in os.listdir(_HOME):
        p = os.path.join(_HOME, name)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    yield config.HOME


@pytest.fixture
def friday(home):
    cfg = load_config()
    cfg["agent"]["confirm_actions"] = True
    f = Friday(cfg)
    f.brain = FakeBrain()
    yield f
    f.memory.db.close()


def run(coro):
    return asyncio.run(coro)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)
