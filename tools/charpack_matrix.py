#!/usr/bin/env python3
"""GPU-free assertions for charpack.py — the Forge Lab CHARACTER PACK.

    /opt/homebrew/bin/python3 tools/charpack_matrix.py

No Tk, no forge, no GPU. It stubs forge_core.submit / wait_and_collect, the Director
LLM and the audio client, and runs the REAL build_sheet / pack_refs / bible /
make_voice code against a TEMP data dir, so nothing real is touched.

The load-bearing properties:
  * every view prompt carries <image1> AND the character's ONE absurd feature;
  * a built sheet is RESUMABLE (a second run submits nothing);
  * an RGBA render is FLATTENED to RGB before it is stored (transparent PNGs brick Tk);
  * the r2v reference ORDER and the 9-ref cap;
  * the tag sentence matches the app's own r2v help;
  * make_voice UNLOADS the Director before it runs TTS (GPU1 budget);
  * character.json is byte-identical afterwards.
"""
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TMP = tempfile.mkdtemp(prefix="forge_lab_charpack_")
os.environ["FORGE_LAB_DATA"] = TMP          # ← before import: forge_lab reads it at import
sys.path.insert(0, ROOT)

import charpack as cp  # noqa: E402
import forge_lab as fl  # noqa: E402

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------------------
# tiny PNG fixtures, pure stdlib
# ---------------------------------------------------------------------------
def make_png(path, w, h, color_type, rgb=(180, 120, 80)):
    channels = {2: 3, 6: 4}[color_type]
    raw = b""
    px = bytes(rgb[:3]) + (b"\xff" if channels == 4 else b"")
    for _y in range(h):
        raw += b"\x00" + px * w

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return path


# ---------------------------------------------------------------------------
# a character with an absurd feature (a slider at +3, exactly like bighead)
# ---------------------------------------------------------------------------
CHARS = os.path.join(TMP, "characters")
BIG = os.path.join(CHARS, "bighead")
os.makedirs(BIG, exist_ok=True)
CHAR_JSON = {"name": "Big Head", "slug": "bighead",
             "prompt": "photorealistic, ... a COLOSSAL oversized head, ...",
             "seed": "-1", "picks": {"Head size": 3},
             "note": "Head size +3, everything else ordinary",
             "action": "walks slowly toward the camera", "still": "still.png"}
make_png(os.path.join(BIG, "still.png"), 16, 9, 6)      # hero still (RGBA, like the real one)
with open(os.path.join(BIG, "character.json"), "w") as f:
    json.dump(CHAR_JSON, f, indent=1)
CHAR_BYTES = open(os.path.join(BIG, "character.json"), "rb").read()

# ---------------------------------------------------------------------------
# stub the GPU seams
# ---------------------------------------------------------------------------
CALLS = {"submit": [], "waited": []}
RENDER_CT = 6       # what the fake renderer returns: 6 = RGBA


def fake_submit(mode, p, ref_paths=None):
    CALLS["submit"].append({"mode": mode, "p": dict(p), "refs": list(ref_paths or [])})
    pid = "pid%03d" % len(CALLS["submit"])
    d = os.path.join(cp.core.RUNS, pid)
    os.makedirs(d, exist_ok=True)
    make_png(os.path.join(d, "charpack_00001_.png"), 24, 32, RENDER_CT)
    return pid, int(p.get("seed", -1))


def fake_wait(pid, timeout=1800, poll=3):
    CALLS["waited"].append(pid)
    return {"id": pid, "status": "success",
            "images": [{"file": "charpack_00001_.png", "node": "461", "bytes": 123}]}


cp.core.submit = fake_submit
cp.core.wait_and_collect = fake_wait


# ---------------------------------------------------------------------------
print("\nSHEET · shape")
check(len(cp.SHEET_MAIN_KEYS) == 6, "there are 6 main views (got %d)" % len(cp.SHEET_MAIN_KEYS))
check(cp.SHEET_VIEWS[-1]["key"] == "back-three-quarter" and cp.SHEET_VIEWS[-1].get("optional"),
      "the back three-quarter view is LAST and optional")

