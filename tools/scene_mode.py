#!/usr/bin/env python3
"""SCENE MODE — turn ONE script scene into timed multi-shot H3 render prompts (+ shots.json).

    FORGE_LAB_NOAUTOSAVE=1 /opt/homebrew/bin/python3 tools/scene_mode.py \
        SCRIPT.md "SCENE 1" CAST.json OUTDIR [--style S] [--location PNG] [--plan PLAN.json]

It reads the scene under the script's `## SCENE N` heading (the same headings as
~/forge-lab-work/yg-101-pilot.md), resolves each speaker's turnaround sheet
(~/forge-lab-work/yg-101/sheets/<slug>.png, else assets/<slug>.png) and Dialogue voice
(`dialogue.voice_wav(load_voice(slug), "neutral")`), plans the render(s) with
`director.Director.scene_plan` (the local model supplies the shot plan; director.py assembles the
six Ref2VA sections in code), and writes:

    OUTDIR/prompts/shot<n>.txt   one prompt per render
    OUTDIR/shots.json            [{"n","secs","refs","guide":null,"lora","voice_refs"}, ...]

That is exactly the shape tools/dialogue_scene_h3.py consumes. This tool NEVER submits a render:
it prints a table and the problems list and exits non-zero if any render has problems.

`--plan PLAN.json` skips the model entirely and assembles the prompts from a plan you supply
(a dict, or a list of dicts — one per render). That is what tools/scene_matrix.py uses, so it can
prove the tool GPU-free and model-free.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import director  # noqa: E402
from director import scene_prompt_from_plan, scene_prompt_problems, scene_plan_order  # noqa: E402

DEFAULT_BASE = os.path.expanduser("~/forge-lab-work/yg-101")
DEFAULT_LORA = "character_lora_v1.safetensors"
DEFAULT_LOOKS = os.path.expanduser("~/forge-lab-work/the character-looks.json")
DEFAULT_LOCATIONS = os.path.expanduser("~/forge-lab-work/the character-locations.json")

# Dialogue lines look like `> **GUIDE** — text`; everything else under the heading is prose.
_DIALOGUE_LINE_RE = re.compile(r"^\s*>?\s*\*\*\S.{0,40}?\*\*\s*(?:—|–|-{2}|-|:)\s*")
# A markdown horizontal rule ('---', '***', '___') that closes a scene is NOT a stage direction.
_MD_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def extract_scene(md_text, which):
    """The `## SCENE <which>` block of a script, up to the next scene heading.

    Returns (heading_line, block_text). Raises ValueError if the scene is not found.
    """
    want = str(which).strip()
    m = re.search(r"SCENE\s+(\S+)", want, re.I)
    if m:
        want = m.group(1)
    lines = md_text.splitlines()
    out, heading, on = [], None, False
    for ln in lines:
        hm = re.match(r"^##\s*SCENE\s+(\S+)\b(.*)$", ln)
        if hm:
            if on:
                break
            if hm.group(1) == want:
                on, heading = True, ln
            continue
        if on:
            out.append(ln)
    if heading is None:
        raise ValueError("no '## SCENE %s' heading in the script" % want)
    return heading, "\n".join(out)


# The ARTWORK slug does not always match the spoken-name slug: 'Citi Bike Guy' voices under
# voices/citi-bike-guy/ but its turnaround sheet is sheets/citibike.png. Map those explicitly so
# the sheet resolves without editing the shared cast json.
_SLUG_ALIASES = {"CITI BIKE GUY": "citibike"}


def load_cast(path):
    """CAST.json -> ({NAME: bio}, {NAME: slug}, {NAME: voice-meta}) for every bound character.

    `bio` is the voice/notes brief (fed to nothing that writes <Subject> lines — voice notes must
    never land in subject_definitions); `voice-meta` is the raw voice dict used to build the voice
    intro from gender/age/accent/notes.
    """
    with open(path) as fh:
        data = json.load(fh)
    voices = data.get("voices") if isinstance(data, dict) else data
    cast, slug_of, voice_meta = {}, {}, {}
    try:
        from dialogue import normalise_speaker as _ns
    except Exception:
        _ns = lambda s: str(s).strip().upper()   # noqa: E731
    for v in voices or []:
        name = (v.get("name") or "").strip()
        bio = ", ".join(x for x in (name, (v.get("notes") or "").strip()) if x)
        for ch in (v.get("characters") or [name]):
            ch = _ns((ch or "").strip().upper())   # 'AVERY (V.O.)' and 'AVERY' are one character
            if ch:
                cast[ch] = bio
                slug_of[ch] = ((v.get("slug") or "").strip() or _SLUG_ALIASES.get(ch)
                               or _slug(ch))
                voice_meta[ch] = v
    return cast, slug_of, voice_meta


def load_looks(path, slug_of, extra_names=()):
    """LOOKS.json (slug -> appearance) -> {NAME: appearance} for every name we can map to a slug.

    Appearance ONLY — never the voice notes. Falls back to `_slug(name)` for a speaker missing a
    cast entry.
    """
    if not path or not os.path.isfile(path):
        return {}
    with open(path) as fh:
        looks = json.load(fh)
    out = {}
    names = set(slug_of) | {str(n).strip().upper() for n in extra_names if str(n).strip()}
    for name in names:
        slug = slug_of.get(name) or _slug(name)
        txt = (looks.get(slug) or "").strip()
        if txt:
            out[name] = txt
    return out


def load_locations(path):
    """LOCATIONS.json -> {basename: {"name": str, "desc": str}} (also keyed w/o .png).

    A value may be a bare description string (legacy) or an object {"name": ..., "desc": ...}
    (defect 5: a human PLACE name so the location <Subject N> line never says 'the ferryfarm').
    """
    if not path or not os.path.isfile(path):
        return {}
    with open(path) as fh:
        data = json.load(fh)
    out = {}
    for k, v in (data or {}).items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, dict):
            rec = {"name": str(v.get("name") or "").strip(),
                   "desc": str(v.get("desc") or v.get("description") or "").strip()}
        else:
            rec = {"name": "", "desc": str(v)}
        out[str(k)] = rec
        out.setdefault(os.path.splitext(str(k))[0], rec)
    return out


def _loc_rec(location, locations):
    if not location or not locations:
        return {}
    b = os.path.basename(str(location))
    rec = locations.get(b) or locations.get(os.path.splitext(b)[0]) or {}
    return rec if isinstance(rec, dict) else {"name": "", "desc": str(rec)}


def location_description(location, locations, override=""):
    """The description for `location`: --location-desc wins, else the LOCATIONS mapping."""
    if override:
        return override.strip()
    return (_loc_rec(location, locations).get("desc") or "").strip()


def location_name(location, locations, override=""):
    """A human PLACE name for `location`: explicit `name`, else derived, else ''."""
    if override:
        return override.strip()
    n = (_loc_rec(location, locations).get("name") or "").strip()
    if n:
        return n
    return director.derive_location_name((_loc_rec(location, locations).get("desc") or ""))


def stage_directions(block):
    """The scene's PROSE (stage directions) — the block with its `> **SPEAKER** — ...` lines cut."""
    out = []
    for ln in (block or "").splitlines():
        if _DIALOGUE_LINE_RE.match(ln) or _MD_RULE_RE.match(ln):
            continue
        if ln.strip():
            out.append(ln.strip())
    return "\n".join(out)


