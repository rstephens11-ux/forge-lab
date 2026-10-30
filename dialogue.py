"""dialogue.py — Tk-free, stdlib-only DIALOGUE engine for Forge Lab.

Parse scripts, manage voice bank, build speaker descriptions, generate ffmpeg
fx filter strings, assemble scene audio, score ASR output, and estimate run
cost.  No numpy, no tkinter — works on Linux too.  `re` IS allowed here.

DATA dir: read from FORGE_LAB_DATA env at CALL time (~/forge-lab by default).
"""
import array
import difflib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import wave

# --------------------------------------------------------------------------- DATA paths

def _data():
    """Return the FORGE_LAB_DATA path, defaulting to ~/forge-lab."""
    return os.environ.get("FORGE_LAB_DATA",
                          os.path.join(os.path.expanduser("~"), "forge-lab"))


def voices_dir():
    """Path to the voice bank directory."""
    return os.path.join(_data(), "voices")


def dialogue_dir():
    """Path to the dialogue projects directory."""
    return os.path.join(_data(), "dialogue")


# --------------------------------------------------------------------------- constants

SLIDERS = ("pitch", "pace", "energy", "age", "warmth", "grit")

# Each slider at each non-zero level gets a natural-language phrase.
# 0 always maps to "" (omitted from descriptions).
SLIDER_WORDS = {
    "pitch": {
        -2: "very low",
        -1: "low",
         0: "",
        +1: "high",
        +2: "very high",
    },
    "pace": {
        -2: "slow, unhurried, deliberate pauses",
        -1: "measured pace",
         0: "",
        +1: "brisk, even pace",
        +2: "fast, forward clip, no dramatic pauses, no drawn-out words",
    },
    "energy": {
        -2: "subdued",
        -1: "low-key",
         0: "",
        +1: "animated",
        +2: "very energetic",
    },
    "age": {
        -2: "sounds younger",
        -1: "slightly younger",
         0: "",
        +1: "slightly older",
        +2: "sounds older",
    },
    "warmth": {
        -2: "cold",
        -1: "cool",
         0: "",
        +1: "warm",
        +2: "very warm and friendly",
    },
    "grit": {
        -2: "very clean and smooth",
        -1: "clean",
         0: "",
        +1: "slightly raspy",
        +2: "gravelly and rough",
    },
}

DEFAULT_SAMPLE = (
    "The morning was cold, and the road ahead was longer than we planned."
)

MOOD_WORDS = {
    "neutral": "",
    "alarmed": "alarmed, urgent, breath quickened",
    "angry": "angry, clipped, forceful",
    "warm": "gentle and affectionate",
    "sad": "quiet, heavy, sorrowful",
    "shouting": "shouting, projecting loudly",
    "whisper": "whispering, hushed",
    "amused": "dryly amused, a smile in the voice",
    # added 2026-10-12 (Ryan: "and other moods")
    "indignant": "indignant, offended, drawing himself up",
    "proud": "proud, chest out, ringing with conviction",
    "solemn": "solemn, grave, slow and reverent",
    "stern": "stern, firm, commanding",
    "deadpan": "deadpan, flat, completely sincere, no irony in the voice",
    "sarcastic": "sarcastic, dry, eye-rolling",
    "nervous": "nervous, hesitant, a slight tremble",
    "confused": "confused, puzzled, uncertain, questioning",
    "excited": "excited, bright, words tumbling out",
    "pleading": "pleading, earnest, begging",
    "tired": "tired, weary, drained",
    "disgusted": "disgusted, recoiling",
    "laughing": "laughing while speaking, breaking up with laughter",
    "tearful": "tearful, voice cracking, on the edge of crying",
    "oratory": "orating to a crowd, rhetorical, rising cadence, projecting like a speech",
    "drunk": "tipsy, slurring slightly, loose and loud",
}

# --------------------------------------------------------------------------- voice engines
# Which model reads a spoken line. The two are NOT interchangeable:
#
#   qwen — Qwen3-TTS. VoiceDesign makes a voice from a DESCRIPTION; Base clones it per line.
#          Its clone has NO emotion control, so a "mood" here is a SEPARATELY DESIGNED
#          reference sample (`voices/<slug>/<mood>.wav`).
#   fish — Fish Audio S2-Pro. Clone only, but a mood is an INLINE `[tag]` in the text, so ONE
#          reference per voice is enough and per-mood samples are never needed. It has no
#          VoiceDesign at all, so voice references are still MADE by qwen — the engine choice
#          governs how LINES are read, not how voices are made.
#   drama3 — Fish Drama 3 (35B MoE) INT4, served by SGLang-Omni on the rig's two cards. Same
#          inline-[tag] shape as fish and no VoiceDesign, so it reuses fish_tag/fish_line_text
#          and the "<slug>" voices key. Why the int4 checkpoint: the bf16/FP8 one cannot load on
#          the 3090s at all (FP8 experts need SM89+; it dequantizes to ~70 GB and OOMs,
#          measured 2026-10-14). The int4 one fits the two cards and runs ~0.9x realtime.
#          ⚠️ It is a RESIDENT SERVER, not a one-shot load, and it owns card 1 while it runs.
ENGINES = (
    ("qwen", "Qwen3-TTS"),
    ("fish", "Fish S2-Pro"),
    ("drama3", "Drama 3 (int4)"),
)
DEFAULT_ENGINE = "qwen"


