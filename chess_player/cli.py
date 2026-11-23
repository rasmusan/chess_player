"""Command line: argument parsing and headless (--nogui) games."""

import argparse
import os
import sys
from . import __version__
from .app import run_gui
from .engine import UCIEngine
from .game import Game, print_summary


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="chess_player",
        description="Play a match between two UCI chess engines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m chess_player --w engines/stockfish.exe --b engines/mybot.exe
  python -m chess_player --w sf.exe --b cd.exe --time 120 --increment 2
  python -m chess_player --w sf.exe --b cd.exe --time 30 --nogui --pgn out.pgn
        """,
    )
    parser.add_argument("--w",           dest="white_engine", metavar="PATH", default=None,
                        help="Path to engine playing White")
    parser.add_argument("--b",           dest="black_engine", metavar="PATH", default=None,
                        help="Path to engine playing Black")
    parser.add_argument("--time", "-t",  type=float, default=None,  metavar="SEC",
                        help="Total time per player in seconds (default: 60, or last GUI setting)")
    parser.add_argument("--increment", "-i", type=float, default=None, metavar="SEC",
                        help="Increment per move in seconds (default: 0, or last GUI setting)")
    parser.add_argument("--nogui",       action="store_true", help="Run headless (no window)")
    parser.add_argument("--pgn",         type=str, default=None, metavar="FILE")
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    if args.nogui:
        args.time = 60.0 if args.time is None else args.time
        args.increment = 0.0 if args.increment is None else args.increment
        # Headless: both engines required
        if not args.white_engine or not args.black_engine:
            sys.exit("Error: --w and --b are required in --nogui mode")
        for path, label in [(args.white_engine, "--w"), (args.black_engine, "--b")]:
            if not os.path.isfile(path):
                sys.exit(f"Error: engine not found ({label}): {path}")
        white_name = os.path.splitext(os.path.basename(args.white_engine))[0]
        black_name = os.path.splitext(os.path.basename(args.black_engine))[0]
        white = UCIEngine(args.white_engine, white_name)
        black = UCIEngine(args.black_engine, black_name)
        print("Starting engines...")
        try:
            white.start(); print(f"  {white_name}: OK")
            black.start(); print(f"  {black_name}: OK")
        except RuntimeError as e:
            sys.exit(f"Engine startup failed: {e}")
        result, pgn = Game(white, black, args.time, args.increment).run()
        white.quit(); black.quit()
        if args.pgn and pgn:
            with open(args.pgn, "w") as fh:
                print(pgn, file=fh)
            print(f"PGN saved -> {args.pgn}")
        print_summary(result, white, black)
        return

    run_gui(args)
