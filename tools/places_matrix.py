#!/usr/bin/env python3
"""GPU-free checks for places.py — the PLACES library — and the set built IN a place.
The rig is faked (real tiny pictures via ffmpeg); what's checked is what is SENT and what
is kept.
    python3 tools/places_matrix.py"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
_TMP = tempfile.mkdtemp(prefix="places-")
os.environ["FORGE_LAB_DATA"] = _TMP
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import places as pl  # noqa: E402
import studio_pipeline as sp  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def ff(args, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + args + [out], check=True)
    return out


SUB = []


def fake_submit(kind, p, refs=None):
    SUB.append((kind, dict(p), list(refs or [])))
    pid = "pl%03d" % len(SUB)
    ff(["-f", "lavfi", "-i", "color=c=0x%02x6080:s=%dx%d" % (
        (40 + 20 * len(SUB)) % 256, p.get("width", 704), p.get("height", 1280)),
        "-frames:v", "1"], os.path.join(fc.RUNS, pid, "o.png"))
    return pid, 7


fc.submit = fake_submit
fc.wait_and_collect = lambda pid, timeout=1800, **k: {"id": pid, "images": [{"file": "o.png"}]}


def dims(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    return tuple(int(x) for x in out.strip().split(",")[:2])


print("§1 make a place")
bad = ""
try:
    pl.options("", "9:16")
except ValueError as exc:
    bad = str(exc)
check("describe" in bad, "no description -> a clear message")
opts = pl.options("a lighthouse lamp room at dusk", "9:16")
check(len(opts) == 3 and all(os.path.exists(o) for o in opts) and
      all(k == "t2i" for k, _p, _r in SUB[-3:]),
      "Make 3 -> three pictures (three stills)")
p0 = SUB[-1][1]
check("a lighthouse lamp room at dusk" in p0["prompt"] and "no people" in p0["prompt"] and
      (p0["width"], p0["height"]) == sp.CANVAS["Final"],
      "an EMPTY place (no people) at the tall set canvas %r" % ((p0["width"], p0["height"]),))
pl.options("a TV studio", "16:9")
check((SUB[-1][1]["width"], SUB[-1][1]["height"]) == sp.CANVAS_WIDE["Final"],
      "wide places at the wide set canvas")

print("§2 keep, list, variants, photos")
slug = pl.keep("Lamp room", "a lighthouse lamp room at dusk", opts[1], "9:16")
m = pl.read(slug)
check(slug == "lamp-room" and m["name"] == "Lamp room" and os.path.exists(m["image"]) and
      dims(m["image"]) == sp.CANVAS["Final"], "Keep -> a named place with its picture")
slug2 = pl.keep("Lamp room", "x", opts[0], "9:16")
check(slug2 == "lamp-room-2" and [s for s, _n in pl.library()][:2] == [slug2, slug],
      "a taken name gets its own slug; the library lists newest first")
n0 = len(SUB)
vs = pl.variant(slug, "at night")
check(len(vs) == 3 and all(k == "edit" and r == [m["image"]] for k, _p, r in SUB[n0:]) and
      "at night" in SUB[-1][1]["prompt"] and "same place" in SUB[-1][1]["prompt"],
      "a variant is an edit OF the kept picture: the same place, changed")
v_slug = pl.keep("Lamp room (night)", "a lighthouse lamp room at dusk, at night", vs[0],
                 "9:16", parent=slug)
check(pl.read(v_slug).get("parent") == slug, "...and remembers which place it came from")
photo = ff(["-f", "lavfi", "-i", "color=c=0x445566:s=1600x900", "-frames:v", "1"],
           os.path.join(_TMP, "my.jpg"))
p_slug = pl.from_photo(photo, "My kitchen", "9:16")
check(dims(pl.image(p_slug)) == sp.CANVAS["Final"],
      "your own photo is fitted to the canvas (filled, never letterboxed)")

bad_ = os.path.join(_TMP, "photo.heic")
open(bad_, "wb").write(b"not a picture")
msg_ = ""
try:
    pl.from_photo(bad_, "Broken", "9:16")
except ValueError as exc:
    msg_ = str(exc)
check("JPG or PNG" in msg_ and not os.path.exists(pl._path("broken")),
      "a photo ffmpeg can't read -> a clear message, no half-made place")
pl.rename(p_slug, "Our kitchen")
check(pl.describe(p_slug) == "My kitchen",
      "renaming a photo place keeps its description (the set isn't silently rebuilt)")

print("§3 the set is built IN the place")
fc.character_still = lambda s: {"gus": photo, "clara": photo}.get(s)
sp.fl.write_character("gus", {"name": "Gus", "prompt": "He wears a black tailcoat.",
                              "feature": "wild hair", "picks": {}})
sp.fl.write_character("clara", {"name": "Clara", "prompt": "x", "feature": "ears to her chin",
                                "picks": {}})
pj = sp.new_project(["gus", "clara"], "x", aspect="9:16")
pj["shots"] = [sp._shot_migrate({"n": i, "speaker": s, "video_mode": "set"})
               for i, s in ((1, "gus"), (2, "clara"))]
pj["place"] = slug
n0 = len(SUB)
built = sp.make_set(pj)
e1 = SUB[n0]
check(e1[2] == [photo, pl.image(slug)] and "<image2> is the place" in e1[1]["prompt"] and
      "keep it EXACTLY" in e1[1]["prompt"],
      "pass 1 puts the first person INTO the kept picture (<image2>)")
check(built.get("place") == slug and sp.set_ready(pj) and
      sp.set_where(pj) == "a lighthouse lamp room at dusk",
      "the set remembers its place; prompts describe it in its own words")
twin = pl.keep("Twin", "a lighthouse lamp room at dusk", opts[2], "9:16")
pj["place"] = twin
check(sp.set_where(pj) == "a lighthouse lamp room at dusk" and not sp.set_ready(pj),
      "another place -> the set must be rebuilt (even one described the same)")
pj["place"] = None
n0 = len(SUB)
sp.make_set(pj)
check(SUB[n0][2] == [photo] and "<image2> is the place" not in SUB[n0][1]["prompt"],
      "no place -> the described set, as before")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
