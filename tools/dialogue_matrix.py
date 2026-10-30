#!/usr/bin/env python3
"""GPU-free assertions for dialogue.py — the DIALOGUE engine.

Tests EVERY row of the API table, plus the specific fixture cases from
Phase A.  No GPU, no network, no Tk.  Synthetic audio via stdlib wave+array,
real ffmpeg/ffprobe for the assemble test.

Run: /opt/homebrew/bin/python3 tools/dialogue_matrix.py   (exit 0 = green)
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import types
import wave
import array

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIALOGUE_PATH = os.path.join(ROOT, "dialogue.py")

FAILS = []
CHECKS = [0]


def check(cond, msg):
    CHECKS[0] += 1
    if cond:
        print("  ok  %s" % msg)
    else:
        print("FAIL  %s" % msg)
        FAILS.append(msg)


def near(a, b, tol=0.05):
    return abs(a - b) <= tol


def load_dialogue():
    """Load dialogue.py by compiling its source text, never a .pyc.

    ⚠️ Do NOT use importlib's SourceFileLoader: it trusts cached .pyc files
    that can silently go stale (same rationale as tools/audio_matrix.py).
    """
    with open(DIALOGUE_PATH, "rb") as f:
        src = f.read()
    mod = types.ModuleType("dialogue")
    mod.__file__ = DIALOGUE_PATH
    exec(compile(src, DIALOGUE_PATH, "exec"), mod.__dict__)
    return mod


def make_sine_wav(path, duration=1.0, freq=440, sr=24000, amplitude=16384):
    """Write a mono 16-bit WAV with a sine tone."""
    n = int(duration * sr)
    samples = array.array("h",
                          (int(amplitude * math.sin(2 * math.pi * freq * t / sr))
                           for t in range(n)))
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())


def make_silence_wav(path, duration=1.0, sr=24000):
    """Write a silent mono 16-bit WAV."""
    n = int(duration * sr)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n)


def ffprobe_duration(path):
    """Get duration in seconds via ffprobe."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
         path],
        capture_output=True, text=True)
    return float(r.stdout.strip())


# ---------------------------------------------------------------------------
# The 105 teaser fixture (scene 1 only)
FIXTURE = """> **AVERY (V.O.)** — *(quill scratching)* Survey of the Modern World. Entry Number Five: A parcel arrived for Thomas.
THOMAS holds a mangled box: a cracked blender that was supposed to be a coffee maker.
> **AVERY** — You will not complain?
> **THOMAS** — To who? It's a website.
> **AVERY** — A website is not a who.
> **AVERY** — *(picking up the invoice like a treaty)* I shall write them a letter.
**TITLE CARD: YOUNG AVERY**"""

EXPECTED_LINES = [
    {"idx": 0, "speaker": "AVERY (V.O.)", "direction": "quill scratching",
     "text": "Survey of the Modern World. Entry Number Five: A parcel arrived for Thomas."},
    {"idx": 1, "speaker": "AVERY", "direction": "",
     "text": "You will not complain?"},
    {"idx": 2, "speaker": "THOMAS", "direction": "",
     "text": "To who? It's a website."},
    {"idx": 3, "speaker": "AVERY", "direction": "",
     "text": "A website is not a who."},
    {"idx": 4, "speaker": "AVERY", "direction": "picking up the invoice like a treaty",
     "text": "I shall write them a letter."},
]

# ---------------------------------------------------------------------------


