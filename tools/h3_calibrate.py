#!/usr/bin/env python3
"""H3 cost-curve CALIBRATION on the standard (1-card) path.

Why: the existing law was fitted to five points at 73 frames plus one at 124, and
H3_FRAME_REF=124 means its frame term never engaged below that. Refitting against the
historic numbers does not work — the 73-frame canvas sweep implies NO fixed load while
the 124/362-frame points demand ~129 s of it. Those measurements were taken under
different cache conditions. So the curve has to be re-measured cleanly, in one session,
on the same engine so the load term is consistent.

Design:
  * one canvas (1344x768, 1.032 MP) — the one that matters for the Alice work
  * the app's own default path: 4 steps + the 4-step turbo LoRA (turbo turns on at <=4)
  * a 1-STEP probe at 124 frames to pin the fixed load as the intercept
  * frames 124 / 181 / 241 / 362 — the model's trained range (~124-362)
  * every case submitted through the app's OWN submit()/run_state()/collect()

⚠️ REFUSES to start if forge's queue is not empty.
"""
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

ALICE = os.path.expanduser("~/forge-lab-assets/character-ref.png")
OUT = os.path.join(HERE, "drafts", "h3-calibration%s.txt"
                   % ("-ray2" if "--ray2" in sys.argv else
                      "-long" if "--long" in sys.argv else
                      "-base" if "--base" in sys.argv else
                      "-sep" if "--sep" in sys.argv else
                      "-canvas" if "--canvas" in sys.argv else "-frames"))

BASE = {"width": 1344, "height": 768, "audio": True, "gpus": 1,
        "lora": fl.H3_LONGFORM_LORA, "lora_strength": 1.0, "video_mode": "r2v",
        "prompt": "<Picture 1> is the character. He walks toward camera."}

FRAME_CASES = [
    ("load probe", dict(BASE, length=124, steps=1, prefix="cal_load")),
    ("124 fr",     dict(BASE, length=124, steps=4, prefix="cal_124")),
    ("181 fr",     dict(BASE, length=181, steps=4, prefix="cal_181")),
    ("241 fr",     dict(BASE, length=241, steps=4, prefix="cal_241")),
    ("362 fr",     dict(BASE, length=362, steps=4, prefix="cal_362")),
]

# The CANVAS sweep. Must run in the SAME session and config as the frame sweep, or the
# two datasets cannot be combined into one law — which is exactly the trap the historic
# numbers fell into (a 73-frame sweep that fits with NO fixed load, next to 124/362-frame
# points that demand ~25 s of it).
CANVAS_CASES = [
    ("480p  832x480",   dict(BASE, length=124, steps=4, width=832,  height=480,  prefix="calc_480")),
    ("720p 1280x704",   dict(BASE, length=124, steps=4, width=1280, height=704,  prefix="calc_720")),
    ("native 1344x768", dict(BASE, length=124, steps=4, width=1344, height=768,  prefix="calc_nat")),
    ("1080p 1920x1088", dict(BASE, length=124, steps=4, width=1920, height=1088, prefix="calc_1080")),
    ("2K 2560x1440",    dict(BASE, length=124, steps=4, width=2560, height=1440, prefix="calc_2k")),
]

# SEPARABILITY TEST. The fitted law multiplies a canvas term by a frame term, but canvas
# was only ever swept at 124 fr and frames only at native — so the multiplication is an
# ASSUMPTION. These two points sit at a canvas (720p) measured only at 124 fr, at two
# different frame counts. If the law is right they land on prediction; if the axes don't
# separate, they won't.
SEP_CASES = [
    ("720p 181fr", dict(BASE, length=181, steps=4, width=1280, height=704, prefix="calp_181")),
    ("720p 241fr", dict(BASE, length=241, steps=4, width=1280, height=704, prefix="calp_241")),
]

# TURBO-vs-BASE TEST. Every clean measurement so far is the 4-step turbo path. The historic
# 73-frame rows are the 12-step BASE model, and they imply 0.242 s/frame-step at native while
# the turbo runs imply 0.333. Either the base model is really cheaper per step, or those old
# rows are stale. Re-measuring the SAME two configs in this session decides it.
BASE_CASES = [
    ("native 73fr/12st",  dict(BASE, length=73,  steps=12, prefix="calb_073")),
    ("native 124fr/12st", dict(BASE, length=124, steps=12, prefix="calb_124")),
]

