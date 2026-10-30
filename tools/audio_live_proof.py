#!/usr/bin/env python3
"""LIVE proof driver for the Forge Lab audio service -- TOUCHES forge's GPU1.

Unlike tools/audio_matrix.py (GPU-free, stubbed), this actually runs the client against
the rig: design a voice, clone it for a second line, force-align both, and ASR-check both
for intelligibility. Writes wavs + words.json + proof.json to --out (default
~/forge-lab-work/audio-sample).

  /opt/homebrew/bin/python3 tools/audio_live_proof.py [--out DIR]

It refuses nothing (audio jobs do not queue in ComfyUI), but it does load a few GB onto
GPU1 -- make sure the LLM/Raylight are not resident there first.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import client  # noqa: E402

DESC = "a flat, nasal, middle-aged man with a dry Maine accent, deadpan, slightly too loud"
L1 = "I'd like a hat. A big one."
L2 = "No, bigger than that."


def gpu1():
    """Read GPU1 memory by selecting the index COLUMN.

    ⚠️ Do NOT trust `-i 1` alone and take splitlines()[0]: if the filter is not honoured
    nvidia-smi prints a row per GPU and [0] is GPU0 (ComfyUI, ~23 GB), which produced a
    bogus 23015 MiB "peak" on the first live run.
    """
    p = subprocess.run(["ssh", client.HOST,
        "nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits"],
        capture_output=True, text=True)
    for line in p.stdout.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2 and parts[0] == "1":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def duration(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nk=1:nw=1", path], capture_output=True, text=True)
    return float(p.stdout.strip()) if p.stdout.strip() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/forge-lab-work/audio-sample"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    stop = threading.Event()
    peak = [0]

    def poll():
        while not stop.is_set():
            m = gpu1()
            if m is not None:
                peak[0] = max(peak[0], m)
            time.sleep(0.3)

    results = {"desc": DESC, "line1": L1, "line2": L2}
    print("deploying...", flush=True)
    client.deploy()
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    words1 = words2 = None
    try:
        print("=== tts_design (cold) ===", flush=True)
        t0 = time.time()
        info1 = client.tts_design(DESC, L1, os.path.join(args.out, "line1.wav"))
        w1 = time.time() - t0
        results["design"] = {"info": info1, "wall_seconds": round(w1, 1)}
        print(json.dumps(info1), "wall=%.1fs" % w1, flush=True)

        print("=== tts_clone (line2 from the line1 sample) ===", flush=True)
        t0 = time.time()
        info2 = client.tts_clone(os.path.join(args.out, "line1.wav"), L1, L2,
                                 os.path.join(args.out, "line2.wav"))
        w2 = time.time() - t0
        results["clone"] = {"info": info2, "wall_seconds": round(w2, 1)}
        print(json.dumps(info2), "wall=%.1fs" % w2, flush=True)

        for tag, text, fname in (("line1", L1, "line1.words.json"),
                                 ("line2", L2, "line2.words.json")):
            print("=== align %s ===" % tag, flush=True)
            t0 = time.time()
            words = client.align(os.path.join(args.out, tag + ".wav"), text)
            dt = time.time() - t0
            with open(os.path.join(args.out, fname), "w") as f:
                json.dump(words, f, indent=2)
            results["align_" + tag] = {"count": len(words), "wall_seconds": round(dt, 1)}
            print("align %s: %d words wall=%.1fs" % (tag, len(words), dt), flush=True)

        print("=== ASR intelligibility check ===", flush=True)
        for tag, intended in (("line1", L1), ("line2", L2)):
            t0 = time.time()
            heard = client.transcribe(os.path.join(args.out, tag + ".wav"))
            dt = time.time() - t0
            ratio = SequenceMatcher(None, intended.lower(), heard.lower()).ratio()
            results.setdefault("asr", {})[tag] = {
                "text": heard, "intended": intended,
                "match_ratio": round(ratio, 3), "wall_seconds": round(dt, 1)}
            print("%s ASR=%r match=%.2f wall=%.1fs" % (tag, heard, ratio, dt), flush=True)
    finally:
        stop.set()

    results["gpu1_peak_mib"] = peak[0]
    results["gpu1_after_mib"] = gpu1()
    results["durations_s"] = {
        "line1": duration(os.path.join(args.out, "line1.wav")),
        "line2": duration(os.path.join(args.out, "line2.wav")),
    }
    with open(os.path.join(args.out, "proof.json"), "w") as f:
        json.dump(results, f, indent=2)

    print("\n===== SUMROBIN =====")
    print(json.dumps({k: results[k] for k in
        ("design", "clone", "align_line1", "align_line2", "asr",
         "gpu1_peak_mib", "gpu1_after_mib", "durations_s")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())