"""The CHARACTERS workspace, redesigned (2026-10-17) — and WEIRDO folded into it.

Ryan: "the dialogue, song, characters, long video, studio, and weirdo sections need some work
… simplify and enhance the experience". The old Characters window was a six-step scrolling
column (27 controls, NEW twice, BUILD PROMPT, the name asked twice, a SEED that did nothing);
Weirdo was a separate window whose rolls could never be kept as characters.

This window is three columns that fit without scrolling:

    YOUR CAST            NAME + THE STILL / CLIP          LOOK | BODY | IN MOTION | ADVANCED
    + New character      [ big preview ]                  the picks, the sliders, the action,
    Surprise me          Make still · ~89 s  Draft|Fast|Full     seed / LoRA / delete
    (vein)               Make a 5 s test clip
    gustav …             Use in Video

* SAVED AUTOMATICALLY. Nothing to remember: once a character has a name or a still, every
  change is written (debounced). A brand-new character with neither is not saved, so rerolling
  Surprise me never litters the cast.
* SURPRISE ME = the old Weirdo roll (normal in every axis, ONE thing broken), landing in this
  editor as an unsaved character — render it, test-clip it, and it is a real character.
* THE PROMPT FOLLOWS THE PICKS until you edit it by hand; then it is yours, and "Reset to the
  picks" (not a BUILD PROMPT button) puts it back.

Everything rig-facing goes through the forge_lab MODULE passed as ctx["mod"] (submit,
run_state, collect, compose_character_prompt, write_character …) and is looked up AT CALL TIME,
so drivers can stub the rig. ⚠️ Never `import forge_lab` here: run as a script the app is
module __main__, and an import would load a SECOND copy with its own globals.
"""
import os
import sys
import subprocess
import tempfile
import threading
import time

import dispatch as _dsp          # the rig's queue (Tk-free): every render waits its turn
EST_IMAGE = 45                  # seconds — what the QUEUE row quotes for one still

# macOS draws its own white buttons and ignores bg: light text on them vanished
# ("Stop after this step" was blank on the MacBook) - dark text there, always
_MAC = sys.platform == "darwin"

NEW_HINT = "a new character — pick a look, then Make still"



def weirdo_picks(spec):
    """A Weirdo's own look IN THE PICK BOXES. Only Style + Framing used to be set, so every
    other box fell back to its default ("a middle-aged man", "a plain grey cable-knit
    cardigan…", "short dark hair") — Weirdo looked like it changed nothing, and those
    defaults were what got saved as the character's picks. Hair/skin/facial hair are left
    blank: the invented subject + look already say them, and a default would contradict."""
    subj = (spec.get("subject") or "").strip()
    look = (spec.get("look") or "").strip().rstrip(".")
    return {"Style": "photorealistic",
            "Framing": "full body shot, the whole figure head to feet in frame",
            "Subject": ("%s, %s" % (subj, look)) if look else subj,
            "Skin": "", "Hair": "", "Facial hair": "",
            "Wardrobe": (spec.get("wardrobe") or "").strip(),
            "Accessory": "nothing extra",
            "Feature": (spec.get("feature") or "").strip().rstrip("."),
            "Setting": (spec.get("setting") or "").strip()}

