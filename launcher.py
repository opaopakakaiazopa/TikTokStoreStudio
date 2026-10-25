"""Start one local server and open the studio in the default browser."""
from pathlib import Path
import json
import os
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn

URL = "http://127.0.0.1:8765"


def is_studio():
    try:
        with urllib.request.urlopen(URL + "/api/state", timeout=2) as response:
            return "settings" in json.load(response)
    except Exception:
        return False


def open_when_ready():
    for _ in range(60):
        if is_studio():
            webbrowser.open(URL)
            return
        time.sleep(.5)


if __name__ == "__main__":
    if is_studio():
        webbrowser.open(URL)
    else:
        threading.Thread(target=open_when_ready, daemon=True).start()
        print("TikTok Store Studio: " + URL)
        print("Keep this window open. Press Ctrl+C to stop.")
        host = os.environ.get("STORE_STUDIO_HOST", "127.0.0.1")
        uvicorn.run("app:app", host=host, port=8765, log_level="info", timeout_graceful_shutdown=5, app_dir=str(Path(__file__).resolve().parent))
