#!/usr/bin/env python3
"""Re-render specific shots of a finished Studio project, then re-assemble.

Used to repair ludvig #3, whose shots 5 and 6 read 'broken' before AND after the runner's
re-roll (the runner kept the equally-broken re-roll — see the trade-up fix in runner.py).

Runs ON FORGE:
    FORGE_LAB_DATA=/home/ryan/forge-studio-data /usr/bin/python3 fix_shots.py <slug> <n> [<n> ...]
"""
import json
import os
import random
import sys

os.environ.setdefault("FORGE_LAB_DATA", "/home/ryan/forge-studio-data")
os.environ.setdefault("FORGE_URL", "http://127.0.0.1:8188")
os.environ.setdefault("FORGE_AUDIO_LOCAL", "1")
sys.path.insert(0, "/home/ryan/forge-studio")
sys.path.insert(0, "/home/ryan/forge-studio/audio")

import studio_pipeline as sp                                   # noqa: E402

slug = sys.argv[1]
want = [int(x) for x in sys.argv[2:]]

pj = sp.load_project(slug)
if not pj:
    raise SystemExit("no such project: %s" % slug)
print("project %s | gpus=%s quality=%s target=%ss" %
      (slug, pj.get("gpus"), pj.get("quality"), pj.get("length_s")))
for sh in pj["shots"]:
    if sh["n"] in want:
        print("  shot %s  %.0fs  %s  prompt=%r" %
              (sh["n"], sh["seconds"], sh.get("video_mode"), (sh.get("prompt") or "")[:150]))

# A 2-card render needs BOTH cards, and the Director lives on GPU1.
print("gpu1 released:", sp.release_gpu1_for_render())

for n in want:
    sh = sp._shot(pj, n)
    old = sh.get("chosen")
    seed = random.randint(0, 2 ** 31 - 1)
    res = sp.render_shot(pj, n, seed=seed)
    print("shot %s -> %s (seed %s) [was %s]" % (n, res.get("status"), res.get("seed"), old))
    if res.get("status") == "done":
        sh["chosen"] = res["pid"]          # force the NEW take in
        sp.save_project(pj)
    else:
        print("  !! shot %s failed to render: %r" % (n, (res.get("meta") or {}).get("error")))

print("re-assembling…")
cut = sp.assemble(pj)
print("CUT:", json.dumps(cut, indent=1)[:800])