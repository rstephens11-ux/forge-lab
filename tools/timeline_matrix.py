#!/usr/bin/env python3
"""GPU-free checks for the Cut page's TIMELINE in the edit (studio_pipeline.assemble):
leave a shot out, start / end it earlier or later, change the order.
    python3 tools/timeline_matrix.py"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_TMP = tempfile.mkdtemp(prefix="timeline_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import studio_pipeline as sp  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def run(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + list(a), check=True)


sp.get_director = lambda: (_ for _ in ()).throw(RuntimeError("no writer in tests"))
LINES = ("First line here.", "Second line now.", "Third line ends.")
shots = []
for i, line in enumerate(LINES):
    pid = "tl%d" % i
    d_ = os.path.join(fc.RUNS, pid)
    os.makedirs(d_, exist_ok=True)
    run("-f", "lavfi", "-i", "color=c=0x%02x5070:s=360x640:r=24:d=3" % (40 * i + 30), "-f",
        "lavfi", "-i", "sine=frequency=400:d=3", "-shortest", "-pix_fmt", "yuv420p", "-c:a",
        "aac", os.path.join(d_, "clip.mp4"))
    w_ = os.path.join(_TMP, "v%d.wav" % i)
    run("-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=24000:d=1.4" % (300 + 50 * i),
        "-ac", "1", w_)
    shots.append(sp._shot_migrate({
        "n": i + 1, "speaker": "ann", "chosen": pid, "status": "done", "video_mode": "r2v",
        "line": line, "audio_wav": w_,
        "words": [{"word": w, "start": 0.2 + 0.3 * k, "end": 0.45 + 0.3 * k}
                  for k, w in enumerate(line.split())]}))
pj = {"slug": "tl-test", "aspect": "9:16", "characters": ["ann"], "captions": True,
      "sound_design": False, "trim": True, "shots": shots}
os.makedirs(sp.project_dir("tl-test"), exist_ok=True)


def cut():
    sp.assemble(pj)
    lst = open(os.path.join(sp.project_dir("tl-test"), "cut", "concat_list.txt")).read()
    ass = open(os.path.join(sp.project_dir("tl-test"), "cut", "captions.ass")).read()
    return [ln.split("/")[-1].rstrip("'") for ln in lst.splitlines() if ln], ass


order0, ass0 = cut()
base = {sh["n"]: (sh["trim_in"], sh["trim_out"]) for sh in pj["shots"]}
d0 = sp._duration(pj["cut"]["mp4"])
check(order0 == ["n01.mp4", "n02.mp4", "n03.mp4"] and "SECOND LINE" in ass0.upper(),
      "the plain cut: three shots in order, every line captioned")

print("§1 leave it out")
sp._shot(pj, 2)["skip"] = True
order1, ass1 = cut()
check(order1 == ["n01.mp4", "n03.mp4"] and "SECOND LINE" not in ass1.upper() and
      sp._duration(pj["cut"]["mp4"]) < d0 - 0.5,
      "a shot left out is gone from the picture, the captions and the length")
sp._shot(pj, 2)["skip"] = False
sp._shot(pj, 2)["chosen"] = None
sp._shot(pj, 2)["skip"] = True
try:
    cut()
    _ok = True
except RuntimeError:
    _ok = False
check(_ok, "a left-out shot doesn't need to be made at all")
sp._shot(pj, 2)["chosen"], sp._shot(pj, 2)["skip"] = "tl1", False

print("§2 start / end earlier or later")
sp._shot(pj, 1)["nudge_in"] = 0.2
sp._shot(pj, 3)["nudge_out"] = -0.3
cut()
s1, s3 = sp._shot(pj, 1), sp._shot(pj, 3)
check(abs(s1["trim_in"] - (base[1][0] + 0.2)) < 0.01 and
      abs(s3["trim_out"] - (base[3][1] - 0.3)) < 0.01,
      "start +0.2 s and end -0.3 s move exactly those trims (%.2f->%.2f, %.2f->%.2f)"
      % (base[1][0], s1["trim_in"], base[3][1], s3["trim_out"]))
s1["nudge_in"] = 9.0
cut()
check(s1["trim_out"] - s1["trim_in"] >= 0.29, "a nudge can never make a shot vanish")
s1["nudge_in"], s3["nudge_out"] = 0, 0

print("§3 order")
pj["shots"][0], pj["shots"][2] = pj["shots"][2], pj["shots"][0]
order3, _a = cut()
check(order3 == ["n03.mp4", "n02.mp4", "n01.mp4"], "the cut follows the new order (%r)" % order3)

print("§4 a beat-cut music video: each clip is cut to its slice of the song")
pj["shots"].sort(key=lambda s_: s_["n"])
pj["music_video"], pj["captions"] = True, False
for k_, s_ in enumerate(pj["shots"]):
    s_["slice_len"] = (1.5, 2.0, 1.25)[k_]
cut()
check(abs(sp._duration(pj["cut"]["mp4"]) - 4.75) < 0.15 and
      [round(s_["trim_out"], 2) for s_ in pj["shots"]] == [1.5, 2.0, 1.25],
      "slices of 1.5 + 2.0 + 1.25 s -> a 4.75 s video, every cut where the song cuts (%.2f)"
      % sp._duration(pj["cut"]["mp4"]))

print("§5 the mid-scene glance is a J-cut (no silent beat, no jump cut)")
import array  # noqa: E402


def loud(path, t0, t1):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % t0, "-t", "%.3f" % (t1 - t0),
                          "-i", path, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a_ = array.array("h")
    a_.frombytes(raw[:len(raw) // 2 * 2])
    return sum(x * x for x in a_) / max(1, len(a_))


sh4 = []
for i in range(4):
    w_ = os.path.join(_TMP, "j%d.wav" % i)
    run("-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=24000:d=2.6" % (300 + 60 * i),
        "-ac", "1", w_)
    sh4.append(sp._shot_migrate({
        "n": i + 1, "speaker": ("ann", "bob")[i % 2], "chosen": "tl%d" % (i % 3),
        "status": "done", "video_mode": "set", "line": "One two three four five six.",
        "audio_wav": w_, "words": [{"word": w, "start": 0.1 + 0.4 * k, "end": 0.4 + 0.4 * k}
                                   for k, w in enumerate("One two three four five six.".split())]}))
for k, spk in enumerate(("ann", "bob")):
    sh4.append(sp._shot_migrate({"n": 5 + k, "speaker": spk, "chosen": "tl0", "status": "done",
                                 "video_mode": "set", "is_reaction": True}))
pj4 = {"slug": "jcut", "aspect": "9:16", "characters": ["ann", "bob"], "captions": True,
       "sound_design": False, "shots": sh4}
os.makedirs(sp.project_dir("jcut"), exist_ok=True)
_moves = {}
_norm0 = sp._normalise


def _rec(src, out, **kw):
    _moves[os.path.basename(out)] = kw.get("move")
    return _norm0(src, out, **kw)


sp._normalise = _rec
sp.assemble(pj4)
sp._normalise = _norm0
_s2 = sp._shot(pj4, 2)
lst = [ln.split("/")[-1].rstrip("'") for ln in open(os.path.join(
    sp.project_dir("jcut"), "cut", "concat_list.txt")).read().splitlines() if ln]
ca_at = _s2["cut_out"] - _s2["j_overlap"]
check("n105.mp4" in lst and lst.index("n105.mp4") == lst.index("n02.mp4") + 1 and
      _s2.get("voice_out", 0) > _s2["trim_out"] + 0.05,
      "the line before the glance ends its picture early; its voice runs on (%r)" % lst)
check(loud(pj4["cut"]["mp4"], ca_at + 0.1, ca_at + 0.5) > 1000,
      "...so the glance at the listener is under the speaker's last words, not silence")
check(abs(_s2["cut_out"] - (ca_at + _s2["j_overlap"])) < 0.01,
      "...and the line's captions may run on over the glance")
check(_moves.get("n105.mp4") == "tight" and "1.350" in sp._move_filter("tight", 100, 100, 1.0),
      "the glance is framed tighter than the listener's own speaking shot (no jump cut)")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
