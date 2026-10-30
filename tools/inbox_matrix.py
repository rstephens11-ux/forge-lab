#!/usr/bin/env python3
"""GPU-free checks for picture_inbox.py (Claude's picture requests). Fake rig, temp DATA."""
import json, os, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="inbox-")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import picture_inbox as pi  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


SUB = []


def submit(kind, p, refs=None):
    SUB.append((kind, p, refs))
    pid = "in%03d" % len(SUB)
    os.makedirs(os.path.join(fc.RUNS, pid), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=64x96", "-frames:v", "1",
                    os.path.join(fc.RUNS, pid, "o.png")], check=True)
    return pid, 777


def wait(pid, **k):
    return {"id": pid, "images": [{"file": "o.png"}]}


d = pi.inbox()
check(pi.process_once(submit, wait, d) is None, "an empty inbox does nothing")
json.dump({"kind": "t2i", "prompt": "a robot hound", "width": 704, "height": 1280},
          open(os.path.join(d, "hound.json"), "w"))
check(pi.process_once(submit, wait, d) == "hound", "a t2i request is rendered")
done = json.load(open(os.path.join(d, "hound.done.json")))
check(done["ok"] and os.path.exists(os.path.join(d, "out", "hound.png")) and done["seed"] == 777,
      "the picture lands in inbox/out with its seed")
check(SUB[-1][0] == "t2i" and SUB[-1][1]["prompt"] == "a robot hound" and
      SUB[-1][1]["resolution"] == 1280, "sent to the rig as the app sends a still")
check(not os.path.exists(os.path.join(d, "hound.json")) and
      pi.process_once(submit, wait, d) is None, "rendered once, never twice")
ref = os.path.join(d, "out", "hound.png")
json.dump({"kind": "edit", "prompt": "<image1> in a hall", "refs": [ref]},
          open(os.path.join(d, "put.json"), "w"))
pi.process_once(submit, wait, d)
check(SUB[-1][0] == "edit" and SUB[-1][2] == [ref] and SUB[-1][1].get("custom_size"),
      "an edit request goes with its reference pictures")
json.dump({"kind": "edit", "prompt": "x", "refs": ["/nope.png"]},
          open(os.path.join(d, "bad.json"), "w"))
n = len(SUB)
pi.process_once(submit, wait, d)
bad = json.load(open(os.path.join(d, "bad.done.json")))
check(not bad["ok"] and "missing" in bad["error"] and len(SUB) == n,
      "a missing reference is reported, nothing is sent")
src = open(os.path.join(ROOT, "forge_lab.py")).read()
check("_pi.start()" in src, "the app starts the inbox")
check("'picture_inbox'" in open(os.path.join(ROOT, "Forge Lab.spec")).read(),
      "it is in the build")
print("\n%d checks, %d failed" % (N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
