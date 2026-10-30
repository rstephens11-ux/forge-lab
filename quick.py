"""QUICK — one idea in, a finished video out (2026-10-17).

Ryan: "think of how easy higgsfield and the slop cannon are to use" … "should we have a mode
like how it is now and then maybe another option or mode where its more automated … I like the
full customization we have built."

So: STUDIO stays as it is (every window, every knob). QUICK runs the SAME engines in order and
hands everything it makes to Studio — the characters are real Characters, the voices are in
Voices, the script is a Dialogue scene, the video is an Episode — so any step can be opened and
fine-tuned by hand afterwards. Tk-free; quick_ui is the window.

    idea + format ─▶ CAST ─▶ (LOOKS) ─▶ VOICES ─▶ ┊review┊ ─▶ SCRIPT ─▶ ┊review┊ ─▶ LINES
                    ─▶ EPISODE + SET ─▶ SHOTS ─▶ CUT ─▶ <slug>_final.mp4

A job is a JSON file (DATA/quick/<id>.json) with each step's status, so a run that stops — a
busy card, the app closed, "check each step" — resumes where it stopped. Steps already done are
never redone.
"""
import json
import os
import shutil
import random
import re
import time

import dialogue as dlg
import formats
import scene_engine as se
import studio_pipeline as sp
import voicelab as vl
import weirdos
import writers_room
import dispatch as _dsp       # the rig's queue (Tk-free): every render waits its turn

c = sp.c                      # forge_core (the same facade the pipeline renders through)
fl = sp.fl
EST_IMAGE = 45                # seconds — what the QUEUE row quotes for one still
EST_SONG = 240                # ...and for a song

STEPS = ("cast", "looks", "voices", "script", "lines", "episode", "shots", "cut")
# the steps read as a story (review panel, new user: "'The set' and 'Cut' read like
# film-school words; 'Read the lines' sounds like a job for me")
LABELS = {"cast": "Inventing the characters", "looks": "Drawing them from every side",
          "voices": "Giving them voices", "script": "Writing the jokes",
          "lines": "Recording the lines", "episode": "Building the scene",
          "shots": "Filming", "cut": "Editing it together"}
MUSIC_LABELS = dict(LABELS, script="Writing the song", lines="Recording the song")
# a music video moves: each shot gets the next camera move
CAMERA_CYCLE = ("slow push-in toward the face", "medium shot with a gentle handheld sway",
                "low angle, the camera slowly orbits", "tight close-up, focus breathing in",
                "wide shot, the lights pulse with the beat")
MUSIC_SHOT_SECONDS = 5
FINAL_FACTOR = 1.9           # Final (1280x704) vs Draft (960x544): ~1.77x the pixels


def labels(job):
    return MUSIC_LABELS if formats.kind(job.get("format") or "") == "music" else LABELS
# "check each step": stop for a look after these
# ("episode" = the set: "Show me the set first" alone also stops there — every shot starts
# from it, so a wrong face is caught before 20 minutes of video, not after)
REVIEW_AFTER = ("voices", "script", "episode")
# rough wall seconds, measured on the 3090s in the run-through (2026-10-17)
COST = {"invent": 20, "still": 95, "sheet": 200, "voice": 30, "script": 25,
        "line": 10, "set": 150, "shot": 150, "cut": 30}


def jobs_dir():
    d = os.path.join(fl.DATA, "quick")
    os.makedirs(d, exist_ok=True)
    return d


def _path(jid):
    return os.path.join(jobs_dir(), "%s.json" % jid)


# CAPTIONS row: "none" or one of studio_pipeline.CAPTION_LOOKS
CAPTION_CHOICES = ("none", "clean", "pop", "big", "subtitle")


_IDS = set()                      # ids handed out this session (a job isn't saved at once)


def new_job(idea, fmt="sketch", seconds=None, aspect=None, cast=None, check=False,
            looks=False, quality="Draft", sound=None, song=None, lyrics=None, title=None,
            captions=None, check_set=False, pace=None, place=None, hook=True,
            reactions=True, long_takes=True, weirdos=False, music=None, series=None,
            product=None, product_name="", product_what="", hook_angle=None,
            pick_draft=False, authored=None, pictures_only=False):
    quality = quality if quality in ("Draft", "Final") else "Draft"
    """`cast` = saved character slugs to use; missing places are invented."""
    f = formats.get(fmt)
    jid = time.strftime("q%Y%m%d-%H%M%S") + "-%03d" % random.randint(0, 999)
    while os.path.exists(_path(jid)) or jid in _IDS:     # copies made in the same second
        jid = time.strftime("q%Y%m%d-%H%M%S") + "-%03d" % random.randint(0, 999)
    _IDS.add(jid)
    job = {"id": jid, "idea": (idea or "").strip(), "format": f["key"],
           "seconds": int(seconds or f["seconds"]), "aspect": aspect or f["aspect"],
           "seconds_asked": int(seconds) if seconds else None,
           "want_cast": [s for s in (cast or []) if s][:3], "cast": [],
           "check": bool(check), "looks": bool(looks), "quality": quality,
           "check_set": bool(check_set),
           "pace": pace if pace in sp.PACES else formats.pace(fmt),
           "place": place or None,
           "hook": bool(hook), "reactions": bool(reactions), "long_takes": bool(long_takes),
           "weirdos": bool(weirdos), "music": music or None,
           "series": series or None,             # series.py: the show bible it's written from
           # PRODUCT -> AD: a real product's picture; it's put in the presenter's hands on
           # the set, and the script is about IT (name + what it does, honest claims)
           "product": product if product and os.path.exists(product) else None,
           "product_name": (product_name or "").strip()[:80],
           "product_what": (product_what or "").strip()[:300],
           "hook_angle": hook_angle or None,
           # YOU pick the script: stop after writing with the drafts side by side
           "pick_draft": bool(pick_draft), "drafts": None, "draft_pick": None,
           # (None = the format's own look: a gilded satire's small gold subtitles)
           # AUTHORED (2026-10-20): a job that brings its own cast specs, script, acting and
           # hook - Quick's writers are skipped and it only draws, voices, films and cuts
           "authored": authored or None,
           # PICTURES FIRST (2026-10-21): stop before filming - cast stills, the place and the
           # set are made, then the job waits for Ryan's OK (resume: pictures_only False)
           "pictures_only": bool(pictures_only),
           "captions": captions if captions in CAPTION_CHOICES else
           (f.get("captions") if f.get("captions") in CAPTION_CHOICES else "pop"),
           "steps": {s: "todo" for s in STEPS}, "log": [], "created": time.time(),
           "dialogue": None, "episode": None, "final": None, "error": None}
    if (authored or {}).get("where"):
        job["idea_where"] = authored["where"]     # AUTHORED: the set, described as written
    if not job["looks"]:
        job["steps"]["looks"] = "skip"
    if f.get("kind") == "music":
        job["sound"] = sound or f.get("sound")
        job["steps"]["voices"] = "skip"          # it is sung by the Song engine
        job["check"] = bool(check)
        if song:                                  # YOUR song (the Song window's VIDEO button)
            use_song(job, song, lyrics, title)
    save(job)
    return job


def use_song(job, src, lyrics=None, title=None):
    """A music video of a song you already made: no lyrics or song step; the length is the
    song's (up to the slider's 120 s)."""
    import subprocess
    if not src or not os.path.exists(src):
        raise ValueError("that song file is gone: %s" % src)
    d = os.path.join(jobs_dir(), job["id"])
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "song.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-ac", "2", "-ar", "44100",
                    "-t", str(formats.MAX_SECONDS), out], check=True, capture_output=True,
                   timeout=300)
    dur = sp._duration(out) or float(job["seconds"])
    job["song"] = out
    job["lyrics"] = lyrics or ""
    job["title"] = title or os.path.splitext(os.path.basename(src))[0]
    job["seconds"] = int(min(dur, job.get("seconds_asked") or dur, formats.MAX_SECONDS))
    job["steps"]["script"] = job["steps"]["lines"] = "done"
    job["from_song"] = src
    return job


