#!/usr/bin/env python3
"""director.py — Tk-free client for the local abliterated LLM on the forge rig.

DIRECTOR turns a saved Forge Lab *character* into a validated creative pipeline:
premises -> script -> shot list -> engine-specific render prompts -> post copy,
plus a vision QC read of a rendered frame.

Everything is stdlib (urllib/json). No tkinter, no GPU, no cloud — this only talks to
Ollama at http://forge:11434 (pinned to GPU1). All model output is constrained with a
JSON schema and re-validated in-process with a small hand-written validator, so a caller
never has to trust a free-form string.

NOTE ON GPU COST: every call here loads/uses the LLM (GPU1). It never submits a ComfyUI
render. Unload with ``unload()`` when done so card 2 is released for a Raylight run.

Usage::

    from director import Director
    d = Director()
    d.load()
    shots = d.shots({"name": "Big Head", "prompt": "...", "action": "..."}, script)
    d.unload()
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_MODEL = "orcarouter/Qwen3.8-27B-Uncensored:latest"
FALLBACK_MODEL = "huihui_ai/qwen3.5-abliterated:9b"
DEFAULT_URL = "http://forge:11434"
CTX = 16384

def _characters_dir():
    """Where saved characters live, resolved per HOST.

    ⚠️ This used to be the hardcoded `~/forge-lab/characters`, which is the right answer on the
    Mac and the WRONG one on forge, where the Studio's data root is `/home/ryan/forge-studio-data`
    and the characters are synced to `<data>/characters`. Any slug-based lookup on the rig raised
    FileNotFoundError. The overnight runner never hit it only because it passes a character DICT
    (read through forge_lab, which IS data-aware) rather than a slug — so the bug sat invisible.
    """
    data = os.environ.get("FORGE_LAB_DATA")
    if data:
        p = os.path.join(os.path.expanduser(data), "characters")
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~/forge-lab/characters")


CHARACTERS_DIR = _characters_dir()


class DirectorError(Exception):
    """Raised when the model cannot produce schema-valid output, or the API is unreachable."""


# ---------------------------------------------------------------------------
# Routing: an intent maps to the engine + video dialect whose prompt rules apply.
# The table lives in the module because the shot prompt dialect is DERIVED from it —
# a prompt written in the wrong dialect is the "composed one prompt for every mode"
# trap the app already paid for once. See forge-lab-development skill.
# ---------------------------------------------------------------------------
ROUTE_TABLE = {
    "character_action": {"engine": "h3", "video_mode": "r2v"},
    "continue":         {"engine": "h3", "video_mode": "cont"},
    "lipsync":          {"engine": "h3", "video_mode": "r2v", "audio": True},
    "motion_copy":      {"engine": "animate2", "video_mode": None},
    "establishing":     {"engine": "h3", "video_mode": "t2v"},
}

VALID_SHOT_SECONDS = (3, 5, 8)
REWRITE_MODES = ("funnier", "shorter", "more deadpan")

# The ONLY phrases that are legal in a still prompt but wrong in a video prompt.
_STILL_PHRASES = ("looking straight at the camera", "85mm lens", "sharp focus")

# H3 r2v IS Ref2VA, and Ref2VA's prompt is six labelled sections in this order
# (MiniMax's own h3-prompt-writing skill, references/ref-en.txt). The reference is cited
# INSIDE subject_definitions — '<Subject 1> is ... in <Picture 1>' — and there is NO bare
# '<Picture 1> is the character.' tag anywhere in the format. The app enforced that tag for
# months; it was measured to change nothing (21.4% vs 19.0% on the same defect), because the
# model has no instruction for it. See forge-lab-development/references/h3-prompt-format-ref2va.md.
REF2VA_SECTIONS = ("subject_definitions", "summary", "retention_analysis",
                   "detailed_description", "overall_soundscape", "non_diegetic_music")


_REF2VA_LABEL_RE = re.compile(r"(?<![A-Za-z_])(%s)\s*:" % "|".join(REF2VA_SECTIONS))


def missing_ref2va_sections(text):
    """Which of the six Ref2VA section labels `text` is missing.

    Matched ANYWHERE, not at line start: forge's local model reliably writes the labels INLINE
    ('... in <Picture 1>. summary:\\nA tracking shot.'). A line-anchored match reported 5 of 6
    absent on genuinely conformant output — a false verdict from the instrument, not the prompt.

    A predicate, not an auto-fixer: callers report what is absent rather than inventing a
    retention_analysis or a soundscape the model never wrote.
    """
    found = set(_REF2VA_LABEL_RE.findall(text or ""))
    return [s for s in REF2VA_SECTIONS if s not in found]


def normalise_ref2va_labels(text):
    """Put each section label on its own line. Whitespace only — no content is added or removed.

    The spec's examples show one label per line; the local model tends to inline them. Moving
    them costs nothing and makes the prompt read as the documented format.
    """
    if not text:
        return text
    out = _REF2VA_LABEL_RE.sub(lambda m: "\n" + m.group(1) + ":", text)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()

# ⚠️ H3 HAS NO NEGATIVE CONDITIONING. Verified against forge's /object_info:
#   * `MiniMaxH3ReferenceToVideo` has NO `negative_prompt` input (the Qwen IMAGE nodes do).
#   * the H3 sampler is a `BasicGuider` (model + conditioning only), not a `CFGGuider`.
#   * the H3 path runs cfg 1.0, where a negative prompt is inert anyway.
# So for H3 the ONLY mechanism is PROSE — which is exactly what MiniMax's own published H3
# examples do ("Do not add people, vehicles, buildings, or logos."). The Qwen still path is
# different and DOES have a real `negative_prompt` field; set it there, not here.
#
# ⚠️ Negatives are NOT a seventh section. Ref2VA is fixed at six (see REF2VA_SECTIONS), so the
# clause goes in the TAIL of `detailed_description`, before the `overall_soundscape:` label.
HOUSE_NEGATIVES = (
    "No on-screen text, lettering, logos, watermarks, subtitles or UI overlays; no additional "
    "people or duplicated subjects; no face blending between subjects; no wardrobe or identity "
    "change within the shot."
)


def inject_negatives(text, clause=HOUSE_NEGATIVES):
    """Put the constraint clause in the place the Ref2VA format actually allows. Idempotent.

    Ref2VA is fixed at six sections, so this goes at the TAIL of `detailed_description`, before
    `overall_soundscape:`. Two cases deliberately return the text UNCHANGED rather than guessing:

      * a prompt carrying Ref2VA labels but NO `detailed_description` — appending would land the
        clause after `non_diegetic_music`, i.e. outside the section it belongs to. The caller
        reports the missing section (see `missing_ref2va_sections`) instead of inventing one.
      * a prompt that already carries the clause.

    A plain prose prompt (H3 t2v, or a hand-written beat) has no sections to respect, so the
    clause is appended at the end.
    """
    if not text or not clause:
        return text
    clause = clause.strip()
    if clause in text:
        return text
    m = re.search(r"(?<![A-Za-z_])detailed_description\s*:", text)
    if m:
        start = m.end()
        nxt = None
        for lab in REF2VA_SECTIONS:
            mm = re.search(r"(?<![A-Za-z_])%s\s*:" % lab, text[start:])
            if mm:
                pos = start + mm.start()
                if nxt is None or pos < nxt:
                    nxt = pos
        body_end = nxt if nxt is not None else len(text)
        head, body, tail = text[:start], text[start:body_end], text[body_end:]
        stripped = body.rstrip()
        if not stripped:
            return text
        trail = body[len(stripped):]
        joiner = "" if stripped.endswith((".", "!", "?")) else "."
        return head + stripped + joiner + " " + clause + trail + tail
    if set(_REF2VA_LABEL_RE.findall(text)):
        return text
    body = text.rstrip()
    joiner = "" if body.endswith((".", "!", "?")) else "."
    return body + joiner + " " + clause


HOUSE_STYLE = (
    "HOUSE COMEDY STYLE (non-negotiable): hyper-real and utterly ordinary in every "
    "detail except ONE absurd thing, which is played at a huge volume. The comedy is "
    "DEADPAN SINCERITY — the character and the world treat the absurd thing as completely "
    "normal and never acknowledge it as a joke. Never wink at the camera, never write a "
    "punchline that points at itself, never use the words 'funny' or 'lol'. For vertical "
    "shorts: 9:16, a hook in the first 2 seconds, and a button (a small final payoff) at "
    "the end."
)

_ENGINE_NOTES = (
    "ENGINE PROMPT DIALECTS — write each shot's `prompt` in the dialect of its intent:\n"
    "  character_action -> H3 reference-to-video, which is Ref2VA. Its prompt is SIX LABELLED\n"
    "    SECTIONS in this order: subject_definitions, summary, retention_analysis,\n"
    "    detailed_description, overall_soundscape, non_diegetic_music. Cite the reference\n"
    "    INSIDE subject_definitions ('<Subject 1> is <who> in <Picture 1>: <features to keep>');\n"
    "    never write a bare '<Picture 1> is the character.' tag - it is not part of the format.\n"
    "    retention_analysis: one line per label, with fully_preserved / partially_preserved /\n"
    "    attribute_transfer / weak_reference. detailed_description: 1-2 style sentences, then\n"
    "    '[Shot 1] ...' giving composition, subject, environment, action, and the camera as\n"
    "    type + amplitude + speed; 350-500 words. overall_soundscape: 1-4 sentences.\n"
    "    non_diegetic_music: 1-3 sentences or N/A. 24 fps; no still-photo language such as\n"
    "    'looking straight at the camera' or '85mm lens'. Whole-body locomotion animates and\n"
    "    gestures do not; a camera that holds the subject at a constant size CANCELS its motion;\n"
    "    put nothing on an empty field; never write two clauses that cannot both be true.\n"
    "  continue -> H3 continuing a clip. The reference is the LAST frame; no reference tag. "
    "Describe what happens next, in the same six sections.\n"
    "  lipsync -> H3 reference-to-video WITH an audio guide; the mouth follows the audio. "
    "Same six sections as character_action; keep the spoken line the focus.\n"
    "  motion_copy -> Wan Animate 2. A still performs a driving clip; the prompt is "
    "minimal — say only what should stay or change.\n"
    "  establishing -> H3 text-to-video. No references at all; plain cinematic description "
    "of the shot.\n"
    "Other engines, for reference: WAN 2.2 is 16 fps with no audio and a plain cinematic "
    "description; LTX 2.3 restyle keeps the input clip's structure and its prompt describes "
    "LOOK/STYLE only. H3 valid lengths are 3, 5 or 8 seconds."
)

# ---------------------------------------------------------------------------
# ONE-TAKE SEAMLESS BRIEF — appended to the system prompt for `seamless()` only.
#
# Why this exists (measured, 2026-10-07): a 30 s piece built from six 3-5 s STATIC shots is a
# jump-cut machine. Frame-to-frame motion on those pieces averaged 5.16 with a PEAK of 50.89 —
# the peak is the cut landing — and the audio lurched 8.6 dB between shots because each shot
# generated its own sound. One 15 s take with a moving camera and no invented dialogue scored
# 9.49 mean / 12.91 peak motion and 1.2 dB of audio spread. The four rules that bought that:
# ONE take, a CAMERA MOVE (the measured 5.6x motion lever), NO dialogue, ONE continuous bed.
# ---------------------------------------------------------------------------
SEAMLESS_NOTES = (
    "ONE-TAKE SEAMLESS BRIEF — this piece is a SINGLE CONTINUOUS TAKE. Obey all of it:\n"
    "  * EXACTLY ONE take: one shot, 15 seconds, no cuts, no second shot. The camera never\n"
    "    cuts away. (The 3/5/8-second rule in your instructions does NOT apply here — a single\n"
    "    take may run to 15 seconds.)\n"
    "  * It is amateur vertical PHONE footage shot by a bystander standing in the crowd,\n"
    "    because the character is a local celebrity and members of the public film them. It\n"
    "    must look like something a passer-by shot on their phone, NOT like a film crew.\n"
    "  * The framing is deliberately INCOMPETENT: the subject sits OFF-CENTRE for most of the\n"
    "    take and is never re-centred, the horizon is slightly tilted, there is constant small\n"
    "    handheld shake with the odd correcting lurch, someone crosses the very near foreground\n"
    "    completely out of focus and briefly blocks him, the auto-exposure breathes and brightens\n"
    "    for a moment as the camera swings past something bright, and once in the middle the\n"
    "    operator pinches to zoom in so the image softens and then re-sharpens.\n"
    "  * The camera MOVES — a slow drift, a lean, a small track — written as type + amplitude +\n"
    "    speed. It must NOT hold the subject at a constant size, which cancels his motion.\n"
    "  * A CROWD watches from a distance: behind him, out of focus, standing in a loose arc,\n"
    "    mostly seen from behind or in profile, a few holding phones up to film him. Their faces\n"
    "    stay SMALL in frame and soft. NEVER put a stranger's face in the foreground.\n"
    "  * NO DIALOGUE AT ALL. No spoken lines, no <d> tags, no narration. overall_soundscape is\n"
    "    phone-microphone street ambience — wind on the mic, traffic, footsteps, the low murmur\n"
    "    of the crowd, a phone shutter, pigeons — with no clear spoken words, and\n"
    "    non_diegetic_music is N/A.\n"
    "  * The character never acknowledges the camera, never performs for it, and never breaks the\n"
    "    deadpan. The comedy is that the crowd is filming something he treats as entirely normal.\n"
    "  * `card` is his catchphrase: ON-SCREEN TEXT only, under 12 words, in his voice, deadpan.\n"
    "    It is the only dialogue the piece has.\n"
    "  * The render prompt is the six Ref2VA sections in order, detailed_description 350-500\n"
    "    words, with '[Shot 1]' carrying the whole 15 seconds."
)

SEAMLESS_SCHEMA = {
    "type": "object", "required": ["title", "logline", "card", "prompt"],
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "card": {"type": "string"},
        "prompt": {"type": "string"},
    },
}

_MARKERS = ("fully_preserved", "partially_preserved", "attribute_transfer", "weak_reference")
# film language that must not survive into a piece whose whole point is phone footage
_FILM_WORDS = ((r"fine 35mm film grain", "mild digital sensor noise"),
               (r"35mm film grain", "digital sensor noise"),
               (r"\bfilm grain\b", "digital sensor noise"))


def seamless_dd_words(text):
    """Word count of the LONGEST detailed_description block.

    ⚠️ Longest, not first: the local model sometimes emits a duplicated label with junk in it
    ('detailed_description: fully_preserved;. No on-screen text…') followed by the real one, so
    measuring the FIRST block reports a good prompt as empty.
    """
    blocks = re.findall(r"detailed_description\s*:(.*?)(?=overall_soundscape\s*:|$)",
                        text or "", re.S | re.I)
    return max([len(b.split()) for b in blocks] or [0])


def seamless_prompt_ok(text):
    """Is this roll worth a 26-minute 2-card render?

    All six sections present AND a real detailed_description. A roll that merely *contains* the
    labels can still be junk — a duplicated label with a stub body passes a presence check and
    costs a full render.
    """
    return (not missing_ref2va_sections(text)) and seamless_dd_words(text) >= 150


def _repair_seamless_prompt(p):
    """Deterministic repairs for what the local model reliably gets wrong in this dialect.

    ⚠️ An instruction is a hint; only a code-level repair binds the model (measured: a live
    call came back with `retention_analysis` written as the SECTION LABELS — 'subject_definitions:
    fully_preserved / summary: fully_preserved' — which tells H3 nothing about what to preserve,
    and with 'fine 35mm film grain' in a piece specified as phone footage). Three repairs:

      1. retention_analysis, if it does not carry a '<Subject N> (appears in ...' line, is
         REBUILT one line per subject from the subjects actually declared.
      2. overall_soundscape gets the no-speech clause appended when it is missing — otherwise
         nothing stops H3 improvising voices, which is the loudest AI tell there is.
      3. Film language is swapped for sensor language (this piece is a phone).
    """
    subs = []
    for s in re.findall(r"<Subject\s+(\d+)>", p):
        if s not in subs:
            subs.append(s)

    m = re.search(r"(retention_analysis\s*:)(.*?)(?=detailed_description\s*:)", p, re.S | re.I)
    if m:
        body = m.group(2)
        if not re.search(r"<Subject\s+\d+>\s*\(appears in", body):
            lines = []
            for i, s in enumerate(subs):
                if i == 0:
                    lines.append("<Subject %s> (appears in [Shot 1]): fully_preserved - identity, "
                                 "hair, wardrobe and expression retained for the whole take." % s)
                else:
                    lines.append("<Subject %s> (appears in [Shot 1]): reference - incidental, "
                                 "stays in the background and out of focus." % s)
            if lines:
                p = p[:m.start(2)] + "\n" + "\n".join(lines) + "\n" + p[m.end(2):]

    m = re.search(r"(overall_soundscape\s*:)(.*?)(?=non_diegetic_music\s*:|$)", p, re.S | re.I)
    if m:
        body = m.group(2)
        if not re.search(r"no (human speech|spoken|voices|dialogue)", body, re.I):
            p = p[:m.end(2)] + " No human speech and no voices." + p[m.end(2):]

    for pat, rep in _FILM_WORDS:
        p = re.sub(pat, rep, p, flags=re.I)

    # 4. The model ECHOES THE BRIEF back into detailed_description — sentences that describe the
    #    FORMAT rather than the shot ("NO DIALOGUE AT ALL. No spoken lines, no <d> tags, no
    #    narration." / "overall_soundscape is phone-microphone street ambience … non_diegetic_music
    #    is N/A."). They are meaningless to H3, they bloat the prompt, and the '<d>' inside the
    #    NEGATION makes an honest "contains dialogue" check fire on a silent piece. Drop them.
    # 5. A DUPLICATED detailed_description label whose stub body is junk ('detailed_description:
    #    fully_preserved;. No on-screen text…') — the model misplaces the retention value. Keep
    #    only the longest block and drop the stub, so the prompt carries one clean description.
    blocks = list(re.finditer(r"detailed_description\s*:(.*?)(?=detailed_description\s*:"
                              r"|overall_soundscape\s*:|$)", p, re.S | re.I))
    if len(blocks) > 1:
        best = max(blocks, key=lambda m: len(m.group(1).split()))
        p = (p[:blocks[0].start()] + "detailed_description:" + best.group(1)
             + " " + p[blocks[-1].end():])

    m = re.search(r"(detailed_description\s*:)(.*?)(?=overall_soundscape\s*:)", p, re.S | re.I)
    if m:
        keep = []
        for sent in re.split(r"(?<=[.!?])\s+", m.group(2)):
            if not sent.strip():
                continue
            if re.search(r"<d>|DIALOGUE AT ALL|overall_soundscape is|non_diegetic_music is",
                         sent, re.I):
                continue
            keep.append(sent.strip())
        p = p[:m.start(2)] + " " + " ".join(keep) + " " + p[m.end(2):]
    return p


# ---------------------------------------------------------------------------
# JSON schemas — handed to Ollama as `format`, then re-validated in-process.
# ---------------------------------------------------------------------------
PITCH_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 12,
    "items": {
        "type": "object", "required": ["title", "logline", "hook"],
        "properties": {
            "title": {"type": "string"},
            "logline": {"type": "string"},
            "hook": {"type": "string"},
        },
    },
}

SCRIPT_SCHEMA = {
    "type": "object", "required": ["title", "hook", "beats", "button"],
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "beats": {
            "type": "array", "minItems": 1, "maxItems": 30,
            "items": {
                "type": "object",
                "required": ["t_start", "t_end", "action", "line", "speaker"],
                "properties": {
                    "t_start": {"type": "number"},
                    "t_end": {"type": "number"},
                    "action": {"type": "string"},
                    "line": {"type": "string"},
                    "speaker": {"type": "string"},
                },
            },
        },
        "button": {"type": "string"},
    },
}

SHOTS_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 20,
    "items": {
        "type": "object",
        "required": ["n", "seconds", "intent", "camera", "action", "line", "prompt"],
        "properties": {
            "n": {"type": "integer"},
            "seconds": {"type": "integer", "enum": list(VALID_SHOT_SECONDS)},
            "intent": {"type": "string", "enum": list(ROUTE_TABLE)},
            "camera": {"type": "string"},
            "action": {"type": "string"},
            "line": {"type": "string"},
            "prompt": {"type": "string"},
        },
    },
}

REWRITE_SCHEMA = {
    "type": "object", "required": ["text"],
    "properties": {"text": {"type": "string"}},
}

POLISH_SCHEMA = {
    "type": "object", "required": ["prompt"],
    "properties": {"prompt": {"type": "string"}},
}

BIBLE_SCHEMA = {
    "type": "object",
    "required": ["who", "absurd_thing", "voice", "running_bits", "never"],
    "properties": {
        "who": {"type": "string"},
        "absurd_thing": {"type": "string"},
        "voice": {"type": "string"},
        "running_bits": {"type": "array", "minItems": 0, "maxItems": 12,
                         "items": {"type": "string"}},
        "never": {"type": "array", "minItems": 0, "maxItems": 12,
                  "items": {"type": "string"}},
    },
}

VOICE_SCHEMA = {
    "type": "object", "required": ["voice"],
    "properties": {"voice": {"type": "string"}},
}

POST_COPY_SCHEMA = {
    "type": "object", "required": ["title", "caption", "hashtags"],
    "properties": {
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "minItems": 1, "maxItems": 15,
                     "items": {"type": "string"}},
    },
}

# ---- SONG LYRICS (YuE2) -------------------------------------------------------------------
# The model sings exactly what it is given, so these are ONLY the words to be sung: section
# tags and lines. Musical directions belong in the STYLE prompt, not here — the model's own
# guide is explicit that anything else in the lyrics gets SUNG.
LYRICS_SCHEMA = {
    "type": "object", "required": ["title", "lyrics"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 60},
        "lyrics": {"type": "string", "minLength": 40},
    },
}

WEIRDO_FIT_SCHEMA = {
    "type": "object",
    "required": ["subject", "skin", "hair", "facial_hair", "wardrobe", "accessory",
                 "setting", "lighting", "absurd", "action"],
    "properties": {
        "subject": {"type": "string", "minLength": 3, "maxLength": 120},
        "skin": {"type": "string", "maxLength": 120},
        "hair": {"type": "string", "maxLength": 120},
        "facial_hair": {"type": "string", "maxLength": 120},
        "wardrobe": {"type": "string", "minLength": 3, "maxLength": 240},
        "accessory": {"type": "string", "maxLength": 120},
        "setting": {"type": "string", "minLength": 3, "maxLength": 240},
        "lighting": {"type": "string", "maxLength": 120},
        "absurd": {"type": "string", "minLength": 3, "maxLength": 160},
        "action": {"type": "string", "minLength": 3, "maxLength": 200},
    },
}

SYSTEM_WEIRDO = (
    "You cast characters for a deadpan, hyper-real comedy. Every character is a completely "
    "ordinary person with exactly ONE absurd physical trait, photographed and played with total "
    "sincerity. You follow the director's description faithfully, keep everything else "
    "believable and plain, and return only the requested JSON."
)

SYSTEM_LYRICS = (
    "You are a songwriter. You write ONLY the words that get sung — never musical "
    "directions, never notes to the producer, never instrument names or tempos. Your lines "
    "are short, concrete and singable, with a consistent syllable count inside each section "
    "so a melody can carry them. You always return complete, real lines; you never return a "
    "section tag with nothing under it."
)

DIALOGUE_SCHEMA = {
    "type": "object", "required": ["title", "lines"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "lines": {
            "type": "array", "minItems": 2,
            "items": {
                "type": "object", "required": ["speaker", "text"],
                "properties": {
                    "speaker": {"type": "string", "minLength": 1},
                    "direction": {"type": "string"},
                    "text": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

SYSTEM_DIALOGUE = (
    "You are a comedy dialogue writer. You write short, deadpan dialogue for named characters "
    "in a hyper-real, sincere comedy. Keep lines short and spoken-word \u2014 every word is spoken "
    "aloud by the character. Stage directions go ONLY in the `direction` field (short, may be "
    "empty). NEVER put stage directions, bracketed cues, or parenthetical notes inside the "
    "spoken `text`. Write numbers as words. Every speaker must be one of the given cast "
    "names exactly (upper-case)."
)

# Patterns the model must not put inside spoken text.
_DIALOGUE_STAGE_DIR_RE = re.compile(
    r"(?:\([^)]*\))"             # (sighs), (laughing), (beat)
    r"|(?:\*\([^)]*\)\*)"        # *(sighs)*
    r"|(?:\[[^\]]*\])"           # [laughs]
)
_DIALOGUE_DIGIT_RE = re.compile(r"\d")


def validate_dialogue(out, cast):
    """Problems with a dialogue output \u2014 checked in CODE, not entrusted to the model.

    `out` is {title, lines:[{speaker, direction, text}]}.  `cast` is {NAME: bio, ...}.
    Returns list[str] of human-readable problems; empty list == valid.
    """
    errs = []
    lines = (out or {}).get("lines") or []

    if not isinstance(lines, list) or len(lines) < 2:
        errs.append("need at least 2 lines of dialogue, got %d" % len(lines))
        return errs  # no point checking further

    try:
        from dialogue import normalise_speaker as _ns
    except ImportError:
        _ns = lambda s: s.upper().strip()

    speakers_seen = set()
    for i, l in enumerate(lines):
        speaker = _ns((l or {}).get("speaker") or "")
        text = (l or {}).get("text") or ""

        if not speaker:
            errs.append("line %d has no speaker" % i)
            continue
        if speaker not in cast:
            errs.append("line %d: speaker %r is not in the cast" % (i, speaker))
        speakers_seen.add(speaker)

        if not text:
            errs.append("line %d has empty spoken text" % i)
        if text and _DIALOGUE_DIGIT_RE.search(text):
            errs.append("line %d contains digits in spoken text \u2014 write numbers as words" % i)
        if text and _DIALOGUE_STAGE_DIR_RE.search(text):
            errs.append("line %d has stage directions inside the spoken text" % i)

    if len(cast) >= 2 and len(speakers_seen) < 2:
        errs.append("need at least 2 distinct speakers when %d in the cast" % len(cast))

    return errs


def format_dialogue(lines):
    """Format dialogue lines in the house script format, one per line.

    Returns a string that can be round-tripped through `dialogue.parse_script()`
    to recover the same speakers, direction, and text.

    `> **SPEAKER** — *(direction)* text`
    `> **SPEAKER** — text`              (when direction is empty)
    """
    out = []
    for l in lines:
        speaker = (l or {}).get("speaker", "").strip()
        direction = (l or {}).get("direction", "").strip()
        text = (l or {}).get("text", "").strip()
        if not speaker:
            continue
        if direction:
            out.append("> **%s** \u2014 *(%s)* %s" % (speaker, direction, text))
        else:
            out.append("> **%s** \u2014 %s" % (speaker, text))
    return "\n".join(out)


_LYRIC_TAG_RE = re.compile(r"^\s*\[[^\]]+\]\s*$")
# ⚠️ Narrow on purpose: a lyric may legitimately contain the word "guitar" ("he played guitar
# in a bar band"). These patterns only catch DIRECTIONS — a BPM figure, a named solo, a
# bracketed stage cue — which is the thing the model does when it has not understood.
_LYRIC_DIRECTION_RE = re.compile(
    r"\b\d{2,3}\s*bpm\b"
    r"|\b(guitar|synth|drum|bass|piano|string|sax|organ)\s+solo\b"
    r"|\b(instrumental break|fade in|fade out|half time|double time|build up)\b"
    r"|\((?:guitar|synth|drums?|bass|solo|instrumental|break)[^)]*\)", re.I)


def validate_lyrics(text):
    """Problems with a lyric block — checked in CODE, not entrusted to the model.

    ⚠️ The Director's SCRIPTS came back with every `line` empty until the prompt REQUIRED
    one, and a 27B asked for lyrics will just as happily return four section tags and no
    words. The prompt states the requirements; THIS is what rejects a dud, and its errors are
    fed back for one retry.
    """
    errs = []
    t = (text or "").strip()
    if not t:
        return ["the lyrics are empty"]
    lines = t.splitlines()
    tags = [ln.strip() for ln in lines if _LYRIC_TAG_RE.match(ln.strip())]
    if not tags:
        errs.append("no [Verse]/[Chorus] section tags")
    elif not any(tt.lower().startswith("[chorus") for tt in tags):
        errs.append("no [Chorus] — a song needs a hook that comes back")
    cur, sung = None, 0
    for ln in lines:
        s = ln.strip()
        if _LYRIC_TAG_RE.match(s):
            if cur is not None and not cur[1]:
                errs.append("%s has no lines under it" % cur[0])
            cur = [s, 0]
            continue
        if s:
            sung += 1
            if cur is not None:
                cur[1] += 1
    if cur is not None and not cur[1]:
        errs.append("%s has no lines under it" % cur[0])
    if sung < 4:
        errs.append("only %d sung lines" % sung)
    if _LYRIC_DIRECTION_RE.search(t):
        errs.append("musical directions leaked into the words (they belong in STYLE)")
    return errs

QC_SCHEMA = {
    "type": "object", "required": ["verdict", "reasons"],
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "drift", "broken"]},
        "reasons": {"type": "array", "minItems": 0, "maxItems": 10,
                    "items": {"type": "string"}},
    },
}


# ---------------------------------------------------------------------------
# Hand-written schema validator (no jsonschema dependency).
# Supports: type, required, properties, enum, items, minItems/maxItems,
# minimum/maximum, minLength/maxLength. Unknown keywords are ignored.
# Returns a list of human-readable error strings (empty == valid).
# ---------------------------------------------------------------------------
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value, schema, path="$"):
    """Validate `value` against `schema`; return a list of error strings."""
    errors = []
    if not isinstance(schema, dict):
        return errors

    stype = schema.get("type")
    if stype is not None:
        allowed = stype if isinstance(stype, list) else [stype]
        checker = None
        for t in allowed:
            if t in _TYPES:
                if _TYPES[t](value):
                    checker = t
                    break
        if checker is None:
            errors.append("%s: expected %s, got %s" % (path, stype, _tname(value)))
            return errors  # type wrong; deeper checks are meaningless

    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: value %r not in enum %r" % (path, value, schema["enum"]))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s: string shorter than minLength %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("%s: string longer than maxLength %d" % (path, schema["maxLength"]))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: %r below minimum %r" % (path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s: %r above maximum %r" % (path, value, schema["maximum"]))

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append("%s: missing required key %r" % (path, key))
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in value:
                errors.extend(validate(value[key], subschema, "%s.%s" % (path, key)))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("%s: has %d items, minItems %d" % (path, len(value), schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("%s: has %d items, maxItems %d" % (path, len(value), schema["maxItems"]))
        items = schema.get("items")
        if isinstance(items, dict):
            for i, entry in enumerate(value):
                errors.extend(validate(entry, items, "%s[%d]" % (path, i)))

    return errors


def _tname(v):
    if v is None:
        return "null"
    return type(v).__name__


def _extract_json(text):
    """Parse JSON out of a model reply, tolerating ```json fences and stray prose."""
    if isinstance(text, (dict, list)):
        return text
    s = (text or "").strip()
    s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        end = s.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except ValueError:
                continue
    raise ValueError("no JSON found in reply")


def route(intent):
    """Return a COPY of the routing entry for a shot intent, or raise."""
    entry = ROUTE_TABLE.get(intent)
    if entry is None:
        raise DirectorError("unknown shot intent %r (known: %s)"
                            % (intent, ", ".join(sorted(ROUTE_TABLE))))
    return dict(entry)


def load_character(character):
    """Accept a dict, a character.json path, or a slug; return the character dict."""
    if isinstance(character, dict):
        return character
    if not isinstance(character, str):
        raise DirectorError("character must be a dict, slug or path")
    path = character
    if not os.path.isabs(path):
        if "/" not in path and not path.endswith(".json"):
            path = os.path.join(CHARACTERS_DIR, path, "character.json")
        else:
            path = os.path.expanduser(path)
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        path = os.path.join(path, "character.json")
    with open(path) as fh:
        return json.load(fh)


def identity_block(character):
    """The character's identity text + bible, as prompt-ready lines."""
    ch = load_character(character)
    lines = ["CHARACTER: %s" % ch.get("name", "the character")]
    if ch.get("prompt"):
        lines.append("IDENTITY: %s" % ch["prompt"])
    if ch.get("action"):
        lines.append("SIGNATURE ACTION: %s" % ch["action"])
    bible = ch.get("bible")
    if isinstance(bible, dict):
        lines.append("CHARACTER BIBLE:")
        for key in ("who", "absurd_thing", "voice"):
            if bible.get(key):
                lines.append("  %s: %s" % (key.replace("_", " "), bible[key]))
        for key in ("running_bits", "never"):
            vals = bible.get(key)
            if vals:
                lines.append("  %s: %s" % (key.replace("_", " "), "; ".join(vals)))
    return "\n".join(lines)


def system_prompt(character):
    """System prompt carrying the identity block, the bible and the house style."""
    return (
        "You are DIRECTOR, a screenwriter and shot-planner for a hyper-real deadpan "
        "comedy studio. You write tight, shootable material for very short vertical "
        "videos. You ALWAYS answer with JSON that matches the schema you are given and "
        "nothing else.\n\n"
        + HOUSE_STYLE + "\n\n"
        + identity_block(character) + "\n\n"
        + _ENGINE_NOTES
    )


class Director:
    def __init__(self, host=None, model=DEFAULT_MODEL, fallback=FALLBACK_MODEL, timeout=600):
        self.host = (host or os.environ.get("DIRECTOR_URL") or DEFAULT_URL).rstrip("/")
        self.model = model
        self.fallback = fallback
        self.timeout = timeout

    # -- low-level HTTP seam (tests monkeypatch this one method) -------------
    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.host + path, data=data,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise DirectorError("POST %s failed: %s" % (path, exc)) from exc

    def _get(self, path):
        req = urllib.request.Request(self.host + path)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- status / residency --------------------------------------------------
    def status(self):
        """Return {reachable, loaded:[...], vram_hint}. Never raises."""
        out = {"reachable": False, "loaded": [], "vram_hint": None}
        try:
            ps = self._get("/api/ps")
        except Exception:
            return out
        out["reachable"] = True
        resident = 0
        for m in ps.get("models", []) if isinstance(ps, dict) else []:
            name = m.get("name") or m.get("model")
            if name:
                out["loaded"].append(name)
            resident += int(m.get("size_vram") or 0)
        if out["loaded"]:
            out["vram_hint"] = "%d resident model(s), ~%.1f GB in VRAM" % (
                len(out["loaded"]), resident / 1e9)
        return out

    def is_loaded(self, model=None):
        model = model or self.model
        base = model.split(":")[0]
        try:
            st = self.status()
        except Exception:
            return False
        return any(m == model or m.split(":")[0] == base for m in st["loaded"])

    def load(self):
        """Warm the model into VRAM. Returns the raw API response."""
        return self._post("/api/generate", {
            "model": self.model, "prompt": "warm", "stream": False,
            "keep_alive": "15m", "options": {"num_predict": 1},
        })

    def unload(self, model=None):
        """Release the model from VRAM (keep_alive 0). Returns the raw response."""
        return self._post("/api/generate", {"model": model or self.model, "keep_alive": 0})

    def unload_all(self):
        """Release EVERY model resident on the host; returns the names freed.

        ⚠️ unload() frees only THIS client's model. A 2-card Raylight H3 render needs ~21 GB
        on GPU1, and a DIFFERENT model still sitting there is exactly as much of an OOM — so
        the card cannot be freed by unloading one name and hoping. This is what the 'FREE
        CARD 2' setting calls.
        """
        freed = []
        try:
            loaded = list((self.status() or {}).get("loaded") or [])
        except Exception:
            loaded = []
        if not loaded:
            # status() may not list it (or the host was briefly unreachable): still ask for
            # our own model, because that is the one we know about.
            loaded = [self.model]
        for name in loaded:
            try:
                self.unload(name)
                freed.append(name)
            except Exception:
                pass
        return freed

    # -- the one call every high-level method goes through -------------------
    def _chat(self, system, user, schema, temperature=0.7, images=None, model=None):
        model = model or self.model

        def _user_msg(text):
            msg = {"role": "user", "content": text}
            if images:
                msg["images"] = images
            return msg

        messages = [{"role": "system", "content": system}, _user_msg(user)]
        payload = {
            "model": model, "messages": messages, "stream": False, "think": False,
            "format": schema, "options": {"temperature": temperature, "num_ctx": CTX},
        }
        errors = None
        last_err = "no attempt made"
        for _attempt in range(2):
            if errors:
                messages[-1] = _user_msg(
                    user + "\n\nYour previous reply was REJECTED by the schema validator:\n- "
                    + "\n- ".join(errors) + "\nReturn corrected JSON only, matching the schema.")
            try:
                resp = self._post("/api/chat", payload)
            except DirectorError as exc:
                last_err = str(exc)
                if self.fallback and model != self.fallback:
                    model = self.fallback
                    payload["model"] = model
                    errors = None
                    continue
                raise
            content = (resp.get("message") or {}).get("content", "")
            try:
                obj = _extract_json(content)
            except ValueError as exc:
                errors = ["invalid JSON: %s" % exc]
                last_err = errors[0]
                continue
            errors = validate(obj, schema)
            if not errors:
                return obj
            last_err = "; ".join(errors)
        raise DirectorError("model could not produce schema-valid output: %s" % last_err)

    # -- dialect enforcement -------------------------------------------------
    def _enforce_dialect(self, shot):
        """Rewrite one shot's `prompt` to the dialect its intent requires.

        r2v intents get the reference tag prepended; every other intent must NOT
        carry it. Still-photo phrasing is stripped from every video prompt.
        Annotates the shot with its engine/video_mode so callers can route it.
        """
        info = route(shot["intent"])
        prompt = shot.get("prompt", "") or ""
        prompt = re.sub(r"<Picture\s+\d+>\s*is the character\.?", "", prompt, flags=re.I)
        for phrase in _STILL_PHRASES:
            prompt = re.sub(re.escape(phrase), "", prompt, flags=re.I)
        prompt = re.sub(r"\s{2,}", " ", prompt)
        prompt = re.sub(r"\s+([.,;:])", r"\1", prompt)
        prompt = re.sub(r"^[\s.,;:]+", "", prompt).strip()
        out = dict(shot)
        out["prompt"] = prompt.strip()
        out["engine"] = info["engine"]
        out["video_mode"] = info["video_mode"]
        if info.get("video_mode") == "r2v":
            # Ref2VA has no '<Picture 1> is the character.' tag — the reference is cited inside
            # subject_definitions. Report what the prompt is missing rather than silently
            # prepending a phrase the model was never trained on.
            miss = missing_ref2va_sections(out["prompt"])
            if miss:
                out["ref2va_sections_missing"] = miss
        # H3 has no negative conditioning (see HOUSE_NEGATIVES), so the constraints can only be
        # prose — and prose the model may simply omit. Inject them in CODE. H3 only: Animate 2's
        # prompt is deliberately minimal ("say only what should stay or change").
        if out["engine"] == "h3":
            injected = inject_negatives(out["prompt"])
            if injected != out["prompt"]:
                out["prompt"] = injected
                out["negatives_injected"] = True
        if info.get("audio"):
            out["audio"] = True
        return out

    # -- high-level creative calls ------------------------------------------
    def pitch(self, character, n=5, length_s=20, brief=""):
        user = (
            "Write %d DIFFERENT premises for %s-second vertical comedy shorts.\n"
            "Return an array of %d objects, each with:\n"
            "  title   - a short punchy working title\n"
            "  logline - ONE sentence: ordinary world + the single absurd thing\n"
            "  hook    - what the viewer sees and hears in the FIRST TWO SECONDS\n"
            % (n, length_s, n)
        )
        if brief:
            user += "\nDirection to honour: %s\n" % brief
        return self._chat(system_prompt(character), user, PITCH_SCHEMA, temperature=0.9)

    def script(self, character, premise, length_s=20):
        premise_text = premise
        if isinstance(premise, dict):
            premise_text = "TITLE: %s\nLOGLINE: %s\nHOOK: %s" % (
                premise.get("title", ""), premise.get("logline", ""), premise.get("hook", ""))
        user = (
            "Write a %s-second script for this premise.\n\n%s\n\n"
            "Return an object with:\n"
            "  title  - the final title\n"
            "  hook   - what lands in the first 2 seconds\n"
            "  beats  - an ordered array of {t_start, t_end, action, line, speaker}.\n"
            "           t_start/t_end are seconds within the piece. `action` is what the\n"
            "           camera sees. `line` is the spoken text (use \"\" if silent) and\n"
            "           `speaker` names who says it (use \"\" for no one).\n"
            "  button - the final payoff that closes the loop\n"
            "At least ONE beat must have a short spoken `line` (under 12 words) said by the "
            "character, delivered deadpan — the voice and captions are what make a short land.\n"
            % (length_s, premise_text)
        )
        return self._chat(system_prompt(character), user, SCRIPT_SCHEMA, temperature=0.8)

    def shots(self, character, script):
        sch = script
        if isinstance(sch, dict):
            sch = json.dumps(sch, indent=1)
        user = (
            "Break this script into a shot list.\n\n%s\n\n"
            "Return an array of shots. Each shot has:\n"
            "  n        - integer shot number, starting at 1\n"
            "  seconds  - integer length, one of 3, 5 or 8 (they should sum close to the\n"
            "             script's total length)\n"
            "  intent   - one of: character_action, continue, lipsync, motion_copy,\n"
            "             establishing\n"
            "  camera   - the camera move/setup in a few words\n"
            "  action   - what the character physically does\n"
            "  line     - the spoken text in this shot, or \"\"\n"
            "  prompt   - the RENDER prompt, written in the dialect for this shot's intent\n"
            "             (see the dialect rules in your instructions)\n"
            % sch
        )
        raw = self._chat(system_prompt(character), user, SHOTS_SCHEMA, temperature=0.6)
        return [self._enforce_dialect(s) for s in raw]

    def seamless(self, character, idea=""):
        """ONE continuous 15-second take, in the ONE-TAKE SEAMLESS BRIEF dialect.

        Returns {title, logline, card, prompt, ref2va_sections_missing?}. The prompt is cleaned
        the same way a shot prompt is: the undocumented '<Picture N> is the character.' tag is
        stripped, the H3 negatives are injected in code (H3 has no negative conditioning), and
        any missing Ref2VA section is REPORTED rather than invented.
        """
        user = (
            "Write ONE continuous 15-second vertical piece for this character, following the\n"
            "ONE-TAKE SEAMLESS BRIEF in your instructions exactly.\n\n"
            "Return an object with:\n"
            "  title   - a short punchy working title\n"
            "  logline - ONE sentence: the ordinary world, the single absurd thing, and where the\n"
            "            crowd comes into it\n"
            "  card    - his catchphrase for ON-SCREEN TEXT, under 12 words, deadpan\n"
            "  prompt  - the complete six-section Ref2VA render prompt for the single 15-second\n"
            "            take (subject_definitions, summary, retention_analysis,\n"
            "            detailed_description, overall_soundscape, non_diegetic_music)\n"
        )
        if idea:
            user += "\nDirection to honour: %s\n" % idea
        # ⚠️ The local model is STOCHASTIC and sometimes returns a prompt with no sections at all
        # (observed live: the identical call produced a 3,878-char six-section prompt on one roll
        # and a section-less blob on the next). A structurally broken prompt costs a full
        # 26-minute 2-card render, so re-ask until the six sections are present.
        obj, p = None, ""
        for attempt in range(3):
            cand = self._chat(system_prompt(character) + "\n\n" + SEAMLESS_NOTES, user,
                              SEAMLESS_SCHEMA, temperature=0.7 + 0.05 * attempt)
            if not isinstance(cand, dict):
                raise DirectorError("seamless() needs an object, got %s" % type(cand).__name__)
            body = self._clean_seamless_prompt(cand.get("prompt") or "")
            obj, p = cand, body
            if seamless_prompt_ok(p):
                break
        if obj is None:                                    # only if the loop body never ran
            raise DirectorError("seamless() could not get a response")
        obj["prompt"] = p
        miss = missing_ref2va_sections(p)
        if miss:
            obj["ref2va_sections_missing"] = miss
        return obj

    def _clean_seamless_prompt(self, p):
        """Strip the wrong dialect, repair what the model reliably omits, and lay the labels out.
        Kept as its own method so the repairs are unit-testable without touching the model."""
        p = re.sub(r"<Picture\s+\d+>\s*is the character\.?", "", p or "", flags=re.I)
        for phrase in _STILL_PHRASES:
            p = re.sub(re.escape(phrase), "", p, flags=re.I)
        p = re.sub(r"\s{2,}", " ", p)
        p = _repair_seamless_prompt(p)
        p = normalise_ref2va_labels(p)
        p = inject_negatives(p)
        return p.strip()

    def rewrite(self, text, how):
        if how not in REWRITE_MODES:
            raise DirectorError("rewrite mode must be one of %s" % (REWRITE_MODES,))
        user = (
            "Rewrite the following, making it %s. Keep the deadpan sincerity. "
            "Return {\"text\": <the rewrite only>}.\n\n%s" % (how, text)
        )
        res = self._chat(
            "You are DIRECTOR, a deadpan comedy writer. " + HOUSE_STYLE,
            user, REWRITE_SCHEMA, temperature=0.9)
        return res["text"]

    def bible(self, character):
        user = (
            "Write the CHARACTER BIBLE for this character (infer it from the identity "
            "block). Return {who, absurd_thing, voice, running_bits, never}: `who` a "
            "short paragraph, `absurd_thing` the single exaggerated volume, `voice` how "
            "they speak, `running_bits` a few recurring gags, `never` a few things this "
            "character must never do."
        )
        return self._chat(system_prompt(character), user, BIBLE_SCHEMA, temperature=0.8)

    def voice_description(self, character):
        user = (
            "In ONE sentence, describe a TTS voice-design prompt for this character "
            "(age, timbre, pace, accent, attitude). Return {\"voice\": <sentence>}."
        )
        return self._chat(system_prompt(character), user, VOICE_SCHEMA, temperature=0.7)["voice"]

    def post_copy(self, character, script):
        sch = script
        if isinstance(sch, dict):
            sch = json.dumps(sch, indent=1)
        user = (
            "Write the social post copy for this short.\n\n%s\n\n"
            "Return {title, caption, hashtags}. `title` is the on-post title, `caption` "
            "is one or two deadpan lines, `hashtags` an array of tags WITHOUT the leading "
            "'#'." % sch
        )
        return self._chat(system_prompt(character), user, POST_COPY_SCHEMA, temperature=0.8)

    # -- SONG LYRICS (YuE2) -------------------------------------------------
    def lyrics(self, brief, style="", seconds=120.0, model=None):
        """Words for a YuE2 song → {title, lyrics}. `style` informs the diction and mood; the
        lyrics themselves stay words-to-be-sung only.

        The section count follows the requested LENGTH, because YuE2 will happily sing a
        3-line song into a 2-minute slot and leave the rest to the accompaniment.
        """
        secs = float(seconds or 120.0)
        n = max(2, int(round(secs / 45.0)) + 1)
        user = ("Write the LYRICS for a %d-second song.\n\nWHAT IT IS ABOUT:\n%s\n\n"
                % (int(secs), (brief or "").strip() or "anything you like"))
        if style:
            user += ("The music — this is ONLY for the diction and the mood, do NOT describe "
                     "it in the words:\n%s\n\n" % style.strip())
        user += (
            "RULES:\n"
            "- Return ONLY words that get sung. No musical directions, no notes to the "
            "producer, no instrument names, no BPM.\n"
            "- Put a section tag on its own line: [Verse], [Chorus], [Bridge], [Outro]. Use "
            "about %d sections so the song fills the length.\n"
            "- There MUST be a [Chorus], and EVERY section must have real lines under it.\n"
            "- Keep lines short and singable, and keep the syllable count similar from line to "
            "line within a section so a melody can carry them.\n"
            "- Be concrete. One telling detail beats a general statement.\n"
            "Return {title, lyrics}.\n" % n
        )
        problems = None
        for _attempt in range(2):
            ask = user if not problems else (
                user + "\n\nYour previous attempt was REJECTED by the validator:\n- "
                + "\n- ".join(problems) + "\nReturn corrected {title, lyrics}.")
            out = self._chat(SYSTEM_LYRICS, ask, LYRICS_SCHEMA, temperature=0.9, model=model)
            problems = validate_lyrics(out.get("lyrics"))
            if not problems:
                return out
        raise DirectorError("the model could not write usable lyrics: %s" % "; ".join(problems))

    # -- DIALOGUE (comedy dialogue writing) -----------------------------------
    def dialogue(self, brief, cast, seconds=30.0, model=None):
        """Write a short comedic dialogue for a named cast \u2192 {title, lines}.

        `brief` describes the scene.  `cast` is {NAME: short bio, ...}.
        Line count target from `seconds` (~2.5 s per line, min 2).
        One retry feeds the validation errors back; then raises DirectorError.
        """
        secs = float(seconds or 30.0)
        n = max(2, int(round(secs / 2.5)))
        cast_block = "\n".join("  %s: %s" % (k, v) for k, v in sorted(cast.items()))
        cast_names = ", ".join(sorted(cast))
        user = (
            "Write a short comedy dialogue for the following cast. "
            "Every speaker MUST be one of: %s.\n\n"
            "CAST:\n%s\n\n"
            "WHAT IT IS ABOUT:\n%s\n\n"
            "RULES:\n"
            "- Keep lines short and spoken-word only. "
            "Stage directions go in `direction`, never inside `text`.\n"
            "- Write numbers as words (no digits).\n"
            "- Write about %d lines so the scene fills the length.\n"
            "- Every speaker must appear exactly as shown (upper-case).\n"
            "- Return {title, lines:[{speaker, direction, text}, ...]}.\n"
            % (cast_names, cast_block, (brief or "").strip(), n)
        )
        problems = None
        for _attempt in range(2):
            ask = user if not problems else (
                user + "\n\nYour previous attempt was REJECTED by the validator:\n- "
                + "\n- ".join(problems) + "\nReturn corrected {title, lines}."
            )
            out = self._chat(SYSTEM_DIALOGUE, ask, DIALOGUE_SCHEMA,
                             temperature=0.9, model=model)
            problems = validate_dialogue(out, cast)
            if not problems:
                return out
        raise DirectorError(
            "the model could not write usable dialogue: %s" % "; ".join(problems))

    def weirdo_fit(self, description, vein="", base=None, action="", model=None):
        """Fit a Weirdo roll to what Ryan TYPED -> {subject, skin, hair, facial_hair, wardrobe,
        accessory, setting, lighting, absurd, action}. The answer is FINAL: an empty string means
        "none", not "keep the roll" — keeping the roll's values is how a baker came back with a
        goatee (live run, 2026-10-11). `base` is the random roll, given as a starting point so
        two presses with the same words still come back different.

        ⚠️ The idiom is the whole point and the model WILL drift from it: normal in every axis,
        exactly ONE absurd physical trait, played straight. The merge in forge_lab
        (apply_weirdo_fit) enforces the one-trait half in CODE; the prompt carries the rest.
        Guarded by tools/weirdo_matrix.py.
        """
        base = base or {}
        start = "\n".join("- %s: %s" % (k, v) for k, v in base.items()
                          if isinstance(v, str) and v and k not in ("Style", "Framing"))
        user = (
            "WHAT RYAN DESCRIBED:\n%s\n\n"
            "A RANDOM ROLL to start from (replace anything that clashes with the description; "
            "keep what fits):\n%s\n- action: %s\n\n"
            "VEIN (a default flavour ONLY — the description always wins over it): %s\n\n"
            "RULES:\n"
            "- Honour EVERY concrete thing in the description (who, clothes, place, props, the "
            "absurd thing).\n"
            "- The character is NORMAL in every way except EXACTLY ONE absurd physical trait. If "
            "the description names an absurd/exaggerated trait, `absurd` IS that trait. If it "
            "does not, invent ONE that suits this person. Never two.\n"
            "- Everything is hyper-real and played completely straight. No jokes in the wording.\n"
            "- subject: WHO they are, a short noun phrase starting with a/an (e.g. 'an old "
            "fisherman'). Do NOT put the absurd trait in it — that goes ONLY in `absurd`.\n"
            "- wardrobe: the clothes only, WITHOUT the word 'wearing'.\n"
            "- setting: one concrete place. lighting: one short phrase.\n"
            "- Fill EVERY field for THIS person; your answer is final, nothing is kept from the "
            "roll unless you write it. skin / hair / facial_hair / accessory: short ordinary "
            "phrases that fit the subject, or '' for none (e.g. no facial hair on a woman unless "
            "the description asks for it). The absurd trait is the only strange thing.\n"
            "- action: what they do in a short video, starting with a VERB, no subject, "
            "gender-neutral ('their', never 'his'/'her'), deadpan and specific "
            "(e.g. 'mends a fishing net with total concentration').\n"
            "Return the JSON object only.\n"
            % ((description or "").strip(), start or "- (nothing)", action or "(none)",
               vein or "none")
        )
        out = self._chat(SYSTEM_WEIRDO, user, WEIRDO_FIT_SCHEMA, temperature=0.9, model=model)
        clean = {}
        for k in WEIRDO_FIT_SCHEMA["required"]:
            v = re.sub(r"\s+", " ", str(out.get(k) or "")).strip().strip(".").strip()
            clean[k] = v
        # never prompt "wearing wearing …" — compose_character_prompt adds the word itself
        clean["wardrobe"] = re.sub(r"^(is\s+)?wearing\s+", "", clean["wardrobe"], flags=re.I)
        # the model drops the noun ("sparse grey thinning", "pale and slightly blotchy" — live
        # run 2026-10-11), which leaves the image model guessing WHAT is grey or blotchy
        for k, noun in (("hair", "hair"), ("skin", "skin")):
            if clean[k] and noun not in clean[k].lower():
                clean[k] = clean[k] + " " + noun
        # the Subject pool has women in it: an action that says "his" mis-genders half the cast
        clean["action"] = re.sub(r"\b(his|her)\b", "their", clean["action"], flags=re.I)
        return clean

    # -- single-prompt POLISH, engine-aware ---------------------------------
    def polish(self, text, engine="h3", video_mode=None):
        """Rewrite ONE prompt in the dialect of `engine` (and `video_mode` for H3).

        This is what Forge Lab's Studio card mode calls instead of the Qwen enhancer.
        Reuses `_chat` + a {prompt: string} schema, then applies the SAME dialect rules
        `_enforce_dialect` uses, in code: an H3 r2v prompt gets the
        '<Picture 1> is the character.' tag; still-photo phrases are stripped from every
        VIDEO prompt; a qwen STILL keeps its photographic language. Never trust the model
        to place the tag — enforce it.
        """
        is_still = engine not in ("h3", "wan", "ltx", "animate2")
        if engine == "h3":
            if video_mode == "r2v":
                rules = ("This is an H3 reference-to-video prompt, which is Ref2VA. Return SIX "
                         "labelled sections in this order: subject_definitions, summary, "
                         "retention_analysis, detailed_description, overall_soundscape, "
                         "non_diegetic_music. Cite the reference INSIDE subject_definitions as "
                         "'<Subject 1> is ... in <Picture 1>'; do NOT write a bare "
                         "'<Picture 1> is the character.' tag. retention_analysis uses "
                         "fully_preserved / partially_preserved / attribute_transfer / "
                         "weak_reference. detailed_description: 1-2 style sentences, then "
                         "'[Shot 1] ...' with composition, subject, environment, action, and the "
                         "camera as type + amplitude + speed; aim 350-500 words. "
                         "overall_soundscape: 1-4 sentences. non_diegetic_music: 1-3 sentences "
                         "or N/A. 24 fps. No still-photo language.")
            elif video_mode == "cont":
                rules = ("This is an H3 clip-continuation prompt: describe what happens "
                         "next; do NOT write '<Picture N> is the character'.")
            elif video_mode == "t2v":
                rules = ("This is an H3 text-to-video prompt: no references at all, plain "
                         "cinematic description.")
            else:
                rules = "This is an H3 video prompt. 24 fps; describe action."
        elif engine == "wan":
            rules = ("This is a WAN 2.2 prompt: 16 fps, no audio, plain cinematic "
                     "description.")
        elif engine == "ltx":
            rules = ("This is an LTX 2.3 restyle prompt: describe LOOK/STYLE only — the "
                     "source clip carries the structure.")
        elif engine == "animate2":
            rules = ("This is a Wan Animate 2 prompt: keep it minimal — say only what should "
                     "stay or change; a still performs a driving clip.")
        else:
            rules = ("This is a Qwen text-to-image prompt: KEEP rich photographic language "
                     "(lens, lighting, expression).")
        user = (
            "Rewrite this prompt for the engine below. Keep the subject and the intent, make "
            "it concrete and shootable, and return {\"prompt\": <the rewrite only>}.\n\n"
            "%s\n\nPROMPT: %s" % (rules, text)
        )
        res = self._chat(
            "You are DIRECTOR, a prompt engineer for local generative video/image models. "
            "Answer with JSON only.",
            user, POLISH_SCHEMA, temperature=0.6)
        out = (res.get("prompt") or "").strip()
        # enforce the dialect in code too — never trust the model to get the format right
        out = re.sub(r"<Picture\s+\d+>\s*is the character\.?", "", out, flags=re.I).strip()
        # Ref2VA has no '<Picture 1> is the character.' tag (see _enforce_dialect).
        if engine == "h3" and video_mode == "r2v":
            # the model inlines the labels; put them back on their own lines (whitespace only)
            out = normalise_ref2va_labels(out)
        if not is_still:
            for phrase in _STILL_PHRASES:
                out = re.sub(re.escape(phrase), "", out, flags=re.I)
            out = re.sub(r"\s{2,}", " ", out)
            out = re.sub(r"\s+([.,;:])", r"\1", out)
            # stripping a phrase can leave ", ," behind — collapse and drop a leading separator
            out = re.sub(r",\s*,+", ",", out)
            out = re.sub(r"^[\s.,;:]+", "", out)
        # inject LAST so nothing downstream can strip or reflow it
        if engine == "h3":
            out = inject_negatives(out)
        return out.strip()

    # -- vision QC (a SORTING AID, not a verdict you can trust blindly) -------
    def qc_frame(self, image_path, character_still_path, expectation):
        """Compare a rendered frame to the character's still.

        Returns {verdict: 'ok'|'drift'|'broken', reasons:[...]}. This is a SORTING AID
        to triage many frames — it is a model's opinion, not a measurement. Confirm any
        'drift'/'broken' read by eye before discarding a take.
        """
        def _b64(path):
            with open(os.path.expanduser(path), "rb") as fh:
                return base64.b64encode(fh.read()).decode("ascii")

        images = [_b64(character_still_path), _b64(image_path)]
        user = (
            "Image 1 is the character's reference still. Image 2 is a frame from a render. "
            "Expectation for this shot: %s\n\n"
            "Decide whether the frame is consistent with the character. Return {verdict, "
            "reasons}: verdict 'ok' if the character and the expectation hold, 'drift' if "
            "it is recognisably the character but something changed, 'broken' if the "
            "character is lost or the frame is unusable. `reasons` is a short list of what "
            "you see. This is a triage aid, so be specific." % expectation
        )
        return self._chat(
            "You are a careful QC reviewer for a character-consistent render pipeline. "
            "Answer with JSON only.",
            user, QC_SCHEMA, temperature=0.2, images=images)


if __name__ == "__main__":
    d = Director()
    print(json.dumps(d.status(), indent=2))