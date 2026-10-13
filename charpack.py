#!/usr/bin/env python3
"""charpack.py — a CHARACTER PACK for Forge Lab.

A saved character (``characters/<slug>/character.json`` + ``still.png``) is one look.
This module turns it into a *pack* — everything a shot needs to keep the character
consistent, generated headlessly (no Tk, so it runs on the Mac or on the rig):

  * **reference sheet**  ``sheet/<NN>_<key>.png`` + ``sheet.json``
        Six/seven consistent views built with Qwen-Image *edit* (the hero still is
        ``<image1>``), each one re-stating the character's ONE absurd feature so the
        identity — and the exaggeration — survives every angle.
  * **character bible**  ``bible.json``   (Director LLM)
  * **character voice**  ``voice.wav`` + ``voice.json``   (Qwen3 TTS on GPU1)

Design notes
------------
* Tk-FREE. Imports ``forge_core`` (the Tk-free facade) and ``forge_lab`` for the
  character-store helpers; nothing here imports tkinter.
* **New files only.** It never calls ``write_character`` (so it cannot trip the
  "saving a character dropped its still" bug) and never edits ``character.json`` —
  the pack lives in its own ``sheet/``, ``sheet.json``, ``bible.json``, ``voice.json``.
* ⚠️ The sheet PNGs are FLATTENED to RGB. A Qwen edit can return an RGBA PNG, and a
  transparent PNG bricks Tk on load (``thumb_photo`` exists for exactly that reason).
  The stored sheet is always colour-type 2 so it is safe to show in the app.
* Every view PINS and RECORDS its seed, so a sheet is reproducible and resumable.

CLI::

    python3 charpack.py sheet  bighead --scale Fast
    python3 charpack.py bible  bighead
    python3 charpack.py voice  bighead
    python3 charpack.py status bighead
    python3 charpack.py refs   bighead
"""
import argparse
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import time

import forge_core as core
import forge_lab as fl
from director import Director
from audio import client as audio

# H3's reference cap (forge_lab.H3_MAX_REFS) — the sheet is built to fit inside it.
H3_MAX_REFS = 9

# A short line the designed voice actually speaks, so the wav is usable as a clone
# reference and as a listening check.
DEFAULT_SAMPLE_LINE = "Right. So that is the situation, and here we are."

# ---------------------------------------------------------------------------
# The reference sheet. Ordered, and the LAST one is optional (a back view is
# nice-to-have, not part of the core identity set).
#
# ⚠️ Every prompt re-states the character's ONE absurd feature. The house style is
# "ordinary in every way except ONE absurd volume" — if the sheet drops that volume
# the extra angles are worse than useless: they train the pack on a normal person.
# `framing` picks the canvas via forge_lab.char_still_size(); `view` is the pose line.
# ---------------------------------------------------------------------------
SHEET_VIEWS = [
    {"key": "front",
     "framing": "full body shot, the whole figure head to feet in frame",
     "view": "full body shot, standing straight and facing the camera, "
             "the whole figure head to feet in frame"},
    {"key": "three-quarter-left",
     "framing": "three-quarter shot, head to mid-thigh",
     "view": "three-quarter view, body turned to the left, head to mid-thigh in frame"},
    {"key": "profile-right",
     "framing": "full body shot, the whole figure head to feet in frame",
     "view": "full side profile view facing right, the whole figure head to feet in frame"},
    {"key": "closeup-neutral",
     "framing": "bust portrait, head and shoulders only",
     "view": "head-and-shoulders close-up, neutral deadpan expression"},
    {"key": "expression-surprised",
     "framing": "bust portrait, head and shoulders only",
     "view": "head-and-shoulders close-up, mildly surprised expression, "
             "eyebrows slightly raised, mouth slightly open"},
    {"key": "expression-deadpan",
     "framing": "bust portrait, head and shoulders only",
     "view": "head-and-shoulders close-up, deadpan unimpressed expression, "
             "flat mouth, unblinking stare"},
    {"key": "back-three-quarter",
     "framing": "three-quarter shot, head to mid-thigh",
     "view": "three-quarter view from behind, head to mid-thigh in frame",
     "optional": True},
]

