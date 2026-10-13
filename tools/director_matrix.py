#!/usr/bin/env python3
"""GPU-free assertions for director.py — the local-LLM DIRECTOR client.

    /opt/homebrew/bin/python3 tools/director_matrix.py

No Tk, no forge, no GPU: it stubs Director._post with canned model replies, so it
exercises the schema validator, the retry path, the routing table and the engine-dialect
enforcement without ever loading the model.
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import director as D  # noqa: E402

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def chat(obj):
    """A canned /api/chat response whose content is `obj` as JSON text."""
    return {"message": {"content": obj if isinstance(obj, str) else json.dumps(obj)}}


class Fake:
    """Replaces Director._post. Records calls; returns queued responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if not self.responses:
            return {"message": {"content": "{}"}}
        r = self.responses.pop(0)
        return r(path, payload) if callable(r) else r


CHAR = {"name": "Big Head", "prompt": "a middle-aged man with a COLOSSAL oversized head",
        "action": "walks slowly toward the camera, hands in pockets, head steady",
        "bible": {"who": "an ordinary middle manager", "absurd_thing": "his head is colossal",
                  "voice": "flat and reasonable", "running_bits": ["coffee cup too small"],
                  "never": ["acknowledge the head"]}}

print("\nSCHEMA VALIDATOR · accept")
_pitch_good = [{"title": "T", "logline": "L", "hook": "H"} for _ in range(3)]
check(D.validate(_pitch_good, D.PITCH_SCHEMA) == [], "a well-formed pitch list validates")
_script_good = {"title": "T", "hook": "H", "button": "B",
                "beats": [{"t_start": 0, "t_end": 3, "action": "a", "line": "", "speaker": ""}]}
check(D.validate(_script_good, D.SCRIPT_SCHEMA) == [], "a well-formed script validates")

print("\nSCHEMA VALIDATOR · reject")
_err = D.validate([{"title": "T", "logline": "L"}], D.PITCH_SCHEMA)
check(any("hook" in e for e in _err), "missing required key is rejected (%r)" % (_err[:1],))
_bad_enum = [{"n": 1, "seconds": 4, "intent": "character_action", "camera": "c",
              "action": "a", "line": "", "prompt": "p"}]
_err = D.validate(_bad_enum, D.SHOTS_SCHEMA)
check(any("enum" in e for e in _err), "out-of-set seconds (4) is rejected")
_bad_intent = [dict(_bad_enum[0], seconds=5, intent="teleport")]
_err = D.validate(_bad_intent, D.SHOTS_SCHEMA)
check(any("intent" in e and "enum" in e for e in _err), "unknown intent is rejected by enum")
_err = D.validate([], D.PITCH_SCHEMA)
check(any("minItems" in e for e in _err), "an empty array trips minItems")
_err = D.validate([{"title": "T", "logline": "L", "hook": "H"}] * 20, D.PITCH_SCHEMA)
check(any("maxItems" in e for e in _err), "an over-long array trips maxItems")
_err = D.validate([{"n": 1, "seconds": "5", "intent": "establishing", "camera": "c",
                    "action": "a", "line": "", "prompt": "p"}], D.SHOTS_SCHEMA)
check(any("integer" in e for e in _err), "a string where an integer is required is rejected")
_err = D.validate({"verdict": "maybe", "reasons": []}, D.QC_SCHEMA)
check(any("enum" in e for e in _err), "a bad QC verdict is rejected")

print("\nROUTING TABLE")
check(D.route("character_action") == {"engine": "h3", "video_mode": "r2v"},
      "character_action -> h3 r2v")
check(D.route("continue") == {"engine": "h3", "video_mode": "cont"}, "continue -> h3 cont")
check(D.route("lipsync")["audio"] is True and D.route("lipsync")["video_mode"] == "r2v",
      "lipsync -> h3 r2v with an audio guide")
check(D.route("motion_copy")["engine"] == "animate2", "motion_copy -> animate2")
check(D.route("establishing")["video_mode"] == "t2v", "establishing -> h3 t2v")
check(sorted(D.ROUTE_TABLE) == sorted(D.SHOTS_SCHEMA["items"]["properties"]["intent"]["enum"]),
      "the routing table and the shots schema enum agree")
check(D.SHOTS_SCHEMA["items"]["properties"]["seconds"]["enum"] == [3, 5, 8],
      "shot seconds are restricted to 3/5/8")
try:
    D.route("nope")
    check(False, "an unknown intent raises DirectorError")
except D.DirectorError:
    check(True, "an unknown intent raises DirectorError")

print("\nDIALECT ENFORCEMENT (_enforce_dialect)")
d = D.Director(host="http://stub")
_r2v = d._enforce_dialect({"intent": "character_action", "prompt": "he crosses the car park"})
check(not _r2v["prompt"].startswith("<Picture 1> is the character."),
      "r2v prompt does NOT get the undocumented '<Picture 1> is the character.' tag")
