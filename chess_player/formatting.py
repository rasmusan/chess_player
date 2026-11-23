"""Text formatting and parsing helpers: durations, clocks, scores, ASCII board."""

import chess
from typing import Optional


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
