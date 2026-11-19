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

Unless `--nogui` is passed, a window opens showing:

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
