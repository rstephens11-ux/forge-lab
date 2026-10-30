#!/usr/bin/env python3
"""studio_pipeline.py — the Tk-FREE engine behind Forge Lab Studio.

A PROJECT carries one short from idea to postable 9:16 file:

    character(s) + idea -> script -> shots -> voice lines -> renders -> cut -> captions

⚠️ NO tkinter anywhere. This module must import and RUN on `forge` as well as on the
Mac. It goes through forge_core (the Tk-free facade), so a Studio run lands in Forge
Lab's Takes index exactly like a UI run — a project's shots are ordinary Takes with
lineage written back onto them.

⚠️ Two hosts, one code path. On the Mac, audio (TTS/align) and the caption burn run on
`forge` over ssh/scp. On `forge` itself (overnight runs) both run LOCALLY — see
audio/client.py's FORGE_AUDIO_LOCAL and finish()'s ffmpeg-capability probe.

⚠️ GPU discipline: GPU1 is shared by the Director LLM and the TTS/aligner jobs. They do
NOT fit together (~19 GB + ~5 GB > 24 GB), so voice_lines() UNLOADs the Director before
touching audio.

The project lives at DATA/studio/<slug>/project.json and is saved ATOMICALLY (write a
temp file in the same directory, then os.replace). Every stage is idempotent and
resumable: a crash part-way through surfaces back in project.json and re-running run()
picks up where it stopped.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "audio")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import forge_core as c          # noqa: E402  (Tk-free facade over forge_lab)
import forge_lab as fl          # noqa: E402  (same module object; for CHARACTERS/_index_update)
import dispatch as _dsp         # noqa: E402  (the rig's queue: every render waits its turn)
EST_IMAGE = 45                  # seconds — what the QUEUE row quotes for one still

try:                            # the Mac side of the audio service
    import client as audio_client
except Exception:               # pragma: no cover - only if the file moved
    audio_client = None


# ---------------------------------------------------------------------------
# Constants — the studio's own rules, stated once
# ---------------------------------------------------------------------------
VALID_SHOT_SECONDS = (3, 5, 8)
_SECONDS_LADDER = {3: 0, 5: 1, 8: 2}

# ⚠️ The H3 node accepts ANY multiple of 32, and 720 is NOT one (the file's own comment:
# "720 and 1080 are not multiples of 32, so the honest numbers are 704 and 1088"). So
# 'Final' is 704x1280, the nearest legal ~9:16 pair to the intended 720x1280.
# Draft 544x960 and Final 704x1280 — both /32 — and 12 steps is the floor for anything
# kept: 4 steps is H3's Draft and the app's own note says the FACE GOES SOFT.
CANVAS = {"Draft": (544, 960), "Final": (704, 1280)}
# 16:9 EPISODES (Ryan, 2026-10-17: the merged Studio + Long video window makes cartoon
# episodes). The same two sizes turned on their side — both still multiples of 32.
CANVAS_WIDE = {"Draft": (960, 544), "Final": (1280, 704)}
ASPECTS = ("9:16", "16:9")
OUT_SIZE = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
EPISODE_LENGTHS = (30, 60, 90, 120, 180)
MUSIC_VOLUME = 0.22             # the bed sits well under the voices and the clips' own sound
STEPS = 12
QUALITIES = tuple(CANVAS)


def canvas(pj):
    """(w, h) a shot renders at — by the project's ASPECT and QUALITY."""
    table = CANVAS_WIDE if (pj or {}).get("aspect") == "16:9" else CANVAS
    return table.get((pj or {}).get("quality", "Draft"), table["Draft"])


def wants_captions(pj):
    """Burned-in captions? An explicit True/False wins; None means the aspect's default —
    on for 9:16 shorts (made for silent scrolling), off for 16:9 episodes."""
    v = (pj or {}).get("captions")
    if v is None:
        return (pj or {}).get("aspect", "9:16") == "9:16"
    return bool(v)

STAGES = ("pitch", "choose", "script", "shots", "voice", "estimate", "render", "assemble")

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FORGE_SSH = os.environ.get("FORGE_SSH", "name@forge")
FORGE_FFMPEG = os.environ.get("FORGE_FFMPEG", "/usr/bin/ffmpeg")
FORGE_FINISH_TMP = os.environ.get("FORGE_FINISH_TMP", "/tmp/forge-finish")

OUT_W, OUT_H = 1080, 1920        # the export canvas
OUT_FPS = 24


# ---------------------------------------------------------------------------
# Small process helpers
# ---------------------------------------------------------------------------
def _run(argv, timeout=600, cwd=None):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError("command failed (%d): %s\n%s\n%s"
                           % (p.returncode, " ".join(argv), (p.stdout or "")[-800:],
                              (p.stderr or "")[-1500:]))
    return p


