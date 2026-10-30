#!/usr/bin/env python3
"""GPU-free, model-free checks for IN A STYLE (char_styles.py + the Characters button).

    /opt/homebrew/bin/python3 tools/char_styles_matrix.py      # exit 1 on any failure

The writer is a FAKE (no forge, no Ollama). FORGE_LAB_DATA is a throwaway dir.
"""
import importlib.util
import os
import random
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["FORGE_LAB_DATA"] = tempfile.mkdtemp(prefix="forgestyles-")     # BEFORE import
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(ROOT, "forge_lab.py"))
fl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fl)
import char_styles as cs                                                 # noqa: E402
import weirdos as w                                                      # noqa: E402

FAILS, N = [], [0]


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


class FakeDirector:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def _chat(self, system, user, schema, temperature=0.7, model=None):
        self.sent.append((system, user, schema))
        return dict(self.reply)


REPLY = {"name": "Lord Cuthbert Mallow", "subject": "a bloated old lord", "pronoun": "he",
         "look": "seventies, ruddy skin, bald", "feature": "jowls that rest on his collar",
         "wardrobe": "wearing a black fur robe over gold brocade", "setting": "in a gilded hall",
         "action": "He dabs his mouth with a lace napkin", "catchphrase": "\"Pursue joy.\"",
         "hook": "h", "who": "w", "voice": "plummy", "pov": "Believes x.", "want": "y",
         "blind_spot": "z", "habit": "q"}

print("§1 every style is complete")
for name, st in cs.STYLES.items():
    for k in ("hint", "roles", "system", "framing", "compose", "pools", "settings",
              "actions", "lines"):
        check(k in st, "%s has %r" % (name, k))
    check(st["framing"] in dict(fl.CHAR_OPTIONS)["Framing"],
          "%s: framing is one of the editor's Framing picks (so the still gets its size)"
          % name)
    check(st["roles"][0][0] == "Any role", "%s: the first role is 'Any role'" % name)
    check(set(st["pools"]) == {r for r, _d in st["roles"][1:]},
          "%s: every role has an offline pool, and no pool lacks a role" % name)
    check("child" in st["system"] and "sexual" in st["system"],
          "%s: the writer is told adults only, nothing sexual" % name)
check(cs.DEFAULT in cs.STYLES, "the default style exists")

print("§2 invent (fake writer)")
d = FakeDirector(REPLY)
s = cs.invent(d, cs.DEFAULT, "The powerful one", brief="a duke who owns the mills",
              rng=random.Random(1))
sysmsg, user, schema = d.sent[-1]
check(schema is w.SCHEMA, "the reply is constrained to the Weirdo schema (lands like one)")
check("The powerful one" in user and "a duke who owns the mills" in user,
      "the role and what Ryan typed are in the request")
check(sysmsg == cs.style(cs.DEFAULT)["system"], "the style's own system prompt is used")
check(s["style"] == cs.DEFAULT and s["role"] == "The powerful one", "style + role are kept")
check(s["wardrobe"].startswith("a black fur") and s["setting"] == "a gilded hall"
      and s["action"].startswith("dabs") and "his" not in s["action"],
      "the reply is cleaned exactly like a Weirdo's")
s2 = cs.invent(FakeDirector(REPLY), cs.DEFAULT, "Any role", rng=random.Random(2))
check(s2["role"] in cs.roles(cs.DEFAULT)[1:], "'Any role' picks a real role (%s)" % s2["role"])
try:
    cs.invent(FakeDirector(dict(REPLY, subject="a schoolboy footman")), cs.DEFAULT)
    check(False, "a minor from the writer is refused")
except ValueError:
    check(True, "a minor from the writer is refused")

TR = "Genre trailer"
check(TR in cs.STYLES, "the genre trailer style is registered")
dt = FakeDirector(dict(REPLY, name="Sir Mittens", subject="a fluffy kitten knight",
                       pronoun="she", feature="a pink bow and a single tear"))
