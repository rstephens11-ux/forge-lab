#!/usr/bin/env python3
"""GPU-free checks for the GILDED SATIRE format (2026-10-20): many short lines, small gold
subtitles, a fade to black at the end.

    python3 tools/gilded_matrix.py        # exit 1 on any failure

The writer is FAKE; the fade is checked on a real (tiny) ffmpeg video. FORGE_LAB_DATA is a
throwaway dir, so nothing of Ryan's is touched.
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
_TMP = tempfile.mkdtemp(prefix="gilded-")
os.environ["FORGE_LAB_DATA"] = _TMP
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import formats  # noqa: E402
import quick  # noqa: E402
import studio_pipeline as sp  # noqa: E402
import writers_room as wr  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


print("§1 the format")
g = formats.get("gilded")
check(g["key"] == "gilded" and g["cast"] == 3 and g["aspect"] == "9:16",
      "Gilded satire: three roles, vertical")
check(formats.MIN_SECONDS <= g["seconds"] <= formats.MAX_SECONDS == 120,
      "it defaults to 2 minutes, inside Quick's limit")
check(g["pace"] in sp.PACES and g["captions"] in quick.CAPTION_CHOICES,
      "its pace and caption look are real choices")
sh = g["shape"]
check(all(w in sh for w in ("LORD", "PETITIONER", "ENABLER", "LOOP", "button")),
      "the shape names the three roles, the loop and the button")
check("THE DEMO" in sh and "INVENTOR" in sh and "too literally" in sh,
      "the shape also knows the demo game, when the idea asks for it")
_SRC = "".join(open(os.path.join(ROOT, f_)).read().lower() for f_ in (
    "formats.py", "char_styles.py", "characters_ui.py", "quick.py", "studio_pipeline.py"))
import codecs                                 # (rot13: the names stay out of this file too)
check(not any(codecs.decode(n_, "rot13") in _SRC for n_ in ("tbffvc", "tboyva", "pheena")),
      "no creator who inspired a style is named in the code (public repo)")
check(quick.guess_format("a gilded satire about reskilling") == "gilded",
      "an idea that says 'gilded satire' suggests the format")

print("§2 many short lines")
rg = formats.rhythm("gilded")
check(formats.rhythm("sketch") == formats.DEFAULT_RHYTHM == (3.8, 10),
      "every other format keeps the old rhythm")
check(wr.n_lines(120, 3) == 10, "default: 120 s is still 10 lines (nothing else changed)")
n = wr.n_lines(120, 3, rg)
check(35 <= n <= 45, "gilded: 120 s asks for %d lines (35-45)" % n)
check(wr.n_lines(30, 3, rg) == 10 and wr.n_lines(600, 3, rg) == 45,
      "gilded: one line per 3 s, never more than 45")
bw = wr.budget(120, 3, rg)
check(4 <= bw / float(n) <= 8, "gilded lines average %.1f words (short)" % (bw / float(n)))
check(wr.budget(120, 3) / 10.0 > 20, "default lines stay long (%.0f words)"
      % (wr.budget(120, 3) / 10.0))
rule = quick.length_rule(120, 3, rg)
check(("in %d lines" % n) in rule, "the writer's length rule asks for the %d lines" % n)


class FakeDirector:
    def __init__(self):
        self.sent = []

    def _chat(self, system, user, schema, temperature=0.7, model=None):
        self.sent.append(user)
        cast = ["Duke", "Ned", "Clem"]
        return {"title": "T", "hook": "h", "button": "Whose life, m'lord?",
                "lines": [{"speaker": cast[i % 3], "text": "Line %d here." % i}
                          for i in range(n - 1)] +
                         [{"speaker": "Ned", "text": "Whose life, m'lord?"}]}


fd = FakeDirector()
try:
    wr.draft(fd, "brief", {"Duke": "", "Ned": "", "Clem": ""}, 120, rhythm=rg)
except Exception:
    pass                                    # the fake's lines don't validate; only the ask matters
check(fd.sent and ("%d lines" % n) in fd.sent[0],
      "a gilded draft is asked for %d lines" % n)
fd2 = FakeDirector()
try:
    wr.draft(fd2, "brief", {"Duke": "", "Ned": "", "Clem": ""}, 120)
except Exception:
    pass
check(fd2.sent and "10 lines" in fd2.sent[0], "a default draft is still asked for 10")

print("§3 Quick")
j = quick.new_job("Reskilling Day", fmt="gilded")
check(j["captions"] == "subtitle" and j["seconds"] == 120 and j["pace"] == "natural",
      "a gilded job gets subtitles, 2 minutes and the natural pace")
check(quick.new_job("x", fmt="gilded", captions="pop")["captions"] == "pop",
      "captions you pick still win")
check(quick.new_job("x", fmt="sketch")["captions"] == "pop",
      "every other format still defaults to Pop")
est_g = quick.estimate(dict(j, want_cast=["a", "b", "c"]))
est_s = quick.estimate(dict(quick.new_job("x", fmt="sketch", seconds=120),
                            want_cast=["a", "b", "c"]))
check(est_g > est_s * 2.5, "the estimate counts the extra shots (%.0f min vs %.0f min)"
      % (est_g / 60, est_s / 60))
qs = open(os.path.join(ROOT, "quick.py")).read()
check('ep["end_fade"] = float(f["end_fade"])' in qs, "the episode gets the format's fade")
check(qs.count("rhythm=rhythm") >= 2 and "budget(job[\"seconds\"], len(cast), rhythm)" in qs,
      "both writer paths and the length check use the format's rhythm")
qu = open(os.path.join(ROOT, "quick_ui.py")).read()
check(qu.count('"subtitle"') >= 2, "Subtitle is on the CAPTIONS row and the re-cut chips")

print("§4 subtitle captions")
shots = [{"start": 0.0, "cut_in": 0.0, "cut_out": 3.0,
          "words": [{"word": "Then", "start": 0.2, "end": 0.4},
                    {"word": "eat", "start": 0.45, "end": 0.6},
                    {"word": "something", "start": 0.65, "end": 1.0},
                    {"word": "else.", "start": 1.05, "end": 1.4}]}]
fs, mv = sp.caption_style(1080, 1920)
a = sp.build_ass(shots, 1080, 1920, font_size=fs, margin_v=mv, look="subtitle")
st_ = [l for l in a.splitlines() if l.startswith("Style: Sub,")]
check(st_ and sp.SUB_COLOUR in st_[0], "the Sub style is warm gold")
size = int(st_[0].split(",")[2]) if st_ else 0
check(0 < size <= fs * 0.6, "it's about half the size (%d vs %d px)" % (size, fs))
sub_mv = int(st_[0].split(",")[-2]) if st_ else 0
check(0 < sub_mv < mv and sub_mv > 1920 * 0.2,
      "it sits lower than the other looks, still above the app's buttons (%d px up)" % sub_mv)
ev = [l for l in a.splitlines() if l.startswith("Dialogue:") and ",Sub," in l]
check(len(ev) == 1 and ev[0].endswith("Then eat something else."),
      "one event, sentence case, punctuation kept, no pop-in")
check(sp.POP_COLOUR not in "".join(ev), "no word-by-word highlight")
c = sp.build_ass(shots, 1080, 1920, font_size=fs, margin_v=mv, look="clean")
check("THEN EAT" in c and "SOMETHING ELSE." in c and ",Cap," in c and ",Sub," not in c,
      "Clean is unchanged (uppercase, its own style)")
check(dict(sp.CAPTION_LOOKS).get("subtitle"), "Subtitle is one of the caption looks")

print("§5 the fade to black")
src = os.path.join(_TMP, "src.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=c=white:s=160x284:r=24:d=4", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=4", "-shortest", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", src], check=True)
out = sp.end_fade(src, os.path.join(_TMP, "faded.mp4"), 1.5)
check(out.endswith("faded.mp4") and os.path.exists(out), "end_fade writes a new file")


def luma(path, t):
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.2f" % t, "-i", path, "-frames:v",
                        "1", "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG"
                        ":file=-", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"YAVG=([0-9.]+)", p.stdout)
    return float(m.group(1)) if m else -1


def vol(path, ss, t):
    p = subprocess.run(["ffmpeg", "-v", "info", "-ss", "%.2f" % ss, "-t", "%.2f" % t, "-i",
                        path, "-af", "volumedetect", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"max_volume: (-?[0-9.]+|-inf) dB", p.stderr)
    return float(m.group(1)) if m and m.group(1) != "-inf" else -99.0


check(abs(sp._duration(out) - sp._duration(src)) < 0.2, "the length is unchanged")
check(luma(out, 0.5) > 200, "the start is untouched (bright)")
check(0 <= luma(out, 3.9) < 30, "the last frame is black (%.0f)" % luma(out, 3.9))
check(vol(out, 3.85, 0.1) < vol(out, 0.5, 0.5) - 15, "the sound fades out too")
check(sp.end_fade(src, os.path.join(_TMP, "x.mp4"), 0) == src, "no fade asked: unchanged")
spsrc = open(os.path.join(ROOT, "studio_pipeline.py")).read()
check("if fade_s and i == len(order) - 1:" in spsrc and "b = min(clip_s, b + fade_s)" in spsrc,
      "the last shot runs on into the fade (it doesn't eat the button)")
check("mixed = end_fade(mixed" in spsrc, "the cut fades after the music, before captions")

print("\u00a76 the close-up lens")
check("85 mm" in sp._close_lens({}) and sp._close_lens({"lens": "28 mm wide-angle, close."})
      == "28 mm wide-angle, close.", "a project can choose its own close-up lens (85 mm "
      "stays the default)")
_sps = open(os.path.join(ROOT, "studio_pipeline.py")).read()
check(_sps.count("_close_lens(pj)") == 3 and "85 mm lens, shallow depth of field, the room "
      "soft behind. Photorealistic" not in _sps, "both close-up prompts use it")
check('ep["lens"] = job["authored"]["lens"]' in open(os.path.join(ROOT, "quick.py")).read(),
      "an authored job passes its lens to the episode")
print("\n%d checks, %d failed" % (N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