def scene_beats(block, n_lines):
    """The scene's STAGE DIRECTIONS as ordered, timed ACTION BEATS (director.scene_action_beats).

    A beat is a prose paragraph (split once at a 'turn' sentence), tagged with `gap` = how many
    dialogue lines precede it. Beats give the splitter real action time so a ~30 s scene is not
    crammed into one 15 s render (defect 1); each beat lands in exactly ONE render (defect 3); a
    wake/arrival after a transition is 'carried' to the next scene (defect 4).
    """
    paras, buf, seen = [], [], 0

    def flush():
        nonlocal buf
        if buf:
            paras.append({"text": " ".join(buf), "gap": seen})
            buf = []

    for ln in (block or "").splitlines():
        if _DIALOGUE_LINE_RE.match(ln):
            flush()
            seen += 1
        elif _MD_RULE_RE.match(ln) or not ln.strip():
            flush()
        else:
            buf.append(ln.strip())
    flush()
    return director.scene_action_beats(paras)


def scene_target_secs(heading):
    """The scene heading's '~Ns' target length when present (e.g. '... — ~30s' -> 30.0)."""
    m = re.search(r"~\s*(\d+(?:\.\d+)?)\s*s\b", heading or "")
    return float(m.group(1)) if m else None


def characters_present(directions, cast, speakers):
    """The characters PHYSICALLY PRESENT in the scene (from the stage directions), in first-mention
    order, with the speakers appended (a speaker is by definition present)."""
    text = directions or ""
    hits = []
    for c in (list(cast) + [s for s in speakers if s not in cast]):
        tok = c.split()[0]
        m = re.search(r"\b%s\b" % re.escape(tok), text, re.I)
        if m:
            hits.append((m.start(), c))
    hits.sort()
    present = []
    for _pos, c in hits:
        if c not in present:
            present.append(c)
    for s in speakers:
        if s and s not in present:
            present.append(s)
    return present


