# Chess Player

A Windows desktop app that plays games and tournaments between UCI chess engines, for engine development and testing. It shows the board, clocks and per-move graphs, adjudicates every result, and saves each game as PGN.

- [Build and install](#build-and-install)
- [Run from source](#run-from-source)
- [Using the app](#using-the-app)
  - [Tournaments](#tournaments)
- [Where files are kept](#where-files-are-kept)
- [Command line](#command-line)
- [Project layout](#project-layout)

## Build and install

You need:

- Python 3.9+ on PATH
- Inno Setup 6: `winget install JRSoftware.InnoSetup`

From the repository folder, run:

```bat
build -Install
```

This builds the app and installs it for your user (no admin rights needed) in `%LOCALAPPDATA%\Programs\ChessPlayer\`, with a Start menu entry *Chess Player*. If the app is running, the installer closes it first. A new install upgrades the old one in place. Uninstall it from *Settings → Apps*.

| Command | What it does |
|---|---|
| `build` | Build `dist\ChessPlayer\` and the installer `dist\installer\ChessPlayerSetup-<version>.exe` |
| `build -Install` | …and install it |
| `build -Bump minor -Install` | Raise the version first (`major` / `minor` / `patch`), then build and install |
| `build -NoInstaller` | Only build `dist\ChessPlayer\ChessPlayer.exe` (Inno Setup not needed) |
| `build -Clean` | Rebuild from scratch, if a build behaves strangely |

The first build creates a virtualenv in `.venv-build` and installs the dependencies into it, so it takes longer. Later builds take about half a minute. The version is `__version__` in `chess_player/__init__.py`.

## Run from source

```bat
pip install -r requirements.txt
python -m chess_player
```

## Using the app

**Engines.** Click a player's name above or below the board to pick its engine. Engines are not included with the app. Any UCI engine `.exe` works. Recently used engines are offered again, and the last White and Black engines are loaded on the next start.

**Playing.** ▶ starts or pauses the game and ■ stops it. When no game is running, that button becomes ⇆ and swaps colors. The panel of the side to move is highlighted. **☰** opens an engine's log window, with its UCI traffic and options.

**Graphs.** The panel next to the board shows *Eval*, *Depth* and/or *Nodes* per move. Toggle them at the top of the panel.

**Settings (⚙).** Time and increment, the same for both engines or different per engine, and the font size.

**Results.** A game ends on checkmate, stalemate, threefold repetition, the 50-move rule, insufficient material, a lost clock, an illegal move or no move. An engine that crashes or hangs is restarted after the game.

### Tournaments

Click **♛ Tournament**:

- **Engines:** select 2 or more (*Add all* / *Remove all*, or *Add engine…* from disk).
- **Format:** *Round robin* (number of rounds, each engine meets every other once per round) or *Swiss* (matches per engine; in each round, engines with similar scores meet, and the bottom boards play first).
- **Time / increment** for all games, and **Pause before each match**.

A tournament starts paused with the first pairing on the board. Press ▶ to play. The panel on the right shows the standings (points, then Sonneborn-Berger) and the results.

While the tournament is paused, **♛ Tournament** still works. Before the first game you can change everything, including the engines. After that you can change the number of rounds / matches and the auto-pause.

A finished tournament can be extended: open the dialog with the same engines and format, raise the rounds / matches, and keep *Extend the finished tournament* checked.

The running tournament is saved after every game. If the app is closed or crashes, it resumes on the next start from the last completed game. It is also kept up to date in `tournamentsutosave_<date>-<time>.json`, which is never deleted, so you can always continue it with **Continue from file…**. **■ → Stop and save…** saves it to a file of your choice.

## Where files are kept

The installed app keeps its data in `%APPDATA%\chess_player\`. When run from source, it uses the repository folder.

- `games\`: every finished game as PGN, with each engine's eval and clock per move
- `tournaments\`: saved tournaments
- `chess_player.log`: output of the installed app
- `.*.json`: settings, engine history and tournament state

## Command line

The installed `chess_player_cli.exe` (or `python -m chess_player`) takes options to preselect engines and time control, or to run a single game without a window:

```bat
chess_player_cli --w engine1.exe --b engine2.exe --time 120 --increment 2
chess_player_cli --w engine1.exe --b engine2.exe --nogui --pgn game.pgn
```

Run `chess_player_cli --help` for all options.

## Project layout

| Path | Contents |
|---|---|
| `chess_player/` | The app: `engine.py` (UCI), `game.py`, `tournament.py`, `settings.py`, `app.py` (GUI wiring), `cli.py` |
| `chess_player/gui/` | Main window (`window.py`) and its parts: board, graph, dialogs, tournament panel |
| `chess_player/assets/` | Piece images and the app icon |
| `tests/` | Unit tests: `python -m unittest` |
| `packaging/` | PyInstaller spec and Inno Setup script. The installer's `AppId` must never change, or upgrades install side by side |
| `build.cmd`, `build.ps1` | Build script |