def save(job):
    tmp = _path(job["id"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(job, f, indent=1)
    os.replace(tmp, _path(job["id"]))


def load(jid):
    try:
        return json.load(open(_path(jid)))
    except Exception:
        return None


def list_jobs():
    out = []
    for fn in sorted(os.listdir(jobs_dir()), reverse=True):
        if fn.endswith(".json"):
            j = load(fn[:-5])
            if j:
                out.append(j)
    return out


def estimate(job):
    """Seconds still to go, from the measured COST table."""
    f = formats.get(job["format"])
    n_cast = max(f["cast"], len(job.get("want_cast") or []))
    n_new = max(0, n_cast - len(job.get("want_cast") or []))
    n_lines = writers_room.n_lines(job["seconds"], n_cast,    # ~one line per 3 s now
                                   formats.rhythm(job["format"]))
    if f.get("kind") == "music":
        import song as _song
        n_shots = -(-int(job["seconds"]) // MUSIC_SHOT_SECONDS)
        left = {"cast": n_new * (COST["invent"] + COST["still"]), "looks": n_cast * COST["sheet"],
                "voices": 0, "script": COST["script"],
                "lines": _song.yue2_estimate_seconds(job["seconds"]),
                "episode": COST["set"], "shots": n_shots * COST["shot"], "cut": COST["cut"]}
        if sp.both_cards():
            left["shots"] = left["shots"] / 2.0
        return sum(v for k, v in left.items() if job["steps"].get(k) in ("todo", "running"))
    left = {"cast": n_new * (COST["invent"] + COST["still"]),
            "looks": n_cast * COST["sheet"],
            "voices": n_cast * COST["voice"], "script": COST["script"],
            "lines": n_lines * COST["line"],
            "episode": COST["set"] if n_cast >= 2 or f.get("where") else 0,
            "shots": (n_lines + (n_cast if n_cast >= 2 and job.get("reactions", True)
                                 else 0)) * COST["shot"], "cut": COST["cut"]}
    if job.get("quality") == "Final":
        left["shots"] *= FINAL_FACTOR
    if sp.both_cards():
        left["shots"] = left["shots"] / 2.0            # two at a time, one per card
    return sum(v for k, v in left.items() if job["steps"].get(k) in ("todo", "running"))


def notify(title, text):
    """A Mac notification (the run takes minutes; you may be in another app). Never raises."""
    import subprocess
    import sys
    if sys.platform != "darwin":
        return False
    try:
        subprocess.Popen(["osascript", "-e", 'display notification "%s" with title "%s" '
                          'sound name "Glass"' % (text.replace('"', "'")[:180],
                                                  title.replace('"', "'")[:60])])
        return True
    except Exception:
        return False


def _log(job, msg):
    job["log"].append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
    job["log"] = job["log"][-80:]


# ------------------------------------------------------------------ the steps
def _still_params(spec):
    w, h = fl.char_still_size("full body shot, the whole figure head to feet in frame",
                              "Full")
    return {"prompt": weirdos.compose(spec), "width": w, "height": h,
            "resolution": max(w, h), "steps": 25, "seed": -1, "prefix": "forge_lab_char"}


def _first_image(meta):
    for f_ in (meta or {}).get("images") or []:
        fn = f_.get("file") or ""
        if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return os.path.join(c.RUNS, meta.get("id") or "", fn)
    return None


_VOICE_ITEM = {"type": "object", "required": ["name", "what", "voice", "gender", "age"],
               "properties": {"name": {"type": "string"}, "what": {"type": "string"},
                              "voice": {"type": "string"}, "gender": {"type": "string"},
                              "age": {"type": "integer"}}}
ROLES_SCHEMA = {"type": "object", "properties": {"roles": {"type": "array",
                                                          "items": {"type": "string"}},
                                                "voices": {"type": "array",
                                                           "items": _VOICE_ITEM}},
                "required": ["roles", "voices"]}
MAX_VOICES = 2                  # voice-only speakers a job designs (a doorbell, a car...)


def idea_cast(director, idea, n):
    """(roles, voices) the idea names. ROLES: the people (or creatures) SEEN on screen, as
    "a/an + adjective + job" ("a nervous lighthouse keeper") - each invented character IS one
    (the first real run asked for a lighthouse keeper and got "a fog forecast analyst").
    VOICES (2026-10-24): what SPEAKS but has no body on screen - an AI doorbell, a self-
    driving taxi, a voice on the intercom - [{name, what, voice, gender, age}]. Before this
    the doorbell in "locked out by his AI doorbell" was never cast: the writer had to give
    its lines to the homeowner or invent a person for it. ([], []) when there's no idea or
    the writer is away."""
    if not (idea or "").strip() or n < 1:
        return [], []
    try:
        out = director._chat(
            "You read a one-line video idea and list who is in it.",
            "THE IDEA: %s\n\n"
            "roles: the %d PEOPLE (or creatures with a body) we SEE on screen, as short roles, "
            "each a/an + adjective + job, using the idea's OWN words (\"a nervous lighthouse "
            "keeper\"). If it names fewer than %d, invent the missing ones so they CLASH with "
            "the named ones. Put first the one who behaves most unusually (the odd one), then "
            "the one who reacts most normally (the straight one).\n"
            "voices: anything else in the idea that TALKS but has no body on screen - a "
            "device, an app, an AI, a car, a doorbell, a voice on the phone or an intercom. "
            "For each: name (ONE upper-case word, e.g. DOORBELL), what (what it is, in the "
            "idea's words), voice (how it sounds, in a few plain words, e.g. \"calm, polite "
            "and a little too cheerful\"), gender (male or female) and age (a number). Most "
            "ideas have none: []. Something that talks is NEVER also one of the roles.\n"
            "Return JSON {\"roles\": [...], \"voices\": [...]} only."
            % (idea.strip(), n, n), ROLES_SCHEMA, temperature=0.2)
        roles = [r.strip() for r in (out or {}).get("roles") or [] if isinstance(r, str)
                 and r.strip()]
        voices = []
        for v in (out or {}).get("voices") or []:
            if not isinstance(v, dict):
                continue
            nm = re.sub(r"[^A-Z ]", "", str(v.get("name") or "").upper()).strip()
            nm = " ".join(nm.split()[:2])
            what = str(v.get("what") or "").strip()
            if not nm or not what or nm in [x["name"] for x in voices]:
                continue
            g = str(v.get("gender") or "").strip().lower()
            try:
                age = max(18, min(90, int(v.get("age") or 35)))
            except (TypeError, ValueError):
                age = 35
            voices.append({"name": nm, "what": what[:160],
                           "voice": str(v.get("voice") or "").strip()[:160],
                           "gender": g if g in vl.GENDERS else "female", "age": age})
        return roles[:n], voices[:MAX_VOICES]
    except Exception:
        return [], []


def idea_roles(director, idea, n):
    """The people the idea names, as roles (see idea_cast). [] when the idea names nobody
    or the writer is away."""
    return idea_cast(director, idea, n)[0]


# the idea itself asks for weirdos ("two weirdos argue", "a man with huge ears")
_BODY = (r"ears?|nose|head|hands?|feet|foot|neck|eyebrows?|teeth|tooth|chin|forehead|arms?|"
         r"legs?|eyes?|moustache|mustache|beard|hair|mouth|lips|fingers?|belly|jaw|smile")
WEIRD_RE = re.compile(r"\b(?:weirdos?|freaks?|freakish|grotesque|cartoonish)\b|"
                      r"\b(?:huge|giant|enormous|massive|gigantic|tiny|long|big|massive|"
                      r"oversized|three|no)\s+(?:%s)\b" % _BODY, re.I)


# the idea often says what KIND of video it is ("a weatherman who...", "if Death had a
# vlog") - Quick suggests that format instead of making it a sketch (round-2 plan)
_FORMAT_CUES = (
    ("gilded", r"\bgilded( satire)?\b"),
    ("ifvlog", r"\bif\b.{1,60}\bhad a vlog\b"),
    ("doorbell", r"\b(doorbell|ring cam(era)?|security cam(era)?|cctv|porch cam(era)?|"
                 r"caught on (camera|cam))\b"),
    ("nature", r"\b(nature doc(umentary)?|wildlife doc(umentary)?|attenborough|"
               r"documentary narrator)\b"),
    ("music", r"\b(music video|sings? (a|an|the|about)|song about|a ballad|rap about)\b"),
    ("weather", r"\b(weather ?(man|woman|person|girl|guy|presenter|report|forecast)|"
                r"forecasts?|meteorologist)\b"),
    ("podcast", r"\bpodcast(er|ers|s)?\b"),
    ("interview", r"\b(street interview|vox pop|interviews? (a |an )?(stranger|passer|"
                  r"random|people))\b"),
    ("confessional", r"\b(confessional|reality (tv|show))\b"),
    ("ad", r"\b(commercial|infomercial|advert|ad for|sells? (a|an|the) (new )?product)\b"),
    ("vlog", r"\b(vlog|vlogger|vlogs|youtuber|influencer|talking head|to (the )?camera)\b"),
)


def guess_format(idea):
    """The format the idea itself names, or None ("a weatherman who forecasts feelings" ->
    weather; "if a lighthouse had a vlog" -> ifvlog). First match wins, most specific first."""
    t = (idea or "").lower()
    for key, pat in _FORMAT_CUES:
        if re.search(pat, t):
            return key
    return None


def guess_seconds(idea):
    """A length the idea states ("a 15-second sketch", "45 sec", "one minute") or None."""
    t = (idea or "").lower()
    m = re.search(r"\b(\d{1,3})\s*-?\s*(s|sec|secs|second|seconds)\b", t)
    if m:
        v = int(m.group(1))
        return max(formats.MIN_SECONDS, min(formats.MAX_SECONDS, v))
    if re.search(r"\b(one|a|1)\s*-?\s*minute\b", t):
        return 60
    return None


def wants_weirdos(job):
    """Invent weirdos (one ridiculous feature each) only when asked: the More options box,
    or the idea says so. Otherwise ordinary people (Ryan: "only if specified")."""
    return bool(job.get("weirdos")) or bool(WEIRD_RE.search(job.get("idea") or ""))


def _cast_authored(job, specs, say):
    """AUTHORED cast: each spec (a char_styles / weirdos record) is drawn exactly as written.
    A resumed run keeps the ones already drawn (job["authored_slugs"])."""
    import characters_ui
    import char_styles
    made = dict(job.get("authored_slugs") or {})
    cast = []
    for k, spec in enumerate(specs):
        nm = spec["name"]
        if made.get(nm) and c.read_character(made[nm]):
            cast.append(made[nm])
            continue
        say("drawing %s (%d of %d)" % (nm, k + 1, len(specs)))
        styled = bool(spec.get("style"))
        prompt = char_styles.compose(spec) if styled else weirdos.compose(spec)
        picks = char_styles.picks(spec) if styled else characters_ui.weirdo_picks(spec)
        w, h = fl.char_still_size(picks.get("Framing"), "Full")
        # through the QUEUE (2026-10-22): every render waits for a card like the Make tab does
        meta = _dsp.render("Character — %s" % nm, "t2i",
                           {"prompt": prompt, "width": w, "height": h,
                            "resolution": max(w, h), "steps": 25, "seed": -1,
                            "prefix": "forge_lab_char"}, None,
                           need="image", est_s=EST_IMAGE)
        img = _first_image(meta)
        seed = meta.get("seed", -1)
        if not img:
            raise RuntimeError("no still came back for %s" % nm)
        base = fl.char_slug(nm)
        slug, i = base, 2
        while os.path.exists(fl.char_dir(slug)):
            slug, i = "%s-%d" % (base, i), i + 1
        fl.write_character(slug, {
            "name": nm, "prompt": prompt, "seed": str(seed), "lora": None,
            "lora_strength": "1.0", "picks": picks, "action": spec.get("action", ""),
            "saved": time.strftime("%Y-%m-%d %H:%M"),
            "catchphrase": spec.get("catchphrase", ""), "hook": spec.get("hook", ""),
            "feature": spec.get("feature", ""), "style": spec.get("style"),
            "role": spec.get("subject", ""), "kind": "authored",
            "bible": {k_: spec.get(k_, "") for k_ in ("who", "voice", "pov", "want",
                                                      "blind_spot", "habit")},
            "note": "AUTHORED \u2014 %s" % spec.get("subject", "")}, still_src=img)
        made[nm] = slug
        cast.append(slug)
        job["authored_slugs"] = made
        job["invented"] = list(cast)
        job["cast"] = list(cast)
        save(job)                                  # kept if the next character fails
        _log(job, "drew %s (%s)" % (nm, slug))
    job["cast"] = cast


def step_cast(job, director, say):
    """Use the characters asked for; invent the rest (a Weirdo each, with a still) — each
    invented one plays one of the ROLES in the idea."""
    import characters_ui                       # weirdo_picks — Tk-free at module level
    if (job.get("authored") or {}).get("cast"):
        return _cast_authored(job, job["authored"]["cast"], say)
    f = formats.get(job["format"])
    picked = [s for s in job["want_cast"] if c.read_character(s)]
    # a resumed run (a busy card on character 2) keeps the characters already made
    made = [s for s in job.get("invented") or [] if c.read_character(s) and s not in picked]
    cast = picked + made
    avoid = [(c.read_character(s) or {}).get("feature", "") for s, _n in c.list_characters()]
    need = max(f["cast"], len(picked)) - len(cast)
    voices = job.get("voice_roles")
    if voices is None and f.get("kind") == "music":
        voices = []                            # a song is sung by the cast
    if made and job.get("roles") is not None:
        roles = job["roles"]
    elif need or made or voices is None:
        roles, vs_ = idea_cast(director, job["idea"], max(f["cast"], len(picked)))
        roles = roles[len(picked):]            # the picked characters fill the first roles
        if voices is None:
            voices = vs_
    else:
        roles = []
    job["roles"] = roles
    # VOICE-ONLY speakers (a doorbell, a taxi): never drawn, given a voice in step_voices.
    # A name that clashes with a cast member's first name is dropped (that IS the person).
    firsts = {_speaker(s_) for s_ in cast}
    job["voice_roles"] = [v for v in (voices or []) if v["name"] not in firsts]
    for v in job["voice_roles"]:
        _log(job, "voice only: %s (%s)" % (v["name"], v["what"]))
    job["invented"] = list(made)
    weird = wants_weirdos(job)
    if not weird:
        avoid = [_name(s) for s, _n in c.list_characters()]
    first = None
    if made:
        m0 = c.read_character(made[0]) or {}
        first = {"name": m0.get("name") or made[0], "subject": m0.get("role") or "",
                 "pov": (m0.get("bible") or {}).get("pov", "")}
    for k in range(len(made), len(made) + need):
        say("inventing character %d of %d" % (k + 1, len(made) + need))
        brief = "for this idea: %s" % job["idea"] if job["idea"] else ""
        if k < len(roles):
            brief = ("THEIR ROLE \u2014 the subject, in these words: %s. The video idea: %s."
                     % (roles[k], job["idea"]))
            if weird:
                brief += (" Their gag must be a DIFFERENT body part from everyone already in "
                          "the cast.")
        if first:
            brief += (" THE OTHER PERSON IN THE SCENE: %s, %s. Their POV: %s Your "
                      "character's POV must CLASH with theirs - opposite status, opposite "
                      "instinct." % (first["name"], first["subject"], first.get("pov") or "?"))
        try:
            spec = (weirdos.invent(director, brief, "Anything", avoid) if weird else
                    weirdos.invent_person(director, brief, avoid))
        except Exception as exc:
            _log(job, "writer unavailable (%s) - rolled from the built-in list" % exc)
            spec = None
            for _t in range(8):
                try:
                    spec = weirdos.roll() if weird else weirdos.roll_person()
                    break
                except ValueError:
                    continue
        if k < len(roles) and roles[k]:
            spec["subject"] = roles[k]             # the idea's role, word for word
        avoid.append(spec["feature"] if weird else spec["name"])
        first = first or spec
        say("drawing %s" % spec["name"])
        meta = _dsp.render("Character — %s" % spec["name"], "t2i", _still_params(spec), None,
                           need="image", est_s=EST_IMAGE)
        img = _first_image(meta)
        seed = meta.get("seed", -1)
        if not img:
            raise RuntimeError("no still came back for %s" % spec["name"])
        base = fl.char_slug(spec["name"])
        slug, i = base, 2
        while os.path.exists(fl.char_dir(slug)):
            slug, i = "%s-%d" % (base, i), i + 1
        fl.write_character(slug, {
            "name": spec["name"], "prompt": weirdos.compose(spec), "seed": str(seed),
            "lora": None, "lora_strength": "1.0", "picks": characters_ui.weirdo_picks(spec),
            "action": spec["action"], "saved": time.strftime("%Y-%m-%d %H:%M"),
            "catchphrase": spec["catchphrase"], "hook": spec["hook"],
            "feature": spec["feature"], "flavour": spec.get("flavour"),
            "bible": {"who": spec["who"], "voice": spec["voice"],
                      "pov": spec.get("pov", ""), "want": spec.get("want", ""),
                      "blind_spot": spec.get("blind_spot", ""), "habit": spec.get("habit", "")},
            "role": spec["subject"], "kind": "weirdo" if weird else "person",
            "note": "QUICK — %s" % spec["subject"]}, still_src=img)
        cast.append(slug)
        job["invented"].append(slug)
        job["cast"] = list(cast)
        save(job)                                  # kept if the next character fails
        _log(job, "invented %s (%s)" % (spec["name"], slug))
    job["cast"] = cast


def step_looks(job, say):
    import charpack
    for slug in job["cast"]:
        if charpack.load_sheet(slug):
            continue
        say("drawing %s from every side" % _name(slug))
        charpack.build_sheet(slug, scale="Fast")


def step_voices(job, say):
    for slug in job["cast"]:
        if (c.read_character(slug) or {}).get("voice_slug"):
            continue
        say("finding %s's voice" % _name(slug))
        vl.auto_voice(slug)
    voices = {v["slug"] for v in dlg.list_voices()}
    for v in job.get("voice_roles") or []:
        if v.get("voice_slug") and v["voice_slug"] in voices:
            continue
        say("finding the %s's voice" % v["name"].lower())
        v["voice_slug"] = design_voice(v)
        save(job)                                  # kept if the next voice fails
        _log(job, "designed a voice for %s (%s)" % (v["name"], v["voice_slug"]))


def design_voice(role):
    """A voice-only speaker's voice, designed from its description (VoiceDesign: one read,
    kept, saved in Voices bound to the speaker name) -> voice slug."""
    desc = "; ".join(x for x in (role.get("voice"), "the voice of %s" % role.get("what"))
                     if (x or "").strip())
    v = dlg.new_voice(role["name"].title(), gender=role.get("gender") or "female",
                      age_years=int(role.get("age") or 35), notes=desc[:200])
    v["language"] = "English"
    v["characters"] = [role["name"]]
    vl._mark_new(v)
    wavs = vl.audition(v, 1)
    if not wavs or not wavs[0]:
        raise RuntimeError("no voice came back from the rig for %s" % role["name"])
    return vl.keep(v, wavs[0])


def voice_names(job):
    """The voice-only speakers' script names (DOORBELL, TAXI)."""
    return [v["name"] for v in job.get("voice_roles") or [] if v.get("name")]


def _voice_bio(v):
    return ("VOICE ONLY - %s. Heard, never seen: it talks, but has no body on screen. "
            "Sounds %s." % (v.get("what") or "a voice", v.get("voice") or "natural"))[:300]


def _name(slug):
    return (c.read_character(slug) or {}).get("name") or slug


def _speaker(slug):
    return dlg.normalise_speaker(_name(slug).split(" ")[0])


_ACTION_IN_TEXT = re.compile(r"\*([^*]{2,})\*|\(([^)]{2,})\)|\[([^\]]{2,})\]")


def _spoken(text, direction=""):
    """(text, direction) with any *action*, (action) or [action] written into the spoken text
    moved to the direction - the voice engine reads everything in `text` out loud."""
    t = text or ""
    acts = [next(g for g in m.groups() if g) for m in _ACTION_IN_TEXT.finditer(t)]
    if not acts:
        return t, direction or ""
    t = re.sub(r"\s{2,}", " ", _ACTION_IN_TEXT.sub(" ", t)).strip()
    d = "; ".join(x.strip() for x in [direction or ""] + acts if x and x.strip())
    return (t or text or ""), d


ACTING_SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {
    "type": "object", "properties": {"gesture": {"type": "string"},
                                     "reaction": {"type": "string"},
                                     "sound": {"type": "string"}},
    "required": ["gesture", "reaction"]}}}, "required": ["lines"]}


