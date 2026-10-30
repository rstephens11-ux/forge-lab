#!/usr/bin/env python3
"""LIVE proof of the new pieces: Old-English accent + three moods (TTS, GPU1) and one SUNG line
(YuE2, GPU0) through the window's own plan_render / plan_sung / render_sung.

  FORGE_LAB_DATA=<scratch> /opt/homebrew/bin/python3 tools/dialogue_live_extras.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import dialogue as dlg                                           # noqa: E402
import forge_lab as fl                                           # noqa: E402  (DATA from env)
from dialogue_ui import plan_render, plan_sung, render_sung      # noqa: E402
from audio.client import tts_batch, transcribe_many              # noqa: E402

SCRIPT = """> **BARD** — Good morrow, sirs. What news upon the road?
> **AVERY** — *(indignantly)* The bridge does not wish to be paid?
> **AVERY** — *(solemnly)* I gave my word. A man's word is the whole of him.
> **AVERY** — *(addresses the crowd)* Gentlemen, the post road is ours to mend!
> **AVERY** — *(singing)* Yankee Doodle went to town, a-riding on a pony.
"""

t_all = time.time()
oe = dict(dlg.ACCENTS)[next(a for a, _w in dlg.ACCENTS if a.startswith("Old English"))]
bard = dlg.new_voice("Bard", gender="male", age_years=50, accent=oe,
                     notes="a travelling player, rich and theatrical", characters=["BARD"])
dlg.save_voice(bard)
g = dlg.new_voice("Avery", gender="male", age_years=20,
                  accent=dict(dlg.ACCENTS)["Colonial Virginia (1750s)"],
                  notes="formal 18th-century cadence, direct, never uses contractions",
                  sliders={"pitch": -1, "pace": 0, "energy": 0, "age": 1, "warmth": 0, "grit": 0},
                  characters=["AVERY"], singing="earnest young male tenor, plain untrained folk voice")
for m in ("indignant", "solemn", "oratory"):
    g["moods"][m] = {"extra": dlg.MOOD_WORDS[m], "wav": "", "ref_text": dlg.DEFAULT_SAMPLE}
dlg.save_voice(g)

pj = dlg.new_project("extras", SCRIPT)
voices = {v["slug"]: v for v in dlg.list_voices()}
pj["cast"] = dlg.auto_cast(dlg.speakers(pj["lines"]), voices)
proj_dir = os.path.join(dlg.dialogue_dir(), pj["slug"])
os.makedirs(os.path.join(proj_dir, "lines"), exist_ok=True)

designs, _j, sk = plan_render(pj, voices, "missing")
print("designs:", [d["id"] for d in designs], "skipped:", sk)
for d in designs:
    print("   %s -> %s" % (d["id"], d["instruct"]))
t0 = time.time()
info = tts_batch({"design": designs, "clone": [], "voices": {}}, tempfile.mkdtemp())
for it in info["items"]:
    if "error" in it:
        print("DESIGN FAIL", it); continue
    slug, mood = it["id"].split(":")
    shutil.copyfile(it["out"], os.path.join(dlg.voices_dir(), slug, mood + ".wav"))
    v = dlg.load_voice(slug); v["moods"].setdefault(mood, {}).update(
        {"wav": mood + ".wav", "ref_text": dlg.DEFAULT_SAMPLE}); dlg.save_voice(v)
print("designed %d in %.1f s" % (len(designs), time.time() - t0))

voices = {v["slug"]: v for v in dlg.list_voices()}
_d, job, _s = plan_render(pj, voices, "missing")
print("clone keys:", [c["voice"] for c in job["clone"]])
t0 = time.time()
info = tts_batch(job, tempfile.mkdtemp())
by = {c["id"]: c for c in job["clone"]}
new = []
for it in info["items"]:
    li = pj["lines"][by[it["id"]]["_line"]]
    if "error" in it:
        print("LINE FAIL", it); continue
    fn = "%03d_1.wav" % li["idx"]
    shutil.copyfile(it["out"], os.path.join(proj_dir, "lines", fn))
    li["takes"] = [{"file": "lines/" + fn, "seconds": it["duration"], "heard": "", "score": None}]
    li["kept"] = 0
    new.append(li)
print("cloned %d in %.1f s" % (len(new), time.time() - t0))

sung = plan_sung(pj, voices, "missing")
print("sung jobs:", [(x["_line"], x["style"], x["seconds"]) for x in sung])
t0 = time.time()
made = render_sung(sung, pj, proj_dir, fl.submit, fl.wait_and_collect, fl.RUNS,
                   progress=lambda m: print("  ..", m))
print("sung %d in %.1f s  errors: %r" % (len(made), time.time() - t0,
                                        [li.get("error") for li in pj["lines"] if li.get("error")]))

paths = [os.path.join(proj_dir, li["takes"][0]["file"]) for li in new + made]
heard = transcribe_many(paths)
for li, h in zip(new + made, heard):
    li["takes"][0]["heard"], li["takes"][0]["score"] = h, round(dlg.score(dlg.sung_text(li["text"]), h), 3)
dlg.save_project(pj)

tmp = tempfile.mkdtemp(); ps = []
for li in pj["lines"]:
    if li.get("takes"):
        d = os.path.join(tmp, "n%03d.wav" % li["idx"])
        dlg.peak_normalise(os.path.join(proj_dir, li["takes"][0]["file"]), d)
        ps.append(d)
total = dlg.assemble(ps, os.path.join(proj_dir, "scene.wav"), 400)
print("\nscene %.1f s, total wall %.1f s" % (total, time.time() - t_all))
for li in pj["lines"]:
    tk = (li.get("takes") or [{}])[0]
    print("%d %-6s %-10s %5s %5.1fs  %s\n                       heard: %s" % (
        li["idx"], dlg.normalise_speaker(li["speaker"]), "SUNG" if dlg.is_sung(li) else li.get("mood") or "neutral",
        tk.get("score"), tk.get("seconds") or 0, li["text"][:60], (tk.get("heard") or "")[:60]))
