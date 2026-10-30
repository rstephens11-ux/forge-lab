#!/usr/bin/env python3
"""GPU-free assertions for the CHARACTER store in forge_lab.py.

    /opt/homebrew/bin/python3 tools/character_matrix.py

Runs against a TEMP characters dir, so it never touches real characters. No Tk, no forge,
no GPU — safe to run anywhere, any time.

The load-bearing property here is that a character's still is COPIED into its own folder.
Takes get pruned; a character that pointed into runs/ would silently lose the one asset it
cannot regenerate.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(ROOT, "forge_lab.py"))
fl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fl)

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def _fake_png(path):
    """We only ever compare BYTES here, so the content never needs to be a decodable image."""
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"FORGE-LAB-CHARACTER-TEST" * 8)
    return path


tmp = tempfile.mkdtemp(prefix="forge_lab_chars_")
fl.CHARACTERS = tmp          # ← redirect the whole store; nothing real is touched

print("\nCHARACTERS · slug")
check(fl.char_slug("Big  Ears!") == "big-ears", "punctuation + a run of spaces collapse to a slug")
check(fl.char_slug("Ms. Rose") == "ms-rose", "a dot becomes a dash")
check(fl.char_slug("   ") == "character", "a blank name still yields a usable folder")
check("/" not in fl.char_slug("../../etc/passwd") and ".." not in fl.char_slug("a..b"),
      "a path-ish name cannot smuggle a separator")

print("\nCHARACTERS · round trip")
src = _fake_png(os.path.join(tmp, "src-still.png"))
fl.write_character("ears",
                   {"name": "Ears", "prompt": "identity block", "seed": "12345",
                    "lora": "character_lora_v1.safetensors", "lora_strength": "0.8"},
                   still_src=src)
meta = fl.read_character("ears")
check(bool(meta), "read_character returns the saved dict")
check(meta.get("prompt") == "identity block", "the identity prompt block round-trips")
check(meta.get("seed") == "12345", "the seed round-trips")
check(meta.get("lora") == "character_lora_v1.safetensors", "the LoRA name round-trips")
check(meta.get("lora_strength") == "0.8", "the LoRA strength round-trips")
check(fl.list_characters() == [("ears", "Ears")], "list_characters reports (slug, name)")

print("\nCHARACTERS · the still is COPIED, never referenced in place")
os.remove(src)                                   # ← the whole point
st = fl.character_still("ears")
check(st is not None and os.path.exists(st),
      "the still STILL resolves after its source file is deleted")
if st:
    check(os.path.dirname(os.path.abspath(st)) == os.path.join(tmp, "ears"),
          "…and its copy lives inside the character's own folder")

print("\nCHARACTERS \u00b7 RE-SAVING must not drop the still")
# \u26a0\ufe0f _fill() hands the editor the character's OWN still.png, so a plain SAVE passes that
# path back to write_character. copyfile(src, src) raises SameFileError, which a bare except
# swallowed into meta.pop("still") - the PNG survived but the character forgot it, silently.
_rt = fl.character_still("ears")
fl.write_character("ears", fl.read_character("ears"), still_src=_rt)
_m2 = fl.read_character("ears")
check(bool(_m2.get("still")), "re-saving with the character's OWN still keeps the still key")
check(fl.character_still("ears") is not None, "...and character_still still resolves")
check(os.path.exists(os.path.join(tmp, "ears", "still.png")), "...and the png is still there")

print("\nCHARACTERS · refuses to escape its directory")
try:
    fl.delete_character("../../definitely-not-here")
    check(False, "delete_character refuses a path outside CHARACTERS")
except ValueError:
    check(True, "delete_character refuses a path outside CHARACTERS")

print("\nCHARACTERS · garbage in, no crash out")
check(fl.read_character("does-not-exist") is None, "a missing character reads as None")
check(fl.character_still("does-not-exist") is None, "a missing character has no still")
# The real-world garbage case is a character FOLDER whose character.json is corrupt — not a
# stray file (list_characters only ever looks at directories).
os.makedirs(os.path.join(tmp, "broken"), exist_ok=True)
with open(os.path.join(tmp, "broken", "character.json"), "w") as f:
    f.write("not json at all")
check(fl.read_character("broken") is None, "garbled JSON reads as None rather than raising")
check(fl.list_characters() == [("broken", "broken"), ("ears", "Ears")],
      "a corrupt character still lists (falling back to its slug as the name)")

print("\nCHARACTERS · delete")
fl.delete_character("ears")
check(fl.read_character("ears") is None, "delete removes the character")
check(not os.path.exists(os.path.join(tmp, "ears")), "…and its folder")

print("\nCHARACTERS \u00b7 proportional axes are SLIDERS carrying NUMBERS")
check(len(fl.CHAR_SLIDERS) >= 10, "there are %d proportional axes" % len(fl.CHAR_SLIDERS))
check(all(len(ph) == 7 for _, ph in fl.CHAR_SLIDERS),
      "every slider has exactly 7 phrases, one per level -3..+3")
check(all(ph[3] == "" for _, ph in fl.CHAR_SLIDERS),
      "level 0 is EMPTY on every slider, so a neutral body adds no filler")
check(any(a == "Ears" for a, _ in fl.CHAR_SLIDERS), "Ears is a slider")
check(any(a == "Arm length" for a, _ in fl.CHAR_SLIDERS), "arms are a slider (Ryan asked for them)")
check(any(a == "Leg length" for a, _ in fl.CHAR_SLIDERS), "legs are a slider")
check(fl.char_slider_level({}, "Ears") == 0, "an unset slider reads as 0")
check(fl.char_slider_level({"Ears": 99}, "Ears") == 3, "+99 clamps to +3")
check(fl.char_slider_level({"Ears": -99}, "Ears") == -3, "-99 clamps to -3")
check(fl.char_slider_level({"Ears": "wat"}, "Ears") == 0, "garbage reads as 0 rather than crashing")
check(fl.char_slider_level({"Ears": "2"}, "Ears") == 2,
      "a numeric STRING works - JSON round-trips numbers as text")
check(fl.char_slider_phrase({"Ears": -3}, "Ears") == fl.CHAR_SLIDER_MAP["Ears"][0],
      "-3 gives the smallest phrase")
check(fl.char_slider_phrase({"Ears": 3}, "Ears") == fl.CHAR_SLIDER_MAP["Ears"][6],
      "+3 gives the largest phrase")
check(fl.char_slider_phrase({"Ears": 0}, "Ears") == "", "level 0 composes to nothing")
check(fl.char_slider_phrase({}, "Not An Axis") == "", "an unknown axis composes to nothing")

print("\nCHARACTERS \u00b7 the composer")
_t = fl.compose_character_prompt({})
check("photorealistic" in _t and "full body" in _t, "the neutral block still names style + framing")
check(_t.endswith("85mm lens."), "the fixed deadpan tail survives")
_big = fl.compose_character_prompt({"Ears": 3})
check(fl.CHAR_SLIDER_MAP["Ears"][6] in _big, "sliding to +3 puts the extreme phrase in the block")
check(_big.lower().count("ears") == 1, "...and says it exactly once")
# ⚠️ The dedupe: the Feature speaks more strongly, so a slider that AGREES must stand down.
_dup = fl.compose_character_prompt({"Head size": 3,
                                    "Feature": "a COLOSSAL oversized head, twice as wide"})
check(_dup.lower().count("colossal") == 1, "a slider stands down when the Feature already says it")
_gar = fl.compose_character_prompt({"Ears": None, "Nose": "x", "Height": 2.7})
check(isinstance(_gar, str) and _gar.endswith("85mm lens."), "garbage picks still compose safely")

print("\nCHARACTERS \u00b7 the option vocabulary")

# ⚠️ These lists are pasted STRAIGHT into a prompt. The composer joins them with ", " and
# PREFIXES Wardrobe with "wearing ", so a value carrying its own prefix composes to
# "wearing wearing a coat" - a defect you only ever see by reading a composed block out loud.
_opt = dict(fl.CHAR_OPTIONS)
for _ax, _vals in fl.CHAR_OPTIONS:
    _dups = sorted(v for v in set(_vals) if _vals.count(v) > 1)
    check(not _dups, "%s has no duplicate options (dupes: %r)" % (_ax, _dups))
    check(len(_vals) >= 2, "%s offers at least two choices (%d)" % (_ax, len(_vals)))
    check(all(isinstance(v, str) and v == v.strip() for v in _vals),
          "%s has no stray whitespace in any option" % _ax)

check(all(v.startswith(("a ", "an ")) for v in _opt["Subject"]),
      "every Subject is a noun phrase starting with a/an")
check(all(v.startswith("with ") for v in _opt["Skin"]),
      "every Skin reads as a clause starting with 'with'")
check(not any(v.lower().startswith("wearing") for v in _opt["Wardrobe"]),
      "no Wardrobe value carries its own 'wearing' - the composer adds it")
check(_opt["Feature"][0] == "", "Feature's first value is blank (an ordinary person is valid)")

# Ryan asked for MORE of these five, so pin the floor - a future edit must not quietly shrink it.
for _ax, _min in (("Subject", 12), ("Skin", 10), ("Hair", 16), ("Wardrobe", 16), ("Feature", 20)):
    check(len(_opt[_ax]) >= _min,
          "%s offers a real spread of choices (%d >= %d)" % (_ax, len(_opt[_ax]), _min))

# the real test: pick the LAST value of every axis and read what actually comes out
_pick = {ax: vals[-1] for ax, vals in fl.CHAR_OPTIONS}
_full = fl.compose_character_prompt(_pick)
check("wearing wearing" not in _full.lower(), "the composer never doubles 'wearing'")
check(" a a " not in _full.lower(), "the composer never doubles an article")
check(_full.count(_pick["Subject"]) == 1, "the chosen Subject appears exactly once")
check(_full.count("wearing " + _pick["Wardrobe"]) == 1, "the Wardrobe is worn exactly once")
check(_full.lower().count(_pick["Skin"].lower()) == 1, "the Skin appears exactly once")
check(_full.lower().count(_pick["Hair"].lower()) == 1, "the Hair appears exactly once")
check(_full.endswith("85mm lens."), "and it still ends with the deadpan tail")

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 80)
print("FAILURES: %d" % len(FAILS))
for f in FAILS:
    print("   -", f)
print("=" * 80)
sys.exit(1 if FAILS else 0)