def stage_directions(director, lines, job=None):
    """One physical beat per line for the shots: what the speaker DOES while saying it and
    how the listener reacts (review panel: everyone stood like statues — the shot prompts
    said "small natural gestures, the camera holds still"). [] when the writer is away."""
    if not lines:
        return []
    vo = set(voice_names(job or {}))
    vo_note = ("\nA speaker marked (VOICE ONLY) is heard, never seen. For ITS lines the "
               "gesture is what the person ON SCREEN does while hearing it, and the reaction "
               "is that same person's face; never give the voice a body.\n" if vo else "")
    script = "\n".join("%d. %s%s: %s" % (i + 1, li.get("speaker", "") +
                                         (" (VOICE ONLY)" if dlg.normalise_speaker(
                                             li.get("speaker", "")) in vo else ""),
                                         (" (%s)" % li["direction"].strip().rstrip("."))
                                         if (li.get("direction") or "").strip() else "",
                                         li.get("text", ""))
                       for i, li in enumerate(lines))
    try:
        out = director._chat(
            "You are a comedy director giving each line of a short sketch ONE piece of "
            "physical acting a camera can see in three seconds.",
            "THE SCRIPT:\n%s\n%s\nFor EACH numbered line give:\n"
            "The writer's own direction is in brackets after a name: when it is an action "
            "(\"stamps his forehead with a rubber stamp\"), THAT is the gesture - keep it.\n"
            "- gesture: what the SPEAKER does while saying it — verb first, no name, specific "
            "and visible (\"jabs a finger at the fog outside the window\", \"clutches a "
            "pigeon to his chest\"). Never \"gestures naturally\".\n"
            "- reaction: what the LISTENER's face or body does in answer — verb first, no "
            "name (\"slowly narrows her eyes\", \"takes one step back\").\n"
            "Play it straight and sincere; the comedy is in how seriously they mean it. The "
            "STRAIGHT character (the reasonable one) stays still and small - a look, a blink, "
            "a slow breath - and the scene gets at most ONE big gesture, saved for the line "
            "that turns it.\n"
            "- sound: ONE short sound effect the line's action makes (\"a knock at the door\", "
            "\"a phone buzzing\", \"a glass clink\"), or \"\" \u2014 most lines get \"\"; at "
            "most two sounds in the whole script, never music or laughter.\n"
            "Return JSON {\"lines\": [{\"gesture\": ..., \"reaction\": ..., \"sound\": "
            "...}, ...]} with exactly %d items, in order." % (script, vo_note, len(lines)),
            ACTING_SCHEMA, temperature=0.6)
        got = [x for x in (out or {}).get("lines") or [] if isinstance(x, dict)]
        return got[:len(lines)] if len(got) >= len(lines) - 1 else []
    except Exception as exc:
        if job is not None:
            _log(job, "no stage directions (%s) - shots keep the generic acting" % exc)
        return []


