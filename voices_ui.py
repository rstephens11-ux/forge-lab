"""The VOICES page (2026-10-17) — build a voice once, every scene uses it.

    Voices                                  New voice   Clone from a recording…   Close
    ┌ LIBRARY ─────────────┐  ┌ BUILD ─────────────────────────────────────────────────┐
    │ ▶ Clarence           │  │ Name [Clarence]                 Language [English ▾]    │
    │   38 male · Minnesot │  │ Gender (male)(female)(androgynous)    Age ──●── 38      │
    │   Clarence's voice   │  │ Accent [Minnesotan              ▾]                      │
    │ ▶ Gustav             │  │ pitch ●  pace ●  energy ●  warmth ●  grit ●  older ●    │
    │   …                  │  │ How they sound [breathy, calm urgency]                  │
    └──────────────────────┘  │ VoiceDesign reads: "A 38-year-old male voice, …"        │
                              │ Sample line [Expect a forty percent chance of rain.]   │
                              │ [Audition 3]   ▶A Keep   ▶B Keep   ▶C Keep              │
                              │ ─ TEST ─ [line…] mood[▾]  [Say it: both]  ▶Qwen ▶Fish   │
                              │ ─ USED BY ─ [character ▾] [Link]                        │
                              └─────────────────────────────────────────────────────────┘

Engine: voicelab (Tk-free). Rig jobs run on a worker thread; the Tk thread polls a job dict.
A busy card brings up the card prompt (cards_ui), like every other window.
"""
import os
import sys
import subprocess
import threading
import time

import dialogue as dlg
import voicelab as vl

# macOS draws its own white buttons and ignores bg: light text on them vanished
# ("Stop after this step" was blank on the MacBook) - dark text there, always
_MAC = sys.platform == "darwin"

SLIDER_LABELS = (("pitch", "pitch"), ("pace", "pace"), ("energy", "energy"),
                 ("warmth", "warmth"), ("grit", "grit"), ("age", "sounds older"))
ACCENT_GROUPS = (
    ("English-speaking", ("British", "Cockney", "Scottish", "Irish", "Welsh", "Yorkshire",
                          "Northern", "Liverpool", "Geordie", "West Country", "Australian",
                          "New Zealand", "South African", "Canadian", "Jamaican",
                          "Trinidadian")),
    ("United States", ("New York", "Boston", "Maine", "Southern", "Texas", "Midwest",
                       "Minnesotan", "Chicago", "Philadelphia", "Brooklyn", "Cajun",
                       "Appalachian", "Valley", "Hawaiian", "Mid-Atlantic")),
    ("Europe", ("French", "German", "Italian", "Spanish", "Russian", "Dutch", "Swedish",
                "Norwegian", "Danish", "Finnish", "Polish", "Greek", "Turkish")),
    ("Asia, Africa & Middle East", ("Indian", "Pakistani", "Japanese", "Korean", "Mandarin",
                                    "Cantonese", "Filipino", "Vietnamese", "Thai", "Arabic",
                                    "Israeli", "Persian", "Nigerian", "Kenyan", "Ghanaian")),
    ("Latin America", ("Brazilian", "Mexican", "Argentinian", "Cuban", "Puerto Rican")),
    ("History & characters", ("Old English", "18th-c", "Early American", "Colonial",
                              "Transatlantic", "Game-show", "Movie-trailer", "Robot")),
)


def accent_groups():
    """[(group, [labels])] — every accent lands in exactly one group ("More" catches rest)."""
    left = [a for a, _p in vl.accents() if a and not a.startswith("—")]
    out = []
    for g, keys in ACCENT_GROUPS:
        mine = [a for a in left if any(a.startswith(k) for k in keys)]
        left = [a for a in left if a not in mine]
        out.append((g, mine))
    if left:
        out.append(("More", left))
    return out