print("\nSHEET · build (6 main views, Fast)")
doc = cp.build_sheet("bighead", scale="Fast", seed=100)
check(doc["built"] == 6, "six views were built (got %d)" % doc["built"])
check(len(doc["views"]) == 6, "six views recorded")
_feat = cp.absurd_feature(cp.core.read_character("bighead"))
check(_feat == "a COLOSSAL oversized head", "the absurd feature is read from the slider (got %r)" % _feat)
for v in doc["views"]:
    ok = ("<image1>" in v["prompt"]) and (_feat in v["prompt"])
    check(ok, "view %-22s prompt has <image1> and the absurd feature" % v["key"])
check(all(c["mode"] == "edit" for c in CALLS["submit"]), "every view submits the EDIT mode")
check(all(c["refs"] and c["refs"][0] == cp.core.character_still("bighead") for c in CALLS["submit"]),
      "every view references the hero still as <image1>")
check([v["seed"] for v in doc["views"]] == [100, 101, 102, 103, 104, 105],
      "seeds are pinned per view (base+offset) and recorded")
check(all(os.path.exists(os.path.join(BIG, "sheet", v["file"])) for v in doc["views"]),
      "every recorded file exists on disk")

print("\nSHEET · an RGBA render is FLATTENED to RGB before storing")
_cts = [cp.png_color_type(os.path.join(BIG, "sheet", v["file"])) for v in doc["views"]]
check(all(ct == 2 for ct in _cts),
      "every stored sheet PNG is colour-type 2 (RGB) (got %r)" % (_cts[:3],))
check(cp.png_color_type(os.path.join(cp.core.RUNS, "pid001", "charpack_00001_.png")) == 6,
      "the source render really was RGBA (colour-type 6) — so the flatten did the work")

print("\nSHEET · resumable")
CALLS["submit"].clear()
doc2 = cp.build_sheet("bighead", scale="Fast")
check(CALLS["submit"] == [], "a second run submits NOTHING (all views exist)")
check(doc2["built"] == 0, "a second run builds 0 views")
check(len(doc2["views"]) == 6, "…but still records all 6")
check([v["seed"] for v in doc2["views"]] == [100, 101, 102, 103, 104, 105],
      "the recorded seeds survive a resume")

print("\nSHEET · estimate uses the real canvas, not a 1 MP square")
est = cp.sheet_estimate("bighead", scale="Fast")
check(est["count"] == 6, "the estimate covers the 6 main views")
check(all(r["seconds"] > 0 for r in est["views"]), "every view is priced")
check(any(r["height"] > r["width"] for r in est["views"]),
      "a full-body view is priced on a TALL canvas (the edit mode's default would be square)")

# ---------------------------------------------------------------------------
print("\nREFS · order + cap")
refs = cp.pack_refs("bighead")
check(refs[0] == cp.core.character_still("bighead"), "the hero still is ref 1 (always <Picture 1>)")
check(refs[1].endswith("01_front.png") and refs[2].endswith("02_three-quarter-left.png"),
      "then front, then three-quarter, in sheet order")
check(len(refs) == 7, "hero + 6 main views = 7 refs (got %d)" % len(refs))
check(len(cp.pack_refs("bighead", max_refs=3)) == 3, "max_refs truncates the list")

print("\nREFS · the tag sentence matches the app's own r2v help")
_s3 = cp.ref_tag_sentence(3)
check("<Picture 1>" in _s3 and "<Picture 3>" in _s3, "the sentence names every <Picture N>")
check("BY TAG" in _s3 and "in the order added" in _s3,
      "it uses the app's wording (BY TAG / in the order added)")
_app_help = fl.MODEL_HELP["video:r2v"] + fl.VIDEO_MODES["r2v"]["note"]
check("in the order added" in _app_help and "<Picture 1>" in _app_help,
      "…and the app's own help says the same thing")
check(cp.ref_tag_sentence(99).count("<Picture") == cp.H3_MAX_REFS,
      "the tag sentence is capped at H3_MAX_REFS=%d" % cp.H3_MAX_REFS)

print("\nSHEET · the optional back view builds, and a partial build does NOT clobber the sheet")
doc3 = cp.build_sheet("bighead", views=["back-three-quarter"], scale="Fast", seed=500)
check(doc3["built"] == 1 and any(v["key"] == "back-three-quarter" for v in doc3["views"]),
      "the optional back view builds on request")
check(os.path.exists(os.path.join(BIG, "sheet", "07_back-three-quarter.png")),
      "…and lands in the sheet dir (07_, the ordered slot)")