def _duration(path):
    """Seconds of a media file via ffprobe, or 0.0 when unreadable."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nk=1:nw=1", path],
                           capture_output=True, text=True, timeout=30)
        return float((p.stdout or "").strip() or 0.0)
    except Exception:
        return 0.0


def probe(path):
    """(width, height, fps, frames, seconds) for a local file, or None."""
    return fl.video_probe(path)


# ---------------------------------------------------------------------------
# Paths + atomic persistence
# ---------------------------------------------------------------------------
def studio_root():
    return os.path.join(fl.DATA, "studio")


def project_dir(slug):
    return os.path.join(studio_root(), slug)


def project_path(slug):
    return os.path.join(project_dir(slug), "project.json")


def slugify(text):
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:48] or "untitled")


_SAVE_LOCK = threading.RLock()


def save_project(pj):
    """ATOMIC save: temp file in the same dir, then os.replace.

    A crash mid-write leaves the PREVIOUS project.json byte-for-byte intact — there is
    never a half-written file for a resumer to parse.
    """
    d = project_dir(pj["slug"])
    os.makedirs(d, exist_ok=True)
    path = project_path(pj["slug"])
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".project-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f, _SAVE_LOCK:
            # ⚠️ LOCKED: BOTH-cards mode renders two shots at once and both save the same
            # project; a dump while the other thread appends a take raced.
            json.dump(pj, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_project(slug):
    try:
        with open(project_path(slug)) as f:
            return project_migrate(json.load(f))
    except Exception:
        return None


def _shot_migrate(sh):
    """Default every field a shot can have, so a half-written shot never KeyErrors."""
    for k, v in (("n", 0), ("seconds", 3), ("intent", "character_action"), ("engine", "h3"),
                 ("video_mode", "r2v"), ("camera", ""), ("action", ""), ("line", ""),
                 ("speaker", ""), ("prompt", ""), ("audio_wav", None), ("words", None),
                 ("takes", []), ("chosen", None), ("status", "new"), ("start", 0.0),
                 ("voice_from", None)):
        sh.setdefault(k, v)
    return sh


def project_migrate(pj):
    """Backfill every field a project can have. Mirrors long_plan_migrate: a field added
    to the writer and forgotten here is a KeyError on the first read of an old project."""
    for k, v in (("slug", "untitled"), ("title", "untitled"), ("created", time.time()),
                 ("characters", []), ("idea", ""), ("length_s", 20), ("quality", "Draft"),
                 # ⚠️ gpus=1 is the DEFAULT and every existing caller keeps it. A 2-card run
                 # (Raylight, both cards on ONE render) is opted into per project, so nothing
                 # about the app's normal behaviour changes.
                 ("gpus", 1),
                 # every project made before 2026-10-17 is a 9:16 Studio short
                 ("aspect", "9:16"), ("captions", None), ("music", None), ("source", None),
                 ("pitch", None), ("pitches", []), ("script", None), ("shots", []),
                 ("voice", {}), ("cut", {}), ("log", [])):
        pj.setdefault(k, v)
    pj["shots"] = [_shot_migrate(dict(s)) for s in (pj.get("shots") or [])]
    return pj


def _blank(slug, characters, idea, length_s, quality, title=None, gpus=1, aspect="9:16"):
    return project_migrate({
        "slug": slug, "title": title or slug, "created": time.time(),
        "characters": list(characters), "idea": idea, "length_s": int(length_s),
        "quality": quality, "gpus": int(gpus), "pitch": None, "pitches": [], "script": None,
        "shots": [], "voice": {}, "cut": {}, "log": [], "aspect": aspect,
    })


def _log(pj, msg):
    pj.setdefault("log", []).append({"t": round(time.time(), 3), "msg": str(msg)})


# ---------------------------------------------------------------------------
# Director access (one shared instance; tests swap get_director)
# ---------------------------------------------------------------------------
_DIRECTOR = None


def get_director():
    global _DIRECTOR
    if _DIRECTOR is None:
        import director as _d
        _DIRECTOR = _d.Director()
    return _DIRECTOR


def _unload_director():
    """Release GPU1 before a TTS/align job — they do NOT fit alongside the LLM."""
    try:
        get_director().unload()
        return True
    except Exception:
        return False


def release_gpu1_for_render(tries=3, wait=3.0):
    """Make card 2 FREE before a 2-card (Raylight) render. Returns True when it is free.

    ⚠️ WHY THIS EXISTS: a Raylight run puts BOTH cards on ONE render (USP splits the tensor
    across ranks), but the Director LLM lives on card 2 (~17 GB resident). The two do not
    fit, so the Director must be off GPU1 before every 2-card submit — including the
    re-rolls that happen mid-QC, which is the case that would otherwise OOM silently.

    Unloading is a REQUEST, not a result: the model can still be resident (a lingering
    keep_alive, or a request still finishing), so this confirms via status() and retries.
    The cheap path matters — when the model is already gone this costs one status() call.
    """
    try:
        d = get_director()
        st = d.status()
    except Exception:
        return False
    if not (st or {}).get("loaded"):
        return True
    for _ in range(max(1, int(tries))):
        try:
            d.unload()
        except Exception:
            pass
        time.sleep(wait)
        try:
            if not (d.status() or {}).get("loaded"):
                return True
        except Exception:
            return False
    return False


def _char_dict(slug):
    d = c.read_character(slug)
    if d:
        return d
    return {"slug": slug, "name": slug or "the character"}


def _primary_char(pj):
    chars = pj.get("characters") or []
    return _char_dict(chars[0] if chars else "the character")


def _shot_char(pj, sh):
    name = sh.get("speaker") or ""
    # speakers are display names; map to a slug when we can, else the primary character
    for slug in (pj.get("characters") or []):
        d = _char_dict(slug)
        if name and (name == slug or name == d.get("name")):
            return slug, d
    slug = (pj.get("characters") or ["the character"])[0]
    return slug, _char_dict(slug)


# ---------------------------------------------------------------------------
# Stage: new / pitch / choose / script / shots
# ---------------------------------------------------------------------------
def new_project(characters, idea, length_s=20, quality="Draft", title=None, slug=None, gpus=1,
                aspect="9:16"):
    if isinstance(characters, str):
        characters = [characters]
    characters = [x for x in (characters or []) if x]
    if not characters:
        raise ValueError("new_project needs at least one character")
    if quality not in QUALITIES:
        raise ValueError("quality must be one of %s (got %r)" % (QUALITIES, quality))
    if int(gpus) not in (1, 2):
        raise ValueError("gpus must be 1 or 2 (got %r)" % (gpus,))
    if aspect not in ASPECTS:
        raise ValueError("aspect must be one of %s (got %r)" % (ASPECTS, aspect))
    if not slug:
        # never overwrite another project: the same title twice (a song made into a video
        # twice, two copies, "Untitled") used to replace the first run's video and takes
        base = slugify(title or idea or "untitled")
        slug, i = base, 2
        while os.path.exists(os.path.join(project_dir(slug), "project.json")):
            slug, i = "%s-%d" % (base, i), i + 1
    pj = _blank(slug, characters, idea, length_s, quality, title, gpus=int(gpus), aspect=aspect)
    _log(pj, "new project: %s" % ", ".join(characters))
    save_project(pj)
    return pj


def pitch(pj, n=5):
    """Director pitches premises; stored on the project (idempotent to re-run)."""
    d = get_director()
    d.load()
    res = d.pitch(_primary_char(pj), n=n, length_s=pj.get("length_s", 20),
                  brief=pj.get("idea") or "")
    pj["pitches"] = list(res)
    _log(pj, "pitched %d premises" % len(pj["pitches"]))
    save_project(pj)
    return pj["pitches"]


def choose_pitch(pj, i):
    if not pj.get("pitches"):
        raise ValueError("no pitches on the project — run pitch() first")
    try:
        chosen = pj["pitches"][int(i)]
    except (IndexError, ValueError):
        raise ValueError("pitch index %r out of range (have %d)"
                         % (i, len(pj["pitches"])))
    pj["pitch"] = chosen
    _log(pj, "chose pitch %s: %s" % (i, (chosen or {}).get("title", "")))
    save_project(pj)
    return chosen


def write_script(pj):
    if not pj.get("pitch"):
        raise ValueError("choose a pitch before writing the script")
    d = get_director()
    d.load()
    pj["script"] = d.script(_primary_char(pj), pj["pitch"], length_s=pj.get("length_s", 20))
    _log(pj, "wrote script: %s" % (pj["script"] or {}).get("title", ""))
    save_project(pj)
    return pj["script"]


def _clamp_seconds(v, fallback=3):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        n = fallback
    if n in VALID_SHOT_SECONDS:
        return n
    return min(VALID_SHOT_SECONDS, key=lambda s: abs(s - n))


def clamp_shots(shots, target):
    """Force every shot's `seconds` into {3,5,8} and bring the TOTAL close to `target`.

    Grow the last shots up the ladder toward the target first, then drop trailing shots
    while the total still overshoots (never dropping below one shot). Renumbers `n`.
    """
    shots = [s for s in shots if s]
    if not shots:
        return shots
    for s in shots:
        s["seconds"] = _clamp_seconds(s.get("seconds"))
    total = sum(s["seconds"] for s in shots)
    for s in reversed(shots):                       # grow toward target
        while total < target and _SECONDS_LADDER[s["seconds"]] < 2:
            nxt = VALID_SHOT_SECONDS[_SECONDS_LADDER[s["seconds"]] + 1]
            total += nxt - s["seconds"]
            s["seconds"] = nxt
    while total > target and len(shots) > 1:        # trim toward target
        total -= shots.pop()["seconds"]
    if total > target:                              # a lone shot still over: shrink it
        s = shots[-1]
        while total > target and _SECONDS_LADDER[s["seconds"]] > 0:
            prv = VALID_SHOT_SECONDS[_SECONDS_LADDER[s["seconds"]] - 1]
            total += prv - s["seconds"]
            s["seconds"] = prv
    for i, s in enumerate(shots, start=1):
        s["n"] = i
    return shots


def plan_shots(pj):
    if not pj.get("script"):
        raise ValueError("write the script before planning shots")
    d = get_director()
    d.load()
    raw = d.shots(_primary_char(pj), pj["script"])
    shots = [_shot_migrate(dict(s)) for s in raw]
    for s in shots:                                  # the Director annotates engine/video_mode
        s.setdefault("engine", "h3")
        s.setdefault("video_mode", "r2v")
    shots = clamp_shots(shots, int(pj.get("length_s", 20)))
    pj["shots"] = shots
    _log(pj, "planned %d shots (%ss total, target %ss)"
         % (len(shots), sum(s["seconds"] for s in shots), pj.get("length_s")))
    save_project(pj)
    return shots


# ---------------------------------------------------------------------------
# From a DIALOGUE scene: lines already written AND voiced become lip-sync shots
# ---------------------------------------------------------------------------
def _fit_seconds(dur):
    """The shortest legal shot length that holds `dur` seconds of speech. A line longer
    than 8 s gets a shot long enough for all of it (up to ~15 s, H3's longest) instead of
    being cut off mid-word."""
    for s in VALID_SHOT_SECONDS:
        if dur <= s:
            return s
    return round(min(15.0, dur + 0.5), 2)


def match_speaker(speaker, chars=None):
    """A saved character for a script SPEAKER name ("AVERY" -> avery), or None."""
    want = (speaker or "").strip().lower()
    if not want:
        return None
    chars = list(chars if chars is not None else fl.list_characters())
    for slug, name in chars:
        if want in (slug.lower(), str(name).strip().lower(), slugify(str(name))):
            return slug
    # a FIRST name ("CLARENCE" for "Clarence Holt") — as the Dialogue page casts — when only
    # one character has it
    firsts = [slug for slug, name in chars
              if str(name).strip().lower().split(" ")[0] == want]
    return firsts[0] if len(firsts) == 1 else None


def from_dialogue(dlg_slug, cast=None, quality="Draft", title=None, aspect="16:9", voices=None):
    """A 16:9 EPISODE from a voiced Dialogue scene: one lip-sync shot per line that has a kept
    take, that take as the shot's guide audio, the line as its words. `cast` overrides the
    speaker -> character match ({"AVERY": "gustav"}); a speaker with no character renders
    as the episode's first character. `voices` = the VOICE-ONLY speakers ({"DOORBELL": "the
    house's new AI doorbell"}): heard, never seen - their lines are shots of the person on
    screen LISTENING, mouth closed, with the voice laid in at the cut (voice_action).
    No Director call — nothing to wait for."""
    import dialogue as dlg
    dp = dlg.load_project(dlg_slug)
    if not dp:
        raise ValueError("no Dialogue scene called %r" % dlg_slug)
    cast = dict(cast or {})
    voices = {dlg.normalise_speaker(k): (v or "") for k, v in (voices or {}).items()}
    chars = fl.list_characters()
    pdir = os.path.join(dlg.dialogue_dir(), dp.get("slug", dlg_slug))
    shots, used, skipped = [], [], []
    for li in dp.get("lines", []):
        takes = li.get("takes") or []
        k = int(li.get("kept", 0) or 0)
        wav = os.path.join(pdir, takes[k].get("file", "")) if takes and k < len(takes) else None
        if not wav or not os.path.exists(wav):
            skipped.append(li.get("idx"))
            continue
        spk = dlg.normalise_speaker(li.get("speaker", ""))
        if spk in voices:
            dur = _duration(wav) or 3.0
            shots.append(_shot_migrate({
                "n": len(shots) + 1, "seconds": _fit_seconds(dur),
                "intent": "character_action", "engine": "h3", "video_mode": "r2v",
                "speaker": None, "voice_only": True, "voice_of": spk,
                "line": li.get("text", ""), "direction": li.get("direction") or "",
                "audio_wav": wav, "words": None, "status": "new",
                "voice_from": "dialogue"}))
            continue
        slug = cast.get(spk) or match_speaker(spk, chars)
        if slug and slug not in used:
            used.append(slug)
        name = (c.read_character(slug) or {}).get("name") or spk.title() if slug else spk.title()
        dur = _duration(wav) or 3.0
        act = "%s says the line with natural gestures and expression, facing the other " \
              "character" % name
        shots.append(_shot_migrate({
            "n": len(shots) + 1, "seconds": _fit_seconds(dur), "intent": "lipsync",
            "engine": "h3", "video_mode": "r2v", "speaker": slug or spk,
            "action": act, "prompt": act, "line": li.get("text", ""),
            "direction": li.get("direction") or "",
            "audio_wav": wav, "words": None, "status": "new", "voice_from": "dialogue"}))
    if not shots:
        raise ValueError("that Dialogue scene has no voiced lines yet \u2014 make its lines "
                         "first")
    if not used:
        # a scene that is ALL voice-only lines still needs someone on screen to hear them
        for slug in [v for v in cast.values() if v]:
            if c.read_character(slug) and slug not in used:
                used.append(slug)
                break
    if not used:
        raise ValueError("none of the speakers matches a saved character \u2014 make the "
                         "characters first (or pass cast=)")
    # who is on screen for each voice-only line: the last person seen speaking, else the
    # next one, else the first of the cast
    on = [sh.get("speaker") if not sh.get("voice_only") else None for sh in shots]
    for i, sh in enumerate(shots):
        if sh.get("voice_only"):
            before = [x for x in on[:i] if x in used]
            after = [x for x in on[i + 1:] if x in used]
            sh["speaker"] = (before[-1] if before else after[0] if after else used[0])
    pj = new_project(used, "from the Dialogue scene %s" % dlg_slug,
                     length_s=sum(s["seconds"] for s in shots), quality=quality,
                     title=title or dp.get("name") or dlg_slug, aspect=aspect)
    pj["shots"] = shots
    if voices:
        pj["voices_only"] = voices
    for sh in shots:
        if sh.get("voice_only"):
            sh["action"] = sh["prompt"] = voice_action(pj, sh["speaker"], sh)
    if len(used) >= 2:
        # two or more characters: shoot it on ONE set (make_set), not one place each
        for sh in shots:
            spk = sh["speaker"] if sh["speaker"] in used else used[0]
            sh["video_mode"] = "set"
            sh["action"] = sh["prompt"] = (voice_action(pj, spk, sh) if sh.get("voice_only")
                                           else set_action(pj, spk,
                                                           [u for u in used if u != spk], sh))
        where = (dp.get("where") or "").strip()
        if where:
            pj["where"] = where
    pj["source"] = {"dialogue": dlg_slug, "skipped_lines": skipped}
    _log(pj, "from dialogue %s: %d shots, %d unvoiced lines skipped"
         % (dlg_slug, len(shots), len(skipped)))
    save_project(pj)
    return pj


# ---------------------------------------------------------------------------
# Stage: voice lines (GPU1 — Director must be unloaded)
# ---------------------------------------------------------------------------
def _voice_store_dir(pj, slug):
    """Where a character's voice lives. Preferred: the character's own folder (so it is
    reused across projects). Fallback: inside the project, if that slug is not a saved
    character."""
    cdir = os.path.join(fl.CHARACTERS, slug)
    try:
        if slug and os.path.isdir(cdir):
            return cdir
    except Exception:
        pass
    return os.path.join(project_dir(pj["slug"]), "voice", slug or "voice")


def _has_saved_voice(slug):
    vdir = os.path.join(fl.CHARACTERS, slug)
    return (os.path.exists(os.path.join(vdir, "voice.wav"))
            and os.path.exists(os.path.join(vdir, "voice.json")))


def _voice_line(pj, slug, line, n, desc=None):
    """One spoken line. Reuse the character's saved voice (clone) if there is one;
    otherwise DESIGN a voice, use it, and SAVE it so every later line matches."""
    vdir = _voice_store_dir(pj, slug)
    vwav = os.path.join(vdir, "voice.wav")
    vjson = os.path.join(vdir, "voice.json")
    out = os.path.join(project_dir(pj["slug"]), "voice", "line_%s.wav" % n)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    if os.path.exists(vwav) and os.path.exists(vjson):
        ref_text = ""
        try:
            ref_text = (json.load(open(vjson)) or {}).get("ref_text") or ""
        except Exception:
            ref_text = ""
        audio_client.tts_clone(vwav, ref_text, line, out)
        return out

    if not desc:
        desc = "a flat, dry, deadpan speaking voice"
    audio_client.tts_design(desc, line, out)
    try:
        os.makedirs(vdir, exist_ok=True)
        if os.path.abspath(out) != os.path.abspath(vwav):
            shutil.copyfile(out, vwav)
        with open(vjson, "w") as f:
            json.dump({"ref_text": line, "desc": desc}, f, indent=1)
        _log(pj, "saved %s's designed voice -> %s" % (slug, vwav))
    except Exception as exc:
        _log(pj, "could not persist voice for %s: %r" % (slug, exc))
    return out


def voice_lines(pj):
    """TTS every non-empty shot line, align it, and mark lip-sync shots for guide audio.

    ⚠️ ORDER MATTERS: the Director LLM (~19 GB on GPU1) and the TTS/aligner jobs (~5 GB)
    do NOT fit together, so any voice description the LLM must write is gathered FIRST,
    then the Director is UNLOADED, and only then do the speech jobs run.
    """
    if audio_client is None:
        raise RuntimeError("audio client not importable (expected <repo>/audio/client.py)")

    todo = []
    for sh in pj["shots"]:
        line = (sh.get("line") or "").strip()
        if not line:
            continue
        if sh.get("audio_wav") and os.path.exists(sh["audio_wav"]) and sh.get("words"):
            continue                                 # resume: already voiced + aligned
        if (sh.get("voice_from") == "dialogue" and sh.get("audio_wav")
                and os.path.exists(sh["audio_wav"])):
            continue                                 # voiced in DIALOGUE: keep that take
        slug, _ = _shot_char(pj, sh)
        todo.append((sh, slug, line))

    descs = {}
    need_desc = [slug for _, slug, _ in todo if not _has_saved_voice(slug)]
    if need_desc:
        d = get_director()
        for slug in dict.fromkeys(need_desc):
            try:
                descs[slug] = d.voice_description(_char_dict(slug))
            except Exception as exc:
                _log(pj, "voice_description failed for %s: %r" % (slug, exc))
                descs[slug] = "a flat, dry, deadpan speaking voice"

    _unload_director()                               # now free GPU1 for the speech jobs

    for sh, slug, line in todo:
        wav = _voice_line(pj, slug, line, sh["n"], desc=descs.get(slug))
        sh["audio_wav"] = wav
        try:
            sh["words"] = audio_client.align(wav, line)
        except Exception as exc:
            _log(pj, "align failed on shot %s: %r" % (sh["n"], exc))
            sh["words"] = []
        _log(pj, "voiced shot %s (%d words)" % (sh["n"], len(sh.get("words") or [])))
    pj.setdefault("voice", {})
    save_project(pj)
    return pj["shots"]


# ---------------------------------------------------------------------------
# Stage: estimate (price BEFORE spend)
# ---------------------------------------------------------------------------
def apply_shot_edit(sh, action=None, line=None):
    """Apply the user's edit of a shot so the RENDER sees it. Returns the fields changed.

    ⚠️ shot_params() renders sh["prompt"] — the Director-written render prompt, which the
    window never shows. Storing only sh["action"] (the old SAVE SHOT EDITS) changed nothing
    that reached the rig. An action edit is therefore carried INTO the prompt: the old action
    is replaced where it appears verbatim, otherwise the new action leads the prompt.
    A line edit clears the shot's audio, or voice_lines() skips it as already voiced.
    """
    changed = []
    if action is not None:
        new, old = action.strip(), (sh.get("action") or "").strip()
        if new and new != old:
            p = sh.get("prompt") or ""
            if old and old in p:
                sh["prompt"] = p.replace(old, new)
            else:
                sh["prompt"] = ("%s. %s" % (new.rstrip("."), p)).strip() if p else new
            sh["action"] = new
            changed.append("action")
    if line is not None:
        new = line.strip()
        if new != (sh.get("line") or "").strip():
            sh["line"] = new
            sh["audio_wav"] = None
            sh["words"] = None
            changed.append("line")
    return changed


CAMERA_NONE = "No move"


def set_camera(sh, move):
    """Give a shot a camera move by NAME (apps.MOVES) — or CAMERA_NONE. The words go on the end
    of the render prompt; the previous move's words are taken off first, so switching moves
    never stacks two."""
    import apps as _apps
    old = sh.get("camera_words") or ""
    p = (sh.get("prompt") or "").strip()
    if old and old in p:
        p = p.replace(" " + old, "").replace(old, "").strip()
    words = dict(_apps.MOVES).get(move, "") if move and move != CAMERA_NONE else ""
    if words:
        words = words[:1].upper() + words[1:] + "."
        p = ("%s %s" % (p, words)).strip() if p else words
    sh["prompt"] = p
    sh["camera_move"] = move if words else None
    sh["camera_words"] = words or None
    return sh


def shot_params(pj, sh, seed=-1):
    w, h = canvas(pj)
    vm = sh.get("video_mode") or "r2v"
    if vm == "set":
        vm = "i2v"          # THE SET: the shot's first frame is its plate (see make_set)
    return {
        "prompt": sh.get("prompt") or "",
        "width": w, "height": h, "steps": STEPS,
        "length": c.h3_frames(sh.get("seconds", 3)),
        "seed": seed,
        "prefix": "studio_%s_s%s" % (pj["slug"], sh.get("n", 0)),
        "video_mode": vm, "audio": True, "lora": None, "lora_strength": 1.0,
        "quality": pj.get("quality", "Draft"), "gpus": int(pj.get("gpus", 1) or 1),
    }


def estimate(pj):
    """Per-shot seconds via forge_core.estimate_seconds, with the engine kept warm after
    the first shot (the load term applies once, not per shot)."""
    per, total, warm = [], 0.0, None
    for sh in pj["shots"]:
        p = shot_params(pj, sh)
        try:
            t = c.estimate_seconds("video", p, [], warm_engine=warm)
        except Exception as exc:
            t = None
            _log(pj, "estimate failed for shot %s: %r" % (sh["n"], exc))
        per.append({"n": sh["n"], "seconds": sh["seconds"], "canvas": [p["width"], p["height"]],
                    "steps": STEPS, "estimate_s": (round(t, 1) if t else None)})
        if t:
            total += t
            warm = "h3"
    return {"total_s": round(total, 1), "per_shot": per}


# ---------------------------------------------------------------------------
# Stage: the SET — one place, the whole cast in it, every shot starts there
# ---------------------------------------------------------------------------
# Why: an r2v shot pins ONE character from their still and invents everything else, so a
# two-hander came out as two different places (Clarence on his green screen, Gustav in a
# plaza), each talking to an invented stranger who lent Clarence his scarf by shot 3.
# The set fixes all three at once: ONE wide plate with both characters (an image edit
# from their stills), then a closer single per speaker made FROM that plate, so place,
# light and clothes are shared. A 'set' shot is an i2v render whose first frame is its
# plate: the opening shot gets the wide, later shots the speaker's single.
SET_SIZE = (1280, 704)          # the Final 16:9 canvas; the plate is the shot's first frame


def set_size(pj):
    """The plate canvas: the project's own Final canvas, so a 9:16 short gets a tall plate."""
    return canvas(dict(pj or {}, quality="Final"))
SET_STEPS = 25
SET_DEFAULT_WHERE = "a small, slightly shabby local TV studio, warm practical lights"


def _char_look(slug):
    """(name, look) for a prompt: the character's signature feature when it has one, else
    the description part of its still prompt."""
    d = _char_dict(slug)
    name = d.get("name") or slug
    picks = d.get("picks") if isinstance(d.get("picks"), dict) else {}
    look = (d.get("feature") or picks.get("Feature") or "").strip()
    if not look:
        p = (d.get("prompt") or "").strip()
        cut = p.find("in frame.")
        look = (p[cut + 9:] if cut >= 0 else p).strip()[:220]
    return name, look.rstrip(". ")


def _char_outfit(slug):
    """What they WEAR, from the Wardrobe pick or the still prompt's "He wears …" sentence.
    The two-person plate needs it named: with both stills in one edit the model swapped
    clothes and features between them (run-through 2026-10-17: Gustav came out in Clarence's
    blazer and towel, with Clarence's ears)."""
    d = _char_dict(slug)
    picks = d.get("picks") if isinstance(d.get("picks"), dict) else {}
    w = (picks.get("Wardrobe") or "").strip()
    m = re.search(r"\b(?:He|She|They) wears? ([^.]+)\.", d.get("prompt") or "")
    if m:
        w = m.group(1).strip()
    return w


def _char_desc(slug):
    """One phrase that pins a person: their gag (minus any simile) and what they wear."""
    name, look = _char_look(slug)
    feat, _n = _gag(slug)
    look = feat or look
    wear = _char_outfit(slug)
    bits = [b for b in (look, ("wearing %s" % wear) if wear else "") if b]
    return name, ", ".join(bits)


def set_cast(pj):
    """The characters on the set, in the order they first speak (then the rest)."""
    order = []
    for sh in pj.get("shots") or []:
        slug, _ = _shot_char(pj, sh)
        if slug and slug not in order:
            order.append(slug)
    for slug in pj.get("characters") or []:
        if slug not in order:
            order.append(slug)
    return order[:3]


def place_image(pj):
    """The kept PLACE picture this project's set is built in (places.py), or None."""
    slug = (pj or {}).get("place")
    if not slug:
        return None
    try:
        import places as _pl
        img = _pl.image(slug)
        return img if img and os.path.exists(img) else None
    except Exception:
        return None


def set_where(pj):
    if place_image(pj):
        try:
            import places as _pl
            d = _pl.describe(pj["place"])
            if d:
                return d
        except Exception:
            pass
    return (pj.get("where") or "").strip() or SET_DEFAULT_WHERE


def master_prompt(pj, cast):
    names = []
    for i, slug in enumerate(cast, start=1):
        name, look = _char_look(slug)
        names.append((i, name, look))
    who = " and ".join("%s from <image%d>" % (n, i) for i, n, _l in names)
    looks = " ".join("%s: %s." % (n, l) for _i, n, l in names if l)
    return ("A film still. %s stand together in %s, turned toward each other in the middle "
            "of a conversation. Each person keeps exactly the face, hair, body and clothes of "
            "their own reference image — nothing is swapped or shared between them. %s "
            "%s, everyone in focus, photorealistic, natural light."
            % (who, set_where(pj), looks,
               "A tall vertical frame, both people in it from the waist up, close together"
               if (pj or {}).get("aspect") == "9:16" else
               "Medium-wide two-shot at eye level, everyone in frame from the knees up")
            ).strip()


# words in a gag that image models draw literally ("ears ... like heavy wet towels" put a
# towel round Clarence's neck, then Gustav's): the simile is dropped from SET prompts
_SIMILE = re.compile(r"\s*,?\s*\b(?:like|as if|as though)\b.*$", re.I)


_BODY_PARTS = ("ears|ear|nose|head|hands|hand|feet|foot|neck|eyebrows|eyebrow|teeth|tooth|"
               "chin|forehead|arms|arm|legs|leg|eyes|eye|moustache|mustache|beard|hair|mouth|"
               "lips|fingers|belly|jaw|smile|shoulders|torso")


def _gag(slug):
    """(full gag without its simile, short noun) — 'ears so large ... chest' / 'ears'."""
    d = _char_dict(slug)
    picks = d.get("picks") if isinstance(d.get("picks"), dict) else {}
    feat = (d.get("feature") or picks.get("Feature") or "").strip().rstrip(".")
    if not feat:
        return "", ""
    feat = _SIMILE.sub("", feat).strip()
    import weirdos as _w
    noun = _w._gag_noun(feat)
    # "ears to her chin" -> "ears"; "enormous wild mane of silver-white hair" -> "mane"
    noun = re.split(r"\s+(?:to|of|on|in|down|over|around|from|across|below)\s+", noun)[0]
    noun = noun.split()[-1] if noun else ""
    part = re.search(r"\b(%s)\b" % _BODY_PARTS, feat, re.I)
    if part and noun.lower() not in ("mane", "locks", "hairdo", "mop"):
        noun = part.group(1)                 # "a neck as long as a giraffe's" -> "neck"
    return feat, {"mane": "hair", "locks": "hair", "hairdo": "hair", "mop": "hair"}.get(
        noun.lower(), noun)


def _only_theirs(slug, others, img):
    """Sentences that stop features and clothes leaking between people: each OTHER person's
    gag and outfit belong to them alone; `slug`'s own are as in <image`img`>."""
    name = _char_look(slug)[0]
    out = []
    for o in others:
        if o == slug:
            continue
        oname = _char_look(o)[0]
        feat, noun = _gag(o)
        mine, _ = _gag(slug)
        if feat and noun and noun.lower() not in mine.lower():
            verb = "are" if noun.endswith("s") and not noun.endswith("ss") else "is"
            out.append("Only %s has the exaggerated %s; %s's %s %s ordinary, exactly as in "
                       "<image%d>." % (oname, noun, name, noun, verb, img))
        wear = _char_outfit(o)
        if wear:
            out.append("Only %s wears %s." % (oname, wear))
    return " ".join(out)


def _framing(pj, n):
    if (pj or {}).get("aspect") == "9:16":
        return ("A tall vertical frame, %s from the waist up, 28 mm lens, deep focus, "
                "photorealistic film still, natural light."
                % ("both people" if n > 1 else "the person"))
    return ("Medium-wide shot at eye level, %s from the knees up, 28 mm lens, deep focus, "
            "photorealistic film still, natural light."
            % ("everyone" if n > 1 else "the person"))


SIDES = ("on the left", "on the right", "in the middle")
SIDE_X = {"on the left": 0.1, "on the right": 0.9, "in the middle": 0.5}   # across the wide


def _side_x(i):
    """Where cast member i stands on the wide, 0..1 - the SAME order the plate is built in
    (left, right, then middle), so the push and the eyelines agree with the picture."""
    return SIDE_X[SIDES[min(i, len(SIDES) - 1)]]


def place_prompt(pj, slug, n):
    """Pass 1 of the plate: the first person, alone, from their own still — put INTO the
    kept place picture (<image2>) when the project has one."""
    name, desc = _char_desc(slug)
    room = (" The %s of the frame is left open, as if someone is standing just out of shot."
            % ("right side" if n > 1 else "")) if n > 1 else ""
    if place_image(pj):
        return ("<image2> is the place: keep it EXACTLY - the same room, objects, light and "
                "layout, the same camera position. Put the person from <image1> \u2014 %s "
                "\u2014 into it, with exactly the same face, hair, body and clothes as "
                "<image1>, standing %s, turned %s, at the right scale for the room.%s %s"
                % (desc or name, SIDES[0] if n > 1 else "in the frame",
                   "toward the right as if mid-conversation" if n > 1 else "toward the camera",
                   room, _framing(pj, 1)))
    return ("A film still. The person from <image1> \u2014 %s \u2014 with exactly the same "
            "face, hair, body and clothes as <image1>, stands %s in %s, turned %s.%s %s"
            % (desc or name, SIDES[0] if n > 1 else "in the frame", set_where(pj),
               "toward the right as if mid-conversation" if n > 1 else "toward the camera",
               room, _framing(pj, 1)))


def add_prompt(pj, slug, k, n):
    """Pass k+1: add one more person to the plate, from their own still, changing nothing."""
    name, desc = _char_desc(slug)
    return ("<image1> is the scene: keep it EXACTLY as it is \u2014 the place, the light and "
            "the person already in it, unchanged in every detail. Add the person from <image2> "
            "\u2014 %s \u2014 standing %s, turned toward the other person, with exactly their "
            "own face, hair, ears, body and clothes from <image2>. The new person borrows "
            "NOTHING from anyone else in the scene: not their clothes, hair or features. %s %s"
            % (desc or name, SIDES[min(k, 2)],
               " ".join(x for x in [_only_theirs(slug, set_cast(pj) or [], 2)] +
                        [_only_theirs(e, [slug], 1)
                         for e in (set_cast(pj) or [])[:k]] if x),
               _framing(pj, n)))


def _look_dir(pj, slug, cast):
    """Which way a character looks in their close-up: toward where the others stand on the
    wide (the first in the cast stands left, so looks screen-right) — keeps the 180 line."""
    try:
        i = list(cast).index(slug)
    except ValueError:
        return "toward the camera's right"
    x = _side_x(i)
    if x == 0.5:
        return "just off the lens, toward the others"
    return "screen-right" if x < 0.5 else "screen-left"


def _close_lens(pj):
    """The close-up's lens sentence: a project may set its own ("lens") - a gilded satire is
    shot WIDE and close (distorted, unflattering), not the flattering 85 mm portrait."""
    return ((pj or {}).get("lens") or
            "85 mm lens, shallow depth of field, the room soft behind.").strip()


def single_prompt(pj, slug, cast):
    """A CLEAN single: the speaker alone, chest-up, eyes just off the lens toward the
    others' side, long lens with the room soft behind. The old 'soft shoulder at the edge'
    made the edit model add the other person on top of the one already in the wide — the
    review panel found Vivian twice in Gerald's close-up, and Vivian's was a two-shot."""
    name, look = _char_desc(slug)
    others = [s_ for s_ in cast if s_ != slug]
    look_s = (" \u2014 " + look) if look else ""
    if not others:                           # a one-person set
        return ("The same place and light as <image1>, a moment later. Closer: a chest-up "
                "medium close-up of %s, the person from <image2>%s, with exactly the face, "
                "hair, ears and clothes from <image2>, facing the camera at three-quarters. "
                "%s Photorealistic film still." % (name, look_s, _close_lens(pj)))
    return ("The same place and light as <image1>, a moment later, the camera much "
            "closer. A head-and-shoulders close-up of %s ALONE, the person from <image2>%s, "
            "with exactly the face, hair, ears and clothes from <image2>: the head fills the "
            "top third of the frame and the frame cuts mid-chest, body turned three-quarters. "
            "Nobody else is in the frame \u2014 no shoulder, "
            "no second person in the background. Eyes just past the lens, looking %s at "
            "someone off-screen, mouth closed, about to speak. "
            "%s %s Photorealistic film "
            "still." % (name, look_s, _look_dir(pj, slug, cast), _close_lens(pj),
                        _only_theirs(slug, cast, 2)))


def _plate_from(pid=None, m=None):
    """The picture a plate pass produced (a local path). `m` may be passed in (the queue's
    meta) to skip the wait."""
    m = m if m is not None else c.wait_and_collect(pid, timeout=1800)
    _pid = m.get("id") or pid
    for f in (m.get("images") or []):
        fn = f.get("file") or ""
        if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return os.path.join(c.RUNS, _pid, fn)
    raise RuntimeError("the set plate produced no image (pid %s, %s)"
                       % (_pid, m.get("error") or m.get("status")))


def make_set(pj, seed=-1, progress=None, only=None, resume=False):
    """Build (or rebuild) the set: the wide plate, then one single per speaker FROM it.
    `only` = "master" or a character slug rebuilds just that one (a single needs the wide).
    `resume`: keep a wide and singles already made for this same place and cast (a busy
    card on the second close-up no longer redoes every pass). Each finished pass is saved.
    Writes <project>/set/*.png and pj["set"]. Returns pj["set"]."""
    cast = set_cast(pj)
    stills = [c.character_still(s) for s in cast]
    missing = [s for s, p in zip(cast, stills) if not p]
    if missing:
        raise ValueError("make a still for %s first — the set is built from the "
                         "characters' stills" % ", ".join(missing))
    sdir = os.path.join(project_dir(pj["slug"]), "set")
    os.makedirs(sdir, exist_ok=True)
    cur = dict(pj.get("set") or {})
    same = (cur.get("where") == set_where(pj) and cur.get("cast") == cast and
            (cur.get("place") or None) == (pj.get("place") or None))
    keep = bool(resume and same)
    if not same:
        cur = {"master": None}
    cur["where"] = set_where(pj)
    cur["cast"] = cast
    cur["place"] = pj.get("place")
    cur["singles"] = dict(cur.get("singles") or {})
    w, h = set_size(pj)

    def _save():
        pj["set"] = dict(cur)
        save_project(pj)

    def edit(prompt, refs, out, label):
        if progress:
            progress(label)
        p = {"prompt": prompt, "steps": SET_STEPS, "seed": seed, "prefix": "forge_lab_set",
             "custom_size": True, "width": w, "height": h}
        # through the QUEUE (2026-10-22): a set pass waits for a card like every other render
        m = _dsp.render(label or "The set", "edit", dict(p), refs, need="image", est_s=EST_IMAGE)
        shutil.copyfile(_plate_from(m=m), out)
        return out

    have_master = bool(cur.get("master") and os.path.exists(cur["master"]))
    if only == "master" or not have_master or (only is None and not keep):
        # ONE PERSON AT A TIME: the first is placed from their own still, then each next one
        # is ADDED to that plate from THEIR own still. All stills in one edit blended the
        # people (swapped clothes and features); this keeps each identity on its own pass.
        pimg = place_image(pj)
        plate = edit(place_prompt(pj, cast[0], len(cast)),
                     [stills[0]] + ([pimg] if pimg else []),
                     os.path.join(sdir, "master_1.png" if len(cast) > 1 else "master.png"),
                     "the set: %s on the set" % _char_look(cast[0])[0])
        for k in range(1, len(cast)):
            last = k == len(cast) - 1
            plate = edit(add_prompt(pj, cast[k], k, len(cast)), [plate, stills[k]],
                         os.path.join(sdir, "master.png" if last else "master_%d.png" % (k + 1)),
                         "the set: adding %s" % _char_look(cast[k])[0])
        cur["master"] = plate
        cur["singles"] = {}                 # a new wide invalidates every single
        if only == "master":
            only = None
        keep = False
        _save()
    speakers = [s for s in cast if any(_shot_char(pj, sh)[0] == s
                                       for sh in pj.get("shots") or [])]
    for slug in speakers:
        if only not in (None, slug):
            continue
        if only is None and keep and os.path.exists(cur["singles"].get(slug) or ""):
            continue                        # made on the run that stopped
        cur["singles"][slug] = edit(single_prompt(pj, slug, cast),
                                    [cur["master"], stills[cast.index(slug)]],
                                    os.path.join(sdir, "single_%s.png" % slug),
                                    "the set: a closer shot of %s" % _char_look(slug)[0])
        _save()
    pj["set"] = cur
    _log(pj, "set built in %s: %s" % (cur["where"], ", ".join(cast)))
    save_project(pj)
    return cur


def set_ready(pj):
    s = pj.get("set") or {}
    if not (s.get("master") and os.path.exists(s["master"])):
        return False
    if s.get("where") != set_where(pj) or (s.get("place") or None) != (pj.get("place") or None):
        return False                         # the place changed: rebuild
    return all(os.path.exists(s.get("singles", {}).get(slug, "") or "")
               for slug in {(_shot_char(pj, sh)[0]) for sh in pj.get("shots") or []
                            if sh.get("video_mode") == "set" and not is_wide(sh)})


def is_wide(sh):
    """A set shot that starts from the WIDE (everyone in frame): the opening shot, and the
    mid-scene two-shot vary_sizes() picks. Every other set shot is the speaker's close-up."""
    return sh.get("n") == 1 or bool(sh.get("wide"))


def vary_sizes(pj):
    """Not every line in a close-up (round-2 director: "vary shots: two-shot at midpoint").
    A set scene with two or more people and five or more lines goes back to the WIDE once,
    on a line in the middle (never the first, the last two, or a long take), so the
    audience sees them together again before the button. Re-writes that shot's action.
    Returns the shot number made wide, or None."""
    cast = list(pj.get("characters") or [])
    lines = [sh for sh in line_shots(pj) if sh.get("video_mode") == "set"]
    if len(cast) < 2 or len(lines) < 5 or pj.get("music_video"):
        return None
    if any(sh.get("wide") for sh in lines):
        return None                                  # already chosen (or Ryan picked one)
    mid = len(lines) // 2
    for i in (mid, mid - 1, mid + 1):
        if 1 <= i < len(lines) - 2 and len(lines[i].get("line_starts") or []) <= 1 and \
                not lines[i].get("voice_only"):
            sh = lines[i]
            sh["wide"] = True
            spk = sh.get("speaker") if sh.get("speaker") in cast else cast[0]
            sh["action"] = sh["prompt"] = set_action(pj, spk, [c_ for c_ in cast
                                                               if c_ != spk], sh)
            return sh["n"]
    return None


def set_plate(pj, sh):
    """The plate a 'set' shot starts from: the wide for the opening shot and a vary_sizes
    two-shot (and as a fallback), the speaker's single otherwise."""
    s = pj.get("set") or {}
    master = s.get("master")
    if not (master and os.path.exists(master)):
        raise ValueError("shot %s starts from the set, but the set isn't built yet"
                         % sh.get("n"))
    if is_wide(sh):
        return master
    slug, _ = _shot_char(pj, sh)
    single = (s.get("singles") or {}).get(slug)
    return single if single and os.path.exists(single) else master


REACTION_HOLD = 1.2           # seconds of the listener's face after the button (0.8 was
                              # too short for the laugh to land - round-2 review)
CUTAWAY = 0.7                 # a mid-scene glance at the listener, at the turn
PENULT_HOLD = 0.4             # the pause BEFORE the button (after the setup line)
REACTION_IN = 0.4             # skip the clip's first beat (the move starts from stillness)


def reaction_prompt(pj, slug, others):
    name = _char_look(slug)[0]
    other = " and ".join(_char_look(o)[0] for o in others) or "the other person"
    keep = []
    feat, noun = _gag(slug)
    if noun:
        keep.append("%s's %s stays exactly as large and as shaped as in the first frame." %
                    (name, noun))
    return ("%s, alone in the frame, LISTENS to %s just off-screen and takes it in: a slow "
            "blink, then a small, sincere, disbelieving reaction - the eyebrows move, the "
            "mouth stays CLOSED, no speaking at all. Eyes toward %s, not the lens. %s keeps "
            "the same face, hair and clothes as in the first frame. %s"
            % (name, other, other, name, " ".join(keep))).strip()


def add_reactions(pj):
    """One silent REACTION clip per character on the set (review panel: "half the laughs in
    a two-hander come from the listener"; every cut went to the speaker). They are shots
    flagged `is_reaction` AFTER the lines — the edit cuts to the listener's face after the
    button. Idempotent. Returns the number added."""
    used = list(pj.get("characters") or [])
    shots = pj.get("shots") or []
    if len(used) < 2 or not any(sh.get("video_mode") == "set" for sh in shots):
        return 0
    have = {sh.get("speaker") for sh in shots if sh.get("is_reaction")}
    added = 0
    for slug in set_cast(pj):
        if slug in have:
            continue
        others = [u for u in used if u != slug]
        n = max([sh.get("n", 0) for sh in shots] + [0]) + 1
        shots.append(_shot_migrate({
            "n": n, "seconds": 3, "intent": "character_action", "engine": "h3",
            "video_mode": "set", "speaker": slug, "is_reaction": True, "line": "",
            "action": reaction_prompt(pj, slug, others),
            "prompt": reaction_prompt(pj, slug, others), "status": "new"}))
        added += 1
    pj["shots"] = shots
    return added


LONG_TAKE_MAX = 14.5          # seconds of speech in one take (H3 tops out at ~15.08 s)
LONG_TAKE_GAP = 0.3           # the pause stitched between two lines inside a take
JUMP = 0.12                   # the vlog "jump" zoom at each new line inside a long take


def merge_long_takes(pj, max_s=LONG_TAKE_MAX, gap=LONG_TAKE_GAP):
    """ONE-PERSON videos: consecutive lines become one continuous take of up to ~15 s
    (Ryan: "why are all the shots so short"). One voice, so lip sync is safe; the take keeps
    every line's gesture in order and `line_starts` (where each line begins in the take's
    voice) for the jump zooms. Two-person videos are left one line per shot (the right
    mouth moves; punch-ins and reaction cutaways need the cuts). Returns takes made."""
    if len(pj.get("characters") or []) != 1 or pj.get("music_video"):
        return 0
    shots = line_shots(pj)
    if len(shots) < 2:
        return 0
    groups, cur, cur_s = [], [], 0.0
    for sh in shots:
        d = _duration(sh.get("audio_wav") or "") if sh.get("audio_wav") else 0.0
        if not d or sh.get("voice_only"):           # another voice: the take stops there
            if cur:
                groups.append(cur)
            groups.append([sh])
            cur, cur_s = [], 0.0
            continue
        add = d + (gap if cur else 0.0)
        if cur and cur_s + add > max_s:
            groups.append(cur)
            cur, cur_s = [], 0.0
            add = d
        cur.append(sh)
        cur_s += add
    if cur:
        groups.append(cur)
    if all(len(g) == 1 for g in groups):
        return 0
    vdir = os.path.join(project_dir(pj["slug"]), "voice")
    os.makedirs(vdir, exist_ok=True)
    out, made = [], 0
    for gi, g in enumerate(groups, start=1):
        if len(g) == 1:
            out.append(g[0])
            continue
        wav = os.path.join(vdir, "take_%02d.wav" % gi)
        args = [FFMPEG, "-v", "error", "-y"]
        for sh in g:
            args += ["-i", sh["audio_wav"]]
        parts, starts, t = [], [], 0.0
        for i, sh in enumerate(g):
            starts.append(round(t, 3))
            parts.append("[%d:a]aformat=sample_fmts=fltp:sample_rates=24000:"
                         "channel_layouts=mono%s[a%d]"
                         % (i, (",apad=pad_dur=%.3f" % gap) if i < len(g) - 1 else "", i))
            t += _duration(sh["audio_wav"]) + (gap if i < len(g) - 1 else 0.0)
        fc = ";".join(parts) + ";" + "".join("[a%d]" % i for i in range(len(g))) + \
            "concat=n=%d:v=0:a=1[out]" % len(g)
        _run(args + ["-filter_complex", fc, "-map", "[out]", wav], timeout=300)
        first = g[0]
        take = dict(first)
        take.update({
            "line": " ".join((sh.get("line") or "").strip() for sh in g),
            "direction": "; ".join(d for d in ((sh.get("direction") or "").strip()
                                                for sh in g) if d),
            "gesture": "; then ".join(x for x in ((sh.get("gesture") or "").strip()
                                                  for sh in g) if x),
            "reaction": "", "audio_wav": wav, "words": None, "takes": [], "chosen": None,
            "status": "new", "seconds": round(min(15.0, t + 0.7), 2),
            "line_starts": starts, "long_take": len(g)})
        if take.get("video_mode") == "set":
            take["action"] = take["prompt"] = set_action(pj, take["speaker"], [], take)
        elif take["gesture"]:
            take["prompt"] = take["action"] = ("%s While speaking: %s." % (
                (first.get("prompt") or "").rstrip(), take["gesture"])).strip()
        out.append(take)
        made += 1
    for i, sh in enumerate(out, start=1):
        sh["n"] = i
    pj["shots"] = out + [sh for sh in pj.get("shots") or [] if sh.get("is_reaction")]
    _log(pj, "long takes: %d lines -> %d shots" % (len(shots), len(out)))
    return made


def line_shots(pj):
    return [sh for sh in pj.get("shots") or [] if not sh.get("is_reaction")]


def apply_acting(pj, acting):
    """Give each shot its line's acting ([{gesture, reaction}] in shot order) and rebuild
    the set shots' prompts with it. Missing entries keep the generic direction."""
    used = list(pj.get("characters") or [])
    for i, sh in enumerate(line_shots(pj)):
        a = acting[i] if i < len(acting or []) and isinstance(acting[i], dict) else {}
        sh["gesture"] = (a.get("gesture") or "").strip()[:160]
        sh["reaction"] = (a.get("reaction") or "").strip()[:160]
        if sh.get("voice_only"):
            # heard, never seen: the acting is the LISTENER's, on screen
            sh["action"] = sh["prompt"] = voice_action(pj, sh.get("speaker") or (
                used[0] if used else ""), sh)
        elif sh.get("video_mode") == "set":
            spk = sh["speaker"] if sh.get("speaker") in used else (used[0] if used else
                                                                  sh.get("speaker"))
            sh["action"] = sh["prompt"] = set_action(pj, spk, [u for u in used if u != spk],
                                                     sh)
        elif sh["gesture"]:
            # r2v shots too (a vlog with no set lost its acting): the gesture, once
            base = re.sub(r"\s*While speaking: [^.]*\.\s*$", "",
                          (sh.get("prompt") or "").rstrip())
            sh["prompt"] = sh["action"] = ("%s While speaking: %s." % (
                base, sh["gesture"].rstrip("."))).strip()
    return pj


def set_action(pj, slug, others, sh=None):
    """What a 'set' lip-sync shot does. The opening shot (the wide) shows everyone: the
    speaker talks, the others LISTEN AND REACT. Later shots are the speaker's clean
    close-up: alone, eyes toward the others off-screen. Each line's ACTING (sh["gesture"],
    sh["reaction"], from the writer's stage-directions pass) goes in — the review panel:
    "small natural gestures, the camera holds still" made everyone stand like statues."""
    sh = sh or {}
    name = _char_look(slug)[0]
    other = " and ".join(_char_look(o)[0] for o in others) or "the other person"
    keep = []
    for x in [slug] + list(others):
        feat, noun = _gag(x)
        if noun:
            keep.append("%s's %s" % (_char_look(x)[0], noun))
    keep_s = (" %s stay exactly as large and as shaped as in the first frame, in every "
              "frame." % " and ".join(keep)) if keep else ""
    gesture = (sh.get("gesture") or "").strip().rstrip(".")
    reaction = (sh.get("reaction") or "").strip().rstrip(".")
    act = (" While speaking, %s %s." % (name, gesture)) if gesture else (
        " %s speaks with clear, specific expression and one natural gesture that fits the "
        "line." % name)
    if not others:                    # one person: talking straight to us (vlog, weather)
        return ("%s talks straight to the camera; %s's lips move with the words.%s %s keeps "
                "the same face, hair and clothes as in the first frame.%s"
                % (name, name, act, name, keep_s))
    if is_wide(sh):
        listen = (" %s listens and reacts: %s." % (other, reaction)) if (reaction and others) \
            else (" %s listens and visibly reacts." % other if others else "")
        return ("%s talks to %s; only %s's lips move with the words.%s%s Everyone keeps the "
                "same face, hair and clothes as in the first frame.%s"
                % (name, other, name, act, listen, keep_s))
    return ("%s, alone in the frame, talks to %s just off-screen; only %s's lips move with "
            "the words, eyes toward %s, not the lens.%s %s keeps the same face, hair and "
            "clothes as in the first frame.%s" % (name, other, name, other, act, name, keep_s))


def voice_label(pj, sh):
    """What a voice-only line's voice comes from, in words ("the house's new AI doorbell")."""
    v = sh.get("voice_of") or ""
    what = ((pj.get("voices_only") or {}).get(v) or "").strip().rstrip(".")
    return what or ("a voice off-screen" if not v else "the %s" % v.lower())


def voice_action(pj, slug, sh=None):
    """A VOICE-ONLY line (2026-10-24: a doorbell, a self-driving taxi, a voice on the
    intercom - heard, never seen). The shot is the person ON SCREEN hearing it: mouth CLOSED,
    reacting, with the line's acting (gesture / reaction from the stage-directions pass,
    which writes them for the listener on these lines). Not lip-synced and sent with no
    guide audio; the voice is laid in at the cut like every other line (_mix_audio)."""
    sh = sh or {}
    name = _char_look(slug)[0]
    src = voice_label(pj, sh)
    gesture = (sh.get("gesture") or "").strip().rstrip(".")
    reaction = (sh.get("reaction") or "").strip().rstrip(".")
    act = " ".join(x for x in (
        ("While listening, %s %s." % (name, gesture)) if gesture else "",
        ("Then %s %s." % (name, reaction)) if reaction and reaction != gesture else "")
        if x) or ("%s takes it in: a slow blink, a small, sincere reaction." % name)
    keep = []
    feat, noun = _gag(slug)
    if noun:
        keep.append(" %s's %s stays exactly as large and as shaped as in the first frame."
                    % (name, noun))
    return ("%s, alone in the frame, LISTENS to %s, which speaks from off-screen; %s's mouth "
            "stays CLOSED - %s says nothing. %s %s keeps the same face, hair and clothes as "
            "in the first frame.%s" % (name, src, name, name, act, name, "".join(keep)))


# ---------------------------------------------------------------------------
# H3 PROMPT FORMAT - the text a shot is actually SENT with (MiniMax's own guides).
#
# A shot's `prompt` is the ACTION Ryan reads and edits (set_action + a camera chip). What H3 is
# sent is that action wrapped in the documented format for the node the shot runs on:
#   * i2v (set) / cont -> I2VA (MiniMax base guide 2.1): the fixed first-frame line, a blank line,
#     then integrated_multimodal_description / overall_soundscape / non_diegetic_music.
#   * t2v              -> T2VA: the same three fields with no instruction line.
#   * r2v              -> Ref2VA (MiniMax ref guide): six sections, and EVERY reference still gets
#     a job inside <Subject 1>'s line. The bare '<Picture 1> is the character.' tag this used to
#     prepend is not in the spec and was measured inert (21.4% vs 19.0%, h3-prompt-format-ref2va).
# Before this a Quick set shot went out as ~60 words of bare prose: no style, no sound, no
# don'ts, the camera as a free phrase. A prompt ALREADY in a documented format (hand-written in
# Studio, or the Director's) is sent unchanged, apart from naming extra reference stills.
# ---------------------------------------------------------------------------
I2VA_LINE = ("For the target video, at 0.00 seconds into the target video, <Picture 1> "
             "(from [Shot 1]) is fully referenced.")
# No camera chip: the spec wants a move written as type + amplitude + speed, and a fully static
# frame is the default the model drifts to (it suppresses the acting too). This holds steady.
H3_DEFAULT_CAMERA = ("The camera holds a static shot apart from a very slight push in with small "
                     "amplitude at slow speed.")
_H3_FORMAT_RE = re.compile(
    r"(?<![A-Za-z_])(integrated_multimodal_description|subject_definitions)\s*:")
_OLD_TAG_RE = re.compile(r"<Picture\s+\d+>\s*is the character\.?\s*", re.I)
_CAMERA_SAID_RE = re.compile(
    r"\b(static shot|tracking shot|arc shot|handheld|whip pan|dolly zoom)\b|\bcamera\b[^.]*\b("
    r"push|pull|pan|truck|tilt|pedestal|arc|track|zoom|orbit|rise|hold|crane|sway|follow)", re.I)


def _h3_negatives():
    try:
        import director as _d
        return _d.HOUSE_NEGATIVES
    except Exception:
        return ("No on-screen text, lettering, logos, watermarks, subtitles or UI overlays; no "
                "additional people or duplicated subjects; no face blending between subjects; "
                "no wardrobe or identity change within the shot.")


def _sentence(t):
    t = (t or "").strip()
    return t if not t or t.endswith((".", "!", "?")) else t + "."


def _h3_dialogue(pj, sh, who=None):
    """The spoken line in the spec's form: speaker + (S1) outside, the words inside <d>."""
    line = re.sub(r"\s+", " ", (sh.get("line") or "")).strip().replace("</d>", "")
    if not line or sh.get("is_reaction") or sh.get("voice_only"):
        return ""                          # (a voice-only line: nobody on screen says it)
    who = who or _char_look(_shot_char(pj, sh)[0])[0]
    return "%s (S1) says: <d>[English] %s</d>" % (who, _sentence(line))


def _h3_sound(pj, sh):
    """(overall_soundscape, non_diegetic_music). The score is laid in at the cut, so the clip
    carries none. Left unwritten, H3 invents the sound - and a stray voice is the loudest tell."""
    # the whole place: cutting at the first comma left "a small," of "a small, slightly shabby..."
    where = set_where(pj).strip().rstrip(".")
    if pj.get("music_video"):
        snd = ("The sung vocal from the guide track carries the shot over a faint room tone; "
               "there are no other voices.")
    elif _h3_dialogue(pj, sh):
        snd = ("Quiet natural ambience of %s, soft clothing movement and small breaths. The "
               "spoken line is the only voice; no other voices." % where)
    else:
        snd = ("Quiet natural ambience of %s, soft clothing movement and small breaths. No one "
               "speaks." % where)
    return snd, "N/A"


def _ref_jobs(refs):
    """'<Picture 1>' - or, for a character pack, the job of every still in it."""
    n = len([r for r in (refs or []) if r])
    if n <= 1:
        return "<Picture 1>"
    rest = ", ".join("<Picture %d>" % i for i in range(2, n + 1))
    return ("<Picture 1> (the main still), with %s showing the same person from other angles"
            % rest)


def _ref2va_name_refs(text, refs):
    """A Ref2VA prompt that names only <Picture 1> while the shot sends the whole character pack:
    give the other stills their job inside the first subject line (ref guide 2.1 - one subject
    may come from several assets). The app's own help: untagged extra stills blend faces."""
    if len([r for r in (refs or []) if r]) <= 1 or "<Picture 2>" in text:
        return text
    m = re.search(r"subject_definitions\s*:(.*?)(?=(?<![A-Za-z_])summary\s*:|$)", text, re.S)
    if not m or "<Picture 1>" not in m.group(1):
        return text
    sec = m.group(1).replace("<Picture 1>", _ref_jobs(refs), 1)
    return text[:m.start(1)] + sec + text[m.end(1):]


def h3_prompt(pj, sh, video_mode, refs=()):
    """The text H3 is sent for this shot, in the documented format for `video_mode`
    ('i2v' = a set shot, 'cont', 't2v', 'r2v'). An already-formatted prompt passes through."""
    base = _OLD_TAG_RE.sub("", (sh.get("prompt") or "")).strip()
    if not base:
        return base
    if _H3_FORMAT_RE.search(base):
        return _ref2va_name_refs(base, refs) if video_mode == "r2v" else base
    look = _sentence((pj.get("style") or "").strip())
    camera = "" if (sh.get("camera_words") or _CAMERA_SAID_RE.search(base)) else H3_DEFAULT_CAMERA
    neg = _h3_negatives()
    snd, music = _h3_sound(pj, sh)
    if video_mode == "r2v":
        slug = _shot_char(pj, sh)[0]
        name, desc = _char_desc(slug)
        dd = " ".join(x for x in (
            look or "In the exact visual style of the reference stills.",
            "[Shot 1] " + _sentence(base), _h3_dialogue(pj, sh, "<Subject 1>"), camera, neg) if x)
        return "\n".join([
            "subject_definitions:",
            "<Subject 1> is %s, whose appearance comes from %s."
            % ("%s, %s" % (name, desc) if desc else name, _ref_jobs(refs)),
            "summary:",
            "[reference generation] A %g-second single shot of <Subject 1>."
            % float(sh.get("seconds") or 3),
            "retention_analysis:",
            "<Subject 1> (appears in [Shot 1]): fully_preserved - face, hair, body and clothes "
            "are retained for the whole shot.",
            "detailed_description:", dd,
            "overall_soundscape:", snd,
            "non_diegetic_music:", music]) + "\n"
    if video_mode == "t2v":
        head, anchor = "", ""
        look = look or "Live-action, cinematic."
    else:
        head = I2VA_LINE + "\n\n"
        look = look or "In the exact visual style, light and colour of <Picture 1>."
        anchor = ("The shot %s, keeping the place, the light, the framing and every person's "
                  "face, hair, body and clothes exactly as they are in it."
                  % ("continues directly from <Picture 1>, the last frame of the previous shot"
                     if video_mode == "cont" else "begins exactly on <Picture 1>"))
    body = " ".join(x for x in ("[Shot 1] " + look, anchor, _sentence(base),
                               _h3_dialogue(pj, sh), camera, neg) if x)
    return ("%sintegrated_multimodal_description: %s\n\noverall_soundscape: %s\n\n"
            "non_diegetic_music: %s" % (head, body, snd, music))


# ---------------------------------------------------------------------------
# Stage: render
# ---------------------------------------------------------------------------
def _clip_for_pid(pid):
    d = os.path.join(fl.RUNS, pid)
    if not os.path.isdir(d):
        return None
    files = sorted(f for f in os.listdir(d)
                   if f.lower().endswith((".mp4", ".webm", ".mkv")))
    return os.path.join(d, files[0]) if files else None


def _shot(pj, i):
    for sh in pj["shots"]:
        if sh.get("n") == int(i):
            return sh
    raise ValueError("no shot %r in project %s" % (i, pj["slug"]))


def _character_refs(slug):
    """Reference stills for an r2v shot. charpack (another agent's module) supplies a pack
    of angles when importable; otherwise the character's single still."""
    try:
        import charpack                                 # noqa: F401
        refs = charpack.pack_refs(slug)
        if refs:
            return [r for r in refs if r and os.path.exists(r)]
    except Exception:
        pass
    st = c.character_still(slug)
    return [st] if st else []


def _chosen_clip_path(pj, sh):
    pid = sh.get("chosen")
    return _clip_for_pid(pid) if pid else None


def _prev_chosen(pj, n):
    """The nearest earlier shot that has a chosen clip."""
    best = None
    for sh in pj["shots"]:
        if sh.get("n", 0) < n and sh.get("chosen"):
            if best is None or sh["n"] > best["n"]:
                best = sh
    return best


def _render_refs(pj, sh):
    """The ordered refs a shot submits, extracting the previous clip's last frame for 'cont'."""
    vm = sh.get("video_mode") or "r2v"
    if vm == "t2v":
        return []
    if vm == "cont":
        prev = _prev_chosen(pj, sh.get("n", 0))
        if not prev:
            raise ValueError("shot %s is 'cont' but no earlier shot has a chosen clip"
                             % sh.get("n"))
        src = _chosen_clip_path(pj, prev)
        if not src:
            raise ValueError("shot %s: previous chosen clip file is missing (%s)"
                             % (sh.get("n"), prev.get("chosen")))
        out = os.path.join(project_dir(pj["slug"]), "frames",
                           "shot%02d_first.png" % sh.get("n", 0))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        fr = fl.long_last_frame(src, out)
        if not fr:
            raise ValueError("shot %s: could not extract the last frame of %s"
                             % (sh.get("n"), src))
        return [fr]
    if vm == "set":
        return [set_plate(pj, sh)]
    slug, _ = _shot_char(pj, sh)
    return _character_refs(slug)


def _meta_ok(meta):
    if not isinstance(meta, dict):
        return False
    if meta.get("error"):
        return False
    return any((f.get("file") or "").lower().endswith((".mp4", ".webm", ".mkv"))
               for f in (meta.get("images") or []))


def render_shot(pj, i, seed=-1):
    """Submit one shot and wait for it. Records the pid as a take and chooses it when
    nothing is chosen yet. Writes lineage back onto the Take's index entry."""
    sh = _shot(pj, i)
    # RESUME: the app closed (or crashed) while this shot rendered on the rig - collect THAT
    # take if it is still running or finished, instead of paying for a second render
    if sh.get("status") == "running" and sh.get("takes") and sh.get("chosen") is None:
        pid = sh["takes"][-1]
        try:
            st_ = (c.run_state(pid) or {}).get("state")
        except Exception:
            st_ = None
        if st_ in ("running", "queued", "done"):
            meta = c.wait_and_collect(pid)
            if _meta_ok(meta):
                sh["chosen"], sh["status"] = pid, "done"
                _log(pj, "shot %s -> %s (collected after a restart)" % (sh["n"], pid))
                save_project(pj)
                return {"pid": pid, "seed": None, "status": "done", "meta": meta}
    p = shot_params(pj, sh, seed=seed)
    if int(p.get("gpus", 1)) >= 2:
        # A 2-card render needs BOTH cards; the Director lives on card 2. Release it first
        # and record the answer — "GPU1 released" is a fact in the log, never a hope.
        if not release_gpu1_for_render():
            _log(pj, "shot %s: GPU1 NOT confirmed released before a 2-card render" % sh["n"])
    refs = _render_refs(pj, sh)

    # sent in the documented H3 format for the node it runs on (see h3_prompt); the old
    # bare "<Picture 1> is the character." prepend is gone - not in the spec, measured inert
    p["prompt"] = h3_prompt(pj, sh, p["video_mode"], refs)

    if sh.get("intent") == "lipsync" and sh.get("audio_wav"):
        if not os.path.exists(sh["audio_wav"]):
            raise ValueError("shot %s: guide audio missing: %s" % (sh["n"], sh["audio_wav"]))
        p["guide_audio_path"] = sh["audio_wav"]      # submit() uploads it

    # through the QUEUE (2026-10-22): a shot waits for a card like every other render, and its
    # entry HOLDS that card for the whole render (the queue row is the progress).
    try:
        _est = fl.estimate_seconds("video", p, refs)
    except Exception:
        _est = 0
    meta = _dsp.render("Shot %s — %s" % (sh["n"], pj.get("slug") or ""), "video", dict(p), refs,
                       est_s=_est) or {}
    pid = meta.get("id")
    got_seed = meta.get("seed")
    sh.setdefault("takes", []).append(pid)
    sh["status"] = "running"
    save_project(pj)

    meta = c.wait_and_collect(pid)
    ok = _meta_ok(meta)
    if ok and sh.get("chosen") is None:
        sh["chosen"] = pid
    sh["status"] = "done" if ok else "error"
    try:
        fl._index_update(pid, character=(pj.get("characters") or [None])[0],
                         project=pj["slug"], shot=sh["n"])
    except Exception as exc:
        _log(pj, "lineage index update failed for %s: %r" % (pid, exc))
    _log(pj, "shot %s -> %s (%s, seed %s)" % (sh["n"], pid, sh["status"], got_seed))
    save_project(pj)
    return {"pid": pid, "seed": got_seed, "status": sh["status"], "meta": meta}


def both_cards():
    """The main window's card mode is BOTH (cards.route) — two shots can run at once."""
    try:
        import cards as _c
        return _c.enabled() and _c.route() == "both"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# TAKE CHECK: flag shots that look wrong, for Ryan to reroll (never rerolled on its own —
# Ryan: "highlight bad takes for me to review and approve a reroll")
# ---------------------------------------------------------------------------
QC_SHORT = 0.8          # a clip shorter than this share of what was asked came back cut
QC_FROZEN = 0.6         # a picture frozen for more than this share of the clip
QC_BLACK = 0.5          # seconds of black frames


def _qc_local(clip, want_s):
    """Cheap checks on the Mac (ffmpeg): came back short, frozen, black. -> [reason]."""
    out = []
    dur = _duration(clip) or 0.0
    if want_s and dur < QC_SHORT * want_s:
        out.append("it stops early (%.1f of %.1f s)" % (dur, want_s))
    try:
        r = subprocess.run([FFMPEG, "-v", "info", "-nostats", "-i", clip, "-an", "-vf",
                            "freezedetect=n=0.002:d=1.0,blackdetect=d=%.2f:pix_th=0.08"
                            % QC_BLACK, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=180)
        log = r.stderr or ""
    except Exception:
        return out
    frozen = sum(float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", log))
    if "freeze_start" in log and "freeze_end" not in log.split("freeze_start")[-1]:
        st_ = re.findall(r"freeze_start:\s*([\d.]+)", log)
        frozen += max(0.0, dur - float(st_[-1])) if st_ else 0.0
    if dur and frozen > QC_FROZEN * dur:
        out.append("the picture hardly moves (frozen %.1f of %.1f s)" % (frozen, dur))
    black = sum(float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", log))
    if black >= QC_BLACK:
        out.append("it goes black for %.1f s" % black)
    return out


def _qc_expect(pj, sh):
    """What the frame SHOULD show, in words, for the vision read."""
    name = _char_look(sh.get("speaker") or "")[0] if sh.get("speaker") else "the character"
    cast = pj.get("characters") or []
    if sh.get("is_reaction") or sh.get("voice_only"):
        return ("%s alone in the frame, listening with the mouth closed; exactly one person "
                "visible, no second copy of anyone." % name)
    if sh.get("video_mode") == "set" and len(cast) > 1 and not is_wide(sh):
        return ("%s alone in a close-up, talking; exactly one person visible - nobody else, "
                "no shoulder or second copy of anyone." % name)
    if len(cast) > 1:
        names = " and ".join(_char_look(x)[0] for x in cast)
        return ("exactly %d people: %s, each looking like their own reference (no swapped "
                "features or clothes); %s is talking." % (len(cast), names, name))
    return "%s alone, talking to camera; exactly one person visible." % name


def qc_shot(pj, sh, director=None, frames_dir=None):
    """Check one shot's chosen take -> {"pid", "flags": [reason], "frame"}. Local checks
    always; a vision read of one frame against the speaker's still when a writer is given
    (a SORTING AID — Ryan decides)."""
    clip = _chosen_clip_path(pj, sh)
    res = {"pid": sh.get("chosen"), "flags": [], "frame": None}
    if not clip or not os.path.exists(clip):
        res["flags"].append("the shot is missing")
        return res
    want = fl.h3_frames(sh.get("seconds", 3)) / 24.0 if sh.get("engine", "h3") == "h3" \
        else None
    res["flags"] += _qc_local(clip, want)
    frames_dir = frames_dir or os.path.join(project_dir(pj["slug"]), "qc")
    os.makedirs(frames_dir, exist_ok=True)
    frame = os.path.join(frames_dir, "n%02d.png" % sh["n"])
    try:
        dur = _duration(clip) or 1.0
        _run([FFMPEG, "-v", "error", "-y", "-ss", "%.2f" % (dur * 0.45), "-i", clip,
              "-frames:v", "1", "-vf", "scale=-2:480", frame], timeout=60)
        res["frame"] = frame if os.path.exists(frame) else None
    except Exception:
        res["frame"] = None
    still = c.character_still(sh.get("speaker")) if sh.get("speaker") else None
    if director is not None and hasattr(director, "qc_frame") and res["frame"] and still:
        try:
            v = director.qc_frame(res["frame"], still, _qc_expect(pj, sh)) or {}
            if v.get("verdict") in ("drift", "broken"):
                why = "; ".join(str(x) for x in (v.get("reasons") or [])[:3])
                res["flags"].append(("something looks off: " if v["verdict"] == "drift"
                                     else "it looks wrong: ") + (why or v["verdict"]))
            res["vision"] = v.get("verdict")
        except Exception as exc:
            res["vision_error"] = "%s" % exc
    return res


def qc_shots(pj, director=None, progress=None):
    """Check every line/reaction shot whose chosen take hasn't been checked yet. Stores
    sh["qc"]; returns the shot numbers flagged. A vision failure turns the vision read off
    for the rest (the local checks still run)."""
    flagged = []
    for sh in pj.get("shots") or []:
        if not sh.get("chosen"):
            continue
        q = sh.get("qc") or {}
        if q.get("pid") != sh["chosen"]:
            if progress:
                progress("checking shot %d" % sh["n"])
            q = qc_shot(pj, sh, director)
            if q.get("vision_error"):
                _log(pj, "take check: the vision read is unavailable (%s)" % q["vision_error"])
                director = None
            sh["qc"] = q
        if q.get("flags") and not q.get("ok_by_you"):
            flagged.append(sh["n"])
    save_project(pj)
    return flagged


def render_many(pj, ns, lanes=None, stop=None, progress=None):
    """Render shots `ns`, `lanes` at a time (default: 2 when BOTH cards are on). A shot that
    starts from the LAST shot ('cont') waits until that shot is made. Shared by Quick (and
    the same rule Episode's Make-missing runs). Returns the errors (empty = all made)."""
    import time as _t
    lanes = lanes or (2 if both_cards() else 1)
    pending, running, errors, made = list(ns), set(), [], [0]
    lk = threading.Lock()

    def ready(n):
        sh = _shot(pj, n)
        return sh.get("video_mode") != "cont" or not ({n - 1} & (set(pending) | running))

    def lane():
        while True:
            with lk:
                if (stop and stop()) or errors or not pending:
                    return
                pick = next((n for n in pending if ready(n)), None)
                if pick is not None:
                    pending.remove(pick)
                    running.add(pick)
                    if progress:
                        progress("shooting %s (%d of %d made%s)" % (
                            " and ".join(str(x) for x in sorted(running)), made[0], len(ns),
                            ", both cards" if lanes > 1 else ""))
            if pick is None:
                _t.sleep(0.5)
                continue
            try:
                render_shot(pj, pick)
            except Exception as exc:
                with lk:
                    errors.append(exc)
            finally:
                with lk:
                    running.discard(pick)
                    made[0] += 1
    ts = [threading.Thread(target=lane, daemon=True) for _ in range(max(1, lanes))]
    for t_ in ts:
        t_.start()
        _t.sleep(0.2)                         # card 1 gets the first shot
    for t_ in ts:
        t_.join()
    return errors


def render_all(pj, budget_s=None):
    """Render every un-chosen shot in order. `budget_s` stops it before the shot that would
    push the cumulative ESTIMATE past the budget (never mid-shot)."""
    est = estimate(pj)
    est_by_n = {e["n"]: (e.get("estimate_s") or 0) for e in est["per_shot"]}
    ran, cum = [], 0.0
    for sh in pj["shots"]:
        if sh.get("chosen") and sh.get("status") == "done":
            continue
        cost = est_by_n.get(sh["n"], 0)
        if budget_s is not None and ran and cum + cost > budget_s:
            _log(pj, "budget stop: shot %s would take cumulative estimate %.0fs past %.0fs"
                 % (sh["n"], cum + cost, budget_s))
            break
        cum += cost
        render_shot(pj, sh["n"])
        ran.append(sh["n"])
    return ran


# ---------------------------------------------------------------------------
# Captions: ASS built from aligned words + shot start offsets
# ---------------------------------------------------------------------------
def _ass_time(t):
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return "%d:%02d:%05.2f" % (h, m, s)


# a caption never ENDS on one of these ("AND THAT CHIN IS" / "VIOLATIONS WITHIN THE" in the
# first real run): the word moves to the next chunk, with the words it belongs to
_WEAK = set("""a an the and or but nor so yet of to in on at by for from with within without
into onto over under about as than that this these those is are was were be been am it its
my your his her our their i you he she we they not no very just if then when while because
whose which who what where does do did has have had will would can could should
must may might shall""".split())
_CAP_CHARS = 22                      # ~ what reads in one glance at caption size


def _chunk_cost(ws, hi, whole=False, cap=None):
    """How badly a run of words reads as ONE caption (lower is better)."""
    toks = [(w.get("word") or "").strip() for w in ws]
    text = " ".join(toks)
    n, ch = len(toks), len(text)
    cap = cap or _CAP_CHARS
    cost = 4.0                                            # fewer, fuller chunks
    if ch > cap + 6 or n > hi + 1:
        cost += 100
    elif ch > cap:
        cost += (ch - cap) * 1.5
    if n > hi:
        cost += 3 if ch <= cap else 8
    last = toks[-1]
    ends_punct = bool(re.search(r"[,.!?;:\u2014]$", last))
    if n == 1 and not whole:
        cost += 7                                         # a lonely word
    if not ends_punct and last.lower().strip("\"'") in _WEAK:
        cost += 12                                        # "...WITHIN THE"
    for t in toks[:-1]:
        if re.search(r"[.!?]$", t):
            cost += 100                                   # two sentences in one caption
        elif re.search(r"[,;:\u2014]$", t):
            cost += 6
    if ends_punct:
        cost -= 2
    return cost


# an ADJECTIVE belongs with its noun: "YOUR ENORMOUS | WET EARS", "INSIDE THE BROKEN |
# VENDING MACHINE" read wrong (round-2 editor: "caption ADJ+NOUN split"). No tagger here, so
# common adjectives + adjective endings; a break right after one costs extra.
_ADJ = set("""big small little tiny huge giant enormous massive old new young wet dry cold hot
warm cool long short tall wide deep high low red blue green black white yellow pink purple
brown grey gray golden good bad great best worst nice fine sweet sour bitter fresh rotten
first last next other same whole real fake free full empty heavy light dark bright loud
quiet soft hard strange weird odd crazy wild dead angry happy sad lonely fancy dirty clean
rich poor ugly pretty lovely bald hairy fat thin smelly sticky soggy slimy shiny broken
own personal federal local public private secret special perfect terrible horrible awful
entire certain""".split())
_ADJ_END = ("ous", "ful", "ive", "able", "ible", "less", "ish", "ical")


def _adj_like(tok):
    t = re.sub(r"[^a-z\-]", "", (tok or "").lower())
    return bool(t) and (t in _ADJ or (len(t) > 4 and t.endswith(_ADJ_END)) or
                        ("-" in t and not t.endswith("-")))


def _split_cost(left, right):
    """Extra cost of a caption break between `left` and `right`: an adjective torn from
    the word it describes."""
    if re.search(r"[,.!?;:\u2014]$", left or ""):
        return 0.0
    r = re.sub(r"[^a-z]", "", (right or "").lower())
    if not r or r in _WEAK or r.endswith("ly"):
        return 0.0
    return 9.0 if _adj_like(left) else 0.0


def _group_words(words, lo=2, hi=4, cap=None):
    """Chunk words into caption events that read as PHRASES — the best of every way to cut
    the line (dynamic programming over the break points): ~22 characters / up to `hi`
    words each, a cut after punctuation, never a chunk ending on a weak word
    (the/a/is/that/within...) and no lonely one-word chunks. The first real run cut
    "YOUR EAR IS | DRIPPING CONDENSATION." and "VIOLATIONS WITHIN THE" by plain counting."""
    words = [w for w in (words or []) if (w.get("word") or "").strip()]
    n = len(words)
    if not n:
        return []
    best = [0.0] + [float("inf")] * n
    back = [0] * (n + 1)
    for j in range(1, n + 1):
        for i in range(max(0, j - hi - 2), j):
            c = best[i] + _chunk_cost(words[i:j], hi, whole=(i == 0 and j == n), cap=cap)
            if j < n:                                     # don't tear "WET | EARS" apart
                c += _split_cost(words[j - 1].get("word"), words[j].get("word"))
            if c < best[j]:
                best[j], back[j] = c, i
    out, j = [], n
    while j > 0:
        out.append(words[back[j]:j])
        j = back[j]
    return out[::-1]


def _syll(tok):
    """Rough syllable count — speech time follows syllables, not letters."""
    t = re.sub(r"[^a-z]", "", tok.lower())
    n = len(re.findall(r"[aeiouy]+", t))
    if t.endswith("e") and n > 1 and not t.endswith(("le", "ee")):
        n -= 1
    return max(1, n) + (len(re.sub(r"[^0-9]", "", tok)) * 1)


def _speech_span(wav, dur):
    """(start, end) of the speech in a voice wav — ffmpeg silencedetect, so captions don't
    show before the voice starts or hang after it stops. Falls back to (0.05, dur)."""
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", wav, "-af",
                            "silencedetect=noise=-35dB:d=0.12", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=60)
        st = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", p.stderr)]
        en = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", p.stderr)]
        a, b = 0.05, dur
        if st and en and st[0] < 0.05 and en[0] < dur * 0.6:
            a = en[0]                                  # leading silence
        if st and st[-1] > a + 0.3 and (len(en) < len(st) or en[-1] >= dur - 0.05):
            b = st[-1]                                 # trailing silence
        return a, max(a + 0.4, min(b, dur))
    except Exception:
        return 0.05, dur


def _even_words(line, dur, wav=None):
    """Word timings for a shot that has a line but no aligned words: the words share the
    SPEECH span (silence trimmed when the wav is given) in proportion to their syllables,
    with a beat after commas and full stops. Returns [] for an empty line."""
    toks = [t for t in (line or "").split() if t.strip()]
    if not toks:
        return []
    dur = max(0.4, float(dur or 0.0))
    a, b = _speech_span(wav, dur) if wav and os.path.exists(wav) else (0.05, dur * 0.95)
    weights, pauses = [], []
    for tk_ in toks:
        weights.append(_syll(tk_) + 0.6)
        pauses.append(1.4 if re.search(r"[.!?]$", tk_) else
                      0.8 if re.search(r"[,;:\u2014]$", tk_) else 0.0)
    total = float(sum(weights) + sum(pauses[:-1]))
    out, t = [], a
    for tk_, w, pz in zip(toks, weights, pauses):
        d = (b - a) * w / total
        out.append({"word": tk_, "start": round(t, 3), "end": round(t + d, 3)})
        t += d + (b - a) * pz / total
    return out


def _with_line_words(got, line):
    """The aligner's words come back bare ("rentfree", no commas or full stops); put the
    SCRIPT's own spelling and punctuation back on them, keeping the aligner's times. Matches
    by letters only, in order; anything unmatched keeps the aligner's word."""
    import difflib
    toks = [t for t in (line or "").split() if t.strip()]
    norm = lambda w: re.sub(r"[^a-z0-9]", "", (w or "").lower())
    a = [norm(w.get("word")) for w in got]
    b = [norm(t) for t in toks]
    out = [dict(w) for w in got]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                out[i1 + k]["word"] = toks[j1 + k]
    return out


def _shot_words(sh):
    """The aligned words when the shot has them, else the line spread over its voice length
    (`voice_s`, set by assemble from the wav) or, failing that, the shot length."""
    if sh.get("words"):
        return sh["words"]
    return _even_words(sh.get("line"), sh.get("voice_s") or sh.get("seconds") or 3,
                       wav=sh.get("audio_wav"))


# the caption font ships WITH the app (review panel: "a heavy OFL font" - Arial Black only
# exists on Macs, and the burn can run on the rig): Inter Black, SIL OFL 1.1, assets/fonts/
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
CAPTION_FONT = "Inter Black"


def caption_fonts():
    """The font files the caption burn hands libass (fontsdir); [] if none shipped."""
    try:
        return [os.path.join(FONTS_DIR, f) for f in sorted(os.listdir(FONTS_DIR))
                if f.lower().endswith((".otf", ".ttf"))]
    except OSError:
        return []
CAP_LEAD = 2 / 24.0               # a caption appears 2 frames before its word
CAP_HOLD_GAP = 0.4                # a gap shorter than this: hold the caption across it
CAP_MIN = 10 / 24.0               # never on screen for less than 10 frames


def caption_style(width, height):
    """(font_size, margin_v) for the frame.
    Vertical: 96 px with the block around y~1200 — the bottom ~400 px are under TikTok /
    Reels / Shorts' own text and buttons (the old 240 px margin sat right in them).
    Widescreen: 72 px, 96 px up (clear of YouTube's hover controls)."""
    if width >= height:
        return int(round(72 * height / 1080.0)), int(round(96 * height / 1080.0))
    return int(round(96 * width / 1080.0)), int(round(700 * height / 1920.0))


def caption_chars(width, height):
    """(characters per caption, per line): a phone shows ~16 heavy capitals across, two
    lines at most; widescreen one line of ~30."""
    if width == height:                      # square (Instagram feed): two short lines
        return (24, 18)
    return (28, 16) if width < height else (30, 30)


def _two_lines(words, per_line):
    """Index to break a caption into two lines (None = one line): the space nearest the
    middle, only when the text is wider than one line."""
    text = " ".join(words)
    if len(text) <= per_line or len(words) < 2:
        return None
    best, bi, acc = None, None, 0
    for i in range(1, len(words)):
        acc = len(" ".join(words[:i]))
        d = abs(acc - len(text) / 2.0) + (6 if _split_cost(words[i - 1], words[i]) else 0)
        if best is None or d < best:
            best, bi = d, i
    return bi


# how the burned-in captions look (Episode chips, Quick's CAPTIONS row)
CAPTION_LOOKS = (("clean", "Clean"), ("pop", "Pop \u00b7 word lights up"),
                 ("big", "Big \u00b7 one word at a time"),
                 ("subtitle", "Subtitle \u00b7 small and gold, like a film"))
POP_COLOUR = "&H0000E5FF&"          # ASS is BGR: a warm yellow
SUB_COLOUR = "&H0028C8E8&"          # ASS is BGR: #E8C828, the warm gold of film subtitles
SUB_SCALE = 0.5                     # half the size of the other looks
SUB_FROM_BOTTOM = (0.24, 0.10)      # (vertical, widescreen): ~76 % down a phone frame


def caption_look(pj):
    """The project's caption look; anything unknown reads as "clean" (the old style)."""
    v = (pj or {}).get("caption_look") or "clean"
    return v if v in dict(CAPTION_LOOKS) else "clean"


def _ass_clean(t):
    return (t or "").replace("\n", " ").replace("{", "(").replace("}", ")").strip().upper()


def _ass_text(t):
    """As _ass_clean, but the case is KEPT (the subtitle look reads in sentence case)."""
    return (t or "").replace("\n", " ").replace("{", "(").replace("}", ")").strip()


HOOK_SECONDS = 2.0


def build_ass(shots, width=OUT_W, height=OUT_H, font=None, font_size=76, margin_v=240,
              look="clean", hook=None, captions=True, stamp=None):
    """A word-by-word caption track: one event per short phrase, timed from the shot's
    aligned words PLUS the shot's `start` offset in the finished cut.

    look "clean": the phrase in white.  "pop": the word being said turns yellow and grows
    TALLER only (growing wider shoved the whole centred line sideways on every word).
    "big": one word at a time, nearly twice the size, popping in.
    Captions lead their word by 2 frames, hold across short gaps (no blinking), never show
    for under 10 frames, and stay inside their shot (`cut_in`/`cut_out` when given).
    Two lines on a phone when a phrase is wider than ~16 characters.

    `shots` must carry `start` (seconds offset) and `words` ([{word,start,end}])."""
    look = look if look in dict(CAPTION_LOOKS) else "clean"
    font = font or CAPTION_FONT
    cap, per_line = caption_chars(width, height)
    sub = look == "subtitle"
    if sub:                           # half-size letters: twice the words fit a line
        cap, per_line = (44, 30) if width < height else (60, 60)
    sub_size = int(round(font_size * SUB_SCALE))
    sub_mv = int(round(height * SUB_FROM_BOTTOM[0 if width < height else 1]))
    outline = max(4, int(round(font_size * 0.075)))
    big_size = int(round(font_size * (1.65 if width < height else 1.9)))
    big_mv = margin_v if width < height else int(margin_v * 1.5)
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: %d" % width,
        "PlayResY: %d" % height,
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # heavy white, thick black outline, bottom-centre (2), clear of the platform UI;
        # a little more room on the right (the like/share rail on phones)
        "Style: Cap,%s,%d,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,"
        "1,0,1,%d,0,2,%d,%d,%d,1" % (font, font_size, outline,
                                     100 if width < height else 80,
                                     160 if width < height else 80, margin_v),
        "Style: Big,%s,%d,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,"
        "1,0,1,%d,0,2,%d,%d,%d,1" % (font, big_size, outline + 3,
                                     100 if width < height else 80,
                                     160 if width < height else 80, big_mv),
        # the SUBTITLE look: small warm gold, thin outline + soft shadow, sentence case
        "Style: Sub,%s,%d,%s,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,"
        "0,0,1,%d,1,2,%d,%d,%d,1" % (font, sub_size, SUB_COLOUR,
                                     max(2, int(round(sub_size * 0.06))),
                                     90 if width < height else 80,
                                     90 if width < height else 80, sub_mv),
        # the HOOK: black on a white box, top-centre (8), below the phone's top bar
        "Style: Hook,%s,%d,&H00141414,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,"
        "0,0,3,14,0,8,%d,%d,%d,1" % (font, int(round(font_size * (0.82 if width < height
                                                                    else 0.75))),
                                     90 if width < height else 120,
                                     90 if width < height else 120,
                                     int(round(height * (0.12 if width < height else 0.06)))),
        # the STAMP (doorbell / CCTV formats): a small camera label top-left (7), all along
        "Style: Stamp,%s,%d,&H00F0F0F0,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,"
        "2,0,1,2,0,7,%d,%d,%d,1" % (font, int(round(font_size * 0.42)), int(width * 0.05),
                                    int(width * 0.05), int(round(height * 0.05))),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ENTER = "{\\fscx88\\fscy88\\t(0,70,\\fscx104\\fscy104)\\t(70,130,\\fscx100\\fscy100)}"

    def tidy(word):
        if sub:                       # a film subtitle keeps its case and punctuation
            return _ass_text(word)
        w = _ass_clean(word)
        return w.rstrip(",.;:") or w if look != "clean" else w

    # 1) every phrase in the cut, with its shot's window
    groups = []
    for sh in (shots if captions else []):
        start = float(sh.get("start") or 0.0)
        lo = sh.get("cut_in")
        hi = sh.get("cut_out")
        for group in _group_words(_shot_words(sh), cap=cap):
            ws = [(start + float(w.get("start") or 0.0), start + float(w.get("end") or 0.0),
                   tidy(w.get("word"))) for w in group]
            ws = [w for w in ws if w[2]]
            if ws:
                groups.append({"ws": ws, "lo": lo, "hi": hi,
                               "g0": ws[0][0] - CAP_LEAD, "g1": ws[-1][1]})
    # 2) timing: hold across short gaps, a floor, clamped inside the shot
    for k, g in enumerate(groups):
        nxt = groups[k + 1]["g0"] if k + 1 < len(groups) else None
        if nxt is not None and 0 <= nxt - g["g1"] < CAP_HOLD_GAP:
            g["g1"] = nxt
        if g["g1"] - g["g0"] < CAP_MIN:
            g["g1"] = g["g0"] + CAP_MIN
            if nxt is not None:
                g["g1"] = min(g["g1"], max(nxt, g["g0"] + 0.2))
        if nxt is not None:
            g["g1"] = min(g["g1"], nxt)              # never two captions at once
        if g["lo"] is not None:
            g["g0"] = max(g["g0"], float(g["lo"]))
        if g["hi"] is not None:
            g["g1"] = min(g["g1"], float(g["hi"]))
    events = []

    def ev(t0, t1, style, text):
        if t1 <= t0:
            t1 = t0 + 0.2
        events.append("Dialogue: 0,%s,%s,%s,,0,0,0,,%s"
                      % (_ass_time(max(0.0, t0)), _ass_time(t1), style, text))

    def join(parts, words):
        br = _two_lines(words, per_line)
        return "".join(p + ("" if i == len(parts) - 1 else ("\\N" if br == i + 1 else " "))
                       for i, p in enumerate(parts))

    stp = _ass_clean(stamp or "")
    if stp:
        ev(0.0, 599.0, "Stamp", stp)
    hk = _ass_clean(hook or "")
    if hk:
        words = hk.split()
        ev(0.0, HOOK_SECONDS, "Hook", ENTER + join(words, words))
    for g in groups:
        ws, g0, g1 = g["ws"], g["g0"], g["g1"]
        plain = [w[2] for w in ws]
        if look == "clean":
            ev(g0, g1, "Cap", ENTER + join(plain, plain))
            continue
        if sub:                       # the whole phrase at once, no pop-in
            ev(g0, g1, "Sub", join(plain, plain))
            continue
        for i, (t0, _t1, word) in enumerate(ws):
            # each word holds until the next one starts (no flicker between words)
            t_beg = g0 if i == 0 else max(g0, t0 - CAP_LEAD)
            t_end = (ws[i + 1][0] - CAP_LEAD) if i + 1 < len(ws) else g1
            t_end = min(max(t_end, t_beg + 0.05), g1)
            if look == "big":
                ev(t_beg, t_end, "Big",
                   "{\\fscx70\\fscy70\\t(0,90,\\fscx100\\fscy100)}" + word)
            else:
                parts = [("{\\c%s\\fscy118\\t(0,80,\\fscy108)}%s{\\c&H00FFFFFF&\\fscy100}"
                          % (POP_COLOUR, w[2])) if j == i else w[2]
                         for j, w in enumerate(ws)]
                ev(t_beg, t_end, "Cap", join(parts, plain))
    return "\n".join(head + events) + "\n"


# ---------------------------------------------------------------------------
# finish(): burn captions (+ loudnorm) WHERE THE FFMPEG CAN
# ---------------------------------------------------------------------------
def _ffmpeg_filters(ffmpeg=FFMPEG):
    """The set of filter names an ffmpeg reports, or None when it cannot be run."""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    names = set()
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] and parts[0][0] in "TSCA." and not parts[0].startswith("Filt"):
            names.add(parts[1])
    return names


def ffmpeg_has(filt, ffmpeg=FFMPEG):
    names = _ffmpeg_filters(ffmpeg)
    return bool(names) and filt in names


LOUD = "I=-14:TP=-1.0:LRA=11"


def loudnorm_af(src):
    """A TWO-PASS loudnorm filter for `src` (measure, then linear): one pass pumps on short
    dialogue (review panel, motion designer). Falls back to one pass if measuring fails."""
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", src, "-vn", "-af",
                            "loudnorm=%s:print_format=json" % LOUD, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=600)
        m = json.loads(p.stderr[p.stderr.rindex("{"):p.stderr.rindex("}") + 1])
        return ("loudnorm=%s:measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s:"
                "offset=%s:linear=true" % (LOUD, m["input_i"], m["input_tp"], m["input_lra"],
                                           m["input_thresh"], m["target_offset"]))
    except Exception:
        return "loudnorm=%s" % LOUD


def _finish_local(in_mp4, ass_path, out_mp4, loudnorm=True):
    """Burn captions locally — run from the input's own dir so the ass= filename needs no
    filtergraph escaping (no ':' or quotes)."""
    wd = os.path.dirname(os.path.abspath(in_mp4))
    ass_name = "captions.ass"
    if os.path.abspath(ass_path) != os.path.abspath(os.path.join(wd, ass_name)):
        shutil.copyfile(ass_path, os.path.join(wd, ass_name))
    fonts = caption_fonts()
    vf = "ass=%s" % ass_name
    if fonts:                      # the shipped font, by a relative dir (no escaping)
        os.makedirs(os.path.join(wd, "fonts"), exist_ok=True)
        for f_ in fonts:
            shutil.copyfile(f_, os.path.join(wd, "fonts", os.path.basename(f_)))
        vf += ":fontsdir=fonts"
    args = [FFMPEG, "-v", "error", "-y", "-i", os.path.basename(in_mp4), "-vf", vf]
    if loudnorm:
        args += ["-af", loudnorm_af(in_mp4)]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "20",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", os.path.abspath(out_mp4)]
    _run(args, timeout=1800, cwd=wd)
    return out_mp4


def _finish_remote(in_mp4, ass_path, out_mp4, loudnorm=True):
    """The Mac's ffmpeg has no libass, so the caption burn runs on forge over ssh/scp."""
    remote = "%s/%s" % (FORGE_FINISH_TMP, uuid.uuid4().hex)

    def ssh(cmd):
        return _run(["ssh", FORGE_SSH, cmd], timeout=1800)

    try:
        ssh("mkdir -p %s" % shlex.quote(remote))
        _run(["scp", "-q", in_mp4, "%s:%s/in.mp4" % (FORGE_SSH, remote)])
        _run(["scp", "-q", ass_path, "%s:%s/captions.ass" % (FORGE_SSH, remote)])
        fonts = caption_fonts()
        for f_ in fonts:
            _run(["scp", "-q", f_, "%s:%s/%s" % (FORGE_SSH, remote, os.path.basename(f_))])
        af = (" -af " + shlex.quote(loudnorm_af(in_mp4))) if loudnorm else ""  # = local
        cmd = ("cd %s && %s -v error -y -i in.mp4 -vf ass=captions.ass%s%s "
               "-c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 "
               "-c:a aac -b:a 192k -ar 48000 out.mp4"
               % (shlex.quote(remote), shlex.quote(FORGE_FFMPEG),
                  ":fontsdir=." if fonts else "", af))
        ssh(cmd)
        _run(["scp", "-q", "%s:%s/out.mp4" % (FORGE_SSH, remote), out_mp4])
    finally:
        try:
            ssh("rm -rf %s" % shlex.quote(remote))
        except Exception:
            pass
    return out_mp4


def finish_plain(in_mp4, out_mp4, loudnorm=True):
    """Finish a cut with NO burned captions — local ffmpeg, no libass, no ssh."""
    args = [FFMPEG, "-v", "error", "-y", "-i", in_mp4]
    if loudnorm:
        args += ["-af", loudnorm_af(in_mp4)]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "20",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out_mp4]
    _run(args, timeout=1800)
    return out_mp4


