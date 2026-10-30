#!/usr/bin/env python3
"""GPU-free checks for nightshift.py — tonight's list, the unattended run, the contact sheet.
    python3 tools/nightshift_matrix.py"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_TMP = tempfile.mkdtemp(prefix="nightshift_matrix_")
os.environ["FORGE_LAB_DATA"] = _TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import forge_core as fc  # noqa: E402
fc.configure(data_dir=_TMP)
import quick  # noqa: E402
import nightshift as ns  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


quick.notify = lambda *a, **k: True
MADE, RUNS = [], []
FAIL_ONCE = {"the toaster": 1}
_n = [0]


def fake_make(idea, **kw):
    _n[0] += 1
    j = {"id": "j%d" % _n[0], "idea": idea, "title": idea.title(), "format": kw.get("format"),
         "kw": kw, "state": None}
    MADE.append(j)
    return j


def fake_run(job, director=None, progress=None, stop=None):
    RUNS.append((job["id"], dict(job)))
    if progress:
        progress("filming")
    if FAIL_ONCE.get(job["idea"]):
        FAIL_ONCE[job["idea"]] -= 1
        job["state"], job["error"] = "error", "CardBusy: card 1 is busy"
        return job
    if job["idea"] == "never works":
        job["state"], job["error"] = "error", "RuntimeError: shot 2 failed"
        return job
    d_ = os.path.join(_TMP, "vids", job["id"])
    os.makedirs(d_, exist_ok=True)
    fin = os.path.join(d_, "final.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=180x320:r=24:d=1.5", "-pix_fmt", "yuv420p", fin],
                   check=True)
    open(os.path.join(d_, "cover.png"), "wb").write(b"\x89PNG")
    job["state"], job["final"] = "done", fin
    return job


quick.load = lambda jid: next((j for j in MADE if j["id"] == jid), None)
quick.flagged = lambda job: [(2, ["it stops early"], None)] if job["idea"] == "a cat" else []

print("§1 tonight's list")
check(ns.items() == [], "empty to start")
a = ns.add(["a cat", "the toaster"], {"format": "vlog", "check": True, "quality": "Draft"})
ns.add("never works", {"format": "sketch"})
ns.add("a dog", {"format": "ad"})
check([i["idea"] for i in ns.items()] == ["a cat", "the toaster", "never works", "a dog"] and
      len({i["id"] for i in ns.items()}) == 4, "ideas go on the list in order, each its own")
check(all(i["options"]["check"] is False and i["options"]["check_set"] is False
          for i in ns.items()), "the list is unattended: the review stops are off")
ns.remove(a[1]["id"])
ns.add("the toaster", {"format": "vlog"})
check([i["idea"] for i in ns.items()] == ["a cat", "never works", "a dog", "the toaster"],
      "remove one; add it back at the end")
_t0 = ns.time.time
ns.time.time = lambda: 1700000000.0                # every add in the same millisecond
_a = ns.add(["same ms 0", "same ms 1"], {})
ns.remove(_a[0]["id"])                             # remove one, add one straight back
ns.add("same ms 2", {})
ns.time.time = _t0
check(len({i["id"] for i in ns.items()}) == len(ns.items()),
      "remove one and add one in the same millisecond: every idea keeps its own id")
for i_ in [i for i in ns.items() if i["idea"].startswith("same ms")]:
    ns.remove(i_["id"])

print("§2 the night")
night = ns.run(make_job=fake_make, run_job=fake_run, progress=lambda m: None)
check(ns.items() == [], "every idea left the list as it was made")
st = {v["idea"]: v for v in night["videos"]}
check(st["a cat"]["state"] == "done" and st["a dog"]["state"] == "done" and
      st["a cat"]["flags"] == 1 and st["a dog"]["flags"] == 0,
      "made videos are listed with how many shots might look off")
check(MADE[0]["kw"]["format"] == "vlog" and MADE[0]["kw"]["check"] is False,
      "each video is made with the options it was added with, unattended")
check(st["the toaster"]["state"] == "done" and st["the toaster"].get("retried") and
      [r[0] for r in RUNS].count(st["the toaster"]["job"]) == 2,
      "a video that stopped with an error is tried once more at the end (and made it)")
check(st["never works"]["state"] == "error" and "shot 2 failed" in st["never works"]["error"]
      and [r[0] for r in RUNS].count(st["never works"]["job"]) == 2,
      "...one that fails again is listed with the reason (no endless retries)")
sheet = open(os.path.join(night["dir"], "index.html")).read()
check(sheet.count("<video") == 3 and "1 shot might look off" in sheet and
      "shot 2 failed" in sheet and "3 made, 1 stopped" in sheet,
      "the contact sheet: each video plays on the page, flags and stops shown")
check(ns.last_night()["id"] == night["id"] and
      os.path.exists(os.path.join(night["dir"], "night.json")), "last night is kept")

print("§3 stopping")
ns.add(["one", "two", "three"], {"format": "vlog"})
calls = [0]


def stop_after_one():
    return calls[0] >= 1


def run_and_count(job, **kw):
    calls[0] += 1
    return fake_run(job, **kw)


n2 = ns.run(make_job=fake_make, run_job=run_and_count, stop=stop_after_one)
check(len(n2["videos"]) == 1 and [i["idea"] for i in ns.items()] == ["two", "three"] and
      n2["stopped_early"], "stopped: what's made is kept, the rest stays on the list")
check("stopped early" in open(os.path.join(n2["dir"], "index.html")).read(),
      "...and the sheet says so")
check(n2["dir"] != night["dir"] and len(ns.nights()) == 2 and
      ns.last_night()["id"] == n2["id"], "two nights in the same minute keep both sheets")
ns.clear()
check(ns.items() == [], "clear empties the list")

print("\u00a7 start on launch (START_NOW flag)")
import nightshift as _nsa  # noqa: E402
_nsa.clear()
_ran = []
check(_nsa.autostart(run_fn=lambda **k: _ran.append(1), threaded=False) is False and not _ran,
      "no flag: nothing starts")
open(os.path.join(_nsa.root(), _nsa.START_FLAG), "w").close()
check(_nsa.autostart(run_fn=lambda **k: _ran.append(1), threaded=False) is False and not _ran,
      "a flag with an empty list: nothing starts")
check(not os.path.exists(os.path.join(_nsa.root(), _nsa.START_FLAG)), "...and the flag is used up")
_nsa.add(["an idea"], {"fmt": "sketch"})
open(os.path.join(_nsa.root(), _nsa.START_FLAG), "w").close()
check(_nsa.autostart(run_fn=lambda **k: (_ran.append(1), {"videos": []})[1],
                     threaded=False) is True and _ran == [1],
      "flag + a list: the night starts once")
check(_nsa.autostart(run_fn=lambda **k: _ran.append(1), threaded=False) is False and _ran == [1],
      "a second launch does not start it again")
check("NIGHT DONE" in open(os.path.join(_nsa.root(), "autostart.log")).read(),
      "it logs to nightshift/autostart.log")
_jp = {"id": "qpictures-1", "idea": "pictures job", "pictures_only": True}
MADE.append(_jp)
_nsa.clear()
_nsa.add(["film it"], {"resume": _jp["id"]})
_got = []
_nsa.run(make_job=lambda *a, **k: (_ for _ in ()).throw(AssertionError("made a new job")),
         run_job=lambda job, **k: (_got.append(job), dict(job, state="done"))[1])
check(len(_got) == 1 and _got[0]["id"] == _jp["id"] and _got[0]["pictures_only"] is False,
      "a resume item films the SAME pictures-only job (no new job is made)")
_nsa.clear()
_nsa._RUNNING["on"] = True
open(os.path.join(_nsa.root(), _nsa.START_FLAG), "w").close()
check(_nsa.autostart(run_fn=lambda **k: _ran.append(1), threaded=False) is False and
      os.path.exists(os.path.join(_nsa.root(), _nsa.START_FLAG)),
      "while a night is running a new flag waits (it is not used up)")
_nsa._RUNNING["on"] = False
os.remove(os.path.join(_nsa.root(), _nsa.START_FLAG))
_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "forge_lab.py")).read()
check("_ns.autostart()" in _src and "root.after(4000, _night_autostart)" in _src and
      "root.after(60000, _night_autostart)" in _src,
      "the app checks for the flag a few seconds after launch")
_nsa.clear()

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f_ in FAILS:
    print("  FAILED: %s" % f_)
sys.exit(1 if FAILS else 0)
