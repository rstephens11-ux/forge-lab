"""The CARD IS BUSY prompt (2026-10-17) — shared by every window that loads a model on forge.

    ┌ Card 2 is busy ─────────────────────────────────────────────────┐
    │ Your voice line can't start yet.                                │
    │ ComfyUI is making a video on both cards (Turbo) — “Gustav sniffs │
    │ the loaf”, started 2 min ago.                                   │
    │ [Wait for it] [Do mine first (the video goes back in line)]     │
    │ [Stop the video] [Cancel]                                       │
    └─────────────────────────────────────────────────────────────────┘

handle(win, exc, ctx, retry, say, owner): ask, do the chosen thing OFF the Tk thread (the rig
calls block), then call retry() ON the Tk thread. The caller runs its job again and, when that
job ends — however it ends — calls cards.release(owner), which puts any held work back in line.
Workers never touch Tk: each action fills a dict and win.after polls it.
"""
import threading
import time

import cards


def ask(win, exc, ctx, on_choice):
    """Show the prompt; on_choice(key) is called once, on the Tk thread."""
    import tkinter as tk
    C, F = ctx["C"], ctx["fonts"]
    f, fh, fsm = F["f"], F["fh"], F["fsm"]
    top = tk.Toplevel(win)
    top.title("%s is busy" % cards.card_name(exc.card))
    top.configure(bg=C["panel"])
    top.transient(win)
    top.resizable(False, False)
    body = tk.Frame(top, bg=C["panel"], padx=20, pady=16)
    body.pack(fill="both", expand=True)
    tk.Label(body, text="%s is busy" % cards.card_name(exc.card), bg=C["panel"], fg=C["ink"],
             font=fh, anchor="w").pack(fill="x")
    tk.Label(body, text="%s can't start yet." % cards.PURPOSE.get(exc.job, exc.job)
             .capitalize(), bg=C["panel"], fg=C["muted"], font=f, anchor="w").pack(
        fill="x", pady=(6, 2))
    for h in exc.holders:
        tk.Label(body, text="%s is %s." % (h["who"][0].upper() + h["who"][1:], h["doing"]),
                 bg=C["panel"], fg=C["ink"], font=f, anchor="w", justify="left",
                 wraplength=520).pack(fill="x", pady=(2, 0))
    row = tk.Frame(body, bg=C["panel"])
    row.pack(fill="x", pady=(14, 0))
    chosen = {"k": None}

    def pick(k):
        if chosen["k"] is not None:
            return
        chosen["k"] = k
        try:
            top.destroy()
        except Exception:
            pass
        on_choice(k)

    buttons = {}
    for i, (k, label) in enumerate(exc.options()):
        b = tk.Button(row, text=label, command=lambda k=k: pick(k), relief="flat",
                      borderwidth=0, highlightthickness=0, font=fh if i == 0 else fsm,
                      padx=12, pady=7 if i == 0 else 5,
                      bg=C["ochre"] if i == 0 else C["panel2"],
                      fg="#14110f" if i == 0 else C["ink"],
                      activebackground=C.get("rust", C["panel2"]),
                      activeforeground="#14110f")
        b.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 6), pady=(0, 6))
        buttons[k] = b
    row.columnconfigure(0, weight=1)
    row.columnconfigure(1, weight=1)
    top.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
    top.bind("<Escape>", lambda _e: pick("cancel"))
    top.update_idletasks()
    try:
        x = win.winfo_rootx() + max(0, (win.winfo_width() - top.winfo_reqwidth()) // 2)
        y = win.winfo_rooty() + 80
        top.geometry("+%d+%d" % (x, y))
    except Exception:
        pass
    top.cards_buttons = buttons
    top.cards_pick = pick
    win.cards_prompt = top            # drivers find the open prompt here
    return top


def act(win, exc, choice, owner, say, retry, state=None):
    """Do `choice` off the Tk thread, then retry() on it. `state` (a dict) may carry
    'cancel': True to stop a WAIT; it also reports 'phase'."""
    st = state if state is not None else {}
    st.update(done=False, ok=False, phase=choice, tick=None)
    if choice == "cancel":
        say("cancelled — nothing was started")
        return st
    what = exc.thing()

    def work():
        try:
            if choice == "wait":
                st["ok"] = cards.wait_for_room(exc.job, exc.card, exc.need,
                                               cancel=lambda: st.get("cancel"),
                                               on_tick=lambda cb: st.update(tick=cb))
            elif choice == "first":
                cards.hold(exc.holders, exc.card, owner)
                st["ok"] = cards.wait_for_room(exc.job, exc.card, exc.need, poll=2.0,
                                               timeout=180, cancel=lambda: st.get("cancel"))
            elif choice == "stop":
                cards.stop(exc.holders)
                st["ok"] = cards.wait_for_room(exc.job, exc.card, exc.need, poll=2.0,
                                               timeout=120, cancel=lambda: st.get("cancel"))
        except Exception as e:
            st["err"] = str(e)
        finally:
            st["done"] = True

    t0 = time.time()

    def poll():
        if not st["done"]:
            tick = st.get("tick")
            if choice == "wait":
                say("waiting for %s (%s) — %s"
                    % (cards.card_name(exc.card), (tick or exc).holders[0]["doing"]
                       if (tick or exc).holders else "busy", cards.ago(time.time() - t0)))
            elif choice == "first":
                say("putting %s back in line so yours goes first…" % what)
            else:
                say("stopping %s…" % what)
            win.after(700, poll)
            return
        if st.get("cancel"):
            cards.release(owner)
            say("stopped waiting — nothing was started")
            return
        if not st.get("ok"):
            cards.release(owner)
            say("%s still has no room (%s) — try again in a moment"
                % (cards.card_name(exc.card), st.get("err") or "something else took it"),
                warn=True)
            return
        if choice == "first":
            say("%s is waiting in line; starting yours" % what)
        retry()
    threading.Thread(target=work, daemon=True).start()
    win.after(400, poll)
    return st


def handle(win, exc, ctx, retry, say, owner):
    """ask() then act(). retry() runs on the Tk thread once there is room."""
    def chosen(k):
        act(win, exc, k, owner, say, retry)
    return ask(win, exc, ctx, chosen)


# ================================================================== the CARDS panel
def build_panel(parent, ctx, poll_ms=5000):
    """Both cards at a glance, under RUN in the main window:

        CARDS                                                   checked 3 s ago
        Card 1  ▕█████░░░░░▏ 9.7 / 24 GB · working 0%
            ComfyUI · nothing right now, just keeping a model loaded …  9.7 GB  [Free]
        Card 2  ▕█████████░▏ 21.1 / 24 GB · working 0%
            a voice job · making a voice line … looks stuck              4.1 GB  [Stop]

    The rig is read on a THREAD every `poll_ms` (an ssh round-trip, 3 s when forge is down);
    the Tk thread only repaints from the last answer. Free = let go of an idle model (nothing
    stops). Stop = end something that is working or stuck, after asking."""
    import tkinter as tk
    C, F = ctx["C"], ctx["fonts"]
    fsm, fh = F["fsm"], F["fh"]
    mono = (ctx.get("mono") or {}).get("s") or fsm
    box = tk.Frame(parent, bg=C["panel"])
    head = tk.Frame(box, bg=C["panel"])
    head.pack(fill="x")
    tk.Label(head, text="CARDS", bg=C["panel"], fg=C["muted"], font=fh).pack(side="left")
    ago_lbl = tk.Label(head, text="checking forge…", bg=C["panel"], fg=C["faint"],
                       font=fsm)
    ago_lbl.pack(side="right")
    # AUTO-UNLOAD: how long an idle model may stay loaded (cards.reap runs after each check)
    idle_lab = {m: ("never" if m is None else "%d min" % m) for m in cards.IDLE_CHOICES}
    idle_var = tk.StringVar(value=idle_lab.get(cards.idle_minutes(), "10 min"))

    def _set_idle(label):
        idle_var.set(label)
        m = next(k for k, v in idle_lab.items() if v == label)
        cards.set_idle_minutes(m)
        state["shown"] = None
    tk.Label(head, text="unload idle models after", bg=C["panel"], fg=C["faint"],
             font=fsm).pack(side="left", padx=(14, 4))
    idle_menu = tk.OptionMenu(head, idle_var, *idle_lab.values(), command=_set_idle)
    idle_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, borderwidth=0, width=6,
                        activebackground=C.get("rust", C["panel2"]))
    idle_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    idle_menu.pack(side="left")
    reaped_lbl = tk.Label(box, text="", bg=C["panel"], fg=C["faint"], font=fsm, anchor="w")
    reaped_lbl.pack(fill="x")
    rows = tk.Frame(box, bg=C["panel"])
    rows.pack(fill="x", pady=(4, 0))
    state = {"snap": None, "busy": False, "shown": None, "t": 0.0, "msg": ""}

    def fetch():
        if state["busy"] or not cards.enabled():
            return
        state["busy"] = True

        def work():
            try:
                state["snap"] = cards.snapshot()
                state["t"] = time.time()
                if cards.reap(state["snap"]):
                    time.sleep(2.0)                    # let the card show the memory back
                    state["snap"] = cards.snapshot()
                    state["t"] = time.time()
            finally:
                state["busy"] = False
        threading.Thread(target=work, daemon=True).start()

    def act_on(h, what):
        if what == "stop":
            from tkinter import messagebox            # handler-time import (AGENTS.md)
            if not messagebox.askyesno(
                    "Stop it?", "Stop %s?\n\nIt is %s.\n\nAnything it was making is lost."
                    % (h["who"], h["doing"]), parent=box.winfo_toplevel()):
                return
        state["msg"] = ("freeing %s…" if what == "free" else "stopping %s…") % h["who"]

        def work():
            if what == "free":
                cards.free_holder(h)
            else:
                cards.stop([h])
            time.sleep(2.0)
            state["msg"] = ""
            state["shown"] = None
            fetch()
        threading.Thread(target=work, daemon=True).start()

    def bar(parent_, used, total):
        cv = tk.Canvas(parent_, width=110, height=10, bg="#100e0c", highlightthickness=1,
                       highlightbackground=C["line"])
        frac = (used / float(total)) if total else 0.0
        cv.create_rectangle(0, 0, int(110 * min(1.0, frac)), 10, width=0,
                            fill=C["rust"] if frac > 0.85 else C["moss"])
        return cv

    def paint():
        snap = state["snap"]
        key = None if snap is None else json_key(snap)
        if key != state["shown"]:
            state["shown"] = key
            for w in rows.winfo_children():
                w.destroy()
            if snap is None:
                tk.Label(rows, text=("card check is off" if not cards.enabled() else
                                     "can't read forge's cards right now (ssh)"),
                         bg=C["panel"], fg=C["faint"], font=fsm, anchor="w").pack(fill="x")
            for card in (snap or {}).get("cards") or []:
                r = tk.Frame(rows, bg=C["panel"])
                r.pack(fill="x", pady=(4, 0))
                tk.Label(r, text=cards.card_name(card["index"]), bg=C["panel"], fg=C["ink"],
                         font=fsm, width=7, anchor="w").pack(side="left")
                bar(r, card["used"], card["total"]).pack(side="left", padx=(0, 8))
                _q = card.get("queue")
                _line = ("" if not _q or not (_q[0] or _q[1]) else
                         " · %d making, %d waiting" % _q)
                tk.Label(r, text="%.1f / %.0f GB · working %d%%%s"
                         % (card["used"] / 1024.0, card["total"] / 1024.0, card["util"],
                            _line),
                         bg=C["panel"], fg=C["muted"], font=mono, anchor="w").pack(side="left")
                holders = [h for h in card["holders"] if h["mb"] >= 300]
                if not holders:
                    tk.Label(rows, text="      free", bg=C["panel"], fg=C["faint"], font=fsm,
                             anchor="w").pack(fill="x")
                for h in holders:
                    hr = tk.Frame(rows, bg=C["panel"])
                    hr.pack(fill="x")
                    if h.get("freeable"):
                        b = tk.Button(hr, text="Free", command=lambda h=h: act_on(h, "free"))
                    else:
                        b = tk.Button(hr, text="Stop", command=lambda h=h: act_on(h, "stop"))
                    b.configure(relief="flat", borderwidth=0, highlightthickness=0, font=fsm,
                                padx=8, pady=1, bg=C["ink"], fg="#14110f",
                                activebackground=C["ochre"])
                    b.pack(side="right")
                    tk.Label(hr, text="%.1f GB" % (h["mb"] / 1024.0), bg=C["panel"],
                             fg=C["muted"], font=mono).pack(side="right", padx=6)
                    fg = C["ochre"] if h.get("stuck") else (C["ink"] if h.get("busy")
                                                             else C["muted"])
                    _doing = (("the same work as on %s" % cards.card_name(h["same_as"]))
                              if h.get("same_as") is not None else h["doing"])
                    if (h["kind"] == "comfy" and h.get("freeable") and h.get("same_as") is None
                            and cards.idle_for(h["port"]) >= 60):
                        _m = cards.idle_minutes()
                        _doing += " \u00b7 idle %s%s" % (
                            cards.ago(cards.idle_for(h["port"])),
                            "" if _m is None else ", unloads at %d min" % _m)
                    tk.Label(hr, text="%s%s · %s" % ("⚠ " if h.get("stuck") else "   ",
                                                          h["who"], _doing),
                             bg=C["panel"], fg=fg, font=fsm, anchor="w", justify="left",
                             wraplength=430).pack(side="left", fill="x", expand=True)
                    hr.cards_holder = h            # drivers read what each row is
                    hr.cards_button = b
        if cards.REAPED:
            r = cards.REAPED[-1]
            reaped_lbl.configure(text="auto-unloaded %s (%.1f GB) %s ago, after %s idle"
                                 % (r["who"], r["mb"] / 1024.0, cards.ago(time.time() - r["t"]),
                                    idle_var.get()))
        if state["msg"]:
            ago_lbl.configure(text=state["msg"])
        elif state["t"]:
            ago_lbl.configure(text="checked %s ago" % cards.ago(time.time() - state["t"]))

    def tick():
        try:
            if not box.winfo_exists():
                return
        except Exception:
            return
        try:
            shown = bool(box.winfo_ismapped())
        except Exception:
            shown = True
        if shown:                 # hidden behind "Show the machine room": no ssh every 5 s
            paint()
            if time.time() - state["t"] >= poll_ms / 1000.0:
                fetch()
        box.after(1000, tick)

    box.cards_state = state
    box.cards_idle = (idle_var, _set_idle)
    box.cards_reaped = reaped_lbl
    box.cards_rows = rows
    box.cards_refresh = lambda: (state.update(t=0.0, shown=None), fetch())
    box.after(300, tick)
    return box


def json_key(snap):
    """What the panel shows, as a comparable key — repaint only when it CHANGES."""
    out = []
    for c in snap.get("cards") or []:
        out.append((c["index"], c["used"] // 256, c["util"] // 10, c.get("queue"),
                    tuple((h["pid"], h["mb"] // 256, h.get("doing"), h.get("stuck"))
                          for h in c["holders"])))
    # the idle clocks + the setting change what the rows SAY, so they repaint too
    out.append(tuple(sorted((p, int(cards.idle_for(p) // 60)) for p in cards.LAST_BUSY)))
    out.append(cards.idle_minutes())
    return tuple(out)