check(D.missing_ref2va_sections(_r2v["prompt"]) == list(D.REF2VA_SECTIONS),
      "a prose r2v prompt reports every Ref2VA section as missing")
# The local model writes the labels INLINE ('... in <Picture 1>. summary:\nA shot.') rather than
# each on its own line. They are present, so the predicate must not call them missing — a
# line-anchored match reported 5 of 6 absent on genuinely conformant output.
_inline = ("subject_definitions:\n<Subject 1> is him in <Picture 1>. summary:\nA tracking shot. "
           "retention_analysis:\nfully_preserved detailed_description:\n[Shot 1] He walks. "
           "overall_soundscape:\nRoom tone. non_diegetic_music:\nN/A")
check(D.missing_ref2va_sections(_inline) == [],
      "inline section labels count as PRESENT (%s)" % D.missing_ref2va_sections(_inline))
_norm = D.normalise_ref2va_labels(_inline)
check(_norm.count("\nsummary:") == 1 and _norm.count("\nnon_diegetic_music:") == 1
      and _norm.startswith("subject_definitions:"),
      "labels are normalised onto their own lines")
check(D.missing_ref2va_sections(_norm) == [], "the normalised prompt is conformant")
check(_r2v["engine"] == "h3" and _r2v["video_mode"] == "r2v", "r2v shot is annotated with its route")
_cont = d._enforce_dialect({"intent": "continue",
                            "prompt": "<Picture 1> is the character. he turns and walks off"})
check("<Picture 1>" not in _cont["prompt"], "cont prompt has the reference tag REMOVED")
_t2v = d._enforce_dialect({"intent": "establishing", "prompt": "an empty office at dawn"})
check("<Picture 1>" not in _t2v["prompt"], "t2v/establishing prompt carries no tag")
_still = d._enforce_dialect({"intent": "character_action",
                             "prompt": "looking straight at the camera, 85mm lens, he blinks"})
check("looking straight at the camera" not in _still["prompt"]
      and "85mm lens" not in _still["prompt"],
      "still-photo phrases are stripped from a video prompt")
check("<Picture 1> is the character" not in _still["prompt"],
      "no reference tag is added after the strip either")
_mc = d._enforce_dialect({"intent": "motion_copy", "prompt": "hold the pose"})
check(_mc["engine"] == "animate2" and "<Picture" not in _mc["prompt"],
      "motion_copy is animate2 and no tag")

print("\nCHAT · schema-valid straight through")
f = Fake([chat(_pitch_good)])
d = D.Director(host="http://stub"); d._post = f
res = d.pitch(CHAR, n=3)
check(len(res) == 3 and res[0]["title"] == "T", "pitch() returns the parsed list")
check(f.calls[0][0] == "/api/chat", "pitch() posts to /api/chat")
check(f.calls[0][1]["think"] is False, "think is disabled (no leaked reasoning)")
check("format" in f.calls[0][1], "the schema is handed to Ollama as `format`")

print("\nCHAT · retry feeds the validation errors back")
bad = chat([{"title": "T", "logline": "L"}])          # missing hook
f = Fake([bad, chat(_pitch_good)])
d._post = f
res = d.pitch(CHAR, n=3)
check(len(res) == 3, "the retry recovered a valid list")
check(len(f.calls) == 2, "exactly one retry was made")
_retry_user = f.calls[1][1]["messages"][-1]["content"]
check("REJECTED" in _retry_user and "hook" in _retry_user,
      "the retry message names the missing key")

print("\nCHAT · exhausts retries and raises")
f = Fake([bad, bad, bad])
d._post = f
try:
    d.pitch(CHAR, n=3)
    check(False, "two bad replies raise DirectorError")
except D.DirectorError:
    check(True, "two bad replies raise DirectorError")

print("\nSHOTS · post-processes every prompt")
_canned_shots = [
    {"n": 1, "seconds": 5, "intent": "character_action", "camera": "wide",
     "action": "walks in", "line": "", "prompt": "walking toward camera, 85mm lens"},
    {"n": 2, "seconds": 3, "intent": "continue",
     "action": "turns", "line": "morning", "camera": "medium",
     "prompt": "<Picture 1> is the character. he turns to the window"},
]
f = Fake([chat(_canned_shots)])
d._post = f
shots = d.shots(CHAR, _script_good)
check("<Picture 1> is the character" not in shots[0]["prompt"]
      and "85mm lens" not in shots[0]["prompt"], "shots(): r2v shot is cleaned, not tagged")
check("<Picture 1>" not in shots[1]["prompt"], "shots(): cont shot has no tag")
check(all("engine" in s for s in shots), "shots(): every shot is annotated with its engine")

print("\nREWRITE · mode guard")
try:
    d.rewrite("hi", "spicier")
    check(False, "an unknown rewrite mode raises")
except D.DirectorError:
    check(True, "an unknown rewrite mode raises")
f = Fake([chat({"text": "All good."})])
d._post = f
check(d.rewrite("It is fine.", "more deadpan") == "All good.", "rewrite returns the text field")

