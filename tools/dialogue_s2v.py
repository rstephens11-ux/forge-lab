#!/usr/bin/env python3
"""Lip-sync Dialogue lines: each line's voice take + its speaker's portrait -> WAN 2.2 S2V clip,
through Forge Lab's own submit()/wait_and_collect() (so every clip is a normal Take).

  FORGE_LAB_NOAUTOSAVE=1 python3 tools/dialogue_s2v.py PROJECT PORTRAITS.json OUTDIR [idx,idx,...] [--go]

Prints the estimate and does nothing without --go. Refuses unless forge's queue is empty.
Output: OUTDIR/line_NNN.mp4 — trimmed to the line's own speech length, voice muxed in.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dialogue as dlg   # noqa: E402

S2V_COST = {1: 112, 2: 230, 3: 348}          # measured, incl. load (forge_lab.py S2V_* notes)
SIDE = 640
# S2V chunk = 77 frames @ 16 fps; chained lengths 4.81 / 9.81 / 14.81 s
CHUNK_S = {1: 4.81, 2: 9.81, 3: 14.81}


def chunks_for(sec):
    for n in (1, 2, 3):
        if sec <= CHUNK_S[n] - 0.1:
            return n
    return None


def square(src, dst):
    """Head-and-shoulders square from a tall portrait: full width, from the top."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf",
                    "crop=iw:iw:0:ih*0.04,scale=%d:%d" % (SIDE, SIDE), dst], check=True)


PROMPT = ("2D cel-shaded cartoon animation, thick black outlines, flat colours, the same character "
          "as the reference image, talking to someone just off camera, natural lip movement matched "
          "to the speech, small head movements and blinks, plain light background, static camera")


def main():
    args = [a for a in sys.argv[1:] if a != "--go"]
    go = "--go" in sys.argv
    slug, portraits_path, outdir = args[:3]
    only = set(int(x) for x in args[3].split(",")) if len(args) > 3 and args[3] else None
    pj = dlg.load_project(slug)
    proj = os.path.join(dlg.dialogue_dir(), slug)
    portraits = json.load(open(portraits_path))
    os.makedirs(outdir, exist_ok=True)
    jobs = []
    for li in pj["lines"]:
        if only is not None and li["idx"] not in only:
            continue
        spk = dlg.normalise_speaker(li["speaker"])
        if spk not in portraits or "(V.O.)" in li["speaker"].upper():
            continue
        wav = os.path.join(proj, li["takes"][li["kept"]]["file"])
        sec = dlg._ffprobe_duration(wav)
        n = chunks_for(sec)
        if n is None:
            print("skip line %d: %.1f s is longer than S2V's 14.8 s" % (li["idx"], sec)); continue
        out = os.path.join(outdir, "line_%03d.mp4" % li["idx"])
        if os.path.exists(out):
            continue
        jobs.append((li, spk, wav, sec, n, out))
    est = sum(S2V_COST[j[4]] for j in jobs)
    print("%d clips, estimate %.0f s (%.1f min) on GPU0" % (len(jobs), est, est / 60))
    for li, spk, _w, sec, n, _o in jobs:
        print("  line %2d %-7s %4.1f s -> %d chunk(s)" % (li["idx"], spk, sec, n))
    if not go:
        print("(dry run — add --go)"); return
    q = json.loads(urllib.request.urlopen("http://forge:8188/queue", timeout=20).read())
    if q.get("queue_running") or q.get("queue_pending"):
        sys.exit("forge's queue is busy — refusing to evict someone else's run")

    import forge_lab as fl
    sq = {}
    for k, p in portraits.items():
        if k.startswith("_"):
            continue
        sq[k] = os.path.join(outdir, "ref_%s.png" % dlg.slugify(k))
        square(p, sq[k])
    t_all = time.time()
    for i, (li, spk, wav, sec, n, out) in enumerate(jobs):
        t0 = time.time()
        p = {"prompt": PROMPT, "width": SIDE, "height": SIDE, "seed": -1,
             "wan_mode": "s2v", "s2v_chunks": n, "audio_path": wav,
             "quality": fl.S2V_QUALITY, "seconds": CHUNK_S[n]}
        pid, seed = fl.submit("wan", p, [sq[spk]])
        meta = fl.wait_and_collect(pid, timeout=1200) or {}
        src = os.path.join(fl.RUNS, meta.get("id") or pid, meta.get("muxed") or "s2v_synced.mp4")
        if not os.path.exists(src):
            print("line %d FAILED: %s" % (li["idx"], {k: meta.get(k) for k in ("state", "error", "mux_error")}))
            continue
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-t", "%.3f" % (sec + 0.25),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", out], check=True)
        print("line %2d %-7s done in %.0f s (est %d)  [%d/%d]  run %s"
              % (li["idx"], spk, time.time() - t0, S2V_COST[n], i + 1, len(jobs), pid), flush=True)
    print("all done in %.1f min" % ((time.time() - t_all) / 60))


if __name__ == "__main__":
    main()
