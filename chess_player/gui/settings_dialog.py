"""⚙ Settings dialog: time control and font size."""

from ..formatting import fmt_duration, parse_duration



class SettingsDialogMixin:
    # ── Settings dialog (time control) ──

    def _open_settings(self):
        if not self._settings_get_cb:
            return
        tk  = self._tk
        BG, FG, DIM = "#1a1a1a", "#cccccc", "#888888"
        ENTRY = dict(font=self._font(10), width=9, bg="#2e2e2e", fg="white",
                     insertbackground="white", relief="flat",
                     highlightthickness=1, highlightbackground="#444444",
                     highlightcolor="#55aaff", justify="right")
        tc = dict(self._settings_get_cb())

        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.configure(bg=BG, padx=18, pady=14)
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text="Time control", font=self._font(12, "bold"),
                 fg="white", bg=BG).grid(row=0, column=0, columnspan=3,
                                         sticky="w", pady=(0, 10))

        grid = tk.Frame(win, bg=BG)
        grid.grid(row=1, column=0, columnspan=3, sticky="w")
        grid.columnconfigure(0, minsize=self._font(10).measure("0" * 20))
        tk.Label(grid, text="", bg=BG).grid(row=0, column=0)
        tk.Label(grid, text="Time", font=self._font(9), fg=DIM, bg=BG
                 ).grid(row=0, column=1, sticky="e", padx=(10, 0))
        tk.Label(grid, text="Increment", font=self._font(9), fg=DIM, bg=BG
                 ).grid(row=0, column=2, sticky="e", padx=(10, 0))

        def _row(r, label, t, inc, white=None):
            lbl = tk.Label(grid, text=label, font=self._font(10), fg=FG, bg=BG,
                           anchor="w")
            if white is not None:
                self._set_name_label(lbl, label, white, self._font(10))
            te  = tk.Entry(grid, **ENTRY)
            ie  = tk.Entry(grid, **ENTRY)
            te.insert(0, fmt_duration(t))
            ie.insert(0, f"{inc:g}")
            lbl.grid(row=r, column=0, sticky="w", pady=3)
            te.grid(row=r, column=1, padx=(10, 0), pady=3)
            ie.grid(row=r, column=2, padx=(10, 0), pady=3)
            return (lbl, te, ie)

        both_row  = _row(1, "Both engines", tc["white_time"], tc["white_inc"])
        white_row = _row(2, self.white_name[:17], tc["white_time"], tc["white_inc"], True)
        black_row = _row(3, self.black_name[:17], tc["black_time"], tc["black_inc"], False)

        sep_var = tk.BooleanVar(value=tc["separate"])

        def _sync_rows(*_):
            if sep_var.get():
                for w in both_row:
                    w.grid_remove()
                for row in (white_row, black_row):
                    for w in row:
                        w.grid()
            else:
                for row in (white_row, black_row):
                    for w in row:
                        w.grid_remove()
                for w in both_row:
                    w.grid()

        def _on_toggle():
            if sep_var.get():
                # Copy shared values into both per-engine rows
                for _, te, ie in (white_row, black_row):
                    te.delete(0, "end"); te.insert(0, both_row[1].get())
                    ie.delete(0, "end"); ie.insert(0, both_row[2].get())
            else:
                # Collapse back to White's values
                both_row[1].delete(0, "end"); both_row[1].insert(0, white_row[1].get())
                both_row[2].delete(0, "end"); both_row[2].insert(0, white_row[2].get())
            _sync_rows()

        tk.Checkbutton(win, text="Different time per engine", variable=sep_var,
                       command=_on_toggle, font=self._font(10), fg=FG, bg=BG,
                       selectcolor="#2e2e2e", activebackground=BG,
                       activeforeground="white", cursor="hand2"
                       ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        note = ("Changes apply from the next game." if self.game_in_progress
                else "")
        msg = tk.Label(win, text=note, font=self._font(9), fg="#ffaa44", bg=BG)
        msg.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        tk.Label(win, text="Display", font=self._font(12, "bold"),
                 fg="white", bg=BG).grid(row=5, column=0, columnspan=3,
                                         sticky="w", pady=(14, 6))
        dfr = tk.Frame(win, bg=BG)
        dfr.grid(row=6, column=0, columnspan=3, sticky="w")
        tk.Label(dfr, text="Font size", font=self._font(10), fg=FG, bg=BG,
                 anchor="w", width=20).pack(side="left")
        font_var = tk.StringVar(value=str(self._font_size))
        tk.Spinbox(dfr, from_=self.FONT_MIN, to=self.FONT_MAX, textvariable=font_var,
                   width=5, font=self._font(10), bg="#2e2e2e", fg="white",
                   relief="flat", buttonbackground="#3a3a3a",
                   insertbackground="white", justify="right").pack(side="left", padx=(10, 0))
        tk.Label(dfr, text=f"(default {self.FONT_DEFAULT})", font=self._font(9),
                 fg=DIM, bg=BG).pack(side="left", padx=(8, 0))

        _sync_rows()

        def _read(row, what):
            _, te, ie = row
            try:
                t = parse_duration(te.get())
                if t <= 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Invalid time for {what}: {te.get()!r}") from None
            try:
                i = float(ie.get().strip() or 0)
                if i < 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Invalid increment for {what}: {ie.get()!r}") from None
            return t, i

        def _ok(event=None):
            try:
                if sep_var.get():
                    wt, wi = _read(white_row, "White")
                    bt, bi = _read(black_row, "Black")
                else:
                    wt, wi = _read(both_row, "both engines")
                    bt, bi = wt, wi
            except ValueError as e:
                msg.config(text=str(e), fg="#ff5533")
                return
            try:
                fsize = int(font_var.get())
                if not self.FONT_MIN <= fsize <= self.FONT_MAX:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Font size must be {self.FONT_MIN}–{self.FONT_MAX}",
                           fg="#ff5533")
                return
            new_tc = {"separate": sep_var.get(),
                      "white_time": wt, "white_inc": wi,
                      "black_time": bt, "black_inc": bi}
            win.destroy()
            self.set_font_size(fsize)
            if self._settings_apply_cb and new_tc != dict(self._settings_get_cb()):
                self._settings_apply_cb(new_tc)

        def _cancel(event=None):
            win.destroy()

        bf = tk.Frame(win, bg=BG)
        bf.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
        tk.Button(bf, text="OK", font=self._font(9),
                  bg="#2a4a00", fg="#88ff44", relief="flat", padx=14, pady=4,
                  activebackground="#3a6a00", activeforeground="#aaff66",
                  cursor="hand2", command=_ok).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=12, pady=4,
                  activebackground="#555555", activeforeground="white",
                  cursor="hand2", command=_cancel).pack(side="left")

        win.bind("<Escape>", _cancel)
        win.bind("<Return>", _ok)

        # Centre over the main window, then make modal
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()  - win.winfo_reqwidth())  // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        (white_row if sep_var.get() else both_row)[1].focus_set()
        win.wait_window()
