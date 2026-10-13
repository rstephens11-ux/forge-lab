#!/usr/bin/env python3
"""GPU-free proof that LTX 2.3 IC-LoRA's rules are BAKED IN and cannot be half-switched.

Every one of these rules degrades the output with NO error in ComfyUI if set wrong — which is
why the engine is a MODEL in the list rather than a pile of knobs. This builds graphs through the
app's OWN builder and asserts the wiring, the baked values, the refusals, and the absence of any
mis-settable argument. Nothing is submitted and no GPU is touched.

Optional: pass --live to also validate every built graph against forge's /object_info (read-only,
still no GPU). Expect the one known false positive: SaveVideo's `format.codec` dotted sub-key.

    python3 tools/v2v_matrix.py            # exits 1 on any failure
    python3 tools/v2v_matrix.py --live     # + validate node/input names against the rig
"""
import importlib.util
import inspect
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

FAILS = []
LIVE = "--live" in sys.argv


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def nodes(g, cls):
    return {k: v for k, v in g.items() if v["class_type"] == cls}


def expect_raise(fn, exc, label):
    try:
        fn()
    except exc:
        check(True, label)
        return
    except Exception as e:                     # wrong exception type = a different bug
        check(False, label + " (raised %r, not %s)" % (e, exc.__name__))
        return
    check(False, label + " (did NOT raise)")


def build(width=1280, height=704, control="src.mp4", first=None, seed=1234567, **over):
    kw = dict(prompt="a test prompt", width=width, height=height, seed=seed,
              control_name=control, first_name=first)
    kw.update(over)
    return fl.ltx_graph(**kw)


print("=" * 80)
print("LTX 2.3 IC-LoRA — baked rules, refusals and wiring (built by the app's builder)")
print("=" * 80)

# ---------------------------------------------------------------- 1 · baked, not settable
print("\n1 · the path is BAKED — there is no argument to get wrong")
sig = inspect.signature(fl.ltx_graph).parameters
for banned in ("steps", "cfg", "sampler", "scheduler", "shift", "lora", "turbo", "negative"):
    check(banned not in sig, "ltx_graph has NO %r parameter" % banned)
rules = fl.ltx_rules()
check(rules["steps"] == 8, "rules: 8 steps")
check(rules["cfg"] == 1.0, "rules: cfg 1.0 (the negative prompt is inert)")
check(rules["sampler"] == "euler_ancestral", "rules: euler_ancestral")
check(rules["scheduler"] == "linear_quadratic", "rules: linear_quadratic")
check(rules["fps"] == 25, "rules: 25 fps")
check(rules["divisor"] == 64, "rules: canvas divisor is 64 (NOT 32)")

g = build()
ks = nodes(g, "KSampler")
check(len(ks) == 1, "exactly one KSampler")
k = list(ks.values())[0]["inputs"]
check(k["steps"] == 8 and k["cfg"] == 1.0, "sampler node: 8 steps / cfg 1.0")
check(k["sampler_name"] == "euler_ancestral" and k["scheduler"] == "linear_quadratic",
      "sampler node: euler_ancestral / linear_quadratic")
check(k["seed"] == 1234567, "sampler node: seed passes through")
check(k["denoise"] == 1.0, "sampler node: denoise 1.0")

# ---------------------------------------------------------------- 2 · the guide slot wiring
print("\n2 · LTXVAddGuide's [positive, negative, latent] slots are the point")
add = nodes(g, "LTXVAddGuide")
check(len(add) == 1, "exactly one LTXVAddGuide")
gi = list(add.values())[0]["inputs"]
check(k["positive"] == [list(add)[0], 0], "KSampler.positive <- AddGuide slot 0")
check(k["negative"] == [list(add)[0], 1], "KSampler.negative <- AddGuide slot 1")
concat = list(nodes(g, "LTXVConcatAVLatent").values())[0]["inputs"]
check(concat["video_latent"] == [list(add)[0], 2], "ConcatAV.video_latent <- AddGuide slot 2")
# ⚠️ THIS check was MISSING until the break-proof caught it: with only positive/negative asserted,
# feeding the guide's slot 0 into latent_image passed the whole matrix green. It is the exact
# silent-drop/type-mismatch class (CONDITIONING into a LATENT) — forge rejects it at submit with
# return_type_mismatch, or worse, a plausible-looking wrong graph. Assert the latent link too.
cat_id = list(nodes(g, "LTXVConcatAVLatent"))[0]
check(k["latent_image"] == [cat_id, 0], "KSampler.latent_image <- ConcatAV slot 0")
crop = list(nodes(g, "LTXVCropGuides").values())[0]["inputs"]
check(crop["positive"] == [list(add)[0], 0] and crop["negative"] == [list(add)[0], 1],
      "CropGuides reads the AddGuide conditioning (0/1)")
