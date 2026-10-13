#!/usr/bin/env python3
"""WAN 2.2 vs MiniMax H3, head to head on ONE shot.

Same start image, same prompt, same seed, same canvas, same duration. Both runs go through
Forge Lab's OWN submit()/collect(), so this exercises the wired code rather than a parallel
path, and both land in Takes like any other render.

    python3 tools/wan_vs_h3.py --image <start.png> [--seconds 5] [--seed 771203]

Each run is submitted SEQUENTIALLY on purpose: one GPU, so overlapping jobs would make the
wall times meaningless.
"""
import argparse
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab",
                                               os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

PROMPT = ("A black-and-white photograph looking straight down a narrow modernist corridor "
          "built from nested rectangular frames — heavy black beams against bright white "
          "panels — receding to a bright vanishing point at the far end. The camera pushes "
          "slowly forward down the centre as the frames glide past one after another; fine "
          "dust drifts through the shafts of light. Extreme one-point perspective, "
          "high-contrast chiaroscuro, hard-edged shadows, no people, monochrome, "
          "fine 35mm film grain.")

ap = argparse.ArgumentParser()
ap.add_argument("--image", required=True, help="start (first) frame — used by BOTH engines")
ap.add_argument("--seconds", type=float, default=5.0)
ap.add_argument("--width", type=int, default=544)
ap.add_argument("--height", type=int, default=736)
ap.add_argument("--seed", type=int, default=771203)
ap.add_argument("--only", default=None, help="comma-separated run keys to limit the set")
args = ap.parse_args()

if not os.path.exists(args.image):
    sys.exit("no such image: %s" % args.image)

# (key, kind, label, param overrides)
RUNS = [
    ("wan-turbo", "wan", "WAN 2.2 i2v · Turbo 4 steps",
     {"wan_mode": "i2v", "quality": "Turbo 4 steps"}),
    ("h3-draft-audio", "video", "H3 i2v · Draft 4 steps · audio ON",
     {"video_mode": "i2v", "steps": 4, "audio": True}),
    ("h3-std-audio", "video", "H3 i2v · Standard 12 steps · audio ON",
     {"video_mode": "i2v", "steps": 12, "audio": True}),
    ("h3-std-silent", "video", "H3 i2v · Standard 12 steps · audio OFF",
     {"video_mode": "i2v", "steps": 12, "audio": False}),
]
if args.only:
    want = set(args.only.split(","))
    RUNS = [r for r in RUNS if r[0] in want]

base = {"prompt": PROMPT, "seconds": args.seconds,
        "width": args.width, "height": args.height, "seed": args.seed,
        "prefix": "wan_vs_h3"}

print("canvas %dx%d  ·  %.0fs requested  ·  seed %d" % (args.width, args.height,
                                                        args.seconds, args.seed))
print("frames: WAN i2v %d @16fps   H3 i2v %d @24fps"
      % (fl.wan_frames(args.seconds), fl.h3_frames(args.seconds)))
print("prompt (%d chars): %s…\n" % (len(PROMPT), PROMPT[:70]))

results = []
for key, kind, label, over in RUNS:
    p = dict(base, **over)
    est = fl.estimate_seconds(kind, p)
    print("=" * 78)
    print("%s\n  estimate ~%ds" % (label, est))
    t0 = time.time()
    try:
        pid, seed = fl.submit(kind, p, [args.image])
    except Exception as exc:
        print("  SUBMIT FAILED: %s: %s" % (type(exc).__name__, exc))
        results.append({"key": key, "label": label, "error": str(exc)})
        continue
    print("  submitted %s seed=%s" % (pid[:8], seed))
    last = None
    while True:
        time.sleep(4)
        s = fl.run_state(pid)
        el = time.time() - t0
        if s["state"] != last:
            print("   %6.1fs  %s %s" % (el, s["state"], s.get("detail", "")), flush=True)
            last = s["state"]
        if s["state"] in ("done", "error", "gone"):
            break
        if el > 3600:
            print("   giving up after an hour")
            break
    wall = time.time() - t0
    meta = fl.collect(pid)
    if not meta:
        print("  collect returned nothing")
        results.append({"key": key, "label": label, "pid": pid, "error": "collect failed"})
        continue
    d = os.path.join(fl.RUNS, pid)
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    mp4 = [f for f in files if f.endswith((".mp4", ".webm"))]
    print("  %s in %.1fs wall  ·  %s  ·  files: %s"
          % (meta.get("status"), wall, meta.get("error") or "", files))
    results.append({"key": key, "label": label, "pid": pid, "seed": seed,
                    "status": meta.get("status"), "wall_s": round(wall, 1),
                    "estimate_s": round(est, 1), "frames": meta.get("length"),
                    "fps": meta.get("fps"), "audio": meta.get("audio"),
                    "quality": meta.get("quality"), "steps": meta.get("steps"),
                    "dir": d, "videos": mp4})

out = os.path.join(HERE, "runs", "wan_vs_h3-summary.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump({"prompt": PROMPT, "start_image": args.image, "seed": args.seed,
           "canvas": [args.width, args.height], "seconds": args.seconds,
           "results": results}, open(out, "w"), indent=1)

print("\n" + "=" * 78)
print("%-16s %-34s %8s %8s %6s %5s %s" % ("key", "run", "est", "wall", "frames", "fps", "audio"))
for r in results:
    print("%-16s %-34s %8s %8s %6s %5s %s" % (
        r["key"], r["label"][:34], r.get("estimate_s", "-"), r.get("wall_s", "-"),
        r.get("frames", "-"), r.get("fps", "-"), r.get("audio", "-")))
print("\nsummary: %s" % out)