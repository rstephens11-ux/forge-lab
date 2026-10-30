"""PICTURE INBOX (2026-10-21) - lets Claude make pictures WITH Forge Lab without anyone at the
screen. Ryan: "dont need a job i just want you to use the forge lab and do it".

Only the app can reach the rig (macOS Local Network permission), so the app watches a
folder: DATA/inbox/<name>.json is a picture request, rendered on the rig by the same
engines the app uses, and the result lands next to it:

    {"kind": "t2i", "prompt": "...", "width": 704, "height": 1280, "steps": 25, "seed": -1}
    {"kind": "edit", "prompt": "... <image1> ... <image2>", "refs": ["/abs/a.png", ...],
     "width": 704, "height": 1280}

    -> DATA/inbox/out/<name>.png  (+ <name>.done.json: the params, the seed, or the error)

One request at a time, oldest first. A request is renamed <name>.taken first, so it is
never rendered twice (a relaunch, two windows). Tk-free; forge_lab starts start().
"""
import json
import os
import shutil
import threading
import time

import forge_core as c

_STATE = {"busy": False, "started": False}


def inbox():
    import forge_lab as fl
    d = os.path.join(fl.DATA, "inbox")
    os.makedirs(os.path.join(d, "out"), exist_ok=True)
    return d


def _params(req):
    w, h = int(req.get("width") or 704), int(req.get("height") or 1280)
    p = {"prompt": str(req.get("prompt") or "").strip(), "width": w, "height": h,
         "steps": int(req.get("steps") or 25), "seed": int(req.get("seed", -1)),
         "prefix": "forge_lab_inbox"}
    if (req.get("kind") or "t2i") == "edit":
        p["custom_size"] = True
    else:
        p["resolution"] = max(w, h)
    return p


def _first_image(meta):
    for f in (meta or {}).get("images") or []:
        fn = f.get("file") or ""
        if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return os.path.join(c.RUNS, meta.get("id") or "", fn)
    return None


def process_once(submit=None, wait=None, d=None):
    """Render the oldest waiting request -> its name, or None when the inbox is empty."""
    submit = submit or c.submit
    wait = wait or c.wait_and_collect
    d = d or inbox()
    reqs = sorted((os.path.getmtime(os.path.join(d, f)), f) for f in os.listdir(d)
                  if f.endswith(".json") and not f.endswith(".done.json"))
    if not reqs:
        return None
    name = reqs[0][1][:-5]
    taken = os.path.join(d, name + ".taken")
    try:
        os.replace(os.path.join(d, name + ".json"), taken)
    except OSError:
        return None                                 # someone else took it
    out = {"name": name, "started": time.time()}
    try:
        req = json.load(open(taken))
        kind = req.get("kind") or "t2i"
        if kind not in ("t2i", "edit"):
            raise ValueError("kind must be t2i or edit")
        refs = [r for r in (req.get("refs") or []) if r]
        missing = [r for r in refs if not os.path.exists(r)]
        if missing:
            raise ValueError("missing reference pictures: %s" % missing)
        params = _params(req)
        if not params["prompt"]:
            raise ValueError("empty prompt")
        pid, seed = submit(kind, dict(params), refs or None)
        img = _first_image(wait(pid))
        if not img:
            raise RuntimeError("no picture came back (%s)" % pid)
        dst = os.path.join(d, "out", name + os.path.splitext(img)[1].lower())
        shutil.copyfile(img, dst)
        out.update(ok=True, file=dst, seed=seed, pid=pid, kind=kind, params=params,
                   refs=refs)
    except Exception as exc:
        out.update(ok=False, error="%s: %s" % (type(exc).__name__, exc))
    out["finished"] = time.time()
    json.dump(out, open(os.path.join(d, name + ".done.json"), "w"), indent=1)
    return name


def start(every=5.0):
    """Watch the inbox on a background thread (once per process)."""
    if _STATE["started"]:
        return False
    _STATE["started"] = True

    def loop():
        while True:
            try:
                while process_once():
                    pass
            except Exception as exc:
                print("picture inbox: %r" % (exc,))
            time.sleep(every)
    threading.Thread(target=loop, daemon=True).start()
    return True
