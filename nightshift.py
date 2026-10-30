"""NIGHT SHIFT — a list of ideas that Quick makes while you sleep, and a contact sheet of
what came out in the morning (roadmap 2026-10-18, item 6; Higgsfield has no batch at all).
Tk-free; Quick's "Tonight" view is the window.

    add(ideas, options)       -> the items added to tonight's list (DATA/nightshift/queue.json)
    items() / remove(id) / clear()
    run(director, progress, stop) -> the night: every item made unattended, in order
    last_night() / contact_sheet(night) -> DATA/nightshift/<night>/index.html

Rules:
  * Unattended: the check-each-step reviews are off (nothing would answer them). The take
    check still runs, so a doubtful shot is FLAGGED on the sheet; nothing is rerolled
    without Ryan.
  * Each finished item leaves the list as it finishes (saved), so a night that stops — the
    Mac went to sleep, the app closed — carries on from the next idea.
  * A video that stops with an error gets one more try at the end of the night (Quick
    resumes it where it stopped), then is listed as stopped with the reason.
  * macOS: `caffeinate` keeps the Mac awake for exactly as long as the night runs.
"""
import html
import json
import os
import subprocess
import sys
import time
import uuid

import quick

UNATTENDED = {"check": False, "check_set": False, "pick_draft": False}


def root():
    d = os.path.join(quick.fl.DATA, "nightshift")
    os.makedirs(d, exist_ok=True)
    return d


def _qpath():
    return os.path.join(root(), "queue.json")


def items():
    try:
        q = json.load(open(_qpath()))
        return q if isinstance(q, list) else []
    except Exception:
        return []


def _save(q):
    tmp = _qpath() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(q, f, indent=1)
    os.replace(tmp, _qpath())


def add(ideas, options=None):
    """Put ideas on tonight's list with the Quick options they were made with."""
    q = items()
    opts = dict(options or {})
    opts.update(UNATTENDED)
    added = []
    for idea in ideas if isinstance(ideas, (list, tuple)) else [ideas]:
        idea = (idea or "").strip()
        # (the position alone repeated: remove one, add one back in the same millisecond
        # and two ideas shared an id, so making one took BOTH off the list)
        it = {"id": "n%d-%s" % (int(time.time() * 1000), uuid.uuid4().hex[:8]),
              "idea": idea, "options": opts, "added": time.time()}
        added.append(it)
    _save(q + added)
    return added


START_FLAG = "START_NOW"
_RUNNING = {"on": False}                       # a flag-started night in this process


def autostart(director=None, run_fn=None, threaded=True):
    """START ON LAUNCH (2026-10-20): when DATA/nightshift/START_NOW exists, the app starts
    tonight's list by itself a few seconds after it opens - so Claude (or a script) can start
    a night without anyone pressing Start. The flag is used up (renamed) first, so a crash
    or a relaunch never starts the same night twice. Progress goes to autostart.log.
    -> True when a night was started."""
    flag = os.path.join(root(), START_FLAG)
    if not os.path.exists(flag):
        return False
    if _RUNNING.get("on"):                     # a night is already going: leave the flag
        return False
    try:
        os.replace(flag, flag + ".used-%d" % int(time.time()))
    except OSError:
        return False
    if not items():
        return False
    log = os.path.join(root(), "autostart.log")

    def say(m):
        try:
            with open(log, "a") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), m))
        except OSError:
            pass

    def work():
        _RUNNING["on"] = True
        say("night shift started by the START_NOW flag")
        try:
            night = (run_fn or run)(director=director, progress=say)
            say("NIGHT DONE: %s" % [(v.get("title"), v.get("state"), v.get("error"))
                                    for v in (night or {}).get("videos", [])])
        except Exception as exc:
            say("night shift stopped: %s: %s" % (type(exc).__name__, exc))
        try:
            import cards as _c
            _c.release("quick")
        except Exception:
            pass
        _RUNNING["on"] = False

    if threaded:
        import threading
        threading.Thread(target=work, daemon=True).start()
    else:
        work()
    return True