print("\nUNLOAD · keep_alive 0")
f = Fake([{"done": True}])
d = D.Director(host="http://stub"); d._post = f
d.unload()
_path, _payload = f.calls[0]
check(_path == "/api/generate", "unload posts to /api/generate")
check(_payload.get("keep_alive") == 0, "unload sends keep_alive 0")
check(_payload.get("model") == D.DEFAULT_MODEL, "unload names the model")

print("\nPROMPT CONTENT · identity + bible + house style")
ib = D.identity_block(CHAR)
check("Big Head" in ib and "COLOSSAL oversized head" in ib, "identity block carries name+prompt")
check("walks slowly toward the camera" in ib, "identity block carries the signature action")
check("colossal" in ib and "coffee cup too small" in ib, "bible (if present) is included")
sp = D.system_prompt(CHAR)
check(D.HOUSE_STYLE.split(":")[0] in sp, "system prompt embeds the house style")
check("Big Head" in sp, "system prompt embeds the identity block")
check("subject_definitions" in sp and "retention_analysis" in sp and "detailed_description" in sp,
      "system prompt documents the Ref2VA six-section dialect")
check("begin with" not in sp, "system prompt no longer INSTRUCTS the reference tag")
check("never write a bare" in sp, "system prompt explicitly FORBIDS the undocumented tag")

print("\nLOAD CHARACTER · dict / path / slug")
check(D.load_character(CHAR) is CHAR, "a dict passes straight through")
_tmp = tempfile.mkdtemp(prefix="director_chars_")
_p = os.path.join(_tmp, "ch.json")
with open(_p, "w") as fh:
    json.dump({"name": "Zed"}, fh)
check(D.load_character(_p)["name"] == "Zed", "a json path loads")
_d = os.path.join(_tmp, "a_dir")
os.makedirs(_d)
with open(os.path.join(_d, "character.json"), "w") as fh:
    json.dump({"name": "Qux"}, fh)
try:
    _dir_ok = D.load_character(_d)["name"] == "Qux"
except Exception:
    _dir_ok = False
check(_dir_ok, "a character DIRECTORY resolves to character.json")

print("\nVISION QC · two images, verdict schema")
f = Fake([chat({"verdict": "ok", "reasons": []})])
d = D.Director(host="http://stub"); d._post = f
_png = os.path.join(_tmp, "x.png")
with open(_png, "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n" + b"pixels" * 4)
qc = d.qc_frame(_png, _png, "head stays colossal")
check(qc["verdict"] == "ok", "qc_frame returns the verdict")
_imgs = f.calls[0][1]["messages"][-1].get("images")
check(isinstance(_imgs, list) and len(_imgs) == 2, "qc_frame sends the still AND the frame")

print("\nPOLISH · engine-aware single-prompt rewrite")
# schema
check(D.validate({"prompt": "x"}, D.POLISH_SCHEMA) == [], "a {prompt} object validates")
check(any("prompt" in e for e in D.validate({}, D.POLISH_SCHEMA)),
      "POLISH_SCHEMA requires the prompt key")

# h3 r2v: NO undocumented tag, and the request carries the Ref2VA spec
f = Fake([chat({"prompt": "he crosses the car park, hands in pockets"})])
d = D.Director(host="http://stub"); d._post = f
_p = d.polish("man walks", engine="h3", video_mode="r2v")
check(not _p.startswith("<Picture 1> is the character."),
      "polish h3/r2v does NOT prepend the undocumented tag (%r)" % _p[:40])
check(f.calls[0][0] == "/api/chat", "polish posts to /api/chat")
_rule = f.calls[0][1]["messages"][-1]["content"]
check("subject_definitions" in _rule and "retention_analysis" in _rule
      and "detailed_description" in _rule,
      "the r2v Ref2VA six-section rule is in the request")
check("begin with" not in _rule, "the request no longer INSTRUCTS the undocumented tag")
check("do NOT write a bare" in _rule, "the request explicitly FORBIDS the undocumented tag")

# the model echoes the OLD tag AND still-photo phrasing -> tag gone, phrases stripped
f = Fake([chat({"prompt": "<Picture 2> is the character. looking straight at the camera, "
                          "85mm lens, he walks"})])
d._post = f
_p = d.polish("walk", engine="h3", video_mode="r2v")
check("<Picture" not in _p, "the model's own tag is stripped (%r)" % _p)
check("85mm lens" not in _p and "looking straight at the camera" not in _p,
      "still-photo phrases are stripped from an H3 video prompt")

# cont: NO tag, even if the model adds one
f = Fake([chat({"prompt": "<Picture 1> is the character. he turns to the window"})])
d._post = f
_p = d.polish("continue", engine="h3", video_mode="cont")
check("<Picture" not in _p, "polish h3/cont strips the tag (%r)" % _p)