vdec = list(nodes(g, "VAEDecodeTiled").values())[0]["inputs"]
crop_id = list(nodes(g, "LTXVCropGuides"))[0]
check(vdec["samples"] == [crop_id, 2], "VAEDecodeTiled.samples <- CropGuides slot 2")

# ---------------------------------------------------------------- 3 · depth branch + headline
print("\n3 · the depth control branch and the model set")
check(len(nodes(g, "LoadMoGeModel")) == 1, "MoGe model is loaded (the depth branch exists)")
check(len(nodes(g, "MoGeInference")) == 1, "MoGeInference present")
dep = list(nodes(g, "MoGeRender").values())
check(any(d["inputs"]["output"] == "depth" for d in dep), "MoGeRender output=depth feeds the guide")
check(gi["image"] == [list(nodes(g, "MoGeRender"))[[d["inputs"]["output"] for d in dep].index("depth")], 0],
      "AddGuide.image <- the DEPTH render (not the raw clip)")
ck = list(nodes(g, "CheckpointLoaderSimple").values())[0]["inputs"]["ckpt_name"]
check(ck == fl.LTX_CKPT, "checkpoint = %s" % fl.LTX_CKPT)
check(list(nodes(g, "LoraLoaderModelOnly").values())[0]["inputs"]["lora_name"] == fl.LTX_ICLORA,
      "IC-LoRA loaded with the union-control file")
check(len(nodes(g, "GetICLoRAParameters")) == 1, "GetICLoRAParameters wired into the guide")
check(gi["iclora_parameters"] == [list(nodes(g, "GetICLoRAParameters"))[0], 0],
      "guide.iclora_parameters <- GetICLoRAParameters")

print("\n4 · the deliberate simplification is really gone")
gone = ["TextGenerateLTX2Prompt", "PrimitiveBoolean"]
for cls in gone:
    check(len(nodes(g, cls)) == 0, "no %s node" % cls)
ablit = [v for v in nodes(g, "LoraLoader").values()
         if "abliterated" in str(v["inputs"].get("lora_name", ""))]
check(not ablit, "no gemma-abliterated LoRA node (prompt goes straight in)")

# ---------------------------------------------------------------- 5 · audio + save
print("\n5 · audio is generated on this path, and it SAVES (unlike S2V)")
mv = list(nodes(g, "CreateVideo").values())[0]["inputs"]
check(mv["fps"] == 25, "CreateVideo fps 25")
check(isinstance(mv.get("audio"), list), "CreateVideo.audio is WIRED (LTX generates a soundtrack)")
ad = list(nodes(g, "LTXVAudioVAEDecode").values())[0]["inputs"]
check(ad["samples"] == [list(nodes(g, "LTXVSeparateAVLatent"))[0], 1],
      "audio decode reads slot 1 of the separated AV latent")
check(len(nodes(g, "SaveVideo")) == 1, "SaveVideo present")

# ---------------------------------------------------------------- 6 · first frame is optional
print("\n6 · the first-frame still is OPTIONAL (a pure restyle has no still)")
g2 = build(first="frame0.png")
check(len(nodes(g2, "LoadImage")) == 1 and len(nodes(g2, "LTXVImgToVideoInplace")) == 1,
      "with a still: LoadImage + LTXVImgToVideoInplace appear")
gi2 = list(nodes(g2, "LTXVAddGuide").values())[0]["inputs"]
i2v = list(nodes(g2, "LTXVImgToVideoInplace"))[0]
check(gi2["latent"] == [i2v, 0], "guide.latent <- ImgToVideoInplace when a still is given")
check(gi["latent"] not in ([i2v, 0],), "without a still the guide reads the empty latent instead")
check(len(nodes(g, "LTXVImgToVideoInplace")) == 0, "no ImgToVideoInplace node in the no-still graph")

# ---------------------------------------------------------------- 7 · the canvas rule
print("\n7 · THE CANVAS RULE — divisible by 64, not 32")
for (w, h, ok) in [(1280, 704, True), (768, 512, True), (576, 320, True), (512, 512, True),
                   (640, 352, False), (768, 480, False), (960, 544, False), (640, 320, True)]:
    check(fl.ltx_canvas_ok(w, h) is ok, "ltx_canvas_ok(%d, %d) == %s" % (w, h, ok))
