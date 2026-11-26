"""UCI engine process wrapper."""

import chess
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple


# Don't flash a console window for each engine process on Windows
_POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


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
        self._start_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def ensure_started(self):
        """Start the engine process unless it is already running. Engines are
        only started when a game needs them. Raises on failure."""
        with self._start_lock:
            if not self.running:
                self.start()

    def start(self):
        if self.process is not None:   # restarting after quit / a crash
            try:
                self.process.kill()
            except Exception:
                pass
        old_options = self.options
        self.options = {}
        self._q = queue.Queue()
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
        threading.Thread(target=self._reader, args=(self.process, self._q),
                         daemon=True).start()
        uci_start = len(self.log)
        self._send("uci")
        if not self._wait("uciok", timeout=15):
            self._kill()
            raise RuntimeError(f"{self.name}: did not respond to 'uci'")
        self._parse_uci_options(uci_start)
        # Options the user changed before a restart are set again
        for name, opt in old_options.items():
            if (opt["type"] != "button" and opt["value"] is not None
                    and opt["value"] != opt["default"] and name in self.options):
                self.set_option(name, opt["value"])
                self.options[name]["value"] = opt["value"]
        self._send("isready")
        if not self._wait("readyok", timeout=15):
            self._kill()
            raise RuntimeError(f"{self.name}: did not respond to 'isready'")

    def _kill(self):
        try:
            self.process.kill()
        except Exception:
            pass

    def _reader(self, process, q):
        for line in process.stdout:
            line = line.rstrip()
            self.log.append(("<", line))
            q.put(line)

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

    def _parse_uci_options(self, start: int = 0):
        for direction, line in self.log[start:]:
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
        if not self.running:
            return
        try:
            self._send("quit")
            self.process.wait(timeout=3)
        except Exception:
            if self.process:
                self.process.kill()


# ──────────────────────────────────────────────────────────────────────────────
# Crashed / hung engines
# ──────────────────────────────────────────────────────────────────────────────

# An engine that loses on time (or without a move) after thinking at least this
# long on its last move has probably crashed or hung, and is restarted.
HUNG_AFTER_S = 10.0

def is_unresponsive(engine: UCIEngine, elapsed: float) -> bool:
    exited = engine.process is not None and engine.process.poll() is not None
    return exited or elapsed >= HUNG_AFTER_S

def restart_engine(old: UCIEngine) -> UCIEngine:
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
