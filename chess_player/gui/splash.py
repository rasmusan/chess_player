"""Startup splash window and fatal-error dialog."""

import threading
import time
from typing import Optional
from .. import __version__


class SplashWindow:
    """Small borderless "starting up" window, shown while the engines start
    (before the main window exists)."""

    BG = "#1a1a1a"

    @classmethod
    def create(cls) -> Optional["SplashWindow"]:
        try:
            return cls()
        except Exception as e:   # no display etc.: start without a splash
            print(f"(no splash window: {e})")
            return None

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.status = "Starting…"   # set from any thread, shown by run()
        self.root = r = tk.Tk()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=self.BG, highlightthickness=1, highlightbackground="#444444")
        tk.Label(r, text="♛  Chess Player", font=("Segoe UI Symbol", 18, "bold"),
                 fg="#ffcc44", bg=self.BG).pack(padx=48, pady=(26, 0))
        tk.Label(r, text=f"version {__version__}", font=("Consolas", 9),
                 fg="#777777", bg=self.BG).pack()
        self._msg = tk.Label(r, text=self.status, font=("Consolas", 10), fg="#cccccc",
                             bg=self.BG, width=40)
        self._msg.pack(pady=(18, 8))
        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Splash.Horizontal.TProgressbar", troughcolor="#2b2b2b",
                        background="#88ff44", bordercolor=self.BG,
                        lightcolor="#88ff44", darkcolor="#88ff44")
        self._bar = ttk.Progressbar(r, mode="indeterminate", length=280,
                                    style="Splash.Horizontal.TProgressbar")
        self._bar.pack(padx=48, pady=(0, 28))
        r.update_idletasks()
        w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        r.geometry(f"+{(r.winfo_screenwidth() - w) // 2}+{(r.winfo_screenheight() - h) // 3}")
        self._bar.start(12)
        r.update()

    def run(self, fn):
        """Run fn() in a worker thread while keeping the splash animated.
        Returns fn's result; re-raises its exception (incl. SystemExit)."""
        box: dict = {}

        def work():
            try:
                box["result"] = fn()
            except BaseException as e:
                box["error"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            if self._msg.cget("text") != self.status:
                self._msg.config(text=self.status)
            self.root.update()
            time.sleep(0.015)
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def close(self):
        try:
            self._bar.stop()
            self.root.destroy()
        except Exception:
            pass


def show_error_dialog(msg: str):
    """Show a fatal error in a message box (windowed build has no console)."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(f"Chess Player {__version__}", msg)
        r.destroy()
    except Exception:
        pass
