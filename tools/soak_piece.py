#!/usr/bin/env python3
"""SOAK PIECE — the Winner Takes It All piece through the 2-CARD Raylight path.

Why this exists: burn in BOTH 3090s under a REAL production render (not a synthetic loop)
and come out with something watchable. Same spec / seeds / per-scene audio windows as the
1-card finals (tools/winner_finals.py), so the output is a direct 1-card vs 2-card compare.

    python3 tools/soak_piece.py --dry     # plan + estimates, submit nothing
    python3 tools/soak_piece.py --smoke   # 1 scene, 124 fr, 4 steps — proves the path
    python3 tools/soak_piece.py --wait    # hold until forge is quiet, then run the full piece
    python3 tools/soak_piece.py           # the full 9-scene, 12-step, gpus=2 piece

⚠️ Forge Lab is routinely driven by a SECOND agent. The 2-card path owns BOTH GPUs for the
whole run, so before starting this holds off while `forge_lab.py` is being edited or another
render is queued (use --wait to actually hold; without it, it refuses). Never stomp a peer.

Both cards' telemetry (temp / fan / power / clock / util / VRAM) plus CPU Tctl + loadavg is
sampled every 5 s ON FORGE into the episode dir.

REFUSES to start if forge's queue is not empty (cannot evict another agent's run).
Every scene goes through the app's OWN submit()/run_state()/collect() — the code the buttons
call — not a test hook.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

IN = os.path.join(HERE, "inputs")
OUT = os.path.join(fl.DATA, "episodes", "winner-2card")
os.makedirs(OUT, exist_ok=True)

DRY = "--dry" in sys.argv
SMOKE = "--smoke" in sys.argv
WAIT = "--wait" in sys.argv
GPUS = 2
HOST = "name@forge"
QUIET_MIN = 15.0        # forge_lab.py must be untouched this long before we take the GPUs
MAX_WAIT_S = 6 * 3600   # give up after this long

SPEC = json.load(open(os.path.join(IN, "winner_bigeyes_scenes.json")))
REF = SPEC["reference"]
SONG = SPEC["song"]
SHOTS = SPEC["scenes"]
LEN = 124 if SMOKE else int(SPEC.get("scene_frames", 241))
STEPS = 4 if SMOKE else 12
FPS = 24.0
SCENE_S = LEN / FPS
SEEDS = [93000 + i * 131 for i in range(1, len(SHOTS) + 1)]

REMOTE_SAMPLE = ("nvidia-smi --query-gpu=index,temperature.gpu,fan.speed,power.draw,"
                 "clocks.sm,utilization.gpu,memory.used --format=csv,noheader,nounits "
                 "| sed 's/^/GPU,/'; "
                 "printf 'CPU,'; sensors 2>/dev/null | grep -m1 -E 'Tctl|Tccd1' "
                 "| tr -d '\\n'; printf ','; cut -d' ' -f1-3 /proc/loadavg")


class Sampler(threading.Thread):
    """Polls both cards + CPU Tctl over ssh every 5 s into a LOCAL csv.

    Deliberately runs on the Mac, not as a detached process on forge: keeping a background
    job alive across an ssh channel is fragile (ssh waits on the child's descriptors), and a
    dropped sampler would leave the soak unmeasured. An ssh every 5 s is cheap and if one
    probe fails we just lose one row.
    """

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.stop = threading.Event()
        self.rows = 0

    def run(self):
        with open(self.path, "w") as fh:
            fh.write("kind,time,gpu,temp_c,fan_pct,power_w,clock_mhz,util_pct,vram_mib,extra\n")
            while not self.stop.is_set():
                ts = time.strftime("%H:%M:%S")
                try:
                    out = ssh(REMOTE_SAMPLE, timeout=25).stdout.strip()
                except Exception:
                    out = ""
                for ln in out.splitlines():
                    parts = [p.strip() for p in ln.split(",")]
                    if len(parts) >= 7 and parts[0] == "GPU" and parts[1].isdigit():
                        # GPU,idx,temp,fan,power,clock,util,vram
                        g = parts[1:]
                        fh.write("GPU,%s,%s,%s,%s,%s,%s,%s,%s,\n"
                                 % (ts, g[0], g[1], g[2], g[3], g[4], g[5], g[6]))
                        self.rows += 1
                    elif parts and parts[0] == "CPU":
                        fh.write("CPU,%s,,,,,,,,%s\n" % (ts, ",".join(parts[1:])))
                fh.flush()
                self.stop.wait(5)

    def finish(self):
        self.stop.set()
        self.join(timeout=30)
        return self.path


def ssh(cmd, timeout=30):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", HOST, cmd],
                          capture_output=True, text=True, timeout=timeout)


def queue_empty():
    try:
        q = fl.get_json("/queue")
        return not (q.get("queue_running") or q.get("queue_pending"))
    except Exception as exc:
        print("  could not read forge's queue: %s" % exc)
        return False


def probe(path):
    v = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                        "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate",
                        "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    w, h, fr, n = v.split(",")
    return int(w), int(h), fr, int(n)


def main():
    for need, p in (("reference", REF), ("song", SONG)):
        if not os.path.exists(p):
            print("MISSING %s: %s" % (need, p))
            return 2
    scenes = SHOTS[:1] if SMOKE else SHOTS
    seeds = SEEDS[:1] if SMOKE else SEEDS
    print("SOAK PIECE  gpus=%d  steps=%d  %d scene(s) x %d fr (%.2f s) = %.2f s of picture"
          % (GPUS, STEPS, len(scenes), LEN, SCENE_S, len(scenes) * SCENE_S))

    plan = []
    total_est = 0.0
    for i, shot in enumerate(scenes, start=1):
        p = {"prompt": shot, "video_mode": "r2v", "lora": None, "lora_strength": 1.0,
             "steps": STEPS, "length": LEN, "width": 832, "height": 480,
             "seed": seeds[i - 1], "audio": True, "gpus": GPUS,
             "prefix": "video/winner_2card"}
        try:
            est = fl.estimate_seconds("video", p, [REF], None)
        except Exception as exc:
            est = None
            print("  scene %d ESTIMATE RAISED %s: %s" % (i, type(exc).__name__, exc))
        plan.append((i, p))
        total_est += est or 0
        print("  scene %d/%d  est=%s" % (i, len(scenes), ("%.0f s" % est) if est else "?"))
    print("  TOTAL estimate: %.1f min (gpus=%d)" % (total_est / 60.0, GPUS))
    if DRY:
        print("\n--dry: nothing submitted.")
        return 0
    lab = os.path.join(HERE, "forge_lab.py")
    waited = 0.0
    while True:
        q_ok = queue_empty()
        idle_min = (time.time() - os.path.getmtime(lab)) / 60.0
        quiet = q_ok and idle_min >= QUIET_MIN
        if quiet:
            print("  [gate] forge is quiet (queue empty, forge_lab.py idle %.0f min) — starting"
                  % idle_min)
            break
        if not WAIT:
            print("\nREFUSING TO START: forge is not quiet "
                  "(queue_empty=%s, forge_lab.py idle=%.0f min). Use --wait to hold."
                  % (q_ok, idle_min))
            return 3
        if waited >= MAX_WAIT_S:
            print("\n[gate] gave up after %.1f h of waiting." % (waited / 3600.0))
            return 4
        print("  [gate] waiting: queue_empty=%s  forge_lab.py idle=%.0f min  (%.0f min so far)"
              % (q_ok, idle_min, waited / 60.0), flush=True)
        time.sleep(120)
        waited += 120.0

    sampler = Sampler(os.path.join(OUT, "telemetry.csv"))
    sampler.start()
    print("  sampler: polling forge every 5 s -> %s" % sampler.path)
    files = []
    t_all = time.time()
    for i, p in plan:
        win = os.path.join(IN, "_s2c%02d.wav" % i)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.3f" % ((i - 1) * SCENE_S),
                        "-t", "%.3f" % SCENE_S, "-i", SONG, win], check=True)
        p["guide_audio_path"] = win
        t0 = time.time()
        try:
            pid, seed = fl.submit("video", p, ref_paths=[REF])
        except Exception as exc:
            print("  scene %d SUBMIT FAILED — %s: %s" % (i, type(exc).__name__, str(exc)[:400]),
                  flush=True)
            continue
        print("  scene %d/%d pid=%s seed=%s — rendering…" % (i, len(scenes), pid, seed), flush=True)
        last = None
        while time.time() - t0 < 3600:
            time.sleep(5)
            st = fl.run_state(pid)
            if st["state"] != last:
                print("    %6.1fs %s %s" % (time.time() - t0, st["state"],
                                            (st.get("detail") or "")[:120]), flush=True)
                last = st["state"]
            if st["state"] in ("done", "error", "gone"):
                break
        took = time.time() - t0
        if last != "done":
            print("  scene %d STATE=%s after %.0f s" % (i, last, took), flush=True)
            continue
        # collect() can lose a race on the LAST scene: run_state() reports "done" off the
        # history status while outputs/index are not committed yet, so collect() comes back
        # empty and the scene is silently dropped (that is how scene 9 vanished on the first
        # full run). Retry, then fall back to whatever media is sitting in the run dir.
        got = None
        for attempt in range(6):
            meta = fl.collect(pid) or {}
            for ent in (meta.get("images") or []):
                fn = ent.get("file") if isinstance(ent, dict) else ent
                if not fn:
                    continue
                f = fn if os.path.isabs(str(fn)) else os.path.join(fl.RUNS, pid, str(fn))
                if os.path.exists(f) and os.path.getsize(f) > 5000:
                    got = f
                    break
            if got:
                break
            if meta.get("error"):
                print("  scene %d collect: %s (retry %d)" % (i, meta["error"], attempt + 1),
                      flush=True)
            time.sleep(5)
        if not got:
            d = os.path.join(fl.RUNS, pid)
            cands = [os.path.join(d, x) for x in (os.listdir(d) if os.path.isdir(d) else [])
                     if x.endswith((".mp4", ".webm", ".png"))]
            cands = [c for c in cands if os.path.getsize(c) > 5000]
            if cands:
                got = cands[0]
                print("  scene %d recovered from run dir: %s"
                      % (i, os.path.basename(got)), flush=True)
        if not got:
            print("  scene %d no artifact" % i, flush=True)
            continue
        w, h, fr, n = probe(got)
        print("  scene %d OK  %dx%d %s %d frames  %.0f s wall" % (i, w, h, fr, n, took), flush=True)
        files.append(got)

    sampler.finish()
    print("  telemetry: %d rows -> %s" % (sampler.rows, sampler.path))
    print("\n%d/%d scenes rendered in %.1f min" % (len(files), len(scenes),
                                                   (time.time() - t_all) / 60.0))
    if SMOKE:
        print("SMOKE PASS — the 2-card path renders with the audio guide."
              if files else "SMOKE FAIL — no artifact.")
        return 0 if files else 1
    if not files:
        print("nothing to stitch.")
        return 1

    # stitch (concat demuxer, -c copy) then lay the song under it
    lst = os.path.join(OUT, "concat.txt")
    with open(lst, "w") as fh:
        for f in files:
            fh.write("file '%s'\n" % f)
    silent = os.path.join(OUT, "winner_2card_silent.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c", "copy", silent], check=True)
    final = os.path.join(OUT, "winner_2card_FINAL.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", SONG,
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                    "-shortest", final], check=True)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", final], capture_output=True, text=True).stdout.strip()
    print("\nFINAL: %s  (%.2f s)" % (final, float(dur or 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())