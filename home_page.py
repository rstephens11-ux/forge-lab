#!/usr/bin/env python3
"""home_page — the HOME tab: what the rig is doing, what is waiting, what came out.

Ryan, 2026-10-22: "i want a new home tab ... recent takes, the machine room details, and the
queue." To be clear about what this is NOT: it does not replace or rewrite the Make tab. The
Make tab is where you make something; this tab is a reading room, and it is safe to be wrong
here because nothing is submitted from it except queue controls.

    ┌ QUEUE ─────────────────────────────────────────┐ ┌ MACHINE ROOM ─────────┐
    │ running  Make it move — 6 s    card 1  comfy_a │ │ CARDS                 │
    │ queued   Turbo — the barn      both cards      │ │ Card 1 ▕███░░░░▏ 9/24 │
    │ [▲][▼][Pause][Delete][Run next]                │ │ Card 2 ▕██████░░▏ ... │
    └────────────────────────────────────────────────┘ └───────────────────────┘
    ┌ RECENT TAKES ──────────────────────────────────┐
    │ [All][Images][Videos][Songs]  show [All ▾]  unkept only
    │ 100 of 606 takes · click a thumbnail to open it full size
    │ ▣  Video · whip pan into the subject           │
    │    seed 1639253397 · 12 steps · 2 h ago        │
    └────────────────────────────────────────────────┘

⚠️ RECENT TAKES MIRRORS THE IMAGE TAB (Ryan, 2026-10-22: "can you mirror how the recent takes
are in the image tab to the home tab" … "should have the same filters as the image tab for
image, video, song. and it should show the last 100 just like the image tab does").

So it is the image tab's own pipeline, not a lookalike:
  * the same INDEX (read_index), not a walk of the runs folder;
  * the same ORDER — `filter_kind(index, kind)` FIRST, then `[:TAKES_SHOWN]`, so "Songs" means
    your last 100 songs, not the songs that happen to be among the last 100 takes;
  * the same rating filter ∩ unkept-only (`filter_takes`);
  * the same controls, sharing the image tab's STATE: the kind buttons write
    `st["take_kind"]`, the unkept toggle writes `st["take_unkept"]`, and the rating menu is the
    very same tk.StringVar — change it in either tab and the other follows;
  * the same row shape: the take's own thumbnail (190 px, ▶ over a video's poster, ♪ for a
    song), label + prompt, a seed/steps line, and a ✕-rejected take drawn dim like the Make tab.

⚠️ The filter RULES live in forge_lab (take_kind / filter_kind / filter_takes) and arrive
through ctx — never re-implemented here, or the two tabs would drift. ⚠️ And never
`import forge_lab`: the app runs as __main__, so importing it again would execute the module a
second time (the trap characters_ui documents). With no bundle in ctx (a bare unit test) the
page still renders the newest takes from runs/, just without the filter row.

The image tab's keep/reject/compare/delete controls stay in the image tab: this is the reading
room, not a second control panel.
"""
import os
import threading
import tkinter as tk

import cards_ui
import home_queue
import jobqueue

MEDIA_EXT = (".mp4", ".mov", ".png", ".jpg", ".jpeg", ".wav", ".mp3", ".flac")
KIND_OF = {".mp4": "video", ".mov": "video", ".png": "image", ".jpg": "image",
           ".jpeg": "image", ".wav": "audio", ".mp3": "audio", ".flac": "audio"}
BOX = 190                     # the thumbnail box, same as a take row in the Make tab
ROWS = 14                     # only used when ctx carries no takes bundle (a bare test)


def data_dir():
    """The app's DATA folder, resolved the way the app resolves it."""
    return os.path.expanduser(os.environ.get("FORGE_LAB_DATA") or "~/forge-lab")


def runs_dir():
    return os.path.join(data_dir(), "runs")


def index_path():
    return os.path.join(data_dir(), "runs", "index.json")


