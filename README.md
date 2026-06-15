# chess_player

A command-line tool that runs a game between two UCI-compatible chess engines, with an optional graphical board window.

Designed for engine development and testing — handles time controls, adjudicates illegal moves and timeouts, detects all draw conditions, and saves games as PGN.

## Requirements

- Python 3.9+
- [python-chess](https://python-chess.readthedocs.io/)

```
pip install chess
```

## Usage

```
python chess_player.py <engine1> <engine2> [options]
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `engine1` | required | Path to first engine (plays White by default) |
| `engine2` | required | Path to second engine (plays Black by default) |
| `--time <sec>` | `60` | Total time per player in seconds |
| `--increment <sec>` | `0` | Time increment added per move (Fischer clock) |
| `--flip` | off | Swap colors: engine2 plays White, engine1 plays Black |
| `--gui` | off | Open graphical board window |
| `--pgn <file>` | none | Save the completed game to a PGN file |

### Examples

```bash
# 1-minute game, no increment
python chess_player.py engines/stockfish.exe engines/mybot.exe

# 2-minute + 2s increment, engine2 plays White, save PGN
python chess_player.py mybot.exe stockfish.exe --time 120 --increment 2 --flip --pgn result.pgn

# Same but with the GUI board open
python chess_player.py mybot.exe stockfish.exe --time 60 --gui
```

## Console output

Every move is printed with its SAN notation, evaluation (if provided by the engine), and time used. The board is printed as ASCII after each move.

```
White (rubberduck5.1-duration) — 00:05.0 remaining
    1. e3        [+0.80]  (0.01s)
  a b c d e f g h
8 r n b q k b n r
...
```

## GUI window

When `--gui` is passed, a window opens showing:

- **Chess board** with the last move highlighted
- **Player panels** (top = Black, bottom = White) with engine name, clock, and live evaluation
- The active player's panel is highlighted while they are thinking
- A running move list along the bottom

## Result and adjudication

The game ends and a winner is declared when:

| Condition | Result |
|---|---|
| Checkmate | Checkmated side loses |
| Stalemate | Draw |
| Threefold repetition (claimed) | Draw |
| 50-move rule (claimed) | Draw |
| Insufficient material | Draw |
| Engine runs out of time | That engine loses |
| Engine plays an illegal move | That engine loses |
| Engine returns no move / times out | That engine loses |

The final result (`1-0`, `0-1`, or `1/2-1/2`) is printed to the console. If `--pgn` is given, it is also written to the PGN file.

## Engine compatibility

Any UCI-compliant engine executable is supported. Engines must respond to the standard `uci` / `isready` / `position` / `go` / `quit` commands. Evaluation is parsed from `info score cp` and `info score mate` lines.