# qwen STILL: photo language is KEPT
f = Fake([chat({"prompt": "portrait, 85mm lens, sharp focus, soft window light"})])
d._post = f
_p = d.polish("a man", engine="qwen", video_mode=None)
check("85mm lens" in _p and "sharp focus" in _p,
      "polish for a qwen STILL keeps the photographic language (%r)" % _p)

# wan: 16 fps dialect note goes into the request
f = Fake([chat({"prompt": "a wide shot at dusk"})])
d._post = f
_p = d.polish("sunset", engine="wan", video_mode=None)
check("16 fps" in f.calls[0][1]["messages"][-1]["content"],
      "the WAN prompt dialect note is sent")

print("\nNEGATIVES · H3 has no negative conditioning, so constraints must be PROSE")
# forge's /object_info: MiniMaxH3ReferenceToVideo takes NO `negative_prompt` (the Qwen image
# nodes do), and the H3 sampler is a BasicGuider at cfg 1.0. So the only mechanism is prose —
# and it belongs at the TAIL of detailed_description, NOT as a seventh section.
_r2v_six = ("subject_definitions:\n<Subject 1> is him in <Picture 1>.\n"
            "summary:\nA tracking shot.\nretention_analysis:\nfully_preserved\n"
            "detailed_description:\n[Shot 1] He walks across the yard.\n"
            "overall_soundscape:\nRoom tone.\nnon_diegetic_music:\nN/A")
_neg = D.inject_negatives(_r2v_six)
check(D.HOUSE_NEGATIVES.strip() in _neg, "the constraint clause IS injected")
check(_neg.index(D.HOUSE_NEGATIVES.strip()) < _neg.index("overall_soundscape:"),
      "negatives land INSIDE detailed_description, before overall_soundscape")
check(_neg.index("detailed_description:") < _neg.index(D.HOUSE_NEGATIVES.strip()),
      "and after the detailed_description label")
check(D.inject_negatives(_neg) == _neg, "inject_negatives is IDEMPOTENT (no double clause)")
check(D.missing_ref2va_sections(_neg) == [],
      "all six sections still present after injection (%s)" % D.missing_ref2va_sections(_neg))
check(D.inject_negatives("he crosses the car park").endswith(D.HOUSE_NEGATIVES.strip()),
      "a PLAIN prose prompt (no sections) gets the clause appended at the end")
_orphan = "summary:\nA shot.\nnon_diegetic_music:\nN/A"     # labels but NO detailed_description
check(D.inject_negatives(_orphan) == _orphan,
      "a Ref2VA prompt with no detailed_description is LEFT ALONE (report, never invent a section)")

d = D.Director(host="http://stub")          # fresh: do not inherit the previous block's stub
_sh = d._enforce_dialect({"intent": "character_action", "prompt": _r2v_six})
check(D.HOUSE_NEGATIVES.strip() in _sh["prompt"] and _sh.get("negatives_injected") is True,
      "_enforce_dialect injects the negatives for an H3 shot")
check(d._enforce_dialect({"intent": "character_action", "prompt": _r2v_six}).get(
      "negatives_injected") is True, "and flags it as injected")
_mc2 = d._enforce_dialect({"intent": "motion_copy", "prompt": "hold the pose"})
check("negatives_injected" not in _mc2,
      "Animate 2 is NOT given the clause (its prompt stays minimal)")
_q = d._enforce_dialect({"intent": "establishing", "prompt": "an empty office at dawn"})
check(D.HOUSE_NEGATIVES.strip() in _q["prompt"],
      "an H3 t2v prompt gets the clause appended (no sections to respect)")

f = Fake([chat({"prompt": _r2v_six})])
d = D.Director(host="http://stub"); d._post = f
_p = d.polish("walk", engine="h3", video_mode="r2v")
check(D.HOUSE_NEGATIVES.strip() in _p, "polish() injects the negatives for h3/r2v")
check(D.missing_ref2va_sections(_p) == [],
      "polish() output is still six-section conformant (%s)" % D.missing_ref2va_sections(_p))
f = Fake([chat({"prompt": "portrait, 85mm lens, soft window light"})])
d._post = f
_p = d.polish("a man", engine="qwen", video_mode=None)
check(D.HOUSE_NEGATIVES.strip() not in _p,
      "a Qwen STILL does NOT get the video prose clause (it has a real negative_prompt field)")

print("\nSEAMLESS ONE-TAKE DIALECT")
# The repairs must be provable without touching the model — the instruction alone does NOT bind
# it (measured: a live call returned retention_analysis written as the SECTION LABELS, and
# "fine 35mm film grain" in a piece specified as phone footage).
_malformed = (
    "subject_definitions: <Subject 1> is X in <Picture 1>: a man.\n"
    "summary: One take.\n"
    "retention_analysis: subject_definitions: fully_preserved\nsummary: fully_preserved\n"
    "detailed_description: [Shot 1] A handheld shot with fine 35mm film grain.\n"
    "overall_soundscape: Wind and traffic.\n"
    "non_diegetic_music: N/A"
)
_r = D._repair_seamless_prompt(_malformed)
check("<Subject 1> (appears in [Shot 1]): fully_preserved" in _r,
      "a label-as-value retention_analysis is REBUILT into the documented line form")
