"""
Synchro 12 batch report exporter (pywinauto, UIA backend).

Usage:
    python synchro_batch.py                 # runs ALL scenarios into OUTPUT_DIR (set in the config)
    python synchro_batch.py --project XXXXXX     # pick the project when several are open
    python synchro_batch.py --pick          # choose the output folder in a picker window
    python synchro_batch.py --out "D:\\Reports"
    python synchro_batch.py --only "EX AM" "BG (5Y) PM"     # just these
    python synchro_batch.py --list          # just print the scenarios found, then exit
    python synchro_batch.py --resume        # skip scenarios whose .pdf and .txt already exist

For each scenario:
    1. switch scenario
    2. Create Report -> Print   -> "Save Print Output As" -> <name>.pdf   (Synchro freezes while it renders)
    3. Create Report -> Save Text -> save dialog          -> <name>.txt

Run with Synchro open on the right project and DON'T touch mouse/keyboard while it runs.
Tip: test with --only "EX AM" first.
"""
ENGINE_API = 2          # the UI checks this; bump it when the UI/engine interface changes

import argparse
import ctypes
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from pywinauto import Application, Desktop, mouse
from pywinauto.keyboard import send_keys

# ------------------------------------------------------------------ CONFIG
# Matches any open Synchro project window (any version / project number).
# If several projects are open you'll be asked which one, or set PROJECT_FILTER / use --project.
PROJECT_TITLE_RE = r"^Synchro \d+ - .*\.syn.*$"
PROJECT_FILTER = ""             # e.g. "260000" to only match titles containing this text

# Default output folder (the UI remembers your last choice; the command line uses OUTPUT_DIR below)
DEFAULT_REPORT_DIR = Path.home() / "Documents" / "Synchro Reports"

# Optional: scenario names to add on top of what the dropdown exposes (empty = only what Synchro shows).
EXTRA_SCENARIOS = []

# Output folder used for every run. Change it here, or override with --out "path", or use --pick
# to open a folder picker instead.
OUTPUT_DIR = str(DEFAULT_REPORT_DIR)
ASK_TO_CONFIRM = True           # False = start immediately without pressing Enter

OVERWRITE = True                # delete existing files first (avoids the "Confirm Save As" prompt)

PDF_TIMEOUT = 30 * 60           # seconds to wait for a PDF + Synchro to unfreeze
TXT_TIMEOUT = 5 * 60
STABLE_SECS = 1                 # PDF size must stop changing for this long
TXT_STABLE_SECS = .25             # same, for the small .txt

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SynchroReportExporter"
try:
    APP_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    APP_DIR = Path(__file__).resolve().parent
LOG_FILE = APP_DIR / "export.log"

# Used when you press the play button in VS Code (no command-line arguments).
# Examples:  ["--list"]   ["--select-only", "TT (5Y) AM"]   ["--only", "EX AM"]   ["--add", "TT (5Y) AM", "TT (5Y) PM"]   []  (= run everything)
RUN_ARGS = []
# ---------------------------------------------------------------------------

_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stderr is not None:                       # no console when running as a windowed .exe
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", handlers=_handlers)
log = logging.info

desk = Desktop(backend="uia")
main_spec = HWND = PID = app = main = None      # set by connect()

# ---- stop support, progress events, and Esc handling (used by the UI)
STOP = threading.Event()
PROGRESS = lambda **kw: None                     # the UI replaces this to receive events


class Stopped(BaseException):
    """Raised inside the worker when the user presses Stop / Esc (BaseException so that
    `except Exception` blocks in the helpers can't swallow it)."""


_ignore_stop = False
_synthetic_until = [0.0]


def emit(**kw):
    try:
        PROGRESS(**kw)
    except Exception:
        pass


def check_stop():
    if STOP.is_set() and not _ignore_stop:
        raise Stopped()


def nap(seconds):
    """sleep() that notices a stop request."""
    check_stop()
    time.sleep(seconds)
    check_stop()


def _send(keys, **kw):
    """send_keys() that tells escape_pressed() the next Esc is ours, not the user's."""
    _synthetic_until[0] = time.time() + 0.8
    send_keys(keys, **kw)


def escape_pressed():
    """True while the USER is physically holding Esc (Esc we send ourselves is ignored)."""
    if time.time() < _synthetic_until[0]:
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000)
    except Exception:
        return False


