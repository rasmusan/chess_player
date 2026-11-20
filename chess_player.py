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
__version__ = "1.0.1"


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
        self.last_depth: int = 0
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
        self.last_depth = 0
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
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG_IDLE)
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
        self._piece_imgs = self._load_pieces()
        self._anim_done  = threading.Event()
        self._build()
        self._poll()

    def set_restart_callback(self, cb):
        self._restart_cb = cb

    def set_engines(self, white_engine, black_engine):
        self._white_engine = white_engine
        self._black_engine = black_engine

    def set_change_engine_callback(self, cb):
        self._change_engine_cb = cb

    def set_swap_callback(self, cb):
        self._swap_cb = cb

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
        ENTRY = dict(font=("Consolas", 10), width=9, bg="#2e2e2e", fg="white",
                     insertbackground="white", relief="flat",
                     highlightthickness=1, highlightbackground="#444444",
                     highlightcolor="#55aaff", justify="right")
        tc = dict(self._settings_get_cb())

        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.configure(bg=BG, padx=18, pady=14)
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text="Time control", font=("Consolas", 12, "bold"),
                 fg="white", bg=BG).grid(row=0, column=0, columnspan=3,
                                         sticky="w", pady=(0, 10))

        grid = tk.Frame(win, bg=BG)
        grid.grid(row=1, column=0, columnspan=3, sticky="w")
        tk.Label(grid, text="", bg=BG).grid(row=0, column=0)
        tk.Label(grid, text="Time", font=("Consolas", 9), fg=DIM, bg=BG
                 ).grid(row=0, column=1, sticky="e", padx=(10, 0))
        tk.Label(grid, text="Increment", font=("Consolas", 9), fg=DIM, bg=BG
                 ).grid(row=0, column=2, sticky="e", padx=(10, 0))

        def _row(r, label, t, inc):
            lbl = tk.Label(grid, text=label, font=("Consolas", 10), fg=FG, bg=BG,
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
                       command=_on_toggle, font=("Consolas", 10), fg=FG, bg=BG,
                       selectcolor="#2e2e2e", activebackground=BG,
                       activeforeground="white", cursor="hand2"
                       ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        tk.Label(win, text="Time: seconds or m:ss   Increment: seconds",
                 font=("Consolas", 8), fg="#666666", bg=BG
                 ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        note = ("Changes apply from the next game." if self.game_in_progress
                else "")
        msg = tk.Label(win, text=note, font=("Consolas", 9), fg="#ffaa44", bg=BG)
        msg.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

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
            new_tc = {"separate": sep_var.get(),
                      "white_time": wt, "white_inc": wi,
                      "black_time": bt, "black_inc": bi}
            win.destroy()
            if self._settings_apply_cb:
                self._settings_apply_cb(new_tc)

        def _cancel(event=None):
            win.destroy()

        bf = tk.Frame(win, bg=BG)
        bf.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        tk.Button(bf, text="OK", font=("Consolas", 9),
                  bg="#2a4a00", fg="#88ff44", relief="flat", padx=14, pady=4,
                  activebackground="#3a6a00", activeforeground="#aaff66",
                  cursor="hand2", command=_ok).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=("Consolas", 9),
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
        if self._game_started and not self._game_over:
            self._action_btn.config(text="■", fg="#ff4444", activeforeground="#ff6666")
        else:
            self._action_btn.config(text="⇆", fg="#aaaaaa", activeforeground="#cccccc")

    def _on_action_btn(self):
        if self._game_started and not self._game_over:
            if self._resume_event.is_set():
                self._resume_event.clear()
                self._play_btn.config(text="▶", fg="#88ff44")
            self._open_stop_confirm()
        else:
            if self._swap_cb:
                self._swap_cb()

    def _open_stop_confirm(self):
        tk  = self._tk
        win = tk.Toplevel(self.root)
        win.title("Stop game")
        win.configure(bg="#1a1a1a")
        win.geometry("280x120")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="Stop and reset the current game?",
                 font=("Consolas", 10), fg="#cccccc", bg="#1a1a1a").pack(pady=(22, 14))

        bf = tk.Frame(win, bg="#1a1a1a")
        bf.pack()

        def _yes():
            win.destroy()
            if self._restart_cb:
                self._restart_cb()

        def _no():
            win.destroy()

        tk.Button(bf, text="Reset", font=("Consolas", 9),
                  bg="#5a1500", fg="#ff5533", relief="flat", padx=12, pady=4,
                  activebackground="#7a2000", activeforeground="#ff7755",
                  cursor="hand2", command=_yes).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=("Consolas", 9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=12, pady=4,
                  activebackground="#555555", activeforeground="white",
                  cursor="hand2", command=_no).pack(side="left")

        win.bind("<Escape>", lambda e: _no())
        win.bind("<Return>",  lambda e: _yes())
        win.wait_window()

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
        sz = self.SQ - 14
        order = [chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN]
        imgs = {}
        for row, is_white in enumerate([True, False]):
            for col, pt in enumerate(order):
                cell = transparent.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
                # Crop to the tight bounding box of non-transparent pixels so that
                # empty cell margins (which vary per piece) don't affect centering.
                bbox = cell.getbbox()
                piece = cell.crop(bbox) if bbox else cell
                # Scale proportionally to fit within sz×sz.
                piece.thumbnail((sz, sz), Image.LANCZOS)
                # Centre on a transparent sz×sz canvas.
                canvas = Image.new('RGBA', (sz, sz), (0, 0, 0, 0))
                ox = (sz - piece.width) // 2
                oy = (sz - piece.height) // 2
                canvas.paste(piece, (ox, oy), piece)
                imgs[(pt, is_white)] = ImageTk.PhotoImage(canvas)
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

        # ── Main content: board column + graph column ──
        content = tk.Frame(r, bg=self.BG_IDLE)
        content.pack(fill="both")

        left = tk.Frame(content, bg=self.BG_IDLE)
        left.pack(side="left")

        right = tk.Frame(content, bg=self.GBG)
        right.pack(side="left", fill="y")

        # ── Player panels ──
        def player_panel(parent, name, symbol):
            f  = tk.Frame(parent, bg=self.BG_IDLE, pady=8, padx=10)
            f.pack(fill="x")
            nm = self._lbl(f, f"{symbol} {name}", ("Consolas", 13, "bold"))
            nm.pack(side="left")
            ev = self._lbl(f, "eval: ---", ("Consolas", 11), fg="#aaaaaa")
            ev.pack(side="right", padx=14)
            tm = self._lbl(f, "--:--.-", ("Consolas", 16, "bold"), fg="#55aaff")
            tm.pack(side="right")
            return f, nm, tm, ev

        self._bf, self._bn, self._bt, self._be = player_panel(left, self.black_name, "♟")

        self._canvas = tk.Canvas(left, width=px, height=px, highlightthickness=0)
        self._canvas.pack()

        self._wf, self._wn, self._wt, self._we = player_panel(left, self.white_name, "♙")

        # Make name labels clickable when idle (game over or not yet started)
        for lbl, color in ((self._bn, chess.BLACK), (self._wn, chess.WHITE)):
            lbl.config(cursor="hand2")
            lbl.bind("<Button-1>", lambda e, c=color: self._on_name_click(c))

        # ── Eval graph ──
        self._gcv = tk.Canvas(right, width=self.GW, height=px,
                              bg=self.GBG, highlightthickness=0)
        self._gcv.pack(fill="both", expand=True)

        # ── Status bar ──
        sf = tk.Frame(r, bg="#1a1a1a", pady=5, padx=10)
        sf.pack(fill="x")
        _init_status = ("Click the player names to select engines"
                        if not self._engines_ready else "Initialising...")
        self._status = self._lbl(sf, _init_status, ("Consolas", 11),
                                 fg="#cccccc", bg="#1a1a1a")
        self._status.pack(side="left")
        self._last_move_lbl = self._lbl(sf, "", ("Consolas", 11, "bold"),
                                        fg="#ffaa00", bg="#1a1a1a")
        self._last_move_lbl.pack(side="right")

        # ── Move list ──
        mf = tk.Frame(r, bg="#111111", pady=4, padx=10)
        mf.pack(fill="x")
        self._moves_lbl = self._lbl(mf, "", ("Consolas", 10), fg="#aaaaaa",
                                    bg="#111111", wraplength=px + self.GW - 20)
        self._moves_lbl.pack(side="left")

        # ── Button bar ──
        bf = tk.Frame(r, bg="#222222", pady=6, padx=10)
        bf.pack(fill="x")

        def _mk_btn(parent, text, font, fg, cmd, padx=(0, 6)):
            b = tk.Button(parent, text=text, font=font, fg=fg,
                          bg="#3a3a3a", activebackground="#555555",
                          activeforeground=fg, relief="flat",
                          padx=10, pady=4, cursor="hand2", command=cmd)
            b.pack(side="left", padx=padx)
            return b

        # Play / Pause
        self._play_btn = tk.Button(
            bf, text="▶", font=("Segoe UI Symbol", 14),
            fg="#88ff44" if self._engines_ready else "#555555",
            bg="#3a3a3a", activebackground="#555555", activeforeground="#88ff44",
            relief="flat", padx=10, pady=4,
            cursor="hand2" if self._engines_ready else "",
            disabledforeground="#444444",
            state="normal" if self._engines_ready else "disabled",
            command=self._on_play_pause,
        )
        self._play_btn.pack(side="left", padx=(0, 4))

        # Stop / Swap-colors
        self._action_btn = tk.Button(
            bf, text="⇆", font=("Segoe UI Symbol", 14),
            fg="#aaaaaa" if self._engines_ready else "#555555",
            bg="#3a3a3a", activebackground="#555555", activeforeground="#cccccc",
            relief="flat", padx=10, pady=4,
            cursor="hand2" if self._engines_ready else "",
            disabledforeground="#444444",
            state="normal" if self._engines_ready else "disabled",
            command=self._on_action_btn,
        )
        self._action_btn.pack(side="left", padx=(0, 4))

        # Settings (time control)
        self._settings_btn = tk.Button(
            bf, text="⚙", font=("Segoe UI Symbol", 14),
            fg="#aaaaaa", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#cccccc", relief="flat", padx=10, pady=4,
            cursor="hand2", command=self._open_settings,
        )
        self._settings_btn.pack(side="left", padx=(0, 12))

        # Log buttons — right side (pack Black first so it lands rightmost)
        self._log_btn_b = tk.Button(
            bf, text=f"☰  {self.black_name[:14]}", font=("Segoe UI Symbol", 10),
            fg="#ffffff", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#ffffff", relief="flat", padx=10, pady=4,
            cursor="hand2", command=lambda: self._open_log_window(self._black_engine),
        )
        self._log_btn_b.pack(side="right", padx=(0, 6))
        self._log_btn_w = tk.Button(
            bf, text=f"☰  {self.white_name[:14]}", font=("Segoe UI Symbol", 10),
            fg="#ffffff", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#ffffff", relief="flat", padx=10, pady=4,
            cursor="hand2", command=lambda: self._open_log_window(self._white_engine),
        )
        self._log_btn_w.pack(side="right", padx=(0, 6))


        self._draw_board(chess.Board())
        self._draw_graph([], [])

    # ── Board drawing ──

    def _draw_board(self, board: chess.Board, last_move: Optional[chess.Move] = None):
        c  = self._canvas
        SQ = self.SQ
        c.delete("all")
        hi = {last_move.from_square, last_move.to_square} if last_move else set()

        for rank in range(8):
            for file in range(8):
                sq    = chess.square(file, rank)
                x1    = file * SQ
                y1    = (7 - rank) * SQ
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

        for i in range(8):
            c.create_text(3, (7 - i) * SQ + SQ // 2, text=str(i + 1),
                          font=("Consolas", 8), fill="#666", anchor="w")
            c.create_text(i * SQ + SQ // 2, 8 * SQ - 2, text="abcdefgh"[i],
                          font=("Consolas", 8), fill="#666", anchor="s")

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
        self._anim_step()

    def _anim_step(self):
        t = min(1.0, (time.monotonic() - self._anim_start) / 0.2)
        ease = t * t * (3 - 2 * t)   # smoothstep
        self._draw_board_anim(self._anim_board, self._anim_move, ease)
        if t < 1.0:
            self.root.after(16, self._anim_step)
        else:
            self._anim_done.set()

    def _draw_board_anim(self, board: chess.Board, move: chess.Move, t: float):
        """Board with the moving piece interpolated at progress t (0→from, 1→to)."""
        c  = self._canvas
        SQ = self.SQ
        c.delete("all")
        hi     = {move.from_square, move.to_square}
        moving = board.piece_at(move.from_square)

        for rank in range(8):
            for file in range(8):
                sq    = chess.square(file, rank)
                x1    = file * SQ
                y1    = (7 - rank) * SQ
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

        for i in range(8):
            c.create_text(3, (7 - i) * SQ + SQ // 2, text=str(i + 1),
                          font=("Consolas", 8), fill="#666", anchor="w")
            c.create_text(i * SQ + SQ // 2, 8 * SQ - 2, text="abcdefgh"[i],
                          font=("Consolas", 8), fill="#666", anchor="s")

        if moving:
            ff = chess.square_file(move.from_square)
            fr = chess.square_rank(move.from_square)
            tf = chess.square_file(move.to_square)
            tr = chess.square_rank(move.to_square)
            px = (ff + 0.5) * SQ + t * ((tf - ff) * SQ)
            py = (7 - fr + 0.5) * SQ + t * ((fr - tr) * SQ)
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

    # ── Eval graph ──

    def _draw_graph(self, w_evals: list, b_evals: list):
        """
        w_evals, b_evals: parallel lists normalised to White's POV.
        Y-axis is inverted: top = black ahead (negative), bottom = white ahead (positive).
        Scale auto-fits to current data, always spanning at least ±1, capped at ±10.
        """
        c  = self._gcv
        W  = self.GW
        H  = max(c.winfo_height(), self.SQ * 8)
        c.delete("all")

        ML, MR, MT, MB = 38, 10, 22, 52
        pw = W - ML - MR
        ph = H - MT - MB

        c.create_rectangle(0, 0, W, H, fill=self.GBG, outline="")
        c.create_text(W // 2, 10, text="Evaluation", fill="#777",
                      font=("Consolas", 8, "bold"), anchor="center")

        # Auto-scale: always include ±1, outer limit ±10
        all_vals = [v for v in w_evals + b_evals if v is not None]
        if all_vals:
            y_max = min(10.0, max(1.0,  max(all_vals)))
            y_min = max(-10.0, min(-1.0, min(all_vals)))
        else:
            y_min, y_max = -1.0, 1.0

        # Inverted y: negative (black ahead) → screen top, positive (white ahead) → screen bottom
        def gy(v):
            v = max(y_min, min(y_max, float(v)))
            return MT + ph * (v - y_min) / (y_max - y_min)

        n = max(len(w_evals), len(b_evals), 1)

        def gx(i):
            return ML + (pw * i / (n - 1) if n > 1 else pw // 2)

        # Gridlines + Y labels (only those within the current auto-scale range)
        CANDS = [(v, ("0" if v == 0 else f"+{v}" if v > 0 else f"−{abs(v)}"))
                 for v in range(-10, 11)]
        for val, label in CANDS:
            if not (y_min <= val <= y_max):
                continue
            y   = gy(val)
            col = "#666" if val != 0 else "#888"
            lw  = 2 if val == 0 else 1
            c.create_line(ML, y, ML + pw, y, fill=col, width=lw,
                          dash=() if val == 0 else (2, 3))
            c.create_text(ML - 4, y, text=label, fill="#888",
                          font=("Consolas", 7), anchor="e")

        # Plot border
        c.create_rectangle(ML, MT, ML + pw, MT + ph, outline="#444", width=1)

        # Lines
        for evals, color in [(w_evals, self.C_W), (b_evals, self.C_B)]:
            pts = [(i, v) for i, v in enumerate(evals) if v is not None]
            if len(pts) >= 2:
                coords = []
                for i, v in pts:
                    coords.extend([gx(i), gy(v)])
                c.create_line(*coords, fill=color, width=2, smooth=False)

        # Legend — Black on top, White below (mirrors board orientation)
        for y_off, color, label in [
            (MT + ph + 16, self.C_B, f"B: {self.black_name[:12]}"),
            (MT + ph + 36, self.C_W, f"W: {self.white_name[:12]}"),
        ]:
            c.create_line(ML, y_off, ML + 16, y_off, fill=color, width=2)
            c.create_text(ML + 20, y_off, text=label, fill=color,
                          font=("Consolas", 9), anchor="w")

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
        if not (self._game_over or not self._game_started):
            return
        self._open_engine_picker(color)

    def _open_engine_picker(self, color: chess.Color):
        tk   = self._tk
        side = "White" if color == chess.WHITE else "Black"

        # Build list: engines/ folder first, then history extras
        engines_dir = os.path.join(APP_DIR, "engines")
        items: list[tuple[str, str, str]] = []  # (display_name, abs_path, source)
        seen: set[str] = set()

        if os.path.isdir(engines_dir):
            for fname in sorted(os.listdir(engines_dir)):
                fpath = os.path.join(engines_dir, fname)
                if os.path.isfile(fpath) and not fname.startswith("."):
                    apath = os.path.abspath(fpath)
                    name  = os.path.splitext(fname)[0]
                    items.append((name, apath, "default"))
                    seen.add(apath)

        for hpath in _load_engine_history():
            apath = os.path.abspath(hpath)
            if apath not in seen and os.path.isfile(apath):
                name = os.path.splitext(os.path.basename(apath))[0]
                items.append((f"{name}  ↩", apath, "recent"))
                seen.add(apath)

        # Dialog
        BG  = "#1a1a1a"
        FG  = "#cccccc"
        win = tk.Toplevel(self.root)
        win.title(f"Select Engine — {side}")
        win.configure(bg=BG)
        win.geometry("380x300")
        win.resizable(False, True)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text=f"Engine for {side}:", font=("Consolas", 10, "bold"),
                 fg="#aaaaaa", bg=BG, padx=12).pack(anchor="w", pady=(8, 4))

        lf = tk.Frame(win, bg=BG, padx=12)
        lf.pack(fill="both", expand=True)
        vsb = tk.Scrollbar(lf)
        vsb.pack(side="right", fill="y")
        lb = tk.Listbox(
            lf, font=("Consolas", 10), bg="#111111", fg=FG,
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
            return tk.Button(parent, text=text, font=("Consolas", 9),
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
        win.geometry("960x480" if has_options else "740x480")

        # Toolbar
        tb = tk.Frame(win, bg="#1a1a1a", pady=4, padx=8)
        tb.pack(fill="x")
        tk.Label(tb, text=engine.name, font=("Consolas", 11, "bold"),
                 fg="#dddddd", bg="#1a1a1a").pack(side="left")
        tk.Button(tb, text="Clear", font=("Consolas", 9),
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
            font=("Consolas", 9), wrap="word",
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

            panel = tk.Frame(content, bg=OBG, width=212)
            panel.pack(side="left", fill="y")
            panel.pack_propagate(False)

            tk.Label(panel, text="Engine Options", font=("Consolas", 10, "bold"),
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
                        row, text=opt_name, font=("Consolas", 9),
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
                        font=("Consolas", 9), fg=OFG, bg=OBG,
                        selectcolor="#333333", activebackground=OBG,
                        activeforeground=OFG, relief="flat",
                        cursor="hand2", command=_on_check,
                    )
                    cb.pack(anchor="w")
                    _bind_mousewheel(cb)

                elif opt_type == "spin":
                    tk.Label(row, text=opt_name, font=("Consolas", 9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    spin_var = tk.StringVar(value=opt["value"] or "")
                    from_val = int(opt["min"]) if opt["min"] is not None else 0
                    to_val   = int(opt["max"]) if opt["max"] is not None else 9999
                    spin = tk.Spinbox(
                        row, textvariable=spin_var,
                        from_=from_val, to=to_val,
                        font=("Consolas", 9), width=8,
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
                    tk.Label(row, text=opt_name, font=("Consolas", 9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    combo_var = tk.StringVar(value=opt["value"] or "")
                    choices = opt["var"] or [opt["value"]]

                    def _on_combo(val, n=opt_name):
                        engine.options[n]["value"] = val
                        engine.set_option(n, val)

                    om = tk.OptionMenu(row, combo_var, *choices, command=_on_combo)
                    om.config(font=("Consolas", 9), bg="#2a2a2a", fg=OFG,
                              activebackground="#3a3a3a", activeforeground=OFG,
                              relief="flat", padx=4, cursor="hand2")
                    om["menu"].config(bg="#2a2a2a", fg=OFG, activebackground="#444",
                                      activeforeground=OFG, font=("Consolas", 9))
                    om.pack(anchor="w")
                    _bind_mousewheel(om)

                else:  # string
                    tk.Label(row, text=opt_name, font=("Consolas", 9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    str_var = tk.StringVar(value=opt["value"] or "")
                    entry = tk.Entry(
                        row, textvariable=str_var,
                        font=("Consolas", 9), width=20,
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

    # ── Play / Pause ──

    def _on_play_pause(self):
        if self._game_over:
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
        self._draw_graph([], [])
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
        if "eval_history" in d:
            self._draw_graph(*d["eval_history"])
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
    ):
        """total_time/increment apply to White (and to Black unless
        black_time/black_increment are given)."""
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
        # Per-ply eval lists, normalised to White's POV, parallel + same length
        self._w_evals: list[Optional[float]] = []
        self._b_evals: list[Optional[float]] = []

    def _gui(self, **kw):
        if self.gui:
            self.gui.update(**kw)

    def _push_eval(self, score: Optional[str], turn: chess.Color):
        v = parse_eval(score)
        # Normalise to White's POV: negate when it's Black's score
        norm = (v if turn == chess.WHITE else (-v if v is not None else None))
        if turn == chess.WHITE:
            self._w_evals.append(norm)
            self._b_evals.append(None)
        else:
            self._w_evals.append(None)
            self._b_evals.append(norm)
        self._gui(eval_history=(list(self._w_evals), list(self._b_evals)))

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
        pgn_game.headers.update({
            "White":  w,
            "Black":  b,
            "Date":   datetime.date.today().strftime("%Y.%m.%d"),
            "Result": "*",
        })
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
                print(f"\n  *** {engine.name} FLAGGED (ran out of time) ***")
                print(f"  Result: {result} — {opp.name} wins on time\n")
                self._gui(status=f"{engine.name} ran out of time! {opp.name} wins — {result}", game_over=True)
                break

            # ── No move ──
            if move_uci is None:
                result = "0-1" if turn == chess.WHITE else "1-0"
                print(f"\n  *** {engine.name} returned no move ***")
                print(f"  Result: {result} — {opp.name} wins\n")
                self._gui(status=f"{engine.name} failed to respond! {opp.name} wins — {result}", game_over=True)
                break

            # ── Parse & validate ──
            try:
                move = chess.Move.from_uci(move_uci)
            except ValueError:
                result = "0-1" if turn == chess.WHITE else "1-0"
                print(f"\n  *** {engine.name} returned malformed move: {move_uci!r} ***")
                print(f"  Result: {result} — {opp.name} wins\n")
                self._gui(status=f"{engine.name} malformed move! {opp.name} wins — {result}", game_over=True)
                break

            if move not in self.board.legal_moves:
                result = "0-1" if turn == chess.WHITE else "1-0"
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
            self._push_eval(score, turn)

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
            verdict = (f"{self.white.name} wins" if result == "1-0" else
                       f"{self.black.name} wins" if result == "0-1" else "Draw")
            print(f"\n{sep}")
            print(f"  Game over: {reason}")
            print(f"  Result:    {result}  —  {verdict}")
            print(sep + "\n")
            self._gui(status=f"Game over — {reason} — {result} ({verdict})", active=None, game_over=True)

        pgn_game.headers["Result"] = result
        return result, pgn_game


# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────

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

    # Command-line engines take precedence; otherwise restore last session's
    last = _load_last_engines()
    for key, cli_path in (("white", args.white_engine), ("black", args.black_engine)):
        if cli_path:
            state[key] = _start_engine(cli_path, required=True)
        elif last.get(key) and os.path.isfile(last[key]):
            state[key] = _start_engine(last[key], required=False)

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

        t = threading.Thread(target=run, daemon=True)
        state["thread"] = t
        t.start()

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
        if gui.game_in_progress:
            gui.update(status="Time control saved — applies from the next game")
        elif state["game_launched"]:
            restart()   # not started yet / game over: reload clocks now

    gui.set_settings_callbacks(lambda: state["tc"], apply_settings)
    gui.set_restart_callback(restart)
    gui.set_swap_callback(swap_colors)
    gui.set_change_engine_callback(change_engine)

    if engines_ready:
        start_game()
    # else: user must select engines via the GUI before ▶ is enabled

    gui.root.mainloop()

    state["stop"].set()
    if state["thread"]:
        state["thread"].join(timeout=5)

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
