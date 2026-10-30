"""APPS — one-click tools, each does ONE job well (2026-10-17).

Ryan: "set a goal of having a working version of the forge lab … closer to higgsfield and
slopcannon. if that means making new tabs within the lab … give it a try."

Higgsfield's "apps" are small single-purpose tools (Lipsync Studio, Outfit Swap, Angles,
Product Placement…). These are the same idea on Forge Lab's own open engines — Qwen image
edit for stills, H3 for motion and lip-sync, Qwen3-TTS for the voice — and every result is an
ordinary take (so USE ▾ carries it on: an outfit swap -> "make it move" -> a talking photo).

Tk-free; apps_ui is the window. An app = inputs -> ONE run (or a voice + a run) -> a file.
"""
import os
import subprocess
import tempfile

import studio_pipeline as sp

c = sp.c

# the camera moves a "make it move" can ask for (Higgsfield's signature presets, in words H3
# reads well)
MOVES = [
    ("Slow push-in", "the camera slowly pushes in toward the subject"),
    ("Pull back reveal", "the camera slowly pulls back to reveal the whole scene"),
    ("Orbit", "the camera orbits slowly around the subject, keeping them centred"),
    ("Handheld", "handheld camera, a gentle natural sway, documentary feel"),
    ("Dolly zoom", "a dolly zoom: the subject stays the same size while the background "
                   "stretches away"),
    ("Crane up", "the camera rises smoothly upward, looking down at the subject"),
    ("Whip pan", "a fast whip pan into the subject, then the camera settles"),
    ("Static, alive", "a locked-off camera; only the subject and small things in the scene "
                      "move: hair, cloth, light"),
]
ANGLES = [
    ("Front", "full body, facing the camera"),
    ("Three-quarter", "three-quarter view, body turned slightly to the left"),
    ("Profile", "full side profile, facing right"),
    ("From behind", "seen from behind, three-quarter back view"),
    ("Close-up", "head-and-shoulders close-up"),
    ("Low angle", "low angle looking up at them, heroic"),
    ("High angle", "high angle looking down at them"),
]
STYLES = [
    ("Claymation", "a handmade claymation stop-motion still, plasticine textures, fingerprints "
                   "in the clay, miniature set"),
    ("Anime", "a hand-drawn anime cel, clean ink lines, flat cel shading, painted background"),
    ("Comic ink", "a bold comic-book panel, heavy black ink, halftone dots, flat colours"),
    ("Oil painting", "a classical oil painting, visible brushstrokes, rich glazes"),
    ("3D toy", "a glossy 3D vinyl toy figure render, soft studio light, rounded shapes"),
    ("70s film", "a 1970s 35mm photograph, warm faded colours, heavy grain, soft focus"),
    ("Felt puppet", "a felt puppet photographed on a little stage, stitched fabric faces"),
    ("Watercolour", "a loose watercolour painting, soft bleeding edges, paper texture"),
    ("Pixel art", "detailed 16-bit pixel art, a limited palette, crisp pixels"),
    ("Low-poly", "a low-poly 3D render, flat-shaded facets, pastel light"),
    # 2026-10-22 (Ryan: "leave the ones i have now i like them and update to include those
    # ones"). No studio, brand or living-artist names: trademark risk, and the repo is public.
    # -- for the cartoon series
    ("Saturday morning", "a 1990s TV cartoon cel, thick clean outlines, flat bright colours, "
                         "a simple painted background"),
    ("Rubber hose", "a 1930s black-and-white rubber-hose cartoon, pie-cut eyes, white gloves, "
                    "bouncy noodle limbs, film scratches"),
    ("Adult sitcom", "a flat 2D animated sitcom frame, simple shapes, thick even outlines, "
                     "muted suburban colours, deadpan faces"),
    ("Paper cut-out", "crude construction-paper cut-out animation, visible torn paper edges, "
                      "flat drop shadows"),
    ("Picture book", "a gouache children's picture-book illustration, soft textured paper, "
                     "gentle colours"),
    ("Chibi", "a cute chibi drawing, oversized head, tiny body, big shiny eyes"),
    # -- handmade and toys
    ("Action figure box", "a boxed action figure on a printed peg card, a clear plastic bubble, "
                          "little accessories in the tray beside it, the character's name "
                          "printed on the box"),
    ("Knitted", "crochet amigurumi dolls, visible yarn loops, a knitted miniature set"),
    ("Carved wood", "hand-carved painted wooden figurines, visible wood grain, chipped paint"),
    ("Tilt-shift miniature", "a tiny model-railway diorama photographed with tilt-shift blur, "
                             "toy-like scale"),
    ("Origami", "folded paper origami figures, crisp creases, a folded paper set"),
    ("Plush", "soft stuffed plush toys, fuzzy fabric, stitched seams, button eyes"),
    # -- satire and period
    ("Royal portrait", "a pompous baroque court portrait in a heavy gilt frame, a dark "
                       "varnished background, rich oil glazes"),
    ("Medieval manuscript", "an illuminated medieval manuscript page, flat stiff figures, gold "
                            "leaf, ornate margins with little doodles"),
    ("Propaganda poster", "a 1930s propaganda poster, bold diagonals, two or three flat inks, "
                          "heroic upward-looking poses"),
    ("Courtroom sketch", "a pastel courtroom sketch, quick loose strokes on beige paper"),
    ("Newspaper engraving", "a Victorian newspaper engraving, fine black crosshatching on "
                            "off-white newsprint"),
    ("Tintype", "an 1860s tintype photograph, silver-brown tones, a dark vignette, a stiff "
                "formal pose"),
    ("Marble statue", "a classical white marble statue, carved drapery, soft museum light"),
    # -- camera looks
    ("Security cam", "a grainy CCTV still from a high corner camera, washed-out colour, a "
                     "timestamp overlay"),
    ("Camcorder", "a 1990s home camcorder frame, VHS colour bleed, a date stamp in the corner"),
    ("Disposable camera", "a harsh-flash disposable-camera snapshot, red-eye, slightly "
                          "overexposed, film grain"),
    ("Film noir", "a 1940s black-and-white film noir still, hard shadows through window "
                  "blinds, smoky light"),
    ("Technicolor", "a 1950s Technicolor film still, saturated reds and teals, glossy studio "
                    "light"),
    ("Night vision", "a green night-vision camera image, glowing eyes, heavy noise"),
    ("Thermal", "a thermal-camera image, a rainbow heat map, warm bodies glowing"),
    # -- fake-trailer grades
    ("Epic fantasy", "an epic fantasy film still, golden-hour backlight, storm clouds, sun "
                     "shafts, embers in the air"),
    ("Grimdark war", "a grimdark war film still, overcast desaturated grey-green, fog, harsh "
                     "floodlights"),
    ("80s action poster", "a painted airbrushed 1980s action-movie poster, fire and neon, "
                          "dramatic poses"),
    ("Pulp paperback", "a painted 1950s pulp sci-fi paperback cover, lurid colours, worn "
                       "paper edges"),
    # -- wildcards
    ("Kid's crayon", "a drawing by a six-year-old in wax crayon, wobbly lines, wrong "
                     "proportions, on white paper"),
    ("Stained glass", "a cathedral stained-glass window, black lead lines, glowing jewel "
                      "colours"),
    ("Ukiyo-e", "a Japanese ukiyo-e woodblock print, flat colour, bold outlines, paper "
                "texture"),
    ("Tarot card", "an ornate tarot card with a title banner and a decorative gold border"),
    ("PS1 game", "a 1990s console 3D game screenshot, wobbly low-resolution textures, chunky "
                 "polygons"),
]
# The Restyle picker shows these as labelled groups in one sideways-scrolling strip.
# Every STYLES name sits in exactly one group (tools/apps_matrix.py checks it).
STYLE_GROUPS = [
    ("Favourites", ["Claymation", "Anime", "Comic ink", "Oil painting", "3D toy", "70s film",
                    "Felt puppet", "Watercolour", "Pixel art", "Low-poly"]),
    ("Cartoon", ["Saturday morning", "Rubber hose", "Adult sitcom", "Paper cut-out",
                 "Picture book", "Chibi"]),
    ("Handmade & toys", ["Action figure box", "Knitted", "Carved wood", "Tilt-shift miniature",
                         "Origami", "Plush"]),
    ("Satire & period", ["Royal portrait", "Medieval manuscript", "Propaganda poster",
                         "Courtroom sketch", "Newspaper engraving", "Tintype",
                         "Marble statue"]),
    ("Camera looks", ["Security cam", "Camcorder", "Disposable camera", "Film noir",
                      "Technicolor", "Night vision", "Thermal"]),
    ("Trailer grades", ["Epic fantasy", "Grimdark war", "80s action poster",
                        "Pulp paperback"]),
    ("Wildcards", ["Kid's crayon", "Stained glass", "Ukiyo-e", "Tarot card", "PS1 game"]),
]
EXPRESSIONS = ["laughing", "furious", "terrified", "smug", "crying", "shocked", "in love",
               "bored", "suspicious", "proud"]