def engine_label(name):
    """Display label for an engine key (falls back to the default's label)."""
    for key, label in ENGINES:
        if key == name:
            return label
    return ENGINES[0][1]


def is_engine(name):
    return any(key == name for key, _ in ENGINES)


# ACCENT presets — the ACCENT box stays free text; these just fill it. The model follows accent
# words APPROXIMATELY: always AUDITION, and believe your ears over the label.
# ⚠️ "Old English" literally = Anglo-Saxon (unintelligible to a modern listener, and the model
# cannot speak it). The preset means OLD-FASHIONED English — Early Modern / 18th-century.
ACCENTS = [
    ("— none —", ""),
    ("Old English (archaic, Shakespearean)", "old-fashioned archaic English, Early Modern, theatrical Shakespearean"),
    ("18th-c. English gentleman", "18th-century English gentleman, refined upper-class Georgian"),
    # ⚠️ Ryan, 2026-10-12: "the proper accent of an early american gentleman
    # accent". No recording exists; this is the scholarly reconstruction of 1750s Virginia
    # Tidewater gentry speech: RHOTIC (the r's are said — NOT posh British RP, which dropped
    # them later), roots in West Country / Southern English, softened toward the Southern
    # American that grew from it; formal, measured, educated. The default character voice.
    ("Early American gentleman (Avery Stone)",
     "early American gentleman, 1750s Virginia Tidewater gentry, rhotic with the r's pronounced, "
     "old West Country English roots softened toward a gentle Southern lilt, refined, measured "
     "and formal, not British RP"),
    ("Colonial Virginia (1750s)", "colonial Virginia, between Southern and old English, 1750s gentry"),
    ("British RP (posh)", "British Received Pronunciation, posh"),
    ("Cockney", "London Cockney"),
    ("Scottish", "Scottish"),
    ("Irish", "Irish"),
    ("Welsh", "Welsh"),
    ("Yorkshire", "Yorkshire, northern English"),
    ("New York", "New York"),
    ("New York Italian", "New York Italian-American"),
    ("Boston", "Boston"),
    ("Maine / Downeast", "Downeast Maine, New England"),
    ("Southern US", "Southern American drawl"),
    ("Texas", "Texan"),
    ("Midwest", "Midwestern American"),
    ("Valley / California", "California valley"),
    ("Mid-Atlantic (1940s film)", "Mid-Atlantic, old Hollywood 1940s film"),
    ("Australian", "Australian"),
    ("South African", "South African"),
    ("French", "French"),
    ("German", "German"),
    ("Italian", "Italian"),
    ("Spanish", "Spanish"),
    ("Russian", "Russian"),
    ("Indian", "Indian English"),
    ("Jamaican", "Jamaican"),
]


def accent_label(text):
    """The preset label whose words match `text` exactly, else "custom" (or "— none —")."""
    t = (text or "").strip()
    for label, words in ACCENTS:
        if words == t:
            return label
    return "custom"


# SUNG lines go to YuE2 (the Song engine) instead of the speaking model. A line is sung when its
# direction says so ("sings", "singing", "humming a tune") or its text starts with [sung] / ♪.
# ⚠️ YuE2 is a different model: the singer will NOT be the same voice as the speaking clone. The
# voice's `singing` words steer it (gender, range, character) — the timbre is YuE2's own.
# ⚠️ MEASURED 2026-10-12 (ASR word-match on one Yankee Doodle line): a cappella at 12 s = 0.43
# (garbled); a cappella at 20 s = 0.75 (words repeated); THIS style at 20 s = 0.99. One trial —
# it is not known whether the diction words or the guitar did it. Change with evidence only.
SUNG_STYLE = ("English, slow traditional folk song, 70 BPM, solo vocal, clear diction, every word "
              "sung plainly, light acoustic guitar")


def is_sung(line):
    if "sung" in line:              # an explicit choice in the LINES row wins over guessing
        return bool(line["sung"])
    d = (line.get("direction") or "").lower()
    t = (line.get("text") or "").lstrip()
    return bool(re.search(r"\bsing(s|ing)?\b|\bsung\b|\bhums?\b|\bhumming\b|\bchant", d)) \
        or t.startswith("[sung]") or t.startswith("\u266a")


def sung_text(text):
    """The words to sing: tags/♪ stripped."""
    t = re.sub(r"^\s*\[sung\]\s*", "", text or "")
    return t.replace("\u266a", "").strip()


def sung_lyrics(text):
    """YuE2 wants lyrics in section blocks; one sentence or comma clause per sung line."""
    words = sung_text(text)
    parts = [x.strip() for x in re.split(r"(?<=[.!?;,])\s+", words) if x.strip()]
    return "[Verse]\n" + "\n".join(parts or [words])


def sung_style(voice, project_style=None):
    """Style prompt for one sung line: the project's musical style + this voice's singer."""
    singer = (voice or {}).get("singing") or ""
    if not singer:
        g = (voice or {}).get("gender", "male")
        a = (voice or {}).get("accent", "")
        singer = "%s vocal%s" % (g, (", " + a + " accent") if a else "")
    return "%s, %s" % (project_style or SUNG_STYLE, singer)


def sung_seconds(text):
    """Requested length (YuE2 minimum is 10 s; it ends on its own end token, so this is a cap)."""
    n = len(sung_text(text).split())
    # ⚠️ 20 s floor, not YuE2's own 10: a 12 s request came back garbled (see SUNG_STYLE).
    return float(max(20, min(60, round(n * 0.9 + 10))))

PASS_SCORE = 0.85