def recent_runs(limit=ROWS, root=None):
    """Newest media files in runs/, newest first — the no-bundle fallback path only.

    `id` is the run folder's name — the same id the run index is keyed by.
    """
    root = root or runs_dir()
    found = []
    try:
        for d in os.listdir(root):
            p = os.path.join(root, d)
            if not os.path.isdir(p):
                continue
            for f in os.listdir(p):
                ext = os.path.splitext(f)[1].lower()
                if ext not in MEDIA_EXT or "_thumb" in f:
                    continue
                full = os.path.join(p, f)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                found.append({"id": d, "path": full, "name": f,
                              "kind": KIND_OF.get(ext, "file"),
                              "when": st.st_mtime, "bytes": st.st_size})
    except OSError:
        return []
    found.sort(key=lambda r: r["when"], reverse=True)
    return found[:limit]


def _age(seconds):
    if seconds < 90:
        return "%d s ago" % max(1, int(seconds))
    if seconds < 5400:
        return "%d min ago" % int(seconds // 60)
    if seconds < 172800:
        return "%d h ago" % int(seconds // 3600)
    return "%d days ago" % int(seconds // 86400)


def _size(n):
    if not n:
        return ""
    if n > 1e9:
        return "%.1f GB" % (n / 1e9)
    if n > 1e6:
        return "%.1f MB" % (n / 1e6)
    return "%d KB" % max(1, int(n / 1e3))


def scroll_delta(ev, touchpad=False):
    """Pixels to scroll for one event.

    ⚠️ Tk 9 sends a TRACKPAD's two-finger scroll as <TouchpadScroll> (TIP 684: Δy in the LOW
    16 bits, signed) and a physical wheel as <MouseWheel> (±120/notch). The app's own global
    router decodes both; this is the same decode, for the Home tab's canvas.
    """
    try:
        d = int(getattr(ev, "delta", 0) or 0)
    except Exception:                              # noqa: BLE001
        return 0
    if not d:
        return 0
    if touchpad:
        low = d & 0xFFFF
        return low - 0x10000 if low >= 0x8000 else low
    return int(round(d / 3.0))


def _line(it, r, now):
    """The two lines beside the thumbnail — the image tab's take text, trimmed to a home page."""
    label, mode = (it.get("label") or "").strip(), (it.get("mode") or "").strip()
    # "Video · video" reads like a stutter: the label only adds something when it says more
    if label and mode and label.lower() != mode.lower():
        head = "%s · %s" % (label, mode)
    else:
        head = label or mode or r.get("kind") or "take"
    bits = []
    if it.get("seed") is not None:
        bits.append("seed %s" % it.get("seed"))
    if it.get("steps"):
        bits.append("%s steps" % it.get("steps"))
    if it.get("seconds"):
        bits.append("%.1f s" % it["seconds"])
    if r.get("when"):
        bits.append(_age(now - r["when"]))
    if r.get("bytes"):
        bits.append(_size(r["bytes"]))
    if r.get("name"):
        bits.append(r["name"])                 # the file itself: a take with no index entry
    body = (it.get("prompt") or "").strip().replace("\n", " ")
    return head, (body[:150] + ("…" if len(body) > 150 else "")), " · ".join(bits)


def build(dock, ctx, limit=None):
    """Build the Home tab. Returns the docked window (for tests)."""
    C = ctx["C"]
    fonts = ctx.get("fonts") or {}
    fsm = fonts.get("fsm") or ("TkDefaultFont", 11)
    fh = fonts.get("fh") or ("TkDefaultFont", 11)
    mono = (ctx.get("mono") or {}).get("s") or fsm
    thumb_of = ctx.get("thumb")                  # forge_lab.thumb_photo — None in a bare test
    poster_of = ctx.get("poster")                # forge_lab.video_poster
    full_of = ctx.get("open_full")               # forge_lab's open_full (macOS Preview)
    T = ctx.get("takes") or {}                   # the image tab's own filter machinery
    read_index = T.get("index")
    kind_of = T.get("kind") or (lambda it: KIND_OF.get(
        os.path.splitext(str(((it.get("images") or [{}])[0] or {}).get("file") or ""))[1]
        .lower(), "file"))
    filter_kind = T.get("filter_kind")
    filter_takes = T.get("filter_takes")
    rating_of = T.get("rating") or (lambda it: 0)
    kinds = list(T.get("kinds") or [])
    ratings = list(T.get("ratings") or [])
    shown_cap = int(T.get("shown") or limit or ROWS)
    runs = T.get("runs") or runs_dir()
    state = T.get("state") if isinstance(T.get("state"), dict) else {}
    rating_var = T.get("rating_var")
    on_filter = T.get("on_filter")           # tell the image tab its filter state moved
    filtered = bool(read_index and filter_kind and filter_takes and kinds and ratings)

    win = dock.window()
    win.title("Home")
    page = tk.Frame(win, bg=C["bg"])
    page.pack(fill="both", expand=True, padx=18, pady=14)

    tk.Label(page, text="HOME", bg=C["bg"], fg=C["ink"], font=fh).pack(anchor="w")
    tk.Label(page, text="what is running, what is waiting, and what came out",
             bg=C["bg"], fg=C["muted"], font=fsm).pack(anchor="w", pady=(0, 10))

    cols = tk.Frame(page, bg=C["bg"])
    cols.pack(fill="both", expand=True)
    left = tk.Frame(cols, bg=C["bg"])
    left.pack(side="left", fill="both", expand=True)
    right = tk.Frame(cols, bg=C["bg"], width=470)
    right.pack(side="right", fill="y")
    right.pack_propagate(False)

    # ---- QUEUE (the panel owns its own 2 s repaint; no ssh)
    # ⚠️ BUILDING A PANEL DOES NOT SHOW IT. build_queue() (like cards_ui.build_panel) returns
    # its frame and the OWNER packs it — without the pack the panel sat in the tree, held its
    # rows, and was 1x1 and unmapped: the Home tab shipped with an invisible queue because the
    # driver walked winfo_children() (which sees unmapped widgets) instead of asking whether it
    # was on screen. The TESTQUEUE driver now asserts mapped + sized.
    try:
        qbox = home_queue.build_queue(left, dict(ctx, queue=ctx.get("queue")
                                                 or home_queue.app_queue()))
        qbox.pack(fill="x")
    except Exception as exc:                      # noqa: BLE001
        tk.Label(left, text="queue panel unavailable: %r" % (exc,), bg=C["bg"],
                 fg=C["faint"], font=fsm).pack(anchor="w")

    # ---- RECENT TAKES (thumbnail rows, the image tab's shape AND its filters)
    tk.Label(left, text="RECENT TAKES", bg=C["bg"], fg=C["muted"], font=fh).pack(
        anchor="w", pady=(16, 2))

    kind_btns = {}
    dirty = {"v": True}              # a repaint is needed
    rows = []                        # the take dicts currently on screen
    thumbs = []                      # PhotoImage refs — Tk drops an image nobody points at
    posters_wanted, posters_wanted_list = set(), []

    def _filters_moved():
        """The image tab shares this state: let it repaint its own chips and list at once."""
        try:
            if on_filter is not None:
                on_filter()
        except Exception:                          # noqa: BLE001
            pass

    def _paint_kind():
        for _k, _b in kind_btns.items():
            _on = _k == state.get("take_kind", "All")
            _b.configure(bg=C["ink"] if _on else C["panel2"],
                         fg="#14110f" if _on else C["ink"])

    def _set_kind(k):
        state["take_kind"] = k if k in kinds else "All"
        _paint_kind()
        try:
            tcanvas.yview_moveto(0.0)
        except Exception:                          # noqa: BLE001
            pass
        _filters_moved()
        _fill(True)

    def _paint_unkept():
        _on = bool(state.get("take_unkept", False))
        _unkept.configure(bg=C["ink"] if _on else C["panel2"],
                          fg="#14110f" if _on else C["ink"])

    def _toggle_unkept():
        state["take_unkept"] = not state.get("take_unkept", False)
        _paint_unkept()
        _filters_moved()
        _fill(True)

    if filtered:
        ctl = tk.Frame(left, bg=C["bg"])
        ctl.pack(anchor="w", pady=(2, 0))
        # LABELS, not Buttons: macOS paints a Button light whatever bg says, a Label it honours
        for _kf in kinds:
            _kb = tk.Label(ctl, text=_kf, bg=C["panel2"], fg=C["ink"], font=fsm,
                           padx=12, pady=3, cursor="hand2")
            _kb.bind("<Button-1>", lambda _e, k=_kf: _set_kind(k))
            _kb.pack(side="left", padx=(0, 4))
            kind_btns[_kf] = _kb
        state.setdefault("take_kind", "All")
        tk.Label(ctl, text="show", bg=C["bg"], fg=C["faint"], font=fsm).pack(
            side="left", padx=(10, 0))
        if rating_var is None:
            rating_var = tk.StringVar(value="All")
        _rmenu = tk.OptionMenu(ctl, rating_var, *ratings,
                               command=lambda *_a: (_filters_moved(), _fill(True)))
        _rmenu.configure(bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
                         activeforeground="#fff", relief="flat", highlightthickness=0,
                         font=fsm)
        # ⚠️ the POPUP is its own tk.Menu and ignores the button's colours — macOS would flash
        # a white panel out of a near-black app unless it is configured explicitly.
        _rmenu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
        _rmenu.pack(side="left", padx=(6, 10))
        _unkept = tk.Button(ctl, text="unkept only", bg=C["panel2"], fg=C["ink"],
                            activebackground=C["rust"], activeforeground="#fff",
                            relief="flat", font=fsm, padx=8, pady=1, borderwidth=0,
                            command=lambda: _toggle_unkept())
        _unkept.pack(side="left")
        _paint_kind()
        _paint_unkept()
        _seen_filters = {"v": None}

        def _follow_filters():
            """The image tab may change its filter while this tab is open: follow it."""
            v = (state.get("take_kind", "All"),
                 (rating_var.get() if rating_var is not None else "All"),
                 bool(state.get("take_unkept", False)))
            if v != _seen_filters["v"]:
                _seen_filters["v"] = v
                _paint_kind()
                _paint_unkept()
                dirty["v"] = True

    hdr = tk.Label(left, text="", bg=C["bg"], fg=C["faint"], font=fsm, anchor="w")
    hdr.pack(anchor="w")

    tbox = tk.Frame(left, bg=C["bg"])
    tbox.pack(fill="both", expand=True, pady=(4, 0))
    tcanvas = tk.Canvas(tbox, bg=C["bg"], highlightthickness=0, borderwidth=0,
                        yscrollincrement=1)
    tsbar = tk.Scrollbar(tbox, orient="vertical", command=tcanvas.yview,
                         troughcolor=C["panel"], bg=C["panel2"], borderwidth=0,
                         highlightthickness=0, width=10)
    tcanvas.configure(yscrollcommand=tsbar.set)
    tsbar.pack(side="right", fill="y")
    tcanvas.pack(side="left", fill="both", expand=True)
    tinner = tk.Frame(tcanvas, bg=C["bg"])
    tcanvas.create_window((0, 0), window=tinner, anchor="nw")
    tinner.bind("<Configure>",
                lambda _e: tcanvas.configure(scrollregion=tcanvas.bbox("all")))
    tcanvas.bind("<Configure>",
                 lambda e: tcanvas.itemconfigure(1, width=e.width))

    # ⚠️ Tk does NOT bubble an event to the parent widget, and the app's global wheel router
    # only knows the image tab's canvases (it walks up from the widget under the pointer
    # looking for lcanvas/tcanvas). So the wheel has to be bound on the ROWS as well as the
    # canvas — bound only here, scrolling with the pointer over a take did nothing. The global
    # router ignores these widgets, so there is no double-scroll.
    def _wheel(ev, touchpad=False):
        dy = scroll_delta(ev, touchpad)
        if not dy:
            return
        try:
            tcanvas.yview_scroll(max(-240, min(240, -dy)), "units")
        except Exception:                          # noqa: BLE001
            pass

    def _bind_wheel(w):
        for _seq, _tp in (("<MouseWheel>", False), ("<TouchpadScroll>", True)):
            try:
                w.bind(_seq, lambda e, t=_tp: _wheel(e, t))
            except Exception:                      # noqa: BLE001
                pass

    for _w in (tcanvas, tinner):
        _bind_wheel(_w)

    def _warm_posters(paths):
        """Make the missing video posters on ONE background thread.

        ⚠️ A worker never touches Tk here: it writes the poster file and marks the page dirty,
        so the next poll repaints and finds it (win.after() called from a worker was seen never
        to run under a driver).
        """
        def _one():
            for p in paths:
                try:
                    if poster_of is not None:
                        poster_of(p, True)
                except Exception:                  # noqa: BLE001
                    pass
            dirty["v"] = True                      # a plain flag write: no Tk from a worker
        threading.Thread(target=_one, daemon=True).start()

    def _open(path):
        if full_of is not None:
            full_of(path)                          # the image tab's own "open full size"
            return
        try:
            import subprocess
            subprocess.Popen(["open", path])
        except Exception:                          # noqa: BLE001
            pass

    def _gather():
        """The takes to draw, in the image tab's own order and with its own filters.

        `filter_kind` FIRST, then the 100, then the rating ∩ unkept filter — exactly the image
        tab's pipeline, so "Songs" is your last 100 songs.
        """
        total, found = 0, []
        if filtered:
            try:
                ents = filter_kind(read_index(), state.get("take_kind", "All"))[:shown_cap]
            except Exception:                      # noqa: BLE001
                ents = []
            total = len(ents)
            keep = {i.get("id") for i in filter_takes(
                ents, (rating_var.get() if rating_var is not None else "All"),
                bool(state.get("take_unkept", False)))}
            for it in ents:
                if it.get("id") not in keep:
                    continue
                path = None
                if it.get("images"):
                    cand = os.path.join(runs, str(it.get("id")),
                                        str((it["images"][0] or {}).get("file") or ""))
                    if os.path.exists(cand):
                        path = cand
                when = bytes_ = None
                if path:
                    try:
                        st_ = os.stat(path)
                        when, bytes_ = st_.st_mtime, st_.st_size
                    except OSError:
                        pass
                found.append({"id": it.get("id"), "it": it, "path": path,
                              "name": os.path.basename(path) if path else "",
                              "kind": kind_of(it),
                              "when": when or it.get("started") or it.get("finished"),
                              "bytes": bytes_})
        else:
            for r in recent_runs(shown_cap):
                r["it"] = {}
                found.append(r)
            total = len(found)
        return total, found

    def _fill(force=False):
        if not force and not dirty["v"]:
            return
        dirty["v"] = False
        rows.clear()
        del thumbs[:]
        del posters_wanted_list[:]
        for w in tinner.winfo_children():
            w.destroy()
        now = time_now()
        total, found = _gather()
        for r in found:
            it = r.get("it") or {}
            rejected = rating_of(it) == -1
            row_bg = C["bg"] if rejected else C["panel"]      # a ✕ take is dim, like the Make tab
            row = tk.Frame(tinner, bg=row_bg, highlightthickness=1,
                           highlightbackground=C["line"])
            row.pack(fill="x", pady=3, padx=(0, 2))
            rows.append(r)
            path, vid = r.get("path"), r["kind"] == "video"
            pic = None
            if thumb_of is not None and path:
                if vid:
                    pst = poster_of(path, False) if poster_of is not None else None
                    if pst:
                        pic = thumb_of(pst, BOX)
                    elif poster_of is not None and path not in posters_wanted:
                        posters_wanted.add(path)
                        posters_wanted_list.append(path)
                elif r["kind"] == "image":
                    pic = thumb_of(path, BOX)
            if pic is not None:
                thumbs.append(pic)                 # keep a reference or Tk draws nothing
            # ⚠️ The ▶ is the MAKE TAB's own signal, and only when a poster EXISTS: with a
            # poster it is a white ▶ drawn centred OVER it (compound), without one the row
            # says "[video]". Using ▶ as the no-poster placeholder made the two states
            # indistinguishable — a test could not tell a real preview from a promise.
            if pic is not None and vid:
                lbl = tk.Label(row, bg=row_bg, image=pic, text="▶", compound="center",
                               fg="#ffffff", font=("Helvetica", 28), cursor="hand2")
            else:
                lbl = tk.Label(row, bg=row_bg, image=pic,
                               text="" if pic else ("[video]" if vid else
                                                    "♪ song" if r["kind"] == "song" else
                                                    "[no preview]"),
                               fg=C["muted"] if pic else C["faint"], font=fsm,
                               width=0 if pic else 14, cursor="hand2")
            lbl.pack(side="left", padx=8, pady=8)
            if path:
                lbl.bind("<Button-1>", lambda _e, p=path: _open(p))
            head, body, meta = _line(it, r, now)
            txt = tk.Label(row, bg=row_bg, fg=C["faint"] if rejected else C["ink"], font=fsm,
                           justify="left", anchor="w", wraplength=520,
                           text="%s\n%s\n%s" % (head, body, meta) if body
                           else "%s\n%s" % (head, meta))
            txt.pack(side="left", padx=6, pady=8, fill="x", expand=True)
            if path:
                for _w in (row, txt):
                    _w.bind("<Double-Button-1>", lambda _e, p=path: _open(p))
            for _w in (row, lbl, txt):            # the wheel over a ROW scrolls the list
                _bind_wheel(_w)
        if not rows:
            _kn = {"Images": "pictures", "Videos": "videos",
                   "Songs": "songs"}.get(state.get("take_kind", "All"), "takes")
            tk.Label(tinner, text=("No takes match this filter." if total
                                   else "No %s yet." % _kn),
                     bg=C["bg"], fg=C["muted"], font=fsm, anchor="w").pack(anchor="w", pady=10)
        hdr.configure(text="%d of %d %s%s  ·  click a thumbnail to open it full size  ·  "
                           "double-click a row to open the file%s"
                      % (len(rows), total,
                         {"Images": "image", "Videos": "video",
                          "Songs": "song"}.get(state.get("take_kind", "All"), "take"),
                         "" if total == 1 else "s",
                         "  ·  %d video poster%s coming" % (len(posters_wanted_list),
                                                            "" if len(posters_wanted_list) == 1
                                                            else "s")
                         if posters_wanted_list else ""))
        if posters_wanted_list:
            _warm_posters(list(posters_wanted_list))

    _seen_index = {"v": None}

    def _poll():
        # a take landing rewrites the index: that is the only thing worth a repaint. Rebuilding
        # 100 rows (and re-decoding their thumbnails) every 15 s would flicker for nothing.
        try:
            m = os.path.getmtime(index_path())
            if m != _seen_index["v"]:
                _seen_index["v"] = m
                dirty["v"] = True
        except OSError:
            pass
        if filtered:
            try:
                _follow_filters()
            except Exception:                      # noqa: BLE001
                pass
        try:
            _fill()
        except Exception:                          # noqa: BLE001
            pass
        try:
            win.after(15000, _poll)                # a take does not appear every second
        except Exception:                          # noqa: BLE001
            pass

    _fill(True)
    _poll()

    # ---- MACHINE ROOM
    try:
        cbox = cards_ui.build_panel(right, ctx)
        cbox.pack(fill="both", expand=True)       # see the QUEUE note above: build ≠ shown
    except Exception as exc:                       # noqa: BLE001
        tk.Label(right, text="machine room unavailable: %r" % (exc,), bg=C["bg"],
                 fg=C["faint"], font=fsm).pack(anchor="w")

    win._home_takes = tinner                    # the takes area (assert it is ON SCREEN)
    win._home_rows = rows                       # the take dicts on screen
    win._home_thumbs = thumbs                   # live image refs
    win._home_canvas = tcanvas
    win._home_hdr = hdr
    win._home_kind_btns = kind_btns
    win._home_mono = mono
    win._home_fill = lambda: _fill(True)        # a driver can seed takes and repaint at once
    return win


def time_now():
    """Separated so a test can freeze it if it ever needs to."""
    import time
    return time.time()
