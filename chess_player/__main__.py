"""Entry point: python -m chess_player (also the PyInstaller entry script)."""

import sys
from chess_player.cli import main
from chess_player.gui.splash import show_error_dialog
from chess_player.paths import NO_CONSOLE


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if NO_CONSOLE and isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            show_error_dialog(e.code)
            sys.exit(1)
        raise
    except Exception:
        if NO_CONSOLE:
            import traceback
            traceback.print_exc()
            show_error_dialog("Unexpected error:\n\n" + traceback.format_exc(limit=5))
            sys.exit(1)
        raise