def list_projects():
    """[(handle, title)] for every open Synchro project window."""
    return [(w.handle, w.window_text()) for w in desk.windows(title_re=PROJECT_TITLE_RE)]


def connect(handle):
    global main_spec, HWND, PID, app, main
    HWND = handle
    app = Application(backend="uia").connect(handle=handle)
    main = app.window(handle=handle)
    main_spec = main.wrapper_object()
    PID = main_spec.process_id()
    _cache.clear()
    log(f"Connected to: {main_spec.window_text()}")


def _find_project_window():
    """Command line: find the Synchro project window; ask which one if several are open."""
    args = RUN_ARGS if len(sys.argv) == 1 else sys.argv[1:]
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--project")
    flt = (pre.parse_known_args(args)[0].project or PROJECT_FILTER or "").lower()

    wins = [(h, t) for h, t in list_projects() if flt in t.lower()]
    if not wins:
        raise SystemExit("No open Synchro project window found"
                         + (f" matching {flt!r}" if flt else "") + ". Open your project first.")
    if len(wins) > 1:
        print("\nSeveral Synchro projects are open:")
        for i, (_, t) in enumerate(wins, 1):
            print(f"  {i}. {t}")
        choice = input("Which one? Enter a number: ").strip()
        wins = [wins[int(choice) - 1]]
    return wins[0][0]


# ------------------------------------------------------------------ helpers
def is_hung():
    return bool(ctypes.windll.user32.IsHungAppWindow(HWND))


def file_stem(scenario):
    """'TT (10Y) PM' -> 'TT (10Y)_PM'  (matches your existing file names)"""
    head, tail = scenario.rsplit(" ", 1)
    return f"{head}_{tail}"


REPORT_DIR = DEFAULT_REPORT_DIR   # set at start-up by main_loop()


def wait_for_file(path, since, timeout, stable_secs=STABLE_SECS, poll=0.5):
    """Wait until file exists, is newer than `since`, and its size stops changing."""
    end = time.time() + timeout
    last, stable_since = -1, None
    while time.time() < end:
        if path.exists() and path.stat().st_mtime >= since - 2:
            size = path.stat().st_size
            if size > 0 and size == last:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= stable_secs:
                    return
            else:
                last, stable_since = size, None
        nap(poll)
    raise TimeoutError(f"{path.name} did not finish within {timeout}s")


def wait_until_responsive(timeout):
    """After the PDF is written, wait for Synchro to unfreeze and show its ribbon again."""
    end = time.time() + timeout
    while time.time() < end:
        if not is_hung():
            try:
                if main.child_window(title="Create Report", control_type="Button").exists(timeout=1):
                    return
            except Exception:
                pass
        nap(1)
    raise TimeoutError("Synchro did not become responsive again")


def find_by_id(container, auto_id, control_type, title=None):
    """descendants() can't filter on auto_id, so filter on the element info ourselves."""
    hits = []
    for w in container.descendants(control_type=control_type):
        if w.element_info.automation_id != auto_id:
            continue
        if title is not None and w.window_text() != title:
            continue
        hits.append(w)
    return hits


def _keys_escape(text):
    return "".join("{" + c + "}" if c in "{}()+^%~" else c for c in text)


# ------------------------------------------------------------------ scenarios
_cache = {}


def _combo():
    w = _cache.get("combo")
    if w is None:
        item = main.child_window(auto_id="BarEditItemLinkscenarioComboBox", control_type="Button")
        w = _cache["combo"] = item.child_window(control_type="ComboBox").wrapper_object()
    return w


def _dd_button():
    w = _cache.get("dd")
    if w is None:
        item = main.child_window(auto_id="BarEditItemLinkscenarioComboBox", control_type="Button")
        w = _cache["dd"] = item.child_window(auto_id="PART_Item", control_type="Button").wrapper_object()
    return w


def current_scenario():
    for _ in range(2):
        try:
            return _combo().iface_value.CurrentValue or None
        except Exception:
            _cache.pop("combo", None)      # stale reference: look it up again
    return None


def _refetch_list(deep=False):
    """Find the open dropdown's popup list (one walk of the main window's tree)."""
    try:
        lists = main.wrapper_object().descendants(control_type="List")
    except Exception:
        lists = []
    if lists:
        return lists[0]
    if deep:                               # fallback: popup as its own top-level window
        for w in desk.windows(process=PID):
            lst = desk.window(handle=w.handle).child_window(control_type="List")
            if lst.exists(timeout=0.3):
                return lst.wrapper_object()
    return None


