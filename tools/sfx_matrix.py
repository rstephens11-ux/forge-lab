#!/usr/bin/env python3
"""GPU-free checks for sfx.py + the sound effects laid in by studio_pipeline.sound_design.
    python3 tools/sfx_matrix.py"""
import array
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_TMP = tempfile.mkdtemp(prefix="sfx_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import sfx  # noqa: E402
import studio_pipeline as sp  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def energy(path, t0, t1):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t0), "-t", str(t1 - t0), "-i",
                          path, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = array.array("h")
    a.frombytes(raw[:len(raw) // 2 * 2])
    return sum(x * x for x in a) / max(1, len(a))


print("§1 finding a sound")
check(all(sfx.builtin(n) and os.path.getsize(sfx.builtin(n)) > 1000 for n in sfx.BUILTIN),
      "every built-in sound is made (%d)" % len(sfx.BUILTIN))
check(os.path.basename(sfx.find("a knock at the door") or "") == "knock.wav" and
      os.path.basename(sfx.find("he slams his fist down") or "") == "thud.wav" and
      os.path.basename(sfx.find("a phone buzzing") or "") == "phone_buzz.wav",
      "cues find their sound (knock / slams -> thud / buzzing -> phone buzz)")
check(sfx.find("the ocean at night") is None and sfx.find("") is None,
      "no match -> no sound (better than a wrong one)")
mine = os.path.join(sfx.sfx_dir(), "Door Knock.wav")
shutil.copyfile(sfx.builtin("ding"), mine)
check(sfx.find("a knock at the door") == mine and ("Door Knock", mine, "yours") in
      sfx.library(), "a sound you add wins over the built-in")

print("§2 laid in under the line")
vid = os.path.join(_TMP, "cut.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=c=black:s=320x180:r=24:d=4", "-f", "lavfi", "-i",
                "anullsrc=r=44100:cl=stereo", "-t", "4", "-pix_fmt", "yuv420p", "-c:a", "aac",
                vid], check=True)
o0 = sp.sound_design(vid, os.path.join(_TMP, "o0.mp4"))
o1 = sp.sound_design(vid, os.path.join(_TMP, "o1.mp4"), sfx=[(2.0, sfx.builtin("thud"))])
check(energy(o1, 2.0, 2.4) > 20 * max(1.0, energy(o0, 2.0, 2.4)) and
      energy(o1, 0.5, 1.5) < 3 * max(1.0, energy(o0, 0.5, 1.5)),
      "the sound plays at 2.0 s and nowhere else")
sh = {"n": 1, "sfx": "a thud", "cut_in": 1.0, "trim_in": 0.2,
      "words": [{"word": "Hey", "start": 0.3, "end": 0.6}]}
ev = sp.sfx_events({}, [(sh, None)])
check(len(ev) == 1 and abs(ev[0][0] - 1.4) < 1e-6 and ev[0][1].endswith("thud.wav"),
      "a line's sound lands as its first word ends (cut 1.0 + 0.6 - trim 0.2 = %r)" % (ev,))
check(sp.sfx_events({"sfx": False}, [(sh, None)]) == [] and
      sp.sfx_events({}, [(dict(sh, is_reaction=True), None)]) == [],
      "off for the project, and never on a reaction shot")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
