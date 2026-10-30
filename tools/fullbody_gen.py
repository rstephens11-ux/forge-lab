#!/usr/bin/env python3
"""Generate 4 full-body weirdo candidates on forge via Forge Lab's OWN graph path.

Submits through forge_lab.submit(), so the runs land in runs/index.json and show up
in Forge Lab's Takes exactly like a run from the UI. Polls /history, pulls the PNGs
into ~/forge-lab/drafts/fullbody_*.png.
"""
import os, sys, json, time, urllib.request, urllib.parse

sys.path.insert(0, os.path.expanduser("~/forge-lab"))
import forge_lab as fl

HOST = "http://forge:8188"
DRAFTS = os.path.expanduser("~/forge-lab/drafts")

BASE = ("Full-body studio photograph, the ENTIRE figure head to toe in frame, wide shot, "
        "standing straight, facing the camera, arms relaxed at the sides, feet together. "
        "Completely deadpan neutral expression, absolutely no smile. Seamless plain light-grey "
        "studio backdrop, soft even lighting, photorealistic, sharp focus, 85mm lens. "
        "Ordinary everyday clothing. The comedy is that he is utterly sincere about it. ")

CANDIDATES = [
    ("ears",   "A man in his fifties with comically ENORMOUS protruding ears — great flapping "
               "ears the size of dinner plates sticking straight out sideways from his head. "
               "Otherwise totally ordinary: grey cable-knit cardigan, brown trousers, sensible "
               "brown shoes, thinning hair."),
    ("bighead","A man with a COLOSSAL oversized head — his head is nearly twice as wide as his "
               "shoulders, a great heavy block of a head on a slight, narrow body. Neat short "
               "dark hair, small neat features set in the middle of that vast face. Charcoal "
               "suit jacket, dark trousers, black shoes."),
    ("neck",   "A man with an ABSURDLY long thick neck — a neck a full foot tall and as thick as "
               "a tree trunk, holding his small ordinary head high above his shoulders. Navy "
               "mechanic's coverall worn over a grey t-shirt, work boots."),
    ("pomp",   "A man with a TOWERING black pompadour — a full two feet of glossy black hair "
               "swept up and back off his forehead into a great lacquered wave. Blue denim "
               "jacket over a plain white t-shirt, dark jeans, white sneakers."),
]


def get_json(path):
    with urllib.request.urlopen(HOST + path, timeout=30) as r:
        return json.loads(r.read().decode())


def get_bytes(path):
    with urllib.request.urlopen(HOST + path, timeout=60) as r:
        return r.read()


def main():
    jobs = []
    for tag, body in CANDIDATES:
        prompt = BASE + body
        pid, seed = fl.submit("t2i", {"prompt": prompt, "width": 1024, "height": 576,
                                     "steps": 25, "seed": -1,
                                     "prefix": "forge_lab_fullbody"}, None)
        print("SUBMITTED %-8s %s seed=%s" % (tag, pid, seed), flush=True)
        jobs.append((tag, pid))

    pending = {pid: tag for tag, pid in jobs}
    deadline = time.time() + 1800
    done = {}
    while pending and time.time() < deadline:
        time.sleep(8)
        for pid in list(pending):
            try:
                hist = get_json("/history/" + pid)
            except Exception as e:
                print("  poll err", pid, e, flush=True)
                continue
            rec = hist.get(pid)
            if not rec:
                continue
            status = (rec.get("status") or {})
            tag = pending.pop(pid)
            done[pid] = rec
            print("DONE %-8s %s state=%s" % (tag, pid[:8], status.get("status_str")),
                  flush=True)
        if pending:
            print("  waiting on %d..." % len(pending), flush=True)

    for tag, pid in jobs:
        rec = done.get(pid)
        if not rec:
            print("NO RESULT %s (%s)" % (tag, pid), flush=True)
            continue
        outs = rec.get("outputs") or {}
        n = 0
        for _node, out in outs.items():
            for img in (out.get("images") or []):
                q = urllib.parse.urlencode({"filename": img["filename"],
                                            "subfolder": img.get("subfolder", ""),
                                            "type": img.get("type", "output")})
                try:
                    blob = get_bytes("/view?" + q)
                except Exception as e:
                    print("  dl err", tag, e, flush=True)
                    continue
                n += 1
                dest = os.path.join(DRAFTS, "fullbody_%s.png" % tag)
                with open(dest, "wb") as f:
                    f.write(blob)
                print("SAVED %s (%d bytes) from %s" % (dest, len(blob), img["filename"]),
                      flush=True)
        if not n:
            print("NO IMAGES in outputs for %s -> %s" % (tag, json.dumps(outs)[:300]), flush=True)

    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
