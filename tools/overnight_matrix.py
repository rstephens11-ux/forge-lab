#!/usr/bin/env python3
"""GPU-free assertions for the OVERNIGHT runner (overnight/runner.py) and its Mac client
(overnight/client.py).

Nothing here touches forge, the GPU, ffmpeg or the network: studio_pipeline, forge_core,
Director, subprocess and the clock are all stubbed. It asserts the runner's LOGIC:

  * the brief LIFECYCLE (queue -> running -> done) and the atomic claim
  * the BUDGET stop (a short whose cumulative estimate would exceed the cap is not run)
  * QC re-render ONCE per bad shot (never a loop)
  * an exception in one short does NOT stop the next
  * the temperature pause (a fake nvidia-smi at 85 C pauses; 70 C does not)
  * importing the runner does NOT import tkinter
  * client.queue_brief writes ATOMICALLY (temp file, then mv)

Run: /opt/homebrew/bin/python3 tools/overnight_matrix.py    (exit 0 = green, 1 = failure)
"""
import importlib.util
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ⚠️ Point forge_lab's DATA at a scratch dir BEFORE anything imports it, so nothing here
# can touch Ryan's real runs/ or characters/.
TMP = tempfile.mkdtemp(prefix="overnight_matrix_")
os.environ["FORGE_LAB_DATA"] = TMP
os.environ.pop("FORGE_AUDIO_LOCAL", None)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "audio"))
sys.path.insert(0, os.path.join(ROOT, "overnight"))

import forge_core as fc            # noqa: E402
fc.configure(data_dir=TMP)

FAILS = []
CHECKS = [0]