# MEASURED on forge GPU1, 2026-10-12 (Phase B live proof), Mac wall time incl. ssh/scp:
#   design 1 voice = 12.68 s (gen 3.98)  ·  clone 3 short lines = 11.01 s (gen 3.78)  ·  ASR 3 = 8.50 s
# The fixed terms are ssh + scp + model load. PER_LINE_S is padded above the measured 1.26 s
# because those were 2-7 word lines; a real script line runs several seconds of speech.
# Re-measured on the 105 teaser (9 real script lines, 54 s of speech): clone 47.0 s wall,
# design x4 17.8 s, ASR x9 10.9 s. Real lines are LONG — the per-line term is what moved.
LOAD_S = 7.2               # clone batch fixed cost
PER_LINE_S = 4.4           # per cloned line (real script lines, ~6 s of speech each)
DESIGN_LOAD_S = 8.7        # design batch fixed cost
PER_DESIGN_S = 2.4         # per designed reference sample (17.8 s for 4 incl. load)
ASR_LOAD_S = 6.0           # ASR batch fixed cost
PER_ASR_S = 1.0            # per line transcribed

# FISH (S2-Pro) — MEASURED on forge GPU1, 2026-10-14, one-shot rig_fish.py (Mac wall incl ssh/scp).
# ⚠️ Its LOAD is 5x Qwen's: S2-Pro is a 5B model and each render pays to build it from disk
# (no resident server — that would sit on 20.5 GB of GPU1 and fight ComfyUI/Ollama).
#   load: 38.38 / 38.71 / 38.4 s across three runs  ·  RTF 0.95-1.10
#   7.62 s of speech generated in 7.27 s · 2.23 s in 2.28 s · 1.02 s in 1.13 s
# So the per-line term tracks SPEECH LENGTH, not a fixed cost: ~1.0x realtime. A real script
# line runs ~6 s of speech (the same assumption PER_LINE_S is built on).
FISH_LOAD_S = 38.5
FISH_PER_LINE_S = 6.0

# DRAMA 3 (int4 / SGLang-Omni) — MEASURED on forge 2026-10-21 (2x3090, CUDA graphs off, Mac wall
# incl. ssh/scp). TWO cost shapes, because it is a server rather than a one-shot load:
#   * ALREADY RUNNING: no load at all — 5.9 s of speech in 5.1 s, 16.25 s in 14.7 s (~0.9x RTF).
#     The per-line term is therefore ~1.0x the speech length, like fish.
#   * COLD: the script starts the server and waits for /v1/models — 90-130 s (weights 20.8 GB
#     off the NVMe; the int4 Marlin kernel is JIT-cached after the first ever start), then stops
#     it again (~5 s) to give card 1 back. That cold start is a BATCH cost, paid once.
# Estimate the cold shape: a Forge Lab batch always starts from a stopped server, because
# nothing else may hold card 1 between runs.
DRAMA3_LOAD_S = 120.0
DRAMA3_PER_LINE_S = 6.0


# --------------------------------------------------------------------------- parse_script

def parse_script(text):
    """Parse a script into list of {idx, speaker, direction, text} dicts.

    Accepts these formats (any separator: em-dash, en-dash, ` - `, `:`):
      `> **AVERY** — *(dir)* text`
      `**AVERY** — text`
      `AVERY: text`
      `AVERY (V.O.): text`
      `AVERY — text`

    Non-dialogue lines (actions, headings, ``---``, TITLE CARD) are skipped.
    Direction `*(…)*` or `(…)` at the START of text is moved to `direction`;
    inline `*(…)*` later in the text are removed.  Markdown ``*``/``_``/``**``
    are stripped.  `idx` is 0-based in order of appearance.
    """
    lines = []
    idx = 0
    sep_re = r"(?:—|–|-{2}|-|\s*:\s*)"

    bold_re = re.compile(
        r"^(?:>\s+)?\*\*([A-Z][A-Z .'/]*(?:\s*\([A-Z.' ]+\))?)\*\*"
        r"\s*" + sep_re + r"\s*(.*)$"
    )
    plain_re = re.compile(
        r"^([A-Z][A-Z .'/]*(?:\s*\([A-Z.' ]+\))?)"
        r"\s*" + sep_re + r"\s*(.*)$"
    )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Skip lines that are action/heading markers
        if re.match(r"^---+$", line):
            continue
        if re.match(r"^\*\*[^*]+\*\*$|^[A-Z\s]+:", line):
            # Could be a TITLE CARD or a simple label — check if it's a
            # bold-formatted card without a separator
            if re.match(r"^\*\*TITLE CARD", line, re.I):
                continue
            if re.match(r"^\*\*END", line, re.I):
                continue

        m = bold_re.match(line)
        if not m:
            m = plain_re.match(line)
        if not m:
            continue

        speaker_raw = m.group(1).strip()
        rest = m.group(2).strip()
        # ⚠️ Screenplay furniture that LOOKS like "NAME: text" — found in the real s1-v2 scripts
        # ("CARD: **THAT WAS THREE WEEKS AGO.**", "THE EXPERIMENT: phones in a drawer...").
        if normalise_speaker(speaker_raw) in _NOT_SPEAKERS:
            continue

        direction, rest_text = _extract_direction(rest)
        clean_text = _clean_text(rest_text)
        if not clean_text:
            continue

        lines.append({
            "idx": idx,
            "speaker": speaker_raw,
            "direction": direction,
            "text": clean_text,
        })
        idx += 1

    return lines