check("subject_definitions: fully_preserved" not in _r,
      "the bogus label-as-value line is gone")
check("35mm film grain" not in _r and "digital sensor noise" in _r,
      "film language is swapped for sensor language (this piece is a phone)")
check("No human speech and no voices." in _r,
      "the no-speech clause is APPENDED when the soundscape lacks it")

_good = ("retention_analysis: <Subject 1> (appears in [Shot 1]): fully_preserved - identity.\n"
         "detailed_description: [Shot 1] A phone shot.\n"
         "overall_soundscape: Wind, traffic. No human speech and no voices.\n")
_g = D._repair_seamless_prompt(_good)
check(_g.count("No human speech") == 1,
      "an already-correct soundscape is NOT double-claused (idempotent)")
check("<Subject 1> (appears in [Shot 1]): fully_preserved - identity." in _g,
      "a correct retention_analysis is left alone")

_clean = D.Director(host="http://stub")
_cp = _clean._clean_seamless_prompt(
    "<Picture 1> is the character. subject_definitions: <Subject 1> is X in <Picture 1>: a man.\n"
    "summary: s\nretention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - x\n"
    "detailed_description: [Shot 1] phone.\noverall_soundscape: wind. No human speech and no voices.\n"
    "non_diegetic_music: N/A")
check("is the character" not in _cp, "the undocumented tag is stripped from a seamless prompt")
check(D.missing_ref2va_sections(_cp) == [],
      "a seamless prompt survives cleaning still six-section conformant (%s)"
      % D.missing_ref2va_sections(_cp))
check(D.HOUSE_NEGATIVES.strip() in _cp, "the H3 negatives are injected into a seamless prompt")
check(D.SEAMLESS_SCHEMA["required"] == ["title", "logline", "card", "prompt"],
      "the seamless schema requires the card (the only dialogue these pieces have)")

_leak = ("retention_analysis: <Subject 1> (appears in [Shot 1]): fully_preserved - x\n"
         "detailed_description: [Shot 1] A phone shot. He conducts. NEVER put a stranger's face "
         "in the foreground. NO DIALOGUE AT ALL. No spoken lines, no <d> tags, no narration. "
         "overall_soundscape is phone-microphone street ambience, and non_diegetic_music is N/A. "
         "The crowd stays back.\n"
         "overall_soundscape: Wind. No human speech and no voices.\n")
_lk = D._repair_seamless_prompt(_leak)
check("<d>" not in _lk, "the echoed '<d> tags' line is dropped from the prompt")
check("DIALOGUE AT ALL" not in _lk, "the echoed 'NO DIALOGUE AT ALL' line is dropped")
check("overall_soundscape is" not in _lk.split("overall_soundscape:")[0],
      "an echoed 'overall_soundscape is ...' sentence is dropped from detailed_description")
check("He conducts." in _lk and "The crowd stays back." in _lk,
      "genuine shot sentences SURVIVE the leak cleanup")
check(re.search(r"<d>|</d>|\(S\d\)", _lk) is None,
      "after the cleanup a silent prompt no longer trips a dialogue check")

_dup = ("subject_definitions: <Subject 1> is X in <Picture 1>: a man.\n"
        "summary: s\n"
        "retention_analysis: <Subject 1> (appears in [Shot 1]): fully_preserved - x\n"
        "detailed_description: fully_preserved;. No on-screen text, lettering, logos.\n"
        "detailed_description: [Shot 1] " + ("A man conducts in the square with a crowd behind him. " * 30) + "\n"
        "overall_soundscape: Wind. No human speech and no voices.\n"
        "non_diegetic_music: N/A\n")
check(D.seamless_dd_words(_dup) >= 150,
      "the LONGEST detailed_description block is measured, not the first (%d words)"
      % D.seamless_dd_words(_dup))
check(D.seamless_prompt_ok(_dup) is True,
      "a duplicated label with a real second body is still accepted")
_short = ("subject_definitions: <Subject 1> is X in <Picture 1>: a man.\nsummary: s\n"
          "retention_analysis: <Subject 1> (appears in [Shot 1]): fully_preserved - x\n"
          "detailed_description: [Shot 1] A phone shot.\n"
          "overall_soundscape: Wind. No human speech and no voices.\nnon_diegetic_music: N/A\n")
check(D.seamless_prompt_ok(_short) is False,
      "a stub detailed_description is REJECTED before it costs a render")
check(D.seamless_prompt_ok("just some prose with no labels") is False,
      "a section-less blob is REJECTED")

