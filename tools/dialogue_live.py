#!/usr/bin/env python3
"""LIVE: design a cast from a JSON file, render a script, ASR-check, assemble — headless.

Uses the SAME plan_render() the Dialogue window uses, so a green run here proves the window's
rig path end to end. GPU1 only (TTS + ASR are one-shot rig processes).

  FORGE_LAB_DATA=<scratch> /opt/homebrew/bin/python3 tools/dialogue_live.py CAST.json SCRIPT.md NAME
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import dialogue as dlg                      # noqa: E402
from dialogue_ui import plan_render         # noqa: E402
from audio.client import tts_batch, transcribe_many   # noqa: E402


def main(cast_path, script_path, name, engine="qwen"):
    t_all = time.time()
    with open(cast_path) as f:
        cast = json.load(f)["voices"]
    for c in cast:
        v = dlg.new_voice(c["name"], gender=c["gender"], age_years=c["age_years"],
                          accent=c["accent"], notes=c["notes"], sliders=c["sliders"],
                          characters=c["characters"])
        if not dlg.load_voice(v["slug"]):
            dlg.save_voice(v)
    with open(script_path) as f:
        script = f.read()
    pj = dlg.load_project(dlg.slugify(name)) or dlg.new_project(name, script)
    voices = {v["slug"]: v for v in dlg.list_voices()}
    pj["cast"] = dlg.auto_cast(dlg.speakers(pj["lines"]), voices, pj.get("cast"))
    print("cast:", pj["cast"], "engine:", engine)
    proj_dir = os.path.join(dlg.dialogue_dir(), pj["slug"])
    os.makedirs(os.path.join(proj_dir, "lines"), exist_ok=True)

    # every cast voice gets designed, even ones this script does not use (Ryan judges them all)
    designs = []
    for c in cast:
        v = voices[dlg.slugify(c["name"])]
        if not os.path.isfile(os.path.join(dlg.voices_dir(), v["slug"], "neutral.wav")):
            designs.append({"id": "%s:neutral" % v["slug"],
                            "instruct": dlg.compose_description(v, "neutral"),
                            "text": dlg.DEFAULT_SAMPLE, "out": "%s__neutral.wav" % v["slug"]})
    d2, _j, skipped = plan_render(pj, voices, "missing", engine)
    designs += [d for d in d2 if d["id"] not in {x["id"] for x in designs}]
    print("skipped:", skipped)
    print("designs (%d, always the qwen VoiceDesign rig): %s"
          % (len(designs), [d["id"] for d in designs]))
    if designs:
        t0 = time.time()
        out = tempfile.mkdtemp()
        info = tts_batch({"design": designs, "clone": [], "voices": {}}, out)
        for it in info["items"]:
            if "error" in it:
                print("DESIGN FAIL", it); continue
            slug, mood = it["id"].split(":")
            shutil.copyfile(it["out"], os.path.join(dlg.voices_dir(), slug, mood + ".wav"))
            v = dlg.load_voice(slug)
            v["moods"].setdefault(mood, {}).update({"wav": mood + ".wav", "ref_text": dlg.DEFAULT_SAMPLE})
            dlg.save_voice(v)
        print("designed %d in %.1f s wall (rig load %s)" % (len(designs), time.time() - t0,
                                                            info.get("load_seconds")))
        voices = {v["slug"]: v for v in dlg.list_voices()}

    _d, job, _s = plan_render(pj, voices, "missing", engine)
    new = []
    if job:
        t0 = time.time()
        out = tempfile.mkdtemp()
        info = tts_batch(job, out, engine=engine)
        by = {c["id"]: c for c in job["clone"]}
        for it in info["items"]:
            li = pj["lines"][by[it["id"]]["_line"]]
            if "error" in it:
                print("LINE FAIL", li["idx"], it["error"]); continue
            fn = "%03d_%d.wav" % (li["idx"], len(li.get("takes", [])) + 1)
            shutil.copyfile(it["out"], os.path.join(proj_dir, "lines", fn))
            li.setdefault("takes", []).append({"file": "lines/" + fn, "seconds": it["duration"],
                                               "heard": "", "score": None})
            li["kept"] = len(li["takes"]) - 1
            new.append(li)
        print("read %d lines in %.1f s wall (rig load %s)"
              % (len(new), time.time() - t0, info.get("load_seconds")))
    if new:
        t0 = time.time()
        heard = transcribe_many([os.path.join(proj_dir, li["takes"][li["kept"]]["file"]) for li in new])
        for li, h in zip(new, heard):
            tk = li["takes"][li["kept"]]
            tk["heard"], tk["score"] = h, round(dlg.score(li["text"], h), 3)
        print("ASR %d in %.1f s wall" % (len(new), time.time() - t0))
    dlg.save_project(pj)

    paths, tmp = [], tempfile.mkdtemp()
    for li in pj["lines"]:
        if not li.get("takes"):
            continue
        src = os.path.join(proj_dir, li["takes"][li["kept"]]["file"])
        dst = os.path.join(tmp, "n%03d.wav" % li["idx"])
        dlg.peak_normalise(src, dst)
        paths.append(dst)
    total = dlg.assemble(paths, os.path.join(proj_dir, "scene.wav"), pj.get("gap_ms", 350))
    print("\nscene.wav %.2f s  (total wall %.1f s)" % (total, time.time() - t_all))
    for li in pj["lines"]:
        tk = (li.get("takes") or [{}])[li.get("kept", 0)] if li.get("takes") else {}
        print("%2d %-8s %5s  %s\n            heard: %s" % (li["idx"], dlg.normalise_speaker(li["speaker"]),
              tk.get("score"), li["text"][:70], (tk.get("heard") or "")[:70]))


if __name__ == "__main__":
    main(*sys.argv[1:4], engine=(sys.argv[4] if len(sys.argv) > 4 else "qwen"))
