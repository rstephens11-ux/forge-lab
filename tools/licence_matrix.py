#!/usr/bin/env python3
"""GPU-free checks for licences.py — the "paid work" switch blocks non-commercial engines.
    python3 tools/licence_matrix.py"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_TMP = tempfile.mkdtemp(prefix="licence_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import licences as lic  # noqa: E402
import client  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


reached = []
client._ensure_deployed = lambda *a, **k: reached.append("deploy") or (_ for _ in ()).throw(
    RuntimeError("stop here (no rig in tests)"))


def tts(engine):
    try:
        client.tts_batch({"design": [], "clone": [], "voices": {}}, _TMP, engine=engine)
    except lic.PaidWorkBlocked as exc:
        return "blocked: %s" % exc
    except RuntimeError as exc:
        return "went on: %s" % exc
    return "went on"


print("§1 off (the default)")
check(lic.paid_work() is False, "paid work is off by default")
check(tts("fish").startswith("went on") and tts("qwen").startswith("went on"),
      "both voice engines go to the rig")
print("§2 on")
json.dump({"tabs": False, "aspect": "9:16"}, open(lic.settings_path(), "w"))
lic.set_paid_work(True)
s_ = json.load(open(lic.settings_path()))
check(s_ == {"tabs": False, "aspect": "9:16", "paid_work": True},
      "the switch is saved beside the other settings (none lost)")
n0 = len(reached)
r_ = tts("fish")
check(r_.startswith("blocked") and "Qwen3-TTS" in r_ and "non-commercial" in r_ and
      len(reached) == n0, "Fish is refused BEFORE anything reaches the rig, with what to do")
check(tts("qwen").startswith("went on"), "Qwen still works")
check("Made for paid work: yes" in lic.note("qwen") and "NON-COMMERCIAL" in lic.note("fish"),
      "the export note says paid work and each engine's licence")
lic.set_paid_work(False)
check(tts("fish").startswith("went on"), "off again: Fish is allowed")

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
