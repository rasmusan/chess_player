"""Plays one game between two engines, and saves game records."""

import chess
import chess.pgn
import datetime
import os
import threading
import time
from typing import TYPE_CHECKING, Optional, Tuple
from . import __version__
from .engine import UCIEngine, is_unresponsive
from .formatting import board_ascii, fmt_time, parse_eval
from .paths import DATA_DIR

if TYPE_CHECKING:
    from .gui import ChessGUI

# Metrics recorded per move for the graph panel, in display order (top to bottom)
GRAPH_METRICS = ("eval", "depth", "nodes")


# ──────────────────────────────────────────────────────────────────────────────
# Game records — every finished game is saved as a PGN file in games/
# ──────────────────────────────────────────────────────────────────────────────

GAMES_DIR = os.path.join(DATA_DIR, "games")

def _safe_filename(s: str, maxlen: int = 40) -> str:
    out = "".join(ch if ch.isalnum() or ch in "._-+" else "_" for ch in s).strip("._")
    return out[:maxlen] or "engine"

def save_game_record(pgn_game) -> Optional[str]:
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
# Game runner
# ──────────────────────────────────────────────────────────────────────────────

class Game:
    def __init__(
        self,
        white: UCIEngine,
        black: UCIEngine,
        total_time: float,
        increment: float,
        gui: Optional["ChessGUI"] = None,
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
        # Engine that probably crashed or hung (see is_unresponsive); the
        # caller restarts it after the game
        self.crashed_engine: Optional[UCIEngine] = None
        # Engine that could not be started; the game was not played
        self.failed_engine: Optional[UCIEngine] = None
        self.start_error: str = ""

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

    def _start_engines(self) -> bool:
        """Start engines that are not running yet (engines are only loaded
        when a game starts). False if one failed or the game was stopped."""
        for eng in (self.white, self.black):
            if self.stop_event.is_set():
                return False
            if eng.running:
                continue
            self._gui(status=f"Starting {eng.name}…")
            print(f"Starting {eng.name}...")
            try:
                eng.ensure_started()
                print(f"  {eng.name}: OK")
            except Exception as e:
                print(f"  {eng.name}: failed to start ({e})")
                self.failed_engine = eng
                self.start_error = str(e)
                return False
        return not self.stop_event.is_set()

    def _wait_for_play(self) -> bool:
        """Block while waiting for ▶. False if the game was stopped."""
        if self.resume_event is not None:
            while not self.resume_event.wait(timeout=0.1):
                if self.stop_event.is_set():
                    return False
        return not self.stop_event.is_set()

    def run(self) -> Tuple[str, chess.pgn.Game]:
        w, b = self.white.name, self.black.name
        waiting = self.resume_event is not None and not self.resume_event.is_set()
        self._gui(
            board=chess.Board(),
            white_time=self.wtime,
            black_time=self.btime,
            status="Press ▶ to start" if waiting else f"WHITE: {w}   BLACK: {b}",
            move="",
            active=None,
        )
        if not self._wait_for_play() or not self._start_engines():
            if self.failed_engine is not None:
                self._gui(status=f"{self.failed_engine.name} failed to start: {self.start_error}",
                          game_over=True)
            return "*", chess.pgn.Game()
        if waiting:
            self._gui(status=f"WHITE: {w}   BLACK: {b}")

        self.white.new_game()
        self.black.new_game()

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

            move_uci, score = engine.get_move(
                self.board, wms, bms, winc_ms, binc_ms,
                hard_limit, stop_event=self.stop_event,
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
            )

            # ── Flag ──
            flagged = self.wtime if turn == chess.WHITE else self.btime
            if flagged < 0:
                result = "0-1" if turn == chess.WHITE else "1-0"
                self.reason = "time forfeit"
                if is_unresponsive(engine, elapsed):
                    self.crashed_engine = engine
                print(f"\n  *** {engine.name} FLAGGED (ran out of time) ***")
                print(f"  Result: {result} — {opp.name} wins on time\n")
                self._gui(status=f"{engine.name} ran out of time! {opp.name} wins — {result}", game_over=True)
                break

            # ── No move ──
            if move_uci is None:
                result = "0-1" if turn == chess.WHITE else "1-0"
                self.reason = "no move"
                if is_unresponsive(engine, elapsed):
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
                self.record_path = save_game_record(pgn_game)
                if self.record_path:
                    print(f"  Game saved -> {self.record_path}")
        return result, pgn_game


def print_summary(result: str, white: UCIEngine, black: UCIEngine):
    print("RESULT:", result)
    if result == "1-0":
        print(f"WINNER: {white.name} (White)")
    elif result == "0-1":
        print(f"WINNER: {black.name} (Black)")
    else:
        print("RESULT: Draw")