def _click_dropdown_button():
    try:
        _dd_button().click_input()
    except Exception:
        _cache.pop("dd", None)
        _dd_button().click_input()


def _open_dropdown(retries=3):
    """Open the scenario dropdown (or reuse it if already open) and return the popup list wrapper."""
    main.set_focus()
    lst = _refetch_list()
    if lst is not None:            # already open: clicking again would CLOSE it
        return lst
    for _ in range(retries):
        _click_dropdown_button()
        for _ in range(12):        # wait up to ~3s for the popup to appear
            nap(0.25)
            lst = _refetch_list()
            if lst is not None:
                return lst
    lst = _refetch_list(deep=True)
    if lst is not None:
        return lst
    raise RuntimeError("Scenario dropdown did not open")


def _close_dropdown():
    """Close the dropdown without changing the scenario, and confirm it is closed."""
    if _refetch_list() is None:
        return
    for _ in range(2):
        _send("{ESC}")
        nap(0.3)
        if _refetch_list() is None:
            return
    _click_dropdown_button()       # Esc didn't do it: toggle with the button
    nap(0.5)


def _realized(lst):
    return [li.window_text() for li in lst.children(control_type="ListItem") if li.window_text()]


SCENARIO_ORDER = []      # full dropdown order (alphabetical), set in main_loop()
DISCOVER_HIDDEN = True   # find scenarios the list doesn't expose, by clicking on-screen rows
_pitch = {"px": 20, "rows": 15}   # row height and rows visible in the popup (measured at start-up)