APPS = [
    {"key": "talk", "name": "Talking photo", "kind": "video",
     "blurb": "any picture says your line, lips in sync",
     "needs": ["image", "line", "voice", "audio"]},
    {"key": "move", "name": "Make it move", "kind": "video",
     "blurb": "a still becomes a shot, with a camera move",
     "needs": ["image", "move", "prompt"]},
    {"key": "outfit", "name": "Outfit swap", "kind": "image",
     "blurb": "same person, new clothes",
     "needs": ["image", "text:What should they wear?", "image2:or a picture of the outfit"]},
    {"key": "angle", "name": "New angle", "kind": "image",
     "blurb": "the same person from another side",
     "needs": ["image", "angle"]},
    {"key": "place", "name": "Change the place", "kind": "image",
     "blurb": "same person, somewhere else",
     "needs": ["image", "text:Where are they now?"]},
    {"key": "face", "name": "Change the expression", "kind": "image",
     "blurb": "same face, a new feeling",
     "needs": ["image", "expression"]},
    {"key": "restyle", "name": "Restyle", "kind": "image",
     "blurb": "the same picture as claymation, anime, comic ink…",
     "needs": ["image", "style"]},
    {"key": "vertical", "name": "Make it vertical", "kind": "local",
     "blurb": "a wide video becomes a 9:16 short, instantly",
     "needs": ["video"]},
    {"key": "product", "name": "Product in hand", "kind": "image",
     "blurb": "they hold, wear or use your product",
     "needs": ["image", "image2:The product", "text:How do they use it?"]},
]