HOOK_SCHEMA = {"type": "object", "required": ["hook_text"],
               "properties": {"hook_text": {"type": "string"}}}


def hook_text(director, lines, idea="", job=None):
    """The on-screen HOOK for the first 2.5 s — what makes a scroller stop (review panel,
    short-form editor: both runs opened on a static wide with no text). Seven words or
    fewer, never the punchline. "" when the writer is away."""
    if not lines:
        return ""
    try:
        out = director._chat(
            "You write the on-screen hook text for short comedy videos on TikTok and Reels.",
            "THE IDEA: %s\n\nTHE SCRIPT:\n%s\n\nWrite the text that sits at the top of "
            "the screen for the first two seconds, so someone scrolling past stops. SEVEN "
            "WORDS OR FEWER. It is the PREMISE - who meets whom, or what is at stake - in "
            "plain words (\"retired zoning inspector meets his neighbour\", \"POV: your "
            "lighthouse has a pigeon problem\"). It must NOT contain any joke from the "
            "script or reuse its punchline words - the video tells the jokes. No hashtags, "
            "no emoji. Return JSON "
            "{\"hook_text\": ...}." % (idea or "(open)", writers_room.numbered(lines)),
            HOOK_SCHEMA, temperature=0.8) or {}
        t = re.sub(r"[#\\{}]", "", str(out.get("hook_text") or "")).strip().strip('"')
        words = t.split()
        return " ".join(words[:7])
    except Exception as exc:
        if job is not None:
            _log(job, "no hook text (%s)" % exc)
        return ""


JUDGE_SCHEMA = {"type": "object", "properties": {"best": {"type": "integer"},
                                                 "why": {"type": "string"}},
                "required": ["best", "why"]}
DRAFTS = 2


def _draft_text(d):
    return "\n".join("%s: %s" % (li.get("speaker", ""), li.get("text", ""))
                     for li in (d or {}).get("lines") or [])


def _best_draft(director, brief, cast, job, say):
    """Write DRAFTS scripts and let the writer, as a tough comedy editor, keep the one that
    stays on the idea and lands its last line. The first real run's single draft was eight
    unconnected one-liners. Any failure: the first draft that came back."""
    drafts = []
    for k in range(DRAFTS):
        say("writing the script" + (" (draft %d of %d)" % (k + 1, DRAFTS) if DRAFTS > 1 else ""))
        try:
            d = director.dialogue(brief, cast, seconds=job["seconds"])
            if d and d.get("lines"):
                drafts.append(d)
        except Exception as exc:
            if not drafts and k == DRAFTS - 1:
                raise
            _log(job, "draft %d failed: %s" % (k + 1, exc))
    if not drafts:
        raise RuntimeError("the writer came back with no lines")
    if len(drafts) < 2:
        return drafts[0]
    say("picking the funnier draft")
    try:
        ask = ("You are a tough comedy editor choosing which script to shoot.\n\n%s\n\n"
               % brief + "\n\n".join("DRAFT %d:\n%s" % (i + 1, _draft_text(d))
                                      for i, d in enumerate(drafts)) +
               "\n\nPick the draft that (1) stays on THE IDEA's one situation, every line "
               "answering the one before; (2) escalates; (3) ends on the strongest last line. "
               "Return JSON {\"best\": draft number, \"why\": one sentence}.")
        v = director._chat("You judge short comedy scripts. Be decisive.", ask, JUDGE_SCHEMA,
                           temperature=0.2)
        i = int((v or {}).get("best") or 1) - 1
        if 0 <= i < len(drafts):
            _log(job, "kept draft %d of %d: %s" % (i + 1, len(drafts), (v or {}).get("why")))
            return drafts[i]
    except Exception as exc:
        _log(job, "couldn't judge the drafts (%s) - kept the first" % exc)
    return drafts[0]


def cast_bio(slug, role=None):
    """What the writer sees about a character: role, POV, want, blind spot, habit and the
    look — the comedy writer's block. No catchphrase (both real runs pasted it in, off the
    topic); older characters without the new fields fall back to their bible."""
    meta = dict(se.list_characters()).get(slug) or {}
    bible = meta.get("bible") if isinstance(meta.get("bible"), dict) else {}
    role = role or meta.get("role") or (meta.get("note") or "").replace("QUICK \u2014 ", "")
    bits = ["%s." % (role or "a character")]
    for k, lab in (("pov", "POV"), ("want", "WANTS"), ("blind_spot", "BLIND SPOT"),
                   ("habit", "HABIT")):
        v = (bible.get(k) or meta.get(k) or "").strip()
        if v:
            bits.append("%s: %s" % (lab, v.rstrip(".") + "."))
    if len(bits) == 1 and bible.get("who"):
        bits.append(bible["who"])
    if meta.get("feature"):
        bits.append("Looks: %s (the picture shows this; mention it at most once, only if it "
                    "lands)." % meta["feature"])
    return " ".join(bits)[:500]


WHERE_SCHEMA = {"type": "object", "properties": {"where": {"type": "string"}},
                "required": ["where"]}


def idea_where(director, idea):
    """The place THE IDEA names ("on a city street" -> "a busy city street on a grey
    afternoon"), for the set - or "" when it names none. The city-street run was shot in
    an indoor gallery: nothing read the place out of the idea."""
    if not (idea or "").strip():
        return ""
    try:
        out = director._chat(
            "You read a one-line video idea and say where it happens.",
            "THE IDEA: %s\n\nIf the idea names or clearly implies a place, describe it as one "
            "concrete film location, 6-14 words, starting with a/an/the, using the idea's own "
            "words (\"on a city street\" -> \"a busy city sidewalk on a grey afternoon\"). If it "
            "names no place, return an empty string. Return JSON {\"where\": ...} only."
            % idea.strip(), WHERE_SCHEMA, temperature=0.2)
        w = re.sub(r"\s+", " ", str((out or {}).get("where") or "")).strip().rstrip(".")
        return w[:140]
    except Exception:
        return ""


class NeedPick(Exception):
    """The drafts are written; the job waits for you to pick one (state review:drafts)."""


def pick_draft(job, k):
    """Use draft k (0-based); run() then carries on from the script."""
    ds = job.get("drafts") or []
    if not (isinstance(k, int) and 0 <= k < len(ds)):
        raise ValueError("there's no draft %s" % k)
    job["draft_pick"] = k
    save(job)
    return job


def more_drafts(job):
    """Throw these drafts away; the next run writes new ones."""
    job["drafts"], job["draft_pick"] = None, None
    job["steps"]["script"] = "todo"
    save(job)
    return job


