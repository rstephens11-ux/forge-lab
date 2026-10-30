"""dialogue_ui.py — DIALOGUE window for Forge Lab.

open_dialogue(root, ctx) — the window; tkinter imported inside.

ctx keys: C (palette dict), fonts {f,fh,fsm}, mono {m}, btn_colors,
          data (DATA path), _driver (optional, for driver gating).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import dialogue as dlg   # stdlib-only engine; safe at module top (no tkinter)
try:
    import dispatch as _dsp      # the rig's queue (Tk-free); None if it is missing
except Exception:                # noqa: BLE001
    _dsp = None


def mood_wav_own(v, mood):
    """Absolute path of a voice's OWN sample for `mood`, or None.

    ⚠️ Deliberately NOT the neutral fallback that dlg.voice_wav() gives bank voices: that is
    right for playback and wrong for planning — it made a brand-new mood look already designed.
    Character-pack voices bring their own wav, so theirs is taken as-is.
    """
    if v.get("source") == "character":
        return dlg.voice_wav(v, mood)
    mw = ((v.get("moods") or {}).get(mood) or {}).get("wav") or ""
    if not mw:
        return None
    slug = v.get("slug") or dlg.slugify(v.get("name", ""))
    path = os.path.join(dlg.voices_dir(), slug, mw)
    return path if os.path.isfile(path) else None


def plan_render(pj, voices, mode="missing", engine="qwen"):
    """PURE: what a RENDER click must send. -> (design_items, clone_job or None, skipped msgs).

    * design_items: one per reference sample a line needs that does not exist yet.
      Character-pack voices are never designed (read-only; they bring their own wav).
      ⚠️ Designs ALWAYS run on qwen, under BOTH engines: only VoiceDesign can make a voice
      from a DESCRIPTION, and fish has no VoiceDesign at all. Under `engine="fish"` the only
      sample worth designing is a voice's NEUTRAL one — the single reference fish clones from.
    * clone_job: the rig job for every line with no take (mode "missing") or every line
      (mode "all").
        - qwen: voices keyed "<slug>:<mood>" — there a mood IS its own reference sample.
        - fish: voices keyed "<slug>" — ONE reference per voice, and the mood rides in the
          line's TEXT as an inline `[tag]` (see dlg.fish_line_text).
      Lines whose sample does not exist yet are left out (they come in after the design batch).
    * A line's mood: its own "mood", else mood_of(direction, the voice's moods), else neutral.
    """
    designs, clones, voices_map, skipped = [], [], {}, []
    seen = set()
    # "inline tag" engines — fish (S2-Pro) and drama3 (Drama 3 int4) both take the mood as a
    # `[tag]` in the line's TEXT and clone from ONE reference per voice. Neither has a
    # VoiceDesign, so designs still run on qwen under either.
    fish = engine in ("fish", "drama3")
    for i, li in enumerate(pj.get("lines", [])):
        if dlg.is_sung(li):
            continue        # sung lines go to YuE2 — see plan_sung()
        spk = dlg.normalise_speaker(li.get("speaker", ""))
        slug = (pj.get("cast") or {}).get(spk)
        v = voices.get(slug) if slug else None
        if v is None:
            msg = "no voice cast for %s" % spk
            if msg not in skipped:
                skipped.append(msg)
            continue
        moods = v.get("moods") or {}
        if fish:
            # ⚠️ fish does NOT limit the mood to the samples a voice happens to have designed:
            # the mood is a free inline tag, so every mood in the vocabulary is available
            # (that is the whole point — `*(shouting)*` can shout without a shouting sample).
            # Under qwen the mood MUST name an existing sample, hence the cap there.
            avail = list(dlg.MOOD_WORDS)
        else:
            avail = list(moods) or ["neutral"]
        mood = li.get("mood") or dlg.mood_of(li.get("direction", ""), avail)
        if not fish and mood not in moods and v.get("source") == "character":
            mood = "neutral"
        # fish clones the voice's NEUTRAL sample whatever the mood is — the TAG carries the mood
        sample = "neutral" if fish else mood
        key = slug if fish else "%s:%s" % (slug, mood)
        wav = mood_wav_own(v, sample)
        if not wav:
            if key not in seen and v.get("source") != "character":
                seen.add(key)
                md = moods.get(sample) or {}
                designs.append({"id": "%s:%s" % (slug, sample),
                                "instruct": dlg.compose_description(v, sample),
                                "text": md.get("ref_text") or dlg.DEFAULT_SAMPLE,
                                "out": "%s__%s.wav" % (slug, sample)})
            elif v.get("source") == "character":
                skipped.append("%s has no voice.wav" % slug)
            continue
        if li.get("takes") and mode != "all":
            continue
        voices_map[key] = {"ref_wav": wav,
                           "ref_text": (moods.get(sample) or {}).get("ref_text") or dlg.DEFAULT_SAMPLE}
        clones.append({"id": "line%d" % i, "voice": key,
                       "text": dlg.fish_line_text(mood, li["text"], v.get("fish_direction"))
                       if fish else li["text"],
                       "out": "line%03d.wav" % i, "_line": i})
    job = None
    if clones:
        job = {"design": [], "clone": clones, "voices": voices_map, "language": "English"}
    return designs, job, skipped


def plan_sung(pj, voices, mode="missing"):
    """PURE: the YuE2 jobs for SUNG lines -> list of {_line, style, lyrics, seconds}.
    A sung line with no cast voice still sings (generic singer) — the voice only steers it."""
    out = []
    for i, li in enumerate(pj.get("lines", [])):
        if not dlg.is_sung(li) or (li.get("takes") and mode != "all"):
            continue
        slug = (pj.get("cast") or {}).get(dlg.normalise_speaker(li.get("speaker", "")))
        v = voices.get(slug) if slug else None
        out.append({"_line": i, "style": dlg.sung_style(v, pj.get("sung_style")),
                    "lyrics": dlg.sung_lyrics(li["text"]),
                    "seconds": dlg.sung_seconds(li["text"])})
    return out


def sung_estimate_seconds(items):
    import song as _song
    return sum(_song.yue2_estimate_seconds(x["seconds"], plan=True) for x in items)


# a-cappella YuE2 output -> the dialogue's own format, with the dead air at both ends cut, so a
# sung line drops into assemble()/peak_normalise() beside the 24 kHz mono speech takes.
_SUNG_AF = ("silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
            "silenceremove=start_periods=1:start_threshold=-45dB,areverse")


def render_sung(items, pj, proj_dir, submit, wait_and_collect, runs_dir, progress=None):
    """Run each sung item through the app's own Song path (submit("song") -> GPU0 ComfyUI),
    convert to 24 kHz mono wav, append it as a take. Returns the lines that got a new take.
    ⚠️ Worker-thread safe: no Tk. One item failing marks THAT line and carries on."""
    import song as _song
    made = []
    for n, it in enumerate(items):
        li = pj["lines"][it["_line"]]
        if progress:
            progress("singing line %d (%d/%d) on YuE2" % (it["_line"] + 1, n + 1, len(items)))
        try:
            p = _song.yue2_plan_params(it["style"], it["lyrics"], seconds=it["seconds"],
                                       prefix="dialogue/sung")
            if _dsp is not None and _dsp.running():
                # through the QUEUE (2026-10-22): a sung line waits for a card like everything
                # else. The submit/wait seam below stays for callers with no scheduler (tests).
                meta = _dsp.render("Sung line %d — %s" % (it["_line"] + 1, pj.get("slug") or ""),
                                   "song", p, None, need="song",
                                   est_s=float(it.get("seconds") or 30) * 4) or {}
            else:
                pid, _seed = submit("song", p, None)
                meta = wait_and_collect(pid, timeout=600) or {}
            files = [x for x in (meta.get("images") or [])
                     if (x.get("file") or "").lower().endswith((".flac", ".wav", ".mp3"))]
            if not files:
                raise RuntimeError("YuE2 finished but no audio came back (%s)"
                                   % (meta.get("state") or meta.get("error") or "?"))
            src = os.path.join(runs_dir, meta.get("id") or "", files[0]["file"])
            fn = "%03d_%d_sung.wav" % (li["idx"], len(li.get("takes", [])) + 1)
            dst = os.path.join(proj_dir, "lines", fn)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-af", _SUNG_AF,
                            "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", dst],
                           check=True, capture_output=True, timeout=120)
            secs = dlg._ffprobe_duration(dst)
            li.pop("error", None)
            li.setdefault("takes", []).append({"file": "lines/" + fn, "seconds": secs,
                                               "heard": "", "score": None, "sung": True,
                                               "run": meta.get("id") or ""})
            li["kept"] = len(li["takes"]) - 1
            made.append(li)
        except Exception as exc:
            li["error"] = "sung: %s" % exc
    return made


def open_dialogue(root, ctx):
    """Open the DIALOGUE workspace window — a Toplevel with numbered steps.

    Step 1: SCRIPT — paste/type, PARSE, project save/load.
    Step 2: CAST — assign voices to speakers.
    Step 3: VOICE — editor for a selected voice (sliders, moods, FX).
    Step 4: LINES — per-line takes, playback, redo.
    Step 5: MAKE — estimate, render missing/all, assemble, play.
    """
    import tkinter as tk
    from tkinter import filedialog as _fd
    from tkinter import simpledialog as _sd
    from tkinter import messagebox as _mb

    # ------------------------------------------------------------------ unpack context
    C = ctx["C"]
    f, fh, fsm = ctx["fonts"]["f"], ctx["fonts"]["fh"], ctx["fonts"]["fsm"]
    fmono = ctx["mono"]["m"]
    btn_colors = ctx.get("btn_colors")
    DATA = (ctx.get("data")
            or os.environ.get("FORGE_LAB_DATA",
                              os.path.join(os.path.expanduser("~"), "forge-lab")))
    # driver gating — replicated from forge_lab.py's _DRIVER
    _driver = ctx.get("_driver", False)
    if not _driver:
        _driver = any(k.startswith("FORGE_LAB_") and v == "1"
                      for k, v in os.environ.items())
    _go = os.environ.get("FORGE_LAB_TESTDIALOGUE_GO") == "1"
    # ------------------------------------------------------------------ engine imports (INSIDE the function)
    import dialogue as dlg
    SUNG_CHOICE = "\u266a sung"

    # ------------------------------------------------------------------ helpers
    RED = "#e0553b"

    # ------------------------------------------------------------------ window
    import dock as _dock                 # a tab in the main window (Ryan: tabs)
    win = _dock.window(root)
    win.title("Dialogue")
    win.configure(bg=C["bg"])
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1180, sw - 40), min(860, sh - 120)))

    st = {"pj": None, "editing": None, "voices": {}, "busy": None,
          "player": None, "job": None, "progress": "", "cast_vars": {}, "cast_menus": {},
          "cast_rows": {}}

    # ------------------------------------------------------------------ voice engine state
    # Created HERE, above every handler. The trap this app has already hit once: a handler
    # defined above a widget referring to something defined below it — a NameError at
    # CONSTRUCTION takes the whole window down, and inside a Tk callback it is swallowed.
    # Only the widgets themselves belong in step 4.
    _ENGINE_NOTES = {
        "qwen": "design + clone \u00b7 a mood is its own DESIGNED sample",
        "fish": "S2-Pro \u00b7 a mood is an inline [tag], ONE reference per voice \u00b7 5\u00d7 the load",
        "drama3": "Drama 3 int4 \u00b7 inline [tag], ONE reference per voice \u00b7 "
                  "server start ~2 min, then ~1\u00d7 realtime",
    }
    eng_var = tk.StringVar(value=dlg.engine_label(dlg.DEFAULT_ENGINE))
    _eng_lbl = {}

    def _engine_key():
        """The engine key behind the dropdown's current label."""
        lab = eng_var.get()
        for k, l in dlg.ENGINES:
            if l == lab:
                return k
        return dlg.DEFAULT_ENGINE

    def _engine_note():
        if _eng_lbl.get("w") is not None:
            _eng_lbl["w"].configure(text=_ENGINE_NOTES.get(_engine_key(), ""))

    def _sync_engine(*_a):
        """Mirror the project's engine into the dropdown. A project with no key stays qwen."""
        if st["pj"] is None:
            eng_var.set(dlg.engine_label(dlg.DEFAULT_ENGINE))
        else:
            eng_var.set(dlg.engine_label(st["pj"].get("engine") or dlg.DEFAULT_ENGINE))
        _engine_note()

    def _engine_changed(*_a):
        if st["pj"] is not None:
            st["pj"]["engine"] = _engine_key()
        _engine_note()
        _refresh_estimate()

    # ------------------------------------------------------------------ widget helpers
    def lam(parent, text, **kw):
        kw.setdefault("bg", C["panel"])
        kw.setdefault("fg", C["muted"])
        return tk.Label(parent, text=text, font=kw.pop("font", fsm), **kw)

    def btn(parent, text, cmd, primary=False):
        return tk.Button(parent, text=text,
                         bg=(C["rust_deep"] if primary else C["panel2"]),
                         fg="#fff" if primary else C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=10, pady=3,
                         borderwidth=0, highlightthickness=1,
                         highlightbackground=C["line"], command=cmd)

    def say(msg, warn=False):
        try:
            status.configure(text=msg, fg=(RED if warn else C["faint"]))
        except Exception:
            pass

    def _open(path):
        if not path:
            say("nothing to open", warn=True)
            return
        try:
            subprocess.Popen(["open", path])
        except Exception as exc:
            say("could not open: %r" % exc, warn=True)

    def stop_play():
        p = st.get("player")
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        st["player"] = None

    def play_audio(path):
        if not path or not os.path.exists(path):
            say("nothing to play", warn=True)
            return
        stop_play()
        try:
            st["player"] = subprocess.Popen(["afplay", path])
            say("playing %s" % os.path.basename(path))
        except Exception as exc:
            say("could not play: %r" % exc, warn=True)

    # ------------------------------------------------------------------ header
    hd = tk.Frame(win, bg=C["bg"])
    hd.pack(fill="x", padx=14, pady=(12, 2))
    tk.Label(hd, text="DIALOGUE", bg=C["bg"], fg=C["ink"], font=fh).pack(side="left")
    status = tk.Label(hd, text="", bg=C["bg"], fg=C["faint"], font=fsm)
    status.pack(side="left", padx=12)
    btn(hd, "CLOSE", win.destroy).pack(side="right")

    # ------------------------------------------------------------------ body: left column (scrollable)
    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=14, pady=(0, 14))

    # ------------------------------------------------------------- QUICK LINE (pinned, top)
    # Ryan, 2026-10-15: "i cant easily type a sentence and see the output or choose accents".
    # The 5-step flow has NO sound in it until SCRIPT+CAST+VOICE exist, and the editor that owns
    # the 28 ACCENTS is a separate Toplevel behind CAST's EDIT. This bar is the fast path: one
    # sentence, one voice (existing or SCRATCH), one press. It reuses the AUDITION path, so the
    # typed sentence simply BECOMES that voice's sample text - no new engine code.
    QL_SCRATCH = "\u2014 scratch voice \u2014"
    qlf = tk.Frame(body, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    qlf.pack(fill="x", pady=(0, 8))
    # ONE row (redesign 2026-10-17): sentence, voice, Speak, replay. The scratch-voice knobs
    # (gender / age / accent / save accent) fold behind "scratch voice ▾" — on the Mac the old
    # second row ran off the right edge and clipped SET ACCENT.
    _qlh = tk.Frame(qlf, bg=C["panel"]); _qlh.pack(fill="x", padx=10, pady=(8, 8))
    tk.Label(_qlh, text="TRY A LINE", bg=C["panel"], fg=C["ochre"], font=fmono).pack(side="left")
    ql_txt = tk.StringVar(value="")
    tk.Entry(_qlh, textvariable=ql_txt, bg="#100e0c", fg=C["ink"], relief="flat",
             font=fmono, insertbackground=C["ink"]).pack(side="left", fill="x", expand=True,
                                                         padx=(8, 6))
    ql_voice = tk.StringVar(value=QL_SCRATCH)
    ql_vmenu = tk.OptionMenu(_qlh, ql_voice, QL_SCRATCH)
    ql_vmenu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                       highlightthickness=0, activebackground=C["rust"],
                       activeforeground="#fff", borderwidth=0, width=14, anchor="w")
    ql_vmenu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    ql_vmenu.pack(side="left")
    btn(_qlh, "\u21bb", lambda: _ql_refill()).pack(side="left", padx=(2, 8))
    btn(_qlh, "Speak", lambda: _ql_speak()).pack(side="left")
    btn(_qlh, "\u25b6 Last", lambda: _ql_play()).pack(side="left", padx=(6, 0))
    _ql_more = tk.Label(_qlh, text="scratch voice \u25be", bg=C["panel"], fg=C["ochre"],
                        font=fsm, cursor="hand2")
    _ql_more.pack(side="left", padx=(10, 0))
    _qlr2 = tk.Frame(qlf, bg=C["panel"])          # folded until "scratch voice ▾"
    tk.Label(_qlr2, text="scratch voice:", bg=C["panel"], fg=C["faint"], font=fsm).pack(side="left")
    ql_gender = tk.StringVar(value="male")
    _gm = tk.OptionMenu(_qlr2, ql_gender, "male", "female")
    _gm.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, width=7, anchor="w")
    _gm["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _gm.pack(side="left", padx=4)
    tk.Label(_qlr2, text="age", bg=C["panel"], fg=C["faint"], font=fsm).pack(side="left", padx=(8, 2))
    ql_age = tk.Entry(_qlr2, width=4, bg="#100e0c", fg=C["ink"], relief="flat",
                      font=fmono, insertbackground=C["ink"])
    ql_age.insert(0, "30"); ql_age.pack(side="left")
    tk.Label(_qlr2, text="accent", bg=C["panel"], fg=C["faint"], font=fsm).pack(side="left", padx=(8, 2))
    ql_accent = tk.StringVar(value="\u2014 none \u2014")
    _am = tk.OptionMenu(_qlr2, ql_accent, *[a for a, _w in dlg.ACCENTS])
    _am.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, highlightthickness=0,
                  borderwidth=0, width=22, anchor="w")
    _am["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    _am.pack(side="left", padx=4)
    btn(_qlr2, "Save accent to voice", lambda: _ql_set_accent()).pack(side="left", padx=(8, 0))

    def _ql_toggle(_e=None):
        if _qlr2.winfo_manager():
            _qlr2.pack_forget()
            _ql_more.configure(text="scratch voice \u25be")
        else:
            _qlr2.pack(fill="x", padx=10, pady=(0, 8), after=_qlh)
            _ql_more.configure(text="scratch voice \u25b4")
    _ql_more.bind("<Button-1>", _ql_toggle)

    # Row 3: WHAT YOU MADE. A one-shot sound you cannot replay is not a take: the clip has to
    # stay visible and re-playable (Ryan, 2026-10-15: "i still dont see an option to listen to
    # the quick line i generated").
    _qlr3 = tk.Frame(qlf, bg=C["panel"]); _qlr3.pack(fill="x", padx=10, pady=(0, 8))
    _ql_last = tk.Label(_qlr3, text="nothing yet - type a sentence and press SPEAK",
                        bg=C["panel"], fg=C["faint"], font=fsm, justify="left",
                        anchor="w", wraplength=1000)
    _ql_last.pack(anchor="w")
    _ql_hist = tk.Frame(_qlr3, bg=C["panel"]); _ql_hist.pack(anchor="w", pady=(4, 0))

    def _ql_refill():
        """Rebuild the voice menu from the bank - voices are MADE in CAST, so rescan on demand."""
        m = ql_vmenu["menu"]; m.delete(0, "end")
        for lab in [QL_SCRATCH] + [v.get("name", "?") for v in dlg.list_voices()]:
            m.add_command(label=lab, command=lambda lab=lab: ql_voice.set(lab))
        ql_voice.set(QL_SCRATCH)

    def _ql_speak():
        txt = (ql_txt.get() or "").strip()
        if not txt:
            say("type a sentence in QUICK LINE first", warn=True); return
        lab = ql_voice.get()
        if lab == QL_SCRATCH:
            words = dict(dlg.ACCENTS).get(ql_accent.get(), "")
            try:
                age = int(ql_age.get() or 30)
            except Exception:
                age = 30
            v = dlg.new_voice("Scratch \u2014 %s" % (ql_accent.get() or "voice").strip("\u2014 "),
                              gender=ql_gender.get(), age_years=age, accent=words)
        else:
            v = None
            for cand in dlg.list_voices():
                if cand.get("name") == lab:
                    v = cand; break
            if v is None:
                say("voice %r not found - press \u21bb to rescan" % lab, warn=True); return
        # \u26a0\ufe0f DEDICATED mood key. Writing ref_text into "neutral" and auditioning would
        # overwrite that voice's real reference sample (neutral.wav) with this sentence.
        v.setdefault("moods", {})["quick"] = {"extra": "", "wav": "quick.wav", "ref_text": txt}
        st["editing"] = v
        say("designing \u201c%s\u2026\u201d in %s" % (txt[:32], v.get("name")))
        st["ql_pending"] = {"text": txt, "voice": v.get("name")}
        _start_tts_job("audition", v, "quick")

    def _ql_takes():
        return st.setdefault("ql_takes", [])

    def _ql_play():
        tk_ = _ql_takes()
        if not tk_:
            say("nothing to play yet - press SPEAK", warn=True); return
        play_audio(tk_[0]["wav"])

    def _ql_show():
        """Name the clip on screen and give every recent one a PLAY button."""
        tk_ = _ql_takes()
        if not tk_:
            _ql_last.configure(text="nothing yet - type a sentence and press SPEAK")
            return
        t0 = tk_[0]
        _ql_last.configure(text="LAST:  %s   \u00b7   %s   \u00b7   \u201c%s\u2026\u201d"
                                % (os.path.basename(t0.get("wav", "")), t0.get("voice", "?"),
                                   (t0.get("text") or "")[:44]))
        for w in _ql_hist.winfo_children():
            w.destroy()
        tk.Label(_ql_hist, text="replay:", bg=C["panel"], fg=C["faint"],
                 font=fsm).pack(side="left")
        for i2, t2 in enumerate(tk_):
            btn(_ql_hist, "\u25b6 %d" % (i2 + 1),
                lambda p=t2["wav"]: play_audio(p)).pack(side="left", padx=2)
        btn(_ql_hist, "OPEN FOLDER", lambda: _open(dlg.voices_dir())).pack(side="left", padx=(8, 0))

    def _ql_set_accent():
        """Write the picked accent onto a BANKED voice - so an accent never needs its own step."""
        lab = ql_voice.get()
        if lab == QL_SCRATCH:
            say("pick a saved voice (not scratch) to store an accent", warn=True); return
        v = None
        for cand in dlg.list_voices():
            if cand.get("name") == lab:
                v = cand; break
        if v is None:
            say("voice %r not found - press \u21bb" % lab, warn=True); return
        if v.get("source") == "character":
            say("character-pack voices are read-only - CAST \u2192 NEW VOICE to fork one",
                warn=True); return
        v["accent"] = dict(dlg.ACCENTS).get(ql_accent.get(), "")
        try:
            dlg.save_voice(v)
        except Exception as exc:
            say("could not save: %r" % exc, warn=True); return
        say("accent saved for %s" % v.get("name"))

    _ql_refill()


    # Two columns (redesign): the SCRIPT on the left, CAST + LINES on the right, and MAKE as a
    # bar pinned under the right column so it is always on screen.
    cols = tk.Frame(body, bg=C["bg"])
    cols.pack(fill="both", expand=True)
    scol = tk.Frame(cols, bg=C["panel"], width=420, highlightthickness=1,
                    highlightbackground=C["line"])
    scol.pack(side="left", fill="y")
    scol.pack_propagate(False)
    rcol = tk.Frame(cols, bg=C["bg"])
    rcol.pack(side="left", fill="both", expand=True, padx=(10, 0))
    makebar = tk.Frame(rcol, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    makebar.pack(side="bottom", fill="x", pady=(8, 0))
    lp = tk.Frame(rcol, bg=C["bg"])
    lp.pack(side="top", fill="both", expand=True)
    vsb = tk.Scrollbar(lp, orient="vertical")
    vsb.pack(side="right", fill="y")
    canvas = tk.Canvas(lp, bg=C["bg"], highlightthickness=0,
                       yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    vsb.configure(command=canvas.yview)
    steps = tk.Frame(canvas, bg=C["bg"])
    cwin = canvas.create_window((0, 0), window=steps, anchor="nw")
    steps.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>",
                lambda e: canvas.itemconfigure(cwin, width=e.width))

    # Scoped wheel
    def _on_wheel(ev):
        try:
            d = int(getattr(ev, "delta", 0) or 0)
        except Exception:
            d = 0
        if not d:
            return "break"
        try:
            canvas.yview_scroll(-1 if d > 0 else 1, "units")
        except Exception:
            pass
        return "break"
    win.bind("<MouseWheel>", lambda e: _on_wheel(e))
    win.bind("<Button-4>", lambda e: _on_wheel(e))
    win.bind("<Button-5>", lambda e: _on_wheel(e))

    # ------------------------------------------------------------------ step scaffolding
    def step_frame(n, title, parent=None, hint=""):
        """A titled section. parent=None -> the scrolling right column; a parent (the script
        column) -> fills it. The numbered chips went: left-to-right IS the order now."""
        if parent is None:
            outer = tk.Frame(steps, bg=C["panel"], highlightthickness=1,
                             highlightbackground=C["line"])
            outer.pack(fill="x", pady=(0, 8))
        else:
            outer = parent
        head = tk.Frame(outer, bg=C["panel"])
        head.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(head, text=title, bg=C["panel"], fg=C["ink"], font=fh).pack(side="left")
        if hint:
            tk.Label(head, text=hint, bg=C["panel"], fg=C["faint"],
                     font=fsm).pack(side="left", padx=8)
        b = tk.Frame(outer, bg=C["panel"])
        b.pack(fill="both", expand=parent is not None, padx=10, pady=(2, 10))
        return b

    # ===== 1 · SCRIPT ===================================================================
    b1 = step_frame(1, "Script", parent=scol, hint="lines update as you type")
    srow2 = tk.Frame(b1, bg=C["panel"])
    srow2.pack(side="bottom", fill="x", pady=(6, 0))
    lam(srow2, "Project").pack(side="left")
    proj_var = tk.StringVar(value="\u2014 new \u2014")
    proj_picker = tk.OptionMenu(srow2, proj_var, "\u2014 new \u2014")
    proj_picker.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                          highlightthickness=0, activebackground=C["rust"],
                          activeforeground="#fff", borderwidth=0, width=14,
                          anchor="w")
    proj_picker["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    proj_picker.pack(side="left", padx=6)
    btn(srow2, "New",
        lambda: _new_project()).pack(side="left", padx=4)
    saved_lbl = lam(srow2, "", fg=C["faint"])
    saved_lbl.pack(side="left", padx=6)
    srow1 = tk.Frame(b1, bg=C["panel"])
    srow1.pack(side="bottom", fill="x", pady=(6, 0))
    btn(srow1, "Write it for me\u2026",
        lambda: _write_dialogue()).pack(side="left")
    btn(srow1, "Open file\u2026",
        lambda: _open_script()).pack(side="left", padx=6)
    lint_lbl = lam(b1, "", fg=C["faint"], font=fsm, wraplength=380, justify="left",
                   anchor="w")
    lint_lbl.pack(side="bottom", fill="x", pady=(4, 0))
    t_script = tk.Text(b1, height=11, width=40, wrap="word",
                       bg="#100e0c", fg=C["ink"], relief="flat", font=fmono,
                       padx=6, pady=4, insertbackground=C["ink"])
    t_script.pack(fill="both", expand=True)
    _ap = {"job": None}

    def _script_typed(_e=None):
        """Re-parse ~0.8 s after typing stops. Never while a render is running: a line that
        changes under a running job would get the take meant for its old words."""
        if _ap["job"]:
            try:
                win.after_cancel(_ap["job"])
            except Exception:
                pass
        _ap["job"] = win.after(800, _auto_parse)

    def _auto_parse():
        _ap["job"] = None
        text = t_script.get("1.0", "end").strip()
        if not text or (st["pj"] is not None and text == (st["pj"].get("script") or "").strip()):
            return
        if st.get("busy"):
            _ap["job"] = win.after(1500, _auto_parse)
            return
        _parse()
    def _script_modified(_e=None):
        # <<Modified>> fires for typing, paste, drag-drop and programmatic inserts alike
        # (a <KeyRelease> binding misses all but the first); re-arm it every time.
        if t_script.edit_modified():
            t_script.edit_modified(False)
            _script_typed()
    t_script.bind("<<Modified>>", _script_modified)

    def _autosave_project():
        """Every change saves the project — no SAVE button to forget."""
        if st["pj"] is None or st["pj"].get("_skip_save"):
            return
        try:
            st["pj"]["script"] = t_script.get("1.0", "end").strip()
            st["pj"]["gap_ms"] = int(gap_var.get())
            dlg.save_project(st["pj"])
        except Exception as exc:
            say("could not save the project: %s" % str(exc)[:80], warn=True)
            return
        _rebuild_project_list()
        proj_var.set(st["pj"]["slug"])
        saved_lbl.configure(text="saved %s" % time.strftime("%H:%M"))

    def _rebuild_project_list():
        projs = dlg.list_projects()
        m = proj_picker["menu"]
        m.delete(0, "end")
        m.add_command(label="\u2014 new \u2014",
                      command=lambda: proj_var.set("\u2014 new \u2014"))
        for p in projs:
            m.add_command(label=p,
                          command=lambda v=p: _load_project(v))
    _rebuild_project_list()

    def _write_cast():
        """NAME -> one-line bio for the LLM: the speakers already in the box, else the bank.
        A bio is the voice's own notes + accent — the words Ryan typed for that voice."""
        names = dlg.speakers(dlg.parse_script(t_script.get("1.0", "end")))
        bank = {v["slug"]: v for v in dlg.list_voices() if v.get("source") != "character"}
        if not names:
            names = [v["name"].upper() for v in bank.values()]
        cast = {}
        for n in names:
            v = next((v for v in bank.values()
                      if n in (v.get("characters") or []) or v["name"].upper() == n), None)
            bio = ""
            if v is not None:
                bio = ", ".join(x for x in (v.get("notes", ""), (v.get("accent") or "")
                                            and v["accent"] + " accent") if x)
            cast[n] = bio or "a character"
        return cast

    def write_apply(out):
        """Put a Director.dialogue() result in the box (house format) and PARSE it. Exposed
        for the driver, so the fill path is tested without the network."""
        import director as _dmod
        text = _dmod.format_dialogue(out.get("lines") or [])
        t_script.delete("1.0", "end")
        t_script.insert("1.0", text + "\n")
        _parse()
        say("WROTE \u201c%s\u201d \u00b7 %d lines \u2014 edit freely, then render"
            % (out.get("title", "untitled"), len(out.get("lines") or [])))

    def _write_dialogue():
        """Ask the rig's local model for a scene. ⚠️ Worker thread + win.after poller — a 27B
        takes ~10-30 s and a synchronous call would freeze the whole app."""
        if st.get("busy"):
            say("busy \u2014 wait for the current job", warn=True)
            return
        cast = _write_cast()
        if not cast:
            say("make at least one voice (step 2) or paste speaker names first", warn=True)
            return
        brief = _sd.askstring("Write dialogue",
                              "What happens in the scene?  (cast: %s)" % ", ".join(cast),
                              parent=win)
        if not brief:
            return
        secs = _sd.askinteger("Write dialogue", "About how many seconds of talk?",
                              parent=win, initialvalue=30, minvalue=5, maxvalue=300) or 30
        if _driver and not _go:
            say("driver: WRITE DIALOGUE does not call the model", warn=True)
            return
        job = {"done": False, "out": None, "err": None}

        def _work():
            try:
                import director as _dmod
                job["out"] = _dmod.Director().dialogue(brief, cast, seconds=secs)
            except Exception as exc:
                job["err"] = str(exc)
            finally:
                job["done"] = True

        def _poll():
            if not job["done"]:
                win.after(400, _poll)
                return
            st["busy"] = None
            if job["err"]:
                say("WRITE failed: %s" % job["err"][:120], warn=True)
            else:
                write_apply(job["out"])

        st["busy"] = job
        say("writing with the rig's local model\u2026 (~10-30 s; it unloads before any voice work)")
        threading.Thread(target=_work, daemon=True).start()
        win.after(400, _poll)

    win.dialogue_write_apply = lambda out: write_apply(out)

    def _open_script():
        path = _fd.askopenfilename(
            title="Open script",
            filetypes=[("Scripts", "*.md *.txt *.fountain"), ("All", "*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                t_script.delete("1.0", "end")
                t_script.insert("1.0", fh.read())
            say("loaded %s" % os.path.basename(path))
            _parse()
        except Exception as exc:
            say("could not read: %r" % exc, warn=True)

    def _parse():
        text = t_script.get("1.0", "end").strip()
        if not text:
            say("paste or open a script first", warn=True)
            return
        lines = dlg.parse_script(text)
        spkrs = dlg.speakers(lines)
        say("%d lines \u00b7 %d speakers: %s"
            % (len(lines), len(spkrs), " \u00b7 ".join(spkrs)))
        if st["pj"] is None:
            # ⚠️ a UNIQUE name, never "untitled": saving is automatic now, and a shared default
            # name is exactly how one script silently overwrote another.
            st["pj"] = dlg.new_project(
                proj_var.get() if proj_var.get() != "\u2014 new \u2014"
                else time.strftime("scene %b %d %H%M%S").lower(), text)
        else:
            st["pj"] = dlg.reparse(st["pj"], text)
        _rebuild_project_list()
        # Lint warnings
        warnings = []
        for l in lines:
            ws = dlg.lint_line(l["text"])
            if ws:
                warnings.append("Line %d: %s" % (l["idx"], "; ".join(ws)))
        found = "%d line%s \u00b7 %d speaker%s found" % (
            len(lines), "" if len(lines) == 1 else "s", len(spkrs), "" if len(spkrs) == 1 else "s")
        if warnings:
            lint_lbl.configure(text=found + "  \u00b7  check: " + " | ".join(warnings[:2]),
                               fg=C["ochre"])
        else:
            lint_lbl.configure(text=found, fg=C["moss"])
        _refresh_cast()
        if any(v.get() == "\u2014 none \u2014" for v in st["cast_vars"].values()):
            _auto_cast(quiet=True)          # new speakers get a voice straight away if one fits
        _refresh_lines()
        _sync_engine()
        _refresh_estimate()
        _autosave_project()

    def _new_project():
        # no pop-up: a new project is named by the clock; the script names itself in the menu
        name = "test" if _driver else time.strftime("scene %b %d %H%M%S").lower()
        st["pj"] = {
            "name": name,
            "slug": dlg.slugify(name),
            "script": "",
            "cast": {},
            "gap_ms": 350,
            "lines": [],
        }
        proj_var.set(name)
        t_script.delete("1.0", "end")
        lint_lbl.configure(text="", fg=C["faint"])
        _refresh_cast()
        _refresh_lines()
        _sync_engine()
        say("new project: %s" % name)

    def _save_project():
        if st["pj"] is None:
            say("no project to save", warn=True)
            return
        st["pj"]["script"] = t_script.get("1.0", "end").strip()
        if st["pj"].get("_skip_save"):
            return
        dlg.save_project(st["pj"])
        _rebuild_project_list()
        proj_var.set(st["pj"]["slug"])
        say("saved %s" % st["pj"]["slug"])

    def _load_project(slug):
        p = dlg.load_project(slug)
        if p is None:
            say("project %s not found" % slug, warn=True)
            return
        st["pj"] = p
        t_script.delete("1.0", "end")
        t_script.insert("1.0", p.get("script", ""))
        proj_var.set(slug)
        try:
            gap_var.set(int(p.get("gap_ms", 350)))     # GAP is saved with the project now
        except Exception:
            pass
        _refresh_cast()
        _refresh_lines()
        _sync_engine()
        say("loaded %s (%d lines)" % (slug, len(p.get("lines", []))))

    # ===== 2 · CAST =====================================================================
    b2 = step_frame(2, "Cast", hint="a voice for each speaker")
    cast_frame = tk.Frame(b2, bg=C["panel"])
    cast_frame.pack(fill="x")

    def _rebuild_voice_list():
        voices = dlg.list_voices()
        items = [("\u2014 none \u2014", "\u2014 none \u2014")]
        for v in voices:
            slug = v.get("slug", "")
            label = "%s  (%s)" % (v.get("name", slug), v.get("source", "bank"))
            items.append((slug, label))
        return items

    def _refresh_cast():
        for w in list(st["cast_rows"].values()):
            w.destroy()
        st["cast_rows"].clear()
        st["cast_vars"].clear()
        if st["pj"] is None:
            return
        spkrs = dlg.speakers(st["pj"].get("lines", []))
        if not spkrs:
            return
        cast = st["pj"].get("cast", {})
        voice_items = _rebuild_voice_list()
        for spk in spkrs:
            row = tk.Frame(cast_frame, bg=C["panel"])
            row.pack(fill="x", pady=2)
            lam(row, spk, fg=C["ink"], font=fh).pack(side="left", padx=(0, 8))
            # ⚠️ The var holds a SLUG — plan_render() and EDIT look voices up by slug. A project
            # saved by the old code may hold a display LABEL ("Avery  (bank)"): map it back.
            _lab2slug = {l: s for s, l in voice_items}
            _val = cast.get(spk, "\u2014 none \u2014")
            var = tk.StringVar(value=_lab2slug.get(_val, _val))
            st["cast_vars"][spk] = var
            if _lab2slug.get(_val, _val) != _val:
                cast[spk] = _lab2slug[_val]
            menu = tk.OptionMenu(row, var,
                                 *[l for _, l in voice_items],
                                 command=lambda *a, s=spk: _cast_changed(s))
            menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat",
                           font=fsm, highlightthickness=0,
                           activebackground=C["rust"], activeforeground="#fff",
                           borderwidth=0, width=16, anchor="w")
            menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
            # Each entry SHOWS the label but SETS the slug. tk.OptionMenu would set the label,
            # which is how a hand-picked voice rendered as "no voice cast" (bug fixed 2026-10-17).
            _mm = menu["menu"]
            _mm.delete(0, "end")
            for _slug, _lab in voice_items:
                _mm.add_command(label=_lab, command=lambda v_=var, s_=_slug, sp=spk: (
                    v_.set(s_), _cast_changed(sp)))
            st["cast_menus"][spk] = menu
            menu.pack(side="left", padx=4)
            btn(row, "Design new",
                lambda s=spk: _new_voice_for(s)).pack(side="left", padx=2)
            btn(row, "Edit",
                lambda s=spk: _edit_voice(
                    st["cast_vars"].get(s, tk.StringVar()).get())
                ).pack(side="left", padx=2)
            _need = lam(row, "needs a voice \u2014 pick one or Design new", fg=C["ochre"])

            def _need_paint(*_a, v_=var, l_=_need):
                if v_.get() == "\u2014 none \u2014":
                    l_.pack(side="left", padx=8)
                else:
                    l_.pack_forget()
            var.trace_add("write", _need_paint)
            _need_paint()
            st["cast_rows"][spk] = row

    def _cast_changed(speaker):
        if st["pj"] is not None:
            st["pj"]["cast"] = {s: v.get() for s, v in st["cast_vars"].items()}
            _autosave_project()

    def _new_voice_for(speaker):
        name = speaker.title() if speaker.isupper() else speaker
        v = dlg.new_voice(name)
        v["characters"] = [speaker]
        st["editing"] = v
        _open_editor_window()

    def _edit_voice(slug):
        if not slug or slug == "\u2014 none \u2014":
            say("pick a voice first", warn=True)
            return
        for v in dlg.list_voices():
            if v.get("slug") == slug:
                st["editing"] = v
                _open_editor_window()
                return
        say("voice %s not found" % slug, warn=True)

    cast_ctl = tk.Frame(b2, bg=C["panel"])
    cast_ctl.pack(anchor="w", pady=(6, 0))
    btn(cast_ctl, "Auto-cast", lambda: _auto_cast()).pack(side="left")

    def _auto_cast(quiet=False):
        if st["pj"] is None:
            if not quiet:
                say("type or paste a script first", warn=True)
            return
        spkrs = dlg.speakers(st["pj"].get("lines", []))
        all_v = dlg.list_voices()
        slugs = {v.get("slug"): v for v in all_v}
        auto = dlg.auto_cast(spkrs, slugs, st["pj"].get("cast"))
        for spk, slug in auto.items():
            if spk in st["cast_vars"]:
                st["cast_vars"][spk].set(slug or "\u2014 none \u2014")
        if st["pj"] is not None:
            st["pj"]["cast"] = {s: v.get() for s, v in st["cast_vars"].items()}
        if not quiet:
            say("auto-cast: %d speakers mapped" % len(auto))
        _refresh_estimate()
        _autosave_project()

    # ===== 3 · VOICE editor =============================================================
    _ew = {"win": None}      # editor window handle

    def _open_editor_window():
        v = st.get("editing")
        if v is None:
            say("no voice selected", warn=True)
            return
        # Close existing editor if open
        if _ew.get("win") is not None:
            try:
                _ew["win"].destroy()
            except Exception:
                pass
        ewt = tk.Toplevel(win)
        _ew["win"] = ewt
        ewt.title("Voice: %s" % v.get("name", ""))
        ewt.configure(bg=C["bg"])
        ewt.transient(win)
        # ⚠️ The editor's content (~1050 px) is taller than many screens: everything is built in
        # a SCROLLING inner frame `ew`, so SAVE VOICE at the bottom can always be reached.
        _ecv = tk.Canvas(ewt, bg=C["bg"], highlightthickness=0)
        _esb = tk.Scrollbar(ewt, orient="vertical", command=_ecv.yview)
        _ecv.configure(yscrollcommand=_esb.set)
        _esb.pack(side="right", fill="y")
        _ecv.pack(side="left", fill="both", expand=True)
        ew = tk.Frame(_ecv, bg=C["bg"])
        _ecv.create_window((0, 0), window=ew, anchor="nw")
        ew.bind("<Configure>", lambda e: _ecv.configure(scrollregion=_ecv.bbox("all")))
        _ecv.bind("<MouseWheel>", lambda e: _ecv.yview_scroll(int(-e.delta / 30) or
                                                               (-1 if e.delta > 0 else 1), "units"))
        st["editor_canvas"] = _ecv
        # ⚠️ Sized from the SCREEN and resizable: the accent menu + SINGING row made the old
        # fixed 520x720 clip its own controls (a clipped control is invisible, not an error).
        _eh = min(860, sh - 80)
        ewt.geometry("800x%d+%d+%d" % (_eh, max(0, sw // 2 - 400), 30))
        ewt.resizable(True, True)

        def _v_lbl(text, **kw):
            kw.setdefault("bg", C["bg"])
            kw.setdefault("fg", C["faint"])
            return tk.Label(ew, text=text, font=kw.pop("font", fsm), **kw)

        def _v_btn(text, cmd, **kw):
            kw.setdefault("bg", C["panel2"])
            kw.setdefault("fg", C["ink"])
            return tk.Button(ew, text=text,
                             activebackground=C["rust"],
                             activeforeground="#fff", relief="flat",
                             font=fsm, padx=9, pady=2, borderwidth=0,
                             highlightthickness=1,
                             highlightbackground=C["line"],
                             command=cmd, **kw)

        # Basic info
        f1 = tk.Frame(ew, bg=C["bg"])
        f1.pack(fill="x", padx=10, pady=(8, 2))
        _v_lbl("NAME").pack(side="left")
        name_var = tk.StringVar(value=v.get("name", ""))
        tk.Entry(f1, textvariable=name_var, width=20, bg="#100e0c",
                 fg=C["ink"], relief="flat", font=fsm,
                 insertbackground=C["ink"]).pack(side="left", padx=6)

        f2 = tk.Frame(ew, bg=C["bg"])
        f2.pack(fill="x", padx=10, pady=2)
        _v_lbl("GENDER").pack(side="left")
        gender_var = tk.StringVar(value=v.get("gender", "male"))
        gmenu = tk.OptionMenu(f2, gender_var, "male", "female")
        gmenu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust"],
                        activeforeground="#fff", borderwidth=0, width=8)
        gmenu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
        gmenu.pack(side="left", padx=6)
        _v_lbl("AGE").pack(side="left", padx=(10, 0))
        age_var = tk.IntVar(value=v.get("age_years", 20))
        tk.Scale(f2, from_=8, to=90, resolution=1, orient="horizontal",
                 variable=age_var, length=120, bg=C["panel"], fg=C["ink"],
                 troughcolor=C["panel2"], highlightthickness=0,
                 font=fsm, activebackground=C["rust"], showvalue=True
                 ).pack(side="left", padx=4)

        f3 = tk.Frame(ew, bg=C["bg"])
        f3.pack(fill="x", padx=10, pady=2)
        _v_lbl("ACCENT").pack(side="left")
        accent_var = tk.StringVar(value=v.get("accent", ""))
        accent_pick = tk.StringVar(value=dlg.accent_label(v.get("accent", "")))

        def _pick_accent(label):
            accent_pick.set(label)
            words = dict(dlg.ACCENTS).get(label)
            if words is not None:
                accent_var.set(words)        # fills the box; the box stays editable
            _refresh_desc(name_var, gender_var, age_var, accent_var,
                          notes_var, slider_vars, desc_text)

        am = tk.OptionMenu(f3, accent_pick, *[a for a, _w in dlg.ACCENTS], command=_pick_accent)
        am.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                     highlightthickness=0, activebackground=C["rust"],
                     activeforeground="#fff", borderwidth=0, width=22, anchor="w")
        am["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
        am.pack(side="left", padx=6)
        tk.Entry(f3, textvariable=accent_var, width=30, bg="#100e0c",
                 fg=C["ink"], relief="flat", font=fsm,
                 insertbackground=C["ink"]).pack(side="left", padx=6)
        def _accent_typed(*_a):
            # typing in the box keeps the menu honest ("custom") and the preview live.
            # desc_text is built further down; a trace only fires after the editor exists.
            accent_pick.set(dlg.accent_label(accent_var.get()))
            _refresh_desc(name_var, gender_var, age_var, accent_var,
                          notes_var, slider_vars, desc_text)
        accent_var.trace_add("write", _accent_typed)
        st["editor_accent"] = (accent_var, accent_pick, _pick_accent)

        f3b = tk.Frame(ew, bg=C["bg"])
        f3b.pack(fill="x", padx=10, pady=2)
        _v_lbl("SINGING").pack(side="left")
        singing_var = tk.StringVar(value=v.get("singing", ""))
        tk.Entry(f3b, textvariable=singing_var, width=46, bg="#100e0c",
                 fg=C["ink"], relief="flat", font=fsm,
                 insertbackground=C["ink"]).pack(side="left", padx=6)
        _v_lbl("for \u266a sung lines (YuE2) \u2014 e.g. warm baritone folk singer",
               fg=C["faint"]).pack(in_=f3b, side="left")
        st["editor_singing"] = singing_var

        f4 = tk.Frame(ew, bg=C["bg"])
        f4.pack(fill="x", padx=10, pady=2)
        _v_lbl("NOTES").pack(side="left")
        notes_var = tk.StringVar(value=v.get("notes", ""))
        tk.Entry(f4, textvariable=notes_var, width=40, bg="#100e0c",
                 fg=C["ink"], relief="flat", font=fsm,
                 insertbackground=C["ink"]).pack(side="left", padx=6)

        # SLIDERS
        _v_lbl("SLIDERS", font=fh).pack(anchor="w", padx=10, pady=(8, 0))
        sv = v.get("sliders", {})
        slider_vars = {}
        for sk in dlg.SLIDERS:
            sr = tk.Frame(ew, bg=C["bg"])
            sr.pack(fill="x", padx=10, pady=1)
            _v_lbl(sk.capitalize(), width=8).pack(side="left")
            var = tk.IntVar(value=sv.get(sk, 0))
            slider_vars[sk] = var
            tk.Scale(sr, from_=-2, to=2, resolution=1, orient="horizontal",
                     variable=var, length=180, bg=C["panel"], fg=C["ink"],
                     troughcolor=C["panel2"], highlightthickness=0,
                     font=fsm, activebackground=C["rust"],
                     tickinterval=1, showvalue=True,
                     command=lambda *a: _refresh_desc(
                         name_var, gender_var, age_var, accent_var,
                         notes_var, slider_vars, desc_text)
                     ).pack(side="left", padx=4)

        # Description preview
        _v_lbl("WHAT THE MODEL READS", fg=C["moss"], font=fh).pack(
            anchor="w", padx=10, pady=(8, 0))
        desc_text = tk.Text(ew, height=4, width=60, wrap="word",
                            bg="#100e0c", fg=C["ink"], relief="flat",
                            font=fsm, padx=4, pady=2, state="disabled")
        desc_text.pack(anchor="w", padx=10)
        st["editor_desc"] = desc_text

        def _refresh_desc(nv, gv, av, acv, ntv, svars, dt):
            tmp = {
                "name": nv.get().strip() or v.get("name", ""),
                "gender": gv.get(),
                "age_years": av.get(),
                "accent": acv.get().strip(),
                "notes": ntv.get().strip(),
                "sliders": {k: svars[k].get() for k in dlg.SLIDERS},
                "fx": v.get("fx", {}),
                "moods": v.get("moods", {}),
            }
            desc = dlg.compose_description(tmp, "neutral")
            dt.configure(state="normal")
            dt.delete("1.0", "end")
            dt.insert("1.0", desc)
            dt.configure(state="disabled")

        # Initial description
        _refresh_desc(name_var, gender_var, age_var, accent_var,
                      notes_var, slider_vars, desc_text)

        # Moods
        _v_lbl("MOODS", font=fh).pack(anchor="w", padx=10, pady=(8, 0))
        moods_canvas = tk.Canvas(ew, bg=C["bg"], highlightthickness=0,
                                 height=120)
        moods_canvas.pack(fill="x", padx=10)
        moods_vsb = tk.Scrollbar(ew, orient="vertical",
                                 command=moods_canvas.yview)
        moods_vsb.pack(side="right", fill="y")
        moods_canvas.configure(yscrollcommand=moods_vsb.set)
        moods_inner = tk.Frame(moods_canvas, bg=C["bg"])
        moods_canvas.create_window((0, 0), window=moods_inner, anchor="nw")
        moods_inner.bind("<Configure>",
                         lambda e: moods_canvas.configure(
                             scrollregion=moods_canvas.bbox("all")))

        def _render_moods():
            for w in moods_inner.winfo_children():
                w.destroy()
            moods = v.get("moods", {})
            if not moods:
                _v_lbl("(neutral only \u2014 use ADD MOOD below)").pack(
                    in_=moods_inner, anchor="w")
            for mk in sorted(moods.keys()):
                mr = tk.Frame(moods_inner, bg=C["bg"])
                mr.pack(fill="x", pady=1)
                extra = moods[mk].get("extra", "")
                wav = moods[mk].get("wav", "")
                has_wav = bool(wav) and os.path.join(
                    dlg.voices_dir(), v.get("slug", ""), wav) if wav else False
                stat = "designed" if has_wav else "\u2014"
                _v_lbl("\u00b7 %s:  %s [%s]" % (mk, extra or "(no extra)",
                                                  stat)).pack(
                    side="left", in_=mr)
                _v_btn("AUDITION",
                       lambda mood=mk: _audition_voice(mood)).pack(
                    side="left", padx=4, in_=mr)
                if mk != "neutral":
                    _v_btn("X",
                           lambda mood=mk: _del_mood(mood)).pack(
                        side="left", padx=2, in_=mr)

        _render_moods()
        btn_row = tk.Frame(ew, bg=C["bg"])
        btn_row.pack(fill="x", padx=10, pady=2)
        _v_btn("ADD MOOD", lambda: _add_mood()).pack(in_=btn_row, side="left")

        def _add_mood():
            avail = [m for m in dlg.MOOD_WORDS if m not in v.get("moods", {})]
            if not avail:
                say("all moods added already", warn=True)
                return
            nxt = avail[0] if _driver else _sd.askstring(
                "Add Mood", "Mood name:\n%s" % ", ".join(avail),
                initialvalue=avail[0])
            if not nxt or nxt not in dlg.MOOD_WORDS:
                say("mood must be one of: %s" % ", ".join(avail), warn=True)
                return
            v.setdefault("moods", {})[nxt] = {
                "extra": dlg.MOOD_WORDS.get(nxt, ""),
                "wav": "",
                "ref_text": dlg.DEFAULT_SAMPLE,
            }
            _render_moods()

        def _del_mood(mood):
            if mood == "neutral":
                return
            v.get("moods", {}).pop(mood, None)
            _render_moods()

        # FX
        _v_lbl("FX  (apply at assembly/preview, no re-render)",
               fg=C["faint"]).pack(anchor="w", padx=10, pady=(8, 0))
        fx = v.get("fx", {"semitones": 0.0, "tempo": 1.0, "room": 0.0})
        fx_vars = {}
        for fk, fmin, fmax, fres in (
                ("semitones", -4.0, 4.0, 0.5),
                ("tempo", 0.85, 1.15, 0.01),
                ("room", 0.0, 1.0, 0.05),
        ):
            fr = tk.Frame(ew, bg=C["bg"])
            fr.pack(fill="x", padx=10, pady=1)
            _v_lbl(fk.capitalize(), width=12).pack(side="left")
            var = tk.DoubleVar(value=fx.get(fk, 0.0))
            fx_vars[fk] = var
            tk.Scale(fr, from_=fmin, to=fmax, resolution=fres,
                     orient="horizontal", variable=var, length=140,
                     bg=C["panel"], fg=C["ink"], troughcolor=C["panel2"],
                     highlightthickness=0, font=fsm,
                     activebackground=C["rust"], showvalue=True
                     ).pack(side="left", padx=4)

        # Buttons
        eb = tk.Frame(ew, bg=C["bg"])
        eb.pack(fill="x", padx=10, pady=(10, 4))
        _v_btn("AUDITION",
               lambda: _audition_voice("neutral")).pack(in_=eb, side="left")
        _v_btn("PLAY WITH FX",
               lambda: _play_voice_fx(fx_vars)).pack(in_=eb, side="left",
                                                     padx=6)
        _v_btn("SAVE VOICE",
               lambda: _save_voice_editor(
                   ewt, name_var, gender_var, age_var, accent_var,
                   notes_var, slider_vars, fx_vars)).pack(in_=eb, side="left",
                                                          padx=6)
        _v_btn("CANCEL", ewt.destroy).pack(in_=eb, side="right", padx=6)

        if btn_colors:
            try:
                btn_colors(ewt)
            except Exception:
                pass

    def _build_from_editor_vars(name_var, gender_var, age_var, accent_var,
                                notes_var, slider_vars, fx_vars):
        """Return a voice dict from editor widget state."""
        v = st.get("editing") or {}
        slug = dlg.slugify(name_var.get().strip() or v.get("name", "untitled"))
        return {
            "name": name_var.get().strip() or v.get("name", ""),
            "slug": slug,
            "gender": gender_var.get(),
            "age_years": age_var.get(),
            "accent": accent_var.get().strip(),
            "notes": notes_var.get().strip(),
            "singing": (st.get("editor_singing").get().strip()
                        if st.get("editor_singing") else v.get("singing", "")),
            "sliders": {k: slider_vars[k].get() for k in dlg.SLIDERS},
            "fx": {k: fx_vars[k].get() for k in fx_vars},
            "moods": v.get("moods", {}),
            "characters": v.get("characters", []),
            "source": "bank",
        }

    def _save_voice_editor(ew, name_var, gender_var, age_var, accent_var,
                           notes_var, slider_vars, fx_vars):
        try:
            v = _build_from_editor_vars(name_var, gender_var, age_var,
                                        accent_var, notes_var, slider_vars,
                                        fx_vars)
            dlg.save_voice(v)
            st["editing"] = dlg.load_voice(v["slug"])
            say("voice saved: %s" % v["name"])
            _refresh_cast()
            ew.destroy()
        except ValueError as exc:
            say(str(exc), warn=True)
        except Exception as exc:
            say("could not save: %r" % exc, warn=True)

    def _audition_voice(mood):
        if _driver and not _go:
            say("AUDITION skipped (driver mode)")
            return
        v = st.get("editing")
        if v is None:
            say("no voice selected", warn=True)
            return
        _start_tts_job("audition", v, mood)

    def _play_voice_fx(fx_vars):
        """Preview the voice's neutral wav with current FX applied."""
        v = st.get("editing")
        if v is None:
            say("no voice selected", warn=True)
            return
        wav = dlg.voice_wav(v, "neutral")
        if not wav or not os.path.exists(wav):
            say("audition the voice first", warn=True)
            return
        fx = {k: fx_vars[k].get() for k in fx_vars}
        try:
            tmp = os.path.join(tempfile.mkdtemp(prefix="forge-dlg-"),
                               "fx_preview.wav")
            fx_str = dlg.fx_filter(fx)
            if fx_str:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", wav, "-af", fx_str, tmp],
                    capture_output=True, timeout=120, check=True)
            else:
                shutil.copy2(wav, tmp)
            play_audio(tmp)
        except Exception as exc:
            say("FX preview failed: %r" % exc, warn=True)

    # ===== 3 · LINES ====================================================================
    b4 = step_frame(3, "Lines", hint="\u25b6 to listen \u00b7 Redo for another take")
    lf_vsb = tk.Scrollbar(b4, orient="vertical")
    lf_vsb.pack(side="right", fill="y")
    lf_canvas = tk.Canvas(b4, bg=C["panel"], highlightthickness=0,
                           yscrollcommand=lf_vsb.set, height=340)
    lf_canvas.pack(fill="both", expand=True)
    lf_vsb.configure(command=lf_canvas.yview)
    # ⚠️ a CHILD of the canvas. It used to be a sibling created BEFORE the canvas, so the
    # canvas stacked above it and hid every line — the table looked empty after PARSE.
    lf_inner = tk.Frame(lf_canvas, bg=C["panel"])
    lf_canvas.create_window((0, 0), window=lf_inner, anchor="nw")
    lf_inner.bind("<Configure>",
                  lambda e: lf_canvas.configure(
                      scrollregion=lf_canvas.bbox("all")))
    lf_canvas.bind("<Configure>",
                   lambda e: lf_canvas.itemconfigure(
                       lf_canvas.find_withtag("all")[0]
                       if lf_canvas.find_withtag("all") else "",
                       width=e.width))

    def _refresh_lines():
        for w in lf_inner.winfo_children():
            w.destroy()
        if st["pj"] is None:
            return
        pj_lines = st["pj"].get("lines", [])
        if not pj_lines:
            return
        # Header
        hdr = tk.Frame(lf_inner, bg=C["panel"])
        hdr.pack(fill="x", pady=1)
        for h, w in (("#", 2), ("WHO", 10), ("LINE", 34), ("MOOD", 8),
                     ("TAKES", 5), ("SCORE", 5)):
            tk.Label(hdr, text=h, bg=C["panel"], fg=C["faint"],
                     font=fsm, width=w, anchor="w").pack(side="left", padx=2)
        for li in pj_lines:
            lr = tk.Frame(lf_inner, bg=C["panel"])
            lr.pack(fill="x", pady=1)
            tk.Label(lr, text=str(li["idx"] + 1), bg=C["panel"], fg=C["muted"],
                     font=fsm, width=2).pack(side="left", padx=2)
            tk.Label(lr, text=dlg.normalise_speaker(li["speaker"]),
                     bg=C["panel"], fg=C["ink"], font=fsm, width=10,
                     anchor="w").pack(side="left", padx=2)
            # the WHOLE line (it was cut at 36 characters), plus its error if the last try failed
            _tx = li["text"] + (("\n\u26a0 " + str(li["error"])[:120]) if li.get("error") else "")
            txt_lbl = tk.Label(lr, text=_tx, bg=C["panel"],
                               fg=(RED if li.get("error") else C["ink"]), font=fsm, anchor="w",
                               justify="left", wraplength=300, width=34)
            txt_lbl.pack(side="left", padx=2)
            txt_lbl.bind("<Button-1>",
                         lambda e, idx=li["idx"]: _edit_line_text(idx))

            # Mood
            avail_moods = list(dlg.MOOD_WORDS) + [SUNG_CHOICE]
            dir_mood = dlg.mood_of(li.get("direction", ""), list(dlg.MOOD_WORDS))
            mvar = tk.StringVar(value=SUNG_CHOICE if dlg.is_sung(li)
                                else (li.get("mood") or dir_mood))
            mm = tk.OptionMenu(lr, mvar, *avail_moods)
            mm.configure(bg=C["panel2"], fg=C["ink"], relief="flat",
                         font=fsm, highlightthickness=0,
                         activebackground=C["rust"], activeforeground="#fff",
                         borderwidth=0, width=8, anchor="w")
            mm["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
            mm.pack(side="left", padx=2)
            mvar.trace_add("write", lambda *a, idx=li["idx"], v=mvar:
                           _line_mood_changed(idx, v))

            takes = li.get("takes", [])
            kept = li.get("kept", 0)
            tk.Label(lr, text=str(len(takes)), bg=C["panel"], fg=C["faint"],
                     font=fsm, width=5, anchor="e").pack(side="left", padx=2)
            # Score
            score_val = ""
            score_fg = C["moss"]
            if takes:
                tk_item = takes[min(kept, len(takes) - 1)]
                s = tk_item.get("score", 0)
                if s:
                    score_val = "%.2f" % s
                    score_fg = RED if s < dlg.PASS_SCORE else C["moss"]
            tk.Label(lr, text=score_val, bg=C["panel"], fg=score_fg,
                     font=fsm, width=5, anchor="e").pack(side="left", padx=2)

            btn(lr, "\u25b6",
                lambda idx=li["idx"]: _play_line(idx)).pack(side="left",
                                                            padx=2)
            btn(lr, "Redo",
                lambda idx=li["idx"]: _redo_line(idx)).pack(side="left",
                                                            padx=2)
            if len(takes) > 1:
                tp = tk.Frame(lr, bg=C["panel"])
                tp.pack(side="left", padx=2)
                btn(tp, "\u25c0",
                    lambda idx=li["idx"]: _pick_take(idx, -1)).pack(
                    side="left")
                tk.Label(tp, text="%d/%d" % (kept + 1, len(takes)),
                         bg=C["panel"], fg=C["faint"], font=fsm).pack(
                    side="left")
                btn(tp, "\u25b6",
                    lambda idx=li["idx"]: _pick_take(idx, 1)).pack(
                    side="left")

    def _line_mood_changed(idx, var):
        if st["pj"] is not None:
            lines = st["pj"].get("lines", [])
            if idx < len(lines):
                if var.get() == SUNG_CHOICE:
                    lines[idx]["sung"] = True           # this line goes to YuE2
                else:
                    lines[idx]["sung"] = False
                    lines[idx]["mood"] = var.get()
                _refresh_estimate()
                _autosave_project()
    win.dialogue_set_line_mood = lambda idx, m: _line_mood_changed(
        idx, type("V", (), {"get": staticmethod(lambda: m)}))

    def _edit_line_text(idx):
        if st["pj"] is None:
            return
        lines = st["pj"].get("lines", [])
        if idx >= len(lines):
            return
        old = lines[idx]["text"]
        if _driver:
            return
        new = _sd.askstring("Edit Line %d" % idx, "New text:",
                            initialvalue=old)
        if new and new != old:
            lines[idx]["text"] = new
            _refresh_lines()

    def _play_line(idx):
        if st["pj"] is None:
            return
        lines = st["pj"].get("lines", [])
        if idx >= len(lines):
            return
        takes = lines[idx].get("takes", [])
        kept = lines[idx].get("kept", 0)
        if not takes or kept >= len(takes):
            say("no take yet for line %d" % idx, warn=True)
            return
        proj_dir = os.path.join(dlg.dialogue_dir(),
                                st["pj"].get("slug", ""))
        wav = os.path.join(proj_dir,
                           takes[kept].get("file", ""))
        if not os.path.exists(wav):
            say("file missing for line %d" % idx, warn=True)
            return
        play_audio(wav)

    def _redo_line(idx):
        if _driver and not _go:
            say("REDO skipped (driver mode)")
            return
        # Drop the line's last take and make the new one RIGHT AWAY (it used to say "run
        # RENDER MISSING" and leave the second click to you).
        lines = st["pj"].get("lines", [])
        if idx < len(lines) and lines[idx].get("takes"):
            lines[idx]["takes"] = lines[idx]["takes"][:-1]
            lines[idx]["kept"] = max(0, len(lines[idx]["takes"]) - 1)
        _refresh_lines()
        if st.get("busy"):
            say("line %d will be redone after the current job \u2014 Make the missing lines"
                % idx, warn=True)
            return
        say("making a new take of line %d\u2026" % (idx + 1))
        _start_tts_job("missing")

    def _pick_take(idx, delta):
        if st["pj"] is None:
            return
        lines = st["pj"].get("lines", [])
        if idx >= len(lines):
            return
        takes = lines[idx].get("takes", [])
        if not takes:
            return
        kept = lines[idx].get("kept", 0)
        n = max(0, min(len(takes) - 1, kept + delta))
        lines[idx]["kept"] = n
        _refresh_lines()
        _autosave_project()

    # ===== 4 · MAKE =====================================================================
    b5 = makebar
    mk = tk.Frame(makebar, bg=C["panel"])
    mk.pack(fill="x", padx=10, pady=10)
    b_make = tk.Button(mk, text="Make the missing lines",
                       command=lambda: _start_tts_job("missing"),
                       bg=C["ochre"], fg="#14110f", activebackground="#d9b45e",
                       activeforeground="#14110f", relief="flat", borderwidth=0, font=fh,
                       padx=14, pady=8, highlightthickness=0)
    b_make.pack(side="left")
    btn(mk, "\u25b6 Play scene", lambda: _play_scene_auto()).pack(side="left", padx=8)
    est_lbl = lam(mk, "", fg=C["faint"], font=fmono)
    est_lbl.pack(side="left", padx=6)
    _adv_lbl = tk.Label(mk, text="More \u25b8", bg=C["panel"], fg=C["ochre"], font=fsm,
                        cursor="hand2")
    _adv_lbl.pack(side="right")
    adv = tk.Frame(makebar, bg=C["panel"])        # folded: engine, remake all, assemble, gap

    def _adv_toggle(_e=None):
        if adv.winfo_manager():
            adv.pack_forget()
            _adv_lbl.configure(text="More \u25b8")
        else:
            adv.pack(fill="x", padx=10, pady=(0, 10))
            _adv_lbl.configure(text="Less \u25be")
    _adv_lbl.bind("<Button-1>", _adv_toggle)

    eng_row = tk.Frame(adv, bg=C["panel"])
    eng_row.pack(anchor="w", pady=(0, 4))
    lam(eng_row, "Voice engine").pack(side="left")
    eng_menu = tk.OptionMenu(eng_row, eng_var, *[l for _k, l in dlg.ENGINES],
                             command=_engine_changed)
    eng_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                       highlightthickness=0, activebackground=C["rust"],
                       activeforeground="#fff", borderwidth=0, width=16, anchor="w")
    eng_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    eng_menu.pack(side="left", padx=6)
    _eng_lbl["w"] = lam(eng_row, "", fg=C["faint"], font=fmono)
    _eng_lbl["w"].pack(side="left", padx=4)
    _engine_note()

    mrow1 = tk.Frame(adv, bg=C["panel"])
    mrow1.pack(anchor="w", pady=2)
    btn(mrow1, "Remake every line",
        lambda: _start_tts_job("all")).pack(side="left")
    btn(mrow1, "Assemble scene",
        lambda: _assemble()).pack(side="left", padx=6)
    btn(mrow1, "Open folder",
        lambda: _open_scene_folder()).pack(side="left", padx=6)

    gap_row = tk.Frame(adv, bg=C["panel"])
    gap_row.pack(anchor="w", pady=(4, 0))
    lam(gap_row, "Gap").pack(side="left")
    gap_var = tk.IntVar(value=350)
    tk.Scale(gap_row, from_=0, to=1500, resolution=10, orient="horizontal",
             variable=gap_var, length=200, bg=C["panel"], fg=C["ink"],
             troughcolor=C["panel2"], highlightthickness=0, font=fsm,
             activebackground=C["rust"], showvalue=True
             ).pack(side="left", padx=6)
    lam(gap_row, "ms between lines", fg=C["faint"]).pack(side="left", padx=6)
    _gap_job = {"j": None}

    def _gap_changed():
        if _gap_job["j"]:
            try:
                win.after_cancel(_gap_job["j"])
            except Exception:
                pass
        _gap_job["j"] = win.after(600, _autosave_project)
    # a TRACE, not the Scale's -command: that only fires on some paths; the variable sees all
    gap_var.trace_add("write", lambda *_a: _gap_changed())

    def _scene_path():
        return os.path.join(dlg.dialogue_dir(), (st["pj"] or {}).get("slug", "untitled"),
                            "scene.wav")

    def _scene_stale():
        """The scene needs (re)building: missing, or older than a kept take or the project."""
        sp_ = _scene_path()
        if not os.path.exists(sp_):
            return True
        t_scene = os.path.getmtime(sp_)
        pdir = os.path.dirname(sp_)
        for li in (st["pj"] or {}).get("lines", []):
            tks = li.get("takes", [])
            k = li.get("kept", 0)
            if tks and k < len(tks):
                f_ = os.path.join(pdir, tks[k].get("file", ""))
                if os.path.exists(f_) and os.path.getmtime(f_) > t_scene:
                    return True
        return st.get("scene_gap") != int(gap_var.get())

    def _play_scene_auto():
        """▶ Play scene builds the scene first whenever it is missing or stale — there is no
        separate ASSEMBLE step to remember."""
        if st["pj"] is None:
            say("type or paste a script first", warn=True)
            return
        if _scene_stale():
            _assemble()
            st["scene_gap"] = int(gap_var.get())
        _play_scene()

    def _refresh_estimate(*_a):
        if st["pj"] is None:
            est_lbl.configure(text="")
            return
        lines = st["pj"].get("lines", [])
        n_lines = len(lines)
        # ⚠️ The design count comes from plan_render() itself, not a second copy of the rule —
        # a re-implemented count silently disagreed with what a render actually sends.
        all_v = {v["slug"]: v for v in dlg.list_voices()}
        eng = _engine_key()
        designs, _job, _sk = plan_render(st["pj"], all_v, "all", eng)
        n_designs = len(designs)
        n_sung = sum(1 for l in lines if dlg.is_sung(l))
        est = dlg.estimate_seconds(n_lines - n_sung, n_designs, engine=eng)
        txt = "\u2248 %.0f s of GPU" % est
        if eng != dlg.DEFAULT_ENGINE:
            txt += "  (%s)" % dlg.engine_label(eng)
        if n_sung:
            sv = plan_sung(dict(st["pj"], lines=[dict(l, takes=[]) for l in lines]), {}, "all")
            txt += "  + \u2248 %.0f s YuE2 for %d sung" % (sung_estimate_seconds(sv), n_sung)
        est_lbl.configure(text=txt)
        _n_miss = sum(1 for l in lines if not l.get("takes"))
        try:
            b_make.configure(text=("Make the %d missing line%s" % (_n_miss, "" if _n_miss == 1
                                                                     else "s"))
                             if _n_miss else "Every line has a take")
        except Exception:
            pass

    # ---- TTS job management ============================================================
    def _start_tts_job(mode, voice=None, mood=None):
        """Start TTS on a worker thread. mode: missing|all|audition."""
        if st.get("busy"):
            say("already rendering", warn=True)
            return

        def _work():
            try:
                _tts_work(mode, voice, mood, job)
            except Exception as exc:
                job["err"] = repr(exc)
            finally:
                job["done"] = True

        job = {"done": False, "out": None, "err": None, "t0": time.time(),
               "mode": mode, "progress": "", "voice": voice, "mood": mood}
        st["busy"] = job
        say("starting TTS job (%s)\u2026" % mode)
        threading.Thread(target=_work, daemon=True).start()
        win.after(700, lambda: _poll_tts(job))

    def _tts_work(mode, voice, mood, job):
        """Run TTS. Called from worker thread — NEVER touch Tk objects."""
        try:
            from audio.client import tts_batch, transcribe_many, deploy
            deploy()
        except ImportError:
            job["err"] = "audio.client not available"
            return
        except Exception as exc:
            job["err"] = str(exc)
            return

        # ROOM ON CARD 2 first (cards.py): frees an idle writer / idle ComfyUI by itself, and
        # names whatever is WORKING there instead of letting the voice model die with an OOM.
        try:
            import cards as _cards
        except Exception:
            _cards = None
        if _cards is not None and _cards.enabled():
            _eng = (st.get("pj") or {}).get("engine") or dlg.DEFAULT_ENGINE
            if mode == "audition":
                _ek = "voice"
            elif _eng == "drama3":
                _ek = "drama3"          # needs BOTH cards: card 1 is its own
            elif _eng == "fish":
                _ek = "fish"
            else:
                _ek = "voice"
            try:
                _room = _cards.preflight(_ek, card=0 if _ek == "drama3" else 1)
                if _room.get("freed"):
                    job["progress"] = "freed card 2 (%s)" % ", ".join(_room["freed"])
            except Exception as exc:
                if type(exc).__name__ == "CardBusy":
                    job["card_busy"] = exc
                    return
        # Unload Director if resident (GPU1 needs room) — the old path, when the card
        # manager is off
        elif os.environ.get("FORGE_LAB_NODIRECTOR") != "1":
            try:
                import director as _dir
                d = _dir.Director()
                try:
                    ds = d.status()
                    # ⚠️ status() reports "loaded" (not "loaded_models"): GPU1 cannot hold the
                    # 27B LLM and TTS together, so a resident model MUST go first.
                    if ds.get("loaded"):
                        d.unload_all()
                        job["progress"] = "freed card 2 (unloaded %s)" % ", ".join(ds["loaded"])
                except Exception:
                    pass
            except Exception:
                pass

        if mode == "audition" and voice is not None:
            _tts_audition(voice, mood, job)
            return

        _tts_render(mode, job)

    def _tts_audition(voice, mood, job):
        """Design one sample for the given mood and play it."""
        slug = dlg.slugify(voice.get("name", "untitled"))
        moods = voice.setdefault("moods", {})
        m_data = moods.setdefault(mood, {
            "extra": dlg.MOOD_WORDS.get(mood, ""),
            "wav": "",
            "ref_text": dlg.DEFAULT_SAMPLE,
        })
        ref_text = m_data.get("ref_text", dlg.DEFAULT_SAMPLE)
        desc = dlg.compose_description(voice, mood)
        out_name = "%s_%s.wav" % (slug, mood)
        v_dir = dlg.voices_dir()
        os.makedirs(os.path.join(v_dir, slug), exist_ok=True)

        design_job = {
            "design": [{
                "id": "%s:%s" % (slug, mood),
                "instruct": desc,
                "text": ref_text,
                "out": out_name,
            }],
            "clone": [],
            "voices": {},
            "language": "English",
        }
        out_dir = tempfile.mkdtemp(prefix="forge-dlg-")
        try:
            from audio.client import tts_batch
            info = tts_batch(design_job, out_dir)
            items = info.get("items", [])
            if items and items[0].get("out") and "error" not in items[0]:
                local_wav = items[0]["out"]
                dst = os.path.join(v_dir, slug, "%s.wav" % mood)
                shutil.copyfile(local_wav, dst)
                m_data["wav"] = "%s.wav" % mood
                # Update the editing copy
                if st.get("editing"):
                    em = st["editing"].setdefault("moods", {})
                    em.setdefault(mood, {})["wav"] = "%s.wav" % mood
                job["out"] = dst
                job["progress"] = "audition done for %s/%s" % (slug, mood)
                # Play it
                # playback happens in _poll_tts, ON THE TK THREAD: this runs on a
                # worker, and play_audio() calls say() (Tk). Off-thread Tk is why the
                # clip played with nothing on screen confirming it.
                job["out"] = dst
            else:
                err = items[0].get("error", "no output") if items else "no items"
                job["err"] = "design failed: %s" % err
        except Exception as exc:
            job["err"] = str(exc)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def _tts_render(mode, job):
        """Render missing (or all) lines. Runs on the WORKER thread — never touches Tk.

        ⚠️ Reviewer rewrite (2026-10-12). The first version had four faults no GPU-free test
        could see: clone items named a voice key the voices map did not contain (every line
        would fail), designs and clones went in ONE batch so a brand-new voice had no reference
        yet, the design-output loop also swallowed line takes and wrote them over the voice's
        reference sample, and RENDER ALL re-designed every voice (a new "person" each click).

        Now: (1) a DESIGN batch only for samples that do not exist yet — RENDER ALL
        never re-designs, a voice is changed only by AUDITION; (2) a READING batch — keyed
        "<slug>:<mood>" on qwen (a mood IS its own sample), keyed "<slug>" on fish (one
        reference per voice, the mood rides in the text as an inline [tag]); (3) ASR on the new
        takes; (4) save. `plan_render()` is the pure part. The DESIGN batch always goes to the
        qwen rig, because fish has no VoiceDesign at all.
        """
        pj = st["pj"]
        if pj is None:
            job["err"] = "no project"
            return
        eng = pj.get("engine") or dlg.DEFAULT_ENGINE
        all_v = {v["slug"]: v for v in dlg.list_voices()}
        proj_dir = os.path.join(dlg.dialogue_dir(), pj.get("slug", "untitled"))
        lines_dir = os.path.join(proj_dir, "lines")
        os.makedirs(lines_dir, exist_ok=True)
        from audio.client import tts_batch, transcribe_many

        designs, _clones, skipped = plan_render(pj, all_v, mode, eng)
        for msg in skipped:
            job["progress"] += "; " + msg

        # (1) DESIGN the missing reference samples — ALWAYS on the qwen rig (see plan_render)
        if designs:
            job["progress"] = "designing %d voice sample(s)" % len(designs)
            out_dir = tempfile.mkdtemp(prefix="forge-dlg-")
            try:
                info = tts_batch({"design": designs, "clone": [], "voices": {},
                                  "language": "English"}, out_dir, engine="qwen")
                for item in info.get("items", []):
                    if "error" in item or not item.get("out"):
                        job["progress"] += "; design %s failed: %s" % (
                            item.get("id"), item.get("error", "no output"))
                        continue
                    slug, mood = item["id"].split(":", 1)
                    v = all_v.get(slug)
                    if v is None or v.get("source") == "character":
                        continue
                    os.makedirs(os.path.join(dlg.voices_dir(), slug), exist_ok=True)
                    shutil.copyfile(item["out"],
                                    os.path.join(dlg.voices_dir(), slug, "%s.wav" % mood))
                    ref_text = next((d["text"] for d in designs if d["id"] == item["id"]),
                                    dlg.DEFAULT_SAMPLE)
                    md = v.setdefault("moods", {}).setdefault(mood, {})
                    md.update({"wav": "%s.wav" % mood, "ref_text": ref_text,
                               "extra": dlg.MOOD_WORDS.get(mood, "")})
                    if not _driver or _go:
                        dlg.save_voice(v)
            except Exception as exc:
                job["err"] = "voice design failed: %s" % exc
                return
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)

        # (2) READ every line that needs a take — re-planned now the samples exist
        _d, clones, skipped2 = plan_render(pj, all_v, mode, eng)
        for msg in skipped2:
            if msg not in skipped:
                job["progress"] += "; " + msg
        new_takes = []
        if clones:
            job["progress"] = "reading %d line(s) on %s" % (len(clones["clone"]),
                                                            dlg.engine_label(eng))
            out_dir = tempfile.mkdtemp(prefix="forge-dlg-")
            try:
                info = tts_batch(clones, out_dir, engine=eng)
                by_id = {c["id"]: c for c in clones["clone"]}
                for item in info.get("items", []):
                    c = by_id.get(item.get("id"))
                    if c is None:
                        continue
                    li = pj["lines"][c["_line"]]
                    if "error" in item or not item.get("out"):
                        li["error"] = item.get("error", "no output")
                        job["progress"] += "; line %d failed" % (c["_line"] + 1)
                        continue
                    li.pop("error", None)
                    wav_name = "%03d_%d.wav" % (li["idx"], len(li.get("takes", [])) + 1)
                    shutil.copyfile(item["out"], os.path.join(lines_dir, wav_name))
                    li.setdefault("takes", []).append({
                        "file": "lines/%s" % wav_name,
                        "seconds": item.get("duration", 0),
                        "heard": "", "score": None})
                    li["kept"] = len(li["takes"]) - 1
                    new_takes.append(li)
            except Exception as exc:
                job["err"] = "line render failed: %s" % exc
                return
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)

        # (2b) SUNG lines -> YuE2 on GPU0 through the app's own Song path
        sung = plan_sung(pj, all_v, mode)
        if sung:
            if ctx.get("submit") and ctx.get("wait_and_collect"):
                def _pr(msg):
                    job["progress"] = msg
                new_takes += render_sung(sung, pj, proj_dir, ctx["submit"],
                                         ctx["wait_and_collect"], ctx.get("runs") or
                                         os.path.join(dlg._data(), "runs"), progress=_pr)
            else:
                job["progress"] += "; %d sung line(s) skipped (no Song engine)" % len(sung)

        # (3) LISTEN BACK — the objective check, since nobody can hear it from here
        if new_takes:
            job["progress"] = "checking %d take(s) by ear (ASR)" % len(new_takes)
            try:
                paths = [os.path.join(proj_dir, li["takes"][li["kept"]]["file"])
                         for li in new_takes]
                heard = transcribe_many(paths)
                for li, text in zip(new_takes, heard):
                    tk2 = li["takes"][li["kept"]]
                    tk2["heard"] = text
                    tk2["score"] = round(dlg.score(li["text"], text), 3)
            except Exception as exc:
                job["progress"] += "; ASR failed: %s" % exc

        # (4) save
        if os.environ.get("FORGE_LAB_NOAUTOSAVE") != "1" and (not _driver or _go):
            try:
                dlg.save_project(pj)
            except Exception as exc:
                job["progress"] += "; could not save project: %s" % exc
        flagged = sum(1 for li in new_takes
                      if (li["takes"][li["kept"]].get("score") or 0) < dlg.PASS_SCORE)
        job["out"] = True
        job["progress"] = "%d take(s) made, %d flagged for a listen" % (len(new_takes), flagged)

    def _poll_tts(job):
        if not job["done"]:
            elapsed = time.time() - job["t0"]
            say("%s  %.0f s" % (job.get("progress", "working\u2026"), elapsed))
            win.after(500, lambda: _poll_tts(job))
            return
        st["busy"] = None
        try:
            import cards as _cards
            threading.Thread(target=_cards.release, args=("dialogue",), daemon=True).start()
        except Exception:
            _cards = None
        if job.get("card_busy") is not None:
            # card 2 is WORKING on something: say what, in words, and offer the choices
            _cb = job["card_busy"]
            say("%s is busy \u2014 choose what to do" % _cards.card_name(_cb.card), warn=True)
            import cards_ui
            cards_ui.handle(win, _cb, ctx, say=say, owner="dialogue",
                            retry=lambda: _start_tts_job(job["mode"], job.get("voice"),
                                                         job.get("mood")))
            return
        if job["err"]:
            # ⚠️ ONE readable sentence on screen; the rig's full traceback goes to the error
            # log. A pasted traceback filled the whole window (2026-10-17, an OOM on card 2).
            _full = str(job["err"])
            try:
                with open(os.path.join(DATA, "forge_lab_errors.log"), "a") as _lf:
                    _lf.write("%s  dialogue TTS: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                         _full))
            except Exception:
                pass
            _short = _cards.explain(_full) if _cards is not None else _full[:200]
            say("TTS error: %s" % _short, warn=True)
            return
        say("TTS done  %s" % job.get("progress", ""))
        _qp = st.pop("ql_pending", None)
        if job.get("mode") == "audition" and job.get("out"):
            if _qp:
                st.setdefault("ql_takes", []).insert(0, dict(_qp, wav=job["out"]))
                del st["ql_takes"][8:]
                _ql_show()
            play_audio(job["out"])
        _refresh_lines()
        _refresh_estimate()
        if job.get("mode") in ("missing", "all") and st["pj"] is not None:
            _ls = st["pj"].get("lines", [])
            if _ls and all(l.get("takes") for l in _ls):
                _assemble()
                st["scene_gap"] = int(gap_var.get())
                say("every line has a take \u2014 the scene is ready: \u25b6 Play scene")
            _autosave_project()

    def _assemble():
        if st["pj"] is None:
            say("no project", warn=True)
            return
        pj = st["pj"]
        lines = pj.get("lines", [])
        if not lines:
            say("no lines to assemble", warn=True)
            return
        cast = pj.get("cast", {})
        proj_dir = os.path.join(dlg.dialogue_dir(), pj.get("slug", "untitled"))
        all_v = {v["slug"]: v for v in dlg.list_voices()}
        tmpdirs = []

        paths = []
        for li in lines:
            spk = dlg.normalise_speaker(li["speaker"])
            slug = cast.get(spk)
            takes = li.get("takes", [])
            kept = li.get("kept", 0)
            if not takes or kept >= len(takes):
                say("line %d has no take" % li["idx"], warn=True)
                continue
            wav_rel = takes[kept].get("file", "")
            wav_abs = os.path.join(proj_dir, wav_rel)
            if not os.path.exists(wav_abs):
                say("file missing for line %d" % li["idx"], warn=True)
                continue
            # Apply voice fx -> peak_normalise
            v = all_v.get(slug)
            if v:
                fx_str = dlg.fx_filter(v.get("fx", {}))
                if fx_str:
                    td = tempfile.mkdtemp(prefix="forge-dlg-")
                    tmpdirs.append(td)
                    tmp = os.path.join(td, "fx_%03d.wav" % li["idx"])
                    try:
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", wav_abs,
                             "-af", fx_str, tmp],
                            capture_output=True, timeout=120, check=True)
                    except Exception:
                        say("FX failed for line %d, using raw" % li["idx"],
                            warn=True)
                        tmp = wav_abs
                    nrm = os.path.join(td, "nrm_%03d.wav" % li["idx"])
                    try:
                        dlg.peak_normalise(tmp, nrm)
                        paths.append(nrm)
                        continue
                    except Exception:
                        pass
            # No fx: just peak normalise
            td = tempfile.mkdtemp(prefix="forge-dlg-")
            tmpdirs.append(td)
            nrm = os.path.join(td, "nrm_%03d.wav" % li["idx"])
            try:
                dlg.peak_normalise(wav_abs, nrm)
                paths.append(nrm)
            except Exception as exc:
                say("peak norm failed for line %d: %s" % (
                    li["idx"], str(exc)[:50]), warn=True)
        if not paths:
            say("no usable takes to assemble", warn=True)
            return
        out = os.path.join(proj_dir, "scene.wav")
        gap_ms = gap_var.get()
        try:
            total = dlg.assemble(paths, out, gap_ms=gap_ms)
            say("scene assembled: %.1f s  (%d lines, %d ms gap)"
                % (total, len(paths), gap_ms))
        except Exception as exc:
            say("assembly failed: %s" % str(exc)[:80], warn=True)

    def _play_scene():
        if st["pj"] is None:
            return
        proj_dir = os.path.join(dlg.dialogue_dir(),
                                st["pj"].get("slug", "untitled"))
        scene = os.path.join(proj_dir, "scene.wav")
        if not os.path.exists(scene):
            say("assemble the scene first", warn=True)
            return
        play_audio(scene)

    def _open_scene_folder():
        if st["pj"] is None:
            say("no project", warn=True)
            return
        proj_dir = os.path.join(dlg.dialogue_dir(),
                                st["pj"].get("slug", "untitled"))
        if not os.path.exists(proj_dir):
            say("project directory not created yet", warn=True)
            return
        _open(proj_dir)

    # ------------------------------------------------------------------ initial
    say("paste a script, then PARSE it")

    if btn_colors:
        try:
            btn_colors(win)
        except Exception:
            pass

    # ------------------------------------------------------------------ driver API
    def _dialogue_dump():
        return {
            "lines": len(st["pj"].get("lines", [])) if st["pj"] else 0,
            "speakers": list(st["cast_vars"].keys()),
            "cast": {s: v.get() for s, v in st["cast_vars"].items()},
            "estimate_text": est_lbl.cget("text") if est_lbl else "",
            "busy": st["busy"] is not None,
            "status": status.cget("text") if status else "",
            "project": st["pj"]["slug"] if st["pj"] else None,
            "engine": _engine_key(),
            "engine_label": eng_var.get(),
            "engine_note": _eng_lbl["w"].cget("text") if _eng_lbl.get("w") else "",
            "project_engine": (st["pj"] or {}).get("engine"),
            "buttons": _all_button_labels(win),
        }

    def _all_button_labels(w0):
        out = []
        for ch in w0.winfo_children():
            if ch.winfo_class() == "Button":
                out.append(ch.cget("text"))
            out.extend(_all_button_labels(ch))
        return out
    win.dialogue_dump = _dialogue_dump

    def _set_script_text(text):
        t_script.delete("1.0", "end")
        t_script.insert("1.0", text)
    win.dialogue_set_script = _set_script_text

    def _do_parse():
        _parse()
    win.dialogue_parse = _do_parse

    def _do_set_engine(key):
        """Driver hook: pick a VOICE ENGINE the way a menu click does (label -> callback)."""
        eng_var.set(dlg.engine_label(key))
        _engine_changed()
        return _engine_key()
    win.dialogue_set_engine = _do_set_engine

    def _do_new_voice(name, spk):
        """Create a voice in the bank and assign it to speaker spk in the cast."""
        v = dlg.new_voice(name)
        v["characters"] = [spk]
        dlg.save_voice(v)
        _rebuild_project_list()
        _refresh_cast()
        if spk in st["cast_vars"]:
            st["cast_vars"][spk].set(v["slug"])
        return v
    win.dialogue_new_voice = _do_new_voice

    def _do_auto_cast():
        _auto_cast()
    win.dialogue_auto_cast = _do_auto_cast

    def _set_cast(spk, slug):
        if spk in st["cast_vars"]:
            st["cast_vars"][spk].set(slug)
    win.dialogue_set_cast = _set_cast
    # the speaker's real dropdown, so a driver can pick from it exactly as a click does
    win.dialogue_cast_menu = lambda spk: st["cast_menus"].get(spk)
    win.dialogue_project = lambda: st["pj"]
    win.dialogue_refresh_cast = lambda: _refresh_cast()
    win.dialogue_play_scene = lambda: _play_scene_auto()
    win.dialogue_parts = {"script": t_script, "make": b_make, "makebar": makebar,
                          "gap": gap_var, "lint": lint_lbl, "quick_row": _qlh,
                          "quick_more": _qlr2, "cast_frame": cast_frame, "lines": lf_inner,
                          "scene_path": _scene_path, "saved": saved_lbl,
                          # TRY A LINE, for drivers (TESTCARDS)
                          "quick_text": ql_txt, "quick_speak": _ql_speak, "status": status}

    def _drv_edit(slug):
        _edit_voice(slug)
        return _ew.get("win")
    win.dialogue_edit_voice = _drv_edit
    win.dialogue_editor_state = lambda: {
        "accent": st["editor_accent"][0].get() if st.get("editor_accent") else None,
        "accent_menu": st["editor_accent"][1].get() if st.get("editor_accent") else None,
        "pick_accent": st["editor_accent"][2] if st.get("editor_accent") else None,
        "desc": st["editor_desc"].get("1.0", "end").strip() if st.get("editor_desc") else "",
        "singing": st["editor_singing"] if st.get("editor_singing") else None,
        "canvas": st.get("editor_canvas")}

    return win