expect_raise(lambda: build(640, 352), ValueError, "640x352 (the exact case that failed on the rig)")
expect_raise(lambda: build(768, 480), ValueError, "768x480 refused")
check(len(build(576, 320)) > 0, "576x320 accepted")
check(len(build(768, 512)) > 0, "768x512 accepted")
check(len(build(1280, 704)) > 0, "1280x704 (native) accepted")

# ---------------------------------------------------------------- 8 · refusals
print("\n8 · refusals for what the API cannot express")
expect_raise(lambda: build(control=None), ValueError, "no control video -> refused")
expect_raise(lambda: build(control=""), ValueError, "empty control name -> refused")

# ---------------------------------------------------------------- 9 · the estimate
print("\n9 · the estimate reproduces the two MEASURED points")
e_draft = fl.ltx_estimate_seconds(576, 320, 121)
e_native = fl.ltx_estimate_seconds(1280, 704, 121)
check(abs(e_draft - 24) < 3, "576x320/121fr -> %.1f s (measured 24 s)" % e_draft)
check(abs(e_native - 102) < 4, "1280x704/121fr -> %.1f s (measured 102 s)" % e_native)

# ---------------------------------------------------------------- 10 · registry wiring
print("\n10 · the engine is reachable from the app's own tables")
labels = [lab for (_m, _v, lab) in fl.MODEL_CHOICES["Video"]]
check(any("LTX 2.3" in lab for lab in labels), "LTX appears in the Video MODEL list")
check(fl.kind_of("ltx") == "ltx", "kind_of('ltx') -> its own KIND slot")
check(fl.engine_of("ltx") == "ltx", "engine_of('ltx') -> 'ltx' (the estimate's load term keys on it)")
check(fl.size_key("ltx") == "ltx", "size_key('ltx') -> 'ltx' (own SIZE memory)")
check(fl.SIZE_DEFAULT_BY_KIND.get("ltx") == fl.LTX_SIZE_DEFAULT, "SIZE default registered")
check("LTX" in fl.model_help("ltx"), "model_help('ltx') returns the LTX text")
check(fl.MODES["ltx"].get("video") and fl.MODES["ltx"].get("ltx"), "MODES['ltx'] flags set")

# ---------------------------------------------------------------- 11 · graph shape sanity
print("\n11 · shape: frame count follows the CONTROL clip, not a typed length")
lat = list(nodes(g, "EmptyLTXVLatentVideo").values())[0]["inputs"]
# ⚠️ THIS ASSERTION USED TO GUARD THE BUG ITSELF. It read
#     check(lat["length"] == [dsz[-1], 2], "latent length <- GetImageSize(depth).batch_size")
# under the heading "frame count follows the CONTROL clip" — the label said the clip while the
# check asserted the DEPTH branch, so the suite passed while the engine rendered the wrong length
# (73 frames from a 121-frame source). An assertion that encodes the IMPLEMENTATION instead of the
# REQUIREMENT is worse than none: it makes the defect look verified. Now it checks the label.
check(lat["length"] == fl.ltx_length_for(None),
      "latent length <- the CONTROL clip, snapped to 8n+1 (%r)" % (lat["length"],))
check(lat["width"] == 1280 and lat["height"] == 704, "latent canvas is the requested one")
check(len(nodes(g, "Video Slice")) == 1, "the control clip is sliced before use")

# ---------------------------------------------------------------- 11b · ANIMATE 2
print("\n11b · WAN ANIMATE 2 — baked rules, slots and refusals")


def build_a2(width=480, height=848, ref="char.png", drive="drive.mp4", seed=7654321, **over):
    kw = dict(prompt="a test character brief", width=width, height=height, seed=seed,
              ref_name=ref, drive_name=drive)
    kw.update(over)
    return fl.animate2_graph(**kw)


sig2 = inspect.signature(fl.animate2_graph).parameters
for banned in ("steps", "cfg", "shift", "sampler", "scheduler", "lora", "turbo",
               "device", "dtype", "cache"):
    check(banned not in sig2, "animate2_graph has NO %r parameter" % banned)
r2 = fl.animate2_rules()
check(r2["steps"] == 6 and r2["cfg"] == 1.0, "rules: 6 steps / cfg 1.0")
check(r2["shift"] == 5.0, "rules: shift 5.0")
check(r2["sampler"] == "lcm" and r2["scheduler"] == "simple", "rules: lcm / simple")
check(r2["fps"] == 24.0, "rules: 24 fps (normalised, NOT the source's rate)")
check(r2["audio"] is False, "rules: audio False (WanAnimate2ToVideo is picture only)")