def _slug(name):
    try:
        from dialogue import slugify
        return slugify(name)
    except Exception:
        return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "untitled"


def slug_variants(name, slug=""):
    """Slug spellings to try for a character, in priority order (defect 3).

    The sheet/voice slug does NOT always match the speaker's name: 'CITI BIKE GUY' voices under
    voices/citi-bike-guy/ but its sheet is sheets/citibike.png. Try the given slug, then the name
    with spaces -> '-', -> '_', removed, and the fully-squashed alphanumeric form, so a voice or an
    asset resolves without hand-editing the shared cast json.
    """
    cands = []

    def add(s):
        s = (s or "").strip()
        if s and s not in cands:
            cands.append(s)

    add(slug)
    add(_slug(name))
    low = str(name or "").strip().lower()
    add(re.sub(r"\s+", "-", low))
    add(re.sub(r"\s+", "_", low))
    add(re.sub(r"\s+", "", low))
    add(re.sub(r"[^a-z0-9]+", "", low))
    return cands


def resolve_asset(base, slug, prefer=("sheets", "assets"), name=None):
    """<base>/sheets/<slug>.png else <base>/assets/<slug>.png, else None (absolute path).

    Tries every `slug_variants` spelling of `name` (or `slug`), so a sheet named for the character
    ('citibike.png') resolves without hand-editing the cast json.
    """
    for sub in prefer:
        for s in slug_variants(name or slug, slug):
            p = os.path.join(base, sub, "%s.png" % s)
            if os.path.isfile(p):
                return os.path.abspath(p)
    return None


