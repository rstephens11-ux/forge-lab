#!/usr/bin/env python3
"""Prove dialogue.py's ffmpeg calls are now bounded (no orphaned runaways).

1. A deliberately infinite ffmpeg (anullsrc -> null muxer, writes nothing to
   disk) must raise RuntimeError at the timeout, not hang.
2. assemble() normal + zero-gap cases must still work.
Exits 0 when both hold.
"""
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIALOGUE_PATH = os.path.join(ROOT, "dialogue.py")

with open(DIALOGUE_PATH, "rb") as f:
    src = f.read()
dlg = types.ModuleType("dialogue")
dlg.__file__ = DIALOGUE_PATH
exec(compile(src, DIALOGUE_PATH, "exec"), dlg.__dict__)

fails = []

# ---- 1. the timeout guard --------------------------------------------------
print("1  _run_ffmpeg kills a hung graph instead of leaking it")
before = set(subprocess.run(["pgrep", "-f", "anullsrc"], capture_output=True,
                            text=True).stdout.split())
t0 = os.times().elapsed
try:
    dlg._run_ffmpeg(["ffmpeg", "-y", "-f", "lavfi", "-i",
                     "anullsrc=cl=mono:r=24000", "-f", "null", "-"],
                    timeout=3)
    fails.append("expected RuntimeError, got clean return")
    print("  FAIL  no timeout raised")
except RuntimeError as e:
    dt = os.times().elapsed - t0
    ok = "timed out" in str(e) and dt < 12
    print("  %s  raised after %.1fs: %s" % ("ok  " if ok else "FAIL", dt,
                                            str(e).splitlines()[0]))
    if not ok:
        fails.append("timeout did not fire promptly (%ss): %s" % (dt, e))
except Exception as e:  # noqa
    fails.append("wrong exception: %r" % e)
    print("  FAIL  wrong exception: %r" % e)

# No residual ffmpeg left behind
after = set(subprocess.run(["pgrep", "-f", "anullsrc"], capture_output=True,
                           text=True).stdout.split())
leaked = after - before
print("  %s  no leftover ffmpeg process" % ("ok  " if not leaked else "FAIL"))
if leaked:
    fails.append("leaked processes: %s" % leaked)

# ---- 2. assemble still works ----------------------------------------------
print("2  assemble() normal + zero-gap still produce audio")
tmp = tempfile.mkdtemp(prefix="timeout_check_")

def sine(path, freq):
    import array as _a
    import math as _m
    import wave as _w
    n = 24000
    s = _a.array("h", (int(10000 * _m.sin(2 * _m.pi * freq * t / 24000))
                       for t in range(n)))
    with _w.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(s.tobytes())

c1 = os.path.join(tmp, "c1.wav"); sine(c1, 440)
c2 = os.path.join(tmp, "c2.wav"); sine(c2, 880)

d350 = dlg.assemble([c1, c2], os.path.join(tmp, "gap.wav"), gap_ms=350)
print("  %s  350ms gap -> %.3fs (expect ~2.35)" % ("ok  " if abs(d350 - 2.35) < 0.06 else "FAIL", d350))
if abs(d350 - 2.35) >= 0.06:
    fails.append("350ms assemble wrong: %s" % d350)

d0 = dlg.assemble([c1, c2], os.path.join(tmp, "zero.wav"), gap_ms=0)
print("  %s  zero gap -> %.3fs (expect ~2.0)" % ("ok  " if abs(d0 - 2.0) < 0.06 else "FAIL", d0))
if abs(d0 - 2.0) >= 0.06:
    fails.append("zero-gap assemble wrong: %s" % d0)

print()
if fails:
    print("FAILURES:")
    for m in fails:
        print("  -", m)
    sys.exit(1)
print("all good")