def step_script(job, director, say):
    say("writing the script")
    if "idea_where" not in job and not job.get("place"):
        job["idea_where"] = idea_where(director, job.get("idea"))
    roles = dict(zip(job.get("invented") or [], job.get("roles") or []))
    cast = {_speaker(slug): cast_bio(slug, roles.get(slug)) for slug in job["cast"]}
    for v in job.get("voice_roles") or []:      # heard, never seen (a doorbell, a taxi)
        cast.setdefault(v["name"], _voice_bio(v))
    f = formats.get(job["format"])
    rhythm = formats.rhythm(job["format"])      # a gilded satire: many short lines
    extra = ""
    if job.get("series"):
        import series as _series                # the show bible: tone, rules, gags, past eps
        extra += _series.brief(_series.load(job["series"]))
    extra += product_brief(job)
    extra += taste_notes(job.get("format"))       # his star ratings: what landed, what didn't
    # the one-shot writer (the last fallback) keeps the long brief; the room writes PLAN
    # FIRST from the format's shape + THE IDEA (the bake-off's winning prompt, 2026-10-24)
    brief = formats.brief(job["format"], job["idea"]) + length_rule(job["seconds"], len(cast),
                                                                    rhythm) + extra
    room_brief = formats.writer_brief(job["format"], job["idea"]) + extra
    out = None
    picked = False
    au = job.get("authored") or {}
    if au.get("script"):                          # AUTHORED: the script as written
        out = {"title": au["script"].get("title") or job["idea"][:40],
               "lines": [dict(li) for li in au["script"]["lines"]]}
        job["room"] = {"authored": True}
        _log(job, "authored script: %d lines" % len(out["lines"]))
        picked = True
    if out is None and job.get("pick_draft") and formats.kind(job["format"]) != "music":
        ds = job.get("drafts") or []
        k = job.get("draft_pick")
        if isinstance(k, int) and 0 <= k < len(ds):
            out = {"title": ds[k].get("title"), "lines": [dict(li) for li in ds[k]["lines"]]}
            job["room"] = dict(job.get("room") or {}, picked=k + 1, of=len(ds))
            _log(job, "you picked draft %d of %d" % (k + 1, len(ds)))
            picked = True
        else:
            try:
                game, ds = writers_room.drafts_for_pick(
                    director, room_brief, cast, job["seconds"], idea=job["idea"], shape=f["shape"],
                    n=DRAFTS + 1, say=say, log=lambda m: _log(job, m), rhythm=rhythm)
            except Exception as exc:
                _log(job, "couldn't write drafts to pick from (%s) - the room picks" % exc)
                ds = None
            if ds:
                job["drafts"], job["draft_pick"] = ds, None
                job["room"] = {"game": game}
                raise NeedPick()
    if out is None and ROOM:
        try:
            out = writers_room.write(director, room_brief, cast, job["seconds"], idea=job["idea"],
                                     shape=f["shape"], format_name=f["name"], drafts=DRAFTS + 1,
                                     say=say, log=lambda m: _log(job, m), rhythm=rhythm)
            job["room"] = out.get("room")
        except Exception as exc:
            _log(job, "the writers' room couldn't write it (%s) - one-shot writer" % exc)
            out = None
    if out is None:
        out = _best_draft(director, brief, cast, job, say)
    budget = writers_room.budget(job["seconds"], len(cast), rhythm)
    n_words = sum(len((li.get("text") or "").split()) for li in out.get("lines") or [])
    if n_words > budget * 1.25 and not picked:     # (you chose it knowing its length)
        say("tightening the script (%d words for %d s)" % (n_words, job["seconds"]))
        _log(job, "script too long: %d words, budget %d - asked for a cut" % (n_words, budget))
        try:
            cut_ = director.dialogue(
                brief + "\n\nA first draft ran %d words \u2014 far too long. Cut it to about "
                "%d words: fewer, shorter lines, keep the best joke and the button."
                % (n_words, budget), cast, seconds=job["seconds"])
            if cut_ and len(cut_.get("lines") or []) >= 2:
                out = cut_                       # an empty / broken cut keeps the draft
        except Exception as exc:
            _log(job, "the cut failed (%s) - keeping the long draft" % exc)
    # a line never starts with its speaker's name (a punch-up came back "Elena: Returned."
    # and the label was spoken and captioned - test run 2026-10-18)
    _names = set(cast) | {n.split()[0] for n in cast if n}
    for li in out.get("lines") or []:
        li["text"] = writers_room.strip_speaker(li.get("text"), _names)
    import director as _d
    slug = "quick-%s" % job["id"][1:]
    pj = dlg.new_project(slug, "")
    pj["slug"] = slug
    pj["name"] = out.get("title") or job["idea"][:40] or slug
    pj["engine"] = dlg.DEFAULT_ENGINE
    for li in out.get("lines") or []:          # a sound or action never reaches the voice
        li["text"], li["direction"] = _spoken(li.get("text"), li.get("direction"))
    say("directing the acting")
    if len(au.get("acting") or []) == len(out.get("lines") or []):
        job["acting"] = [dict(a) for a in au["acting"]]          # AUTHORED acting
    else:
        job["acting"] = stage_directions(director, out.get("lines") or [], job)
    if job.get("hook", True):
        job["hook_text"] = (au.get("hook_text") or
                            hook_text(director, out.get("lines") or [], job.get("idea"), job))
    pj = dlg.reparse(pj, _d.format_dialogue(out.get("lines") or []))
    for v in job.get("voice_roles") or []:      # the voice designed for it in step_voices
        if v.get("voice_slug"):
            pj.setdefault("cast", {})[v["name"]] = v["voice_slug"]
    pj = se.auto_cast(pj)
    dlg.save_project(pj)
    job["dialogue"] = slug
    job["title"] = pj["name"]
    _log(job, "script: %s (%d lines)" % (pj["name"], len(pj.get("lines") or [])))


# ------------------------------------------------------------------ music video
def _music_style(job):
    import song as _song
    found = _song.find_sound(job.get("sound") or "") or _song.find_sound(
        formats.get("music")["sound"])
    _fam, text, vocal = found
    meta = dict(se.list_characters()).get(job["cast"][0]) or {}
    gender = se.voice_seed(_name(job["cast"][0]), meta).get("gender", "male")
    if len(job["cast"]) > 1:
        gender = "duet"
    return _song.with_voice(text, vocal, gender)


def step_lyrics(job, director, say):
    say("writing the song")
    bits = []
    for slug in job["cast"]:
        meta = dict(se.list_characters()).get(slug) or {}
        bible = meta.get("bible") if isinstance(meta.get("bible"), dict) else {}
        bits.append("%s: %s %s" % (_name(slug), bible.get("who") or "",
                                   ("Their one absurd thing: %s." % meta["feature"])
                                   if meta.get("feature") else ""))
    brief = "%s\n\nSUNG BY: %s\n\nTHE IDEA: %s" % (
        formats.get("music")["shape"], " / ".join(bits),
        job["idea"] or "whatever this character would sing about")
    out = director.lyrics(brief, style=_music_style(job), seconds=job["seconds"])
    job["lyrics"] = out.get("lyrics") or ""
    job["title"] = out.get("title") or "Untitled song"
    _log(job, "lyrics: %s" % job["title"])


def step_song(job, say):
    import song as _song
    say("making the song (%s)" % (job.get("sound") or "the format's sound"))
    p = _song.yue2_plan_params(_music_style(job), job["lyrics"], seconds=job["seconds"],
                               prefix="songs/quick")
    meta = _dsp.render("Song — %s" % (job.get("title") or "the song"), "song", p, None,
                       need="song", est_s=EST_SONG) or {}
    files = [x for x in (meta.get("images") or [])
             if (x.get("file") or "").lower().endswith((".flac", ".wav", ".mp3"))]
    if not files:
        raise RuntimeError("the song engine finished but no audio came back")
    src = os.path.join(c.RUNS, meta.get("id") or "", files[0]["file"])
    d = os.path.join(jobs_dir(), job["id"])
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "song.wav")
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-ac", "2", "-ar", "44100",
                    "-t", str(int(job["seconds"]) + 2), out], check=True, capture_output=True,
                   timeout=300)
    job["song"] = out
    _log(job, "song made: %s" % out)


def music_slices(total, shot=MUSIC_SHOT_SECONDS, beats_info=None):
    """[(start, length, shot_seconds)] covering `total` seconds of song. Each slice is EXACTLY
    the length H3 renders a shot of `shot` seconds (n*17+5 frames at 24 fps), so the sung audio
    lines up with the picture shot after shot instead of drifting.
    With `beats_info` (beats.detect) the cuts land ON the music instead: each slice is as
    many whole bars as fit in a shot, and its shot is the shortest H3 length that covers it
    (the edit trims the clip to the slice, so nothing drifts)."""
    out, t = [], 0.0
    full = c.h3_frames(shot) / 24.0
    if beats_info and beats_info.get("beats"):
        import beats as _beats
        for st_, ln in _beats.cut_points(total, beats_info, max_len=full):
            s_ = next((v for v in sp.VALID_SHOT_SECONDS if c.h3_frames(v) / 24.0 >= ln - 1e-3),
                      shot)
            out.append((st_, ln, s_))
        return out
    while total - t > 0.25:
        left = total - t
        if left >= full:
            out.append((round(t, 3), full, shot))
            t += full
        else:
            s_ = next((v for v in sp.VALID_SHOT_SECONDS if c.h3_frames(v) / 24.0 >= left),
                      sp.VALID_SHOT_SECONDS[-1])
            out.append((round(t, 3), left, s_))
            t = total
    return out