def resolve_voice(slug, name=None):
    """The Dialogue voice wav for a slug, or None.

    Tries every `slug_variants` spelling: 'CITI BIKE GUY' resolves voices/citi-bike-guy/ (via the
    hyphenated spelling) even though its alias slug is 'citibike' (defect 3).
    """
    try:
        from dialogue import load_voice, voice_wav
    except Exception:
        return None
    for cand in slug_variants(name or slug, slug):
        try:
            v = load_voice(cand)
        except Exception:
            v = None
        if not v:
            continue
        w = voice_wav(v, "neutral")
        if w:
            return w
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("scene")
    ap.add_argument("cast")
    ap.add_argument("outdir")
    ap.add_argument("--style", default=director.DEFAULT_SCENE_STYLE)
    ap.add_argument("--location", default="")
    ap.add_argument("--location-desc", default="",
                    help="the location's description (else read from --locations)")
    ap.add_argument("--locations", default=DEFAULT_LOCATIONS,
                    help="JSON mapping png basename -> description")
    ap.add_argument("--looks", default=DEFAULT_LOOKS,
                    help="JSON mapping character slug -> visual appearance")
    ap.add_argument("--music", default="", help="force the non_diegetic_music line")
    ap.add_argument("--no-music", action="store_true", help="allow non_diegetic_music to be N/A")
    ap.add_argument("--lora", default=DEFAULT_LORA)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--plan", default="")
    ap.add_argument("--model", default="")
    a = ap.parse_args()

    from dialogue import parse_script
    md = open(a.script).read()
    heading, block = extract_scene(md, a.scene)
    lines = parse_script(block)
    if not lines:
        sys.exit("no dialogue lines found in %s" % a.scene)

    cast, slug_of, voice_meta = load_cast(a.cast)
    speakers = []
    refs_by_char, voices_by_char = {}, {}
    for spk in {re.sub(r"\s*\(.*?\)\s*", "", (l["speaker"] or "")).strip().upper()
                for l in lines}:
        slug = slug_of.get(spk) or _slug(spk)
        sheet = resolve_asset(a.base, slug, name=spk)
        if sheet:
            refs_by_char[spk] = sheet
        wav = resolve_voice(slug, name=spk)
        if wav:
            voices_by_char[spk] = wav
    for l in lines:
        s = re.sub(r"\s*\(.*?\)\s*", "", (l["speaker"] or "")).strip().upper()
        if s and s not in speakers:
            speakers.append(s)

    directions = stage_directions(block)
    present = characters_present(directions, cast, speakers)
    looks_by_char = load_looks(a.looks, slug_of, extra_names=speakers)
    locations = load_locations(a.locations)
    beats = scene_beats(block, len(lines))
    carried = [b for b in beats if b.get("kind") == "carried"]
    target = scene_target_secs(heading)

    loc = os.path.abspath(os.path.expanduser(a.location)) if a.location else None
    loc_desc = location_description(loc, locations, a.location_desc)
    loc_name = location_name(loc, locations)
    if not loc_name and a.location_desc:
        loc_name = director.derive_location_name(a.location_desc)
    scene_num = re.search(r"SCENE\s+(\S+)", a.scene, re.I)
    scene_num = (scene_num.group(1) if scene_num else a.scene)
    forced_music = a.music or ("N/A" if a.no_music else "")

    print("directions  : %s" % (directions.replace("\n", " ")[:150] or "-"))
    print("present     : %s" % (", ".join(present) or "-"))
    print("location    : %s  (%s)" % (os.path.basename(loc) if loc else "-",
                                      loc_desc[:70] or "no description"))
    print("beats       : %d action beat(s)%s"
          % (len([b for b in beats if b.get("kind") != "carried"]),
             "  target ~%gs" % target if target else ""))
    for b in beats:
        if b.get("kind") != "carried":
            print("   beat %d  %.1fs  gap=%d  [%s]  %s"
                  % (b["i"] + 1, b["seconds"], b["gap"], b["kind"], b["text"][:78]))
    for b in carried:
        print("   CARRIED to next scene (dropped): %s" % b["text"][:78])

    plans = None
    if a.plan:
        plans = json.load(open(a.plan))
        if isinstance(plans, dict):
            plans = [plans]
        renders = director.split_scene_lines(lines, present=present, beats=beats,
                                             target_secs=target)
    else:
        d = director.Director()
        renders = d.scene_plan(lines, cast, a.style, refs_by_char, loc, voices_by_char,
                               model=(a.model or None), directions=directions,
                               location_desc=loc_desc, looks_by_char=looks_by_char,
                               voice_meta=voice_meta, present=present,
                               music=forced_music, allow_na_music=a.no_music,
                               beats=beats, target_secs=target, location_name=loc_name)

    os.makedirs(os.path.join(a.outdir, "prompts"), exist_ok=True)
    shots, all_problems = [], []
    print("%-4s %6s %-26s %-5s %s" % ("n", "secs", "characters", "lines", "problems"))
    for i, r in enumerate(renders):
        name = "%s%s" % (scene_num, chr(ord("A") + i))
        plan = (plans[i] if plans and i < len(plans) else (r.get("plan") or {}))
        subjects = list(r.get("characters") or [])
        if not subjects:
            subjects, _sp = scene_plan_order(plan)
        speaking = list(r.get("speakers") or [])
        if not speaking:
            _subj, speaking = scene_plan_order(plan)
        n_voices = sum(1 for c in speaking if voices_by_char.get(c))
        if plans:
            prompt = scene_prompt_from_plan(plan, cast=cast, refs_by_char=refs_by_char,
                                            location=loc, voices_by_char=voices_by_char,
                                            secs=r["secs"], style=a.style,
                                            looks_by_char=looks_by_char, voice_meta=voice_meta,
                                            location_name=loc_name, location_desc=loc_desc,
                                            present=subjects,
                                            music=forced_music, allow_na_music=a.no_music)
            problems = scene_prompt_problems(prompt, r["line_texts"], n_voices, subjects=subjects)
        else:
            prompt, problems = r["prompt"], r["problems"]
        with open(os.path.join(a.outdir, "prompts", "shot%s.txt" % name), "w") as fh:
            fh.write(prompt)
        refs = [refs_by_char[c] for c in subjects if refs_by_char.get(c)]
        if loc:
            refs.append(loc)
        voice_refs = [voices_by_char[c] for c in speaking if voices_by_char.get(c)]
        shots.append({"n": name, "secs": r["secs"], "refs": refs, "guide": None,
                      "lora": a.lora, "voice_refs": voice_refs})
        all_problems += problems
        print("%-4s %6.2f %-26s %-5d %s" % (name, r["secs"], ",".join(subjects) or "-",
                                            len(r["lines"]), "OK" if not problems else "FAIL"))

    with open(os.path.join(a.outdir, "shots.json"), "w") as fh:
        json.dump(shots, fh, indent=1)

    print("\nwrote %s/prompts/shot<n>.txt and %s/shots.json"
          % (a.outdir, a.outdir))
    for spk in sorted(set(refs_by_char) | set(voices_by_char)):
        print("  %-10s sheet=%s voice=%s" % (spk, refs_by_char.get(spk, "-"),
                                             voices_by_char.get(spk, "-")))
    if all_problems:
        print("\nPROBLEMS (%d):" % len(all_problems))
        for p in all_problems:
            print("  - %s" % p)
        sys.exit(1)
    print("\nall renders clean")


if __name__ == "__main__":
    main()
