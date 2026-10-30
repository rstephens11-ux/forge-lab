#!/usr/bin/env python3
"""LIVE proof for the Studio pipeline: one real 2-shot Draft short with bighead.

Runs against the REAL rig (ComfyUI on GPU0, TTS/align + Director on GPU1) but writes ONLY
into a scratch data dir (FORGE_LAB_DATA, default ~/forge-lab-work/testdata-pipeline), so
Ryan's real runs/ and characters/ are untouched.

    /opt/homebrew/bin/python3 tools/studio_live.py            # price only, submits nothing
    /opt/homebrew/bin/python3 tools/studio_live.py --go       # do the real run

⚠️ Prices first and refuses to submit unless forge's queue is EMPTY, so it cannot evict
someone else's render.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DATA = os.path.expanduser(os.environ.get("FORGE_LAB_DATA")
                          or "~/forge-lab-work/testdata-pipeline")
os.environ["FORGE_LAB_DATA"] = DATA
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))

import forge_core as fc          # noqa: E402
import forge_lab as fl           # noqa: E402
import studio_pipeline as sp     # noqa: E402

fc.configure(data_dir=DATA)

SLUG = "he-tries-on-hats-in-a-shop"

SHOTS = [
    {"n": 1, "seconds": 3, "intent": "lipsync", "engine": "h3", "video_mode": "r2v",
     "camera": "steady medium shot, mirror in frame",
     "action": "holds an enormous wide-brimmed hat, deadpan",
     "line": "I'll take this one.",
     "prompt": "<Picture 1> is the character. The man stands in a small hat shop holding "
               "an enormous wide-brimmed hat, deadpan, facing a mirror. Camera holds steady."},
    {"n": 2, "seconds": 3, "intent": "continue", "engine": "h3", "video_mode": "cont",
     "camera": "same shot, tighter",
     "action": "lowers the giant hat onto his head",
     "line": "",
     "prompt": "He slowly lowers the giant hat onto his head; it sinks down past his eyes."},
]


def queue_empty():
    import urllib.request
    try:
        with urllib.request.urlopen(fl.FORGE + "/queue", timeout=10) as r:
            q = json.loads(r.read().decode())
        return not q.get("queue_running") and not q.get("queue_pending")
    except Exception as exc:
        print("!! could not read forge queue: %r" % exc)
        return False


def ensure_data():
    os.makedirs(os.path.join(DATA, "runs"), exist_ok=True)
    src = os.path.expanduser("~/forge-lab/characters/bighead")
    dst = os.path.join(DATA, "characters", "bighead")
    os.makedirs(dst, exist_ok=True)
    for f in ("character.json", "still.png"):
        s = os.path.join(src, f)
        d = os.path.join(dst, f)
        if os.path.exists(s) and not os.path.exists(d):
            shutil.copyfile(s, d)
    print("data dir : %s" % DATA)
    print("character: %s" % fl.character_still("bighead"))


def build_project():
    pj = sp.load_project(SLUG)
    if pj is None:
        pj = sp.new_project(["bighead"], "he tries on hats in a shop", length_s=6,
                            quality="Draft", title="He tries on hats in a shop", slug=SLUG)
    pj["shots"] = [sp._shot_migrate(dict(s)) for s in SHOTS]
    pj["quality"] = "Draft"
    pj["length_s"] = 6
    sp.save_project(pj)
    return pj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--budget", type=float, default=900.0)
    a = ap.parse_args()

    print("=== ffmpeg capability probe ===")
    for label, exe in (("mac", os.environ.get("FFMPEG", "ffmpeg")),
                       ("forge", sp.FORGE_FFMPEG)):
        has = sp.ffmpeg_has("ass", exe) if label == "mac" else _remote_has("ass")
        print("  %-5s %s -> ass=%s" % (label, exe, has))

    ensure_data()
    pj = build_project()
    est = sp.estimate(pj)
    print("\n=== PRICE (before any submit) ===")
    for e in est["per_shot"]:
        print("  shot %s  %ss  %sx%s  %s steps  ~%s s"
              % (e["n"], e["seconds"], e["canvas"][0], e["canvas"][1], e["steps"],
                 e["estimate_s"]))
    print("  TOTAL ESTIMATE: %s s (budget %s s)" % (est["total_s"], a.budget))
    if est["total_s"] > a.budget:
        print("  !! over budget — would reduce shots/canvas. NOT submitting.")
        return 3

    if not a.go:
        print("\n(dry run — re-run with --go)")
        return 0

    if not queue_empty():
        print("!! forge queue is NOT empty — refusing to submit")
        return 4

    t0 = time.time()
    print("\n=== voice_lines ===", flush=True)
    sp.voice_lines(pj)
    for sh in pj["shots"]:
        print("  shot %s line=%r wav=%s words=%d"
              % (sh["n"], sh.get("line"), sh.get("audio_wav"), len(sh.get("words") or [])))

    print("\n=== render_all (budget %s s) ===" % a.budget, flush=True)
    per = {}
    for sh in pj["shots"]:
        ts = time.time()
        sp.render_shot(pj, sh["n"])
        per[sh["n"]] = time.time() - ts
        print("  shot %s -> %s  %.0f s wall" % (sh["n"], sh.get("chosen"), per[sh["n"]]),
              flush=True)

    print("\n=== assemble ===", flush=True)
    cut = sp.assemble(pj)
    print("  cut: %s" % cut.get("mp4"))

    print("\n=== REPORT ===")
    for sh in pj["shots"]:
        e = next((x for x in est["per_shot"] if x["n"] == sh["n"]), {})
        print("  shot %s  estimated ~%ss  actual %.0fs  chosen=%s"
              % (sh["n"], e.get("estimate_s"), per.get(sh["n"], 0), sh.get("chosen")))
    final = cut.get("mp4")
    print("  final: %s" % final)
    print("  probe: %s" % (fl.video_probe(final),))
    print("  captions: %s" % cut.get("captions"))
    print("  post: %s" % cut.get("post"))
    print("  cover: %s" % cut.get("cover"))

    # extract 3 frames for a vision read
    frames = []
    dur = fl.video_probe(final)["seconds"] if fl.video_probe(final) else 3.0
    for frac, name in ((0.15, "f_start"), (0.5, "f_mid"), (0.95, "f_end")):
        out = os.path.join(DATA, "studio", SLUG, "%s.png" % name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.2f" % (dur * frac),
                        "-i", final, "-frames:v", "1", out], check=False)
        frames.append(out)
        print("  frame: %s" % out)

    print("\n=== caption excerpt ===")
    with open(cut["captions"]) as f:
        for line in f:
            if line.startswith("Dialogue:"):
                print("  " + line.rstrip())

    print("\ntotal wall: %.0f s" % (time.time() - t0))

    # release GPU1 — the Director is no longer needed
    try:
        sp._unload_director()
        print("Director unloaded (GPU1 released)")
    except Exception as exc:
        print("Director unload failed: %r" % exc)
    return 0


def _remote_has(filt):
    try:
        p = subprocess.run(["ssh", sp.FORGE_SSH,
                            "%s -hide_banner -filters | grep -c ' %s '" % (sp.FORGE_FFMPEG, filt)],
                           capture_output=True, text=True, timeout=30)
        return p.stdout.strip() not in ("", "0")
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())