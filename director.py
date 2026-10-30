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
import math
import os
import re
import urllib.error
import urllib.request

# Gemma 4 31B since 2026-10-24: the writer bake-off (claude.ai project doc
# writer-bakeoff-2026-10-23.md) - four blind judges, 10 scripts each, Gemma 4 31B 6.53 vs
# Qwen3.8 27B 5.45 vs the old Qwen3.8 27B Uncensored 5.3-5.75; ~19 GB, one card, ~6 s a
# script once loaded.
DEFAULT_MODEL = "gemma4:31b"
FALLBACK_MODEL = "huihui_ai/qwen3.5-abliterated:9b"
DEFAULT_URL = "http://forge:11434"
CTX = 16384

# THE WRITER'S BRAIN (Ryan, Oct 19: "being able to choose models like I do in hermes").
# Like Hermes, the writer is a PROVIDER + a MODEL. The rig's own Ollama is the default and the
# only one an "open weights only" piece may use; the others are opt-in cloud providers. Every
# Director() that isn't handed a model reads this from settings.json:
#     "writer": {"provider": "anthropic", "model": "claude-opus-5-5", "base_url": ""}
# ($DIRECTOR_PROVIDER / $DIRECTOR_MODEL win; an old "writer_model" means an Ollama model).
# Keys never live in settings.json or the repo: $<key_env>, else writer_keys.json (chmod 600,
# git-ignored) beside settings.json.
PROVIDERS = {
    "ollama":     {"label": "Your rig (Ollama)", "api": "ollama", "base_url": DEFAULT_URL,
                   "key_env": None, "open": True},
    "anthropic":  {"label": "Anthropic (Claude)", "api": "anthropic",
                   "base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY",
                   "suggest": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]},
    "openrouter": {"label": "OpenRouter", "api": "openai",
                   "base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
    "nous":       {"label": "Nous Portal", "api": "openai",
                   "base_url": "https://inference-api.nousresearch.com/v1",
                   "key_env": "NOUS_API_KEY"},
    "openai":     {"label": "OpenAI", "api": "openai", "base_url": "https://api.openai.com/v1",
                   "key_env": "OPENAI_API_KEY"},
    "custom":     {"label": "Custom (OpenAI-compatible URL)", "api": "openai", "base_url": "",
                   "key_env": "WRITER_API_KEY"},
}
PROVIDER_ORDER = ("ollama", "anthropic", "openrouter", "nous", "openai", "custom")


def _settings_path():
    data = os.environ.get("FORGE_LAB_DATA")
    if data:
        return os.path.join(os.path.expanduser(data), "settings.json")
    return os.path.expanduser("~/forge-lab/settings.json")


def keys_path():
    return os.path.join(os.path.dirname(_settings_path()), "writer_keys.json")


def _read_json(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def chosen_writer():
    """{"provider", "model", "base_url"} this machine is set to. Never raises."""
    st = _read_json(_settings_path())
    w = st.get("writer") if isinstance(st.get("writer"), dict) else {}
    prov = (os.environ.get("DIRECTOR_PROVIDER") or w.get("provider") or "ollama").strip()
    if prov not in PROVIDERS:
        prov = "ollama"
    model = (os.environ.get("DIRECTOR_MODEL") or w.get("model") or "").strip()
    if not model and prov == "ollama":
        model = (st.get("writer_model") or "").strip()
    if not model:
        sug = PROVIDERS[prov].get("suggest") or []
        model = sug[0] if sug else DEFAULT_MODEL
        if not sug and prov != "ollama":
            prov, model = "ollama", DEFAULT_MODEL      # a cloud provider with no model: stay home
    base = (w.get("base_url") or "").strip() or PROVIDERS[prov]["base_url"]
    return {"provider": prov, "model": model, "base_url": base}


def chosen_model():
    return chosen_writer()["model"]


def writer_key(provider):
    """The API key for a cloud provider: $<key_env>, else writer_keys.json. '' when none."""
    env = PROVIDERS.get(provider, {}).get("key_env")
    if env and os.environ.get(env):
        return os.environ[env].strip()
    return str(_read_json(keys_path()).get(provider) or "").strip()


def save_writer_key(provider, key):
    """Store a key beside settings.json, readable only by this user."""
    path = keys_path()
    d = _read_json(path)
    if key:
        d[provider] = key.strip()
    else:
        d.pop(provider, None)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh)
    os.chmod(path, 0o600)


def brain_label(model, provider="ollama"):
    p = PROVIDERS.get(provider, PROVIDERS["ollama"])
    return model if provider == "ollama" else "%s · %s" % (p["label"], model)


def _http_json(url, body=None, headers=None, timeout=600):
    """The cloud seam (tests monkeypatch it). GET when body is None."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=dict(headers or {},
                                 **({"Content-Type": "application/json"} if data else {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise DirectorError("%s -> HTTP %s %s" % (url, exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise DirectorError("%s failed: %s" % (url, exc)) from exc


def _auth_headers(provider, key):
    if PROVIDERS[provider]["api"] == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    h = {"Authorization": "Bearer " + key} if key else {}
    if provider == "openrouter":
        h.update({"HTTP-Referer": "https://github.com/rstephens11-ux/forge-lab",
                  "X-Title": "Forge Lab"})
    return h


def list_models(provider, base_url=None, timeout=15):
    """Model names a provider offers (Ollama: what the rig has pulled). Falls back to the
    provider's suggestions when the list can't be fetched; never raises."""
    p = PROVIDERS.get(provider) or PROVIDERS["ollama"]
    base = (base_url or p["base_url"] or "").rstrip("/")
    try:
        if p["api"] == "ollama":
            d = _http_json(base + "/api/tags", timeout=timeout)
            names = [m.get("name") for m in d.get("models", [])
                     if "embed" not in (m.get("name") or "")
                     and "bge" not in (m.get("name") or "")]
        else:
            path = "/v1/models" if p["api"] == "anthropic" else "/models"
            d = _http_json(base + path, headers=_auth_headers(provider, writer_key(provider)),
                           timeout=timeout)
            names = [m.get("id") for m in d.get("data", [])]
        names = [n for n in names if n]
        if names:
            return sorted(names)
    except Exception:
        pass
    return list(p.get("suggest") or ([DEFAULT_MODEL, FALLBACK_MODEL]
                                     if p["api"] == "ollama" else []))


def _to_cloud(api, model, messages, temperature, schema):
    """Ollama-shaped messages -> an OpenAI or Anthropic request body. The schema goes into the
    system prompt (every provider can follow that; the reply is still validated in-process)."""
    rule = ("\n\nReply with ONE JSON value only - no prose, no markdown fences - matching this "
            "JSON schema:\n" + json.dumps(schema))
    system = ""
    convo = []
    for m in messages:
        if m["role"] == "system":
            system += m["content"]
            continue
        imgs = m.get("images") or []
        if api == "anthropic":
            parts = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                  "data": b}} for b in imgs]
            parts.append({"type": "text", "text": m["content"]})
        else:
            parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}}
                     for b in imgs]
            parts.append({"type": "text", "text": m["content"]})
        convo.append({"role": m["role"], "content": parts if imgs else m["content"]})
    system += rule
    if api == "anthropic":
        return {"model": model, "max_tokens": 8000, "temperature": temperature,
                "system": system, "messages": convo}
    return {"model": model, "temperature": temperature,
            "messages": [{"role": "system", "content": system}] + convo}


def _from_cloud(api, resp):
    """A cloud reply -> Ollama's {"message": {"content": ...}} so _chat stays one path."""
    if api == "anthropic":
        text = "".join(b.get("text", "") for b in resp.get("content", [])
                       if b.get("type") == "text")
    else:
        ch = (resp.get("choices") or [{}])[0]
        text = (ch.get("message") or {}).get("content") or ""
    return {"message": {"content": text}}


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


