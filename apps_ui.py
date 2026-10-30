"""The APPS window (2026-10-17) — one-click tools, Higgsfield-style.

    Apps                                                                         Close
    ┌ APPS ───────────────┐  ┌ TALKING PHOTO — any picture says your line ───────────────┐
    │ Talking photo       │  │ PICTURE  [thumb]  Choose…   recent: ▢ ▢ ▢ ▢ ▢ ▢ ▢ ▢         │
    │ Make it move        │  │ LINE     [Expect a forty percent chance of rain.        ]  │
    │ Outfit swap         │  │ VOICE    [Clarence ▾]   build one on the Voices page →     │
    │ New angle           │  │ [ Make it · ~2 min ]                                       │
    │ Change the place    │  │ result: [thumb] ▶ Open  Show in Finder  Use this picture → │
    │ …                   │  └────────────────────────────────────────────────────────────┘
Engine: apps.py (Tk-free). Every result is also an ordinary take in the main window.
"""
import os
import sys
import subprocess
import threading
import time

import apps
import dispatch as _dsp          # the rig's queue + scheduler (Tk-free, no rig calls at import)
import jobqueue as jq
import previews

# macOS draws its own white buttons and ignores bg: light text on them vanished
# ("Stop after this step" was blank on the MacBook) - dark text there, always
_MAC = sys.platform == "darwin"