a = build_a2()
samp = list(nodes(a, "SamplerCustom").values())[0]["inputs"]
w2 = list(nodes(a, "WanAnimate2ToVideo"))[0]
# ⚠️ THE slot check that matters: [positive, negative, latent] -> 0 / 1 / 2
check(samp["positive"] == [w2, 0], "SamplerCustom.positive <- WanAnimate2ToVideo slot 0")
check(samp["negative"] == [w2, 1], "SamplerCustom.negative <- WanAnimate2ToVideo slot 1")
check(samp["latent_image"] == [w2, 2], "SamplerCustom.latent_image <- WanAnimate2ToVideo slot 2")
trim = list(nodes(a, "TrimVideoLatent").values())[0]["inputs"]
check(trim["samples"] == [list(nodes(a, "SamplerCustom"))[0], 0],
      "TrimVideoLatent.samples <- SamplerCustom slot 0")
check(trim["trim_amount"] == [w2, 3], "TrimVideoLatent.trim_amount <- WanAnimate2ToVideo slot 3")
vdec2 = list(nodes(a, "VAEDecode").values())[0]["inputs"]
check(vdec2["samples"] == [list(nodes(a, "TrimVideoLatent"))[0], 0],
      "VAEDecode.samples <- TrimVideoLatent slot 0")
check(samp["cfg"] == 1.0 and samp["add_noise"] is True, "SamplerCustom: cfg 1.0, add_noise on")
check(samp["noise_seed"] == 7654321, "SamplerCustom: seed passes through")
sch = list(nodes(a, "BasicScheduler").values())[0]["inputs"]
check(sch["steps"] == 6 and sch["scheduler"] == "simple", "BasicScheduler: 6 steps / simple")
sh = list(nodes(a, "ModelSamplingSD3").values())[0]["inputs"]
check(sh["shift"] == 5.0, "ModelSamplingSD3 shift 5.0")
cache = list(nodes(a, "WanAnimate2Cache").values())[0]["inputs"]
check(cache["device"] == "gpu" and cache["dtype"] == "int8", "face cache pinned gpu/int8")
check(cache["model"] == [list(nodes(a, "LoraLoaderModelOnly"))[0], 0],
      "cache reads the LoRA'd model")
check(list(nodes(a, "LoraLoaderModelOnly").values())[0]["inputs"]["lora_name"] == fl.ANIMATE2_LORA,
      "the I2V distill LoRA is the one loaded")
check(sh["model"] == [list(nodes(a, "WanAnimate2Cache"))[0], 0], "shift reads the cache")

mv2 = list(nodes(a, "CreateVideo").values())[0]["inputs"]
check(mv2["fps"] == 24.0, "CreateVideo fps is NORMALISED to 24.0 (not the source's 60)")
check("audio" not in mv2, "CreateVideo has NO audio link (picture-only path; see S2V's AAC bug)")
_w2i = list(nodes(a, "WanAnimate2ToVideo").values())[0]["inputs"]
check(_w2i["length"] == 81, "default length is one 81-frame pass")
# ⚠️ this pair is MEASURED, not taste: 0.0 is the shipped template's value and it suppresses the
# motion transfer (corr 0.127 / motion 6.2 vs source 16.3); 1.0 tracks it (0.634 / 17.6 vs 16.3).
# The assertion exists so a refactor cannot quietly revert us to the template's behaviour.
check(_w2i["pose_end_percent"] == 1.0, "pose_end_percent 1.0 (template's 0.0 suppresses motion)")
check(_w2i["pose_start_percent"] == 0.0, "pose_start_percent 0.0")
check(len(nodes(a, "StartLoop")) == 0 and len(nodes(a, "EndLoop")) == 0,
      "SINGLE PASS: no loop machinery in this slice")

print("\n11f · the DURATION field still matters (regression guard)")

# `length` is a PER-ENGINE override: every consumer reads `p.get("length") or <seconds-derived
# frames>`, so setting it for a mode that does not own it pins that mode's DURATION field out of
# the picture. A blanket `length` in params() did exactly that — H3 and WAN were stuck at 81
# frames (a 6 s shot estimated 247 s instead of 628 s) while the field still LOOKED live.
_h3_3 = fl.estimate_seconds("video", {"width": 1344, "height": 768, "seconds": 3.0, "steps": 12},
                            None, None)
