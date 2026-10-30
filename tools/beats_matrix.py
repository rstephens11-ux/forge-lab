#!/usr/bin/env python3
"""GPU-free checks for beats.py — a music video cuts on the beat.
    python3 tools/beats_matrix.py"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_TMP = tempfile.mkdtemp(prefix="beats_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import beats  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def song(path, bpm, first=0.3, secs=30, extra=""):
    p = 60.0 / bpm
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='(0.9*exp(-60*mod(t-%.3f,%.4f))*sin(2*PI*900*t))*"
                    "(1+1.0*lt(mod(t-%.3f,%.4f),0.1))':s=44100:d=%d"
                    % (first, p, first, 4 * p, secs)] + (
        ["-f", "lavfi", "-i", "sine=frequency=110:d=%d" % secs, "-f", "lavfi", "-i",
         "anoisesrc=d=%d:a=0.05" % secs, "-filter_complex", "[0][1][2]amix=inputs=3"]
        if extra == "mix" else []) + [path], check=True)
    return path


print("§1 the beat")
for bpm, first in ((120, 0.3), (96, 0.5), (140, 0.2), (170, 0.25), (80, 0.4)):
    d = beats.detect(song(os.path.join(_TMP, "s%d.wav" % bpm), bpm, first, extra="mix"))
    want = [first + k * 60.0 / bpm for k in range(40)]
    near = [min(abs(b - w) for w in want) for b in d["beats"][:30]]
    check(d["bpm"] and abs(d["bpm"] - bpm) <= 2.5 and max(near) < 0.05,
          "%d bpm under a drone and noise: found %.1f bpm, beats within %.0f ms"
          % (bpm, d["bpm"] or 0, 1000 * max(near or [9])))
    acc = [first + k * 4 * 60.0 / bpm for k in range(12)]
    check(all(min(abs(b - a) for a in acc) < 0.05 for b in d["downbeats"][:6]),
          "...and the downbeat (the accented beat) is found")
q = os.path.join(_TMP, "noise.wav")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=20:a=0.3",
                q], check=True)
dn = beats.detect(q)
_d120 = beats.detect(os.path.join(_TMP, "s120.wav"))
check(dn["confidence"] < _d120["confidence"] - 0.3,
      "plain noise reads as much less sure than a real beat (%.2f vs %.2f)"
      % (dn["confidence"], _d120["confidence"]))

print("§2 cuts on the music")
d120 = beats.detect(os.path.join(_TMP, "s120.wav"))
cp = beats.cut_points(20, d120, 5.04)
cuts = [s_ for s_, _l in cp[1:]]
check(cp[0][0] == 0 and abs(sum(l_ for _s, l_ in cp) - 20) < 0.01 and
      all(l_ <= 5.04 + 1e-6 for _s, l_ in cp),
      "the slices cover the song, none longer than a shot (%r)" % cp)
check(all(min(abs(c_ - d_) for d_ in d120["downbeats"]) < 0.01 for c_ in cuts),
      "every cut is ON a downbeat (a bar line)")
plain = beats.cut_points(12, {}, 5.04)
check([round(l_, 2) for _s, l_ in plain] == [5.04, 5.04, 1.92],
      "no beat: plain shot-length cuts, as before")
import quick  # noqa: E402
ms = quick.music_slices(20, beats_info=d120)
check(all(fc.h3_frames(sec) / 24.0 >= ln - 1e-3 for _s, ln, sec in ms) and
      [round(s_, 3) for s_, _l, _x in ms] == [s_ for s_, _l in cp],
      "Quick's music video uses those cuts; each shot is long enough for its slice")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