def remove(item_id):
    _save([it for it in items() if it.get("id") != item_id])


def clear():
    _save([])


def _keep_awake():
    """macOS: `caffeinate -i -w <pid>` holds off idle sleep while this app runs; the caller
    stops it when the night is over."""
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except Exception:
        return None


def _summary(job):
    fin = job.get("final")
    cover = os.path.join(os.path.dirname(fin), "cover.png") if fin else None
    try:
        flags = len(quick.flagged(job)) if job.get("state") == "done" else 0
    except Exception:
        flags = 0
    try:
        secs = quick.sp._duration(fin) if fin and os.path.exists(fin) else 0.0
    except Exception:
        secs = 0.0
    return {"job": job.get("id"), "title": job.get("title") or job.get("idea") or "untitled",
            "idea": job.get("idea") or "", "format": job.get("format"),
            "state": job.get("state"), "error": job.get("error"),
            "final": fin if fin and os.path.exists(fin) else None,
            "cover": cover if cover and os.path.exists(cover) else None,
            "seconds": round(secs or 0.0, 1), "flags": flags}


def run(director=None, progress=None, stop=None, make_job=None, run_job=None):
    """Make every idea on the list, one after another. Returns the night (also saved as
    <night>/night.json + index.html)."""
    make_job = make_job or quick.new_job
    run_job = run_job or quick.run
    say = progress or (lambda _m: None)
    stop = stop or (lambda: False)
    night_id = base = time.strftime("%Y-%m-%d_%H%M")
    k = 2
    while os.path.exists(os.path.join(root(), night_id)):   # two nights in one minute
        night_id, k = "%s-%d" % (base, k), k + 1
    d = os.path.join(root(), night_id)
    os.makedirs(d)
    night = {"id": night_id, "dir": d, "started": time.time(), "finished": None,
             "videos": [], "stopped_early": False}
    awake = _keep_awake()
    retry = []
    try:
        while True:
            q = items()
            if not q:
                break
            if stop():
                night["stopped_early"] = True
                break
            it = q[0]
            n_done = len(night["videos"]) + 1
            say("night shift: %d of %d — %s" % (n_done, n_done - 1 + len(q),
                                                     it["idea"][:60] or "a surprise"))
            kw = dict(it.get("options") or {})
            kw.update(UNATTENDED)
            try:
                if kw.get("resume"):                    # film a pictures-only job, as approved
                    job = quick.load(kw["resume"])
                    if not job:
                        raise ValueError("no such job to resume: %s" % kw["resume"])
                    job["pictures_only"] = False
                else:
                    job = make_job(it["idea"], **kw)
                job = run_job(job, director=director,
                              progress=lambda m, n=n_done: say("night shift %d: %s" % (n, m)),
                              stop=stop)
            except Exception as exc:
                job = {"id": None, "idea": it["idea"], "state": "error",
                       "error": "%s: %s" % (type(exc).__name__, exc)}
            if job.get("state") == "stopped" and stop():
                night["stopped_early"] = True           # leave it on the list for next time
                _write(night)
                break
            _save([x for x in items() if x.get("id") != it["id"]])
            if job.get("state") == "error" and job.get("id"):
                retry.append(job)
            night["videos"].append(_summary(job))
            _write(night)
        for job in retry:                               # one more try, from where it stopped
            if stop():
                break
            say("night shift: trying %s again" % (job.get("title") or job.get("idea"))[:50])
            try:
                job = run_job(quick.load(job["id"]) or job, director=director, stop=stop)
            except Exception as exc:
                job["state"], job["error"] = "error", "%s: %s" % (type(exc).__name__, exc)
            for i, v in enumerate(night["videos"]):
                if v.get("job") == job.get("id"):
                    night["videos"][i] = dict(_summary(job), retried=True)
            _write(night)
    finally:
        if awake is not None:
            try:
                awake.terminate()
            except Exception:
                pass
        night["finished"] = time.time()
        _write(night)
    made = [v for v in night["videos"] if v.get("state") == "done"]
    quick.notify("Forge Lab night shift",
                 "%d video%s made%s — the contact sheet is ready" % (
                     len(made), "" if len(made) == 1 else "s",
                     ", %d stopped" % (len(night["videos"]) - len(made))
                     if len(night["videos"]) > len(made) else ""))
    return night