_NOT_SPEAKERS = {"CARD", "TITLE CARD", "TITLE", "SUPER", "INSERT", "CHYRON", "CAPTION",
                 "THE EXPERIMENT", "NOTE", "SCENE", "INT", "EXT", "CUT TO", "FADE IN",
                 "FADE OUT", "MONTAGE", "LOCATION", "LOCATIONS", "BEAT", "SFX", "MUSIC"}


def _extract_direction(text):
    """Extract `*(…)` or `(…)` direction from the START of text.

    Returns (direction, remaining_text).
    """
    t = text.strip()
    if t.startswith("*("):
        end = t.find(")*")
        if end != -1 and end >= 2:
            direction = t[2:end].strip()
            rest = t[end + 2:].strip()
            return direction, rest
    elif t.startswith("("):
        end = t.find(")")
        if end != -1:
            direction = t[1:end].strip()
            rest = t[end + 1:].strip()
            return direction, rest
    return "", t


def _clean_text(text):
    """Remove inline `*(…)` directions and all markdown markers."""
    # Remove inline *(...)* that are NOT at the very start (already extracted)
    t = re.sub(r"\*\([^)]*\)\*", "", text)
    # Strip bold/italic/underscore markers
    t = t.replace("**", "").replace("*", "").replace("_", "")
    return t.strip()


# --------------------------------------------------------------------------- normalise_speaker

def normalise_speaker(name):
    """Upper-case, strips `(V.O.)`, `(O.S.)`, `(CONT'D)`, `(O.C.)`, extra spaces."""
    n = name.upper().strip()
    for suffix in ("(V.O.)", "(O.S.)", "(CONT'D)", "(O.C.)"):
        n = n.replace(suffix, "")
    return re.sub(r"\s+", " ", n).strip()


# --------------------------------------------------------------------------- speakers

def speakers(lines):
    """Unique normalised speaker names, first-appearance order."""
    seen = []
    for l in lines:
        s = normalise_speaker(l["speaker"])
        if s not in seen:
            seen.append(s)
    return seen


# --------------------------------------------------------------------------- lint_line

# Placeholder domains for URL detection (not exhaustive, catches common cases)
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)


def lint_line(text):
    """Human warnings about a line of dialogue.

    Returns list of warning strings (empty = clean).
    Warnings for: digits, ALL-CAPS words (>=2 letters, not I/OK),
    `&`, `%`, `$`, `/`, URLs, length > 300.
    """
    warnings = []

    # Digits
    digits = re.findall(r"\d+", text)
    if digits:
        warnings.append(
            "write numbers as words — %r is read digit by digit"
            % " ".join(digits[:3])
        )

    # ALL-CAPS words (>=2 letters, not I/OK, not contractions)
    words = text.split()
    caps_words = [w for w in words
                  if w.isupper() and len(w) >= 2
                  and w not in ("I", "OK", "OKAY")
                  and w not in ("I'", "I'M", "I'LL", "I'D", "I'VE")]
    if caps_words:
        warnings.append(
            "ALL-CAPS words may be spelled out: %r" % caps_words[:5]
        )

    # Symbols
    if "&" in text:
        warnings.append("'&' is read as 'ampersand'")
    if "%" in text:
        warnings.append("'%' is read as 'percent'")
    if "$" in text:
        warnings.append("'$' is read as 'dollar'")
    if "/" in text:
        warnings.append("'/' is read as 'slash'")

    # URLs
    if _URL_RE.search(text):
        warnings.append("URLs are read character by character")

    # Length
    if len(text) > 300:
        warnings.append("split long lines (over 300 characters)")

    return warnings


# --------------------------------------------------------------------------- slugify

def slugify(name):
    """Lower, a-z0-9 and ``-``, collapse repeats, strip ends.  ``""`` → ``"untitled"``."""
    if not name or not name.strip():
        return "untitled"
    s = name.lower()
    s = s.replace("'", "")  # apostrophes removed, not dashes
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    return s if s else "untitled"


# --------------------------------------------------------------------------- compose_description

_STARTS_WITH_VOWEL = re.compile(r"^([aeiou]|8|11-|18-)", re.I)   # "An 8-year-old"
# accent words that already say how strong it is -- don't stack "strong" on top of them
_ACCENT_STRENGTH = re.compile(r"\b(slight|slightly|mild|mildly|light|faint|subtle|soft|"
                              r"gentle|hint|trace|thick|heavy|strong|broad|pronounced)\b", re.I)