def _music_episode(job, say):
    import subprocess
    f = formats.get(job["format"])
    song_len = sp._duration(job["song"]) or float(job["seconds"])
    total = min(song_len, float(job["seconds"]))
    ep = sp.new_project(job["cast"], job["idea"] or job.get("title") or "music video",
                        length_s=int(round(total)), quality=job["quality"],
                        title=job.get("title"), aspect=job["aspect"])
    ep["where"] = f.get("where") or ""
    ep["captions"] = False
    ep["music_video"] = True
    sdir = os.path.join(sp.project_dir(ep["slug"]), "song")
    os.makedirs(sdir, exist_ok=True)
    shots = []
    binfo = None
    if job.get("beat_sync", True):
        try:
            import beats as _beats
            binfo = _beats.detect(job["song"])
            if binfo.get("confidence", 0) < 0.25:   # (plain noise reads ~0.16)
                binfo = None
            else:
                _log(job, "beat: %s bpm (confidence %.2f) - cuts land on the bar"
                     % (binfo["bpm"], binfo["confidence"]))
        except Exception as exc:
            _log(job, "no beat found (%s) - plain cuts" % exc)
            binfo = None
    for i, (start, length, secs) in enumerate(music_slices(total, beats_info=binfo)):
        slug = job["cast"][i % len(job["cast"])]
        wav = os.path.join(sdir, "slice_%02d.wav" % (i + 1))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "%.3f" % start, "-t",
                        "%.3f" % length, "-i", job["song"], wav], check=True,
                       capture_output=True, timeout=120)
        act = ("%s sings this part of the song with real feeling, performing to the camera, "
               "lips moving exactly with the vocals; %s. Music-video lighting. The same face, "
               "hair and clothes as in the first frame."
               % (_name(slug), CAMERA_CYCLE[i % len(CAMERA_CYCLE)]))
        shots.append(sp._shot_migrate({
            "n": i + 1, "seconds": secs, "intent": "lipsync", "engine": "h3",
            "video_mode": "set", "speaker": slug, "action": act, "prompt": act, "line": "",
            "audio_wav": wav, "words": None, "status": "new", "voice_from": "song",
            "slice_len": round(length, 3)}))
    ep["shots"] = shots
    ep["source"] = {"quick": job["id"], "song": job["song"]}
    sp.save_project(ep)
    job["episode"] = ep["slug"]
    _log(job, "music video: %d shots over %.1f s" % (len(shots), total))


def ideas(director, fmt, cast=(), n=3, seconds=None):
    """n one-line ideas for this format and cast (the slop-cannon "surprise me").
    -> ["logline", ...]"""
    f = formats.get(fmt)
    who = []
    for slug in cast or []:
        meta = dict(se.list_characters()).get(slug) or {}
        who.append("%s%s" % (meta.get("name") or slug, (" (%s)" % meta["feature"])
                             if meta.get("feature") else ""))
    lead = (dict(se.list_characters()).get(cast[0]) if cast else None) or {
        "name": "a new character", "note": "invented for this video"}
    brief = ("FORMAT: %s \u2014 %s\nCAST: %s\nEach logline must work as THIS format, played "
             "completely straight, and be shareable." % (
                 f["name"], f["shape"], ", ".join(who) or "new characters, invented for it"))
    try:
        if hasattr(director, "load"):
            director.load()
    except Exception:
        pass
    out = director.pitch(lead, n=n, length_s=int(seconds or f["seconds"]), brief=brief)
    if isinstance(out, dict):
        out = out.get("pitches") or out.get("items") or []
    return [(p.get("logline") or p.get("title") or "").strip() for p in out
            if isinstance(p, dict)][:n]


WORDS_PER_SECOND = 2.4          # spoken comedy pace, measured on the 15 s run-through


def length_rule(seconds, cast=2, rhythm=None):
    """A WORD budget holds the length (the first run asked 15 s, got 34 s), in about one line
    per 3 s with uneven lengths (writers_room: setups need room before their payoff)."""
    return writers_room.length_rule(seconds, cast, rhythm)


ROOM = True                 # the writers' room (3 plan-first drafts -> scored -> punch-up)


def script_text(job):
    if formats.kind(job.get("format") or "") == "music":
        return job.get("lyrics") or ""
    pj = dlg.load_project(job["dialogue"]) if job.get("dialogue") else None
    return se.script_from_lines(pj["lines"]) if pj else ""


def set_script(job, text):
    """The script edited at the review: untouched lines keep their takes (dialogue.reparse)."""
    if formats.kind(job.get("format") or "") == "music":
        job["lyrics"] = text.strip()
        save(job)
        return
    pj = dlg.load_project(job["dialogue"])
    before = [li.get("text") for li in pj.get("lines") or []]
    pj = se.auto_cast(dlg.reparse(pj, text))
    dlg.save_project(pj)
    after = [li.get("text") for li in pj.get("lines") or []]
    if job.get("acting") and len(after) != len(before):
        # lines added or cut: the acting no longer lines up — keep it only for lines that
        # are still where they were
        keep = [a for a, b0 in zip(job["acting"], before) if b0 in after]
        job["acting"] = keep if len(keep) == len(after) else []
        save(job)


def step_lines(job, say):
    pj = se.auto_cast(dlg.load_project(job["dialogue"]))
    miss = se.missing(pj)
    if miss:
        say("reading %d lines" % len(miss))
        se.render(pj, miss, "all", progress=say, submit=c.submit,
                  wait_and_collect=c.wait_and_collect, runs=c.RUNS)
    pj = dlg.load_project(job["dialogue"])
    left = se.missing(pj)
    if left:
        raise RuntimeError("%d line(s) couldn't be read — open the scene in Dialogue"
                           % len(left))


def _authored_place(job, say):
    """AUTHORED place: the empty room is made on its own FIRST (machines, grime and all) and
    kept in Places; the set then puts the people INTO that exact picture - described in one
    prompt with the cast, the image model drew the people and dropped the room's machines
    (Reskilling Day, 2026-10-20)."""
    pl = (job.get("authored") or {}).get("place") or {}
    if not pl.get("text") or job.get("place"):
        return
    import places as _pl
    say("building the place")
    img = _pl.options(pl["text"], aspect=job["aspect"], n=1)[0]
    job["place"] = _pl.keep(pl.get("name") or pl["text"][:40], pl["text"], img,
                            aspect=job["aspect"])
    save(job)
    _log(job, "made the place %s" % job["place"])


def step_episode(job, say):
    f = formats.get(job["format"])
    if not job.get("episode"):
        _authored_place(job, say)
    if not job.get("episode") and f.get("kind") == "music":
        _music_episode(job, say)
    if not job.get("episode"):
        ep = sp.from_dialogue(job["dialogue"], quality=job["quality"],
                              title=job.get("title"), aspect=job["aspect"],
                              cast={_speaker(s): s for s in job["cast"]},
                              voices={v["name"]: v.get("what") or ""
                                      for v in job.get("voice_roles") or []})
        ep["pace"] = job.get("pace") or formats.pace(job["format"])
        if f.get("look"):
            ep["post_look"] = f["look"]             # e.g. the doorbell camera picture
        if f.get("stamp"):
            ep["stamp_text"] = f["stamp"]           # "FRONT DOOR ● REC", top-left
        if f.get("end_fade"):
            ep["end_fade"] = float(f["end_fade"])   # the last shot fades to black
        if (job.get("authored") or {}).get("style"):
            ep["style"] = job["authored"]["style"]  # AUTHORED: the look every shot asks for
        if (job.get("authored") or {}).get("lens"):
            ep["lens"] = job["authored"]["lens"]    # AUTHORED: the close-ups' lens
        if job.get("hook", True) and job.get("hook_text"):
            ep["hook_text"] = job["hook_text"]      # on screen for the first 2.5 s
        if job.get("place"):
            ep["place"] = job["place"]              # the set is built IN this kept place
        if job.get("music"):
            import music as _music                  # a song from the MUSIC library, ducked
            if job["music"] == "auto":              # "Pick one for me": by the format's mood
                job["music_picked"] = _music.pick_for(job.get("idea"), job["format"])
            ep["music"] = _music.path(job.get("music_picked") if job["music"] == "auto"
                                      else job["music"])  # under the voices
        # made for scrolling with the sound off: on unless you picked None
        ep["captions"] = job.get("captions", "pop") != "none"
        if ep["captions"]:
            ep["caption_look"] = job.get("captions", "pop")
        if not ep.get("where") and (job.get("idea_where") or f.get("where")):
            # the place the IDEA names wins over the format's usual one
            ep["where"] = job.get("idea_where") or f["where"]
        if len(job["cast"]) == 1 and (ep.get("where") or job.get("place")):
            for sh in ep["shots"]:                  # one character: a set still keeps the place
                sh["video_mode"] = "set"
        if len(job["cast"]) > 1:
            sp.vary_sizes(ep)                       # one line back on the wide, mid-scene
        if job.get("acting"):
            sp.apply_acting(ep, job["acting"])      # a gesture + a reaction in every shot
            for sh_, a_ in zip(sp.line_shots(ep), job["acting"]):
                cue = (a_.get("sound") or "").strip() if isinstance(a_, dict) else ""
                if cue:
                    sh_["sfx"] = cue[:80]           # laid in under the line at the cut
        if job.get("long_takes", True):
            sp.merge_long_takes(ep)                 # one person: lines -> ~15 s takes
        if job.get("reactions", True):
            sp.add_reactions(ep)                    # the listener's face after the button
        sp.save_project(ep)
        job["episode"] = ep["slug"]
    ep = sp.load_project(job["episode"])
    only = job.get("set_only")                  # the set check's "rebuild" / "redo X"
    if any(sh.get("video_mode") == "set" for sh in ep["shots"]) and (
            only or not sp.set_ready(ep)):
        say("building the set" if only in (None, "master") else "redoing a close-up")
        sp.make_set(ep, progress=say, only=only, resume=not only)
    if job.get("product"):
        product_pass(job, sp.load_project(job["episode"]), say)
    job.pop("set_only", None)                   # only once it worked (a failed redo retries)


