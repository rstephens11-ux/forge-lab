"""VOICES — build a voice ONCE, properly, and every scene uses it (2026-10-17).

Ryan: "should we consider a different page for voices where we build that first? including
accents and other options available using fish and qwen tts?"

Tk-free engine for voices_ui. What the two engines can and cannot do decides the page:

  * QWEN3-TTS VoiceDesign is the ONLY thing that can invent a voice from a description — the
    gender, age, ACCENT, the six sliders and the free "how they sound" line all become one
    sentence (dialogue.compose_description). Each design is a NEW person, so we audition 3.
  * Both QWEN and FISH S2-Pro can CLONE: a voice IS a reference wav + its transcript.
  * FISH takes the mood INLINE ("[whispering] words"); qwen reads a mood from a mood sample.
  * FISH S2-Pro is Research / NON-COMMERCIAL only (AGENTS.md) — anything rendered with it is
    labelled so on the page.

The rig calls go through scene_engine._rig() (the same tts_batch / transcribe_many the
Dialogue page uses), so the card manager and the busy-card prompt apply unchanged.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

import dialogue as dlg
import scene_engine as se

# Qwen3-TTS's languages (the design/clone `language` argument)
LANGUAGES = ("English", "Spanish", "French", "German", "Italian", "Portuguese", "Russian",
             "Chinese", "Japanese", "Korean")
GENDERS = ("male", "female", "androgynous")

# the Voices page's default sample line (2026-10-21). The old one ("Well. I suppose that's
# one way of looking at it.") is ~2.5 s -- too little for an accent to show, and the voice
# designer drifts to plain English on short reads. This one is longer and full of the
# vowels and r's accents bend.
SAMPLE_LINE = ("Well, I suppose that's one way of looking at it. Mind you, I've lived round "
               "here my whole life, and I've never once heard anybody put it quite like that.")

# more accents on top of dialogue.ACCENTS (label, the words VoiceDesign reads)
MORE_ACCENTS = [
    ("Canadian", "Canadian"),
    ("Minnesotan", "Minnesotan Upper-Midwest"),
    ("Cajun / Louisiana", "Cajun Louisiana"),
    ("Appalachian", "Appalachian mountain"),
    ("Chicago", "nasal Chicago"),
    ("Philadelphia", "Philadelphia"),
    ("Brooklyn (old)", "old-school Brooklyn"),
    ("Hawaiian pidgin", "Hawaiian pidgin English"),
    ("New Zealand (Kiwi)", "New Zealand"),
    ("Northern English (Manchester)", "Mancunian Northern English"),
    ("Liverpool (Scouse)", "Liverpool Scouse"),
    ("Geordie (Newcastle)", "Geordie Newcastle"),
    ("West Country", "rural West Country English"),
    ("Dutch", "Dutch"),
    ("Swedish", "Swedish"),
    ("Norwegian", "Norwegian"),
    ("Danish", "Danish"),
    ("Finnish", "Finnish"),
    ("Polish", "Polish"),
    ("Greek", "Greek"),
    ("Turkish", "Turkish"),
    ("Arabic", "Levantine Arabic"),
    ("Israeli", "Israeli Hebrew"),
    ("Persian", "Persian"),
    ("Nigerian", "Nigerian"),
    ("Kenyan", "Kenyan"),
    ("Ghanaian", "Ghanaian"),
    ("Brazilian", "Brazilian Portuguese"),
    ("Mexican", "Mexican Spanish"),
    ("Argentinian", "Argentinian Rioplatense"),
    ("Cuban", "Cuban"),
    ("Puerto Rican", "Puerto Rican"),
    ("Trinidadian", "Trinidadian Caribbean"),
    ("Japanese", "Japanese"),
    ("Korean", "Korean"),
    ("Mandarin Chinese", "Mandarin Chinese"),
    ("Cantonese (Hong Kong)", "Hong Kong Cantonese"),
    ("Filipino", "Filipino"),
    ("Vietnamese", "Vietnamese"),
    ("Thai", "Thai"),
    ("Pakistani", "Pakistani"),
    ("Transatlantic news anchor", "1950s transatlantic newsreader"),
    ("Game-show host", "booming American game-show announcer"),
    ("Movie-trailer voice", "deep American movie-trailer narrator"),
    ("Robot / synthetic", "flat synthetic robotic"),
]


def accents():
    """[(label, phrase)] — dialogue.ACCENTS first (its order is Ryan's), then the rest A-Z,
    no label twice."""
    seen, out = set(), []
    for lab, ph in list(dlg.ACCENTS) + sorted(MORE_ACCENTS):
        if lab not in seen:
            seen.add(lab)
            out.append((lab, ph))
    return out


def accent_phrase(label_or_text):
    """The words VoiceDesign reads for a picked label; typed text passes through as is."""
    t = (label_or_text or "").strip()
    for lab, ph in accents():
        if lab == t:
            return ph
    return t


def accent_label(phrase):
    p = (phrase or "").strip()
    for lab, ph in accents():
        if ph == p and p:
            return lab
    return p


def licence(engine):
    return "non-commercial only (Fish S2-Pro)" if engine == "fish" else "ok for commercial use"


# ------------------------------------------------------------------ the library
def library():
    """Every saved voice as the page lists it: [{slug, name, line, wav, chars, origin}]."""
    out = []
    for v in dlg.list_voices():
        wav = dlg.voice_wav(v, "neutral")
        bits = [("%s %s" % (v.get("age_years", ""), v.get("gender", ""))).strip()]
        if v.get("accent"):
            bits.append(accent_label(v["accent"]))
        if v.get("origin") == "clone":
            bits.append("cloned")
        out.append({"slug": v.get("slug"), "name": v.get("name") or v.get("slug"),
                    "line": " · ".join(b for b in bits if b),
                    "wav": wav if wav and os.path.exists(wav) else None,
                    "chars": list(v.get("characters") or []),
                    "character": v.get("character"),
                    "origin": v.get("origin") or "designed",
                    "readonly": v.get("source") == "character", "voice": v})
    return out


def _free_slug(name):
    """A slug for a NEW voice that no voice in the library has yet ("x", then "x-2"...)."""
    base = dlg.slugify(name)
    s_, k = base, 2
    while os.path.exists(os.path.join(dlg.voices_dir(), s_)):
        s_ = "%s-%d" % (base, k)
        k += 1
    return s_


def _mark_new(v):
    """⚠️ 2026-10-21: every voice built from "New voice" got the slug "new-voice", so each
    Keep REPLACED the last one built that way, and editing a fresh form auto-saved over it.
    A new voice now carries a slug nothing uses yet, and Keep re-derives it from the name
    typed by then (see keep)."""
    v["slug"] = _free_slug(v.get("name") or "voice")
    v["_new"] = True
    return v


def blank(name="New voice"):
    v = dlg.new_voice(name)
    v["age_years"] = 35
    v["language"] = "English"
    return _mark_new(v)


def describe(v):
    """The exact sentence VoiceDesign will read (shown on the page)."""
    return dlg.compose_description(v, "neutral")


# ------------------------------------------------------------------ build: design
def audition(v, n=3, text=None, progress=None):
    """n reads of the description — each a new person (VoiceDesign varies) — reading `text`
    (default: the audition line). -> [wav or None, ...]"""
    tb, _tm = se._rig()
    base = describe(v).rstrip(".")
    say = (text or "").strip() or se.AUDITION_TEXT
    items = [{"id": "aud:%d" % k, "instruct": base + se.AUDITION_TWISTS[k % 3] + ".",
              "text": say, "out": "aud_%d.wav" % k} for k in range(n)]
    out_dir = tempfile.mkdtemp(prefix="forge-voice-")
    if progress:
        progress("designing %d voices" % n)
    info = tb({"design": items, "clone": [], "voices": {},
               "language": v.get("language") or "English"}, out_dir, engine="qwen")
    got = {it.get("id"): it.get("out") for it in info.get("items", []) if it.get("out")
           and "error" not in it}
    wavs = [got.get("aud:%d" % k) for k in range(n)]
    if not any(wavs):
        raise RuntimeError("no audition came back from the rig")
    v["_aud_text"] = say
    return wavs


def keep(v, wav, char_slug=None):
    """Keep one audition: it becomes the voice's reference, saved to the library (and linked
    to a character when one is given). -> slug"""
    name = (v.get("name") or "voice").strip()
    v = json.loads(json.dumps(v))
    v.setdefault("origin", "designed")
    say = v.pop("_aud_text", None)
    if v.pop("_new", False):            # a NEW voice: its own slug, from the name it has now
        v["slug"] = _free_slug(name)
        v["characters"] = [c for c in (v.get("characters") or [])
                           if c != "NEW VOICE"]
    slug = se.keep_audition(v, wav, name, char_slug)
    if say and say != se.AUDITION_TEXT:              # the reference transcript must match
        sv = dlg.load_voice(slug)
        sv["moods"]["neutral"]["ref_text"] = say
        dlg.save_voice(sv)
    return slug


# ------------------------------------------------------------------ build: clone
def _to_wav(src, out):
    """Any audio/video file -> a mono 24 kHz wav (what both engines clone from best)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-ac", "1", "-ar", "24000",
                    "-t", "30", out], check=True, capture_output=True, timeout=120)
    return out