def compose_description(voice, mood="neutral"):
    """One sentence for VoiceDesign.

    Template::
      ``{A|An} {age_years}-year-old {gender} voice, {accent} accent, {slider phrases}, {notes}, {mood extra}.``

    Empty parts are skipped; no double commas/spaces.  Ends with one period.
    Deterministic.
    """
    parts = []

    # Core: age + gender
    age = voice.get("age_years", 20)
    gender = voice.get("gender", "male")
    core = "%d-year-old %s voice" % (age, gender)
    parts.append(core)

    # Accent -- Ryan 2026-10-21: "the accent is not working, it all sounds like plain
    # english". VoiceDesign leans hard toward standard American English and treats a bare
    # "Scottish accent" buried mid-list as optional. So the accent comes straight after the
    # core, said strongly (unless the words already say how strong), and a closing sentence
    # names the language it is spoken in -- Qwen's own examples read "a thick French accent
    # speaking English".
    accent = (voice.get("accent") or "").strip()
    strong_accent = accent and accent.lower() not in ("neutral", "none", "no", "standard")
    told_strength = bool(_ACCENT_STRENGTH.search(accent))
    if strong_accent:
        parts[0] += " with a %s%s accent" % ("" if told_strength else "strong, unmistakable ",
                                            accent)
    elif accent:
        parts.append(accent + " accent")

    # Slider phrases (non-zero only, in SLIDERS order)
    sliders = voice.get("sliders", {})
    for key in SLIDERS:
        val = sliders.get(key, 0)
        phrase = SLIDER_WORDS.get(key, {}).get(val, "")
        if phrase:
            parts.append(phrase)

    # Notes
    notes = (voice.get("notes") or "").strip()
    if notes:
        parts.append(notes)

    # Mood extra
    extra = MOOD_WORDS.get(mood, "")
    if extra:
        parts.append(extra)

    # Join with ", " — no part is empty at this point
    body = ", ".join(parts)

    # Prefix "A" or "An"
    if _STARTS_WITH_VOWEL.match(parts[0]):
        article = "An"
    else:
        article = "A"

    result = "%s %s." % (article, body)
    if strong_accent:
        who = {"female": "She", "male": "He"}.get(gender, "They")
        lang = (voice.get("language") or "English").strip() or "English"
        if told_strength:
            result += " %s speak%s %s with that %s accent on every word." % (
                who, "" if who == "They" else "s", lang, accent)
        else:
            result += " %s speak%s %s with a heavy %s accent, clearly audible on every " \
                      "word, never neutral standard %s." % (
                          who, "" if who == "They" else "s", lang, accent, lang)
    # Single period at the end; remove any double periods
    result = result.replace("..", ".")
    return result


# --------------------------------------------------------------------------- mood_of

_MOOD_KEYWORDS = [
    ({"angry", "furious", "irate", "seething", "snap"}, "angry"),
    ({"shout", "yelling", "loud", "scream", "bellow", "roar"}, "shouting"),
    ({"whisper", "hushed", "murmur", "mutter", "aside", "breath"}, "whisper"),
    ({"alarm", "panic", "startled", "urgent", "gasp", "shock"}, "alarmed"),
    ({"sad", "quiet", "tear", "cry", "sob", "mournful", "heavy"}, "sad"),
    ({"laugh", "grin", "amused", "chuckle", "smile", "wry"}, "amused"),
    ({"fond", "gentle", "soft", "warm", "tender", "affection"}, "warm"),
    ({"indignant", "offended", "affronted", "outraged", "huffs"}, "indignant"),
    ({"proud", "proudly", "triumphant", "puffed"}, "proud"),
    ({"solemn", "grave", "gravely", "reverent", "somber", "sombre"}, "solemn"),
    ({"stern", "firm", "commanding", "orders"}, "stern"),
    ({"deadpan", "flat", "flatly", "dry", "dryly", "sincere"}, "deadpan"),
    ({"sarcastic", "sarcasm", "eye-roll", "eyeroll", "mock"}, "sarcastic"),
    ({"nervous", "nervously", "hesitant", "stammer", "anxious", "uneasy"}, "nervous"),
    ({"confused", "puzzled", "baffled", "bewildered", "squint"}, "confused"),
    ({"excited", "eager", "thrilled", "delighted", "beaming"}, "excited"),
    ({"plead", "begging", "beg", "imploring"}, "pleading"),
    ({"tired", "weary", "exhausted", "yawn", "drained"}, "tired"),
    ({"disgust", "revolted", "grimace", "recoil"}, "disgusted"),
    ({"laughing", "laughs", "cackl", "giggl"}, "laughing"),
    ({"tearful", "choked", "voice cracking", "weeping"}, "tearful"),
    ({"oratory", "orating", "addresses the crowd", "speech", "proclaims", "declares"}, "oratory"),
    ({"drunk", "tipsy", "slurring"}, "drunk"),
]


def mood_of(direction, available):
    """Keyword-match direction → a mood present in ``available``.

    Falls back to ``"neutral"``.  Never returns a mood not in ``available``.
    """
    if not direction:
        return "neutral"
    d = direction.lower()
    for keywords, mood in _MOOD_KEYWORDS:
        if mood not in available:
            continue
        if any(re.search(r"\b" + re.escape(kw), d) for kw in keywords):
            return mood
    return "neutral"


# --------------------------------------------------------------------------- fish (S2-Pro)
# S2-Pro takes FREE-FORM instructions in square brackets, inline in the text — `[whisper]`,
# `[angry]`, `[professional broadcast tone]`. There is no fixed vocabulary to validate against.
# These reuse MOOD_WORDS (the same vocabulary VoiceDesign composes from) so the two engines
# cannot drift apart; MEASURED on forge 2026-10-14: `[whisper]` came back at half the level of
# the untagged take (RMS 0.043 vs 0.087) and `[angry]` differed again — the tags are real.

def fish_tag(mood):
    """The inline instruction S2-Pro reads for `mood`. ``""`` for neutral (no tag)."""
    return "[%s]" % MOOD_WORDS[mood] if MOOD_WORDS.get(mood) else ""


def fish_line_text(mood, text, direction=""):
    """One line's text as S2-Pro should receive it: the tag in front, words untouched.
    `direction` is the VOICE's own standing delivery note (voices page: "Fish direction",
    e.g. "slow, deadpan, slightly bored") — it goes in the same bracket, before the mood."""
    d = (direction or "").strip().strip("[]").strip()
    m = MOOD_WORDS.get(mood) or ""
    inner = "; ".join(x for x in (d, m) if x)
    return ("[%s] %s" % (inner, text)).strip() if inner else text