SHEET_MAIN_KEYS = [v["key"] for v in SHEET_VIEWS if not v.get("optional")]


# ---------------------------------------------------------------------------
# paths / store
# ---------------------------------------------------------------------------
def characters_dir():
    """The character store root, honouring FORGE_LAB_DATA / configure()."""
    return os.path.join(core.DATA, "characters")


def _char_dir(slug):
    return os.path.join(characters_dir(), slug)


def _resolve_slug(name):
    """Accept a slug, or a character NAME that slugifies to an existing folder."""
    if os.path.isdir(_char_dir(name)):
        return name
    try:
        slug = fl.char_slug(name)
    except Exception:
        return name
    if os.path.isdir(_char_dir(slug)):
        return slug
    # last resort: match against the store's own (slug, name) list
    for s, n in core.list_characters():
        if str(n).lower() == str(name).lower():
            return s
    return slug


# ---------------------------------------------------------------------------
# the absurd feature — the ONE volume dial
# ---------------------------------------------------------------------------
def absurd_feature(meta):
    """The character's single exaggeration, as prompt-ready words.

    Priority: a SLIDER driven to an extreme (the strongest statement — the skill
    measured that the slider inks harder than a Feature phrase), then a Feature
    pick, then the author's note. Falls back to a neutral phrase so a prompt is
    never left with an empty slot.
    """
    picks = meta.get("picks") or {}
    for axis, _phrases in fl.CHAR_SLIDERS:
        try:
            if abs(fl.char_slider_level(picks, axis)) == 3:
                ph = fl.char_slider_phrase(picks, axis)
                if ph:
                    return ph
        except Exception:
            continue
    feat = (picks.get("Feature") or "").strip()
    if feat:
        return feat
    note = (meta.get("note") or "").strip()
    if note:
        return note
    return "the character's signature feature"


def view_prompt(view, feature):
    """The edit prompt for one sheet view. ``<image1>`` is the hero still."""
    return ("Same person as <image1>, identical face, hair, skin, build and clothing, "
            "keep the %s exactly the same; %s; same plain light-grey studio backdrop, "
            "soft even light, photorealistic." % (feature, view["view"]))


def _select_views(views):
    """Resolve a views argument to the ordered list of view dicts to build."""
    if not views:
        return [v for v in SHEET_VIEWS if not v.get("optional")]
    out = []
    by_key = {v["key"]: v for v in SHEET_VIEWS}
    for item in views:
        if isinstance(item, dict):
            out.append(item)
        elif item in by_key:
            out.append(by_key[item])
        else:
            raise ValueError("unknown sheet view %r (known: %s)"
                             % (item, ", ".join(by_key)))
    return out


# ---------------------------------------------------------------------------
# PNG handling — flatten alpha so Tk can load the stored sheet
# ---------------------------------------------------------------------------
def png_color_type(path):
    """IHDR colour type (2=RGB, 6=RGBA, …) or None if not a readable PNG."""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return head[25]
    except Exception:
        return None


