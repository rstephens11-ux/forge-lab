#!/usr/bin/env python3
"""night_report.py — watch the overnight briefs, fetch them as they land, and build a
morning summary. Runs ON THE MAC; the rendering is forge's job.

    /opt/homebrew/bin/python3 tools/night_report.py <brief-id> [<brief-id> ...] \
        --dest ~/Desktop/"Lab Results"/Shorts/night-20261007

For each brief, waits for `done/<id>/` on the rig (the runner writes it when the WHOLE brief
finishes), scp's it down, then probes every mp4. It also sweeps the rig for any finished
`*_final.mp4` under studio/ that the brief never got to report — so a brief killed part-way
still yields its finished shorts instead of nothing.

Writes summary.json + report.html into --dest and prints one line per video.
"""
import argparse
import html
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "overnight"))
import client as oc                                            # noqa: E402


def _ssh(cmd):
    p = subprocess.run(["ssh", oc.HOST, cmd], capture_output=True, text=True)
    return (p.stdout or "").strip()


def done_exists(bid):
    return _ssh("test -d %s/%s && echo yes || echo no" % (oc.DONE, bid)) == "yes"


def sweep_finals(bid):
    """Every assembled *_final.mp4 on the rig under studio/ for this brief id."""
    out = _ssh("ls -1 %s/studio/%s-s*/*_final.mp4 2>/dev/null || true"
               % (oc.REMOTE_DATA, bid))
    return [p.strip() for p in out.splitlines() if p.strip()]


def probe(path):
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
                            "-of", "json", path], capture_output=True, text=True, timeout=60)
        d = json.loads(p.stdout or "{}")
    except Exception:
        return {}
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        dur = round(float(d.get("format", {}).get("duration") or 0), 2)
    except Exception:
        dur = 0
    return {"w": v.get("width"), "h": v.get("height"), "fps": v.get("r_frame_rate"),
            "video": v.get("codec_name"), "audio": a.get("codec_name") or None, "seconds": dur,
            "mb": round(os.path.getsize(path) / 1e6, 1) if os.path.exists(path) else None}


def pull(remote, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    p = subprocess.run(["scp", "-q", "%s:%s" % (oc.HOST, remote), dest],
                       capture_output=True, text=True)
    return p.returncode == 0 and os.path.exists(dest)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("briefs", nargs="+")
    ap.add_argument("--dest", default=os.path.expanduser("~/Desktop/Lab Results/Shorts/night"))
    ap.add_argument("--timeout-h", type=float, default=8.0)
    ap.add_argument("--poll-s", type=int, default=90)
    a = ap.parse_args(argv)
    dest = os.path.expanduser(a.dest)
    os.makedirs(dest, exist_ok=True)

    deadline = time.time() + a.timeout_h * 3600
    fetched = {}
    while time.time() < deadline:
        for bid in a.briefs:
            if bid in fetched:
                continue
            if done_exists(bid):
                try:
                    local = oc.fetch(bid, dest=dest)
                    fetched[bid] = local
                    print("FETCHED %s -> %s" % (bid, local), flush=True)
                except Exception as exc:
                    print("fetch failed for %s: %r" % (bid, exc), flush=True)
        if len(fetched) == len(a.briefs):
            break
        time.sleep(a.poll_s)

    # Whatever the runner didn't get to, sweep the studio dirs for assembled finals.
    rows = []
    for bid in a.briefs:
        local = fetched.get(bid)
        if local:
            for f in sorted(os.listdir(local)):
                if f.endswith(".mp4"):
                    fp = os.path.join(local, f)
                    post = f.replace(".mp4", "_post.txt")
                    rows.append({"brief": bid, "file": fp, "src": "brief",
                                 "post": (open(os.path.join(local, post)).read().strip()
                                          if os.path.exists(os.path.join(local, post)) else ""),
                                 **probe(fp)})
        for remote in sweep_finals(bid):
            base = os.path.basename(remote)
            fp = os.path.join(dest, "%s_%s" % (bid, base))
            if not os.path.exists(fp) and pull(remote, fp):
                rows.append({"brief": bid, "file": fp, "src": "swept", "post": "", **probe(fp)})

    with open(os.path.join(dest, "summary.json"), "w") as f:
        json.dump({"briefs": a.briefs, "fetched": list(fetched), "videos": rows}, f, indent=1)

    total = sum(r.get("seconds") or 0 for r in rows)
    h = ["<html><head><meta charset='utf-8'><title>Overnight batch</title><style>",
         "body{background:#14161a;color:#e8e8e8;font:15px/1.5 -apple-system,Segoe UI,sans-serif;"
         "margin:0;padding:28px}h1{font-size:22px;margin:0 0 4px}",
         ".m{color:#8b93a1;margin-bottom:22px}",
         ".c{display:inline-block;width:300px;margin:0 14px 18px 0;vertical-align:top}",
         "video{width:300px;border-radius:8px;background:#000}",
         ".t{font-weight:600;margin:6px 0 2px}.s{color:#8b93a1;font-size:13px}",
         "pre{white-space:pre-wrap;color:#c8cfd8;font-size:12px;margin:4px 0 0}</style></head><body>"]
    h.append("<h1>Overnight batch — %s</h1>" % time.strftime("%a %d %b %Y %H:%M"))
    h.append("<div class='m'>%d videos · %.1f s of finished picture · briefs: %s</div>"
             % (len(rows), total, ", ".join(a.briefs)))
    for r in sorted(rows, key=lambda x: x["file"]):
        h.append("<div class='c'><video src='%s' controls preload='metadata'></video>"
                 "<div class='t'>%s</div><div class='s'>%s×%s · %s s · %s fps · audio %s · %s MB · %s</div>"
                 "<pre>%s</pre></div>"
                 % (html.escape(os.path.basename(r["file"])), html.escape(os.path.basename(r["file"])),
                    r.get("w"), r.get("h"), r.get("seconds"), (r.get("fps") or "").replace("/1", ""),
                    r.get("audio") or "NONE", r.get("mb"), r.get("src"),
                    html.escape(r.get("post") or "")))
    h.append("</body></html>")
    rp = os.path.join(dest, "report.html")
    with open(rp, "w") as f:
        f.write("\n".join(h))

    print("\n=== SUMROBIN ===")
    for r in rows:
        print("%-52s %sx%s %6.2fs fps=%-5s audio=%-4s %sMB" %
              (os.path.basename(r["file"]), r.get("w"), r.get("h"), r.get("seconds") or 0,
               (r.get("fps") or "").replace("/1", ""), r.get("audio") or "NONE", r.get("mb")))
    print("%d videos, %.1f s total" % (len(rows), total))
    print("report: %s" % rp)
    return 0


if __name__ == "__main__":
    sys.exit(main())