# --------------------------------------------------------------------------- voice CRUD

_VOICE_SCHEMA = {
    "name": "",
    "slug": "",
    "gender": "male",
    "accent": "",
    "notes": "",
    "age_years": 20,
    "sliders": {k: 0 for k in SLIDERS},
    "fx": {"semitones": 0.0, "tempo": 1.0, "room": 0.0},
    "moods": {"neutral": {"extra": "", "wav": "neutral.wav",
                          "ref_text": DEFAULT_SAMPLE}},
    "characters": [],
    "source": "bank",
}


def new_voice(name, **kw):
    """Create a new voice dict with defaults."""
    # ⚠️ DEEP copy: a shallow dict() shares the nested sliders/fx/moods dicts with the schema,
    # so moving one voice's slider silently changed the default for every later voice.
    v = json.loads(json.dumps(_VOICE_SCHEMA))
    v["name"] = name
    v["slug"] = slugify(name)
    v["characters"] = [name.upper()]
    for k, val in kw.items():
        if k in v and isinstance(v[k], dict):
            v[k].update(val)
        else:
            v[k] = val
    return v


def save_voice(v):
    """Save a voice dict to ``voices/<slug>/voice.json``.

    Raises ``ValueError`` if ``source`` is ``"character"`` (read-only).
    """
    if v.get("source") == "character":
        raise ValueError("Character-pack voices are read-only; use fork_voice() to copy.")
    vd = voices_dir()
    slug = v.get("slug") or slugify(v.get("name", "untitled"))
    d = os.path.join(vd, slug)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "voice.json"), "w", encoding="utf-8") as f:
        json.dump(v, f, indent=2)


