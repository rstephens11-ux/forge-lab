#!/usr/bin/env python3
"""Does the H3 text encoder cost anything on Ampere? nvfp4_awq vs int8_convrot.

WHY THIS EXISTS
Forge Lab pins H3_CLIP = qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors. Project notes say
NVFP4 is "Blackwell-only, won't run" on a 3090 — yet H3 jobs run with nvfp4 tensors being
EMULATED (ComfyUI logs "emulated ops: nvfp4"). The int8_convrot twin (27.1 GB) is on disk
next to it. Nobody has measured the difference.

DESIGN (the part that makes the numbers mean something)
- Same graph builder as the app (fl.h3_graph), so this is the wired code, not a parallel path.
- Same seed, canvas, frames, steps, turbo LoRA for every run.
- TWO different prompts, so ComfyUI's conditioning cache cannot hide the encode pass:
    nvfp4/P1, int8/P1, int8/P2, nvfp4/P2   <- order counterbalanced against drift
  Each encoder therefore gets two measurements, and each prompt is encoded by both.
- The clips for a given prompt are directly comparable (same seed + prompt => any
  difference between them IS the encoder's effect).
- Shortest legal canvas/clip on purpose: the encode happens ONCE per job, so the
  smaller the job, the larger the encoder's share of total wall time and the easier
  a real difference is to see.

    python3 tools/h3_encoder_ab.py --validate-only     # build graphs, spend nothing
    python3 tools/h3_encoder_ab.py                     # ~4 jobs, sequential
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

ENCODERS = {
    "nvfp4": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "int8": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
}
PROMPTS = {
    "P1": ("A black horse gallops across a wet beach at dawn, water spraying from its hooves, "
           "slow tracking shot, overcast light, no text."),
    "P2": ("A red ceramic teapot sits on a wooden table, steam curling from the spout, "
           "static medium shot, warm window light, no text."),
}
LOG = "~/comfyui-service.log"
OUTDIR = os.path.join(HERE, "drafts", "h3-encoder-ab")

ap = argparse.ArgumentParser()
ap.add_argument("--width", type=int, default=960)
ap.add_argument("--height", type=int, default=544)
ap.add_argument("--seconds", type=float, default=5.17, help="5.17 s = 124 frames = H3's trained minimum")
ap.add_argument("--steps", type=int, default=4)
ap.add_argument("--seed", type=int, default=771203)
ap.add_argument("--validate-only", action="store_true")
args = ap.parse_args()

LENGTH = int(round(args.seconds * 24.0))
ORDER = [("nvfp4", "P1"), ("int8", "P1"), ("int8", "P2"), ("nvfp4", "P2")]


def sh(cmd, timeout=30):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                           fl.SSH_TARGET, cmd],
                          capture_output=True, text=True, timeout=timeout).stdout.strip()


def log_lines():
    try:
        return int(sh("wc -l < %s" % LOG) or 0)
    except Exception:
        return 0


def log_region(start):
    return sh("sed -n '%d,$p' %s" % (start + 1, LOG), timeout=30)


def build(key, pk):
    fl.H3_CLIP = ENCODERS[key]
    g = fl.h3_graph("i2v", PROMPTS[pk], [], LENGTH, args.width, args.height,
                    args.steps, args.seed, turbo=(args.steps <= 4), audio=True)
    fl._check_graph(g)
    assert g["clip"]["inputs"]["clip_name"] == ENCODERS[key], "encoder did not take"
    return g


def wait_idle(limit=900):
    t0 = time.time()
    while time.time() - t0 < limit:
        q = fl.get_json("/queue")
        if not q.get("queue_running") and not q.get("queue_pending"):
            return True
        time.sleep(5)
    return False


def run(key, pk, idx, total):
    print("\n=== [%d/%d] encoder=%s prompt=%s ===" % (idx, total, key, pk), flush=True)
    g = build(key, pk)
    before = log_lines()
    t0 = time.time()
    r = fl.post_json("/prompt", {"prompt": g, "client_id": "h3-encoder-ab"})
    pid = r["prompt_id"]
    peak_v = peak_w = peak_t = 0
    while True:
        time.sleep(2)
        st = fl.gpu_stats()
        if st:
            peak_v = max(peak_v, st[0]["used"])
            peak_w = max(peak_w, st[0]["power"])
            peak_t = max(peak_t, st[0]["temp"])
        h = fl.get_json("/history/%s" % pid, timeout=30)
        if pid in h:
            break
        if time.time() - t0 > 3600:
            raise SystemExit("run timed out")
    wall = time.time() - t0
    hist = h[pid]
    status = hist.get("status", {})
    files = []
    for _, out in (hist.get("outputs") or {}).items():
        for v in (out.get("images") or []) + (out.get("videos") or []):
            files.append(v)
    region = log_region(before)
    sched = [ln.strip() for ln in region.splitlines()
             if "s/it" in ln and "100%" in ln]
    staged = [ln.strip() for ln in region.splitlines()
              if "Staged" in ln or "Requested to load" in ln]
    rec = {"encoder": key, "prompt": pk, "clip_name": ENCODERS[key],
           "wall_s": round(wall, 1), "peak_vram_mib": peak_v, "peak_power_w": peak_w,
           "peak_temp_c": peak_t, "status": status.get("status_str"),
           "completed": status.get("completed"), "files": files,
           "sampler_last": sched[-1] if sched else None, "log": staged}
    print(json.dumps(rec, indent=1)[:1400], flush=True)
    return rec


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    fl.resolve_forge()
    print("forge:", fl.FORGE, "target:", fl.SSH_TARGET)
    print("config: %dx%d, %d frames (%.2f s @24fps), %d steps, seed %d"
          % (args.width, args.height, LENGTH, LENGTH / 24.0, args.steps, args.seed))
    for k, p in ORDER:
        build(k, p)
    print("all 4 graphs built + dangling-link checked")
    if args.validate_only:
        for k, p in ORDER:
            g = build(k, p)
            print("  %-5s %-3s nodes=%d clip=%s cond=%s"
                  % (k, p, len(g), g["clip"]["inputs"]["clip_name"], g["cond"]["class_type"]))
        return
    if not wait_idle():
        raise SystemExit("forge never went idle — something else is queued")
    # warm the disk cache for the file the app does NOT currently use
    print("pre-reading the int8 encoder from disk (cold read is a one-time cost)...")
    sh("time head -c 1000000 /mnt/models/comfy/text_encoders/%s > /dev/null"
       % ENCODERS["int8"], timeout=60)
    results = []
    for i, (k, p) in enumerate(ORDER, start=1):
        results.append(run(k, p, i, len(ORDER)))
    path = os.path.join(OUTDIR, "results.json")
    with open(path, "w") as fh:
        json.dump({"config": vars(args), "length": LENGTH, "results": results}, fh, indent=1)
    print("\n=== RESULT TABLE (wall seconds) ===")
    for r in results:
        print("  %-5s %-3s  wall %7.1fs  peak VRAM %6d MiB  peak %4d W  %3d C  last %s"
              % (r["encoder"], r["prompt"], r["wall_s"], r["peak_vram_mib"],
                 r["peak_power_w"], r["peak_temp_c"], r["sampler_last"]))
    print("saved:", path)


if __name__ == "__main__":
    main()