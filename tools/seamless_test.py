#!/usr/bin/env python3
"""seamless_test.py — ONE 15-second SINGLE-TAKE piece, hand-authored.

The thesis under test: "seamless / hard to tell it's AI" is bought by
  (1) ONE generation instead of 5-6 stitched ones  -> no cuts at all,
  (2) a real CAMERA MOVE (the measured 5.6x motion lever), not "Static camera",
  (3) NO invented dialogue — H3's improvised speech is the loudest AI tell,
  (4) one grade + film grain over the whole thing.

15 s = 362 frames, which is H3's trained ceiling. `shot_params` derives the frame count from
`seconds`, so this needs NO change to the pipeline: a hand-authored single shot just works.

Runs ON FORGE:
    FORGE_LAB_DATA=/home/ryan/forge-studio-data /usr/bin/python3 seamless_test.py <slug> <prompt-file>
"""
import json
import os
import random
import sys
import time

os.environ.setdefault("FORGE_LAB_DATA", "/home/ryan/forge-studio-data")
os.environ.setdefault("FORGE_URL", "http://127.0.0.1:8188")
sys.path.insert(0, "/home/ryan/forge-studio")
sys.path.insert(0, "/home/ryan/forge-studio/audio")

import forge_core as c                                          # noqa: E402
import studio_pipeline as sp                                    # noqa: E402

slug = sys.argv[1]
prompt_file = sys.argv[2]
character = sys.argv[3] if len(sys.argv) > 3 else slug
SECONDS = 15

prompt = open(prompt_file).read().strip()
print("prompt: %d chars, %d words" % (len(prompt), len(prompt.split())))
for sec in ("subject_definitions", "summary", "retention_analysis",
            "detailed_description", "overall_soundscape", "non_diegetic_music"):
    print("  %-22s %s" % (sec + ":", "present" if sec + ":" in prompt else "*** MISSING ***"))

pj = sp.new_project([character], "seamless one-take test", length_s=SECONDS,
                    quality="Final", gpus=2, title="Seamless Test", slug=slug)
pj["shots"] = [sp._shot_migrate({
    "n": 1, "seconds": SECONDS, "intent": "character_action", "engine": "h3",
    "video_mode": "r2v", "camera": "Arc Shot, large amplitude, slow speed",
    "action": "conducts an invisible orchestra", "line": "", "prompt": prompt,
})]
sp.save_project(pj)

sh = pj["shots"][0]
p = sp.shot_params(pj, sh)
print("submit params: %dx%d  frames=%d  steps=%s  gpus=%s  mode=%s"
      % (p["width"], p["height"], p["length"], p["steps"], p["gpus"], p["video_mode"]))
try:
    est = c.estimate_seconds("video", p, [], warm_engine=None)
    print("estimate: %.0f s (%.1f min) on 2 cards" % (est, est / 60.0))
except Exception as exc:
    print("estimate failed: %r" % exc)

print("gpu1 released:", sp.release_gpu1_for_render())
t0 = time.time()
res = sp.render_shot(pj, 1, seed=random.randint(0, 2 ** 31 - 1))
print("render: %s in %.0f s (%.1f min) seed=%s" % (res.get("status"), time.time() - t0,
                                                  (time.time() - t0) / 60.0, res.get("seed")))
if res.get("status") != "done":
    print("meta:", json.dumps(res.get("meta") or {})[:600])
    raise SystemExit(1)
sh["chosen"] = res["pid"]
sp.save_project(pj)
print("TAKE:", sp._chosen_clip_path(pj, sh))
