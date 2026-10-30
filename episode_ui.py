"""The EPISODE workspace (2026-10-17): Studio + Long video merged into ONE place that makes a
16:9 cartoon episode. Ryan chose the format; the engine is studio_pipeline (pitch -> script ->
shots -> voice -> render -> cut), now aspect-aware.

    Episode  [The Dear Loaf ▾]  New episode     ① Idea ✓ ② Script ✓ ③ Shots ④ Cut     Close
    IDEA    cast chips · one-line idea · length · quality · Pitch me 5 / Use my idea
            or: start from a voiced DIALOGUE scene (one lip-sync shot per line)
    SCRIPT  the script, editable (saved as you type) · rewrite funnier/shorter/deadpan
    SHOTS   a board of shot cards (KEPT / 2 TAKES / NOT MADE) + an inspector: takes, what
            happens (EXACTLY what renders), the line, starts from, length, another take
            [Make the N missing shots · ~X min]  [Stop after this shot]  [Cut the episode →]
    CUT     music bed (one of your songs) · captions on/off · Cut · open / show in Finder

Everything is SAVED AUTOMATICALLY. The old Studio and Long video windows stay one click away
(bottom of the Idea page) for projects made in them.

Rig access through ctx["mod"] (forge_lab, looked up at call time) and studio_pipeline (`sp`,
also looked up at call time, so a driver can stub it). Workers never touch Tk: they fill a
dict, the Tk thread polls (see characters_ui._run).
"""
import os
import sys
import subprocess
import threading
import time

# macOS draws its own white buttons and ignores bg: light text on them vanished
# ("Stop after this step" was blank on the MacBook) - dark text there, always
_MAC = sys.platform == "darwin"

STEPS = ("Idea", "Script", "Shots", "Cut")
SECONDS = (3, 5, 8)
STARTS = (("The set", "set"), ("Just the cast", "r2v"), ("Last shot", "cont"),
          ("Text only", "t2v"))


def list_episodes(sp):
    """[(slug, label)] newest first — 16:9 episodes, then older 9:16 Studio shorts."""
    out = []
    root = sp.studio_root()
    try:
        names = os.listdir(root)
    except Exception:
        names = []
    for n in names:
        pj = sp.load_project(n)
        if not pj:
            continue
        lab = pj.get("title") or n
        if pj.get("aspect") != "16:9":
            lab += "  (short)"
        out.append((pj.get("aspect") == "16:9", pj.get("created") or 0, n, lab))
    out.sort(key=lambda t: (not t[0], -t[1]))
    return [(n, lab) for _w, _c, n, lab in out]