def open_apps(root, ctx):
    import tkinter as tk
    import tkinter.font as tkfont

    C = ctx["C"]
    f, fh, fsm = ctx["fonts"]["f"], ctx["fonts"]["fh"], ctx["fonts"]["fsm"]
    fbig = tkfont.Font(family=fh.cget("family"), size=max(15, int(fh.cget("size")) + 5),
                       weight="bold")
    fl = ctx["mod"]
    import dock as _dock                 # a tab in the main window (Ryan: tabs)
    win = _dock.window(root)
    win.title("Apps")
    win.configure(bg=C["bg"])
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1240, _sw - 40), min(820, _sh - 110)))
    win.minsize(1000, 640)
    st = {"app": apps.APPS[0]["key"], "image": None, "image2": None, "move": apps.MOVES[0][0],
          "angle": apps.ANGLES[1][0], "expression": apps.EXPRESSIONS[0], "busy": False,
          "style": apps.STYLES[0][0], "video": None,
          "result": None, "voice": None, "_jobs": set()}

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

    def chip(parent, text, on, cmd):
        l_ = tk.Label(parent, text=text, bg=C["ink"] if on else C["panel2"],
                      fg="#14110f" if on else C["ink"], font=fsm, padx=10, pady=5,
                      cursor="hand2")
        l_.bind("<Button-1>", lambda _e: cmd())
        return l_

    # ------------------------------------------------------------------ header
    hd = tk.Frame(win, bg=C["panel"])
    hd.pack(fill="x")
    tk.Label(hd, text="Apps", bg=C["panel"], fg=C["ink"], font=fbig).pack(
        side="left", padx=(16, 14), pady=10)
    lab(hd, "one-click tools — every result also lands in your takes").pack(side="left")
    button(hd, "Close", lambda: win.destroy()).pack(side="right", padx=12)
    tk.Frame(win, bg=C["line"], height=1).pack(fill="x")
    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=16, pady=12)
    # Ryan 2026-10-22: "make the screen scrollable, I cant see the generate button on the
    # make it move screen" -- the eight moving camera previews pushed "Make it" below the
    # window. Both columns now scroll (wheel or scrollbar); `listf` / `panel` are the
    # scrolled inner frames, so everything that paints into them is unchanged.
    def _scroller(parent, **kw):
        outer = tk.Frame(parent, bg=C["panel"], highlightthickness=1,
                         highlightbackground=C["line"], **kw)
        sb_ = tk.Scrollbar(outer, orient="vertical")
        sb_.pack(side="right", fill="y")
        cv_ = tk.Canvas(outer, bg=C["panel"], highlightthickness=0, borderwidth=0,
                        yscrollcommand=sb_.set)
        cv_.pack(side="left", fill="both", expand=True)
        sb_.configure(command=cv_.yview)
        inner = tk.Frame(cv_, bg=C["panel"])
        wid = cv_.create_window(0, 0, window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: cv_.configure(scrollregion=cv_.bbox("all")))
        cv_.bind("<Configure>", lambda e: cv_.itemconfigure(wid, width=e.width))
        return outer, cv_, inner

    list_outer, list_cv, listf = _scroller(body, width=276)
    list_outer.pack(side="left", fill="y", padx=(0, 14))
    list_outer.pack_propagate(False)
    panel_outer, panel_cv, panel = _scroller(body)
    panel_outer.pack(side="left", fill="both", expand=True)
    try:
        fl.scoped_wheel(win, panel_cv)
        fl.scoped_wheel(win, list_cv, add=True)
    except Exception:
        pass
    status = lab(win, "", fg=C["muted"])
    status.pack(fill="x", padx=18, pady=(0, 8), side="bottom")

    def say(msg, warn=False):
        status.configure(text=msg, fg=C["ochre"] if warn else C["muted"])

    # ================================================================== painting
    def _paint_list():
        for w_ in listf.winfo_children():
            w_.destroy()
        lab(listf, "APPS", fg=C["faint"]).pack(fill="x", padx=12, pady=(12, 4))
        for a in apps.APPS:
            on = a["key"] == st["app"]
            fr = tk.Frame(listf, bg="#2a221b" if on else C["panel"], cursor="hand2")
            fr.pack(fill="x", padx=6, pady=1)
            l1 = tk.Label(fr, text=a["name"], bg=fr["bg"], fg=C["ink"],
                          font=fh if on else f, anchor="w")
            l1.pack(fill="x", padx=8, pady=(6, 0))
            l2 = tk.Label(fr, text=a["blurb"], bg=fr["bg"], fg=C["muted"], font=fsm,
                          anchor="w", wraplength=220, justify="left")
            l2.pack(fill="x", padx=8, pady=(0, 6))
            for w_ in (fr, l1, l2):
                w_.bind("<Button-1>", lambda _e, k=a["key"]: _pick_app(k))

    def _thumb(parent, path, box):
        img = fl.thumb_photo(path, box) if path and os.path.exists(path) else None
        l_ = tk.Label(parent, image=img, bg="#100e0c", fg=C["faint"], font=fsm,
                      text="" if img else "no picture", width=0 if img else 14,
                      height=0 if img else 6)
        l_.image = img
        return l_

    def _row(title):
        r = tk.Frame(panel, bg=C["panel"])
        r.pack(fill="x", padx=18, pady=(12, 0))
        lab(r, title, fg=C["faint"], width=10).pack(side="left", anchor="n")
        body_ = tk.Frame(r, bg=C["panel"])
        body_.pack(side="left", fill="x", expand=True)
        return body_

    def _picker(slot, title):
        b_ = _row(title)
        top = tk.Frame(b_, bg=C["panel"])
        top.pack(fill="x")
        _thumb(top, st[slot], 130).pack(side="left")
        col = tk.Frame(top, bg=C["panel"])
        col.pack(side="left", padx=10, anchor="n")
        button(col, "Choose a file…", lambda: _choose(slot)).pack(anchor="w")
        if st[slot]:
            lab(col, os.path.basename(st[slot])[:40]).pack(anchor="w", pady=(4, 0))
        rec = apps.recent_images(10)
        if rec:
            lab(b_, "or one of your recent pictures:", fg=C["faint"]).pack(fill="x",
                                                                          pady=(6, 2))
            strip = tk.Frame(b_, bg=C["panel"])
            strip.pack(fill="x")
            for p, _lab in rec:
                t_ = _thumb(strip, p, 56)
                t_.configure(cursor="hand2")
                t_.pack(side="left", padx=(0, 4))
                t_.bind("<Button-1>", lambda _e, pp=p: _set(slot, pp))

    # Ryan 2026-10-22: "have the styles how they are but in a box that is scrollable
    # horizontally so I can choose what I want and see them all without a dropdown".
    # Every style is a chip; the groups sit side by side (3 chips tall) in ONE strip that
    # scrolls sideways: two-finger swipe, shift+wheel, or the scrollbar under it. An up/down
    # swipe over it still scrolls the page. Guarded by tools/apps_ui_matrix.py.
    STRIP_ROWS = 3

    def _style_strip(parent):
        box = tk.Frame(parent, bg=C["panel"])
        box.pack(fill="x")
        cv = tk.Canvas(box, bg=C["panel"], highlightthickness=0, borderwidth=0, height=10,
                       xscrollincrement=40)
        cv.pack(fill="x")
        sb = tk.Scrollbar(box, orient="horizontal", command=cv.xview)
        sb.pack(fill="x", pady=(2, 0))

        # where the strip was before this repaint (a pick repaints the whole panel); the
        # new canvas reports x=0 while it lays out, so only remember x once it's restored
        saved_x = st.get("style_x") or 0.0
        ready = {"on": False}

        def _xs(a, b):
            sb.set(a, b)
            if ready["on"]:
                st["style_x"] = float(a)
        cv.configure(xscrollcommand=_xs)
        inner = tk.Frame(cv, bg=C["panel"])
        cv.create_window(0, 0, window=inner, anchor="nw")
        st["style_chips"] = {}
        for g, (gname, names) in enumerate(apps.STYLE_GROUPS):
            col = tk.Frame(inner, bg=C["panel"])
            col.pack(side="left", anchor="n", padx=(0 if g == 0 else 14, 0))
            lab(col, gname.upper(), fg=C["faint"]).grid(row=0, column=0, columnspan=9,
                                                        sticky="w", pady=(0, 3))
            for i, sy in enumerate(names):
                ch = chip(col, sy, sy == st["style"], lambda m=sy: _set("style", m))
                ch.grid(row=1 + i % STRIP_ROWS, column=i // STRIP_ROWS, sticky="ew",
                        padx=(0, 4), pady=(0, 4))
                st["style_chips"][sy] = ch
        words = dict(apps.STYLES).get(st["style"], "")
        lab(parent, (st["style"] + " \u2014 " + words) if words else "", fg=C["faint"],
            wraplength=640, justify="left").pack(fill="x", pady=(4, 0))

        def _fit(_e=None):
            cv.configure(height=inner.winfo_reqheight(), scrollregion=cv.bbox("all"))
        inner.bind("<Configure>", _fit)

        def _restore():
            try:
                _fit()
                cv.update_idletasks()
                cv.xview_moveto(saved_x)
            except Exception:
                pass
            ready["on"] = True
        cv.after_idle(_restore)

        def _shift_wheel(ev):
            try:
                d = int(getattr(ev, "delta", 0) or 0)
            except Exception:
                d = 0
            if d:
                cv.xview_scroll(-1 if d > 0 else 1, "units")
            return "break"

        def _swipe(ev):
            # TIP 684: dx is the HIGH 16 bits, dy the LOW 16 bits, both signed. A mostly
            # sideways swipe moves the strip; anything else falls through to the page.
            try:
                d = int(getattr(ev, "delta", 0) or 0)
            except Exception:
                return None
            hi, lo = (d >> 16) & 0xFFFF, d & 0xFFFF
            dx = hi - 0x10000 if hi >= 0x8000 else hi
            dy = lo - 0x10000 if lo >= 0x8000 else lo
            if not dx or abs(dx) <= abs(dy):
                return None
            try:
                sr = [float(v) for v in str(cv.cget("scrollregion")).split()]
                w = (sr[2] - sr[0]) if len(sr) == 4 else 0.0
                if w > cv.winfo_width() > 0:
                    cv.xview_moveto(max(0.0, min(1.0, cv.xview()[0] + (-dx) / w)))
            except Exception:
                pass
            return "break"

        def _bind_all_in(w):
            for ev_, fn in (("<Shift-MouseWheel>", _shift_wheel), ("<TouchpadScroll>", _swipe)):
                try:
                    w.bind(ev_, fn)
                except Exception:          # Tk 8.6 has no <TouchpadScroll>
                    pass
            for k in w.winfo_children():
                _bind_all_in(k)
        _bind_all_in(box)
        st["style_canvas"] = cv
        return cv

    def _paint_panel():
        for w_ in panel.winfo_children():
            w_.destroy()
        a = apps.get(st["app"])
        tk.Label(panel, text=a["name"], bg=C["panel"], fg=C["ink"], font=fbig,
                 anchor="w").pack(fill="x", padx=18, pady=(14, 0))
        lab(panel, a["blurb"]).pack(fill="x", padx=18)
        for need in a["needs"]:
            nm, _, title = need.partition(":")
            if nm == "image":
                _picker("image", "PICTURE")
            elif nm == "image2":
                _picker("image2", "PRODUCT" if st["app"] == "product" else "OUTFIT")
                if title:
                    lab(panel, "   " + title, fg=C["faint"]).pack(fill="x", padx=18)
            elif nm == "line":
                b_ = _row("LINE")
                e = tk.Entry(b_, textvariable=line_var, bg="#100e0c", fg=C["ink"],
                             relief="flat", font=f, insertbackground=C["ink"])
                e.pack(fill="x", ipady=4)
            elif nm == "voice":
                b_ = _row("VOICE")
                import dialogue as _dl
                vs = [(v.get("slug"), v.get("name") or v.get("slug")) for v in _dl.list_voices()
                      if _dl.voice_wav(v, "neutral")]
                names = [n for _s, n in vs] or ["(no voices yet)"]
                if voice_var.get() not in names:
                    voice_var.set(names[0])
                st["voice_map"] = {n: s for s, n in vs}
                om = tk.OptionMenu(b_, voice_var, *names)
                om.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                             highlightthickness=0, borderwidth=0, width=22)
                om["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
                om.pack(side="left")
                if ctx.get("open_voices"):
                    l_ = tk.Label(b_, text="   build a new voice on the Voices page →",
                                  bg=C["panel"], fg=C["ochre"], font=fsm, cursor="hand2")
                    l_.pack(side="left")
                    l_.bind("<Button-1>", lambda _e: ctx["open_voices"]())
            elif nm == "audio":
                b_ = _row("OR")
                top = tk.Frame(b_, bg=C["panel"])
                top.pack(fill="x")
                button(top, "Use a recording\u2026", lambda: _choose_audio()).pack(side="left")
                if st.get("audio"):
                    lab(top, "   %s  (instead of the line + voice)"
                        % os.path.basename(st["audio"]), fg=C["ink"]).pack(side="left")
                    l_ = tk.Label(top, text="  \u2715", bg=C["panel"], fg=C["ochre"], font=fsm,
                                  cursor="hand2")
                    l_.pack(side="left")
                    l_.bind("<Button-1>", lambda _e: _set("audio", None))
                else:
                    lab(top, "   your own voice or any audio, up to %d s" % apps.MAX_TALK,
                        fg=C["faint"]).pack(side="left")
            elif nm == "move":
                b_ = _row("CAMERA")
                # each move as a little moving picture of YOUR picture (or a sample scene)
                # doing it; hover to play (roadmap: Higgsfield's preset grid)
                src = st.get("image")
                frames = _move_frames(src)
                st["move_loops"] = {}
                for i, (mv, _w) in enumerate(apps.MOVES):
                    cell = tk.Frame(b_, bg=C["ink"] if mv == st["move"] else C["panel2"],
                                    cursor="hand2")
                    cell.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0, 4),
                              pady=(0, 4))
                    pic = tk.Label(cell, bg="#100e0c", cursor="hand2")
                    pic.pack(fill="x")
                    cl = chip(cell, mv, mv == st["move"], lambda m=mv: _set("move", m))
                    cl.pack(fill="x")
                    if frames.get(mv):
                        st["move_loops"][mv] = previews.Loop(pic, frames[mv],
                                                             hover_widgets=(cl, cell))
                    else:
                        pic.configure(text="\u2026", fg=C["faint"], height=4)
                    for w_ in (cell, pic):
                        w_.bind("<Button-1>", lambda _e, m=mv: _set("move", m))
            elif nm == "angle":
                b_ = _row("ANGLE")
                for i, (an, _w) in enumerate(apps.ANGLES):
                    chip(b_, an, an == st["angle"], lambda m=an: _set("angle", m)).grid(
                        row=i // 4, column=i % 4, sticky="ew", padx=(0, 4), pady=(0, 4))
            elif nm == "style":
                b_ = _row("STYLE")
                _style_strip(b_)
            elif nm == "video":
                b_ = _row("VIDEO")
                top = tk.Frame(b_, bg=C["panel"])
                top.pack(fill="x")
                button(top, "Choose a video\u2026", lambda: _choose_video()).pack(side="left")
                lab(top, "   " + (os.path.basename(st["video"]) if st["video"] else
                                  "no video picked"), fg=C["ink"] if st["video"] else
                    C["faint"]).pack(side="left")
                rv = apps.recent_videos(8)
                if rv:
                    lab(b_, "or a recent one:", fg=C["faint"]).pack(fill="x", pady=(6, 2))
                    for p, lb in rv:
                        l_ = tk.Label(b_, text="\u25b8 %s" % (lb or os.path.basename(p)),
                                      bg=C["panel"], fg=C["ink"] if p == st["video"] else
                                      C["ochre"], font=fsm, cursor="hand2", anchor="w")
                        l_.pack(fill="x")
                        l_.bind("<Button-1>", lambda _e, pp=p: _set("video", pp))
            elif nm == "expression":
                b_ = _row("FEELING")
                for i, ex in enumerate(apps.EXPRESSIONS):
                    chip(b_, ex, ex == st["expression"],
                         lambda m=ex: _set("expression", m)).grid(
                        row=i // 5, column=i % 5, sticky="ew", padx=(0, 4), pady=(0, 4))
            elif nm in ("text", "prompt"):
                b_ = _row("WHAT" if nm == "text" else "ALSO")
                if title:
                    lab(b_, title, fg=C["faint"]).pack(fill="x")
                e = tk.Entry(b_, textvariable=text_var if nm == "text" else prompt_var,
                             bg="#100e0c", fg=C["ink"], relief="flat", font=f,
                             insertbackground=C["ink"])
                e.pack(fill="x", ipady=4)
                if nm == "prompt":
                    lab(b_, "optional — anything else that happens", fg=C["faint"]).pack(
                        fill="x")
        gr = tk.Frame(panel, bg=C["panel"])
        gr.pack(fill="x", padx=18, pady=(18, 0))
        # NOT disabled while a job runs (2026-10-22): a second "Make it" is a queued job on
        # the OTHER card, not a mistake to prevent.
        st["b_go"] = button(gr, "Make it · ~%s" % _fmt(apps.estimate(
            st["app"], {"image": st["image"]})), lambda: _go(), primary=True)
        st["b_go"].pack(side="left")
        res = tk.Frame(panel, bg=C["panel"])
        res.pack(fill="x", padx=18, pady=(14, 14))
        st["res"] = res
        _paint_result()

    def _paint_result():
        res = st.get("res")
        if res is None:
            return
        for w_ in res.winfo_children():
            w_.destroy()
        r = st["result"]
        if not r:
            return
        lab(res, "RESULT", fg=C["faint"]).pack(fill="x")
        row = tk.Frame(res, bg=C["panel"])
        row.pack(fill="x")
        thumb_src = r["file"]
        if r["kind"] == "video":
            thumb_src = os.path.splitext(r["file"])[0] + "_thumb.png"
            if not os.path.exists(thumb_src):
                try:
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i",
                                    r["file"], "-frames:v", "1", thumb_src],
                                   capture_output=True, timeout=30)
                except Exception:
                    pass
        _thumb(row, thumb_src, 160).pack(side="left")
        col = tk.Frame(row, bg=C["panel"])
        col.pack(side="left", padx=10, anchor="n")
        button(col, "▶ Open" if r["kind"] == "video" else "Open",
               lambda: _open(r["file"]), primary=True).pack(anchor="w")
        button(col, "Show in Finder", lambda: _open(r["file"], reveal=True)).pack(
            anchor="w", pady=(6, 0))
        if r["kind"] == "image":
            button(col, "Use this picture next →", lambda: _set("image", r["file"])).pack(
                anchor="w", pady=(6, 0))
            button(col, "Make it move →", lambda: (_pick_app("move"),
                                                       _set("image", r["file"]))).pack(
                anchor="w", pady=(6, 0))

    def _fmt(sec):
        sec = int(sec or 0)
        return ("%d min" % max(1, round(sec / 60.0))) if sec >= 90 else "%d s" % sec

    # ================================================================== actions
    line_var = tk.StringVar(value="")
    voice_var = tk.StringVar(value="")
    text_var = tk.StringVar(value="")
    prompt_var = tk.StringVar(value="")

    def _pick_app(k):
        st["app"] = k
        st["result"] = None
        _paint_list()
        _paint_panel()
        try:
            panel_cv.yview_moveto(0.0)       # a new app starts at its top
        except Exception:
            pass
        say(apps.get(k)["blurb"])

    _mf = {"cache": {}, "busy": set()}

    def _move_frames(src):
        """{move: [frames]} for `src` (None = the sample scene); made once per picture on a
        worker, and the panel repaints when they're ready."""
        key = (src or "", os.path.getmtime(src) if src and os.path.exists(src) else 0)
        if key in _mf["cache"]:
            return _mf["cache"][key]
        if key not in _mf["busy"]:
            _mf["busy"].add(key)
            res = {}

            def work():
                out = {}
                for mv, _w in apps.MOVES:
                    try:
                        out[mv] = previews.camera_move(mv, src)
                    except Exception:
                        out[mv] = []
                res["out"] = out

            def poll():
                if "out" not in res:
                    win.after(250, poll)
                    return
                _mf["cache"][key] = res["out"]
                _mf["busy"].discard(key)
                try:
                    if st["app"] == "move" and (st.get("image") or "") == key[0]:
                        _paint_panel()           # (only for the picture still picked)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()
            win.after(250, poll)
        return {}

    def _set(slot, val):
        st[slot] = val
        _paint_panel()

    def _choose(slot):
        from tkinter import filedialog            # handler-time import (AGENTS.md)
        p = filedialog.askopenfilename(parent=win, title="A picture",
                                       filetypes=[("Images", "*.png *.jpg *.jpeg *.webp"),
                                                  ("All files", "*.*")])
        if p:
            _set(slot, p)

    def _choose_audio():
        from tkinter import filedialog            # handler-time import (AGENTS.md)
        p = filedialog.askopenfilename(parent=win, title="A recording",
                                       filetypes=[("Audio or video", "*.wav *.mp3 *.m4a *.aif "
                                                   "*.aiff *.flac *.mp4 *.mov"),
                                                  ("All files", "*.*")])
        if p:
            _set("audio", p)

    def _choose_video():
        from tkinter import filedialog            # handler-time import (AGENTS.md)
        p = filedialog.askopenfilename(parent=win, title="A video",
                                       filetypes=[("Video", "*.mp4 *.mov *.webm"),
                                                  ("All files", "*.*")])
        if p:
            _set("video", p)

    def _inputs():
        return {"image": st["image"], "image2": st["image2"], "move": st["move"],
                "style": st["style"], "video": st["video"], "audio": st.get("audio"),
                "angle": st["angle"], "expression": st["expression"],
                "text": text_var.get().strip(), "prompt": prompt_var.get().strip(),
                "line": line_var.get().strip(),
                "voice": (st.get("voice_map") or {}).get(voice_var.get())}

    # ---------------------------------------------------------------- the rig's queue
    # Ryan, 2026-10-22: "after submitting one video he is refused a second. he wants a job on
    # the OTHER card when it is free, and when both cards are busy the job should QUEUE."
    # So every "Make it" is now a QUEUE ENTRY: the dispatcher (dispatch.py) starts it on the
    # card it has checked has room and, when both are busy, it waits visibly (Home tab:
    # reorder, pause, delete) instead of being refused. Nothing in this file talks to the rig
    # any more — submit-and-wait is apps.run(), and that is what the dispatcher calls.
    def _job_title(key, a):
        """'Make it move — the loft' — what the queue row says."""
        app = apps.get(key)
        hint = (a.get("line") or a.get("prompt") or a.get("text") or "").strip()
        if not hint:
            hint = os.path.basename(a.get("image") or a.get("image2") or a.get("video") or "")
        return ("%s — %s" % (app["name"], hint[:34])) if hint else app["name"]

    def _need_of(key):
        kind = apps.get(key)["kind"]
        return {"image": "image", "local": "local"}.get(kind, "video")

    def _live_mine():
        """Is any job THIS window submitted still queued or running?"""
        q = _dsp.queue()
        for jid in list(st["_jobs"]):
            e = q.get(jid) or {}
            if e.get("state") in ("queued", "paused", "running"):
                return True
        return False

    def _go():
        key, a = st["app"], _inputs()
        try:
            est = apps.estimate(key, a)
        except Exception:
            est = 0
        entry = _dsp.enqueue(_job_title(key, a), {"key": key, "args": a, "quality": "Draft"},
                             need=_need_of(key), est_s=est)
        st["_jobs"].add(entry["id"])
        st["busy"] = True
        if not _dsp.running():
            # No scheduler in this process (a driver run, or a loop that failed to start):
            # do it now, unpinned, the way this window submitted before the queue existed.
            _dsp.run_directly(entry)
            say("started — the queue isn't running in this session")
        else:
            say("queued — %s" % _dsp.summary())
        _paint_panel()
        _watch(entry["id"])

    def _watch(jid):
        """Follow one queued job on the Tk thread: waiting -> running -> result."""
        def poll():
            q = _dsp.queue()
            e = q.get(jid) or {}
            state = e.get("state")
            if state in ("queued", "paused"):
                ahead = q.queued_ahead(jid)
                wait = _fmt(jq.wait_seconds(q, jid))
                if state == "paused":
                    say("paused — it keeps its place in line   (%s)" % _dsp.summary())
                else:
                    where = ("you're #%d in the queue" % (ahead + 1)) if ahead and ahead > 0 \
                        else "next up"
                    say("waiting for a card — %s, about %s   (%s)"
                        % (where, wait, _dsp.summary()))
                _repaint_if_new(jid, state)
                win.after(700, poll)
                return
            if state == "running":
                say("%s   %s   %.0f s   (%s)" % (e.get("lane") or "", _dsp.progress(jid)
                                                or "working…", time.time() - _t0(jid),
                                                _dsp.summary()))
                _repaint_if_new(jid, state)
                win.after(700, poll)
                return
            # terminal: done, failed, cancelled or gone
            out, err = _dsp.result(jid), _dsp.error(jid)
            if state == "done" and out:
                st["result"] = out
                say("done — it's also in your takes")
            elif err:
                if "is busy, so" in err and not _dsp.running():
                    # a card went busy under a job with no dispatcher to re-queue it
                    if q.requeue(jid, note="waiting for a card"):
                        say("that card is busy — it's in the queue and starts on its own")
                    else:
                        say(err[:200], warn=True)
                else:
                    say(err[:200], warn=True)
            else:
                say("it is no longer in the queue")
            st["busy"] = _live_mine()
            _paint_panel()
        win.after(300, poll)

    _seen = {}

    def _repaint_if_new(jid, state):
        """Repaint on a state CHANGE only — the queue row is the live thing, not this panel."""
        if _seen.get(jid) != state:
            _seen[jid] = state
            _paint_panel()

    def _t0(jid):
        return _dsp.started_at(jid) or time.time()

    def _open(path, reveal=False):
        try:
            subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
        except Exception:
            pass

    # ================================================================== start
    _paint_list()
    _paint_panel()
    say("pick an app on the left")
    if ctx.get("btn_colors"):
        try:
            ctx["btn_colors"](win)
        except Exception:
            pass
    win.ap = {"st": st, "pick": _pick_app, "set": _set, "go": _go, "line_var": line_var,
              "voice_var": voice_var, "text_var": text_var, "prompt_var": prompt_var,
              "panel": panel, "status": status, "inputs": _inputs,
              "panel_cv": panel_cv, "list_cv": list_cv}
    return win
