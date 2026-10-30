"""The DIALOGUE screen, rebuilt (2026-10-17) — "the page" with audition casting.

Ryan: "i still think the dialogue needs a better UI for me to use. should be intuitive" ->
"do what would be best for dialogue". The old window made you think like the software (a raw
text box, a separate table of the same lines, a voice bank that didn't know your Characters,
"◀ 2/3 ▶" takes, TRY A LINE bolted on top). This one is the screenplay:

    Dialogue  [The bread argument ▾]  New scene  Write it for me…  Edit as text      Close
    CAST  (G) Gustav · his voice ▶   (M) Robin · her voice ▶   (B) Baker · needs a voice [Cast]
    ┌──────────────────────────────────────────────────────────────────────────────────┐
    │ GUSTAV (flat)    Twelve kronor. For a loaf.                              ~~~~ ▶ │
    │ ROBIN   (warm)    It's rye, Gustav. Good rye.                             ~~~~ ▶ │
    │ GUSTAV (mutter)  Outrageous.                       <- click: words · mood ·      │
    │                  redo: angrier deadpan whisper …  · takes ① ② ③ · ↑ ↓ delete       │
    │ [BAKER ▾] [type the next line — Enter adds it]                    [Add] [Speak] │
    └──────────────────────────────────────────────────────────────────────────────────┘
    [Make the 2 missing lines · ~25 s]  [▶ Play the scene]  [Send to Episode →]   More ▾

CASTING is an audition: three reads of the character's own voice description (from the
Characters bible when it has one), keep one, and it is saved to the CHARACTER — every later
scene sounds the same. Everything else saves itself. The engine is scene_engine (Tk-free);
the rig is behind it, and a busy card brings up the card prompt (cards_ui).
"""
import os
import sys
import subprocess
import threading
import time

import dialogue as dlg
import scene_engine as se

# macOS draws its own white buttons and ignores bg: light text on them vanished
# ("Stop after this step" was blank on the MacBook) - dark text there, always
_MAC = sys.platform == "darwin"

PALETTE = ("#d9a05b", "#8fb3a8", "#b49ad1", "#c98a8a", "#9fb36a", "#7fa6d1", "#d1b36a",
           "#c49ad1")
NEW_SPEAKER = "+ new speaker…"



def latest_scene(names):
    """The scene changed most recently (its project.json's mtime). It used to be the LAST
    NAME ALPHABETICALLY, so the window opened "untitled" instead of the scene just made."""
    def mt(n):
        try:
            return os.path.getmtime(os.path.join(dlg.dialogue_dir(), n, "project.json"))
        except Exception:
            return 0
    return max(names, key=mt) if names else None

