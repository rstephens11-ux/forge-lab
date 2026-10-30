#!/usr/bin/env python3
"""seamless_batch.py — N one-take pieces, end to end, unattended. Runs ON FORGE.

    director.seamless()  ->  one 15 s take (362 frames)  ->  phone-look post (grade + card + bed)

Each piece is independent: a failure is recorded and the next piece still runs. The Director
lives on GPU1 and a 2-card render needs BOTH cards, so GPU1 is released and CONFIRMED free
before every render.

    FORGE_LAB_DATA=/home/ryan/forge-studio-data /usr/bin/python3 seamless_batch.py \
        --character gustav --count 3 --bed bed_seed111.mp3 --out /home/ryan/seamless-batch

Writes <out>/report.json + index.html + <slug>_final.mp4 per piece.
"""
import argparse
import html
import json
import os
import random
import re
import subprocess
import sys
import time

os.environ.setdefault("FORGE_LAB_DATA", "/home/ryan/forge-studio-data")
os.environ.setdefault("FORGE_URL", "http://127.0.0.1:8188")
os.environ.setdefault("FORGE_AUDIO_LOCAL", "1")
HERE = "/home/ryan/forge-studio"
for p in (HERE, os.path.join(HERE, "audio")):
    if p not in sys.path:
        sys.path.insert(0, p)

import director as dr                                            # noqa: E402
import studio_pipeline as sp                                     # noqa: E402
import finish_seamless as fs                                     # noqa: E402

SECONDS = 15
SECTIONS = ("subject_definitions", "summary", "retention_analysis",
            "detailed_description", "overall_soundscape", "non_diegetic_music")


def slugify(t):
    s = re.sub(r"[^a-z0-9]+", "-", str(t or "").lower()).strip("-")
    return s[:48] or "piece"


def validate(prompt):
    """Cheap pre-render gate. Returns a list of warnings (never blocks a render)."""
    w = []
    for s in SECTIONS:
        if not re.search(r"(?<![A-Za-z_])%s\s*:" % s, prompt, re.I):
            w.append("missing section %s" % s)
    if re.search(r"<d>|</d>|\(S\d\)", prompt):
        w.append("prompt contains dialogue")
    if re.search(r"\bstatic (shot|camera)\b", prompt, re.I):
        w.append("a static shot is described")
    if not re.search(r"no (human speech|spoken|voices|dialogue)", prompt, re.I):
        w.append("soundscape does not forbid speech")
    dd = re.search(r"detailed_description\s*:(.*?)(?=overall_soundscape\s*:)", prompt, re.S | re.I)
    n = len((dd.group(1) if dd else "").split())
    if n < 250:
        w.append("detailed_description only %d words" % n)
    return w