MAX_TALK = 8          # one H3 shot; a longer recording is cut to its first 8 s


def get(key):
    return next(a for a in APPS if a["key"] == key)


# ------------------------------------------------------------------ canvas
def image_wh(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except Exception:
        return None


def canvas_for(path, quality="Draft"):
    """The video canvas that matches the picture's shape: tall -> 9:16, wide -> 16:9."""
    wh = image_wh(path)
    tall = not wh or wh[1] >= wh[0]
    return sp.canvas({"aspect": "9:16" if tall else "16:9", "quality": quality})


def still_size(path, long=1280):
    """An edit keeps the source picture's shape, long side `long`, multiples of 32."""
    wh = image_wh(path) or (long, long)
    w, h = wh
    s = float(long) / max(w, h)
    return max(256, int(round(w * s / 32)) * 32), max(256, int(round(h * s / 32)) * 32)


# ------------------------------------------------------------------ the prompts
SAME = ("the same person as <image1> — identical face, hair, skin and build")


def prompt_for(key, a):
    """The edit / video prompt for app `key` with inputs `a`."""
    if key == "outfit":
        if a.get("image2"):
            return ("%s, now wearing exactly the outfit shown in <image2>%s. Same pose, same "
                    "place, same light, photorealistic." % (
                        SAME.capitalize(), (" (" + a["text"] + ")") if a.get("text") else ""))
        return ("%s, now wearing %s. Same pose, same place, same light, photorealistic."
                % (SAME.capitalize(), a.get("text") or "a different outfit"))
    if key == "angle":
        view = dict(ANGLES).get(a.get("angle"), a.get("angle") or "three-quarter view")
        return ("%s and exactly the same clothes, %s; same place and light, photorealistic."
                % (SAME.capitalize(), view))
    if key == "place":
        return ("%s and exactly the same clothes and pose, now in %s, lit by that place's own "
                "light, photorealistic." % (SAME.capitalize(), a.get("text") or "a new place"))
    if key == "face":
        return ("%s and the same clothes, pose and place, but %s — a clear, natural "
                "expression, photorealistic." % (SAME.capitalize(), a.get("expression") or
                                                 "smiling"))
    if key == "product":
        return ("%s and the same clothes and place, now %s the product from <image2> — the "
                "product exactly as in <image2>, its shape, colours and label unchanged, "
                "clearly visible. Photorealistic advertising photo."
                % (SAME.capitalize(), a.get("text") or "holding up"))
    if key == "restyle":
        style = dict(STYLES).get(a.get("style"), a.get("style") or STYLES[0][1])
        return ("The same scene and the same people as <image1> \u2014 the same composition, "
                "poses and each person's recognisable features, clothes and colours \u2014 "
                "redrawn as %s." % style)
    if key == "move":
        mv = dict(MOVES).get(a.get("move"), a.get("move") or MOVES[0][1])
        extra = (a.get("prompt") or "").strip()
        return ("%s%s. Everything in the first frame stays the same person, clothes and place."
                % ((extra.rstrip(".") + ". ") if extra else "", mv[:1].upper() + mv[1:]))
    if key == "talk":
        extra = (a.get("prompt") or "").strip()
        return ("The person in the first frame says the line to the camera, lips moving "
                "exactly with the words, natural small head movements and blinks%s. The same "
                "face, hair and clothes as the first frame."
                % ("; " + extra.rstrip(".") if extra else ""))
    raise ValueError("unknown app %r" % key)


# ------------------------------------------------------------------ running
def _wait_file(pid, exts):
    m = c.wait_and_collect(pid, timeout=1800) or {}
    for f_ in m.get("images") or []:
        fn = f_.get("file") or ""
        if fn.lower().endswith(exts):
            return os.path.join(c.RUNS, m.get("id") or pid, fn)
    raise RuntimeError("the run finished but nothing came back (%s)"
                       % (m.get("error") or m.get("status") or "?"))


def say_line(line, voice_slug, progress=None):
    """The line in that library voice (Qwen clone of its reference). -> wav path"""
    import dialogue as dlg
    import voicelab as vl
    v = dlg.load_voice(voice_slug)
    if not v:
        raise ValueError("pick a voice first (or build one on the Voices page)")
    out = vl.test(v, line, "neutral", engines=("qwen",), progress=progress)
    wav = out.get("qwen")
    if not wav or str(wav).startswith("error"):
        raise RuntimeError("the voice didn't come back: %s" % wav)
    keep = os.path.join(tempfile.mkdtemp(prefix="forge-app-"), "line.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", wav, "-ac", "1", "-ar", "24000", keep],
                   check=True, capture_output=True, timeout=60)
    return keep