_dedup = ("subject_definitions: <Subject 1> is X in <Picture 1>: a man.\nsummary: s\n"
          "retention_analysis: <Subject 1> (appears in [Shot 1]): fully_preserved - x\n"
          "detailed_description: fully_preserved;. stubbjunk.\n"
          "detailed_description: [Shot 1] " + ("Real body sentence about the shot. " * 30) + "\n"
          "overall_soundscape: Wind. No human speech and no voices.\nnon_diegetic_music: N/A\n")
_dd = D._repair_seamless_prompt(_dedup)
check(_dd.lower().count("detailed_description") == 1,
      "a duplicated detailed_description label is collapsed to ONE (got %d)"
      % _dd.lower().count("detailed_description"))
check("stubbjunk" not in _dd, "the stub body is dropped")
check("Real body sentence" in _dd, "the REAL body survives")
check(D.seamless_dd_words(_dd) >= 150, "the surviving body is the long one")

print("\nunload_all · frees EVERY resident model, not just this client's")
dr = D.Director()
dr._get = lambda path, *a, **k: {"models": [{"name": "a:1", "size_vram": 1},
                                           {"name": "b:2", "size_vram": 2}]}
_sent = []
dr._post = lambda path, payload, **k: (_sent.append(payload), {"done": True})[1]
_freed = dr.unload_all()
check(_freed == ["a:1", "b:2"], "unload_all returns every resident name (%r)" % _freed)
check([p.get("model") for p in _sent] == ["a:1", "b:2"],
      "and asks the host to release each one (%r)" % [p.get("model") for p in _sent])
check(all(p.get("keep_alive") == 0 for p in _sent), "every unload uses keep_alive 0")

dr2 = D.Director()
dr2._get = lambda path, *a, **k: {"models": []}
_sent2 = []
dr2._post = lambda path, payload, **k: (_sent2.append(payload), {"done": True})[1]
_freed2 = dr2.unload_all()
check(_freed2 == [dr2.model],
      "with nothing listed it still releases its OWN model (%r)" % _freed2)

print("\nLYRICS — only the words that get sung")
GOOD = ("[Verse]\nRust on the fender cap\nMud in the gear shift track\n\n"
        "[Chorus]\nThrow it out, he says\nBut it stays in the ways\n\n"
        "[Outro]\nIt stays in the ways\n")
check(D.validate_lyrics(GOOD) == [], "a proper lyric block passes clean")
check(any("empty" in e for e in D.validate_lyrics("")), "empty lyrics are rejected")
check(any("section tags" in e for e in
          D.validate_lyrics("just prose\nwith no tags\nand four\nlines here")),
      "lyrics with no section tags are rejected")
check(any("Chorus" in e for e in
          D.validate_lyrics("[Verse]\naa bb\ncc dd\nee ff\ngg hh\n")),
      "a song with no [Chorus] is rejected \u2014 a hook that never comes back is not a song")
check(any("has no lines" in e for e in
          D.validate_lyrics("[Verse]\naa bb\ncc dd\n\n[Chorus]\n\n[Outro]\nfinal line here\n")),
      "a section tag with NOTHING under it is rejected (the 'silent script' failure)")
check(any("sung lines" in e for e in D.validate_lyrics("[Verse]\none line\n\n[Chorus]\ntwo line\n")),
      "too few sung lines is rejected")
check(any("musical directions" in e for e in
          D.validate_lyrics("[Verse]\nplay it at 96 BPM\n(guitar solo)\n\n[Chorus]\nla la la\nla la la\n")),
      "BPM and stage directions in the words are rejected \u2014 they would be SUNG")
check(not any("musical directions" in e for e in
              D.validate_lyrics("[Verse]\nhe played guitar in a bar band\nnever made a dime\n\n"
                                "[Chorus]\nand the song goes on\nand the song goes on\n")),
      "but the word 'guitar' inside a LYRIC does not trip the direction check")

print("\nLYRICS — the Director call (model stubbed)")
seen = []
DUD = "just prose that is definitely longer than forty characters and has no tags at all"

def _stub(seq):
    calls = {"n": 0}

    def _post(path, payload):
        seen.append(payload)
        msg = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return {"message": {"content": json.dumps({"title": "T", "lyrics": msg})}}
    return _post, calls

dr = D.Director()
dr._post, _c = _stub([DUD, GOOD])
out = dr.lyrics("a guy who hoards old trucks in his driveway", style="lo-fi, 84 BPM")
check(_c["n"] == 2, "a reply the VALIDATOR rejects is retried once (%d call(s))" % _c["n"])
check(out.get("lyrics") == GOOD, "and the corrected reply is what comes back")
check(any("REJECTED" in m.get("content", "") for m in (seen[-1].get("messages") or [])),
      "the retry tells the model WHY it was rejected")
check("84 BPM" in seen[-1]["messages"][-1]["content"],
      "and the STYLE is passed in for the diction")
check("ONLY words that get sung" in seen[-1]["messages"][-1]["content"],
      "the prompt forbids musical directions in the lyrics")

dr2 = D.Director()
dr2._post, _c2 = _stub([GOOD])
seen.clear()
dr2.lyrics("x", seconds=45)
check("about 2 sections" in seen[-1]["messages"][-1]["content"],
      "a 45 s song asks for ~2 sections")
