#!/usr/bin/env python3
"""GPU-free proof for Forge Lab Studio Phase 0.

Guards three things, each of which was a real bug or a deliberate refactor:

  A · INDEX HONESTY. submit() used to log `steps` as int(p.get("steps", 25)) for EVERY
      engine, so an ltx run (baked 8 steps) or an animate2 run (baked 6) was recorded as
      25. Now the index records what the graph actually sampled. Also asserts the public
      wait_and_collect() helper exists and drives run_state -> collect.

  B · ENGINE DISPATCH. The module-level dispatch decisions (build_graph / estimate_seconds /
      submit / model_help / kind_of) now test engine_of(mode) == "h3" instead of the broad
      MODES[mode].get("video") flag. Proven behaviour-identical by building the SAME graph
      and computing the SAME estimate with the ORIGINAL file (git show <base>:forge_lab.py,
      imported under another module name) and the NEW one, pinned seeds.

  C · forge_core FACADE. Importing it must NOT pull in tkinter; FORGE_LAB_DATA must move
      DATA/RUNS/CHARACTERS.

No GPU, no network: submit()'s upload/post are monkeypatched and all index writes go to a
throwaway FORGE_LAB_DATA dir, never Ryan's real ~/forge-lab.

    /opt/homebrew/bin/python3 tools/core_matrix.py      # exits 1 on any failure
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)             # so a plain `import forge_core` resolves from the repo root
BASE = "5cb04a1"                      # the pre-Phase-0 baseline commit
FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ⚠️ Point forge_lab at a THROWAWAY data dir BEFORE importing it, so submit()'s index writes
# can never touch Ryan's real ~/forge-lab/runs/index.json.
TMP = tempfile.mkdtemp(prefix="forgecore-p0-")
os.environ["FORGE_LAB_DATA"] = TMP

fl = load("forge_lab", os.path.join(ROOT, "forge_lab.py"))
sys.modules["forge_lab"] = fl            # so `import forge_lab` inside forge_core reuses it

# the ORIGINAL dispatch code, straight out of git
ORIG = os.path.join(TMP, "fl_orig.py")
with open(ORIG, "w", encoding="utf-8") as f:
    f.write(subprocess.check_output(["git", "-C", ROOT, "show", "%s:forge_lab.py" % BASE],
                                    text=True))
flo = load("forge_lab_orig", ORIG)

print("=" * 80)
print("A \u00b7 index honesty — submit() logs the steps the graph actually ran")
print("=" * 80)
print("  data dir (throwaway): %s" % fl.RUNS)

# --- the graph itself must sample the baked count -------------------------------------
_ltx_g, _ = fl.build_graph("ltx", {"prompt": "x", "control_name": "c.mp4",
                                   "width": 1280, "height": 704, "seed": 12345,
                                   "src_frames": 96})
_ltx_steps = [v["inputs"]["steps"] for v in _ltx_g.values() if v["class_type"] == "KSampler"][0]
check(_ltx_steps == 8, "ltx graph samples 8 steps (got %r)" % _ltx_steps)

_ani_g, _ = fl.build_graph("animate2", {"prompt": "x", "ref_names": ["a.png"],
                                        "drive_name": "d.mp4", "width": 480, "height": 848,
                                        "seed": 12345, "length": 81})
_ani_steps = [v["inputs"]["steps"] for v in _ani_g.values()
              if v["class_type"] == "BasicScheduler"][0]
check(_ani_steps == 6, "animate2 graph samples 6 steps (got %r)" % _ani_steps)

# --- submit() must record that, not 25 -------------------------------------------------
_seq = {"n": 0}


def _fake_post(path, obj, timeout=60, base=None):
    _seq["n"] += 1
    return {"prompt_id": "coretest-%d" % _seq["n"]}


fl.upload_image = lambda p: "up_" + os.path.basename(p or "")
fl.post_json = _fake_post

pid_l, _ = fl.submit("ltx", {"prompt": "x", "source_path": "/tmp/src.mp4",
                             "width": 1280, "height": 704, "seed": 12345, "src_frames": 96})
pid_a, _ = fl.submit("animate2", {"prompt": "x", "source_path": "/tmp/drv.mp4",
                                  "width": 480, "height": 848, "seed": 12345, "length": 81},
                     ref_paths=["/tmp/ref.png"])
_idx = {i["id"]: i for i in fl.read_index()}
check(_idx[pid_l]["steps"] == 8,
      "index: ltx run logs 8 steps (got %r)" % _idx[pid_l]["steps"])
check(_idx[pid_a]["steps"] == 6,
      "index: animate2 run logs 6 steps (got %r)" % _idx[pid_a]["steps"])
check(_idx[pid_l]["status"] == "running",
      "index: a fresh headless submit starts as 'running'")

# --- wait_and_collect exists and drives run_state -> collect --------------------------
check(callable(getattr(fl, "wait_and_collect", None)), "wait_and_collect() is a public helper")
fl.run_state = lambda pid: {"state": "done"}
fl.collect = lambda pid: {"id": pid, "status": "success"}
_out = fl.wait_and_collect("coretest-1", timeout=5, poll=1)
check(_out.get("status") == "success",
      "wait_and_collect flips the entry via collect() (got %r)" % _out.get("status"))


print("=" * 80)
print("B \u00b7 engine dispatch through engine_of() \u2014 identical to the original code")
print("=" * 80)

CASES = [
    ("t2i", {"prompt": "x", "width": 1024, "height": 1024, "steps": 25, "seed": 12345}),
    ("edit", {"prompt": "x", "seed": 12345, "ref_names": ["a.png"], "resolution": 1024}),
    ("bg", {"prompt": "x", "seed": 12345, "ref_names": ["a.png"], "resolution": 1024}),
    ("video", {"prompt": "x", "video_mode": "r2v", "ref_names": ["a.png"], "steps": 4,
               "seed": 12345, "seconds": 3.0, "width": 1344, "height": 768}),
    ("video", {"prompt": "x", "video_mode": "i2v", "ref_names": ["a.png"], "steps": 12,
               "seed": 12345, "seconds": 5.0, "width": 1344, "height": 768}),
    ("video", {"prompt": "x", "video_mode": "t2v", "steps": 12, "seed": 12345,
               "seconds": 3.0, "width": 832, "height": 480}),
    ("wan", {"prompt": "x", "wan_mode": "t2v", "seed": 12345, "seconds": 5.0,
             "width": 832, "height": 480, "quality": "Turbo 4 steps"}),
    ("wan", {"prompt": "x", "wan_mode": "i2v", "ref_names": ["a.png"], "seed": 12345,
             "seconds": 5.0, "width": 832, "height": 480, "quality": "Full 20 steps"}),
    ("ltx", {"prompt": "x", "control_name": "c.mp4", "width": 1280, "height": 704,
             "seed": 12345, "src_frames": 96}),
    ("animate2", {"prompt": "x", "ref_names": ["a.png"], "drive_name": "d.mp4",
                  "width": 480, "height": 848, "seed": 12345, "length": 81}),
]

for mode, p in CASES:
    tag = "%s %s" % (mode, p.get("video_mode") or p.get("wan_mode") or "")
    try:
        gn, _ = fl.build_graph(mode, dict(p))
        go, _ = flo.build_graph(mode, dict(p))
        check(json.dumps(gn, sort_keys=True) == json.dumps(go, sort_keys=True),
              "build_graph identical to original: %s" % tag)
    except Exception as e:
        check(False, "build_graph identical to original: %s (raised %r)" % (tag, e))
    try:
        en = fl.estimate_seconds(mode, dict(p))
        eo = flo.estimate_seconds(mode, dict(p))
        check(en == eo, "estimate_seconds identical to original: %s" % tag)
    except Exception as e:
        check(False, "estimate_seconds identical to original: %s (raised %r)" % (tag, e))

# engine_of itself is the single dispatcher
for mode, want in (("t2i", "qwen"), ("edit", "qwen"), ("video", "h3"), ("wan", "wan"),
                   ("ltx", "ltx"), ("animate2", "animate2")):
    check(fl.engine_of(mode) == want, "engine_of(%r) == %r" % (mode, want))


print("=" * 80)
print("C \u00b7 forge_core facade")
print("=" * 80)

_import_code = (
    "import sys; import forge_core as c;"
    "assert 'tkinter' not in sys.modules, 'tkinter WAS imported (%r)' % [m for m in sys.modules if 'tkinter' in m];"
    "assert c.engine_of('video') == 'h3';"
    "assert callable(c.submit) and callable(c.wait_and_collect) and callable(c.build_graph);"
    "import json; print(c.RUNS)"
)
_r = subprocess.run([sys.executable, "-c", _import_code], cwd=ROOT,
                    env=dict(os.environ, FORGE_LAB_DATA=TMP),
                    capture_output=True, text=True)
check(_r.returncode == 0,
      "importing forge_core pulls in NO tkinter + exposes the API (%s)" % (_r.stderr.strip()[-300:] or "clean"))

_r2 = subprocess.run([sys.executable, "-c", "import forge_lab; print(forge_lab.RUNS)"], cwd=ROOT,
                     env=dict(os.environ, FORGE_LAB_DATA="/tmp/x"),
                     capture_output=True, text=True)
check(_r2.stdout.strip() == "/tmp/x/runs",
      "FORGE_LAB_DATA=/tmp/x makes RUNS == /tmp/x/runs (got %r)" % _r2.stdout.strip())

import forge_core as fc
_sub = os.path.join(TMP, "sub")
_info = fc.configure(data_dir=_sub, forge_url="http://127.0.0.1:8188")
check(fc.DATA == os.path.expanduser(_sub), "configure(data_dir) rebinds DATA (got %r)" % fc.DATA)
check(fc.RUNS == os.path.join(os.path.expanduser(_sub), "runs"),
      "configure(data_dir) rebinds RUNS (got %r)" % fc.RUNS)
check(fc._fl.CHARACTERS == os.path.join(os.path.expanduser(_sub), "characters"),
      "configure(data_dir) moves CHARACTERS with DATA")
check(fc._fl.FORGE == "http://127.0.0.1:8188" and fc._fl.F_HOST == "127.0.0.1"
      and fc._fl.F_PORT == 8188, "configure(forge_url) rebinds FORGE host/port")

print("=" * 80)
if FAILS:
    print("FAILED %d check(s):" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")