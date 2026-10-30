#!/usr/bin/env python3
"""GPU-free checks for the expanded character vocabulary (char_vocab.py) and its ADULTS-ONLY
rule: swimwear / lingerie never compose, roll or render onto a minor.
    python3 tools/vocab_matrix.py"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["FORGE_LAB_DATA"] = tempfile.mkdtemp(prefix="vocab-")
import char_vocab as cv  # noqa: E402
import forge_lab as fl  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


print("§0 a character saved with TEXT picks/bible reads back as dicts; bible.json counts")
import json as _json  # noqa: E402
import scene_engine as _se  # noqa: E402
_cd = os.path.join(fl.CHARACTERS, "clar")
os.makedirs(_cd, exist_ok=True)
_json.dump({"name": "Clar", "picks": "{'Subject': 'a weatherman', 'Feature': 'big ears'}",
            "bible": "{'who': 'a weatherman', 'voice': 'breathy baritone'}"},
           open(os.path.join(_cd, "character.json"), "w"))
_gd = os.path.join(fl.CHARACTERS, "gust")
os.makedirs(_gd, exist_ok=True)
_json.dump({"name": "Gust", "picks": {}}, open(os.path.join(_gd, "character.json"), "w"))
_json.dump({"who": "a conductor", "voice": "dry weary baritone"},
           open(os.path.join(_gd, "bible.json"), "w"))
_m = fl.read_character("clar")
check(isinstance(_m["picks"], dict) and _m["picks"]["Feature"] == "big ears" and
      _m["bible"]["voice"] == "breathy baritone", "read_character turns text dicts back into dicts")
_sc = dict(_se.list_characters())
check(_sc["clar"]["bible"]["voice"] == "breathy baritone" and
      _sc["gust"]["bible"]["voice"] == "dry weary baritone",
      "casting sees both bibles (text-saved, and from bible.json)")
check("dry weary baritone" in str(_se.voice_seed("Gust", _sc["gust"])),
      "the audition voice is seeded from bible.json's voice")

O = dict(fl.CHAR_OPTIONS)
SWIM = [v for g, vs in cv.GROUPS["Wardrobe"] if g in cv.ADULT_ONLY_GROUPS for v in vs]
print("§1 the Swim & lingerie section")
check(len(SWIM) >= 12 and all(v in O["Wardrobe"] for v in SWIM),
      "%d swim / lingerie outfits, all in the Wardrobe list" % len(SWIM))
check(not any(v.lower().startswith("wearing") for v in O["Wardrobe"]),
      "no Wardrobe value carries its own 'wearing'")
grp = dict(cv.groups("Wardrobe", O["Wardrobe"]))
check(all(g in grp for g in cv.ADULT_ONLY_GROUPS)
      and sum((grp[g] for g in cv.ADULT_ONLY_GROUPS), []) == SWIM,
      "Swimwear and Lingerie & sleepwear are their own submenus")
check(len(SWIM) >= 40, "the section is big: %d outfits" % len(SWIM))
_untyped = [v for v in SWIM if not cv._SKIN_WEAR.search(v)]
check(not _untyped, "every one is recognised in TYPED text too, so a hand-written prompt "
      "copying it is caught (missed: %s)" % _untyped)
print("§2 who counts as a minor")
MINORS = [s for s in O["Subject"] if cv.is_minor(s)]
check("a child of about six" in MINORS and any("teenage model" in m for m in MINORS),
      "the list's non-adult subjects are recognised (%s)" % MINORS)
ADULTS = [s for s in O["Subject"] if not cv.is_minor(s)]
check(len(ADULTS) >= 130 and "a fashion model" in ADULTS and "a mail carrier" in ADULTS
      and "an elderly woman" in ADULTS and "a 1950s housewife" in ADULTS,
      "every other subject is an adult (%d)" % len(ADULTS))
for t in ("a 12-year-old girl", "a teenage boy", "a schoolgirl", "two kids", "a toddler",
          "a girl of about 9", "a 16 year old"):
    check(cv.is_minor(t), "minor: %r" % t)
for t in ("a woman of about 30", "a 35-year-old man", "an old boyfriend? no: a man",
          "a gaunt man in his nineties"):
    check(not cv.is_minor(t.replace("boyfriend", "friend")), "adult: %r" % t)
print("§3 the composer never pairs them")
bad = 0
for subj in MINORS:
    for w in SWIM:
        p = fl.compose_character_prompt({"Subject": subj, "Wardrobe": w})
        if cv.is_adult_only(p.split(".")[0]):
            bad += 1
check(bad == 0, "every minor x every swim/lingerie outfit: the outfit is replaced (%d bad of %d)"
      % (bad, len(MINORS) * len(SWIM)))
p = fl.compose_character_prompt({"Subject": "a fashion model", "Wardrobe": SWIM[0]})
check("wearing " + SWIM[0] in p, "an adult keeps the outfit")
print("§4 the rolls never pair them")
bad = 0
r = random.Random(7)
for i in range(4000):
    pk, _a = fl.roll_weirdo(r, vein=("Anything", "Modern", "Formal-European")[i % 3])
    if cv.conflict(pk["Subject"], pk["Wardrobe"]):
        bad += 1
check(bad == 0, "4000 Surprise-me rolls: never a minor in swimwear / lingerie (%d)" % bad)


class Rigged(random.Random):
    """Always rolls a MINOR subject and, the first time, a swimwear outfit — the worst case."""
    def __init__(self):
        super().__init__(1)
        self.first = True

    def choice(self, seq):
        seq = list(seq)
        if seq and seq == O["Subject"]:
            return "a child of about six"
        if seq and seq == O["Wardrobe"] and self.first:
            self.first = False
            return SWIM[0]
        return super().choice(seq)


bad = 0
for v in ("Anything", "Modern", "Formal-European"):
    for _i in range(50):
        pk, _a = fl.roll_weirdo(Rigged(), vein=v)
        if cv.conflict(pk["Subject"], pk["Wardrobe"]):
            bad += 1
check(bad == 0, "a roll rigged to draw a minor AND swimwear re-rolls the outfit (%d bad)" % bad)
print("§5 a hand-typed prompt is caught too")
check(cv.conflict("A photo of a teenage girl in a red string bikini on a beach"),
      "a typed minor + bikini is caught")
check(not cv.conflict("A photo of a woman in a red string bikini on a beach"),
      "an adult in a bikini is fine")
check(not cv.conflict("A photo of a child in a yellow raincoat"), "a child in a raincoat is fine")
print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f in FAILS:
    print("   FAILED: %s" % f)
sys.exit(1 if FAILS else 0)