def add_music(video, music, out, volume=MUSIC_VOLUME, stop_at=None):
    """Lay a music bed under the whole cut — looped if short, cut off at the video's end.
    `stop_at`: the bed cuts out (a 60 ms fade) there - the silence before the button word
    is the hit (round-2 review: drop the whoosh, use silence)."""
    # the bed DUCKS under the voices (sidechain keyed on the cut's own audio). Formats are
    # pinned on both inputs: an older ffmpeg (the Mac's VM) couldn't negotiate a mono bed
    # against a stereo key. If ducking still fails, the plain mix — never no music.
    fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    stop = (",afade=t=out:st=%.3f:d=0.06" % max(0.0, float(stop_at) - 0.06)) \
        if stop_at else ""
    tail = ["-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-ar", "44100",
            "-ac", "2", out]
    try:
        _run([FFMPEG, "-v", "error", "-y", "-i", video, "-stream_loop", "-1", "-i", music,
              "-filter_complex",
              "[0:a]%s,asplit=2[v][key];[1:a]%s,volume=%.2f%s[m];"
              "[m][key]sidechaincompress=threshold=0.03:ratio=8:attack=10:release=350[md];"
              "[v][md]amix=inputs=2:duration=first:normalize=0[aout]" % (fmt, fmt, volume,
                                                                         stop)]
             + tail, timeout=900)
    except Exception:
        _run([FFMPEG, "-v", "error", "-y", "-i", video, "-stream_loop", "-1", "-i", music,
              "-filter_complex",
              "[1:a]volume=%.2f%s[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[aout]"
              % (volume, stop)] + tail, timeout=900)
    return out


