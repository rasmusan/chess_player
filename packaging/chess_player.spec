# PyInstaller spec — builds two executables sharing one folder:
#   ChessPlayer.exe      windowed GUI app (what the Start menu shortcut runs)
#   chess_player_cli.exe console app, for --nogui runs and seeing console output
#
# Run via build.ps1 (or: pyinstaller packaging/chess_player.spec from repo root).

import os

ROOT   = os.path.abspath(os.path.join(SPECPATH, ".."))
ASSETS = os.path.join(ROOT, "chess_player", "assets")
ICON   = os.path.join(ASSETS, "chess_player.ico")

a = Analysis(
    [os.path.join(ROOT, "chess_player", "__main__.py")],
    pathex=[ROOT],
    datas=[(ASSETS, "assets")],
    # Not used by the app. Leaving them out makes the build faster and the app
    # smaller. ssl (OpenSSL, ~6 MB) is only an optional import of stdlib
    # modules; Pillow only needs PNG support (AVIF alone is ~7.5 MB).
    excludes=["numpy", "matplotlib", "scipy", "pandas", "IPython", "pytest",
              "ssl", "_ssl",
              "PIL._avif", "PIL.AvifImagePlugin", "PIL._webp", "PIL.WebPImagePlugin",
              "PIL._imagingcms", "PIL.ImageCms", "PIL._imagingft"],
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