HOOK_ANGLES = ("a problem everyone watching has", "a surprising claim about the product",
               "a question the viewer can't help answering")


def product_brief(job):
    """The writer's brief for a REAL product (Product -> ad): what it is and does, honest."""
    if not (job.get("product") or job.get("product_name")):
        return ""
    name = job.get("product_name") or "the product in the picture"
    out = ("\n\nTHIS TIME THE PRODUCT IS REAL (ignore 'invented product' above): %s.%s The ad "
           "is for THIS product: say its name, show what it does, and keep every claim "
           "honest \u2014 no invented prices, awards, studies, medical or safety claims. The "
           "comedy comes from the presenter, never from lying about the product. The presenter "
           "is holding it." % (name, (" What it does: %s." % job["product_what"])
                               if job.get("product_what") else ""))
    if job.get("hook_angle"):
        out += " Open with a hook that is %s." % job["hook_angle"]
    return out + "\n"


def product_pass(job, ep, say=None, submit=None):
    """Put the product in the presenter's hands on every set plate (the wide and each
    close-up): an edit of [plate, product photo]. Done once per set + product."""
    prod = job.get("product")
    s_ = ep.get("set") or {}
    if not prod or not os.path.exists(prod) or not s_.get("master"):
        return False
    # per plate: a plate already carrying THIS product (its file unchanged since) is left
    # alone; a plate rebuilt since (redo a close-up, rebuild the set) gets it again
    done = dict(s_.get("product_plates") or {}) if s_.get("product") == prod else {}
    submit = submit or c.submit
    lead = job["cast"][0] if job.get("cast") else None
    who = _name(lead) if lead else "the presenter"
    name = job.get("product_name") or "the product"
    w, h = sp.set_size(ep)
    plates = [("master", s_["master"])] + sorted((s_.get("singles") or {}).items())
    n = 0
    for key, plate in plates:
        if not plate or not os.path.exists(plate):
            continue
        if done.get(key) == os.path.getmtime(plate):
            continue
        if say:
            say("putting %s in %s's hands" % (name, who))
        prompt = ("<image1> exactly \u2014 the same people, faces, hair, clothes, place, "
                  "framing and light \u2014 and now %s holds %s from <image2> naturally in one "
                  "hand, at a natural size, clearly visible. The product exactly as in "
                  "<image2>: its shape, colours, label and text unchanged. Nobody else changes."
                  % (who, name))
        p = {"prompt": prompt, "steps": sp.SET_STEPS, "seed": -1,
             "prefix": "forge_lab_product", "custom_size": True, "width": w, "height": h}
        pid, _ = submit("edit", p, [plate, prod])
        shutil.copyfile(sp._plate_from(pid), plate)
        done[key] = os.path.getmtime(plate)
        n += 1
        s_["product"], s_["product_plates"] = prod, done
        ep["set"] = s_
        sp.save_project(ep)                       # each plate saved as it's done
    if n:
        _log(job, "product in hand on %d plate(s)" % n)
    return bool(n)


def has_set(job):
    ep = sp.load_project(job.get("episode") or "") if job.get("episode") else None
    return bool(ep and (ep.get("set") or {}).get("master"))


def redo_set(job, only="master"):
    """The set check: rebuild the whole set ("master") or one person's close-up (a slug),
    then stop to look again."""
    job["set_only"] = only or "master"
    job["steps"]["episode"] = "todo"
    job["state"] = "stopped"
    save(job)
    return job


def step_shots(job, say, stop=None, director=None):
    ep = sp.load_project(job["episode"])
    todo = [sh["n"] for sh in ep["shots"] if not sh.get("chosen")]
    errs = sp.render_many(ep, todo, stop=stop, progress=say)
    if stop and stop() and not errs:
        raise InterruptedError("stopped")
    if errs:
        raise errs[0]
    ep = sp.load_project(job["episode"])
    bad = [sh["n"] for sh in ep["shots"] if not sh.get("chosen")]
    if bad:
        raise RuntimeError("shot%s %s failed — open the episode to make another take"
                           % ("s" if len(bad) > 1 else "", ", ".join(map(str, bad))))
    if job.get("check_takes", True):
        say("checking the takes")
        if director is not None and hasattr(director, "load"):
            try:
                director.load()        # asks the card manager for room first (frees idle)
            except Exception as exc:
                _log(job, "take check without the vision read (%s)" % exc)
                director = None
        job["flagged"] = sp.qc_shots(ep, director, progress=say)


def flagged(job):
    """Shots the take check flagged that Ryan hasn't okayed: [(n, reasons, frame)]."""
    if not job.get("episode"):
        return []
    try:
        ep = sp.load_project(job["episode"])
    except Exception:
        return []
    return [(sh["n"], list((sh.get("qc") or {}).get("flags") or []),
             (sh.get("qc") or {}).get("frame"))
            for sh in ep.get("shots") or []
            if (sh.get("qc") or {}).get("flags") and not sh["qc"].get("ok_by_you")]


def ok_shot(job, n):
    """"It's fine": the flag is dismissed for this take."""
    ep = sp.load_project(job["episode"])
    sh = sp._shot(ep, n)
    if sh.get("qc"):
        sh["qc"]["ok_by_you"] = True
    sp.save_project(ep)
    job["flagged"] = [x for x in job.get("flagged") or [] if x != n]
    save(job)


def reroll(job, ns):
    """Reroll the shots Ryan picked: each gets a new take (the old ones stay in Takes),
    then the video is re-cut. Run the job again to do it."""
    ep = sp.load_project(job["episode"])
    for n in ns:
        sh = sp._shot(ep, n)
        sh["chosen"], sh["status"], sh["qc"] = None, "new", None
    sp.save_project(ep)
    job["steps"]["shots"] = "todo"
    job["steps"]["cut"] = "todo"
    job["state"] = "stopped"
    job["flagged"] = [x for x in job.get("flagged") or [] if x not in ns]
    _log(job, "reroll shots %s" % ", ".join(map(str, ns)))
    save(job)
    return job


def recut(job, captions=None, pace=None, hook=None):
    """CHANGE IT -> Re-cut: the same shots, cut again with other captions / pace / hook
    (local ffmpeg, no new renders). Run the job again to do it. The old video is kept
    beside the new one (the cut is written fresh each time)."""
    ep = sp.load_project(job["episode"])
    job["recut_prev"] = {"captions": job.get("captions", "pop"), "pace": job.get("pace"),
                         "hook": job.get("hook", True)}
    if captions is not None:
        job["captions"] = captions
        ep["captions"] = captions != "none"
        if ep["captions"]:
            ep["caption_look"] = captions
    if pace is not None:
        job["pace"] = ep["pace"] = pace
    if hook is not None:
        job["hook"] = bool(hook)
        if hook and job.get("hook_text"):
            ep["hook_text"] = job["hook_text"]
        else:
            ep.pop("hook_text", None)
    sp.save_project(ep)
    if job.get("final") and os.path.exists(job["final"]):
        keep = job.setdefault("old_cuts", [])
        old = "%s.%d.mp4" % (os.path.splitext(job["final"])[0], len(keep) + 1)
        try:
            shutil.copyfile(job["final"], old)
            keep.append(old)
        except Exception:
            pass
    job.pop("finished", None)                  # a finished copy of the OLD cut is stale
    job["steps"]["cut"] = "todo"
    job["state"] = "stopped"
    _log(job, "re-cut (captions=%s pace=%s hook=%s)" % (captions, pace, hook))
    save(job)
    return job


def undo_recut(job):
    """CHANGE IT -> "Back to the previous version": the last re-cut is undone (the video
    before it comes back; the one being replaced is kept beside it, so this can be redone)."""
    keep = job.get("old_cuts") or []
    if not keep or not job.get("final"):
        raise ValueError("there's no earlier version")
    prev = keep.pop()
    if not os.path.exists(prev):
        job["old_cuts"] = keep
        save(job)
        raise ValueError("the earlier version is gone")
    cur = job["final"]
    tmp = cur + ".swap"
    shutil.copyfile(cur, tmp)
    shutil.copyfile(prev, cur)
    shutil.move(tmp, prev)                         # the replaced one is still there
    redo = job.setdefault("redo_cuts", [])
    redo.append(prev)
    st_ = job.pop("recut_prev", None)
    if st_:                                        # its captions / pace / hook come back
        for k, v in st_.items():
            job[k] = v
        try:                                       # ...on the episode too (export re-cuts it)
            ep = sp.load_project(job["episode"])
            cap = st_.get("captions", "pop")
            ep["captions"] = cap != "none"
            if ep["captions"]:
                ep["caption_look"] = cap
            if st_.get("pace"):
                ep["pace"] = st_["pace"]
            if st_.get("hook", True) and job.get("hook_text"):
                ep["hook_text"] = job["hook_text"]
            else:
                ep.pop("hook_text", None)
            sp.save_project(ep)
        except Exception:
            pass
    job["old_cuts"] = keep
    _log(job, "back to the previous version")
    save(job)
    return job


def rate(job, stars):
    """1-5 stars on a finished video (0 clears). The writer reads them as taste notes."""
    stars = int(stars or 0)
    if not 0 <= stars <= 5:
        raise ValueError("1 to 5 stars")
    job["rating"] = stars or None
    save(job)
    return job