def end_fade(video, out, seconds):
    """The picture fades to black and the sound fades out over the last `seconds`
    (gilded satire). Anything that goes wrong: the video unchanged."""
    dur = _duration(video)
    s = float(seconds or 0)
    if dur <= 0 or s <= 0:
        return video
    s = min(s, dur * 0.5)
    st = max(0.0, dur - s)
    try:
        _run([FFMPEG, "-v", "error", "-y", "-i", video,
              "-vf", "fade=t=out:st=%.3f:d=%.3f" % (st, s),
              "-af", "afade=t=out:st=%.3f:d=%.3f" % (st, s),
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "18",
              "-c:a", "aac", "-b:a", "192k", out], timeout=900)
        return out
    except Exception:
        return video


def finish(in_mp4, ass_path, out_mp4, loudnorm=True):
    """Burn captions onto the cut. Chooses the host by the 'ass' filter capability."""
    if ffmpeg_has("ass"):
        return _finish_local(in_mp4, ass_path, out_mp4, loudnorm=loudnorm)
    return _finish_remote(in_mp4, ass_path, out_mp4, loudnorm=loudnorm)


# ---------------------------------------------------------------------------
# Stage: assemble
# ---------------------------------------------------------------------------
# THE EDIT (review panel 2026-10-17: motion designer, director, short-form editor) —
# each clip is trimmed to its words plus handles, so half the video is no longer dead air
# (fog-of-war: 13.7 s of speech in 26.5 s), and gets a camera move made in post.
HANDLE_IN = 4 / 24.0            # before the first word
HANDLE_OUT = 6 / 24.0           # after the last word
BUTTON_HOLD = 0.85              # after the LAST line's last word: the reaction beat
MIN_CLIP = 1.0
PUSH = 0.06                     # slow eased push on setups: 100 -> 106 %
TIGHT = 1.35                    # the mid-scene glance at the listener (>= ~30% tighter than
                                # their speaking close-up, the size change that reads as a cut)