check(len(doc3["views"]) == 7, "the 6 existing views are CARRIED, not dropped (got %d)" % len(doc3["views"]))
CALLS["submit"].clear()
cp.build_sheet("bighead", views=["front"], scale="Fast", force=True)
check(len(CALLS["submit"]) == 1, "force=True rebuilds an existing view")
check(len(cp.load_sheet("bighead")["views"]) == 7, "…and the other 6 views survive the partial build")
check(len(cp.pack_refs("bighead", max_refs=99)) == 8,
      "with the back view it is 8 refs — still under H3's cap of %d" % cp.H3_MAX_REFS)

# ---------------------------------------------------------------------------
print("\nBIBLE · Director LLM")
EVENTS = []


class FakeDirector:
    def __init__(self, *a, **k):
        pass

    def load(self):
        EVENTS.append("load")

    def unload(self):
        EVENTS.append("unload")

    def bible(self, meta):
        EVENTS.append("bible")
        return {"who": "an ordinary man", "absurd_thing": "his head is colossal",
                "voice": "flat and reasonable", "running_bits": ["hat too small"],
                "never": ["acknowledge the head"]}

    def voice_description(self, meta):
        EVENTS.append("voice_description")
        return "a flat, nasal, middle-aged man, deadpan"


cp.Director = FakeDirector
b = cp.bible("bighead")
check(b["absurd_thing"] == "his head is colossal", "the bible round-trips through json")
check(os.path.exists(os.path.join(BIG, "bible.json")), "bible.json is written")
check(cp.load_bible("bighead")["who"] == "an ordinary man", "load_bible reads it back")
check(EVENTS == ["load", "bible", "unload"], "bible loads then unloads the Director (got %r)" % EVENTS)

print("\nVOICE · TTS after the Director is unloaded")
EVENTS.clear()
AUDIO_CALLS = []


def fake_tts_design(desc, text, out_path, language="English"):
    AUDIO_CALLS.append({"desc": desc, "text": text, "out": out_path})
    EVENTS.append("tts")
    with open(out_path, "wb") as f:          # the rig writes a real wav
        f.write(b"RIFF....WAVE")
    return {"ok": True, "seconds": 1.2}


cp.audio.tts_design = fake_tts_design
vdoc = cp.make_voice("bighead")
check(EVENTS.index("unload") < EVENTS.index("tts"),
      "the Director is UNLOADED before TTS runs (got %r)" % EVENTS)
check(os.path.exists(os.path.join(BIG, "voice.wav")), "voice.wav is written")
check(os.path.exists(os.path.join(BIG, "voice.json")), "voice.json is written")
check(vdoc["ref_text"] and vdoc["description"], "voice.json carries description + ref_text")
check(cp.load_voice("bighead")["description"].startswith("a flat"), "load_voice reads it back")
check(cp.pack_status("bighead")["has_voice"], "pack_status reports the voice")

print("\nVOICE · an explicit description skips the LLM entirely")
EVENTS.clear()
cp.make_voice("bighead", description="a booming voice", sample_line="Hello there.")
check("load" not in EVENTS and "unload" not in EVENTS,
      "no Director is loaded when a description is given (got %r)" % EVENTS)
check(cp.load_voice("bighead")["description"] == "a booming voice", "the given description is used")

# ---------------------------------------------------------------------------
print("\nSTATUS · the pack")
st = cp.pack_status("bighead")
check(st["has_still"] and st["sheet_views"] >= 6 and st["has_bible"] and st["has_voice"],
      "status reports still + sheet + bible + voice (%r)" % st)

print("\nSAFETY · character.json is byte-identical")
_after = open(os.path.join(BIG, "character.json"), "rb").read()
check(_after == CHAR_BYTES, "charpack never rewrote character.json")

print("\nSAFETY · importing charpack does not import tkinter")
_probe = subprocess.run(
    [sys.executable, "-c",
     "import sys, os; os.environ['FORGE_LAB_DATA']=%r; sys.path.insert(0, %r); "
     "import charpack; print('TK' if 'tkinter' in sys.modules else 'NOTK')" % (TMP, ROOT)],
    capture_output=True, text=True, timeout=120)
check(_probe.stdout.strip().endswith("NOTK"),
      "charpack imports Tk-free (%r)" % (_probe.stdout.strip()[-40:],))

# ---------------------------------------------------------------------------
print("\n%s  (%d checks failed)" % ("ALL GREEN" if not FAILS else "FAILURES", len(FAILS)))
if FAILS:
    for f in FAILS:
        print("   FAIL: %s" % f)
sys.exit(1 if FAILS else 0)