_h3_6 = fl.estimate_seconds("video", {"width": 1344, "height": 768, "seconds": 6.0, "steps": 12},
                            None, None)
check(_h3_6 > _h3_3 * 1.5,
      "H3 estimate follows its DURATION field (3 s -> %.0f s, 6 s -> %.0f s)" % (_h3_3, _h3_6))
_wq = list(fl.WAN_QUALITY)[0]
_w5 = fl.estimate_seconds("wan", {"width": 832, "height": 480, "seconds": 5.0, "steps": 4,
                                  "wan_mode": "t2v", "quality": _wq}, None, None)
_w10 = fl.estimate_seconds("wan", {"width": 832, "height": 480, "seconds": 10.0, "steps": 4,
                                   "wan_mode": "t2v", "quality": _wq}, None, None)
check(_w10 > _w5, "WAN estimate follows its DURATION field (5 s -> %.0f s, 10 s -> %.0f s)"
      % (_w5, _w10))

# params() is a closure inside launch(), so this guard has to read the source. That is the only
# GPU-free way to reach it — and it names the trap for whoever reads it next.
_srcp = open(os.path.join(HERE, "forge_lab.py"), encoding="utf-8").read()
_pi = _srcp.find("    def params():")
_pbody = _srcp[_pi:_srcp.find("\n    def ", _pi + 10)]
check('"length": int(st.get("length") or ANIMATE2_LENGTH),' not in _pbody,
      "params() sets NO blanket `length` key (that pinned H3/WAN to 81 frames)")
check('p["length"] = ANIMATE2_LENGTH' in _pbody and 'p["length"] = ltx_length_for' in _pbody,
      "params() sets `length` only for the engines that OWN it (animate2 / ltx)")

print("\n11e · LTX LENGTH — the official 8n+1 rule AND where the number comes from")

# The rule, from three agreeing official sources: Lightricks' LTX-2 model card ("frame count must
# be divisible by 8 + 1"), docs.ltx.io ("Length: 97 frames | 1 + a multiple of 8"), and the
# template's own EmptyLTXVLatentVideo default of 97.
for _n, _want in ((121, 121), (125, 121), (100, 97), (65, 65), (20, 17), (9, 9)):
    _got = fl.ltx_length_for(_n)
    check(_got == _want and (_got - 1) % 8 == 0,
          "source %3d frames -> length %3d (expect %3d, and 8n+1)" % (_n, _got, _want))
check(fl.ltx_length_for(0) == fl.LTX_FRAME_REF and fl.ltx_length_for(None) == fl.LTX_FRAME_REF,
      "no probe result falls back to the measured default %d" % fl.LTX_FRAME_REF)

_g = build()
_lat = list(nodes(_g, "EmptyLTXVLatentVideo").values())[0]["inputs"]["length"]
_ala = list(nodes(_g, "LTXVEmptyLatentAudio").values())[0]["inputs"]["frames_number"]
check(isinstance(_lat, int) and (_lat - 1) % 8 == 0,
      "video latent length is a literal 8n+1 int (%r), not a link" % (_lat,))
check(_ala == _lat, "audio frames_number MATCHES the video latent (the AV concat needs it)")
# ⚠️ THE ASSERTION THAT WOULD HAVE CAUGHT THE BUG: pointed at PROVENANCE, not legality. 73
# satisfied 8n+1 and was still wrong — it came from the depth branch, not from the clip.
check("dsize" not in _g, "PROVENANCE: nothing reads the DEPTH image's size for the length")
check(abs(_g["slice"]["inputs"]["duration"] - _lat / fl.LTX_FPS) < 1e-9,
      "the slice duration matches the snapped length (%.2f s = %d frames @%d fps)"
      % (_g["slice"]["inputs"]["duration"], _lat, fl.LTX_FPS))
_e = fl.estimate_seconds("ltx", {"width": 1280, "height": 704, "src_frames": 121}, None, None)
check(abs(_e - fl.ltx_estimate_seconds(1280, 704, fl.ltx_length_for(121))) < 0.1,
      "estimate_seconds uses the SAME length the graph builds (%.1f s)" % _e)
expect_raise(lambda: fl.ltx_graph(prompt="x", width=1280, height=704, seed=1,
                                  control_name="c.mp4", frames=5),
             ValueError, "a source too short for the 8n+1 minimum is refused")

print("\n11d · INPUT PATH plumbing — estimate routing, refusals, video_probe")

