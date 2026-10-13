#!/usr/bin/env python3
"""LIVE director run against the real Ollama on forge (LLM only — no GPU0 renders).

    /opt/homebrew/bin/python3 tools/director_live.py

Runs the full cheap pipeline for characters/bighead: pitch -> script -> shots -> post_copy,
plus one qc_frame read (the still against itself), prints per-call timings, saves everything
to ~/forge-lab-work/director-sample.json, then UNLOADS the model so card 2 is released.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import director as D  # noqa: E402

OUT = os.path.expanduser("~/forge-lab-work/director-sample.json")
CHAR = os.path.expanduser("~/forge-lab/characters/bighead")
STILL = os.path.join(CHAR, "still.png")

result = {}
d = D.Director()
print("host=%s model=%s" % (d.host, d.model))
print("status before:", json.dumps(d.status()))


def timed(label, fn):
    t0 = time.time()
    val = fn()
    dt = time.time() - t0
    print("  %-14s %6.1f s" % (label, dt))
    result.setdefault("timings", {})[label] = round(dt, 1)
    return val


try:
    timed("load", d.load)

    pitches = timed("pitch", lambda: d.pitch(CHAR, n=5, length_s=20,
                                             brief="a mundane office worker whose head is colossal"))
    result["pitches"] = pitches
    print("  pitches: %d, first = %r" % (len(pitches), pitches[0]["title"]))

    premise = pitches[0]
    script = timed("script", lambda: d.script(CHAR, premise, length_s=20))
    result["script"] = script
    print("  script: %r, %d beats, button=%r"
          % (script["title"], len(script["beats"]), script["button"]))

    shots = timed("shots", lambda: d.shots(CHAR, script))
    result["shots"] = shots
    print("  shots: %d, total %ds, intents=%s"
          % (len(shots), sum(s["seconds"] for s in shots), [s["intent"] for s in shots]))
    print("  shot1 prompt: %s" % shots[0]["prompt"][:160])

    post = timed("post_copy", lambda: d.post_copy(CHAR, script))
    result["post_copy"] = post
    print("  post title = %r, %d hashtags" % (post["title"], len(post["hashtags"])))

    qc = timed("qc_frame", lambda: d.qc_frame(STILL, STILL,
               "a middle-aged man whose head is colossal relative to his body, plain grey cardigan"))
    result["qc_self"] = qc
    print("  qc (still vs itself) -> %r  %s" % (qc["verdict"], qc["reasons"][:2]))

    result["ok"] = True
except Exception as exc:  # noqa: BLE001
    result["ok"] = False
    result["error"] = "%s: %s" % (type(exc).__name__, exc)
    print("PIPELINE FAILED:", result["error"])
finally:
    timed("unload", d.unload)

with open(OUT, "w") as fh:
    json.dump(result, fh, indent=2)
print("saved", OUT)
print("status after:", json.dumps(d.status()))
sys.exit(0 if result.get("ok") else 1)