#!/usr/bin/env python3
"""GPU-free checks for previews.py — the moving thumbnails (camera moves, your format loops).
    python3 tools/previews_matrix.py"""
import hashlib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_TMP = tempfile.mkdtemp(prefix="previews_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import previews as pv  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def h(p):
    return hashlib.sha1(open(p, "rb").read()).hexdigest()


def size(p):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip()
    return tuple(int(x) for x in o.split(","))


print("§1 camera moves")
pic = os.path.join(_TMP, "pic.png")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=s=720x1280:d=1", "-frames:v", "1", pic], check=True)
got = {mv: pv.camera_move(mv, pic) for mv, _w in __import__("apps").MOVES}
check(all(len(v) == pv.N_FRAMES for v in got.values()),
      "every Make it move camera move has a preview (%r)" % {k: len(v) for k, v in got.items()})
check(size(got["Orbit"][0]) == (112, 112), "small squares (a grid of eight fits)")
moving = [mv for mv, fr in got.items() if h(fr[0]) != h(fr[len(fr) // 2])]
check(set(moving) >= {"Slow push-in", "Pull back reveal", "Orbit", "Crane up", "Whip pan",
                      "Dolly zoom"}, "the moves actually move (%r)" % sorted(moving))
check(h(got["Slow push-in"][3]) != h(got["Pull back reveal"][3]),
      "...and differently from each other")
t0 = os.path.getmtime(got["Orbit"][0])
again = pv.camera_move("Orbit", pic)
check(again == got["Orbit"] and os.path.getmtime(again[0]) == t0,
      "made once per picture (the second ask is the cache)")
check(pv.camera_move("Orbit") and pv.camera_move("Orbit")[0] != got["Orbit"][0],
      "with no picture picked, a sample scene stands in")
check(pv.camera_move("not a move") == [], "an unknown move -> nothing")

print("§2 your formats")
vid = os.path.join(_TMP, "v.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=s=1080x1920:r=24:d=5", "-pix_fmt", "yuv420p", vid], check=True)
jobs = [{"format": "vlog", "state": "done", "final": vid},
        {"format": "ad", "state": "error", "final": None}]
fr = pv.format_loop("vlog", jobs)
check(len(fr) >= 10 and size(fr[0]) == (150, 84), "a format you've made: a flick-book band of "
      "your latest video (%d frames)" % len(fr))
check(len({h(x) for x in fr}) > 5, "...frames from across the video, not one repeated")
check(pv.format_loop("ad", jobs) == [] and pv.format_loop("sketch", jobs) == [],
      "a format with no finished video: nothing (the card stays plain)")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
