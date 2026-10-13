#!/usr/bin/env python3
"""GPU-free, model-free checks for the Weirdo DESCRIBE IT box.

    /opt/homebrew/bin/python3 tools/weirdo_matrix.py      # exit 1 on any failure

The model is STUBBED (Director._post is replaced), so nothing reaches forge or Ollama.
FORGE_LAB_DATA is a throwaway dir, so Ryan's real data is never touched.
"""
import importlib.util
import json
import os
import random
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["FORGE_LAB_DATA"] = tempfile.mkdtemp(prefix="forgeweirdo-")      # BEFORE import
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(ROOT, "forge_lab.py"))
fl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fl)
import director as D                                                     # noqa: E402

SRC = open(os.path.join(ROOT, "forge_lab.py"), encoding="utf-8").read()
FAILS, N = [], [0]


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def hot(picks):
    return [k for k, v in picks.items()
            if (k == "Feature" and v) or (k in fl.CHAR_SLIDER_ORDER and v)]


FISHER = {"subject": "an old fisherman", "skin": "", "hair": "wiry white hair",
          "facial_hair": "with a grey beard", "wardrobe": "yellow oilskins and rubber boots",
          "accessory": "", "setting": "a wet wooden dock in a grey harbour at dawn",
          "lighting": "flat overcast morning light", "absurd": "enormous hands the size of shovels",
          "action": "mends a fishing net with total concentration"}

# ---------------------------------------------------------------- §1 the merge
print("§1 apply_weirdo_fit — the description lands, and ONE thing stays wrong")
for seed in range(20):
    base, act = fl.roll_weirdo(random.Random(seed))          # half Feature, half a ±3 slider
    p, a = fl.apply_weirdo_fit(base, act, FISHER)
    if hot(p) != ["Feature"] or p["Feature"] != FISHER["absurd"]:
        check(False, "seed %d: exactly one absurd thing after the fit (got %r)" % (seed, hot(p)))
        break
else:
    check(True, "20 rolls: a fitted roll breaks EXACTLY ONE thing — the model's absurd trait "
                "(a rolled ±3 slider is cleared)")
base, act = fl.roll_weirdo(random.Random(3))
p, a = fl.apply_weirdo_fit(base, act, FISHER)
check(p["Subject"] == "an old fisherman" and p["Wardrobe"] == FISHER["wardrobe"]
      and p["Setting"] == FISHER["setting"], "who / clothes / place come from the description")
check(p["Skin"] == "" and p["Accessory"] == "" and p["Hair"] == "wiry white hair",
      "the fit is FINAL: an empty look field means NONE, not the roll's (the goatee bug)")
_w = dict(FISHER, subject="", wardrobe="", setting="")
p3, _a3 = fl.apply_weirdo_fit(base, act, _w)
check(p3["Subject"] == base["Subject"] and p3["Wardrobe"] == base["Wardrobe"]
      and p3["Setting"] == base["Setting"],
      "an empty who / clothes / place falls back to the roll (a block never loses them)")
_b = fl.compose_character_prompt(dict(base, **{"Facial hair": ""}), False, act)
check("goatee" not in _b and "moustache" not in _b and "beard" not in _b,
      "an empty Facial hair composes with NO facial hair at all")
check(p["Style"] == "photorealistic" and p["Framing"] == fl.CHAR_OPTION_DEFAULTS["Framing"],
      "Style stays hyper-real and Framing stays full-body")
check(a == FISHER["action"], "the action comes from the description")
p2, a2 = fl.apply_weirdo_fit(base, act, dict(FISHER, action="", absurd=""))
check(a2 == act and hot(p2) == hot(base),
      "no action / no absurd from the model -> the roll's own action and dial are kept")
blk = fl.compose_character_prompt(p, False, a)
check("an old fisherman" in blk and "wearing yellow oilskins" in blk
      and "enormous hands" in blk and blk.endswith("85mm lens."),
      "the composed block carries it, with the deadpan still tail")

# ---------------------------------------------------------------- §2 the model call (stubbed)
print("§2 Director.weirdo_fit — cleans what the model returns")
sent = []


def stub(reply):
    def _post(path, payload):
        sent.append(payload)
        return {"message": {"content": json.dumps(reply)}}
    return _post


d = D.Director(host="http://stub")
d._post = stub(dict(FISHER, wardrobe="wearing yellow oilskins.",
                    action="mends his net slowly", hair="sparse grey thinning",
                    skin="pale and slightly blotchy"))
out = d.weirdo_fit("an old fisherman with enormous hands", vein="Formal-European",
                   base=base, action=act)
check(out["wardrobe"] == "yellow oilskins", "a leading 'wearing' and trailing '.' are stripped "
                                           "(got %r)" % out["wardrobe"])
check(out["hair"] == "sparse grey thinning hair" and out["skin"] == "pale and slightly blotchy skin",
      "hair/skin phrases that drop their noun get it back (got %r / %r)" % (out["hair"], out["skin"]))
check(not re.search(r"\b(his|her)\b", out["action"]), "the action is made gender-neutral "
                                                       "(got %r)" % out["action"])
u = sent[-1]["messages"][-1]["content"]
check("an old fisherman with enormous hands" in u, "the description is in the request")
check("description always wins" in u, "the request says the description beats the vein")
check(sent[-1]["format"] is D.WEIRDO_FIT_SCHEMA, "the reply is schema-constrained")
del sent[:]
bad = dict(FISHER)
bad.pop("absurd")
d._post = stub(bad)
try:
    d.weirdo_fit("x", base=base)
    check(False, "a reply missing a required key raises (it returned instead)")
except D.DirectorError:
    check(len(sent) == 2, "a reply missing a required key is retried once, then raises "
                          "(%d attempts)" % len(sent))

# ---------------------------------------------------------------- §3 the window wiring
print("§3 the window wiring")
m = re.search(r"\n    def _roll\(\):\n(.*?)\n    def _poll_fit", SRC, re.S)
body = m.group(1) if m else ""
check(bool(body), "found the window's _roll()")
check("if not desc:" in body and "_show_roll(picks, action" in body,
      "an EMPTY box rolls instantly, exactly like before (no model call)")
check("threading.Thread(target=work" in body, "the model call runs on a worker thread")
m2 = re.search(r"\n    def _poll_fit\(.*?\n    def _show_roll", SRC, re.S)
pf = m2.group(0) if m2 else ""
check('if job["err"]:' in pf and "return" in pf.split('if job["err"]:')[1].split("p2, a2")[0],
      "a failed fit changes nothing (no silent random fallback)")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f in FAILS:
    print("  FAILED: %s" % f)
sys.exit(1 if FAILS else 0)
