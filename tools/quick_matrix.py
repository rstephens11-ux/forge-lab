#!/usr/bin/env python3
"""GPU-free checks for quick.py — QUICK mode, one idea -> a finished video.

The rig is faked (stills, sheet edits, voices, video clips are real tiny files made with
ffmpeg), the writer is faked; everything in between is the real pipeline: Characters,
Voices, Dialogue, Episode, the set, the cut. Checks the order, the hand-offs, the review
pauses, resume after an error, and that every product lands where Studio can open it.
    python3 tools/quick_matrix.py"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
_TMP = tempfile.mkdtemp(prefix="quick-")
os.environ["FORGE_LAB_DATA"] = _TMP
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import dialogue as dlg  # noqa: E402
import formats  # noqa: E402
import quick  # noqa: E402
import scene_engine as se  # noqa: E402
import studio_pipeline as sp  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def ff(args, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + args + [out], check=True)
    return out


# ---------------------------------------------------------------- the fake rig
SUB = []
RAISE = {}


def fake_submit(kind, p, refs=None):
    if RAISE.get(kind):
        raise RAISE.pop(kind)
    SUB.append((kind, dict(p), list(refs or [])))
    pid = "qk%03d" % len(SUB)
    d = os.path.join(fc.RUNS, pid)
    if kind == "video":
        # a MOVING picture as long as was asked (the take check flags frozen / short clips)
        _d = max(1.0, float(p.get("length") or 24) / 24.0)
        ff(["-f", "lavfi", "-i", "testsrc=s=320x180:r=24:d=%.3f" % _d, "-f", "lavfi", "-i",
            "sine=frequency=200:d=%.3f" % _d, "-shortest", "-pix_fmt", "yuv420p"],
           os.path.join(d, "clip.mp4"))
    else:
        ff(["-f", "lavfi", "-i", "color=c=0x806040:s=96x160", "-frames:v", "1"],
           os.path.join(d, "o.png"))
    return pid, 4242


def fake_wait(pid, timeout=1800, **kw):
    d = os.path.join(fc.RUNS, pid)
    return {"id": pid, "images": [{"file": f} for f in sorted(os.listdir(d))]}


fc.submit = fake_submit
fc.wait_and_collect = fake_wait
sp.fl._index_update = lambda pid, **kw: None
SAID = {}


def fake_tb(job, out_dir, language="English", engine="qwen"):
    items = []
    for it in job["design"] + job["clone"]:
        o = ff(["-f", "lavfi", "-i", "sine=frequency=300", "-t", "1.2"],
               os.path.join(out_dir, it["id"].replace(":", "_") + ".wav"))
        SAID[os.path.basename(o)] = it.get("text", "")
        items.append({"id": it["id"], "out": o, "duration": 1.2})
    return {"ok": True, "items": items}


se.tts_batch = fake_tb
se.transcribe_many = lambda paths, language="English": ["" for _ in paths]
sp.finish = lambda i_, a_, o_, **k: (open(a_).read(), __import__("shutil").copyfile(i_, o_))
sp.finish_plain = lambda i_, o_, **k: __import__("shutil").copyfile(i_, o_)


class FakeDirector:
    def __init__(self):
        self.calls = []
        self.n = 0

    def load(self):
        return {}

    def _chat(self, system, user, schema, temperature=1.0, model=None):
        if schema is quick.WHERE_SCHEMA:
            self.calls.append(("where", user))
            return {"where": "a busy city sidewalk on a grey afternoon"
                    if "city street" in user.split("\n")[0] else ""}
        if schema is quick.ROLES_SCHEMA:
            self.calls.append(("roles", user))
            return {"roles": ["a nervous weatherman", "a proud conductor", "a spare"]}
        if schema is quick.JUDGE_SCHEMA:
            self.calls.append(("judge", user))
            return {"best": 2, "why": "it stays on the soup"}
        wr = quick.writers_room
        if schema is wr.GAME_SCHEMA:
            self.calls.append(("game", user))
            return {"games": [{"odd_one": "A", "straight_one": "B", "game": "soup is weather",
                               "steps": ["s1", "s2", "s3"], "button": "That's the cold front."}
                              for _ in range(3)], "best": 2}
        if schema is wr.DRAFT_SCHEMA:
            names = re.search(r"MUST be one of: ([A-Z ,]+)\)", user).group(1).split(", ")
            self.calls.append(("draft", user))
            a, b = names[0], names[-1]
            k = len([c_ for c_ in self.calls if c_[0] == "draft"])
            return {"title": "Soup Front", "hook": "Good evening.",
                    "situation": "a weatherman blames the soup on the weather",
                    "joke": "soup is weather", "escalations": ["e1", "e2", "e3"],
                    "button": "That's the cold front.", "lines": [
                        {"speaker": a, "direction": "", "text": "Good evening."},
                        {"speaker": b, "direction": "flat", "text": "Your ears are in my soup."},
                        {"speaker": a, "direction": "", "text": "That's the cold front."}]}
        if schema is wr.JUDGE_SCHEMA:
            self.calls.append(("rubric", user))
            return {k_: 3 for k_ in wr.RUBRIC} | {"weakest_line": 2, "total": 18}
        if schema is wr.COLD_SCHEMA:
            self.calls.append(("cold", user))
            return {"what_happens": "soup", "confusing": [], "makes_sense": 5}
        if schema is wr.PUNCH_SCHEMA:
            self.calls.append(("punch", user))
            return {"options": ["alt %d" % i for i in range(6)]}
        if schema is wr.PICK_SCHEMA:
            self.calls.append(("pick", user))
            return {"best": 1, "why": "the original lands"}
        if schema is quick.HOOK_SCHEMA:
            self.calls.append(("hook", user))
            return {"hook_text": "#He blames the soup on the weather, obviously, today"}
        if schema is quick.ACTING_SCHEMA:
            self.calls.append(("acting", user))
            n = user.count("\n") + 1
            return {"lines": [{"gesture": "points at the soup %d" % i,
                               "reaction": "frowns %d" % i} for i in range(12)]}
        self.calls.append(("person" if schema is quick.weirdos.PERSON_SCHEMA else "invent",
                           user))
        self.n += 1
        return {"name": ["Clarence Holt", "Gustav Brandt"][(self.n - 1) % 2],
                "subject": ["a local weather presenter", "a retired orchestra conductor"][
                    (self.n - 1) % 2], "pronoun": "he", "look": "in his mid-30s",
                "feature": ["ears so large they drag below his chin",
                            "a wild silver mane of hair"][(self.n - 1) % 2],
                "wardrobe": "a navy blazer", "setting": "a TV studio",
                "action": "nods slowly", "catchphrase": "Expect delays.",
                "hook": "it loops", "who": "He is very serious. He never notices.",
                "voice": "a breathy baritone with calm urgency"}

    def dialogue(self, brief, cast, seconds=30.0, model=None):
        self.calls.append(("dialogue", brief, dict(cast), seconds))
        a, b = sorted(cast)[0], sorted(cast)[-1]
        return {"title": "Soup Front", "lines": [
            {"speaker": a, "direction": "", "text": "Good evening."},
            {"speaker": b, "direction": "flat", "text": "Your ears are in my soup."},
            {"speaker": a, "direction": "", "text": "That's the cold front."}]}


print("§1 formats")
check(len(formats.FORMATS) >= 6 and len({f["key"] for f in formats.FORMATS}) ==
      len(formats.FORMATS), "%d formats, unique keys" % len(formats.FORMATS))
# (three: the gilded satire's lord / petitioner / enabler - Quick takes up to three)
check(all(f["aspect"] in formats.ASPECTS and f["cast"] in (1, 2, 3) and f["shape"]
          for f in formats.FORMATS), "each has a shape, a cast size and an aspect")
b = formats.brief("weather", "a heatwave of opinions")
check("weather forecast" in b and "a heatwave of opinions" in b,
      "the writer's brief = the format's shape + the idea")

print("§2 one sentence -> a finished video (nothing to pick)")
d = FakeDirector()
check(all(f_["aspect"] == "9:16" for f_ in formats.FORMATS) and
      quick.new_job("x", "sketch")["aspect"] == "9:16",
      "every format starts TALL (phones first); wide is one click away")
job = quick.new_job("a weatherman and a conductor argue about soup", "sketch", seconds=8,
                    aspect="16:9", weirdos=True)
est0 = quick.estimate(job)
check(est0 > 600, "it quotes the whole run up front (~%d s)" % est0)
said = []
job = quick.run(job, director=d, progress=said.append)
check(job["state"] == "done" and os.path.exists(job["final"] or ""),
      "state done, the video exists (%s)" % job.get("error"))
check(len(job["cast"]) == 2 and all(fc.read_character(s) for s in job["cast"]),
      "two characters invented and SAVED as real Characters")
m0 = fc.read_character(job["cast"][0])
check(m0.get("feature") and m0.get("role") == "a nervous weatherman" and
      (m0.get("picks") or {}).get("Subject", "").startswith("a nervous weatherman") and
      fc.character_still(job["cast"][0]),
      "...with a still, their gag and filled pick boxes, playing the idea's ROLE "
      "(editable in Characters)")
check(all((fc.read_character(s) or {}).get("voice_slug") for s in job["cast"]),
      "each got a voice, linked (editable in Voices)")
dl = [c_ for c_ in d.calls if c_[0] == "draft"][0]
check("comedy sketch" in dl[1] and "argue about soup" in dl[1] and "ears so large" in dl[1]
      and "THE IDEA COMES FIRST" in dl[1] and "- situation:" in dl[1] and
      "HOW TO WRITE IT" not in dl[1] and "THE GAME" not in dl[1],
      "the writer got the format's shape, the idea (which wins over the format), each "
      "character's gag, and wrote PLAN FIRST (the bake-off prompt, no CRAFT block)")
check([c_[0] for c_ in d.calls].count("draft") == 3 and
      [c_[0] for c_ in d.calls].count("rubric") == 3 and "punch" in [c_[0] for c_ in d.calls]
      and "game" not in [c_[0] for c_ in d.calls] and
      job.get("room", {}).get("plan", "").endswith("soup is weather"),
      "the writers' room ran: 3 plan-first drafts, each scored alone, a punch-up (no game "
      "step)")
dp = dlg.load_project(job["dialogue"])
check(dp and len(dp["lines"]) == 3 and not se.missing(dp),
      "the script is a Dialogue scene, every line read (openable in Dialogue)")
ep = sp.load_project(job["episode"])
check(ep and ep["aspect"] == "16:9" and ep.get("captions") is True and
      all(sh.get("video_mode") == "set" and sh.get("chosen") for sh in ep["shots"]),
      "the video is an Episode: 16:9, captions on, all shots on THE SET, all made")
check(len(job.get("acting") or []) >= 3 and
      "points at the soup 0" in ep["shots"][0]["prompt"] and
      "frowns 0" in ep["shots"][0]["prompt"] and "points at the soup 1" in ep["shots"][1]["prompt"],
      "the writer directs one gesture + one reaction per line; each shot's prompt carries it")
check(job.get("hook_text") == "He blames the soup on the weather," and
      ep.get("hook_text") == job["hook_text"] and
      "must NOT contain any joke" in [c_ for c_ in d.calls if c_[0] == "hook"][0][1],
      "a HOOK line is written (7 words max, no hashtags, the premise - not a joke) and "
      "reaches the Episode")
_jh = quick.new_job("x", "sketch", hook=False)
check(_jh["hook"] is False, "...and can be switched off")
_rx = [sh for sh in ep["shots"] if sh.get("is_reaction")]
check(len(_rx) == 2 and {sh["speaker"] for sh in _rx} == set(job["cast"]) and
      all("mouth stays CLOSED" in sh["prompt"] for sh in _rx) and
      quick.estimate(dict(job, steps={s_: "todo" for s_ in quick.STEPS})) >
      quick.estimate(dict(job, reactions=False, steps={s_: "todo" for s_ in quick.STEPS})),
      "REACTION shots: one silent listening clip per character, counted in the estimate")
check(job.get("captions") == "pop" and ep.get("caption_look") == "pop",
      "CAPTIONS defaults to Pop (the word being said lights up)")
_jn = quick.new_job("x", "sketch", captions="none")
_jb = quick.new_job("x", "sketch", captions="big")
_jw = quick.new_job("x", "sketch", captions="sparkly")
check(_jn["captions"] == "none" and _jb["captions"] == "big" and _jw["captions"] == "pop",
      "CAPTIONS None / Big are kept on the job; an unknown look falls back to Pop")
check(job["pace"] == "snappy" and ep.get("pace") == "snappy" and
      quick.new_job("x", "confessional")["pace"] == "breathe" and
      quick.new_job("x", "sketch", pace="natural")["pace"] == "natural" and
      quick.new_job("x", "sketch", pace="zz")["pace"] == "snappy",
      "PACE: each format has its own (sketch snappy, confessional lets pauses breathe); "
      "picking one overrides it; it reaches the Episode")
kinds = [k for k, _p, _r in SUB]
check(kinds.index("t2i") < kinds.index("edit") < kinds.index("video") and
      kinds.count("video") == 5, "order: stills, then the set, then 3 shots + 2 reaction "
      "shots (%s)"
      % "".join(k[0] for k in kinds))
check(any("inventing" in s for s in said) and any(s.startswith("shooting") for s in said),
      "progress is reported step by step")

print("§2b MORE LIKE THIS: re-shoot in Final, another script")
SUB.clear()
nd0 = len(d.calls)
jf = quick.again(job, "final")
check(quick.estimate(jf) < quick.estimate(dict(jf, steps={s_: "todo" for s_ in quick.STEPS})),
      "re-shoot estimate counts only the shots and the cut")
jf = quick.run(jf, director=d)
epf = sp.load_project(jf["episode"])
vids = [p_ for k_, p_, _r in SUB if k_ == "video"]
check(jf["state"] == "done" and jf["quality"] == "Final" and epf["quality"] == "Final" and
      epf["slug"] != ep["slug"] and jf["episode"].endswith("-final"),
      "Re-shoot in Final: a new Final episode next to the Draft (%s)" % jf.get("error"))
check(len(vids) == 5 and all((p_["width"], p_["height"]) == sp.CANVAS_WIDE["Final"]
                             for p_ in vids),
      "...every shot re-rendered at the Final canvas")
check(len(d.calls) == nd0 and not [k_ for k_, _p, _r in SUB if k_ != "video"] and
      jf["dialogue"] == job["dialogue"],
      "...same script, same voices, same set: no writer, no stills, no plate rebuilt")
check(os.path.exists(job["final"]) and os.path.exists(jf["final"]) and
      jf["final"] != job["final"], "...the Draft video is kept")
bad = ""
try:
    quick.again(jf, "final")
except ValueError as exc:
    bad = str(exc)
check("already Final" in bad, "re-shooting a Final -> a clear message")
SUB.clear()
nd0 = len(d.calls)
js = quick.again(job, "script")
js = quick.run(js, director=d)
check(js["state"] == "done" and js["cast"] == job["cast"] and js["dialogue"] != job["dialogue"]
      and not [c_ for c_ in d.calls[nd0:] if c_[0] == "invent"] and
      [c_ for c_ in d.calls[nd0:] if c_[0] == "draft"],
      "Another script: same characters (none invented), a new script, a new video")
check(js["idea"] == job["idea"] and js["captions"] == job["captions"] and
      js["aspect"] == job["aspect"], "...same idea, shape and captions")


print("§2c show me the set first")
SUB.clear()
import places as _pl
_pimg = ff(["-f", "lavfi", "-i", "color=c=0x335577:s=704x1280", "-frames:v", "1"],
           os.path.join(_TMP, "place.png"))
_pslug = _pl.keep("Lamp room", "a lighthouse lamp room", _pimg, "9:16")
jc = quick.new_job("they argue about rain", "sketch", seconds=8, cast=job["cast"],
                   check_set=True, place=_pslug)
jc = quick.run(jc, director=d)
check(jc["state"] == "review:episode" and not [k_ for k_, _p, _r in SUB if k_ == "video"]
      and len([k_ for k_, _p, _r in SUB if k_ == "edit"]) == 4,
      "'Show me the set' stops after the set (2 passes + 2 close-ups), before any video")
_e1 = [r_ for k_, _p, r_ in SUB if k_ == "edit"][0]
check(sp.load_project(jc["episode"]).get("place") == _pslug and _e1[-1] == _pl.image(_pslug),
      "PLACE: the job's kept place reaches the Episode; the set is built in that picture")
quick.redo_set(jc, jc["cast"][1])
jc = quick.run(jc, director=d)
check(jc["state"] == "review:episode" and
      len([k_ for k_, _p, _r in SUB if k_ == "edit"]) == 5,
      "redo one close-up -> one more picture, then it asks again")
quick.redo_set(jc, "master")
jc = quick.run(jc, director=d)
check(len([k_ for k_, _p, _r in SUB if k_ == "edit"]) == 9, "rebuild the whole set -> 4 more")
jc = quick.run(jc, director=d, go_on=True)
check(jc["state"] == "done" and len([k_ for k_, _p, _r in SUB if k_ == "video"]) == 5,
      "looks right -> shoot it -> done")
jn_ = quick.new_job("x", "sketch")
check(not jn_["check_set"], "off unless asked (the window ticks it by default)")


class PostDir(FakeDirector):
    def post_copy(self, ch, script):
        return {"title": "Feelings Forecast", "caption": "he's not wrong",
                "hashtags": ["weather", "comedy"]}


sp.get_director = lambda: PostDir()
print("§3 your own cast, vertical, check each step")
SUB.clear()
d2 = FakeDirector()
j2 = quick.new_job("he forecasts feelings", "weather", cast=[job["cast"][0]], check=True)
check(j2["aspect"] == "9:16" or formats.get("weather")["aspect"] == j2["aspect"],
      "the format sets the shape unless you choose one")
j2 = quick.new_job("he forecasts feelings", "weather", cast=[job["cast"][0]], check=True,
                   aspect="9:16")
j2 = quick.run(j2, director=d2)
check(j2["state"] == "review:voices" and not [c_ for c_ in d2.calls if c_[0] == "invent"],
      "uses the character you picked (no invention) and stops to show the cast + voice")
check(not SUB, "...nothing rendered yet (he already had a still and a voice)")
j2 = quick.run(j2, director=d2, go_on=True)
check(j2["state"] == "review:script" and quick.script_text(j2).count(":") >= 3,
      "continue -> stops again to show the script")
quick.set_script(j2, quick.script_text(j2).replace("Good evening.",
                                                   "Good evening, feelings fans."))
j2 = quick.run(j2, director=d2, go_on=True)
check(j2["state"] == "review:episode", "check each step: it also stops at the set")
j2 = quick.run(j2, director=d2, go_on=True)
dp2 = dlg.load_project(j2["dialogue"])
check(j2["state"] == "done" and dp2["lines"][0]["text"] == "Good evening, feelings fans.",
      "an edit at the review is what gets read and shot")
ep2 = sp.load_project(j2["episode"])
check(ep2["aspect"] == "9:16" and ep2.get("where") == formats.get("weather")["where"] and
      all(sh["video_mode"] == "set" for sh in ep2["shots"]),
      "vertical; one presenter still gets the format's set (the weather studio)")
_nl = len(dp2["lines"])
check(len(ep2["shots"]) < _nl and any(sh.get("long_take") for sh in ep2["shots"]) and
      len([k for k, _p, _r in SUB if k == "video"]) == len(ep2["shots"]),
      "one presenter: %d lines shot as %d long take(s), not one clip per line"
      % (_nl, len(ep2["shots"])))
check(quick.new_job("x", "weather", long_takes=False)["long_takes"] is False and
      "long_takes" in quick._CARRY, "Long takes can be turned off, and a copy keeps it")
ed = [p_ for k, p_, _r in SUB if k == "edit"][0]
check("#weather #comedy #aigenerated" in quick.post_text(j2) and
      "Feelings Forecast" in quick.post_text(j2),
      "a vertical cut comes with a caption + hashtags ready to post")
check(quick.post_text(job) == "", "a 16:9 episode has none (it isn't a short)")
check(ed["height"] > ed["width"] and "weather map" in ed["prompt"],
      "the set plate is TALL for 9:16 and shows the format's place (%dx%d)"
      % (ed["width"], ed["height"]))

print("§4 an error stops it; run again resumes where it stopped")
SUB.clear()
j3 = quick.new_job("a podcast about naps", "podcast", cast=job["cast"], seconds=8)
RAISE["video"] = RuntimeError("card busy")
j3 = quick.run(j3, director=FakeDirector())
check(j3["state"] == "error" and "card busy" in j3["error"] and
      j3["steps"]["shots"] == "todo" and j3["steps"]["lines"] == "done",
      "a failed shot stops the run with the reason; earlier steps stay done")
check(isinstance(quick.LAST_EXC.get(j3["id"]), RuntimeError),
      "the exception is kept for the window (a busy card -> the card prompt)")
n_before = len(SUB)
j3 = quick.run(quick.load(j3["id"]), director=FakeDirector())
check(j3["state"] == "done" and not [k for k, _p, _r in SUB[n_before:] if k == "t2i"],
      "run again: it picks up at the shots - no new stills, voices or script")
check(json.load(open(quick._path(j3["id"])))["state"] == "done" and
      len(quick.list_jobs()) >= 3, "jobs are saved and listed")

print("§5 lock their looks (optional)")
SUB.clear()
j4 = quick.new_job("x", "vlog", cast=[job["cast"][1]], looks=True, seconds=5)
check(j4["steps"]["looks"] == "todo" and quick.new_job("x", "vlog")["steps"]["looks"] ==
      "skip", "the sheet step is on only when asked")
j4 = quick.run(j4, director=FakeDirector())
import charpack  # noqa: E402
check(j4["state"] == "done" and charpack.load_sheet(job["cast"][1]),
      "Lock their looks builds the character sheet before shooting (%s)" % j4.get("error"))

print("§7 music video: lyrics -> a song -> sung shots that stay in sync")
SUB.clear()
d7 = FakeDirector()
d7.lyrics = lambda brief, style="", seconds=120.0, model=None: (
    d7.calls.append(("lyrics", brief, style, seconds)) or
    {"title": "Cold Front", "lyrics": "[Verse]\nears in the wind\n[Chorus]\ncold front"})
_real_fs = fc.submit


def song_submit(kind, p, refs=None):
    if kind != "song":
        return _real_fs(kind, p, refs)
    SUB.append((kind, dict(p), []))
    pid = "song%03d" % len(SUB)
    ff(["-f", "lavfi", "-i", "sine=frequency=440", "-t", "14"],
       os.path.join(fc.RUNS, pid, "yue2_00001.flac"))
    return pid, 9


fc.submit = song_submit
j7 = quick.new_job("a ballad about his ears", "music", cast=[job["cast"][0]], seconds=12)
check(j7["steps"]["voices"] == "skip" and j7["sound"] == "Alt-girl future R&B" and
      quick.labels(j7)["lines"] == "Recording the song",
      "a music video skips Voices (it's sung), uses the format's sound, says 'Recording the song'")
j7 = quick.run(j7, director=d7)
check(j7["state"] == "done" and os.path.exists(j7.get("final") or ""),
      "lyrics -> song -> shots -> cut: a finished music video (%s)" % j7.get("error"))
ly = [c_ for c_ in d7.calls if c_[0] == "lyrics"][0]
check("ears in the wind" in j7["lyrics"] and "ballad about his ears" in ly[1] and
      "female vocal" not in ly[2] and ly[3] == 12,
      "the lyrics know the idea and the singer; the style's vocal matches him (male)")
sp_ = [p_ for k, p_, _r in SUB if k == "song"][0]
check(sp_["lyrics"] == j7["lyrics"] and sp_["max_duration"] == 12,
      "the Song engine sings THOSE lyrics for the asked length")
ep7 = sp.load_project(j7["episode"])
sl = quick.music_slices(12)
full = fc.h3_frames(5) / 24.0
check(len(ep7["shots"]) == len(sl) == 3 and abs(sl[1][0] - full) < 1e-3 and
      abs(sum(x[1] for x in sl) - 12) < 1e-3,
      "12 s -> 3 shots; each slice is exactly one H3 5-s clip long (%.3f s), so no drift"
      % full)
check(all(sh["intent"] == "lipsync" and os.path.exists(sh["audio_wav"]) and
          sh["video_mode"] == "set" for sh in ep7["shots"]) and ep7["music_video"] and
      ep7.get("captions") is False,
      "every shot lip-syncs to its slice of the song, on the set; no captions")
cams = [sh["prompt"] for sh in ep7["shots"]]
check(len({quick.CAMERA_CYCLE.index(next(c_ for c_ in quick.CAMERA_CYCLE if c_ in p_))
           for p_ in cams}) == 3, "each shot gets the next camera move")
sing = sp.single_prompt(ep7, job["cast"][0], [job["cast"][0]])
check("shoulder" not in sing, "a one-person set has no stranger's shoulder in its close-up")
fc.submit = _real_fs

print("§7b a music video of a song you already made")
mine = ff(["-f", "lavfi", "-i", "sine=frequency=500", "-t", "9"], os.path.join(_TMP, "my.flac"))
SUB.clear()
j7b = quick.new_job("", "music", cast=[job["cast"][0]], song=mine, lyrics="[Verse]\nmine",
                    title="My Song")
check(j7b["steps"]["script"] == "done" and j7b["steps"]["lines"] == "done" and
      j7b["title"] == "My Song" and j7b["seconds"] == 9 and os.path.exists(j7b["song"]),
      "your song: no lyrics or song step; the length is the song's (9 s)")
j7b = quick.run(j7b, director=FakeDirector())
check(j7b["state"] == "done" and not [k for k, _p, _r in SUB if k == "song"] and
      len(sp.load_project(j7b["episode"])["shots"]) == 2,
      "-> a music video of it, nothing re-sung (%s)" % j7b.get("error"))

print("§8 the script fits the length")
class LongDirector(FakeDirector):
    def dialogue(self, brief, cast, seconds=30.0, model=None):
        self.calls.append(("dialogue", brief, dict(cast), seconds))
        a, b = sorted(cast)[0], sorted(cast)[-1]
        long = " ".join(["word"] * 18)
        if "far too long" not in brief:
            return {"title": "Long", "lines": [{"speaker": a if i % 2 else b, "direction": "",
                                                "text": long} for i in range(6)]}
        return {"title": "Short", "lines": [{"speaker": a, "direction": "", "text": "Soup."},
                                            {"speaker": b, "direction": "", "text": "No."}]}


quick.ROOM = False                                # §8/§8b: the one-shot writer (fallback)
quick.DRAFTS = 1                                  # §8 is about length, one draft at a time
d8 = LongDirector()
j8 = quick.new_job("x", "sketch", cast=job["cast"], seconds=15)
j8["cast"] = list(job["cast"])
quick.step_script(j8, d8, lambda m: None)
b8 = [c_ for c_ in d8.calls if c_[0] == "dialogue"]
check("LENGTH:" in b8[0][1] and "28 words" in b8[0][1],
      "the writer is given a word budget for the length (15 s -> ~28 words, edit time out)")
check(len(b8) == 2 and "far too long" in b8[1][1] and j8["title"] == "Short",
      "a draft way over budget gets ONE rewrite asking for the cut")
d9 = FakeDirector()
j9 = quick.new_job("x", "sketch", cast=job["cast"], seconds=15)
j9["cast"] = list(job["cast"])
quick.step_script(j9, d9, lambda m: None)
check(len([c_ for c_ in d9.calls if c_[0] == "dialogue"]) == 1,
      "a draft within budget is used as is")
quick.DRAFTS = 2

print("§8b two drafts, the funnier one kept; characters play the idea's roles")
class TwoDrafts(FakeDirector):
    def dialogue(self, brief, cast, seconds=30.0, model=None):
        self.calls.append(("dialogue", brief, dict(cast), seconds))
        a, b = sorted(cast)[0], sorted(cast)[-1]
        k = len([c_ for c_ in self.calls if c_[0] == "dialogue"])
        return {"title": "Draft %d" % k, "lines": [
            {"speaker": a, "direction": "", "text": "Draft %d line one." % k},
            {"speaker": b, "direction": "", "text": "Draft %d line two." % k}]}
d11 = TwoDrafts()
j11 = quick.new_job("x", "sketch", cast=job["cast"], seconds=15)
j11["cast"] = list(job["cast"])
quick.step_script(j11, d11, lambda m: None)
jd = [c_ for c_ in d11.calls if c_[0] == "judge"]
check(len([c_ for c_ in d11.calls if c_[0] == "dialogue"]) == 2 and len(jd) == 1 and
      "DRAFT 1:" in jd[0][1] and "DRAFT 2:" in jd[0][1] and j11["title"] == "Draft 2",
      "two drafts are written, the editor sees both, the one it picks is shot")
_dl11 = [c_ for c_ in d11.calls if c_[0] == "dialogue"][0]
check("Each line is a direct reply to the line before it" in _dl11[1] and
      "THE BUTTON. The last line is the biggest laugh" in _dl11[1],
      "the writer is told to stay on the idea, escalate and land the last line")
class BadJudge(TwoDrafts):
    def _chat(self, system, user, schema, temperature=1.0, model=None):
        if schema is quick.JUDGE_SCHEMA:
            raise RuntimeError("writer fell over")
        return TwoDrafts._chat(self, system, user, schema, temperature, model)
j12 = quick.new_job("x", "sketch", cast=job["cast"], seconds=15)
j12["cast"] = list(job["cast"])
quick.step_script(j12, BadJudge(), lambda m: None)
check(j12["title"] == "Draft 1", "the editor fails -> the first draft, not a crash")
d13 = FakeDirector()
SUB.clear()
j13 = quick.new_job("a nervous weatherman and a proud conductor argue", "sketch", seconds=8,
                    weirdos=True)
quick.step_cast(j13, d13, lambda m: None)
inv = [c_[1] for c_ in d13.calls if c_[0] == "invent"]
check([c_[0] for c_ in d13.calls][:1] == ["roles"] and len(inv) == 2 and
      "a nervous weatherman" in inv[0] and "a proud conductor" in inv[1] and
      "DIFFERENT body part" in inv[0] and j13["roles"] == ["a nervous weatherman",
                                                            "a proud conductor"],
      "the idea's roles are read first; each invented character plays its own role "
      "(asked for a lighthouse keeper, the first run got a fog analyst)")
# ORDINARY PEOPLE unless asked (Ryan: "only if specified or generated using the weirdo
# button")
d14 = FakeDirector()
SUB.clear()
j14 = quick.new_job("one old man talking to a middle aged woman on a city street", "sketch",
                    seconds=8)
quick.step_cast(j14, d14, lambda m: None)
per = [c_[1] for c_ in d14.calls if c_[0] == "person"]
m14 = [fc.read_character(s_) for s_ in j14["cast"]]
st14 = [p_["prompt"] for k_, p_, _r in SUB if k_ == "t2i"]
check(not j14["weirdos"] and len(per) == 2 and not [c_ for c_ in d14.calls
                                                     if c_[0] == "invent"] and
      all(m_.get("feature") == "" and m_.get("kind") == "person" for m_ in m14) and
      "DIFFERENT body part" not in per[0],
      "a plain idea -> two ORDINARY people (no gag), cast by the people writer")
check(len(st14) == 2 and all("Normal human proportions" in p_ and "exaggerated far past"
                             not in p_ for p_ in st14),
      "...drawn as ordinary people (normal proportions, no exaggerated feature)")
check(quick.wants_weirdos({"idea": "two weirdos argue about soup"}) and
      quick.wants_weirdos({"idea": "a man with huge ears sells hats"}) and
      quick.wants_weirdos({"idea": "x", "weirdos": True}) and
      not quick.wants_weirdos({"idea": "a giant lighthouse at night"}) and
      not quick.wants_weirdos({"idea": "a long day at the office"}) and
      "weirdos" in quick._CARRY,
      "weirdos only when the box is ticked or the idea asks (huge ears, a weirdo), never "
      "for 'a giant lighthouse'")
# a busy card while drawing character 2: character 1 is kept, only 2 is redone
d16 = FakeDirector()
j16 = quick.new_job("an old man and a young woman", "sketch", seconds=8)
_t2i = {"n": 0}
_fs16 = fc.submit


def _busy_2nd_still(kind, p, refs=None):
    if kind == "t2i":
        _t2i["n"] += 1
        if _t2i["n"] == 2:
            raise RuntimeError("card busy")
    return _fs16(kind, p, refs)


fc.submit = _busy_2nd_still
try:
    quick.step_cast(j16, d16, lambda m: None)
except RuntimeError:
    pass
fc.submit = _fs16
_first16 = list(j16.get("invented") or [])
_saved16 = json.load(open(quick._path(j16["id"]))).get("invented")
quick.step_cast(j16, d16, lambda m: None)
check(len(_first16) == 1 and _saved16 == _first16 and len(j16["cast"]) == 2 and
      j16["cast"][0] == _first16[0] and
      len([c_ for c_ in d16.calls if c_[0] == "person"]) == 3,
      "a busy card on character 2: character 1 was saved to the job; the retry invents only "
      "character 2")
_src = dict(quick.new_job("x", "sketch", reactions=False, hook=False), cast=["a"],
            state="done")
_ag = quick.again(_src, "script")
check(_ag["reactions"] is False and _ag["hook"] is False,
      "Another script keeps the options you turned off (reactions, hook)")
_ids = {quick.new_job("x", "sketch")["id"] for _ in range(60)}
check(len(_ids) == 60, "60 jobs made in the same second get 60 different ids")
check(all(formats.get(k_)["key"] == k_ for k_ in ("doorbell", "nature", "ifvlog")) and
      formats.get("nature")["cast"] == 2 and "NARRATOR" in formats.get("nature")["shape"] and
      formats.get("doorbell")["look"] == "cctv" and
      "IS that thing" in formats.get("ifvlog")["shape"],
      "three new formats: doorbell camera, nature documentary (a narrator + a 'specimen'), "
      "if ___ had a vlog")
j17 = quick.run(quick.new_job("he wants to borrow your ladder at 2am", "doorbell",
                              cast=[job["cast"][0]], seconds=8), director=FakeDirector())
ep17 = sp.load_project(j17["episode"])
_ass17 = open(os.path.join(sp.project_dir(ep17["slug"]), "cut", "captions.ass")).read()
check(j17["state"] == "done" and ep17.get("post_look") == "cctv" and
      "FRONT DOOR" in _ass17 and "porch" in (ep17.get("where") or ""),
      "a doorbell video: shot on the porch, the camera picture and label in the cut (%s)"
      % j17.get("error"))
j18 = quick.run(quick.new_job("an old man and a woman argue on a city street", "sketch",
                              cast=list(job["cast"]), seconds=8), director=FakeDirector())
ep18 = sp.load_project(j18["episode"])
_ed18 = [p_["prompt"] for k_, p_, _r in SUB if k_ == "edit"]
check(j18["state"] == "done" and ep18.get("where") == "a busy city sidewalk on a grey afternoon"
      and any("city sidewalk" in p_ for p_ in _ed18[-4:]),
      "the place the idea names is where the set is built (the city-street run was shot "
      "indoors)")
check(quick.idea_where(FakeDirector(), "two men argue about soup") == "" and
      quick.idea_where(type("X", (), {"_chat": lambda *a, **k: 1 / 0})(), "x") == "",
      "no place in the idea (or no writer) -> the format's usual place")
import music as _music  # noqa: E402
_mp = os.path.join(fc.DATA if hasattr(fc, "DATA") else os.environ["FORGE_LAB_DATA"],
                   "bed.mp3")
ff(["-f", "lavfi", "-i", "sine=frequency=262:d=4"], _mp)
_ms = _music.add_file(_mp, "Bed")
j19 = quick.run(quick.new_job("he reviews spoons", "vlog", cast=[job["cast"][0]], seconds=8,
                              music=_ms), director=FakeDirector())
ep19 = sp.load_project(j19["episode"])
check(j19["state"] == "done" and ep19.get("music") == _music.path(_ms) and
      os.path.exists(os.path.join(sp.project_dir(ep19["slug"]), "cut", "with_music.mp4")) and
      quick.again(j19, "script")["music"] == _ms,
      "MUSIC: a song from the library plays under the video (and Another script keeps it)")
check(quick.guess_format("a weatherman who forecasts feelings") == "weather" and
      quick.guess_format("if the grim reaper had a vlog") == "ifvlog" and
      quick.guess_format("caught on the doorbell camera at 3am") == "doorbell" and
      quick.guess_format("two cops argue about soup") is None and
      quick.guess_seconds("a 15-second sketch") == 15 and
      quick.guess_seconds("one minute of chaos") == 60 and
      quick.guess_seconds("two cops argue") is None,
      "the idea's own format and length are read (weatherman -> Weather report, 15-second)")
_rp = quick.weirdos.roll_person()
check(_rp["feature"] == "" and "Normal human proportions" in quick.weirdos.compose(_rp),
      "no writer -> an ordinary person from the built-in list, not a weirdo")
check(quick.idea_roles(d13, "", 2) == [] and
      quick.idea_roles(type("X", (), {"_chat": lambda *a, **k: 1 / 0})(), "x", 2) == [],
      "no idea, or no writer -> no roles, and inventing carries on as before")

print("§9 give me ideas; Draft / Final")
d10 = FakeDirector()
d10.pitch = lambda ch, n=5, length_s=20, brief="": (
    d10.calls.append(("pitch", ch, n, length_s, brief)) or
    [{"title": "t%d" % i, "logline": "idea %d" % i, "hook": "h"} for i in range(n)])
got = quick.ideas(d10, "weather", [job["cast"][0]], 3, 20)
pc = [c_ for c_ in d10.calls if c_[0] == "pitch"][0]
check(got == ["idea 0", "idea 1", "idea 2"] and pc[2] == 3 and "Weather report" in pc[4] and
      "ears so large" in pc[4] and pc[1].get("name") == "Clarence Holt",
      "3 one-line ideas, written for THIS format and THIS character's gag")
got2 = quick.ideas(d10, "podcast", [], 3)
check(len(got2) == 3 and "invented for it" in d10.calls[-1][4],
      "...and with nobody cast, for new characters")
jd = quick.new_job("x", "sketch", seconds=30, quality="Draft")
jf = quick.new_job("x", "sketch", seconds=30, quality="Final")
check(quick.estimate(jf) > quick.estimate(jd) and
      quick.new_job("x", "sketch", quality="Ultra")["quality"] == "Draft",
      "Final quotes longer than Draft; an unknown quality falls back to Draft")

print("§10 the take check: bad takes are flagged, rerolled only when Ryan says")
d15 = FakeDirector()
_qn = []


def _qc_frame(frame, still, expect):
    _qn.append(expect)
    return ({"verdict": "broken", "reasons": ["a second Clarence in the background"]}
            if len(_qn) == 1 else {"verdict": "ok", "reasons": []})


d15.qc_frame = _qc_frame
SUB.clear()
j15 = quick.new_job("he reviews clouds", "vlog", cast=[job["cast"][0]], seconds=8)
j15 = quick.run(j15, director=d15)
f15 = quick.flagged(j15)
nv0 = len([k for k, _p, _r in SUB if k == "video"])
check(j15["state"] == "done" and [x[0] for x in f15] == [1] and j15["flagged"] == [1] and
      "second Clarence" in f15[0][1][0] and os.path.exists(f15[0][2] or ""),
      "a take that looks wrong is flagged with the reason and a frame; the video still "
      "finishes (%r)" % (f15,))
quick.reroll(j15, [1])
check(j15["steps"]["shots"] == "todo" and j15["steps"]["cut"] == "todo" and
      j15["state"] == "stopped", "Reroll queues a new take of that shot and a new cut")
j15 = quick.run(j15, director=d15)
ep15 = sp.load_project(j15["episode"])
check(j15["state"] == "done" and len([k for k, _p, _r in SUB if k == "video"]) == nv0 + 1 and
      len(ep15["shots"][0]["takes"]) == 2 and quick.flagged(j15) == [],
      "...one new take (the old one kept in Takes), checked, re-cut, nothing flagged")
ep15["shots"][0]["qc"]["flags"] = ["looks off: x"]
sp.save_project(ep15)
check([x[0] for x in quick.flagged(j15)] == [1], "(a flag shows up in the list)")
quick.ok_shot(j15, 1)
check(quick.flagged(j15) == [] and len([k for k, _p, _r in SUB if k == "video"]) == nv0 + 1,
      "'it's fine' clears the flag and rerolls nothing")

print("§10b all my videos; re-cut; re-film any shot")
h_ = quick.history()
check(h_ and h_[0]["id"] == j15["id"] and all(v["final"] for v in h_) and
      all(quick.load(v["id"])["state"] == "done" for v in h_),
      "ALL MY VIDEOS lists the finished videos, newest first (%d)" % len(h_))
check([v["id"] for v in quick.history("clouds")] == [j15["id"]] and
      quick.history("zzqx nothing") == [], "search finds by the idea's words")
sl_ = quick.shot_list(j15)
check(sl_ and sl_[0][0] == 1 and sl_[0][1] and os.path.exists(sl_[0][3] or ""),
      "Re-film a shot lists every shot with who and a frame (%r)" % (sl_[:1],))
nv1 = len([k for k, _p, _r in SUB if k == "video"])
fin0 = j15["final"]
quick.recut(j15, captions="big", pace="breathe", hook=False)
ep_rc = sp.load_project(j15["episode"])
check(j15["steps"]["cut"] == "todo" and j15["steps"]["shots"] == "done" and
      ep_rc["caption_look"] == "big" and ep_rc["pace"] == "breathe" and
      "hook_text" not in ep_rc and os.path.exists(j15["old_cuts"][0]),
      "Re-cut changes the captions / pace / hook, keeps the old video, queues only the cut")
j15 = quick.run(j15, director=d15)
check(j15["state"] == "done" and os.path.exists(j15["final"]) and
      len([k for k, _p, _r in SUB if k == "video"]) == nv1 and
      quick.load(j15["id"])["captions"] == "big",
      "...cut again with no new renders")
quick.recut(j15, captions="none")
check(sp.load_project(j15["episode"])["captions"] is False, "captions None turns them off")
quick.reroll(j15, [sl_[0][0]])
j15 = quick.run(j15, director=d15)
check(j15["state"] == "done" and len([k for k, _p, _r in SUB if k == "video"]) == nv1 + 1,
      "re-filming a shot that wasn't flagged makes one new take")

print("§10c stars teach the writer; back to the previous version")
quick.rate(j15, 5)
jl = quick.load(j15["id"])
check(jl["rating"] == 5, "a finished video keeps its stars")
tn = quick.taste_notes(jl["format"])
check("LANDED" in tn and (jl.get("idea") or "") in tn, "a 5-star video becomes a taste note")
_low = quick.new_job("a dull idea about nothing", "vlog")
_low.update(state="done", rating=1, title="Dull")
quick.save(_low)
tn2 = quick.taste_notes("vlog")
check("DIDN'T" in tn2 and "a dull idea about nothing" in tn2,
      "...and a 1-star one as what to avoid")
d10c = FakeDirector()
j10c = quick.run(quick.new_job("he reviews rain", "vlog", cast=[job["cast"][0]], seconds=8),
                 director=d10c)
_b10 = " ".join(str(c_[1]) for c_ in d10c.calls if c_[0] in ("draft", "game", "dialogue"))
check("LANDED" in _b10 and "a dull idea about nothing" in _b10,
      "the next video's writer gets both")
check(quick.taste_notes("vlog", jobs=[]) == "", "no ratings: nothing added")
_fin_before = open(j15["final"], "rb").read()
_n_old = len(j15.get("old_cuts") or [])
quick.recut(j15, captions="clean")
j15 = quick.run(j15, director=d15)
_fin_after = open(j15["final"], "rb").read()
quick.undo_recut(j15)
check(open(j15["final"], "rb").read() == _fin_before and j15["captions"] != "clean" and
      len(j15["old_cuts"]) == _n_old and
      open(j15["redo_cuts"][-1], "rb").read() == _fin_after and
      sp.load_project(j15["episode"]).get("captions") == (j15["captions"] != "none") and
      (j15["captions"] == "none" or
       sp.load_project(j15["episode"]).get("caption_look") == j15["captions"]),
      "Back to the previous version: the old video and its settings come back; the newer "
      "one is kept beside it")
quick.rate(j15, 0)
check(quick.load(j15["id"]).get("rating") is None, "stars can be cleared")

print("§11 a series: the show bible goes into every episode")
import series as _series  # noqa: E402
sr_ = _series.new("Barnaby's Weather", logline="a weatherman who takes the weather personally",
                  tone="deadpan local news", fmt="sketch", cast=job["cast"],
                  rules="Barnaby never admits he's wrong\n- the studio is always too cold",
                  gags=["the green screen is in the wrong place"])
check(sr_["rules"] == ["Barnaby never admits he's wrong", "the studio is always too cold"] and
      _series.load(sr_["slug"])["cast"] == job["cast"],
      "a series keeps its cast, rules (one per line) and gags")
d11 = FakeDirector()
j11 = quick.new_job("he forecasts his own divorce", "sketch", seconds=8, cast=job["cast"],
                    series=sr_["slug"])
j11 = quick.run(j11, director=d11)
_brief = " ".join(str(c_[1]) for c_ in d11.calls if c_[0] in ("draft", "game", "dialogue"))
check(j11["state"] == "done" and "ALWAYS TRUE IN THIS SHOW" in _brief and
      "never admits he's wrong" in _brief and "green screen" in _brief and
      "deadpan local news" in _brief, "the writer gets the bible: tone, rules, running gags")
check(_series.load(sr_["slug"])["episodes"] == [j11["id"]],
      "a finished video becomes the series' next episode")
d12 = FakeDirector()
j12 = quick.run(quick.new_job("he forecasts a parade", "sketch", seconds=8,
                              cast=job["cast"], series=sr_["slug"]), director=d12)
_brief2 = " ".join(str(c_[1]) for c_ in d12.calls if c_[0] in ("draft", "game", "dialogue"))
check("EARLIER EPISODES" in _brief2 and "his own divorce" in _brief2,
      "episode 2's writer is told what episode 1 was (so it doesn't repeat it)")
check(quick.again(j12, "script").get("series") == sr_["slug"],
      "Another script stays in the series")
j13 = quick.run(quick.again(j12, "final"), director=FakeDirector())
check(j13["state"] == "done" and
      _series.load(sr_["slug"])["episodes"] == [j11["id"], j13["id"]],
      "a Final re-shoot takes its Draft's place in the series (not a second episode)")


class _IdeaDir(FakeDirector):
    def _chat(self, system, user, schema, temperature=1.0, model=None):
        if schema is _series.IDEAS_SCHEMA:
            self.calls.append(("ideas", user))
            return {"ideas": ["he forecasts a parade", "the studio floods", "a rival weatherman"]}
        return FakeDirector._chat(self, system, user, schema, temperature, model)


_di = _IdeaDir()
_ni = _series.next_ideas(_di, _series.load(sr_["slug"]), 3)
check(_ni == ["the studio floods", "a rival weatherman"] and "his own divorce" in _di.calls[-1][1],
      "next episode ideas fit the bible and skip what's been made (%r)" % _ni)
check([s_["slug"] for s_ in _series.all_series()] == [sr_["slug"]] and
      _series.update(sr_["slug"], tone="warmer")["tone"] == "warmer",
      "series are listed and editable")
try:
    _series.new("  ")
    _e11 = ""
except ValueError as exc:
    _e11 = str(exc)
check("name" in _e11, "a series needs a name")

print("§12 product -> ad: a real product in the presenter's hands, an honest script")
prod = ff(["-f", "lavfi", "-i", "color=c=0x22aa66:s=200x300", "-frames:v", "1"],
          os.path.join(_TMP, "glowmug.png"))
SUB.clear()
d12p = FakeDirector()
jp = quick.new_job("the mug that keeps tea hot", "ad", seconds=8, cast=[job["cast"][0]],
                   product=prod, product_name="Glowmug", product_what="keeps tea hot for 6 hours",
                   hook_angle=quick.HOOK_ANGLES[1])
jp = quick.run(jp, director=d12p)
_bp = " ".join(str(c_[1]) for c_ in d12p.calls if c_[0] in ("draft", "game", "dialogue"))
check(jp["state"] == "done" and "THIS TIME THE PRODUCT IS REAL" in _bp and "Glowmug" in _bp
      and "keeps tea hot for 6 hours" in _bp and "honest" in _bp and
      quick.HOOK_ANGLES[1] in _bp,
      "the writer is told the real product, what it does, honest claims, the hook angle")
_pe = [x for x in SUB if x[0] == "edit" and x[2][-1:] == [prod]]
_ep = sp.load_project(jp["episode"])
_plates = [_ep["set"]["master"]] + list((_ep["set"].get("singles") or {}).values())
check(len(_pe) == len(_plates) >= 1 and all("Glowmug" in x[1]["prompt"] and
                                            "label" in x[1]["prompt"] for x in _pe) and
      sorted(_ep["set"]["product_plates"]) == sorted(["master"] +
                                                    list(_ep["set"].get("singles") or {})),
      "every set plate gets the product put in hand, from the product photo (%d)" % len(_pe))
_vid = [x for x in SUB if x[0] == "video"]
check(_vid and all(r_ and r_[0] in _plates for _k, _p, r_ in _vid),
      "...and the shots are filmed from those plates")
n_e = len(SUB)
check(quick.product_pass(jp, sp.load_project(jp["episode"])) is False and len(SUB) == n_e,
      "done once: running it again changes nothing")
quick.redo_set(jp, "master")
jp = quick.run(jp, director=d12p, go_on=True)
_pe2 = [x for x in SUB[n_e:] if x[0] == "edit" and x[2][-1:] == [prod]]
check(len(_pe2) >= 1, "a rebuilt set gets the product again (%d)" % len(_pe2))
j_plain = quick.new_job("x", "ad", seconds=8)
check(quick.product_brief(j_plain) == "" and j_plain["product"] is None,
      "no product: the invented-product ad as before")
check(quick.again(jp, "script").get("product") == prod, "Another script keeps the product")

print("§13 a line's sound effect")


class _SfxDir(FakeDirector):
    def _chat(self, system, user, schema, temperature=1.0, model=None):
        if schema is quick.ACTING_SCHEMA:
            self.calls.append(("acting", user))
            return {"lines": [{"gesture": "g%d" % i, "reaction": "r%d" % i,
                               "sound": "a knock at the door" if i == 1 else ""}
                              for i in range(12)]}
        return FakeDirector._chat(self, system, user, schema, temperature, model)


_ds = _SfxDir()
js = quick.run(quick.new_job("someone at the door", "sketch", seconds=8, cast=job["cast"]),
               director=_ds)
_eps = sp.load_project(js["episode"])
_ls = sp.line_shots(_eps)
check(js["state"] == "done" and _ls[1].get("sfx") == "a knock at the door" and
      not _ls[0].get("sfx") and "sound: ONE short sound effect" in
      [c_[1] for c_ in _ds.calls if c_[0] == "acting"][0],
      "the acting pass can name a sound for a line; it goes on that shot")
check(any(p_.endswith("knock.wav") for _t, p_ in _eps.get("sfx_placed") or []),
      "...and the cut lays it in (%r)" % _eps.get("sfx_placed"))
_act = [c_[1] for c_ in _ds.calls if c_[0] == "acting"][0]
check("(flat)" in _act and "writer's own direction" in _act,
      "the acting pass sees the writer's direction for each line (and keeps an action)")


class _LabelDir(FakeDirector):
    def dialogue(self, brief, cast, seconds=30.0, model=None):
        out = FakeDirector.dialogue(self, brief, cast, seconds, model)
        a = sorted(cast)[0]
        out["lines"][-1]["text"] = "%s: That's the cold front." % a.title()
        return out


quick.ROOM, _room0 = False, quick.ROOM
jl_ = quick.run(quick.new_job("labels", "sketch", seconds=8, cast=job["cast"]),
                director=_LabelDir())
quick.ROOM = _room0
_lines = dlg.load_project(jl_["dialogue"])["lines"]
check(_lines[-1]["text"] == "That's the cold front.",
      "a line that came back with its speaker's name in front is cleaned (%r)"
      % _lines[-1]["text"])

print("§14 you pick the script")
class _PickDir(FakeDirector):
    def _chat(self, system, user, schema, temperature=1.0, model=None):
        out = FakeDirector._chat(self, system, user, schema, temperature, model)
        if schema is quick.writers_room.DRAFT_SCHEMA:
            k = len([c_ for c_ in self.calls if c_[0] == "draft"])
            out = dict(out, title="Draft %d" % k)
            out["lines"] = [dict(li) for li in out["lines"]]
            out["lines"][1]["text"] = "Line two of draft %s." % ["one", "two", "three", "four",
                                                                  "five", "six"][(k - 1) % 6]
        return out


dp = _PickDir()
jp = quick.new_job("a weatherman and a conductor argue about soup", "sketch", seconds=8,
                   cast=job["cast"], pick_draft=True)
jp = quick.run(jp, director=dp)
check(jp["state"] == "review:drafts" and len(jp["drafts"]) == 3 and
      jp["steps"]["script"] == "pick" and not jp.get("dialogue"),
      "it stops after writing with three drafts and nothing filmed (%s)" % jp["state"])
check(not [c_ for c_ in dp.calls if c_[0] in ("rubric", "punch", "cold")],
      "no judge, punch-up or cold read when you pick")
check([d_["title"] for d_ in jp["drafts"]] == ["Draft 1", "Draft 2", "Draft 3"] and
      all(li["text"] and ":" not in li["text"][:12] for d_ in jp["drafts"]
          for li in d_["lines"]),
      "each draft is kept whole, speaker names stripped")
jp = quick.run(quick.load(jp["id"]), director=dp)
check(jp["state"] == "review:drafts" and
      len([c_ for c_ in dp.calls if c_[0] == "draft"]) == 3,
      "running it again without a pick waits again (no new drafts written)")
quick.pick_draft(jp, 1)
jp = quick.run(quick.load(jp["id"]), director=dp)
_pl = dlg.load_project(jp["dialogue"])["lines"] if jp.get("dialogue") else []
check(jp["state"] in ("done", "review:episode") and
      any(l_["text"] == "Line two of draft two." for l_ in _pl) and
      jp["room"].get("picked") == 2,
      "pick draft 2 -> it carries on with THAT script (%s)" % jp["state"])
try:
    quick.pick_draft(jp, 7)
    check(False, "a draft that isn't there is refused")
except ValueError:
    check(True, "a draft that isn't there is refused")
jm = quick.new_job("soup", "sketch", seconds=8, cast=job["cast"], pick_draft=True)
dm = _PickDir()
jm = quick.run(jm, director=dm)
quick.more_drafts(jm)
jm = quick.run(quick.load(jm["id"]), director=dm)
check(jm["state"] == "review:drafts" and
      len([c_ for c_ in dm.calls if c_[0] == "draft"]) == 6,
      "None of these -> three NEW drafts")
import nightshift as _nsx
check(_nsx.UNATTENDED.get("pick_draft") is False,
      "the night shift never stops to ask (nobody's there to pick)")
check(quick.new_job("x", "sketch")["pick_draft"] is False and
      "pick_draft" in quick._CARRY,
      "off unless asked; More like this keeps the choice")

print("§15 a speaker with no body: the AI doorbell is a VOICE, not a person (2026-10-24)")


class _VoiceDir(FakeDirector):
    def _chat(self, system, user, schema, temperature=1.0, model=None):
        if schema is quick.ROLES_SCHEMA:
            self.calls.append(("roles", user))
            return {"roles": ["a locked-out homeowner"], "voices": [
                {"name": "Doorbell!", "what": "his new AI doorbell", "voice": "calm and polite",
                 "gender": "robot", "age": 300},
                {"name": "DOORBELL", "what": "a duplicate", "voice": "", "gender": "male",
                 "age": 40}]}
        return FakeDirector._chat(self, system, user, schema, temperature, model)


dv = _VoiceDir()
_n_sub = len(SUB)
_room15, quick.ROOM = quick.ROOM, True             # (§8 left the one-shot writer on)
jv = quick.new_job("a man is locked out because his new AI doorbell won't recognise him",
                   "doorbell", seconds=8)
jv = quick.run(jv, director=dv)
check(jv["state"] == "done" and os.path.exists(jv.get("final") or ""),
      "a doorbell job with a voice-only speaker runs to the end (%s)" % jv.get("error"))
_ra = [c_ for c_ in dv.calls if c_[0] == "roles"]
check(_ra and "TALKS but has no body" in _ra[0][1] and "NEVER also one of the roles" in _ra[0][1],
      "the idea is read for PEOPLE on screen and for VOICES with no body, apart")
_vr = jv.get("voice_roles") or []
check(len(jv["cast"]) == 1 and len(_vr) == 1 and _vr[0]["name"] == "DOORBELL" and
      _vr[0]["gender"] == "female" and _vr[0]["age"] == 90,
      "one person drawn; the doorbell kept once as a voice role, its name cleaned, a bad "
      "gender / age made safe (%r)" % _vr)
_vs = {v_["slug"]: v_ for v_ in dlg.list_voices()}
check(_vr and _vr[0].get("voice_slug") in _vs and
      "DOORBELL" in _vs[_vr[0]["voice_slug"]]["characters"] and
      "his new AI doorbell" in (_vs[_vr[0]["voice_slug"]].get("notes") or "") and
      "calm and polite" in (_vs[_vr[0]["voice_slug"]].get("notes") or ""),
      "the doorbell got a DESIGNED voice from its description, saved in Voices")
_dl = [c_ for c_ in dv.calls if c_[0] == "draft"]
check(_dl and "DOORBELL: VOICE ONLY - his new AI doorbell" in _dl[0][1] and
      "THE IDEA COMES FIRST" in _dl[0][1],
      "the writer is told the doorbell is heard, never seen - and that the idea beats the "
      "format's usual porch visitor")
_pjv = dlg.load_project(jv["dialogue"])
check(_pjv["cast"].get("DOORBELL") == _vr[0].get("voice_slug") and
      [l_["speaker"] for l_ in _pjv["lines"]][1] == "DOORBELL",
      "the doorbell's lines are read in ITS voice")
_ac = [c_ for c_ in dv.calls if c_[0] == "acting"]
check(_ac and "DOORBELL (VOICE ONLY)" in _ac[-1][1] and "never give the voice a body" in
      _ac[-1][1], "the acting pass gives the doorbell's lines to the person on screen")
_epv = sp.load_project(jv["episode"])
_ls = sp.line_shots(_epv)
_vo = [sh for sh in _ls if sh.get("voice_only")]
check(len(_ls) == 3 and len(_vo) == 1 and _vo[0]["speaker"] == jv["cast"][0] and
      _epv["characters"] == jv["cast"] and _vo[0].get("intent") != "lipsync",
      "the doorbell line is a shot of the homeowner (not a second person, not lip-synced)")
check("LISTENS to his new AI doorbell" in _vo[0]["prompt"] and "mouth stays CLOSED" in
      _vo[0]["prompt"] and "frowns" in _vo[0]["prompt"] and
      "<d>" not in sp.h3_prompt(_epv, _vo[0], "i2v") and
      "<d>" in sp.h3_prompt(_epv, _ls[0], "i2v"),
      "...listening, mouth closed, with his acting; H3 is sent no spoken line for it")
_vsub = [p_ for k_, p_, r_ in SUB[_n_sub:] if k_ == "video"]
check(len(_vsub) == 3 and sum(1 for p_ in _vsub if p_.get("guide_audio_path")) == 2,
      "two lip-synced shots with guide audio, the listening shot with none")
check(_vo[0].get("audio_wav") and os.path.exists(_vo[0]["audio_wav"]) and _vo[0].get("words")
      and _vo[0].get("cut_in") is not None,
      "the doorbell's voice is laid into the cut over that shot, with its captions")
_mt = {"slug": "mt", "characters": ["x"], "shots": [
    {"n": 1, "audio_wav": _vo[0]["audio_wav"], "line": "a"},
    {"n": 2, "audio_wav": _vo[0]["audio_wav"], "line": "b", "voice_only": True},
    {"n": 3, "audio_wav": _vo[0]["audio_wav"], "line": "c"}]}
sp.merge_long_takes(_mt)
check(len(_mt["shots"]) == 3 and _mt["shots"][1].get("voice_only"),
      "a long take never swallows another voice's line")
_jx = quick.again(jv, "script")
check(_jx.get("voice_roles") == jv["voice_roles"],
      "More like this keeps the doorbell's voice")
check(quick._spoken("Fine. *wags tail* Hire him.", "") == ("Fine. Hire him.", "wags tail"),
      "an action written into a line is moved to its direction before it's voiced")
quick.ROOM = _room15

print("§6 both cards: two shots at once; a 'cont' shot waits for the one before")
import threading as _th  # noqa: E402
import time as _tm  # noqa: E402
live, peak, order = set(), [0], []
_lk = _th.Lock()
_real_rs = sp.render_shot


def slow_rs(pj, n, seed=-1):
    with _lk:
        live.add(n)
        peak[0] = max(peak[0], len(live))
        order.append(("start", n))
    _tm.sleep(0.4)
    with _lk:
        live.discard(n)
        order.append(("end", n))


sp.render_shot = slow_rs
pjm = {"slug": "lanes", "shots": [{"n": 1}, {"n": 2}, {"n": 3, "video_mode": "cont"}, {"n": 4}]}
errs = sp.render_many(pjm, [1, 2, 3, 4], lanes=2)
check(not errs and peak[0] == 2, "two lanes: two shots run at the same time (peak %d)" % peak[0])
check(order.index(("start", 3)) > order.index(("end", 2)),
      "shot 3 (starts from the last shot) waited for shot 2 to finish")
live.clear(); peak[0] = 0
sp.render_many(pjm, [1, 2], lanes=1)
check(peak[0] == 1, "one lane: one at a time")
sp.both_cards = lambda: True
jx = quick.new_job("x", "sketch", seconds=30)
e2 = quick.estimate(jx)
sp.both_cards = lambda: False
check(e2 < quick.estimate(jx), "the quote drops when both cards share the shots")
sp.render_shot = _real_rs

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
