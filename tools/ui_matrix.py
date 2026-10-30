#!/usr/bin/env python3
"""GPU-free, forge-free checks for Forge Lab's scroll + UI-thread fixes.

    /opt/homebrew/bin/python3 tools/ui_matrix.py      # exit 1 on any failure

Opens two tiny Tk windows for < 1 s. Never launches the app, never touches forge.
"""
import importlib.util
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["FORGE_LAB_DATA"] = tempfile.mkdtemp(prefix="forgeui-")    # BEFORE import

spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(ROOT, "forge_lab.py"))
fl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fl)
SRC = open(os.path.join(ROOT, "forge_lab.py"), encoding="utf-8").read()

FAILS, N = [], [0]


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------- §1 static: no app-wide unbind
print("§1 no sub-window binds or unbinds the wheel app-wide")
check(SRC.count('unbind_all("<MouseWheel>")') == 0,
      'no unbind_all("<MouseWheel>") anywhere (found %d) — it deletes the MAIN window\'s wheel'
      % SRC.count('unbind_all("<MouseWheel>")'))
sites = re.findall(r"^\s+scoped_wheel\(win, \w+\)", SRC, re.M)
check(len(sites) == 4, "all four sub-windows use scoped_wheel(win, …) (found %d)" % len(sites))

# ---------------------------------------------------------------- §1b the workspace sidebar
print("§1b every workspace the dispatcher knows has a sidebar row, and nothing else does")
_m = re.search(r"NAV_ITEMS = \((.*?)\)\n(?:\s*#[^\n]*\n)*    NAV_WINDOWED", SRC, re.S)
check(_m is not None, "NAV_ITEMS is defined in launch()")
_nav = set(re.findall(r'\("[^"]+", "([^"]+)"\)', _m.group(1))) if _m else set()
_disp = set(re.findall(r'^        if kind == "([^"]+)":\n            try:\n                open_',
                       SRC, re.M))
# WEIRDO is folded into Characters (its roll is "Surprise me" there); STUDIO + LONG VIDEO are
# folded into Episode (2026-10-17). on_kind() still opens all three; they have no row.
_MERGED = {"Weirdo", "Studio", "Long video"}
check(_disp and (_disp - _MERGED) <= _nav, "every workspace on_kind() opens is in the "
      "sidebar, the merged ones aside (missing: %s)" % sorted(_disp - _nav - _MERGED))
check(_nav == (_disp - _MERGED) | {"Image", "Video"}, "sidebar = those workspaces (minus the "
      "merged %s) + Image + Video (extra: %s)" % (sorted(_MERGED),
                                                  sorted(_nav - _disp - {"Image", "Video"})))
_EUI = open(os.path.join(ROOT, "episode_ui.py"), encoding="utf-8").read()
check('ctx["open_studio"]()' in _EUI and 'ctx["open_long"]()' in _EUI
      and '"open_studio": open_studio_workspace, "open_long": open_long_video_workspace' in SRC,
      "the merged Studio + Long video stay reachable from inside Episode")
_CUI = open(os.path.join(ROOT, "characters_ui.py"), encoding="utf-8").read()
check('"Surprise me"' in _CUI and "fl.roll_weirdo(" in _CUI,
      "the merged Weirdo roll lives in Characters as Surprise me")
check(SRC.count("_nav_go(k)") >= 1 and "kind_var.set(kind)\n        on_kind()" in SRC,
      "sidebar rows go through on_kind(), not their own window code")

# ---------------------------------------------------------------- §1c the MODEL cards table
print("§1c MODEL_CARDS describes every model exactly once, and every grid fits")
_labs = {k: [c[2] for c in v] for k, v in fl.MODEL_CHOICES.items()}
_all = [l for v in _labs.values() for l in v]
check(not [l for l in _all if l not in fl.MODEL_CARDS],
      "every MODEL_CHOICES label has a card (missing: %s)"
      % [l for l in _all if l not in fl.MODEL_CARDS])
check(not [l for l in fl.MODEL_CARDS if l not in _all],
      "no card for a model that no longer exists (stale: %s)"
      % [l for l in fl.MODEL_CARDS if l not in _all])
