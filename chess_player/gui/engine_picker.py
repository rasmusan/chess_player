"""Engine picker, opened by clicking a player's name."""

import chess
from ..settings import engine_candidates



class EnginePickerMixin:
    # ── Engine picker ──

    def _update_engine_name(self, color: chess.Color, name: str):
        if color == chess.WHITE:
            self.white_name = name
            self._set_name_label(self._wn, name, True)
            self._log_btn_w.config(text=f"☰  {name[:14]}")
        else:
            self.black_name = name
            self._set_name_label(self._bn, name, False)
            self._log_btn_b.config(text=f"☰  {name[:14]}")

    def _on_name_click(self, color: chess.Color):
        if self._tournament_running:
            return
        if not (self._game_over or not self._game_started):
            return
        self._open_engine_picker(color)

    def _open_engine_picker(self, color: chess.Color):
        tk   = self._tk
        side = "White" if color == chess.WHITE else "Black"

        # engines/ folder first, then history extras
        items = engine_candidates()   # (display_name, abs_path, source)

        # Dialog
        BG  = "#1a1a1a"
        FG  = "#cccccc"
        win = tk.Toplevel(self.root)
        win.title(f"Select Engine — {side}")
        win.configure(bg=BG)
        win.geometry(f"{round(380 * self._scale)}x{round(300 * self._scale)}")
        win.resizable(True, True)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text=f"Engine for {side}:", font=self._font(10, "bold"),
                 fg="#aaaaaa", bg=BG, padx=12).pack(anchor="w", pady=(8, 4))

        lf = tk.Frame(win, bg=BG, padx=12)
        lf.pack(fill="both", expand=True)
        vsb = tk.Scrollbar(lf)
        vsb.pack(side="right", fill="y")
        lb = tk.Listbox(
            lf, font=self._font(10), bg="#111111", fg=FG,
            selectbackground="#3a5a00", selectforeground="white",
            relief="flat", activestyle="none", yscrollcommand=vsb.set,
        )
        lb.pack(fill="both", expand=True)
        vsb.config(command=lb.yview)

        for display, _, _ in items:
            lb.insert("end", f"  {display}")
        if items:
            lb.selection_set(0)
            lb.activate(0)

        # Separator
        tk.Frame(win, bg="#333333", height=1).pack(fill="x", padx=12, pady=(6, 0))

        # Buttons
        bf = tk.Frame(win, bg=BG, pady=8, padx=12)
        bf.pack(fill="x")

        result: list[str | None] = [None]

        def _select():
            sel = lb.curselection()
            if sel:
                result[0] = items[sel[0]][1]
            win.destroy()

        def _browse():
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title=f"Select engine executable for {side}",
                filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
                parent=win,
            )
            if path:
                result[0] = path
                win.destroy()

        def _mk(parent, text, fg, cmd, **kw):
            bg  = kw.pop("bg",              "#3a3a3a")
            abg = kw.pop("activebackground", "#555555")
            return tk.Button(parent, text=text, font=self._font(9),
                             bg=bg, fg=fg, relief="flat", padx=10, pady=3,
                             activebackground=abg, activeforeground="white",
                             cursor="hand2", command=cmd, **kw)

        _mk(bf, "Browse...", "#aaaaaa", _browse).pack(side="left")
        _mk(bf, "Cancel",    "#888888", win.destroy).pack(side="right")
        _mk(bf, "Select",    "#88ff44", _select,
            bg="#2d4a00", activebackground="#3a6000").pack(side="right", padx=(0, 8))

        lb.bind("<Double-Button-1>", lambda e: _select())
        lb.bind("<Return>",          lambda e: _select())
        win.bind("<Escape>",         lambda e: win.destroy())

        win.wait_window()

        if result[0] and self._change_engine_cb:
            self._change_engine_cb(color, result[0])
