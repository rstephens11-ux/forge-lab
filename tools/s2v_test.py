#!/usr/bin/env python3
"""Wan 2.2 S2V — audio-DRIVEN video. One chunk: the character performs the supplied audio.

Built node-for-node from ComfyUI's shipped `video_wan2_2_14B_s2v` template. The template's
own notes are the authority here, and three of them matter:

  * "77 is the default length from the WAN2.2S2V official code. This model needs at least
    73 frames. If you set the value too high, it might cause an out-of-memory issue. So you
    can just leave 77 as it is."          -> 77 frames @16fps = 4.8s per chunk
  * steps/cfg table: 4 / 1.0 WITH the 4-step lightning LoRA, 20 / 6.0 WITHOUT it
  * "Tested all wan2.2 lightning LoRAs; all worked, but many keys didn't match" — the 4-step
    LoRA is a *t2v* LoRA run on the *s2v* model, so it is a mismatch by design. It is the
    template's default; `--no-lora` is the author's own fallback.

Longer output is NOT a longer `length` — it is chained `WanSoundImageToVideoExtend` chunks,
each adding 77 frames. This tool deliberately builds ONE chunk: the question it answers is
"does it sync?", not "is it beautiful?".

    python3 tools/s2v_test.py --audio a.wav --image ref.png --validate-only
    python3 tools/s2v_test.py --audio a.wav --image ref.png --seconds 4.8
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab",
                                               os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

S2V_UNET = "wan2.2_s2v_14B_fp8_scaled.safetensors"
S2V_AUDIO_ENCODER = "wav2vec2_large_english_fp16.safetensors"
S2V_LORA = "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors"
S2V_FPS = 16.0
S2V_MIN_FRAMES = 73            # the model's floor, per the template note
S2V_CHUNK = 77                 # the template's own default; don't raise it casually
# S2V runs ModelSamplingSD3 at 8 (the template has 8 in both the turbo and 20-step sections),
# unlike Wan t2v turbo, which uses 5.
S2V_SHIFT = 8.0
# The template's own negative prompt, kept verbatim.
S2V_NEG = fl.WAN_NEG

ap = argparse.ArgumentParser()
ap.add_argument("--audio", required=True, help="audio file ON THIS MAC (uploaded to forge)")
ap.add_argument("--image", required=True, help="reference still ON THIS MAC")
ap.add_argument("--prompt", default=("A young man in a plain modern t-shirt looks straight "
                                     "at the camera and sings, mouth and jaw moving with "
                                     "the words, small natural head movements. Static "
                                     "medium close-up, neutral indoor light, plain "
                                     "background, no subtitles, no text."))
ap.add_argument("--seconds", type=float, default=None,
                help="clip length in seconds (snapped to the 16fps frame grid, min 4.56s)")
ap.add_argument("--frames", type=int, default=S2V_CHUNK)
ap.add_argument("--chunks", type=int, default=1,
                help="1 = a single 4.8s chunk. Each EXTRA chunk appends another 77 frames via "
                     "WanSoundImageToVideoExtend + LatentConcat — the only way to get longer "
                     "output, since a larger `length` risks OOM.")
ap.add_argument("--width", type=int, default=640)
ap.add_argument("--height", type=int, default=640)
ap.add_argument("--steps", type=int, default=4)
ap.add_argument("--cfg", type=float, default=1.0)
ap.add_argument("--no-lora", action="store_true",
                help="the author's fallback path: 20 steps / cfg 6.0, no lightning LoRA")
ap.add_argument("--seed", type=int, default=90210)
ap.add_argument("--prefix", default="video/forge_lab_s2v")
ap.add_argument("--native-audio", action="store_true",
                help="⚠️ BROKEN ON FORGE (2026-09-26). Wires LoadAudio into CreateVideo. The "
                     "render completes and then the SAVE dies: avcodec_open2('aac', "
                     "{'movflags': ...}) returned 22 — ComfyUI passes a MUXER option to the "
                     "AAC ENCODER context. Both forge's ffmpeg and its PyAV have the aac "
                     "encoder, so this is an upstream bug, not a missing capability. Default "
                     "OFF: render silent, then mux the driving audio locally (proven).")
ap.add_argument("--validate-only", action="store_true",
                help="check the graph against /object_info and exit — no submit, no GPU")
args = ap.parse_args()

if args.seconds:
    # ⚠️ The Wan latent grid is 1 + 4k (77 = 1 + 4x19), NOT "a multiple of 4" — 80 is OFF the
    # grid. Snap UP to 1+4k, the same rule `wan_frames()` uses for the t2v graphs.
    n = int(round(args.seconds * S2V_FPS))
    args.frames = 1 + 4 * ((n - 1 + 3) // 4)
    if args.frames < S2V_MIN_FRAMES:
        args.frames = S2V_MIN_FRAMES                 # 73, which is itself 1 + 4x18
if args.no_lora:
    args.steps, args.cfg = 20, 6.0


def build(audio_name, image_name):
    g = {
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": fl.WAN_CLIP, "type": "wan"}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0], "text": args.prompt}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0], "text": S2V_NEG}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": fl.WAN_VAE}},
        "aenc": {"class_type": "AudioEncoderLoader",
                 "inputs": {"audio_encoder_name": S2V_AUDIO_ENCODER}},
        "aud": {"class_type": "LoadAudio", "inputs": {"audio": audio_name}},
        "aeout": {"class_type": "AudioEncoderEncode",
                  "inputs": {"audio_encoder": ["aenc", 0], "audio": ["aud", 0]}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": S2V_UNET, "weight_dtype": "default"}},
    }
    src = "unet"
    if not args.no_lora:
        g["lora"] = {"class_type": "LoraLoaderModelOnly",
                     "inputs": {"model": ["unet", 0], "lora_name": S2V_LORA,
                                "strength_model": 1.0}}
        src = "lora"
    g["shift"] = {"class_type": "ModelSamplingSD3",
                  "inputs": {"model": [src, 0], "shift": S2V_SHIFT}}
    g["s2v"] = {"class_type": "WanSoundImageToVideo",
                "inputs": {"positive": ["pos", 0], "negative": ["neg", 0],
                           "vae": ["vae", 0], "width": int(args.width),
                           "height": int(args.height), "length": int(args.frames),
                           "batch_size": 1,
                           "audio_encoder_output": ["aeout", 0],
                           "ref_image": ["img", 0]}}
    def ks(pos, neg, latent, seed):
        # ⚠️ The slots are the whole point. Both WanSoundImageToVideo and ...Extend emit
        # [positive, negative, latent] — so the sampler must read slot 0 / 1 / 2. An earlier
        # version of this helper took the node id three times and hardcoded slot 0, which fed
        # CONDITIONING into latent_image; forge rejected it with return_type_mismatch.
        return {"class_type": "KSampler",
                "inputs": {"model": ["shift", 0], "positive": [pos, 0], "negative": [neg, 1],
                           "latent_image": [latent, 2], "seed": int(seed),
                           "steps": int(args.steps), "cfg": float(args.cfg),
                           "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}}

    g["samp"] = ks("s2v", "s2v", "s2v", args.seed)
    acc = "samp"
    # Each extend appends ONE more chunk to the accumulated latent. Note what the template
    # does NOT do: it does not slice the audio per chunk. The SAME audio_encoder_output goes
    # to every chunk, and the node derives its offset from how much latent already exists —
    # so the audio is ONE continuous track across the whole clip. That is exactly the
    # "one song, uninterrupted" property Ryan asked for, and it comes free.
    for i in range(1, args.chunks):
        ext, k, cat = "ext%d" % i, "ks%d" % i, "cat%d" % i
        g[ext] = {"class_type": "WanSoundImageToVideoExtend",
                  "inputs": {"positive": ["pos", 0], "negative": ["neg", 0],
                             "vae": ["vae", 0], "length": int(args.frames),
                             "video_latent": [acc, 0],
                             "audio_encoder_output": ["aeout", 0],
                             "ref_image": ["img", 0]}}
        g[k] = ks(ext, ext, ext, args.seed + i)
        g[cat] = {"class_type": "LatentConcat",
                  "inputs": {"samples1": [acc, 0], "samples2": [k, 0], "dim": "t"}}
        acc = cat

    g["vdec"] = {"class_type": "VAEDecode",
                 "inputs": {"samples": [acc, 0], "vae": ["vae", 0]}}
    # ⚠️ CreateVideo has an OPTIONAL `audio` input and the shipped template wires LoadAudio
    # into it. It does NOT work on forge (2026-09-26): the render completes and then the SAVE
    # fails — `avcodec_open2("aac", {'movflags': 'use_metadata_tags+faststart'}) returned 22`.
    # ComfyUI hands a MUXER option to the AAC ENCODER context. forge's ffmpeg AND its PyAV
    # (18.1.0, libavcodec 62.28) both report the aac encoder present, so this is an upstream
    # bug, not a missing capability. Hence opt-in only; default is render-silent + local mux.
    g["mkvid"] = {"class_type": "CreateVideo",
                  "inputs": {"images": ["vdec", 0], "fps": S2V_FPS}}
    if args.native_audio:
        g["mkvid"]["inputs"]["audio"] = ["aud", 0]
    g["save"] = {"class_type": "SaveVideo",
                 "inputs": {"video": ["mkvid", 0], "filename_prefix": args.prefix,
                            "format": "auto", "format.codec": "auto", "codec": "auto"}}
    return g


def validate(g):
    """Node types, input names, required inputs and links, against the LIVE server."""
    oi = fl.get_json("/object_info", timeout=120)
    bad = []
    for nid, n in g.items():
        ct = n["class_type"]
        if ct not in oi:
            bad.append("%s.%s: NO SUCH NODE" % (nid, ct)); continue
        req = oi[ct]["input"].get("required", {}) or {}
        opt = oi[ct]["input"].get("optional", {}) or {}
        for k in (n.get("inputs") or {}):
            if k not in req and k not in opt:
                # A dotted key is a ComfyUI *dynamic-combo* sub-key (e.g. SaveVideo's
                # `format.codec`). It rides alongside the combo widget and is not a real
                # input, so it must not be reported as an unknown input.
                if "." in k:
                    continue
                bad.append("%s.%s: unknown input %r" % (nid, ct, k))
        for k in req:
            if k not in (n.get("inputs") or {}):
                bad.append("%s.%s: MISSING required %r" % (nid, ct, k))
        for k, v in (n.get("inputs") or {}).items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] not in g:
                bad.append("%s.%s: dangling link -> %s" % (nid, k, v[0]))
        # TYPE checking — names alone are not enough. This is the check that would have caught
        # the slot bug above offline: a link can point at a real node and still carry the
        # wrong type (conditioning into a LATENT input), and forge only rejects it at submit.
        for k, v in (n.get("inputs") or {}).items():
            if not (isinstance(v, list) and len(v) == 2 and v[0] in g):
                continue
            src_ct = g[v[0]]["class_type"]
            src_out = (oi.get(src_ct) or {}).get("output") or []
            if v[1] >= len(src_out):
                bad.append("%s.%s: source %s has no output slot %s" % (nid, k, v[0], v[1]))
                continue
            want = req.get(k, opt.get(k))
            if not want:
                continue
            want_t = want[0]
            got_t = src_out[v[1]]
            want_set = set(want_t) if isinstance(want_t, list) else {want_t}
            if "*" in want_set or got_t == "*" or got_t in want_set:
                continue
            bad.append("%s.%s: TYPE %s input got %s from %s.slot%s"
                       % (nid, k, "/".join(sorted(want_set)), got_t, v[0], v[1]))
    return bad


def out_frames(frames, chunks):
    """Chained output length. LatentConcat ADDS latents and does NOT dedupe the seam, so the
    naive `frames * chunks` is wrong: 3x77 is 237 frames, not 231.
    L = (frames-1)/4 + 1 latents per chunk (77 -> 20); total = 4*(chunks*L - 1) + 1.
    """
    L = (frames - 1) // 4 + 1
    return 4 * (chunks * L - 1) + 1


total = out_frames(args.frames, args.chunks)
print("S2V: %dx%d, %d chunk(s) x %d frames -> %d frames = %.2fs @16fps"
      % (args.width, args.height, args.chunks, args.frames, total, total / S2V_FPS))
if args.chunks > 1:
    print("     (seams are NOT deduped: %d, not %d — cut the audio to %.2fs to match)"
          % (total, args.frames * args.chunks, total / S2V_FPS))
print("path: %s  (%d steps, cfg %.1f, shift %.1f)"
      % ("20-step, no LoRA" if args.no_lora else "4-step lightning LoRA",
         args.steps, args.cfg, S2V_SHIFT))
print("audio : %s" % args.audio)
print("image : %s" % args.image)

g = build(os.path.basename(args.audio), os.path.basename(args.image))
fl._check_graph(g)
problems = validate(g)
print("\nvalidated %d nodes against forge /object_info" % len(g))
print(problems and "\n".join("   " + p for p in problems) or "   no problems, no dangling links")

if args.validate_only:
    print("\n--validate-only: nothing submitted, no GPU touched.")
    sys.exit(1 if problems else 0)

for p in (args.audio, args.image):
    if not os.path.exists(p):
        sys.exit("missing file: %s" % p)

aup = fl.upload_image(args.audio)          # same multipart path; the endpoint takes any file
iup = fl.upload_image(args.image)
print("\nstaged on forge: audio=%s image=%s" % (aup, iup))
g = build(aup, iup)
fl._check_graph(g)

t0 = time.time()
r = fl.post_json("/prompt", {"prompt": g, "client_id": fl.CLIENT_ID})
pid = r.get("prompt_id")
if not pid:
    sys.exit("forge returned no prompt_id: %r" % r)
print("submitted %s" % pid[:8])
last = None
while True:
    time.sleep(4)
    st = fl.run_state(pid)
    el = time.time() - t0
    if st["state"] != last:
        print("  %6.1fs  %s %s" % (el, st["state"], st.get("detail", "")), flush=True)
        last = st["state"]
    if st["state"] in ("done", "error", "gone") or el > 3600:
        break
wall = time.time() - t0

hist = fl.get_json("/history/%s" % pid, timeout=60).get(pid) or {}
print("\nstatus: %s   forge compute: see service log" % (hist.get("status", {}).get("status_str")))
files = []
d = os.path.join(fl.RUNS, pid)
os.makedirs(d, exist_ok=True)
for _nid, out in (hist.get("outputs") or {}).items():
    for v in (out.get("images") or []) + (out.get("videos") or []) + (out.get("gifs") or []):
        from urllib.parse import urlencode
        blob = fl.get_bytes("/view?" + urlencode({"filename": v["filename"],
                                                  "subfolder": v.get("subfolder", ""),
                                                  "type": v.get("type", "output")}))
        dst = os.path.join(d, v["filename"])
        open(dst, "wb").write(blob)
        files.append(dst)
print("wall %.1fs · %d file(s)" % (wall, len(files)))

# S2V outputs PICTURE ONLY — the audio that drove it has to be muxed back on, or there is
# nothing to judge the sync against. Mux the ORIGINAL slice, not a re-encode of it.
# ⚠️ Iterate the render files, NOT `files` — appending to a list while iterating it loops
# forever and prefixes the name every pass (it produced 21 nested files before it stopped).
def has_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())

# CreateVideo now carries the audio natively, so this is a FALLBACK for a render that came
# back silent — it used to be mandatory because my graph omitted CreateVideo's `audio`.
renders = [f for f in files if f.endswith((".mp4", ".webm"))]
out = os.path.join(d, "s2v_synced.mp4")
if renders and not has_audio(renders[0]):
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", renders[0], "-i", args.audio,
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
           "-b:a", "192k", out]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0 and os.path.exists(out):
        print("render was silent; muxed the driving audio back: %s" % out)
        files.append(out)
    else:
        print("mux failed: %s" % (p.stderr or "")[:200])
elif renders:
    print("render already carries its audio natively — no mux needed")

print("\nfiles:")
for f in files:
    print("   ", f)
print("\nsummary: %s" % json.dumps({"pid": pid, "wall_s": round(wall, 1),
                                     "frames_out": total, "fps": S2V_FPS,
                                     "chunks": args.chunks, "steps": args.steps,
                                     "cfg": args.cfg, "lora": not args.no_lora,
                                     "size": [args.width, args.height],
                                     "files": files}, indent=1))