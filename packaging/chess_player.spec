# PyInstaller spec — builds two executables sharing one folder:
#   ChessPlayer.exe      windowed GUI app (what the Start menu shortcut runs)
#   chess_player_cli.exe console app, for --nogui runs and seeing console output
#
# Run via build.ps1 (or: pyinstaller packaging/chess_player.spec from repo root).

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(SPECPATH, "chess_player.ico")

a = Analysis(
    [os.path.join(ROOT, "chess_player.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "pieces.png"), "."),
        (ICON, "."),
    ],
    excludes=["matplotlib", "scipy", "pandas", "IPython", "pytest"],
)
pyz = PYZ(a.pure)

gui_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ChessPlayer",
    console=False,
    icon=ICON,
)
cli_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="chess_player_cli",
    console=True,
    icon=ICON,
)

coll = COLLECT(
    gui_exe, cli_exe,
    a.binaries, a.datas,
    name="ChessPlayer",
)
