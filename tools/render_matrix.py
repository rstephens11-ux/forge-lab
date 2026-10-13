#!/usr/bin/env python3
"""REAL RENDERS on forge — one job per mode, at the cheapest legal settings, to prove every
path actually produces a file. Not a layout test: this SUBMITS WORK.

    python3 tools/render_matrix.py --dry          # print the plan and the estimate, submit nothing
    python3 tools/render_matrix.py                # run everything (expect ~30-45 min)
    python3 tools/render_matrix.py --only t2i,ltx # a subset

Why this is a separate tool and not another in-app driver:
  * it imports the app the same way tools/v2v_matrix.py does, so it uses the app's OWN
    `submit()` / `build_graph()` / `collect()` — the same code the buttons call;
  * it never edits the 8,000-line shared file to add a test hook.

⚠️ It REFUSES to start if forge's queue is not empty, so it cannot evict another agent's run.
⚠️ Every case is priced with the app's own `estimate_seconds()` before it is submitted.
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
STILL = os.path.join(HERE, "drafts", "kf-smoketest-char.png")
STILL2 = os.path.join(HERE, "drafts", "kf-video-frame.png")
ALICE = os.path.join(HOME, "forge-lab-assets", "character-ref.png")
CLIP = os.path.join(HERE, "drafts", "chain-1-i2v.mp4")
DRIVE = os.path.join(HERE, "drafts", "chain-2-flf.mp4")
AUDIO = os.path.join(HOME, "forge-lab", "inputs", "_w08.wav")

ONLY = None
for _a in sys.argv[1:]:
    if _a.startswith("--only"):
        ONLY = (_a.split("=", 1)[1] if "=" in _a else "").split(",")
DRY = "--dry" in sys.argv


def frames_of(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
            capture_output=True, text=True).stdout.strip()
        return int(out)
    except Exception:
        return 0


def probe(path):
    """(w, h, frames, fps, seconds) or None."""
    try:
        v = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate",
             "-of", "csv=p=0", path],
            capture_output=True, text=True).stdout.strip()
        d = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
             path], capture_output=True, text=True).stdout.strip()
        w, h, fr, n = v.split(",")
        return int(w), int(h), fr, int(n), float(d)
    except Exception:
        return None


def has_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)


SRC_FRAMES = frames_of(CLIP)
LTX_LEN = fl.ltx_length_for(SRC_FRAMES) if SRC_FRAMES else 121

H3C = {"steps": 4, "length": fl.H3_SCENE_FRAMES.get(1, 124), "width": 544, "height": 736,
       "audio": True, "lora": fl.H3_LONGFORM_LORA, "lora_strength": 1.0, "prefix": "verify_h3"}
WANC = {"quality": "Turbo 4 steps", "width": 832, "height": 480, "length": 81,
        "prefix": "verify_wan"}

CASES = [
    # ── IMAGE ────────────────────────────────────────────────────────────────────────────
    ("img:t2i", "t2i",
     dict({"prompt": "a barn-style house in a Maine pine clearing, 90s cel style",
           "steps": 12, "width": 768, "height": 768, "resolution": 720}, **{}), []),
    ("img:edit", "edit",
     dict({"prompt": "give him a red wool scarf", "steps": 12, "width": 544, "height": 736,
           "resolution": 720, "custom_size": True}), [STILL]),
    ("img:bg", "bg",
     dict({"prompt": fl.BG_PROMPT, "steps": 12, "width": 544, "height": 736,
           "resolution": 720, "custom_size": True}), [STILL]),
    # ── H3 VIDEO — every mode the flags can select ───────────────────────────────────────
    ("h3:r2v", "video", dict(H3C, video_mode="r2v",
                             prompt="<Picture 1> is the character. He walks toward camera."),
     [ALICE]),
    ("h3:i2v", "video", dict(H3C, video_mode="i2v",
                             prompt="animate this frame: he turns his head"), [STILL]),
    ("h3:flf", "video", dict(H3C, video_mode="flf",
                             prompt="he walks between these two positions"), [STILL, STILL2]),
    ("h3:t2v", "video", dict(H3C, video_mode="t2v",
                             prompt="mist over a colonial pine forest, no people"), []),
    ("h3:cont", "video", dict(H3C, video_mode="cont",
                              prompt="he keeps walking"), [STILL]),
    # ── WAN 2.2 ──────────────────────────────────────────────────────────────────────────
    ("wan:t2v", "wan", dict(WANC, wan_mode="t2v",
                            prompt="a fire burning in a stone hearth"), []),
    ("wan:i2v", "wan", dict(WANC, wan_mode="i2v",
                            prompt="he blinks and looks up"), [STILL]),
    ("wan:flf2v", "wan", dict(WANC, wan_mode="flf2v",
                              prompt="he crosses the room"), [STILL, STILL2]),
    ("wan:s2v", "wan", dict(WANC, wan_mode="s2v", s2v_chunks=1, audio_path=AUDIO,
                            prompt="<Picture 1> performs the song"), [STILL]),
    # ── v2v — the two engines that had never been rendered ───────────────────────────────
    ("ltx:ic-lora", "ltx",
     dict({"prompt": "rebuild this clip as a 90s cel animation, muted earth palette",
           "steps": 8, "width": 1280, "height": 704, "source_path": CLIP,
           "src_frames": SRC_FRAMES, "length": LTX_LEN, "prefix": "verify_ltx"}), []),
    ("animate2", "animate2",
     dict({"prompt": "<Picture 1> performs the driving performance", "steps": 6,
           "width": 480, "height": 848, "source_path": DRIVE, "src_frames": frames_of(DRIVE),
           "length": 81, "prefix": "verify_anim2"}), [STILL]),
]


def queue_empty():
    try:
        q = fl.get_json("/queue")
        return not (q.get("queue_running") or q.get("queue_pending"))
    except Exception as exc:
        print("  could not read forge's queue: %s" % exc)
        return False


def main():
    cases = [c for c in CASES if not ONLY or c[0] in ONLY]
    missing = [p for p in (STILL, STILL2, ALICE, CLIP, DRIVE, AUDIO) if not os.path.exists(p)]
    if missing:
        print("MISSING ASSETS (fix before running):")
        for m in missing:
            print("   " + m)
        return 2
    print("render matrix — %d case(s)  ·  source clip %d frames, LTX length %d\n"
          % (len(cases), SRC_FRAMES, LTX_LEN))
    total = 0.0
    for label, mode, p, refs in cases:
        try:
            est = fl.estimate_seconds(mode, p, refs, None)
        except Exception as exc:
            est = None
            print("  %-13s ESTIMATE RAISED %s: %s" % (label, type(exc).__name__, exc))
        total += est or 0.0
        print("  %-13s mode=%-8s est=%s" % (label, mode,
                                            ("%.0f s" % est) if est else "?"))
    print("\n  TOTAL ESTIMATE ≈ %.1f min" % (total / 60.0))
    if DRY:
        print("\n--dry: nothing submitted.")
        return 0
    if not queue_empty():
        print("\nREFUSING TO START: forge's queue is not empty — another run is using the rig.")
        return 3

    results = []
    for label, mode, p, refs in cases:
        print("\n" + "=" * 74)
        print("%s   mode=%s" % (label, mode))
        t0 = time.time()
        try:
            pid, seed = fl.submit(mode, p, refs)
        except Exception as exc:
            print("  SUBMIT FAILED — %s: %s" % (type(exc).__name__, str(exc)[:160]))
            results.append((label, False, "submit: %s" % str(exc)[:60]))
            continue
        print("  pid=%s seed=%s — waiting…" % (pid, seed))
        state, detail = "unknown", ""
        for _ in range(900):
            time.sleep(3)
            rs = fl.run_state(pid)
            state = rs.get("state")
            if state in ("done", "error", "gone"):
                detail = rs.get("detail") or ""
                break
        took = time.time() - t0
        if state != "done":
            print("  STATE=%s after %.0f s  %s" % (state, took, detail[:120]))
            results.append((label, False, "%s %s" % (state, detail[:50])))
            continue
        meta = fl.collect(pid) or {}
        if meta.get("error"):
            print("  COLLECT ERROR — %s" % meta["error"])
            results.append((label, False, "collect: %s" % str(meta["error"])[:50]))
            continue
        # ⚠️ `meta["images"]` is a list of DICTS ({"file": <name>, "node": …, "bytes": …}), and
        # `file` is relative to runs/<pid>/ — not a path. Reading it as a path is the TypeError that
        # killed the first sweep. Every entry is also guarded, so one surprise cannot abort the run.
        files = meta.get("images") or []
        good = []
        for _ent in files:
            try:
                _fn = _ent.get("file") if isinstance(_ent, dict) else _ent
                if not _fn:
                    continue
                f = _fn if os.path.isabs(str(_fn)) else os.path.join(fl.RUNS, pid, str(_fn))
                if not (os.path.exists(f) and os.path.getsize(f) > 5000):
                    print("  small/missing: %r" % (_fn,))
                    continue
                if f.lower().endswith((".mp4", ".webm", ".mkv")):
                    pr = probe(f)
                    if not pr:
                        print("  UNPROBEABLE %s" % os.path.basename(f))
                        continue
                    w, h, fr, n, secs = pr
                    ok = n > 1 and w > 0 and h > 0
                    print("  VIDEO %s  %dx%d  %s  %d frames  %.2f s  %s  audio=%s"
                          % (os.path.basename(f), w, h, fr, n, secs,
                             "OK" if ok else "BAD", has_audio(f)))
                    if ok:
                        good.append(f)
                else:
                    sz = fl.image_size(f)
                    print("  IMAGE %s  %s  %d bytes" % (os.path.basename(f), sz,
                                                        os.path.getsize(f)))
                    if sz:
                        good.append(f)
            except Exception as exc:
                print("  VERIFY ERROR on %r — %s: %s" % (_ent, type(exc).__name__,
                                                         str(exc)[:80]))
        print("  → %s   %.0f s  (%d artifact%s)"
              % ("PASS" if good else "FAIL", took, len(good), "" if len(good) == 1 else "s"))
        results.append((label, bool(good), "%.0fs" % took))

    print("\n" + "=" * 74)
    print("RENDER MATRIX SUMMARY")
    for label, ok, note in results:
        print("  %-4s %-13s %s" % ("ok" if ok else "FAIL", label, note))
    bad = [r for r in results if not r[1]]
    print("\n  %d/%d passed, %d failed" % (len(results) - len(bad), len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())