#!/usr/bin/env python3
"""overnight/runner.py — the OVERNIGHT STUDIO RUNNER. This process runs ON FORGE.

Ryan's Mac sleeps; forge does not. This daemon polls a queue of *briefs* and turns each
one into finished, captioned, 9:16 shorts while nobody is watching:

    brief (Big Head, 3 shorts, 12 s, Draft, max 30 GPU-min)
      -> pitch (once, n = count) -> for each short: script -> shots -> voice
      -> estimate -> render -> Director QC (+ one re-roll) -> assemble
      -> report.json + report.html (a morning contact sheet) -> done/

Everything it needs already exists Tk-free: `studio_pipeline` drives one short, `director`
is the local LLM, `audio/client.py` runs the Qwen TTS/aligner LOCALLY on forge
(FORGE_AUDIO_LOCAL=1), `forge_core` is the Tk-free facade over the shared app.

⚠️ NO tkinter, anywhere. Importing this module must not pull Tk in — asserted by
   tools/overnight_matrix.py and verified on the rig.

GPU discipline (the whole reason this is careful):
  * A brief only STARTS when ComfyUI's /queue is empty — it must never evict someone
    else's render. (It never calls /interrupt or clears the queue.)
  * Between shorts it reads the GPU CORE temperatures with nvidia-smi and PAUSES for
    5 minutes if any core is >= 83 C. (Junction temps are not exposed on Linux; core
    temps are.)
  * GPU1 is shared by the Director LLM and the TTS/aligner jobs. `studio_pipeline`
    UNLOADS the Director before voice_lines(); this runner ALSO unloads it at the end
    of every brief so card 2 is free.

Layout under FORGE_LAB_DATA (default ~/forge-lab, on the rig: /home/ryan/forge-studio-data):

    overnight/queue/<id>.json     briefs waiting   (client.py writes here, atomically)
    overnight/running/<id>.json   the brief being worked
    overnight/done/<id>/          report.json, report.html, brief.json, short_N.mp4 …
    overnight/status.json         heartbeat {state, brief, short, stage, updated, temps}

Run:  /usr/bin/python3 overnight/runner.py          (systemd: forge-studio-runner.service)
      /usr/bin/python3 overnight/runner.py --once   (drain the queue once, then exit)
"""
import argparse
import glob
import json
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, os.path.join(ROOT, "audio")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import forge_core as core            # noqa: E402  (Tk-free facade over forge_lab)
import studio_pipeline as sp         # noqa: E402  (the one-short engine)

