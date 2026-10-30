#!/usr/bin/env python3
"""GPU-free assertions for the Studio pipeline engine (studio_pipeline.py).

Nothing here touches forge or the GPU: Director, forge_core.submit/wait_and_collect/
estimate_seconds, the audio client and subprocess are all stubbed. It asserts the
pipeline's LOGIC — migration defaults, resume, the seconds clamp, the lip-sync guide audio,
'cont' referencing the previous chosen clip, the caption ASS builder, finish()'s local-vs-
remote choice, the render budget stop, and atomic saves.

Run: /opt/homebrew/bin/python3 tools/pipeline_matrix.py   (exit 0 = green, 1 = failure)
"""
import json
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ⚠️ Point forge_lab's DATA at a scratch dir BEFORE importing it, so nothing this suite does
# can touch Ryan's real runs/ or characters/.
_TMP = tempfile.mkdtemp(prefix="pipeline_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
os.environ.pop("FORGE_AUDIO_LOCAL", None)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))

import forge_core as fc            # noqa: E402
import forge_lab as fl             # noqa: E402
fc.configure(data_dir=_TMP)

FAILS = []
CHECKS = [0]


def check(cond, msg):
    CHECKS[0] += 1
    if cond:
        print("  ok  %s" % msg)
    else:
        print("FAIL  %s" % msg)
        FAILS.append(msg)


def load_module(path, name):
    """Compile SOURCE TEXT, never a cached .pyc (a same-size mutation would else read old code)."""
    with open(path, "rb") as f:
        src = f.read()
    mod = types.ModuleType(name)
    mod.__file__ = path
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDirector:
    def __init__(self):
        self.calls = []
        self.loaded = 0
        self.unloaded = 0

    def load(self):
        self.loaded += 1
        return {}

    def unload(self, model=None):
        self.unloaded += 1
        return {}

    def pitch(self, ch, n=5, length_s=20, brief=""):
        self.calls.append("pitch")
        return [{"title": "t%d" % i, "logline": "l", "hook": "h"} for i in range(n)]

    def script(self, ch, premise, length_s=20):
        self.calls.append("script")
        return {"title": "s", "hook": "h", "beats": [], "button": "b"}

    def shots(self, ch, script):
        self.calls.append("shots")
        return [{"n": 1, "seconds": 4, "intent": "lipsync", "camera": "c", "action": "a",
                 "line": "hello there", "prompt": "P1", "engine": "h3", "video_mode": "r2v"},
                {"n": 2, "seconds": 3, "intent": "continue", "camera": "c", "action": "a",
                 "line": "", "prompt": "P2", "engine": "h3", "video_mode": "cont"}]

    def voice_description(self, ch):
        self.calls.append("voice_description")
        return "a flat, nasal man"

    def post_copy(self, ch, script):
        self.calls.append("post_copy")
        return {"title": "T", "caption": "C", "hashtags": ["a", "b"]}


class FakeAudio:
    def __init__(self):
        self.designs = []
        self.clones = []
        self.aligns = []

    def tts_design(self, desc, text, out, **kw):
        self.designs.append((desc, text, out))
        open(out, "wb").write(b"RIFFdesign")
        return {"ok": True, "mode": "design"}

    def tts_clone(self, ref, ref_text, text, out, **kw):
        self.clones.append((ref, ref_text, text, out))
        open(out, "wb").write(b"RIFFclone")
        return {"ok": True, "mode": "clone"}

    def align(self, wav, text, **kw):
        self.aligns.append((wav, text))
        return [{"word": "hello", "start": 0.10, "end": 0.45},
                {"word": "there", "start": 0.50, "end": 0.90}]


class FakeSubmit:
    def __init__(self, runs_dir):
        self.runs = runs_dir
        self.calls = []
        self.n = 0

    def __call__(self, mode, p, ref_paths=None):
        self.n += 1
        pid = "run%02d" % self.n
        self.calls.append({"mode": mode, "p": dict(p), "refs": list(ref_paths or [])})
        d = os.path.join(self.runs, pid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "clip.mp4"), "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42")
        return pid, 1234 + self.n

    def wait_and_collect(self, pid, **kw):
        return {"id": pid, "images": [{"file": "clip.mp4", "bytes": 12}]}


