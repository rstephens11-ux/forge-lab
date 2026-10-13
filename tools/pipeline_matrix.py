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


def two_shot_project(mod):
    pj = mod.new_project(["bighead"], "he tries on hats in a shop", length_s=8, quality="Draft")
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
    pj2 = two_shot_project(mod)
    pj2["shots"][0]["n"] = 1
    mod.voice_lines(pj2)
    check(bool(fau.clones), "a later line CLONES the saved voice instead of re-designing")

    print("== render: guide_audio_path + refs ==")
    mod, fak, fau, fs = fresh()
    pj = two_shot_project(mod)
    mod.voice_lines(pj)
    mod.render_shot(pj, 1)
    call1 = fs.calls[-1]
    check(call1["p"]["guide_audio_path"] == pj["shots"][0]["audio_wav"],
          "the lip-sync shot submits guide_audio_path")
    check(call1["p"]["video_mode"] == "r2v", "shot 1 is r2v")
    check(call1["prompt_tag"] if "prompt_tag" in call1 else
          call1["p"]["prompt"].startswith("<Picture 1> is the character."),
          "r2v prompt is tagged <Picture 1>")
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
    check("Dialogue: 0,0:00:00.00,0:00:00.60,Cap,,0,0,0,,ONE TWO THREE" in ass,
          "shot-1 words group into one timed event")
    check("Dialogue: 0,0:00:03.00,0:00:03.80,Cap,,0,0,0,,FOUR FIVE SIX SEVEN" in ass,
          "shot-2 words are OFFSET by the shot start (3.0 s)")
    check(ass.count("Dialogue:") == 2, "3 words -> 1 event; 4 words -> 1 event (2 total)")
    groups = mod._group_words([{"word": w} for w in "a b c d e f g".split()])
    check(all(2 <= len(g) <= 4 for g in groups) and sum(len(g) for g in groups) == 7,
          "word grouping never leaves a 1-word tail: %r" % [[w["word"] for w in g] for g in groups])

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

    print("\n%d checks, %d failures" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for m in FAILS:
            print("  FAILED: %s" % m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())