def taste_notes(fmt=None, jobs=None, n=3):
    """What Ryan liked and didn't (his star ratings), for the writer's brief: the ideas and
    titles of his best- and worst-rated videos, this format first."""
    jobs = jobs if jobs is not None else list_jobs()
    rated = [j for j in jobs if j.get("rating") and j.get("state") == "done"]
    if not rated:
        return ""
    rated.sort(key=lambda j: (j.get("format") != fmt, -j["rating"]))
    good = [j for j in rated if j["rating"] >= 4][:n]
    bad = sorted([j for j in rated if j["rating"] <= 2],
                 key=lambda j: (j.get("format") != fmt, j["rating"]))[:n]
    out = []
    if good:
        out.append("THE KIND OF THING THAT LANDED (Ryan rated these highly):\n" + "\n".join(
            "- %s: %s" % (j.get("title") or "", j.get("idea") or "") for j in good))
    if bad:
        out.append("THE KIND OF THING THAT DIDN'T (rated low; avoid what made them weak):\n" +
                   "\n".join("- %s: %s" % (j.get("title") or "", j.get("idea") or "")
                              for j in bad))
    return ("\n\n" + "\n".join(out) + "\nDon't copy these; write something new in the "
            "spirit of the ones that landed.\n") if out else ""


def shot_list(job):
    """CHANGE IT -> Re-film a shot: every shot in the video as
    [(n, who, line, frame-or-None)], in order. The frame is the take check's still."""
    if not job.get("episode"):
        return []
    try:
        ep = sp.load_project(job["episode"])
    except Exception:
        return []
    out = []
    for sh in ep.get("shots") or []:
        fr = (sh.get("qc") or {}).get("frame")
        who = _name(sh["speaker"]) if sh.get("speaker") else ""
        line = "(reaction)" if sh.get("is_reaction") else (sh.get("line") or "")
        out.append((sh["n"], who, line, fr if fr and os.path.exists(fr) else None))
    return out


def history(query=""):
    """ALL MY VIDEOS: every finished Quick video, newest first, as dicts with
    id / title / idea / format / when / final / cover. `query` filters on title + idea."""
    q = (query or "").strip().lower()
    out = []
    for j in list_jobs():
        if j.get("state") != "done" or not j.get("final"):
            continue
        text = ("%s %s %s" % (j.get("title") or "", j.get("idea") or "",
                              j.get("format") or "")).lower()
        if q and not all(w in text for w in q.split()):
            continue
        fin = j["final"]
        try:
            when = os.path.getmtime(fin)
        except OSError:
            when = 0
        cover = os.path.join(os.path.dirname(fin), "cover.png")
        out.append({"id": j["id"], "title": j.get("title") or j.get("idea") or "untitled",
                    "idea": j.get("idea") or "", "format": j.get("format") or "",
                    "when": when, "final": fin if os.path.exists(fin) else None,
                    "cover": cover if os.path.exists(cover) else None})
    out.sort(key=lambda d: d["when"], reverse=True)
    return out


def step_cut(job, say):
    say("cutting it together")
    ep = sp.load_project(job["episode"])
    cut = sp.assemble(ep)
    job.pop("finished", None)                  # a finished copy of an older cut is stale
    job["final"] = cut["mp4"]
    job["post"] = cut.get("post")


# ---------------------------------------------------------------- MORE LIKE THIS
_CARRY = ("format", "aspect", "seconds", "seconds_asked", "captions", "pace", "place", "hook",
          "reactions", "long_takes", "weirdos", "check_takes", "music",
          "hook_text", "sound",
          "title",
          "lyrics", "song", "song_src", "check", "series", "product", "product_name",
          "product_what", "pick_draft", "voice_roles")


def again(job, how):
    """A new job from a finished one (the done panel's MORE LIKE THIS):
      "script" - same characters, same idea, a fresh script (and voices/looks reused);
      "final"  - the SAME script, voices and set re-shot at Final quality (Draft is for
                 finding the funny one; this keeps it and makes it sharp). The Draft stays.
    Returns the new job, saved, ready for run()."""
    if how not in ("script", "final"):
        raise ValueError("again: how is 'script' or 'final' (got %r)" % (how,))
    if not job.get("cast"):
        raise ValueError("that run never got a cast \u2014 start it fresh")
    if how == "final":
        if not job.get("episode") or not sp.load_project(job["episode"]):
            raise ValueError("re-shooting needs the finished episode")
        if job.get("quality") == "Final":
            raise ValueError("it is already Final")
    nj = new_job(job.get("idea", ""), job.get("format", "sketch"),
                 seconds=job.get("seconds_asked"), aspect=job.get("aspect"),
                 cast=job["cast"], quality="Final" if how == "final" else job.get("quality"),
                 sound=job.get("sound"), captions=job.get("captions", "pop"),
                 pace=job.get("pace"), place=job.get("place"))
    nj["from_job"] = job["id"]
    if how == "script":
        for k in ("hook", "reactions", "long_takes", "weirdos", "check_takes", "music",
                  "series", "product", "product_name", "product_what", "hook_angle",
                  "voice_roles"):
            if k in job:                     # options turned off stay off
                nj[k] = job[k]
        nj["want_cast"] = list(job["cast"])
        nj["seconds"] = job.get("seconds", nj["seconds"])
        save(nj)
        return nj
    for k in _CARRY:
        if k in job:
            nj[k] = job[k]
    nj["cast"] = list(job["cast"])
    nj["want_cast"] = list(job["cast"])
    nj["dialogue"] = job.get("dialogue")
    ep = sp.load_project(job["episode"])
    base = ep["slug"] + "-final"
    slug, k = base, 2
    while os.path.exists(sp.project_dir(slug)):
        slug, k = "%s-%d" % (base, k), k + 1
    ne = json.loads(json.dumps(ep))
    ne["slug"] = slug
    ne["quality"] = "Final"
    ne.pop("cut", None)
    for sh in ne.get("shots") or []:
        sh["takes"], sh["chosen"], sh["status"] = [], None, "new"
    sp._log(ne, "re-shoot of %s at Final (same script, voices and set)" % ep["slug"])
    sp.save_project(ne)
    nj["episode"] = slug
    for st_ in STEPS:
        if st_ not in ("shots", "cut"):
            nj["steps"][st_] = "skip" if job["steps"].get(st_) == "skip" else "done"
    save(nj)
    return nj


def post_text(job):
    """The written caption + hashtags a vertical cut comes with ("" when there is none)."""
    p = job.get("post")
    try:
        return open(p).read().strip() if p and os.path.exists(p) else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ the runner
LAST_EXC = {}


def run(job, director=None, progress=None, stop=None, go_on=False):
    """Run every step not yet done, in order. Returns the job; job["state"] is
    "done" | "review:<step>" | "error" | "stopped". `go_on` = the review was approved."""
    def say(msg):
        job["now"] = msg
        if progress:
            progress(msg)
    if director is None:
        import director as _d
        director = _d.Director()
    job["state"] = "running"
    job["error"] = None
    for step in STEPS:
        if job["steps"][step] in ("done", "skip"):
            continue
        if step == "shots" and job.get("pictures_only"):
            job["state"] = "pictures"
            job["now"] = "the pictures are ready \u2014 waiting for your OK to film"
            _log(job, "pictures only: stopped before filming")
            save(job)
            return job
        if job["steps"][step] == "pick" and job.get("draft_pick") is None:
            job["state"] = "review:drafts"
            job["now"] = "waiting for you \u2014 pick a script"
            save(job)
            return job
        if job["steps"][step] == "review":
            if not go_on:
                job["state"] = "review:%s" % step
                save(job)
                return job
            job["steps"][step] = "done"
            go_on = False
            continue
        job["steps"][step] = "running"
        job["now"] = "%s\u2026" % labels(job).get(step, step)   # never a stale "waiting for you"
        save(job)
        try:
            if step == "cast":
                if director is not None and hasattr(director, "load"):
                    director.load()
                step_cast(job, director, say)
            elif step == "looks":
                step_looks(job, say)
            elif step == "voices":
                step_voices(job, say)
            elif step == "script" and formats.kind(job["format"]) == "music":
                step_lyrics(job, director, say)
            elif step == "lines" and formats.kind(job["format"]) == "music":
                step_song(job, say)
            elif step == "script":
                step_script(job, director, say)
            elif step == "lines":
                step_lines(job, say)
            elif step == "episode":
                step_episode(job, say)
            elif step == "shots":
                step_shots(job, say, stop, director)
            elif step == "cut":
                step_cut(job, say)
        except NeedPick:
            job["steps"][step] = "pick"
            job["state"] = "review:drafts"
            job["now"] = "waiting for you \u2014 pick a script"
            _log(job, "%d drafts written - waiting for you to pick" % len(job["drafts"]))
            save(job)
            return job
        except InterruptedError:
            job["steps"][step] = "todo"
            job["state"] = "stopped"
            save(job)
            return job
        except Exception as exc:
            job["steps"][step] = "todo"
            job["state"] = "error"
            job["error"] = "%s: %s" % (type(exc).__name__, exc)
            job["error_exc"] = type(exc).__name__
            _log(job, "%s failed: %s" % (step, job["error"]))
            save(job)
            LAST_EXC[job["id"]] = exc            # the window needs it (a busy card -> prompt)
            return job
        if step in REVIEW_AFTER and (job["check"] if step != "episode" else
                                     (job["check"] or job.get("check_set")) and has_set(job)):
            job["steps"][step] = "review"
            job["state"] = "review:%s" % step
            job["now"] = "waiting for you \u2014 have a look, then keep going"
            save(job)
            return job
        job["steps"][step] = "done"
        save(job)
    job["state"] = "done"
    job["now"] = "done"
    _log(job, "finished: %s" % job.get("final"))
    if job.get("series"):
        try:
            import series as _series
            _series.add_episode(job["series"], job)
        except Exception as exc:
            _log(job, "couldn't add it to the series: %s" % exc)
    save(job)
    return job
