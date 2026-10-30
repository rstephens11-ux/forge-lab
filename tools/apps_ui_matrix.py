#!/usr/bin/env python3
"""GPU-free checks for the Apps window's Restyle STYLE strip (2026-10-22).

Ryan: "have the styles how they are but in a box that is scrollable horizontally so I can
choose what I want and see them all without a dropdown".

    /opt/homebrew/bin/python3 tools/apps_ui_matrix.py      # exit 1 on any failure

Opens the real Apps window for a second. Never launches the app, never touches forge.
Pass a path as the first argument to also save a screenshot (needs ImageMagick `import`).
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["FORGE_LAB_DATA"] = tempfile.mkdtemp(prefix="appsui-")    # BEFORE import

spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(ROOT, "forge_lab.py"))
fl = importlib.util.module_from_spec(spec)
sys.modules["forge_lab"] = fl
spec.loader.exec_module(fl)
import apps  # noqa: E402
import apps_ui  # noqa: E402

FAILS, N = [], [0]


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def pump(n=8):
    for _ in range(n):
        root.update()


root = tk.Tk()
root.geometry("1240x820+0+0")
C = {"bg": "#14110f", "panel": "#1b1815", "panel2": "#211d19", "line": "#312b25",
     "ink": "#efe6d8", "muted": "#a89c8c", "faint": "#6d6358", "ochre": "#c9a14a",
     "rust": "#a5532f"}
f = ("TkDefaultFont", 12)
import tkinter.font as tkfont  # noqa: E402
F = tkfont.Font(family="Helvetica", size=12)
FH = tkfont.Font(family="Helvetica", size=13, weight="bold")
FSM = tkfont.Font(family="Helvetica", size=11)
ctx = {"C": C, "fonts": {"f": F, "fh": FH, "fsm": FSM}, "mono": {"m": F},
       "btn_colors": lambda *a, **k: {}, "mod": fl}
win = apps_ui.open_apps(root, ctx)
try:
    win.geometry("1240x820+0+0")
except Exception:
    pass
win.ap["pick"]("restyle")
pump(12)
st = win.ap["st"]
cv = st.get("style_canvas")

print("§1 every style is a chip in one sideways strip")
chips = st.get("style_chips") or {}
check(cv is not None and cv.winfo_exists(), "Restyle shows a STYLE strip (a canvas)")
check(sorted(chips) == sorted(n for n, _w in apps.STYLES),
      "a chip for every style (%d of %d)" % (len(chips), len(apps.STYLES)))
check(not any(isinstance(w, tk.OptionMenu) for w in st.get("style_canvas").master.winfo_children()),
      "no dropdown")
rows = {chips[n].grid_info().get("row") for n in chips}
check(rows <= {1, 2, 3}, "chips sit in at most three rows (rows %s)" % sorted(rows))
sr = [float(v) for v in str(cv.cget("scrollregion")).split()]
check(len(sr) == 4 and sr[2] - sr[0] > cv.winfo_width() > 100,
      "the strip is wider than its box, so it scrolls sideways (%s vs %d px)"
      % (sr[2] - sr[0] if len(sr) == 4 else "?", cv.winfo_width()))
inner_h = cv.winfo_children()[0].winfo_reqheight() if cv.winfo_children() else 0
check(inner_h > 60 and cv.winfo_height() >= inner_h - 2,
      "the box is tall enough for the three rows (%d px for %d)" % (cv.winfo_height(), inner_h))
groups = [g for g, _n in apps.STYLE_GROUPS]
labels = []
for col in cv.winfo_children()[0].winfo_children() if cv.winfo_children() else []:
    for w in col.winfo_children():
        if isinstance(w, tk.Label) and w.cget("text").isupper():
            labels.append(w.cget("text"))
check(labels == [g.upper() for g in groups], "the groups are labelled, in order: %s" % labels)

print("§2 sideways scrolling")
cv.xview_moveto(0.0)
pump()
x0 = cv.xview()[0]
cv.event_generate("<Shift-MouseWheel>", delta=-120, x=20, y=20)
pump()
check(cv.xview()[0] > x0, "shift + wheel moves the strip right (%.3f -> %.3f)"
      % (x0, cv.xview()[0]))
y_page = None
try:
    pcv = cv
    while pcv is not None and not (isinstance(pcv, tk.Canvas) and pcv is not cv):
        pcv = pcv.master
    y_page = pcv.yview()[0] if pcv is not None else None
except Exception:
    pcv = None
sideways = ((-60 & 0xFFFF) << 16) | 0                 # dx = -60 (finger left), dy = 0
before = cv.xview()[0]
try:
    chips["Anime"].event_generate("<TouchpadScroll>", delta=sideways)
    pump()
    tp = True
except tk.TclError:
    tp = False
if tp:
    check(cv.xview()[0] > before, "a sideways two-finger swipe over a chip moves the strip "
          "(%.3f -> %.3f)" % (before, cv.xview()[0]))
    check(pcv is None or abs(pcv.yview()[0] - y_page) < 1e-6,
          "...and leaves the page where it was")
    hold = cv.xview()[0]
    chips["Anime"].event_generate("<TouchpadScroll>", delta=(0 << 16) | 40)
    pump()
    check(abs(cv.xview()[0] - hold) < 1e-6, "an up/down swipe doesn't move the strip sideways")
else:
    print("  --   (this Tk has no <TouchpadScroll>; swipe checks skipped)")

print("§3 picking a style far to the right")
cv.xview_moveto(1.0)
pump()
far = cv.xview()[0]
last = apps.STYLE_GROUPS[-1][1][-1]
chips[last].event_generate("<Button-1>")
pump(12)
st = win.ap["st"]
cv2 = st.get("style_canvas")
check(st["style"] == last, "clicking %r picks it" % last)
check(cv2.xview()[0] > 0.5 and abs(cv2.xview()[0] - far) < 0.05,
      "the strip stays where you were after the pick (%.3f, was %.3f)" % (cv2.xview()[0], far))
on = st["style_chips"][last]
check(on.cget("bg") == C["ink"] and st["style_chips"]["Claymation"].cget("bg") == C["panel2"],
      "the picked chip is highlighted, the others aren't")
words = dict(apps.STYLES)[last]
check(any(isinstance(w, tk.Label) and words in (w.cget("text") or "")
          for w in cv2.master.master.winfo_children()),
      "the picked style's description shows under the strip")
check(words in apps.prompt_for("restyle", {"style": st["style"]}),
      "the picked style is what the edit asks for")

if len(sys.argv) > 1:
    try:
        if win is not root and isinstance(win, tk.Toplevel):
            root.withdraw()                     # no dock here: show the Apps window alone
        win.lift()
        win.geometry("1240x820+0+0")
    except Exception:
        pass
    pump(10)
    subprocess.run(["import", "-window", "root", "-crop", "1240x820+0+0", sys.argv[1]],
                   capture_output=True)
root.destroy()
print("\n%d checks, %d failed" % (N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
