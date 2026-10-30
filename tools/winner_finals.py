#!/usr/bin/env python3
"""The Winner piece at FINAL quality: same 9 scenes, same seeds, same song windows,
same reference -- but 12 steps, which drops the turbo distill LoRA (h3_graph turns it
on only at steps <= 4). The draft took 121 s/scene; this is the ~2.5x path.

Resumable: every scene is copied to a stable path (finals/scene_NN.mp4) and recorded in
finals/manifest.json, so a re-run skips whatever already landed. Never re-renders a
scene that is already on disk.

  python3 tools/winner_finals.py --scenes 1        # just scene 1 (a timing probe)
  python3 tools/winner_finals.py                   # all 9, skipping anything done
  python3 tools/winner_finals.py --stitch          # then stitch + lay the track under it
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sp = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(sp)
sp.loader.exec_module(fl)

IN = os.path.join(HERE, "inputs")
SPEC = json.load(open(os.path.join(IN, "winner_bigeyes_scenes.json")))
REF, SONG, SHOTS = SPEC["reference"], SPEC["song"], SPEC["scenes"]
LEN = int(SPEC.get("scene_frames", 241))
FPS = 24.0
SCENE_S = LEN / FPS

EP = os.path.join(fl.DATA, "episodes", "winner-bigeyes")
STEPS = 12


def load_man(man_path, w, h):
    if os.path.exists(man_path):
        try:
            return json.load(open(man_path))
        except Exception:
            pass
    return {"steps": STEPS, "scene_frames": LEN, "size": [w, h], "scenes": {}}


def save_man(m, man_path):
    json.dump(m, open(man_path, "w"), indent=1)


def probe(path):
    j = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames,width,height",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True).stdout)
    v = j["streams"][0]
    return {"w": int(v["width"]), "h": int(v["height"]),
            "frames": int(v.get("nb_read_frames") or 0),
            "dur": round(float(j["format"]["duration"]), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", default="all",
                    help="all | 1 | 1-9 | 2,5,7")
    ap.add_argument("--steps", type=int, default=STEPS)
    ap.add_argument("--size", default="832x480", help="WxH, e.g. 1344x768")
    ap.add_argument("--out", default="finals", help="subdir under the episode")
    ap.add_argument("--stitch", action="store_true")
    a = ap.parse_args()
    W, H = (int(x) for x in a.size.lower().split("x"))
    OUT = os.path.join(EP, a.out)
    os.makedirs(OUT, exist_ok=True)
    MAN = os.path.join(OUT, "manifest.json")

    if a.scenes == "all":
        want = list(range(1, len(SHOTS) + 1))
    else:
        want = []
        for part in a.scenes.split(","):
            if "-" in part:
                lo, hi = part.split("-")
                want += list(range(int(lo), int(hi) + 1))
            else:
                want.append(int(part))

    man = load_man(MAN, W, H)
    man["steps"] = int(a.steps)
    done = man["scenes"]

    print("Winner finals -- %d scenes x %d frames (%.2f s) @ %d steps, %dx%d"
          % (len(SHOTS), LEN, SCENE_S, a.steps, W, H))
    todo = [i for i in want if not (str(i) in done and os.path.exists(done[str(i)]["file"]))]
    skip = [i for i in want if i not in todo]
    if skip:
        print("already on disk, skipping: %s" % skip)
    if not todo:
        print("nothing to render")
    else:
        print("rendering: %s   (draft measured 121 s/scene; this path ~2.5x)"
              % todo, flush=True)

    t0 = time.time()
    times = []
    for n, i in enumerate(todo, start=1):
        shot = SHOTS[i - 1]
        win = os.path.join(IN, "_fs%02d.wav" % i)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.3f" % ((i - 1) * SCENE_S),
                        "-t", "%.3f" % SCENE_S, "-i", SONG, win], check=True)
        p = {"prompt": shot, "video_mode": "r2v", "lora": None, "lora_strength": 1.0,
             "steps": int(a.steps), "length": LEN, "width": W, "height": H,
             "seed": 93000 + i * 131, "audio": True, "guide_audio_path": win,
             "prefix": "video/winner_final12"}
        t1 = time.time()
        try:
            pid, seed = fl.submit("video", p, ref_paths=[REF])
        except Exception as exc:
            print("  scene %d SUBMIT FAILED: %s" % (i, exc), flush=True)
            continue
        st, last = None, None
        while time.time() - t1 < 1500:
            time.sleep(5)
            st = fl.run_state(pid)
            if st["state"] != last:
                print("  scene %d/%d %6.1fs %s %s" % (i, len(SHOTS), time.time() - t1,
                                                      st["state"], st.get("detail", "")),
                      flush=True)
                last = st["state"]
            if st["state"] in ("done", "error", "gone"):
                break
        if st["state"] != "done":
            print("  scene %d FAILED (%s)" % (i, st["state"]), flush=True)
            continue
        meta = fl.collect(pid)
        fs = [os.path.join(fl.RUNS, pid, f["file"]) for f in (meta.get("images") or [])]
        clean = next((f for f in fs if "synced" not in f), None)
        if not clean:
            print("  scene %d: no clean output" % i, flush=True)
            continue
        dst = os.path.join(OUT, "scene_%02d.mp4" % i)
        shutil.copy2(clean, dst)
        info = probe(dst)
        wall = time.time() - t1
        times.append(wall)
        done[str(i)] = {"file": dst, "run": pid, "seed": seed, "wall_s": round(wall, 1),
                        "size": [info["w"], info["h"]], "frames": info["frames"],
                        "dur": info["dur"], "steps": int(a.steps)}
        save_man(man, MAN)
        avg = sum(times) / len(times)
        left = [x for x in todo if str(x) not in done]
        print("  scene %d/%d DONE %.0fs  %s  %sx%s %s frames %.2fs   avg %.0fs/scene, "
              "%d left -> ~%.1f min"
              % (i, len(SHOTS), wall, os.path.basename(dst), info["w"], info["h"],
                 info["frames"], info["dur"], avg, len(left), len(left) * avg / 60.0),
              flush=True)

    print("\nrendered %d scene(s) in %.1f min" % (len(times), (time.time() - t0) / 60.0))
    have = sorted(int(k) for k in done)
    print("on disk: %s" % have)

    if a.stitch:
        stitch(man, OUT, MAN)
    else:
        allin = all(str(i) in done and os.path.exists(done[str(i)]["file"])
                    for i in range(1, len(SHOTS) + 1))
        if allin:
            print("\nall %d scenes present -- re-run with --stitch to assemble" % len(SHOTS))


def stitch(man, OUT, MAN):
    done = man["scenes"]
    missing = [i for i in range(1, len(SHOTS) + 1) if str(i) not in done]
    if missing:
        raise SystemExit("REFUSING to stitch -- scenes missing: %s" % missing)
    lst = os.path.join(OUT, "concat.txt")
    with open(lst, "w") as fh:
        for i in range(1, len(SHOTS) + 1):
            fh.write("file '%s'\n" % done[str(i)]["file"].replace("'", "'\\''"))
    silent = os.path.join(OUT, "winner_final_silent.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c", "copy", silent], check=True)
    final = os.path.join(OUT, "winner_takes_it_all_FINAL.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", SONG, "-map", "0:v:0",
                    "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-af", "apad", "-shortest", final], check=True)
    ctx = probe(final)
    pic = probe(silent)
    cut = round(pic["dur"] - ctx["dur"], 3)
    print("\nFINAL: %s" % final)
    print("  %sx%s  %s frames  %.3f s  (%d scenes, %d steps)"
          % (ctx["w"], ctx["h"], ctx["frames"], ctx["dur"], len(SHOTS), man["steps"]))
    if cut > 0.01:
        print("  bed track %.2fs vs picture %.2fs -- CUT %.3fs (%d frames at %g fps)"
              % (ctx["dur"], pic["dur"], cut, round(cut * FPS), FPS))
    man["silent"] = silent
    man["final"] = final
    man["frames"] = ctx["frames"]
    man["duration"] = ctx["dur"]
    save_man(man, MAN)


if __name__ == "__main__":
    main()