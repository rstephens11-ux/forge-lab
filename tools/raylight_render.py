#!/usr/bin/env python3
"""REAL RAYLIGHT RENDERS — prove the 2-GPU H3 path end-to-end through the app's own code.

    python3 tools/raylight_render.py --dry     # plan + estimate, submit nothing
    python3 tools/raylight_render.py           # render 1-card and 2-card, compare

Same discipline as tools/render_matrix.py: it imports the app and calls the app's OWN
`build_graph()` / `submit()` / `run_state()` / `collect()` — the code the buttons call —
rather than adding a test hook to the shared file.

⚠️ REFUSES to start if forge's queue is not empty (cannot evict another agent's run).
⚠️ The 2-GPU form is NOT a transparent speed switch: same seed, different film, and at a
   small canvas the ~60 s fixed load makes it SLOWER than one card. That is the point of
   rendering both here — the size-dependence is a measurement, not a footnote.
"""
import importlib.util
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

HOME = os.path.expanduser("~")
ALICE = os.path.join(HOME, "forge-lab-assets", "character-ref.png")

DRY = "--dry" in sys.argv

# Cheapest legal H3 settings — mirrors render_matrix.py's H3C.
BASE = {"steps": 4, "length": 124, "width": 544, "height": 736, "audio": True,
        "lora": fl.H3_LONGFORM_LORA, "lora_strength": 1.0,
        "video_mode": "r2v",
        "prompt": "<Picture 1> is the character. He walks toward camera."}

CASES = [("h3:r2v 1card", dict(BASE, gpus=1, prefix="verify_ray1")),
         ("h3:r2v 2card", dict(BASE, gpus=2, prefix="verify_ray2"))]


def probe(path):
    try:
        v = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate",
             "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
        w, h, fr, n = v.split(",")
        return int(w), int(h), fr, int(n)
    except Exception:
        return None


def has_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream=index", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)


def queue_empty():
    try:
        q = fl.get_json("/queue")
        return not (q.get("queue_running") or q.get("queue_pending"))
    except Exception as exc:
        print("  could not read forge's queue: %s" % exc)
        return False


def main():
    if not os.path.exists(ALICE):
        print("MISSING ASSET: %s" % ALICE)
        return 2
    print("raylight render test — %d case(s)\n" % len(CASES))
    for label, p in CASES:
        try:
            est = fl.estimate_seconds("video", p, [ALICE], None)
        except Exception as exc:
            est = None
            print("  %-14s ESTIMATE RAISED %s: %s" % (label, type(exc).__name__, exc))
        print("  %-14s gpus=%s  est=%s" % (label, p["gpus"],
                                           ("%.0f s" % est) if est else "?"))
    if DRY:
        print("\n--dry: nothing submitted.")
        return 0
    if not queue_empty():
        print("\nREFUSING TO START: forge's queue is not empty.")
        return 3

    results = []
    for label, p in CASES:
        print("\n" + "=" * 74)
        print(label)
        t0 = time.time()
        try:
            pid, seed = fl.submit("video", p, [ALICE])
        except Exception as exc:
            print("  SUBMIT FAILED — %s: %s" % (type(exc).__name__, str(exc)[:300]))
            results.append((label, False, "submit"))
            continue
        print("  pid=%s seed=%s — waiting…" % (pid, seed))
        state, detail = "unknown", ""
        for _ in range(600):
            time.sleep(3)
            rs = fl.run_state(pid)
            state = rs.get("state")
            if state in ("done", "error", "gone"):
                detail = rs.get("detail") or ""
                break
        took = time.time() - t0
        if state != "done":
            print("  STATE=%s after %.0f s  %s" % (state, took, detail[:300]))
            results.append((label, False, "%s %s" % (state, detail[:60])))
            continue
        meta = fl.collect(pid) or {}
        if meta.get("error"):
            print("  COLLECT ERROR — %s" % meta["error"])
            results.append((label, False, "collect"))
            continue
        good = []
        for ent in (meta.get("images") or []):
            try:
                fn = ent.get("file") if isinstance(ent, dict) else ent
                if not fn:
                    continue
                f = fn if os.path.isabs(str(fn)) else os.path.join(fl.RUNS, pid, str(fn))
                if not (os.path.exists(f) and os.path.getsize(f) > 5000):
                    print("  small/missing: %r" % (fn,))
                    continue
                pr = probe(f)
                if pr:
                    w, h, fr, n = pr
                    ok = n > 1 and w > 0 and h > 0
                    print("  VIDEO %s  %dx%d  %s  %d frames  audio=%s  %s"
                          % (os.path.basename(f), w, h, fr, n, has_audio(f),
                             "OK" if ok else "BAD"))
                    if ok:
                        good.append(f)
            except Exception as exc:
                print("  VERIFY ERROR on %r — %s" % (ent, exc))
        print("  → %s   %.0f s  (%d artifact%s)"
              % ("PASS" if good else "FAIL", took, len(good), "" if len(good) == 1 else "s"))
        results.append((label, bool(good), "%.0fs" % took))

    print("\n" + "=" * 74)
    print("RAYLIGHT RENDER SUMMARY")
    for label, ok, note in results:
        print("  %-4s %-14s %s" % ("ok" if ok else "FAIL", label, note))
    bad = [r for r in results if not r[1]]
    print("\n  %d/%d passed, %d failed" % (len(results) - len(bad), len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())