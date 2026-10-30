#!/usr/bin/env python3
"""REINS — motion spike: which H3 mode actually ANIMATES?

MEASURED 2026-10-05: the Reins scenes rendered through `r2v` are a Ken-Burns ZOOM on the reference
still. Comparing sc05's first and last frame: the figure is 1.5x larger, and its pose is
PIXEL-IDENTICAL — "every detail exactly the same, only scaled up". So the camera move happened and
the figure never moved. That is why the shots read as dead, and why rewriting the beats did not
help: the prompt asked for a blink, a cloak ripple and beating wings, and the pose did not change
at all.

The cause is the MODE, not the wording. VIDEO_MODES says it plainly:

    r2v  -> ref2va   "Character reference"      — its job is PINNING a character
    i2v  -> fl2va    "animate one keyframe"
    t2v  -> fl2va    "no image at all"
    flf  -> fl2va    "ref 1 = first frame, ref 2 = last frame"

So this renders ONE beat through the candidate modes and reports motion energy. Pick on numbers,
not on another 71-minute render.

    python3 tools/reins_spike.py --modes i2v,t2v
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
OUT = os.path.join(PROD, "spike")
os.makedirs(OUT, exist_ok=True)
HOST = "name@forge"
W, H, FPS = 1344, 768, 24.0


def argval(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


MODES = [m.strip() for m in argval("--modes", "i2v,t2v").split(",") if m.strip()]
SCENE = int(argval("--scene", "5"))          # 5 = HERMES, the worst offender
STEPS = int(argval("--steps", "12"))
# --seed: the default is DERIVED FROM THE SCENE NUMBER, so every re-run of a scene reuses the same
# seed and reproduces the same composition. That bit me: sc02 kept coming back pillarboxed with
# ~18% black bars no matter how the beat or the reference changed, because the framing was baked
# into seed 88262, not into the prompt. Override it to explore a different composition.
SEED = int(argval("--seed", str(88000 + SCENE * 131)))
# --guide: r2v + the scene's own song window as an AddGuide anchor. This is the Winner-piece
# recipe (r2v + AddGuide(audio)), and AddGuide is documented as steering the PERFORMANCE —
# which is the thing every no-guide render so far has failed to produce.
GUIDE = "--guide" in sys.argv

PLAN = json.load(open(os.path.join(PROD, "production", "timed-plan-v2.json")))
SC = PLAN["scenes"][SCENE - 1]
DUR = (SC["endMs"] - SC["startMs"]) / 1000.0
FRAMES = fl.h3_frames(DUR)
CAST = SC["cast"][0].upper()
PRODJ = json.load(open(os.path.join(PROD, "production.json")))
REF = os.path.join(PROD, next(r["path"] for r in PRODJ["references"] if r["name"].upper() == CAST))
REF_OVERRIDE = argval("--ref", "")
if REF_OVERRIDE:
    REF = os.path.expanduser(REF_OVERRIDE)
# The app tags <Picture N> only for r2v; i2v/t2v are fl2va modes and take no tag.
BEAT = SC["beat"].replace("<Picture 1> is the subject. ", "")
BEAT_FILE = argval("--beat-file", "")
if BEAT_FILE:
    BEAT = open(os.path.expanduser(BEAT_FILE)).read().strip()
    print("beat: %s (%d words)" % (BEAT_FILE, len(BEAT.split())))
else:
    print("beat: timed-plan-v2 scene %d (%d words)" % (SCENE, len(BEAT.split())))
SUBJECT_TAG = "--subject-tag" in sys.argv
PROMPT = ("<Picture 1> is the subject. " + BEAT) if (GUIDE or SUBJECT_TAG) else BEAT
GUIDE_AUDIO = os.path.join(PROD, "production", "scene-audio", "sc%02d.wav" % SCENE)
if GUIDE and not os.path.exists(GUIDE_AUDIO):
    print("MISSING guide audio: %s" % GUIDE_AUDIO)
    sys.exit(2)


def ssh(cmd, timeout=30):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", HOST, cmd],
                          capture_output=True, text=True, timeout=timeout)


def motion(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=64:36,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(raw) // (64 * 36)
    fr = [raw[i * 64 * 36:(i + 1) * 64 * 36] for i in range(n)]
    if len(fr) < 2:
        return 0.0, 0.0
    d = [sum(abs(x - y) for x, y in zip(a, b)) / (64 * 36) for a, b in zip(fr, fr[1:])]
    return sum(d) / len(d), sum(abs(x - y) for x, y in zip(fr[0], fr[-1])) / (64 * 36)


def collect_media(pid):
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
    return None


def main():
    modes = ["r2v+guide"] if GUIDE else MODES
    print("SPIKE — scene %d (%s), %d fr @ %dx%d, %d steps" % (SCENE, CAST, FRAMES, W, H, STEPS))
    print("  ref : %s" % os.path.basename(REF))
    print("  modes: %s" % ", ".join(modes))
    if GUIDE:
        print("  guide audio: %s" % GUIDE_AUDIO)
    results = []
    for mode in modes:
        vm = "r2v" if mode == "r2v+guide" else mode
        p = {"prompt": PROMPT, "video_mode": vm, "lora": None, "lora_strength": 1.0,
             "steps": STEPS, "length": FRAMES, "width": W, "height": H,
             "seed": SEED, "audio": True, "gpus": 1,
             "prefix": "video/reins_spike_%s" % mode.replace("+", "_")}
        if GUIDE:
            p["guide_audio_path"] = GUIDE_AUDIO
        refs = [REF] if vm in ("r2v", "i2v", "cont") else []
        t0 = time.time()
        try:
            pid, seed = fl.submit("video", p, ref_paths=refs)
        except Exception as exc:
            print("\n  [%s] SUBMIT FAILED — %s: %s" % (mode, type(exc).__name__, str(exc)[:300]))
            results.append((mode, None, None, None))
            continue
        print("\n  [%s] pid=%s rendering…" % (mode, pid), flush=True)
        last = None
        while time.time() - t0 < 2400:
            time.sleep(5)
            st = fl.run_state(pid)
            if st["state"] != last:
                print("    %6.1fs %s" % (time.time() - t0, st["state"]), flush=True)
                last = st["state"]
            if st["state"] in ("done", "error", "gone"):
                break
        if last != "done":
            print("    [%s] state=%s" % (mode, last))
            results.append((mode, None, None, None))
            continue
        f = collect_media(pid)
        if not f:
            print("    [%s] no artifact" % mode)
            results.append((mode, None, None, None))
            continue
        dst = os.path.join(OUT, "scene%02d_%s.mp4" % (SCENE, mode))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f, "-t", "%.3f" % DUR, "-an",
                        "-c:v", "libx264", "-crf", "16", dst], check=True)
        mean, total = motion(dst)
        print("    -> %s   per-frame %.2f   first→last %.2f" % (dst, mean, total), flush=True)
        results.append((mode, mean, total, dst))

    print("\n" + "=" * 62)
    print("%-8s %12s %14s" % ("mode", "per-frame", "first->last"))
    print("%-8s %12.2f %14.2f   <- r2v baseline (v2)" % ("r2v", 0.18, 6.29))
    for mode, m, t, _ in results:
        if m is None:
            print("%-8s %12s %14s" % (mode, "FAILED", ""))
        else:
            print("%-8s %12.2f %14.2f" % (mode, m, t))
    return 0


if __name__ == "__main__":
    sys.exit(main())