def open_dialogue2(root, ctx):
    import tkinter as tk
    import tkinter.font as tkfont

    C = ctx["C"]
    f, fh, fsm = ctx["fonts"]["f"], ctx["fonts"]["fh"], ctx["fonts"]["fsm"]
    fmono = ctx["mono"]["m"]
    btn_colors = ctx.get("btn_colors")
    fbig = tkfont.Font(family=fh.cget("family"), size=max(15, int(fh.cget("size")) + 5),
                       weight="bold")
    fline = tkfont.Font(family=f.cget("family"), size=max(14, int(f.cget("size")) + 2))

    import dock as _dock                 # a tab in the main window (Ryan: tabs)
    win = _dock.window(root)
    win.title("Dialogue")
    win.configure(bg=C["bg"])
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1240, _sw - 40), min(820, _sh - 110)))
    win.minsize(1060, 680)

    st = {"pj": None, "sel": None, "busy": None, "player": None, "text_mode": False,
          "aud": {}, "colors": {}, "save_job": None, "step": "Lines"}

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

    def colour(spk):
        if spk not in st["colors"]:
            st["colors"][spk] = PALETTE[len(st["colors"]) % len(PALETTE)]
        return st["colors"][spk]

    # ------------------------------------------------------------------ header
    hd = tk.Frame(win, bg=C["panel"])
    hd.pack(fill="x")
    tk.Label(hd, text="Dialogue", bg=C["panel"], fg=C["ink"], font=fbig).pack(
        side="left", padx=(16, 10), pady=10)
    proj_var = tk.StringVar(value="")
    proj_menu = tk.OptionMenu(hd, proj_var, "")
    proj_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, width=22,
                        anchor="w", highlightthickness=0, borderwidth=0,
                        activebackground=C["rust"])
    proj_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    proj_menu.pack(side="left")
    button(hd, "New scene", lambda: _new()).pack(side="left", padx=(8, 0))
    button(hd, "Close", lambda: win.destroy()).pack(side="right", padx=12)
    # THE THREE STEPS (Ryan: "the dialogue UI still doesnt really make sense to me"). The old
    # page showed writing, casting and performing controls at once, and its big gold button
    # (Make) could not work until the voices were cast — so it bounced you somewhere else.
    # Now each step shows only its own controls and ONE button for the next move.
    STEPS = (("Lines", "1  Lines"), ("Voices", "2  Voices"), ("Make", "3  Make & play"))
    step_row = tk.Frame(hd, bg=C["panel"])
    step_row.pack(side="right", padx=(0, 18))
    chips = {}
    for _k, _t in STEPS:
        _c = tk.Label(step_row, text=_t, bg=C["panel"], fg=C["muted"], font=fh, padx=14,
                      pady=6, cursor="hand2")
        _c.pack(side="left", padx=(0, 4))
        _c.bind("<Button-1>", lambda _e, k=_k: _go(k))
        chips[_k] = _c
    tk.Frame(win, bg=C["line"], height=1).pack(fill="x")

    # ------------------------------------------------------------------ cast strip
    cast_row = tk.Frame(win, bg=C["bg"])
    cast_row.pack(fill="x", padx=16, pady=(10, 2))
    shead = tk.Frame(win, bg=C["bg"])
    shead.pack(fill="x", padx=18, pady=(6, 0))
    step_title = tk.Label(shead, text="", bg=C["bg"], fg=C["ink"], font=fbig, anchor="w")
    step_title.pack(side="left")
    step_hint = tk.Label(shead, text="", bg=C["bg"], fg=C["muted"], font=fsm, anchor="w")
    step_hint.pack(side="left", padx=(12, 0))
    tools = tk.Frame(shead, bg=C["bg"])          # step 1 only
    b_write = button(tools, "Write it for me…", lambda: _write())
    b_write.pack(side="left", padx=(0, 6))
    b_text = button(tools, "Paste / edit as text", lambda: _toggle_text())
    b_text.pack(side="left")
    status = tk.Label(win, text="", bg=C["bg"], fg=C["muted"], font=fsm, anchor="w")
    status.pack(fill="x", padx=18, pady=(2, 4))

    # ------------------------------------------------------------------ the make bar (bottom)
    bar = tk.Frame(win, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    bar.pack(side="bottom", fill="x")
    b_next = button(bar, "Next →", lambda: _next(), primary=True)
    b_make = button(bar, "Make the missing lines", lambda: _make(), primary=True)
    b_play = button(bar, "▶ Play the scene", lambda: _play_scene())
    b_stop = button(bar, "■ Stop", lambda: _stop_play())
    b_send = button(bar, "Send to Episode →", lambda: _send())
    bar_info = tk.Label(bar, text="", bg=C["panel"], fg=C["muted"], font=fsm)
    more_lbl = tk.Label(bar, text="More ▾", bg=C["panel"], fg=C["ochre"], font=fsm,
                        cursor="hand2")
    more_lbl.pack(side="right", padx=14)
    more = tk.Frame(win, bg=C["panel"])          # gap + engine, folded
    gap_var = tk.IntVar(value=350)
    eng_var = tk.StringVar(value=dlg.engine_label(dlg.DEFAULT_ENGINE))
    lab(more, "pause between lines").pack(side="left", padx=(16, 4), pady=6)
    tk.Scale(more, from_=0, to=1200, resolution=50, orient="horizontal", variable=gap_var,
             length=180, showvalue=True, bg=C["panel"], fg=C["ink"], highlightthickness=0,
             troughcolor="#100e0c", font=fsm, command=lambda _v: _gap_changed()).pack(
        side="left")
    lab(more, "ms      voice engine").pack(side="left", padx=(4, 4))
    _old = tk.Label(more, text="open the old Dialogue window", bg=C["panel"], fg=C["faint"],
                    font=fsm, cursor="hand2")
    _old.pack(side="right", padx=16)
    _old.bind("<Button-1>", lambda _e: ctx.get("open_old") and ctx["open_old"]())
    _em = tk.OptionMenu(more, eng_var, *[dlg.engine_label(e) for e, _l in dlg.ENGINES],
                        command=lambda *_: _engine_changed())
    _em.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                  highlightthickness=0, borderwidth=0, activebackground=C["rust"])
    _em["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _em.pack(side="left")

    def _toggle_more(_e=None):
        if more.winfo_ismapped():
            more.pack_forget()
            more_lbl.configure(text="More ▾")
        else:
            more.pack(side="bottom", fill="x", before=bar)
            more_lbl.configure(text="More ▴")
    more_lbl.bind("<Button-1>", _toggle_more)

    # ------------------------------------------------------------------ body: page / text / cast
    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=16, pady=(0, 10))
    page = tk.Frame(body, bg=C["bg"])
    vsb = tk.Scrollbar(page, orient="vertical")
    vsb.pack(side="right", fill="y")
    can = tk.Canvas(page, bg=C["bg"], highlightthickness=0, yscrollcommand=vsb.set)
    can.pack(side="left", fill="both", expand=True)
    vsb.configure(command=can.yview)
    cards = tk.Frame(can, bg=C["bg"])
    _cw = can.create_window((0, 0), window=cards, anchor="nw")
    cards.bind("<Configure>", lambda e: can.configure(scrollregion=can.bbox("all")))

    def _centre(e):
        w_ = min(900, e.width - 8)                 # a readable page, centred in the window
        can.itemconfigure(_cw, width=w_)
        can.coords(_cw, max(0, (e.width - w_) // 2), 0)
    can.bind("<Configure>", _centre)
    try:
        ctx["mod"].scoped_wheel(win, can)
    except Exception:
        pass
    textp = tk.Frame(body, bg=C["bg"])
    lab(textp, "The whole script as text — one line each, NAME: words. "
               "(direction) at the start sets the mood.", fg=C["faint"]).pack(fill="x")
    t_script = tk.Text(textp, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                       font=fline, padx=10, pady=8, insertbackground=C["ink"])
    t_script.pack(fill="both", expand=True, pady=(4, 0))
    castp = tk.Frame(body, bg=C["bg"])

    # ================================================================== painting
    def _voices():
        return {v["slug"]: v for v in dlg.list_voices()}

    def _paint_cast():
        for w_ in cast_row.winfo_children():
            w_.destroy()
        pj = st["pj"]
        lab(cast_row, "CAST", font=fmono).pack(side="left", padx=(0, 10))
        if not pj or not pj.get("lines"):
            lab(cast_row, "type a line below, or Write it for me — the cast appears "
                          "here", fg=C["faint"]).pack(side="left")
            return
        voices = _voices()
        for spk in dlg.speakers(pj["lines"]):
            vslug = (pj.get("cast") or {}).get(spk)
            v = voices.get(vslug)
            chip = tk.Frame(cast_row, bg=C["panel"], highlightthickness=1,
                            highlightbackground=C["line"] if v else C["ochre"])
            chip.pack(side="left", padx=(0, 8))
            tk.Label(chip, text=spk[:1], bg=colour(spk), fg="#14110f", font=fh, width=2).pack(
                side="left", padx=(4, 6), pady=4)
            tk.Label(chip, text=spk.title(), bg=C["panel"], fg=C["ink"], font=fh).pack(
                side="left")
            if v:
                _vn = (v.get("name") or vslug)
                tk.Label(chip, text=" · %s" % ("voice" if dlg.normalise_speaker(_vn)
                                                    == spk else _vn[:18]),
                         bg=C["panel"], fg=C["muted"], font=fsm).pack(side="left")
                _p = tk.Label(chip, text=" ▶ ", bg=C["panel"], fg=C["ink"], font=fsm,
                              cursor="hand2")
                _p.pack(side="left")
                _p.bind("<Button-1>", lambda _e, vv=v: _play(dlg.voice_wav(vv, "neutral")))
            else:
                tk.Label(chip, text=" · needs a voice", bg=C["panel"], fg=C["ochre"],
                         font=fsm).pack(side="left")
            _c = tk.Label(chip, text="Recast" if v else "Cast", bg=C["ink"] if not v else
                          C["panel2"], fg="#14110f" if not v else C["ink"], font=fsm, padx=8,
                          cursor="hand2")
            _c.pack(side="left", padx=(6, 4), pady=4)
            _c.bind("<Button-1>", lambda _e, s=spk: _open_casting(s))

    def _line_state(i, li):
        if li.get("error"):
            return "failed — redo it", C["ochre"]
        if not li.get("takes"):
            return "not made yet", C["faint"]
        if li.get("stale"):
            return "old voice \u2014 Make reads it again", C["ochre"]
        tk_ = li["takes"][li.get("kept", 0)] if li.get("kept", 0) < len(li["takes"]) else {}
        sc = tk_.get("score")
        if sc is not None and sc < dlg.PASS_SCORE:
            return "check the words (heard: %s)" % (tk_.get("heard") or "?")[:28], C["ochre"]
        n = len(li["takes"])
        return ("%d takes" % n) if n > 1 else "made", C["moss"]

    def _paint_page():
        for w_ in cards.winfo_children():
            w_.destroy()
        pj = st["pj"]
        lines = (pj or {}).get("lines") or []
        st["card_w"] = {}
        for i, li in enumerate(lines):
            spk = dlg.normalise_speaker(li["speaker"])
            sel = i == st["sel"]
            card = tk.Frame(cards, bg="#1f1a15" if sel else C["panel"], highlightthickness=1,
                            highlightbackground=C["ochre"] if sel else C["line"],
                            cursor="hand2")
            card.pack(fill="x", pady=(0, 6))
            card.columnconfigure(1, weight=1)
            who = tk.Frame(card, bg=card["bg"])
            who.grid(row=0, column=0, sticky="nw", padx=(12, 10), pady=8)
            tk.Label(who, text=spk, bg=card["bg"], fg=colour(spk), font=fh, anchor="w").pack(
                anchor="w")
            mood = li.get("mood") or (li.get("direction") or "")[:14]
            if mood and mood != "neutral":
                tk.Label(who, text="(%s)" % mood, bg=card["bg"], fg=C["muted"],
                         font=fsm).pack(anchor="w")
            words = tk.Label(card, text=li["text"], bg=card["bg"], fg=C["ink"], font=fline,
                             anchor="w", justify="left", wraplength=520)
            words.grid(row=0, column=1, sticky="w", pady=8)
            right = tk.Frame(card, bg=card["bg"])
            right.grid(row=0, column=2, sticky="e", padx=10)
            s_txt, s_fg = _line_state(i, li)
            if st["step"] == "Make":
                tk.Label(right, text=s_txt, bg=card["bg"], fg=s_fg, font=fsm).pack(
                    side="left", padx=(0, 8))
            if li.get("takes"):
                pb = tk.Label(right, text=" ▶ ", bg=C["ink"], fg="#14110f", font=fh,
                              cursor="hand2")
                pb.pack(side="left")
                pb.bind("<Button-1>", lambda _e, k=i: _play(se.take_path(st["pj"], k)))
            for w_ in (card, who, words):
                w_.bind("<Button-1>", lambda _e, k=i: _select(k))
            st["card_w"][i] = card
            if sel:
                _paint_editor(card, i, li)
        if st["step"] != "Lines":
            _more = tk.Label(cards, text="+ add or change lines (step 1)", bg=C["bg"],
                             fg=C["ochre"], font=fsm, cursor="hand2")
            _more.pack(anchor="w", pady=(4, 10))
            _more.bind("<Button-1>", lambda _e: _go("Lines"))
            st["add_entry"] = None
            _refresh_bar()
            return
        # the add row
        add = tk.Frame(cards, bg=C["bg"], highlightthickness=1, highlightbackground=C["line"])
        add.pack(fill="x", pady=(4, 10))
        spk_opts = (dlg.speakers(lines) if lines else []) + [NEW_SPEAKER]
        if add_spk.get() not in spk_opts:
            add_spk.set(_next_speaker(lines))
        m = tk.OptionMenu(add, add_spk, *spk_opts, command=lambda v: _pick_speaker(v))
        m.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, width=10,
                    highlightthickness=0, borderwidth=0, activebackground=C["rust"])
        m["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
        m.pack(side="left", padx=8, pady=8)
        e = tk.Entry(add, textvariable=add_txt, bg="#100e0c", fg=C["ink"], relief="flat",
                     font=fline, insertbackground=C["ink"])
        e.pack(side="left", fill="x", expand=True, ipady=4)
        e.bind("<Return>", lambda _e: _add(speak=False))
        button(add, "Add line", lambda: _add(speak=False)).pack(side="left", padx=(8, 8))
        st["add_entry"] = e
        _refresh_bar()

    def _paint_editor(card, i, li):
        ed = tk.Frame(card, bg="#211d19")
        ed.grid(row=1, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 10))
        r1 = tk.Frame(ed, bg=ed["bg"])
        r1.pack(fill="x", padx=10, pady=(8, 4))
        lab(r1, "Words").pack(side="left")
        ev = tk.StringVar(value=li["text"])
        ee = tk.Entry(r1, textvariable=ev, bg="#100e0c", fg=C["ink"], relief="flat",
                      font=fline, insertbackground=C["ink"])
        ee.pack(side="left", fill="x", expand=True, padx=8, ipady=3)
        ee.bind("<Return>", lambda _e: _save_words(i, ev.get()))
        button(r1, "Save words", lambda: _save_words(i, ev.get())).pack(side="left")
        st["editor"] = {"words": ev, "entry": ee}
        if st["step"] == "Make":
            # redo chips in two rows — one row ran off the card ("…gain, same moo")
            chips_ = list(se.REDO) + [("again, same mood", None)]
            half = (len(chips_) + 1) // 2
            for ri, part in enumerate((chips_[:half], chips_[half:])):
                r2 = tk.Frame(ed, bg=ed["bg"])
                r2.pack(fill="x", padx=10, pady=(4, 0))
                lab(r2, "Redo it…" if ri == 0 else "", width=8).pack(side="left")
                for label, mood in part:
                    b_ = tk.Label(r2, text=label, bg=C["panel"], fg=C["ink"], font=fsm,
                                  padx=9, pady=3, cursor="hand2", highlightthickness=1,
                                  highlightbackground=C["line"])
                    b_.pack(side="left", padx=(0, 4))
                    b_.bind("<Button-1>", lambda _e, m=mood: _redo(i, m))
        r3 = tk.Frame(ed, bg=ed["bg"])
        r3.pack(fill="x", padx=10, pady=(6, 8))
        takes = li.get("takes") or []
        if st["step"] == "Make":
            lab(r3, "Takes").pack(side="left", padx=(0, 6))
            if not takes:
                lab(r3, "none yet", fg=C["faint"]).pack(side="left")
        for k, _t in (enumerate(takes) if st["step"] == "Make" else ()):
            on = k == li.get("kept", 0)
            t_ = tk.Label(r3, text=" %d " % (k + 1), bg=C["moss"] if on else C["panel"],
                          fg="#14110f" if on else C["ink"], font=fh, cursor="hand2")
            t_.pack(side="left", padx=(0, 4))
            t_.bind("<Button-1>", lambda _e, kk=k: _keep(i, kk))
        for txt, cmd in (("Delete line", lambda: _delete(i)), ("↓", lambda: _move(i, 1)),
                         ("↑", lambda: _move(i, -1))):
            b_ = tk.Label(r3, text=txt, bg=ed["bg"], fg=C["ochre"], font=fsm, cursor="hand2")
            b_.pack(side="right", padx=6)
            b_.bind("<Button-1>", lambda _e, c=cmd: c())

    def _paint_steps():
        pj = st["pj"]
        lines = (pj or {}).get("lines") or []
        done = {"Lines": bool(lines), "Voices": bool(lines) and not se.uncast(pj),
                "Make": bool(lines) and not se.missing(pj)}
        for k, c_ in chips.items():
            on = k == st["step"]
            c_.configure(text=("✓ " if done[k] and not on else "") + dict(STEPS)[k],
                         bg=C["ink"] if on else C["panel"],
                         fg="#14110f" if on else (C["ink"] if done[k] else C["muted"]))
        n_un = se.uncast(pj) if lines else []
        titles = {
            "Lines": ("Write the lines", "who says what — pick the speaker, type the "
                                         "words, Enter adds it. Click a line to change it."),
            "Voices": ("Give each speaker a voice", "audition three, keep the one that sounds "
                                                  "right — it's saved to the character"),
            "Make": ("Make it and listen", "make the lines, play the scene, click a line to "
                                          "redo it in another mood")}
        step_title.configure(text=titles[st["step"]][0])
        step_hint.configure(text=titles[st["step"]][1])
        if st["step"] == "Lines":
            tools.pack(side="right")
        else:
            tools.pack_forget()
        for b_ in (b_next, b_make, b_play, b_stop, b_send, bar_info):
            b_.pack_forget()
        if st["step"] == "Lines":
            b_next.configure(text="Next: voices →" if n_un or not lines
                             else "Next: make & play →")
            b_next.pack(side="left", padx=(14, 8), pady=10)
            bar_info.configure(text=("%d line%s · %s" % (
                len(lines), "" if len(lines) == 1 else "s",
                ", ".join(s.title() for s in dlg.speakers(lines)))) if lines else
                "no lines yet")
            bar_info.pack(side="left", padx=8)
        elif st["step"] == "Voices":
            b_next.configure(text="Next: make & play →")
            b_next.pack(side="left", padx=(14, 8), pady=10)
            bar_info.configure(text=("still needs a voice: %s" % ", ".join(
                s.title() for s in n_un)) if n_un else "everyone has a voice")
            bar_info.pack(side="left", padx=8)
        else:
            b_make.pack(side="left", padx=(14, 8), pady=10)
            b_play.pack(side="left")
            b_stop.pack(side="left", padx=(4, 8))
            b_send.pack(side="left")

    def _go(step):
        """Show one of the three steps."""
        if step == "Voices":
            _open_casting()
            return
        if st["text_mode"] and step != "Lines":
            _toggle_text()                     # leaving the text box applies it
        st["step"] = step
        _show("page")
        _paint_all()

    def _next():
        pj = st["pj"]
        if st["step"] == "Lines":
            if not pj or not pj.get("lines"):
                say("type a line first — or Write it for me", warn=True)
                return
            if st["text_mode"]:
                _toggle_text()
            _go("Voices" if se.uncast(st["pj"]) else "Make")
        else:
            un = se.uncast(pj) if pj else []
            if un:
                say("%s still need%s a voice — Audition, then Keep one" % (
                    ", ".join(s.title() for s in un), "s" if len(un) == 1 else ""), warn=True)
                return
            _go("Make")

    def _refresh_bar():
        _paint_steps()
        pj = st["pj"]
        miss = se.missing(pj) if pj else []
        if not pj or not pj.get("lines"):
            b_make.configure(text="Make the missing lines")
        elif not miss:
            b_make.configure(text="Every line is made")
        else:
            try:
                est = se.estimate(pj, miss)
            except Exception:
                est = 0
            b_make.configure(text="Make the %d missing line%s%s" % (
                len(miss), "" if len(miss) == 1 else "s",
                (" · ~%d s" % est) if est else ""))

    def _paint_all():
        _paint_cast()
        if not st["text_mode"] and not castp.winfo_ismapped():
            _paint_page()
        _refresh_bar()

    # ================================================================== projects
    add_spk = tk.StringVar(value="")
    add_txt = tk.StringVar(value="")

    def _relist():
        m = proj_menu["menu"]
        m.delete(0, "end")
        for slug in dlg.list_projects():
            m.add_command(label=slug, command=lambda s=slug: _open(s))
        proj_var.set(st["pj"]["slug"] if st["pj"] else "— new scene —")

    def _save():
        if st["pj"] is not None:
            dlg.save_project(st["pj"])

    def _ensure_project():
        if st["pj"] is None:
            base = "scene-" + time.strftime("%Y%m%d-%H%M%S")
            slug, k = base, 2
            while slug in dlg.list_projects():
                slug, k = "%s-%d" % (base, k), k + 1
            pj = dlg.new_project(slug, "")
            pj["slug"] = slug
            pj["engine"] = dlg.DEFAULT_ENGINE
            st["pj"] = pj
            _relist()
        return st["pj"]

    def _open(slug):
        pj = dlg.load_project(slug)
        if pj is None:
            say("could not open %s" % slug, warn=True)
            return
        st.update(pj=se.auto_cast(pj), sel=None, colors={})
        gap_var.set(int(pj.get("gap_ms", 350)))
        eng_var.set(dlg.engine_label(pj.get("engine") or dlg.DEFAULT_ENGINE))
        _relist()
        lines_ = st["pj"].get("lines") or []
        if not lines_:
            st["step"] = "Lines"
        elif se.uncast(st["pj"]):
            st["step"] = "Voices"
        else:
            st["step"] = "Make"
        if st["step"] == "Voices":
            _open_casting()
        else:
            _show("page")
            _paint_all()
        n_un = se.uncast(st["pj"])
        say("%s · %d lines%s" % (slug, len(pj.get("lines") or []),
                                     (" · %s need%s a voice: Cast" % (
                                         ", ".join(s.title() for s in n_un),
                                         "s" if len(n_un) == 1 else ""))
                                     if n_un else " · every change saves itself"))

    def _new():
        st.update(pj=None, sel=None, colors={}, step="Lines")
        add_txt.set("")
        _relist()
        _show("page")
        _paint_all()
        say("a new scene — pick who speaks, type a line, press Enter")
        if st.get("add_entry"):
            st["add_entry"].focus_set()

    def _next_speaker(lines):
        sp = dlg.speakers(lines) if lines else []
        if not sp:
            chars = se.list_characters()
            return dlg.normalise_speaker((chars[0][1].get("name") or chars[0][0]).split(" ")[0]) \
                if chars else NEW_SPEAKER
        last = dlg.normalise_speaker(lines[-1]["speaker"])
        others = [s for s in sp if s != last]
        return others[0] if others else last

    def _pick_speaker(v):
        if v != NEW_SPEAKER:
            return
        from tkinter import simpledialog            # handler-time import (AGENTS.md)
        names = [m.get("name") or s for s, m in se.list_characters()]
        nm = simpledialog.askstring("New speaker", "Who is speaking?%s" % (
            ("\n(your characters: %s)" % ", ".join(names[:8])) if names else ""), parent=win)
        add_spk.set(dlg.normalise_speaker(nm) if nm else _next_speaker(
            (st["pj"] or {}).get("lines") or []))

    # ================================================================== line actions
    def _commit(pj, msg=None):
        st["pj"] = se.auto_cast(pj)
        _save()
        _paint_all()
        if msg:
            say(msg)

    def _add(speak=False):
        txt = add_txt.get().strip()
        spk = add_spk.get()
        if not txt:
            say("type the line first", warn=True)
            return
        if not spk or spk == NEW_SPEAKER:
            _pick_speaker(NEW_SPEAKER)
            spk = add_spk.get()
            if not spk or spk == NEW_SPEAKER:
                return
        pj = _ensure_project()
        _commit(se.add_line(pj, spk, txt))
        add_txt.set("")
        add_spk.set(_next_speaker(st["pj"]["lines"]))
        _paint_page()
        if st.get("add_entry"):
            st["add_entry"].focus_set()
        n = len(st["pj"]["lines"]) - 1
        if speak:
            _render([n], "missing", "speaking it")
        else:
            say("added — Enter for the next one, or Make the missing lines")

    def _select(i):
        st["sel"] = None if st["sel"] == i else i
        _paint_page()

    def _save_words(i, text):
        if not text.strip():
            say("a line needs words — or Delete line", warn=True)
            return
        if text.strip() == st["pj"]["lines"][i]["text"]:
            return
        _commit(se.edit_line(st["pj"], i, text=text), "new words — this line will be "
                                                      "read again (Make, or a Redo chip)")

    def _keep(i, k):
        se.keep_take(st["pj"], i, k)
        _save()
        _paint_page()
        _play(se.take_path(st["pj"], i))

    def _delete(i):
        st["sel"] = None
        _commit(se.delete_line(st["pj"], i), "line deleted")

    def _move(i, d):
        st["sel"] = max(0, min(len(st["pj"]["lines"]) - 1, i + d))
        _commit(se.move_line(st["pj"], i, d))

    def _redo(i, mood):
        if mood:
            se.set_mood(st["pj"], i, mood)
        _render([i], "all", "redoing line %d%s" % (i + 1, (" " + mood) if mood else ""))

    def _make():
        pj = st["pj"]
        if not pj or not pj.get("lines"):
            say("no lines yet — type one below", warn=True)
            return
        if st["step"] != "Make":
            st["step"] = "Make"
            _show("page")
            _paint_all()
        miss = se.missing(pj)
        if not miss:
            say("every line is made — ▶ Play the scene")
            return
        _render(miss, "all", "making %d line(s)" % len(miss))

    # ================================================================== rig jobs
    def _render(idxs, mode, label):
        pj = st["pj"]
        un = [s for s in se.uncast(pj) if s in {dlg.normalise_speaker(pj["lines"][i]["speaker"])
                                                for i in idxs}]
        if un:
            say("%s need%s a voice first — Cast" % (
                ", ".join(s.title() for s in un), "s" if len(un) == 1 else ""), warn=True)
            _open_casting(un[0])
            return

        def run(job):
            return se.render(pj, idxs, mode, progress=lambda m: job.update(progress=m),
                             submit=ctx.get("submit"), wait_and_collect=ctx.get(
                                 "wait_and_collect"), runs=ctx.get("runs"))

        def done(out):
            _paint_all()
            if idxs and len(idxs) == 1:
                _play(se.take_path(st["pj"], idxs[0]))
            fl_ = out.get("flagged") or []
            say("%d made%s%s" % (out.get("made", 0),
                                 (" · %d to check by ear" % len(fl_)) if fl_ else "",
                                 (" · " + "; ".join(out["notes"])[:120]) if out.get(
                                     "notes") else ""), warn=bool(fl_ or out.get("notes")))
        _job(run, done, label, retry=lambda: _render(idxs, mode, label))

    def _job(fn, done, label, retry=None):
        if st["busy"]:
            say("still busy: %s" % st["busy"], warn=True)
            return
        job = {"done": False, "out": None, "err": None, "exc": None, "progress": "",
               "t0": time.time()}
        st["busy"] = label
        for b_ in (b_make, b_send, b_write):
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
            for b_ in (b_make, b_send, b_write):
                b_.configure(state="normal")
            try:
                import cards as _cards
                threading.Thread(target=_cards.release, args=("dialogue2",),
                                 daemon=True).start()
            except Exception:
                _cards = None
            if type(job["exc"]).__name__ == "CardBusy" and retry:
                say("%s is busy — choose what to do" % _cards.card_name(job["exc"].card),
                    warn=True)
                import cards_ui
                cards_ui.handle(win, job["exc"], ctx, retry=retry, say=say, owner="dialogue2")
                return
            if job["err"]:
                _full = job["err"]
                try:
                    with open(os.path.join(dlg._data(), "forge_lab_errors.log"), "a") as lf:
                        lf.write("%s  dialogue: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                         _full))
                except Exception:
                    pass
                say("%s failed: %s" % (label, _cards.explain(_full) if _cards else _full[:160]),
                    warn=True)
                _paint_all()
                return
            done(job["out"])
        threading.Thread(target=work, daemon=True).start()
        win.after(400, poll)

    # ================================================================== casting
    def _open_casting(focus=None):
        pj = st["pj"]
        if not pj or not pj.get("lines"):
            say("add a line first — the cast comes from who speaks", warn=True)
            return
        if st["text_mode"]:
            _toggle_text()
        st["step"] = "Voices"
        _show("cast")
        for w_ in castp.winfo_children():
            w_.destroy()
        _paint_cast()
        _paint_steps()
        if ctx.get("open_voices"):
            _vl = tk.Label(castp, text="Want an accent, a cloned voice, or to hear it in Fish "
                                       "too?  Build it on the Voices page \u2192",
                           bg=C["bg"], fg=C["ochre"], font=fsm, cursor="hand2", anchor="w")
            _vl.pack(fill="x", pady=(0, 8))
            _vl.bind("<Button-1>", lambda _e: ctx["open_voices"]())
        voices = _voices()
        for spk in dlg.speakers(pj["lines"]):
            cslug, meta = se.character_for(spk)
            row = tk.Frame(castp, bg=C["panel"], highlightthickness=1,
                           highlightbackground=C["ochre"] if spk == focus else C["line"])
            row.pack(fill="x", pady=(0, 8))
            left = tk.Frame(row, bg=C["panel"], width=210)
            left.pack(side="left", fill="y", padx=12, pady=10)
            left.pack_propagate(False)
            tk.Label(left, text=spk.title(), bg=C["panel"], fg=colour(spk), font=fbig,
                     anchor="w").pack(fill="x")
            vs = (pj.get("cast") or {}).get(spk)
            lab(left, ("voice: %s" % (voices[vs].get("name") or vs)) if vs in voices else
                "no voice yet", fg=C["moss"] if vs in voices else C["ochre"]).pack(fill="x")
            lab(left, ("from Characters: %s" % (meta.get("name") or cslug)) if cslug else
                "not a saved character", fg=C["faint"]).pack(fill="x")
            mid = tk.Frame(row, bg=C["panel"])
            mid.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=10)
            # start from: the auditions in hand, else the voice this speaker already has
            # (so Recast keeps "warm, quick" / female), else the character's bible
            seed = (st["aud"].get(spk, {}).get("voice")
                    or (dict(voices[vs], slug=None) if vs in voices else None)
                    or se.voice_seed(spk, meta))
            typed = st.setdefault("typed", {}).get(spk)
            dv = tk.StringVar(value=typed if typed is not None else (seed.get("notes") or ""))
            dv.trace_add("write", lambda *_a, s=spk, v=dv: st["typed"].__setitem__(s, v.get()))
            r1 = tk.Frame(mid, bg=C["panel"])
            r1.pack(fill="x")
            lab(r1, "How they sound").pack(side="left")
            tk.Entry(r1, textvariable=dv, bg="#100e0c", fg=C["ink"], relief="flat", font=fsm,
                     insertbackground=C["ink"]).pack(side="left", fill="x", expand=True,
                                                     padx=8, ipady=3)
            gv = tk.StringVar(value=seed.get("gender", "male"))
            _gm = tk.OptionMenu(r1, gv, "male", "female")
            _gm.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, width=6,
                          highlightthickness=0, borderwidth=0)
            _gm.pack(side="left")
            r2 = tk.Frame(mid, bg=C["panel"])
            r2.pack(fill="x", pady=(8, 0))
            got = st["aud"].get(spk, {}).get("wavs") or []
            b_aud = button(r2, "Audition 3 voices" if not got else "3 new auditions",
                           lambda s=spk, d=dv, g=gv, sd=seed, cs=cslug: _audition(s, d, g,
                                                                                  sd, cs),
                           primary=not got and not vs)
            b_aud.pack(side="left")
            for k, wav in enumerate(got):
                if not wav:
                    continue
                cell = tk.Frame(r2, bg=C["panel2"])
                cell.pack(side="left", padx=(10, 0))
                _pl = tk.Label(cell, text=" ▶ %s " % "ABC"[k], bg=C["panel2"],
                               fg=C["ink"], font=fh, cursor="hand2")
                _pl.pack(side="left", pady=4)
                _pl.bind("<Button-1>", lambda _e, w=wav: _play(w))
                _kp = tk.Label(cell, text=" Keep ", bg=C["ink"], fg="#14110f", font=fsm,
                               cursor="hand2")
                _kp.pack(side="left", padx=4)
                _kp.bind("<Button-1>", lambda _e, s=spk, w=wav, cs=cslug: _keep_aud(s, w, cs))
            if voices:
                ov = tk.StringVar(value="or use a saved voice ▾")
                _om = tk.OptionMenu(r2, ov, *sorted(v.get("name") or s for s, v in
                                                    voices.items()),
                                    command=lambda nm, s=spk: _use_voice(s, nm))
                _om.configure(bg=C["panel"], fg=C["ochre"], relief="flat", font=fsm,
                              highlightthickness=0, borderwidth=0)
                _om["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
                _om.pack(side="right")
            st.setdefault("cast_ui", {})[spk] = {"desc": dv, "gender": gv, "aud": b_aud}

    def _audition(spk, dv, gv, seed, cslug):
        v = dict(seed)
        v["notes"] = dv.get().strip()
        v["gender"] = gv.get()
        st["aud"][spk] = {"voice": v, "wavs": []}

        def run(job):
            return se.audition(v, 3, progress=lambda m: job.update(progress=m))

        def done(wavs):
            st["aud"][spk]["wavs"] = wavs
            _open_casting(spk)
            say("three voices for %s — ▶ to listen, Keep the right one" % spk.title())
            if wavs and wavs[0]:
                _play(wavs[0])
        _job(run, done, "auditioning voices for %s" % spk.title(),
             retry=lambda: _audition(spk, dv, gv, seed, cslug))

    def _keep_aud(spk, wav, cslug):
        v = st["aud"].get(spk, {}).get("voice")
        if not v:
            return
        vslug = se.keep_audition(v, wav, spk, cslug)
        pj = st["pj"]
        pj.setdefault("cast", {})[spk] = vslug
        for li in pj["lines"]:              # a new voice: this speaker's lines are re-read
            if dlg.normalise_speaker(li["speaker"]) == spk and li.get("takes"):
                li["stale"] = True
        _save()
        st["aud"].pop(spk, None)
        st.get("typed", {}).pop(spk, None)
        _open_casting(spk)
        say("%s's voice is kept%s" % (spk.title(), (" and saved to the character %s" % cslug)
                                      if cslug else ""))

    def _use_voice(spk, name):
        voices = _voices()
        vslug = next((s for s, v in voices.items() if (v.get("name") or s) == name), None)
        if not vslug:
            return
        st["pj"].setdefault("cast", {})[spk] = vslug
        st.get("typed", {}).pop(spk, None)
        _save()
        _open_casting(spk)
        say("%s now uses %s" % (spk.title(), name))

    # ================================================================== text mode, write
    def _show(which):
        for fr in (page, textp, castp):
            fr.pack_forget()
        {"page": page, "text": textp, "cast": castp}[which].pack(fill="both", expand=True)
        st["text_mode"] = which == "text"
        b_text.configure(text="Done — back to the lines" if which == "text"
                         else "Paste / edit as text")

    def _toggle_text():
        if st["text_mode"]:
            pj = _ensure_project()
            st["text_mode"] = False
            _show("page")
            _commit(dlg.reparse(pj, t_script.get("1.0", "end")))
            say("%d lines — untouched lines kept their takes" % len(st["pj"]["lines"]))
            return
        t_script.delete("1.0", "end")
        if st["pj"]:
            t_script.insert("1.0", se.script_from_lines(st["pj"]["lines"]))
        _show("text")

    def _write():
        from tkinter import simpledialog            # handler-time import (AGENTS.md)
        pj = st["pj"]
        names = dlg.speakers(pj["lines"]) if pj and pj.get("lines") else []
        if not names:
            names = [dlg.normalise_speaker((m.get("name") or s).split(" ")[0])
                     for s, m in se.list_characters()][:3]
        if not names:
            say("add a line (or make a character) first — the writer needs a cast",
                warn=True)
            return
        brief = simpledialog.askstring("Write it for me", "What happens in the scene?\n"
                                       "(cast: %s)" % ", ".join(n.title() for n in names),
                                       parent=win)
        if not brief:
            return
        _write_run(brief, names)

    def _write_run(brief, names, secs=30):
        cast = {}
        for n in names:
            cslug, meta = se.character_for(n)
            bible = (meta or {}).get("bible") or {}
            cast[n] = (bible.get("who") or (meta or {}).get("note") or "a character")[:200]

        def run(job):
            import director as _d
            return _d.Director().dialogue(brief, cast, seconds=secs)

        def done(out):
            import director as _d
            pj = _ensure_project()
            _commit(dlg.reparse(pj, _d.format_dialogue(out.get("lines") or [])),
                    "wrote “%s” — %d lines. Click a line to change it, then Make"
                    % (out.get("title", "untitled"), len(out.get("lines") or [])))
        _job(run, done, "writing the scene", retry=lambda: _write_run(brief, names, secs))

    # ================================================================== the scene
    def _gap_changed():
        if st["pj"] is not None:
            st["pj"]["gap_ms"] = int(gap_var.get())
            _save()

    def _engine_changed():
        if st["pj"] is not None:
            st["pj"]["engine"] = next((e for e, _l in dlg.ENGINES
                                       if dlg.engine_label(e) == eng_var.get()),
                                      dlg.DEFAULT_ENGINE)
            _save()
            _refresh_bar()

    def _play_scene():
        pj = st["pj"]
        if not pj or not pj.get("lines"):
            say("no lines yet", warn=True)
            return
        if se.missing(pj):
            say("%d line(s) not made yet — Make the missing lines first"
                % len(se.missing(pj)), warn=True)
            return
        try:
            path, secs = se.assemble(pj, gap_var.get())
        except Exception as exc:
            say("could not put the scene together: %s" % exc, warn=True)
            return
        st["scene"] = path
        say("the scene: %.1f s" % secs)
        _play(path)

    def _send():
        pj = st["pj"]
        if not pj or se.missing(pj):
            say("make every line first — Episode uses the takes you keep", warn=True)
            return
        if ctx.get("send_to_episode"):
            ctx["send_to_episode"](pj["slug"])
            say("sent to Episode — one lip-sync shot per line")

    def _play(path):
        _stop_play()
        if not path or not os.path.exists(path):
            say("nothing to play yet", warn=True)
            return
        try:
            st["player"] = subprocess.Popen(["afplay", path])
        except Exception:
            st["player"] = None
        st["played"] = path

    def _stop_play():
        p = st.get("player")
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
        st["player"] = None

    # ================================================================== start
    win.protocol("WM_DELETE_WINDOW", lambda: (_stop_play(), win.destroy()))
    _relist()
    _show("page")
    names = dlg.list_projects()
    if names and ctx.get("open_latest", True):
        _open(latest_scene(names))
    else:
        _new()
    if btn_colors:
        try:
            btn_colors(win)
        except Exception:
            pass
        b_make.configure(bg=C["ochre"], fg="#14110f")
        b_next.configure(bg=C["ochre"], fg="#14110f")

    win.d2 = {"st": st, "open": _open, "new": _new, "add_txt": add_txt, "add_spk": add_spk,
              "add": _add, "select": _select, "save_words": _save_words, "keep": _keep,
              "delete": _delete, "move": _move, "redo": _redo, "make": _make,
              "open_casting": _open_casting, "audition": _audition, "keep_aud": _keep_aud,
              "use_voice": _use_voice, "toggle_text": _toggle_text, "t_script": t_script,
              "write_run": _write_run, "play_scene": _play_scene, "send": _send,
              "status": status, "cards": cards, "cast_row": cast_row, "castp": castp,
              "page": page, "bar": bar, "b_make": b_make, "gap_var": gap_var,
              "eng_var": eng_var, "more": more, "toggle_more": _toggle_more,
              "go": _go, "next": _next, "chips": chips, "b_next": b_next,
              "step_title": step_title, "tools": tools, "bar_info": bar_info}
    return win
