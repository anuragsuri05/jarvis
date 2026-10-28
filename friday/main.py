"""Entry point: starts the backend and the always-on-top floating FRIDAY window."""
from __future__ import annotations

import argparse
import logging
import threading
import time

import uvicorn

from .config import DATA, load_config
from .core import Friday
from .server import create_app

ORB = 92
MARGIN = 24


class WindowApi:
    """Methods callable from the UI's JavaScript via window.pywebview.api."""

    def __init__(self, width: int, height: int):
        self._window = None
        self._size = (width, height)

    def _screen(self):
        import webview

        s = webview.screens[0]
        return s.width, s.height

    def set_mode(self, mode: str):
        sw, sh = self._screen()
        w, h = (ORB, ORB) if mode == "orb" else self._size
        self._window.resize(w, h)
        self._window.move(sw - w - MARGIN, sh - h - MARGIN - 40)

    def quit(self):
        self._window.destroy()


def main():
    ap = argparse.ArgumentParser(prog="friday")
    ap.add_argument("--headless", action="store_true", help="no floating window; open the UI in a browser instead")
    ap.add_argument("--no-watch", action="store_true", help="don't watch the screen this session")
    args = ap.parse_args()

    cfg = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(DATA / "friday.log"), logging.StreamHandler()],
    )
    for noisy in ("httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    friday = Friday(cfg)
    if args.no_watch:
        friday.watcher.paused = True
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    server = uvicorn.Server(uvicorn.Config(create_app(friday), host=host, port=port, log_level="warning"))
    url = f"http://{host}:{port}/"

    if args.headless:
        print(f"FRIDAY is online → {url}")
        server.run()
        return

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise SystemExit(f"Server failed to start (is port {port} in use?)")
        time.sleep(0.1)
    print(f"FRIDAY is online → {url}")

    import webview

    w, h = cfg["window"]["width"], cfg["window"]["height"]
    api = WindowApi(w, h)
    s = webview.screens[0]
    window = webview.create_window(
        cfg["name"], url, js_api=api, width=w, height=h,
        x=s.width - w - MARGIN, y=s.height - h - MARGIN - 40,
        frameless=True, on_top=True, transparent=True, easy_drag=False, resizable=True,
    )
    api._window = window
    webview.start()
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