def clone(name, src, transcript=None, language="English", char_slug=None, progress=None):
    """A voice from a recording. The transcript is what the clip SAYS — typed, or heard by the
    rig's ASR when left blank (a wrong transcript makes a wobbly clone). Capped at 30 s.
    -> slug"""
    if not src or not os.path.exists(src):
        raise ValueError("pick a recording first")
    name = (name or "").strip() or os.path.splitext(os.path.basename(src))[0]
    v = dlg.new_voice(name)
    v.update({"origin": "clone", "language": language, "notes": "cloned from %s"
              % os.path.basename(src)})
    slug = v["slug"]
    d = os.path.join(dlg.voices_dir(), slug)
    os.makedirs(d, exist_ok=True)
    wav = _to_wav(src, os.path.join(d, "neutral.wav"))
    text = (transcript or "").strip()
    if not text:
        if progress:
            progress("listening to the clip for its words")
        _tb, tm = se._rig()
        text = ((tm([wav], language=language) or [""])[0] or "").strip()
        if not text:
            raise RuntimeError("couldn't hear any words in that clip — type what it says")
    v["moods"]["neutral"] = {"extra": "", "wav": "neutral.wav", "ref_text": text}
    if char_slug:
        v["character"] = char_slug
    dlg.save_voice(v)
    if char_slug:
        se._link_character(char_slug, slug)
    return slug


