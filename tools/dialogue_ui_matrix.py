#!/usr/bin/env python3
"""GPU-free assertions for dialogue_ui.plan_render() — what a RENDER click sends to the rig.

Written by the reviewer after the first UI draft's render path would have failed on every line
(clone items named a voice key the voices map did not contain). The window itself is covered by
the FORGE_LAB_TESTDIALOGUE driver; THIS covers the rig job it builds, which no driver sees.

Run: /opt/homebrew/bin/python3 tools/dialogue_ui_matrix.py   (exit 0 = green)
"""
import os
import sys
import tempfile
import wave

TMP = tempfile.mkdtemp(prefix="dlgui-matrix-")
os.environ["FORGE_LAB_DATA"] = TMP          # BEFORE importing anything that reads DATA
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import dialogue as dlg          # noqa: E402
import dialogue_ui as ui        # noqa: E402  (tkinter is imported inside open_dialogue only)

FAILS, N = [], [0]


def check(cond, msg):
    N[0] += 1
    print(("  ok  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def _wav(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x10" * 2400)


SCRIPT = ("> **ALICE** — Are you going to complain?\n"
          "> **BOB** — To whom? It's an app.\n"
          "> **ALICE** — *(shouting)* An app is not a person.\n"
          "> **POSTAL CLERK** — Next.\n")

g = dlg.new_voice("Alice"); dlg.save_voice(g)
t = dlg.new_voice("Bob"); dlg.save_voice(t)
pj = dlg.new_project("t", SCRIPT)
pj["cast"] = {"ALICE": "alice", "BOB": "bob"}      # POSTAL CLERK left uncast
voices = {v["slug"]: v for v in dlg.list_voices()}

print("1  fresh voices: everything is a DESIGN, nothing can clone yet")
d, job, skipped = ui.plan_render(pj, voices, "missing")
ids = sorted(x["id"] for x in d)
check(ids == ["alice:neutral", "bob:neutral"],
      "one design per voice+mood actually needed (got %r)" % ids)
check(job is None, "no clone job before any reference exists")
check(any("POSTAL CLERK" in m for m in skipped), "an uncast speaker is reported, not dropped silently")
check(all(x["instruct"] == dlg.compose_description(voices[x["id"].split(":")[0]], "neutral")
          for x in d), "design instruct is exactly what the VOICE box shows")
check(len({x["out"] for x in d}) == len(d), "design outputs have distinct names")

print("2  samples exist: every cast line clones, keys line up")
_wav(os.path.join(dlg.voices_dir(), "alice", "neutral.wav"))
_wav(os.path.join(dlg.voices_dir(), "bob", "neutral.wav"))
voices = {v["slug"]: v for v in dlg.list_voices()}
d, job, skipped = ui.plan_render(pj, voices, "missing")
check(d == [], "no re-design once the samples exist (got %r)" % [x["id"] for x in d])
check(job is not None and len(job["clone"]) == 3, "three cast lines -> three clone items")
if job:
    check(all(c["voice"] in job["voices"] for c in job["clone"]),
          "EVERY clone item names a key that is in the voices map")
    check(job["voices"]["alice:neutral"]["ref_wav"] != job["voices"]["bob:neutral"]["ref_wav"],
          "Alice and Bob clone from different samples")
    check(all(os.path.isabs(v["ref_wav"]) for v in job["voices"].values()),
          "ref paths are absolute local files (client uploads them)")
    check([c["_line"] for c in job["clone"]] == [0, 1, 2], "clone items keep line order")
    check(job["clone"][2]["voice"] == "alice:neutral",
          "a 'shouting' direction falls back to neutral when the voice HAS no shouting mood")

print("3  missing vs all")
pj["lines"][0]["takes"] = [{"file": "lines/000_1.wav", "seconds": 1, "heard": "", "score": None}]
_d, job, _s = ui.plan_render(pj, voices, "missing")
check(job and [c["_line"] for c in job["clone"]] == [1, 2], "MISSING skips a line that has a take")
_d, job, _s = ui.plan_render(pj, voices, "all")
check(job and len(job["clone"]) == 3, "ALL re-reads every line")
check(_d == [], "ALL never re-designs an existing voice (that would be a different person)")

print("4  a mood the voice has gets its OWN sample")
g = dlg.load_voice("alice")
g["moods"]["shouting"] = {"extra": dlg.MOOD_WORDS["shouting"], "wav": "shouting.wav",
                          "ref_text": dlg.DEFAULT_SAMPLE}
dlg.save_voice(g)
voices = {v["slug"]: v for v in dlg.list_voices()}
d, job, _s = ui.plan_render(pj, voices, "all")
check([x["id"] for x in d] == ["alice:shouting"], "the new mood is designed (got %r)" % [x["id"] for x in d])
check(job and "alice:shouting" not in job["voices"], "...and not cloned until its sample exists")
_wav(os.path.join(dlg.voices_dir(), "alice", "shouting.wav"))
voices = {v["slug"]: v for v in dlg.list_voices()}
d, job, _s = ui.plan_render(pj, voices, "all")
check(job and job["clone"][2]["voice"] == "alice:shouting", "the shouted line clones the shouting sample")
check(dlg.compose_description(voices["alice"], "shouting").count("shouting") == 1,
      "the shouting design prompt says shouting once")

print("5  character-pack voices are read-only")
cdir = os.path.join(TMP, "characters", "bighead")
_wav(os.path.join(cdir, "voice.wav"))
with open(os.path.join(cdir, "voice.json"), "w") as f:
    f.write('{"description": "a big head", "line": "Hello there.", "file": "voice.wav"}')
voices = {v["slug"]: v for v in dlg.list_voices()}
pj2 = dlg.new_project("t2", "BIGHEAD: *(shouting)* Hi.\n")
pj2["cast"] = {"BIGHEAD": "bighead"}
d, job, _s = ui.plan_render(pj2, voices, "missing")
check(d == [], "a character voice is never re-designed")
check(job and job["voices"]["bighead:neutral"]["ref_wav"].endswith("voice.wav"),
      "a character voice clones from its own voice.wav")
check(job and job["voices"]["bighead:neutral"]["ref_text"] == "Hello there.",
      "...with its own transcript as ref_text")

print("6  accents, moods, sung lines")
labels = [a for a, _w in dlg.ACCENTS]
check(any(a.startswith("Old English") for a in labels), "an Old English accent preset exists")
_gw = [a for a in labels if "George Washington" in a]
check(len(_gw) == 1, "an Early American gentleman (George Washington) preset exists")
check(_gw and "rhotic" in dict(dlg.ACCENTS)[_gw[0]] and "not British RP" in dict(dlg.ACCENTS)[_gw[0]],
      "the Washington preset says rhotic and not-RP (the two things that make it period-right)")
check(len(dlg.ACCENTS) >= 20, "at least 20 accent presets (got %d)" % len(dlg.ACCENTS))
check(len(set(labels)) == len(labels), "accent labels are unique")
oe = dict(dlg.ACCENTS)[next(a for a in labels if a.startswith("Old English"))]
vo = dlg.new_voice("Bard", accent=oe)
check("Shakespearean accent" in dlg.compose_description(vo), "the Old English words reach the prompt")
check(dlg.accent_label(oe).startswith("Old English") and dlg.accent_label("Martian") == "custom"
      and dlg.accent_label("") == "\u2014 none \u2014", "accent_label maps words back to the menu")
check(len(dlg.MOOD_WORDS) >= 20, "at least 20 moods (got %d)" % len(dlg.MOOD_WORDS))
for m in ("indignant", "solemn", "deadpan", "oratory", "nervous", "proud"):
    check(m in dlg.MOOD_WORDS and dlg.MOOD_WORDS[m], "mood %s exists with words" % m)
allm = list(dlg.MOOD_WORDS)
check(dlg.mood_of("indignantly", allm) == "indignant", "direction 'indignantly' -> indignant")
check(dlg.mood_of("gravely", allm) == "solemn", "direction 'gravely' -> solemn")
check(dlg.mood_of("addresses the crowd", allm) == "oratory", "'addresses the crowd' -> oratory")
check(dlg.mood_of("shouting", allm) == "shouting", "old moods keep precedence (shouting)")

check(dlg.is_sung({"direction": "singing", "text": "x"}), "direction 'singing' is a sung line")
check(dlg.is_sung({"direction": "", "text": "[sung] Yankee Doodle went to town"}), "[sung] tag is sung")
check(dlg.is_sung({"direction": "", "text": "\u266a Hooray"}), "\u266a prefix is sung")
check(not dlg.is_sung({"direction": "holding the receipt up to the light", "text": "x"}),
      "an ordinary direction is NOT sung")
check(not dlg.is_sung({"direction": "sings", "text": "x", "sung": False}),
      "an explicit sung=False in the row overrides the guess")
check(dlg.sung_text("[sung] \u266a Hello there \u266a") == "Hello there", "sung_text strips tags")
check(dlg.sung_lyrics("Yankee Doodle went to town, a-riding on a pony.").startswith("[Verse]\n"),
      "sung lyrics are a YuE2 section block")
check(dlg.sung_seconds("Hooray") >= 20 and dlg.sung_seconds("w " * 200) == 60,
      "sung length: 20 s floor (12 s was measured garbled) and a 60 s cap")
check("clear diction" in dlg.SUNG_STYLE, "the default sung style asks for clear diction (measured 0.99)")

pj3 = dlg.new_project("t3", "> **ALICE** — Good morning.\n"
                            "> **ALICE** — *(singing)* Row, row, row your boat.\n")
pj3["cast"] = {"ALICE": "alice"}
voices = {v["slug"]: v for v in dlg.list_voices()}
_d, job3, _s = ui.plan_render(pj3, voices, "missing")
check(job3 and [c["_line"] for c in job3["clone"]] == [0],
      "the SPEAKING batch leaves the sung line out")
sv = ui.plan_sung(pj3, voices, "missing")
check([x["_line"] for x in sv] == [1], "plan_sung picks exactly the sung line")
check(sv and "male vocal" in sv[0]["style"] and sv[0]["style"].startswith(dlg.SUNG_STYLE),
      "sung style = project music + the voice's singer (got %r)" % (sv[0]["style"] if sv else None))
g2 = dlg.load_voice("alice"); g2["singing"] = "warm baritone folk singer"; dlg.save_voice(g2)
voices = {v["slug"]: v for v in dlg.list_voices()}
sv = ui.plan_sung(pj3, voices, "missing")
check(sv and sv[0]["style"].endswith("warm baritone folk singer"), "the voice's SINGING words steer it")
pj3["lines"][1]["takes"] = [{"file": "x"}]
check(ui.plan_sung(pj3, voices, "missing") == [] and len(ui.plan_sung(pj3, voices, "all")) == 1,
      "MISSING skips a sung line with a take; ALL re-sings")
check(ui.sung_estimate_seconds([{"seconds": 20.0}]) > 10, "sung estimate uses the YuE2 cost law")

print("\n%d checks, %d failures" % (N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