# ⚠️ The route is the risk: ltx and animate2 both carry video=True, so a missing branch sends them
# into H3's curve and quotes a confident wrong number. Assert the routing AND the distinctness.
e_ltx = fl.estimate_seconds("ltx", {"width": 1280, "height": 704, "src_frames": 121}, None, None)
e_a2 = fl.estimate_seconds("animate2", {"width": 480, "height": 848, "length": 81}, None, None)
e_h3 = fl.estimate_seconds("video", {"width": 1280, "height": 704, "length": 121, "steps": 12},
                           None, None)
check(abs(e_ltx - fl.ltx_estimate_seconds(1280, 704, 121)) < 0.1,
      "estimate_seconds('ltx') uses the LTX fit (%.1f s)" % e_ltx)
check(abs(e_a2 - fl.animate2_estimate_seconds(480, 848, 81)) < 0.1,
      "estimate_seconds('animate2') uses the Animate 2 fit (%.1f s)" % e_a2)
check(abs(e_ltx - e_h3) > 1 and abs(e_a2 - e_h3) > 1,
      "neither falls through into H3's curve (H3 would say %.1f s)" % e_h3)
check(fl.engine_load_s("ltx") == 0.0 and fl.engine_load_s("animate2") == 0.0,
      "engine_load_s() KNOWS both new engines (0.0 = unmeasured switch, not a missing key)")
check(fl.engine_load_s("h3") > 0, "and still returns a real term for h3")

# refusals must fire BEFORE any network call, or a bad click becomes a failed POST
expect_raise(lambda: fl.submit("ltx", {}, []), ValueError, "submit ltx with no source -> refused")
expect_raise(lambda: fl.submit("animate2", {"source_path": "/tmp/x.mp4"}, []), ValueError,
             "submit animate2 with no still -> refused")
expect_raise(lambda: fl.submit("animate2", {}, []), ValueError,
             "submit animate2 with no driving clip -> refused")

# video_probe must DEGRADE, not raise: a missing ffprobe/file must not break the panel
check(fl.video_probe(None) is None, "video_probe(None) -> None")
check(fl.video_probe("/definitely/not/here.mp4") is None, "video_probe(missing file) -> None")
check(fl.video_probe(os.path.join(HERE, "forge_lab.py")) is None,
      "video_probe(non-video file) -> None (no crash on a stray pick)")
_vp = fl.video_probe(os.environ.get("V2V_PROBE_FILE", ""))
check(_vp is None or isinstance(_vp, dict),
      "video_probe(real clip) -> dict, or None when V2V_PROBE_FILE is unset")
if _vp:
    check(_vp["frames"] > 0 and _vp["fps"] > 0,
          "probe read real numbers: %dx%d %g fps %d frames" % (_vp["width"], _vp["height"],
                                                               _vp["fps"], _vp["frames"]))

print("\n11c · Animate 2 refusals + the estimate fitted to MEASURED runs")
expect_raise(lambda: build_a2(ref=None), ValueError, "no reference still -> refused")
expect_raise(lambda: build_a2(drive=None), ValueError, "no driving video -> refused")
expect_raise(lambda: build_a2(481, 848), ValueError, "canvas not a multiple of 16 -> refused")
check(len(build_a2(480, 848)) > 0, "480x848 accepted")
check(fl.ANIMATE2_ESTIMATE_MEASURED is True, "the estimate is fitted to MEASURED runs")
e81 = fl.animate2_estimate_seconds(480, 848, 81)
e121 = fl.animate2_estimate_seconds(480, 848, 121)
check(abs(e81 - 181) < 12, "81 frames -> %.0f s (MEASURED 181 s)" % e81)
check(abs(e121 - 252) < 15, "121 frames -> %.0f s (MEASURED 252 s)" % e121)
check("ANIMATE 2" in fl.MODEL_CHOICES["Video"][-1][2], "Animate 2 is in the Video MODEL list")
check(fl.kind_of("animate2") == "animate2" and fl.engine_of("animate2") == "animate2",
      "its own KIND + engine slots")
check(fl.size_key("animate2") == "animate2", "its own SIZE memory")
check(fl.SIZE_DEFAULT_BY_KIND.get("animate2") == fl.ANIMATE2_SIZE_DEFAULT, "SIZE default registered")
check("Wan Animate 2" in fl.model_help("animate2"), "model_help('animate2') returns its text")


