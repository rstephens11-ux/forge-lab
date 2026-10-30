#!/usr/bin/env python3
"""overnight/client.py — the MAC side of the overnight runner. Stdlib only.

Ryan's Mac queues a brief and, in the morning, fetches the results. Both sides talk over
ssh/scp to `forge`; the runner itself lives on the rig.

    python3 overnight/client.py queue bighead --count 3 --length 12 --budget-min 30
    python3 overnight/client.py status
    python3 overnight/client.py done
    python3 overnight/client.py fetch <brief-id>

`queue_brief` writes the brief ATOMICALLY on the rig: it streams the JSON to a hidden
temp file in the queue dir, then `mv`s it into place — so the runner can never read a
half-written brief.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HOST = os.environ.get("FORGE_SSH", "name@forge")
REMOTE_DATA = os.environ.get("FORGE_STUDIO_DATA", "/home/ryan/forge-studio-data")
ODIR = REMOTE_DATA + "/overnight"
QUEUE = ODIR + "/queue"
RUNNING = ODIR + "/running"
DONE = ODIR + "/done"
STATUS = ODIR + "/status.json"

DEFAULT_DEST = os.path.expanduser("~/Desktop/Lab Results/Shorts")


# ---------------------------------------------------------------------------
# ssh/scp seams (tests patch _ssh)
# ---------------------------------------------------------------------------
def _ssh(remote_cmd, input=None):
    """Run ONE remote shell command on the rig. `input` is streamed to its stdin."""
    return subprocess.run(["ssh", HOST, remote_cmd], input=input,
                          capture_output=True, text=True)


def _check(proc, what):
    if proc.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (what, proc.returncode,
                                                   (proc.stderr or "").strip()))
    return proc


def _slug(text):
    s = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or ""))
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:40] or "brief"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def queue_brief(character, count=1, length_s=12, budget_min=30, quality="Draft",
                ideas=None, brief_id=None, gpus=1):
    """Write a brief into the rig's queue. Returns the brief id.

    ATOMIC: temp file then `mv`, both inside the queue dir.
    """
    count = int(count)
    length_s = int(length_s)
    budget_gpu_s = float(budget_min) * 60.0
    if brief_id is None:
        brief_id = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), _slug(character))
    doc = {"id": brief_id, "character": character, "count": count, "length_s": length_s,
           "quality": quality, "gpus": int(gpus), "budget_gpu_s": budget_gpu_s,
           "ideas": list(ideas or []), "created": time.time()}
    payload = json.dumps(doc, indent=1)
    final = "%s/%s.json" % (QUEUE, brief_id)
    tmp = "%s/.%s.json.tmp" % (QUEUE, brief_id)
    _check(_ssh("mkdir -p %s && cat > %s" % (QUEUE, tmp), input=payload), "brief write")
    _check(_ssh("mv -f %s %s" % (tmp, final)), "brief commit")
    return brief_id


def status():
    """The runner's heartbeat dict, or None when it has not written one yet."""
    p = _ssh("cat %s 2>/dev/null || true" % STATUS)
    txt = (p.stdout or "").strip()
    if not txt:
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None


def list_done():
    """Brief ids that have finished (one dir each under done/)."""
    p = _ssh("ls -1 %s 2>/dev/null || true" % DONE)
    return [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]


def fetch(brief_id, dest=None):
    """scp the report dir (report.json/html + mp4s + frames) back to the Mac."""
    dest = os.path.expanduser(dest or DEFAULT_DEST)
    os.makedirs(dest, exist_ok=True)
    p = subprocess.run(["scp", "-q", "-r", "%s:%s/%s" % (HOST, DONE, brief_id), dest],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("fetch failed (%d): %s" % (p.returncode, (p.stderr or "").strip()))
    return os.path.join(dest, brief_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Forge Lab overnight runner — Mac client")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pq = sub.add_parser("queue", help="queue a brief")
    pq.add_argument("character")
    pq.add_argument("--count", type=int, default=1)
    pq.add_argument("--length", type=int, default=12, dest="length_s")
    pq.add_argument("--budget-min", type=float, default=30)
    pq.add_argument("--quality", default="Draft", choices=("Draft", "Final"))
    pq.add_argument("--gpus", type=int, default=1, choices=(1, 2),
                    help="2 = Raylight, BOTH cards on one render (Director is released)")
    pq.add_argument("--idea", action="append", default=None)

    sub.add_parser("status", help="print the runner heartbeat")
    sub.add_parser("done", help="list finished briefs")

    pf = sub.add_parser("fetch", help="pull a finished brief's report + mp4s")
    pf.add_argument("brief_id")
    pf.add_argument("--dest", default=None)

    a = ap.parse_args(argv)

    if a.cmd == "queue":
        bid = queue_brief(a.character, count=a.count, length_s=a.length_s,
                          budget_min=a.budget_min, quality=a.quality, ideas=a.idea,
                          gpus=a.gpus)
        print("queued %s" % bid)
        return 0
    if a.cmd == "status":
        st = status()
        print(json.dumps(st, indent=1) if st else "(no status yet)")
        return 0
    if a.cmd == "done":
        for bid in list_done():
            print(bid)
        return 0
    if a.cmd == "fetch":
        print(fetch(a.brief_id, a.dest))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
