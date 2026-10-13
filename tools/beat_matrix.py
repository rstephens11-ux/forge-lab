#!/usr/bin/env python3
"""GPU-free assertions for tools/beat_grid.py — the tempo/beat grid.

    /opt/homebrew/bin/python3 tools/beat_matrix.py

No ffmpeg, no forge, no GPU, no network: the click tracks are SYNTHESISED here with numpy and
written as PCM wav, so every tempo in the test has a known ground truth to be scored against.
A detector that only agrees with itself proves nothing — the whole point is that the answer is
known before the analysis runs.

⚠️ Silence is a case, not an edge case: a beat detector that reports a CONFIDENT tempo on a
silent file is lying, and that lie would place every cut at a fabricated beat.
"""
import os
import sys
import tempfile
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import beat_grid as B  # noqa: E402

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def click_track(bpm, seconds, sr=22050, accent_every=None):
    """An impulse train at a KNOWN tempo. Ground truth, not a recording."""
    n = int(round(seconds * sr))
    x = np.zeros(n, dtype=np.float32)
    period = 60.0 / bpm
    k = 0
    while True:
        t = k * period
        if t >= seconds:
            break
        i, L = int(round(t * sr)), int(round(0.005 * sr))
        if i + L < n:
            env = np.exp(-np.linspace(0.0, 6.0, L))
            amp = 0.9 if (accent_every and k % accent_every == 0) else 0.5
            x[i:i + L] += amp * np.sin(2 * np.pi * 1000 * np.arange(L) / sr) * env
        k += 1
    return x, sr


def write_wav(path, x, sr):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def nearest_err(beats, bpm):
    """Median distance from each detected beat to the nearest TRUE beat."""
    if not beats:
        return float("inf")
    period = 60.0 / bpm
    errs = []
    for t in beats:
        k = round(t / period)
        errs.append(abs(t - k * period))
    return float(np.median(errs))


TMP = tempfile.mkdtemp(prefix="beatgrid_")
print("\nTEMPO RECOVERY · synthesised click tracks with a KNOWN bpm")
CASES = [(90.0, 12.0, None), (120.0, 10.0, None), (140.0, 10.0, None),
         (128.0, 12.0, 4)]          # the last is ACCENTED every 4th beat
for bpm, secs, accent in CASES:
    x, sr = click_track(bpm, secs, accent_every=accent)
    p = os.path.join(TMP, "click_%g.wav" % bpm)
    write_wav(p, x, sr)
    g = B.grid(p)
    tag = " (accented 4/4)" if accent else ""
    check(abs(g["bpm"] - bpm) <= 0.015 * bpm,
          "%g BPM recovered as %.2f (within 1.5%%)%s" % (bpm, g["bpm"], tag))
    check(nearest_err(g["beats"], bpm) < 0.025,
          "%g BPM beats land on the true grid (median err %.1f ms)"
          % (bpm, 1000 * nearest_err(g["beats"], bpm)))
    expect = int(secs / (60.0 / bpm))
    check(abs(g["beat_count"] - expect) <= 2,
          "%g BPM found %d beats (expected ~%d)" % (bpm, g["beat_count"], expect))

print("\n⚠️ SILENCE — a confident tempo here would fabricate every cut point")
_sil = os.path.join(TMP, "silence.wav")
write_wav(_sil, np.zeros(int(6.0 * 22050), dtype=np.float32), 22050)
_gs = B.grid(_sil)
check(_gs["bpm_confidence"] < 0.15,
      "silence reports LOW confidence (%.4f), not a confident tempo" % _gs["bpm_confidence"])
check(_gs["duration_s"] > 5.9, "silence still reports its true duration (%.2fs)" % _gs["duration_s"])

print("\nONSET ENVELOPE · the analysis is scale-free but not signal-free")
_x, _sr = click_track(120.0, 6.0)
_e = B.onset_envelope(_x, _sr)
check(_e.size > 100, "the envelope has frames (%d)" % _e.size)
check(abs(float(_e.mean())) < 1e-6, "the envelope is mean-centred (%.2e)" % float(_e.mean()))
check(B.onset_envelope(np.zeros(22050, dtype=np.float32)).size > 0,
      "a silent buffer yields an envelope, not a crash")
_flat = B.onset_envelope(np.zeros(22050, dtype=np.float32))
check(float(np.abs(_flat).max()) == 0.0, "and that envelope is FLAT (no invented onsets)")
check(B.tempo_from_envelope(np.zeros(200, dtype=np.float32))[0] == 0.0,
      "a flat envelope yields bpm 0.0, not a guess")

print("\nREAL SONG · smoke test against an actual mixed track (skipped if absent)")
_real = os.path.expanduser(
    "~/nous-hermes-commercial/Genesis-Three-Scene-Kit/UPLOAD-AUDIO-Genesis-30s.mp3")
if os.path.exists(_real):
    g = B.grid(_real)
    check(B.BPM_LO <= g["bpm"] <= B.BPM_HI,
          "the real song lands in the legal band (%.2f BPM)" % g["bpm"])
    check(g["beat_count"] > 10 and g["beats"][-1] <= g["duration_s"] + 0.05,
          "every beat lies inside the track (last %.3fs of %.2fs)"
          % (g["beats"][-1] if g["beats"] else -1, g["duration_s"]))
    check(29.0 <= g["duration_s"] <= 31.0, "the 30s song measures ~30s (%.2fs)" % g["duration_s"])
    print("       -> %.2f BPM, conf %.3f, runner-up %.2f"
          % (g["bpm"], g["bpm_confidence"], g["bpm_runner_up"]))
else:
    print("  --   real song not present; smoke test skipped")

print("\nPY COMPILE")
import py_compile  # noqa: E402
try:
    py_compile.compile(os.path.join(HERE, "beat_grid.py"), doraise=True)
    check(True, "beat_grid.py compiles")
except py_compile.PyCompileError as exc:
    check(False, "beat_grid.py compiles: %s" % exc)

print("\n%s — %d checks failed" % ("FAIL" if FAILS else "ALL GREEN", len(FAILS)))
sys.exit(1 if FAILS else 0)