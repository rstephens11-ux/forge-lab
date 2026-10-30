#!/usr/bin/env python3
"""Tk assertions for the home page's QUEUE panel (home_queue.py).

    /opt/homebrew/bin/python3 tools/queue_ui_matrix.py      # exits 1 on any failure

⚠️ Run this with /opt/homebrew/bin/python3 (Tk 9). /usr/bin/python3 is 3.9 with Tk 8.5.

No forge, no GPU, no network: the panel is built against a TEMP queue file and a colour/font
stub. The window is withdrawn immediately, so nothing appears on screen. What is asserted is
what the person actually sees and clicks: the rows, the buttons, and that a click reaches the
rules in jobqueue.py (which has its own suite, tools/queue_matrix.py).
"""
import os
import shutil
import sys
import tempfile
import tkinter as tk

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import jobqueue as jq       # noqa: E402
import home_queue            # noqa: E402

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


C = {"bg": "#101010", "panel": "#181818", "panel2": "#222222", "ink": "#eeeeee",
     "muted": "#999999", "faint": "#666666", "rust": "#b45a2a"}
CTX = {"C": C, "fonts": {"fsm": ("Menlo", 11), "fh": ("Menlo", 12)}, "mono": {"s": ("Menlo", 11)}}

tmp = tempfile.mkdtemp(prefix="quimatrix-")
path = os.path.join(tmp, "queue.json")

root = tk.Tk()
root.withdraw()

print("\nEMPTY QUEUE")
q = jq.Queue(path=path)
ctx = dict(CTX, queue=q)
box = home_queue.build_queue(root, ctx)
root.update_idletasks()
lst = box._queue_listbox
check(lst.size() == 1, "an empty queue shows ONE hint row, not a blank box")
check("nothing queued" in lst.get(0), "the hint says what the queue is for")
check(box._queue_buttons["delete"].cget("text") == "Delete", "the Delete button exists")
check(box._queue_buttons["pause"].cget("text") == "Pause", "the Pause button exists")
check(box._queue_buttons["bump"].cget("text") == "Run next", "the Run-next button exists")
box.destroy()

print("\nROWS")
q = jq.Queue(path=path)
a = q.add(jq.new_entry("Make it move — 6 s", "comfy", 0, est_s=240))
b = q.add(jq.new_entry("Turbo — the barn at dawn", "comfy", [0, 1], est_s=900))
c = q.add(jq.new_entry("Outfit swap — the doctor", "comfy", 1, est_s=180))
box = home_queue.build_queue(root, dict(CTX, queue=q))
root.update_idletasks()
lst = box._queue_listbox
check(lst.size() == 3, "three entries -> three rows")
check("card 1" in lst.get(0), "row 1 names its card")
check("both cards" in lst.get(1), "row 2 says both cards (Turbo)")
check("card 2" in lst.get(2), "row 3 names its card")
check(box._queue_ids == [a["id"], b["id"], c["id"]], "rows map to entry ids in order")

print("\nCLICKS REACH THE RULES")
lst.selection_clear(0, "end"); lst.selection_set(2)
box._queue_buttons["pause"].invoke()
root.update_idletasks()
check(q.get(c["id"])["state"] == jq.STATE_PAUSED, "Pause pauses the selected row")
check("paused" in lst.get(2), "the row says paused after the click")
check("keeps its place" in box._queue_say.cget("text"), "the panel explains what pause did")

lst.selection_clear(0, "end"); lst.selection_set(2)
box._queue_buttons["pause"].invoke()
root.update_idletasks()
check(q.get(c["id"])["state"] == jq.STATE_QUEUED, "the same button resumes it")

lst.selection_clear(0, "end"); lst.selection_set(2)
box._queue_buttons["bump"].invoke()
root.update_idletasks()
check(box._queue_ids[0] == c["id"], "Run next moves the selected row to the top of the list")

lst.selection_clear(0, "end"); lst.selection_set(0)
box._queue_buttons["down"].invoke()
root.update_idletasks()
check(box._queue_ids[1] == c["id"], "▼ moves it down one place")

lst.selection_clear(0, "end"); lst.selection_set(0)
box._queue_buttons["delete"].invoke()
root.update_idletasks()
check(lst.size() == 2, "Delete removes the row")
with open(path) as f:
    on_disk = [e["id"] for e in __import__("json").load(f)]
check(len(on_disk) == 2, "the delete reached the queue FILE, not just the screen")

print("\nA RUNNING ROW REFUSES POLITELY")
run_id = box._queue_ids[0]
q.mark_running(run_id, lane="comfy_a")
box._queue_listbox.selection_clear(0, "end")
box._queue_listbox.selection_set(0)
box._queue_buttons["delete"].invoke()
root.update_idletasks()
check(q.get(run_id) is not None, "a running row was not deleted")
check("stopped first" in box._queue_say.cget("text"),
      "the panel says a running job must be stopped, not deleted")
box._queue_listbox.selection_clear(0, "end")
box._queue_listbox.selection_set(0)
box._queue_buttons["pause"].invoke()
root.update_idletasks()
check("cannot be paused" in box._queue_say.cget("text"), "and that it cannot be paused either")

print("\nA FINISHED JOB FALLS OFF THE LIST")
# Ryan, 2026-10-22: "once the generation is completed it should fall off the list of the queue."
# The panel listed q.entries(), so every finished job stayed as a row for ever — 47 all-done rows
# in his real queue file, which reads like a backlog when nothing is waiting.
q.finish(run_id, ok=True)
box._queue_paint()
check(run_id not in box._queue_ids, "the finished job's row is GONE from the list")
check(len(box._queue_ids) == 1,
      "...and only the waiting one is left (%d)" % len(box._queue_ids))
check(q.get(run_id) is not None,
      "...but the entry is still in the queue FILE, so its own window can read the outcome")
check("nothing queued" not in str(box._queue_say.cget("text")) or box._queue_ids,
      "the panel still reports the job that IS waiting")
box._queue_listbox.selection_clear(0, "end")

print("\nNO SELECTION")
box._queue_listbox.selection_clear(0, "end")
box._queue_buttons["delete"].invoke()
check("Pick a row" in box._queue_say.cget("text"), "clicking Delete with nothing selected says so")

root.destroy()
shutil.rmtree(tmp, ignore_errors=True)
print("\n%s — %d checks failed" % ("FAIL" if FAILS else "ALL GREEN", len(FAILS)))
sys.exit(1 if FAILS else 0)
