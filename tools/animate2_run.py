#!/usr/bin/env python3
"""Run ONE animate2 (Wan Animate 2 motion transfer) job through Forge Lab's own path.

usage: python3 tools/animate2_run.py <character_still.png> <driving_clip.mp4> [tag]

Ref 1 is the character still; the clip is the driving performance. Canvas defaults to
Landscape 832x480 so a landscape driving clip is NOT centre-cropped into a portrait strip
(the graph resizes the drive onto the canvas with a centre crop — portrait truncates the
player). Override with A2_W / A2_H.
"""
import os, sys, json, time, urllib.request, urllib.parse

sys.path.insert(0, os.path.expanduser("~/forge-lab"))
import forge_lab as fl

HOST = "http://forge:8188"
DRAFTS = os.path.expanduser("~/forge-lab/drafts")

still = os.path.abspath(sys.argv[1])
clip = os.path.abspath(sys.argv[2])
tag = sys.argv[3] if len(sys.argv) > 3 else "a2"

W = int(os.environ.get("A2_W", "832"))
H = int(os.environ.get("A2_H", "480"))
LENGTH = int(os.environ.get("A2_LEN", str(fl.ANIMATE2_LENGTH)))
SEED = os.environ.get("A2_SEED")

PROMPT = os.environ.get("A2_PROMPT", "")   # empty is correct: the STILL carries identity

for p, what in ((still, "still"), (clip, "clip")):
    if not os.path.exists(p):
        sys.exit("missing %s: %s" % (what, p))

print("still :", still, fl.image_size(still), flush=True)
print("clip  :", clip, fl.video_probe(clip), flush=True)
print("canvas: %dx%d  length=%d" % (W, H, LENGTH), flush=True)

p = {"prompt": PROMPT, "width": W, "height": H, "length": LENGTH,
     "source_path": clip, "prefix": "forge_lab_animate2"}
if SEED:
    p["seed"] = int(SEED)

pid, seed = fl.submit("animate2", p, ref_paths=[still])
print("SUBMITTED", pid, "seed", seed, flush=True)


def get_json(path):
    with urllib.request.urlopen(HOST + path, timeout=30) as r:
        return json.loads(r.read().decode())


def get_bytes(path):
    with urllib.request.urlopen(HOST + path, timeout=120) as r:
        return r.read()


t0 = time.time()
rec = None
while time.time() - t0 < 2400:
    time.sleep(10)
    try:
        rec = get_json("/history/" + pid).get(pid)
    except Exception as e:
        print("  poll err", e, flush=True)
        continue
    if rec:
        break
    print("  %3ds waiting..." % (time.time() - t0), flush=True)

if not rec:
    sys.exit("timed out waiting for %s" % pid)

print("state:", (rec.get("status") or {}).get("status_str"), flush=True)
print("wall : %.1f s" % (time.time() - t0), flush=True)

outs = rec.get("outputs") or {}
saved = []
# ComfyUI echoes the SOURCE clip back as a node output. Downloading that would clobber the
# render under the same name — the exact collision STATUS.md records for Promoted takes.
src_base = os.path.basename(clip)
for _node, out in outs.items():
    for v in (out.get("videos") or []) + (out.get("gifs") or []) + (out.get("images") or []):
        if v.get("filename") == src_base:
            print("  (skipping source echo %s)" % src_base, flush=True)
            continue
        q = urllib.parse.urlencode({"filename": v["filename"],
                                    "subfolder": v.get("subfolder", ""),
                                    "type": v.get("type", "output")})
        try:
            blob = get_bytes("/view?" + q)
        except Exception as e:
            print("  dl err", e, flush=True)
            continue
        ext = os.path.splitext(v["filename"])[1] or ".mp4"
        dest = os.path.join(DRAFTS, "a2_%s%s" % (tag, ext))
        with open(dest, "wb") as f:
            f.write(blob)
        saved.append(dest)
        print("SAVED %s (%d bytes) <- %s" % (dest, len(blob), v["filename"]), flush=True)

if not saved:
    print("NO OUTPUT FILES. outputs json:", json.dumps(outs)[:600], flush=True)

# whether the run was actually marked success in the app's own index
for it in fl.read_index():
    if it.get("id") == pid:
        print("index entry:", json.dumps({k: it.get(k) for k in
              ("label", "status", "ref", "source", "length", "width", "height",
               "steps", "seed", "seconds")}), flush=True)
print("RUN DONE", flush=True)
