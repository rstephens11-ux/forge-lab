#!/usr/bin/env python3
"""Does LOCAL H3 actually do audio-driven lip-sync?

Built on the app's PROVEN h3_graph() (r2v, Alice-capable, 24 fps, native audio) with one
injection: `MiniMaxH3AddGuide`, whose own description is

    "Anchor an image, a short clip, audio, or a clip with its soundtrack at any frame of a
     MiniMax H3 video."

and whose `audio` input is "Soundtrack to anchor starting at the same frame index, cropped to
the video's remaining duration".

The guide substitutes for the guider's conditioning, so the sampler still runs the r2v
conditioning — the anchored soundtrack is added on top of it.

    python3 tools/h3_audio_test.py --audio a.wav --image ref.png --validate-only
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

ap = argparse.ArgumentParser()
ap.add_argument("--audio", required=True)
ap.add_argument("--image", required=True)
ap.add_argument("--prompt", default=("A young man looks straight at the camera and sings, "
                                     "mouth and jaw moving with the words, small natural head "
                                     "movements. Static medium close-up, plain background. "
                                     "Modern dress, no text."))
ap.add_argument("--seconds", type=float, default=5.17)
ap.add_argument("--width", type=int, default=544)
ap.add_argument("--height", type=int, default=736)
ap.add_argument("--steps", type=int, default=12)
ap.add_argument("--frame-idx", type=int, default=0,
                help="the frame the soundtrack is anchored at")
ap.add_argument("--seed", type=int, default=771203)
ap.add_argument("--no-guide", action="store_true",
                help="CONTROL: the identical base graph with the audio guide removed. Same "
                     "seed means the base is deterministic, so any difference between the two "
                     "clips IS the guide's effect.")
ap.add_argument("--lora", default=None,
                help="character LoRA — H3 r2v is the ONLY path that carries one")
ap.add_argument("--validate-only", action="store_true")
args = ap.parse_args()

LENGTH = int(round(args.seconds * 24.0))
print("H3 %s: %dx%d, %d frames = %.2f s @24fps, %d steps"
      % ("NO-GUIDE CONTROL" if args.no_guide else "+ AddGuide(audio)",
        args.width, args.height, LENGTH, LENGTH / 24.0, args.steps))
print("audio : %s" % args.audio)
print("image : %s" % args.image)


def build(audio_name, image_name):
    # the app's own proven H3 r2v graph — 24 fps, native audio, LoRA-capable
    g = fl.h3_graph("r2v", args.prompt, [image_name], LENGTH, args.width, args.height,
                    args.steps, args.seed, audio=True, lora=args.lora)
    if args.no_guide:
        return g                      # the control: no LoadAudio, no AddGuide, nothing moved
    g["aud"] = {"class_type": "LoadAudio", "inputs": {"audio": audio_name}}
    g["guide"] = {"class_type": "MiniMaxH3AddGuide",
                  "inputs": {"positive": g["guider"]["inputs"]["conditioning"],
                             "latent": ["cond", 1],
                             "frame_idx": int(args.frame_idx),
                             "vae": ["vae", 0],
                             "audio_vae": ["avae", 0],
                             "audio": ["aud", 0]}}
    # the guide REPLACES the guider's conditioning; nothing else moves
    g["guider"]["inputs"]["conditioning"] = ["guide", 0]
    return g


def validate(g):
    oi = json.loads(urllib.request.urlopen("http://forge:8188/object_info", timeout=180).read())
    bad = []
    for nid, n in g.items():
        ct = n["class_type"]
        if ct not in oi:
            bad.append("%s: NO SUCH NODE %s" % (nid, ct))
            continue
        req = oi[ct]["input"].get("required", {}) or {}
        opt = oi[ct]["input"].get("optional", {}) or {}
        for k in (n.get("inputs") or {}):
            if k not in req and k not in opt and "." not in k:
                bad.append("%s.%s: unknown input" % (nid, k))
        for k in req:
            if k not in (n.get("inputs") or {}):
                bad.append("%s.%s: MISSING required" % (nid, k))
        for k, v in (n.get("inputs") or {}).items():
            if not (isinstance(v, list) and len(v) == 2 and v[0] in g):
                continue
            src = g[v[0]]["class_type"]
            out = (oi.get(src) or {}).get("output") or []
            if v[1] >= len(out):
                bad.append("%s.%s: no output slot %s on %s" % (nid, k, v[1], v[0]))
                continue
            want = req.get(k, opt.get(k))
            if not want:
                continue
            wt = want[0]
            ws = set(wt) if isinstance(wt, list) else {wt}
            got = out[v[1]]
            if "*" in ws or got == "*" or got in ws:
                continue
            bad.append("%s.%s: TYPE want %s got %s from %s.slot%s"
                       % (nid, k, "/".join(sorted(ws)), got, v[0], v[1]))
    return bad


g = build(os.path.basename(args.audio), os.path.basename(args.image))
fl._check_graph(g)
problems = validate(g)
print("\nvalidated %d nodes against forge /object_info" % len(g))
print("\n".join("   " + p for p in problems) if problems else "   no problems, no dangling links")
print("   guide node: %s" % (json.dumps(g["guide"]["inputs"], default=str)[:200]
                                if "guide" in g else "NONE (control)"))
if args.validate_only:
    print("\n--validate-only: nothing submitted.")
    sys.exit(1 if problems else 0)

for f in (args.audio, args.image):
    if not os.path.exists(f):
        sys.exit("missing file: %s" % f)
aup = fl.upload_image(args.audio)
iup = fl.upload_image(args.image)
print("\nstaged: audio=%s image=%s" % (aup, iup))
g = build(aup, iup)
fl._check_graph(g)

t0 = time.time()
r = fl.post_json("/prompt", {"prompt": g, "client_id": fl.CLIENT_ID})
pid = r.get("prompt_id")
if not pid:
    sys.exit("forge returned no prompt_id: %r" % r)
print("submitted %s" % pid[:8])
last = None
while True:
    time.sleep(4)
    st = fl.run_state(pid)
    el = time.time() - t0
    if st["state"] != last:
        print("  %6.1fs  %s %s" % (el, st["state"], st.get("detail", "")), flush=True)
        last = st["state"]
    if st["state"] in ("done", "error", "gone") or el > 1800:
        break
wall = time.time() - t0
hist = fl.get_json("/history/%s" % pid, timeout=60).get(pid) or {}
d = os.path.join(fl.RUNS, pid)
os.makedirs(d, exist_ok=True)
files = []
for _n, out in (hist.get("outputs") or {}).items():
    for v in (out.get("images") or []) + (out.get("videos") or []) + (out.get("gifs") or []):
        from urllib.parse import urlencode
        blob = fl.get_bytes("/view?" + urlencode({"filename": v["filename"],
                                                  "subfolder": v.get("subfolder", ""),
                                                  "type": v.get("type", "output")}))
        dst = os.path.join(d, v["filename"])
        open(dst, "wb").write(blob)
        files.append(dst)
print("wall %.1fs · %d file(s)" % (wall, len(files)))
for f in files:
    print("   ", f)
print("\nsummary: %s" % json.dumps({"pid": pid, "wall_s": round(wall, 1),
                                     "frames": LENGTH, "fps": 24.0, "steps": args.steps,
                                     "size": [args.width, args.height],
                                     "audio_anchored": os.path.basename(args.audio),
                                     "guide_frame_idx": args.frame_idx, "lora": args.lora,
                                     "files": files}, indent=1))