# ------------------------------------------------------------------ the test bench
def test(v, text, mood="neutral", engines=("qwen", "fish"), progress=None):
    """Say `text` in voice `v` on each engine. -> {engine: wav path | "error: ..."}
    Fish gets the mood inline; qwen clones the neutral reference (its moods come from
    designed mood samples, made when a scene needs them)."""
    ref = dlg.voice_wav(v, "neutral")
    if not ref or not os.path.exists(ref):
        raise ValueError("this voice has no reference yet — keep an audition first")
    rt = ((v.get("moods") or {}).get("neutral") or {}).get("ref_text") or dlg.DEFAULT_SAMPLE
    tb, _tm = se._rig()
    out = {}
    for eng in engines:
        if progress:
            progress("saying it with %s" % ("Fish S2-Pro" if eng == "fish" else "Qwen3-TTS"))
        said = dlg.fish_line_text(mood, text, v.get("fish_direction")) if eng == "fish" \
            else text
        # ⚠️ "out" is required by BOTH rig scripts (rig_dialogue / rig_fish read item["out"]);
        # without it every test came back "error: 'out'" (the fake rig used to forgive it)
        job = {"design": [], "clone": [{"id": "t:%s" % eng, "voice": "v", "text": said,
                                        "out": "test_%s.wav" % eng}],
               "voices": {"v": {"ref_wav": ref, "ref_text": rt}},
               "language": v.get("language") or "English"}
        od = tempfile.mkdtemp(prefix="forge-vtest-")
        try:
            info = tb(job, od, engine=eng)
            it = next((i for i in info.get("items", []) if i.get("id") == "t:%s" % eng), {})
            out[eng] = it.get("out") if it.get("out") and "error" not in it else \
                "error: %s" % (it.get("error") or "no audio came back")
        except Exception as exc:
            out[eng] = "error: %s" % exc
        out[eng] = _keep_test(v, eng, out[eng], said, mood)
    return out


