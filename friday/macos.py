"""Thin async wrappers around macOS command-line facilities."""
from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile

from PIL import Image

BROWSERS = {
    "Safari": 'tell application "Safari" to get URL of front document',
    "Google Chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to get URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to get URL of active tab of front window',
    "Arc": 'tell application "Arc" to get URL of active tab of front window',
}


async def run(*cmd: str, timeout: float = 30, input: bytes | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def osascript(script: str, timeout: float = 20) -> str:
    code, out, err = await run("osascript", "-e", script, timeout=timeout)
    if code != 0:
        raise RuntimeError(err.strip() or f"osascript exited {code}")
    return out.strip()


FRONT_SCRIPT = """
tell application "System Events"
  set fp to first application process whose frontmost is true
  set appName to name of fp
  set winTitle to ""
  try
    set winTitle to name of front window of fp
  end try
end tell
return appName & "|||" & winTitle
"""


async def frontmost() -> tuple[str, str]:
    """(app name, window title). Window title needs Accessibility permission."""
    try:
        out = await osascript(FRONT_SCRIPT, timeout=8)
        app, _, title = out.partition("|||")
    except Exception:
        try:
            from AppKit import NSWorkspace

            app = str(NSWorkspace.sharedWorkspace().frontmostApplication().localizedName())
        except Exception:
            app = "unknown"
        title = ""
    if app in BROWSERS:
        try:
            url = await osascript(BROWSERS[app], timeout=4)
            if url and url not in title:
                title = f"{title} — {url}" if title else url
        except Exception:
            pass
    return app, title


async def idle_seconds() -> float:
    _, out, _ = await run("ioreg", "-c", "IOHIDSystem", timeout=5)
    m = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(m.group(1)) / 1e9 if m else 0.0


async def screenshot(max_width: int = 1280) -> bytes | None:
    """JPEG bytes of the main display, or None (usually: Screen Recording permission missing)."""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="friday_")
    os.close(fd)
    try:
        code, _, _ = await run("screencapture", "-x", "-C", "-t", "png", path, timeout=15)
        if code != 0 or os.path.getsize(path) == 0:
            return None
        img = Image.open(path).convert("RGB")
        if img.width > max_width:
            img = img.resize((max_width, int(img.height * max_width / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def dhash(jpeg: bytes) -> int:
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize((17, 16))
    px = img.tobytes()
    bits = 0
    for row in range(16):
        for col in range(16):
            bits = (bits << 1) | (px[row * 17 + col] > px[row * 17 + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _as_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def notify(title: str, text: str) -> None:
    try:
        await osascript(f"display notification {_as_str(text[:200])} with title {_as_str(title)} sound name \"Glass\"")
    except Exception:
        pass


async def clipboard_get() -> str:
    return (await run("pbpaste"))[1]


async def clipboard_set(text: str) -> None:
    await run("pbcopy", input=text.encode())


class Speaker:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None

    async def say(self, text: str, voice: str = "Samantha") -> None:
        self.stop()
        clean = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
        clean = re.sub(r"[*_`#>\[\]]|\(http[^)]*\)", "", clean)[:1500]
        self.proc = await asyncio.create_subprocess_exec("say", "-v", voice, clean)

    def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
