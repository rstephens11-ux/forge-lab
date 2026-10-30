#!/usr/bin/env python3
"""GPU-free checks for music.py — the MUSIC library (songs Ryan makes or adds).
    python3 tools/music_matrix.py"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
_TMP = tempfile.mkdtemp(prefix="music-")
os.environ["FORGE_LAB_DATA"] = _TMP
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import music as mu  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def tone(path, secs=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=330:d=%d" % secs, path], check=True)
    return path


INDEX = []
mu.fl.read_index = lambda: list(INDEX)

print("§1 it fills itself from the songs you make")
check(mu.library() == [], "empty to start")
tone(os.path.join(fc.RUNS, "songA", "yue2_00001.flac"))
tone(os.path.join(fc.RUNS, "songB", "yue2_00001.flac"))
INDEX[:] = [{"id": "songA", "mode": "song", "lyrics": "[Verse]\nears in the wind\n",
             "style": "ballad", "images": [{"file": "yue2_00001.flac"}]},
            {"id": "songB", "mode": "song", "lyrics": "", "style": "lofi beat, chill",
             "images": [{"file": "yue2_00001.flac"}]},
            {"id": "img1", "mode": "t2i", "images": [{"file": "o.png"}]}]
lib = mu.library()
names = sorted(n for _s, n in lib)
check(names == ["ears in the wind", "lofi beat"] and
      all(os.path.exists(mu.path(s)) for s, _n in lib),
      "each song take is copied in, named by its first sung line or its style (%r)" % names)
check(mu.library() == lib, "...once (reading it again adds nothing)")
shutil.rmtree(os.path.join(fc.RUNS, "songA"))
check(len(mu.library()) == 2 and all(os.path.exists(mu.path(s)) for s, _n in mu.library()),
      "deleting the take doesn't take the kept song with it")

print("§2 songs you add")
mine = tone(os.path.join(_TMP, "My Theme.mp3"))
slug = mu.add_file(mine)
check(mu.read(slug)["name"] == "My Theme" and mu.read(slug)["source"] == "added" and
      mu.library()[0][0] == slug, "an added file is kept under its name, newest first")
slug2 = mu.add_file(mine)
check(slug2 != slug, "the same name twice gets its own entry")
bad = ""
try:
    mu.add_file(os.path.join(_TMP, "notes.txt") if open(os.path.join(_TMP, "notes.txt"), "w")
                .write("x") else None)
except ValueError as exc:
    bad = str(exc)
check("audio file" in bad, "a non-audio file -> a clear message")
mu.rename(slug, "Opening theme")
check(mu.read(slug)["name"] == "Opening theme" and os.path.exists(mu.path(slug)),
      "rename keeps the song")

print("§3 pick one for me")
mu.rename(slug2, "Dark suspense drone")
check(mu.pick_for("someone at the door", "doorbell") == slug2 and
      mu.pick_for("x", "ad") in [s_ for s_, _n in mu.library()],
      "a doorbell video picks the suspense track; nothing matching -> still a song")
_e = tempfile.mkdtemp()
_old = mu.music_dir
mu.music_dir = lambda: _e
INDEX[:] = []
check(mu.pick_for("x", "sketch") is None, "an empty library picks nothing")
mu.music_dir = _old

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
