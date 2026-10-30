#!/usr/bin/env python3
"""Checks for dock.py — workspaces as TABS in the main window (needs a display; the app's
own drivers cover the real windows: TESTSIDEBAR, TESTCHAR3, TESTEPISODE...).
    python3 tools/dock_matrix.py"""
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dock  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


C = {"bg": "#000", "panel2": "#111", "ink": "#eee", "muted": "#888", "ochre": "#c90"}
root = tk.Tk()
root.geometry("900x600")

print("§1 no dock: a plain window, as before")
w0 = dock.window(root)
check(isinstance(w0, tk.Toplevel), "without a dock, window() is a Toplevel")
w0.destroy()

print("§2 tabs")
strip, area = tk.Frame(root), tk.Frame(root)
strip.pack(fill="x")
area.pack(fill="both", expand=True)
home = tk.Frame(area, bg="red")
d = dock.Dock(root, strip, area, home, C, None)
dock.install(d)
root.update()
check(d.cur is None and home.winfo_ismapped(), "the Make tab (home) shows first")
a = dock.window(root)
a.title("Forge Lab — Quick")
a.geometry("1240x820")
a.minsize(800, 600)
tk.Label(a, text="quick").pack()
root.update()
check(isinstance(a, tk.Frame) and not isinstance(a, tk.Toplevel) and d.cur is a and
      a.winfo_ismapped() and not home.winfo_ismapped(),
      "a workspace opens as a TAB in the window and shows")
labels = [c.winfo_children()[0].cget("text") for c in strip.winfo_children()]
check(labels == ["Image \u00b7 Video", "Quick"], "the strip names the tabs, prefix dropped (%r)" % labels)
b = dock.window(root)
b.title("Characters")
_hd = tk.Frame(b)                  # built like the workspaces: header first, same tick
_hd.pack()
_cb = tk.Button(_hd, text="Close")
_cb.pack()
root.update()
check(not _cb.winfo_manager(), "a window's own header Close button is hidden in a tab (the "
      "tab has its x)")
root.update()
check(d.cur is b and not a.winfo_ismapped(), "a second tab shows; the first hides")
a.lift()
root.update()
check(d.cur is a and a.winfo_ismapped(), "lift() (opening it again) selects its tab")

print("§3 bindings reach a tab's widgets")
hits = []
inner = tk.Canvas(a, width=50, height=50)
inner.pack()
a.bind("<MouseWheel>", lambda e: hits.append(("wheel", str(e.widget))) or "break")
a.bind("<Escape>", lambda e: hits.append(("esc", str(e.widget))))
conf = []
a.bind("<Configure>", lambda e: conf.append(1))
root.update()
inner.event_generate("<MouseWheel>", delta=-120, x=5, y=5)
inner.focus_force()
root.update()
inner.event_generate("<Escape>")
root.update()
check(("wheel", str(inner)) in hits, "the wheel inside the tab reaches the tab's binding")
check(("esc", str(inner)) in hits, "a key inside the tab reaches the tab's binding")
check("<Configure>" in tk.Frame.bind(a), "structure events still bind on the frame itself")
b.lift()
root.update()
n0 = len(hits)
inner.event_generate("<MouseWheel>", delta=-120, x=5, y=5)
root.update()
check(len(hits) == n0, "a hidden tab doesn't get the wheel")
a.lift()
root.update()

print("§4 closing")
asked = []
a.protocol("WM_DELETE_WINDOW", lambda: (asked.append(1), a.destroy()))
a.close()
root.update()
check(asked and not a.winfo_exists() and d.cur is b and a not in d.tabs,
      "the tab's x runs the window's own close; the previous tab shows")
b.destroy()
root.update()
check(d.cur is None and home.winfo_ismapped() and not d.tabs,
      "closing the last tab shows the Make tab")
c = dock.window(root)
c.withdraw()
root.update()
check(d.cur is None and c in d.tabs, "withdraw() hides the tab (it stays open)")
c.deiconify()
root.update()
check(d.cur is c, "deiconify() shows it again")
root.destroy()

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
