#!/usr/bin/env python3
"""Render one VIDEO from a saved Forge Lab CHARACTER.

    /opt/homebrew/bin/python3 tools/char_video.py --character bighead

WHY THIS EXISTS: the Character workspace could compose a look and load it onto the form, but
nothing exercised the whole loop character -> video end to end, so "does a character actually
make a good video" was untestable.

It goes through the app's OWN submit()/run_state()/collect(), so the run lands in
runs/index.json and shows up in Forge Lab's Takes exactly like a UI run.

⚠️ Price first. It prints estimate_seconds() and does nothing unless --go is passed, so a
render is never submitted by accident.

⚠️ Refuses to start unless forge's queue is EMPTY, so it cannot evict someone else's run.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.expanduser("~/forge-lab"))
import urllib.request

HOST = "http://forge:8188"


def load_app():
    spec = importlib.util.spec_from_file_location(
        "forge_lab", os.path.expanduser("~/forge-lab/forge_lab.py"))
    fl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fl)
    return fl


def get_json(path):
    with urllib.request.urlopen(HOST + path, timeout=30) as r:
        return json.loads(r.read().decode())


def probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate",
             "-of", "json", path], capture_output=True, text=True).stdout
        st = (json.loads(out).get("streams") or [{}])[0]
        return (st.get("width"), st.get("height"), st.get("nb_read_frames"),
                st.get("r_frame_rate"))
    except Exception as e:
        return ("probe failed: %r" % e,)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--character", required=True)
    ap.add_argument("--action", default=None, help="override the character's saved ACTION")
    ap.add_argument("--out", default=os.path.expanduser("~/forge-lab/drafts"))
    ap.add_argument("--go", action="store_true", help="actually submit")
    ap.add_argument("--size", default="544x736",
                    help="WxH, both multiples of 32. 544x736 puts the whole figure in ~300px of "
                         "body, which is the real limit on hands/buttons/shoes - not the steps.")
    ap.add_argument("--steps", type=int, default=12,
                    help="H3 steps: 4=Draft (the app says the FACE GOES SOFT, blocking only), "
                         "12=Standard (the default), 20=High")
    a = ap.parse_args()

    fl = load_app()
    meta = fl.read_character(a.character)
    if not meta:
        print("no such character: %s" % a.character)
        return 2
    still = fl.character_still(a.character)
    if not still or not os.path.exists(still):
        print("character has no still on disk")
        return 2

    action = a.action if a.action is not None else (meta.get("action") or "")
    look = fl.compose_character_prompt(meta.get("picks") or {}, for_video=True, action=action)
    # ⚠️ r2v binds the still as a reference, and the app's own reference case TAGS it. Without
    # "<Picture 1>" the model is not told which image is the character.
    prompt = "<Picture 1> is the character. " + look

    # ⚠️ Default to the app's own Standard, NOT the cheapest legal config. 4 steps is H3's
    # "Draft", which VIDEO_QUALITY itself flags as "the FACE GOES SOFT - blocking only".
    _w, _h = (int(v) for v in a.size.lower().split("x"))
    assert _w % 32 == 0 and _h % 32 == 0, "canvas must be multiples of 32 (got %dx%d)" % (_w, _h)
    p = {"prompt": prompt, "width": _w, "height": _h, "steps": a.steps,
         "length": fl.H3_SCENE_FRAMES.get(1, 124), "seed": -1, "prefix": "char_video_%dst_%s" % (a.steps, a.size),
         "video_mode": "r2v", "audio": True, "lora": None, "lora_strength": 1.0,
         "quality": {4: "Draft", 12: "Standard", 20: "High"}.get(a.steps, "custom"),
         "gpus": 1, "ref_paths": [still]}

    print("character : %s  (%s)" % (meta.get("name"), a.character))
    print("still     : %s" % still)
    print("action    : %s" % (action or "(none)"))
    print()
    print("PROMPT:")
    print("  " + prompt)
    print()

    q = get_json("/queue")
    if q.get("queue_running") or q.get("queue_pending"):
        print("!! forge queue is NOT empty - refusing to submit")
        return 3

    try:
        est = fl.estimate_seconds("video", p, [still], warm_engine=None)
    except Exception as e:
        est = None
        print("estimate failed: %r" % e)
    print("canvas    : %sx%s  length=%s frames  steps=%s (%s)"
          % (p["width"], p["height"], p["length"], p["steps"], p["quality"]))
    print("ESTIMATE  : %s s (includes the engine-load term)" % (round(est, 1) if est else "?"))

    if not a.go:
        print("\n(dry run - re-run with --go to submit)")
        return 0

    pid, seed = fl.submit("video", p, [still])
    print("\nSUBMITTED %s seed=%s" % (pid, seed), flush=True)

    t0 = time.time()
    last = None
    while time.time() - t0 < 2400:
        time.sleep(6)
        try:
            s = fl.run_state(pid)
        except Exception:
            continue
        state = s.get("state")
        if state != last:
            print("  %6.0fs  %s" % (time.time() - t0, state), flush=True)
            last = state
        if state in ("done", "error", "gone"):
            break

    meta2 = fl.collect(pid) or {}
    print("\ncollect: %s" % json.dumps({k: v for k, v in meta2.items() if k != "images"})[:300])
    files = meta2.get("images") or []
    if not files:
        print("NO OUTPUT FILES")
        return 1
    run_dir = os.path.join(fl.RUNS, pid)
    for f in files:
        src = os.path.join(run_dir, f["file"])
        print("  %-44s %9s bytes" % (f["file"], f.get("bytes")))
        if src.lower().endswith((".mp4", ".webm", ".mkv")):
            print("      probe: %s" % (probe(src),))
            dest = os.path.join(a.out, "charvideo_%s.mp4" % a.character)
            with open(src, "rb") as fi, open(dest, "wb") as fo:
                fo.write(fi.read())
            print("      copied -> %s" % dest)
    print("\nTAKE %s" % pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