if LIVE:
    print("\n12 · LIVE: every class_type and input name against forge /object_info")
    try:
        fl.resolve_forge()
        oi = fl.get_json("/object_info")
        spec_problems = []
        checked_nodes = 0
        graphs_checked = [("ltx-1280x704", build()), ("ltx-576x320", build(576, 320)),
                          ("ltx-with-still", build(first="f.png")),
                          ("animate2-480x848", build_a2())]
        for nm, gg in graphs_checked:
            for nid, n in gg.items():
                checked_nodes += 1
                ct = n["class_type"]
                if ct not in oi:
                    spec_problems.append("%s: unknown class_type %s" % (nm, ct))
                    continue
                spec = oi[ct]["input"]
                allowed = set(spec.get("required", {}) or {}) | set(spec.get("optional", {}) or {})
                for name in n["inputs"]:
                    if name in allowed:
                        continue
                    if any(name.startswith(a + ".") for a in allowed):   # dotted dynamic sub-key
                        continue
                    spec_problems.append("%s: %s has unknown input %r" % (nm, ct, name))
                # ⚠️ THE MISSING DIRECTION. This check did not exist, and its absence cost a
                # server round-trip: LTXAVTextEncoderLoader requires `device` (COMBO, no default)
                # and ltx_graph() omitted it, so every LTX submit died with HTTP 400
                # "required_input_missing: device" while this validator reported the graph clean.
                # A validator that only looks for what is WRONG cannot see what is MISSING.
                for name in (spec.get("required", {}) or {}):
                    if name in n["inputs"]:
                        continue
                    if any(k.startswith(name + ".") for k in n["inputs"]):  # dotted parent
                        continue
                    spec_problems.append("%s: %s MISSING required input %r" % (nm, ct, name))
        if spec_problems:
            for p in spec_problems[:12]:
                print("      !", p)
        check(not spec_problems, "no unknown/missing class_type or input, both directions (%d nodes across %d graphs)"
              % (checked_nodes, len(graphs_checked)))
    except Exception as e:
        check(False, "live validation could not run: %r" % (e,))
else:
    print("\n12 · live validation skipped (pass --live to check against forge)")

# ------------------------------------------------- 13 · a TRANSPARENT PNG must not hang Tk 9
#
# ⚠️ THIS WAS A REAL REGRESSION THAT BRICKED THE APP, and it is invisible to every other
# check here because nothing raises: tk.PhotoImage on a transparent PNG simply never returns.
# Tk 9 builds a CoreGraphics region from the PNG's alpha mask (TkpBuildRegionFromAlphaData)
# and that union is superlinear in area. MEASURED on this machine, with a control:
#
#     1696x2464 RGB (alpha flattened away) ......   0.05 s
#     1696x2464 RGBA (a real `bg` take) .........  > 45 s — never finished
#     1200x1700 RGBA (the fixture below) ........  > 25 s
#
# The load runs inside a Tk `after` callback, so the hang is INSIDE mainloop: no window ever
# paints, nothing is logged, and reopening the app hangs again on the same take. ONE
# `remove background` take in the takes list was enough to brick it on startup.
#
# ⚠️ BOTH probes run in SUBPROCESSES with a timeout on purpose. In-process, a regression would
# HANG this suite forever instead of failing it — and a test that hangs is not a test.
print("\n13 · thumbnails: a transparent PNG must not hang Tk 9")

import struct as _struct
import subprocess as _sub
import tempfile as _tempfile
import zlib as _zlib

_fx = os.path.join(_tempfile.gettempdir(), "forge_lab_thumb_fixture")
os.makedirs(_fx, exist_ok=True)