class WriterSetupError(DirectorError):
    """The WRITER setting can't work as set (e.g. a cloud provider with no key). Never falls
    back silently: the person has to see it and fix the setting."""


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
    # the main body: Ref2VA's detailed_description, or the base format's (T2VA / I2VA)
    # integrated_multimodal_description - appending to the END of either lands the clause in
    # non_diegetic_music
    m = re.search(r"(?<![A-Za-z_])(?:detailed_description|integrated_multimodal_description)"
                  r"\s*:", text)
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
    "  continue -> H3 continuing a clip from the previous clip's LAST FRAME, which runs in\n"
    "    FIRST-FRAME mode (MiniMax I2VA) - NOT the six Ref2VA sections. The first line is exactly\n"
    "    'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.'\n"
    "    then a blank line, then three fields: integrated_multimodal_description: '[Shot 1]\n"
    "    <style>, <what <Picture 1> shows>, then what happens next', camera as type + amplitude +\n"
    "    speed; overall_soundscape: 1-4 sentences; non_diegetic_music: 1-3 sentences or N/A.\n"
    "  lipsync -> H3 reference-to-video WITH an audio guide; the mouth follows the audio. "
    "Same six sections as character_action; keep the spoken line the focus.\n"
    "  motion_copy -> Wan Animate 2. A still performs a driving clip; the prompt is "
    "minimal — say only what should stay or change.\n"
    "  establishing -> H3 text-to-video (MiniMax T2VA). No references and no instruction\n"
    "    line; three fields: integrated_multimodal_description: '[Shot 1] <style>, <composition>,\n"
    "    <action>', camera as type + amplitude + speed; overall_soundscape: 1-4 sentences;\n"
    "    non_diegetic_music: 1-3 sentences or N/A.\n"
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
                    lines.append("<Subject %s> (appears in [Shot 1]): weak_reference - incidental, "
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
# SCENE MODE — one script SCENE -> timed multi-shot H3 renders.
#
# The worked example Ryan approved (yg-101 sc1v2 shot1A/shot1B) is the ground truth: ONE 15 s
# render holds 2-5 TIMED SHOTS, every reference image has a stated job inside its <Subject N>
# line, the Dialogue voices ride along as <Audio N> TIMBRE references (never a lip-sync copy of a
# flat TTS take), and every line is performed with its emotion written OUTSIDE the <d> tag.
#
# ⚠️ The local model is asked ONLY for the SHOT PLAN as JSON. The six Ref2VA sections are
# assembled in CODE from that plan, because instruction-only enforcement does not bind a local
# model (see the forge-lab-development skill): left to itself it drops sections, inlines the
# labels, or forgets the negatives. `scene_prompt_problems` is the code-level check.
# ---------------------------------------------------------------------------
SCENE_MAX_SECS = 15.08                 # 362 frames at 24 fps
SCENE_MAX_CHARACTERS = 4               # 5+ named characters fails on H3
SCENE_MAX_VOICES = 3                   # H3 takes at most 3 ref_audios
SCENE_MAX_PROMPT_CHARS = 7000          # fal guide limit
SCENE_DD_WORDS = (300, 650)            # detailed_description word band
# The framing words a cut may use. Consecutive shots must NOT repeat one (every cut is a new angle).
SCENE_SHOT_TYPES = ("wide", "medium", "medium close-up", "close-up", "over-the-shoulder",
                    "low angle", "profile two-shot", "reaction")

DEFAULT_SCENE_STYLE = (
    "The target video is a hand-drawn 1990s Nickelodeon television cartoon in the style of "
    "Hey Arnold and Doug: flat gouache colour fields on textured paper, scratchy wobbly black "
    "nib-pen outlines, hard-edged single-tone cel shadows, a muted dusty palette, and lively, "
    "expressive character animation with clear poses and big readable faces."
)

# Spoken-time model, measured against the real TTS takes of this cast (not a guess): the Guide's
# 27-word line lands at ~8.7 s and the character's 12-word line at ~3.8 s, i.e. ~3.1 words/s + 0.3 s per
# line. The older 2.6 w/s + 0.6 s estimate ran ~40% slow and split scenes that comfortably fit one
# render (a two-hander became two single-character clips). Always TIGHTEN the estimate first.
SCENE_WORDS_PER_SEC = 3.1
SCENE_PER_LINE_S = 0.3
SCENE_BEAT_SECS = (1.0, 1.2, 0.8, 1.5, 0.6)   # action beats used when the cut times are derived
# A short score line (instrumentation + tempo + dynamics — never mood words, never bare N/A).
DEFAULT_SCENE_MUSIC = (
    "A sparse solo fiddle and a low plucked guitar at a slow walking tempo, quiet under the "
    "dialogue and lifting a little on the last beat."
)

# HOUSE_NEGATIVES extended for a SCENE (rule 9). Injected with inject_negatives(..., clause=)
# so it lands at the TAIL of detailed_description, before overall_soundscape, and stays idempotent.
SCENE_NEGATIVES = (
    HOUSE_NEGATIVES + " Do not add any other people: no extra family members, children, "
    "servants or background figures; every shot contains only the characters named in it. No "
    "duplicate characters, no motion-blur smearing of faces, no warping hands, and no change of "
    "art style."
)

SCENE_PLAN_SCHEMA = {
    "type": "object",
    "required": ["characters", "summary", "shots", "soundscape", "music"],
    "properties": {
        "characters": {"type": "array", "minItems": 1, "maxItems": 4,
                       "items": {"type": "string"}},
        "summary": {"type": "string"},
        "shots": {
            "type": "array", "minItems": 2, "maxItems": 5,
            "items": {
                "type": "object",
                "required": ["type", "start", "camera", "framing", "action", "lines"],
                "properties": {
                    "type": {"type": "string"},
                    "start": {"type": "number"},
                    "camera": {"type": "string"},
                    "framing": {"type": "string"},
                    "action": {"type": "string"},
                    "lines": {
                        "type": "array", "minItems": 0, "maxItems": 6,
                        "items": {
                            "type": "object",
                            "required": ["speaker", "delivery", "text"],
                            "properties": {
                                "speaker": {"type": "string"},
                                "delivery": {"type": "string"},
                                "text": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "soundscape": {"type": "string"},
        "music": {"type": "string"},
    },
}

SYSTEM_SCENE = (
    "You are a shot-planner for an H3 reference-to-video scene: ONE 15-second timeline holding "
    "2-5 timed shots. A cut must introduce a genuinely NEW angle (a different framing), never a "
    "slight reframe. Write the shot plan only; you never write the prompt's section labels. "
    "Answer with JSON only."
)

# framing-word patterns, longest written first on purpose — a shot that says 'medium close-up'
# must not be read as a plain 'close-up' or a plain 'medium'.
_SCENE_SHOT_PATTERNS = (
    ("profile two-shot", r"two-shot in profile|profile two-shot"),
    ("over-the-shoulder", r"over-the-shoulder"),
    ("medium close-up", r"medium close-?up"),
    ("close-up", r"close-?up"),
    ("reaction", r"reaction shot"),
    ("low angle", r"low angle"),
    ("wide", r"wide shot"),
    ("medium", r"medium shot"),
)

_SCENE_SHOT_MARK = re.compile(
    r"\[Shot\s+(\d+)\]\s*(?:At\s+(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\s*,)?")


# --------------------------------------------------------------------------- SCENE ACTION BEATS
# A scene's STAGE DIRECTIONS cost screen time just like its dialogue. The live defect: a ~30 s
# scene whose directions were never budgeted got crammed into ONE 15 s render — 10.6 s of dialogue
# in one static shot, then compass + kneel + touch + flash + sky-yank into three 1.5 s cuts. So the
# splitter budgets the ACTION too: a simple beat ~2.5 s, a beat with a turn ~3.5 s, a VFX beat (a
# flash, a transformation, a supernatural event) ~4 s.
SCENE_BEAT_SIMPLE = 2.5
SCENE_BEAT_TURN = 3.5
SCENE_BEAT_VFX = 4.0

# A 'turn' is the sentence where a paragraph's physical business genuinely changes gear, so a long
# stage-direction paragraph splits into at most two beats (compass business | kneel + touch).
_BEAT_TURN_RE = re.compile(
    r"\b(kneels?|kneeling|reaches?|reaching|flash(?:es)?|explodes?|exploding|"
    r"transform(?:s|ed|ation)?|teleports?|wakes?|waking|"
    r"rises?|rising|enters?|entering|arrives?|arriving|materiali[sz]es?|spins?|spinning|"
    r"turns? into|bursts?|collapses?)\b", re.I)
# A beat that IS a VFX / transformation event takes the long estimate.
_BEAT_VFX_RE = re.compile(
    r"\b(flash|thunderclap|thunder|column of light|transformation|transform|teleport|"
    r"explo(?:de|des|ding|sion)|yanked|yanks|vanishes?|vanishing|supernatural|magic|glow(?:s|ing)?|"
    r"blinding|bell inside)\b", re.I)
# The scene's final exit ('and is gone', 'steps out', 'leaves'): LAST render only.
_BEAT_EXIT_RE = re.compile(
    r"\b(is gone|are gone|leaves?|leaving|departs?|departing|exits?|exiting|"
    r"steps? out|walks? out|out of the (?:kitchen|door|room|frame))\b", re.I)
# A transition in the scene (a flash / teleport / transformation / time jump)...
_TRANSITION_RE = re.compile(
    r"\b(flash|teleport|transformation|transforms?|vanishes?|vanishing|yanked|hurls?|hurled|"
    r"column of light|time[- ]jump|thrown|flung|snatched)\b", re.I)
# ...followed by a direction that describes WAKING or ARRIVING in the NEXT place.
_WAKE_ARRIVE_RE = re.compile(
    r"\b(wakes?|waking|awakes?|awakening|comes to|arrives?|arriving|materiali[sz]es?|"
    r"finds (?:himself|herself|themselves)|face-down|face down|lands (?:in|on))\b", re.I)


def scene_beat_split(text):
    """One stage-direction paragraph -> 1-2 beats, splitting once at its first 'turn' sentence."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    sents = re.split(r"(?<=[.!?])\s+", text)
    for i in range(1, len(sents)):
        if _BEAT_TURN_RE.search(sents[i]):
            a = " ".join(sents[:i]).strip()
            b = " ".join(sents[i:]).strip()
            if a and b:
                return [a, b]
    return [text]


def scene_beat_seconds(text):
    """Estimated screen time of one action beat: simple 2.5 s, a turn 3.5 s, a VFX beat 4 s."""
    t = text or ""
    if _BEAT_VFX_RE.search(t):
        return SCENE_BEAT_VFX
    if _BEAT_TURN_RE.search(t) and len(re.findall(r"[.!?](?:\s|$)", t)) >= 2:
        return SCENE_BEAT_TURN
    return SCENE_BEAT_SIMPLE


def scene_action_beats(paragraphs):
    """Stage-direction paragraphs -> ordered, timed ACTION BEATS.

    `paragraphs` = [{'text', 'gap'}] where `gap` = how many dialogue lines precede the paragraph
    (0 = before the first line, len(lines) = after the last). Returns [{'i','text','gap','seconds',
    'kind'}], kind in {'action','exit','carried'}.

    * a beat that describes waking/arriving somewhere AFTER a transition is 'carried' to the NEXT
      scene (its characters were left behind, so it must not be staged here);
    * the scene's final exit beat is 'exit' and goes in the LAST render only;
    * every beat is assigned to exactly ONE render (the splitter never shares one).
    """
    paras = [p for p in (paragraphs or []) if (p.get("text") or "").strip()]
    has_transition = any(_TRANSITION_RE.search(p["text"]) for p in paras)
    flat = []
    for p in paras:
        for t in scene_beat_split(p["text"]):
            flat.append({"text": t, "gap": int(p.get("gap") or 0)})
    beats = []
    for i, b in enumerate(flat):
        last = i == len(flat) - 1
        kind = "action"
        if has_transition and last and _WAKE_ARRIVE_RE.search(b["text"]):
            kind = "carried"
        elif last and _BEAT_EXIT_RE.search(b["text"]):
            kind = "exit"
        beats.append({"i": i, "text": b["text"], "gap": b["gap"],
                      "seconds": scene_beat_seconds(b["text"]), "kind": kind})
    return beats


def _scene_line_text(line):
    """The spoken words of one script line (accepts a dict or a bare string)."""
    if isinstance(line, dict):
        return (line.get("text") or "").strip()
    return str(line or "").strip()


def _scene_line_speaker(line):
    """The (upper-cased, normalised) speaker of one script line, or ''."""
    if not isinstance(line, dict):
        return ""
    for key in ("speaker", "name", "character"):
        v = line.get(key)
        if v:
            try:
                from dialogue import normalise_speaker as _ns
                return _ns(str(v))
            except Exception:
                return str(v).strip().upper()
    return ""


def scene_line_seconds(line, words_per_sec=SCENE_WORDS_PER_SEC, per_line_s=SCENE_PER_LINE_S):
    """Estimated spoken time of one line: words / rate + a fixed per-line cost.

    Words/s and the per-line cost come from the MEASURED TTS takes of this cast (see the note on
    SCENE_WORDS_PER_SEC), not from an eyeballed 2.6 words/s.
    """
    return len(_scene_line_text(line).split()) / float(words_per_sec) + float(per_line_s)


def scene_line_seconds_text(text, words_per_sec=SCENE_WORDS_PER_SEC, per_line_s=SCENE_PER_LINE_S):
    """Estimated spoken time of a bare line of text (used when deriving cut times)."""
    return len(str(text or "").split()) / float(words_per_sec) + float(per_line_s)


def split_scene_lines(script_lines, max_secs=SCENE_MAX_SECS,
                      max_characters=SCENE_MAX_CHARACTERS, max_voices=SCENE_MAX_VOICES,
                      words_per_sec=SCENE_WORDS_PER_SEC, per_line_s=SCENE_PER_LINE_S,
                      beat_s=1.5, present=None, beats=None, target_secs=None):
    """Split ONE scene's LINES **and ACTION BEATS** into renders that each fit `max_secs`.

    Time model: ~3.1 spoken words/s + 0.3 s per line, PLUS each action beat's own estimate
    (2.5 s simple / 3.5 s a turn / 4 s VFX — see scene_action_beats). A line is NEVER split and a
    beat is assigned to exactly ONE render (defect 1 + 3). A 'carried' beat (a wake/arrival after a
    transition, which belongs to the NEXT scene) is left out entirely (defect 4).

    `present` = the characters PHYSICALLY PRESENT in the scene (from the stage directions).
    When given, EVERY render carries them ALL, not just the speakers — so a short two-hander that
    fits one 15.08 s render stays ONE render and nobody delivers lines to an empty frame. A render
    stops only before it would carry a 5th character (H3 fails on 5) or a 4th voice (H3 takes at
    most 3 ref_audios); too many present characters to fit means falling back to the speakers.

    `target_secs` = the scene heading's '~Ns' when present; used to report the aimed render count
    (max_secs still governs the actual split).

    Returns [{n, secs, characters:[NAME...], lines:[idx...], speakers:[NAME...], line_texts:[...],
    beats:[beat dicts]}].
    """
    present = [str(c).strip().upper() for c in (present or []) if str(c).strip()]
    if len(present) > int(max_characters):
        present = []                       # cannot carry them all at once: fall back to speakers
    beats = [b for b in (beats or []) if (b.get("kind") or "action") != "carried"]
    n = len(script_lines)
    by_gap = {}
    for b in beats:
        by_gap.setdefault(int(b.get("gap") or 0), []).append(b)

    # One ordered event stream: beats before line k, then line k.
    events = []
    for k in range(n + 1):
        for b in by_gap.get(k, []):
            events.append(("beat", b))
        if k < n:
            events.append(("line", k))

    renders, cur, cur_secs, cur_chars = [], [], 0.0, list(present)

    def _chars_for(kind, ev, base):
        if kind == "line":
            spk = _scene_line_speaker(script_lines[ev])
            return list(base) + ([spk] if spk and spk not in base else [])
        return list(base)

    def _secs_for(kind, ev):
        if kind == "line":
            return scene_line_seconds(script_lines[ev], words_per_sec, per_line_s)
        return float(ev.get("seconds") or 0.0)

    for kind, ev in events:
        t = _secs_for(kind, ev)
        new_chars = _chars_for(kind, ev, cur_chars)
        over_time = (cur_secs + t) > float(max_secs)
        over_chars = len(new_chars) > int(max_characters)
        over_voices = len(new_chars) > int(max_voices)
        if cur and (over_time or over_chars or over_voices):
            renders.append((list(cur), list(cur_chars)))
            cur, cur_secs, cur_chars = [], 0.0, list(present)
            new_chars = _chars_for(kind, ev, cur_chars)
        cur.append((kind, ev))
        cur_secs += t
        cur_chars = new_chars
    if cur:
        renders.append((list(cur), list(cur_chars)))

    out = []
    for k, (evs, chars) in enumerate(renders, 1):
        idxs, rbeats = [], []
        for kind, ev in evs:
            if kind == "line":
                idxs.append(ev)
            else:
                rbeats.append(ev)
        speakers = []
        for i in idxs:
            spk = _scene_line_speaker(script_lines[i])
            if spk and spk not in speakers:
                speakers.append(spk)
            if spk and spk not in chars:
                chars.append(spk)
        secs = sum(scene_line_seconds(script_lines[i], words_per_sec, per_line_s)
                   for i in idxs) + sum(float(b.get("seconds") or 0.0) for b in rbeats)
        out.append({"n": k, "secs": round(min(secs, float(max_secs)), 2),
                    "characters": chars, "speakers": speakers, "lines": idxs,
                    "beats": rbeats,
                    "line_texts": [_scene_line_text(script_lines[i]) for i in idxs]})
    if target_secs:
        aimed = max(1, int(math.ceil(float(target_secs) / float(max_secs))))
    else:
        aimed = len(out)
    for r in out:
        r["aimed_renders"] = aimed
    return out


def scene_shot_type(text):
    """The canonical framing word of one shot body, or None if it names none."""
    best = None
    for label, pat in _SCENE_SHOT_PATTERNS:
        m = re.search(pat, text or "", re.I)
        if m is None:
            continue
        if (best is None or m.start() < best[0]
                or (m.start() == best[0] and len(m.group(0)) > best[2])):
            best = (m.start(), label, len(m.group(0)))
    return best[1] if best else None


def scene_shots(dd):
    """Split a detailed_description body into [(n, cut_time_or_None, body), ...]."""
    ms = list(_SCENE_SHOT_MARK.finditer(dd or ""))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(dd)
        t = None
        if m.group(2) is not None:
            t = int(m.group(2)) * 60 + float(m.group(3))
        out.append((int(m.group(1)), t, (dd or "")[m.end():end].strip()))
    return out


def scene_sections(prompt):
    """{label: body} for the six Ref2VA sections (labels must be at line start)."""
    parts = re.split(r"^(%s)\s*:" % "|".join(REF2VA_SECTIONS), prompt or "", flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def scene_prompt_problems(prompt, expected_lines, n_voices=0, subjects=None):
    """Rule 1-12 checks for ONE scene render prompt. Empty list == ok.

    `expected_lines` are the script lines THIS render must perform (verbatim words inside <d>);
    `n_voices` is how many voice-timbre references the render carries. Every check is in CODE
    because the local model does not obey instructions about format.
    """
    probs = []
    t = prompt or ""
    expected = [(_scene_line_text(l) if isinstance(l, dict) else str(l or "")).strip()
                for l in (expected_lines or [])]
    expected = [e for e in expected if e]

    # 1. six Ref2VA sections, in order, each label on its own line
    found = re.findall(r"^(%s)\s*:" % "|".join(REF2VA_SECTIONS), t, flags=re.M)
    if found != list(REF2VA_SECTIONS):
        probs.append("sections: expected the six Ref2VA labels in order, found %s" % (found,))
    if re.search(r"<Picture\s*\d+>\s*is\s+the\s+(subject|character)", t, re.I):
        probs.append("sections: carries the undocumented '<Picture N> is the character.' tag")

    sec = scene_sections(t)
    sd = sec.get("subject_definitions", "")
    ra = sec.get("retention_analysis", "")
    dd = sec.get("detailed_description", "")

    # 2. summary task-type prefix
    summary = sec.get("summary", "")
    if n_voices and int(n_voices) > 0 and not re.match(
            r"\s*\[reference generation \+ audio reference\]", summary):
        probs.append("summary: must start with '[reference generation + audio reference]' "
                     "when voice refs are present")
    # 2b. defect 4: the prefix appears EXACTLY once (never stacked, never doubled by the model)
    n_pref = len(re.findall(r"\[reference generation(?:\s*\+\s*audio reference)?\]", summary))
    if n_pref > 1:
        probs.append("summary: the '[reference generation + audio reference]' prefix appears "
                     "%d times (must be exactly once)" % n_pref)

    # 3. a TIMED shot list inside the 15.08 s window, 2-5 shots, each >= 1.5 s
    shots = scene_shots(dd)
    if not (2 <= len(shots) <= 5):
        probs.append("shots: %d shot(s) in this render (must be 2-5)" % len(shots))
    prev = 0.0
    for idx, (n, ts, _body) in enumerate(shots):
        if idx == 0:
            if ts is not None:
                probs.append("shots: [Shot 1] must not carry a timestamp")
            continue
        if ts is None:
            probs.append("shots: [Shot %d] has no 'At MM:SS.mmm,' cut time" % n)
            continue
        if ts <= prev:
            probs.append("shots: cut times must strictly increase ([Shot %d] at %.3f s "
                         "after %.3f s)" % (n, ts, prev))
        elif ts - prev < 1.5:
            probs.append("shots: [Shot %d] is only %.3f s long (each shot must be >= 1.5 s)"
                         % (n, ts - prev))
        if ts >= SCENE_MAX_SECS:
            probs.append("shots: [Shot %d] cuts at %.3f s, past the %.2f s duration"
                         % (n, ts, SCENE_MAX_SECS))
        prev = ts

    # 4. every cut is a NEW angle — the framing word must change between consecutive shots
    types = [scene_shot_type(b) for _n, _t, b in shots]
    for i in range(1, len(shots)):
        if types[i] is not None and types[i] == types[i - 1]:
            probs.append("shots: [Shot %d] and [Shot %d] reuse the same framing (%s) - "
                         "every cut must be a new angle" % (shots[i - 1][0], shots[i][0], types[i]))

    # 5. every reference image has an explicit job inside its <Subject N> line
    for ln in sd.splitlines():
        for pid in re.findall(r"<Picture\s+\d+>", ln):
            if not re.match(r"\s*<Subject\s+\d+>", ln):
                probs.append("subject_definitions: %s is not inside a <Subject N> line" % pid)
            elif not re.search(r"\b(from|in)\b[^.]{0,120}%s" % re.escape(pid), ln, re.I):
                probs.append("subject_definitions: %s has no stated job in its <Subject N> line"
                             % pid)

    # 6. voices: max 3 timbre refs, each bound to a <Subject M> (Sx); retention says 'reference'
    if n_voices and int(n_voices) > SCENE_MAX_VOICES:
        probs.append("voices: %d voice refs requested (max %d)" % (int(n_voices), SCENE_MAX_VOICES))
    n_audio = len(set(re.findall(r"<Audio\s+\d+>", sd)))
    if n_audio > SCENE_MAX_VOICES:
        probs.append("voices: %d audio refs declared (max %d)" % (n_audio, SCENE_MAX_VOICES))
    if n_voices and n_audio < int(n_voices):
        probs.append("voices: %d audio refs declared but %d expected" % (n_audio, int(n_voices)))
    for m in re.finditer(r"(<Audio\s+\d+>)\s+is\s+the\s+voice-timbre\s+reference[^\n]*", sd):
        if not re.search(r"<Subject\s+\d+>\s*\(S\d+\)", m.group(0)):
            probs.append("voices: %s is not bound to a '<Subject M> (Sx)'" % m.group(1))
    if ra.strip():
        for k in sorted({int(x) for x in re.findall(r"<Audio\s+(\d+)>", sd)}):
            rm = re.search(r"<Audio\s+%d>\s*:([^\n]*)" % k, ra)
            if rm is None:
                probs.append("retention_analysis: <Audio %d> has no 'reference' line" % k)
            else:
                if "reference" not in rm.group(1):
                    probs.append("retention_analysis: <Audio %d> line is not marked 'reference'" % k)
                if re.search(r"\(\s*S\d+\s*\)", rm.group(1)):
                    probs.append("retention_analysis: <Audio %d> line carries a (Sx) - the spec "
                                 "forbids it there" % k)

    # 7. dialogue: <Subject M> (Sx) + non-empty delivery OUTSIDE <d>; every script line once
    for m in re.finditer(r"<d>(.*?)</d>", dd, re.S):
        pre = dd[:m.start()]
        sm = None
        for cand in re.finditer(r"\(\s*S\d+\s*\)", pre):
            sm = cand
        if sm is None:
            probs.append("dialogue: a <d> line has no (Sx) speaker id")
            continue
        if len(re.findall(r"[A-Za-z']{2,}", pre[sm.end():])) < 2:
            probs.append("dialogue: the line for (%s) has no delivery/emotion outside <d>"
                         % pre[sm.start():sm.end()].strip("()"))
    # A script line may legitimately be SPLIT across two consecutive <d> beats (shot1B renders
    # 'Fairfax pays by the mile… come home.' as two <d> blocks), so the dialogue is matched
    # against the JOINED text of the <d> blocks, in order.
    dtexts = [re.sub(r"^\s*\[[A-Za-z]+\]\s*", "", s).strip()
              for s in re.findall(r"<d>(.*?)</d>", dd, re.S)]
    joined = re.sub(r"\s+", " ", " ".join(dtexts)).strip()
    for line in expected:
        c = joined.count(line)
        if c == 0:
            probs.append("dialogue: script line missing from the <d> dialogue: %r" % line[:70])
        elif c > 1:
            probs.append("dialogue: script line appears %d times (must be exactly once): %r"
                         % (c, line[:70]))

    # 8. max 4 named characters; a character's retention line cites its shots
    char_lines = [ln for ln in sd.splitlines()
                  if re.match(r"\s*<Subject\s+\d+>", ln)
                  and re.search(r"turnaround sheet|whose identity comes from", ln, re.I)]
    if len(char_lines) > SCENE_MAX_CHARACTERS:
        probs.append("characters: %d named characters in one render (max %d)"
                     % (len(char_lines), SCENE_MAX_CHARACTERS))
    if ra.strip():
        for m in re.finditer(r"<Subject\s+\d+>[^\n]*", ra):
            if "(appears in [" not in m.group(0):
                probs.append("retention_analysis: '%s' lacks '(appears in [Shot N])'"
                             % m.group(0)[:26])

    # 9. the ending negatives clause
    low = dd.lower()
    if "no extra family members" not in low or "only the characters named" not in low:
        probs.append("detailed_description: missing the ending negatives clause (no extra family "
                     "members / only the characters named)")

    # 10. 1-2 style sentences before [Shot 1]; camera with amplitude and speed
    pre = dd.split("[Shot 1]")[0].strip()
    sents = [s for s in re.split(r"(?<=[.!?])\s+", pre) if s.strip()]
    if not (1 <= len(sents) <= 2):
        probs.append("detailed_description: the style must be 1-2 sentences before [Shot 1] "
                     "(found %d)" % len(sents))
    if not re.search(r"with (small|large) amplitude", dd, re.I):
        probs.append("camera: no camera move with a stated amplitude")
    if not re.search(r"at (slow|fast) speed", dd, re.I):
        probs.append("camera: no camera move with a stated speed")

    # 11. non_diegetic_music: instrumentation (+ tempo/dynamics) or N/A
    mus = sec.get("non_diegetic_music", "").strip()
    if not mus:
        probs.append("non_diegetic_music: section is empty")
    elif not re.match(r"^n/?a\.?$", mus, re.I) and not re.search(
            r"\b(fiddle|guitar|violin|piano|string|cello|double bass|bass|drum|percussion|flute|"
            r"clarinet|horn|banjo|mandolin|synth|organ|harp|choir|accordion|fife|trumpet|timpani|"
            r"dulcimer)\w*\b|\bsame\b", mus, re.I):
        probs.append("non_diegetic_music: names no instrumentation (or 'same')")

    # 13. defect 5: the body uses <Subject N> labels, never raw ALL-CAPS character names
    caps = scene_caps_names(dd, names=subjects)
    if caps:
        probs.append("detailed_description: uses ALL-CAPS character names (%s) - the body must "
                     "use <Subject N> labels, names never in caps" % ", ".join(sorted(set(caps))))

    # 14. defect 6: '... of <noun phrase> <finite verb>' joins and 'of behind'
    probs.extend(scene_join_problems(dd))

    # 15. defect 2b: no sentence may start LOWER-CASE outside a <d> dialogue block
    no_d = re.sub(r"<d>.*?</d>", " ", dd, flags=re.S)
    for m in re.finditer(r"(?<=[.!?])\s+([a-z])", no_d):
        probs.append("detailed_description: a sentence starts lower-case ('%s')"
                     % re.sub(r"\s+", " ", no_d[max(0, m.start() - 2):m.start() + 22]).strip())
    # 16. defect 1c: a possessive left written as a contraction ('he's hand', 'she's face')
    for m in re.finditer(r"\b(he|she|they)'s\s+([A-Za-z][A-Za-z'-]*)", no_d, re.I):
        nxt = m.group(2).lower()
        if nxt.endswith("ing") or nxt.endswith("ed") or nxt in _POSS_LEAVE:
            continue
        probs.append("detailed_description: possessive written as a contraction ('%s') - "
                     "use his/her/their" % m.group(0))

    # 12. length caps
    if len(t) > SCENE_MAX_PROMPT_CHARS:
        probs.append("length: prompt is %d chars, over the %d-char limit"
                     % (len(t), SCENE_MAX_PROMPT_CHARS))
    dw = len(dd.split())
    if not (SCENE_DD_WORDS[0] <= dw <= SCENE_DD_WORDS[1]):
        probs.append("detailed_description: %d words, outside the %d-%d band"
                     % (dw, SCENE_DD_WORDS[0], SCENE_DD_WORDS[1]))

    return probs


def scene_plan_order(plan):
    """(subjects, speaking_order) from a plan.

    `subjects` = the render's character order (plan['characters'] else first-speaking order) and
    drives the <Subject N> / <Picture N> numbering. `speaking_order` = order of first vocal event
    and drives the (Sx) / <Audio N> numbering. Both are what scene_mode.py writes to shots.json,
    so the prompt's <Picture N> order and the refs list order always agree.
    """
    plan = plan or {}
    subjects = [str(c).strip().upper() for c in (plan.get("characters") or []) if str(c).strip()]
    speaking = []
    for sh in plan.get("shots") or []:
        for ln in sh.get("lines") or []:
            sp = str((ln or {}).get("speaker") or "").strip().upper()
            if sp and sp not in speaking:
                speaking.append(sp)
    for sp in speaking:
        if sp not in subjects:
            subjects.append(sp)
    return subjects, speaking


# The framing phrase a shot may open with, and the describing verbs the model likes to begin an
# action with ("Focuses tightly on the Guide's weathered face"). Both are stripped so the shot type
# and the action join into ONE grammatical sentence instead of colliding ("A wide shot A flat,
# dusty landscape") or leaving a capital glued to the framing word ("medium close-up Focuses").
def _article(word):
    w = (word or "").strip()
    return "an" if w[:1].lower() in "aeiou" else "a"


_SCENE_VERB_PREFIX = re.compile(
    r"^(?:the\s+(?:framing|camera|shot|image|view|frame|angle)\s+)?"
    r"(?:focus(?:es|ed)?|tighten(?:s|ed)?|zoom(?:s|ed)?|frame(?:s|d)?|show(?:s|n|ing)?|"
    r"view(?:s|ed)?|reveal(?:s|ed)?|captur(?:es|ed|e)|present(?:s|ed)?|depict(?:s|ed)?|"
    r"offer(?:s|ed)?|display(?:s|ed)?|emphasiz(?:es|ed|e)|highlight(?:s|ed)?|center(?:s|ed)?|"
    r"settle(?:s|d)?|cut(?:s)?|pan(?:s|ned)?|track(?:s|ed)?|follow(?:s|ed)?|hold(?:s)?|"
    r"open(?:s|ed)?|close(?:s|d)?|push(?:es|ed)?|pull(?:s|ed)?|tilt(?:s|ed)?|dolly(?:s)?)"
    r"\b[^.,;:]*?\b(?:on|of|at|into|toward|towards|over|across|upon|from)\b\s*", re.I)


def _sentence_case(shot_type):
    """'wide shot' -> 'A wide shot' (or leaves an already-articled type alone)."""
    typ = (shot_type or "shot").strip()
    if re.match(r"^(a|an|the)\b", typ, re.I):
        s = typ
    else:
        s = "%s %s" % (_article(typ), typ)
    return s[0].upper() + s[1:]


_SCENE_SHOT_WORD_ALT = ("wide shot|medium shot|medium close-up|close-up|over-the-shoulder|"
                        "low angle|profile two-shot|reaction shot|reaction")
# Words that may open a caption and are safe to lower-case mid-sentence (articles, prepositions).
_SCENE_LEAD_DOWNCASE = ("A|An|The|This|That|These|Those|On|In|At|Above|Below|Across|Beyond|"
                        "Behind|Inside|Outside|Under|Over|From|With")


def _scene_action_clause(shot_type, action, framing=None):
    """Reduce a shot's action to a clause that can follow '<shot type> of <framing>.'.

    Strips a leading framing phrase (even one the model padded, 'Extreme close-up on ...'), the
    separately-supplied `framing` noun phrase if the model repeated it, a describing verb ('Focuses
    tightly on ...') and a dangling connective, then lower-cases a leading article/preposition so
    the result reads inside the sentence we just started.
    """
    act = (action or "").strip()
    if not act:
        return ""
    typ = (shot_type or "").strip()
    if framing:
        act = re.sub(r"^\s*%s\b[\s,.:;\-]*" % re.escape(framing.strip()), "", act,
                     flags=re.I).strip()
    for _ in range(2):                         # a padding adjective may hide the framing phrase
        before = act
        if typ:
            act = re.sub(r"^(?:an?\s+|the\s+)?(?:\w+\s+)?%s\b[\s,.:;\-]*" % re.escape(typ), "",
                         act, flags=re.I).strip()
        act = re.sub(r"^(?:an?\s+|the\s+)?(?:\w+\s+)?(?:%s)\b[\s,.:;\-]*" % _SCENE_SHOT_WORD_ALT,
                     "", act, flags=re.I).strip()
        if act == before:
            break
    act = _SCENE_VERB_PREFIX.sub("", act).strip()
    act = re.sub(r"^(?:of|from|showing|that|which|on|in|at)\b[\s,]+", "", act, flags=re.I).strip()
    if re.match(r"^(%s)\b" % _SCENE_LEAD_DOWNCASE, act):
        act = act[0].lower() + act[1:]         # lower only the first character, keep the word
    return act or "the scene"


def _scene_shot_subject(sh, subjects, pic):
    """The subject a shot is ABOUT (for the framing fallback): the first subject named in it, else
    the first of its speakers, else the first subject."""
    sh = sh or {}
    text = ((sh.get("action") or "") + " " + (sh.get("framing") or ""))
    for c in subjects or []:
        if re.search(r"\b%s\b" % re.escape(str(c).split()[0]), text, re.I):
            return c
    for ln in sh.get("lines") or []:
        sp = str((ln or {}).get("speaker") or "").strip().upper()
        if sp in (pic or {}):
            return sp
    return (subjects or [""])[0]


# ---- shot-join GRAMMAR & SUBJECT LABELS (defects 5 + 6) ---------------------------------------
# Finite 3rd-person / be verbs that make '... of <noun phrase> <verb>' an ungrammatical join.
# Gerunds and participles are FINE ('a close-up of Avery looking down'), so only -s / be forms.
_SCENE_FINITE_VERB = (
    "spins?|erupts?|wakes?|is|are|was|were|looks?|stands?|sits?|rises?|turns?|taps?|touches?|"
    "holds?|leans?|walks?|stares?|gazes?|frames?|shows?|reveals?|focuses|opens?|closes?|runs?|"
    "comes?|goes?|leaves?|enters?|pulls?|pushes?|takes?|gives?|puts?|says?|sees?|finds?|begins?|"
    "starts?|falls?|vanishes?|explodes?|kneels?|reaches?|arrives?|appears?|disappears?|sweeps?|"
    "drifts?|glows?|shivers?|blinks?|breathes?|catches?|throws?|hurls?|yanks?|slips?|spits?"
)
_BEAT_PREP = (r"at|in|on|of|from|to|with|as|by|for|across|behind|over|under|into|after|before|"
              r"near|beside|toward|towards|through|around|between|against|along|past|down|up|off")
# 'of behind', 'of a tight frame', and 'of <name|label> ... <finite verb>' — the exact reported
# bad joins. Intervening words may not be prepositions, so gold's 'of <Subject 1> from across the
# table' is never flagged. The head noun may NOT be a determiner/pronoun: without that guard
# 'out of the frame, leaving…' matched as noun 'the' + finite verb 'frame' (checker FALSE POSITIVE —
# 'frame' is also a verb, 'frames?'). A genuine join ('of the frame is empty') still matches once
# the article is consumed and 'frame' becomes the noun.
_SCENE_JOIN_NONOUN = (r"(?!(?:the|a|an|this|that|these|those|his|her|their|its|my|your|our)\b)")
_SCENE_BAD_JOIN_RE = re.compile(
    r"\bof\s+(?:behind\b|a tight frame\b|"
    r"(?:[Aa]n?\s+|[Tt]he\s+)?%s(?:<Subject\s+\d+>|[A-Za-z][A-Za-z']+)(?:'s|’s)?"
    r"(?:\s+(?!(?:%s)\b)[A-Za-z][A-Za-z']+){0,2}?\s+(?:%s)\b)"
    % (_SCENE_JOIN_NONOUN, _BEAT_PREP, _SCENE_FINITE_VERB),
    re.I)
# ALL-CAPS words in the shot body (a raw character NAME): the body must use <Subject N> labels.
_SCENE_CAPS_RE = re.compile(r"\b([A-Z][A-Z0-9']{2,})\b")
_SCENE_CAPS_ALLOW = {"N/A", "VFX", "TV", "OK", "PI", "AI", "USA", "MP4"}


def scene_framing_ok(framing):
    """True when `framing` is a NOUN PHRASE fit to follow 'a close-up of ...'.

    Rejects an empty string, a leading connective ('of ...', 'behind ...', 'a tight frame'), and
    any fragment smuggling a FINITE verb into the join ('... compass spins wildly'). Gerund
    phrases stay legal ('<Subject 1> standing at the plank table').
    """
    f = (framing or "").strip().rstrip(".")
    if not f:
        return False
    if re.match(r"^(of|behind|a tight frame|the shot|the camera)\b", f, re.I):
        return False
    if re.search(r"\b(?:%s)\b" % _SCENE_FINITE_VERB, f, re.I):
        return False
    return True


def scene_caps_names(text, names=None):
    """ALL-CAPS words (len>=3) in a shot body OUTSIDE the <d> dialogue — i.e. raw NAME spellings.

    `names` = the render's characters. When given, only an ALL-CAPS token that matches one of their
    names is flagged, so ALL-CAPS EMPHASIS/onomatopoeia the model copied from the stage directions
    ('the bell DINGING', 'WHITE FLASH', 'A BUS') is never mistaken for a raw name.
    """
    no_d = re.sub(r"<d>.*?</d>", " ", text or "", flags=re.S)
    toks = [w for w in _SCENE_CAPS_RE.findall(no_d) if w not in _SCENE_CAPS_ALLOW]
    if names:
        allowed = set()
        for n in names:
            for w in str(n).upper().split():
                allowed.add(w)
                allowed.add(w + "'S")
        toks = [w for w in toks if w in allowed]
    return toks


def scene_join_problems(text):
    """Problems for a 'shot cuts to ... of X' join that collides with a finite verb (defect 6).

    A common-noun prepositional phrase followed by a COPULA is grammatical ('the texture of the wood
    is prominent', 'the edge of the frame is empty') and is NOT flagged; a join onto a NAME/<Subject>
    ('… of <Subject 1> is standing') or onto any non-copula verb still is.
    """
    out = []
    for m in _SCENE_BAD_JOIN_RE.finditer(text or ""):
        s = re.sub(r"\s+", " ", m.group(0)).strip()
        verb = s.split()[-1].lower()
        if verb in _OBJECT_COPULA and not re.search(r"<Subject\s+\d+>|[A-Z][a-z]|[A-Z]{2,}", s):
            continue
        out.append("detailed_description: ungrammatical shot join '%s'" % s)
    return out


# ---- LOCATION NAMING & SENTENCE SEAMS (defects 2 + 5) ----------------------------------------
# Leading connective words a location's lead phrase is cut at ('a flat tar roof at night' -> 'a flat
# tar roof'), so a derived name stays a place, not a clause.
_LOC_PREP_RE = re.compile(r"\b(?:in|under|on|at|with|of|near|beside|by|from|for|into|over|"
                          r"during|beyond|across|behind)\b", re.I)


def derive_location_name(desc):
    """A short human place name from a location DESCRIPTION's lead phrase (defect 5).

    'the 1752 farmhouse kitchen: …' -> 'the farmhouse kitchen'; 'a wild Shenandoah ridge in 1752
    under a darkening storm sky: …' -> 'the wild Shenandoah ridge'. Used only when the locations
    json carries no explicit `name` — the explicit name always wins.
    """
    d = (desc or "").strip()
    if not d:
        return ""
    lead = re.split(r"[:;]", d, 1)[0]
    lead = lead.split(",")[0].strip()
    lead = re.sub(r"\b(1[0-9]{3}|20[0-9]{2})\b", " ", lead)          # drop a year
    lead = _LOC_PREP_RE.split(lead, 1)[0]                             # stop at a preposition
    lead = re.sub(r"^(a|an|the)\s+", "", lead.strip(), flags=re.I)
    lead = re.sub(r"\s{2,}", " ", lead).strip(" ,.")
    if not lead:
        return ""
    return lead if re.match(r"^(the|a|an)\b", lead, re.I) else "the " + lead


def _strip_location_lead(act, location_name="", location_desc=""):
    """Drop a leading 'the <place>,' fragment the model glued to the front of an action (defect 2).

    'the 1752 farmhouse kitchen, he leans against the frame …' -> 'he leans against the frame …'.
    Only a NOUN phrase (<= 12 words, NO finite verb) that overlaps the location's name or description
    is removed — a real locating sentence ('the wild ridge is dark under a storm sky, …' carries the
    finite verb 'is') is left for the sentence-casing pass instead.
    """
    if not act:
        return act
    m = re.match(r"^\s*([^,.!?]{1,140}?),\s+(\S.*)$", act)
    if not m:
        return act
    frag, rest = m.group(1).strip(), m.group(2).strip()
    if not rest or len(frag.split()) > 12:
        return act
    if re.search(r"\b(?:%s)\b" % _SCENE_FINITE_VERB, frag, re.I):
        return act
    kws = set(re.findall(r"[a-z]{3,}", (location_name or "").lower()))
    kws |= set(re.findall(r"[a-z]{3,}", (location_desc or "").lower()))
    shares = sum(1 for w in re.findall(r"[a-z]{3,}", frag.lower()) if w in kws)
    starts_art = bool(re.match(r"^(the|a|an)\b", frag, re.I))
    if shares >= 2 or (starts_art and shares >= 1 and kws):
        return rest
    return act


def _cap_sentences(text):
    """Capitalise the first letter of EVERY sentence, except inside a <d>…</d> dialogue block
    (defect 2b). A model-written fragment after a period ('… doorway. the kitchen, he leans …')
    otherwise leaves a lower-case sentence in the shot body."""
    if not text:
        return text
    parts = re.split(r"(<d>.*?</d>)", text, flags=re.S)
    for i, part in enumerate(parts):
        if i % 2 == 1:                       # the protected <d> captures
            continue
        parts[i] = re.sub(r"(^|[.!?]\s+)([a-z])",
                          lambda m: m.group(1) + m.group(2).upper(), part)
    return "".join(parts)


def _strip_style_from_summary(summary, style):
    """Drop leading sentences of a plan summary that merely restate the STYLE (defect 4).

    The local model sometimes echoes the style into `summary` ('A hand-drawn 1990s Nickelodeon-style
    scene where …'), which then sits after our own prefix. Any leading sentence sharing >= 3 content
    words (len >= 4) with the style text is removed.
    """
    stoks = set(re.findall(r"[a-z]{4,}", (style or "").lower()))
    if not stoks:
        return summary
    sents = re.split(r"(?<=[.!?])\s+", (summary or "").strip())
    i = 0
    while i < len(sents):
        wt = set(re.findall(r"[a-z]{4,}", sents[i].lower()))
        if len(wt & stoks) >= 3:
            i += 1
        else:
            break
    return " ".join(sents[i:]).strip()


_POSS_PRONOUN = {"he": "his", "she": "her", "they": "their"}
_OBJ_PRONOUN = {"he": "him", "she": "her", "they": "them"}
# A pronoun replacing a name in OBJECT position needs him/her/them ('a column surrounds Avery' ->
# '... surrounds him'). Object case is used when the name follows a PREPOSITION or a transitive
# finite verb; a copula ('is/was') keeps the subject form.
_OBJECT_PREP = set(
    ("to with for at near beside behind from toward towards around by over under into on of past "
     "after before against without about across between among through above below off upon "
     "within along").split())
_OBJECT_COPULA = {"is", "are", "was", "were", "be", "been", "being", "am"}
# Transitive verbs that (with `_SCENE_FINITE_VERB`) put a following NAME in object case. Includes
# the common -ing PARTICIPLES ('watching <Name>' -> 'watching him'), which finite-verb lists miss.
# Kept separate from the join grammar so broadening it never changes `scene_join_problems`.
_OBJECT_VERB = (_SCENE_FINITE_VERB + "|surrounds?|lifts?|raises?|covers?|grabs?|grips?|embraces?|"
                "carries?|encircles?|drags?|confronts?|greets?|wraps?|startles?|blocks?|"
                "watching|holding|following|facing|carrying|seeing|observing|studying|gripping|"
                "grabbing|covering|dragging|lifting|raising|pushing|pulling|joining|greeting|"
                "addressing|confronting|wrapping|surrounding|touching|leading|watches?|watch")
# 'he's X' is a possessive ONLY when X is not a verb/participle/adjective/particle. These are the
# -ing / -ed endings are excluded structurally too (see _repair_possessive).
_POSS_LEAVE = set(
    ("a an the this that these those not no so too here there up down out in on off away again "
     "already still just now gone back tired ready dead done sure awake asleep alone going gonna "
     "about able got get gets been being had has have will would can could should must may might "
     "do does did is are was were am forced about").split())


def _repair_possessive(text):
    """'he's <noun>' -> 'his <noun>' when a POSSESSIVE is meant, never when 'he is' is meant.

    The local model writes 'he's hand holds …' / 'she's hands tuck …' where 'his/her' belongs
    (defect 1c). Only the pronoun + "'s" + a NOUN is rewritten; a following -ing/-ed word or a
    common predicate/particle ('he's tired', 'he's gone', 'he's a man') is left alone. Sentence-
    initial capitals are preserved ('She's hands' -> 'Her hands').
    """
    if not text or "'s" not in text:
        return text

    def _repl(m):
        head, nxt = m.group(1), m.group(2)
        low = nxt.lower()
        if low.endswith("ing") or low.endswith("ed") or low in _POSS_LEAVE:
            return m.group(0)
        poss = _POSS_PRONOUN.get(head.lower())
        if not poss:
            return m.group(0)
        if head[0].isupper():
            poss = poss[0].upper() + poss[1:]
        return poss + " " + nxt

    return re.sub(r"\b(he|she|they)'s\s+([A-Za-z][A-Za-z'-]*)", _repl, text, flags=re.I)


def scene_full_names(subjects, looks_by_char):
    """{NAME: 'Avery Stone'} — the FULL name where the LOOKS text carries one (defect 1a).

    The model writes the surname ('Avery Stone stands'), so the label pass must consume the
    whole name; matching only 'Avery' leaves 'Stone' orphaned ('he Stone stands'). The
    full name is the subject's own name plus the immediately following Capitalised surname found in
    its looks text; a character with no surname in the looks (the GUIDE) gets none.
    """
    out = {}
    for c in subjects or []:
        txt = (looks_by_char or {}).get(c) or ""
        first = str(c).split()[0].title()
        m = re.search(r"\b%s\s+([A-Z][a-z]+)\b" % re.escape(first), txt)
        if m:
            out[c] = "%s %s" % (first, m.group(1))
    return out


def scene_label_names(*texts, subjects=None, pic=None, gender_of=None, full_names=None):
    """Replace raw character NAMES in a shot's text with '<Subject N>' at FIRST mention and a
    gendered pronoun after (defect 5). Matching is case-sensitive on the name and its Title-case
    form only, so the common noun 'guide' is never touched. A FULL name ('Avery Stone') is
    matched before the bare first name so the surname is consumed with it (defect 1a); a possessive
    name becomes his/her/their, never "he's" (defect 1b); a model-written "he's <noun>" is repaired
    to 'his <noun>' (defect 1c). Returns one string per input, in order, with a SHARED first-mention
    counter (framing, then action)."""
    subjects = subjects or []
    pic = pic or {}
    gender_of = gender_of or {}
    full_names = full_names or {}
    forms = {}
    for c in subjects:
        lab = "<Subject %d>" % pic.get(c, 1)
        toks = []
        fn = (full_names.get(c) or "").strip()
        if fn:
            toks += [fn, " ".join(w.title() for w in fn.split())]
        toks += [str(c), str(c).title(), " ".join(w.title() for w in str(c).split()),
                 str(c).replace("_", " ")]
        for tok in toks:
            tok = tok.strip()
            if tok:
                forms.setdefault(tok, (lab, c))
    if not forms:
        return [_repair_possessive(t or "") for t in texts]
    # an optional leading article is swallowed with the name ('the GUIDE' -> '<Subject 2>')
    pat = re.compile(r"\b(?:(?:the|The|a|A|an|An)\s+)?(?:%s)(?:'s|’s|s')?\b"
                     % "|".join(re.escape(t) for t in sorted(forms, key=len, reverse=True)))
    seen = {}

    def _sub(m):
        full = m.group(0)
        at_start = bool(re.search(r"(^|[.!?:]\s+)\s*$", m.string[:m.start()]))
        art = re.match(r"^(?:the|a|an)\s+", full, re.I)   # drop the article the name needed
        core = full[art.end():] if art else full
        base, poss = core, ""
        for suf in ("'s", "’s", "s'"):
            if core.endswith(suf):
                base, poss = core[:-len(suf)], suf
                break
        info = forms.get(base)
        if info is None:
            return full
        lab, c = info
        if c not in seen:
            seen[c] = lab
            return lab + poss
        g = (gender_of.get(c) or "").strip().lower()
        pron = {"male": "he", "female": "she"}.get(g, "they")
        if poss:                                   # 'Avery's' -> his/her/their, NEVER "he's"
            pron = {"he": "his", "she": "her", "they": "their"}[pron]
        else:                                      # object position -> him/her/them
            pm = re.search(r"([A-Za-z][A-Za-z']*)\s*$", m.string[:m.start()])
            prev = (pm.group(1).lower() if pm else "")
            if (not at_start and prev not in _OBJECT_COPULA
                    and (prev in _OBJECT_PREP
                         or re.match(r"^(?:%s)$" % _OBJECT_VERB, prev, re.I))):
                pron = _OBJ_PRONOUN[pron]
        if at_start:                               # a replaced name at a sentence start stays capped
            pron = pron[0].upper() + pron[1:]
        return pron

    return [_repair_possessive(pat.sub(_sub, t or "")) for t in texts]


def scene_speaker_change_problems(shots, short_secs=2.0,
                                  words_per_sec=SCENE_WORDS_PER_SEC, per_line_s=SCENE_PER_LINE_S):
    """defect 2: a SPEAKER CHANGE usually wants its own shot/angle. Two or more speakers may share
    ONE shot only when EVERY line in it is short (< `short_secs` each) — the gold 1A pattern, where
    'I shall.' and 'You will eat.' share a shot. A long line sharing a shot with another speaker is
    a problem."""
    probs = []
    for i, sh in enumerate(shots or []):
        lines = sh.get("lines") or []
        spk = {str((ln or {}).get("speaker") or "").strip().upper() for ln in lines}
        spk.discard("")
        if len(spk) < 2:
            continue
        long_ones = [ln for ln in lines
                     if scene_line_seconds_text((ln or {}).get("text"), words_per_sec, per_line_s)
                     >= float(short_secs)]
        if long_ones:
            probs.append("shots: [Shot %d] puts a long line from %s beside another speaker - a "
                         "speaker change wants its own angle (two lines may share a shot only when "
                         "both are under %.1f s)"
                         % (i + 1, ", ".join(sorted(spk)), short_secs))
    return probs


def scene_shot_cut_times(shots, max_secs=SCENE_MAX_SECS, beats=SCENE_BEAT_SECS):
    """Cut times (seconds) derived IN CODE from the estimated spoken time of each shot's lines plus
    a short action beat — never from the numbers the model writes. Strictly increasing, every shot
    at least 1.5 s, the last cut inside `max_secs`. Returns [0.0, start_of_shot2, ...].
    """
    shots = shots or []
    n = len(shots)
    if n == 0:
        return []
    durs, floors = [], []
    for sh in shots:
        spoken = sum(scene_line_seconds_text((l or {}).get("text"))
                     for l in (sh.get("lines") or []))
        # defect 1: a shot that CARRIES ACTION gets at least 2.5 s (a 1.5 s cut cannot hold a beat);
        # only a pure reaction cut (no action, no spoken words) may run at the 1.5 s floor.
        has_action = bool((sh.get("action") or "").strip()) or bool((sh.get("framing") or "").strip())
        floor = SCENE_BEAT_SIMPLE if has_action else 1.5
        floors.append(floor)
        durs.append(max(floor, spoken + beats[len(durs) % len(beats)]))
    total = sum(durs)
    if total > max_secs:                       # trim the beats first, then scale, never below floor
        over = total - max_secs
        for i in range(n):
            if over <= 0:
                break
            cut = min(over, durs[i] - floors[i])
            durs[i] -= cut
            over -= cut
        if sum(durs) > max_secs:
            f = max_secs / sum(durs)
            durs = [max(floors[i], d * f) for i, d in enumerate(durs)]
    starts, t = [0.0], 0.0
    for d in durs[:-1]:
        t = min(t + d, max_secs - 0.001)
        starts.append(round(t, 3))
    return starts


def scene_timeline_seconds(shots, max_secs=SCENE_MAX_SECS, beats=SCENE_BEAT_SECS):
    """The render length implied by the derived shot timeline (last cut + its own shot's beat)."""
    starts = scene_shot_cut_times(shots, max_secs=max_secs, beats=beats)
    if not starts:
        return 0.0
    n = len(shots)
    if n == 1:
        spoken = sum(scene_line_seconds_text((l or {}).get("text"))
                     for l in (shots[0].get("lines") or []))
        return round(min(max_secs, max(1.5, spoken + beats[0])), 2)
    return round(min(max_secs, starts[-1] + 1.5), 2)


_SCENE_PITCH_WORDS = {-2: "deep", -1: "low", 0: "medium", 1: "high", 2: "high"}
# Writing-style notes ('never uses contractions', 'not flowery', 'formal cadence') describe the
# PAGE, not the SPOKEN voice — they must not reach the voice-timbre intro (defect 7).
_SCENE_STYLE_NOTE_RE = re.compile(
    r"contraction|flowery|cadence|writing|writ(?:es|ing)|punctuat|grammar|vocabulary|"
    r"not a word|never says|\bwords?\b|sentences?", re.I)


def scene_voice_intro(meta):
    """The FULL voice intro for a speaker's FIRST line, built in code from the cast json
    (gender/age/accent/sliders) — the model only ever supplies the per-line emotion.

    defect 7: a female character gets 'gentlewoman' and 'her' (the cast json's accent literally
    says 'gentleman'; the json is NOT edited); writing-style notes are dropped from the SPOKEN-voice
    intro while timbre/attitude words survive; PITCH comes from the sliders (-2..+2 -> deep/low/
    medium/high) and age from age_years.
    """
    meta = meta or {}
    g = (meta.get("gender") or "").strip().lower()
    noun = {"male": "man", "female": "woman"}.get(g, "person")
    age = meta.get("age_years")
    age_w = ""
    if isinstance(age, (int, float)) and not isinstance(age, bool):
        if age <= 24:
            age_w = "young "
        elif age >= 65:
            age_w = "old "
        elif age >= 55:
            age_w = "older "
    sliders = meta.get("sliders") or {}
    try:
        p = int(sliders.get("pitch", 0))
    except (TypeError, ValueError):
        p = 0
    pitch_w = _SCENE_PITCH_WORDS[max(-2, min(2, p))]
    accent = (meta.get("accent") or "").split(",")[0].strip()
    if g == "female":                          # 'early American gentleman' -> '... gentlewoman'
        accent = re.sub(r"\bgentleman\b", "gentlewoman", accent)
        accent = re.sub(r"\bhis\b", "her", accent)
        accent = re.sub(r"\bhim\b", "her", accent)
    keep = [x.strip() for x in (meta.get("notes") or "").split(",") if x.strip()]
    keep = [x for x in keep if not _SCENE_STYLE_NOTE_RE.search(x)]
    phrase = "%s %s%s with a %s voice" % (_article(age_w + noun), age_w, noun, pitch_w)
    if keep:
        phrase += ", " + ", ".join(keep[:3])
    if accent:
        phrase += ", with %s %s accent" % (_article(accent), accent)
    return phrase


def _scene_line_delivery(delivery, speaker, first):
    """The delivery clause before a <d> block. First line of a speaker = the full voice intro built
    from the cast (added by the caller) + the model's per-line emotion; later lines = just the
    model's per-line emotion (the gold pattern). """
    d = (delivery or "").strip().strip(",").strip()
    if len(re.findall(r"[A-Za-z']{2,}", d)) < 2:
        d = "replies plainly" if not first else "speaks the line in %s's own voice" % (
            speaker.title() or "their")
    return d


def scene_prompt_from_plan(plan, cast=None, refs_by_char=None, location=None,
                           voices_by_char=None, secs=SCENE_MAX_SECS, style=DEFAULT_SCENE_STYLE,
                           looks_by_char=None, voice_meta=None, location_name=None,
                           location_desc=None, present=None, music="", allow_na_music=False,
                           derive_cuts=True):
    """Assemble the six-section Ref2VA prompt IN CODE from a shot-plan JSON.

    `plan` = {"characters":[...], "summary": "...", "shots":[{"type","start","camera","action",
              "lines":[{"speaker","delivery","text"}, ...]}, ...], "soundscape": "...",
              "music": "..."} and optionally "subjects": [{"name","description","features"}].
    Picture numbering follows the refs list the caller writes to shots.json: characters in
    subject order first, then the location.
    """
    cast = cast or {}
    refs_by_char = refs_by_char or {}
    voices_by_char = voices_by_char or {}
    looks_by_char = looks_by_char or {}
    voice_meta = voice_meta or {}
    plan = plan or {}
    shots = plan.get("shots") or []
    subjects, speaking = scene_plan_order(plan)
    if present:                                # every character physically in the scene, in order
        fixed = [str(c).strip().upper() for c in present if str(c).strip()]
        for c in subjects:
            if c not in fixed:
                fixed.append(c)
        subjects = fixed
    sx = {sp: "S%d" % (i + 1) for i, sp in enumerate(speaking)}
    pic = {c: i + 1 for i, c in enumerate(subjects)}
    loc_pic = (len(subjects) + 1) if location else None
    # defect 3: audio refs are numbered CONTIGUOUSLY 1..n over the speakers that ACTUALLY carry a
    # voice ref, in speaking order — never the raw speaking index (a voiceless first speaker left a
    # lone '<Audio 2>' in a render). The <Audio N> line still cites the speaker's real (Sx).
    audio, _an = {}, 0
    for sp in speaking:
        if voices_by_char.get(sp):
            _an += 1
            audio[sp] = _an
    full_names = scene_full_names(subjects, looks_by_char)
    # defect 5: a human PLACE name for the location, never the reference filename. The explicit
    # `name` (passed in or in the plan) wins, else derive from the description's lead phrase.
    loc_name, loc_desc = "", ""
    if location:
        loc_desc = (location_desc if location_desc is not None
                    else plan.get("location_description") or "").strip().rstrip(".")
        loc_name = (location_name or plan.get("location_name") or "").strip()
        if not loc_name:
            loc_name = derive_location_name(loc_desc)
        if not loc_name:
            loc_name = os.path.basename(str(location)).rsplit(".", 1)[0].replace("-", " ")
    desc_by = {}
    for s in plan.get("subjects") or []:
        if isinstance(s, dict):
            desc_by[str(s.get("name") or "").strip().upper()] = s

    # subject_definitions — the <Subject N> line carries the character's APPEARANCE (the looks json
    # / the plan's features), never the cast json's voice/accent notes. Voice notes belong in the
    # dialogue delivery only.
    sd = []
    for c in subjects:
        info = desc_by.get(c, {})
        looks = (looks_by_char.get(c) or "").strip().rstrip(".")
        if looks:
            who, feats = looks, ""
        else:
            who = (info.get("description") or info.get("features")
                   or c.replace("_", " ").title()).strip().rstrip(".")
            feats = ""
            if info.get("description") and info.get("features"):
                who = info["description"].strip().rstrip(".")
                feats = info["features"].strip().rstrip(".")
        line = "<Subject %d> is %s, whose identity comes from the character turnaround sheet " \
               "<Picture %d>" % (pic[c], who, pic[c])
        if feats:
            line += ": %s" % feats
        sd.append(line + ".")
    if location:
        line = "<Subject %d> is %s from <Picture %d>" % (loc_pic, loc_name, loc_pic)
        if loc_desc:
            line += ": %s" % loc_desc
        sd.append(line + ".")
    for sp in speaking:
        if sp in audio:
            sd.append("<Audio %d> is the voice-timbre reference for <Subject %d> (%s)."
                      % (audio[sp], pic.get(sp, 1), sx[sp]))

    # summary. defect 4: strip any [reference generation …] prefix and any STYLE sentence the model
    # itself wrote into `summary` BEFORE we add ours, so the prefix appears exactly once.
    n_voices = len(audio)
    prefix = "[reference generation + audio reference] " if n_voices else "[reference generation] "
    summary = (plan.get("summary") or "A %.2f-second %d-shot scene." % (float(secs), len(shots)))
    summary = re.sub(r"^\s*(?:\[reference generation(?:\s*\+\s*audio reference)?\]\s*)+",
                     "", summary, flags=re.I)
    summary = _strip_style_from_summary(summary, style)
    summary = summary.strip().rstrip(".").strip()
    if not summary:                     # stripping the model's style/prefix left nothing to say
        summary = "A %.2f-second %d-shot scene" % (float(secs), len(shots))
    if n_voices:
        aud = " and ".join("<Audio %d>" % audio[sp] for sp in speaking if sp in audio)
        if n_voices == 1:
            verb = "sets the voice"
        elif n_voices == 2:
            verb = "set the two voices"
        else:
            verb = "set the %d voices" % n_voices
        summary += "; %s %s" % (aud, verb)
    summary_line = prefix + summary + "."

    # retention_analysis — a character's shots are the ones where they SPEAK or are named in the
    # action; a character physically present but silent in every shot cites the whole render.
    ra = []
    all_shots = "[Shot 1]" if len(shots) == 1 else ", ".join(
        "[Shot %d]" % (i + 1) for i in range(len(shots)))
    for c in subjects:
        shots_c = []
        for i, sh in enumerate(shots):
            says = any(str((ln or {}).get("speaker") or "").strip().upper() == c
                       for ln in (sh.get("lines") or []))
            named = re.search(r"\b%s\b" % re.escape(c.split()[0]), sh.get("action") or "", re.I)
            if says or named:
                shots_c.append(i + 1)
        seen = ", ".join("[Shot %d]" % i for i in shots_c) if shots_c else all_shots
        ra.append("<Subject %d> (appears in %s): fully_preserved - identity, hair, wardrobe and "
                  "expression are retained." % (pic[c], seen))
    if location:
        ra.append("<Subject %d> (appears in %s): partially_preserved - the location is retained "
                  "while the camera angle changes." % (loc_pic, all_shots))
    for sp in speaking:
        if sp in audio:
            ra.append("<Audio %d>: reference - %s's lines follow the voice timbre of <Audio %d>; "
                      "the words come from the script." % (audio[sp], sp.title(), audio[sp]))

    # detailed_description. Cut times are derived IN CODE from the estimated line durations + short
    # action beats (the model's own `start` numbers are ignored); shot type and action are joined
    # into one grammatical sentence; a speaker's FIRST line gets the full voice intro from the cast,
    # later lines only the per-line emotion.
    starts = (scene_shot_cut_times(shots) if derive_cuts
              else [float(sh.get("start") or 0.0) for sh in shots])
    parts = [style.strip()]
    seen_speaker = set()
    gender_of = {c: (voice_meta.get(c) or {}).get("gender") for c in subjects}
    for i, sh in enumerate(shots):
        n = i + 1
        typ = (sh.get("type") or "").strip()
        if re.match(r"^(?:%s)$" % _SCENE_SHOT_WORD_ALT, typ, re.I):
            typ = typ.lower()                      # a model-capitalised type ('Close-Up') -> 'a close-up'
        art = _sentence_case(typ)                  # 'A wide shot' — sentence-initial form
        # framing is a NOUN PHRASE the model returns SEPARATELY from the action (defect 6). If it is
        # missing or not a noun phrase, fall back to the shot's subject label.
        framing = (sh.get("framing") or "").strip().rstrip(".")
        if not scene_framing_ok(framing):
            framing = ""
        act = _scene_action_clause(typ, (sh.get("action") or "").strip(), framing or None)
        # defect 2a: drop a leading location-fragment the model glued to the action.
        act = _strip_location_lead(act, loc_name, loc_desc)
        # defect 1/5: names -> <Subject N> at first mention per shot, a pronoun after; the FULL name
        # is consumed as one, a possessive becomes his/her/their, and "he's <noun>" is repaired.
        framing_l, act_l = scene_label_names(framing, act, subjects=subjects, pic=pic,
                                             gender_of=gender_of, full_names=full_names)
        # a framing NOUN PHRASE is joined after 'of', so a model-capitalised article reads wrong
        # ('a low angle of The base…' -> '… of the base…'); lower-case ONLY a leading article.
        framing_l = re.sub(r"^(The|A|An|This|That|These|Those)\b",
                           lambda m: m.group(1)[0].lower() + m.group(1)[1:], framing_l)
        if not framing_l:
            framing_l = "<Subject %d>" % pic.get(_scene_shot_subject(sh, subjects, pic), 1)
        if n == 1:
            body = "[Shot 1] %s of %s." % (art, framing_l)
        else:
            st = float(starts[i] if i < len(starts) else 0.0)
            body = "[Shot %d] At %02d:%06.3f, the shot cuts to %s of %s." % (
                n, int(st // 60), st % 60, art[0].lower() + art[1:], framing_l)
        if act_l:
            body += " " + act_l
        for ln in sh.get("lines") or []:
            sp = str((ln or {}).get("speaker") or "").strip().upper()
            first = sp not in seen_speaker
            seen_speaker.add(sp)
            emo = _scene_line_delivery((ln.get("delivery") or ""), sp, first)
            if first:
                intro = scene_voice_intro(voice_meta.get(sp)) if voice_meta.get(sp) else ""
                delivery = "%s, %s" % (intro, emo) if intro else emo
                body += " <Subject %d> (%s), %s, <d>[English] %s</d>" % (
                    pic.get(sp, 1), sx.get(sp, "S1"), delivery, (ln.get("text") or "").strip())
            else:
                body += " <Subject %d> (%s) %s, <d>[English] %s</d>" % (
                    pic.get(sp, 1), sx.get(sp, "S1"), emo, (ln.get("text") or "").strip())
        cam = (sh.get("camera") or "").strip()
        if cam:
            body += " The camera " + cam
            if not body.rstrip().endswith((".", "!", "?")):
                body += "."
        parts.append(body)
    dd = " ".join(p for p in parts if p).strip()
    if not dd.endswith((".", "!", "?")):
        dd += "."
    # defect 2b: make every sentence start capitalised (never inside <d>), so a fragment the model
    # left after a period ('… doorway. the kitchen, he leans …') reads as its own sentence.
    dd = _cap_sentences(dd)
    if "no extra family members" not in dd.lower():
        dd += " " + SCENE_NEGATIVES

    soundscape = (plan.get("soundscape") or "").strip() or "Room tone and natural ambience."
    raw_music = (music if music else (plan.get("music") or "")).strip()
    if allow_na_music:
        music_line = raw_music or "N/A"
    else:                                       # never bare N/A: a short score line by default
        music_line = (raw_music if raw_music and not re.match(r"^n/?a\.?$", raw_music, re.I)
                      else DEFAULT_SCENE_MUSIC)

    return "\n".join([
        "subject_definitions:", "\n".join(sd),
        "summary:", summary_line,
        "retention_analysis:", "\n".join(ra),
        "detailed_description:", dd,
        "overall_soundscape:", soundscape,
        "non_diegetic_music:", music_line,
    ]) + "\n"


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
# A SOUND or an ACTION written as a spoken line (2026-10-24: the golden retriever's lines were
# "Woof!" and "*wags tail*" - the voice engine reads them out as words). Animal and machine
# noises only: "Hmm." / "Uh." are things people say.
_NOISE_WORDS = set("""woof woofs arf ruff bark barks yip yap grr grrr meow miaow mew purr hiss
moo oink quack baa neigh tweet chirp squawk ribbit howl awoo bow wow beep beeps boop bzz bzzt
buzz whirr whir ding dong ping bleep blip click clunk vroom honk achoo pant pants whimper
whine growl sniff sniffs""".split())
_ACTION_RE = re.compile(r"\*[^*]{2,}\*")


def noise_line(text):
    """True when a line's `text` is a sound or an action, not words: *wags tail*, or at least
    half its words animal / machine noises ("Woof woof!", "Beep. Boop.")."""
    t = (text or "").strip()
    if _ACTION_RE.search(t):
        return True
    ws = re.findall(r"[a-z]+", t.lower())
    if not ws:
        return bool(t)
    noise = sum(1 for w in ws if w in _NOISE_WORDS or re.fullmatch(r"(gr+|bz+|hm+ph|a+w+o+)", w))
    return noise * 2 >= len(ws)


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
        if text and noise_line(text):
            errs.append("line %d is a sound or an action (%r), not words - put it in "
                        "`direction`; a speaker who can't talk gets no lines" % (i, text[:40]))

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


def _keep_alive():
    """The writer's Ollama keep_alive = the CARDS panel's idle setting (cards.keep_alive)."""
    try:
        import cards as _cards
        return _cards.keep_alive()
    except Exception:
        return "15m"


class Director:
    def __init__(self, host=None, model=None, fallback=FALLBACK_MODEL, timeout=600,
                 provider=None, base_url=None):
        self.host = (host or os.environ.get("DIRECTOR_URL") or DEFAULT_URL).rstrip("/")
        if model:                              # an explicit model is an Ollama model unless told
            w = {"provider": provider or "ollama", "model": model, "base_url": base_url or ""}
        else:                                  # the WRITER setting (machine room)
            w = chosen_writer()
            if provider:
                w["provider"] = provider
        self.provider = w["provider"] if w["provider"] in PROVIDERS else "ollama"
        self.model = w["model"]
        self.base_url = ((base_url or w.get("base_url") or PROVIDERS[self.provider]["base_url"])
                         .rstrip("/"))
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
        if model is None and self.is_cloud():
            return True                          # always "ready": it lives elsewhere
        model = model or self.model
        base = model.split(":")[0]
        try:
            st = self.status()
        except Exception:
            return False
        return any(m == model or m.split(":")[0] == base for m in st["loaded"])

    def load(self):
        """Warm the model into VRAM. Returns the raw API response.

        Asks the card manager (cards.py) for room on card 2 first when the APP turned it on:
        raises cards.CardBusy naming what is working there, instead of a failed load."""
        if self.is_cloud():
            return {"cloud": self.provider}      # nothing to warm on the rig
        try:
            import cards as _cards
            if _cards.enabled() and not self.is_loaded():
                _cards.preflight("writer", card=1)
        except ImportError:
            pass
        return self._post("/api/generate", {
            "model": self.model, "prompt": "warm", "stream": False,
            # the CARDS panel's "unload idle models after" setting (15m when it's off)
            "keep_alive": _keep_alive(), "options": {"num_predict": 1},
        })

    def unload(self, model=None):
        """Release the model from VRAM (keep_alive 0). Returns the raw response."""
        if model is None and self.is_cloud():
            return {"cloud": self.provider}      # a cloud writer holds nothing on the rig
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

    def is_cloud(self):
        return PROVIDERS[self.provider]["api"] != "ollama"

    def _send_chat(self, payload):
        """Ollama's /api/chat, or the same request translated for a cloud provider. The local
        FALLBACK model always goes to the rig, so a cloud outage still gets a script written."""
        model = payload.get("model")
        if not self.is_cloud() or model == self.fallback:
            return self._post("/api/chat", payload)
        api = PROVIDERS[self.provider]["api"]
        key = writer_key(self.provider)
        if not key and self.provider != "custom":
            raise WriterSetupError("no API key for %s - add one in the machine room (WRITER)"
                                % PROVIDERS[self.provider]["label"])
        body = _to_cloud(api, model, payload["messages"],
                         (payload.get("options") or {}).get("temperature", 0.7),
                         payload.get("format") or {})
        url = self.base_url + ("/v1/messages" if api == "anthropic" else "/chat/completions")
        return _from_cloud(api, _http_json(url, body, _auth_headers(self.provider, key),
                                           timeout=self.timeout))

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
                resp = self._send_chat(payload)
            except DirectorError as exc:
                if isinstance(exc, WriterSetupError):
                    raise
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
        prompt = re.sub(r"[ \t]{2,}", " ", prompt)        # spaces only: keep the label lines
        prompt = re.sub(r"[ \t]+([.,;:])", r"\1", prompt)
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

    # -- SCENE MODE: a script scene -> timed multi-shot H3 renders ----------
    def _scene_plan_ask(self, lines, cast, style, refs_by_char, location, voices_by_char,
                        characters, n_voices, max_secs=SCENE_MAX_SECS, model=None, problems=None,
                        directions="", location_desc="", looks_by_char=None, present=None,
                        beats=None):
        """Ask the local model for ONE render's shot plan as JSON (never the sections).

        `beats` = THIS render's action beats (defect 3: the model is told which beats belong to it
        and that no other render covers them, so an exit or a wake/arrival is never staged twice).
        """
        looks_by_char = looks_by_char or {}
        line_block = "\n".join(
            "  %d. %s: %s" % (i + 1, _scene_line_speaker(l) or "?", _scene_line_text(l))
            for i, l in enumerate(lines))
        cast_block = "\n".join(
            "  %s: %s" % (c, (looks_by_char.get(c) or cast.get(c) or "(appearance not supplied)"))
            for c in characters)
        n_pics = sum(1 for c in characters if refs_by_char.get(c)) + (1 if location else 0)
        voice_block = "\n".join("  %s -> has a voice-timbre reference" % c
                                for c in characters if voices_by_char.get(c))
        present_block = ("\nEVERY character physically present in the scene MUST appear in "
                         "`characters` and in the action: %s.\n" % ", ".join(present)) if present else ""
        dir_block = ("\nTHE SCENE'S STAGE DIRECTIONS (what actually happens — respect them):\n%s\n"
                     % directions) if directions else ""
        loc_block = ""
        if location:
            loc_block = ("\nLOCATION (the scene's setting — every shot happens here; write action "
                         "that matches it, never invent a different place): %s%s\n"
                         % (os.path.basename(str(location)),
                            (": " + location_desc) if location_desc else ""))
        beat_block = ""
        if beats:
            beat_block = ("\nTHIS RENDER'S ACTION BEATS (the stage directions THIS render is "
                          "responsible for, in order — cover exactly these and NOTHING more; no "
                          "other render repeats them; do not stage a character's final exit or a "
                          "wake/arrival that belongs to the next scene):\n"
                          + "\n".join("  %d. %s" % (i + 1, b.get("text"))
                                      for i, b in enumerate(beats)) + "\n")
        user = (
            "Plan ONE H3 reference-to-video scene render: a single %.2f-second timeline holding "
            "2-5 TIMED shots.\n\n"
            "THE SCENE'S LINES (perform every one, in this order, verbatim in `text`):\n%s\n"
            "%s%s%s\n"
            "CHARACTERS IN THIS RENDER (never more than 4): %s\n%s%s\n"
            "VOICE REFERENCES:\n%s\n\n"
            "STYLE (fixed; write action that suits it, do not rewrite it):\n%s\n\n"
            "RULES:\n"
            "- shots: 2 to 5. Give each a DIFFERENT framing — the shot TYPE (wide shot, medium "
            "shot, medium close-up, close-up, over-the-shoulder, low angle, profile two-shot, "
            "reaction) must CHANGE between consecutive shots.\n"
            "- SPEAKER CHANGES: when the speaker changes, CUT to a new shot/angle for the new "
            "speaker (shot/reverse-shot). Two different speakers may share ONE shot only when "
            "BOTH of their lines are under 2 seconds.\n"
            "- ACTION TIME: any shot that carries physical action needs at least 2.5 s on screen; "
            "only a pure reaction cut may be shorter. Do not crowd the render's action into "
            "1.5 s cuts.\n"
            "- start: the cut time in seconds for this shot (Shot 1 is 0.0; later shots strictly "
            "increase, every cut is before %.2f s and every shot is at least 1.5 s long). Cut "
            "times are re-derived from the line lengths, so do not chase them — just order the "
            "shots.\n"
            "- camera: a move in the spec vocabulary with amplitude and speed, e.g. 'pushes in "
            "with small amplitude at slow speed', 'pulls out with small amplitude at slow speed', "
            "or 'holds a static shot'.\n"
            "- framing: a NOUN PHRASE naming what we see, e.g. \"Avery's brass compass in his "
            "palm\", \"the standing stone from below\", \"Robin's face\". NOT a sentence and NOT a "
            "verb ('... compass spins wildly' is WRONG); do not start it with 'of' or 'behind'.\n"
            "- action: what the camera sees — composition, subject, environment, physical action. "
            "1-3 sentences, kept SEPARATE from `framing`. Say WHERE we are (the location above), "
            "not a different place. Do not name the reference labels here; you may use the "
            "character names in the plan, the tool replaces them with labels.\n"
            "- lines: the spoken lines in this shot, each {speaker, delivery, text}. `text` is "
            "the EXACT words from the script. `delivery` is ONLY this line's emotion, manner and "
            "pace (e.g. 'says with motherly authority', 'replies lightly, a little evasive') — "
            "never who the speaker is, never their accent or voice quality (that is added "
            "automatically). It is written OUTSIDE the <d> tag.\n"
            "- characters: every character who appears, in the order they should be numbered.\n"
            "- soundscape: ambience and physical sounds. music: a short score line — "
            "instrumentation + tempo + dynamics (e.g. 'a sparse solo fiddle and plucked guitar at "
            "a slow walking tempo, quiet under the dialogue'), never mood words, never 'N/A'.\n"
            "- summary: one sentence describing the render.\n"
            "Return {characters, summary, shots:[{type,start,camera,framing,action,"
            "lines:[{speaker,delivery,text}]}], soundscape, music}.\n"
            % (float(max_secs), line_block, dir_block, loc_block, beat_block, ", ".join(characters),
               (cast_block + ("\n  (%d reference images available)" % n_pics if n_pics else "")),
               present_block, voice_block or "  (none)", style, float(max_secs))
        )
        if problems:
            user += ("\n\nYour previous plan was REJECTED by the validator:\n- "
                     + "\n- ".join(problems) + "\nReturn a corrected plan.")
        return self._chat(SYSTEM_SCENE, user, SCENE_PLAN_SCHEMA, temperature=0.7, model=model)

    def scene_plan(self, script_lines, cast, style, refs_by_char, location, voices_by_char,
                   max_secs=SCENE_MAX_SECS, model=None, directions="", location_desc="",
                   looks_by_char=None, voice_meta=None, present=None, music="", allow_na_music=False,
                   beats=None, target_secs=None, location_name=None):
        """One scene's lines -> renders, each a timed multi-shot H3 prompt.

        The SPLIT is deterministic (split_scene_lines) and budgets the LINES **and the ACTION
        BEATS** (`beats`, from scene_action_beats): a ~30 s scene does not get crammed into one
        15 s render, no beat appears in two renders, and a wake/arrival after a transition is left
        out ('carried' to the next scene). Every character PHYSICALLY PRESENT in the stage
        directions rides along in each render (`present`).

        For each render the model supplies ONLY the shot plan as JSON; the six Ref2VA sections are
        assembled in CODE (scene_prompt_from_plan). The prompt is checked with scene_prompt_problems
        plus scene_speaker_change_problems (a speaker change wants its own angle), and if it fails
        the model is re-asked ONCE with the exact problems. Returns
        [{n, secs, characters, lines:[idx], beats, prompt, problems:[...], plan}, ...].
        """
        renders = split_scene_lines(script_lines, max_secs=max_secs, present=present,
                                    beats=beats, target_secs=target_secs)
        out = []
        for r in renders:
            r_lines = [script_lines[i] for i in r["lines"]]
            # only SPEAKERS carry a voice ref; a present-but-silent character still appears.
            n_voices = sum(1 for c in r.get("speakers", r["characters"]) if voices_by_char.get(c))
            plan, prompt, problems, best = {}, "", [], None
            for _attempt in range(2):
                cand = self._scene_plan_ask(r_lines, cast, style, refs_by_char, location,
                                            voices_by_char, r["characters"], n_voices,
                                            max_secs=max_secs, model=model,
                                            problems=(problems or None), directions=directions,
                                            location_desc=location_desc, looks_by_char=looks_by_char,
                                            present=r["characters"], beats=r.get("beats"))
                cand_prompt = scene_prompt_from_plan(
                    cand, cast=cast, refs_by_char=refs_by_char,
                    location=location, voices_by_char=voices_by_char,
                    secs=r["secs"], style=style,
                    looks_by_char=looks_by_char, voice_meta=voice_meta,
                    location_name=location_name, location_desc=location_desc,
                    present=r["characters"], music=music, allow_na_music=allow_na_music)
                p_shots = (cand.get("shots") or []) if isinstance(cand, dict) else []
                cand_problems = (scene_prompt_problems(cand_prompt, r["line_texts"], n_voices,
                                                       subjects=r["characters"])
                                 + scene_speaker_change_problems(p_shots))
                # defect 6: KEEP WHICHEVER VERSION HAS FEWER PROBLEMS — the re-ask can come back
                # worse, and a non-empty problems list must never be silently shipped.
                if best is None or len(cand_problems) < len(best[2]):
                    best = (cand, cand_prompt, cand_problems)
                if not cand_problems:
                    break
                problems = cand_problems
            plan, prompt, problems = best
            p_shots = (plan.get("shots") or []) if isinstance(plan, dict) else []
            secs = max(r["secs"], scene_timeline_seconds(p_shots))
            out.append({"n": r["n"], "secs": round(min(secs, SCENE_MAX_SECS), 2),
                        "characters": r["characters"], "speakers": r.get("speakers", []),
                        "lines": r["lines"], "beats": r.get("beats", []),
                        "prompt": prompt, "problems": problems, "plan": plan})
        return out

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
                rules = ("This is an H3 clip-continuation prompt in FIRST-FRAME mode (MiniMax "
                         "I2VA), NOT the six Ref2VA sections. The first line is exactly "
                         "'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.', then a blank line, then three "
                         "fields: integrated_multimodal_description: '[Shot 1] <style>, what "
                         "<Picture 1> shows, then what happens next' with the camera as type + "
                         "amplitude + speed; overall_soundscape: 1-4 sentences; "
                         "non_diegetic_music: 1-3 sentences or N/A. Do NOT write "
                         "'<Picture N> is the character'.")
            elif video_mode == "t2v":
                rules = ("This is an H3 text-to-video prompt (MiniMax T2VA): no references "
                         "and no instruction line. Three fields: "
                         "integrated_multimodal_description: '[Shot 1] <style>, <composition>, "
                         "<action>' with the camera as type + amplitude + speed; "
                         "overall_soundscape: 1-4 sentences; non_diegetic_music: 1-3 sentences "
                         "or N/A.")
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
            # spaces only: `\s` here ate the newlines normalise_ref2va_labels had just put
            # before each section label, re-inlining every one of them
            out = re.sub(r"[ \t]{2,}", " ", out)
            out = re.sub(r"[ \t]+([.,;:])", r"\1", out)
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