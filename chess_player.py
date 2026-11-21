#!/usr/bin/env python3
"""
chess_player.py — Run a game between two UCI chess engines.

Usage:
    python chess_player.py [--w ENGINE] [--b ENGINE] [options]

Options:
    --w <path>           Path to engine playing White
    --b <path>           Path to engine playing Black
    --time <sec>         Total time per player in seconds (default: 60, or the
                         last setting from the GUI's ⚙ settings dialog)
    --increment <sec>    Time increment per move in seconds (default: 0, or the
                         last GUI setting)
    --nogui              Run without GUI (headless); --w and --b required
    --pgn <file>         Save game to PGN file
"""

import argparse
import chess
import chess.pgn
import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

# Single source of truth for the version. build.ps1 reads (and bumps) this line.
__version__ = "1.2.1"


# ──────────────────────────────────────────────────────────────────────────────
# Paths — work both from source and as a PyInstaller-frozen app
# ──────────────────────────────────────────────────────────────────────────────

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # Bundled read-only resources (pieces.png, icon) are unpacked here
    RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    # Folder containing the installed exe (engines/ is installed next to it)
    APP_DIR = os.path.dirname(sys.executable)
    # Writable per-user data (engine history, log)
    DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "chess_player")
else:
    RESOURCE_DIR = APP_DIR = DATA_DIR = os.path.dirname(os.path.abspath(__file__))

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

# Don't flash a console window for each engine process on Windows
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ──────────────────────────────────────────────────────────────────────────────
# Engine history helpers
# ──────────────────────────────────────────────────────────────────────────────

_HISTORY_FILE = os.path.join(DATA_DIR, ".engine_history.json")

def _load_engine_history() -> list:
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

def _save_engine_history(path: str):
    path = os.path.abspath(path)
    history = [p for p in _load_engine_history() if p != path]
    history.insert(0, path)
    try:
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[:30], f, indent=2)
    except OSError:
        pass


# Last engines used for White/Black, restored on the next start
_LAST_ENGINES_FILE = os.path.join(DATA_DIR, ".last_engines.json")