def _mkpng(path, w, h, rgba):
    """A transparent-background PNG with MANY disjoint alpha regions — the shape that kills Tk."""
    def _chunk(t, d):
        return (_struct.pack(">I", len(d)) + t + d
                + _struct.pack(">I", _zlib.crc32(t + d) % 4294967296))
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            inside = ((x * 7 + y * 3) % 53 < 26) or ((x * x + y) % 997 < 120)
            if rgba:
                raw += bytes((x % 256, y % 256, (x + y) % 256, 255 if inside else 0))
            else:
                raw += bytes((x % 256, y % 256, (x + y) % 256))
    ihdr = _struct.pack(">IIBBBBB", w, h, 8, 6 if rgba else 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", _zlib.compress(bytes(raw), 6)) + _chunk(b"IEND", b""))


_rgba = os.path.join(_fx, "transparent.png")
_rgb = os.path.join(_fx, "opaque.png")
if not (os.path.exists(_rgba) and os.path.exists(_rgb)):
    _mkpng(_rgba, 1200, 1700, True)
    _mkpng(_rgb, 1200, 1700, False)

check(fl._png_has_alpha(_rgba) is True, "detects the alpha channel in an RGBA PNG")
check(fl._png_has_alpha(_rgb) is False, "an RGB PNG reports NO alpha (so it takes the fast path)")
check(fl._png_has_alpha(__file__) is False, "a non-PNG path reports no alpha")

_probe = os.path.join(_fx, "probe.py")
with open(_probe, "w") as f:
    f.write(
        "import importlib.util, os, sys, tkinter as tk\n"
        "mode, img_path, here = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "root = tk.Tk(); root.withdraw()\n"
        "if mode == 'raw':\n"
        "    i = tk.PhotoImage(file=img_path)\n"
        "else:\n"
        "    spec = importlib.util.spec_from_file_location(\n"
        "        'forge_lab', os.path.join(here, 'forge_lab.py'))\n"
        "    fl = importlib.util.module_from_spec(spec); spec.loader.exec_module(fl)\n"
        "    i = fl.thumb_photo(img_path, 190)\n"
        "print('LOADED %dx%d' % (i.width(), i.height()) if i else 'LOADED None')\n")


def _run_probe(mode, img_path, limit):
    """Returns (dims, elapsed, raw_line). dims is None for a refusal, a crash or a TIMEOUT."""
    t0 = time.time()
    try:
        p = _sub.run([sys.executable, _probe, mode, img_path, HERE],
                     capture_output=True, text=True, timeout=limit)
        out = (p.stdout or "").strip().splitlines() or [""]
        out = out[-1] or ("exit %d" % p.returncode)
    except _sub.TimeoutExpired:
        out = "TIMEOUT after %ds" % limit
    dt = time.time() - t0
    dims = None
    if out.startswith("LOADED ") and out != "LOADED None":
        try:
            w, h = out.split()[1].split("x")
            dims = (int(w), int(h))
        except Exception:
            dims = None
    return dims, dt, out


# 1. the raw call the app must never make again — pathological, proven, in a subprocess
_d, _dt, _o = _run_probe("raw", _rgba, 12)
check(_d is None, "the RAW tk.PhotoImage on that transparent PNG never returns (%s) — "
                  "this is the bug, and the guard is load-bearing" % _o)
_d, _dt, _o = _run_probe("raw", _rgb, 12)
check(_d is not None and _dt < 5,
      "…while the SAME pixels WITHOUT alpha load in %.2fs — it is the ALPHA MASK, not the size"
      % _dt)

# 2. the app's own path: a thumbnail-sized image, FAST (the 15s probe timeout IS the timing
#    assertion — a regression makes this TIMEOUT, so the suite FAILS instead of hanging)
_d, _dt, _o = _run_probe("app", _rgba, 15)
check(_d is not None and 100 <= max(_d) <= 190,
      "thumb_photo returns a thumbnail-sized image for the transparent PNG — %s in %.2fs"
      % (_o, _dt))

# 3. the common (opaque) path is untouched
_d, _dt, _o = _run_probe("app", _rgb, 15)
check(_d is not None and max(_d) <= 190,
      "thumb_photo still handles an OPAQUE png — %s in %.2fs" % (_o, _dt))

_src = open(os.path.join(HERE, "forge_lab.py")).read()
# ⚠️ Checked OUTSIDE thumb_photo, not by one literal string. The old check only looked for
# "tk.PhotoImage(file=path)" and so missed a real second call site written as
# tk.PhotoImage(file=os.path.join(d, n)) - a NEW window doing exactly what the guard forbids.
# thumb_photo is the one place allowed to touch Tk images; anywhere else is a bug.
_i = _src.index("def thumb_photo")
_j = _src.index("\ndef ", _i + 10)
_outside = _src[:_i] + _src[_j:]
# strip full-line comments first: the file DOCUMENTS this rule in three places, and a bare
# substring count flags those warnings as violations.
_code = "\n".join(l for l in _outside.splitlines() if not l.strip().startswith("#"))
_hits = _code.count("tk.PhotoImage(file=")
check(_hits == 0,
      "no UI call site OUTSIDE thumb_photo hands a file to Tk (%d found)" % _hits)
check("thumb = thumb_photo(path, 190)" in _src,
      "…and the takes list is the one that goes through it (that was the app-bricking line)")

print("\n" + "=" * 80)
print("FAILURES: %d" % len(FAILS))
for f in FAILS:
    print("   -", f)
print("=" * 80)
sys.exit(1 if FAILS else 0)
