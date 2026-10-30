#!/usr/bin/env python3
"""home_queue — the QUEUE panel on Forge Lab's home page.

What Ryan asked for (2026-10-22): a homepage showing the recent takes, the machine room and
the queue — one place to see what the rig is doing and what is waiting for it.

This panel is deliberately thin. All the rules (order, pause, delete, which cards a job needs,
what may run now) live in jobqueue.py, which is tested with no Tk at all. Here there is only:
a list, a selection, and buttons that call those rules.

    QUEUE                                      2 waiting · 1 running
      running  Make it move — 6 s            card 1     comfy_a
      queued   Turbo — the barn at dawn      both cards
      paused   Outfit swap — the doctor      card 2     paused by you
    [ ▲ ] [ ▼ ] [ Pause ] [ Delete ] [ Run next ]

⚠️ A running entry cannot be paused, moved or deleted — the buttons say so instead of silently
doing nothing, and cancelling a RUNNING job is the dispatcher's business, not this panel's.
"""
import tkinter as tk

import jobqueue as jq

# One queue for the whole process: the panel, the Home tab and the DISPATCHER must share it, or
# the panel would reorder a file nothing re-reads. The instance itself lives in jobqueue.py,
# which is Tk-free (dispatch.py imports it and must not import tkinter).


def app_queue(path=None):
    """The process-wide queue, loaded from disk on first use (see jobqueue.app_queue)."""
    return jq.app_queue(path)


def _summary(q):
    st = q.stats()
    parts = []
    for key, word in (("running", "running"), ("queued", "waiting"), ("paused", "paused")):
        n = st.get(key, 0)
        if n:
            parts.append("%d %s" % (n, word))
    return " · ".join(parts) or "nothing waiting"


def build_queue(parent, ctx, poll_ms=2000):
    """Build the panel inside `parent`. Returns the frame (for tests)."""
    C = ctx["C"]
    fonts = ctx.get("fonts") or {}
    fsm = fonts.get("fsm") or ("TkDefaultFont", 11)
    fh = fonts.get("fh") or ("TkDefaultFont", 11)
    mono = (ctx.get("mono") or {}).get("s") or fsm
    q = ctx.get("queue") or app_queue()

    box = tk.Frame(parent, bg=C["bg"])
    head = tk.Frame(box, bg=C["bg"])
    head.pack(fill="x")
    tk.Label(head, text="QUEUE", bg=C["bg"], fg=C["muted"], font=fh).pack(side="left")
    count = tk.Label(head, text="", bg=C["bg"], fg=C["faint"], font=fsm)
    count.pack(side="right")

    body = tk.Frame(box, bg=C["panel"])
    body.pack(fill="x", pady=(4, 0))
    lst = tk.Listbox(body, height=5, bg=C["panel"], fg=C["ink"], font=mono,
                     selectbackground=C.get("rust", C["panel2"]), selectforeground=C["ink"],
                     highlightthickness=0, borderwidth=0, activestyle="none")
    lst.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
    sb = tk.Scrollbar(body, orient="vertical", command=lst.yview,
                      troughcolor=C["panel"], bg=C["panel2"], borderwidth=0)
    lst.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y", pady=6)

    bar = tk.Frame(box, bg=C["bg"])
    bar.pack(fill="x", pady=(4, 0))
    # hint line under the buttons: why a button refused, or what the queue is for
    say = tk.Label(box, text="", bg=C["bg"], fg=C["faint"], font=fsm, anchor="w",
                   justify="left", wraplength=560)

    ids = []

    def _paint(msg=None):
        # keep the selection across the 2 s poll, or a row you picked is gone by the time you
        # click Delete — the poll repaints constantly
        keep = lst.curselection()
        keep_id = ids[keep[0]] if keep and keep[0] < len(ids) else None
        ids.clear()
        lst.delete(0, "end")
        # ⚠️ LIVE ENTRIES ONLY. Ryan, 2026-10-22: *"once the generation is completed it should
        # fall off the list of the queue."* This listed q.entries(), so every finished job stayed
        # as a row for ever (47 of them in his file, all done) and the panel read like a backlog
        # when nothing was actually waiting. The finished entries stay in the queue FILE (the last
        # KEEP_TERMINAL of them) so a window can still read its own outcome — they are just not
        # part of "the queue" any more.
        ents = q.live()
        for e in ents:
            ids.append(e.get("id"))
            lst.insert("end", jq.label(e))
        count.configure(text=_summary(q))
        if keep_id and keep_id in ids:
            lst.selection_set(ids.index(keep_id))
        if not ents:
            lst.insert("end", "   nothing queued — a job started while both cards are busy waits here")
        say.configure(text=msg or ("" if ents else
                                   "The queue is empty. Jobs appear here the moment a card is busy."))

    def _sel():
        s = lst.curselection()
        if not s:
            return None
        i = s[0]
        return ids[i] if 0 <= i < len(ids) and lst.get(i).strip() else None

    def _need():
        jid = _sel()
        if jid is None:
            _paint("Pick a row first.")
        return jid

    def _btn(text, fn, width=8):
        b = tk.Button(bar, text=text, command=fn, bg=C["panel2"], fg=C["ink"], font=fsm,
                      relief="flat", borderwidth=0, width=width, cursor="hand2",
                      activebackground=C.get("rust", C["panel2"]))
        b.pack(side="left", padx=(0, 6))
        return b

    def up():
        jid = _need()
        if jid:
            _paint("" if q.move(jid, -1) else "That one is already at the top (or it is running).")

    def down():
        jid = _need()
        if jid:
            _paint("" if q.move(jid, 1) else "That one is already at the bottom (or it is running).")

    def pause_toggle():
        jid = _need()
        if not jid:
            return
        e = q.get(jid) or {}
        st = e.get("state")
        if st == jq.STATE_PAUSED:
            q.resume(jid)
            _paint("Resumed.")
        elif st == jq.STATE_QUEUED:
            q.pause(jid)
            _paint("Paused — it keeps its place in line.")
        elif st == jq.STATE_RUNNING:
            _paint("That one is already on a card, so it cannot be paused — use Stop on the card.")
        else:
            _paint("Only waiting jobs can be paused.")

    def delete():
        jid = _need()
        if not jid:
            return
        if q.remove(jid):
            _paint("Deleted.")
        else:
            _paint("Only waiting jobs can be deleted; a running one has to be stopped first.")

    def run_next():
        jid = _need()
        if jid:
            _paint("Moved to the front." if q.bump(jid) else "That one is running already.")

    buttons = {"up": _btn("▲", up, 3), "down": _btn("▼", down, 3),
               "pause": _btn("Pause", pause_toggle, 7), "delete": _btn("Delete", delete, 7),
               "bump": _btn("Run next", run_next, 9)}
    say.pack(fill="x", pady=(2, 0))

    def _poll():
        try:
            _paint()
        except Exception:
            pass
        try:
            box.after(poll_ms, _poll)
        except Exception:
            pass

    _paint()
    _poll()
    box._queue_ids = ids          # tests read the rendered ids off the frame
    box._queue_listbox = lst
    box._queue_buttons = buttons
    box._queue_say = say
    box._queue_paint = _paint     # a test can repaint at once instead of waiting out the 2 s poll
    return box
