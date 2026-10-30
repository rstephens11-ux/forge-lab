#!/usr/bin/env python3
"""GPU-free checks for voicelab.py — the VOICES page engine: accents, describe, audition,
keep, clone (typed or heard transcript), the two-engine test bench, linking.
The rig (tts_batch / transcribe_many) is faked: what the page SENDS is what's checked.
    python3 tools/voicelab_matrix.py"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["FORGE_LAB_DATA"] = tempfile.mkdtemp(prefix="voicelab-")
import dialogue as dlg  # noqa: E402
import scene_engine as se  # noqa: E402
import voicelab as vl  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def tone(path, secs=1.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300",
                    "-t", str(secs), path], check=True)
    return path


SENT, HEARD = [], []


def fake_tb(job, out_dir, language="English", engine="qwen"):
    SENT.append({"job": json.loads(json.dumps(job)), "engine": engine})
    items = []
    for it in job.get("design", []) + job.get("clone", []):
        o = tone(os.path.join(out_dir, it["out"]))     # the rig scripts require "out" too
        items.append({"id": it["id"], "out": o})
    return {"ok": True, "items": items}


def fake_tm(paths, language="English"):
    HEARD.append((list(paths), language))
    return ["Hello, this is my real voice." for _ in paths]


se.tts_batch, se.transcribe_many = fake_tb, fake_tm

print("§1 accents")
acc = vl.accents()
labels = [a for a, _p in acc]
check(len(acc) >= 70 and len(set(labels)) == len(labels),
      "%d accents, none listed twice" % len(acc))
check(labels.index("Early American gentleman (Avery Stone)") < labels.index("Canadian"),
      "dialogue's own accents keep their order, first")
check(vl.accent_phrase("Minnesotan").startswith("Minnesotan") and
      vl.accent_phrase("a Martian lilt") == "a Martian lilt",
      "a picked label -> its words; typed text passes through")

check(len(vl.SAMPLE_LINE.split()) >= 20,
      "the default sample line is long enough for an accent to show (%d words)"
      % len(vl.SAMPLE_LINE.split()))

print("§2 build by description")
v = vl.blank("Clarence")
v.update({"gender": "male", "age_years": 38, "accent": vl.accent_phrase("Minnesotan"),
          "notes": "breathy, calm urgency", "language": "English"})
v["sliders"]["pace"] = 1
d = vl.describe(v)
check("38-year-old male voice" in d and "Minnesotan" in d and "breathy" in d,
      "the description carries age, gender, accent, notes (%r)" % d[:90])
wavs = vl.audition(v, 3, text="Expect a forty percent chance of rain.")
j = SENT[-1]
check(j["engine"] == "qwen" and len(j["job"]["design"]) == 3 and
      all(i["text"] == "Expect a forty percent chance of rain." for i in j["job"]["design"]) and
      all(i["instruct"].startswith(d.rstrip(".")) for i in j["job"]["design"]),
      "Audition 3 = three qwen DESIGNS of that sentence, reading YOUR sample line")
check(len({i["instruct"] for i in j["job"]["design"]}) == 3, "...each a slightly different take")
slug = vl.keep(v, wavs[1])
sv = dlg.load_voice(slug)
check(sv and os.path.exists(os.path.join(dlg.voices_dir(), slug, "neutral.wav")) and
      sv["moods"]["neutral"]["ref_text"] == "Expect a forty percent chance of rain." and
      sv.get("origin") == "designed",
      "Keep: saved to the library; its reference transcript is the line it actually read")
lib = {x["slug"]: x for x in vl.library()}
check(slug in lib and "Minnesotan" in lib[slug]["line"] and lib[slug]["wav"],
      "the library lists it with its accent and a playable sample (%r)" % lib[slug]["line"])

# 2026-10-21: two voices built from "New voice" must not land in the same slot
na = vl.blank()
na["name"] = "First try"
wa = vl.audition(na, 1)
sa = vl.keep(na, wa[0])
nb = vl.blank()
nb_slug0 = nb["slug"]
nb["name"] = "First try"
wb = vl.audition(nb, 1)
sb = vl.keep(nb, wb[0])
check(sa == "first-try" and sb == "first-try-2" and dlg.load_voice(sa)["name"] == "First try"
      and dlg.load_voice(sb), "a new voice gets its own slot from its name (%s, %s)" % (sa, sb))
check(not os.path.exists(os.path.join(dlg.voices_dir(), nb_slug0)) or nb_slug0 in (sa, sb),
      "a fresh form's slug belongs to no saved voice (%s)" % nb_slug0)
check("NEW VOICE" not in dlg.load_voice(sa)["characters"] and "_new" not in
      dlg.load_voice(sa), "no placeholder speaker, no working flag saved")
_again = dlg.load_voice(sa)
_again["notes"] = "re-kept"
vl.keep(_again, wa[0])
check(len([d for d in os.listdir(dlg.voices_dir()) if d.startswith("first-try")]) == 2,
      "re-keeping a SAVED voice replaces it in place (no third copy)")

print("§3 clone from a clip")
clip = tone(os.path.join(tempfile.mkdtemp(), "me.mp3"), 2.0)
s2 = vl.clone("My voice", clip, transcript="")
cv = dlg.load_voice(s2)
check(HEARD and cv["moods"]["neutral"]["ref_text"] == "Hello, this is my real voice." and
      cv["origin"] == "clone", "no transcript typed -> the rig's ASR hears it")
check(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
                      "-of", "csv=p=0", os.path.join(dlg.voices_dir(), s2, "neutral.wav")],
                     capture_output=True, text=True).stdout.strip() == "24000,1",
      "an mp3 becomes a mono 24 kHz wav reference")
n_heard = len(HEARD)
s3 = vl.clone("Typed", clip, transcript="Exactly these words.")
check(len(HEARD) == n_heard and
      dlg.load_voice(s3)["moods"]["neutral"]["ref_text"] == "Exactly these words.",
      "a typed transcript is used as is (no ASR call)")
bad = False
try:
    vl.clone("x", "/nope.wav")
except ValueError:
    bad = True
check(bad, "no recording -> a clear refusal")

print("§4 the test bench: both engines")
out = vl.test(dlg.load_voice(slug), "Your ears are in my soup.", mood="whisper")
jq, jf = SENT[-2], SENT[-1]
check(jq["engine"] == "qwen" and jf["engine"] == "fish", "one line, said by qwen AND fish")
check(jq["job"]["clone"][0]["text"] == "Your ears are in my soup." and
      jf["job"]["clone"][0]["text"].startswith("[") and
      jf["job"]["clone"][0]["text"].endswith("Your ears are in my soup."),
      "fish gets the mood INLINE as a tag; qwen gets the plain words (%r)"
      % jf["job"]["clone"][0]["text"])
check(jq["job"]["voices"]["v"]["ref_wav"].endswith(os.path.join(slug, "neutral.wav")) and
      jq["job"]["voices"]["v"]["ref_text"] == "Expect a forty percent chance of rain.",
      "both clone from the voice's reference + its true transcript")
check(all(os.path.exists(p) for p in out.values()), "a wav back from each engine")
_td = os.path.join(dlg.voices_dir(), slug, "tests")
_ti = json.load(open(os.path.join(_td, "index.json"))) if os.path.exists(
    os.path.join(_td, "index.json")) else []
check(all(os.path.dirname(p) == _td for p in out.values()) and len(_ti) == 2 and
      _ti[-1]["engine"] == "fish" and _ti[-1]["said"].endswith("Your ears are in my soup."),
      "test takes are kept beside the voice, with what was said (%s)" % _ti[-1:])
check(vl.licence("fish").startswith("non-commercial") and "commercial" in vl.licence("qwen"),
      "fish renders are labelled non-commercial")
nov = vl.blank("Empty")
bad = False
try:
    vl.test(nov, "hi")
except ValueError:
    bad = True
check(bad, "testing a voice with no reference yet -> a clear refusal")

print("§4b Fish direction: the voice's own standing delivery, in Fish's bracket")
check(dlg.fish_line_text("neutral", "Hi.") == "Hi." and
      dlg.fish_line_text("neutral", "Hi.", "slow, deadpan") == "[slow, deadpan] Hi." and
      dlg.fish_line_text("whisper", "Hi.", "[slow]").startswith("[slow; ") and
      dlg.fish_line_text("whisper", "Hi.", "").startswith("[") and
      dlg.fish_line_text("whisper", "Hi.") == dlg.fish_line_text("whisper", "Hi.", None),
      "no direction = exactly as before; direction leads the mood in ONE bracket")
fv = dlg.load_voice(slug)
fv["fish_direction"] = "slow, deadpan, slightly bored"
vl.save_settings(fv)
vl.test(dlg.load_voice(slug), "Rain.", mood="neutral")
check(SENT[-1]["job"]["clone"][0]["text"] == "[slow, deadpan, slightly bored] Rain." and
      SENT[-2]["job"]["clone"][0]["text"] == "Rain.",
      "the test bench: Fish gets the direction, Qwen the plain words")
import dialogue_ui as _dui  # noqa: E402
pj2 = dlg.new_project("fd", "CLARENCE: Rain.")
pj2["cast"] = {"CLARENCE": slug}
_d, cj, _sk = _dui.plan_render(pj2, {slug: dlg.load_voice(slug)}, "missing", "fish")
check(cj and cj["clone"][0]["text"] == "[slow, deadpan, slightly bored] Rain.",
      "...and so does a Dialogue scene rendered on Fish (%r)"
      % (cj and cj["clone"][0]["text"]))

print("§5 link to a character")
cdir = os.path.join(os.environ["FORGE_LAB_DATA"], "characters", "clarence")
os.makedirs(cdir)
json.dump({"name": "Clarence"}, open(os.path.join(cdir, "character.json"), "w"))
vl.link(slug, "clarence")
check(json.load(open(os.path.join(cdir, "character.json"))).get("voice_slug") == slug and
      dlg.load_voice(slug).get("character") == "clarence",
      "Link: the character points at the voice, and the voice at the character")
pj = dlg.new_project("t", "CLARENCE: Hello.")
check(se.auto_cast(pj).get("cast", {}).get("CLARENCE") == slug,
      "...so a Dialogue scene casts Clarence with it automatically")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