def open_characters2(root, ctx):
    import tkinter as tk
    import tkinter.font as tkfont

    fl = ctx["mod"]
    C, F, mono = ctx["C"], ctx["fonts"], ctx["mono"]
    f, fh, fsm = F["f"], F["fh"], F["fsm"]
    fmono = mono["m"]
    load_into_form = ctx.get("load_character")
    btn_colors = ctx.get("btn_colors")
    fbig = tkfont.Font(family=fh.cget("family"), size=max(15, int(fh.cget("size")) + 5),
                       weight="bold")

    import dock as _dock                 # a tab in the main window (Ryan: tabs)
    win = _dock.window(root)
    win.title("Characters")
    win.configure(bg=C["bg"])
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1220, _sw - 40), min(800, _sh - 110)))
    win.minsize(1060, 700)

    cur = {"slug": None, "still": None, "seed_used": None, "clip": None, "frames": [],
           "play": None, "last_composed": "", "save_job": None, "busy": None,
           "loading": False, "tab": "Look", "del_armed": None,
           # ⚠️ every press of a render button is its OWN job now (Ryan, 2026-10-22): the window
           # counts what is in flight instead of refusing the second one. `busy` stays as the
           # newest job's kind, so the rest of the window reads the way it always did.
           "jobs": [], "fitting": 0}

    # ------------------------------------------------------------------ small helpers
    def lab(parent, text, fg=None, font=None, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["muted"],
                        font=font or fsm, anchor="w", **kw)

    def button(parent, text, cmd, primary=False, **kw):
        b = tk.Button(parent, text=text, command=cmd, relief="flat", borderwidth=0,
                      font=fh if primary else fsm, padx=12, pady=8 if primary else 5,
                      bg=C["ochre"] if primary else C["panel2"],
                      fg="#14110f" if primary or _MAC else C["ink"],
                      activebackground="#d9b45e" if primary else C["rust"],
                      activeforeground="#14110f", highlightthickness=0, **kw)
        return b

    def say(msg, warn=False):
        status.configure(text=msg, fg=C["ochre"] if warn else C["muted"])

    # ------------------------------------------------------------------ header
    hd = tk.Frame(win, bg=C["panel"])
    hd.pack(fill="x")
    tk.Label(hd, text="Characters", bg=C["panel"], fg=C["ink"], font=fbig).pack(
        side="left", padx=(16, 12), pady=10)
    status = tk.Label(hd, text="", bg=C["panel"], fg=C["muted"], font=fsm, anchor="w")
    status.pack(side="left", fill="x", expand=True)
    button(hd, "Close", lambda: _close()).pack(side="right", padx=12)
    tk.Frame(win, bg=C["line"], height=1).pack(fill="x")

    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ left: the cast
    # Ryan 2026-10-22: "make the left bar of the characters page scrollable, I can barely see
    # the your cast section". The whole column now scrolls as ONE (the tools on top, then the
    # cast, inline); cast_cv is that column's canvas. It used to be a second canvas squeezed
    # into whatever height the invent tools left over.
    nav_outer = tk.Frame(body, bg=C["panel"], width=252)
    nav_outer.pack(side="left", fill="y")
    nav_outer.pack_propagate(False)
    cast_sb = tk.Scrollbar(nav_outer, orient="vertical")
    cast_sb.pack(side="right", fill="y")
    cast_cv = tk.Canvas(nav_outer, bg=C["panel"], highlightthickness=0, borderwidth=0,
                        yscrollcommand=cast_sb.set)
    cast_cv.pack(side="left", fill="both", expand=True)
    cast_sb.configure(command=cast_cv.yview)
    nav = tk.Frame(cast_cv, bg=C["panel"])
    _nav_win = cast_cv.create_window(0, 0, window=nav, anchor="nw")
    nav.bind("<Configure>", lambda _e: cast_cv.configure(scrollregion=cast_cv.bbox("all")))
    cast_cv.bind("<Configure>", lambda e: cast_cv.itemconfigure(_nav_win, width=e.width))
    b_new = button(nav, "+ New character", lambda: _new(), primary=True)
    b_new.pack(fill="x", padx=12, pady=(14, 6))
    # FROM A PHOTO (Higgsfield's "Soul ID"): any picture becomes a character — its look is
    # the picture; Angles draws them from every side, Voice gives them a voice.
    b_photo = button(nav, "From a photo\u2026", lambda: _from_photo())
    b_photo.pack(fill="x", padx=12, pady=(0, 6))
    b_surprise = button(nav, "Surprise me", lambda: _surprise())
    b_surprise.pack(fill="x", padx=12)
    vrow = tk.Frame(nav, bg=C["panel"])
    vrow.pack(fill="x", padx=12, pady=(4, 0))
    lab(vrow, "flavour").pack(side="left")
    vein_var = tk.StringVar(value=fl.WEIRDO_VEIN_DEFAULT)
    vein_menu = tk.OptionMenu(vrow, vein_var, *fl.WEIRDO_VEINS)
    vein_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, borderwidth=0, activebackground=C["rust"])
    vein_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    vein_menu.pack(side="left", padx=(6, 0))
    lab(nav, "a normal person with ONE thing wrong", fg=C["faint"], wraplength=206,
        justify="left").pack(fill="x", padx=12, pady=(2, 10))
    # WEIRDO (2026-10-17): the rig's writer invents a WHOLE funny character — who, the one
    # ridiculous gag, the outfit and place, a looping bit, a catchphrase, the voice — in the
    # shape Gustav / Ludvig were built in, opened up to any world (weirdos.py).
    b_weirdo = button(nav, "Weirdo \u2726 invent one", lambda: _weirdo(), primary=True)
    b_weirdo.pack(fill="x", padx=12)
    wrow = tk.Frame(nav, bg=C["panel"])
    wrow.pack(fill="x", padx=12, pady=(4, 0))
    import weirdos as _wd
    wflav_var = tk.StringVar(value=_wd.FLAVOURS[0][0])
    wflav_menu = tk.OptionMenu(wrow, wflav_var, *[f_ for f_, _d in _wd.FLAVOURS])
    wflav_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, width=18,
                         anchor="w", highlightthickness=0, borderwidth=0,
                         activebackground=C["rust"])
    wflav_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    wflav_menu.pack(side="left", fill="x", expand=True)
    lab(nav, "funny, ironic, ridiculous: a gag, a bit, a line. Uses the words you type "
             "under Look.", fg=C["faint"], wraplength=206, justify="left").pack(
        fill="x", padx=12, pady=(2, 10))
    # IN A STYLE (2026-10-18): a character in the style of a creator Ryan admires
    # (char_styles.py: gilded satire, genre trailer). The craft, never their characters.
    import char_styles as _cs
    b_styled = button(nav, "In a style \u2726 invent one", lambda: _styled())
    b_styled.pack(fill="x", padx=12)
    srow = tk.Frame(nav, bg=C["panel"])
    srow.pack(fill="x", padx=12, pady=(4, 0))
    style_var = tk.StringVar(value=_cs.DEFAULT)
    role_var = tk.StringVar(value=_cs.roles(_cs.DEFAULT)[0])
    style_menu = tk.OptionMenu(srow, style_var, *list(_cs.STYLES))
    role_menu = tk.OptionMenu(srow, role_var, *_cs.roles(_cs.DEFAULT))
    for _m in (style_menu, role_menu):
        _m.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, width=18,
                     anchor="w", highlightthickness=0, borderwidth=0,
                     activebackground=C["rust"])
        _m["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
        _m.pack(fill="x", pady=(0, 2))
    style_hint = lab(nav, _cs.style(_cs.DEFAULT)["hint"], fg=C["faint"], wraplength=206,
                     justify="left")
    style_hint.pack(fill="x", padx=12, pady=(2, 10))

    def _style_changed(*_a):
        """Another style: its own roles in the role box, its own hint."""
        m_ = role_menu["menu"]
        m_.delete(0, "end")
        for r_ in _cs.roles(style_var.get()):
            m_.add_command(label=r_, command=lambda v=r_: role_var.set(v))
        role_var.set(_cs.roles(style_var.get())[0])
        style_hint.configure(text=_cs.style(style_var.get())["hint"])
    style_var.trace_add("write", _style_changed)
    lab(nav, "YOUR CAST", font=fmono).pack(fill="x", padx=14, pady=(4, 4))
    # FIND + a SCROLLING list: with 15+ characters the list ran off the bottom and could not
    # be scrolled (Ryan, looking for Gerald and Vivian)
    find_var = tk.StringVar(value="")
    find_ent = tk.Entry(nav, textvariable=find_var, bg="#100e0c", fg=C["ink"], font=fsm,
                        relief="flat", insertbackground=C["ink"])
    find_ent.pack(fill="x", padx=12, pady=(0, 4), ipady=3)
    find_ent.insert(0, "")
    lab(nav, "type to find someone", fg=C["faint"]).pack(fill="x", padx=14)
    cast_box = tk.Frame(nav, bg=C["panel"])
    cast_box.pack(fill="x", padx=6, pady=(0, 12))
    try:
        fl.scoped_wheel(win, cast_cv)     # the wheel anywhere over the column scrolls it
    except Exception:
        pass
    cast_rows = {}          # slug -> (row, [widgets], thumb image)
    cast_x = {}             # slug -> its ✕ delete label

    # ------------------------------------------------------------------ centre: name + still
    mid = tk.Frame(body, bg=C["bg"], width=430)
    mid.pack(side="left", fill="y", padx=(0, 0))
    mid.pack_propagate(False)
    lab(mid, "Name").pack(fill="x", padx=18, pady=(14, 2))
    name_var = tk.StringVar()
    e_name = tk.Entry(mid, textvariable=name_var, bg="#100e0c", fg=C["ink"], relief="flat",
                      font=fbig, insertbackground=C["ink"])
    e_name.pack(fill="x", padx=18, ipady=4)
    bit_lbl = tk.Label(mid, text="", bg=C["bg"], fg=C["ochre"], font=fsm, anchor="w",
                       justify="left", wraplength=390)
    bit_lbl.pack(fill="x", padx=18, pady=(4, 0))
    vt = tk.Frame(mid, bg=C["bg"])
    vt.pack(fill="x", padx=18, pady=(10, 4))
    view_btns = {}
    for _v in ("Still", "Clip"):
        _l = tk.Label(vt, text=_v, bg=C["bg"], fg=C["muted"], font=fsm, padx=10, pady=3,
                      cursor="hand2")
        _l.pack(side="left")
        _l.bind("<Button-1>", lambda _e, v=_v: _show(v))
        view_btns[_v] = _l
    # still quality sits up here beside Still|Clip, so Make still gets the whole row below
    # (side by side they needed ~410 px at macOS font widths in a 394 px column)
    q_var = tk.StringVar(value="Full")
    qbox = tk.Frame(vt, bg=C["line"])
    qbox.pack(side="right")
    q_btns = {}
    for _q, _ in fl.CHAR_STILL_SCALE[::-1]:
        _l = tk.Label(qbox, text=_q, bg=C["panel"], fg=C["ink"], font=fsm, padx=8, pady=3,
                      cursor="hand2")
        _l.pack(side="left", padx=(0, 1))
        _l.bind("<Button-1>", lambda _e, q=_q: (q_var.set(q), _paint_q(), _refresh_est()))
        q_btns[_q] = _l
    prev = tk.Label(mid, bg="#100e0c", fg=C["faint"], font=fsm, text="no still yet",
                    width=1, height=1, cursor="hand2")
    prev.pack(fill="both", expand=True, padx=18)
    prev.bind("<Button-1>", lambda _e: _preview_click())
    prow = tk.Frame(mid, bg=C["bg"])
    prow.pack(fill="x", padx=18, pady=(10, 0))
    b_still = button(prow, "Make still", lambda: _make_still(), primary=True)
    b_still.pack(side="left", fill="x", expand=True)
    b_clip = button(mid, "Make a 5 s test clip of them", lambda: _make_clip())
    b_clip.pack(fill="x", padx=18, pady=(8, 0))
    urow = tk.Frame(mid, bg=C["bg"])
    urow.pack(fill="x", padx=18, pady=(8, 14))
    lab(urow, "Use in").pack(side="left")
    b_use_video = button(urow, "Video", lambda: _use_in_video())
    b_use_video.pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------------ right: tabs
    right = tk.Frame(body, bg=C["panel"])
    right.pack(side="left", fill="both", expand=True)
    tabbar = tk.Frame(right, bg=C["panel"])
    tabbar.pack(fill="x", padx=16, pady=(10, 0))
    tk.Frame(right, bg=C["line"], height=1).pack(fill="x", padx=16)
    pages = {}
    tab_lbls = {}
    for _t in ("Look", "Body", "In motion", "Angles", "Voice", "Advanced"):
        _l = tk.Label(tabbar, text=_t, bg=C["panel"], fg=C["muted"], font=fsm, padx=12,
                      pady=7, cursor="hand2")
        _l.pack(side="left")
        _l.bind("<Button-1>", lambda _e, t=_t: _tab(t))
        tab_lbls[_t] = _l
        pages[_t] = tk.Frame(right, bg=C["panel"])

    # -- LOOK: the categorical picks in two columns, then the prompt fold
    pk_vars = {}
    pk_menus = {}
    pk_entries = {}
    look = pages["Look"]
    grid = tk.Frame(look, bg=C["panel"])
    grid.pack(fill="x", padx=16, pady=(10, 0))
    grid.columnconfigure(0, weight=1, uniform="pk")
    grid.columnconfigure(1, weight=1, uniform="pk")
    for _i, (_axis, _vals) in enumerate(fl.CHAR_OPTIONS):
        cell = tk.Frame(grid, bg=C["panel"])
        cell.grid(row=_i // 2, column=_i % 2, sticky="ew", padx=(0, 10), pady=3)
        lab(cell, _axis).pack(fill="x")
        _var = tk.StringVar(value=fl.CHAR_OPTION_DEFAULTS.get(_axis, ""))
        # TYPE OR PICK (2026-10-17, Ryan: "i want the option to type in the dropdowns ... in
        # case I think of something else"): every pick is a text box — whatever is typed goes
        # into the prompt as-is — with a ▾ beside it that opens the list (submenus for the
        # long axes) and drops the choice into the box.
        _row = tk.Frame(cell, bg=C["panel"])
        _row.pack(fill="x")
        _e = tk.Entry(_row, textvariable=_var, bg=C["panel2"], fg=C["ink"], relief="flat",
                      font=fsm, insertbackground=C["ink"], width=1, highlightthickness=1,
                      highlightbackground=C["line"], highlightcolor=C["ochre"])
        _e.pack(side="left", fill="x", expand=True, ipady=3)
        _e.bind("<KeyRelease>", lambda _ev: _typed())
        _e.bind("<Return>", lambda _ev: _on_pick())
        _e.bind("<FocusOut>", lambda _ev: _typed(now=True))
        _m = tk.Menubutton(_row, text="\u25be", relief="flat", font=fsm, bg=C["ink"],
                           fg="#14110f", highlightthickness=0, borderwidth=0, padx=7,
                           activebackground=C["ochre"], cursor="hand2")
        _m.pack(side="left", fill="y")
        _top = tk.Menu(_m, tearoff=0, bg=C["panel2"], fg="#000000", font=fsm)
        _grp = None
        try:
            import char_vocab as _cv
            _grp = _cv.groups(_axis, _vals)
        except Exception:
            _grp = None
        if _grp:
            # a LONG axis (Subject has 130+): one submenu per group, not an 80-row popup
            for _gname, _gvals in _grp:
                _sub = tk.Menu(_top, tearoff=0, bg=C["panel2"], fg="#000000", font=fsm)
                for _v in _gvals:
                    _sub.add_command(label=_v or "(none)",
                                     command=lambda v=_v, var=_var: (var.set(v), _on_pick()))
                _top.add_cascade(label="%s  (%d)" % (_gname, len(_gvals)), menu=_sub)
            _m.cards_groups = _grp                 # drivers read the grouping
        else:
            for _v in _vals:
                _top.add_command(label=_v or "(none)",
                                 command=lambda v=_v, var=_var: (var.set(v), _on_pick()))
        _m.configure(menu=_top)
        pk_menus[_axis] = _m
        pk_entries[_axis] = _e
        pk_vars[_axis] = _var
    lab(look, "Describe them (optional) — Surprise me fits its roll to these words",
        ).pack(fill="x", padx=16, pady=(10, 2))
    t_desc = tk.Text(look, height=2, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                     font=fsm, insertbackground=C["ink"], padx=6, pady=4)
    t_desc.pack(fill="x", padx=16)
    pfold = tk.Frame(look, bg=C["panel"])
    pfold.pack(fill="x", padx=16, pady=(10, 0))
    l_prompt_toggle = tk.Label(pfold, text="", bg=C["panel"], fg=C["ochre"], font=fsm,
                               cursor="hand2", anchor="w")
    l_prompt_toggle.pack(side="left")
    l_reset = tk.Label(pfold, text="Reset to the picks", bg=C["panel"], fg=C["ochre"],
                       font=fsm, cursor="hand2")
    l_reset.bind("<Button-1>", lambda _e: _reset_prompt())
    t_prompt = tk.Text(look, height=5, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                       font=fsm, insertbackground=C["ink"], padx=6, pady=4)
    l_prompt_toggle.bind("<Button-1>", lambda _e: _toggle_prompt())
    cur["prompt_open"] = False

    # -- BODY: the sliders in two columns
    sl_vars = {}
    sl_phr = {}
    bodyp = pages["Body"]
    lab(bodyp, "Drag a slider away from the middle to exaggerate it. The middle is ordinary.",
        fg=C["faint"]).pack(fill="x", padx=16, pady=(10, 4))
    sgrid = tk.Frame(bodyp, bg=C["panel"])
    sgrid.pack(fill="x", padx=16)
    sgrid.columnconfigure(0, weight=1, uniform="sl")
    sgrid.columnconfigure(1, weight=1, uniform="sl")
    for _i, _axis in enumerate(fl.CHAR_SLIDER_ORDER):
        cell = tk.Frame(sgrid, bg=C["panel"])
        cell.grid(row=_i // 2, column=_i % 2, sticky="ew", padx=(0, 12), pady=1)
        top_ = tk.Frame(cell, bg=C["panel"])
        top_.pack(fill="x")
        lab(top_, _axis, fg=C["ink"]).pack(side="left")
        _p = lab(top_, "", fg=C["faint"])
        _p.pack(side="right")
        _var = tk.IntVar(value=0)
        _s = tk.Scale(cell, from_=-3, to=3, orient="horizontal", variable=_var, showvalue=0,
                      bg=C["panel"], fg=C["ink"], troughcolor="#100e0c", highlightthickness=0,
                      borderwidth=0, sliderlength=18, width=10,
                      command=lambda _v, a=_axis: _on_slide(a))
        _s.pack(fill="x")
        sl_vars[_axis] = _var
        sl_phr[_axis] = _p
    button(bodyp, "Back to ordinary", lambda: _reset_sliders()).pack(anchor="w", padx=16,
                                                                     pady=(8, 0))

    # -- IN MOTION: the action + clip settings
    mo = pages["In motion"]
    lab(mo, "What they do in a video (used by the test clip and by Use in Video)").pack(
        fill="x", padx=16, pady=(12, 2))
    t_action = tk.Text(mo, height=3, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                       font=fsm, insertbackground=C["ink"], padx=6, pady=4)
    t_action.pack(fill="x", padx=16)
    mrow = tk.Frame(mo, bg=C["panel"])
    mrow.pack(fill="x", padx=16, pady=(12, 0))
    lab(mrow, "Clip length").pack(side="left")
    len_var = tk.StringVar(value=fl.WEIRDO_VIDEO_LENGTH_DEFAULT)
    _lm = tk.OptionMenu(mrow, len_var, *[k for k, _ in fl.WEIRDO_VIDEO_LENGTHS],
                        command=lambda *_a: _sync_clip_label())
    _lm.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, activebackground=C["rust"])
    _lm["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _lm.pack(side="left", padx=(6, 16))
    lab(mrow, "Shape").pack(side="left")
    canvas_var = tk.StringVar(value=fl.WEIRDO_VIDEO_CANVAS_DEFAULT)
    _cm = tk.OptionMenu(mrow, canvas_var, *[k for k, _ in fl.WEIRDO_VIDEO_CANVAS])
    _cm.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, activebackground=C["rust"])
    _cm["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _cm.pack(side="left", padx=(6, 0))

    # -- ANGLES: the character sheet — the same person from every side (charpack). Every
    # video of this character takes these as its references (studio_pipeline._character_refs
    # -> charpack.pack_refs), so the face and the outfit stop drifting between shots.
    angp = pages["Angles"]
    ang_head = lab(angp, "", wraplength=470, justify="left")
    ang_head.pack(fill="x", padx=16, pady=(12, 6))
    arow_ = tk.Frame(angp, bg=C["panel"])
    arow_.pack(fill="x", padx=16)
    b_sheet = button(arow_, "Make the sheet", lambda: _make_sheet(), primary=True)
    b_sheet.pack(side="left")
    b_back = button(arow_, "+ back view", lambda: _make_sheet(["back-three-quarter"]))
    b_back.pack(side="left", padx=8)
    ang_grid = tk.Frame(angp, bg=C["panel"])
    ang_grid.pack(fill="both", expand=True, padx=16, pady=(10, 0))
    for _i in range(4):
        ang_grid.columnconfigure(_i, weight=1, uniform="ang")

    # -- VOICE: who they sound like (voicelab). One click designs one from the character;
    # the Voices page auditions three, clones, tests in Fish and fine-tunes.
    vop = pages["Voice"]
    vo_head = lab(vop, "", wraplength=470, justify="left", fg=C["ink"], font=fh)
    vo_head.pack(fill="x", padx=16, pady=(14, 2))
    vo_sub = lab(vop, "", wraplength=470, justify="left")
    vo_sub.pack(fill="x", padx=16)
    vrow_ = tk.Frame(vop, bg=C["panel"])
    vrow_.pack(fill="x", padx=16, pady=(12, 0))
    b_vplay = button(vrow_, "\u25b6 Hear it", lambda: _play_voice())
    b_auto_v = button(vrow_, "Give them a voice", lambda: _auto_voice(), primary=True)
    b_open_v = button(vrow_, "Fine-tune on the Voices page \u2192", lambda: _open_voices())
    lab(vop, "One click designs a voice from their description and keeps it. The Voices "
             "page auditions three, adds an accent, clones a recording, or tests it in "
             "Fish.", fg=C["faint"], wraplength=470, justify="left").pack(
        fill="x", padx=16, pady=(14, 0))

    # -- ADVANCED: seed, LoRA, delete
    adv = pages["Advanced"]
    arow = tk.Frame(adv, bg=C["panel"])
    arow.pack(fill="x", padx=16, pady=(12, 0))
    lab(arow, "Seed").pack(side="left")
    seed_var = tk.StringVar(value="-1")
    e_seed = tk.Entry(arow, textvariable=seed_var, width=12, bg="#100e0c", fg=C["ink"],
                      relief="flat", font=fmono, insertbackground=C["ink"])
    e_seed.pack(side="left", padx=(6, 8))
    l_seed_note = lab(arow, "-1 = a new face each still", fg=C["faint"])
    l_seed_note.pack(side="left")
    lrow = tk.Frame(adv, bg=C["panel"])
    lrow.pack(fill="x", padx=16, pady=(10, 0))
    lab(lrow, "LoRA").pack(side="left")
    lora_var = tk.StringVar(value=fl.CHAR_NONE)
    _lo = tk.OptionMenu(lrow, lora_var, *list(fl.CHARACTER_LORAS.keys()),
                        command=lambda *_a: _changed())
    _lo.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, activebackground=C["rust"])
    _lo["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _lo.pack(side="left", padx=(6, 10))
    lab(lrow, "strength").pack(side="left")
    lstr_var = tk.StringVar(value="1.0")
    tk.Entry(lrow, textvariable=lstr_var, width=5, bg="#100e0c", fg=C["ink"], relief="flat",
             font=fmono, insertbackground=C["ink"]).pack(side="left", padx=6)
    drow = tk.Frame(adv, bg=C["panel"])
    drow.pack(fill="x", padx=16, pady=(22, 0))
    b_dup = button(drow, "Duplicate", lambda: _duplicate())
    b_dup.pack(side="left")
    b_del = button(drow, "Delete…", lambda: _delete())
    b_del.pack(side="left", padx=(8, 0))
    lab(adv, "Delete asks once more on the button itself — no pop-up.",
        fg=C["faint"]).pack(fill="x", padx=16, pady=(6, 0))

    # ================================================================== state <-> widgets
    def picks():
        out = {a: v.get() for a, v in pk_vars.items()}
        for a, v in sl_vars.items():
            if int(v.get()):
                out[a] = int(v.get())
        return out

    def prompt_text():
        return t_prompt.get("1.0", "end").strip()

    def action_text():
        return t_action.get("1.0", "end").strip()

    def prompt_edited():
        return bool(prompt_text()) and prompt_text() != cur["last_composed"].strip()

    def seed_box():
        try:
            v = int((seed_var.get() or "").strip())
        except ValueError:
            return -1
        return v if v >= 0 else -1

    def _compose(force=False):
        new = fl.compose_character_prompt(picks())
        if force or not prompt_edited():
            t_prompt.delete("1.0", "end")
            t_prompt.insert("1.0", new)
        cur["last_composed"] = new
        _paint_prompt_fold()

    def _paint_prompt_fold():
        ed_ = prompt_edited()
        l_prompt_toggle.configure(text=("Hide the prompt ▴" if cur["prompt_open"] else
                                        "Show the prompt it builds ▾")
                                  + ("   (edited by you)" if ed_ else ""))
        if ed_:
            l_reset.pack(side="left", padx=(12, 0))
        else:
            l_reset.pack_forget()

    def _toggle_prompt():
        cur["prompt_open"] = not cur["prompt_open"]
        if cur["prompt_open"]:
            t_prompt.pack(fill="x", padx=16, pady=(4, 0))
        else:
            t_prompt.pack_forget()
        _paint_prompt_fold()

    def _reset_prompt():
        _compose(force=True)
        _changed()

    def _typed(now=False):
        """Typing in a pick: re-compose ~0.5 s after the last key (or at once on leaving
        the box), so the prompt follows without a rebuild on every keystroke."""
        if cur.get("type_job"):
            try:
                win.after_cancel(cur["type_job"])
            except Exception:
                pass
            cur["type_job"] = None
        if now:
            _on_pick()
        else:
            cur["type_job"] = win.after(500, _on_pick)

    def _on_pick():
        cur["type_job"] = None
        if cur["loading"]:
            return
        reset = _adult_conflict(pk_vars["Subject"].get(), pk_vars["Wardrobe"].get())
        if reset:
            # ADULTS ONLY: swimwear / lingerie never go on a minor — undo the outfit
            cur["loading"] = True
            pk_vars["Wardrobe"].set(fl.CHAR_OPTION_DEFAULTS["Wardrobe"])
            cur["loading"] = False
        _compose()
        _changed()
        if reset:                  # after _changed(), so its "not saved yet" can't hide it
            say("swimwear and lingerie are for adult characters only \u2014 outfit reset",
                warn=True)

    def _adult_conflict(a, b=None):
        try:
            import char_vocab as _cv
            return _cv.conflict(a, b)
        except Exception:
            return False

    def _on_slide(axis):
        ph = fl.char_slider_phrase(picks(), axis)
        sl_phr[axis].configure(text=ph or fl.CHAR_SLIDER_NEUTRAL)
        if cur["loading"]:
            return
        _compose()
        _changed()

    def _reset_sliders():
        cur["loading"] = True
        try:
            for a, v in sl_vars.items():
                v.set(0)
                sl_phr[a].configure(text=fl.CHAR_SLIDER_NEUTRAL)
        finally:
            cur["loading"] = False
        _compose()
        _changed()

    def _set_fields(pk, prompt=None, action="", name="", seed="-1", lora=None, lstr="1.0"):
        cur["loading"] = True
        try:
            pk = pk or {}
            for a, v in pk_vars.items():
                v.set(pk.get(a) if pk.get(a) is not None else fl.CHAR_OPTION_DEFAULTS.get(a, ""))
            for a, v in sl_vars.items():
                v.set(fl.char_slider_level(pk, a))
                sl_phr[a].configure(text=fl.char_slider_phrase(pk, a) or fl.CHAR_SLIDER_NEUTRAL)
            cur["last_composed"] = fl.compose_character_prompt(picks())
            t_prompt.delete("1.0", "end")
            t_prompt.insert("1.0", prompt if prompt else cur["last_composed"])
            t_action.delete("1.0", "end")
            t_action.insert("1.0", action or "")
            name_var.set(name or "")
            seed_var.set(str(seed if seed not in (None, "") else "-1"))
            lora_var.set(lora if lora in fl.CHARACTER_LORAS else
                         next((k for k, v in fl.CHARACTER_LORAS.items() if v == lora and lora),
                              fl.CHAR_NONE))
            lstr_var.set(str(lstr or "1.0"))
        finally:
            cur["loading"] = False
        _paint_prompt_fold()

    # ================================================================== the preview
    def _paint_view(which):
        for v, l_ in view_btns.items():
            on = v == which
            enabled = v == "Still" or bool(cur["frames"])
            l_.configure(bg=C["panel2"] if on else C["bg"],
                         fg=(C["ink"] if on else (C["muted"] if enabled else C["faint"])))

    def _show(which):
        if which == "Clip" and not cur["frames"]:
            say("no test clip yet — Make a 5 s test clip first", warn=True)
            return
        cur["view"] = which
        _stop_play()
        if which == "Still":
            _show_still()
        else:
            _start_play()
        _paint_view(which)

    def _show_still():
        path = cur["still"]
        img = fl.thumb_photo(path, 420) if path else None
        if img is None:
            prev.configure(image="", text=("no still yet" if not path else
                                          "could not show %s" % os.path.basename(path)))
            prev.image = None
        else:
            prev.configure(image=img, text="")
            prev.image = img

    def _stop_play():
        if cur["play"]:
            try:
                win.after_cancel(cur["play"])
            except Exception:
                pass
        cur["play"] = None

    def _start_play(i=0):
        fr = cur["frames"]
        if not fr:
            return
        img = fr[i % len(fr)]
        prev.configure(image=img, text="")
        prev.image = img
        cur["play"] = win.after(83, lambda: _start_play(i + 1))

    def _preview_click():
        if cur.get("view") == "Clip":
            if cur["play"]:
                _stop_play()
            else:
                _start_play()
            return
        if cur["still"]:
            _enlarge(cur["still"])

    def _enlarge(path):
        top = tk.Toplevel(win)
        top.title(os.path.basename(path))
        top.configure(bg=C["bg"])
        img = fl.thumb_photo(path, max(400, min(win.winfo_screenheight() - 160, 1400)))
        l_ = tk.Label(top, image=img, bg=C["bg"], cursor="hand2")
        l_.image = img
        l_.pack(padx=8, pady=8)
        l_.bind("<Button-1>", lambda _e: top.destroy())
        top.bind("<Escape>", lambda _e: top.destroy())

    # ================================================================== the cast list
    def _cast_paint():
        for slug, (row, ws, _img) in cast_rows.items():
            on = slug == cur["slug"]
            bg = "#2a221b" if on else C["panel"]
            row.configure(bg=bg, highlightbackground=C["ochre"] if on else C["panel"])
            for w_ in ws:
                w_.configure(bg=bg)
            x_ = cast_x.get(slug)
            if x_ is not None:
                armed = cur.get("row_del") == slug
                x_.configure(bg=C["rust_deep"] if armed else bg,
                             fg="#fff" if armed else C["faint"],
                             text="Delete?" if armed else "\u2715")

    def _row_delete(slug, x_=None):
        """✕ on a cast row: the first click arms it, the second (within 4 s) deletes."""
        if cur.get("row_del") != slug:
            cur["row_del"] = slug
            _cast_paint()
            nm_ = (fl.read_character(slug) or {}).get("name") or slug
            say("click Delete? again to delete %s" % nm_)
            win.after(4000, lambda s=slug: (
                cur.get("row_del") == s and (cur.__setitem__("row_del", None),
                                             _cast_paint())))
            return False
        cur["row_del"] = None
        return _delete_slug(slug)

    def _delete_slug(slug):
        """Delete one character by slug — the open one or any other in the list."""
        nm_ = (fl.read_character(slug) or {}).get("name") or slug
        was_open = slug == cur["slug"]
        if was_open and cur.get("save_job"):
            try:                       # a pending autosave would write it straight back
                win.after_cancel(cur["save_job"])
            except Exception:
                pass
            cur["save_job"] = None
        try:
            fl.delete_character(slug)
        except Exception as exc:
            say("could not delete: %s" % exc, warn=True)
            return False
        if was_open:
            cur["slug"] = None
            cur["gen"] = cur.get("gen", 0) + 1     # a different character now
            cur["del_armed"] = None
            b_del.configure(text="Delete…")
            _relist()
            _new()
        else:
            _relist()
        say("deleted %s" % nm_)
        return True

    def _relist(*_a):
        for w_ in cast_box.winfo_children():
            w_.destroy()
        cast_rows.clear()
        cast_x.clear()
        q = find_var.get().strip().lower()
        for slug, name in fl.list_characters():
            if q and q not in name.lower() and q not in slug.lower():
                continue
            meta = fl.read_character(slug) or {}
            row = tk.Frame(cast_box, bg=C["panel"], cursor="hand2", highlightthickness=1,
                           highlightbackground=C["panel"])
            row.pack(fill="x", pady=1)
            img = fl.thumb_photo(fl.character_still(slug), 34) if meta.get("still") else None
            th = tk.Label(row, bg=C["panel"], width=4, height=2, cursor="hand2")
            if img is not None:
                th.configure(image=img, width=34, height=34)
            th.pack(side="left", padx=(4, 8), pady=3)
            col = tk.Frame(row, bg=C["panel"], cursor="hand2")
            col.pack(side="left", fill="x", expand=True)
            t1 = tk.Label(col, text=name, bg=C["panel"], fg=C["ink"], font=fsm, anchor="w",
                          cursor="hand2")
            t1.pack(fill="x")
            sub = ("still saved" if meta.get("still") else "no still yet")
            t2 = tk.Label(col, text=sub, bg=C["panel"], fg=C["faint"], font=fsm, anchor="w",
                          cursor="hand2")
            t2.pack(fill="x")
            ws = [th, col, t1, t2]
            for w_ in [row] + ws:
                w_.bind("<Button-1>", lambda _e, s=slug: _open(s))
            # Ryan 2026-10-22: "need an option to delete characters" (the only Delete was
            # hidden on the Advanced tab). ✕ on every row: one click arms it ("Delete?"),
            # a second click within 4 s deletes. No pop-up, same as the Advanced button.
            x_ = tk.Label(row, text="\u2715", bg=C["panel"], fg=C["faint"], font=fsm,
                          cursor="hand2", padx=6)
            x_.place(relx=1.0, rely=0.5, anchor="e", x=-4)
            x_.bind("<Button-1>", lambda _e, s=slug, l_=x_: _row_delete(s, l_) or "break")
            x_.bind("<Enter>", lambda _e, l_=x_: l_.configure(fg=C["ochre"]))
            x_.bind("<Leave>", lambda _e, s=slug, l_=x_: l_.configure(
                fg=C["ochre"] if cur.get("row_del") == s else C["faint"]))
            cast_rows[slug] = (row, ws, img)
            cast_x[slug] = x_
        if q and not cast_rows:
            lab(cast_box, "nobody called \u201c%s\u201d" % find_var.get().strip(),
                fg=C["faint"]).pack(fill="x", padx=8, pady=6)
        _cast_paint()
        win.after_idle(_show_current)

    def _show_current():
        """Scroll the list so the open character is in view."""
        try:
            rw = cast_rows.get(cur["slug"])
            cast_cv.update_idletasks()
            total = max(1, nav.winfo_height())
            if rw is None or total <= cast_cv.winfo_height():
                return
            y = cast_box.winfo_y() + rw[0].winfo_y()
            top, bot = cast_cv.yview()
            if not (top * total <= y <= bot * total - rw[0].winfo_height()):
                cast_cv.yview_moveto(max(0.0, (y - 40) / float(total)))
        except Exception:
            pass

    _find_job = {"id": None}

    def _relist_soon(*_a):
        """Typing in the search box: rebuild the list once typing pauses, not per key."""
        if _find_job["id"]:
            try:
                win.after_cancel(_find_job["id"])
            except Exception:
                pass
        _find_job["id"] = win.after(150, _relist)

    find_var.trace_add("write", _relist_soon)

    # ================================================================== saving (automatic)
    def _changed(*_a):
        if cur["loading"]:
            return
        if cur["save_job"]:
            try:
                win.after_cancel(cur["save_job"])
            except Exception:
                pass
        cur["save_job"] = win.after(800, _autosave)
        if cur["slug"] or cur["still"] or name_var.get().strip():
            say("saving…")
        else:
            say("not saved yet — give them a name or make a still to keep them")

    def _next_auto_name():
        taken = {n.lower() for _s, n in fl.list_characters()}
        i = 1
        while ("character %d" % i) in taken:
            i += 1
        return "character %d" % i

    def _meta():
        return {"name": name_var.get().strip(), "prompt": prompt_text(),
                "seed": (str(seed_box()) if seed_box() >= 0 else
                         str(cur["seed_used"]) if cur["seed_used"] is not None else "-1"),
                "lora": fl.CHARACTER_LORAS.get(lora_var.get()),
                "lora_strength": lstr_var.get() or "1.0",
                "picks": picks(), "action": action_text(),
                "saved": time.strftime("%Y-%m-%d %H:%M"),
                # keys this editor doesn't show (note, catchphrase, bible…) are KEPT: a save
                # used to drop Gustav's "note" silently
                **{k: v for k, v in (cur.get("extra") or {}).items()
                   if k not in ("name", "prompt", "seed", "lora", "lora_strength", "picks",
                                "action", "saved", "still", "slug")}}

    def _autosave(force=False):
        """Write the character. A brand-new one with neither a name nor a still is NOT written,
        so rerolling Surprise me never fills the cast with throwaways.

        ⚠️ It CANCELS the armed debounce timer first. Setting cur["save_job"] = None without
        cancelling it left a stale timer to fire ~800 ms later and write the character a SECOND
        time — after whatever this save (or another window) had just put in the file. That is
        how a description typed elsewhere got replaced by this window's own composed prompt.
        """
        if cur["save_job"]:
            try:
                win.after_cancel(cur["save_job"])
            except Exception:
                pass
        cur["save_job"] = None
        name = name_var.get().strip()
        if not (cur["slug"] or cur["still"] or name):
            return None
        if not name:
            name = _next_auto_name()
            cur["loading"] = True
            name_var.set(name)
            cur["loading"] = False
        if not cur["slug"]:
            base = fl.char_slug(name)
            slug, i = base, 2
            while os.path.exists(fl.char_dir(slug)):
                slug, i = "%s-%d" % (base, i), i + 1
            cur["slug"] = slug
        meta = _meta()
        meta["name"] = name
        # keys written to the file by OTHER windows since this one loaded it (a voice linked
        # in Dialogue or Voices -> voice_slug) are kept: the editor doesn't own them
        for k_, v_ in (fl.read_character(cur["slug"]) or {}).items():
            if k_ not in meta and k_ not in ("still", "slug"):
                meta[k_] = v_
        try:
            fl.write_character(cur["slug"], meta, still_src=cur["still"])
            if cur["clip"] and os.path.exists(cur["clip"]):
                import shutil
                dst = os.path.join(fl.char_dir(cur["slug"]), "clip.mp4")
                if os.path.abspath(cur["clip"]) != os.path.abspath(dst):
                    shutil.copyfile(cur["clip"], dst)
                    cur["clip"] = dst
        except Exception as exc:
            say("could not save: %s" % exc, warn=True)
            return None
        own = fl.character_still(cur["slug"])
        if own:
            cur["still"] = own
        if cur["slug"] not in cast_rows or force:
            _relist()
        else:
            _cast_paint()
        say("saved automatically · %s" % time.strftime("%H:%M:%S"))
        return cur["slug"]

    def _flush():
        if cur["save_job"]:
            try:
                win.after_cancel(cur["save_job"])
            except Exception:
                pass
            _autosave()

    # ================================================================== open / new / surprise
    def _reset_media():
        _stop_play()
        cur.update({"still": None, "seed_used": None, "clip": None, "frames": [],
                    "del_armed": None})
        cur["view"] = "Still"
        _show_still()
        _paint_view("Still")
        b_del.configure(text="Delete…")

    def _paint_bit():
        """THE BIT under the name: the catchphrase and why it's funny (Weirdo characters)."""
        ex = cur.get("extra") or {}
        bits = []
        if ex.get("catchphrase"):
            bits.append("\u201c%s\u201d" % ex["catchphrase"])
        if ex.get("hook"):
            bits.append(ex["hook"])
        bit_lbl.configure(text="  \u00b7  ".join(bits))

    def _from_photo(path=None):
        """A character whose look IS a picture you have (yourself, a drawing, a find)."""
        if cur["busy"]:
            say("already working on the %s\u2026" % cur["busy"], warn=True)
            return None
        if path is None:
            from tkinter import filedialog            # handler-time import (AGENTS.md)
            path = filedialog.askopenfilename(
                parent=win, title="A picture of the character",
                filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.heic"), ("All files", "*.*")])
            if not path:
                return None
        if not os.path.exists(path):
            say("that picture is gone: %s" % path, warn=True)
            return None
        # a transparent PNG bricks Tk on load (see thumb_photo): store a flattened copy
        safe = os.path.join(tempfile.mkdtemp(prefix="forge-photo-"), "still.png")
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-frames:v", "1",
                            "-pix_fmt", "rgb24", safe], check=True, capture_output=True,
                           timeout=120)
        except Exception as exc:
            say("couldn't read that picture: %s" % exc, warn=True)
            return None
        _new()
        nm = os.path.splitext(os.path.basename(path))[0].replace("_", " ").replace("-", " ")
        nm = " ".join(w.capitalize() for w in nm.split()[:3]) or "From a photo"
        cur["extra"] = {"note": "FROM A PHOTO \u2014 %s" % os.path.basename(path),
                        "from_photo": os.path.basename(path)}
        cur["loading"] = True
        name_var.set(nm)
        t_prompt.delete("1.0", "end")
        t_prompt.insert("1.0", "The person in the photo, exactly as they look in it.")
        cur["loading"] = False
        cur["still"] = safe
        cur["view"] = "Still"
        _show_still()
        _paint_view("Still")
        slug = _autosave(force=True)
        say("%s is saved from your photo \u2014 Angles draws them from every side, Voice gives "
            "them a voice. Rename them above." % nm)
        return slug

    def _new():
        _flush()
        cur["extra"] = {}
        _paint_bit()
        cur["slug"] = None
        cur["gen"] = cur.get("gen", 0) + 1     # a different character now
        _reset_media()
        _set_fields({}, name="")
        t_desc.delete("1.0", "end")
        _cast_paint()
        _refresh_est()
        say(NEW_HINT)
        e_name.focus_set()

    def _open(slug):
        if slug == cur["slug"]:
            return
        _flush()
        meta = fl.read_character(slug)
        if meta is None:
            say("could not read %s" % slug, warn=True)
            return
        cur["slug"] = slug
        cur["gen"] = cur.get("gen", 0) + 1
        cur["extra"] = dict(meta)
        _paint_bit()
        _reset_media()
        cur["still"] = fl.character_still(slug)
        clip = os.path.join(fl.char_dir(slug), "clip.mp4")
        cur["clip"] = clip if os.path.exists(clip) else None
        _set_fields(meta.get("picks") or {}, prompt=meta.get("prompt"),
                    action=meta.get("action") or "", name=meta.get("name") or slug,
                    seed=meta.get("seed", "-1"), lora=meta.get("lora"),
                    lstr=meta.get("lora_strength", "1.0"))
        _show_still()
        _cast_paint()
        _refresh_est()
        say("%s · every change saves itself" % (meta.get("name") or slug))
        if cur.get("tab") in ("Angles", "Voice"):
            _tab(cur["tab"])
        if cur["clip"]:
            _load_frames(cur["clip"], slug)

    def _surprise():
        """The old WEIRDO roll, landing here as an unsaved character. Empty description ->
        instant random roll; words -> the rig's model fits the roll to them on a worker
        thread, and a failure changes NOTHING (never a silent random fallback)."""
        # ⚠️ NO refusal while another roll is being fitted (Ryan, 2026-10-22): each press is its
        # own invention. Two fits land in their own order and the last one fills the form.
        base, action = fl.roll_weirdo(vein=vein_var.get())
        desc = t_desc.get("1.0", "end").strip()
        if not desc:
            _land_roll(base, action, "surprise! — Make still to see them")
            return
        job = {"done": False, "fit": None, "err": None, "t0": time.time()}

        def work():
            try:
                import director as _d
                job["fit"] = _d.Director().weirdo_fit(desc, vein=vein_var.get(), base=base,
                                                      action=action)
            except Exception as exc:
                job["err"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["done"] = True

        cur["fitting"] = cur.get("fitting", 0) + 1     # a count: two rolls may be in flight
        say("fitting your description on the rig…%s" % _in_flight())
        threading.Thread(target=work, daemon=True).start()
        win.after(500, lambda: _poll_fit(job, base, action))

    def _poll_fit(job, base, action):
        if not job["done"]:
            say("fitting your description… %.0f s" % (time.time() - job["t0"]))
            win.after(700, lambda: _poll_fit(job, base, action))
            return
        cur["fitting"] = max(0, cur.get("fitting", 0) - 1)
        b_surprise.configure(state="normal")
        if job["err"]:
            say("the rig's model could not fit it (%s) — nothing changed" % job["err"][:80],
                warn=True)
            return
        p2, a2 = fl.apply_weirdo_fit(base, action, job["fit"])
        _land_roll(p2, a2, "rolled to your description — Make still to see them")

    def _weirdo():
        """WEIRDO: the rig's writer invents a whole character (weirdos.invent) on a worker;
        if the writer can't be reached, the built-in pools roll one and the window SAYS so."""
        # ⚠️ NO refusal here either (Ryan, 2026-10-22): press it again and it invents another.
        import weirdos as _w
        brief = t_desc.get("1.0", "end").strip()
        flav = wflav_var.get()
        avoid = []
        for slug_, name_ in fl.list_characters():
            m_ = fl.read_character(slug_) or {}
            avoid.append("%s (%s)" % (name_, ((m_.get("picks") or {}).get("Feature")
                                              or m_.get("feature") or "")[:60]))
        job = {"done": False, "spec": None, "err": None, "t0": time.time()}

        def work():
            try:
                import director as _d
                job["spec"] = _w.invent(_d.Director(), brief=brief, flavour=flav, avoid=avoid)
            except Exception as exc:
                job["err"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["done"] = True

        cur["fitting"] = cur.get("fitting", 0) + 1
        say("inventing a weirdo on the rig…%s" % _in_flight())
        threading.Thread(target=work, daemon=True).start()
        win.after(400, lambda: _poll_weirdo(job))

    def _poll_weirdo(job):
        if not job["done"]:
            say("inventing a weirdo\u2026 %.0f s" % (time.time() - job["t0"]))
            win.after(500, lambda: _poll_weirdo(job))
            return
        cur["fitting"] = max(0, cur.get("fitting", 0) - 1)
        b_weirdo.configure(state="normal")
        import weirdos as _w
        spec, note = job["spec"], ""
        if spec is None:
            for _try in range(8):              # a refused roll must never kill the window
                try:
                    spec = _w.roll(flavour=wflav_var.get())
                    break
                except ValueError:
                    continue
            if spec is None:
                say("couldn't invent a weirdo just now — try again", warn=True)
                return
            note = (" (the rig's writer couldn't be reached \u2014 rolled from the built-in "
                    "list instead: %s)" % (job["err"] or "")[:60])
        _land_weirdo(spec, note)

    def _styled():
        """IN A STYLE: the rig's writer casts one character in the chosen style and role
        (char_styles.invent); if the writer can't be reached, the style's own pools roll one
        and the window SAYS so."""
        if cur.get("fitting"):
            say("still working on the last one \u2014 give it a moment", warn=True)
            return
        import char_styles as _c
        brief = t_desc.get("1.0", "end").strip()
        sname, role = style_var.get(), role_var.get()
        avoid = []
        for slug_, name_ in fl.list_characters():
            m_ = fl.read_character(slug_) or {}
            avoid.append("%s (%s)" % (name_, ((m_.get("picks") or {}).get("Feature")
                                              or m_.get("feature") or "")[:60]))
        job = {"done": False, "spec": None, "err": None, "t0": time.time(),
               "style": sname, "role": role}

        def work():
            try:
                import director as _d
                job["spec"] = _c.invent(_d.Director(), name=sname, role=role, brief=brief,
                                        avoid=avoid)
            except Exception as exc:
                job["err"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["done"] = True

        cur["fitting"] = cur.get("fitting", 0) + 1
        say("casting a %s character on the rig…%s" % (sname.split(" ·")[0], _in_flight()))
        threading.Thread(target=work, daemon=True).start()
        win.after(400, lambda: _poll_styled(job))

    def _poll_styled(job):
        if not job["done"]:
            say("casting\u2026 %.0f s" % (time.time() - job["t0"]))
            win.after(500, lambda: _poll_styled(job))
            return
        cur["fitting"] = max(0, cur.get("fitting", 0) - 1)
        b_styled.configure(state="normal")
        import char_styles as _c
        spec, note = job["spec"], ""
        if spec is None:
            for _try in range(8):              # a refused roll must never kill the window
                try:
                    spec = _c.roll(job["style"], job["role"])
                    break
                except ValueError:
                    continue
            if spec is None:
                say("couldn't cast one just now \u2014 try again", warn=True)
                return
            note = (" (the rig's writer couldn't be reached \u2014 rolled from the built-in "
                    "list instead: %s)" % (job["err"] or "")[:60])
        _land_weirdo(spec, note)

    def _land_weirdo(spec, note=""):
        """Land an invented weirdo as an UNSAVED character: the composed prompt (yours to
        edit), the bit as its action, its name, and the catchphrase / hook / bible / voice
        kept with it when it's saved (Dialogue designs the voice from `bible.voice`)."""
        import weirdos as _w
        _flush()
        cur["slug"] = None
        cur["gen"] = cur.get("gen", 0) + 1     # a different character now
        _reset_media()
        if spec.get("style"):                  # IN A STYLE: the style's framing + prompt
            import char_styles as _c
            _pk_, _pr_ = _c.picks(spec), _c.compose(spec)
        else:
            _pk_, _pr_ = weirdo_picks(spec), _w.compose(spec)
        _set_fields(_pk_, prompt=_pr_, action=spec["action"], name=spec["name"])
        prev.configure(image="", text="meet %s — press Make still to see them"
                       % spec["name"])
        prev.image = None
        cur["extra"] = {"catchphrase": spec["catchphrase"], "hook": spec["hook"],
                        "feature": spec["feature"], "flavour": spec.get("flavour"),
                        "bible": {"who": spec["who"], "voice": spec["voice"]},
                        "note": "WEIRDO (%s) \u2014 %s" % (spec.get("flavour") or "Anything",
                                                            spec["subject"])}
        if spec.get("style"):
            cur["extra"].update(style=spec["style"], role=spec.get("role"),
                                genre=spec.get("genre"),
                                note="STYLE (%s, %s) \u2014 %s" % (
                                    spec["style"], spec.get("role"), spec["subject"]))
        _paint_bit()
        _cast_paint()
        _refresh_est()
        _paint_prompt_fold()
        say("meet %s, %s with %s%s \u2014 Make still to see them"
            % (spec["name"], spec["subject"], spec["feature"][:70], note),
            warn=bool(note))

    def _land_roll(pk, action, msg):
        _flush()
        cur["extra"] = {}
        _paint_bit()
        cur["slug"] = None
        cur["gen"] = cur.get("gen", 0) + 1     # a different character now
        _reset_media()
        _set_fields(pk, action=action, name="")
        _cast_paint()
        _refresh_est()
        say(msg)

    def _duplicate():
        if not cur["slug"]:
            say("nothing saved to duplicate yet", warn=True)
            return
        _flush()
        still = cur["still"]
        nm = (name_var.get().strip() or "character") + " copy"
        cur["slug"] = None
        cur["gen"] = cur.get("gen", 0) + 1     # a different character now
        cur["loading"] = True
        name_var.set(nm)
        cur["loading"] = False
        cur["still"] = still
        _autosave(force=True)
        say("duplicated as %s" % nm)

    def _delete():
        if not cur["slug"]:
            say("this character is not saved — nothing to delete", warn=True)
            return
        if cur["del_armed"] != cur["slug"]:
            cur["del_armed"] = cur["slug"]
            b_del.configure(text="Really delete %s?" % (name_var.get().strip() or cur["slug"]))
            win.after(4000, lambda s=cur["slug"]: (
                cur.get("del_armed") == s and (cur.__setitem__("del_armed", None),
                                               b_del.configure(text="Delete…"))))
            return
        _delete_slug(cur["slug"])

    # ================================================================== rendering
    def _still_params():
        w, h = fl.char_still_size(pk_vars["Framing"].get(), q_var.get())
        return {"prompt": prompt_text(), "width": w, "height": h, "resolution": max(w, h),
                "steps": 25, "seed": seed_box(), "prefix": "forge_lab_char"}

    def _refresh_est():
        try:
            t = fl.estimate_seconds("t2i", _still_params(), None, None)
            b_still.configure(text="Make still · ~%.0f s" % t)
        except Exception:
            b_still.configure(text="Make still")
        _sync_clip_label()

    def _sync_clip_label():
        b_clip.configure(text="Make a %s test clip of them" % len_var.get())

    def _paint_q():
        for q, l_ in q_btns.items():
            on = q == q_var.get()
            l_.configure(bg=C["ink"] if on else C["panel"], fg="#14110f" if on else C["ink"])

    def _run(kind, params, refs, done):
        """Render through the RIG QUEUE and report on the Tk thread via `job`.

        ⚠️ The worker NEVER touches Tk. It fills `job`; the Tk thread polls it (the house
        pattern, same as the legacy window's _poll). A win.after() called FROM the worker was
        seen never to run under a driver, leaving the window stuck on "making the still…".

        2026-10-22: this used to submit and poll run_state itself. dispatch.render() does the
        same job AND puts it in the queue, so an angles sheet or a character clip waits for a
        card (visible, reorderable, deletable) instead of racing another render onto the same
        one — and the card is held until the take is really finished.
        """
        job = {"done": False, "meta": None, "seed": None}
        _title = "Character — %s" % (params.get("prefix") or kind)

        def work():
            try:
                job["meta"] = _dsp.render(_title, kind, dict(params or {}), refs,
                                          est_s=EST_IMAGE)
                job["seed"] = (job["meta"] or {}).get("seed")
            except Exception as exc:
                job["meta"] = {"error": str(exc)}
            finally:
                job["done"] = True

        def poll():
            if not job["done"]:
                win.after(250, poll)
                return
            done(job["meta"], job["seed"])

        threading.Thread(target=work, daemon=True).start()
        win.after(250, poll)

    def _files(meta, exts):
        out = []
        for f_ in (meta or {}).get("images") or []:
            p = os.path.join(fl.RUNS, meta.get("id") or "", f_.get("file") or "")
            if p.lower().endswith(exts) and os.path.exists(p):
                out.append(p)
        return out

    def _busy(kind):
        """kind = a job STARTING · None = a job FINISHED.

        ⚠️ 2026-10-22, Ryan: *"it wont let me generate a new character if one is in progress. it
        should let me hit the button and then add it to the queue."* This used to set one flag,
        DISABLE both buttons and refuse the second press. It now COUNTS what is in flight: every
        press becomes its own queue entry (dispatch.render / submit) and the window stays usable
        — the entries are visible, reorderable and deletable on the Home tab, which is where the
        app answers "what is running". `cur["busy"]` is kept as the NEWEST job's kind so the
        angles/voice repaints and the from-a-photo guard read exactly as they always did.
        """
        if kind:
            cur["jobs"].append({"kind": kind, "t0": time.time(), "gen": cur.get("gen", 0)})
        elif cur["jobs"]:
            cur["jobs"].pop(0)                       # oldest first: the queue is FIFO
        cur["busy"] = cur["jobs"][-1]["kind"] if cur["jobs"] else None
        for b_ in (b_still, b_clip):
            b_.configure(state="normal")             # never disabled: a press means "queue it"
        try:
            if cur.get("tab") == "Angles":
                _paint_angles()
            elif cur.get("tab") == "Voice":
                _paint_voice()
        except NameError:
            pass

    def _in_flight():
        """A short sentence for the status line: how much is queued right now."""
        n = len(cur["jobs"])
        if not n:
            return ""
        kinds = ", ".join(j["kind"] for j in cur["jobs"])
        return "  ·  %d in flight (%s)" % (n, kinds)

    def _make_still():
        # ⚠️ NO busy refusal, by design (Ryan, 2026-10-22): a second press is a SECOND queue
        # entry. The dispatcher runs them in turn, the Home tab lists them, and deleting the
        # row there is how you cancel one. The buttons are never disabled for this.
        if not prompt_text():
            say("nothing to render — pick a look first", warn=True)
            return
        if _adult_conflict(prompt_text()):
            say("this prompt puts a minor in swimwear or lingerie \u2014 not rendered. "
                "Those outfits are for adult characters only.", warn=True)
            return
        _busy("still")
        say("making the still on forge…%s" % _in_flight() if len(cur["jobs"]) == 1 else
            "queued — %d ahead of it, the queue is on the Home tab%s"
            % (len(cur["jobs"]) - 1, _in_flight()))
        # ⚠️ track WHICH character, not its slug: typing a name while the still renders
        # gives a new character its slug (autosave) mid-render, and the finished still was
        # thrown away as "a character you have moved away from"
        gen_at_start = cur.get("gen", 0)

        def done(meta, seed):
            _busy(None)
            if cur.get("gen", 0) != gen_at_start:
                say("a still finished for a character you have moved away from — not applied",
                    warn=True)
                return
            pngs = _files(meta, (".png", ".jpg", ".jpeg", ".webp"))
            if (meta or {}).get("error") or not pngs:
                say("still failed: %s" % ((meta or {}).get("error") or "no image"), warn=True)
                return
            cur["still"] = pngs[0]
            cur["seed_used"] = seed
            cur["view"] = "Still"
            _stop_play()
            _show_still()
            _paint_view("Still")
            slug = _autosave(force=True)
            say("still made with seed %s · %s" % (
                seed, "saved as %s" % (name_var.get().strip()) if slug else "not saved"))

        _run("t2i", _still_params(), None, done)

    def _make_clip():
        # ⚠️ NO busy refusal (Ryan, 2026-10-22): press it as often as you like — each press is
        # its own queue entry, and the clip needs the still that is on screen when it starts.
        if not cur["still"]:
            say("make a still first — the clip uses it as the reference", warn=True)
            return
        prompt = fl.weirdo_video_prompt(picks(), prompt_text(), action_text())
        params = fl.weirdo_video_params(prompt, canvas_var.get(), len_var.get())
        params["prefix"] = "forge_lab_char_clip"
        _busy("clip")
        say("making the test clip on forge… about a minute or two%s" % _in_flight()
            if len(cur["jobs"]) == 1 else
            "queued — %d ahead of it, the queue is on the Home tab%s"
            % (len(cur["jobs"]) - 1, _in_flight()))
        # ⚠️ track WHICH character, not its slug: typing a name while the still renders
        # gives a new character its slug (autosave) mid-render, and the finished still was
        # thrown away as "a character you have moved away from"
        gen_at_start = cur.get("gen", 0)

        def done(meta, seed):
            _busy(None)
            mp4s = [p for p in _files(meta, (".mp4",)) if not p.endswith("s2v_synced.mp4")] or \
                _files(meta, (".mp4",))
            if (meta or {}).get("error") or not mp4s:
                say("clip failed: %s" % ((meta or {}).get("error") or "no video"), warn=True)
                return
            if cur.get("gen", 0) != gen_at_start:
                say("a clip finished for a character you have moved away from", warn=True)
                return
            cur["clip"] = mp4s[0]
            _load_frames(mp4s[0], cur["slug"])
            _autosave()

        _run("video", params, [cur["still"]], done)

    def _load_frames(mp4, slug):
        """ffmpeg on a worker thread (it can take a while); the frames become Tk images on the
        Tk thread, which polls — the worker never touches Tk (see _run)."""
        out = tempfile.mkdtemp(prefix="charclip-")
        job = {"done": False, "names": [], "err": None}

        def work():
            try:
                r = fl.weirdo_player_frames(mp4, out)
                job["names"] = sorted(n for n in os.listdir(out) if n.endswith(".png"))
                if not job["names"]:
                    job["err"] = (r.stderr or "").strip()[:120] or "no frames"
            except Exception as exc:
                job["err"] = str(exc)
            finally:
                job["done"] = True

        def apply():
            if not job["done"]:
                win.after(200, apply)
                return
            if cur["slug"] != slug and slug is not None:
                return
            if job["err"]:
                say("could not play the clip (%s)" % job["err"], warn=True)
                return
            cur["frames"] = [tk.PhotoImage(file=os.path.join(out, n)) for n in job["names"]]
            _show("Clip")
            say("test clip ready — click it to pause")

        threading.Thread(target=work, daemon=True).start()
        win.after(200, apply)

    def _use_in_video():
        if not load_into_form:
            say("the main form is not available", warn=True)
            return
        _flush()
        try:
            note = load_into_form(_meta(), cur["still"])
        except Exception as exc:
            say("could not load into the form: %s" % exc, warn=True)
            return
        say(note or "loaded into the main form")
        try:
            import dock as _dock
            if _dock.installed() is not None:
                _dock.installed().select(None)   # tabs: show the Make tab it filled
            root.deiconify()
            root.lift()
        except Exception:
            pass

    # ================================================================== tabs + close
    # ================================================================== angles + voice tabs
    def _cname():
        return name_var.get().strip() or "this character"

    def _paint_angles():
        for w_ in ang_grid.winfo_children():
            w_.destroy()
        slug = cur["slug"]
        if not (slug and cur["still"]):
            ang_head.configure(text="Make a still first \u2014 the sheet is drawn from it.")
            b_sheet.configure(state="disabled")
            b_back.configure(state="disabled")
            return
        import charpack as _cp
        doc = _cp.load_sheet(slug) or {}
        have = {v.get("key"): os.path.join(fl.char_dir(slug), "sheet", v.get("file", ""))
                for v in doc.get("views") or []}
        n_have = sum(1 for p in have.values() if os.path.exists(p))
        b_sheet.configure(state="normal",
                          text="Remake the whole sheet" if n_have else "Make the sheet")
        b_back.configure(state="normal")
        ang_head.configure(text=("%s from every side \u2014 %d angles. Every video of %s "
                                 "uses them, so the face and the outfit stay the same."
                                 % (_cname(), n_have, _cname())) if n_have else
                           "The same person from every side: front, three-quarter, profile, "
                           "close-ups and expressions \u2014 about 6 images, a few minutes. "
                           "Every video of %s then uses them to keep the face and outfit."
                           % _cname())
        for i, view in enumerate(_cp.SHEET_VIEWS):
            p = have.get(view["key"])
            cell = tk.Frame(ang_grid, bg=C["panel2"])
            cell.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0, 6), pady=(0, 6))
            img = fl.thumb_photo(p, 104) if p and os.path.exists(p) else None
            pic = tk.Label(cell, bg="#100e0c", image=img, text="" if img else "not made",
                           fg=C["faint"], font=fsm, height=0 if img else 5, cursor="hand2")
            pic.image = img
            pic.pack(fill="x")
            if img:
                pic.bind("<Button-1>", lambda _e, pp=p: _enlarge(pp))
            r_ = tk.Frame(cell, bg=C["panel2"])
            r_.pack(fill="x")
            tk.Label(r_, text=view["key"].replace("-", " "), bg=C["panel2"], fg=C["muted"],
                     font=fsm, anchor="w").pack(side="left", padx=4)
            if img:
                rd = tk.Label(r_, text="redo", bg=C["panel2"], fg=C["ochre"], font=fsm,
                              cursor="hand2")
                rd.pack(side="right", padx=4)
                rd.bind("<Button-1>", lambda _e, k=view["key"]: _make_sheet([k], force=True))

    def _make_sheet(views=None, force=None):
        slug = cur["slug"]
        if not (slug and cur["still"]):
            say("make a still first — the sheet is drawn from it", warn=True)
            return
        # ⚠️ NO busy refusal (Ryan, 2026-10-22): each press is its own queue entry. The sheet is
        # the one job here that is NOT a dispatch.render() — charpack.build_sheet does its own
        # submits — so the entry is a GATE (see dispatch.admit): it holds a card while it draws.
        _flush()
        if force is None:
            force = views is None          # "Remake the whole sheet" redraws every angle
        job = {"done": False, "out": None, "err": None, "step": ""}
        _busy("sheet")
        _paint_angles()
        say("drawing %s from every side…%s" % (_cname(), _in_flight()))

        def work():
            try:
                import charpack as _cp
                job["out"] = _cp.build_sheet(
                    slug, views=views, scale="Fast", force=force,
                    on_progress=lambda i, k, s: job.__setitem__("step", "%s %s" % (k, s)))
            except Exception as exc:
                job["err"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["done"] = True

        def poll():
            if not job["done"]:
                say("drawing %s from every side\u2026 %s" % (_cname(), job["step"]))
                win.after(500, poll)
                return
            _busy(None)
            _paint_angles()
            if job["err"]:
                say("the sheet stopped: %s" % job["err"][:160], warn=True)
            else:
                say("%s's sheet is ready \u2014 every video of them uses it" % _cname())
        threading.Thread(target=work, daemon=True).start()
        win.after(500, poll)

    def _voice_of():
        if not cur["slug"]:
            return None
        meta = fl.read_character(cur["slug"]) or {}
        vs = meta.get("voice_slug")
        import dialogue as _dl
        return _dl.load_voice(vs) if vs else None

    def _paint_voice():
        for b_ in (b_vplay, b_auto_v, b_open_v):
            b_.pack_forget()
        if not cur["slug"]:
            vo_head.configure(text="Save them first (give them a name)")
            vo_sub.configure(text="")
            return
        v = _voice_of()
        if v:
            import voicelab as _vl
            vo_head.configure(text="%s's voice: %s" % (_cname(), v.get("name") or v["slug"]))
            vo_sub.configure(text=_vl.describe(v))
            b_vplay.pack(side="left")
            b_open_v.pack(side="left", padx=8)
            b_auto_v.configure(text="Design a new one")
            b_auto_v.pack(side="left")
        else:
            vo_head.configure(text="%s has no voice yet" % _cname())
            vo_sub.configure(text="Dialogue and Episode will ask for one.")
            b_auto_v.configure(text="Give them a voice")
            b_auto_v.pack(side="left")
            b_open_v.pack(side="left", padx=8)
        b_auto_v.configure(state="normal")     # a press queues another; never refused

    def _play_voice():
        v = _voice_of()
        import dialogue as _dl
        w_ = _dl.voice_wav(v, "neutral") if v else None
        if not w_:
            say("no voice to play yet", warn=True)
            return
        try:
            subprocess.Popen(["afplay", w_])
        except Exception as exc:
            say("could not play: %s" % exc, warn=True)

    def _auto_voice():
        slug = cur["slug"]
        if not slug:
            say("give them a name first, so the voice has someone to belong to", warn=True)
            return
        # ⚠️ NO busy refusal (Ryan, 2026-10-22): a press starts its own job. Voices are the one
        # thing here that is NOT a queue entry yet — voicelab asks the card manager for room and
        # a busy card comes back as the card prompt (with a retry), not as a queue row.
        _flush()
        job = {"done": False, "out": None, "err": None, "exc": None, "step": ""}
        _busy("voice")
        _paint_voice()

        def work():
            try:
                import voicelab as _vl
                job["out"] = _vl.auto_voice(slug, progress=lambda m: job.__setitem__(
                    "step", m))
            except Exception as exc:
                job["exc"] = exc
                job["err"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["done"] = True

        def poll():
            if not job["done"]:
                say("finding %s's voice\u2026 %s" % (_cname(), job["step"]))
                win.after(500, poll)
                return
            _busy(None)
            if type(job["exc"]).__name__ == "CardBusy":
                import cards_ui
                cards_ui.handle(win, job["exc"], ctx, retry=_auto_voice, say=say,
                                owner="characters")
                _paint_voice()
                return
            _paint_voice()
            if job["err"]:
                say("no voice: %s" % job["err"][:160], warn=True)
            else:
                say("%s has a voice \u2014 \u25b6 Hear it. Dialogue and Episode use it "
                    "from now on" % _cname())
                _play_voice()
        threading.Thread(target=work, daemon=True).start()
        win.after(500, poll)

    def _open_voices():
        if ctx.get("open_voices"):
            ctx["open_voices"](cur["slug"])
        else:
            say("the Voices page isn't available here", warn=True)

    def _tab(t):
        cur["tab"] = t
        if t == "Angles":
            _paint_angles()
        elif t == "Voice":
            _paint_voice()
        for name_, pg in pages.items():
            if name_ == t:
                pg.pack(fill="both", expand=True)
            else:
                pg.pack_forget()
        for name_, l_ in tab_lbls.items():
            on = name_ == t
            l_.configure(fg=C["ink"] if on else C["muted"], font=fh if on else fsm)

    def _close():
        _flush()
        _stop_play()
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _close)
    win.bind("<Escape>", lambda _e: _close())

    # wiring that marks the character dirty
    name_var.trace_add("write", lambda *_a: _changed())
    seed_var.trace_add("write", lambda *_a: (_changed(), _refresh_est()))
    lstr_var.trace_add("write", lambda *_a: _changed())
    t_prompt.bind("<KeyRelease>", lambda _e: (_paint_prompt_fold(), _changed()))
    t_action.bind("<KeyRelease>", lambda _e: _changed())
    pk_vars["Framing"].trace_add("write", lambda *_a: _refresh_est())

    # ------------------------------------------------------------------ start
    _tab("Look")
    _paint_q()
    _relist()
    _new()
    if btn_colors:
        try:
            btn_colors(win)
        except Exception:
            pass
        # btn_colors repaints every Button light; the two PRIROBIN actions stay ochre
        for b_ in (b_new, b_still, b_weirdo):
            b_.configure(bg=C["ochre"], fg="#14110f")

    # ------------------------------------------------------------------ driver API
    win.ch2 = {"cur": cur, "picks": picks, "prompt_text": prompt_text, "pk_vars": pk_vars,
               "sl_vars": sl_vars, "name_var": name_var, "seed_var": seed_var,
               "t_prompt": t_prompt, "t_action": t_action, "t_desc": t_desc,
               "new": _new, "open": _open, "surprise": _surprise, "make_still": _make_still,
               "make_clip": _make_clip, "use_in_video": _use_in_video, "delete": _delete,
               "duplicate": _duplicate, "flush": _flush, "tab": _tab, "pages": pages,
               "cast_rows": cast_rows, "status": status, "prev": prev,
               "find_var": find_var, "cast_cv": cast_cv, "relist": _relist,
               "cast_x": cast_x, "row_delete": _row_delete,
               "buttons": {"new": b_new, "surprise": b_surprise, "still": b_still,
                           "clip": b_clip, "use_video": b_use_video, "delete": b_del},
               "reset_prompt": _reset_prompt, "toggle_prompt": _toggle_prompt,
               "on_pick": _on_pick, "nav": nav, "mid": mid, "right": right,
               "weirdo": _weirdo, "land_weirdo": _land_weirdo, "wflav_var": wflav_var,
               "make_sheet": _make_sheet, "from_photo": _from_photo, "ang_grid": ang_grid, "ang_head": ang_head,
               "b_sheet": b_sheet, "auto_voice": _auto_voice, "vo_head": vo_head,
               "b_auto_v": b_auto_v, "open_voices": _open_voices,
               "b_weirdo": b_weirdo, "bit_lbl": bit_lbl, "pk_menus": pk_menus,
               "pk_entries": pk_entries, "typed": _typed,
               "vein_var": vein_var}
    return win