def check(cond, msg):
    CHECKS[0] += 1
    print("  %s  %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        FAILS.append(msg)


def load_module(path, name):
    """Compile SOURCE TEXT, never a cached .pyc."""
    with open(path, "rb") as f:
        src = f.read()
    mod = types.ModuleType(name)
    mod.__file__ = path
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDirector:
    def __init__(self):
        self.loaded = 0
        self.unloaded = 0
        self.qc_calls = []
        self.next_verdict = "ok"
        self.verdict_queue = []

    def load(self):
        self.loaded += 1
        return {}

    def unload(self, model=None):
        self.unloaded += 1
        return {}

    def pitch(self, ch, n=5, length_s=20, brief=""):
        return [{"title": "T%d" % i, "logline": "L%d" % i, "hook": "H%d" % i}
                for i in range(n)]

    def qc_frame(self, image, still, expectation):
        self.qc_calls.append((image, still, expectation))
        v = self.verdict_queue.pop(0) if self.verdict_queue else self.next_verdict
        return {"verdict": v, "reasons": ["fake read"]}

    def status(self):
        return {"reachable": True, "loaded": [], "vram_hint": None}


class FakeSP:
    def __init__(self, per_shot=2, est_total=100.0):
        self.director = FakeDirector()
        self.per_shot = per_shot
        self.est_total = est_total
        self.projects = {}
        self.gpus_seen = []
        self.render_all_calls = []
        self.render_shot_calls = []
        self.assemble_calls = []
        self.fail_render_for = set()
        self.n = 0

    def get_director(self):
        return self.director

    def _char_dict(self, slug):
        return {"slug": slug, "name": slug}

    def new_project(self, characters, idea, length_s=20, quality="Draft", title=None,
                    slug=None, gpus=1):
        self.n += 1
        pj = {"slug": slug or ("p%d" % self.n), "title": title, "characters": characters,
              "idea": idea, "length_s": length_s, "quality": quality, "gpus": gpus,
              "pitches": [], "shots": []}
        self.projects[pj["slug"]] = pj
        self.gpus_seen.append(gpus)
        return pj

    def choose_pitch(self, pj, i):
        pj["pitch"] = pj["pitches"][i] if pj.get("pitches") else None

    def write_script(self, pj):
        pj["script"] = {"title": "s"}

    def plan_shots(self, pj):
        pj["shots"] = [{"n": k, "seconds": 3, "prompt": "p%d" % k, "action": "a%d" % k,
                        "chosen": None, "takes": []} for k in range(1, self.per_shot + 1)]

    def voice_lines(self, pj):
        pass

    def estimate(self, pj):
        return {"total_s": self.est_total, "per_shot": []}

    def render_all(self, pj, budget_s=None):
        if pj["slug"] in self.fail_render_for:
            raise RuntimeError("boom on %s" % pj["slug"])
        self.render_all_calls.append(pj["slug"])
        for sh in pj["shots"]:
            sh["chosen"] = "pid-%s-%s" % (pj["slug"], sh["n"])
        return [s["n"] for s in pj["shots"]]

    def render_shot(self, pj, i, seed=-1):
        self.render_shot_calls.append((pj["slug"], i, seed))
        return {"pid": "reroll-%s-%s" % (pj["slug"], i), "seed": seed, "status": "done",
                "meta": {}}

    def assemble(self, pj):
        self.assemble_calls.append(pj["slug"])
        d = os.path.join(TMP, "studio", pj["slug"])
        os.makedirs(d, exist_ok=True)
        mp4 = os.path.join(d, "final.mp4")
        open(mp4, "wb").write(b"x" * 128)
        cover = os.path.join(d, "cover.png")
        open(cover, "wb").write(b"png")
        post = os.path.join(d, "post.txt")
        open(post, "w").write("A title\nA caption\n#one #two")
        return {"mp4": mp4, "cover": cover, "post": post, "captions": None, "starts": {}}

    def _chosen_clip_path(self, pj, sh):
        return os.path.join(TMP, "studio", pj["slug"], "clip_%s.mp4" % sh["n"])

    def project_dir(self, slug):
        return os.path.join(TMP, "studio", slug)

    def shot_params(self, pj, sh):
        return {"prompt": "", "width": 544, "height": 960, "steps": 12, "length": 77, "seed": -1}


class FakeCore:
    def __init__(self):
        self.DATA = TMP
        self.busy = False
        self.character_still = lambda slug: os.path.join(TMP, "still.png")
        self.estimate_seconds = lambda mode, p, refs=None, warm_engine=None: 50.0
        self.video_probe = lambda path: {"seconds": 3.0}

    def get_json(self, path):
        if self.busy:
            return {"queue_running": [{}], "queue_pending": []}
        return {"queue_running": [], "queue_pending": []}


def fake_extract(video, frac, out):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
    return out


def fresh(per_shot=2, est_total=100.0):
    """A loaded runner module with every seam stubbed, plus its fakes."""
    mod = load_module(os.path.join(ROOT, "overnight", "runner.py"), "overnight_runner")
    sp = FakeSP(per_shot=per_shot, est_total=est_total)
    core = FakeCore()
    slept = []
    mod.sp = sp
    mod.core = core
    mod.extract_frame = fake_extract
    mod.read_temps = lambda: {0: 40, 1: 40}
    mod._sleep = lambda s: slept.append(s)
    mod._fakes = {"sp": sp, "core": core, "slept": slept}
    return mod, sp, core, slept


def brief(bid="b1", count=2, budget=1800.0, **kw):
    d = {"id": bid, "character": "bighead", "count": count, "length_s": 8, "quality": "Draft",
         "budget_gpu_s": budget, "ideas": [], "created": 1.0}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
def main():
    print("== no tkinter ==")
    mod, sp, core, slept = fresh()
    check("tkinter" not in sys.modules, "importing the runner does NOT import tkinter")

    print("== budget stop ==")
    # est_total 100, budget 150 -> the SECOND short (100+100) must not run
    mod, sp, core, slept = fresh(est_total=100.0)
    rep = mod.run_brief(brief(count=3, budget=150.0))
    check(sp.render_all_calls == ["b1-s1"],
          "budget 150 stops after one 100 s short (ran %r)" % sp.render_all_calls)
    check(rep["state"] == "budget_stop", "report state is budget_stop (got %r)" % rep["state"])
    check(rep["count_done"] == 1, "count_done is 1 (got %r)" % rep["count_done"])
    check(any("budget stop" in e for e in rep["errors"]), "the budget stop is recorded in errors")
    check(len(rep["shorts"]) == 2 and rep["shorts"][1]["state"] == "budget-stop",
          "the stopped short is present and marked budget-stop")

    print("== budget gate uses REAL gpu seconds, not just estimates ==")
    # estimate says 100 s/short (2 fit in 250), but each short REALLY costs 200 s of GPU.
    # After short 1, real use (200) + next estimate (100) > 250 -> short 2 must NOT run.
    mod, sp, core, slept = fresh(est_total=100.0)
    _clock = [0.0]
    def _fake_time():
        return _clock[0]
    _orig_render = sp.render_all
    def _slow_render(pj, budget_s=None):
        _clock[0] += 200.0
        return _orig_render(pj, budget_s)
    sp.render_all = _slow_render
    mod.time.time = _fake_time
    try:
        rep = mod.run_brief(brief(count=2, budget=250.0))
    finally:
        import time as _t
        mod.time.time = _t.time
    check(len(sp.render_all_calls) == 1,
          "real GPU overrun stops the next short (ran %r)" % sp.render_all_calls)

    print("== budget generous -> all shorts run ==")
    mod, sp, core, slept = fresh(est_total=100.0)
    rep = mod.run_brief(brief(count=2, budget=1800.0))
    check(sp.render_all_calls == ["b1-s1", "b1-s2"], "no stop when the budget allows both")
    check(rep["state"] == "done" and rep["count_done"] == 2, "report state done, 2 shorts")

    print("== the brief carries the 2-card opt-in ==")
    mod, sp, core, slept = fresh(per_shot=2, est_total=100.0)
    rep = mod.run_brief(brief(count=1, budget=1800.0, gpus=2))
    check(sp.gpus_seen == [2], "a gpus=2 brief reaches new_project as gpus=2 (got %r)" % sp.gpus_seen)
    check(rep.get("gpus") == 2, "the report records the card count (got %r)" % rep.get("gpus"))
    mod, sp, core, slept = fresh(per_shot=1, est_total=100.0)
    mod.run_brief(brief(count=1, budget=1800.0))
    check(sp.gpus_seen == [1], "a brief with no gpus field stays on ONE card (got %r)" % sp.gpus_seen)

    print("== a re-roll may only TRADE UP ==")
    mod, sp, core, slept = fresh(per_shot=1, est_total=100.0)
    check(mod.qc_rank("ok") < mod.qc_rank("drift") < mod.qc_rank("broken"),
          "qc_rank orders ok < drift < broken")
    check(mod.qc_rank("something-new") == mod.qc_rank("broken"),
          "an UNKNOWN verdict ranks as unusable, not as ok")
    # 1: re-roll comes back BETTER -> keep it
    mod, sp, core, slept = fresh(per_shot=1, est_total=100.0)
    sp.director.verdict_queue = ["drift", "ok"]
    rep = mod.run_brief(brief(count=1, budget=1800.0))
    row = rep["shorts"][0]["qc"][0]
    check(row.get("kept") == "reroll",
          "a BETTER re-roll is kept (got %r)" % row.get("kept"))
    pj = list(sp.projects.values())[0]
    check("reroll" in str(pj["shots"][0]["chosen"]),
          "the shot points at the re-rolled take (chosen=%r)" % pj["shots"][0]["chosen"])
    # 2: re-roll comes back WORSE -> revert to the original take
    mod, sp, core, slept = fresh(per_shot=1, est_total=100.0)
    sp.director.verdict_queue = ["drift", "broken"]
    rep = mod.run_brief(brief(count=1, budget=1800.0))
    row = rep["shorts"][0]["qc"][0]
    check(row.get("kept") == "original",
          "a WORSE re-roll is REVERTED (got %r)" % row.get("kept"))
    pj = list(sp.projects.values())[0]
    check("reroll" not in str(pj["shots"][0]["chosen"]),
          "the shot points BACK at the original take (chosen=%r)" % pj["shots"][0]["chosen"])
    # 3: equally bad -> no trade, revert (a broken take must never replace a broken take)
    mod, sp, core, slept = fresh(per_shot=1, est_total=100.0)
    sp.director.verdict_queue = ["broken", "broken"]
    rep = mod.run_brief(brief(count=1, budget=1800.0))
    row = rep["shorts"][0]["qc"][0]
    check(row.get("kept") == "original",
          "an EQUALLY-bad re-roll is reverted, never swapped in (got %r)" % row.get("kept"))

    print("== QC re-render exactly once ==")
    mod, sp, core, slept = fresh(per_shot=2, est_total=100.0)
    sp.director.next_verdict = "drift"
    rep = mod.run_brief(brief(count=1, budget=1800.0))
    check(len(sp.render_shot_calls) == 2,
          "2 shots -> exactly 2 re-rolls, never a loop (got %d)" % len(sp.render_shot_calls))
    for r in rep["shorts"]:
        check(all(q.get("rerolled") is True for q in r["qc"]),
              "short %s: every non-ok shot was re-rolled once" % r["i"])
    check(sp.director.qc_calls, "the Director QC was actually consulted")

    print("== QC re-render respects the budget ==")
    # estimate 10 -> the short runs under a 20 s budget; the per-shot re-roll price (50 s,
    # FakeCore.estimate_seconds) does not fit, so the re-roll must be skipped.
    mod, sp, core, slept = fresh(per_shot=1, est_total=10.0)
    sp.director.next_verdict = "broken"
    rep = mod.run_brief(brief(count=1, budget=20.0))
    check(not sp.render_shot_calls, "a zero budget blocks the re-roll (no extra render)")
    check(rep["shorts"][0]["qc"][0]["rerolled"] == "skipped-budget",
          "the re-roll is recorded as skipped-budget")

    print("== an exception in one short does not stop the next ==")
    mod, sp, core, slept = fresh(est_total=100.0)
    sp.fail_render_for = {"b1-s1"}
    rep = mod.run_brief(brief(count=2, budget=1800.0))
    check(rep["shorts"][0]["state"] == "error", "short 1 is marked error")
    check(rep["shorts"][1]["state"] == "done", "short 2 still ran")
    check(sp.assemble_calls == ["b1-s2"], "only the good short was assembled (%r)" % sp.assemble_calls)
    check(rep["count_done"] == 1, "count_done is 1")
    check(any("short 1" in e for e in rep["errors"]), "the short-1 error is recorded")

    print("== temperature pause ==")
    mod, sp, core, slept = fresh()
    mod.read_temps = lambda: {0: 85, 1: 40}
    paused = mod.pause_if_hot(brief_id="b1", short=1)
    check(paused is True, "85 C pauses")
    check(slept == [mod.TEMP_PAUSE_S], "the pause sleeps %ss (got %r)" % (mod.TEMP_PAUSE_S, slept))
    check(mod.hot_gpus({0: 85, 1: 40}) == [0], "hot_gpus names the hot card")
    mod, sp, core, slept = fresh()
    mod.read_temps = lambda: {0: 70, 1: 40}
    check(mod.pause_if_hot(brief_id="b1", short=1) is False and not slept,
          "70 C does not pause")

    print("== brief lifecycle: queue -> running -> done ==")
    mod, sp, core, slept = fresh(est_total=100.0)
    mod.ensure_dirs()
    qp = os.path.join(mod.queue_dir(), "b9.json")
    with open(qp, "w") as f:
        json.dump(brief(bid="b9", count=1, budget=1800.0), f)
    n = mod.process_queue()
    check(n == 1, "process_queue ran one brief (got %r)" % n)
    check(not os.path.exists(qp), "the brief left the queue dir")
    check(os.path.exists(os.path.join(mod.done_dir(), "b9", "report.json")),
          "report.json landed in done/b9")
    check(os.path.exists(os.path.join(mod.done_dir(), "b9", "report.html")),
          "report.html landed in done/b9")
    check(os.path.exists(os.path.join(mod.done_dir(), "b9", "brief.json")),
          "the brief was moved into done/b9/brief.json")
    check(not os.listdir(mod.running_dir()), "the running dir is empty again")
    st = json.load(open(mod.status_path()))
    check(st["state"] == "idle", "the heartbeat returns to idle (got %r)" % st["state"])

    print("== a busy ComfyUI queue defers the brief ==")
    mod, sp, core, slept = fresh(est_total=100.0)
    mod.ensure_dirs()
    qp = os.path.join(mod.queue_dir(), "b10.json")
    with open(qp, "w") as f:
        json.dump(brief(bid="b10", count=1), f)
    core.busy = True
    n = mod.process_queue()
    check(n == 0 and os.path.exists(qp), "a busy queue runs nothing and leaves the brief queued")
    core.busy = False
    n = mod.process_queue()
    check(n == 1 and not os.path.exists(qp), "the same brief runs once the queue drains")

    print("== GPU1 release is CONFIRMED, not assumed ==")
    mod, sp, core, slept = fresh(est_total=100.0)
    rep = mod.run_brief(brief(count=1, budget=1800.0))
    check(rep.get("director_released") is True,
          "a released Director is recorded as released (got %r)" % rep.get("director_released"))

    class StuckDirector:
        def __init__(self):
            self.unloads = 0
        def unload(self, model=None):
            self.unloads += 1
        def status(self):
            return {"loaded": ["still-here"]}

    stuck = StuckDirector()
    released = mod.release_director(stuck, tries=2, wait=1.0)
    check(released is False, "a Director that will not leave is reported as NOT released")
    check(stuck.unloads == 2, "release_director RETRIES the unload (got %d)" % stuck.unloads)

    print("== report contents ==")
    mod, sp, core, slept = fresh(est_total=100.0)
    rep = mod.run_brief(brief(count=1, budget=1800.0))
    check(rep["gpu_seconds_used"] >= 0.0, "the report carries gpu_seconds_used")
    check(rep["budget_gpu_s"] == 1800.0, "the report carries the budget")
    check(rep["temps"], "temperatures were logged into the report")
    html = mod.build_report_html(rep)
    for needle in ("Overnight", "short_1.mp4", "short_1_cover.png", "budget"):
        check(needle in html, "the HTML contact sheet mentions %r" % needle)

    print("== client: atomic brief write ==")
    client = load_module(os.path.join(ROOT, "overnight", "client.py"), "overnight_client")
    calls = []

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    client._ssh = lambda cmd, input=None: (calls.append((cmd, input)) or R())
    bid = client.queue_brief("bighead", count=3, length_s=12, budget_min=30)
    check(len(calls) == 2, "queue_brief makes exactly two ssh calls (got %d)" % len(calls))
    write_cmd, payload = calls[0]
    check("cat >" in write_cmd and ".%s.json.tmp" % bid in write_cmd,
          "the first call streams to a hidden TEMP file: %r" % write_cmd)
    check(json.loads(payload)["budget_gpu_s"] == 1800.0,
          "budget_min 30 -> budget_gpu_s 1800")
    check(json.loads(payload).get("gpus") == 1,
          "queue_brief defaults the brief to ONE card")
    bid2 = client.queue_brief("bighead", gpus=2, brief_id="g2")
    check(json.loads(calls[-2][1]).get("gpus") == 2,
          "queue_brief(gpus=2) writes gpus=2 into the brief the runner reads")
    mv_cmd, _ = calls[1]
    check(mv_cmd.startswith("mv -f") and mv_cmd.endswith("/%s.json" % bid),
          "the second call atomically moves the temp into place: %r" % mv_cmd)

    print("\n%d checks, %d failures" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for m in FAILS:
            print("  FAILED: %s" % m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
