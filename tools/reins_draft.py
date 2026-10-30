#!/usr/bin/env python3
"""REINS — render the Nous "Reins" spot LOCALLY on forge (Forge Lab), as a draft.

The production at ~/nous-hermes-ad/ was staged for slop.cc's cloud renderer and blocked by their
MCP bug (401 on mutating tools). It does not need them: every input is on disk — a timed plan of
5 scenes, a reference still per scene, per-scene song windows, and the song. This drives the app's
OWN submit()/run_state()/collect() so the local render is the same code path the buttons use.

    python3 tools/reins_draft.py --dry    # plan + estimates, submit nothing
    python3 tools/reins_draft.py          # render the 5 scenes, trim to plan, stitch + song

Faithfulness notes:
  * Prompts are the timed plan's own beats, verbatim (they are self-contained — each already
    carries its palette and the no-text rules). Only `<Picture 1>` is prepended, the app's r2v
    dialect. Pre-pending a global style block would CONTRADICT sc04, which inverts to blue-on-white.
  * H3 only emits frames of the form n*17+5, so each scene renders slightly longer than its plan
    slot and is then TRIMMED to the exact duration — otherwise every scene after sc01 drifts late
    and the spot's hit marks (13.54 … 16.16, wordmark 30.0) stop lining up.
  * No audio guide: the bible requires statue-still figures with all motion in the camera, and
    guiding H3 with the song steers PERFORMANCE. The song is laid under the cut instead.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

PROD = os.path.expanduser("~/nous-hermes-ad")


def argval(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


OUT = os.path.expanduser(argval("--out", os.path.join(PROD, "draft-local")))
os.makedirs(OUT, exist_ok=True)

DRY = "--dry" in sys.argv
WAIT = "--wait" in sys.argv
GPUS = int(argval("--gpus", "1"))   # 1 card: small canvas, 2-card is near break-even here
STEPS = int(argval("--steps", "4"))  # 4 = H3 Draft, 12 = the app's Standard
W, H = (int(x) for x in argval("--size", "832x480").lower().split("x"))
FPS = 24.0
QUIET_MIN = 15.0
MAX_WAIT_S = 6 * 3600
HOST = "name@forge"

PLAN_PATH = os.path.expanduser(argval("--plan", os.path.join(PROD, "production",
                                                               "timed-plan.json")))
PLAN = json.load(open(PLAN_PATH))
print("plan: %s (schemaVersion %s)" % (PLAN_PATH, PLAN.get("schemaVersion", 1)))
PRODJ = json.load(open(os.path.join(PROD, "production.json")))
REFS = {r["name"].upper(): os.path.join(PROD, r["path"]) for r in PRODJ["references"]}
SONG = os.path.join(PROD, PRODJ["song"])


def ssh(cmd, timeout=30):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", HOST, cmd],
                          capture_output=True, text=True, timeout=timeout)


def queue_empty():
    try:
        q = fl.get_json("/queue")
        return not (q.get("queue_running") or q.get("queue_pending"))
    except Exception as exc:
        print("  could not read forge's queue: %s" % exc)
        return False


def gate():
    lab = os.path.join(HERE, "forge_lab.py")
    waited = 0.0
    while True:
        q_ok = queue_empty()
        idle_min = (time.time() - os.path.getmtime(lab)) / 60.0
        if q_ok and idle_min >= QUIET_MIN:
            print("  [gate] forge quiet (queue empty, forge_lab.py idle %.0f min)" % idle_min)
            return True
        if not WAIT:
            print("\nREFUSING: forge not quiet (queue_empty=%s, forge_lab.py idle %.0f min). "
                  "Use --wait." % (q_ok, idle_min))
            return False
        if waited >= MAX_WAIT_S:
            print("\n[gate] gave up waiting.")
            return False
        print("  [gate] waiting: queue_empty=%s idle=%.0f min (%.0f min)" % (q_ok, idle_min,
                                                                             waited / 60.0),
              flush=True)
        time.sleep(120)
        waited += 120.0


def collect_media(pid):
    """collect() can race on the LAST scene (history says done before outputs commit) — retry,
    then fall back to the run dir."""
    for _ in range(6):
        meta = fl.collect(pid) or {}
        for ent in (meta.get("images") or []):
            fn = ent.get("file") if isinstance(ent, dict) else ent
            if not fn:
                continue
            f = fn if os.path.isabs(str(fn)) else os.path.join(fl.RUNS, pid, str(fn))
            if os.path.exists(f) and os.path.getsize(f) > 5000:
                return f
        time.sleep(5)
    d = os.path.join(fl.RUNS, pid)
    cands = [os.path.join(d, x) for x in (os.listdir(d) if os.path.isdir(d) else [])
             if x.endswith((".mp4", ".webm"))]
    cands = [c for c in cands if os.path.getsize(c) > 5000]
    return cands[0] if cands else None


def trim(src, dst, dur):
    """Re-encode to the EXACT plan duration so nothing drifts (drop H3's own audio)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-t", "%.3f" % dur, "-an",
                        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", dst],
                       capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(dst)


def main():
    scenes = PLAN["scenes"]
    print("REINS draft — %d scenes, %.3f s total, %dx%d, %d steps, gpus=%d"
          % (len(scenes), PLAN["durationMs"] / 1000.0, W, H, STEPS, GPUS))
    plan = []
    total_est = 0.0
    for i, sc in enumerate(scenes, start=1):
        dur = (sc["endMs"] - sc["startMs"]) / 1000.0
        frames = fl.h3_frames(dur)
        cast = (sc.get("cast") or ["?"])[0].upper()
        ref = REFS.get(cast)
        p = {"prompt": "<Picture 1> is the subject. " + sc["beat"], "video_mode": "r2v",
             "lora": None, "lora_strength": 1.0, "steps": STEPS, "length": frames,
             "width": W, "height": H, "seed": 77000 + i * 131, "audio": True, "gpus": GPUS,
             "prefix": "video/reins_draft"}
        try:
            est = fl.estimate_seconds("video", p, [ref], None)
        except Exception as exc:
            est = None
            print("  sc%02d ESTIMATE RAISED %s: %s" % (i, type(exc).__name__, exc))
        total_est += est or 0
        print("  sc%02d %-11s plan %5.2fs -> H3 %3d fr (%5.2fs)  ref=%s  est=%s"
              % (i, cast, dur, frames, frames / FPS,
                 os.path.basename(ref) if ref else "MISSING", ("%.0f s" % est) if est else "?"))
        plan.append((i, dur, p, ref))
    print("  TOTAL estimate: %.1f min" % (total_est / 60.0))
    for i, dur, p, ref in plan:
        if not ref or not os.path.exists(ref):
            print("\nMISSING REFERENCE for sc%02d: %s" % (i, ref))
            return 2
    if not os.path.exists(SONG):
        print("\nMISSING SONG: %s" % SONG)
        return 2
    if DRY:
        print("\n--dry: nothing submitted.")
        return 0
    if not gate():
        return 3

    got = []
    t_all = time.time()
    for i, dur, p, ref in plan:
        t0 = time.time()
        try:
            pid, seed = fl.submit("video", p, ref_paths=[ref])
        except Exception as exc:
            print("  sc%02d SUBMIT FAILED — %s: %s" % (i, type(exc).__name__, str(exc)[:300]),
                  flush=True)
            continue
        print("  sc%02d pid=%s — rendering…" % (i, pid), flush=True)
        last = None
        while time.time() - t0 < 3600:
            time.sleep(5)
            st = fl.run_state(pid)
            if st["state"] != last:
                print("       %6.1fs %s" % (time.time() - t0, st["state"]), flush=True)
                last = st["state"]
            if st["state"] in ("done", "error", "gone"):
                break
        if last != "done":
            print("  sc%02d STATE=%s after %.0f s" % (i, last, time.time() - t0), flush=True)
            continue
        raw = collect_media(pid)
        if not raw:
            print("  sc%02d no artifact" % i, flush=True)
            continue
        cut = os.path.join(OUT, "scene_%02d.mp4" % i)
        if not trim(raw, cut, dur):
            print("  sc%02d TRIM FAILED" % i, flush=True)
            continue
        print("  sc%02d OK  trimmed to %.3f s  (%.0f s render)" % (i, dur, time.time() - t0),
              flush=True)
        got.append((i, cut))

    print("\n%d/%d scenes rendered in %.1f min" % (len(got), len(plan), (time.time() - t_all) / 60.0))
    if len(got) != len(plan):
        print("NOT stitching — some scenes are missing.")
        return 1

    lst = os.path.join(OUT, "concat.txt")
    with open(lst, "w") as fh:
        for _, f in got:
            fh.write("file '%s'\n" % f)
    silent = os.path.join(OUT, "reins_silent.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c", "copy", silent], check=True)
    final = os.path.join(OUT, "reins_draft.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", SONG,
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "192k", "-shortest", final], check=True)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", final], capture_output=True, text=True).stdout.strip()
    print("\nDRAFT: %s  (%.2f s)" % (final, float(dur or 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())