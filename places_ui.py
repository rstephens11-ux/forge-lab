"""The PLACES page (2026-10-17) — make a location once; every video can be set in it.

    Places                                         From a photo…   Close
    ┌ YOUR PLACES ────────┐  ┌ MAKE A PLACE ──────────────────────────────────────┐
    │ [img] Lamp room     │  │ Describe it [a lighthouse lamp room at dusk…]       │
    │ [img] TV studio     │  │ Shape (Tall 9:16)(Wide 16:9)   [Make 3 · ~3 min]     │
    │ …                   │  │ [opt A] [opt B] [opt C]   name [Lamp room] Keep ▸   │
    └─────────────────────┘  ├ THIS PLACE ─────────────────────────────────────────┤
                             │ [big picture]  name [Lamp room]                      │
                             │ Change it [at night, in a storm]  [Make 3 versions]  │
                             │ Use it in Quick ▸   Show in Finder                   │
                             └─────────────────────────────────────────────────────┘

Engine: places (Tk-free). Rig jobs run on a worker thread; the Tk thread polls.
"""
import os
import subprocess
import sys
import threading
import time

import places as pl

# macOS draws its own white buttons and ignores bg: dark text there, always
_MAC = sys.platform == "darwin"


def open_places(root, ctx):
    import tkinter as tk

    C = ctx["C"]
    f, fh, fsm = ctx["fonts"]["f"], ctx["fonts"]["fh"], ctx["fonts"]["fsm"]
    fl = ctx["mod"]
    import dock as _dock                 # a tab in the main window (Ryan: tabs)
    win = _dock.window(root)
    win.title("Places")
    win.configure(bg=C["bg"])
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1240, _sw - 40), min(820, _sh - 110)))
    win.minsize(1000, 640)
    st = {"aspect": "9:16", "opts": [], "sel": None, "busy": False, "job": None,
          "opt_kind": "new", "big": None}
    OPT_BOX = 260                       # the three options (was 200: too small to choose)

    def lab(parent, text, fg=None, font=None, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["muted"],
                        font=font or fsm, anchor="w", **kw)

    def button(parent, text, cmd, primary=False, **kw):
        return tk.Button(parent, text=text, command=cmd, relief="flat", borderwidth=0,
                         font=fh if primary else fsm, padx=14, pady=8 if primary else 5,
                         bg=C["ochre"] if primary else C["panel2"],
                         fg="#14110f" if primary or _MAC else C["ink"],
                         activebackground="#d9b45e" if primary else C["rust"],
                         activeforeground="#14110f", highlightthickness=0, **kw)

    def chip(parent, text, on, cmd):
        l_ = tk.Label(parent, text=text, font=fsm, padx=10, pady=5, cursor="hand2",
                      bg=C["ink"] if on else C["panel2"], fg="#14110f" if on else C["ink"])
        l_.bind("<Button-1>", lambda _e: cmd())
        return l_

    def thumb(parent, path, box):
        img = fl.thumb_photo(path, box) if path and os.path.exists(path) else None
        l_ = tk.Label(parent, bg=C["panel2"], text="" if img else "—", fg=C["faint"],
                      font=fsm)
        if img is not None:
            l_.configure(image=img)
            l_.image = img
        return l_

    # ------------------------------------------------------------------ header
    hd = tk.Frame(win, bg=C["panel"])
    hd.pack(fill="x")
    lab(hd, "Places", fg=C["ink"], font=fh).pack(side="left", padx=(16, 8), pady=10)
    lab(hd, "make a location once — every video can be set in it, the same every "
            "time", fg=C["faint"]).pack(side="left")
    button(hd, "Close", lambda: win.withdraw()).pack(side="right", padx=12)
    button(hd, "From a photo…", lambda: _from_photo()).pack(side="right")
    status = lab(win, "", fg=C["faint"])
    status.pack(fill="x", padx=16, pady=(4, 0))

    def say(msg, warn=False):
        status.configure(text=msg, fg=C["ochre"] if warn else C["faint"])

    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=14, pady=10)

    # ------------------------------------------------------------------ left: library
    left = tk.Frame(body, bg=C["panel"], width=250)
    left.pack(side="left", fill="y", padx=(0, 12))
    left.pack_propagate(False)
    lab(left, "YOUR PLACES", fg=C["faint"]).pack(fill="x", padx=12, pady=(10, 6))
    lib_cv = tk.Canvas(left, bg=C["panel"], highlightthickness=0, borderwidth=0)
    lib_sb = tk.Scrollbar(left, orient="vertical", command=lib_cv.yview)
    lib_cv.configure(yscrollcommand=lib_sb.set)
    lib_sb.pack(side="right", fill="y")
    lib_cv.pack(side="left", fill="both", expand=True)
    lib_box = tk.Frame(lib_cv, bg=C["panel"])
    _lw = lib_cv.create_window(0, 0, window=lib_box, anchor="nw")
    lib_box.bind("<Configure>", lambda _e: lib_cv.configure(scrollregion=lib_cv.bbox("all")))
    lib_cv.bind("<Configure>", lambda e: lib_cv.itemconfigure(_lw, width=e.width))
    try:
        fl.scoped_wheel(win, lib_cv)
    except Exception:
        pass

    # ------------------------------------------------------------------ right
    right = tk.Frame(body, bg=C["panel"])
    right.pack(side="left", fill="both", expand=True)
    lab(right, "MAKE A PLACE", fg=C["faint"]).pack(fill="x", padx=16, pady=(12, 4))
    r1 = tk.Frame(right, bg=C["panel"])
    r1.pack(fill="x", padx=16)
    lab(r1, "Describe it").pack(side="left")
    desc_var = tk.StringVar(value="")
    desc_ent = tk.Entry(r1, textvariable=desc_var, bg="#100e0c", fg=C["ink"], font=fsm,
                        relief="flat", insertbackground=C["ink"])
    desc_ent.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
    r2 = tk.Frame(right, bg=C["panel"])
    r2.pack(fill="x", padx=16, pady=(8, 0))
    lab(r2, "Shape").pack(side="left")
    shape_box = tk.Frame(r2, bg=C["panel"])
    shape_box.pack(side="left", padx=8)
    b_make = button(r2, "Make 3", lambda: _make(), primary=True)
    b_make.pack(side="left", padx=(16, 0))
    lab(right, "e.g. “a lighthouse lamp room at dusk, brass instruments, rain on the "
               "glass” — empty, no people: your characters go in later",
        fg=C["faint"]).pack(fill="x", padx=16, pady=(4, 0))
    opt_box = tk.Frame(right, bg=C["panel"])
    opt_box.pack(fill="x", padx=16, pady=(10, 0))
    r3 = tk.Frame(right, bg=C["panel"])
    r3.pack(fill="x", padx=16, pady=(6, 0))
    lab(r3, "Name").pack(side="left")
    name_var = tk.StringVar(value="")
    tk.Entry(r3, textvariable=name_var, bg="#100e0c", fg=C["ink"], font=fsm, relief="flat",
             insertbackground=C["ink"], width=28).pack(side="left", padx=8, ipady=3)
    lab(r3, "click a picture above to see it big", fg=C["faint"]).pack(side="left")

    tk.Frame(right, bg=C["line"], height=1).pack(fill="x", padx=16, pady=12)
    lab(right, "THIS PLACE", fg=C["faint"]).pack(fill="x", padx=16)
    sel_box = tk.Frame(right, bg=C["panel"])
    sel_box.pack(fill="both", expand=True, padx=16, pady=(6, 12))

    # ------------------------------------------------------------------ painting
    def _paint_shape():
        for w_ in shape_box.winfo_children():
            w_.destroy()
        for a_, t_ in (("9:16", "Tall 9:16"), ("16:9", "Wide 16:9")):
            chip(shape_box, t_, st["aspect"] == a_,
                 lambda v=a_: (st.__setitem__("aspect", v), _paint_shape())).pack(
                side="left", padx=(0, 3))

    def _paint_lib():
        for w_ in lib_box.winfo_children():
            w_.destroy()
        lib = pl.library()
        if not lib:
            lab(lib_box, "none yet — describe one on the right", fg=C["faint"],
                wraplength=200, justify="left").pack(fill="x", padx=10, pady=6)
        for slug, name in lib:
            on = slug == st["sel"]
            row = tk.Frame(lib_box, bg="#2a221b" if on else C["panel"], cursor="hand2")
            row.pack(fill="x", pady=1)
            th = thumb(row, pl.image(slug), 48)
            th.pack(side="left", padx=(6, 8), pady=3)
            t = tk.Label(row, text=name, bg=row["bg"], fg=C["ink"], font=fsm, anchor="w",
                         cursor="hand2")
            t.pack(side="left", fill="x", expand=True)
            for w_ in (row, th, t):
                w_.bind("<Button-1>", lambda _e, s=slug: _select(s))

    def _paint_opts():
        for w_ in opt_box.winfo_children():
            w_.destroy()
        # Ryan 2026-10-21: "the previews are too small for me to decide". Bigger here, and a
        # click opens the picture as big as the page (it used to KEEP it on the spot).
        for i, p in enumerate(st["opts"]):
            fr = tk.Frame(opt_box, bg=C["panel2"], cursor="hand2")
            fr.pack(side="left", padx=(0, 8))
            th = thumb(fr, p, OPT_BOX)
            th.configure(cursor="hand2")
            th.pack(padx=4, pady=4)
            br_ = tk.Frame(fr, bg=C["panel2"])
            br_.pack(pady=(0, 4))
            button(br_, "See it big", lambda i_=i: _big(st["opts"], i_)).pack(side="left")
            button(br_, "Keep this one", lambda p_=p: _keep(p_)).pack(side="left", padx=(4, 0))
            th.bind("<Button-1>", lambda _e, i_=i: _big(st["opts"], i_))

    def _paint_sel():
        for w_ in sel_box.winfo_children():
            w_.destroy()
        m = pl.read(st["sel"]) if st["sel"] else None
        if not m:
            lab(sel_box, "pick a place on the left, or make one", fg=C["faint"]).pack(
                fill="x")
            return
        row = tk.Frame(sel_box, bg=C["panel"])
        row.pack(fill="both", expand=True)
        _th = thumb(row, m.get("image"), 300)
        _th.configure(cursor="hand2")
        _th.pack(side="left", anchor="n")
        _th.bind("<Button-1>", lambda _e: _big([m.get("image")], 0, keep=False))
        col = tk.Frame(row, bg=C["panel"])
        col.pack(side="left", fill="both", expand=True, padx=14)
        lab(col, m.get("name") or st["sel"], fg=C["ink"], font=fh).pack(fill="x")
        lab(col, (m.get("text") or "your photo") + "  ·  " +
            ("tall" if m.get("aspect") == "9:16" else "wide"), wraplength=420,
            justify="left").pack(fill="x", pady=(2, 10))
        lab(col, "Change it (the same place, changed)").pack(fill="x")
        cr = tk.Frame(col, bg=C["panel"])
        cr.pack(fill="x", pady=(2, 0))
        ch_var = tk.StringVar(value="")
        st["change_var"] = ch_var
        tk.Entry(cr, textvariable=ch_var, bg="#100e0c", fg=C["ink"], font=fsm, relief="flat",
                 insertbackground=C["ink"]).pack(side="left", fill="x", expand=True, ipady=3)
        button(cr, "Make 3 versions", lambda: _variant()).pack(side="left", padx=(8, 0))
        lab(col, "e.g. “at night”, “after a storm”, “seen from the "
                 "doorway”", fg=C["faint"]).pack(fill="x", pady=(2, 10))
        br = tk.Frame(col, bg=C["panel"])
        br.pack(fill="x")
        if ctx.get("open_quick"):
            button(br, "Use it in Quick →", lambda: ctx["open_quick"](place=st["sel"]),
                   primary=True).pack(side="left")
        button(br, "See it big", lambda: _big([m.get("image")], 0, keep=False)).pack(
            side="left", padx=(8, 0))
        button(br, "Show in Finder", lambda: _reveal(m.get("image"))).pack(side="left",
                                                                           padx=8)

    # ------------------------------------------------------------------ the big view
    def _fit(path, avail_w, avail_h):
        """The longest-side box that makes `path` fill avail_w x avail_h without spilling."""
        dims = None
        try:
            dims = fl._png_dims(path)
        except Exception:
            dims = None
        if not dims:
            try:
                from PIL import Image
                with Image.open(path) as im_:
                    dims = im_.size
            except Exception:
                dims = None
        if not dims or not dims[0] or not dims[1]:
            return max(64, min(avail_w, avail_h))
        w_, h_ = dims
        s_ = min(avail_w / float(w_), avail_h / float(h_))
        return max(64, int(max(w_, h_) * s_))

    def _close_big(_e=None):
        b_ = st.get("big")
        st["big"] = None
        if b_:
            try:
                b_["ov"].destroy()
            except Exception:
                pass

    def _big_step(d):
        b_ = st.get("big")
        if b_ and len(b_["paths"]) > 1:
            b_["i"] = (b_["i"] + d) % len(b_["paths"])
            _paint_big()

    def _big_keep():
        b_ = st.get("big")
        if b_ and b_["keep"]:
            p_ = b_["paths"][b_["i"]]
            _close_big()
            _keep(p_)

    def _big(paths, i=0, keep=True):
        """One picture as big as the page: arrows step through the three, Keep keeps the one
        on screen, Esc / Close goes back."""
        paths = [p for p in (paths or []) if p and os.path.exists(p)]
        if not paths:
            return
        _close_big()
        ov = tk.Frame(win, bg="#0b0a09")
        ov.place(x=0, y=0, relwidth=1, relheight=1)
        ov.lift()
        st["big"] = {"ov": ov, "paths": paths, "i": i % len(paths), "keep": keep}
        _paint_big()

    def _paint_big():
        b_ = st.get("big")
        if not b_:
            return
        ov = b_["ov"]
        for w_ in ov.winfo_children():
            w_.destroy()
        bar = tk.Frame(ov, bg="#0b0a09")
        bar.pack(side="top", fill="x", padx=16, pady=10)
        n_ = len(b_["paths"])
        if n_ > 1:
            button(bar, "\u25c2 Previous", lambda: _big_step(-1)).pack(side="left")
            lab(bar, "  %d of %d  " % (b_["i"] + 1, n_), fg=C["ink"]).pack(side="left")
            button(bar, "Next \u25b8", lambda: _big_step(1)).pack(side="left")
        button(bar, "Close  (Esc)", _close_big).pack(side="right")
        if b_["keep"]:
            button(bar, "Keep this one", _big_keep, primary=True).pack(side="right",
                                                                      padx=(0, 10))
        win.update_idletasks()
        aw = max(320, ov.winfo_width() - 40)
        ah = max(240, ov.winfo_height() - bar.winfo_reqheight() - 40)
        p_ = b_["paths"][b_["i"]]
        img = thumb(ov, p_, _fit(p_, aw, ah))
        img.configure(bg="#0b0a09", cursor="hand2")
        img.pack(expand=True)
        img.bind("<Button-1>", lambda _e: _big_step(1) if n_ > 1 else _close_big())
        b_["img"] = img

    def _big_key(fn):
        def h_(_e=None):
            if st.get("big"):
                fn()
                return "break"
            return None
        return h_

    try:
        win.bind("<Escape>", _big_key(_close_big))
        win.bind("<Left>", _big_key(lambda: _big_step(-1)))
        win.bind("<Right>", _big_key(lambda: _big_step(1)))
    except Exception:
        pass

    def _paint_all():
        _paint_shape()
        _paint_lib()
        _paint_opts()
        _paint_sel()
        b_make.configure(text="Make 3 · ~%d min" % max(1, round(pl.N_OPTIONS * 50 / 60.0)),
                         state="disabled" if st["busy"] else "normal")

    # ------------------------------------------------------------------ actions
    def _select(slug):
        st["sel"] = slug
        _paint_lib()
        _paint_sel()

    def _run(label, fn, done):
        if st["busy"]:
            say("already making something — wait for it", warn=True)
            return
        st["busy"] = True
        st["opts"] = []                 # the last run's pictures go: a stale Keep would save
        _paint_opts()                   # one with THIS run's text and shape
        st["job"] = job = {"done": False, "out": None, "err": None, "now": label}
        b_make.configure(state="disabled")
        say(label + "…")

        def work():
            try:
                job["out"] = fn(lambda m: job.__setitem__("now", m))
            except Exception as exc:
                job["err"] = exc
            finally:
                job["done"] = True

        def poll():
            if not job["done"]:
                say(job["now"] + "…")
                win.after(400, poll)
                return
            st["busy"] = False
            if job["err"] is not None:
                say(str(job["err"])[:200], warn=True)
            else:
                done(job["out"])
            _paint_all()
        threading.Thread(target=work, daemon=True).start()
        win.after(400, poll)

    def _make():
        text = desc_var.get().strip()
        if not text:
            say("describe the place first", warn=True)
            return
        if not name_var.get().strip():
            name_var.set(text.split(",")[0][:40])
        st["opt_kind"] = "new"
        st["opt_text"] = text
        st["opt_aspect"] = st["aspect"]
        _run("making the place", lambda pr: pl.options(text, st["aspect"], progress=pr),
             lambda out: (st.__setitem__("opts", out),
                          say("pick the one to keep (click it)")))

    def _variant():
        ch = (st.get("change_var").get() if st.get("change_var") else "").strip()
        if not ch:
            say("say what to change", warn=True)
            return
        m = pl.read(st["sel"]) or {}
        st["opt_kind"] = "variant"
        st["opt_text"] = ("%s, %s" % (m.get("text") or m.get("name") or "", ch)).strip(", ")
        st["opt_aspect"] = m.get("aspect", "9:16")
        st["opt_parent"] = st["sel"]
        name_var.set(("%s (%s)" % (m.get("name") or "", ch))[:50])
        _run("changing the place", lambda pr: pl.variant(st["sel"], ch, progress=pr),
             lambda out: (st.__setitem__("opts", out),
                          say("pick the version to keep (click it)")))

    def _keep(path):
        try:
            slug = pl.keep(name_var.get().strip(), st.get("opt_text") or "", path,
                           st.get("opt_aspect") or st["aspect"],
                           parent=st.get("opt_parent") if st["opt_kind"] == "variant" else None)
        except ValueError as exc:
            say(str(exc), warn=True)
            return
        st["opts"] = []
        st["sel"] = slug
        say("kept “%s” — Quick and Episode can be set in it now"
            % (pl.read(slug) or {}).get("name", slug))
        _paint_all()

    def _from_photo(path=None):
        if path is None:
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=win, title="A photo of the place",
                                              filetypes=[("Pictures", "*.png *.jpg *.jpeg "
                                                          "*.webp *.heic"), ("All", "*")])
        if not path:
            return None
        try:
            slug = pl.from_photo(path, name_var.get().strip() or None, st["aspect"])
        except ValueError as exc:
            say(str(exc), warn=True)
            return None
        st["sel"] = slug
        say("kept your photo as a place")
        _paint_all()
        return slug

    def _reveal(path):
        if path and os.path.exists(path) and sys.platform == "darwin":
            subprocess.run(["open", "-R", path])

    _paint_all()
    win.pl = {"st": st, "desc_var": desc_var, "name_var": name_var, "make": _make,
              "keep": _keep, "select": _select, "variant": _variant,
              "from_photo": _from_photo, "lib_box": lib_box, "opt_box": opt_box,
              "sel_box": sel_box, "status": status, "b_make": b_make,
              "paint": _paint_all, "big": _big, "big_step": _big_step,
              "big_keep": _big_keep, "close_big": _close_big}
    return win
