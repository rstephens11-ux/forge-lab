#!/usr/bin/env python3
"""LIVE check of SCENE MODE's LLM path against the local model on forge — TEXT ONLY, no render.

    /opt/homebrew/bin/python3 tools/check_scene_live.py \
        [--script ~/forge-lab-work/yg-101-pilot.md] [--scene "SCENE 2"] \
        [--cast ~/forge-lab-work/cast.json] \
        [--location ~/forge-lab-work/yg-101/assets/ridge.png] [--timeout 900]

It reads the scene, resolves the sheets/voices, and calls director.Director.scene_plan (the local
model plans the shots; director.py assembles the six Ref2VA sections in code and validates them
with scene_prompt_problems). It prints the model, each render's prompt and its problems.

It NEVER submits a render to ComfyUI (8188/8189) and NEVER touches a running job: the ONLY
network call is Ollama's /api/chat on forge:11434. If the model cannot load (a second ComfyUI on
GPU1 may hold the VRAM), it prints the exact error and exits non-zero.
"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import director  # noqa: E402
from dialogue import parse_script  # noqa: E402
import scene_mode as SM  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=os.path.expanduser("~/forge-lab-work/yg-101-pilot.md"))
    ap.add_argument("--scene", default="SCENE 2")
    ap.add_argument("--cast", default=os.path.expanduser("~/forge-lab-work/cast.json"))
    ap.add_argument("--base", default=SM.DEFAULT_BASE)
    ap.add_argument("--location", default=os.path.expanduser("~/forge-lab-work/yg-101/assets/ridge.png"))
    ap.add_argument("--location-desc", default="")
    ap.add_argument("--locations", default=SM.DEFAULT_LOCATIONS)
    ap.add_argument("--looks", default=SM.DEFAULT_LOOKS)
    ap.add_argument("--style", default=director.DEFAULT_SCENE_STYLE)
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    d = director.Director(timeout=a.timeout)
    print("Ollama host : %s" % d.host)
    print("primary     : %s" % d.model)
    print("fallback    : %s" % d.fallback)
    st = d.status()
    print("reachable   : %s  resident=%s  %s"
          % (st.get("reachable"), st.get("loaded"), st.get("vram_hint") or ""))
    if not st.get("reachable"):
        sys.exit("forge Ollama at %s is UNREACHABLE — cannot run the live check" % d.host)

    md = open(a.script).read()
    heading, block = SM.extract_scene(md, a.scene)
    lines = parse_script(block)
    beats = SM.scene_beats(block, len(lines))
    target = SM.scene_target_secs(heading)
    carried = [b for b in beats if b.get("kind") == "carried"]
    print("\nscene       : %s  (%d line(s), %d action beat(s), target %s)"
          % (a.scene, len(lines), len([b for b in beats if b.get("kind") != "carried"]),
             ("~%gs" % target) if target else "none"))
    for i, l in enumerate(lines):
        print("  %d. %-8s %s" % (i + 1, l["speaker"], l["text"]))

    cast, slug_of, voice_meta = SM.load_cast(a.cast)
    speakers = []
    refs_by_char, voices_by_char = {}, {}
    for spk in {re.sub(r"\s*\(.*?\)\s*", "", (l["speaker"] or "")).strip().upper() for l in lines}:
        slug = slug_of.get(spk) or SM._slug(spk)
        sheet = SM.resolve_asset(a.base, slug, name=spk)
        if sheet:
            refs_by_char[spk] = sheet
        wav = SM.resolve_voice(slug, name=spk)
        if wav:
            voices_by_char[spk] = wav
    for l in lines:
        s = re.sub(r"\s*\(.*?\)\s*", "", (l["speaker"] or "")).strip().upper()
        if s and s not in speakers:
            speakers.append(s)
    directions = SM.stage_directions(block)
    present = SM.characters_present(directions, cast, speakers)
    looks_by_char = SM.load_looks(a.looks, slug_of, extra_names=speakers)
    locations = SM.load_locations(a.locations)
    loc = os.path.abspath(os.path.expanduser(a.location)) if a.location else None
    loc_desc = SM.location_description(loc, locations, a.location_desc)
    loc_name = SM.location_name(loc, locations)
    print("\nrefs        : %s" % {k: os.path.basename(v) for k, v in refs_by_char.items()})
    print("voices      : %s" % {k: v for k, v in voices_by_char.items()})
    print("location    : %s  (%s)  name=%r" % (loc, loc_desc[:70] or "no description", loc_name))
    print("present     : %s" % (", ".join(present) or "-"))
    print("looks       : %s" % {k: (v[:48] + "...") for k, v in looks_by_char.items()})
    print("directions  : %s" % directions.replace("\n", " ")[:220])
    for b in beats:
        if b.get("kind") == "carried":
            print("beat (carried to next scene, dropped): %s" % b["text"][:150])
        else:
            print("beat %d  %.1fs  [%s]  %s" % (b["i"] + 1, b["seconds"], b["kind"],
                                                b["text"][:150]))

    t0 = time.time()
    try:
        renders = d.scene_plan(lines, cast, a.style, refs_by_char, loc, voices_by_char,
                               directions=directions, location_desc=loc_desc,
                               looks_by_char=looks_by_char, voice_meta=voice_meta, present=present,
                               beats=beats, target_secs=target, location_name=loc_name)
    except Exception as exc:
        print("\nLLM PATH FAILED after %.0f s: %s: %s"
              % (time.time() - t0, type(exc).__name__, exc))
        print("(this is the model failing to load or to answer — NOT a render; nothing was submitted)")
        sys.exit(2)

    bad = 0
    for r in renders:
        print("\n" + "=" * 78)
        print("RENDER %s  %.2f s  characters=%s  lines=%s"
              % (r["n"], r["secs"], ",".join(r["characters"]), r["lines"]))
        print("plan subjects=%s  shots=%d"
              % ((r["plan"] or {}).get("characters"), len((r["plan"] or {}).get("shots") or [])))
        print("problems    : %s" % ("NONE" if not r["problems"] else ""))
        for p in r["problems"]:
            print("  - %s" % p)
        bad += len(r["problems"])
        print("-" * 78)
        print(r["prompt"])
    print("\n%.1f s wall; %d problem(s) across %d render(s)"
          % (time.time() - t0, bad, len(renders)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
