#!/usr/bin/env python3
"""Beat / tempo grid for a song — the cut-point source Forge Lab has never had.

    /opt/homebrew/bin/python3 tools/beat_grid.py <audio> [--json out.json] [--text]
    /opt/homebrew/bin/python3 tools/beat_grid.py <audio> --json -        # to stdout

WHY THIS EXISTS
---------------
Forge Lab can already burn word captions (Qwen3 forced alignment gives per-word timings) and
it can stitch scenes, but it has **no beat or tempo analysis anywhere** (`grep -ril bpm` over
the tree returns nothing). So a music-video cut cannot be placed on the beat grid — the single
piece the MiniMax `music-video-subtitle-generator` recipe depends on and the one thing this
rig could not do.

NO NEW DEPENDENCIES: ffmpeg decodes, numpy does the DSP. No librosa, no aubio, no scipy
(none of which are installed). Deliberately standalone — it does NOT import forge_lab, so it
cannot collide with the other agent editing the shared file.

WHAT IT IS AND IS NOT
---------------------
It returns a tempo and a beat GRID. It is a *scheduling aid for cuts*, not a transcription:
it does not know about downbeats, bars, or lyrics. Verify the grid against the waveform before
trusting it on an expensive render — see `--text`, which prints the beats for eyeballing.

⚠️ Tempo estimation has a known failure mode: OCTAVE ERROR (reporting 70 when the track is 140,
or vice versa). The autocorrelation is biased toward the 70-160 BPM band, and the result
carries `bpm_confidence` plus the runner-up so a wrong call is visible rather than silent.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import wave

import numpy as np

SR = 22050          # decode rate; enough for onsets, cheap to decode
HOP = 512
WIN = 1024
BPM_LO, BPM_HI = 60.0, 200.0
PREF_BPM = 120.0     # tempo prior, used ONLY to break the N-vs-2N tie (see _comb_scan)
PREF_SIGMA = 0.9     # width of that prior in log2 BPM
OCTAVE_SHARE = 0.85  # a peak this close to the best is treated as the SAME tempo, octave-ambiguous


# --------------------------------------------------------------------------- io
def read_wav(path):
    """Read a PCM wav with the stdlib. Used by the tests (which synthesise their own input)."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("read_wav expects 16-bit PCM")
        n, ch = w.getnframes(), w.getnchannels()
        raw = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
        if ch > 1:
            raw = raw.reshape(-1, ch).mean(axis=1)
        return raw, w.getframerate()


def decode(path, sr=SR):
    """Any audio ffmpeg can read -> mono float32 at `sr`. Raises on failure (no silent zeros)."""
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg could not decode %r: %s" % (path, p.stderr.decode()[:200]))
    x = np.frombuffer(p.stdout, dtype="<f4").astype(np.float32)
    if x.size == 0:
        raise RuntimeError("ffmpeg decoded 0 samples from %r" % path)
    return x, sr


# --------------------------------------------------------------------------- dsp
# ⚠️ TIME MAPPING. `flux[j]` is the change BETWEEN frame j and frame j+1, so the energy of an
# onset first appears in frame j+1. Envelope index j therefore corresponds to time
# (j + FLUX_OFFSET) * hop / sr, NOT j * hop / sr. Getting this wrong puts a constant ~23 ms
# (one hop) bias on every beat — caught by the ground-truth click-track test, not by eye.
FLUX_OFFSET = 1.0


def onset_envelope(x, sr=SR, hop=HOP, win=WIN):
    """Positive spectral flux — the standard onset strength curve. numpy only."""
    if len(x) < win:
        return np.zeros(0, dtype=np.float32)
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    fr = x[idx] * np.hanning(win)[None, :]
    mag = np.abs(np.fft.rfft(fr, axis=1))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    # normalise so the tempo/phase maths is scale-free; guard a silent or flat input
    sd = float(flux.std())
    return (flux - flux.mean()) / sd if sd > 1e-9 else np.zeros_like(flux)


