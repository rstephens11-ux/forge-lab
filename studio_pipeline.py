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
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "audio")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import forge_core as c          # noqa: E402  (Tk-free facade over forge_lab)
import forge_lab as fl          # noqa: E402  (same module object; for CHARACTERS/_index_update)

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
STEPS = 12
QUALITIES = tuple(CANVAS)

STAGES = ("pitch", "choose", "script", "shots", "voice", "estimate", "render", "assemble")

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FORGE_SSH = os.environ.get("FORGE_SSH", "ryan@forge")
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
        with os.fdopen(fd, "w") as f:
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
                 ("takes", []), ("chosen", None), ("status", "new"), ("start", 0.0)):
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
                 ("pitch", None), ("pitches", []), ("script", None), ("shots", []),
                 ("voice", {}), ("cut", {}), ("log", [])):
        pj.setdefault(k, v)
    pj["shots"] = [_shot_migrate(dict(s)) for s in (pj.get("shots") or [])]
    return pj


def _blank(slug, characters, idea, length_s, quality, title=None, gpus=1):
    return project_migrate({
        "slug": slug, "title": title or slug, "created": time.time(),
        "characters": list(characters), "idea": idea, "length_s": int(length_s),
        "quality": quality, "gpus": int(gpus), "pitch": None, "pitches": [], "script": None,
        "shots": [], "voice": {}, "cut": {}, "log": [],
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
def new_project(characters, idea, length_s=20, quality="Draft", title=None, slug=None, gpus=1):
    if isinstance(characters, str):
        characters = [characters]
    characters = [x for x in (characters or []) if x]
    if not characters:
        raise ValueError("new_project needs at least one character")
    if quality not in QUALITIES:
        raise ValueError("quality must be one of %s (got %r)" % (QUALITIES, quality))
    if int(gpus) not in (1, 2):
        raise ValueError("gpus must be 1 or 2 (got %r)" % (gpus,))
    slug = slug or slugify(title or idea or "untitled")
    pj = _blank(slug, characters, idea, length_s, quality, title, gpus=int(gpus))
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
def shot_params(pj, sh, seed=-1):
    w, h = CANVAS.get(pj.get("quality", "Draft"), CANVAS["Draft"])
    vm = sh.get("video_mode") or "r2v"
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
    p = shot_params(pj, sh, seed=seed)
    if int(p.get("gpus", 1)) >= 2:
        # A 2-card render needs BOTH cards; the Director lives on card 2. Release it first
        # and record the answer — "GPU1 released" is a fact in the log, never a hope.
        if not release_gpu1_for_render():
            _log(pj, "shot %s: GPU1 NOT confirmed released before a 2-card render" % sh["n"])
    refs = _render_refs(pj, sh)

    if p["video_mode"] == "r2v" and refs and "<Picture 1>" not in p["prompt"]:
        p["prompt"] = "<Picture 1> is the character. " + p["prompt"]

    if sh.get("intent") == "lipsync" and sh.get("audio_wav"):
        if not os.path.exists(sh["audio_wav"]):
            raise ValueError("shot %s: guide audio missing: %s" % (sh["n"], sh["audio_wav"]))
        p["guide_audio_path"] = sh["audio_wav"]      # submit() uploads it

    pid, got_seed = c.submit("video", p, refs)
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


def _group_words(words, lo=2, hi=4):
    """Chunk words into caption events of 2-4 words (never a lonely 1-word tail)."""
    words = [w for w in (words or []) if (w.get("word") or "").strip()]
    groups, i = [], 0
    while i < len(words):
        take = min(hi, len(words) - i)
        if len(words) - i - take == 1:
            take = max(lo, take - 1)
        groups.append(words[i:i + take])
        i += take
    return groups


def build_ass(shots, width=OUT_W, height=OUT_H, font="Arial", font_size=76):
    """A word-by-word caption track: one event per 2-4 words, timed from the shot's aligned
    words PLUS the shot's `start` offset in the finished cut.

    `shots` must carry `start` (seconds offset) and `words` ([{word,start,end}])."""
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
        # large bold white, thick black outline, bottom-centre (2), lifted into the lower third
        "Style: Cap,%s,%d,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,"
        "0,0,1,5,3,2,60,60,240,1" % (font, font_size),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    for sh in shots:
        start = float(sh.get("start") or 0.0)
        for group in _group_words(sh.get("words")):
            t0 = start + float(group[0].get("start") or 0.0)
            t1 = start + float(group[-1].get("end") or 0.0)
            if t1 <= t0:
                t1 = t0 + 0.2
            text = " ".join((w.get("word") or "").strip() for w in group).strip()
            text = text.replace("\n", " ").upper()
            if not text:
                continue
            events.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s"
                          % (_ass_time(t0), _ass_time(t1), text))
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


def _finish_local(in_mp4, ass_path, out_mp4, loudnorm=True):
    """Burn captions locally — run from the input's own dir so the ass= filename needs no
    filtergraph escaping (no ':' or quotes)."""
    wd = os.path.dirname(os.path.abspath(in_mp4))
    ass_name = "captions.ass"
    if os.path.abspath(ass_path) != os.path.abspath(os.path.join(wd, ass_name)):
        shutil.copyfile(ass_path, os.path.join(wd, ass_name))
    args = [FFMPEG, "-v", "error", "-y", "-i", os.path.basename(in_mp4),
            "-vf", "ass=%s" % ass_name]
    if loudnorm:
        args += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
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
        af = " -af loudnorm=I=-14:TP=-1.5:LRA=11" if loudnorm else ""
        cmd = ("cd %s && %s -v error -y -i in.mp4 -vf ass=captions.ass%s "
               "-c:v libx264 -pix_fmt yuv420p -preset medium -crf 20 "
               "-c:a aac -b:a 192k -ar 48000 out.mp4"
               % (shlex.quote(remote), shlex.quote(FORGE_FFMPEG), af))
        ssh(cmd)
        _run(["scp", "-q", "%s:%s/out.mp4" % (FORGE_SSH, remote), out_mp4])
    finally:
        try:
            ssh("rm -rf %s" % shlex.quote(remote))
        except Exception:
            pass
    return out_mp4


def finish(in_mp4, ass_path, out_mp4, loudnorm=True):
    """Burn captions onto the cut. Chooses the host by the 'ass' filter capability."""
    if ffmpeg_has("ass"):
        return _finish_local(in_mp4, ass_path, out_mp4, loudnorm=loudnorm)
    return _finish_remote(in_mp4, ass_path, out_mp4, loudnorm=loudnorm)


# ---------------------------------------------------------------------------
# Stage: assemble
# ---------------------------------------------------------------------------
def _normalise(src, out, w=OUT_W, h=OUT_H, fps=OUT_FPS, ar=44100):
    vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%d" % (w, h, w, h, fps))
    _run([FFMPEG, "-v", "error", "-y", "-i", src, "-vf", vf,
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
          "-c:a", "aac", "-ar", str(ar), "-ac", "2", "-shortest", out], timeout=900)
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
        fc.append("[%d:a]adelay=%d|%d,volume=1.0[v%d]" % (i, ms, ms, i))
        mixins.append("[v%d]" % i)
    fc.append("%samix=inputs=%d:duration=first:normalize=0[aout]"
              % ("".join(mixins), len(mixins)))
    args += ["-filter_complex", ";".join(fc), "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2", out]
    _run(args, timeout=900)
    return out


def _cover(video, out, at=1.0):
    _run([FFMPEG, "-v", "error", "-y", "-ss", "%.2f" % at, "-i", video,
          "-frames:v", "1", out], timeout=120)
    return out


def _post_text(pc):
    if not isinstance(pc, dict):
        return str(pc or "")
    tags = " ".join("#" + str(t).lstrip("#") for t in (pc.get("hashtags") or []))
    return "%s\n\n%s\n\n%s\n" % (pc.get("title", ""), pc.get("caption", ""), tags)


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
        cp = _chosen_clip_path(pj, sh)
        if not cp:
            raise RuntimeError("shot %s has no chosen take — render first" % sh["n"])
        picked.append((sh, cp))

    work = os.path.join(project_dir(pj["slug"]), "cut")
    os.makedirs(work, exist_ok=True)

    norm = []
    for sh, cp in picked:
        out = os.path.join(work, "n%02d.mp4" % sh["n"])
        _normalise(cp, out)
        norm.append((sh, out))

    cat = _concat([p for _, p in norm], os.path.join(work, "concat.mp4"))

    starts, t = {}, 0.0
    for sh, p in norm:                              # offsets from the ACTUAL clip lengths
        starts[sh["n"]] = t
        t += _duration(p) or float(sh.get("seconds", 3))
    for sh, _ in norm:
        sh["start"] = round(starts[sh["n"]], 3)

    mixed = _mix_audio(cat, [(sh, starts[sh["n"]]) for sh, _ in norm],
                       os.path.join(work, "mixed.mp4"))

    ass = os.path.join(work, "captions.ass")
    with open(ass, "w") as f:
        f.write(build_ass(pj["shots"]))

    final = os.path.join(project_dir(pj["slug"]), "%s_final.mp4" % pj["slug"])
    finish(mixed, ass, final)

    cover = _cover(final, os.path.join(project_dir(pj["slug"]), "cover.png"), at=1.0)

    post = os.path.join(project_dir(pj["slug"]), "post.txt")
    try:
        d = get_director()
        d.load()
        pc = d.post_copy(_primary_char(pj), _post_script(pj))
    except Exception as exc:
        pc = {}
        _log(pj, "post_copy failed: %r" % exc)
    with open(post, "w") as f:
        f.write(_post_text(pc))

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