def load_voice(slug):
    """Load a voice dict from ``voices/<slug>/voice.json``, or ``None``."""
    path = os.path.join(voices_dir(), slug, "voice.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_charpack_voices():
    """Import read-only voices from ``DATA/characters/<slug>/voice.json``."""
    chars_dir = os.path.join(_data(), "characters")
    if not os.path.isdir(chars_dir):
        return []
    result = []
    for slug in sorted(os.listdir(chars_dir)):
        vp = os.path.join(chars_dir, slug, "voice.json")
        if not os.path.isfile(vp):
            continue
        with open(vp, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Charpack format: {"description": ..., "line": ..., "file": "voice.wav"}
        # The plan's spec uses "line"; real data may use "ref_text".
        ref_text = (raw.get("line") or raw.get("ref_text") or DEFAULT_SAMPLE)
        wav_file = raw.get("file", "voice.wav")
        wav_path = os.path.join(chars_dir, slug, wav_file)
        voice = {
            "name": slug,
            "slug": slug,
            "gender": "male",         # unknown from charpack data
            "accent": "",
            "notes": raw.get("description", ""),
            "age_years": 30,          # unknown default
            "sliders": {k: 0 for k in SLIDERS},
            "fx": {"semitones": 0.0, "tempo": 1.0, "room": 0.0},
            "moods": {"neutral": {
                "extra": "",
                "wav": wav_file,
                "ref_text": ref_text,
            }},
            "characters": [slug.upper()],
            "source": "character",
            "_wav_path": wav_path if os.path.exists(wav_path) else None,
        }
        result.append(voice)
    return result


def list_voices():
    """List all voices (bank + charpack), sorted by name.

    Charpack voices appear as ``source:"character"`` and are read-only.
    """
    vd = voices_dir()
    result = []
    if os.path.isdir(vd):
        for slug in sorted(os.listdir(vd)):
            vp = os.path.join(vd, slug, "voice.json")
            if os.path.isfile(vp):
                with open(vp, "r", encoding="utf-8") as f:
                    result.append(json.load(f))

    # Append charpack voices
    result.extend(_load_charpack_voices())

    result.sort(key=lambda v: v.get("name", ""))
    return result


def fork_voice(v, name):
    """Create an editable (``source:"bank"``) copy of a voice."""
    new = json.loads(json.dumps(v))   # deep copy — never share nested dicts with the source
    new["name"] = name
    new["slug"] = slugify(name)
    new["source"] = "bank"
    new["characters"] = [name.upper()]
    # Ensure sliders and fx are present
    for key in SLIDERS:
        new.setdefault("sliders", {})[key] = new.get("sliders", {}).get(key, 0)
    new.setdefault("fx", {"semitones": 0.0, "tempo": 1.0, "room": 0.0})
    new.pop("_wav_path", None)
    return new


def voice_wav(v, mood):
    """Absolute path of a mood's reference wav, or ``None``.

    Returns mood-specific path if the file exists, else neutral, else None.
    """
    slug = v.get("slug") or slugify(v.get("name", ""))
    # Check charpack wav first (stored as absolute _wav_path)
    wav_abs = v.get("_wav_path")
    if wav_abs and mood in v.get("moods", {}):
        return wav_abs

    # Bank-style: `<voices>/<slug>/<mood>.wav`
    base = os.path.join(voices_dir(), slug)
    moods = v.get("moods", {})
    # Try the specific mood
    if mood in moods:
        wav_name = moods[mood].get("wav", "")
        if wav_name:
            path = os.path.join(base, wav_name)
            if os.path.exists(path):
                return path
    # Fall back to neutral
    if "neutral" in moods:
        wav_name = moods["neutral"].get("wav", "")
        if wav_name:
            path = os.path.join(base, wav_name)
            if os.path.exists(path):
                return path
    return None


# --------------------------------------------------------------------------- auto_cast

def auto_cast(speakers, voices, previous=None):
    """Speaker → voice slug.  Precedence: previous > characters > name."""
    result = {}
    previous = previous or {}

    for spk in speakers:
        slug = None

        # 1. Previous cast if the slug still exists
        prev_slug = previous.get(spk)
        if prev_slug and prev_slug in voices:
            slug = prev_slug

        # 2. Voice whose `characters` contains this speaker
        if slug is None:
            for s, v in voices.items():
                if spk in v.get("characters", []):
                    slug = s
                    break

        # 3. Voice whose name (upper-cased) == speaker
        if slug is None:
            for s, v in voices.items():
                if v.get("name", "").upper() == spk:
                    slug = s
                    break

        result[spk] = slug

    return result


# --------------------------------------------------------------------------- project CRUD

def new_project(name, script):
    """Create a new project dict, parsing the script."""
    p = {
        "name": name,
        "slug": slugify(name),
        "script": script,
        "cast": {},
        "gap_ms": 350,
        "lines": parse_script(script),
    }
    # Ensure takes fields exist
    for l in p["lines"]:
        l.setdefault("takes", [])
        l.setdefault("kept", 0)
    return p


def save_project(p):
    """Save a project to ``dialogue/<slug>/project.json``."""
    dd = dialogue_dir()
    slug = p.get("slug") or slugify(p.get("name", "untitled"))
    d = os.path.join(dd, slug)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)


def load_project(slug):
    """Load a project dict from ``dialogue/<slug>/project.json``, or ``None``."""
    path = os.path.join(dialogue_dir(), slug, "project.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_projects():
    """List all saved project slugs (sorted)."""
    dd = dialogue_dir()
    if not os.path.isdir(dd):
        return []
    projects = []
    for slug in sorted(os.listdir(dd)):
        if os.path.isfile(os.path.join(dd, slug, "project.json")):
            projects.append(slug)
    return projects


def reparse(p, script):
    """Re-parse a script and keep takes for lines with unchanged (speaker, text).

    Returns a NEW project dict (does not mutate the original).
    """
    old_lines = {l["idx"]: l for l in p.get("lines", [])}
    new_lines = parse_script(script)
    # Map old lines by (speaker, text) to preserve takes
    old_map = {}
    for l in p.get("lines", []):
        key = (normalise_speaker(l["speaker"]), l["text"])
        old_map[key] = l

    for l in new_lines:
        key = (normalise_speaker(l["speaker"]), l["text"])
        old = old_map.get(key)
        if old is not None:
            l["takes"] = old.get("takes", [])
            l["kept"] = old.get("kept", 0)
            # ⚠️ and the choices made in the LINES table. The window re-parses as you type, so
            # without this every keystroke in the script reset a line's mood / sung choice.
            for _k in ("mood", "sung"):
                if _k in old:
                    l[_k] = old[_k]
        else:
            l.setdefault("takes", [])
            l.setdefault("kept", 0)

    p = dict(p)
    p["script"] = script
    p["lines"] = new_lines
    return p


# --------------------------------------------------------------------------- fx_filter

def _chain_atempo(factor):
    """Build an ``atempo=...`` chain where each factor is within [0.5, 2.0].

    ffmpeg's atempo filter only supports 0.5..2.0, so factors outside this
    range must be split into a chain (e.g. 4.0 -> atempo=2.0,atempo=2.0).
    """
    if factor == 1.0:
        return ""
    parts = []
    f = factor
    while f > 2.0:
        parts.append("atempo=2")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    # Append the remainder (within [0.5, 2.0])
    if f != 1.0:
        parts.append("atempo=%.10g" % f)
    return ",".join(parts)


def fx_filter(fx):
    """Build an ffmpeg ``-af`` chain from the fx dict.

    Order: pitch (semitones), tempo, room.
    Returns ``""`` when all are neutral.
    """
    parts = []
    s = float(fx.get("semitones", 0))
    t = float(fx.get("tempo", 1.0))
    r = float(fx.get("room", 0.0))

    # Pitch: asetrate + aresample + atempo chain
    if s != 0:
        rate = 24000.0 * (2.0 ** (s / 12.0))
        factor = 1.0 / (2.0 ** (s / 12.0))
        chain = ["asetrate=%g" % rate, "aresample=24000"]
        atempo_chain = _chain_atempo(factor)
        if atempo_chain:
            chain.append(atempo_chain)
        parts.append(",".join(chain))

    # Tempo
    if t != 1.0:
        t_chain = _chain_atempo(t)
        if t_chain:
            parts.append(t_chain)

    # Room: aecho
    if r > 0:
        delay = int(20 + 60 * r)
        decay = round(0.15 + 0.35 * r, 2)
        parts.append("aecho=0.8:0.85:%d:%.2f" % (delay, decay))

    return ",".join(parts)


# --------------------------------------------------------------------------- peak_normalise

def peak_normalise(src, dst, ceiling_db=-3.0):
    """Scale a 16-bit mono PCM wav so the peak equals ``ceiling_db`` dBFS.

    A silent file (all-zero samples) is copied unchanged.
    """
    with wave.open(src, "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())

    samples = array.array("h")
    samples.frombytes(frames)

    peak = max(abs(s) for s in samples) if samples else 0
    if peak == 0:
        shutil.copy2(src, dst)
        return

    target = 32767.0 * (10.0 ** (ceiling_db / 20.0))
    scale = target / peak

    scaled = array.array("h", (int(s * scale) for s in samples))

    with wave.open(dst, "wb") as w:
        w.setparams(params)
        w.writeframes(scaled.tobytes())


# ------------------------------------------------------------------- subprocess safety

# ffmpeg does NOT die when its parent does.  A hung filter graph therefore
# becomes an orphaned, CPU-pegging runaway that ALSO fills the disk — e.g.
# `anullsrc` feeding `concat` via `atrim=duration=0` never reaches EOF and
# writes silence forever (observed: ~3 cores pinned, 5.6 GB in 15 s).
# Every ffmpeg call gets a hard timeout and is killed by process group.
FFMPEG_TIMEOUT = 900   # seconds; generous for a long scene, still bounded


def _run_ffmpeg(cmd, timeout=FFMPEG_TIMEOUT):
    """Run an ffmpeg command; kill its whole process group on timeout.

    Returns ffmpeg's stderr.  Raises RuntimeError (with the tail of stderr) on
    timeout or non-zero exit, so a bad filter graph fails loudly instead of
    spinning a runaway process.
    """
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True)
    try:
        _, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        p.communicate()
        raise RuntimeError(
            "ffmpeg timed out after %ss and was killed (bad filter graph?):\n"
            "  %s" % (timeout, " ".join(cmd)))
    if p.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed (exit %s):\n  %s\n%s"
            % (p.returncode, " ".join(cmd),
               err.decode("utf-8", "replace")[-2000:]))
    return err


# --------------------------------------------------------------------------- assemble

def assemble(paths, out, gap_ms=350, sr=24000):
    """Concat audio clips with silence gaps into one wav.

    Uses ffmpeg concat filter with ``anullsrc`` silence between clips.
    Returns total duration in seconds (measured by ffprobe).
    Returns 0.0 when ``paths`` is empty.
    """
    if not paths:
        return 0.0

    n = len(paths)
    gap_sec = gap_ms / 1000.0
    has_gap = gap_sec > 0

    if not has_gap:
        # Simple concat: no silence between clips
        cmd = ["ffmpeg", "-y"]
        for p in paths:
            cmd.extend(["-i", p])
        concat_inputs = "".join("[%d:a]" % i for i in range(n))
        cmd.extend([
            "-filter_complex",
            "%sconcat=n=%d:v=0:a=1[a]" % (concat_inputs, n),
            "-map", "[a]", "-ac", "1", "-ar", str(sr),
            "-c:a", "pcm_s16le", out,
        ])
        _run_ffmpeg(cmd)
        return _ffprobe_duration(out)

    # With gaps: insert anullsrc silence between clips
    total_seg = n * 2 - 1
    cmd = ["ffmpeg", "-y"]
    for i, p in enumerate(paths):
        cmd.extend(["-i", p])
        if i < n - 1:
            cmd.extend(["-f", "lavfi", "-i",
                        "anullsrc=cl=mono:r=%d" % sr])

    filters = []
    concat_inputs = []
    for i in range(n):
        clip_idx = i * 2
        concat_inputs.append("[%d:a]" % clip_idx)
        if i < n - 1:
            gap_idx = i * 2 + 1
            tag = "g%d" % i
            filters.append("[%d:a]atrim=duration=%g[%s]" % (gap_idx, gap_sec, tag))
            concat_inputs.append("[%s]" % tag)

    filter_complex = ";".join(filters + [
        "%sconcat=n=%d:v=0:a=1[a]" % ("".join(concat_inputs), total_seg)
    ])
    cmd.extend(["-filter_complex", filter_complex,
                "-map", "[a]", "-ac", "1", "-ar", str(sr),
                "-c:a", "pcm_s16le", out])
    _run_ffmpeg(cmd)
    return _ffprobe_duration(out)


def _ffprobe_duration(path):
    """Duration in seconds from ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1",
             path],
            capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except (ValueError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        return 0.0


# --------------------------------------------------------------------------- score

def score(intended, heard):
    """Similarity score (0..1) via difflib ratio on clean text.

    Lower-cases, strips punctuation, collapses whitespace.
    """
    def clean(t):
        t = t.lower()
        t = re.sub(r"[^\w\s]", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    return difflib.SequenceMatcher(None, clean(intended), clean(heard)).ratio()


# --------------------------------------------------------------------------- estimate_seconds

def estimate_seconds(n_lines, n_designs=0, asr=True, engine=DEFAULT_ENGINE):
    """Estimated wall-clock seconds for a dialogue batch.

    Constants are MEASURED live on forge (see the block above each); `n_designs` always runs
    on the qwen rig, because only VoiceDesign can make a voice from a description.
    """
    total = 0.0

    # VoiceDesign batch (if any new voice designs needed) — always the qwen path
    if n_designs > 0:
        total += DESIGN_LOAD_S + PER_DESIGN_S * n_designs

    # TTS generation (all lines)
    if engine == "drama3":
        total += DRAMA3_LOAD_S + DRAMA3_PER_LINE_S * n_lines
    elif engine == "fish":
        total += FISH_LOAD_S + FISH_PER_LINE_S * n_lines
    else:
        total += LOAD_S + PER_LINE_S * n_lines

    # ASR pass
    if asr and n_lines > 0:
        total += ASR_LOAD_S + PER_ASR_S * n_lines

    return total