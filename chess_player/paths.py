"""Paths and output setup. Works both from source and as a PyInstaller-frozen app."""

import os
import sys


FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # Bundled read-only resources (pieces.png, icon) are unpacked here
    RESOURCE_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "assets")
    # Folder containing the installed exe (engines/ is installed next to it)
    APP_DIR = os.path.dirname(sys.executable)
    # Writable per-user data (settings, engine history, log, games)
    DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "chess_player")
else:
    _PKG_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = os.path.join(_PKG_DIR, "assets")
    # From source: engines/, settings and games live in the repository root
    APP_DIR = DATA_DIR = os.path.dirname(_PKG_DIR)

try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass

# A windowed (no-console) build has sys.stdout/stderr = None, which makes print()
# crash. Send output to a log file instead.
NO_CONSOLE = sys.stdout is None or sys.stderr is None
if NO_CONSOLE:
    try:
        _log = open(os.path.join(DATA_DIR, "chess_player.log"), "w", encoding="utf-8", buffering=1)
    except OSError:
        _log = open(os.devnull, "w")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log