def _load_last_engines() -> dict:
    try:
        with open(_LAST_ENGINES_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def _save_last_engines(white, black):
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

def _default_time_control(total: float = 60.0, inc: float = 0.0) -> dict:
    return {"separate": False,
            "white_time": total, "white_inc": inc,
            "black_time": total, "black_inc": inc}

def _load_time_settings() -> Optional[dict]:
    try:
        with open(_TIME_SETTINGS_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
        tc = _default_time_control()
        tc["separate"] = bool(d.get("separate", False))
        for k in ("white_time", "white_inc", "black_time", "black_inc"):
            v = float(d.get(k, tc[k]))
            if v < 0 or (k.endswith("_time") and v <= 0):
                return None
            tc[k] = v
        return tc
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None

def _save_time_settings(tc: dict):
    try:
        with open(_TIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(tc, f, indent=2)
    except OSError:
        pass


def parse_duration(s: str) -> float:
    """Parse '90', '1:30', '1:30.5' or '1:00:00' into seconds. Raises ValueError."""
    s = s.strip()
    if not s:
        raise ValueError("empty")
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    if total < 0:
        raise ValueError("negative")
    return total


def fmt_duration(sec: float) -> str:
    """Inverse of parse_duration for display in the settings dialog."""
    sec = float(sec)
    if sec < 60:
        return f"{sec:g}"
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:02g}" if s == int(s) else f"{int(m)}:{s:04.1f}"


# ──────────────────────────────────────────────────────────────────────────────
# UCI Engine wrapper
# ──────────────────────────────────────────────────────────────────────────────

class UCIEngine:
    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name
        self.process: Optional[subprocess.Popen] = None
        self._q: queue.Queue = queue.Queue()
        self.last_score: Optional[str] = None
        self.last_depth: Optional[int] = None   # from UCI 'info depth', if reported
        self.last_nodes: Optional[int] = None   # from UCI 'info nodes', if reported
        self.log: list[tuple[str, str]] = []   # (">"|"<", line)
        self.options: dict = {}                # name → {type, default, min, max, var, value}

    def start(self):
        abs_path = os.path.abspath(self.path)
        self.process = subprocess.Popen(
            [abs_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=os.path.dirname(abs_path),
            creationflags=_POPEN_FLAGS,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        self._send("uci")
        if not self._wait("uciok", timeout=15):
            raise RuntimeError(f"{self.name}: did not respond to 'uci'")
        self._parse_uci_options()
        self._send("isready")
        if not self._wait("readyok", timeout=15):
            raise RuntimeError(f"{self.name}: did not respond to 'isready'")

    def _reader(self):
        for line in self.process.stdout:
            line = line.rstrip()
            self.log.append(("<", line))
            self._q.put(line)

    def _send(self, cmd: str):
        self.log.append((">", cmd))
        try:
            self.process.stdin.write(cmd + "\n")
            self.process.stdin.flush()
        except OSError:
            pass

    def _wait(self, token: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._q.get(timeout=0.05)
                if token in line:
                    return True
            except queue.Empty:
                pass
        return False

    def new_game(self):
        # Drain any stale output left over from a previous game or stop command
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._send("ucinewgame")
        self._send("isready")
        self._wait("readyok", timeout=10)

    def get_move(
        self,
        board: chess.Board,
        wtime_ms: int,
        btime_ms: int,
        winc_ms: int,
        binc_ms: int,
        hard_limit_s: float,
        info_callback=None,
        stop_event: Optional[threading.Event] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        moves = " ".join(m.uci() for m in board.move_stack)
        pos = f"position startpos moves {moves}" if moves else "position startpos"
        self._send(pos)
        self._send(f"go wtime {wtime_ms} btime {btime_ms} winc {winc_ms} binc {binc_ms}")

        self.last_score = None
        self.last_depth = None
        self.last_nodes = None
        deadline = time.monotonic() + hard_limit_s

        while time.monotonic() < deadline:
            if stop_event and stop_event.is_set():
                self._send("stop")
                return None, self.last_score
            try:
                line = self._q.get(timeout=min(0.05, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if line.startswith("info"):
                self._parse_info(line)
                if info_callback:
                    info_callback(line)
            elif line.startswith("bestmove"):
                parts = line.split()
                move = parts[1] if len(parts) > 1 and parts[1] != "(none)" else None
                return move, self.last_score

        self._send("stop")
        drain_deadline = time.monotonic() + 1.0
        while time.monotonic() < drain_deadline:
            try:
                line = self._q.get(timeout=0.1)
                if line.startswith("info"):
                    self._parse_info(line)
                elif line.startswith("bestmove"):
                    parts = line.split()
                    move = parts[1] if len(parts) > 1 and parts[1] != "(none)" else None
                    return move, self.last_score
            except queue.Empty:
                pass
        return None, self.last_score

    def _parse_info(self, line: str):
        parts = line.split()
        if len(parts) > 1 and parts[1] == "string":
            return   # free text; may contain words like 'depth'
        try:
            if "score" in parts:
                idx  = parts.index("score")
                kind = parts[idx + 1]
                val  = int(parts[idx + 2])
                if kind == "cp":
                    self.last_score = f"{val / 100:+.2f}"
                elif kind == "mate":
                    self.last_score = f"M{val}" if val > 0 else f"-M{abs(val)}"
            if "depth" in parts:
                self.last_depth = int(parts[parts.index("depth") + 1])
            if "nodes" in parts:
                self.last_nodes = int(parts[parts.index("nodes") + 1])
        except (IndexError, ValueError):
            pass

    def _parse_uci_options(self):
        for direction, line in self.log:
            if direction == "<" and line.startswith("option "):
                self._parse_option_line(line)

    def _parse_option_line(self, line: str):
        # option name <id> type <t> [default <x>] [min <x>] [max <x>] [var <x>]*
        parts = line.split()
        try:
            name_idx = parts.index("name") + 1
            type_idx = parts.index("type")
            name = " ".join(parts[name_idx:type_idx])
            opt_type = parts[type_idx + 1]
        except (ValueError, IndexError):
            return
        opt = {"type": opt_type, "default": None, "min": None, "max": None, "var": [], "value": None}
        remaining = parts[type_idx + 2:]
        i = 0
        while i < len(remaining):
            key = remaining[i]
            if key in ("default", "min", "max"):
                j = i + 1
                while j < len(remaining) and remaining[j] not in ("min", "max", "var", "default"):
                    j += 1
                opt[key] = " ".join(remaining[i + 1:j]) if j > i + 1 else (remaining[i + 1] if i + 1 < len(remaining) else "")
                i = j
            elif key == "var":
                j = i + 1
                while j < len(remaining) and remaining[j] not in ("min", "max", "var", "default"):
                    j += 1
                opt["var"].append(" ".join(remaining[i + 1:j]))
                i = j
            else:
                i += 1
        opt["value"] = opt["default"]
        self.options[name] = opt

    def set_option(self, name: str, value: str):
        self._send(f"setoption name {name} value {value}")

    def quit(self):
        try:
            self._send("quit")
            self.process.wait(timeout=3)
        except Exception:
            if self.process:
                self.process.kill()


# ──────────────────────────────────────────────────────────────────────────────
# Console helpers
# ──────────────────────────────────────────────────────────────────────────────

_PIECE_CHAR = {
    (chess.PAWN,   True): "P", (chess.PAWN,   False): "p",
    (chess.KNIGHT, True): "N", (chess.KNIGHT, False): "n",
    (chess.BISHOP, True): "B", (chess.BISHOP, False): "b",
    (chess.ROOK,   True): "R", (chess.ROOK,   False): "r",
    (chess.QUEEN,  True): "Q", (chess.QUEEN,  False): "q",
    (chess.KING,   True): "K", (chess.KING,   False): "k",
}

def board_ascii(board: chess.Board) -> str:
    lines = ["  a b c d e f g h"]
    for rank in range(7, -1, -1):
        row = f"{rank + 1} "
        for file in range(8):
            sq = chess.square(file, rank)
            p  = board.piece_at(sq)
            row += (_PIECE_CHAR[(p.piece_type, p.color)] if p else ".") + " "
        lines.append(row.rstrip())
    return "\n".join(lines)

def fmt_time(seconds: float) -> str:
    s   = max(0.0, seconds)
    m   = int(s) // 60
    sec = int(s) % 60
    ds  = int((s % 1) * 10)
    return f"{m:02d}:{sec:02d}.{ds}"


# ──────────────────────────────────────────────────────────────────────────────
# Eval helpers
# ──────────────────────────────────────────────────────────────────────────────

def parse_eval(s: Optional[str]) -> Optional[float]:
    """Score string ('+1.23', 'M3', '-M5') -> float clamped to +-10, or None."""
    if s is None:
        return None
    s = s.strip()
    if s.startswith("M"):
        return 10.0
    if s.startswith("-M"):
        return -10.0
    try:
        return max(-10.0, min(10.0, float(s)))
    except ValueError:
        return None


def fmt_count(v: float) -> str:
    """1500 -> '1.5k', 2000000 -> '2M'."""
    for div, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if v >= div:
            return f"{v / div:g}{suffix}"
    return f"{v:g}"


# Metrics the graph panel can show, in display order (top to bottom)
GRAPH_METRICS = ("eval", "depth", "nodes")


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

def _load_gui_settings() -> dict:
    return _load_json_dict(_GUI_SETTINGS_FILE)

def _save_gui_settings(update: dict):
    d = _load_gui_settings()
    d.update(update)
    _save_json_dict(_GUI_SETTINGS_FILE, d)

def _load_tournament_settings() -> dict:
    return _load_json_dict(_TOURNAMENT_SETTINGS_FILE)

def _save_tournament_settings(d: dict):
    _save_json_dict(_TOURNAMENT_SETTINGS_FILE, d)


def _write_json_atomic(path: str, d: dict):
    """Write JSON via a temp file so a crash never leaves a half-written file.
    Raises OSError."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────────────────
# Game records — every finished game is saved as a PGN file in games/
# ──────────────────────────────────────────────────────────────────────────────

GAMES_DIR = os.path.join(DATA_DIR, "games")

def _safe_filename(s: str, maxlen: int = 40) -> str:
    out = "".join(ch if ch.isalnum() or ch in "._-+" else "_" for ch in s).strip("._")
    return out[:maxlen] or "engine"

def _save_game_record(pgn_game) -> Optional[str]:
    """Save a finished game to GAMES_DIR. Returns the file path, or None."""
    h = pgn_game.headers
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}_{_safe_filename(h.get('White', '?'))}-vs-{_safe_filename(h.get('Black', '?'))}"
    try:
        os.makedirs(GAMES_DIR, exist_ok=True)
        path = os.path.join(GAMES_DIR, base + ".pgn")
        k = 2
        while os.path.exists(path):
            path = os.path.join(GAMES_DIR, f"{base}_{k}.pgn")
            k += 1
        with open(path, "w", encoding="utf-8") as fh:
            print(pgn_game, file=fh, end="\n\n")
        return path
    except OSError as e:
        print(f"  Could not save game record: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Tournament state files — the running tournament is autosaved after every
# game and resumed on the next start; a stopped one can be saved to a file
# ──────────────────────────────────────────────────────────────────────────────

_CURRENT_TOURNAMENT_FILE = os.path.join(DATA_DIR, ".current_tournament.json")
TOURNAMENTS_DIR          = os.path.join(DATA_DIR, "tournaments")
_TOURNAMENT_STATE_VERSION = 1

def _check_tournament_state(d) -> dict:
    """Validate a tournament state dict (see run_tournament). Raises ValueError."""
    if not isinstance(d, dict) or d.get("kind") != "chess_player tournament":
        raise ValueError("not a chess_player tournament file")
    if d.get("version") != _TOURNAMENT_STATE_VERSION:
        raise ValueError(f"unsupported tournament file version: {d.get('version')!r}")
    try:
        engines = [{"name": str(e["name"]), "path": str(e["path"])} for e in d["engines"]]
        t, inc = float(d["time"]), float(d["inc"])
        tour = Tournament.from_dict(d["tournament"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"invalid tournament file ({e})") from None
    if t <= 0 or inc < 0:
        raise ValueError("invalid time control in tournament file")
    if [e["name"] for e in engines] != tour.names:
        raise ValueError("engine list does not match the tournament")
    return {**d, "engines": engines, "time": t, "inc": inc}

def _load_tournament_file(path: str, check_engines: bool = True) -> dict:
    """Load and validate a saved tournament. Raises ValueError."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"could not read {os.path.basename(path)}: {e}") from None
    d = _check_tournament_state(d)
    if check_engines:
        for e in d["engines"]:
            if not os.path.isfile(e["path"]):
                raise ValueError(f"engine not found: {e['path']}")
    return d

def _remove_current_tournament():
    try:
        os.remove(_CURRENT_TOURNAMENT_FILE)
    except OSError:
        pass


def _engine_candidates() -> list:
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
    for hpath in _load_engine_history():
        apath = os.path.abspath(hpath)
        if apath not in seen and os.path.isfile(apath):
            name = os.path.splitext(os.path.basename(apath))[0]
            items.append((f"{name}  ↩", apath, "recent"))
            seen.add(apath)
    return items


# ──────────────────────────────────────────────────────────────────────────────
# Tournament logic (pairings, standings) — no engine or GUI code here
# ──────────────────────────────────────────────────────────────────────────────

_SHORT_REASON = {
    "checkmate": "mate", "stalemate": "stalemate",
    "insufficient material": "material", "threefold repetition": "3-fold",
    "fivefold repetition": "5-fold", "fifty moves": "50-move",
    "seventyfive moves": "75-move", "time forfeit": "time",
    "no move": "no move", "illegal move": "illegal", "malformed move": "malformed",
}

def _game_points(result: str) -> Tuple[float, float]:
    """(white points, black points) for a PGN result string."""
    return {"1-0": (1.0, 0.0), "0-1": (0.0, 1.0), "1/2-1/2": (0.5, 0.5)}.get(result, (0.0, 0.0))


class Tournament:
    """
    Round robin ("rr"):  `count` = number of rounds, i.e. full cycles in which
                         every engine meets every other engine once. Colors
                         are reversed in every second cycle.
    Swiss ("swiss"):     `count` = number of matches per engine (Swiss rounds).
                         Each round pairs engines with equal or similar scores
                         who have not met yet, balancing colors. With an odd
                         number of engines one gets a bye worth 1 point.

    Tie-break: Sonneborn-Berger (SB) = sum of the final scores of the opponents
    this engine beat, plus half the scores of the opponents it drew with.
    """

    def __init__(self, names: list, fmt: str, count: int):
        if len(names) < 2:
            raise ValueError("a tournament needs at least 2 engines")
        if fmt not in ("rr", "swiss"):
            raise ValueError(f"unknown tournament format: {fmt!r}")
        self.names = list(names)
        self.n     = len(names)
        self.fmt   = fmt
        self.count = max(1, int(count))
        self.events: list = []   # chronological: {"type": "game"|"bye", ...}
        self.current: Optional[Tuple[int, int, int]] = None   # (round, white, black)
        # Swiss pairings per round, fixed once made (they depend on results,
        # so a resumed tournament must not recompute a half-played round)
        self._pairings: dict = {}
        if fmt == "rr":
            self._rr = self._round_robin_schedule()
            self.total_rounds = len(self._rr)
            self.total_games  = sum(len(pairs) for pairs, _ in self._rr)
        else:
            self.total_rounds = self.count
            self.total_games  = self.count * (self.n // 2)

    # ── Results ──

    @property
    def games(self) -> list:
        return [e for e in self.events if e["type"] == "game"]

    def record(self, rnd: int, white: int, black: int, result: str, reason: str = ""):
        self.events.append({"type": "game", "round": rnd, "white": white,
                            "black": black, "result": result, "reason": reason})

    def record_bye(self, rnd: int, player: int):
        self.events.append({"type": "bye", "round": rnd, "player": player})

    def has_game(self, rnd: int, white: int, black: int) -> bool:
        return any(e["type"] == "game" and (e["round"], e["white"], e["black"]) == (rnd, white, black)
                   for e in self.events)

    def has_bye(self, rnd: int) -> bool:
        return any(e["type"] == "bye" and e["round"] == rnd for e in self.events)

    # ── Persistence ──

    def to_dict(self) -> dict:
        return {"names": list(self.names), "format": self.fmt, "count": self.count,
                "events": [dict(e) for e in self.events],
                "pairings": {str(r): {"pairs": [list(p) for p in pairs], "bye": bye}
                             for r, (pairs, bye) in self._pairings.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "Tournament":
        """Inverse of to_dict. Raises ValueError/KeyError/TypeError on bad data."""
        t = cls([str(n) for n in d["names"]], d["format"], int(d["count"]))
        idx = range(t.n)
        for e in d.get("events", []):
            rnd = int(e["round"])
            if not 0 <= rnd < t.total_rounds:
                raise ValueError(f"round out of range: {rnd}")
            if e["type"] == "bye":
                if int(e["player"]) not in idx:
                    raise ValueError("bad bye player")
                t.record_bye(rnd, int(e["player"]))
            elif e["type"] == "game":
                w, b = int(e["white"]), int(e["black"])
                if w not in idx or b not in idx or w == b:
                    raise ValueError("bad game players")
                if e["result"] not in ("1-0", "0-1", "1/2-1/2"):
                    raise ValueError(f"bad result: {e['result']!r}")
                t.record(rnd, w, b, e["result"], str(e.get("reason", "")))
            else:
                raise ValueError(f"unknown event type: {e['type']!r}")
        for r, p in (d.get("pairings") or {}).items():
            pairs = [(int(a), int(b)) for a, b in p["pairs"]]
            bye = None if p.get("bye") is None else int(p["bye"])
            t._pairings[int(r)] = (pairs, bye)
        return t

    def score(self, i: int) -> float:
        pts = 0.0
        for e in self.events:
            if e["type"] == "bye":
                if e["player"] == i:
                    pts += 1.0
            elif i in (e["white"], e["black"]):
                wp, bp = _game_points(e["result"])
                pts += wp if e["white"] == i else bp
        return pts

    def sb(self, i: int) -> float:
        """Sonneborn-Berger tie-break score."""
        total = 0.0
        for g in self.games:
            if i not in (g["white"], g["black"]):
                continue
            wp, bp = _game_points(g["result"])
            mine, opp = (wp, g["black"]) if g["white"] == i else (bp, g["white"])
            total += mine * self.score(opp)
        return total

    def standings(self) -> list:
        rows = []
        for i, name in enumerate(self.names):
            w = d = l = 0
            for g in self.games:
                if i not in (g["white"], g["black"]):
                    continue
                wp, bp = _game_points(g["result"])
                mine = wp if g["white"] == i else bp
                if mine == 1.0:
                    w += 1
                elif mine == 0.5:
                    d += 1
                else:
                    l += 1
            rows.append({"idx": i, "name": name, "games": w + d + l,
                         "wins": w, "draws": d, "losses": l,
                         "points": self.score(i), "sb": self.sb(i)})
        rows.sort(key=lambda r: (-r["points"], -r["sb"], -r["wins"], r["idx"]))
        # Shared rank when points and tie-break are both equal
        for k, r in enumerate(rows):
            prev = rows[k - 1] if k else None
            same = prev and (prev["points"], prev["sb"]) == (r["points"], r["sb"])
            r["rank"] = prev["rank"] if same else k + 1
        return rows

    # ── Pairings ──

    def pairings(self, rnd: int) -> Tuple[list, Optional[int]]:
        """([(white, black), ...], bye player or None) for round `rnd` (0-based).
        Swiss pairings depend on earlier results: call once the previous round
        is complete."""
        if self.fmt == "rr":
            pairs, bye = self._rr[rnd]
            return list(pairs), bye
        if rnd not in self._pairings:
            self._pairings[rnd] = self._swiss_pairings(rnd)
        pairs, bye = self._pairings[rnd]
        return list(pairs), bye

    def _round_robin_schedule(self) -> list:
        """Circle method. Returns [(pairs, sitting_out_player)] per round."""
        arr = list(range(self.n)) + ([None] if self.n % 2 else [])
        m = len(arr)
        cycle = []
        balance = [0] * self.n   # whites minus blacks so far in the cycle
        for k in range(m - 1):
            pairs, bye = [], None
            for i in range(m // 2):
                a, b = arr[i], arr[m - 1 - i]
                if a is None or b is None:
                    bye = b if a is None else a
                    continue
                # White to whoever has had it less; alternate on a tie
                if balance[a] != balance[b]:
                    w, bl = (a, b) if balance[a] < balance[b] else (b, a)
                else:
                    w, bl = (a, b) if (k + i) % 2 == 0 else (b, a)
                balance[w] += 1
                balance[bl] -= 1
                pairs.append((w, bl))
            cycle.append((pairs, bye))
            arr = [arr[0], arr[-1]] + arr[1:-1]
        rounds = []
        for c in range(self.count):
            for pairs, bye in cycle:
                rounds.append(([(b, a) for a, b in pairs] if c % 2 else list(pairs), bye))
        return rounds

    def _colors(self, i: int) -> list:
        """Color history of player i: +1 = White, -1 = Black."""
        out = []
        for g in self.games:
            if g["white"] == i:
                out.append(1)
            elif g["black"] == i:
                out.append(-1)
        return out

    def _meetings(self) -> dict:
        met: dict = {}
        for g in self.games:
            key = (min(g["white"], g["black"]), max(g["white"], g["black"]))
            met[key] = met.get(key, 0) + 1
        return met

    def _swiss_pairings(self, rnd: int) -> Tuple[list, Optional[int]]:
        order = sorted(range(self.n), key=lambda i: (-self.score(i), -self.sb(i), i))
        bye = None
        if self.n % 2:
            had_bye = {e["player"] for e in self.events if e["type"] == "bye"}
            bottom_up = [p for p in reversed(order) if p not in had_bye] or list(reversed(order))
            bye = bottom_up[0]
            order.remove(bye)

        if rnd == 0:
            # Top half vs bottom half, alternating colors by board
            h = len(order) // 2
            pairs = [(order[i], order[i + h]) if i % 2 == 0 else (order[i + h], order[i])
                     for i in range(h)]
            return pairs, bye

        met = self._meetings()
        # Avoid rematches; relax only when no pairing exists (more rounds
        # than opponents), allowing at most `limit` earlier meetings per pair.
        result = None
        for limit in range(0, self.total_rounds + 1):
            self._budget = 20000
            result = self._pair_rec(order, met, limit)
            if result is not None:
                break
        if result is None:   # unreachable in practice; fall back to adjacent pairs
            result = [(order[i], order[i + 1]) for i in range(0, len(order) - 1, 2)]
        return [self._assign_colors(a, b, rnd) for a, b in result], bye

    def _pair_rec(self, order: list, met: dict, limit: int) -> Optional[list]:
        if not order:
            return []
        p, rest = order[0], order[1:]
        for j, q in enumerate(rest):
            if met.get((min(p, q), max(p, q)), 0) > limit:
                continue
            self._budget -= 1
            if self._budget < 0:
                return None
            sub = self._pair_rec(rest[:j] + rest[j + 1:], met, limit)
            if sub is not None:
                return [(p, q)] + sub
        return None

    def _assign_colors(self, a: int, b: int, rnd: int) -> Tuple[int, int]:
        """a is the higher-ranked player. Returns (white, black)."""
        ca, cb = self._colors(a), self._colors(b)
        if sum(ca) != sum(cb):
            return (a, b) if sum(ca) < sum(cb) else (b, a)
        la, lb = (ca[-1] if ca else 0), (cb[-1] if cb else 0)
        if la != lb:
            return (a, b) if la < lb else (b, a)
        return (a, b) if rnd % 2 == 0 else (b, a)

    # ── Display ──

    def snapshot(self, status: str, tc_text: str) -> dict:
        """Plain-data view for the GUI. status: starting|running|finished|stopped."""
        done = len(self.games)
        rnd = (self.current[0] if self.current else
               max((e["round"] for e in self.events), default=-1))
        fmt_name = "Round robin" if self.fmt == "rr" else "Swiss"
        results = []
        for e in reversed(self.events):
            if e["type"] == "bye":
                results.append({"type": "bye", "round": e["round"] + 1,
                                "name": self.names[e["player"]]})
            else:
                results.append({"type": "game", "round": e["round"] + 1,
                                "white": self.names[e["white"]],
                                "black": self.names[e["black"]],
                                "result": e["result"],
                                "reason": _SHORT_REASON.get(e["reason"], e["reason"])})
        current = None
        if self.current and status == "running":
            current = (self.names[self.current[1]], self.names[self.current[2]])
        return {
            "title":    f"{fmt_name} · {self.n} engines",
            "progress": (f"Round {max(rnd, 0) + 1}/{self.total_rounds} · "
                         f"game {min(done + (1 if current else 0), self.total_games)}"
                         f"/{self.total_games}"),
            "tc":       tc_text,
            "status":   status,
            "current":  current,
            "rows":     self.standings(),
            "results":  results,
        }


# ──────────────────────────────────────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────────────────────────────────────

class ChessGUI:
    SQ      = 68          # board square size (px)
    GW      = 250         # eval graph panel width (px)
    LIGHT   = "#F0D9B5"
    DARK    = "#B58863"
    HI      = "#CDD16E"   # last-move highlight
    BG_IDLE = "#2b2b2b"
    BG_ACTV = "#3a5a00"
    GBG     = "#1a1a1a"   # graph background
    GCARD   = "#222222"   # background of each graph card
    C_W     = "#ffffff"   # white-engine line
    C_B     = "#888888"   # black-engine line


    def __init__(self, white_name: str, black_name: str, engines_ready: bool = True):
        import tkinter as tk
        self._tk = tk
        self.root = tk.Tk()
        self.root.title(f"Chess Player {__version__}")
        ico = os.path.join(RESOURCE_DIR, "chess_player.ico")
        if sys.platform == "win32" and os.path.exists(ico):
            try:
                self.root.iconbitmap(default=ico)
            except tk.TclError:
                pass
        self.root.resizable(True, True)
        self.root.configure(bg=self.BG_IDLE)
        # Named fonts, so a font-size change in ⚙ settings updates every widget
        self._fonts: dict = {}
        try:
            fs = int(_load_gui_settings().get("font_size", self.FONT_DEFAULT))
        except (TypeError, ValueError):
            fs = self.FONT_DEFAULT
        self._font_size = max(self.FONT_MIN, min(self.FONT_MAX, fs))
        # Board geometry; recomputed whenever the board canvas is resized
        self._sq = self.SQ
        self._bx = self._by = 0
        self._cur_board = chess.Board()
        self._cur_last: Optional[chess.Move] = None
        self._animating = False
        self.white_name = white_name
        self.black_name = black_name
        self._uq: queue.Queue = queue.Queue()
        self._restart_cb = None
        self._white_engine = None
        self._black_engine = None
        self._resume_event = threading.Event()   # cleared = paused/waiting
        self._game_over    = False
        self._think_start: Optional[float] = None
        self._think_color: Optional[chess.Color] = None
        self._think_budget: float = 0.0
        self._game_started     = False
        self._change_engine_cb = None
        self._swap_cb = None
        self._settings_get_cb   = None
        self._settings_apply_cb = None
        self._engines_ready = engines_ready
        # Tournament mode
        self._tournament_running  = False
        self._tournament_start_cb = None
        self._tournament_stop_cb  = None
        self._last_tournament_snap: Optional[dict] = None
        # Graph panel: which metrics to show, and the last data drawn
        sel = [m for m in _load_gui_settings().get("graph_metrics", ["eval"])
               if m in GRAPH_METRICS]
        self._graph_sel: set = set(sel or ["eval"])
        self._graph_hist: dict = {}
        self._piece_src  = self._load_pieces()   # PIL images at source size
        self._piece_cache: dict = {}             # pixel size -> {(pt, white): PhotoImage}
        self._piece_imgs = self._pieces_at(self._sq)
        self._anim_done  = threading.Event()
        self._build()
        self._update_min_size()
        self._poll()

    # ── Fonts ──

    FONT_DEFAULT, FONT_MIN, FONT_MAX = 10, 7, 24

    @property
    def _scale(self) -> float:
        return self._font_size / self.FONT_DEFAULT

    def _font(self, size: int, weight: str = "normal", family: str = "Consolas"):
        """Shared named font. `size` is the size at the default font setting."""
        key = (family, size, weight)
        f = self._fonts.get(key)
        if f is None:
            import tkinter.font as tkfont
            f = tkfont.Font(root=self.root, family=family, weight=weight,
                            size=max(6, round(size * self._scale)))
            self._fonts[key] = f
        return f

    def set_font_size(self, size: int):
        size = max(self.FONT_MIN, min(self.FONT_MAX, int(size)))
        if size == self._font_size:
            return
        self._font_size = size
        _save_gui_settings({"font_size": size})
        for (_, base, _), f in self._fonts.items():
            f.configure(size=max(6, round(base * self._scale)))
        self._update_min_size()
        self._layout()
        self._draw_graph()
        if self._last_tournament_snap:
            self._render_tournament(self._last_tournament_snap)

    # ── Window layout ──
    # The content area (board column, graph, tournament panel) is laid out by
    # hand with place(): the board takes the largest square that fits, the
    # graph gets the remaining width, and the tournament panel a fixed width.

    def _gw_min(self) -> int:
        return round(self.GW * self._scale)

    def _tw(self) -> int:
        return round(self.TW * self._scale) if self._tpanel_shown else 0

    def _update_min_size(self):
        """Set the content's natural size (board at its default size) and use
        the resulting window size as the minimum: the window can only grow."""
        self.root.update_idletasks()
        ph = self._bf.winfo_reqheight() + self._wf.winfo_reqheight()
        self._content.config(width=8 * self.SQ + self._gw_min() + self._tw(),
                             height=8 * self.SQ + ph)
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())

    def _layout(self, event=None):
        c = self._content
        W, H = c.winfo_width(), c.winfo_height()
        if W < 50 or H < 50:
            return
        ph = self._bf.winfo_reqheight() + self._wf.winfo_reqheight()
        tw = self._tw()
        bs = max(8 * 16, min(H - ph, W - tw - self._gw_min()))
        gw = max(1, W - tw - bs)
        top = max(0, (H - ph - bs) // 2)
        self._left.place(x=0, y=top, width=bs, height=bs + ph)
        self._right.place(x=bs, y=0, width=gw, height=H)
        if tw:
            self._tpanel.place(x=bs + gw, y=0, width=tw, height=H)
        else:
            self._tpanel.place_forget()

    def _on_board_resize(self, event):
        sq = max(16, min(event.width, event.height) // 8)
        self._bx = (event.width - 8 * sq) // 2
        self._by = (event.height - 8 * sq) // 2
        if sq != self._sq:
            self._sq = sq
            self._piece_imgs = self._pieces_at(sq)
        if not self._animating:
            self._draw_board(self._cur_board, self._cur_last)

    def set_restart_callback(self, cb):
        self._restart_cb = cb

    def set_engines(self, white_engine, black_engine):
        self._white_engine = white_engine
        self._black_engine = black_engine

    def set_change_engine_callback(self, cb):
        self._change_engine_cb = cb

    def set_swap_callback(self, cb):
        self._swap_cb = cb

    def set_tournament_callbacks(self, start_cb, stop_cb):
        """start_cb(cfg dict) starts a tournament; stop_cb() aborts it."""
        self._tournament_start_cb = start_cb
        self._tournament_stop_cb  = stop_cb

    @property
    def tournament_running(self) -> bool:
        return self._tournament_running

    def set_settings_callbacks(self, get_cb, apply_cb):
        """get_cb() -> time-control dict; apply_cb(dict) stores/applies it."""
        self._settings_get_cb   = get_cb
        self._settings_apply_cb = apply_cb

    @property
    def game_in_progress(self) -> bool:
        return self._game_started and not self._game_over

    # ── Settings dialog (time control) ──

    def _open_settings(self):
        if not self._settings_get_cb:
            return
        tk  = self._tk
        BG, FG, DIM = "#1a1a1a", "#cccccc", "#888888"
        ENTRY = dict(font=self._font(10), width=9, bg="#2e2e2e", fg="white",
                     insertbackground="white", relief="flat",
                     highlightthickness=1, highlightbackground="#444444",
                     highlightcolor="#55aaff", justify="right")
        tc = dict(self._settings_get_cb())

        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.configure(bg=BG, padx=18, pady=14)
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text="Time control", font=self._font(12, "bold"),
                 fg="white", bg=BG).grid(row=0, column=0, columnspan=3,
                                         sticky="w", pady=(0, 10))

        grid = tk.Frame(win, bg=BG)
        grid.grid(row=1, column=0, columnspan=3, sticky="w")
        tk.Label(grid, text="", bg=BG).grid(row=0, column=0)
        tk.Label(grid, text="Time", font=self._font(9), fg=DIM, bg=BG
                 ).grid(row=0, column=1, sticky="e", padx=(10, 0))
        tk.Label(grid, text="Increment", font=self._font(9), fg=DIM, bg=BG
                 ).grid(row=0, column=2, sticky="e", padx=(10, 0))

        def _row(r, label, t, inc):
            lbl = tk.Label(grid, text=label, font=self._font(10), fg=FG, bg=BG,
                           anchor="w", width=20)
            te  = tk.Entry(grid, **ENTRY)
            ie  = tk.Entry(grid, **ENTRY)
            te.insert(0, fmt_duration(t))
            ie.insert(0, f"{inc:g}")
            lbl.grid(row=r, column=0, sticky="w", pady=3)
            te.grid(row=r, column=1, padx=(10, 0), pady=3)
            ie.grid(row=r, column=2, padx=(10, 0), pady=3)
            return (lbl, te, ie)

        both_row  = _row(1, "Both engines", tc["white_time"], tc["white_inc"])
        white_row = _row(2, f"♙ {self.white_name[:17]}", tc["white_time"], tc["white_inc"])
        black_row = _row(3, f"♟ {self.black_name[:17]}", tc["black_time"], tc["black_inc"])

        sep_var = tk.BooleanVar(value=tc["separate"])

        def _sync_rows(*_):
            if sep_var.get():
                for w in both_row:
                    w.grid_remove()
                for row in (white_row, black_row):
                    for w in row:
                        w.grid()
            else:
                for row in (white_row, black_row):
                    for w in row:
                        w.grid_remove()
                for w in both_row:
                    w.grid()

        def _on_toggle():
            if sep_var.get():
                # Copy shared values into both per-engine rows
                for _, te, ie in (white_row, black_row):
                    te.delete(0, "end"); te.insert(0, both_row[1].get())
                    ie.delete(0, "end"); ie.insert(0, both_row[2].get())
            else:
                # Collapse back to White's values
                both_row[1].delete(0, "end"); both_row[1].insert(0, white_row[1].get())
                both_row[2].delete(0, "end"); both_row[2].insert(0, white_row[2].get())
            _sync_rows()

        tk.Checkbutton(win, text="Different time per engine", variable=sep_var,
                       command=_on_toggle, font=self._font(10), fg=FG, bg=BG,
                       selectcolor="#2e2e2e", activebackground=BG,
                       activeforeground="white", cursor="hand2"
                       ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        tk.Label(win, text="Time: seconds or m:ss   Increment: seconds",
                 font=self._font(8), fg="#666666", bg=BG
                 ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        note = ("Changes apply from the next game." if self.game_in_progress
                else "")
        msg = tk.Label(win, text=note, font=self._font(9), fg="#ffaa44", bg=BG)
        msg.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        tk.Label(win, text="Display", font=self._font(12, "bold"),
                 fg="white", bg=BG).grid(row=5, column=0, columnspan=3,
                                         sticky="w", pady=(14, 6))
        dfr = tk.Frame(win, bg=BG)
        dfr.grid(row=6, column=0, columnspan=3, sticky="w")
        tk.Label(dfr, text="Font size", font=self._font(10), fg=FG, bg=BG,
                 anchor="w", width=20).pack(side="left")
        font_var = tk.StringVar(value=str(self._font_size))
        tk.Spinbox(dfr, from_=self.FONT_MIN, to=self.FONT_MAX, textvariable=font_var,
                   width=5, font=self._font(10), bg="#2e2e2e", fg="white",
                   relief="flat", buttonbackground="#3a3a3a",
                   insertbackground="white", justify="right").pack(side="left", padx=(10, 0))
        tk.Label(dfr, text=f"(default {self.FONT_DEFAULT})", font=self._font(9),
                 fg=DIM, bg=BG).pack(side="left", padx=(8, 0))

        _sync_rows()

        def _read(row, what):
            _, te, ie = row
            try:
                t = parse_duration(te.get())
                if t <= 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Invalid time for {what}: {te.get()!r}") from None
            try:
                i = float(ie.get().strip() or 0)
                if i < 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Invalid increment for {what}: {ie.get()!r}") from None
            return t, i

        def _ok(event=None):
            try:
                if sep_var.get():
                    wt, wi = _read(white_row, "White")
                    bt, bi = _read(black_row, "Black")
                else:
                    wt, wi = _read(both_row, "both engines")
                    bt, bi = wt, wi
            except ValueError as e:
                msg.config(text=str(e), fg="#ff5533")
                return
            try:
                fsize = int(font_var.get())
                if not self.FONT_MIN <= fsize <= self.FONT_MAX:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Font size must be {self.FONT_MIN}–{self.FONT_MAX}",
                           fg="#ff5533")
                return
            new_tc = {"separate": sep_var.get(),
                      "white_time": wt, "white_inc": wi,
                      "black_time": bt, "black_inc": bi}
            win.destroy()
            self.set_font_size(fsize)
            if self._settings_apply_cb and new_tc != dict(self._settings_get_cb()):
                self._settings_apply_cb(new_tc)

        def _cancel(event=None):
            win.destroy()

        bf = tk.Frame(win, bg=BG)
        bf.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
        tk.Button(bf, text="OK", font=self._font(9),
                  bg="#2a4a00", fg="#88ff44", relief="flat", padx=14, pady=4,
                  activebackground="#3a6a00", activeforeground="#aaff66",
                  cursor="hand2", command=_ok).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=12, pady=4,
                  activebackground="#555555", activeforeground="white",
                  cursor="hand2", command=_cancel).pack(side="left")

        win.bind("<Escape>", _cancel)
        win.bind("<Return>", _ok)

        # Centre over the main window, then make modal
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()  - win.winfo_reqwidth())  // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        (white_row if sep_var.get() else both_row)[1].focus_set()
        win.wait_window()

    # ── Action button (Stop / Swap-colors) ──

    def _update_action_btn(self):
        if self._tournament_running or (self._game_started and not self._game_over):
            self._action_btn.config(text="■", fg="#ff4444", activeforeground="#ff6666")
        else:
            self._action_btn.config(text="⇆", fg="#aaaaaa", activeforeground="#cccccc")

    def _on_action_btn(self):
        if self._tournament_running:
            if self._resume_event.is_set():
                self._resume_event.clear()
                self._play_btn.config(text="▶", fg="#88ff44")
            stop = self._tournament_stop_cb or (lambda save: None)
            self._open_stop_confirm(
                "Stop the tournament?\nSave it to a file to continue it later.",
                "Stop", lambda: stop(False), title="Stop tournament",
                alt=("Stop and save…", lambda: stop(True)))
            return
        if self._game_started and not self._game_over:
            if self._resume_event.is_set():
                self._resume_event.clear()
                self._play_btn.config(text="▶", fg="#88ff44")
            self._open_stop_confirm()
        else:
            if self._swap_cb:
                self._swap_cb()

    def _open_stop_confirm(self, text="Stop and reset the current game?",
                           yes_text="Reset", on_yes=None, title="Stop game", alt=None):
        """alt: optional (label, callback) for a second confirm button."""
        on_yes = on_yes or self._restart_cb
        tk  = self._tk
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg="#1a1a1a", padx=24, pady=18)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text=text, justify="center",
                 font=self._font(10), fg="#cccccc", bg="#1a1a1a").pack(pady=(4, 14))

        bf = tk.Frame(win, bg="#1a1a1a")
        bf.pack()

        def _yes():
            win.destroy()
            if on_yes:
                on_yes()

        def _no():
            win.destroy()

        if alt:
            def _alt():
                win.destroy()
                alt[1]()
            tk.Button(bf, text=alt[0], font=self._font(9),
                      bg="#2a4a00", fg="#88ff44", relief="flat", padx=12, pady=4,
                      activebackground="#3a6a00", activeforeground="#aaff66",
                      cursor="hand2", command=_alt).pack(side="left", padx=(0, 8))

        tk.Button(bf, text=yes_text, font=self._font(9),
                  bg="#5a1500", fg="#ff5533", relief="flat", padx=12, pady=4,
                  activebackground="#7a2000", activeforeground="#ff7755",
                  cursor="hand2", command=_yes).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=12, pady=4,
                  activebackground="#555555", activeforeground="white",
                  cursor="hand2", command=_no).pack(side="left")

        win.bind("<Escape>", lambda e: _no())
        win.bind("<Return>",  lambda e: _yes())
        self._center_dialog(win)
        win.wait_window()

    def _center_dialog(self, win):
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()  - win.winfo_reqwidth())  // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # ── Piece image loading ──

    def _load_pieces(self):
        """Load piece sprites from pieces.png (2 rows × 6 cols: white top, black bottom)."""
        try:
            from PIL import Image, ImageDraw, ImageTk
        except ImportError:
            return None

        png = os.path.join(RESOURCE_DIR, 'pieces.png')
        if not os.path.exists(png):
            return None

        sheet = Image.open(png).convert('RGBA')
        W, H = sheet.size

        # Remove white background: flood-fill from each corner with a magenta marker,
        # then zero-out the alpha wherever that marker ended up.
        rgb = sheet.convert('RGB')
        MAGIC = (255, 0, 255)
        for corner in [(0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)]:
            ImageDraw.floodfill(rgb, corner, MAGIC, thresh=5)

        try:
            import numpy as np
            arr = np.array(rgb)
            rgba = np.array(sheet)
            bg = (arr[:, :, 0] == 255) & (arr[:, :, 1] == 0) & (arr[:, :, 2] == 255)
            rgba[bg, 3] = 0
            transparent = Image.fromarray(rgba, 'RGBA')
        except ImportError:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                flat = list(rgb.getdata())
            alpha = Image.frombytes('L', (W, H), bytes(0 if p == MAGIC else 255 for p in flat))
            r, g, b, _ = sheet.split()
            transparent = Image.merge('RGBA', (r, g, b, alpha))

        cw, ch = W // 6, H // 2
        order = [chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN]
        src = {}
        for row, is_white in enumerate([True, False]):
            for col, pt in enumerate(order):
                cell = transparent.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
                # Crop to the tight bounding box of non-transparent pixels so that
                # empty cell margins (which vary per piece) don't affect centering.
                bbox = cell.getbbox()
                src[(pt, is_white)] = cell.crop(bbox) if bbox else cell
        return src

    def _pieces_at(self, sq: int):
        """PhotoImages for square size `sq`, scaled from the sprite sheet."""
        if not self._piece_src:
            return None
        if sq in self._piece_cache:
            return self._piece_cache[sq]
        from PIL import Image, ImageTk
        sz = max(8, round(sq * 54 / 68))   # piece size relative to the square
        imgs = {}
        for key, piece in self._piece_src.items():
            p = piece.copy()
            # Scale proportionally to fit within sz×sz, centred on a transparent canvas
            p.thumbnail((sz, sz), Image.LANCZOS)
            canvas = Image.new('RGBA', (sz, sz), (0, 0, 0, 0))
            canvas.paste(p, ((sz - p.width) // 2, (sz - p.height) // 2), p)
            imgs[key] = ImageTk.PhotoImage(canvas)
        # Keep only a few sizes (resizing the window passes through many)
        if len(self._piece_cache) >= 4:
            for k in [k for k in self._piece_cache if k not in (self.SQ, self._sq)][:2]:
                del self._piece_cache[k]
        self._piece_cache[sq] = imgs
        return imgs

    # ── Layout ──

    def _lbl(self, parent, text, font, fg="white", bg=None, anchor="w", **kw):
        return self._tk.Label(parent, text=text, font=font, fg=fg,
                              bg=bg or self.BG_IDLE, anchor=anchor, **kw)

    def _build(self):
        tk = self._tk
        r  = self.root
        SQ = self.SQ
        px = SQ * 8   # board pixel size

        # ── Main content: board column + graph column (+ tournament panel) ──
        # Packed last (below) so that the bars at the bottom keep their height
        # and the content area absorbs any change of window size.
        content = tk.Frame(r, bg=self.BG_IDLE, width=px + self.GW, height=px + 100)
        self._content = content
        content.bind("<Configure>", self._layout)

        left = tk.Frame(content, bg=self.BG_IDLE)
        self._left = left

        right = tk.Frame(content, bg=self.GBG)
        self._right = right

        # Tournament panel: placed to the right of the graph only in tournament mode
        self._build_tournament_panel(content)

        # ── Player panels ──
        def player_panel(parent, name, symbol):
            f  = tk.Frame(parent, bg=self.BG_IDLE, pady=8, padx=10)
            f.pack(fill="x")
            nm = self._lbl(f, f"{symbol} {name}", self._font(13, "bold"))
            nm.pack(side="left")
            ev = self._lbl(f, "eval: ---", self._font(11), fg="#aaaaaa")
            ev.pack(side="right", padx=14)
            tm = self._lbl(f, "--:--.-", self._font(16, "bold"), fg="#55aaff")
            tm.pack(side="right")
            return f, nm, tm, ev

        self._bf, self._bn, self._bt, self._be = player_panel(left, self.black_name, "♟")

        self._canvas = tk.Canvas(left, width=px, height=px, highlightthickness=0,
                                 bg=self.BG_IDLE)
        self._wf, self._wn, self._wt, self._we = player_panel(left, self.white_name, "♙")
        # White's panel is packed before the canvas so it keeps its height
        self._wf.pack_configure(side="bottom")
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>", self._on_board_resize)

        # Make name labels clickable when idle (game over or not yet started)
        for lbl, color in ((self._bn, chess.BLACK), (self._wn, chess.WHITE)):
            lbl.config(cursor="hand2")
            lbl.bind("<Button-1>", lambda e, c=color: self._on_name_click(c))

        # ── Graph panel: metric selector + canvas ──
        self._build_graph_selector(right)
        self._gcv = tk.Canvas(right, width=self.GW, height=px - 30,
                              bg=self.GBG, highlightthickness=0)
        self._gcv.pack(fill="both", expand=True)
        self._gcv.bind("<Configure>", lambda e: self._draw_graph())

        # ── Bottom bars, packed bottom-up: buttons, move list, status ──
        bf = tk.Frame(r, bg="#222222", pady=6, padx=10)
        bf.pack(side="bottom", fill="x")
        mf = tk.Frame(r, bg="#111111", pady=4, padx=10)
        mf.pack(side="bottom", fill="x")
        sf = tk.Frame(r, bg="#1a1a1a", pady=5, padx=10)
        sf.pack(side="bottom", fill="x")
        content.pack(side="top", fill="both", expand=True)

        # ── Status bar ──
        _init_status = ("Click the player names to select engines"
                        if not self._engines_ready else "Initialising...")
        self._status = self._lbl(sf, _init_status, self._font(11),
                                 fg="#cccccc", bg="#1a1a1a")
        self._status.pack(side="left")
        self._last_move_lbl = self._lbl(sf, "", self._font(11, "bold"),
                                        fg="#ffaa00", bg="#1a1a1a")
        self._last_move_lbl.pack(side="right")

        # ── Move list (wraps at the window width) ──
        self._moves_lbl = self._lbl(mf, "", self._font(10), fg="#aaaaaa",
                                    bg="#111111", wraplength=px + self.GW - 20,
                                    justify="left")
        self._moves_lbl.pack(side="left")
        mf.bind("<Configure>", lambda e: self._moves_lbl.config(
            wraplength=max(100, e.width - 20)))

        # ── Button bar ──
        # All buttons fill the bar's height, so text buttons line up with the
        # larger symbol buttons.

        # Play / Pause
        self._play_btn = tk.Button(
            bf, text="▶", font=self._font(14, family="Segoe UI Symbol"),
            fg="#88ff44" if self._engines_ready else "#555555",
            bg="#3a3a3a", activebackground="#555555", activeforeground="#88ff44",
            relief="flat", padx=10, pady=4,
            cursor="hand2" if self._engines_ready else "",
            disabledforeground="#444444",
            state="normal" if self._engines_ready else "disabled",
            command=self._on_play_pause,
        )
        self._play_btn.pack(side="left", fill="y", padx=(0, 4))

        # Stop / Swap-colors
        self._action_btn = tk.Button(
            bf, text="⇆", font=self._font(14, family="Segoe UI Symbol"),
            fg="#aaaaaa" if self._engines_ready else "#555555",
            bg="#3a3a3a", activebackground="#555555", activeforeground="#cccccc",
            relief="flat", padx=10, pady=4,
            cursor="hand2" if self._engines_ready else "",
            disabledforeground="#444444",
            state="normal" if self._engines_ready else "disabled",
            command=self._on_action_btn,
        )
        self._action_btn.pack(side="left", fill="y", padx=(0, 4))

        # Settings (time control)
        self._settings_btn = tk.Button(
            bf, text="⚙", font=self._font(14, family="Segoe UI Symbol"),
            fg="#aaaaaa", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#cccccc", relief="flat", padx=10, pady=4,
            cursor="hand2", command=self._open_settings,
        )
        self._settings_btn.pack(side="left", fill="y", padx=(0, 4))

        # Tournament setup
        self._tourn_btn = tk.Button(
            bf, text="♛ Tournament", font=self._font(10, family="Segoe UI Symbol"),
            fg="#ffcc44", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#ffdd66", disabledforeground="#555555",
            relief="flat", padx=10, pady=4, cursor="hand2",
            command=self._open_tournament_dialog,
        )
        self._tourn_btn.pack(side="left", fill="y", padx=(0, 12))

        # Log buttons — right side (pack Black first so it lands rightmost)
        self._log_btn_b = tk.Button(
            bf, text=f"☰  {self.black_name[:14]}", font=self._font(10, family="Segoe UI Symbol"),
            fg="#ffffff", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#ffffff", relief="flat", padx=10, pady=4,
            cursor="hand2", command=lambda: self._open_log_window(self._black_engine),
        )
        self._log_btn_b.pack(side="right", fill="y", padx=(0, 6))
        self._log_btn_w = tk.Button(
            bf, text=f"☰  {self.white_name[:14]}", font=self._font(10, family="Segoe UI Symbol"),
            fg="#ffffff", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#ffffff", relief="flat", padx=10, pady=4,
            cursor="hand2", command=lambda: self._open_log_window(self._white_engine),
        )
        self._log_btn_w.pack(side="right", fill="y", padx=(0, 6))


        self._draw_board(chess.Board())
        self._draw_graph({})

    # ── Board drawing ──

    def _draw_board(self, board: chess.Board, last_move: Optional[chess.Move] = None):
        self._cur_board, self._cur_last = board.copy(stack=False), last_move
        c  = self._canvas
        SQ = self._sq
        X0, Y0 = self._bx, self._by
        c.delete("all")
        hi = {last_move.from_square, last_move.to_square} if last_move else set()

        for rank in range(8):
            for file in range(8):
                sq    = chess.square(file, rank)
                x1    = X0 + file * SQ
                y1    = Y0 + (7 - rank) * SQ
                light = (file + rank) % 2 == 1
                col   = self.HI if sq in hi else (self.LIGHT if light else self.DARK)
                c.create_rectangle(x1, y1, x1 + SQ, y1 + SQ, fill=col, outline="")

                piece = board.piece_at(sq)
                if piece:
                    cx, cy = x1 + SQ // 2, y1 + SQ // 2
                    img = self._piece_imgs and self._piece_imgs.get((piece.piece_type, piece.color))
                    if img:
                        c.create_image(cx, cy, image=img, anchor='center')
                    else:
                        self._draw_piece(c, cx, cy, SQ, piece)

        self._draw_coords(c, SQ, X0, Y0)

    # ── Move animation ──

    def animate_move(self, board: chess.Board, move: chess.Move,
                     stop_event: Optional[threading.Event] = None):
        """Called from the game thread. Blocks (~0.2 s) while the GUI slides the piece."""
        self._anim_done.clear()
        self.update(animate_move=(board.copy(), move))
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            if stop_event and stop_event.is_set():
                return
            if self._anim_done.wait(timeout=0.025):
                return

    def _start_animation(self, board_before: chess.Board, move: chess.Move):
        self._anim_board = board_before
        self._anim_move  = move
        self._anim_start = time.monotonic()
        self._animating  = True
        self._anim_step()

    def _anim_step(self):
        t = min(1.0, (time.monotonic() - self._anim_start) / 0.2)
        ease = t * t * (3 - 2 * t)   # smoothstep
        self._draw_board_anim(self._anim_board, self._anim_move, ease)
        if t < 1.0:
            self.root.after(16, self._anim_step)
        else:
            self._animating = False
            self._anim_done.set()

    def _draw_coords(self, c, SQ: int, X0: int, Y0: int):
        for i in range(8):
            c.create_text(X0 + 3, Y0 + (7 - i) * SQ + SQ // 2, text=str(i + 1),
                          font=self._font(8), fill="#666", anchor="w")
            c.create_text(X0 + i * SQ + SQ // 2, Y0 + 8 * SQ - 2, text="abcdefgh"[i],
                          font=self._font(8), fill="#666", anchor="s")

    def _draw_board_anim(self, board: chess.Board, move: chess.Move, t: float):
        """Board with the moving piece interpolated at progress t (0→from, 1→to)."""
        c  = self._canvas
        SQ = self._sq
        X0, Y0 = self._bx, self._by
        c.delete("all")
        hi     = {move.from_square, move.to_square}
        moving = board.piece_at(move.from_square)

        for rank in range(8):
            for file in range(8):
                sq    = chess.square(file, rank)
                x1    = X0 + file * SQ
                y1    = Y0 + (7 - rank) * SQ
                light = (file + rank) % 2 == 1
                col   = self.HI if sq in hi else (self.LIGHT if light else self.DARK)
                c.create_rectangle(x1, y1, x1 + SQ, y1 + SQ, fill=col, outline="")
                piece = board.piece_at(sq)
                if piece and sq != move.from_square:
                    cx2, cy2 = x1 + SQ // 2, y1 + SQ // 2
                    img = self._piece_imgs and self._piece_imgs.get((piece.piece_type, piece.color))
                    if img:
                        c.create_image(cx2, cy2, image=img, anchor='center')
                    else:
                        self._draw_piece(c, cx2, cy2, SQ, piece)

        self._draw_coords(c, SQ, X0, Y0)

        if moving:
            ff = chess.square_file(move.from_square)
            fr = chess.square_rank(move.from_square)
            tf = chess.square_file(move.to_square)
            tr = chess.square_rank(move.to_square)
            px = X0 + (ff + 0.5) * SQ + t * ((tf - ff) * SQ)
            py = Y0 + (7 - fr + 0.5) * SQ + t * ((fr - tr) * SQ)
            img = self._piece_imgs and self._piece_imgs.get((moving.piece_type, moving.color))
            if img:
                c.create_image(int(px), int(py), image=img, anchor='center')
            else:
                self._draw_piece(c, int(px), int(py), SQ, moving)

    def _draw_piece(self, c, cx, cy, SQ, piece):
        """Draw a chess piece shape using canvas primitives, centred at (cx, cy)."""
        w  = piece.color == chess.WHITE
        fg = "#ededed" if w else "#1a1a1a"
        ol = "#333333" if w else "#888888"
        u  = SQ / 80.0       # 1 drawing-unit in pixels

        def O(x, y, rx, ry, outline=ol):
            c.create_oval(cx+x*u-rx*u, cy+y*u-ry*u,
                          cx+x*u+rx*u, cy+y*u+ry*u,
                          fill=fg, outline=outline, width=1)

        def R(x1, y1, x2, y2, border=True):
            c.create_rectangle(cx+x1*u, cy+y1*u, cx+x2*u, cy+y2*u,
                               fill=fg, outline=ol if border else fg, width=1)

        def P(*pts, smooth=False):
            scaled = [v for x, y in pts for v in (cx+x*u, cy+y*u)]
            c.create_polygon(scaled, fill=fg, outline=ol, width=1, smooth=smooth)

        pt = piece.piece_type

        if pt == chess.PAWN:
            O(0, 13, 12, 5)                  # base
            R(-3, -8, 3, 14, border=False)   # neck gap fill
            O(0,  1,  8, 5)                  # collar
            O(0, -14,  7, 7)                 # head

        elif pt == chess.ROOK:
            O(0, 13, 13, 5)                  # base
            R(-10, -18, 10, 12)              # body
            R(-10, -17, 10, 12, border=False) # seal body-to-base gap
            for bx in (-10, -3, 4):          # three merlons
                R(bx, -27, bx+6, -16)
            R(-10, -17, 10, -16, border=False) # fill gap between merlons and body

        elif pt == chess.KNIGHT:
            O(0, 13, 13, 5)   # base
            # Horse-head silhouette polygon
            P((-8, 13), (8, 13), (10, 4), (10, -3), (5, -11),
              (13, -19), (8, -28), (2, -26), (-1, -30),
              (-6, -23), (-2, -17), (-10, -6))
            O(0, 13, 13, 4, outline=fg)      # hide join at base

        elif pt == chess.BISHOP:
            O(0, 13, 13, 5)                  # base
            R(-3,  -8,  3, 14, border=False) # stem fill
            O(0,   1,   7,  9)               # body bulge
            P((-5, -3), (5, -3), (2, -22), (-2, -22))  # pointed head
            O(0, -23,   4,  4)               # top ball

        elif pt == chess.QUEEN:
            O(0, 13, 14, 5)                  # base
            R(-8, -10, 8, 14, border=False)  # body fill
            O(0,  0,   9, 11)                # body
            P((-9, -10), (9, -10), (7, -22), (-7, -22))  # crown band
            for bx in (-8, -4, 0, 4, 8):    # five crown balls
                O(bx, -24, 3, 3)

        elif pt == chess.KING:
            O(0, 13, 14, 5)                  # base
            R(-8, -10, 8, 14, border=False)  # body fill
            O(0,  0,   9, 11)                # body
            P((-9, -10), (9, -10), (7, -20), (-7, -20))  # crown band
            R(-2, -30,  2, -18)              # cross vertical
            R(-7, -27,  7, -23)              # cross horizontal

    # ── Graph panel (eval / depth / nodes) ──

    GRAPH_TITLES = {"eval": "Evaluation", "depth": "Search depth",
                    "nodes": "Nodes searched (log)"}

    def _build_graph_selector(self, parent):
        tk  = self._tk
        bar = tk.Frame(parent, bg=self.GBG, pady=5)
        bar.pack(fill="x")
        tk.Label(bar, text="Show:", font=self._font(9), fg="#777777",
                 bg=self.GBG).pack(side="left", padx=(8, 4))
        self._graph_btns = {}
        for m, label in (("eval", "Eval"), ("depth", "Depth"), ("nodes", "Nodes")):
            b = tk.Label(bar, text=label, font=self._font(9, "bold"),
                         padx=8, pady=2, cursor="hand2")
            b.pack(side="left", padx=2)
            b.bind("<Button-1>", lambda e, m=m: self._toggle_graph_metric(m))
            self._graph_btns[m] = b
        self._style_graph_btns()

    def _style_graph_btns(self):
        for m, b in self._graph_btns.items():
            on = m in self._graph_sel
            b.config(bg="#3a3a3a" if on else self.GBG,
                     fg="#ffffff" if on else "#666666")

    def _toggle_graph_metric(self, metric: str):
        if metric in self._graph_sel:
            if len(self._graph_sel) == 1:
                return   # keep at least one graph
            self._graph_sel.discard(metric)
        else:
            self._graph_sel.add(metric)
        self._style_graph_btns()
        _save_gui_settings({"graph_metrics": [m for m in GRAPH_METRICS
                                              if m in self._graph_sel]})
        self._draw_graph()

    def _draw_graph(self, hist: Optional[dict] = None):
        """
        hist: metric -> (white_values, black_values), parallel per-ply lists
        with None on the other side's plies and where the engine reported
        nothing. Evals are normalised to White's POV. hist=None redraws the
        last data (after toggling metrics or a resize).
        The selected metrics are stacked top to bottom with a shared legend.
        """
        if hist is not None:
            self._graph_hist = hist
        c = self._gcv
        W = c.winfo_width()
        H = c.winfo_height()
        if H < 50 or W < 50:   # not mapped yet
            W, H = int(c.cget("width")), int(c.cget("height"))
        self._gw_now = W
        k = self._scale
        c.delete("all")
        c.create_rectangle(0, 0, W, H, fill=self.GBG, outline="")

        metrics = [m for m in GRAPH_METRICS if m in self._graph_sel] or ["eval"]
        LEG = round(50 * k)
        GAP = round(12 * k)   # space between the graph cards
        sec = (H - LEG - GAP * (len(metrics) - 1)) / len(metrics)
        for j, m in enumerate(metrics):
            w_vals, b_vals = self._graph_hist.get(m, ([], []))
            self._plot_metric(m, j * (sec + GAP), sec, w_vals, b_vals)

        # Legend — Black on top, White below (mirrors board orientation)
        ML = round(44 * k)
        for y_off, color, label in [
            (H - LEG + 16 * k, self.C_B, f"B: {self.black_name[:24]}"),
            (H - LEG + 36 * k, self.C_W, f"W: {self.white_name[:24]}"),
        ]:
            c.create_line(ML, y_off, ML + 16, y_off, fill=color, width=2)
            c.create_text(ML + 20, y_off, text=label, fill=color,
                          font=self._font(10), anchor="w")

    def _plot_metric(self, metric: str, top: float, height: float,
                     w_vals: list, b_vals: list):
        """
        eval:  y inverted (top = Black ahead), auto-scaled, at least ±1, capped ±10
        depth: linear from 0
        nodes: log10 scale
        """
        import math
        c = self._gcv
        k = self._scale
        INSET = 6   # card edge to window edge
        ML, MR, MT, MB = round(44 * k), 16, round(26 * k), 10
        pw = max(10, self._gw_now - ML - MR)
        y0 = top + MT
        ph = max(10.0, height - MT - MB)
        MIN_GAP = 16 * k   # min px between labelled gridlines

        # Each graph is a card of its own: lighter background, border, title bar
        c.create_rectangle(INSET, top, self._gw_now - INSET, top + height,
                           fill=self.GCARD, outline="#3a3a3a")
        c.create_text(INSET + 8, top + 12 * k, text=self.GRAPH_TITLES[metric],
                      fill="#bbbbbb", font=self._font(9, "bold"), anchor="w")

        vals = [v for v in list(w_vals) + list(b_vals) if v is not None]
        n = max(len(w_vals), len(b_vals), 1)

        def gx(i):
            return ML + (pw * i / (n - 1) if n > 1 else pw / 2)

        zero = None
        if metric == "eval":
            if vals:
                y_max = min(10.0, max(1.0, max(vals)))
                y_min = max(-10.0, min(-1.0, min(vals)))
            else:
                y_min, y_max = -1.0, 1.0

            def gy(v):
                v = max(y_min, min(y_max, float(v)))
                return y0 + ph * (v - y_min) / (y_max - y_min)

            step = next((s for s in (1, 2, 5) if ph * s / (y_max - y_min) >= MIN_GAP), 10)
            ticks = [(v, "0" if v == 0 else f"+{v}" if v > 0 else f"−{abs(v)}")
                     for v in range(-10, 11) if v % step == 0 and y_min <= v <= y_max]
            zero = 0
        elif metric == "depth":
            vals = [v for v in vals if v >= 0]
            top_v = max(vals) if vals else 10
            step = next((s for s in (1, 2, 5, 10, 20, 50)
                         if ph * s / max(top_v, 1) >= MIN_GAP), 100)
            y_max = max(step, -(-top_v // step) * step)   # round up to a step

            def gy(v):
                return y0 + ph * (1 - max(0, min(y_max, v)) / y_max)

            ticks = [(v, str(v)) for v in range(0, int(y_max) + 1, step)]
        else:   # nodes
            vals = [v for v in vals if v > 0]
            if vals:
                lo = math.floor(math.log10(min(vals)))
                hi = math.ceil(math.log10(max(vals)))
            else:
                lo, hi = 3, 6
            if hi <= lo:
                hi = lo + 1

            def gy(v):
                if v <= 0:
                    return y0 + ph
                return y0 + ph * (1 - (math.log10(v) - lo) / (hi - lo))

            every = next((k for k in range(1, 10) if ph * k / (hi - lo) >= MIN_GAP), 10)
            ticks = [(10 ** e, fmt_count(10 ** e)) for e in range(lo, hi + 1)
                     if (e - lo) % every == 0]

        for val, label in ticks:
            y = gy(val)
            is_zero = val == zero
            c.create_line(ML, y, ML + pw, y, fill="#888" if is_zero else "#666",
                          width=2 if is_zero else 1, dash=() if is_zero else (2, 3))
            c.create_text(ML - 4, y, text=label, fill="#999",
                          font=self._font(8), anchor="e")

        c.create_rectangle(ML, y0, ML + pw, y0 + ph, outline="#4a4a4a", width=1)

        if not vals and n >= 2:
            # Moves have been played, but the engines never reported this value
            c.create_text(ML + pw / 2, y0 + ph / 2, text="no data from engines",
                          fill="#666", font=self._font(9))

        for series, color in ((w_vals, self.C_W), (b_vals, self.C_B)):
            pts = [(i, v) for i, v in enumerate(series)
                   if v is not None and (metric != "nodes" or v > 0)]
            if len(pts) >= 2:
                coords = []
                for i, v in pts:
                    coords.extend([gx(i), gy(v)])
                c.create_line(*coords, fill=color, width=2, smooth=False)
            elif len(pts) == 1:
                x, y = gx(pts[0][0]), gy(pts[0][1])
                c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")

    # ── Engine picker ──

    def _update_engine_name(self, color: chess.Color, name: str):
        if color == chess.WHITE:
            self.white_name = name
            self._wn.config(text=f"♙ {name}")
            self._log_btn_w.config(text=f"☰  {name[:14]}")
        else:
            self.black_name = name
            self._bn.config(text=f"♟ {name}")
            self._log_btn_b.config(text=f"☰  {name[:14]}")

    def _on_name_click(self, color: chess.Color):
        if self._tournament_running:
            return
        if not (self._game_over or not self._game_started):
            return
        self._open_engine_picker(color)

    def _open_engine_picker(self, color: chess.Color):
        tk   = self._tk
        side = "White" if color == chess.WHITE else "Black"

        # engines/ folder first, then history extras
        items = _engine_candidates()   # (display_name, abs_path, source)

        # Dialog
        BG  = "#1a1a1a"
        FG  = "#cccccc"
        win = tk.Toplevel(self.root)
        win.title(f"Select Engine — {side}")
        win.configure(bg=BG)
        win.geometry(f"{round(380 * self._scale)}x{round(300 * self._scale)}")
        win.resizable(True, True)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text=f"Engine for {side}:", font=self._font(10, "bold"),
                 fg="#aaaaaa", bg=BG, padx=12).pack(anchor="w", pady=(8, 4))

        lf = tk.Frame(win, bg=BG, padx=12)
        lf.pack(fill="both", expand=True)
        vsb = tk.Scrollbar(lf)
        vsb.pack(side="right", fill="y")
        lb = tk.Listbox(
            lf, font=self._font(10), bg="#111111", fg=FG,
            selectbackground="#3a5a00", selectforeground="white",
            relief="flat", activestyle="none", yscrollcommand=vsb.set,
        )
        lb.pack(fill="both", expand=True)
        vsb.config(command=lb.yview)

        for display, _, _ in items:
            lb.insert("end", f"  {display}")
        if items:
            lb.selection_set(0)
            lb.activate(0)

        # Separator
        tk.Frame(win, bg="#333333", height=1).pack(fill="x", padx=12, pady=(6, 0))

        # Buttons
        bf = tk.Frame(win, bg=BG, pady=8, padx=12)
        bf.pack(fill="x")

        result: list[str | None] = [None]

        def _select():
            sel = lb.curselection()
            if sel:
                result[0] = items[sel[0]][1]
            win.destroy()

        def _browse():
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title=f"Select engine executable for {side}",
                filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
                parent=win,
            )
            if path:
                result[0] = path
                win.destroy()

        def _mk(parent, text, fg, cmd, **kw):
            bg  = kw.pop("bg",              "#3a3a3a")
            abg = kw.pop("activebackground", "#555555")
            return tk.Button(parent, text=text, font=self._font(9),
                             bg=bg, fg=fg, relief="flat", padx=10, pady=3,
                             activebackground=abg, activeforeground="white",
                             cursor="hand2", command=cmd, **kw)

        _mk(bf, "Browse...", "#aaaaaa", _browse).pack(side="left")
        _mk(bf, "Cancel",    "#888888", win.destroy).pack(side="right")
        _mk(bf, "Select",    "#88ff44", _select,
            bg="#2d4a00", activebackground="#3a6000").pack(side="right", padx=(0, 8))

        lb.bind("<Double-Button-1>", lambda e: _select())
        lb.bind("<Return>",          lambda e: _select())
        win.bind("<Escape>",         lambda e: win.destroy())

        win.wait_window()

        if result[0] and self._change_engine_cb:
            self._change_engine_cb(color, result[0])

    # ── Log window ──

    def _open_log_window(self, engine):
        if engine is None:
            return
        tk = self._tk

        win = tk.Toplevel(self.root)
        win.title(f"Engine Log — {engine.name}")
        win.configure(bg="#111111")
        has_options = bool(engine.options)
        k = self._scale
        win.geometry(f"{round((960 if has_options else 740) * k)}x{round(480 * k)}")

        # Toolbar
        tb = tk.Frame(win, bg="#1a1a1a", pady=4, padx=8)
        tb.pack(fill="x")
        tk.Label(tb, text=engine.name, font=self._font(11, "bold"),
                 fg="#dddddd", bg="#1a1a1a").pack(side="left")
        tk.Button(tb, text="Clear", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=8,
                  activebackground="#555", activeforeground="white",
                  cursor="hand2",
                  command=lambda: _clear()).pack(side="right")

        # Main area: log on left, options panel on right
        content = tk.Frame(win, bg="#111111")
        content.pack(fill="both", expand=True)

        # Log frame
        log_frame = tk.Frame(content, bg="#111111")
        log_frame.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(log_frame)
        sb.pack(side="right", fill="y")
        txt = tk.Text(
            log_frame, bg="#111111", fg="#cccccc",
            font=self._font(9), wrap="word",
            yscrollcommand=sb.set,
            state="disabled", relief="flat",
        )
        txt.pack(fill="both", expand=True)

        txt.tag_config("sent", foreground="#ffcc44")
        txt.tag_config("recv", foreground="#cccccc")
        txt.tag_config("info", foreground="#888888")
        txt.tag_config("best", foreground="#88ff88")

        shown      = [0]
        autoscroll = [True]

        def _on_view_change(first, last):
            sb.set(first, last)
            autoscroll[0] = float(last) >= 0.999

        txt.config(yscrollcommand=_on_view_change)
        sb.config(command=txt.yview)

        def _append(direction, line):
            txt.config(state="normal")
            if direction == ">":
                tag, prefix = "sent", "  > "
            elif line.startswith("bestmove"):
                tag, prefix = "best", "  < "
            elif line.startswith("info"):
                tag, prefix = "info", "  < "
            else:
                tag, prefix = "recv", "  < "
            txt.insert("end", prefix + line + "\n", tag)
            txt.config(state="disabled")

        def _clear():
            shown[0] = len(engine.log)
            txt.config(state="normal")
            txt.delete("1.0", "end")
            txt.config(state="disabled")

        # Options panel
        if has_options:
            OBG = "#1a1a1a"
            OFG = "#cccccc"
            ODG = "#888888"

            tk.Frame(content, bg="#333333", width=1).pack(side="left", fill="y")

            panel = tk.Frame(content, bg=OBG, width=round(212 * k))
            panel.pack(side="left", fill="y")
            panel.pack_propagate(False)

            tk.Label(panel, text="Engine Options", font=self._font(10, "bold"),
                     fg="#aaaaaa", bg=OBG, anchor="w", padx=10, pady=6).pack(fill="x")
            tk.Frame(panel, bg="#2a2a2a", height=1).pack(fill="x")

            # Scrollable area
            canvas = tk.Canvas(panel, bg=OBG, highlightthickness=0, bd=0)
            vsb = tk.Scrollbar(panel, orient="vertical", command=canvas.yview)
            sf = tk.Frame(canvas, bg=OBG)
            sf.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=sf, anchor="nw")
            canvas.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)

            def _bind_mousewheel(widget):
                widget.bind("<MouseWheel>",
                            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

            _bind_mousewheel(canvas)
            _bind_mousewheel(sf)

            for opt_name, opt in engine.options.items():
                row = tk.Frame(sf, bg=OBG, pady=4, padx=10)
                row.pack(fill="x")
                _bind_mousewheel(row)

                opt_type = opt["type"]

                if opt_type == "button":
                    btn = tk.Button(
                        row, text=opt_name, font=self._font(9),
                        bg="#3a3a3a", fg=OFG, relief="flat", padx=6,
                        activebackground="#555", activeforeground="white",
                        cursor="hand2",
                        command=(lambda n=opt_name: engine._send(f"setoption name {n}")),
                    )
                    btn.pack(anchor="w")
                    _bind_mousewheel(btn)

                elif opt_type == "check":
                    var = tk.BooleanVar(value=(opt["value"] or "").lower() == "true")

                    def _on_check(n=opt_name, v=var):
                        val = "true" if v.get() else "false"
                        engine.options[n]["value"] = val
                        engine.set_option(n, val)

                    cb = tk.Checkbutton(
                        row, text=opt_name, variable=var,
                        font=self._font(9), fg=OFG, bg=OBG,
                        selectcolor="#333333", activebackground=OBG,
                        activeforeground=OFG, relief="flat",
                        cursor="hand2", command=_on_check,
                    )
                    cb.pack(anchor="w")
                    _bind_mousewheel(cb)

                elif opt_type == "spin":
                    tk.Label(row, text=opt_name, font=self._font(9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    spin_var = tk.StringVar(value=opt["value"] or "")
                    from_val = int(opt["min"]) if opt["min"] is not None else 0
                    to_val   = int(opt["max"]) if opt["max"] is not None else 9999
                    spin = tk.Spinbox(
                        row, textvariable=spin_var,
                        from_=from_val, to=to_val,
                        font=self._font(9), width=8,
                        bg="#2a2a2a", fg=OFG, relief="flat",
                        buttonbackground="#3a3a3a",
                        insertbackground=OFG,
                    )
                    spin.pack(anchor="w")
                    _bind_mousewheel(spin)

                    def _on_spin(event=None, n=opt_name, v=spin_var):
                        engine.options[n]["value"] = v.get()
                        engine.set_option(n, v.get())

                    spin.bind("<FocusOut>", _on_spin)
                    spin.bind("<Return>",   _on_spin)

                elif opt_type == "combo":
                    tk.Label(row, text=opt_name, font=self._font(9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    combo_var = tk.StringVar(value=opt["value"] or "")
                    choices = opt["var"] or [opt["value"]]

                    def _on_combo(val, n=opt_name):
                        engine.options[n]["value"] = val
                        engine.set_option(n, val)

                    om = tk.OptionMenu(row, combo_var, *choices, command=_on_combo)
                    om.config(font=self._font(9), bg="#2a2a2a", fg=OFG,
                              activebackground="#3a3a3a", activeforeground=OFG,
                              relief="flat", padx=4, cursor="hand2")
                    om["menu"].config(bg="#2a2a2a", fg=OFG, activebackground="#444",
                                      activeforeground=OFG, font=self._font(9))
                    om.pack(anchor="w")
                    _bind_mousewheel(om)

                else:  # string
                    tk.Label(row, text=opt_name, font=self._font(9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    str_var = tk.StringVar(value=opt["value"] or "")
                    entry = tk.Entry(
                        row, textvariable=str_var,
                        font=self._font(9), width=20,
                        bg="#2a2a2a", fg=OFG, relief="flat",
                        insertbackground=OFG,
                    )
                    entry.pack(anchor="w", fill="x")
                    _bind_mousewheel(entry)

                    def _on_entry(event=None, n=opt_name, v=str_var):
                        engine.options[n]["value"] = v.get()
                        engine.set_option(n, v.get())

                    entry.bind("<FocusOut>", _on_entry)
                    entry.bind("<Return>",   _on_entry)

                # Subtle separator between options
                tk.Frame(sf, bg="#252525", height=1).pack(fill="x", padx=10)

        def _poll():
            if not win.winfo_exists():
                return
            log = engine.log
            while shown[0] < len(log):
                _append(*log[shown[0]])
                shown[0] += 1
            if autoscroll[0]:
                txt.see("end")
            win.after(100, _poll)

        _poll()

    # ── Tournament panel (right of the graph) ──

    TW   = 380         # tournament panel width (px)
    T_BG = "#161616"

    def _build_tournament_panel(self, parent):
        tk, BG = self._tk, self.T_BG
        p = tk.Frame(parent, bg=BG, width=self.TW)
        p.pack_propagate(False)   # size is set by _layout()
        self._tpanel = p
        self._tpanel_shown = False

        hdr = tk.Frame(p, bg=BG)
        hdr.pack(fill="x", padx=10, pady=(8, 2))
        self._t_title = tk.Label(hdr, text="Tournament", font=self._font(11, "bold"),
                                 fg="white", bg=BG, anchor="w")
        self._t_title.pack(side="left")
        self._t_close = tk.Label(hdr, text="✕", font=self._font(10, family="Segoe UI Symbol"),
                                 fg="#444444", bg=BG)
        self._t_close.pack(side="right")
        self._t_close.bind("<Button-1>", lambda e: self._close_tournament_panel())

        self._t_info = tk.Label(p, text="", font=self._font(9), fg="#aaaaaa",
                                bg=BG, anchor="w", justify="left")
        self._t_info.pack(fill="x", padx=10)

        TXT = dict(font=self._font(9), bg="#111111", fg="#cccccc", relief="flat",
                   wrap="none", padx=6, pady=4, cursor="arrow", highlightthickness=0,
                   state="disabled")
        # The table's box is sized in pixels after each render: lines with a
        # pawn glyph are taller than a Text widget's line-based height allows
        self._t_table_box = tk.Frame(p, bg="#111111", height=60)
        self._t_table_box.pack_propagate(False)
        self._t_table_box.pack(fill="x", padx=10, pady=(6, 0))
        self._t_table = tk.Text(self._t_table_box, height=3, **TXT)
        self._t_table.pack(fill="both", expand=True)

        tk.Label(p, text="Recent results", font=self._font(9, "bold"), fg="#777777",
                 bg=BG, anchor="w").pack(fill="x", padx=10, pady=(10, 2))
        rf = tk.Frame(p, bg=BG)
        rf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        sb = tk.Scrollbar(rf)
        self._t_results = tk.Text(rf, **TXT)
        self._t_results.pack(side="left", fill="both", expand=True)
        sb.config(command=self._t_results.yview)

        # Show the scrollbar only when the results don't fit
        sb_shown = [False]

        def _yscroll(first, last):
            sb.set(first, last)
            need = float(first) > 0.0 or float(last) < 1.0
            if need != sb_shown[0]:
                sb_shown[0] = need
                if need:
                    sb.pack(side="right", fill="y", before=self._t_results)
                else:
                    sb.pack_forget()

        self._t_results.config(yscrollcommand=_yscroll)

        for t in (self._t_table, self._t_results):
            t.tag_config("hdr",     foreground="#777777")
            t.tag_config("playing", foreground="#88ff44")
            t.tag_config("leader",  foreground="#ffcc44")
            t.tag_config("dim",     foreground="#666666")
            t.tag_config("win",     foreground="#ffffff")
            t.tag_config("draw",    foreground="#aaaaaa")
            t.tag_config("side_w",  foreground="#ffffff",
                         font=self._font(12, family="Segoe UI Symbol"))
            t.tag_config("side_b",  foreground="#ffffff",
                         font=self._font(12, family="Segoe UI Symbol"))

    def _show_tournament_panel(self):
        if not self._tpanel_shown:
            self._tpanel_shown = True
            self._update_min_size()
            self._layout()

    def _close_tournament_panel(self):
        if self._tournament_running or not self._tpanel_shown:
            return
        self._tpanel_shown = False
        self._last_tournament_snap = None
        self._update_min_size()
        self._layout()

    def _pawn_marker(self, white: bool):
        """Small pawn image for the standings, sized to the text: solid white
        for White, black with a white outline for Black (so both read clearly
        on the dark background). None without PIL / pieces.png."""
        if not self._piece_src:
            return None
        h = max(8, self._font(9).metrics("linespace") - 1)
        key = (white, h)
        cache = self.__dict__.setdefault("_pawn_marks", {})
        if key in cache:
            return cache[key]
        from PIL import Image, ImageFilter, ImageTk
        shape = self._piece_src[(chess.PAWN, True)].getchannel("A")
        inner = h - 2   # leave 1 px on each side for the outline
        w = max(4, round(shape.width * inner / shape.height))
        mask = shape.resize((w, inner), Image.LANCZOS)
        canvas_mask = Image.new("L", (w + 2, h), 0)
        canvas_mask.paste(mask, (1, 1))
        if white:
            img = Image.new("RGBA", canvas_mask.size, (255, 255, 255, 255))
            img.putalpha(canvas_mask)
        else:
            outline = canvas_mask.filter(ImageFilter.MaxFilter(3))
            img = Image.new("RGBA", canvas_mask.size, (255, 255, 255, 255))
            img.putalpha(outline)
            body = Image.new("RGBA", canvas_mask.size, (0, 0, 0, 255))
            body.putalpha(canvas_mask)
            img = Image.alpha_composite(img, body)
        cache[key] = ImageTk.PhotoImage(img)
        return cache[key]

    # ── Tooltips for engine names shortened to fit a column ──

    def _insert_name(self, t, name: str, width: int, tags=()):
        """Insert `name` padded to `width` chars. A longer name is cut off with
        '…' and shows the full name in a tooltip on hover."""
        tags = tuple(tags) if isinstance(tags, (tuple, list)) else ((tags,) if tags else ())
        if len(name) <= width:
            t.insert("end", f"{name:<{width}}", tags)
            return
        self._tip_seq = getattr(self, "_tip_seq", 0) + 1
        tip = f"nametip{self._tip_seq}"
        t.tag_bind(tip, "<Enter>", lambda e, n=name: self._show_tip(e, n))
        t.tag_bind(tip, "<Motion>", lambda e: self._move_tip(e))
        t.tag_bind(tip, "<Leave>", lambda e: self._hide_tip())
        t.insert("end", name[:width - 1] + "…", tags + (tip,))

    def _clear_name_tips(self, t):
        self._hide_tip()
        for tag in t.tag_names():
            if tag.startswith("nametip"):
                t.tag_delete(tag)

    def _show_tip(self, event, text: str):
        self._hide_tip()
        tk = self._tk
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, font=self._font(9), fg="#eeeeee", bg="#333333",
                 padx=6, pady=2, relief="solid", bd=1).pack()
        self._tip = tip
        self._move_tip(event)

    def _move_tip(self, event):
        tip = getattr(self, "_tip", None)
        if tip is not None:
            tip.geometry(f"+{event.x_root + 12}+{event.y_root + 16}")

    def _hide_tip(self):
        tip = getattr(self, "_tip", None)
        if tip is not None:
            self._tip = None
            try:
                tip.destroy()
            except Exception:
                pass

    def _fit_table_height(self, lines: int):
        t = self._t_table
        pad = 2 * int(t.cget("pady")) + 4
        px = None
        try:
            t.update_idletasks()
            n = t.count("1.0", "end", "ypixels")
            px = n[0] if isinstance(n, tuple) else n
        except Exception:
            pass
        if not px:
            px = lines * self._font(9).metrics("linespace") * 1.4
        cap = 15 * self._font(9).metrics("linespace") * 1.4   # scroll beyond ~14 rows
        self._t_table_box.config(height=int(min(px, cap)) + pad)

    def _render_tournament(self, snap: dict):
        """Draw a Tournament.snapshot() in the panel."""
        self._show_tournament_panel()
        self._last_tournament_snap = snap
        status, rows = snap["status"], snap["rows"]
        self._t_title.config(text=snap["title"])

        lines = [f"{snap['progress']}   ·   {snap['tc']}"]
        color = "#aaaaaa"
        if status == "starting":
            lines.append("Starting engines…")
        elif status == "running" and snap["current"]:
            lines.append(f"Now: ♙ {snap['current'][0]} – ♟ {snap['current'][1]}")
        elif status == "finished":
            winners = [r["name"] for r in rows if r["rank"] == 1]
            lines.append(("Winner: " if len(winners) == 1 else "Shared 1st: ")
                         + ", ".join(winners))
            color = "#ffcc44"
        elif status == "stopped":
            lines.append("Stopped")
            color = "#ff7755"
        self._t_info.config(text="\n".join(lines), fg=color)

        # Standings
        # Engines in the current game are green, with a pawn of their color
        # in the second column: ♙ = White, ♟ = Black
        playing = {}
        if snap["current"]:
            playing = {snap["current"][0]: "W", snap["current"][1]: "B"}
        t = self._t_table
        t.config(state="normal")
        t.delete("1.0", "end")
        self._clear_name_tips(t)
        # The pawn marker is an image (or a glyph without PIL), not a Consolas
        # character, so a tab stop after it keeps the columns aligned
        f_txt = self._font(9)
        marks = {s: self._pawn_marker(s == "W") for s in ("W", "B")}
        if marks["W"]:
            mark_w = marks["W"].width()
        else:
            mark_w = self._font(12, family="Segoe UI Symbol").measure("♟")
        tab = f_txt.measure("99 ") + mark_w + f_txt.measure(" ")
        t.config(tabs=(tab,))
        t.insert("end", f"{'#':>2}\t{'Engine':<17} {'G':>2} {'W':>2} {'D':>2} "
                        f"{'L':>2} {'Pts':>5} {'SB':>6}", "hdr")
        for r in rows:
            side = playing.get(r["name"])
            if side:
                tag = "playing"
            elif status == "finished" and r["rank"] == 1:
                tag = "leader"
            else:
                tag = ()
            t.insert("end", f"\n{r['rank']:>2} ", tag)
            if side and marks[side]:
                t.image_create("end", image=marks[side], align="center")
            elif side:
                t.insert("end", "♙" if side == "W" else "♟",
                         "side_w" if side == "W" else "side_b")
            t.insert("end", "\t", tag)
            self._insert_name(t, r["name"], 17, tag)
            t.insert("end", f" {r['games']:>2} {r['wins']:>2} "
                            f"{r['draws']:>2} {r['losses']:>2} {r['points']:>5g} "
                            f"{r['sb']:>6.2f}", tag)
        t.config(state="disabled")
        self._fit_table_height(len(rows) + 1)

        # Results, newest first
        t = self._t_results
        t.config(state="normal")
        t.delete("1.0", "end")
        self._clear_name_tips(t)
        if not snap["results"]:
            t.insert("end", "No games finished yet", "dim")
        for k, e in enumerate(snap["results"]):
            if k:
                t.insert("end", "\n")
            t.insert("end", f"R{e['round']:<3}", "dim")
            if e["type"] == "bye":
                self._insert_name(t, e["name"], 14)
                t.insert("end", " ")
                t.insert("end", "bye (+1)", "dim")
                continue
            res = e["result"].replace("1/2", "½")
            self._insert_name(t, e["white"], 14)
            t.insert("end", " ")
            t.insert("end", f"{res:^5}", "draw" if "½" in res else "win")
            t.insert("end", " ")
            self._insert_name(t, e["black"], 14)
            t.insert("end", " ")
            t.insert("end", e["reason"], "dim")
        t.config(state="disabled")
        t.yview_moveto(0.0)

    # ── Tournament mode ──

    def enter_tournament_mode(self, paused: bool = False):
        """Switch buttons to tournament behaviour and start running (or wait
        for ▶ if `paused`). Main thread."""
        self._tournament_running = True
        self._think_start  = None
        self._game_started = True
        self._game_over    = False
        if paused:
            self._resume_event.clear()
            self._play_btn.config(state="normal", text="▶", fg="#88ff44", cursor="hand2")
        else:
            self._resume_event.set()
            self._play_btn.config(state="normal", text="⏸", fg="#ffaa44", cursor="hand2")
        self._action_btn.config(state="normal", cursor="hand2")
        self._tourn_btn.config(state="disabled", cursor="")
        self._t_close.config(fg="#444444", cursor="")
        self._bn.config(cursor="")
        self._wn.config(cursor="")
        self._update_action_btn()
        self._show_tournament_panel()
        self._t_title.config(text="Tournament")
        self._t_info.config(text="Starting engines…", fg="#aaaaaa")
        for t in (self._t_table, self._t_results):
            t.config(state="normal")
            t.delete("1.0", "end")
            t.config(state="disabled")

    def prepare_tournament_game(self, white_engine, black_engine):
        """Clear the board for the next tournament game. Main thread."""
        self.set_engines(white_engine, black_engine)
        self._update_engine_name(chess.WHITE, white_engine.name)
        self._update_engine_name(chess.BLACK, black_engine.name)
        self._think_start  = None
        self._game_over    = False
        self._game_started = True
        self._draw_board(chess.Board())
        self._draw_graph({})
        self._last_move_lbl.config(text="")
        self._moves_lbl.config(text="")
        for frame, nm, tm, ev in [
            (self._wf, self._wn, self._wt, self._we),
            (self._bf, self._bn, self._bt, self._be),
        ]:
            for w in (frame, nm, tm, ev):
                w.config(bg=self.BG_IDLE)
            ev.config(text="eval: ---")
        self._update_action_btn()

    def exit_tournament_mode(self):
        """Back to normal buttons. The panel stays until closed with ✕. Main thread."""
        self._tournament_running = False
        self._think_start = None
        self._tourn_btn.config(state="normal", cursor="hand2")
        self._t_close.config(fg="#aaaaaa", cursor="hand2")
        self._update_action_btn()

    # ── Tournament setup dialog ──

    def _open_tournament_dialog(self):
        if self._tournament_running or not self._tournament_start_cb:
            return
        tk = self._tk
        BG, FG, DIM = "#1a1a1a", "#cccccc", "#888888"
        ENTRY = dict(font=self._font(10), width=9, bg="#2e2e2e", fg="white",
                     insertbackground="white", relief="flat",
                     highlightthickness=1, highlightbackground="#444444",
                     highlightcolor="#55aaff", justify="right")
        saved = _load_tournament_settings()
        tc = self._settings_get_cb() if self._settings_get_cb else _default_time_control()

        items = [(disp, path) for disp, path, _ in _engine_candidates()]
        known = {p for _, p in items}
        for pth in saved.get("engines", []):
            apath = os.path.abspath(pth)
            if apath not in known and os.path.isfile(apath):
                items.append((os.path.splitext(os.path.basename(apath))[0], apath))
                known.add(apath)

        win = tk.Toplevel(self.root)
        win.title("Tournament")
        win.configure(bg=BG, padx=18, pady=14)
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text="Tournament", font=self._font(12, "bold"), fg="white",
                 bg=BG).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        tk.Label(win, text="Engines (click to select, 2 or more):", font=self._font(9),
                 fg=DIM, bg=BG).grid(row=1, column=0, columnspan=4, sticky="w")

        lf = tk.Frame(win, bg=BG)
        lf.grid(row=2, column=0, columnspan=4, sticky="we", pady=(2, 4))
        vsb = tk.Scrollbar(lf)
        vsb.pack(side="right", fill="y")
        lb = tk.Listbox(lf, font=self._font(10), bg="#111111", fg=FG, height=8, width=44,
                        selectmode="multiple", exportselection=False,
                        selectbackground="#3a5a00", selectforeground="white",
                        relief="flat", activestyle="none", yscrollcommand=vsb.set)
        lb.pack(side="left", fill="both", expand=True)
        vsb.config(command=lb.yview)
        selected = {os.path.abspath(p) for p in saved.get("engines", [])}
        for k, (disp, path) in enumerate(items):
            lb.insert("end", f"  {disp}")
            if path in selected:
                lb.selection_set(k)

        def _browse():
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title="Add engine to tournament",
                filetypes=[("Executable", "*.exe"), ("All files", "*.*")], parent=win)
            if not path:
                return
            apath = os.path.abspath(path)
            if apath in known:
                k = [p for _, p in items].index(apath)
            else:
                items.append((os.path.splitext(os.path.basename(apath))[0], apath))
                known.add(apath)
                lb.insert("end", f"  {items[-1][0]}")
                k = len(items) - 1
            lb.selection_set(k)
            lb.see(k)
            _update()

        tk.Button(win, text="Add engine…", font=self._font(9), bg="#3a3a3a", fg="#aaaaaa",
                  relief="flat", padx=10, pady=2, activebackground="#555555",
                  activeforeground="white", cursor="hand2", command=_browse
                  ).grid(row=3, column=0, columnspan=4, sticky="w")

        # Format + count
        fmt_var = tk.StringVar(value=saved.get("format", "rr")
                               if saved.get("format") in ("rr", "swiss") else "rr")
        counts = {"rr": int(saved.get("rr_rounds", 1) or 1),
                  "swiss": int(saved.get("swiss_matches", 5) or 5)}
        count_var = tk.StringVar(value=str(counts[fmt_var.get()]))
        cur_fmt = [fmt_var.get()]

        ff = tk.Frame(win, bg=BG)
        ff.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        tk.Label(ff, text="Format:", font=self._font(10), fg=FG, bg=BG, width=20,
                 anchor="w").pack(side="left")
        RB = dict(variable=fmt_var, font=self._font(10), fg=FG, bg=BG,
                  selectcolor="#2e2e2e", activebackground=BG, activeforeground="white",
                  cursor="hand2")

        cf = tk.Frame(win, bg=BG)
        cf.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        count_lbl = tk.Label(cf, text="", font=self._font(10), fg=FG, bg=BG,
                             width=20, anchor="w")
        count_lbl.pack(side="left")
        tk.Spinbox(cf, from_=1, to=99, textvariable=count_var, width=5,
                   font=self._font(10), bg="#2e2e2e", fg="white", relief="flat",
                   buttonbackground="#3a3a3a", insertbackground="white",
                   justify="right").pack(side="left")
        games_lbl = tk.Label(cf, text="", font=self._font(9), fg=DIM, bg=BG)
        games_lbl.pack(side="left", padx=(10, 0))

        def _on_fmt():
            try:
                counts[cur_fmt[0]] = max(1, int(count_var.get()))
            except ValueError:
                pass
            cur_fmt[0] = fmt_var.get()
            count_var.set(str(counts[cur_fmt[0]]))
            _update()

        tk.Radiobutton(ff, text="Round robin", value="rr", command=_on_fmt, **RB
                       ).pack(side="left")
        tk.Radiobutton(ff, text="Swiss", value="swiss", command=_on_fmt, **RB
                       ).pack(side="left", padx=(10, 0))

        # Time control (same for all engines)
        tf = tk.Frame(win, bg=BG)
        tf.grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))
        tk.Label(tf, text="Time / increment:", font=self._font(10), fg=FG, bg=BG,
                 width=20, anchor="w").pack(side="left")
        time_e = tk.Entry(tf, **ENTRY)
        time_e.insert(0, fmt_duration(saved.get("time", tc["white_time"])))
        time_e.pack(side="left")
        tk.Label(tf, text="+", font=self._font(10), fg=DIM, bg=BG).pack(side="left", padx=4)
        inc_e = tk.Entry(tf, **ENTRY)
        inc_e.insert(0, f"{float(saved.get('inc', tc['white_inc'])):g}")
        inc_e.pack(side="left")
        tk.Label(win, text="Time: seconds or m:ss   Increment: seconds",
                 font=self._font(8), fg="#666666", bg=BG
                 ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))

        msg = tk.Label(win, text="", font=self._font(9), fg="#ff5533", bg=BG)
        msg.grid(row=8, column=0, columnspan=4, sticky="w", pady=(6, 0))

        def _update(*_):
            fmt = fmt_var.get()
            count_lbl.config(text="Rounds:" if fmt == "rr" else "Matches per engine:")
            n = len(lb.curselection())
            try:
                c = max(1, int(count_var.get()))
            except ValueError:
                games_lbl.config(text="")
                return
            if n < 2:
                games_lbl.config(text="select at least 2 engines")
                return
            total = c * n * (n - 1) // 2 if fmt == "rr" else c * (n // 2)
            note = " (with rematches)" if fmt == "swiss" and c > n - 1 + n % 2 else ""
            games_lbl.config(text=f"= {total} game{'s' if total != 1 else ''}{note}")

        lb.bind("<<ListboxSelect>>", _update)
        count_var.trace_add("write", _update)
        _update()

        def _start(event=None):
            sel = [items[k][1] for k in lb.curselection()]
            if len(sel) < 2:
                msg.config(text="Select at least 2 engines.")
                return
            try:
                count = int(count_var.get())
                if not 1 <= count <= 999:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Invalid number: {count_var.get()!r}")
                return
            try:
                t = parse_duration(time_e.get())
                if t <= 0:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Invalid time: {time_e.get()!r}")
                return
            try:
                inc = float(inc_e.get().strip() or 0)
                if inc < 0:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Invalid increment: {inc_e.get()!r}")
                return
            fmt = fmt_var.get()
            counts[fmt] = count
            _save_tournament_settings({
                "engines": sel, "format": fmt,
                "rr_rounds": counts["rr"], "swiss_matches": counts["swiss"],
                "time": t, "inc": inc,
            })
            # Unique display names (two engines may share a file name)
            names, used = [], {}
            for pth in sel:
                base = os.path.splitext(os.path.basename(pth))[0]
                used[base] = used.get(base, 0) + 1
                names.append(base if used[base] == 1 else f"{base} #{used[base]}")
            cfg = {"engines": list(zip(names, sel)), "format": fmt,
                   "count": count, "time": t, "inc": inc}
            win.destroy()
            self._tournament_start_cb(cfg)

        def _continue():
            from tkinter import filedialog
            os.makedirs(TOURNAMENTS_DIR, exist_ok=True)
            path = filedialog.askopenfilename(
                title="Continue tournament", initialdir=TOURNAMENTS_DIR,
                filetypes=[("Tournament", "*.json"), ("All files", "*.*")], parent=win)
            if not path:
                return
            try:
                st = _load_tournament_file(path)
            except ValueError as e:
                msg.config(text=str(e))
                return
            win.destroy()
            self._tournament_start_cb({"resume": st})

        tk.Button(win, text="Continue from file…", font=self._font(9), bg="#3a3a3a",
                  fg="#ffcc44", relief="flat", padx=10, pady=4, activebackground="#555555",
                  activeforeground="#ffdd66", cursor="hand2", command=_continue
                  ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(12, 0))

        bf = tk.Frame(win, bg=BG)
        bf.grid(row=9, column=2, columnspan=2, sticky="e", pady=(12, 0))
        tk.Button(bf, text="Start", font=self._font(9),
                  bg="#2a4a00", fg="#88ff44", relief="flat", padx=14, pady=4,
                  activebackground="#3a6a00", activeforeground="#aaff66",
                  cursor="hand2", command=_start).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=12, pady=4,
                  activebackground="#555555", activeforeground="white",
                  cursor="hand2", command=win.destroy).pack(side="left")
        if self.game_in_progress:
            tk.Label(win, text="Starting a tournament stops the current game.",
                     font=self._font(9), fg="#ffaa44", bg=BG
                     ).grid(row=10, column=0, columnspan=4, sticky="w", pady=(8, 0))

        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Return>", _start)

        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()  - win.winfo_reqwidth())  // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.wait_window()

    def save_tournament_file(self, st: dict, ask: Optional[str] = None) -> Optional[str]:
        """Let the user save a tournament state to a file. With `ask`, first
        ask whether to save at all. Returns the path, or None. Main thread."""
        from tkinter import filedialog, messagebox
        if ask and not messagebox.askyesno("Save tournament", ask, parent=self.root):
            return None
        try:
            os.makedirs(TOURNAMENTS_DIR, exist_ok=True)
        except OSError:
            pass
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        path = filedialog.asksaveasfilename(
            title="Save tournament", parent=self.root, initialdir=TOURNAMENTS_DIR,
            initialfile=f"tournament_{stamp}.json", defaultextension=".json",
            filetypes=[("Tournament", "*.json"), ("All files", "*.*")])
        if not path:
            return None
        try:
            _write_json_atomic(path, st)
        except OSError as e:
            messagebox.showerror("Save tournament", f"Could not save:\n{e}", parent=self.root)
            return None
        return path

    # ── Play / Pause ──

    def _on_play_pause(self):
        if self._game_over and not self._tournament_running:
            self._on_restart()
            return
        if self._resume_event.is_set():
            self._resume_event.clear()
            self._play_btn.config(text="▶", fg="#88ff44")
        else:
            self._resume_event.set()
            self._play_btn.config(text="⏸", fg="#ffaa44")
            if not self._game_started:
                self._game_started = True
                self._bn.config(cursor="")
                self._wn.config(cursor="")
                self._update_action_btn()

    def _set_game_over_btn(self):
        if self._tournament_running:
            # The tournament moves on to the next game by itself; ▶/⏸ keeps
            # pausing and resuming the tournament.
            self._game_over   = True
            self._think_start = None
            return
        self._game_over = True
        self._resume_event.clear()
        self._play_btn.config(text="↺", fg="#44aaff")
        self._bn.config(cursor="hand2")
        self._wn.config(cursor="hand2")
        self._update_action_btn()

    def reset_play_button(self):
        self._game_over = False
        self._resume_event.clear()
        self._play_btn.config(text="▶", fg="#88ff44")
        self._update_action_btn()

    # ── Restart ──

    def _on_restart(self):
        if self._restart_cb:
            self._restart_cb()

    def reset(self):
        """Reset GUI state for a new game. Call from main thread."""
        self._think_start  = None
        self._game_over    = False
        self._game_started = False
        self._bn.config(cursor="hand2")
        self._wn.config(cursor="hand2")
        self._draw_board(chess.Board())
        self._draw_graph({})
        self._status.config(text="Restarting...")
        self._last_move_lbl.config(text="")
        self._moves_lbl.config(text="")
        for frame, nm, tm, ev in [
            (self._wf, self._wn, self._wt, self._we),
            (self._bf, self._bn, self._bt, self._be),
        ]:
            for w in (frame, nm, tm, ev):
                w.config(bg=self.BG_IDLE)
            tm.config(text="--:--.-")
            ev.config(text="eval: ---")
        self._update_action_btn()

    # ── Thread-safe update ──

    def update(self, **kw):
        self._uq.put(kw)

    def _poll(self):
        try:
            while True:
                self._apply(self._uq.get_nowait())
        except queue.Empty:
            pass
        # Live countdown for the thinking player
        if self._think_start is not None:
            remaining = max(0.0, self._think_budget - (time.monotonic() - self._think_start))
            lbl = self._wt if self._think_color == chess.WHITE else self._bt
            lbl.config(text=fmt_time(remaining))
        self.root.after(40, self._poll)

    def _apply(self, d: dict):
        if "animate_move" in d:
            self._start_animation(*d["animate_move"])
        if "board" in d:
            self._draw_board(d["board"], d.get("last_move"))
        if "think_start" in d:
            self._think_start  = d["think_start"]
            self._think_color  = d.get("think_color")
            self._think_budget = d.get("think_budget", 0.0)
        if "white_time" in d:
            self._wt.config(text=fmt_time(d["white_time"]))
        if "black_time" in d:
            self._bt.config(text=fmt_time(d["black_time"]))
        if "white_eval" in d:
            v = d["white_eval"]
            self._we.config(text=f"eval: {v}" if v else "eval: ---")
        if "black_eval" in d:
            v = d["black_eval"]
            self._be.config(text=f"eval: {v}" if v else "eval: ---")
        if "status" in d:
            self._status.config(text=d["status"])
        if "move" in d:
            self._last_move_lbl.config(text=d["move"])
        if "moves_text" in d:
            self._moves_lbl.config(text=d["moves_text"])
        if "graph_history" in d:
            self._draw_graph(d["graph_history"])
        if "tournament" in d:
            self._render_tournament(d["tournament"])
        if "game_over" in d:
            self._set_game_over_btn()
        if "reset_play_btn" in d:
            self.reset_play_button()
        if "active" in d:
            active = d["active"]
            for frame, nm, tm, ev, color in [
                (self._wf, self._wn, self._wt, self._we, chess.WHITE),
                (self._bf, self._bn, self._bt, self._be, chess.BLACK),
            ]:
                bg = self.BG_ACTV if active == color else self.BG_IDLE
                for w in (frame, nm, tm, ev):
                    w.config(bg=bg)
        if "engine_changed" in d:
            self._update_engine_name(*d["engine_changed"])
        if "play_btn_enabled" in d:
            if d["play_btn_enabled"]:
                self._play_btn.config(state="normal", fg="#88ff44", cursor="hand2")
                self._action_btn.config(state="normal", fg="#aaaaaa", cursor="hand2")
            else:
                self._play_btn.config(state="disabled", fg="#555555", cursor="")
                self._action_btn.config(state="disabled", fg="#555555", cursor="")
        if "call" in d:
            d["call"]()


# ──────────────────────────────────────────────────────────────────────────────
# Game runner
# ──────────────────────────────────────────────────────────────────────────────

class Game:
    def __init__(
        self,
        white: UCIEngine,
        black: UCIEngine,
        total_time: float,
        increment: float,
        gui: Optional[ChessGUI] = None,
        stop_event: Optional[threading.Event] = None,
        resume_event: Optional[threading.Event] = None,
        black_time: Optional[float] = None,
        black_increment: Optional[float] = None,
        event: str = "Engine game",
        round_: str = "-",
        save_record: bool = True,
    ):
        """total_time/increment apply to White (and to Black unless
        black_time/black_increment are given). Finished games are saved to
        GAMES_DIR unless save_record is False."""
        self.event        = event
        self.round        = round_
        self.save_record  = save_record
        self.record_path: Optional[str] = None
        self.white        = white
        self.black        = black
        self.white_total  = total_time
        self.black_total  = total_time if black_time is None else black_time
        self.white_inc    = increment
        self.black_inc    = increment if black_increment is None else black_increment
        self.gui          = gui
        self.stop_event   = stop_event or threading.Event()
        self.resume_event = resume_event
        self.board      = chess.Board()
        self.wtime      = self.white_total
        self.btime      = self.black_total
        self.white_eval: Optional[str] = None
        self.black_eval: Optional[str] = None
        self._move_texts: list[str]    = []
        # Per-ply stats per side, parallel + same length (None on the other
        # side's plies). Evals are normalised to White's POV.
        self._hist: dict = {m: ([], []) for m in GRAPH_METRICS}
        self.reason: str = ""   # how the game ended, e.g. "checkmate", "time forfeit"
        self.reason_detail: str = ""   # e.g. "illegal move e2e5"
        # Engine that probably crashed or hung (see _is_unresponsive); the
        # caller restarts it after the game
        self.crashed_engine: Optional[UCIEngine] = None

    def _gui(self, **kw):
        if self.gui:
            self.gui.update(**kw)

    def _push_stats(self, score: Optional[str], depth: Optional[int],
                    nodes: Optional[int], turn: chess.Color):
        v = parse_eval(score)
        # Normalise to White's POV: negate when it's Black's score
        norm = (v if turn == chess.WHITE else (-v if v is not None else None))
        side = 0 if turn == chess.WHITE else 1
        for metric, val in (("eval", norm), ("depth", depth), ("nodes", nodes)):
            lists = self._hist[metric]
            lists[side].append(val)
            lists[1 - side].append(None)
        self._gui(graph_history={m: (list(w), list(b)) for m, (w, b) in self._hist.items()})

    def run(self) -> Tuple[str, chess.pgn.Game]:
        self.white.new_game()
        self.black.new_game()

        w, b = self.white.name, self.black.name
        sep  = "-" * 62
        print(f"\n{sep}")
        print(f"  WHITE: {w}")
        print(f"  BLACK: {b}")
        if (self.white_total, self.white_inc) == (self.black_total, self.black_inc):
            print(f"  Time : {self.white_total:g}s + {self.white_inc:g}s/move")
        else:
            print(f"  Time : White {self.white_total:g}s + {self.white_inc:g}s/move, "
                  f"Black {self.black_total:g}s + {self.black_inc:g}s/move")
        print(sep + "\n")
        print(board_ascii(self.board))
        print()

        waiting = self.resume_event is not None and not self.resume_event.is_set()
        self._gui(
            board=chess.Board(),
            white_time=self.wtime,
            black_time=self.btime,
            white_eval=None,
            black_eval=None,
            status="Press ▶ to start" if waiting else f"WHITE: {w}   BLACK: {b}",
            move="",
            active=None,
        )

        pgn_game = chess.pgn.Game()
        now = datetime.datetime.now()
        pgn_game.headers.update({
            "Event":  self.event,
            "Site":   f"chess_player {__version__}",
            "Date":   now.strftime("%Y.%m.%d"),
            "Round":  self.round,
            "White":  w,
            "Black":  b,
            "Result": "*",
            "Time":   now.strftime("%H:%M:%S"),
        })
        if (self.white_total, self.white_inc) == (self.black_total, self.black_inc):
            pgn_game.headers["TimeControl"] = f"{self.white_total:g}+{self.white_inc:g}"
        else:
            pgn_game.headers["WhiteTimeControl"] = f"{self.white_total:g}+{self.white_inc:g}"
            pgn_game.headers["BlackTimeControl"] = f"{self.black_total:g}+{self.black_inc:g}"
        pgn_node = pgn_game
        result   = "*"
        move_num = 1

        while not self.board.is_game_over(claim_draw=True):
            if self.stop_event.is_set():
                result = "*"
                break

            # Wait while paused / waiting for ▶
            if self.resume_event is not None:
                was_paused = not self.resume_event.is_set()
                while not self.resume_event.wait(timeout=0.1):
                    if self.stop_event.is_set():
                        break
                if was_paused:
                    self._gui(status=f"WHITE: {w}   BLACK: {b}")

            if self.stop_event.is_set():
                result = "*"
                break

            turn   = self.board.turn
            engine = self.white if turn == chess.WHITE else self.black
            my_t   = self.wtime if turn == chess.WHITE else self.btime
            opp    = self.black if turn == chess.WHITE else self.white

            wms    = int(self.wtime * 1000)
            bms    = int(self.btime * 1000)
            winc_ms = int(self.white_inc * 1000)
            binc_ms = int(self.black_inc * 1000)

            color_str = "White" if turn == chess.WHITE else "Black"
            prefix    = f"  {move_num:3d}." if turn == chess.WHITE else "      "
            print(f"{color_str} ({engine.name}) — {fmt_time(my_t)} remaining", flush=True)

            hard_limit = my_t + 1.0
            t0 = time.monotonic()

            self._gui(
                active=turn,
                status=f"Move {move_num} — {color_str}: {engine.name} thinking...",
                think_start=t0,
                think_color=turn,
                think_budget=my_t,
            )

            def on_info(line, _turn=turn):
                parts = line.split()
                try:
                    if "score" in parts:
                        idx  = parts.index("score")
                        kind = parts[idx + 1]
                        val  = int(parts[idx + 2])
                        if kind == "cp":
                            s = f"{val / 100:+.2f}"
                        elif kind == "mate":
                            s = f"M{val}" if val > 0 else f"-M{abs(val)}"
                        else:
                            return
                        if self.gui:
                            if _turn == chess.WHITE:
                                self.gui.update(white_eval=s)
                            else:
                                self.gui.update(black_eval=s)
                except (IndexError, ValueError):
                    pass

            move_uci, score = engine.get_move(
                self.board, wms, bms, winc_ms, binc_ms,
                hard_limit, info_callback=on_info, stop_event=self.stop_event,
            )

            if self.stop_event.is_set():
                result = "*"
                break

            elapsed = time.monotonic() - t0

            if turn == chess.WHITE:
                self.wtime = self.wtime - elapsed + self.white_inc
                self.white_eval = score
            else:
                self.btime = self.btime - elapsed + self.black_inc
                self.black_eval = score

            self._gui(
                think_start=None,      # stop the live countdown
                white_time=self.wtime,
                black_time=self.btime,
                white_eval=self.white_eval,
                black_eval=self.black_eval,
            )

            # ── Flag ──
            flagged = self.wtime if turn == chess.WHITE else self.btime
            if flagged < 0:
                result = "0-1" if turn == chess.WHITE else "1-0"
                self.reason = "time forfeit"
                if _is_unresponsive(engine, elapsed):
                    self.crashed_engine = engine
                print(f"\n  *** {engine.name} FLAGGED (ran out of time) ***")
                print(f"  Result: {result} — {opp.name} wins on time\n")
                self._gui(status=f"{engine.name} ran out of time! {opp.name} wins — {result}", game_over=True)
                break

            # ── No move ──
            if move_uci is None:
                result = "0-1" if turn == chess.WHITE else "1-0"
                self.reason = "no move"
                if _is_unresponsive(engine, elapsed):
                    self.crashed_engine = engine
                print(f"\n  *** {engine.name} returned no move ***")
                print(f"  Result: {result} — {opp.name} wins\n")
                self._gui(status=f"{engine.name} failed to respond! {opp.name} wins — {result}", game_over=True)
                break

            # ── Parse & validate ──
            try:
                move = chess.Move.from_uci(move_uci)
            except ValueError:
                result = "0-1" if turn == chess.WHITE else "1-0"
                self.reason = "malformed move"
                self.reason_detail = f"malformed move {move_uci!r}"
                print(f"\n  *** {engine.name} returned malformed move: {move_uci!r} ***")
                print(f"  Result: {result} — {opp.name} wins\n")
                self._gui(status=f"{engine.name} malformed move! {opp.name} wins — {result}", game_over=True)
                break

            if move not in self.board.legal_moves:
                result = "0-1" if turn == chess.WHITE else "1-0"
                self.reason = "illegal move"
                self.reason_detail = f"illegal move {move_uci}"
                print(f"\n  *** {engine.name} played ILLEGAL move: {move_uci} ***")
                print(f"  Result: {result} — {opp.name} wins\n")
                self._gui(status=f"{engine.name} illegal move! {opp.name} wins — {result}", game_over=True)
                break

            # ── Animate then apply ──
            san = self.board.san(move)
            if self.gui and not self.stop_event.is_set():
                self.gui.animate_move(self.board, move, stop_event=self.stop_event)
            self.board.push(move)
            pgn_node = pgn_node.add_variation(move)
            if score:
                pgn_node.comment = score
            pgn_node.set_clock(max(0.0, self.wtime if turn == chess.WHITE else self.btime))
            self._push_stats(score, engine.last_depth, engine.last_nodes, turn)

            score_str  = f" [{score}]" if score else ""
            print(f"{prefix} {san:<9s}{score_str}  ({elapsed:.2f}s)", flush=True)

            if turn == chess.WHITE:
                self._move_texts.append(f"{move_num}. {san}")
            else:
                if self._move_texts:
                    self._move_texts[-1] += f" {san}"
                else:
                    self._move_texts.append(f"{move_num}... {san}")
                move_num += 1

            moves_display = "  ".join(self._move_texts[-10:])
            move_lbl = f"{move_num - (0 if turn == chess.WHITE else 1)}. {san}"
            self._gui(
                board=self.board,
                last_move=move,
                move=move_lbl,
                moves_text=moves_display,
            )

            print(board_ascii(self.board))
            print()

        # ── Natural game-over ──
        if result == "*" and not self.stop_event.is_set():
            result  = self.board.result(claim_draw=True)
            outcome = self.board.outcome(claim_draw=True)
            reason  = outcome.termination.name.replace("_", " ").title() if outcome else "draw"
            self.reason = reason.lower()
            verdict = (f"{self.white.name} wins" if result == "1-0" else
                       f"{self.black.name} wins" if result == "0-1" else "Draw")
            print(f"\n{sep}")
            print(f"  Game over: {reason}")
            print(f"  Result:    {result}  —  {verdict}")
            print(sep + "\n")
            self._gui(status=f"Game over — {reason} — {result} ({verdict})", active=None, game_over=True)

        pgn_game.headers["Result"] = result
        if result != "*":
            winner = (self.white.name if result == "1-0" else
                      self.black.name if result == "0-1" else "draw")
            pgn_game.headers["Winner"] = winner
            pgn_game.headers["Reason"] = self.reason_detail or self.reason
            pgn_game.headers["Termination"] = (
                "time forfeit" if self.reason == "time forfeit" else
                "rules infraction" if self.reason in ("illegal move", "malformed move", "no move")
                else "normal")
            if self.save_record:
                self.record_path = _save_game_record(pgn_game)
                if self.record_path:
                    print(f"  Game saved -> {self.record_path}")
        return result, pgn_game


# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Crashed / hung engines
# ──────────────────────────────────────────────────────────────────────────────

# An engine that loses on time (or without a move) after thinking at least this
# long on its last move has probably crashed or hung, and is restarted.
HUNG_AFTER_S = 10.0

def _is_unresponsive(engine: UCIEngine, elapsed: float) -> bool:
    exited = engine.process is not None and engine.process.poll() is not None
    return exited or elapsed >= HUNG_AFTER_S

def _restart_engine(old: UCIEngine) -> UCIEngine:
    """Kill `old` and start a fresh process of the same engine, with the
    options the user changed re-applied. Raises on failure."""
    try:
        old.quit()   # kills the process if it does not respond to 'quit'
    except Exception:
        pass
    new = UCIEngine(old.path, old.name)
    new.start()
    for name, opt in old.options.items():
        if (opt["type"] != "button" and opt["value"] is not None
                and opt["value"] != opt["default"] and name in new.options):
            new.set_option(name, opt["value"])
            new.options[name]["value"] = opt["value"]
    return new


def print_summary(result: str, white: UCIEngine, black: UCIEngine):
    print("RESULT:", result)
    if result == "1-0":
        print(f"WINNER: {white.name} (White)")
    elif result == "0-1":
        print(f"WINNER: {black.name} (Black)")
    else:
        print("RESULT: Draw")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Play a match between two UCI chess engines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python chess_player.py --w engines/stockfish.exe --b engines/mybot.exe
  python chess_player.py --w sf.exe --b cd.exe --time 120 --increment 2
  python chess_player.py --w sf.exe --b cd.exe --time 30 --nogui --pgn out.pgn
        """,
    )
    parser.add_argument("--w",           dest="white_engine", metavar="PATH", default=None,
                        help="Path to engine playing White")
    parser.add_argument("--b",           dest="black_engine", metavar="PATH", default=None,
                        help="Path to engine playing Black")
    parser.add_argument("--time", "-t",  type=float, default=None,  metavar="SEC",
                        help="Total time per player in seconds (default: 60, or last GUI setting)")
    parser.add_argument("--increment", "-i", type=float, default=None, metavar="SEC",
                        help="Increment per move in seconds (default: 0, or last GUI setting)")
    parser.add_argument("--nogui",       action="store_true", help="Run headless (no window)")
    parser.add_argument("--pgn",         type=str, default=None, metavar="FILE")
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    if args.nogui:
        args.time = 60.0 if args.time is None else args.time
        args.increment = 0.0 if args.increment is None else args.increment
        # Headless: both engines required
        if not args.white_engine or not args.black_engine:
            sys.exit("Error: --w and --b are required in --nogui mode")
        for path, label in [(args.white_engine, "--w"), (args.black_engine, "--b")]:
            if not os.path.isfile(path):
                sys.exit(f"Error: engine not found ({label}): {path}")
        white_name = os.path.splitext(os.path.basename(args.white_engine))[0]
        black_name = os.path.splitext(os.path.basename(args.black_engine))[0]
        white = UCIEngine(args.white_engine, white_name)
        black = UCIEngine(args.black_engine, black_name)
        print("Starting engines...")
        try:
            white.start(); print(f"  {white_name}: OK")
            black.start(); print(f"  {black_name}: OK")
        except RuntimeError as e:
            sys.exit(f"Engine startup failed: {e}")
        result, pgn = Game(white, black, args.time, args.increment).run()
        white.quit(); black.quit()
        if args.pgn and pgn:
            with open(args.pgn, "w") as fh:
                print(pgn, file=fh)
            print(f"PGN saved -> {args.pgn}")
        print_summary(result, white, black)
        return

    # ── GUI mode ──
    # Validate any paths given on the command line
    if args.white_engine and not os.path.isfile(args.white_engine):
        sys.exit(f"Error: engine not found (--w): {args.white_engine}")
    if args.black_engine and not os.path.isfile(args.black_engine):
        sys.exit(f"Error: engine not found (--b): {args.black_engine}")

    # Time control: saved GUI settings, overridden by --time / --increment
    tc = _load_time_settings() or _default_time_control()
    if args.time is not None or args.increment is not None:
        tc = _default_time_control(
            args.time if args.time is not None else tc["white_time"],
            args.increment if args.increment is not None else tc["white_inc"],
        )

    state: dict = {
        "game": None, "thread": None, "stop": threading.Event(),
        "result": "*", "pgn": None,
        "white": None, "black": None,
        "game_launched": False,
        "tc": tc,
        "tournament": None,   # {"stop", "engines", "thread"} while one runs
    }

    def _start_engine(path, required: bool):
        """Start an engine. Engines from the command line are required (exit on
        failure); engines restored from last session are skipped if they fail."""
        name = os.path.splitext(os.path.basename(path))[0]
        eng = UCIEngine(path, name)
        print(f"Starting {name}...")
        try:
            eng.start()
            print(f"  {name}: OK")
        except Exception as e:
            if required:
                sys.exit(f"Engine startup failed: {e}")
            print(f"  {name}: failed to start ({e}) — not restored")
            try:
                eng.quit()
            except Exception:
                pass
            return None
        return eng

    # Command-line engines take precedence; otherwise restore last session's.
    # This can take a while, so a splash window shows that the app is starting.
    splash = _Splash.create()

    def _startup():
        last = _load_last_engines()
        for key, cli_path in (("white", args.white_engine), ("black", args.black_engine)):
            path = cli_path or (last.get(key) if last.get(key) and os.path.isfile(last[key]) else None)
            if not path:
                continue
            if splash:
                splash.status = f"Starting {os.path.splitext(os.path.basename(path))[0]}…"
            state[key] = _start_engine(path, required=bool(cli_path))
        if splash:
            splash.status = "Loading…"
        try:   # preload the graphics libraries the main window needs
            import PIL.Image, PIL.ImageTk, PIL.ImageDraw  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            pass

    if splash:
        try:
            splash.run(_startup)
        finally:
            splash.close()
    else:
        _startup()

    engines_ready = state["white"] is not None and state["black"] is not None
    white_name = state["white"].name if state["white"] else "—"
    black_name = state["black"].name if state["black"] else "—"

    gui = ChessGUI(white_name, black_name, engines_ready=engines_ready)
    gui.set_engines(state["white"], state["black"])
    if args.white_engine or args.black_engine:
        # Remember command-line choices (don't overwrite on a failed restore)
        _save_last_engines(state["white"], state["black"])

    def start_game():
        state["game_launched"] = True
        stop = threading.Event()
        state["stop"] = stop
        tc = state["tc"]
        game = Game(state["white"], state["black"], tc["white_time"], tc["white_inc"],
                    gui, stop, resume_event=gui._resume_event,
                    black_time=tc["black_time"], black_increment=tc["black_inc"])
        state["game"] = game

        def run():
            state["result"], state["pgn"] = game.run()
            if game.crashed_engine is not None:
                replace_crashed_engine(game.crashed_engine)

        t = threading.Thread(target=run, daemon=True)
        state["thread"] = t
        t.start()

    def replace_crashed_engine(old: UCIEngine):
        """Game thread: restart an engine that stopped responding."""
        key = ("white" if state["white"] is old else
               "black" if state["black"] is old else None)
        if key is None:
            return   # already replaced (engine changed meanwhile)
        print(f"  {old.name} stopped responding — restarting it")
        gui.update(status=f"{old.name} stopped responding — restarting it…")
        try:
            new = _restart_engine(old)
        except Exception as e:
            print(f"  {old.name}: restart failed ({e})")
            gui.update(status=f"{old.name} stopped responding and could not be restarted: {e}")
            return
        if state[key] is not old:   # replaced by the user meanwhile
            new.quit()
            return
        state[key] = new

        def _done():
            gui.set_engines(state["white"], state["black"])
            gui.update(status=f"{old.name} stopped responding and was restarted")
        gui.update(call=_done)

    def restart():
        state["stop"].set()
        gui.update(reset_play_btn=True)
        gui.reset()
        gui.root.after(300, start_game)

    def change_engine(color: chess.Color, new_path: str):
        name = os.path.splitext(os.path.basename(new_path))[0]
        gui.update(status=f"Starting {name}…")

        def _do_start():
            new_eng = UCIEngine(new_path, name)
            try:
                new_eng.start()
            except RuntimeError as e:
                gui.update(status=f"Engine failed to start: {e}")
                return
            _save_engine_history(new_path)
            if color == chess.WHITE:
                old = state["white"]
                state["white"] = new_eng
            else:
                old = state["black"]
                state["black"] = new_eng
            if old:
                old.quit()
            gui.set_engines(state["white"], state["black"])
            _save_last_engines(state["white"], state["black"])

            both_ready = state["white"] is not None and state["black"] is not None
            if both_ready and not state["game_launched"]:
                # Both engines now selected for the first time — enable ▶ and launch
                gui.update(engine_changed=(color, name), play_btn_enabled=True, call=start_game)
            else:
                if state["white"] is None:
                    hint = f"{name} ready — click ♙ White name to select its engine"
                elif state["black"] is None:
                    hint = f"{name} ready — click ♟ Black name to select its engine"
                else:
                    hint = f"{name} ready"
                gui.update(engine_changed=(color, name), status=hint)

        threading.Thread(target=_do_start, daemon=True).start()

    def swap_colors():
        state["white"], state["black"] = state["black"], state["white"]
        w_name = state["white"].name if state["white"] else "—"
        b_name = state["black"].name if state["black"] else "—"
        gui.set_engines(state["white"], state["black"])
        _save_last_engines(state["white"], state["black"])
        gui.update(engine_changed=(chess.WHITE, w_name))
        gui.update(engine_changed=(chess.BLACK, b_name))
        restart()

    def apply_settings(new_tc: dict):
        state["tc"] = new_tc
        _save_time_settings(new_tc)
        if gui.tournament_running:
            gui.update(status="Time control saved — applies to normal games")
        elif gui.game_in_progress:
            gui.update(status="Time control saved — applies from the next game")
        elif state["game_launched"]:
            restart()   # not started yet / game over: reload clocks now

    # ── Tournament mode ──
    # Tournament games use their own engine processes, so the White/Black
    # engines of normal mode stay loaded and come back when it ends.

    def start_tournament(cfg: dict):
        """cfg: a new tournament from the setup dialog ({"engines", "format",
        "count", "time", "inc"}), or {"resume": state} to continue a saved one.
        A tournament always starts paused: the first game waits for ▶."""
        if state["tournament"] is not None:
            return
        tour = {"stop": threading.Event(), "engines": [], "thread": None, "save": False}
        state["tournament"] = tour
        state["stop"].set()          # stop any normal game
        gui.enter_tournament_mode(paused=True)
        t = threading.Thread(target=run_tournament, args=(cfg, tour), daemon=True)
        tour["thread"] = t
        t.start()

    def stop_tournament(save: bool = False):
        tour = state["tournament"]
        if tour:
            tour["save"] = save
            tour["stop"].set()
            gui.update(status="Stopping tournament…")

    def run_tournament(cfg: dict, tour: dict):
        """Tournament thread. The state (engines, time control, pairings and
        results so far) is autosaved to _CURRENT_TOURNAMENT_FILE whenever it
        changes, so an app that is closed mid-tournament resumes it on the
        next start; the game that was in progress is replayed."""
        stop = tour["stop"]
        if state["thread"]:
            state["thread"].join(timeout=5)   # let the normal game wind down
        resume = cfg.get("resume")
        if resume:
            eng_cfg = [(e["name"], e["path"]) for e in resume["engines"]]
            t_time, t_inc = resume["time"], resume["inc"]
            try:   # e.g. continued from a file: make sure a crash keeps it
                _write_json_atomic(_CURRENT_TOURNAMENT_FILE, resume)
            except OSError as e:
                print(f"  Could not autosave tournament: {e}")
        else:
            eng_cfg = cfg["engines"]
            t_time, t_inc = cfg["time"], cfg["inc"]
        tc_text = f"{fmt_duration(t_time)} + {t_inc:g}s"

        engines, failed = [], []
        for name, path in eng_cfg:
            if stop.is_set():
                break
            gui.update(status=f"Tournament: starting {name}…")
            eng = UCIEngine(path, name)
            try:
                eng.start()
                engines.append(eng)
                tour["engines"].append(eng)
                _save_engine_history(path)
            except Exception as e:
                print(f"  {name}: failed to start ({e}) — left out of the tournament")
                gui.update(status=f"{name} failed to start — left out")
                failed.append(name)
                try:
                    eng.quit()
                except Exception:
                    pass
                if resume:
                    break   # a resumed tournament needs all of its engines

        if len(engines) < 2 or stop.is_set() or (resume and failed):
            for eng in engines:
                eng.quit()
            if stop.is_set():
                msg = "Tournament stopped"
            elif resume:
                msg = f"Could not resume the tournament: {failed[0]} failed to start"
            else:
                msg = "Tournament aborted: fewer than 2 engines started"
            ask = (f"{msg}.\n\nSave the tournament to a file so you can continue it later?"
                   if resume and not stop.is_set() else None)
            gui.update(call=lambda: finish_tournament(None, msg, final_state=resume, ask=ask))
            return

        if resume:
            tourney = Tournament.from_dict(resume["tournament"])
        else:
            tourney = Tournament([e.name for e in engines], cfg["format"], cfg["count"])
        fmt_name = "Round robin" if tourney.fmt == "rr" else "Swiss"

        def make_state() -> dict:
            return {"kind": "chess_player tournament",
                    "version": _TOURNAMENT_STATE_VERSION,
                    "saved": datetime.datetime.now().isoformat(timespec="seconds"),
                    "engines": [{"name": e.name, "path": os.path.abspath(e.path)}
                                for e in engines],
                    "time": t_time, "inc": t_inc,
                    "tournament": tourney.to_dict()}

        def autosave():
            try:
                _write_json_atomic(_CURRENT_TOURNAMENT_FILE, make_state())
            except OSError as e:
                print(f"  Could not autosave tournament: {e}")

        autosave()
        gui.update(tournament=tourney.snapshot("running", tc_text))

        def wait_resume() -> bool:
            """Block while paused. False if the tournament was stopped."""
            if not gui._resume_event.is_set() and not stop.is_set():
                gui.update(status="Tournament paused — press ▶ to play the next game")
            while not gui._resume_event.wait(timeout=0.1):
                if stop.is_set():
                    return False
            return not stop.is_set()

        for rnd in range(tourney.total_rounds):
            if stop.is_set():
                break
            pairs, bye = tourney.pairings(rnd)
            if bye is not None and tourney.fmt == "swiss" and not tourney.has_bye(rnd):
                tourney.record_bye(rnd, bye)
            autosave()   # fixes this round's pairings
            for wi, bi in pairs:
                if tourney.has_game(rnd, wi, bi):
                    continue   # played before the tournament was resumed
                if not wait_resume():
                    break
                we, be = engines[wi], engines[bi]
                tourney.current = (rnd, wi, bi)
                ready = threading.Event()

                def _prep(we=we, be=be, ready=ready):
                    gui.prepare_tournament_game(we, be)
                    ready.set()

                gui.update(call=_prep, tournament=tourney.snapshot("running", tc_text))
                ready.wait(timeout=5)
                print(f"\n=== Tournament round {rnd + 1}, game {len(tourney.games) + 1}"
                      f"/{tourney.total_games}: {we.name} vs {be.name} ===")
                game = Game(we, be, t_time, t_inc, gui, stop,
                            resume_event=gui._resume_event,
                            event=f"{fmt_name} tournament", round_=str(rnd + 1))
                result, _pgn = game.run()
                tourney.current = None
                crashed = game.crashed_engine
                if crashed is not None and not stop.is_set():
                    # Restart it so it can play its remaining games
                    print(f"  {crashed.name} stopped responding — restarting it")
                    gui.update(status=f"{crashed.name} stopped responding — restarting it…")
                    try:
                        new = _restart_engine(crashed)
                        engines[engines.index(crashed)] = new
                        tour["engines"][tour["engines"].index(crashed)] = new
                        print(f"  {crashed.name}: restarted")
                    except Exception as e:
                        print(f"  {crashed.name}: restart failed ({e})")
                        gui.update(status=f"{crashed.name} could not be restarted: {e}")
                if stop.is_set() or result == "*":
                    break
                tourney.record(rnd, wi, bi, result, game.reason)
                autosave()
                gui.update(tournament=tourney.snapshot("running", tc_text))
                # Short pause so the final position and result can be seen
                if stop.wait(timeout=2.5):
                    break

        finished = not stop.is_set()
        for eng in engines:
            eng.quit()
        snap = tourney.snapshot("finished" if finished else "stopped", tc_text)
        if finished:
            winners = [r["name"] for r in snap["rows"] if r["rank"] == 1]
            msg = f"Tournament finished — winner: {', '.join(winners)}"
            print(f"\n{msg}")
            for r in snap["rows"]:
                print(f"  {r['rank']:>2}. {r['name']:<24} {r['points']:>5g} pts"
                      f"   SB {r['sb']:.2f}   (+{r['wins']} ={r['draws']} -{r['losses']})")
        else:
            msg = "Tournament stopped"
        final = None if finished else make_state()
        gui.update(call=lambda: finish_tournament(snap, msg, final_state=final))

    def finish_tournament(snap: Optional[dict], msg: str,
                          final_state: Optional[dict] = None, ask: Optional[str] = None):
        """Main thread: leave tournament mode and set up a normal game again.
        final_state: state of an unfinished tournament, saved to a file if the
        user asked for that (Stop and save…) or agrees to `ask`."""
        tour = state["tournament"]
        save = bool(tour and tour.get("save"))
        state["tournament"] = None
        gui.exit_tournament_mode()
        if snap:
            gui._render_tournament(snap)
        else:
            gui._close_tournament_panel()
        w, b = state["white"], state["black"]
        gui.set_engines(w, b)
        gui._update_engine_name(chess.WHITE, w.name if w else "—")
        gui._update_engine_name(chess.BLACK, b.name if b else "—")
        gui.reset()
        # Apply directly (not via the queue) so the next game starts paused
        gui._apply({"play_btn_enabled": bool(w and b)})
        gui.reset_play_button()
        if final_state is not None and (save or ask):
            path = gui.save_tournament_file(final_state, ask=None if save else ask)
            if path:
                msg += f" — saved to {os.path.basename(path)}"
        # The tournament is over (finished, or stopped by the user): don't resume it
        _remove_current_tournament()
        if w and b:
            start_game()
            # start_game's first status update says "Press ▶"; show the result instead
            gui.root.after(1500, lambda: gui.update(status=msg))
        else:
            gui.update(status=msg + " — click the player names to select engines")

    gui.set_tournament_callbacks(start_tournament, stop_tournament)

    gui.set_settings_callbacks(lambda: state["tc"], apply_settings)
    gui.set_restart_callback(restart)
    gui.set_swap_callback(swap_colors)
    gui.set_change_engine_callback(change_engine)

    # A tournament that was running when the app was closed: resume it
    resume_state = None
    if os.path.exists(_CURRENT_TOURNAMENT_FILE):
        try:
            resume_state = _load_tournament_file(_CURRENT_TOURNAMENT_FILE, check_engines=False)
        except ValueError as e:
            print(f"Ignoring saved tournament: {e}")
            try:
                os.replace(_CURRENT_TOURNAMENT_FILE, _CURRENT_TOURNAMENT_FILE + ".invalid")
            except OSError:
                pass

    if resume_state:
        print("Resuming the tournament from the last session")
        gui.root.after(300, lambda: start_tournament({"resume": resume_state}))
    elif engines_ready:
        start_game()
    # else: user must select engines via the GUI before ▶ is enabled

    gui.root.mainloop()

    state["stop"].set()
    if state["thread"]:
        state["thread"].join(timeout=5)
    tour = state["tournament"]
    if tour:
        tour["stop"].set()
        tour["thread"].join(timeout=5)
        for eng in tour["engines"]:
            eng.quit()

    if state["white"]:
        state["white"].quit()
    if state["black"]:
        state["black"].quit()

    if args.pgn and state["pgn"]:
        with open(args.pgn, "w") as fh:
            print(state["pgn"], file=fh)
        print(f"PGN saved -> {args.pgn}")

    if state["white"] and state["black"]:
        print_summary(state["result"], state["white"], state["black"])


class _Splash:
    """Small borderless "starting up" window, shown while the engines start
    (before the main window exists)."""

    BG = "#1a1a1a"

    @classmethod
    def create(cls) -> Optional["_Splash"]:
        try:
            return cls()
        except Exception as e:   # no display etc.: start without a splash
            print(f"(no splash window: {e})")
            return None

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.status = "Starting…"   # set from any thread, shown by run()
        self.root = r = tk.Tk()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=self.BG, highlightthickness=1, highlightbackground="#444444")
        tk.Label(r, text="♛  Chess Player", font=("Segoe UI Symbol", 18, "bold"),
                 fg="#ffcc44", bg=self.BG).pack(padx=48, pady=(26, 0))
        tk.Label(r, text=f"version {__version__}", font=("Consolas", 9),
                 fg="#777777", bg=self.BG).pack()
        self._msg = tk.Label(r, text=self.status, font=("Consolas", 10), fg="#cccccc",
                             bg=self.BG, width=40)
        self._msg.pack(pady=(18, 8))
        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Splash.Horizontal.TProgressbar", troughcolor="#2b2b2b",
                        background="#88ff44", bordercolor=self.BG,
                        lightcolor="#88ff44", darkcolor="#88ff44")
        self._bar = ttk.Progressbar(r, mode="indeterminate", length=280,
                                    style="Splash.Horizontal.TProgressbar")
        self._bar.pack(padx=48, pady=(0, 28))
        r.update_idletasks()
        w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        r.geometry(f"+{(r.winfo_screenwidth() - w) // 2}+{(r.winfo_screenheight() - h) // 3}")
        self._bar.start(12)
        r.update()

    def run(self, fn):
        """Run fn() in a worker thread while keeping the splash animated.
        Returns fn's result; re-raises its exception (incl. SystemExit)."""
        box: dict = {}

        def work():
            try:
                box["result"] = fn()
            except BaseException as e:
                box["error"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            if self._msg.cget("text") != self.status:
                self._msg.config(text=self.status)
            self.root.update()
            time.sleep(0.015)
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def close(self):
        try:
            self._bar.stop()
            self.root.destroy()
        except Exception:
            pass


def _show_error_dialog(msg: str):
    """Show a fatal error in a message box (windowed build has no console)."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(f"Chess Player {__version__}", msg)
        r.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if NO_CONSOLE and isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            _show_error_dialog(e.code)
            sys.exit(1)
        raise
    except Exception:
        if NO_CONSOLE:
            import traceback
            traceback.print_exc()
            _show_error_dialog("Unexpected error:\n\n" + traceback.format_exc(limit=5))
            sys.exit(1)
        raise
