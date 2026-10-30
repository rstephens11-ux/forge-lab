"""PLACES — a library of locations, made once and reused (Ryan, 2026-10-17: "generate
background images which can then be leveraged in the videos ... full cycle character and
background creation"). Tk-free.

A place is a photoreal, EMPTY location still (no people) at the set's own canvas, kept in
DATA/places/<slug>/ (place.json + image.png). The SET builds on it: the first person is put
INTO that exact picture (studio_pipeline.place_prompt with the place as <image2>), so every
shot starts from a background Ryan approved, the same in every video that uses it.

  options(text, aspect)        -> 3 candidate pictures (t2i, ~1-2 min)
  keep(name, text, img, aspect) -> slug
  variant(slug, change)        -> 3 pictures of the SAME place, changed (Qwen edit)
  from_photo(path, name)       -> slug (your own picture, fitted to the canvas)
"""
import json
import os
import re
import shutil
import subprocess
import time

import forge_core as c
import forge_lab as fl
import dispatch as _dsp          # the rig's queue (Tk-free): every render waits its turn

ASPECTS = ("9:16", "16:9")
# the set's plate canvas (studio_pipeline.set_size — the Final canvas for the shape)
CANVAS = {"9:16": (704, 1280), "16:9": (1280, 704)}
STEPS = 25
N_OPTIONS = 3
EST_IMAGE = 45                   # seconds — what the QUEUE row quotes for one still


def places_dir():
    d = os.path.join(fl.DATA, "places")
    os.makedirs(d, exist_ok=True)
    return d


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48] or "place"


def _path(slug):
    return os.path.join(places_dir(), slug)


def read(slug):
    try:
        meta = json.load(open(os.path.join(_path(slug), "place.json")))
    except Exception:
        return None
    img = os.path.join(_path(slug), "image.png")
    meta["image"] = img if os.path.exists(img) else None
    meta["slug"] = slug
    return meta


def image(slug):
    m = read(slug) if slug else None
    return (m or {}).get("image")


def library():
    """[(slug, name)] newest first."""
    out = []
    for slug in os.listdir(places_dir()):
        m = read(slug)
        if m and m.get("image"):
            out.append((m.get("saved", 0), slug, m.get("name") or slug))
    return [(s, n) for _t, s, n in sorted(out, reverse=True)]


def prompt_for(text, aspect="9:16"):
    """An EMPTY location, lit and framed like a film set — nobody in it, room to stand."""
    text = (text or "").strip().rstrip(".")
    if not text:
        raise ValueError("describe the place first")
    frame = ("a tall vertical frame" if aspect == "9:16" else "a wide frame")
    return ("A photograph of %s, completely empty, no people, no animals, no text. A real "
            "location dressed like a film set, %s at eye level, natural practical light, an "
            "open floor in the middle where two people could stand and talk. Photorealistic, "
            "28 mm lens, deep focus." % (text, frame))


def _image_of(pid=None, m=None):
    """The picture a run produced (a path). `m` may be passed in (the queue's meta) to skip
    the wait — that is how the queue path reaches the same place the direct one does."""
    m = m if m is not None else c.wait_and_collect(pid, timeout=1800)
    _pid = m.get("id") or pid
    for f in (m.get("images") or []):
        fn = f.get("file") or ""
        if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return os.path.join(c.RUNS, _pid, fn)
    raise RuntimeError("no picture came back (%s)" % _pid)


def options(text, aspect="9:16", n=N_OPTIONS, progress=None):
    """n candidate pictures of a described place -> [path]."""
    aspect = aspect if aspect in ASPECTS else "9:16"
    w, h = CANVAS[aspect]
    p = {"prompt": prompt_for(text, aspect), "width": w, "height": h,
         "resolution": max(w, h), "steps": STEPS, "seed": -1, "prefix": "forge_lab_place"}
    out = []
    for k in range(n):
        if progress:
            progress("place %d of %d" % (k + 1, n))
        # through the QUEUE (2026-10-22): a place waits for a card like everything else
        m = _dsp.render("Place — %s (%d of %d)" % ((text or "")[:24], k + 1, n),
                        "t2i", dict(p), None, need="image", est_s=EST_IMAGE)
        out.append(_image_of(m=m))
    return out


def variant(slug, change, n=N_OPTIONS, progress=None):
    """The SAME place, changed ("at night", "after a storm", "from the doorway") ->
    [path]: a Qwen edit of the kept picture."""
    m = read(slug)
    if not m or not m.get("image"):
        raise ValueError("that place has no picture")
    change = (change or "").strip().rstrip(".")
    if not change:
        raise ValueError("say what to change")
    w, h = CANVAS.get(m.get("aspect"), CANVAS["9:16"])
    p = {"prompt": ("<image1> is a location. Show exactly the same place - the same room, "
                    "objects and layout - but %s. Still completely empty: no people. "
                    "Photorealistic." % change),
         "steps": STEPS, "seed": -1, "custom_size": True, "width": w, "height": h,
         "prefix": "forge_lab_place"}
    out = []
    for k in range(n):
        if progress:
            progress("variant %d of %d" % (k + 1, n))
        r = _dsp.render("Place variant — %s (%d of %d)" % ((slug or "")[:20], k + 1, n),
                        "edit", dict(p), [m["image"]], need="image", est_s=EST_IMAGE)
        out.append(_image_of(m=r))
    return out


def keep(name, text, img, aspect="9:16", parent=None):
    """Save a picture as a place -> slug (a new slug if the name is taken)."""
    if not img or not os.path.exists(img):
        raise ValueError("pick a picture to keep")
    base = slugify(name or text)
    slug, i = base, 2
    while os.path.exists(_path(slug)):
        slug, i = "%s-%d" % (base, i), i + 1
    os.makedirs(_path(slug))
    try:
        _fit(img, os.path.join(_path(slug), "image.png"), aspect)
    except Exception:
        shutil.rmtree(_path(slug), ignore_errors=True)
        raise
    meta = {"name": (name or text or slug).strip()[:60], "text": (text or "").strip(),
            "aspect": aspect if aspect in ASPECTS else "9:16", "saved": time.time()}
    if parent:
        meta["parent"] = parent
    json.dump(meta, open(os.path.join(_path(slug), "place.json"), "w"), indent=1)
    return slug


def from_photo(path, name, aspect="9:16"):
    if not path or not os.path.exists(path):
        raise ValueError("that file is gone")
    name = name or os.path.splitext(os.path.basename(path))[0]
    # its description is fixed at the name it was kept under: renaming it later must not
    # change what the set prompts say (that silently rebuilt the set)
    return keep(name, name, path, aspect)


def rename(slug, name):
    m = read(slug)
    if not m:
        raise ValueError("no such place")
    m["name"] = (name or "").strip()[:60] or m["name"]
    m.pop("image", None)
    m.pop("slug", None)
    json.dump(m, open(os.path.join(_path(slug), "place.json"), "w"), indent=1)


def _fit(src, dst, aspect):
    """Fill the set canvas (crop, never letterbox), as an RGB png."""
    w, h = CANVAS.get(aspect, CANVAS["9:16"])
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf",
                        "scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,"
                        "crop=%d:%d,format=rgb24" % (w, h, w, h), "-frames:v", "1", dst],
                       check=True, capture_output=True, timeout=120)
    except Exception:
        # a HEIC or broken file: say so (copying it in as "image.png" broke the set later)
        raise ValueError("couldn't read that picture - try a JPG or PNG")


def describe(slug):
    """A few words for prompts: the place's description (or its name)."""
    m = read(slug) or {}
    return (m.get("text") or m.get("name") or "").strip()
