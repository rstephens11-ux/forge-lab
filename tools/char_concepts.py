#!/usr/bin/env python3
"""Character concepts for the build-channel host — REAL renders on forge.

    python3 tools/char_concepts.py --dry     # plan + estimates, submit nothing
    python3 tools/char_concepts.py           # render the set

Follows tools/render_matrix.py exactly: imports the app the same way, and uses the app's
OWN `submit()` / `run_state()` / `collect()` — the same code the buttons call. Never edits
the 8,000-line shared file to add a hook.

⚠️ REFUSES to start if forge's queue is not empty, so it cannot evict another agent's run.
⚠️ Every case is priced with the app's own `estimate_seconds()` before it is submitted.

WHY THE PROMPTS LOOK LIKE THIS
Outlines are deliberately constrained for a reason beyond taste: the output is destined to be
CUT OUT and composited over build photos. That needs (a) a flat, uncluttered, single-colour
background, (b) a full silhouette with feet and arms clear of the body, and (c) somewhere the
matte can find a clean edge. Poses with arms crossed or hands behind the back cut badly.
"""
import importlib.util
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("forge_lab", os.path.join(HERE, "forge_lab.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

OUT = os.path.join(HERE, "drafts", "char-concepts")

# ---- the shared technical brief ----------------------------------------------------------
# Appended to every concept. These are CUTOUT requirements, not style choices.
CUTOUT = ("full body visible from head to toe, feet included and not cropped, "
          "arms held clear of the torso so the silhouette is clean, standing upright, "
          "flat plain single-colour background with nothing else in frame, "
          "no text, no lettering, no watermark, no signature, no border, no logo, "
          "exactly one character, centred, nothing overlapping the figure")

STYLE_LOCK = ("2D animation frame, hand-drawn cartoon, flat cel shading, bold clean outlines, "
              "clearly animated and NOT photorealistic, not a 3D render, not a photograph, "
              "adult woman with a curvy full-figured build and a confident expression")

CONCEPTS = [
    ("c1_2000s_adult_swim",
     "2000s adult-animation style: flat saturated cel fills, slightly angular features, "
     "crisp uniform line weight like American Dad or the Cleveland Show. "
     "Sleeveless grey work tee and dark trousers, heavy work gloves hanging at her sides, "
     "dark hair in a messy topknot, one smudge of thermal paste on her cheek, "
     "eyebrow raised in a wry confident smirk, standing in a plain flat mid-grey studio backdrop"),
    ("c2_90s_cel_overalls",
     "1990s cel animation style: thick black outlines, limited warm palette, "
     "the flat look of a Saturday-morning cartoon. Denim overalls worn over a striped tee, "
     "sleeves rolled, a ponytail, hands open at her sides, "
     "friendly half-smile, standing in a plain flat warm-tan backdrop"),
    ("c3_neon_retrofuturist",
     "retro-futurist cartoon style: strong cyan and magenta rim lighting, faint CRT scanline glow, "
     "neon highlights along the edges. Bomber jacket worn open over a fitted dark top, "
     "an LED collar band, short bob haircut, arms at her sides with fingers relaxed, "
     "cool appraising look, standing in a plain flat near-black backdrop"),
    ("c4_clean_mascot",
     "clean modern 2D mascot style: simple rounded shapes, large expressive eyes, "
     "bright flat palette, minimal shading. Oversized zip hoodie with the sleeves pushed up, "
     "hands visible and open, cropped hair, warm open smile, "
     "standing in a plain flat off-white backdrop"),
]

ONLY = None
for _a in sys.argv[1:]:
    if _a.startswith("--only"):
        ONLY = (_a.split("=", 1)[1] if "=" in _a else "").split(",")
DRY = "--dry" in sys.argv


def queue_empty():
    try:
        q = fl.get_json("/queue")
        return not (q.get("queue_running") or q.get("queue_pending"))
    except Exception as exc:
        print("  could not read forge's queue: %s" % exc)
        return False


def main():
    cases = [c for c in CONCEPTS if not ONLY or c[0] in ONLY]
    os.makedirs(OUT, exist_ok=True)

    plan = []
    for name, body in cases:
        prompt = "%s, %s, %s" % (STYLE_LOCK, body, CUTOUT)
        # Verified shape: this is exactly render_matrix.py's `img:t2i` case, but portrait so a
        # full-body figure has somewhere to stand.
        p = {"prompt": prompt, "steps": 12, "width": 768, "height": 1024, "resolution": 720}
        try:
            est = fl.estimate_seconds("t2i", p, [], None)
        except Exception as exc:
            est = None
            print("  %-22s ESTIMATE RAISED %s: %s" % (name, type(exc).__name__, exc))
        plan.append((name, p, est))
        print("  %-22s est=%s  %dx%d  %d chars of prompt"
              % (name, ("%.0f s" % est) if est else "?", p["width"], p["height"], len(prompt)))

    total = sum(e or 0 for _, _, e in plan)
    print("\n  %d concept(s), TOTAL ESTIMATE ≈ %.1f min" % (len(plan), total / 60.0))
    if DRY:
        print("\n--dry: nothing submitted.")
        for name, p, _ in plan:
            print("\n--- %s ---\n%s" % (name, p["prompt"]))
        return 0

    if not queue_empty():
        print("\nREFUSING TO START: forge's queue is not empty — another run is using the rig.")
        return 3

    results = []
    for name, p, _ in plan:
        print("\n" + "=" * 74)
        print(name)
        t0 = time.time()
        try:
            pid, seed = fl.submit("t2i", p, [])
        except Exception as exc:
            print("  SUBMIT FAILED — %s: %s" % (type(exc).__name__, str(exc)[:200]))
            results.append((name, None))
            continue
        print("  pid=%s seed=%s — waiting…" % (pid, seed))
        state, detail = "unknown", ""
        for _ in range(400):
            time.sleep(3)
            rs = fl.run_state(pid)
            state = rs.get("state")
            if state in ("done", "error", "gone"):
                detail = rs.get("detail") or ""
                break
        took = time.time() - t0
        if state != "done":
            print("  STATE=%s after %.0f s  %s" % (state, took, str(detail)[:200]))
            results.append((name, None))
            continue
        meta = fl.collect(pid) or {}
        if meta.get("error"):
            print("  COLLECT ERROR — %s" % meta["error"])
            results.append((name, None))
            continue
        # meta["images"] entries are DICTS ({"file": <name>, ...}); `file` is relative to
        # runs/<pid>/. Reading it as a path is the TypeError that killed the first sweep.
        saved = []
        for ent in (meta.get("images") or []):
            try:
                fn = ent.get("file") if isinstance(ent, dict) else ent
                if not fn:
                    continue
                f = fn if os.path.isabs(str(fn)) else os.path.join(fl.RUNS, pid, str(fn))
                if not (os.path.exists(f) and os.path.getsize(f) > 5000):
                    print("  small/missing: %r" % (fn,))
                    continue
                dest = os.path.join(OUT, "%s.png" % name)
                shutil.copy2(f, dest)
                sz = fl.image_size(dest)
                print("  IMAGE %s  %s  %d bytes  %.0f s"
                      % (os.path.basename(dest), sz, os.path.getsize(dest), took))
                saved.append(dest)
            except Exception as exc:
                print("  COPY ERROR on %r — %s: %s" % (ent, type(exc).__name__, str(exc)[:80]))
        results.append((name, saved[0] if saved else None))

    print("\n" + "=" * 74)
    print("CHARACTER CONCEPTS SUMROBIN")
    for name, path in results:
        print("  %-4s %-22s %s" % ("ok" if path else "FAIL", name, path or ""))
    bad = [r for r in results if not r[1]]
    print("\n  %d/%d rendered, %d failed" % (len(results) - len(bad), len(results), len(bad)))
    if not bad:
        print("  open:  %s" % OUT)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())