"""SCENE ENGINE — the Tk-free half of the new Dialogue screen (2026-10-17).

Ryan: "i still think the dialogue needs a better UI for me to use. should be intuitive" ->
"do what would be best for dialogue". The new screen (dialogue2_ui) is the screenplay itself:
each line a card you click to hear, redo ("angrier", "deadpan"), pick a take or reword; voices
are CAST like auditions — three reads of the character's own voice description, keep one, and
it is saved to the CHARACTER so every later scene sounds the same.

This module holds everything that is not drawing:
  * line edits on the project (edit / add / delete / move / mood) through the script text, so
    dialogue.reparse keeps every untouched line's takes;
  * render(): the old window's proven render pipeline (design missing samples on qwen -> read
    the lines -> sung lines through Song -> listen back with ASR -> save), callable for the whole
    scene OR for chosen lines (a redo);
  * casting from Characters: a character's bible voice becomes a voice description; auditions
    design three reads of it; keeping one saves the voice and links it to the character;
  * assemble(): takes -> fx -> peak-normalise -> one scene.wav with gaps.
Nothing here imports tkinter or forge_lab. The rig is reached through audio.client (tests
swap `tts_batch` / `transcribe_many` on this module).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import dialogue as dlg
import dialogue_ui as _dui          # Tk-free at import: plan_render, plan_sung, render_sung

# Redo chips -> the mood the line is read in (all are dlg.MOOD_WORDS keys)
REDO = (("angrier", "angry"), ("deadpan", "deadpan"), ("whisper", "whisper"),
        ("shout it", "shouting"), ("laughing", "laughing"), ("nervous", "nervous"),
        ("sarcastic", "sarcastic"), ("sad", "sad"), ("excited", "excited"))
AUDITION_TWISTS = ("", ", a little lower and slower", ", a little brighter and quicker")
AUDITION_TEXT = "Well. I suppose that's one way of looking at it."

tts_batch = None            # set lazily from audio.client; tests replace these two
transcribe_many = None


def _rig():
    global tts_batch, transcribe_many
    if tts_batch is None or transcribe_many is None:
        from audio import client as _ac
        tts_batch = tts_batch or _ac.tts_batch
        transcribe_many = transcribe_many or _ac.transcribe_many
    return tts_batch, transcribe_many


def project_dir(pj):
    return os.path.join(dlg.dialogue_dir(), pj.get("slug", "untitled"))


# ================================================================== line edits
def line_to_text(li):
    spk = dlg.normalise_speaker(li.get("speaker", "")) or "NARRATOR"
    d = (li.get("direction") or "").strip()
    return "%s: %s%s" % (spk, ("(%s) " % d) if d else "", (li.get("text") or "").strip())


def script_from_lines(lines):
    return "\n".join(line_to_text(li) for li in lines) + "\n"


def _apply(pj, lines):
    """Lines -> script -> reparse (takes, moods and sung choices survive on unchanged lines)."""
    keep = {k: pj[k] for k in pj if k not in ("script", "lines")}
    out = dlg.reparse(pj, script_from_lines(lines))
    out.update(keep)
    return out


def edit_line(pj, i, text=None, speaker=None):
    """New words (or a new speaker) for line i — that line loses its takes (it must be
    re-read); every other line keeps its own."""
    lines = [dict(li) for li in pj.get("lines", [])]
    if text is not None:
        lines[i]["text"] = " ".join(str(text).split())
    if speaker is not None:
        lines[i]["speaker"] = dlg.normalise_speaker(speaker)
    return _apply(pj, lines)


def add_line(pj, speaker, text, at=None):
    lines = [dict(li) for li in pj.get("lines", [])]
    new = {"speaker": dlg.normalise_speaker(speaker), "direction": "",
           "text": " ".join(str(text).split())}
    lines.insert(len(lines) if at is None else at, new)
    return _apply(pj, lines)


def delete_line(pj, i):
    lines = [dict(li) for li in pj.get("lines", [])]
    del lines[i]
    return _apply(pj, lines)


def move_line(pj, i, delta):
    lines = [dict(li) for li in pj.get("lines", [])]
    j = max(0, min(len(lines) - 1, i + delta))
    lines.insert(j, lines.pop(i))
    return _apply(pj, lines)


def set_mood(pj, i, mood):
    pj["lines"][i]["mood"] = mood
    return pj


def keep_take(pj, i, k):
    li = pj["lines"][i]
    if 0 <= k < len(li.get("takes") or []):
        li["kept"] = k
    return pj


def take_path(pj, i, k=None):
    li = pj["lines"][i]
    takes = li.get("takes") or []
    k = li.get("kept", 0) if k is None else k
    if not takes or k >= len(takes):
        return None
    p = os.path.join(project_dir(pj), takes[k].get("file", ""))
    return p if os.path.exists(p) else None


def missing(pj):
    """Indexes of lines to make: no take yet, or taken in a voice that has since changed."""
    return [i for i, li in enumerate(pj.get("lines", []))
            if not li.get("takes") or li.get("stale")]


def estimate(pj, idxs):
    voices = {v["slug"]: v for v in dlg.list_voices()}
    eng = pj.get("engine") or dlg.DEFAULT_ENGINE
    view = dict(pj, lines=[pj["lines"][i] for i in idxs])
    designs, _c, _s = _dui.plan_render(view, voices, "all", eng)
    return dlg.estimate_seconds(len(idxs), n_designs=len(designs), engine=eng)


# ================================================================== casting from Characters
def _chars_dir():
    return os.path.join(dlg._data(), "characters")


def _tidy(meta, cdir):
    """A character as casting needs it: `picks`/`bible` as dicts even when saved as the
    TEXT of a dict, and the bible from bible.json when character.json has none (Gustav's
    lives there — casting used to read his description as empty)."""
    import ast
    for k in ("picks", "bible"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip().startswith("{"):
            try:
                v = ast.literal_eval(v.strip())
                if isinstance(v, dict):
                    meta[k] = v
            except Exception:
                pass
    if not (isinstance(meta.get("bible"), dict) and meta["bible"].get("voice")):
        try:
            b = json.load(open(os.path.join(cdir, "bible.json")))
            if isinstance(b, dict):
                meta["bible"] = dict(b, **(meta.get("bible") if isinstance(meta.get("bible"),
                                                                       dict) else {}))
        except Exception:
            pass
    return meta


def list_characters():
    """[(slug, meta)] of saved characters (read-only here)."""
    out = []
    d = _chars_dir()
    try:
        for s in sorted(os.listdir(d)):
            p = os.path.join(d, s, "character.json")
            if os.path.isfile(p):
                try:
                    out.append((s, _tidy(json.load(open(p)), os.path.join(d, s))))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def character_for(speaker):
    """The saved character a script SPEAKER means: full name, first name or slug."""
    spk = dlg.normalise_speaker(speaker)
    for slug, meta in list_characters():
        name = dlg.normalise_speaker(meta.get("name") or slug)
        if spk in (name, dlg.normalise_speaker(slug), name.split(" ")[0]):
            return slug, meta
    return None, None


def _link_character(slug, voice_slug):
    p = os.path.join(_chars_dir(), slug, "character.json")
    try:
        meta = json.load(open(p))
    except Exception:
        return False
    meta["voice_slug"] = voice_slug
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=1)
    os.replace(tmp, p)
    return True


_AGE = (("elderly", 72), ("old ", 70), ("nineties", 90), ("seventies", 75), ("retired", 68),
        ("middle-aged", 48), ("young", 26), ("thirties", 35), ("teen", 17))
_FEMALE = re.compile(r"\b(she|her|woman|lady|girl|grandmother|mother|mom|wife|queen|nun|"
                     r"housewife|governess|cowgirl|heiress|actress|waitress)\b", re.I)


def voice_seed(speaker, meta=None):
    """A voice dict (not saved) for a speaker: from the character's bible voice / subject
    when there is one, else a neutral adult voice to audition from."""
    meta = meta or {}
    bible = meta.get("bible") or {}
    subject = ((meta.get("picks") or {}).get("Subject") or meta.get("note") or
               meta.get("prompt") or "")[:300]
    blob = " ".join([subject, bible.get("who", "")])
    gender = "female" if _FEMALE.search(blob) else "male"
    age = next((a for k, a in _AGE if k in blob.lower()), 40)
    notes = (bible.get("voice") or "").strip().rstrip(".")
    name = meta.get("name") or speaker.title()
    v = dlg.new_voice(name, gender=gender, age_years=age, notes=notes)
    v["characters"] = [dlg.normalise_speaker(speaker)]
    return v


def audition(voice, n=3, progress=None):
    """Three reads of `voice`'s description (each a NEW person: VoiceDesign varies), so you
    pick by ear. -> [wav path...] in a temp folder."""
    tb, _tm = _rig()
    base = dlg.compose_description(voice, "neutral").rstrip(".")
    items = [{"id": "aud:%d" % k, "instruct": base + AUDITION_TWISTS[k % 3] + ".",
              "text": AUDITION_TEXT, "out": "aud_%d.wav" % k} for k in range(n)]
    out_dir = tempfile.mkdtemp(prefix="forge-aud-")
    if progress:
        progress("auditioning %d voices for %s" % (n, voice.get("name")))
    info = tb({"design": items, "clone": [], "voices": {}, "language": "English"}, out_dir,
              engine="qwen")
    got = {it.get("id"): it.get("out") for it in info.get("items", []) if it.get("out")
           and "error" not in it}
    wavs = [got.get("aud:%d" % k) for k in range(n)]
    if not any(wavs):
        raise RuntimeError("no audition came back from the rig")
    return wavs


def keep_audition(voice, wav, speaker, char_slug=None):
    """Keep one audition: it becomes the voice's neutral reference, the voice is saved to the
    bank (bound to the speaker) and linked to the character. -> voice slug."""
    v = json.loads(json.dumps(voice))
    slug = v.get("slug") or dlg.slugify(v.get("name", speaker))
    d = os.path.join(dlg.voices_dir(), slug)
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(wav, os.path.join(d, "neutral.wav"))
    v.setdefault("moods", {})["neutral"] = {"extra": "", "wav": "neutral.wav",
                                            "ref_text": AUDITION_TEXT}
    v["slug"] = slug
    v["source"] = "bank"
    spk = dlg.normalise_speaker(speaker)
    if spk not in v.setdefault("characters", []):
        v["characters"].append(spk)
    if char_slug:
        v["character"] = char_slug
    dlg.save_voice(v)
    if char_slug:
        _link_character(char_slug, slug)
    return slug


def auto_cast(pj):
    """Cast every speaker: the project's own choice, else the character's linked voice, else
    a bank voice bound to that speaker. -> pj (cast filled where possible)."""
    voices = {v["slug"]: v for v in dlg.list_voices()}
    cast = dict(pj.get("cast") or {})
    for spk in dlg.speakers(pj.get("lines", [])):
        if cast.get(spk) in voices:
            continue
        cslug, meta = character_for(spk)
        vs = (meta or {}).get("voice_slug")
        if vs in voices:
            cast[spk] = vs
            continue
        auto = dlg.auto_cast([spk], voices, previous=cast).get(spk)
        if auto:
            cast[spk] = auto
    pj["cast"] = cast
    return pj


def uncast(pj):
    voices = {v["slug"] for v in dlg.list_voices()}
    return [s for s in dlg.speakers(pj.get("lines", []))
            if (pj.get("cast") or {}).get(s) not in voices]


# ================================================================== render
def render(pj, idxs=None, mode="missing", progress=None, submit=None, wait_and_collect=None,
           runs=None, save=True):
    """The old window's render pipeline, for the whole scene (idxs None) or chosen lines (a
    redo: mode "all" on just those). Runs on a WORKER thread. -> summary dict.
    (1) design missing reference samples (qwen), (2) read the lines, (2b) sung lines through
    Song, (3) listen back (ASR score), (4) save."""
    tb, tm = _rig()
    say = progress or (lambda m: None)
    eng = pj.get("engine") or dlg.DEFAULT_ENGINE
    all_v = {v["slug"]: v for v in dlg.list_voices()}
    pdir = project_dir(pj)
    os.makedirs(os.path.join(pdir, "lines"), exist_ok=True)
    if idxs is None:
        view = pj
    else:
        view = dict(pj, lines=[pj["lines"][i] for i in idxs])   # same line dicts
    notes = []
    designs, _c, skipped = _dui.plan_render(view, all_v, mode, eng)
    notes += skipped
    if designs:
        say("designing %d voice sample(s)" % len(designs))
        out_dir = tempfile.mkdtemp(prefix="forge-dlg-")
        try:
            info = tb({"design": designs, "clone": [], "voices": {}, "language": "English"},
                      out_dir, engine="qwen")
            for item in info.get("items", []):
                if "error" in item or not item.get("out"):
                    notes.append("design %s failed" % item.get("id"))
                    continue
                vslug, mood = item["id"].split(":", 1)
                v = all_v.get(vslug)
                if v is None or v.get("source") == "character":
                    continue
                os.makedirs(os.path.join(dlg.voices_dir(), vslug), exist_ok=True)
                shutil.copyfile(item["out"], os.path.join(dlg.voices_dir(), vslug,
                                                          "%s.wav" % mood))
                ref = next((d["text"] for d in designs if d["id"] == item["id"]),
                           dlg.DEFAULT_SAMPLE)
                v.setdefault("moods", {}).setdefault(mood, {}).update(
                    {"wav": "%s.wav" % mood, "ref_text": ref,
                     "extra": dlg.MOOD_WORDS.get(mood, "")})
                dlg.save_voice(v)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
    _d, clones, skipped2 = _dui.plan_render(view, all_v, mode, eng)
    notes += [m for m in skipped2 if m not in notes]
    made = []
    if clones and clones.get("clone"):
        say("reading %d line(s)" % len(clones["clone"]))
        out_dir = tempfile.mkdtemp(prefix="forge-dlg-")
        try:
            info = tb(clones, out_dir, engine=eng)
            by_id = {c["id"]: c for c in clones["clone"]}
            for item in info.get("items", []):
                c = by_id.get(item.get("id"))
                if c is None:
                    continue
                li = view["lines"][c["_line"]]
                if "error" in item or not item.get("out"):
                    li["error"] = item.get("error", "no output")
                    continue
                li.pop("error", None)
                name = "%03d_%d.wav" % (li["idx"], len(li.get("takes", [])) + 1)
                shutil.copyfile(item["out"], os.path.join(pdir, "lines", name))
                li.setdefault("takes", []).append({"file": "lines/%s" % name,
                                                   "seconds": item.get("duration", 0),
                                                   "heard": "", "score": None})
                li["kept"] = len(li["takes"]) - 1
                li.pop("stale", None)               # read in the CURRENT voice now
                made.append(li)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
    sung = _dui.plan_sung(view, all_v, mode)
    if sung:
        if submit and wait_and_collect:
            made += _dui.render_sung(sung, view, pdir, submit, wait_and_collect,
                                     runs or os.path.join(dlg._data(), "runs"), progress=say)
        else:
            notes.append("%d sung line(s) skipped (no Song engine)" % len(sung))
    if made:
        say("listening back to %d take(s)" % len(made))
        try:
            heard = tm([os.path.join(pdir, li["takes"][li["kept"]]["file"]) for li in made])
            for li, text in zip(made, heard):
                tk = li["takes"][li["kept"]]
                tk["heard"] = text
                tk["score"] = round(dlg.score(li["text"], text), 3)
        except Exception as exc:
            notes.append("listen-back failed: %s" % exc)
    if save:
        dlg.save_project(pj)
    flagged = [li["idx"] for li in made
               if (li["takes"][li["kept"]].get("score") or 0) < dlg.PASS_SCORE]
    return {"made": len(made), "flagged": flagged, "notes": notes}


# ================================================================== the scene
def assemble(pj, gap_ms=None):
    """Every line's kept take -> voice fx -> peak-normalised -> scene.wav. -> (path, seconds).
    Raises when a line has no take (the window offers to make it)."""
    voices = {v["slug"]: v for v in dlg.list_voices()}
    pdir = project_dir(pj)
    paths, tmp = [], tempfile.mkdtemp(prefix="forge-scene-")
    try:
        for i, li in enumerate(pj.get("lines", [])):
            src = take_path(pj, i)
            if not src:
                raise ValueError("line %d (%s) has no take yet" % (i + 1, li.get("speaker")))
            v = voices.get((pj.get("cast") or {}).get(dlg.normalise_speaker(li["speaker"])))
            fx = dlg.fx_filter((v or {}).get("fx", {}))
            if fx:
                fxo = os.path.join(tmp, "fx_%03d.wav" % i)
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-af", fx, fxo],
                                   capture_output=True, timeout=120)
                if r.returncode == 0:
                    src = fxo
            out = os.path.join(tmp, "n_%03d.wav" % i)
            dlg.peak_normalise(src, out)
            paths.append(out)
        dest = os.path.join(pdir, "scene.wav")
        secs = dlg.assemble(paths, dest, gap_ms=int(pj.get("gap_ms", 350) if gap_ms is None
                                                      else gap_ms))
        return dest, secs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