PUNCH = 1.15                    # hard punch-in on the button word (1.22 went soft: a Draft
                                # clip is already upscaled ~2x before the punch)


# PACE (Ryan: "silence can be different video to video depending on the prompt") —
# (before the first word, after the last word, after the LAST line) in seconds
PACES = {"snappy": (4 / 24.0, 6 / 24.0, 0.85),
         "natural": (6 / 24.0, 12 / 24.0, 1.1),
         "breathe": (8 / 24.0, 0.9, 1.6),
         "full": None}                                   # keep every clip whole
PACE_LABELS = (("snappy", "Snappy"), ("natural", "Natural"), ("breathe", "Let it breathe"),
               ("full", "Keep every pause"))
# a line the writer marked with a pause keeps one after it
_PAUSE_RE = re.compile(r"\b(beat|pause|silence|stares?|waits?|awkward)\b", re.I)
PAUSE_HOLD = 0.7


def pace_of(pj):
    v = (pj or {}).get("pace") or "snappy"
    return v if v in PACES else "snappy"


def _trim_for(sh, clip_s, last=False, pace="snappy", extra=0.0):
    """(in, out) seconds to keep from a shot's clip: its words plus the PACE's handles (and
    a held pause when the line's direction asks for one) — the whole clip when it has no
    voice or no word times (music videos, b-roll) or the pace is "full"."""
    ws = sh.get("words") or []
    hp = PACES.get(pace, PACES["snappy"])
    if not ws or not sh.get("audio_wav") or not clip_s or hp is None:
        return 0.0, clip_s
    h_in, h_out, hold = hp
    tail = hold if last else h_out
    if _PAUSE_RE.search(sh.get("direction") or ""):
        tail = max(tail, h_out + PAUSE_HOLD)
    tail += max(0.0, float(extra or 0))
    a = max(0.0, float(ws[0].get("start") or 0) - h_in)
    b = min(clip_s, float(ws[-1].get("end") or 0) + tail)
    if b - a < MIN_CLIP:
        b = min(clip_s, a + MIN_CLIP)
        a = max(0.0, b - MIN_CLIP)
    return round(a, 3), round(b, 3)