def run(key, a, progress=None, quality="Draft", port=None):
    """Run app `key` with inputs `a` -> {"file", "pid", "kind"}. Inputs: image, image2, text,
    line, voice (a library slug), move, angle, expression, prompt, seconds.

    `port` pins the ComfyUI instance the job was GIVEN (the queue dispatcher checks a card has
    room and then hands the job to that exact lane — re-picking a lane at submit time would
    double-book the other card). None = route the way everything else in the app routes.
    """
    app = get(key)
    if app["kind"] == "local":
        return run_local(key, a, progress)
    img = a.get("image")
    if not img or not os.path.exists(img):
        raise ValueError("pick a picture first")
    for need in app["needs"]:
        nm = need.split(":")[0]
        has_rec = bool(a.get("audio") and os.path.exists(a["audio"]))
        if nm == "line" and not (a.get("line") or "").strip() and not has_rec:
            raise ValueError("type the line they should say \u2014 or use a recording")
        if nm == "voice" and not a.get("voice") and not has_rec:
            raise ValueError("pick a voice for them \u2014 or use a recording")
        if nm == "image2" and key == "product" and not a.get("image2"):
            raise ValueError("pick a picture of the product")
    prompt = prompt_for(key, a)
    if app["kind"] == "image":
        w, h = still_size(img)
        refs = [img] + ([a["image2"]] if a.get("image2") else [])
        if progress:
            progress("%s…" % app["name"].lower())
        p = {"prompt": prompt, "steps": 25, "seed": int(a.get("seed", -1)), "custom_size": True,
             "width": w, "height": h, "prefix": "apps/%s" % key, "port": port}
        pid, _ = c.submit("edit", p, refs)
        return {"file": _wait_file(pid, (".png", ".jpg", ".jpeg", ".webp")), "pid": pid,
                "kind": "image", "prompt": prompt}
    # video apps: H3 from the picture as the first frame
    w, h = canvas_for(img, quality)
    secs = int(a.get("seconds") or 5)
    p = {"prompt": prompt, "width": w, "height": h, "steps": sp.STEPS,
         "length": c.h3_frames(secs), "seed": int(a.get("seed", -1)),
         "prefix": "apps/%s" % key, "video_mode": "i2v", "audio": True, "lora": None,
         "lora_strength": 1.0, "quality": quality, "gpus": 1, "port": port}
    if key == "talk":
        if a.get("audio") and os.path.exists(a["audio"]):
            # YOUR RECORDING (any audio or video file): mono 24 kHz, up to one 8 s shot
            wav = os.path.join(tempfile.mkdtemp(prefix="forge-app-"), "rec.wav")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", a["audio"], "-vn", "-ac", "1",
                            "-ar", "24000", "-t", str(MAX_TALK), wav], check=True,
                           capture_output=True, timeout=120)
        else:
            if progress:
                progress("saying the line")
            wav = say_line(a["line"], a["voice"], progress=progress)
        dur = sp._duration(wav) or 3.0
        secs = sp._fit_seconds(dur)
        p["length"] = c.h3_frames(secs)
        p["guide_audio_path"] = wav
    if progress:
        progress("%s — rendering on forge" % app["name"].lower())
    pid, _ = c.submit("video", p, [img])
    return {"file": _wait_file(pid, (".mp4", ".webm", ".mov")), "pid": pid, "kind": "video",
            "prompt": prompt}