for _k, _v in _labs.items():
    _n = sum(1 for l in _v if fl.MODEL_CARDS.get(l, ("", "", False))[2])
    check(1 <= _n <= 4, "%s has 1-4 big cards for the 2x2 grid (has %d)" % (_k, _n))
_eng = [l for l in _all if l.startswith(("H3", "WAN", "LTX"))]
check(not [l for l in _eng if l.split()[0] not in fl.MODEL_CARDS[l][1]],
      "every video card names its ENGINE in the subtitle (H3 vs WAN is invisible otherwise)")

# ---------------------------------------------------------------- §2 behaviour of scoped_wheel
print("§2 scoped_wheel behaviour")
if not hasattr(fl, "scoped_wheel"):
    check(False, "scoped_wheel() exists")
else:
    import tkinter as tk
    r = tk.Tk()
    r.geometry("120x120+0+0")
    main_hits = []
    r.bind_all("<MouseWheel>", lambda e: main_hits.append("wheel"))
    r.bind_all("<TouchpadScroll>", lambda e: main_hits.append("touch"))
    win = tk.Toplevel(r)
    win.geometry("300x300+140+0")
    header = tk.Label(win, text="header — outside the canvas")
    header.pack()
    cv = tk.Canvas(win, height=200, highlightthickness=0)
    cv.pack(fill="both", expand=True)
    inner = tk.Frame(cv)
    cv.create_window((0, 0), window=inner, anchor="nw")
    row = tk.Label(inner, text="a row inside the canvas")
    row.pack()
    txt = tk.Text(inner, height=3, width=20)
    txt.pack()
    tk.Frame(inner, height=3000, width=50).pack()          # tall content -> scrollable
    r.update()
    cv.configure(scrollregion=cv.bbox("all"))
    r.update()
    fl.scoped_wheel(win, cv)

    def reset():
        cv.yview_moveto(0.0); r.update(); del main_hits[:]

    def top():
        r.update(); return cv.yview()[0]

    reset(); row.event_generate("<MouseWheel>", delta=-120, when="now")
    check(top() > 0, "mouse wheel over a row inside the canvas scrolls it down")
    check(not main_hits, "…and the main window's handler did not ALSO run (got %r)" % main_hits)

    reset(); row.event_generate("<TouchpadScroll>", delta=(-40 & 0xFFFF), when="now")
    check(top() > 0, "TRACKPAD over a row inside the canvas scrolls it down")

    reset(); txt.event_generate("<MouseWheel>", delta=-120, when="now")
    check(top() == 0, "wheel over a Text leaves the canvas alone (the Text scrolls itself)")

    reset(); header.event_generate("<MouseWheel>", delta=-120, when="now")
    check(top() == 0, "wheel outside the canvas does not scroll it")

    check(bool(r.bind_all("<MouseWheel>")), "the main window's wheel binding is still installed")
    check(bool(r.bind_all("<TouchpadScroll>")), "the main window's trackpad binding is still installed")
    # a SECOND scrolling canvas in the same window (Quick's stage): add=True keeps both
    cv2 = tk.Canvas(win, height=80, highlightthickness=0)
    cv2.pack(fill="x")
    in2 = tk.Frame(cv2)
    cv2.create_window((0, 0), window=in2, anchor="nw")
    row2 = tk.Label(in2, text="second canvas row")
    row2.pack()
    tk.Frame(in2, height=3000, width=50).pack()
    r.update()
    cv2.configure(scrollregion=cv2.bbox("all"))
    fl.scoped_wheel(win, cv2, add=True)
    reset(); cv2.yview_moveto(0.0); r.update()
    row2.event_generate("<MouseWheel>", delta=-120, when="now")
    r.update()
    check(cv2.yview()[0] > 0 and top() == 0, "add=True: the wheel over the second canvas "
          "scrolls only it")
    reset(); row.event_generate("<MouseWheel>", delta=-120, when="now")
    check(top() > 0 and not main_hits, "...and the first canvas still scrolls")
    r.destroy()

# ---------------------------------------------------------------- §3 static: track() (Task 5)
print("§3 the 1.2 s track() tick does no ssh on the Tk thread")
m = re.search(r"\n    def track\(\):\n(.*?)\n    def ", SRC, re.S)
body = m.group(1) if m else ""
check(bool(body), "found launch()'s track() body")
check("gpu_stats()" not in body, "track() does not call gpu_stats() (an ssh) on the Tk thread")