def _keep_test(v, eng, wav, said, mood):
    """Every test take is kept beside the voice (2026-10-21, Ryan testing Fish accents): it
    used to live in a temp folder and was gone after the next test, so nothing could be
    compared. -> the kept path (or what came in, when there is nothing to keep).
    DATA/voices/<slug>/tests/<when>-<engine>.wav + tests/index.json (what was said, how)."""
    slug = v.get("slug")
    if not slug or not wav or str(wav).startswith("error") or not os.path.exists(wav):
        return wav
    try:
        d = os.path.join(dlg.voices_dir(), slug, "tests")
        os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(d, "%s-%s.wav" % (stamp, eng))
        k = 2
        while os.path.exists(dst):
            dst = os.path.join(d, "%s-%s-%d.wav" % (stamp, eng, k))
            k += 1
        shutil.copyfile(wav, dst)
        idx_f = os.path.join(d, "index.json")
        try:
            idx = json.load(open(idx_f))
        except Exception:
            idx = []
        idx.append({"file": os.path.basename(dst), "engine": eng, "said": said, "mood": mood,
                    "direction": v.get("fish_direction") or "", "when": stamp})
        with open(idx_f + ".tmp", "w") as fh:
            json.dump(idx, fh, indent=1)
        os.replace(idx_f + ".tmp", idx_f)
        return dst
    except Exception:
        return wav


# ------------------------------------------------------------------ from a character
def _age_from(text):
    """'mid-30s' / 'in his forties' / 'elderly' -> years, or None."""
    import re
    t = (text or "").lower()
    m = re.search(r"\b(early|mid|late)?[- ]?([2-9])0s\b", t)
    if m:
        return int(m.group(2)) * 10 + {"early": 2, "mid": 5, "late": 8}.get(m.group(1), 5)
    for word, yrs in (("twenties", 25), ("thirties", 35), ("forties", 45), ("fifties", 55),
                      ("sixties", 65), ("seventies", 75), ("eighties", 85)):
        if word in t:
            return yrs
    return None


def from_character(slug):
    """A starting voice for a saved character: gender, age and the bible's voice line
    (accent words in it carry through to VoiceDesign). Not saved."""
    meta = {}
    for s_, m_ in se.list_characters():
        if s_ == slug:
            meta = m_
    if not meta:
        raise ValueError("no character %r" % slug)
    v = se.voice_seed(meta.get("name") or slug, meta)
    v.update({"language": "English", "character": slug})
    _mark_new(v)
    age = _age_from(" ".join([meta.get("prompt") or "", str((meta.get("picks") or {})
                                                                 .get("Subject") or "")]))
    if age:
        v["age_years"] = age
    if not v.get("notes"):
        v["notes"] = "a clear, natural speaking voice that suits %s" % (
            (meta.get("picks") or {}).get("Subject") or "them")
    return v


def auto_voice(slug, progress=None):
    """ONE CLICK: design a voice from the character, keep the first read, link it.
    -> voice slug. (The Voices page is there to audition three and fine-tune.)"""
    v = from_character(slug)
    wavs = audition(v, 1, progress=progress)
    if not wavs or not wavs[0]:
        raise RuntimeError("no voice came back from the rig")
    vs = keep(v, wavs[0], slug)
    return vs


# ------------------------------------------------------------------ linking
def link(slug, char_slug):
    """This voice is that character's: Dialogue and Episode cast it automatically."""
    v = dlg.load_voice(slug)
    if v is None:
        raise ValueError("no voice %r" % slug)
    v["character"] = char_slug
    dlg.save_voice(v)
    return se._link_character(char_slug, slug)


def save_settings(v):
    """Rename / re-describe a kept voice without re-recording it."""
    if v.get("source") == "character":
        raise ValueError("this voice comes with a character pack — duplicate it to edit")
    dlg.save_voice(v)
    return v.get("slug")
