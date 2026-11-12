"""Browser interface for dqp (optional; needs the [gui] extra: pip install 'dqp[gui]')."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP = Path(__file__).with_name("app.py")
THEME = {"theme.base": "light", "theme.primaryColor": "#E30613", "theme.backgroundColor": "#FFFFFF",
         "theme.secondaryBackgroundColor": "#F4F4F4", "theme.textColor": "#111111"}


def launch(port: int | None = None, open_browser: bool = True) -> int:
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("The GUI needs Streamlit:  pip install 'dqp[gui]'   (or: pip install streamlit)", file=sys.stderr)
        return 1
    cmd = [sys.executable, "-m", "streamlit", "run", str(APP), "--browser.gatherUsageStats", "false",
           "--server.headless", "false" if open_browser else "true", "--server.maxUploadSize", "500", "--client.toolbarMode", "viewer"]
    if port:
        cmd += ["--server.port", str(port)]
    for k, v in THEME.items():
        cmd += [f"--{k}", v]
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0