def main():
    # ---- Setup: throwaway data dir BEFORE loading dialogue ------------------
    tmp = tempfile.mkdtemp(prefix="dialogue_matrix_")
    os.environ["FORGE_LAB_DATA"] = tmp

    dlg = load_dialogue()

    print("=" * 70)
    print("1  parse_script")
    print("=" * 70)

    lines = dlg.parse_script(FIXTURE)
    check(len(lines) == 5, "fixture parses to 5 lines (got %d)" % len(lines))
    for i, exp in enumerate(EXPECTED_LINES):
        got = lines[i]
        check(got["idx"] == exp["idx"], "line %d idx = %d" % (i, exp["idx"]))
        check(got["speaker"] == exp["speaker"],
              "line %d speaker = %r (got %r)" % (i, exp["speaker"], got["speaker"]))
        check(got["direction"] == exp["direction"],
              "line %d direction = %r (got %r)" % (i, exp["direction"], got["direction"]))
        check(got["text"] == exp["text"],
              "line %d text = %r (got %r)" % (i, exp["text"], got["text"]))

    # Action & heading lines are skipped
    check(lines[0]["idx"] == 0, "first line idx = 0")
    check(lines[1]["idx"] == 1, "skipped action line, next idx = 1")

    # Colon format
    col_lines = dlg.parse_script("AVERY: Hello there.\nTHOMAS (V.O.): Over here.")
    check(len(col_lines) == 2, "colon format: 2 lines (got %d)" % len(col_lines))
    if len(col_lines) >= 2:
        check(col_lines[0]["speaker"] == "AVERY", "colon format speaker (got %r)"
              % col_lines[0]["speaker"])
        check(col_lines[1]["speaker"] == "THOMAS (V.O.)",
              "colon format V.O. speaker (got %r)" % col_lines[1]["speaker"])

    # Em-dash / en-dash / plain dash separators
    for sep in [" — ", " – ", " - "]:
        txt = "AVERY%stext" % sep
        r = dlg.parse_script(txt)
        check(len(r) == 1 and r[0]["speaker"] == "AVERY",
              "%r separator works" % sep)

    # Action lines (no speaker prefix) are skipped
    act = dlg.parse_script("The room was quiet.\n\nAVERY: Hello.")
    check(len(act) == 1, "action line skipped (got %d)" % len(act))
    check(act[0]["speaker"] == "AVERY", "only the dialogue line kept")

    # "---" and TITLE CARD are skipped
    junk = dlg.parse_script("---\n**TITLE CARD: TEST**\nAVERY: Hi.")
    check(len(junk) == 1, "--- and TITLE CARD skipped (got %d)" % len(junk))

    # Markdown formatting stripped from text
    md = dlg.parse_script("AVERY: **bold** and *italic* and _underscore_.")
    check(len(md) == 1, "markdown line parses")
    if md:
        check("*" not in md[0]["text"] and "_" not in md[0]["text"],
              "markdown markers stripped (got %r)" % md[0]["text"])

    # Direction inside parentheses at start extracted; inline parens removed
    par = dlg.parse_script("AVERY: *(alarmed)* Help *now*.")
    check(len(par) == 1, "parenthetical direction parses")
    if par:
        check(par[0]["direction"] == "alarmed",
              "direction extracted (got %r)" % par[0]["direction"])
        check("*" not in par[0]["text"] and "Help" in par[0]["text"],
              "inline directions stripped from text (got %r)" % par[0]["text"])

    # Speaker is 1-4 words, ALL CAPS
    cap = dlg.parse_script("SUPPORT AGENT: Hello.\nJOSEPH: Hi.")
    check(len(cap) == 2, "multi-word speaker name works")

    # Non-matching lines without speaker prefix
    no_speaker = dlg.parse_script("He walks to the door.\n\nHe sits down.")
    check(len(no_speaker) == 0, "non-dialogue lines produce 0 lines")

    print("")
    print("=" * 70)
    print("2  normalise_speaker")
    print("=" * 70)

    check(dlg.normalise_speaker("Avery (V.O.)") == "AVERY",
          "(V.O.) stripped: Avery (V.O.) -> AVERY")
    check(dlg.normalise_speaker("Avery (O.S.)") == "AVERY",
          "(O.S.) stripped")
    check(dlg.normalise_speaker("Avery (CONT'D)") == "AVERY",
          "(CONT'D) stripped")
    check(dlg.normalise_speaker("Avery (O.C.)") == "AVERY",
          "(O.C.) stripped")
    check(dlg.normalise_speaker("avery") == "AVERY",
          "lowercase uppercased")
    check(dlg.normalise_speaker("  Avery  ") == "AVERY",
          "extra spaces stripped")
    check(dlg.normalise_speaker("SUPPORT AGENT") == "SUPPORT AGENT",
          "multi-word name preserved")

    print("")
    print("=" * 70)
    print("3  speakers()")
    print("=" * 70)

    spk = dlg.speakers(lines)
    check(spk == ["AVERY", "THOMAS"],
          "unique speakers in first-appearance order (got %r)" % spk)

    # The V.O. line is folded: normalise removes the suffix -> AVERY
    norm_spk = [dlg.normalise_speaker(l["speaker"]) for l in lines]
    unique = []
    for s in norm_spk:
        if s not in unique:
            unique.append(s)
    check(unique == ["AVERY", "THOMAS"],
          "V.O. folded: speakers = %r" % unique)

    print("")
    print("=" * 70)
    print("4  lint_line")
    print("=" * 70)

    # Digits
    w = dlg.lint_line("Two 3090s")
    check(len(w) > 0, "digits produce warnings")
    any_digit = any("3090" in m or "digit" in m.lower() for m in w)
    check(any_digit, "lint warns about digits (got %r)" % w)

    # ALL-CAPS words (>=2 letters, not I/OK)
    w2 = dlg.lint_line("HELLO WORLD I OK")
    cap_warnings = [m for m in w2 if "spelled" in m.lower() or "ALL-CAPS" in m]
    check(len(cap_warnings) >= 1, "ALL-CAPS words flagged (got %r)" % cap_warnings)

    # Symbols
    w3 = dlg.lint_line("Cost: $50 & 10% off / visit http://x.com")
    sym_warnings = [m for m in w3
                    if any(c in m for c in ("$", "&", "%", "/", "http"))]
    check(len(sym_warnings) >= 1, "symbols flagged (got %r)" % sym_warnings)

    # Long line
    long_text = "Hello " * 60  # > 300 chars
    w4 = dlg.lint_line(long_text)
    long_w = [m for m in w4 if "split" in m.lower() or "long" in m.lower()]
    check(len(long_w) >= 1,
          "lines over 300 chars flagged (got %r)" % long_w)

    # Clean line
    clean = dlg.lint_line("Hello, how are you?")
    check(len(clean) == 0, "clean line produces no warnings (got %r)" % clean)

    print("")
    print("=" * 70)
    print("5  slugify")
    print("=" * 70)

    check(dlg.slugify("Hello World!") == "hello-world",
          "basic slug: Hello World! -> hello-world")
    check(dlg.slugify("  AVERY  ") == "avery",
          "whitespace stripped")
    check(dlg.slugify("Big   Spaces") == "big-spaces",
          "multiple spaces collapsed")
    check(dlg.slugify("A--B") == "a-b",
          "repeated dashes collapsed")
    check(dlg.slugify("") == "untitled",
          "empty string -> untitled")
    check(dlg.slugify("What's up?") == "whats-up",
          "apostrophe stripped")
    check("/" not in dlg.slugify("../etc"),
          "path separators removed")
    check(dlg.slugify("Ms. Rose") == "ms-rose",
          "dot becomes dash")

    print("")
    print("=" * 70)
    print("6  SLIDERS / constants")
    print("=" * 70)

    check(isinstance(dlg.SLIDERS, tuple), "SLIDERS is a tuple")
    check(len(dlg.SLIDERS) == 6, "SLIDERS has 6 entries (got %d)" % len(dlg.SLIDERS))
    check(dlg.SLIDERS == ("pitch", "pace", "energy", "age", "warmth", "grit"),
          "SLIDERS order correct (got %r)" % (dlg.SLIDERS,))

    check(isinstance(dlg.SLIDER_WORDS, dict), "SLIDER_WORDS is a dict")
    check(dlg.SLIDER_WORDS["pace"][0] == "",
          "pace 0 -> empty string")
    check("no dramatic pauses" in dlg.SLIDER_WORDS["pace"][2],
          "pace +2 contains 'no dramatic pauses'")
    check("slow, unhurried" in dlg.SLIDER_WORDS["pace"][-2],
          "pace -2 has slow/unhurried")
    check(dlg.SLIDER_WORDS["pitch"][-2] == "very low",
          "pitch -2 = very low")
    check(dlg.SLIDER_WORDS["pitch"][2] == "very high",
          "pitch +2 = very high")
    check(dlg.SLIDER_WORDS["energy"][-2] == "subdued",
          "energy -2 = subdued")
    check(dlg.SLIDER_WORDS["energy"][2] == "very energetic",
          "energy +2 = very energetic")

    print("")
    print("=" * 70)
    print("7  compose_description")
    print("=" * 70)

    v = dlg.new_voice("Test", gender="male", accent="slight Virginia",
                       age_years=20,
                       notes="formal 18th-century cadence")
    desc = dlg.compose_description(v)
    check(desc.startswith("A "), "description starts with 'A ' (got %r)" % desc[:5])
    check("20-year-old male voice" in desc,
          "contains age/gender (got %r)" % desc)
    check("slight Virginia accent" in desc,
          "contains accent (got %r)" % desc)
    check("formal 18th-century cadence" in desc,
          "contains notes (got %r)" % desc)
    check(desc.endswith("."), "ends with period")
    check(", ," not in desc, "no double commas (got %r)" % desc)

    # Ryan 2026-10-21: "the accent is not working, it all sounds like plain english" --
    # the accent leads (right after age/gender), is said strongly, and a closing sentence
    # names the language it is spoken in. "neutral" gets none of that.
    vs = dlg.new_voice("Scot", gender="female", accent="Scottish", age_years=40,
                       notes="dry, amused")
    ds = dlg.compose_description(vs)
    check(ds.startswith("A 40-year-old female voice with a strong, unmistakable Scottish "
                        "accent") and ds.index("Scottish") < ds.index("dry, amused"),
          "the accent comes first and strongly (got %r)" % ds[:90])
    check("She speaks English with a heavy Scottish accent" in ds and
          "never neutral standard English" in ds,
          "...and is reinforced in the language it's spoken in (got %r)" % ds[-110:])
    check("heavy slight" not in desc and "strong, unmistakable slight" not in desc,
          "a typed strength ('slight') is not stacked under 'strong' (got %r)" % desc)
    dn = dlg.compose_description(dlg.new_voice("N", gender="female", accent="neutral",
                                               age_years=30, notes=""))
    check("speaks" not in dn and "unmistakable" not in dn,
          "a neutral accent gets no accent push (got %r)" % dn)
    check(dlg.compose_description(dlg.new_voice("Kid", gender="male", age_years=8))
          .startswith("An 8-year-old"), "An 8-year-old")

    # All-zero sliders = no slider phrases at all
    v2 = dlg.new_voice("Zero", gender="female", accent="neutral",
                        age_years=30, notes="")
    desc2 = dlg.compose_description(v2)
    check(",  " not in desc2 and ", ," not in desc2,
          "no empty slider parts (got %r)" % desc2)

    # Mood extra included
    v3 = dlg.new_voice("Moody", gender="male", age_years=25,
                        accent="neutral", notes="")
    desc3 = dlg.compose_description(v3, mood="alarmed")
    check("alarmed" in desc3 or "urgent" in desc3 or "breath" in desc3,
          "mood extra included (got %r)" % desc3)

    # Deterministic
    d1 = dlg.compose_description(v)
    d2 = dlg.compose_description(v)
    check(d1 == d2, "deterministic: same input -> same output")

    print("")
    print("=" * 70)
    print("8  DEFAULT_SAMPLE / MOOD_WORDS")
    print("=" * 70)

    check(isinstance(dlg.DEFAULT_SAMPLE, str) and len(dlg.DEFAULT_SAMPLE.split()) >= 10,
          "DEFAULT_SAMPLE is a ~12-word string (got %d words)"
          % len(dlg.DEFAULT_SAMPLE.split()))

    check(isinstance(dlg.MOOD_WORDS, dict), "MOOD_WORDS is a dict")
    check("neutral" in dlg.MOOD_WORDS, "neutral in MOOD_WORDS")
    check(dlg.MOOD_WORDS["neutral"] == "", "neutral -> empty string")
    for mood in ("alarmed", "angry", "warm", "sad", "shouting", "whisper", "amused"):
        check(mood in dlg.MOOD_WORDS, "%s in MOOD_WORDS" % mood)
        check(dlg.MOOD_WORDS[mood] != "", "%s has non-empty phrase" % mood)

    print("")
    print("=" * 70)
    print("9  mood_of")
    print("=" * 70)

    avail = {"neutral", "shouting", "whisper", "alarmed", "angry", "sad",
             "amused", "warm"}
    check(dlg.mood_of("He shouts angrily", avail) == "shouting",
          "shout -> shouting")
    check(dlg.mood_of("whispered softly", avail) == "whisper",
          "whisper -> whisper")
    check(dlg.mood_of("alarmed and startled", avail) == "alarmed",
          "alarmed -> alarmed")
    check(dlg.mood_of("furious yelling", avail) == "angry",
          "furious -> angry")
    check(dlg.mood_of("quiet and tearful", avail) == "sad",
          "tear -> sad")
    check(dlg.mood_of("grinning", avail) == "amused",
          "grin -> amused")
    check(dlg.mood_of("gentle and fond", avail) == "warm",
          "fond -> warm")
    check(dlg.mood_of("nothing matches", avail) == "neutral",
          "no match falls back to neutral")
    check(dlg.mood_of("shout", {"neutral", "warm"}) == "neutral",
          "never returns unavailable mood")

    print("")
    print("=" * 70)
    print("10  voices_dir / dialogue_dir")
    print("=" * 70)

    vd = dlg.voices_dir()
    dd = dlg.dialogue_dir()
    check(vd == os.path.join(tmp, "voices"), "voices_dir() = DATA/voices")
    check(dd == os.path.join(tmp, "dialogue"), "dialogue_dir() = DATA/dialogue")
    check(os.environ.get("FORGE_LAB_DATA") == tmp,
          "FORGE_LAB_DATA read from env at call time")
    check(vd.startswith(tmp) and dd.startswith(tmp),
          "paths are inside the test data dir, never real ~/forge-lab")

    # Env read at call time — changing the env changes the result
    tmp2 = tempfile.mkdtemp(prefix="dialogue_matrix_2_")
    old = os.environ["FORGE_LAB_DATA"]
    os.environ["FORGE_LAB_DATA"] = tmp2
    check(dlg.voices_dir() == os.path.join(tmp2, "voices"),
          "changing FORGE_LAB_DATA changes voices_dir()")
    os.environ["FORGE_LAB_DATA"] = old

    # Default when env not set
    os.environ.pop("FORGE_LAB_DATA", None)
    default_vd = dlg.voices_dir()
    check(default_vd == os.path.join(os.path.expanduser("~"), "forge-lab", "voices"),
          "no env -> ~/forge-lab/voices")
    os.environ["FORGE_LAB_DATA"] = tmp  # restore

    print("")
    print("=" * 70)
    print("11  new_voice")
    print("=" * 70)

    v = dlg.new_voice("Avery Test")
    check(v["name"] == "Avery Test", "name set (got %r)" % v["name"])
    check(v["slug"] == "avery-test", "slug auto-generated (got %r)" % v["slug"])
    check(v["characters"] == ["AVERY TEST"],
          "characters auto-set (got %r)" % v["characters"])
    check(v["source"] == "bank", "source = 'bank' (got %r)" % v["source"])
    check(isinstance(v["sliders"], dict) and v["sliders"]["pitch"] == 0,
          "sliders defaults all zero")
    check(isinstance(v["fx"], dict) and v["fx"]["tempo"] == 1.0,
          "fx defaults")
    check(isinstance(v["moods"], dict) and "neutral" in v["moods"],
          "neutral mood present by default")
    check(v["gender"] == "male", "default gender = male (got %r)" % v["gender"])
    check(v["age_years"] == 20, "default age_years = 20 (got %r)" % v["age_years"])
    check(v["accent"] == "", "default accent = ''")
    check(v["notes"] == "", "default notes = ''")

    # Override via **kw
    v2 = dlg.new_voice("Jane", gender="female", age_years=30,
                        accent="British", notes="test note")
    check(v2["gender"] == "female", "kw overrides gender")
    check(v2["age_years"] == 30, "kw overrides age")
    check(v2["accent"] == "British", "kw overrides accent")

    print("")
    print("=" * 70)
    print("12  save_voice / load_voice / list_voices / charpack import")
    print("=" * 70)

    v = dlg.new_voice("Avery Test")
    dlg.save_voice(v)
    loaded = dlg.load_voice("avery-test")
    check(loaded is not None, "load_voice returns the saved voice")
    if loaded:
        check(loaded["name"] == "Avery Test", "name round-trips")
        check(loaded["slug"] == "avery-test", "slug round-trips")

    # list_voices
    v2 = dlg.new_voice("Thomas", gender="male", accent="neutral")
    dlg.save_voice(v2)
    lst = dlg.list_voices()
    check(len(lst) >= 2, "list_voices has 2+ voices (got %d)" % len(lst))
    names = [(v["name"], v["slug"]) for v in lst]
    check(("Avery Test", "avery-test") in names, "Avery Test in list")
    check(("Thomas", "thomas") in names, "Thomas in list")
    # Sorted by name
    check(lst[0]["name"] <= lst[-1]["name"],
          "list sorted by name (got %r)" % [v["name"] for v in lst])

    # Charpack voice import (source:"character")
    char_dir = os.path.join(tmp, "characters", "bighead")
    os.makedirs(char_dir, exist_ok=True)
    char_wav = os.path.join(char_dir, "voice.wav")
    make_sine_wav(char_wav, duration=0.5, amplitude=8000)
    char_json = {
        "description": "A deep voice",
        "line": "This is a test line.",
        "file": "voice.wav"
    }
    with open(os.path.join(char_dir, "voice.json"), "w") as f:
        json.dump(char_json, f)

    lst2 = dlg.list_voices()
    char_voices = [v for v in lst2 if v["source"] == "character"]
    check(len(char_voices) >= 1, "charpack voice imported in list_voices")
    if char_voices:
        cv = char_voices[0]
        check(cv["name"] == "bighead" or "bighead" in cv["slug"],
              "charpack voice name from slug (got %r)" % cv["name"])
        check(cv["source"] == "character", "source is 'character'")
        check("neutral" in cv["moods"], "charpack has neutral mood")
        check(cv["moods"]["neutral"]["ref_text"] == "This is a test line.",
              "charpack ref_text from 'line' field (got %r)"
              % cv["moods"]["neutral"]["ref_text"])

    # save_voice refuses source:"character"
    if char_voices:
        try:
            dlg.save_voice(char_voices[0])
            check(False, "save_voice should raise ValueError on character voice")
        except ValueError:
            check(True, "save_voice raises ValueError on character voice")
        except Exception as e:
            check(False, "save_voice raised %s (expected ValueError)" % type(e).__name__)

    # fork_voice creates an editable copy
    if char_voices:
        forked = dlg.fork_voice(char_voices[0], "Forked Bighead")
        check(forked["source"] == "bank",
              "forked voice has source='bank' (got %r)" % forked["source"])
        check(forked["name"] == "Forked Bighead", "forked voice uses new name")
        check(forked["slug"] == "forked-bighead", "forked voice has new slug")
        check("neutral" in forked["moods"],
              "forked voice keeps moods from original")
        # Now we CAN save it
        try:
            dlg.save_voice(forked)
            check(True, "forked voice can be saved")
        except ValueError:
            check(False, "forked voice should be saveable")

    print("")
    print("=" * 70)
    print("13  voice_wav")
    print("=" * 70)

    v = dlg.new_voice("Wav Test")
    # No files created yet → None
    check(dlg.voice_wav(v, "neutral") is None,
          "no files yet -> voice_wav returns None")

    # Create neutral wav
    neut_path = os.path.join(dlg.voices_dir(), v["slug"], "neutral.wav")
    os.makedirs(os.path.dirname(neut_path), exist_ok=True)
    make_sine_wav(neut_path, duration=0.3)
    check(dlg.voice_wav(v, "neutral") == neut_path,
          "voice_wav returns neutral path")
    check(dlg.voice_wav(v, "angry") == neut_path,
          "missing mood falls back to neutral")

    # Add angry mood, create angry wav
    v["moods"]["angry"] = {"extra": "angry, clipped", "wav": "angry.wav",
                            "ref_text": "I am angry."}
    angry_path = os.path.join(dlg.voices_dir(), v["slug"], "angry.wav")
    make_sine_wav(angry_path, duration=0.3)
    check(dlg.voice_wav(v, "angry") == angry_path,
          "voice_wav returns mood-specific path when it exists")

    print("")
    print("=" * 70)
    print("14  auto_cast")
    print("=" * 70)

    speakers = ["AVERY", "THOMAS", "NATE"]
    voices_list = [
        dlg.new_voice("Thomas", gender="male"),
        dlg.new_voice("Avery", gender="male"),
        dlg.new_voice("Joseph", gender="male"),
    ]
    # Set characters for some
    voices_list[0]["characters"] = ["THOMAS", "TOM"]
    voices_list[1]["characters"] = ["AVERY"]
    voices_list[2]["characters"] = ["JOSEPH"]

    # Map by slug name
    voices_by_slug = {v["slug"]: v for v in voices_list}

    cast = dlg.auto_cast(speakers, voices_by_slug)
    check(cast.get("AVERY") == "avery",
          "auto_cast AVERY -> avery (got %r)" % cast.get("AVERY"))
    check(cast.get("THOMAS") == "thomas",
          "auto_cast THOMAS -> thomas (got %r)" % cast.get("THOMAS"))
    check(cast.get("NATE") is None,
          "NATE has no match -> None (got %r)" % cast.get("NATE"))

    # Previous cast takes precedence
    prev = {"NATE": "nate_saved", "AVERY": "old_avery"}
    # Create dummy voices for the previous slugs so they "still exist"
    v_nate_saved = dlg.new_voice("nate_saved", gender="male", accent="")
    v_old_avery = dlg.new_voice("old_avery", gender="male", accent="")
    expanded_voices = dict(voices_by_slug)
    expanded_voices["nate_saved"] = v_nate_saved
    expanded_voices["old_avery"] = v_old_avery
    cast2 = dlg.auto_cast(speakers, expanded_voices, previous=prev)
    check(cast2.get("NATE") == "nate_saved",
          "previous cast preserved for NATE (slug exists)")
    check(cast2.get("AVERY") == "old_avery",
          "previous cast preserved for AVERY (slug exists)")

    # When previous is not set, auto-cast kicks in
    check(cast2.get("THOMAS") == "thomas",
          "THOMAS auto-cast when no previous mapping")

    # When previous slug does NOT exist, fall through to characters/name
    cast_fallthrough = dlg.auto_cast(speakers, voices_by_slug, previous=prev)
    check(cast_fallthrough.get("NATE") is None,
          "NATE unchanged when previous slug does not exist")
    check(cast_fallthrough.get("AVERY") == "avery",
          "AVERY auto-cast when previous slug does not exist")

    # Precedence: previous > characters > name (all slugs exist)
    prev3 = {"AVERY": "thomas"}  # Thomas voice has AVERY in characters
    cast3 = dlg.auto_cast(speakers, expanded_voices, previous=prev3)
    check(cast3.get("AVERY") == "thomas",
          "previous cast takes priority even over characters match")

    print("")
    print("=" * 70)
    print("15  new_project / save_project / load_project / list_projects / reparse")
    print("=" * 70)

    proj = dlg.new_project("105 Teaser",
                           "AVERY: Line one.\nTHOMAS: Line two.")
    check(proj["name"] == "105 Teaser",
          "project name set (got %r)" % proj["name"])
    check(proj["slug"] == "105-teaser",
          "project slug auto-generated (got %r)" % proj["slug"])
    check(len(proj["lines"]) == 2, "project parsed to 2 lines")
    check(proj["cast"] == {}, "cast starts empty")
    check(proj["gap_ms"] == 350, "default gap_ms = 350")

    dlg.save_project(proj)
    loaded_p = dlg.load_project("105-teaser")
    check(loaded_p is not None, "load_project returns saved project")
    if loaded_p:
        check(loaded_p["name"] == "105 Teaser", "project name round-trips")
        check(len(loaded_p["lines"]) == 2, "lines round-trip")

    # list_projects
    proj2 = dlg.new_project("Another", "NATE: Yes.")
    dlg.save_project(proj2)
    plist = dlg.list_projects()
    check(len(plist) >= 2, "list_projects has 2+ projects (got %d)" % len(plist))

    # reparse keeps takes for unchanged lines
    p3 = dlg.new_project("Reparse Test", "AVERY: Hello.\nTHOMAS: Goodbye.")
    p3["lines"][0]["takes"] = [{"file": "lines/000_1.wav", "seconds": 1.0,
                                "score": 0.95}]
    p3["lines"][1]["takes"] = [{"file": "lines/001_1.wav", "seconds": 1.5,
                                "score": 0.90}]
    p3["lines"][0]["kept"] = 0
    p3["lines"][1]["kept"] = 0

    # Re-parse with unchanged first line, changed second line
    new_script = "AVERY: Hello.\nTHOMAS: See you later."
    p4 = dlg.reparse(p3, new_script)
    check(len(p4["lines"]) == 2, "reparse keeps 2 lines")
    check(len(p4["lines"][0]["takes"]) == 1,
          "unchanged line keeps its takes (got %d)"
          % len(p4["lines"][0]["takes"]))
    check(len(p4["lines"][1]["takes"]) == 0,
          "changed line loses its takes (got %d)"
          % len(p4["lines"][1]["takes"]))

    # reparse with added line
    p5 = dlg.reparse(p3, "AVERY: Hello.\nTHOMAS: Goodbye.\nNATE: Added.")
    check(len(p5["lines"]) == 3, "reparse with added line -> 3 lines")
    check(len(p5["lines"][0]["takes"]) == 1, "first line keeps takes")
    check(len(p5["lines"][1]["takes"]) == 1, "second line keeps takes")
    check(len(p5["lines"][2]["takes"]) == 0, "added line has no takes")

    # reparse keeps the LINES-table choices too: the window re-parses as you type, so a
    # mood picked in the table must survive an edit elsewhere in the script
    p3["lines"][0]["mood"] = "angry"
    p3["lines"][1]["sung"] = True
    p6 = dlg.reparse(p3, "AVERY: Hello.\nTHOMAS: Goodbye.\nNATE: Added.")
    check(p6["lines"][0].get("mood") == "angry",
          "an unchanged line keeps its chosen mood (got %r)" % p6["lines"][0].get("mood"))
    check(p6["lines"][1].get("sung") is True, "an unchanged line keeps its sung choice")
    p7 = dlg.reparse(p3, "AVERY: Hello there.\nTHOMAS: Goodbye.")
    check(p7["lines"][0].get("mood") != "angry",
          "a CHANGED line starts fresh (mood %r)" % p7["lines"][0].get("mood"))

    print("")
    print("=" * 70)
    print("16  fx_filter")
    print("=" * 70)

    # Neutral = ""
    check(dlg.fx_filter({"semitones": 0, "tempo": 1.0, "room": 0}) == "",
          "neutral fx -> empty string")

    # Semitones != 0
    fx = dlg.fx_filter({"semitones": 12, "tempo": 1.0, "room": 0})
    check("asetrate=" in fx, "semitones: asetrate in filter (got %r)" % fx)
    check("aresample=24000" in fx, "semitones: aresample in filter")
    check("atempo=" in fx, "semitones: atempo in filter")

    # +12 semitones: atempo factor = 1/2 = 0.5, within [0.5,2.0]
    # Extract atempo factors and check each is within bounds
    def extract_atempo_factors(fx_str):
        """Return list of atempo values from a filter string."""
        import re
        parts = fx_str.split(",")
        factors = []
        for p in parts:
            m = re.match(r"atempo=([\d.]+)", p.strip())
            if m:
                factors.append(float(m.group(1)))
        return factors

    factors = extract_atempo_factors(fx)
    check(all(0.5 <= f <= 2.0 for f in factors),
          "all atempo factors within [0.5, 2.0] (got %r)" % factors)
    # For +12 semitones: 2^(12/12)=2, atempo needed=1/2=0.5 -> single factor
    # But let's test a more extreme case
    fx_big = dlg.fx_filter({"semitones": 24, "tempo": 1.0, "room": 0})
    big_factors = extract_atempo_factors(fx_big)
    check(all(0.5 <= f <= 2.0 for f in big_factors),
          "extreme semitones: all factors within [0.5,2.0] (got %r)" % big_factors)
    # 1/2^(24/12) = 1/4 = 0.25, which needs to be split: 0.5, 0.5
    check(len(big_factors) >= 2, "extreme semitones needs multiple atempo factors")

    # Tempo != 1
    fx_t = dlg.fx_filter({"semitones": 0, "tempo": 2.0, "room": 0})
    check("atempo=2.0" in fx_t or "atempo=2" in fx_t,
          "tempo 2.0 produces atempo=2.0")

    # Room > 0
    fx_r = dlg.fx_filter({"semitones": 0, "tempo": 1.0, "room": 0.5})
    check("aecho=" in fx_r, "room > 0 produces aecho (got %r)" % fx_r)
    check("0.8:0.85:" in fx_r, "aecho params match spec (got %r)" % fx_r)

    # Order: pitch, tempo, room
    fx_all = dlg.fx_filter({"semitones": -3, "tempo": 1.2, "room": 0.3})
    asetre_idx = fx_all.find("asetrate")
    atempo_idx = fx_all.find("atempo=")
    aecho_idx = fx_all.find("aecho=")
    check(asetre_idx >= 0 and atempo_idx >= 0 and aecho_idx >= 0,
          "all three fx present")
    check(asetre_idx < atempo_idx < aecho_idx,
          "order: pitch before tempo before room")

    print("")
    print("=" * 70)
    print("17  peak_normalise")
    print("=" * 70)

    # Synthetic wav at half amplitude (-6 dBFS) -> peak_normalise -> -3 dBFS
    src = os.path.join(tmp, "peak_src.wav")
    dst = os.path.join(tmp, "peak_dst.wav")
    # Half amplitude = 16384 (out of 32767 max)
    make_sine_wav(src, duration=0.5, amplitude=16384)
    dlg.peak_normalise(src, dst, ceiling_db=-3.0)

    with wave.open(dst, "rb") as w:
        frames = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    actual_peak = max(abs(s) for s in samples)
    expected_peak = int(32767 * 10 ** (-3.0 / 20))  # ~23200
    check(near(actual_peak, expected_peak, tol=expected_peak * 0.05),
          "peak_normalise peak=%d, expected ~%d"
          % (actual_peak, expected_peak))

    # Silent file copied unchanged
    silent_src = os.path.join(tmp, "silent_src.wav")
    silent_dst = os.path.join(tmp, "silent_dst.wav")
    make_silence_wav(silent_src, duration=0.5)
    dlg.peak_normalise(silent_src, silent_dst)
    with wave.open(silent_dst, "rb") as w:
        sil_frames = w.readframes(w.getnframes())
    check(all(b == 0 for b in sil_frames),
          "silent file stays silent after peak_normalise")

    print("")
    print("=" * 70)
    print("18  assemble")
    print("=" * 70)

    # Two 1-second clips + 350 ms gap -> 2.35 s ± 0.05
    clip1 = os.path.join(tmp, "assemble_clip1.wav")
    clip2 = os.path.join(tmp, "assemble_clip2.wav")
    assembled = os.path.join(tmp, "assemble_out.wav")
    make_sine_wav(clip1, duration=1.0, freq=440, amplitude=10000)
    make_sine_wav(clip2, duration=1.0, freq=880, amplitude=8000)

    total = dlg.assemble([clip1, clip2], assembled, gap_ms=350, sr=24000)
    dur = ffprobe_duration(assembled)
    check(near(dur, 2.35, 0.06),
          "assemble duration ~2.35 s (got %.3f s)" % dur)
    check(near(total, dur, 0.06),
          "returned total (%.3f) matches ffprobe (%.3f)" % (total, dur))

    # Single clip (no gap)
    single_out = os.path.join(tmp, "assemble_single.wav")
    total2 = dlg.assemble([clip1], single_out, gap_ms=350, sr=24000)
    dur2 = ffprobe_duration(single_out)
    check(near(dur2, 1.0, 0.06),
          "single clip assemble ~1.0 s (got %.3f)" % dur2)

    # Empty list -> 0
    check(dlg.assemble([], os.path.join(tmp, "empty_out.wav")) == 0.0,
          "empty paths -> 0.0")

    # Gap 0 (no silence between clips)
    zero_gap = os.path.join(tmp, "assemble_zero_gap.wav")
    total3 = dlg.assemble([clip1, clip2], zero_gap, gap_ms=0, sr=24000)
    dur3 = ffprobe_duration(zero_gap)
    check(near(dur3, 2.0, 0.06),
          "zero gap ~2.0 s (got %.3f)" % dur3)

    print("")
    print("=" * 70)
    print("19  score / PASS_SCORE")
    print("=" * 70)

    check(dlg.PASS_SCORE == 0.85,
          "PASS_SCORE = 0.85 (got %r)" % dlg.PASS_SCORE)
    check(near(dlg.score("Hello world", "Hello world"), 1.0),
          "identical strings score 1.0")
    check(near(dlg.score("Hello world.", "hello world"), 1.0),
          "case & punctuation treated as match")
    s = dlg.score("I shall write them a letter.",
                  "I should write them a letter")
    check(s < 1.0 and s > 0.0,
          "similar but not identical: score in (0,1) (got %.4f)" % s)

    print("")
    print("=" * 70)
    print("20  estimate_seconds")
    print("=" * 70)

    e0 = dlg.estimate_seconds(n_lines=0)
    check(e0 >= 0, "estimate(0 lines) >= 0 (got %.1f)" % e0)

    e10 = dlg.estimate_seconds(n_lines=10)
    e20 = dlg.estimate_seconds(n_lines=20)
    check(e20 > e10,
          "estimate grows with n_lines (10=>%.1f, 20=>%.1f)" % (e10, e20))

    e_design = dlg.estimate_seconds(n_lines=10, n_designs=2)
    check(e_design > e10,
          "estimate with designs > without (got %.1f vs %.1f)" % (e_design, e10))

    e_no_asr = dlg.estimate_seconds(n_lines=10, asr=False)
    check(e_no_asr <= e10,
          "estimate without ASR <= with (got %.1f vs %.1f)" % (e_no_asr, e10))

    print("")
    print("=" * 70)
    print("21  edge: pure speech-only formats")
    print("=" * 70)

    # Plain 'NAME — text' without bold
    plain = dlg.parse_script("AVERY — just text")
    check(len(plain) == 1 and plain[0]["speaker"] == "AVERY" and
          plain[0]["text"] == "just text",
          "plain NAME — text format works")

    # Mixed bold and plain in same script
    mixed = dlg.parse_script("> **AVERY** — bold.\nTHOMAS: plain.")
    check(len(mixed) == 2, "mixed bold/plain formats -> 2 lines")

    # Multiple suffix types
    for suffix in ["(V.O.)", "(O.S.)", "(CONT'D)", "(O.C.)"]:
        r = dlg.parse_script("AVERY %s: text." % suffix)
        check(len(r) == 1 and r[0]["speaker"] == "AVERY %s" % suffix,
              "%r speaker parsed (got %r)" % (suffix, r[0]["speaker"]))

    print("")
    print("=" * 70)
    print("22  edge: compose_description with active sliders")
    print("=" * 70)

    v_sliders = dlg.new_voice("Slider Test", gender="male", age_years=30,
                               accent="neutral", notes="")
    v_sliders["sliders"]["pitch"] = 2   # "very high"
    v_sliders["sliders"]["pace"] = -1   # "measured pace"
    desc_s = dlg.compose_description(v_sliders)
    check("very high" in desc_s, "pitch +2 included (got %r)" % desc_s)
    check("measured pace" in desc_s, "pace -1 included (got %r)" % desc_s)
    check(", ," not in desc_s, "no double commas (got %r)" % desc_s)

    print("")
    print("=" * 70)
    print("23  edge: directory creation on first save")
    print("=" * 70)

    v_new = dlg.new_voice("Never Saved Before")
    slug = v_new["slug"]
    dlg.save_voice(v_new)
    json_path = os.path.join(tmp, "voices", slug, "voice.json")
    check(os.path.exists(json_path),
          "save_voice creates voice.json at %s" % json_path)
    with open(json_path) as f:
        saved_data = json.load(f)
    check(saved_data["name"] == "Never Saved Before",
          "saved JSON contains correct name")

    print("")
    print("=" * 70)
    print("24  edge: project directory creation")
    print("=" * 70)

    p_new = dlg.new_project("Brand New", "AVERY: Hi.")
    dlg.save_project(p_new)
    p_json = os.path.join(tmp, "dialogue", "brand-new", "project.json")
    check(os.path.exists(p_json), "save_project creates project.json")

    print("")
    print("=" * 70)
    print("25  edge: lint_line on 'Two 3090s'")
    print("=" * 70)

    # The plan's specific example
    two_3090 = dlg.lint_line("Two 3090s")
    check(len(two_3090) > 0, "lint_line('Two 3090s') produces warnings (got %d)"
          % len(two_3090))
    any_3090 = any("3090" in m for m in two_3090)
    check(any_3090, "lint_line warns about '3090' (got %r)" % two_3090)

    # Also check ALL-CAPS word "TWO" is flagged? No, "Two" is title-cased. "3090S"
    # The plan says 'Two 3090s' - 3090 has digits and then 's'. So 3090s contains
    # digits and maybe the 'S' is all-caps. Let's just check we got warnings.
    check(len(two_3090) >= 1, "at least one warning for 'Two 3090s'")

    print("")
    print("=" * 70)
    print("26  reviewer: shared-default leak, screenplay furniture, the real season")
    print("=" * 70)

    va = dlg.new_voice("Leak A"); va["sliders"]["pace"] = 2; va["fx"]["room"] = 0.5
    vb = dlg.new_voice("Leak B")
    check(vb["sliders"]["pace"] == 0 and vb["fx"]["room"] == 0.0,
          "new_voice does not share nested dicts (pace=%r room=%r)"
          % (vb["sliders"]["pace"], vb["fx"]["room"]))
    vf = dlg.fork_voice(va, "Leak Fork"); vf["sliders"]["pace"] = -2
    check(va["sliders"]["pace"] == 2, "fork_voice does not share nested dicts with its source")

    furn = dlg.parse_script("CARD: **THAT WAS THREE WEEKS AGO.**\n"
                            "THE EXPERIMENT: phones in a drawer, one day.\n"
                            "AVERY: Huzzah.")
    check([l["speaker"] for l in furn] == ["AVERY"],
          "CARD:/THE EXPERIMENT: lines are not dialogue (got %r)" % [l["speaker"] for l in furn])

    s1 = os.path.expanduser("~/project/scripts/s1-v2")
    if os.path.isdir(s1):
        import glob as _g
        allsp = set(); n = 0
        for fp in sorted(_g.glob(os.path.join(s1, "1*.md"))):
            with open(fp, encoding="utf-8") as fh:
                L = dlg.parse_script(fh.read())
            n += len(L); allsp.update(dlg.speakers(L))
        check(n >= 300, "the real season parses to >= 300 lines (got %d)" % n)
        check({"AVERY", "NATE", "THOMAS", "JOSEPH"} <= allsp, "the four leads are all found")
        check(not ({"CARD", "THE EXPERIMENT"} & allsp), "no furniture speakers in the real season")
    else:
        print("  skip  real season not on this machine")

    # ---- summary ----------------------------------------------------------------
    print("")
    print("=" * 70)
    print("%d checks, %d failures" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for m in FAILS:
            print("  FAILED: %s" % m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())