seen.clear()
dr2.lyrics("x", seconds=120)
check("about 4 sections" in seen[-1]["messages"][-1]["content"],
      "a 120 s song asks for ~4 \u2014 the length drives the structure")

dr3 = D.Director()
dr3._post, _ = _stub([DUD])
try:
    dr3.lyrics("x")
    check(False, "a model that never satisfies the validator raises")
except D.DirectorError as exc:
    check("could not write usable lyrics" in str(exc),
          "a model that never satisfies the validator raises (%s)" % str(exc)[:56])

print("\nDIALOGUE SCHEMA · accept")
GOOD_DIALOGUE = {
    "title": "The Return",
    "lines": [
        {"speaker": "ALICE", "direction": "", "text": "Are you going to complain?"},
        {"speaker": "BOB", "direction": "", "text": "To whom? It is an app."},
        {"speaker": "ALICE", "direction": "holding the receipt", "text": "I'm writing them a letter."},
    ],
}
check(D.validate(GOOD_DIALOGUE, D.DIALOGUE_SCHEMA) == [],
      "a well-formed dialogue object validates")
_just_title = {"title": "X", "lines": []}
check(any("minItems" in e for e in D.validate(_just_title, D.DIALOGUE_SCHEMA)),
      "empty lines array trips minItems")
_missing_speaker = {"title": "X", "lines": [{"direction": "", "text": "Hi"}]}
check(any("required" in e for e in D.validate(_missing_speaker, D.DIALOGUE_SCHEMA)),
      "a line missing required 'speaker' is rejected")
check(any("required" in e for e in D.validate({}, D.DIALOGUE_SCHEMA)),
      "a bare object is rejected for missing 'title' and 'lines'")

print("\nvalidate_dialogue · accept")
check(D.validate_dialogue(GOOD_DIALOGUE, {"ALICE": "b", "BOB": "b"}) == [],
      "good dialogue passes with matching cast")
check(D.validate_dialogue(GOOD_DIALOGUE, {"ALICE": "b", "BOB": "b", "CAROL": "b"}) == [],
      "passes even when 1 cast member is unused")

print("\nvalidate_dialogue · reject")
_short = {"title": "T", "lines": [{"speaker": "ALICE", "direction": "", "text": "Hi"}]}
check(any("at least 2 lines" in e for e in D.validate_dialogue(_short, {"ALICE": "b"})),
      "a single line is rejected")
_cast_miss = {"title": "T", "lines": [
    {"speaker": "ALICE", "direction": "", "text": "Hi"},
    {"speaker": "ZED", "direction": "", "text": "Hello back"},
]}
check(any("not in the cast" in e for e in D.validate_dialogue(_cast_miss, {"ALICE": "b"})),
      "a speaker not in the cast is rejected")
_empty_text = {"title": "T", "lines": [
    {"speaker": "ALICE", "direction": "", "text": ""},
    {"speaker": "BOB", "direction": "", "text": "Hello"},
]}
check(any("empty spoken" in e for e in D.validate_dialogue(_empty_text, {"ALICE": "b", "BOB": "b"})),
      "a line with empty text is rejected")
_digits = {"title": "T", "lines": [
    {"speaker": "ALICE", "direction": "", "text": "It cost 30 dollars"},
    {"speaker": "BOB", "direction": "", "text": "Hello"},
]}
check(any("digits" in e for e in D.validate_dialogue(_digits, {"ALICE": "b", "BOB": "b"})),
      "digits in spoken text are rejected")
_stage = {"title": "T", "lines": [
    {"speaker": "ALICE", "direction": "", "text": "I am (sighs) writing a letter"},
    {"speaker": "BOB", "direction": "", "text": "Hello"},
]}
errs = D.validate_dialogue(_stage, {"ALICE": "b", "BOB": "b"})
check(any("stage directions" in e for e in errs),
      "parenthetical in text (%s) is rejected" % errs[:1])
_stage2 = {"title": "T", "lines": [
    {"speaker": "ALICE", "direction": "", "text": "[laughs] Write a letter"},
    {"speaker": "BOB", "direction": "", "text": "Hello"},
]}
errs = D.validate_dialogue(_stage2, {"ALICE": "b", "BOB": "b"})
check(any("stage directions" in e for e in errs),
      "bracketed cue in text (%s) is rejected" % errs[:1])
_one_speaker = {"title": "T", "lines": [
    {"speaker": "ALICE", "direction": "", "text": "Hi there"},
    {"speaker": "ALICE", "direction": "", "text": "I said hi"},
]}
check(any("distinct speakers" in e for e in D.validate_dialogue(_one_speaker, {"ALICE": "b", "BOB": "b"})),
      "only 1 speaker with >= 2 in cast is rejected")

