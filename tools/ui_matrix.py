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
    r.destroy()

# ---------------------------------------------------------------- §3 static: track() (Task 5)
print("§3 the 1.2 s track() tick does no ssh on the Tk thread")
m = re.search(r"\n    def track\(\):\n(.*?)\n    def ", SRC, re.S)
body = m.group(1) if m else ""
check(bool(body), "found launch()'s track() body")
check("gpu_stats()" not in body, "track() does not call gpu_stats() (an ssh) on the Tk thread")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f in FAILS:
    print("  FAILED: %s" % f)
sys.exit(1 if FAILS else 0)