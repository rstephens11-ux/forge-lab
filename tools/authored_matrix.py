#!/usr/bin/env python3
"""(header copied from quick_matrix.py)
GPU-free checks for quick.py — QUICK mode, one idea -> a finished video.

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



# ======================================================================= AUTHORED jobs
print("§A an AUTHORED job: its own cast, script, acting, hook, set and look")
import char_styles  # noqa: E402
import json as _json  # noqa: E402
SPEC = dict(char_styles.roll("Gilded satire", "The powerful one", __import__("random").Random(1)))
SPECS = [dict(SPEC, name="Barnaby Suet"), dict(SPEC, name="Ned Thackery",
                                                   subject="a gaunt weaver foreman"),
         dict(SPEC, name="Clementine Suet", subject="a radiant duchess", pronoun="she")]
LINES = [{"speaker": "BARNABY", "direction": "chewing", "text": "Good news, everyone."},
         {"speaker": "NED", "direction": "", "text": "Is it, m'lord?"},
         {"speaker": "BARNABY", "direction": "", "text": "You're being reskilled."},
         {"speaker": "CLEMENTINE", "direction": "beaming", "text": "Isn't it marvellous?"},
         {"speaker": "NED", "direction": "quietly", "text": "Whose life, m'lord?"}]
ACT = [{"gesture": "tears a drumstick in half %d" % i, "reaction": "blinks %d" % i,
        "sound": ""} for i in range(len(LINES))]
AU = {"cast": SPECS, "script": {"title": "Reskilling Day", "lines": LINES}, "acting": ACT,
      "hook_text": "Good news. You're being reskilled.",
      "where": "a gilded banquet hall, warm window light", "style": "Warm period film look."}
d = FakeDirector()
said = []
job = quick.new_job("Reskilling Day", "gilded", seconds=20, authored=AU)
job = quick.run(job, director=d, progress=said.append)
check(job.get("state") == "done", "the authored job finishes (%s %s)" % (job.get("state"),
                                                                        job.get("error")))
kinds = {k[0] for k in d.calls}
check(not kinds & {"draft", "game", "rubric", "cold", "punch", "pick", "dialogue", "acting",
                   "hook", "invent", "person", "roles", "where"},
      "no writer was asked for anything (%s)" % sorted(kinds))
names = [quick._name(s_) for s_ in job["cast"]]
check(names == ["Barnaby Suet", "Ned Thackery", "Clementine Suet"],
      "the cast is the three authored characters, in order (%s)" % names)
m0 = quick.c.read_character(job["cast"][0]) or {}
check(m0.get("style") == "Gilded satire" and "wide-angle" in (m0.get("prompt") or "")
      and m0.get("picks", {}).get("Framing") == char_styles.style("Gilded satire")["framing"],
      "each was drawn with the style's own prompt and framing")
stills = [p_ for k_, p_, _r in SUB if k_ == "t2i"]
check(any("Barnaby" not in p_.get("prompt", "") and "prosthetic" in p_.get("prompt", "")
          for p_ in stills), "the still prompts are the gilded ones")
pj = quick.dlg.load_project(job["dialogue"])
check([li.get("text") for li in pj["lines"]] == [li["text"] for li in LINES],
      "the script is used word for word")
check(job["acting"] == ACT and job["hook_text"] == AU["hook_text"],
      "the authored acting and hook are used")
ep = sp.load_project(job["episode"])
check(ep.get("where") == AU["where"] and ep.get("style") == AU["style"],
      "the set and every shot get the authored place and look")
check(ep.get("end_fade") and ep.get("caption_look") == "subtitle",
      "the gilded format's fade and subtitles still apply")
check(os.path.exists(job.get("final") or ""), "a finished video was cut")
n_before = len([1 for k_, _p, _r in SUB if k_ == "t2i"])
job2 = quick.new_job("again", "gilded", seconds=20, authored=AU)
job2["authored_slugs"] = dict(job["authored_slugs"])
quick._cast_authored(job2, SPECS, lambda m: None)
check(len([1 for k_, _p, _r in SUB if k_ == "t2i"]) == n_before and
      job2["cast"] == job["cast"], "a resumed job keeps the characters already drawn")
print("§B an authored PLACE: the room is made first, then the people go into it")
AU2 = dict(AU, place={"name": "Grimy hall", "text": "a soot-black gilded hall full of "
                      "rusted porcelain robot guards"})
n_t2i = len([1 for k_, _p, _r in SUB if k_ == "t2i"])
job4 = quick.new_job("Reskilling Day 2", "gilded", seconds=20, authored=AU2)
job4["authored_slugs"] = dict(job["authored_slugs"])      # same cast, already drawn
job4 = quick.run(job4, director=d, progress=lambda m: None)
check(job4.get("state") == "done", "the job with a place finishes (%s)" % job4.get("error"))
pl_calls = [p_ for k_, p_, _r in SUB[len(SUB) - 200:] if k_ == "t2i" and
            "robot guards" in p_.get("prompt", "")]
check(len([1 for k_, _p, _r in SUB if k_ == "t2i"]) == n_t2i + 1 and pl_calls and
      "no people" in pl_calls[0]["prompt"], "exactly one picture: the EMPTY place, machines "
      "and all (the cast was reused)")
import places as _places  # noqa: E402
check(job4.get("place") and _places.read(job4["place"]) and
      "robot guards" in _places.describe(job4["place"]), "it is kept in Places")
ep4 = sp.load_project(job4["episode"])
check(ep4.get("place") == job4["place"] and sp.place_image(ep4),
      "the set is built INTO that picture")
quick._authored_place(job4, lambda m: None)
check(len([1 for k_, _p, _r in SUB if k_ == "t2i"]) == n_t2i + 1,
      "a resumed job doesn't make the place again")
print("§C pictures first: stop before filming, then film exactly those pictures")
n_vid = len([1 for k_, _p, _r in SUB if k_ == "video"])
n_img = len([1 for k_, _p, _r in SUB if k_ != "video"])
job5 = quick.new_job("Pictures first", "gilded", seconds=20, authored=dict(
    AU2, place={"name": "Grimy hall 2", "text": "a second soot-black hall of robot hounds"}),
    pictures_only=True)
job5["authored_slugs"] = dict(job["authored_slugs"])
job5 = quick.run(job5, director=d, progress=lambda m: None)
check(job5.get("state") == "pictures" and job5["steps"]["shots"] == "todo" and
      job5["steps"]["episode"] == "done", "a pictures-only job stops before filming (%s)"
      % job5.get("state"))
check(len([1 for k_, _p, _r in SUB if k_ == "video"]) == n_vid, "nothing was filmed")
ep5 = sp.load_project(job5["episode"])
check(job5.get("place") and (ep5.get("set") or {}).get("master") and
      os.path.exists(ep5["set"]["master"]), "the place and the set pictures are made")
n_img2 = len([1 for k_, _p, _r in SUB if k_ != "video"])
job5 = quick.load(job5["id"])
job5["pictures_only"] = False
job5 = quick.run(job5, director=d, progress=lambda m: None)
check(job5.get("state") == "done" and os.path.exists(job5.get("final") or ""),
      "resumed, it films and cuts")
check(len([1 for k_, _p, _r in SUB if k_ != "video"]) == n_img2 and
      sp.load_project(job5["episode"])["set"]["master"] == ep5["set"]["master"],
      "the approved pictures are used as they are - nothing is redrawn")
j3 = quick.new_job("plain", "sketch")
check(j3.get("authored") is None and "idea_where" not in j3,
      "an ordinary job is unchanged")
_json.dumps(job)                                    # it must survive the night-shift queue

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