print("\nformat_dialogue")
_lines3 = [
    {"speaker": "ALICE", "direction": "", "text": "Are you going to complain?"},
    {"speaker": "BOB", "direction": "", "text": "To whom? It is an app."},
    {"speaker": "ALICE", "direction": "holding the receipt", "text": "I'm writing them a letter."},
]
_fmt = D.format_dialogue(_lines3)
check("> **ALICE** \u2014 Are you going to complain?" in _fmt,
      "format_dialogue starts with the bold-speaker line (%r)" % _fmt[:50])
check("> **ALICE** \u2014 *(holding the receipt)* I'm writing" in _fmt,
      "a line with a direction has the *(direction)* part")
import dialogue as DI  # noqa: E402
_parsed = DI.parse_script(_fmt)
check(len(_parsed) == 3, "format_dialogue round-trips to 3 lines, got %d" % len(_parsed))
for i, p in enumerate(_parsed):
    s = DI.normalise_speaker(p["speaker"])
    exp = _lines3[i]
    check(s == exp["speaker"] and p["text"] == exp["text"] and p["direction"] == exp["direction"],
          "round-trip line %d: speaker=%r dir=%r text=%r (expected %r/%r/%r)"
          % (i, s, p["direction"], p["text"],
             exp["speaker"], exp["direction"], exp["text"]))

print("\nDIALOGUE \u00b7 Director call (model stubbed)")
_seen = []
_cast = {"ALICE": "b", "BOB": "b"}
_BAD_DLG = {"title": "T", "lines": [
    {"speaker": "ALICE", "direction": "", "text": "Hi"},
    {"speaker": "ALICE", "direction": "", "text": "Hello again"},
]}
_GOOD_DLG_RESP = {"title": "The Return", "lines": [
    {"speaker": "ALICE", "direction": "", "text": "Are you going to complain?"},
    {"speaker": "BOB", "direction": "", "text": "To whom? It is an app."},
    {"speaker": "ALICE", "direction": "holding the receipt", "text": "I'm writing them a letter."},
]}

def _dlg_stub(seq):
    ctr = {"n": 0}
    def _post(path, payload):
        _seen.append(payload)
        msg = seq[min(ctr["n"], len(seq) - 1)]
        ctr["n"] += 1
        return {"message": {"content": __import__("json").dumps(msg)}}
    return _post, ctr

dd = D.Director()
dd._post, _dc = _dlg_stub([_BAD_DLG, _GOOD_DLG_RESP])
out = dd.dialogue("Alice tries to return a parcel", _cast)
check(_dc["n"] == 2, "a bad first reply triggers exactly one retry (%d call(s))" % _dc["n"])
check(out.get("title") == "The Return", "the retry output is what comes back (%r)" % out.get("title"))
check(len(out.get("lines", [])) == 3, "retry output has 3 lines")

_seen.clear()
dd2 = D.Director()
dd2._post, _dc2 = _dlg_stub([_GOOD_DLG_RESP])
dd2.dialogue("x", _cast, seconds=10)
_ask = _seen[-1]["messages"][-1]["content"]
check("about 4 lines" in _ask, "a 10 s dialogue asks for ~4 lines")
_seen.clear()
dd3 = D.Director()
dd3._post, _dc3 = _dlg_stub([_GOOD_DLG_RESP])
dd3.dialogue("x", _cast, seconds=5)
_ask3 = _seen[-1]["messages"][-1]["content"]
check("about 2 lines" in _ask3, "a 5 s dialogue asks for min 2 lines")
check("ALICE" in _ask and "BOB" in _ask,
      "the cast names appear in the prompt")

dd4 = D.Director()
_BAD2 = {"title": "T", "lines": [{"speaker": "X", "direction": "", "text": "H"}, {"speaker": "X", "direction": "", "text": "H2"}]}
dd4._post, _ = _dlg_stub([_BAD2, _BAD2])
try:
    dd4.dialogue("x", _cast)
    check(False, "two bad replies raise DirectorError")
except D.DirectorError as exc:
    check("could not write usable dialogue" in str(exc),
          "two bad replies raise DirectorError (%s)" % str(exc)[:56])

_seen.clear()
dd5 = D.Director()
dd5._post, _ = _dlg_stub([_BAD_DLG, _GOOD_DLG_RESP])
dd5.dialogue("x", _cast)
_retry_feedback = _seen[-1]["messages"][-1]["content"]
check("REJECTED" in _retry_feedback and ("distinct speakers" in _retry_feedback or "speaker" in _retry_feedback),
      "the retry message names the validation problem (%r)" % _retry_feedback[:80])

print("\nPY COMPILE")
import py_compile  # noqa: E402
try:
    py_compile.compile(os.path.join(ROOT, "director.py"), doraise=True)
    check(True, "director.py compiles")
except py_compile.PyCompileError as exc:
    check(False, "director.py compiles: %s" % exc)

print("\n%s — %d checks failed" % ("FAIL" if FAILS else "ALL GREEN", len(FAILS)))
sys.exit(1 if FAILS else 0)
