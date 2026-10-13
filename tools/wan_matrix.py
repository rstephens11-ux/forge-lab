#!/usr/bin/env python3
"""GPU-free proof that WAN's path rules are BAKED IN and cannot be half-switched.

Every rule here degrades the output with NO error in ComfyUI if it is set wrong, which is
the entire reason WAN is a dropdown of PATHS in Forge Lab rather than a pile of knobs. This
builds every (variant x quality) graph through the app's OWN builder and asserts the rules,
plus the refusals for the combinations the API cannot express. Nothing is submitted to forge
and no GPU is touched — it is pure graph inspection, so it is safe to run any time.

    python3 tools/wan_matrix.py        # exits 1 on any failure
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab",
                                               os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def nodes(g, cls):
    return {k: v for k, v in g.items() if v["class_type"] == cls}


def build(vm, qk, **over):
    args = {"mode": vm, "prompt": "a test prompt", "length": 81, "width": 832,
            "height": 480, "quality": qk, "seed": 1234567}
    if fl.WAN_MODES[vm]["refs"] >= 1:
        args["start_name"] = "start.png"
    if fl.WAN_MODES[vm]["refs"] >= 2:
        args["last_name"] = "last.png"
    args.update(over)
    return fl.wan_graph(**args)


print("=" * 78)
print("WAN path matrix — one graph per (variant x quality), built by the app's builder")
print("=" * 78)
for vm in [k for k in fl.WAN_MODES if k != "s2v"]:   # s2v has its own section below
    for qk in fl.WAN_QUALITY:
        r = fl.wan_rules(vm, qk)
        g = build(vm, qk)
        print("\n%s / %s" % (vm, qk))

        # 1 · shift — 5.0 WITH the lightning LoRAs, 8.0 WITHOUT, on BOTH experts
        sh = [n["inputs"]["shift"] for n in nodes(g, "ModelSamplingSD3").values()]
        check(sh == [r["shift"], r["shift"]],
              "shift == %.1f on BOTH experts (found %s)" % (r["shift"], sh))

        # 2 · the lightning LoRA pair — per TASK, and present IFF turbo
        lo = nodes(g, "LoraLoaderModelOnly")
        check(bool(lo) == bool(r["lora"]),
              "LoRA nodes present == turbo (turbo=%s, found %d)" % (r["turbo"], len(lo)))
        lnames = set(n["inputs"]["lora_name"] for n in lo.values())
        check((not r["lora"]) or lnames == set(r["lora"]),
              "LoRA pair is this task's pair" if r["lora"] else "no LoRAs on the full path")

        # 3 · steps + cfg + the two-stage split
        sc = {(n["inputs"]["steps"], n["inputs"]["cfg"])
              for n in nodes(g, "KSamplerAdvanced").values()}
        check(sc == {(r["steps"], r["cfg"])},
              "both samplers at steps=%d cfg=%.1f (found %s)" % (r["steps"], r["cfg"], sc))
        hi, low = g["samp_hi"]["inputs"], g["samp_lo"]["inputs"]
        check(hi["end_at_step"] == r["half"] and low["start_at_step"] == r["half"],
              "high 0->%d, low %d->end" % (r["half"], r["half"]))
        check(hi["add_noise"] == "enable" and low["add_noise"] == "disable",
              "add_noise enable -> disable")
        check(hi["return_with_leftover_noise"] == "enable"
              and low["return_with_leftover_noise"] == "disable",
              "leftover-noise carry is set on the first stage only")

        # 4 · the expert pair — per VARIANT (t2v experts vs i2v experts)
        un = sorted(n["inputs"]["unet_name"] for n in nodes(g, "UNETLoader").values())
        check(un == sorted(r["unet"]), "expert pair is this variant's pair")
        check(g["clip"]["inputs"]["type"] == "wan" and g["vae"]["inputs"]["vae_name"] == fl.WAN_VAE,
              "umt5 clip at type 'wan' + wan 2.1 VAE")

        # 5 · conditioning wiring — the image must not be silently droppable
        if r["node"] is None:
            check("load_start" not in g and "cond" not in g,
                  "t2v wires NO image loader and no fused node")
            check(hi["positive"] == ["pos", 0] and hi["latent_image"] == ["latent", 0],
                  "t2v conditions straight off CLIPTextEncode + empty latent")
            check(g["latent"]["class_type"] == "EmptyHunyuanLatentVideo",
                  "t2v keeps the VERIFIED latent node")
        else:
            check(g["cond"]["class_type"] == r["node"], "fused node is %s" % r["node"])
            check(hi["positive"] == ["cond", 0] and hi["negative"] == ["cond", 1]
                  and hi["latent_image"] == ["cond", 2],
                  "samplers take the FUSED conditioning AND latent (image cannot drop)")
            check("start_image" in g["cond"]["inputs"], "start_image wired")
            check(("end_image" in g["cond"]["inputs"]) == (fl.WAN_MODES[vm]["refs"] >= 2),
                  "end_image wired IFF flf2v")

        # 6 · output truth — 16 fps, and no audio path anywhere
        check(g["mkvid"]["inputs"]["fps"] == fl.WAN_FPS, "CreateVideo at 16 fps")
        check(not any("Audio" in v["class_type"] for v in g.values()), "no audio nodes")

print("\n" + "=" * 78)
print("frame grid — the Wan* nodes take length in steps of 4 from 1")
print("=" * 78)
for sec, want in ((1, 17), (3, 49), (5, 81), (10, 161)):
    got = fl.wan_frames(sec)
    check(got == want and (got - 1) % 4 == 0, "%.0fs -> %d frames (1+4k)" % (sec, got))

print("\n" + "=" * 78)
print("estimate must reproduce BOTH measured points (832x480, 81 frames, one 3090)")
print("=" * 78)
e_t = fl.estimate_seconds("wan", {"width": 832, "height": 480, "seconds": 5,
                                  "quality": "Turbo 4 steps"})
e_f = fl.estimate_seconds("wan", {"width": 832, "height": 480, "seconds": 5,
                                  "quality": "Full 20 steps"})
check(abs(e_t - 105.7) < 3.0, "turbo estimate %.1fs vs measured 105.7s" % e_t)
check(abs(e_f - 942.8) < 12.0, "full estimate %.1fs vs measured 942.8s" % e_f)

print("\n" + "=" * 78)
print("refusals — the mis-sets this design exists to make impossible")
print("=" * 78)
for label, over in (("t2v handed an image", {"mode": "t2v", "start_name": "a.png"}),
                    ("i2v with no image", {"mode": "i2v"}),
                    ("flf2v with only a first frame",
                     {"mode": "flf2v", "start_name": "a.png"})):
    kw = {"prompt": "x", "length": 81, "width": 832, "height": 480,
          "quality": fl.WAN_QUALITY_DEFAULT, "seed": 1}
    kw.update(over)
    try:
        fl.wan_graph(**kw)
        check(False, "%s -> ACCEPTED (it must refuse)" % label)
    except ValueError as exc:
        check(True, "%s -> refused: %s" % (label, str(exc)[:70]))

print("\n" + "=" * 78)
print("H3 AUDIO toggle — 'off' must actually remove the audio path, not just relabel it")
print("=" * 78)
for aud in (True, False):
    g = fl.h3_graph("i2v", "x", [], 124, 544, 736, 12, 42, audio=aud)
    has_adec = any(v["class_type"] == "VAEDecodeAudio" for v in g.values())
    linked = "audio" in g["mkvid"]["inputs"]
    check(has_adec == aud and linked == aud,
          "audio=%-5s -> VAEDecodeAudio=%s, CreateVideo.audio wired=%s"
          % (aud, has_adec, linked))
    # the video path must be IDENTICAL either way: the AV latent is joint, so this is a
    # decision about the FILE, not about the render
    check(any(v["class_type"] == "VAEDecode" for v in g.values())
          and "samp" in g and g["samp"]["inputs"]["latent_image"] == ["cond", 1],
          "audio=%-5s -> video path unchanged" % aud)

print("\n" + "=" * 78)
print("RAYLIGHT 2-GPU — the SAME graph, transformed; the 1-card path must be untouched")
print("=" * 78)
_RK = dict(prompt="x", refs=[], length=124, width=544, height=736, steps=4, seed=42,
           lora="character_lora_v2.safetensors", turbo=True)
_g1 = fl.h3_graph("i2v", **_RK)                 # default gpus=1 = today's path
_g2 = fl.h3_graph("i2v", gpus=2, **_RK)
_c1 = {v["class_type"] for v in _g1.values()}
_c2 = {v["class_type"] for v in _g2.values()}

_RAY = ("RayInitializer", "RayUNETLoader", "RayBasicScheduler", "RayBasicGuider",
        "XFuserSamplerCustomAdvanced", "RayLoraLoader")
_STD = ("UNETLoader", "BasicScheduler", "BasicGuider", "SamplerCustomAdvanced",
        "LoraLoaderModelOnly", "RandomNoise")
for _c in _RAY:
    check(_c not in _c1, "1 card  -> no %s" % _c)
    check(_c in _c2, "2 cards -> %s present" % _c)
for _c in _STD:
    check(_c in _c1, "1 card  -> %s still standard" % _c)
    check(_c not in _c2, "2 cards -> %s GONE" % _c)

# the seed must MOVE into the sampler, not vanish with the RandomNoise node
check(_g2["samp"]["inputs"].get("noise_seed") == 42,
      "2 cards -> seed carried into XFuserSamplerCustomAdvanced.noise_seed")
check("noise" not in _g2, "2 cards -> RandomNoise dropped, not left dangling")

# the LoRA chain must keep its ORDER: turbo first, then the character LoRA
check(_g2["chan"]["inputs"]["lora_name"] == fl.H3_TURBO["i2v"]
      and "prev_ray_lora" not in _g2["chan"]["inputs"],
      "2 cards -> turbo LoRA is the HEAD of the RAY_LORA chain")
check(_g2["chan2"]["inputs"].get("prev_ray_lora") == ["chan", 0],
      "2 cards -> character LoRA chains FROM the turbo LoRA")
check(_g2["unet"]["inputs"].get("lora") == ["chan2", 0],
      "2 cards -> RayUNETLoader takes the END of the chain")
check(_g2["unet"]["inputs"].get("ray_actors_init") == ["init", 0]
      and _g2["init"]["inputs"]["GPU"] == 2
      and _g2["init"]["inputs"]["ulysses_degree"] == 2,
      "2 cards -> loader keyed to the initializer, GPU=2, ulysses=2")
check(_g2["sched"]["inputs"]["ray_actors"] == ["unet", 0]
      and _g2["guider"]["inputs"]["ray_actors"] == ["unet", 0],
      "2 cards -> sched/guider key on ray_actors, NOT on a model edge")

# the LAZY path: everything off the model chain must be byte-identical
for _n in ("vae", "avae", "clip", "cond", "vdec", "mkvid", "save"):
    check(_g1[_n] == _g2[_n], "2 cards -> %s node is byte-identical to 1 card" % _n)

# the Ray actors MUST release VRAM when the job ends: left False, they hold ~20.8 GB
# per card at idle and the NEXT run fails. Measured on forge 2026-10-02.
check(fl.ray_initializer(2)["clear_vram_after_sampling"] is True,
      "ray_initializer releases worker VRAM after sampling (else the next run OOMs)")
check(fl.ray_initializer(2)["FSDP"] is False,
      "FSDP stays OFF (it needs the cu12 NCCL that breaks a cu13 torch)")
check(fl.ray_initializer(1)["GPU"] == 1 and fl.ray_initializer(1)["ulysses_degree"] == 1,
      "ray_initializer(1) is a valid single-rank config")

# and the transform must never be reachable by accident
check(fl.h3_graph("i2v", prompt="x", refs=[], length=124, width=544, height=736,
                  steps=4, seed=1, gpus=1)["unet"]["class_type"] == "UNETLoader",
      "gpus=1 (the default) still builds a plain UNETLoader")

# a shift/LoRA/steps argument must not even exist to pass
import inspect
_params = list(inspect.signature(fl.wan_graph).parameters)
check(not any(p in _params for p in ("shift", "lora", "turbo")),
      "wan_graph takes no shift/lora/turbo argument (params: %s)" % ", ".join(_params[1:]))

print("\n" + "=" * 78)
print("WAN 2.2 S2V — audio-DRIVEN. ONE path, and the chaining rules")
print("=" * 78)
S2V_KEYS = ("steps", "shift", "cfg", "lora", "turbo")
for n_chunks in (1, 2, 3):
    p = {"wan_mode": "s2v", "ref_names": ["alice.png"], "audio_name": "song.wav",
         "s2v_chunks": n_chunks, "width": 640, "height": 640, "prompt": "he sings"}
    g, _seed = fl.build_graph("wan", p)
    print("\ns2v / %d chunk(s)" % n_chunks)

    # 1 · the baked path: 4 steps, cfg 1.0, shift 8, LoRA ON — on EVERY sampler
    r = fl.wan_rules("s2v", None)
    check((r["steps"], r["cfg"], r["shift"]) == (4, 1.0, 8.0),
          "rules are 4 steps / cfg 1.0 / shift 8.0")
    samps = nodes(g, "KSampler")
    check(samps and all(n["inputs"]["steps"] == 4 and n["inputs"]["cfg"] == 1.0
                        for n in samps.values()),
          "every sampler at steps=4 cfg=1.0 (%d samplers)" % len(samps))
    sh = {n["inputs"]["shift"] for n in nodes(g, "ModelSamplingSD3").values()}
    check(sh == {8.0}, "shift 8.0 (S2V's own — NOT WAN turbo's 5.0), found %s" % sh)
    lo = nodes(g, "LoraLoaderModelOnly")
    check(len(lo) == 1 and list(lo.values())[0]["inputs"]["lora_name"] == fl.S2V_LORA
          and list(lo.values())[0]["inputs"]["strength_model"] == 1.0,
          "exactly ONE model-only LoRA, strength 1.0 (not WAN's per-expert PAIR)")

    # 2 · the SLOT bug that forge caught: latent must come from slot 2, never slot 0
    for k, n in samps.items():
        src = n["inputs"]["latent_image"][0]
        slot = n["inputs"]["latent_image"][1]
        check(slot == 2 and src in g and g[src]["class_type"] in
              ("WanSoundImageToVideo", "WanSoundImageToVideoExtend"),
              "%s: latent_image <- %s.slot%d (the LATENT output, not conditioning)"
              % (k, src, slot))

    # 3 · chaining: chunks-1 extends, each followed by a LatentConcat on 't'
    exts = nodes(g, "WanSoundImageToVideoExtend")
    cats = nodes(g, "LatentConcat")
    check(len(exts) == n_chunks - 1 and len(cats) == n_chunks - 1,
          "%d extend(s) + %d LatentConcat for %d chunks" % (len(exts), len(cats), n_chunks))
    check(all(c["inputs"]["dim"] == "t" for c in cats.values()), "concat is on 't'")
    check(len(nodes(g, "WanSoundImageToVideo")) == 1, "exactly one root S2V node")
    # ONE continuous audio track: the SAME encoder output feeds every chunk
    feeds = {tuple(n["inputs"]["audio_encoder_output"]) for n in exts.values()}
    feeds.add(tuple(g["s2v"]["inputs"]["audio_encoder_output"]))
    check(feeds == {("aeout", 0)},
          "every chunk reads the SAME audio_encoder_output (audio is NOT sliced per chunk)")

    # 4 · output: the measured length, and NO audio in the file (forge's AAC bug)
    want_fr = fl.s2v_chunks_to_frames(n_chunks)
    check(want_fr == 4 * (n_chunks * 20 - 1) + 1,
          "%d chunk(s) -> %d frames = %.2f s (LatentConcat ADDS, it does not dedupe)"
          % (n_chunks, want_fr, want_fr / 16.0))
    check(g["mkvid"]["inputs"]["fps"] == 16.0, "CreateVideo at 16 fps")
    check("audio" not in g["mkvid"]["inputs"],
          "CreateVideo carries NO audio — forge's ComfyUI dies opening the AAC encoder")

print("\nrefusals — S2V cannot run without its two inputs")
for label, over in (("no still", {"audio_name": "a.wav"}),
                    ("no audio", {"start_name": "x.png"})):
    kw = {"prompt": "x", "length": 77, "width": 640, "height": 640,
          "quality": fl.S2V_QUALITY, "seed": 1}
    kw.update(over)
    try:
        fl.wan_graph("s2v", **kw)
        check(False, "%s -> ACCEPTED (it must refuse)" % label)
    except ValueError as exc:
        check(True, "%s -> refused: %s" % (label, str(exc)[:60]))

print("\nS2V estimate must reproduce BOTH measured points (640x640, 4 steps)")
for n_chunks, want in ((1, 112.0), (3, 348.0)):
    e = fl.estimate_seconds("wan", {"wan_mode": "s2v", "s2v_chunks": n_chunks,
                                    "width": 640, "height": 640}, warm_engine="h3")
    check(abs(e - want) < 2.0, "%d chunk(s): estimate %.0fs vs measured %.0fs"
          % (n_chunks, e, want))

_p2 = list(inspect.signature(fl.s2v_graph).parameters)
check(not any(x in _p2 for x in S2V_KEYS) and "chunks" in _p2,
      "s2v_graph takes NO steps/shift/cfg/LoRA argument (params: %s)" % ", ".join(_p2))

print("\n%d failure(s)" % len(FAILS))
for f in FAILS:
    print("   FAILED: %s" % f)
sys.exit(1 if FAILS else 0)