def vram_free_mib():
    """{gpu_index: free MiB} from nvidia-smi, or {} when it cannot be read."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.free",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout
        d = {}
        for line in (out or "").splitlines():
            if "," in line:
                a, b = line.split(",")[:2]
                d[int(a.strip())] = int(b.strip())
        return d
    except Exception:
        return {}


def preflight_vram(need_mib=2500, tries=6, wait=10.0):
    """A 2-card render needs room on BOTH cards before it starts.

    ⚠️ Raylight's worker teardown is buggy ("AttributeError: 'NoneType' object has no attribute
    'vrambuf_destroy'") and can leave a worker holding ~17 GB after a finished piece. The NEXT
    piece then OOMs during init — measured on the 2026-10-07 batch, where that killed piece 2 of
    3 while pieces 1 and 3 rendered fine. Ask ComfyUI to unload and WAIT for the memory to
    actually come back, rather than assuming the previous render released it.
    """
    for i in range(max(1, tries)):
        f = vram_free_mib()
        if f and all(v >= need_mib for v in f.values()):
            return True
        if i == 0:
            print("  preflight: only %s MiB free — asking ComfyUI to unload" % (f or "?"),
                  flush=True)
        try:
            import urllib.request
            req = urllib.request.Request(
                os.environ.get("FORGE_URL", "http://127.0.0.1:8188").rstrip("/") + "/free",
                data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass
        time.sleep(wait)
    print("  preflight: still only %s MiB free — rendering anyway" % (vram_free_mib() or "?"),
          flush=True)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--character", default="gustav")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--bed", default="bed_seed111.mp3")
    ap.add_argument("--look", default="phone", choices=("phone", "film"))
    ap.add_argument("--out", default="/home/ryan/seamless-batch")
    ap.add_argument("--idea", action="append", default=None)
    ap.add_argument("--prefix", default=None)
    a = ap.parse_args(argv)

    os.makedirs(a.out, exist_ok=True)
    out = {"started": time.time(), "character": a.character, "look": a.look, "bed": a.bed,
           "pieces": []}
    d = dr.Director()
    print("director: %s  model=%s" % (d.host, d.model), flush=True)

    ideas = list(a.idea or [])
    for i in range(a.count):
        idea = ideas[i] if i < len(ideas) else (ideas[0] if ideas else "")
        row = {"i": i + 1, "idea": idea, "state": "running"}
        out["pieces"].append(row)
        t0 = time.time()
        try:
            print("\n=== piece %d/%d  idea=%r" % (i + 1, a.count, idea), flush=True)
            d.load()
            # Pass a character DICT, not a slug: the dict is read through forge_lab (which knows
            # the data root), so this works on the rig whatever CHARACTERS_DIR resolves to.
            char = sp._char_dict(a.character)
            obj = d.seamless(char, idea=idea)
            row["title"] = obj.get("title")
            row["logline"] = obj.get("logline")
            row["card"] = obj.get("card")
            row["prompt_chars"] = len(obj.get("prompt") or "")
            row["warnings"] = validate(obj.get("prompt") or "")
            print("  %s\n  card: %r\n  %d chars, warnings=%s"
                  % (row["title"], row["card"], row["prompt_chars"], row["warnings"] or "none"),
                  flush=True)

            slug = "%s-%s" % (a.prefix or a.character, slugify(row["title"]))
            pj = sp.new_project([a.character], idea or row["logline"] or "seamless",
                                length_s=SECONDS, quality="Final", gpus=2,
                                title=row["title"], slug=slug)
            pj["shots"] = [sp._shot_migrate({
                "n": 1, "seconds": SECONDS, "intent": "character_action", "engine": "h3",
                "video_mode": "r2v", "camera": "handheld phone, slow drift, off-centre",
                "action": "his signature performance", "line": "", "prompt": obj["prompt"],
            })]
            sp.save_project(pj)
            row["project"] = slug

            freed = sp.release_gpu1_for_render()
            row["gpu1_released"] = freed
            print("  gpu1 released: %s — rendering…" % freed, flush=True)
            # ⚠️ Retry once. A finished piece can leave a Raylight worker holding ~17 GB (a
            # teardown bug), which OOMs the NEXT piece during init — that is exactly how piece 2
            # of 3 died on the first batch while 1 and 3 rendered fine. A retry after a VRAM
            # preflight recovers it rather than losing the piece.
            res, ok = None, False
            for attempt in (1, 2):
                preflight_vram()
                tr = time.time()
                res = sp.render_shot(pj, 1, seed=random.randint(0, 2 ** 31 - 1))
                row["render_s"] = round(time.time() - tr, 1)
                row["seed"] = res.get("seed")
                if res.get("status") == "done":
                    ok = True
                    break
                row.setdefault("attempts", []).append(
                    {"attempt": attempt, "seconds": row["render_s"],
                     "error": json.dumps(res.get("meta") or {})[:300]})
                print("  render attempt %d FAILED after %.0fs: %s"
                      % (attempt, row["render_s"],
                         json.dumps(res.get("meta") or {})[:160]), flush=True)
                if attempt == 1:
                    time.sleep(30)
            if not ok:
                row["state"] = "render_failed"
                row["error"] = json.dumps((res or {}).get("meta") or {})[:400]
                print("  RENDER FAILED (both attempts)", flush=True)
                continue
            sh = pj["shots"][0]
            sh["chosen"] = res["pid"]
            sp.save_project(pj)
            tp = sp._chosen_clip_path(pj, sh)
            row["take"] = tp
            print("  rendered in %.1f min -> %s"
                  % (row["render_s"] / 60.0, os.path.basename(tp or "?")), flush=True)

            secs = fs.duration(tp)
            work = sp.project_dir(slug)
            ass = os.path.join(work, ".card.ass")
            fs.write_card_ass(ass, row["card"] or "", max(0.0, secs - 4.5),
                              min(secs - 0.05, secs - 0.0))
            bed = a.bed if (a.bed and os.path.exists(a.bed)) else ""
            dst = os.path.join(a.out, "%s_final.mp4" % slug)
            cmd = fs.build_cmd(tp, dst, ass, bed, secs, look=a.look)
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                row["state"] = "post_failed"
                row["error"] = (r.stderr or "")[-500:]
                print("  POST FAILED", flush=True)
                continue
            row["mp4"] = dst
            row["seconds"] = fs.duration(dst)
            row["mb"] = round(os.path.getsize(dst) / 1e6, 1)
            row["state"] = "done"
            print("  done: %s  %.1fs  %s MB" % (os.path.basename(dst), row["seconds"], row["mb"]),
                  flush=True)
        except Exception as exc:
            row["state"] = "error"
            import traceback
            row["error"] = repr(exc)
            row["traceback"] = traceback.format_exc()[-1200:]
            print("  ERROR: %r\n%s" % (exc, row["traceback"]), flush=True)
        finally:
            row["wall_s"] = round(time.time() - t0, 1)
            with open(os.path.join(a.out, "report.json"), "w") as f:
                json.dump(out, f, indent=1)

    try:
        d.unload()
    except Exception:
        pass
    done = [p for p in out["pieces"] if p.get("state") == "done"]
    out["finished"] = time.time()
    out["count_done"] = len(done)
    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump(out, f, indent=1)

    h = ["<html><head><meta charset='utf-8'><title>Seamless batch</title><style>",
         "body{background:#14161a;color:#e9ecf1;font:15px/1.5 -apple-system,sans-serif;margin:0;padding:26px}",
         ".c{display:inline-block;width:300px;margin:0 14px 20px 0;vertical-align:top}",
         "video{width:300px;border-radius:8px;background:#000}.t{font-weight:640;margin:6px 0 2px}",
         ".s{color:#9aa3b2;font-size:13px}pre{white-space:pre-wrap;color:#c9d1da;font-size:12px}",
         "</style></head><body><h1>Seamless batch — %s</h1><div class='s'>%d/%d done · %s look · bed %s</div><br>"
         % (time.strftime("%a %d %b %Y %H:%M"), len(done), len(out["pieces"]), a.look, a.bed or "none")]
    for p in out["pieces"]:
        h.append("<div class='c'>")
        if p.get("mp4"):
            h.append("<video src='%s' controls preload='metadata'></video>" % html.escape(os.path.basename(p["mp4"])))
        h.append("<div class='t'>%s</div><div class='s'>%s<br>%s s · %s MB · render %.1f min · %s</div>"
                 "<pre>%s</pre></div>"
                 % (html.escape(p.get("title") or "?"), html.escape(p.get("logline") or ""),
                    p.get("seconds"), p.get("mb"), (p.get("render_s") or 0) / 60.0,
                    p.get("state"), html.escape(p.get("card") or "")))
    h.append("</body></html>")
    with open(os.path.join(a.out, "index.html"), "w") as f:
        f.write("\n".join(h))
    print("\n%d/%d done, report: %s" % (len(done), len(out["pieces"]),
                                       os.path.join(a.out, "index.html")))
    return 0


if __name__ == "__main__":
    sys.exit(main())