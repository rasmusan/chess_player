"""Tournament panel (standings, results) and tournament mode of the window."""

import chess
from typing import Optional
from ..formatting import fmt_time



class TournamentPanelMixin:
    # ── Tournament panel (right of the graph) ──

    TW   = 380         # tournament panel width (px)
    T_BG = "#161616"

    def _build_tournament_panel(self, parent):
        tk, BG = self._tk, self.T_BG
        p = tk.Frame(parent, bg=BG, width=self.TW)
        p.pack_propagate(False)   # size is set by _layout()
        self._tpanel = p
        self._tpanel_shown = False

        hdr = tk.Frame(p, bg=BG)
        hdr.pack(fill="x", padx=10, pady=(8, 2))
        self._t_title = tk.Label(hdr, text="Tournament", font=self._font(11, "bold"),
                                 fg="white", bg=BG, anchor="w")
        self._t_title.pack(side="left")
        self._t_close = tk.Label(hdr, text="✕", font=self._font(10, family="Segoe UI Symbol"),
                                 fg="#444444", bg=BG)
        self._t_close.pack(side="right")
        self._t_close.bind("<Button-1>", lambda e: self._close_tournament_panel())

        self._t_info = tk.Label(p, text="", font=self._font(9), fg="#aaaaaa",
                                bg=BG, anchor="w", justify="left")
        self._t_info.pack(fill="x", padx=10)

        TXT = dict(font=self._font(9), bg="#111111", fg="#cccccc", relief="flat",
                   wrap="none", padx=6, pady=4, cursor="arrow", highlightthickness=0,
                   state="disabled")
        # The table's box is sized in pixels after each render: lines with a
        # pawn glyph are taller than a Text widget's line-based height allows
        self._t_table_box = tk.Frame(p, bg="#111111", height=60)
        self._t_table_box.pack_propagate(False)
        self._t_table_box.pack(fill="x", padx=10, pady=(6, 0))
        self._t_table = tk.Text(self._t_table_box, height=3, **TXT)
        self._t_table.pack(fill="both", expand=True)

        tk.Label(p, text="Recent results", font=self._font(9, "bold"), fg="#777777",
                 bg=BG, anchor="w").pack(fill="x", padx=10, pady=(10, 2))
        rf = tk.Frame(p, bg=BG)
        rf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        sb = tk.Scrollbar(rf)
        self._t_results = tk.Text(rf, **TXT)
        self._t_results.pack(side="left", fill="both", expand=True)
        sb.config(command=self._t_results.yview)

        # Show the scrollbar only when the results don't fit
        sb_shown = [False]

        def _yscroll(first, last):
            sb.set(first, last)
            need = float(first) > 0.0 or float(last) < 1.0
            if need != sb_shown[0]:
                sb_shown[0] = need
                if need:
                    sb.pack(side="right", fill="y", before=self._t_results)
                else:
                    sb.pack_forget()

        self._t_results.config(yscrollcommand=_yscroll)

        for t in (self._t_table, self._t_results):
            t.tag_config("hdr",     foreground="#777777")
            t.tag_config("playing", foreground="#88ff44")
            t.tag_config("leader",  foreground="#ffcc44")
            t.tag_config("dim",     foreground="#666666")
            t.tag_config("win",     foreground="#ffffff")
            t.tag_config("draw",    foreground="#aaaaaa")
            t.tag_config("side_w",  foreground="#ffffff",
                         font=self._font(12, family="Segoe UI Symbol"))
            t.tag_config("side_b",  foreground="#ffffff",
                         font=self._font(12, family="Segoe UI Symbol"))

    def _show_tournament_panel(self):
        if not self._tpanel_shown:
            self._tpanel_shown = True
            self._update_min_size()
            self._layout()

    def _close_tournament_panel(self):
        if self._tournament_running or not self._tpanel_shown:
            return
        self._tpanel_shown = False
        self._last_tournament_snap = None
        self._update_min_size()
        self._layout()

    def _pawn_marker(self, white: bool, font=None):
        """Small pawn image sized to the text of `font` (default: the standings
        font): solid white for White, black with a white outline for Black (so
        both read clearly on the dark background). None without PIL / pieces.png."""
        if not self._piece_src:
            return None
        h = max(8, (font or self._font(9)).metrics("linespace") - 1)
        key = (white, h)
        cache = self.__dict__.setdefault("_pawn_marks", {})
        if key in cache:
            return cache[key]
        from PIL import Image, ImageFilter, ImageTk
        shape = self._piece_src[(chess.PAWN, True)].getchannel("A")
        inner = h - 2   # leave 1 px on each side for the outline
        w = max(4, round(shape.width * inner / shape.height))
        mask = shape.resize((w, inner), Image.LANCZOS)
        canvas_mask = Image.new("L", (w + 2, h), 0)
        canvas_mask.paste(mask, (1, 1))
        if white:
            img = Image.new("RGBA", canvas_mask.size, (255, 255, 255, 255))
            img.putalpha(canvas_mask)
        else:
            outline = canvas_mask.filter(ImageFilter.MaxFilter(3))
            img = Image.new("RGBA", canvas_mask.size, (255, 255, 255, 255))
            img.putalpha(outline)
            body = Image.new("RGBA", canvas_mask.size, (0, 0, 0, 255))
            body.putalpha(canvas_mask)
            img = Image.alpha_composite(img, body)
        cache[key] = ImageTk.PhotoImage(img)
        return cache[key]

    def _set_name_label(self, lbl, name: str, white: bool, font=None):
        """Show `name` with a White/Black pawn image (glyph without PIL)."""
        img = self._pawn_marker(white, font or self._font(13, "bold"))
        if img:
            lbl.config(text=f" {name}", image=img, compound="left")
            lbl._pawn_img = img   # keep a reference
        else:
            lbl.config(text=f"{'♙' if white else '♟'} {name}", image="")

    # ── Tooltips for engine names shortened to fit a column ──

    def _insert_name(self, t, name: str, width: int, tags=()):
        """Insert `name` padded to `width` chars. A longer name is cut off with
        '…' and shows the full name in a tooltip on hover."""
        tags = tuple(tags) if isinstance(tags, (tuple, list)) else ((tags,) if tags else ())
        if len(name) <= width:
            t.insert("end", f"{name:<{width}}", tags)
            return
        self._tip_seq = getattr(self, "_tip_seq", 0) + 1
        tip = f"nametip{self._tip_seq}"
        t.tag_bind(tip, "<Enter>", lambda e, n=name: self._show_tip(e, n))
        t.tag_bind(tip, "<Motion>", lambda e: self._move_tip(e))
        t.tag_bind(tip, "<Leave>", lambda e: self._hide_tip())
        t.insert("end", name[:width - 1] + "…", tags + (tip,))

    def _clear_name_tips(self, t):
        self._hide_tip()
        for tag in t.tag_names():
            if tag.startswith("nametip"):
                t.tag_delete(tag)

    def _show_tip(self, event, text: str):
        self._hide_tip()
        tk = self._tk
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, font=self._font(9), fg="#eeeeee", bg="#333333",
                 padx=6, pady=2, relief="solid", bd=1).pack()
        self._tip = tip
        self._move_tip(event)

    def _move_tip(self, event):
        tip = getattr(self, "_tip", None)
        if tip is not None:
            tip.geometry(f"+{event.x_root + 12}+{event.y_root + 16}")

    def _hide_tip(self):
        tip = getattr(self, "_tip", None)
        if tip is not None:
            self._tip = None
            try:
                tip.destroy()
            except Exception:
                pass

    def _fit_table_height(self, lines: int):
        t = self._t_table
        pad = 2 * int(t.cget("pady")) + 4
        px = None
        try:
            t.update_idletasks()
            n = t.count("1.0", "end", "ypixels")
            px = n[0] if isinstance(n, tuple) else n
        except Exception:
            pass
        if not px:
            px = lines * self._font(9).metrics("linespace") * 1.4
        cap = 15 * self._font(9).metrics("linespace") * 1.4   # scroll beyond ~14 rows
        self._t_table_box.config(height=int(min(px, cap)) + pad)

    def _render_tournament(self, snap: dict):
        """Draw a Tournament.snapshot() in the panel."""
        self._show_tournament_panel()
        self._last_tournament_snap = snap
        status, rows = snap["status"], snap["rows"]
        self._t_title.config(text=snap["title"])

        lines = [f"{snap['progress']}   ·   {snap['tc']}"]
        color = "#aaaaaa"
        if status == "starting":
            lines.append("Starting engines…")
        elif status == "running" and snap["current"]:
            lines.append(f"Now: ♙ {snap['current'][0]} – ♟ {snap['current'][1]}")
        elif status == "finished":
            winners = [r["name"] for r in rows if r["rank"] == 1]
            lines.append(("Winner: " if len(winners) == 1 else "Shared 1st: ")
                         + ", ".join(winners))
            color = "#ffcc44"
        elif status == "stopped":
            lines.append("Stopped")
            color = "#ff7755"
        self._t_info.config(text="\n".join(lines), fg=color)

        # Standings
        # Engines in the current game are green, with a pawn of their color
        # in the second column: ♙ = White, ♟ = Black
        playing = {}
        if snap["current"]:
            playing = {snap["current"][0]: "W", snap["current"][1]: "B"}
        t = self._t_table
        t.config(state="normal")
        t.delete("1.0", "end")
        self._clear_name_tips(t)
        # The pawn marker is an image (or a glyph without PIL), not a Consolas
        # character, so a tab stop after it keeps the columns aligned
        f_txt = self._font(9)
        marks = {s: self._pawn_marker(s == "W") for s in ("W", "B")}
        if marks["W"]:
            mark_w = marks["W"].width()
        else:
            mark_w = self._font(12, family="Segoe UI Symbol").measure("♟")
        tab = f_txt.measure("99 ") + mark_w + f_txt.measure(" ")
        t.config(tabs=(tab,))
        t.insert("end", f"{'#':>2}\t{'Engine':<17} {'G':>2} {'W':>2} {'D':>2} "
                        f"{'L':>2} {'Pts':>5} {'SB':>6}", "hdr")
        for r in rows:
            side = playing.get(r["name"])
            if side:
                tag = "playing"
            elif status == "finished" and r["rank"] == 1:
                tag = "leader"
            else:
                tag = ()
            t.insert("end", f"\n{r['rank']:>2} ", tag)
            if side and marks[side]:
                t.image_create("end", image=marks[side], align="center")
            elif side:
                t.insert("end", "♙" if side == "W" else "♟",
                         "side_w" if side == "W" else "side_b")
            t.insert("end", "\t", tag)
            self._insert_name(t, r["name"], 17, tag)
            t.insert("end", f" {r['games']:>2} {r['wins']:>2} "
                            f"{r['draws']:>2} {r['losses']:>2} {r['points']:>5g} "
                            f"{r['sb']:>6.2f}", tag)
        t.config(state="disabled")
        self._fit_table_height(len(rows) + 1)

        # Results, newest first
        t = self._t_results
        t.config(state="normal")
        t.delete("1.0", "end")
        self._clear_name_tips(t)
        if not snap["results"]:
            t.insert("end", "No games finished yet", "dim")
        for k, e in enumerate(snap["results"]):
            if k:
                t.insert("end", "\n")
            t.insert("end", f"R{e['round']:<3}", "dim")
            if e["type"] == "bye":
                self._insert_name(t, e["name"], 14)
                t.insert("end", " ")
                t.insert("end", "bye (+1)", "dim")
                continue
            res = e["result"].replace("1/2", "½")
            self._insert_name(t, e["white"], 14)
            t.insert("end", " ")
            t.insert("end", f"{res:^5}", "draw" if "½" in res else "win")
            t.insert("end", " ")
            self._insert_name(t, e["black"], 14)
            t.insert("end", " ")
            t.insert("end", e["reason"], "dim")
        t.config(state="disabled")
        t.yview_moveto(0.0)

    # ── Tournament mode ──

    def enter_tournament_mode(self, paused: bool = False):
        """Switch buttons to tournament behaviour and start running (or wait
        for ▶ if `paused`). Main thread."""
        self._tournament_running = True
        self._tournament_done = False
        self._think_start  = None
        self._game_started = True
        self._game_over    = False
        if paused:
            self._resume_event.clear()
            self._play_btn.config(state="normal", text="▶", fg="#88ff44", cursor="hand2")
        else:
            self._resume_event.set()
            self._play_btn.config(state="normal", text="⏸", fg="#ffaa44", cursor="hand2")
        self._action_btn.config(state="normal", cursor="hand2")
        self._update_tourn_btn()
        self._t_close.config(fg="#444444", cursor="")
        self._bn.config(cursor="")
        self._wn.config(cursor="")
        self._update_action_btn()
        self._show_tournament_panel()
        self._t_title.config(text="Tournament")
        self._t_info.config(text="Starting engines…", fg="#aaaaaa")
        for t in (self._t_table, self._t_results):
            t.config(state="normal")
            t.delete("1.0", "end")
            t.config(state="disabled")

    def prepare_tournament_game(self, white_engine, black_engine,
                                white_time: Optional[float] = None,
                                black_time: Optional[float] = None):
        """Clear the board for the next tournament game. Main thread."""
        self.set_engines(white_engine, black_engine)
        self._update_engine_name(chess.WHITE, white_engine.name)
        self._update_engine_name(chess.BLACK, black_engine.name)
        self._think_start  = None
        self._game_over    = False
        self._game_started = True
        self._draw_board(chess.Board())
        self._draw_graph({})
        self._last_move_lbl.config(text="")
        self._moves_lbl.config(text="")
        for frame, nm, tm, t in [
            (self._wf, self._wn, self._wt, white_time),
            (self._bf, self._bn, self._bt, black_time),
        ]:
            for w in (frame, *frame.winfo_children()):
                w.config(bg=self.BG_IDLE)
            if t is not None:
                tm.config(text=fmt_time(t))
        self._update_action_btn()

    def show_tournament_done(self):
        """All games are played: the last game stays on screen and ■ becomes
        ✓, which ends the tournament. Main thread."""
        if not self._tournament_running:
            return
        self._tournament_done = True
        self._resume_event.clear()
        self._play_btn.config(text="▶", state="disabled", fg="#555555", cursor="")
        self._update_action_btn()
        self._update_tourn_btn()

    def show_tournament_extended(self):
        """A finished tournament got more rounds: ▶ plays on. Main thread."""
        if not (self._tournament_running and self._tournament_done):
            return
        self._tournament_done = False
        self._resume_event.clear()
        self._play_btn.config(text="▶", state="normal", fg="#88ff44", cursor="hand2")
        self._update_action_btn()
        self._update_tourn_btn()

    def exit_tournament_mode(self):
        """Back to normal buttons. The panel stays until closed with ✕. Main thread."""
        self._tournament_running = False
        self._tournament_done = False
        self._think_start = None
        self._update_tourn_btn()
        self._t_close.config(fg="#aaaaaa", cursor="hand2")
        self._update_action_btn()
