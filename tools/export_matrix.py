#!/usr/bin/env python3
"""GPU-free checks for export_pack.py — one click -> tall / square / wide + cover + .srt +
caption. Builds a real two-shot tall project from lavfi clips and cuts it with ffmpeg.
    python3 tools/export_matrix.py"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_TMP = tempfile.mkdtemp(prefix="export_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import studio_pipeline as sp  # noqa: E402
import export_pack as ex  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def run(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + list(a), check=True)


def dims(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", p],
                         capture_output=True, text=True).stdout.strip()
    return tuple(int(x) for x in out.split(",")) if out else None


def pixel(p, x, y):
    """mean brightness of a 4x4 patch at (x, y) of the frame at 0.5 s"""
    seek = [] if p.endswith(".png") else ["-ss", "0.5"]
    raw = subprocess.run(["ffmpeg", "-v", "error"] + seek + ["-i", p, "-frames:v", "1",
                          "-vf", "crop=4:4:%d:%d,format=gray" % (x, y), "-f", "rawvideo",
                          "-"], capture_output=True).stdout
    return sum(raw) / max(1, len(raw))


def busy_band(p, y0, y1):
    """max - min brightness across a band of the picture: text in it -> a big spread"""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-frames:v", "1", "-vf",
                          "crop=iw:%d:0:%d,format=gray" % (y1 - y0, y0), "-f", "rawvideo",
                          "-"], capture_output=True).stdout
    return (max(raw) - min(raw)) if raw else -1


# a tall project: two lines, each a 720x1280 clip with a WHITE top-left marker (so a crop
# vs a blur-fit can be told apart) and a voice wav
sp.get_director = lambda: (_ for _ in ()).throw(RuntimeError("no writer in tests"))
for i, pid in enumerate(("expa", "expb")):
    d_ = os.path.join(fc.RUNS, pid)
    os.makedirs(d_, exist_ok=True)
    run("-f", "lavfi", "-i", "color=c=0x305070:s=720x1280:r=24:d=3", "-f", "lavfi", "-i",
        "sine=frequency=400:d=3", "-vf", "drawbox=x=300:y=40:w=120:h=120:color=white:t=fill",
        "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", os.path.join(d_, "clip.mp4"))
wavs = []
for i in range(2):
    w_ = os.path.join(_TMP, "v%d.wav" % i)
    run("-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=24000:d=1.6" % (300 + 80 * i),
        "-ac", "1", w_)
    wavs.append(w_)
pj = {"slug": "exp-test", "title": "Ears in the soup", "aspect": "9:16",
      "characters": ["ann"], "captions": True, "caption_look": "pop",
      "hook_text": "He has never seen a cloud before", "sound_design": False,
      "shots": [sp._shot_migrate({"n": i + 1, "speaker": "ann", "chosen": pid,
                                  "status": "done", "video_mode": "r2v",
                                  "line": ("Your enormous ears are in my soup.",
                                           "They are load-bearing ears.")[i],
                                  "audio_wav": wavs[i],
                                  "words": [{"word": w, "start": 0.2 + 0.25 * k,
                                             "end": 0.42 + 0.25 * k}
                                            for k, w in enumerate(
                                                ("Your enormous ears are in my soup.",
                                                 "They are load-bearing ears.")[i].split())]})
                for i, pid in enumerate(("expa", "expb"))]}
os.makedirs(sp.project_dir(pj["slug"]), exist_ok=True)
sp.save_project(pj)
sp.assemble(pj)
pj = sp.load_project("exp-test")
before = open(sp.project_path("exp-test")).read()

print("§1 the pack")
res = ex.export("exp-test", progress=lambda m: None)
check(not res["errors"] and set(res["videos"]) == {"9:16", "1:1", "16:9"},
      "three shapes made (%r)" % res["errors"])
check(dims(res["videos"]["9:16"]) == (1080, 1920) and dims(res["videos"]["1:1"]) ==
      (1080, 1080) and dims(res["videos"]["16:9"]) == (1920, 1080),
      "tall 1080x1920, square 1080x1080, wide 1920x1080")
check(open(res["videos"]["9:16"], "rb").read() == open(pj["cut"]["mp4"], "rb").read(),
      "the video's own shape is the finished cut itself")
check(open(sp.project_path("exp-test")).read() == before,
      "the project itself is untouched by the re-cuts")
check(abs(sp._duration(res["videos"]["1:1"]) - sp._duration(pj["cut"]["mp4"])) < 0.2 and
      abs(sp._duration(res["videos"]["16:9"]) - sp._duration(pj["cut"]["mp4"])) < 0.2,
      "every shape runs the same length")
# the wide one from a tall source keeps the WHOLE shot over a blur: the dark-left edge is
# the blurred background, the centre column is the clip (its white marker visible)
check(pixel(res["videos"]["16:9"], 40, 900) < pixel(res["videos"]["16:9"], 960, 900) - 10,
      "tall -> wide: the whole shot over a darkened blur, not a crop of a sliver")
check(ex.recut_plan("9:16", "1:1") == ("fill", 0.38) and
      ex.recut_plan("9:16", "16:9")[0] == "blur" and ex.recut_plan("16:9", "9:16")[0] ==
      "blur" and ex.recut_plan("16:9", "1:1")[0] == "fill",
      "square is a crop aimed a little high; the opposite orientation is a blur-fit")
srt = open(res["srt"]).read()
check(srt.startswith("1\n00:00:0") and "-->" in srt and "ENORMOUS" not in srt and
      "enormous" in srt.lower() and srt.count("-->") >= 3,
      "an .srt with the caption phrases, timed in the cut")
_times = [l_.split(" --> ") for l_ in srt.splitlines() if "-->" in l_]
_secs = [[sum(float(x) * m for x, m in zip(t.replace(",", ".").split(":"), (3600, 60, 1)))
          for t in pr] for pr in _times]
check(all(a < b for a, b in _secs) and all(_secs[i][1] <= _secs[i + 1][0] + 1e-6
                                           for i in range(len(_secs) - 1)),
      "...in order, never overlapping")
check(res["cover"] and os.path.exists(res["cover"]) and res["cover_hook"] and
      os.path.exists(res["cover_hook"]) and
      open(res["cover"], "rb").read() != open(res["cover_hook"], "rb").read(),
      "a cover, and a cover with the hook written on it")
check(busy_band(res["cover"], 1000, 1500) < 20 and
      busy_band(pj["cut"]["cover"], 1000, 1500) > 100,
      "the cover is a CLEAN frame (no burned-in caption) to put the hook on")
check(sorted(f_ for f_ in os.listdir(res["dir"]) if not f_.endswith((".mp4", ".srt"))) ==
      ["caption.txt", "cover.png", "cover_hook.png", "licence.txt", "pack.json"] or
      sorted(f_ for f_ in os.listdir(res["dir"]) if not f_.endswith((".mp4", ".srt"))) ==
      ["cover.png", "cover_hook.png", "licence.txt", "pack.json"],
      "no working files left in the folder (%r)"
      % os.listdir(res["dir"]))
check(os.path.exists(os.path.join(res["dir"], "pack.json")) and
      os.path.basename(res["dir"]).startswith("Ears-in-the-soup-"),
      "all in one folder named after the video")
res2 = ex.export("exp-test", aspects=("9:16",), post_text="Title\n\ncap #aigenerated\n")
check(res2["dir"] != res["dir"] and list(res2["videos"]) == ["9:16"] and
      "#aigenerated" in open(res2["caption"]).read(),
      "a second export gets its own folder; the post caption is included")
check("Voices: Qwen3-TTS" in open(res["licence"]).read() and "commercial use" in
      open(res["licence"]).read(), "licence.txt says what the voices were made with")
res3 = ex.export("exp-test", aspects=("9:16",), voice_engine="fish")
check("NON-COMMERCIAL" in open(res3["licence"]).read(),
      "...and flags a non-commercial voice engine")
try:
    ex.export("nope-not-a-project")
    _err = ""
except ValueError as exc:
    _err = str(exc)
check("cut the video first" in _err, "no finished video -> a plain message")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