def camera_moves(pj):
    """{shot n: move} for the edit: no two neighbours alike, every third shot rests, the last
    line (the button) punches in on its last word. Music videos move in H3 already."""
    shots = line_shots(pj)
    out = {sh["n"]: "static" for sh in pj.get("shots") or [] if sh.get("is_reaction")}
    if pj.get("music_video") or pj.get("post_moves") is False or pj.get("post_look") == "cctv":
        out.update({sh["n"]: "static" for sh in shots})
        return out
    for i, sh in enumerate(shots):
        if len(sh.get("line_starts") or []) > 1:
            out[sh["n"]] = "jumps"                 # a long take: a jump zoom per line
        elif i == len(shots) - 1 and len(shots) > 1 and sh.get("words"):
            out[sh["n"]] = "punch"
        else:
            out[sh["n"]] = ("static", "push", "push_r")[i % 3]
    return out


def speaker_x(pj, sh):
    """Where the speaker stands across the frame, 0..1: on a set shot the wide puts the
    cast left to right in order, so a push/punch aims at THEM (the first recut punched into
    the middle and cut Vivian off). 0.5 when unknown."""
    try:
        if sh.get("video_mode") != "set" or not is_wide(sh):
            return 0.5                     # a close-up is centred on its speaker already
        cast = set_cast(pj)
        slug = _shot_char(pj, sh)[0]
        if len(cast) < 2 or slug not in cast:
            return 0.5
        return _side_x(cast.index(slug))
    except Exception:
        return 0.5


