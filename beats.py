"""BEATS — find a song's beat so a music video cuts ON it (roadmap 2026-10-18, item 3: Slop
Cannon's whole pitch is song -> video). Pure Python + ffmpeg (no numpy / librosa in the app).

    detect(path) -> {"bpm", "beats": [s...], "downbeats": [s...], "confidence"}
    cut_points(total, beats_info, max_len, min_len=1.5) -> [(start, length)]

How: the song is decoded to 11 kHz mono; a frame's loudness rise is its ONSET strength; the
tempo is the lag (60-180 BPM, nudged toward ~120) where the onset curve best repeats; the
beat grid is the phase of that lag that lands on the most onsets, each beat then snapped to
the strongest onset near it; the downbeat is the one of four phases that's loudest.
Good on music with a steady pulse (pop, rock, hip hop, EDM); rubato or beatless songs come
back with low confidence and the caller falls back to plain cuts.
"""
import array
import math
import subprocess

SR = 11025
HOP = 256                          # ~23 ms per frame
FPS = SR / float(HOP)


def _pcm(path, sr=SR):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr),
                          "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    a = array.array("h")
    a.frombytes(raw[:len(raw) // 2 * 2])
    return a


def onsets(path):
    """[onset strength per frame]"""
    pcm = _pcm(path)
    env = []
    for i in range(0, len(pcm) - HOP, HOP):
        s = 0
        for x in pcm[i:i + HOP:2]:          # every other sample: plenty for loudness
            s += x * x
        env.append(math.log1p(s / (HOP / 2.0)))
    on = [0.0] + [max(0.0, env[k] - env[k - 1]) for k in range(1, len(env))]
    # remove the slow trend so a loud chorus doesn't swamp the pulse
    w = int(FPS * 0.5)
    out = []
    acc = sum(on[:w])
    for k in range(len(on)):
        lo, hi = k - w, k + w
        if hi < len(on):
            acc += on[hi]
        if lo - 1 >= 0:
            acc -= on[lo - 1]
        n = min(hi, len(on) - 1) - max(lo, 0) + 1
        out.append(max(0.0, on[k] - acc / max(1, n)))
    return out


def _tempo(on):
    lo, hi = int(FPS * 60 / 180.0), int(FPS * 60 / 60.0)
    # smoothed first: a beat period that isn't a whole number of frames (140 bpm = 18.4)
    # otherwise scores low at every integer lag, and the double period wins (half tempo)
    k_ = (1.0, 2.0, 3.0, 2.0, 1.0)
    on = [sum(k_[j] * on[i + j - 2] for j in range(5) if 0 <= i + j - 2 < len(on))
          for i in range(len(on))]
    n = len(on)
    best, best_lag, scores = -1.0, None, {}
    for lag in range(max(1, lo), min(hi, n - 1) + 1):
        s = 0.0
        for k in range(n - lag):
            s += on[k] * on[k + lag]
        bpm = 60.0 * FPS / lag
        s *= math.exp(-0.5 * (math.log2(bpm / 120.0) / 0.9) ** 2)   # a gentle pull to ~120
        scores[lag] = s
        if s > best:
            best, best_lag = s, lag
    if best_lag is None:
        return None, 0.0
    # sub-frame lag: a parabola through the peak and its neighbours
    a, b, c_ = scores.get(best_lag - 1, best), best, scores.get(best_lag + 1, best)
    den = a - 2 * b + c_
    frac = 0.5 * (a - c_) / den if den else 0.0
    lag = best_lag + max(-0.5, min(0.5, frac))
    mean = sum(scores.values()) / max(1, len(scores))
    conf = (best - mean) / best if best > 0 else 0.0
    return lag, conf


def detect(path):
    on = onsets(path)
    if len(on) < FPS * 4:
        return {"bpm": None, "beats": [], "downbeats": [], "confidence": 0.0}
    lag, conf = _tempo(on)
    if not lag:
        return {"bpm": None, "beats": [], "downbeats": [], "confidence": 0.0}
    n = len(on)
    # phase: where the grid lands on the most onsets
    best_p, best_s = 0.0, -1.0
    steps = max(1, int(lag))
    for p in range(steps):
        s, t = 0.0, float(p)
        while t < n:
            s += on[int(t)]
            t += lag
        if s > best_s:
            best_p, best_s = float(p), s
    # follow the beat: each next beat is looked for one period after the last one FOUND
    # (a fixed grid drifts: 1% off the tempo is 0.1 s off after 30 beats)
    r = max(2, int(round(lag * 0.15)))
    peak = max(on) or 1.0
    beats, t = [], best_p
    while t < n:
        k0 = int(round(t))
        lo, hi = max(0, k0 - r), min(n - 1, k0 + r)
        k = max(range(lo, hi + 1), key=lambda i: on[i]) if hi >= lo else k0
        if on[k] < 0.15 * peak:                 # nothing there: keep the grid's guess
            k = t
        beats.append(k / FPS)
        t = k + lag
    # downbeat: the loudest of the four phases
    ph = max(range(4), key=lambda q: sum(on[min(n - 1, int(round(b_ * FPS)))]
                                         for b_ in beats[q::4]))
    return {"bpm": round(60.0 * FPS / lag, 1), "beats": [round(b_, 3) for b_ in beats],
            "downbeats": [round(b_, 3) for b_ in beats[ph::4]],
            "confidence": round(max(0.0, min(1.0, conf)), 3)}


def cut_points(total, info, max_len, min_len=1.5):
    """[(start, length)] covering `total` seconds, every cut ON the music: as many whole bars
    as fit in a shot (max_len), else whole beats, else max_len when the song has no clear
    beat. No slice is longer than max_len."""
    beats = [b for b in (info or {}).get("beats") or [] if 0 < b < total]
    downs = set((info or {}).get("downbeats") or [])
    if len(beats) < 4 or (info or {}).get("confidence", 0) < 0.05:
        out, t = [], 0.0
        while total - t > 0.25:
            ln = min(max_len, total - t)
            out.append((round(t, 3), round(ln, 3)))
            t += ln
        return out
    out, t = [], 0.0
    while total - t > 0.25:
        if total - t <= max_len:
            out.append((round(t, 3), round(total - t, 3)))
            break
        window = [b for b in beats if t + min_len <= b <= t + max_len]
        bars = [b for b in window if b in downs]
        if bars:
            cut = bars[-1]                       # the most whole bars that fit
        elif window:
            cut = window[-1]
        else:
            cut = t + max_len
        out.append((round(t, 3), round(cut - t, 3)))
        t = cut
    return out
