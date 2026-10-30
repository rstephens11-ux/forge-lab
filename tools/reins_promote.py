#!/usr/bin/env python3
"""REINS — promote the rendered scenes to 2K (2560x1440) and stitch the master.

UPSCALING, not re-rendering. Re-rendering at a bigger canvas is a DIFFERENT shot — the canvas
changes the render — so upscaling is the only way to keep the motion that was approved.

Rides the app's OWN promote path (upload_image -> promote_graph -> /prompt), then downloads
directly from /history + /view rather than collect(), because collect() looks the run up in the
app's index and a headless submit has no index entry.

⚠️ TWO HARD-WON TRAPS, both measured 2026-10-05:

 1. **promote_graph exposes TWO outputs — `video` (LoadVideo, i.e. the INPUT) and `save` (the
    result).** Taking the first video found silently copies the SOURCE file back and reports
    success at the original resolution. Read the `save` node ONLY.

 2. **Long clips OOM the rig.** `GetVideoComponents` loads every frame as ONE batch, so a 4x
    upscale of 192 / 226 frames reached **58 GB RSS against 60 GB of RAM** and the kernel
    OOM-killed ComfyUI (twice; `NRestarts` climbed, and the in-flight jobs then read as state
    "gone" because the restart wiped their history). 120-frame clips are fine. Hence CHUNKING:
    any scene over --chunk frames is split, promoted in pieces and rejoined. The upscaler is
    per-frame, so a chunk boundary is invisible.

⚠️ Model choice: the app defaults to the PHOTO ESRGAN (right for the photoreal pieces made here).
This spot is ENGRAVING LINE ART, so the CEL/ANIME checkpoint is the correct tool.

    python3 tools/reins_promote.py --src ~/nous-hermes-ad/native-local \\
        --out ~/nous-hermes-ad/2k-local --target 2560x1440
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)


def argval(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


SRC = os.path.expanduser(argval("--src", os.path.expanduser("~/nous-hermes-ad/native-local")))
OUT = os.path.expanduser(argval("--out", os.path.expanduser("~/nous-hermes-ad/2k-local")))
TW, TH = (int(x) for x in argval("--target", "2560x1440").lower().split("x"))
MODEL = argval("--model", "RealESRGAN_x4plus_anime_6B.pth")
CHUNK = int(argval("--chunk", "96"))      # frames per promote job; 120 measured safe, 96 is margin
SONG = os.path.expanduser("~/nous-hermes-ad/song.wav")
os.makedirs(OUT, exist_ok=True)


def probe_frames(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip().split(",")
    try:
        return int(out[0])
    except Exception:
        return 0


def queue_empty():
    try:
        q = fl.get_json("/queue")
        return not (q.get("queue_running") or q.get("queue_pending"))
    except Exception:
        return False


def wait_ready():
    while not queue_empty():
        print("  [gate] forge busy — waiting…", flush=True)
        time.sleep(60)


def download_save_output(pid):
    """ONLY the `save` node. `video` is the LoadVideo INPUT — taking it copies the source back."""
    hist = fl.get_json("/history/%s" % pid, timeout=30)
    rec = hist.get(pid) or {}
    save = (rec.get("outputs") or {}).get("save") or {}
    items = (save.get("images") or []) + (save.get("gifs") or []) + (save.get("videos") or [])
    for item in items:
        q = urllib.parse.urlencode({"filename": item["filename"],
                                    "subfolder": item.get("subfolder", ""),
                                    "type": item.get("type", "output")})
        blob = fl.get_bytes("/view?" + q)
        d = os.path.join(fl.RUNS, pid)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, item["filename"])
        with open(p, "wb") as fh:
            fh.write(blob)
        return p
    return None


def promote_job(src):
    """One promote submission -> local path, or None. Retries once without the audio branch."""
    name = fl.upload_image(src)
    for drop in (False, True):
        g = fl.promote_graph(name, TW, TH, MODEL)
        if drop:
            g["vid"]["inputs"].pop("audio", None)
        r = fl.post_json("/prompt", {"prompt": g, "client_id": fl.CLIENT_ID})
        pid = r.get("prompt_id")
        if not pid:
            print("    submit returned no prompt_id", flush=True)
            continue
        t0, last = time.time(), None
        while time.time() - t0 < 2400:
            time.sleep(5)
            st = fl.run_state(pid)
            if st["state"] != last:
                print("    %6.1fs %s" % (time.time() - t0, st["state"]), flush=True)
                last = st["state"]
            if st["state"] in ("done", "error", "gone"):
                break
        if last == "done":
            p = download_save_output(pid)
            if p:
                return p
            print("    done but no `save` output found", flush=True)
        elif last == "gone":
            print("    state=gone — a restart wipes history; check NRestarts/OOM in the log",
                  flush=True)
        if not drop:
            print("    retrying without the audio branch…", flush=True)
    return None


def promote_scene(src, work):
    """Chunked promote: split if long, promote each piece, rejoin."""
    n = probe_frames(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    if n and n > CHUNK:
        cdir = os.path.join(work, stem + "_chunks")
        if os.path.isdir(cdir):
            shutil.rmtree(cdir)
        os.makedirs(cdir)
        # ⚠️ ffmpeg's segment muxer ADVERTISES -segment_frames but SILENTLY no-ops on these
        # files (a 96-frame setting returned one 180-frame chunk), which would have re-triggered
        # the OOM. Split frame-exact with `select` instead.
        chunks = []
        for k in range((n + CHUNK - 1) // CHUNK):
            a, b = k * CHUNK, min(n - 1, (k + 1) * CHUNK - 1)
            out = os.path.join(cdir, "chunk_%03d.mp4" % k)
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src,
                                "-vf", "select='between(n,%d,%d)',setpts=N/24/TB" % (a, b),
                                "-fps_mode", "passthrough", "-an",
                                "-c:v", "libx264", "-crf", "16", out],
                               capture_output=True, text=True)
            if r.returncode or not os.path.exists(out):
                print("    split failed at chunk %d: %s" % (k, r.stderr[:200]), flush=True)
                return None
            chunks.append(out)
        print("  %s: %d frames -> %d chunks of <=%d" % (stem, n, len(chunks), CHUNK), flush=True)
        ups = []
        for i, c in enumerate(chunks, 1):
            print("    chunk %d/%d (%d fr)" % (i, len(chunks), probe_frames(c)), flush=True)
            u = promote_job(c)
            if not u:
                print("    chunk %d FAILED" % i, flush=True)
                return None
            ups.append(u)
        lst = os.path.join(cdir, "concat.txt")
        with open(lst, "w") as fh:
            for u in ups:
                fh.write("file '%s'\n" % u)
        joined = os.path.join(work, stem + "_joined.mp4")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", joined], capture_output=True, text=True)
        if r.returncode:
            print("    rejoin failed: %s" % r.stderr[:200], flush=True)
            return None
        return joined
    print("  %s: %d frames — single job" % (stem, n), flush=True)
    return promote_job(src)


def main():
    scenes = sorted(f for f in os.listdir(SRC) if f.startswith("scene_") and f.endswith(".mp4"))
    if not scenes:
        print("no scene_*.mp4 in %s" % SRC)
        return 2
    work = os.path.join(OUT, "_work")
    os.makedirs(work, exist_ok=True)
    print("REINS promote -> %dx%d (%.2f MP)  model=%s  chunk<=%d fr"
          % (TW, TH, TW * TH / 1e6, MODEL, CHUNK))
    wait_ready()

    outs, t_all = [], time.time()
    for s in scenes:
        src = os.path.join(SRC, s)
        print("\n%s" % s, flush=True)
        up = promote_scene(src, work)
        if not up:
            print("  %s FAILED" % s, flush=True)
            continue
        dst = os.path.join(OUT, s.replace(".mp4", "_2k.mp4"))
        shutil.copyfile(up, dst)
        pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-count_frames", "-show_entries", "stream=width,height,nb_read_frames",
                             "-of", "csv=p=0", dst], capture_output=True, text=True).stdout.strip()
        print("  -> %s  (%s)" % (os.path.basename(dst), pr), flush=True)
        outs.append(dst)

    if len(outs) != len(scenes):
        print("\nNOT stitching — %d/%d promoted." % (len(outs), len(scenes)))
        return 1

    lst = os.path.join(OUT, "concat.txt")
    with open(lst, "w") as fh:
        for f in outs:
            fh.write("file '%s'\n" % f)
    silent = os.path.join(OUT, "reins_2k_silent.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c", "copy", silent], check=True)
    final = os.path.join(OUT, "reins_2k_master.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", SONG,
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "320k", "-shortest", final], check=True)
    probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,nb_read_frames",
                            "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1",
                            final], capture_output=True, text=True).stdout.strip()
    print("\nMASTER: %s\n%s\ntotal %.1f min" % (final, probe, (time.time() - t_all) / 60.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())