def vertical_out(src):
    base, _ext = os.path.splitext(src)
    return base + "_vertical.mp4"


def run_local(key, a, progress=None):
    """Apps that run on the Mac, not the rig. vertical: 1080x1920, the video fitted to the
    width over a blurred, darkened copy of itself (the TikTok/Reels look), sound kept."""
    if key != "vertical":
        raise ValueError("unknown app %r" % key)
    src = a.get("video")
    if not src or not os.path.exists(src):
        raise ValueError("pick a video first")
    if progress:
        progress("making it vertical")
    out = vertical_out(src)
    vf = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
          "boxblur=24:2,eq=brightness=-0.12[bg];"
          "[0:v]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
          "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, "-filter_complex", vf, "-map", "[v]",
           "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-preset", "medium", "-c:a", "aac", "-b:a", "192k", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("couldn't make it vertical: %s" % (r.stderr or "")[-200:])
    return {"file": out, "pid": None, "kind": "video", "prompt": ""}


def recent_videos(n=10):
    """Newest finished cuts (Episode / Quick) first, then video takes: [(path, label)]."""
    out = []
    try:
        root = sp.studio_root()
        cuts = []
        for d in os.listdir(root):
            p = os.path.join(root, d, "%s_final.mp4" % d)
            if os.path.exists(p):
                cuts.append((os.path.getmtime(p), p, d))
        out += [(p, d) for _t, p, d in sorted(cuts, reverse=True)[:n]]
    except Exception:
        pass
    try:
        for it in c.read_index():
            for f_ in it.get("images") or []:
                fn = f_.get("file") or ""
                if fn.lower().endswith((".mp4", ".webm", ".mov")):
                    p = os.path.join(c.RUNS, it.get("id") or "", fn)
                    if os.path.exists(p):
                        out.append((p, (it.get("label") or "")[:30]))
                    break
            if len(out) >= n:
                break
    except Exception:
        pass
    return out[:n]


def estimate(key, a=None, quality="Draft"):
    """Rough seconds, from the app's own estimate where it can."""
    app = get(key)
    if app["kind"] == "local":
        return 20
    if app["kind"] == "image":
        return 45
    secs = int((a or {}).get("seconds") or 5)
    try:
        img = (a or {}).get("image")
        w, h = canvas_for(img, quality) if img else sp.canvas({"aspect": "9:16",
                                                                "quality": quality})
        t = c.estimate_seconds("video", {"prompt": "x", "width": w, "height": h,
                                         "steps": sp.STEPS, "length": c.h3_frames(secs),
                                         "video_mode": "i2v", "quality": quality}, [])
        return t + (25 if key == "talk" else 0)
    except Exception:
        return 150


def recent_images(n=12):
    """The newest image takes, for the picker: [(path, label)]."""
    out = []
    try:
        for it in c.read_index():
            for f_ in it.get("images") or []:
                fn = f_.get("file") or ""
                if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    p = os.path.join(c.RUNS, it.get("id") or "", fn)
                    if os.path.exists(p):
                        out.append((p, (it.get("label") or it.get("mode") or "")[:30]))
                    break
            if len(out) >= n:
                break
    except Exception:
        pass
    return out
