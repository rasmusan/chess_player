"""The main window. Feature areas live in mixins in the sibling modules."""

import chess
import os
import queue
import sys
import threading
import time
from typing import Optional
from .. import __version__
from ..formatting import fmt_time
from .board import BoardMixin
from .engine_picker import EnginePickerMixin
from ..game import GRAPH_METRICS
from .graph import GraphMixin
from .log_window import LogWindowMixin
from .settings_dialog import SettingsDialogMixin
from .tournament_dialog import TournamentDialogMixin
from .tournament_panel import TournamentPanelMixin
from ..settings import load_gui_settings, save_gui_settings
from ..paths import RESOURCE_DIR


class ChessGUI(SettingsDialogMixin, BoardMixin, GraphMixin, EnginePickerMixin,
               LogWindowMixin, TournamentPanelMixin, TournamentDialogMixin):
    SQ      = 68          # board square size (px)
    GW      = 250         # eval graph panel width (px)
    LIGHT   = "#F0D9B5"
    DARK    = "#B58863"
    HI      = "#CDD16E"   # last-move highlight
    BG_IDLE = "#2b2b2b"
    BG_ACTV = "#3a5a00"
    GBG     = "#1a1a1a"   # graph background
    GCARD   = "#222222"   # background of each graph card
    C_W     = "#ffffff"   # white-engine line
    C_B     = "#888888"   # black-engine line


    def __init__(self, white_name: str, black_name: str, engines_ready: bool = True):
        import tkinter as tk
        self._tk = tk
        self.root = tk.Tk()
        self.root.title(f"Chess Player {__version__}")
        ico = os.path.join(RESOURCE_DIR, "chess_player.ico")
        if sys.platform == "win32" and os.path.exists(ico):
            try:
                self.root.iconbitmap(default=ico)
            except tk.TclError:
                pass
        self.root.resizable(True, True)
        self.root.configure(bg=self.BG_IDLE)
        # Named fonts, so a font-size change in ⚙ settings updates every widget
        self._fonts: dict = {}
        try:
            fs = int(load_gui_settings().get("font_size", self.FONT_DEFAULT))
        except (TypeError, ValueError):
            fs = self.FONT_DEFAULT
        self._font_size = max(self.FONT_MIN, min(self.FONT_MAX, fs))
        # Board geometry; recomputed whenever the board canvas is resized
        self._sq = self.SQ
        self._bx = self._by = 0
        self._cur_board = chess.Board()
        self._cur_last: Optional[chess.Move] = None
        self._animating = False
        self.white_name = white_name
        self.black_name = black_name
        self._uq: queue.Queue = queue.Queue()
        self._restart_cb = None
        self._white_engine = None
        self._black_engine = None
        self._resume_event = threading.Event()   # cleared = paused/waiting
        self._game_over    = False
        self._think_start: Optional[float] = None
        self._think_color: Optional[chess.Color] = None
        self._think_budget: float = 0.0
        self._game_started     = False
        self._change_engine_cb = None
        self._swap_cb = None
        self._settings_get_cb   = None
        self._settings_apply_cb = None
        self._engines_ready = engines_ready
        # Tournament mode
        self._tournament_running  = False
        # All games played; waits for ✓ (end) or an extension
        self._tournament_done     = False
        self._tournament_start_cb = None
        self._tournament_stop_cb  = None
        self._tournament_info_cb = self._tournament_edit_cb = None
        self._tournament_restart_cb = None
        self._last_tournament_snap: Optional[dict] = None
        # State of the last finished tournament, which can be extended
        self._finished_tournament: Optional[dict] = None
        # Graph panel: which metrics to show, and the last data drawn
        sel = [m for m in load_gui_settings().get("graph_metrics", ["eval"])
               if m in GRAPH_METRICS]
        self._graph_sel: set = set(sel or ["eval"])
        self._graph_hist: dict = {}
        self._piece_src  = self._load_pieces()   # PIL images at source size
        self._piece_cache: dict = {}             # pixel size -> {(pt, white): PhotoImage}
        self._piece_imgs = self._pieces_at(self._sq)
        self._anim_done  = threading.Event()
        self._build()
        self._update_min_size()
        self._poll()

    # ── Fonts ──

    FONT_DEFAULT, FONT_MIN, FONT_MAX = 10, 7, 24

    @property
    def _scale(self) -> float:
        return self._font_size / self.FONT_DEFAULT

    def _font(self, size: int, weight: str = "normal", family: str = "Consolas"):
        """Shared named font. `size` is the size at the default font setting."""
        key = (family, size, weight)
        f = self._fonts.get(key)
        if f is None:
            import tkinter.font as tkfont
            f = tkfont.Font(root=self.root, family=family, weight=weight,
                            size=max(6, round(size * self._scale)))
            self._fonts[key] = f
        return f

    def set_font_size(self, size: int):
        size = max(self.FONT_MIN, min(self.FONT_MAX, int(size)))
        if size == self._font_size:
            return
        self._font_size = size
        save_gui_settings({"font_size": size})
        for (_, base, _), f in self._fonts.items():
            f.configure(size=max(6, round(base * self._scale)))
        self._set_name_label(self._wn, self.white_name, True)
        self._set_name_label(self._bn, self.black_name, False)
        self._update_min_size()
        self._layout()
        self._draw_graph()
        if self._last_tournament_snap:
            self._render_tournament(self._last_tournament_snap)

    # ── Window layout ──
    # The content area (board column, graph, tournament panel) is laid out by
    # hand with place(): the board takes the largest square that fits, the
    # graph gets the remaining width, and the tournament panel a fixed width.

    def _gw_min(self) -> int:
        return round(self.GW * self._scale)

    def _tw(self) -> int:
        return round(self.TW * self._scale) if self._tpanel_shown else 0

    def _update_min_size(self):
        """Set the content's natural size (board at its default size) and use
        the resulting window size as the minimum: the window can only grow."""
        self.root.update_idletasks()
        ph = self._bf.winfo_reqheight() + self._wf.winfo_reqheight()
        self._content.config(width=8 * self.SQ + self._gw_min() + self._tw(),
                             height=8 * self.SQ + ph)
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())

    def _layout(self, event=None):
        c = self._content
        W, H = c.winfo_width(), c.winfo_height()
        if W < 50 or H < 50:
            return
        ph = self._bf.winfo_reqheight() + self._wf.winfo_reqheight()
        tw = self._tw()
        bs = max(8 * 16, min(H - ph, W - tw - self._gw_min()))
        gw = max(1, W - tw - bs)
        top = max(0, (H - ph - bs) // 2)
        self._left.place(x=0, y=top, width=bs, height=bs + ph)
        self._right.place(x=bs, y=0, width=gw, height=H)
        if tw:
            self._tpanel.place(x=bs + gw, y=0, width=tw, height=H)
        else:
            self._tpanel.place_forget()

    def _on_board_resize(self, event):
        sq = max(16, min(event.width, event.height) // 8)
        self._bx = (event.width - 8 * sq) // 2
        self._by = (event.height - 8 * sq) // 2
        if sq != self._sq:
            self._sq = sq
            self._piece_imgs = self._pieces_at(sq)
        if not self._animating:
            self._draw_board(self._cur_board, self._cur_last)

    def set_restart_callback(self, cb):
        self._restart_cb = cb

    def set_engines(self, white_engine, black_engine):
        self._white_engine = white_engine
        self._black_engine = black_engine

    def set_change_engine_callback(self, cb):
        self._change_engine_cb = cb

    def set_swap_callback(self, cb):
        self._swap_cb = cb

    def set_tournament_callbacks(self, start_cb, stop_cb, info_cb=None,
                                 edit_cb=None, restart_cb=None):
        """start_cb(cfg dict) starts a tournament; stop_cb() aborts it.
        While paused: info_cb() describes the running tournament, edit_cb(ch)
        changes its rounds / auto-pause, restart_cb(cfg) replaces it (only
        before its first game)."""
        self._tournament_start_cb   = start_cb
        self._tournament_stop_cb    = stop_cb
        self._tournament_info_cb    = info_cb
        self._tournament_edit_cb    = edit_cb
        self._tournament_restart_cb = restart_cb

    @property
    def tournament_running(self) -> bool:
        return self._tournament_running

    def set_settings_callbacks(self, get_cb, apply_cb):
        """get_cb() -> time-control dict; apply_cb(dict) stores/applies it."""
        self._settings_get_cb   = get_cb
        self._settings_apply_cb = apply_cb

    @property
    def game_in_progress(self) -> bool:
        return self._game_started and not self._game_over

    # ── Action button (Stop / Swap-colors) ──

    def _update_action_btn(self):
        if self._tournament_running and self._tournament_done:
            self._action_btn.config(text="✓", fg="#88ff44", activeforeground="#aaff66")
        elif self._tournament_running or (self._game_started and not self._game_over):
            self._action_btn.config(text="■", fg="#ff4444", activeforeground="#ff6666")
        else:
            self._action_btn.config(text="⇆", fg="#aaaaaa", activeforeground="#cccccc")

    def _on_action_btn(self):
        if self._tournament_running:
            self._confirm_end_tournament()
            return
        if self._game_started and not self._game_over:
            if self._resume_event.is_set():
                self._resume_event.clear()
                self._play_btn.config(text="▶", fg="#88ff44")
            self._open_stop_confirm()
        else:
            if self._swap_cb:
                self._swap_cb()

    def _confirm_end_tournament(self):
        """✓ / ■ / Stop in the Tournament dialog: end the tournament after
        asking whether to save it to a file (Save… / Don't save / Cancel)."""
        if not self._tournament_running:
            return
        if self._resume_event.is_set():
            self._resume_event.clear()
            self._play_btn.config(text="▶", fg="#88ff44")
            self._update_tourn_btn()
        stop = self._tournament_stop_cb or (lambda save: None)
        if self._tournament_done:
            text = "The tournament is finished.\nSave it to a file?"
        else:
            text = "Stop the tournament?\nSave it to a file to continue it later."
        self._open_stop_confirm(text, "Don't save", lambda: stop(False),
                                title="End tournament",
                                alt=("Save…", lambda: stop(True)))

    def _open_stop_confirm(self, text="Stop and reset the current game?",
                           yes_text="Reset", on_yes=None, title="Stop game", alt=None):
        """alt: optional (label, callback) for a second confirm button."""
        on_yes = on_yes or self._restart_cb
        tk  = self._tk
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg="#1a1a1a", padx=24, pady=18)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text=text, justify="center",
                 font=self._font(10), fg="#cccccc", bg="#1a1a1a").pack(pady=(4, 14))

        bf = tk.Frame(win, bg="#1a1a1a")
        bf.pack()

        def _yes():
            win.destroy()
            if on_yes:
                on_yes()

        def _no():
            win.destroy()

        if alt:
            def _alt():
                win.destroy()
                alt[1]()
            tk.Button(bf, text=alt[0], font=self._font(9),
                      bg="#2a4a00", fg="#88ff44", relief="flat", padx=12, pady=4,
                      activebackground="#3a6a00", activeforeground="#aaff66",
                      cursor="hand2", command=_alt).pack(side="left", padx=(0, 8))

        tk.Button(bf, text=yes_text, font=self._font(9),
                  bg="#5a1500", fg="#ff5533", relief="flat", padx=12, pady=4,
                  activebackground="#7a2000", activeforeground="#ff7755",
                  cursor="hand2", command=_yes).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=12, pady=4,
                  activebackground="#555555", activeforeground="white",
                  cursor="hand2", command=_no).pack(side="left")

        win.bind("<Escape>", lambda e: _no())
        win.bind("<Return>",  lambda e: _yes())
        self._center_dialog(win)
        win.wait_window()

    def _center_dialog(self, win):
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()  - win.winfo_reqwidth())  // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # ── Layout ──

    def _lbl(self, parent, text, font, fg="white", bg=None, anchor="w", **kw):
        return self._tk.Label(parent, text=text, font=font, fg=fg,
                              bg=bg or self.BG_IDLE, anchor=anchor, **kw)

    def _build(self):
        tk = self._tk
        r  = self.root
        SQ = self.SQ
        px = SQ * 8   # board pixel size

        # ── Main content: board column + graph column (+ tournament panel) ──
        # Packed last (below) so that the bars at the bottom keep their height
        # and the content area absorbs any change of window size.
        content = tk.Frame(r, bg=self.BG_IDLE, width=px + self.GW, height=px + 100)
        self._content = content
        content.bind("<Configure>", self._layout)

        left = tk.Frame(content, bg=self.BG_IDLE)
        self._left = left

        right = tk.Frame(content, bg=self.GBG)
        self._right = right

        # Tournament panel: placed to the right of the graph only in tournament mode
        self._build_tournament_panel(content)

        # ── Player panels ──
        def player_panel(parent):
            f  = tk.Frame(parent, bg=self.BG_IDLE, pady=8, padx=10)
            f.pack(fill="x")
            nm = self._lbl(f, "", self._font(13, "bold"))
            nm.pack(side="left")
            tm = self._lbl(f, "--:--.-", self._font(16, "bold"), fg="#55aaff")
            tm.pack(side="right", padx=(0, 14))
            # Net material captured from the opponent, left of the clock
            cp = self._lbl(f, "", self._font(12, family="Segoe UI Symbol"), fg="#bbbbbb")
            cp.pack(side="right", padx=(0, 10))
            return f, nm, tm, cp

        self._bf, self._bn, self._bt, self._bc = player_panel(left)

        self._canvas = tk.Canvas(left, width=px, height=px, highlightthickness=0,
                                 bg=self.BG_IDLE)
        self._wf, self._wn, self._wt, self._wc = player_panel(left)
        self._set_name_label(self._wn, self.white_name, True)
        self._set_name_label(self._bn, self.black_name, False)
        # White's panel is packed before the canvas so it keeps its height
        self._wf.pack_configure(side="bottom")
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>", self._on_board_resize)

        # Make name labels clickable when idle (game over or not yet started)
        for lbl, color in ((self._bn, chess.BLACK), (self._wn, chess.WHITE)):
            lbl.config(cursor="hand2")
            lbl.bind("<Button-1>", lambda e, c=color: self._on_name_click(c))

        # ── Graph panel: metric selector + canvas ──
        self._build_graph_selector(right)
        self._gcv = tk.Canvas(right, width=self.GW, height=px - 30,
                              bg=self.GBG, highlightthickness=0)
        self._gcv.pack(fill="both", expand=True)
        self._gcv.bind("<Configure>", lambda e: self._draw_graph())

        # ── Bottom bars, packed bottom-up: buttons, move list, status ──
        bf = tk.Frame(r, bg="#222222", pady=6, padx=10)
        bf.pack(side="bottom", fill="x")
        mf = tk.Frame(r, bg="#111111", pady=4, padx=10)
        mf.pack(side="bottom", fill="x")
        sf = tk.Frame(r, bg="#1a1a1a", pady=5, padx=10)
        sf.pack(side="bottom", fill="x")
        content.pack(side="top", fill="both", expand=True)

        # ── Status bar ──
        _init_status = ("Click the player names to select engines"
                        if not self._engines_ready else "Initialising...")
        self._status = self._lbl(sf, _init_status, self._font(11),
                                 fg="#cccccc", bg="#1a1a1a")
        self._status.pack(side="left")
        self._last_move_lbl = self._lbl(sf, "", self._font(11, "bold"),
                                        fg="#ffaa00", bg="#1a1a1a")
        self._last_move_lbl.pack(side="right")

        # ── Move list (wraps at the window width) ──
        self._moves_lbl = self._lbl(mf, "", self._font(10), fg="#aaaaaa",
                                    bg="#111111", wraplength=px + self.GW - 20,
                                    justify="left")
        self._moves_lbl.pack(side="left")
        mf.bind("<Configure>", lambda e: self._moves_lbl.config(
            wraplength=max(100, e.width - 20)))

        # ── Button bar ──
        # All buttons fill the bar's height, so text buttons line up with the
        # larger symbol buttons.

        # Play / Pause
        self._play_btn = tk.Button(
            bf, text="▶", font=self._font(14, family="Segoe UI Symbol"),
            fg="#88ff44" if self._engines_ready else "#555555",
            bg="#3a3a3a", activebackground="#555555", activeforeground="#88ff44",
            relief="flat", padx=10, pady=4,
            cursor="hand2" if self._engines_ready else "",
            disabledforeground="#444444",
            state="normal" if self._engines_ready else "disabled",
            command=self._on_play_pause,
        )
        self._play_btn.pack(side="left", fill="y", padx=(0, 4))

        # Stop / Swap-colors
        self._action_btn = tk.Button(
            bf, text="⇆", font=self._font(14, family="Segoe UI Symbol"),
            fg="#aaaaaa" if self._engines_ready else "#555555",
            bg="#3a3a3a", activebackground="#555555", activeforeground="#cccccc",
            relief="flat", padx=10, pady=4,
            cursor="hand2" if self._engines_ready else "",
            disabledforeground="#444444",
            state="normal" if self._engines_ready else "disabled",
            command=self._on_action_btn,
        )
        self._action_btn.pack(side="left", fill="y", padx=(0, 4))

        # Settings (time control)
        self._settings_btn = tk.Button(
            bf, text="⚙", font=self._font(14, family="Segoe UI Symbol"),
            fg="#aaaaaa", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#cccccc", relief="flat", padx=10, pady=4,
            cursor="hand2", command=self._open_settings,
        )
        self._settings_btn.pack(side="left", fill="y", padx=(0, 4))

        # Tournament setup
        self._tourn_btn = tk.Button(
            bf, text="♛ Tournament", font=self._font(10, family="Segoe UI Symbol"),
            fg="#ffcc44", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#ffdd66", disabledforeground="#555555",
            relief="flat", padx=10, pady=4, cursor="hand2",
            command=self._open_tournament_dialog,
        )
        self._tourn_btn.pack(side="left", fill="y", padx=(0, 12))

        # Log buttons — right side (pack Black first so it lands rightmost)
        self._log_btn_b = tk.Button(
            bf, text=f"☰  {self.black_name[:14]}", font=self._font(10, family="Segoe UI Symbol"),
            fg="#ffffff", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#ffffff", relief="flat", padx=10, pady=4,
            cursor="hand2", command=lambda: self._open_log_window(self._black_engine),
        )
        self._log_btn_b.pack(side="right", fill="y", padx=(0, 6))
        self._log_btn_w = tk.Button(
            bf, text=f"☰  {self.white_name[:14]}", font=self._font(10, family="Segoe UI Symbol"),
            fg="#ffffff", bg="#3a3a3a", activebackground="#555555",
            activeforeground="#ffffff", relief="flat", padx=10, pady=4,
            cursor="hand2", command=lambda: self._open_log_window(self._white_engine),
        )
        self._log_btn_w.pack(side="right", fill="y", padx=(0, 6))


        self._draw_board(chess.Board())
        self._draw_graph({})

    # ── Play / Pause ──

    def _on_play_pause(self):
        if self._game_over and not self._tournament_running:
            self._on_restart()
            return
        if self._resume_event.is_set():
            self._resume_event.clear()
            self._play_btn.config(text="▶", fg="#88ff44")
            self._update_tourn_btn()
        else:
            self._resume_event.set()
            self._play_btn.config(text="⏸", fg="#ffaa44")
            self._update_tourn_btn()
            if not self._game_started:
                self._game_started = True
                self._bn.config(cursor="")
                self._wn.config(cursor="")
                self._update_action_btn()

    def show_paused(self):
        """Show ▶ after the tournament paused itself. Main thread."""
        if self._tournament_running and not self._resume_event.is_set():
            self._play_btn.config(text="▶", fg="#88ff44")
        self._update_tourn_btn()

    def _update_tourn_btn(self):
        """♛ Tournament works when no tournament runs, or while it is paused."""
        ok = not self._tournament_running or not self._resume_event.is_set()
        self._tourn_btn.config(state="normal" if ok else "disabled",
                               cursor="hand2" if ok else "")

    def _set_game_over_btn(self):
        if self._tournament_running:
            # The tournament moves on to the next game by itself; ▶/⏸ keeps
            # pausing and resuming the tournament.
            self._game_over   = True
            self._think_start = None
            return
        self._game_over = True
        self._resume_event.clear()
        self._play_btn.config(text="↺", fg="#44aaff")
        self._bn.config(cursor="hand2")
        self._wn.config(cursor="hand2")
        self._update_action_btn()

    def reset_play_button(self):
        self._game_over = False
        self._resume_event.clear()
        self._play_btn.config(text="▶", fg="#88ff44")
        self._update_action_btn()

    # ── Restart ──

    def _on_restart(self):
        if self._restart_cb:
            self._restart_cb()

    def reset(self):
        """Reset GUI state for a new game. Call from main thread."""
        self._think_start  = None
        self._game_over    = False
        self._game_started = False
        self._bn.config(cursor="hand2")
        self._wn.config(cursor="hand2")
        self._draw_board(chess.Board())
        self._draw_graph({})
        self._status.config(text="Restarting...")
        self._last_move_lbl.config(text="")
        self._moves_lbl.config(text="")
        for frame, nm, tm in [
            (self._wf, self._wn, self._wt),
            (self._bf, self._bn, self._bt),
        ]:
            for w in (frame, *frame.winfo_children()):
                w.config(bg=self.BG_IDLE)
            tm.config(text="--:--.-")
        self._update_action_btn()

    # ── Thread-safe update ──

    def update(self, **kw):
        self._uq.put(kw)

    def _poll(self):
        try:
            while True:
                self._apply(self._uq.get_nowait())
        except queue.Empty:
            pass
        # Live countdown for the thinking player
        if self._think_start is not None:
            remaining = max(0.0, self._think_budget - (time.monotonic() - self._think_start))
            lbl = self._wt if self._think_color == chess.WHITE else self._bt
            lbl.config(text=fmt_time(remaining))
        self.root.after(40, self._poll)

    def _apply(self, d: dict):
        if "animate_move" in d:
            self._start_animation(*d["animate_move"])
        if "board" in d:
            self._draw_board(d["board"], d.get("last_move"))
        if "think_start" in d:
            self._think_start  = d["think_start"]
            self._think_color  = d.get("think_color")
            self._think_budget = d.get("think_budget", 0.0)
        if "white_time" in d:
            self._wt.config(text=fmt_time(d["white_time"]))
        if "black_time" in d:
            self._bt.config(text=fmt_time(d["black_time"]))
        if "status" in d:
            self._status.config(text=d["status"])
        if "move" in d:
            self._last_move_lbl.config(text=d["move"])
        if "moves_text" in d:
            self._moves_lbl.config(text=d["moves_text"])
        if "graph_history" in d:
            self._draw_graph(d["graph_history"])
        if "tournament" in d:
            self._render_tournament(d["tournament"])
        if "game_over" in d:
            self._set_game_over_btn()
        if "reset_play_btn" in d:
            self.reset_play_button()
        if "active" in d:
            active = d["active"]
            for frame, nm, tm, color in [
                (self._wf, self._wn, self._wt, chess.WHITE),
                (self._bf, self._bn, self._bt, chess.BLACK),
            ]:
                bg = self.BG_ACTV if active == color else self.BG_IDLE
                for w in (frame, *frame.winfo_children()):
                    w.config(bg=bg)
        if "engine_changed" in d:
            self._update_engine_name(*d["engine_changed"])
        if "play_btn_enabled" in d:
            if d["play_btn_enabled"]:
                self._play_btn.config(state="normal", fg="#88ff44", cursor="hand2")
                self._action_btn.config(state="normal", fg="#aaaaaa", cursor="hand2")
            else:
                self._play_btn.config(state="disabled", fg="#555555", cursor="")
                self._action_btn.config(state="disabled", fg="#555555", cursor="")
        if "call" in d:
            d["call"]()
