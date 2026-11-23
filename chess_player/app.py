"""GUI mode: wires the main window to games and tournaments."""

import chess
import datetime
import os
import sys
import threading
from typing import Optional
from .engine import UCIEngine, restart_engine
from .formatting import fmt_duration
from .game import Game, print_summary
from .gui.splash import SplashWindow
from .gui.window import ChessGUI
from .settings import default_time_control, load_last_engines, load_time_settings, save_engine_history, save_last_engines, save_time_settings, write_json_atomic
from .tournament import Tournament, CURRENT_TOURNAMENT_FILE, FINISHED_TOURNAMENT_FILE, TOURNAMENT_STATE_VERSION, load_tournament_file, remove_current_tournament


def run_gui(args):
    # ── GUI mode ──
    # Validate any paths given on the command line
    if args.white_engine and not os.path.isfile(args.white_engine):
        sys.exit(f"Error: engine not found (--w): {args.white_engine}")
    if args.black_engine and not os.path.isfile(args.black_engine):
        sys.exit(f"Error: engine not found (--b): {args.black_engine}")

    # Time control: saved GUI settings, overridden by --time / --increment
    tc = load_time_settings() or default_time_control()
    if args.time is not None or args.increment is not None:
        tc = default_time_control(
            args.time if args.time is not None else tc["white_time"],
            args.increment if args.increment is not None else tc["white_inc"],
        )

    state: dict = {
        "game": None, "thread": None, "stop": threading.Event(),
        "result": "*", "pgn": None,
        "white": None, "black": None,
        "game_launched": False,
        "tc": tc,
        "tournament": None,   # {"stop", "engines", "thread"} while one runs
    }

    def _start_engine(path, required: bool):
        """Start an engine. Engines from the command line are required (exit on
        failure); engines restored from last session are skipped if they fail."""
        name = os.path.splitext(os.path.basename(path))[0]
        eng = UCIEngine(path, name)
        print(f"Starting {name}...")
        try:
            eng.start()
            print(f"  {name}: OK")
        except Exception as e:
            if required:
                sys.exit(f"Engine startup failed: {e}")
            print(f"  {name}: failed to start ({e}) — not restored")
            try:
                eng.quit()
            except Exception:
                pass
            return None
        return eng

    # Command-line engines take precedence; otherwise restore last session's.
    # This can take a while, so a splash window shows that the app is starting.
    splash = SplashWindow.create()

    def _startup():
        last = load_last_engines()
        for key, cli_path in (("white", args.white_engine), ("black", args.black_engine)):
            path = cli_path or (last.get(key) if last.get(key) and os.path.isfile(last[key]) else None)
            if not path:
                continue
            if splash:
                splash.status = f"Starting {os.path.splitext(os.path.basename(path))[0]}…"
            state[key] = _start_engine(path, required=bool(cli_path))
        if splash:
            splash.status = "Loading…"
        try:   # preload the graphics libraries the main window needs
            import PIL.Image, PIL.ImageTk, PIL.ImageDraw, PIL.ImageChops  # noqa: F401
        except ImportError:
            pass

    if splash:
        try:
            splash.run(_startup)
        finally:
            splash.close()
    else:
        _startup()

    engines_ready = state["white"] is not None and state["black"] is not None
    white_name = state["white"].name if state["white"] else "—"
    black_name = state["black"].name if state["black"] else "—"

    gui = ChessGUI(white_name, black_name, engines_ready=engines_ready)
    gui.set_engines(state["white"], state["black"])
    if args.white_engine or args.black_engine:
        # Remember command-line choices (don't overwrite on a failed restore)
        save_last_engines(state["white"], state["black"])

    def start_game():
        state["game_launched"] = True
        stop = threading.Event()
        state["stop"] = stop
        tc = state["tc"]
        game = Game(state["white"], state["black"], tc["white_time"], tc["white_inc"],
                    gui, stop, resume_event=gui._resume_event,
                    black_time=tc["black_time"], black_increment=tc["black_inc"])
        state["game"] = game

        def run():
            state["result"], state["pgn"] = game.run()
            if game.crashed_engine is not None:
                replace_crashed_engine(game.crashed_engine)

        t = threading.Thread(target=run, daemon=True)
        state["thread"] = t
        t.start()

    def replace_crashed_engine(old: UCIEngine):
        """Game thread: restart an engine that stopped responding."""
        key = ("white" if state["white"] is old else
               "black" if state["black"] is old else None)
        if key is None:
            return   # already replaced (engine changed meanwhile)
        print(f"  {old.name} stopped responding — restarting it")
        gui.update(status=f"{old.name} stopped responding — restarting it…")
        try:
            new = restart_engine(old)
        except Exception as e:
            print(f"  {old.name}: restart failed ({e})")
            gui.update(status=f"{old.name} stopped responding and could not be restarted: {e}")
            return
        if state[key] is not old:   # replaced by the user meanwhile
            new.quit()
            return
        state[key] = new

        def _done():
            gui.set_engines(state["white"], state["black"])
            gui.update(status=f"{old.name} stopped responding and was restarted")
        gui.update(call=_done)

    def restart():
        state["stop"].set()
        gui.update(reset_play_btn=True)
        gui.reset()
        gui.root.after(300, start_game)

    def change_engine(color: chess.Color, new_path: str):
        name = os.path.splitext(os.path.basename(new_path))[0]
        gui.update(status=f"Starting {name}…")

        def _do_start():
            new_eng = UCIEngine(new_path, name)
            try:
                new_eng.start()
            except RuntimeError as e:
                gui.update(status=f"Engine failed to start: {e}")
                return
            save_engine_history(new_path)
            if color == chess.WHITE:
                old = state["white"]
                state["white"] = new_eng
            else:
                old = state["black"]
                state["black"] = new_eng
            if old:
                old.quit()
            gui.set_engines(state["white"], state["black"])
            save_last_engines(state["white"], state["black"])

            both_ready = state["white"] is not None and state["black"] is not None
            if both_ready and not state["game_launched"]:
                # Both engines now selected for the first time — enable ▶ and launch
                gui.update(engine_changed=(color, name), play_btn_enabled=True, call=start_game)
            else:
                if state["white"] is None:
                    hint = f"{name} ready — click ♙ White name to select its engine"
                elif state["black"] is None:
                    hint = f"{name} ready — click ♟ Black name to select its engine"
                else:
                    hint = f"{name} ready"
                gui.update(engine_changed=(color, name), status=hint)

        threading.Thread(target=_do_start, daemon=True).start()

    def swap_colors():
        state["white"], state["black"] = state["black"], state["white"]
        w_name = state["white"].name if state["white"] else "—"
        b_name = state["black"].name if state["black"] else "—"
        gui.set_engines(state["white"], state["black"])
        save_last_engines(state["white"], state["black"])
        gui.update(engine_changed=(chess.WHITE, w_name))
        gui.update(engine_changed=(chess.BLACK, b_name))
        restart()

    def apply_settings(new_tc: dict):
        state["tc"] = new_tc
        save_time_settings(new_tc)
        if gui.tournament_running:
            gui.update(status="Time control saved — applies to normal games")
        elif gui.game_in_progress:
            gui.update(status="Time control saved — applies from the next game")
        elif state["game_launched"]:
            restart()   # not started yet / game over: reload clocks now

    # ── Tournament mode ──
    # Tournament games use their own engine processes, so the White/Black
    # engines of normal mode stay loaded and come back when it ends.

    def start_tournament(cfg: dict):
        """cfg: a new tournament from the setup dialog ({"engines", "format",
        "count", "time", "inc"}), or {"resume": state} to continue a saved one.
        A tournament always starts paused: the first game waits for ▶."""
        if state["tournament"] is not None:
            return
        tour = {"stop": threading.Event(), "engines": [], "thread": None, "save": False}
        state["tournament"] = tour
        gui._finished_tournament = None
        try:
            os.remove(FINISHED_TOURNAMENT_FILE)
        except OSError:
            pass
        state["stop"].set()          # stop any normal game
        gui.enter_tournament_mode(paused=True)
        t = threading.Thread(target=run_tournament, args=(cfg, tour), daemon=True)
        tour["thread"] = t
        t.start()

    def stop_tournament(save: bool = False):
        tour = state["tournament"]
        if tour:
            tour["save"] = save
            tour["stop"].set()
            gui.update(status="Stopping tournament…")

    def tournament_info() -> Optional[dict]:
        """Main thread: what the running tournament allows to change, for
        the Tournament dialog while paused. None while engines are starting."""
        tour = state["tournament"]
        ctl = tour and tour.get("ctl")
        if not ctl:
            return None
        with ctl["lock"]:
            t = ctl["tourney"]
            fresh = not t.games and not ctl["game_started"]
            min_c = 1 if fresh else t.min_count(ctl["round"])
            st = ctl["make_state"]()
            return {"fresh": fresh, "format": t.fmt, "count": t.count,
                    "min_count": min_c, "auto_pause": tour["auto_pause"],
                    "engines": [e["path"] for e in st["engines"]],
                    "time": st["time"], "inc": st["inc"]}

    def edit_tournament(ch: dict):
        """Main thread: change rounds / matches and auto-pause of the running
        tournament ({"count", "auto_pause"})."""
        tour = state["tournament"]
        ctl = tour and tour.get("ctl")
        if not ctl:
            return
        tour["auto_pause"] = bool(ch.get("auto_pause", tour["auto_pause"]))
        with ctl["lock"]:
            t = ctl["tourney"]
            if "count" in ch and int(ch["count"]) != t.count:
                t.set_count(max(int(ch["count"]), t.min_count(ctl["round"])))
        ctl["autosave"]()
        gui._render_tournament(ctl["snap"]())

    def restart_tournament(cfg: dict):
        """Main thread: replace a tournament that has not played a game yet
        with a new setup (cfg as for start_tournament)."""
        tour = state["tournament"]
        if not tour:
            start_tournament(cfg)
            return
        tour["restart_cfg"] = cfg
        tour["stop"].set()
        gui.update(status="Restarting tournament…")

    def run_tournament(cfg: dict, tour: dict):
        """Tournament thread. The state (engines, time control, pairings and
        results so far) is autosaved to CURRENT_TOURNAMENT_FILE whenever it
        changes, so an app that is closed mid-tournament resumes it on the
        next start; the game that was in progress is replayed."""
        stop = tour["stop"]
        if state["thread"]:
            state["thread"].join(timeout=5)   # let the normal game wind down
        resume = cfg.get("resume")
        if resume:
            eng_cfg = [(e["name"], e["path"]) for e in resume["engines"]]
            t_time, t_inc = resume["time"], resume["inc"]
            try:   # e.g. continued from a file: make sure a crash keeps it
                write_json_atomic(CURRENT_TOURNAMENT_FILE, resume)
            except OSError as e:
                print(f"  Could not autosave tournament: {e}")
        else:
            eng_cfg = cfg["engines"]
            t_time, t_inc = cfg["time"], cfg["inc"]
        auto_pause = bool((resume or cfg).get("auto_pause", False))
        tc_text = f"{fmt_duration(t_time)} + {t_inc:g}s"

        engines, failed = [], []
        for name, path in eng_cfg:
            if stop.is_set():
                break
            gui.update(status=f"Tournament: starting {name}…")
            eng = UCIEngine(path, name)
            try:
                eng.start()
                engines.append(eng)
                tour["engines"].append(eng)
                save_engine_history(path)
            except Exception as e:
                print(f"  {name}: failed to start ({e}) — left out of the tournament")
                gui.update(status=f"{name} failed to start — left out")
                failed.append(name)
                try:
                    eng.quit()
                except Exception:
                    pass
                if resume:
                    break   # a resumed tournament needs all of its engines

        if len(engines) < 2 or stop.is_set() or (resume and failed):
            for eng in engines:
                eng.quit()
            if stop.is_set():
                msg = "Tournament stopped"
            elif resume:
                msg = f"Could not resume the tournament: {failed[0]} failed to start"
            else:
                msg = "Tournament aborted: fewer than 2 engines started"
            ask = (f"{msg}.\n\nSave the tournament to a file so you can continue it later?"
                   if resume and not stop.is_set() else None)
            gui.update(call=lambda: finish_tournament(None, msg, final_state=resume, ask=ask))
            return

        if resume:
            tourney = Tournament.from_dict(resume["tournament"])
        else:
            tourney = Tournament([e.name for e in engines], cfg["format"], cfg["count"])
        fmt_name = "Round robin" if tourney.fmt == "rr" else "Swiss"

        tour["auto_pause"] = auto_pause
        lock = threading.RLock()   # guards tourney against edits from the GUI

        def make_state() -> dict:
            with lock:
                return {"kind": "chess_player tournament",
                        "version": TOURNAMENT_STATE_VERSION,
                        "saved": datetime.datetime.now().isoformat(timespec="seconds"),
                        "engines": [{"name": e.name, "path": os.path.abspath(e.path)}
                                    for e in engines],
                        "time": t_time, "inc": t_inc, "auto_pause": tour["auto_pause"],
                        "tournament": tourney.to_dict()}

        def autosave():
            try:
                write_json_atomic(CURRENT_TOURNAMENT_FILE, make_state())
            except OSError as e:
                print(f"  Could not autosave tournament: {e}")

        def snap(status: str = "running") -> dict:
            with lock:
                return tourney.snapshot(status, tc_text)

        # For editing from the GUI while paused (see edit_tournament)
        ctl = {"lock": lock, "tourney": tourney, "round": 0, "game_started": False,
               "make_state": make_state, "autosave": autosave, "snap": snap}
        tour["ctl"] = ctl

        autosave()
        gui.update(tournament=snap())

        def wait_resume() -> bool:
            """Block while paused. False if the tournament was stopped."""
            if not gui._resume_event.is_set() and not stop.is_set():
                gui.update(status="Tournament paused — press ▶ to play the next game")
            while not gui._resume_event.wait(timeout=0.1):
                if stop.is_set():
                    return False
            return not stop.is_set()

        rnd = 0
        while not stop.is_set():
            with lock:
                # The number of rounds may change while paused
                if rnd >= tourney.total_rounds:
                    break
                ctl["round"] = rnd
                pairs, bye = tourney.pairings(rnd)
                if bye is not None and tourney.fmt == "swiss" and not tourney.has_bye(rnd):
                    tourney.record_bye(rnd, bye)
            autosave()   # fixes this round's pairings
            if tourney.fmt == "swiss":
                # Play the bottom boards first, so the leaders meet last
                pairs = pairs[::-1]
            for wi, bi in pairs:
                if tourney.has_game(rnd, wi, bi):
                    continue   # played before the tournament was resumed
                we, be = engines[wi], engines[bi]
                tourney.current = (rnd, wi, bi)
                ready = threading.Event()

                def _prep(we=we, be=be, ready=ready):
                    gui.prepare_tournament_game(we, be, t_time, t_time)
                    ready.set()

                # Show the next pairing right away, also while paused
                gui.update(call=_prep, tournament=snap())
                ready.wait(timeout=5)
                if tour["auto_pause"] and gui._resume_event.is_set():
                    gui._resume_event.clear()
                    gui.update(call=gui.show_paused)
                if not wait_resume():
                    tourney.current = None
                    break
                ctl["game_started"] = True
                print(f"\n=== Tournament round {rnd + 1}, game {len(tourney.games) + 1}"
                      f"/{tourney.total_games}: {we.name} vs {be.name} ===")
                game = Game(we, be, t_time, t_inc, gui, stop,
                            resume_event=gui._resume_event,
                            event=f"{fmt_name} tournament", round_=str(rnd + 1))
                result, _pgn = game.run()
                tourney.current = None
                crashed = game.crashed_engine
                if crashed is not None and not stop.is_set():
                    # Restart it so it can play its remaining games
                    print(f"  {crashed.name} stopped responding — restarting it")
                    gui.update(status=f"{crashed.name} stopped responding — restarting it…")
                    try:
                        new = restart_engine(crashed)
                        engines[engines.index(crashed)] = new
                        tour["engines"][tour["engines"].index(crashed)] = new
                        print(f"  {crashed.name}: restarted")
                    except Exception as e:
                        print(f"  {crashed.name}: restart failed ({e})")
                        gui.update(status=f"{crashed.name} could not be restarted: {e}")
                if stop.is_set() or result == "*":
                    break
                with lock:
                    tourney.record(rnd, wi, bi, result, game.reason)
                autosave()
                gui.update(tournament=snap())
                # Short pause so the final position and result can be seen
                if stop.wait(timeout=2.5):
                    break
            rnd += 1

        finished = not stop.is_set()
        for eng in engines:
            eng.quit()
        final_snap = snap("finished" if finished else "stopped")
        if finished:
            winners = [r["name"] for r in final_snap["rows"] if r["rank"] == 1]
            msg = f"Tournament finished — winner: {', '.join(winners)}"
            print(f"\n{msg}")
            for r in final_snap["rows"]:
                print(f"  {r['rank']:>2}. {r['name']:<24} {r['points']:>5g} pts"
                      f"   SB {r['sb']:.2f}   (+{r['wins']} ={r['draws']} -{r['losses']})")
        else:
            msg = "Tournament stopped"
        final = None if finished else make_state()
        if finished:
            done_state = make_state()

            def _keep():   # so the dialog can offer to extend it
                gui._finished_tournament = done_state
                try:
                    write_json_atomic(FINISHED_TOURNAMENT_FILE, done_state)
                except OSError as e:
                    print(f"  Could not save finished tournament: {e}")
            gui.update(call=_keep)
        gui.update(call=lambda: finish_tournament(final_snap, msg, final_state=final))

    def finish_tournament(snap: Optional[dict], msg: str,
                          final_state: Optional[dict] = None, ask: Optional[str] = None):
        """Main thread: leave tournament mode and set up a normal game again.
        final_state: state of an unfinished tournament, saved to a file if the
        user asked for that (Stop and save…) or agrees to `ask`."""
        tour = state["tournament"]
        save = bool(tour and tour.get("save"))
        state["tournament"] = None
        gui.exit_tournament_mode()
        if tour and tour.get("restart_cfg"):
            # Set up again before the first game: start the new tournament
            remove_current_tournament()
            start_tournament(tour["restart_cfg"])
            return
        if snap:
            gui._render_tournament(snap)
        else:
            gui._close_tournament_panel()
        w, b = state["white"], state["black"]
        gui.set_engines(w, b)
        gui._update_engine_name(chess.WHITE, w.name if w else "—")
        gui._update_engine_name(chess.BLACK, b.name if b else "—")
        gui.reset()
        # Apply directly (not via the queue) so the next game starts paused
        gui._apply({"play_btn_enabled": bool(w and b)})
        gui.reset_play_button()
        if final_state is not None and (save or ask):
            path = gui.save_tournament_file(final_state, ask=None if save else ask)
            if path:
                msg += f" — saved to {os.path.basename(path)}"
        # The tournament is over (finished, or stopped by the user): don't resume it
        remove_current_tournament()
        if w and b:
            start_game()
            # start_game's first status update says "Press ▶"; show the result instead
            gui.root.after(1500, lambda: gui.update(status=msg))
        else:
            gui.update(status=msg + " — click the player names to select engines")

    gui.set_tournament_callbacks(start_tournament, stop_tournament,
                                 tournament_info, edit_tournament, restart_tournament)
    if os.path.exists(FINISHED_TOURNAMENT_FILE):
        try:
            gui._finished_tournament = load_tournament_file(
                FINISHED_TOURNAMENT_FILE, check_engines=False)
        except ValueError as e:
            print(f"Ignoring finished tournament: {e}")

    gui.set_settings_callbacks(lambda: state["tc"], apply_settings)
    gui.set_restart_callback(restart)
    gui.set_swap_callback(swap_colors)
    gui.set_change_engine_callback(change_engine)

    # A tournament that was running when the app was closed: resume it
    resume_state = None
    if os.path.exists(CURRENT_TOURNAMENT_FILE):
        try:
            resume_state = load_tournament_file(CURRENT_TOURNAMENT_FILE, check_engines=False)
        except ValueError as e:
            print(f"Ignoring saved tournament: {e}")
            try:
                os.replace(CURRENT_TOURNAMENT_FILE, CURRENT_TOURNAMENT_FILE + ".invalid")
            except OSError:
                pass

    if resume_state:
        print("Resuming the tournament from the last session")
        gui.root.after(300, lambda: start_tournament({"resume": resume_state}))
    elif engines_ready:
        start_game()
    # else: user must select engines via the GUI before ▶ is enabled

    gui.root.mainloop()

    state["stop"].set()
    if state["thread"]:
        state["thread"].join(timeout=5)
    tour = state["tournament"]
    if tour:
        tour["stop"].set()
        tour["thread"].join(timeout=5)
        for eng in tour["engines"]:
            eng.quit()

    if state["white"]:
        state["white"].quit()
    if state["black"]:
        state["black"].quit()

    if args.pgn and state["pgn"]:
        with open(args.pgn, "w") as fh:
            print(state["pgn"], file=fh)
        print(f"PGN saved -> {args.pgn}")

    if state["white"] and state["black"]:
        print_summary(state["result"], state["white"], state["black"])