t = cs.invent(dt, TR, "The unlikely hero", rng=random.Random(4))
sy, us, _sc = dt.sent[-1]
check(t.get("genre") in dict(cs.TRAILER_GENRES) and ("THE GENRE: %s" % t["genre"]) in us,
      "a trailer character gets a genre, and the writer is told it (%s)" % t.get("genre"))
check(cs.TRAILER_RULES in us and "THIS STYLE'S RULES" in us,
      "the style's own casting rules are in the request")
check("lookalike" in sy and "trademarked" in sy,
      "the trailer writer is told: no real people or lookalikes, no trademarked characters")
check("posh" not in us, "the gilded satire rules don't leak into another style's request")
tp = cs.compose(t)
check(dict(cs.TRAILER_GENRES)[t["genre"]] in tp and "widescreen" in tp
      and t["genre"].lower() in tp, "the trailer prompt is lit for its own genre")
check(cs.picks(t)["Framing"] == cs.style(TR)["framing"], "trailer picks use its framing")
check("THE GENRE" not in d.sent[-1][1], "a style without genres sends no genre")
check(s.get("genre") is None, "and keeps none")

print("§3 the still prompt + picks")
p = cs.compose(s)
check(p.startswith("A photorealistic dystopian retro-future film still, three-quarter shot"),
      "the prompt opens with the style's framing")
check("wide-angle" in p and "film grain" in p and "prosthetic" in p,
      "the style's look (lens, grain, prosthetic realism) is in it")
check("a bloated old lord" in p and "jowls that rest on his collar".capitalize() in p,
      "the subject and the feature are in it")
pk = cs.picks(s)
check(pk["Framing"] == cs.style(cs.DEFAULT)["framing"] and pk["Feature"]
      and pk["Subject"].startswith("a bloated old lord"), "picks carry the framing + feature")
check(fl.char_still_size(pk["Framing"]) != fl.CHAR_STILL_SIZE_FALLBACK
      or pk["Framing"] in fl.CHAR_STILL_SIZE, "the framing has a real still size")
m = cs.compose(dict(cs.roll(cs.DEFAULT, "The machine", random.Random(0))))
check(" They are " in m and "They stand in" in m, "a machine ('they') composes grammatically")

print("§4 offline rolls")
for sn in cs.STYLES:
    bad, genres = 0, set()
    for seed in range(300):
        try:
            r_ = cs.roll(sn, random.choice(cs.roles(sn)), random.Random(seed))
            cs.compose(r_)
            genres.add(r_.get("genre"))
        except Exception:
            bad += 1
    check(bad == 0, "%s: 300 offline rolls across every role, none refused or crashing (%d)"
          % (sn, bad))
    if cs.style(sn).get("genres"):
        check(len(genres) > 1, "%s: offline rolls vary the genre" % sn)
        gs = cs.style(sn)["genre_settings"]
        off = [r_ for r_ in (cs.roll(sn, "Any role", random.Random(k)) for k in range(200))
               if r_["setting"] not in gs[r_["genre"]]]
        check(not off, "%s: an offline roll's setting always belongs to its genre (%d off)"
              % (sn, len(off)))
        check(set(gs) == {g for g, _l in cs.style(sn)["genres"]},
              "%s: every genre has its places" % sn)

print("§5 the Characters window")
ui = open(os.path.join(ROOT, "characters_ui.py"), encoding="utf-8").read()
check("In a style \\u2726 invent one" in ui, "the In a style button is in the cast column")
check("threading.Thread(target=work" in ui.split("def _styled")[1].split("def _poll_styled")[0],
      "the writer runs on a worker thread")
check("_c.roll(job[\"style\"], job[\"role\"])" in ui, "an unreachable writer falls back to the "
      "style's own pools")
land = ui.split("def _land_weirdo")[1].split("def _land_roll")[0]
check("_c.picks(spec), _c.compose(spec)" in land, "a styled character lands with its own "
      "picks and prompt")
check("style=spec[\"style\"]" in land and "genre=spec.get(\"genre\")" in land,
      "style, role and genre are saved with the character")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
