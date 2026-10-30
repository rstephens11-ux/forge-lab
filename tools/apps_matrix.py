#!/usr/bin/env python3
"""GPU-free checks for apps.py — the one-click APPS. The rig is faked (real tiny files via
ffmpeg); what each app SENDS (mode, refs, canvas, prompt, guide audio) is what's checked.
    python3 tools/apps_matrix.py"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
_TMP = tempfile.mkdtemp(prefix="apps-")
os.environ["FORGE_LAB_DATA"] = _TMP
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import apps  # noqa: E402
import dialogue as dlg  # noqa: E402
import scene_engine as se  # noqa: E402
import voicelab as vl  # noqa: E402

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


SUB = []


def fake_submit(kind, p, refs=None):
    SUB.append((kind, dict(p), list(refs or [])))
    pid = "ap%03d" % len(SUB)
    d = os.path.join(fc.RUNS, pid)
    if kind == "video":
        ff(["-f", "lavfi", "-i", "color=c=0x406080:s=160x90:d=1", "-pix_fmt", "yuv420p"],
           os.path.join(d, "clip.mp4"))
    else:
        ff(["-f", "lavfi", "-i", "color=c=0x806040:s=96x160", "-frames:v", "1"],
           os.path.join(d, "o.png"))
    return pid, 1


fc.submit = fake_submit
fc.wait_and_collect = lambda pid, timeout=1800, **k: {
    "id": pid, "images": [{"file": x} for x in os.listdir(os.path.join(fc.RUNS, pid))]}
se.tts_batch = lambda job, out_dir, language="English", engine="qwen": {"items": [
    {"id": i["id"], "out": ff(["-f", "lavfi", "-i", "sine=frequency=300", "-t", "4.2"],
                              os.path.join(out_dir, i["id"].replace(":", "_") + ".wav"))}
    for i in job["design"] + job["clone"]]}
se.transcribe_many = lambda paths, language="English": ["" for _ in paths]

tall = ff(["-f", "lavfi", "-i", "color=c=0x333333:s=720x1280", "-frames:v", "1"],
          os.path.join(_TMP, "tall.png"))
wide = ff(["-f", "lavfi", "-i", "color=c=0x333333:s=1280x720", "-frames:v", "1"],
          os.path.join(_TMP, "wide.png"))
prod = ff(["-f", "lavfi", "-i", "color=c=0xaa2222:s=400x400", "-frames:v", "1"],
          os.path.join(_TMP, "can.png"))

print("§1 the list")
check(len(apps.APPS) >= 7 and len({a["key"] for a in apps.APPS}) == len(apps.APPS),
      "%d apps, unique" % len(apps.APPS))
check(len(apps.MOVES) >= 6 and len(apps.ANGLES) >= 5 and len(apps.EXPRESSIONS) >= 6,
      "camera moves, angles and expressions to pick from")

print("§2 image apps: one Qwen edit each, the picture's own shape")
r = apps.run("outfit", {"image": tall, "text": "a yellow raincoat"})
k, p, refs = SUB[-1]
check(k == "edit" and refs == [tall] and p["custom_size"] and p["height"] > p["width"] and
      p["width"] % 32 == 0 and "yellow raincoat" in p["prompt"] and "<image1>" in p["prompt"]
      and os.path.exists(r["file"]) and r["kind"] == "image",
      "Outfit swap: an edit of the picture, tall stays tall, the new clothes named")
apps.run("outfit", {"image": tall, "image2": prod})
check(SUB[-1][2] == [tall, prod] and "<image2>" in SUB[-1][1]["prompt"],
      "...or from a picture of the outfit (it becomes <image2>)")
apps.run("angle", {"image": wide, "angle": "Profile"})
check(SUB[-1][1]["width"] > SUB[-1][1]["height"] and "side profile" in SUB[-1][1]["prompt"],
      "New angle: wide stays wide; the angle is described in words H3/Qwen read")
apps.run("place", {"image": tall, "text": "a submarine galley"})
check("submarine galley" in SUB[-1][1]["prompt"], "Change the place")
apps.run("face", {"image": tall, "expression": "furious"})
check("furious" in SUB[-1][1]["prompt"], "Change the expression")
bad = False
try:
    apps.run("product", {"image": tall, "text": "drinking"})
except ValueError as exc:
    bad = "product" in str(exc)
check(bad, "Product in hand without a product picture -> a clear message")
apps.run("product", {"image": tall, "image2": prod, "text": "drinking"})
check(SUB[-1][2] == [tall, prod] and "label unchanged" in SUB[-1][1]["prompt"],
      "Product in hand: the product is <image2>, kept exactly")

print("§3 video apps: H3 with the picture as the first frame")
n0 = len(SUB)
r = apps.run("move", {"image": wide, "move": "Orbit", "prompt": "rain starts"})
k, p, refs = SUB[-1]
check(k == "video" and p["video_mode"] == "i2v" and refs == [wide] and
      p["width"] > p["height"] and p["length"] == fc.h3_frames(5) and
      "orbits" in p["prompt"] and "rain starts" in p["prompt"] and r["kind"] == "video",
      "Make it move: i2v from the picture, 16:9 for a wide one, the camera move in words")
apps.run("move", {"image": tall, "move": "Slow push-in"})
check(SUB[-1][1]["height"] > SUB[-1][1]["width"], "...9:16 for a tall picture")
bad = False
try:
    apps.run("talk", {"image": tall, "line": "Hello.", "voice": None})
except ValueError as exc:
    bad = "voice" in str(exc)
check(bad, "Talking photo without a voice -> a clear message")
v = vl.blank("Dale")
wav0 = ff(["-f", "lavfi", "-i", "sine=frequency=200", "-t", "1"], os.path.join(_TMP, "r.wav"))
slug = vl.keep(v, wav0)
r = apps.run("talk", {"image": tall, "line": "Expect a forty percent chance of rain.",
                      "voice": slug})
k, p, refs = SUB[-1]
check(k == "video" and p.get("guide_audio_path") and os.path.exists(p["guide_audio_path"])
      and p["length"] == fc.h3_frames(5) and "lips moving exactly" in p["prompt"],
      "Talking photo: the line in that voice is the lip-sync guide; 4.2 s of speech -> a 5 s "
      "shot")
rec = ff(["-f", "lavfi", "-i", "sine=frequency=220", "-t", "11"], os.path.join(_TMP, "me.m4a"))
n_tb = len(SUB)
apps.run("talk", {"image": tall, "audio": rec})
k, p, refs = SUB[-1]
check(k == "video" and p.get("guide_audio_path") and
      abs(apps.sp._duration(p["guide_audio_path"]) - apps.MAX_TALK) < 0.1 and
      p["length"] == fc.h3_frames(8),
      "Talking photo from YOUR recording: no voice needed; an 11 s clip is cut to one 8 s shot")
bad = False
try:
    apps.run("move", {"image": "/nope.png"})
except ValueError:
    bad = True
check(bad, "no picture -> a clear message")
check(apps.estimate("outfit") < apps.estimate("talk", {"image": tall}),
      "an estimate for every app (a still is quicker than a talking clip)")

print("§4 restyle + make it vertical")
apps.run("restyle", {"image": wide, "style": "Claymation"})
check(SUB[-1][0] == "edit" and "claymation" in SUB[-1][1]["prompt"] and
      SUB[-1][1]["width"] > SUB[-1][1]["height"],
      "Restyle: an edit of the picture in the picked style, its own shape")
_names = [n for n, _w in apps.STYLES]
_grouped = [n for _g, ns in apps.STYLE_GROUPS for n in ns]
check(_names[:10] == ["Claymation", "Anime", "Comic ink", "Oil painting", "3D toy", "70s film",
                      "Felt puppet", "Watercolour", "Pixel art", "Low-poly"]
      and len(_names) >= 45,
      "Restyle keeps the original ten first, plus the new looks (%d in all)" % len(_names))
check(len(set(_names)) == len(_names) and sorted(_grouped) == sorted(_names)
      and len(set(_grouped)) == len(_grouped),
      "every style sits in exactly one picker group, no duplicates")
apps.run("restyle", {"image": wide, "style": "Medieval manuscript"})
check("illuminated medieval manuscript" in SUB[-1][1]["prompt"]
      and "<image1>" in SUB[-1][1]["prompt"],
      "Restyle: a new style's words reach the edit prompt")
_BRANDS = ("pixar", "ghibli", "disney", "lego", "simpsons", "muppet", "marvel", "nintendo")
check(not any(b in (n + " " + w).lower() for n, w in apps.STYLES for b in _BRANDS),
      "no studio or brand names in the styles (public repo)")
ep16 = ff(["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:d=2", "-f", "lavfi", "-i",
           "sine=frequency=330:d=2", "-shortest", "-pix_fmt", "yuv420p"],
          os.path.join(_TMP, "studio", "ep", "ep_final.mp4"))
n_sub = len(SUB)
r = apps.run("vertical", {"video": ep16})
pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                     "-of", "csv=p=0", r["file"]], capture_output=True, text=True).stdout
check(len(SUB) == n_sub and "1080,1920" in pr and "audio" in pr,
      "Make it vertical: 1080x1920 with the sound, made on the Mac (nothing sent to forge)")
check(r["file"].endswith("_vertical.mp4") and os.path.exists(ep16),
      "...saved next to the original, which is kept")
check(any(p_ == ep16 for p_, _l in apps.recent_videos()),
      "finished episodes are offered first in the video picker")
bad = False
try:
    apps.run("vertical", {"video": None})
except ValueError:
    bad = True
check(bad, "no video -> a clear message")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