def fresh(overrides=None):
    """A loaded pipeline module with all the seams stubbed, plus its fakes."""
    mod = load_module(os.path.join(ROOT, "studio_pipeline.py"), "studio_pipeline")
    fak = FakeDirector()
    fau = FakeAudio()
    fs = FakeSubmit(fc.RUNS)
    mod.get_director = lambda: fak
    mod.audio_client = fau
    fc.submit = fs
    fc.wait_and_collect = fs.wait_and_collect
    fc.estimate_seconds = lambda mode, p, refs=None, warm_engine=None: 12.0
    fl._index_update = lambda pid, **fields: None
    # a real still so _character_refs finds one
    still = os.path.join(_TMP, "still.png")
    with open(still, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
    fc.character_still = lambda slug: still
    fl.character_still = lambda slug: still
    mod._fakes = {"dir": fak, "audio": fau, "submit": fs, "still": still}
    if overrides:
        overrides(mod)
    return mod, fak, fau, fs


def two_shot_project(mod, slug=None):
    pj = mod.new_project(["bighead"], "he tries on hats in a shop", length_s=8, quality="Draft",
                         slug=slug)
    d = mod.get_director()
    pj["pitches"] = d.pitch(  {"slug": "bighead"}, n=2)
    pj["pitch"] = pj["pitches"][0]
    pj["script"] = d.script({"slug": "bighead"}, pj["pitch"])
    pj["shots"] = mod.clamp_shots([dict(s) for s in d.shots({"slug": "bighead"}, pj["script"])],
                                  pj["length_s"])
    for s in pj["shots"]:
        mod._shot_migrate(s)
    mod.save_project(pj)
    return pj


# ---------------------------------------------------------------------------
def main():
    print("== migrate defaults ==")
    mod, fak, fau, fs = fresh()
    pj = mod.project_migrate({})
    for k in ("slug", "title", "created", "characters", "idea", "length_s", "quality",
              "pitch", "pitches", "script", "shots", "voice", "cut", "log"):
        check(k in pj, "project_migrate fills %r" % k)
    sh = mod._shot_migrate({})
    for k in ("n", "seconds", "intent", "engine", "video_mode", "camera", "action", "line",
              "speaker", "prompt", "audio_wav", "words", "takes", "chosen", "status", "start"):
        check(k in sh, "shot_migrate fills %r" % k)

    print("== canvas + steps legality ==")
    for q, (w, h) in mod.CANVAS.items():
        check(w % 32 == 0 and h % 32 == 0, "%s canvas %dx%d is a multiple of 32" % (q, w, h))
    check(mod.STEPS == 12, "STEPS is 12, never the soft 4-step draft")
    check(mod.STEPS != 4, "STEPS is not 4 (faces go soft)")
    p = mod.shot_params({"slug": "x", "quality": "Draft"}, {"seconds": 3, "prompt": "p"})
    check(p["length"] == fc.h3_frames(3) and (p["length"] - 5) % 17 == 0,
          "shot length uses h3_frames (n*17+5): %s" % p["length"])

    print("== seconds clamp ==")
    shots = [{"seconds": 4}, {"seconds": 9}, {"seconds": 2}]
    out = mod.clamp_shots([dict(s) for s in shots], target=20)
    check(all(s["seconds"] in (3, 5, 8) for s in out),
          "every clamped second is in {3,5,8} (%r)" % [s["seconds"] for s in out])
    check([s["n"] for s in out] == list(range(1, len(out) + 1)),
          "clamp renumbers n (1..n): %r" % [s["n"] for s in out])
    big = mod.clamp_shots([{"seconds": 8}, {"seconds": 8}, {"seconds": 8}], target=8)
    check(sum(s["seconds"] for s in big) <= 8 and len(big) >= 1,
          "clamp trims toward the target (sum %d, %d shots)" % (sum(s["seconds"] for s in big), len(big)))

    print("== voice_lines -> lip-sync gets guide audio ==")
    mod, fak, fau, fs = fresh()
    pj = two_shot_project(mod)
    mod.voice_lines(pj)
    check(fak.unloaded >= 1, "voice_lines UNLOADS the Director before TTS (GPU1 is shared)")
    s1, s2 = pj["shots"][0], pj["shots"][1]
    check(bool(s1.get("audio_wav")) and os.path.exists(s1["audio_wav"]),
          "shot 1 got a spoken wav")
    check(bool(s1.get("words")), "shot 1 got aligned words")
    check(fau.designs and not fau.clones, "a first line DESIGNS a voice")

    # second line reuses the saved voice (clone) — a fresh project, voice now exists
    pj2 = two_shot_project(mod, slug=pj["slug"])       # the same project, re-voiced
    pj2["shots"][0]["n"] = 1
    mod.voice_lines(pj2)
    check(bool(fau.clones), "a later line CLONES the saved voice instead of re-designing")
    _pa = mod.new_project(["bighead"], "x", title="Same Title")
    _pb = mod.new_project(["bighead"], "y", title="Same Title")
    check(_pa["slug"] == "same-title" and _pb["slug"] == "same-title-2" and
          mod.load_project("same-title")["idea"] == "x",
          "a second project with the same title gets its own folder (it used to overwrite "
          "the first run's video and takes)")

    print("== render: guide_audio_path + refs ==")
    mod, fak, fau, fs = fresh()
    pj = two_shot_project(mod)
    mod.voice_lines(pj)
    mod.render_shot(pj, 1)
    call1 = fs.calls[-1]
    check(call1["p"]["guide_audio_path"] == pj["shots"][0]["audio_wav"],
          "the lip-sync shot submits guide_audio_path")
    check(call1["p"]["video_mode"] == "r2v", "shot 1 is r2v")
    _p1 = call1["p"]["prompt"]
    check("is the character" not in _p1 and
          all(("%s:" % s) in _p1 for s in ("subject_definitions", "summary", "retention_analysis",
                                           "detailed_description", "overall_soundscape",
                                           "non_diegetic_music"))
          and "whose appearance comes from <Picture 1>" in _p1
          and "[reference generation]" in _p1 and "(S1) says: <d>[English] " in _p1,
          "r2v is SENT as Ref2VA: six sections, the still cited inside <Subject 1>, the line in "
          "<d> - no bare '<Picture 1> is the character.' tag (not in MiniMax's spec)")
    _pk = mod.h3_prompt(pj, dict(pj["shots"][0]), "r2v", ["a.png", "b.png", "c.png"])
    check("<Picture 1> (the main still), with <Picture 2>, <Picture 3> showing the same person"
          in _pk, "a character PACK gives every still its job (untagged extra stills blend faces)")
    _dirp = ("subject_definitions:\n<Subject 1> is a man in <Picture 1>.\nsummary:\nx\n"
             "retention_analysis:\ny\ndetailed_description:\nz\noverall_soundscape:\nq\n"
             "non_diegetic_music:\nN/A")
    _pd = mod.h3_prompt(pj, dict(pj["shots"][0], prompt=_dirp), "r2v", ["a.png", "b.png"])
    check(_pd.replace("<Picture 1> (the main still), with <Picture 2> showing the same person "
                      "from other angles", "<Picture 1>") == _dirp,
          "a Director Ref2VA prompt is sent as written, only the pack's extra stills named")
    check(mod.h3_prompt(pj, dict(pj["shots"][0], prompt=_dirp), "r2v", ["a.png"]) == _dirp,
          "...and with one still it is sent unchanged")
    check(pj["shots"][0]["chosen"] == "run01", "the first take is chosen automatically")

    # cont: previous chosen clip's LAST FRAME becomes the ref
    extracted = {}

    def fake_last_frame(src, out):
        extracted["src"] = src
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "wb").write(b"\x89PNG\r\n\x1a\n")
        return out

    fl.long_last_frame = fake_last_frame
    mod.render_shot(pj, 2)
    call2 = fs.calls[-1]
    check(call2["p"]["video_mode"] == "cont", "shot 2 is cont")
    check("guide_audio_path" not in call2["p"], "the cont shot does NOT carry guide audio")
    check(extracted.get("src", "").endswith("clip.mp4"),
          "cont extracted the previous CHOSEN clip's last frame (%s)" % extracted.get("src"))
    check(call2["refs"] and call2["refs"][0].endswith("shot02_first.png"),
          "cont submits the last-frame png as its ref (%r)" % call2["refs"])

    print("== render budget stop ==")
    mod, fak, fau, fs = fresh()
    pj = two_shot_project(mod)
    mod.voice_lines(pj)
    fl.long_last_frame = fake_last_frame
    ran = mod.render_all(pj, budget_s=20)   # each shot estimates 12 s -> second would exceed
    check(ran == [1], "budget 20 stops before shot 2 (ran %r)" % ran)
    check(len(fs.calls) == 1, "only one shot was submitted under the budget")
    mod, fak, fau, fs = fresh()
    pj = two_shot_project(mod)
    mod.voice_lines(pj)
    fl.long_last_frame = fake_last_frame
    ran2 = mod.render_all(pj, budget_s=None)
    check(ran2 == [1, 2], "no budget renders every shot (%r)" % ran2)

    print("== resume skips done stages ==")
    mod, fak, fau, fs = fresh()
    pj = two_shot_project(mod)
    fak.calls = []                                    # ignore the SETUP calls above
    pj["shots"][0]["chosen"] = "runX"
    pj["shots"][0]["status"] = "done"
    pj["shots"][1]["chosen"] = "runY"
    pj["shots"][1]["status"] = "done"
    mod.save_project(pj)
    mod.run(pj, stages=["pitch", "choose", "script", "shots"])
    check(not any(c in ("pitch", "script", "shots") for c in fak.calls),
          "resume does NOT regenerate pitch/script/shots (%r)" % fak.calls)
    ran3 = mod.render_all(pj, budget_s=None)
    check(ran3 == [], "render_all skips an already-chosen shot (%r)" % ran3)
    check(not fs.calls, "no render was submitted for a fully-chosen project")

    print("== ASS caption builder ==")
    ass = mod.build_ass([
        {"start": 0.0, "words": [{"word": "one", "start": 0.0, "end": 0.2},
                                 {"word": "two", "start": 0.2, "end": 0.4},
                                 {"word": "three", "start": 0.4, "end": 0.6}]},
        {"start": 3.0, "words": [{"word": "four", "start": 0.0, "end": 0.2},
                                 {"word": "five", "start": 0.2, "end": 0.4},
                                 {"word": "six", "start": 0.4, "end": 0.6},
                                 {"word": "seven", "start": 0.6, "end": 0.8}]},
    ])
    check("[Script Info]" in ass and "[V4+ Styles]" in ass and "[Events]" in ass,
          "ASS has the three required sections")
    def _plain(a):
        return [re.sub(r"\{[^}]*\}", "", l).replace("\\N", " ")
                for l in a.splitlines() if l.startswith("Dialogue:")]
    check("Dialogue: 0,0:00:00.00,0:00:00.60,Cap,,0,0,0,,ONE TWO THREE" in _plain(ass),
          "shot-1 words group into one timed event")
    check("Dialogue: 0,0:00:02.92,0:00:03.80,Cap,,0,0,0,,FOUR FIVE SIX SEVEN" in _plain(ass),
          "shot-2 words are OFFSET by the shot start (3.0 s), shown 2 frames early")
    check(ass.count("Dialogue:") == 2, "3 words -> 1 event; 4 words -> 1 event (2 total)")
    groups = mod._group_words([{"word": w} for w in "a b c d e f g".split()])
    check(all(2 <= len(g) <= 4 for g in groups) and sum(len(g) for g in groups) == 7,
          "word grouping never leaves a 1-word tail: %r" % [[w["word"] for w in g] for g in groups])

    print("== camera moves on a shot ==")
    _s = {"prompt": "Gus sniffs the loaf."}
    mod.set_camera(_s, "Orbit")
    check(_s["prompt"].startswith("Gus sniffs the loaf. The camera orbits") and
          _s["camera_move"] == "Orbit", "a move's words go on the end of the render prompt")
    mod.set_camera(_s, "Handheld")
    check("orbits" not in _s["prompt"] and _s["prompt"].count("Handheld") == 1 and
          _s["prompt"].startswith("Gus sniffs the loaf."),
          "switching moves replaces the words, never stacks two (%r)" % _s["prompt"])
    mod.set_camera(_s, mod.CAMERA_NONE)
    check(_s["prompt"] == "Gus sniffs the loaf." and _s["camera_move"] is None,
          "No move takes them off again")

    print("== speaker -> character by first name ==")
    _ch = [("clarence-holt", "Clarence Holt"), ("gus", "Gustav"), ("mary-a", "Robin Ann"),
           ("mary-b", "Robin Beth")]
    check(mod.match_speaker("CLARENCE", _ch) == "clarence-holt" and
          mod.match_speaker("GUSTAV", _ch) == "gus" and mod.match_speaker("ROBIN", _ch) is None,
          "a first name matches one character; an ambiguous one matches none")

    print("== caption size per aspect ==")
    check(mod.caption_style(1080, 1920) == (96, 700),
          "vertical short: 96 px, block around y~1200 — clear of TikTok/Reels' own text and "
          "buttons (the old 240 px margin sat under them)")
    fs_w, mv_w = mod.caption_style(1920, 1080)
    check(fs_w >= 60 and mv_w < 120,
          "widescreen 1080p: big enough to read (%d px, was 43) and near the bottom (%d)"
          % (fs_w, mv_w))
    check(",%d,1" % mv_w in mod.build_ass([], width=1920, height=1080, font_size=fs_w,
                                           margin_v=mv_w).split("Style: Cap")[1].splitlines()[0],
          "the margin reaches the ASS style line")

    print("== captions for a shot with a line but NO aligned words ==")
    ass2 = mod.build_ass([
        {"start": 0.0, "words": [{"word": "one", "start": 0.0, "end": 0.2},
                                 {"word": "two", "start": 0.2, "end": 0.4}]},
        {"start": 8.0, "line": "Your ears are in my soup.", "voice_s": 2.0, "seconds": 3},
    ])
    ev2 = [l for l in ass2.splitlines() if l.startswith("Dialogue:")]
    ev2 = _plain(ass2)
    check(len(ev2) == 3 and " ".join(e.split(",,")[-1] for e in ev2[1:]) == "YOUR EARS ARE IN MY SOUP.",
          "a words-less shot still gets captions from its line (%r)" % ev2)
    check(ev2[1].split(",")[1] >= "0:00:07.90" and ev2[2].split(",")[2] <= "0:00:10.00",
          "...offset by the shot start and kept inside its 2.0 s voice (%r)" % ev2[1:])
    check(mod.build_ass([{"start": 0, "line": "", "seconds": 3}]).count("Dialogue:") == 0,
          "an empty line makes no caption event")

    print("== caption looks: Clean / Pop / Big ==")
    w3 = [{"start": 2.0, "words": [{"word": "expect", "start": 0.0, "end": 0.4},
                                   {"word": "forty", "start": 0.5, "end": 0.9},
                                   {"word": "{rain}", "start": 1.0, "end": 1.4}]}]
    def evs(look):
        return [l for l in mod.build_ass(w3, look=look).splitlines() if l.startswith("Dialogue:")]
    cl, po, bg = evs("clean"), evs("pop"), evs("big")
    check(len(cl) == 1 and _plain(cl[0])[0].endswith(",,EXPECT FORTY (RAIN)") and
          evs("odd") == cl,
          "Clean: one white group, braces can't break the ASS; unknown looks read as Clean")
    check(len(po) == 3 and all(e.count(mod.POP_COLOUR) == 1 for e in po) and
          po[1].split(",,")[-1].index(mod.POP_COLOUR) > po[1].split(",,")[-1].index("EXPECT"),
          "Pop: one event per word, the word being said (only it) in yellow")
    check([e.split(",")[1] for e in po] == ["0:00:01.92", "0:00:02.42", "0:00:02.92"] and
          po[0].split(",")[2] == po[1].split(",")[1] and po[-1].split(",")[2] == "0:00:03.40",
          "...each word holds until the next shows (no flicker), 2 frames ahead of its word, "
          "the last until the group ends")
    check(not any("\\fscx" in e.split(mod.POP_COLOUR)[1].split("}")[0] for e in po),
          "Pop grows the word TALLER only — growing wider shoved the line sideways")
    check(len(bg) == 3 and all(",Big," in e for e in bg) and bg[1].endswith("FORTY") and
          "Style: Big" in mod.build_ass(w3, look="big"),
          "Big: one word at a time in the big style")
    _wide = [{"start": 0.0, "cut_in": 0.0, "cut_out": 1.5, "words": [
        {"word": "personally", "start": 0.1, "end": 0.6}, {"word": "attacked", "start": 0.62,
                                                           "end": 1.0},
        {"word": "me,", "start": 1.02, "end": 1.4}]},
        {"start": 1.5, "words": [{"word": "Gerald.", "start": 0.05, "end": 0.5}]}]
    _v = mod.build_ass(_wide, width=1080, height=1920, font_size=96, margin_v=700, look="pop")
    _pv = _plain(_v)
    check(any("\\N" in l for l in _v.splitlines() if l.startswith("Dialogue")) and
          not any(",ME," in l or l.endswith("ME,") for l in _pv),
          "on a phone a wide phrase breaks onto two lines; Pop drops trailing commas")
    _t = [(l.split(",")[1], l.split(",")[2]) for l in _pv]
    check(all(_t[i][1] <= _t[i + 1][0] for i in range(len(_t) - 1)) and
          max(e for _b, e in _t[:-1]) <= "0:00:01.50",
          "no two captions on screen at once; a caption never runs past its shot's cut")
    _gap = [{"start": 0.0, "words": [{"word": "one", "start": 0.0, "end": 0.3},
                                     {"word": "two.", "start": 0.35, "end": 0.6}]},
            {"start": 0.0, "words": [{"word": "three", "start": 0.8, "end": 1.1},
                                     {"word": "four.", "start": 1.15, "end": 1.5}]}]
    _gp = _plain(mod.build_ass(_gap))
    check(_gp[0].split(",")[2] == _gp[1].split(",")[1],
          "a gap under 0.4 s: the caption holds until the next (no blink)")
    _h = mod.build_ass(_gap, width=1080, height=1920, font_size=96, margin_v=700,
                       look="pop", hook="He blames the fog on her pigeons")
    _hl = [l for l in _h.splitlines() if ",Hook," in l]
    check(len(_hl) == 1 and _hl[0].startswith("Dialogue: 0,0:00:00.00,0:00:02.00,Hook,") and
          "HE BLAMES THE FOG" in _hl[0] and "Style: Hook," in _h and
          len([l for l in _h.splitlines() if l.startswith("Dialogue")]) > 1,
          "the HOOK line sits on screen for the first 2 s, above the captions")
    _ho = mod.build_ass(_gap, hook="Only the hook", captions=False)
    check([l.split(",")[3] for l in _ho.splitlines() if l.startswith("Dialogue")] == ["Hook"],
          "captions off: the hook still shows, alone")
    _tight = [{"start": 0.0, "words": [{"word": "one", "start": 0.0, "end": 0.3},
                                       {"word": "two.", "start": 0.32, "end": 0.6}]},
              {"start": 0.0, "words": [{"word": "three", "start": 0.62, "end": 0.9},
                                       {"word": "four.", "start": 0.92, "end": 1.2}]}]
    _tp = _plain(mod.build_ass(_tight))
    check(_tp[0].split(",")[2] <= _tp[1].split(",")[1],
          "the next caption's early start never overlaps the one before (%r)" % _tp)
    check(mod.caption_look({}) == "clean" and mod.caption_look({"caption_look": "pop"}) == "pop"
          and mod.caption_look({"caption_look": "zz"}) == "clean",
          "an old project (no look saved) keeps the old Clean captions")
    ew = mod._even_words("a bb ccc", 3.0)
    check(ew[0]["start"] > 0 and ew[-1]["end"] <= 3.0 and
          all(ew[i]["end"] <= ew[i + 1]["start"] + 1e-6 for i in range(len(ew) - 1)),
          "even words are ordered and stay inside the duration (%r)" % ew)

    print("== finish() host choice ==")
    mod, fak, fau, fs = fresh()
    calls = []
    mod._run = lambda argv, timeout=600, cwd=None: calls.append(list(argv)) or types.SimpleNamespace(
        returncode=0, stdout="", stderr="")
    work = tempfile.mkdtemp(prefix="finish_")
    inp = os.path.join(work, "in.mp4")
    open(inp, "wb").write(b"x")
    assf = os.path.join(work, "c.ass")
    open(assf, "w").write("[Events]\n")
    outp = os.path.join(work, "out.mp4")
    mod.ffmpeg_has = lambda filt, ffmpeg=None: False       # the Mac lacks libass
    mod.finish(inp, assf, outp)
    joined = [" ".join(c) for c in calls]
    check(any(c.startswith("ssh ") for c in joined), "no 'ass' filter -> the burn runs on forge (ssh)")
    check(any("scp " in c and "captions.ass" in c for c in joined), "the ass file is scp'd up to forge")
    calls.clear()
    mod.ffmpeg_has = lambda filt, ffmpeg=None: True        # this ffmpeg can burn
    mod._finish_local = lambda i, a, o, loudnorm=True: o
    mod.finish(inp, assf, outp)
    check(not any(c[0] == "ssh" for c in calls), "with 'ass' present the burn stays LOCAL (no ssh)")

    print("== atomic save ==")
    mod, fak, fau, fs = fresh()
    pj = mod.new_project(["bighead"], "atomic test", length_s=6, quality="Draft")
    path = mod.project_path(pj["slug"])
    before = open(path, "rb").read()
    pj["bad"] = set([1, 2, 3])
    broke = False
    try:
        mod.save_project(pj)
    except Exception:
        broke = True
    check(broke, "saving unserializable data raises")
    try:
        after = open(path, "rb").read()
    except FileNotFoundError:
        after = None
    check(after == before, "the on-disk project is UNCHANGED after a failed save")
    leftovers = [f for f in os.listdir(mod.project_dir(pj["slug"])) if f.startswith(".project-")]
    check(not leftovers, "no temp file is left behind (%r)" % leftovers)

    print("== post_copy fallback ==")
    mod, fak, fau, fs = fresh()
    pj = two_shot_project(mod)
    pj["script"] = None
    ps = mod._post_script(pj)
    check(ps.get("hook") == pj["idea"] and ps.get("beats"),
          "a script-less project still gives post_copy the idea + shot beats")
    pj["script"] = {"title": "Real", "beats": [1]}
    check(mod._post_script(pj).get("title") == "Real",
          "an existing Director script is passed through unchanged")

    print("== 2-card (Raylight) opt-in ==")
    mod, fak, fau, fs = fresh()
    check(mod.project_migrate({}).get("gpus") == 1, "project_migrate backfills gpus=1")
    pj = two_shot_project(mod)
    check(pj.get("gpus") == 1, "an ordinary project is ONE card by default")
    check(mod.shot_params(pj, pj["shots"][0]).get("gpus") == 1,
          "shot_params sends gpus=1 by default (no regression for the app or the UI)")
    pj2 = mod.new_project(["bighead"], "two-card test", length_s=8, quality="Final", gpus=2)
    check(pj2["gpus"] == 2, "new_project(..., gpus=2) is stored on the project")
    check(mod.project_migrate(dict(pj2))["gpus"] == 2, "project_migrate KEEPS an explicit gpus=2")
    sh = {"n": 1, "seconds": 3, "prompt": "p", "video_mode": "r2v"}
    check(mod.shot_params(pj2, sh)["gpus"] == 2, "shot_params carries gpus=2 through to submit")
    sh2 = mod.shot_params(pj2, sh)
    check(sh2["quality"] == "Final" and (sh2["width"], sh2["height"]) == mod.CANVAS["Final"],
          "a 2-card Final project still uses the Final canvas")
    bad = False
    try:
        mod.new_project(["bighead"], "bad value", gpus=3)
    except ValueError:
        bad = True
    check(bad, "gpus=3 is REFUSED — never a silent step down to one card")
    # ⚠️ the load-bearing one: a 2-card render MUST free card 2 (the Director lives there),
    # and a 1-card render must not start touching the Director at all.
    released = []
    mod.release_gpu1_for_render = lambda *a, **k: (released.append(1), True)[1]
    pj2["shots"] = [mod._shot_migrate(dict(sh))]
    mod.save_project(pj2)
    mod.render_shot(pj2, 1)
    check(len(released) == 1, "a 2-card render RELEASES the Director off GPU1 before submitting")
    released[:] = []
    mod.render_all(pj)
    check(not released, "a 1-card render never touches the Director")

    # ---------------------------------------------------------------- §edits (bug fix 3)
    print("\n§edits a shot edit reaches the render prompt; a line edit is re-voiced")
    sh = mod._shot_migrate({"n": 1, "action": "he waves", "line": "hello",
                            "prompt": "summary: he waves at the camera.",
                            "audio_wav": "/x.wav", "words": [1]})
    ch = mod.apply_shot_edit(sh, "he trips over a hat", "hello")
    p = mod.shot_params({"slug": "s", "quality": "Draft"}, sh)["prompt"]
    check(ch == ["action"], "an action edit is reported as such (%r)" % ch)
    check("he trips over a hat" in p and "he waves" not in p,
          "the old action is REPLACED in the render prompt (%r)" % p)
    check(sh["audio_wav"] == "/x.wav", "an unchanged line keeps its audio")
    sh2 = mod._shot_migrate({"n": 2, "action": "x", "prompt": "summary: a wide shot of a shop."})
    mod.apply_shot_edit(sh2, "she juggles three loaves")
    p2 = mod.shot_params({"slug": "s", "quality": "Draft"}, sh2)["prompt"]
    check(p2.startswith("she juggles three loaves.") and "a wide shot of a shop" in p2,
          "an action not found verbatim LEADS the prompt, the rest kept (%r)" % p2)
    ch3 = mod.apply_shot_edit(sh, None, "goodbye")
    check(ch3 == ["line"] and sh["audio_wav"] is None and sh["words"] is None,
          "a line edit clears the audio so VOICE LINES redoes it")
    check(mod.apply_shot_edit(sh, sh["action"], sh["line"]) == [],
          "re-saving unchanged fields changes nothing")

    # ---------------------------------------------------------------- §episode
    print("\n== §episode: 16:9 aspect, captions, music, from_dialogue ==")
    import subprocess as _sp
    import shutil as _sh
    mod, fak, fau, fs = fresh()
    old = mod.project_migrate({"slug": "old"})
    check(old["aspect"] == "9:16" and mod.wants_captions(old),
          "a project made before episodes stays a captioned 9:16 short")
    ep = mod.new_project(["bighead"], "a wager at the bakery", length_s=30, quality="Final",
                         aspect="16:9")
    w, h = mod.canvas(ep)
    check(w > h and w % 32 == 0 and h % 32 == 0, "16:9 renders landscape, /32 (%dx%d)" % (w, h))
    p_ = mod.shot_params(ep, {"seconds": 3, "prompt": "p"})
    check((p_["width"], p_["height"]) == (w, h), "shot_params uses the episode canvas")
    check(not mod.wants_captions(ep), "an episode is uncaptioned by default")
    bad = False
    try:
        mod.new_project(["bighead"], "x", aspect="4:3")
    except ValueError:
        bad = True
    check(bad, "an unknown aspect is refused")

    if _sh.which("ffmpeg"):
        # two real clips + a real music bed; the cut must be 1920x1080 with sound, the caption
        # burn (finish) must NOT run, and no social post copy is written for an episode
        for i, n in enumerate((1, 2)):
            pid = "epclip%d" % n
            d_ = os.path.join(fc.RUNS, pid)
            os.makedirs(d_, exist_ok=True)
            _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                     "testsrc=size=960x544:rate=24", "-f", "lavfi", "-i",
                     "sine=frequency=%d" % (300 + 100 * i), "-t", "1.2", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-shortest", os.path.join(d_, "clip.mp4")], check=True)
        ep["shots"] = [mod._shot_migrate({"n": n, "seconds": 3, "chosen": "epclip%d" % n,
                                          "status": "done"}) for n in (1, 2)]
        music = os.path.join(_TMP, "bed.wav")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220",
                 "-t", "0.8", music], check=True)
        ep["music"] = music
        burned = []
        # a stand-in burn that still writes its output, so a wrong call is REPORTED by the
        # check below instead of crashing the cut further on
        mod.finish = lambda i_, a_, o_, **k: (burned.append(a_), _sh.copyfile(i_, o_))
        n_post = fak.calls.count("post_copy")
        cut = mod.assemble(ep)
        check(not burned, "an uncaptioned episode never runs the caption burn")
        check(os.path.exists(cut["mp4"]), "the 16:9 cut is written")
        prb = _sp.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                       "-of", "csv=p=0", cut["mp4"]], capture_output=True, text=True).stdout
        check("1920,1080" in prb or "video,1920,1080" in prb, "...at 1920x1080 (%r)" % prb.strip())
        check("audio" in prb, "...with sound (clips + music bed)")
        check(os.path.exists(os.path.join(mod.project_dir(ep["slug"]), "cut", "with_music.mp4")),
              "the music bed was mixed in")
        check(fak.calls.count("post_copy") == n_post and cut.get("post") is None,
              "no social post copy for an episode")
        # captions ON for the same episode: the burn gets an ASS sized for 16:9
        ep["captions"] = True
        ep["shots"][0]["line"] = "Your ears are in my soup."
        del burned[:]
        mod.assemble(ep)
        _ass = open(burned[0]).read() if burned else ""
        check("Style: Cap,%s,72," % mod.CAPTION_FONT in _ass and _ass.count("Dialogue:") >= 2,
              "a captioned 16:9 cut burns 72 px captions with the line in them")
        # a Dialogue-made shot (voice, no word times): the cut ALIGNS the line on the rig
        vw = os.path.join(_TMP, "v1.wav")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=200",
                 "-t", "1.0", vw], check=True)
        ep["shots"][0].update({"line": "Hello there.", "audio_wav": vw, "words": None})
        del burned[:]
        n_al = len(fau.aligns)
        mod.assemble(ep)
        _ass = open(burned[0]).read() if burned else ""
        check(len(fau.aligns) == n_al + 1 and fau.aligns[-1] == (vw, "Hello there.") and
              ",0:00:00.02," in _ass and "HELLO THERE." in _ass,
              "no word times -> the cut aligns the line to its voice; captions use the real "
              "times (%r)" % [l for l in _ass.splitlines() if l.startswith("Dialogue")][:2])
        ep["shots"][0]["words"] = None
        _orig_al = fau.align
        fau.align = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rig away"))
        del burned[:]
        mod.assemble(ep)
        fau.align = _orig_al
        _ass = open(burned[0]).read() if burned else ""
        check("HELLO THERE." in _ass, "...and if the rig can't align, captions still come "
                                      "from the line")

        print("== THE EDIT: trim to the words, fill the frame, move the camera ==")
        for n in (1, 2):
            d_ = os.path.join(fc.RUNS, "edclip%d" % n)
            os.makedirs(d_, exist_ok=True)
            _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                     "color=c=red:s=960x544:r=24:d=3", "-f", "lavfi", "-i",
                     "sine=frequency=300:d=3", "-shortest", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", os.path.join(d_, "clip.mp4")], check=True)
        ep["captions"] = False
        ep["shots"] = [
            mod._shot_migrate({"n": 1, "seconds": 3, "chosen": "edclip1", "status": "done",
                               "line": "Hello there.", "audio_wav": vw,
                               "words": [{"word": "Hello", "start": 0.5, "end": 0.9},
                                         {"word": "there.", "start": 0.95, "end": 1.3}]}),
            mod._shot_migrate({"n": 2, "seconds": 3, "chosen": "edclip2", "status": "done",
                               "line": "Goodbye.", "audio_wav": vw,
                               "words": [{"word": "Goodbye.", "start": 0.4, "end": 1.0}]})]
        _mixes, _runs = [], []
        _om, _or = mod._mix_audio, mod._run
        mod._mix_audio = lambda v, e, o, **k: (_mixes.append([dict(sh) for sh, _o in e]),
                                               _om(v, e, o, **k))[1]
        mod._run = lambda a, **k: (_runs.append(list(a)), _or(a, **k))[1]
        cut = mod.assemble(ep)
        mod._mix_audio, mod._run = _om, _or
        _mixcmd = " ".join(next((a for a in _runs if "adelay" in " ".join(a)), []))
        s1, s2 = ep["shots"]
        n1 = os.path.join(mod.project_dir(ep["slug"]), "cut", "n01.mp4")
        n2 = os.path.join(mod.project_dir(ep["slug"]), "cut", "n02.mp4")
        d1, d2 = mod._duration(n1), mod._duration(n2)
        check(abs(s1["trim_in"] - (0.5 - mod.HANDLE_IN)) < 0.01 and
              abs(s1["trim_out"] - (1.3 + mod.HANDLE_OUT + mod.PENULT_HOLD)) < 0.01 and
              abs(d1 - 1.62) < 0.1,
              "a line keeps its words + 4 frames before / 6 after, and the SETUP line (the "
              "one before the button) holds %.1f s more - 3 s clip -> %.2f s"
              % (mod.PENULT_HOLD, d1))
        check(abs(s2["trim_out"] - (1.0 + mod.BUTTON_HOLD)) < 0.01 and abs(d2 - 1.62) < 0.1,
              "the LAST line holds %.2f s after its last word for the reaction (%.2f s)"
              % (mod.BUTTON_HOLD, d2))
        check(mod._duration(cut["mp4"]) < 3.4,
              "the cut is the talking, not the dead air: 6 s of clips -> %.1f s"
              % mod._duration(cut["mp4"]))
        check(abs(s2["start"] - (s2["cut_in"] - s2["trim_in"])) < 0.01 and
              ("atrim=start=%.3f" % s2["trim_in"]) in _mixcmd and
              ("adelay=%d|" % int(round(s2["cut_in"] * 1000))) in _mixcmd,
              "voices and captions move with the trim: each voice is cut by its clip's trim "
              "and laid where the clip now starts (lip sync kept)")
        check(ep.get("whooshes") == [] and "volume=0.00" in _mixcmd,
              "SOUND: no whoosh by default (silence before the button is the hit), and the "
              "clip's own audio is muted under the voices (it doubled them)")
        _mx = os.path.join(mod.project_dir(ep["slug"]), "cut", "sound.mp4")
        _sil = _sp.run(["ffmpeg", "-hide_banner", "-nostats", "-i", _mx, "-af",
                        "silencedetect=noise=-70dB:d=0.2", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
        check(os.path.exists(_mx) and "silence_start" not in _sil,
              "SOUND: room tone runs under the whole cut — no dead silence between lines")
        check("color=pink" in mod.ambience("a busy city sidewalk") and
              "color=brown" in mod.ambience("a cramped kitchen") and
              "color=brown" in mod.ambience(""),
              "SOUND: an outdoor place gets an outdoor wash, a room gets room tone")
        _bed = os.path.join(fc.RUNS, "bed4.wav")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "sine=frequency=330:d=4", _bed], check=True)
        _quiet = os.path.join(fc.RUNS, "quiet4.mp4")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "color=c=black:s=64x64:r=24:d=2", "-f", "lavfi", "-i",
                 "anullsrc=r=44100:cl=stereo", "-t", "2", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 _quiet], check=True)
        _bm = mod.add_music(_quiet, _bed, os.path.join(fc.RUNS, "bm.mp4"), volume=1.0,
                            stop_at=0.5)
        _bv = _sp.run(["ffmpeg", "-v", "info", "-ss", "0.7", "-i", _bm, "-vn", "-af",
                       "volumedetect", "-f", "null", "-"], capture_output=True,
                      text=True).stderr
        _bmax = float(re.search(r"max_volume:\s*(-?[\d.]+)", _bv).group(1))
        check(_bmax < -25, "MUSIC: the bed cuts out on the button word (%.0f dB after)" % _bmax)
        check("linear=true" in mod.loudnorm_af(_mx) and "measured_I=" in mod.loudnorm_af(_mx),
              "loudness: a measured, two-pass loudnorm to -14 LUFS")
        px = _sp.run(["ffmpeg", "-v", "error", "-i", n1, "-vf", "crop=4:4:0:0,scale=1:1",
                      "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                     capture_output=True).stdout
        check(len(px) == 3 and px[0] > 150 and px[1] < 80,
              "the frame is FILLED to the corner — no black bars (corner rgb %r)" % list(px))
        check(s1["move"] in ("static", "push", "push_r") and s2["move"] == "punch" and
              "zoompan" in mod._move_filter("punch", 1920, 1080, 1.6, 0.3) and
              mod._move_filter("static", 1920, 1080, 1.6) == "",
              "camera: the last line punches in on its last word; the rest push or rest")
        _mv = mod.camera_moves({"shots": [{"n": i, "words": [1]} for i in range(1, 8)]})
        check(all(_mv[i] != _mv[i + 1] for i in range(1, 7)) and _mv[7] == "punch" and
              mod.camera_moves({"music_video": True, "shots": [{"n": 1}]}) == {1: "static"},
              "no two neighbouring shots move alike; a music video is left alone (%r)" % _mv)
        _shp = {"audio_wav": "x", "words": [{"start": 0.5, "end": 0.9},
                                           {"start": 0.95, "end": 1.3}]}
        _len = {p_: (lambda ab: round(ab[1] - ab[0], 2))(mod._trim_for(_shp, 3.0, pace=p_))
                for p_ in mod.PACES}
        check(_len["snappy"] < _len["natural"] < _len["breathe"] < _len["full"] == 3.0,
              "PACE: snappy < natural < let it breathe < keep every pause (%r)" % _len)
        _b = dict(_shp, direction="flat, then a beat")
        check(mod._trim_for(_b, 3.0)[1] - mod._trim_for(_shp, 3.0)[1] >= mod.PAUSE_HOLD - 0.01,
              "a line the writer marked 'beat' keeps a pause after it, even when snappy")
        # REACTION: the listener's face after the button
        d_ = os.path.join(fc.RUNS, "edreact")
        os.makedirs(d_, exist_ok=True)
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "color=c=blue:s=960x544:r=24:d=3", "-f", "lavfi", "-i",
                 "sine=frequency=500:d=3", "-shortest", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", os.path.join(d_, "clip.mp4")], check=True)
        _chars0 = ep.get("characters")
        ep["characters"] = ["ann", "bob"]
        ep["shots"][0]["speaker"], ep["shots"][1]["speaker"] = "ann", "bob"
        ep["shots"].append(mod._shot_migrate({"n": 3, "is_reaction": True, "speaker": "ann",
                                              "chosen": "edreact", "status": "done",
                                              "video_mode": "set"}))
        ep["shots"][0]["reaction"] = "rolls her eyes"           # acting text, not a flag
        mod.assemble(ep)
        _lst = open(os.path.join(mod.project_dir(ep["slug"]), "cut", "concat_list.txt")).read()
        _n3 = os.path.join(mod.project_dir(ep["slug"]), "cut", "n03.mp4")
        check(_lst.count("file ") == 3 and _lst.strip().splitlines()[-1].endswith("n03.mp4'")
              and abs(mod._duration(_n3) - mod.REACTION_HOLD) < 0.1 and
              abs(ep["shots"][1]["trim_out"] - (1.0 + mod.HANDLE_OUT)) < 0.01,
              "after the button the cut goes to the LISTENER's reaction for %.1f s (the "
              "speaker no longer holds)" % mod.REACTION_HOLD)
        check(mod.camera_moves(ep)[3] == "static" and len(mod.line_shots(ep)) == 2,
              "a reaction shot isn't a line: no camera move, not in the line list")
        # AT THE TURN: 4 lines -> a short glance at the listener mid-scene, then the button
        # reaction at the end (each its own segment and file)
        _e4 = {"slug": "turn4", "characters": ["ann", "bob"], "aspect": "9:16",
               "captions": False, "sound_design": False, "shots": [
                   mod._shot_migrate({"n": i + 1, "speaker": ("ann", "bob")[i % 2],
                                      "chosen": "edreact", "status": "done",
                                      "video_mode": "set"}) for i in range(4)] + [
                   mod._shot_migrate({"n": 5 + k, "is_reaction": True, "speaker": spk,
                                      "chosen": "edreact", "status": "done",
                                      "video_mode": "set"})
                   for k, spk in enumerate(("ann", "bob"))]}
        mod.assemble(_e4)
        _l4 = [ln.split("/")[-1].rstrip("'") for ln in open(os.path.join(
            mod.project_dir("turn4"), "cut", "concat_list.txt")).read().split("\n") if ln]
        check(_l4 == ["n01.mp4", "n02.mp4", "n105.mp4", "n03.mp4", "n04.mp4", "n05.mp4"] and
              abs(mod._duration(os.path.join(mod.project_dir("turn4"), "cut", "n105.mp4"))
                  - mod.CUTAWAY) < 0.1,
              "4 lines: a %.1f s glance at the LISTENER at the turn (ann, after bob's line 2), "
              "and the button reaction at the end (%r)" % (mod.CUTAWAY, _l4))
        _ep2 = {"characters": ["gus", "clara"], "shots": [
            mod._shot_migrate({"n": 1, "speaker": "gus", "video_mode": "set"}),
            mod._shot_migrate({"n": 2, "speaker": "clara", "video_mode": "set"})]}
        _added = mod.add_reactions(_ep2)
        check(_added == 2 and mod.add_reactions(_ep2) == 0 and
              [sh["speaker"] for sh in _ep2["shots"] if sh.get("is_reaction")] ==
              ["gus", "clara"] and all("mouth stays CLOSED" in sh["prompt"] and
                                       sh["intent"] != "lipsync" for sh in _ep2["shots"][2:]),
              "add_reactions: one silent clip per character, once")
        # LONG TAKES: one person -> lines run together in shots of up to ~15 s
        _lt_w = []
        for i_, d_s in enumerate((2.0, 3.0, 2.5, 4.0, 5.5)):
            w_ = os.path.join(fc.RUNS, "lt%d.wav" % i_)
            _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=%d:sample_rate=24000:d=%.1f" % (300 + 50 * i_, d_s),
                     "-ac", "1", w_], check=True)
            _lt_w.append(w_)
        _lt = {"slug": "longtake", "characters": ["gus"], "shots": [
            mod._shot_migrate({"n": i_ + 1, "speaker": "gus", "video_mode": "r2v",
                               "line": "Line %d." % (i_ + 1), "audio_wav": w_,
                               "gesture": "g%d" % (i_ + 1), "seconds": 5,
                               "prompt": "Gus talks."})
            for i_, w_ in enumerate(_lt_w)]}
        _made = mod.merge_long_takes(_lt)
        _t1 = _lt["shots"][0]
        _lens = [round(mod._duration(sh["audio_wav"]), 1) for sh in _lt["shots"]]
        check(_made >= 1 and len(_lt["shots"]) == 2 and _t1["long_take"] == 4 and
              _t1["line"] == "Line 1. Line 2. Line 3. Line 4." and
              _t1["line_starts"] == [0.0, 2.3, 5.6, 8.4] and
              abs(_lens[0] - (2 + 3 + 2.5 + 4 + 3 * mod.LONG_TAKE_GAP)) < 0.15 and
              _lt["shots"][1]["line"] == "Line 5." and
              [sh["n"] for sh in _lt["shots"]] == [1, 2],
              "long takes: 5 lines -> one %.1f s take (4 lines, one stitched voice) + the "
              "line that didn't fit (%r)" % (_lens[0], _lens))
        check(8 < _t1["seconds"] <= 15 and fl.h3_frames(_t1["seconds"]) <= 362 and
              "g1; then g2; then g3; then g4" in _t1["prompt"] and _t1["words"] is None
              and _t1["status"] == "new",
              "the take is long enough for its voice, legal for H3, and acts every line in "
              "order (%.2f s)" % _t1["seconds"])
        check(mod.camera_moves(_lt)[1] == "jumps" and
              "gte(in_time,2.300)" in mod._move_filter("jumps", 704, 1280, 12, None,
                                                      jumps=[2.3, 5.6]),
              "a long take jump-zooms at each new line")
        _jo = os.path.join(fc.RUNS, "jumps.mp4")
        mod._normalise(os.path.join(d_, "clip.mp4"), _jo, w=352, h=640, dur=3.0,
                       move="jumps", punch_at=2.5, jumps=[1.0, 2.0])
        check(abs(mod._duration(_jo) - 3.0) < 0.15,
              "...and ffmpeg renders it (jumps at 1 s and 2 s, punch at 2.5 s)")
        # the punch-in lands in TRIMMED time (it fired trim_in seconds early)
        _ts = os.path.join(fc.RUNS, "ts6.mp4")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "testsrc=s=320x568:r=24:d=6", "-pix_fmt", "yuv420p", _ts], check=True)

        def _fr(path, t):
            return _sp.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % t, "-i", path,
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                           capture_output=True).stdout
        _st = mod._normalise(_ts, os.path.join(fc.RUNS, "pt_s.mp4"), w=320, h=568, ss=2.0,
                             dur=2.0, move="static")
        _pu = mod._normalise(_ts, os.path.join(fc.RUNS, "pt_p.mp4"), w=320, h=568, ss=2.0,
                             dur=2.0, move="punch", punch_at=1.0)

        def _diff(a, b):
            return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))
        check(_diff(_fr(_st, 0.5), _fr(_pu, 0.5)) < 2 and
              _diff(_fr(_st, 1.5), _fr(_pu, 1.5)) > 8,
              "the punch-in lands at its word in the TRIMMED clip, not trim_in early "
              "(before %.1f, after %.1f)" % (_diff(_fr(_st, 0.5), _fr(_pu, 0.5)),
                                             _diff(_fr(_st, 1.5), _fr(_pu, 1.5))))
        # the voice is trimmed at BOTH ends: a short-aligned line can't spill over the next
        _v4 = os.path.join(fc.RUNS, "v4.mp4")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "color=c=black:s=64x64:r=24:d=4", "-f", "lavfi", "-i",
                 "anullsrc=r=44100:cl=stereo", "-t", "4", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 _v4], check=True)
        _vw = os.path.join(fc.RUNS, "v3.wav")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "sine=frequency=440:d=3:sample_rate=24000", _vw], check=True)
        _mx = mod._mix_audio(_v4, [({"audio_wav": _vw, "trim_in": 0.5, "trim_out": 1.5}, 0.0)],
                             os.path.join(fc.RUNS, "mx.mp4"))
        _vd = _sp.run(["ffmpeg", "-v", "info", "-ss", "1.3", "-i", _mx, "-vn", "-af",
                       "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
        _mxv = float(re.search(r"max_volume:\s*(-?[\d.]+|-inf)", _vd).group(1)
                     .replace("-inf", "-200"))
        check(_mxv < -50, "the voice stops where the clip was cut (%.0f dB after it)" % _mxv)
        # the caption font ships with the app and is handed to libass
        _cf = mod.caption_fonts()
        _ass = mod.build_ass([{"n": 1, "line": "hi there", "start": 0.0, "cut_in": 0.0,
                               "cut_out": 1.0, "words": [{"word": "hi", "start": 0.1,
                                                          "end": 0.4}]}], 704, 1280,
                             mod.CAPTION_FONT, 96, 700, "clean")
        _cap_d = os.path.join(fc.RUNS, "capburn")
        os.makedirs(_cap_d, exist_ok=True)
        _ci = os.path.join(_cap_d, "in.mp4")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "color=c=black:s=352x640:r=24:d=1", "-f", "lavfi", "-i", "anullsrc", "-t",
                 "1", "-pix_fmt", "yuv420p", _ci], check=True)
        open(os.path.join(_cap_d, "c.ass"), "w").write(_ass)
        mod._finish_local(_ci, os.path.join(_cap_d, "c.ass"), os.path.join(_cap_d, "o.mp4"),
                          loudnorm=False)
        check([os.path.basename(x) for x in _cf] == ["Inter-Black.otf"] and
              ",Inter Black," in _ass and
              os.path.exists(os.path.join(_cap_d, "fonts", "Inter-Black.otf")) and
              os.path.exists(os.path.join(_cap_d, "o.mp4")),
              "captions use the font shipped with the app (Inter Black, OFL), handed to libass")
        _sa = mod.build_ass([], 704, 1280, mod.CAPTION_FONT, 96, 700, "clean", captions=False,
                            stamp="FRONT DOOR")
        check("Style: Stamp," in _sa and ",Stamp,,0,0,0,,FRONT DOOR" in _sa and
              mod.camera_moves({"post_look": "cctv", "shots": [
                  {"n": i, "words": [1]} for i in (1, 2, 3)]}) == {1: "static", 2: "static",
                                                                   3: "static"},
              "the doorbell-camera look: a camera label top-left all along, the camera never "
              "moves")
        _cc = mod._normalise(_ts, os.path.join(fc.RUNS, "cc.mp4"), w=320, h=568, dur=1.0,
                             look="cctv")
        _pl = mod._normalise(_ts, os.path.join(fc.RUNS, "cc0.mp4"), w=320, h=568, dur=1.0)
        check(abs(mod._duration(_cc) - 1.0) < 0.1 and
              _diff(_fr(_cc, 0.5), _fr(_pl, 0.5)) > 8,
              "...and its picture (wide-angle warp, washed colour, noise) renders")
        _two = {"slug": "lt2", "characters": ["gus", "clara"], "shots": [
            mod._shot_migrate({"n": i_ + 1, "speaker": ("gus", "clara")[i_ % 2],
                               "audio_wav": w_}) for i_, w_ in enumerate(_lt_w[:3])]}
        _mv0 = mod.merge_long_takes({"slug": "lt3", "music_video": True, "characters": ["gus"],
                                     "shots": [dict(x) for x in _two["shots"]]})
        check(mod.merge_long_takes(_two) == 0 and len(_two["shots"]) == 3 and _mv0 == 0,
              "two people (or a music video) stay one line per shot")
        check(mod._fit_seconds(2.4) == 3 and mod._fit_seconds(7.9) == 8 and
              mod._fit_seconds(10.0) == 10.5 and mod._fit_seconds(30) == 15.0,
              "a single line over 8 s gets a shot long enough for it, up to 15 s")
        _cd0 = mod._char_dict
        _got = []
        for _f in ("a neck as long as a giraffe's", "hands the size of dinner plates",
                   "eyes set impossibly far apart", "an enormous wild mane of hair"):
            mod._char_dict = lambda s_, f_=_f: {"feature": f_}
            _got.append(mod._gag("x")[1])
        mod._char_dict = _cd0
        check(_got == ["neck", "hands", "eyes", "hair"],
              "the gag's noun is the BODY PART ('giraffe's', 'size', 'apart' before) %r" % _got)
        _r2 = {"characters": ["gus"], "shots": [mod._shot_migrate(
            {"n": 1, "speaker": "gus", "video_mode": "r2v", "prompt": "Gus talks."})]}
        mod.apply_acting(_r2, [{"gesture": "taps the desk"}])
        mod.apply_acting(_r2, [{"gesture": "shrugs"}])
        check(_r2["shots"][0]["prompt"] == "Gus talks. While speaking: shrugs.",
              "an r2v shot gets its line's gesture too (once, replaced on a redo)")
        check("straight to the camera" in mod.set_action({}, "gus", [], {"n": 2}),
              "one person on a set talks to the camera, not to 'the other person'")
        # TAKE CHECK: flag bad takes for Ryan (never rerolled on its own)
        def _take(pid, src):
            os.makedirs(os.path.join(fc.RUNS, pid), exist_ok=True)
            _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-pix_fmt",
                     "yuv420p", os.path.join(fc.RUNS, pid, "clip.mp4")], check=True)
        _take("qcgood", "testsrc=s=320x568:r=24:d=%.3f" % (fl.h3_frames(3) / 24.0))
        _take("qcfrozen", "color=c=0x406080:s=320x568:r=24:d=%.3f" % (fl.h3_frames(3) / 24.0))
        _take("qcshort", "testsrc=s=320x568:r=24:d=1")
        _take("qcblack", "testsrc=s=320x568:r=24:d=1.5[a];color=c=black:s=320x568:r=24:"
                         "d=1.6[b];[a][b]concat=n=2:v=1:a=0")
        _qp = {"slug": "qcproj", "characters": ["gus", "clara"], "shots": [
            mod._shot_migrate({"n": i_ + 1, "speaker": "gus", "seconds": 3, "chosen": pid_,
                               "video_mode": "set"})
            for i_, pid_ in enumerate(("qcgood", "qcfrozen", "qcshort", "qcblack"))]}
        _fl = mod.qc_shots(_qp)
        _why = {sh["n"]: " ".join(sh["qc"]["flags"]) for sh in _qp["shots"]}
        check(_fl == [2, 3, 4] and not _why[1] and "hardly moves" in _why[2] and
              "stops early" in _why[3] and "goes black" in _why[4] and
              all(os.path.exists(sh["qc"]["frame"] or "") for sh in _qp["shots"]),
              "take check: a frozen, a short and a black take are flagged; a good one isn't "
              "(%r)" % _why)

        class _QCDir:
            calls = []

            def qc_frame(self, frame, still, expect):
                self.calls.append(expect)
                return {"verdict": "drift", "reasons": ["two copies of Gus"]} \
                    if len(self.calls) == 1 else {"verdict": "ok", "reasons": []}
        _qd = _QCDir()
        _qp["shots"][0]["qc"] = None
        _qp["shots"][1]["qc"]["ok_by_you"] = True
        fc.character_still = (lambda s_, _o=fc.character_still: _o(s_) or
                              os.path.join(fc.RUNS, "qcproj_still.png"))
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                 "-frames:v", "1", os.path.join(fc.RUNS, "qcproj_still.png")], check=True)
        _fl2 = mod.qc_shots(_qp, _qd)
        check(_fl2 == [1, 3, 4] and len(_qd.calls) == 1 and
              "something looks off: two copies of Gus" in _qp["shots"][0]["qc"]["flags"] and
              "alone in a close-up" not in _qd.calls[0] and "exactly 2 people" in _qd.calls[0],
              "the vision read flags what it sees; checked takes aren't re-read; 'it's fine' "
              "takes stay unflagged")

        class _NoVision:
            n = 0

            def qc_frame(self, *a):
                _NoVision.n += 1
                raise RuntimeError("model has no vision")
        for sh in _qp["shots"]:
            sh["qc"] = None
        mod.qc_shots(_qp, _NoVision())
        check(_NoVision.n == 1 and "hardly moves" in " ".join(_qp["shots"][1]["qc"]["flags"]),
              "no vision on the rig -> asked once, then the local checks carry on")
        check("alone in a close-up" in mod._qc_expect(_qp, dict(_qp["shots"][1], n=2)) and
              "mouth closed" in mod._qc_expect(_qp, {"n": 5, "speaker": "gus",
                                                     "is_reaction": True}),
              "a close-up must show one person; a reaction, a closed mouth")
        ep["shots"] = ep["shots"][:2]
        ep["characters"] = _chars0
        ep["pace"] = "full"
        mod.assemble(ep)
        check(mod._duration(n1) > 2.8 and ep["shots"][0]["trim_in"] == 0,
              "Keep every pause: nothing trimmed")
        ep["pace"] = "snappy"
        ep["shots"][0]["words"] = None
        ep["shots"][0]["audio_wav"] = None
        mod.assemble(ep)
        check(ep["shots"][0]["trim_in"] == 0 and mod._duration(n1) > 2.8,
              "a shot with no voice (b-roll) keeps its whole clip")

    _g = [{"word": w, "start": i, "end": i + .5}
          for i, w in enumerate(["clouds", "dont", "pay", "taxes", "gerald"])]
    check([w["word"] for w in mod._with_line_words(_g, "Clouds don't pay taxes, Gerald.")] ==
          ["Clouds", "don't", "pay", "taxes,", "Gerald."] and
          mod._with_line_words(_g, "Clouds don't pay taxes, Gerald.")[3]["start"] == 3,
          "aligned words get the script's spelling and punctuation back, times kept "
          "(the second run showed 'RENTFREE')")
    print("== captions read as phrases (first real run cut 'YOUR EAR IS | DRIPPING...') ==")
    def chunks(line):
        return [" ".join(w["word"] for w in g)
                for g in mod._group_words(mod._even_words(line, 4.0))]
    _lines = ["You are breathing directly into my consommé, and your ear is dripping "
              "condensation.", "Expect a forty percent chance of personal space violations "
              "within the next minute.", "And that chin is currently predicting a soup "
              "explosion.", "It is not condensation, it is a high-pressure system weeping for "
              "you.", "Good evening."]
    _all = [chunks(l) for l in _lines]
    _ends = [c.split()[-1].lower() for cs in _all for c in cs
             if not c.split()[-1].endswith((",", ".", "!", "?"))]
    check(not [e for e in _ends if e in mod._WEAK],
          "no caption ends on the/a/is/that/within... (%r)" % _all)
    _adj = [chunks(l) for l in ("Your enormous wet ears keep stealing my delicious tomato "
                                "soup.", "We found a tiny angry pigeon living inside the "
                                "broken vending machine.")]
    _torn = [c for cs in _adj for c in cs if c.split()[-1].lower() in
             ("enormous", "wet", "delicious", "tiny", "angry", "broken")]
    check(not _torn and mod._two_lines(["your", "big", "wet", "ears", "dripping"], 16) == 4,
          "an adjective stays with its noun ('ENORMOUS | WET EARS' was torn) (%r)" % _adj)
    check(all(len(c) <= mod._CAP_CHARS + 6 for cs in _all for c in cs) and
          not [c for cs in _all for c in cs if len(c.split()) == 1],
          "every caption is short enough to read at a glance, none a lonely word")
    check(all(" ".join(cs) == l for cs, l in zip(_all, _lines)) and
          chunks("Good evening.") == ["Good evening."] and
          "It is not condensation," in _all[3],
          "...every word kept, in order; a comma ends a caption")
    if _sh.which("ffmpeg"):
        sw = os.path.join(_TMP, "sp.wav")
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                 "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=24000", "-filter_complex",
                 "[0]atrim=0:0.5[a];[1]atrim=0:2.0[b];[0]atrim=0:0.6[c];"
                 "[a][b][c]concat=n=3:v=0:a=1", sw], check=True)
        ew = mod._even_words("Good evening, feelings fans.", 3.1, wav=sw)
        check(abs(ew[0]["start"] - 0.5) < 0.05 and abs(ew[-1]["end"] - 2.5) < 0.05,
              "unaligned captions start when the voice starts and end when it stops "
              "(%.2f-%.2f)" % (ew[0]["start"], ew[-1]["end"]))
    else:
        check(False, "ffmpeg is needed for the 16:9 cut check")

    # from_dialogue: a voiced scene -> lip-sync shots with the scene's own takes
    import dialogue as _dlg
    fl.write_character("avery", {"name": "Avery", "prompt": "a man", "picks": {}})
    dpj = _dlg.new_project("Bakery", "AVERY: Twelve kronor?\nBAKER: Eleven, then.\n"
                                     "AVERY: Fine, fine, fine, I shall pay it gladly and go.")
    pdir = os.path.join(_dlg.dialogue_dir(), dpj["slug"])
    os.makedirs(os.path.join(pdir, "lines"), exist_ok=True)
    for li, secs in zip(dpj["lines"], (2.2, None, 6.0)):     # line 2 has no take yet
        if secs is None:
            continue
        rel = "lines/%03d_0.wav" % li["idx"]
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330",
                 "-t", str(secs), os.path.join(pdir, rel)], check=True)
        li["takes"] = [{"file": rel}]
        li["kept"] = 0
    _dlg.save_project(dpj)
    mod, fak, fau, fs = fresh()
    fe = mod.from_dialogue(dpj["slug"])
    check(fe["aspect"] == "16:9" and fe["characters"] == ["avery"],
          "from_dialogue makes a 16:9 episode cast with the matched character (%r)"
          % fe["characters"])
    check(len(fe["shots"]) == 2 and fe["source"]["skipped_lines"] == [1],
          "one shot per VOICED line; the unvoiced line is reported (%r)"
          % fe["source"]["skipped_lines"])
    check([s["seconds"] for s in fe["shots"]] == [3, 8],
          "each shot is long enough for its line (2.2 s -> 3, 6 s -> 8): %r"
          % [s["seconds"] for s in fe["shots"]])
    check(all(s["intent"] == "lipsync" and os.path.exists(s["audio_wav"]) for s in fe["shots"]),
          "every shot lip-syncs to the line's own take")
    n_tts = len(fau.calls) if hasattr(fau, "calls") else None
    mod.voice_lines(fe)
    check(all(s["audio_wav"].startswith(pdir) for s in fe["shots"]),
          "VOICE LINES keeps the Dialogue takes (does not re-voice them)")
    nomatch = False
    try:
        mod.from_dialogue(dpj["slug"], cast={"AVERY": None})
        fl.delete_character("avery")
        mod.from_dialogue(dpj["slug"])
    except ValueError:
        nomatch = True
    check(nomatch, "no matching character -> a clear refusal, not a castless episode")

    print("\n== THE SET: one place for a two-hander ==")
    import dialogue as _dlg2
    fl.write_character("clara", {"name": "Clara", "prompt": "x", "feature": "ears to her chin",
                                 "picks": {}})
    fl.write_character("gus", {"name": "Gus", "prompt": "He is old. He wears a black tailcoat "
                                                       "and white gloves. Wild hair.",
                               "picks": {"Feature": "wild hair"}})
    dpj2 = _dlg2.new_project("Weather", "GUS: Your ears are in my soup.\n"
                                        "CLARA: That's the cold front.\nGUS: No. Again.")
    dpj2["where"] = "a rooftop weather deck"
    pdir2 = os.path.join(_dlg2.dialogue_dir(), dpj2["slug"])
    os.makedirs(os.path.join(pdir2, "lines"), exist_ok=True)
    for li in dpj2["lines"]:
        rel = "lines/%03d_0.wav" % li["idx"]
        _sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330",
                 "-t", "1.5", os.path.join(pdir2, rel)], check=True)
        li["takes"] = [{"file": rel}]
        li["kept"] = 0
    _dlg2.save_project(dpj2)
    mod, fak, fau, fs = fresh()
    stills = {}
    for slug in ("clara", "gus"):
        stills[slug] = os.path.join(_TMP, "still_%s.png" % slug)
        open(stills[slug], "wb").write(b"\x89PNG\r\n\x1a\n" + slug.encode())
    fc.character_still = lambda slug: stills.get(slug)
    edits = []

    def fake_submit(mode, p, ref_paths=None):
        if mode != "edit":
            return fs(mode, p, ref_paths)
        edits.append({"p": dict(p), "refs": list(ref_paths or [])})
        pid = "edit%02d" % len(edits)
        os.makedirs(os.path.join(fc.RUNS, pid), exist_ok=True)
        open(os.path.join(fc.RUNS, pid, "o.png"), "wb").write(b"\x89PNG" + pid.encode())
        return pid, 77

    def fake_wait(pid, **kw):
        if pid.startswith("edit"):
            return {"id": pid, "images": [{"file": "o.png"}]}
        return fs.wait_and_collect(pid)
    fc.submit = fake_submit
    fc.wait_and_collect = fake_wait
    ep2 = mod.from_dialogue(dpj2["slug"])
    check(ep2["characters"] == ["gus", "clara"] and
          all(s["video_mode"] == "set" for s in ep2["shots"]),
          "two characters -> every shot starts from THE SET (%r)"
          % [s["video_mode"] for s in ep2["shots"]])
    a2 = ep2["shots"][1]["prompt"]
    a1 = ep2["shots"][0]["prompt"]
    check(a1.startswith("Gus talks to Clara") and "Clara listens and visibly reacts" in a1 and
          a2.startswith("Clara, alone in the frame, talks to Gus just off-screen") and
          "only Clara's lips move" in a2 and "not the lens" in a2 and
          "stranger" not in a2 and "the other character" not in a2,
          "the wide: one talks, the other listens and reacts; a close-up: the speaker alone, "
          "eyes on the other person off-screen (%r)" % a2[:90])
    check("holds still" not in a1 + a2 and "Small natural gestures" not in a1 + a2,
          "no more 'small natural gestures, the camera holds still' (everyone stood like "
          "statues)")
    mod.apply_acting(ep2, [{"gesture": "jabs a finger at the sky", "reaction": "slowly "
                            "narrows her eyes"}, {"gesture": "clutches a pigeon",
                                                  "reaction": "steps back"}])
    check("While speaking, Gus jabs a finger at the sky." in ep2["shots"][0]["prompt"] and
          "Clara listens and reacts: slowly narrows her eyes." in ep2["shots"][0]["prompt"]
          and "While speaking, Clara clutches a pigeon." in ep2["shots"][1]["prompt"] and
          ep2["shots"][2]["gesture"] == "",
          "the writer's acting goes into each shot: the speaker's gesture, and on the wide "
          "the listener's reaction; a line without one keeps the generic note")
    a2 = ep2["shots"][1]["prompt"]
    check("Clara's ears and Gus's hair stay exactly as large" in a2,
          "...and names each gag to keep (the second run's nose faded out mid-shot)")
    check(ep2.get("where") == "a rooftop weather deck" and
          mod.set_where(ep2) == "a rooftop weather deck",
          "the scene's place carries into the episode")
    check(mod.shot_params(ep2, ep2["shots"][0])["video_mode"] == "i2v",
          "a set shot renders as i2v (its plate is the first frame)")
    check(not mod.set_ready(ep2), "a new episode has no set yet")
    refused = False
    try:
        mod._render_refs(ep2, ep2["shots"][0])
    except ValueError as exc:
        refused = "set isn't built" in str(exc)
    check(refused, "rendering a set shot before the set is built is a clear refusal")
    built = mod.make_set(ep2)
    check(len(edits) == 4, "the set = 2 passes for the wide + 1 single per speaker (%d edits)"
          % len(edits))
    check(edits[0]["refs"] == [stills["gus"]] and edits[1]["refs"][1:] == [stills["clara"]],
          "ONE PERSON PER PASS: Gus placed from his still, then Clara added from hers (%r)"
          % [e["refs"] for e in edits[:2]])
    p1, p2 = edits[0]["p"]["prompt"], edits[1]["p"]["prompt"]
    check("rooftop weather deck" in p1 and "wild hair" in p1 and "black tailcoat" in p1 and
          "ears to her chin" not in p1,
          "pass 1 names Gus's gag and clothes, and nothing of Clara's")
    check("keep it EXACTLY" in p2 and "ears to her chin" in p2 and "borrows NOTHING" in p2 and
          "Only Gus wears a black tailcoat" in p2 and
          "Only Gus has the exaggerated hair; Clara's hair is ordinary, exactly as in <image2>"
          in p2 and
          "Only Clara has the exaggerated ears; Gus's ears are ordinary, exactly as in <image1>"
          in p2,
          "pass 2 adds Clara, keeps the plate, and names whose gag and clothes are whose "
          "(run-through: Gustav got Clarence's ears)")
    fl.write_character("tom", {"name": "Tom", "prompt": "x", "picks": {},
                               "feature": "ears to his chest like heavy wet towels"})
    check(mod._gag("clara") == ("ears to her chin", "ears") and
          mod._gag("tom")[0] == "ears to his chest" and "towel" not in mod._char_desc("tom")[1],
          "a gag's simile is dropped for the set ('like heavy wet towels' drew a towel)")
    check(edits[1]["refs"][0].endswith("master_1.png") and built["master"].endswith("master.png"),
          "pass 2 builds on pass 1's plate")
    check([e["refs"] for e in edits[2:]] == [[built["master"], stills["gus"]],
                                              [built["master"], stills["clara"]]] and
          edits[0]["p"]["custom_size"] and edits[0]["p"]["width"] == 1280,
          "each close-up: place from the wide, face and clothes from THEIR OWN still (16:9)")
    check("Only Clara has the exaggerated ears; Gus's ears are ordinary" in
          edits[2]["p"]["prompt"] and "exactly the face, hair, ears and clothes from <image2>"
          in edits[2]["p"]["prompt"], "...and Gus's close-up says the big ears aren't his")
    check(all("ALONE" in e["p"]["prompt"] and "Nobody else is in the frame" in e["p"]["prompt"]
              and "shoulder at the edge" not in e["p"]["prompt"] and "85 mm" in e["p"]["prompt"]
              for e in edits[2:]) and "28 mm" in edits[0]["p"]["prompt"] and
          "looking screen-right" in edits[2]["p"]["prompt"] and
          "looking screen-left" in edits[3]["p"]["prompt"],
          "close-ups are CLEAN singles (nobody else, long lens) looking toward the other's "
          "side; the wide is a deep-focus 28 mm (Vivian appeared twice in Gerald's)")
    check(os.path.exists(built["master"]) and set(built["singles"]) == {"gus", "clara"}
          and mod.set_ready(ep2), "the plates are on disk and the set reads as ready")
    check(mod._render_refs(ep2, ep2["shots"][0]) == [built["master"]] and
          mod._render_refs(ep2, ep2["shots"][1]) == [built["singles"]["clara"]] and
          mod._render_refs(ep2, ep2["shots"][2]) == [built["singles"]["gus"]],
          "shot 1 opens on the wide; later shots start on the SPEAKER's single")
    check(mod.speaker_x(ep2, ep2["shots"][0]) < 0.4 and
          mod.speaker_x(ep2, dict(ep2["shots"][1], n=1)) > 0.6 and
          mod.speaker_x(ep2, ep2["shots"][1]) == 0.5 and
          mod.speaker_x(ep2, {"video_mode": "r2v"}) == 0.5,
          "on the wide a push / punch-in aims at the speaker's side (Gus left, Clara right); "
          "a close-up stays centred")
    # VARY SIZES: a longer two-hander goes back to the wide once, mid-scene
    _v6 = {"characters": ["gus", "clara"], "shots": [
        mod._shot_migrate({"n": i + 1, "speaker": ("gus", "clara")[i % 2], "video_mode": "set",
                           "line": "Line %d." % (i + 1)}) for i in range(6)]}
    _wn = mod.vary_sizes(_v6)
    _wsh = [sh for sh in _v6["shots"] if sh.get("wide")]
    check(_wn in (3, 4) and len(_wsh) == 1 and mod.set_plate(ep2, dict(_wsh[0], n=_wn))
          == built["master"] and "alone in the frame" not in _wsh[0]["prompt"] and
          "listens" in _wsh[0]["prompt"] and mod.vary_sizes(_v6) is None,
          "6 lines: one mid-scene line goes back to the WIDE (both in frame, the other "
          "listens), once (shot %r)" % _wn)
    _v4 = {"characters": ["gus", "clara"], "shots": [
        mod._shot_migrate({"n": i + 1, "speaker": ("gus", "clara")[i % 2], "video_mode": "set"})
        for i in range(4)]}
    _v1 = {"characters": ["gus"], "shots": [
        mod._shot_migrate({"n": i + 1, "speaker": "gus", "video_mode": "set"})
        for i in range(6)]}
    check(mod.vary_sizes(_v4) is None and mod.vary_sizes(_v1) is None,
          "...not in a short scene, nor with one person")
    check(mod.speaker_x(ep2, dict(ep2["shots"][1], wide=True)) > 0.6,
          "...and on that wide the punch-in aims at the speaker")
    n0 = len(fs.calls)
    mod.render_shot(ep2, 2)
    call = fs.calls[n0]
    _ip = call["p"]["prompt"]
    check(call["p"]["video_mode"] == "i2v" and call["refs"] == [built["singles"]["clara"]]
          and "is the character" not in _ip
          and call["p"].get("guide_audio_path"),
          "the render: i2v from Clara's single, her line as the lip-sync guide")
    check(_ip.startswith(mod.I2VA_LINE + "\n\nintegrated_multimodal_description: [Shot 1] ")
          and "begins exactly on <Picture 1>" in _ip
          and ep2["shots"][1]["prompt"].strip().rstrip(".") in _ip
          and "(S1) says: <d>[English] " in _ip
          and "No on-screen text" in _ip
          and "\n\noverall_soundscape: " in _ip and "\n\nnon_diegetic_music: N/A" in _ip,
          "a set shot is SENT as I2VA (MiniMax base guide): the first-frame line, the action "
          "kept, the line in <d>, the don'ts, and the sound written")
    check(mod.H3_DEFAULT_CAMERA in _ip, "no camera chip -> a written steady move, not nothing")
    _sh = dict(ep2["shots"][1])
    mod.set_camera(_sh, "Slow push-in")
    check(mod.H3_DEFAULT_CAMERA not in mod.h3_prompt(ep2, _sh, "i2v"),
          "a camera chip replaces the default move (never two moves)")
    _t = mod.h3_prompt(ep2, dict(ep2["shots"][1]), "t2v")
    check(_t.startswith("integrated_multimodal_description: [Shot 1] ") and "<Picture" not in _t,
          "t2v is SENT as T2VA: no first-frame line, no picture")
    _c = mod.h3_prompt(ep2, dict(ep2["shots"][1]), "cont")
    check(_c.startswith(mod.I2VA_LINE) and "last frame of the previous shot" in _c,
          "cont is SENT as I2VA from the previous clip's last frame")
    _r = mod.h3_prompt(ep2, dict(ep2["shots"][1], is_reaction=True, line="hello"), "i2v")
    check("<d>" not in _r and "No one speaks." in _r, "a reaction has no line and says no one speaks")
    _w = "integrated_multimodal_description: [Shot 1] hand written.\n\noverall_soundscape: x"
    check(mod.h3_prompt(ep2, dict(ep2["shots"][1], prompt=_w), "i2v") == _w,
          "a prompt already in a documented format is sent unchanged")
    # RESUME: a busy card mid-set no longer redoes every pass
    n0 = len(edits)
    mod.make_set(ep2, resume=True)
    check(len(edits) == n0, "resume with a current set -> nothing redone")
    os.remove(ep2["set"]["singles"]["clara"])
    mod.make_set(ep2, resume=True)
    check(len(edits) == n0 + 1 and edits[-1]["refs"][1] == stills["clara"],
          "...a missing close-up -> only that one")
    for _p in ep2["set"]["singles"].values():
        os.remove(_p)
    _boom = {"n": 0}
    _fs0 = fc.submit

    def _busy_second(mode, p, ref_paths=None):
        if mode == "edit":
            _boom["n"] += 1
            if _boom["n"] == 2:
                raise RuntimeError("card busy")
        return _fs0(mode, p, ref_paths)
    fc.submit = _busy_second
    try:
        mod.make_set(ep2, resume=True)
    except RuntimeError:
        pass
    fc.submit = _fs0
    _saved = [k_ for k_, v_ in mod.load_project(ep2["slug"])["set"]["singles"].items()
              if os.path.exists(v_)]
    n1 = len(edits)
    mod.make_set(mod.load_project(ep2["slug"]), resume=True)
    check(len(_saved) == 1 and len(edits) == n1 + 1,
          "the card was busy on the 2nd close-up: the 1st was saved, the retry makes only "
          "the 2nd (%d saved)" % len(_saved))
    n2 = len(edits)
    mod.make_set(ep2)
    check(len(edits) == n2 + 4, "Rebuild the set (not resume) -> every pass again")
    _c3 = ["a", "b", "c"]
    check([mod._side_x(i) for i in range(3)] == [0.1, 0.9, 0.5] and
          [mod._look_dir(ep2, x, _c3) for x in _c3] ==
          ["screen-right", "screen-left", "just off the lens, toward the others"],
          "three on a set: positions and eyelines follow the plate (left, right, middle)")
    # RESUME a render that was in flight when the app closed: collect it, don't re-pay
    _s2 = ep2["shots"][1]
    _s2.update({"status": "running", "chosen": None, "takes": ["inflight01"]})
    os.makedirs(os.path.join(fc.RUNS, "inflight01"), exist_ok=True)
    open(os.path.join(fc.RUNS, "inflight01", "clip.mp4"), "wb").write(b"x")
    _rs0 = fc.run_state
    fc.run_state = lambda pid: {"state": "done" if pid == "inflight01" else "gone"}
    n0 = len(fs.calls)
    _r = mod.render_shot(ep2, 2)
    check(len(fs.calls) == n0 and _r["pid"] == "inflight01" and _s2["chosen"] == "inflight01",
          "a shot left rendering when the app closed is COLLECTED, not rendered again")
    _s2.update({"status": "running", "chosen": None, "takes": ["gonepid"]})
    mod.render_shot(ep2, 2)
    check(len(fs.calls) == n0 + 1, "...and one the rig lost is rendered again")
    fc.run_state = _rs0
    ep2["where"] = "a submarine galley"
    check(not mod.set_ready(ep2), "a new place makes the set stale (rebuilt with next shots)")
    fc.character_still = lambda slug: None if slug == "clara" else stills.get(slug)
    no_still = False
    try:
        mod.make_set(ep2)
    except ValueError as exc:
        no_still = "clara" in str(exc)
    check(no_still, "a character with no still -> a clear refusal naming them")

    print("\n%d checks, %d failures" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for m in FAILS:
            print("  FAILED: %s" % m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())