def _measure_popup(lst):
    items = lst.children(control_type="ListItem")
    if len(items) >= 2:
        p = items[1].rectangle().top - items[0].rectangle().top
        if 10 <= p <= 60:
            _pitch["px"] = p
    r = lst.rectangle()
    _pitch["rows"] = max(1, (r.bottom - r.top) // _pitch["px"])
    # total items ~ (rows visible) / (fraction of the list that is visible)
    _pitch["total"] = None
    try:
        view = float(lst.iface_scroll.CurrentVerticalViewSize)
        if view > 0:
            _pitch["total"] = round(((r.bottom - r.top) / _pitch["px"]) / (view / 100.0))
    except Exception:
        pass


def get_all_scenarios():
    """Names the dropdown exposes to automation (only what is on screen, ~17), plus EXTRA_SCENARIOS."""
    lst = _open_dropdown()
    names = _realized(lst)
    _measure_popup(lst)
    _close_dropdown()              # without changing the scenario
    merged = list(names)
    for n in EXTRA_SCENARIOS:
        if n not in merged:
            merged.append(n)
    return sorted(merged)          # dropdown is a plain alphabetical sort


def _wait_for_scenario(name, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if current_scenario() == name:
            return True
        nap(0.3)
    return False


# ---- position-based clicking, for rows the list doesn't expose to automation
def _scroll_percent(lst):
    try:
        return float(lst.iface_scroll.CurrentVerticalScrollPercent)
    except Exception:
        return None


def _scroll_popup(lst, to):
    """Wheel-scroll the popup to 'top' or 'bottom', re-checking its scroll percentage until it gets there."""
    rect = lst.rectangle()
    pt = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    goal = 100.0 if to == "bottom" else 0.0
    for _ in range(15):
        pct = _scroll_percent(lst)
        if pct is not None and abs(pct - goal) < 0.5:
            return True
        mouse.scroll(coords=pt, wheel_dist=-10 if to == "bottom" else 10)
        nap(0.4)
    pct = _scroll_percent(lst)
    return pct is None or abs(pct - goal) < 0.5


def _click_edge_row(n, edge):
    """Open the dropdown, scroll it to `edge` ('top'/'bottom') and click the n-th row from that edge."""
    lst = _open_dropdown()
    if not _scroll_popup(lst, edge):
        log("   warning: could not confirm the dropdown scrolled all the way")
    nap(0.3)
    rect = lst.rectangle()
    pitch = _pitch["px"]
    x = (rect.left + rect.right) // 2
    if edge == "top":
        y = rect.top + pitch // 2 + pitch * n + 2
    else:
        y = rect.bottom - pitch // 2 - pitch * n - 2
    mouse.click(button="left", coords=(x, y))


def _value_after_click(before, timeout=12):
    """Wait for the combo to change after a click; make sure the popup is closed; return the new value."""
    end = time.time() + timeout
    val = before
    while time.time() < end:
        nap(0.4)
        val = current_scenario()
        if val != before:
            break
    nap(0.5)
    if _refetch_list() is not None:
        _close_dropdown()
    return val


def bottom_pass(exposed_set, handle):
    """Click the rows up from the bottom of the dropdown. Every click SELECTS a scenario, so
    `handle(name)` is called right away for each one (export it, or just note it).
    Stops after the first click that lands on a scenario the list already exposed.
    Returns the names the list did NOT expose."""
    hidden, landed = [], []
    for n in range(_pitch["rows"]):
        before = current_scenario()
        _click_edge_row(n, "bottom")
        name = _value_after_click(before)
        log(f"   bottom row {n}: {name!r}")
        if not name or name in landed:          # nothing selected / same scenario twice: click didn't move
            break
        landed.append(name)
        if name not in exposed_set:
            hidden.append(name)
        handle(name)
        if name in exposed_set:
            break
    return sorted(hidden)


def _select_by_position(name, timeout=15):
    order = SCENARIO_ORDER
    idx, total = order.index(name), len(order)
    reach = _pitch["rows"] - 2                      # rows safely clickable from an edge
    if idx <= reach:
        edge, n = "top", idx
    elif total - 1 - idx <= reach:
        edge, n = "bottom", total - 1 - idx
    else:
        raise RuntimeError(f"{name!r} is out of reach of position-based clicking")
    for _ in range(3):
        before = current_scenario()
        _click_edge_row(n, edge)
        got = _value_after_click(before, timeout)
        if got == name:
            return
        if got in order:                            # off by some rows: correct using the known order
            shift = order.index(got) - idx
            n = max(0, n + shift if edge == "bottom" else n - shift)
            log(f"   landed on {got!r}, retrying {n} rows from the {edge}")
    raise RuntimeError(f"Scenario switch failed: showing {current_scenario()!r}, wanted {name!r}")


def _item_visible(item, lst):
    """True if the item's centre is inside the popup's visible area (the list exposes a few rows
    that are scrolled out of view; clicking those would miss)."""
    try:
        r, lr = item.rectangle(), lst.rectangle()
        cy = (r.top + r.bottom) // 2
        return lr.top <= cy - 3 and cy + 3 <= lr.bottom
    except Exception:
        return False


def select_scenario(name, timeout=3):
    if current_scenario() == name:
        return
    lst = _open_dropdown()

    # 1) item the list exposes AND that is on screen: real mouse click (the combo updates within ~1s)
    found = lst.children(title=name, control_type="ListItem")
    if found and _item_visible(found[0], lst):
        found[0].click_input()
        if _wait_for_scenario(name, timeout):
            return
    if _refetch_list() is not None:
        _close_dropdown()

    # 2) otherwise click its on-screen position (keyboard input goes to the map, so no keyboard fallback)
    if name in SCENARIO_ORDER:
        _select_by_position(name)
        return
    raise RuntimeError(f"{name!r} is not in the scenario list")


# ------------------------------------------------------------------ dialogs
def close_report_dialog():
    dlg = main.child_window(title="Create Report", control_type="Window")
    if dlg.exists(timeout=0.1):
        dlg.child_window(auto_id="Cancel", control_type="Button").click_input()
        nap(0.3)


def open_report_dialog(timeout=15):
    main.set_focus()
    main.child_window(title="Create Report", control_type="Button").invoke()
    end = time.time() + timeout
    while time.time() < end:
        dlg = main.child_window(title="Create Report", control_type="Window")
        if dlg.exists(timeout=0.5):
            return dlg
        nap(0.3)
    raise RuntimeError("Create Report dialog did not appear")


def find_save_dialog(title=None, timeout=20):
    """Return a wrapper for the Windows Save As dialog (child of the main window)."""
    end = time.time() + timeout
    while time.time() < end:
        if title:
            d = main.child_window(title=title, control_type="Window")
            if d.exists(timeout=0.3):
                return d.wrapper_object()
        else:
            for w in main.children(control_type="Window"):
                if w.window_text() == "Create Report":
                    continue
                if find_by_id(w, "FileNameControlHost", "ComboBox"):
                    return w
        nap(0.3)
    raise RuntimeError(f"Save dialog {title or '(any)'} did not appear")


def _filename_edit(dialog):
    """The file name box. Search via its small combo host: the file list holds thousands of Edit cells."""
    hosts = find_by_id(dialog, "FileNameControlHost", "ComboBox")
    if hosts:
        edits = hosts[0].descendants(control_type="Edit")
        if edits:
            return edits[0]
    return find_by_id(dialog, "1001", "Edit")[0]      # slow fallback


def save_as(dialog, path):
    edit = _filename_edit(dialog)
    try:
        edit.set_edit_text(str(path))
    except Exception:
        edit.set_focus()
        _send("^a{BACKSPACE}", pause=0.02)
        _send(_keys_escape(str(path)), with_spaces=True, pause=0.01)
    nap(0.3)
    find_by_id(dialog, "1", "Button", title="Save")[0].click_input()


def recover():
    """Best-effort cleanup after a failure/stop so the next scenario starts clean."""
    global _ignore_stop
    if is_hung():
        return                                   # Synchro is busy rendering; touching it would just hang
    _ignore_stop = True
    try:
        try:
            for d in main.children(control_type="Window"):
                if d.window_text() != "Create Report":
                    btns = find_by_id(d, "2", "Button", title="Cancel")
                    if btns:
                        btns[0].click_input()
        except Exception:
            pass
        try:
            if _refetch_list() is not None:
                _close_dropdown()
        except Exception:
            pass
        try:
            close_report_dialog()
        except Exception:
            pass
    finally:
        _ignore_stop = False


# ------------------------------------------------------------------ exports
def export_pdf(scn):
    path = REPORT_DIR / f"{file_stem(scn)}.pdf"
    if OVERWRITE and path.exists():
        path.unlink()
    close_report_dialog()
    dlg = open_report_dialog()
    t0 = time.time()
    dlg.child_window(auto_id="PrintReport", control_type="Button").click_input()
    save_as(find_save_dialog("Save Print Output As"), path)
    log(f"   pdf: saving {path.name} (Synchro may freeze)...")
    emit(kind="step", name=scn, step="pdf-render")
    wait_for_file(path, t0, PDF_TIMEOUT)
    wait_until_responsive(PDF_TIMEOUT)


def export_txt(scn):
    path = REPORT_DIR / f"{file_stem(scn)}.txt"
    if OVERWRITE and path.exists():
        path.unlink()
    close_report_dialog()
    dlg = open_report_dialog()
    t0 = time.time()
    dlg.child_window(title="Save Text", control_type="Button").click_input()
    save_as(find_save_dialog(None), path)
    log(f"   txt: saving {path.name}...")
    emit(kind="step", name=scn, step="txt-save")
    wait_for_file(path, t0, TXT_TIMEOUT, stable_secs=TXT_STABLE_SECS, poll=0.25)
    close_report_dialog()      # in case the Create Report dialog is still open


DO_PDF, DO_TXT, RESUME = True, True, False


def _target(scn, ext):
    return REPORT_DIR / f"{file_stem(scn)}.{ext}"


def _all_done(scn):
    return ((not DO_PDF or _target(scn, "pdf").exists()) and
            (not DO_TXT or _target(scn, "txt").exists()))


def process_scenario(scn, pos, already_selected, failures):
    """Select (unless a click already did) and export one scenario. Returns 'done'/'skipped'/'failed'.
    Only Stopped propagates."""
    n, total, approx = pos
    tag = f"[{n}/{'~' if approx else ''}{total}]"
    info = dict(name=scn, n=n, total=total, approx=approx)
    if RESUME and _all_done(scn):
        log(f"{tag} {scn}: already exported, skipping")
        emit(kind="scenario", state="skipped", **info)
        return "skipped"
    log(f"{tag} {scn}")
    emit(kind="scenario", state="active", **info)
    try:
        if not already_selected:
            emit(kind="step", name=scn, step="select")
            t = time.time()
            select_scenario(scn)
            log(f"   scenario selected ({time.time() - t:.1f}s)")
        for flag, ext, step, fn in ((DO_PDF, "pdf", "pdf", export_pdf), (DO_TXT, "txt", "txt", export_txt)):
            if not flag:
                continue
            if RESUME and _target(scn, ext).exists():
                log(f"   {ext}: already exists, skipping")
                continue
            check_stop()
            t = time.time()
            fn(scn)
            log(f"   {ext} done ({time.time() - t:.1f}s)")
        emit(kind="scenario", state="done", **info)
        return "done"
    except Stopped:
        emit(kind="scenario", state="stopped", **info)
        raise
    except Exception as e:
        log(f"   FAILED: {type(e).__name__}: {e}")
        failures.append((scn, str(e)))
        emit(kind="scenario", state="failed", error=str(e), **info)
        recover()
        return "failed"


def run_export(handle, out_dir, pdf=True, txt=True, only=None, resume=False, extra=None,
               discover=True, list_only=False, select_only=None, confirm=None):
    """Export every scenario of the Synchro project window `handle`.
    Returns {'done', 'skipped', 'failed': [(name, error)], 'stopped', 'total', 'folder'}.
    Progress arrives through PROGRESS(**event); set STOP to stop between steps."""
    global SCENARIO_ORDER, REPORT_DIR, DO_PDF, DO_TXT, RESUME
    STOP.clear()
    summary = {"done": 0, "skipped": 0, "failed": [], "stopped": False, "total": 0, "folder": str(out_dir)}
    failures = summary["failed"]
    try:
        connect(handle)
        DO_PDF, DO_TXT, RESUME = bool(pdf), bool(txt), bool(resume)
        if not (DO_PDF or DO_TXT) and not (list_only or select_only):
            raise RuntimeError("Choose PDF, TXT or both")

        emit(kind="status", text="reading the scenario list")
        log("Reading scenarios from the dropdown...")
        t = time.time()
        exposed = get_all_scenarios()
        est = _pitch.get("total")
        log(f"(reading scenarios took {time.time() - t:.1f}s) "
            f"Exposed: {len(exposed)}; the list's size suggests about {est if est else '?'} in total")

        exposed_set = set(exposed)
        SCENARIO_ORDER = sorted(exposed_set)
        only_set = set(only) if only else None
        wanted = lambda n: only_set is None or n in only_set
        need_pass = DISCOVER_HIDDEN and discover and (est is None or est > len(exposed))
        emit(kind="scenarios", names=list(SCENARIO_ORDER), est=est, final=not need_pass)

        hidden, seen, done = [], set(), set()

        def all_names():
            return sorted(exposed_set | set(hidden) | set(extra or []))

        def pass1(handle_fn):
            nonlocal hidden
            emit(kind="status", text="finding scenarios the list doesn't show")
            log("Finding scenarios the dropdown doesn't expose (clicking up from the bottom of the list)...")
            hidden = bottom_pass(exposed_set, handle_fn)
            if hidden:
                log(f"   found {len(hidden)} hidden: " + ", ".join(hidden))

        # Modes that need the full list BEFORE exporting anything (these switch scenario while looking)
        discover_first = need_pass and (list_only or select_only or (only_set and not only_set <= exposed_set))
        if discover_first:
            pass1(seen.add)
            need_pass = False
        SCENARIO_ORDER = all_names()
        emit(kind="scenarios", names=list(SCENARIO_ORDER), est=est, final=not need_pass)

        if select_only:
            select_scenario(select_only)
            log(f"Now showing: {current_scenario()!r}")
            return summary
        if list_only:
            log(f"Found {len(SCENARIO_ORDER)} scenarios: " + ", ".join(SCENARIO_ORDER))
            return summary

        if only_set and not need_pass:
            missing = [n for n in only_set if n not in SCENARIO_ORDER]
            if missing:
                raise RuntimeError(f"Not in the dropdown: {missing}")

        REPORT_DIR = Path(out_dir)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        probe = REPORT_DIR / ".write_test"
        probe.write_text("x")
        probe.unlink()
        summary["folder"] = str(REPORT_DIR)
        log(f"Output folder: {REPORT_DIR}")

        n_expected = len(only_set) if only_set else (len(SCENARIO_ORDER) if not need_pass else (est or len(SCENARIO_ORDER)))
        if confirm and not confirm(n_expected, need_pass):
            return summary

        counter = {"n": 0, "total": n_expected, "approx": bool(need_pass and est)}

        def run_one(name, already_selected):
            counter["n"] += 1
            pos = (counter["n"], counter["total"], counter["approx"])
            result = process_scenario(name, pos, already_selected, failures)
            if result in ("done", "skipped"):
                summary[result] += 1
            summary["total"] = counter["n"]

        log(f"Starting -> {REPORT_DIR}")
        # Pass 1 (only when the list hides some): the bottom-row clicks that FIND the hidden scenarios
        # also SELECT them, so each one is exported right then and never has to be selected again.
        if need_pass:
            def handle_fn(name):
                seen.add(name)
                if name not in SCENARIO_ORDER:
                    emit(kind="scenarios", names=sorted(set(SCENARIO_ORDER) | {name}), est=est, final=False)
                if wanted(name):
                    run_one(name, True)
                    done.add(name)
            pass1(handle_fn)
            SCENARIO_ORDER = all_names()
            emit(kind="scenarios", names=list(SCENARIO_ORDER), est=est, final=True)
            if only_set:
                missing = [n for n in only_set if n not in SCENARIO_ORDER]
                if missing:
                    log(f"   WARNING: not in the dropdown: {missing}")

        if est and len(SCENARIO_ORDER) != est:
            log(f"   WARNING: expected about {est} scenarios from the list's size but found "
                f"{len(SCENARIO_ORDER)}; some may be missing")

        # Pass 2: everything else, selected in the normal way
        todo = [x for x in SCENARIO_ORDER if x not in done and wanted(x)]
        counter["total"], counter["approx"] = counter["n"] + len(todo), False
        for scn in todo:
            run_one(scn, False)

        log("Finished. Failures: " + (", ".join(f for f, _ in failures) if failures else "none"))
    except Stopped:
        summary["stopped"] = True
        log("Stopped by the user.")
        recover()
    except Exception as e:
        summary["error"] = str(e) or type(e).__name__
        log(f"FAILED: {type(e).__name__}: {e}")
        log(traceback.format_exc())
    finally:
        emit(kind="finished", summary=summary)
    return summary


# ------------------------------------------------------------------ command line
def choose_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        d = filedialog.askdirectory(title="Choose output folder for Synchro reports",
                                    initialdir=str(DEFAULT_REPORT_DIR) if DEFAULT_REPORT_DIR.exists() else None)
        root.destroy()
        if d:
            return Path(d)
    except Exception as e:
        log(f"Folder picker failed ({e}); type the folder in the terminal instead")
    d = input(f"Output folder [{DEFAULT_REPORT_DIR}]: ").strip().strip('"')
    return Path(d) if d else DEFAULT_REPORT_DIR


def main_loop():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="output folder (default: OUTPUT_DIR in the config)")
    ap.add_argument("--no-discover", action="store_true", help="skip looking for scenarios the list hides")
    ap.add_argument("--project", help="only match a Synchro window whose title contains this text")
    ap.add_argument("--pick", action="store_true", help="choose the output folder in a picker window")
    ap.add_argument("--only", nargs="+", help="only these scenario names")
    ap.add_argument("--add", nargs="+", help="extra scenario names to add if the dropdown doesn't expose them")
    ap.add_argument("--select-only", metavar="NAME", help="just switch to this scenario and exit (test)")
    ap.add_argument("--list", action="store_true", help="print scenarios found and exit (switches scenario)")
    ap.add_argument("--resume", action="store_true", help="skip files already exported")
    ap.add_argument("--formats", choices=["pdf", "txt", "both"], default="both")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args(RUN_ARGS if len(sys.argv) == 1 else None)

    handle = _find_project_window()
    if args.out:
        out = Path(args.out)
    elif args.pick:
        out = choose_folder()
    else:
        out = Path(OUTPUT_DIR)

    def confirm(n, approx):
        if not ASK_TO_CONFIRM or args.yes:
            return True
        input(f"\n{'about ' if approx else ''}{n} scenario(s) -> {out}\n"
              "Don't touch the mouse/keyboard while it runs. Press Enter to start... ")
        return True

    result = run_export(handle, out, pdf=args.formats in ("pdf", "both"), txt=args.formats in ("txt", "both"),
                        only=args.only, resume=args.resume, extra=args.add, discover=not args.no_discover,
                        list_only=args.list, select_only=args.select_only, confirm=confirm)
    if result.get("error"):
        raise SystemExit(result["error"])


if __name__ == "__main__":
    main_loop()