def _write(night):
    with open(os.path.join(night["dir"], "night.json"), "w") as f:
        json.dump(night, f, indent=1)
    contact_sheet(night)


def nights():
    """Every night, newest first."""
    out = []
    for n in sorted(os.listdir(root()), reverse=True):
        p = os.path.join(root(), n, "night.json")
        if os.path.exists(p):
            try:
                out.append(json.load(open(p)))
            except Exception:
                pass
    return out


def last_night():
    n = nights()
    return n[0] if n else None


def _uri(p):
    return "file://" + "/".join(
        __import__("urllib.parse").parse.quote(x) for x in os.path.abspath(p).split("/"))


def contact_sheet(night):
    """index.html: one card per video — it plays right on the page — with its idea, length,
    and how many shots might look off. Opens in any browser."""
    e = html.escape
    made = [v for v in night["videos"] if v.get("state") == "done"]
    cards = []
    for v in night["videos"]:
        ok = v.get("state") == "done" and v.get("final")
        media = ('<video src="%s" poster="%s" controls preload="none"></video>'
                 % (_uri(v["final"]), _uri(v["cover"]) if v.get("cover") else "")) if ok \
            else '<div class="none">%s</div>' % e("stopped: %s" % (v.get("error") or
                                                                 v.get("state") or "?"))[:200]
        note = ("%d shot%s might look off" % (v["flags"], "" if v["flags"] == 1 else "s")
                if v.get("flags") else ("looks fine" if ok else ""))
        cards.append(
            '<div class="card%s">%s<div class="t">%s</div><div class="i">%s</div>'
            '<div class="m">%s%s%s</div></div>'
            % ("" if ok else " bad", media, e(v.get("title") or ""), e(v.get("idea") or ""),
               e(v.get("format") or ""), (" · %.0f s" % v["seconds"]) if ok and
               v.get("seconds") else "", (' · <b>%s</b>' % e(note)) if note else ""))
    head = ("%d made, %d stopped" % (len(made), len(night["videos"]) - len(made))
            if night["videos"] else "nothing made")
    when = time.strftime("%a %d %b, %H:%M", time.localtime(night.get("started") or 0))
    page = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Night shift %s</title><style>
:root{--bg:#14110f;--card:#1f1b17;--ink:#eee6da;--mute:#8f857a;--warn:#d99a3a}
body{margin:0;background:var(--bg);color:var(--ink);font:15px -apple-system,Helvetica,sans-serif}
h1{font-size:20px;margin:24px 24px 4px}p.s{color:var(--mute);margin:0 24px 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;
padding:0 24px 32px}.card{background:var(--card);border-radius:10px;overflow:hidden}
video{width:100%%;display:block;background:#000;aspect-ratio:9/16;object-fit:contain}
.none{aspect-ratio:9/16;display:flex;align-items:center;justify-content:center;
padding:16px;color:var(--warn);text-align:center}
.t{font-weight:600;padding:10px 12px 2px}.i{color:var(--mute);padding:0 12px;font-size:13px}
.m{color:var(--mute);padding:6px 12px 12px;font-size:13px}.m b{color:var(--warn)}
</style></head><body><h1>Night shift — %s</h1><p class="s">%s%s</p>
<div class="grid">%s</div></body></html>""" % (
        e(night["id"]), e(when), e(head),
        " · stopped early; the rest is still on the list" if night.get("stopped_early")
        else "", "\n".join(cards))
    p = os.path.join(night["dir"], "index.html")
    with open(p, "w") as f:
        f.write(page)
    return p
