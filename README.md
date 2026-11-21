# chess_player

A command-line tool that runs a game between two UCI-compatible chess engines, with an optional graphical board window.

Designed for engine development and testing — handles time controls, adjudicates illegal moves and timeouts, detects all draw conditions, and saves games as PGN.

## Requirements

- Python 3.9+
- [python-chess](https://python-chess.readthedocs.io/), Pillow (piece images), numpy (optional, faster image loading)

```
pip install -r requirements.txt
```

## Usage

```
python chess_player.py [--w ENGINE] [--b ENGINE] [options]
```

The GUI opens by default. Engines not given on the command line can be picked in the window by clicking a player's name.

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--w <path>` | none | Engine playing White |
| `--b <path>` | none | Engine playing Black |
| `--time <sec>`, `-t` | `60` | Total time per player in seconds |
| `--increment <sec>`, `-i` | `0` | Time increment added per move (Fischer clock) |
| `--nogui` | off | Run headless in the console (`--w` and `--b` required) |
| `--pgn <file>` | none | Save the completed game to a PGN file |
| `--version`, `-V` | | Print the version and exit |

### Examples

```bash
# 1-minute game in the GUI
python chess_player.py --w engines/stockfish.exe --b engines/mybot.exe

# 2-minute + 2s increment
python chess_player.py --w sf.exe --b mybot.exe --time 120 --increment 2

# Headless, save PGN
python chess_player.py --w sf.exe --b mybot.exe --time 30 --nogui --pgn result.pgn
```

## Windows app: build and install

The app is built with [PyInstaller](https://pyinstaller.org/) and packaged as an installer with [Inno Setup](https://jrsoftware.org/isinfo.php). Everything is driven by `build.cmd`, a wrapper around `build.ps1`.

### One-time setup

- Python 3.9+ on PATH
- Inno Setup 6: `winget install JRSoftware.InnoSetup`

The build script creates its own virtualenv (`.venv-build`) and installs PyInstaller and the dependencies into it. Your normal Python environment is not touched.

### Everyday workflow

```bat
build -Bump patch -Install
```

This bumps the version, builds the app and the installer, and silently installs the new version over the old one. If the app is running, the installer closes it first.

| Command | What it does |
|---|---|
| `build` | Build `dist\ChessPlayer\` and `dist\installer\ChessPlayerSetup-<version>.exe` |
| `build -Install` | …and install it |
| `build -Bump patch` / `minor` / `major` | Increment the version first (e.g. 1.0.0 → 1.0.1) |
| `build -NoInstaller` | Only run PyInstaller (Inno Setup not needed); run `dist\ChessPlayer\ChessPlayer.exe` directly |

From PowerShell you can also call `.\build.ps1` with the same arguments. Running it directly may require `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### Version number

The version lives in one place: `__version__` in `chess_player.py`. `-Bump` edits that line. You can also edit it by hand. The version is shown in the window title, by `--version`, in the installer file name, and in Windows' *Installed apps* list.

### What gets installed

- **Location:** `%LOCALAPPDATA%\Programs\ChessPlayer\`. It installs per-user, so no admin rights are needed.
- **Shortcuts:** Start menu entry *Chess Player*, plus an optional desktop shortcut.
- **`ChessPlayer.exe`:** the GUI app with no console window. Output and errors go to `%APPDATA%\chess_player\chess_player.log`, and fatal errors are also shown in a dialog.
- **`chess_player_cli.exe`:** the same program as a console app. Use it for `--nogui` runs or to see the move-by-move console output.
- **Engines are not included.** Pick them in the app. Recently used engines are remembered in `%APPDATA%\chess_player\.engine_history.json`. The last White and Black engines are saved in `.last_engines.json` in the same folder and reloaded on the next start. Engines given with `--w` or `--b` override them.
- **Upgrades and uninstall:** a new install replaces the previous version in place. Uninstall it from *Settings → Apps*.

### Build files

| File | Purpose |
|---|---|
| `build.cmd` / `build.ps1` | Build script |
| `packaging/chess_player.spec` | PyInstaller config: builds both exes into one folder |
| `packaging/installer.iss` | Inno Setup script. Its `AppId` GUID must never change, or upgrades will install side by side |
| `packaging/make_icon.py` | Generates `packaging/chess_player.ico` from `pieces.png`. Delete the `.ico` to regenerate it |

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

Unless `--nogui` is passed, a small splash window shows progress while the engines start, then the main window opens showing:

- **Chess board** with the last move highlighted
- **Player panels** (top = Black, bottom = White) with engine name, clock, and live evaluation
- The active player's panel is highlighted while they are thinking
- A running move list along the bottom
- A **graph panel** next to the board. Use the *Eval*, *Depth* and *Nodes* toggles at its top to choose what it shows. Selected graphs are stacked, each in its own box. The choice is remembered in `.gui_settings.json`.
  - *Eval*: each engine's evaluation, from `info score cp` / `info score mate`
  - *Depth*: the search depth each engine reached per move, from `info depth`
  - *Nodes*: nodes searched per move, from `info nodes`, on a log scale

  If an engine never reports a value, its line is missing from that graph.

The window can be resized and maximized (not made smaller than its starting size). The board grows to the largest square that fits, and the graph takes the remaining width.

Click **⚙** to set the time control and the **font size** (7–24, default 10). The font size applies to the whole window at once and is remembered in `.gui_settings.json`.

## Tournament mode

Click **♛ Tournament** to set up a tournament:

- **Engines:** select 2 or more from the list. *Add engine…* adds one from disk.
- **Format:**
  - *Round robin*: set the number of **rounds**. In each round every engine plays every other engine once. Colors are reversed in every second round.
  - *Swiss*: set the number of **matches per engine**. Each round pairs engines with equal or similar scores that have not met yet, and balances colors. With an odd number of engines, one engine sits out each round and gets a bye worth 1 point.
- **Time / increment:** the time control for all tournament games. It is separate from the ⚙ setting for normal games.
- **Continue from file…:** continue a tournament saved earlier (see below).

While the tournament runs, a panel to the right of the graph shows the standings and the most recent results, newest first. The two engines in the current game are shown in green, with a white pawn ♙ or black pawn ♟ next to their names. A tournament always starts paused: press ▶ to play the first game (or the next game of a continued tournament). After that, games follow each other automatically. Engine names too long for the table or the results list are cut off with …; hover over them to see the full name. ▶/⏸ pauses and resumes the tournament, and ■ stops it.

**Saving and resuming:**

- The running tournament is saved automatically after every game, in `.current_tournament.json`. If the app is closed during a tournament, it is restored on the next start, paused. The game that was in progress is replayed from the start.
- When you press ■, choose **Stop and save…** to save the tournament to a `.json` file (by default in the `tournaments` folder). Later, open the tournament dialog and click **Continue from file…** to continue it with the same engines, time control, pairings and results.

The standings are sorted by points, then by the **SB** tie-break (Sonneborn-Berger). An engine's SB is the sum of the final scores of the opponents it beat, plus half the scores of the opponents it drew with. Win = 1, draw = ½, loss = 0.

Tournament engines run as separate processes. When the tournament ends, the normal White and Black engines come back. The final standings stay visible until you close the panel with ✕. The last tournament setup is saved in `.tournament_settings.json`.

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

If an engine loses on time or without a move after thinking for 10 seconds or more on its last move, or its process has exited, it has probably crashed or hung. It is then restarted after the game, also in the middle of a tournament, so it can play its next games. Engine options you changed in its log window are applied again.

The final result (`1-0`, `0-1`, or `1/2-1/2`) is printed to the console. If `--pgn` is given, it is also written to the PGN file.

## Game records

Every finished game is saved as a PGN file in the `games` folder of the user data folder (`%APPDATA%\chess_player\games\` for the installed app, the script's folder when run from source). This includes normal games, tournament games and `--nogui` games. Games stopped before they end are not saved.

Each file contains the moves, with each engine's evaluation and remaining clock time per move, and these headers:

| Header | Content |
|---|---|
| `Event`, `Round` | `Engine game`, or e.g. `Round robin tournament` and the round number |
| `White`, `Black`, `Result` | Engines and result |
| `Winner` | Name of the winning engine, or `draw` |
| `Reason` | How the game ended, e.g. `checkmate`, `threefold repetition`, `time forfeit`, `illegal move e2e5` |
| `Termination` | Standard PGN value: `normal`, `time forfeit` or `rules infraction` |
| `TimeControl` | e.g. `60+0` (or `WhiteTimeControl`/`BlackTimeControl` when they differ) |

## Engine compatibility

Any UCI-compliant engine executable is supported. Engines must respond to the standard `uci` / `isready` / `position` / `go` / `quit` commands. Evaluation is parsed from `info score cp` and `info score mate` lines.
