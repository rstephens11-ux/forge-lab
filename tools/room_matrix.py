#!/usr/bin/env python3
"""GPU-free checks for writers_room.py — the writers' room (plan-first drafts -> scored ->
punch-up -> cut; the separate game step retired 2026-10-24 after the writer bake-off). The writer is faked; what's checked is what each step ASKS and how the
results are chosen.
    python3 tools/room_matrix.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import writers_room as wr  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


IDEA = "a nervous lighthouse keeper and an overconfident pigeon trainer argue about fog"
CAST = {"GERALD": "a nervous lighthouse keeper.", "VIVIAN": "an overconfident pigeon trainer."}

GOOD = [("VIVIAN", "Gerald. My pigeons have circled your lighthouse for six hours."),
        ("GERALD", "I'm so sorry. Should I turn the lamp off?"),
        ("VIVIAN", "Ships would crash, Gerald."),
        ("GERALD", "I panicked. I said Portugal.")]
OFF_IDEA = [("VIVIAN", "The clouds owe me rent."), ("GERALD", "You call that a face?"),
            ("VIVIAN", "They live here rent-free."), ("GERALD", "Paying for the view.")]
LONG = [("VIVIAN", "Gerald. My pigeons have circled your lighthouse for six hours."),
        ("GERALD", " ".join(["very"] * 40) + " sorry about the lamp and the fog."),
        ("VIVIAN", " ".join(["truly"] * 40) + " the pigeons are champions."),
        ("GERALD", "I panicked. I said Portugal.")]


class Room:
    def __init__(self, drafts, scores=None, pick=1, game=True, cut_lines=None, cold=None,
                 clar=None):
        self.cold = cold or (lambda lines: (5, []))
        self.clar = clar
        self.drafts = list(drafts)
        self.scores = scores or [3] * len(self.drafts)
        self.pick, self.game, self.cut_lines = pick, game, cut_lines
        self.asks = []

    def _chat(self, system, user, schema, temperature=0.7, model=None):
        self.asks.append((schema, user, temperature))
        if schema is wr.GAME_SCHEMA:
            if not self.game:
                raise RuntimeError("writer busy")
            return {"games": [{"odd_one": "VIVIAN never admits fault", "straight_one":
                               "GERALD apologises", "game": "he takes the blame for more and "
                               "more", "steps": ["a", "b", "c"], "button": "I said Portugal."}
                              ] * 3, "best": 2}
        if schema is wr.DRAFT_SCHEMA:
            k = len([a for a in self.asks if a[0] is wr.DRAFT_SCHEMA]) - 1
            lines = self.drafts[k % len(self.drafts)]
            if lines is None:
                return {"title": "", "lines": []}
            return {"title": "Draft %d" % (k + 1), "hook": lines[0][1],
                    "situation": "a lighthouse keeper apologises to a pigeon trainer",
                    "joke": "he takes the blame for more and more",
                    "escalations": ["the lamp", "the fog", "Portugal"],
                    "button": "I panicked. I said Portugal.",
                    "lines": [{"speaker": s, "direction": "", "text": t} for s, t in lines]}
        if schema is wr.JUDGE_SCHEMA:
            k = len([a for a in self.asks if a[0] is wr.JUDGE_SCHEMA]) - 1
            v = self.scores[k % len(self.scores)]
            return {r: v for r in wr.RUBRIC} | {"weakest_line": 2, "total": 99}
        if schema is wr.PUNCH_SCHEMA:
            return {"options": ["Well. They'll love Portugal.", "b", "c", "d", "e", "f"]}
        if schema is wr.PICK_SCHEMA:
            return {"best": self.pick, "why": "x"}
        if schema is wr.COLD_SCHEMA:
            body = user.split("\n\n")[1]
            ms, conf = self.cold([ln.split(": ", 1)[1] for ln in body.splitlines()])
            return {"what_happens": "someone returns something", "makes_sense": ms,
                    "confusing": [{"line": k, "why": "huh"} for k in conf]}
        if schema is wr.DIALOGUE_SCHEMA and "loses viewers" in user:
            ls = self.clar
            return {"title": "Clear", "lines": [{"speaker": s_, "direction": "", "text": t}
                                                for s_, t in ls]}
        if schema is wr.DIALOGUE_SCHEMA:
            ls = self.cut_lines or [("VIVIAN", "changed hook"), ("GERALD", "short"),
                                    ("GERALD", "changed button")]
            return {"title": "Cut", "lines": [{"speaker": s, "direction": "", "text": t}
                                              for s, t in ls]}
        raise AssertionError("unexpected ask")


print("§1 lengths")
check((wr.n_lines(15), wr.n_lines(20), wr.n_lines(30), wr.n_lines(120)) == (4, 5, 8, 10),
      "about one line per 3.8 s of video (15 s -> 4, 20 s -> 5, 30 s -> 8, capped at 10)")
r = wr.length_rule(20)
check("39 words" in r and "5 lines" in r and "UNEVEN" in r and "under eight words" in r,
      "the length rule: a word budget, a line count, uneven lines, a short button")
check(wr.budget(30) == 61 and wr.budget(30) < 30 * wr.WORDS_PER_SECOND,
      "the word budget leaves room for the edit's handles and end hold (30 s -> 61 words; "
      "72 made a 30 s ask cut to 38 s)")
check(wr.idea_nouns(IDEA)[:6] == ["nervous", "lighthouse", "keeper", "overconfident", "pigeon",
                                  "trainer"] and "and" not in wr.idea_nouns(IDEA),
      "the idea's content words (no stop words)")

print("§2 the room picks the best draft")
room = Room([OFF_IDEA, GOOD, GOOD], scores=[5, 3, 4])
out = wr.write(room, "BRIEF", CAST, 20, idea=IDEA, shape="SHAPE", format_name="Sketch")
ga = [a for a in room.asks if a[0] is wr.GAME_SCHEMA]
da = [a for a in room.asks if a[0] is wr.DRAFT_SCHEMA]
ja = [a for a in room.asks if a[0] is wr.JUDGE_SCHEMA]
check(not ga, "no separate game step any more (the bake-off: each draft plans its own)")
_a0 = da[0][1] if da else ""
check(len(da) == 3 and all(a[1].startswith("BRIEF") and "GERALD: a nervous" in a[1]
                           for a in da) and
      _a0.index("- situation:") < _a0.index("- joke:") < _a0.index("- escalations:") <
      _a0.index("- button:") < _a0.index("Then write the lines") and
      "about 5 lines, about 39 words" in _a0,
      "step B: three drafts, each PLAN FIRST (situation, joke, escalations, button) then "
      "the lines, sized to the length (5 lines, 39 words for 20 s)")
check("Plain words only" in _a0 and "jargon" in _a0 and "Woof!" in _a0 and
      "VOICE ONLY" in _a0 and "plain words" in wr.SYSTEM_WRITE.lower().replace(
          "everyday words", "plain words"),
      "the ask: plain words, no jargon, no sounds as lines, voice-only speakers explained")
check(out.get("room", {}).get("plan", "").startswith("a lighthouse keeper apologises"),
      "the plan the shot scene was written from is kept (room['plan'])")
check(len(ja) == 3 and all(a[1].count("SCRIPT:") == 1 for a in ja),
      "step C: each draft scored ALONE (no side-by-side position bias)")
check(out["lines"][0]["text"].startswith("Gerald. My pigeons") and
      out["room"]["scores"][0]["total"] < out["room"]["scores"][2]["total"],
      "the off-idea draft lost even with the top raw score — it ignored the idea and used "
      "banned lines (%r)" % [s_["total"] for s_ in out["room"]["scores"]])
check(out["room"]["scores"][0]["scores"].get("penalty_idea") == -3 and
      out["room"]["scores"][0]["scores"].get("penalty_banned") == -6,
      "penalties: -3 for ignoring THE IDEA, -2 per banned line ('you call that a', "
      "'rent-free', 'owe me rent')")
check(out["lines"][-1]["text"] == "I panicked. I said Portugal." and "punch" not in out["room"],
      "the editor kept the original button when it was best")

print("§2b the cold read: a viewer who sees only the script")
GRAPE = [("HAROLD", "I am returning this single grape for negligence."),
         ("ELENA", "Technically speaking, the bunch is one item."),
         ("HAROLD", "Then I am returning the bunch, the aisle, and the building."),
         ("ELENA", "Returned.")]
GRAPE_CAST = {"HAROLD": "an old man.", "ELENA": "a grocery clerk."}
GRAPE_CLEAR = [("HAROLD", "I'd like to return this one grape. It was sour."),
               ("ELENA", "Sir, grapes are sold by the bunch. One grape isn't an item."),
               ("HAROLD", "Then I'm returning the bunch, the aisle, and the building."),
               ("ELENA", "Returned.")]
_muddled = lambda ls: (2, [1, 2]) if any("negligence" in t for t in ls) else (5, [])
room = Room([GRAPE, GRAPE_CLEAR], scores=[5, 4], cold=_muddled)
out = wr.write(room, "BRIEF", GRAPE_CAST, 15, idea="an old man returns a single grape")
ca = [a for a in room.asks if a[0] is wr.COLD_SCHEMA]
check(ca and all("THE IDEA" not in a[1] and "game" not in a[1].split("1. what")[0].lower()
                 for a in ca),
      "the cold read sees only the script: no idea, no game")
check(out["lines"][0]["text"].startswith("I'd like to return") and
      out["room"]["scores"][0]["scores"].get("penalty_unclear") == -8,
      "the muddled grape draft lost to the clear one despite the higher raw score "
      "(%r)" % [s_["total"] for s_ in out["room"]["scores"]])
room = Room([GRAPE], cold=_muddled, clar=GRAPE_CLEAR)
logs = []
out = wr.write(room, "BRIEF", GRAPE_CAST, 15, idea="x", drafts=1, log=logs.append)
check(out["lines"][0]["text"].startswith("I'd like to return") and
      out["lines"][-1]["text"] == "I panicked. I said Portugal." and
      out["room"]["cold"].get("rewrote") and
      out["room"]["cold"]["makes_sense"] == 5,
      "only draft muddled -> the lines a viewer lost are rewritten, the button kept")
check(any("cold read: 2/5 | line 1: huh" in l_ for l_ in logs),
      "the log says what the viewer didn't follow")
ask = [a for a in room.asks if a[0] is wr.DIALOGUE_SCHEMA and "loses viewers" in a[1]][0][1]
check("line 1: huh" in ask and "including the last line" in ask and
      "he takes the blame" in ask,
      "the rewrite is told which lines and why, the plan, and to keep the button")
room = Room([GRAPE], cold=lambda ls: (2, [1]), clar=GRAPE_CLEAR)
out = wr.write(room, "BRIEF", GRAPE_CAST, 15, idea="x", drafts=1)
check(out["lines"] == [{"speaker": s_, "direction": "", "text": t} for s_, t in GRAPE] or
      out["lines"][0]["text"] == GRAPE[0][1],
      "a rewrite that reads no clearer is thrown away")
room = Room([GRAPE], cold=_muddled, clar=GRAPE_CLEAR[:1])
out = wr.write(room, "BRIEF", GRAPE_CAST, 15, idea="x", drafts=1)
check(out["lines"][0]["text"] == GRAPE[0][1],
      "a rewrite that drops lines is thrown away")
room = Room([GOOD])
out = wr.write(room, "BRIEF", CAST, 20, idea=IDEA, drafts=1)
check(len([a for a in room.asks if a[0] is wr.COLD_SCHEMA]) == 1 and
      not [a for a in room.asks if "loses viewers" in a[1]] and
      out["room"]["cold"]["makes_sense"] == 5,
      "a clear scene: one cold read, no rewrite")
check("[Stamps his forehead.]" in wr.seen([{"speaker": "ELENA", "text": "Returned.",
                                            "direction": "Stamps his forehead."}]) and
      "[beat]" not in wr.seen([{"speaker": "A", "text": "x", "direction": "beat"}]),
      "the viewer sees what people do, too")

print("§3 punch-up, the button, the cut")
room = Room([GOOD], pick=2)
out = wr.write(room, "BRIEF", CAST, 20, idea=IDEA, drafts=1)
check(out["lines"][-1]["text"] == "Well. They'll love Portugal." and
      out["room"].get("punch") == "Well. They'll love Portugal.",
      "step D: a better last line replaces the button")
bad_btn = [("VIVIAN", "Gerald. My pigeons have circled your lighthouse."),
           ("GERALD", "Sorry."), ("VIVIAN", "Something else entirely.")]
room = Room([bad_btn])
d = wr.draft(room, "BRIEF", CAST, 20)
check(d["lines"][-1]["text"] == "I panicked. I said Portugal.",
      "a draft whose last line isn't its button: the button becomes the last line")
room = Room([LONG], cut_lines=[("VIVIAN", "changed"), ("GERALD", "short"),
                               ("GERALD", "changed")])
out = wr.write(room, "BRIEF", CAST, 15, idea=IDEA, drafts=1)
check(out["lines"][0]["text"] == LONG[0][1] and out["lines"][-1]["text"] == LONG[-1][1] and
      out["lines"][1]["text"] == "short" and wr.words_in(out["lines"]) < wr.words_in(
          [{"text": t} for _s, t in LONG]),
      "step E: over budget -> the middle is cut; the hook and the button stay word for word")

print("§4 fallbacks")
room = Room([GOOD], game=False)
out = wr.write(room, "BRIEF", CAST, 20, idea=IDEA, drafts=1)
da = [a for a in room.asks if a[0] is wr.DRAFT_SCHEMA]
check(out["lines"] and "THE GAME" not in da[0][1] and "game" not in out["room"],
      "no game (writer busy) -> drafts without one")
room = Room([None])
try:
    wr.write(room, "BRIEF", CAST, 20, idea=IDEA, drafts=2)
    raised = False
except ValueError:
    raised = True
check(raised, "no usable draft at all -> it says so (Quick then uses the one-shot writer)")

print("§5 a sound or an action is never a spoken line (2026-10-24: 'Woof!')")
WOOF = [("PAULA", "Tell me about yourself, Bennett."), ("BENNETT", "Woof! Woof!"),
        ("PAULA", "I panicked. I said Portugal.")]
room = Room([WOOF, GOOD])
d = wr.draft(room, "BRIEF", {"PAULA": "an HR manager.", "BENNETT": "a dog.",
                             "GERALD": "x", "VIVIAN": "y"}, 20)
_da = [a for a in room.asks if a[0] is wr.DRAFT_SCHEMA]
check(len(_da) == 2 and "REJECTED" in _da[1][1] and "sound or an action" in _da[1][1] and
      not any("Woof" in li["text"] for li in d["lines"]),
      "a draft with 'Woof! Woof!' as a line is sent back with the reason, and the retry kept")
_rp = Room([GOOD])
_ds = wr.drafts_for_pick(_rp, "BRIEF", CAST, 20, n=3)
_pa = [a[1] for a in _rp.asks if a[0] is wr.DRAFT_SCHEMA]
check(len(_pa) == 3 and "OTHER DRAFTS" not in _pa[0] and
      _pa[1].count("* he takes the blame for more and more") == 1 and
      _pa[2].count("* he takes the blame for more and more") == 2,
      "each later draft is told the jokes the earlier ones used, and to find a different one")
check(_ds[1] and all(x.get("plan", "").startswith("a lighthouse keeper") for x in _ds[1]) and
      _ds[0].startswith("a lighthouse keeper"),
      "drafts to pick from carry their plan (shown beside each draft)")

print("§9 repeats and the straight character")
check("straight" in wr.RUBRIC and "straight:" in wr.JUDGE_ASK,
      "the editor scores whether one character stays the straight one")
_rl = [{"text": t} for t in ("Your jaw exceeds the setback.", "It is load-bearing.",
                             "It overhangs the street.", "I am load-bearing, legally.",
                             "Your jaw is load-bearing too.")]
check(wr.repeat_penalty(_rl) == 2 and wr.repeat_penalty(_rl[:3]) == 0 and
      wr.repeat_penalty([{"text": "My ears. Your ears."}, {"text": "Ears again."},
                         {"text": "Fine."}]) == 2,
      "-2 when the button leans on a word used twice already; -2 for a body gag said 3 times")

print("§x a line never starts with its speaker's name (test run: 'Elena: Returned.')")
_wr = sys.modules.get("writers_room") or __import__("writers_room")
check(_wr.strip_speaker("Elena: Returned.", {"ELENA", "HAROLD"}) == "Returned." and
      _wr.strip_speaker("ELENA \u2014 Returned.", {"Elena"}) == "Returned." and
      _wr.strip_speaker("Note: it is cold.", {"ELENA"}) == "Note: it is cold." and
      _wr.strip_speaker("Returned.", {"ELENA"}) == "Returned.",
      "a speaker label is stripped; other colons stay")


class _PD:
    def __init__(self):
        self.n = 0

    def _chat(self, system, user, schema, temperature=1.0, model=None):
        self.n += 1
        if schema is _wr.PUNCH_SCHEMA:
            return {"options": ["ELENA: Returned.", "ELENA: Denied."]}
        return {"best": 2, "why": "x"}


_d0 = {"lines": [{"speaker": "HAROLD", "text": "I return this grape."},
                 {"speaker": "ELENA", "text": "No."}]}
_nd, _pk = _wr.punch_up(_PD(), _d0)
check(_pk == "Returned." and _nd["lines"][-1]["text"] == "Returned.",
      "a punch-up option that comes back labelled is used without the label (%r)" % _pk)

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