def open_episode(root, ctx):
    import tkinter as tk
    import tkinter.font as tkfont
    import studio_pipeline as sp

    fl = ctx["mod"]
    C, F, mono = ctx["C"], ctx["fonts"], ctx["mono"]
    f, fh, fsm = F["f"], F["fh"], F["fsm"]
    fmono = mono["m"]
    btn_colors = ctx.get("btn_colors")
    fbig = tkfont.Font(family=fh.cget("family"), size=max(15, int(fh.cget("size")) + 5),
                       weight="bold")

    import dock as _dock                 # a tab in the main window (Ryan: tabs)
    win = _dock.window(root)
    win.title("Episode")
    win.configure(bg=C["bg"])
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1240, _sw - 40), min(820, _sh - 110)))
    win.minsize(1080, 700)

    st = {"pj": None, "page": "Idea", "sel": None, "busy": None, "job": None,
          "thumbs": {}, "cast": [], "pitch_cards": [], "save_job": None}

    def say(msg, warn=False):
        status.configure(text=msg, fg=C["ochre"] if warn else C["muted"])

    def lab(parent, text, fg=None, font=None, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["muted"],
                        font=font or fsm, anchor="w", **kw)

    def button(parent, text, cmd, primary=False, **kw):
        return tk.Button(parent, text=text, command=cmd, relief="flat", borderwidth=0,
                         font=fh if primary else fsm, padx=12, pady=8 if primary else 5,
                         bg=C["ochre"] if primary else C["panel2"],
                         fg="#14110f" if primary or _MAC else C["ink"],
                         activebackground="#d9b45e" if primary else C["rust"],
                         activeforeground="#14110f", highlightthickness=0, **kw)

    def save():
        if st["pj"] is not None:
            try:
                sp.save_project(st["pj"])
            except Exception as exc:
                say("could not save: %s" % exc, warn=True)

    # ------------------------------------------------------------------ header
    hd = tk.Frame(win, bg=C["panel"])
    hd.pack(fill="x")
    tk.Label(hd, text="Episode", bg=C["panel"], fg=C["ink"], font=fbig).pack(
        side="left", padx=(16, 10), pady=10)
    proj_var = tk.StringVar(value="")
    proj_menu = tk.OptionMenu(hd, proj_var, "")
    proj_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, borderwidth=0, activebackground=C["rust"],
                        width=22, anchor="w")
    proj_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    proj_menu.pack(side="left")
    button(hd, "New episode", lambda: _new()).pack(side="left", padx=8)
    button(hd, "Close", lambda: win.destroy()).pack(side="right", padx=12)
    chipbar = tk.Frame(hd, bg=C["panel"])
    chipbar.pack(side="right", padx=8)
    chips = {}
    for _i, _s in enumerate(STEPS):
        _l = tk.Label(chipbar, text="", bg=C["panel"], fg=C["muted"], font=fsm, padx=10,
                      pady=5, cursor="hand2", highlightthickness=1,
                      highlightbackground=C["line"])
        _l.pack(side="left", padx=2)
        _l.bind("<Button-1>", lambda _e, s=_s: _go(s))
        chips[_s] = _l
    tk.Frame(win, bg=C["line"], height=1).pack(fill="x")
    status = tk.Label(win, text="", bg=C["bg"], fg=C["muted"], font=fsm, anchor="w")
    status.pack(fill="x", padx=16, pady=(6, 0))

    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=14, pady=(6, 12))
    pages = {s: tk.Frame(body, bg=C["bg"]) for s in STEPS}

    # ================================================================== IDEA
    ip = pages["Idea"]
    icol = tk.Frame(ip, bg=C["panel"], padx=18, pady=14)
    icol.pack(fill="both", expand=True)
    lab(icol, "Cast — click to add; the first one leads the story", fg=C["ink"],
        font=fh).pack(fill="x")
    castbox = tk.Frame(icol, bg=C["panel"])
    castbox.pack(fill="x", pady=(6, 12))
    lab(icol, "Idea — one line", fg=C["ink"], font=fh).pack(fill="x")
    idea_var = tk.StringVar()
    tk.Entry(icol, textvariable=idea_var, bg="#100e0c", fg=C["ink"], relief="flat", font=f,
             insertbackground=C["ink"]).pack(fill="x", ipady=5, pady=(4, 10))
    orow = tk.Frame(icol, bg=C["panel"])
    orow.pack(fill="x")
    lab(orow, "Length").pack(side="left")
    len_var = tk.StringVar(value="60 s")
    _lm = tk.OptionMenu(orow, len_var, *["%d s" % s for s in sp.EPISODE_LENGTHS])
    _lm.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, activebackground=C["rust"])
    _lm["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _lm.pack(side="left", padx=(6, 18))
    lab(orow, "Quality").pack(side="left")
    q_var = tk.StringVar(value="Draft")
    _qm = tk.OptionMenu(orow, q_var, *sp.QUALITIES)
    _qm.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, activebackground=C["rust"])
    _qm["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _qm.pack(side="left", padx=6)
    irow = tk.Frame(icol, bg=C["panel"])
    irow.pack(fill="x", pady=(14, 0))
    b_use = button(irow, "Use my idea →", lambda: _use_idea(), primary=True)
    b_use.pack(side="left")
    b_pitch = button(irow, "Pitch me 5", lambda: _pitch())
    b_pitch.pack(side="left", padx=8)
    pitchbox = tk.Frame(icol, bg=C["panel"])
    pitchbox.pack(fill="x", pady=(10, 0))
    tk.Frame(icol, bg=C["line"], height=1).pack(fill="x", pady=14)
    lab(icol, "Or start from a voiced Dialogue scene — one lip-sync shot per line, "
              "using the takes you chose", fg=C["ink"], font=fh).pack(fill="x")
    drow = tk.Frame(icol, bg=C["panel"])
    drow.pack(fill="x", pady=(6, 0))
    dlg_var = tk.StringVar(value="")
    dlg_menu = tk.OptionMenu(drow, dlg_var, "")
    dlg_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                       highlightthickness=0, borderwidth=0, activebackground=C["rust"],
                       width=26, anchor="w")
    dlg_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    dlg_menu.pack(side="left")
    b_from_dlg = button(drow, "Make shots from it →", lambda: _from_dialogue())
    b_from_dlg.pack(side="left", padx=8)
    lrow = tk.Frame(icol, bg=C["panel"])
    lrow.pack(side="bottom", fill="x")
    lab(lrow, "Made something in the old windows?", fg=C["faint"]).pack(side="left")
    _lg1 = tk.Label(lrow, text="Open the old Studio", bg=C["panel"], fg=C["ochre"], font=fsm,
                    cursor="hand2")
    _lg1.pack(side="left", padx=8)
    _lg1.bind("<Button-1>", lambda _e: ctx.get("open_studio") and ctx["open_studio"]())
    _lg2 = tk.Label(lrow, text="Open the old Long video", bg=C["panel"], fg=C["ochre"],
                    font=fsm, cursor="hand2")
    _lg2.pack(side="left")
    _lg2.bind("<Button-1>", lambda _e: ctx.get("open_long") and ctx["open_long"]())

    # ================================================================== SCRIPT
    spg = pages["Script"]
    scol = tk.Frame(spg, bg=C["panel"], padx=18, pady=14)
    scol.pack(fill="both", expand=True)
    shd = tk.Frame(scol, bg=C["panel"])
    shd.pack(fill="x")
    lab(shd, "Script — edit freely; it saves as you type", fg=C["ink"], font=fh).pack(
        side="left")
    b_plan = button(shd, "Plan the shots →", lambda: _plan(), primary=True)
    b_plan.pack(side="right")
    b_write = button(shd, "Write the script", lambda: _write_script())
    b_write.pack(side="right", padx=8)
    t_script = tk.Text(scol, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                       font=fmono, padx=8, pady=6, insertbackground=C["ink"], height=20)
    t_script.pack(fill="both", expand=True, pady=(8, 8))
    rrow = tk.Frame(scol, bg=C["panel"])
    rrow.pack(fill="x")
    lab(rrow, "Rewrite it").pack(side="left")
    for _how in ("funnier", "shorter", "more deadpan"):
        button(rrow, _how, lambda h=_how: _rewrite(h)).pack(side="left", padx=4)

    # ================================================================== SHOTS
    shp = pages["Shots"]
    bar = tk.Frame(shp, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    bar.pack(side="bottom", fill="x", pady=(8, 0))
    b_make = button(bar, "Make the missing shots", lambda: _make_missing(), primary=True)
    b_make.pack(side="left", padx=10, pady=10)
    b_stop = button(bar, "Stop after this shot", lambda: _stop(), state="disabled")
    b_stop.pack(side="left")
    est_lbl = lab(bar, "", fg=C["faint"])
    est_lbl.pack(side="left", padx=10)
    b_to_cut = button(bar, "Cut the episode →", lambda: _go("Cut"))
    b_to_cut.pack(side="right", padx=10)

    insp = tk.Frame(shp, bg=C["panel"], width=350, padx=14, pady=12)
    insp.pack(side="right", fill="y")
    insp.pack_propagate(False)
    # -- THE SET: one place for the whole cast (studio_pipeline.make_set)
    setbar = tk.Frame(shp, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    setbar.pack(side="top", fill="x", padx=(0, 10), pady=(0, 8))
    _sl = tk.Frame(setbar, bg=C["panel"])
    _sl.pack(side="left", fill="y", padx=10, pady=8)
    lab(_sl, "THE SET — where it happens", fg=C["ink"], font=fh).pack(anchor="w")
    where_var = tk.StringVar()
    e_where = tk.Entry(_sl, textvariable=where_var, bg="#100e0c", fg=C["ink"], relief="flat",
                       font=fsm, width=44, insertbackground=C["ink"])
    e_where.pack(anchor="w", pady=(4, 4))
    # or a KEPT PLACE (the Places page): the set is built in that exact picture
    _plr = tk.Frame(_sl, bg=C["panel"])
    _plr.pack(anchor="w", pady=(0, 4))
    lab(_plr, "or a kept place").pack(side="left")
    place_var = tk.StringVar(value="\u2014 described above \u2014")
    place_mb = tk.Menubutton(_plr, textvariable=place_var, bg=C["panel2"], fg=C["ink"],
                             font=fsm, relief="flat", padx=8, pady=3, width=26, anchor="w")
    place_m = tk.Menu(place_mb, tearoff=0, bg=C["panel2"], fg="#000000", font=fsm)
    place_mb["menu"] = place_m
    place_mb.pack(side="left", padx=6)
    _sbr = tk.Frame(_sl, bg=C["panel"])
    _sbr.pack(anchor="w")
    b_set = button(_sbr, "Build the set", lambda: _rebuild_set())
    b_set.pack(side="left")
    set_note = lab(_sbr, "", fg=C["faint"])
    set_note.pack(side="left", padx=8)
    set_pics = tk.Frame(setbar, bg=C["panel"])
    set_pics.pack(side="left", padx=(0, 10), pady=8)

    board_wrap = tk.Frame(shp, bg=C["bg"])
    board_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
    bvsb = tk.Scrollbar(board_wrap, orient="vertical")
    bvsb.pack(side="right", fill="y")
    bcan = tk.Canvas(board_wrap, bg=C["bg"], highlightthickness=0, yscrollcommand=bvsb.set)
    bcan.pack(side="left", fill="both", expand=True)
    bvsb.configure(command=bcan.yview)
    board = tk.Frame(bcan, bg=C["bg"])
    _bw = bcan.create_window((0, 0), window=board, anchor="nw")
    board.bind("<Configure>", lambda e: bcan.configure(scrollregion=bcan.bbox("all")))
    bcan.bind("<Configure>", lambda e: bcan.itemconfigure(_bw, width=e.width))
    for _i in range(4):
        board.columnconfigure(_i, weight=1, uniform="card")
    try:
        fl.scoped_wheel(win, bcan)
    except Exception:
        pass
    cards = {}

    # -- the inspector
    i_title = lab(insp, "pick a shot", fg=C["ink"], font=fbig)
    i_title.pack(fill="x")
    i_sub = lab(insp, "", fg=C["faint"])
    i_sub.pack(fill="x")
    lab(insp, "Takes — click one to keep it").pack(fill="x", pady=(10, 2))
    takes_row = tk.Frame(insp, bg=C["panel"])
    takes_row.pack(fill="x")
    lab(insp, "What happens — this is EXACTLY what gets rendered").pack(fill="x",
                                                                          pady=(10, 2))
    t_action = tk.Text(insp, height=4, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                       font=fsm, padx=6, pady=4, insertbackground=C["ink"])
    t_action.pack(fill="x")
    lab(insp, "Line — change the words and the voice is redone").pack(fill="x",
                                                                         pady=(10, 2))
    lnrow = tk.Frame(insp, bg=C["panel"])
    lnrow.pack(fill="x")
    line_var = tk.StringVar()
    tk.Entry(lnrow, textvariable=line_var, bg="#100e0c", fg=C["ink"], relief="flat",
             font=fsm, insertbackground=C["ink"]).pack(side="left", fill="x", expand=True)
    button(lnrow, "▶", lambda: _play_line()).pack(side="left", padx=(6, 0))
    lab(insp, "Starts from").pack(fill="x", pady=(10, 2))
    start_row = tk.Frame(insp, bg=C["line"])
    start_row.pack(fill="x")
    start_btns = {}
    for _lab, _vm in STARTS:
        _l = tk.Label(start_row, text=_lab, bg=C["panel"], fg=C["ink"], font=fsm, pady=6,
                      cursor="hand2")
        _l.pack(side="left", fill="x", expand=True, padx=(0, 1))
        _l.bind("<Button-1>", lambda _e, vm=_vm: _set_start(vm))
        start_btns[_vm] = _l
    lab(insp, "Camera").pack(fill="x", pady=(10, 2))
    cam_row = tk.Frame(insp, bg=C["panel"])
    cam_row.pack(fill="x")
    cam_btns = {}
    import apps as _apps
    for _i, _cm in enumerate([sp.CAMERA_NONE] + [m for m, _w in _apps.MOVES]):
        _l = tk.Label(cam_row, text=_cm, bg=C["panel2"], fg=C["ink"], font=fsm, padx=6, pady=3,
                      cursor="hand2")
        _l.grid(row=_i // 3, column=_i % 3, sticky="ew", padx=(0, 2), pady=(0, 2))
        _l.bind("<Button-1>", lambda _e, m=_cm: _set_camera(m))
        cam_btns[_cm] = _l
    for _i in range(3):
        cam_row.columnconfigure(_i, weight=1, uniform="cam")
    lab(insp, "Length").pack(fill="x", pady=(10, 2))
    sec_row = tk.Frame(insp, bg=C["line"])
    sec_row.pack(fill="x")
    sec_btns = {}
    for _s in SECONDS:
        _l = tk.Label(sec_row, text="%d s" % _s, bg=C["panel"], fg=C["ink"], font=fsm, pady=6,
                      cursor="hand2")
        _l.pack(side="left", fill="x", expand=True, padx=(0, 1))
        _l.bind("<Button-1>", lambda _e, s=_s: _set_secs(s))
        sec_btns[_s] = _l
    b_take = button(insp, "Make another take", lambda: _another_take())
    b_take.pack(side="bottom", fill="x", pady=(10, 0))

    # ================================================================== CUT
    cp = pages["Cut"]
    ccol = tk.Frame(cp, bg=C["panel"], padx=18, pady=14)
    ccol.pack(fill="both", expand=True)
    lab(ccol, "Cut the episode", fg=C["ink"], font=fbig).pack(fill="x")
    cut_note = lab(ccol, "", fg=C["faint"], wraplength=900, justify="left")
    cut_note.pack(fill="x", pady=(2, 10))
    mrow = tk.Frame(ccol, bg=C["panel"])
    mrow.pack(fill="x")
    lab(mrow, "Music under it").pack(side="left")
    music_var = tk.StringVar(value="none")
    music_menu = tk.OptionMenu(mrow, music_var, "none")
    music_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                         highlightthickness=0, borderwidth=0, activebackground=C["rust"],
                         width=34, anchor="w")
    music_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    music_menu.pack(side="left", padx=8)
    cap_lbl = tk.Label(ccol, text="", bg=C["panel"], fg=C["ink"], font=fsm, cursor="hand2",
                       anchor="w")
    cap_lbl.pack(fill="x", pady=(10, 0))
    cap_lbl.bind("<Button-1>", lambda _e: _toggle_captions())
    look_row = tk.Frame(ccol, bg=C["panel"])       # Clean / Pop / Big, when captions are on
    look_row.pack(fill="x", pady=(4, 0))
    pace_row = tk.Frame(ccol, bg=C["panel"])       # how much silence stays between lines
    pace_row.pack(fill="x", pady=(8, 0))
    tl_box = tk.Frame(ccol, bg=C["panel"])         # the TIMELINE: order, trims, leave out
    tl_box.pack(fill="x", pady=(12, 0))
    crow = tk.Frame(ccol, bg=C["panel"])
    crow.pack(fill="x", pady=(16, 0))
    b_cut = button(crow, "Cut the episode", lambda: _cut(), primary=True)
    b_cut.pack(side="left")
    b_open = button(crow, "▶ Watch it", lambda: _open_cut())
    b_open.pack(side="left", padx=8)
    b_reveal = button(crow, "Show in Finder", lambda: _reveal_cut())
    b_reveal.pack(side="left")
    cover_lbl = tk.Label(ccol, bg="#100e0c", fg=C["faint"], font=fsm, text="not cut yet")
    cover_lbl.pack(fill="both", expand=True, pady=(14, 0))

    # ================================================================== state + painting
    def _done(step):
        pj = st["pj"]
        if not pj:
            return False
        shots = pj.get("shots") or []
        if step == "Idea":
            return bool(pj.get("pitch") or (pj.get("source") or {}).get("dialogue"))
        if step == "Script":
            return bool(pj.get("script") or (pj.get("source") or {}).get("dialogue"))
        if step == "Shots":
            return bool(shots) and all(s.get("chosen") for s in shots)
        if step == "Cut":
            return bool((pj.get("cut") or {}).get("mp4")) and os.path.exists(pj["cut"]["mp4"])
        return False

    def _paint_chips():
        for i, s in enumerate(STEPS):
            on = s == st["page"]
            ok = _done(s)
            chips[s].configure(text="%s%d  %s" % ("✓ " if ok else "", i + 1, s),
                               bg=C["ink"] if on else C["panel"],
                               fg="#14110f" if on else (C["muted"] if ok else C["ink"]))

    def _go(step):
        if step != "Idea" and st["pj"] is None:
            say("start an episode first — pick a cast and an idea", warn=True)
            step = "Idea"
        st["page"] = step
        for s, pg in pages.items():
            if s == step:
                pg.pack(fill="both", expand=True)
            else:
                pg.pack_forget()
        if step == "Script":
            _fill_script()
        elif step == "Shots":
            _build_board()
        elif step == "Cut":
            _fill_cut()
        _paint_chips()

    # -- projects
    def _relist_projects():
        m = proj_menu["menu"]
        m.delete(0, "end")
        eps = list_episodes(sp)
        for slug, label in eps:
            m.add_command(label=label, command=lambda s=slug: _open(s))
        if not eps:
            m.add_command(label="(no episodes yet)", command=lambda: None)
        if st["pj"]:
            proj_var.set(next((l for s, l in eps if s == st["pj"]["slug"]), st["pj"]["slug"]))
        else:
            proj_var.set("— new episode —")

    def _open(slug):
        pj = sp.load_project(slug)
        if pj is None:
            say("could not open %s" % slug, warn=True)
            return
        st["pj"] = pj
        st["sel"] = None
        st["cast"] = [c for c in (pj.get("characters") or [])]
        idea_var.set(pj.get("idea") or "")
        if "%d s" % int(pj.get("length_s") or 60) in ["%d s" % s for s in sp.EPISODE_LENGTHS]:
            len_var.set("%d s" % int(pj.get("length_s")))
        if pj.get("quality") in sp.QUALITIES:
            q_var.set(pj["quality"])
        _paint_cast()
        _show_pitches(pj.get("pitches") or [], quiet=True)
        _relist_projects()
        if not _done("Idea"):
            _go("Idea")
        elif not (pj.get("shots")):
            _go("Script")
        elif not _done("Cut"):
            _go("Shots")
        else:
            _go("Cut")
        say("%s · %s · every change saves itself"
            % (pj.get("title") or slug, pj.get("aspect")))

    def _new():
        st["pj"] = None
        st["cast"] = []
        idea_var.set("")
        _show_pitches([], quiet=True)
        _paint_cast()
        _relist_projects()
        _go("Idea")
        say("pick a cast and an idea")

    # -- IDEA
    def _paint_cast():
        for w_ in castbox.winfo_children():
            w_.destroy()
        chars = fl.list_characters()
        if not chars:
            lab(castbox, "no characters yet — make one in Characters first",
                fg=C["ochre"]).pack(side="left")
            return
        for slug, name in chars:
            on = slug in st["cast"]
            idx = (st["cast"].index(slug) + 1) if on else 0
            l_ = tk.Label(castbox, text=("%d · %s" % (idx, name)) if on else name,
                          bg=C["ink"] if on else C["panel2"], fg="#14110f" if on else C["ink"],
                          font=fsm, padx=10, pady=5, cursor="hand2")
            l_.pack(side="left", padx=(0, 6), pady=2)
            l_.bind("<Button-1>", lambda _e, s=slug: _toggle_cast(s))

    def _toggle_cast(slug):
        if slug in st["cast"]:
            st["cast"].remove(slug)
        else:
            st["cast"].append(slug)
        _paint_cast()

    def _length():
        try:
            return int(len_var.get().split()[0])
        except Exception:
            return 60

    def _ensure_project():
        if st["pj"] is not None:
            # an open episode: what is typed and clicked on the Idea page now wins
            pj = st["pj"]
            if idea_var.get().strip():
                pj["idea"] = idea_var.get().strip()
            if st["cast"]:
                pj["characters"] = list(st["cast"])
            pj["length_s"] = _length()
            pj["quality"] = q_var.get()
            save()
            return pj
        if not st["cast"]:
            say("click at least one character for the cast", warn=True)
            return None
        idea = idea_var.get().strip()
        if not idea:
            say("type a one-line idea first", warn=True)
            return None
        base = sp.slugify(idea)[:40] or "episode"
        slug, i = base, 2
        while os.path.exists(sp.project_path(slug)):       # never overwrite another episode
            slug, i = "%s-%d" % (base, i), i + 1
        pj = sp.new_project(list(st["cast"]), idea, length_s=_length(), quality=q_var.get(),
                            title=idea[:60], slug=slug, aspect="16:9")
        st["pj"] = pj
        _relist_projects()
        return pj

    def _use_idea():
        pj = _ensure_project()
        if pj is None:
            return
        idea = pj.get("idea") or ""
        pj["pitch"] = {"title": (idea[:48] or pj.get("slug")), "logline": idea, "hook": idea}
        save()
        _go("Script")
        say("your idea is the pitch — now Write the script")

    def _pitch():
        pj = _ensure_project()
        if pj is None:
            return
        _job(lambda: sp.pitch(pj, n=5), _show_pitches, "pitching 5 ideas on the rig")

    def _show_pitches(ps, quiet=False):
        for w_ in pitchbox.winfo_children():
            w_.destroy()
        for i, p_ in enumerate(ps or []):
            row = tk.Frame(pitchbox, bg=C["panel2"], padx=10, pady=6)
            row.pack(fill="x", pady=2)
            button(row, "Choose", lambda k=i: _choose(k)).pack(side="right")
            lab(row, (p_ or {}).get("title", "?"), fg=C["ink"], font=fh).pack(fill="x")
            lab(row, (p_ or {}).get("logline", ""), fg=C["muted"], wraplength=760,
                justify="left").pack(fill="x")
        if not quiet:
            say("pick one of the %d pitches" % len(ps or []))

    def _choose(i):
        try:
            sp.choose_pitch(st["pj"], i)
        except Exception as exc:
            say(str(exc), warn=True)
            return
        _go("Script")
        say("good pick — now Write the script")

    def _from_dialogue():
        lab_ = dlg_var.get()
        slug = st.get("dlg_map", {}).get(lab_)
        if not slug:
            say("pick a Dialogue scene first", warn=True)
            return
        try:
            pj = sp.from_dialogue(slug, quality=q_var.get())
        except Exception as exc:
            say(str(exc), warn=True)
            return
        st["pj"] = pj
        _relist_projects()
        _go("Shots")
        say("%d lip-sync shots from %s — Make the missing shots"
            % (len(pj["shots"]), lab_))

    def _relist_dialogue():
        m = dlg_menu["menu"]
        m.delete(0, "end")
        st["dlg_map"] = {}
        try:
            import dialogue as dlg
            names = dlg.list_projects()
        except Exception:
            names = []
        for n in names:
            st["dlg_map"][n] = n
            m.add_command(label=n, command=lambda v=n: dlg_var.set(v))
        dlg_var.set(names[0] if names else "(no Dialogue scenes yet)")

    # -- SCRIPT
    def _fill_script():
        pj = st["pj"]
        t_script.delete("1.0", "end")
        src = (pj or {}).get("source") or {}
        if src.get("dialogue"):
            t_script.insert("1.0", "This episode comes from the Dialogue scene %r — its "
                                   "lines are the shots. Go to Shots.\n" % src["dialogue"])
            return
        if pj and pj.get("script"):
            t_script.insert("1.0", fl.studio_script_to_text(pj["script"]))
        t_script.edit_modified(False)

    def _script_changed(_e=None):
        if not t_script.edit_modified():
            return
        t_script.edit_modified(False)
        if st.get("save_job"):
            try:
                win.after_cancel(st["save_job"])
            except Exception:
                pass
        st["save_job"] = win.after(700, _save_script)

    def _save_script():
        st["save_job"] = None
        pj = st["pj"]
        if not pj or not pj.get("script") or (pj.get("source") or {}).get("dialogue"):
            return
        pj["script"] = fl.studio_text_to_script(t_script.get("1.0", "end"), pj.get("script"))
        save()
        say("script saved")
    t_script.bind("<<Modified>>", _script_changed)

    def _write_script():
        pj = st["pj"]
        if not pj:
            return
        _job(lambda: sp.write_script(pj), lambda _sc: (_fill_script(), _paint_chips(),
                                                       say("there is the script — edit "
                                                           "it, or Plan the shots")),
             "writing the script on the rig")

    def _rewrite(how):
        pj = st["pj"]
        if not pj or not pj.get("script"):
            say("write the script first", warn=True)
            return
        text = t_script.get("1.0", "end")

        def run():
            d = sp.get_director()
            try:
                d.load()
            except Exception:
                pass
            return d.rewrite(text, how)

        def after(out):
            t_script.delete("1.0", "end")
            t_script.insert("1.0", out or text)
            _save_script()
            say("rewrote it (%s)" % how)
        _job(run, after, "rewriting (%s)" % how)

    def _plan():
        pj = st["pj"]
        if not pj:
            return
        if not pj.get("script"):
            say("write the script first", warn=True)
            return
        _save_script()
        _job(lambda: sp.plan_shots(pj), lambda _s: (_go("Shots"), say(
            "%d shots planned — Make the missing shots" % len(pj.get("shots") or []))),
            "planning the shots on the rig")

    # -- SHOTS: the set
    def _paint_set():
        pj = st["pj"]
        for w_ in set_pics.winfo_children():
            w_.destroy()
        if not pj:
            return
        if not st.get("where_typing"):
            where_var.set(sp.set_where(pj))
        place_m.delete(0, "end")
        place_m.add_command(label="\u2014 described above \u2014",
                            command=lambda: _set_place(None))
        try:
            import places as _pl
            _lib = _pl.library()
        except Exception:
            _lib = []
        for s_, n_ in _lib:
            place_m.add_command(label=n_, command=lambda v=s_: _set_place(v))
        place_var.set(dict(_lib).get(pj.get("place"), "\u2014 described above \u2014")
                      if pj.get("place") else "\u2014 described above \u2014")
        e_where.configure(state="disabled" if pj.get("place") else "normal")
        s = pj.get("set") or {}
        uses = sum(1 for sh in pj.get("shots") or [] if sh.get("video_mode") == "set")
        plates = [("wide", s.get("master"))] + [
            (dict(fl.list_characters()).get(k, k), v)
            for k, v in sorted((s.get("singles") or {}).items())]
        shown = 0
        for name, path in plates:
            if not (path and os.path.exists(path)):
                continue
            img = fl.thumb_photo(path, 120)
            if img is None:
                continue
            col = tk.Frame(set_pics, bg=C["panel"])
            col.pack(side="left", padx=(0, 6))
            l_ = tk.Label(col, image=img, bg=C["panel"])
            l_.image = img
            l_.pack()
            lab(col, name, fg=C["faint"]).pack()
            shown += 1
        ready = sp.set_ready(pj)
        b_set.configure(text="Rebuild the set" if shown else "Build the set")
        if not uses:
            set_note.configure(text="no shot starts from the set")
        elif ready:
            set_note.configure(text="%d shot%s start here" % (uses, "" if uses == 1 else "s"))
        else:
            set_note.configure(text="built with the next shots (~2 min)")

    def _set_place(slug):
        pj = st["pj"]
        if not pj:
            return
        pj["place"] = slug
        save()
        _paint_set()
        say(("set in a kept place \u2014 Rebuild the set to see it" if slug else
             "the set is the place described above") if (pj.get("set") or {}).get("master")
            else "")

    def _where_done(_e=None):
        pj = st["pj"]
        st["where_typing"] = False
        if not pj:
            return
        new = where_var.get().strip()
        if new and new != sp.set_where(pj):
            pj["where"] = new
            save()
            say("the set will be rebuilt in %s with the next shots" % new)
        _paint_set()

    def _rebuild_set():
        pj = st["pj"]
        if not pj or not pj.get("shots"):
            say("no shots yet — the set is built for the shots", warn=True)
            return
        _where_done()

        def run(job):
            return sp.make_set(pj, progress=lambda m: job.__setitem__("progress", m))
        _job(run, lambda _s: (_paint_set(), say("the set is built — Make the missing "
                                                 "shots")), "building the set", pass_job=True)

    e_where.bind("<KeyRelease>", lambda _e: st.__setitem__("where_typing", True))
    e_where.bind("<Return>", _where_done)
    e_where.bind("<FocusOut>", _where_done)

    # -- SHOTS: the board
    def _state(sh):
        n_t = len(sh.get("takes") or [])
        if sh.get("status") == "running":
            return "MAKING", C["ochre"], "#14110f"
        if sh.get("status") == "error" and not sh.get("chosen"):
            return "FAILED", "#e0553b", "#14110f"
        if sh.get("chosen"):
            return ("KEPT · %d TAKES" % n_t) if n_t > 1 else "KEPT", C["moss"], "#14110f"
        return "NOT MADE", C["panel2"], C["ink"]

    def _thumb_path(sh):
        pid = sh.get("chosen")
        if not pid or not st["pj"]:
            return None
        return os.path.join(sp.project_dir(st["pj"]["slug"]), "frames",
                            "thumb_%02d_%s.png" % (sh.get("n", 0), pid[:8]))

    def _build_board():
        for w_ in board.winfo_children():
            w_.destroy()
        cards.clear()
        _paint_set()
        pj = st["pj"]
        shots = (pj or {}).get("shots") or []
        if not shots:
            lab(board, "no shots yet — Plan the shots on the Script page, or start from "
                       "a Dialogue scene", fg=C["faint"]).grid(row=0, column=0, columnspan=4,
                                                               sticky="w", pady=20)
        need = []
        names = dict(fl.list_characters())
        for i, sh in enumerate(shots):
            card = tk.Frame(board, bg=C["panel"], highlightthickness=2,
                            highlightbackground=C["line"], cursor="hand2")
            card.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0, 8), pady=(0, 8))
            pic = tk.Label(card, bg="#100e0c", height=6, cursor="hand2")
            pic.pack(fill="x")
            tp = _thumb_path(sh)
            if tp and os.path.exists(tp):
                img = fl.thumb_photo(tp, 200)
                if img is not None:
                    pic.configure(image=img, height=112)
                    pic.image = img
            elif sh.get("chosen"):
                need.append(sh)
            stt, sbg, sfg = _state(sh)
            chip = tk.Label(card, text=stt, bg=sbg, fg=sfg, font=fsm, padx=6)
            chip.pack(anchor="w", padx=8, pady=(6, 0))
            who = sh.get("speaker") or names.get((pj.get("characters") or ["?"])[0],
                                                 (pj.get("characters") or ["?"])[0])
            t1 = tk.Label(card, text="%d · %s · %d s" % (sh.get("n", i + 1), who,
                                                                  sh.get("seconds", 3)),
                          bg=C["panel"], fg=C["ink"], font=fsm, anchor="w", cursor="hand2")
            t1.pack(fill="x", padx=8)
            t2 = tk.Label(card, text=(sh.get("line") or sh.get("action") or "")[:90],
                          bg=C["panel"], fg=C["muted"], font=fsm, anchor="w", justify="left",
                          wraplength=190, cursor="hand2")
            t2.pack(fill="x", padx=8, pady=(0, 8))
            for w_ in (card, pic, t1, t2):
                w_.bind("<Button-1>", lambda _e, n=sh.get("n"): _select(n))
            cards[sh.get("n")] = card
        if need:
            _make_thumbs(need)
        if st["sel"] not in cards:
            st["sel"] = shots[0]["n"] if shots else None
        _paint_sel()
        _fill_inspector()
        _refresh_est()

    def _paint_sel():
        for n, card in cards.items():
            card.configure(highlightbackground=C["ochre"] if n == st["sel"] else C["line"])

    def _make_thumbs(shots):
        jobs = []
        for sh in shots:
            clip = sp._chosen_clip_path(st["pj"], sh)
            out = _thumb_path(sh)
            if clip and out and not os.path.exists(out):
                jobs.append((clip, out))
        if not jobs:
            return

        def work():
            for clip, out in jobs:
                try:
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.4", "-i", clip,
                                    "-frames:v", "1", "-vf", "scale=-2:200", out],
                                   capture_output=True, timeout=60)
                except Exception:
                    pass
            job["done"] = True
        job = {"done": False}

        def poll():
            if not job["done"]:
                win.after(300, poll)
                return
            if st["page"] == "Shots":
                _build_board()
        threading.Thread(target=work, daemon=True).start()
        win.after(300, poll)

    # -- SHOTS: the inspector
    def _sel_shot():
        pj = st["pj"]
        for sh in (pj or {}).get("shots") or []:
            if sh.get("n") == st["sel"]:
                return sh
        return None

    def _select(n):
        _commit_inspector()
        st["sel"] = n
        _paint_sel()
        _fill_inspector()

    def _fill_inspector():
        sh = _sel_shot()
        for w_ in takes_row.winfo_children():
            w_.destroy()
        st["filling"] = True
        t_action.delete("1.0", "end")
        line_var.set("")
        if sh is None:
            i_title.configure(text="pick a shot")
            i_sub.configure(text="")
            st["filling"] = False
            return
        i_title.configure(text="Shot %d" % sh.get("n", 0))
        i_sub.configure(text="%s · %s" % (sh.get("speaker") or "", fl.studio_plain_intent(
            sh.get("intent")) if hasattr(fl, "studio_plain_intent") else sh.get("intent")))
        for k, pid in enumerate(sh.get("takes") or []):
            on = pid == sh.get("chosen")
            l_ = tk.Label(takes_row, text=("✓ take %d" if on else "take %d") % (k + 1),
                          bg=C["moss"] if on else C["panel2"], fg="#14110f" if on else C["ink"],
                          font=fsm, padx=8, pady=4, cursor="hand2")
            l_.pack(side="left", padx=(0, 4))
            l_.bind("<Button-1>", lambda _e, p_=pid: _keep(p_))
        if not sh.get("takes"):
            lab(takes_row, "none yet", fg=C["faint"]).pack(side="left")
        t_action.insert("1.0", sh.get("action") or "")
        line_var.set(sh.get("line") or "")
        for vm, l_ in start_btns.items():
            on = vm == (sh.get("video_mode") or "r2v")
            l_.configure(bg=C["ink"] if on else C["panel"], fg="#14110f" if on else C["ink"])
        for m_, l_ in cam_btns.items():
            on = m_ == (sh.get("camera_move") or sp.CAMERA_NONE)
            l_.configure(bg=C["ink"] if on else C["panel2"], fg="#14110f" if on else C["ink"])
        for s_, l_ in sec_btns.items():
            on = s_ == sh.get("seconds")
            l_.configure(bg=C["ink"] if on else C["panel"], fg="#14110f" if on else C["ink"])
        try:
            e = sp.estimate({**st["pj"], "shots": [sh]})
            b_take.configure(text="Make another take · ~%s" % _fmt(e.get("total_s") or 0))
        except Exception:
            b_take.configure(text="Make another take")
        st["filling"] = False

    def _commit_inspector():
        """The inspector's typed edits -> the shot, through apply_shot_edit, so the ACTION
        reaches the render prompt and a changed LINE is re-voiced (see fix 6633b6f)."""
        sh = _sel_shot()
        if sh is None or st.get("filling"):
            return []
        ch = sp.apply_shot_edit(sh, t_action.get("1.0", "end"), line_var.get())
        if ch:
            if "line" in ch and sh.get("voice_from") == "dialogue":
                sh["voice_from"] = None          # new words: no longer the Dialogue take
            save()
        return ch

    def _keep(pid):
        sh = _sel_shot()
        if sh is None:
            return
        sh["chosen"] = pid
        save()
        _build_board()
        say("kept that take")

    def _set_start(vm):
        sh = _sel_shot()
        if sh is None:
            return
        if vm == "cont" and sh.get("n") == 1:
            say("the first shot has no last shot to start from", warn=True)
            return
        sh["video_mode"] = vm
        save()
        _fill_inspector()

    def _set_camera(move):
        sh = _sel_shot()
        if sh is None:
            return
        _commit_inspector()
        sp.set_camera(sh, move)
        save()
        _fill_inspector()
        say("shot %d: %s" % (sh["n"], move.lower() if move != sp.CAMERA_NONE else
                              "no camera move"))

    def _set_secs(s_):
        sh = _sel_shot()
        if sh is None:
            return
        sh["seconds"] = s_
        save()
        _build_board()

    def _play_line():
        sh = _sel_shot()
        wav = (sh or {}).get("audio_wav")
        if not wav or not os.path.exists(wav):
            say("this shot's line has no voice yet — it is made with the shot", warn=True)
            return
        try:
            subprocess.Popen(["afplay", wav])
        except Exception as exc:
            say("could not play: %s" % exc, warn=True)

    # -- making shots
    def _fmt(sec):
        sec = int(round(sec or 0))
        return ("%d min %02d s" % (sec // 60, sec % 60)) if sec >= 60 else "%d s" % sec

    def _missing():
        return [sh for sh in ((st["pj"] or {}).get("shots") or [])
                if not sh.get("chosen") and not sh.get("skip")]   # left out: not needed

    def _refresh_est():
        miss = _missing()
        if not st["pj"] or not st["pj"].get("shots"):
            b_make.configure(text="Make the missing shots")
            est_lbl.configure(text="")
            return
        if not miss:
            b_make.configure(text="Every shot is made")
            est_lbl.configure(text="")
            return
        try:
            e = sp.estimate({**st["pj"], "shots": miss})
            t = e.get("total_s") or 0
        except Exception:
            t = 0
        b_make.configure(text="Make the %d missing shot%s" % (len(miss), "" if len(miss) == 1
                                                              else "s"))
        est_lbl.configure(text=(("≈ %s on forge" % _fmt(t)) + (
            " · two at a time: ≈ %s" % _fmt(t / 2.0)
            if _both_cards() and len(miss) > 1 else "")) if t else "")

    def _both_cards():
        """The main window's card mode is BOTH (cards.route) — two shots at once."""
        try:
            import cards as _c
            return _c.enabled() and _c.route() == "both"
        except Exception:
            return False

    def _needs_voice(pj):
        return any((sh.get("line") or "").strip() and not (
            sh.get("audio_wav") and os.path.exists(sh["audio_wav"]))
            for sh in pj.get("shots") or [])

    def _make_missing():
        pj = st["pj"]
        if not pj or not pj.get("shots"):
            say("no shots to make yet", warn=True)
            return
        _commit_inspector()
        todo = [sh["n"] for sh in _missing()]
        if not todo:
            say("every shot is already made — Cut the episode")
            return
        st["stop"] = False

        def run(job):
            if any(sh.get("video_mode") == "set" and sh["n"] in todo for sh in pj["shots"]) \
                    and not sp.set_ready(pj):
                # THE SET first: every 'set' shot starts from one of its plates
                sp.make_set(pj, progress=lambda m: job.__setitem__("progress", m),
                            resume=True)
                job["tick"] = job.get("tick", 0) + 1
            if _needs_voice(pj):
                job["progress"] = "voicing the lines first"
                sp.voice_lines(pj)
            # BOTH cards (the card mode in the main window): two shots at once, one per card.
            # A shot that starts FROM THE LAST SHOT waits until that shot is made.
            lanes = 2 if _both_cards() else 1
            pending, running, errors = list(todo), set(), []
            lk = threading.Lock()
            made = [0]

            def ready(n):
                sh = next(x for x in pj["shots"] if x["n"] == n)
                if sh.get("video_mode") != "cont":
                    return True
                return not ({n - 1} & (set(pending) | running))

            def lane():
                while True:
                    with lk:
                        if st.get("stop") or errors or not pending:
                            return
                        pick = next((n for n in pending if ready(n)), None)
                        if pick is not None:
                            pending.remove(pick)
                            running.add(pick)
                            job["progress"] = ("shot%s %s (%d of %d made)" % (
                                "s" if len(running) > 1 else "",
                                " and ".join(str(x) for x in sorted(running)),
                                made[0], len(todo)))
                            job["tick"] = job.get("tick", 0) + 1
                    if pick is None:
                        time.sleep(0.5)               # waiting for the shot it starts from
                        continue
                    try:
                        sp.render_shot(pj, pick)
                    except Exception as exc:
                        with lk:
                            errors.append(exc)
                    finally:
                        with lk:
                            running.discard(pick)
                            made[0] += 1
                            job["tick"] = job.get("tick", 0) + 1
            ts = [threading.Thread(target=lane, daemon=True) for _ in range(lanes)]
            for t_ in ts:
                t_.start()
                time.sleep(0.2)                       # card 1 gets the first shot
            for t_ in ts:
                t_.join()
            if st.get("stop") and pending:
                job["progress"] = "stopped"
            if errors:
                raise errors[0]
            return len(todo)
        _job(run, lambda _n: (_build_board(), _paint_chips(),
                              say("done — %s" % ("every shot is made: Cut the episode"
                                                      if not _missing() else
                                                      "%d still to make" % len(_missing())))),
             "making the shots", pass_job=True, board_live=True)

    def _another_take():
        sh = _sel_shot()
        if sh is None:
            return
        _commit_inspector()
        pj, n = st["pj"], sh["n"]

        def run():
            if (sh.get("line") or "").strip() and not (sh.get("audio_wav") and
                                                       os.path.exists(sh["audio_wav"])):
                sp.voice_lines(pj)
            return sp.render_shot(pj, n)
        _job(run, lambda r: (_build_board(), say("shot %d: %s" % (n, (r or {}).get("status")))),
             "making another take of shot %d" % n)

    def _stop():
        if st["busy"]:
            st["stop"] = True
            say("will stop after the shot that is running")

    # -- CUT
    def _fill_cut():
        pj = st["pj"]
        if not pj:
            return
        miss = _missing()
        cut_note.configure(text=("%d of %d shots made — make the rest on the Shots page "
                                 "first" % (len(pj["shots"]) - len(miss), len(pj["shots"])))
                           if miss else "%d shots · 1920×1080 · voices mixed "
                                        "over each shot" % len(pj.get("shots") or []))
        m = music_menu["menu"]
        m.delete(0, "end")
        st["music_map"] = {"none": None}
        m.add_command(label="none", command=lambda: _set_music("none"))
        try:
            import music as _music                  # the MUSIC library (fills itself)
            songs = _music.library()
        except Exception:
            songs, _music = [], None
        for slug_, name_ in songs[:40]:
            label = name_[:40]
            while label in st["music_map"]:
                label += " "
            st["music_map"][label] = _music.path(slug_)
            m.add_command(label=label, command=lambda v=label: _set_music(v))
        m.add_separator()
        m.add_command(label="+ add a song\u2026", command=lambda: _add_song())
        cur = pj.get("music")
        music_var.set(next((k for k, v in st["music_map"].items() if v == cur), "none")
                      if cur else "none")
        cap_lbl.configure(text=("☑ Captions burned in" if sp.wants_captions(pj)
                                else "☐ Captions burned in") + "  (click to change)")
        for w_ in look_row.winfo_children():
            w_.destroy()
        for w_ in pace_row.winfo_children():
            w_.destroy()
        tk.Label(pace_row, text="Pace", bg=C["panel"], fg=C["faint"], font=fsm).pack(
            side="left", padx=(0, 6))
        for k_, lbl_ in sp.PACE_LABELS:
            on_ = k_ == sp.pace_of(pj)
            c_ = tk.Label(pace_row, text=lbl_, font=fsm, padx=8, pady=3, cursor="hand2",
                          bg=C["ink"] if on_ else C["panel2"],
                          fg="#14110f" if on_ else C["ink"])
            c_.pack(side="left", padx=(0, 3))
            c_.bind("<Button-1>", lambda _e, v=k_: _set_pace(v))
        if sp.wants_captions(pj):
            for k_, lbl_ in sp.CAPTION_LOOKS:
                on_ = k_ == sp.caption_look(pj)
                c_ = tk.Label(look_row, text=lbl_, font=fsm, padx=8, pady=3, cursor="hand2",
                              bg=C["ink"] if on_ else C["panel2"],
                              fg="#14110f" if on_ else C["ink"])
                c_.pack(side="left", padx=(0, 3))
                c_.bind("<Button-1>", lambda _e, v=k_: _set_caption_look(v))
        _paint_timeline()
        cut = (pj.get("cut") or {})
        img = fl.thumb_photo(cut.get("cover"), 420) if cut.get("cover") else None
        if img is not None:
            cover_lbl.configure(image=img, text="")
            cover_lbl.image = img
        else:
            cover_lbl.configure(image="", text="not cut yet" if not cut.get("mp4") else
                                os.path.basename(cut["mp4"]))
            cover_lbl.image = None

    # ------------------------------------------------------------ TIMELINE (Cut page)
    def _paint_timeline():
        """The cut as a strip: one block per shot, as wide as it runs in the cut (or its
        length before the first cut). Click one: move it earlier / later, start or end it a
        little earlier / later, or leave it out. Then Cut again."""
        for w_ in tl_box.winfo_children():
            w_.destroy()
        pj = st["pj"]
        shots = (pj or {}).get("shots") or []
        if not shots:
            return
        tk.Label(tl_box, text="TIMELINE  — click a shot to change it, then Cut again",
                 bg=C["panel"], fg=C["faint"], font=fsm, anchor="w").pack(fill="x")
        strip = tk.Frame(tl_box, bg=C["panel"])
        strip.pack(fill="x", pady=(4, 0))
        names = dict(fl.list_characters())

        def dur(sh):
            if sh.get("cut_out") is not None and sh.get("cut_in") is not None:
                return max(0.3, float(sh["cut_out"]) - float(sh["cut_in"]))
            return float(sh.get("seconds") or 3)
        total = sum(dur(sh) for sh in shots if not sh.get("skip")) or 1.0
        width = 860
        st["tl_blocks"] = {}
        for sh in shots:
            off = bool(sh.get("skip"))
            w_px = 46 if off else max(46, int(width * dur(sh) / total))
            sel = st.get("tl_sel") == sh.get("n")
            blk = tk.Frame(strip, bg=C["ochre"] if sel else (C["line"] if off else
                                                             C["panel2"]),
                           width=w_px, height=74, cursor="hand2")
            blk.pack(side="left", padx=(0, 2))
            blk.pack_propagate(False)
            tp = _thumb_path(sh)
            img = fl.thumb_photo(tp, 40) if tp and os.path.exists(tp) and not off else None
            if img is not None:
                pic = tk.Label(blk, image=img, bg=blk["bg"])
                pic.image = img
                pic.pack(pady=(4, 0))
            who = sh.get("speaker") or ""
            who = (names.get(who) or who).split(" ")[0][:8]
            t_ = tk.Label(blk, text=("%d out" % sh["n"]) if off else
                          ("%d%s" % (sh["n"], (" " + who) if w_px > 70 else "")),
                          bg=blk["bg"], fg="#14110f" if sel else C["ink"], font=fsm)
            t_.pack(side="bottom", pady=(0, 3))
            for w2 in blk.winfo_children() + [blk]:
                w2.bind("<Button-1>", lambda _e, n=sh["n"]: _tl_select(n))
            st["tl_blocks"][sh["n"]] = blk
        sh = sp._shot(pj, st["tl_sel"]) if st.get("tl_sel") is not None else None
        if not sh:
            return
        row = tk.Frame(tl_box, bg=C["panel"])
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text="Shot %d: %s" % (sh["n"], (sh.get("line") or ("(reaction)" if
                                                                     sh.get("is_reaction")
                                                                     else ""))[:60]),
                 bg=C["panel"], fg=C["ink"], font=fsm).pack(side="left", padx=(0, 10))
        for txt, fn in (("◀ earlier", lambda: _tl_move(-1)),
                        ("later ▶", lambda: _tl_move(1)),
                        ("start −0.1 s", lambda: _tl_nudge("nudge_in", -0.1)),
                        ("start +0.1 s", lambda: _tl_nudge("nudge_in", 0.1)),
                        ("end −0.1 s", lambda: _tl_nudge("nudge_out", -0.1)),
                        ("end +0.1 s", lambda: _tl_nudge("nudge_out", 0.1)),
                        ("put it back" if sh.get("skip") else "leave it out",
                         lambda: _tl_skip())):
            b_ = tk.Label(row, text=txt, bg=C["panel2"], fg=C["ink"], font=fsm, padx=8, pady=3,
                          cursor="hand2")
            b_.pack(side="left", padx=(0, 3))
            b_.bind("<Button-1>", lambda _e, f_=fn: f_())
        ni, no = float(sh.get("nudge_in") or 0), float(sh.get("nudge_out") or 0)
        if ni or no:
            tk.Label(row, text="  (start %+.1f s, end %+.1f s)" % (ni, no), bg=C["panel"],
                     fg=C["faint"], font=fsm).pack(side="left")

    def _tl_select(n):
        st["tl_sel"] = None if st.get("tl_sel") == n else n
        _paint_timeline()

    def _tl_changed(msg):
        sp.save_project(st["pj"])
        _paint_timeline()
        say("%s — Cut again to see it" % msg)

    def _tl_move(d):
        pj = st["pj"]
        shots = pj["shots"]
        i = next((k for k, s_ in enumerate(shots) if s_["n"] == st.get("tl_sel")), None)
        if i is None or not 0 <= i + d < len(shots):
            return
        shots[i], shots[i + d] = shots[i + d], shots[i]
        _tl_changed("shot %d moved %s" % (shots[i + d]["n"], "earlier" if d < 0 else "later"))

    def _tl_nudge(key, delta):
        sh = sp._shot(st["pj"], st.get("tl_sel"))
        if not sh:
            return
        sh[key] = round(max(-2.0, min(2.0, float(sh.get(key) or 0) + delta)), 2)
        _tl_changed("shot %d %s %s" % (sh["n"], "starts" if key == "nudge_in" else "ends",
                                       "earlier" if delta < 0 else "later"))

    def _tl_skip():
        sh = sp._shot(st["pj"], st.get("tl_sel"))
        if not sh:
            return
        if not sh.get("skip") and len([s_ for s_ in st["pj"]["shots"]
                                       if not s_.get("skip")]) <= 1:
            say("that's the last shot left in the cut", warn=True)
            return
        sh["skip"] = not sh.get("skip")
        _tl_changed("shot %d %s" % (sh["n"], "left out" if sh["skip"] else "put back"))

    def _add_song():
        from tkinter import filedialog
        p_ = filedialog.askopenfilename(parent=win, title="Add a song to the music library",
                                        filetypes=[("Audio", "*.mp3 *.wav *.m4a *.flac "
                                                    "*.aif *.aiff *.ogg *.aac"),
                                                   ("All files", "*")])
        if not p_:
            return
        try:
            import music as _music
            slug_ = _music.add_file(p_)
        except Exception as exc:
            say("couldn't add that song: %s" % exc, warn=True)
            return
        st["pj"]["music"] = _music.path(slug_)
        save()
        _fill_cut()
        say("added to your music \u2014 Cut again to hear it")

    def _set_music(label):
        music_var.set(label)
        st["pj"]["music"] = st.get("music_map", {}).get(label)
        save()

    def _set_pace(pace):
        st["pj"]["pace"] = pace
        save()
        _fill_cut()
        say("pace: %s \u2014 Cut again to hear it" % dict(sp.PACE_LABELS)[pace].lower())

    def _set_caption_look(look):
        st["pj"]["caption_look"] = look
        save()
        _fill_cut()
        say("captions: %s \u2014 Cut again to see it" % dict(sp.CAPTION_LOOKS)[look].lower())

    def _toggle_captions():
        pj = st["pj"]
        pj["captions"] = not sp.wants_captions(pj)
        save()
        _fill_cut()

    def _cut():
        pj = st["pj"]
        if _missing():
            say("make every shot first (%d to go)" % len(_missing()), warn=True)
            return
        _job(lambda: sp.assemble(pj), lambda _c: (_fill_cut(), _paint_chips(),
                                                  say("the episode is cut — ▶ Watch it")),
             "cutting the episode")

    def _open_cut():
        mp4 = ((st["pj"] or {}).get("cut") or {}).get("mp4")
        if not mp4 or not os.path.exists(mp4):
            say("cut it first", warn=True)
            return
        subprocess.Popen(["open", mp4])

    def _reveal_cut():
        mp4 = ((st["pj"] or {}).get("cut") or {}).get("mp4")
        if not mp4 or not os.path.exists(mp4):
            say("cut it first", warn=True)
            return
        subprocess.Popen(["open", "-R", mp4])

    # ================================================================== jobs (worker + poll)
    def _job(fn, done, label, pass_job=False, board_live=False):
        if st["busy"]:
            say("already busy: %s" % st["busy"], warn=True)
            return
        job = {"done": False, "out": None, "err": None, "t0": time.time(), "progress": "",
               "tick": 0}
        st["busy"] = label
        st["job"] = job
        b_stop.configure(state="normal" if pass_job else "disabled")
        for b_ in (b_make, b_take, b_cut, b_plan, b_write, b_pitch, b_use, b_from_dlg, b_set):
            b_.configure(state="disabled")

        def work():
            try:
                job["out"] = fn(job) if pass_job else fn()
            except Exception as exc:
                if type(exc).__name__ == "CardBusy":
                    job["card_busy"] = exc
                job["err"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["done"] = True

        last = {"tick": 0}

        def poll():
            if not job["done"]:
                say("%s … %s  %.0f s" % (label, job.get("progress") or "",
                                              time.time() - job["t0"]))
                if board_live and job["tick"] != last["tick"] and st["page"] == "Shots":
                    last["tick"] = job["tick"]
                    _build_board()
                win.after(400, poll)
                return
            st["busy"] = None
            st["job"] = None
            b_stop.configure(state="disabled")
            for b_ in (b_make, b_take, b_cut, b_plan, b_write, b_pitch, b_use, b_from_dlg, b_set):
                b_.configure(state="normal")
            try:
                import cards as _cards
                threading.Thread(target=_cards.release, args=("episode",), daemon=True).start()
            except Exception:
                _cards = None
            if job.get("card_busy") is not None:
                # a card is WORKING on something: say what, and offer wait / mine first / stop
                say("%s is busy \u2014 choose what to do"
                    % _cards.card_name(job["card_busy"].card), warn=True)
                import cards_ui
                cards_ui.handle(win, job["card_busy"], ctx, say=say, owner="episode",
                                retry=lambda: _job(fn, done, label, pass_job, board_live))
                if board_live and st["page"] == "Shots":
                    _build_board()
                return
            if job["err"]:
                _msg = _cards.explain(job["err"]) if _cards is not None else job["err"][:160]
                say("%s failed: %s" % (label, _msg), warn=True)
                if board_live and st["page"] == "Shots":
                    _build_board()
                return
            done(job["out"])
        threading.Thread(target=work, daemon=True).start()
        win.after(400, poll)

    # ================================================================== start
    _paint_cast()
    _relist_projects()
    _relist_dialogue()
    _go("Idea")
    say("pick a cast and a one-line idea — or start from a voiced Dialogue scene")
    win.bind("<FocusIn>", lambda _e: None)
    if btn_colors:
        try:
            btn_colors(win)
        except Exception:
            pass
        for b_ in (b_use, b_plan, b_make, b_cut):
            b_.configure(bg=C["ochre"], fg="#14110f")

    win.ep = {"st": st, "go": _go, "open": _open, "new": _new, "toggle_cast": _toggle_cast,
              "idea_var": idea_var, "len_var": len_var, "use_idea": _use_idea,
              "pitch": _pitch, "choose": _choose, "write_script": _write_script,
              "t_script": t_script, "plan": _plan, "cards": cards, "select": _select,
              "tl_select": _tl_select, "tl_move": _tl_move, "tl_nudge": _tl_nudge,
              "tl_skip": _tl_skip, "tl_box": tl_box, "cut": _cut,
              "t_action": t_action, "line_var": line_var, "keep": _keep,
              "set_start": _set_start, "set_secs": _set_secs, "set_camera": _set_camera,
              "cam_btns": cam_btns, "make_missing": _make_missing,
              "another_take": _another_take, "stop": _stop, "cut": _cut,
              "set_music": _set_music, "toggle_captions": _toggle_captions,
              "set_caption_look": _set_caption_look, "look_row": look_row,
              "set_pace": _set_pace, "pace_row": pace_row, "set_place": _set_place,
              "place_var": place_var,
              "paint_set": _paint_set, "rebuild_set": _rebuild_set, "where_var": where_var,
              "where_done": _where_done, "set_note": set_note, "set_pics": set_pics,
              "b_set": b_set,
              "music_map": lambda: st.get("music_map", {}), "dlg_var": dlg_var,
              "from_dialogue": _from_dialogue, "relist_dialogue": _relist_dialogue,
              "chips": chips, "pages": pages, "status": status,
              "buttons": {"make": b_make, "stop": b_stop, "to_cut": b_to_cut, "take": b_take,
                          "cut": b_cut, "use": b_use, "pitch": b_pitch, "plan": b_plan,
                          "from_dlg": b_from_dlg},
              "bar": bar, "insp": insp, "legacy": (_lg1, _lg2)}
    return win
