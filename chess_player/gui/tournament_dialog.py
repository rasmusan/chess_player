"""Tournament setup / edit dialogs and saving tournament files."""

import datetime
import os
from typing import Optional
from ..formatting import fmt_duration, parse_duration
from ..settings import default_time_control, engine_candidates, load_tournament_settings, save_tournament_settings, write_json_atomic
from ..tournament import TOURNAMENTS_DIR, Tournament, load_tournament_file



class TournamentDialogMixin:
    # ── Tournament setup dialog ──

    def _open_tournament_dialog(self):
        if not self._tournament_start_cb:
            return
        start_cb = self._tournament_start_cb
        restart = None   # info on a running tournament that can be set up anew
        if self._tournament_running:
            if self._resume_event.is_set() or not self._tournament_info_cb:
                return
            info = self._tournament_info_cb()
            if info is None:
                return
            if not info["fresh"]:
                self._open_tournament_edit(info)
                return
            restart, start_cb = info, self._tournament_restart_cb
        tk = self._tk
        BG, FG, DIM = "#1a1a1a", "#cccccc", "#888888"
        ENTRY = dict(font=self._font(10), width=9, bg="#2e2e2e", fg="white",
                     insertbackground="white", relief="flat",
                     highlightthickness=1, highlightbackground="#444444",
                     highlightcolor="#55aaff", justify="right")
        saved = load_tournament_settings()
        if restart:   # show the setup of the paused tournament
            cnt_key = "rr_rounds" if restart["format"] == "rr" else "swiss_matches"
            saved = {**saved, "engines": restart["engines"], "format": restart["format"],
                     cnt_key: restart["count"], "time": restart["time"],
                     "inc": restart["inc"], "auto_pause": restart["auto_pause"],
                     "load_two": restart["load_two"]}
        tc = self._settings_get_cb() if self._settings_get_cb else default_time_control()

        items = [(disp, path) for disp, path, _ in engine_candidates()]
        known = {p for _, p in items}
        for pth in saved.get("engines", []):
            apath = os.path.abspath(pth)
            if apath not in known and os.path.isfile(apath):
                items.append((os.path.splitext(os.path.basename(apath))[0], apath))
                known.add(apath)

        win = tk.Toplevel(self.root)
        win.title("Tournament")
        win.configure(bg=BG, padx=18, pady=14)
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text="Tournament", font=self._font(12, "bold"), fg="white",
                 bg=BG).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        tk.Label(win, text="Engines (click to select, 2 or more):", font=self._font(9),
                 fg=DIM, bg=BG).grid(row=1, column=0, columnspan=4, sticky="w")

        lf = tk.Frame(win, bg=BG)
        lf.grid(row=2, column=0, columnspan=4, sticky="we", pady=(2, 4))
        vsb = tk.Scrollbar(lf)
        vsb.pack(side="right", fill="y")
        lb = tk.Listbox(lf, font=self._font(10), bg="#111111", fg=FG, height=8, width=44,
                        selectmode="multiple", exportselection=False,
                        selectbackground="#3a5a00", selectforeground="white",
                        relief="flat", activestyle="none", yscrollcommand=vsb.set)
        lb.pack(side="left", fill="both", expand=True)
        vsb.config(command=lb.yview)
        selected = {os.path.abspath(p) for p in saved.get("engines", [])}
        for k, (disp, path) in enumerate(items):
            lb.insert("end", f"  {disp}")
            if path in selected:
                lb.selection_set(k)

        def _browse():
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title="Add engine to tournament",
                filetypes=[("Executable", "*.exe"), ("All files", "*.*")], parent=win)
            if not path:
                return
            apath = os.path.abspath(path)
            if apath in known:
                k = [p for _, p in items].index(apath)
            else:
                items.append((os.path.splitext(os.path.basename(apath))[0], apath))
                known.add(apath)
                lb.insert("end", f"  {items[-1][0]}")
                k = len(items) - 1
            lb.selection_set(k)
            lb.see(k)
            _update()

        def _select_all(on: bool):
            if on:
                lb.selection_set(0, "end")
            else:
                lb.selection_clear(0, "end")
            _update()

        SB = dict(font=self._font(9), bg="#3a3a3a", fg="#aaaaaa", relief="flat",
                  padx=10, pady=2, activebackground="#555555",
                  activeforeground="white", cursor="hand2")
        ef = tk.Frame(win, bg=BG)
        ef.grid(row=3, column=0, columnspan=4, sticky="w")
        tk.Button(ef, text="Add engine…", command=_browse, **SB).pack(side="left")
        tk.Button(ef, text="Add all", command=lambda: _select_all(True), **SB
                  ).pack(side="left", padx=(8, 0))
        tk.Button(ef, text="Remove all", command=lambda: _select_all(False), **SB
                  ).pack(side="left", padx=(8, 0))

        # Format + count
        fmt_var = tk.StringVar(value=saved.get("format", "rr")
                               if saved.get("format") in ("rr", "swiss") else "rr")
        counts = {"rr": int(saved.get("rr_rounds", 1) or 1),
                  "swiss": int(saved.get("swiss_matches", 5) or 5)}
        count_var = tk.StringVar(value=str(counts[fmt_var.get()]))
        cur_fmt = [fmt_var.get()]

        ff = tk.Frame(win, bg=BG)
        ff.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        tk.Label(ff, text="Format:", font=self._font(10), fg=FG, bg=BG, width=20,
                 anchor="w").pack(side="left")
        RB = dict(variable=fmt_var, font=self._font(10), fg=FG, bg=BG,
                  selectcolor="#2e2e2e", activebackground=BG, activeforeground="white",
                  cursor="hand2")

        cf = tk.Frame(win, bg=BG)
        cf.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        count_lbl = tk.Label(cf, text="", font=self._font(10), fg=FG, bg=BG,
                             width=20, anchor="w")
        count_lbl.pack(side="left")
        tk.Spinbox(cf, from_=1, to=99, textvariable=count_var, width=5,
                   font=self._font(10), bg="#2e2e2e", fg="white", relief="flat",
                   buttonbackground="#3a3a3a", insertbackground="white",
                   justify="right").pack(side="left")
        games_lbl = tk.Label(cf, text="", font=self._font(9), fg=DIM, bg=BG)
        games_lbl.pack(side="left", padx=(10, 0))

        def _on_fmt():
            try:
                counts[cur_fmt[0]] = max(1, int(count_var.get()))
            except ValueError:
                pass
            cur_fmt[0] = fmt_var.get()
            count_var.set(str(counts[cur_fmt[0]]))
            _update()

        tk.Radiobutton(ff, text="Round robin", value="rr", command=_on_fmt, **RB
                       ).pack(side="left")
        tk.Radiobutton(ff, text="Swiss", value="swiss", command=_on_fmt, **RB
                       ).pack(side="left", padx=(10, 0))

        # Time control (same for all engines)
        tf = tk.Frame(win, bg=BG)
        tf.grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))
        tk.Label(tf, text="Time / increment:", font=self._font(10), fg=FG, bg=BG,
                 width=20, anchor="w").pack(side="left")
        time_e = tk.Entry(tf, **ENTRY)
        time_e.insert(0, fmt_duration(saved.get("time", tc["white_time"])))
        time_e.pack(side="left")
        tk.Label(tf, text="+", font=self._font(10), fg=DIM, bg=BG).pack(side="left", padx=4)
        inc_e = tk.Entry(tf, **ENTRY)
        inc_e.insert(0, f"{float(saved.get('inc', tc['white_inc'])):g}")
        inc_e.pack(side="left")

        CB = dict(font=self._font(10), fg=FG, bg=BG, selectcolor="#2e2e2e",
                  activebackground=BG, activeforeground="white", cursor="hand2")
        auto_var = tk.BooleanVar(value=bool(saved.get("auto_pause", False)))
        tk.Checkbutton(win, text="Pause before each match", variable=auto_var, **CB
                       ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))
        load_two_var = tk.BooleanVar(value=bool(saved.get("load_two", False)))
        tk.Checkbutton(win, text="Load only two engines at a time (saves memory)",
                       variable=load_two_var, **CB
                       ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # A finished tournament with the same engines and format can be
        # extended with more rounds / matches instead of starting over
        extend_var = tk.BooleanVar(value=True)
        extend_cb = tk.Checkbutton(win, text="", variable=extend_var, **CB)
        extend_cb.grid(row=9, column=0, columnspan=4, sticky="w", pady=(2, 0))
        extend_cb.grid_remove()

        def _norm(pth):
            return os.path.normcase(os.path.abspath(pth))

        def _extend_games() -> int:
            """Number of extra games if the finished tournament can be
            extended with the current choices, else 0."""
            ft = self._finished_tournament
            if not ft:
                return 0
            td = ft["tournament"]
            try:
                c = int(count_var.get())
            except ValueError:
                return 0
            sel = sorted(_norm(items[k][1]) for k in lb.curselection())
            if (fmt_var.get() != td["format"] or c <= int(td["count"]) or
                    sel != sorted(_norm(e["path"]) for e in ft["engines"])):
                return 0
            try:
                old = Tournament.from_dict(td)
                new = Tournament.from_dict({**td, "count": c})
            except (KeyError, TypeError, ValueError):
                return 0
            return new.total_games - old.total_games

        msg = tk.Label(win, text="", font=self._font(9), fg="#ff5533", bg=BG)
        msg.grid(row=10, column=0, columnspan=4, sticky="w", pady=(6, 0))

        def _update(*_):
            fmt = fmt_var.get()
            count_lbl.config(text="Rounds:" if fmt == "rr" else "Matches per engine:")
            extra = _extend_games()
            if extra:
                extend_cb.config(text=f"Extend the finished tournament "
                                      f"(+{extra} game{'s' if extra != 1 else ''})")
                extend_cb.grid()
            else:
                extend_cb.grid_remove()
            n = len(lb.curselection())
            try:
                c = max(1, int(count_var.get()))
            except ValueError:
                games_lbl.config(text="")
                return
            if n < 2:
                games_lbl.config(text="select at least 2 engines")
                return
            total = c * n * (n - 1) // 2 if fmt == "rr" else c * (n // 2)
            note = " (with rematches)" if fmt == "swiss" and c > n - 1 + n % 2 else ""
            games_lbl.config(text=f"= {total} game{'s' if total != 1 else ''}{note}")

        lb.bind("<<ListboxSelect>>", _update)
        count_var.trace_add("write", _update)
        _update()

        def _start(event=None):
            sel = [items[k][1] for k in lb.curselection()]
            if len(sel) < 2:
                msg.config(text="Select at least 2 engines.")
                return
            try:
                count = int(count_var.get())
                if not 1 <= count <= 999:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Invalid number: {count_var.get()!r}")
                return
            try:
                t = parse_duration(time_e.get())
                if t <= 0:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Invalid time: {time_e.get()!r}")
                return
            try:
                inc = float(inc_e.get().strip() or 0)
                if inc < 0:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Invalid increment: {inc_e.get()!r}")
                return
            fmt = fmt_var.get()
            counts[fmt] = count
            save_tournament_settings({
                "engines": sel, "format": fmt,
                "rr_rounds": counts["rr"], "swiss_matches": counts["swiss"],
                "time": t, "inc": inc, "auto_pause": auto_var.get(),
                "load_two": load_two_var.get(),
            })
            if extend_var.get() and _extend_games():
                ft = self._finished_tournament
                st = {**ft, "tournament": {**ft["tournament"], "count": count},
                      "time": t, "inc": inc, "auto_pause": auto_var.get(),
                      "load_two": load_two_var.get()}
                win.destroy()
                start_cb({"resume": st})
                return
            # Unique display names (two engines may share a file name)
            names, used = [], {}
            for pth in sel:
                base = os.path.splitext(os.path.basename(pth))[0]
                used[base] = used.get(base, 0) + 1
                names.append(base if used[base] == 1 else f"{base} #{used[base]}")
            cfg = {"engines": list(zip(names, sel)), "format": fmt,
                   "count": count, "time": t, "inc": inc,
                   "auto_pause": auto_var.get(), "load_two": load_two_var.get()}
            win.destroy()
            start_cb(cfg)

        def _continue():
            from tkinter import filedialog
            os.makedirs(TOURNAMENTS_DIR, exist_ok=True)
            path = filedialog.askopenfilename(
                title="Continue tournament", initialdir=TOURNAMENTS_DIR,
                filetypes=[("Tournament", "*.json"), ("All files", "*.*")], parent=win)
            if not path:
                return
            try:
                st = load_tournament_file(path)
            except ValueError as e:
                msg.config(text=str(e))
                return
            st["auto_pause"] = auto_var.get()
            st["load_two"] = load_two_var.get()
            st["autosave"] = path   # keep this file up to date
            win.destroy()
            start_cb({"resume": st})

        tk.Button(win, text="Continue from file…", font=self._font(9), bg="#3a3a3a",
                  fg="#ffcc44", relief="flat", padx=10, pady=4, activebackground="#555555",
                  activeforeground="#ffdd66", cursor="hand2", command=_continue
                  ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(12, 0))

        bf = tk.Frame(win, bg=BG)
        bf.grid(row=11, column=2, columnspan=2, sticky="e", pady=(12, 0))
        tk.Button(bf, text="Start", font=self._font(9),
                  bg="#2a4a00", fg="#88ff44", relief="flat", padx=14, pady=4,
                  activebackground="#3a6a00", activeforeground="#aaff66",
                  cursor="hand2", command=_start).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=12, pady=4,
                  activebackground="#555555", activeforeground="white",
                  cursor="hand2", command=win.destroy).pack(side="left")
        if restart:
            tk.Label(win, text="Start restarts the paused tournament with this setup.",
                     font=self._font(9), fg="#ffaa44", bg=BG
                     ).grid(row=12, column=0, columnspan=4, sticky="w", pady=(8, 0))
        elif self.game_in_progress:
            tk.Label(win, text="Starting a tournament stops the current game.",
                     font=self._font(9), fg="#ffaa44", bg=BG
                     ).grid(row=12, column=0, columnspan=4, sticky="w", pady=(8, 0))

        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Return>", _start)

        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()  - win.winfo_reqwidth())  // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.wait_window()

    def _open_tournament_edit(self, info: dict):
        """While a started tournament is paused: change the number of rounds /
        matches and auto-pause. The engines and time control stay fixed."""
        tk = self._tk
        BG, FG, DIM = "#1a1a1a", "#cccccc", "#888888"
        win = tk.Toplevel(self.root)
        win.title("Tournament")
        win.configure(bg=BG, padx=18, pady=14)
        win.resizable(False, False)
        win.transient(self.root)

        fmt_name = "Round robin" if info["format"] == "rr" else "Swiss"
        tk.Label(win, text=f"Tournament · {fmt_name}", font=self._font(12, "bold"),
                 fg="white", bg=BG).grid(row=0, column=0, columnspan=3,
                                         sticky="w", pady=(0, 10))
        tk.Label(win, text="Rounds:" if info["format"] == "rr" else "Matches per engine:",
                 font=self._font(10), fg=FG, bg=BG, width=20, anchor="w"
                 ).grid(row=1, column=0, sticky="w")
        count_var = tk.StringVar(value=str(info["count"]))
        tk.Spinbox(win, from_=info["min_count"], to=999, textvariable=count_var,
                   width=5, font=self._font(10), bg="#2e2e2e", fg="white",
                   relief="flat", buttonbackground="#3a3a3a",
                   insertbackground="white", justify="right").grid(row=1, column=1, sticky="w")
        tk.Label(win, text=f"(at least {info['min_count']})", font=self._font(9),
                 fg=DIM, bg=BG).grid(row=1, column=2, sticky="w", padx=(10, 0))
        auto_var = tk.BooleanVar(value=info["auto_pause"])
        tk.Checkbutton(win, text="Pause before each match", variable=auto_var,
                       font=self._font(10), fg=FG, bg=BG, selectcolor="#2e2e2e",
                       activebackground=BG, activeforeground="white", cursor="hand2"
                       ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tk.Label(win, text="Engines can only be changed before the first game.",
                 font=self._font(9), fg=DIM, bg=BG
                 ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        msg = tk.Label(win, text="", font=self._font(9), fg="#ff5533", bg=BG)
        msg.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        def _ok(event=None):
            try:
                count = int(count_var.get())
                if not info["min_count"] <= count <= 999:
                    raise ValueError
            except ValueError:
                msg.config(text=f"Enter a number from {info['min_count']} to 999.")
                return
            win.destroy()
            if self._tournament_edit_cb and self._tournament_running:
                self._tournament_edit_cb({"count": count, "auto_pause": auto_var.get()})
                saved = load_tournament_settings()
                key = "rr_rounds" if info["format"] == "rr" else "swiss_matches"
                save_tournament_settings({**saved, key: count,
                                           "auto_pause": auto_var.get()})

        bf = tk.Frame(win, bg=BG)
        bf.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        tk.Button(bf, text="OK", font=self._font(9),
                  bg="#2a4a00", fg="#88ff44", relief="flat", padx=14, pady=4,
                  activebackground="#3a6a00", activeforeground="#aaff66",
                  cursor="hand2", command=_ok).pack(side="left", padx=(0, 8))
        tk.Button(bf, text="Cancel", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=12, pady=4,
                  activebackground="#555555", activeforeground="white",
                  cursor="hand2", command=win.destroy).pack(side="left")
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Return>", _ok)

        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()  - win.winfo_reqwidth())  // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.wait_window()

    def save_tournament_file(self, st: dict, ask: Optional[str] = None) -> Optional[str]:
        """Let the user save a tournament state to a file. With `ask`, first
        ask whether to save at all. Returns the path, or None. Main thread."""
        from tkinter import filedialog, messagebox
        if ask and not messagebox.askyesno("Save tournament", ask, parent=self.root):
            return None
        try:
            os.makedirs(TOURNAMENTS_DIR, exist_ok=True)
        except OSError:
            pass
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        path = filedialog.asksaveasfilename(
            title="Save tournament", parent=self.root, initialdir=TOURNAMENTS_DIR,
            initialfile=f"tournament_{stamp}.json", defaultextension=".json",
            filetypes=[("Tournament", "*.json"), ("All files", "*.*")])
        if not path:
            return None
        try:
            write_json_atomic(path, st)
        except OSError as e:
            messagebox.showerror("Save tournament", f"Could not save:\n{e}", parent=self.root)
            return None
        return path
