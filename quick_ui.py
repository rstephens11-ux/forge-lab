"""The QUICK window (2026-10-17) — one idea in, a finished video out.

    Quick                one idea → a finished video · everything opens in Studio      Close
    ┌ WHAT HAPPENS ──────────────────────────────┐  ┌ PROGRESS ────────────────────────┐
    │ [a weatherman and a conductor argue about…] │  │ ✓ Cast         Clarence, Gustav  │
    │ FORMAT  [Two-hander sketch][Weather]…       │  │ ✓ Voices                         │
    │ CAST    (Clarence)(Gustav)… none = invent   │  │ ● Script       writing…          │
    │ LENGTH  15 20 30 45 60   SHAPE  9:16  16:9  │  │ ○ Read the lines                 │
    │ ☐ check each step   ☐ lock their looks      │  │ ○ The set   ○ Shoot it   ○ Cut   │
    │ [ Make it · ~16 min ]                       │  │  review / result / error here    │
    └─────────────────────────────────────────────┘  └──────────────────────────────────┘

Engine: quick.py (Tk-free). The run is on a worker thread; the Tk thread polls the job.
"""
import os
import sys
import subprocess
import threading
import time

import formats
import previews
import quick

# macOS draws its own white buttons and ignores bg: light text on them vanished
# ("Stop after this step" was blank on the MacBook) - dark text there, always
_MAC = sys.platform == "darwin"

ICON = {"todo": "○", "running": "●", "done": "✓", "review": "⏸",
        "skip": "–"}