# ---------------------------------------------------------------- §4 Cmd+H hides the app
print("§4 Cmd+H hides the app on a Mac")
check("bind_mac_hide(root)" in SRC, "launch() binds Cmd+H")


class _R:
    def __init__(self):
        self.b = {}

    def bind_all(self, seq, fn):
        self.b[seq] = fn

    def iconify(self):
        self.b["iconified"] = True


_plat = fl.sys.platform
try:
    fl.sys.platform = "linux"
    _r0 = _R()
    check(fl.bind_mac_hide(_r0) is False and not _r0.b and fl.mac_hide_app() is False,
          "not a Mac: nothing bound, nothing sent")
    fl.sys.platform = "darwin"
    _sent = []
    _r1 = _R()
    check(fl.bind_mac_hide(_r1, hide=lambda: _sent.append(1) or True) and
          set(_r1.b) == {"<Command-h>", "<Command-H>"}, "on a Mac: Cmd+H (and Cmd+Shift+H) bound app-wide")
    check(_r1.b["<Command-h>"]() == "break" and _sent == [1] and "iconified" not in _r1.b,
          "...it hides the app (and stops there)")
    _r2 = _R()
    fl.bind_mac_hide(_r2, hide=lambda: False)
    _r2.b["<Command-h>"]()
    check(_r2.b.get("iconified"), "...and if hiding fails, the window is minimised instead")
finally:
    fl.sys.platform = _plat

print("§6 settings keep the keys the main window doesn't own; plain card words are complete")
import tempfile as _tf6  # noqa: E402
_old6 = fl.SETTINGS
fl.SETTINGS = os.path.join(_tf6.mkdtemp(), "settings.json")
open(fl.SETTINGS, "w").write('{"tabs": false, "open_on": "main", "aspect": "1:1"}')
fl.save_settings({"aspect": "16:9", "machine_room": True})
_s6 = fl.load_settings()
check(_s6 == {"tabs": False, "open_on": "main", "aspect": "16:9", "machine_room": True},
      "the autosave updates its own keys and keeps 'tabs' / 'open_on' (%r)" % _s6)
os.remove(fl.SETTINGS)
fl.save_settings({"aspect": "9:16"})
check(fl.load_settings() == {"aspect": "9:16"}, "...and still writes a first settings file")
fl.SETTINGS = _old6
check(set(fl.MODEL_CARDS_PLAIN) == set(fl.MODEL_CARDS) and not [
    v for v in fl.MODEL_CARDS_PLAIN.values() if any(
        w in v for w in ("Qwen", "H3", "WAN", "LTX", "FLUX", "Krea", "steps", "i2v", "r2v"))],
      "every model card has plain words, with no engine names or step counts in them")

print("§5 workers ask for UI updates through ui_later (never widget.after off-thread)")
import threading  # noqa: E402
import tkinter as tk  # noqa: E402
r5 = tk.Tk()
fl.start_ui_pump(r5, every=10)
ran, gone = [], tk.Label(r5)
threading.Thread(target=lambda: (fl.ui_later(r5, lambda: ran.append(
    threading.current_thread() is threading.main_thread())),
    fl.ui_later(gone, lambda: ran.append("gone"))), daemon=True).start()
gone.destroy()
import time as _t5  # noqa: E402
_t0 = _t5.time()
while not ran and _t5.time() - _t0 < 3:
    r5.update()
    _t5.sleep(0.02)
for _i in range(10):
    r5.update()
    _t5.sleep(0.02)
check(ran == [True], "a worker's call runs on the Tk thread; a destroyed widget's is "
      "dropped (%r)" % ran)
r5.destroy()
fl._UI_PUMP["on"] = False
_sites = re.findall(r"\n(\s+)def (work|_work|run|_worker|_kf_worker)\(.*?\):\n(.*?)"
                    r"(?=\n\1\S)", SRC, re.S)
_bad = [n for _ind, n, b in _sites if re.search(r"\b(win|root)\.after\(0,", b)]
check(not _bad, "no worker body calls win/root.after(0, ...) (%r)" % _bad[:5])

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f in FAILS:
    print("  FAILED: %s" % f)
sys.exit(1 if FAILS else 0)