def _move_filter(move, w, h, dur, punch_at=None, x=0.5, jumps=()):
    """A zoompan expression for a move on a clip already scaled to w x h, aimed at `x`
    (0..1 across the frame). "jumps": the frame jumps in/out at each time in `jumps` (a
    long take's new lines), and punches in at `punch_at` when given."""
    yc = "(ih-ih/zoom)*0.38"                       # faces sit high: zoom toward them
    if move == "jumps" and jumps:
        step = "1+%.3f*mod(%s,2)" % (JUMP, "+".join("gte(in_time,%.3f)" % max(0.0, j)
                                                    for j in jumps))
        z = ("if(gte(in_time,%.3f),%.3f,%s)" % (punch_at, PUNCH, step)
             if punch_at is not None else step)
        return ",scale=%d:%d,zoompan=z='%s':x='(iw-iw/zoom)*%.3f':y='%s':d=1:s=%dx%d:fps=%d" % (
            2 * w, 2 * h, z, min(1.0, max(0.0, x)), yc, w, h, OUT_FPS)
    if move in ("push", "push_r"):
        z = "1+%.3f*(0.5-0.5*cos(PI*min(in_time/%.3f,1)))" % (PUSH, max(dur, 0.1))
        xc = "(iw-iw/zoom)*%.3f" % min(1.0, max(0.0, x + (-0.05 if move == "push" else 0.05)))
    elif move == "punch" and punch_at is not None:
        z = "if(gte(in_time,%.3f),%.3f,1)" % (max(0.0, punch_at), PUNCH)
        xc = "(iw-iw/zoom)*%.3f" % min(1.0, max(0.0, x))
    elif move == "tight":                          # the mid-scene glance: a TIGHTER frame
        z = "%.3f" % TIGHT                         # than the same person's speaking shot,
        xc = "(iw-iw/zoom)*%.3f" % min(1.0, max(0.0, x))   # so the cut between them isn't
                                                   # a jump cut
    else:
        return ""
    return ",scale=%d:%d,zoompan=z='%s':x='%s':y='%s':d=1:s=%dx%d:fps=%d" % (
        2 * w, 2 * h, z, xc, yc, w, h, OUT_FPS)


POST_LOOKS = {
    # a doorbell / security camera: wide-angle barrel, washed-out colour, sensor noise,
    # a dark vignette (the camera never moves)
    "cctv": ",lenscorrection=k1=0.18:k2=0.06,scale=iw*1.3:ih*1.3,crop=iw/1.3:ih/1.3,"
            "hue=s=0.35,eq=contrast=1.12:brightness=0.02,noise=alls=14:allf=t,"
            "vignette=PI/4.2",
}


def _normalise(src, out, w=OUT_W, h=OUT_H, fps=OUT_FPS, ar=44100, ss=0.0, dur=None,
               move="static", punch_at=None, x=0.5, jumps=(), look=None, fit="fill",
               crop_y=0.5):
    """One clip for the cut: FILL the frame (scale up + crop, lanczos, a touch of unsharp —
    the old decrease+pad left 7-12 px black bars), trimmed to [ss, ss+dur], with a camera
    move done here in post (a push, or a punch-in at `punch_at` s into the trimmed clip).
    The crop aims at `x` across / `crop_y` down (only matters when the clip's shape differs
    from the frame: the export pack's square cut). fit="blur": the whole clip, centred over
    a blurred copy of itself (a tall video exported wide) instead of a crop that would keep
    a sliver of it."""
    cx = "(iw-%d)*%.3f" % (w, min(1.0, max(0.0, x)))
    cy = "(ih-%d)*%.3f" % (h, min(1.0, max(0.0, crop_y)))
    if fit == "blur":
        vf = ("split[fl_a][fl_b];[fl_a]scale=%d:%d:force_original_aspect_ratio=increase,"
              "crop=%d:%d,boxblur=24:2,eq=brightness=-0.10[fl_bg];[fl_b]scale=%d:%d:"
              "force_original_aspect_ratio=decrease:flags=lanczos[fl_fg];"
              "[fl_bg][fl_fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=%d"
              % (w, h, w, h, w, h, fps))
    else:
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,"
              "crop=%d:%d:%s:%s,setsar=1,fps=%d" % (w, h, w, h, cx, cy, fps))
    vf += _move_filter(move, w, h, dur or 3.0, punch_at, x=x, jumps=jumps)
    vf += POST_LOOKS.get(look or "", ",unsharp=5:5:0.45:3:3:0")
    # -ss BEFORE -i: the clip starts at 0 inside the filters, so zoompan's in_time (punch-in,
    # jump zooms) is trimmed time. After -i it was source time, and every punch landed
    # trim_in seconds early.
    args = [FFMPEG, "-v", "error", "-y"] + (["-ss", "%.3f" % ss] if ss else []) + ["-i", src]
    if dur:
        args += ["-t", "%.3f" % dur]
    _run(args + ["-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                 "-crf", "20", "-c:a", "aac", "-ar", str(ar), "-ac", "2", out], timeout=900)
    return out


def _concat(paths, out):
    wd = os.path.dirname(os.path.abspath(out))
    lst = os.path.join(wd, "concat_list.txt")
    with open(lst, "w") as f:
        for p in paths:
            f.write("file '%s'\n" % os.path.abspath(p).replace("'", "'\\''"))
    _run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
          "-c", "copy", out], timeout=900)
    return out


def _mix_audio(video, entries, out, duck=0.30):
    """Voice lines laid on top of the clips' own (H3) audio, which is ducked under them.

    `entries` is [(shot, offset_seconds), ...] for shots that carry `audio_wav`."""
    voices = [(sh, off) for sh, off in entries
              if sh.get("audio_wav") and os.path.exists(sh["audio_wav"])]
    if not voices:
        shutil.copyfile(video, out)
        return out
    args = [FFMPEG, "-v", "error", "-y", "-i", video]
    for sh, _ in voices:
        args += ["-i", sh["audio_wav"]]
    fc = ["[0:a]volume=%.2f[bed]" % duck]
    mixins = ["[bed]"]
    for i, (sh, off) in enumerate(voices, start=1):
        ms = int(round(off * 1000))
        tin = float(sh.get("trim_in") or 0.0)       # the clip was trimmed: so is its voice
        tout = sh.get("voice_out") or sh.get("trim_out")  # (voice_out: a J-cut runs on)
                                                    # ...at both ends, or a line the aligner
        parts = []                                  # cut short spills over the next shot
        if tin > 0:
            parts.append("start=%.3f" % tin)
        if tout:
            parts.append("end=%.3f" % float(tout))
        trim = ("atrim=%s,asetpts=PTS-STARTPTS," % ":".join(parts)) if parts else ""
        fc.append("[%d:a]%sadelay=%d|%d,volume=1.0[v%d]" % (i, trim, ms, ms, i))
        mixins.append("[v%d]" % i)
    fc.append("%samix=inputs=%d:duration=first:normalize=0[aout]"
              % ("".join(mixins), len(mixins)))
    args += ["-filter_complex", ";".join(fc), "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2", out]
    _run(args, timeout=900)
    return out


ROOM_TONE = 0.012             # brown noise, filtered: a quiet room under every cut
WHOOSH_LEAD = 0.25            # the whoosh starts this long before the punch-in


_OUTDOOR = re.compile(r"\b(street|sidewalk|road|park|beach|porch|parking|plaza|square|"
                      r"garden|yard|field|forest|trail|boardwalk|market|outside|outdoor|"
                      r"alley|bus stop|station platform|rooftop|lake|harbor|harbour)\b", re.I)


def ambience(where):
    """The bed under the cut, by place: an outdoor place gets a wider, brighter wash (traffic,
    wind, distance); anything else the quiet low room tone."""
    if where and _OUTDOOR.search(where):
        return ("anoisesrc=color=pink:amplitude=%.4f:duration=%%.3f:sample_rate=44100,"
                "highpass=f=120,lowpass=f=3500,tremolo=f=0.15:d=0.3" % (ROOM_TONE * 1.6))
    return ("anoisesrc=color=brown:amplitude=%.4f:duration=%%.3f:sample_rate=44100,"
            "highpass=f=70,lowpass=f=900" % ROOM_TONE)


def sound_design(video, out, whooshes=(), where="", sfx=()):
    """Glue the cut (review panel, motion designer: dry booth voices over pictures whose
    background jumps at every cut): a continuous ROOM TONE under everything, and a short
    WHOOSH before each punch-in (`whooshes` = times in seconds). Both made by ffmpeg — no
    sound files to ship."""
    dur = _duration(video)
    if not dur:
        shutil.copyfile(video, out)
        return out
    fc = [ambience(where) % dur + ",aformat=channel_layouts=stereo[room]"]
    mix = ["[0:a]", "[room]"]
    extra = []                      # SOUND EFFECTS (sfx.py): [(seconds, file)] under lines
    for k, (t, path) in enumerate([x for x in sfx if 0 <= x[0] < dur and x[1] and
                                   os.path.exists(x[1])][:6]):
        extra += ["-i", path]
        ms = int(round(t * 1000))
        fc.append("[%d:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=0.55,"
                  "adelay=%d|%d[s%d]" % (k + 1, ms, ms, k))
        mix.append("[s%d]" % k)
    for i, t in enumerate([w for w in whooshes if 0 <= w < dur]):
        ms = int(round(max(0.0, t - WHOOSH_LEAD) * 1000))
        fc.append("anoisesrc=color=pink:amplitude=0.35:duration=0.32:sample_rate=44100,"
                  "bandpass=f=1800:width_type=o:w=1.4,"
                  "afade=t=in:st=0:d=0.22,afade=t=out:st=0.22:d=0.10,volume=0.5,"
                  "aformat=channel_layouts=stereo,adelay=%d|%d[w%d]" % (ms, ms, i))
        mix.append("[w%d]" % i)
    fc.append("%samix=inputs=%d:duration=first:normalize=0[aout]" % ("".join(mix), len(mix)))
    _run([FFMPEG, "-v", "error", "-y", "-i", video] + extra +
         ["-filter_complex", ";".join(fc),
          "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-ar", "44100",
          "-ac", "2", out], timeout=900)
    return out


def sfx_events(pj, norm):
    """[(seconds into the cut, file)] for the lines whose acting names a sound (sh["sfx"]):
    the sound lands as the line's first word ends (or 0.3 s into the shot)."""
    if pj.get("sfx") is False:
        return []
    try:
        import sfx as _sfx
    except Exception:
        return []
    out = []
    for sh, _p in norm:
        cue = sh.get("sfx")
        if not cue or sh.get("is_reaction") or sh.get("cutaway"):
            continue
        path = _sfx.find(cue)
        if not path:
            continue
        ws = sh.get("words") or []
        t = (float(sh.get("cut_in") or 0) + float(ws[0].get("end") or 0) -
             float(sh.get("trim_in") or 0)) if ws else float(sh.get("cut_in") or 0) + 0.3
        out.append((round(max(float(sh.get("cut_in") or 0), t), 3), path))
    return out


def _cover(video, out, at=1.0):
    _run([FFMPEG, "-v", "error", "-y", "-ss", "%.2f" % at, "-i", video,
          "-frames:v", "1", out], timeout=120)
    return out


AI_TAG = "aigenerated"          # TikTok / Reels / Shorts ask for AI content to be labelled