def _comb_score(env, sr, hop, bpm, phases=48):
    """Best mean envelope value a grid at `bpm` can reach, and the phase that achieves it.

    This is a COMB FILTER, and it is used instead of autocorrelation on purpose. Measured:
    autocorrelation could not separate 120 from 60 BPM, because at sr=22050/hop=512 the 120 BPM
    period is **21.53 frames — not an integer** — so no lag lands on it while lag 43 lands exactly
    on two periods, and the half-tempo wins the argmax. A comb scan optimises the thing actually
    wanted (how well a grid sits on the onsets) and is immune to that.
    """
    period = 60.0 / bpm * sr / hop
    if period < 2.0 or env.size < 4:
        return -1e18, 0.0
    idx = np.arange(env.size, dtype=np.float64)
    last_t = env.size + FLUX_OFFSET
    k = np.arange(int(last_t / period) + 1, dtype=np.float64)
    if k.size < 2:
        return -1e18, 0.0
    base = period * k
    best_s, best_ph = -1e18, 0.0
    for ph in np.linspace(0.0, period, phases, endpoint=False):
        t = base + ph
        t = t[t <= last_t]
        if t.size == 0:
            continue
        # mean, not sum: a slower grid has fewer beats and must not win by having fewer samples
        s = float(np.interp(t - FLUX_OFFSET, idx, env).mean())
        if s > best_s:
            best_s, best_ph = s, float(ph)
    return best_s, best_ph


def _scan(env, sr, hop, bpms, phases):
    scores, phs = [], []
    for b in bpms:
        s, ph = _comb_score(env, sr, hop, float(b), phases=phases)
        scores.append(s)
        phs.append(ph)
    return np.asarray(scores), np.asarray(phs)


def tempo_from_envelope(env, sr=SR, hop=HOP, lo=BPM_LO, hi=BPM_HI):
    """Tempo by comb scan. Returns (bpm, confidence, runner_up_bpm).

    Confidence is the winning score's prominence over the median candidate score — so a FLAT
    envelope (silence, a steady drone) scores ~0 rather than fabricating a tempo. That guard is
    load-bearing: without it a zeroed envelope still produced a number out of the lag arithmetic.

    ⚠️ The octave ambiguity is inherent and is REPORTED rather than hidden: `bpm_runner_up` is the
    best score at least 5 BPM away, so a half/double-time call is visible in the JSON.
    """
    bpm, conf, runner, _ph = _comb_scan(env, sr, hop, lo, hi)
    return bpm, conf, runner


def _comb_scan(env, sr, hop, lo, hi):
    """Tempo by comb scan over EVERY local peak, then an octave tie-break by tempo prior.

    Shape of the problem, MEASURED on a 120 BPM click track (sr=22050, hop=512):

        60.0 BPM  score 3.94   <- highest, and WRONG
       120.0 BPM  score 3.63   <- correct, 8% lower
        90.0 BPM  score 1.21
       140.0 BPM  score 0.44

    Two things follow, and both are why the obvious implementations failed here:

    1. **`argmax` is not enough and neither is a tight tie window.** The half-tempo wins by ~8%,
       because a grid at half the tempo lands on half the clicks but also accumulates half the
       period error, so its MEAN is higher. A tolerance tight enough to be safe (5%) excludes the
       right answer; a loose one drags in unrelated tempos. So the tie is resolved on the OCTAVE
       RELATIONSHIP specifically (`OCTAVE_SHARE`), not on a bare score window.
    2. **Refining only around the coarse winner is a trap.** The winner is the WRONG peak, so the
       fine pass never re-examines the right one. Every local peak gets refined here.

    Within the octave-tied set the winner is whichever sits nearest the tempo prior, so N beats
    2N. This yields to the prior for a genuinely slow track (70 vs 140 goes to 140) — that is the
    known, deliberate trade-off of every tempo estimator in this band, and it is why
    `bpm_runner_up` is reported: a wrong call is visible in the JSON rather than silent.
    """
    if env.size < 8:
        return 0.0, 0.0, 0.0, 0.0
    e = env - env.mean()
    if float(e.std()) <= 1e-12:
        return 0.0, 0.0, 0.0, 0.0
    coarse = np.arange(lo, hi + 1e-9, 0.5)
    sc, _ = _scan(e, sr, hop, coarse, 48)
    if not np.isfinite(sc).any() or float(sc.max()) <= -1e17:
        return 0.0, 0.0, 0.0, 0.0
    peaks = [i for i in range(1, len(sc) - 1)
             if sc[i] >= sc[i - 1] and sc[i] >= sc[i + 1] and sc[i] > -1e17]
    if not peaks:
        peaks = [int(np.argmax(sc))]
    gmax = max(float(sc[i]) for i in peaks)
    keep = [i for i in peaks if float(sc[i]) >= 0.5 * gmax] or [int(np.argmax(sc))]
    cb, cs, cp = [], [], []
    for i in keep:
        c = float(coarse[i])
        fine = np.arange(max(lo, c - 0.5), min(hi, c + 0.5) + 1e-9, 0.02)
        if fine.size == 0:
            continue
        s2, p2 = _scan(e, sr, hop, fine, 96)
        j = int(np.argmax(s2))
        cb.append(float(fine[j]))
        cs.append(float(s2[j]))
        cp.append(float(p2[j]))
    if not cb:
        return 0.0, 0.0, 0.0, 0.0
    cb, cs, cp = np.array(cb), np.array(cs), np.array(cp)
    top = float(cs.max())
    tie = np.flatnonzero(cs >= OCTAVE_SHARE * top)
    if tie.size > 1:
        lb = np.log2(cb[tie] / PREF_BPM)
        k = int(tie[int(np.argmax(-(lb ** 2)))])
    else:
        k = int(np.argmax(cs))
    bpm, phase = float(cb[k]), float(cp[k])
    med = float(np.median(sc))
    best = float(cs[k])
    conf = (best - med) / (abs(best) + 1e-12) if best > med else 0.0
    runner = 0.0
    for oi in np.argsort(cs)[::-1]:
        rb = float(cb[oi])
        if abs(rb - bpm) > 5.0:
            runner = rb
            break
    return bpm, conf, runner, phase


