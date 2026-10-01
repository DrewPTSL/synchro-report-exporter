"""
synchro report exporter - desktop UI

Run:   python synchro_exporter_ui.py      (or double-click launch.bat)
Needs: Windows, Python 3.9+, and   pip install pywinauto
Keep this file next to synchro_batch.py (the engine that drives Synchro).

Design notes
  * a quiet-to-loud layout: small lowercase labels at the top, a big START at the bottom
  * the header is a time-space diagram: diagonal "green wave" bands that drift while a run is active
  * every scenario is a signal lamp: hollow = waiting, lime = running, green = done, magenta = failed
  * Esc stops a run from anywhere (even while Synchro has the focus)

All Synchro automation happens on ONE background thread (COM must stay on the thread that created it);
this window only sends it commands and draws what it reports back.
"""
import ctypes
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

# crisp text on high-DPI screens (must happen before the first window exists)
try:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ------------------------------------------------------------------ palette
INK = "#0A121F"          # page
SURFACE = "#0F1A2C"      # fields
RAISED = "#15243B"       # hover
LINE = "#213453"         # hairlines
TEXT = "#E8EFFA"
MUTED = "#8196B6"
DIM = "#4B6286"
GREEN = "#3FE05B"        # go
LIME = "#C8F04A"         # active
MAGENTA = "#F23C8A"      # stop / failed
INDIGO = "#3D4BD8"       # selection
INDIGO_HI = "#5565F0"
VIOLET = "#967DFF"       # focus

SC = 1.0                 # screen scale (set once the window exists)
W, RAIL = 768, 96        # window width; left rail (12 x 8)
BODY_W = W - 64
CONTENT_W = BODY_W - RAIL
HANDS_OFF = "hands off the mouse and keyboard  \u00b7  esc stops"
FORMATS = {"pdf": (True, False), "txt": (False, True), "both": (True, True)}

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SynchroReportExporter"
SETTINGS = APP_DIR / "settings.json"
DEFAULT_FOLDER = str(Path.home() / "Documents" / "Synchro Reports")


def S(x):
    return int(round(x * SC))


