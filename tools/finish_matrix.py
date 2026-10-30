#!/usr/bin/env python3
"""GPU-free checks for finish.py — the last pass (sharper / film grain / smooth motion).
    python3 tools/finish_matrix.py"""
import hashlib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_TMP = tempfile.mkdtemp(prefix="finish_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import finish  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def probe(p, what):
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", what[0],
                           "-show_entries", "stream=" + what[1], "-of", "csv=p=0", p],
                          capture_output=True, text=True).stdout.strip()


src = os.path.join(_TMP, "final.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=s=360x640:r=24:d=1.5", "-f", "lavfi", "-i", "sine=d=1.5",
                "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", src], check=True)
h0 = hashlib.sha1(open(src, "rb").read()).hexdigest()

print("§1 the filters")
check(finish.filters(["sharper"], has=lambda f: True) == "cas=0.45" and
      finish.filters(["sharper"], has=lambda f: False).startswith("unsharp="),
      "sharper: cas, or unsharp when this ffmpeg has no cas")
check(finish.filters(["grain", "sharper", "smooth"], has=lambda f: True).split(",")[0]
      .startswith("minterpolate") and finish.filters([]) == "",
      "smooth first, then sharpen, then grain; nothing picked -> nothing")

print("§2 a finished copy")
o1 = finish.finish(src, looks=["sharper", "grain"])
check(o1.endswith("final_finished.mp4") and os.path.exists(o1) and
      hashlib.sha1(open(src, "rb").read()).hexdigest() == h0,
      "the copy sits beside the original; the original is untouched")
check(probe(o1, ("v", "r_frame_rate")) == "24/1" and probe(o1, ("a", "codec_name")) == "aac",
      "sharper + grain: same frame rate, the sound carried over")
o2 = finish.finish(src, os.path.join(_TMP, "smooth.mp4"), looks=["smooth"])
check(probe(o2, ("v", "r_frame_rate")) == "48/1",
      "smooth motion: 48 frames a second")
try:
    finish.finish(src, looks=[])
    e_ = ""
except ValueError as exc:
    e_ = str(exc)
check("pick at least one" in e_, "nothing picked -> a plain message")
check(not [f for f in os.listdir(_TMP) if ".part" in f], "no half-written files left")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
