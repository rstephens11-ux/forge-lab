#!/usr/bin/env python3
"""Render ONE H3 r2v shot for a music video, headlessly, through Forge Lab's own submit path.

    /opt/homebrew/bin/python3 tools/mv_render.py --prompt prompts/sc01.txt \
        --refs "a.png,b.png" --size 832x480 --frames 124 --steps 12 --go

WHY THIS EXISTS: a music video is 20+ H3 shots cut to a beat grid. Driving that from the GUI is
not possible, and tools/char_video.py is wired to a saved CHARACTER (one still, app-composed
prompt) — this takes an arbitrary six-section Ref2VA prompt file and an ORDERED reference list.

⚠️ Price first. Prints the estimate and does nothing without --go.
⚠️ Refuses to submit unless forge's queue is EMPTY, so it cannot evict someone else's run.
⚠️ Every run lands in ~/forge-lab/runs/ and shows up in Forge Lab's Takes, like a UI run.
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.expanduser("~/forge-lab"))
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
        return "%sx%s %s frames @ %s" % (st.get("width"), st.get("height"),
                                         st.get("nb_read_frames"), st.get("r_frame_rate"))
    except Exception as e:
        return "probe failed: %r" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True, help="the six-section Ref2VA prompt file")
    ap.add_argument("--refs", default="", help="ORDERED comma-separated stills; ref 1 is primary")
    ap.add_argument("--size", default="832x480", help="WxH, multiples of 32")
    ap.add_argument("--frames", type=int, default=124, help="H3 wants frames %% 17 == 5")
    ap.add_argument("--steps", type=int, default=12, help="4=Draft 12=Standard 20=High")
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--gpus", type=int, default=1)
    ap.add_argument("--tag", default="mv")
    ap.add_argument("--out", default=os.path.expanduser("~/forge-lab/drafts/natasha-mv/clips"))
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--go", action="store_true", help="actually submit")
    a = ap.parse_args()

    if a.frames % 17 != 5:
        print("!! frames %% 17 must be 5 (an illegal count is silently SNAPPED UP by H3, so the "
              "render would not match the length you asked for). got %d" % a.frames)
        return 2

    fl = load_app()
    prompt = open(a.prompt).read().strip()
    refs = [x for x in (a.refs or "").split(",") if x]
    for r in refs:
        if not os.path.exists(r):
            print("!! missing reference: %s" % r)
            return 2

    _w, _h = (int(v) for v in a.size.lower().split("x"))
    assert _w % 32 == 0 and _h % 32 == 0, "canvas must be multiples of 32 (got %dx%d)" % (_w, _h)

    p = {"prompt": prompt, "width": _w, "height": _h, "steps": a.steps,
         "length": a.frames, "seed": a.seed,
         "prefix": "mv/%s" % a.tag,
         "video_mode": "r2v", "audio": True, "lora": None, "lora_strength": 1.0,
         "quality": {4: "Draft", 12: "Standard", 20: "High"}.get(a.steps, "custom"),
         "gpus": a.gpus, "ref_paths": refs}

    print("prompt : %s  (%d chars)" % (a.prompt, len(prompt)))
    print("refs   : %s" % (", ".join(os.path.basename(r) for r in refs) or "(none)"))
    print("canvas : %dx%d  %d frames (%.2f s @24fps)  steps=%d (%s)  gpus=%d"
          % (_w, _h, a.frames, a.frames / 24.0, a.steps, p["quality"], a.gpus))

    q = get_json("/queue")
    if q.get("queue_running") or q.get("queue_pending"):
        print("!! forge queue is NOT empty - refusing to submit")
        return 3

    try:
        est = fl.estimate_seconds("video", p, refs, warm_engine=None)
        print("estimate: %.1f s (includes the engine-load term)" % est)
    except Exception as e:
        print("estimate failed: %r" % e)

    if not a.go:
        print("\n(dry run - re-run with --go to submit)")
        return 0

    pid, seed = fl.submit("video", p, refs)
    print("\nSUBMITTED %s seed=%s" % (pid, seed), flush=True)

    t0, last = time.time(), None
    while time.time() - t0 < a.timeout:
        time.sleep(6)
        try:
            st = (fl.run_state(pid) or {}).get("state")
        except Exception:
            continue
        if st != last:
            print("  %6.0fs  %s" % (time.time() - t0, st), flush=True)
            last = st
        if st in ("done", "error", "gone"):
            break

    meta = fl.collect(pid) or {}
    files = meta.get("images") or []
    if not files:
        print("\nNO OUTPUT FILES  status=%s err=%s" % (meta.get("status"), meta.get("error")))
        return 1

    os.makedirs(a.out, exist_ok=True)
    run_dir = os.path.join(fl.RUNS, pid)
    for f in files:
        src = os.path.join(run_dir, f["file"])
        if not src.lower().endswith((".mp4", ".webm", ".mkv")):
            continue
        dest = os.path.join(a.out, "%s.mp4" % a.tag)
        shutil.copyfile(src, dest)
        print("\n  %-40s %9s bytes" % (f["file"], f.get("bytes")))
        print("  probe: %s" % probe(dest))
        print("  -> %s" % dest)
    print("\nTAKE %s" % pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