def open_voices(root, ctx):
    import tkinter as tk
    import tkinter.font as tkfont

    C = ctx["C"]
    f, fh, fsm = ctx["fonts"]["f"], ctx["fonts"]["fh"], ctx["fonts"]["fsm"]
    fbig = tkfont.Font(family=fh.cget("family"), size=max(15, int(fh.cget("size")) + 5),
                       weight="bold")
    import dock as _dock                 # a tab in the main window (Ryan: tabs)
    win = _dock.window(root)
    win.title("Voices")
    win.configure(bg=C["bg"])
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1240, _sw - 40), min(820, _sh - 110)))
    win.minsize(1060, 680)
    st = {"v": vl.blank(), "aud": [], "busy": None, "player": None, "tests": {},
          "loading": False}

    def say(msg, warn=False):
        status.configure(text=msg, fg=C["ochre"] if warn else C["muted"])

    def lab(parent, text, fg=None, font=None, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["muted"],
                        font=font or fsm, anchor="w", **kw)

    def button(parent, text, cmd, primary=False, **kw):
        return tk.Button(parent, text=text, command=cmd, relief="flat", borderwidth=0,
                         font=fh if primary else fsm, padx=12, pady=7 if primary else 5,
                         bg=C["ochre"] if primary else C["panel2"],
                         fg="#14110f" if primary or _MAC else C["ink"],
                         activebackground="#d9b45e" if primary else C["rust"],
                         activeforeground="#14110f", highlightthickness=0, **kw)

    def entry(parent, var, width=None):
        e = tk.Entry(parent, textvariable=var, bg="#100e0c", fg=C["ink"], relief="flat",
                     font=f, insertbackground=C["ink"], **({"width": width} if width else {}))
        return e

    # ------------------------------------------------------------------ header
    hd = tk.Frame(win, bg=C["panel"])
    hd.pack(fill="x")
    tk.Label(hd, text="Voices", bg=C["panel"], fg=C["ink"], font=fbig).pack(
        side="left", padx=(16, 14), pady=10)
    lab(hd, "build a voice once — every scene uses it").pack(side="left")
    button(hd, "Close", lambda: win.destroy()).pack(side="right", padx=12)
    b_clone = button(hd, "Clone from a recording…", lambda: _show_clone())
    b_clone.pack(side="right", padx=(0, 6))
    button(hd, "New voice", lambda: _new(), primary=True).pack(side="right", padx=(0, 6))
    tk.Frame(win, bg=C["line"], height=1).pack(fill="x")
    status = tk.Label(win, text="", bg=C["bg"], fg=C["muted"], font=fsm, anchor="w")
    status.pack(fill="x", padx=18, pady=(6, 4))

    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    # ------------------------------------------------------------------ library (left)
    libf = tk.Frame(body, bg=C["panel"], width=300, highlightthickness=1,
                    highlightbackground=C["line"])
    libf.pack(side="left", fill="y", padx=(0, 14))
    libf.pack_propagate(False)
    lab(libf, "LIBRARY", fg=C["faint"]).pack(fill="x", padx=12, pady=(10, 4))
    libc = tk.Canvas(libf, bg=C["panel"], highlightthickness=0)
    libc.pack(fill="both", expand=True)
    lib = tk.Frame(libc, bg=C["panel"])
    _lw = libc.create_window((0, 0), window=lib, anchor="nw")
    lib.bind("<Configure>", lambda e: libc.configure(scrollregion=libc.bbox("all")))
    libc.bind("<Configure>", lambda e: libc.itemconfigure(_lw, width=e.width))
    try:
        ctx["mod"].scoped_wheel(win, libc)
    except Exception:
        pass

    # ------------------------------------------------------------------ builder (right)
    right = tk.Frame(body, bg=C["bg"])
    right.pack(side="left", fill="both", expand=True)
    build = tk.Frame(right, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    clonep = tk.Frame(right, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    build.pack(fill="both", expand=True)
    pad = {"padx": 16}

    title = tk.Label(build, text="", bg=C["panel"], fg=C["ink"], font=fbig, anchor="w")
    title.pack(fill="x", padx=16, pady=(12, 6))
    r = tk.Frame(build, bg=C["panel"])
    r.pack(fill="x", **pad)
    lab(r, "Name").pack(side="left")
    name_var = tk.StringVar()
    e_name = entry(r, name_var, 24)
    e_name.pack(side="left", padx=(8, 18), ipady=3)
    e_name.bind("<KeyRelease>", lambda _e: _changed())
    lab(r, "Speaks").pack(side="left")
    lang_var = tk.StringVar(value="English")
    _lm = tk.OptionMenu(r, lang_var, *vl.LANGUAGES, command=lambda *_a: _changed())
    _lm.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, width=10)
    _lm["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _lm.pack(side="left", padx=8)

    r = tk.Frame(build, bg=C["panel"])
    r.pack(fill="x", pady=(10, 0), **pad)
    lab(r, "Voice").pack(side="left")
    g_var = tk.StringVar(value="male")
    g_btns = {}
    for g in vl.GENDERS:
        b_ = tk.Label(r, text=g, bg=C["panel2"], fg=C["ink"], font=fsm, padx=10, pady=4,
                      cursor="hand2")
        b_.pack(side="left", padx=(8 if g == vl.GENDERS[0] else 2, 0))
        b_.bind("<Button-1>", lambda _e, gg=g: (g_var.set(gg), _changed()))
        g_btns[g] = b_
    lab(r, "     Age").pack(side="left")
    age_var = tk.IntVar(value=35)
    tk.Scale(r, from_=8, to=90, orient="horizontal", variable=age_var, length=220,
             showvalue=True, bg=C["panel"], fg=C["ink"], highlightthickness=0,
             troughcolor="#100e0c", font=fsm, command=lambda _v: _changed()).pack(
        side="left", padx=8)

    r = tk.Frame(build, bg=C["panel"])
    r.pack(fill="x", pady=(8, 0), **pad)
    lab(r, "Accent").pack(side="left")
    acc_var = tk.StringVar()
    e_acc = entry(r, acc_var, 34)
    e_acc.pack(side="left", padx=(8, 0), ipady=3)
    e_acc.bind("<KeyRelease>", lambda _e: _changed())
    acc_mb = tk.Menubutton(r, text="▾ pick", bg=C["panel2"], fg=C["ink"], font=fsm,
                           relief="flat", padx=8, pady=3)
    acc_m = tk.Menu(acc_mb, tearoff=0, bg=C["panel2"], fg="#000000", font=fsm)
    acc_m.add_command(label="none", command=lambda: (acc_var.set(""), _changed()))
    for g, labels in accent_groups():
        sub = tk.Menu(acc_m, tearoff=0, bg=C["panel2"], fg="#000000", font=fsm)
        for a in labels:
            sub.add_command(label=a, command=lambda aa=a: (acc_var.set(aa), _changed()))
        acc_m.add_cascade(label=g, menu=sub)
    acc_mb.configure(menu=acc_m)
    acc_mb.pack(side="left", padx=4)
    lab(r, "  pick one, or type any accent \u2014 still plain? clone a real 10\u201315 s "
           "clip of it").pack(side="left")

    r = tk.Frame(build, bg=C["panel"])
    r.pack(fill="x", pady=(8, 0), **pad)
    sl_vars = {}
    for key, txt in SLIDER_LABELS:
        col = tk.Frame(r, bg=C["panel"])
        col.pack(side="left", padx=(0, 10))
        lab(col, txt).pack()
        sv = tk.IntVar(value=0)
        tk.Scale(col, from_=-2, to=2, orient="horizontal", variable=sv, length=96,
                 showvalue=False, bg=C["panel"], highlightthickness=0, troughcolor="#100e0c",
                 command=lambda _v: _changed()).pack()
        sl_vars[key] = sv

    r = tk.Frame(build, bg=C["panel"])
    r.pack(fill="x", pady=(8, 0), **pad)
    lab(r, "How they sound").pack(side="left")
    notes_var = tk.StringVar()
    e_notes = entry(r, notes_var)
    e_notes.pack(side="left", fill="x", expand=True, padx=8, ipady=3)
    e_notes.bind("<KeyRelease>", lambda _e: _changed())
    r = tk.Frame(build, bg=C["panel"])
    r.pack(fill="x", pady=(8, 0), **pad)
    lab(r, "Delivery").pack(side="left")
    fishd_var = tk.StringVar()
    e_fishd = entry(r, fishd_var)
    e_fishd.pack(side="left", fill="x", expand=True, padx=8, ipady=3)
    e_fishd.bind("<KeyRelease>", lambda _e: _changed())
    lab(r, "how every line is said, e.g. slow, deadpan, slightly bored (Fish only, "
           "experimental)", fg=C["faint"]).pack(side="left")
    desc_lbl = tk.Label(build, text="", bg=C["panel"], fg=C["faint"], font=fsm, anchor="w",
                        justify="left", wraplength=780)
    desc_lbl.pack(fill="x", pady=(6, 0), **pad)

    r = tk.Frame(build, bg=C["panel"])
    r.pack(fill="x", pady=(10, 0), **pad)
    lab(r, "Sample line").pack(side="left")
    sample_var = tk.StringVar(value=vl.SAMPLE_LINE)
    entry(r, sample_var).pack(side="left", fill="x", expand=True, padx=8, ipady=3)
    aud_row = tk.Frame(build, bg=C["panel"])
    aud_row.pack(fill="x", pady=(8, 0), **pad)
    b_aud = button(aud_row, "Audition 3 voices", lambda: _audition(), primary=True)
    b_aud.pack(side="left")
    aud_cells = tk.Frame(aud_row, bg=C["panel"])
    aud_cells.pack(side="left", padx=8)

    tk.Frame(build, bg=C["line"], height=1).pack(fill="x", pady=(14, 8), padx=16)
    test_box = tk.Frame(build, bg=C["panel"])
    test_box.pack(fill="x", **pad)
    lab(test_box, "TEST IT", fg=C["faint"]).pack(anchor="w")
    r = tk.Frame(test_box, bg=C["panel"])
    r.pack(fill="x", pady=(4, 0))
    test_var = tk.StringVar(value="Your ears are in my soup.")
    entry(r, test_var).pack(side="left", fill="x", expand=True, ipady=3)
    mood_var = tk.StringVar(value="neutral")
    _mm = tk.OptionMenu(r, mood_var, *list(dlg.MOOD_WORDS))
    _mm.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, width=10)
    _mm["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _mm.pack(side="left", padx=8)
    b_test = button(r, "Hear it both ways", lambda: _test())
    b_test.pack(side="left")
    test_res = tk.Frame(test_box, bg=C["panel"])
    test_res.pack(fill="x", pady=(6, 0))

    tk.Frame(build, bg=C["line"], height=1).pack(fill="x", pady=(12, 8), padx=16)
    link_box = tk.Frame(build, bg=C["panel"])
    link_box.pack(fill="x", pady=(0, 14), **pad)
    lab(link_box, "USED BY").pack(side="left")
    char_var = tk.StringVar(value="")
    char_menu = tk.OptionMenu(link_box, char_var, "")
    char_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, borderwidth=0, width=18)
    char_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    char_menu.pack(side="left", padx=8)
    b_link = button(link_box, "Make it their voice", lambda: _link())
    b_link.pack(side="left")
    link_lbl = lab(link_box, "")
    link_lbl.pack(side="left", padx=10)

    # ------------------------------------------------------------------ clone panel
    lab(clonep, "Clone from a recording", fg=C["ink"], font=fbig).pack(fill="x", padx=16,
                                                                        pady=(12, 2))
    lab(clonep, "5–30 seconds of one person talking, no music. Qwen and Fish can both "
                "speak in a cloned voice.", wraplength=760).pack(fill="x", padx=16)
    r = tk.Frame(clonep, bg=C["panel"])
    r.pack(fill="x", padx=16, pady=(12, 0))
    cl_path = tk.StringVar(value="")
    button(r, "Choose a file…", lambda: _pick_clip()).pack(side="left")
    cl_lbl = lab(r, "no file yet")
    cl_lbl.pack(side="left", padx=10)
    r = tk.Frame(clonep, bg=C["panel"])
    r.pack(fill="x", padx=16, pady=(10, 0))
    lab(r, "Name").pack(side="left")
    cl_name = tk.StringVar()
    entry(r, cl_name, 28).pack(side="left", padx=8, ipady=3)
    r = tk.Frame(clonep, bg=C["panel"])
    r.pack(fill="x", padx=16, pady=(10, 0))
    lab(r, "What it says").pack(side="left")
    cl_text = tk.StringVar()
    entry(r, cl_text).pack(side="left", fill="x", expand=True, padx=8, ipady=3)
    lab(clonep, "leave it blank and the rig will listen to it", fg=C["faint"]).pack(
        fill="x", padx=16)
    cl_ok = tk.BooleanVar(value=False)
    tk.Checkbutton(clonep, text="This is my voice, or I have permission to use it",
                   variable=cl_ok, bg=C["panel"], fg=C["ink"], selectcolor="#100e0c",
                   activebackground=C["panel"], font=fsm).pack(anchor="w", padx=16,
                                                               pady=(12, 0))
    r = tk.Frame(clonep, bg=C["panel"])
    r.pack(fill="x", padx=16, pady=(14, 14))
    b_make_clone = button(r, "Make the voice", lambda: _clone(), primary=True)
    b_make_clone.pack(side="left")
    button(r, "Cancel", lambda: _show_build()).pack(side="left", padx=8)

    # ================================================================== state <-> widgets
    def _paint_gender():
        for g, b_ in g_btns.items():
            on = g == g_var.get()
            b_.configure(bg=C["ink"] if on else C["panel2"], fg="#14110f" if on else C["ink"])

    def _read():
        v = st["v"]
        v["name"] = name_var.get().strip() or "New voice"
        v["language"] = lang_var.get()
        v["gender"] = g_var.get()
        v["age_years"] = int(age_var.get())
        v["accent"] = vl.accent_phrase(acc_var.get())
        v["notes"] = notes_var.get().strip()
        v["fish_direction"] = fishd_var.get().strip()
        for k, sv in sl_vars.items():
            v.setdefault("sliders", {})[k] = int(sv.get())
        return v

    def _changed():
        if st["loading"]:
            return
        _paint_gender()
        desc_lbl.configure(text="The voice is made from:  “%s”" % vl.describe(_read()))
        # a KEPT voice saves its settings as you go (name, Fish direction, the description
        # its next mood samples / auditions are designed from) — the recording is unchanged
        if st["v"].get("slug") and dlg.voice_wav(st["v"], "neutral") and \
                st["v"].get("source") != "character":
            if st.get("save_job"):
                win.after_cancel(st["save_job"])
            st["save_job"] = win.after(700, _save_settings)

    def _save_settings():
        st["save_job"] = None
        try:
            vl.save_settings(_read())
            _paint_lib()
        except Exception as exc:
            say("couldn't save: %s" % exc, warn=True)

    def _fill(v):
        st["loading"] = True
        try:
            st["v"] = v
            name_var.set(v.get("name") or "")
            lang_var.set(v.get("language") or "English")
            g_var.set(v.get("gender") or "male")
            age_var.set(int(v.get("age_years") or 35))
            acc_var.set(vl.accent_label(v.get("accent") or ""))
            notes_var.set(v.get("notes") or "")
            fishd_var.set(v.get("fish_direction") or "")
            for k, sv in sl_vars.items():
                sv.set(int((v.get("sliders") or {}).get(k, 0)))
        finally:
            st["loading"] = False
        kept = bool(v.get("slug") and dlg.voice_wav(v, "neutral"))
        title.configure(text=(v.get("name") or "New voice") if kept else
                        "Build a new voice")
        b_aud.configure(text="3 new auditions" if kept else "Audition 3 voices")
        st["aud"] = []
        st["tests"] = {}
        _paint_aud()
        _paint_tests()
        _paint_link()
        _changed()

    def _paint_lib():
        for w_ in lib.winfo_children():
            w_.destroy()
        items = vl.library()
        try:
            import scene_engine as se
            cname = {s_: (m_.get("name") or s_) for s_, m_ in se.list_characters()}
        except Exception:
            cname = {}
        if not items:
            lab(lib, "no voices yet — build one, or clone a recording", fg=C["faint"],
                wraplength=260).pack(fill="x", padx=12, pady=8)
        for it in items:
            on = it["slug"] == st["v"].get("slug")
            row = tk.Frame(lib, bg="#2a221b" if on else C["panel"], cursor="hand2")
            row.pack(fill="x", padx=6, pady=2)
            top_ = tk.Frame(row, bg=row["bg"])
            top_.pack(fill="x", padx=6, pady=(6, 0))
            if it["wav"]:
                pb = tk.Label(top_, text=" ▶ ", bg=C["ink"], fg="#14110f", font=fsm,
                              cursor="hand2")
                pb.pack(side="left", padx=(0, 6))
                pb.bind("<Button-1>", lambda _e, w=it["wav"]: _play(w))
            nm = tk.Label(top_, text=it["name"], bg=row["bg"], fg=C["ink"], font=fh,
                          anchor="w")
            nm.pack(side="left")
            sub_ = tk.Label(row, text=it["line"] + ("  ·  %s's voice" % cname.get(it["character"], it["character"])
                                                    if it.get("character") else ""),
                            bg=row["bg"], fg=C["muted"], font=fsm, anchor="w",
                            wraplength=260, justify="left")
            sub_.pack(fill="x", padx=8, pady=(0, 6))
            for w_ in (row, top_, nm, sub_):
                w_.bind("<Button-1>", lambda _e, vv=it["voice"]: _select(vv))

    def _paint_aud():
        for w_ in aud_cells.winfo_children():
            w_.destroy()
        for k, wav in enumerate(st["aud"]):
            if not wav:
                continue
            cell = tk.Frame(aud_cells, bg=C["panel2"])
            cell.pack(side="left", padx=(0, 8))
            pl = tk.Label(cell, text=" ▶ %s " % "ABC"[k], bg=C["panel2"], fg=C["ink"],
                          font=fh, cursor="hand2")
            pl.pack(side="left", pady=4)
            pl.bind("<Button-1>", lambda _e, w=wav: _play(w))
            kp = tk.Label(cell, text=" Keep ", bg=C["ink"], fg="#14110f", font=fsm,
                          cursor="hand2")
            kp.pack(side="left", padx=4)
            kp.bind("<Button-1>", lambda _e, w=wav: _keep(w))

    def _paint_tests():
        for w_ in test_res.winfo_children():
            w_.destroy()
        kept = bool(st["v"].get("slug") and dlg.voice_wav(st["v"], "neutral"))
        b_test.configure(state="normal" if kept else "disabled")
        if not kept:
            lab(test_res, "keep an audition first — then test any line here",
                fg=C["faint"]).pack(side="left")
            return
        for eng, nm in (("qwen", "Qwen3-TTS"), ("fish", "Fish S2-Pro")):
            got = st["tests"].get(eng)
            cell = tk.Frame(test_res, bg=C["panel2"])
            cell.pack(side="left", padx=(0, 10))
            if got and not str(got).startswith("error"):
                pl = tk.Label(cell, text=" ▶ %s " % nm, bg=C["panel2"], fg=C["ink"],
                              font=fh, cursor="hand2")
                pl.pack(side="left", pady=4)
                pl.bind("<Button-1>", lambda _e, w=got: _play(w))
            else:
                tk.Label(cell, text=" %s: %s " % (nm, (str(got)[7:60] if got else "—")),
                         bg=C["panel2"], fg=C["ochre"] if got else C["faint"],
                         font=fsm).pack(side="left", pady=4)
            tk.Label(cell, text=vl.licence(eng), bg=C["panel2"],
                     fg=C["ochre"] if eng == "fish" else C["faint"], font=fsm).pack(
                side="left", padx=(0, 6))
        lab(test_box, "the mood changes Fish's reading; Qwen keeps the voice's own way of "
                      "speaking",
            fg=C["faint"]).pack(anchor="w", pady=(4, 0)) if not st.get("_mood_note") else None
        st["_mood_note"] = True

    def _paint_link():
        m = char_menu["menu"]
        m.delete(0, "end")
        try:
            import scene_engine as se
            chars = se.list_characters()
        except Exception:
            chars = []
        for s_, meta in chars:
            m.add_command(label=meta.get("name") or s_, command=lambda s=s_, n=(
                meta.get("name") or s_): (char_var.set(n), st.__setitem__("char", s)))
        mine = [(s_, meta) for s_, meta in chars if meta.get("voice_slug") == st["v"].get("slug")
                and st["v"].get("slug")]
        if mine:
            char_var.set(mine[0][1].get("name") or mine[0][0])
            st["char"] = mine[0][0]
            link_lbl.configure(text="✓ %s's voice — Dialogue and Episode use it"
                               % ", ".join(m_.get("name") or s_ for s_, m_ in mine),
                               fg=C["moss"])
        else:
            char_var.set("pick a character")
            st["char"] = None
            link_lbl.configure(text="", fg=C["muted"])

    # ================================================================== actions
    def _select(v):
        _show_build()
        _fill(dict(v))
        _paint_lib()
        say("%s — ▶ in the library to hear it; change anything and Audition for "
            "a new take" % (v.get("name") or v.get("slug")))

    def _new():
        _show_build()
        _fill(vl.blank("New voice"))
        _paint_lib()
        say("describe them — pick an accent, move the sliders — then Audition 3")

    def _audition():
        v = _read()

        def run(job):
            return vl.audition(v, 3, text=sample_var.get(),
                               progress=lambda m: job.update(progress=m))

        def done(wavs):
            st["aud"] = wavs
            _paint_aud()
            say("three takes on that description — ▶ to listen, Keep the right one")
            if wavs and wavs[0]:
                _play(wavs[0])
        _job(run, done, "auditioning", retry=_audition)

    def _keep(wav):
        v = _read()
        slug = vl.keep(v, wav, st.get("char"))
        nv = dlg.load_voice(slug)
        _fill(nv)
        _paint_lib()
        say("kept — %s is in the library. Test a line, or make it a character's voice"
            % nv.get("name"))

    def _test():
        v = st["v"]
        text, mood = test_var.get().strip(), mood_var.get()
        if not text:
            say("type a line to test", warn=True)
            return

        def run(job):
            return vl.test(v, text, mood, progress=lambda m: job.update(progress=m))

        def done(out):
            st["tests"] = out
            _paint_tests()
            ok = [e for e, w in out.items() if w and not str(w).startswith("error")]
            say("said it in %s" % " and ".join("Fish" if e == "fish" else "Qwen" for e in ok)
                if ok else "neither engine came back — see the notes", warn=not ok)
            if ok:
                _play(out[ok[0]])
        _job(run, done, "testing the line", retry=_test)

    def _link():
        if not (st["v"].get("slug") and dlg.voice_wav(st["v"], "neutral")):
            say("keep an audition first — there's no voice to give them yet", warn=True)
            return
        if not st.get("char"):
            say("pick the character first", warn=True)
            return
        vl.link(st["v"]["slug"], st["char"])
        st["v"] = dlg.load_voice(st["v"]["slug"])
        _paint_link()
        _paint_lib()
        say("%s now speaks with %s" % (char_var.get(), st["v"].get("name")))

    def _show_clone():
        build.pack_forget()
        clonep.pack(fill="both", expand=True)
        say("pick a recording of one voice")

    def _show_build():
        clonep.pack_forget()
        build.pack(fill="both", expand=True)

    def _pick_clip():
        from tkinter import filedialog            # handler-time import (AGENTS.md)
        p = filedialog.askopenfilename(
            parent=win, title="A recording of the voice",
            filetypes=[("Audio or video", "*.wav *.mp3 *.m4a *.aif *.aiff *.flac *.ogg *.mp4 "
                                          "*.mov"), ("All files", "*.*")])
        if p:
            cl_path.set(p)
            cl_lbl.configure(text=os.path.basename(p), fg=C["ink"])
            if not cl_name.get().strip():
                cl_name.set(os.path.splitext(os.path.basename(p))[0])

    def _clone():
        if not cl_path.get():
            say("choose a recording first", warn=True)
            return
        if not cl_ok.get():
            say("tick the box: only clone a voice you have the right to use", warn=True)
            return
        name, src, text = cl_name.get(), cl_path.get(), cl_text.get()

        def run(job):
            return vl.clone(name, src, transcript=text, language=lang_var.get(),
                            progress=lambda m: job.update(progress=m))

        def done(slug):
            cl_path.set("")
            cl_text.set("")
            cl_ok.set(False)
            cl_lbl.configure(text="no file yet", fg=C["muted"])
            _select(dlg.load_voice(slug))
            say("cloned — test a line in both engines below")
        _job(run, done, "making the voice", retry=_clone)

    # ================================================================== jobs + audio
    def _job(fn, done, label, retry=None):
        if st["busy"]:
            say("still busy: %s" % st["busy"], warn=True)
            return
        job = {"done": False, "out": None, "err": None, "exc": None, "progress": "",
               "t0": time.time()}
        st["busy"] = label
        for b_ in (b_aud, b_test, b_make_clone):
            b_.configure(state="disabled")

        def work():
            try:
                job["out"] = fn(job)
            except Exception as exc:
                job["exc"] = exc
                job["err"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["done"] = True

        def poll():
            if not job["done"]:
                say("%s… %s  %.0f s" % (label, job["progress"], time.time() - job["t0"]))
                win.after(400, poll)
                return
            st["busy"] = None
            for b_ in (b_aud, b_make_clone):
                b_.configure(state="normal")
            _paint_tests()
            try:
                import cards as _cards
                threading.Thread(target=_cards.release, args=("voices",), daemon=True).start()
            except Exception:
                _cards = None
            if type(job["exc"]).__name__ == "CardBusy" and retry:
                import cards_ui
                cards_ui.handle(win, job["exc"], ctx, retry=retry, say=say, owner="voices")
                return
            if job["err"]:
                say("%s failed: %s" % (label, _cards.explain(job["err"]) if _cards
                                        else job["err"][:160]), warn=True)
                return
            done(job["out"])
        threading.Thread(target=work, daemon=True).start()
        win.after(400, poll)

    def _play(path):
        _stop()
        if not path or not os.path.exists(path):
            say("nothing to play", warn=True)
            return
        try:
            st["player"] = subprocess.Popen(["afplay", path])
        except Exception:
            st["player"] = None
        st["played"] = path

    def _stop():
        p = st.get("player")
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
        st["player"] = None

    # ================================================================== start
    win.protocol("WM_DELETE_WINDOW", lambda: (_stop(), win.destroy()))
    _fill(st["v"])
    _paint_lib()
    say("pick a voice on the left, or build a new one")
    if ctx.get("btn_colors"):
        try:
            ctx["btn_colors"](win)
        except Exception:
            pass
    def _for_character(slug):
        """Open on a character's voice — or a new one drafted from the character."""
        _show_build()
        meta = {}
        try:
            import scene_engine as se
            meta = dict(se.list_characters()).get(slug) or {}
        except Exception:
            pass
        v = dlg.load_voice(meta.get("voice_slug")) if meta.get("voice_slug") else None
        if v:
            _fill(v)
            say("%s's voice \u2014 change anything, audition again, or test it in Fish"
                % (meta.get("name") or slug))
        else:
            _fill(vl.from_character(slug))
            title.configure(text="A voice for %s" % (meta.get("name") or slug))
            say("drafted from %s's description \u2014 adjust it, then Audition 3"
                % (meta.get("name") or slug))
        st["char"] = slug
        char_var.set(meta.get("name") or slug)
        _paint_lib()

    win.vo = {"st": st, "new": _new, "for_character": _for_character, "select": _select, "read": _read, "fill": _fill,
              "audition": _audition, "keep": _keep, "test": _test, "link": _link,
              "clone": _clone, "show_clone": _show_clone, "name_var": name_var,
              "acc_var": acc_var, "notes_var": notes_var, "age_var": age_var,
              "g_var": g_var, "lang_var": lang_var, "sl_vars": sl_vars,
              "sample_var": sample_var, "fishd_var": fishd_var, "test_var": test_var, "mood_var": mood_var,
              "cl_path": cl_path, "cl_name": cl_name, "cl_text": cl_text, "cl_ok": cl_ok,
              "desc_lbl": desc_lbl, "lib": lib, "aud_cells": aud_cells, "test_res": test_res,
              "status": status, "char_var": char_var, "link_lbl": link_lbl,
              "changed": _changed, "save_settings": _save_settings, "clonep": clonep, "build": build, "title": title}
    return win
