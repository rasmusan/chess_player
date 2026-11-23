"""Graph panel next to the board: eval / depth / nodes per move."""

from typing import Optional
from ..formatting import fmt_count
from ..game import GRAPH_METRICS
from ..settings import save_gui_settings




class GraphMixin:
    # ── Graph panel (eval / depth / nodes) ──

    GRAPH_TITLES = {"eval": "Evaluation", "depth": "Search depth",
                    "nodes": "Nodes searched (log)"}

    def _build_graph_selector(self, parent):
        tk  = self._tk
        bar = tk.Frame(parent, bg=self.GBG, pady=5)
        bar.pack(fill="x")
        tk.Label(bar, text="Show:", font=self._font(9), fg="#777777",
                 bg=self.GBG).pack(side="left", padx=(8, 4))
        self._graph_btns = {}
        for m, label in (("eval", "Eval"), ("depth", "Depth"), ("nodes", "Nodes")):
            b = tk.Label(bar, text=label, font=self._font(9, "bold"),
                         padx=8, pady=2, cursor="hand2")
            b.pack(side="left", padx=2)
            b.bind("<Button-1>", lambda e, m=m: self._toggle_graph_metric(m))
            self._graph_btns[m] = b
        self._style_graph_btns()

    def _style_graph_btns(self):
        for m, b in self._graph_btns.items():
            on = m in self._graph_sel
            b.config(bg="#3a3a3a" if on else self.GBG,
                     fg="#ffffff" if on else "#666666")

    def _toggle_graph_metric(self, metric: str):
        if metric in self._graph_sel:
            if len(self._graph_sel) == 1:
                return   # keep at least one graph
            self._graph_sel.discard(metric)
        else:
            self._graph_sel.add(metric)
        self._style_graph_btns()
        save_gui_settings({"graph_metrics": [m for m in GRAPH_METRICS
                                              if m in self._graph_sel]})
        self._draw_graph()

    def _draw_graph(self, hist: Optional[dict] = None):
        """
        hist: metric -> (white_values, black_values), parallel per-ply lists
        with None on the other side's plies and where the engine reported
        nothing. Evals are normalised to White's POV. hist=None redraws the
        last data (after toggling metrics or a resize).
        The selected metrics are stacked top to bottom with a shared legend.
        """
        if hist is not None:
            self._graph_hist = hist
        c = self._gcv
        W = c.winfo_width()
        H = c.winfo_height()
        if H < 50 or W < 50:   # not mapped yet
            W, H = int(c.cget("width")), int(c.cget("height"))
        self._gw_now = W
        k = self._scale
        c.delete("all")
        c.create_rectangle(0, 0, W, H, fill=self.GBG, outline="")

        metrics = [m for m in GRAPH_METRICS if m in self._graph_sel] or ["eval"]
        LEG = round(50 * k)
        GAP = round(12 * k)   # space between the graph cards
        sec = (H - LEG - GAP * (len(metrics) - 1)) / len(metrics)
        for j, m in enumerate(metrics):
            w_vals, b_vals = self._graph_hist.get(m, ([], []))
            self._plot_metric(m, j * (sec + GAP), sec, w_vals, b_vals)

        # Legend — Black on top, White below (mirrors board orientation)
        ML = round(44 * k)
        for y_off, color, label in [
            (H - LEG + 16 * k, self.C_B, f"B: {self.black_name[:24]}"),
            (H - LEG + 36 * k, self.C_W, f"W: {self.white_name[:24]}"),
        ]:
            c.create_line(ML, y_off, ML + 16, y_off, fill=color, width=2)
            c.create_text(ML + 20, y_off, text=label, fill=color,
                          font=self._font(10), anchor="w")

    def _plot_metric(self, metric: str, top: float, height: float,
                     w_vals: list, b_vals: list):
        """
        eval:  y inverted (top = Black ahead), auto-scaled, at least ±1, capped ±10
        depth: linear from 0
        nodes: log10 scale
        """
        import math
        c = self._gcv
        k = self._scale
        INSET = 6   # card edge to window edge
        ML, MR, MT, MB = round(44 * k), 16, round(26 * k), 10
        pw = max(10, self._gw_now - ML - MR)
        y0 = top + MT
        ph = max(10.0, height - MT - MB)
        MIN_GAP = 16 * k   # min px between labelled gridlines

        # Each graph is a card of its own: lighter background, border, title bar
        c.create_rectangle(INSET, top, self._gw_now - INSET, top + height,
                           fill=self.GCARD, outline="#3a3a3a")
        c.create_text(INSET + 8, top + 12 * k, text=self.GRAPH_TITLES[metric],
                      fill="#bbbbbb", font=self._font(9, "bold"), anchor="w")

        vals = [v for v in list(w_vals) + list(b_vals) if v is not None]
        n = max(len(w_vals), len(b_vals), 1)

        def gx(i):
            return ML + (pw * i / (n - 1) if n > 1 else pw / 2)

        zero = None
        if metric == "eval":
            if vals:
                y_max = min(10.0, max(1.0, max(vals)))
                y_min = max(-10.0, min(-1.0, min(vals)))
            else:
                y_min, y_max = -1.0, 1.0

            def gy(v):
                v = max(y_min, min(y_max, float(v)))
                return y0 + ph * (v - y_min) / (y_max - y_min)

            step = next((s for s in (1, 2, 5) if ph * s / (y_max - y_min) >= MIN_GAP), 10)
            ticks = [(v, "0" if v == 0 else f"+{v}" if v > 0 else f"−{abs(v)}")
                     for v in range(-10, 11) if v % step == 0 and y_min <= v <= y_max]
            zero = 0
        elif metric == "depth":
            vals = [v for v in vals if v >= 0]
            top_v = max(vals) if vals else 10
            step = next((s for s in (1, 2, 5, 10, 20, 50)
                         if ph * s / max(top_v, 1) >= MIN_GAP), 100)
            y_max = max(step, -(-top_v // step) * step)   # round up to a step

            def gy(v):
                return y0 + ph * (1 - max(0, min(y_max, v)) / y_max)

            ticks = [(v, str(v)) for v in range(0, int(y_max) + 1, step)]
        else:   # nodes
            vals = [v for v in vals if v > 0]
            if vals:
                lo = math.floor(math.log10(min(vals)))
                hi = math.ceil(math.log10(max(vals)))
            else:
                lo, hi = 3, 6
            if hi <= lo:
                hi = lo + 1

            def gy(v):
                if v <= 0:
                    return y0 + ph
                return y0 + ph * (1 - (math.log10(v) - lo) / (hi - lo))

            every = next((k for k in range(1, 10) if ph * k / (hi - lo) >= MIN_GAP), 10)
            ticks = [(10 ** e, fmt_count(10 ** e)) for e in range(lo, hi + 1)
                     if (e - lo) % every == 0]

        for val, label in ticks:
            y = gy(val)
            is_zero = val == zero
            c.create_line(ML, y, ML + pw, y, fill="#888" if is_zero else "#666",
                          width=2 if is_zero else 1, dash=() if is_zero else (2, 3))
            c.create_text(ML - 4, y, text=label, fill="#999",
                          font=self._font(8), anchor="e")

        c.create_rectangle(ML, y0, ML + pw, y0 + ph, outline="#4a4a4a", width=1)

        if not vals and n >= 2:
            # Moves have been played, but the engines never reported this value
            c.create_text(ML + pw / 2, y0 + ph / 2, text="no data from engines",
                          fill="#666", font=self._font(9))

        for series, color in ((w_vals, self.C_W), (b_vals, self.C_B)):
            pts = [(i, v) for i, v in enumerate(series)
                   if v is not None and (metric != "nodes" or v > 0)]
            if len(pts) >= 2:
                coords = []
                for i, v in pts:
                    coords.extend([gx(i), gy(v)])
                c.create_line(*coords, fill=color, width=2, smooth=False)
            elif len(pts) == 1:
                x, y = gx(pts[0][0]), gy(pts[0][1])
                c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")