def mix(a, b, t):
    a, b = a.lstrip("#"), b.lstrip("#")
    ra, rb = [int(a[i:i + 2], 16) for i in (0, 2, 4)], [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle from real arc points (Tk's own smoothing leaves chamfered corners)."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = []
    for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        for i in range(0, 7):
            a = math.radians(a0 + i * 15)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return cv.create_polygon(pts, **kw)


def fmt_time(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ------------------------------------------------------------------ widgets
class Btn(tk.Canvas):
    """Rounded button / chip drawn on a canvas."""

    def __init__(self, master, text, command, w, h, font, surround, fill=SURFACE, fg=TEXT, outline=LINE,
                 hover=RAISED, radius=10, anchor="center", pad=14, sub="", subfont=None, dot=None,
                 dfill=None, dfg=DIM, doutline=None):
        super().__init__(master, width=S(w), height=S(h), bg=surround, highlightthickness=0, bd=0)
        self.w, self.h, self.font, self.cmd = S(w), S(h), font, command
        self.opt = dict(text=text, fill=fill, fg=fg, outline=outline, hover=hover, radius=radius, anchor=anchor,
                        pad=pad, sub=sub, subfont=subfont or font, dot=dot,
                        dfill=dfill or SURFACE, dfg=dfg, doutline=doutline or LINE)
        self.enabled, self.over = True, False
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<Button-1>", self._click)
        self.draw()

    def style(self, **kw):
        enabled = kw.pop("enabled", None)
        self.opt.update(kw)
        if enabled is not None:
            self.enabled = enabled
        self.draw()

    def _hov(self, v):
        self.over = v
        self.draw()

    def _click(self, _):
        if self.enabled and self.cmd:
            self.cmd()

    def draw(self):
        o = self.opt
        self.delete("all")
        self.config(cursor="hand2" if self.enabled else "arrow")
        if self.enabled:
            fill = o["hover"] if self.over else o["fill"]
            fg, outline = o["fg"], o["outline"]
        else:
            fill, fg, outline = o["dfill"], o["dfg"], o["doutline"]
        round_rect(self, 1, 1, self.w - 1, self.h - 1, S(o["radius"]), fill=fill, outline=outline or fill, width=1)
        x = S(o["pad"])
        if o["dot"]:
            r = S(4)
            self.create_oval(x, self.h / 2 - r, x + 2 * r, self.h / 2 + r, fill=o["dot"], outline="")
            x += 2 * r + S(11)
        if o["anchor"] == "center":
            self.create_text(self.w / 2, self.h / 2, text=o["text"], font=self.font, fill=fg)
        else:
            self.create_text(x, self.h / 2, text=o["text"], font=self.font, fill=fg, anchor="w")
        if o["sub"]:
            self.create_text(self.w - S(o["pad"]), self.h / 2, text=o["sub"], font=o["subfont"],
                             fill=MUTED if self.enabled else DIM, anchor="e")


class Segmented(tk.Canvas):
    def __init__(self, master, options, value, command, w, h, font, surround):
        super().__init__(master, width=S(w), height=S(h), bg=surround, highlightthickness=0, bd=0)
        self.options, self.value, self.cmd, self.font = options, value, command, font
        self.w, self.h, self.hov, self.enabled = S(w), S(h), None, True
        self.bind("<Motion>", self._move)
        self.bind("<Leave>", lambda e: self._set_hov(None))
        self.bind("<Button-1>", self._click)
        self.draw()

    def _idx(self, x):
        pad = S(4)
        seg = (self.w - 2 * pad) / len(self.options)
        i = int((x - pad) // seg)
        return i if 0 <= i < len(self.options) else None

    def _move(self, e):
        self._set_hov(self._idx(e.x))

    def _set_hov(self, i):
        if i != self.hov:
            self.hov = i
            self.draw()

    def _click(self, e):
        i = self._idx(e.x)
        if self.enabled and i is not None:
            self.value = self.options[i][0]
            self.draw()
            if self.cmd:
                self.cmd(self.value)

    def set_enabled(self, v):
        self.enabled = v
        self.draw()

    def draw(self):
        self.delete("all")
        self.config(cursor="hand2" if self.enabled else "arrow")
        round_rect(self, 1, 1, self.w - 1, self.h - 1, S(12), fill=SURFACE, outline=LINE)
        pad = S(4)
        seg = (self.w - 2 * pad) / len(self.options)
        for i, (key, label) in enumerate(self.options):
            x1, x2 = pad + i * seg, pad + (i + 1) * seg
            sel = key == self.value
            if sel:
                round_rect(self, x1 + 1, pad + 1, x2 - 1, self.h - pad - 1, S(9),
                           fill=INDIGO if self.enabled else mix(INDIGO, INK, .55), outline="")
            colour = TEXT if sel else (TEXT if (self.hov == i and self.enabled) else MUTED)
            if not self.enabled:
                colour = mix(colour, INK, .5)
            self.create_text((x1 + x2) / 2, self.h / 2, text=label, font=self.font, fill=colour)


class Switch(tk.Canvas):
    def __init__(self, master, text, value, command, font, surround):
        self.font, self.text, self.value, self.cmd, self.enabled = font, text, value, command, True
        tw = font.measure(text)
        super().__init__(master, width=S(46) + S(11) + tw, height=S(28), bg=surround, highlightthickness=0, bd=0)
        self.bind("<Button-1>", self._click)
        self.draw()

    def _click(self, _):
        if self.enabled:
            self.value = not self.value
            self.draw()
            if self.cmd:
                self.cmd(self.value)

    def set_enabled(self, v):
        self.enabled = v
        self.draw()

    def draw(self):
        self.delete("all")
        self.config(cursor="hand2" if self.enabled else "arrow")
        h = S(28)
        on = self.value
        track = (INDIGO if on else LINE)
        knob = TEXT if on else MUTED
        if not self.enabled:
            track, knob = mix(track, INK, .5), mix(knob, INK, .5)
        round_rect(self, 1, S(4), S(46), h - S(4), S(10), fill=track, outline="")
        cx = S(46) - S(14) if on else S(14)
        r = S(8)
        self.create_oval(cx - r, h / 2 - r, cx + r, h / 2 + r, fill=knob, outline="")
        self.create_text(S(46) + S(11), h / 2, text=self.text, font=self.font, anchor="w",
                         fill=(TEXT if on else MUTED) if self.enabled else DIM)


class Header(tk.Canvas):
    """A time-space diagram: horizontal 'intersection' lines and diagonal green-wave bands."""
    BANDS = (15, 26, 34, 40, 88, 94)       # band positions across the width (out of 96)

    def __init__(self, master, fonts):
        self.h = S(96)
        super().__init__(master, width=S(W), height=self.h, bg=INK, highlightthickness=0, bd=0)
        self.f, self.off = fonts, 0.0
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = S(W), self.h
        bw, dx = S(46), S(150)
        span = w + bw + dx
        line = mix(INK, DIM, .40)
        rows = (h * 0.16, h * 0.60, h * 0.90)           # between the title and subtitle, clear of the text
        for y in rows:
            self.create_line(0, y, w, y, fill=line)
        band, edge, dot = mix(INK, GREEN, .085), mix(INK, GREEN, .30), mix(INK, LIME, .65)
        for p in self.BANDS:
            x = ((p / 96.0) * w + self.off) % span - bw
            self.create_polygon(x, h, x + bw, h, x + bw + dx, 0, x + dx, 0, fill=band, outline="")
            self.create_line(x, h, x + dx, 0, fill=edge)
            for y in rows:                            # signal timing points where the band crosses a line
                cx = x + bw / 2 + dx * (1 - y / h)
                self.create_oval(cx - 2, y - 2, cx + 2, y + 2, fill=dot, outline="")
        self.create_text(S(32), h * 0.40, text="synchro report exporter", font=self.f["title"], fill=TEXT, anchor="w")
        self.create_text(S(32), h * 0.73, text="every scenario, hands-free  \u00b7  pdf and txt", font=self.f["small"],
                         fill=MUTED, anchor="w")
        self.create_text(w - S(32), h * 0.40, text="v1.0", font=self.f["mono9"], fill=DIM, anchor="e")


class Lamps(tk.Canvas):
    """One signal lamp per scenario."""
    PITCH, R = 29, 8

    def __init__(self, master, w, fonts):
        self.fonts = fonts
        self.w = S(w)
        super().__init__(master, width=self.w, height=S(self.PITCH + 4), bg=INK, highlightthickness=0, bd=0)
        self.entries, self.state, self.pulse, self.hover, self.pos = [], {}, 0.0, None, []
        self.bind("<Motion>", self._move)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.layout()

    def apply_names(self, names, est, final):
        names = list(names)
        if not final and est and est > len(names):
            names += [None] * (est - len(names))
        self.entries = names
        self.layout()

    def ensure(self, name):
        if name not in self.entries:
            if None in self.entries:
                self.entries[self.entries.index(None)] = name
            else:
                self.entries.append(name)
            self.layout()

    def set_state(self, name, state):
        self.state[name] = state
        self.draw()

    def reset(self):
        self.state = {}
        self.draw()

    def layout(self):
        n, pitch = len(self.entries), S(self.PITCH)
        maxc = max(1, self.w // pitch)
        rows = max(1, math.ceil(n / maxc)) if n else 1
        cols = max(1, math.ceil(n / rows)) if n else 1       # balanced rows (11 + 10, not 20 + 1)
        self.pos = [(pitch / 2 + (i % cols) * pitch, pitch / 2 + (i // cols) * pitch) for i in range(n)]
        self.config(height=rows * pitch + S(4))
        self.draw()

    def _move(self, e):
        r = S(self.R) + S(5)
        hit = None
        for i, (x, y) in enumerate(self.pos):
            if (e.x - x) ** 2 + (e.y - y) ** 2 <= r * r:
                hit = i
                break
        self._set_hover(hit)

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.draw()

    def draw(self):
        self.delete("all")
        if not self.entries:
            self.create_text(S(4), S(self.PITCH) / 2, text="scenarios appear here once a run starts",
                             font=self.fonts["small"], fill=DIM, anchor="w")
            return
        r = S(self.R)
        for name, (x, y) in zip(self.entries, self.pos):
            st = self.state.get(name, "pending") if name else "unknown"
            if st == "active":
                pr = r + S(2) + S(2.5) * abs(math.sin(self.pulse))
                self.create_oval(x - pr, y - pr, x + pr, y + pr, outline=mix(INK, LIME, .45), width=1)
                self.create_oval(x - r, y - r, x + r, y + r, fill=LIME, outline="")
            elif st == "done":
                self.create_oval(x - r, y - r, x + r, y + r, fill=GREEN, outline="")
            elif st == "skipped":
                self.create_oval(x - r, y - r, x + r, y + r, fill=mix(INDIGO, INK, .35), outline="")
            elif st == "failed":
                self.create_oval(x - r, y - r, x + r, y + r, fill=MAGENTA, outline="")
                d = r * .42
                self.create_line(x - d, y - d, x + d, y + d, fill=INK, width=2)
                self.create_line(x - d, y + d, x + d, y - d, fill=INK, width=2)
            elif st == "stopped":
                self.create_oval(x - r, y - r, x + r, y + r, outline=MAGENTA, width=2)
            elif st == "unknown":
                self.create_oval(x - r, y - r, x + r, y + r, outline=DIM, dash=(2, 3))
            else:
                self.create_oval(x - r, y - r, x + r, y + r, outline=DIM, width=2)
        if self.hover is not None and self.hover < len(self.entries) and self.entries[self.hover]:
            name = self.entries[self.hover]
            x, y = self.pos[self.hover]
            label = f"{name}  \u00b7  {self.state.get(name, 'waiting').replace('pending', 'waiting')}"
            tw = self.fonts["small"].measure(label) + S(20)
            x1 = min(max(x - tw / 2, 1), self.w - tw - 1)
            y2 = max(y - r - S(5), S(24))
            round_rect(self, x1, y2 - S(24), x1 + tw, y2, S(7), fill=RAISED, outline=LINE)
            self.create_text(x1 + tw / 2, y2 - S(12), text=label, font=self.fonts["small"], fill=TEXT)


class Prog(tk.Canvas):
    def __init__(self, master, w):
        self.w, self.h = S(w), S(8)
        super().__init__(master, width=self.w, height=self.h, bg=INK, highlightthickness=0, bd=0)
        self.done = self.active = 0.0
        self.draw()

    def set(self, done, active=0.0):
        self.done, self.active = max(0, min(1, done)), max(0, min(1, active))
        self.draw()

    def draw(self):
        self.delete("all")
        round_rect(self, 0, 0, self.w, self.h, S(4), fill=LINE, outline="")
        x1 = self.w * self.done
        if self.done > 0:
            round_rect(self, 0, 0, max(x1, S(8)), self.h, S(4), fill=GREEN, outline="")
        if self.active > 0:
            x2 = min(self.w, x1 + self.w * self.active)
            round_rect(self, x1, 0, max(x2, x1 + S(8)), self.h, S(4), fill=LIME, outline="")


# ------------------------------------------------------------------ automation thread
class QueueLog(logging.Handler):
    def __init__(self, q):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class Automation(threading.Thread):
    """Owns every pywinauto / COM call. The UI talks to it only through queues."""

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out, self.cmd, self.engine = out, queue.Queue(), None

    def run(self):
        if not getattr(sys, "frozen", False):        # running as plain .py files: make sure the engine is the new one
            here = Path(__file__).resolve().parent / "synchro_batch.py"
            try:
                src = here.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                self.out.put(("fatal", f"synchro_batch.py was not found next to this file ({here.parent})"))
                return
            if "ENGINE_API" not in src:
                self.out.put(("fatal", "synchro_batch.py is out of date. Replace it with the new version "
                                       f"that came with this window (in {here.parent})"))
                return
        try:
            import synchro_batch as engine          # imported HERE so COM lives on this thread
        except Exception as e:
            self.out.put(("fatal", f"{type(e).__name__}: {e}"))
            return
        self.engine = engine
        engine.PROGRESS = lambda **kw: self.out.put(("event", kw))
        logging.getLogger().addHandler(QueueLog(self.out))
        self.out.put(("ready",))
        while True:
            name, args = self.cmd.get()
            if name == "quit":
                return
            try:
                if name == "scan":
                    try:
                        self.out.put(("projects", engine.list_projects()))
                    except BaseException as e:
                        self.out.put(("scan_error", f"{type(e).__name__}: {e}", traceback.format_exc()))
                elif name == "run":
                    engine.run_export(**args)
            except BaseException as e:               # report instead of dying silently
                self.out.put(("error", str(e) or type(e).__name__, traceback.format_exc()))


# ------------------------------------------------------------------ the app
class App:
    def __init__(self):
        global SC
        self.root = tk.Tk()
        self.root.title("synchro report exporter")
        self.root.configure(bg=INK)
        self.root.resizable(False, False)
        SC = max(1.0, min(3.0, self.root.winfo_fpixels("1i") / 96.0))
        self.make_fonts()

        self.q = queue.Queue()
        self.auto = Automation(self.q)
        self.projects, self.sel_handle = [], None
        self.ready = self.fatal = False
        self.running = self.stopping = False
        self.t0 = 0.0
        self.states, self.total, self.approx, self.step_text = {}, 0, False, ""
        self.cur_step = ""
        st = self.load_settings()
        self.folder_var = tk.StringVar(value=st.get("folder", DEFAULT_FOLDER))
        self.fmt_value = st.get("format", "both")
        self.skip_value = bool(st.get("skip", False))
        self.details_open = bool(st.get("details", False))

        self.build()
        self.center()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", lambda e: self.request_stop())
        self.root.bind("<FocusIn>", self.on_focus)
        self.auto.start()
        self.root.after(25000, self.watchdog)
        self.pump()
        self.tick()
        self.poll_esc()

    # ---- setup
    def make_fonts(self):
        have = set(tkfont.families())
        pick = lambda *n: next((x for x in n if x in have), n[-1])
        disp = pick("Bahnschrift SemiBold", "Bahnschrift", "Segoe UI Semibold", "DejaVu Sans")
        body = pick("Segoe UI", "Helvetica", "DejaVu Sans")
        mono = pick("Cascadia Mono", "Consolas", "DejaVu Sans Mono")
        F = tkfont.Font
        self.f = dict(
            title=F(family=disp, size=21), num=F(family=disp, size=24), big=F(family=disp, size=26),
            h=F(family=disp, size=12), btn=F(family=disp, size=11), start=F(family=disp, size=15),
            small=F(family=body, size=9), body=F(family=body, size=10), chip=F(family=disp, size=11),
            mono=F(family=mono, size=10), mono9=F(family=mono, size=9), cnt=F(family=disp, size=13),
        )

    def load_settings(self):
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_settings(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps(dict(
                folder=self.folder_var.get(), format=self.fmt_value, skip=self.skip_value,
                details=self.details_open)), encoding="utf-8")
        except Exception:
            pass

    def center(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = max(0, (self.root.winfo_screenwidth() - w) // 2)
        y = max(0, (self.root.winfo_screenheight() - h) // 3)
        self.root.geometry(f"+{x}+{y}")

    # ---- layout
    def section(self, num, caption, pady=(13, 13)):
        row = tk.Frame(self.body, bg=INK)
        row.pack(fill="x", pady=pady)
        row.grid_columnconfigure(0, minsize=S(RAIL))
        row.grid_columnconfigure(1, weight=1)
        rail = tk.Frame(row, bg=INK)
        rail.grid(row=0, column=0, sticky="nw")
        tk.Label(rail, text=num, font=self.f["num"], fg=DIM, bg=INK).pack(anchor="w")
        tk.Label(rail, text=caption, font=self.f["small"], fg=MUTED, bg=INK).pack(anchor="w")
        content = tk.Frame(row, bg=INK)
        content.grid(row=0, column=1, sticky="ew")
        tk.Frame(self.body, bg=LINE, height=1).pack(fill="x")
        return content

    def build(self):
        f = self.f
        self.header = Header(self.root, f)
        self.header.pack()
        self.body = tk.Frame(self.root, bg=INK)
        self.body.pack(fill="x", padx=S(32), pady=(6, 0))

        # 01 project
        c = self.section("01", "project")
        self.proj_btn = Btn(c, "looking for synchro\u2026", self.pick_project, CONTENT_W - 125, 44, f["chip"], INK,
                            anchor="w", dot=DIM)
        self.proj_btn.pack(side="left")
        self.scan_btn = Btn(c, "\u21bb  rescan", self.rescan, 118, 44, f["btn"], INK)
        self.scan_btn.pack(side="left", padx=(7, 0))

        # 02 folder
        c = self.section("02", "folder")
        self.entry_wrap = tk.Frame(c, bg=LINE, padx=1, pady=1)
        self.entry_wrap.pack(side="left", fill="x", expand=True)
        self.entry = tk.Entry(self.entry_wrap, textvariable=self.folder_var, bg=SURFACE, fg=TEXT,
                              insertbackground=GREEN, relief="flat", bd=0, highlightthickness=0, font=f["mono"],
                              disabledbackground=SURFACE, disabledforeground=DIM)
        self.entry.pack(fill="both", ipady=S(11), ipadx=S(10))
        self.entry.bind("<FocusIn>", lambda e: self.entry_wrap.config(bg=VIOLET))
        self.entry.bind("<FocusOut>", lambda e: self.entry_wrap.config(bg=LINE))
        self.folder_var.trace_add("write", lambda *a: self.refresh_idle())
        self.browse_btn = Btn(c, "browse\u2026", self.browse, 104, 44, f["btn"], INK)
        self.browse_btn.pack(side="left", padx=(7, 0))
        self.open_btn = Btn(c, "open", self.open_folder, 64, 44, f["btn"], INK)
        self.open_btn.pack(side="left", padx=(7, 0))

        # 03 formats
        c = self.section("03", "formats")
        self.seg = Segmented(c, [("pdf", ".pdf"), ("txt", ".txt"), ("both", ".pdf  +  .txt")], self.fmt_value,
                             self.on_format, 336, 44, f["btn"], INK)
        self.seg.pack(side="left")
        self.skip = Switch(c, "skip files that already exist", self.skip_value, self.on_skip, f["body"], INK)
        self.skip.pack(side="left", padx=(24, 0))

        # 04 run
        c = self.section("04", "run", pady=(13, 14))
        top = tk.Frame(c, bg=INK)
        top.pack(fill="x")
        cnt = tk.Frame(top, bg=INK)
        cnt.pack(side="right")
        self.cnt_done = tk.Label(cnt, text="00", font=f["big"], fg=TEXT, bg=INK)
        self.cnt_done.pack(side="left")
        self.cnt_total = tk.Label(cnt, text="/--", font=f["cnt"], fg=DIM, bg=INK)
        self.cnt_total.pack(side="left", anchor="s", pady=(0, 5))
        self.status = tk.Label(top, text="", font=f["h"], fg=TEXT, bg=INK, anchor="w", justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        self.lamps = Lamps(c, CONTENT_W, f)
        self.lamps.pack(fill="x", pady=(9, 9))
        self.prog = Prog(c, CONTENT_W)
        self.prog.pack(fill="x")
        foot = tk.Frame(c, bg=INK)
        foot.pack(fill="x", pady=(8, 0))
        self.elapsed = tk.Label(foot, text="00:00", font=f["mono9"], fg=DIM, bg=INK)
        self.elapsed.pack(side="left")
        self.hint = tk.Label(foot, text="", font=f["small"], fg=LIME, bg=INK)
        self.hint.pack(side="right")

        # action bar: the loud end of the page
        bar = tk.Frame(self.root, bg=INK)
        bar.pack(fill="x", padx=S(32), pady=(6, 0))
        self.start_btn = Btn(bar, "START EXPORT", self.start, BODY_W - 220, 56, f["start"], INK, fill=GREEN,
                             fg=INK, outline=GREEN, hover=mix(GREEN, "#ffffff", .18), radius=14,
                             dfill=RAISED, dfg=DIM, doutline=RAISED)
        self.start_btn.pack(side="left")
        self.stop_btn = Btn(bar, "STOP  \u00b7  ESC", self.request_stop, 209, 56, f["start"], INK, fill=MAGENTA,
                            fg="#ffffff", outline=MAGENTA, hover=mix(MAGENTA, "#ffffff", .18), radius=14,
                            dfill=INK, dfg=DIM, doutline=LINE)
        self.stop_btn.pack(side="left", padx=(11, 0))

        # details
        row = tk.Frame(self.root, bg=INK)
        row.pack(fill="x", padx=S(32), pady=(11, 12))
        self.det_lbl = tk.Label(row, text="", font=f["small"], fg=MUTED, bg=INK, cursor="hand2")
        self.det_lbl.pack(side="left")
        self.det_lbl.bind("<Button-1>", lambda e: self.toggle_details())
        link = tk.Label(row, text="log file \u2197", font=f["small"], fg=DIM, bg=INK, cursor="hand2")
        link.pack(side="right")
        link.bind("<Button-1>", lambda e: self.open_path(APP_DIR))
        self.logbox = tk.Text(self.root, height=8, bg=SURFACE, fg=MUTED, font=f["mono9"], relief="flat", bd=0,
                              highlightthickness=1, highlightbackground=LINE, padx=S(12), pady=S(9), wrap="none",
                              state="disabled", insertwidth=0)
        self.logbox.tag_config("bad", foreground=MAGENTA)
        self.logbox.tag_config("good", foreground=GREEN)
        self.apply_details(first=True)
        self.refresh_idle()
        self.update_controls()

    # ---- details panel
    def apply_details(self, first=False):
        self.det_lbl.config(text="details  \u25be" if not self.details_open else "details  \u25b4")
        if self.details_open:
            self.logbox.pack(fill="x", padx=S(32), pady=(0, 16))
        else:
            self.logbox.pack_forget()
        if not first:
            self.root.geometry("")

    def open_details(self):
        if not self.details_open:
            self.details_open = True
            self.apply_details()

    def toggle_details(self):
        self.details_open = not self.details_open
        self.apply_details()
        self.save_settings()

    def log_line(self, text):
        tag = ()
        if "FAILED" in text or "WARNING" in text or "Stopped" in text:
            tag = ("bad",)
        elif "Finished" in text:
            tag = ("good",)
        self.logbox.config(state="normal")
        self.logbox.insert("end", text + "\n", tag)
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    # ---- controls
    def on_format(self, v):
        self.fmt_value = v
        self.save_settings()
        self.refresh_idle()

    def on_skip(self, v):
        self.skip_value = v
        self.save_settings()

    def browse(self):
        start = self.folder_var.get().strip().strip('"')
        d = filedialog.askdirectory(title="Where should the reports go?",
                                    initialdir=start if os.path.isdir(start) else str(Path.home()))
        if d:
            self.folder_var.set(os.path.normpath(d))
            self.save_settings()

    def open_folder(self):
        self.open_path(self.folder_var.get().strip().strip('"'))

    def open_path(self, p):
        try:
            if os.path.isdir(str(p)):
                os.startfile(str(p))
        except Exception:
            pass

    def rescan(self):
        if self.ready and not self.running:
            self.proj_btn.style(text="looking for synchro\u2026", dot=DIM, sub="")
            self.auto.cmd.put(("scan", None))

    def on_focus(self, e):
        if e.widget is self.root and self.ready and not self.running and self.sel_handle is None \
                and time.time() - getattr(self, "_last_scan", 0) > 2:
            self._last_scan = time.time()
            self.auto.cmd.put(("scan", None))

    def pick_project(self):
        if self.running or len(self.projects) < 2:
            return
        m = tk.Menu(self.root, tearoff=0, bg=SURFACE, fg=TEXT, activebackground=INDIGO, activeforeground=TEXT,
                    bd=0, font=self.f["body"])
        for h, t in self.projects:
            m.add_command(label=self.project_label(t)[0], command=lambda hh=h: self.select_project(hh))
        x, y = self.proj_btn.winfo_rootx(), self.proj_btn.winfo_rooty() + self.proj_btn.winfo_height()
        m.tk_popup(x, y)

    @staticmethod
    def project_label(title):
        m = re.match(r"^(Synchro \d+) - (.*)$", title)
        ver, path = (m.group(1).lower(), m.group(2)) if m else ("synchro", title)
        return path.replace("/", "\\").split("\\")[-1] or title, ver

    def select_project(self, handle):
        self.sel_handle = handle
        title = next((t for h, t in self.projects if h == handle), "")
        name, ver = self.project_label(title)
        self.proj_btn.style(text=name, dot=GREEN, sub=ver + ("  \u25be" if len(self.projects) > 1 else ""))
        self.refresh_idle()
        self.update_controls()

    # ---- state
    def can_start(self):
        return (self.ready and not self.fatal and not self.running and self.sel_handle is not None
                and bool(self.folder_var.get().strip()))

    def update_controls(self):
        run = self.running
        self.start_btn.style(text="RUNNING\u2026" if run else "START EXPORT", enabled=self.can_start())
        self.stop_btn.style(text="STOPPING\u2026" if self.stopping else "STOP  \u00b7  ESC",
                            enabled=run and not self.stopping)
        self.scan_btn.style(enabled=self.ready and not run)
        self.browse_btn.style(enabled=not run)
        self.seg.set_enabled(not run)
        self.skip.set_enabled(not run)
        self.entry.config(state="disabled" if run else "normal")

    def set_status(self, text, colour=TEXT):
        self.status.config(text=text, fg=colour)

    def refresh_idle(self):
        if self.running or getattr(self, "finished_msg", False):
            return
        if self.fatal:
            return
        if not self.ready:
            self.set_status("starting up\u2026", MUTED)
        elif self.sel_handle is None:
            self.set_status("open a synchro project, then press rescan", MUTED)
        elif not self.folder_var.get().strip():
            self.set_status("choose a folder for the reports", MUTED)
        else:
            n = {"pdf": "pdf files", "txt": "text files", "both": "pdf and text files"}[self.fmt_value]
            self.set_status(f"ready \u00b7 {n} for every scenario", MUTED)
        self.update_controls()

    # ---- run / stop
    def start(self):
        if not self.can_start():
            return
        folder = os.path.expanduser(self.folder_var.get().strip().strip('"'))
        pdf, txt = FORMATS[self.fmt_value]
        self.running, self.stopping, self.finished_msg = True, False, False
        self.t0 = time.time()
        self.states, self.total, self.approx, self.cur_step = {}, 0, False, ""
        self.lamps.reset()
        self.cnt_done.config(text="00", fg=TEXT)
        self.cnt_total.config(text="/--")
        self.prog.set(0)
        self.hint.config(text=HANDS_OFF, fg=LIME)
        self.set_status("starting\u2026", TEXT)
        self.update_controls()
        self.auto.cmd.put(("run", dict(handle=self.sel_handle, out_dir=folder, pdf=pdf, txt=txt,
                                       resume=self.skip_value)))
        self.save_settings()

    def request_stop(self):
        if not self.running or self.stopping:
            return
        self.stopping = True
        if self.auto.engine:
            self.auto.engine.STOP.set()
        note = "stopping\u2026 "
        if self.cur_step == "pdf-render":
            note += "synchro may keep rendering the current pdf for a moment"
        self.set_status(note, MAGENTA)
        self.update_controls()

    def poll_esc(self):
        """Esc from anywhere stops the run (Esc the engine sends to Synchro itself is ignored)."""
        try:
            if self.running and not self.stopping:
                eng = self.auto.engine
                ours = eng is not None and time.time() < eng._synthetic_until[0]
                if not ours and ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:
                    self.request_stop()
        except Exception:
            pass
        self.root.after(40, self.poll_esc)

    def watchdog(self):
        if not self.ready and not self.fatal:
            self.proj_btn.style(text="still starting\u2026 see details", dot=MAGENTA, sub="")
            self.set_status("the automation engine is taking unusually long to load", MAGENTA)
            self.log_line("The automation engine has not finished loading after 25 seconds. "
                          "If a black console window is waiting for input, that is an old synchro_batch.py.")
            self.open_details()

    # ---- messages from the automation thread
    def pump(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def handle(self, msg):
        kind = msg[0]
        if kind == "ready":
            self.ready = True
            self.auto.cmd.put(("scan", None))
            self.refresh_idle()
        elif kind == "fatal":
            self.fatal = True
            self.proj_btn.style(text="can't start the automation engine", dot=MAGENTA, sub="")
            self.set_status(msg[1], MAGENTA)
            self.status.config(wraplength=S(CONTENT_W - 110))
            if "pywinauto" in msg[1].lower() or "No module" in msg[1]:
                self.hint.config(text="install it with:  pip install pywinauto", fg=MAGENTA)
            self.log_line("FAILED: " + msg[1])
            self.open_details()
            self.update_controls()
        elif kind == "scan_error":
            self.proj_btn.style(text="couldn't look for synchro \u2014 see details", dot=MAGENTA, sub="")
            self.set_status(msg[1][:90], MAGENTA)
            self.log_line("FAILED: " + msg[1])
            self.log_line(msg[2])
            self.open_details()
            self.update_controls()
        elif kind == "projects":
            self.projects = msg[1]
            keep = next((h for h, _ in self.projects if h == self.sel_handle), None)
            if self.projects:
                self.select_project(keep or self.projects[0][0])
            else:
                self.sel_handle = None
                self.proj_btn.style(text="no synchro project is open", dot=MAGENTA, sub="")
                self.update_controls()
            self.refresh_idle()
        elif kind == "log":
            self.log_line(msg[1])
        elif kind == "error":                          # unexpected crash in the automation thread
            self.log_line("FAILED: " + msg[1])
            self.log_line(msg[2])
            self.running = self.stopping = False
            self.finished_msg = True
            self.set_status(msg[1].splitlines()[0][:90], MAGENTA)
            self.update_controls()
        elif kind == "event":
            self.on_event(msg[1])

    def on_event(self, e):
        k = e.get("kind")
        if k == "status":
            self.set_status(e["text"], TEXT)
        elif k == "scenarios":
            self.lamps.apply_names(e["names"], e.get("est"), e.get("final"))
            if e.get("est") and not self.total:
                self.cnt_total.config(text=f"/~{e['est']}")
        elif k == "step":
            self.cur_step = e["step"]
            label = {"select": "switching to", "pdf-render": "rendering pdf \u00b7",
                     "txt-save": "saving text \u00b7"}.get(e["step"], e["step"])
            if not self.stopping:
                self.set_status(f"{label} {e['name']}", TEXT)
                self.hint.config(text=("synchro freezes while a pdf renders  \u00b7  esc stops"
                                       if e["step"] == "pdf-render" else HANDS_OFF), fg=LIME)
        elif k == "scenario":
            name, state = e["name"], e["state"]
            self.lamps.ensure(name)
            self.lamps.set_state(name, state)
            self.states[name] = state
            self.total, self.approx = e["total"], e.get("approx", False)
            finished = sum(1 for s in self.states.values() if s in ("done", "skipped", "failed"))
            self.cnt_done.config(text=f"{finished:02d}")
            self.cnt_total.config(text=f"/{'~' if self.approx else ''}{self.total}")
            self.prog.set(finished / max(1, self.total), (1 / max(1, self.total)) if state == "active" else 0)
            if state == "active" and not self.stopping:
                self.set_status(name, TEXT)
        elif k == "finished":
            self.on_finished(e["summary"])

    def on_finished(self, s):
        self.running = self.stopping = False
        el = fmt_time(time.time() - self.t0)
        failed = len(s.get("failed", []))
        ok = False
        if s.get("error"):
            msg, col = s["error"].splitlines()[0][:90], MAGENTA
        elif s.get("stopped"):
            msg, col = f"stopped \u00b7 {s.get('done', 0)} exported before you pressed stop", MAGENTA
            if self.cur_step == "pdf-render":
                msg = "stopped \u00b7 the pdf that was rendering may be incomplete"
        elif failed:
            msg, col = f"finished in {el} \u00b7 {s.get('done', 0)} exported \u00b7 {failed} failed (see details)", MAGENTA
        else:
            skipped = f" \u00b7 {s['skipped']} skipped" if s.get("skipped") else ""
            msg, col, ok = f"done \u00b7 {s.get('done', 0)} scenarios exported in {el}{skipped}", GREEN, True
        self.finished_msg = True
        self.set_status(msg, col)
        self.hint.config(text="")
        if ok:
            self.prog.set(1.0)
        self.update_controls()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(400, lambda: self.root.attributes("-topmost", False))

    # ---- animation
    def tick(self):
        if self.running:
            self.header.off += S(1.5)
            self.header.draw()
            self.lamps.pulse += 0.28
            self.lamps.draw()
            self.elapsed.config(text=fmt_time(time.time() - self.t0))
        self.root.after(60, self.tick)

    def on_close(self):
        if self.running:
            self.request_stop()
        self.save_settings()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
