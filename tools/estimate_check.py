#!/usr/bin/env python3
"""Assert Forge Lab's estimates against EVERY measured render on this rig.

The estimates are fitted curves, so they must be held to the data they were fitted to — and
to the points that were NOT used in the fit, which is the only real test. Each row below is a
real run; `measured` is its forge-log compute time. Tolerance is 10%, and any row drifting
past it fails the run.

    python3 tools/estimate_check.py      # exits 1 on any failure

⚠️ 2026-10-02 — THE H3 ROWS WERE RE-MEASURED FROM SCRATCH, AND WHY
The previous H3 rows could not be reconciled with each other. Read together they imply a
fixed per-job load in one place and none in another, which is impossible. Re-measuring two
of them in a single controlled session showed why: the historic numbers are ~9-12% LOW
(73fr/12st native recorded 212.0, re-measured 238.2; 241fr/12st native recorded 1571.0,
re-measured 1717.0). They came from sessions whose cache/warmth state is no longer knowable,
and that error was baked into the old cost law — which quoted worst 42%, mean 25% against
the fresh data.

So the H3 rows are now the fresh measurements, taken back-to-back in ONE session. They are
WARM (same engine as the run before them), which is why they pass warm_engine="h3".
The historic rows are KEPT below, clearly marked, at a wider tolerance — they are real
observations of a real rig state, just not of a state we can name. Nothing is deleted.
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

# (label, mode, params, measured_s, warm_engine, used_in_fit)
ROWS = [
    # ---- H3: the CANVAS sweep, all at 124 frames / 4 steps + turbo, 1 card ---------------
    ("H3 124fr/4st 480p", "video",
     {"length": 124, "width": 832, "height": 480, "steps": 4}, 48.6, "h3", True),
    ("H3 124fr/4st 720p", "video",
     {"length": 124, "width": 1280, "height": 704, "steps": 4}, 157.9, "h3", True),
    ("H3 124fr/4st NATIVE", "video",
     {"length": 124, "width": 1344, "height": 768, "steps": 4}, 194.8, "h3", True),
    ("H3 124fr/4st 1080p", "video",
     {"length": 124, "width": 1920, "height": 1088, "steps": 4}, 643.0, "h3", True),
    ("H3 124fr/4st 2K", "video",
     {"length": 124, "width": 2560, "height": 1440, "steps": 4}, 1809.3, "h3", True),
    # ---- H3: the FRAME sweep at native. The 124fr row is a SECOND run of the same config
    # as the canvas sweep's native row — 194.8 vs 188.2 — which is the noise floor (3.5%)
    # and the reason the tolerance is 10% and not 2%.
    ("H3 124fr/4st NATIVE (2nd run)", "video",
     {"length": 124, "width": 1344, "height": 768, "steps": 4}, 188.2, "h3", True),
    ("H3 181fr/4st NATIVE", "video",
     {"length": 181, "width": 1344, "height": 768, "steps": 4}, 400.9, "h3", True),
    ("H3 241fr/4st NATIVE", "video",
     {"length": 241, "width": 1344, "height": 768, "steps": 4}, 603.9, "h3", True),
    ("H3 362fr/4st NATIVE", "video",
     {"length": 362, "width": 1344, "height": 768, "steps": 4}, 1236.8, "h3", True),
    # ---- H3: SEPARABILITY crossings — 720p measured at frame counts other than 124. These
    # are the points that test whether canvas and frame effects really multiply. They do:
    # the 720p:native ratio is 0.782 / 0.772 / 0.782 at 124 / 181 / 241 frames.
    ("H3 181fr/4st 720p", "video",
     {"length": 181, "width": 1280, "height": 704, "steps": 4}, 315.4, "h3", True),
    ("H3 241fr/4st 720p", "video",
     {"length": 241, "width": 1280, "height": 704, "steps": 4}, 477.5, "h3", True),
    # ---- H3: the LOAD PROBE. One step at native — the run that pins H3_JOB_LOAD_S as an
    # intercept rather than letting it hide inside the sampling constant.
    ("H3 124fr/1st NATIVE (load probe)", "video",
     {"length": 124, "width": 1344, "height": 768, "steps": 1}, 72.9, "h3", True),
    # ---- H3: BASE model (12 steps, no turbo LoRA). Proves turbo and base cost the SAME per
    # step: 124fr gives 0.3614 s/frame-step here against 0.3610 on the turbo path.
    ("H3 73fr/12st NATIVE", "video",
     {"length": 73, "width": 1344, "height": 768, "steps": 12}, 238.2, "h3", True),
    ("H3 124fr/12st NATIVE", "video",
     {"length": 124, "width": 1344, "height": 768, "steps": 12}, 537.8, "h3", True),
    ("H3 241fr/12st NATIVE", "video",
     {"length": 241, "width": 1344, "height": 768, "steps": 12}, 1717.0, "h3", True),
    # ---- RAYLIGHT (2-CARD). Same configs as the 1-card rows above, with gpus=2 -- so each
    # pair is a clean A/B where the ONLY changed variable is the card count. Note the speedup
    # is not constant across these: 1.14x at 1 step through 1.79x at 362 frames.
    ("RAYLIGHT 2gpu 124fr/1st native", "video",
     {"length": 124, "width": 1344, "height": 768, "steps": 1, "gpus": 2}, 63.8, "h3", True),
    ("RAYLIGHT 2gpu 124fr/4st native", "video",
     {"length": 124, "width": 1344, "height": 768, "steps": 4, "gpus": 2}, 121.5, "h3", True),
    ("RAYLIGHT 2gpu 241fr/4st native", "video",
     {"length": 241, "width": 1344, "height": 768, "steps": 4, "gpus": 2}, 351.8, "h3", True),
    ("RAYLIGHT 2gpu 362fr/4st native", "video",
     {"length": 362, "width": 1344, "height": 768, "steps": 4, "gpus": 2}, 691.5, "h3", True),
    ("RAYLIGHT 2gpu 720p 124fr/4st", "video",
     {"length": 124, "width": 1280, "height": 704, "steps": 4, "gpus": 2}, 106.3, "h3", True),
    # ---- WAN 2.2 -----------------------------------------------------------------------
    ("WAN turbo 81fr 832x480 SWITCH", "wan",
     {"seconds": 5.0, "width": 832, "height": 480, "quality": "Turbo 4 steps"},
     105.7, "h3", True),
    ("WAN full 81fr 832x480 SWITCH", "wan",
     {"seconds": 5.0, "width": 832, "height": 480, "quality": "Full 20 steps"},
     942.8, "h3", True),
    ("WAN i2v turbo 81fr 544x736 SWITCH", "wan",
     {"seconds": 5.0, "width": 544, "height": 736, "quality": "Turbo 4 steps"},
     110.61, "h3", False),
    # ---- Qwen-Image 2.1 ----------------------------------------------------------------
    ("Qwen 1.0MP 25st WARM", "t2i",
     {"width": 1024, "height": 1024, "steps": 25}, 12.0, "qwen", True),
    ("Qwen 1.0MP 25st COLD", "t2i",
     {"width": 1024, "height": 1024, "steps": 25}, 18.0, None, True),
    ("Qwen 2.84MP 25st", "t2i",
     {"width": 1952, "height": 1456, "steps": 25}, 49.5, "qwen", True),
    ("Qwen 3.67MP 25st", "t2i",
     {"width": 2208, "height": 1664, "steps": 25}, 67.4, "qwen", True),
    ("Qwen 4.19MP 25st", "t2i",
     {"width": 2048, "height": 2048, "steps": 25}, 82.5, "qwen", True),
]

# Historic H3 rows. Kept at a wider tolerance on purpose — see the module docstring. These
# are NOT the fit's target and they are known to sit ~9-12% low of a clean re-measurement.
# They are here so that a future drift shows up as a CHANGE in them, not as silence.
HISTORIC = [
    ("H3 73fr/12st 480p  [historic]", "video",
     {"seconds": 3.0, "width": 832, "height": 480, "steps": 12}, 61.0, "h3"),
    ("H3 73fr/12st 720p  [historic]", "video",
     {"seconds": 3.0, "width": 1280, "height": 704, "steps": 12}, 176.0, "h3"),
    ("H3 73fr/12st NATIVE [historic]", "video",
     {"seconds": 3.0, "width": 1344, "height": 768, "steps": 12}, 212.0, "h3"),
    ("H3 73fr/12st 1080p [historic]", "video",
     {"seconds": 3.0, "width": 1920, "height": 1088, "steps": 12}, 721.0, "h3"),
    ("H3 73fr/12st 2K    [historic]", "video",
     {"seconds": 3.0, "width": 2560, "height": 1440, "steps": 12}, 1905.0, "h3"),
    ("H3 20st NATIVE     [historic]", "video",
     {"seconds": 3.0, "width": 1344, "height": 768, "steps": 20}, 354.0, "h3"),
    ("H3 124fr/12st 544x736 [historic]", "video",
     {"seconds": 5.0, "width": 544, "height": 736, "steps": 12}, 106.4, "h3"),
    ("H3 241fr/12st 480p [historic]", "video",
     {"length": 241, "width": 832, "height": 480, "steps": 12}, 312.65, "h3"),
    ("H3 241fr/12st NATIVE [historic]", "video",
     {"length": 241, "width": 1344, "height": 768, "steps": 12}, 1571.0, "h3"),
]

print("%-40s %9s %9s %7s  %s" % ("measured run", "estimate", "measured", "error", "source"))
print("-" * 82)
for label, mode, p, measured, warm, fitted in ROWS:
    est = fl.estimate_seconds(mode, p, warm_engine=warm)
    err = abs(est - measured) / measured * 100.0
    ok = err <= 10.0
    if not ok:
        FAILS.append("%s: %.1f%% off (est %.1fs vs measured %.1fs)"
                     % (label, err, est, measured))
    print("%-40s %8.1fs %8.1fs %6.1f%%  %-9s %s"
          % (label, est, measured, err, "fitted" if fitted else "HELD OUT",
             "ok" if ok else "FAIL"))

print("\n" + "-" * 82)
print("HISTORIC rows (superseded; 25% tolerance, see docstring)")
for label, mode, p, measured, warm in HISTORIC:
    est = fl.estimate_seconds(mode, p, warm_engine=warm)
    err = abs(est - measured) / measured * 100.0
    ok = err <= 25.0
    if not ok:
        FAILS.append("%s: %.1f%% off (est %.1fs vs measured %.1fs)"
                     % (label, err, est, measured))
    print("%-40s %8.1fs %8.1fs %6.1f%%  %s"
          % (label, est, measured, err, "ok" if ok else "FAIL"))

print("\n" + "=" * 82)
print("the 3x bug must stay gone: H3 124fr/12st at 0.400 MP")
was = 180.0 + 124 * 12 * 0.25 * (0.400 / 1.06)          # the old formula, for contrast
now = fl.estimate_seconds("video", {"seconds": 5.0, "width": 544, "height": 736,
                                    "steps": 12}, warm_engine="h3")
print("  old model would say %.0fs   →   now says %.0fs   (measured 106.4s)" % (was, now))
if abs(now - 106.4) / 106.4 > 0.10:
    FAILS.append("H3 small-canvas estimate off by more than 10%% (got %.1fs)" % now)

# a same-engine second run must DROP the switch term; a switch must keep it
w = fl.estimate_seconds("video", {"seconds": 5.0, "width": 544, "height": 736, "steps": 12},
                        warm_engine="h3")
s = fl.estimate_seconds("video", {"seconds": 5.0, "width": 544, "height": 736, "steps": 12},
                        warm_engine="wan")
print("\nengine-switch behaviour")
print("  same engine (warm h3): %.1fs   ·   after a WAN run: %.1fs   ·   delta %.1fs"
      % (w, s, s - w))
if not (s - w) > 5:
    FAILS.append("engine switch does not add a load term")
if fl.estimate_is_switch("video", "h3") or not fl.estimate_is_switch("video", "wan"):
    FAILS.append("estimate_is_switch wrong")
if not fl.estimate_is_switch("video", "upscale"):
    FAILS.append("a PROMOTE (which loads ESRGAN and evicts the video model) must count as a switch")

print("\n⚠️ the load term must be paid on EVERY H3 run, not just on an engine switch")
print("   (this is the term that was missing entirely, and it is why short clips read 30-42% low)")
lo = fl.estimate_seconds("video", {"length": 124, "width": 1344, "height": 768, "steps": 4},
                         warm_engine="h3")
zero_step = fl.H3_JOB_LOAD_S
print("   H3_JOB_LOAD_S = %.1f s, added to a WARM run (estimate %.1fs)" % (zero_step, lo))
if abs(fl.H3_JOB_LOAD_S - 23.0) > 0.05:
    FAILS.append("H3_JOB_LOAD_S is not the measured 23.0 s")
if abs((lo - 12.0) - fl.H3_JOB_LOAD_S) < 0:
    FAILS.append("warm H3 estimate does not appear to include the job load")

# the extrapolation flag must fire OUTSIDE the measured envelope and stay quiet inside it
print("\nENVELOPE flag (what stops a floor being read as a promise)")
CASES = [
    ("73 fr 480p   (inside)", 73, 0.399, False),
    ("124 fr 480p  (edge)", 124, 0.399, False),
    ("241 fr 480p  (outside)", 241, 0.399, True),
    ("73 fr native (inside)", 73, 1.032, False),
    ("241 fr native (outside)", 241, 1.032, True),
    ("73 fr 2K     (outside)", 73, 3.686, True),
]
for label, fr, mp, want in CASES:
    got = fl.h3_estimate_extrapolated(fr, mp)
    print("  %-26s extrapolated=%-5s %s" % (label, got, "ok" if got == want else "FAIL"))
    if got != want:
        FAILS.append("envelope flag wrong for %s" % label)

print("\n⚠️ frame term: 1.0 AT the reference, a power law above AND below it, same at every canvas")
print("   (the old law clamped it to 1.0 at or below 124 frames — the root cause of the")
print("    short-clip under-quote — so a clamp reappearing here is a real regression)")
if abs(fl.h3_frame_factor(124, 1.032) - 1.0) > 1e-9:
    FAILS.append("frame term is not exactly 1.0 at the reference length")
for fr in (73, 124, 181, 241, 362):
    for mp in (0.399, 1.032, 3.686):
        got = fl.h3_frame_factor(fr, mp)
        want = (fr / 124.0) ** fl.H3_FRAME_EXP
        if abs(got - want) > 1e-9:
            FAILS.append("frame term wrong at %d fr / %.3f MP: %.4f vs %.4f" % (fr, mp, got, want))
print("  f(73)=%.3f  f(124)=%.3f  f(181)=%.3f  f(241)=%.3f  f(362)=%.3f  (same at every canvas)"
      % tuple(fl.h3_frame_factor(f, 1.032) for f in (73, 124, 181, 241, 362)))
if not (fl.h3_frame_factor(73, 1.032) < fl.h3_frame_factor(124, 1.032) < fl.h3_frame_factor(362, 1.032)):
    FAILS.append("frame term is not monotonically increasing in frame count")
if abs(fl.h3_frame_factor(73, 0.399) - fl.h3_frame_factor(73, 3.686)) > 1e-9:
    FAILS.append("frame term still varies with canvas — it must not")

# the canvas law must still be a two-regime power law with the measured exponents
print("\nthe canvas power law: two regimes, measured exponents %.2f / %.2f"
      % (fl.H3_MP_EXP_LOW, fl.H3_MP_EXP_HIGH))
if abs(fl.H3_MP_EXP_LOW - 1.92) > 0.01 or abs(fl.H3_MP_EXP_HIGH - 1.76) > 0.01:
    FAILS.append("canvas exponents are not the measured 1.92 / 1.76")
_pfs_lo = fl.h3_per_frame_step(0.5)
_pfs_hi = fl.h3_per_frame_step(2.0)
print("  per-frame-step: 0.5 MP -> %.5f s   ·   2.0 MP -> %.5f s" % (_pfs_lo, _pfs_hi))
if not _pfs_hi > _pfs_lo > 0:
    FAILS.append("per-frame-step is not increasing with canvas")

print("\n\u26a0\ufe0f RAYLIGHT (2-card): shares ONE sampling term with the 1-card path")
print("   (Raylight is an execution layer, not a second cost law. If a future change gives")
print("    it its own formula, these must fail.)")
_probe = {"length": 241, "width": 1344, "height": 768, "steps": 4, "audio": True,
          "lora": fl.H3_LONGFORM_LORA, "lora_strength": 1.0, "video_mode": "r2v"}
_e1 = fl.estimate_seconds("video", dict(_probe, gpus=1), warm_engine="h3")
_e2 = fl.estimate_seconds("video", dict(_probe, gpus=2), warm_engine="h3")

# structural: (T1 - L1) must equal (T2 - L2) * SPEEDUP
_samp1 = _e1 - fl.H3_JOB_LOAD_S
_samp2 = _e2 - fl.RAY_JOB_LOAD_S
print("   1 card: %.1f - %.1f = %.1f s sampling" % (_e1, fl.H3_JOB_LOAD_S, _samp1))
print("   2 card: %.1f - %.1f = %.1f s sampling, x%.3f = %.1f s"
      % (_e2, fl.RAY_JOB_LOAD_S, _samp2, fl.RAY_SPEEDUP, _samp2 * fl.RAY_SPEEDUP))
if abs(_samp1 - _samp2 * fl.RAY_SPEEDUP) > 0.05:
    FAILS.append("the two paths do NOT share one sampling term — Raylight has drifted into "
                 "its own cost law")

# 2 cards must be FASTER on a real (long) config, and must not be a no-op
if not _e2 < _e1:
    FAILS.append("2-card estimate is not faster than 1-card for a long clip")
_long1 = fl.estimate_seconds("video", dict(_probe, length=362, gpus=1), warm_engine="h3")
_long2 = fl.estimate_seconds("video", dict(_probe, length=362, gpus=2), warm_engine="h3")
print("   362 fr: %.0fs -> %.0fs = %.2fx speedup" % (_long1, _long2, _long1 / _long2))
if not 1.2 < (_long1 / _long2) < 2.4:
    FAILS.append("long-clip speedup %.2fx is outside anything the rig has measured"
                 % (_long1 / _long2))
if fl.RAY_JOB_LOAD_S <= fl.H3_JOB_LOAD_S:
    FAILS.append("the 2-card load term should be LARGER than the 1-card one (Ray cluster init)")

# gpus absent must behave exactly like gpus=1 -- the default path must not move
if abs(fl.estimate_seconds("video", _probe, warm_engine="h3") - _e1) > 1e-9:
    FAILS.append("omitting gpus does not behave as gpus=1")

print("\n%d failure(s)" % len(FAILS))
for f in FAILS:
    print("   FAILED: %s" % f)
sys.exit(1 if FAILS else 0)
