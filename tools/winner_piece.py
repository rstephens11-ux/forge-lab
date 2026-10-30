#!/usr/bin/env python3
"""The full 90-second piece: 9 scenes, the big-eyed character, the Winner Takes It All track.

Every scene goes through the app's own submit()/run_state()/collect(). Each scene is handed
ITS OWN window of the song (the AddGuide anchor sits at the start of each clip, so a scene
given the whole track performs the opening seconds instead of its own — the bug Ryan caught
by ear). The stitched picture is >= the song, so apad pads rather than -shortest trimming the
track's ending.
"""
import importlib.util
import json
import os
import subprocess
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sp = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(sp)
sp.loader.exec_module(fl)

IN = os.path.join(HERE, "inputs")
OUT = os.path.join(fl.DATA, "episodes", "winner-bigeyes")
os.makedirs(OUT, exist_ok=True)
SPEC = json.load(open(os.path.join(IN, "winner_bigeyes_scenes.json")))
REF = SPEC["reference"]
SONG = SPEC["song"]
SHOTS = SPEC["scenes"]
LEN = int(SPEC.get("scene_frames", 241))
FPS = 24.0
SCENE_S = LEN / FPS
print("%d scenes x %d frames (%.2f s) = %.2f s of picture" % (len(SHOTS), LEN, SCENE_S,
                                                             len(SHOTS) * SCENE_S))
print("song: %.2f s   reference: %s" % (float(subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", SONG],
    capture_output=True, text=True).stdout.strip()), os.path.basename(REF)))

t0 = time.time()
files = []
for i, shot in enumerate(SHOTS, start=1):
    win = os.path.join(IN, "_w%02d.wav" % i)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.3f" % ((i - 1) * SCENE_S),
                    "-t", "%.3f" % SCENE_S, "-i", SONG, win], check=True)
    p = {"prompt": shot, "video_mode": "r2v", "lora": None, "lora_strength": 1.0,
         "steps": 4, "length": LEN, "width": 832, "height": 480,
         "seed": 93000 + i * 131, "audio": True, "guide_audio_path": win,
         "prefix": "video/winner_final"}
    t1 = time.time()
    try:
        pid, seed = fl.submit("video", p, ref_paths=[REF])
    except Exception as exc:
        print("  scene %d SUBMIT FAILED: %s" % (i, exc), flush=True)
        continue
    last = None
    while time.time() - t1 < 420:
        time.sleep(5)
        st = fl.run_state(pid)
        if st["state"] != last:
            print("  scene %d/%d %6.1fs %s %s" % (i, len(SHOTS), time.time() - t1,
                                                  st["state"], st.get("detail", "")), flush=True)
            last = st["state"]
        if st["state"] in ("done", "error", "gone"):
            break
    if st["state"] != "done":
        print("  scene %d FAILED (%s)" % (i, st["state"]), flush=True)
        continue
    meta = fl.collect(pid)
    fs = [os.path.join(fl.RUNS, pid, f["file"]) for f in (meta.get("images") or [])]
    clean = next((f for f in fs if "synced" not in f), None)
    if clean:
        files.append(clean)
    print("  scene %d done in %.0fs -> %s" % (i, time.time() - t1,
                                              os.path.basename(clean or "-")), flush=True)

print("\ndrafted %d/%d in %.1f min" % (len(files), len(SHOTS), (time.time() - t0) / 60.0))
if len(files) < 2:
    raise SystemExit("not enough scenes")

lst = os.path.join(OUT, "concat.txt")
with open(lst, "w") as fh:
    for f in files:
        fh.write("file '%s'\n" % f.replace("'", "'\\''"))
silent = os.path.join(OUT, "piece_silent.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                "-c", "copy", silent], check=True)
final = os.path.join(OUT, "winner_takes_it_all.mp4")
# -af apad makes the padded audio the longer stream, so -shortest ends at the PICTURE and no
# frame is lost; if the picture is longer, the track is padded with silence instead of cut.
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", SONG, "-map", "0:v:0",
                "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-af", "apad", "-shortest", final], check=True)
j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
    "-count_frames", "-show_entries", "stream=nb_read_frames,width,height",
    "-show_entries", "format=duration", "-of", "json", final],
    capture_output=True, text=True).stdout)
v = j["streams"][0]
print("\nPIECE: %s" % final)
print("  %sx%s  %s frames  %.2f s  (%d scenes, H3 draft path)"
      % (v["width"], v["height"], v.get("nb_read_frames"), float(j["format"]["duration"]),
         len(files)))
print("  total wall: %.1f min" % ((time.time() - t0) / 60.0))
json.dump({"scenes": files, "final": final, "frames": v.get("nb_read_frames")},
          open(os.path.join(OUT, "piece.json"), "w"), indent=1)