POLL_S = float(os.environ.get("OVERNIGHT_POLL_S", "20"))
TEMP_LIMIT_C = int(os.environ.get("OVERNIGHT_TEMP_LIMIT", "83"))
TEMP_PAUSE_S = float(os.environ.get("OVERNIGHT_TEMP_PAUSE_S", "300"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

# The module-level sleep seam — tests patch this instead of waiting 5 real minutes.
_sleep = time.sleep


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def overnight_dir():
    return os.path.join(core.DATA, "overnight")


def queue_dir():
    return os.path.join(overnight_dir(), "queue")


def running_dir():
    return os.path.join(overnight_dir(), "running")


def done_dir():
    return os.path.join(overnight_dir(), "done")


def status_path():
    return os.path.join(overnight_dir(), "status.json")


def ensure_dirs():
    for d in (queue_dir(), running_dir(), done_dir()):
        os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------------------
# Atomic JSON + heartbeat
# ---------------------------------------------------------------------------
def atomic_write_json(path, obj):
    """Write via a temp file in the same dir, then os.replace — never a half-written file."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp" % os.path.basename(path))
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)
    return path


def write_status(state, brief=None, short=None, stage=None, temps=None):
    atomic_write_json(status_path(), {
        "state": state, "brief": brief, "short": short, "stage": stage,
        "updated": time.time(), "temps": temps if temps is not None else read_temps(),
    })


# ---------------------------------------------------------------------------
# GPU safety
# ---------------------------------------------------------------------------
def read_temps():
    """{gpu_index: core_temp_C} from nvidia-smi, or {} when it cannot be read."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return {}
    temps = {}
    for line in (out or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            try:
                temps[int(parts[0])] = int(float(parts[1]))
            except ValueError:
                continue
    return temps


def hot_gpus(temps, limit=TEMP_LIMIT_C):
    """Indices of GPUs whose CORE temp is at or above the limit."""
    return sorted(i for i, t in (temps or {}).items() if t >= limit)


def queue_empty():
    """True only when ComfyUI has nothing running and nothing pending."""
    try:
        q = core.get_json("/queue")
    except Exception:
        return False
    if not isinstance(q, dict):
        return False
    return not q.get("queue_running") and not q.get("queue_pending")


def pause_if_hot(brief_id=None, short=None, limit=TEMP_LIMIT_C, pause_s=TEMP_PAUSE_S):
    """Sleep `pause_s` when any GPU core is >= limit. Returns True when it paused."""
    temps = read_temps()
    hot = hot_gpus(temps, limit)
    if not hot:
        return False
    write_status("paused", brief=brief_id, short=short,
                 stage="temp>=%dC on GPU %s" % (limit, ",".join(str(i) for i in hot)),
                 temps=temps)
    _sleep(pause_s)
    return True


# ---------------------------------------------------------------------------
# Frames (contact sheet + QC)
# ---------------------------------------------------------------------------
def extract_frame(video, frac, out):
    """Write one frame of `video` at `frac` (0..1) to `out`. Returns out or None."""
    if not video or not os.path.exists(video):
        return None
    dur = 1.0
    try:
        p = core.video_probe(video)
        if p and p.get("seconds"):
            dur = float(p["seconds"])
    except Exception:
        pass
    at = max(0.0, min(dur - 0.05, dur * float(frac)))
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        r = subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "%.2f" % at,
                            "-i", video, "-frames:v", "1", out],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# QC — Director reads a mid frame against the character still; ONE re-roll if not ok
# ---------------------------------------------------------------------------
# ⚠️ RANKING, so a re-roll can only ever be a TRADE UP. The old code kept the re-roll
# unconditionally, which shipped a KNOWN-BROKEN take: on the 2026-10-07 batch, ludvig #3
# shot 6 read "broken" before the re-roll and "broken" again after it — and the second,
# equally-broken take was the one that got used. A re-roll that is no better must revert.
_QC_RANK = {"ok": 0, "drift": 1, "broken": 2, "no-clip": 2, "no-frame": 2,
            "no-still": 2, "qc-error": 2}


def qc_rank(verdict):
    """0 = ok, 1 = drift, 2 = broken/unusable. An unknown verdict is treated as unusable."""
    return _QC_RANK.get(str(verdict or "").strip().lower(), 2)


def _shot_estimate(pj, sh):
    try:
        p = sp.shot_params(pj, sh)
        return float(core.estimate_seconds("video", p, [], warm_engine="h3"))
    except Exception:
        return 0.0


def qc_one(director, pj, clip, still, sh):
    """One shot's QC read: extract a mid frame, ask the Director, return a row dict."""
    frame = os.path.join(sp.project_dir(pj["slug"]), "qc", "shot%02d_mid.png" % sh["n"])
    if not extract_frame(clip, 0.5, frame):
        return {"verdict": "no-frame", "reasons": ["could not extract a frame"]}
    if not still:
        return {"verdict": "no-still", "reasons": ["the character has no still"]}
    try:
        v = director.qc_frame(frame, still, sh.get("action") or sh.get("prompt") or "")
    except Exception as exc:
        return {"verdict": "qc-error", "reasons": [repr(exc)]}
    v = v or {}
    return {"verdict": v.get("verdict"), "reasons": v.get("reasons") or []}


def qc_and_reroll(pj, character, budget_s, used_s):
    """QC every shot. When a verdict is not 'ok', re-render that shot ONCE with a new
    seed — but only if the budget still allows it. Returns (rows, extra_gpu_seconds)."""
    still = core.character_still(character)
    d = sp.get_director()
    rows, extra = [], 0.0
    for sh in pj["shots"]:
        clip = sp._chosen_clip_path(pj, sh)
        if not clip:
            rows.append({"shot": sh["n"], "verdict": "no-clip", "reasons": [],
                         "rerolled": False})
            continue
        row = qc_one(d, pj, clip, still, sh)
        row["shot"] = sh["n"]
        row["rerolled"] = False
        if row.get("verdict") != "ok":
            est = _shot_estimate(pj, sh)
            if used_s + extra + est > budget_s:
                row["rerolled"] = "skipped-budget"
            else:
                original = sh.get("chosen")
                seed = random.randint(0, 2 ** 31 - 1)
                t0 = time.time()
                try:
                    res = sp.render_shot(pj, sh["n"], seed=seed)
                except Exception as exc:
                    res = None
                    row["reroll_error"] = repr(exc)
                extra += time.time() - t0
                if res and res.get("status") == "done":
                    sh["chosen"] = res["pid"]            # show qc_one the NEW take
                    new_clip = sp._chosen_clip_path(pj, sh)
                    after = (qc_one(d, pj, new_clip, still, sh) if new_clip
                             else {"verdict": "no-clip", "reasons": []})
                    row["rerolled"] = True
                    row["seed_after"] = res.get("seed")
                    row["verdict_after"] = after.get("verdict")
                    row["reasons_after"] = after.get("reasons")
                    # ⚠️ KEEP THE RE-ROLL ONLY IF IT IS BETTER. Equal-or-worse reverts, so a
                    # re-roll can never replace a usable take with a worse one.
                    if qc_rank(after.get("verdict")) < qc_rank(row.get("verdict")):
                        row["kept"] = "reroll"
                    else:
                        row["kept"] = "original"
                        if original is not None:
                            sh["chosen"] = original
                else:
                    if original is not None:
                        sh["chosen"] = original          # revert: the re-roll failed
                    row["rerolled"] = "failed"
                    row["kept"] = "original"
        rows.append(row)
    return rows, extra


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------
def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""


def release_director(director, tries=3, wait=3.0):
    """Unload the Director AND CONFIRM it left GPU1. Returns True when released.

    A single unload() request is a REQUEST, not a result: if the model is still resident
    (a race with a lingering keep_alive, or a request still finishing) we retry. The
    caller records the outcome, so 'GPU1 released' is a fact in the report, not a hope.
    """
    for _ in range(max(1, tries)):
        try:
            director.unload()
        except Exception:
            pass
        _sleep(wait)
        try:
            st = director.status()
        except Exception:
            return False
        if not (st or {}).get("loaded"):
            return True
    return False


def run_brief(brief):
    """Run ONE brief end to end. Never raises for a per-short failure: the error is
    recorded and the NEXT short still runs."""
    bid = brief["id"]
    character = brief["character"]
    count = max(1, int(brief.get("count", 1)))
    length_s = int(brief.get("length_s", 12))
    quality = brief.get("quality", "Draft")
    gpus = int(brief.get("gpus", 1) or 1)
    budget = float(brief.get("budget_gpu_s", 1800))
    ideas = list(brief.get("ideas") or [])

    report = {"id": bid, "character": character, "count_requested": count, "count_done": 0,
              "length_s": length_s, "quality": quality, "gpus": gpus, "budget_gpu_s": budget,
              "estimate_total_s": 0.0, "gpu_seconds_used": 0.0,
              "started": time.time(), "finished": None, "state": "running",
              "shorts": [], "errors": [], "temps": []}
    report["temps"].append({"t": time.time(), "temps": read_temps()})

    d = sp.get_director()
    try:
        d.load()
    except Exception as exc:
        report["errors"].append("director load: %r" % exc)

    char = sp._char_dict(character)
    pitches = []
    try:
        pitches = list(d.pitch(char, n=count, length_s=length_s,
                               brief=(ideas[0] if ideas else "")))
    except Exception as exc:
        report["errors"].append("pitch: %r" % exc)

    est_used = 0.0
    for i in range(count):
        if i >= len(pitches):
            report["errors"].append("no pitch for short %d — stopping" % (i + 1))
            break
        pitch = pitches[i] or {}
        idea_i = ideas[i] if i < len(ideas) else (ideas[0] if ideas else "")
        row = {"i": i + 1, "slug": "%s-s%d" % (bid, i + 1),
               "title": pitch.get("title"), "logline": pitch.get("logline"),
               "hook": pitch.get("hook"), "estimate_s": None, "render_s": 0.0,
               "reroll_s": 0.0, "qc": [], "mp4": None, "cover": None, "frames": [],
               "post": "", "state": "running", "error": None}
        report["shorts"].append(row)
        write_status("running", brief=bid, short=i + 1, stage="short-start")

        pause_if_hot(brief_id=bid, short=i + 1)

        try:
            pj = sp.new_project([character], idea_i, length_s=length_s, quality=quality,
                                gpus=gpus,
                                title=pitch.get("title"), slug=row["slug"])
            pj["pitches"] = pitches
            sp.choose_pitch(pj, i)

            write_status("running", brief=bid, short=i + 1, stage="script")
            sp.write_script(pj)
            write_status("running", brief=bid, short=i + 1, stage="shots")
            sp.plan_shots(pj)
            write_status("running", brief=bid, short=i + 1, stage="voice")
            sp.voice_lines(pj)

            write_status("running", brief=bid, short=i + 1, stage="estimate")
            est = sp.estimate(pj)
            est_total = float(est.get("total_s") or 0.0)
            row["estimate_s"] = est_total
            report["estimate_total_s"] += est_total
            # ⚠️ gate on the LARGER of (estimates so far, real GPU seconds so far): re-rolls and
            # estimate error both make real time exceed the estimate, and the cap is a hard cap.
            if max(est_used, report["gpu_seconds_used"]) + est_total > budget:
                row["state"] = "budget-stop"
                report["errors"].append(
                    "budget stop before short %d: cumulative estimate %.0fs + %.0fs "
                    "> %.0fs" % (i + 1, est_used, est_total, budget))
                report["state"] = "budget_stop"
                break
            est_used += est_total

            write_status("running", brief=bid, short=i + 1, stage="render")
            t0 = time.time()
            sp.render_all(pj)
            row["render_s"] = time.time() - t0
            report["gpu_seconds_used"] += row["render_s"]

            write_status("running", brief=bid, short=i + 1, stage="qc")
            qc_rows, reroll_s = qc_and_reroll(pj, character, budget,
                                              report["gpu_seconds_used"])
            row["qc"] = qc_rows
            row["reroll_s"] = reroll_s
            report["gpu_seconds_used"] += reroll_s

            write_status("running", brief=bid, short=i + 1, stage="assemble")
            cut = sp.assemble(pj)
            row["mp4"] = cut.get("mp4")
            row["cover"] = cut.get("cover")
            row["post"] = _read_text(cut.get("post"))
            row["state"] = "done"
            report["count_done"] += 1
            report["temps"].append({"t": time.time(), "temps": read_temps()})
        except Exception as exc:
            # ⚠️ NEVER crash the daemon on one bad short — record it and keep going.
            row["state"] = "error"
            row["error"] = repr(exc)
            report["errors"].append("short %d: %r" % (i + 1, exc))

    try:
        collect_artifacts(report)
    except Exception as exc:
        report["errors"].append("collect: %r" % exc)

    # release GPU1 so card 2 is free for whatever comes next — and CONFIRM it
    report["director_released"] = release_director(d)

    report["finished"] = time.time()
    if report["state"] == "running":
        report["state"] = "done"
    return report


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def collect_artifacts(report):
    """Copy each short's mp4 / cover / 3 frames into done/<id>/ so the report is
    self-contained and `client.py fetch` is a single scp."""
    d = os.path.join(done_dir(), report["id"])
    os.makedirs(d, exist_ok=True)
    for row in report["shorts"]:
        mp4 = row.get("mp4")
        if mp4 and os.path.exists(mp4):
            dst = os.path.join(d, "short_%d.mp4" % row["i"])
            shutil.copyfile(mp4, dst)
            row["mp4_rel"] = os.path.basename(dst)
            cov = row.get("cover")
            if cov and os.path.exists(cov):
                cdst = os.path.join(d, "short_%d_cover.png" % row["i"])
                shutil.copyfile(cov, cdst)
                row["cover_rel"] = os.path.basename(cdst)
            frames = []
            for k, frac in enumerate((0.1, 0.5, 0.9), start=1):
                f = os.path.join(d, "short_%d_f%d.png" % (row["i"], k))
                if extract_frame(mp4, frac, f):
                    frames.append(os.path.basename(f))
            row["frames"] = frames


def _esc(s):
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_report_html(report):
    """The morning contact sheet: cover + 3 frames per short, title, logline, post text,
    QC verdicts, GPU seconds vs budget, and links to the mp4 files."""
    rows = []
    for row in report["shorts"]:
        imgs = []
        if row.get("cover_rel"):
            imgs.append('<img src="%s" title="cover">' % _esc(row["cover_rel"]))
        for f in (row.get("frames") or []):
            imgs.append('<img src="%s">' % _esc(f))
        qc = "".join(
            "<li>shot %s — <b>%s</b>%s%s</li>" % (
                _esc(q.get("shot")), _esc(q.get("verdict")),
                (" (re-rolled)" if q.get("rerolled") is True else
                 (" (%s)" % _esc(q.get("rerolled")) if q.get("rerolled") else "")),
                (": " + _esc("; ".join(q.get("reasons") or []))
                 if q.get("reasons") else ""))
            for q in (row.get("qc") or []))
        link = ('<a href="%s">%s</a>' % (_esc(row["mp4_rel"]), _esc(row["mp4_rel"]))
                if row.get("mp4_rel") else "<i>no file</i>")
        rows.append("""
  <section class="short">
    <h2>%s. %s</h2>
    <p class="logline">%s</p>
    <div class="strip">%s</div>
    <p class="meta">state %s &middot; estimate %s s &middot; render %.0f s &middot; re-roll %.0f s
       &middot; %s</p>
    <pre class="post">%s</pre>
    <ul class="qc">%s</ul>
    <p>%s</p>
  </section>""" % (
            _esc(row.get("i")), _esc(row.get("title") or row.get("slug")),
            _esc(row.get("logline")), "".join(imgs),
            _esc(row.get("state")),
            _esc(row.get("estimate_s")), float(row.get("render_s") or 0),
            float(row.get("reroll_s") or 0), link,
            _esc(row.get("post")), qc or "<li>no QC rows</li>",
            ("<b>ERROR:</b> " + _esc(row.get("error"))) if row.get("error") else ""))

    errs = "".join("<li>%s</li>" % _esc(e) for e in (report.get("errors") or []))
    used = float(report.get("gpu_seconds_used") or 0.0)
    budget = float(report.get("budget_gpu_s") or 0.0)
    pct = (100.0 * used / budget) if budget else 0.0
    dur = (float(report.get("finished") or 0) - float(report.get("started") or 0))
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>Overnight — %s</title>
<style>
 body{background:#14110f;color:#e9e3da;font:15px/1.5 -apple-system,Segoe UI,sans-serif;
      margin:0;padding:32px}
 h1{color:#c25e33;margin:0 0 4px} h2{color:#e9e3da;margin:0 0 2px;font-size:18px}
 .logline{color:#9b9187;margin:0 0 12px}
 .bar{background:#1b1815;border:1px solid #312b25;border-radius:8px;padding:12px 16px;
      margin:0 0 24px}
 .bar b{color:#c9a24a}
 .short{background:#1b1815;border:1px solid #312b25;border-radius:8px;padding:16px;
        margin:0 0 20px}
 .strip{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
 .strip img{height:200px;border-radius:6px;border:1px solid #312b25}
 .meta{color:#8a8076;font-size:13px;margin:6px 0}
 .post{background:#211d19;border:1px solid #312b25;border-radius:6px;padding:10px;
       white-space:pre-wrap;color:#e9e3da;font-size:13px}
 .qc{color:#9b9187;font-size:13px} .qc b{color:#7c8c62}
 a{color:#c9a24a} .err{color:#c25e33}
</style></head><body>
<h1>Overnight — %s</h1>
<div class="bar">
  character <b>%s</b> &middot; %s/%s shorts &middot; state <b>%s</b><br>
  GPU seconds used <b>%.0f</b> / budget <b>%.0f</b> (%.0f%%) &middot; wall %.0f s<br>
  estimate total %.0f s &middot; temps logged: %s
</div>
%s
<div class="bar err"><b>Errors / notes</b><ul>%s</ul></div>
</body></html>
""" % (_esc(report.get("id")), _esc(report.get("id")), _esc(report.get("character")),
       _esc(report.get("count_done")), _esc(report.get("count_requested")),
       _esc(report.get("state")), used, budget, pct, dur,
       float(report.get("estimate_total_s") or 0.0),
       _esc(report.get("temps")), "".join(rows), errs or "<li>none</li>")


def write_report(report):
    d = os.path.join(done_dir(), report["id"])
    os.makedirs(d, exist_ok=True)
    atomic_write_json(os.path.join(d, "report.json"), report)
    with open(os.path.join(d, "report.html"), "w") as f:
        f.write(build_report_html(report))
    return d


# ---------------------------------------------------------------------------
# Queue lifecycle
# ---------------------------------------------------------------------------
def claim_brief(path):
    """queue/<id>.json -> running/<id>.json (atomic rename) and return (path, brief)."""
    ensure_dirs()
    dst = os.path.join(running_dir(), os.path.basename(path))
    os.replace(path, dst)
    with open(dst) as f:
        brief = json.load(f)
    brief.setdefault("id", os.path.splitext(os.path.basename(path))[0])
    return dst, brief


def finish_brief(bid):
    """running/<id>.json -> done/<id>/brief.json."""
    src = os.path.join(running_dir(), "%s.json" % bid)
    dst = os.path.join(done_dir(), bid, "brief.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(src):
        os.replace(src, dst)


def _error_report(brief, exc):
    return {"id": brief.get("id"), "character": brief.get("character"),
            "count_requested": brief.get("count"), "count_done": 0,
            "budget_gpu_s": brief.get("budget_gpu_s"), "gpu_seconds_used": 0.0,
            "estimate_total_s": 0.0, "started": time.time(), "finished": time.time(),
            "state": "error", "shorts": [], "errors": [repr(exc)], "temps": []}


def process_queue():
    """Drain the queue once. Returns the number of briefs run."""
    ensure_dirs()
    paths = sorted(glob.glob(os.path.join(queue_dir(), "*.json")))
    if not paths:
        write_status("idle")
        return 0
    if not queue_empty():
        write_status("waiting", stage="comfy-queue-busy")
        return 0
    ran = 0
    for path in paths:
        if not queue_empty():
            write_status("waiting", stage="comfy-queue-busy")
            break
        try:
            _path, brief = claim_brief(path)
        except Exception:
            write_status("error", stage="claim")
            continue
        bid = brief.get("id")
        write_status("running", brief=bid, stage="brief-start")
        try:
            report = run_brief(brief)
        except Exception as exc:
            report = _error_report(brief, exc)
        try:
            write_report(report)
        except Exception:
            write_status("error", brief=bid, stage="report")
        finish_brief(bid)
        write_status("idle")
        ran += 1
    return ran


def main(argv=None):
    ap = argparse.ArgumentParser(description="Forge Lab overnight Studio runner")
    ap.add_argument("--once", action="store_true",
                    help="drain the queue once and exit (no daemon loop)")
    a = ap.parse_args(argv)

    if os.environ.get("FORGE_LAB_DATA") or os.environ.get("FORGE_URL"):
        core.configure(data_dir=os.environ.get("FORGE_LAB_DATA"),
                       forge_url=os.environ.get("FORGE_URL"))
    ensure_dirs()
    write_status("starting")
    if a.once:
        process_queue()
        return 0
    while True:
        try:
            process_queue()
        except Exception:
            # the daemon must survive anything a single pass throws
            try:
                write_status("error", stage="loop")
            except Exception:
                pass
        _sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
