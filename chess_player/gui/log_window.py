"""Engine log window: UCI traffic and engine options."""


class LogWindowMixin:
    # ── Log window ──

    def _open_log_window(self, engine):
        if engine is None:
            return
        tk = self._tk

        win = tk.Toplevel(self.root)
        win.title(f"Engine Log — {engine.name}")
        win.configure(bg="#111111")
        has_options = bool(engine.options)
        k = self._scale
        win.geometry(f"{round((960 if has_options else 740) * k)}x{round(480 * k)}")

        # Toolbar
        tb = tk.Frame(win, bg="#1a1a1a", pady=4, padx=8)
        tb.pack(fill="x")
        tk.Label(tb, text=engine.name, font=self._font(11, "bold"),
                 fg="#dddddd", bg="#1a1a1a").pack(side="left")
        if not engine.running and not engine.log:
            tk.Label(tb, text="not loaded yet — it starts with the game; reopen this "
                              "window then to change its options",
                     font=self._font(9), fg="#888888", bg="#1a1a1a"
                     ).pack(side="left", padx=(12, 0))
        tk.Button(tb, text="Clear", font=self._font(9),
                  bg="#3a3a3a", fg="#aaaaaa", relief="flat", padx=8,
                  activebackground="#555", activeforeground="white",
                  cursor="hand2",
                  command=lambda: _clear()).pack(side="right")

        # Main area: log on left, options panel on right
        content = tk.Frame(win, bg="#111111")
        content.pack(fill="both", expand=True)

        # Log frame
        log_frame = tk.Frame(content, bg="#111111")
        log_frame.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(log_frame)
        sb.pack(side="right", fill="y")
        txt = tk.Text(
            log_frame, bg="#111111", fg="#cccccc",
            font=self._font(9), wrap="word",
            yscrollcommand=sb.set,
            state="disabled", relief="flat",
        )
        txt.pack(fill="both", expand=True)

        txt.tag_config("sent", foreground="#ffcc44")
        txt.tag_config("recv", foreground="#cccccc")
        txt.tag_config("info", foreground="#888888")
        txt.tag_config("best", foreground="#88ff88")

        shown      = [0]
        autoscroll = [True]

        def _on_view_change(first, last):
            sb.set(first, last)
            autoscroll[0] = float(last) >= 0.999

        txt.config(yscrollcommand=_on_view_change)
        sb.config(command=txt.yview)

        def _append(direction, line):
            txt.config(state="normal")
            if direction == ">":
                tag, prefix = "sent", "  > "
            elif line.startswith("bestmove"):
                tag, prefix = "best", "  < "
            elif line.startswith("info"):
                tag, prefix = "info", "  < "
            else:
                tag, prefix = "recv", "  < "
            txt.insert("end", prefix + line + "\n", tag)
            txt.config(state="disabled")

        def _clear():
            shown[0] = len(engine.log)
            txt.config(state="normal")
            txt.delete("1.0", "end")
            txt.config(state="disabled")

        # Options panel
        if has_options:
            OBG = "#1a1a1a"
            OFG = "#cccccc"
            ODG = "#888888"

            tk.Frame(content, bg="#333333", width=1).pack(side="left", fill="y")

            panel = tk.Frame(content, bg=OBG, width=round(212 * k))
            panel.pack(side="left", fill="y")
            panel.pack_propagate(False)

            tk.Label(panel, text="Engine Options", font=self._font(10, "bold"),
                     fg="#aaaaaa", bg=OBG, anchor="w", padx=10, pady=6).pack(fill="x")
            tk.Frame(panel, bg="#2a2a2a", height=1).pack(fill="x")

            # Scrollable area
            canvas = tk.Canvas(panel, bg=OBG, highlightthickness=0, bd=0)
            vsb = tk.Scrollbar(panel, orient="vertical", command=canvas.yview)
            sf = tk.Frame(canvas, bg=OBG)
            sf.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=sf, anchor="nw")
            canvas.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)

            def _bind_mousewheel(widget):
                widget.bind("<MouseWheel>",
                            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

            _bind_mousewheel(canvas)
            _bind_mousewheel(sf)

            for opt_name, opt in engine.options.items():
                row = tk.Frame(sf, bg=OBG, pady=4, padx=10)
                row.pack(fill="x")
                _bind_mousewheel(row)

                opt_type = opt["type"]

                if opt_type == "button":
                    btn = tk.Button(
                        row, text=opt_name, font=self._font(9),
                        bg="#3a3a3a", fg=OFG, relief="flat", padx=6,
                        activebackground="#555", activeforeground="white",
                        cursor="hand2",
                        command=(lambda n=opt_name: engine._send(f"setoption name {n}")),
                    )
                    btn.pack(anchor="w")
                    _bind_mousewheel(btn)

                elif opt_type == "check":
                    var = tk.BooleanVar(value=(opt["value"] or "").lower() == "true")

                    def _on_check(n=opt_name, v=var):
                        val = "true" if v.get() else "false"
                        engine.options[n]["value"] = val
                        engine.set_option(n, val)

                    cb = tk.Checkbutton(
                        row, text=opt_name, variable=var,
                        font=self._font(9), fg=OFG, bg=OBG,
                        selectcolor="#333333", activebackground=OBG,
                        activeforeground=OFG, relief="flat",
                        cursor="hand2", command=_on_check,
                    )
                    cb.pack(anchor="w")
                    _bind_mousewheel(cb)

                elif opt_type == "spin":
                    tk.Label(row, text=opt_name, font=self._font(9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    spin_var = tk.StringVar(value=opt["value"] or "")
                    from_val = int(opt["min"]) if opt["min"] is not None else 0
                    to_val   = int(opt["max"]) if opt["max"] is not None else 9999
                    spin = tk.Spinbox(
                        row, textvariable=spin_var,
                        from_=from_val, to=to_val,
                        font=self._font(9), width=8,
                        bg="#2a2a2a", fg=OFG, relief="flat",
                        buttonbackground="#3a3a3a",
                        insertbackground=OFG,
                    )
                    spin.pack(anchor="w")
                    _bind_mousewheel(spin)

                    def _on_spin(event=None, n=opt_name, v=spin_var):
                        engine.options[n]["value"] = v.get()
                        engine.set_option(n, v.get())

                    spin.bind("<FocusOut>", _on_spin)
                    spin.bind("<Return>",   _on_spin)

                elif opt_type == "combo":
                    tk.Label(row, text=opt_name, font=self._font(9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    combo_var = tk.StringVar(value=opt["value"] or "")
                    choices = opt["var"] or [opt["value"]]

                    def _on_combo(val, n=opt_name):
                        engine.options[n]["value"] = val
                        engine.set_option(n, val)

                    om = tk.OptionMenu(row, combo_var, *choices, command=_on_combo)
                    om.config(font=self._font(9), bg="#2a2a2a", fg=OFG,
                              activebackground="#3a3a3a", activeforeground=OFG,
                              relief="flat", padx=4, cursor="hand2")
                    om["menu"].config(bg="#2a2a2a", fg=OFG, activebackground="#444",
                                      activeforeground=OFG, font=self._font(9))
                    om.pack(anchor="w")
                    _bind_mousewheel(om)

                else:  # string
                    tk.Label(row, text=opt_name, font=self._font(9),
                             fg=OFG, bg=OBG, anchor="w").pack(fill="x")
                    str_var = tk.StringVar(value=opt["value"] or "")
                    entry = tk.Entry(
                        row, textvariable=str_var,
                        font=self._font(9), width=20,
                        bg="#2a2a2a", fg=OFG, relief="flat",
                        insertbackground=OFG,
                    )
                    entry.pack(anchor="w", fill="x")
                    _bind_mousewheel(entry)

                    def _on_entry(event=None, n=opt_name, v=str_var):
                        engine.options[n]["value"] = v.get()
                        engine.set_option(n, v.get())

                    entry.bind("<FocusOut>", _on_entry)
                    entry.bind("<Return>",   _on_entry)

                # Subtle separator between options
                tk.Frame(sf, bg="#252525", height=1).pack(fill="x", padx=10)

        def _poll():
            if not win.winfo_exists():
                return
            log = engine.log
            while shown[0] < len(log):
                _append(*log[shown[0]])
                shown[0] += 1
            if autoscroll[0]:
                txt.see("end")
            win.after(100, _poll)

        _poll()