def open_quick(root, ctx):
    import tkinter as tk
    import tkinter.font as tkfont

    C = ctx["C"]
    f, fh, fsm = ctx["fonts"]["f"], ctx["fonts"]["fh"], ctx["fonts"]["fsm"]
    fbig = tkfont.Font(family=fh.cget("family"), size=max(15, int(fh.cget("size")) + 5),
                       weight="bold")
    fidea = tkfont.Font(family=f.cget("family"), size=max(15, int(f.cget("size")) + 3))
    fl = ctx["mod"]
    import dock as _dock                 # a tab in the main window (Ryan: tabs)
    win = _dock.window(root)
    win.title("Quick")
    win.configure(bg=C["bg"])
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1240, _sw - 40), min(820, _sh - 110)))
    win.minsize(1060, 680)
    st = {"fmt": formats.FORMATS[0]["key"], "cast": [], "secs": None, "aspect": None,
          "quality": "Draft", "copies": 1, "queue": [], "captions": "pop",
          "job": None, "busy": False, "stop": False}

    def lab(parent, text, fg=None, font=None, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["muted"],
                        font=font or fsm, anchor="w", **kw)

    def button(parent, text, cmd, primary=False, **kw):
        return tk.Button(parent, text=text, command=cmd, relief="flat", borderwidth=0,
                         font=fh if primary else fsm, padx=14, pady=9 if primary else 5,
                         bg=C["ochre"] if primary else C["panel2"],
                         fg="#14110f" if primary or _MAC else C["ink"],
                         activebackground="#d9b45e" if primary else C["rust"],
                         activeforeground="#14110f", highlightthickness=0, **kw)

    def chip(parent, text, on, cmd, sub=None):
        fr = tk.Frame(parent, bg=C["ink"] if on else C["panel2"], cursor="hand2")
        l1 = tk.Label(fr, text=text, bg=fr["bg"], fg="#14110f" if on else C["ink"],
                      font=fh if sub else fsm, padx=10, pady=(4 if sub else 5), anchor="w")
        l1.pack(fill="x")
        ws = [fr, l1]
        if sub:
            l2 = tk.Label(fr, text=sub, bg=fr["bg"], fg="#3a332c" if on else C["muted"],
                          font=fsm, padx=10, anchor="w", wraplength=170, justify="left")
            l2.pack(fill="x", pady=(0, 5))
            ws.append(l2)
        for w_ in ws:
            w_.bind("<Button-1>", lambda _e: cmd())
        return fr

    # ------------------------------------------------------------------ header
    hd = tk.Frame(win, bg=C["panel"])
    hd.pack(fill="x")
    tk.Label(hd, text="Quick", bg=C["panel"], fg=C["ink"], font=fbig).pack(
        side="left", padx=(16, 14), pady=10)
    lab(hd, "one idea → a finished video. Fine-tune anything it makes in the Episode and "
            "Dialogue tabs.").pack(side="left")
    button(hd, "Close", lambda: win.destroy()).pack(side="right", padx=12)
    tk.Frame(win, bg=C["line"], height=1).pack(fill="x")
    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=16, pady=12)

    # ------------------------------------------------------------------ left: the ask
    # the left column SCROLLS (More options open on a 790 px MacBook ran off the bottom);
    # Make it + the time note stay pinned under it
    left_outer = tk.Frame(body, bg=C["panel"], highlightthickness=1,
                          highlightbackground=C["line"])
    left_outer.pack(side="left", fill="both", expand=True, padx=(0, 14))
    left_foot = tk.Frame(left_outer, bg=C["panel"])
    left_foot.pack(side="bottom", fill="x")
    left_cv = tk.Canvas(left_outer, bg=C["panel"], highlightthickness=0, borderwidth=0)
    left_sb = tk.Scrollbar(left_outer, orient="vertical", command=left_cv.yview)
    left_cv.configure(yscrollcommand=left_sb.set)
    left_sb.pack(side="right", fill="y")
    left_cv.pack(side="left", fill="both", expand=True)
    left = tk.Frame(left_cv, bg=C["panel"])
    _left_win = left_cv.create_window(0, 0, window=left, anchor="nw")
    left.bind("<Configure>", lambda _e: left_cv.configure(scrollregion=left_cv.bbox("all")))
    left_cv.bind("<Configure>", lambda e: left_cv.itemconfigure(_left_win, width=e.width))
    try:
        fl.scoped_wheel(win, left_cv)
    except Exception:
        pass
    ihd = tk.Frame(left, bg=C["panel"])
    ihd.pack(fill="x", padx=16, pady=(14, 4))
    lab(ihd, "WHAT HAPPENS?", fg=C["faint"]).pack(side="left")
    idea_lnk = tk.Label(ihd, text="\u2728 give me ideas", bg=C["panel"], fg=C["ochre"],
                        font=fsm, cursor="hand2")
    idea_lnk.pack(side="right")
    idea_lnk.bind("<Button-1>", lambda _e: _ideas())
    t_idea = tk.Text(left, height=3, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                     font=fidea, padx=10, pady=8, insertbackground=C["ink"])
    t_idea.pack(fill="x", padx=16)
    lab(left, "one sentence is enough — or leave it empty and let it surprise you",
        fg=C["faint"]).pack(fill="x", padx=16, pady=(4, 0))
    ideas_box = tk.Frame(left, bg=C["panel"])
    ideas_box.pack(fill="x", padx=16)
    # THE IDEA NAMES ITS FORMAT ("a weatherman who...", "if Death had a vlog"): a one-click
    # suggestion; if you never picked a format yourself, Make it uses it
    guess_lbl = tk.Label(left, text="", bg=C["panel"], fg=C["ochre"], font=fsm, anchor="w",
                         cursor="hand2")
    guess_lbl.bind("<Button-1>", lambda _e: _take_guess())
    # ONE VIDEO PER LINE: several ideas typed one per line -> a batch, made back to back
    perline_var = tk.BooleanVar(value=False)
    perline_cb = tk.Checkbutton(left, text="", variable=perline_var, bg=C["panel"],
                                fg=C["ink"], selectcolor="#100e0c", activebackground=C["panel"],
                                font=fsm, anchor="w", command=lambda: _paint_go())

    # SERIES: the show this video is an episode of (series.py)
    ser_row = tk.Frame(left, bg=C["panel"])
    ser_row.pack(fill="x", padx=16, pady=(12, 0))
    lab(ser_row, "SERIES", fg=C["faint"]).pack(side="left")
    ser_box = tk.Frame(ser_row, bg=C["panel"])
    ser_box.pack(side="left", padx=8)
    ser_note = lab(left, "", fg=C["faint"], wraplength=620, justify="left")

    lab(left, "FORMAT", fg=C["faint"]).pack(fill="x", padx=16, pady=(14, 4))
    fmt_box = tk.Frame(left, bg=C["panel"])
    fmt_box.pack(fill="x", padx=16)
    for _i in range(4):
        fmt_box.columnconfigure(_i, weight=1, uniform="fmt")
    # PRODUCT -> AD (Product ad only): a real product's photo goes in the presenter's hands
    prod_row = tk.Frame(left, bg=C["panel"])
    prod_top = tk.Frame(prod_row, bg=C["panel"])
    prod_top.pack(fill="x")
    lab(prod_top, "YOUR PRODUCT", fg=C["faint"]).pack(side="left")
    prod_pick = tk.Label(prod_top, text="  Choose a photo\u2026",
                         bg=C["panel"], fg=C["ochre"], font=fsm,
                         cursor="hand2")
    prod_pick.pack(side="left")
    prod_pick.bind("<Button-1>", lambda _e: _pick_product())
    prod_thumb = tk.Label(prod_top, bg=C["panel"])
    prod_thumb.pack(side="left", padx=(8, 0))
    prod_name_var = tk.StringVar()
    prod_what_var = tk.StringVar()
    for _v, _t in ((prod_name_var, "name"), (prod_what_var, "what it does")):
        _r = tk.Frame(prod_row, bg=C["panel"])
        _r.pack(fill="x", pady=(4, 0))
        lab(_r, "%-13s" % _t, fg=C["muted"]).pack(side="left")
        tk.Entry(_r, textvariable=_v, bg="#100e0c", fg=C["ink"], relief="flat", font=fsm,
                 insertbackground=C["ink"]).pack(side="left", fill="x", expand=True, ipady=2)
    lab(prod_row, "Leave it empty for an invented product. With a photo, the presenter "
                  "holds it, the ad is about it (honest claims only), and 3 versions get "
                  "different hooks.", fg=C["faint"], wraplength=620,
        justify="left").pack(fill="x", pady=(4, 0))

    lab(left, "CAST", fg=C["faint"]).pack(fill="x", padx=16, pady=(14, 4))
    cast_box = tk.Frame(left, bg=C["panel"])
    cast_box.pack(fill="x", padx=16)
    cast_note = lab(left, "", fg=C["faint"])
    cast_note.pack(fill="x", padx=16, pady=(4, 0))
    # SHAPE stays up front; everything else waits behind MORE OPTIONS (review panel, new
    # user: "Higgsfield shows one box and one button ... I'm afraid to leave anything at its
    # default")
    shape_row = tk.Frame(left, bg=C["panel"])
    shape_row.pack(fill="x", padx=16, pady=(14, 0))
    lab(shape_row, "SHAPE", fg=C["faint"]).pack(side="left")
    asp_box = tk.Frame(shape_row, bg=C["panel"])
    asp_box.pack(side="left", padx=8)
    more_hdr = tk.Frame(left, bg=C["panel"], cursor="hand2")
    more_hdr.pack(fill="x", padx=16, pady=(14, 0))
    more_lbl = tk.Label(more_hdr, text="", bg=C["panel"], fg=C["ochre"], font=fsm,
                        anchor="w", cursor="hand2")
    more_lbl.pack(side="left")
    more_sum = tk.Label(more_hdr, text="", bg=C["panel"], fg=C["faint"], font=fsm,
                        anchor="w", cursor="hand2", justify="left", wraplength=520)
    more_sum.pack(side="left", padx=(10, 0), fill="x", expand=True)   # wraps, never cut off
    for _w in (more_hdr, more_lbl, more_sum):
        _w.bind("<Button-1>", lambda _e: _toggle_more())
    more = tk.Frame(left, bg=C["panel"])

    row = tk.Frame(more, bg=C["panel"])
    row.pack(fill="x", padx=0, pady=(8, 0))
    lab(row, "LENGTH", fg=C["faint"]).pack(side="left")
    len_box = tk.Frame(row, bg=C["panel"])
    len_box.pack(side="left", padx=(8, 24))
    # any length, not 15-second steps (Ryan: "the length should be adjustable")
    len_var = tk.IntVar(value=formats.FORMATS[0]["seconds"])
    len_scale = tk.Scale(len_box, from_=formats.MIN_SECONDS, to=formats.MAX_SECONDS,
                         resolution=1, orient="horizontal", variable=len_var, length=220,
                         showvalue=False, bg=C["panel"], fg=C["ink"], highlightthickness=0,
                         troughcolor="#100e0c", command=lambda _v: _len_moved())
    len_scale.pack(side="left")
    len_lbl = tk.Label(len_box, text="", bg=C["panel"], fg=C["ink"], font=fh, width=16,
                       anchor="w")
    len_lbl.pack(side="left", padx=(8, 0))
    row2 = tk.Frame(more, bg=C["panel"])
    row2.pack(fill="x", padx=0, pady=(10, 0))
    lab(row2, "LOOK", fg=C["faint"]).pack(side="left")      # own row: it was cut off at 1240 px
    q_box = tk.Frame(row2, bg=C["panel"])
    q_box.pack(side="left", padx=(8, 24))
    lab(row2, "VERSIONS", fg=C["faint"]).pack(side="left")
    n_box = tk.Frame(row2, bg=C["panel"])
    n_box.pack(side="left", padx=8)
    n_hint = lab(row2, "", fg=C["faint"])
    n_hint.pack(side="left")
    # CAPTIONS — how the words on screen look (not for a music video: no captions there)
    cap_row = tk.Frame(more, bg=C["panel"])
    cap_row.pack(fill="x", padx=0, pady=(10, 0))
    lab(cap_row, "CAPTIONS", fg=C["faint"]).pack(side="left")
    cap_box = tk.Frame(cap_row, bg=C["panel"])
    cap_box.pack(side="left", padx=8)
    CAP_LABELS = (("none", "None"), ("clean", "Clean"), ("pop", "Pop \u00b7 word lights up"),
                  ("big", "Big \u00b7 one word at a time"),
                  ("subtitle", "Subtitle \u00b7 small and gold"))
    # MUSIC — a song from the library under the video (it fills itself: every song made in
    # the Song window, plus any file Ryan adds)
    mus_row = tk.Frame(more, bg=C["panel"])
    mus_row.pack(fill="x", padx=0, pady=(10, 0))
    lab(mus_row, "MUSIC", fg=C["faint"]).pack(side="left")
    mus_box = tk.Frame(mus_row, bg=C["panel"])
    mus_box.pack(side="left", padx=8)
    # PACE — how much silence stays between lines (each format has its own default)
    pace_row = tk.Frame(more, bg=C["panel"])
    pace_row.pack(fill="x", padx=0, pady=(10, 0))
    lab(pace_row, "PACE", fg=C["faint"]).pack(side="left")
    pace_box = tk.Frame(pace_row, bg=C["panel"])
    pace_box.pack(side="left", padx=8)
    pace_hint = lab(pace_row, "", fg=C["faint"])
    pace_hint.pack(side="left")
    # PLACE — set it in a kept place (Places page), or let the idea decide
    place_row = tk.Frame(left, bg=C["panel"])
    place_row.pack(fill="x", padx=16, pady=(10, 0), after=shape_row)
    lab(place_row, "PLACE", fg=C["faint"]).pack(side="left")
    place_box = tk.Frame(place_row, bg=C["panel"])
    place_box.pack(side="left", padx=8)
    PACE_HINTS = {"snappy": "tight cuts, no dead air", "natural": "a breath between lines",
                  "breathe": "room for awkward pauses",
                  "full": "every clip whole, nothing trimmed"}

    # SOUND — only for a music video: the Song window's sounds, by family
    snd_row = tk.Frame(left, bg=C["panel"])
    lab(snd_row, "SOUND", fg=C["faint"]).pack(side="left")
    snd_var = tk.StringVar(value="")
    snd_mb = tk.Menubutton(snd_row, textvariable=snd_var, bg=C["panel2"], fg=C["ink"],
                           font=fsm, relief="flat", padx=10, pady=4, width=28, anchor="w")
    snd_m = tk.Menu(snd_mb, tearoff=0, bg=C["panel2"], fg="#000000", font=fsm)
    try:
        import song as _song
        _fams = {}
        for _fam, _n, _t, _v in _song.all_sounds():
            _fams.setdefault(_fam, []).append(_n)
        for _fam, _names in _fams.items():
            _sm = tk.Menu(snd_m, tearoff=0, bg=C["panel2"], fg="#000000", font=fsm)
            for _n in _names:
                _sm.add_command(label=_n, command=lambda n=_n: (
                    snd_var.set(n), st.__setitem__("song", None), _paint_go()))
            snd_m.add_cascade(label=_fam, menu=_sm)
    except Exception:
        pass
    snd_mb.configure(menu=snd_m)
    snd_mb.pack(side="left", padx=8)
    lab(snd_row, "the song is made in this sound; your character sings it",
        fg=C["faint"]).pack(side="left")

    check_var = tk.BooleanVar(value=False)
    looks_var = tk.BooleanVar(value=False)
    setcheck_var = tk.BooleanVar(value=True)
    pick_var = tk.BooleanVar(value=True)
    hook_var = tk.BooleanVar(value=True)
    react_var = tk.BooleanVar(value=True)
    long_var = tk.BooleanVar(value=True)
    weird_var = tk.BooleanVar(value=False)
    for var, txt in ((hook_var, "A hook line at the top for the first 2 seconds (makes "
                                "people stop scrolling)"),
                     (react_var, "Reaction shots \u2014 cut to the listener's face after the "
                                 "punchline (+ about 2.5 min per character)"),
                     (long_var, "Long takes for one-person videos \u2014 lines run together "
                                "in shots of up to 15 s, fewer cuts"),
                     (weird_var, "Weirdos \u2014 new characters get one ridiculous feature "
                                 "(otherwise ordinary people, unless the idea asks)"),
                     (pick_var, "Let me pick the script \u2014 show me three drafts "
                                "before anything is filmed (recommended)"),
                     (setcheck_var, "Show me the scene before filming \u2014 check it looks "
                                    "like them (recommended)"),
                     (check_var, "Check each step \u2014 stop after the voices, the jokes and "
                                 "the scene so I can change them"),
                     (looks_var, "Keep faces consistent \u2014 draw each character from every "
                                 "side first (slower, + about 3 min each)")):
        _cb = tk.Checkbutton(more, text=txt, variable=var, bg=C["panel"], fg=C["ink"],
                             selectcolor="#100e0c", activebackground=C["panel"], font=fsm,
                             anchor="w", command=lambda: (_paint_go(), _paint_more()))
        _cb.pack(fill="x", padx=0, pady=(8, 0))
        if var is check_var:
            check_box = _cb
    # PAID WORK (licences.py): saved app-wide, not per video. Fish S2-Pro voices are
    # non-commercial; with this on, anything that would use them refuses with a message
    import licences as _lic
    paid_var = tk.BooleanVar(value=_lic.paid_work())
    paid_cb = tk.Checkbutton(more, text="Paid work (an ad, a client) — only use engines "
                                        "licensed for it (Fish voices are blocked)",
                             variable=paid_var, bg=C["panel"], fg=C["ink"],
                             selectcolor="#100e0c", activebackground=C["panel"], font=fsm,
                             anchor="w", command=lambda: (_lic.set_paid_work(paid_var.get()),
                                                          _paint_more()))
    paid_cb.pack(fill="x", padx=0, pady=(8, 0))
    b_go = button(left_foot, "Make it", lambda: _go(), primary=True)
    # pinned at the bottom, outside the scrolling column: on a short screen the rows above
    # scroll, the button never moves (on Ryan's MacBook Make it was once cut off)
    go_row = tk.Frame(left_foot, bg=C["panel"])
    go_row.pack(side="top", fill="x", padx=16, pady=(8, 4))
    b_go.pack(in_=go_row, side="left")
    b_go.lift(go_row)                    # made before go_row: without this it sits BEHIND it
    night_lnk = tk.Label(go_row, text="+ add to tonight's list", bg=C["panel"], fg=C["ochre"],
                         font=fsm, cursor="hand2")
    night_lnk.pack(side="left", padx=(14, 0))
    night_lnk.bind("<Button-1>", lambda _e: _add_tonight())
    wait_lbl = lab(left_foot, "", fg=C["faint"], wraplength=640, justify="left")
    wait_lbl.pack(side="top", fill="x", padx=16, pady=(0, 10))

    # ------------------------------------------------------------------ right: the run
    right = tk.Frame(body, bg=C["panel"], width=440, highlightthickness=1,
                     highlightbackground=C["line"])
    right.pack(side="right", fill="y", before=left_outer)   # its 440 px first; the ask takes the rest
    right.pack_propagate(False)
    lab(right, "PROGRESS", fg=C["faint"]).pack(fill="x", padx=16, pady=(14, 4))
    steps_box = tk.Frame(right, bg=C["panel"])
    steps_box.pack(fill="x", padx=16)
    now_lbl = lab(right, "", fg=C["ink"], wraplength=380, justify="left")
    now_lbl.pack(fill="x", padx=16, pady=(8, 0))
    recent = tk.Frame(right, bg=C["panel"])
    recent.pack(fill="x", side="bottom", padx=16, pady=(0, 12))
    # review / result / error — scrolls: the done panel (flags, CHANGE IT, post) and
    # ALL MY VIDEOS can be taller than the window
    stage_cv = tk.Canvas(right, bg=C["panel"], highlightthickness=0, borderwidth=0)
    stage_cv.pack(fill="both", expand=True, padx=(16, 4), pady=(10, 0))
    stage = tk.Frame(stage_cv, bg=C["panel"])
    _stage_win = stage_cv.create_window(0, 0, window=stage, anchor="nw")
    stage.bind("<Configure>",
               lambda _e: stage_cv.configure(scrollregion=stage_cv.bbox("all")))
    stage_cv.bind("<Configure>", lambda e: stage_cv.itemconfigure(_stage_win, width=e.width))
    try:
        fl.scoped_wheel(win, stage_cv, add=True)
    except Exception:
        pass

    # ================================================================== painting the ask
    def _from_song(path, title, lyrics):
        """The Song window's VIDEO button: a music video of THAT song."""
        st["fmt"] = "music"
        st["song"] = {"path": path, "title": title, "lyrics": lyrics}
        st["secs"] = None
        t_idea.delete("1.0", "end")
        t_idea.insert("1.0", "a music video for \u201c%s\u201d" % title)
        _paint_all()
        win.deiconify()
        win.lift()
        now_lbl.configure(text="using your song \u201c%s\u201d \u2014 pick who sings it, then "
                               "Make it" % title)

    def _ideas():
        for w_ in ideas_box.winfo_children():
            w_.destroy()
        lab(ideas_box, "thinking of a few\u2026", fg=C["faint"]).pack(fill="x", pady=(4, 0))
        job = {"done": False, "out": None, "err": None}
        fmt, cast, secs = st["fmt"], list(st["cast"]), st["secs"]
        ser_ = st.get("series")

        def work():
            try:
                d = ctx.get("director")
                if d is None:
                    import director as _d
                    d = _d.Director()
                if ser_:
                    import series as _sr       # the next episodes of the picked series
                    job["out"] = _sr.next_ideas(d, _sr.load(ser_), 3)
                else:
                    job["out"] = quick.ideas(d, fmt, cast, 3, secs)
            except Exception as exc:
                job["err"] = str(exc)
            finally:
                job["done"] = True

        def poll():
            if not job["done"]:
                win.after(400, poll)
                return
            for w_ in ideas_box.winfo_children():
                w_.destroy()
            if job["err"] or not job["out"]:
                lab(ideas_box, "the writer couldn't be reached \u2014 %s"
                    % (job["err"] or "no ideas came back")[:80], fg=C["ochre"]).pack(
                    fill="x", pady=(4, 0))
                return
            for idea in job["out"]:
                l_ = tk.Label(ideas_box, text="\u25b8 " + idea, bg=C["panel"], fg=C["ink"],
                              font=fsm, anchor="w", justify="left", wraplength=620,
                              cursor="hand2")
                l_.pack(fill="x", pady=(4, 0))
                l_.bind("<Button-1>", lambda _e, t=idea: (t_idea.delete("1.0", "end"),
                                                          t_idea.insert("1.0", t)))
        threading.Thread(target=work, daemon=True).start()
        win.after(400, poll)

    def _job_like():
        return {"format": st["fmt"], "quality": st["quality"], "reactions": react_var.get(),
                "seconds": st["secs"] or formats.get(st["fmt"])["seconds"],
                "aspect": st["aspect"], "want_cast": st["cast"],
                "steps": {s: ("todo" if s != "looks" or looks_var.get() else "skip")
                          for s in quick.STEPS}}

    def _paint_fmt():
        for w_ in fmt_box.winfo_children():
            w_.destroy()
        st["fmt_loops"] = {}
        loops = _fmt_frames()
        for i, fm in enumerate(formats.FORMATS):
            ch = chip(fmt_box, fm["name"], fm["key"] == st["fmt"],
                      lambda k=fm["key"]: _pick_fmt(k, user=True), sub=fm["blurb"])
            ch.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0, 6), pady=(0, 6))
            if loops.get(fm["key"]):
                # a flick-book of YOUR last video in this format; hover plays it
                pic = tk.Label(ch, bg="#100e0c", cursor="hand2")
                pic.pack(fill="x", before=ch.winfo_children()[0])
                pic.bind("<Button-1>", lambda _e, k=fm["key"]: _pick_fmt(k, user=True))
                st["fmt_loops"][fm["key"]] = previews.Loop(
                    pic, loops[fm["key"]], hover_widgets=tuple(ch.winfo_children()) + (ch,))

    def _family(font):
        try:
            return font.actual("family") if hasattr(font, "actual") else (
                font[0] if isinstance(font, (tuple, list)) else None)
        except Exception:
            return None

    _ff = {"frames": None, "busy": False}

    def _fmt_frames(refresh=False):
        """{format: frames} from your finished videos; made on a worker the first time (and
        again after a video finishes), then the format cards repaint."""
        if _ff["frames"] is not None and not refresh:
            return _ff["frames"]
        if not _ff["busy"]:
            _ff["busy"] = True
            res = {}

            def work():
                out = {}
                try:
                    jobs = quick.list_jobs()
                    for fm in formats.FORMATS:
                        fr = previews.format_loop(fm["key"], jobs)
                        if fr:
                            out[fm["key"]] = fr
                except Exception:
                    pass
                res["out"] = out

            def poll():
                if "out" not in res:
                    win.after(300, poll)
                    return
                _ff["busy"] = False
                changed = res["out"] != (_ff["frames"] or {})
                _ff["frames"] = res["out"]
                if changed:
                    try:
                        _paint_fmt()
                    except tk.TclError:
                        pass
            threading.Thread(target=work, daemon=True).start()
            win.after(300, poll)
        return _ff["frames"] or {}

    def _pick_fmt(k, user=False):
        if user:
            st["fmt_picked"] = True               # your choice wins over the idea's hint
        if k != "music":
            st["song"] = None                     # a different format: not your song any more
        st["fmt"] = k
        if formats.get(k).get("captions"):        # a format with its own caption look
            st["captions"] = formats.get(k)["captions"]
        st["pace"] = None                         # the new format's own pace
        st["secs"] = None
        st["aspect"] = None
        _paint_all()

    def _paint_cast():
        for w_ in cast_box.winfo_children():
            w_.destroy()
        fm = formats.get(st["fmt"])
        chars = fl.list_characters()
        # FACES, not a wall of names (review panel, new user: "I remember yesterday's
        # character by their face"); characters with no picture yet are left out
        thumbs = st.setdefault("_thumbs", {})
        shown = []
        for slug, name in chars:
            p_ = fl.character_still(slug)
            if not p_ or not os.path.exists(p_):
                continue
            key = (slug, os.path.getmtime(p_))
            if key not in thumbs:
                thumbs[key] = fl.thumb_photo(p_, 46)
            shown.append((slug, name, thumbs[key]))
            if len(shown) >= 42:              # only the faces shown are ever decoded
                break
        for i, (slug, name, img) in enumerate(shown[:42]):   # wraps: 7 a row
            on = slug in st["cast"]
            fr = tk.Frame(cast_box, bg=C["ink"] if on else C["panel2"], cursor="hand2")
            th = tk.Label(fr, bg=fr["bg"], cursor="hand2")
            if img is not None:
                th.configure(image=img)
                th.image = img
            th.pack(padx=3, pady=(3, 0))
            nl = tk.Label(fr, text=(("\u2713 " if on else "") + name)[:14], bg=fr["bg"],
                          fg="#14110f" if on else C["ink"], font=fsm, cursor="hand2")
            nl.pack(padx=3, pady=(0, 3))
            for w_ in (fr, th, nl):
                w_.bind("<Button-1>", lambda _e, s=slug: _toggle_cast(s))
            fr.grid(row=i // 7, column=i % 7, sticky="ew", padx=(0, 4), pady=(0, 4))
        n_new = max(0, fm["cast"] - len(st["cast"]))
        cast_note.configure(text=(
            "optional \u2014 pick someone you've made, or Quick invents %d new character%s"
            % (fm["cast"], "" if fm["cast"] == 1 else "s")) if not st["cast"] else
            ("%s%s" % (", ".join(dict(chars).get(s, s) for s in st["cast"]),
                       (" + Quick invents %d more" % n_new) if n_new else "")))

    def _toggle_cast(slug):
        if slug in st["cast"]:
            st["cast"].remove(slug)
        elif len(st["cast"]) < 3:
            st["cast"].append(slug)
        _paint_all()

    def _len_moved():
        st["secs"] = int(len_var.get())
        _len_label()
        _paint_go()

    def _len_label():
        s_ = int(len_var.get())
        len_lbl.configure(text="%d s \u00b7 ~%d lines" % (s_, max(2, int(round(s_ / 2.5)))))

    def _paint_len():
        for w_ in list(asp_box.winfo_children()):
            w_.destroy()
        fm = formats.get(st["fmt"])
        cur_s = st["secs"] or fm["seconds"]
        if int(len_var.get()) != cur_s:
            len_var.set(cur_s)
        _len_label()
        for w_ in q_box.winfo_children():
            w_.destroy()
        for w_ in n_box.winfo_children():
            w_.destroy()
        for n_ in (1, 2, 3):
            chip(n_box, str(n_), n_ == st["copies"],
                 lambda v=n_: (st.__setitem__("copies", v), _paint_all())).pack(
                side="left", padx=(0, 3))
        for w_ in cap_box.winfo_children():
            w_.destroy()
        if fm.get("kind") == "music":
            cap_row.pack_forget()
        else:
            if not cap_row.winfo_ismapped():
                cap_row.pack(fill="x", padx=0, pady=(10, 0), after=row2)
            for c_, lab_ in CAP_LABELS:
                chip(cap_box, lab_, c_ == st["captions"],
                     lambda v=c_: (st.__setitem__("captions", v), _paint_all())).pack(
                    side="left", padx=(0, 3))
            if st["captions"] in ("clean", "pop", "big"):   # what the picked look looks like
                st["cap_loop"] = previews.CaptionLoop(cap_box, st["captions"], w=150, h=40,
                                                      font_family=_family(fsm))
                st["cap_loop"].cv.pack(side="left", padx=(8, 0))
            else:
                st["cap_loop"] = None
        for w_ in place_box.winfo_children():
            w_.destroy()
        try:
            import places as _pl
            _lib = _pl.library()
        except Exception:
            _lib = []
        if fm.get("kind") == "music":
            place_row.pack_forget()
        else:
            if not place_row.winfo_ismapped():
                place_row.pack(fill="x", padx=16, pady=(10, 0), after=shape_row)
            chip(place_box, "From the idea", not st.get("place"),
                 lambda: (st.__setitem__("place", None), _paint_all())).pack(
                side="left", padx=(0, 3))
            shown = [x for x in _lib[:4]]
            if st.get("place") and st["place"] not in [s_ for s_, _n in shown]:
                shown = [(st["place"], dict(_lib).get(st["place"], st["place"]))] + shown[:3]
            for s_, n_ in shown:
                chip(place_box, n_[:22], st.get("place") == s_,
                     lambda v=s_: (st.__setitem__("place", v), _paint_all())).pack(
                    side="left", padx=(0, 3))
            if ctx.get("open_places"):
                _studio_link(place_box, "  + make a place" if not _lib else "  more places",
                             lambda: ctx["open_places"]()).pack(side="left")
        for w_ in mus_box.winfo_children():
            w_.destroy()
        if fm.get("kind") == "music":
            mus_row.pack_forget()               # a music video IS the song
        else:
            if not mus_row.winfo_manager():
                mus_row.pack(fill="x", padx=0, pady=(10, 0),
                             after=cap_row if cap_row.winfo_manager() else row2)
            try:
                import music as _music
                _songs = _music.library(sync=not st.get("_music_synced"))
                st["_music_synced"] = True
            except Exception:
                _songs = []
            chip(mus_box, "None", not st.get("music"),
                 lambda: (st.__setitem__("music", None), _paint_all())).pack(
                side="left", padx=(0, 3))
            if _songs:
                chip(mus_box, "Pick for me", st.get("music") == "auto",
                     lambda: (st.__setitem__("music", "auto"), _paint_all())).pack(
                    side="left", padx=(0, 3))
            shown_m = list(_songs[:3])
            if st.get("music") and st["music"] != "auto" and \
                    st["music"] not in [s_ for s_, _n in shown_m]:
                shown_m = [(st["music"], dict(_songs).get(st["music"], st["music"]))] + \
                    shown_m[:2]
            for s_, n_ in shown_m:
                chip(mus_box, n_[:20], st.get("music") == s_,
                     lambda v=s_: (st.__setitem__("music", v), _paint_all())).pack(
                    side="left", padx=(0, 3))
            if len(_songs) > 3:
                _studio_link(mus_box, "  more\u2026", lambda: _pick_music(_songs)).pack(
                    side="left")
            _studio_link(mus_box, "  + add a song", lambda: _add_music()).pack(side="left")
        for w_ in pace_box.winfo_children():
            w_.destroy()
        cur_p = st.get("pace") or formats.pace(st["fmt"])
        if fm.get("kind") == "music":
            pace_row.pack_forget()
        else:
            if not pace_row.winfo_ismapped():
                pace_row.pack(fill="x", padx=0, pady=(10, 0), after=cap_row)
            for p_, lab_ in quick.sp.PACE_LABELS:
                chip(pace_box, lab_, p_ == cur_p,
                     lambda v=p_: (st.__setitem__("pace", v), _paint_all())).pack(
                    side="left", padx=(0, 3))
            pace_hint.configure(text=PACE_HINTS.get(cur_p, ""))
        for q_, lab_ in (("Draft", "Quick preview"), ("Final", "Best quality \u00b7 2\u00d7 longer")):
            chip(q_box, lab_, q_ == st["quality"],
                 lambda v=q_: (st.__setitem__("quality", v), _paint_all())).pack(
                side="left", padx=(0, 3))
        cur_a = st["aspect"] or fm["aspect"]
        for a_, lab_ in (("9:16", "Tall \u00b7 TikTok / Reels"), ("16:9", "Wide \u00b7 YouTube")):
            chip(asp_box, lab_, a_ == cur_a,
                 lambda v=a_: (st.__setitem__("aspect", v), _paint_all())).pack(
                side="left", padx=(0, 3))

    def _fmt_time(sec):
        sec = int(sec or 0)
        return ("~%d min" % max(1, round(sec / 60.0))) if sec >= 90 else "~%d s" % sec

    def _paint_go():
        j = _job_like()
        j["aspect"] = j["aspect"] or formats.get(st["fmt"])["aspect"]
        n_ = st.get("copies", 1)
        try:
            n_ *= len(_ideas_typed())
        except NameError:
            pass
        b_go.configure(text=("Make it · %s" % _fmt_time(quick.estimate(j))) if n_ == 1
                       else "Make %d · %s" % (n_, _fmt_time(quick.estimate(j) * n_)),
                       state="disabled" if st["busy"] else "normal")
        wait_lbl.configure(text=(
            "Takes about %s on your computer. You can close this window \u2014 it keeps "
            "going, and you'll get a notification when it's ready%s."
            % (_fmt_time(quick.estimate(j) * n_).lstrip("~"),
               " or needs you" if (setcheck_var.get() or check_var.get() or
                                   pick_var.get()) else "")))
        try:
            n_hint.configure(text="each extra version adds about %s"
                             % _fmt_time(quick.estimate(j)).lstrip("~"))
        except NameError:
            pass

    def _paint_sound():
        fm = formats.get(st["fmt"])
        sg = st.get("song")
        if sg and fm.get("kind") == "music":
            snd_var.set("your song: %s" % sg["title"][:30])
        if fm.get("kind") == "music":
            if not snd_var.get():
                snd_var.set(fm.get("sound") or "")
            snd_row.pack(fill="x", padx=16, pady=(12, 0), before=more_hdr)
        else:
            snd_row.pack_forget()

    def _toggle_more():
        st["more"] = not st.get("more")
        _paint_more()

    def _paint_more():
        """MORE OPTIONS: closed, it says what the settings are in one line."""
        if st.get("more"):
            more.pack(fill="x", padx=16, pady=(4, 0), after=more_hdr)
            more_lbl.configure(text="More options \u25be")
        else:
            more.pack_forget()
            more_lbl.configure(text="More options \u25b8")
        fm = formats.get(st["fmt"])
        bits = ["%d s" % (st["secs"] or fm["seconds"]),
                "draft quality (faster)" if st["quality"] == "Draft" else "best quality",
                "%d version%s" % (st["copies"], "" if st["copies"] == 1 else "s")]
        if fm.get("kind") != "music":
            bits.append("no captions" if st["captions"] == "none" else
                        "%s captions" % st["captions"])
            bits.append(dict(quick.sp.PACE_LABELS).get(st.get("pace") or formats.pace(st["fmt"]),
                                                      "").lower())
        if weird_var.get():
            bits.append("weirdos")
        if st.get("music") and fm.get("kind") != "music":
            bits.append("music")
        if pick_var.get() and fm.get("kind") != "music":
            bits.append("I pick the script")
        if setcheck_var.get() or check_var.get():
            bits.append("I'll check the scene before filming")
        if paid_var.get():
            bits.insert(0, "PAID WORK")
        more_sum.configure(text="  \u00b7  ".join(b for b in bits if b))

    # ------------------------------------------------------------ SERIES (series.py)
    def _paint_series():
        import series as _sr
        for w_ in ser_box.winfo_children():
            w_.destroy()
        lst = _sr.all_series()
        if st.get("series") and not _sr.load(st["series"]):
            st["series"] = None
        chip(ser_box, "None", not st.get("series"), lambda: _pick_series(None)).pack(
            side="left", padx=(0, 3))
        for sr in lst[:6]:
            chip(ser_box, sr["name"][:22], st.get("series") == sr["slug"],
                 lambda s_=sr["slug"]: _pick_series(s_)).pack(side="left", padx=(0, 3))
        _studio_link(ser_box, "  + new series", lambda: _series_form(None)).pack(side="left")
        if st.get("series"):
            _studio_link(ser_box, "   edit", lambda: _series_form(st["series"])).pack(
                side="left")
            sr = _sr.load(st["series"])
            n_ = len([j for j in _sr.episodes(sr) if j.get("state") == "done"])
            ser_note.configure(text="Episode %d of %s — the cast, format and place come "
                                    "from the series; “give me ideas” pitches the "
                                    "next episodes." % (n_ + 1, sr["name"]))
            if not ser_note.winfo_ismapped():
                ser_note.pack(fill="x", padx=16, pady=(4, 0), after=ser_row)
        else:
            ser_note.pack_forget()

    def _pick_series(slug):
        import series as _sr
        st["series"] = slug
        sr = _sr.load(slug) if slug else None
        if sr:
            if sr.get("cast"):
                st["cast"] = list(sr["cast"])
            st["place"] = sr.get("place")
            _pick_fmt(sr.get("format") or st["fmt"], user=True)
        _paint_series()
        _paint_all()

    def _series_form(slug):
        """New / edit a series: the bible. The cast, format and place are what the form on
        the left has picked now."""
        st["view"] = "series"
        st["series_edit"] = slug
        _paint_run()

    def _paint_series_form():
        import series as _sr
        sr = _sr.load(st.get("series_edit")) if st.get("series_edit") else None
        hd = tk.Frame(stage, bg=C["panel"])
        hd.pack(fill="x")
        lab(hd, ("Edit %s" % sr["name"]) if sr else "A new series", fg=C["ink"],
            font=fh).pack(side="left")
        _studio_link(hd, "close", lambda: (st.__setitem__("view", None), _paint_run())
                     ).pack(side="right")
        lab(stage, "The show's bible: every video made in it is written from this. The "
                   "regular cast, format and place are what's picked on the left now.",
            fg=C["faint"], wraplength=380, justify="left").pack(fill="x", pady=(2, 8))
        ents = {}
        for key, title, h_, hint in (
                ("name", "NAME", 1, "e.g. Barnaby's Weather"),
                ("logline", "THE SHOW, IN ONE LINE", 2, "a weatherman who takes the weather "
                                                       "personally"),
                ("tone", "TONE", 1, "deadpan, local-news cheap, warm underneath"),
                ("rules", "ALWAYS TRUE (one per line)", 3, "Barnaby never admits he's wrong"),
                ("gags", "RUNNING GAGS (one per line)", 3, "the green screen is always "
                                                          "slightly the wrong place")):
            lab(stage, title, fg=C["faint"]).pack(fill="x", pady=(6, 1))
            t_ = tk.Text(stage, height=h_, wrap="word", bg="#100e0c", fg=C["ink"],
                         relief="flat", font=fsm, padx=6, pady=4, insertbackground=C["ink"])
            v_ = (sr or {}).get(key)
            t_.insert("1.0", "\n".join(v_) if isinstance(v_, list) else (v_ or ""))
            t_.pack(fill="x")
            lab(stage, hint, fg=C["faint"]).pack(fill="x")
            ents[key] = t_
        st["series_ents"] = ents
        names = [(fl.read_character(c_) or {}).get("name") or c_ for c_ in st["cast"]]
        lab(stage, "Cast: %s · %s%s" % (
            ", ".join(names) or "invented each time", formats.get(st["fmt"])["name"],
            (" · place: %s" % st["place"]) if st.get("place") else ""),
            fg=C["muted"], wraplength=380, justify="left").pack(fill="x", pady=(8, 0))
        button(stage, "Save the series", lambda: _save_series(), primary=True).pack(
            anchor="w", pady=(8, 0))

    def _save_series():
        import series as _sr
        e_ = st.get("series_ents") or {}
        val = {k: t_.get("1.0", "end").strip() for k, t_ in e_.items()}
        try:
            if st.get("series_edit"):
                sr = _sr.update(st["series_edit"], cast=list(st["cast"]), format=st["fmt"],
                                place=st.get("place"), **val)
            else:
                sr = _sr.new(val.pop("name", ""), fmt=st["fmt"], cast=list(st["cast"]),
                             place=st.get("place"), **val)
        except ValueError as exc:
            now_lbl.configure(text=str(exc))
            return None
        st["view"] = None
        _pick_series(sr["slug"])
        now_lbl.configure(text="%s is saved — its videos are written from it" % sr["name"])
        _paint_run()
        return sr

    def _pick_product(path=None):
        if path is None:
            from tkinter import filedialog          # handler-time import (AGENTS.md)
            path = filedialog.askopenfilename(
                parent=win, title="A photo of the product",
                filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.heic"), ("All files", "*.*")])
        if not path:
            return
        st["product"] = path
        if not prod_name_var.get().strip():
            prod_name_var.set(os.path.splitext(os.path.basename(path))[0].replace("_", " ")
                              .replace("-", " ").strip().title()[:40])
        _paint_product()

    def _paint_product():
        if st["fmt"] != "ad":
            prod_row.pack_forget()
            return
        if not prod_row.winfo_ismapped():
            prod_row.pack(fill="x", padx=16, pady=(10, 0), after=fmt_box)
        p_ = st.get("product")
        img = fl.thumb_photo(p_, 44) if p_ and os.path.exists(p_) else None
        prod_thumb.configure(image=img or "", text="" if img else "")
        prod_thumb.image = img
        prod_pick.configure(text=("  " + os.path.basename(p_)[:28] + "  \u00d7 remove")
                            if p_ else "  Choose a photo\u2026")
        prod_pick.unbind("<Button-1>")
        prod_pick.bind("<Button-1>", (lambda _e: (st.__setitem__("product", None),
                                                  _paint_product())) if p_ else
                       (lambda _e: _pick_product()))

    def _paint_all():
        _paint_product()
        _paint_more()
        _paint_sound()
        _paint_fmt()
        _paint_cast()
        _paint_len()
        _paint_go()

    # ================================================================== painting the run
    def _paint_steps():
        j = st["job"]
        # the run polls this twice a second: rebuild the list ONLY when a step changed —
        # tearing it down every 500 ms flickered on the MacBook ("the screen is glitching")
        key = (id(j), tuple(sorted(((j or {}).get("steps") or {}).items())),
               (j or {}).get("format"), st["fmt"], (j or {}).get("state"))
        if st.get("_steps_key") == key and steps_box.winfo_children():
            now_lbl.configure(text=(j or {}).get("now") or "")
            return
        st["_steps_key"] = key
        for w_ in steps_box.winfo_children():
            w_.destroy()
        if (j or {}).get("state") == "done":
            # (test run 2026-10-18: on a laptop screen the 7 ticked steps pushed Watch /
            # Export / Finish below the fold; once it's done one line says it all)
            r = tk.Frame(steps_box, bg=C["panel"])
            r.pack(fill="x", pady=1)
            tk.Label(r, text=ICON.get("done", "\u2713"), bg=C["panel"], fg=C["moss"],
                     font=fh, width=2).pack(side="left")
            tk.Label(r, text="All done", bg=C["panel"], fg=C["ink"], font=fsm,
                     anchor="w").pack(side="left")
            now_lbl.configure(text="")
            return
        for s in quick.STEPS:
            stt = (j or {}).get("steps", {}).get(s, "todo" if s != "looks" else "skip")
            if s == "looks" and stt == "skip":
                continue
            r = tk.Frame(steps_box, bg=C["panel"])
            r.pack(fill="x", pady=1)
            col = {"done": C["moss"], "running": C["ochre"], "review": C["ochre"]}.get(
                stt, C["faint"])
            tk.Label(r, text=ICON.get(stt, "○"), bg=C["panel"], fg=col, font=fh,
                     width=2).pack(side="left")
            tk.Label(r, text=quick.labels(j or {"format": st["fmt"]})[s], bg=C["panel"],
                     fg=C["ink"] if stt in ("running", "review", "done") else C["muted"],
                     font=fh if stt == "running" else fsm, anchor="w").pack(side="left")
        now_lbl.configure(text=(j or {}).get("now") or "")

    def _clear_stage():
        for w_ in stage.winfo_children():
            w_.destroy()
        try:
            stage_cv.yview_moveto(0)
        except tk.TclError:
            pass

    def _cover_box(avail):
        """How big the done panel's cover is: about half the panel's height, 180-400 px
        (unknown height -> 260)."""
        if not avail or avail < 50:
            return 260
        return int(max(180, min(400, avail * 0.5)))

    def _thumb(parent, path, box=96):
        img = fl.thumb_photo(path, box) if path and os.path.exists(path) else None
        l_ = tk.Label(parent, image=img, bg="#100e0c", text="" if img else "—",
                      fg=C["faint"])
        l_.image = img
        return l_

    def _studio_link(parent, text, cmd):
        l_ = tk.Label(parent, text=text, bg=parent["bg"], fg=C["ochre"], font=fsm,
                      cursor="hand2", anchor="w")
        l_.bind("<Button-1>", lambda _e: cmd())
        return l_

    def _paint_history():
        """ALL MY VIDEOS: every finished video, newest first; click one to open it (watch,
        re-cut, re-film shots, make more like it)."""
        hd = tk.Frame(stage, bg=C["panel"])
        hd.pack(fill="x")
        lab(hd, "All my videos", fg=C["ink"], font=fh).pack(side="left")
        _studio_link(hd, "close", lambda: (st.__setitem__("view", None), _paint_run())
                     ).pack(side="right")
        q_ = tk.Entry(stage, bg="#100e0c", fg=C["ink"], relief="flat", font=fsm,
                      insertbackground=C["ink"])
        q_.insert(0, st.get("hist_q") or "")
        q_.pack(fill="x", pady=(6, 2), ipady=3)
        lab(stage, "search by title or idea", fg=C["faint"]).pack(fill="x")
        rows = tk.Frame(stage, bg=C["panel"])
        rows.pack(fill="x", pady=(6, 0))
        st["hist_rows"] = []

        def fill():
            for w_ in rows.winfo_children():
                w_.destroy()
            vids = quick.history(st.get("hist_q") or "")
            st["hist_rows"] = vids
            if not vids:
                lab(rows, "nothing yet" if not st.get("hist_q") else "no match",
                    fg=C["faint"]).pack(fill="x")
            for v in vids[:40]:
                r_ = tk.Frame(rows, bg=C["panel2"], cursor="hand2")
                r_.pack(fill="x", pady=(0, 4))
                _thumb(r_, v["cover"], 64).pack(side="left", padx=6, pady=4)
                inf = tk.Frame(r_, bg=C["panel2"])
                inf.pack(side="left", fill="x", expand=True)
                lab(inf, v["title"][:44], fg=C["ink"]).pack(anchor="w")
                lab(inf, "%s · %s" % (formats.get(v["format"])["name"]
                                      if v["format"] else "",
                                      time.strftime("%d %b, %H:%M", time.localtime(v["when"]))
                                      if v["when"] else ""), fg=C["faint"]).pack(anchor="w")
                for w_ in [r_, inf] + list(inf.winfo_children()):
                    w_.bind("<Button-1>", lambda _e, i_=v["id"]: _open_history(i_))
            if len(vids) > 40:
                lab(rows, "and %d more — search to find them" % (len(vids) - 40),
                    fg=C["faint"]).pack(fill="x")

        def typed(_e=None):
            st["hist_q"] = q_.get()
            if st.get("hist_after"):
                try:
                    win.after_cancel(st["hist_after"])
                except Exception:
                    pass
            st["hist_after"] = win.after(250, fill)
        q_.bind("<KeyRelease>", typed)
        st["hist_entry"], st["hist_typed"] = q_, typed
        fill()

    def _open_history(jid):
        st["view"] = None
        _show_job({"id": jid})

    def _show_history():
        if st["busy"]:
            now_lbl.configure(text="a video is being made — open All my videos when it's done")
            return
        st["view"], st["hist_q"] = "history", ""
        _paint_run()

    def _paint_change(j):
        """CHANGE IT, on a finished video: re-cut it (captions / pace / hook — no new shots)
        or re-film any shot (not just the flagged ones)."""
        lab(stage, "CHANGE IT", fg=C["faint"]).pack(fill="x", pady=(12, 2))
        rc = st.setdefault("recut", {})
        rc.setdefault("captions", j.get("captions", "pop"))
        rc.setdefault("pace", j.get("pace") or formats.pace(j.get("format")))
        rc.setdefault("hook", j.get("hook", True))
        r1 = tk.Frame(stage, bg=C["panel"])
        r1.pack(fill="x")
        lab(r1, "Captions ", fg=C["muted"]).pack(side="left")
        for k_, nm_ in (("clean", "Clean"), ("pop", "Pop"), ("big", "Big"),
                        ("subtitle", "Subtitle"), ("none", "None")):
            chip(r1, nm_, rc["captions"] == k_,
                 lambda k__=k_: (rc.__setitem__("captions", k__), _paint_stage())).pack(
                side="left", padx=(0, 3))
        r2 = tk.Frame(stage, bg=C["panel"])
        r2.pack(fill="x", pady=(4, 0))
        lab(r2, "Pace ", fg=C["muted"]).pack(side="left")
        for k_, nm_ in (("snappy", "Snappy"), ("natural", "Natural"),
                        ("breathe", "Breathe")):
            chip(r2, nm_, rc["pace"] == k_,
                 lambda k__=k_: (rc.__setitem__("pace", k__), _paint_stage())).pack(
                side="left", padx=(0, 3))
        if j.get("hook_text"):
            chip(r2, "Hook text", bool(rc["hook"]),
                 lambda: (rc.__setitem__("hook", not rc["hook"]), _paint_stage())).pack(
                side="left", padx=(8, 0))
        changed = (rc["captions"] != j.get("captions", "pop") or
                   rc["pace"] != (j.get("pace") or formats.pace(j.get("format"))) or
                   bool(rc["hook"]) != bool(j.get("hook", True)))
        b_rc = button(stage, "Re-cut it · about a minute, no new shots",
                      lambda: _recut(j))
        b_rc.pack(anchor="w", pady=(6, 0))
        if j.get("old_cuts"):
            _studio_link(stage, "\u21b6 Back to the previous version",
                         lambda: _undo_recut(j)).pack(fill="x", pady=(4, 0))
        if not changed:
            b_rc.configure(state="disabled")
        st["recut_btn"] = b_rc
        # FINISH: a last pass on this Mac (finish.py) — sharper / grain / smooth motion
        import finish as _fin
        fr_ = tk.Frame(stage, bg=C["panel"])
        fr_.pack(fill="x", pady=(10, 0))
        lab(fr_, "Finish ", fg=C["muted"]).pack(side="left")
        fl_ = st.setdefault("finish_looks", ["sharper"])
        for k_, nm_ in _fin.LOOKS:
            chip(fr_, nm_, k_ in fl_,
                 lambda k__=k_: (fl_.remove(k__) if k__ in fl_ else fl_.append(k__),
                                 _paint_stage())).pack(side="left", padx=(0, 3))
        b_fin = button(stage, "Finishing\u2026" if st.get("finishing") else
                       "Make a finished copy", lambda: _finish(j))
        b_fin.pack(anchor="w", pady=(6, 0))
        if st.get("finishing") or not fl_:
            b_fin.configure(state="disabled")
        st["finish_btn"] = b_fin
        if j.get("finished") and os.path.exists(j["finished"]):
            _studio_link(stage, "\u25b6 Watch the finished copy (exports use it)",
                         lambda: _open_file(j["finished"])).pack(fill="x", pady=(2, 0))
        open_ = st.get("show_shots")
        _studio_link(stage, ("▾ " if open_ else "▸ ") + "Re-film a shot…",
                     lambda: (st.__setitem__("show_shots", not open_), _paint_stage())
                     ).pack(fill="x", pady=(8, 0))
        st["shot_rows"] = []
        if open_:
            lab(stage, "Pick the shot that's wrong. It gets a new take and the video is cut "
                       "again; the old take stays in Takes.", fg=C["faint"],
                wraplength=380, justify="left").pack(fill="x")
            for n_, who_, line_, frame_ in quick.shot_list(j):
                fr_ = tk.Frame(stage, bg=C["panel2"])
                fr_.pack(fill="x", pady=(3, 0))
                _thumb(fr_, frame_, 56).pack(side="left", padx=(6, 6), pady=3)
                inf = tk.Frame(fr_, bg=C["panel2"])
                inf.pack(side="left", fill="x", expand=True)
                lab(inf, "Shot %d%s" % (n_, " · " + who_ if who_ else ""),
                    fg=C["ink"]).pack(anchor="w")
                lab(inf, (line_ or "")[:70], fg=C["muted"], wraplength=230,
                    justify="left").pack(anchor="w")
                b_ = button(fr_, "Redo", lambda n__=n_: _reroll([n__]))
                b_.pack(side="right", padx=6)
                st["shot_rows"].append((n_, b_))

    def _finish(j):
        if st.get("finishing") or not j.get("final"):
            return
        import finish as _fin
        looks = list(st.get("finish_looks") or [])
        st["finishing"] = True
        res = {}

        def work():
            try:
                res["out"] = _fin.finish(j["final"], looks=looks,
                                         progress=lambda m: res.__setitem__("now", m))
            except Exception as exc:
                res["err"] = "%s" % exc
            res["done"] = True

        def poll():
            if not res.get("done"):
                if res.get("now"):
                    now_lbl.configure(text=res["now"])
                win.after(400, poll)
                return
            st["finishing"] = False
            if res.get("err"):
                now_lbl.configure(text="finishing stopped: %s" % res["err"][:140])
            else:
                j["finished"], j["finished_looks"] = res["out"], looks
                quick.save(j)
                now_lbl.configure(text="the finished copy is ready \u2014 \u25b6 Watch it")
            if (st["job"] or {}).get("id") == j["id"]:
                _paint_stage()
        threading.Thread(target=work, daemon=True).start()
        _paint_stage()
        win.after(400, poll)

    def _rate(j, v):
        quick.rate(j, 0 if (j.get("rating") or 0) == v else v)   # click the same star: clear
        _paint_stage()

    def _undo_recut(j):
        if st["busy"]:
            return
        try:
            quick.undo_recut(j)
        except ValueError as exc:
            now_lbl.configure(text=str(exc))
            return
        st["recut"] = {}
        now_lbl.configure(text="back to the previous version \u2014 \u25b6 Watch it")
        _paint_stage()

    def _shortcut_go(_e=None):
        """Cmd+Return (Ctrl+Return off a Mac): Make it on the form; Keep going on a review."""
        j = st["job"]
        if st["busy"]:
            return "break"
        if j and j.get("state") == "review:drafts":
            return "break"                       # a pick is a click on the one you want
        if j and (j.get("state") or "").startswith("review"):
            _continue()
        else:
            _go()
        return "break"

    def _recut(j):
        if st["busy"]:
            return
        rc = st.get("recut") or {}
        quick.recut(j, captions=rc.get("captions"), pace=rc.get("pace"),
                    hook=rc.get("hook"))
        st["recut"] = {}
        _run(go_on=False)

    def _paint_drafts(j, big):
        """YOU pick the script: the drafts side by side (in a row on the wide review,
        stacked in the narrow column). Nothing has been filmed yet."""
        ds = j.get("drafts") or []
        lab(stage, "Pick the script", fg=C["ink"], font=fh).pack(fill="x")
        lab(stage, "Nothing's been filmed yet. Pick the one that makes sense and makes you "
                   "laugh — you can still change words after.",
            fg=C["faint"], wraplength=900 if big else 380, justify="left").pack(
            fill="x", pady=(0, 8))
        row = tk.Frame(stage, bg=C["panel"])
        row.pack(fill="x")
        st["draft_btns"] = []
        cols = len(ds) if big and len(ds) <= 3 else 1
        for k, d in enumerate(ds):
            card = tk.Frame(row, bg=C["panel2"], highlightthickness=1,
                            highlightbackground=C["line"])
            card.grid(row=k // cols, column=k % cols, sticky="nsew", padx=(0, 8), pady=(0, 8))
            lab(card, "%d · %s" % (k + 1, d.get("title") or "Untitled"), fg=C["ink"],
                font=fh).pack(fill="x", padx=10, pady=(8, 4))
            wl = 330 if cols > 1 else 360
            for li in d.get("lines") or []:
                who = (li.get("speaker") or "").strip().title()
                lab(card, "%s: %s" % (who, li.get("text") or ""), fg=C["ink"],
                    wraplength=wl, justify="left").pack(fill="x", padx=10, pady=(2, 0))
                dr = (li.get("direction") or "").strip()
                if dr and dr.lower() != "beat":
                    lab(card, "(%s)" % dr, fg=C["faint"], wraplength=wl,
                        justify="left").pack(fill="x", padx=18)
            b_ = button(card, "Use this one", lambda k_=k: _pick_draft(k_))
            b_.pack(anchor="w", padx=10, pady=(8, 10))
            st["draft_btns"].append(b_)
        for c_ in range(cols):
            row.grid_columnconfigure(c_, weight=1, uniform="drafts")
        br = tk.Frame(stage, bg=C["panel"])
        br.pack(fill="x", pady=(4, 0))
        _studio_link(br, "None of these — write three more", lambda: _more_drafts()).pack(
            side="left")
        g_ = (j.get("room") or {}).get("game")
        if g_:
            lab(stage, "The idea behind them: %s" % g_, fg=C["faint"],
                wraplength=900 if big else 380, justify="left").pack(fill="x", pady=(10, 0))

    def _pick_draft(k):
        j = st["job"]
        if not j or st["busy"]:
            return
        try:
            quick.pick_draft(j, k)
        except ValueError as exc:
            now_lbl.configure(text="%s" % exc)
            return
        _run(go_on=False)

    def _more_drafts():
        j = st["job"]
        if not j or st["busy"]:
            return
        quick.more_drafts(j)
        _run(go_on=False)

    def _paint_stage():
        _clear_stage()
        if st.get("view") == "history":
            _paint_history()
            return
        if st.get("view") == "night":
            _paint_night()
            return
        if st.get("view") == "series":
            _paint_series_form()
            return
        j = st["job"]
        if not j:
            lab(stage, "Pick a format, type an idea, press Make it. You can close this "
                       "window — the run keeps going and shows up under Recent.",
                fg=C["faint"], wraplength=370, justify="left").pack(fill="x")
            return
        state = j.get("state") or ""
        big = bool(st.get("wide"))
        if big:
            _studio_link(stage, "\u2190 show the form", _show_form).pack(anchor="w",
                                                                         pady=(0, 6))
        if state == "review:voices":
            lab(stage, "Meet the cast", fg=C["ink"], font=fh).pack(fill="x")
            lab(stage, "Not right? Change their look or voice, then press Keep going.",
                fg=C["faint"]).pack(
                fill="x", pady=(0, 6))
            for slug in j.get("cast") or []:
                meta = fl.read_character(slug) or {}
                cr = tk.Frame(stage, bg=C["panel2"])
                cr.pack(fill="x", pady=(0, 6))
                _thumb(cr, fl.character_still(slug), 80).pack(side="left", padx=6, pady=6)
                info = tk.Frame(cr, bg=C["panel2"])
                info.pack(side="left", fill="x", expand=True)
                lab(info, meta.get("name") or slug, fg=C["ink"], font=fh).pack(fill="x")
                lab(info, (meta.get("feature") or "")[:70], wraplength=230,
                    justify="left").pack(fill="x")
                lr = tk.Frame(info, bg=C["panel2"])
                lr.pack(fill="x", pady=(2, 0))
                _studio_link(lr, "Hear them", lambda s=slug: _play_voice(s)).pack(
                    side="left")
                _studio_link(lr, "   Change look", lambda s=slug: ctx["open_character"](s)
                             ).pack(side="left")
                _studio_link(lr, "   Change voice", lambda s=slug: ctx["open_voices"](s)).pack(
                    side="left")
            button(stage, "Looks good — keep going →", lambda: _continue(),
                   primary=True).pack(anchor="w", pady=(6, 0))
        elif state == "review:episode":
            lab(stage, "Does this look like them?", fg=C["ink"], font=fh).pack(fill="x")
            lab(stage, "Every shot starts from these pictures. Compare each close-up with "
                       "their own picture on the left.", fg=C["faint"], wraplength=400,
                justify="left").pack(fill="x", pady=(0, 6))
            br = tk.Frame(stage, bg=C["panel"])          # buttons FIRST: always on screen
            br.pack(fill="x", pady=(0, 8))
            button(br, "Looks right \u2014 shoot it \u2192", lambda: _continue(),
                   primary=True).pack(side="left")
            button(br, "Rebuild the whole set", lambda: _redo_set("master")).pack(
                side="left", padx=6)
            ep_ = quick.sp.load_project(j["episode"]) or {}
            set_ = ep_.get("set") or {}
            if set_.get("where"):          # so a wrong place is caught BEFORE filming
                lab(stage, "Set: %s" % set_["where"], fg=C["muted"], wraplength=400,
                    justify="left").pack(fill="x", pady=(0, 4))
            _thumb(stage, set_.get("master"), 560 if big else 300).pack(fill="x")
            for slug, single in (set_.get("singles") or {}).items():
                nm = (fl.read_character(slug) or {}).get("name") or slug
                cr = tk.Frame(stage, bg=C["panel2"])
                cr.pack(fill="x", pady=(6, 0))
                _thumb(cr, fl.character_still(slug), 140 if big else 80).pack(
                    side="left", padx=(6, 2), pady=4)
                _thumb(cr, single, 240 if big else 120).pack(side="left", padx=2, pady=4)
                inf = tk.Frame(cr, bg=C["panel2"])
                inf.pack(side="left", fill="x", padx=6)
                lab(inf, nm, fg=C["ink"]).pack(anchor="w")
                _studio_link(inf, "redo %s's close-up" % nm,
                             lambda s_=slug: _redo_set(s_)).pack(anchor="w")
        elif state == "review:drafts":
            _paint_drafts(j, big)
        elif state == "review:script":
            lab(stage, "Read the lyrics" if formats.kind(j["format"]) == "music" else
                "Read the script", fg=C["ink"], font=fh).pack(fill="x")
            lab(stage, "Change any words here — NAME: line, one per line.",
                fg=C["faint"]).pack(fill="x", pady=(0, 6))
            t = tk.Text(stage, height=24 if big else 12, wrap="word", bg="#100e0c",
                        fg=C["ink"],
                        relief="flat", font=fsm, padx=8, pady=6, insertbackground=C["ink"])
            t.insert("1.0", quick.script_text(j))
            t.pack(fill="both", expand=True)
            st["script_box"] = t
            br = tk.Frame(stage, bg=C["panel"])
            br.pack(fill="x", pady=(6, 0))
            button(br, "Shoot it →", lambda: _continue(), primary=True).pack(side="left")
            _studio_link(br, "   open in Dialogue",
                         lambda: ctx["open_dialogue"](j["dialogue"])).pack(side="left")
        elif state == "done":
            lab(stage, j.get("title") or "Done", fg=C["ink"], font=fh).pack(fill="x")
            # HOW WAS IT? 1-5 stars; the writer reads them as taste notes next time
            rt = tk.Frame(stage, bg=C["panel"])
            rt.pack(fill="x", pady=(2, 0))
            lab(rt, "How was it?  ", fg=C["faint"]).pack(side="left")
            st["star_lbls"] = []
            for k_ in range(1, 6):
                on_ = (j.get("rating") or 0) >= k_
                sl = tk.Label(rt, text="\u2605" if on_ else "\u2606", bg=C["panel"],
                              fg=C["ochre"] if on_ else C["faint"], font=fh, cursor="hand2")
                sl.pack(side="left")
                sl.bind("<Button-1>", lambda _e, v=k_: _rate(j, v))
                st["star_lbls"].append(sl)
            if j.get("rating"):
                lab(rt, "  the writer learns from this", fg=C["faint"]).pack(side="left")
            ep_cover = os.path.join(os.path.dirname(j.get("final") or ""), "cover.png")
            # the buttons come BEFORE the cover and the cover fits the window: on a laptop
            # screen a 400 px cover left every action below the fold (test run 2026-10-18)
            br = tk.Frame(stage, bg=C["panel"])
            br.pack(fill="x", pady=(6, 0))
            button(br, "▶ Watch it", lambda: _open_file(j["final"]),
                   primary=True).pack(side="left")
            button(br, "Show in Finder", lambda: _reveal(j["final"])).pack(side="left",
                                                                            padx=6)
            b_ex = button(br, "Export for posting" if not st.get("exporting")
                          else "Exporting\u2026", lambda: _export(j))
            b_ex.pack(side="left")
            st["export_btn"] = b_ex
            if st.get("exporting"):
                b_ex.configure(state="disabled")
            elif (st.get("export_done") or {}).get("job") == j["id"]:
                _ed = st["export_done"]
                lab(stage, "Exported: %s%s" % (", ".join(_ed["shapes"]) or "nothing",
                                                ("  (not made: %s)" % ", ".join(_ed["errors"]))
                                                if _ed["errors"] else ""),
                    fg=C["moss"] if not _ed["errors"] else C["ochre"], wraplength=400,
                    justify="left").pack(fill="x", pady=(4, 0))
                _studio_link(stage, "open the export folder",
                             lambda d_=_ed["dir"]: _open_file(d_)).pack(fill="x")
            try:
                _avail = stage_cv.winfo_height()
            except tk.TclError:
                _avail = 0
            _cov = _cover_box(_avail)
            _thumb(stage, ep_cover, _cov).pack(fill="x", pady=(6, 2))
            lr = tk.Frame(stage, bg=C["panel"])
            lr.pack(fill="x", pady=(8, 0))
            lab(lr, "fine-tune in Studio:").pack(side="left")
            _studio_link(lr, "  Episode", lambda: ctx["open_episode"](j["episode"])).pack(
                side="left")
            if j.get("dialogue"):
                _studio_link(lr, "  Dialogue", lambda: ctx["open_dialogue"](j["dialogue"])
                             ).pack(side="left")
            flg = quick.flagged(j)
            st["flag_rows"] = []
            if flg:
                lab(stage, "%s MIGHT LOOK OFF" % ("1 SHOT" if len(flg) == 1 else
                                                  "%d SHOTS" % len(flg)),
                    fg=C["ochre"]).pack(fill="x", pady=(12, 0))
                lab(stage, "Watch the video first, then redo only the ones you agree with "
                           "(the old version is kept).",
                    fg=C["faint"], wraplength=380, justify="left").pack(fill="x")
                for n_, why_, frame_ in flg:
                    fr_ = tk.Frame(stage, bg=C["panel2"])
                    fr_.pack(fill="x", pady=(4, 0))
                    _thumb(fr_, frame_, 72).pack(side="left", padx=(6, 6), pady=4)
                    inf = tk.Frame(fr_, bg=C["panel2"])
                    inf.pack(side="left", fill="x", expand=True)
                    lab(inf, "Shot %d" % n_, fg=C["ink"]).pack(anchor="w")
                    lab(inf, "; ".join(why_), fg=C["muted"], wraplength=240,
                        justify="left").pack(anchor="w")
                    bx_ = tk.Frame(inf, bg=C["panel2"])
                    bx_.pack(anchor="w", pady=(2, 4))
                    b_r = button(bx_, "Redo this shot", lambda n__=n_: _reroll([n__]))
                    b_r.pack(side="left")
                    _studio_link(bx_, "   keep it", lambda n__=n_: _ok_shot(n__)).pack(
                        side="left")
                    st["flag_rows"].append((n_, b_r))
                if len(flg) > 1:
                    button(stage, "Redo all %d" % len(flg),
                           lambda ns_=[f_[0] for f_ in flg]: _reroll(ns_)).pack(
                        anchor="w", pady=(6, 0))
            if j.get("episode"):
                _paint_change(j)
            post = quick.post_text(j)
            if post:
                lab(stage, "READY TO POST", fg=C["faint"]).pack(fill="x", pady=(10, 0))
                lab(stage, "Turn on the app's \u201cAI-generated\u201d label when you post "
                           "(TikTok, Reels and Shorts ask for it).", fg=C["faint"],
                    wraplength=380, justify="left").pack(fill="x")
                pt = tk.Text(stage, height=5, wrap="word", bg="#100e0c", fg=C["ink"],
                             relief="flat", font=fsm, padx=6, pady=4)
                pt.insert("1.0", post)
                pt.pack(fill="x")

                def _copy(text=post):
                    win.clipboard_clear()
                    win.clipboard_append(text)
                    now_lbl.configure(text="caption copied \u2014 paste it with the video")
                button(stage, "Copy the caption", _copy).pack(anchor="w", pady=(4, 0))
            if j.get("cast"):
                lab(stage, "MORE LIKE THIS", fg=C["faint"]).pack(fill="x", pady=(12, 2))
                mr = tk.Frame(stage, bg=C["panel"])
                mr.pack(fill="x")
                button(mr, "Same characters, new idea", lambda: _again_idea(j)).pack(
                    side="left")
                button(mr, "Another song" if formats.kind(j.get("format")) == "music"
                       else "Another script", lambda: _again(j, "script")).pack(
                    side="left", padx=6)
                if j.get("quality") != "Final" and j.get("episode"):
                    fr_ = tk.Frame(stage, bg=C["panel"])
                    fr_.pack(fill="x", pady=(6, 0))
                    _fe = _fmt_time(quick.estimate(dict(
                        j, quality="Final", steps={s_: ("todo" if s_ in ("shots", "cut")
                                                        else "done") for s_ in quick.STEPS})))
                    button(fr_, "Re-shoot in Final \u00b7 %s" % _fe,
                           lambda: _again(j, "final")).pack(side="left")
                    lab(fr_, "  same script, voices, set", fg=C["faint"]).pack(side="left")
            if j.get("aspect") == "16:9" and ctx.get("open_app"):
                _studio_link(stage, "Make a vertical version for TikTok / Reels \u2192",
                             lambda: ctx["open_app"]("vertical", video=j["final"])).pack(
                    fill="x", pady=(8, 0))
        elif state == "error":
            lab(stage, "It stopped", fg=C["ochre"], font=fh).pack(fill="x")
            lab(stage, _explain(j.get("error") or ""), wraplength=400, justify="left").pack(
                fill="x", pady=(2, 8))
            button(stage, "Try again from there", lambda: _continue(go_on=False),
                   primary=True).pack(anchor="w")
        elif state == "stopped":
            lab(stage, "Stopped — everything made so far is kept.", fg=C["muted"]).pack(
                fill="x")
            button(stage, "Carry on", lambda: _continue(go_on=False), primary=True).pack(
                anchor="w", pady=(6, 0))
        elif st["busy"]:
            b_ = button(stage, "Pause after this step", lambda: st.__setitem__("stop", True))
            b_.pack(anchor="w")

    def _paint_tonight_link(parent):
        import nightshift as _ns
        n_ = len(_ns.items())
        if n_ or _ns.last_night() or st.get("night_running"):
            _studio_link(parent, ("Tonight (%d)   " % n_) if n_ else "Tonight   ",
                         _show_night).pack(side="right")

    def _paint_recent():
        for w_ in recent.winfo_children():
            w_.destroy()
        jobs = [j for j in quick.list_jobs() if not st["job"] or j["id"] != st["job"]["id"]]
        rh = tk.Frame(recent, bg=C["panel"])
        rh.pack(fill="x", pady=(8, 2))
        _paint_tonight_link(rh)                 # the night list shows even with no videos
        if not jobs:
            return
        lab(rh, "RECENT", fg=C["faint"]).pack(side="left")
        n_done = sum(1 for j in jobs if j.get("state") == "done") + (
            1 if st["job"] and st["job"].get("state") == "done" else 0)
        if n_done:
            _studio_link(rh, "All my videos (%d) →" % n_done, _show_history).pack(
                side="right")
        for j in jobs[:4]:
            txt = "%s · %s" % ((j.get("title") or j.get("idea") or "untitled")[:34],
                                    {"done": "done", "error": "stopped (error)",
                                     "running": "stopped — click to carry on",
                                     "stopped": "stopped — click to carry on"}.get(
                                        j.get("state"), (j.get("state") or "").replace(
                                            "review:", "waiting for you: ")))
            _studio_link(recent, txt, lambda jj=j: _show_job(jj)).pack(fill="x")

    def _layout():
        """A review (meet the cast / read the script / check the set) gets the WHOLE window:
        the form on the left is folded away until the run moves on (round-2 review, new user:
        "the review steps are squeezed into the right column"). "show the form" brings it
        back for a moment."""
        j = st["job"]
        wide = bool(j) and (j.get("state") or "").startswith("review") and \
            not st["busy"] and st.get("view") not in ("history", "night", "series") and \
            not st.get("form_open")
        if wide == bool(st.get("wide")):
            return
        st["wide"] = wide
        if wide:
            left_outer.pack_forget()
            right.pack_configure(side="left", fill="both", expand=True)
            recent.pack_forget()         # (test run: RECENT left the set pictures 70 px tall)
        else:
            right.pack_configure(side="right", fill="y", expand=False)
            left_outer.pack(side="left", fill="both", expand=True, padx=(0, 14))
            recent.pack(fill="x", side="bottom", padx=16, pady=(0, 12), before=stage_cv)

    def _show_form():
        st["form_open"] = True
        _paint_run()

    def _paint_run():
        _layout()
        _paint_steps()
        _paint_stage()
        _paint_recent()
        _paint_go()

    def _explain(err):
        try:
            import cards as _c
            return _c.explain(err)
        except Exception:
            return err[:240]

    # ================================================================== actions
    def _guess():
        """(format key, seconds) the typed idea names, when they differ from the form."""
        text = t_idea.get("1.0", "end").strip()
        g = quick.guess_format(text)
        if g == st["fmt"] or (st.get("fmt_picked") and g):
            g = None
        sec = quick.guess_seconds(text)
        if sec and st.get("secs") == sec:
            sec = None
        return g, sec

    def _paint_guess():
        g, sec = _guess()
        bits = []
        if g:
            bits.append("a %s" % formats.get(g)["name"])
        if sec:
            bits.append("%d s" % sec)
        if bits:
            guess_lbl.configure(text="Sounds like %s \u2014 use %s \u2192" % (
                " \u00b7 ".join(bits), "it" if len(bits) == 1 else "them"))
            if not guess_lbl.winfo_manager():
                guess_lbl.pack(fill="x", padx=16, pady=(4, 0), after=ideas_box)
        else:
            guess_lbl.pack_forget()

    def _take_guess(silent=False):
        g, sec = _guess()
        if g:
            _pick_fmt(g)
        if sec:
            st["secs"] = sec
        if g or sec:
            _paint_all()
            if not silent:
                now_lbl.configure(text="using %s" % ", ".join(
                    x for x in ((formats.get(g)["name"] if g else ""),
                                ("%d s" % sec if sec else "")) if x))
        _paint_guess()
        return g, sec

    def _go():
        if st["busy"]:
            return
        if not st.get("fmt_picked"):
            g_, s_ = _take_guess(silent=True)  # the idea's own format / length
            if g_ or s_:
                now_lbl.configure(text="made it a %s, as the idea says" % (
                    formats.get(st["fmt"])["name"]))
        n_cp = max(1, int(st.get("copies", 1)))
        kw = _job_kwargs()
        # a real product's ad, several versions: each opens with a different kind of hook
        angles = quick.HOOK_ANGLES if (kw.get("product") or kw.get("product_name")) and \
            n_cp > 1 else (None,)
        jobs = [quick.new_job(idea, **dict(kw, hook_angle=angles[_k % len(angles)]))
                for idea in _ideas_typed()
                for _k in range(n_cp)]
        st["job"] = jobs[0]
        st["queue"] = jobs[1:]
        _run(go_on=False)

    def _job_kwargs():
        """Everything the form says, as quick.new_job's options (Make it and the night list
        use the same ones)."""
        sg = st.get("song") if st["fmt"] == "music" else None
        return dict(fmt=st["fmt"], seconds=st["secs"], aspect=st["aspect"],
                    cast=st["cast"], check=check_var.get(), looks=looks_var.get(),
                    sound=snd_var.get() or None, quality=st["quality"],
                    song=(sg or {}).get("path"), lyrics=(sg or {}).get("lyrics"),
                    title=(sg or {}).get("title"), captions=st["captions"],
                    check_set=setcheck_var.get(), pick_draft=pick_var.get(),
                    pace=st.get("pace") or formats.pace(st["fmt"]),
                    place=st.get("place"), hook=hook_var.get(),
                    reactions=react_var.get(), long_takes=long_var.get(),
                    music=st.get("music"),
                    weirdos=weird_var.get(), series=st.get("series"),
                    **({"product": st.get("product"),
                        "product_name": prod_name_var.get().strip(),
                        "product_what": prod_what_var.get().strip()}
                       if st["fmt"] == "ad" else {}))

    # ------------------------------------------------------------ NIGHT SHIFT
    def _add_tonight():
        """The idea box (one video per line when ticked; copies count) -> tonight's list,
        with the form's options. Made unattended by the night shift."""
        import nightshift as _ns
        if not st.get("fmt_picked"):
            _take_guess(silent=True)
        ideas = [i for i in _ideas_typed()
                 for _k in range(max(1, int(st.get("copies", 1))))]
        _ns.add(ideas, _job_kwargs())
        n_ = len(_ns.items())
        now_lbl.configure(text="added %d to tonight's list (%d waiting)" % (len(ideas), n_))
        _paint_recent()

    def _show_night():
        if st["busy"] and not st.get("night_running"):
            now_lbl.configure(text="a video is being made — open Tonight when it's done")
            return
        st["view"] = "night"
        _paint_run()

    def _paint_night():
        import nightshift as _ns
        hd = tk.Frame(stage, bg=C["panel"])
        hd.pack(fill="x")
        lab(hd, "Tonight", fg=C["ink"], font=fh).pack(side="left")
        _studio_link(hd, "close", lambda: (st.__setitem__("view", None), _paint_run())
                     ).pack(side="right")
        lab(stage, "Quick makes these one after another while you sleep — no stops to "
                   "check each step. Shots that might look off are marked in the morning; "
                   "nothing is redone without you. Leave the app open; the Mac stays awake "
                   "while it works.", fg=C["faint"], wraplength=380,
            justify="left").pack(fill="x", pady=(2, 8))
        q = _ns.items()
        st["night_rows"] = []
        if not q:
            lab(stage, "The list is empty. Type an idea and press “+ add to tonight's "
                       "list”.", fg=C["muted"], wraplength=380,
                justify="left").pack(fill="x")
        for it in q:
            r_ = tk.Frame(stage, bg=C["panel2"])
            r_.pack(fill="x", pady=(0, 3))
            o_ = it.get("options") or {}
            lab(r_, it["idea"][:48] or "(a surprise)", fg=C["ink"]).pack(
                side="left", padx=8, pady=4)
            lab(r_, "%s · %s" % (formats.get(o_.get("fmt") or "sketch")["name"],
                                      o_.get("quality") or "Draft"),
                fg=C["faint"]).pack(side="left")
            if not st.get("night_running"):
                _studio_link(r_, "  ×  ", lambda i_=it["id"]: (
                    _ns.remove(i_), _paint_stage(), _paint_recent())).pack(side="right")
            st["night_rows"].append(it["id"])
        if st.get("night_running"):
            lab(stage, st.get("night_now") or "working…", fg=C["ochre"],
                wraplength=380, justify="left").pack(fill="x", pady=(8, 0))
            button(stage, "Stop after this video",
                   lambda: st.__setitem__("stop", True)).pack(anchor="w", pady=(6, 0))
        elif q:
            row = tk.Frame(stage, bg=C["panel"])
            row.pack(fill="x", pady=(10, 0))
            button(row, "Start now", lambda: _night_start(), primary=True).pack(side="left")
            lab(row, "   or at ", fg=C["muted"]).pack(side="left")
            if st.get("night_at_var") is None:
                st["night_at_var"] = tk.StringVar(value="01:00")
            tk.Entry(row, textvariable=st["night_at_var"], width=6, bg="#100e0c",
                     fg=C["ink"], relief="flat", font=fsm,
                     insertbackground=C["ink"]).pack(side="left")
            button(row, "Start then", lambda: _night_at(st["night_at_var"].get())).pack(
                side="left", padx=(6, 0))
            if st.get("night_wait"):
                lab(stage, "starts at %s — leave the app open" % st["night_wait"],
                    fg=C["ochre"]).pack(fill="x", pady=(6, 0))
                _studio_link(stage, "cancel the start time",
                             lambda: (st.__setitem__("night_wait", None),
                                      _paint_stage())).pack(fill="x")
        ln = _ns.last_night()
        st["night_sheet"] = None
        if ln:
            made = [v for v in ln["videos"] if v.get("state") == "done"]
            lab(stage, "LAST NIGHT", fg=C["faint"]).pack(fill="x", pady=(14, 2))
            lab(stage, "%d made, %d stopped — %s" % (
                len(made), len(ln["videos"]) - len(made),
                time.strftime("%a %H:%M", time.localtime(ln.get("started") or 0))),
                fg=C["muted"]).pack(fill="x")
            st["night_sheet"] = os.path.join(ln["dir"], "index.html")
            _studio_link(stage, "Open the contact sheet (every video, plays in your browser)",
                         lambda p_=st["night_sheet"]: _open_file(p_)).pack(fill="x",
                                                                           pady=(2, 4))
            for v in ln["videos"][:12]:
                r_ = tk.Frame(stage, bg=C["panel2"], cursor="hand2")
                r_.pack(fill="x", pady=(0, 3))
                _thumb(r_, v.get("cover"), 48).pack(side="left", padx=6, pady=3)
                inf = tk.Frame(r_, bg=C["panel2"])
                inf.pack(side="left", fill="x", expand=True)
                lab(inf, (v.get("title") or "")[:44], fg=C["ink"]).pack(anchor="w")
                if v.get("state") != "done":
                    note = "stopped: %s" % (v.get("error") or "")[:60]
                elif v.get("flags"):
                    note = "%d shot%s might look off" % (v["flags"],
                                                         "" if v["flags"] == 1 else "s")
                else:
                    note = "looks fine"
                lab(inf, note, fg=C["ochre"] if v.get("state") != "done" or v.get("flags")
                    else C["faint"]).pack(anchor="w")
                if v.get("job"):
                    for w_ in [r_, inf] + list(inf.winfo_children()):
                        w_.bind("<Button-1>", lambda _e, i_=v["job"]: _open_history(i_))

    def _night_at(hhmm):
        try:
            h_, m_ = (int(x) for x in hhmm.strip().split(":"))
            assert 0 <= h_ < 24 and 0 <= m_ < 60
        except Exception:
            now_lbl.configure(text="type the time like 01:00")
            return False
        st["night_wait"] = want = "%02d:%02d" % (h_, m_)
        _paint_stage()

        def tick():
            if st.get("night_wait") != want:
                return                                  # cancelled or changed
            if time.strftime("%H:%M") == want and not st["busy"]:
                st["night_wait"] = None
                _night_start()
                return
            win.after(20000, tick)
        win.after(20000, tick)
        return True

    def _night_start():
        import nightshift as _ns
        if st["busy"] or not _ns.items():
            return
        st["busy"] = st["night_running"] = True
        st["stop"] = False
        st["night_now"] = "starting…"
        res = {}

        def work():
            try:
                res["night"] = _ns.run(director=ctx.get("director"),
                                       progress=lambda m: st.__setitem__("night_now", m),
                                       stop=lambda: st["stop"])
            except Exception as exc:
                res["err"] = "%s" % exc
            res["done"] = True

        def poll():
            if not res.get("done"):
                if st.get("view") == "night":
                    _paint_stage()
                win.after(3000, poll)
                return
            st["busy"] = st["night_running"] = False
            now_lbl.configure(text=("night shift stopped: %s" % res["err"][:120])
                              if res.get("err") else "night shift done — see Tonight")
            try:
                import cards as _c
                threading.Thread(target=_c.release, args=("quick",), daemon=True).start()
            except Exception:
                pass
            _paint_run()
        threading.Thread(target=work, daemon=True).start()
        st["view"] = "night"
        _paint_run()
        win.after(3000, poll)

    def _ideas_typed():
        """The idea box -> the list of ideas to make: every line on its own when "one video per
        line" is ticked, else the whole box as one idea."""
        text = t_idea.get("1.0", "end").strip()
        lines = [ln.strip(" -\u2022\t") for ln in text.splitlines() if ln.strip(" -\u2022\t")]
        if perline_var.get() and len(lines) > 1:
            return lines
        return [text]

    def _paint_perline(_e=None):
        _paint_guess()
        n_ = len([ln for ln in t_idea.get("1.0", "end").splitlines() if ln.strip()])
        if n_ > 1:
            perline_cb.configure(text="make one video per line (%d videos)" % n_)
            if not perline_cb.winfo_ismapped():
                perline_cb.pack(fill="x", padx=12, before=ideas_box)
        else:
            perline_var.set(False)
            perline_cb.pack_forget()
        _paint_go()

    t_idea.bind("<KeyRelease>", _paint_perline)
    for _seq in ("<Command-Return>", "<Control-Return>"):
        try:
            win.bind(_seq, _shortcut_go)
        except tk.TclError:
            pass                       # (no Command key off a Mac)

    def _redo_set(only):
        j = st["job"]
        if not j or st["busy"]:
            return
        quick.redo_set(j, only)
        _run(go_on=False)

    def _use_place(slug):
        """Places page -> "Use it in Quick": this place is picked."""
        st["place"] = slug
        if st["fmt"] == "music":
            st["fmt"] = formats.FORMATS[0]["key"]
        _paint_all()
        win.deiconify()
        win.lift()
        try:
            import places as _pl
            now_lbl.configure(text="set in \u201c%s\u201d \u2014 type the idea, then Make it"
                              % (_pl.read(slug) or {}).get("name", slug))
        except Exception:
            pass

    def _again_idea(j):
        """MORE LIKE THIS -> Same characters, new idea: the form filled with this run's
        cast, format, shape, length and captions; the idea box empty and focused."""
        st["fmt"] = j.get("format", st["fmt"])
        st["cast"] = list(j.get("cast") or [])[:3]
        st["aspect"] = j.get("aspect")
        st["secs"] = j.get("seconds_asked")
        st["captions"] = j.get("captions", st["captions"])
        st["place"] = j.get("place")
        st["song"] = None
        st["job"] = None
        t_idea.delete("1.0", "end")
        _paint_all()
        _paint_run()
        t_idea.focus_set()
        now_lbl.configure(text="same characters \u2014 type the new idea, then Make it")

    def _add_music(path=None):
        """MUSIC -> + add a song: any audio file goes into the library and is picked."""
        if path is None:
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                parent=win, title="Add a song to the music library",
                filetypes=[("Audio", "*.mp3 *.wav *.m4a *.flac *.aif *.aiff *.ogg *.aac"),
                           ("All files", "*")])
        if not path:
            return None
        try:
            import music as _music
            st["music"] = _music.add_file(path)
        except Exception as exc:
            now_lbl.configure(text="couldn't add that song: %s" % exc)
            return None
        _paint_all()
        now_lbl.configure(text="added to your music — it plays under this video")
        return st["music"]

    def _pick_music(songs):
        """MUSIC -> more...: the whole library in a menu."""
        m = tk.Menu(win, tearoff=0)
        for s_, n_ in songs:
            m.add_command(label=n_, command=lambda v=s_: (st.__setitem__("music", v),
                                                          _paint_all()))
        try:
            m.tk_popup(win.winfo_pointerx(), win.winfo_pointery())
        finally:
            m.grab_release()

    def _reroll(ns):
        """CHECK THESE SHOTS -> Reroll: new takes for those shots, then a fresh cut."""
        j = st["job"]
        if not j or st["busy"]:
            return
        quick.reroll(j, ns)
        _run(go_on=False)

    def _ok_shot(n):
        j = st["job"]
        if not j or st["busy"]:
            return
        quick.ok_shot(j, n)
        _paint_run()

    def _again(j, how):
        if st["busy"]:
            return
        try:
            st["job"] = quick.again(j, how)
        except ValueError as exc:
            now_lbl.configure(text=str(exc))
            return
        _run(go_on=False)

    def _next_in_queue():
        """A copy finished (or stopped with an error): start the next one."""
        if st["queue"] and not st["busy"]:
            st["job"] = st["queue"].pop(0)
            _run(go_on=False)

    def _continue(go_on=True):
        j = st["job"]
        if not j or st["busy"]:
            return
        if j.get("state") == "review:script" and st.get("script_box") is not None:
            try:
                quick.set_script(j, st["script_box"].get("1.0", "end"))
            except Exception as exc:
                now_lbl.configure(text="couldn't read the script: %s" % exc)
                return
        _run(go_on=go_on)

    def _show_job(j):
        if st["busy"]:
            return
        if (st["job"] or {}).get("id") != j.get("id"):
            st["recut"], st["show_shots"] = {}, False
        st["job"] = quick.load(j["id"]) or j
        if st["job"].get("state") in ("running", None, ""):
            # it was running when the app closed: nothing is running it now — offer to go on
            st["job"]["state"] = "stopped"
        _paint_run()

    def _run(go_on):
        st["form_open"] = False          # the next review gets the whole window again
        st["busy"] = True
        st["stop"] = False
        st["done"] = False
        jref = st["job"]

        def work():
            try:
                quick.run(jref, director=ctx.get("director"),
                          progress=lambda m: jref.__setitem__("now", m),
                          stop=lambda: st["stop"], go_on=go_on)
            finally:
                st["done"] = True

        def poll():
            _paint_steps()
            if not st["done"]:
                win.after(500, poll)
                return
            st["busy"] = False
            try:
                import cards as _c
                threading.Thread(target=_c.release, args=("quick",), daemon=True).start()
            except Exception:
                pass
            exc = quick.LAST_EXC.pop(jref["id"], None)
            if type(exc).__name__ == "CardBusy":
                import cards_ui
                cards_ui.handle(win, exc, ctx, retry=lambda: _continue(go_on=False),
                                say=lambda m: now_lbl.configure(text=m), owner="quick")
            _paint_run()
            if jref.get("state") == "done":
                now_lbl.configure(text="done — ▶ Watch it")
                _fmt_frames(refresh=True)        # its format card shows the new video
                quick.notify("Forge Lab", "%s is ready — open Quick to watch it"
                             % (jref.get("title") or "Your video"))
            elif jref.get("state") == "review:drafts":
                quick.notify("Forge Lab", "%d scripts are ready — pick one in Quick"
                             % len(jref.get("drafts") or []))
            elif (jref.get("state") or "").startswith("review"):
                quick.notify("Forge Lab", "Quick is waiting for you to check the %s"
                             % jref["state"].split(":")[1])
            elif jref.get("state") == "error":
                quick.notify("Forge Lab", "Quick stopped: %s" % (jref.get("error") or "")[:120])
            if jref.get("state") in ("done", "error") and st["queue"]:
                win.after(1500, _next_in_queue)
        threading.Thread(target=work, daemon=True).start()
        _paint_run()
        win.after(500, poll)

    def _play_voice(slug):
        import dialogue as _dl
        vs = (fl.read_character(slug) or {}).get("voice_slug")
        v = _dl.load_voice(vs) if vs else None
        w_ = _dl.voice_wav(v, "neutral") if v else None
        if w_:
            try:
                subprocess.Popen(["afplay", w_])
            except Exception:
                pass

    def _open_file(path):
        try:
            subprocess.Popen(["open", path])
        except Exception:
            pass

    def _export(j):
        """EXPORT FOR POSTING: tall + square + wide, a cover with the hook, subtitles and
        the caption, in one folder (export_pack). Local ffmpeg; the window stays usable."""
        if st.get("exporting") or not j.get("episode"):
            return
        import export_pack as _ex
        st["exporting"] = True
        res = {}

        def work():
            try:
                try:
                    import dialogue as _dlg_
                    _veng = (_dlg_.load_project(j["dialogue"]) or {}).get("engine") \
                        if j.get("dialogue") else None
                except Exception:
                    _veng = None
                res["r"] = _ex.export(j["episode"], title=j.get("title"),
                                      post_text=quick.post_text(j) or None,
                                      voice_engine=_veng,
                                      own_video=j.get("finished") if j.get("finished") and
                                      os.path.exists(j["finished"]) else None,
                                      progress=lambda m: res.__setitem__("now", m))
            except Exception as exc:
                res["err"] = "%s" % exc
            res["done"] = True

        def poll():
            if not res.get("done"):
                if res.get("now"):
                    now_lbl.configure(text="exporting: %s" % res["now"])
                win.after(300, poll)
                return
            st["exporting"] = False
            if res.get("err"):
                now_lbl.configure(text="export stopped: %s" % res["err"][:160])
            else:
                r = res["r"]
                names = {"9:16": "tall", "1:1": "square", "16:9": "wide"}
                st["export_done"] = {"job": j["id"], "dir": r["dir"],
                                     "shapes": [names[a] for a in r["videos"]],
                                     "errors": [names.get(a, a) for a in r["errors"]]}
                now_lbl.configure(text="exported \u2014 everything is in one folder")
            if st["job"] is j or (st["job"] or {}).get("id") == j["id"]:
                _paint_stage()
        threading.Thread(target=work, daemon=True).start()
        _paint_stage()
        win.after(300, poll)

    def _reveal(path):
        try:
            subprocess.Popen(["open", "-R", path])
        except Exception:
            pass

    # ================================================================== start
    _paint_series()
    _paint_all()
    _paint_run()
    t_idea.focus_set()
    if ctx.get("btn_colors"):
        try:
            ctx["btn_colors"](win)
        except Exception:
            pass
        b_go.configure(bg=C["ochre"], fg="#14110f")
    win.qk = {"st": st, "t_idea": t_idea, "pick_fmt": _pick_fmt, "toggle_cast": _toggle_cast,
              "go": _go, "cont": _continue, "show_job": _show_job, "b_go": b_go,
              "check_var": check_var, "looks_var": looks_var, "stage": stage,
              "steps_box": steps_box, "now_lbl": now_lbl, "cast_note": cast_note,
              "paint_steps": _paint_steps,
              "fmt_box": fmt_box, "recent": recent, "ctx": ctx, "len_var": len_var,
              "show_history": _show_history, "open_history": _open_history,
              "recut": _recut, "stage_cv": stage_cv, "left_outer": left_outer,
              "export": _export, "add_tonight": _add_tonight, "fmt_frames": _fmt_frames,
              "pick_series": _pick_series, "series_form": _series_form, "paid_var": paid_var,
              "rate": _rate, "undo_recut": _undo_recut, "shortcut_go": _shortcut_go,
              "finish": _finish, "paint_stage": _paint_stage, "paint_run": _paint_run,
              "paid_cb": paid_cb, "pick_product": _pick_product, "prod_row": prod_row,
              "prod_name_var": prod_name_var, "prod_what_var": prod_what_var,
              "save_series": _save_series, "ser_box": ser_box, "ser_note": ser_note,
              "ideas_fn": _ideas,
              "show_night": _show_night, "night_start": _night_start, "night_at": _night_at,
              "night_lnk": night_lnk, "job_kwargs": _job_kwargs,
              "right": right, "show_form": _show_form,
              "len_moved": _len_moved, "len_lbl": len_lbl, "snd_var": snd_var,
              "ideas": _ideas, "ideas_box": ideas_box, "n_box": n_box,
              "from_song": _from_song, "perline_var": perline_var,
              "paint_perline": _paint_perline, "ideas_typed": _ideas_typed,
              "cap_box": cap_box, "cap_row": cap_row, "again": _again,
              "pace_box": pace_box, "pace_row": pace_row, "place_box": place_box,
              "use_place": _use_place, "toggle_more": _toggle_more, "more": more,
              "more_sum": more_sum, "wait_lbl": wait_lbl, "hook_var": hook_var,
              "react_var": react_var, "long_var": long_var, "weird_var": weird_var, "mus_box": mus_box,
              "guess_lbl": guess_lbl, "take_guess": lambda: _take_guess(),
              "add_music": lambda p=None: _add_music(p),
              "reroll": lambda ns: _reroll(ns), "ok_shot": lambda n: _ok_shot(n),
              "flag_rows": lambda: st.get("flag_rows") or [],
              "again_idea": _again_idea, "setcheck_var": setcheck_var,
              "pick_var": pick_var, "pick_draft": _pick_draft, "more_drafts": _more_drafts,
              "redo_set": _redo_set,
              "snd_row": snd_row}
    return win
