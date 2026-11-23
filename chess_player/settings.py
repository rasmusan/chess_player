"""Settings and history files in the user data folder (JSON)."""

import json
import os
from typing import Optional
from .paths import APP_DIR, DATA_DIR


# ──────────────────────────────────────────────────────────────────────────────
# Engine history helpers
# ──────────────────────────────────────────────────────────────────────────────

_HISTORY_FILE = os.path.join(DATA_DIR, ".engine_history.json")

def load_engine_history() -> list:
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

def save_engine_history(path: str):
    path = os.path.abspath(path)
    history = [p for p in load_engine_history() if p != path]
    history.insert(0, path)
    try:
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[:30], f, indent=2)
    except OSError:
        pass


# Last engines used for White/Black, restored on the next start
_LAST_ENGINES_FILE = os.path.join(DATA_DIR, ".last_engines.json")

def load_last_engines() -> dict:
    try:
        with open(_LAST_ENGINES_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def save_last_engines(white, black):
    """white/black: UCIEngine or None."""
    d = {
        "white": os.path.abspath(white.path) if white else None,
        "black": os.path.abspath(black.path) if black else None,
    }
    try:
        with open(_LAST_ENGINES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


# Time control settings (per side), edited via the ⚙ settings dialog
_TIME_SETTINGS_FILE = os.path.join(DATA_DIR, ".time_settings.json")

def default_time_control(total: float = 60.0, inc: float = 0.0) -> dict:
    return {"separate": False,
            "white_time": total, "white_inc": inc,
            "black_time": total, "black_inc": inc}

def load_time_settings() -> Optional[dict]:
    try:
        with open(_TIME_SETTINGS_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
        tc = default_time_control()
        tc["separate"] = bool(d.get("separate", False))
        for k in ("white_time", "white_inc", "black_time", "black_inc"):
            v = float(d.get(k, tc[k]))
            if v < 0 or (k.endswith("_time") and v <= 0):
                return None
            tc[k] = v
        return tc
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None

def save_time_settings(tc: dict):
    try:
        with open(_TIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(tc, f, indent=2)
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# GUI / tournament settings
# ──────────────────────────────────────────────────────────────────────────────

_GUI_SETTINGS_FILE        = os.path.join(DATA_DIR, ".gui_settings.json")
_TOURNAMENT_SETTINGS_FILE = os.path.join(DATA_DIR, ".tournament_settings.json")

def _load_json_dict(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def _save_json_dict(path: str, d: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass

def load_gui_settings() -> dict:
    return _load_json_dict(_GUI_SETTINGS_FILE)

def save_gui_settings(update: dict):
    d = load_gui_settings()
    d.update(update)
    _save_json_dict(_GUI_SETTINGS_FILE, d)

def load_tournament_settings() -> dict:
    return _load_json_dict(_TOURNAMENT_SETTINGS_FILE)

def save_tournament_settings(d: dict):
    _save_json_dict(_TOURNAMENT_SETTINGS_FILE, d)


def write_json_atomic(path: str, d: dict):
    """Write JSON via a temp file so a crash never leaves a half-written file.
    Raises OSError."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)


def engine_candidates() -> list:
    """Engines to offer in pickers: [(display_name, abs_path, source)] with the
    engines/ folder first, then recently used engines from the history."""
    engines_dir = os.path.join(APP_DIR, "engines")
    items: list = []
    seen: set = set()
    if os.path.isdir(engines_dir):
        for fname in sorted(os.listdir(engines_dir)):
            fpath = os.path.join(engines_dir, fname)
            if os.path.isfile(fpath) and not fname.startswith("."):
                apath = os.path.abspath(fpath)
                items.append((os.path.splitext(fname)[0], apath, "default"))
                seen.add(apath)
    for hpath in load_engine_history():
        apath = os.path.abspath(hpath)
        if apath not in seen and os.path.isfile(apath):
            name = os.path.splitext(os.path.basename(apath))[0]
            items.append((f"{name}  ↩", apath, "recent"))
            seen.add(apath)
    return items