def _flatten_png(src, dst):
    """Write an RGB PNG at `dst`. ffmpeg first, sips as a fallback."""
    for cmd in (["ffmpeg", "-v", "error", "-y", "-i", src, "-pix_fmt", "rgb24", dst],
                ["sips", "-s", "format", "png", "--setProperty", "hasAlpha", "no",
                 src, "--out", dst]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and os.path.exists(dst) and png_color_type(dst) not in (4, 6):
                return dst
        except Exception:
            pass
    # last resort: copy as-is (⚠️ an RGBA copy can hang Tk — say so, do not hide it)
    shutil.copyfile(src, dst)
    return dst


def store_png(src, dst):
    """Copy a render into the sheet, FLATTENING a transparent PNG to RGB."""
    ct = png_color_type(src)
    if ct in (4, 6):
        return _flatten_png(src, dst)
    shutil.copyfile(src, dst)
    return dst


# ---------------------------------------------------------------------------
# sheet
# ---------------------------------------------------------------------------
def _sheet_doc_path(slug):
    return os.path.join(_char_dir(slug), "sheet.json")


def load_sheet(slug):
    """The recorded sheet.json, or None."""
    try:
        with open(_sheet_doc_path(slug)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def sheet_estimate(slug, views=None, scale="Fast"):
    """Price the sheet with the app's own estimate_seconds().

    ⚠️ The sheet submits with ``custom_size=True`` (so the framing picks the canvas),
    so the estimate is passed the REAL width/height rather than the edit mode's
    default ``resolution`` — otherwise a tall portrait is quoted as a 1 MP square.
    """
    meta = core.read_character(slug)
    if not meta:
        raise ValueError("no such character: %s" % slug)
    hero = core.character_still(slug)
    feature = absurd_feature(meta)
    rows = []
    total = 0.0
    for view in _select_views(views):
        w, h = fl.char_still_size(view["framing"], scale)
        p = {"prompt": view_prompt(view, feature), "steps": 25, "seed": 0,
             "custom_size": True, "width": w, "height": h}
        secs = core.estimate_seconds("edit", p, [hero] if hero else None)
        rows.append({"key": view["key"], "width": w, "height": h, "seconds": round(secs, 1)})
        total += secs
    return {"views": rows, "count": len(rows), "total_seconds": round(total, 1)}


def build_sheet(slug, views=None, scale="Fast", seed=None, on_progress=None, force=False):
    """Build the multi-angle reference sheet for a character.

    Resumable: a view whose PNG already exists is SKIPPED (and its recorded seed
    reused) unless ``force=True``. Every built view PINS and RECORDS its seed.

    Returns the sheet.json document.
    """
    slug = _resolve_slug(slug)
    meta = core.read_character(slug)
    if not meta:
        raise ValueError("no such character: %s" % slug)
    hero = core.character_still(slug)
    if not hero:
        raise ValueError("character %r has no still.png to build a sheet from" % slug)

    feature = absurd_feature(meta)
    cdir = _char_dir(slug)
    sheet_dir = os.path.join(cdir, "sheet")
    os.makedirs(sheet_dir, exist_ok=True)

    prev = load_sheet(slug) or {}
    prev_views = {v.get("key"): v for v in (prev.get("views") or [])}

    selected = _select_views(views)
    selected_keys = {v["key"] for v in selected}
    order = {v["key"]: i for i, v in enumerate(SHEET_VIEWS, start=1)}
    out_views = []
    built = 0
    # Walk the FULL ordered list so a partial build (e.g. just the back view) CARRIES
    # the views already on disk instead of clobbering the recorded sheet.
    for view in SHEET_VIEWS:
        key = view["key"]
        i = order[key]
        fname = "%02d_%s.png" % (i, key)
        fpath = os.path.join(sheet_dir, fname)
        prompt = view_prompt(view, feature)
        old = prev_views.get(key) or {}

        if key not in selected_keys:
            if os.path.exists(fpath) and old:
                out_views.append(old)          # carry a previously built view
            continue

        if os.path.exists(fpath) and not force:
            entry = {"key": key, "file": fname, "prompt": prompt}
            if old.get("pid"):
                entry["pid"] = old["pid"]
            if old.get("seed") is not None:
                entry["seed"] = old["seed"]
            out_views.append(entry)
            if on_progress:
                on_progress(i, key, "skipped (exists)")
            continue

        # pin a seed per view: explicit base+offset, or a recorded one, else random
        vs = old.get("seed")
        if vs is None:
            vs = (int(seed) + i - 1) if seed is not None else random.randint(0, 2 ** 31 - 1)
        w, h = fl.char_still_size(view["framing"], scale)
        p = {"prompt": prompt, "steps": 25, "seed": vs, "prefix": "charpack",
             "custom_size": True, "width": w, "height": h}
        pid, used_seed = core.submit("edit", p, [hero])
        m = core.wait_and_collect(pid, timeout=1800)
        files = [f for f in (m.get("images") or [])
                 if (f.get("file") or "").lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        if not files:
            raise RuntimeError("view %r produced no image (pid %s, status %s)"
                               % (key, pid, m.get("status")))
        src = os.path.join(core.RUNS, pid, files[0]["file"])
        store_png(src, fpath)
        out_views.append({"key": key, "file": fname, "pid": pid,
                          "seed": int(used_seed), "prompt": prompt})
        built += 1
        if on_progress:
            on_progress(i, key, "built")

    doc = {"slug": slug, "feature": feature, "scale": scale, "views": out_views,
           "built": built, "created": prev.get("created") or time.time(),
           "updated": time.time()}
    with open(_sheet_doc_path(slug), "w") as f:
        json.dump(doc, f, indent=1)
    return doc


# ---------------------------------------------------------------------------
# H3 reference packing — order + the tag sentence
# ---------------------------------------------------------------------------
def pack_refs(slug, max_refs=H3_MAX_REFS):
    """Ordered H3 r2v reference list that exists on disk.

    [hero still, front, 3/4, profile, close-up, expressions…] — the hero first so
    ``<Picture 1>`` is always the canonical face. Capped at ``max_refs`` (≤ 9).
    """
    slug = _resolve_slug(slug)
    refs = []
    hero = core.character_still(slug)
    if hero:
        refs.append(hero)
    cdir = _char_dir(slug)
    by_key = {}
    doc = load_sheet(slug) or {}
    for v in (doc.get("views") or []):
        fn = v.get("file")
        if fn:
            p = os.path.join(cdir, "sheet", fn)
            if os.path.exists(p):
                by_key[v.get("key")] = p
    if not by_key:                       # no sheet.json — fall back to the filenames
        sd = os.path.join(cdir, "sheet")
        if os.path.isdir(sd):
            for fn in sorted(os.listdir(sd)):
                for view in SHEET_VIEWS:
                    if fn.endswith("_%s.png" % view["key"]):
                        by_key[view["key"]] = os.path.join(sd, fn)
    for view in SHEET_VIEWS:
        p = by_key.get(view["key"])
        if p and p not in refs:
            refs.append(p)
    return refs[:max(1, int(max_refs))]


def ref_tag_sentence(n):
    """The r2v tag text for `n` references, phrased as the app's own help says.

    forge_lab's r2v help: *"Tag each one in the prompt (<Picture 1>, <Picture 2>… in
    the order added)"* / _ref_help_text: *"Address them in the prompt BY TAG, in the
    order added: ref 1 = <Picture 1> … Say what each one IS and which part of the
    shot it drives."*
    """
    n = max(1, min(int(n), H3_MAX_REFS))
    refs = ", ".join("ref %d = <Picture %d>" % (i, i) for i in range(1, n + 1))
    return ("Address them in the prompt BY TAG, in the order added: %s. "
            "Say what each one IS and which part of the shot it drives." % refs)


# ---------------------------------------------------------------------------
# bible
# ---------------------------------------------------------------------------
def bible(slug, director=None):
    """Write the character bible with the Director LLM -> ``bible.json``."""
    slug = _resolve_slug(slug)
    meta = core.read_character(slug)
    if not meta:
        raise ValueError("no such character: %s" % slug)
    own = director is None
    d = director or Director()
    if own:
        d.load()
    try:
        result = d.bible(meta)
    finally:
        if own:
            try:
                d.unload()
            except Exception:
                pass
    doc = dict(result)
    doc["slug"] = slug
    doc["created"] = time.time()
    with open(os.path.join(_char_dir(slug), "bible.json"), "w") as f:
        json.dump(doc, f, indent=1)
    return doc


def load_bible(slug):
    try:
        with open(os.path.join(_char_dir(_resolve_slug(slug)), "bible.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# voice
# ---------------------------------------------------------------------------
def make_voice(slug, description=None, sample_line=None):
    """Design the character's voice -> ``voice.wav`` + ``voice.json``.

    ⚠️ The Director LLM (GPU1, ~19 GB) is UNLOADED before TTS runs (GPU1, ~5 GB) —
    the two cannot be resident together.
    """
    slug = _resolve_slug(slug)
    meta = core.read_character(slug)
    if not meta:
        raise ValueError("no such character: %s" % slug)
    line = (sample_line or DEFAULT_SAMPLE_LINE).strip()
    if not description:
        d = Director()
        d.load()
        try:
            description = d.voice_description(meta)
        finally:
            try:
                d.unload()
            except Exception:
                pass
    cdir = _char_dir(slug)
    wav = os.path.join(cdir, "voice.wav")
    info = audio.tts_design(description, line, wav)
    doc = {"description": description, "ref_text": line, "created": time.time(),
           "file": "voice.wav"}
    with open(os.path.join(cdir, "voice.json"), "w") as f:
        json.dump(doc, f, indent=1)
    doc["info"] = info
    return doc


def load_voice(slug):
    try:
        with open(os.path.join(_char_dir(_resolve_slug(slug)), "voice.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def pack_status(slug):
    """What this character pack currently has."""
    slug = _resolve_slug(slug)
    cdir = _char_dir(slug)
    sd = os.path.join(cdir, "sheet")
    n = 0
    if os.path.isdir(sd):
        n = len([f for f in os.listdir(sd) if f.lower().endswith(".png")])
    return {
        "slug": slug,
        "has_still": core.character_still(slug) is not None,
        "sheet_views": n,
        "has_bible": os.path.exists(os.path.join(cdir, "bible.json")),
        "has_voice": (os.path.exists(os.path.join(cdir, "voice.json"))
                      and os.path.exists(os.path.join(cdir, "voice.wav"))),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _say(i, key, status):
    print("  [%d] %-22s %s" % (i, key, status), flush=True)


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="charpack", description="Forge Lab character pack")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("sheet", help="build the multi-angle reference sheet")
    ps.add_argument("slug")
    ps.add_argument("--scale", default="Fast", choices=("Full", "Fast", "Draft"))
    ps.add_argument("--seed", type=int, default=None)
    ps.add_argument("--force", action="store_true")
    ps.add_argument("--estimate", action="store_true", help="price only, build nothing")

    pb = sub.add_parser("bible", help="write the character bible")
    pb.add_argument("slug")

    pv = sub.add_parser("voice", help="design and save the character voice")
    pv.add_argument("slug")
    pv.add_argument("--description", default=None)
    pv.add_argument("--line", default=None)

    pt = sub.add_parser("status", help="show what the pack has")
    pt.add_argument("slug")

    pr = sub.add_parser("refs", help="print the ordered H3 r2v reference list + tag text")
    pr.add_argument("slug")

    args = ap.parse_args(argv)

    if args.cmd == "sheet":
        est = sheet_estimate(args.slug, scale=args.scale)
        print("estimate (%s): %d views, ~%.0f s total" %
              (args.scale, est["count"], est["total_seconds"]))
        for r in est["views"]:
            print("   %-22s %dx%d  ~%.0f s" % (r["key"], r["width"], r["height"], r["seconds"]))
        if args.estimate:
            return 0
        doc = build_sheet(args.slug, scale=args.scale, seed=args.seed,
                          on_progress=_say, force=args.force)
        print("sheet: %d built, %d recorded -> %s" %
              (doc["built"], len(doc["views"]), _sheet_doc_path(args.slug)))
        return 0

    if args.cmd == "bible":
        doc = bible(args.slug)
        print(json.dumps(doc, indent=1))
        return 0

    if args.cmd == "voice":
        doc = make_voice(args.slug, description=args.description, sample_line=args.line)
        print("voice: %s  (%s)" % (doc["file"], doc["description"]))
        return 0

    if args.cmd == "status":
        print(json.dumps(pack_status(args.slug), indent=1))
        return 0

    if args.cmd == "refs":
        refs = pack_refs(args.slug)
        for i, r in enumerate(refs, start=1):
            print("  %d  %s" % (i, r))
        print()
        print(ref_tag_sentence(len(refs)))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_main())