# The one old regression row the new law still misses: 241fr/12st at native (old record
# 1571.0 s, new law 1819 s). It is a LONG native clip at high steps -- the regime Ryan
# actually renders Alice in -- so it gets re-measured rather than assumed.
LONG_CASES = [
    ("native 241fr/12st", dict(BASE, length=241, steps=12, prefix="call_241")),
]

# RAYLIGHT 2-GPU. The 1-GPU half of this ladder was already measured in the sweeps above, so
# only the 2-GPU side is needed. Same canvas, same steps, same turbo LoRA -- the ONLY change
# is gpus=1 -> 2, so any difference is Raylight and nothing else.
RAY2_CASES = [
    ("2gpu native 124fr/1st", dict(BASE, gpus=2, length=124, steps=1, prefix="ray2_124_1")),
    ("2gpu native 124fr/4st", dict(BASE, gpus=2, length=124, steps=4, prefix="ray2_124_4")),
    ("2gpu native 241fr/4st", dict(BASE, gpus=2, length=241, steps=4, prefix="ray2_241_4")),
    ("2gpu native 362fr/4st", dict(BASE, gpus=2, length=362, steps=4, prefix="ray2_362_4")),
    ("2gpu 720p   124fr/4st", dict(BASE, gpus=2, length=124, steps=4, width=1280, height=704,
                                   prefix="ray2_720_4")),
]

if "--ray2" in sys.argv:
    CASES = RAY2_CASES
elif "--long" in sys.argv:
    CASES = LONG_CASES
elif "--base" in sys.argv:
    CASES = BASE_CASES
elif "--sep" in sys.argv:
    CASES = SEP_CASES
elif "--canvas" in sys.argv:
    CASES = CANVAS_CASES
else:
    CASES = FRAME_CASES
DRY = "--dry" in sys.argv


def main():
    if not os.path.exists(ALICE):
        print("MISSING ASSET: %s" % ALICE); return 2
    print("H3 calibration — standard 1-card path, 4 steps + turbo LoRA\n")
    for label, p in CASES:
        est = fl.estimate_seconds("video", p, [ALICE], None)
        print("  %-11s frames=%-4d steps=%d   current estimate %6.0f s"
              % (label, p["length"], p["steps"], est))
    if DRY:
        print("\n--dry: nothing submitted."); return 0
    q = fl.get_json("/queue")
    if q.get("queue_running") or q.get("queue_pending"):
        print("\nREFUSING: forge's queue is not empty."); return 3

    rows = []
    for label, p in CASES:
        print("\n" + "=" * 70)
        print("%s   frames=%d steps=%d" % (label, p["length"], p["steps"]))
        t0 = time.time()
        try:
            pid, seed = fl.submit("video", p, [ALICE])
        except Exception as exc:
            print("  SUBMIT FAILED — %s: %s" % (type(exc).__name__, str(exc)[:200]))
            rows.append((label, p["length"], p["steps"], None)); continue
        print("  pid=%s" % pid)
        state = "unknown"
        for _ in range(900):
            time.sleep(3)
            rs = fl.run_state(pid)
            state = rs.get("state")
            if state in ("done", "error", "gone"):
                break
        took = time.time() - t0
        ok = state == "done"
        if ok:
            meta = fl.collect(pid) or {}
            n = len([e for e in (meta.get("images") or [])])
            print("  done in %.1f s, %d artifact(s)" % (took, n))
        else:
            print("  STATE=%s after %.0f s  %s" % (state, took, (rs.get("detail") or "")[:150]))
        rows.append((label, p["length"], p["steps"], took if ok else None))

    print("\n" + "=" * 70)
    print("MEASURED (paste into the estimator comment):")
    with open(OUT, "w") as f:
        f.write("# H3 calibration, standard 1-card path, 1344x768, 4 steps + turbo LoRA\n")
        f.write("# measured %s\n" % time.strftime("%Y-%m-%d %H:%M"))
        f.write("%-12s %7s %6s %10s\n" % ("label", "frames", "steps", "seconds"))
        for label, fr, st, took in rows:
            print("  %-12s %5d fr  %d st   %s" % (label, fr, st,
                  ("%.1f s" % took) if took else "FAILED"))
            if took:
                f.write("%-12s %7d %6d %10.1f\n" % (label, fr, st, took))
    print("\n  written to %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())