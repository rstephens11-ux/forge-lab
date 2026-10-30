"""MUSIC — a library of songs to put under videos (Ryan, 2026-10-18: "the music library
makes sense for songs i will generate going forward or songs I add ... should fill up as i
use it"). Tk-free.

It fills itself: every song made in the Song window (a "song" take in the index) is copied
in the next time the library is read, so a pruned take never takes a kept song with it.
Ryan can also ADD any audio file (mp3 / wav / m4a / flac / aiff). Kept in
DATA/music/<slug>/ as track.<ext> + track.json.

  library()             -> [(slug, name)] newest first (after syncing new song takes)
  add_file(path, name)  -> slug
  path(slug), read(slug), rename(slug, name)
"""
import json
import os
import re
import shutil
import time

import forge_core as c
import forge_lab as fl

AUDIO_EXT = (".mp3", ".wav", ".m4a", ".flac", ".aif", ".aiff", ".ogg", ".aac")


def music_dir():
    d = os.path.join(fl.DATA, "music")
    os.makedirs(d, exist_ok=True)
    return d


def _slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48] or "song"


def _free_slug(name):
    base = _slugify(name)
    slug, i = base, 2
    while os.path.exists(os.path.join(music_dir(), slug)):
        slug, i = "%s-%d" % (base, i), i + 1
    return slug


def read(slug):
    try:
        meta = json.load(open(os.path.join(music_dir(), slug, "track.json")))
    except Exception:
        return None
    f = os.path.join(music_dir(), slug, meta.get("file") or "")
    meta["path"] = f if meta.get("file") and os.path.exists(f) else None
    meta["slug"] = slug
    return meta


def path(slug):
    return (read(slug) or {}).get("path") if slug else None


def _keep(src, name, source, extra=None):
    ext = os.path.splitext(src)[1].lower() or ".flac"
    slug = _free_slug(name)
    d = os.path.join(music_dir(), slug)
    os.makedirs(d)
    try:
        shutil.copyfile(src, os.path.join(d, "track" + ext))
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    meta = {"name": (name or slug).strip()[:60], "file": "track" + ext, "source": source,
            "saved": time.time()}
    meta.update(extra or {})
    json.dump(meta, open(os.path.join(d, "track.json"), "w"), indent=1)
    return slug


def add_file(src, name=None):
    """Ryan's own song -> slug."""
    if not src or not os.path.exists(src):
        raise ValueError("that file is gone")
    if not src.lower().endswith(AUDIO_EXT):
        raise ValueError("pick an audio file (mp3, wav, m4a, flac, aiff)")
    return _keep(src, name or os.path.splitext(os.path.basename(src))[0], "added")


def song_name(it):
    """A song take's name: the first sung line, else the style, else the file name."""
    ly = [l.strip() for l in (it.get("lyrics") or "").splitlines()
          if l.strip() and not l.strip().startswith("[")]
    fn = ((it.get("images") or [{}])[0] or {}).get("file") or "song"
    return (ly[0] if ly else (it.get("style") or fn).split(",")[0]).strip()[:40] or "song"


def sync_from_takes():
    """Copy every song take not yet in the library. Returns the slugs added."""
    have = set()
    for slug in os.listdir(music_dir()):
        m = read(slug)
        if m and m.get("take"):
            have.add(m["take"])
    added = []
    try:
        items = [i for i in fl.read_index() if i.get("mode") == "song" and i.get("images")]
    except Exception:
        items = []
    for it in items:
        tid = it.get("id")
        if not tid or tid in have:
            continue
        fn = ((it.get("images") or [{}])[0] or {}).get("file") or ""
        src = os.path.join(c.RUNS, tid, fn)
        if not fn or not os.path.exists(src):
            continue
        try:
            added.append(_keep(src, song_name(it), "song",
                               {"take": tid, "style": it.get("style") or ""}))
        except Exception:
            continue
    return added


def library(sync=True):
    """[(slug, name)] newest first."""
    if sync:
        sync_from_takes()
    out = []
    for slug in os.listdir(music_dir()):
        m = read(slug)
        if m and m.get("path"):
            out.append((m.get("saved", 0), slug, m.get("name") or slug))
    return [(s, n) for _t, s, n in sorted(out, reverse=True)]


def rename(slug, name):
    m = read(slug)
    if not m:
        raise ValueError("no such song")
    m["name"] = (name or "").strip()[:60] or m["name"]
    for k in ("path", "slug"):
        m.pop(k, None)
    json.dump(m, open(os.path.join(music_dir(), slug, "track.json"), "w"), indent=1)


# what each format's bed should feel like, in words a song's style or name might use
MOOD = {
    "sketch": "comedy quirky playful light upbeat lofi jazz funk bouncy",
    "interview": "upbeat light quirky lofi jazz",
    "podcast": "lofi chill jazz hip hop mellow",
    "weather": "news upbeat synth corporate bright retro",
    "ad": "upbeat corporate bright pop happy jingle",
    "vlog": "upbeat pop lofi chill happy indie",
    "ifvlog": "upbeat pop lofi quirky indie",
    "confessional": "dramatic strings tense emotional piano cinematic",
    "nature": "orchestral ambient calm strings cinematic gentle",
    "doorbell": "suspense tense ambient dark minimal",
}


def pick_for(idea="", fmt="sketch"):
    """MUSIC -> "Pick one for me": the library song whose name/style best matches the
    format's mood (and the idea's words); the newest song when nothing matches; None when
    the library is empty."""
    lib = library()
    if not lib:
        return None
    want = set((MOOD.get(fmt, MOOD["sketch"]) + " " + (idea or "")).lower().split())
    best, best_s = lib[0][0], 0
    for slug, name in lib:
        m = read(slug) or {}
        words = set(re.findall(r"[a-z]+", ("%s %s" % (name, m.get("style") or "")).lower()))
        sc = len(words & want)
        if sc > best_s:
            best, best_s = slug, sc
    return best
