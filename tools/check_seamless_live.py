#!/usr/bin/env python3
"""check_seamless_live.py — a LIVE check that the Director actually writes a one-take
seamless prompt. The GPU-free suite can be fully green over a dialect the model ignores, so
this one calls the real Ollama on forge and reports what came back.

    /opt/homebrew/bin/python3 tools/check_seamless_live.py [character] [idea]

Exits non-zero if the prompt is unusable (missing sections, dialogue, a static camera, no
crowd, or a detailed_description outside the documented band).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "audio"))
os.environ.setdefault("FORGE_LAB_NODIRECTOR", "1")

import director as d                                             # noqa: E402

SECTIONS = ("subject_definitions", "summary", "retention_analysis",
            "detailed_description", "overall_soundscape", "non_diegetic_music")


def words_between(text, a, b):
    m = re.search(r"%s\s*:(.*?)(?=%s\s*:)" % (a, b), text, re.S | re.I)
    return len((m.group(1) if m else "").split())


def main(argv):
    char = argv[0] if argv else "gustav"
    idea = argv[1] if len(argv) > 1 else ""
    dr = d.Director()
    print("director: %s  model=%s" % (dr.host, dr.model))
    dr.load()
    try:
        obj = dr.seamless(char, idea=idea)
    finally:
        try:
            dr.unload()
            print("unloaded")
        except Exception:
            pass

    p = obj.get("prompt") or ""
    print("\n=== title: %s" % obj.get("title"))
    print("=== logline: %s" % obj.get("logline"))
    print("=== card: %r" % obj.get("card"))
    print("=== prompt: %d chars, %d words" % (len(p), len(p.split())))

    fails = []
    print("\nsections:")
    for s in SECTIONS:
        ok = re.search(r"(?<![A-Za-z_])%s\s*:" % s, p, re.I) is not None
        print("   %-22s %s" % (s, "present" if ok else "*** MISSING ***"))
        if not ok:
            fails.append("missing section %s" % s)
    dd = d.seamless_dd_words(p)
    print("\ndetailed_description: %d words (documented band 350-500)" % dd)
    if not d.seamless_prompt_ok(p):
        fails.append("seamless_prompt_ok() says NO (sections/description)")
    if dd < 200:
        fails.append("detailed_description too short (%d words)" % dd)

    has_d = bool(re.search(r"<d>|</d>|<d\s", p))
    has_off = bool(re.search(r"\(S\d\)", p))
    print("dialogue tags: %s   speaker ids: %s" % (has_d, has_off))
    if has_d or has_off:
        fails.append("prompt contains dialogue (the piece must be silent)")

    low = p.lower()
    checks = [("camera moves (not static)", not re.search(r"\bstatic (shot|camera)\b", low)),
              ("mentions handheld/phone", ("handheld" in low or "phone" in low)),
              ("mentions a crowd", "crowd" in low or "bystander" in low),
              ("off-centre framing asked for", "off-centre" in low or "off-center" in low),
              ("soundscape has no speech", "no human speech" in low or "no spoken" in low)]
    print()
    for name, ok in checks:
        print("   %-34s %s" % (name, "ok" if ok else "*** NO ***"))
        if not ok:
            fails.append(name)

    print("\n%s" % ("FAIL: " + "; ".join(fails) if fails else "PASS — usable one-take prompt"))
    print("\n--- prompt ---\n%s" % p[:3000])
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))