def _post_text(pc, ai_label=False):
    if not isinstance(pc, dict):
        return str(pc or "")
    tags = ["#" + str(t).lstrip("#") for t in (pc.get("hashtags") or [])]
    if ai_label and ("#" + AI_TAG) not in [t.lower() for t in tags]:
        tags.append("#" + AI_TAG)
    return "%s\n\n%s\n\n%s\n" % (pc.get("title", ""), pc.get("caption", ""), " ".join(tags))


def _post_script(pj):
    """What post_copy() is written FROM. A project built without a Director script (e.g. a
    forced test) would otherwise hand the LLM an empty script and get unrelated copy — so
    fall back to the idea + the shots' own actions/lines."""
    sc = pj.get("script")
    if isinstance(sc, dict) and (sc.get("title") or sc.get("beats")):
        return sc
    beats = [{"t_start": 0, "t_end": s.get("seconds", 3), "action": s.get("action", ""),
              "line": s.get("line", ""), "speaker": s.get("speaker", "")}
             for s in (pj.get("shots") or [])]
    return {"title": pj.get("title") or pj.get("slug"), "hook": pj.get("idea", ""),
            "beats": beats, "button": ""}


def assemble(pj):
    """Concat the chosen clips, overlay the voice lines, burn captions, loudnorm, and write
    <slug>_final.mp4 + cover.png + post.txt."""
    picked = []
    for sh in pj["shots"]:
        if sh.get("skip"):                  # left out on the Cut page's timeline
            continue
        cp = _chosen_clip_path(pj, sh)
        if not cp:
            raise RuntimeError("shot %s has no chosen take — render first" % sh["n"])
        picked.append((sh, cp))

    work = os.path.join(project_dir(pj["slug"]), "cut")
    os.makedirs(work, exist_ok=True)

    out_w, out_h = OUT_SIZE.get(pj.get("aspect", "9:16"), OUT_SIZE["9:16"])
    # word times FIRST: the edit trims each clip to its words
    for sh, p in picked:
        if not sh.get("words") and sh.get("audio_wav") and os.path.exists(sh["audio_wav"]):
            # no aligned words (Dialogue -> Episode): align the line against its real voice
            # on the rig, so each caption lands with its word; failing that, spread the words
            # over the voice's real speech span
            vs = _duration(sh["audio_wav"])
            if vs:
                sh["voice_s"] = round(min(vs, _duration(p) or vs), 3)
            if wants_captions(pj) and (sh.get("line") or "").strip():
                try:
                    got = audio_client.align(sh["audio_wav"], sh["line"])
                    # nearly every word, or the trim would cut the line off at the last
                    # word the aligner found (half used to pass)
                    if got and len(got) >= max(1, int(0.9 * len(sh["line"].split()))):
                        got = _with_line_words(got, sh["line"])
                        sh["words"] = [w for w in got
                                       if float(w.get("start") or 0) < (sh.get("voice_s") or 1e9)]
                except Exception as exc:
                    _log(pj, "caption align failed on shot %s (%r) - spread over the voice"
                         % (sh["n"], exc))
            if not sh.get("words") and (sh.get("line") or "").strip():
                # no aligner: the words over the voice's real speech span — still good
                # enough to trim to (silence-detected)
                sh["words"] = _even_words(sh["line"], sh.get("voice_s") or _duration(p),
                                          wav=sh["audio_wav"])
                sh["words_estimated"] = True

    moves = camera_moves(pj)
    trim_on = pj.get("trim", True) and not pj.get("music_video")
    # the ORDER: the lines, and after the button the LISTENER's reaction (if one was shot)
    lines_p = [(sh, cp) for sh, cp in picked if not sh.get("is_reaction")]
    reacts = {sh.get("speaker"): (sh, cp) for sh, cp in picked if sh.get("is_reaction")}
    order = list(lines_p)
    button_react = None
    if reacts and len(lines_p) >= 2 and pace_of(pj) != "full":
        last_spk = lines_p[-1][0].get("speaker")
        for spk in [x for x in (pj.get("characters") or []) if x != last_spk]:
            if spk in reacts:
                button_react = reacts[spk]
                order.append(button_react)
                break
        # AT THE TURN: a short glance at the listener mid-scene (round-2 review: the
        # listener only ever appeared after the last line). Its own copy, so the button
        # reaction keeps its own segment and file.
        if len(lines_p) >= 4:
            mid = len(lines_p) // 2
            said = lines_p[mid - 1][0].get("speaker")
            lis = next((x for x in (pj.get("characters") or []) if x != said and x in reacts),
                       None)
            if lis:
                ca = dict(reacts[lis][0], n=reacts[lis][0]["n"] + 100, cutaway=True)
                order.insert(order.index(lines_p[mid]), (ca, reacts[lis][1]))
    norm = []
    fade_s = max(0.0, float(pj.get("end_fade") or 0.0))
    for i, (sh, cp) in enumerate(order):
        out = os.path.join(work, "n%02d.mp4" % sh["n"])
        clip_s = _duration(cp) or float(sh.get("seconds", 3))
        if sh.get("cutaway"):
            a = min(REACTION_IN + REACTION_HOLD, max(0.0, clip_s - CUTAWAY))
            b = min(clip_s, a + CUTAWAY)
        elif sh.get("is_reaction"):
            a = min(REACTION_IN, max(0.0, clip_s - REACTION_HOLD))
            b = min(clip_s, a + REACTION_HOLD)
        else:
            is_button = sh is lines_p[-1][0]
            is_setup = len(lines_p) >= 2 and sh is lines_p[-2][0]
            a, b = (_trim_for(sh, clip_s, last=is_button and button_react is None,
                              pace=pace_of(pj), extra=PENULT_HOLD if is_setup else 0.0)
                    if trim_on else (0.0, min(clip_s, float(sh.get("slice_len") or clip_s))))
        # the Cut page's timeline: "start earlier / later", "end earlier / later" (seconds)
        a = min(max(0.0, a + float(sh.get("nudge_in") or 0.0)), max(0.0, b - 0.3))
        b = max(min(clip_s, b + float(sh.get("nudge_out") or 0.0)), a + 0.3)
        if fade_s and i == len(order) - 1:
            # the LAST shot runs on into the fade (as much as its clip has), so the fade
            # never eats the button or the reaction
            b = min(clip_s, b + fade_s)
        # J-CUT into the mid-scene glance (test run 2026-10-18: a silent 0.7 s on the
        # listener, then the SAME person in close-up speaking = a jump cut): the line before
        # the glance ends its picture early and its voice carries on under the listener
        sh.pop("voice_out", None)
        sh.pop("j_overlap", None)
        nxt = order[i + 1][0] if i + 1 < len(order) else None
        if nxt is not None and nxt.get("cutaway") and not sh.get("is_reaction"):
            ov = round(min(CUTAWAY, max(0.0, (b - a) - 1.0)), 3)
            if ov > 0.05:
                sh["voice_out"], sh["j_overlap"] = b, ov
                b -= ov
        sh["trim_in"], sh["trim_out"] = a, b
        mv = "tight" if sh.get("cutaway") else moves.get(sh["n"], "static")
        punch_at, jumps = None, ()
        ws = sh.get("words") or []
        is_last_line = bool(lines_p) and sh is lines_p[-1][0]
        if mv == "punch" or (mv == "jumps" and is_last_line and len(lines_p) >= 1):
            punch_at = (float(ws[-1].get("start") or 0) - a) if ws else None
        if mv == "jumps":
            jumps = [st_ - a for st_ in (sh.get("line_starts") or [])[1:]]
        _normalise(cp, out, w=out_w, h=out_h, ss=a, dur=b - a, move=mv, punch_at=punch_at,
                   fit=pj.get("fit", "fill"), crop_y=float(pj.get("crop_y", 0.5)),
                   x=speaker_x(pj, sh), jumps=jumps, look=pj.get("post_look"))
        sh["move"] = mv
        norm.append((sh, out))

    cat = _concat([p for _, p in norm], os.path.join(work, "concat.mp4"))

    starts, t = {}, 0.0
    for sh, p in norm:                              # offsets from the ACTUAL clip lengths
        starts[sh["n"]] = t
        t += _duration(p) or float(sh.get("seconds", 3))
    for sh, p in norm:
        # captions read word times from the VOICE's start: the clip was trimmed by trim_in,
        # so the voice's zero sits trim_in before the clip starts in the cut
        sh["start"] = round(starts[sh["n"]] - float(sh.get("trim_in") or 0), 3)
        sh["cut_in"] = round(starts[sh["n"]], 3)
        # (a J-cut line's words run on over the glance: its captions may too)
        sh["cut_out"] = round(starts[sh["n"]] + (_duration(p) or 0) +
                              float(sh.get("j_overlap") or 0), 3)

    # H3's own track carries the guide voice again (and its own guesses): under the TTS it
    # sounded doubled. Muted for talking videos; the room tone below replaces its air.
    mixed = _mix_audio(cat, [(sh, starts[sh["n"]]) for sh, _ in norm],
                       os.path.join(work, "mixed.mp4"),
                       duck=0.30 if pj.get("clip_audio") else 0.0)
    button_t = None
    if lines_p and lines_p[-1][0].get("words"):
        lb = lines_p[-1][0]
        button_t = (float(lb.get("cut_in") or 0) + float(lb["words"][-1].get("start") or 0)
                    - float(lb.get("trim_in") or 0))
    if pj.get("sound_design", True) and not pj.get("music_video"):
        wh = []
        for sh, _p in norm:
            if pj.get("whoosh") and sh.get("move") == "punch" and sh.get("words"):
                wh.append(float(sh["cut_in"]) + float(sh["words"][-1].get("start") or 0)
                          - float(sh.get("trim_in") or 0))
        try:
            ev = sfx_events(pj, norm)
            mixed = sound_design(mixed, os.path.join(work, "sound.mp4"), wh,
                                 where=set_where(pj) if pj.get("where") or
                                 pj.get("place") else "", sfx=ev)
            pj["sfx_placed"] = [[t, os.path.basename(p_)] for t, p_ in ev]
            pj["whooshes"] = [round(w, 3) for w in wh]
        except Exception as exc:
            _log(pj, "sound design skipped: %r" % exc)
    music = pj.get("music")
    if music and os.path.exists(music):
        mixed = add_music(mixed, music, os.path.join(work, "with_music.mp4"),
                          stop_at=button_t)
    elif music:
        _log(pj, "music bed missing, cut without it: %s" % music)
    if fade_s:
        mixed = end_fade(mixed, os.path.join(work, "faded.mp4"), fade_s)

    final = os.path.join(project_dir(pj["slug"]), "%s_final.mp4" % pj["slug"])
    ass = None
    if wants_captions(pj) or (pj.get("hook_text") or "").strip() or pj.get("stamp_text"):
        ass = os.path.join(work, "captions.ass")
        with open(ass, "w") as f:
            fs_, mv_ = caption_style(out_w, out_h)
            f.write(build_ass([s_ for s_ in pj["shots"] if not s_.get("skip")],
                              width=out_w, height=out_h, font_size=fs_,
                              margin_v=mv_, look=caption_look(pj),
                              hook=pj.get("hook_text"), captions=wants_captions(pj),
                              stamp=pj.get("stamp_text")))
        finish(mixed, ass, final)
    else:
        finish_plain(mixed, final)

    cover = _cover(final, os.path.join(project_dir(pj["slug"]), "cover.png"), at=1.0)

    post = os.path.join(project_dir(pj["slug"]), "post.txt")
    if pj.get("aspect", "9:16") == "9:16":       # social copy is for SHORTS only
        try:
            d = get_director()
            d.load()
            pc = d.post_copy(_primary_char(pj), _post_script(pj))
        except Exception as exc:
            pc = {}
            _log(pj, "post_copy failed: %r" % exc)
        with open(post, "w") as f:
            f.write(_post_text(pc, ai_label=pj.get("ai_label", True)))
    else:
        post = None

    pj["cut"] = {"mp4": final, "captions": ass, "post": post, "cover": cover,
                 "starts": starts}
    _log(pj, "assembled -> %s" % final)
    save_project(pj)
    return pj["cut"]


# ---------------------------------------------------------------------------
# One-call driver
# ---------------------------------------------------------------------------
def run(project_or_brief, stages=None, budget_s=None, n_pitches=5):
    """Drive the project through `stages` (default all). Every stage is guarded so a
    re-run RESUMES: an existing pitch/script/shot list is not regenerated, and a rendered
    shot is not re-rendered."""
    if isinstance(project_or_brief, dict) and "slug" in project_or_brief and "shots" in project_or_brief:
        pj = project_migrate(project_or_brief)
    elif isinstance(project_or_brief, dict):
        chars = project_or_brief.get("characters") or project_or_brief.get("character")
        pj = new_project(chars, project_or_brief.get("idea", ""),
                         length_s=project_or_brief.get("length_s", 20),
                         quality=project_or_brief.get("quality", "Draft"),
                         gpus=project_or_brief.get("gpus", 1),
                         title=project_or_brief.get("title"))
    elif isinstance(project_or_brief, str):
        pj = load_project(project_or_brief)
        if pj is None:
            raise ValueError("no such project: %r" % project_or_brief)
    else:
        raise TypeError("run() takes a project dict, a brief dict or a slug")

    for st in (stages or STAGES):
        if st == "pitch":
            if not pj.get("pitches"):
                pitch(pj, n=n_pitches)
        elif st == "choose":
            if pj.get("pitches") and not pj.get("pitch"):
                choose_pitch(pj, 0)
        elif st == "script":
            if not pj.get("script"):
                write_script(pj)
        elif st == "shots":
            if not pj.get("shots"):
                plan_shots(pj)
        elif st == "voice":
            voice_lines(pj)
        elif st == "estimate":
            pj["_estimate"] = estimate(pj)
        elif st == "render":
            render_all(pj, budget_s=budget_s)
        elif st == "assemble":
            assemble(pj)
        else:
            raise ValueError("unknown stage %r (known: %s)" % (st, ", ".join(STAGES)))
    save_project(pj)
    return pj


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_pj(pj):
    print("project : %s  (%s)" % (pj["title"], pj["slug"]))
    print("chars   : %s" % ", ".join(pj.get("characters") or []))
    print("idea    : %s" % pj.get("idea"))
    print("quality : %s   length target %ss" % (pj.get("quality"), pj.get("length_s")))
    if pj.get("pitch"):
        print("pitch   : %s" % pj["pitch"].get("title"))
    if pj.get("script"):
        print("script  : %s" % pj["script"].get("title"))
    for sh in pj.get("shots") or []:
        print("  shot %s  %ss  %-16s %-5s line=%r chosen=%s"
              % (sh["n"], sh["seconds"], sh["intent"], sh["video_mode"],
                 (sh.get("line") or "")[:40], sh.get("chosen")))
    if pj.get("cut", {}).get("mp4"):
        print("cut     : %s" % pj["cut"]["mp4"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Forge Lab Studio pipeline (Tk-free)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new")
    p_new.add_argument("--character", action="append", required=True)
    p_new.add_argument("--idea", required=True)
    p_new.add_argument("--length", type=int, default=20)
    p_new.add_argument("--quality", default="Draft", choices=list(QUALITIES))
    p_new.add_argument("--title")
    p_new.add_argument("--slug")

    for name in ("pitch", "choose", "script", "shots", "voice", "estimate", "render",
                 "assemble", "show", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("slug")
        if name == "pitch":
            sp.add_argument("-n", type=int, default=5)
        if name == "choose":
            sp.add_argument("-i", type=int, default=0)
        if name == "render":
            sp.add_argument("--budget", type=float, default=None)
        if name == "run":
            sp.add_argument("--stages", default=",".join(STAGES))
            sp.add_argument("--budget", type=float, default=None)

    a = ap.parse_args(argv)

    if a.cmd == "new":
        pj = new_project(a.character, a.idea, length_s=a.length, quality=a.quality,
                         title=a.title, slug=a.slug)
        _print_pj(pj)
        return 0

    if a.cmd == "show":
        pj = load_project(a.slug)
        if not pj:
            print("no such project: %s" % a.slug)
            return 2
        print(json.dumps(pj, indent=1))
        return 0

    if a.cmd == "run":
        pj = run(a.slug, stages=[s for s in a.stages.split(",") if s], budget_s=a.budget)
        _print_pj(pj)
        return 0

    pj = load_project(a.slug)
    if not pj:
        print("no such project: %s" % a.slug)
        return 2

    if a.cmd == "pitch":
        d = pitch(pj, n=a.n)
        for i, p in enumerate(d):
            print("%d. %s\n   %s" % (i, p.get("title"), p.get("logline")))
    elif a.cmd == "choose":
        choose_pitch(pj, a.i)
    elif a.cmd == "script":
        write_script(pj)
    elif a.cmd == "shots":
        plan_shots(pj)
    elif a.cmd == "voice":
        voice_lines(pj)
    elif a.cmd == "estimate":
        est = estimate(pj)
        for e in est["per_shot"]:
            print("shot %s  %ss  %sx%s  %s steps  ~%s s"
                  % (e["n"], e["seconds"], e["canvas"][0], e["canvas"][1], e["steps"],
                     e["estimate_s"]))
        print("TOTAL ~%s s" % est["total_s"])
    elif a.cmd == "render":
        ran = render_all(pj, budget_s=a.budget)
        print("rendered shots: %s" % (ran or "none"))
    elif a.cmd == "assemble":
        cut = assemble(pj)
        print("cut: %s" % cut.get("mp4"))
    _print_pj(load_project(pj["slug"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())