def beat_times(env, bpm, sr=SR, hop=HOP, phase=None):
    """The beat GRID: a fitted period and phase, not a hunt for local peaks.

    Snapping each beat to its nearest envelope peak looks appealing and is worse — it inherits the
    peak's jitter and, on a track whose tempo is a hair off, drifts. A rigid fitted grid is what a
    cut schedule actually wants. (Real music has rubato; the grid is an approximation, which is why
    `--text` prints it for eyeballing before an expensive render.)
    """
    if bpm <= 0 or env.size < 4:
        return []
    period = 60.0 / bpm * sr / hop
    if period < 2.0:
        return []
    if phase is None:
        _s, phase = _comb_score(env, sr, hop, bpm, phases=96)
    last_t = env.size + FLUX_OFFSET
    out, k = [], 0
    while True:
        t = phase + period * k
        if t > last_t:
            break
        out.append((t + FLUX_OFFSET) * hop / sr)
        k += 1
    return out


def grid(path, sr=SR):
    """Full analysis of an audio file -> the dict that gets written as JSON."""
    x, sr = (read_wav(path) if path.lower().endswith(".wav") else decode(path, sr))
    env = onset_envelope(x, sr)
    bpm, conf, runner, phase = _comb_scan(env, sr, HOP, BPM_LO, BPM_HI)
    beats = beat_times(env, bpm, sr, hop=HOP, phase=phase)
    return {
        "path": os.path.abspath(path),
        "duration_s": round(len(x) / float(sr), 3),
        "sample_rate": sr,
        "bpm": round(bpm, 2),
        "bpm_confidence": round(conf, 4),
        "bpm_runner_up": round(runner, 2),
        "beat_count": len(beats),
        "beats": [round(t, 4) for t in beats],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="beat/tempo grid for a song (no new deps)")
    ap.add_argument("audio")
    ap.add_argument("--json", metavar="PATH", help="write the grid here ('-' = stdout)")
    ap.add_argument("--text", action="store_true", help="print the beat times")
    ap.add_argument("--sr", type=int, default=SR)
    a = ap.parse_args(argv)
    g = grid(a.audio, a.sr)
    if a.json:
        text = json.dumps(g, indent=2)
        if a.json == "-":
            print(text)
        else:
            with open(a.json, "w") as fh:
                fh.write(text + "\n")
            print("wrote %s" % a.json)
    print("%s\n  %.2f BPM (conf %.3f, runner-up %.2f)  %.2fs  %d beats"
          % (os.path.basename(a.audio), g["bpm"], g["bpm_confidence"],
             g["bpm_runner_up"], g["duration_s"], g["beat_count"]))
    if a.text:
        print("  beats: " + " ".join("%.3f" % t for t in g["beats"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())