#!/usr/bin/env python3
"""
Forge Lab — a native macOS app for the forge ComfyUI rig.

Built like Lazy Downloader: pure stdlib tkinter, bundled with PyInstaller into a
standalone .app. No local web server, no browser, no CORS layer — it talks to
forge's HTTP API (and the websocket for live step progress) directly.

Three modes, all lifted from graphs that have SUCCEEDED on this rig:
  text to image   qwen_t2i     (verified: prompt 02267022)
  edit image      qwen_edit    (verified: prompt 47774a56)
  remove bg       qwen_edit + a canned prompt  (same graph, per the template notes)

Staged, estimate-first (borrowed from the Slop Cannon workstation model):
nothing is submitted until you approve the estimate.

  python3 forge_lab.py              # launch the app
  python3 forge_lab.py --selftest   # exercise the engine headlessly
"""

import base64
import hashlib
import json
import os
import queue
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

# ⚠️ FORGE_LAB_DATA lets a headless process (the rig, a test harness) point at its
# OWN data dir. Default unchanged; RUNS/HOSTFILE/CHARACTERS all derive from DATA below.
DATA = os.path.expanduser(os.environ.get("FORGE_LAB_DATA") or "~/forge-lab")

# ⚠️ Any FORGE_LAB_* env var set to 1 means a DRIVER is running — all 15 of them are env-gated.
# A driver must FAIL, never HANG: Tk swallows callback exceptions and keeps looping, so a broken
# assertion or a FileNotFoundError inside an after() callback runs until the outer timeout instead
# of reporting anything. That cost 5 minutes twice on 2026-09-27. See _log_exc.
_DRIVER = any(k.startswith("FORGE_LAB_") and v == "1" for k, v in os.environ.items())
RUNS = os.path.join(DATA, "runs")
HOSTFILE = os.path.join(DATA, "forge_host.txt")
os.makedirs(RUNS, exist_ok=True)

CANDIDATES = [u.rstrip("/") for u in [
    os.environ.get("FORGE_URL"),
    "http://forge:8188",
    "http://forge.local:8188",
] if u]
SSH_TARGET = os.environ.get("FORGE_SSH", "ryan@forge")

# ComfyUI streams step progress ONLY to the websocket that submitted the prompt,
# so this id must match at submit time. Deriving it per launch also stops a stale
# socket (from an earlier run, or a probe) from shadowing the registration under
# the same name — which silently kills progress forever.
CLIENT_ID = "forge-lab-" + os.urandom(4).hex()
WS_LAST = 0.0
WS_CONNECTED = False

FORGE = CANDIDATES[0]
F_HOST, F_PORT = "forge", 8188
_ok = False

# --------------------------------------------------------------------- graphs
QWEN_T2I = {
    "451": {"class_type": "UNETLoader",
            "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors",
                       "weight_dtype": "default"}},
    "453": {"class_type": "CLIPLoader",
            "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors",
                       "type": "qwen_image"}},
    "454": {"class_type": "VAELoader",
            "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "452": {"class_type": "TextEncodeQwenImage21",
            "inputs": {"clip": ["453", 0], "prompt": "", "negative_prompt": "",
                       "resolution": 1024, "images": {}}},
    "456": {"class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
    "458": {"class_type": "KSampler",
            "inputs": {"model": ["451", 0], "positive": ["452", 0],
                       "negative": ["452", 1], "latent_image": ["456", 0],
                       "seed": 0, "steps": 25, "cfg": 1.0,
                       "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "457": {"class_type": "VAEDecode", "inputs": {"samples": ["458", 0], "vae": ["454", 0]}},
    "461": {"class_type": "SaveImage", "inputs": {"images": ["457", 0],
                                                  "filename_prefix": "forge_lab"}},
}

# edit: the Unet goes through QwenImage21Cache, the reference image is spliced in
# as latents by the text encoder, and ComfySwitchNode picks which latent wins.
QWEN_EDIT = {
    "451": {"class_type": "UNETLoader",
            "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors",
                       "weight_dtype": "default"}},
    "453": {"class_type": "CLIPLoader",
            "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors",
                       "type": "qwen_image"}},
    "454": {"class_type": "VAELoader",
            "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "469": {"class_type": "QwenImage21Cache",
            "inputs": {"model": ["451", 0], "device": "auto", "dtype": "default"}},
    "470": {"class_type": "LoadImage", "inputs": {"image": ""}},
    "474": {"class_type": "TextEncodeQwenImage21",
            "inputs": {"clip": ["453", 0], "prompt": "", "negative_prompt": "",
                       "resolution": 1024,
                       # ⚠️ AUTOGROW INPUTS NEED DOTTED KEYS. A nested dict
                       # {"images": {"image_1": [...]}} VALIDATES and even runs —
                       # but never reaches the node, so the reference is silently
                       # ignored and you get a plain text-to-image render. Verified
                       # with a solid-colour control image. Must be dotted.
                       "images.image_1": ["470", 0],
                       "vae": ["454", 0]}},
    "456": {"class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
    # switch False -> on_false -> the encoder's LATENT (canvas follows image_1)
    # switch True  -> on_true  -> EmptyLatentImage (custom size)
    "468": {"class_type": "ComfySwitchNode",
            "inputs": {"switch": False, "on_false": ["474", 2], "on_true": ["456", 0]}},
    "458": {"class_type": "KSampler",
            "inputs": {"model": ["469", 0], "positive": ["474", 0],
                       "negative": ["474", 1], "latent_image": ["468", 0],
                       "seed": 0, "steps": 25, "cfg": 1.0,
                       "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "457": {"class_type": "VAEDecode", "inputs": {"samples": ["458", 0], "vae": ["454", 0]}},
    "461": {"class_type": "SaveImage", "inputs": {"images": ["457", 0],
                                                  "filename_prefix": "forge_lab"}},
}

BG_PROMPT = ("This is an RGBA format image with transparency. Remove the background "
             "from <image1> completely, keeping the subject exactly as it is — same "
             "pose, lighting, colours and detail. The image has an alpha channel and "
             "a transparent background.")

# Server-spec caps (read from /object_info, not guessed): the H3 reference node's
# ref_images autogrow is max 9, and Qwen-Image's images autogrow names image_1..image_16.
# Defined BEFORE MODES — MODES references them at import time.
H3_MAX_REFS = 9
QWEN_MAX_REFS = 16

MODES = {
    "t2i": {"label": "Text to image", "graph": QWEN_T2I, "ref": False,
            "help": "25 steps / cfg 1 / euler / simple."},
    "edit": {"label": "Edit image", "graph": QWEN_EDIT, "ref": True,
             "max_refs": QWEN_MAX_REFS,
             "help": "Mention references as <image1>, <image2>… Output follows <image1>'s aspect."},
    "bg": {"label": "Remove background", "graph": QWEN_EDIT, "ref": True,
           "max_refs": QWEN_MAX_REFS,
           "help": "Image-edit with a canned transparency prompt — edit it freely.",
           "prompt": BG_PROMPT},
    # One video entry; the VARIANT lives in the VIDEO MODE dropdown (VIDEO_MODES).
    "video": {"label": "Video", "video": True, "graph": None, "ref": False,
              "steps": 12, "width": 1344, "height": 768,
              "help": "H3 video — the per-model note under POLISH PROMPT says what each "
                      "variant is for. 12 steps (base model) is the "
                      "quality default; 4 steps uses the turbo distill — ~4x faster but the "
                      "face goes soft."},
    # WAN 2.2 is a separate KIND, not a variant of H3: 16 fps instead of 24, NO audio at
    # all, no character LoRA, and its own quality axis (turbo and full are different
    # models, not a speed slider). The variant lives in WAN_MODES.
    "wan": {"label": "WAN 2.2 video", "video": True, "wan": True, "graph": None, "ref": False,
            "steps": 4, "width": 832, "height": 480,
            "help": "WAN 2.2 14B — 16 fps and NO audio. Turbo (4 steps + lightning LoRAs, "
                    "cfg 1.0 — the negative prompt is INERT) and Full (20 steps, no LoRAs, "
                    "shift 8.0, a real negative prompt) are different models, not a speed "
                    "setting. Shift, the LoRA pair, the expert split and the step count are "
                    "fixed per path. It CANNOT carry a character LoRA (H3 ref2va only)."},
    # LTX 2.3 IC-LoRA — video-to-video by STRUCTURE, not by text. Its own KIND slot for SIZE
    # (canvas must be divisible by 64, which is a rule no other engine has).
    "ltx": {"label": "LTX 2.3 video-to-video", "video": True, "ltx": True, "graph": None,
            "ref": False, "steps": 8, "width": 1280, "height": 704,
            "help": "LTX 2.3 + IC-LoRA Union Control — feed it a CLIP and it rebuilds that clip's "
                    "structure in a new look. Depth is extracted from your clip on the rig, so "
                    "the motion and framing survive while the prompt decides the style. 25 fps "
                    "and it generates its own soundtrack. 8 steps, cfg 1.0 (the negative prompt "
                    "is inert on this path). The CANVAS MUST BE DIVISIBLE BY 64."},
    # Wan Animate 2 — MOTION TRANSFER: a character still performs a driving clip. Its own engine:
    # no character LoRA (identity is the still), its own canvas set and its own load term.
    "animate2": {"label": "Wan Animate 2 motion transfer", "video": True, "animate2": True,
                 "graph": None, "ref": True, "steps": 6, "width": 480, "height": 848,
                 "help": "Wan Animate 2 — your STILL performs the DRIVING CLIP. The pose comes "
                         "from the clip itself (no pose estimator) and there is NO character LoRA "
                         "here: identity is the still alone. 6 steps, lcm, cfg 1.0, shift 5.0, "
                         "one 81-frame pass, picture only — the driving track is muxed on this "
                         "Mac if you want it. REF 1 is the character, and the source video comes "
                         "from the SOURCE VIDEO picker."},
    # SONG — YuE2 (style + lyrics -> a finished song). Its OWN engine (a separate model set
    # that evicts the others on one 3090, like WAN vs H3), and its own workspace window: a
    # song has no canvas, no frames, no fps and no reference stills, so every control on the
    # form would be wrong for it. ⚠️ NOT `video: True` — that flag routes it into the H3
    # dispatch chain (the broad-flag trap). engine_of() gives it "yue2".
    "song": {"label": "Song", "song": True, "graph": None, "ref": False,
             "steps": 32, "help": "YuE2 — a style prompt and lyrics become a full song with "
                     "vocals and accompaniment, planned as a melody first and then sung. "
                     "~10x faster than it plays; one card is enough."},
}

IMAGE_MODES = ("t2i", "edit", "bg")

# ---- the MODEL dropdown ---------------------------------------------------------------
# KIND is Image|Video; MODEL lists that KIND's models in ONE flat set. Video entries name
# their ENGINE in the label, because "first frame" exists on BOTH H3 and WAN and picking the
# wrong one is invisible until you look at the result. Each entry carries (mode, variant) so
# everything downstream keeps working off MODES / VIDEO_MODES / WAN_MODES unchanged.
MODEL_CHOICES = {
    "Image": [("t2i", None, "Text to image"),
              ("edit", None, "Edit image"),
              ("bg", None, "Remove background")],
    "Video": [("video", "r2v", "H3 · Character reference (r2v)"),
              ("video", "i2v", "H3 · First frame (i2v)"),
              ("video", "flf", "H3 · First + last frame"),
              ("video", "t2v", "H3 · Text only"),
              ("video", "cont", "H3 · Continue a clip"),
              ("wan", "t2v", "WAN 2.2 · Text to video"),
              ("wan", "i2v", "WAN 2.2 · First frame (i2v)"),
              ("wan", "flf2v", "WAN 2.2 · First + last frame"),
              ("wan", "s2v", "WAN 2.2 · Song / lip-sync (s2v)"),
              ("ltx", "iclora", "LTX 2.3 · Restyle from a clip (ic-lora)"),
              ("animate2", "motion", "WAN ANIMATE 2 · Motion transfer (still + clip)")],
}


def model_label(mode, video_mode=None, wan_mode=None):
    """The MODEL dropdown label for a (mode, variant) pair — the reverse of the registry."""
    for _kind, choices in MODEL_CHOICES.items():
        for (m, v, lab) in choices:
            if m != mode:
                continue
            if m == "video" and v != video_mode:
                continue
            if m == "wan" and v != wan_mode:
                continue
            return lab
    return MODES.get(mode, {}).get("label", str(mode))


def kind_label(mode):
    """The TYPE dropdown label for a mode."""
    m = MODES.get(mode) or {}
    if m.get("song"):
        return "Song"
    return "Video" if m.get("video") else "Image"


# The note under POLISH PROMPT. Per MODEL, not per mode: "H3 first frame" and "WAN first
# frame" are different jobs, and the old per-mode text still told you to use a "VIDEO MODE
# dropdown" that no longer exists.
MODEL_HELP = {
    "t2i": "Text to image — the whole canvas is yours: shape it with ASPECT, size it with "
           "WIDTH/HEIGHT. No reference needed, so this is the fastest way to explore a look.",
    "edit": "Edit image — change a still you already have. Add references and cite them in "
            "the prompt as <image1>, <image2>… <image1> is the one being edited, and the "
            "canvas follows it.",
    "bg": "Remove background — image-edit with a canned transparency prompt. The output "
          "carries a real alpha channel.",
    "video:r2v": "H3 character reference — LOCKS A CHARACTER'S IDENTITY from your stills. "
                 "Tag each one in the prompt (<Picture 1>, <Picture 2>… in the order added) "
                 "and say what each drives. ONE clean still per character: untagged extras "
                 "blend the faces together. This is the main character path.",
    "video:i2v": "H3 first frame — animates ONE still: an approved frame, or a still you "
                 "generated first. ⚠️ This is the fl2va path, where the character LoRA does NOT "
                 "apply — the still IS the identity, so make it a good one.",
    "video:flf": "H3 first + last frame — animates BETWEEN two stills. Use it when you know "
                 "where the shot has to end: a door closing, a cut to a pose.",
    "video:t2v": "H3 text only — no image at all. B-roll, skies, establishing shots: anything "
                 "with no face to keep consistent.",
    "video:cont": "H3 continue a clip — takes the previous clip's LAST frame as the first "
                  "frame. This is how you go past the model's ~15s ceiling without a cut.",
    "wan:t2v": "WAN 2.2 text only — 16 fps, NO audio, and it CANNOT carry a character LoRA "
               "(H3 ref2va only). Use it for B-roll and backgrounds. Turbo (4 steps, cfg 1.0) "
               "has an INERT negative prompt; the Full 20-step path is where it works.",
    "wan:i2v": "WAN 2.2 first frame — 16 fps, NO audio, no character LoRA. B-roll and "
               "backgrounds; the still is resized onto the SIZE canvas.",
    "wan:flf2v": "WAN 2.2 first + last frame — 16 fps, NO audio, no character LoRA. Ref 1 is "
                 "the first frame, ref 2 the last one.",
    "wan:s2v": "WAN 2.2 Song / lip-sync (S2V) — the character PERFORMS the audio you pick: "
               "mouth, jaw and small head movements follow the track. ONE clean still and one "
               "audio file. That still IS the character — this path CANNOT carry a "
               "character LoRA, so identity comes from the picture alone. 16 fps with one continuous "
               "audio track across the whole scene. ONE path only: 4 steps, cfg 1.0, shift 8. "
               "It renders SILENT; the audio is muxed in on this Mac when the clip downloads.",
    "ltx:iclora": "LTX 2.3 + IC-LoRA Union Control — RESTYLE FROM A CLIP. Hand it a source video: "
                  "depth is extracted from it on the rig and the IC-LoRA rebuilds that structure in "
                  "a new look (prompt decides the style). The clip's motion and framing survive; the "
                  "content does not. 25 fps, and unlike WAN it DOES generate a soundtrack. 8 steps "
                  "at cfg 1.0 — the negative prompt is inert here. ⚠️ Canvas must be divisible by "
                  "64 (1280x704 native, 576x320 draft). ⚠️ Changing resolution changes the RENDER, "
                  "not just its sharpness — draft small, then UPSCALE the file, never re-render up.",
}


def model_help(mode, video_mode=None, wan_mode=None):
    """The note text for the CURRENT model (falls back to the mode's own help)."""
    m = MODES.get(mode) or {}
    if m.get("wan"):
        return MODEL_HELP.get("wan:%s" % (wan_mode or WAN_VARIANT_DEFAULT), m.get("help", ""))
    if m.get("ltx"):
        return MODEL_HELP.get("ltx:iclora", m.get("help", ""))
    if m.get("animate2"):
        return MODEL_HELP.get("animate2:motion", m.get("help", ""))
    if engine_of(mode) == "h3":
        return MODEL_HELP.get("video:%s" % (video_mode or "r2v"), m.get("help", ""))
    return MODEL_HELP.get(mode, m.get("help", ""))

# H3 video variants. `node` selects the DiT + conditioning node; `refs` is how many
# stills the mode needs (0 = pure text). t2v, flf and cont all run the SAME fl2va
# node — only which of first_frame / last_frame get wired differs, which is why they
# are dropdown options and not separate graphs.
VIDEO_MODES = {
    "r2v":  {"label": "Character reference (r2v)", "node": "r2v", "refs": 1, "max": 9,
             "note": "ref2va — address each still in the prompt as <Picture 1>, "
                     "<Picture 2>… in the order added. One clean still per character: "
                     "UNTAGGED extra stills blend the faces together"},
    "i2v":  {"label": "First frame (i2v)", "node": "i2v", "refs": 1, "max": 1,
             "note": "fl2va — animate one keyframe"},
    "flf":  {"label": "First + last frame (flf)", "node": "i2v", "refs": 2, "max": 2,
             "note": "fl2va — ref 1 = first frame, ref 2 = last frame"},
    "t2v":  {"label": "Text only (t2v)", "node": "i2v", "refs": 0, "max": 0,
             "note": "fl2va — no image at all"},
    "cont": {"label": "Continue a clip (cont)", "node": "i2v", "refs": 1, "max": 1,
             "note": "fl2va — feed the previous clip's last frame"},
}
# Server-spec caps live above MODES (they are referenced at import time).

# Quality presets. Steps and canvas size are the two knobs that actually matter,
# and the sane range is narrow: Qwen-Image 2.1's official pipeline runs ~40-50
# steps, its own template starts at 25, and the model is native at 2K (2048).
# Cost on one RTX 3090, MEASURED: 0.70 s per MP-step (25 steps at 1 MP ~= 18 s).
QUALITY = {
    "Low":    {"steps": 12, "mp": 0.5, "resolution": 720,
               "note": "Fast draft - 12 steps at ~0.5 MP. For checking composition, "
                       "not for keeping."},
    "Medium": {"steps": 25, "mp": 1.0, "resolution": 1024,
               "note": "Default - 25 steps at ~1 MP, what the official template uses."},
    "Max":    {"steps": 50, "mp": 4.0, "resolution": 2048,
               "note": "Best - 50 steps at the model's native 2K ceiling. Slowest "
                       "(~2m20s on one 3090) and heaviest on VRAM."},
    "Custom": {"steps": None, "mp": None, "resolution": None,
               "note": "Your own numbers - no preset applied."},
}

# Video quality is a DIFFERENT axis. H3 has no useful megapixel knob here (the canvas
# IS the clip size) and the only thing that changed quality in testing was the step
# count: 4 steps = soft/melted face, 12 = crisp, 20 = no visible gain. So video gets
# its own presets instead of borrowing the image ones (a 50-step image preset leaking
# into video is what surfaced the dangling-link bug).
VIDEO_QUALITY = {
    "Draft":    {"steps": 4,  "note": "4 steps + the turbo distill - about 20x faster "
                                      "(~10s warm) but the FACE GOES SOFT. Blocking only."},
    "Standard": {"steps": 12, "note": "Default - 12 steps on the base model. Crisp "
                                      "features; 20 steps measured no better."},
    "High":     {"steps": 20, "note": "20 steps - no visible gain over 12 in testing, "
                                      "for ~2.5x the render time."},
}

# Video canvas presets. The node accepts ANY multiple of 32 (INT, 32..16384), but
# every shipped H3 template uses the native 1344x768, and VRAM scales with the pixel
# count: native measured 23,799 MiB peak on one 3090 (96.8% of 24 GB). 720 and 1080
# are not multiples of 32, so the honest numbers are 704 and 1088.
VIDEO_SIZES = {
    "Native 1344×768": (1344, 768),
    "480p 832×480":    (832, 480),
    # WAN 2.2 S2V's own canvas — the shipped template's, and the only size it has been timed
    # at. Square also suits a face/singing shot better than WAN's 832x480.
    "640×640 (s2v)":   (640, 640),
    "720p 1280×704":   (1280, 704),
    "1080p 1920×1088": (1920, 1088),
    "2K 2560×1440":    (2560, 1440),
}
VIDEO_SIZE_DEFAULT = "Native 1344×768"
# WAN's measured — and natural — canvas is 480p. Carrying H3's native 1344x768 into WAN
# silently makes every WAN run an untimed one at ~2.5x the pixels of the only size that has
# been measured, so each KIND remembers its own SIZE.
WAN_SIZE_DEFAULT = "480p 832×480"
# S2V's own measured canvas (the template's). Inheriting WAN's 832x480 or H3's 1344x768 would
# silently make every S2V run a size that has never been timed here.
S2V_SIZE_DEFAULT = "640×640 (s2v)"
SIZE_DEFAULT_BY_KIND = {"image": VIDEO_SIZE_DEFAULT, "video": VIDEO_SIZE_DEFAULT,
                        "wan": WAN_SIZE_DEFAULT, "wan_s2v": S2V_SIZE_DEFAULT}


def size_key(mode, wan_mode=None):
    """Which SIZE memory slot a (mode, variant) uses. S2V gets its OWN slot: it is a different
    model with a different measured canvas, and WAN's 832x480 must not bleed into it. LTX
    likewise: its canvas has the divisible-by-64 rule and its own measured pair."""
    if MODES.get(mode, {}).get("wan") and (wan_mode or "") == "s2v":
        return "wan_s2v"
    return kind_of(mode)


# ⚠️ LTX and Wan Animate 2 BAKE their step count into the graph (8 and 6) and `params()` reads it
# from the REGISTRY, not the STEPS field — so H3's Draft/Standard/High had nothing to set here:
# picking "High" showed 20 steps while forge ran 8. A control that changes the number on screen and
# nothing else is a LIE, and it is the same broad-`video`-flag mistake that produced six other wrong
# readings. Both engines get ONE readout entry, exactly like S2V's single path.
# ⚠️ The label is DERIVED from the registry so it cannot drift from what actually runs.
def fixed_quality_label(kind):
    return "%d steps — baked into the graph" % int(MODES[kind]["steps"])


FIXED_QUALITY_NOTE = {
    "ltx": "8 steps, cfg 1.0 — fixed on this path, and the negative prompt is inert here.",
    "animate2": "6 steps, lcm, cfg 1.0, shift 5.0 — fixed, one 81-frame pass.",
}


def kind_of(mode):
    """Which KIND a mode belongs to — used to swap the right quality axis and SIZE."""
    m = MODES.get(mode) or {}
    if m.get("wan"):
        return "wan"
    if m.get("ltx"):
        return "ltx"
    if m.get("animate2"):
        return "animate2"
    return "video" if engine_of(mode) == "h3" else "image"


def engine_of(mode):
    """Which MODEL SET a mode loads. Finer than kind_of: H3 and WAN are both KIND 'video' but
    they evict each other on one 3090, so the estimate's load term keys on THIS."""
    m = MODES.get(mode) or {}
    if m.get("wan"):
        return "wan"
    if m.get("ltx"):
        return "ltx"
    if m.get("animate2"):
        return "animate2"
    if m.get("song"):
        # YuE2 is its own model set: it evicts H3/WAN/Qwen on one 3090 exactly like they
        # evict each other, so the estimate's load term must key on it rather than on "qwen".
        return "yue2"
    if m.get("video"):
        return "h3"
    return "qwen"


# ---- CARD MODE helpers: Solo / Turbo / Studio ---------------------------------------------
# ONE three-way switch, because Turbo (a 2-card Raylight render) and Studio (the writer kept
# resident on card 2) are mutually exclusive on the same 3090. As two independent switches they
# could be set to a state the rig cannot honour, and the failure was quiet.

def migrate_card_mode(value):
    """The OLD 1/2 CARDS toggle -> the three-way names. Unknown -> Solo (the default).

    '1' -> Solo, '2' -> Turbo, 'Studio' stays Studio, anything else -> Solo.
    """
    if value in ("2", "Turbo"):
        return "Turbo"
    if value == "Studio":
        return "Studio"
    return "Solo"


def turbo_preflight(director, wait_s=10.0, poll_s=0.5, free_card=True):
    """Guard a 2-card (Turbo) H3 render against anything resident on GPU1.

    Ollama on forge is pinned to GPU1 (~19 GB per model); a Raylight H3 render needs ~21 GB on
    that same card, so ANY resident LLM there is an OOM.

    free_card=True (the FREE CARD 2 setting, default ON): unload EVERYTHING resident, then
    re-check. ⚠️ Not just the Director's own model — a second LLM left behind is exactly as
    much of an OOM, and unloading one name and then refusing is the failure that wastes a run.
    free_card=False: never touch the writer; refuse if anything is resident, and say which
    setting would free it.
    If the Director is unreachable it ALLOWS — Ollama down means nothing is on card 2.

    Returns (ok: bool, message: str). Pure relative to Tk (it only talks to the Director passed
    in) so tools/cards_matrix.py can drive it with a fake. NEVER call on the Tk main thread: the
    unload + re-check blocks.
    """
    try:
        st = director.status()
    except Exception as exc:
        return True, ("writer status unknown (%s) - Ollama down means nothing is on card 2"
                      % type(exc).__name__)
    if not st.get("reachable"):
        return True, "writer unreachable (Ollama down) - nothing is on card 2, running Turbo"
    resident = list(st.get("loaded") or [])
    if not resident:
        return True, "card 2 is clear - nothing resident"
    if not free_card:
        return False, ("%d model(s) resident on card 2 (%s) and FREE CARD 2 is OFF - turn it "
                       "on to unload them for a 2-card run, or pick Solo."
                       % (len(resident), ", ".join(resident[:3])))
    freed = []
    try:
        if hasattr(director, "unload_all"):
            freed = list(director.unload_all() or [])
        else:
            director.unload()
            freed = [getattr(director, "model", "the writer")]
    except Exception:
        pass
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            left = list((director.status() or {}).get("loaded") or [])
        except Exception:
            return True, "writer unreachable after unload - assuming card 2 is free"
        if not left:
            return True, ("unloaded from card 2 (%s) - Turbo can use both cards"
                          % (", ".join(freed[:3]) if freed else "writer"))
        time.sleep(poll_s)
    return False, ("%d model(s) still resident on card 2 after %.0fs - refusing Turbo "
                   "(a 2-card render needs ~21 GB there). Close the writer or pick Solo."
                   % (len(left), wait_s))


def director_polish(text, mode="t2i", video_mode=None):
    """Rewrite a prompt with DIRECTOR (the local LLM) in the CURRENT engine's dialect.

    Studio mode routes POLISH PROMPT here instead of the Qwen enhancer. The engine is resolved
    with engine_of() (never the broad video flag) so the dialect matches the graph that will run.
    """
    from director import Director
    return Director().polish(text, engine_of(mode), video_mode)
VIDEO_SIZE_NOTES = {
    "Native 1344×768": "H3's native canvas — what every shipped template uses. "
                       "Measured: 212s for a 3s clip (12 steps, 73 frames), 23.8 GB peak.",
    "480p 832×480":    "0.40 MP/frame. Measured 61s for the same clip — 3.5x faster than "
                       "native. The one to iterate at, then upscale.",
    "720p 1280×704":   "0.90 MP/frame. Measured 176s — close to native's cost. 720 is off "
                       "the /32 grid, so 704.",
    "1080p 1920×1088": "2.09 MP/frame. Measured 721s — 3.4x native's time for 2x the "
                       "pixels. It RUNS (fits at ~97% VRAM) but ComfyUI offloads heavily "
                       "and the card is tied up meanwhile. 1080 is off the /32 grid, so 1088.",
    "2K 2560×1440":    "3.69 MP/frame. Measured 1905s — 31m 44s. It COMPLETES, but that is "
                       "2.6x the 1080p time and 31x a 480p draft. Final shot only.",
}
# WAN has only been timed at 832x480, and H3's cost curve is super-linear in pixels — so
# these say "untimed" rather than quoting H3's numbers as if they were WAN's.
WAN_SIZE_NOTES = {
    "Native 1344×768": "Untimed on WAN — every measured WAN run so far is 832×480.",
    "480p 832×480":    "The only WAN size measured: 106s turbo / 943s full for a 5s clip "
                       "(81 frames, one 3090). Iterate here, then PROMOTE.",
    "640×640 (s2v)":   "S2V's own canvas, and the only size it has been timed at: 112s for "
                       "one 4.81s chunk, 348s for three (both incl. load). Untimed at any "
                       "other size — do not assume it scales like WAN or H3.",
    "720p 1280×704":   "Untimed on WAN. H3 is super-linear with pixel count; do not assume.",
    "1080p 1920×1088": "Untimed on WAN, and heavier than H3's 721s. Draft at 480p and use "
                       "PROMOTE — that keeps the motion you approved.",
    "2K 2560×1440":    "Untimed on WAN. H3 took 31m 44s here; assume WAN is at least that.",
}

# Aspect ratios, matching the ones ComfyUI's own ResolutionSelector offers.
# Shape only matters for text-to-image: in the edit modes the canvas follows the
# reference image, which is why this control greys out there.
ASPECTS = {
    "1:1 Square": 1.0,
    "3:2 Photo": 1.5,
    "4:3 Standard": 4.0 / 3.0,
    "16:9 Widescreen": 16.0 / 9.0,
    "9:16 Portrait": 9.0 / 16.0,
    "21:9 Ultrawide": 21.0 / 9.0,
}


# ------------------------------------------------------------- H3 video (forge)
# Lifted from successful H3 runs on this rig, then adapted: the UNET swaps per
# variant and the sampler/output tail is shared. Three things bite here — the
# frame-count rule, the DOTTED reference autogrow key, and a 3-minute model load.
H3_UNET = {"r2v": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
           "i2v": "minimax_h3_fl2va_pruned_int8_convrot.safetensors"}
H3_TURBO = {"r2v": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
            "i2v": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"}
H3_CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
H3_VAE = "minimax_h3_video_vae_int8_convrot.safetensors"
H3_AVAE = "minimax_h3_audio_vae_fp32.safetensors"
# H3 LOAD + cost curve. On one 3090 the engine model sets EVICT each other, so a load happens
# whenever the engine CHANGES — but the cost of that is a re-stage from system RAM, not a disk
# read, once the weights have been touched. Measured: H3's four model loads after a WAN run
# cost ~11 s of the 46.28 s total run. H3_COLD_DISK_S (~3 min) applies only to the FIRST run
# after a reboot (or after memory pressure evicts the page cache) and is therefore NOT baked
# into the estimate — putting it in would make every engine switch 3x too high, which is the
# exact bug this replaced.
H3_SWITCH_S = 12.0       # re-stage after another engine used the card (MEASURED ~11 s)
H3_COLD_DISK_S = 180.0   # first run after a reboot — documented, deliberately not used
# ---- H3 video cost law — RE-MEASURED 2026-10-02 ON FORGE -------------------------------
#   T = H3_JOB_LOAD_S                                  fixed per-job load, WARM
#     + steps x frames x (frames/H3_FRAME_REF)**exp     frame-count penalty
#                      x h3_per_frame_step(mp)          canvas power law
#     + H3_SWITCH_S if the engine changed
#
# Fitted to FIFTEEN points measured back-to-back in ONE session (1 card; 4-step turbo LoRA
# unless the label says otherwise). Worst error 7.3%, mean 4.1%. For scale: two runs of the
# SAME config differed by 3.5%, so this sits on the measurement noise floor.
#   124fr/4st  832x480  48.6 | 1280x704 157.9 | 1344x768 194.8 | 1920x1088 643.0
#   124fr/4st  2560x1440 1809.3 | 1344x768 (2nd run) 188.2
#   181fr/4st  1344x768 400.9 | 1280x704 315.4
#   241fr/4st  1344x768 603.9 | 1280x704 477.5
#   362fr/4st  1344x768 1236.8 | 124fr/1st 1344x768  72.9
#   73fr/12st  1344x768 238.2 | 124fr/12st 1344x768 537.8 | 241fr/12st 1344x768 1717.0
#
# ⚠️ WHAT THE PREVIOUS LAW GOT WRONG — it quoted worst 42%, mean 25% against those points:
#   1. NO LOAD TERM AT ALL. The per-job load was baked into a per-frame-step constant fitted
#      at 73-frame clips, so the number read as sampling-only and ran up to 42% low (worst at
#      1-step and large canvases, where the load dominates). The 12 s switch term is NOT a
#      substitute: it only applies when the engine changes, and the load is paid every run.
#   2. THE FRAME TERM WAS CLAMPED at 1.0 for clips <= 124 frames, which forced the frame
#      penalty to zero over the exact range the old fit had no data for. It does engage
#      below 124 (measured: 73fr/12st native 238.2 s vs 124fr/12st 537.8 s).
#   3. THE FRAME EXPONENT WAS MADE CANVAS-DEPENDENT (k linear in megapixels, 0.632 -> 1.217).
#      Re-measured it is ~0.80 at EVERY canvas; that slope was absorbing the missing load.
# ⚠️ The previous "measured" long-clip rows were STALE, not wrong in principle. Re-measured
#   in one session: 73fr/12st native 238.2 s (recorded 212.0) and 241fr/12st native 1717.0 s
#   (recorded 1571.0) — both ~9-12% low. That is why refitting to them never reconciled.
H3_KNEE_MP = 1.032        # the native 1344x768 canvas — where the two branches meet
H3_SEC_PER_FRAME_STEP_AT_KNEE = 0.36093   # MEASURED s/frame-step at native
H3_MP_EXP_LOW = 1.92      # MEASURED, below native
H3_MP_EXP_HIGH = 1.76     # MEASURED, above native
H3_JOB_LOAD_S = 23.0      # MEASURED per-job load, paid on EVERY run (warm engine)
# Turbo vs base: MEASURED EQUAL per step — 124fr at native is 0.3610 s/frame-step on the
# 4-step turbo path and 0.3614 s/frame-step on the 12-step base path. So the 4-step LoRA
# buys its speed by needing fewer steps, not cheaper ones.
H3_TURBO_FACTOR = 1.0
# ⚠️ EXCLUDED data point, kept so nobody re-fits it: the old steps-ladder recorded "4 steps
# turbo = 10 s warm" at native. Impossible against a clean measurement at a SMALLER canvas
# (it implies 0.034 s/frame-step), and treated as a cache hit rather than fitted.

# ---- RAYLIGHT (2-card) modifier ---------------------------------------------------------
# Raylight is an EXECUTION LAYER over the same H3 graph, not a second cost law. The 2-card
# estimate reuses the SAME `sampling` expression as the 1-card one and only changes the
# load term and divides by the measured speedup:
#     1 card:  H3_JOB_LOAD_S + sampling
#     2 cards: RAY_JOB_LOAD_S + sampling / RAY_SPEEDUP
#
# Measured 2026-10-02, same session and config as the 1-card sweeps above (native 1344x768,
# 4 steps + turbo; only gpus=1 -> 2 changed), 1 card vs 2 cards:
#     124fr/1st   72.9 ->  63.8     124fr/4st  188.2 -> 121.5
#     241fr/4st  603.9 -> 351.8     362fr/4st 1236.8 -> 691.5
#     720p 124fr/4st  157.9 -> 106.3
# Worst error 5.3%, mean 4.4%.
#
# ⚠️ THE SPEEDUP IS NOT A CONSTANT AND 1.995 IS A COMPROMISE. The raw ratios climb with
# clip length -- 1.14x at 1 step, 1.55x at 124 frames, 1.72x at 241, 1.79x at 362 -- because
# the 2-card load is fixed and a longer run amortises it. The per-point gain therefore wobbles
# between 1.73 and 2.14 and no single number is exact for all of them. If a quote for a long
# clip comes out a little optimistic (measured 691.5 s at 362fr, quoted ~655 s), this is why,
# and it is the honest limitation of modelling it as one number.
#
# ⚠️ A 2-card run's FIRST job pays the Ray cluster init and later ones do not, so the 1-step
# probe above is the one measurement of its kind on a cold cluster. It is fitted as-is.
#
# ⚠️ The engine-SWITCH term is assumed to be the same 12 s as the 1-card path. That is NOT
# measured for Raylight -- every run above was back-to-back, so all were warm. Treat the
# Raylight number after a WAN run as unverified.
#
# Memory note: Raylight used to leave ~20.8 GB PER CARD pinned at idle, which would OOM the
# next run. That is fixed (h3_graph sets clear_vram_after_sampling=True on this path); do not
# flip it back. See the RAYLIGHT section in SKILL.md.
RAY_JOB_LOAD_S = 38.0     # MEASURED per-job load on the 2-card path (1 card: 23.0 s)
RAY_SPEEDUP = 1.995       # MEASURED, mean over the ladder above -- see the caveat
# ---- estimate curves for the image model ------------------------------------------------
# t = 11.30 x MP^1.388 x (steps/25), least-squares over all four measured image points:
#   1.049MP -> 12.1 (measured 12.0) · 2.842 -> 48.2 (49.5) · 3.674 -> 68.8 (67.4)
#   · 4.194 -> 82.6 (82.5).  Max error 2.6%, and it fixes the old linear fit's ~45%
#   over-prediction at 1 MP.
QWEN_SEC_AT_1MP_25ST = 11.30
QWEN_MP_EXP = 1.388
QWEN_REF_STEPS = 25
QWEN_LOAD_S = 6.0              # the 18.0-vs-12.0 s cold penalty


def h3_per_frame_step(mp):
    """Seconds per frame-step at this canvas size. Two-regime power law — see above."""
    mp = max(mp, 0.01)
    e = H3_MP_EXP_LOW if mp <= H3_KNEE_MP else H3_MP_EXP_HIGH
    return H3_SEC_PER_FRAME_STEP_AT_KNEE * ((mp / H3_KNEE_MP) ** e)


# ---- the FRAME-COUNT term ---------------------------------------------------------------
# The canvas law above is a SHORT-CLIP law. Its six fitted points are all 73 or 124 frames,
# and it is accurate there (~1%) — but cost per frame-step is NOT flat in frame count, so on
# a real long scene it is badly low. Measured 2026-09-27 on a 241-frame scene, 12 steps, both
# confirmed against ComfyUI's own "Prompt executed in" line:
#     832x480   0.399 MP:  24.62 s/it ->  312.7 s   | law says 206 s -> 1.52x LOW
#     1344x768  1.032 MP: 128.20 s/it -> 1571.0 s   | law says 700 s -> 2.24x LOW
# So the penalty grows with BOTH frames and canvas:
#     M(frames, mp) = max(1, (frames / H3_FRAME_REF) ** k(mp)),  k linear in megapixels
# pinned to those two measured long points:  k(0.399) = 0.632,  k(1.032) = 1.217.
# It reproduces EVERY measured H3 point, short and long, within ~2%:
#   0.399/73 1.00 (61 s) · 0.399/124 1.00 (106.4) · 0.399/241 1.52 (312.7) · 0.901/73 1.00 (176)
#   · 1.032/73 1.00 (212) · 1.032/241 2.25 (1571) · 2.089/73 1.00 (721) · 3.686/73 1.00 (1905)
# ⚠️ Six of those eight have M clamped to 1: the term only engages ABOVE H3_FRAME_REF frames,
# which is exactly why the old law looked perfect on the data it was fitted to.
H3_FRAME_REF = 124                       # reference clip length — the term is 1.0 here
H3_FRAME_EXP = 0.80                      # MEASURED — constant, NOT canvas-dependent


def h3_frame_exp(mp):
    """Kept because callers and tests refer to it. The frame penalty measured as the same
    ~0.80 exponent at EVERY canvas, so this no longer varies with megapixels. It previously
    ramped 0.632 -> 1.217 across the canvas range, and that slope was compensating for the
    missing load term rather than describing a real effect."""
    return H3_FRAME_EXP


def h3_frame_factor(frames, mp):
    """Frame-count multiplier. 1.0 at H3_FRAME_REF frames.

    ⚠️ NOT clamped at 1.0 any more. The previous law clamped it (`max(1.0, ...)`), which
    forced the frame penalty to zero for every clip at or below 124 frames — the exact
    range its fit had no data for. That clamp was the root cause of the up-to-30% under-
    quote on short clips. Measured: 73fr/12st native 238.2 s against 124fr/12st 537.8 s,
    so the penalty is very much engaged below 124.
    """
    return (max(int(frames), 1) / float(H3_FRAME_REF)) ** H3_FRAME_EXP


def h3_estimate_extrapolated(length, mp):
    """True when a run sits OUTSIDE what has actually been measured on this rig — longer than
    124 frames, or a bigger canvas than the native 1344x768. The estimate is still shown, but
    it is a FLOOR rather than a fit, and the UI must say so: the frame term is pinned by long
    clips at only 0.399 and 1.032 MP, and the canvas law above 1.032 MP has only ever been
    measured at 73 frames. Both directions are extrapolation."""
    return int(length) > H3_FRAME_REF or mp > H3_KNEE_MP + 1e-9


def h3_frames(seconds):
    """H3 only accepts frame counts of the form n*17 + 5. The official template
    snaps as max(5, round(a*24)) + (5 - (that % 17)) % 17 — copy it exactly, or
    the clip comes back a different length than you asked for."""
    f = max(5, round(float(seconds) * 24))
    return f + (5 - (f % 17)) % 17


def h3_graph(mode, prompt, refs, length, width, height, steps, seed,
             lora=None, lora_strength=1.0, audio=True, last_frame=None, turbo=True,
              guide_audio=None, guide_frame_idx=0, gpus=1):
    """H3 video graph. mode 'r2v' = reference images -> video (pins a character; up to
    H3_MAX_REFS stills); 'i2v' = first/last frame -> video. Both share the sampler and
    mux tail. `refs` is an ORDERED list of already-uploaded filenames."""
    rl = [r for r in (refs or []) if r]
    g = {
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE}},
        "avae": {"class_type": "VAELoader", "inputs": {"vae_name": H3_AVAE}},
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": H3_CLIP, "type": "minimax", "device": "default"}},
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": H3_UNET[mode], "weight_dtype": "default"}},
        "sel": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sched": {"class_type": "BasicScheduler",
                  "inputs": {"scheduler": "simple", "steps": steps, "denoise": 1.0,
                             "model": ["unet", 0]}},
        "guider": {"class_type": "BasicGuider",
                   "inputs": {"model": ["unet", 0], "conditioning": ["cond", 0]}},
        "samp": {"class_type": "SamplerCustomAdvanced",
                 "inputs": {"noise": ["noise", 0], "guider": ["guider", 0],
                            "sampler": ["sel", 0], "sigmas": ["sched", 0],
                            "latent_image": ["cond", 1]}},
        "vdec": {"class_type": "VAEDecode",
                 "inputs": {"samples": ["samp", 0], "vae": ["vae", 0]}},
        "mkvid": {"class_type": "CreateVideo",
                  "inputs": {"fps": 24.0, "images": ["vdec", 0], "bit_depth": 8,
                             "color_space": "sRGB", "codec": "none"}},
        "save": {"class_type": "SaveVideo",
                 "inputs": {"video": ["mkvid", 0], "filename_prefix": "video/forge_lab",
                            "format": "auto", "format.codec": "auto", "codec": "auto"}},
    }
    if audio:
        g["adec"] = {"class_type": "VAEDecodeAudio",
                     "inputs": {"samples": ["samp", 0], "vae": ["avae", 0]}}
        g["mkvid"]["inputs"]["audio"] = ["adec", 0]

    # ---- AUDIO ANCHOR (the lip-sync path) ------------------------------------------
    # MiniMaxH3AddGuide's own description: "Anchor an image, a short clip, audio, or a clip
    # with its soundtrack at any frame of a MiniMax H3 video." Measured 2026-09-26 on this
    # rig at the SAME SEED: with the guide vs without it, 124 of 124 frames differed
    # (mean |diff| 21.3/255) and H3's own generated audio was uncorrelated (-0.005). So the
    # guide genuinely steers the generation — and by ear the guided + LoRA clip follows the
    # track while the unguided control does not.
    # The guide REPLACES the guider's conditioning; nothing else in the graph moves.
    if guide_audio:
        g["aud"] = {"class_type": "LoadAudio", "inputs": {"audio": guide_audio}}
        g["guide"] = {"class_type": "MiniMaxH3AddGuide",
                      "inputs": {"positive": g["guider"]["inputs"]["conditioning"],
                                 "latent": ["cond", 1],
                                 "frame_idx": int(guide_frame_idx),
                                 "vae": ["vae", 0],
                                 "audio_vae": ["avae", 0],
                                 "audio": ["aud", 0]}}
        g["guider"]["inputs"]["conditioning"] = ["guide", 0]
    # Model chain: UNET -> [turbo] -> [character LoRA].
    # ALWAYS repoint sched/guider at the end of the chain. This must NOT sit behind
    # an `if lora or turbo` guard: with no LoRA and steps > 4 (turbo off) that guard
    # is false, so the chain never resolved and sched/guider kept pointing at a node
    # that was never created — forge answered with a bare HTTP 400 "KeyError 'chan'".
    src = "unet"
    if turbo:
        # The turbo LoRA is a 4-STEP distill — it belongs only at 4 steps. At higher
        # step counts run the base model instead (mirrors the official template), or
        # the speed patch fights the extra steps and degrades the result.
        g["chan"] = {"class_type": "LoraLoaderModelOnly",
                     "inputs": {"lora_name": H3_TURBO[mode], "strength_model": 1.0,
                                "model": [src, 0]}}
        src = "chan"
    if lora:
        g["chan2"] = {"class_type": "LoraLoaderModelOnly",
                      "inputs": {"lora_name": lora,
                                 "strength_model": float(lora_strength),
                                 "model": [src, 0]}}
        src = "chan2"
    g["sched"]["inputs"]["model"] = [src, 0]
    g["guider"]["inputs"]["model"] = [src, 0]
    if mode == "r2v":
        g["cond"] = {"class_type": "MiniMaxH3ReferenceToVideo",
                     "inputs": {"clip": ["clip", 0], "prompt": prompt,
                                "width": width, "height": height, "length": length,
                                "ref_image_size": "match", "vae": ["vae", 0],
                                "audio_vae": ["avae", 0]}}
        if rl:
            # up to 9 stills (the server's own cap on ref_images), each on a DOTTED
            # autogrow key: a nested dict validates and then gets dropped, silently
            # ignoring the reference
            for i, rn in enumerate(rl[:H3_MAX_REFS], start=1):
                nid = "load%d" % i
                g[nid] = {"class_type": "LoadImage", "inputs": {"image": rn}}
                g["cond"]["inputs"]["ref_images.ref_image_%d" % i] = [nid, 0]
    else:
        # fl2va: first_frame and last_frame are BOTH optional, which is what makes
        # text-only (neither) and first+last (both) the same node as plain i2v.
        g["cond"] = {"class_type": "MiniMaxH3ImageToVideo",
                     "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": prompt,
                                "width": width, "height": height, "length": length}}
        if rl:
            g["load1"] = {"class_type": "LoadImage", "inputs": {"image": rl[0]}}
            g["cond"]["inputs"]["first_frame"] = ["load1", 0]
        if last_frame:
            g["load2"] = {"class_type": "LoadImage", "inputs": {"image": last_frame}}
            g["cond"]["inputs"]["last_frame"] = ["load2", 0]
    if int(gpus or 1) >= 2:
        h3_to_raylight(g, mode, gpus)
    return g


# ---- RAYLIGHT: the same H3 graph, split across BOTH cards (USP) ----------------------
# Raylight is an EXECUTION layer, not a model. The weights, encoder, VAEs, LoRAs, refs
# and prompt are IDENTICAL to the standard path — only the node CHAIN changes. So this
# is a TRANSFORM over the graph h3_graph() has already built, never a second definition
# of it: a parallel graph builder is how the cost numbers in this file have drifted before.
#
# MEASURED on forge (2x RTX 3090, 2026-10-01), 1344x768 x 362 frames, 8 steps, one seed:
#   1 GPU 2206.7 s  ->  2 GPUs 1301.4 s  =  1.75x on a real 15 s clip.
#
# ⚠️ THREE THINGS ABOUT THIS THAT ARE NOT OBVIOUS, all measured on this rig:
#   1. The gain DEPENDS ON CLIP LENGTH — ~1.40x at 124 frames, 1.75x at 362, because
#      ~60 s of model load cannot be parallelised. At WAN/LTX sequence sizes this is
#      likely a LOSS, not a win. Never quote the ratio without the clip length.
#   2. The OUTPUT DIFFERS from a 1-GPU render of the SAME seed (22.0 dB PSNR, against
#      19.1 dB for a different text encoder and 8.8 dB for a different prompt). A take
#      cannot be reproduced on a different card count. This is NOT a transparent speed
#      switch, and the UI must not present it as one.
#   3. The per-step gain (~1.73x) is NOT the wall-clock gain — always quote end-to-end.
RAY_ATTENTION = "TORCH_FLASH"     # verified working on Ampere (sm86) on this rig
RAY_LOAD_2GPU_S = 62.7            # measured fixed load, 2 ranks (1 GPU: 54.6)
RAY_STEP_GAIN = 1.73              # measured per-step ratio at 124 frames


def ray_initializer(gpus=2):
    """RayInitializer inputs: `gpus` Ray workers, with USP split across all of them.

    ulysses_degree splits the SEQUENCE across ranks; each rank still holds the FULL
    weights. FSDP is what would shard them and it is OFF here deliberately — it needs
    the cu12 NCCL that clobbers a cu13 torch's libnccl.so.2 (see the skill), and USP
    alone carries H3 at this canvas.
    """
    n = max(1, int(gpus or 1))
    return {"ray_cluster_address": "local", "ray_cluster_namespace": "default",
            "GPU": n, "ulysses_degree": n, "ring_degree": 1, "cfg_degree": 1,
            "dp_degree": 1, "sync_ulysses": False,
            # ⚠️ TRUE, and it is not optional in an APP that shares the card with
            # later runs. Left False (Raylight's own example default — it assumes a
            # long-lived dedicated worker), the Ray actors survive the job holding
            # ~20.8 GB PER CARD at idle: measured after one 2-GPU render, both cards
            # sat at 22.8/21.1 GB and 33/22 W with six ray::IDLE workers still alive.
            # That does not fail three runs later — it fails the NEXT run. Only a
            # ComfyUI restart released it. The cost of True is re-loading the model
            # on the following run, which is the honest trade.
            "clear_vram_after_sampling": True,
            "FSDP": False, "FSDP_CPU_OFFLOAD": False, "XFuser_attention": RAY_ATTENTION,
            "skip_comm_test": False, "use_mmap": False}


def h3_to_raylight(g, mode, gpus=2):
    """Rewrite a BUILT H3 graph into its Raylight (multi-GPU USP) form. In place; returns g.

    Exactly six node types differ. VAE, audio VAE, CLIP, the conditioning node, both
    decodes, CreateVideo and SaveVideo are NOT touched — which is why this is a transform
    and not a second graph.
    """
    g["init"] = {"class_type": "RayInitializer", "inputs": ray_initializer(gpus)}

    # --- LoRAs. The standard chain is unet -> chan(turbo) -> chan2(character) carried on a
    # `model` edge. Raylight carries them on the LOADER as a RAY_LORA chain instead, so
    # detach them from the model edges and re-link in the SAME ORDER via prev_ray_lora.
    prev = None
    for nid in ("chan", "chan2"):
        n = g.get(nid)
        if not n:
            continue
        inp = dict(n.get("inputs") or {})
        inp.pop("model", None)                 # the standard model edge — now unused
        n["class_type"] = "RayLoraLoader"
        n["inputs"] = inp
        if prev:
            n["inputs"]["prev_ray_lora"] = [prev, 0]
        prev = nid

    # --- the loader: UNETLoader -> RayUNETLoader, with the LoRA chain hanging off it
    ui = dict(g["unet"]["inputs"])
    g["unet"] = {"class_type": "RayUNETLoader",
                 "inputs": {"unet_name": ui.get("unet_name"),
                            "weight_dtype": ui.get("weight_dtype", "default"),
                            "ray_actors_init": ["init", 0]}}
    if prev:
        g["unet"]["inputs"]["lora"] = [prev, 0]

    # --- scheduler + guider now key on `ray_actors` (the loader), not on a `model` edge
    si = dict(g["sched"]["inputs"])
    g["sched"] = {"class_type": "RayBasicScheduler",
                  "inputs": {"ray_actors": ["unet", 0],
                             "scheduler": si.get("scheduler", "simple"),
                             "steps": si.get("steps", 8),
                             "denoise": si.get("denoise", 1.0)}}
    gi = dict(g["guider"]["inputs"])
    g["guider"] = {"class_type": "RayBasicGuider",
                   "inputs": {"ray_actors": ["unet", 0],
                              "conditioning": gi.get("conditioning")}}

    # --- the sampler. XFuserSamplerCustomAdvanced takes the SEED directly, so RandomNoise
    # is dropped rather than left dangling. `conditioning` may already be the audio
    # guide's output (MiniMaxH3AddGuide) — it is carried across untouched.
    seed = ((g.pop("noise", None) or {}).get("inputs") or {}).get("noise_seed", 0)
    sa = dict(g["samp"]["inputs"])
    g["samp"] = {"class_type": "XFuserSamplerCustomAdvanced",
                 "inputs": {"add_noise": True, "noise_seed": seed,
                            "guider": sa.get("guider"), "sampler": sa.get("sampler"),
                            "sigmas": sa.get("sigmas"),
                            "latent_image": sa.get("latent_image")}}
    return g

# --------------------------------------------------------------------- helpers
def _probe(url):
    try:
        req = urllib.request.Request(url.rstrip("/") + "/system_stats",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return b"comfyui_version" in r.read(4000)
    except Exception:
        return False


def resolve_forge(force=False):
    """Never trust mDNS: .local fails inside long-running Mac processes (and needs
    Local Network permission), so probe for the rig and remember the winner."""
    global FORGE, F_HOST, F_PORT, _ok
    if _ok and not force:
        return FORGE
    order = list(CANDIDATES)
    try:
        last = open(HOSTFILE).read().strip()
        if last and last in order:
            order.remove(last)
        if last:
            order.insert(0, last)
    except Exception:
        pass
    for url in order:
        if _probe(url):
            FORGE = url.rstrip("/")
            pr = urllib.parse.urlparse(FORGE)
            F_HOST, F_PORT, _ok = pr.hostname or "forge", pr.port or 8188, True
            try:
                open(HOSTFILE, "w").write(FORGE)
            except Exception:
                pass
            return FORGE
    _ok = False
    return FORGE


def get_json(path, timeout=15):
    m = _ensure()
    with urllib.request.urlopen(FORGE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _ensure():
    if not _ok:
        resolve_forge(force=True)


def post_json(path, obj, timeout=60):
    _ensure()
    req = urllib.request.Request(FORGE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # ⚠️ ComfyUI answers /interrupt and POST /queue with 200 and an EMPTY body —
            # that is success, not a parse error (json.loads("") raised on every INTERRUPT).
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        # forge puts the actual reason in the BODY. Discarding it cost a whole
        # debugging round-trip once: the UI showed a bare "HTTP 400 Bad Request"
        # while the body named the node and the exception.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError("forge HTTP %s on %s: %s"
                           % (e.code, path, (body or "(empty body)")[:600])) from None


def get_bytes(path, timeout=180):
    _ensure()
    with urllib.request.urlopen(FORGE + path, timeout=timeout) as r:
        return r.read()


def upload_image(path):
    """Multipart POST to /upload/image — no requests library, no PIL."""
    _ensure()
    name = os.path.basename(path)
    with open(path, "rb") as f:
        blob = f.read()
    boundary = "----ForgeSmith" + base64.b16encode(os.urandom(8)).decode()
    parts = []
    for field, value in (("type", "input"), ("overwrite", "true")):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, field, value)).encode())
    ext = os.path.splitext(name)[1].lower()
    ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp"}.get(ext, "application/octet-stream")
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
                  "Content-Type: %s\r\n\r\n" % (boundary, name, ctype)).encode())
    parts.append(blob)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    body = b"".join(parts)
    req = urllib.request.Request(FORGE + "/upload/image", data=body,
                                 headers={"Content-Type":
                                          "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    sub = out.get("subfolder") or ""
    return (sub + "/" + out["name"]) if sub else out["name"]


def image_size(path):
    """PNG + JPEG dimensions, stdlib only, so the app can show real numbers."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                return w, h
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    if not b:
                        return None
                    if b != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if marker in (b"\xc0", b"\xc1", b"\xc2", b"\xc3", b"\xc5",
                                  b"\xc6", b"\xc7", b"\xc9", b"\xca", b"\xcb"):
                        f.read(3)
                        h, w = struct.unpack(">HH", f.read(4))
                        return w, h
                    ln = struct.unpack(">H", f.read(2))[0]
                    f.seek(ln - 2, 1)
    except Exception:
        return None
    return None


def _png_has_alpha(path):
    """True for an RGBA/GA PNG — read straight out of the IHDR colour type, no decode.

    4 = grey+alpha, 6 = RGBA. Everything else here (RGB, palette, JPEG, GIF) has no alpha
    mask for Tk to choke on. Pure stdlib and ~0.1 ms, so it can sit in front of every load."""
    try:
        if not str(path).lower().endswith(".png"):
            return False
        with open(path, "rb") as f:
            head = f.read(26)
        return bool(len(head) >= 26 and head[:8] == b"\x89PNG\r\n\x1a\n"
                    and head[25] in (4, 6))
    except Exception:
        return False


# ⚠️⚠️ DO NOT call tk.PhotoImage(file=...) on a transparent PNG directly — use thumb_photo().
#
# Tk 9 builds a CoreGraphics REGION out of a PNG's alpha mask (TkpBuildRegionFromAlphaData)
# and on a transparent PNG that region union is PATHOLOGICAL. MEASURED on this machine,
# with a control (identical pixels, alpha flattened away):
#
#     1696x2464 RGB (no alpha) ..................   0.05 s
#     1696x2464 RGBA  (a real `bg` take) ........  > 45 s — NEVER finished, 100% CPU
#     1200x1700 RGBA (synthetic, same shape) ....  > 25 s
#      900x1280 RGBA ...........................  > 20 s
#      700x1000 RGBA ...........................    12.9 s
#      500x700  RGBA ...........................     3.3 s
#      424x616  RGBA ...........................     1.75 s
#      190x276  RGBA ...........................     0.05 s
#
# It is the ALPHA MASK, superlinear in area — NOT the pixel count, which is why the control
# was instant at the same size.
#
# ⚠️ WHY THIS LOOKED LIKE "the app won't launch": the load sits inside a Tk `after` callback,
# so the hang is INSIDE mainloop — no window paints, nothing is logged, the app pegs one core,
# and force-quitting and reopening hangs again on the same take. Takes list + refs list both
# load image files, so ONE transparent-background take (every `remove background` run) was
# enough to brick the whole app on startup.
#
# The fix: shrink it OUT OF PROCESS with `sips` (macOS built-in) and hand Tk only the small
# file. The result is cached in the temp dir, keyed on path+size+mtime+box, so a repeat
# refresh costs nothing.
_THUMB_CACHE = os.path.join(tempfile.gettempdir(), "forge_lab_thumbs")


def thumb_photo(path, box):
    """A tk image for a thumbnail `box` px on its longest side, or None.

    ⚠️ The ONLY safe way to turn a file on disk into a Tk image here — see the block above.
    Returns None rather than raising: a missing thumbnail must never take a window down.
    """
    import tkinter as tk
    try:
        src = str(path)
        if not os.path.exists(src):
            return None
        if _png_has_alpha(src):
            small = _thumb_cache_path(src, box)
            if not os.path.exists(small):
                os.makedirs(_THUMB_CACHE, exist_ok=True)
                subprocess.run(["sips", "--resampleHeightWidthMax", str(int(box)),
                                src, "--out", small],
                               capture_output=True, timeout=60)
            if os.path.exists(small) and os.path.getsize(small) > 0:
                return tk.PhotoImage(file=small)     # already <= box, no subsample needed
        img = tk.PhotoImage(file=src)
        k = max(1, int(round(max(img.width(), img.height()) / float(box))))
        return img.subsample(k, k) if k > 1 else img
    except Exception:
        return None


def _thumb_cache_path(src, box):
    """Deterministic cache name. ⚠️ NOT `hash(tuple)`: str hashing is salted per PROCESS, so
    a hash() key never hits across launches and every start would re-run sips for every take."""
    try:
        st = os.stat(src)
        key = "%s|%d|%d|%d" % (os.path.abspath(src), st.st_size, int(st.st_mtime), int(box))
    except Exception:
        key = "%s|%d" % (os.path.abspath(src), int(box))
    return os.path.join(_THUMB_CACHE,
                        "t" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".png")


def gpu_stats():
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", SSH_TARGET,
             "nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,"
             "power.draw,temperature.gpu --format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6).stdout.strip()
        rows = []
        for line in out.splitlines():
            c = [x.strip() for x in line.split(",")]
            if len(c) >= 6 and c[0].isdigit():
                rows.append({"index": int(c[0]), "util": int(float(c[1])),
                             "used": int(float(c[2])), "total": int(float(c[3])),
                             "power": int(float(c[4])), "temp": int(float(c[5]))})
        return rows or None
    except Exception:
        return None


# ------------------------------------------------------------ engine: graphs
def _seed_for(p):
    seed = p.get("seed", -1)
    return random.randint(0, 2**31 - 1) if seed in (-1, None, "") else int(seed)


def _check_graph(g):
    """Every link must point at a node that EXISTS. forge answers a dangling link
    with an opaque HTTP 400 ("KeyError 'chan'"), which tells you nothing, so catch
    it here and name the offending node and link."""
    bad = []
    for nid, node in g.items():
        for name, val in (node.get("inputs") or {}).items():
            if (isinstance(val, (list, tuple)) and len(val) == 2
                    and isinstance(val[0], str) and val[0] not in g):
                bad.append("%s.%s -> %r" % (nid, name, val[0]))
    if bad:
        raise ValueError("graph has dangling links (node not in graph): "
                         + "; ".join(bad))
    return True


def build_graph(mode, p):
    m = MODES[mode]
    if m.get("song"):
        # YuE2. The graph lives in song.py (Tk-free, stdlib) so this 14k-line file does not
        # grow another engine — and so a headless caller can build the same graph without
        # importing tkinter. ⚠️ The seed is computed ONCE and used for all three nodes: the
        # h3 branch below calls _seed_for(p) twice, and with seed=-1 those are two DIFFERENT
        # random draws, which makes the take label disagree with the render.
        import song as _song
        seed = _seed_for(p)
        style = p.get("style") or p.get("prompt", "")
        secs = float(p.get("max_duration") or _song.YUE2_DEFAULT_SECONDS)
        if p.get("cover"):
            # COVER: the melody is transcribed from a recording and fed to the generator in
            # place of the planner's own score. `audio_name` was uploaded by submit() — the
            # graph cannot be built before that, and saying so here beats a dangling link.
            if not p.get("audio_name"):
                raise ValueError("a cover needs the recording uploaded first — submit() does it")
            g = _song.yue2_cover_graph(p["audio_name"], style, p.get("lyrics", ""),
                                       seconds=secs, seed=seed,
                                       prefix=p.get("prefix") or "covers/yue2")
        else:
            g = _song.yue2_graph(style, p.get("lyrics", ""),
                                 mode=p.get("song_mode") or "full", seconds=secs,
                                 seed=seed, plan=bool(p.get("plan", True)),
                                 prefix=p.get("prefix") or "songs/yue2")
        _check_graph(g)
        return g, seed
    if m.get("wan"):
        # WAN: the variant decides how many stills it takes (i2v 1, flf2v 2, t2v none) and
        # `quality` picks the PATH — steps/shift/cfg/LoRAs all follow from that key.
        vm = WAN_MODES.get(p.get("wan_mode") or WAN_VARIANT_DEFAULT,
                           WAN_MODES[WAN_VARIANT_DEFAULT])
        rl = [r for r in (p.get("ref_names") or []) if r][:vm.get("max", 0)]
        if vm["refs"] == 0:
            rl = []
        seed = _seed_for(p)
        g = wan_graph(vm["node"], p.get("prompt", ""),
                      int(p.get("length") or wan_frames(p.get("seconds", 5.0))),
                      int(p.get("width", 832)), int(p.get("height", 480)),
                      p.get("quality") or WAN_QUALITY_DEFAULT, seed,
                      start_name=(rl[0] if rl else None),
                      last_name=(rl[1] if len(rl) > 1 else None),
                      audio_name=p.get("audio_name"),
                      chunks=int(p.get("s2v_chunks") or 1))
        _check_graph(g)
        return g, seed
    if m.get("animate2"):
        # Animate 2: ref 1 is the CHARACTER still; the driving clip comes from the source picker.
        rl = [r for r in (p.get("ref_names") or []) if r]
        g = animate2_graph(p.get("prompt", ""),
                           int(p.get("width", 480)), int(p.get("height", 848)),
                           _seed_for(p), (rl[0] if rl else None), p.get("drive_name"),
                           length=int(p.get("length") or ANIMATE2_LENGTH),
                           pose_prompt=p.get("pose_prompt"),
                           fps=p.get("fps") or ANIMATE2_FPS)
        return g, _seed_for(p)
    if m.get("ltx"):
        # LTX IC-LoRA: the CONTROL clip is the structure; ref 1 is an optional pinned first frame.
        rl = [r for r in (p.get("ref_names") or []) if r]
        g = ltx_graph(p.get("prompt", ""),
                      int(p.get("width", 1280)), int(p.get("height", 704)),
                      _seed_for(p), p.get("control_name"),
                      first_name=(rl[0] if rl else None),
                      # ⚠️ NOT p["seconds"]: that is H3's DURATION field (default 3 s) and it was
                      # slicing the control clip to 3 s, which is where the 73-frame output came
                      # from. The slice is LTX_SLICE_S and the length follows the CLIP.
                      frames=p.get("src_frames"))
        return g, _seed_for(p)
    if engine_of(mode) == "h3":
        vm = VIDEO_MODES.get(p.get("video_mode") or "r2v", VIDEO_MODES["r2v"])
        rl = [r for r in (p.get("ref_names") or []) if r][:vm.get("max", 1)]
        if vm["refs"] == 0:
            rl = []
        # ONLY flf takes a last frame. Without this gate, handing a second reference
        # to i2v would silently turn it into first+last.
        _last = rl[1] if (vm["refs"] >= 2 and len(rl) > 1) else None
        # h3_graph returns ONLY the graph — do not unpack it as a tuple (that mistake
        # reads as "too many values to unpack (expected 2, got 16)", i.e. the node
        # count, and breaks every video run)
        g = h3_graph(vm["node"], p.get("prompt", ""), rl,
                     int(p.get("length") or h3_frames(p.get("seconds", 3.0))),
                     int(p.get("width", 1344)), int(p.get("height", 768)),
                     int(p.get("steps", 4)), _seed_for(p),
                     lora=p.get("lora") or None,
                     lora_strength=p.get("lora_strength", 1.0),
                     last_frame=_last,
                     audio=bool(p.get("audio", True)),
                     # turbo LoRA = 4-step distill: on at <=4 steps, off above
                     turbo=bool(p.get("turbo", int(p.get("steps", 4)) <= 4)),
                     guide_audio=p.get("guide_audio"),
                     guide_frame_idx=int(p.get("guide_frame_idx", 0)),
                     # RAYLIGHT: 1 card = today's path, 2 = the same graph split
                     # across both (see h3_to_raylight). Default 1 so nothing that
                     # does not opt in changes behaviour.
                     gpus=int(p.get("gpus", 1)))
        _check_graph(g)
        return g, _seed_for(p)
    g = json.loads(json.dumps(m["graph"]))
    seed = p.get("seed", -1)
    seed = random.randint(0, 2**31 - 1) if seed in (-1, None, "") else int(seed)
    prompt = p.get("prompt", "") or m.get("prompt", "")
    g["474" if m["ref"] else "452"]["inputs"]["prompt"] = prompt
    g["474" if m["ref"] else "452"]["inputs"]["negative_prompt"] = p.get("negative", "") or ""
    g["474" if m["ref"] else "452"]["inputs"]["resolution"] = int(p.get("resolution", 1024))
    g["458"]["inputs"]["seed"] = seed
    g["458"]["inputs"]["steps"] = int(p.get("steps", 25))
    g["461"]["inputs"]["filename_prefix"] = p.get("prefix", "forge_lab")
    if m["ref"]:
        # up to 16 references (the encoder's autogrow names image_1..image_16)
        rl = [r for r in (p.get("ref_names") or []) if r][:QWEN_MAX_REFS]
        if not rl:
            # no reference: drop the loader and the autogrow keys rather than submit an
            # empty filename (which fails validation and reads like a broken graph)
            g.pop("470", None)
            for k in [k for k in g["474"]["inputs"] if k.startswith("images.")]:
                g["474"]["inputs"].pop(k)
        for i, rn in enumerate(rl, start=1):
            if i == 1:
                g["470"]["inputs"]["image"] = rn
                nid = "470"
            else:
                nid = "rf%d" % i
                g[nid] = {"class_type": "LoadImage", "inputs": {"image": rn}}
            g["474"]["inputs"]["images.image_%d" % i] = [nid, 0]
        g["456"]["inputs"]["width"] = int(p.get("width", 1024))
        g["456"]["inputs"]["height"] = int(p.get("height", 1024))
        # custom_size ON swaps the canvas to EmptyLatentImage; default keeps image_1's canvas
        g["468"]["inputs"]["switch"] = bool(p.get("custom_size", False))
    else:
        g["456"]["inputs"]["width"] = int(p.get("width", 1024))
        g["456"]["inputs"]["height"] = int(p.get("height", 1024))
    _check_graph(g)
    return g, seed


def estimate_seconds(mode, p, ref_paths=None, warm_engine=None):
    """Seconds, from curves fitted to MEASURED runs on this rig. Labelled an estimate
    everywhere in the UI, and it says whether the engine-load term is included.

    `warm_engine` is the engine of the last run submitted in this session. On one 3090 the
    engine model sets evict each other, so the switch cost applies when the engine changes
    and is dropped when the same engine runs again. Ignoring that is what made a 106 s H3 job
    read as a 320 s estimate.
    """
    eng = engine_of(mode)
    switch = (warm_engine != eng)          # None (unknown/first run) counts as a switch
    if MODES[mode].get("wan"):
        if (p.get("wan_mode") or "") == "s2v":
            # Fitted to TWO measured points (1 chunk 112 s, 3 chunks 348 s, both incl. load).
            # Scaling with canvas is UNTESTED, same caveat as the rest of WAN.
            chunks = max(1, min(S2V_MAX_CHUNKS, int(p.get("s2v_chunks") or 1)))
            mp = int(p.get("width", 640)) * int(p.get("height", 640)) / 1e6
            t = (S2V_FIRST_CHUNK_S + (chunks - 1) * S2V_EXTRA_CHUNK_S) * (mp / S2V_REF_MP)
            return t + (WAN_LOAD_S if switch else 0.0)
        # The step count is part of the quality PATH, not a field.
        length = int(p.get("length") or wan_frames(p.get("seconds", 5.0)))
        mp = int(p.get("width", 832)) * int(p.get("height", 480)) / 1e6
        qk = p.get("quality") if p.get("quality") in WAN_QUALITY else WAN_QUALITY_DEFAULT
        t = (length * WAN_QUALITY[qk]["steps"] * WAN_PER_FRAME_STEP[qk]
             * (mp / WAN_REF_MP))
        return t + (WAN_LOAD_S if switch else 0.0)
    # ⚠️ ORDER MATTERS: ltx and animate2 both carry video=True, so without these branches they
    # would silently fall into H3's curve below and quote a wrong number for a different engine.
    if MODES[mode].get("ltx"):
        # the SAME helper the graph uses — otherwise the estimate describes another render
        frames = ltx_length_for(p.get("src_frames"))
        t = ltx_estimate_seconds(int(p.get("width", 1280)), int(p.get("height", 704)), frames)
        return t + (LTX_SWITCH_S if switch else 0.0)
    if MODES[mode].get("animate2"):
        frames = int(p.get("length") or ANIMATE2_LENGTH)
        t = animate2_estimate_seconds(int(p.get("width", 480)), int(p.get("height", 848)), frames)
        return t + (ANIMATE2_SWITCH_S if switch else 0.0)
    if MODES[mode].get("song"):
        # ⚠️ Before the h3 branch: a song carries no `video` flag so it cannot fall into H3's
        # curve here, but the ORDER of these branches is what the ltx/animate2 comment above
        # warns about, so keep song with them rather than at the end.
        import song as _song
        secs = float(p.get("max_duration") or _song.YUE2_DEFAULT_SECONDS)
        if p.get("cover"):
            # a cover pays the TRANSCRIPTION as well as the generation
            t = _song.yue2_cover_estimate_seconds(secs)
        else:
            t = _song.yue2_estimate_seconds(secs, bool(p.get("plan", True)))
        return t + (YUE2_SWITCH_S if switch else 0.0)
    if engine_of(mode) == "h3":
        length = int(p.get("length") or h3_frames(p.get("seconds", 3.0)))
        steps = int(p.get("steps", 4))
        mp = int(p.get("width", 1344)) * int(p.get("height", 768)) / 1e6
        turbo = bool(p.get("turbo", steps <= 4))       # same rule build_graph uses
        # ONE sampling expression for both paths. Raylight changes the load term and divides
        # the sampling; it does NOT get its own cost law (a parallel formula is how this file
        # has been broken before).
        sampling = (length * steps * h3_per_frame_step(mp) * h3_frame_factor(length, mp)
                    * (H3_TURBO_FACTOR if turbo else 1.0))
        if int(p.get("gpus", 1)) >= 2:
            t = RAY_JOB_LOAD_S + sampling / RAY_SPEEDUP
        else:
            t = H3_JOB_LOAD_S + sampling
        return t + (H3_SWITCH_S if switch else 0.0)
    steps = int(p.get("steps", 25))
    m = MODES[mode]
    if m["ref"] and not p.get("custom_size"):
        res = int(p.get("resolution", 1024))
        mp = (res * res / 1e6) if res else 0.0
        if not res and ref_paths:
            wh = image_size(ref_paths[0])
            if wh:
                mp = wh[0] * wh[1] / 1e6
    else:
        mp = int(p.get("width", 1024)) * int(p.get("height", 1024)) / 1e6
    t = QWEN_SEC_AT_1MP_25ST * (max(mp, 0.05) ** QWEN_MP_EXP) * (
        steps / float(QWEN_REF_STEPS))
    return t + (QWEN_LOAD_S if switch else 0.0)


def estimate_is_switch(mode, warm_engine):
    """True when the estimate above includes an engine switch/re-stage."""
    return engine_of(mode) != warm_engine


def engine_load_s(mode):
    """The load term estimate_seconds() adds when the engine changes. Shown in the UI so the
    number is never a black box."""
    return {"h3": H3_SWITCH_S, "wan": WAN_LOAD_S, "qwen": QWEN_LOAD_S,
            "ltx": LTX_SWITCH_S, "animate2": ANIMATE2_SWITCH_S,
            "yue2": YUE2_SWITCH_S}[engine_of(mode)]


def video_probe(path):
    """(width, height, fps, frames, seconds) for a LOCAL video, via ffprobe — or None.

    Forge Lab needs the source clip's real numbers BEFORE it submits: on the LTX path the latent
    length follows the clip's frame count, and the estimate has to know it. Reading it here at
    pick time is cheaper and more honest than guessing on the rig. Returns None rather than
    raising: a missing ffprobe must degrade to a warning, not break the panel.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                              "-show_entries",
                              "stream=width,height,r_frame_rate,nb_read_frames,duration",
                              "-of", "json", path],
                             capture_output=True, text=True, timeout=30).stdout
        st = (json.loads(out or "{}").get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        num, _, den = (st.get("r_frame_rate") or "0/1").partition("/")
        den = float(den or 1) or 1.0
        fps = float(num or 0) / den
        frames = int(st.get("nb_read_frames") or 0)
        secs = float(st.get("duration") or 0.0)
        if not frames and fps and secs:
            frames = int(round(fps * secs))
        if not (w and h and frames):
            return None
        return {"width": w, "height": h, "fps": fps, "frames": frames, "seconds": secs}
    except Exception:
        return None


def submit(mode, p, ref_paths=None):
    """`ref_paths` is the ORDERED list of local stills — ref 1 is the primary."""
    paths = [x for x in (ref_paths or []) if x]
    if MODES[mode].get("wan"):
        # WAN: i2v takes the FIRST frame, flf2v the first AND last, t2v nothing. Upload in
        # the order added — ref 1 IS the first frame, so order is the wiring.
        vm = WAN_MODES.get(p.get("wan_mode") or WAN_VARIANT_DEFAULT,
                           WAN_MODES[WAN_VARIANT_DEFAULT])
        if vm["refs"] >= 1 and not paths:
            raise ValueError("%s needs a still" % vm["label"])
        if vm["refs"] == 0:
            paths = []
        extra = {}
        if vm.get("audio_file"):
            # S2V drives the performance from this file. Same multipart endpoint as the
            # stills — ComfyUI's /upload/image stores ANY file into the input dir.
            ap = p.get("audio_path")
            if not ap:
                raise ValueError("%s needs an audio file — that file IS the performance"
                                 % vm["label"])
            extra["audio_name"] = upload_image(ap)
        p = dict(p, ref_names=[upload_image(x) for x in paths[:vm.get("max", 0)]], **extra)
    elif MODES[mode].get("ltx"):
        # LTX IC-LoRA: the SOURCE CLIP is the structure. Ref 1 is an OPTIONAL pinned first frame.
        srcv = p.get("source_path")
        if not srcv:
            raise ValueError("LTX IC-LoRA needs a SOURCE VIDEO — that clip IS the structure")
        rl = [x for x in paths if x][:1]
        p = dict(p, ref_names=[upload_image(x) for x in rl],
                 control_name=upload_image(srcv))
    elif MODES[mode].get("animate2"):
        # Animate 2: ref 1 is the CHARACTER still; the SOURCE clip is the driving performance.
        srcv = p.get("source_path")
        if not srcv:
            raise ValueError("Wan Animate 2 needs a DRIVING VIDEO — that clip IS the performance")
        if not paths:
            raise ValueError("Wan Animate 2 needs a REFERENCE STILL — that still IS the character")
        p = dict(p, ref_names=[upload_image(x) for x in paths[:1]],
                 drive_name=upload_image(srcv))
    elif MODES[mode].get("song"):
        # A COVER needs the recording ON THE RIG: that file IS the melody. Same multipart
        # endpoint as the stills — ComfyUI's /upload/image stores ANY file into the input dir.
        if p.get("cover"):
            ap = p.get("audio_path")
            if not ap:
                raise ValueError("a cover needs a SOURCE RECORDING — that file IS the melody")
            if not os.path.exists(ap):
                raise ValueError("the recording to cover is not on disk any more: %s" % ap)
            p = dict(p, audio_name=upload_image(ap))
    elif engine_of(mode) == "h3":
        # the variant decides how many stills it takes (r2v up to 9, flf 2, i2v 1)
        vm = VIDEO_MODES.get(p.get("video_mode") or "r2v", VIDEO_MODES["r2v"])
        if vm["refs"] >= 1 and not paths:
            raise ValueError("%s needs a still" % vm["label"])
        if vm["refs"] == 0:
            paths = []
        extra = {}
        if p.get("guide_audio_path"):
            # the lip-sync track. Same multipart endpoint as stills — /upload/image stores
            # any file into the input dir.
            extra["guide_audio"] = upload_image(p["guide_audio_path"])
        p = dict(p, ref_names=[upload_image(x) for x in paths[:vm.get("max", 1)]], **extra)
    elif MODES[mode]["ref"]:
        if not paths:
            raise ValueError("this mode needs a reference image")
        p = dict(p, ref_names=[upload_image(x) for x in paths[:QWEN_MAX_REFS]])
    graph, seed = build_graph(mode, p)
    r = post_json("/prompt", {"prompt": graph, "client_id": CLIENT_ID})
    pid = r.get("prompt_id")
    if not pid:
        raise RuntimeError("forge did not return a prompt_id: %r" % r)
    meta = {"id": pid, "mode": mode, "label": MODES[mode]["label"],
            "prompt": p.get("prompt", "") or MODES[mode].get("prompt", ""),
            "seed": seed, "prefix": p.get("prefix", "forge_lab"),
            "steps": int(p.get("steps", 25)),
            "resolution": int(p.get("resolution", 1024)),
            "width": int(p.get("width", 1024)), "height": int(p.get("height", 1024)),
            "custom_size": bool(p.get("custom_size", False)),
            "ref": os.path.basename(paths[0]) if paths else None,
            "refs": [os.path.basename(x) for x in paths],
            "started": time.time(), "status": "running", "images": []}
    if MODES[mode].get("ltx") or MODES[mode].get("animate2"):
        # ⚠️ INDEX HONESTY: ltx and animate2 BAKE their step count into the graph (8 and
        # 6) and read it from the REGISTRY, not the STEPS field — so the generic
        # int(p.get("steps", 25)) above recorded 25 for a run that actually sampled 8/6.
        # Log what the graph ran.
        meta["steps"] = int(MODES[mode]["steps"])
        # take-collection matches the RUN's own output by this name (see the S2V sorting note)
        meta["source"] = os.path.basename(p.get("source_path") or "")
        meta["src_frames"] = int(p.get("src_frames") or 0)
    if MODES[mode].get("wan"):
        _vm = WAN_MODES.get(p.get("wan_mode") or WAN_VARIANT_DEFAULT,
                            WAN_MODES[WAN_VARIANT_DEFAULT])
        if _vm.get("node") == "s2v":
            # Audio-DRIVEN. The clip is SILENT — forge's ComfyUI cannot mux AAC, so the
            # performance audio is added on this Mac after the download (see collect()).
            _chunks = max(1, min(S2V_MAX_CHUNKS, int(p.get("s2v_chunks") or 1)))
            _fr = s2v_chunks_to_frames(_chunks)
            meta.update({"video": True, "wan": True, "s2v": True, "chunks": _chunks,
                         "length": _fr, "seconds": round(_fr / S2V_FPS, 2), "fps": S2V_FPS,
                         "audio": False, "quality": S2V_QUALITY, "steps": S2V_STEPS,
                         "lora": S2V_LORA, "wan_mode": "s2v",
                         "audio_name": p.get("audio_name"),
                         "audio_path": p.get("audio_path"),
                         "audio_file": os.path.basename(p.get("audio_path") or "")})
        else:
            # WAN: 16 fps, no audio, and the step count/label come from the quality PATH.
            _len = int(p.get("length") or wan_frames(p.get("seconds", 5.0)))
            _qk = p.get("quality") if p.get("quality") in WAN_QUALITY else WAN_QUALITY_DEFAULT
            meta.update({"video": True, "wan": True, "length": _len,
                         "seconds": round(_len / WAN_FPS, 2), "fps": WAN_FPS,
                         "audio": False, "quality": _qk,
                         "steps": int(WAN_QUALITY[_qk]["steps"]), "lora": None,
                         "wan_mode": _vm["node"]})
        meta["label"] = "WAN 2.2 · %s" % _vm["label"]
    elif engine_of(mode) == "h3":
        _len = int(p.get("length") or h3_frames(p.get("seconds", 3.0)))
        meta.update({"video": True, "length": _len,
                     "seconds": round(_len / 24.0, 2), "fps": 24.0,
                     "audio": bool(p.get("audio", True)),
                     "lora": p.get("lora") or None,
                     "lora_strength": float(p.get("lora_strength", 1.0))})
    if MODES[mode].get("song"):
        # A song is neither an image nor a video: without this the take is described with
        # H3's fields (a song has no frames, no fps and no canvas) and take_specs() prints
        # "25 steps" for a run that sampled 32.
        meta.update({"song": True, "audio": True,
                     "style": p.get("style", ""), "lyrics": p.get("lyrics", ""),
                     "song_mode": p.get("song_mode") or "full",
                     "plan": bool(p.get("plan", True)),
                     "seconds": round(float(p.get("max_duration") or 0.0), 1),
                     "steps": int(MODES[mode]["steps"]),
                     "width": 0, "height": 0})
        meta["prompt"] = p.get("style", "") or ""      # for a song the STYLE is the prompt
        if p.get("cover"):
            # a cover is a different KIND of song and the take list should say so: it has a
            # SOURCE recording whose melody it follows, and a transcribed score beside it.
            meta.update({"cover": True, "song_mode": "melody",
                         "source": os.path.basename(p.get("audio_path") or ""),
                         "source_path": p.get("audio_path")})
            meta["label"] = "Song \u00b7 cover"
    _index_insert(meta)
    return pid, seed


# ---- promote a finished take to a bigger canvas (UPSCALE, not re-render) -----
# Upscaling is the only way to keep an approved draft's exact motion: re-rendering
# at a higher resolution is a different canvas, and therefore a different shot.
# Measured on forge: 720p->1080p upscale 93s cold / 40s warm for a 3s clip, vs 721s
# to render 1080p natively.
PROMOTE_MODEL = "RealESRGAN_x4plus.pth"    # PHOTO. ✅ corrected 2026-09-27: this defaulted to
# the ANIME model, which is the wrong tool for the photoreal pieces made here. Both are on
# disk and both are now offered in the promote dialog — pick per piece, not per app.
PROMOTE_MODELS = {"Photo · RealESRGAN x4plus": "RealESRGAN_x4plus.pth",
                  "Cel/anime · RealESRGAN x4plus anime 6B":
                      "RealESRGAN_x4plus_anime_6B.pth"}
PROMOTE_TARGETS = {
    "1080p 1920×1088": (1920, 1088),
    "2K 2560×1440":    (2560, 1440),
}
PROMOTE_ESTIMATE = {"1080p 1920×1088": 60, "2K 2560×1440": 150}   # estimates, shown as such

# Image takes promote by SCALE, not to fixed pixel targets: a still's sensible
# destination depends on where it started. 2x is a 4x upscale downsampled back to 2x
# (the same sharpening trick), 4x keeps the model's native output.
IMAGE_PROMOTE = {"2x": 2.0, "4x": 4.0}
IMAGE_PROMOTE_ESTIMATE = {"2x": 15, "4x": 20}          # estimates; a still is one frame


def image_promote_graph(src_name, tw, th, model=PROMOTE_MODEL):
    """LoadImage -> 4x ESRGAN -> [downscale to target] -> SaveImage.
    tw/th of 0 means the source size was unreadable, so keep the native 4x."""
    g = {
        "img": {"class_type": "LoadImage", "inputs": {"image": src_name}},
        "up": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "big": {"class_type": "ImageUpscaleWithModel",
                "inputs": {"upscale_model": ["up", 0], "image": ["img", 0]}},
        "save": {"class_type": "SaveImage",
                 "inputs": {"images": ["big", 0], "filename_prefix": "promoted"}},
    }
    if tw and th:
        g["fit"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["big", 0], "upscale_method": "lanczos",
                               "width": int(tw), "height": int(th), "crop": "disabled"}}
        g["save"]["inputs"]["images"] = ["fit", 0]
    _check_graph(g)
    return g


def promote_graph(src_name, tw, th, model=PROMOTE_MODEL):
    """LoadVideo -> frames -> 4x ESRGAN -> downscale to the target -> mux audio back.
    The 4x-then-downscale order is what makes this sharper than a plain resize, and
    the audio/fps come from the source so the result is motion-identical to the draft."""
    g = {
        "video": {"class_type": "LoadVideo", "inputs": {"file": src_name}},
        "parts": {"class_type": "GetVideoComponents", "inputs": {"video": ["video", 0]}},
        "up": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "big": {"class_type": "ImageUpscaleWithModel",
                "inputs": {"upscale_model": ["up", 0], "image": ["parts", 0]}},
        "fit": {"class_type": "ImageScale",
                "inputs": {"image": ["big", 0], "upscale_method": "lanczos",
                           "width": int(tw), "height": int(th), "crop": "disabled"}},
        "vid": {"class_type": "CreateVideo",
                "inputs": {"images": ["fit", 0], "fps": ["parts", 2],
                           "audio": ["parts", 1], "bit_depth": 8,
                           "color_space": "sRGB", "codec": "none"}},
        "save": {"class_type": "SaveVideo",
                 "inputs": {"video": ["vid", 0], "filename_prefix": "video/promoted",
                            "format": "auto", "format.codec": "auto", "codec": "auto"}},
    }
    _check_graph(g)
    return g


# ---- WAN 2.2 14B (second video engine; three variants) ---------------------------
# Shape LIFTED VERBATIM from a successful run on this rig (c8b7eed6, t2v) and, for i2v /
# flf2v, from ComfyUI's own shipped templates (video_wan2_2_14B_i2v / _flf2v) resolved node
# by node — not guessed. The shape is load-bearing: two experts (high/low noise), one
# lightning LoRA per expert, a ModelSamplingSD3 AFTER each LoRA, then a two-stage sampler
# (high-noise 0->half with add_noise=enable + return_with_leftover_noise=enable, low-noise
# half->end with add_noise=disable, seed reused).
#
# THE RULES ARE BAKED INTO THE PATH, not exposed as controls. Every one of these degrades
# the output with NO error if set wrong, which is exactly why WAN is a dropdown of PATHS
# rather than a pile of knobs, and why wan_graph() takes no shift/LoRA/steps argument:
#   * shift       5.0 WITH the lightning LoRAs, 8.0 WITHOUT. Derived from `turbo`.
#   * LoRA pair   per TASK (t2v and i2v ship DIFFERENT lightning LoRAs) and ON only on turbo.
#                 There is NO user LoRA for WAN — character LoRAs are H3 ref2va-only
#                 and cannot load here.
#   * steps/cfg   turbo = 4 steps / cfg 1.0 (+LoRAs); full = 20 steps / cfg >1 (no LoRAs).
#                 cfg 1.0 makes the negative prompt INERT, so the two paths are different
#                 generations, not a speed setting.
#   * the split   high expert 0->half, low expert half->end. Structural.
#   * 16 fps and NO audio — WAN has no audio VAE at all.
WAN_FPS = 16.0
WAN_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE = "wan_2.1_vae.safetensors"
# (high-noise expert, low-noise expert). flf2v runs on the I2V pair — its own template's
# loaders say so; the t2v pair would be the wrong model for it.
WAN_UNET = {
    "t2v":   ("wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
              "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors"),
    "i2v":   ("wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
              "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"),
    "flf2v": ("wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
              "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"),
}
# The 4-step lightning distill, one LoRA per expert, PER TASK.
WAN_TURBO = {
    "t2v":   ("wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors",
              "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors"),
    "i2v":   ("wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
              "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors"),
    "flf2v": ("wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
              "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors"),
}
# t2v keeps its VERIFIED EmptyHunyuanLatentVideo path (a real measured run). i2v/flf2v use the
# fused Wan nodes, which emit the conditioning AND the latent — so the CLIPTextEncode outputs
# feed INTO them. Wiring the samplers straight off CLIPTextEncode there drops the image with
# no error at all.
WAN_NODE = {"t2v": None, "i2v": "WanImageToVideo", "flf2v": "WanFirstLastFrameToVideo"}
# The templates' own negative prompt, kept verbatim (it is what the model was tuned with).
WAN_NEG = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
           "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
           "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走，"
           "裸露，NSFW")
# `cfg_full` differs by variant because the templates differ: both t2v and i2v use 3.5, the
# shipped flf2v template uses 4. Baked per mode so nobody has to remember which.
WAN_MODES = {
    "t2v":   {"label": "Text to video (t2v)", "node": "t2v", "refs": 0, "max": 0,
              "cfg_full": 3.5,
              "note": "WAN 2.2 14B t2v — 16 fps, NO audio. Turbo and Full are different "
                      "models, not a speed slider."},
    "i2v":   {"label": "First frame (i2v)", "node": "i2v", "refs": 1, "max": 1,
              "cfg_full": 3.5,
              "note": "WanImageToVideo — ref 1 IS the first frame. 16 fps, NO audio. The "
                      "canvas comes from SIZE; the still is resized onto it."},
    "flf2v": {"label": "First + last frame (flf2v)", "node": "flf2v", "refs": 2, "max": 2,
              "cfg_full": 4.0,
              "note": "WanFirstLastFrameToVideo — ref 1 = first frame, ref 2 = last frame. "
                      "16 fps, NO audio."},
    # S2V is audio-DRIVEN: `refs` 1 is the character still, and it ALSO needs an audio file
    # (the `audio_file` flag is what makes the UI show that row). One path only.
    "s2v":   {"label": "Song / lip-sync (s2v)", "node": "s2v", "refs": 1, "max": 1,
              "cfg_full": 6.0, "audio_file": True,
              "note": "WanSoundImageToVideo — the character PERFORMS the audio. 16 fps. ONE "
                      "path: 4 steps, cfg 1.0, shift 8, lightning LoRA on (Ryan preferred it "
                      "by eye and it is 9x cheaper than the 20-step path). The audio runs "
                      "CONTINUOUSLY across every chained chunk. Renders silent by design — "
                      "forge's ComfyUI cannot mux AAC, so the audio is added on this Mac."},
}
WAN_VARIANT_DEFAULT = "t2v"
# WAN's quality axis selects a PATH. steps/turbo/shift/cfg all follow from the key — none of
# them is a field, so a path cannot be half-switched.
WAN_QUALITY = {
    "Turbo 4 steps": {"steps": 4, "turbo": True,
                      "note": "4 steps + the lightning LoRAs, cfg 1.0, shift 5.0. The "
                              "negative prompt is INERT at cfg 1.0. Measured 106s for a 5s "
                              "clip at 832x480 (81 frames). The default."},
    "Full 20 steps": {"steps": 20, "turbo": False,
                      "note": "20 steps, no LoRAs, shift 8.0, cfg 3.5 (4.0 on flf2v — the "
                              "templates differ). The negative prompt actually applies. "
                              "Measured 943s (15.7 min) for the same clip: 8.9x turbo, and "
                              "that gap IS the negative prompt, not slack."},
}
WAN_QUALITY_DEFAULT = "Turbo 4 steps"
WAN_SHIFT = {True: 5.0, False: 8.0}        # WITH the lightning LoRA / WITHOUT it
# Cost fit: two measured 832x480 / 81-frame points (turbo 105.7 s, full 942.8 s), both of
# which include the model-load phase, so  t = LOAD + frames x steps x c  with LOAD = 25 s.
# Expressed per frame-step AT WAN_REF_MP (turbo 0.624 x 0.399 = 0.249; full 1.420 x 0.399).
# ⚠️ WAN's per-frame-step is calibrated at ONE canvas. Its scaling with pixel count is
# UNTESTED, so the estimate scales linearly and the UI says "untested at other sizes".
WAN_LOAD_S = 25.0
WAN_REF_MP = 0.399
WAN_PER_FRAME_STEP = {"Turbo 4 steps": 0.249, "Full 20 steps": 0.567}

# ---- WAN 2.2 S2V — audio-DRIVEN video, the lip-sync path ------------------------------
# NOT a variant of the WAN above: a DIFFERENT model (s2v_14B), ONE unet instead of a high/low
# expert pair, a model-only LoRA, and a wav2vec2 audio encoder whose output drives the
# performance. Same WAN family, so engine_of()'s load/switch terms still apply.
#
# The shipped template's own notes are the authority, and they are baked in here:
#   * 77 frames IS the chunk — "This model needs at least 73 frames. If you set the value too
#     high, it might cause an out-of-memory issue. So you can just leave 77 as it is."
#     A longer scene comes from CHAINING extends, never from raising `length`.
#   * steps/cfg: 4 / 1.0 WITH the LoRA, 20 / 6.0 without. Ryan judged the 4-step path better
#     BY EYE and it is 9.0x cheaper, so exactly ONE path is offered.
#   * ModelSamplingSD3 shift 8 — not the 5 the rest of WAN turbo uses. uni_pc + simple.
S2V_UNET = "wan2.2_s2v_14B_fp8_scaled.safetensors"
S2V_AUDIO_ENCODER = "wav2vec2_large_english_fp16.safetensors"
S2V_LORA = "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors"
S2V_FPS = 16.0
S2V_CHUNK = 77
S2V_MIN_FRAMES = 73
S2V_MAX_CHUNKS = 3
S2V_STEPS, S2V_CFG, S2V_SHIFT = 4, 1.0, 8.0
S2V_QUALITY = "4-step (the only path)"
# Cost, MEASURED on forge at 640x640 / 4 steps, each including the ~25 s load:
#   1 chunk (77 fr) = 112 s  ·  3 chunks (237 fr) = 348 s
# Two points, two unknowns -> charge the load ONCE (as elsewhere) and let extra chunks cost
# more than the first, which is what the two numbers actually say:
#   112 - 25 =  87 s for chunk 1  ·  (348 - 112) / 2 = 118 s per extra chunk
S2V_FIRST_CHUNK_S = 87.0
S2V_EXTRA_CHUNK_S = 118.0
S2V_REF_MP = 0.4096                    # 640x640, the template's own canvas


def s2v_chunks_to_frames(chunks):
    """Chained output length. LatentConcat ADDS latents and does NOT dedupe the seam, so the
    naive `chunks x 77` is WRONG: 3 chunks is 237 frames (14.81 s), not 231 (14.44 s).
    L = (77-1)/4 + 1 = 20 latents per chunk; frames = 4*(chunks*L - 1) + 1.
    """
    L = (S2V_CHUNK - 1) // 4 + 1
    return 4 * (int(chunks) * L - 1) + 1


def s2v_length_label(chunks):
    n = int(chunks)
    return "%.2f s  (%d chunk%s)" % (s2v_chunks_to_frames(n) / S2V_FPS, n,
                                     "" if n == 1 else "s")


S2V_LENGTHS = {n: s2v_length_label(n) for n in range(1, S2V_MAX_CHUNKS + 1)}


def s2v_graph(prompt, width, height, chunks, seed, ref_name, audio_name):
    """WAN 2.2 S2V: the character PERFORMS the audio. Built from the shipped
    `video_wan2_2_14B_s2v` template, node for node.

    Baked, NOT settable: 4 steps, cfg 1.0, shift 8, the lightning LoRA on, uni_pc/simple, and
    one 77-frame chunk per unit of `chunks`. There is deliberately no steps/shift/cfg/LoRA
    argument here to get wrong.
    """
    if not ref_name:
        raise ValueError("WAN 2.2 S2V needs a reference still — that still IS the character")
    if not audio_name:
        raise ValueError("WAN 2.2 S2V needs an audio file — that audio IS the performance")
    chunks = max(1, min(S2V_MAX_CHUNKS, int(chunks)))
    g = {
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": WAN_CLIP, "type": "wan"}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0], "text": WAN_NEG}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        "aenc": {"class_type": "AudioEncoderLoader",
                 "inputs": {"audio_encoder_name": S2V_AUDIO_ENCODER}},
        "aud": {"class_type": "LoadAudio", "inputs": {"audio": audio_name}},
        "aeout": {"class_type": "AudioEncoderEncode",
                  "inputs": {"audio_encoder": ["aenc", 0], "audio": ["aud", 0]}},
        "img": {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": S2V_UNET, "weight_dtype": "default"}},
        "lora": {"class_type": "LoraLoaderModelOnly",
                 "inputs": {"model": ["unet", 0], "lora_name": S2V_LORA,
                            "strength_model": 1.0}},
        "shift": {"class_type": "ModelSamplingSD3",
                  "inputs": {"model": ["lora", 0], "shift": S2V_SHIFT}},
        "s2v": {"class_type": "WanSoundImageToVideo",
                "inputs": {"positive": ["pos", 0], "negative": ["neg", 0],
                           "vae": ["vae", 0], "width": int(width),
                           "height": int(height), "length": S2V_CHUNK, "batch_size": 1,
                           "audio_encoder_output": ["aeout", 0],
                           "ref_image": ["img", 0]}},
    }

    def _ks(pos, neg, lat, sd):
        # ⚠️ The SLOTS are the point: both S2V nodes emit [positive, negative, latent], so the
        # sampler must read 0 / 1 / 2. Collapsing them feeds CONDITIONING into latent_image,
        # and forge rejects that at submit with return_type_mismatch — no GPU time, but a
        # wasted round trip.
        return {"class_type": "KSampler",
                "inputs": {"model": ["shift", 0], "positive": [pos, 0],
                           "negative": [neg, 1], "latent_image": [lat, 2],
                           "seed": int(sd), "steps": S2V_STEPS, "cfg": S2V_CFG,
                           "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}}

    g["samp"] = _ks("s2v", "s2v", "s2v", seed)
    acc = "samp"
    # Each extend appends ONE more chunk to the accumulated latent. The SAME
    # audio_encoder_output feeds every chunk — the audio is ONE continuous track across the
    # whole scene, not sliced per chunk, and the node derives its offset from the latent
    # length. That is the "one song, uninterrupted" property, and it comes free.
    for i in range(1, chunks):
        ext, k, cat = "ext%d" % i, "ks%d" % i, "cat%d" % i
        g[ext] = {"class_type": "WanSoundImageToVideoExtend",
                  "inputs": {"positive": ["pos", 0], "negative": ["neg", 0],
                             "vae": ["vae", 0], "length": S2V_CHUNK,
                             "video_latent": [acc, 0],
                             "audio_encoder_output": ["aeout", 0],
                             "ref_image": ["img", 0]}}
        g[k] = _ks(ext, ext, ext, int(seed) + i)
        g[cat] = {"class_type": "LatentConcat",
                  "inputs": {"samples1": [acc, 0], "samples2": [k, 0], "dim": "t"}}
        acc = cat
    g["vdec"] = {"class_type": "VAEDecode",
                 "inputs": {"samples": [acc, 0], "vae": ["vae", 0]}}
    # ⚠️ No `audio` on CreateVideo. ComfyUI hands the muxer's movflags to the AAC encoder and
    # the SAVE dies AFTER the render completes —
    #   avcodec_open2("aac", {'movflags': 'use_metadata_tags+faststart'}) returned 22
    # — losing the whole run. S2V renders silent; the performance audio is muxed on this Mac
    # once the clip is downloaded.
    g["mkvid"] = {"class_type": "CreateVideo",
                  "inputs": {"images": ["vdec", 0], "fps": S2V_FPS}}
    g["save"] = {"class_type": "SaveVideo",
                 "inputs": {"video": ["mkvid", 0],
                            "filename_prefix": "video/forge_lab_s2v",
                            "format": "auto", "format.codec": "auto", "codec": "auto"}}
    _check_graph(g)
    return g


def wan_rules(mode, quality):
    """The DERIVED settings for a WAN path — the single source of truth, so the UI hint, the
    estimate and the graph can never disagree about what a run will actually use."""
    if mode == "s2v":
        # ONE path, fully baked. steps/cfg/shift/LoRA all follow from this dict; there is no
        # field a user can half-switch, which is the whole point of the design.
        return {"quality": S2V_QUALITY, "mode": "s2v", "steps": S2V_STEPS, "turbo": True,
                "shift": S2V_SHIFT, "cfg": S2V_CFG, "half": max(1, S2V_STEPS // 2),
                "unet": None, "lora": None, "node": "s2v", "fps": S2V_FPS, "audio": True}
    qk = quality if quality in WAN_QUALITY else WAN_QUALITY_DEFAULT
    q = WAN_QUALITY[qk]
    vm = WAN_MODES.get(mode, WAN_MODES[WAN_VARIANT_DEFAULT])
    turbo, steps = bool(q["turbo"]), int(q["steps"])
    return {"quality": qk, "mode": mode, "steps": steps, "turbo": turbo,
            "shift": WAN_SHIFT[turbo],
            "cfg": 1.0 if turbo else float(vm["cfg_full"]),
            "half": max(1, steps // 2),
            "unet": WAN_UNET[vm["node"]],
            "lora": WAN_TURBO[vm["node"]] if turbo else None,
            "node": WAN_NODE[vm["node"]], "fps": WAN_FPS, "audio": False}


def wan_frames(seconds):
    """floor(duration x fps) + 1 at 16 fps (5s -> 81), then UP to the nearest 1+4k count: the
    Wan* nodes take `length` in steps of 4 starting at 1, and the template's own math
    expression is floor(a*b+1)."""
    n = int(round(float(seconds) * WAN_FPS)) + 1
    return 1 + 4 * ((n - 1 + 3) // 4)


def wan_graph(mode, prompt, length, width, height, quality, seed,
              start_name=None, last_name=None, audio_name=None, chunks=1):
    """WAN 2.2 14B video. `quality` picks a PATH; steps, cfg, shift and the LoRA pair all
    follow from it — there is deliberately NO steps/shift/LoRA argument to get wrong.

    `mode` is a WAN_MODES key. start_name / last_name are already-UPLOADED input filenames
    (i2v needs start, flf2v needs both, t2v neither). See the header comment — do not
    'tidy' the shape.
    """
    vm = WAN_MODES.get(mode, WAN_MODES[WAN_VARIANT_DEFAULT])
    if vm.get("node") == "s2v":
        # Audio-DRIVEN: a different model, a single unet, an audio encoder, and a chained
        # shape. Kept in its own function so the high/low-expert paths below stay untouched.
        return s2v_graph(prompt, width, height, chunks, seed, start_name, audio_name)
    r = wan_rules(mode, quality)
    steps, half, shift, cfg = r["steps"], r["half"], r["shift"], r["cfg"]
    if vm["refs"] >= 1 and not start_name:
        raise ValueError("%s needs a first frame" % vm["label"])
    if vm["refs"] >= 2 and not last_name:
        raise ValueError("%s needs both a first and a last frame" % vm["label"])
    if vm["refs"] == 0 and (start_name or last_name):
        raise ValueError("WAN t2v takes no image — use 'First frame' to animate one")
    hi, lo = r["unet"]
    g = {
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": WAN_CLIP, "type": "wan"}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0], "text": WAN_NEG}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        "unet_hi": {"class_type": "UNETLoader",
                    "inputs": {"unet_name": hi, "weight_dtype": "default"}},
        "unet_lo": {"class_type": "UNETLoader",
                    "inputs": {"unet_name": lo, "weight_dtype": "default"}},
    }
    if r["node"] is None:
        # t2v: the verified path — an empty latent, conditioning straight from the text
        g["latent"] = {"class_type": "EmptyHunyuanLatentVideo",
                       "inputs": {"width": int(width), "height": int(height),
                                  "length": int(length), "batch_size": 1}}
        cond_pos, cond_neg, lat = ["pos", 0], ["neg", 0], ["latent", 0]
    else:
        g["load_start"] = {"class_type": "LoadImage", "inputs": {"image": start_name}}
        ins = {"positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0],
               "width": int(width), "height": int(height), "length": int(length),
               "batch_size": 1, "start_image": ["load_start", 0]}
        if r["node"] == "WanFirstLastFrameToVideo":
            g["load_last"] = {"class_type": "LoadImage", "inputs": {"image": last_name}}
            ins["end_image"] = ["load_last", 0]
        g["cond"] = {"class_type": r["node"], "inputs": ins}
        cond_pos, cond_neg, lat = ["cond", 0], ["cond", 1], ["cond", 2]
    hi_src, lo_src = "unet_hi", "unet_lo"
    if r["lora"]:
        lh, ll = r["lora"]
        g["lora_hi"] = {"class_type": "LoraLoaderModelOnly",
                        "inputs": {"model": [hi_src, 0], "lora_name": lh,
                                   "strength_model": 1.0}}
        g["lora_lo"] = {"class_type": "LoraLoaderModelOnly",
                        "inputs": {"model": [lo_src, 0], "lora_name": ll,
                                   "strength_model": 1.0}}
        hi_src, lo_src = "lora_hi", "lora_lo"
    g["shift_hi"] = {"class_type": "ModelSamplingSD3",
                     "inputs": {"model": [hi_src, 0], "shift": shift}}
    g["shift_lo"] = {"class_type": "ModelSamplingSD3",
                     "inputs": {"model": [lo_src, 0], "shift": shift}}
    g["samp_hi"] = {"class_type": "KSamplerAdvanced",
                    "inputs": {"model": ["shift_hi", 0], "add_noise": "enable",
                               "noise_seed": seed, "steps": steps, "cfg": cfg,
                               "sampler_name": "euler", "scheduler": "simple",
                               "positive": cond_pos, "negative": cond_neg,
                               "latent_image": lat, "start_at_step": 0,
                               "end_at_step": half,
                               "return_with_leftover_noise": "enable"}}
    g["samp_lo"] = {"class_type": "KSamplerAdvanced",
                    "inputs": {"model": ["shift_lo", 0], "add_noise": "disable",
                               "noise_seed": 0, "steps": steps, "cfg": cfg,
                               "sampler_name": "euler", "scheduler": "simple",
                               "positive": cond_pos, "negative": cond_neg,
                               "latent_image": ["samp_hi", 0], "start_at_step": half,
                               "end_at_step": 10000,
                               "return_with_leftover_noise": "disable"}}
    g["vdec"] = {"class_type": "VAEDecode",
                 "inputs": {"samples": ["samp_lo", 0], "vae": ["vae", 0]}}
    g["mkvid"] = {"class_type": "CreateVideo",
                  "inputs": {"images": ["vdec", 0], "fps": WAN_FPS}}
    g["save"] = {"class_type": "SaveVideo",
                 "inputs": {"video": ["mkvid", 0],
                            "filename_prefix": "video/forge_lab_wan",
                            "format": "auto", "format.codec": "auto", "codec": "auto"}}
    _check_graph(g)
    return g


# ======================= LTX 2.3 · IC-LoRA Union Control (video-to-video) ======================
# Video-to-video by STRUCTURE: a control clip supplies the geometry (depth is extracted from it
# on the rig by MoGe), the prompt supplies the look. Verified end to end on forge 2026-09-27:
# these graphs are the shipped `video_ltx2_3_ic_lora` template flattened to API form.
#
# ⚠️ THE CANVAS MUST BE DIVISIBLE BY 64 — not 32. The IC-LoRA runs reference_downscale_factor=2
# over LTX's /32 latent, so an ODD latent dimension is fatal:
#     640x352 -> latent 20x11 -> ValueError "Latent spatial size 20x11 must be divisible by
#     reference_downscale_factor 2 from the IC-LoRA parameters."
# 1280x704 (native), 768x512 and 576x320 all pass. Enforced in ltx_graph() so it can never be a
# crash discovered after a long wait.
#
# DELIBERATE SIMPLIFICATION vs the template: its prompt-enhancer branch (gemma-abliterated LoRA
# -> TextGenerateLTX2Prompt -> ComfySwitchNode) is inert in the shipped graph (switch=false), so
# it is REMOVED here and the prompt goes straight into CLIPTextEncode. That also drops the
# abliterated LoRA download. Behavior-identical; 4 fewer nodes and one fewer model file.
# Likewise a constant-true ComfySwitchNode on the depth path is collapsed.
LTX_CKPT = "ltx-2.3-22b-distilled-fp8.safetensors"    # combined: DiT + audio VAE + text projection
LTX_TEXT_ENCODER = "gemma_3_12B_it_fp4_mixed.safetensors"
LTX_ICLORA = "ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors"
LTX_MOGE = "moge_2_vitl_normal_fp16.safetensors"      # the depth branch's geometry model
LTX_FPS = 25
LTX_STEPS = 8                 # distilled model: 8 steps at cfg 1 IS the shipped path
LTX_CFG = 1.0                 # cfg 1.0 => the negative prompt is INERT on this path
LTX_SAMPLER = "euler_ancestral"
LTX_SCHEDULER = "linear_quadratic"
LTX_SLICE_S = 5.0             # default control-clip slice; frame count follows from it
LTX_CANVAS_DIVISOR = 64       # NOT 32 — see the warning above
LTX_SIZE_DEFAULT = "Native 1280×704  (0.90 MP · measured)"
LTX_SIZES = {"Draft 576×320  (0.18 MP · measured)": (576, 320),
             "768×512  (0.39 MP · off-native aspect)": (768, 512),
             "Native 1280×704  (0.90 MP · measured)": (1280, 704)}
LTX_NEG = ("blurry, out of focus, overexposed, underexposed, low contrast, washed out colors, "
           "excessive noise, grainy texture, poor lighting, flickering, motion blur, distorted "
           "proportions, unnatural skin tones, deformed facial features, asymmetrical face, "
           "missing facial features, extra limbs, disfiguring makeup, choppy, glitch")
# MEASURED on one 3090, 121 frames, model already resident (warm):
#     576x320 (0.18 MP) -> 24 s        1280x704 (0.90 MP, native) -> 102 s
# Solve t = LOAD + MP*c over those two points -> c = 108.3 s/MP, LOAD = 4.5 s.
# ⚠️ The FIRST run after an engine switch carries the model load. The 102 s first-ever LTX run
# at 768x512 was load-inclusive and is NOT a compute figure. Cost is SUBLINEAR in pixels
# (4.25x time for 5x pixels) — there is no streaming cliff at native res on this card.
LTX_LOAD_S = 4.5
LTX_SEC_PER_MP = 108.3
# ⚠️ UNMEASURED, and deliberately 0.0 rather than a guess: both LTX timings were WARM runs (the
# engine already resident). Inventing a switch constant would make the estimate worse, not better.
# Measure it by running another engine then LTX back-to-back and diffing. Until then the UI says
# no switch is counted for LTX.
LTX_SWITCH_S = 0.0
LTX_FRAME_REF = 121           # the fit's frame count; scaled linearly from here


SIZE_DEFAULT_BY_KIND["ltx"] = LTX_SIZE_DEFAULT


def ltx_canvas_ok(width, height):
    """Divisible by 64, NOT 32 — the IC-LoRA's reference_downscale_factor=2 rule."""
    return (int(width) % LTX_CANVAS_DIVISOR == 0) and (int(height) % LTX_CANVAS_DIVISOR == 0)


def ltx_estimate_seconds(width, height, frames):
    """Estimate from the two measured points. Labelled an estimate in the UI, like the others."""
    mp = (int(width) * int(height)) / 1_000_000.0
    return LTX_LOAD_S + LTX_SEC_PER_MP * mp * (float(frames) / LTX_FRAME_REF)


def ltx_rules():
    """DERIVED settings for the LTX IC-LoRA path — the single source of truth, so the UI hint,
    the estimate and the graph cannot disagree. There is no steps/cfg/sampler argument anywhere
    to mis-set: 8 steps at cfg 1.0 with euler_ancestral/linear_quadratic is what this distilled
    model wants, and the IC-LoRA supplies the structure."""
    return {"steps": LTX_STEPS, "cfg": LTX_CFG, "sampler": LTX_SAMPLER,
            "scheduler": LTX_SCHEDULER, "fps": LTX_FPS, "lora": LTX_ICLORA,
            "divisor": LTX_CANVAS_DIVISOR, "audio": True, "engine": "ltx"}


def ltx_length_for(frames):
    """The latent length for a source clip of `frames` frames — the official 8n+1 rule applied
    to a LTX_SLICE_S slice of it.

    ⚠️ ONE definition, deliberately: the graph and estimate_seconds() must agree. If they compute
    the length separately they drift, and the estimate then quotes a time for a render that cannot
    happen. (The bug this replaces read the length off the DEPTH branch instead: a 3 s slice of a
    4.84 s clip became 73 frames — and 73 IS 8n+1, so no legality check could see it.)

    Rules, from three agreeing sources: Lightricks' LTX-2 model card ("frame count must be
    divisible by 8 + 1"), docs.ltx.io ("Length: 97 frames | 1 + a multiple of 8"), and the
    template itself (EmptyLTXVLatentVideo default 97, Video Slice 5 s).
    """
    cap = int(round(LTX_SLICE_S * LTX_FPS))          # the template's 5 s control-clip slice
    n_src = int(frames) if frames and int(frames) > 0 else LTX_FRAME_REF
    return 8 * ((min(n_src, cap) - 1) // 8) + 1      # snap DOWN to 8n+1


def ltx_graph(prompt, width, height, seed, control_name, first_name=None, duration=None,
              frames=None):
    """LTX 2.3 + IC-LoRA Union Control. `control_name` is the source clip whose STRUCTURE the
    render keeps (uploaded via the same /upload/image endpoint the stills use — ComfyUI stores
    any file into the input dir). `first_name` is an optional still pinned as frame 0.

    Baked, NOT settable: 8 steps, cfg 1.0, euler_ancestral, linear_quadratic, MoGe depth at
    resolution_level 9, IC-LoRA strength 1, 25 fps. Refusals live here, not only in the UI.
    """
    if not control_name:
        raise ValueError("LTX IC-LoRA needs a CONTROL VIDEO — that clip IS the structure")
    width, height = int(width), int(height)
    if not ltx_canvas_ok(width, height):
        raise ValueError(
            "LTX IC-LoRA canvas must be divisible by %d (got %dx%d). The IC-LoRA's "
            "reference_downscale_factor=2 applies to LTX's /32 latent, so both dimensions must "
            "be multiples of 64 — e.g. 1280x704, 768x512, 576x320."
            % (LTX_CANVAS_DIVISOR, width, height))
    fps = LTX_FPS
    # ⚠️ The number must come from the CONTROL CLIP. This used to be ["dsize", 2] — the frame
    # count of the MoGe DEPTH image — so the render's length was whatever the depth pass returned
    # (a 3 s slice of a 4.84 s / 121-frame clip produced 73 frames).
    _cap = (int(round(float(duration) * fps)) if duration not in (None, "", 0) else None)
    length = ltx_length_for(_cap if _cap is not None else frames)
    if length < 9:
        raise ValueError(
            "LTX IC-LoRA needs a longer source clip: there are too few frames to make even 9, "
            "the shortest length the 8n+1 rule allows (got %r frames)." % (frames,))
    dur = length / float(fps)      # slice EXACTLY the frames the latent will use
    g = {
        # --- control clip -> frames -> depth (MoGe) -------------------------------------
        "ctrl":    {"class_type": "LoadVideo", "inputs": {"file": control_name}},
        "slice":   {"class_type": "Video Slice",
                    "inputs": {"video": ["ctrl", 0], "start_time": 0, "duration": dur,
                               "strict_duration": False}},
        "cframes": {"class_type": "GetVideoComponents", "inputs": {"video": ["slice", 0]}},
        "moge":    {"class_type": "LoadMoGeModel", "inputs": {"model_name": LTX_MOGE}},
        "csize":   {"class_type": "GetImageSize", "inputs": {"image": ["cframes", 0]}},
        # the template's own guard: only downscale the control frames if they are wider than 2048
        "big":     {"class_type": "ComfyMathExpression",
                    "inputs": {"values.a": ["csize", 0], "expression": "a > 2048"}},
        "fit":     {"class_type": "ResizeImageMaskNode",
                    "inputs": {"input": ["cframes", 0], "resize_type": "scale longer dimension",
                               "resize_type.longer_size": 2048, "scale_method": "area"}},
        "ctl":     {"class_type": "ComfySwitchNode",
                    "inputs": {"on_false": ["cframes", 0], "on_true": ["fit", 0],
                               "switch": ["big", 2]}},
        "geom":    {"class_type": "MoGeInference",
                    "inputs": {"moge_model": ["moge", 0], "image": ["ctl", 0],
                               "resolution_level": 9, "batch_size": 4, "fov_x_degrees": 0.0,
                               "force_projection": True, "apply_mask": True, "refine_steps": 3}},
        "depth":   {"class_type": "MoGeRender",
                    "inputs": {"moge_geometry": ["geom", 0], "output": "depth"}},
        # --- model + text ---------------------------------------------------------------
        "ckpt":    {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": LTX_CKPT}},
        "avae":    {"class_type": "LTXVAudioVAELoader", "inputs": {"ckpt_name": LTX_CKPT}},
        "clip":    {"class_type": "LTXAVTextEncoderLoader",
                    "inputs": {"text_encoder": LTX_TEXT_ENCODER, "ckpt_name": LTX_CKPT,
                               # ⚠️ `device` is REQUIRED and has NO default. Omitting it killed
                               # every LTX submit with HTTP 400 "required_input_missing: device".
                               # Found by the END-TO-END run; the graph validator could not see it
                               # because it only looked for wrong inputs, never missing ones.
                               "device": "default"}},
        "lora":    {"class_type": "LoraLoaderModelOnly",
                    "inputs": {"model": ["ckpt", 0], "lora_name": LTX_ICLORA,
                               "strength_model": 1.0}},
        "icp":     {"class_type": "GetICLoRAParameters", "inputs": {"iclora_model": ["lora", 0]}},
        "pos":     {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg":     {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": LTX_NEG}},
        "cond":    {"class_type": "LTXVConditioning",
                    "inputs": {"positive": ["pos", 0], "negative": ["neg", 0], "frame_rate": fps}},
        # --- latenats: length comes from the CONTROL clip's frame count ------------------
        "lat":     {"class_type": "EmptyLTXVLatentVideo",
                    "inputs": {"width": width, "height": height, "length": length,
                               "batch_size": 1}},
        "alat":    {"class_type": "LTXVEmptyLatentAudio",
                    "inputs": {"audio_vae": ["avae", 0], "frames_number": length,
                               "frame_rate": fps, "batch_size": 1}},
        "guide":   {"class_type": "LTXVAddGuide",
                    "inputs": {"positive": ["cond", 0], "negative": ["cond", 1],
                               "vae": ["ckpt", 2], "latent": ["lat", 0], "image": ["depth", 0],
                               "iclora_parameters": ["icp", 0], "frame_idx": 0, "strength": 1.0}},
        # --- sample -> split AV -> crop guides -> decode ---------------------------------
        "cat":     {"class_type": "LTXVConcatAVLatent",
                    "inputs": {"video_latent": ["guide", 2], "audio_latent": ["alat", 0]}},
        "samp":    {"class_type": "KSampler",
                    "inputs": {"model": ["lora", 0], "positive": ["guide", 0],
                               "negative": ["guide", 1], "latent_image": ["cat", 0],
                               "seed": int(seed), "steps": LTX_STEPS, "cfg": LTX_CFG,
                               "sampler_name": LTX_SAMPLER, "scheduler": LTX_SCHEDULER,
                               "denoise": 1.0}},
        "sep":     {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["samp", 0]}},
        "crop":    {"class_type": "LTXVCropGuides",
                    "inputs": {"positive": ["guide", 0], "negative": ["guide", 1],
                               "latent": ["sep", 0]}},
        "vdec":    {"class_type": "VAEDecodeTiled",
                    "inputs": {"samples": ["crop", 2], "vae": ["ckpt", 2], "tile_size": 768,
                               "overlap": 64, "temporal_size": 4096, "temporal_overlap": 64}},
        "adec":    {"class_type": "LTXVAudioVAEDecode",
                    "inputs": {"samples": ["sep", 1], "audio_vae": ["avae", 0]}},
        # NOTE: unlike S2V, wiring audio into CreateVideo SAVES FINE on this path — the verified
        # LTX run produced a file with an AAC track. Do not "fix" this to match S2V's workaround.
        "mkvid":   {"class_type": "CreateVideo",
                    "inputs": {"images": ["vdec", 0], "audio": ["adec", 0], "fps": fps,
                               "bit_depth": "auto", "color_space": "sRGB"}},
        "save":    {"class_type": "SaveVideo",
                    "inputs": {"video": ["mkvid", 0], "filename_prefix": "video/forge_lab_ltx",
                               "format": "auto", "format.codec": "auto", "codec": "auto"}},
    }
    if first_name:
        g["still"] = {"class_type": "LoadImage", "inputs": {"image": first_name}}
        g["i2v"] = {"class_type": "LTXVImgToVideoInplace",
                    "inputs": {"vae": ["ckpt", 2], "image": ["still", 0], "latent": ["lat", 0],
                               "strength": 1.0, "bypass": False}}
        g["guide"]["inputs"]["latent"] = ["i2v", 0]
    _check_graph(g)
    return g



# ==================== Wan Animate 2 · motion transfer (video-to-video) =========================
# Takes a CHARACTER STILL + a DRIVING CLIP and makes the character perform the clip. Wan Animate 2
# extracts the pose from the driving video ITSELF (no separate pose estimator node) and there is NO
# character LoRA on this path — identity comes from the still alone.
#
# Built from the shipped `video_wan_animate2` template, SINGLE-PASS path only for v1: the template
# wraps generation in a chunk loop (StartLoop/EndLoop + ContextWindowsManual + RebatchImages) whose
# on_true branch is what produces more than one 81-frame pass (the verified demo run emitted 121
# frames at 480x848 in 252 s). The loop is a SEPARATE slice — this is one pass, wired off the
# template's own switch=false branch.
ANIMATE2_UNET = "wan_animate_2_int8_convrot.safetensors"
ANIMATE2_LORA = "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
ANIMATE2_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
ANIMATE2_CLIP_VISION = "clip_vision_h.safetensors"
ANIMATE2_VAE = "Wan2_1_VAE_bf16.safetensors"
ANIMATE2_FPS = 24.0
ANIMATE2_STEPS = 6              # lcm sampler, cfg 1.0 — the template's baked fast path
ANIMATE2_CFG = 1.0
ANIMATE2_SHIFT = 5.0
ANIMATE2_SAMPLER = "lcm"
ANIMATE2_SCHEDULER = "simple"
ANIMATE2_LENGTH = 81            # one pass. The 1+4k frame grid governs the LATENT, not this.
ANIMATE2_CACHE_DEVICE = "gpu"
ANIMATE2_CACHE_DTYPE = "int8"
ANIMATE2_SIZE_DEFAULT = "Native 480×848  (measured output)"
ANIMATE2_SIZES = {"Native 480×848  (measured output)": (480, 848),
                  "Portrait 720×1280  (heavier)": (720, 1280),
                  "Landscape 832×480": (832, 480),
                  "Square 640×640": (640, 640)}
# The template's negative conditioning, verbatim (Chinese, and it is what the verified run used).
ANIMATE2_NEG = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
                "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
                "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")
# MEASURED, TWO points, same canvas (480x848), same 6 steps, one 3090:
#    121 frames — the LOOPED path (the template's own demo run)          -> 252 s = 2.083 s/frame
#     81 frames — SINGLE PASS, built by THIS builder                      -> 181 s = 2.235 s/frame
# The two agree within 7%, so cost is ~LINEAR in frames on this engine and the estimate uses the
# mean 2.16 s/frame at 0.407 MP. ⚠️ Scaling to another canvas multiplies by (MP / 0.407) with an
# exponent of 1.0 — that exponent is an ASSUMPTION, not a measurement: only ONE canvas has ever
# been timed here. Re-fit the moment a second canvas is measured.
ANIMATE2_MP_REF = 0.407                     # 480 x 848
ANIMATE2_MEASURED = ((81, 181.0, "single pass, this builder"),
                     (121, 252.0, "looped path, template demo run"))
ANIMATE2_SEC_PER_FRAME_AT_REF_MP = 2.16
ANIMATE2_SWITCH_S = 0.0      # ⚠️ UNMEASURED — same reasoning as LTX_SWITCH_S: warm runs only
# YuE2: MEASURED. The same 60 s song cost 20.0 s predicted warm and 25.2 s on a cold page
# cache + restarted ComfyUI — so the whole switch/stage penalty is ~5 s, not H3's 12 s: the
# int8 checkpoint is 4 GB and loads from NVMe in seconds.
YUE2_SWITCH_S = 5.0
ANIMATE2_ESTIMATE_MEASURED = True           # fitted to the two real runs above
# Registered HERE, not beside the LTX one: a module-level dict is evaluated immediately, so
# referencing ANIMATE2_SIZE_DEFAULT from a block that appears earlier in the file is a NameError
# at import — the app dies with no window and py_compile says nothing. Anchors must sit below
# the names they use.
SIZE_DEFAULT_BY_KIND["animate2"] = ANIMATE2_SIZE_DEFAULT


def animate2_rules():
    """DERIVED settings for the Animate 2 path — single source of truth. No steps/shift/cfg/LoRA
    argument exists to mis-set: 6 steps with lcm at cfg 1.0, shift 5.0, the I2V distill LoRA on,
    and the 1.8B-face-cache node pinned to gpu/int8 is the template's own configuration."""
    return {"steps": ANIMATE2_STEPS, "cfg": ANIMATE2_CFG, "shift": ANIMATE2_SHIFT,
            "sampler": ANIMATE2_SAMPLER, "scheduler": ANIMATE2_SCHEDULER, "fps": ANIMATE2_FPS,
            "lora": ANIMATE2_LORA, "length": ANIMATE2_LENGTH, "audio": False, "engine": "animate2"}


def animate2_estimate_seconds(width, height, frames):
    """Fitted to the two MEASURED runs above (same canvas, same 6 steps). Labelled an estimate
    everywhere it is shown. Canvas scaling is linear in megapixels as a STATED assumption."""
    mp = (int(width) * int(height)) / 1_000_000.0
    return ANIMATE2_SEC_PER_FRAME_AT_REF_MP * float(frames) * (mp / ANIMATE2_MP_REF)


def animate2_graph(prompt, width, height, seed, ref_name, drive_name,
                   length=ANIMATE2_LENGTH, pose_prompt=None, fps=ANIMATE2_FPS):
    """Wan Animate 2, ONE PASS. `ref_name` is the character still (uploaded via /upload/image,
    which stores any file in the input dir); `drive_name` is the video whose motion is copied.

    Baked, NOT settable: 6 steps, cfg 1.0, lcm/simple, shift 5.0, the distill LoRA, gpu/int8 face
    cache. Refusals live here as well as in the UI. Output fps is deliberately NORMALISED to 24:
    the template inherits the source clip's rate and emits a fractional 603/25 = 24.12 fps.
    """
    if not ref_name:
        raise ValueError("Wan Animate 2 needs a REFERENCE STILL — that still IS the character")
    if not drive_name:
        raise ValueError("Wan Animate 2 needs a DRIVING VIDEO — that clip IS the performance")
    width, height = int(width), int(height)
    if width % 16 or height % 16:
        raise ValueError("Wan Animate 2 canvas must be a multiple of 16 (got %dx%d)" % (width, height))
    pose_text = pose_prompt if pose_prompt else prompt
    g = {
        "ref":     {"class_type": "LoadImage", "inputs": {"image": ref_name}},
        "drive":   {"class_type": "LoadVideo", "inputs": {"file": drive_name}},
        "dvframes": {"class_type": "GetVideoComponents", "inputs": {"video": ["drive", 0]}},
        # the driving frames ARE the pose source, resized onto the target canvas
        "pose":    {"class_type": "ResizeImageMaskNode",
                    "inputs": {"input": ["dvframes", 0], "resize_type": "scale dimensions",
                               "resize_type.width": width, "resize_type.height": height,
                               "resize_type.crop": "center", "scale_method": "area"}},
        "psize":   {"class_type": "GetImageSize", "inputs": {"image": ["pose", 0]}},
        # the still is resized to the SAME canvas, so reference and pose agree
        "rref":    {"class_type": "ResizeImageMaskNode",
                    "inputs": {"input": ["ref", 0], "resize_type": "scale dimensions",
                               "resize_type.width": ["psize", 0], "resize_type.height": ["psize", 1],
                               "resize_type.crop": "center", "scale_method": "area"}},
        "unet":    {"class_type": "UNETLoader",
                    "inputs": {"unet_name": ANIMATE2_UNET, "weight_dtype": "default"}},
        "lora":    {"class_type": "LoraLoaderModelOnly",
                    "inputs": {"model": ["unet", 0], "lora_name": ANIMATE2_LORA,
                               "strength_model": 1.0}},
        "cache":   {"class_type": "WanAnimate2Cache",
                    "inputs": {"model": ["lora", 0], "device": ANIMATE2_CACHE_DEVICE,
                               "dtype": ANIMATE2_CACHE_DTYPE}},
        "shift":   {"class_type": "ModelSamplingSD3",
                    "inputs": {"model": ["cache", 0], "shift": ANIMATE2_SHIFT}},
        "clip":    {"class_type": "CLIPLoader",
                    "inputs": {"clip_name": ANIMATE2_CLIP, "type": "wan", "device": "default"}},
        "cvis":    {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": ANIMATE2_CLIP_VISION}},
        "vae":     {"class_type": "VAELoader", "inputs": {"vae_name": ANIMATE2_VAE}},
        "pos":     {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg":     {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0],
                                                               "text": ANIMATE2_NEG}},
        "pospose": {"class_type": "CLIPTextEncode",
                    "inputs": {"clip": ["clip", 0], "text": pose_text}},
        "visref":  {"class_type": "CLIPVisionEncode",
                    "inputs": {"clip_vision": ["cvis", 0], "image": ["rref", 0], "crop": "none"}},
        "first":   {"class_type": "ImageFromBatch",
                    "inputs": {"image": ["pose", 0], "batch_index": 0, "length": 1}},
        "vispose": {"class_type": "CLIPVisionEncode",
                    "inputs": {"clip_vision": ["cvis", 0], "image": ["first", 0], "crop": "none"}},
        # pose_end_percent 1.0 = the node's declared default AND the shipped template's own value
        # (video_wan_animate2.json says 1 in BOTH widgets_values_named and positional
        # widgets_values; /object_info declares default 1.0). MEASURED A/B (2026-09-27), same
        # seed, same canvas, ONE variable: 0.0 -> motion-energy correlation 0.127 vs the driving
        # clip, render motion 6.2 against the source's 16.3; 1.0 -> 0.634, render 17.6 vs 16.3.
        # Ryan's eye: 1.0 is "much better". Do not "restore" this to 0.0.
        #
        # ⚠️ CORRECTION (kept so the mistake is not repeated): an earlier note here claimed the
        # SHIPPED TEMPLATE passes 0.0 and suppresses motion. That was WRONG. The 0.0 came from our
        # own scratch flattener (~/.hermes/tmp/flatten_workflow.py), which emitted
        # pose_end_percent=0 for a template that says 1 — so the first "cool" clip was degraded by
        # OUR tool, not by ComfyUI's template, and there is NO upstream defect to report.
        # tools/flatten_fidelity.py now id-pairs a flattened graph against its template's named
        # values and flags exactly this class of silent value shift (LTX: clean; Animate 2: caught it).
        "wan2":    {"class_type": "WanAnimate2ToVideo",
                    "inputs": {"positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0],
                               "reference_image": ["rref", 0], "pose_video": ["pose", 0],
                               "clip_vision_output": ["visref", 0], "positive_pose": ["pospose", 0],
                               "clip_vision_output_pose": ["vispose", 0],
                               "width": ["psize", 0], "height": ["psize", 1],
                               "length": int(length), "batch_size": 1, "video_frame_offset": 0,
                               "pose_strength": 1.0, "pose_start_percent": 0.0,
                               "pose_end_percent": 1.0, "reference_image_strength": 1.0}},
        "ksel":    {"class_type": "KSamplerSelect", "inputs": {"sampler_name": ANIMATE2_SAMPLER}},
        "sched":   {"class_type": "BasicScheduler",
                    "inputs": {"model": ["cache", 0], "scheduler": ANIMATE2_SCHEDULER,
                               "steps": ANIMATE2_STEPS, "denoise": 1.0}},
        # slots are the point: WanAnimate2ToVideo emits [positive, negative, latent] -> 0 / 1 / 2
        "samp":    {"class_type": "SamplerCustom",
                    "inputs": {"model": ["shift", 0], "positive": ["wan2", 0],
                               "negative": ["wan2", 1], "latent_image": ["wan2", 2],
                               "sampler": ["ksel", 0], "sigmas": ["sched", 0],
                               "add_noise": True, "noise_seed": int(seed), "cfg": ANIMATE2_CFG}},
        "trim":    {"class_type": "TrimVideoLatent",
                    "inputs": {"samples": ["samp", 0], "trim_amount": ["wan2", 3]}},
        "vdec":    {"class_type": "VAEDecode",
                    "inputs": {"samples": ["trim", 0], "vae": ["vae", 0]}},
        # fps is NORMALISED (the template inherits the source's 60 -> a 603/25 = 24.12 output).
        # No audio link: WanAnimate2ToVideo renders picture only and CreateVideo.audio is the
        # known-broken mux on this rig (see S2V); the driving track is muxed on this Mac if wanted.
        "mkvid":   {"class_type": "CreateVideo",
                    "inputs": {"images": ["vdec", 0], "fps": float(fps), "bit_depth": "auto",
                               "color_space": "sRGB", "codec": "none"}},
        "save":    {"class_type": "SaveVideo",
                    "inputs": {"video": ["mkvid", 0], "filename_prefix": "video/forge_lab_animate2",
                               "format": "auto", "format.codec": "auto", "codec": "auto"}},
    }
    _check_graph(g)
    return g



def _index_path():
    return os.path.join(RUNS, "index.json")


def read_index():
    try:
        return json.load(open(_index_path()))
    except Exception:
        return []


def _index_insert(meta):
    items = [i for i in read_index() if i.get("id") != meta["id"]]
    items.insert(0, meta)
    json.dump(items, open(_index_path(), "w"), indent=1)
    _index_notify(meta)


# ---- A LANDING TAKE MUST REACH THE TAKES PANEL ----------------------------------------------
# Ryan, 2026-10-11: "new renders in the weirdo page should show up in the takes in the lab
# forge." A workspace render (Weirdo / Song / Studio / Long video) runs on its OWN thread and
# then calls collect(), which lands the take in runs/index.json — and NOTHING in the main window
# used to notice: load_takes() ran at startup and after a MAIN-FORM run only. So a Weirdo render
# was already in the index (and there after a relaunch) while the open Takes list never changed.
#
# A watcher HERE is the one hook that covers every path, because submit() and collect() — and so
# every window and every headless tool — go through _index_insert(). ⚠️ _index_insert() runs on
# WORKER threads, so a watcher MUST NOT touch Tk (documented trap: Tk is main-thread-only); the
# watcher launch() registers only sets a flag and its own pump does the reload.
_INDEX_WATCHERS = []


def watch_index(fn):
    """Fire `fn(meta)` after any take lands. Returns fn, so it can be used as a decorator."""
    if fn not in _INDEX_WATCHERS:
        _INDEX_WATCHERS.append(fn)
    return fn


def _index_notify(meta):
    for fn in list(_INDEX_WATCHERS):
        try:
            fn(meta)
        except Exception as exc:          # a watcher must never break a landing take
            _log_err("index watcher failed: %r" % exc)


def collect(pid):
    """Pull outputs onto this Mac so nothing depends on forge's output dir."""
    items = read_index()
    meta = next((i for i in items if i.get("id") == pid), None)
    if not meta:
        return None
    try:
        hist = get_json("/history/%s" % pid, timeout=30)
    except Exception as e:
        return {"error": "history fetch failed: %s" % e}
    rec = hist.get(pid)
    if not rec:
        return {"error": "no history yet"}
    files = []
    for node_id, out in (rec.get("outputs") or {}).items():
        # ⚠️ "audio" is a FOURTH output key, not a flavour of "images": a SaveAudio node
        # reports {"audio": [{filename, subfolder, type}]} and nothing else, so before this
        # a song's flac was never pulled off forge — the run reported success and delivered
        # an empty folder. Confirmed against a real run, not guessed.
        for img in ((out.get("images") or []) + (out.get("gifs") or [])
                    + (out.get("videos") or []) + (out.get("audio") or [])):
            q = urllib.parse.urlencode({"filename": img["filename"],
                                        "subfolder": img.get("subfolder", ""),
                                        "type": img.get("type", "output")})
            try:
                blob = get_bytes("/view?" + q)
            except Exception as e:
                return {"error": "download failed: %s" % e}
            d = os.path.join(RUNS, pid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, img["filename"]), "wb") as f:
                f.write(blob)
            files.append({"file": img["filename"], "node": node_id, "bytes": len(blob)})
    # ⚠️ A FIFTH output shape: PreviewAny reports inline TEXT, not a file on the rig. A
    # cover's transcribed melody score arrives this way and forge keeps NO copy on disk, so
    # if it is not written out here it is gone — and the score is the only way to see what
    # melody was actually extracted. Text is not a download, so it is handled apart from the
    # /view loop above.
    for node_id, out in (rec.get("outputs") or {}).items():
        txt = out.get("text")
        if not txt:
            continue
        if isinstance(txt, (list, tuple)):
            txt = "\n".join(str(t) for t in txt)
        d = os.path.join(RUNS, pid)
        os.makedirs(d, exist_ok=True)
        fn = "%s_%s.txt" % (os.path.basename(meta.get("prefix") or "output"), node_id)
        try:
            with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                f.write(str(txt))
            files.append({"file": fn, "node": node_id, "bytes": len(str(txt)), "text": True})
            meta.setdefault("text_files", []).append(fn)
        except Exception as exc:
            _log_err("could not save text output from node %s: %r" % (node_id, exc))
    files.sort(key=lambda f: 1 if (f.get("file") or "") == meta.get("source") else 0)
    # ---- S2V: the render is SILENT on purpose (forge's ComfyUI cannot mux AAC — see
    # s2v_graph), so the performance audio is muxed HERE, from the take's own original file.
    if meta.get("s2v") and meta.get("audio_path") and os.path.exists(meta["audio_path"]):
        vids = [f["file"] for f in files
                if (f.get("file") or "").endswith((".mp4", ".webm"))]
        if vids:
            src = os.path.join(RUNS, pid, vids[0])
            dst = os.path.join(RUNS, pid, "s2v_synced.mp4")
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, "-i", meta["audio_path"],
                   "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                   "-b:a", "192k", dst]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if r.returncode == 0 and os.path.exists(dst):
                    files.append({"file": "s2v_synced.mp4", "node": "mux",
                                  "bytes": os.path.getsize(dst)})
                    meta["muxed"] = "s2v_synced.mp4"
                else:
                    meta["mux_error"] = (r.stderr or "")[:200]
            except Exception as exc:
                meta["mux_error"] = repr(exc)
    meta["images"] = files
    meta["finished"] = time.time()
    meta["wall"] = meta["finished"] - meta.get("started", meta["finished"])
    st = rec.get("status") or {}
    meta["status"] = st.get("status_str", "unknown")
    for msg in (st.get("messages") or []):
        if isinstance(msg, list) and msg[0] == "execution_error":
            meta["error"] = (msg[1] or {}).get("exception_message", "execution error")
    _index_insert(meta)
    return meta


def run_state(pid):
    try:
        hist = get_json("/history/%s" % pid, timeout=15)
    except Exception:
        return {"state": "unknown"}
    if pid in hist:
        st = (hist[pid].get("status") or {}).get("status_str", "unknown")
        if st == "success":
            return {"state": "done"}
        if st == "error":
            det = ""
            for m in ((hist[pid].get("status") or {}).get("messages") or []):
                if isinstance(m, list) and m[0] == "execution_error":
                    det = ((m[1] or {}).get("exception_message") or "").strip().splitlines()
            return {"state": "error", "detail": det[0] if det else ""}
        return {"state": "running"}
    try:
        q = get_json("/queue", timeout=15)
    except Exception:
        return {"state": "unknown"}
    for i, it in enumerate(q.get("queue_running") or []):
        if it[1] == pid:
            return {"state": "running"}
    for i, it in enumerate(q.get("queue_pending") or []):
        if it[1] == pid:
            return {"state": "queued", "position": i + 1}
    return {"state": "gone"}




def queue_note(ahead, own_est_s=0.0):
    """The line the estimate panel adds when forge is already busy. ⚠️ A COUNT, not a total:
    other clients' jobs (the overnight runner) cannot be priced honestly from here.
    Guarded by tools/net_matrix.py §4."""
    if not ahead:
        return ""
    s = ("⚠ forge already has %d job(s) running or waiting — yours starts after them"
         % int(ahead))
    if own_est_s and own_est_s > 0:
        t = int(round(own_est_s))
        s += " (your own runs in flight were estimated at ~%s)" % (
            ("%dm %02ds" % (t // 60, t % 60)) if t >= 60 else "%ds" % t)
    return s


def cancel_queued(pids):
    """Remove these prompt ids from forge's PENDING queue and mark their takes cancelled.

    ⚠️ Only the ids you pass, and only if they are still WAITING: forge's queue is shared with
    the overnight runner and other agents, and the RUNNING prompt is INTERRUPT's job. Returns
    the ids actually cancelled. Guarded by tools/net_matrix.py §3."""
    q = get_json("/queue", timeout=10)
    pending = {it[1] for it in (q.get("queue_pending") or [])}
    mine = [p for p in pids if p in pending]
    if mine:
        post_json("/queue", {"delete": mine}, timeout=15)
        for pid in mine:
            _index_update(pid, status="cancelled", orphan_checked=True)
    return mine


def wait_and_collect(pid, timeout=1800, poll=3):
    """Poll run_state(pid) until the run leaves running/queued, then collect(pid).

    Headless callers (a script, the rig) import forge_lab and call submit() directly —
    nothing flips the index entry's status off 'running' unless collect() runs. This is
    the public helper that closes that gap: it blocks up to `timeout` seconds and returns
    the meta dict collect() produces (or a state dict if collect() had nothing to read).
    """
    deadline = time.time() + float(timeout)
    state = {"state": "unknown"}
    while time.time() < deadline:
        state = run_state(pid)
        if state.get("state") not in ("running", "queued"):
            break
        time.sleep(poll)
    meta = collect(pid)
    if meta is None:
        meta = dict(state, id=pid)
    return meta


def _index_update(pid, **fields):
    """Update one index entry in place WITHOUT reordering the list."""
    items = read_index()
    for it in items:
        if it.get("id") == pid:
            it.update(fields)
            break
    json.dump(items, open(_index_path(), "w"), indent=1)


# ---- takes · RATING / FILTER / LINEAGE (module-level, so a GPU-free test can prove them) ----
# A take is judged ★ keep (1) / ✕ reject (-1) / unrated (0), stored on its index entry as
# `rating`. "unkept only" hides the takes already judged ★ — the takes-side echo of the
# Long-video board's per-scene keep. It COMBINES with the OptionMenu as an INTERSECTION: a take
# must pass BOTH the rating_filter and unkept_only to be drawn.
RATING_FILTERS = ["All", "★ only", "unrated", "hide ✕"]


def take_rating(it):
    """The stored rating as one of 1 / -1 / 0. A missing or junk value reads as 0."""
    try:
        v = int((it or {}).get("rating") or 0)
    except Exception:
        v = 0
    return 1 if v > 0 else (-1 if v < 0 else 0)


def set_rating(pid, value):
    """Write a take's rating to the index via _index_update (no list reorder). Clamps 1/-1/0."""
    try:
        v = int(value)
    except Exception:
        v = 0
    v = 1 if v > 0 else (-1 if v < 0 else 0)
    _index_update(pid, rating=v)
    return v


def filter_takes(items, rating_filter="All", unkept_only=False):
    """PURE. rating_filter in RATING_FILTERS selects by rating (unknown -> All):
       All      every take
       ★ only   rating == 1
       unrated  rating == 0 (missing counts as 0)
       hide ✕   rating != -1
    unkept_only additionally drops ★ keeps. Both apply (intersection)."""
    out = []
    for it in (items or []):
        r = take_rating(it)
        if rating_filter in ("★ only", "star"):
            if r != 1:
                continue
        elif rating_filter == "unrated":
            if r != 0:
                continue
        elif rating_filter in ("hide ✕", "noreject"):
            if r == -1:
                continue
        if unkept_only and r == 1:
            continue
        out.append(it)
    return out


def take_lineage(it):
    """"from: <character> · <project> · shot N" when the entry carries any of those, else "".
    The upcoming Studio writes character / project / shot onto index entries."""
    it = it or {}
    bits = []
    if it.get("character"):
        bits.append(str(it["character"]))
    if it.get("project"):
        bits.append(str(it["project"]))
    shot = it.get("shot")
    if shot not in (None, ""):
        bits.append("shot %s" % shot)
    return ("from: " + " · ".join(bits)) if bits else ""


def take_specs(it):
    """A compact "engine · N steps · Xs" line. Empty for a promoted take (no seed/steps of its own)."""
    it = it or {}
    if it.get("promote"):
        return ""
    bits = [engine_of(it.get("mode") or "t2i")]
    if it.get("steps") not in (None, ""):
        bits.append("%s steps" % it.get("steps"))
    secs = it.get("wall") or it.get("seconds")
    if secs:
        try:
            bits.append("%.1fs" % float(secs))
        except Exception:
            pass
    return " · ".join(bits)


def _style_toggle(btn, on, C):
    """House toggle idiom (no Checkbutton): rust fill + white when ON, panel2 + ink when off.
    C is passed in because the palette lives inside launch()."""
    try:
        if on:
            btn.configure(bg=C["rust_deep"], fg="#fff",
                          activebackground=C["rust_hi"], activeforeground="#fff")
        else:
            btn.configure(bg=C["panel2"], fg=C["ink"],
                          activebackground=C["rust"], activeforeground="#fff")
    except Exception:
        pass


def open_compare(root, ctx, takes):
    """COMPARE 2-4 takes side by side. Videos are synced FRAME CYCLERS (one PLAY drives all;
    shorter clips hold their last frame); stills are thumb_photo previews. Reuses the WEIRDO
    window's own weirdo_player_frames(), so it adds no dependency.

    ⚠️ Built ON CLICK, long after `_btn_colors(root)` ran at the end of launch(), so it repaints
    its OWN buttons through ctx["btn_colors"] — the documented on-demand Toplevel trap."""
    import tkinter as tk
    C = ctx["C"]
    F = ctx.get("fonts") or {}
    fh, fsm = F.get("fh"), F.get("fsm")
    takes = list(takes or [])[:4]
    win = tk.Toplevel(root)
    win.title("Compare takes")
    win.configure(bg=C["bg"])
    try:
        win.transient(root)
    except Exception:
        pass
    n = max(1, len(takes))
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(n * 360 + 40, sw - 40), min(820, sh - 120)))

    state = {"i": 0, "max": 1, "after": None}
    players, refs = [], []

    def _runnable():
        return any(len(p["frames"]) > 1 for p in players)

    def _tick():
        if not _runnable():
            state["after"] = None
            return
        state["i"] += 1
        for p in players:
            frames = p["frames"]
            if not frames:
                continue
            fr = frames[min(state["i"], len(frames) - 1)]     # shorter clips HOLD
            p["prev"].configure(image=fr, text="")
            p["prev"].image = fr
        state["after"] = win.after(1000 // 12, _tick)

    def _toggle():
        if not _runnable():
            return
        if state["after"] is not None:
            try:
                win.after_cancel(state["after"])
            except Exception:
                pass
            state["after"] = None
            play_btn.configure(text="PLAY")
        else:
            state["after"] = win.after(0, _tick)
            play_btn.configure(text="PAUSE")

    def _close():
        if state["after"] is not None:
            try:
                win.after_cancel(state["after"])
            except Exception:
                pass
            state["after"] = None
        win.destroy()

    def _paint(p):
        _style_toggle(p["star"], p.get("rating") == 1, C)
        _style_toggle(p["reject"], p.get("rating") == -1, C)

    def _set(p, val):
        cur = p.get("rating", 0)
        new = 0 if cur == val else val
        set_rating(p["id"], new)
        p["rating"] = new
        _paint(p)
        cb = ctx.get("reload_takes")
        if cb:
            try:
                cb()
            except Exception:
                pass

    bar = tk.Frame(win, bg=C["panel"])
    bar.pack(fill="x")
    tk.Label(bar, text="COMPARE · %d takes" % n, bg=C["panel"], fg=C["ink"],
             font=fh).pack(side="left", padx=10, pady=6)
    play_btn = tk.Button(bar, text="PLAY", bg=C["panel2"], fg=C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=12, pady=2, borderwidth=0)
    play_btn.pack(side="right", padx=10)
    play_btn.configure(command=_toggle)
    tk.Label(bar, text="one PLAY drives every clip · shorter clips hold their last frame",
             bg=C["panel"], fg=C["faint"], font=fsm).pack(side="right", padx=6)

    cols = tk.Frame(win, bg=C["bg"])
    cols.pack(fill="both", expand=True, padx=10, pady=10)
    for it, path in takes:
        col = tk.Frame(cols, bg=C["panel"], highlightthickness=1,
                       highlightbackground=C["line"])
        col.pack(side="left", fill="both", expand=True, padx=4)
        box = tk.Frame(col, bg=C["panel"], width=320, height=300)
        box.pack(pady=(10, 6))
        box.pack_propagate(False)
        prev = tk.Label(box, bg=C["panel"], text="", fg=C["faint"], font=fsm)
        prev.pack(fill="both", expand=True)
        is_vid = bool(path and path.lower().endswith((".mp4", ".webm")))
        frames = []
        if path:
            if is_vid:
                d = os.path.join(tempfile.gettempdir(), "forge_lab_compare_%s" % it.get("id"))
                shutil.rmtree(d, ignore_errors=True)
                os.makedirs(d, exist_ok=True)
                try:
                    r = weirdo_player_frames(path, d)
                    if r.returncode == 0:
                        for x in sorted(x for x in os.listdir(d) if x.endswith(".png")):
                            fr = thumb_photo(os.path.join(d, x), 300)
                            if fr is not None:
                                frames.append(fr)
                except Exception as exc:
                    _log_err("compare frames failed: %r" % exc)
            else:
                img = thumb_photo(path, 320)
                if img is not None:
                    frames.append(img)
        if frames:
            prev.configure(image=frames[0], text="")
            prev.image = frames[0]
            refs.append(prev.image)
        else:
            prev.configure(text="[video]" if is_vid else "[no preview]")
        tk.Label(col, text=(it.get("label") or "take"), bg=C["panel"], fg=C["ink"],
                 font=fsm, wraplength=330, justify="left").pack(anchor="w", padx=10)
        meta = "seed %s · %s steps · %s" % (
            it.get("seed"), it.get("steps", ""),
            ("%sx%s" % (it.get("width"), it.get("height"))) if it.get("width") else "—")
        tk.Label(col, text=meta, bg=C["panel"], fg=C["muted"], font=fsm,
                 justify="left").pack(anchor="w", padx=10)
        line = take_lineage(it)
        if line:
            tk.Label(col, text=line, bg=C["panel"], fg=C["muted"], font=fsm,
                     justify="left").pack(anchor="w", padx=10)
        rrow = tk.Frame(col, bg=C["panel"])
        rrow.pack(anchor="w", padx=10, pady=(4, 2))
        star = tk.Button(rrow, text="★ keep", bg=C["panel2"], fg=C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=8, pady=2, borderwidth=0)
        rej = tk.Button(rrow, text="✕ reject", bg=C["panel2"], fg=C["ink"],
                        activebackground=C["rust"], activeforeground="#fff",
                        relief="flat", font=fsm, padx=8, pady=2, borderwidth=0)
        star.pack(side="left", padx=(0, 6))
        rej.pack(side="left")
        p = {"id": it.get("id"), "frames": frames, "rating": take_rating(it),
             "prev": prev, "star": star, "reject": rej}
        star.configure(command=lambda p=p: _set(p, 1))
        rej.configure(command=lambda p=p: _set(p, -1))
        _paint(p)
        players.append(p)
        state["max"] = max(state["max"], len(frames))

    if not _runnable():
        play_btn.configure(state="disabled")

    bc = ctx.get("btn_colors")
    if bc:
        try:
            bc(win)
        except Exception:
            pass
    for p in players:                    # re-assert ON state after the blanket repaint
        _paint(p)

    win.bind("<Escape>", lambda _e: _close())
    win.protocol("WM_DELETE_WINDOW", _close)
    # driver exports
    win.cmp_players = players
    win.cmp_players_state = state
    win.cmp_play_btn = play_btn
    win.cmp_toggle_play = _toggle
    win.cmp_refs = refs
    return win


def recover_orphans(skip=None):
    """Collect runs that finished while this app was not watching.

    forge completes a prompt whether or not Forge Lab is open. If the app quit
    mid-run, or abandoned a submission, the image sat on forge forever while the
    index kept it as 'running'. Sweep and pull across whatever is genuinely done.
    """
    skip = set(skip or ())
    recovered = []
    queued = set()
    try:
        q = get_json("/queue", timeout=20)
        queued = {it[1] for it in (q.get("queue_running") or [])}
        queued |= {it[1] for it in (q.get("queue_pending") or [])}
    except Exception:
        pass
    for it in read_index():
        if it.get("images") or it.get("orphan_checked"):
            continue
        pid = it["id"]
        if pid in skip or pid in queued:
            continue                       # still in flight — not an orphan yet
        try:
            rec = get_json("/history/%s" % pid, timeout=25).get(pid)
        except Exception:
            continue                       # forge unreachable — retry next sweep
        if not rec:
            _index_update(pid, orphan_checked=True, status="not on forge")
            continue
        st = (rec.get("status") or {}).get("status_str")
        if st == "success":
            m = collect(pid)
            if m and m.get("images"):
                recovered.append(pid)
            else:
                _index_update(pid, orphan_checked=True)
        elif st == "error":
            _index_update(pid, orphan_checked=True, status="error")
    return recovered


SETTINGS = os.path.join(DATA, "settings.json")


def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_settings(d):
    try:
        with open(SETTINGS, "w") as f:
            json.dump(d, f, indent=1)
    except Exception:
        pass


def words_of_recording(path):
    """The take whose AUDIO IS this file — i.e. the words of the song being COVERED.

    Ryan: "not for the last song for the cover song". With a recording picked, the words he
    wants are that recording's, and whenever Forge Lab made the recording the take already
    carries them. Returns None for a recording this app did not make (an outside file) —
    that is a normal answer, not an error, and the UI says so rather than guessing.

    Compared on the RESOLVED path, because the same file is reachable as ~/..., a symlink,
    or a runs/<id>/ relative entry.
    """
    p = str(path or "").strip()
    if not p:
        return None
    try:
        want = os.path.realpath(os.path.expanduser(p))
    except Exception:
        return None
    if not os.path.exists(want):
        return None
    try:
        for it in read_index():
            if it.get("mode") != "song":
                continue
            for f in (it.get("images") or []):
                fn = f.get("file") if isinstance(f, dict) else None
                if not fn:
                    continue
                try:
                    here = os.path.realpath(os.path.join(RUNS, it.get("id") or "", fn))
                except Exception:
                    continue
                if here == want:
                    return it
    except Exception as exc:
        _log_err("could not look up the covered song's words: %r" % exc)
    return None


def last_song_take():
    """The most recent SONG take that has its words recorded, or None.

    The index has carried `style` and `lyrics` on every song run since the SONG section
    shipped, so putting the words back in the boxes is a LOOKUP — there is never a reason to
    retype lyrics you have already used. Pure (no Tk), so tools/song_matrix.py can test it.
    """
    try:
        # ⚠️ NO reversed(): _index_insert() PREPENDS, so read_index() is already NEWEST
        # FIRST. iterating it reversed handed back the OLDEST song with words — a bug that
        # would have quietly restored the wrong lyrics.
        for it in read_index():
            if it.get("mode") == "song" and (it.get("lyrics") or it.get("style")):
                return it
    except Exception as exc:
        _log_err("could not read the saved song words: %r" % exc)
    return None


# ------------------------------------------------- characters (a reusable setup) ----------
# A CHARACTER is a saved SETUP, not a picture: the identity prompt block, the seed, the
# optional LoRA and its strength, and ONE canonical still. Picking one loads all of it, so
# "shoot this character in a new scene" stops meaning "re-type the identity block and go
# hunting for the right reference again".
#
# ⚠️ The still is COPIED into the character's own folder, never referenced in place. Takes can
# be pruned (see the take-delete path) and a character pointing into runs/ would silently lose
# its face — the one asset it cannot regenerate.
#
# Layout:  characters/<slug>/character.json  +  characters/<slug>/still.png
CHARACTERS = os.path.join(DATA, "characters")
CHAR_NONE = "(none)"
try:
    os.makedirs(CHARACTERS, exist_ok=True)
except Exception:
    pass


def char_slug(name):
    """Folder-safe slug for a character name.

    Deliberately hand-rolled rather than `re`: this file does not import `re`, and adding an
    import to the shared header is a collision risk with the other agent for no real gain.
    """
    out = []
    for ch in str(name).strip().lower():
        out.append(ch if (ch.isalnum() or ch in "-_") else "-")
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s or "character"


def char_dir(slug):
    return os.path.join(CHARACTERS, slug)


def read_character(slug):
    """The saved dict, or None. Never raises — a garbled file must not take the window down."""
    try:
        with open(os.path.join(char_dir(slug), "character.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def list_characters():
    """[(slug, name)] sorted by name. Never raises."""
    out = []
    try:
        for entry in os.listdir(CHARACTERS):
            if os.path.isdir(os.path.join(CHARACTERS, entry)):
                meta = read_character(entry) or {}
                out.append((entry, meta.get("name") or entry))
    except Exception:
        pass
    return sorted(out, key=lambda t: str(t[1]).lower())


def write_character(slug, meta, still_src=None):
    """Save a character. `still_src` is COPIED to still.png inside the character's folder."""
    d = char_dir(slug)
    os.makedirs(d, exist_ok=True)
    meta = dict(meta)
    meta["slug"] = slug
    if still_src and os.path.exists(still_src):
        # ⚠️ SAVING AN EXISTING CHARACTER USED TO SILENTLY DROP ITS STILL. _fill() sets the
        # editor's still to the character's OWN still.png, so a plain SAVE passed that path
        # straight back in - and shutil.copyfile(src, src) raises SameFileError, which the bare
        # except below swallowed into `meta.pop("still")`. The PNG stayed on disk but the
        # character stopped pointing at it, and NOTHING said so. Same file = already in place.
        _dst = os.path.join(d, "still.png")
        try:
            if os.path.abspath(still_src) != os.path.abspath(_dst):
                shutil.copyfile(still_src, _dst)
            meta["still"] = "still.png"
        except Exception:
            meta.pop("still", None)
    elif not meta.get("still"):
        meta["still"] = None
    with open(os.path.join(d, "character.json"), "w") as f:
        json.dump(meta, f, indent=1)
    return d


def character_still(slug):
    """Absolute path to the character's OWN copy of its still, or None."""
    meta = read_character(slug) or {}
    n = meta.get("still")
    if not n:
        return None
    p = os.path.join(char_dir(slug), n)
    return p if os.path.exists(p) else None


def delete_character(slug):
    """Remove one character folder.

    GUARDED to paths inside CHARACTERS — same shape and same reason as the take-delete guard:
    the slug is user text, and `shutil.rmtree` on a joined path is how a stray name eats a
    directory it has no business touching.
    """
    d = os.path.abspath(char_dir(slug))
    root = os.path.abspath(CHARACTERS) + os.sep
    if not d.startswith(root):
        raise ValueError("refusing to delete outside %s: %s" % (root, d))
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------- character OPTIONS ---------------------------------
# A picker, not a blank page. Every value below is drawn from the roster that ALREADY EXISTS in
# drafts/ (bighead, basset ears, tonsure, goiter, combover, slab head, tiny top...) rather than
# invented here — the vocabulary is Ryan's own.
#
# ⚠️ The composed block is a STARTING POINT, not a cage. It lands in an EDITABLE box, and a pick
# re-composes only while that box still matches what was last composed — wording you have
# hand-edited is never silently overwritten.
# ---- CHARACTER · CATEGORICAL axes (dropdowns) ------------------------------------
# These are CHOICES, not quantities: you cannot slide "claymation" to +2.
CHAR_OPTIONS = [
    ("Style",
     ["photorealistic",
     "glossy modern 3D animated-film render",
     "1990s hand-drawn cel animation, flat colours, thick ink outlines",
     "1980s Japanese anime cel, painted background",
     "1930s rubber-hose cartoon, black and white, pie-cut eyes",
     "1950s UPA modernist cartoon, flat geometric shapes",
     "American superhero comic book, bold inks and halftone dots",
     "newspaper strip cartoon, cross-hatched linework",
     "claymation stop-motion, visible thumbprints",
     "needle-felted wool puppet",
     "oil painting, heavy impasto brushwork",
     "watercolour and ink sketch",
     "charcoal drawing on toned paper",
     "19th-century tintype photograph, sepia, vignetted edges",
     "faded 1970s Polaroid snapshot, warm colour cast",
     "airbrushed 1980s fantasy paperback cover",
     "cut-paper collage",
     "8-bit pixel art, limited palette",
     "16-bit SNES sprite art",
     "1997 low-poly PlayStation 1 render, affine texture warping"]),
    ("Framing",
     ["full body shot, the whole figure head to feet in frame",
     "bust portrait, head and shoulders only",
     "three-quarter shot, head to mid-thigh"]),
    ("Subject",
     ["a middle-aged man",
     "a young man",
     "an elderly man",
     "a young woman",
     "a middle-aged woman",
     "an elderly woman",
     # the runway build - tall, angular, hollow-cheeked
     "a tall androgynous fashion model with sharp cheekbones",
     "a striking young model with hard, angular features",
     "a supermodel with a chiselled jaw and hollow cheeks",
     "a willowy teenage model, all elbows and knees",
     "a powerful woman with a severe jaw",
     # funny
     "an alarmingly tall, very thin man",
     "a tiny round man",
     "a gaunt man in his nineties",
     "an enormous man built like a wardrobe",
     "a small, nervous man",
     "a person of entirely indeterminate age and gender",
     "a child of about six",
     "a frail little old woman"]),
    ("Skin",
     ["with fair skin",
     "with light olive skin",
     "with medium brown skin",
     "with deep brown skin",
     "with pale freckled skin",
     "with porcelain skin",
     "with warm golden-brown skin",
     "with rich deep brown skin and a smooth sheen",
     "with very pale, almost translucent skin",
     "with deep bronze, sun-tanned skin",
     "with ruddy, weathered skin",
     "with skin like polished mahogany",
     "with a sallow, unhealthy complexion",
     "with a shiny, sweat-slicked face",
     "with a blotchy, sunburnt face",
     "with greyish, waxy skin",
     "with skin that has never once seen the sun"]),
    ("Hair",
     ["short dark hair, neatly combed",
     "thinning grey hair, receding",
     "thick reddish-brown hair tied back",
     "long straight dark hair, severe",
     "bald on top with hair at the sides",
     "short cropped grey hair",
     "a wild unkempt shock of hair",
     "slicked-back black hair",
     # the haircuts that actually walk a runway
     "a sharp platinum-blonde pixie cut",
     "an asymmetric jet-black bob with a blunt fringe",
     "long glossy hair swept back, wet-look",
     "hair scraped back into a tight bun",
     "a towering, gravity-defying beehive",
     "a glossy curtain of straight hair down to the waist",
     "a severe buzz cut",
     "an enormous round afro",
     "a tall pompadour",
     # funny
     "a mullet, short at the front and long at the back",
     "hair dyed a violent shade of orange",
     "curly ginger hair",
     "a tight little perm",
     "waist-length silver hair, unbrushed"]),
    ("Facial hair",
     ["clean shaven",
     "with heavy dark stubble",
     "with a neat moustache",
     "with a full beard",
     "with sideburns and a moustache",
     "with a goatee"]),
    ("Wardrobe",
     ["a plain grey cable-knit cardigan over a collared shirt, brown trousers and sensible shoes",
     "a charcoal suit with a plain dark tie",
     "a navy mechanic's coverall over a grey t-shirt, work boots",
     "a blue denim jacket over a white t-shirt, dark jeans",
     "an olive canvas jacket over a brown shirt",
     "a plain white button-up shirt and dark trousers",
     "period colonial dress: breeches, waistcoat and a long coat",
     "a plain grey tracksuit",
     # high fashion - what actually walks a runway
     "a sharply tailored black suit worn open over a bare chest",
     "an oversized cream trench coat over a fine knit",
     "a severe minimalist column dress in bone white",
     "a boxy cropped leather jacket over a sheer top",
     "a floor-length coat in electric blue with enormous shoulders",
     "a bias-cut satin slip dress",
     "an avant-garde layered black outfit with an asymmetric hem",
     "a three-piece velvet suit in deep purple",
     "a long shearling coat over leather trousers",
     # funny
     "a complete 1970s brown polyester leisure suit",
     "a cheap ill-fitting suit two sizes too large",
     "a stained bathrobe and slippers",
     "a bright yellow raincoat and matching sou'wester hat",
     "a lurid Hawaiian shirt and cargo shorts",
     "a full Renaissance jester's motley with bells",
     "a full cowboy outfit: hat, boots and bolo tie",
     "a suit of armour worn over ordinary clothes",
     "an oversized hoodie with the hood up",
     "a Victorian mourning dress, all black, with a veil",
     "a full tweed shooting outfit with a deerstalker"]),
    ("Accessory",
     ["nothing extra",
     "wearing wire-rimmed glasses",
     "wearing thick black glasses",
     "wearing a flat cap",
     "holding a plain paper coffee cup"]),
    ("Feature",
     ["",
      "a head tilted permanently to one side",
      "a blocky, cubic head with hard flat planes",
      "wildly mismatched eyes, one far larger than the other",
      "long antenna-like eyebrows projecting out from his brow",
      "one single eyebrow stretching all the way to his temple",
      "a gigantic walrus moustache",
      "hair shaped like a hat, a solid brimmed mass on top of his head",
      "hair built into a shelf that juts out over his forehead",
      "a tonsure, bald on the crown with hair ringing the sides",
      "a comb-over stretched across a bald crown",
      "vast sagging jowls",
      "a great goitre bulging from his throat",
      "protruding buck teeth jutting past his lower lip",
      "a vast smooth domed forehead, bald on top",
      # volumes NO SLIDER covers - hands, teeth, throat, brow, cheeks, feet, tongue. The sliders
      # already own head/nose/mouth/ears/chin/neck/brow/eyes, so a Feature naming one of those
      # risks printing it TWICE when both are set.
      "hands twice the size of his head",
      "a set of teeth like a row of tombstones",
      "an Adam's apple like a golf ball",
      "a forehead like a drive-in movie screen",
      "cheeks like a hamster hoarding nuts",
      "protruding collarbones like coat hangers",
      "feet like a clown's, far too long",
      "a tongue far too large for his mouth",
      "a pair of enormous leathery hands",
      "one vast, wet, blinking eye in the centre of his forehead",
      "a permanent open-mouthed gasp of astonishment",
      "a nose like a large raw potato"]),
    ("Setting",
     ["a seamless plain light-grey studio backdrop",
     "a flat chroma-key GREEN SCREEN backdrop, evenly lit and shadowless, for compositing",
     "a flat pure WHITE seamless backdrop",
     "a flat black seamless backdrop",
     "a seamless blue chroma-key screen",
     "a plain concrete wall",
     "an outdoor basketball court under an overcast sky",
     "a 1990s American pizzeria interior",
     "a modern city street in daylight",
     "a woodland clearing at golden hour"]),
    ("Lighting",
     ["soft even studio lighting",
     "flat shadowless front lighting",
     "hard directional key light with deep shadows",
     "dramatic low-key rim lighting",
     "warm golden-hour sunlight",
     "cool overcast daylight"]),
]

# Every axis opens on its first value, which is deliberately the plain/ordinary one.
CHAR_OPTION_DEFAULTS = {axis: vals[0] for axis, vals in CHAR_OPTIONS}

# ---- CHARACTER · the still's canvas follows its FRAMING ---------------------------
# Measured on forge, same prompt, 25 steps, SEED PINNED (character stills):
#   1024x1024   1.05 MP  figure  894 px tall  15.1 s
#   1024x2048   2.10 MP  figure 1799 px tall  34.0 s
#   1440x2880   4.15 MP  figure 2671 px tall  88.8 s   <- +39% pixels on the figure
#   2048x2048   4.19 MP  figure 1817 px tall  88.7 s
# ⚠️ THE FIGURE IS LIMITED BY CANVAS HEIGHT, NOT MEGAPIXELS. A 4 MP SQUARE gives a standing
# figure only ~1% more pixels than a 2 MP PORTRAIT (1817 vs 1799) for 2.6x the time - the extra
# megapixels go into empty space either side of him. A 4 MP PORTRAIT is a different story: it
# pushes the long edge to 2880 and the figure to 2671 px. So a TALL framing gets a TALL 4 MP
# canvas; the BUST gets the 4 MP square.
#
# ⚠️ 2880 IS PAST THE 2048 THE CODE CALLS QWEN'S "NATIVE 2K CEILING" - so I checked whether
# those pixels are real detail or upscaling, deterministically (the vision reads contradicted
# each other, and seed -1 made an earlier pair two DIFFERENT pictures):
#     per-pixel high-frequency energy in the head, native:  2 MP 11.64 | 4 MP 11.84
#     the same, after downscaling the 4 MP head to 2 MP size:          12.51 = 1.08x
# A real 4 MP render stays AT LEAST as sharp when normalised down; upscaled mush would come out
# SOFTER. It comes out 8% SHARPER - the 2880 edge carries genuine detail.
# Peak VRAM measured: 16.6 GB at 2 MP, 17.1 GB at 4 MP, against 24.6 GB. Neither is close.
CHAR_STILL_SIZE = {
    "full body shot, the whole figure head to feet in frame": (1440, 2880),
    "three-quarter shot, head to mid-thigh": (1440, 2880),
    "bust portrait, head and shoulders only": (2048, 2048),
}
CHAR_STILL_SIZE_FALLBACK = (1440, 2880)


# Iteration ladder. FULL is the default and the one to KEEP; the others exist so a quick
# composition check costs seconds instead of a minute and a half. Each step keeps the framing's
# aspect ratio and halves the megapixels, then snaps to a multiple of 32.
#   full body   Full 1440x2880 (4.1 MP)  Fast 1024x2048 (2.1 MP)  Draft 704x1440 (1.0 MP)
#   bust        Full 2048x2048 (4.2 MP)  Fast 1440x1440 (2.1 MP)  Draft 1024x1024 (1.0 MP)
CHAR_STILL_SCALE = (("Full", 1.0), ("Fast", 0.7071), ("Draft", 0.5))


def char_still_size(framing, scale="Full"):
    """(w, h) for a character still. An unknown or missing framing gets the full-body size -
    the tall one, which is the safe shape for a figure of unknown extent. An unknown scale
    gets FULL, so a bad value can never silently shrink a keeper render."""
    base = CHAR_STILL_SIZE.get((framing or "").strip(), CHAR_STILL_SIZE_FALLBACK)
    factor = dict(CHAR_STILL_SCALE).get(scale, 1.0)
    if factor == 1.0:
        return base

    def snap(v):
        return max(32, int(round(v * factor / 32.0)) * 32)

    return (snap(base[0]), snap(base[1]))


# ---- WEIRDO · the one-press studio -------------------------------------------------
# The roster idiom, in code: NORMAL IN EVERY OTHER AXIS, ONE VOLUME DIAL BROKEN, played
# absolutely straight. So a roll picks a plain, sincere base and then exaggerates exactly ONE
# thing - never a scatter of unrelated weirdness, which reads as noise instead of comedy.
# Values at the front of each list are the ordinary ones, so "ordinary" = take from the front.
WEIRDO_ORDINARY = {"Skin": 5, "Hair": 8, "Facial hair": 6, "Wardrobe": 8,
                   "Setting": 1, "Lighting": 2}

# Deadpan actions. The comedy is that they do it with total sincerity, so these are small,
# deliberate and mundane - never a gag.
# ⚠️ GENDER-NEUTRAL on purpose: the Subject pool includes men and women, so a roll that
# produced "a powerful woman" used to get the action "shakes HIS head".
WEIRDO_ACTIONS = [
    "stands perfectly still, blinking slowly",
    "walks slowly toward the camera",
    "turns slowly to face the camera",
    "slowly raises one hand in a stiff wave",
    "looks down at their own hands, then back up",
    "takes one deliberate step forward and stops",
    "slowly shakes their head from side to side",
    "leans forward slightly, peering at the camera",
    "shifts their weight from one foot to the other",
    "slowly turns their head to look off to one side",
]

# ---- WEIRDO · VEINS -----------------------------------------------------------------
# Ryan, 2026-10-05: "update the weirdo section ... where the generated characters are more
# like this" - the deadpan vein: FORMAL EUROPEAN DRESS, a GRAND EUROPEAN SQUARE,
# a SIGNATURE REPEATED MOTION, played deadpan. Added as a VEIN so the classic modern/studio
# weirdos stay; you pick which one you are rolling. ONE volume dial still, in both veins.
WEIRDO_VEIN_DEFAULT = "Formal-European"
WEIRDO_VEINS = ("Formal-European", "Modern")

# The Formal-European base. Deliberately NOT added to CHAR_OPTIONS: this slice changes only
# what the WEIRDO roll composes, so the Character window's vocabulary and its guards (the
# "wearing " prefix rule, the per-list floors, the no-duplicates rule) are untouched.
WEIRDO_EURO_WARDROBE = (
    "a black tailcoat, white dress shirt, white bow tie and white gloves",
    "a black top hat, dark grey tailcoat, grey striped trousers and a polished cane",
    "an immaculate white-tie evening suit with a white waistcoat",
    "a long double-breasted overcoat with a fur collar, worn over a silk cravat",
    "a three-piece tweed suit with a gold pocket-watch chain and a walking cane",
    "a scholarly black frock coat with a high collar and a silk cravat",
    "a braided hussar's jacket with a fur-trimmed pelisse over one shoulder",
    "an embroidered matador's traje de luces, skin-tight and heavy with gold thread",
    "a deep-green velvet evening jacket with a silk scarf and patent shoes",
    "a Victorian mourning suit, all black, with a tall hat and a black cane",
)
WEIRDO_EURO_SETTING = (
    "a grand baroque stone square in Vienna, an ornate fountain behind them",
    "a cobbled square in Bruges, stepped-gable brick houses and a canal behind them",
    "a square in Prague's Old Town, the astronomical clock tower rising behind them",
    "a Parisian street of pale stone facades with wrought-iron balconies",
    "a Lisbon square of azulejo-tiled walls with a yellow tram behind them",
    "a Venetian calle beside a narrow canal, small bridges and shuttered windows",
    "a vast empty opera-house foyer with a marble staircase",
)
WEIRDO_EURO_LIGHTING = ("cool overcast daylight",)
# The signature motion IS the format: one repeated, deliberate routine performed at nobody.
# Still deadpan, still gender-neutral (the Subject pool has women in it).
WEIRDO_ACTIONS_EURO = (
    "conducts an invisible orchestra with total concentration",
    "waltzes alone with an invisible partner",
    "fences an invisible opponent, lunging and parrying",
    "doffs their hat to an empty street, then does it again",
    "passes an invisible cape at an invisible bull",
    "sweeps an invisible floor with slow, dignified strokes",
    "plays an invisible violin with their eyes closed",
    "salutes an empty square and holds it a beat too long",
    "conducts with one hand while checking an invisible pocket watch",
    "bows deeply to nobody, three times, without hurrying",
)

# Video settings for this window. Canvas: only 544x736 has actually been timed on the r2v
# path here, so it is the default; the rest are offered, not blessed.
WEIRDO_VIDEO_CANVAS = (
    ("Portrait 544x736", (544, 736)),
    ("Portrait 640x960", (640, 960)),
    ("Square 832x832", (832, 832)),
    ("Landscape 832x480", (832, 480)),
)
WEIRDO_VIDEO_CANVAS_DEFAULT = "Portrait 544x736"
# Length is in SECONDS and goes through h3_frames(), because H3 only accepts frame counts of
# the form n*17+5 - ask for a raw frame count and the clip comes back a different length.
WEIRDO_VIDEO_LENGTHS = (("3 s", 3.0), ("5 s", 5.0), ("8 s", 8.0))
WEIRDO_VIDEO_LENGTH_DEFAULT = "5 s"
# The app's own Standard. 4 (Draft) is what VIDEO_QUALITY itself calls "the FACE GOES SOFT.
# Blocking only." - never the default for something meant to be kept.
WEIRDO_VIDEO_STEPS = 12


def roll_weirdo(rng=None, vein=None):
    """One press of ROLL A WEIRDO -> (picks, action).

    Takes an rng so a test can PIN the roll; without one it uses the module RNG.
    `vein` is "Formal-European" (the default) or "Modern". An unknown value falls back to the
    default, so a bad value can never crash a roll.

    BOTH veins keep the roster idiom: normal in every other axis, ONE volume dial broken. The
    only difference is the BASE - what "normal" is dressed in and standing in - and the pool of
    ACTIONS. Formal-European: period tailoring, a European square, European overcast light, and
    a signature motion (conducting, waltzing) instead of a micro-action.
    """
    r = rng or random
    vein = vein if vein in WEIRDO_VEINS else WEIRDO_VEIN_DEFAULT
    euro = vein == "Formal-European"
    picks = {}
    for axis, vals in CHAR_OPTIONS:
        if axis == "Style":
            picks[axis] = "photorealistic"        # the roster is hyper-real, always
        elif axis == "Framing":
            picks[axis] = vals[0]                 # full body - the video wants the whole figure
        elif axis == "Feature":
            picks[axis] = ""                      # decided below: THE one exaggeration
        elif axis == "Accessory":
            picks[axis] = ("nothing extra" if r.random() < 0.6 else r.choice(vals[1:]))
        elif euro and axis == "Wardrobe":
            picks[axis] = r.choice(WEIRDO_EURO_WARDROBE)
        elif euro and axis == "Setting":
            picks[axis] = r.choice(WEIRDO_EURO_SETTING)
        elif euro and axis == "Lighting":
            picks[axis] = r.choice(WEIRDO_EURO_LIGHTING)
        elif axis in WEIRDO_ORDINARY:
            picks[axis] = r.choice(vals[:WEIRDO_ORDINARY[axis]])
        else:
            picks[axis] = r.choice(vals)
    # THE ONE VOLUME DIAL: half the time a Feature, half a slider driven to an extreme.
    feats = [v for v in dict(CHAR_OPTIONS).get("Feature", []) if v]
    if feats and (not CHAR_SLIDERS or r.random() < 0.5):
        picks["Feature"] = r.choice(feats)
    else:
        axis, _phrases = r.choice(CHAR_SLIDERS)
        picks[axis] = r.choice((-3, 3))
    return picks, r.choice(WEIRDO_ACTIONS_EURO if euro else WEIRDO_ACTIONS)


def apply_weirdo_fit(picks, action, fit):
    """Merge the model's fit (director.Director.weirdo_fit) onto a random roll -> (picks, action).

    ⚠️ THE ONE-THING RULE IS ENFORCED HERE, IN CODE, not trusted to the model: every slider and
    the Feature are cleared, then the model's single `absurd` trait becomes the Feature. So a
    described roll breaks exactly one thing, the same as a random one. An empty field keeps the
    roll's value; Style and Framing are never touched (hyper-real, full body — the video needs
    the whole figure). Guarded by tools/weirdo_matrix.py.

    ⚠️ The fit is FINAL for the look axes: an empty Skin/Hair/Facial hair/Accessory/Lighting
    means NONE, not "keep the roll". Keeping the roll's value is how "a woman who runs a bakery"
    came back "with a goatee" in the first live run. Only a missing/empty Subject, Wardrobe or
    Setting (the schema requires them) falls back to the roll, so a block can never lose its
    who / clothes / place.
    """
    fit = fit or {}
    out = dict(picks or {})
    for axis, key, must in (("Subject", "subject", True), ("Skin", "skin", False),
                            ("Hair", "hair", False), ("Facial hair", "facial_hair", False),
                            ("Wardrobe", "wardrobe", True), ("Accessory", "accessory", False),
                            ("Setting", "setting", True), ("Lighting", "lighting", False)):
        v = (fit.get(key) or "").strip()
        if v or not must:
            out[axis] = v
    absurd = (fit.get("absurd") or "").strip()
    if absurd:
        for a in CHAR_SLIDER_ORDER:
            out.pop(a, None)
        out["Feature"] = absurd
    act = (fit.get("action") or "").strip() or action
    return out, act


def weirdo_player_frames(video, outdir):
    """Pull the frames the in-window player cycles through, and RETURN the CompletedProcess.

    \u26a0\ufe0f `-vsync` is NOT accepted by every ffmpeg build - the one on this Mac rejects it
    outright ("Unrecognized option 'vsync'"), and because that only runs once a video exists,
    the failure is invisible until then. The `fps` FILTER states the intent directly and needs
    no output-side flag at all. Kept module-level so a test can run the REAL command.
    """
    return subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video,
                           "-vf", "fps=12,scale=-2:300",
                           os.path.join(outdir, "f_%04d.png")],
                          capture_output=True, text=True, timeout=300)


def weirdo_video_params(prompt, canvas_key, length_key, seed=-1):
    """The exact dict submit("video", ...) needs for a character video on the r2v path."""
    w, h = dict(WEIRDO_VIDEO_CANVAS).get(canvas_key, dict(WEIRDO_VIDEO_CANVAS)[
        WEIRDO_VIDEO_CANVAS_DEFAULT])
    secs = dict(WEIRDO_VIDEO_LENGTHS).get(length_key, dict(WEIRDO_VIDEO_LENGTHS)[
        WEIRDO_VIDEO_LENGTH_DEFAULT])
    return {"prompt": prompt, "width": w, "height": h, "steps": WEIRDO_VIDEO_STEPS,
            "length": h3_frames(secs), "seed": seed, "video_mode": "r2v",
            "audio": True, "lora": None, "lora_strength": 1.0,
            "prefix": "forge_lab_weirdo"}

# ---- CHARACTER · PROPORTIONAL axes (sliders) --------------------------------------
# Ryan: "instead of drop downs for body features it should be a scroll bar like in
# video games character creation ... the drop downs seem very limiting."
#
# A slider is a NUMBER, and a number is what a scripted 3D build can actually consume,
# so these are the same parameters a Blender build would read. Index 0..6 maps to levels
# -3..+3. Level 0 (index 3) is deliberately EMPTY, so a neutral character composes to just
# style + framing + subject + clothes + setting - no filler.
CHAR_SLIDERS = [
    ("Height",
     ["extremely short and squat",
     "very short",
     "below average height",
     "",
     "above average height",
     "very tall",
     "gigantic, towering over everyone"]),
    ("Frame",
     ["skeletal and gaunt",
     "very thin",
     "slim",
     "",
     "heavy-set",
     "very heavy-set",
     "enormously fat"]),
    ("Shoulders",
     ["tiny sloping shoulders",
     "narrow shoulders",
     "slightly narrow shoulders",
     "",
     "broad shoulders",
     "very broad shoulders",
     "enormous hulking shoulders"]),
    ("Arm length",
     ["very short arms",
     "short arms",
     "slightly short arms",
     "",
     "long arms",
     "very long arms",
     "absurdly long dangling arms"]),
    ("Arm thickness",
     ["spindly thin arms",
     "thin arms",
     "slightly thin arms",
     "",
     "thick arms",
     "very thick arms",
     "enormously muscular arms"]),
    ("Leg length",
     ["very short legs",
     "short legs",
     "slightly short legs",
     "",
     "long legs",
     "very long legs",
     "absurdly long stilt-like legs"]),
    ("Leg thickness",
     ["spindly thin legs",
     "thin legs",
     "slightly thin legs",
     "",
     "thick legs",
     "very thick legs",
     "enormous pillar-like legs"]),
    ("Head size",
     ["a tiny pin-head",
     "a very small head",
     "a slightly small head",
     "",
     "a large head",
     "a very large head",
     "a COLOSSAL oversized head"]),
    ("Head shape",
     ["a long narrow face",
     "a narrow face",
     "a slightly narrow face",
     "",
     "a broad face",
     "a very broad face",
     "a wide square slab of a face"]),
    ("Brow",
     ["a small receding brow",
     "a low brow",
     "a slightly low brow",
     "",
     "a heavy brow",
     "a very heavy brow",
     "a massive jutting brow ridge"]),
    ("Eyes",
     ["tiny squinting eyes",
     "small close-set eyes",
     "slightly small eyes",
     "",
     "large eyes",
     "very large eyes",
     "enormous bulging eyes"]),
    ("Nose",
     ["a tiny button nose",
     "a small nose",
     "a slightly small nose",
     "",
     "a large nose",
     "a very large nose",
     "a huge hooked nose"]),
    ("Mouth",
     ["a tiny pursed mouth",
     "a small mouth",
     "a slightly small mouth",
     "",
     "a wide mouth",
     "a very wide mouth",
     "a vast gaping mouth"]),
    ("Ears",
     ["tiny ears",
     "small ears",
     "slightly small ears",
     "",
     "large ears",
     "large protruding ears",
     "comically ENORMOUS protruding ears, like dinner plates"]),
    ("Chin",
     ["a weak receding chin",
     "a small chin",
     "a slightly small chin",
     "",
     "a strong jaw",
     "a heavy square jaw",
     "a massively jutting jaw"]),
    ("Neck",
     ["almost no neck",
     "a short neck",
     "a slightly short neck",
     "",
     "a long neck",
     "a very long neck",
     "an ABSURDLY long thick neck, a foot tall"]),
]

CHAR_SLIDER_MAP = {axis: phrases for axis, phrases in CHAR_SLIDERS}
CHAR_SLIDER_ORDER = [axis for axis, _ in CHAR_SLIDERS]
CHAR_SLIDER_LEVELS = (-3, -2, -1, 0, 1, 2, 3)
CHAR_SLIDER_NEUTRAL = "ordinary"     # shown by a slider whose phrase at 0 is empty


def char_slider_level(picks, axis):
    """The -3..+3 level of a slider axis. Unset, or garbage, reads as 0 (ordinary)."""
    try:
        return max(-3, min(3, int((picks or {}).get(axis, 0))))
    except Exception:
        return 0


def char_slider_phrase(picks, axis):
    """The words a slider contributes at its current level, or '' at neutral."""
    spec = CHAR_SLIDER_MAP.get(axis)
    if not spec:
        return ""
    return spec[char_slider_level(picks, axis) + 3]


# ⚠️ TWO tails, not one. A still wants him looking down the lens; a VIDEO must not -
# "looking straight at the camera" fights every motion you ask for, and "85mm lens / sharp focus"
# is still-photography language. Composing ONE block for both meant every character loaded into a
# video mode arrived with an instruction to stare and NOTHING to do.
CHAR_IDENTITY_TAIL = ("Deadpan neutral expression, absolutely no smile, looking straight at the "
                      "camera, sharp focus, 85mm lens.")
CHAR_VIDEO_TAIL = ("Relaxed natural expression, steady posture, cinematic lighting, shallow depth "
                   "of field, 35mm film.")


def compose_character_prompt(picks, for_video=False, action=""):
    """The identity block from the picks. `for_video` swaps the still-photo tail
    for a motion-friendly one and splices in the character's ACTION."""
    pk = dict(CHAR_OPTION_DEFAULTS)
    pk.update({k: v for k, v in (picks or {}).items() if v is not None})

    def g(axis):
        return (pk.get(axis) or "").strip()

    bits = [g("Style") or "photorealistic", g("Framing") or "full body shot",
            g("Subject") or "a person"]
    # proportions first, head-down, then what they are wearing and where they are
    # ⚠️ The Feature is checked FIRST and a slider phrase is skipped when the Feature already
    # says it, more strongly. Sliding Ears to +3 AND picking the big-ears quirk used to print
    # the ears twice in one prompt.
    _feat = g("Feature").lower()
    for _a in CHAR_SLIDER_ORDER:
        _ph = char_slider_phrase(pk, _a)
        if not _ph:
            continue
        if " ".join(_ph.lower().split()[:3]) in _feat:
            continue
        bits.append(_ph)
    for a in ("Skin", "Hair", "Facial hair"):
        if g(a):
            bits.append(g(a))
    if g("Wardrobe"):
        bits.append("wearing " + g("Wardrobe"))
    # the absurd one goes LATE, so it is the last thing read before the pose line
    for a in ("Accessory", "Feature", "Setting", "Lighting"):
        if g(a) and g(a) != "nothing extra":
            bits.append(g(a))
    look = ", ".join(b for b in bits if b) + "."
    if for_video:
        act = (action or "").strip().rstrip(".")
        if act:
            return look + " " + act + ". " + CHAR_VIDEO_TAIL
        return look + " " + CHAR_VIDEO_TAIL
    return look + " " + CHAR_IDENTITY_TAIL


# ------------------------------------------------- prompt enhancer (Qwen PE) --
# qwen3.5_9b_qwen_image_2.1_pe_* are prompt REWRITERS (a small LLM), driven by the
# TextGenerate node. Verified against this rig:
#   * CLIPLoader type MUST be "stable_diffusion" (the generic text-model path)
#   * thinking=True is REQUIRED: with it off the model's reasoning leaks into the
#     text you would paste as a prompt
#   * t2i returns a clean prompt in ~9s at max_length=512
#   * pe_i2i is NOT USABLE yet: it never closes its thinking block, so it burns the
#     whole budget (18k chars of reasoning at 4096, 151s) and returns an empty
#     answer. Edit modes therefore polish the wording with pe_t2i for now.
PE_MODELS = {"t2i": "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
             "i2i": "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors"}
PE_MAX_LEN = {"t2i": 1536, "i2i": 4096}
# Measured on the dog prompt, which is what set these numbers:
#   512  -> EMPTY          (reasoned to the ceiling, never wrote the answer)
#   1024 -> CUT OFF        ('The image has a cinematic,')
#   1536 -> COMPLETE       (2167 chars, 15s, ends on a full sentence)
# polish_prompt() retries once at 2x if the answer comes back empty or cut off.
PE_TIMEOUT = 240          # a polish takes ~15s; never hang the button for 15 minutes
PLOG = os.path.join(DATA, "polish_debug.log")


def _plog(msg):
    """ALWAYS-ON trace of the polish path (a few lines per press).

    The GUI has no console. Three presses in a row came back 'polishing…' forever
    with nothing in any log, because run_state() reports an unreachable /history as
    'unknown' and the loop just spun. If it happens again, this says where.
    """
    try:
        if os.path.exists(PLOG) and os.path.getsize(PLOG) > 60000:
            os.remove(PLOG)
        with open(PLOG, "a") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


ERRORLOG = os.path.join(DATA, "forge_lab_errors.log")


def _log_err(text):
    """ALWAYS-ON error log.

    Thread crashes are invisible in a windowed .app — stderr goes nowhere — so a
    failing worker thread (load_takes, collect, submit) could leave the UI silently
    stale with nothing recorded. This catches those.
    """
    try:
        with open(ERRORLOG, "a") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def _thr_hook(args):
    _log_err("THREAD %s crashed: %s: %s" % (getattr(args.thread, "name", "?"),
                                            args.exc_type.__name__, args.exc_value))


threading.excepthook = _thr_hook


def _find_graph(payload):
    """History stores the payload that was SENT, wrapped in a tuple-ish list. Dig
    out the {node_id: {"class_type": ...}} graph."""
    if isinstance(payload, dict):
        if any(isinstance(x, dict) and "class_type" in x for x in payload.values()):
            return payload
        for x in payload.values():
            r = _find_graph(x)
            if r:
                return r
    elif isinstance(payload, list):
        for x in payload:
            r = _find_graph(x)
            if r:
                return r
    return None


def _payload_client(payload):
    if isinstance(payload, dict):
        for k, x in payload.items():
            if k == "client_id" and isinstance(x, str):
                return x
            r = _payload_client(x)
            if r:
                return r
    elif isinstance(payload, list):
        for x in payload:
            r = _payload_client(x)
            if r:
                return r
    return None


def _latest_polish_result(text, cl):
    """Fallback: forge may well have finished the run even though our polling could
    not see it. Find the newest successful run that is OUR polish graph — same idea,
    same client id — and take its answer. This is what should have saved all three
    of the stuck attempts."""
    try:
        h = get_json("/history?max_items=25", timeout=30)
    except Exception as exc:
        _plog("history fallback unreachable: %r" % exc)
        return None
    best = None
    for pid, v in h.items():
        if not isinstance(v, dict):
            continue
        if (v.get("status") or {}).get("status_str") != "success":
            continue
        g = _find_graph(v.get("prompt"))
        if not g:
            continue
        tg = next((n for n in g.values() if isinstance(n, dict)
                   and n.get("class_type") == "TextGenerate"), None)
        if not tg or (tg.get("inputs") or {}).get("prompt") != text:
            continue
        if _payload_client(v.get("prompt")) != cl:
            continue
        for o in (v.get("outputs") or {}).values():
            if o.get("text"):
                cand = clean_polished(o["text"])
                if cand:                      # history is oldest-first: last wins
                    best = cand
    if best:
        _plog("fallback: recovered the answer from forge history")
    return best


def build_polish_graph(text, pe="t2i", ref_name=None, max_length=512):
    g = {"1": {"class_type": "CLIPLoader",
               "inputs": {"clip_name": PE_MODELS[pe], "type": "stable_diffusion",
                          "device": "default"}},
         "2": {"class_type": "TextGenerate",
               "inputs": {"clip": ["1", 0], "prompt": text, "max_length": max_length,
                          "sampling_mode": "off", "thinking": True,
                          "use_default_template": True}},
         "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 0]}}}
    if ref_name:
        g["4"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        g["2"]["inputs"]["image"] = ["4", 0]
    return g


def clean_polished(t):
    """Keep only the prompt.

    Three real hazards, all seen in testing:
      * the text arrives as a LIST, not a string;
      * the chat template can emit a thinking block (full-width-pipe delimited);
      * t2i sometimes appends 'If you'd like, I can also generate...'
    Match on the plain ASCII words inside the delimiters and slice after them —
    NOT on a literal delimiter, which risks being empty. `str.find("")` always
    succeeds and never advances, which hangs the loop (learned the hard way).
    """
    if isinstance(t, list):
        t = "\n".join(str(x) for x in t)
    t = t or ""
    for word in ("end_of_thought", "end_of_thinking"):
        if word in t:
            t = t.rsplit(word, 1)[-1]
            t = t.lstrip("|>\uff5c \t\r\n")          # drop the closing delimiter
    for tail in ("\nIf you'd like", "\nIf you would like", "\nLet me know",
                 "\nWould you like", "\nI can also"):
        i = t.find(tail)
        if i != -1:
            t = t[:i]
    return t.strip()


def _polish_error(pid):
    try:
        h = get_json("/history/%s" % pid, timeout=20).get(pid) or {}
        for m in ((h.get("status") or {}).get("messages") or []):
            if isinstance(m, list) and m[0] == "execution_error":
                return ((m[1] or {}).get("exception_message") or "").splitlines()[0]
    except Exception:
        pass
    return ""


class PolishEmpty(Exception):
    """The enhancer spent its whole budget reasoning and never wrote the prompt."""


def _looks_truncated(t):
    """The model stops writing when it runs out of budget, which cuts the rewrite
    mid-sentence ('The image has a cinematic,'). A finished one ends with
    terminal punctuation."""
    t = (t or "").rstrip()
    if not t:
        return True
    return t[-1] not in ".!\"')]"


def polish_prompt(text, mode="t2i", ref_path=None):
    """Rewrite a prompt with the enhancer.

    The answer competes with the model's thinking block for the same token budget:
    at 512 a real prompt came back EMPTY, and at 1024 it came back CUT OFF
    mid-sentence. So try the default budget, then retry once with double the room.
    """
    pe = "t2i"          # pe_i2i parked: see the note above
    ref_name = None
    if pe == "i2i" and ref_path:
        ref_name = upload_image(ref_path)
    last = None
    for maxlen in (PE_MAX_LEN[pe], PE_MAX_LEN[pe] * 2):
        try:
            return _polish_once(text, pe, ref_name, maxlen)
        except PolishEmpty as exc:
            last = exc
    raise RuntimeError("%s — try a shorter or more concrete idea" % last)


def _polish_once(text, pe, ref_name, max_length):
    g = build_polish_graph(text, pe=pe, ref_name=ref_name, max_length=max_length)
    r = post_json("/prompt", {"prompt": g, "client_id": CLIENT_ID})
    pid = r.get("prompt_id")
    if not pid:
        _plog("forge rejected the polish request")
        raise RuntimeError("forge did not accept the polish request")
    _plog("submitted %s  pe=%s max_length=%d" % (pid[:8], pe, max_length))
    t0 = time.time()
    last = None
    unreachable = None
    while time.time() - t0 < PE_TIMEOUT:
        time.sleep(2)
        state = run_state(pid)["state"]          # never raises: 'unknown' on failure
        if state != last:
            _plog("  %5.1fs  state=%s" % (time.time() - t0, state))
            last = state
        if state == "done":
            break
        if state in ("error", "gone"):
            raise RuntimeError(_polish_error(pid) or "the enhancer %s" % state)
        if state == "unknown":
            # /history is unreachable. Do NOT wait out the whole timeout: the run may
            # already have finished, so give forge's history a chance to answer it.
            if unreachable is None:
                unreachable = time.time()
            elif time.time() - unreachable > 20:
                out = _latest_polish_result(text, CLIENT_ID)
                if out:
                    return out
                raise RuntimeError("lost contact with forge while polishing "
                                   "(no answer in its history either)")
        else:
            unreachable = None
    else:
        out = _latest_polish_result(text, CLIENT_ID)
        if out:
            return out
        raise RuntimeError("the enhancer timed out after %ds" % PE_TIMEOUT)
    hist = get_json("/history/%s" % pid, timeout=25).get(pid) or {}
    for o in (hist.get("outputs") or {}).values():
        if o.get("text"):
            out = clean_polished(o["text"])
            if out:
                if _looks_truncated(out):
                    _plog("cut off at %d chars (budget %d) — retrying with more room"
                          % (len(out), max_length))
                    raise PolishEmpty("the rewrite was cut off at the %d-token budget"
                                      % max_length)
                _plog("returning %d chars (direct)" % len(out))
                return out
            raise PolishEmpty("the enhancer burned the whole %d-token budget "
                              "reasoning and never wrote the prompt" % max_length)
    out = _latest_polish_result(text, CLIENT_ID)
    if out:
        return out
    raise RuntimeError("the enhancer returned no text")


# --------------------------------------------------- live progress (websocket)
PROGRESS = {}
WS_OK = False


def _ws_once():
    key = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection((F_HOST, F_PORT), timeout=10)
    s.sendall(("GET /ws?clientId=%s HTTP/1.1\r\nHost: %s:%d\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (CLIENT_ID, F_HOST, F_PORT, key)).encode())

    def rx(n):
        buf = b""
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c:
                raise IOError("closed")
            buf += c
        return buf
    hdr = b""
    while b"\r\n\r\n" not in hdr:
        c = s.recv(4096)
        if not c:
            raise IOError("closed")
        hdr += c
    if b"101" not in hdr.split(b"\r\n")[0]:
        raise IOError("bad handshake")
    global WS_OK, WS_CONNECTED
    WS_OK = True
    WS_CONNECTED = True
    s.settimeout(90)
    while True:
        h = rx(2)
        op, ln = h[0] & 0x0F, h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", rx(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", rx(8))[0]
        payload = rx(ln) if ln else b""
        if op == 0x1:
            global WS_LAST
            WS_LAST = time.time()
            try:
                m = json.loads(payload.decode("utf-8", "replace"))
            except Exception:
                continue
            d = m.get("data") or {}
            pid = d.get("prompt_id")
            if m.get("type") == "progress" and pid:
                PROGRESS[pid] = {"value": d.get("value"), "max": d.get("max"),
                                 "node": d.get("node")}
            elif m.get("type") == "executing" and pid:
                e = PROGRESS.setdefault(pid, {})
                e["node"] = d.get("node")
        elif op == 0x9:
            mask = os.urandom(4)
            s.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask +
                      bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        elif op == 0x8:
            return


def _ws_loop():
    global WS_CONNECTED
    while True:
        try:
            resolve_forge()
            _ws_once()
        except Exception:
            time.sleep(4)
        finally:
            WS_CONNECTED = False


# =============================================================== long video (stage 1)
# The approved Stage 1 design: D's split (queue left, review bench right) + B's row-per-scene
# queue + C's step bar, with the treatment demoted to ONE line so it stops fighting the queue
# for the left column. Adds a per-scene PROMPT EDITOR (Ryan's ask) under the bench.
#
# Stage 1 does: treatment (locked), a scene list, per-scene takes with REROLL/KEEP, a scene
# prompt editor, and plan.json on disk. Stage 2 (stitch) is present as a step and as an honest
# REFUSAL when the keeper set is incomplete — it does not write a video yet.
#
# Deliberately module-level (not nested in launch()) so it can be driven headless by a test:
# everything it needs from the app arrives in `ctx`.
LONG_EPISODES = os.path.join(DATA, "episodes")
LONG_DEFAULT_SCENE_S = 14.81          # 3 chained S2V chunks = 237 frames @16 fps
# ---- the H3 engine (24 fps + the character LoRA + the audio guide) ------------------------
# The clip Ryan judged to FOLLOW the track was H3 r2v + character_lora_v2 + AddGuide(audio).
H3_LONGFORM_LORA = "character_lora_v2.safetensors"
# Per-piece character LoRA. ⚠️ This is NOT always the main character — a cowboy, a stranger or a faceless
# sketch wants NO LoRA at all, and forcing one puts a face on something that has none.
CHARACTER_LORAS = {"(none)": None,
                   "character_lora_v2": "character_lora_v2.safetensors"}


def long_lora(plan):
    """The LoRA for this piece. Absent = the default character LoRA (existing plans keep working);
    an explicit "(none)" means none, which is what a piece without the main character must get."""
    h = (plan.get("treatment") or {})
    if "lora" in h:
        return CHARACTER_LORAS.get(h["lora"], h["lora"] or None)
    return H3_LONGFORM_LORA
H3_LONGFORM_STEPS = 12
H3_LONGFORM_SIZE = (544, 736)         # the canvas every H3 measurement here used (0.400 MP)
# H3's own ceiling is ~362 frames (15.08 s at 24 fps), which is the "15 s max per scene".
H3_SCENE_FRAMES = {1: 124, 2: 217, 3: 355}      # 5.17 / 9.04 / 14.79 s
# ⚠️ DEAD CODE, kept only as a record: H3_SCENE_LABELS was written for this and NEVER USED, which
# is why the LENGTH dropdown showed the WAN labels on H3 — '4.81 s  (1 chunk)' while H3 rendered
# 5.17 s. The live labels are derived from H3_LADDER_FRAMES below.
H3_SCENE_LABELS = {1: "5.17 s  (H3)", 2: "9.04 s  (H3)", 3: "14.79 s  (H3)"}

# ── THE H3 LENGTH LADDER ──────────────────────────────────────────────────────────────────
# ⚠️ H3 accepts ONLY frame counts of the form n*17+5, so a scene length is QUANTISED to 0.708 s
# rungs — it is not a continuum. A slider that displayed 7.5 s while the model rendered 7.29 s
# would be the "control that lies" failure this app keeps producing, so the slider moves along
# the LADDER and every label is derived from the same table the renderer uses.
H3_LADDER_FRAMES = tuple(n * 17 + 5 for n in range(4, 22))     # 73 .. 362  =  3.04 .. 15.08 s
# ⚠️ EXACT seconds, NOT rounded. round(226/24, 4) = 9.4167 disagrees with the renderer by 3.3e-5,
# which is small but is exactly the readout-vs-render lie this ladder exists to remove. The UI
# formats to 2 dp for display; the stored value stays exact.
H3_LADDER_S = tuple(f / 24.0 for f in H3_LADDER_FRAMES)


def h3_ladder_index(secs):
    """The rung nearest `secs`. The slider IS an index, so this is only for putting a stored or
    typed value back onto the ladder — never for deciding what to render."""
    if not secs:
        return 0
    want = h3_frames(secs)
    return min(range(len(H3_LADDER_FRAMES)),
               key=lambda i: abs(H3_LADDER_FRAMES[i] - want))
ENGINES = {"h3": "H3 · 24fps · LoRA · audio guide", "wan": "WAN 2.2 S2V · 16fps"}
# Per-piece canvas. H3 takes any multiple of 32, but VRAM and time scale with PIXELS — and
# H3's cost is super-linear in them — so the two 0.40 MP options are the safe pair and the
# native 1344x768 is offered as a heavy one. 544x736 is the size everything here was measured
# at; 832x480 is the SAME pixel count in landscape, so it should cost about the same.
H3_CANVAS = {
    "Portrait 544×736  (0.40 MP · measured)": (544, 736),
    "Landscape 832×480  (0.40 MP · same pixels)": (832, 480),
    "Portrait 640×960  (0.61 MP)": (640, 960),
    "Native 1344×768  (1.03 MP · heavy)": (1344, 768),
}
H3_CANVAS_DEFAULT = "Portrait 544×736  (0.40 MP · measured)"


def long_canvas(plan):
    """The canvas for this piece, per its treatment. 544x736 is the measured default."""
    lab_ = (plan.get("treatment") or {}).get("canvas", H3_CANVAS_DEFAULT)
    return H3_CANVAS.get(lab_, H3_CANVAS[H3_CANVAS_DEFAULT])


def long_scene_slice(bed_path, start_s, dur_s, out_path):
    """⚠️ THE ALIGNMENT FIX. `MiniMaxH3AddGuide.audio` is "Soundtrack to anchor starting at the
    same frame index, cropped to the video's remaining duration" — the anchor is at the START
    of the generated clip. So a scene handed the WHOLE bed track performs its opening seconds,
    and only a piece whose first scene starts at 0:00 lines up. Every scene must be handed ITS
    OWN window of the track, sliced at that scene's offset in the finished piece.
    """
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.3f" % max(0.0, start_s),
                    "-t", "%.3f" % dur_s, "-i", bed_path, out_path],
                   capture_output=True, check=True)
    return out_path


def long_scene_seconds_for(chunks, engine, secs=0.0):
    """How long a scene is, per engine. ONE place, so the slice and the graph agree.

    `secs` is the scene's OWN H3 length from the LENGTH slider. ⚠️ 0.0 means UNSET, and an unset
    scene falls back to the old `chunks` table — so a plan written before the slider existed
    renders byte-identically, and no migration has to rewrite a stored length.
    """
    if engine == "h3":
        if secs and float(secs) > 0:
            return h3_frames(float(secs)) / 24.0
        return H3_SCENE_FRAMES.get(int(chunks), 355) / 24.0
    return s2v_chunks_to_frames(int(chunks)) / S2V_FPS


def long_engine(plan):
    return (plan.get("treatment") or {}).get("engine", "h3")


def long_plan_path(name):
    return os.path.join(LONG_EPISODES, name, "plan.json")


# ── MOTION INTENT — the beat-level field the board never had ───────────────────────────────
# From the vendor's own beat-planning rule (brand-promo STEP 5/6): a beat names its MOTION
# INTENT and its place in the energy curve, not just what happens. Six words, the spec's own.
# ⚠️ AUTHORING ONLY. It does NOT reach the renderer: `_scene_video_plan` is untouched by it, so
# it can never block a scene or change what a render costs. Asserted in TESTLONG — if it ever
# leaks into the plan, a scene silently renders something else.
# Empty means unset: a scene with no intent must compose exactly as it did before this existed.
MOTION_INTENTS = ("setup", "anticipation", "commitment", "impact", "brake", "settle")
BEAT_UNSET = "\u2014"          # the dropdown's neutral entry; saved as ""


def long_scene_new(n):
    """The ONE place a scene dict is made. This literal was written out four times, and the board
    adds six fields to every scene — four copies drift the first time one of them is edited."""
    return {"n": n, "prompt": "", "chunks": 3, "takes": [], "keep": None,
            "still": None, "still_prompt": None,
            # ⚠️ A SECOND slot. `still` is where the shot STARTS (`first`/`ref`); `still_last` is
            # where it LANDS. `first` + `last` is the tightest thing fl2va can do, and it needs two
            # pictures — one field could not hold both.
            "still_last": None, "still_last_prompt": None,
            # ⚠️ KF roles are FLAGS, not a pick-one (D6.2).
            # ⚠️ Default NONE, not `first`. With `first` defaulted ON, the no-roles rows were
            # unreachable — so every fresh scene claimed a keyframe it did not have, and the
            # documented `r2v`/`t2v` behaviour was dead code. A piece nobody keyframes must render
            # exactly as it did before the board existed.
            "kf_first": False, "kf_last": False, "kf_ref": False, "kf_cont": False,
            # ⚠️ The BEAT fields — see MOTION_INTENTS. Empty by default so a neutral scene has no
            # filler, and so a plan written before these existed renders identically after.
            "motion": "", "color_state": "", "transition_out": "",
            # ⚠️ The scene's own H3 length, from the LENGTH slider. 0.0 = UNSET -> fall back to
            # `chunks`, so an old plan is unchanged (see long_scene_seconds_for).
            "secs": 0.0}


# ── The locked LOOK, as a DEFAULT ──────────────────────────────────────────────────────
# ⚠️ VERBATIM from a production style sheet. Not invented here and not paraphrased: this exact
# register produced the approved 90s-cel frames.
# ⚠️ It is the STYLE only. A character's identity deliberately does NOT live here.
# A Long video must be able to have no fixed character in it, so a character in the piece-wide block would
# be hard-coded into every scene of every piece. Identity comes from the reference still and from
# the scene prompts.
DEFAULT_STYLE_BLOCK = (
    "A 1990s hand-drawn cartoon animation cel in the style of 90s Nickelodeon "
    "(Hey Arnold, Doug): flat gouache colors, thick black ink outlines, muted "
    "earth-tone palette, smooth clean flat color fields. "
    "No photorealism, no 3D, no live-action footage, no cinematic depth of field, "
    "no lens flare, no glossy highlights, no film grain."
)


def long_plan_migrate(plan):
    """⚠️ Plans written before the board have no `still`/`still_prompt`, plans written before
    D6.2 have no KF roles, and plans written before the style block was wired have no
    `subject_block`. Backfill rather than crash: a KeyError inside a tkinter callback is a
    silent windowed death (see _log_exc)."""
    # ⚠️ setdefault is NOT enough here, and that is the whole point. Pieces written while the
    # block was wired-to-nothing have the KEY PRESENT AND EMPTY, so setdefault leaves them "" —
    # and an empty style block IS the bug (it is why a keyframe came back painterly). Fill when
    # empty; leave a block Ryan has actually written alone.
    if not (plan.get("subject_block") or "").strip():
        plan["subject_block"] = DEFAULT_STYLE_BLOCK
    for i, sc in enumerate(plan.get("scenes") or []):
        for k, v in (("n", i + 1), ("prompt", ""), ("chunks", 3), ("takes", []),
                     ("keep", None), ("still", None), ("still_prompt", None),
                     ("still_last", None), ("still_last_prompt", None),
                     # ⚠️ Default NONE, not `first` — see `long_scene_new`.
                     ("kf_first", False), ("kf_last", False), ("kf_ref", False),
                     ("kf_cont", False),
                     # ⚠️ The BEAT fields MUST be here as well as in `long_scene_new` — a plan
                     # already on disk has no such keys, and the first read of one raises KeyError
                     # inside a Tk callback, which `_log_exc` swallows into a dead window.
                     ("motion", ""), ("color_state", ""), ("transition_out", ""),
                     ("secs", 0.0)):
            sc.setdefault(k, v)
    # ⚠️ Scenes written while `kf_first` was DEFAULTED ON are pinned to True with nothing to
    # satisfy it, and now that a ticked role with no still is REFUSED, they would block every
    # scene. In a piece where not one still was ever drafted that True cannot be an intent —
    # nobody asks for a keyframe without making one. Clear it. A piece with even one still is left
    # completely alone, because then the ticks are somebody's actual choice.
    _scs = plan.get("scenes") or []
    if not any(sc.get("still") or sc.get("still_last") for sc in _scs):
        for sc in _scs:
            if sc.get("kf_first") and not sc.get("kf_cont"):
                sc["kf_first"] = False
    return plan


def long_plan_load(name):
    try:
        with open(long_plan_path(name)) as f:
            return long_plan_migrate(json.load(f))
    except Exception:
        return None


def long_plan_save(name, plan):
    d = os.path.join(LONG_EPISODES, name)
    os.makedirs(d, exist_ok=True)
    tmp = long_plan_path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(plan, f, indent=1)
    os.replace(tmp, long_plan_path(name))


def long_scene_frames(seconds):
    """Seconds -> chained S2V chunk count, using the app's own baked rule so the UI and the
    graph can never disagree: 1 chunk 4.81 s, 2 chunks 9.81 s, 3 chunks 14.81 s."""
    want = float(seconds)
    return max(1, min(S2V_MAX_CHUNKS, int(round(want / (S2V_CHUNK / S2V_FPS)))))


_AUDIO_PEAKS = {}          # keyed (path, mtime, width) — the strip repaints on every keystroke


def audio_peaks(path, width, sr=4000):
    """A REAL peak envelope for the audio strip: `width` floats 0..1, or None.

    ⚠️ The strip used to draw `abs(sin(x) * cos(x*0.4))` — a decorative pattern IDENTICAL for every
    episode, whatever audio was loaded. It read as a waveform and was not one. This decodes the
    actual track.

    ⚠️ PURE STDLIB ON PURPOSE. numpy is NOT in the PyInstaller bundle, so an `import numpy` here
    would pass every test on this Mac and then break the INSTALLED app. `array` is enough.

    ⚠️ Cached on path+mtime+width: render_bench runs on every keystroke in the prompt box.
    Returns None for a missing/unreadable/silent track so the caller can say so rather than draw
    a shape that means nothing.
    """
    import array as _array
    if not path or not os.path.exists(path):
        return None
    try:
        key = (path, os.path.getmtime(path), int(width))
    except OSError:
        return None
    if key in _AUDIO_PEAKS:
        return _AUDIO_PEAKS[key]
    out = None
    try:
        w = int(width)
        if w > 0:
            p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le",
                                "-ac", "1", "-ar", str(int(sr)), "-"],
                               capture_output=True, check=True)
            raw = p.stdout
            a = _array.array("h")
            a.frombytes(raw[:len(raw) - (len(raw) % 2)])
            n = len(a)
            if n >= w:
                step = max(1, n // w)
                cols = []
                for c in range(w):
                    lo = c * step
                    hi = min(n, lo + step)
                    if hi <= lo:
                        cols.append(0.0)
                        continue
                    seg = a[lo:hi]
                    cols.append(max(max(seg), -min(seg)) / 32768.0)
                mx = max(cols)
                if mx > 1e-6:                 # all-zero audio is NOT a waveform
                    out = [c / mx for c in cols]
    except Exception:
        out = None
    _AUDIO_PEAKS[key] = out
    return out


def long_scene_seconds(chunks):
    """Seconds for a given CHUNK COUNT. ⚠️ Distinct from long_scene_frames(), which goes the
    OTHER way (seconds -> chunks). Mixing them up is what printed "0.06 s scenes": the chunk
    count got divided by fps as if it were a frame count."""
    return s2v_chunks_to_frames(int(chunks)) / S2V_FPS


def long_thumb(path, box=110):
    """A small tk image for a still or the LAST frame of a clip. Returns None on failure —
    a missing thumbnail must never take the workspace down."""
    try:
        import tkinter as tk
        src = path
        if str(path).lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
            src = os.path.join(DATA, "longthumb.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-1", "-i", path,
                            "-vframes", "1", "-vf", "scale=220:-1", src],
                           capture_output=True, timeout=60)
            if not os.path.exists(src):
                return None
        # ⚠️ thumb_photo, never tk.PhotoImage(file=...) — see its docstring: a transparent
        # PNG hangs Tk 9 inside the alpha region build, and this runs on the board.
        return thumb_photo(src, box)
    except Exception:
        return None


def long_last_frame(src, out):
    """The LAST frame of a rendered clip, written to `out` as a PNG — the frame a
    `continue from prev` scene starts FROM. ⚠️ Same ffmpeg recipe `long_thumb` uses (-sseof -1),
    but this one KEEPS the file: the video graph needs it as an input, not as a thumbnail."""
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-1", "-i", src,
                        "-vframes", "1", out], capture_output=True, timeout=120)
    except Exception:
        return None
    return out if os.path.exists(out) else None



def scoped_wheel(win, canvas):
    """Wheel + trackpad scrolling for ONE canvas inside a sub-window (Toplevel).

    ⚠️ NEVER bind_all / unbind_all the wheel from a sub-window. bind_all is APP-WIDE and
    unbind_all DELETES the main window's handler rather than restoring it — the old
    Enter/Leave pattern left the main window with no wheel binding at all after one pass of
    the pointer (probed 2026-10-10). Binding on the TOPLEVEL instead catches events from
    every widget inside it (the toplevel is in each child's bindtags) and touches nothing
    app-wide. Returning "break" stops the main window's 'all'-tag handler running too.

    Scrolls only when the pointer is inside `canvas`; a Text box scrolls itself.
    Guarded by tools/ui_matrix.py §1–§2."""
    import tkinter as tk

    def _hit(w):
        if isinstance(w, str):
            try:
                w = canvas.nametowidget(w)
            except Exception:
                return False
        while w is not None:
            if isinstance(w, tk.Text):
                return False
            if w is canvas:
                return True
            w = getattr(w, "master", None)
        return False

    def _on(ev, touchpad):
        if not _hit(ev.widget):
            return None
        try:
            d = int(getattr(ev, "delta", 0) or 0)
        except Exception:
            d = 0
        if not d:
            return "break"
        try:
            if touchpad:
                # TIP 684: Δy is the LOW 16 bits, signed (same decode as the main window)
                low = d & 0xFFFF
                dy = low - 0x10000 if low >= 0x8000 else low
                sr = [float(v) for v in str(canvas.cget("scrollregion")).split()]
                h = (sr[3] - sr[1]) if len(sr) == 4 else 0.0
                if h > canvas.winfo_height() > 0:
                    top = canvas.yview()[0]
                    canvas.yview_moveto(max(0.0, min(1.0, top + (-dy) / h)))
            else:
                canvas.yview_scroll(-1 if d > 0 else 1, "units")
        except Exception:
            pass
        return "break"

    win.bind("<MouseWheel>", lambda e: _on(e, False))
    win.bind("<TouchpadScroll>", lambda e: _on(e, True))


def open_long_video(root, ctx):
    """The Long video workspace. `ctx` carries the app's palette, fonts and engine calls."""
    import tkinter as tk
    from tkinter import ttk, filedialog

    C, F, mono = ctx["C"], ctx["fonts"], ctx["mono"]
    submit, run_state, collect = ctx["submit"], ctx["run_state"], ctx["collect"]
    f, fh, fsm = F["f"], F["fh"], F["fsm"]
    fmono = mono["m"]
    S2V_SIZE = ctx.get("s2v_size", (640, 640))

    win = tk.Toplevel(root)
    win.title("Long video — Stage 1")
    win.configure(bg=C["bg"])
    # ⚠️ geometry() is a REQUEST, and Tk does NOT grow a Toplevel past it — it starves the CONTENT
    # instead, silently (the content asked for 930 px inside a 900 px window). So size from the
    # real screen, leave margins for the menu bar and the title bar, and let TESTLONG assert that
    # the content's REQUESTED size fits inside the REALIZED one.
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1470, _sw - 40), min(860, _sh - 120)))

    def lam(parent, text, **kw):
        kw.setdefault("bg", C["panel"])
        kw.setdefault("fg", C["muted"])
        return tk.Label(parent, text=text, font=kw.pop("font", fsm), **kw)

    st = {"name": "untitled", "sel": 0, "filter": False, "drafting": False,
          "audio": None, "ref": None, "takes_open": None, "board": False,
          "kf_drafting": False, "loaded": False, "subject_block": ""}
    plan = {"name": "untitled", "audio": None, "ref": None,
            "subject_block": DEFAULT_STYLE_BLOCK,
            "treatment": {"w": S2V_SIZE[0], "h": S2V_SIZE[1], "fps": int(S2V_FPS),
                          "chunks": 3, "steps": S2V_STEPS, "cfg": S2V_CFG,
                          "shift": S2V_SHIFT, "h3_steps": H3_LONGFORM_STEPS,
                                   "engine": "h3"},
            "scenes": [long_scene_new(i + 1) for i in range(12)]}

    def _subject():
        """The per-piece STYLE / SUBJECT block (§3 item 5) — pinned in front of every prompt.
        ⚠️ It belongs to the PIECE, not to a scene, and it is what actually enforces the locked
        look. It used to be a hard-coded "" handed in through ctx, so the treatment's own line
        "5 · subject block: pinned in front of every scene prompt" was a claim about nothing —
        which is how a keyframe came back painterly instead of 90s cel."""
        return (plan.get("subject_block") or st.get("subject_block")
                or ctx.get("subject_block") or "")

    # ── C · step bar, always visible ────────────────────────────────────────────────
    # ── 3 · the EPISODE: a name, so there can be more than one ─────────────────────
    hd = tk.Frame(win, bg=C["bg"])
    hd.pack(fill="x", padx=14, pady=(12, 0))
    tk.Label(hd, text="EPISODE", bg=C["bg"], fg=C["muted"], font=fmono).pack(side="left")
    name_e = tk.Entry(hd, width=22, bg="#100e0c", fg=C["ink"], relief="flat", font=fmono,
                      insertbackground=C["ink"])
    name_e.insert(0, st["name"])
    name_e.pack(side="left", padx=7)

    def open_episode(*_a):
        nm = (name_e.get() or "untitled").strip().replace("/", "-")
        got = long_plan_load(nm)
        if not got:
            # ⚠️ OPEN means OPEN. The old `plan["scenes"] = []` branch then made render_bench
            # index [0] on an empty list — a swallowed IndexError and a permanently frozen
            # window — AND quietly saved a blank episode under a name that never existed.
            ep_lbl.configure(text="no episode called %r — NEW makes one" % nm, fg=C["ochre"])
            return
        st["name"] = nm
        plan.clear(); plan.update(got)
        st["audio"] = plan.get("audio"); st["ref"] = plan.get("ref")
        st["subject_block"] = plan.get("subject_block") or ""
        st["loaded"] = True          # ⚠️ only now may refresh() save
        st["sel"] = 0
        ep_lbl.configure(text="opened %s" % nm, fg=C["faint"])
        refresh()

    def new_episode():
        nm = (name_e.get() or "untitled").strip().replace("/", "-")
        st["name"] = nm
        plan.clear()
        plan.update({"name": nm, "audio": st.get("audio"), "ref": st.get("ref"),
                     "subject_block": st.get("subject_block") or "",
                     "treatment": {"w": S2V_SIZE[0], "h": S2V_SIZE[1], "fps": int(S2V_FPS),
                                   "chunks": 3, "steps": S2V_STEPS, "cfg": S2V_CFG,
                                   "shift": S2V_SHIFT, "h3_steps": H3_LONGFORM_STEPS,
                                   "engine": "h3"},
                     "scenes": [long_scene_new(i + 1) for i in range(12)]})
        st["sel"] = 0
        st["loaded"] = True
        refresh()

    tk.Button(hd, text="OPEN", bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
              activeforeground="#fff", relief="flat", font=fsm, padx=9, pady=1,
              borderwidth=0, command=open_episode).pack(side="left", padx=(0, 5))
    tk.Button(hd, text="NEW", bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
              activeforeground="#fff", relief="flat", font=fsm, padx=9, pady=1,
              borderwidth=0, command=new_episode).pack(side="left")
    ep_lbl = tk.Label(hd, text="", bg=C["bg"], fg=C["faint"], font=fsm)
    ep_lbl.pack(side="left", padx=10)

    # ── C · step bar, always visible ────────────────────────────────────────────────
    steps = tk.Frame(win, bg=C["panel"], highlightthickness=1,
                     highlightbackground=C["line"])
    steps.pack(fill="x", padx=14, pady=(12, 6))
    step_btns = {}
    for i, (k, v) in enumerate((("STEP 1", "Treatment"), ("STEP 2", "Scenes"),
                                ("STEP 3", "Stitch"))):
        b = tk.Frame(steps, bg=C["panel"], cursor="hand2")
        b.pack(side="left", fill="x", expand=True)
        tk.Label(b, text=k, bg=C["panel"], fg=C["faint"], font=fmono).pack(
            anchor="w", padx=12, pady=(7, 0))
        tk.Label(b, text=v, bg=C["panel"], fg=C["ink"], font=fh).pack(anchor="w", padx=12)
        h = tk.Label(b, text="", bg=C["panel"], fg=C["faint"], font=fsm)
        h.pack(anchor="w", padx=12, pady=(0, 7))
        sep = tk.Frame(steps, bg=C["line"], width=1)
        sep.pack(side="left", fill="y")
        step_btns[v] = (b, h)

    # ── treatment: ONE line + a disclosure ─────────────────────────────────────────
    tt = tk.Frame(win, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    tt.pack(fill="x", padx=14)
    tline = tk.Frame(tt, bg=C["panel"])
    tline.pack(fill="x", padx=12, pady=7)
    lam(tline, "TREATMENT", fg=C["ochre"], font=fmono).pack(side="left")
    tsum = tk.Label(tline, text="", bg=C["panel"], fg=C["muted"], font=fmono)
    tsum.pack(side="left", padx=10)
    tdet_btn = tk.Button(tline, text="details ▾", bg=C["panel2"], fg=C["ink"],
                         activebackground=C["rust"], activeforeground="#fff", relief="flat",
                         font=fsm, padx=8, pady=1, borderwidth=0)
    tdet_btn.pack(side="right")

    # ⚠️ TWO ROWS, and this is NOT cosmetic. Everything below requests ~1290 px against a 1126 px
    # row, so on ONE row Tk never allocates space to the tail: REF…, AUDIO…, STYLE… and the
    # ENGINE / QUALITY / CANVAS / CHARACTER menus were ALL UNMAPPED — width 1, invisible, and no
    # error anywhere. The summary alone claimed 960 px of that row. A second row is what makes the
    # controls exist at all.
    tctl = tk.Frame(tt, bg=C["panel"])
    tctl.pack(fill="x", padx=12, pady=(0, 8))

    # Without these two the workspace cannot draft anything: submit() needs an uploaded audio
    # file and a reference still. They live on the TREATMENT line because they are part of the
    # locked treatment, not per-scene.
    def pick_audio_lv():
        got = filedialog.askopenfilename(
            title="Bed track for the whole piece — ONE continuous file",
            filetypes=[("Audio", "*.wav *.mp3 *.m4a *.flac *.ogg"), ("All files", "*.*")])
        if got:
            st["audio"] = got; plan["audio"] = got; refresh()
            refuse.pack_forget()

    def pick_ref_lv():
        got = filedialog.askopenfilename(
            title="Reference still — this still IS the character (no LoRA on this path)",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")])
        if got:
            st["ref"] = got; plan["ref"] = got; refresh()
            refuse.pack_forget()

    # ⚠️ Row 1, packed side="left" right after the summary. These LEAD the treatment area on
    # purpose: "how do I add a reference image or the audio track?" was the question this window
    # could not answer, because these three were being starved off the row entirely.
    tk.Button(tline, text="REF…", bg=C["panel2"], fg=C["ink"],
              activebackground=C["rust"], activeforeground="#fff", relief="flat",
              font=fsm, padx=8, pady=1, borderwidth=0,
              command=pick_ref_lv).pack(side="left", padx=(0, 6))
    tk.Button(tline, text="AUDIO…", bg=C["panel2"], fg=C["ink"],
              activebackground=C["rust"], activeforeground="#fff", relief="flat",
              font=fsm, padx=8, pady=1, borderwidth=0,
              command=pick_audio_lv).pack(side="left", padx=(0, 6))

    # ── the STYLE / SUBJECT block (§3 item 5) — the thing that enforces the look ────
    # ⚠️ It belongs to the PIECE. It was previously a hard-coded "" handed in through ctx, so
    # every prompt went out naked and the locked look was enforced by nothing at all.
    def edit_subject():
        top = tk.Toplevel(win)
        top.title("Style / subject block")
        top.configure(bg=C["bg"])
        top.transient(win)
        tk.Label(top, text="STYLE / SUBJECT BLOCK", bg=C["bg"], fg=C["ochre"],
                 font=fmono).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(top, text="Pinned in front of EVERY scene prompt — stills and video alike.\n"
                           "This is what enforces the look; empty means nothing does.",
                 bg=C["bg"], fg=C["muted"], font=fsm,
                 justify="left").pack(anchor="w", padx=16)
        if not st.get("loaded"):
            tk.Label(top, text="⚠️ No episode opened or created yet — this block is kept for a "
                               "NEW episode but will not be written to disk.",
                     bg=C["bg"], fg=C["ochre"], font=fsm,
                     justify="left").pack(anchor="w", padx=16, pady=(4, 0))
        txt = tk.Text(top, width=84, height=11, wrap="word", bg="#100e0c", fg=C["ink"],
                      relief="flat", font=f, insertbackground=C["ink"], padx=8, pady=6)
        txt.pack(padx=16, pady=(8, 0))
        txt.insert("1.0", _subject())

        def save():
            blk = txt.get("1.0", "end").strip()
            plan["subject_block"] = blk
            st["subject_block"] = blk
            top.destroy()
            refresh()

        row = tk.Frame(top, bg=C["bg"])
        row.pack(anchor="w", padx=16, pady=(10, 14))
        tk.Button(row, text="SAVE", bg=C["rust_deep"], fg="#fff", relief="flat", font=fsm,
                  padx=12, pady=6, borderwidth=0, command=save).pack(side="left")
        tk.Button(row, text="CANCEL", bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                  padx=12, pady=6, borderwidth=0,
                  command=top.destroy).pack(side="left", padx=(8, 0))

    tk.Button(tline, text="STYLE…", bg=C["panel2"], fg=C["ink"],
              activebackground=C["rust"], activeforeground="#fff", relief="flat",
              font=fsm, padx=8, pady=1, borderwidth=0,
              command=edit_subject).pack(side="left")

    # ── the ENGINE: part of the treatment, and it decides what every scene is made of ──
    tk.Label(tctl, text="ENGINE", bg=C["panel"], fg=C["ochre"], font=fmono).pack(
        side="left", padx=(0, 6))
    eng_var = tk.StringVar(value=ENGINES[long_engine(plan)])
    eng_menu = tk.OptionMenu(tctl, eng_var, *ENGINES.values(),
                             command=lambda *_: set_engine())
    eng_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                       highlightthickness=0, activebackground=C["rust"],
                       activeforeground="#fff", borderwidth=0, width=29,
                       highlightbackground=C["panel"])
    eng_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    # ⚠️ One 3090: switching engines EVICTS the loaded models and pays the reload again.
    # `estimate_seconds`' `warm_engine` term already prices this, but nothing ON SCREEN said why a
    # first H3 run costs more than the next one. A disabled menu entry costs no layout — and the
    # `tctl` row is already close to the window's width limit, so text packed into the row would
    # starve something (the exact bug that hid these menus once).
    eng_menu["menu"].add_command(
        label="· one 3090 — a switch reloads models (~40 s)", state="disabled")
    eng_menu.pack(side="left", padx=(0, 14))

    def set_engine():
        for k, lab_ in ENGINES.items():
            if lab_ == eng_var.get():
                plan.setdefault("treatment", {})["engine"] = k
        refresh()

    # ── H3 QUALITY. h3_graph turns the turbo distill ON at steps <= 4, so this is a PATH,
    # not a speed slider — the same rule S2V and WAN already follow. A long piece is drafted
    # on the cheap path and the keepers re-run on the final one.
    H3_Q = {"Draft · 4 steps (turbo)": 4, "Final · 12 steps": 12}
    hq_var = tk.StringVar(value=next(k for k, v in H3_Q.items()
                                     if v == H3_LONGFORM_STEPS))
    tk.Label(tctl, text="QUALITY", bg=C["panel"], fg=C["ochre"], font=fmono).pack(
        side="left", padx=(0, 6))
    hq_menu = tk.OptionMenu(tctl, hq_var, *H3_Q, command=lambda *_: set_h3q())
    hq_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                      highlightthickness=0, activebackground=C["rust"],
                      activeforeground="#fff", borderwidth=0, width=21,
                      highlightbackground=C["panel"])
    hq_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    hq_menu.pack(side="left", padx=(0, 14))

    cv_var = tk.StringVar(value=(plan.get("treatment") or {}).get("canvas",
                                                                 H3_CANVAS_DEFAULT))
    tk.Label(tctl, text="CANVAS", bg=C["panel"], fg=C["ochre"], font=fmono).pack(
        side="left", padx=(0, 6))
    cv_menu = tk.OptionMenu(tctl, cv_var, *H3_CANVAS, command=lambda *_: set_canvas())
    cv_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                      highlightthickness=0, activebackground=C["rust"],
                      activeforeground="#fff", borderwidth=0, width=34,
                      highlightbackground=C["panel"])
    cv_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    cv_menu.pack(side="left", padx=(0, 14))

    lo_var = tk.StringVar(value=(plan.get("treatment") or {}).get(
        "lora", H3_LONGFORM_LORA.replace(".safetensors", "")))
    tk.Label(tctl, text="CHARACTER", bg=C["panel"], fg=C["ochre"], font=fmono).pack(
        side="left", padx=(0, 6))
    lo_menu = tk.OptionMenu(tctl, lo_var, *CHARACTER_LORAS, command=lambda *_: set_lora())
    lo_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                      highlightthickness=0, activebackground=C["rust"],
                      activeforeground="#fff", borderwidth=0, width=20,
                      highlightbackground=C["panel"])
    lo_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    lo_menu.pack(side="left")

    def set_lora():
        plan.setdefault("treatment", {})["lora"] = lo_var.get()
        refresh()

    def set_canvas():
        plan.setdefault("treatment", {})["canvas"] = cv_var.get()
        refresh()

    def set_h3q():
        t = plan.setdefault("treatment", {})
        t["h3_steps"] = H3_Q.get(hq_var.get(), H3_LONGFORM_STEPS)
        t["turbo"] = int(t["h3_steps"]) <= 4
        refresh()
    tdet = tk.Label(tt, text="", bg=C["panel"], fg=C["muted"], font=fsm,
                    justify="left", anchor="w", wraplength=1080)

    def toggle_details():
        if tdet.winfo_ismapped():
            tdet.pack_forget(); tdet_btn.configure(text="details ▾")
        else:
            tdet.pack(fill="x", padx=12, pady=(0, 9)); tdet_btn.configure(text="details ▴")
    tdet_btn.configure(command=toggle_details)

    # ── the split ──────────────────────────────────────────────────────────────────
    frame = tk.Frame(win, bg=C["panel"], highlightthickness=1,
                     highlightbackground=C["line"])
    frame.pack(fill="both", expand=True, padx=14, pady=8)
    leftp = tk.Frame(frame, bg=C["panel"], width=430)
    leftp.pack(side="left", fill="y")
    leftp.pack_propagate(False)
    rightp = tk.Frame(frame, bg=C["panel"])
    rightp.pack(side="left", fill="both", expand=True)

    # ⚠️ Reserved at the BOTTOM of the bench and packed BEFORE anything else, so it can never be
    # squeezed out. It used to be packed LAST, and adding the still-prompt boxes pushed it clean
    # out of the window — the app's only error channel, invisible, silently. Packing it first with
    # side="bottom" makes it win the space contest against every later widget.
    refusewrap = tk.Frame(rightp, bg=C["panel"])
    refusewrap.pack(side="bottom", fill="x")

    # ⚠️ THE BENCH SCROLLS, and that is the real fix for this whole family of bugs. Every
    # "I can't see X" report on this window — the refusal label, the treatment controls, the
    # window not fitting — was Tk starving the tail of an over-subscribed container. The window is
    # RESIZABLE, so shrinking it by hand re-created exactly that. On a canvas nothing has to fit,
    # so nothing can be starved.
    rv_vsb = tk.Scrollbar(rightp, orient="vertical")
    rv_vsb.pack(side="right", fill="y")
    rv_canvas = tk.Canvas(rightp, bg=C["panel"], highlightthickness=0,
                          yscrollcommand=rv_vsb.set)
    rv_canvas.pack(side="left", fill="both", expand=True)
    rv_vsb.configure(command=rv_canvas.yview)
    rv_inner = tk.Frame(rv_canvas, bg=C["panel"])
    rv_win = rv_canvas.create_window((0, 0), window=rv_inner, anchor="nw")
    rv_inner.bind("<Configure>", lambda e: rv_canvas.configure(
        scrollregion=rv_canvas.bbox("all")))
    rv_canvas.bind("<Configure>", lambda e: rv_canvas.itemconfigure(rv_win, width=e.width))

    scoped_wheel(win, rv_canvas)     # ⚠️ never bind_all/unbind_all the wheel — see scoped_wheel()

    qcap = tk.Frame(leftp, bg=C["panel"])
    qcap.pack(fill="x", padx=10, pady=(9, 5))
    tk.Label(qcap, text="QUEUE", bg=C["panel"], fg=C["muted"], font=fmono).pack(side="left")
    qn = tk.Label(qcap, text="", bg=C["panel"], fg=C["faint"], font=fsm)
    qn.pack(side="left", padx=8)
    flt = tk.Button(qcap, text="unkept only", bg=C["panel2"], fg=C["ink"],
                    activebackground=C["rust"], activeforeground="#fff", relief="flat",
                    font=fsm, padx=8, pady=1, borderwidth=0)
    flt.pack(side="right")

    # TABLE | BOARD — the scene list's own view control. TABLE is the Treeview; BOARD is the
    # same scenes as cards (LONG-VIDEO-PLAN.md §3b). Additive: one plan, one filter.
    bview = tk.Button(qcap, text="BOARD ▸", bg=C["panel2"], fg=C["ink"],
                      activebackground=C["rust"], activeforeground="#fff", relief="flat",
                      font=fsm, padx=8, pady=1, borderwidth=0)
    bview.pack(side="right", padx=(0, 6))

    style = ttk.Style(win)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("LongQ.Treeview", background=C["panel"], fieldbackground=C["panel"],
                    foreground=C["muted"], bordercolor=C["line"], borderwidth=0,
                    rowheight=25, font=fsm)
    style.map("LongQ.Treeview", background=[("selected", "#251d17")],
              foreground=[("selected", C["ink"])])
    style.configure("LongQ.Treeview.Heading", background=C["bg"], foreground=C["faint"],
                    font=fmono, relief="flat")
    cols = ("scene", "len", "takes", "state")
    tree = ttk.Treeview(leftp, columns=cols, show="headings", style="LongQ.Treeview",
                        height=14, selectmode="browse")
    for c, w, t in (("scene", 250, "SCENE"), ("len", 56, "LEN"),
                    ("takes", 48, "TAKES"), ("state", 58, "STATE")):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w" if c == "scene" else "center")
    tree.tag_configure("kept", foreground=C["ink"])
    tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # ── the BOARD — the SAME scenes as cards. A VIEW of plan["scenes"], never a second model.
    # ⚠️ A composition gate, never a motion gate: an approved still says nothing about timing.
    board = tk.Frame(leftp, bg=C["panel"])
    board_cards = tk.Frame(board, bg=C["panel"])
    board_cards.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    board_refs = []      # ⚠️ tkinter GCs a PhotoImage with no live reference -> an empty box

    # ── 4 · scene add / delete / reorder ───────────────────────────────────────────
    ops = tk.Frame(leftp, bg=C["panel"])
    ops.pack(fill="x", padx=10, pady=(0, 10))

    def _renumber():
        for i, sc in enumerate(plan["scenes"]):
            sc["n"] = i + 1

    def add_scene():
        i = st["sel"] + 1 if plan["scenes"] else 0
        plan["scenes"].insert(i, long_scene_new(0))
        _renumber(); st["sel"] = i; refresh()

    def del_scene():
        if len(plan["scenes"]) <= 1:
            return
        del plan["scenes"][st["sel"]]
        _renumber()
        st["sel"] = max(0, st["sel"] - 1); refresh()

    def move_scene(d):
        i = st["sel"]; j = i + d
        if not (0 <= j < len(plan["scenes"])):
            return
        plan["scenes"][i], plan["scenes"][j] = plan["scenes"][j], plan["scenes"][i]
        _renumber(); st["sel"] = j; refresh()

    for txt, cmd in (("+ ADD", add_scene), ("− DEL", del_scene),
                     ("▲", lambda: move_scene(-1)), ("▼", lambda: move_scene(1))):
        tk.Button(ops, text=txt, bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
                  activeforeground="#fff", relief="flat", font=fsm, padx=8, pady=3,
                  borderwidth=0, command=cmd).pack(side="left", padx=(0, 5))

    # ⚠️ No command yet — cont_run is defined with the other KF helpers further down, and binding it
    # here would be a NameError at construction (same reason bview and b_kf are wired late).
    b_contrun = tk.Button(ops, text="CONT RUN…", bg=C["panel2"], fg=C["ink"],
                          activebackground=C["rust"], activeforeground="#fff", relief="flat",
                          font=fsm, padx=8, pady=3, borderwidth=0)
    b_contrun.pack(side="left", padx=(0, 5))

    # ── bench ──────────────────────────────────────────────────────────────────────
    bcap = tk.Label(rv_inner, text="", bg=C["panel"], fg=C["ink"], font=fh, anchor="w")
    bcap.pack(fill="x", padx=12, pady=(9, 4))
    top = tk.Frame(rv_inner, bg=C["panel"])
    top.pack(fill="x", padx=12)
    prev = tk.Label(top, text="no take yet", bg=C["panel2"], fg=C["faint"], font=fsm,
                    width=34, height=8, relief="flat")
    prev.pack(side="left")
    tkw = tk.Frame(top, bg=C["panel"])
    tkw.pack(side="left", fill="both", expand=True, padx=(12, 0))
    tk.Label(tkw, text="TAKES", bg=C["panel"], fg=C["faint"], font=fmono).pack(anchor="w")
    tk_list = tk.Frame(tkw, bg=C["panel"])
    tk_list.pack(fill="both", expand=True, pady=(5, 0))
    acts = tk.Frame(tkw, bg=C["panel"])
    acts.pack(fill="x", pady=(6, 0))
    b_roll = tk.Button(acts, text="REROLL", bg=C["ink"], fg="#14110f",
                       activebackground=C["ochre"], activeforeground="#14110f",
                       relief="flat", font=fsm, padx=12, pady=5, borderwidth=0)
    b_roll.pack(side="left")
    b_keep = tk.Button(acts, text="KEEP", bg=C["moss"], fg="#14110f",
                       activebackground="#8fa073", activeforeground="#14110f",
                       relief="flat", font=fsm, padx=12, pady=5, borderwidth=0)
    b_keep.pack(side="left", padx=7)

    # ── the per-scene PROMPT EDITOR (Ryan's ask) ───────────────────────────────────
    ped = tk.Frame(rv_inner, bg=C["panel"])
    ped.pack(fill="x", padx=12, pady=(10, 0))
    ph = tk.Frame(ped, bg=C["panel"])
    ph.pack(fill="x")
    tk.Label(ph, text="SCENE PROMPT", bg=C["panel"], fg=C["faint"], font=fmono).pack(side="left")
    pnote = tk.Label(ph, text="", bg=C["panel"], fg=C["faint"], font=fsm)
    pnote.pack(side="left", padx=8)
    pedit = tk.Text(ped, height=3, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                    font=f, insertbackground=C["ink"], padx=8, pady=6)
    pedit.pack(fill="x", pady=(5, 0))

    # ── 5 · per-scene LENGTH (the scene's own chunk count) ─────────────────────────
    lenrow = tk.Frame(ped, bg=C["panel"])
    lenrow.pack(fill="x", pady=(6, 0))
    tk.Label(lenrow, text="LENGTH", bg=C["panel"], fg=C["faint"], font=fmono).pack(side="left")
    len_var = tk.StringVar(value=S2V_LENGTHS[3])
    # ⚠️ ENGINE-AWARE: exactly ONE of these is mapped at a time (render_bench decides). H3's length
    # is quantised to n*17+5 frames, so H3 gets a SLIDER ALONG THE LADDER; WAN still chunks in
    # 77-frame units, which is a different question, so it keeps its dropdown.
    h3_len_var = tk.IntVar(value=0)
    h3_len_lbl = tk.Label(lenrow, text="", bg=C["panel"], fg=C["ink"], font=fmono)

    def apply_sel_len(*_a):
        sc = plan["scenes"][st["sel"]]
        for k, lab_ in S2V_LENGTHS.items():
            if lab_ == len_var.get():
                sc["chunks"] = k
        refresh()

    def apply_h3_len(idx=None):
        """Set this scene's H3 length from the ladder. `idx` is for a driver; the slider calls it
        with none. ⚠️ refresh(), NOT render_bench() — render_bench only repaints, so the length
        would never reach plan.json."""
        sc = plan["scenes"][st["sel"]]
        i = int(h3_len_var.get()) if idx is None else int(idx)
        i = max(0, min(len(H3_LADDER_S) - 1, i))
        # ⚠️ MEASURED: a PROGRAMMATIC set() on a variable-linked tk.Scale does NOT fire its
        # command (checked on Tk 9.0 / this interpreter: v.set() and scale.set() both fired
        # nothing). So syncing the slider from a scene cannot call back into this handler, and no
        # re-entrancy guard is needed here. Do not re-add one on the assumption that it does.
        if idx is not None:
            h3_len_var.set(i)
        sc["secs"] = H3_LADDER_S[i]
        refresh()

    def _on_len_slide(_v=None):
        apply_h3_len()

    len_menu = tk.OptionMenu(lenrow, len_var, *S2V_LENGTHS.values(),
                             command=lambda *_: apply_sel_len())
    len_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                       highlightthickness=0, activebackground=C["rust"],
                       activeforeground="#fff", borderwidth=0, width=17,
                       highlightbackground=C["panel"])
    len_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    # ⚠️ NOT packed here — render_bench packs exactly one of the two controls, and packing both
    # then hiding one is how a window ends up showing a control that does nothing.
    len_slider = tk.Scale(lenrow, from_=0, to=len(H3_LADDER_S) - 1, orient="horizontal",
                          variable=h3_len_var, showvalue=False, resolution=1,
                          bg=C["panel"], fg=C["ink"], troughcolor="#100e0c",
                          activebackground=C["rust"], relief="flat", highlightthickness=0,
                          borderwidth=0, sliderlength=26, width=12, sliderrelief="flat",
                          length=300, command=_on_len_slide)

    # ── per-scene KEYFRAME ROLE (D6.2) — four independent FLAGS, not a pick-one ─────
    # The still is ONE approved image; these say how it is spent. `first` + `last` are both legal
    # at once — that is the tightest control fl2va offers, both ends of the shot pinned, and it is
    # exactly what a 4-way dropdown would have made impossible.
    # ⚠️ Toggle BUTTONS, not tk.Checkbutton: there is no Checkbutton anywhere else in this app,
    # and macOS draws its own unthemed white Aqua box on a near-black panel. `flt` already is this
    # app's boolean idiom — on = rust fill.
    KF_LABELS = (("kf_first", "first frame"), ("kf_last", "last frame"),
                 ("kf_ref", "reference"), ("kf_cont", "continue from prev"))

    kfrow = tk.Frame(ped, bg=C["panel"])
    kfrow.pack(fill="x", pady=(6, 0))
    tk.Label(kfrow, text="KEYFRAME", bg=C["panel"], fg=C["faint"],
             font=fmono).pack(side="left")
    kf_btns = {}
    for _key, _lab in KF_LABELS:
        _b = tk.Button(kfrow, text=_lab, bg=C["panel2"], fg=C["ink"], relief="flat",
                       font=fsm, padx=8, pady=1, borderwidth=0, activebackground=C["rust"],
                       activeforeground="#fff",
                       command=lambda k=_key: toggle_kf(k))
        _b.pack(side="left", padx=(7, 0))
        kf_btns[_key] = _b
    kf_note = tk.Label(ped, text="", bg=C["panel"], fg=C["faint"], font=fsm,
                       justify="left", anchor="w", wraplength=700)
    kf_note.pack(fill="x", pady=(3, 0))

    # ── per-scene BEAT: motion intent · colour state · transition out ───────────────────────
    # ⚠️ Placed in `ped` inside `rv_inner`, which SCROLLS. The treatment rows are fixed-height and
    # Tk silently STARVES a row that does not fit (the one-row treatment bug) — anything new goes
    # in the scrolling column, never the fixed rows.
    # ⚠️ These are AUTHORING fields (see MOTION_INTENTS). Nothing here reaches the renderer.
    beatrow = tk.Frame(ped, bg=C["panel"])
    beatrow.pack(fill="x", pady=(6, 0))
    tk.Label(beatrow, text="BEAT", bg=C["panel"], fg=C["faint"],
             font=fmono).pack(side="left")
    motion_var = tk.StringVar(value=BEAT_UNSET)
    motion_menu = tk.OptionMenu(beatrow, motion_var, BEAT_UNSET, *MOTION_INTENTS,
                                command=lambda *_: beat_push())
    motion_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                          highlightthickness=0, activebackground=C["rust"],
                          activeforeground="#fff", borderwidth=0, width=13,
                          highlightbackground=C["panel"])
    # ⚠️ OptionMenu's POPUP is a separate tk.Menu and ignores the button's colours — macOS draws
    # it system-WHITE out of a near-black app unless it is configured explicitly.
    motion_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    motion_menu.pack(side="left", padx=(7, 0))
    tk.Label(beatrow, text="colour", bg=C["panel"], fg=C["faint"],
             font=fmono).pack(side="left", padx=(12, 0))
    color_e = tk.Entry(beatrow, width=15, bg="#100e0c", fg=C["ink"], relief="flat",
                       insertbackground=C["ink"], font=fsm, highlightthickness=0)
    color_e.pack(side="left", padx=(5, 0))
    tk.Label(beatrow, text="cut to", bg=C["panel"], fg=C["faint"],
             font=fmono).pack(side="left", padx=(12, 0))
    trans_e = tk.Entry(beatrow, width=28, bg="#100e0c", fg=C["ink"], relief="flat",
                       insertbackground=C["ink"], font=fsm, highlightthickness=0)
    trans_e.pack(side="left", padx=(5, 0))
    tk.Label(ped, text="motion = where this beat sits in the energy curve · colour + cut are "
                       "for the edit, not the render",
             bg=C["panel"], fg=C["faint"], font=fsm, justify="left",
             anchor="w", wraplength=700).pack(fill="x", pady=(3, 0))

    def beat_push(*_):
        """Write the row's CURRENT values into the selected scene, then save + repaint."""
        s = plan["scenes"][st["sel"]]
        _m = motion_var.get()
        s["motion"] = "" if _m == BEAT_UNSET else _m
        s["color_state"] = color_e.get().strip()
        s["transition_out"] = trans_e.get().strip()
        # ⚠️ refresh(), NOT render_bench() — render_bench only repaints, so the value would never
        # reach plan.json. The same trap `toggle_kf` documents.
        refresh()

    def beat_apply(motion=None, color_state=None, transition_out=None):
        """Set the row FROM values, then push. Exported so a driver drives the REAL handlers
        instead of poking the plan behind their backs."""
        if motion is not None:
            motion_var.set(motion or BEAT_UNSET)
        if color_state is not None:
            color_e.delete(0, "end"); color_e.insert(0, color_state)
        if transition_out is not None:
            trans_e.delete(0, "end"); trans_e.insert(0, transition_out)
        beat_push()

    color_e.bind("<KeyRelease>", beat_push)
    trans_e.bind("<KeyRelease>", beat_push)
    color_e.bind("<FocusOut>", beat_push)
    trans_e.bind("<FocusOut>", beat_push)

    def kf_summary(s):
        """One short string for a board card and the dump. ⚠️ `cont` short-circuits: when it is on
        the still is NOT used, so listing roles beside it would imply it is."""
        if s.get("kf_cont"):
            return "← prev"
        on = [lab_.split()[0] for k, lab_ in KF_LABELS if s.get(k)]
        return "+".join(on) if on else "NONE"

    def toggle_kf(key):
        s = plan["scenes"][st["sel"]]
        if key == "kf_cont" and st["sel"] == 0:
            # refuse, do not silently downgrade to a text-only shot (D6.2)
            # ⚠️ Do NOT re-render here. render_bench() rewrites kf_note, so the refusal would be
            # wiped by the very next line and never readable.
            kf_note.configure(text="scene 1 has nothing to continue from — refused",
                              fg=C["ochre"])
            return
        s[key] = not bool(s.get(key))
        if key == "kf_cont" and s[key]:
            s["kf_first"] = False      # cont and first both claim the FIRST frame; cont wins
        # ⚠️ refresh(), NOT render_bench(): render_bench only repaints the bench, so the flag
        # would never reach plan.json and DRAFT KEYFRAMES' denominator would go stale. Caught by
        # TESTLONG asserting the counter fell to 0/11 the moment a scene went `cont`.
        refresh()

    # ── `continue from prev` as a RUN, not just a per-scene toggle ──────────────────
    # ⚠️ It is really a PER-PIECE decision wearing per-scene clothes: scenes 3–8 chained while 1–2
    # and 9–12 are not gives a piece that is neither a montage nor a one-take. A range control is
    # the shape the decision actually has; the per-scene toggle stays for one-off fixes.
    def apply_cont_run(a, b, on=True):
        """⚠️ Scene 1 is ALWAYS excluded even if the range starts at 1 — `cont` there is
        meaningless, the per-scene toggle already refuses it, and a run must not smuggle it in."""
        a = max(2, int(a))
        b = min(len(plan["scenes"]), int(b))
        for k in range(a, b + 1):
            sc = plan["scenes"][k - 1]
            sc["kf_cont"] = bool(on)
            sc["kf_first"] = not bool(on)    # cont and first both claim the START
        refresh()

    def cont_run():
        n = len(plan["scenes"])
        top = tk.Toplevel(win)
        top.title("Continue from previous scene")
        top.configure(bg=C["bg"])
        top.transient(win)
        tk.Label(top, text="CONTINUE FROM PREVIOUS SCENE", bg=C["bg"], fg=C["ochre"],
                 font=fmono).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(top, text="Every scene in the run starts from the PREVIOUS scene's last kept "
                           "frame — no cut between them. Scene 1 is always excluded: it has "
                           "nothing to continue from.",
                 bg=C["bg"], fg=C["muted"], font=fsm, justify="left",
                 wraplength=430).pack(anchor="w", padx=16)
        prow = tk.Frame(top, bg=C["bg"])
        prow.pack(anchor="w", padx=16, pady=(10, 0))
        tk.Label(prow, text="FROM", bg=C["bg"], fg=C["faint"], font=fmono).pack(side="left")
        e_a = tk.Entry(prow, width=4, bg="#100e0c", fg=C["ink"], relief="flat", font=fmono,
                       insertbackground=C["ink"])
        e_a.insert(0, "2")
        e_a.pack(side="left", padx=(6, 14))
        tk.Label(prow, text="TO", bg=C["bg"], fg=C["faint"], font=fmono).pack(side="left")
        e_b = tk.Entry(prow, width=4, bg="#100e0c", fg=C["ink"], relief="flat", font=fmono,
                       insertbackground=C["ink"])
        e_b.insert(0, str(n))
        e_b.pack(side="left", padx=(6, 0))
        msg = tk.Label(top, text="", bg=C["bg"], fg=C["ochre"], font=fsm, justify="left")
        msg.pack(anchor="w", padx=16, pady=(6, 0))

        def _range():
            try:
                a = int(e_a.get()); b = int(e_b.get())
            except Exception:
                return None
            if a < 2 or a > b or b > n:
                return None
            return a, b

        def _go(on):
            r = _range()
            if not r:
                msg.configure(text="give a range inside 2–%d with FROM ≤ TO" % n)
                return
            top.destroy()
            apply_cont_run(r[0], r[1], on)

        acts = tk.Frame(top, bg=C["bg"])
        acts.pack(anchor="w", padx=16, pady=(10, 14))
        tk.Button(acts, text="CONTINUE THIS RUN", bg=C["rust_deep"], fg="#fff", relief="flat",
                  font=fsm, padx=12, pady=6, borderwidth=0,
                  command=lambda: _go(True)).pack(side="left")
        tk.Button(acts, text="CLEAR THIS RUN", bg=C["panel2"], fg=C["ink"], relief="flat",
                  font=fsm, padx=12, pady=6, borderwidth=0,
                  command=lambda: _go(False)).pack(side="left", padx=(8, 0))
        tk.Button(acts, text="CANCEL", bg=C["panel2"], fg=C["ink"], relief="flat",
                  font=fsm, padx=12, pady=6, borderwidth=0,
                  command=top.destroy).pack(side="left", padx=(8, 0))
    b_contrun.configure(command=cont_run)

    # ── per-slot STILL prompts ─────────────────────────────────────────────────────
    # ⚠️ One box per slot the ROLES actually need — never a switch. An empty box means "follow the
    # scene prompt", and the hint spells out what that currently is, so empty is never a mystery.
    # Without this, `first` + `last` renders two near-identical stills from one prompt: the same
    # moment twice, with nothing for the model to travel between.
    spwrap = tk.Frame(ped, bg=C["panel"])
    spwrap.pack(fill="x", pady=(4, 0))

    def _sp_field(slot):
        return "still_last_prompt" if slot == "still_last" else "still_prompt"

    def _sp_hint(s, slot):
        if s.get(_sp_field(slot)):
            return "using this text exactly — not the scene prompt"
        return ("empty → follows the scene prompt: %r…"
                % ((_subject() + " " if _subject() else "") + (s["prompt"] or "") or
                   "(none yet)")[:58])

    def render_still_prompts():
        for w in spwrap.winfo_children():
            w.destroy()
        s = plan["scenes"][st["sel"]]
        for slot in _kf_slots(s):
            fld = _sp_field(slot)
            tk.Label(spwrap, text=("LANDING STILL PROMPT" if slot == "still_last"
                                   else "OPENING STILL PROMPT"),
                     bg=C["panel"], fg=C["faint"], font=fmono).pack(anchor="w", pady=(5, 0))
            box = tk.Text(spwrap, height=1, wrap="word", bg="#100e0c", fg=C["ink"],
                          relief="flat", font=fsm, insertbackground=C["ink"], padx=6, pady=4)
            box.pack(fill="x", pady=(2, 0))
            box.insert("1.0", s.get(fld) or "")
            hint = tk.Label(spwrap, text="", bg=C["panel"], fg=C["faint"], font=fsm,
                            justify="left", anchor="w", wraplength=680)
            hint.pack(fill="x")
            hint.configure(text=_sp_hint(s, slot))

            def _upd(box=box, hint=hint, fld=fld, slot=slot, _s=s):
                vt = box.get("1.0", "end").strip()
                _s[fld] = vt or None
                hint.configure(text=_sp_hint(_s, slot))
                # ⚠️ save WITHOUT refresh(): refresh() rebuilds the bench and would destroy the
                # very Text widget being typed into, stealing the cursor mid-word.
                if st.get("loaded"):
                    long_plan_save(st["name"], plan)
            box.bind("<KeyRelease>", lambda e, u=_upd: u())
            # test seam: event_generate() does not reliably dispatch to an unfocused widget, so
            # TESTLONG asserts the KeyRelease BINDING exists and then calls the handler directly.
            box._kf_update = _upd

    # ── audio strip: this scene's window on ONE continuous track ───────────────────
    au = tk.Frame(rv_inner, bg=C["panel"])
    au.pack(fill="x", padx=12, pady=(10, 0))
    tk.Label(au, text="AUDIO · ONE CONTINUOUS TRACK, THIS SCENE'S WINDOW",
             bg=C["panel"], fg=C["faint"], font=fmono).pack(anchor="w")
    wav = tk.Canvas(au, height=34, bg=C["bg"], highlightthickness=1,
                    highlightbackground=C["line"])
    wav.pack(fill="x", pady=(4, 0))
    wlab = tk.Label(au, text="", bg=C["panel"], fg=C["faint"], font=fsm, anchor="w")
    wlab.pack(fill="x", pady=(3, 0))

    refuse = tk.Label(refusewrap, text="", bg=C["panel"], fg=C["muted"], font=fsm,
                      justify="left", anchor="w", wraplength=700)

    # ── footer ─────────────────────────────────────────────────────────────────────
    foot = tk.Frame(win, bg=C["bg"])
    foot.pack(fill="x", padx=14, pady=(0, 12))
    # ⚠️ To the LEFT of DRAFT ALL on purpose: keyframes come first in TIME, so the footer reads
    # left-to-right in the order you press it (§3b). No command yet — `draft_keyframes` is defined
    # further down, and binding it here would be a NameError at construction (same reason bview
    # is wired late).
    b_kf = tk.Button(foot, text="DRAFT KEYFRAMES", bg=C["ochre"], fg="#14110f",
                     activebackground="#d9b45e", activeforeground="#14110f",
                     relief="flat", font=fsm, padx=12, pady=7, borderwidth=0)
    b_kf.pack(side="left", padx=(0, 7))
    b_draft = tk.Button(foot, text="DRAFT ALL 12", bg=C["rust_deep"], fg="#fff",
                        activebackground=C["rust_hi"], activeforeground="#fff",
                        relief="flat", font=fsm, padx=12, pady=7, borderwidth=0)
    b_draft.pack(side="left")
    b_rollun = tk.Button(foot, text="REROLL UNKEPT", bg=C["panel2"], fg=C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=10, pady=7, borderwidth=0)
    b_rollun.pack(side="left", padx=7)
    b_stitch = tk.Button(foot, text="STITCH KEEPERS →", bg=C["panel2"], fg=C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=10, pady=7, borderwidth=0)
    b_stitch.pack(side="left")
    cost = tk.Label(foot, text="", bg=C["bg"], fg=C["muted"], font=fmono,
                    justify="right", anchor="e")
    cost.pack(side="right")

    # ── rendering ──────────────────────────────────────────────────────────────────
    def kept_n():
        return len([s for s in plan["scenes"] if s.get("keep") is not None])

    def fmt(t):
        return "%d:%02d" % (int(t) // 60, int(round(t)) % 60)

    def total_s():
        """⚠️ ENGINE-AWARE, exactly like `_worker`: the same scene is a different LENGTH on H3
        (24 fps) and WAN (16 fps). An engine-blind total draws this scene's window on the AUDIO
        strip in the wrong place, so the strip disagrees with the audio actually sliced for the run.
        """
        _e = long_engine(plan)
        return sum(long_scene_seconds_for(s.get("chunks", 3), _e, s.get("secs", 0.0))
                   for s in plan["scenes"])

    def _tsum(text):
        """⚠️ tsum is a Label, so its REQUESTED width IS its text width. Unbounded it claimed 960 px
        of the treatment row and starved every control to its right out of existence entirely.
        ⚠️ Elided from the MIDDLE: the tail carries which audio and which ref are loaded, and those
        are the two things this line exists to report — cutting the tail hides exactly them.
        `details ▾` still carries the full, untruncated version."""
        n = 96
        if len(text) <= n:
            return text
        half = (n - 1) // 2
        return text[:half] + " … " + text[-half:]

    def render_treat():
        t = plan["treatment"]
        _sb = _subject()
        if long_engine(plan) == "h3":
            _st = int(t.get("h3_steps", H3_LONGFORM_STEPS))
            _path = "%dst%s" % (_st, " turbo" if _st <= 4 else "")
            tsum.configure(text=_tsum(
                "%d×%d · 24fps · H3 r2v %s · %s · AUDIO GUIDE %s · ref %s"
                % (long_canvas(plan)[0], long_canvas(plan)[1], _path,
                   (long_lora(plan) or "NONE").replace(".safetensors", ""),
                   os.path.basename(st["audio"] or "none"),
                   os.path.basename(st["ref"] or "none"))))
        else:
            tsum.configure(text=_tsum(
                "%d×%d · 16fps · WAN 2.2 S2V %dst · cfg %.1f · shift %.1f · audio %s · ref %s"
                % (S2V_SIZE[0], S2V_SIZE[1], t["steps"], t["cfg"], t["shift"],
                   os.path.basename(st["audio"] or "none"),
                   os.path.basename(st["ref"] or "none"))))
        tdet.configure(text=(
            "What LOCKED means: every scene is generated with these five, so the scenes "
            "match. Changing any of them would start a NEW treatment.\n"
            "1 · canvas + fps: %d×%d @ %d fps      2 · engine: WAN 2.2 s2v 14B, %d steps, "
            "cfg %.1f, shift %.1f — one path, no speed slider\n"
            "3 · audio policy: ONE bed track across the whole piece, unsliced; per-scene "
            "overrides go in that scene's own prompt box      4 · reference: %s (this path "
            "carries no LoRA, so the still holds the identity)\n"
            "5 · subject block: %s"
            % (t["w"], t["h"], t["fps"], t["steps"], t["cfg"], t["shift"],
               os.path.basename(st["ref"] or "none chosen"),
               ("pinned — %d chars, starting %r" % (len(_sb), _sb[:56])) if _sb else
               "NOT SET — nothing enforces the look. Press STYLE… to paste it in.")))

    def render_queue():
        tree.delete(*tree.get_children())
        shown = 0
        for i, s in enumerate(plan["scenes"]):
            k = s.get("keep") is not None
            if st["filter"] and k:
                continue
            shown += 1
            tree.insert("", "end", iid=str(i), tags=("kept",) if k else (),
                        values=("%d  %s" % (s["n"], (s["prompt"] or "(no prompt yet)")[:60]),
                                "%.2f s" % (long_scene_seconds(s.get("chunks", 3))),
                                len(s.get("takes") or []),
                                "KEPT" if k else "draft"))
        if tree.get_children():
            iid = str(st["sel"])
            if iid in tree.get_children():
                tree.selection_set(iid)
                tree.see(iid)
        ns = len([s for s in plan["scenes"] if s.get("still")])
        qn.configure(text="%d shown · %d of %d kept · %d/%d stills"
                          % (shown, kept_n(), len(plan["scenes"]), ns, len(plan["scenes"])))

    def render_takes():
        for w in tk_list.winfo_children():
            w.destroy()
        s = plan["scenes"][st["sel"]]
        takes = s.get("takes") or []
        if not takes:
            lam(tk_list, "no takes yet — REROLL to draft this scene").pack(anchor="w")
        for i, t in enumerate(takes):
            r = tk.Frame(tk_list, bg=C["bg"], highlightthickness=1,
                         highlightbackground=(C["moss"] if i == s.get("keep")
                                              else C["line"]))
            r.pack(fill="x", pady=2)
            tk.Label(r, text="take %d" % (i + 1), bg=C["bg"], fg=C["muted"],
                     font=fmono).pack(side="left", padx=7)
            tk.Label(r, text="seed %s" % t.get("seed"), bg=C["bg"], fg=C["ink"],
                     font=fsm).pack(side="left")
            tk.Label(r, text=t.get("status", "?"), bg=C["bg"],
                     fg=(C["moss"] if t.get("status") == "done" else C["ochre"]),
                     font=fsm).pack(side="left", padx=7)
            if i == s.get("keep"):
                tk.Label(r, text="KEPT", bg=C["moss"], fg="#14110f", font=fmono).pack(side="right")
            elif (t.get("synced") or t.get("file")) and os.path.exists(
                    t.get("synced") or t["file"]):
                _op = t.get("synced") or t["file"]
                tk.Button(r, text="OPEN", bg=C["panel2"], fg=C["ink"], relief="flat",
                          font=fsm, padx=6, pady=0, borderwidth=0,
                          command=lambda q=_op: subprocess.Popen(["open", q])
                          ).pack(side="right", padx=4)
            r.bind("<Button-1>", lambda e, i=i: load_take(i))

    def load_take(i):
        s = plan["scenes"][st["sel"]]
        takes = s.get("takes") or []
        if i >= len(takes):
            return
        st["takes_open"] = i
        f_ = takes[i].get("synced") or takes[i].get("file")
        img = long_thumb(f_) if (f_ and os.path.exists(f_)) else None
        if img:
            prev.configure(image=img, text="", width=img.width(), height=img.height())
            prev.image = img                                   # keep the ref alive
        else:
            prev.configure(image="", text=("take %d\n%s" % (i + 1, os.path.basename(f_ or "—"))),
                           width=34, height=8)
        render_takes()

    def render_bench(*_):
        s = plan["scenes"][st["sel"]]
        bcap.configure(text="SCENE %d — take %s of %d"
                       % (s["n"], (st["takes_open"] or 0) + 1, len(s.get("takes") or [])))
        # ⚠️ LENGTH is ENGINE-AWARE and exactly ONE control is mapped. Tracked in `st`, NOT by
        # winfo_ismapped(): in an unmapped driver window ismapped is meaningless (the documented
        # geometric-assertion trap) and it would re-pack on every repaint.
        _want = "h3" if long_engine(plan) == "h3" else "wan"
        if st.get("len_control") != _want:
            st["len_control"] = _want
            if _want == "h3":
                len_menu.pack_forget()
                len_slider.pack(side="left", padx=7)
                h3_len_lbl.pack(side="left", padx=(7, 0))
            else:
                len_slider.pack_forget()
                h3_len_lbl.pack_forget()
                len_menu.pack(side="left", padx=7)
        if _want == "h3":
            _li = h3_ladder_index(s.get("secs"))
            if int(h3_len_var.get()) != _li:
                # ⚠️ This only MOVES the slider; it must never WRITE to the scene. A scene with no
                # length of its own (0.0) has to stay unset or it silently pins to 3.04 s.
                h3_len_var.set(_li)
            h3_len_lbl.configure(text="%.2f s · %d frames"
                                      % (H3_LADDER_S[_li], H3_LADDER_FRAMES[_li]))
        elif len_var.get() != S2V_LENGTHS.get(int(s.get("chunks", 3))):
            len_var.set(S2V_LENGTHS.get(int(s.get("chunks", 3)), S2V_LENGTHS[3]))
        if pedit.get("1.0", "end").strip() != (s["prompt"] or ""):
            pedit.delete("1.0", "end")
            pedit.insert("1.0", s["prompt"] or "")
        pnote.configure(text="saved with the scene · %d of 12" % s["n"])
        # ⚠️ The BEAT row follows the SELECTED scene. An entry is only rewritten when its value
        # actually differs, or every repaint would clobber what is being typed into it.
        for _w, _k in ((color_e, "color_state"), (trans_e, "transition_out")):
            _v = s.get(_k, "") or ""
            if _w.get().strip() != _v:
                _w.delete(0, "end")
                _w.insert(0, _v)
        _mv = (s.get("motion") or "") or BEAT_UNSET
        if motion_var.get() != _mv:
            motion_var.set(_mv)
        # ── the KF flags: on = rust fill, the same read as the unkept-only filter ──────
        for _k, _b in kf_btns.items():
            _on = bool(s.get(_k, False))
            _b.configure(bg=(C["rust_deep"] if _on else C["panel2"]),
                         fg=("#fff" if _on else C["ink"]))
        # ⚠️ scene 1 gets a DISABLED button, not one that silently refuses on click (D6.2)
        kf_btns["kf_cont"].configure(state=("disabled" if st["sel"] == 0 else "normal"))
        if s.get("kf_cont"):
            kf_note.configure(
                text="inherits its first frame from scene %d's last keeper — the still on this "
                     "scene is UNUSED. What wants approving is scene %d, not this one."
                     % (s["n"] - 1, s["n"] - 1), fg=C["muted"])
        elif s.get("kf_first") and s.get("kf_last"):
            kf_note.configure(text="both ends pinned (fl2va) — the model fills only the middle",
                              fg=C["moss"])
        elif s.get("still") and not (s.get("kf_first") or s.get("kf_last") or s.get("kf_ref")):
            kf_note.configure(text="a still exists but no role is set — it would be ignored",
                              fg=C["ochre"])
        else:
            kf_note.configure(text="", fg=C["faint"])
        render_still_prompts()
        render_takes()
        big = prev.image if hasattr(prev, "image") else None
        if not (s.get("takes") or []):
            prev.configure(image="", text="no take yet", width=34, height=8)
        # the one continuous track, with THIS scene's window drawn on it
        wav.delete("all")
        total = max(1.0, total_s())
        w = max(200, wav.winfo_width())
        # ⚠️ The REAL waveform. The sine/cos pattern that used to be here drew the SAME full-height
        # shape for every episode and for silence alike — a picture of nothing.
        # ⚠️ Kept identical between the mapped and unmapped cases so TESTLONG can assert the drawn
        # heights rather than trusting the code.
        _pk = audio_peaks(st.get("audio"), int(w))
        if _pk:
            for i in range(min(int(w), len(_pk))):
                wav.create_line(i, 34, i, 34 - (2 + _pk[i] * 30), fill=C["moss"])
        else:
            wav.create_line(0, 32, int(w), 32, fill=C["faint"])
        _eng = long_engine(plan)
        a, b = 0.0, 0.0
        for k in range(st["sel"]):
            a += long_scene_seconds_for(plan["scenes"][k].get("chunks", 3), _eng,
                                        plan["scenes"][k].get("secs", 0.0))
        b = a + long_scene_seconds_for(s.get("chunks", 3), _eng, s.get("secs", 0.0))
        x0 = int(a / total * w)
        x1 = int(b / total * w)
        wav.create_rectangle(x0, 0, x1, 34, fill="#3a2418", outline=C["rust"])
        _has_peaks = bool(audio_peaks(st.get("audio"), 64))
        wlab.configure(text=("track 0:00–%s · this scene %s–%s · continuous, not sliced per scene"
                             % (fmt(total), fmt(a), fmt(b)))
                            + ("" if _has_peaks else
                               "   \u26a0 no audio loaded \u2014 the strip is empty"))

    def render_steps():
        ko, n = kept_n(), len(plan["scenes"])
        ns = len([s for s in plan["scenes"] if s.get("still")])
        step_btns["Scenes"][1].configure(text="%d of %d kept · %d of %d have stills"
                                              % (ko, n, ns, n))
        step_btns["Stitch"][1].configure(text="ready" if ko == n else "needs %d keepers" % n)
        # ⚠️ a COUNTER, never a disabled button — the board gates nothing (LONG-VIDEO-PLAN §3b)
        b_draft.configure(text="DRAFT ALL · %d of %d have keyframes" % (ns, n))
        # ⚠️ Counted in SLOTS, not scenes: `last` means a scene needs TWO stills, and a `cont` scene
        # needs none for its start. Counting scenes would show a permanent wrong number.
        _need = sum(len(_kf_slots(s)) for s in plan["scenes"])
        _have = _need - len(_kf_todo())
        b_kf.configure(text="DRAFT KEYFRAMES · %d/%d" % (_have, _need),
                       state=("disabled" if st.get("kf_drafting") else "normal"))
        for v, (b, _h) in step_btns.items():
            b.configure(bg="#251d17" if v == "Scenes" else C["panel"])
        for w in step_btns["Scenes"][0].winfo_children():
            w.configure(bg="#251d17" if w.cget("bg") != C["line"] else C["line"])

    def _cost_facts():
        """Everything the cost line AND the pre-flight dialog need: what will run, what is already
        kept, what will be REFUSED, what it costs, and on which engine.

        ⚠️ ONE computation, TWO consumers. The dialog must never quote a different number than the
        footer — the whole point of the pre-flight is that what you approve is what you were shown.
        """
        run = [(_i, _s) for _i, _s in enumerate(plan["scenes"]) if _s.get("keep") is None]
        kept = len(plan["scenes"]) - len(run)
        total = 0.0
        priced = 0
        blocked_why = []
        _warm = None                      # one 3090: the FIRST scene pays the engine load
        for _i, _s in run:
            _secs, _m, _why = long_scene_cost(_i, _s, warm_engine=_warm)
            if _secs is None:
                blocked_why.append((_s.get("n"), _why))
                continue
            total += _secs
            priced += 1
            _warm = engine_of(_m)
        # the engine and the step path are what actually set the price, so name them
        if long_engine(plan) == "h3":
            _st_ = int(plan["treatment"].get("h3_steps", H3_LONGFORM_STEPS))
            _eng = "H3 %d steps%s" % (_st_, " (turbo)" if _st_ <= 4 else "")
        else:
            _eng = "WAN 2.2 S2V"
        return {"run": run, "kept": kept, "total": total, "priced": priced,
                "blocked": len(blocked_why), "blocked_why": blocked_why, "eng": _eng}

    def render_cost():
        """⚠️ This is the number Ryan plans his time around, so it is priced through the SAME params
        the run submits (`long_scene_cost` → `_scene_params`) and it SKIPS what `_worker` skips.

        It used to add `S2V_FIRST_CHUNK_S + (ch-1)*S2V_EXTRA_CHUNK_S` — WAN's lip-sync model — for
        EVERY piece whatever engine was selected, and count scenes that were already kept.
        MEASURED: it quoted 64.6 min for 12 H3 scenes the app itself priced at 120.7 min at 12
        steps, and the turbo draft path read exactly the same as the slow one.
        """
        _f = _cost_facts()
        run, kept = _f["run"], _f["kept"]
        total, priced, blocked = _f["total"], _f["priced"], _f["blocked"]
        _eng = _f["eng"]
        _bits = ["%d to run" % len(run)]
        if kept:
            _bits.append("%d kept" % kept)
        if blocked:
            _bits.append("⚠ %d blocked" % blocked)
        if st.get("drafting"):
            _bits.append("drafting…")
        _head = " · ".join(_bits)
        # ⚠️ KEEP THIS SHORT. A tk.Label's requested width IS its text width, and the footer is a
        # narrow strip — the long form of this line pushed the whole WINDOW past the screen width
        # (1495 requested vs 1470 realized) and TESTLONG's fit assertion caught it. Compact it.
        if not run:
            cost.configure(text="nothing to run — all scenes kept\n%s" % _eng)
        elif not priced:
            cost.configure(text="%s\n⚠ all blocked — read TAKES" % _head)
        else:
            cost.configure(text="%s · ≈ %s forge time\n≈ %.0f s each · %s · incl. engine load"
                           % (_head, fmt(total / 60), total / priced, _eng))

    # ── the BOARD, as a view (LONG-VIDEO-PLAN.md §3b) ───────────────────────────────
    BOARD_COLS = 4
    BOARD_THUMB = 150

    def _bind_click(w, fn):
        """⚠️ Tk does NOT propagate a click from a child to its parent, so a card built from six
        widgets answers only on its dead space unless every child is bound. Buttons are skipped:
        a widget-level <Button-1> binding would shadow the class binding that draws their press."""
        if w.winfo_class() in ("Button", "Menubutton"):
            return
        w.bind("<Button-1>", fn)
        for ch in w.winfo_children():
            _bind_click(ch, fn)

    def _pick_scene(i):
        st["sel"] = i
        st["takes_open"] = 0
        try:
            tree.selection_set(str(i))
        except Exception:
            pass                      # filtered out of the tree; the selection is still valid
        render_bench(); render_cost(); render_board()

    def render_board():
        """Cards for the scenes. Rebuilt whole — 12 frames is cheaper than diffing them, and a
        half-updated board is worse than a redrawn one."""
        if not st["board"]:
            st["board_modes"] = []
            st["board_motions"] = []      # or a driver reads the PREVIOUS view's log
            return
        for w in board_cards.winfo_children():
            w.destroy()
        board_refs.clear()
        # ⚠️ What each card is PROMISING (i2v / flf / cont / r2v / t2v / kept / BLOCKED), recorded so
        # a driver can assert the board agrees with `_scene_video_plan` rather than trusting a label.
        _mode_log = []
        _beat_log = []
        for c in range(BOARD_COLS):
            board_cards.columnconfigure(c, weight=1, uniform="card")
        shown = 0
        for i, s in enumerate(plan["scenes"]):
            k = s.get("keep") is not None
            if st["filter"] and k:
                continue
            r, c = divmod(shown, BOARD_COLS)
            shown += 1
            sel = (i == st["sel"])
            bg = "#251d17" if sel else C["bg"]          # the Treeview's own selected colour
            card = tk.Frame(board_cards, bg=bg, highlightthickness=1,
                            highlightbackground=(C["rust"] if sel else
                                                 (C["moss"] if k else C["line"])))
            card.grid(row=r, column=c, sticky="nsew", padx=4, pady=4)
            _open_still, _land_still = s.get("still"), s.get("still_last")
            still = _open_still or _land_still    # the card shows whichever end exists
            img = (long_thumb(still, box=BOARD_THUMB)
                   if (still and os.path.exists(still)) else None)
            if img:
                lb = tk.Label(card, image=img, bg=bg, borderwidth=0)
                lb.pack(fill="x")
                lb.image = img                        # keep the ref alive on the widget...
                board_refs.append(img)                # ...and in a list. Both, on purpose.
                # ⚠️ Label which END this is. With `first` + `last` a scene has two pictures, and
                # a landing frame silently reading as the opening one is exactly the confusion
                # the two slots exist to prevent.
                _brow = tk.Frame(card, bg=bg)
                _brow.pack(fill="x", padx=3, pady=(2, 0))
                tk.Button(_brow, text=("OPEN" if _open_still else "LAND"),
                          bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm, padx=6, pady=0,
                          borderwidth=0,
                          command=lambda q=still, j=i: (_pick_scene(j),
                                                        subprocess.Popen(["open", q]))
                          ).pack(side="right")
                if _land_still and _open_still:
                    tk.Button(_brow, text="LAND", bg=C["panel2"], fg=C["ink"], relief="flat",
                              font=fsm, padx=6, pady=0, borderwidth=0,
                              command=lambda q=_land_still, j=i: (
                                  _pick_scene(j), subprocess.Popen(["open", q]))
                              ).pack(side="right", padx=(0, 4))
            else:
                ph = tk.Frame(card, bg=C["panel2"], width=BOARD_THUMB, height=108)
                ph.pack(fill="x"); ph.pack_propagate(False)
                # ⚠️ tk.PhotoImage reads PNG/GIF only. A real .jpg still lands here too — say
                # its name rather than call an existing still "no still".
                _err = s.get("still_error")
                tk.Label(ph, text=((_err or "")[:44] if _err else
                                   (os.path.basename(still)[:22] if still else "no still")),
                         bg=C["panel2"], fg=(C["rust"] if _err else C["faint"]),
                         font=fsm, wraplength=BOARD_THUMB - 12,
                         justify="center").pack(expand=True)
            body = tk.Frame(card, bg=bg)
            body.pack(fill="x", padx=6, pady=(4, 5))
            hl = tk.Frame(body, bg=bg)
            hl.pack(fill="x")
            tk.Label(hl, text="%d" % s["n"], bg=bg, fg=C["ochre"],
                     font=fmono).pack(side="left")
            tk.Label(hl, text="%.2f s" % long_scene_seconds_for(s.get("chunks", 3),
                                                               long_engine(plan),
                                                               s.get("secs", 0.0)),
                     bg=bg, fg=C["faint"], font=fsm).pack(side="right")
            tk.Label(body, text=(s.get("prompt") or "(no prompt yet)")[:56], bg=bg,
                     fg=C["muted"], font=fsm, justify="left", anchor="w",
                     wraplength=BOARD_THUMB, height=2).pack(fill="x", pady=(2, 0))
            tk.Label(body, text="kf " + kf_summary(s), bg=bg, fg=C["faint"],
                     font=fmono).pack(anchor="w", pady=(1, 0))
            # ⚠️ WHAT THIS SCENE WILL ACTUALLY DO — the question the board exists to answer before
            # any GPU is spent. The card used to say only "kf first+last", and nothing told you that
            # meant an `flf` job with TWO refs, or that the role was unsatisfiable and the scene
            # would be REFUSED. `_scene_video_plan` is the same call `_worker` makes, so the chip
            # cannot promise something the run will not do.
            if k:
                _mode, _nrefs, _blocked = "kept", 0, False
            elif long_engine(plan) == "h3":
                _vm2, _rv2, _why2 = _scene_video_plan(i, s)
                _mode, _nrefs, _blocked = (_vm2 or "BLOCKED"), len(_rv2 or []), bool(_why2)
            else:
                # WAN ignores the KF roles on purpose (s2v IS the lip-sync path) — say so on the card
                # rather than showing roles that do nothing.
                _mode, _nrefs, _blocked = "s2v", (1 if st.get("ref") else 0), False
            _mode_log.append(_mode)
            _mrow = tk.Frame(body, bg=bg)
            _mrow.pack(fill="x", pady=(2, 0))
            tk.Label(_mrow, text=_mode.upper(),
                     bg=(C["rust_deep"] if _blocked else (C["moss"] if k else C["panel2"])),
                     fg=("#fff" if (_blocked or k) else C["ink"]),
                     font=fmono, padx=5, pady=0).pack(side="left")
            if _nrefs:
                tk.Label(_mrow, text="%d ref%s" % (_nrefs, "" if _nrefs == 1 else "s"),
                         bg=bg, fg=C["faint"], font=fsm).pack(side="left", padx=(5, 0))
            # ⚠️ The beat's motion intent, when set. It changes nothing about what will RUN, so it
            # gets its own neutral chip rather than joining the mode chip's log.
            _beat_log.append(s.get("motion") or "")
            if (s.get("motion") or ""):
                tk.Label(_mrow, text=(s.get("motion") or "").upper(), bg=C["panel2"],
                         fg=C["ink"], font=fmono, padx=5, pady=0).pack(side="left", padx=(5, 0))
            _bind_click(card, lambda e, j=i: _pick_scene(j))
        st["board_modes"] = _mode_log
        st["board_motions"] = _beat_log

    def set_view(is_board):
        """TABLE <-> BOARD. leftp is a fixed-width, pack_propagate(False) frame, so widening it is
        one configure(). ⚠️ `before=ops` is load-bearing: pack appends to the END of the packing
        list, so repacking without it drops the view BELOW the +ADD/−DEL/▲▼ row."""
        st["board"] = bool(is_board)
        if st["board"]:
            leftp.configure(width=700)
            tree.pack_forget()
            board.pack(fill="both", expand=True, before=ops)
            bview.configure(text="◂ TABLE", bg=C["rust_deep"], fg="#fff")
        else:
            board.pack_forget()
            tree.pack(fill="both", expand=True, padx=10, pady=(0, 10), before=ops)
            leftp.configure(width=430)
            bview.configure(text="BOARD ▸", bg=C["panel2"], fg=C["ink"])
        render_board(); render_queue()
    bview.configure(command=lambda: set_view(not st["board"]))

    # ── DRAFT KEYFRAMES — the board's whole point (§3b). LOCAL FORGE ONLY (D6.1) ────────
    KF_STEPS = QWEN_REF_STEPS

    def _kf_mode():
        """⚠️ `edit` when the piece carries a reference still. Stills take identity from the REF,
        not from a LoRA — a still CANNOT load the ref2va LoRA at all, which is exactly why the
        reference picture outvotes the wardrobe text."""
        return "edit" if st.get("ref") else "t2i"

    def _kf_size():
        """The keyframe canvas IS the locked treatment canvas. A still at a different size is not
        a preview of the scene, it is just a picture (§3b)."""
        if long_engine(plan) == "h3":
            return long_canvas(plan)
        return (S2V_SIZE[0], S2V_SIZE[1])

    def _kf_params(s, seed, slot="still"):
        w, h = _kf_size()
        _sub = _subject()
        # ⚠️ An explicit per-slot prompt wins; otherwise it derives from the scene prompt. The
        # worker records what it used, so an approved still is not silently re-prompted when the
        # scene's MOTION prompt is edited later.
        _own = s.get("still_last_prompt" if slot == "still_last" else "still_prompt")
        return {"prompt": ((_sub + " ") if _sub else "") + (_own or (s["prompt"] or "")),
                "width": w, "height": h, "steps": KF_STEPS,
                "resolution": int(round((w * h) ** 0.5)),
                # ⚠️ custom_size ON: without it an `edit` still inherits <image1>'s canvas and
                # comes back at a different aspect from the video it is meant to preview.
                "custom_size": True,
                "seed": seed, "prefix": "image/forge_lab_kf"}

    def _kf_slots(s):
        """Which still slots a scene actually needs, given its ROLES. ⚠️ The role creates the
        need, and that is the whole point of flags over a dropdown:
          · `first` / `ref` want the opening picture (`still`)
          · `last`          wants the landing picture (`still_last`) — a SECOND submission
          · `cont`          supplies its start from the previous scene, so it wants NO `still`
                            — but still wants a landing picture when `last` is set
        A scene with no roles at all needs nothing: it is text-only."""
        out = []
        if (s.get("kf_first") or s.get("kf_ref")) and not s.get("kf_cont"):
            out.append("still")
        if s.get("kf_last"):
            out.append("still_last")
        return out

    def _kf_todo():
        """⚠️ Returns (scene, SLOT) PAIRS, not scenes. One scene can be TWO submissions once
        `last` is on, and the old scene-only version skipped every `cont` scene outright — so a
        `cont` + `last` scene silently got no landing still at all."""
        todo = []
        for s in plan["scenes"]:
            for slot in _kf_slots(s):
                if not s.get(slot):
                    todo.append((s, slot))
        return todo

    def kf_plan_info():
        """What the UI needs to know BEFORE anything is spent — the estimate is pure maths."""
        todo = _kf_todo()
        mode = _kf_mode()
        w, h = _kf_size()
        per = 0.0
        if todo:
            try:
                per = estimate_seconds(mode, _kf_params(todo[0][0], -1, todo[0][1]),
                                       [st["ref"]] if st.get("ref") else [], warm_engine=None)
            except Exception:
                per = 0.0
        return {"mode": mode, "w": w, "h": h, "todo": len(todo), "per": per,
                "total": per * len(todo), "steps": KF_STEPS,
                "ref": os.path.basename(st["ref"]) if st.get("ref") else None}

    def _kf_worker():
        """SEQUENTIALLY, like _worker — one 3090, so parallel submits only queue.
        ⚠️ Works off (scene, SLOT) pairs: once `last` is on, one scene is TWO submissions."""
        st["kf_drafting"] = True
        try:
            mode = _kf_mode()
            refs = [st["ref"]] if st.get("ref") else []
            for s, slot in _kf_todo():
                p = _kf_params(s, random.randint(0, 2**31 - 1), slot)
                try:
                    pid, _sd = submit(mode, p, refs)
                except Exception as exc:
                    s["still_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:90])
                    continue
                r = {"state": "gone"}
                for _ in range(900):
                    time.sleep(4)
                    r = run_state(pid)
                    if r["state"] in ("done", "error", "gone"):
                        break
                if r["state"] == "done":
                    m = collect(pid) or {}
                    imgs = [f["file"] for f in (m.get("images") or [])
                            if str(f.get("file", "")).lower().endswith(
                                (".png", ".jpg", ".jpeg", ".webp"))]
                    if imgs:
                        s[slot] = os.path.join(RUNS, pid, imgs[0])
                        s["still_last_prompt" if slot == "still_last"
                          else "still_prompt"] = p["prompt"]
                        s.pop("still_error", None)
                    else:
                        s["still_error"] = "no image came back"
                else:
                    s["still_error"] = str(r.get("detail") or r["state"])[:90]
                try:
                    win.after(0, refresh)
                except Exception:
                    pass
        finally:
            st["kf_drafting"] = False
            try:
                win.after(0, refresh)
            except Exception:
                pass

    def draft_keyframes():
        """Estimate BEFORE spend, every time — the house rule."""
        info = kf_plan_info()
        if not info["todo"]:
            refuse.configure(
                text="Nothing to keyframe: every scene that CAN have a still already has one. "
                     "`continue from prev` scenes never get one — they inherit the previous "
                     "scene's last frame, so what wants approving is that scene, not this one.")
            refuse.pack(fill="x", padx=12, pady=(8, 0))
            return
        refuse.pack_forget()
        top = tk.Toplevel(win)
        top.title("Draft keyframes")
        top.configure(bg=C["bg"])
        top.transient(win)
        tk.Label(top, text="DRAFT KEYFRAMES", bg=C["bg"], fg=C["ochre"],
                 font=fmono).pack(anchor="w", padx=16, pady=(14, 3))
        tk.Label(top, text="%d still%s · %d×%d · %d steps\n≈ %s each  ·  ≈ %s total\n%s\n"
                           "LOCAL FORGE ONLY — nothing is billed; the 3090 is the cost"
                      % (info["todo"], "" if info["todo"] == 1 else "s", info["w"],
                         info["h"], info["steps"], fmt(info["per"]), fmt(info["total"]),
                         ("image-edit from %s" % info["ref"]) if info["ref"]
                         else "text to image — no reference chosen"),
                 bg=C["bg"], fg=C["muted"], font=fsm, justify="left").pack(anchor="w", padx=16)

        def go():
            top.destroy()
            threading.Thread(target=_kf_worker, daemon=True).start()

        row = tk.Frame(top, bg=C["bg"])
        row.pack(anchor="w", padx=16, pady=(12, 14))
        tk.Button(row, text="DRAFT %d" % info["todo"], bg=C["ochre"], fg="#14110f",
                  activebackground="#d9b45e", activeforeground="#14110f", relief="flat",
                  font=fsm, padx=12, pady=6, borderwidth=0, command=go).pack(side="left")
        tk.Button(row, text="CANCEL", bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                  padx=12, pady=6, borderwidth=0,
                  command=top.destroy).pack(side="left", padx=(8, 0))
    b_kf.configure(command=draft_keyframes)

    def refresh():
        render_treat(); render_queue(); render_bench(); render_steps(); render_cost()
        render_board()
        # ⚠️ ONLY save once an episode has actually been opened or created. The constructor calls
        # refresh() too, and saving there wrote a BLANK 12-scene plan over
        # episodes/untitled/plan.json — every single time the window opened, destroying whatever
        # was stored under the default name. Caught by the TESTKF smoke run, which saw the file
        # change under it despite being deliberately pointed at test-scratch.
        if st.get("loaded"):
            long_plan_save(st["name"], plan)

    # ── interactions ───────────────────────────────────────────────────────────────
    def on_pick(_e=None):
        sel = tree.selection()
        if sel:
            st["sel"] = int(sel[0])
            st["takes_open"] = 0
            render_bench()
            render_cost()
            render_board()

    tree.bind("<<TreeviewSelect>>", on_pick)
    tree.bind("<ButtonRelease-1>", on_pick)

    def on_filter():
        st["filter"] = not st["filter"]
        flt.configure(bg=(C["rust_deep"] if st["filter"] else C["panel2"]),
                      fg=("#fff" if st["filter"] else C["ink"]))
        render_queue()
    flt.configure(command=on_filter)

    def on_prompt_change():
        """The per-scene prompt editor — saves as you type, so nothing is lost on a reroll."""
        s = plan["scenes"][st["sel"]]
        s["prompt"] = pedit.get("1.0", "end").rstrip("\n")
        render_queue()
        long_plan_save(st["name"], plan)
    pedit.bind("<KeyRelease>", lambda e: on_prompt_change())

    def _eng_mode():
        return "video" if long_engine(plan) == "h3" else "wan"

    # ── THE ONE PLACE the KF flags become a VIDEO MODE ──────────────────────────────
    # Until this exists the flags are decoration: `_worker` built r2v/s2v from the treatment
    # alone and never looked at a still. This is the step that makes the board mean something.
    def _cont_src(i):
        """The frame scene `i` continues FROM: the last frame of scene i-1's KEPT take.
        ⚠️ Exactly i-1, not "the nearest earlier keeper" — `continue from prev` should mean the
        scene before this one, and quietly reaching further back would join shots the piece never
        said were joined."""
        if i <= 0:
            return None
        sc = plan["scenes"][i - 1]
        k = sc.get("keep")
        takes = sc.get("takes") or []
        t = takes[k] if (k is not None and k < len(takes)) else None
        f = (t or {}).get("file")
        if not (f and os.path.exists(f)):
            return None
        return long_last_frame(f, os.path.join(RUNS, "_cont_%s_%02d.png"
                                               % (st["name"], i + 1)))

    def _scene_video_plan(i, s):
        """(video_mode, refs, None) — or (None, None, why) when the flags cannot be satisfied.

        ⚠️ NEVER silently degrade. An flf job handed ONE still has its `last` set to None by the
        graph builder and runs as a plain first-frame shot — it submits, completes, and does
        something else entirely (forge_lab.py, `_last = ...`). Every unsatisfiable case is
        refused here instead, so it shows up in TAKES as `blocked:` rather than as a wrong shot.
        """
        stl, lst = s.get("still"), s.get("still_last")
        _pf = _cont_src(i) if s.get("kf_cont") else None
        if s.get("kf_cont") and not _pf:
            return None, None, ("continue-from-prev needs scene %d rendered AND kept — there is "
                                "no frame to inherit yet" % (s["n"] - 1))
        if s.get("kf_last"):
            if not lst:
                return None, None, "last frame is ticked but no landing still has been drafted"
            first = _pf if s.get("kf_cont") else (stl if s.get("kf_first") else None)
            if not first:
                return None, None, ("last frame is ticked but nothing supplies the FIRST frame — "
                                    "tick 'first frame' or 'continue from prev'")
            return "flf", [first, lst], None      # ref 1 = first frame, ref 2 = last
        if s.get("kf_cont"):
            return "cont", [_pf], None
        if s.get("kf_first") and stl:
            return "i2v", [stl], None             # fl2va — animate one keyframe
        # ⚠️ A TICKED ROLE WITH NOTHING TO SATISFY IT IS REFUSED, never quietly rerouted. Falling
        # through to the no-role branch below renders a DIFFERENT shot that submits fine and
        # reports no error — the silent degradation this function exists to prevent. `first frame`
        # with no still used to land here and come back as a piece-reference `r2v`.
        if s.get("kf_first"):
            return None, None, ("'first frame' is ticked but no opening still has been drafted — "
                                "press DRAFT KEYFRAMES, or untick it")
        if s.get("kf_ref") and not stl:
            return None, None, ("'reference' is ticked but no still has been drafted to add to "
                                "the reference set — press DRAFT KEYFRAMES, or untick it")
        # No role at all: keep the behaviour this window had before the board existed — the PIECE
        # reference carries identity, and nothing else is added.
        refs = ([st["ref"]] if st.get("ref") else [])
        if s.get("kf_ref"):
            refs.append(stl)
        return ("r2v" if refs else "t2v"), refs, None

    def _scene_params(i, s, seed, _win=0.0, _dur=0.0, sliced=True):
        """(mode, params, refs, why) for ONE scene — `why` set (and params None) when the scene's
        flags cannot be satisfied.

        ⚠️ THE ONE PLACE a long-video scene's params are built. `_worker` SUBMITS exactly what this
        returns, and `long_scene_cost()` PRICES exactly what this returns, so the number you plan
        around cannot drift from the run. That drift was the whole bug in `render_cost`: it priced
        every piece with S2V's constants whatever engine was selected.

        ⚠️ `sliced=False` skips writing this scene's audio window. An estimate has NO side effects —
        it runs on every repaint, and it must not litter `runs/` with `_guide_*.wav` for a piece
        nobody submitted.
        """
        _sub = _subject()
        txt = ((_sub + " ") if _sub else "") + (s["prompt"] or "")
        if long_engine(plan) == "h3":
            # ⚠️ THE SCENE'S FLAGS DECIDE THE MODE AND ITS REFERENCE ORDER. Ref order IS the
            # wiring: ref 1 is the first frame, ref 2 the last. A blocked scene is recorded as
            # `blocked:` and skipped — never rendered as something else.
            _vm, _refs, _why = _scene_video_plan(i, s)
            if not _vm:
                return None, None, None, _why
            p = {"prompt": txt, "video_mode": _vm,
                 "lora": long_lora(plan), "lora_strength": 1.0,
                 "steps": int(plan["treatment"].get("h3_steps", H3_LONGFORM_STEPS)),
                 # ⚠️ The SAME ladder the slider shows. If these two ever diverge, the readout
                 # is a lie about the render.
                 "length": (h3_frames(float(s.get("secs") or 0)) if (s.get("secs") or 0)
                            else H3_SCENE_FRAMES.get(int(s.get("chunks", 3)), 355)),
                 "width": long_canvas(plan)[0], "height": long_canvas(plan)[1],
                 "seed": seed, "audio": True,
                 # THIS scene's window of the track, not the whole song
                 "guide_audio_path": (long_scene_slice(
                     st["audio"], _win, _dur,
                     os.path.join(RUNS, "_guide_%s_%02d.wav" % (st["name"], s["n"])))
                     if (sliced and st.get("audio")) else None),
                 "prefix": "video/forge_lab_h3"}
            return _eng_mode(), p, _refs, None
        # ⚠️ WAN stays on s2v: that path IS the lip-sync one, and its reference is the piece's
        # identity still. The per-scene KF roles are an H3/fl2va idea and are deliberately NOT
        # wired here yet.
        p = {"prompt": txt,
             "wan_mode": "s2v", "s2v_chunks": int(s.get("chunks", 3)),
             "width": S2V_SIZE[0], "height": S2V_SIZE[1], "seed": seed,
             "quality": S2V_QUALITY, "prefix": "video/forge_lab_s2v",
             "audio_path": st["audio"]}
        return _eng_mode(), p, ([st["ref"]] if st.get("ref") else []), None

    def long_scene_cost(i, s, warm_engine=None, seed=0):
        """(seconds, mode, why) for ONE scene — priced through the SAME params `_worker` submits.

        ⚠️ `warm_engine` matters: there is ONE 3090, so the first scene of a run pays the engine
        load and the rest do not. Carrying it across the loop is what stops a 106 s job reading as
        a 320 s estimate.
        """
        _m, p, _refs, _why = _scene_params(i, s, seed, sliced=False)
        if not p:
            return None, None, _why
        return float(estimate_seconds(_m, p, _refs, warm_engine)), _m, None

    def _worker(seed_for):
        """Draft the scenes SEQUENTIALLY — one 3090, so parallel submits would just queue."""
        st["drafting"] = True
        try:
            _eng0 = long_engine(plan)
            _t = 0.0                      # this scene's offset in the FINISHED piece
            for _i, s in enumerate(plan["scenes"]):
                _dur = long_scene_seconds_for(s.get("chunks", 3), _eng0, s.get("secs", 0.0))
                _win = _t
                _t += _dur               # advance for EVERY scene, drafted or skipped
                if seed_for is True and s.get("keep") is not None:
                    continue
                seed = random.randint(0, 2**31 - 1) if seed_for is True else seed_for
                # ⚠️ The SAME builder `long_scene_cost()` prices with — see `_scene_params`.
                _em, p, _refs, _why = _scene_params(_i, s, seed, _win, _dur)
                if not p:
                    s.setdefault("takes", []).append(
                        {"seed": None, "status": "blocked: %s" % _why})
                    try:
                        win.after(0, refresh)
                    except Exception:
                        pass
                    continue
                try:
                    pid, sd = submit(_em, p, ref_paths=_refs)
                except Exception as exc:
                    s.setdefault("takes", []).append(
                        {"seed": seed, "status": "error: %s" % str(exc)[:90]})
                    continue
                t = {"pid": pid, "seed": sd, "status": "running"}
                s.setdefault("takes", []).append(t)
                for _ in range(900):
                    time.sleep(4)
                    r = run_state(pid)
                    if r["state"] in ("done", "error", "gone"):
                        break
                if r["state"] == "done":
                    m = collect(pid) or {}
                    files = [os.path.join(RUNS, pid, f["file"]) for f in (m.get("images") or [])]
                    muxed = next((f for f in files if f.endswith("s2v_synced.mp4")), None)
                    clean = next((f for f in files if not f.endswith("s2v_synced.mp4")), None)
                    # ⚠️ The STITCH SOURCE is the SILENT render. Concatenating the muxed files
                    # with -c copy mangles the video: each carries its own AAC track, and the
                    # result came out at 4.75 s of video in a 20 s container. Scenes are silent
                    # by design, so the bed track goes on ONCE, at the end.
                    t.update({"status": "done", "file": clean or muxed, "synced": muxed})
                else:
                    t.update({"status": r["state"], "detail": r.get("detail", "")})
                try:
                    win.after(0, refresh)
                except Exception:
                    pass
        finally:
            st["drafting"] = False
            try:
                win.after(0, refresh)
            except Exception:
                pass

    def preflight():
        """⚠️ THE MOMENT THE GPU IS SPENT. Everything needed to say yes, in one place: how many
        scenes will run, how many are already kept, how many will be REFUSED and why, the engine and
        step path, and the total. Estimate-before-spend is a standing requirement, and DRAFT ALL is
        the one control that commits the whole piece — so it asks here instead of starting a
        two-hour run off a single click.

        The numbers come from `_cost_facts()`, the SAME computation as the footer, so the dialog
        cannot quote something the footer does not say.
        """
        _f = _cost_facts()
        _n = len(_f["run"])
        if not _n:
            refuse.configure(text="every scene is kept — nothing to run")
            return
        top = tk.Toplevel(win)
        top.title("Draft all %d scenes?" % _n)
        top.configure(bg=C["bg"])
        top.transient(win)
        tk.Label(top, text="DRAFT ALL · %d SCENE%s" % (_n, "" if _n == 1 else "S"),
                 bg=C["bg"], fg=C["ochre"], font=fmono).pack(anchor="w", padx=16, pady=(14, 6))
        _bk = ("none" if not _f["blocked"] else
               "%d — recorded as blocked and SKIPPED:\n    %s"
               % (_f["blocked"], "\n    ".join("scene %s: %s" % (_sn, _sw)
                                               for _sn, _sw in _f["blocked_why"][:4])))
        tk.Label(top, text=(
            "engine    %s\n"
            "scenes    %d to run%s\n"
            "blocked   %s\n"
            "estimate  ≈ %s forge time\n"
            "          ≈ %.0f s each — the first scene pays the engine load\n"
            "billing   none · LOCAL forge only — the 3090 is the cost"
            % (_f["eng"], _n,
               ("" if not _f["kept"] else "  ·  %d kept" % _f["kept"]),
               _bk, fmt(_f["total"] / 60),
               (_f["total"] / _f["priced"]) if _f["priced"] else 0.0)),
            bg=C["bg"], fg=C["ink"], font=fsm, justify="left", anchor="w",
            wraplength=560).pack(anchor="w", padx=16)
        _acts = tk.Frame(top, bg=C["bg"])
        _acts.pack(anchor="w", padx=16, pady=(12, 16))

        def _go():
            top.destroy()
            threading.Thread(target=_worker, args=(True,), daemon=True).start()

        tk.Button(_acts, text="DRAFT ALL %d" % _n, bg=C["rust_deep"], fg="#fff", relief="flat",
                  font=fsm, padx=14, pady=7, borderwidth=0, command=_go).pack(side="left")
        tk.Button(_acts, text="CANCEL", bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                  padx=14, pady=7, borderwidth=0,
                  command=top.destroy).pack(side="left", padx=(8, 0))
        # ⚠️ This Toplevel is built ON CLICK, long after `_btn_colors(win)` ran at the end of
        # construction — without this its buttons render dark, out of step with the rest of the app
        # (the documented on-demand-window trap; `_btn_colors` is a launch() local, so it arrives
        # through ctx).
        if ctx.get("btn_colors"):
            try:
                ctx["btn_colors"](top)
            except Exception:
                pass

    b_roll.configure(command=lambda: threading.Thread(
        target=_worker, args=(None,), daemon=True).start())
    # ⚠️ DRAFT ALL asks first — see `preflight`. REROLL stays immediate: it is ONE scene.
    b_draft.configure(command=preflight)
    b_rollun.configure(command=lambda: threading.Thread(
        target=_worker, args=(True,), daemon=True).start())

    def on_keep():
        s = plan["scenes"][st["sel"]]
        if not (s.get("takes") or []):
            refuse.configure(text="Nothing to keep yet — draft this scene first.")
            refuse.pack(fill="x", padx=12, pady=(8, 0))
            return
        s["keep"] = st["takes_open"] or 0
        refuse.pack_forget()
        refresh()

    b_keep.configure(command=on_keep)

    def do_stitch():
        """STAGE 2. Keepers in order -> one file, with the bed track under it.
        ⚠️ Reports the runtime ffprobe reads off the WRITTEN FILE — not the sum of the scene
        lengths. A stitch that "should" be 29.6 s and reads 9.6 s is the bug this catches."""
        n = len(plan["scenes"])
        todo, files = [], []
        for sc in plan["scenes"]:
            k = sc.get("keep")
            takes = sc.get("takes") or []
            tk_ = takes[k] if (k is not None and k < len(takes)) else None
            f_ = (tk_ or {}).get("file")
            if not (f_ and os.path.exists(f_)):
                todo.append(str(sc["n"]))
            files.append(f_)
        if todo:
            refuse.configure(text="Cannot stitch: scenes %s have a KEPT take whose file is "
                                  "not on disk. Reroll and keep one for %s."
                             % (", ".join(todo), "them" if len(todo) > 1 else "it"))
            refuse.pack(fill="x", padx=12, pady=(8, 0))
            return
        noisy = [os.path.basename(f) for f in files
                 if "Audio" in subprocess.run(
                     ["ffprobe", "-v", "error", "-select_streams", "a",
                      "-show_entries", "stream=codec_type", "-of", "csv=p=0", f],
                     capture_output=True, text=True).stdout]
        if noisy:
            refuse.configure(text="Cannot stitch: %d source clip(s) still carry their own "
                                  "audio track (%s). Scenes must be concatenated SILENT and "
                                  "the bed track added once — mixing them mangles the video."
                             % (len(noisy), ", ".join(noisy[:3])))
            refuse.pack(fill="x", padx=12, pady=(8, 0))
            return
        d = os.path.join(LONG_EPISODES, st["name"])
        os.makedirs(d, exist_ok=True)
        lst = os.path.join(d, "concat.txt")
        with open(lst, "w") as fh:
            for f_ in files:
                fh.write("file '%s'\n" % f_.replace("'", "'\\''"))
        silent = os.path.join(d, "stitched_silent.mp4")
        # The concat DEMUXER with -c copy is lossless, and MEASURED equal to the concat
        # filter (both 1116 frames for 9x124), so it stays. I briefly "fixed" a seam frame
        # drop here that did not exist — the loss was downstream, in the mux.
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", lst, "-c", "copy", silent], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(silent):
            refuse.configure(text="Stitch failed at the concat step: %s"
                             % (r.stderr or "no output")[:180])
            refuse.pack(fill="x", padx=12, pady=(8, 0))
            return
        out = silent
        audio = st.get("audio")
        if audio and os.path.exists(audio):
            merged = os.path.join(d, "final.mp4")
            # ⚠️ THE FRAME-LOSS FIX. With a plain -shortest the output ends at the SHORTER
            # stream, so a bed track even 0.03 s short of the picture silently trims the tail:
            # a 9-scene piece read 1112 frames instead of 1116, i.e. ~42 ms of picture gone at
            # the very end. `-af apad` pads the audio with silence indefinitely, which makes
            # the audio the LONGER stream — so -shortest then ends at the PICTURE and no frame
            # is lost. Measured: -shortest -> 1112 frames; -af apad -shortest -> 1116.
            r2 = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", audio,
                                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                                 "-c:a", "aac", "-b:a", "192k", "-af", "apad", "-shortest",
                                 merged], capture_output=True, text=True)
            if r2.returncode == 0 and os.path.exists(merged):
                out = merged
        dur = ""
        try:
            dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", out], capture_output=True,
                                 text=True).stdout.strip()
        except Exception:
            pass
        # ⚠️ -shortest ends the file at the SHORTER stream. For a music video that is
        # correct (the song sets the runtime) — but it must never happen SILENTLY. This run
        # lost 11 frames to a bed track 0.33 s shorter than the picture, and said nothing.
        pic = 0.0
        try:
            pic = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                        "format=duration", "-of", "csv=p=0", silent],
                                       capture_output=True, text=True).stdout.strip() or 0)
        except Exception:
            pass
        _fps = 24 if long_engine(plan) == "h3" else 16
        bd = 0.0
        try:
            if audio and os.path.exists(audio):
                bd = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                           "format=duration", "-of", "csv=p=0", audio],
                                          capture_output=True, text=True).stdout.strip() or 0)
        except Exception:
            bd = 0.0
        cut = ""
        if pic and bd:
            if bd + 0.04 < pic:
                cut = ("\ntrack %.2f s is SHORTER than the picture %.2f s — padded with %.2f s "
                       "of silence so no frame is lost" % (bd, pic, pic - bd))
            elif bd - 0.04 > pic:
                cut = ("\ntrack %.2f s runs PAST the picture %.2f s — trimmed by %.2f s"
                       % (bd, pic, bd - pic))
        plan["stitched"] = out
        plan["stitch_runtime"] = dur
        plan["bed_track_cut_s"] = round(max(0.0, pic - float(dur)), 3) if (pic and dur) else 0.0
        long_plan_save(st["name"], plan)
        refuse.configure(text="Stitched %d scenes -> %s\nreal runtime %s s · audio %s%s"
                         % (n, out, dur or "?", os.path.basename(audio) if audio else "none",
                            cut))
        refuse.pack(fill="x", padx=12, pady=(8, 0))
        try:
            subprocess.Popen(["open", out])
        except Exception:
            pass

    def on_stitch():
        ko, n = kept_n(), len(plan["scenes"])
        if ko < n:
            missing = [str(s["n"]) for s in plan["scenes"] if s.get("keep") is None]
            refuse.configure(
                text="Cannot stitch yet. %d of %d scenes have a keeper — %d have none "
                     "(scenes %s). Nothing is stitched from a partial set: you would get a "
                     "silently short piece."
                     % (ko, n, n - ko, ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")))
            refuse.pack(fill="x", padx=12, pady=(8, 0))
            return
        refuse.pack_forget()
        do_stitch()

    b_stitch.configure(command=on_stitch)

    def on_step(name):
        if name == "Scenes":
            return
        if name == "Stitch":
            on_stitch()
        else:
            refuse.configure(text="Treatment is LOCKED for this piece. To change it, type a "
                                  "new name in EPISODE and press NEW — that starts a fresh "
                                  "treatment and a fresh plan.")
            refuse.pack(fill="x", padx=12, pady=(8, 0))

    for v, (b, _h) in step_btns.items():
        for w in [b] + list(b.winfo_children()):
            w.bind("<Button-1>", lambda e, v=v: on_step(v))

    # ── load a plan, or start one ─────────────────────────────────────────────────
    def load_plan(name, audio=None, ref=None, scenes=None, subject_block=None):
        got = long_plan_load(name)
        if got:
            plan.update(got)
        st["name"] = name
        if audio:
            st["audio"] = audio; plan["audio"] = audio
        elif plan.get("audio"):
            st["audio"] = plan["audio"]
        if ref:
            st["ref"] = ref; plan["ref"] = ref
        elif plan.get("ref"):
            st["ref"] = plan["ref"]
        if scenes:
            plan["scenes"] = scenes
        refresh()
        return plan

    win.load_plan = load_plan
    win.long_refresh = refresh
    win.long_state = st
    win.long_plan = plan

    def _view_above_ops():
        """⚠️ pack appends to the END of the packing list, so repacking a view without `before=ops`
        silently drops it BELOW the +ADD/−DEL/▲▼ row. Assert the ORDER, not just the mapping."""
        vis = board if st["board"] else tree
        order = list(leftp.pack_slaves())
        return vis in order and ops in order and order.index(vis) < order.index(ops)

    def _board_geom():
        """⚠️ Assert 4 ACROSS and every card INSIDE the panel. A card pushed outside by a
        too-narrow leftp is invisible — the same failure TESTTAKES exists for on the takes row,
        and the reason leftp widens at all."""
        cards = board_cards.winfo_children()
        rows = {}
        for c in cards:
            g = c.grid_info()
            rows[g["row"]] = rows.get(g["row"], 0) + 1
        bw = board_cards.winfo_width()
        off = 0
        for c in cards:
            right = (c.winfo_rootx() - board_cards.winfo_rootx()) + c.winfo_width()
            if right > bw + 1:
                off += 1
        return {"cols": max(rows.values()) if rows else 0, "rows": len(rows),
                "off": off, "panel": bw}

    def long_dump():
        """Everything the test driver needs to assert what is ON SCREEN."""
        return {"caption": bcap.cget("text"),
                "steps": {v: h.cget("text") for v, (_b, h) in step_btns.items()},
                "queue_rows": len(tree.get_children()),
                "queued": qn.cget("text"),
                "prompt": pedit.get("1.0", "end").strip(),
                "takes": len(plan["scenes"][st["sel"]].get("takes") or []),
                "kept": kept_n(),
                "refusal": refuse.cget("text") if refuse.winfo_ismapped() else "",
                "cost": cost.cget("text").replace("\n", " | "),
                "treatment": tsum.cget("text"),
                "window": wlab.cget("text"),
                "filter": st["filter"],
                "board": {"on": bool(st["board"]),
                          "cards": len(board_cards.winfo_children()),
                          "left": leftp.winfo_width(),
                          "tree_shown": int(tree.winfo_ismapped()),
                          "board_shown": int(board.winfo_ismapped()),
                          "view_btn": bview.cget("text"),
                          "thumbs": len(board_refs),
                          "geom": _board_geom(),
                          "order_ok": _view_above_ops()},
                "stills": len([s for s in plan["scenes"] if s.get("still")]),
                "kf": kf_summary(plan["scenes"][st["sel"]]),
                "kf_flags": {k: bool(plan["scenes"][st["sel"]].get(k)) for k, _l in KF_LABELS},
                "kf_cont_state": str(kf_btns["kf_cont"].cget("state")),
                "kf_note": kf_note.cget("text"),
                # ⚠️ What every card is PROMISING (see render_board) — the board must agree with
                # `_scene_video_plan`, which is the same call the run makes.
                "board_modes": st.get("board_modes") or [],
                "kf_btn": b_kf.cget("text"),
                "kf_btn_state": str(b_kf.cget("state")),
                "kf_drafting": bool(st.get("kf_drafting")),
                "kf_need": sum(len(_kf_slots(s)) for s in plan["scenes"]),
                "kf_todo": len(_kf_todo()),
                "slots": {k: bool(plan["scenes"][st["sel"]].get(k))
                          for k in ("still", "still_last")},
                # ⚠️ What the BEAT row SHOWS vs what the plan HOLDS. A driver must be able to
                # assert these AGREE: a control that displays one value and saves another is the
                # "control that lies" failure this app has already shipped once.
                "beat_shown": {"motion": ("" if motion_var.get() == BEAT_UNSET
                                          else motion_var.get()),
                               "color_state": color_e.get().strip(),
                               "transition_out": trans_e.get().strip()},
                "beat_saved": {k: (plan["scenes"][st["sel"]].get(k) or "")
                               for k in ("motion", "color_state", "transition_out")},
                "motion_intents": list(MOTION_INTENTS),
                "board_motions": st.get("board_motions") or [],
                # ⚠️ The LENGTH readout vs what the RENDERER will use. Asserting these agree is the
                # guard for the bug this control shipped with: the dropdown showed WAN labels on H3
                # ('4.81 s  (1 chunk)') while H3 actually rendered 5.17 s.
                "len_control": st.get("len_control"),
                "h3_ladder": list(H3_LADDER_S),
                "h3_frames_ladder": list(H3_LADDER_FRAMES),
                "len_shown": (H3_LADDER_S[max(0, min(len(H3_LADDER_S) - 1, int(h3_len_var.get())))]
                              if st.get("len_control") == "h3" else len_var.get()),
                "len_saved": long_scene_seconds_for(
                    plan["scenes"][st["sel"]].get("chunks", 3), long_engine(plan),
                    plan["scenes"][st["sel"]].get("secs", 0.0)),
                "scene_secs": plan["scenes"][st["sel"]].get("secs", 0.0),
                "subject": _subject(),
                "subject_len": len(_subject()),
                "treatment_detail": tdet.cget("text"),
                "ep_label": ep_lbl.cget("text"),
                "loaded": bool(st.get("loaded")),
                "draft_btn": b_draft.cget("text"),
                "draft_state": str(b_draft.cget("state")),
                "scenes": len(plan["scenes"])}

    win.long_dump = long_dump
    win.long_pedit = pedit
    win.long_beat_apply = beat_apply
    win.long_beat_widgets = (motion_var, color_e, trans_e)
    win.long_h3_len = apply_h3_len
    win.long_len_widgets = (h3_len_var, h3_len_lbl, len_slider, len_menu)
    win.long_wav = wav
    win.long_prompt_sync = on_prompt_change
    win.long_set_audio = lambda q: (st.__setitem__("audio", q),
                                     plan.__setitem__("audio", q), refresh())
    win.long_set_ref = lambda q: (st.__setitem__("ref", q),
                                  plan.__setitem__("ref", q), refresh())
    win.long_stitch_for_test = on_stitch
    win.long_stitch = do_stitch
    win.long_worker = _worker
    win.long_apply_len = apply_sel_len
    win.long_len_var = len_var
    win.long_episode = new_episode
    win.long_set_lora = lambda k: (plan.setdefault("treatment", {}).__setitem__(
        "lora", k), lo_var.set(k), refresh())
    win.long_set_canvas = lambda lab_: (plan.setdefault("treatment", {}).__setitem__(
        "canvas", lab_), cv_var.set(lab_), refresh())
    win.long_set_engine = lambda k: (plan.setdefault("treatment", {}).__setitem__(
        "engine", k), eng_var.set(ENGINES[k]), refresh())
    win.long_pick = lambda i: (st.__setitem__("sel", i), render_bench(), render_cost())
    win.long_board = set_view
    win.long_kf = toggle_kf
    win.long_kf_info = kf_plan_info
    win.long_kf_draft = draft_keyframes
    win.long_kf_worker = _kf_worker
    win.long_kf_slots = _kf_slots
    win.long_kf_todo = _kf_todo
    win.long_video_plan = _scene_video_plan
    win.long_scene_cost = long_scene_cost
    win.long_scene_params = _scene_params
    win.long_cost_facts = _cost_facts
    win.long_preflight = preflight
    # ⚠️ The long window's OWN duration formatter ("M:SS"). There are two `fmt` in this file — this
    # one and launch()'s ("Xm YYs") — so a driver in launch()'s scope comparing against its own
    # formatter silently mismatches. Export it and assert through THIS one.
    win.long_fmt = fmt
    win.long_cont_run_apply = apply_cont_run
    win.long_subject = _subject
    win.long_set_subject = lambda q: (plan.__setitem__("subject_block", q),
                                      st.__setitem__("subject_block", q), refresh())
    win.long_kf_prompt = lambda: _kf_params(plan["scenes"][st["sel"]], -1)["prompt"]
    win.long_kf_prompt_slot = lambda slot: _kf_params(plan["scenes"][st["sel"]], -1,
                                                      slot)["prompt"]
    win.long_spwrap = spwrap
    win.long_tline = tline
    win.long_tctl = tctl
    win.long_rv_canvas = rv_canvas
    win.long_rv_inner = rv_inner
    win.long_open = open_episode
    win.long_name_entry = name_e

    # ⚠️ macOS does not honour a custom button fill, so every button in this app is repainted
    # light-with-dark-text — that IS the convention (STATUS.md: "macOS does not honour the fill").
    # `_btn_colors(root)` runs once at the end of launch(), but THIS window is built on demand long
    # after that, so its buttons were never painted and stayed dark — the one window in the app out
    # of step with the rest. Reached through ctx because _btn_colors is a local of launch().
    if ctx.get("btn_colors"):
        try:
            ctx["btn_colors"](win)
        except Exception:
            pass

    refresh()
    return win


# =============================================================== tkinter app
def open_weirdo(root, ctx):
    """WEIRDO — the one-press studio. Roll a character, look at it, make the video. Nothing is
    handed off to the main form and nothing is "loaded into" anything: the whole thing happens
    in this window.

    Ryan: "the video part of the character sheet doesnt make sense ... seems like i need to load
    it into the form which i dont like. this is supposed to be easy for me to create the weird
    characters in one click. and then easily make a video from that."

    Deliberately NOT the Character window. That one is a precision editor and stays as it is;
    this one has exactly one way through it.
    """
    import tkinter as tk
    from tkinter import messagebox

    C, F, mono = ctx["C"], ctx["fonts"], ctx["mono"]
    f, fh, fsm = F["f"], F["fh"], F["fsm"]
    fmono = mono["m"]
    submit, run_state, collect = ctx["submit"], ctx["run_state"], ctx["collect"]
    btn_colors = ctx.get("btn_colors")

    DRAFTS = os.path.expanduser("~/forge-lab/drafts")
    try:
        os.makedirs(DRAFTS, exist_ok=True)
    except Exception:
        pass

    win = tk.Toplevel(root)
    win.title("Weirdo")
    win.configure(bg=C["bg"])
    win.geometry("980x900")

    st = {"picks": {}, "action": "", "still": None, "video": None, "busy": None}
    play = {"frames": [], "i": 0, "after": None}

    def lam(parent, text, **kw):
        kw.setdefault("bg", C["panel"])
        kw.setdefault("fg", C["muted"])
        return tk.Label(parent, text=text, font=kw.pop("font", fsm), **kw)

    def btn(parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, bg=(C["rust_deep"] if primary else C["panel2"]),
                         fg="#fff" if primary else C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=10, pady=3, borderwidth=0,
                         highlightthickness=1, highlightbackground=C["line"], command=cmd)

    # ---- header -----------------------------------------------------------------------
    hd = tk.Frame(win, bg=C["bg"])
    hd.pack(fill="x", padx=14, pady=(12, 2))
    tk.Label(hd, text="WEIRDO", bg=C["bg"], fg=C["ink"], font=fh).pack(side="left")
    status = tk.Label(hd, text="press ROLL", bg=C["bg"], fg=C["faint"], font=fsm)
    status.pack(side="left", padx=12)
    btn(hd, "CLOSE", win.destroy).pack(side="right")
    lam(win, "Roll one, look at it, make the video.  Nothing to load anywhere.",
        fg=C["faint"]).pack(anchor="w", padx=14, pady=(0, 8))

    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=14, pady=(0, 14))

    # left: the pictures.  right: the controls.  No scrolling column to lose things in.
    lf = tk.Frame(body, bg=C["panel"])
    lf.pack(side="left", fill="both", expand=True)
    rf = tk.Frame(body, bg=C["panel"])
    rf.pack(side="left", fill="both", padx=(10, 0))

    def _say(msg, warn=False):
        status.configure(text=msg, fg=(C["ochre"] if warn else C["faint"]))

    # ===== LEFT · the still and the video ==============================================
    lam(lf, "THE STILL", font=fmono).pack(anchor="w", padx=10, pady=(10, 4))
    prev = tk.Frame(lf, bg="#100e0c", height=330)
    prev.pack(fill="x", padx=10)
    prev.pack_propagate(False)
    prev_lbl = tk.Label(prev, bg="#100e0c", fg=C["faint"],
                        text="no still yet \u2014 press ROLL, then RENDER STILL")
    prev_lbl.pack(expand=True)

    lam(lf, "THE VIDEO", font=fmono).pack(anchor="w", padx=10, pady=(12, 4))
    vprev = tk.Frame(lf, bg="#100e0c", height=330)
    vprev.pack(fill="x", padx=10, pady=(0, 10))
    vprev.pack_propagate(False)
    vprev_lbl = tk.Label(vprev, bg="#100e0c", fg=C["faint"],
                         text="no video yet \u2014 render a still first")
    vprev_lbl.pack(expand=True)

    # ===== RIGHT · controls ===========================================================
    # 1 . roll
    tk.Label(rf, text=" 1 ", bg=C["rust"], fg="#14110f", font=fh).pack(anchor="w", pady=(10, 0))
    vein_row = tk.Frame(rf, bg=C["panel"])
    vein_row.pack(anchor="w", pady=(4, 0))
    lam(vein_row, "VEIN").pack(side="left")
    vein_var = tk.StringVar(value=WEIRDO_VEIN_DEFAULT)
    vein_menu = tk.OptionMenu(vein_row, vein_var, *WEIRDO_VEINS)
    vein_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust"], activeforeground="#fff",
                        borderwidth=0, width=16, anchor="w")
    vein_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    vein_menu.pack(side="left", padx=6)
    # ⚠️ Optional. EMPTY = the classic random roll, byte-for-byte. With words in it, ROLL asks
    # the rig's local model (Ollama on forge, GPU1, ~8-25 s, free) to fit the roll to them.
    lam(rf, "DESCRIBE IT  (optional \u2014 leave empty for a random weirdo)").pack(
        anchor="w", pady=(8, 0))
    t_desc = tk.Text(rf, height=3, width=54, wrap="word", bg="#100e0c", fg=C["ink"],
                     relief="flat", font=f, padx=6, pady=4, insertbackground=C["ink"])
    t_desc.pack(anchor="w", pady=(2, 0))
    roll_btn = btn(rf, "ROLL A WEIRDO", lambda: _roll(), primary=True)
    roll_btn.pack(anchor="w", pady=(6, 0))
    lam(rf, "rolls a normal person with ONE thing wrong with them \u2014 what you describe "
        "above is used, the rest is random", fg=C["faint"],
        wraplength=430, justify="left").pack(anchor="w", pady=(4, 0))

    # 2 . the block
    tk.Label(rf, text=" 2 ", bg=C["rust"], fg="#14110f", font=fh).pack(anchor="w", pady=(14, 0))
    lam(rf, "THE BLOCK  (yours to edit once you type in it)").pack(anchor="w", pady=(4, 0))
    t_prompt = tk.Text(rf, height=7, width=54, wrap="word", bg="#100e0c", fg=C["ink"],
                       relief="flat", font=f, padx=6, pady=4, insertbackground=C["ink"])
    t_prompt.pack(anchor="w", pady=(4, 0))

    # 3 . the still
    tk.Label(rf, text=" 3 ", bg=C["rust"], fg="#14110f", font=fh).pack(anchor="w", pady=(14, 0))
    srow = tk.Frame(rf, bg=C["panel"])
    srow.pack(anchor="w", pady=(4, 0))
    btn(srow, "RENDER STILL", lambda: _render_still(), primary=True).pack(side="left")
    btn(srow, "OPEN", lambda: _open(st.get("still"))).pack(side="left", padx=6)
    size_row = tk.Frame(rf, bg=C["panel"])
    size_row.pack(anchor="w", pady=(6, 0))
    lam(size_row, "SIZE").pack(side="left")
    size_var = tk.StringVar(value="Fast")
    size_menu = tk.OptionMenu(size_row, size_var, *[k for k, _ in CHAR_STILL_SCALE],
                              command=lambda *_: _sync_size())
    size_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust"], activeforeground="#fff",
                        borderwidth=0, width=6, anchor="w")
    size_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    size_menu.pack(side="left", padx=6)
    size_lbl = lam(size_row, "", fg=C["faint"], font=fmono)
    size_lbl.pack(side="left", padx=6)

    # 4 . the video
    tk.Label(rf, text=" 4 ", bg=C["rust"], fg="#14110f", font=fh).pack(anchor="w", pady=(14, 0))
    lam(rf, "ACTION  (what they do)").pack(anchor="w", pady=(4, 0))
    e_action = tk.Text(rf, height=2, width=54, wrap="word", bg="#100e0c", fg=C["ink"],
                       relief="flat", font=f, padx=6, pady=4, insertbackground=C["ink"])
    e_action.pack(anchor="w", pady=(4, 0))
    vrow = tk.Frame(rf, bg=C["panel"])
    vrow.pack(anchor="w", pady=(6, 0))
    lam(vrow, "LENGTH").pack(side="left")
    len_var = tk.StringVar(value=WEIRDO_VIDEO_LENGTH_DEFAULT)
    len_menu = tk.OptionMenu(vrow, len_var, *[k for k, _ in WEIRDO_VIDEO_LENGTHS])
    len_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                       highlightthickness=0, activebackground=C["rust"], activeforeground="#fff",
                       borderwidth=0, width=5, anchor="w")
    len_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    len_menu.pack(side="left", padx=(6, 14))
    lam(vrow, "CANVAS").pack(side="left")
    canvas_var = tk.StringVar(value=WEIRDO_VIDEO_CANVAS_DEFAULT)
    canvas_menu = tk.OptionMenu(vrow, canvas_var, *[k for k, _ in WEIRDO_VIDEO_CANVAS])
    canvas_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                          highlightthickness=0, activebackground=C["rust"],
                          activeforeground="#fff", borderwidth=0, width=16, anchor="w")
    canvas_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    canvas_menu.pack(side="left", padx=6)

    vrow2 = tk.Frame(rf, bg=C["panel"])
    vrow2.pack(anchor="w", pady=(8, 0))
    btn(vrow2, "MAKE THE VIDEO", lambda: _render_video(), primary=True).pack(side="left")
    btn(vrow2, "OPEN", lambda: _open(st.get("video"))).pack(side="left", padx=6)
    play_btn = btn(vrow2, "PLAY", lambda: _toggle_play())
    play_btn.pack(side="left")
    vinfo = lam(rf, "", fg=C["faint"], font=fmono)
    vinfo.pack(anchor="w", pady=(6, 0))

    # ===== behaviour ==================================================================
    def _block(for_video):
        return compose_character_prompt(st["picks"], for_video=for_video,
                                        action=st["action"])

    def _sync_size():
        try:
            w, h = char_still_size(st["picks"].get("Framing"), size_var.get())
            size_lbl.configure(text="%d\u00d7%d \u00b7 %.1f MP" % (w, h, w * h / 1e6))
        except Exception:
            size_lbl.configure(text="")

    def _roll():
        """Empty description -> the classic instant random roll. Words in the box -> roll a
        random base, then ask the rig's model to fit it to the words on a WORKER THREAD (a 27B
        takes ~8-25 s; a synchronous call would freeze the window)."""
        if st.get("fitting"):
            _say("still fitting your description \u2014 give it a moment", warn=True)
            return
        picks, action = roll_weirdo(vein=vein_var.get())
        desc = t_desc.get("1.0", "end").strip()
        if not desc:
            _show_roll(picks, action, "rolled a weirdo")
            return
        job = {"done": False, "fit": None, "err": None, "t0": time.time()}

        def work():
            try:
                import director as _d
                job["fit"] = _d.Director().weirdo_fit(desc, vein=vein_var.get(),
                                                      base=picks, action=action)
            except Exception as exc:
                job["err"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                job["done"] = True

        st["fitting"] = True
        roll_btn.configure(state="disabled")
        _say("fitting your description on the rig\u2026")
        threading.Thread(target=work, daemon=True).start()
        win.after(500, lambda: _poll_fit(job, picks, action))

    def _poll_fit(job, picks, action):
        if not job["done"]:
            _say("fitting your description\u2026 %.0f s" % (time.time() - job["t0"]))
            win.after(700, lambda: _poll_fit(job, picks, action))
            return
        st["fitting"] = False
        roll_btn.configure(state="normal")
        if job["err"]:
            # ⚠️ NEVER fall back to a random roll silently — that would look like the
            # description was used. Say it failed and change nothing.
            _log_err("weirdo fit failed: %s" % job["err"])
            _say("the rig's model could not fit it (%s) \u2014 nothing changed; press ROLL "
                 "again, or empty the box for a random roll" % job["err"][:90], warn=True)
            return
        p2, a2 = apply_weirdo_fit(picks, action, job["fit"])
        _show_roll(p2, a2, "rolled to your description \u2014 %.0f s"
                   % (time.time() - job["t0"]))

    def _show_roll(picks, action, msg):
        st["picks"] = picks
        st["action"] = action
        t_prompt.delete("1.0", "end")
        t_prompt.insert("1.0", _block(False))
        e_action.delete("1.0", "end")
        e_action.insert("1.0", action)
        _sync_size()
        st["still"] = None
        prev_lbl.configure(image="", text="rolled \u2014 now RENDER STILL")
        prev_lbl.image = None
        _say(msg)

    def _picks_now():
        """The block box is editable, so what you SEE is what gets rendered."""
        txt = t_prompt.get("1.0", "end").strip()
        return txt or _block(False)

    def _run(kind, params, refs):
        """One submit -> poll -> collect. Returns (pid, meta) or (pid, None) on failure."""
        pid, _seed = submit(kind, params, refs)
        t0 = time.time()
        while time.time() - t0 < 1800:
            time.sleep(3)
            try:
                s = run_state(pid)
            except Exception:
                continue
            if s.get("state") in ("done", "error", "gone"):
                break
        return pid, collect(pid)

    def _first_file(meta):
        """collect() saves everything under RUNS/<pid>. The ComfyUI SOURCE echo sorts LAST, so
        the render is the first entry."""
        if not meta or meta.get("error"):
            return None
        for f_ in (meta.get("images") or []):
            p = os.path.join(RUNS, meta["id"], f_["file"])
            if os.path.exists(p):
                return p
        return None

    def _render_still():
        if st["busy"]:
            _say("already rendering\u2026", warn=True)
            return
        if not st["picks"]:
            _say("press ROLL first", warn=True)
            return
        st["busy"] = "still"
        _say("rendering the still on forge\u2026")

        def _work():
            try:
                w, h = char_still_size(st["picks"].get("Framing"), size_var.get())
                pid, meta = _run("t2i", {"prompt": _picks_now(), "width": w, "height": h,
                                         "resolution": max(w, h), "steps": 25, "seed": -1,
                                         "prefix": "forge_lab_weirdo_still"}, None)
                path = _first_file(meta)
                st["still"] = path
                win.after(0, lambda: _still_done(path, meta))
            except Exception as exc:
                _log_err("weirdo still failed: %r" % exc)
                # ⚠️ bind exc as a DEFAULT ARG. `except ... as exc` DELETES exc at the end of the
                # block, so a lambda that closes over it raises NameError when it finally runs on
                # the main loop - the failure report itself crashed, hiding the real error.
                win.after(0, lambda _e=exc: _say("still failed: %s" % _e, warn=True))
            finally:
                st["busy"] = None

        threading.Thread(target=_work, daemon=True).start()

    def _still_done(path, meta):
        if not path:
            _say("still failed: %s" % ((meta or {}).get("error") or "no output"), warn=True)
            return
        # ⚠️ thumb_photo is the ONLY safe way to turn a file into a Tk image here (it handles
        # the transparent-PNG case, which used to take the app down). Never Tk a raw file path.
        img = thumb_photo(path, 330)
        if img is None:
            _say("could not show the still", warn=True)
        else:
            prev_lbl.configure(image=img, text="")
            prev_lbl.image = img
        _say("still ready \u2014 now MAKE THE VIDEO")

    def _render_video():
        if st["busy"]:
            _say("already rendering\u2026", warn=True)
            return
        if not st["still"]:
            _say("RENDER STILL first \u2014 the video needs it as its reference", warn=True)
            return
        st["busy"] = "video"
        _say("making the video on forge\u2026 this takes a minute or two")

        def _work():
            try:
                st["action"] = e_action.get("1.0", "end").strip()
                prompt = compose_character_prompt(st["picks"], for_video=True,
                                                  action=st["action"])
                params = weirdo_video_params(prompt, canvas_var.get(), len_var.get())
                pid, meta = _run("video", params, [st["still"]])
                path = _first_file(meta)
                st["video"] = path
                win.after(0, lambda: _video_done(path, meta, params))
            except Exception as exc:
                _log_err("weirdo video failed: %r" % exc)
                win.after(0, lambda _e=exc: _say("video failed: %s" % _e, warn=True))
            finally:
                st["busy"] = None

        threading.Thread(target=_work, daemon=True).start()

    def _video_done(path, meta, params):
        if not path:
            _say("video failed: %s" % ((meta or {}).get("error") or "no output"), warn=True)
            return
        vinfo.configure(text="%dx%d \u00b7 %d frames \u00b7 %d steps"
                             % (params["width"], params["height"], params["length"],
                                params["steps"]))
        _load_frames(path)
        _say("video ready")

    # ---- playback --------------------------------------------------------------------
    # Tk cannot play video, so the player IS a frame cycler: ffmpeg pulls every 2nd frame at a
    # small size, and we show them in order. Honest about what it is - and it needs no new
    # dependency, because ffmpeg is already what this app renders with.
    def _load_frames(path):
        _stop_play()
        play["frames"] = []
        play["i"] = 0
        d = os.path.join(tempfile.gettempdir(), "forge_lab_weirdo_frames")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        try:
            r = weirdo_player_frames(path, d)
            if r.returncode != 0:
                _say("could not decode frames: %s" % (r.stderr or "")[:80], warn=True)
                return
            names = sorted(x for x in os.listdir(d) if x.endswith(".png"))
            for x in names:
                fr = thumb_photo(os.path.join(d, x), 300)   # already <=300, so no resample
                if fr is not None:
                    play["frames"].append(fr)
        except Exception as exc:
            _say("could not load frames: %s" % exc, warn=True)
            return
        if not play["frames"]:
            _say("no frames decoded", warn=True)
            return
        vprev_lbl.configure(image=play["frames"][0], text="")
        vprev_lbl.image = play["frames"][0]
        play_btn.configure(text="PLAY")

    def _tick():
        if not play["frames"]:
            return
        play["i"] = (play["i"] + 1) % len(play["frames"])
        fr = play["frames"][play["i"]]
        vprev_lbl.configure(image=fr, text="")
        vprev_lbl.image = fr
        play["after"] = win.after(1000 // 12, _tick)

    def _toggle_play():
        if not play["frames"]:
            _say("make the video first", warn=True)
            return
        if play["after"] is not None:
            _stop_play()
            return
        play["after"] = win.after(0, _tick)
        play_btn.configure(text="PAUSE")

    def _stop_play():
        if play["after"] is not None:
            try:
                win.after_cancel(play["after"])
            except Exception:
                pass
        play["after"] = None
        try:
            play_btn.configure(text="PLAY")
        except Exception:
            pass

    def _open(path):
        if not path or not os.path.exists(path):
            _say("nothing to open yet", warn=True)
            return
        try:
            subprocess.run(["open", path], timeout=30)
        except Exception as exc:
            _say("could not open: %s" % exc, warn=True)

    win.bind("<Escape>", lambda _e: win.destroy())
    win.protocol("WM_DELETE_WINDOW", lambda: (_stop_play(), win.destroy()))

    # ---- driver exports --------------------------------------------------------------
    win.weirdo_roll = _roll
    win.weirdo_still = _render_still
    win.weirdo_video = _render_video
    win.weirdo_block = lambda: t_prompt.get("1.0", "end").strip()
    win.weirdo_state = st
    win.weirdo_vein_var = vein_var
    win.weirdo_size_var = size_var
    win.weirdo_canvas_var = canvas_var
    win.weirdo_len_var = len_var
    win.weirdo_desc = t_desc               # DESCRIBE IT box (empty = classic random roll)
    win.weirdo_widgets = {"prompt": t_prompt, "action": e_action, "status": status,
                          "size_lbl": size_lbl, "vinfo": vinfo, "play_btn": play_btn}
    win.weirdo_play = _toggle_play
    win.weirdo_frames = play
    win.weirdo_first_file = _first_file

    _sync_size()
    # ⚠️ _btn_colors is a LOCAL of launch(); an on-demand window must receive it through ctx or
    # its buttons never get repainted light-with-dark-text. Documented trap.
    if btn_colors:
        btn_colors(win)
    return win


# =============================================================== STUDIO
# The STUDIO window — a NEW sixth TYPE entry (do NOT restructure the windows Ryan
# already uses). It drives studio_pipeline.py through one numbered, button-driven flow:
#
#   1 CAST -> 2 IDEA -> 3 SCRIPT -> 4 SHOTS -> 5 RENDER -> 6 CUT
#
# ⚠️ Every pipeline call runs in a WORKER THREAD. The Tk main thread must never block on
# the Director LLM, a render, TTS or ffmpeg — the UI would freeze with no repaint. Results
# come back through win.after(0, ...); buttons disable while a stage runs; a failure shows
# in a visible RED status line. `except Exception as e: msg=str(e); win.after(0, lambda
# m=msg: _say(m))` — ⚠️ bind the message as a DEFAULT ARG: `except ... as e` DELETES `e`
# at the end of the block, so a lambda closing over it raises NameError when it finally
# runs, and the failure report crashes and HIDES the real error (the WEIRDO lesson).

# Plain-language names for a shot's intent. The number IS the instruction; a flat column of
# engine jargon is not (Ryan: "the flow and ui should be easier for me to follow").
STUDIO_INTENT_PLAIN = {
    "character_action": "character does a thing",
    "continue": "continue the shot",
    "lipsync": "says a line",
    "motion_copy": "copy a clip's motion",
    "establishing": "establishing",
}
STUDIO_LENGTHS = ("12", "20", "30", "45", "60")
STUDIO_QUALITIES = ("Draft", "Final")


def studio_plain_intent(intent):
    """The plain-language name for a shot intent (falls back to the raw intent)."""
    return STUDIO_INTENT_PLAIN.get(intent, intent or "")


def _studio_fnum(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def studio_script_to_text(sc):
    """Render a Director script dict to the editable line format.

    The format is a STRICT round-trip for the fields the pipeline uses:
        TITLE: <title>
        HOOK: <hook>
        BEAT: <t_start> <t_end> <speaker> | <action> | <line>
        ...
        BUTTON: <button>

    ⚠️ The text<->script round-trip is defined HERE, once, so an edit in the box persists
    into project.json and is what plan_shots() reads — never a parallel copy.
    """
    if not isinstance(sc, dict):
        return ""
    lines = ["TITLE: %s" % (sc.get("title") or ""),
             "HOOK: %s" % (sc.get("hook") or "")]
    for b in (sc.get("beats") or []):
        if not isinstance(b, dict):
            continue
        lines.append("BEAT: %s %s | %s | %s | %s" % (
            b.get("t_start", 0), b.get("t_end", 0),
            (b.get("speaker") or "-"), (b.get("action") or "-"), (b.get("line") or "")))
    lines.append("BUTTON: %s" % (sc.get("button") or ""))
    return "\n".join(lines)


def studio_text_to_script(text, prev=None):
    """Parse the editable text back into a script dict (tolerant, never raises).

    A line that matches no keyword is folded into the previous beat's action, so a
    Director REWRITE that returns prose still yields a usable beat list rather than an
    empty script — the edits must survive whatever shape the model returns.
    """
    sc = dict(prev) if isinstance(prev, dict) else {}
    sc.setdefault("title", "")
    sc.setdefault("hook", "")
    sc.setdefault("button", "")
    beats = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        up = line.upper()
        if up.startswith("TITLE:"):
            sc["title"] = line[6:].strip()
        elif up.startswith("HOOK:"):
            sc["hook"] = line[5:].strip()
        elif up.startswith("BUTTON:"):
            sc["button"] = line[7:].strip()
        elif up.startswith("BEAT:"):
            parts = [p.strip() for p in line[5:].strip().split("|")]
            head = (parts[0] if parts else "").split()
            t0 = _studio_fnum(head[0] if len(head) > 0 else 0)
            t1 = _studio_fnum(head[1] if len(head) > 1 else t0, t0)
            if len(parts) >= 4:                       # canonical: t0 t1 | spk | action | line
                speaker, action, spoken = parts[1], parts[2], parts[3]
            else:                                     # tolerant: t0 t1 spk | action | line
                speaker = head[2] if len(head) > 2 else ""
                action = parts[1] if len(parts) > 1 else ""
                spoken = parts[2] if len(parts) > 2 else ""
            if speaker == "-":
                speaker = ""
            if action == "-":
                action = ""
            beats.append({"t_start": t0, "t_end": t1, "speaker": speaker,
                          "action": action, "line": spoken})
        else:
            if beats:
                beats[-1]["action"] = (beats[-1]["action"] + " " + line).strip()
            else:
                beats.append({"t_start": 0, "t_end": 0, "speaker": "",
                              "action": line, "line": ""})
    sc["beats"] = beats
    return sc


def open_studio(root, ctx):
    """STUDIO — one window, six numbered steps, one way through.

    CAST -> IDEA -> SCRIPT -> SHOTS -> RENDER -> CUT, with a big preview/player and a log
    on the right. Opening an existing project restores everything from project.json.

    ⚠️ ONE window only (same as Weirdo/Character): picking the TYPE entry again RAISES the
    open window instead of stacking a second copy.
    """
    import tkinter as tk
    from tkinter import messagebox

    C, F, mono = ctx["C"], ctx["fonts"], ctx["mono"]
    f, fh, fsm = F["f"], F["fh"], F["fsm"]
    fmono = mono["m"]
    btn_colors = ctx.get("btn_colors")

    # ⚠️ Imported HERE (inside the function), not at module top: studio_pipeline/charpack
    # pull in director + the audio client, and a construction-time import failure would take
    # the whole app down. Also, the driver monkeypatches THESE module objects, so every call
    # must go through sp.* / cp.* — never a copied function reference.
    import studio_pipeline as sp
    import charpack as cp

    win = tk.Toplevel(root)
    win.title("Studio")
    win.configure(bg=C["bg"])
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1220, sw - 40), min(880, sh - 120)))

    RED = "#e0553b"        # the visible error colour for the status line

    st = {"pj": None, "chars": [], "busy": None, "done": {}, "estimate": None,
          "sheet_est": None, "render_confirm": "", "sheet_confirm": "",
          "frames": {"list": [], "i": 0, "after": None}, "shot_vars": {},
          "progress": ""}

    def lam(parent, text, **kw):
        kw.setdefault("bg", C["panel"])
        kw.setdefault("fg", C["muted"])
        return tk.Label(parent, text=text, font=kw.pop("font", fsm), **kw)

    def btn(parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, bg=(C["rust_deep"] if primary else C["panel2"]),
                         fg="#fff" if primary else C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=9, pady=3, borderwidth=0,
                         highlightthickness=1, highlightbackground=C["line"], command=cmd)

    # ---- header: title, project picker, status ---------------------------------------
    hd = tk.Frame(win, bg=C["bg"])
    hd.pack(fill="x", padx=12, pady=(10, 2))
    tk.Label(hd, text="STUDIO", bg=C["bg"], fg=C["ink"], font=fh).pack(side="left")
    lam(hd, "PROJECT", fg=C["muted"]).pack(side="left", padx=(14, 4))
    proj_box = tk.Frame(hd, bg=C["bg"])
    proj_box.pack(side="left")
    proj_var = tk.StringVar(value="— new —")
    btn(hd, "OPEN", lambda: _open_clicked()).pack(side="left", padx=(6, 0))
    btn(hd, "NEW", lambda: _new_form()).pack(side="left", padx=4)
    btn(hd, "CLOSE", win.destroy).pack(side="right")

    status = tk.Label(win, text="", bg=C["bg"], fg=C["faint"], font=fsm, anchor="w")
    status.pack(fill="x", padx=12, pady=(0, 6))

    def _say(msg, warn=False):
        try:
            status.configure(text=str(msg), fg=(RED if warn else C["faint"]))
        except Exception:
            pass

    # ---- body: steps (scrolling) left, preview + log right ---------------------------
    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    leftp = tk.Frame(body, bg=C["bg"])
    leftp.pack(side="left", fill="both", expand=True)
    vsb = tk.Scrollbar(leftp, orient="vertical")
    vsb.pack(side="right", fill="y")
    canvas = tk.Canvas(leftp, bg=C["bg"], highlightthickness=0, yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    vsb.configure(command=canvas.yview)
    steps = tk.Frame(canvas, bg=C["bg"])
    cwin = canvas.create_window((0, 0), window=steps, anchor="nw")
    steps.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(cwin, width=e.width))

    scoped_wheel(win, canvas)        # ⚠️ never bind_all/unbind_all the wheel — see scoped_wheel()

    rp = tk.Frame(body, bg=C["panel"], width=380)
    rp.pack(side="left", fill="y", padx=(10, 0))
    rp.pack_propagate(False)
    lam(rp, "PREVIEW", font=fmono).pack(anchor="w", padx=10, pady=(10, 4))
    prev = tk.Frame(rp, bg="#100e0c", height=400, width=360)
    prev.pack(fill="x", padx=10)
    prev.pack_propagate(False)
    prev_lbl = tk.Label(prev, bg="#100e0c", fg=C["faint"], font=fsm,
                        text="nothing yet — pick a character in 1 · CAST")
    prev_lbl.pack(expand=True)

    transport = tk.Frame(rp, bg=C["panel"])
    transport.pack(fill="x", padx=10, pady=(6, 0))
    play_btn = btn(transport, "PLAY", lambda: _toggle_play())
    play_btn.pack(side="left")
    btn(transport, "OPEN", lambda: _open_path(_preview_path())).pack(side="left", padx=4)
    btn(transport, "REVEAL", lambda: _reveal_path(_preview_path())).pack(side="left")
    pnote = lam(rp, "", fg=C["faint"], font=fmono)
    pnote.pack(anchor="w", padx=10, pady=(4, 0))

    lam(rp, "LOG", font=fmono).pack(anchor="w", padx=10, pady=(10, 4))
    t_log = tk.Text(rp, height=10, width=44, wrap="word", bg="#100e0c", fg=C["muted"],
                    relief="flat", font=fmono, padx=6, pady=4)
    t_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _log_line(msg):
        try:
            t_log.insert("end", "%s\n" % msg)
            t_log.see("end")
        except Exception:
            pass

    # ---- step scaffolding ------------------------------------------------------------
    marks = {}
    bodies = {}

    def _step(n, title):
        outer = tk.Frame(steps, bg=C["panel"], highlightthickness=1,
                         highlightbackground=C["line"])
        outer.pack(fill="x", pady=(0, 8))
        head = tk.Frame(outer, bg=C["panel"])
        head.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(head, text=" %d " % n, bg=C["rust"], fg="#14110f", font=fh).pack(side="left")
        tk.Label(head, text=title, bg=C["panel"], fg=C["ink"], font=fsm).pack(side="left", padx=8)
        mark = tk.Label(head, text="", bg=C["panel"], fg=C["moss"], font=fh)
        mark.pack(side="right")
        marks[n] = mark
        b = tk.Frame(outer, bg=C["panel"])
        b.pack(fill="x", padx=10, pady=(2, 10))
        bodies[n] = b
        return b

    # ===== 1 · CAST ===================================================================
    b1 = _step(1, "CAST  — pick the character (1 or 2)")
    cast_box = tk.Frame(b1, bg=C["panel"])
    cast_box.pack(fill="x")
    pack_lbl = lam(b1, "", fg=C["faint"], font=fmono, justify="left")
    pack_lbl.pack(anchor="w", pady=(6, 0))
    cast_ctl = tk.Frame(b1, bg=C["panel"])
    cast_ctl.pack(anchor="w", pady=(6, 0))
    btn(cast_ctl, "BUILD SHEET", lambda: _build_sheet(), primary=True).pack(side="left")
    btn(cast_ctl, "MAKE VOICE", lambda: _make_voice()).pack(side="left", padx=6)
    btn(cast_ctl, "WRITE BIBLE", lambda: _write_bible()).pack(side="left")
    lam(b1, "A sheet gives the shots more angles to keep the character consistent.",
        fg=C["faint"], wraplength=560, justify="left").pack(anchor="w", pady=(4, 0))

    # ===== 2 · IDEA ===================================================================
    b2 = _step(2, "IDEA  — one line, then PITCH or USE MY IDEA")
    e_idea = tk.Entry(b2, width=64, bg="#100e0c", fg=C["ink"], relief="flat", font=f,
                      insertbackground=C["ink"])
    e_idea.pack(anchor="w", pady=(0, 6))
    irow = tk.Frame(b2, bg=C["panel"])
    irow.pack(anchor="w")
    lam(irow, "LENGTH").pack(side="left")
    len_var = tk.StringVar(value="20")
    len_menu = tk.OptionMenu(irow, len_var, *STUDIO_LENGTHS)
    len_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                       highlightthickness=0, activebackground=C["rust"],
                       activeforeground="#fff", borderwidth=0, width=4, anchor="w")
    len_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    len_menu.pack(side="left", padx=(4, 12))
    lam(irow, "s").pack(side="left")
    lam(irow, "QUALITY").pack(side="left", padx=(14, 0))
    qual_var = tk.StringVar(value="Draft")
    qual_menu = tk.OptionMenu(irow, qual_var, *STUDIO_QUALITIES)
    qual_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust"],
                        activeforeground="#fff", borderwidth=0, width=7, anchor="w")
    qual_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    qual_menu.pack(side="left", padx=4)
    idea_ctl = tk.Frame(b2, bg=C["panel"])
    idea_ctl.pack(anchor="w", pady=(8, 0))
    btn(idea_ctl, "PITCH ME 5", lambda: _pitch(), primary=True).pack(side="left")
    btn(idea_ctl, "USE MY IDEA", lambda: _use_idea()).pack(side="left", padx=6)
    pitch_box = tk.Frame(b2, bg=C["panel"])
    pitch_box.pack(fill="x", pady=(8, 0))

    # ===== 3 · SCRIPT =================================================================
    b3 = _step(3, "SCRIPT  — write it, edit it, rewrite a line")
    s_ctl = tk.Frame(b3, bg=C["panel"])
    s_ctl.pack(anchor="w", pady=(0, 6))
    btn(s_ctl, "WRITE SCRIPT", lambda: _write_script(), primary=True).pack(side="left")
    btn(s_ctl, "SAVE EDITS", lambda: _save_edits()).pack(side="left", padx=6)
    t_script = tk.Text(b3, height=11, width=66, wrap="word", bg="#100e0c", fg=C["ink"],
                       relief="flat", font=fmono, padx=6, pady=4, insertbackground=C["ink"])
    t_script.pack(anchor="w", pady=(0, 6))
    rw = tk.Frame(b3, bg=C["panel"])
    rw.pack(anchor="w")
    lam(rw, "REWRITE").pack(side="left")
    btn(rw, "funnier", lambda: _rewrite("funnier")).pack(side="left", padx=4)
    btn(rw, "shorter", lambda: _rewrite("shorter")).pack(side="left", padx=4)
    btn(rw, "more deadpan", lambda: _rewrite("more deadpan")).pack(side="left", padx=4)

    # ===== 4 · SHOTS ==================================================================
    b4 = _step(4, "SHOTS  — plan them, then voice the lines")
    sh_ctl = tk.Frame(b4, bg=C["panel"])
    sh_ctl.pack(anchor="w", pady=(0, 6))
    btn(sh_ctl, "PLAN SHOTS", lambda: _plan_shots(), primary=True).pack(side="left")
    btn(sh_ctl, "VOICE LINES", lambda: _voice()).pack(side="left", padx=6)
    btn(sh_ctl, "SAVE SHOT EDITS", lambda: _save_shots()).pack(side="left")
    shots_box = tk.Frame(b4, bg=C["panel"])
    shots_box.pack(fill="x", pady=(6, 0))

    # ===== 5 · RENDER =================================================================
    b5 = _step(5, "RENDER  — price it, then spend")
    est_lbl = lam(b5, "no estimate yet — PLAN SHOTS, then ESTIMATE", fg=C["faint"], font=fmono,
                  justify="left")
    est_lbl.pack(anchor="w")
    r_ctl = tk.Frame(b5, bg=C["panel"])
    r_ctl.pack(anchor="w", pady=(6, 0))
    btn(r_ctl, "ESTIMATE", lambda: _estimate_click(), primary=True).pack(side="left")
    btn(r_ctl, "RENDER ALL", lambda: _render_all()).pack(side="left", padx=6)
    prog_lbl = lam(b5, "", fg=C["faint"], font=fmono)
    prog_lbl.pack(anchor="w", pady=(4, 0))
    render_box = tk.Frame(b5, bg=C["panel"])
    render_box.pack(fill="x", pady=(6, 0))

    # ===== 6 · CUT ====================================================================
    b6 = _step(6, "CUT  — assemble, play, post")
    c_ctl = tk.Frame(b6, bg=C["panel"])
    c_ctl.pack(anchor="w", pady=(0, 6))
    btn(c_ctl, "ASSEMBLE", lambda: _assemble(), primary=True).pack(side="left")
    btn(c_ctl, "OPEN", lambda: _open_path(_cut_path())).pack(side="left", padx=6)
    btn(c_ctl, "REVEAL IN FINDER", lambda: _reveal_path(_cut_path())).pack(side="left")
    lam(b6, "post.txt", fg=C["muted"]).pack(anchor="w", pady=(6, 2))
    t_post = tk.Text(b6, height=6, width=66, wrap="word", bg="#100e0c", fg=C["ink"],
                     relief="flat", font=fmono, padx=6, pady=4)
    t_post.pack(anchor="w")
    btn(b6, "COPY", lambda: _copy_post()).pack(anchor="w", pady=(4, 0))

    # ===== helpers ====================================================================
    def _all_btns():
        out = []

        def walk(x):
            for c in x.winfo_children():
                if c.winfo_class() in ("Button", "Menubutton"):
                    out.append(c)
                walk(c)
        walk(win)
        return out

    def _set_busy(on):
        st["busy"] = bool(on)
        for b in _all_btns():
            try:
                if str(b.cget("text")).upper() == "CLOSE":
                    continue
                b.configure(state=("disabled" if on else "normal"))
            except Exception:
                pass

    def _work(fn, done=None, label="working"):
        """Run `fn` in a WORKER THREAD; never block the Tk thread.

        ⚠️ busy is cleared BEFORE `done` runs (both on the main thread) so a `done` callback
        may kick off the next stage without tripping the 'already working' guard.
        ⚠️ The failure message is BOUND as a default arg — `except ... as exc` deletes exc.
        """
        if st["busy"]:
            _say("already working on %s — wait" % st["busy"], warn=True)
            return
        _set_busy(True)
        _say(label + "…")

        def run():
            err = None
            res = None
            try:
                res = fn()
            except Exception as exc:
                err = "%s failed: %s" % (label, exc)
                try:
                    _log_err("studio: %s" % err)
                except Exception:
                    pass

            def finish():
                _set_busy(False)
                if err:
                    _say(err, warn=True)
                    _log_line("!! " + err)
                    return
                if done:
                    try:
                        done(res)
                    except Exception as exc:
                        _say("post-step failed: %s" % exc, warn=True)
                else:
                    _say("done")

            win.after(0, finish)
        threading.Thread(target=run, daemon=True).start()

    def _mark_done():
        for n, m in marks.items():
            try:
                m.configure(text=("\u2713" if st["done"].get(n) else ""))
            except Exception:
                pass

    def _sync_done():
        pj = st["pj"] or {}
        shots = pj.get("shots") or []
        st["done"] = {
            1: bool(st["chars"]),
            2: bool(pj.get("pitch")),
            3: bool(pj.get("script")),
            4: bool(shots),
            5: bool(shots) and all(s.get("chosen") for s in shots),
            6: bool((pj.get("cut") or {}).get("mp4")),
        }
        _mark_done()

    # ---- preview / player ------------------------------------------------------------
    def _preview_path():
        pj = st["pj"] or {}
        cut = (pj.get("cut") or {}).get("mp4")
        if cut and os.path.exists(cut):
            return cut
        for s in (pj.get("shots") or []):
            cp_ = _take_clip(s.get("chosen"))
            if cp_:
                return cp_
        if st["chars"]:
            return character_still(st["chars"][0])
        return None

    def _show_image(path, box=360):
        img = thumb_photo(path, box) if path else None
        try:
            if img is None:
                prev_lbl.configure(image="", text="nothing to show yet")
                prev_lbl.image = None
            else:
                prev_lbl.configure(image=img, text="")
                prev_lbl.image = img
        except Exception:
            pass

    def _stop_play():
        if st["frames"]["after"] is not None:
            try:
                win.after_cancel(st["frames"]["after"])
            except Exception:
                pass
        st["frames"]["after"] = None
        try:
            play_btn.configure(text="PLAY")
        except Exception:
            pass

    def _tick():
        fr = st["frames"]["list"]
        if not fr:
            return
        st["frames"]["i"] = (st["frames"]["i"] + 1) % len(fr)
        img = fr[st["frames"]["i"]]
        try:
            prev_lbl.configure(image=img, text="")
            prev_lbl.image = img
        except Exception:
            pass
        st["frames"]["after"] = win.after(1000 // 12, _tick)

    def _toggle_play():
        if not st["frames"]["list"]:
            _say("nothing to play yet — ASSEMBLE first", warn=True)
            return
        if st["frames"]["after"] is not None:
            _stop_play()
            return
        st["frames"]["after"] = win.after(0, _tick)
        play_btn.configure(text="PAUSE")

    def _take_clip(pid):
        if not pid:
            return None
        d = os.path.join(RUNS, pid)
        try:
            files = sorted(f for f in os.listdir(d)
                           if f.lower().endswith((".mp4", ".webm", ".mkv")))
        except Exception:
            return None
        return os.path.join(d, files[0]) if files else None

    def _open_path(path):
        if not path or not os.path.exists(path):
            _say("nothing to open yet", warn=True)
            return
        try:
            subprocess.run(["open", path], timeout=30)
        except Exception as exc:
            _say("could not open: %s" % exc, warn=True)

    def _reveal_path(path):
        if not path or not os.path.exists(path):
            _say("nothing to reveal yet", warn=True)
            return
        try:
            subprocess.run(["open", "-R", path], timeout=30)
        except Exception as exc:
            _say("could not reveal: %s" % exc, warn=True)

    def _copy_post():
        txt = t_post.get("1.0", "end").strip()
        if not txt:
            _say("no post text yet", warn=True)
            return
        try:
            win.clipboard_clear()
            win.clipboard_append(txt)
            _say("post text copied")
        except Exception as exc:
            _say("could not copy: %s" % exc, warn=True)

    def _cut_path():
        return ((st["pj"] or {}).get("cut") or {}).get("mp4")

    # ---- project picker --------------------------------------------------------------
    def _list_projects():
        d = sp.studio_root()
        try:
            return sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))
        except Exception:
            return []

    def _rebuild_proj_menu():
        for c in proj_box.winfo_children():
            c.destroy()
        opts = ["— new —"] + _list_projects()
        if proj_var.get() not in opts:
            proj_var.set("— new —")
        m = tk.OptionMenu(proj_box, proj_var, *opts)
        m.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                    highlightthickness=0, activebackground=C["rust"],
                    activeforeground="#fff", borderwidth=0, width=22, anchor="w")
        m["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
        m.pack(side="left")

    def _open_clicked():
        slug = proj_var.get()
        if slug == "— new —":
            _new_form()
            return
        _open_project(slug)

    # ---- CAST ------------------------------------------------------------------------
    def _select_char(slug):
        cur = list(st["chars"])
        if slug in cur:
            cur.remove(slug)
        else:
            cur.append(slug)
            if len(cur) > 2:
                cur.pop(0)                 # at most TWO characters
        st["chars"] = cur
        if st["pj"] is not None:
            st["pj"]["characters"] = list(cur)
            try:
                sp.save_project(st["pj"])
            except Exception:
                pass
        _sync_done()
        _refresh_all()

    def _build_cast():
        for c in cast_box.winfo_children():
            c.destroy()
        chars = list_characters()
        if not chars:
            lam(cast_box, "no saved characters — make one in the CHARACTER window first",
                fg=C["faint"]).pack(anchor="w")
            return
        for slug, name in chars:
            sel = slug in st["chars"]
            bg = C["rust_deep"] if sel else C["panel2"]
            cell = tk.Frame(cast_box, bg=bg, padx=4, pady=4,
                            highlightthickness=1, highlightbackground=(C["rust"] if sel else C["line"]))
            cell.pack(side="left", padx=4, pady=4)
            still = character_still(slug)
            img = thumb_photo(still, 84) if still else None
            lb = tk.Label(cell, bg=bg, fg=C["faint"], text=("" if img else "no still"))
            if img is not None:
                lb.configure(image=img)
                lb.image = img
            lb.pack()
            nm = tk.Label(cell, text=name, bg=bg, fg=C["ink"], font=fsm)
            nm.pack()
            for wdg in (cell, lb, nm):
                wdg.bind("<Button-1>", lambda e, s=slug: _select_char(s))
        # pack status of the first selected character
        if st["chars"]:
            try:
                ps = cp.pack_status(st["chars"][0])
                pack_lbl.configure(text="pack: %s  ·  %d sheet views  ·  bible %s  ·  voice %s"
                                        % (ps.get("slug"), ps.get("sheet_views", 0),
                                           "yes" if ps.get("has_bible") else "no",
                                           "yes" if ps.get("has_voice") else "no"))
            except Exception as exc:
                pack_lbl.configure(text="pack: ? (%s)" % exc)
        else:
            pack_lbl.configure(text="")

    def _sheet_confirm_text():
        e = st.get("sheet_est") or {}
        return ("Build the reference sheet for %s?\n\n"
                "%d views, estimated ~%s s total.\n\n"
                "This submits real image edits to forge. Continue?"
                % (st["chars"][0] if st["chars"] else "?", e.get("count", 0),
                   e.get("total_seconds", "?")))

    def _build_sheet():
        if not st["chars"]:
            _say("pick a character in 1 · CAST first", warn=True)
            return
        slug = st["chars"][0]

        def after_est(e):
            st["sheet_est"] = e
            txt = _sheet_confirm_text()
            st["sheet_confirm"] = txt
            if not messagebox.askokcancel("Build sheet", txt, parent=win):
                _say("sheet cancelled")
                return
            _work(lambda: cp.build_sheet(slug, scale="Fast"),
                  done=lambda doc: (_say("sheet: %d views" % len(doc.get("views") or [])),
                                    _log_line("sheet -> %s" % slug), _refresh_all()),
                  label="building sheet")

        _work(lambda: cp.sheet_estimate(slug, scale="Fast"), done=after_est, label="pricing sheet")

    def _make_voice():
        if not st["chars"]:
            _say("pick a character in 1 · CAST first", warn=True)
            return
        slug = st["chars"][0]
        _work(lambda: cp.make_voice(slug),
              done=lambda doc: (_say("voice: %s" % (doc or {}).get("description", "")),
                                _log_line("voice -> %s" % slug), _refresh_all()),
              label="designing voice")

    def _write_bible():
        if not st["chars"]:
            _say("pick a character in 1 · CAST first", warn=True)
            return
        slug = st["chars"][0]
        _work(lambda: cp.bible(slug),
              done=lambda doc: (_say("bible written"), _log_line("bible -> %s" % slug), _refresh_all()),
              label="writing bible")

    # ---- IDEA ------------------------------------------------------------------------
    def _length_int():
        try:
            return int(len_var.get())
        except (TypeError, ValueError):
            return 20

    def _ensure_project():
        """Create the project on first use, or keep the existing one in sync with the form."""
        if st["pj"] is not None:
            st["pj"]["idea"] = e_idea.get().strip()
            st["pj"]["length_s"] = _length_int()
            st["pj"]["quality"] = qual_var.get()
            try:
                sp.save_project(st["pj"])
            except Exception:
                pass
            return st["pj"]
        if not st["chars"]:
            _say("pick a character in 1 · CAST first", warn=True)
            return None
        idea = e_idea.get().strip()
        if not idea:
            _say("type an idea in 2 · IDEA first", warn=True)
            return None
        pj = sp.new_project(list(st["chars"]), idea, length_s=_length_int(),
                            quality=qual_var.get())
        st["pj"] = pj
        _log_line("new project: %s" % pj.get("slug"))
        return pj

    def _pitch():
        pj = _ensure_project()
        if pj is None:
            return

        def run():
            res = sp.pitch(pj, n=5)
            if res:
                pj["pitches"] = list(res)
            return pj.get("pitches") or []

        _work(run, done=lambda ps: (_say("pitched %d premises" % len(ps)), _refresh_all()),
              label="pitching premises")

    def _use_idea():
        pj = _ensure_project()
        if pj is None:
            return
        idea = pj.get("idea") or ""
        pj["pitch"] = {"title": (idea[:48] or pj.get("slug")), "logline": idea, "hook": idea}
        try:
            sp.save_project(pj)
        except Exception:
            pass
        _sync_done()
        _refresh_all()
        _say("using your idea — now WRITE SCRIPT")

    def _choose_pitch(i):
        pj = st["pj"]
        if not pj:
            return
        try:
            ch = sp.choose_pitch(pj, i)
            if ch:
                pj["pitch"] = ch
        except Exception as exc:
            _say("could not choose pitch: %s" % exc, warn=True)
            return
        try:
            sp.save_project(pj)
        except Exception:
            pass
        _sync_done()
        _refresh_all()
        _say("chose pitch %d" % i)

    def _build_pitch_cards():
        for c in pitch_box.winfo_children():
            c.destroy()
        pj = st["pj"] or {}
        pitches = pj.get("pitches") or []
        chosen = (pj.get("pitch") or {})
        for i, p in enumerate(pitches):
            sel = bool(chosen) and (p.get("title") == chosen.get("title"))
            card = tk.Frame(pitch_box, bg=(C["rust_deep"] if sel else C["panel2"]),
                            padx=8, pady=6, highlightthickness=1,
                            highlightbackground=(C["rust"] if sel else C["line"]))
            card.pack(fill="x", pady=3)
            tk.Label(card, text="%d. %s" % (i + 1, p.get("title", "")), bg=card["bg"],
                     fg=C["ink"], font=fsm, anchor="w", justify="left").pack(anchor="w")
            tk.Label(card, text=p.get("logline", ""), bg=card["bg"], fg=C["muted"],
                     font=fsm, wraplength=520, justify="left").pack(anchor="w")
            tk.Label(card, text="hook: " + (p.get("hook", "") or ""), bg=card["bg"],
                     fg=C["faint"], font=fmono, wraplength=520, justify="left").pack(anchor="w")
            btn(card, "CHOOSE", lambda i=i: _choose_pitch(i)).pack(anchor="e")
            for wdg in (card,):
                wdg.bind("<Button-1>", lambda e, i=i: _choose_pitch(i))
        if st["pj"] and (st["pj"].get("pitch") or {}).get("title") and not pitches:
            lam(pitch_box, "using your idea: %s" % st["pj"]["pitch"].get("title"),
                fg=C["moss"]).pack(anchor="w")
        if pitch_box.winfo_children():
            _paint()

    # ---- SCRIPT ----------------------------------------------------------------------
    def _write_script():
        pj = st["pj"]
        if not pj or not pj.get("pitch"):
            _say("choose a pitch (or USE MY IDEA) before writing the script", warn=True)
            return

        def run():
            sc = sp.write_script(pj)
            if sc:
                pj["script"] = sc
            return pj.get("script")

        _work(run, done=lambda sc: (_fill_script(sc), _sync_done(), _refresh_all(),
                                    _say("script ready — edit it and press SAVE EDITS")),
              label="writing script")

    def _fill_script(sc):
        try:
            t_script.delete("1.0", "end")
            t_script.insert("1.0", studio_script_to_text(sc))
        except Exception:
            pass

    def _save_edits():
        pj = st["pj"]
        if not pj:
            _say("no project yet", warn=True)
            return
        pj["script"] = studio_text_to_script(t_script.get("1.0", "end"), pj.get("script"))
        try:
            sp.save_project(pj)
        except Exception as exc:
            _say("could not save edits: %s" % exc, warn=True)
            return
        _sync_done()
        _mark_done()
        _say("script edits saved")

    def _rewrite(how):
        pj = st["pj"]
        if not pj or not pj.get("script"):
            _say("write the script first", warn=True)
            return

        def run():
            d = sp.get_director()
            try:
                d.load()
            except Exception:
                pass
            return d.rewrite(t_script.get("1.0", "end"), how)

        def after(out):
            try:
                t_script.delete("1.0", "end")
                t_script.insert("1.0", out or "")
            except Exception:
                pass
            _save_edits()
            _say("rewrote (%s)" % how)

        _work(run, done=after, label="rewriting (%s)" % how)

    # ---- SHOTS -----------------------------------------------------------------------
    def _plan_shots():
        pj = st["pj"]
        if not pj or not pj.get("script"):
            _say("write the script before planning shots", warn=True)
            return

        def run():
            shots = sp.plan_shots(pj)
            if shots:
                pj["shots"] = shots
            return pj.get("shots") or []

        _work(run, done=lambda sh: (_say("planned %d shots" % len(sh)), _sync_done(),
                                    _refresh_all()),
              label="planning shots")

    def _voice():
        pj = st["pj"]
        if not pj or not pj.get("shots"):
            _say("plan shots before voicing lines", warn=True)
            return
        _work(lambda: sp.voice_lines(pj),
              done=lambda sh: (_say("voiced lines"), _refresh_all()),
              label="voicing lines")

    def _save_shots():
        pj = st["pj"]
        if not pj:
            return
        for sh in (pj.get("shots") or []):
            v = st["shot_vars"].get(sh.get("n"))
            if v:
                sh["action"] = v["action"].get()
                sh["line"] = v["line"].get()
        try:
            sp.save_project(pj)
        except Exception:
            pass
        _say("shot edits saved")

    def _shot_card(parent, sh, mode):
        n = sh.get("n")
        card = tk.Frame(parent, bg=C["panel2"], padx=8, pady=6, highlightthickness=1,
                        highlightbackground=C["line"])
        card.pack(fill="x", pady=3)
        head = tk.Frame(card, bg=C["panel2"])
        head.pack(fill="x")
        tk.Label(head, text="SHOT %s · %ss · %s" % (n, sh.get("seconds"),
                 studio_plain_intent(sh.get("intent"))), bg=C["panel2"], fg=C["ink"],
                 font=fsm).pack(side="left")
        tk.Label(head, text="engine %s (%s)" % (sh.get("engine"), sh.get("video_mode")),
                 bg=C["panel2"], fg=C["faint"], font=fmono).pack(side="right")
        v = st["shot_vars"].setdefault(n, {"action": tk.StringVar(value=sh.get("action") or ""),
                                           "line": tk.StringVar(value=sh.get("line") or "")})
        v["action"].set(sh.get("action") or "")
        v["line"].set(sh.get("line") or "")
        row = tk.Frame(card, bg=C["panel2"])
        row.pack(fill="x", pady=(4, 0))
        lam(row, "action", bg=C["panel2"]).pack(side="left")
        tk.Entry(row, textvariable=v["action"], width=46, bg="#100e0c", fg=C["ink"],
                 relief="flat", font=fsm, insertbackground=C["ink"]).pack(side="left", padx=4)
        row2 = tk.Frame(card, bg=C["panel2"])
        row2.pack(fill="x", pady=(2, 0))
        lam(row2, "line", bg=C["panel2"]).pack(side="left")
        tk.Entry(row2, textvariable=v["line"], width=46, bg="#100e0c", fg=C["ink"],
                 relief="flat", font=fsm, insertbackground=C["ink"]).pack(side="left", padx=4)
        ctl = tk.Frame(card, bg=C["panel2"])
        ctl.pack(fill="x", pady=(4, 0))
        thumb_lbl = tk.Label(ctl, bg="#100e0c", fg=C["faint"], text="no take", width=6)
        if mode == "plan":
            btn(ctl, "REROLL", lambda n=n: _reroll(n)).pack(side="left")
            cp_ = _take_clip(sh.get("chosen"))
            img = thumb_photo(cp_, 90) if cp_ else None
            if img is not None:
                thumb_lbl.configure(image=img, text="")
                thumb_lbl.image = img
            thumb_lbl.pack(side="left", padx=6)
        else:
            btn(ctl, "RENDER", lambda n=n: _render_one(n)).pack(side="left")
            cp_ = _take_clip(sh.get("chosen"))
            img = thumb_photo(cp_, 90) if cp_ else None
            if img is not None:
                thumb_lbl.configure(image=img, text="")
                thumb_lbl.image = img
            thumb_lbl.pack(side="left", padx=6)
            takes = sh.get("takes") or []
            if takes:
                tk.Label(ctl, text="takes:", bg=C["panel2"], fg=C["muted"], font=fsm).pack(side="left")
                for pid in takes:
                    mark = "✓ " if pid == sh.get("chosen") else ""
                    btn(ctl, "%s%s" % (mark, str(pid)[:8]),
                        lambda p=pid, nn=n: _pick_chosen(nn, p)).pack(side="left", padx=2)
            lam(ctl, "status %s" % sh.get("status"), bg=C["panel2"], fg=C["faint"]).pack(side="right")
        return card

    def _build_shot_cards():
        for box, mode in ((shots_box, "plan"), (render_box, "render")):
            for c in box.winfo_children():
                c.destroy()
            pj = st["pj"] or {}
            shots = pj.get("shots") or []
            if not shots:
                lam(box, "no shots yet", fg=C["faint"]).pack(anchor="w")
                continue
            for sh in shots:
                _shot_card(box, sh, mode)
        _paint()

    def _reroll(n):
        pj = st["pj"]

        def run():
            r = sp.render_shot(pj, n, seed=-1)
            pid = (r or {}).get("pid")
            if pid:
                for sh in pj["shots"]:
                    if sh.get("n") == n:
                        sh["chosen"] = pid
            try:
                sp.save_project(pj)
            except Exception:
                pass
            return r

        _work(run, done=lambda r: (_say("rerolled shot %s -> %s" % (n, (r or {}).get("pid"))),
                                   _refresh_all()), label="rerolling shot %s" % n)

    def _pick_chosen(n, pid):
        pj = st["pj"]
        for sh in (pj.get("shots") or []):
            if sh.get("n") == n:
                sh["chosen"] = pid
        try:
            sp.save_project(pj)
        except Exception:
            pass
        _sync_done()
        _refresh_all()
        _say("shot %s now uses take %s" % (n, str(pid)[:8]))

    # ---- RENDER ----------------------------------------------------------------------
    def _store_est(e):
        st["estimate"] = e

    def _estimate_click():
        pj = st["pj"]
        if not pj or not pj.get("shots"):
            _say("plan shots before pricing", warn=True)
            return
        _work(lambda: sp.estimate(pj),
              done=lambda e: (_store_est(e), _refresh_all(),
                              _say("estimated %s s total" % e.get("total_s"))),
              label="pricing shots")

    def _render_confirm_text():
        est = st.get("estimate") or {"total_s": 0, "per_shot": []}
        pj = st["pj"] or {}
        lines = ["Render all shots in %r?" % (pj.get("title") or pj.get("slug")),
                 "",
                 "TOTAL %s s (%.1f min)" % (est.get("total_s"), (est.get("total_s") or 0) / 60.0),
                 ""]
        for e in est.get("per_shot") or []:
            lines.append("  shot %s  %ss  %sx%s  ~%s s"
                         % (e.get("n"), e.get("seconds"), (e.get("canvas") or ["?", "?"])[0],
                            (e.get("canvas") or ["?", "?"])[1], e.get("estimate_s")))
        lines += ["", "This submits real jobs to forge. Continue?"]
        return "\n".join(lines)

    def _render_all():
        pj = st["pj"]
        if not pj or not pj.get("shots"):
            _say("plan shots before rendering", warn=True)
            return
        if st.get("estimate") is None:
            _work(lambda: sp.estimate(pj),
                  done=lambda e: (_store_est(e), _refresh_all(), _render_all()),
                  label="pricing shots")
            return
        txt = _render_confirm_text()
        st["render_confirm"] = txt
        if not messagebox.askokcancel("Render all", txt, parent=win):
            _say("render cancelled")
            return

        def loop():
            ran = []
            for sh in list(pj["shots"]):
                if sh.get("chosen") and sh.get("status") == "done":
                    continue
                sp.render_shot(pj, sh["n"])
                ran.append(sh["n"])
                win.after(0, lambda nn=sh["n"]: _shot_progress(nn))
            return ran

        _work(loop, done=lambda ran: (_say("rendered shots: %s" % (ran or "none")),
                                      _refresh_all()), label="rendering all shots")

    def _shot_progress(n):
        st["progress"] = "shot %s done" % n
        try:
            prog_lbl.configure(text=st["progress"])
        except Exception:
            pass

    def _render_one(n):
        pj = st["pj"]
        _work(lambda: sp.render_shot(pj, n),
              done=lambda r: (_say("shot %s -> %s" % (n, (r or {}).get("status"))), _refresh_all()),
              label="rendering shot %s" % n)

    def _build_estimate():
        est = st.get("estimate")
        if not est:
            est_lbl.configure(text="no estimate yet — PLAN SHOTS, then ESTIMATE")
            return
        parts = ["TOTAL ~%s s (%.1f min)" % (est.get("total_s"), (est.get("total_s") or 0) / 60.0)]
        for e in est.get("per_shot") or []:
            parts.append("  shot %s  %ss  %sx%s  ~%s s"
                         % (e.get("n"), e.get("seconds"), (e.get("canvas") or ["?", "?"])[0],
                            (e.get("canvas") or ["?", "?"])[1], e.get("estimate_s")))
        est_lbl.configure(text="\n".join(parts))

    # ---- CUT -------------------------------------------------------------------------
    def _assemble():
        pj = st["pj"]
        if not pj or not pj.get("shots"):
            _say("render the shots before assembling", warn=True)
            return

        def run():
            cut = sp.assemble(pj)
            if cut:
                pj["cut"] = cut
            mp4 = (pj.get("cut") or {}).get("mp4")
            frames = _extract_frames(mp4)
            post = ""
            pp = (pj.get("cut") or {}).get("post")
            if pp and os.path.exists(pp):
                try:
                    with open(pp) as fh:
                        post = fh.read()
                except Exception:
                    post = ""
            return {"cut": pj.get("cut"), "frames": frames, "post": post}

        _work(run, done=_assemble_done, label="assembling the cut")

    def _extract_frames(mp4):
        if not mp4 or not os.path.exists(mp4):
            return []
        d = os.path.join(tempfile.gettempdir(), "forge_lab_studio_frames")
        try:
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
            r = weirdo_player_frames(mp4, d)
            if r.returncode != 0:
                return []
            return [os.path.join(d, x) for x in sorted(os.listdir(d)) if x.endswith(".png")]
        except Exception:
            return []

    def _assemble_done(res):
        frames = []
        for p in (res or {}).get("frames") or []:
            img = thumb_photo(p, 360)      # Tk images are built on the MAIN thread only
            if img is not None:
                frames.append(img)
        st["frames"]["list"] = frames
        st["frames"]["i"] = 0
        try:
            t_post.delete("1.0", "end")
            t_post.insert("1.0", (res or {}).get("post") or "")
        except Exception:
            pass
        if frames:
            try:
                prev_lbl.configure(image=frames[0], text="")
                prev_lbl.image = frames[0]
            except Exception:
                pass
        _sync_done()
        _refresh_all()
        _say("cut ready — %d frames loaded%s"
             % (len(frames), "" if frames else " (frame decode produced none)"))

    # ---- refresh ---------------------------------------------------------------------
    def _paint():
        if btn_colors:
            try:
                btn_colors(win)
            except Exception:
                pass

    def _refresh_all():
        _build_cast()
        _build_pitch_cards()
        _build_shot_cards()
        _build_estimate()
        pj = st["pj"] or {}
        if st["estimate"] is None and (pj.get("shots")):
            pass                            # priced on demand — never spend GPU time here
        # preview: prefer the cut, then a chosen take, then the character still
        if not st["frames"]["list"]:
            _show_image(_preview_path())
        _rebuild_proj_menu()
        _sync_done()
        _paint()

    # ---- open / new ------------------------------------------------------------------
    def _reset_form():
        st["pj"] = None
        st["chars"] = []
        st["estimate"] = None
        st["sheet_est"] = None
        st["render_confirm"] = ""
        st["sheet_confirm"] = ""
        st["shot_vars"] = {}
        st["frames"] = {"list": [], "i": 0, "after": None}
        _stop_play()
        for wdg in (e_idea,):
            wdg.delete(0, "end")
        try:
            t_script.delete("1.0", "end")
            t_post.delete("1.0", "end")
        except Exception:
            pass
        len_var.set("20")
        qual_var.set("Draft")
        proj_var.set("— new —")
        try:
            prev_lbl.configure(image="", text="nothing yet — pick a character in 1 · CAST")
            prev_lbl.image = None
            prog_lbl.configure(text="")
            est_lbl.configure(text="no estimate yet — PLAN SHOTS, then ESTIMATE")
        except Exception:
            pass

    def _new_form():
        _reset_form()
        _refresh_all()
        try:
            e_idea.focus_set()
            st["focus_target"] = e_idea
        except Exception:
            pass
        _say("New project — pick a character in 1 · CAST, type an idea in 2 · IDEA, "
             "then press USE MY IDEA (or PITCH ME 5).")

    def _open_project(slug):
        pj = sp.load_project(slug)
        if not pj:
            _say("no such project: %s" % slug, warn=True)
            return
        _reset_form()
        st["pj"] = pj
        st["chars"] = list(pj.get("characters") or [])
        try:
            e_idea.delete(0, "end")
            e_idea.insert(0, pj.get("idea") or "")
            len_var.set(str(pj.get("length_s") or 20))
            qual_var.set(pj.get("quality") or "Draft")
            proj_var.set(slug)
            _fill_script(pj.get("script") or {})
        except Exception:
            pass
        st["estimate"] = None
        # restore the cut: load frames + post text
        cut = pj.get("cut") or {}
        if cut.get("mp4"):
            try:
                res = {"frames": _extract_frames(cut["mp4"]), "post": ""}
                if cut.get("post") and os.path.exists(cut["post"]):
                    with open(cut["post"]) as fh:
                        res["post"] = fh.read()
                _assemble_done(res)
            except Exception:
                pass
        _refresh_all()
        _say("opened project %s" % slug)

    # ---- exports for drivers ---------------------------------------------------------
    def _dump():
        pj = st["pj"] or {}
        shots = []
        for s in (pj.get("shots") or []):
            shots.append({"n": s.get("n"), "seconds": s.get("seconds"),
                          "intent": s.get("intent"),
                          "intent_plain": studio_plain_intent(s.get("intent")),
                          "engine": s.get("engine"), "video_mode": s.get("video_mode"),
                          "action": s.get("action"), "line": s.get("line"),
                          "chosen": s.get("chosen"), "takes": list(s.get("takes") or []),
                          "status": s.get("status")})
        return {
            "slug": pj.get("slug"), "title": pj.get("title"),
            "step_done": {str(k): bool(v) for k, v in st["done"].items()},
            "step_marks": {str(k): marks[k].cget("text") for k in marks},
            "chars": list(st["chars"]),
            "idea": e_idea.get(),
            "length": len_var.get(), "quality": qual_var.get(),
            "pitches": len(pj.get("pitches") or []),
            "pitch_title": (pj.get("pitch") or {}).get("title"),
            "script_text": t_script.get("1.0", "end").strip(),
            "script": (pj.get("script") or {}),
            "shots": shots,
            "estimate": st.get("estimate"),
            "render_confirm": st.get("render_confirm"),
            "sheet_confirm": st.get("sheet_confirm"),
            "cut": (pj.get("cut") or {}).get("mp4"),
            "post_text": t_post.get("1.0", "end").strip(),
            "status": status.cget("text"),
            "busy": st.get("busy"),
            "canvas_bbox": canvas.bbox("all"),
            "canvas_scrollregion": str(canvas.cget("scrollregion")),
            "canvas_yscroll": str(canvas.cget("yscrollcommand")),
            "steps_reqheight": steps.winfo_reqheight(),
            "steps_reqwidth": steps.winfo_reqwidth(),
            "frames": len(st["frames"]["list"]),
            "log": t_log.get("1.0", "end").strip(),
            "buttons": len(_all_btns()),
            "pack_label": pack_lbl.cget("text"),
        }

    win.studio_dump = _dump
    win.studio_state = st
    win.studio_canvas = canvas
    win.studio_new = _new_form
    win.studio_open = _open_project
    win.studio_select_char = _select_char
    win.studio_set_idea = lambda t: (e_idea.delete(0, "end"), e_idea.insert(0, t))
    win.studio_pitch = _pitch
    win.studio_use_idea = _use_idea
    win.studio_choose_pitch = _choose_pitch
    win.studio_write_script = _write_script
    win.studio_script_text = lambda: t_script.get("1.0", "end")
    win.studio_set_script_text = lambda t: (t_script.delete("1.0", "end"), t_script.insert("1.0", t))
    win.studio_save_edits = _save_edits
    win.studio_plan_shots = _plan_shots
    win.studio_save_shots = _save_shots
    win.studio_voice = _voice
    win.studio_estimate = _estimate_click
    win.studio_render_confirm_text = _render_confirm_text
    win.studio_render_all = _render_all
    win.studio_render_shot = _render_one
    win.studio_reroll = _reroll
    win.studio_pick_chosen = _pick_chosen
    win.studio_assemble = _assemble
    win.studio_sheet_confirm_text = _sheet_confirm_text
    win.studio_build_sheet = _build_sheet
    win.studio_make_voice = _make_voice
    win.studio_write_bible = _write_bible
    win.studio_rewrite = _rewrite
    win.studio_focus_target = lambda: st.get("focus_target")
    win.studio_take_clip = _take_clip
    win.studio_widgets = {"status": status, "idea": e_idea, "script": t_script,
                          "post": t_post, "preview": prev_lbl, "play_btn": play_btn,
                          "log": t_log, "estimate_lbl": est_lbl, "progress": prog_lbl,
                          "pack": pack_lbl}
    win.studio_set_length = lambda v: len_var.set(str(v))
    win.studio_set_quality = lambda v: qual_var.set(v)

    win.bind("<Escape>", lambda _e: win.destroy())
    win.protocol("WM_DELETE_WINDOW", lambda: (_stop_play(), win.destroy()))

    # Open READY: a NEW project form, cursor in IDEA, status saying what to do first.
    _new_form()
    _log_line("Studio ready.")
    _paint()
    return win


def open_characters(root, ctx):
    """The CHARACTER workspace.

    `ctx` carries the palette, the fonts, and a `load_character(meta, still_path)` callback that
    puts a saved character onto the MAIN form.

    WHY ITS OWN WINDOW: creating and editing a character is a CRUD job — a list, a name, a prompt
    block, a still, a seed and a LoRA. None of that belongs in a column that is keyed to ONE
    take. Same reason LONG VIDEO got its own room; the TYPE dropdown is the door.

    ⚠️ The dialog submodules are imported HERE, not at module scope: a missing one costs this
    window, never the app.
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog

    C, F, mono = ctx["C"], ctx["fonts"], ctx["mono"]
    f, fh, fsm = F["f"], F["fh"], F["fsm"]
    fmono = mono["m"]
    load_into_form = ctx.get("load_character")

    win = tk.Toplevel(root)
    win.title("Characters")
    win.configure(bg=C["bg"])
    # geometry() is a REQUEST; size from the real screen and leave room for the chrome.
    _sw, _sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1040, _sw - 60), min(830, _sh - 120)))

    def lam(parent, text, **kw):
        kw.setdefault("bg", C["panel"])
        kw.setdefault("fg", C["muted"])
        return tk.Label(parent, text=text, font=kw.pop("font", fsm), **kw)

    def btn(parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, bg=(C["rust_deep"] if primary else C["panel2"]),
                         fg="#fff" if primary else C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=10, pady=3, borderwidth=0,
                         highlightthickness=1, highlightbackground=C["line"], command=cmd)

    ed = {}          # slug / still_src / thumb — the character currently being edited
    rows = {"list": []}

    # ---- header -----------------------------------------------------------------
    hd = tk.Frame(win, bg=C["bg"])
    hd.pack(fill="x", padx=14, pady=(12, 2))
    tk.Label(hd, text="CHARACTERS", bg=C["bg"], fg=C["ink"], font=fh).pack(side="left")
    status = tk.Label(hd, text="", bg=C["bg"], fg=C["faint"], font=fsm)
    status.pack(side="left", padx=12)
    # ⚠️ NEW belongs in the HEADER. The step-5 copy sits ~1300px down a SCROLLING column, so
    # with an empty list the first thing you need was the last thing on screen. Same placement
    # as the Long-video window's NEW.
    btn(hd, "NEW", lambda: _new()).pack(side="right", padx=(0, 6))
    btn(hd, "CLOSE", win.destroy).pack(side="right")
    lam(win, "Pick one on the left to edit it, or NEW to make one.  LOAD puts its prompt, "
             "seed and still onto the main form.", fg=C["faint"], wraplength=940,
        justify="left").pack(anchor="w", padx=14, pady=(0, 8))

    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=14, pady=(0, 14))

    # ---- left: the list ---------------------------------------------------------
    lf = tk.Frame(body, bg=C["panel"])
    lf.pack(side="left", fill="y")
    lam(lf, "SAVED", font=fmono).pack(anchor="w", padx=8, pady=(8, 2))
    lb = tk.Listbox(lf, width=28, height=18, bg="#100e0c", fg=C["ink"], relief="flat",
                    font=fsm, selectbackground=C["rust"], selectforeground="#fff",
                    highlightthickness=0, activestyle="none")
    lb.pack(fill="y", expand=True, padx=8, pady=(0, 8))

    # ---- right: the editor ------------------------------------------------------
    # ⚠️ The WHOLE column scrolls. The window is capped by the screen, and a column taller than
    # the window does not stretch it — Tk silently STARVES the tail instead, so the last step
    # (and LOAD INTO FORM) would just not be on screen, with no error anywhere. That is the
    # documented "geometry() is a REQUEST" trap, and it bit the Long-video window twice.
    rwrap = tk.Frame(body, bg=C["panel"])
    rwrap.pack(side="left", fill="both", expand=True, padx=(10, 0))
    rvsb = tk.Scrollbar(rwrap, orient="vertical")
    rvsb.pack(side="right", fill="y")
    rcan = tk.Canvas(rwrap, bg=C["panel"], highlightthickness=0, yscrollcommand=rvsb.set)
    rcan.pack(side="left", fill="both", expand=True)
    rvsb.configure(command=rcan.yview)
    rt = tk.Frame(rcan, bg=C["panel"])
    _rtw = rcan.create_window((0, 0), window=rt, anchor="nw")
    rt.bind("<Configure>", lambda e: rcan.configure(scrollregion=rcan.bbox("all")))
    rcan.bind("<Configure>", lambda e: rcan.itemconfigure(_rtw, width=e.width))
    scoped_wheel(win, rcan)          # ⚠️ never bind_all/unbind_all the wheel — see scoped_wheel()

    _steps = []      # driver export: the numbered flow, so a test can assert it exists

    def step(n, text):
        """A numbered chip. THE FLOW IS THE INSTRUCTION — the number is the answer to
        'what do I do next', which the window previously never said."""
        fr = tk.Frame(rt, bg=C["panel"])
        fr.pack(fill="x", padx=10, pady=(14, 4))
        tk.Label(fr, text=" %d " % n, bg=C["rust"], fg="#14110f", font=fh).pack(side="left")
        tk.Label(fr, text=text, bg=C["panel"], fg=C["ink"], font=fsm).pack(side="left", padx=8)
        _steps.append((n, text))
        return fr

    # ---- 1 · the body, as SLIDERS ---------------------------------------------------
    # Ryan: "instead of drop downs for body features it should be a scroll bar like in video
    # games character creation ... the drop downs seem very limiting."
    #
    # A slider is a NUMBER, and a number is what a scripted 3D build can actually consume - so
    # these are the very parameters a Blender build would read. That is why sliders and the
    # Blender route are the same feature, not two.
    step(1, "SHAPE THE BODY")
    lam(rt, "Slide to exaggerate. The middle is ordinary; the words underneath are exactly "
            "what the block will say.", fg=C["faint"], wraplength=640,
        justify="left").pack(anchor="w", padx=10)
    pick, slide = {}, {}
    for _ax, _phr in CHAR_SLIDERS:
        _row = tk.Frame(rt, bg=C["panel"])
        _row.pack(fill="x", padx=10, pady=(6, 0))
        _top = tk.Frame(_row, bg=C["panel"])
        _top.pack(fill="x")
        lam(_top, _ax.upper(), font=fmono).pack(side="left")
        _val = tk.Label(_top, text="0", bg=C["panel"], fg=C["ink"], font=fmono)
        _val.pack(side="right")
        _var = tk.IntVar(value=0)
        _sc = tk.Scale(_row, from_=-3, to=3, orient="horizontal", variable=_var,
                       showvalue=False, resolution=1, bg=C["panel"], fg=C["ink"],
                       troughcolor="#100e0c", activebackground=C["rust"], relief="flat",
                       highlightthickness=0, borderwidth=0, sliderlength=26, width=12,
                       sliderrelief="flat", command=lambda *_: _on_slide())
        _sc.pack(fill="x")
        _phl = lam(_row, _phr[3] or CHAR_SLIDER_NEUTRAL, fg=C["faint"])
        _phl.pack(anchor="w")
        slide[_ax] = {"var": _var, "val": _val, "phrase": _phl, "spec": _phr, "scale": _sc}

    # ---- 2 · the look, as DROPDOWNS -------------------------------------------------
    step(2, "CHOOSE THE LOOK")
    lam(rt, "These are CHOICES, not amounts \u2014 you cannot slide 'claymation' to +2.",
        fg=C["faint"]).pack(anchor="w", padx=10)
    ogrid = tk.Frame(rt, bg=C["panel"])
    ogrid.pack(fill="x", padx=10, pady=(6, 0))
    for _i, (_axis, _vals) in enumerate(CHAR_OPTIONS):
        _col, _row = divmod(_i, 6)          # two columns, six rows
        ogrid.columnconfigure(_col, weight=1)
        cell = tk.Frame(ogrid, bg=C["panel"])
        cell.grid(row=_row, column=_col, sticky="ew", padx=(0, 10), pady=2)
        lam(cell, _axis.upper(), font=fmono).pack(anchor="w")
        _v = tk.StringVar(value=_vals[0])
        _m = tk.OptionMenu(cell, _v, *_vals, command=lambda *_: _on_pick())
        _m.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                     highlightthickness=0, activebackground=C["rust"], activeforeground="#fff",
                     borderwidth=0, anchor="w", width=30)
        _m["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup WHITE
        _m.pack(fill="x")
        pick[_axis] = _v

    # ---- 2 · the block --------------------------------------------------------------
    step(3, "BUILD THE BLOCK")
    orow = tk.Frame(rt, bg=C["panel"])
    orow.pack(fill="x", padx=10)
    btn(orow, "BUILD PROMPT", lambda: _build_prompt(force=True), primary=True).pack(side="left")
    est_lbl = lam(orow, "", fg=C["ink"])
    est_lbl.pack(side="left", padx=10)
    t_prompt = tk.Text(rt, height=4, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                       font=f, padx=6, pady=4, insertbackground=C["ink"])
    t_prompt.pack(fill="x", padx=10, pady=(6, 0))
    lam(rt, "It is yours once you type in it \u2014 picks will not overwrite your words.",
        fg=C["faint"]).pack(anchor="w", padx=10, pady=(2, 0))

    # ⚠️ ACTION is what makes a VIDEO rather than a still. Without it, LOAD INTO FORM in a
    # video mode pasted a photo prompt with no motion in it at all.
    arow = tk.Frame(rt, bg=C["panel"])
    arow.pack(fill="x", padx=10, pady=(10, 0))
    lam(arow, "ACTION   (what they DO in a video)").pack(anchor="w")
    e_action = tk.Text(rt, height=2, wrap="word", bg="#100e0c", fg=C["ink"], relief="flat",
                       font=f, padx=6, pady=4, insertbackground=C["ink"])
    e_action.pack(fill="x", padx=10, pady=(2, 0))
    lam(rt, "Used when you LOAD INTO FORM in a video mode. Leave blank and it is just the look.",
        fg=C["faint"], wraplength=640, justify="left").pack(anchor="w", padx=10, pady=(2, 0))

    # ---- 3 · the still --------------------------------------------------------------
    step(4, "RENDER THE STILL")
    srow = tk.Frame(rt, bg=C["panel"])
    srow.pack(fill="x", padx=10)
    btn(srow, "RENDER STILL", lambda: _render(), primary=True).pack(side="left")
    btn(srow, "CHOOSE\u2026", lambda: _choose_still()).pack(side="left", padx=6)
    btn(srow, "CLEAR", lambda: _set_still(None)).pack(side="left")
    btn(srow, "ENLARGE", lambda: _view()).pack(side="left", padx=6)
    # The canvas is derived from FRAMING, so show it - otherwise the size is invisible magic.
    # SIZE is the ITERATION LADDER: Full is the keeper default; Fast/Draft exist so a quick
    # composition check does not cost 89 s every time.
    size_row = tk.Frame(rt, bg=C["panel"])
    size_row.pack(fill="x", padx=10, pady=(6, 0))
    lam(size_row, "SIZE").pack(side="left")
    size_var = tk.StringVar(value="Full")
    size_menu = tk.OptionMenu(size_row, size_var, *[k for k, _ in CHAR_STILL_SCALE],
                              command=lambda *_: _refresh_est())
    size_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust"], activeforeground="#fff",
                        borderwidth=0, width=7, anchor="w")
    size_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    size_menu.pack(side="left", padx=6)
    size_lbl = lam(size_row, "", fg=C["faint"], font=fmono)
    size_lbl.pack(side="left", padx=6)

    # ⚠️ The preview is CLICKABLE and big enough to actually judge a face. It was a 112px
    # thumbnail that did nothing on click — useless for the one thing it is for.
    prev = tk.Frame(rt, bg="#100e0c", height=300, cursor="hand2")
    prev.pack(fill="x", padx=10, pady=(6, 0))
    prev.pack_propagate(False)
    prev_lbl = tk.Label(prev, bg="#100e0c", fg=C["faint"], cursor="hand2",
                        text="no still yet \u2014 render one, or CHOOSE a picture")
    prev_lbl.pack(expand=True)
    for _w in (prev, prev_lbl):
        _w.bind("<Button-1>", lambda *_: _view())
    still_lbl = lam(rt, "(none)", fg=C["faint"], anchor="w")
    still_lbl.pack(anchor="w", padx=10, pady=(4, 0))

    # ---- 4 · save -------------------------------------------------------------------
    step(5, "SAVE IT")
    nrow = tk.Frame(rt, bg=C["panel"])
    nrow.pack(fill="x", padx=10)
    lam(nrow, "NAME").pack(side="left")
    e_name = tk.Entry(nrow, width=24, bg="#100e0c", fg=C["ink"], relief="flat", font=fsm,
                      insertbackground=C["ink"])
    e_name.pack(side="left", padx=(6, 16))
    lam(nrow, "SEED").pack(side="left")
    e_seed = tk.Entry(nrow, width=12, bg="#100e0c", fg=C["ink"], relief="flat", font=fmono,
                      insertbackground=C["ink"])
    e_seed.pack(side="left", padx=6)

    krow = tk.Frame(rt, bg=C["panel"])
    krow.pack(fill="x", padx=10, pady=(6, 0))
    lam(krow, "LORA").pack(side="left")
    lora_var = tk.StringVar(value="(none)")
    lo_menu = tk.OptionMenu(krow, lora_var, *CHARACTER_LORAS.keys())
    lo_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                      highlightthickness=0, activebackground=C["rust"], activeforeground="#fff",
                      borderwidth=0, width=22)
    lo_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    lo_menu.pack(side="left", padx=6)
    lam(krow, "strength").pack(side="left")
    e_lstr = tk.Entry(krow, width=5, bg="#100e0c", fg=C["ink"], relief="flat", font=fmono,
                      insertbackground=C["ink"])
    e_lstr.insert(0, "1.0")
    e_lstr.pack(side="left", padx=6)

    brow = tk.Frame(rt, bg=C["panel"])
    brow.pack(fill="x", padx=10, pady=(8, 0))
    btn(brow, "NEW", lambda: _new()).pack(side="left")
    btn(brow, "SAVE", lambda: _save(), primary=True).pack(side="left", padx=6)
    btn(brow, "SAVE AS\u2026", lambda: _save_as()).pack(side="left")
    btn(brow, "DELETE", lambda: _delete()).pack(side="left", padx=6)
    ed_note = lam(rt, "", fg=C["ochre"], wraplength=560, justify="left")
    ed_note.pack(anchor="w", padx=10, pady=(6, 0))

    # ---- 5 · use it -----------------------------------------------------------------
    step(6, "USE IT")
    brow2 = tk.Frame(rt, bg=C["panel"])
    brow2.pack(fill="x", padx=10, pady=(0, 14))
    btn(brow2, "LOAD INTO FORM", lambda: _load(), primary=True).pack(side="left")
    lam(brow2, "puts its block, seed and still on the main form", fg=C["faint"]).pack(
        side="left", padx=8)

    # ⚠️ Built ON DEMAND, long after `_btn_colors(win)` ran at the end of construction, so the
    # buttons here were never repainted — the ONLY window in the app out of step with the rest.
    # Ryan: "the buttons are hard to read for me and should be updated to have black text."
    # `_btn_colors` is a launch() local, so it arrives through ctx.
    if ctx.get("btn_colors"):
        try:
            ctx["btn_colors"](win)
        except Exception:
            pass

    # ---- behaviour --------------------------------------------------------------
    composed = {}    # composed["last"] — the last text WE composed, so hand-edits are never clobbered
    job = {}         # job["running"] / ["meta"] / ["err"] — the render currently in flight

    def _picks():
        out = {k: v.get() for k, v in pick.items()}
        for _k, _s in slide.items():
            out[_k] = int(_s["var"].get())
        return out

    def _sync_slider(ax):
        s = slide[ax]
        n = int(s["var"].get())
        s["val"].configure(text=("%+d" % n) if n else "0",
                           fg=(C["faint"] if n == 0 else C["ink"]))
        s["phrase"].configure(text=s["spec"][n + 3] or CHAR_SLIDER_NEUTRAL)

    def _on_slide():
        """A Scale fires on EVERY drag tick, so this keeps the block in step SILENTLY while
        the thumb moves. It never warns - nagging once per pixel would be unusable."""
        for _ax in list(slide):
            _sync_slider(_ax)
        cur = t_prompt.get("1.0", "end").rstrip("\n").strip()
        if not cur or cur == (composed["last"] or "").strip():
            _build_prompt(force=True)
        _refresh_est()

    def _params():
        """A t2i job for this block. warm_engine is deliberately unknown, so the estimate
        INCLUDES the engine-load term — the honest direction to be wrong in.

        The canvas comes from the FRAMING, not a fixed square (see CHAR_STILL_SIZE)."""
        _w, _h = char_still_size(pick["Framing"].get(), size_var.get())
        return {"prompt": t_prompt.get("1.0", "end").strip(),
                "width": _w, "height": _h, "resolution": max(_w, _h),
                "steps": 25, "seed": -1, "prefix": "forge_lab_char"}

    def _sync_size():
        """⚠️ The canvas is DERIVED from the framing, so it has to be VISIBLE. A size that
        changes on screen with no readout is the same lie as a control that changes nothing."""
        _w, _h = char_still_size(pick["Framing"].get(), size_var.get())
        _note = "" if size_var.get() == "Full" else "   (iteration only)"
        size_lbl.configure(text="canvas %d×%d  ·  %.1f MP  ·  %d steps%s"
                                % (_w, _h, _w * _h / 1e6, _params()["steps"], _note))

    def _refresh_est():
        try:
            _sync_size()      # every path that refreshes the estimate also updates the canvas
            est_lbl.configure(text="RENDER \u2248 %.0f s (incl. load)" %
                                   estimate_seconds("t2i", _params(), None, None))
        except Exception:
            est_lbl.configure(text="")

    def _build_prompt(force=False):
        new = compose_character_prompt(_picks())
        cur = t_prompt.get("1.0", "end").rstrip("\n")
        if not force and cur.strip() and cur.strip() != (composed["last"] or "").strip():
            _say("options changed \u2014 BUILD PROMPT overwrites your edits", warn=True)
            return
        t_prompt.delete("1.0", "end")
        t_prompt.insert("1.0", new)
        composed["last"] = new
        _refresh_est()

    def _on_pick():
        cur = t_prompt.get("1.0", "end").rstrip("\n").strip()
        if not cur or cur == (composed["last"] or "").strip():
            _build_prompt(force=True)      # the box is still OURS — keep it in step
        else:
            _say("options changed \u2014 press BUILD PROMPT to overwrite your edits", warn=True)
        _refresh_est()

    def _render():
        if not t_prompt.get("1.0", "end").strip():
            _say("nothing to render \u2014 pick some options first", warn=True)
            return
        if job.get("running"):
            _say("already rendering\u2026")
            return
        job.update({"running": True, "meta": None, "err": None})
        _say("rendering on forge\u2026")

        def _work():
            try:
                pid, _seed = submit("t2i", _params(), None)
                _t0 = time.time()
                while time.time() - _t0 < 1200:
                    time.sleep(3)
                    try:
                        _s = run_state(pid)
                    except Exception:
                        continue
                    if _s.get("state") in ("done", "error", "gone"):
                        break
                job["meta"] = collect(pid)
            except Exception as exc:
                job["err"] = str(exc)
            job["running"] = False

        threading.Thread(target=_work, daemon=True).start()
        win.after(900, _poll)

    def _poll():
        if job.get("running"):
            win.after(900, _poll)
            return
        if job.get("err"):
            _say("render failed: %s" % job["err"], warn=True)
            return
        meta = job.get("meta") or {}
        if meta.get("error"):
            _say("forge error: %s" % meta["error"], warn=True)
            return
        files = [f for f in (meta.get("images") or [])
                 if (f.get("file") or "").lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        if not files:
            _say("the render produced no image", warn=True)
            return
        path = os.path.join(RUNS, meta.get("id") or "", files[0]["file"])
        if not os.path.exists(path):
            _say("render finished but the file is missing", warn=True)
            return
        _set_still(path)
        _plog("character render: %s -> %s" % (meta.get("id"), path))
        _say("rendered \u2014 SAVE keeps it as this character's still")

    def _say(msg, warn=False):
        status.configure(text=msg, fg=(C["ochre"] if warn else C["faint"]))

    def _thumb(path):
        prev_lbl.configure(image="", text="")
        ed["thumb"] = None
        if not path or not os.path.exists(path):
            prev_lbl.configure(text="no still yet \u2014 render one, or CHOOSE a picture")
            return
        img = None
        try:
            img = thumb_photo(path, 290)     # the preview box is 300 tall now, not 112
        except Exception:
            img = None
        if img:
            ed["thumb"] = img            # keep the ref alive or Tk drops it
            prev_lbl.configure(image=img, text="")
        else:
            prev_lbl.configure(text=os.path.basename(path))

    def _open_outside(path):
        """Hand the file to macOS. `open` is the only sane way to get real zoom and pan —
        re-implementing that inside a tk window would be worse than the OS's own viewer."""
        try:
            import subprocess
            subprocess.Popen(["open", path])
        except Exception as exc:
            _say("could not open it: %s" % exc, warn=True)

    def _view():
        """Enlarge the still. The preview box exists to judge a FACE, and no thumbnail does
        that — so the picture opens big, and from there it opens in Preview, where zoom works."""
        path = ed.get("still_src")
        if not path and ed.get("slug"):
            path = character_still(ed["slug"])
        if not path or not os.path.exists(path):
            _say("nothing to enlarge yet \u2014 render or choose a still first", warn=True)
            return
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        vw, vh = min(1100, sw - 80), min(900, sh - 140)
        top = tk.Toplevel(win)
        top.title(os.path.basename(path))
        top.configure(bg=C["bg"])
        top.geometry("%dx%d" % (vw, vh))
        top.bind("<Escape>", lambda *_: top.destroy())
        img = None
        try:
            img = thumb_photo(path, max(200, min(vw - 40, vh - 130)))
        except Exception:
            img = None
        if img:
            lb = tk.Label(top, image=img, bg=C["bg"])
            ed["view_img"] = img         # keep the ref alive or Tk renders an empty box
            lb.pack(expand=True)
        else:
            tk.Label(top, text="cannot preview this file here \u2014 use OPEN IN PREVIEW",
                     bg=C["bg"], fg=C["ochre"], font=fsm).pack(expand=True)
        row = tk.Frame(top, bg=C["bg"])
        row.pack(side="bottom", pady=10)
        tk.Button(row, text="OPEN IN PREVIEW", relief="flat", font=fsm, padx=12, pady=5,
                  borderwidth=0, highlightthickness=0,
                  command=lambda: _open_outside(path)).pack(side="left", padx=6)
        tk.Button(row, text="CLOSE", relief="flat", font=fsm, padx=12, pady=5,
                  borderwidth=0, highlightthickness=0,
                  command=top.destroy).pack(side="left", padx=6)
        # this Toplevel is built on click, so it needs painting like any other on-demand window
        if ctx.get("btn_colors"):
            try:
                ctx["btn_colors"](top)
            except Exception:
                pass

    def _set_still(path):
        ed["still_src"] = path
        still_lbl.configure(text=(os.path.basename(path) if path else "(none)"),
                            fg=(C["ink"] if path else C["faint"]))
        _thumb(path)

    def _choose_still():
        p = filedialog.askopenfilename(
            title="Canonical still for this character",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")])
        if p:
            _set_still(p)

    def _fill(meta, slug):
        meta = meta or {}
        ed["slug"] = slug
        e_name.delete(0, "end")
        e_name.insert(0, meta.get("name") or "")
        t_prompt.delete("1.0", "end")
        if meta.get("prompt"):
            t_prompt.insert("1.0", meta["prompt"])
        e_action.delete("1.0", "end")
        if meta.get("action"):
            e_action.insert("1.0", meta["action"])
        e_seed.delete(0, "end")
        e_seed.insert(0, str(meta.get("seed", -1)))
        _l = meta.get("lora")
        disp = "(none)"
        for k, v in CHARACTER_LORAS.items():
            if v and _l and v == _l:
                disp = k
        lora_var.set(disp)
        e_lstr.delete(0, "end")
        e_lstr.insert(0, str(meta.get("lora_strength", 1.0)))
        ed_note.configure(text="")
        if _l and CHARACTER_LORAS.get(disp) != _l:
            ed_note.configure(
                text="\u26a0 saved with LoRA %s, which this dropdown does not offer \u2014 "
                     "re-pick it before saving or it will be dropped" % _l)
        # restore the OPTION picks. A stored value that is no longer offered falls back to the
        # axis's own default rather than putting an unknown string in the dropdown.
        _opt = dict(CHAR_OPTIONS)
        _pk = meta.get("picks") or {}
        for _ax, _var in pick.items():
            _want = _pk.get(_ax)
            _var.set(_want if _want in _opt.get(_ax, []) else _opt[_ax][0])
        # restore the SLIDERS (they live in the same `picks` dict, as numbers)
        for _ax, _s in slide.items():
            try:
                _n = max(-3, min(3, int(_pk.get(_ax, 0))))
            except Exception:
                _n = 0
            _s["var"].set(_n)
            _sync_slider(_ax)
        # so that changing a pick re-composes when the block still matches it, and warns once it
        # has been hand-edited (the stored prompt is then NOT what the picks compose to).
        composed["last"] = compose_character_prompt(_picks())
        # the character's OWN copy of its still, so a save keeps the face
        _set_still(character_still(slug) if slug else None)
        _refresh_est()

    def _grab():
        return {"name": (e_name.get() or "").strip(),
                "prompt": t_prompt.get("1.0", "end").rstrip("\n"),
                "seed": e_seed.get() or "-1",
                "lora": CHARACTER_LORAS.get(lora_var.get()),
                "lora_strength": e_lstr.get() or "1.0",
                "still_src": ed.get("still_src"),
                "action": e_action.get("1.0", "end").rstrip("\n"),
                "picks": _picks()}

    def _write(slug, g):
        write_character(slug, {"name": g["name"] or slug, "prompt": g["prompt"],
                               "seed": g["seed"], "lora": g["lora"],
                               "lora_strength": g["lora_strength"],
                               "picks": g.get("picks"),
                               "action": g.get("action"),
                               "saved": time.strftime("%Y-%m-%d %H:%M")},
                        still_src=g["still_src"])

    def _relist(keep=None):
        lb.delete(0, "end")
        rows["list"] = list_characters()
        for i, (slug, name) in enumerate(rows["list"]):
            lb.insert("end", name)
            if keep and slug == keep:
                lb.selection_clear(0, "end")
                lb.selection_set(i)
                lb.see(i)

    def _load_slug(slug):
        meta = read_character(slug)
        if not meta:
            _say("%s is gone from disk" % slug, warn=True)
            _relist()
            return
        _fill(meta, slug)

    def _on_list(_e=None):
        sel = lb.curselection()
        if not sel:
            return
        i = sel[0]
        if i < len(rows["list"]):
            slug, name = rows["list"][i]
            _load_slug(slug)
            _say("editing \u201c%s\u201d" % name)

    def _new():
        _fill(None, None)
        lb.selection_clear(0, "end")
        _say("new character \u2014 name it and SAVE")

    def _save_as():
        g = _grab()
        name = simpledialog.askstring("Save character", "Name this character:",
                                      initialvalue=g["name"] or "")
        if not name or not name.strip():
            return
        name = name.strip()
        slug = char_slug(name)
        if read_character(slug) and ed.get("slug") != slug and not messagebox.askyesno(
                "Already exists", "\u201c%s\u201d already exists. Overwrite it?" % name):
            return
        g["name"] = name
        _write(slug, g)
        ed["slug"] = slug
        e_name.delete(0, "end")
        e_name.insert(0, name)
        _relist(keep=slug)
        _say("saved \u201c%s\u201d%s" % (name, " with its still" if g["still_src"]
                                        else " (no still chosen)"))

    def _save():
        if not ed.get("slug"):
            _save_as()
            return
        g = _grab()
        if not g["name"]:
            _say("give it a name first", warn=True)
            return
        _write(ed["slug"], g)
        _relist(keep=ed["slug"])
        _say("updated \u201c%s\u201d" % g["name"])

    def _delete():
        slug = ed.get("slug")
        if not slug:
            _say("pick a saved character first", warn=True)
            return
        nm = e_name.get() or slug
        if not messagebox.askyesno(
                "Delete character",
                "Delete \u201c%s\u201d?\n\nThis removes its folder and its saved still.\n"
                "Local only \u2014 nothing on forge changes." % nm):
            return
        delete_character(slug)
        _fill(None, None)
        _relist()
        _say("deleted \u201c%s\u201d" % nm)

    def _load():
        slug = ed.get("slug")
        if slug:
            meta = read_character(slug)
            still = character_still(slug)
        else:
            # unsaved edits are still loadable — that is the fast path from a fresh setup
            meta = _grab()
            meta["name"] = meta.get("name") or "(unsaved)"
            still = ed.get("still_src")
        if not meta:
            _say("nothing to load", warn=True)
            return
        if not load_into_form:
            _say("no form to load into", warn=True)
            return
        try:
            _say(load_into_form(meta, still))
        except Exception as exc:
            _log_err("character load into form failed: %r" % exc)
            _say("load failed: %s" % exc, warn=True)

    lb.bind("<<ListboxSelect>>", _on_list)
    win.bind("<Escape>", lambda _e: win.destroy())
    _relist()
    # ⚠️ OPEN READY TO CREATE. Ryan: "it should open to a new character i shouldnt have to
    # select it". The editor used to come up blank-but-unlabelled with nothing selected, so the
    # first move was always a scroll to the bottom to find NEW.
    _new()
    if not rows["list"]:
        _say("new character \u2014 no characters saved yet, name it and SAVE")

    # ---- driver exports -------------------------------------------------------------
    # House convention: a workspace window exposes what is ON SCREEN so a driver asserts it
    # rather than poking widgets by luck. TESTCHAR reads these.
    win.char_steps = _steps
    win.char_preview = prev
    win.char_canvas = rcan
    win.char_prompt = t_prompt
    win.char_view = _view
    win.char_set_still = _set_still
    win.char_slides = slide
    win.char_on_slide = _on_slide
    # The save/load internals, so a driver can do a REAL round trip through the same
    # functions the buttons call instead of poking the store behind their backs.
    win.char_grab = _grab
    win.char_write = _write
    win.char_fill = _fill
    win.char_widgets = {"name": e_name, "action": e_action, "seed": e_seed, "prompt": t_prompt}
    win.char_ed = ed
    win.char_scroll = rcan
    win.char_status = status
    win.char_params = _params
    win.char_pick = pick
    win.char_on_pick = _on_pick
    win.char_size_lbl = size_lbl
    win.char_size_var = size_var

    def char_dump():
        return {"steps": [n for n, _ in _steps],
                "axes": [a for a, _ in CHAR_OPTIONS],
                "picks": _picks(),
                "sliders": {k: int(v["var"].get()) for k, v in slide.items()},
                "prompt": t_prompt.get("1.0", "end").strip(),
                "thumb": ed.get("thumb") is not None,
                "still": ed.get("still_src"),
                "slug": ed.get("slug")}

    win.char_dump = char_dump
    return win



def open_song(root, ctx):
    """SONG — the song studio. Style + lyrics in, a finished song out, in ONE window.

    Ryan: "add a section in the forge lab for song creation". This is the door for the TYPE
    entry; nothing is handed off to the main form, because a song is not an image or a video
    and every control on that form (canvas, frames, steps, reference stills) is wrong for it.

    WHY IT IS STILL A MODES ENTRY: `submit("song", p)` is what puts a run in Takes with a
    real engine name, and `engine_of("song") == "yue2"` keeps the cost law honest. It is NOT
    a video variant and must not gain `video: True` (see the Raylight note in the skill).

    The engine itself is the Tk-free `song.py` beside this file — this function is UI only.
    """
    import tkinter as tk
    import song as _song
    try:
        import director as _d0
        _llm_default = _d0.DEFAULT_MODEL
    except Exception:
        _llm_default = "qwen3.8:27b"

    C, F, mono = ctx["C"], ctx["fonts"], ctx["mono"]
    f, fh, fsm = F["f"], F["fh"], F["fsm"]
    fmono = mono["m"]
    submit, run_state, collect = ctx["submit"], ctx["run_state"], ctx["collect"]
    btn_colors = ctx.get("btn_colors")

    win = tk.Toplevel(root)
    win.title("Song")
    win.configure(bg=C["bg"])
    # ⚠️ Size from the SCREEN, never hardcode: Tk starves the tail of the content instead of
    # growing the window past what you asked for, and a starved control is invisible, not an
    # error. Same rule the Long-video window learned the hard way.
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1180, sw - 40), min(820, sh - 120)))

    st = {"busy": False, "pid": None, "player": None, "songs": [], "made": None}

    def lam(parent, text, **kw):
        kw.setdefault("bg", C["panel"])
        kw.setdefault("fg", C["muted"])
        return tk.Label(parent, text=text, font=kw.pop("font", fsm), **kw)

    def btn(parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, bg=(C["rust_deep"] if primary else C["panel2"]),
                         fg="#fff" if primary else C["ink"],
                         activebackground=C["rust"], activeforeground="#fff",
                         relief="flat", font=fsm, padx=10, pady=3, borderwidth=0,
                         highlightthickness=1, highlightbackground=C["line"], command=cmd)

    def say(msg, warn=False):
        status.configure(text=msg, fg=(C["ochre"] if warn else C["faint"]))

    def audio_seconds(path):
        """ffprobe duration for an audio file, or None. Never raises: a missing number must
        not take the window down (same rule as thumb_photo)."""
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", path],
                               capture_output=True, text=True, timeout=30)
            return float((r.stdout or "").strip())
        except Exception:
            return None

    # ---- the words are KEPT, so they never have to be typed twice ----
    # ⚠️ Its OWN file, NOT settings.json: that one is rewritten WHOLESALE by the app's
    # autosave every ~30 s from current_settings(), so any key this window added there would
    # be silently wiped on the next tick. Driver runs neither read nor write it, so a test
    # still starts deterministic and leaves Ryan's saved words alone.
    SONG_STATE = os.path.join(DATA, "song.json")

    def _load_state():
        if _DRIVER:
            return {}
        try:
            with open(SONG_STATE, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def _save_state():
        if _DRIVER or os.environ.get("FORGE_LAB_NOAUTOSAVE") == "1":
            return
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(SONG_STATE, "w", encoding="utf-8") as fh:
                json.dump({"style": t_style.get("1.0", "end").strip(),
                           "lyrics": t_lyr.get("1.0", "end").strip(),
                           "cover": st.get("cover"), "plan": st["plan"],
                           "seconds": dur_var.get()}, fh, indent=1)
        except Exception as exc:
            _log_err("could not save the song words: %r" % exc)

    def stop_play():
        p = st.get("player")
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        st["player"] = None

    def play(path):
        """afplay, not `open`: `open` hands the file to another app and Forge Lab loses the
        handle, so there is no way to STOP it from here."""
        stop_play()
        if not path or not os.path.exists(path):
            say("that file is gone from disk", warn=True)
            return
        try:
            st["player"] = subprocess.Popen(["afplay", path])
            say("playing %s" % os.path.basename(path))
        except Exception as exc:
            say("could not play it: %r" % exc, warn=True)

    # ---- header ---------------------------------------------------------------------
    hd = tk.Frame(win, bg=C["bg"])
    hd.pack(fill="x", padx=14, pady=(12, 2))
    tk.Label(hd, text="SONG", bg=C["bg"], fg=C["ink"], font=fh).pack(side="left")
    status = tk.Label(hd, text="write a style and some lyrics, then press MAKE THE SONG",
                      bg=C["bg"], fg=C["faint"], font=fsm)
    status.pack(side="left", padx=12)
    btn(hd, "CLOSE", win.destroy).pack(side="right")
    lam(win, "YuE2 — style + lyrics in, a finished song out.  It plans a melody first, "
             "then sings it.  Pick a RECORDING and it transcribes that melody and covers it "
             "in your style instead.",
        fg=C["faint"]).pack(anchor="w", padx=14, pady=(0, 8))

    body = tk.Frame(win, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=14, pady=(0, 14))

    # LEFT: the songs.  RIGHT: the four steps.  No scrolling column to lose a control in.
    lf = tk.Frame(body, bg=C["panel"])
    lf.pack(side="left", fill="both", expand=True)
    rp = tk.Frame(body, bg=C["panel"])
    rp.pack(side="left", fill="both", padx=(10, 0))

    lam(lf, "YOUR SONGS  (this session)", font=fmono).pack(anchor="w", padx=10, pady=(10, 4))
    songs_holder = tk.Frame(lf, bg=C["panel"])
    songs_holder.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    empty_lbl = tk.Label(songs_holder, bg=C["panel"], fg=C["faint"], font=fsm,
                         text="nothing yet — the songs you make appear here,",
                         justify="left", anchor="w")
    empty_lbl.pack(anchor="w")
    tk.Label(songs_holder, bg=C["panel"], fg=C["faint"], font=fsm,
             text="newest first, ready to play.", justify="left",
             anchor="w").pack(anchor="w")

    # ---- right column: SCROLLED, so nothing can be starved when the window is resized ----
    rv_vsb = tk.Scrollbar(rp, orient="vertical")
    rv_vsb.pack(side="right", fill="y")
    rv_canvas = tk.Canvas(rp, bg=C["panel"], highlightthickness=0, width=560,
                          yscrollcommand=rv_vsb.set)
    rv_canvas.pack(side="left", fill="both", expand=True)
    rv_vsb.configure(command=rv_canvas.yview)
    rf = tk.Frame(rv_canvas, bg=C["panel"])
    rv_win = rv_canvas.create_window((0, 0), window=rf, anchor="nw")
    rf.bind("<Configure>", lambda e: rv_canvas.configure(scrollregion=rv_canvas.bbox("all")))
    # ⚠️ the inner frame must be told to match the canvas WIDTH or it collapses to its
    # natural width and the text boxes shrink to nothing.
    rv_canvas.bind("<Configure>", lambda e: rv_canvas.itemconfigure(rv_win, width=e.width))
    # scoped wheel: bind_all would hijack the main window's wheel (documented trap)
    scoped_wheel(win, rv_canvas)     # ⚠️ never bind_all/unbind_all the wheel — see scoped_wheel()

    # ===== 1 · write it ==============================================================
    tk.Label(rf, text=" 1 ", bg=C["rust"], fg="#14110f", font=fh).pack(anchor="w", pady=(6, 0))
    lam(rf, "WRITE IT  — the band and the room, then the words").pack(anchor="w", pady=(2, 2))

    pres_row = tk.Frame(rf, bg=C["panel"])
    pres_row.pack(anchor="w", pady=(2, 2))
    lam(pres_row, "START FROM").pack(side="left")
    pres_var = tk.StringVar(value=_song.STYLE_PRESETS[0][0])
    pres_menu = tk.OptionMenu(pres_row, pres_var, *[n for n, _t in _song.STYLE_PRESETS])
    pres_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust"], activeforeground="#fff",
                        borderwidth=0, width=20, anchor="w")
    pres_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup WHITE
    pres_menu.pack(side="left", padx=6)

    # ---- cover a recording (optional) — the melody comes from the FILE ----
    cov_row = tk.Frame(rf, bg=C["panel"])
    cov_row.pack(anchor="w", pady=(8, 0))
    lam(cov_row, "COVER A RECORDING").pack(side="left")
    btn(cov_row, "PICK A SONG\u2026", lambda: _pick_cover()).pack(side="left", padx=6)
    cov_lbl = tk.Label(cov_row, text="none \u2014 this will be an original", bg=C["panel"],
                       fg=C["faint"], font=fsm)
    cov_lbl.pack(side="left", padx=4)
    btn(cov_row, "CLEAR", lambda: _set_cover(None)).pack(side="left", padx=4)
    lam(rf, "its melody is transcribed and re-sung in your STYLE \u2014 put the words you "
            "want sung in LYRICS", fg=C["faint"], wraplength=520,
        justify="left").pack(anchor="w", pady=(2, 0))

    lam(rf, "STYLE  (%s)" % _song.STYLE_RECIPE, fg=C["faint"],
        wraplength=520, justify="left").pack(anchor="w", pady=(2, 2))
    t_style = tk.Text(rf, height=3, width=62, wrap="word", bg="#100e0c", fg=C["ink"],
                      relief="flat", font=f, padx=6, pady=4, insertbackground=C["ink"])
    t_style.pack(anchor="w")
    t_style.insert("1.0", _song.STYLE_PRESETS[0][1])

    # ---- write the words with a LOCAL model on the rig (no API, no cost) ----
    about_row = tk.Frame(rf, bg=C["panel"])
    about_row.pack(anchor="w", pady=(8, 0))
    lam(about_row, "ABOUT").pack(side="left")
    about_var = tk.StringVar(value="")
    tk.Entry(about_row, textvariable=about_var, width=30, bg="#100e0c", fg=C["ink"],
             relief="flat", font=fsm, insertbackground=C["ink"]).pack(side="left", padx=6)
    write_btn = btn(about_row, "WRITE LYRICS", lambda: _write_lyrics())
    write_btn.pack(side="left", padx=4)
    lam(about_row, "what is it about?", fg=C["faint"]).pack(side="left", padx=6)

    # WHICH local model writes it. Measured 2026-10-09 on this task: both Qwen 27B builds
    # pass the validator first try; Mistral-Small (abliterated) returned ONE prose paragraph
    # with no section tags on both briefs — it ignored the format outright. The picker is
    # here so that judgement can be re-made rather than inherited.
    mrow = tk.Frame(rf, bg=C["panel"])
    mrow.pack(anchor="w", pady=(2, 0))
    lam(mrow, "MODEL").pack(side="left")
    lm_var = tk.StringVar(value=_llm_default)
    lm_menu = tk.OptionMenu(mrow, lm_var, _llm_default)
    lm_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                      highlightthickness=0, activebackground=C["rust"],
                      activeforeground="#fff", borderwidth=0, width=26, anchor="w")
    lm_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    lm_menu.pack(side="left", padx=6)
    lm_note = lam(mrow, "checking the rig\u2026", fg=C["faint"])
    lm_note.pack(side="left", padx=6)

    lam(rf, "LYRICS  (only the words to be sung — leave it empty for an instrumental)",
        fg=C["faint"], wraplength=520, justify="left").pack(anchor="w", pady=(6, 2))
    t_lyr = tk.Text(rf, height=11, width=62, wrap="word", bg="#100e0c", fg=C["ink"],
                    relief="flat", font=f, padx=6, pady=4, insertbackground=C["ink"])
    t_lyr.pack(anchor="w")

    def _load_preset(*_a):
        name = pres_var.get()
        for n, txt in _song.STYLE_PRESETS:
            if n == name:
                t_style.delete("1.0", "end")
                t_style.insert("1.0", txt)
                _refresh_est()
                return

    pres_var.trace_add("write", lambda *a: _load_preset())

    def _insert_tags():
        body_txt = t_lyr.get("1.0", "end").strip()
        if body_txt:
            say("the lyrics box already has words in it — clear it first", warn=True)
            return
        t_lyr.insert("1.0", _song.LYRIC_TEMPLATE)
        _refresh_est()

    tag_row = tk.Frame(rf, bg=C["panel"])
    tag_row.pack(anchor="w", pady=(4, 0))
    btn(tag_row, "SECTION TAGS", _insert_tags).pack(side="left")
    reuse_btn = btn(tag_row, "REUSE WORDS", lambda: _reuse_words())
    reuse_btn.pack(side="left", padx=4)
    reuse_hint = lam(tag_row, "brings back the words you used last \u2014 you should never "
                              "have to type the same lyrics twice", fg=C["faint"],
                     wraplength=300, justify="left")
    reuse_hint.pack(side="left", padx=8)

    # ===== 2 · shape it ==============================================================
    tk.Label(rf, text=" 2 ", bg=C["rust"], fg="#14110f", font=fh).pack(anchor="w", pady=(14, 0))
    lam(rf, "SHAPE IT").pack(anchor="w", pady=(2, 2))

    plan_row = tk.Frame(rf, bg=C["panel"])
    plan_row.pack(anchor="w", pady=(2, 2))
    lam(plan_row, "PLAN").pack(side="left")
    plan_btns = {}
    st["plan"] = "full"

    def _set_plan(k):
        st["plan"] = k
        for kk, b in plan_btns.items():
            _style_toggle(b, kk == k, C)
        _refresh_est()

    for _k, _lab in (("full", "full (melody + chords)"), ("melody", "melody only"),
                     ("off", "off (no plan)")):
        _b = tk.Button(plan_row, text=_lab, bg=C["panel2"], fg=C["ink"],
                       activebackground=C["rust"], activeforeground="#fff", relief="flat",
                       font=fsm, padx=9, pady=2, borderwidth=0, highlightthickness=1,
                       highlightbackground=C["line"])
        _b.pack(side="left", padx=4)
        _b.configure(command=lambda kk=_k: _set_plan(kk))
        plan_btns[_k] = _b
    _style_toggle(plan_btns["full"], True, C)

    dur_row = tk.Frame(rf, bg=C["panel"])
    dur_row.pack(anchor="w", pady=(8, 0))
    lam(dur_row, "LENGTH").pack(side="left")
    dur_var = tk.IntVar(value=int(_song.YUE2_DEFAULT_SECONDS))
    dur_scale = tk.Scale(dur_row, from_=15, to=300, resolution=5, orient="horizontal",
                         variable=dur_var, length=300, bg=C["panel"], fg=C["ink"],
                         troughcolor=C["panel2"], highlightthickness=0, font=fsm,
                         activebackground=C["rust"], showvalue=False,
                         command=lambda *a: _refresh_est())
    dur_scale.pack(side="left", padx=6)
    dur_lbl = tk.Label(dur_row, text="", bg=C["panel"], fg=C["ink"], font=fsm, width=9)
    dur_lbl.pack(side="left")

    seed_row = tk.Frame(rf, bg=C["panel"])
    seed_row.pack(anchor="w", pady=(4, 0))
    lam(seed_row, "SEED").pack(side="left")
    seed_var = tk.StringVar(value="-1")
    tk.Entry(seed_row, textvariable=seed_var, width=14, bg="#100e0c", fg=C["ink"],
             relief="flat", font=fsm, insertbackground=C["ink"]).pack(side="left", padx=6)
    btn(seed_row, "DICE", lambda: seed_var.set(str(random.randint(0, 2**31 - 1)))).pack(side="left")
    lam(seed_row, "-1 rolls a new one each time", fg=C["faint"]).pack(side="left", padx=6)

    # ===== 3 · make it ===============================================================
    tk.Label(rf, text=" 3 ", bg=C["rust"], fg="#14110f", font=fh).pack(anchor="w", pady=(14, 0))
    lam(rf, "MAKE IT").pack(anchor="w", pady=(2, 2))
    make_row = tk.Frame(rf, bg=C["panel"])
    make_row.pack(anchor="w", pady=(2, 0))
    make_btn = btn(make_row, "MAKE THE SONG", lambda: _make(), primary=True)
    make_btn.pack(side="left")
    est_lbl = tk.Label(make_row, text="", bg=C["panel"], fg=C["faint"], font=fsm)
    est_lbl.pack(side="left", padx=10)
    lam(rf, "a song is ~10x faster than it plays: the model writes it, it does not "
            "render it frame by frame", fg=C["faint"], wraplength=520,
        justify="left").pack(anchor="w", pady=(2, 0))

    # ===== 4 · hear it ===============================================================
    tk.Label(rf, text=" 4 ", bg=C["rust"], fg="#14110f", font=fh).pack(anchor="w", pady=(14, 0))
    lam(rf, "HEAR IT").pack(anchor="w", pady=(2, 2))
    hear_row = tk.Frame(rf, bg=C["panel"])
    hear_row.pack(anchor="w", pady=(2, 0))
    play_btn = btn(hear_row, "\u25b6 PLAY", lambda: play(st.get("made")), primary=True)
    play_btn.pack(side="left")
    btn(hear_row, "\u25a0 STOP", stop_play).pack(side="left", padx=4)
    open_btn = btn(hear_row, "OPEN", lambda: _open(st.get("made")))
    open_btn.pack(side="left", padx=4)
    btn(hear_row, "FOLDER", lambda: _open(os.path.dirname(st.get("made") or "") or None)
        ).pack(side="left", padx=4)
    lam(rf, "", fg=C["faint"]).pack(anchor="w", pady=(6, 0))

    def _open(path):
        if not path:
            say("nothing made yet", warn=True)
            return
        try:
            subprocess.Popen(["open", path])
        except Exception as exc:
            say("could not open it: %r" % exc, warn=True)

    def _estimate():
        if st.get("cover"):
            # a cover pays the transcription as well as the generation
            return _song.yue2_cover_estimate_seconds(float(dur_var.get())) + YUE2_SWITCH_S
        plan = st["plan"] != "off"
        t = _song.yue2_estimate_seconds(float(dur_var.get()), plan) + YUE2_SWITCH_S
        # the length the song ACTUALLY lands on is the model's call, so this is an upper
        # bound built from the requested length — label it, do not dress it as exact.
        return t

    def _refresh_est(*_a):
        dur_lbl.configure(text="%d s" % dur_var.get())
        est_lbl.configure(text="\u2248 %.0f s of GPU (upper bound)" % _estimate())

    def _run_line(song):
        secs = song.get("seconds")
        got = audio_seconds(song["path"])
        parts = [song["name"]]
        if got:
            parts.append("%.0f s" % got)
        if secs:
            parts.append("asked %s s" % secs)
        if song.get("cover"):
            parts.append("COVER of %s" % (song.get("source") or "a recording"))
        else:
            parts.append(song.get("plan_label") or "full")
        return "  \u00b7  ".join(parts)

    def _render_songs():
        for w in songs_holder.winfo_children():
            w.destroy()
        if not st["songs"]:
            tk.Label(songs_holder, bg=C["panel"], fg=C["faint"], font=fsm, anchor="w",
                     justify="left", text="nothing yet — the songs you make appear here,"
                     ).pack(anchor="w")
            tk.Label(songs_holder, bg=C["panel"], fg=C["faint"], font=fsm, anchor="w",
                     justify="left", text="newest first, ready to play.").pack(anchor="w")
            return
        for song in st["songs"]:
            row = tk.Frame(songs_holder, bg=C["panel"], highlightthickness=1,
                           highlightbackground=C["line"])
            row.pack(fill="x", pady=3)
            rr = tk.Frame(row, bg=C["panel"])
            rr.pack(side="right", padx=6, pady=4)
            tk.Button(rr, text="\u25b6", bg=C["panel2"], fg=C["ink"],
                      activebackground=C["rust"], activeforeground="#fff", relief="flat",
                      font=fsm, padx=8, pady=1, borderwidth=0,
                      command=lambda s=song: play(s["path"])).pack(side="left")
            tk.Button(rr, text="\u2937", bg=C["panel2"], fg=C["ink"],
                      activebackground=C["rust"], activeforeground="#fff", relief="flat",
                      font=fsm, padx=8, pady=1, borderwidth=0,
                      command=lambda s=song: _open(s["path"])).pack(side="left", padx=3)
            if song.get("score"):
                # the transcribed melody, saved beside the audio — proof of what it followed
                tk.Button(rr, text="SCORE", bg=C["panel2"], fg=C["ink"],
                          activebackground=C["rust"], activeforeground="#fff", relief="flat",
                          font=fsm, padx=6, pady=1, borderwidth=0,
                          command=lambda s=song: _open(s["score"])).pack(side="left", padx=3)
            if song.get("lyrics") or song.get("style"):
                # ⚠️ THE point of this button: to cover or re-roll a song you made earlier you
                # need ITS words, and they are already in the take — retyping them was never
                # necessary, the app just never offered them back.
                tk.Button(rr, text="WORDS", bg=C["panel2"], fg=C["ink"],
                          activebackground=C["rust"], activeforeground="#fff", relief="flat",
                          font=fsm, padx=6, pady=1, borderwidth=0,
                          command=lambda s=song: _load_words(s)).pack(side="left", padx=3)
            tk.Label(row, text=_run_line(song), bg=C["panel"], fg=C["ink"], font=fsm,
                     justify="left", anchor="w", wraplength=330).pack(side="left", padx=8, pady=4)

    def _fetch_models():
        """Chat models on the rig's Ollama. Never raises: no list just means no choice."""
        import urllib.request
        try:
            import director as _d
            url = _d.DEFAULT_URL.rstrip("/") + "/api/tags"
            with urllib.request.urlopen(url, timeout=6) as r:
                ms = json.load(r).get("models") or []
            names = [m.get("name") for m in ms if m.get("name")]
            # the embeddings are not chat models and cannot write anything
            names = [n for n in names
                     if not any(k in n.lower() for k in ("embed", "bge-", "nomic"))]
            if _llm_default not in names:
                names.insert(0, _llm_default)
            return names
        except Exception as exc:
            _log_err("could not list the rig's models: %r" % exc)
            return []

    def _apply_models(names):
        if not names:
            lm_note.configure(text="could not reach the rig's models \u2014 using the default")
            return
        m = lm_menu["menu"]
        m.delete(0, "end")
        for n in names:
            m.add_command(label=n, command=lambda v=n: lm_var.set(v))
        lm_var.set(_llm_default if _llm_default in names else names[0])
        lm_note.configure(text="%d local model%s" % (len(names), "" if len(names) == 1 else "s"))

    _lm = {"names": None}

    def _models_worker():
        # ⚠️ Only writes a dict slot; the Tk objects are touched from the polling callback.
        # Calling win.after() from this thread would be a cross-thread Tk call.
        _lm["names"] = _fetch_models() or []

    def _poll_models():
        if _lm["names"] is None:
            win.after(400, _poll_models)
            return
        _apply_models(_lm["names"])

    threading.Thread(target=_models_worker, daemon=True).start()
    win.after(400, _poll_models)

    def _write_lyrics():
        """Ask the rig's own model for the words. Runs on a WORKER THREAD — a 27B takes ~10-25 s
        and a synchronous call would freeze the window for the whole time.

        Uses the app's existing Ollama client (director.py, forge:11434). ⚠️ That model lives
        on GPU1 while a song renders on GPU0, so writing words and rendering do not collide —
        but the LLM must not be left resident if a 2-CARD Raylight video run is coming.
        """
        if st.get("writing"):
            say("still writing \u2014 give it a moment", warn=True)
            return
        brief = about_var.get().strip()
        style = t_style.get("1.0", "end").strip()
        if not brief and not style:
            say("say what it is ABOUT (or at least set a STYLE) and I will write it", warn=True)
            return
        job = {"done": False, "out": None, "err": None, "t0": time.time()}

        def work():
            try:
                import director as _d
                job["out"] = _d.Director(model=lm_var.get()).lyrics(
                    brief, style=style, seconds=float(dur_var.get()))
            except Exception as exc:
                job["err"] = repr(exc)
            finally:
                job["done"] = True

        st["writing"] = True
        write_btn.configure(state="disabled")
        say("writing the words on the rig\u2026")
        threading.Thread(target=work, daemon=True).start()
        win.after(700, lambda: _poll_lyrics(job))

    def _poll_lyrics(job):
        if not job["done"]:
            say("writing the words\u2026 %.0f s" % (time.time() - job["t0"]))
            win.after(900, lambda: _poll_lyrics(job))
            return
        st["writing"] = False
        write_btn.configure(state="normal")
        if job["err"]:
            say("the model could not write it: %s" % job["err"], warn=True)
            return
        out = job["out"] or {}
        _put_words(None, out.get("lyrics"))
        _refresh_est()
        say("wrote \u201c%s\u201d \u2014 press again for another (%d chars)"
            % (out.get("title") or "a song", len(out.get("lyrics") or "")))

    def _put_words(style, lyrics):
        if style:
            t_style.delete("1.0", "end")
            t_style.insert("1.0", style)
        if lyrics:
            t_lyr.delete("1.0", "end")
            t_lyr.insert("1.0", lyrics)
        _save_state()

    def _reuse_words():
        """The words of the song being COVERED when a cover is armed, else of your last song.

        ⚠️ The cover case is NOT the same question as the last-song case, and answering the
        wrong one would restore the wrong lyrics into a cover — which is exactly what Ryan
        caught: "not for the last song for the cover song".
        """
        if st.get("cover"):
            it = words_of_recording(st["cover"])
            if it:
                _put_words(it.get("style"), it.get("lyrics"))
                _refresh_est()
                say("words restored from the song you are covering (%s)"
                    % os.path.basename(st["cover"]))
            elif not t_lyr.get("1.0", "end").strip():
                say("that recording is not one of ours, so I have no words for it \u2014 "
                    "type or paste them once and they will stick", warn=True)
            else:
                say("that recording is not one of ours \u2014 keeping the words you have",
                    warn=True)
            return
        it = last_song_take()
        if not it:
            say("no saved words yet \u2014 type them once and they stick", warn=True)
            return
        _put_words(it.get("style"), it.get("lyrics"))
        _refresh_est()
        say("words restored from your last song (%s)"
            % (it.get("source") or it.get("label") or "song"))

    def _load_words(song):
        _put_words(song.get("style"), song.get("lyrics"))
        say("words loaded from %s" % song.get("name"))

    def _restore_words():
        """Repaint the boxes from last launch — the reason there is nothing to retype."""
        s = _load_state()
        if s.get("style") or s.get("lyrics"):
            _put_words(s.get("style"), s.get("lyrics"))
        if s.get("seconds"):
            dur_var.set(int(s["seconds"]))
        if s.get("plan") in ("full", "melody", "off"):
            _set_plan(s["plan"])
        # a cover is only re-armed when the recording is still on disk
        if s.get("cover") and os.path.exists(s["cover"]):
            _set_cover(s["cover"])
        _refresh_est()

    def _set_cover(path):
        """A cover changes the SHAPE of the run, not just a field: the melody comes from a
        FILE, so the plan toggles are locked to melody (the model's own recommendation for
        covers) and the length defaults to the recording's own length. Locking them beats
        letting him set a combination the cover graph cannot honour."""
        st["cover"] = path or None
        if path:
            cov_lbl.configure(text=os.path.basename(path), fg=C["ink"])
            _set_plan("melody")
            for _k, _b in plan_btns.items():
                if _k != "melody":
                    _b.configure(state="disabled", disabledforeground=C["dim"])
            secs = audio_seconds(path)
            if secs:
                dur_var.set(int(max(15, min(300, round(secs)))))
            make_btn.configure(text="MAKE THE COVER")
            reuse_btn.configure(text="COVER'S WORDS")
            reuse_hint.configure(text="brings back the words of the song you are covering")
            # ⚠️ Fill them ONLY when the lyric box is empty. Overwriting words he has already
            # written would be the app deciding for him — and for a cover the words are often
            # NEW words cut to the old melody, which is the whole point of the workflow.
            _cov = words_of_recording(path)
            if _cov and not t_lyr.get("1.0", "end").strip():
                _put_words(_cov.get("style"), _cov.get("lyrics"))
                say("cover mode \u2014 words loaded from %s; edit them freely"
                    % os.path.basename(path))
            elif _cov:
                say("cover mode \u2014 its words are one press away (COVER'S WORDS)")
            else:
                say("cover mode \u2014 that recording is not one of ours, so the words are "
                    "yours to supply")
        else:
            cov_lbl.configure(text="none \u2014 this will be an original", fg=C["faint"])
            for _b in plan_btns.values():
                _b.configure(state="normal")
            make_btn.configure(text="MAKE THE SONG")
            reuse_btn.configure(text="REUSE WORDS")
            reuse_hint.configure(text="brings back the words you used last \u2014 you should "
                                      "never have to type the same lyrics twice")
        _refresh_est()

    def _pick_cover():
        from tkinter import filedialog as _fd
        p = _fd.askopenfilename(
            title="Pick a recording to cover",
            filetypes=[("Audio", "*.flac *.wav *.mp3 *.m4a *.aiff *.aif *.ogg"),
                       ("All files", "*.*")])
        if p:
            _set_cover(p)
            say("cover mode \u2014 the melody comes from %s"
                % os.path.basename(p))

    def _make():
        if st["busy"]:
            say("already making one — it is about 20 s", warn=True)
            return
        style = t_style.get("1.0", "end").strip()
        lyrics = t_lyr.get("1.0", "end").strip()
        if not style:
            say("give it a STYLE first — that is the band and the room", warn=True)
            return
        cover = st.get("cover")
        if cover:
            p = _song.yue2_cover_params(style, lyrics, cover,
                                        seconds=float(dur_var.get()),
                                        seed=seed_var.get().strip() or "-1",
                                        prefix="covers/yue2")
        else:
            plan = st["plan"] != "off"
            p = _song.yue2_plan_params(style, lyrics, mode=(st["plan"] if plan else "full"),
                                       seconds=float(dur_var.get()),
                                       seed=seed_var.get().strip() or "-1", plan=plan)
        _save_state()          # a submitted run's words are worth keeping
        try:
            pid, seed = submit("song", p, None)
        except Exception as exc:
            say("forge refused it: %r" % exc, warn=True)
            return
        st.update({"busy": True, "pid": pid, "seed": seed, "t0": time.time(),
                   "was_cover": bool(cover)})
        seed_var.set(str(seed))
        make_btn.configure(state="disabled")
        say("making the cover\u2026 (seed %s, melody from %s)"
            % (seed, os.path.basename(cover)) if cover
            else "making it\u2026 (seed %s, %s)" % (seed, st["plan"]))
        win.after(1200, _poll)

    def _poll():
        """Poll the run the way every other workspace does — run_state() then collect(),
        so the take lands in runs/<pid>/ AND in the index like any other run."""
        if not st.get("busy"):
            return
        pid = st["pid"]
        try:
            rs = run_state(pid) or {}
        except Exception as exc:
            rs = {"state": "unknown", "detail": repr(exc)}
        state = rs.get("state")
        if state in ("running", "queued"):
            el = time.time() - st.get("t0", time.time())
            say("%s\u2026 %.0f s" % ("running" if state == "running" else "queued", el))
            win.after(1500, _poll)
            return
        st["busy"] = False
        make_btn.configure(state="normal")
        if state == "error":
            say("forge error: %s" % (rs.get("detail") or "see the console"), warn=True)
            return
        try:
            meta = collect(pid) or {}
        except Exception as exc:
            meta = {}
            say("could not pull the result: %r" % exc, warn=True)
        files = [x for x in (meta.get("images") or [])
                 if (x.get("file") or "").lower().endswith((".flac", ".wav", ".mp3", ".ogg"))]
        if not files:
            say("the run finished but no audio came back — check it in Takes", warn=True)
            return
        path = os.path.join(RUNS, meta.get("id") or pid, files[0]["file"])
        rid = meta.get("id") or pid
        _score = next((f for f in (meta.get("text_files") or []) if f.endswith(".txt")), None)
        song = {"path": path, "name": files[0]["file"], "id": rid,
                "seconds": meta.get("seconds"), "seed": meta.get("seed"),
                # the words ride with the take, so any row can put them back
                "style": meta.get("style"), "lyrics": meta.get("lyrics"),
                "cover": bool(meta.get("cover")), "source": meta.get("source"),
                "score": (os.path.join(RUNS, rid, _score) if _score else None),
                "plan_label": (meta.get("song_mode") or "full") if meta.get("plan") else "off"}
        st["songs"].insert(0, song)
        st["made"] = path
        _render_songs()
        got = audio_seconds(path)
        say("done%s \u2014 press PLAY" % (" \u00b7 %.0f s of song" % got if got else ""))

    # initial paint + the driver's hooks
    _refresh_est()
    _restore_words()          # the words come back with the window, so nothing is retyped

    def _set(style=None, lyrics=None, plan=None, seconds=None):
        """Drive the window from a test (or a future headless caller) without poking at
        widgets: the same shape as the long_* accessors the other workspaces export."""
        if style is not None:
            t_style.delete("1.0", "end")
            t_style.insert("1.0", style)
        if lyrics is not None:
            t_lyr.delete("1.0", "end")
            t_lyr.insert("1.0", lyrics)
        if plan is not None:
            _set_plan(plan)
        if seconds is not None:
            dur_var.set(int(seconds))
        _refresh_est()

    win.song_dump = lambda: {
        "plan": st["plan"], "duration": dur_var.get(), "seed": seed_var.get(),
        "style": t_style.get("1.0", "end").strip(),
        "lyrics_len": len(t_lyr.get("1.0", "end").strip()),
        "estimate": round(_estimate(), 1), "busy": st["busy"],
        "songs": len(st["songs"]), "made": st.get("made"),
        "status": status.cget("text"), "make_state": str(make_btn.cget("state")),
        "make_mapped": bool(make_btn.winfo_ismapped()),
        "estimate_text": est_lbl.cget("text"),
        "plan_labels": [b.cget("text") for b in plan_btns.values()],
        "presets": [n for n, _t in _song.STYLE_PRESETS],
        "canvas_scrolls": bool(rv_canvas.cget("scrollregion")),
        "cover": st.get("cover"), "make_text": make_btn.cget("text"),
        "words_chars": len(t_style.get("1.0", "end").strip())
        + len(t_lyr.get("1.0", "end").strip()),
        "reuse_text": reuse_btn.cget("text"),
        "about": about_var.get(), "writing": bool(st.get("writing")),
        "llm": lm_var.get(), "llm_note": lm_note.cget("text"),
        "write_state": str(write_btn.cget("state")),
        "write_mapped": bool(write_btn.winfo_ismapped()),
        "cover_label": cov_lbl.cget("text"),
        "plan_disabled": sum(1 for b in plan_btns.values() if str(b.cget("state")) == "disabled"),
    }
    win.song_set = _set                # drive it without poking widgets
    win.song_set_cover = _set_cover    # the cover picker, without opening a file dialog
    win.song_reuse_words = _reuse_words   # the REUSE WORDS button, callable from a test
    win.song_load_words = _load_words
    win.song_words_of_recording = words_of_recording
    win.song_write_lyrics = _write_lyrics      # the WRITE LYRICS button
    win.song_about_var = about_var
    win.song_llm_var = lm_var
    win.song_lyrics_text = lambda: t_lyr.get("1.0", "end").strip()

    def _llm_choices():
        m = lm_menu["menu"]
        try:
            return [m.entrycget(i, "label") for i in range(m.index("end") + 1)]
        except Exception:
            return []
    win.song_llm_choices = _llm_choices
    win.song_writing = lambda: bool(st.get("writing"))
    win.song_save_words = _save_state      # so a test can prove the round-trip to disk
    win.song_make = _make              # the real handler, so a driver can run it headless-ish
    win.song_play = lambda: play(st.get("made"))
    win.song_stop = stop_play
    win.song_lines = songs_holder

    if btn_colors:
        try:
            btn_colors(win)
        except Exception:
            pass
    # re-assert the PLAN toggles after the blanket repaint (house rule: _btn_colors repaints
    # every button, which would otherwise leave the selected plan looking OFF)
    _set_plan(st["plan"])

    win.bind("<Escape>", lambda _e: (_save_state(), stop_play(), win.destroy()))
    win.protocol("WM_DELETE_WINDOW", lambda: (_save_state(), stop_play(), win.destroy()))
    return win

def launch():
    import tkinter as tk
    from tkinter import filedialog, font as tkfont

    C = {"bg": "#14110f", "panel": "#1b1815", "panel2": "#211d19", "line": "#312b25",
         "ink": "#e9e3da", "muted": "#9b9187", "faint": "#6d655c",
         "rust": "#c25e33", "moss": "#7c8c62", "ochre": "#c9a24a",
         # Measured against the real backgrounds (WCAG ratio, not eyeballed):
         #   white on #c25e33 was only 4.25:1 -> primary fills use rust_deep 6.63:1
         #   faint on panel2 was 2.92:1 (a FAIL) -> utility labels use ink 13.1:1
         #   dim = legible but visibly INACTIVE for disabled buttons, 4.33:1
         "rust_deep": "#9c4020", "rust_hi": "#b05329", "dim": "#8a8076"}
    q = queue.Queue()

    def _btn_colors(w):
        """macOS does NOT reliably honour a custom -background on Tk buttons (the Aqua
        button cap is drawn light), so LIGHT text on a dark fill renders pale-on-pale and
        is unreadable — reported exactly that way for POLISH PROMPT, even though the
        configured pair measures 13:1. Paint buttons LIGHT with DARK text so they read
        under either rendering. Ochre (warnings) is already light and keeps dark text.

        Defined EARLY on purpose: the takes list is built at kickoff and calls this, so
        it must exist before the UI finishes constructing."""
        for c in w.winfo_children():
            if c.winfo_class() in ("Button", "Menubutton"):
                try:
                    if str(c.cget("bg")).lower() != C["ochre"].lower():
                        c.configure(bg=C["ink"])
                    c.configure(fg="#14110f", disabledforeground=C["faint"],
                                activebackground=C["ochre"],
                                activeforeground="#14110f")
                except Exception:
                    pass
            _btn_colors(c)

    def _dbg(msg):
        """Scroll/key diagnostics, OFF by default. Run with FORGE_LAB_DEBUG=1 to
        log to ~/forge-lab/wheel_debug.log — this is how the Tk 9 Δx/Δy delta
        packing bug was found, so it is worth keeping around."""
        if os.environ.get("FORGE_LAB_DEBUG") != "1":
            return
        try:
            path = os.path.join(DATA, "wheel_debug.log")
            if os.path.exists(path) and os.path.getsize(path) > 80000:
                return
            with open(path, "a") as f:
                f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass
    st = {"mode": "t2i", "refs": [], "video_mode": "r2v", "wan_mode": WAN_VARIANT_DEFAULT,
          "warm_engine": None,
          "est": None, "take": None, "active": {}, "focus": None, "seed_used": None}

    root = tk.Tk()
    root.title("Forge Lab")

    def _log_exc(exc, val, tb):
        """A windowed .app has no console, so any exception inside an after()
        callback vanished silently. Write it somewhere we can actually read.

        ⚠️ Under a DRIVER this must also STOP the run. Tk catches callback exceptions and keeps
        looping, so a test that raises just sits there until its timeout — a hang reads as
        "slow", not as "failed", and that is the worst way for a test to break.
        """
        import traceback
        try:
            with open(os.path.join(DATA, "forge_lab_errors.log"), "a") as f:
                f.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                traceback.print_exception(exc, val, tb, file=f)
        except Exception:
            pass
        if _DRIVER:
            print("\n!! TK CALLBACK EXCEPTION — failing the driver instead of hanging:", flush=True)
            traceback.print_exception(exc, val, tb)
            os._exit(3)
    root.report_callback_exception = _log_exc
    root.configure(bg=C["bg"])
    root.geometry("1120x760")
    root.minsize(940, 640)

    def F(size, weight="normal", mono=False, serif=False):
        fam = (("Menlo",) if mono else
               (("Iowan Old Style", "Palatino", "Georgia") if serif else ("Helvetica Neue",)))
        return tkfont.Font(family=fam[0], size=size, weight=weight)

    fword = F(19, serif=True)
    fh = F(11, "bold")
    f = F(12)
    fsm = F(10)
    fmono = F(11, mono=True)
    fmonos = F(10, mono=True)

    def lab(parent, text, fnt=None, fg=None):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["muted"],
                        font=fnt or fsm, anchor="w")

    # ---- header
    head = tk.Frame(root, bg=C["bg"])
    head.pack(fill="x", padx=20, pady=(16, 0))
    title = tk.Frame(head, bg=C["bg"])
    title.pack(side="left")
    tk.Label(title, text="FORGE LAB", bg=C["bg"], fg=C["ink"], font=fword).pack(anchor="w")
    tk.Label(title, text="local front end for the forge rig  ·  estimate before spend",
             bg=C["bg"], fg=C["muted"], font=fsm).pack(anchor="w")
    status = tk.Frame(head, bg=C["bg"])
    status.pack(side="right")
    dot = tk.Label(status, text="●", bg=C["bg"], fg=C["muted"], font=F(11))
    dot.pack(anchor="e", side="left", padx=(0, 6))
    stxt = tk.Label(status, text="checking forge…", bg=C["bg"], fg=C["muted"], font=fmonos)
    stxt.pack(side="left", anchor="e")
    gtxt = tk.Label(head, text="", bg=C["bg"], fg=C["muted"], font=fmonos)
    gtxt.pack(side="right", padx=(0, 16))
    ws_txt = tk.Label(head, text="", bg=C["bg"], fg=C["muted"], font=fmonos)
    ws_txt.pack(side="right", padx=(0, 16))
    tk.Frame(root, bg=C["line"], height=1).pack(fill="x", padx=20, pady=(14, 0))

    body = tk.Frame(root, bg=C["bg"])
    body.pack(fill="both", expand=True, padx=20, pady=14)
    # The left column SCROLLS. As a plain fixed frame, anything past the bottom
    # edge of the window was unreachable — and the reference picker was pushed
    # there by a placeholder label requesting ~1260x1530 px (a text-mode Label
    # with width=180, height=90 means 180 CHARACTERS x 90 LINES, not pixels).
    left_outer = tk.Frame(body, bg=C["panel"], width=420)
    left_outer.pack(side="left", fill="y")
    left_outer.pack_propagate(False)
    lcanvas = tk.Canvas(left_outer, bg=C["panel"], highlightthickness=0, width=402)
    lvsb = tk.Scrollbar(left_outer, orient="vertical", command=lcanvas.yview,
                        troughcolor=C["panel"], bg=C["panel2"], borderwidth=0)
    lcanvas.configure(yscrollcommand=lvsb.set)
    lvsb.pack(side="right", fill="y")
    lcanvas.pack(side="left", fill="both", expand=True)
    left = tk.Frame(lcanvas, bg=C["panel"])
    lcanvas.create_window((0, 0), window=left, anchor="nw", width=396)
    def _sr_left(e=None):
        before = lcanvas.yview()
        bbox = lcanvas.bbox("all")
        lcanvas.configure(scrollregion=bbox)
        after = lcanvas.yview()
        if before != after:
            _dbg("LEFT Configure bbox=%s yview %s -> %s   <<< VIEW MOVED BY LAYOUT"
                 % (tuple(bbox) if bbox else bbox, tuple(round(v,3) for v in before),
                    tuple(round(v,3) for v in after)))
    left.bind("<Configure>", _sr_left)

    # yscrollincrement=1 so a "unit" IS a pixel: trackpad deltas are pixel travel
    # (fixed-point 16.16), and mapping them to 14px units made every event a jump.
    lcanvas.configure(yscrollincrement=1)

    _wheel_hits = {"n": 0}

    def _wheel_debug(ev, node, target, src="-uncalled-"):
        """TEMPORARY: record what the app actually receives, so a scroll that
        does nothing can be diagnosed from evidence instead of guesses."""
        if _wheel_hits["n"] >= 80:
            return
        _wheel_hits["n"] += 1
        try:
            chain = []
            n = node
            while n is not None and len(chain) < 6:
                chain.append(n.winfo_class())
                n = getattr(n, "master", None)
            with open(os.path.join(DATA, "wheel_debug.log"), "a") as f:
                f.write("src=%-16s delta=%r  x_root=%r y_root=%r  under=%s  chain=%s  scroll_target=%s\n"
                        % (src, getattr(ev, "delta", "<none>"), getattr(ev, "x_root", None),
                           getattr(ev, "y_root", None),
                           node.winfo_class() if node is not None else None,
                           ">".join(chain) or "-",
                           "yes" if target is not None else "NO"))
        except Exception:
            pass

    _acc = {}                      # pending pixel deltas per scrollable panel
    _acc_sched = {"on": False}

    def _flush_scroll():
        """Apply the accumulated movement ONCE, on the next idle cycle.

        Scrolling directly on every event meant ~130 canvas redraws per second,
        which is what looked glitchy. Batching keeps the pixel-accurate total but
        repaints once per frame."""
        _acc_sched["on"] = False
        for key, cvs in (("left", lcanvas), ("takes", tcanvas)):
            d = _acc.get(key, 0.0)
            whole = int(d)                     # truncate; keep the fraction
            if whole == 0:
                _acc[key] = d
                continue
            _acc[key] = d - whole              # carry the remainder to next frame
            try:
                before = cvs.yview()
                cvs.yview_scroll(whole, "units")   # yscrollincrement=1, so units == px
                after = cvs.yview()
                _dbg("FLUSH %-6s px=%+d yview %s -> %s"
                     % (key, whole, tuple(round(v,3) for v in before),
                        tuple(round(v,3) for v in after)))
            except Exception:
                continue

    def _wheel(ev, src="all-tag"):
        """One global wheel handler that routes by what is under the pointer."""
        try:
            node = root.winfo_containing(ev.x_root, ev.y_root)
        except Exception:
            node = None
        target = None
        walker = node
        while walker is not None:
            if isinstance(walker, tk.Text):
                _wheel_debug(ev, node, None, src)   # the prompt scrolls itself
                return
            if walker is lcanvas or walker is tcanvas:
                target = walker
                break
            walker = getattr(walker, "master", None)
        _wheel_debug(ev, node, target, src)
        if target is None:
            return
        # Delta units differ wildly by source. On this Tk build a macOS TRACKPAD
        # reports RAW FIXED-POINT values (~131060 for one notch = 2 * 65536).
        # The old "if > 10 divide by 10" clamp left ~1310 increments per event,
        # which slammed the view to an extreme — that was the "jumps back to the
        # top". Normalise to a small increment count instead.
        # TIP 684 decode (same as tk::PreciseScrollDeltas): Δx is the HIGH 16 bits,
        # Δy the LOW 16 bits, both signed. Dividing by 65536 (my earlier attempt)
        # read Δx as the magnitude and ignored Δy entirely, which is why BOTH the
        # speed and the direction were wrong.
        try:
            d = int(getattr(ev, "delta", 0) or 0)
        except Exception:
            d = 0
        if not d:
            return
        if src == "touchpad":
            low = d & 0xFFFF
            dy = low - 0x10000 if low >= 0x8000 else low
        else:
            dy = int(round(d / 3.0))               # <MouseWheel>: normalised to ±120/notch
        if not dy:
            return
        # Tk's own Text binding scrolls by "-deltaY pixels". Canvas has no "pixels"
        # mode, so yscrollincrement=1 makes one "unit" exactly one pixel.
        step_px = max(-240, min(240, -dy))
        key = "left" if target is lcanvas else "takes"
        _acc[key] = _acc.get(key, 0.0) + step_px
        if not _acc_sched["on"]:
            _acc_sched["on"] = True
            root.after_idle(_flush_scroll)
    right = tk.Frame(body, bg=C["bg"])
    right.pack(side="left", fill="both", expand=True, padx=(16, 0))

    # ---- mode selector
    tk.Label(left, text="1 · WHAT ARE WE MAKING", bg=C["panel"], fg=C["muted"],
             font=fh).pack(anchor="w", padx=16, pady=(16, 8))
    mode_row = tk.Frame(left, bg=C["panel"])
    mode_row.pack(fill="x", padx=16)
    mode_btns = {}          # kept for the layout self-test

    # ---- 1 · KIND then MODEL. KIND is Image|Video; MODEL lists that KIND's models in one
    # flat set, with the ENGINE named in the label for video ("H3 · …" / "WAN 2.2 · …"),
    # because "first frame" exists on both and picking the wrong one is invisible until you
    # look at the result. Two rows, not one: KIND + a 31-character model label on a single
    # 364 px row runs off the right edge, and a clipped control still "works".
    # ⚠️ "TYPE", not "KIND" — plain English for the same thing. The registry still calls it `kind`
    # everywhere (kind_of, SIZE_DEFAULT_BY_KIND); only the label the user READS changed.
    lab(mode_row, "TYPE").pack(side="left")
    kind_var = tk.StringVar(value="Image")
    kind_menu = tk.OptionMenu(mode_row, kind_var, "Image", "Video", "Long video", "Character",
                              "Weirdo", "Studio", "Song", "Dialogue", command=lambda *_: on_kind())
    kind_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust"],
                        activeforeground="#fff", borderwidth=0, width=7,
                        highlightbackground=C["panel"])
    kind_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup panel WHITE and ignores -background: light text is invisible there
    kind_menu.pack(side="left", padx=(6, 0))

    model_row = tk.Frame(left, bg=C["panel"])
    model_row.pack(fill="x", padx=16, pady=(6, 0))
    lab(model_row, "MODEL").pack(side="left")
    model_var = tk.StringVar(value=MODEL_CHOICES["Image"][0][2])
    model_menu = tk.OptionMenu(model_row, model_var,
                               *[c[2] for c in MODEL_CHOICES["Image"]],
                               command=lambda *_: on_model())
    model_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                         highlightthickness=0, activebackground=C["rust"],
                         activeforeground="#fff", borderwidth=0, width=31,
                         highlightbackground=C["panel"])
    model_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup panel WHITE and ignores -background: light text is invisible there
    model_menu.pack(side="left", padx=6)

    # ---- video-only parameters, immediately under the mode pickers
    vid_row = tk.Frame(left, bg=C["panel"])
    lab(vid_row, "DURATION").pack(side="left")
    e_secs = tk.Entry(vid_row, width=4, bg="#100e0c", fg=C["ink"], relief="flat",
                      font=fmono, insertbackground=C["ink"])
    e_secs.insert(0, "3")
    e_secs.pack(side="left", padx=(6, 4))
    lab(vid_row, "s").pack(side="left")
    lab(vid_row, "SIZE").pack(side="left", padx=(14, 0))
    size_var = tk.StringVar(value=VIDEO_SIZE_DEFAULT)
    size_menu = tk.OptionMenu(vid_row, size_var, *VIDEO_SIZES.keys(),
                              command=lambda *_: apply_vsize())
    size_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust_deep"],
                        activeforeground="#fff", borderwidth=0, width=17,
                        highlightbackground=C["panel"])
    size_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup panel WHITE and ignores -background: light text is invisible there
    size_menu.pack(side="left", padx=6)
    vid_row.pack(fill="x", padx=16, pady=(8, 0))

    # the hint gets its OWN line: these notes are long, and sharing a row with the
    # duration + size controls pushed the text past the column width (a clipped
    # control is invisible while every callback still works)
    vid_hint = lab(left, "", fg=C["muted"])
    vid_hint.configure(wraplength=366, justify="left")
    vid_hint.pack(anchor="w", padx=16, pady=(4, 0))

    def apply_vsize():
        """Canvas from the SIZE dropdown. Native 1344x768 is what every shipped H3
        template uses; the node takes any multiple of 32 but VRAM scales with pixels."""
        name = size_var.get()
        # ⚠️ NOT VIDEO_SIZES alone with a VIDEO_SIZE_DEFAULT fallback. LTX's and Animate 2's
        # canvases live in their OWN tables, so this fallback silently rewrote every switch to
        # H3's 1344x768 AFTER the SIZE label had been set: the dropdown read 480x848 while the
        # canvas actually submitted was 1344x768. Two consequences, both real: the estimate was
        # 2.54x high (Animate 2 quoted ~7m24s against a MEASURED 181 s), and the render would have
        # used a canvas no run has ever been timed at — 1344x768 passes the %16 check, so nothing
        # would have refused it. Look the label up in EVERY known table.
        _tab = _size_choices(size_key(st.get("mode"), st.get("wan_mode")))
        w, h = (_tab.get(name) or VIDEO_SIZES.get(name) or LTX_SIZES.get(name)
                or ANIMATE2_SIZES.get(name) or VIDEO_SIZES[VIDEO_SIZE_DEFAULT])
        e_w.delete(0, "end"); e_w.insert(0, str(w))
        e_h.delete(0, "end"); e_h.insert(0, str(h))
        vid_hint.configure(text=((WAN_SIZE_NOTES if MODES[st["mode"]].get("wan")
                                  else VIDEO_SIZE_NOTES).get(name, "")))
        _plog("video size -> %s (%dx%d)" % (name, w, h))
        refresh_estimate()

    vid_row_lora = tk.Frame(left, bg=C["panel"])
    lora_lbl = lab(vid_row_lora, "LORA")
    lora_lbl.pack(side="left")
    lora_var = tk.StringVar(value="(none)")
    lora_menu = tk.OptionMenu(vid_row_lora, lora_var, "(none)")
    lora_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                        highlightthickness=0, activebackground=C["rust"],
                        activeforeground="#fff", borderwidth=0, width=17,
                        highlightbackground=C["panel"])
    lora_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup panel WHITE and ignores -background: light text is invisible there
    lora_menu.pack(side="left", padx=(6, 8))
    lab(vid_row_lora, "strength").pack(side="left")
    e_lstr = tk.Entry(vid_row_lora, width=4, bg="#100e0c", fg=C["ink"], relief="flat",
                      font=fmono, insertbackground=C["ink"])
    e_lstr.insert(0, "1.0")
    e_lstr.pack(side="left", padx=6)
    vid_row_lora.pack(fill="x", padx=16, pady=(4, 0))

    # H3 generates NATIVE synchronized audio, and until now there was no way to ask for a
    # silent clip. Note the cost model: the AV latent is joint either way, so this changes
    # the FILE, not the render time — the audio decode is milliseconds against minutes.
    vid_row_audio = tk.Frame(left, bg=C["panel"])
    audio_lbl = lab(vid_row_audio, "AUDIO")
    audio_lbl.pack(side="left")
    audio_var = tk.StringVar(value="on")
    audio_menu = tk.OptionMenu(vid_row_audio, audio_var, "on", "off",
                               command=lambda *_: apply_audio())
    audio_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                         highlightthickness=0, activebackground=C["rust"],
                         activeforeground="#fff", borderwidth=0, width=5,
                         highlightbackground=C["panel"])
    audio_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup panel WHITE and ignores -background: light text is invisible there
    audio_menu.pack(side="left", padx=(6, 10))
    audio_note = lab(vid_row_audio, "", fg=C["muted"])
    audio_note.configure(wraplength=250, justify="left")
    audio_note.pack(side="left")
    vid_row_audio.pack(fill="x", padx=16, pady=(4, 0))

    def apply_audio(*_):
        audio_note.configure(text=(
            "H3 native stereo, synchronized to the shot"
            if audio_var.get() == "on" else
            "silent clip — the audio decode is left out of the graph"))
        refresh_estimate()
    # NOTE: do NOT call apply_audio() here. It touches refresh_estimate(), which is not
    # defined until further down launch() — calling it now raises NameError at startup
    # (Tk swallows it inside callbacks, but at construction it takes the whole app down).
    # set_mode() calls it during kick-off, so the note fills in before the window is usable.

    # ---- CARD MODE: Solo / Turbo / Studio — ONE three-way switch --------------------------------
    # Turbo = Raylight transforms the H3 graph to run ONE render across BOTH 3090s.
    # Studio = the render stays on card 0 and the DIRECTOR (Ollama, pinned to GPU1) is kept warm
    #          on card 2 so POLISH PROMPT can use it.
    # They are mutually exclusive on card 2, so this is ONE choice, not two independent switches
    # (two switches could be set to a state the rig cannot honour, and the failure was quiet).
    # ⚠️ The same SEED does NOT reproduce across card counts (measured 22.0 dB PSNR between
    # 1-card and 2-card) — Turbo is a choice you make BEFORE a take, not a speed-up of one you like.
    vid_row_cards = tk.Frame(left, bg=C["panel"])
    lab(vid_row_cards, "CARD MODE").pack(side="left")
    cards_var = tk.StringVar(value="Solo")
    cards_menu = tk.OptionMenu(vid_row_cards, cards_var, "Solo", "Turbo", "Studio",
                               command=lambda *_: apply_cards())
    cards_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                         highlightthickness=0, activebackground=C["rust"],
                         activeforeground="#fff", borderwidth=0, width=7,
                         highlightbackground=C["panel"])
    cards_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup panel WHITE
    cards_menu.pack(side="left", padx=(6, 10))
    cards_note = lab(vid_row_cards, "", fg=C["muted"])
    cards_note.configure(wraplength=250, justify="left")
    cards_note.pack(side="left")
    # Studio's writer status. NEVER updated from the Tk thread on a network call — the load runs
    # in a background thread and only the LABEL comes back through root.after (see _writer_load).
    cards_status = lab(vid_row_cards, "", fg=C["muted"])
    cards_status.configure(wraplength=190, justify="left")
    cards_status.pack(side="left", padx=(8, 0))
    _writer = {"thread": None}

    # ---- FREE CARD 2: the setting Ryan asked for -----------------------------------------
    # A 2-card (Turbo) H3 render needs ~21 GB on GPU1, where Ollama keeps its models. ON (the
    # default) unloads EVERYTHING resident there before the run; OFF leaves your models alone
    # and the run REFUSES instead. It is a choice, not a rule, so it is a setting.
    free_card_var = tk.BooleanVar(value=True)
    free_row = tk.Frame(left, bg=C["panel"])
    free_btn = tk.Button(free_row, text="FREE CARD 2", bg=C["panel2"], fg=C["ink"],
                         activebackground=C["rust"], activeforeground="#fff", relief="flat",
                         font=fsm, padx=9, pady=2, borderwidth=0, highlightthickness=1,
                         highlightbackground=C["line"], command=lambda: _toggle_free_card())
    free_btn.pack(side="left")
    free_note = lab(free_row, "", fg=C["muted"])
    free_note.configure(wraplength=430, justify="left")
    free_note.pack(side="left", padx=(8, 0))

    def _paint_free_card():
        _style_toggle(free_btn, bool(free_card_var.get()), C)
        free_note.configure(
            text=("a 2-card run unloads EVERY model on card 2 first, so both cards have room"
                  if free_card_var.get() else
                  "OFF \u2014 a 2-card run will REFUSE if anything is still on card 2, "
                  "instead of unloading it"))

    def _toggle_free_card():
        free_card_var.set(not free_card_var.get())
        _paint_free_card()
        apply_cards()      # the CARD MODE hint has to agree with this

    def _turbo_is_h3():
        # Turbo is a Raylight TRANSFORM of the H3 graph — it only means anything on H3.
        return engine_of(st.get("mode")) == "h3"

    def _cards_hint():
        """What the row SAYS. ⚠️ This must equal what params() SENDS — the "control that
        lies" rule; TESTCARDS asserts the two agree."""
        mode = cards_var.get()
        if mode == "Turbo":
            if _turbo_is_h3():
                # ⚠️ This must equal what params() SENDS (the "control that lies" rule).
                return ("both 3090s on ONE render (Raylight). Card 2 is freed first: "
                        + ("every model on it is unloaded."
                           if free_card_var.get() else
                           "FREE CARD 2 is OFF, so a resident model will REFUSE the run."))
            return "Turbo applies to H3 only — this engine runs on 1 card."
        if mode == "Studio":
            return ("render on card 0; card 2 is kept warm for the writer — "
                    "POLISH PROMPT uses it.")
        return "one 3090 — the default path"

    def _set_writer_status(text, colour=None):
        try:
            cards_status.configure(text=text, fg=colour or C["muted"])
        except Exception:
            pass

    def _start_writer_status():
        """Studio: warm the DIRECTOR on card 2 in the BACKGROUND and keep the label honest.
        ⚠️ Never block the Tk main thread on the network — the round-trip runs off-thread
        and only the label update returns through root.after."""
        if os.environ.get("FORGE_LAB_NODIRECTOR") == "1":
            _set_writer_status("card 2: off", C["muted"])
            return
        t = _writer.get("thread")
        if t is not None and t.is_alive():
            return                       # already warming — do not stack threads
        _set_writer_status("card 2: writer loading…", C["muted"])

        def work():
            try:
                from director import Director
                d = Director()
                d.load()
                ok = d.is_loaded()
                msg = "card 2: ready" if ok else "card 2: not warmed"
                col = C["moss"] if ok else C["ochre"]
            except Exception:
                msg, col = "card 2: unreachable", C["ochre"]
            root.after(0, lambda: _set_writer_status(msg, col))
        tt = threading.Thread(target=work, daemon=True)
        _writer["thread"] = tt
        tt.start()

    def apply_cards(*_):
        cards_note.configure(text=_cards_hint(), fg=C["ink"])
        if cards_var.get() == "Studio":
            _start_writer_status()
        else:
            cards_status.configure(text="")
        refresh_estimate()
    # NOTE: same trap as apply_audio above — do not CALL this here, refresh_estimate() does not
    # exist yet at construction time. set_mode() calls it during kick-off.

    # ---- WAN 2.2 S2V: it needs an AUDIO FILE (that file IS the performance) and a length
    # measured in CHUNKS, because a longer scene is CHAINED — never a bigger `length`.
    # 4.81 / 9.81 / 14.81 s. The label IS the state; there is no free number box to get wrong.
    s2v_row = tk.Frame(left, bg=C["panel"])
    lab(s2v_row, "AUDIO FILE").pack(side="left")
    s2v_btn = tk.Button(s2v_row, text="CHOOSE…", command=lambda: pick_audio(),
                        bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
                        activeforeground="#fff", relief="flat", font=fsm, padx=8, pady=2,
                        borderwidth=0)
    s2v_btn.pack(side="left", padx=(6, 8))
    s2v_name = lab(s2v_row, "none chosen", fg=C["muted"])
    s2v_name.configure(wraplength=180, justify="left")
    s2v_name.pack(side="left")

    s2v_len_row = tk.Frame(left, bg=C["panel"])
    lab(s2v_len_row, "LENGTH").pack(side="left")
    s2v_len_var = tk.StringVar(value=S2V_LENGTHS[S2V_MAX_CHUNKS])
    s2v_len_menu = tk.OptionMenu(s2v_len_row, s2v_len_var, *S2V_LENGTHS.values(),
                                 command=lambda *_: apply_s2v())
    s2v_len_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                           highlightthickness=0, activebackground=C["rust"],
                           activeforeground="#fff", borderwidth=0, width=17,
                           highlightbackground=C["panel"])
    s2v_len_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)   # macOS draws the popup panel WHITE
    s2v_len_menu.pack(side="left", padx=(6, 8))
    s2v_note = lab(s2v_len_row, "", fg=C["muted"])
    s2v_note.configure(wraplength=200, justify="left")
    s2v_note.pack(side="left")

    # ---- the two v2v engines need a SOURCE CLIP: it IS the structure (LTX) or the performance
    # (Animate 2). Its frame count also sets LTX's latent length, so the picker READS the file
    # (video_probe) and the estimate uses the real number rather than a guess.
    src_row = tk.Frame(left, bg=C["panel"])
    lab(src_row, "SOURCE VIDEO").pack(side="left")
    src_btn = tk.Button(src_row, text="CHOOSE…", command=lambda: pick_source(),
                        bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
                        activeforeground="#fff", relief="flat", font=fsm, padx=8, pady=2,
                        borderwidth=0)
    src_btn.pack(side="left", padx=(6, 8))
    src_name = lab(src_row, "none chosen", fg=C["muted"])
    src_name.configure(wraplength=170, justify="left")
    src_name.pack(side="left")

    src_note_row = tk.Frame(left, bg=C["panel"])
    src_note = lab(src_note_row, "", fg=C["muted"])
    src_note.configure(wraplength=340, justify="left")
    src_note.pack(side="left")

    def pick_source():
        path = filedialog.askopenfilename(
            title="The CLIP that drives the shot — the structure (LTX) / the performance (Animate 2)",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"),
                       ("All files", "*.*")])
        if not path:
            return
        st["source_path"] = path
        st.pop("src_frames", None)
        _pb = video_probe(path)
        if _pb:
            st["src_frames"] = int(_pb["frames"])
        src_name.configure(text=os.path.basename(path), fg=C["ink"])
        apply_source()

    def apply_source(*_):
        """The label IS the state: the clip's real numbers, read from the file itself."""
        _pb = video_probe(st.get("source_path")) if st.get("source_path") else None
        if not _pb:
            src_note.configure(text="no clip chosen — this engine has nothing to drive it")
        else:
            src_note.configure(text="%dx%d · %g fps · %d frames · %.2f s"
                                    % (_pb["width"], _pb["height"], _pb["fps"],
                                       _pb["frames"], _pb["seconds"]))
        refresh_estimate()
    # NOTE: do NOT call apply_source() here either — same NameError trap as apply_audio above
    # (refresh_estimate is not defined until further down). relayout() calls it at kick-off.

    def s2v_chunks_now():
        """Chunk count from the menu — the label is the state, so nothing can drift."""
        want = s2v_len_var.get()
        for n, lbl_ in S2V_LENGTHS.items():
            if lbl_ == want:
                return n
        return S2V_MAX_CHUNKS

    def pick_audio():
        path = filedialog.askopenfilename(
            title="Audio for the performance — the mouth follows THIS file",
            filetypes=[("Audio", "*.wav *.mp3 *.m4a *.flac *.ogg"),
                       ("All files", "*.*")])
        if not path:
            return
        st["audio_path"] = path
        s2v_name.configure(text=os.path.basename(path), fg=C["ink"])
        apply_s2v()

    def apply_s2v(*_):
        n = s2v_chunks_now()
        fr = s2v_chunks_to_frames(n)
        s2v_note.configure(text=("%d chunk%s → %d frames = %.2f s @16fps"
                                 % (n, "" if n == 1 else "s", fr, fr / S2V_FPS)))
        refresh_estimate()
    # NOTE: do NOT call apply_s2v() here either — same NameError trap as apply_audio above.
    # relayout() calls it during kick-off, which is safely after refresh_estimate exists.

    def _choices(kind):
        return MODEL_CHOICES.get(kind, MODEL_CHOICES["Image"])

    def _fill_model_menu(kind):
        """Rebuild the MODEL list for a KIND — the KIND's models, in registry order."""
        m = model_menu["menu"]
        m.delete(0, "end")
        for (_md, _var, lab) in _choices(kind):
            m.add_command(label=lab, command=lambda lab=lab: pick_model(lab))

    def pick_model(label):
        """Set state from a MODEL label. The label carries the ENGINE for video, so this is
        where 'H3 · First frame' and 'WAN 2.2 · First frame' are told apart."""
        kind = kind_var.get()
        for (md, var, lab) in _choices(kind):
            if lab != label:
                continue
            # ⚠️ Capture the SIZE slot we are LEAVING before touching the variant. Otherwise
            # set_mode() sees the NEW variant and saves the outgoing canvas under it — which
            # is how S2V came up at WAN's 832x480 instead of its own 640x640.
            st["_size_key_prev"] = size_key(st.get("mode"), st.get("wan_mode"))
            if md == "video":
                st["video_mode"] = var
            elif md == "wan":
                st["wan_mode"] = var
            else:
                st["imode"] = md
            st.setdefault("model_by_kind", {})[kind] = lab
            model_var.set(lab)
            _plog("model -> %s (%s)" % (lab, md))
            set_mode(md)
            return
        _plog("model -> %r is not in the %s list" % (label, kind))

    def on_model(*_):
        """The MODEL dropdown was used directly."""
        pick_model(model_var.get())

    def open_long_video_workspace():
        """LONG VIDEO is a WORKSPACE, not a model: it needs a scene list, takes per scene and
        a full-width layout — none of which fit the single-take column whose payload,
        estimate and settings are all keyed to ONE take. The KIND entry is the door; this
        window is the room."""
        ctx = {"C": C, "fonts": {"f": f, "fh": fh, "fsm": fsm}, "mono": {"m": fmono},
               "submit": submit, "run_state": run_state, "collect": collect,
               "s2v_size": VIDEO_SIZES.get(S2V_SIZE_DEFAULT, (640, 640)),
               "subject_block": "", "btn_colors": _btn_colors}
        return open_long_video(root, ctx)

    def open_character_workspace():
        """CHARACTER is a WORKSPACE too: it needs a saved list, a still, a prompt block and a
        seed — none of which belong in a column keyed to ONE take. TYPE is the door.

        ⚠️ ONE window only. Picking the entry again must RAISE the one already open, not stack
        an identical second copy on top of it.
        """
        held = st.setdefault("_char_win", {})
        w = held.get("win")
        try:
            if w is not None and w.winfo_exists():
                w.deiconify()
                w.lift()
                return w
        except Exception:
            pass
        ctx = {"C": C, "fonts": {"f": f, "fh": fh, "fsm": fsm}, "mono": {"m": fmono},
               "load_character": apply_character,
               # ⚠️ `_btn_colors` is a LOCAL of launch(), so an on-demand window cannot see it —
               # it must arrive through ctx. Without it this window's buttons were never repainted
               # light-with-dark-text and read as unreadable. Ryan: "the buttons are hard to read
               # for me and should be updated to have black text." (Documented on-demand trap.)
               "btn_colors": _btn_colors}
        w = open_characters(root, ctx)
        held["win"] = w
        return w

    def open_weirdo_workspace():
        """WEIRDO is a WORKSPACE, not a model: it rolls a character, renders it and makes the
        video, all inside one window - no LOAD INTO FORM, no second window.

        ⚠️ ONE window only, same as Character: picking the entry again RAISES the open one.
        """
        held = st.setdefault("_weirdo_win", {})
        w = held.get("win")
        try:
            if w is not None and w.winfo_exists():
                w.deiconify()
                w.lift()
                return w
        except Exception:
            pass
        ctx = {"C": C, "fonts": {"f": f, "fh": fh, "fsm": fsm}, "mono": {"m": fmono},
               "submit": submit, "run_state": run_state, "collect": collect,
               "btn_colors": _btn_colors}
        w = open_weirdo(root, ctx)
        held["win"] = w
        return w

    def open_song_workspace():
        """SONG is a WORKSPACE, not a model: style + lyrics + a length, one MAKE button and a
        player. None of that belongs in a column keyed to ONE image or video take.

        ⚠️ ONE window only, same as Character/Weirdo/Studio: picking the entry again RAISES
        the open one instead of stacking an identical second copy on top of it.
        """
        held = st.setdefault("_song_win", {})
        w = held.get("win")
        try:
            if w is not None and w.winfo_exists():
                w.deiconify()
                w.lift()
                return w
        except Exception:
            pass
        ctx3 = {"C": C, "fonts": {"f": f, "fh": fh, "fsm": fsm}, "mono": {"m": fmono},
                "submit": submit, "run_state": run_state, "collect": collect,
                # ⚠️ _btn_colors is a LOCAL of launch(); an on-demand window must receive it
                # through ctx or its buttons never get repainted light-with-dark-text.
                "btn_colors": _btn_colors}
        w = open_song(root, ctx3)
        held["win"] = w
        return w

    def open_dialogue_workspace():
        """DIALOGUE is a WORKSPACE: paste a script, cast voices, render lines,
        and assemble the scene — all in one numbered window.

        ONE window only, same as Song/Weirdo/Studio.
        """
        held = st.setdefault("_dialogue_win", {})
        w = held.get("win")
        try:
            if w is not None and w.winfo_exists():
                w.deiconify()
                w.lift()
                return w
        except Exception:
            pass
        ctx4 = {"C": C, "fonts": {"f": f, "fh": fh, "fsm": fsm}, "mono": {"m": fmono},
                "btn_colors": _btn_colors, "data": DATA, "_driver": _DRIVER,
                # sung lines go through the SAME Song path (submit -> Takes -> collect)
                "submit": submit, "wait_and_collect": wait_and_collect, "runs": RUNS}
        from dialogue_ui import open_dialogue
        w = open_dialogue(root, ctx4)
        held["win"] = w
        return w

    def open_studio_workspace():
        """STUDIO is a WORKSPACE, not a model: the whole short — cast, idea, script,
        shots, renders, cut — happens in one numbered window. No handoff to the form.

        ⚠️ ONE window only, same as Character/Weirdo: picking the entry again RAISES the
        open one instead of stacking a second copy.
        """
        held = st.setdefault("_studio_win", {})
        w = held.get("win")
        try:
            if w is not None and w.winfo_exists():
                w.deiconify()
                w.lift()
                return w
        except Exception:
            pass
        ctx2 = {"C": C, "fonts": {"f": f, "fh": fh, "fsm": fsm}, "mono": {"m": fmono},
                "submit": submit, "run_state": run_state, "collect": collect,
                # ⚠️ _btn_colors is a LOCAL of launch(); an on-demand window must receive
                # it through ctx or its buttons never get repainted light-with-dark-text.
                "btn_colors": _btn_colors}
        w = open_studio(root, ctx2)
        held["win"] = w
        return w

    def on_kind(*_):
        kind = kind_var.get()
        if kind == "Studio":
            try:
                open_studio_workspace()
            except Exception as exc:
                _log_err("studio workspace failed: %r" % exc)
            kind_var.set(kind_label(st["mode"]))      # the form stays where it was
            return
        if kind == "Weirdo":
            try:
                open_weirdo_workspace()
            except Exception as exc:
                _log_err("weirdo workspace failed: %r" % exc)
            kind_var.set(kind_label(st["mode"]))      # the form stays where it was
            return
        if kind == "Song":
            try:
                open_song_workspace()
            except Exception as exc:
                _log_err("song workspace failed: %r" % exc)
            kind_var.set(kind_label(st["mode"]))      # the form stays where it was
            return
        if kind == "Dialogue":
            try:
                open_dialogue_workspace()
            except Exception as exc:
                _log_err("dialogue workspace failed: %r" % exc)
            kind_var.set(kind_label(st["mode"]))      # the form stays where it was
            return
        if kind == "Character":
            try:
                open_character_workspace()
            except Exception as exc:
                _log_err("character workspace failed: %r" % exc)
            kind_var.set(kind_label(st["mode"]))      # the form stays where it was
            return
        if kind == "Long video":
            try:
                open_long_video_workspace()
            except Exception as exc:
                _log_err("long video workspace failed: %r" % exc)
            kind_var.set(kind_label(st["mode"]))      # the form stays where it was
            return
        _fill_model_menu(kind)
        # restore whatever was last chosen for THIS kind, so Image -> Video -> Image does
        # not silently reset the model
        lab = st.setdefault("model_by_kind", {}).get(kind)
        if not any(lab == c[2] for c in _choices(kind)):
            lab = _choices(kind)[0][2]
        pick_model(lab)

    def _sync_dropdowns():
        """Point KIND and MODEL at the current state (settings restore, variant picks)."""
        kind = kind_label(st["mode"])
        kind_var.set(kind)
        _fill_model_menu(kind)
        model_var.set(model_label(st["mode"], st.get("video_mode"), st.get("wan_mode")))

    # ---- prompt
    prompt_head = tk.Label(left, text="2 · PROMPT", bg=C["panel"], fg=C["muted"], font=fh)
    prompt_head.pack(anchor="w", padx=16, pady=(16, 6))
    prompt = tk.Text(left, height=7, wrap="word", bg="#100e0c", fg=C["ink"],
                     insertbackground=C["ink"], relief="flat", font=f, padx=8, pady=6)
    prompt.pack(fill="x", padx=16)

    pol_row = tk.Frame(left, bg=C["panel"])
    pol_row.pack(fill="x", padx=16, pady=(6, 0))
    polish_btn = tk.Button(pol_row, text="POLISH PROMPT", bg=C["panel2"], fg=C["ink"],
                           activebackground=C["rust_deep"], activeforeground="#fff",
                           disabledforeground=C["dim"], relief="flat", font=fh,
                           padx=10, pady=5, borderwidth=0,
                           highlightthickness=1, highlightbackground=C["line"],
                           command=lambda: do_polish())
    polish_btn.pack(side="left")
    revert_btn = tk.Button(pol_row, text="REVERT", bg=C["panel2"], fg=C["ink"],
                           activebackground=C["rust_deep"], activeforeground="#fff",
                           disabledforeground=C["dim"], relief="flat", font=fsm,
                           padx=8, pady=5, borderwidth=0,
                           highlightthickness=1, highlightbackground=C["line"],
                           state="disabled", command=lambda: do_revert())
    revert_btn.pack(side="left", padx=6)
    clear_btn = tk.Button(pol_row, text="CLEAR", bg=C["panel2"], fg=C["ink"],
                          activebackground=C["rust_deep"], activeforeground="#fff",
                          disabledforeground=C["dim"],
                          relief="flat", font=fsm, padx=8, pady=5, borderwidth=0,
                          highlightthickness=1, highlightbackground=C["line"],
                          command=lambda: do_clear())
    clear_btn.pack(side="left", padx=(0, 6))
    pol_msg = tk.Label(pol_row, text="", bg=C["panel"], fg=C["muted"], font=fsm)
    pol_msg.pack(side="left", padx=6)
    phelp = tk.Label(left, text="", bg=C["panel"], fg=C["muted"], font=fsm,
                     wraplength=380, justify="left")
    phelp.pack(anchor="w", padx=16, pady=(5, 0))

    # ---- CHARACTER: applied from the CHARACTER workspace (TYPE -> "Character").
    # The main column deliberately carries NO character UI. Creating and editing a character is
    # a CRUD job with a list, a still and a prompt block — none of which belong in a column
    # keyed to ONE take. Same reason LONG VIDEO got its own window; TYPE is the door.
    def apply_character(meta, still_path):
        """Put a saved character onto the FORM: prompt block, seed, LoRA and ref 1.

        Returns a short human note, which the workspace shows. Called by the workspace, so it
        must never raise into it — a bad character costs a message, not the window.
        """
        meta = meta or {}
        did = []
        # The prompt box is about to be REPLACED. Stash what was there into the SAME buffer
        # CLEAR uses, so the existing REVERT button already restores it — no new undo to learn.
        had = prompt.get("1.0", "end").rstrip("\n")
        if had.strip():
            _pol["original"] = had
            revert_btn.configure(state="normal")
        prompt.delete("1.0", "end")
        # ⚠️ MODE-AWARE. meta["prompt"] is a STILL block - it ends "looking straight at the
        # camera, sharp focus, 85mm lens". In a VIDEO mode that fights the motion you are about to
        # ask for AND gives the model nothing to do. Recompose from the picks with the video tail
        # plus the character's ACTION.
        _vid = bool((MODES.get(st.get("mode")) or {}).get("video"))
        _blk = ""
        if _vid and meta.get("picks"):
            try:
                _blk = compose_character_prompt(meta["picks"], for_video=True,
                                                action=meta.get("action") or "")
            except Exception:
                _blk = ""
        # ⚠️ r2v REQUIRES the tag. The app's own help for this mode says "Tag each one in the
        # prompt (<Picture 1>, <Picture 2>…)" and "UNTAGGED extra stills blend the faces
        # together" - yet loading a character set ref 1 and left the prompt untagged, so the
        # user had to remember to type it. Only r2v: i2v uses the ref as the FIRST FRAME and flf
        # as first+last, where "is the character" would be wrong.
        _tagged = bool(_blk) and st.get("video_mode") == "r2v" and bool(still_path) and _ref_cap()
        if _tagged:
            _blk = "<Picture 1> is the character. " + _blk
        prompt.insert("1.0", _blk or meta.get("prompt") or "")
        did.append("prompt (video look + action%s)" % (", ref 1 tagged" if _tagged else "")
                   if (_vid and _blk) else "prompt")
        e_seed.delete(0, "end")
        e_seed.insert(0, str(meta.get("seed", -1)))
        did.append("seed")
        # LoRA: only set it if the main menu actually OFFERS that name, otherwise the dropdown
        # would show a value it cannot re-select.
        lname = meta.get("lora")
        offered = []
        try:
            _m = lora_menu["menu"]
            _n = _m.index("end")
            offered = [_m.entrycget(i, "label") for i in range(0 if _n is None else _n + 1)]
        except Exception:
            pass
        if lname and lname in offered:
            lora_var.set(lname)
            e_lstr.delete(0, "end")
            e_lstr.insert(0, str(meta.get("lora_strength", 1.0)))
            did.append("LoRA")
        elif lname:
            did.append("LoRA %s (not offered here)" % lname)
        if still_path and _ref_cap():
            st["refs"] = [still_path]
            _render_refs()
            did.append("ref 1")
        elif still_path:
            did.append("still NOT set — this mode takes no references")
        note = "loaded %s: %s%s" % (
            meta.get("name") or "(unsaved)", ", ".join(did),
            "   · REVERT puts your wording back" if had.strip() else "")
        _plog("character load: %s" % note)
        refresh_estimate()
        return note

    # ---- reference image. Always visible in every mode so there is never a
    # mystery about where a reference goes; only the edit modes USE it.
    refbox = tk.Frame(left, bg=C["panel"])
    refbox.pack(fill="x", padx=16, pady=(14, 0))
    ref_head = tk.Frame(refbox, bg=C["panel"])
    ref_head.pack(fill="x")
    tk.Label(ref_head, text="REFERENCE IMAGES", bg=C["panel"], fg=C["muted"],
             font=fh).pack(side="left")
    ref_count = tk.Label(ref_head, text="", bg=C["panel"], fg=C["muted"], font=fsm)
    ref_count.pack(side="right")
    ref_help = tk.Label(refbox, text="", bg=C["panel"], fg=C["muted"], font=fsm,
                        wraplength=366, justify="left", anchor="w")
    ref_help.pack(anchor="w", pady=(3, 4))
    # One row per reference, rebuilt whenever the list changes. ORDER IS MEANINGFUL:
    # ref 1 is the primary (<image1> for edits, first_frame for flf).
    ref_list = tk.Frame(refbox, bg=C["panel"])
    ref_list.pack(fill="x")
    add_btn = tk.Button(refbox, text="ADD IMAGE…", bg=C["rust_deep"], fg="#fff",
                        activebackground=C["rust_hi"], activeforeground="#fff",
                        disabledforeground=C["dim"], relief="flat", font=fh,
                        padx=12, pady=6, borderwidth=0,
                        command=lambda: choose_ref())
    add_btn.pack(anchor="w", pady=(8, 0))

    # ---- settings
    setbox = tk.Frame(left, bg=C["panel"])
    setbox.pack(fill="x", padx=16, pady=(14, 0))
    tk.Label(setbox, text="3 · SETTINGS", bg=C["panel"], fg=C["muted"], font=fh).pack(anchor="w")
    tk.Label(setbox, text="QUALITY", bg=C["panel"], fg=C["muted"], font=fsm).pack(
        anchor="w", pady=(10, 2))
    qvar = tk.StringVar(value="Medium")
    qmenu = tk.OptionMenu(setbox, qvar, *QUALITY.keys(),
                          command=lambda name: apply_quality(name))
    qmenu.configure(bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
                    activeforeground="#fff", relief="flat", borderwidth=0,
                    highlightthickness=0, font=fmono, padx=10, pady=3, anchor="w")
    qmenu["menu"].configure(bg=C["panel2"], fg="#000000", activebackground=C["rust"],
                            activeforeground="#000000", font=fmono, borderwidth=0)   # black: the native macOS menu panel is white
    qmenu.pack(anchor="w", fill="x")
    q_hint = tk.Label(setbox, text="", bg=C["panel"], fg=C["muted"], font=fsm,
                      wraplength=366, justify="left", anchor="w")
    q_hint.pack(anchor="w", pady=(4, 0))

    aspect_lbl = tk.Label(setbox, text="ASPECT", bg=C["panel"], fg=C["muted"], font=fsm)
    aspect_lbl.pack(anchor="w", pady=(10, 2))
    avar = tk.StringVar(value="1:1 Square")
    amenu = tk.OptionMenu(setbox, avar, *ASPECTS.keys(), command=lambda _n: apply_quality())
    amenu.configure(bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
                    activeforeground="#fff", relief="flat", borderwidth=0,
                    highlightthickness=0, font=fmono, padx=10, pady=3, anchor="w")
    amenu["menu"].configure(bg=C["panel2"], fg="#000000", activebackground=C["rust"],
                            activeforeground="#000000", font=fmono, borderwidth=0)   # black: the native macOS menu panel is white
    amenu.pack(anchor="w", fill="x")

    qgrid = tk.Frame(setbox, bg=C["panel"])
    qgrid.pack(fill="x", pady=(8, 0))
    def field(row, col, label, width=7):
        e = tk.Frame(qgrid, bg=C["panel"])
        e.grid(row=row, column=col, sticky="w", padx=(0, 12), pady=4)
        tk.Label(e, text=label, bg=C["panel"], fg=C["muted"], font=fsm).pack(anchor="w")
        v = tk.Entry(e, width=width, bg="#100e0c", fg=C["ink"], insertbackground=C["ink"],
                     relief="flat", font=fmono)
        v.pack()
        return v
    e_w = field(0, 0, "WIDTH")
    e_h = field(0, 1, "HEIGHT")
    e_steps = field(0, 2, "STEPS")
    e_res = field(1, 0, "RESOLUTION (0 = native)")
    e_seed = field(1, 1, "SEED")
    e_prefix = field(1, 2, "PREFIX", 12)
    e_w.insert(0, "1024"); e_h.insert(0, "1024"); e_steps.insert(0, "25")
    e_res.insert(0, "1024"); e_seed.insert(0, "-1"); e_prefix.insert(0, "forge_lab")

    def _size_choices(kind):
        """The SIZE entries a KIND may pick — each engine's own measured set.

        ⚠️ The menu used to be built ONCE from VIDEO_SIZES and never refilled, so LTX_SIZES and
        ANIMATE2_SIZES existed but were UNREACHABLE: the dropdown could not offer the canvas the
        engine was actually measured at, and the estimate would have quoted a size no run used.
        """
        if kind == "ltx":
            return LTX_SIZES
        if kind == "animate2":
            return ANIMATE2_SIZES
        return VIDEO_SIZES          # image, H3, WAN and S2V share one measured set

    def pick_size(label):
        if label != size_var.get():
            size_var.set(label)     # keep the dropdown honest — see _fill_size_menu
        apply_vsize()

    def _fill_size_menu(kind):
        """Rebuild the SIZE list for a KIND.

        ⚠️ Same _setit trap as _fill_quality_menu: adding our own menu commands drops tkinter's
        wrapper, so each command must set the variable ITSELF or the label keeps showing the old
        size while the engine silently renders the new one.
        """
        m = size_menu["menu"]
        m.delete(0, "end")
        for lab_ in _size_choices(kind):
            m.add_command(label=lab_, command=lambda lab_=lab_: pick_size(lab_))

    def _quality_labels():
        """The entries CURRENTLY on the QUALITY dropdown. Reading them back is how the
        model-change validation (and the drivers) stay honest about what the user can pick."""
        n = qmenu["menu"].index("end")
        n = 0 if n is None else n + 1
        return [qmenu["menu"].entrycget(i, "label") for i in range(n)]

    def _fill_quality_menu(kind):
        """Image, H3 video and WAN each get a different quality axis, so the dropdown
        contents swap with the KIND: image presets carry steps + megapixels, H3 video carries
        steps only, WAN carries a PATH (turbo vs full are different models), and LTX / Animate 2
        carry a single READOUT because their steps are baked into their graphs.

        ⚠️ `kind` is the real KIND STRING (`size_key`) and every branch tests it EXPLICITLY. It used
        to be a tri-state — `"wan"` / `True` / `False` — and the video branch passed the broad flag,
        which is how LTX and Wan Animate 2 silently inherited H3's Draft/Standard/High while
        `params()` went on sending their baked 8 and 6."""
        m = qmenu["menu"]
        m.delete(0, "end")
        if kind == "wan":
            # S2V has exactly ONE path, so the dropdown is a readout rather than a choice
            keys = ([S2V_QUALITY] if st.get("wan_mode") == "s2v" else list(WAN_QUALITY))
        elif kind in ("ltx", "animate2"):
            # ⚠️ READOUT, one entry — see `fixed_quality_label`. H3's presets do not apply here.
            keys = [fixed_quality_label(kind)]
        elif kind == "video":
            keys = list(VIDEO_QUALITY)
        else:
            keys = list(QUALITY)
        for k in keys:
            m.add_command(label=k, command=lambda k=k: apply_quality(k))

    def apply_quality(name=None):
        """Seed steps + size from a preset. Every field stays editable afterwards,
        so this sets starting values rather than locking anything. The width/height
        it derives keep whatever aspect ratio is currently in the fields.

        ⚠️ It MUST also set the variable when handed a name. tkinter's OptionMenu wraps each
        item in `_setit`, which does `variable.set(v)` and THEN calls the callback —
        _fill_quality_menu() wipes the entries and re-adds its own commands, dropping those
        wrappers. So picking a preset applied the numbers but left the variable on the OLD
        preset, and refresh_estimate() then compared the new step count against the old one,
        saw a drift, and flipped the dropdown to "Custom".
        """
        if name and name != qvar.get():
            qvar.set(name)                     # keep the dropdown honest (see above)
        name = name or qvar.get()
        if MODES[st["mode"]].get("wan"):
            if st.get("wan_mode") == "s2v":
                # ONE path: 4 steps, cfg 1.0, shift 8, lightning LoRA on — all baked in
                # S2V_STEPS / S2V_CFG / S2V_SHIFT via wan_rules(). A readout, not an input.
                if qvar.get() != S2V_QUALITY:
                    qvar.set(S2V_QUALITY)
                e_steps.configure(state="normal")
                e_steps.delete(0, "end"); e_steps.insert(0, str(S2V_STEPS))
                e_steps.configure(state="disabled")
                q_hint.configure(text=WAN_MODES["s2v"]["note"])
                refresh_estimate()
                return
            # WAN: the preset IS the path. Steps are NOT a free knob here — 4 with the
            # lightning LoRAs and cfg 1.0, or 20 without them and with a real negative
            # prompt. Anything between them half-switches the path, so the field is driven
            # from the preset and disabled rather than left typeable.
            wq = WAN_QUALITY.get(name, WAN_QUALITY[WAN_QUALITY_DEFAULT])
            e_steps.configure(state="normal")
            e_steps.delete(0, "end"); e_steps.insert(0, str(wq["steps"]))
            e_steps.configure(state="disabled")
            q_hint.configure(text=wq["note"])
            refresh_estimate()
            return
        if MODES[st["mode"]].get("ltx") or MODES[st["mode"]].get("animate2"):
            # ⚠️ READOUT, not an input (same as S2V). These graphs run a fixed step count that
            # `params()` takes from the registry; leaving the field typeable is what made the number
            # on screen disagree with the number that runs.
            _kd = kind_of(st["mode"])
            qvar.set(fixed_quality_label(_kd))
            e_steps.configure(state="normal")
            e_steps.delete(0, "end")
            e_steps.insert(0, str(int(MODES[st["mode"]]["steps"])))
            e_steps.configure(state="disabled")
            q_hint.configure(text=FIXED_QUALITY_NOTE[_kd])
            refresh_estimate()
            return
        e_steps.configure(state="normal")
        if MODES[st["mode"]].get("video"):
            # video: steps are the only meaningful knob
            vq = VIDEO_QUALITY.get(name, VIDEO_QUALITY["Standard"])
            e_steps.delete(0, "end"); e_steps.insert(0, str(vq["steps"]))
            q_hint.configure(text=vq["note"])
            refresh_estimate()
            return
        st["ipreset"] = name
        q = QUALITY.get(name, QUALITY["Medium"])
        if q.get("steps") is None:               # "Custom": leave the numbers alone
            q_hint.configure(text=q["note"])
            refresh_estimate()
            return
        e_steps.delete(0, "end"); e_steps.insert(0, str(q["steps"]))
        e_res.delete(0, "end");  e_res.insert(0, str(q["resolution"]))
        ratio = ASPECTS.get(avar.get(), 1.0)     # authoritative for shape
        mp = q["mp"]
        # Round the LONG edge to a multiple of 32, then derive the SHORT edge from
        # it. Rounding both independently lets the ratio drift (16:9 came out
        # 1344x736 = 1.83:1); deriving keeps it honest (1344x768, the native 16:9).
        if ratio >= 1.0:
            w = max(32, int(round(((mp * 1e6 * ratio) ** 0.5) / 32)) * 32)
            h = max(32, int(round((w / ratio) / 32)) * 32)
        else:
            h = max(32, int(round(((mp * 1e6 / ratio) ** 0.5) / 32)) * 32)
            w = max(32, int(round((h * ratio) / 32)) * 32)
        e_w.delete(0, "end"); e_w.insert(0, str(w))
        e_h.delete(0, "end"); e_h.insert(0, str(h))
        q_hint.configure(text=q["note"])
        refresh_estimate()
    dim_lbl = tk.Label(setbox, text="", bg=C["panel"], fg=C["muted"], font=fmonos)
    dim_lbl.pack(anchor="w", pady=(6, 0))

    # ---- estimate + run
    estbox = tk.Frame(left, bg=C["panel"])
    estbox.pack(fill="x", padx=16, pady=(16, 0))
    tk.Label(estbox, text="4 · ESTIMATE", bg=C["panel"], fg=C["muted"], font=fh).pack(anchor="w")
    est_lbl = tk.Label(estbox, text="—", bg=C["panel"], fg=C["ink"], font=fmono,
                       justify="left", wraplength=380)
    est_lbl.pack(anchor="w", pady=(6, 10))
    btn_row = tk.Frame(estbox, bg=C["panel"])
    btn_row.pack(fill="x")
    run_btn = tk.Button(btn_row, text="APPROVE & RUN", bg=C["rust_deep"], fg="#fff",
                        activebackground=C["rust_hi"], activeforeground="#fff",
                        disabledforeground=C["dim"],
                        relief="flat", font=fh, padx=14, pady=7,
                        borderwidth=0)
    run_btn.pack(side="left")
    stop_btn = tk.Button(btn_row, text="INTERRUPT", bg=C["ochre"], fg="#14110f",
                         activebackground="#d8b45f", activeforeground="#14110f",
                         disabledforeground=C["dim"], relief="flat", font=fsm,
                         padx=10, pady=7, borderwidth=0,
                         # ⚠️ in a THREAD: post_json blocks up to 60 s when forge is unreachable,
                         # and on the Tk thread that froze the whole window.
                         command=lambda: threading.Thread(
                             target=lambda: post_json("/interrupt", {}), daemon=True).start())
    stop_btn.pack(side="left", padx=8)
    # removes only THIS app's WAITING runs — see cancel_queued(). Command bound late, below.
    cancelq_btn = tk.Button(btn_row, text="CANCEL QUEUED", bg=C["panel2"], fg=C["ink"],
                            relief="flat", font=fsm, padx=10, pady=7, borderwidth=0)
    cancelq_btn.pack(side="left")

    # ---- run panel (right, top)
    runpan = tk.Frame(right, bg=C["panel"])
    runpan.pack(fill="x")
    tk.Label(runpan, text="5 · RUN", bg=C["panel"], fg=C["muted"], font=fh).pack(
        anchor="w", padx=16, pady=(12, 6))
    runst = tk.Label(runpan, text="idle", bg=C["panel"], fg=C["muted"], font=f,
                     anchor="w", wraplength=560, justify="left")
    runst.pack(anchor="w", padx=16)
    elapsed = tk.Label(runpan, text="", bg=C["panel"], fg=C["ink"], font=F(17, mono=True))
    elapsed.pack(anchor="w", padx=16, pady=(4, 6))
    canvas_bar = tk.Canvas(runpan, height=9, bg="#100e0c", highlightthickness=1,
                           highlightbackground=C["line"])
    canvas_bar.pack(fill="x", padx=16)
    bar = canvas_bar.create_rectangle(0, 0, 0, 9, fill=C["rust"], width=0)
    steptxt = tk.Label(runpan, text="", bg=C["panel"], fg=C["muted"], font=fmonos)
    steptxt.pack(anchor="w", padx=16, pady=(5, 0))
    gpu_lbl = tk.Label(runpan, text="", bg=C["panel"], fg=C["muted"], font=fmonos)
    gpu_lbl.pack(anchor="w", padx=16, pady=(6, 14))

    # ---- takes
    tk.Label(right, text="6 · TAKES", bg=C["bg"], fg=C["muted"], font=fh).pack(
        anchor="w", pady=(14, 4))
    takes_hdr = tk.Label(right, text="", bg=C["bg"], fg=C["muted"], font=fsm)
    takes_hdr.pack(anchor="w")
    takes_hdr.configure(text="every run is kept in ~/forge-lab/runs/  ·  click a "
                             "thumbnail to open it full size")
    # ---- rating filter + COMPARE controls (house toggle idiom; no Checkbutton anywhere) ----
    takes_ctl = tk.Frame(right, bg=C["bg"])
    takes_ctl.pack(anchor="w", pady=(2, 0))
    tk.Label(takes_ctl, text="show", bg=C["bg"], fg=C["faint"], font=fsm).pack(side="left")
    takes_filter_var = tk.StringVar(value="All")
    tf_menu = tk.OptionMenu(takes_ctl, takes_filter_var, *RATING_FILTERS,
                            command=lambda *_a: _refilter_takes())
    tf_menu.configure(bg=C["panel2"], fg=C["ink"], activebackground=C["rust"],
                      activeforeground="#fff", relief="flat", highlightthickness=0,
                      font=fsm)
    # ⚠️ the POPUP is its own tk.Menu and ignores the button's colours — macOS would flash a
    # white panel out of a near-black app unless it is configured explicitly.
    tf_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
    tf_menu.pack(side="left", padx=(6, 10))
    unkept_btn = tk.Button(takes_ctl, text="unkept only", bg=C["panel2"], fg=C["ink"],
                           activebackground=C["rust"], activeforeground="#fff",
                           relief="flat", font=fsm, padx=8, pady=1, borderwidth=0)
    unkept_btn.pack(side="left")
    cmp_btn = tk.Button(takes_ctl, text="COMPARE (0)", bg=C["panel2"], fg=C["ink"],
                        activebackground=C["rust"], activeforeground="#fff", relief="flat",
                        font=fsm, padx=8, pady=1, borderwidth=0)
    cmp_btn.pack(side="left", padx=(10, 0))
    take_wrap = tk.Frame(right, bg=C["bg"])
    take_wrap.pack(fill="both", expand=True, pady=(8, 0))
    tcanvas = tk.Canvas(take_wrap, bg=C["bg"], highlightthickness=0, yscrollincrement=1)
    tscroll = tk.Scrollbar(take_wrap, orient="vertical", command=tcanvas.yview,
                           troughcolor=C["bg"], bg=C["panel2"], borderwidth=0)
    tinner = tk.Frame(tcanvas, bg=C["bg"])
    twin = tcanvas.create_window((0, 0), window=tinner, anchor="nw")
    take_rows = []          # (label, buttons) per row, for the width reflow

    def _reflow_takes(event=None):
        """Pin the inner frame to the VISIBLE width and let the text wrap into what is
        left AFTER the buttons. Without this the rows request their natural width
        (~780px) and anything past the right edge is invisible — which is exactly how
        the take buttons went missing until the window was made full-screen. There is
        no horizontal scrollbar: off-screen means gone, silently."""
        w = tcanvas.winfo_width()
        if w <= 1:
            return
        try:
            tcanvas.itemconfigure(twin, width=w)
        except Exception:
            pass
        for lbl, btns in take_rows:
            try:
                want = max(160, w - btns.winfo_reqwidth() - 40)
                if int(lbl.cget("wraplength")) != want:
                    lbl.configure(wraplength=want)
            except Exception:
                pass
        try:
            takes_hdr.configure(wraplength=max(200, w - 20))
        except Exception:
            pass

    tcanvas.bind("<Configure>", _reflow_takes)
    tcanvas.configure(yscrollcommand=tscroll.set)
    tcanvas.pack(side="left", fill="both", expand=True)
    tscroll.pack(side="right", fill="y")
    def _sr_takes(e=None):
        before = tcanvas.yview()
        bbox = tcanvas.bbox("all")
        tcanvas.configure(scrollregion=bbox)
        after = tcanvas.yview()
        if before != after:
            _dbg("TAKES Configure bbox=%s yview %s -> %s   <<< VIEW MOVED BY LAYOUT"
                 % (tuple(bbox) if bbox else bbox, tuple(round(v,3) for v in before),
                    tuple(round(v,3) for v in after)))
    tinner.bind("<Configure>", _sr_takes)
    THUMBS = {}   # keep PhotoImage refs alive or tk drops them

    # ---------------------------------------------------------- interactions
    # ---- (video parameter rows now live up top, under the KIND/MODE dropdowns)

    def _lora_names():
        """Only offer LoRAs that are worth offering — the turbo/distill speed
        patches are wired in automatically and would be a footgun in the menu."""
        try:
            oi = get_json("/object_info", timeout=120)
            names = list(oi["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0])
        except Exception as exc:
            _log_err("lora list failed: %r" % exc)
            return []
        skip = ("turbo", "lightx2v", "distill", "step")
        return [n for n in names if not any(s in n.lower() for s in skip)]

    def load_loras():
        def work():
            q.put(("loras", _lora_names()))
        threading.Thread(target=work, daemon=True).start()

    def current_variant():
        key = st.get("video_mode", "r2v")
        return key, VIDEO_MODES.get(key, VIDEO_MODES["r2v"])

    def as_frame(path):
        """Take a path and return a still path. Videos are reduced to their LAST
        frame (the natural 'continue from here' input)."""
        if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
            return path
        outdir = os.path.join(DATA, "frames")
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, os.path.splitext(os.path.basename(path))[0] + "-last.png")
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.15", "-i", path,
                            "-vsync", "0", "-frames:v", "1", out],
                           capture_output=True, timeout=120)
        except Exception as exc:
            _log_err("frame extract failed: %r" % exc)
            return None
        if os.path.exists(out):
            _plog("extracted last frame from %s" % os.path.basename(path))
            return out
        runst.configure(text="could not read a frame from %s" % os.path.basename(path),
                        fg=C["ochre"])
        return None

    def relayout():
        """Show ONLY what the current KIND and MODEL actually use.

        Everything used to stay on screen and merely grey out, so the column was the same
        length in every mode and half of it was dead weight. Blocks are re-packed with
        `before=<anchor>` (a widget that is always packed) so the canonical order is restored
        without re-packing the whole column.
        """
        m = MODES[st["mode"]]
        is_vid = bool(m.get("video"))
        is_wan = bool(m.get("wan"))
        is_img = not is_vid
        if is_vid:
            vid_row.pack(fill="x", padx=16, pady=(8, 0), before=prompt_head)
            vid_hint.pack(anchor="w", padx=16, pady=(4, 0), before=prompt_head)
        else:
            vid_row.pack_forget()
            vid_hint.pack_forget()
        if is_vid and not is_wan:            # H3 only — WAN has neither a LoRA nor audio
            vid_row_lora.pack(fill="x", padx=16, pady=(4, 0), before=prompt_head)
            vid_row_audio.pack(fill="x", padx=16, pady=(4, 0), before=prompt_head)
        else:
            vid_row_lora.pack_forget()
            vid_row_audio.pack_forget()
        # CARD MODE is visible for EVERY mode — Studio matters everywhere (POLISH PROMPT uses
        # the writer), and Turbo must be VISIBLE-but-HONEST on a non-H3 engine: the hint says it
        # applies to H3 only and params() sends gpus=1 there. A control that lies is worse than
        # one that hides; this one shows and tells the truth. TESTCARDS asserts hint == params.
        vid_row_cards.pack(fill="x", padx=16, pady=(4, 0), before=prompt_head)
        free_row.pack(fill="x", padx=16, pady=(2, 0), before=prompt_head)
        apply_cards()
        # S2V has no LoRA and no on/off audio — it has a performance FILE and a chained LENGTH
        _is_s2v = is_wan and st.get("wan_mode") == "s2v"
        if _is_s2v:
            s2v_row.pack(fill="x", padx=16, pady=(4, 0), before=prompt_head)
            s2v_len_row.pack(fill="x", padx=16, pady=(4, 0), before=prompt_head)
        else:
            s2v_row.pack_forget()
            s2v_len_row.pack_forget()
        # the v2v engines read a SOURCE CLIP; nothing else in the app does
        if m.get("ltx") or m.get("animate2"):
            src_row.pack(fill="x", padx=16, pady=(4, 0), before=prompt_head)
            src_note_row.pack(anchor="w", padx=16, pady=(2, 0), before=prompt_head)
        else:
            src_row.pack_forget()
            src_note_row.pack_forget()
        # references: only when this MODEL can take one (t2i, H3 t2v and WAN t2v cannot)
        if _ref_cap():
            refbox.pack(fill="x", padx=16, pady=(14, 0), before=setbox)
        else:
            refbox.pack_forget()
        # ASPECT only shapes a text-to-image canvas; every edit mode follows image_1
        if is_img and st["mode"] == "t2i":
            aspect_lbl.pack(anchor="w", pady=(10, 2), before=qgrid)
            amenu.pack(anchor="w", fill="x", before=qgrid)
        else:
            aspect_lbl.pack_forget()
            amenu.pack_forget()
        # RESOLUTION is the edit modes' pixel budget; nothing else reads it
        if is_img and m.get("ref"):
            e_res.master.grid()
        else:
            e_res.master.grid_remove()

    def set_mode(m):
        # remember the SIZE we are leaving under the OUTGOING kind, then restore the incoming
        # kind's own — WAN's canvas is 480p, H3's is native, and they must not bleed into
        # each other (a WAN run at 1344x768 is untimed and much heavier than the measured one)
        _prev = st.pop("_size_key_prev", None) or size_key(st.get("mode"), st.get("wan_mode"))
        st.setdefault("size_by_kind", {})[_prev] = size_var.get()
        st["mode"] = m
        _kd = size_key(m, st.get("wan_mode"))
        _fill_size_menu(_kd)        # LTX/Animate 2 own their canvas sets (see _size_choices)
        if _kd != "image":
            size_var.set(st["size_by_kind"].get(_kd, SIZE_DEFAULT_BY_KIND[_kd]))
            # Setting the VARIABLE does not fire the OptionMenu's command, so the canvas has to
            # be applied explicitly. apply_vsize() now reads the per-KIND table, so this is the
            # single place that turns the SIZE label into WIDTH/HEIGHT.
            apply_vsize()
        MODES[m]["help"] and phelp.configure(
            text=model_help(m, st.get("video_mode"), st.get("wan_mode")))
        # KIND + MODEL follow the state (also covers settings restore and variant picks)
        _sync_dropdowns()
        if MODES[m].get("wan"):
            # WAN swaps in its own quality axis — a PATH, not a step slider
            _fill_quality_menu("wan")
            if qvar.get() not in _quality_labels():
                qvar.set(WAN_QUALITY_DEFAULT)
            apply_quality(qvar.get())
        elif MODES[m].get("video"):
            # ⚠️ THE REAL KIND, not the broad `True` — that flag is what let LTX/Animate 2 inherit
            # H3's presets. See _fill_quality_menu.
            _fill_quality_menu(_kd)
            if qvar.get() not in _quality_labels():
                # H3 keeps "Standard" as its default preset; a baked engine's only entry is its
                # readout, so it takes that rather than a preset it does not use.
                qvar.set("Standard" if "Standard" in _quality_labels()
                         else _quality_labels()[0])
                apply_quality()
        else:
            st["imode"] = m
            _fill_quality_menu("image")
            if qvar.get() not in _quality_labels():
                qvar.set(st.get("ipreset", "Medium"))
                apply_quality()
        is_ref = MODES[m]["ref"]
        # The reference LIST is always visible; only its CAP and hint change with the
        # mode, and _render_refs() owns both (via _ref_cap / _ref_help_text).
        e_res.configure(state="normal" if is_ref else "disabled")
        e_w.configure(state="disabled" if is_ref else "normal")
        e_h.configure(state="disabled" if is_ref else "normal")
        amenu.configure(state="disabled" if is_ref else "normal")
        is_vid = bool(MODES[m].get("video"))
        is_wan = bool(MODES[m].get("wan"))
        for row in (vid_row, vid_row_lora, vid_row_audio, s2v_row, s2v_len_row,
                    src_row, src_note_row):
            for w in row.winfo_children():
                try:
                    w.configure(state="normal" if is_vid else "disabled")
                except Exception:
                    pass          # labels have no state
        if is_wan:
            # WAN has NO user-selectable LoRA: the lightning distill is wired in by the
            # quality path, and character LoRAs are H3 ref2va-only.
            lora_var.set("(none)")
            lora_lbl.configure(text="LORA (built-in)")
            e_lstr.delete(0, "end"); e_lstr.insert(0, "1.0")
            if st.get("wan_mode") == "s2v":
                apply_s2v()           # fill the chunk note before the window is usable
        else:
            lora_lbl.configure(text="LORA")
            apply_audio()          # restore the note for the current audio value
        if MODES[m].get("ltx") or MODES[m].get("animate2"):
            apply_source()         # fill the clip readout before the window is usable
        if is_vid:
            if is_wan:
                _wk = st.get("wan_mode", WAN_VARIANT_DEFAULT)
                vid_hint.configure(text=WAN_MODES.get(_wk,
                                                      WAN_MODES[WAN_VARIANT_DEFAULT])["note"])
            else:
                _k, vm = current_variant()
                # ref requirements belong to the VARIANT, not the mode: t2v wants no
                # image, i2v/r2v want one, first+last wants two — _render_refs() shows
                # the cap and the wiring hint for whichever variant is selected
                vid_hint.configure(text=vm["note"])
            # the canvas IS the clip size and the duration drives the frame count,
            # so the image-only controls (resolution budget, aspect) are irrelevant
            e_w.configure(state="normal")
            e_h.configure(state="normal")
            e_res.configure(state="disabled")
            amenu.configure(state="disabled")
            # the canvas comes from the SIZE dropdown, not a hardcoded native size
            apply_vsize()
        cur = prompt.get("1.0", "end").strip()
        if is_ref and (not cur or cur == MODES["t2i"].get("prompt", "")):
            prompt.delete("1.0", "end")
            if MODES[m].get("prompt"):
                prompt.insert("1.0", MODES[m]["prompt"])
        if not is_ref:
            prompt.delete("1.0", "end") if cur.startswith("This is an RGBA") else None
        _render_refs()          # cap + hint follow the mode AND the video model
        relayout()              # show/hide the blocks this mode does not use
        refresh_estimate()

    def _ref_cap():
        """How many references the CURRENT mode can take — from the server's spec, so
        the UI can never offer more than the node accepts."""
        m = MODES[st["mode"]]
        if m.get("wan"):
            return WAN_MODES.get(st.get("wan_mode", WAN_VARIANT_DEFAULT),
                                 WAN_MODES[WAN_VARIANT_DEFAULT]).get("max", 0)
        # ⚠️ ORDER: ltx and animate2 also carry video=True. Without these two branches the cap
        # came from VIDEO_MODES (up to 9), so the picker offered 9 stills to an engine that uses
        # exactly 1 — submit() then silently used paths[:1]. Same bug class as estimate_seconds.
        if m.get("animate2"):
            return 1        # the still IS the character — one, and only one
        if m.get("ltx"):
            return 0        # the clip is the structure; this path takes no still
        if m.get("video"):
            return VIDEO_MODES.get(st.get("video_mode", "r2v"),
                                   VIDEO_MODES["r2v"]).get("max", 1)
        return m.get("max_refs", 0)

    def _ref_help_text():
        m = MODES[st["mode"]]
        # ⚠️ engines first, for the same reason as _ref_cap
        if m.get("animate2"):
            return ("ref 1 IS the character. The PERFORMANCE comes from the SOURCE VIDEO below — "
                    "the engine takes the pose from that clip rather than estimating it. Exactly "
                    "ONE still, and it should match the 480×848 canvas or the face is resized twice.")
        if m.get("ltx"):
            return ("LTX takes NO still: the SOURCE VIDEO below IS the structure it rebuilds. "
                    "Depth is extracted from your clip on the rig, so the look comes from the "
                    "prompt and the canvas must be divisible by 64.")
        if m.get("wan"):
            vm = WAN_MODES.get(st.get("wan_mode", WAN_VARIANT_DEFAULT),
                               WAN_MODES[WAN_VARIANT_DEFAULT])
            if vm.get("audio_file"):
                return ("ref 1 is the CHARACTER. S2V carries no LoRA, so this ONE still holds "
                        "the identity, the wardrobe and the look — and the PICTURE outvotes "
                        "the wardrobe words in the prompt. Match the SIZE canvas's aspect. "
                        "WAN does NOT use <Picture N> tags (that is H3's ref2va).")
            if vm["refs"] >= 2:
                return ("ref 1 IS the first frame and ref 2 the last one — the order decides "
                        "which is which. Give them the SAME aspect as the SIZE canvas. WAN "
                        "does NOT use <Picture N> tags (that is H3's ref2va).")
            if vm["refs"] == 1:
                return ("ref 1 IS the first frame the clip starts from. Give it the same "
                        "aspect as the SIZE canvas. WAN does not use <Picture N> tags.")
            return ("Not used — WAN t2v takes no image. Pick 'First frame (i2v)' to animate "
                    "one. NOTE: WAN cannot use a character LoRA (H3 ref2va only), so it "
                    "is for B-roll and backgrounds, not for the main character.")
        if m.get("video"):
            vm = VIDEO_MODES.get(st.get("video_mode", "r2v"), VIDEO_MODES["r2v"])
            if vm["refs"] >= 2:            # flf: positional meaning matters most
                return ("Address them in the prompt BY TAG, in the order added: ref 1 = "
                        "<Picture 1> (first frame), ref 2 = <Picture 2> (last frame). "
                        "Ref2va is very sensitive to this wording.")
            if vm.get("max", 0) > 1:       # r2v: the multi-reference mode
                return ("Address them in the prompt BY TAG, in the order added: ref 1 = "
                        "<Picture 1>, ref 2 = <Picture 2>… Say what each one IS and which "
                        "part of the shot it drives. ONE character → ONE clean still, "
                        "tagged. TWO characters → one still each, tagged separately — "
                        "UNTAGGED extra stills blend the faces into one.")
            if vm.get("refs", 0) == 1:     # i2v, cont: a single still
                return ("Address it in the prompt BY TAG as <Picture 1>. The tag order "
                        "IS the connection order.")
            return vm["note"]
        if m.get("ref"):
            return ("Mention them as <image1>, <image2>… <image1> is the edit target. "
                    "Up to %d stills." % QWEN_MAX_REFS)
        return ("Inactive — only Edit image, Remove background and the video modes "
                "use references.")

    def _render_refs():
        """Rebuild the reference list: one row per still (thumb, name, size, ✕)."""
        for w in ref_list.winfo_children():
            w.destroy()
        refs = list(st.get("refs") or [])
        cap = _ref_cap()
        # cap 0 means the mode uses NO references: ADD must be off, not just capped
        add_btn.configure(state="disabled" if (not cap or len(refs) >= cap) else "normal")
        if not cap:
            ref_count.configure(text="not used here")
        elif len(refs) > cap:
            # switching to a variant with a smaller cap must not silently look like all
            # of them count — say how many are USED out of how many are added
            ref_count.configure(text="%d of %d used" % (cap, len(refs)))
        else:
            ref_count.configure(text="%d of %d" % (len(refs), cap))
        ref_help.configure(text=_ref_help_text())
        if not refs:
            tk.Label(ref_list, text="none chosen", bg=C["panel"], fg=C["muted"],
                     font=fsm, anchor="w").pack(anchor="w")
            refresh_estimate()
            return
        for i, path in enumerate(refs):
            beyond = (not cap) or (i >= cap)          # added, but this mode won't use it
            row = tk.Frame(ref_list, bg=C["panel"])
            row.pack(fill="x", pady=2)
            # ⚠️ thumb_photo, never tk.PhotoImage(file=...) — a transparent PNG reference
            # (easy to pick: a cut-out still) hangs the app the same way a bg take does.
            _rt = thumb_photo(path, 56)
            if _rt is not None:
                THUMBS["ref%d" % i] = _rt
                tk.Label(row, bg=C["panel"], image=_rt).pack(side="left")
            else:
                tk.Label(row, text="[img]", bg=C["panel"], fg=C["muted"],
                         font=fsm).pack(side="left")
            wh = image_size(path)
            tk.Label(row, text="%d. %s%s%s" % (
                i + 1, os.path.basename(path),
                ("  %d×%d" % wh) if wh else "",
                "  · not used here" if beyond else ""),
                bg=C["panel"], fg=C["muted"] if beyond else C["ink"], font=fsm,
                anchor="w", wraplength=240, justify="left").pack(side="left", padx=6)
            tk.Button(row, text="✕", bg=C["panel"], fg=C["muted"],
                      activebackground=C["rust_deep"], activeforeground="#fff",
                      relief="flat", font=fsm, padx=5, pady=0, borderwidth=0,
                      command=lambda k=i: remove_ref(k)).pack(side="right")
        refresh_estimate()

    def add_ref(path):
        cap = _ref_cap()
        if not cap:
            runst.configure(text="this mode does not use references", fg=C["ochre"])
            return
        refs = list(st.get("refs") or [])
        if len(refs) >= cap:
            runst.configure(text="%d references is the limit here" % cap, fg=C["ochre"])
            return
        refs.append(path)
        st["refs"] = refs
        _plog("ref add: %s (%d of %d)" % (os.path.basename(path), len(refs), cap))
        _render_refs()

    def remove_ref(i):
        refs = list(st.get("refs") or [])
        if 0 <= i < len(refs):
            gone = refs.pop(i)
            st["refs"] = refs
            _plog("ref remove: %s (%d left)" % (os.path.basename(gone), len(refs)))
        _render_refs()

    def choose_ref():
        """Adds the next reference. A VIDEO also works: its last frame is extracted,
        which is what 'continue a clip' wants."""
        p = filedialog.askopenfilename(
            title="Reference image (or a clip — its last frame is used)",
            filetypes=[("Images or video", "*.png *.jpg *.jpeg *.webp *.mp4 *.mov *.webm"),
                       ("All files", "*.*")])
        if not p:
            return
        f = as_frame(p)
        if f:
            add_ref(f)

    def params():
        def num(w, d):
            try:
                return int(float(w.get()))
            except Exception:
                return d
        _pm = MODES[st["mode"]]
        p = {"prompt": prompt.get("1.0", "end").strip(),
                "width": num(e_w, 1024), "height": num(e_h, 1024),
                # ⚠️ LTX and Animate 2 BAKE their own step counts, so the UI step entry must
                # not reach the estimate for them — it read "12 steps" for an engine that runs 8.
                "steps": (int(MODES[st["mode"]]["steps"])
                          if (MODES[st["mode"]].get("ltx") or MODES[st["mode"]].get("animate2"))
                          else num(e_steps, 25)),
                "resolution": num(e_res, 1024),
                "seed": num(e_seed, -1), "prefix": e_prefix.get() or "forge_lab",
                "seconds": num(e_secs, 3.0),
                "lora": (None if lora_var.get() in ("", "(none)") else lora_var.get()),
                "lora_strength": num(e_lstr, 1.0),
                "video_mode": st.get("video_mode", "r2v"),
                # Raylight: 2 cards for a SINGLE H3 render ('Turbo'). For EVERY other engine
                # this is 1 — Turbo only means anything on H3, and the row's hint says so.
                # ⚠️ This MUST equal _cards_hint()'s claim; TESTCARDS asserts they agree.
                "gpus": (2 if (cards_var.get() == "Turbo"
                               and engine_of(st["mode"]) == "h3") else 1),
                # the FREE CARD 2 setting travels WITH the run, so the worker's preflight
                # cannot disagree with what the row said.
                "free_card": bool(free_card_var.get()),
                "wan_mode": st.get("wan_mode", WAN_VARIANT_DEFAULT),
                # for WAN this IS the path (turbo/full), not a cosmetic label
                "quality": qvar.get(),
                # H3 only — WAN builds no audio regardless
                "audio": audio_var.get() != "off",
                # S2V: the performance file (uploaded by submit()) and the chained length
                "audio_path": st.get("audio_path"),
                "s2v_chunks": s2v_chunks_now(),
                # the v2v engines' driving clip + the frame count read off it at pick time
                "source_path": st.get("source_path"),
                "src_frames": st.get("src_frames"),
                "ref_paths": list(st.get("refs") or [])}
        # ⚠️ `length` is a PER-ENGINE override and must NOT be set blanket. Every consumer reads
        # `p.get("length") or <seconds-derived frames>`, so a blanket value silently pinned the
        # DURATION field out of the picture — H3 and WAN were stuck at 81 frames (a 6 s shot
        # estimated 247 s instead of 628 s) while the field still looked live. Only the engine
        # that OWNS a length gets the key: Animate 2's single pass, or LTX's clip-derived length.
        if _pm.get("animate2"):
            p["length"] = ANIMATE2_LENGTH
        elif _pm.get("ltx"):
            p["length"] = ltx_length_for(p.get("src_frames"))
        return p

    def refresh_estimate(*_):
        p = params()
        m = MODES[st["mode"]]
        try:
            est = estimate_seconds(st["mode"], p, st.get("refs"),
                                   warm_engine=st.get("warm_engine"))
        except Exception:
            est = 0
        st["est"] = est
        if m.get("wan"):
            if st.get("wan_mode") == "s2v":
                _n = s2v_chunks_now()
                _fr = s2v_chunks_to_frames(_n)
                _r = wan_rules("s2v", None)
                _af = os.path.basename(st.get("audio_path") or "")
                dim_lbl.configure(text=(
                    "%d × %d px  ·  %d chunk(s) → %d frames = %.2f s @16fps  ·  SYNC AUDIO\n"
                    "%d steps  ·  cfg %.1f  ·  shift %.1f  ·  lightning LoRA ON"
                    "  ·  audio: %s" % (
                        p["width"], p["height"], _n, _fr, _fr / S2V_FPS,
                        _r["steps"], _r["cfg"], _r["shift"], _af or "NONE CHOSEN")))
            else:
                _len = int(p.get("length") or wan_frames(p.get("seconds", 5.0)))
                _qk = p.get("quality") if p.get("quality") in WAN_QUALITY else WAN_QUALITY_DEFAULT
                _r = wan_rules(st.get("wan_mode", WAN_VARIANT_DEFAULT), _qk)
                dim_lbl.configure(text=(
                    "%d × %d px  ·  %d frames = %.2f s @16fps  ·  NO audio\n"
                    "%s  ·  shift %.1f  ·  cfg %.1f  ·  %s" % (
                        p["width"], p["height"], _len, _len / WAN_FPS, _qk, _r["shift"], _r["cfg"],
                        "lightning LoRAs ON" if _r["turbo"] else "no LoRA (full path)")))
        elif m.get("ltx"):
            # ⚠️ engines first: both carry video=True, so without these the dims line described
            # H3's frame math (24fps, a character LoRA, an audio track) over a run that has none.
            _lf = ltx_length_for(p.get("src_frames"))
            dim_lbl.configure(text="%d × %d px  ·  %d source frames = %.2f s @25fps  ·  "
                                   "structure from the source clip" % (
                p["width"], p["height"], _lf, _lf / 25.0))
        elif m.get("animate2"):
            _af = int(p.get("length") or ANIMATE2_LENGTH)
            dim_lbl.configure(text="%d × %d px  ·  one %d-frame pass = %.2f s @24fps  ·  "
                                   "picture only (no audio)" % (
                p["width"], p["height"], _af, _af / 24.0))
        elif m.get("video"):
            _len = int(p.get("length") or h3_frames(p.get("seconds", 3.0)))
            _mp = int(p["width"]) * int(p["height"]) / 1e6
            # Outside the measured envelope the number is a FLOOR, not a fit — say so here
            # rather than let it read as a promise. (A 241 fr / native run was 2.24x the old
            # estimate; the whole reason this flag exists.)
            _env = ("  ·  ⚠ beyond the measured range (>124 frames or >1.03 MP)"
                    " — treat as a FLOOR"
                    if h3_estimate_extrapolated(_len, _mp) else "")
            dim_lbl.configure(text="%d × %d px  ·  %d frames = %.2f s @24fps  ·  LoRA: %s  ·  "
                                   "audio: %s%s" % (
                p["width"], p["height"], _len, _len / 24.0, p.get("lora") or "none",
                "on" if p.get("audio", True) else "OFF (silent clip)", _env))
        elif m["ref"]:
            res = p["resolution"]
            wh = image_size(st["refs"][0]) if (st.get("refs") or []) else None
            size_txt = ("canvas: native size of image_1" if not res else
                        "canvas: follows image_1 at a %d px budget" % res)
            dim_lbl.configure(text=size_txt + (("  ·  source %d×%d" % wh) if wh else ""))
        else:
            mp = p["width"] * p["height"] / 1e6
            dim_lbl.configure(text="%d × %d px = %.2f MP%s" % (
                p["width"], p["height"], mp,
                "   ⚠ above the model's native 2K" if mp > 4.2 else ""))
        _sw = estimate_is_switch(st["mode"], st.get("warm_engine"))
        _basis = (("incl. %s engine load" % fmt(engine_load_s(st["mode"]))) if _sw
                  else "warm — same engine as the last run")
        if not st["active"]:
            _basis += "   ·   nothing has been submitted yet"
        _own = sum(float(i.get("est") or 0) for i in st["active"].values())
        _qn = queue_note(st.get("queue_ahead", 0), _own)
        est_lbl.configure(text="~%s   ·   %s   ·   %d steps\n%s%s" % (
            fmt(est), MODES[st["mode"]]["label"], p["steps"], _basis,
            ("\n" + _qn) if _qn else ""))
        qc = QUALITY.get(qvar.get()) or {}
        if qc.get("steps") is not None:
            drift = int(p["steps"]) != qc["steps"]
            if m["ref"]:
                if qc.get("resolution") and p["resolution"] != qc["resolution"]:
                    drift = True
            else:
                mp = p["width"] * p["height"] / 1e6
                if qc.get("mp") and abs(mp - qc["mp"]) / max(0.01, qc["mp"]) > 0.12:
                    drift = True
            if drift:
                qvar.set("Custom")
                q_hint.configure(text=QUALITY["Custom"]["note"])

    def fmt(s):
        s = max(0, int(round(s)))
        return ("%dm %02ds" % (s // 60, s % 60)) if s >= 60 else "%ds" % s

    # (the mode BUTTONS are gone — KIND + MODE dropdowns replaced them, up top)
    _pol = {"original": None, "t0": 0.0, "cleared": 0.0}

    def do_polish():
        """Hand the idea to the prompt enhancer and put the rewrite in the box for
        review. Nothing is submitted to the image model automatically."""
        txt = prompt.get("1.0", "end").strip()
        if not txt:
            pol_msg.configure(text="type an idea first", fg=C["ochre"]); return
        _pol["original"] = prompt.get("1.0", "end").rstrip("\n")
        _pol["t0"] = time.time()
        polish_btn.configure(state="disabled")
        pol_msg.configure(text="polishing…", fg=C["muted"])
        _plog("press: %d chars in, mode=%s" % (len(txt), st["mode"]))

        def work():
            try:
                if (cards_var.get() == "Studio"
                        and os.environ.get("FORGE_LAB_NODIRECTOR") != "1"):
                    # Studio: the writer on card 2 rewrites in the CURRENT engine's dialect,
                    # instead of the Qwen enhancer. Fall back to the enhancer if the writer is
                    # unreachable, so POLISH never becomes a dead button.
                    try:
                        out = director_polish(txt, st["mode"], st.get("video_mode"))
                    except Exception as _exc:
                        _plog("director polish failed (%r) - falling back to the enhancer"
                              % _exc)
                        out = polish_prompt(txt, mode=st["mode"])
                else:
                    out = polish_prompt(txt, mode=st["mode"])
                _plog("worker: engine returned %d chars" % len(out or ""))
                _dbg("polish: engine returned %d chars" % len(out or ""))
                q.put(("polished", out, None))
            except Exception as exc:
                _plog("worker: engine raised %r" % exc)
                _dbg("polish: engine raised %r" % exc)
                q.put(("polished", None, "%s: %s" % (type(exc).__name__, exc)))
        threading.Thread(target=work, daemon=True).start()

    def do_revert():
        if _pol["original"] is None:
            return
        prompt.delete("1.0", "end")
        prompt.insert("1.0", _pol["original"])
        _pol["original"] = None
        revert_btn.configure(state="disabled")
        pol_msg.configure(text="reverted to your wording", fg=C["muted"])
        refresh_estimate()

    def do_clear():
        """Empty the prompt box. Whatever was there is stashed into REVERT, so a
        mis-click costs one more click, not your wording."""
        had = prompt.get("1.0", "end").rstrip("\n")
        _pol["cleared"] = time.time()          # anything polishing now is stale
        prompt.delete("1.0", "end")
        if had.strip():
            _pol["original"] = had
            revert_btn.configure(state="normal")
            pol_msg.configure(text="cleared — REVERT puts it back", fg=C["muted"])
        else:
            # Already empty: leave the stash ALONE. Wiping it here would destroy the
            # text an earlier clear saved, so REVERT would restore nothing.
            # (Caught by FORGE_LAB_TESTCLEAR: after clear → clear → revert, box=0.)
            pol_msg.configure(text="prompt was already empty", fg=C["muted"])
        _plog("clear: dropped %d chars" % len(had))
        refresh_estimate()

    def promote_take(src_path, target_label, model=None):
        """Upscale a finished take and file it as a NEW take. Rides the same
        worker/queue path as a render, so the run panel tracks it and it lands in
        Takes when it finishes. Video keeps its frames + audio; images promote by scale.
        `model` picks the ESRGAN checkpoint — photo vs cel/anime — and defaults to the
        photo one, which is right for the photoreal pieces made here."""
        mdl = model or PROMOTE_MODEL
        is_img = src_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        if is_img:
            mult = IMAGE_PROMOTE.get(target_label, 2.0)
            wh = image_size(src_path)
            tw, th = (int(round(wh[0] * mult)), int(round(wh[1] * mult))) if wh else (0, 0)
            out_desc = ("%d×%d" % (tw, th)) if tw else "4x native"
            est = IMAGE_PROMOTE_ESTIMATE.get(target_label)
        else:
            tw, th = PROMOTE_TARGETS[target_label]
            out_desc = "%sx%s" % (tw, th)
            est = PROMOTE_ESTIMATE.get(target_label)
        runst.configure(text="promoting %s → %s …" % (os.path.basename(src_path), out_desc),
                        fg=C["muted"])
        _plog("promote: %s -> %s (%dx%d, %s, model %s)"
              % (os.path.basename(src_path), target_label, tw, th,
                 "image" if is_img else "video", mdl))

        def work():
            try:
                name = upload_image(src_path)      # same multipart path; any file type
                _plog("promote: staged on forge as %s" % name)
                g = (image_promote_graph(name, tw, th, mdl) if is_img
                     else promote_graph(name, tw, th, mdl))
                r = post_json("/prompt", {"prompt": g, "client_id": CLIENT_ID})
                pid = r.get("prompt_id")
                if not pid:
                    raise RuntimeError("forge returned no prompt_id: %r" % r)
                _index_insert({
                    "id": pid, "mode": "promote",
                    "label": "PROMOTED → %s" % out_desc,
                    "prompt": "(upscale of %s)" % os.path.basename(src_path),
                    "seed": "—", "prefix": "promoted", "steps": 0,
                    "resolution": tw or 0, "width": tw or 0, "height": th or 0,
                    "video": not is_img, "promote": True,
                    "source": os.path.basename(src_path),
                    "started": time.time(), "status": "running", "images": []})
                q.put(("submitted", pid, "—", est, "upscale"))
            except Exception as exc:
                _plog("promote FAILED: %r" % exc)
                q.put(("failed", "%s: %s" % (type(exc).__name__, exc)))
        threading.Thread(target=work, daemon=True).start()

    def promote_dialog(it, src_path):
        """Ask for the target before spending anything — same rule as the render panel."""
        top = tk.Toplevel(root)
        top.title("Promote take")
        top.configure(bg=C["panel"])
        try:
            top.transient(root)
        except Exception:
            pass
        is_img = src_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        targets = IMAGE_PROMOTE if is_img else PROMOTE_TARGETS
        ests = IMAGE_PROMOTE_ESTIMATE if is_img else PROMOTE_ESTIMATE
        wh = image_size(src_path) if is_img else None
        sw, sh = wh if wh else (it.get("width") or "?", it.get("height") or "?")
        tk.Label(top, text="PROMOTE  %s" % os.path.basename(src_path), bg=C["panel"],
                 fg=C["ink"], font=fh).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(top, text=("source %sx%s  →  upscale 4x, then fit to the target size\n"
                            % (sw, sh))
                 + ("This is a still — one frame, so it is quick.\n" if is_img else
                    "Frames + audio are carried through, so the motion stays\n"
                    "identical to this take.\n")
                 + "It sharpens what is there; it does not invent detail.",
                 bg=C["panel"], fg=C["muted"], font=fsm, justify="left").pack(
            anchor="w", padx=16, pady=(4, 10))

        # ── WHICH ESRGAN. Photo and cel are different tools; the default used to be cel
        # for everything, which was wrong for the photoreal work. Pick it here. ──────────
        mdl_row = tk.Frame(top, bg=C["panel"])
        mdl_row.pack(anchor="w", padx=16, pady=(0, 10))
        tk.Label(mdl_row, text="MODEL", bg=C["panel"], fg=C["ochre"], font=fmono).pack(
            side="left", padx=(0, 8))
        mdl_var = tk.StringVar(value=next(k for k, v in PROMOTE_MODELS.items()
                                          if v == PROMOTE_MODEL))
        mdl_menu = tk.OptionMenu(mdl_row, mdl_var, *PROMOTE_MODELS)
        mdl_menu.configure(bg=C["panel2"], fg=C["ink"], relief="flat", font=fsm,
                           highlightthickness=0, activebackground=C["rust"],
                           activeforeground="#fff", borderwidth=0, width=38,
                           highlightbackground=C["panel"])
        mdl_menu["menu"].configure(bg=C["panel2"], fg="#000000", font=fsm)
        mdl_menu.pack(side="left")

        rowb = tk.Frame(top, bg=C["panel"])
        rowb.pack(anchor="w", padx=16, pady=(0, 14))

        def go(label):
            top.destroy()
            promote_take(src_path, label,
                         PROMOTE_MODELS.get(mdl_var.get(), PROMOTE_MODEL))

        for label in targets:
            if is_img and wh:
                shown = "%s  →  %d×%d" % (label, int(round(wh[0] * targets[label])),
                                          int(round(wh[1] * targets[label])))
            else:
                shown = label
            tk.Button(rowb, text="%s   ~%ds" % (shown, ests.get(label, 0)),
                      bg=C["rust_deep"], fg="#fff", activebackground=C["rust_hi"],
                      activeforeground="#fff", relief="flat", font=fsm, padx=12,
                      pady=7, borderwidth=0,
                      command=lambda l=label: go(l)).pack(side="left", padx=(0, 8))
        tk.Button(rowb, text="CANCEL", bg=C["panel2"], fg=C["ink"], relief="flat",
                  font=fsm, padx=12, pady=7, borderwidth=0,
                  highlightthickness=1, highlightbackground=C["line"],
                  command=top.destroy).pack(side="left")
        tk.Label(top, text=("estimates, not promises — 2K is still untimed" if not is_img
                            else "estimates, not promises — a still takes seconds"),
                 bg=C["panel"], fg=C["muted"], font=fsm).pack(anchor="w", padx=16,
                                                              pady=(0, 14))

    def do_run():
        p = params()
        if not p["prompt"]:
            runst.configure(text="a prompt is required", fg=C["ochre"]); return
        if MODES[st["mode"]]["ref"] and not (st.get("refs") or []):
            runst.configure(text="this mode needs a reference image", fg=C["ochre"]); return
        run_btn.configure(state="disabled")
        runst.configure(text="submitting — uploading the reference if needed…", fg=C["muted"])
        est_now = st.get("est")
        _plog("RUN press: mode=%s steps=%s seed=%s prompt=%d chars ref=%s"
              % (st["mode"], p.get("steps"), p.get("seed"), len(p["prompt"]),
                 [os.path.basename(x) for x in (st.get("refs") or [])][:3]))

        def work():
            try:
                if p.get("gpus") == 2:
                    # ⚠️ In the WORKER thread, never the Tk thread: the unload + re-check
                    # blocks for up to ~10 s. Ollama is pinned to GPU1 (~19 GB); a 2-card
                    # Raylight render needs ~21 GB on that same card, so a resident writer is
                    # an OOM — refuse rather than submit into it.
                    try:
                        from director import Director
                        _ok, _msg = turbo_preflight(Director(),
                                                    free_card=bool(p.get("free_card", True)))
                    except Exception as _exc:
                        _ok, _msg = True, ("writer check unavailable (%s)"
                                           % type(_exc).__name__)
                    _plog("turbo preflight: ok=%s %s" % (_ok, _msg))
                    if not _ok:
                        q.put(("failed", _msg))
                        return
                _eng = engine_of(st["mode"])
                pid, seed = submit(st["mode"], p, st.get("refs"))
                _plog("submit ok: %s seed=%s" % (pid[:8], seed))
                q.put(("submitted", pid, seed, est_now, _eng))
            except Exception as e:
                _plog("submit FAILED: %s: %s" % (type(e).__name__, e))
                q.put(("failed", "%s: %s" % (type(e).__name__, e)))
        threading.Thread(target=work, daemon=True).start()

    run_btn.configure(command=do_run)

    def cancel_my_queued():
        """⚠️ Only THIS app's runs (st["active"]) — never a blanket clear of forge's queue."""
        pids = list(st["active"].keys())
        runst.configure(text="asking forge to drop your waiting runs…", fg=C["muted"])

        def work():
            try:
                q.put(("cancelled", cancel_queued(pids)))
            except Exception as e:
                q.put(("failed", "cancel: %s: %s" % (type(e).__name__, e)))
        threading.Thread(target=work, daemon=True).start()
    cancelq_btn.configure(command=cancel_my_queued)
    for w in (e_w, e_h, e_steps, e_res, e_seed):
        w.bind("<KeyRelease>", refresh_estimate)

    # --------------------------------------------------------------- pumping
    def drain():
        st["drain_n"] = st.get("drain_n", 0) + 1
        st["drain_last"] = time.time()
        if st["drain_n"] % 300 == 0:
            _plog("drain heartbeat: %d passes" % st["drain_n"])
        try:
            # A take landed somewhere (any window, any worker thread) -> redraw the list once
            # per tick. A FLAG rather than a direct load_takes() call: _index_insert() runs on
            # worker threads and Tk is main-thread-only, and a 12-scene Long-video batch lands
            # 24 takes but should repaint the list ONCE. load_takes() is defined below drain()
            # in this same scope, so it is resolved at call time, not def time.
            if st.pop("_takes_dirty", False):
                load_takes()
            while True:
                msg = q.get_nowait()
                # Each message is isolated. Previously ONE bad handler could take
                # the whole pump down: the reschedule below was skipped, so takes
                # and results sat in the queue forever and the UI went stale.
                try:
                    if msg[0] == "submitted":
                        _plog("drain: submitted %s" % msg[1][:8])
                        st["seed_used"] = msg[2]
                        st["active"][msg[1]] = {"t": time.time(),
                                                "est": msg[3] if len(msg) > 3 else None}
                        st["focus"] = msg[1]
                        # the engine that just loaded is now resident — the next estimate
                        # for the SAME engine drops its load term, a different one keeps it
                        if len(msg) > 4 and msg[4]:
                            st["warm_engine"] = msg[4]
                            refresh_estimate()
                        runst.configure(text="sampling on forge — seed %s  ·  %d run(s) in flight"
                                        % (msg[2], len(st["active"])), fg=C["muted"])
                        run_btn.configure(state="normal")   # queue another if you like
                    elif msg[0] == "cancelled":
                        for _pid in msg[1]:
                            st["active"].pop(_pid, None)
                        if st.get("focus") not in st["active"]:
                            st["focus"] = next(iter(st["active"]), None)
                        runst.configure(text=("cancelled %d waiting run(s)" % len(msg[1]))
                                        if msg[1] else
                                        "nothing of yours was waiting in forge's queue",
                                        fg=C["ochre"])
                        load_takes()
                    elif msg[0] == "failed":
                        _plog("drain: failed — %s" % msg[1])
                        runst.configure(text="forge refused: " + msg[1], fg=C["rust"])
                        run_btn.configure(state="normal")
                    elif msg[0] == "takes":
                        render_takes(msg[1])
                    elif msg[0] == "polished":
                        _plog("drain: received polished (len=%s)"
                              % (len(msg[1]) if msg[1] else 0))
                        polish_btn.configure(state="normal")
                        if msg[1] and _pol.get("t0", 0.0) < _pol.get("cleared", 0.0):
                            # a rewrite started before the box was cleared — dropping
                            # it is the whole point of the clear, so say so out loud
                            _plog("drain: polished DISCARDED (box was cleared)")
                            pol_msg.configure(text="polish finished after you cleared "
                                                   "— discarded", fg=C["ochre"])
                        elif msg[1]:
                            prompt.delete("1.0", "end")
                            prompt.insert("1.0", msg[1])
                            revert_btn.configure(state="normal")
                            pol_msg.configure(text="rewritten — edit it, then Run "
                                                   "(REVERT restores your wording)",
                                              fg=C["moss"])
                            refresh_estimate()
                        else:
                            pol_msg.configure(text=(msg[2] or "polish failed")[:120],
                                              fg=C["rust"])
                    elif msg[0] == "lost":
                        runst.configure(text="run %s is on neither forge's queue nor "
                                             "its history — it was cancelled or dropped"
                                             % msg[1][:8], fg=C["ochre"])
                    elif msg[0] == "recovered":
                        runst.configure(text="recovered %d finished run(s) from forge — "
                                             "they are in Takes" % len(msg[1]), fg=C["moss"])
                        load_takes()
                    elif msg[0] == "loras":
                        names = msg[1] or []
                        menu = lora_menu["menu"]
                        menu.delete(0, "end")
                        menu.add_command(label="(none)",
                                         command=lambda: lora_var.set("(none)"))
                        for n in names:
                            menu.add_command(label=n, command=lambda n=n: lora_var.set(n))
                        _plog("loras offered: %d" % len(names))
                    elif msg[0] == "status":
                        _show_status(msg[1], msg[2])
                    elif msg[0] == "done":
                        _plog("drain: done — %s" % msg[1])
                        runst.configure(text=msg[1], fg=C["moss"])
                        if len(msg) > 2 and msg[2]:
                            elapsed.configure(text=fmt(msg[2]))   # freeze at true wall time
                        run_btn.configure(state="normal")
                        load_takes()
                    else:
                        # Unknown message types are logged, never dropped in silence.
                        _plog("drain: unknown message type %r" % (msg[0],))
                except Exception as exc:
                    _log_err("drain handler for %r failed: %r" % (msg[0], exc))
                    _plog("drain handler for %r failed: %r" % (msg[0], exc))
        except queue.Empty:
            pass
        except Exception as exc:
            _log_err("drain loop failed: %r" % exc)
            _plog("drain loop failed: %r" % exc)
        finally:
            root.after(120, drain)

    def _wd():
        """Watchdog: if the pump ever stops calling itself, restart it. Cheaper than
        finding out why the UI went quiet."""
        if time.time() - st.get("drain_last", 0) > 3:
            _plog("watchdog: the pump had stopped — restarting it")
            drain()
        root.after(3000, _wd)

    # ⚠️ gpu_stats() is an ssh round-trip (0.2 s up, 3.0 s when forge is unreachable — measured).
    # track() runs on the Tk thread every 1.2 s, so calling it there froze the window. Poll it
    # in a thread and let track() read the last answer. Guarded by tools/ui_matrix.py §3.
    _gpu_cache = {"rows": None, "busy": False}

    def _gpu_poll():
        if _gpu_cache["busy"]:
            return
        _gpu_cache["busy"] = True

        def work():
            try:
                _gpu_cache["rows"] = gpu_stats()
            finally:
                _gpu_cache["busy"] = False
        threading.Thread(target=work, daemon=True).start()

    def track():
        # Poll EVERY run in flight. Tracking only the newest pid meant a second
        # Run press abandoned the first one: it finished on forge and was never
        # collected, while the index kept it listed as "running" forever.
        for pid in list(st["active"].keys()):
            info = st["active"][pid]
            try:
                s = run_state(pid)
            except Exception:
                continue
            age = time.time() - info["t"]
            if info.get("last_state") != s["state"]:
                info["last_state"] = s["state"]
                _plog("run %s: %s at %.0fs" % (pid[:8], s["state"], age))
            if s["state"] == "done":
                del st["active"][pid]
                if st.get("focus") == pid:
                    st["focus"] = next(iter(st["active"]), None)
                canvas_bar.coords(bar, 0, 0, canvas_bar.winfo_width(), 9)
                runst.configure(text="finished — pulling the image down to this Mac",
                                fg=C["moss"])

                def work(pid=pid):
                    try:
                        meta = collect(pid)
                        _plog("collect %s: %d image(s)" % (
                            pid[:8], len((meta or {}).get("images") or [])))
                        q.put(("done", "finished in %s  ·  saved to ~/forge-lab/runs/%s"
                               % (fmt(meta.get("wall", 0)) if meta else "?", pid[-8:]),
                               (meta or {}).get("wall")))
                    except Exception as e:
                        _plog("collect %s FAILED: %r" % (pid[:8], e))
                        q.put(("done", "collect failed: %s" % e))
                threading.Thread(target=work, daemon=True).start()
            elif s["state"] == "error":
                del st["active"][pid]
                if st.get("focus") == pid:
                    st["focus"] = next(iter(st["active"]), None)
                runst.configure(text="execution error: " + (s.get("detail") or ""),
                                fg=C["rust"])
            elif s["state"] == "gone":
                # ABSENCE IS NOT PROOF. forge can briefly show a prompt in neither
                # the queue nor history while it hands off between them — declaring
                # "vanished" on that gap abandoned a run that had actually
                # succeeded. Require a sustained absence, then try to collect it
                # anyway before giving up.
                info["misses"] = info.get("misses", 0) + 1
                if info["misses"] >= 10 and age > 20:
                    del st["active"][pid]
                    if st.get("focus") == pid:
                        st["focus"] = next(iter(st["active"]), None)

                    def work(pid=pid):
                        m = None
                        try:
                            m = collect(pid)
                        except Exception:
                            pass
                        q.put(("recovered", [pid]) if (m or {}).get("images")
                              else ("lost", pid))
                    threading.Thread(target=work, daemon=True).start()

        pid = st.get("focus")
        if pid and pid in st["active"]:
            info = st["active"][pid]
            elapsed.configure(text=fmt(time.time() - info["t"]))
            pr = PROGRESS.get(pid) or {}
            if pr.get("value") and pr.get("max"):
                frac = pr["value"] / float(pr["max"])
                canvas_bar.coords(bar, 0, 0, canvas_bar.winfo_width() * frac, 9)
                steptxt.configure(text="step %s / %s  ·  node %s"
                                       % (pr["value"], pr["max"], pr.get("node")))
            else:
                steptxt.configure(text="sampling — no live step feed from forge")
        elif st["active"]:
            st["focus"] = next(iter(st["active"]), None)

        _gpu_poll()
        g = _gpu_cache["rows"]
        if g:
            g = g[0]
            gpu_lbl.configure(text="gpu %d%%   ·   vram %.1f / %.0f GB   ·   %.0f W   ·   %d °C"
                                   % (g["util"], g["used"] / 1024.0, g["total"] / 1024.0,
                                      g["power"], g["temp"]))
        else:
            gpu_lbl.configure(text="gpu telemetry unavailable (ssh)")
        root.after(1200, track)

    def poll_status():
        def work():
            try:
                ss = get_json("/system_stats", timeout=6)
                dev = (ss.get("devices") or [{}])[0]
                qu = get_json("/queue", timeout=6)
                q.put(("status", dev, qu))
            except Exception as e:
                q.put(("status", None, str(e)))
        threading.Thread(target=work, daemon=True).start()
        root.after(4000, poll_status)

    _status = {"dev": None, "qu": None}

    def _show_status(dev=None, qu=None):
        if dev is not None or qu is not None:
            _status["dev"], _status["qu"] = dev, qu
        dev, qu = _status["dev"], _status["qu"]
        # queue depth for the estimate panel — re-render it only when the number CHANGES
        # (this runs every 300 ms)
        _ahead = ((len(qu.get("queue_running") or []) + len(qu.get("queue_pending") or []))
                  if isinstance(qu, dict) else 0)
        if _ahead != st.get("queue_ahead", 0):
            st["queue_ahead"] = _ahead
            try:
                refresh_estimate()
            except Exception:
                pass
        # Report the SOCKET state, not when a message last arrived: forge is silent
        # whenever idle, so recency flagged "no feed" on a healthy connection.
        if WS_CONNECTED:
            streaming = (time.time() - WS_LAST) < 20
            ws_txt.configure(text=("progress: streaming" if streaming
                                   else "progress: connected"), fg=C["moss"])
        else:
            ws_txt.configure(text="progress: reconnecting…", fg=C["ochre"])
        if dev:
            dot.configure(fg=C["moss"])
            stxt.configure(text="forge up" + (("  ·  %d running, %d queued"
                             % (len(qu.get("queue_running") or []),
                                len(qu.get("queue_pending") or [])))
                             if isinstance(qu, dict) else ""))
            gtxt.configure(text=dev.get("name", "")[:34])
        else:
            dot.configure(fg=C["rust"])
            stxt.configure(text="forge unreachable — retrying")
            gtxt.configure(text="")

    def apply_status():
        """Refresh the connection readout only.

        IT MUST NOT TOUCH THE QUEUE. This used to drain the SAME queue as drain()
        and silently throw away every message that was not "status" — which is how
        finished runs and polish results vanished: a 300 ms timer racing the pump
        and discarding whatever it won. Two consumers, one queue, one of them blind.
        """
        _show_status()
        root.after(300, apply_status)

    # Tk 9 delivers TRACKPAD two-finger scrolling as <TouchpadScroll>, a separate
    # event type from <MouseWheel> (which is what a physical wheel sends). Tk's own
    # Text/Listbox/Scrollbar class bindings handle both; Canvas has NO default
    # bindings at all, so a scrollable canvas must bind both itself.
    # ONE binding only. Tk delivers an event to EVERY bindtag in turn (widget,
    # class, toplevel, all), so binding both a widget and the "all" tag ran this
    # handler TWICE per scroll — measurably double speed. The "all" tag alone
    # covers every widget in the window, including canvas children.
    # TIP 684 (Tcl/Tk): a <TouchpadScroll> packs Δx in the HIGH 16 bits and Δy in
    # the LOW 16 bits, as SIGNED 16-bit values. Tk 9 delivers a trackpad scroll as
    # <TouchpadScroll> and a physical wheel as <MouseWheel>, so bind both.
    root.bind_all("<TouchpadScroll>", lambda e: _wheel(e, src="touchpad"))
    root.bind_all("<MouseWheel>", lambda e: _wheel(e, src="wheel"))

    def _key_scroll(amount):
        """Keyboard scrolling — guaranteed to work regardless of trackpad quirks."""
        try:
            node = root.winfo_containing(root.winfo_pointerx(), root.winfo_pointery())
        except Exception:
            node = None
        if isinstance(node, (tk.Text, tk.Entry)):
            return                      # let typing widgets handle their own keys
        target = None
        walker = node
        while walker is not None:
            if walker is lcanvas or walker is tcanvas:
                target = walker
                break
            walker = getattr(walker, "master", None)
        if target is None:
            target = tcanvas
        if amount == "top":
            target.yview_moveto(0.0)
        elif amount == "bottom":
            target.yview_moveto(1.0)
        else:
            target.yview_scroll(amount, "units")
        _dbg("KEY %-6s on %-5s -> yview %s"
             % (amount, "left" if target is lcanvas else "takes",
                tuple(round(v, 3) for v in target.yview())))

    for _seq, _amt in (("<Up>", -40), ("<Down>", 40), ("<Prior>", -400), ("<Next>", 400)):
        root.bind_all(_seq, lambda e, a=_amt: _key_scroll(a))
    root.bind_all("<Home>", lambda e: _key_scroll("top"))
    root.bind_all("<End>", lambda e: _key_scroll("bottom"))

    def load_takes():
        # Tk widgets must be created on the MAIN thread. Building them inside the
        # worker (the old code) is a latent hang/crash on macOS — it hung a
        # headless geometry probe outright. The thread only gathers data now.
        def gather():
            try:
                data = []
                for it in read_index()[:40]:
                    path = None
                    if it.get("images"):
                        cand = os.path.join(RUNS, it["id"], it["images"][0]["file"])
                        if os.path.exists(cand):
                            path = cand
                    data.append((it, path))
                _plog("load_takes: %d entries, %d with an image file"
                      % (len(data), sum(1 for _, pp in data if pp)))
                q.put(("takes", data))
            except Exception as exc:
                _log_err("load_takes gather failed: %r" % exc)
                _plog("load_takes FAILED: %r" % exc)
            # Hand it to the MAIN thread through the queue. The old code called
            # root.after() from this worker, which is itself cross-thread Tk use
            # and could be silently dropped — that is why a finished take never
            # appeared in the panel.
            q.put(("takes", data))
        threading.Thread(target=gather, daemon=True).start()

    # Every landing take marks the list dirty; drain() — the app's OWN main-thread pump, every
    # 120 ms — reads that flag and reloads. Registering it HERE (not in _index_insert) keeps Tk
    # off the worker threads: _index_insert() runs inside submit()/collect(), which the Weirdo,
    # Song, Studio and long-video windows all call from their own render threads.
    @watch_index
    def _takes_landed(_meta=None):
        st["_takes_dirty"] = True          # a flag AND NOTHING ELSE — see the note in drain()

    _rows = {"n": 0}
    _toggle_refs = []        # (button, is_on) rebuilt each render; repainted AFTER _btn_colors

    def _sync_compare_btn():
        nsel = len(st.get("cmp_sel") or [])
        try:
            cmp_btn.configure(text="COMPARE (%d)" % nsel,
                              state=("normal" if 2 <= nsel <= 4 else "disabled"))
        except Exception:
            pass

    def _cmp_toggle(it, btn):
        """Select / deselect a take for COMPARE (2-4)."""
        sel = st.setdefault("cmp_sel", [])
        pid = it.get("id")
        if pid in sel:
            sel.remove(pid)
        elif len(sel) >= 4:
            runst.configure(text="compare holds up to 4 takes", fg=C["ochre"])
        else:
            sel.append(pid)
        _style_toggle(btn, pid in sel, C)
        _sync_compare_btn()

    def open_compare_window():
        idx = {i.get("id"): i for i in read_index()}
        picks = []
        for pid in (st.get("cmp_sel") or []):
            it = idx.get(pid)
            if not it:
                continue
            path = None
            files = it.get("images") or []
            if files:
                cand = os.path.join(RUNS, it["id"], files[0]["file"])
                if os.path.exists(cand):
                    path = cand
            picks.append((it, path))
        if len(picks) < 2:
            runst.configure(text="select 2–4 takes to compare", fg=C["ochre"])
            return None
        cctx = {"C": C, "fonts": {"f": f, "fh": fh, "fsm": fsm}, "mono": {"m": fmono},
                "btn_colors": _btn_colors, "reload_takes": load_takes}
        try:
            return open_compare(root, cctx, picks)
        except Exception as exc:
            _log_err("compare failed: %r" % exc)
            runst.configure(text="could not open compare: %s" % exc, fg=C["rust"])
            return None

    def _refilter_takes():
        # Re-read the index so the filter always agrees with what is STORED — a rating changed
        # anywhere (a card's ★/✕, the COMPARE window) must be reflected, and load_takes() gives
        # render_takes() fresh (it, path) pairs rather than stale stored dicts.
        load_takes()

    def on_unkept():
        st["take_unkept"] = not st.get("take_unkept", False)
        _style_toggle(unkept_btn, st["take_unkept"], C)
        _refilter_takes()

    def _rate(it, val):
        cur = take_rating(it)
        set_rating(it["id"], 0 if cur == val else val)
        load_takes()

    unkept_btn.configure(command=on_unkept)
    cmp_btn.configure(command=open_compare_window)
    _sync_compare_btn()

    def render_takes(data):
        _dbg("render_takes rows=%d  tcanvas.yview before=%s"
             % (len(data), tuple(round(v, 3) for v in tcanvas.yview())))
        st["_takes_data"] = list(data)
        # FILTER — pure, module-level filter_takes(): rating_filter ∩ unkept_only.
        _keep = {i.get("id") for i in filter_takes([it for it, _p in data],
                                                   takes_filter_var.get(),
                                                   st.get("take_unkept", False))}
        shown = [(it, p) for it, p in data if it.get("id") in _keep]
        growth = len(shown) > _rows["n"]      # a new take was added
        _rows["n"] = len(shown)
        takes_hdr.configure(text="%d of %d take%s  ·  every run is kept in "
                                 "~/forge-lab/runs/  ·  click a thumbnail to open it full size"
                                 % (len(shown), len(data), "" if len(data) == 1 else "s"))
        for w in tinner.winfo_children():
            w.destroy()
        take_rows.clear()
        del _toggle_refs[:]
        if not shown:
            tk.Label(tinner, text=("No takes match this filter." if data
                                   else "No takes yet."),
                     bg=C["bg"], fg=C["muted"], font=fsm, anchor="w").pack(anchor="w", pady=10)
            _sync_compare_btn()
            return
        for it, path in shown:
            rating = take_rating(it)
            rejected = rating == -1
            row_bg = C["bg"] if rejected else C["panel"]
            row = tk.Frame(tinner, bg=row_bg, highlightthickness=1,
                           highlightbackground=C["line"])
            row.pack(fill="x", pady=4)
            thumb = None
            if path:
                # ⚠️ THIS is the line that bricked the app: a `remove background` take is an
                # RGBA PNG, and tk.PhotoImage on it never returned (see thumb_photo).
                thumb = thumb_photo(path, 190)
                if thumb is not None:
                    THUMBS[it["id"]] = thumb
            vid = bool(path and path.lower().endswith((".mp4", ".webm")))
            thumb_lbl = tk.Label(row, bg=row_bg, image=thumb,
                                 text="" if thumb else ("[video]" if vid else "[no preview]"),
                                 fg=C["faint"] if rejected else C["muted"], font=fsm,
                                 width=0 if thumb else 16)
            thumb_lbl.pack(side="left", padx=8, pady=8)
            if path:
                # click the thumbnail -> full-size image in Preview
                thumb_lbl.configure(cursor="hand2")
                thumb_lbl.bind("<Button-1>", lambda e, p=path: open_full(p))
            _ink = C["faint"] if rejected else C["muted"]
            extra = ""
            if it.get("video"):
                extra = "\n%s · %d frames = %.2fs @%gfps · %s" % (
                    (("LoRA " + os.path.basename(it["lora"])) if it.get("lora")
                     else (it.get("quality") or "no LoRA")),
                    it.get("length", 0), it.get("seconds", 0) or 0,
                    it.get("fps", 24.0),
                    "audio" if it.get("audio", True) else "NO audio")
            # lineage ("from: …") + a compact engine/steps/seconds line, when present
            head = " · ".join(b for b in (take_lineage(it), take_specs(it)) if b)
            if it.get("promote"):
                txt = "%s\nupscaled from %s\n%s\n%s" % (
                    it.get("label", ""), it.get("source", "?"),
                    fmt(it["wall"]) if it.get("wall") else it.get("status", ""),
                    ("~/forge-lab/runs/%s/" % it["id"]) if it.get("images") else "")
            else:
                txt = "%s%s\n%s\nseed %s · %d steps · %s%s\n%s" % (
                    (head + "\n") if head else "",
                    it.get("label", ""), (it.get("prompt") or "")[:150],
                    it.get("seed"), it.get("steps", 25),
                    fmt(it["wall"]) if it.get("wall") else it.get("status", ""), extra,
                    ("~/forge-lab/runs/%s/" % it["id"]) if it.get("images") else "")
            # BUTTONS FIRST (see the original note): the controls must get their space and the
            # text wraps into what is left.
            btns = tk.Frame(row, bg=row_bg)
            btns.pack(side="right", padx=10)
            txt_lbl = tk.Label(row, text=txt, bg=row_bg, fg=_ink, font=fsm,
                               justify="left", wraplength=560, anchor="w")
            txt_lbl.pack(side="left", padx=6, pady=8, fill="x", expand=True)
            take_rows.append((txt_lbl, btns))
            # ★ keep / ✕ reject rating toggles + a ⧉ COMPARE select toggle
            rate_row = tk.Frame(btns, bg=row_bg)
            rate_row.pack(anchor="e", pady=(0, 2))
            star_b = tk.Button(rate_row, text="★", bg=C["panel2"], fg=C["ink"],
                               activebackground=C["rust"], activeforeground="#fff",
                               relief="flat", font=fsm, padx=7, pady=0, borderwidth=0)
            rej_b = tk.Button(rate_row, text="✕", bg=C["panel2"], fg=C["ink"],
                              activebackground=C["rust"], activeforeground="#fff",
                              relief="flat", font=fsm, padx=7, pady=0, borderwidth=0)
            sel_b = tk.Button(rate_row, text="⧉", bg=C["panel2"], fg=C["ink"],
                              activebackground=C["rust"], activeforeground="#fff",
                              relief="flat", font=fsm, padx=6, pady=0, borderwidth=0)
            star_b.pack(side="left", padx=(0, 3))
            rej_b.pack(side="left", padx=(0, 3))
            sel_b.pack(side="left")
            star_b.configure(command=lambda i=it: _rate(i, 1))
            rej_b.configure(command=lambda i=it: _rate(i, -1))
            sel_b.configure(command=lambda i=it, b=sel_b: _cmp_toggle(i, b))
            _toggle_refs.append((star_b, rating == 1))
            _toggle_refs.append((rej_b, rating == -1))
            _toggle_refs.append((sel_b, it.get("id") in (st.get("cmp_sel") or [])))
            if path:
                # DELETE at the top right of the take. Confirmed before anything is removed —
                # a real delete, not a Trash move. (Labelled DELETE not ✕ so it is not confused
                # with the ✕ REJECT rating toggle just above it.)
                tk.Button(btns, text="DELETE", bg=C["panel"], fg=C["muted"],
                          activebackground=C["rust_deep"], activeforeground="#fff",
                          relief="flat", font=fsm, padx=6, pady=0, borderwidth=0,
                          command=lambda i=it: delete_take(i)).pack(anchor="e", pady=(0, 2))
                # upscale, don't re-render: keeps THIS take's exact motion/frame
                tk.Button(btns, text="PROMOTE", bg=C["panel2"], fg=C["ink"],
                          activebackground=C["rust_deep"], activeforeground="#fff",
                          disabledforeground=C["dim"], relief="flat", font=fsm,
                          padx=8, pady=4, borderwidth=0,
                          highlightthickness=1, highlightbackground=C["line"],
                          command=lambda i=it, p=path: promote_dialog(i, p)
                          ).pack(pady=(0, 4))
            tk.Button(btns, text="LOAD SETTINGS", bg=C["panel2"], fg=C["ink"],
                      disabledforeground=C["dim"],
                      relief="flat", font=fsm, padx=8, pady=4, borderwidth=0,
                      command=lambda i=it: load_take(i)).pack(pady=(0, 4))
            if path:
                fp = path
                tk.Button(btns, text="OPEN", bg=C["panel2"], fg=C["ink"], relief="flat",
                          font=fsm, padx=8, pady=4, borderwidth=0,
                          command=lambda p=fp: subprocess.Popen(["open", p])).pack()
        if growth:
            # Newest takes are inserted at the TOP. If the list was scrolled down,
            # a new one landed above the visible area and looked like it never
            # arrived. Jump to the top so a finished run is always visible.
            tcanvas.yview_moveto(0.0)
            tinner.update_idletasks()
            tcanvas.configure(scrollregion=tcanvas.bbox("all"))
        _reflow_takes()          # wrap the text into whatever width is on screen
        _btn_colors(tinner)      # take-row buttons need the dark-text treatment too
        # ⚠️ Re-assert the ON state AFTER the blanket repaint, or every ★/✕/⧉ toggle reads as
        # "off" no matter what the index says — _btn_colors paints every button light.
        for _b, _on in _toggle_refs:
            _style_toggle(_b, _on, C)
        _sync_compare_btn()

    def open_full(path):
        """Full size hands off to macOS Preview: native zoom/pan, no re-encoding,
        and it works on the full-resolution file we downloaded from forge."""
        try:
            if os.path.exists(path):
                subprocess.Popen(["open", path])
            else:
                runst.configure(text="that file is gone: %s" % path, fg=C["ochre"])
        except Exception as e:
            runst.configure(text="couldn't open the image: %s" % e, fg=C["ochre"])

    def _do_delete(it):
        """The actual removal — separated from the dialog so it can be tested
        without a human clicking, and so there is exactly ONE deletion path."""
        d = os.path.join(RUNS, it.get("id", ""))
        tid = it.get("id", "")
        files = it.get("images") or []
        nbytes = sum((f.get("bytes") or 0) for f in files)
        try:
            # guard: only ever remove a path INSIDE the runs folder
            if (os.path.isdir(d)
                    and os.path.abspath(d).startswith(os.path.abspath(RUNS) + os.sep)):
                shutil.rmtree(d, ignore_errors=True)
            left = [i for i in read_index() if i.get("id") != tid]
            json.dump(left, open(_index_path(), "w"), indent=1)
            _plog("delete: %s — %d file(s), %.1f MB, %d takes left"
                  % (tid[:8], len(files), nbytes / 1e6, len(left)))
            load_takes()
            runst.configure(text="deleted take %s" % tid[:8], fg=C["muted"])
            return True
        except Exception as exc:
            _plog("delete FAILED: %r" % exc)
            runst.configure(text="delete failed: %s" % exc, fg=C["rust"])
            return False

    def delete_take(it):
        """Delete ONE take: its folder under ~/forge-lab/runs/ and its index entry.
        LOCAL ONLY — nothing on forge is removed. Always confirmed, because there is
        no undo: the folder is deleted outright, not moved to the Trash."""
        d = os.path.join(RUNS, it.get("id", ""))
        files = it.get("images") or []
        nbytes = sum((f.get("bytes") or 0) for f in files)
        exists = os.path.isdir(d)
        top = tk.Toplevel(root)
        top.title("Delete take")
        top.configure(bg=C["panel"])
        try:
            top.transient(root)
        except Exception:
            pass
        tk.Label(top, text="DELETE this take?", bg=C["panel"], fg=C["ink"],
                 font=fh).pack(anchor="w", padx=16, pady=(14, 4))
        tk.Label(top, text="%s\nrun %s\n\nRemoves %s\n(%d file(s), %.1f MB) and the entry "
                           "from Takes.\nThis cannot be undone — it does not go to the "
                           "Trash.\nNothing on forge is deleted."
                 % (it.get("label", "take"), (it.get("id") or "?")[:8],
                    d if exists else "(folder already missing)",
                    len(files), nbytes / 1e6),
                 bg=C["panel"], fg=C["muted"], font=fsm, justify="left",
                 wraplength=460).pack(anchor="w", padx=16, pady=(0, 12))
        rowb = tk.Frame(top, bg=C["panel"])
        rowb.pack(anchor="w", padx=16, pady=(0, 14))

        def really():
            top.destroy()
            _do_delete(it)

        tk.Button(rowb, text="DELETE", bg=C["ochre"], fg="#14110f",
                  activebackground="#d8b45f", activeforeground="#14110f",
                  relief="flat", font=fsm, padx=12, pady=7, borderwidth=0,
                  command=really).pack(side="left", padx=(0, 8))
        tk.Button(rowb, text="CANCEL", bg=C["panel2"], fg=C["ink"], relief="flat",
                  font=fsm, padx=12, pady=7, borderwidth=0,
                  highlightthickness=1, highlightbackground=C["line"],
                  command=top.destroy).pack(side="left")

    def load_take(it):
        st["mode"] = it.get("mode", "t2i")
        # the variant is part of the take's identity — restore it BEFORE set_mode so the
        # dropdowns, caps and quality axis all come back with the take
        if it.get("wan_mode") in WAN_MODES:
            st["_size_key_prev"] = size_key(st.get("mode"), st.get("wan_mode"))
            st["wan_mode"] = it["wan_mode"]
        if it.get("video_mode") in VIDEO_MODES:
            st["video_mode"] = it["video_mode"]
        set_mode(st["mode"])
        prompt.delete("1.0", "end")
        prompt.insert("1.0", it.get("prompt", ""))
        for _ent, _key, _dflt in ((e_steps, "steps", 25), (e_res, "resolution", 1024),
                                  (e_w, "width", 1024), (e_h, "height", 1024)):
            _ent.delete(0, "end")
            _ent.insert(0, str(it.get(_key, _dflt)))
        e_seed.delete(0, "end"); e_seed.insert(0, "-1")      # fresh variation
        e_prefix.delete(0, "end")
        e_prefix.insert(0, it.get("prefix") or "forge_lab")
        # sync the dropdowns with the restored numbers — they used to keep showing
        # the previous selection, so the panel contradicted itself
        if MODES[st["mode"]].get("wan"):
            if st.get("wan_mode") == "s2v":
                # S2V has ONE path, and its label is not a WAN_QUALITY key
                qvar.set(S2V_QUALITY)
                apply_quality(S2V_QUALITY)
            else:
                # a WAN take's "steps" IS its path, so restore the PATH, not a step count
                _wq = it.get("quality")
                _wq = _wq if _wq in WAN_QUALITY else WAN_QUALITY_DEFAULT
                qvar.set(_wq)
                apply_quality(_wq)
        # S2V: put the chained LENGTH and the performance FILE back too, or a restored take
        # silently re-runs at a different duration with no audio
        if MODES[st["mode"]].get("wan") and st.get("wan_mode") == "s2v":
            if it.get("chunks") in S2V_LENGTHS:
                s2v_len_var.set(S2V_LENGTHS[it["chunks"]])
            if it.get("audio_path"):
                st["audio_path"] = it["audio_path"]
                s2v_name.configure(text=os.path.basename(it["audio_path"]), fg=C["ink"])
            apply_s2v()
        else:
            _match = None
            for _qn, _q in QUALITY.items():
                if _q.get("steps") and int(it.get("steps", 25)) == _q["steps"]:
                    _match = _qn
                    break
            qvar.set(_match or "Custom")
            q_hint.configure(text=QUALITY[qvar.get()]["note"])
        if it.get("width") and it.get("height"):
            _r = it["width"] / float(it["height"])
            avar.set(min(ASPECTS.items(), key=lambda kv: abs(kv[1] - _r))[0])
        refresh_estimate()
        runst.configure(text="settings loaded from an earlier take — seed set to -1 "
                             "so this is a fresh variation", fg=C["muted"])

    # ------------------------------------------------------- session state
    def current_settings():
        return {"prompt": prompt.get("1.0", "end").rstrip("\n"),
                "width": e_w.get(), "height": e_h.get(), "steps": e_steps.get(),
                "resolution": e_res.get(), "seed": e_seed.get(), "prefix": e_prefix.get(),
                "quality": qvar.get(), "aspect": avar.get(),
                "mode": st.get("mode"), "refs": list(st.get("refs") or []),
                "video_mode": st.get("video_mode", "r2v"),
                "wan_mode": st.get("wan_mode", WAN_VARIANT_DEFAULT),
                "audio": audio_var.get(),
                "card_mode": cards_var.get(),
                "free_card": bool(free_card_var.get()),
                "audio_path": st.get("audio_path"),
                "source_path": st.get("source_path"),
                "src_frames": st.get("src_frames"),
                "s2v_len": s2v_len_var.get(),
                "take_filter": takes_filter_var.get(),
                "take_unkept": bool(st.get("take_unkept", False)),
                "geometry": root.geometry()}

    def save_now():
        save_settings(current_settings())

    def _sweep():
        """Safety net: pick up anything the tracker lost, without a relaunch."""
        def work():
            try:
                got = recover_orphans(skip=set(st["active"].keys()))
                if got:
                    q.put(("recovered", got))
            except Exception as exc:
                _dbg("sweep failed: %s" % exc)
        threading.Thread(target=work, daemon=True).start()

    def _autosave():
        # ⚠️ NEVER persist from a DRIVER run. _autosave() fires at startup — BEFORE the driver
        # has done anything — and it was writing whatever geometry the window happened to have
        # at that instant, silently rewriting Ryan's real settings.json (a bogus 200x200 window)
        # on every driver invocation. A driver must leave NO footprint on his app state.
        if not _DRIVER and os.environ.get("FORGE_LAB_NOAUTOSAVE") != "1":
            save_now()
        _sweep()
        root.after(30000, _autosave)

    root.protocol("WM_DELETE_WINDOW", lambda: (save_now(), root.destroy()))

    # ------------------------------------------------------------- kick off
    _s = load_settings()
    _first = not _s
    if _s.get("aspect") in ASPECTS:
        avar.set(_s["aspect"])
    if (_s.get("quality") in QUALITY or _s.get("quality") in WAN_QUALITY
            or _s.get("quality") == S2V_QUALITY):
        qvar.set(_s["quality"])
    if _s.get("wan_mode") in WAN_MODES:
        st["wan_mode"] = _s["wan_mode"]
    # S2V's two extra pieces of state: the performance FILE and the chained LENGTH
    if _s.get("audio_path"):
        st["audio_path"] = _s["audio_path"]
    if _s.get("source_path"):
        st["source_path"] = _s["source_path"]
        if _s.get("src_frames"):
            st["src_frames"] = _s["src_frames"]
    if _s.get("s2v_len") in S2V_LENGTHS.values():
        s2v_len_var.set(_s["s2v_len"])
    if _s.get("video_mode") in VIDEO_MODES:
        st["video_mode"] = _s["video_mode"]
    if _s.get("audio") in ("on", "off"):
        audio_var.set(_s["audio"])
        apply_audio()
    # migrate the OLD 1/2 CARDS toggle: '1' -> Solo, '2' -> Turbo, anything unknown -> Solo
    # ⚠️ BEFORE apply_cards(): the CARD MODE note is built from this, so setting it after
    # would paint the hint from the previous state.
    free_card_var.set(bool(_s.get("free_card", True)))
    _paint_free_card()
    cards_var.set(migrate_card_mode(_s.get("card_mode", _s.get("cards"))))
    apply_cards()
    if _s.get("take_filter") in RATING_FILTERS:
        takes_filter_var.set(_s["take_filter"])
    st["take_unkept"] = bool(_s.get("take_unkept", False))
    _style_toggle(unkept_btn, st["take_unkept"], C)
    if _s.get("prompt"):
        prompt.delete("1.0", "end")
        prompt.insert("1.0", _s["prompt"])
    for _k, _ent in (("width", e_w), ("height", e_h), ("steps", e_steps),
                     ("resolution", e_res), ("seed", e_seed), ("prefix", e_prefix)):
        if _s.get(_k) not in (None, ""):
            _ent.delete(0, "end")
            _ent.insert(0, str(_s[_k]))
    if _s.get("geometry"):
        try:
            root.geometry(_s["geometry"])
        except Exception:
            pass
    set_mode(_s.get("mode") if _s.get("mode") in MODES else "t2i")
    if st.get("audio_path"):
        s2v_name.configure(text=os.path.basename(st["audio_path"]), fg=C["ink"])
    if st.get("source_path"):
        src_name.configure(text=os.path.basename(st["source_path"]), fg=C["ink"])
    if _first:
        apply_quality("Medium")        # fresh install: seed from the default preset
    else:                              # otherwise the restored numbers are what you used
        # the axis follows the KIND — showing the image note over a video take was a
        # standing contradiction (the dropdown said "Standard", the hint described Medium)
        _qh = (WAN_QUALITY if st.get("mode") == "wan" else
               VIDEO_QUALITY if MODES[st.get("mode")].get("video") else QUALITY)
        q_hint.configure(text=_qh.get(qvar.get(), {}).get(
            "note", MODES[st.get("mode")].get("help", "")))
    _refs = [p for p in (_s.get("refs") or ([_s["ref"]] if _s.get("ref") else []))
             if p and os.path.exists(p)]
    if _refs:
        st["refs"] = _refs
        _render_refs()
    refresh_estimate()
    drain(); track(); poll_status(); apply_status(); _wd()
    threading.Thread(target=_ws_loop, daemon=True).start()
    threading.Thread(target=lambda: resolve_forge(force=True), daemon=True).start()
    load_takes()
    load_loras()
    _autosave()

    _sweep()                       # recover anything finished while we were closed

    # ---- test driver: does CHARACTER really put a character onto the form? ----
    # The store itself is covered GPU-free by tools/character_matrix.py. THIS one exists to
    # prove the WIRING: that the workspace opens and lists what is on disk, that its LOAD puts
    # the prompt, the seed and ref 1 onto the form, and that picking "Character" twice does not
    # stack two identical windows. Runs against a TEMP store, so it touches no real character.
    if os.environ.get("FORGE_LAB_TESTCHAR") == "1":
        def _tc():
            import tempfile as _tf
            fails = []

            def ck(cond, label):
                print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
                if not cond:
                    fails.append(label)

            tmp = _tf.mkdtemp(prefix="forge_lab_testchar_")
            globals()["CHARACTERS"] = tmp          # redirect the store
            # a mode that actually TAKES references, or ref 1 cannot be set at all
            kind_var.set("Video"); on_kind()
            pick_model(model_label("video", video_mode="r2v"))
            root.update_idletasks()

            still = os.path.join(tmp, "src-still.png")
            with open(still, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"z" * 64)
            write_character("unit-test",
                            {"name": "Unit Test", "prompt": "IDENTITY BLOCK v1",
                             "seed": "4242", "lora": None, "lora_strength": "1.0"},
                            still_src=still)
            write_character("unit-video",
                            {"name": "Unit Video", "prompt": "STILL BLOCK",
                             "seed": "-1", "lora": None, "lora_strength": "1.0",
                             "picks": {"Head size": 3},
                             "action": "walks toward the camera"},
                            still_src=still)
            os.remove(still)               # the character's OWN COPY is what must survive

            # 1 · the TYPE entry opens a window showing what is on disk
            win = open_character_workspace()
            root.update_idletasks()
            ck(win is not None and win.winfo_exists(), "picking Character opens the workspace")

            # ⚠️ OPEN READY TO CREATE. Ryan: "it should open to a new character i shouldnt have
            # to select it." The window must come up on a blank NEW character, nothing selected.
            ck(win.char_ed.get("slug") is None,
               "the window OPENS on a new character, not a saved one (slug %r)"
               % win.char_ed.get("slug"))
            ck(win.char_widgets["name"].get() == "",
               "the NAME field is empty and ready to type (got %r)"
               % win.char_widgets["name"].get())
            ck(win.char_ed.get("still_src") is None, "and no still is attached yet")
            # ⚠️ THIS is the assertion that actually distinguishes the two states. slug / name /
            # still are IDENTICAL either way - the widgets are constructed empty - so asserting only
            # those left the guard BLIND: removing _new() still passed. What differs is that the
            # window now SAYS it is on a new character instead of showing an empty status bar.
            ck("new character" in win.char_status.cget("text").lower(),
               "the window SAYS it is on a new character (got %r)"
               % win.char_status.cget("text"))

            boxes = []

            def _walk(w):
                for ch in w.winfo_children():
                    if isinstance(ch, tk.Listbox):
                        boxes.append(ch)
                    _walk(ch)
            _walk(win)
            listed = []
            if boxes:
                listed = [boxes[0].get(i) for i in range(boxes[0].size())]
            ck(listed == ["Unit Test", "Unit Video"],
               "it lists the saved characters (got %r)" % listed)

            # 1b · the OPTIONS picker, and the rule that it never eats your own words
            menus, texts, butts, labels = [], [], [], []

            def _walk2(w):
                for ch in w.winfo_children():
                    if isinstance(ch, tk.OptionMenu):
                        menus.append(ch)
                    elif isinstance(ch, tk.Text) and not isinstance(ch, tk.Entry):
                        texts.append(ch)
                    elif isinstance(ch, tk.Button):
                        butts.append(ch)
                    elif isinstance(ch, tk.Label):
                        labels.append(ch)
                    _walk2(ch)
            _walk2(win)
            # The window also holds the LORA dropdown, so position is not a safe way to find a
            # picker. Identify it by WHAT IT OFFERS, and keep an exact count as the guard.
            def _opts(m):
                _mu, out = m["menu"], []
                for _i in range(_mu.index("end") + 1):
                    try:
                        out.append(_mu.entrycget(_i, "label"))
                    except Exception:
                        pass
                return out

            _F = dict(CHAR_OPTIONS)["Feature"]
            feat = None
            for _m in menus:
                if _opts(_m)[:2] == [_F[0], _F[1]]:
                    feat = _m
                    break
            # axes + the LoRA picker + the SIZE (iteration ladder) picker - nothing stray
            ck(len(menus) == len(CHAR_OPTIONS) + 2,
               "exactly the axes plus the LoRA and SIZE pickers (%d)" % len(menus))
            ck(feat is not None, "the Feature picker is found by what it offers")
            box = texts[0] if texts else None
            ck(box is not None, "the block box is present")
            if box is not None and feat is not None:
                box.delete("1.0", "end")
                ck(box.get("1.0", "end").strip() == "", "the block starts empty")

                feat["menu"].invoke(1)
                root.update_idletasks()
                got = box.get("1.0", "end").strip()
                ck(_F[1] in got, "picking a Feature composes it into the block")
                ck(got.endswith("85mm lens."), "the fixed deadpan tail survives composing")
                ck("photorealistic" in got and "full body" in got,
                   "the other axes contribute their defaults")

                box.delete("1.0", "end")
                box.insert("1.0", "MY OWN WORDS, DO NOT TOUCH")
                feat["menu"].invoke(2)
                root.update_idletasks()
                ck(box.get("1.0", "end").strip() == "MY OWN WORDS, DO NOT TOUCH",
                   "a pick does NOT clobber a hand-edited block")
                # the window's OWN status line — not the main form's
                _warned = [l for l in labels if "options changed" in (l.cget("text") or "")]
                ck(bool(_warned), "…and the window says why nothing moved")

                bp = [b for b in butts if b.cget("text") == "BUILD PROMPT"]
                ck(bool(bp), "there is a BUILD PROMPT button")
                if bp:
                    bp[0].invoke()
                    root.update_idletasks()
                    ck(_F[2] in box.get("1.0", "end"),
                       "BUILD PROMPT overwrites deliberately, and lands the current pick")

            # 1c . Ryan's list: readable buttons, a flow you can follow, a picture you can SEE
            _buts = []

            def _walk3(w):
                for ch in w.winfo_children():
                    if isinstance(ch, tk.Button):
                        _buts.append(ch)
                    _walk3(ch)
            _walk3(win)
            _dark = [b for b in _buts if str(b.cget("fg")).lower() == "#14110f"]
            ck(len(_buts) >= 8 and len(_dark) == len(_buts),
               "every button has DARK text (%d of %d) - the readability fix"
               % (len(_dark), len(_buts)))
            ck([n for n, _ in win.char_steps] == [1, 2, 3, 4, 5, 6],
               "the flow IS the instruction: 6 numbered steps (got %r)"
               % [n for n, _ in win.char_steps])
            _sr = str(win.char_canvas.cget("scrollregion"))
            ck(_sr not in ("", "0 0 1 1", "0 0 0 0"),
               "the editor column SCROLLS so no step is starved (scrollregion %r)" % _sr)
            _opt = dict(CHAR_OPTIONS)
            ck(len(_opt["Style"]) >= 15, "many art styles (%d)" % len(_opt["Style"]))
            ck(any("GREEN SCREEN" in v for v in _opt["Setting"]), "a green screen exists")
            ck(any("WHITE" in v for v in _opt["Setting"]), "a plain white backdrop exists")
            ck(len(win.char_dump()["axes"]) == len(CHAR_OPTIONS),
               "char_dump reports every axis")
            ck(bool(win.char_preview.bind("<Button-1>")), "the preview is CLICKABLE")
            ck(callable(getattr(win, "char_view", None)), "ENLARGE is wired to a viewer")
            # ⚠️ A viewer that RAISES on a missing still is invisible: Tk swallows the callback
            # exception and the button just does nothing. So open it and read what it said.
            win.char_view()
            root.update_idletasks()
            _said = [l for l in labels if "nothing to enlarge" in (l.cget("text") or "")]
            ck(bool(_said), "enlarging with no still SAYS so instead of doing nothing")

            # 1d . a REAL png, so the preview RENDER and the viewer's happy path are covered
            # Tk writes one by itself - no PIL, and no dependence on a render existing on disk.
            _real = os.path.join(tmp, "real-still.png")
            _ph = tk.PhotoImage(width=96, height=128)
            _ph.put("#c25e33", to=(0, 0, 96, 64))
            _ph.put("#e9e3da", to=(0, 64, 96, 128))
            _ph.write(_real, format="png")
            ck(os.path.exists(_real) and os.path.getsize(_real) > 0, "synthesised a real png")
            win.char_set_still(_real)
            root.update_idletasks()
            ck(win.char_dump().get("thumb") is True,
               "a REAL png actually renders into the preview")
            _tops0 = len([c for c in win.winfo_children() if isinstance(c, tk.Toplevel)])
            win.char_view()
            root.update_idletasks()
            _tops = [c for c in win.winfo_children() if isinstance(c, tk.Toplevel)]
            ck(len(_tops) > _tops0, "ENLARGE opens a viewer window")
            ck(all(t.winfo_exists() for t in _tops), "the viewer is a live window, not a corpse")
            for _t in _tops:
                _t.destroy()
            root.update_idletasks()

            # 1e . the SLIDERS - "like in video games character creation"
            _scales = []

            def _walk4(w):
                for ch in w.winfo_children():
                    if isinstance(ch, tk.Scale):
                        _scales.append(ch)
                    _walk4(ch)
            _walk4(win)
            ck(len(_scales) == len(CHAR_SLIDERS),
               "one SLIDER per body axis, not a dropdown (%d)" % len(_scales))
            ck(all(float(s.cget("from")) == -3.0 and float(s.cget("to")) == 3.0
                   for s in _scales),
               "every slider runs -3..+3 with ordinary in the middle")
            _ears = win.char_slides.get("Ears")
            ck(_ears is not None, "there is an Ears slider")
            if _ears is not None:
                box.delete("1.0", "end")   # empty box -> a slide SILENTLY composes
                _ears["var"].set(3)
                win.char_on_slide()
                root.update_idletasks()
                _got = box.get("1.0", "end").strip()
                ck(CHAR_SLIDER_MAP["Ears"][6] in _got,
                   "sliding Ears to +3 composes the extreme phrase")
                ck(_ears["phrase"].cget("text") == CHAR_SLIDER_MAP["Ears"][6],
                   "the words under the slider ARE the words that get used")
                ck(_ears["val"].cget("text") == "+3", "the value readout shows +3")
                _ears["var"].set(-3)
                win.char_on_slide()
                root.update_idletasks()
                ck(CHAR_SLIDER_MAP["Ears"][0] in box.get("1.0", "end"),
                   "sliding the other way composes the opposite phrase")
            # every slider back to the middle must leave NO body filler in the block
            for _s in win.char_slides.values():
                _s["var"].set(0)
            win.char_on_slide()
            root.update_idletasks()
            _pk = win.char_dump()["picks"]
            ck(all(not char_slider_phrase(_pk, _a) for _a in CHAR_SLIDER_ORDER),
               "a neutral body adds NOTHING to the block (no filler)")
            ck(win.char_slides["Nose"]["phrase"].cget("text") == CHAR_SLIDER_NEUTRAL,
               "a centred slider reads '%s'" % CHAR_SLIDER_NEUTRAL)
            ck(len(win.char_dump()["sliders"]) == len(CHAR_SLIDERS),
               "char_dump reports every slider level")


            # ⚠️ NEW must be reachable WITHOUT SCROLLING. Asserted STRUCTURALLY (not by pixel
            # position, which is meaningless in an unmapped driver window): at least one NEW button
            # must live OUTSIDE the scrolling canvas.
            def _ancestors(w):
                p = w.master
                while p is not None:
                    yield p
                    p = p.master
            def _all_widgets(w):
                yield w
                for c in w.winfo_children():
                    yield from _all_widgets(c)
            _news = []
            for _w in _all_widgets(win):
                try:
                    if _w.winfo_class() == "Button" and _w.cget("text") == "NEW":
                        _news.append(_w)
                except Exception:
                    pass
            ck(len(_news) >= 1, "there is a NEW button (found %d)" % len(_news))
            _outside = [_w for _w in _news if win.char_scroll not in set(_ancestors(_w))]
            ck(bool(_outside),
               "a NEW button sits OUTSIDE the scroll column, so it is reachable without "
               "scrolling (%d of %d)" % (len(_outside), len(_news)))

            # ⚠️ THE CANVAS FOLLOWS THE FRAMING, and the window must SAY which canvas it will
            # submit. Measured on forge: the FIGURE is limited by canvas HEIGHT, so a 4 MP square
            # puts only ~1% more pixels on a standing figure than a 2 MP portrait, for 2.6x the
            # time. Tall framing -> tall canvas; only the BUST pays for the square.
            ck(char_still_size("full body shot, the whole figure head to feet in frame")
               == (1440, 2880), "a full-body framing gets the 4 MP tall canvas")
            ck(char_still_size("bust portrait, head and shoulders only") == (2048, 2048),
               "a bust framing gets the 4 MP square")
            ck(char_still_size("three-quarter shot, head to mid-thigh") == (1440, 2880),
               "a three-quarter framing gets the tall canvas too")
            ck(char_still_size("something else entirely") == (1440, 2880),
               "an unknown framing falls back to the full-body canvas")
            ck(char_still_size(None) == (1440, 2880), "and so does a missing one")

            # end to end: set the framing pick, then read the canvas that would be SUBMITTED
            win.char_pick["Framing"].set("bust portrait, head and shoulders only")
            win.char_on_pick()
            root.update_idletasks()
            _pb = win.char_params()
            ck((_pb["width"], _pb["height"]) == (2048, 2048),
               "picking the BUST framing submits 2048x2048 (got %dx%d)"
               % (_pb["width"], _pb["height"]))
            ck("2048" in win.char_size_lbl.cget("text"),
               "and the window SAYS so (got %r)" % win.char_size_lbl.cget("text"))
            win.char_pick["Framing"].set("full body shot, the whole figure head to feet in frame")
            win.char_on_pick()
            root.update_idletasks()
            _pf = win.char_params()
            ck((_pf["width"], _pf["height"]) == (1440, 2880),
               "the full-body framing submits 1440x2880 (got %dx%d)"
               % (_pf["width"], _pf["height"]))
            ck("1440\u00d72880" in win.char_size_lbl.cget("text"),
               "and the readout follows it back (got %r)" % win.char_size_lbl.cget("text"))

            # THE ITERATION LADDER. Full is the keeper default; Fast/Draft exist so a quick
            # composition check does not cost 89 s. Same aspect ratio, half/quarter the pixels.
            ck(char_still_size("full body shot, the whole figure head to feet in frame", "Fast")
               == (1024, 2048), "Fast halves a full-body still to 1024x2048 (2 MP)")
            ck(char_still_size("full body shot, the whole figure head to feet in frame", "Draft")
               == (704, 1440), "Draft quarters it to 704x1440 (1 MP)")
            ck(char_still_size("bust portrait, head and shoulders only", "Fast") == (1440, 1440),
               "Fast halves a bust to 1440x1440")
            ck(char_still_size("bust portrait, head and shoulders only", "Draft") == (1024, 1024),
               "Draft quarters a bust to 1024x1024")
            ck(char_still_size("full body shot, the whole figure head to feet in frame", "garbage")
               == (1440, 2880),
               "an unknown scale falls back to FULL - a bad value must never shrink a keeper")
            ck(char_still_size(None, "Fast") == (1024, 2048),
               "the scale still applies when the framing is missing")
            for _f in ("full body shot, the whole figure head to feet in frame",
                       "bust portrait, head and shoulders only"):
                for _s in ("Full", "Fast", "Draft"):
                    _sw, _sh = char_still_size(_f, _s)
                    ck(_sw % 32 == 0 and _sh % 32 == 0,
                       "%s / %s snaps to a multiple of 32 (%dx%d)" % (_f[:11], _s, _sw, _sh))

            # end to end: the picker changes what is actually SUBMITTED
            _w0, _h0 = win.char_params()["width"], win.char_params()["height"]
            win.char_size_var.set("Fast")
            win.char_on_pick()
            root.update_idletasks()
            _pf2 = win.char_params()
            ck((_pf2["width"], _pf2["height"]) == (1024, 2048),
               "the SIZE picker changes the SUBMITTED canvas to 1024x2048 (was %dx%d)"
               % (_w0, _h0))
            ck("iteration only" in win.char_size_lbl.cget("text"),
               "the readout marks a Fast still as ITERATION ONLY (got %r)"
               % win.char_size_lbl.cget("text"))
            win.char_size_var.set("Full")
            win.char_on_pick()
            root.update_idletasks()
            _pf3 = win.char_params()
            ck((_pf3["width"], _pf3["height"]) == (1440, 2880),
               "back on Full the submitted canvas is 1440x2880 again (got %dx%d)"
               % (_pf3["width"], _pf3["height"]))
            ck("iteration only" not in win.char_size_lbl.cget("text"),
               "...and the iteration marker is gone")

            # 2 · ONE window — picking the entry again must raise, not stack
            ck(open_character_workspace() is win, "picking Character again raises the SAME window")
            if win is not None:
                win.destroy()
            root.update_idletasks()

            # 3 · LOAD puts it onto the form
            prompt.delete("1.0", "end"); prompt.insert("1.0", "something else entirely")
            e_seed.delete(0, "end"); e_seed.insert(0, "-1")
            st["refs"] = []
            _render_refs()
            note = apply_character(read_character("unit-test"),
                                   character_still("unit-test"))
            ck(prompt.get("1.0", "end").strip() == "IDENTITY BLOCK v1",
               "LOAD puts the identity block onto the form")
            ck(e_seed.get() == "4242", "LOAD puts the seed onto the form")
            _r = list(st.get("refs") or [])
            ck(len(_r) == 1 and _r[0].startswith(tmp) and os.path.exists(_r[0]),
               "LOAD sets ref 1 to the character's OWN copy of the still")
            ck(str(revert_btn.cget("state")) == "normal",
               "LOAD arms REVERT so the replaced wording is recoverable")
            ck("loaded Unit Test" in note, "LOAD returns a note saying what it did")

            # 3b . the VIDEO path: r2v must tag ref 1 and carry the ACTION, not the still block
            note2 = apply_character(read_character("unit-video"),
                                    character_still("unit-video"))
            _vp = prompt.get("1.0", "end").strip()
            ck(_vp.startswith("<Picture 1> is the character."),
               "r2v LOAD tags ref 1 as <Picture 1> (the app's own help demands it)")
            ck("walks toward the camera" in _vp, "the ACTION reaches the form in a video mode")
            ck("STILL BLOCK" not in _vp, "...and the still block is NOT what a video gets")
            ck("looking straight at the camera" not in _vp,
               "...and the video prompt does not tell him to stare")
            ck(st["refs"][:1] == [character_still("unit-video")],
               "ref 1 is the character's own still")

            # 4 . still vs VIDEO: a character must not tell a video to stare down the lens
            _st = compose_character_prompt({"Head size": 3})
            _vd = compose_character_prompt({"Head size": 3}, for_video=True,
                                           action="walks toward the camera")
            ck(_st.endswith("85mm lens.") and "looking straight at the camera" in _st,
               "the STILL block asks him to look down the lens")
            ck("looking straight at the camera" not in _vd,
               "the VIDEO block does NOT tell him to stare at the camera")
            ck("walks toward the camera" in _vd, "the ACTION lands in the video block")
            ck(_vd.endswith(CHAR_VIDEO_TAIL), "the video block carries the video tail")
            ck(_st != _vd, "still and video blocks genuinely differ")
            ck(compose_character_prompt({}, for_video=True) ==
               compose_character_prompt({}, for_video=True, action="   "),
               "a blank action is the same as no action")
            ck("85mm lens" not in compose_character_prompt({}, for_video=True),
               "...and the video tail never mentions 85mm")

            # 5 . ROUND TRIP through the real handlers. Two silent-failure bugs lived in this
            # store today, so the point is not one field - it is that NO field is dropped.
            # fresh window: the one above was destroyed in check 2, and a destroyed widget tree
            # raises TclError (which the driver fail-fast now turns into an exit, not a hang).
            _w2 = open_character_workspace()
            root.update_idletasks()
            W = _w2.char_widgets
            _w2.char_fill(None, None)
            # ⚠️ CLEAR first. _fill() seeds a new character's seed field with "-1", so a bare
            # insert produced "777-1" and the assertion failed on MY test, not the app.
            W["name"].delete(0, "end")
            W["name"].insert(0, "Round Trip")
            W["action"].delete("1.0", "end")
            W["action"].insert("1.0", "leans on a wall, arms folded")
            W["seed"].delete(0, "end")
            W["seed"].insert(0, "777")
            _act_before = W["action"].get("1.0", "end").strip()
            _g = _w2.char_grab()
            ck(_g.get("action") == "leans on a wall, arms folded",
               "_grab() picks the ACTION up out of the editor")
            _w2.char_write("round-trip", _g)
            _m = read_character("round-trip") or {}
            ck(_m.get("name") == "Round Trip", "the name survives a save")
            ck(_m.get("seed") == "777", "the seed survives a save")
            ck(_m.get("action") == "leans on a wall, arms folded", "the ACTION survives a save")
            ck(isinstance(_m.get("picks"), dict), "picks survive a save")
            ck("saved" in _m, "a save stamps the time")
            # ⚠️ THE STRUCTURAL GUARD: anything _grab() offers must land in the file. A field
            # added to one and not the other is exactly how today's bugs got in.
            _missing = [k for k in _g if k not in ("still_src",) and k not in _m]
            ck(not _missing, "every field _grab() produces is WRITTEN (missing: %r)" % _missing)
            # and loading it back into the editor restores what was typed
            _w2.char_fill(_m, "round-trip")
            root.update_idletasks()
            ck(_w2.char_widgets["action"].get("1.0", "end").strip() == _act_before,
               "...and _fill puts the ACTION back in the editor")
            ck(_w2.char_widgets["name"].get() == "Round Trip", "...and the name")
            ck(_w2.char_widgets["seed"].get() == "777", "...and the seed")
            if _w2 is not None:
                _w2.destroy()

            print("TESTCHAR %s" % ("FAILED" if fails else "done"), flush=True)
            shutil.rmtree(tmp, ignore_errors=True)
            root.after(300, root.destroy)
        root.after(2500, _tc)

    # ---- test driver: the WEIRDO one-press studio (no GPU - nothing here submits) ----
    if os.environ.get("FORGE_LAB_TESTWEIRDO") == "1":
        def _tw():
            import random as _rnd
            fails = []

            def ck(cond, label):
                print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
                if not cond:
                    fails.append(label)

            w = open_weirdo_workspace()
            root.update_idletasks()
            ck(w is not None and w.winfo_exists(), "the TYPE entry opens the Weirdo window")
            ck(open_weirdo_workspace() is w, "picking Weirdo again raises the SAME window")
            ck(w.weirdo_block() == "", "it opens with an empty block (nothing rolled yet)")
            ck("ROLL" in w.weirdo_widgets["status"].cget("text").upper(),
               "and tells you to press ROLL (got %r)"
               % w.weirdo_widgets["status"].cget("text"))

            # ---- the roll ----
            w.weirdo_roll()
            root.update_idletasks()
            blk = w.weirdo_block()
            ck(blk.startswith("photorealistic"), "a roll composes a block")
            ck("full body shot" in blk, "and it is a full-body character")
            ck(w.weirdo_widgets["action"].get("1.0", "end").strip() != "",
               "a roll fills in an ACTION too, so the video has something to do")
            ck(blk.endswith("85mm lens."), "the still block carries the deadpan still tail")

            # ⚠️ THE IDIOM, ASSERTED: ONE thing wrong, everything else ordinary. A roll that
            # breaks three things is noise, not comedy, and that is the whole point of this window.
            # ---- DESCRIBE IT: words in the box -> the roll is FITTED to them (model stubbed) ----
            import director as _dmod
            _real_fit = _dmod.Director.weirdo_fit
            _calls = []

            def _fake_fit(self, description, vein="", base=None, action="", model=None):
                _calls.append(description)
                return {"subject": "an old fisherman", "skin": "", "hair": "wiry white hair",
                        "facial_hair": "", "wardrobe": "yellow oilskins and rubber boots",
                        "accessory": "", "setting": "a wet wooden dock at dawn",
                        "lighting": "flat grey light", "absurd": "enormous shovel-sized hands",
                        "action": "mends a fishing net with total concentration"}
            _dmod.Director.weirdo_fit = _fake_fit
            try:
                ck(w.weirdo_desc.get("1.0", "end").strip() == "",
                   "the DESCRIBE IT box opens EMPTY (an empty box = the classic random roll)")
                w.weirdo_desc.insert("1.0", "an old fisherman with huge hands")
                w.weirdo_roll()
                _t0 = time.time()
                while w.weirdo_state.get("fitting") and time.time() - _t0 < 10:
                    root.update(); time.sleep(0.05)
                _b = w.weirdo_block()
                ck(_calls == ["an old fisherman with huge hands"],
                   "ROLL sends what you TYPED to the model (got %r)" % _calls)
                ck("an old fisherman" in _b and "yellow oilskins" in _b
                   and "enormous shovel-sized hands" in _b,
                   "the block is built FROM the description")
                _hot = [k for k, v in w.weirdo_state["picks"].items()
                        if (k == "Feature" and v) or (k in CHAR_SLIDER_ORDER and v)]
                ck(_hot == ["Feature"], "a described roll still breaks EXACTLY ONE thing (got %r)"
                   % _hot)
                ck("mends a fishing net" in w.weirdo_widgets["action"].get("1.0", "end"),
                   "and fills the ACTION from it")
                ck("your description" in w.weirdo_widgets["status"].cget("text"),
                   "the status says it used your description (got %r)"
                   % w.weirdo_widgets["status"].cget("text"))

                def _boom(self, *a, **k):
                    raise _dmod.DirectorError("forge:11434 unreachable")
                _dmod.Director.weirdo_fit = _boom
                w.weirdo_roll()
                _t0 = time.time()
                while w.weirdo_state.get("fitting") and time.time() - _t0 < 10:
                    root.update(); time.sleep(0.05)
                ck(w.weirdo_block() == _b, "a FAILED fit leaves the block alone (no silent "
                                           "random roll pretending to be yours)")
                ck("could not fit" in w.weirdo_widgets["status"].cget("text"),
                   "and says so (got %r)" % w.weirdo_widgets["status"].cget("text"))
            finally:
                _dmod.Director.weirdo_fit = _real_fit
                w.weirdo_desc.delete("1.0", "end")
            _calls_before = len(_calls)
            w.weirdo_roll(); root.update_idletasks()
            ck(len(_calls) == _calls_before and not w.weirdo_state.get("fitting"),
               "with the box emptied again, ROLL is instant and calls no model")

            # ---- the VEIN selector: the window opens on the Formal-European vein ----
            ck(w.weirdo_vein_var.get() == WEIRDO_VEIN_DEFAULT,
               "the window opens on the %s vein (got %r)"
               % (WEIRDO_VEIN_DEFAULT, w.weirdo_vein_var.get()))

            picks = w.weirdo_state["picks"]
            hot = [k for k, v in picks.items()
                   if (k == "Feature" and v) or (k in CHAR_SLIDER_ORDER and v)]
            ck(len(hot) == 1, "a roll breaks EXACTLY ONE thing (got %r)" % hot)
            ck(picks.get("Style") == "photorealistic", "and stays hyper-real")
            ck(picks.get("Framing") == CHAR_OPTION_DEFAULTS["Framing"],
               "and full-body, because the video wants the whole figure")
            # Formal-European: ordinary skin/hair/beard, but a PERIOD wardrobe, a European
            # square and European light - and a signature MOTION, not a micro-action.
            for _ax in ("Skin", "Hair", "Facial hair"):
                ck(picks.get(_ax) in dict(CHAR_OPTIONS)[_ax][:WEIRDO_ORDINARY[_ax]],
                   "%s is still an ordinary choice (got %r)" % (_ax, picks.get(_ax)))
            ck(picks.get("Wardrobe") in WEIRDO_EURO_WARDROBE,
               "the Formal-European vein dresses them for 1900 (got %r)"
               % picks.get("Wardrobe"))
            ck(picks.get("Setting") in WEIRDO_EURO_SETTING,
               "and stands them in a European square (got %r)" % picks.get("Setting"))
            ck(picks.get("Lighting") in WEIRDO_EURO_LIGHTING,
               "under European overcast light (got %r)" % picks.get("Lighting"))
            ck(w.weirdo_state["action"] in WEIRDO_ACTIONS_EURO,
               "with a signature MOTION, not a micro-action (got %r)"
               % w.weirdo_state["action"])

            # ---- the Modern vein keeps today's studio weirdo exactly ----
            w.weirdo_vein_var.set("Modern")
            w.weirdo_roll()
            root.update_idletasks()
            _pm = w.weirdo_state["picks"]
            ck(_pm.get("Setting") == dict(CHAR_OPTIONS)["Setting"][0],
               "the Modern vein keeps the plain studio backdrop (got %r)" % _pm.get("Setting"))
            ck(_pm.get("Wardrobe") in dict(CHAR_OPTIONS)["Wardrobe"][:WEIRDO_ORDINARY["Wardrobe"]],
               "and ordinary modern clothes (got %r)" % _pm.get("Wardrobe"))
            ck(w.weirdo_state["action"] in WEIRDO_ACTIONS,
               "and a mundane micro-action (got %r)" % w.weirdo_state["action"])
            for _ax, _n in WEIRDO_ORDINARY.items():
                ck(_pm.get(_ax) in dict(CHAR_OPTIONS)[_ax][:_n],
                   "Modern %s is an ordinary choice (got %r)" % (_ax, _pm.get(_ax)))
            w.weirdo_vein_var.set(WEIRDO_VEIN_DEFAULT)     # back to the default for the rest

            # 20 pinned rolls PER VEIN: the rule must hold EVERY time, not on average
            for _v in WEIRDO_VEINS:
                _bad = []
                for _i in range(20):
                    _p, _a = roll_weirdo(_rnd.Random(_i), vein=_v)
                    _h = [k for k, v in _p.items()
                          if (k == "Feature" and v) or (k in CHAR_SLIDER_ORDER and v)]
                    if len(_h) != 1:
                        _bad.append((_i, _h))
                ck(not _bad, "20 pinned %s rolls ALL break exactly one thing (bad: %r)"
                   % (_v, _bad))
                _acts = {roll_weirdo(_rnd.Random(i), vein=_v)[1] for i in range(40)}
                ck(all(" his " not in a for a in _acts),
                   "no %s action uses a gendered pronoun" % _v)
                ck(len(_acts) >= 6, "the %s vein offers a real spread of actions (%d)"
                   % (_v, len(_acts)))
            # an unknown vein must not crash a roll - it falls back to the default
            ck(roll_weirdo(_rnd.Random(3), vein="nonsense")[0].get("Setting")
               in WEIRDO_EURO_SETTING,
               "an unknown vein falls back to Formal-European")

            # ---- the still size ----
            ck("MP" in w.weirdo_widgets["size_lbl"].cget("text"),
               "the still size is SHOWN (got %r)" % w.weirdo_widgets["size_lbl"].cget("text"))
            ck(w.weirdo_size_var.get() == "Fast",
               "the still defaults to FAST for rolling (got %r)" % w.weirdo_size_var.get())

            # ---- the video params ----
            _vp = weirdo_video_params("x", w.weirdo_canvas_var.get(), w.weirdo_len_var.get())
            ck((_vp["length"] - 5) % 17 == 0,
               "the video length is a legal H3 count n*17+5 (got %d)" % _vp["length"])
            ck(_vp["steps"] == 12,
               "the video uses Standard steps, never Draft 4 (got %d)" % _vp["steps"])
            ck(_vp["video_mode"] == "r2v", "and the r2v path, so the still IS the character")
            ck(_vp["width"] % 32 == 0 and _vp["height"] % 32 == 0,
               "the canvas is a multiple of 32 (%dx%d)" % (_vp["width"], _vp["height"]))
            for _L in [k for k, _ in WEIRDO_VIDEO_LENGTHS]:
                _n = weirdo_video_params("x", WEIRDO_VIDEO_CANVAS_DEFAULT, _L)["length"]
                ck((_n - 5) % 17 == 0, "length %s -> %d frames is legal" % (_L, _n))
            for _c, (_cw, _ch) in WEIRDO_VIDEO_CANVAS:
                ck(_cw % 32 == 0 and _ch % 32 == 0, "canvas %s is a multiple of 32" % _c)

            # ---- the guards that stop a pointless GPU job ----
            ck(w.weirdo_state["still"] is None, "no still yet, so...")
            w.weirdo_video()
            root.update_idletasks()
            ck("RENDER STILL first" in w.weirdo_widgets["status"].cget("text"),
               "...MAKE THE VIDEO SAYS so instead of submitting (got %r)"
               % w.weirdo_widgets["status"].cget("text"))
            ck(w.weirdo_state["busy"] is None, "and it did not start a job")
            ck(w.weirdo_frames["frames"] == [], "no frames loaded yet")
            w.weirdo_play()
            root.update_idletasks()
            ck("make the video first" in w.weirdo_widgets["status"].cget("text"),
               "PLAY says so instead of doing nothing (got %r)"
               % w.weirdo_widgets["status"].cget("text"))

            # ---- buttons readable (the documented on-demand _btn_colors trap) ----
            _btns = []

            def _walk3(x):
                for ch in x.winfo_children():
                    if ch.winfo_class() in ("Button", "Menubutton"):
                        _btns.append(ch)
                    _walk3(ch)
            _walk3(w)
            _dark = [b for b in _btns if str(b.cget("fg")).lower() == "#14110f"]
            ck(len(_btns) >= 6, "the window has its buttons and dropdowns (%d)" % len(_btns))
            ck(len(_dark) == len(_btns),
               "every one has DARK text (%d of %d) - the readability fix"
               % (len(_dark), len(_btns)))

            # ---- THE TAKES FEED, while this window is open ----------------------------------
            # Ryan, 2026-10-11: "new renders in the weirdo page should show up in the takes in
            # the lab forge." A workspace render writes runs/index.json (submit()/collect()),
            # and nothing used to tell the main window: load_takes() ran at startup and after a
            # MAIN-FORM run only, so a Weirdo render was already in the index - and there after a
            # relaunch - while the already-open Takes panel never changed.
            #
            # ⚠️ Driven on a THROWAWAY runs dir (RUNS swapped in globals(), restored below) so it
            # touches NONE of Ryan's real takes, and it proves the LIVE path: a landing take must
            # reach the panel with no click and no main-window action of any kind.
            def _pump(sec=1.6):
                _t = time.time()
                while time.time() - _t < sec:
                    root.update()
                    time.sleep(0.03)

            _g = globals()
            _runs_before = _g["RUNS"]
            _td = tempfile.mkdtemp(prefix="forge_lab_takesfeed_")
            try:
                _g["RUNS"] = _td
                os.makedirs(os.path.join(_td, "seedtake"), exist_ok=True)
                json.dump([{"id": "seedtake", "mode": "t2i", "label": "SEED",
                            "prompt": "seed", "started": time.time(),
                            "status": "success", "images": []}],
                          open(os.path.join(_td, "index.json"), "w"), indent=1)
                takes_filter_var.set("All")
                st["take_unkept"] = False
                load_takes()
                _pump()
                ck(len(take_rows) == 1,
                   "the takes feed runs on a throwaway index (1 seed row, got %d)"
                   % len(take_rows))
                # exactly what a Weirdo/Song/Studio/long-video render does when it lands
                _index_insert({"id": "landedtake", "mode": "t2i", "label": "LANDED",
                               "prompt": "x", "started": time.time(),
                               "status": "success", "images": []})
                _pump()          # <- no click, no main-window action, nothing but time
                ck(len(take_rows) == 2,
                   "a take LANDING reaches Takes with NO click (got %d rows)" % len(take_rows))
                ck("2 of 2" in takes_hdr.cget("text"),
                   "and the header counts it (%r)" % takes_hdr.cget("text"))
            finally:
                _g["RUNS"] = _runs_before
                shutil.rmtree(_td, ignore_errors=True)
            load_takes()          # put Ryan's real list back exactly as it was
            _pump(1.2)

            # ---- LIVE (opt-in): actually roll, render a still, render a video, load frames ----
            # The assertions above never touch the GPU, which is right for a repeatable test but
            # proves nothing about the render path. FORGE_LAB_TESTWEIRDO_GO=1 spends real time to
            # prove the whole chain - roll -> t2i -> r2v -> ffmpeg frames -> the player.
            if os.environ.get("FORGE_LAB_TESTWEIRDO_GO") == "1":
                print("  -- LIVE: rolling and rendering for real --", flush=True)
                w.weirdo_size_var.set("Draft")          # keep the spend small
                w.weirdo_roll()
                w.weirdo_still()
                _t0 = time.time()
                while w.weirdo_state["busy"] and time.time() - _t0 < 600:
                    time.sleep(1)
                    root.update()
                ck(w.weirdo_state["still"] is not None,
                   "LIVE: a still rendered (%s)" % (w.weirdo_state["still"] or "none"))

                # ⚠️ THE ACTUAL CLAIM OF THIS RUN — Ryan: "new renders in the weirdo page should
                # show up in the takes in the lab forge." The still above landed through the REAL
                # submit() -> collect() on the REAL runs dir, so the open Takes panel must now show
                # it with NO click and nothing touched in the main window.
                def _in_takes(pid, secs=15.0):
                    """Seconds until `pid` is in the panel, or None if it never arrives.

                    ⚠️ `st["_takes_data"]` holds (entry, path) PAIRS, not the entries — see
                    render_takes(). A `.get` on the pair is an AttributeError, which the fail-fast
                    callback handler turns into exit 3, so it fails loudly instead of hanging."""
                    t0 = time.time()
                    while time.time() - t0 < secs:
                        if pid in [it.get("id") for it, _p in (st.get("_takes_data") or [])]:
                            return time.time() - t0
                        root.update()
                        time.sleep(0.05)
                    return None

                def _pid_of(pth):
                    return os.path.basename(os.path.dirname(pth or ""))

                _st_pid = _pid_of(w.weirdo_state["still"])
                _lag = _in_takes(_st_pid)
                ck(_lag is not None,
                   "LIVE: the STILL reached Takes by itself (pid %s%s)"
                   % (_st_pid[:8], "" if _lag is None else ", %.1fs after the render" % _lag))

                w.weirdo_len_var.set("3 s")
                w.weirdo_video()
                _t0 = time.time()
                while w.weirdo_state["busy"] and time.time() - _t0 < 1500:
                    time.sleep(1)
                    root.update()
                ck(w.weirdo_state["video"] is not None,
                   "LIVE: a video rendered (%s)" % (w.weirdo_state["video"] or "none"))
                _vd_pid = _pid_of(w.weirdo_state["video"])
                _vlag = _in_takes(_vd_pid)
                ck(_vlag is not None,
                   "LIVE: the VIDEO reached Takes by itself (pid %s%s)"
                   % (_vd_pid[:8], "" if _vlag is None else ", %.1fs after the render" % _vlag))
                ck(len(w.weirdo_frames["frames"]) > 0,
                   "LIVE: the player loaded frames (%d)" % len(w.weirdo_frames["frames"]))
                if w.weirdo_frames["frames"]:
                    w.weirdo_play()
                    root.update()
                    ck(w.weirdo_frames["after"] is not None, "LIVE: PLAY actually runs")
                    ck(w.weirdo_widgets["play_btn"].cget("text") == "PAUSE",
                       "LIVE: and the button flips to PAUSE")

            # ⚠️ GPU-FREE guard on the PLAYER's ffmpeg call, run against a synthetic clip.
            # `-vsync` is rejected by this build, and that failure is invisible until a real
            # video exists - which is exactly how it got in. This runs the REAL command.
            _td = tempfile.mkdtemp(prefix="forge_lab_playtest_")
            _src = os.path.join(_td, "src.mp4")
            _mk = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                                  "-i", "testsrc=size=64x64:rate=24:duration=1",
                                  "-pix_fmt", "yuv420p", _src],
                                 capture_output=True, text=True, timeout=120)
            ck(_mk.returncode == 0 and os.path.exists(_src), "the player test clip builds")
            _ex = weirdo_player_frames(_src, _td)
            ck(_ex.returncode == 0,
               "the PLAYER's ffmpeg call is ACCEPTED by this build (%s)"
               % ((_ex.stderr or "").strip()[:90] or "no error"))
            _got = [x for x in os.listdir(_td) if x.startswith("f_")]
            ck(len(_got) > 0, "and it produced frames (%d)" % len(_got))

            print("TESTWEIRDO %s" % ("FAILED" if fails else "done"), flush=True)
            if w is not None:
                w.destroy()
            root.after(300, root.destroy)
        root.after(2600, _tw)

# ---- test driver: the SONG window (GPU-free; FORGE_LAB_TESTSONG_GO=1 makes a real song) ----
    if os.environ.get("FORGE_LAB_TESTSONG") == "1":
        def _tso():
            import song as _song
            fails = []
            # ⚠️ no-footprint probe: a driver must not touch the saved words (the window's
            # _load_state/_save_state are both gated on _DRIVER for exactly this reason).
            _state_f = os.path.join(DATA, "song.json")

            def _state_sig():
                try:
                    with open(_state_f, encoding="utf-8") as fh:
                        return fh.read()
                except Exception:
                    return None
            _sig0 = _state_sig()

            def ck(cond, label):
                print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
                if not cond:
                    fails.append(label)

            w = open_song_workspace()
            root.update_idletasks()
            ck(w is not None and w.winfo_exists(), "the TYPE entry opens the Song window")
            ck(open_song_workspace() is w, "picking Song again raises the SAME window")
            d = w.song_dump()
            ck(d["plan"] == "full", "it opens on the full plan (melody + chords)")
            ck(d["style"] == _song.STYLE_PRESETS[0][1],
               "with the first STYLE preset already in the box")
            ck(d["lyrics_len"] == 0, "and an empty lyric box (an instrumental is allowed)")
            ck(d["duration"] == int(_song.YUE2_DEFAULT_SECONDS), "at the template's 120 s")
            ck(d["seed"] == "-1", "with a roll-a-new-one seed")
            ck(len(d["presets"]) >= 6, "the START FROM menu carries presets (%d)" % len(d["presets"]))
            ck("MAKE" in d["status"].upper(), "and it says what to do (got %r)" % d["status"])

            # the estimate ON SCREEN must be the calibrated law plus the engine-switch term —
            # not a second formula that drifts away from the one submit() is costed with.
            want = round(_song.yue2_estimate_seconds(d["duration"], True) + YUE2_SWITCH_S, 1)
            ck(abs(d["estimate"] - want) < 0.11,
               "the estimate on screen is the calibrated law (%.1f vs %.1f)" % (d["estimate"], want))
            ck("upper bound" in d["estimate_text"],
               "and it is LABELLED an upper bound (got %r)" % d["estimate_text"])

            # the PLAN control: three states, and OFF must estimate cheaper than FULL
            for _state in ("off", "melody", "full"):
                w.song_set(plan=_state)
                root.update_idletasks()
                ck(w.song_dump()["plan"] == _state, "the PLAN toggle reaches %s" % _state)
            w.song_set(plan="off")
            off_est = w.song_dump()["estimate"]
            w.song_set(plan="full")
            full_est = w.song_dump()["estimate"]
            ck(off_est < full_est,
               "no-plan estimates cheaper than planning (%.1f < %.1f)" % (off_est, full_est))

            # the LENGTH control is a SLIDER (the house rule for anything proportional)
            w.song_set(seconds=300)
            root.update_idletasks()
            ck(w.song_dump()["duration"] == 300, "the LENGTH slider reaches 300 s")
            ck(w.song_dump()["estimate"] > full_est, "and a longer song estimates higher")

            # MAKE refuses an empty STYLE rather than submitting a graph with nothing to sing
            w.song_set(style="", lyrics="[Verse]\nx\n")
            root.update_idletasks()
            w.song_make()
            root.update_idletasks()
            ck(not w.song_dump()["busy"], "MAKE with an empty STYLE starts no job")
            ck("STYLE" in w.song_dump()["status"].upper(),
               "and says why (got %r)" % w.song_dump()["status"])

            # ---- buttons readable (the documented on-demand _btn_colors trap) ----
            # ⚠️ NOT "every button is dark text": that is only true of the buttons
            # _btn_colors repaints (ink fill + #14110f). The house TOGGLE idiom deliberately
            # uses ink-on-panel2 when OFF and white-on-rust_deep when ON. The rule that
            # actually matters is CONTRAST, so measure it (the same WCAG ratio TESTCOLOR uses).
            def _lum(h):
                h = str(h).lstrip("#")
                if len(h) == 3:
                    h = "".join(c * 2 for c in h)
                if len(h) != 6:
                    return None
                r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
                _f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                return 0.2126 * _f(r) + 0.7152 * _f(g) + 0.0722 * _f(b)

            def _ratio(a, b):
                la, lb = _lum(a), _lum(b)
                if la is None or lb is None:
                    return None
                hi, lo = max(la, lb), min(la, lb)
                return (hi + 0.05) / (lo + 0.05)

            _btns = []

            def _walk3(x):
                for ch in x.winfo_children():
                    if ch.winfo_class() in ("Button", "Menubutton"):
                        _btns.append(ch)
                    _walk3(ch)
            _walk3(w)
            _bad = [(b.cget("text"), b.cget("fg"), b.cget("bg")) for b in _btns
                    if (_ratio(b.cget("fg"), b.cget("bg")) or 0) < 4.5]
            ck(len(_btns) >= 10, "the window has its buttons and dropdowns (%d)" % len(_btns))
            ck(not _bad,
               "every button's text clears 4.5:1 against its own fill (%d of %d failed: %r)"
               % (len(_bad), len(_btns), _bad[:3]))

            # ---- REUSE WORDS: the lyrics you already wrote are never retyped ----
            _reuse_b = next((b for b in _btns if str(b.cget("text")) == "REUSE WORDS"), None)
            ck(_reuse_b is not None, "there is a REUSE WORDS button")
            ck(bool(_reuse_b and _reuse_b.winfo_ismapped()), "and it is on screen")
            _lst = last_song_take()
            w.song_set(style="placeholder", lyrics="")
            root.update_idletasks()
            _before = w.song_dump()["words_chars"]
            w.song_reuse_words()
            root.update_idletasks()
            _d6 = w.song_dump()
            if _lst:
                ck(_d6["words_chars"] > _before,
                   "pressing it brings your saved words back (%d -> %d chars)"
                   % (_before, _d6["words_chars"]))
                ck(_d6["lyrics_len"] > 0,
                   "including the LYRICS, which is the whole point (%d chars)" % _d6["lyrics_len"])
                ck(_d6["style"] == (_lst.get("style") or ""),
                   "and the style that went with them")
                ck("last song" in str(_d6["status"]),
                   "it says where they came from (%r)" % _d6["status"])
            else:
                ck("no saved words" in str(_d6["status"]),
                   "with nothing saved it SAYS so, instead of doing nothing silently (%r)"
                   % _d6["status"])

            # ⚠️ THE LONG-VIDEO LESSON, re-applied: a control that does not FIT is never
            # MAPPED, silently — "a clipped control still works". A button and a hint were
            # added to the lyrics toolbar row, so MEASURE it instead of trusting it.
            _tagrow = _reuse_b.master if _reuse_b is not None else None
            if _tagrow is not None:
                root.update_idletasks()
                _tw = _tagrow.winfo_width()
                _kids = _tagrow.winfo_children()
                _unmapped = [c for c in _kids if not c.winfo_ismapped()]
                _past = [c for c in _kids
                         if c.winfo_ismapped() and (c.winfo_x() + c.winfo_width()) > _tw + 2]
                ck(not _unmapped,
                   "every control in the lyrics toolbar is MAPPED (%d of %d unmapped: %r)"
                   % (len(_unmapped), len(_kids),
                      [c.cget("text") for c in _unmapped if c.winfo_class() == "Button"]))
                ck(not _past,
                   "and none runs past the row (%d px) - a clipped control still 'works' "
                   "(over: %r)" % (_tw, [c.cget("text") for c in _past
                                         if c.winfo_class() == "Button"]))

            # ---- WRITE LYRICS: the rig's own model writes the words ----
            _wl = next((b for b in _btns if str(b.cget("text")) == "WRITE LYRICS"), None)
            ck(_wl is not None, "there is a WRITE LYRICS button")
            ck(bool(_wl and _wl.winfo_ismapped()), "and it is on screen")
            ck(w.song_dump()["write_mapped"], "the write button is mapped, not starved")
            ck(bool(w.song_dump()["llm"]),
               "a local model is selected (%r)" % w.song_dump()["llm"])
            # the model list is fetched from the rig on a worker thread.
            # ⚠️ Wait on the NOTE, not on the menu being non-empty: the menu starts with the
            # default, so polling for "non-empty" returns instantly and the assertion passes
            # vacuously with one entry — a test that cannot fail.
            _t0 = time.time()
            while "checking" in str(w.song_dump()["llm_note"]) and time.time() - _t0 < 20:
                time.sleep(0.5)
                root.update()
            _choices = w.song_llm_choices()
            ck(len(_choices) >= 2,
               "the rig's local models are listed (%d: %r)" % (len(_choices), _choices[:5]))
            ck(w.song_dump()["llm"] in _choices, "and the selection is one of them")
            ck(all("embed" not in c.lower() for c in _choices),
               "the embedding models are left out \u2014 they cannot write anything")
            # nothing to go on: it must REFUSE rather than invent a song
            w.song_set(style="", lyrics="")
            w.song_about_var.set("")
            root.update_idletasks()
            w.song_write_lyrics()
            root.update_idletasks()
            ck(not w.song_writing(), "with nothing to go on it does not start a job")
            ck("ABOUT" in str(w.song_dump()["status"]).upper(),
               "and says what it needs (%r)" % w.song_dump()["status"])

            # ---- LIVE (opt-in): let the rig actually write a set of lyrics ----
            if os.environ.get("FORGE_LAB_TESTSONG_LYRICS") == "1":
                import director as _dd
                print("  -- LIVE: writing lyrics on the rig --", flush=True)
                w.song_set(style=_song.STYLE_PRESETS[0][1], lyrics="")
                w.song_about_var.set("a dog who is scared of the cat and of the baby")
                root.update_idletasks()
                w.song_write_lyrics()
                _t0 = time.time()
                while w.song_writing() and time.time() - _t0 < 300:
                    time.sleep(1)
                    root.update()
                _txt = w.song_lyrics_text()
                ck(len(_txt) > 0, "LIVE: the rig's model filled the lyric box (%d chars)"
                   % len(_txt))
                ck(_dd.validate_lyrics(_txt) == [],
                   "LIVE: and the words PASS the validator (%r)" % _dd.validate_lyrics(_txt))
                ck("[Chorus]" in _txt, "LIVE: with a chorus in them")
                ck("wrote" in str(w.song_dump()["status"]).lower(),
                   "LIVE: and the window reports the title (%r)" % w.song_dump()["status"])

            # nothing can be starved: the column SCROLLS, and the make button is on screen
            ck(w.song_dump()["canvas_scrolls"], "the right column has a real scrollregion")
            ck(w.song_dump()["make_mapped"], "MAKE THE SONG is mapped (not starved off-screen)")
            ck(w.winfo_reqheight() <= w.winfo_height() + 2,
               "the content fits the window height (%d vs %d)"
               % (w.winfo_reqheight(), w.winfo_height()))

            # ---- COVERS: the picker flips the whole shape of the run ----
            _src = os.path.expanduser("~/forge-lab/runs/c41099f1-1b09-4924-8924-50ccd830874c/"
                                      "natasha_00002.flac")
            ck(w.song_dump()["cover"] is None, "it opens as an ORIGINAL (no cover picked)")
            ck(w.song_dump()["make_text"] == "MAKE THE SONG", "and the button says so")
            ck("none" in w.song_dump()["cover_label"], "the picker reads none")
            ck(w.song_dump()["plan_disabled"] == 0, "and the plan toggles are all live")
            # ⚠️ Pin the LENGTH first: the slider test above left it at 300 s, and picking a
            # recording resets it to the recording's own length. Comparing across those two
            # durations compares nothing — the transcription has to be isolated at ONE length.
            w.song_set(seconds=120)
            root.update_idletasks()
            _orig_est = w.song_dump()["estimate"]

            if os.path.exists(_src):
                w.song_set_cover(_src)
                root.update_idletasks()
                d3 = w.song_dump()
                ck(d3["cover"] == _src, "picking a recording puts it in the cover slot")
                ck(os.path.basename(_src) in d3["cover_label"],
                   "the window shows WHICH recording (got %r)" % d3["cover_label"])
                ck(d3["plan"] == "melody",
                   "a cover is forced to melody mode — the model's own setting for covers")
                ck(d3["plan_disabled"] == 2,
                   "and full/off are DISABLED, so a combination the cover graph cannot honour "
                   "is not selectable (got %d disabled)" % d3["plan_disabled"])
                ck(d3["make_text"] == "MAKE THE COVER", "the button changes to MAKE THE COVER")
                ck(d3["duration"] == 120,
                   "the length follows the recording's own length (got %s s)" % d3["duration"])
                ck(d3["estimate"] > _orig_est,
                   "and the estimate goes UP for the transcription (%.1f > %.1f)"
                   % (d3["estimate"], _orig_est))

                # ⚠️ THE BUTTON'S MEANING FOLLOWS THE COVER. Ryan: "not for the last song for
                # the cover song" — with a recording armed the words he wants are THAT song's.
                ck(d3["reuse_text"] == "COVER'S WORDS",
                   "the words button re-labels itself for the cover (got %r)" % d3["reuse_text"])
                ck(d3["lyrics_len"] > 0,
                   "and the words already in the box were NOT clobbered by picking a recording")
                _words_before = d3["lyrics_len"]

                w.song_set(lyrics="")           # an empty box: picking should FILL it
                root.update_idletasks()
                w.song_set_cover(_src)
                root.update_idletasks()
                ck(w.song_dump()["lyrics_len"] > 0,
                   "picking a recording with an EMPTY box fills in the covered song's words "
                   "(%d chars)" % w.song_dump()["lyrics_len"])
                ck("loaded from" in str(w.song_dump()["status"]),
                   "and says so (%r)" % w.song_dump()["status"])

                w.song_set(lyrics="")
                root.update_idletasks()
                w.song_reuse_words()
                root.update_idletasks()
                _d7 = w.song_dump()
                ck(_d7["lyrics_len"] > 0,
                   "COVER'S WORDS brings them back from a cleared box (%d chars)"
                   % _d7["lyrics_len"])
                ck("song you are covering" in str(_d7["status"]),
                   "and names the COVERED song, not the last one (%r)" % _d7["status"])

                w.song_set_cover(None)
                root.update_idletasks()
                d4 = w.song_dump()
                ck(d4["cover"] is None and d4["make_text"] == "MAKE THE SONG",
                   "CLEAR puts it back to an original")
                ck(d4["plan_disabled"] == 0, "and re-enables the plan toggles")
            else:
                ck(False, "the test recording for the cover checks exists (%s)" % _src)

            # ---- LIVE (opt-in): make ONE real song through the real handler and PLAY it ----
            # The assertions above never touch the GPU, which is right for a repeatable test
            # but proves nothing about the render path. This one spends ~20 s of forge.
            if os.environ.get("FORGE_LAB_TESTSONG_GO") == "1":
                print("  -- LIVE: making a real song (45 s, plan=full) --", flush=True)
                w.song_set(style=_song.STYLE_PRESETS[0][1],
                           lyrics="[Verse]\nMorning light across the window\nCity waking "
                                  "down below\n\n[Chorus]\nRun with me into the sunlight\n"
                                  "Leave the shadows far behind\n",
                           plan="full", seconds=45)
                w.song_make()
                _t0 = time.time()
                while w.song_dump()["busy"] and time.time() - _t0 < 600:
                    time.sleep(1)
                    root.update()
                d2 = w.song_dump()
                ck(d2["songs"] >= 1, "LIVE: a song came back (%s)" % d2.get("made"))
                ck(bool(d2.get("made")) and os.path.exists(d2["made"] or ""),
                   "LIVE: the flac is on disk at %s" % d2.get("made"))
                ck((d2.get("made") or "").endswith(".flac"), "LIVE: it is a flac (lossless)")
                ck(str(d2["status"]).startswith("done"),
                   "LIVE: the window says done (%r)" % d2["status"])
                w.song_play()
                root.update()
                ck("playing" in str(w.song_dump()["status"]).lower(),
                   "LIVE: PLAY really starts the player (got %r)" % w.song_dump()["status"])
                w.song_stop()

            # ---- LIVE (opt-in): make a real COVER through the real handler ----
            # Proves the leg the GPU-free checks cannot: upload -> SheetSage2 -> the score
            # coming back as a file -> the audio. Uses a recording we own.
            if os.environ.get("FORGE_LAB_TESTSONG_COVER") == "1":
                _csrc = os.path.expanduser("~/forge-lab/runs/c41099f1-1b09-4924-8924-"
                                           "50ccd830874c/natasha_00002.flac")
                if not os.path.exists(_csrc):
                    ck(False, "LIVE COVER: the source recording exists (%s)" % _csrc)
                else:
                    print("  -- LIVE: covering %s --" % os.path.basename(_csrc), flush=True)
                    w.song_set(style="English, 80s arena rock power ballad, 76 BPM, huge "
                                     "gated drums, distorted electric guitars, soaring female "
                                     "lead vocal, synth strings",
                               lyrics="[Verse]\nNatasha weighs a hundred pounds of gentle\n"
                                      "She sleeps beside the door and guards the floor\n\n"
                                      "[Chorus]\nKaya's on the stairs, Kaya's in the doorway\n"
                                      "Little grey cat with a big idea\n",
                               seconds=120)
                    w.song_set_cover(_csrc)
                    w.song_make()
                    _t0 = time.time()
                    while w.song_dump()["busy"] and time.time() - _t0 < 900:
                        time.sleep(1)
                        root.update()
                    d5 = w.song_dump()
                    ck(d5["songs"] >= 1, "LIVE COVER: a cover came back (%s)" % d5.get("made"))
                    ck(bool(d5.get("made")) and os.path.exists(d5["made"] or ""),
                       "LIVE COVER: the audio is on disk at %s" % d5.get("made"))
                    _txt = os.path.join(os.path.dirname(d5.get("made") or ""), "yue2_44.txt")
                    ck(os.path.exists(_txt),
                       "LIVE COVER: the TRANSCRIBED SCORE came back too (%s)" % _txt)
                    if os.path.exists(_txt):
                        _abc = open(_txt, encoding="utf-8").read()
                        ck(len(_abc) > 200,
                           "LIVE COVER: and it is a real score (%d chars)" % len(_abc))
                        ck(_song.is_chordless_abc(_abc),
                           "LIVE COVER: melody-only, no chord symbols, as asked")
                        ck(_song.abc_note_count(_abc) > 50,
                           "LIVE COVER: with a melody in it (%d notes)"
                           % _song.abc_note_count(_abc))
                    w.song_stop()

            ck(_state_sig() == _sig0,
               "a DRIVER run did not touch Ryan's saved words (%s)"
               % os.path.basename(_state_f))

            print("TESTSONG %s" % ("FAILED" if fails else "done"), flush=True)
            try:
                w.song_stop()
            except Exception:
                pass
            if w is not None:
                w.destroy()
            root.after(300, root.destroy)
        root.after(2600, _tso)

# ---- test driver: the DIALOGUE window (GPU-free; FORGE_LAB_TESTDIALOGUE_GO=1 renders) ----
    if os.environ.get("FORGE_LAB_TESTDIALOGUE") == "1":
        def _tdi():
            import dialogue as _dlg
            fails = []

            def ck(cond, label):
                print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
                if not cond:
                    fails.append(label)

            w = open_dialogue_workspace()
            root.update_idletasks()
            ck(w is not None and w.winfo_exists(), "the TYPE entry opens the Dialogue window")
            ck(open_dialogue_workspace() is w, "picking Dialogue again raises the SAME window")
            d0 = w.dialogue_dump()
            ck(d0["lines"] == 0, "it opens with no parsed lines (got %d)" % d0["lines"])

            # Paste the episode-teaser fixture and PARSE
            fixture = """> **ALICE (V.O.)** — *(typing)* Field notes, day five: a parcel arrived for Bob.
BOB holds a dented box: a toaster that was supposed to be a kettle.
> **ALICE** — Are you going to complain?
> **BOB** — To whom? It's an app.
> **ALICE** — An app is not a person.
> **ALICE** — *(holding the receipt up to the light)* I'm writing them a letter.
**TITLE CARD: THE RETURN**"""
            w.dialogue_set_script(fixture)
            root.update_idletasks()
            w.dialogue_parse()
            root.update_idletasks()
            d = w.dialogue_dump()
            ck(d["lines"] == 5, "PARSE gives 5 lines (got %d)" % d["lines"])
            ck(len(d["speakers"]) == 2, "PARSE gives 2 speakers (got %d)" % len(d["speakers"]))
            ck("ALICE" in d["speakers"] and "BOB" in d["speakers"],
               "speakers are ALICE and BOB (got %r)" % d["speakers"])

            # Create voice ALICE via the engine, verify compose_description
            v = _dlg.new_voice("Alice")
            v["sliders"]["pace"] = 2
            desc = _dlg.compose_description(v)
            ck("no dramatic pauses" in desc,
               "pace=+2 description contains 'no dramatic pauses' (got %r)" % desc)
            # Full description check: contains age, gender, pace phrase
            ck("20-year-old male" in desc,
               "description starts with age and gender (got %r)" % desc[:50])

            # Save ALICE to the voice bank, then create BOB too
            v["characters"] = ["ALICE"]
            v["slug"] = _dlg.slugify("Alice")
            os.makedirs(os.path.join(_dlg.voices_dir(), v["slug"]), exist_ok=True)
            _dlg.save_voice(v)
            # Refresh cast dropdowns via the window
            root.update_idletasks()

            # Auto-cast
            w.dialogue_auto_cast()
            root.update_idletasks()
            d2 = w.dialogue_dump()
            ck(bool(d2.get("cast")), "auto-cast produces at least one cast entry")
            # Alice should auto-cast
            ck(d2["cast"].get("ALICE") == "alice",
               "ALICE auto-casts to 'alice' (got %r)" % d2["cast"].get("ALICE"))

            # Estimate non-empty after parse + cast
            root.update_idletasks()
            d3 = w.dialogue_dump()
            ck(bool(d3.get("estimate_text")),
               "estimate text is non-empty (got %r)" % d3.get("estimate_text"))

            # Button WCAG contrast >= 4.5 (Song window pattern)
            def _lum(h):
                h = str(h).lstrip("#")
                if len(h) == 3:
                    h = "".join(c * 2 for c in h)
                if len(h) != 6:
                    return None
                r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
                _f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                return 0.2126 * _f(r) + 0.7152 * _f(g) + 0.0722 * _f(b)

            def _ratio(a, b):
                la, lb = _lum(a), _lum(b)
                if la is None or lb is None:
                    return None
                hi, lo = max(la, lb), min(la, lb)
                return (hi + 0.05) / (lo + 0.05)

            _btns = []
            def _walk3(x):
                for ch2 in x.winfo_children():
                    if ch2.winfo_class() in ("Button", "Menubutton"):
                        _btns.append(ch2)
                    _walk3(ch2)
            _walk3(w)
            _bad = [(b.cget("text"), b.cget("fg"), b.cget("bg")) for b in _btns
                    if (_ratio(b.cget("fg"), b.cget("bg")) or 0) < 4.5]
            ck(len(_btns) >= 6, "the window has buttons and dropdowns (%d)" % len(_btns))
            ck(not _bad,
               "every button's text clears 4.5:1 against its own fill (%d of %d failed: %r)"
               % (len(_bad), len(_btns), _bad[:3]))

            # Nothing clipped — all top-level frames mapped
            _tframes = [ch for ch in w.winfo_children() if ch.winfo_class() == "Frame"]
            ck(w.winfo_ismapped(), "the dialogue window is mapped (not clipped)")
            _um = [f for f in _tframes if not f.winfo_ismapped()]
            ck(not _um, "all top-level frames are mapped (%d unmapped)" % len(_um))

            # WRITE DIALOGUE fill path (no network): a Director-shaped result lands in the box
            # in the house format and re-parses to the same speakers.
            w.dialogue_write_apply({"title": "Test", "lines": [
                {"speaker": "ALICE", "direction": "gravely", "text": "The toll booth has nobody in it?"},
                {"speaker": "BOB", "direction": "", "text": "It reads the plate."},
                {"speaker": "ALICE", "direction": "", "text": "Then I'll pay the plate."}]})
            root.update_idletasks()
            dw = w.dialogue_dump()
            ck(dw["lines"] == 3, "WRITE DIALOGUE result parses to 3 lines (got %d)" % dw["lines"])
            ck(dw.get("speakers") == ["ALICE", "BOB"],
               "WRITE DIALOGUE keeps the speakers (got %r)" % dw.get("speakers"))
            ck(any("WRITE DIALOGUE" in b for b in dw.get("buttons", [])),
               "the WRITE DIALOGUE button is on screen")

            # ACCENT presets in the real editor: pick Old English -> box + prompt follow;
            # typing a custom accent flips the menu to "custom".
            _dlg.save_voice(_dlg.new_voice("Accent Test"))
            ew = w.dialogue_edit_voice("accent-test")
            root.update_idletasks()
            es = w.dialogue_editor_state()
            ck(ew is not None and es["pick_accent"] is not None, "the voice editor opens with an accent menu")
            _oe = next(a for a, _x in _dlg.ACCENTS if a.startswith("Old English"))
            es["pick_accent"](_oe)
            root.update_idletasks()
            es = w.dialogue_editor_state()
            ck("Shakespearean" in es["accent"], "picking Old English fills the ACCENT box (%r)" % es["accent"])
            ck("Shakespearean accent" in es["desc"], "...and WHAT THE MODEL READS says it")
            ck(es["singing"] is not None, "the SINGING field is in the editor")
            ew.update(); ew.update_idletasks()
            _cv = w.dialogue_editor_state()["canvas"]
            _sr = [int(float(x)) for x in str(_cv.cget("scrollregion") or "0 0 0 0").split()]
            _inner_h = _sr[3] - _sr[1] if len(_sr) == 4 else 0
            ck(_inner_h > 0 and (_inner_h <= _cv.winfo_height() or _cv.yview() != (0.0, 1.0)),
               "the editor content is reachable: fits or SCROLLS (content %d, view %d, yview %r)"
               % (_inner_h, _cv.winfo_height(), _cv.yview()))
            ew.destroy()

            # ♪ sung via the LINES row: the estimate grows a YuE2 term
            w.dialogue_set_line_mood(1, "\u266a sung")
            root.update_idletasks()
            ck("YuE2" in w.dialogue_dump()["estimate_text"],
               "marking a line \u266a sung adds a YuE2 term to the estimate (%r)"
               % w.dialogue_dump()["estimate_text"])

            print("TESTDIALOGUE %s" % ("FAILED" if fails else "done"), flush=True)
            if w is not None:
                try:
                    if hasattr(w, 'dialogue_stop'):
                        w.dialogue_stop()
                except Exception:
                    pass
                w.destroy()
            root.after(300, root.destroy)
        root.after(2600, _tdi)

# ---- test driver: the STUDIO window (GPU-free — every pipeline/charpack call STUBBED) ----
    if os.environ.get("FORGE_LAB_TESTSTUDIO") == "1":
        def _tst():
            import zlib
            import studio_pipeline as _sp
            import charpack as _cp
            from tkinter import messagebox as _mb

            fails = []

            def ck(cond, label):
                print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
                if not cond:
                    fails.append(label)

            _errlog = os.path.join(DATA, "forge_lab_errors.log")

            def _errsize():
                try:
                    return os.path.getsize(_errlog)
                except Exception:
                    return 0
            _err0 = _errsize()

            # --- a character in the TEST data dir (FORGE_LAB_DATA) ---
            _cdir = os.path.join(CHARACTERS, "bighead")
            os.makedirs(_cdir, exist_ok=True)
            _still = os.path.join(_cdir, "still.png")
            if not os.path.exists(_still):
                def _wp(path, w, h, rgb):
                    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

                    def chunk(t, d):
                        c = t + d
                        return (struct.pack(">I", len(d)) + c
                                + struct.pack(">I", zlib.crc32(c) & 0xffffffff))
                    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" +
                                           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                                           chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
                _wp(_still, 64, 64, (180, 120, 80))
            if not os.path.exists(os.path.join(_cdir, "character.json")):
                with open(os.path.join(_cdir, "character.json"), "w") as _fh:
                    json.dump({"name": "Big Head", "still": "still.png"}, _fh)

            # --- stubs: canned and instant ---
            def _mkpj(chars, idea, length_s=20, quality="Draft", title=None, slug=None):
                pj = _sp.project_migrate({"slug": _sp.slugify(slug or title or idea or "untitled"),
                                          "title": title or idea or "untitled",
                                          "characters": list(chars), "idea": idea,
                                          "length_s": int(length_s), "quality": quality})
                _sp.save_project(pj)
                return pj
            _sp.new_project = _mkpj

            def _pitch(pj, n=5):
                pj["pitches"] = [{"title": "P%d" % i, "logline": "logline %d" % i,
                                  "hook": "hook %d" % i} for i in range(n)]
                _sp.save_project(pj)
                return pj["pitches"]
            _sp.pitch = _pitch

            def _choose(pj, i):
                pj["pitch"] = pj["pitches"][int(i)]
                _sp.save_project(pj)
                return pj["pitch"]
            _sp.choose_pitch = _choose

            def _script(pj):
                pj["script"] = {"title": "S", "hook": "the hook",
                                "beats": [{"t_start": 0, "t_end": 3, "action": "a1",
                                           "line": "hello there", "speaker": "Big Head"}],
                                "button": "the button"}
                _sp.save_project(pj)
                return pj["script"]
            _sp.write_script = _script

            def _shots(pj):
                pj["shots"] = [
                    _sp._shot_migrate({"n": 1, "seconds": 5, "intent": "lipsync", "engine": "h3",
                                       "video_mode": "r2v", "action": "a1", "line": "hello there",
                                       "prompt": "p1"}),
                    _sp._shot_migrate({"n": 2, "seconds": 3, "intent": "continue", "engine": "h3",
                                       "video_mode": "cont", "action": "a2", "line": "",
                                       "prompt": "p2"})]
                _sp.save_project(pj)
                return pj["shots"]
            _sp.plan_shots = _shots

            def _voice(pj):
                for s in pj["shots"]:
                    s["audio_wav"] = "/tmp/nope.wav"
                    s["words"] = [{"word": "hi", "start": 0, "end": 0.2}]
                _sp.save_project(pj)
                return pj["shots"]
            _sp.voice_lines = _voice

            _sp.estimate = lambda pj: {"total_s": 24.0, "per_shot": [
                {"n": s["n"], "seconds": s["seconds"], "canvas": [544, 960], "steps": 12,
                 "estimate_s": 12.0} for s in pj["shots"]]}

            def _render(pj, i, seed=-1):
                sh = [x for x in pj["shots"] if x["n"] == int(i)][0]
                pid = "syntake-%d-%d" % (i, len(sh.get("takes") or []))
                sh.setdefault("takes", []).append(pid)
                sh["chosen"] = pid
                sh["status"] = "done"
                d = os.path.join(RUNS, pid)
                os.makedirs(d, exist_ok=True)
                open(os.path.join(d, "clip.mp4"), "wb").write(b"\x00\x00\x00\x18ftypmp42")
                _sp.save_project(pj)
                return {"pid": pid, "status": "done"}
            _sp.render_shot = _render

            def _assemble(pj):
                mp4 = os.path.join(_sp.project_dir(pj["slug"]), "final.mp4")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                "testsrc=size=64x96:rate=12:duration=1", "-pix_fmt", "yuv420p",
                                mp4], capture_output=True, timeout=120)
                post = os.path.join(_sp.project_dir(pj["slug"]), "post.txt")
                with open(post, "w") as _fh:
                    _fh.write("TITLE\n\ncaption line\n\n#a #b\n")
                cut = {"mp4": mp4, "post": post}
                pj["cut"] = cut
                _sp.save_project(pj)
                return cut
            _sp.assemble = _assemble

            class _FakeDir:
                def load(self):
                    return {}

                def unload(self, model=None):
                    return {}

                def rewrite(self, text, how):
                    return ("TITLE: S\nHOOK: shorter hook (%s)\n"
                            "BEAT: 0 3 - a1 | hello there\nBUTTON: b\n" % how)
            _sp.get_director = lambda: _FakeDir()

            _cp.pack_status = lambda slug: {"slug": slug, "has_still": True, "sheet_views": 6,
                                            "has_bible": True, "has_voice": True}
            _cp.sheet_estimate = lambda slug, views=None, scale="Fast": {
                "views": [], "count": 6, "total_seconds": 99.0}
            _cp.build_sheet = lambda slug, **kw: {"views": [{"key": "front"}] * 6, "built": 6}
            _cp.make_voice = lambda slug, **kw: {"description": "a flat deadpan voice"}
            _cp.bible = lambda slug, **kw: {"who": "x", "absurd_thing": "y", "voice": "z",
                                            "running_bits": [], "never": []}
            _mb.askokcancel = lambda *a, **k: True

            def _wait(win, timeout=10.0):
                t0 = time.time()
                while win.studio_state["busy"] and time.time() - t0 < timeout:
                    root.update()
                    time.sleep(0.02)
                root.update()

            # ---- open via the TYPE menu ----
            kind_var.set("Studio")
            on_kind()
            root.update()
            w = st.get("_studio_win", {}).get("win")
            ck(w is not None and w.winfo_exists(), "the TYPE entry opens the Studio window")
            ck(open_studio_workspace() is w, "picking Studio again raises the SAME window")
            d = w.studio_dump()
            ck(d["slug"] is None and d["chars"] == [], "it opens on a NEW project form")
            ck("New project" in d["status"] and "idea" in d["status"].lower(),
               "and says what to do first (got %r)" % d["status"][:60])
            ck(w.studio_focus_target() is w.studio_widgets["idea"], "the cursor is in the IDEA entry")

            # ---- 1 CAST ----
            w.studio_select_char("bighead")
            root.update()
            d = w.studio_dump()
            ck(d["chars"] == ["bighead"], "clicking a character selects it (got %r)" % d["chars"])
            ck("pack:" in d["pack_label"], "the pack status is shown (%r)" % d["pack_label"][:44])
            ck(d["step_done"]["1"] is True and d["step_marks"]["1"] == "\u2713",
               "step 1 shows its tick ON SCREEN (mark=%r)" % d["step_marks"]["1"])
            w.studio_build_sheet()
            _wait(w)
            ck("99" in w.studio_dump()["sheet_confirm"],
               "BUILD SHEET confirms with the sheet_estimate total (%r)"
               % w.studio_dump()["sheet_confirm"][:44])
            w.studio_make_voice()
            _wait(w)
            ck("voice" in w.studio_dump()["status"].lower(),
               "MAKE VOICE reports back (%r)" % w.studio_dump()["status"][:44])
            w.studio_write_bible()
            _wait(w)
            ck("bible" in w.studio_dump()["status"].lower(),
               "WRITE BIBLE reports back (%r)" % w.studio_dump()["status"][:44])

            # ---- 2 IDEA ----
            w.studio_set_idea("he waits for a bus that is too small")
            w.studio_set_length("12")
            w.studio_set_quality("Draft")
            w.studio_pitch()
            _wait(w)
            d = w.studio_dump()
            ck(d["pitches"] == 5, "PITCH ME 5 shows 5 pitches (got %d)" % d["pitches"])
            ck(d["pitch_title"] is None, "none chosen yet")
            w.studio_choose_pitch(0)
            root.update()
            d = w.studio_dump()
            ck(d["pitch_title"] == "P0", "clicking a pitch chooses it (got %r)" % d["pitch_title"])
            ck(d["step_done"]["2"] is True and d["step_marks"]["2"] == "\u2713", "step 2 ticks")

            # USE MY IDEA on a fresh project sets a pitch from the idea
            w.studio_new()
            root.update()
            w.studio_select_char("bighead")
            w.studio_set_idea("an idea of my own")
            w.studio_set_length("12")
            w.studio_use_idea()
            root.update()
            ck(w.studio_dump()["pitch_title"] == "an idea of my own",
               "USE MY IDEA sets a pitch from your idea (got %r)" % w.studio_dump()["pitch_title"])

            # ---- 3 SCRIPT ----
            w.studio_write_script()
            _wait(w)
            d = w.studio_dump()
            ck("TITLE:" in d["script_text"] and "BEAT:" in d["script_text"],
               "WRITE SCRIPT fills the editable text")
            ck(d["step_done"]["3"] is True and d["step_marks"]["3"] == "\u2713", "step 3 ticks")
            w.studio_set_script_text("TITLE: Edited\nHOOK: h\n"
                                     "BEAT: 0 3 | - | edited action | edited line\nBUTTON: b\n")
            w.studio_save_edits()
            root.update()
            _slug = w.studio_dump()["slug"]
            try:
                with open(_sp.project_path(_slug)) as _fh:
                    _disk = json.load(_fh)
            except Exception as _exc:
                _disk = {}
                ck(False, "could not read project.json: %r" % _exc)
            _b0 = ((_disk.get("script") or {}).get("beats") or [{}])[0]
            ck(_b0.get("action") == "edited action" and _b0.get("line") == "edited line",
               "edits to the script PERSIST into project.json (got %r)" % _b0)
            w.studio_rewrite("funnier")
            _wait(w)
            ck("shorter hook" in w.studio_dump()["script_text"],
               "REWRITE (funnier) replaces the script text (got %r)"
               % w.studio_dump()["script_text"][:44])

            # ---- 4 SHOTS ----
            w.studio_plan_shots()
            _wait(w)
            d = w.studio_dump()
            ck(len(d["shots"]) == 2, "PLAN SHOTS makes the cards (got %d)" % len(d["shots"]))
            ck(d["shots"][0]["intent_plain"] == "says a line",
               "the shot card says the intent in plain language (got %r)"
               % d["shots"][0]["intent_plain"])
            ck(d["shots"][1]["intent_plain"] == "continue the shot",
               "and 'continue the shot' for a cont shot")
            ck(d["step_done"]["4"] is True and d["step_marks"]["4"] == "\u2713", "step 4 ticks")
            w.studio_voice()
            _wait(w)
            ck(w.studio_dump()["step_done"]["4"] is True, "step 4 still ticked after voicing")

            # ---- 5 RENDER ----
            w.studio_estimate()
            _wait(w)
            d = w.studio_dump()
            ck(d["estimate"] and d["estimate"]["total_s"] == 24.0,
               "ESTIMATE shows a total (got %r)" % (d["estimate"] or {}).get("total_s"))
            _txt = w.studio_render_confirm_text()
            ck("24.0" in _txt, "the RENDER confirm quotes the SAME total as estimate() (%r)"
               % _txt.replace("\n", " ")[:70])
            w.studio_render_all()
            _wait(w)
            d = w.studio_dump()
            ck(all(s["chosen"] for s in d["shots"]), "RENDER ALL chooses a take per shot")
            ck(d["step_done"]["5"] is True and d["step_marks"]["5"] == "\u2713", "step 5 ticks")

            # ---- 6 CUT ----
            w.studio_assemble()
            _wait(w)
            d = w.studio_dump()
            ck(d["cut"] and os.path.exists(d["cut"]), "ASSEMBLE writes the final mp4 (%s)" % d["cut"])
            ck(d["frames"] > 0, "the in-window player loaded frames (%d)" % d["frames"])
            ck("caption line" in d["post_text"], "post.txt is shown with a COPY button")
            ck(d["step_done"]["6"] is True and d["step_marks"]["6"] == "\u2713", "step 6 ticks")

            # ---- content fits / the canvas really scrolls ----
            ck(d["canvas_bbox"] is not None, "the step canvas has a real bbox")
            ck(d["steps_reqheight"] > 300,
               "the step column has real content (%d px)" % d["steps_reqheight"])
            ck(str(d["canvas_yscroll"]) != "" or d["canvas_scrollregion"] not in ("", "0 0 1 1"),
               "the canvas scroll wiring is live (yscroll=%r scrollregion=%r)"
               % (d["canvas_yscroll"], d["canvas_scrollregion"]))

            # ---- every button has DARK text (the on-demand _btn_colors trap) ----
            _btns = []

            def _walk(x):
                for c in x.winfo_children():
                    if c.winfo_class() in ("Button", "Menubutton"):
                        _btns.append(c)
                    _walk(c)
            _walk(w)
            _dark = [b for b in _btns if str(b.cget("fg")).lower() == "#14110f"]
            ck(len(_btns) >= 14, "the window has its buttons (%d)" % len(_btns))
            ck(len(_dark) == len(_btns),
               "every button has DARK text (%d of %d)" % (len(_dark), len(_btns)))

            # ---- reopen restores everything from project.json ----
            _before = w.studio_dump()
            w.destroy()
            root.update()
            w2 = open_studio_workspace()
            root.update()
            w2.studio_open(_before["slug"])
            _wait(w2)
            _after = w2.studio_dump()
            ck(_after["slug"] == _before["slug"], "reopen restores the slug")
            ck(_after["chars"] == _before["chars"], "reopen restores the cast")
            ck(_after["script"] == _before["script"], "reopen restores the script edits")
            ck(_after["script_text"] == studio_script_to_text(_after["script"]),
               "reopen re-renders the script text from project.json (%r)"
               % _after["script_text"][:44])
            ck(len(_after["shots"]) == len(_before["shots"]), "reopen restores the shots")
            ck(all(s["chosen"] for s in _after["shots"]), "reopen restores the chosen takes")
            ck(_after["cut"] == _before["cut"], "reopen restores the cut")
            ck(_after["frames"] > 0, "reopen reloads the player frames")
            for _n in ("1", "2", "3", "4", "5", "6"):
                ck(_after["step_done"][_n] is True and _after["step_marks"][_n] == "\u2713",
                   "reopen: step %s is ticked on screen" % _n)

            # ---- no exception reached the error log ----
            _new = ""
            try:
                with open(_errlog) as _fh:
                    _new = _fh.read()[_err0:]
            except Exception:
                pass
            ck("studio" not in _new.lower(),
               "no studio exception reached the error log (%d new bytes)" % len(_new))

            print("TESTSTUDIO %s" % ("FAILED" if fails else "done"), flush=True)
            for _f in fails:
                print("   FAILED: %s" % _f, flush=True)
            try:
                w2.destroy()
            except Exception:
                pass
            root.after(300, root.destroy)
        root.after(2700, _tst)

    # ---- test driver: STUDIO LIVE (real calls — spends GPU) ----
    if os.environ.get("FORGE_LAB_TESTSTUDIO_GO") == "1":
        def _tst_go():
            import shutil as _sh
            import studio_pipeline as _sp

            fails = []

            def ck(cond, label):
                print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
                if not cond:
                    fails.append(label)

            # make sure the TEST data dir has bighead (with the charpack extras)
            _src = os.path.expanduser("~/forge-lab-work/testdata-charpack/characters/bighead")
            _dst = os.path.join(CHARACTERS, "bighead")
            os.makedirs(_dst, exist_ok=True)
            if os.path.isdir(_src):
                for _f in os.listdir(_src):
                    _s = os.path.join(_src, _f)
                    _d = os.path.join(_dst, _f)
                    if os.path.isfile(_s) and not os.path.exists(_d):
                        _sh.copyfile(_s, _d)
                _ss = os.path.join(_src, "sheet")
                _ds = os.path.join(_dst, "sheet")
                if os.path.isdir(_ss) and not os.path.isdir(_ds):
                    _sh.copytree(_ss, _ds)

            kind_var.set("Studio")
            on_kind()
            root.update()
            w = st.get("_studio_win", {}).get("win")
            ck(w is not None and w.winfo_exists(), "the TYPE entry opens the Studio window")

            def _wait(timeout):
                t0 = time.time()
                while w.studio_state["busy"] and time.time() - t0 < timeout:
                    root.update()
                    time.sleep(0.5)
                root.update()

            w.studio_select_char("bighead")
            w.studio_set_idea("he waits for a bus that is too small")
            w.studio_set_length("12")
            w.studio_set_quality("Draft")
            w.studio_use_idea()                    # skip PITCH
            root.update()
            print("  using my idea -> %r" % w.studio_dump()["pitch_title"], flush=True)

            w.studio_write_script()
            _wait(600)
            d = w.studio_dump()
            ck(bool(d["script_text"]), "WRITE SCRIPT (live) produced a script")
            print("  script:\n%s" % d["script_text"][:400], flush=True)

            w.studio_plan_shots()
            _wait(600)
            d = w.studio_dump()
            ck(len(d["shots"]) >= 1, "PLAN SHOTS (live) produced %d shots" % len(d["shots"]))

            w.studio_voice()
            _wait(900)
            d = w.studio_dump()
            ck(bool(d["shots"]) and any(s["status"] for s in d["shots"]), "VOICE LINES (live) ran")

            w.studio_estimate()
            _wait(300)
            est = w.studio_dump()["estimate"] or {}
            total = est.get("total_s") or 0
            print("  estimate total: %s s" % total, flush=True)

            if total <= 600:
                w.studio_render_all()
                _wait(3600)
                print("  rendered all", flush=True)
            else:
                print("  estimate %s s > 600 s — rendering ONLY shot 1" % total, flush=True)
                w.studio_render_shot(1)
                _wait(1200)

            w.studio_assemble()
            _wait(1200)
            d = w.studio_dump()
            final = d["cut"]
            ck(bool(final) and os.path.exists(final), "ASSEMBLE (live) wrote %s" % final)
            if final and os.path.exists(final):
                pr = video_probe(final)
                print("  probe: %s" % (pr,), flush=True)
                ck(bool(pr) and pr.get("width") == 1080 and pr.get("height") == 1920,
                   "final is 1080x1920 (%s)" % (pr,))
                _has_audio = False
                try:
                    _p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                                         "-show_entries", "stream=codec_type", "-of",
                                         "csv=p=0", final], capture_output=True, text=True,
                                        timeout=30)
                    _has_audio = "audio" in (_p.stdout or "")
                except Exception:
                    pass
                ck(_has_audio, "final HAS an audio stream")
                for _frac, _nm in ((0.3, "f_a"), (0.8, "f_b")):
                    _dur = (video_probe(final) or {}).get("seconds") or 3.0
                    _out = os.path.join(_sp.project_dir(d["slug"]), "%s.png" % _nm)
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.2f" % (_dur * _frac),
                                    "-i", final, "-frames:v", "1", _out], check=False)
                    print("  frame: %s" % _out, flush=True)
                print("  post.txt:\n%s" % d["post_text"][:300], flush=True)

            try:
                _sp._unload_director()
                print("  Director unloaded (GPU1 released)", flush=True)
            except Exception as _exc:
                print("  Director unload failed: %r" % _exc, flush=True)

            print("TESTSTUDIO_GO %s" % ("FAILED" if fails else "done"), flush=True)
            for _f in fails:
                print("   FAILED: %s" % _f, flush=True)
            root.after(300, root.destroy)
        root.after(2700, _tst_go)

    # ---- test driver: press POLISH PROMPT for me and report what the UI did ----
    if os.environ.get("FORGE_LAB_TESTLAYOUT") == "1":
        def _dump(tag):
            root.update_idletasks()
            for nm, row in (("KIND row", mode_row), ("MODEL row", model_row)):
                pw = row.winfo_width()
                print("[%s] %s width=%d" % (tag, nm, pw), flush=True)
                for w in row.winfo_children():
                    try:
                        txt = w.cget("text")
                    except Exception:
                        txt = ""
                    print("    %-12s text=%-34r x=%-4d w=%-4d INSIDE=%s"
                          % (w.winfo_class(), txt, w.winfo_x(), w.winfo_width(),
                             w.winfo_x() + w.winfo_width() <= pw + 1), flush=True)
            n = model_menu["menu"].index("end")
            n = 0 if n is None else n + 1
            print("    MODEL menu: %d entries" % n, flush=True)
            for i in range(n):
                print("      - %s" % model_menu["menu"].entrycget(i, "label"), flush=True)
            print("    kind=%r model=%r" % (kind_var.get(), model_var.get()), flush=True)
            # what is actually ON SCREEN — the whole point of the simplification
            vis = []
            for nm, blk in (("duration/size", vid_row), ("size-hint", vid_hint),
                            ("lora", vid_row_lora), ("audio", vid_row_audio),
                            ("cards", vid_row_cards),
                            ("refs", refbox), ("aspect", aspect_lbl),
                            ("resolution", e_res.master), ("quality", qmenu),
                            ("width", e_w.master), ("steps", e_steps.master),
                            ("seed", e_seed.master), ("prefix", e_prefix.master),
                            ("prompt", prompt), ("estimate", est_lbl)):
                vis.append("%s=%s" % (nm, "SHOWN" if blk.winfo_ismapped() else "hidden"))
            print("    blocks: " + "  ".join(vis), flush=True)
            for nm, row in (("vid_row", vid_row), ("vid_row_lora", vid_row_lora),
                            ("vid_row_audio", vid_row_audio),
                            ("vid_row_cards", vid_row_cards), ("ref_list", ref_list)):
                if not row.winfo_ismapped():
                    continue
                rw = row.master.winfo_width()
                print("    %s parent_width=%d" % (nm, rw), flush=True)
                for w2 in row.winfo_children():
                    print("        %-12s x=%-4d w=%-4d INSIDE=%s state=%s"
                          % (w2.winfo_class(), w2.winfo_x(), w2.winfo_width(),
                             w2.winfo_x() + w2.winfo_width() <= rw + 1,
                             (w2.cget("state") if "state" in w2.keys() else "-")), flush=True)

        def _tl():
            """Verify the CONTROLS, not just the callbacks: a control clipped off the right
            edge is invisible to the user and perfectly fine to the code, and a block that
            should be HIDDEN must not be merely greyed out."""
            pick_model("H3 · Character reference (r2v)"); _dump("Video / H3 r2v")
            pick_model("H3 · First + last frame"); _dump("Video / H3 first+last")
            pick_model("H3 · Text only"); _dump("Video / H3 t2v")
            pick_model("WAN 2.2 · Text to video"); _dump("Video / WAN t2v")
            pick_model("WAN 2.2 · First frame (i2v)"); _dump("Video / WAN i2v")
            pick_model("WAN 2.2 · First + last frame"); _dump("Video / WAN flf2v")
            # the image models live under KIND Image — switching KIND is what lists them
            kind_var.set("Image"); on_kind()
            pick_model("Text to image"); _dump("Image / Text to image")
            pick_model("Edit image"); _dump("Image / Edit image")
            pick_model("Remove background"); _dump("Image / Remove background")
            # an Image label under KIND Video must be refused, not silently applied
            kind_var.set("Video"); on_kind()
            _before = model_var.get()
            pick_model("Text to image")
            print("    cross-kind pick refused: %s (model still %r)"
                  % (model_var.get() == _before, model_var.get()), flush=True)
            # KIND round-trip: switching away and back must not reset the model
            kind_var.set("Image"); on_kind(); _dump("KIND -> Image (restored)")
            kind_var.set("Video"); on_kind(); _dump("KIND -> Video (restored)")
            root.destroy()
        root.after(2500, _tl)

    if os.environ.get("FORGE_LAB_TESTCARDS") == "1":
        # Drives the ONE three-way CARD MODE switch. GPU-free and NETWORK-FREE (run with
        # FORGE_LAB_NODIRECTOR=1 so the Studio status thread never touches Ollama). It asserts
        # the DISPLAYED hint equals what params() would SEND — the "control that lies" rule —
        # the row is visible in every mode, the migration, and the Studio status label.
        def _tcm():
            fails = []

            def ck(cond, label):
                print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
                if not cond:
                    fails.append(label)

            print("TESTCARDS start  (NODIRECTOR=%s)"
                  % os.environ.get("FORGE_LAB_NODIRECTOR"), flush=True)

            # 1 . ⚠️ NOT "the default is Solo". The card mode is RESTORED from Ryan's live
            # settings.json, so a returning user who picked Studio comes back on Studio — the
            # driver was asserting a FRESH-INSTALL default and failing for him. It fails on
            # the committed HEAD too, so it was broken before this work. Assert what is true
            # either way, then PIN the state the rest of this driver depends on.
            ck(cards_var.get() in ("Solo", "Turbo", "Studio"),
               "the restored card mode is one of the three (got %r)" % cards_var.get())
            ck(migrate_card_mode(None) == "Solo",
               "and an unknown value still migrates to Solo (the fresh-install default)")
            cards_var.set("Solo")
            apply_cards()
            root.update_idletasks()
            ck(cards_var.get() == "Solo", "pinned to Solo so the rest of this driver is stable")

            # 2 . the row is VISIBLE for an image mode — Studio matters here too
            kind_var.set("Image"); on_kind()
            pick_model("Text to image"); root.update_idletasks()
            ck(vid_row_cards.winfo_ismapped(),
               "CARD MODE row is SHOWN on t2i (Studio matters everywhere)")
            ck(params()["gpus"] == 1, "t2i params gpus=1 (got %s)" % params()["gpus"])

            # 3 . ...and for H3
            kind_var.set("Video"); on_kind()
            pick_model(model_label("video", video_mode="r2v")); root.update_idletasks()
            ck(vid_row_cards.winfo_ismapped(), "CARD MODE row is SHOWN on H3 r2v")

            # 4 . Turbo on H3 -> gpus 2, and the hint AGREES
            cards_var.set("Turbo"); apply_cards(); root.update_idletasks()
            ck(params()["gpus"] == 2, "Turbo on H3 sends gpus=2 (got %s)" % params()["gpus"])
            ck("both 3090s" in cards_note.cget("text"),
               "Turbo/H3 hint says both cards (got %r)" % cards_note.cget("text"))

            # 4b . FREE CARD 2 — the setting, and the hint must not LIE about it
            ck(free_card_var.get() is True, "FREE CARD 2 defaults to ON")
            ck(free_btn.winfo_ismapped(), "the FREE CARD 2 row is on screen")
            ck(free_row.winfo_ismapped(), "and its row is mapped (not starved off-screen)")
            ck(params()["free_card"] is True, "with it ON, params() sends free_card=True")
            ck("unloaded" in cards_note.cget("text"),
               "and the Turbo hint says card 2 is freed (%r)" % cards_note.cget("text"))
            _toggle_free_card()
            root.update_idletasks()
            ck(free_card_var.get() is False, "the toggle turns it OFF")
            ck(params()["free_card"] is False, "and params() sends free_card=False")
            ck("OFF" in cards_note.cget("text"),
               "and the hint SAYS SO \u2014 a control that lies is the failure mode (%r)"
               % cards_note.cget("text"))
            _toggle_free_card()
            root.update_idletasks()
            ck(params()["free_card"] is True, "toggling back restores it")
            ck("unloaded" in cards_note.cget("text"), "and the hint follows it back")

            # 5 . Turbo on a NON-H3 engine -> gpus 1 AND the hint SAYS so (a control that lies)
            kind_var.set("Image"); on_kind()
            pick_model("Text to image"); cards_var.set("Turbo"); apply_cards()
            root.update_idletasks()
            ck(params()["gpus"] == 1,
               "Turbo on t2i sends gpus=1 (got %s)" % params()["gpus"])
            ck("H3 only" in cards_note.cget("text"),
               "t2i Turbo hint names H3-only (got %r)" % cards_note.cget("text"))

            # 6 . Studio -> gpus 1, and the status label reports off under NODIRECTOR (no net)
            cards_var.set("Studio"); apply_cards(); root.update_idletasks()
            ck(params()["gpus"] == 1, "Studio sends gpus=1 (got %s)" % params()["gpus"])
            if os.environ.get("FORGE_LAB_NODIRECTOR") == "1":
                root.update_idletasks()
                ck("off" in cards_status.cget("text"),
                   "Studio status reports 'off' with NODIRECTOR (got %r)"
                   % cards_status.cget("text"))

            # 7 . settings save + the OLD 1/2 migration
            _cs = current_settings()
            ck(_cs.get("card_mode") == "Studio",
               "current_settings saves card_mode (got %r)" % _cs.get("card_mode"))
            ck(migrate_card_mode("1") == "Solo", "migrate '1' -> Solo")
            ck(migrate_card_mode("2") == "Turbo", "migrate '2' -> Turbo")
            ck(migrate_card_mode("Turbo") == "Turbo", "migrate 'Turbo' -> Turbo")
            ck(migrate_card_mode("Studio") == "Studio", "migrate 'Studio' -> Studio")
            ck(migrate_card_mode(None) == "Solo", "migrate None -> Solo")
            ck(migrate_card_mode("wat") == "Solo", "migrate unknown -> Solo")

            print("TESTCARDS %s (%d failures)"
                  % ("FAILED" if fails else "done", len(fails)), flush=True)
            root.destroy()
            if fails:
                os._exit(1)
        root.after(2500, _tcm)

    if os.environ.get("FORGE_LAB_TESTE2E") == "1":
        # END-TO-END: makes the EXACT call the SUBMIT button makes — submit(st["mode"], params(),
        # st.get("refs")) — with the two known clips set as if they had been picked in the UI.
        # TESTV2V proves the STATE (params, caps, estimates, visibility); this proves the RUN:
        # params -> submit() -> upload -> graph -> render -> take tracking.
        def _e2e():
            print("TESTE2E start", flush=True)
            kind_var.set("Video"); on_kind()
            _cases = (("animate2", os.environ.get("E2E_STILL"), os.environ.get("E2E_DRIVE")),
                      ("ltx",      None,                     os.environ.get("E2E_LTXCLIP")))
            for _mode, _still, _clip in _cases:
                if not _clip or not os.path.exists(_clip):
                    print("  %s SKIPPED (no clip at %r)" % (_mode, _clip), flush=True)
                    continue
                pick_model(model_label(_mode)); root.update_idletasks()
                st["refs"] = [_still] if _still else []
                st["source_path"] = _clip
                st.pop("src_frames", None)
                _pb = video_probe(_clip)
                if _pb:
                    st["src_frames"] = int(_pb["frames"])
                _render_refs(); refresh_estimate(); root.update_idletasks()
                _p = params()
                print("  %s params: w=%s h=%s len=%s steps=%s src_frames=%s refs=%r"
                      % (_mode, _p["width"], _p["height"], _p.get("length"), _p["steps"],
                         _p.get("src_frames"), [os.path.basename(x) for x in (_p.get("ref_paths") or [])]),
                      flush=True)
                print("  %s est=%r" % (_mode, est_lbl.cget("text").split("\n")[0]), flush=True)
                try:
                    _pid, _seed = submit(st["mode"], _p, st.get("refs"))
                    print("  %s SUBMITTED prompt_id=%s seed=%s" % (_mode, _pid, _seed), flush=True)
                except Exception as _exc:
                    print("  %s SUBMIT FAILED: %r" % (_mode, _exc), flush=True)
            root.destroy()
        root.after(2500, _e2e)

    if os.environ.get("FORGE_LAB_TESTV2V") == "1":
        # Drives the REAL UI through BOTH v2v engines and prints what is ON SCREEN: the SOURCE
        # VIDEO row's visibility per mode, the reference CAP (the ordering bug's symptom — 9
        # offered to an engine that takes 1), each engine's OWN SIZE list (LTX_SIZES and
        # ANIMATE2_SIZES were unreachable while the menu was built once from VIDEO_SIZES), and the
        # estimate. The graph rules themselves are checked GPU-free by tools/v2v_matrix.py.
        def _tv():
            print("TESTV2V start", flush=True)
            kind_var.set("Video"); on_kind()
            # model_label() is the ORIGINAL source of these strings — hardcoding the MODES
            # label here made pick_model a silent no-op that still printed a plausible state.
            for mode in ("ltx", "animate2"):
                lbl = model_label(mode)
                pick_model(lbl); root.update_idletasks()
                print("  %-9s model=%-36r cap=%-3r src_shown=%-5s"
                      % (mode, model_var.get(), _ref_cap(), src_row.winfo_ismapped()), flush=True)
                print("            size=%-34r choices=%s"
                      % (size_var.get(), list(_size_choices(kind_of(mode)).keys())), flush=True)
                print("            note=%r" % src_note.cget("text")[:64], flush=True)
                print("            refs_shown=%-5s help=%r"
                      % (refbox.winfo_ismapped(), ref_help.cget("text")[:94]), flush=True)
                print("            est=%r" % est_lbl.cget("text").split("\n")[0], flush=True)
                _pp = params()
                print("            ▶ e_w/e_h=%r/%r  params w/h=%r/%r len=%r steps=%r  "
                      "est_fn=%r  apply_vsize called=%s" % (
                    e_w.get(), e_h.get(), _pp.get("width"), _pp.get("height"),
                    _pp.get("length"), _pp.get("steps"),
                    round(estimate_seconds(st["mode"], _pp, None, None), 1),
                    "yes"), flush=True)
            # the row must be HIDDEN where there is no clip to read — hidden, not greyed
            for lbl in ("H3 · Text only", "WAN 2.2 · Text to video"):
                pick_model(lbl); root.update_idletasks()
                print("  no-clip mode %-32r src_shown=%s"
                      % (model_var.get(), src_row.winfo_ismapped()), flush=True)
            kind_var.set("Image"); on_kind()
            pick_model("Text to image"); root.update_idletasks()
            print("  image mode            src_shown=%s" % src_row.winfo_ismapped(), flush=True)
            root.destroy()
        root.after(2500, _tv)

    if os.environ.get("FORGE_LAB_TESTWAN") == "1":
        # Drives the REAL UI through the WAN KIND and asserts what is ON SCREEN — the three
        # variants, the baked path (steps / shift / cfg / LoRA), the disabled-by-design step
        # and LoRA controls, the per-variant reference caps, and the estimate. The graph
        # rules themselves are checked GPU-free by tools/wan_matrix.py.
        def _tw():
            print("TESTWAN start", flush=True)
            # WAN is now a MODEL inside KIND Video, not its own KIND
            kind_var.set("Video"); on_kind()
            pick_model(model_label("wan", wan_mode=WAN_VARIANT_DEFAULT))
            root.update_idletasks()
            print("  kind=%r model=%r quality=%r" % (kind_var.get(), model_var.get(),
                                                     qvar.get()), flush=True)
            print("  steps=%r (entry state=%s)  lora_row_shown=%s  audio_row_shown=%s  "
                  "lora_label=%r"
                  % (e_steps.get(), e_steps.cget("state"), vid_row_lora.winfo_ismapped(),
                     vid_row_audio.winfo_ismapped(), lora_lbl.cget("text")), flush=True)
            print("  dim_lbl=%r" % dim_lbl.cget("text").replace("\n", " | "), flush=True)
            print("  est=%r" % est_lbl.cget("text").replace("\n", " | "), flush=True)
            for k in WAN_MODES:
                pick_model(model_label("wan", wan_mode=k))
                root.update_idletasks()
                print("  WAN %-7s model=%-28r cap=%-12r add=%-8s eval=%d refs_shown=%s"
                      % (k, model_var.get(), ref_count.cget("text"),
                         add_btn.cget("state"), _ref_cap(), refbox.winfo_ismapped()), flush=True)
                print("            help=%r" % ref_help.cget("text")[:110], flush=True)
            pick_model(model_label("wan", wan_mode="t2v"))
            for qk in WAN_QUALITY:
                qvar.set(qk)
                apply_quality(qk)
                root.update_idletasks()
                r = wan_rules("t2v", qk)
                print("  path %-14r ui_steps=%s shift=%.1f cfg=%.1f lora=%s entry=%s"
                      % (qk, e_steps.get(), r["shift"], r["cfg"], bool(r["lora"]),
                         e_steps.cget("state")), flush=True)
                print("       est=%r" % est_lbl.cget("text").split("\n")[0], flush=True)
            # the mis-set this is built to prevent: a step count between the two paths
            print("  stray STEPS entry accepted? %s"
                  % ("no — the field is a label" if e_steps.cget("state") == "disabled"
                     else "YES — BUG"), flush=True)
            # WAN must not SHOW a control it cannot honour (it used to show them disabled)
            print("  WAN hides what it cannot do: lora_shown=%s audio_shown=%s"
                  % (vid_row_lora.winfo_ismapped(), vid_row_audio.winfo_ismapped()), flush=True)
            # and on H3 both controls must be live and the toggle must show in the dims line
            pick_model(model_label("video", video_mode="i2v"))
            root.update_idletasks()
            print("  H3 audio on:  var=%r audio_shown=%s lora_shown=%s dims=%r"
                  % (audio_var.get(), vid_row_audio.winfo_ismapped(),
                     vid_row_lora.winfo_ismapped(),
                     dim_lbl.cget("text").split("audio:")[-1].strip()), flush=True)
            audio_var.set("off"); apply_audio()
            root.update_idletasks()
            print("  H3 audio off: var=%r note=%r dims=%r"
                  % (audio_var.get(), audio_note.cget("text"),
                     dim_lbl.cget("text").split("audio:")[-1].strip()), flush=True)
            print("  settings save/restore carries audio: %s"
                  % ("audio" in current_settings()), flush=True)
            # ---- S2V: it must show a FILE row and a LENGTH row, must NOT show H3's on/off
            # audio row, must pin its own 640x640 canvas, and must offer exactly ONE path.
            pick_model(model_label("wan", wan_mode="s2v"))
            root.update_idletasks()
            print("  S2V rows: file=%s length=%s (h3_audio=%s lora=%s)"
                  % (s2v_row.winfo_ismapped(), s2v_len_row.winfo_ismapped(),
                     vid_row_audio.winfo_ismapped(), vid_row_lora.winfo_ismapped()), flush=True)
            print("  S2V size=%r -> canvas %sx%s" % (size_var.get(), e_w.get(), e_h.get()),
                  flush=True)
            _cnt = qmenu["menu"].index("end")
            _cnt = 0 if _cnt is None else _cnt + 1
            print("  S2V quality menu: %d entry(ies) %s"
                  % (_cnt, [qmenu["menu"].entrycget(i, "label") for i in range(_cnt)]),
                  flush=True)
            print("  S2V steps=%r state=%s   ref help=%r"
                  % (e_steps.get(), e_steps.cget("state"), ref_help.cget("text")[:80]),
                  flush=True)
            for _n in sorted(S2V_LENGTHS):
                s2v_len_var.set(S2V_LENGTHS[_n])
                apply_s2v()
                root.update_idletasks()
                print("     %d chunk(s) -> %r | est=%r"
                      % (_n, s2v_note.cget("text"), est_lbl.cget("text").split("\n")[0]),
                      flush=True)
            print("  S2V audio row says: %r" % s2v_name.cget("text"), flush=True)
            print("  S2V dims=%r" % dim_lbl.cget("text").replace("\n", " | "), flush=True)
            print("  S2V settings carry audio_path+s2v_len: %s"
                  % (("audio_path" in current_settings()) and ("s2v_len" in current_settings())),
                  flush=True)
            print("TESTWAN done", flush=True)
            root.after(300, root.destroy)
        root.after(2500, _tw)

    if os.environ.get("FORGE_LAB_TESTLONG") == "1":
        # Drives the LONG VIDEO workspace headlessly: does it build, does the queue fill,
        # does the per-scene PROMPT EDITOR round-trip, does the "unkept only" filter work,
        # and does STITCH refuse an incomplete keeper set (the guard, not the video).
        def _tl():
            print("TESTLONG start", flush=True)
            fails = []

            def ck(cond, label):
                print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
                if not cond:
                    fails.append(label)

            # ⚠️ Snapshot episodes/untitled/plan.json BEFORE the window exists. The constructor
            # called refresh(), and refresh() saved — so merely OPENING the workspace wrote a
            # blank 12-scene plan over whatever lived under the default episode name.
            _u = long_plan_path("untitled")
            _u_was = (os.path.exists(_u), os.path.getsize(_u) if os.path.exists(_u) else 0,
                      os.path.getmtime(_u) if os.path.exists(_u) else 0)

            w = open_long_video_workspace()
            # ⚠️ A SCRATCH name, set on STATE as well as the plan: saves go to
            # long_plan_path(st["name"]) and only happen once st["loaded"] is True. The old
            # comment claimed this was handled, but it renamed only plan["name"] — so every run
            # still wrote episodes/untitled/plan.json.
            w.long_plan.clear()
            w.long_plan.update({"name": "test-scratch", "audio": None, "ref": None,
                                "treatment": {"w": 640, "h": 640, "fps": 16, "chunks": 3,
                                              "steps": 4, "cfg": 1.0, "shift": 8.0,
                                              "h3_steps": 12, "engine": "h3"},
                                "scenes": [long_scene_new(i + 1) for i in range(12)]})
            w.long_state["name"] = "test-scratch"
            w.long_state["loaded"] = True
            root.update_idletasks()
            w.update_idletasks()
            d = w.long_dump()
            print("  treatment=%r" % d["treatment"], flush=True)
            ck(d["scenes"] == 12 and d["queue_rows"] == 12,
               "12 scenes and 12 queue rows (got %d/%d)" % (d["scenes"], d["queue_rows"]))
            ck("H3" in d["treatment"] and "character_lora_v2" in d["treatment"]
               and "AUDIO GUIDE" in d["treatment"],
               "treatment names the H3 engine, its LoRA and the audio guide (%r)"
               % d["treatment"][:76])
            w.long_set_engine("wan")
            root.update_idletasks()
            ck("S2V" in w.long_dump()["treatment"],
               "switching the ENGINE shows the S2V treatment (%r)"
               % w.long_dump()["treatment"][:60])
            w.long_set_engine("h3")
            root.update_idletasks()
            # per-piece CANVAS: it must reach the treatment line and the draft payload
            w.long_set_canvas("Landscape 832×480  (0.40 MP · same pixels)")
            root.update_idletasks()
            ck("832×480" in w.long_dump()["treatment"],
               "changing CANVAS updates the treatment (%r)" % w.long_dump()["treatment"][:34])
            ck(long_canvas(w.long_plan) == (832, 480),
               "the payload canvas follows the plan (%s)" % (long_canvas(w.long_plan),))
            w.long_set_canvas("Portrait 544×736  (0.40 MP · measured)")
            root.update_idletasks()
            ck(long_canvas(w.long_plan) == (544, 736), "and back to the measured default")
            ck(d["steps"].get("Scenes") == "0 of 12 kept · 0 of 12 have stills" and
               d["steps"].get("Stitch") == "needs 12 keepers",
               "step bar shows the live keeper AND still counts (%r / %r)"
               % (d["steps"].get("Scenes"), d["steps"].get("Stitch")))
            # ⚠️ The cost line must EQUAL the honest per-scene sum, and name what sets the price.
            # It used to be priced with S2V's constants whatever engine ran, and it counted KEPT
            # scenes — so the number Ryan planned his evening around was half the truth.
            _warm = None
            _sum = 0.0
            _nrun = 0
            for _i2, _s2 in enumerate(w.long_plan["scenes"]):
                if _s2.get("keep") is not None:
                    continue
                _nrun += 1
                _sec, _m, _why = w.long_scene_cost(_i2, _s2, warm_engine=_warm)
                if _sec is None:
                    continue
                _sum += _sec
                _warm = engine_of(_m)
            ck("%d to run" % _nrun in d["cost"],
               "the cost line counts ONLY the scenes that will run (%r)" % d["cost"][:76])
            ck(("H3" in d["cost"]) or ("WAN" in d["cost"]),
               "and names the engine that actually sets the price (%r)" % d["cost"][:76])
            ck(w.long_fmt(_sum / 60) in d["cost"],
               "and its total IS the app's own per-scene estimate summed (%s)"
               % w.long_fmt(_sum / 60))
            # ⚠️ THE DIALOG AND THE FOOTER MUST AGREE — they share `_cost_facts()` precisely so that
            # what you APPROVE in the pre-flight is what you were SHOWN in the footer.
            _cf = w.long_cost_facts()
            ck(w.long_fmt(_cf["total"] / 60) in d["cost"] and _cf["blocked"] == 0,
               "the pre-flight dialog and the footer quote ONE computation (%s, %d blocked)"
               % (w.long_fmt(_cf["total"] / 60), _cf["blocked"]))

            # ── the PRE-FLIGHT DIALOG — the commit point, so it must actually OPEN ─────────────
            # ⚠️ A dialog that RAISES on open is invisible: Tk swallows the callback exception (see
            # _log_exc) and the button simply does nothing. `_cost_facts()` being correct does not
            # prove the dialog renders, so open it and read what it really says.
            w.long_preflight()
            root.update_idletasks()
            _tls = [c for c in w.winfo_children() if c.winfo_class() == "Toplevel"]
            ck(len(_tls) == 1, "DRAFT ALL opens a pre-flight dialog (%d Toplevel(s))" % len(_tls))
            if _tls:
                _dlg = _tls[0]
                _txt, _btns = [], []

                def _walk(wd):
                    for _c in wd.winfo_children():
                        try:
                            _txt.append(str(_c.cget("text")))
                        except Exception:
                            pass
                        if _c.winfo_class() == "Button":
                            _btns.append(_c)
                        _walk(_c)

                _walk(_dlg)
                _all = " ".join(_txt)
                ck("DRAFT ALL" in _all, "…and it is a DRAFT ALL dialog (%r)" % _all[:56])
                ck(("H3" in _all) or ("WAN" in _all),
                   "…naming the ENGINE it is about to spend on (%r)" % _all[:56])
                ck("forge time" in _all, "…quoting the estimate it will take")
                ck("LOCAL forge only" in _all,
                   "…and saying nothing is billed — local forge, the 3090 is the cost")
                ck(w.long_fmt(w.long_cost_facts()["total"] / 60) in _all,
                   "…with the SAME total the footer shows, not a second opinion")
                ck(len(_btns) >= 2,
                   "…and a way to go plus a way to back out (%d buttons)" % len(_btns))
                _dlg.destroy()
                root.update_idletasks()

            # ⚠️ geometry() is a REQUEST. Tk grows a Toplevel to its CONTENT's requested size, so a
            # bench that wants more than the geometry asks for makes the window bigger than the
            # screen — with no error. Measure realized vs requested, not what was asked for.
            print("  window realized=%dx%d  requested=%dx%d  screen=%dx%d"
                  % (w.winfo_width(), w.winfo_height(), w.winfo_reqwidth(),
                     w.winfo_reqheight(), w.winfo_screenwidth(), w.winfo_screenheight()),
                  flush=True)

            # ⚠️ NOTHING asserted that the treatment controls actually EXIST on screen. The previous
            # version of this check only compared right edges — and an UNMAPPED widget reports
            # x=0, width=1, so it passed cheerfully while REF…, AUDIO…, STYLE… and ALL FOUR
            # TREATMENT MENUS were invisible: the summary alone requested 960 px of a 1126 px row,
            # so Tk never allocated space to anything after it. MAPPED is the assertion that counts.
            _bad = []
            for _rname, _row in (("treatment", w.long_tline), ("controls", w.long_tctl)):
                _rw = _row.winfo_width()
                for _w in _row.winfo_children():
                    _rx = _w.winfo_rootx() - _row.winfo_rootx()
                    _txt = (str(_w.cget("text"))[:13] if _w.winfo_class() in ("Label", "Button")
                            else "<%s>" % _w.winfo_class())
                    if not _w.winfo_ismapped() or _rx + _w.winfo_width() > _rw + 1:
                        _bad.append((_rname, _txt, _rx, _w.winfo_width(),
                                     int(_w.winfo_ismapped())))
            ck(not _bad,
               "every treatment control is MAPPED and inside its row (rows %d/%dpx, bad=%r)\n"
               "        controls: %r"
               % (w.long_tline.winfo_width(), w.long_tctl.winfo_width(), _bad,
                  [(("" if _w.winfo_class() not in ("Label", "Button")
                     else str(_w.cget("text"))[:13]) or "<%s>" % _w.winfo_class(),
                    _w.winfo_rootx() - w.long_tctl.winfo_rootx(), _w.winfo_width(),
                    _w.winfo_reqwidth(), int(_w.winfo_ismapped()))
                   for _w in w.long_tctl.winfo_children()]))

            # ⚠️ And the CONTENT must fit the WINDOW. Tk will not grow a Toplevel past its
            # geometry() request — it starves the tail of the packed content instead, invisibly.
            # The bench alone asked for 930 px inside a 900 px window when Ryan reported that he
            # "cant see everything". This assertion is what makes that class of loss impossible.
            ck(w.winfo_reqheight() <= w.winfo_height() and
               w.winfo_reqwidth() <= w.winfo_width(),
               "the content FITS the window (%dx%d requested vs %dx%d realized)"
               % (w.winfo_reqwidth(), w.winfo_reqheight(),
                  w.winfo_width(), w.winfo_height()))

            # ⚠️ And the BENCH must be on a real scrollregion. That is what makes the previous
            # assertion safe to satisfy: the bench no longer has to fit, so it can never be
            # starved — the failure mode behind every "I can't see X" report on this window.
            _sr = str(w.long_rv_canvas.cget("scrollregion")).strip()
            ck(_sr not in ("", "0 0 1 1"),
               "the bench canvas has a real scrollregion (%r) — so shrinking the window can no "
               "longer starve it" % (_sr,))

            # ── the BOARD (§3b) — a VIEW of the same scenes, not a second model ─────
            legacy = {"name": "x", "scenes": [{"n": 1, "prompt": "p", "chunks": 2}]}
            long_plan_migrate(legacy)
            ck("still" in legacy["scenes"][0] and "still_prompt" in legacy["scenes"][0]
               and legacy["scenes"][0]["keep"] is None
               and legacy["scenes"][0]["kf_cont"] is False
               and legacy["scenes"][0]["kf_first"] is False
               and legacy.get("subject_block") == DEFAULT_STYLE_BLOCK,
               "a plan.json written BEFORE the board/KF roles/style block gains every new field "
               "on load — including a FILLED style block, not an empty one (%r)"
               % (sorted(legacy["scenes"][0]),))
            # ⚠️ kf_first is False, not True, and that is the point: this legacy scene has no still,
            # so its `first` can only be the OLD DEFAULT, never a request. Leaving it ticked would
            # now REFUSE the scene ("no opening still has been drafted") — turning a piece that
            # used to render plainly into a wall of blocks.

            # ⚠️ The empty-string case is the one that matters: `setdefault` would leave it "" and
            # an empty block IS the original bug. And a block Ryan wrote must survive untouched.
            _blank = {"name": "y", "subject_block": "   ", "scenes": []}
            long_plan_migrate(_blank)
            ck(_blank["subject_block"] == DEFAULT_STYLE_BLOCK,
               "a piece whose block is present-but-BLANK gets the locked look — the case "
               "setdefault cannot fix")
            _own = {"name": "z", "subject_block": "my own look", "scenes": []}
            long_plan_migrate(_own)
            ck(_own["subject_block"] == "my own look",
               "and a block Ryan has actually written is left ALONE")

            w.long_board(True)
            root.update_idletasks()
            b = w.long_dump()["board"]
            ck(b["on"] and b["board_shown"] and not b["tree_shown"] and b["cards"] == 12,
               "BOARD draws 12 cards and hides the table (%r)" % (b,))

            # ── the card CHIP must be the answer the RUN will get ─────────────────────
            w.long_state["filter"] = False
            for _j in (0, 1):
                w.long_plan["scenes"][_j]["keep"] = None
            _c0, _c1 = w.long_plan["scenes"][0], w.long_plan["scenes"][1]
            _c0.update({"kf_first": True, "kf_last": False, "kf_cont": False, "kf_ref": False})
            _c0["still"] = "/tmp/fake-still.png"
            _c1.update({"kf_first": False, "kf_last": True, "kf_cont": False, "kf_ref": False})
            _c1["still"], _c1["still_last"] = None, None
            w.long_board(True)
            root.update_idletasks()
            _modes = w.long_dump()["board_modes"]
            ck(_modes[:2] == ["i2v", "BLOCKED"],
               "a card says WHAT WILL RUN — i2v where the role is satisfiable, BLOCKED where it "
               "cannot be met (%r)" % (_modes[:2],))
            ck(len(_modes) == len(w.long_plan["scenes"]),
               "and every scene gets a chip (%d chips for %d scenes)"
               % (len(_modes), len(w.long_plan["scenes"])))
            # ⚠️ Put scene 0 back the way the fixture had it. A later assertion checks the DEFAULT
            # roles, and a test that inherits state from the test before it is a test that lies.
            _c0.update({"kf_first": False, "kf_last": False, "kf_ref": False, "kf_cont": False})
            _c0["still"] = None
            w.long_board(True)
            root.update_idletasks()
            ck(b["left"] > 600 and b["order_ok"],
               "the board widens leftp and stays ABOVE the +ADD/−DEL/▲▼ row (%r)" % (b,))
            ck(b["geom"]["cols"] == 4 and b["geom"]["rows"] == 3 and b["geom"]["off"] == 0,
               "the cards lay out 4 across x 3 rows, none pushed outside the panel (%r)"
               % (b["geom"],))
            ck(b["view_btn"].startswith("◂"),
               "the toggle reads TABLE while the board is up (%r)" % b["view_btn"])
            db = w.long_dump()
            ck("0 of 12 have keyframes" in db["draft_btn"] and db["draft_state"] == "normal",
               "DRAFT ALL counts keyframes and is NEVER disabled (%r, state=%r)"
               % (db["draft_btn"], db["draft_state"]))

            # the IMAGE path, not just the placeholder: a PhotoImage with no live reference is
            # garbage-collected and the card silently renders EMPTY — the bug this guards.
            _ph = tk.PhotoImage(width=64, height=64)
            _ph.put("#c25e33", to=(0, 0, 64, 64))
            _still = os.path.join(DATA, "boardtest-still.png")
            _ph.write(_still, format="png")
            w.long_plan["scenes"][0]["still"] = _still
            w.long_board(True)
            root.update_idletasks()
            b = w.long_dump()
            ck(b["stills"] == 1 and b["board"]["thumbs"] == 1,
               "a card with a still HOLDS its PhotoImage (no live ref = an empty box) "
               "(%r thumbs, %r stills)" % (b["board"]["thumbs"], b["stills"]))
            w.long_plan["scenes"][0]["still"] = None

            w.long_board(False)
            root.update_idletasks()
            b = w.long_dump()["board"]
            ck((not b["on"]) and b["tree_shown"] and not b["board_shown"]
               and b["left"] < 500 and b["order_ok"],
               "back to TABLE: tree up, board down, leftp back to 430 (%r)" % (b,))

            # ── KF roles (D6.2) — FLAGS, so first + last can both be on ────────────
            def _setkf(**wf):
                """⚠️ Set roles by DIFFERENCE, not by toggling a fixed number of times. This block
                used to toggle a hard-coded sequence, so it only held while the DEFAULT was
                `first` — changing that default flipped every toggle and broke three assertions
                that had nothing to do with defaults. A test that inherits is a test that lies."""
                for _n, _v in wf.items():
                    if bool(w.long_plan["scenes"][w.long_state["sel"]].get(_n)) != bool(_v):
                        w.long_kf(_n)
                root.update_idletasks()

            w.long_pick(0)
            root.update_idletasks()
            d0 = w.long_dump()
            ck(d0["kf_flags"] == {"kf_first": False, "kf_last": False, "kf_ref": False,
                                  "kf_cont": False} and d0["kf"] == "NONE",
               "a new scene defaults to NO roles, so the pre-board plain-video path stays "
               "reachable (%r / %r)" % (d0["kf_flags"], d0["kf"]))
            ck(d0["kf_cont_state"] == "disabled",
               "continue-from-prev is DISABLED on scene 1 (%r)" % d0["kf_cont_state"])

            _setkf(kf_first=True, kf_last=True)   # two flags ON — a pick-one could not do this
            d0 = w.long_dump()
            ck(d0["kf_flags"]["kf_last"] and d0["kf_flags"]["kf_first"]
               and d0["kf"] == "first+last",
               "first + last can BOTH be on (%r)" % d0["kf"])
            ck("both ends pinned" in d0["kf_note"],
               "the note explains pinning both ends (%r)" % d0["kf_note"][:44])

            _setkf(kf_first=False, kf_last=False)
            ck(w.long_dump()["kf"] == "NONE",
               "all roles off reads NONE, not an empty string (%r)" % w.long_dump()["kf"])

            # ⚠️ A TICKED ROLE WITH NO STILL IS REFUSED — the bug this pass found. `first frame`
            # ticked with nothing drafted used to fall through to the no-role branch and come back
            # as a piece-reference `r2v` that SUBMITTED FINE. Nothing anywhere said the role had
            # been dropped; the shot was simply different from the one that was asked for.
            _sc0 = w.long_plan["scenes"][0]
            _setkf(kf_first=True)
            _sc0["still"] = None
            _m, _r, _why = w.long_video_plan(0, _sc0)
            ck(_m is None and "no opening still" in (_why or ""),
               "first-frame ticked with NO still is REFUSED, not silently rerouted to r2v "
               "(%r / %r)" % (_m, _why))
            ck(w.long_dump()["kf"] == "first",
               "and the role line still says what was ticked (%r)" % w.long_dump()["kf"])
            # ...while NO role at all keeps the behaviour this window had before the board
            _setkf(kf_first=False, kf_ref=False)
            _m2, _r2, _why2 = w.long_video_plan(0, _sc0)
            ck(_why2 is None and _m2 in ("r2v", "t2v"),
               "no role at all still renders the pre-board way (%r, %d refs)"
               % (_m2, len(_r2 or [])))

            w.long_pick(4)
            w.long_kf("kf_cont")
            root.update_idletasks()
            d4 = w.long_dump()
            ck(d4["kf_flags"]["kf_cont"] and not d4["kf_flags"]["kf_first"],
               "CONT forces FIRST off — both claim the first frame (%r)" % (d4["kf_flags"],))
            ck(d4["kf"] == "← prev" and "UNUSED" in d4["kf_note"],
               "a cont scene says its own still is UNUSED (%r / %r)"
               % (d4["kf"], d4["kf_note"][:44]))
            ck(d4["kf_cont_state"] == "normal",
               "continue-from-prev is ENABLED on scene 5 (%r)" % d4["kf_cont_state"])
            w.long_pick(0)

            # ── DRAFT KEYFRAMES — pure maths, so it is testable with NO spend ────────
            # ⚠️ Reset EVERY role, not just cont. The block above left scene 1 with all roles OFF
            # and scene 5 as cont, so this block was inheriting a baseline of 10 and not 12. A test
            # that inherits state from the test before it is a test that lies — second time here.
            for _s in w.long_plan["scenes"]:
                _s.update({"kf_first": True, "kf_last": False, "kf_ref": False,
                           "kf_cont": False})
                _s["still"] = None
                _s["still_last"] = None
            w.long_refresh()
            root.update_idletasks()
            inf = w.long_kf_info()
            ck(inf["todo"] == 12 and inf["mode"] == "t2i" and inf["w"] == 544
               and inf["h"] == 736 and inf["per"] > 0,
               "keyframes: 12 to draft, t2i, at the TREATMENT canvas (%r)" % (inf,))
            ck(abs(inf["total"] - inf["per"] * inf["todo"]) < 1e-6,
               "the total is per-still × count, quoted BEFORE spending (%r)" % (inf["total"],))
            w.long_set_ref("fake-ref.png")     # the mode choice never opens the file
            root.update_idletasks()
            ck(w.long_kf_info()["mode"] == "edit",
               "with a reference still, keyframes switch to EDIT (%r)"
               % w.long_kf_info()["mode"])
            w.long_set_ref(None)
            w.long_pick(5)
            w.long_kf("kf_cont")
            root.update_idletasks()
            ck(w.long_kf_info()["todo"] == 11,
               "a cont scene is not something to keyframe (%r)" % w.long_kf_info()["todo"])
            ck(w.long_dump()["kf_btn"] == "DRAFT KEYFRAMES · 0/11",
               "the counter's DENOMINATOR drops cont scenes (%r)" % w.long_dump()["kf_btn"])

            # ── the SECOND SLOT: `last` is a separate submission ─────────────────
            for _s in w.long_plan["scenes"]:
                _s.update({"kf_first": True, "kf_last": False, "kf_ref": False,
                           "kf_cont": False})
            w.long_pick(2)
            w.long_kf("kf_last")              # scene 3 becomes first + last
            root.update_idletasks()
            ck(w.long_kf_slots(w.long_plan["scenes"][2]) == ["still", "still_last"],
               "first + last needs TWO slots (%r)"
               % (w.long_kf_slots(w.long_plan["scenes"][2]),))
            ck(w.long_dump()["kf_need"] == 13 and w.long_dump()["kf_todo"] == 13,
               "the denominator grows to 13 because one scene wants a landing still (%r/%r)"
               % (w.long_dump()["kf_btn"], w.long_dump()["kf_need"]))
            ck(w.long_dump()["kf_btn"] == "DRAFT KEYFRAMES · 0/13",
               "the counter reports 13 slots (%r)" % w.long_dump()["kf_btn"])

            # the case the OLD scene-only todo got wrong: cont + last
            w.long_kf("kf_first")             # first off, last still on
            w.long_kf("kf_cont")              # now cont + last
            root.update_idletasks()
            ck(w.long_kf_slots(w.long_plan["scenes"][2]) == ["still_last"],
               "cont + last wants ONLY the landing still (%r)"
               % (w.long_kf_slots(w.long_plan["scenes"][2]),))
            ck(w.long_dump()["kf_need"] == 12,
               "and the denominator comes back to 12 (%r)" % w.long_dump()["kf_need"])
            for _s in w.long_plan["scenes"]:
                _s.update({"kf_first": True, "kf_last": False, "kf_ref": False,
                           "kf_cont": False})
            w.long_pick(0)
            w.long_refresh()

            # ── per-slot STILL prompts ─────────────────────────────────────────────
            for _s in w.long_plan["scenes"]:
                _s.update({"kf_first": True, "kf_last": False, "kf_ref": False,
                           "kf_cont": False})
                _s["still_prompt"] = None
                _s["still_last_prompt"] = None
            w.long_pick(0)
            w.long_refresh()
            root.update_idletasks()
            _boxes = [x for x in w.long_spwrap.winfo_children()
                      if x.winfo_class() == "Text"]
            ck(len(_boxes) == 1,
               "a first-only scene shows ONE still-prompt box (%d)" % len(_boxes))
            w.long_kf("kf_last")
            root.update_idletasks()
            _boxes = [x for x in w.long_spwrap.winfo_children()
                      if x.winfo_class() == "Text"]
            ck(len(_boxes) == 2,
               "first+last shows TWO — each end gets its own prompt (%d)" % len(_boxes))
            _lb = _boxes[1]                      # slots order: still, then still_last
            ck("<KeyRelease>" in _lb.bind(),
               "the still-prompt box is BOUND to KeyRelease (%r)" % (_lb.bind(),))
            _lb.insert("1.0", "settle on the deadpan reaction, wide eyes")
            _lb._kf_update()
            root.update_idletasks()
            ck(w.long_plan["scenes"][0]["still_last_prompt"]
               == "settle on the deadpan reaction, wide eyes",
               "typing in the landing box writes THROUGH to the scene (%r)"
               % w.long_plan["scenes"][0]["still_last_prompt"])
            ck("settle on the deadpan" in w.long_kf_prompt_slot("still_last"),
               "the landing prompt is what the landing still would use (%r)"
               % w.long_kf_prompt_slot("still_last")[:46])
            ck("settle on the deadpan" not in w.long_kf_prompt_slot("still"),
               "and it does NOT leak into the opening still's prompt")
            for _s in w.long_plan["scenes"]:
                _s.update({"kf_first": True, "kf_last": False, "kf_ref": False,
                           "kf_cont": False})
                _s["still_prompt"] = None
                _s["still_last_prompt"] = None
            w.long_pick(0)
            w.long_refresh()

            # ── `continue from prev` as a RUN ──────────────────────────────────────
            for _s in w.long_plan["scenes"]:
                _s.update({"kf_first": True, "kf_last": False, "kf_ref": False,
                           "kf_cont": False})
            w.long_cont_run_apply(1, 5, True)     # a range starting at 1 — scene 1 must be skipped
            root.update_idletasks()
            _flags = [(x["n"], x["kf_cont"], x["kf_first"]) for x in w.long_plan["scenes"][:5]]
            ck(_flags == [(1, False, True), (2, True, False), (3, True, False),
                          (4, True, False), (5, True, False)],
               "a cont run covers 2–5 and SKIPS scene 1 even when the range asks for it (%r)"
               % (_flags,))
            ck(w.long_plan["scenes"][5]["kf_cont"] is False,
               "and it stops at the end of the range — scene 6 untouched")
            w.long_cont_run_apply(2, 5, False)
            root.update_idletasks()
            _flags = [(x["n"], x["kf_cont"], x["kf_first"]) for x in w.long_plan["scenes"][:5]]
            ck(all(c is False and f is True for _n, c, f in _flags),
               "CLEAR gives the START back to the still (%r)" % (_flags,))

            # ── THE FLAGS DECIDE THE VIDEO MODE (item 4) — pure selection, NO spend ──
            def _mk(**kw):
                _s = long_scene_new(1)
                _s.update(kw)
                return _s

            _s = _mk(kf_first=True, still="/tmp/a.png")
            _r = w.long_video_plan(0, _s)
            ck(_r[0] == "i2v" and _r[1] == ["/tmp/a.png"],
               "first only → i2v carrying the opening still (%r)" % (_r,))
            _s = _mk(kf_first=True, kf_last=True, still="/tmp/a.png", still_last="/tmp/b.png")
            _r = w.long_video_plan(1, _s)
            ck(_r[0] == "flf" and _r[1] == ["/tmp/a.png", "/tmp/b.png"],
               "first+last → flf, ref1=first / ref2=last — the ORDER is the wiring (%r)" % (_r,))
            _s = _mk(kf_first=True, kf_last=True, still="/tmp/a.png")
            _r = w.long_video_plan(1, _s)
            ck(_r[0] is None and "no landing still" in _r[2],
               "last with no landing still is REFUSED, not run as a first-frame shot (%r)"
               % (_r,))
            _s = _mk(kf_first=False, kf_last=True, still="/tmp/a.png", still_last="/tmp/b.png")
            _r = w.long_video_plan(1, _s)
            ck(_r[0] is None and "FIRST frame" in _r[2],
               "last with nothing supplying a START is REFUSED — the silent-degradation guard "
               "(%r)" % (_r,))
            _s = _mk(kf_first=False, kf_cont=True)
            _r = w.long_video_plan(3, _s)
            ck(_r[0] is None and "rendered AND kept" in _r[2],
               "cont with an unrendered previous scene is REFUSED, with the reason (%r)" % (_r,))
            _s = _mk(kf_first=False, kf_cont=True, kf_last=True, still_last="/tmp/b.png")
            _r = w.long_video_plan(3, _s)
            ck(_r[0] is None and "rendered AND kept" in _r[2],
               "cont + last still needs the inherited frame before it can build the flf (%r)"
               % (_r,))
            _s = _mk(kf_first=False, kf_ref=True, still="/tmp/a.png")
            _r = w.long_video_plan(1, _s)
            ck(_r[0] == "r2v" and _r[1][-1] == "/tmp/a.png",
               "ref-only → r2v carrying the still (%r)" % (_r,))
            _s = _mk(kf_first=False)
            _r = w.long_video_plan(1, _s)
            ck(_r[0] == "t2v" and _r[1] == [],
               "no roles and no piece reference → t2v, not a phantom r2v (%r)" % (_r,))

            # both of these assert REAL BUGS found while building this block
            # ⚠️ st["name"], not plan["name"]: refresh() saves under st["name"], and this driver
            # renames only plan["name"] — so reading the plan path off plan["name"] raised
            # FileNotFoundError, which _log_exc swallowed and left the mainloop spinning forever.
            # ⚠️ And it toggles its OWN flag rather than depending on state an earlier block left
            # behind — that dependency is what broke when the slot block above reset every role.
            w.long_pick(5)
            w.long_kf("kf_cont")
            root.update_idletasks()
            _pj = json.load(open(long_plan_path(w.long_state["name"])))
            ck(_pj["scenes"][5].get("kf_cont") is True,
               "toggling a KF flag PERSISTS to plan.json, not just to the widgets")
            w.long_kf("kf_cont")            # …and back off
            root.update_idletasks()
            w.long_pick(0)
            w.long_kf("kf_cont")            # scene 1 — must refuse
            root.update_idletasks()
            ck("refused" in w.long_dump()["kf_note"]
               and not w.long_dump()["kf_flags"]["kf_cont"],
               "scene 1's cont is refused AND the note survives the click (%r)"
               % w.long_dump()["kf_note"])
            w.long_refresh()

            for _s in w.long_plan["scenes"]:
                if not _s.get("kf_cont"):
                    _s["still"] = "/tmp/kf-does-not-exist.png"   # stand-in; nothing submits
            w.long_kf_draft()
            root.update_idletasks()
            ck("Nothing to keyframe" in w.long_dump()["refusal"],
               "DRAFT with nothing left refuses instead of submitting (%r)"
               % w.long_dump()["refusal"][:52])
            for _s in w.long_plan["scenes"]:
                _s["still"] = None
                _s["kf_cont"] = False
                _s["kf_first"] = True
            w.long_pick(0)
            w.long_refresh()

            # ── the STYLE BLOCK (§3 item 5) — per-PIECE, and it must reach the prompt ──
            ck(w.long_subject() == "", "a fresh piece has NO style block (%r)"
               % w.long_subject())
            ck("NOT SET" in w.long_dump()["treatment_detail"],
               "the treatment SAYS the block is unset instead of claiming it is pinned")
            _blk = "90s cel animation, muted earth palette, flat colours, thick ink outlines"
            w.long_set_subject(_blk)
            w.long_pick(0)
            root.update_idletasks()
            ck(w.long_plan["subject_block"] == _blk and w.long_subject() == _blk,
               "the style block lives on the PIECE, not on a scene")
            ck(w.long_kf_prompt().startswith(_blk),
               "the block is pinned in FRONT of the scene prompt (%r)"
               % w.long_kf_prompt()[:44])
            ck("pinned" in w.long_dump()["treatment_detail"],
               "the treatment reports it as pinned once set")

            # ── OPEN on a name that does not exist ────────────────────────────────
            _n_before = len(w.long_plan["scenes"])
            _e = w.long_name_entry
            _e.delete(0, "end"); _e.insert(0, "definitely-not-an-episode")
            w.long_open()
            root.update_idletasks()
            ck(len(w.long_plan["scenes"]) == _n_before
               and "no episode called" in w.long_dump()["ep_label"],
               "OPEN on a missing episode REFUSES instead of emptying the list (%r, %d scenes)"
               % (w.long_dump()["ep_label"], len(w.long_plan["scenes"])))
            _e.delete(0, "end"); _e.insert(0, "test-scratch")
            root.update_idletasks()
            w.long_pick(3)
            w.long_pedit.insert("1.0", "Fireside: he sings, hands flat on the table.")
            w.long_prompt_sync()
            root.update_idletasks()
            d2 = w.long_dump()
            ck(w.long_plan["scenes"][3]["prompt"].startswith("Fireside"),
               "prompt editor writes through to the scene model")
            ck("Fireside" in d2["caption"] or "SCENE 4" in d2["caption"],
               "bench follows the selection (%r)" % d2["caption"])
            ck(d2["window"].count("continuous") == 1 and "this scene" in d2["window"],
               "audio strip shows this scene's window on the one track (%r)" % d2["window"])

            # the drafted window label should be scene 4's slice, not scene 1's
            # scene 4 starts at 3 x 14.81 s = 0:44 and ends at 0:59 on a 2:57 track
            ck("0:44" in d2["window"] and "0:59" in d2["window"],
               "window label recomputed for the selected scene (%r)" % d2["window"])

            # "unkept only" filter
            w.long_plan["scenes"][0]["keep"] = 0        # mark one kept, or the filter is vacuous
            before = w.long_dump()["queue_rows"]
            w.long_state["filter"] = True
            w.long_refresh()
            root.update_idletasks()
            after = w.long_dump()["queue_rows"]
            ck(after == before - 1, "unkept-only filter drops the kept scene (%d -> %d)"
               % (before, after))
            w.long_state["filter"] = False
            w.long_refresh()

            # the STITCH refusal — the whole point of the guard
            w.long_stitch_for_test()
            root.update_idletasks()
            d3 = w.long_dump()
            ck("Cannot stitch yet" in d3["refusal"],
               "STITCH refuses an incomplete keeper set (%r)" % d3["refusal"][:70])
            ck("0 of 12" in d3["refusal"] or "of 12 scenes have a keeper" in d3["refusal"],
               "refusal names how many are missing")

            # the treatment picks: without these the draft would fail at submit
            w.long_set_audio("/tmp/fake-track.mp3")
            w.long_set_ref("/tmp/fake-ref.png")
            root.update_idletasks()
            d4 = w.long_dump()
            ck("fake-track.mp3" in d4["treatment"],
               "the AUDIO pick shows on the treatment line (%r)" % d4["treatment"][-46:])
            ck("fake-ref.png" in d4["treatment"],
               "the REF pick shows on the treatment line")
            # and they must survive a round-trip through the REAL save path — not a direct
            # long_plan_save call, because the loaded-guard is exactly what governs that path
            w.long_refresh()
            back = long_plan_load(w.long_state["name"])
            ck(back.get("audio") == "/tmp/fake-track.mp3" and
               back.get("ref") == "/tmp/fake-ref.png" and
               back.get("subject_block") == _blk,
               "audio + ref + the style block all persist in plan.json")

            # ── the BEAT row (motion intent · colour state · transition out) ───────────────
            _sel = w.long_state["sel"]
            ck(w.long_dump()["motion_intents"] == ["setup", "anticipation", "commitment",
                                                   "impact", "brake", "settle"],
               "the motion vocabulary is the spec's six (%r)" % (w.long_dump()["motion_intents"],))
            _fresh = long_scene_new(1)
            ck(all(_fresh.get(k) == "" for k in ("motion", "color_state", "transition_out")),
               "a fresh scene's BEAT fields are EMPTY (no filler in a neutral scene)")
            _legacy = {"n": 4, "prompt": "x", "chunks": 3, "takes": [], "keep": None}
            long_plan_migrate({"scenes": [_legacy]})
            ck(all(k in _legacy for k in ("motion", "color_state", "transition_out")),
               "long_plan_migrate backfills the BEAT fields onto an OLD plan")
            _kept = {"n": 5, "motion": "impact", "color_state": "cold blue",
                     "transition_out": "hard cut on the bass hit"}
            long_plan_migrate({"scenes": [_kept]})
            ck(_kept["motion"] == "impact" and _kept["color_state"] == "cold blue",
               "migration does NOT overwrite a BEAT value that is already set")
            # drive the REAL handler, never the dict
            w.long_beat_apply(motion="impact", color_state="cold blue",
                              transition_out="hard cut on the bass hit")
            root.update_idletasks()
            d5 = w.long_dump()
            ck(d5["beat_saved"]["motion"] == "impact", "apply sets the selected scene's motion")
            ck(d5["beat_shown"] == d5["beat_saved"],
               "the BEAT row SHOWS what it SAVES (%r vs %r)" % (d5["beat_shown"], d5["beat_saved"]))
            # ⚠️ The chip lives on a BOARD card, so the board has to be ON — render_board
            # early-returns (and clears the log) while the TABLE view is showing.
            w.long_board(True)
            root.update_idletasks()
            ck(w.long_dump()["board_motions"][_sel] == "impact",
               "the board card carries the motion intent (%r)" % (w.long_dump()["board_motions"],))
            w.long_board(False)
            root.update_idletasks()
            # ⚠️ NO `long_refresh()` here on purpose. An explicit refresh saved the plan itself
            # and made this assertion pass even with `beat_push`'s own refresh() deleted — a test
            # that lies. The save must come from the handler under test.
            _back5 = long_plan_load(w.long_state["name"])
            ck(_back5["scenes"][_sel].get("transition_out") == "hard cut on the bass hit",
               "the BEAT fields persist through the REAL save path")
            # ⚠️ The row must FOLLOW the selection. If it does not, it shows scene A's beat while
            # scene B is selected and the next keystroke writes A's values onto B.
            _other = 0 if _sel != 0 else 1
            w.long_pick(_other)
            root.update_idletasks()
            ck(w.long_dump()["beat_shown"] == {"motion": "", "color_state": "",
                                               "transition_out": ""},
               "selecting an untouched scene EMPTIES the BEAT row (%r)"
               % (w.long_dump()["beat_shown"],))
            w.long_pick(_sel)
            root.update_idletasks()
            ck(w.long_dump()["beat_shown"]["motion"] == "impact"
               and w.long_dump()["beat_shown"]["color_state"] == "cold blue",
               "picking back restores the row from the scene (%r)"
               % (w.long_dump()["beat_shown"],))
            # ⚠️ AUTHORING-ONLY. If a beat field ever reaches `_scene_video_plan`, a scene silently
            # renders something else — so this is asserted, not assumed.
            _s2 = dict(w.long_plan["scenes"][1])
            _with = w.long_video_plan(1, dict(_s2, motion="impact", color_state="cold blue",
                                              transition_out="hard cut"))
            _without = w.long_video_plan(1, dict(_s2, motion="", color_state="",
                                                 transition_out=""))
            ck(_with == _without,
               "a BEAT field does NOT change what will run (authoring only)")
            # and clearing it must return the row to neutral, not to a stale value
            w.long_beat_apply(motion="", color_state="", transition_out="")
            root.update_idletasks()
            ck(w.long_dump()["beat_saved"] == {"motion": "", "color_state": "",
                                               "transition_out": ""},
               "clearing the BEAT row saves EMPTY, not the old value")
            ck(w.long_dump()["beat_shown"]["motion"] == "",
               "and the dropdown reads neutral again (%r)" % (w.long_dump()["beat_shown"],))

            # ── the LENGTH slider (H3 ladder) ──────────────────────────────────────────────
            _d = w.long_dump()
            ck(len(_d["h3_ladder"]) == 18
               and abs(_d["h3_ladder"][0] - 73 / 24.0) < 1e-9
               and abs(_d["h3_ladder"][-1] - 362 / 24.0) < 1e-9,
               "the ladder runs 3.04 s -> 15.08 s in 18 rungs (%.4f .. %.4f)"
               % (_d["h3_ladder"][0], _d["h3_ladder"][-1]))
            ck(all((f - 5) % 17 == 0 for f in _d["h3_frames_ladder"]),
               "every rung is a legal n*17+5 frame count")
            ck(all(h3_frames(s) == f for s, f in zip(_d["h3_ladder"], _d["h3_frames_ladder"])),
               "h3_frames() is IDEMPOTENT on every rung (the readout cannot drift from the render)")
            ck(all(abs(long_scene_seconds_for(3, "h3", s) - s) < 1e-6
                   for s in _d["h3_ladder"]),
               "long_scene_seconds_for() reproduces every rung exactly")
            ck(_d["len_control"] == "h3", "on H3 the SLIDER is the mapped length control (%r)"
               % _d["len_control"])
            # ⚠️ THE GUARD FOR THE BUG THIS REPLACED: the old dropdown showed WAN labels on H3
            # ('4.81 s  (1 chunk)') while H3 rendered 5.17 s. The readout must equal the render.
            w.long_h3_len(9)
            root.update_idletasks()
            _d = w.long_dump()
            ck(abs(float(_d["len_shown"]) - float(_d["len_saved"])) < 1e-6,
               "the LENGTH readout EQUALS what will render (%.4f shown vs %.4f saved)"
               % (_d["len_shown"], _d["len_saved"]))
            ck(abs(float(_d["len_saved"]) - _d["h3_ladder"][9]) < 1e-6,
               "and that is rung 9 (%.4f s)" % _d["h3_ladder"][9])
            # ⚠️ `_scene_params` returns a TUPLE (mode, params, refs, why), and `params` is None
            # when the scene is REFUSED — so the reason is printed, not swallowed.
            _probe = dict(long_scene_new(3), prompt="a test shot", secs=_d["h3_ladder"][9])
            # ⚠️ `sliced=False` — an earlier assertion left a fake audio path on the state, and
            # the sliced path shells out to ffmpeg for the guide wav. This is the same call
            # `long_scene_cost` makes.
            _pm, _pp, _pr, _pwhy = w.long_scene_params(2, _probe, -1, 0.0, 0.0, False)
            ck(_pp is not None and _pp.get("length") == _d["h3_frames_ladder"][9],
               "the GRAPH is told the same frame count as the readout (%r vs %s%s)"
               % (_pp and _pp.get("length"), _d["h3_frames_ladder"][9],
                  "" if _pp else " why=%r" % (_pwhy,)))
            # ⚠️ NO explicit refresh: the save must come from the handler under test.
            _bl = long_plan_load(w.long_state["name"])
            ck(abs(float(_bl["scenes"][_sel].get("secs") or 0) - _d["h3_ladder"][9]) < 1e-6,
               "the length persists through the REAL save path")
            # ⚠️ SELECTING a scene must not WRITE to it. tk.Scale.set() fires the command, so
            # without the busy-guard an untouched scene got silently pinned to 3.04 s.
            w.long_pick(_other)
            root.update_idletasks()
            ck(float(w.long_dump()["scene_secs"] or 0) == 0.0,
               "merely SELECTING an untouched scene leaves it UNSET (%r)"
               % (w.long_dump()["scene_secs"],))
            ck(abs(float(w.long_dump()["len_saved"]) - H3_SCENE_FRAMES[3] / 24.0) < 1e-6,
               "and an UNSET scene still falls back to the old chunks table (%.2f s)"
               % w.long_dump()["len_saved"])
            # the other engine keeps its own control
            w.long_set_engine("wan")
            root.update_idletasks()
            ck(w.long_dump()["len_control"] == "wan",
               "on WAN the CHUNK dropdown is mapped instead (%r)" % w.long_dump()["len_control"])
            w.long_set_engine("h3")
            root.update_idletasks()
            _mig = {"n": 7, "prompt": "x", "chunks": 3, "takes": [], "keep": None}
            long_plan_migrate({"scenes": [_mig]})
            ck(_mig.get("secs") == 0.0,
               "migration backfills secs as UNSET (0.0), not a pinned length")

            # ── the AUDIO STRIP is a real waveform, not a drawing ─────────────────────────
            import array as _arr
            import wave as _wave
            import tempfile as _tf

            def _strip_heights():
                return [max(0.0, 34 - w.long_wav.coords(i)[3]) for i in w.long_wav.find_all()
                        if w.long_wav.type(i) == "line"]

            _sil = os.path.join(_tf.gettempdir(), "tl_silence.wav")
            with _wave.open(_sil, "wb") as _wf:
                _wf.setnchannels(1); _wf.setsampwidth(2); _wf.setframerate(8000)
                _wf.writeframes(b"\x00\x00" * 16000)
            w.long_set_audio(_sil)
            w.long_refresh(); root.update_idletasks()
            # ⚠️ THE FALSIFIABLE ONE. The sine pattern drew the same full-height shape for ANY
            # input including silence, so a flat strip for a silent track is exactly what it
            # could not do.
            _hs = _strip_heights()
            ck(_hs and max(_hs) <= 5.0,
               "a SILENT track draws a FLAT strip (max %.1f px; the old sine drew 26)"
               % (max(_hs) if _hs else -1))
            ck("no audio loaded" in w.long_dump()["window"],
               "and the strip SAYS there is no audio rather than showing an empty box")
            ck(audio_peaks(_sil, 64) is None,
               "audio_peaks returns None for silence (all-zero audio is not a waveform)")

            _loud = os.path.join(_tf.gettempdir(), "tl_loud.wav")
            _n = 8000 * 2
            _buf = _arr.array("h", [0] * _n)
            for _k in range(0, _n, 2000):                      # 4 pulses/second
                for _j in range(_k, min(_n, _k + 60)):
                    _buf[_j] = 20000 if (_j // 10) % 2 == 0 else -20000
            with _wave.open(_loud, "wb") as _wf:
                _wf.setnchannels(1); _wf.setsampwidth(2); _wf.setframerate(8000)
                _wf.writeframes(_buf.tobytes())
            w.long_set_audio(_loud)
            w.long_refresh(); root.update_idletasks()
            _hs2 = _strip_heights()
            ck(_hs2 and max(_hs2) >= 28.0,
               "a LOUD track draws FULL-height peaks (max %.1f px)" % (max(_hs2) if _hs2 else -1))
            ck(_hs2 and min(_hs2) <= 8.0,
               "and the columns VARY, so it is a waveform and not a solid block (min %.1f px)"
               % (min(_hs2) if _hs2 else -1))
            ck(len(audio_peaks(_loud, 64) or []) == 64,
               "audio_peaks returns one value per requested column")
            ck(audio_peaks("/tmp/tl-nope-does-not-exist.mp3", 64) is None,
               "a missing track returns None, not a fabricated waveform")

            # ⚠️ and the whole run must leave the DEFAULT episode alone
            _u_now = (os.path.exists(_u), os.path.getsize(_u) if os.path.exists(_u) else 0,
                      os.path.getmtime(_u) if os.path.exists(_u) else 0)
            ck(_u_now == _u_was,
               "a whole TESTLONG run NEVER touches episodes/untitled/plan.json (%r -> %r)"
               % (_u_was, _u_now))

            print("  plan.json path: %s" % long_plan_path(w.long_state["name"]), flush=True)
            print("TESTLONG failures: %d" % len(fails), flush=True)
            root.after(200, root.destroy)

        root.after(2500, _tl)

    if os.environ.get("FORGE_LAB_TESTKF") == "1":
        # ⚠️ THE ONLY DRIVER THAT SUBMITS REAL WORK. Everything else here drives widgets; this
        # one proves the KEYFRAME path end to end: submit -> poll -> collect -> a PNG on disk at
        # the treatment canvas. Set FORGE_LAB_TESTKF_REF to a still to exercise `edit` +
        # custom_size (the path a piece uses when a reference is chosen) instead of `t2i`.
        def _tkf():
            print("TESTKF start", flush=True)
            try:
                w = open_long_video_workspace()
                w.long_plan.clear()
                w.long_plan.update({
                    "name": "test-scratch", "audio": None, "ref": None,
                    "treatment": {"w": 544, "h": 736, "fps": 24, "chunks": 3, "steps": 4,
                                  "cfg": 1.0, "shift": 8.0, "h3_steps": 12, "engine": "h3"},
                    "scenes": [long_scene_new(1)]})
                # ⚠️ st["name"] too, NOT just plan["name"]: refresh() saves under st["name"], and
                # leaving it "untitled" would OVERWRITE a real episode's plan.json.
                w.long_state["name"] = "test-scratch"
                w.long_state["loaded"] = True
                w.long_plan["scenes"][0]["prompt"] = (
                    "young man of 20, strong angular jaw, broad shoulders, standing in a "
                    "1990s pizzeria, deadpan, muted earth palette, flat colours, thick ink "
                    "outlines")
                _ref = os.environ.get("FORGE_LAB_TESTKF_REF")
                if _ref:
                    w.long_set_ref(_ref)
                # ⚠️ EXPLICIT. The default is now NO roles, so a driver that wants a still has to
                # ask for one — a fresh scene wants zero slots and `want` comes back empty, which
                # reads as a failure of the still path when it is only a driver with no roles set.
                w.long_plan["scenes"][0]["kf_first"] = True
                if os.environ.get("FORGE_LAB_TESTKF_LAST") == "1":
                    w.long_plan["scenes"][0]["kf_last"] = True   # the SECOND SLOT
                root.update_idletasks()
                inf = w.long_kf_info()
                print("  plan: mode=%s %dx%d steps=%d todo=%d  est=%.1fs"
                      % (inf["mode"], inf["w"], inf["h"], inf["steps"], inf["todo"],
                         inf["per"]), flush=True)
                print("  ref=%r" % inf["ref"], flush=True)
                w.long_kf_worker()               # BLOCKS until the stills land, or error
                s = w.long_plan["scenes"][0]
                want = w.long_kf_slots(s)    # every slot the ROLES asked for
                res = {}
                for _sl in want:
                    _p = s.get(_sl)
                    _ex = bool(_p) and os.path.exists(_p)
                    res[_sl] = (_ex, image_size(_p) if _ex else None,
                                os.path.getsize(_p) if _ex else 0,
                                bool(s.get("still_last_prompt" if _sl == "still_last"
                                           else "still_prompt")))
                print("  still_error=%r" % s.get("still_error"), flush=True)
                for _sl in want:
                    print("  %-11s exists/size/bytes/prompt = %r" % (_sl, res[_sl]),
                          flush=True)
                # ⚠️ OPTIONAL, and the only part of TESTKF that renders VIDEO. Set
                # FORGE_LAB_TESTKF_VIDEO=1 to prove the flag→mode mapping end to end. It uses the
                # 1-chunk turbo draft path on purpose: this is a PLUMBING test, not a quality one,
                # and 124 frames at 4 steps costs a fraction of 355 at 12.
                if os.environ.get("FORGE_LAB_TESTKF_VIDEO") == "1":
                    _sv = w.long_plan["scenes"][0]
                    _sv["chunks"] = 1
                    w.long_plan["treatment"]["h3_steps"] = 4
                    w.long_plan["treatment"]["turbo"] = True
                    _vm, _rv, _wy = w.long_video_plan(0, _sv)
                    print("  video: mode=%r refs=%r why=%r"
                          % (_vm, [os.path.basename(x) for x in (_rv or [])], _wy), flush=True)
                    w.long_worker(True)
                    _tk = (_sv.get("takes") or [{}])[-1]
                    _f = _tk.get("file")
                    print("  take: seed=%r status=%r" % (_tk.get("seed"), _tk.get("status")),
                          flush=True)
                    if _f and os.path.exists(_f):
                        print("  video file: %s  (%d bytes)"
                              % (_f, os.path.getsize(_f)), flush=True)

                        # ⚠️ _f, NOT f: `f` in this scope is the app's FONT object, and passing it
                        # to ffprobe is a TypeError ("expected str ... not Font").
                        _probe = subprocess.run(
                            ["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-count_frames", "-show_entries",
                             "stream=width,height,nb_read_frames", "-of", "csv=p=0", _f],
                            capture_output=True, text=True).stdout.strip()
                        _dur = subprocess.run(
                            ["ffprobe", "-v", "error", "-show_entries",
                             "format=duration", "-of", "csv=p=0", _f],
                            capture_output=True, text=True).stdout.strip()
                        print("  video probe: %r  duration=%s s" % (_probe, _dur), flush=True)
                        res["__video__"] = (True, (_vm, len(_rv or [])), _dur, True)
                    else:
                        res["__video__"] = (False, None, None, False)

                # ⚠️ OPTIONAL: prove `flf` AND `cont` with REAL renders. THREE passes, one scene
                # each — a single _worker over all three cannot work, because `_cont_src` is
                # exactly scene i-1's KEPT take, so scene 3 would be refused until scene 2 is kept.
                if os.environ.get("FORGE_LAB_TESTKF_CHAIN") == "1":
                    _P = ("young man of 20, strong angular jaw, broad shoulders, standing in a "
                          "1990s pizzeria, deadpan")
                    w.long_plan["treatment"]["h3_steps"] = 4
                    w.long_plan["treatment"]["turbo"] = True

                    def _mk(n, first):
                        _sc = long_scene_new(n)
                        _sc["prompt"] = _P
                        _sc["chunks"] = 1
                        # ⚠️ Explicit: the default is now NO roles, so a chain that wants a
                        # first-frame start has to say so.
                        _sc["kf_first"] = first
                        return _sc

                    _one = _mk(1, True)
                    _two = _mk(2, True)
                    _two["kf_last"] = True                                # first + last → flf
                    _three = _mk(3, False)
                    _three["kf_cont"] = True                              # → cont
                    _mats = [_one, _two, _three]
                    _want = ["i2v", "flf", "cont"]
                    for _k in range(3):
                        # only scenes 1.._k exist in this pass, so _worker cannot run ahead
                        w.long_plan["scenes"] = list(_mats[:_k + 1])
                        # ⚠️ DRAFT FIRST, then ask what mode the scene is. Asking before drafting
                        # reported "no landing still has been drafted" for a scene whose still had
                        # simply not been made yet — and the driver then SKIPPED the draft that
                        # would have made it satisfiable. A test that fails itself is not a test.
                        if w.long_kf_todo():
                            w.long_kf_worker()
                        _vm, _rv, _wy = w.long_video_plan(_k, _mats[_k])
                        print("  chain[%d] mode=%r refs=%r why=%r"
                              % (_k, _vm, [os.path.basename(x) for x in (_rv or [])], _wy),
                              flush=True)
                        res["__chain_%d__" % _k] = (False, (_vm, _wy), None, False)
                        if _wy:
                            continue
                        w.long_worker(True)         # renders only the un-kept scene
                        _t = (_mats[_k].get("takes") or [{}])[-1]
                        _f2 = _t.get("file")
                        _ok2 = bool(_f2) and os.path.exists(_f2)
                        _fr = "-"
                        if _ok2:
                            _fr = subprocess.run(
                                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                                 "-count_frames", "-show_entries", "stream=nb_read_frames",
                                 "-of", "csv=p=0", _f2],
                                capture_output=True, text=True).stdout.strip()
                            # ⚠️ KEEP it, or the NEXT pass's `cont` has no frame to inherit
                            _mats[_k]["keep"] = 0
                        print("  chain[%d] status=%r frames=%s bytes=%s"
                              % (_k, _t.get("status"), _fr,
                                 os.path.getsize(_f2) if _ok2 else 0), flush=True)
                        res["__chain_%d__" % _k] = (_ok2, (_vm, len(_rv or [])), _fr, True)
                    print("  chain result: modes=%r rendered=%r  (want %r)"
                          % ([res["__chain_%d__" % _k][1][0] for _k in range(3)],
                             [res["__chain_%d__" % _k][0] for _k in range(3)], _want),
                          flush=True)

                # ⚠️ The chain's verdict folds into TESTKF's single pass/fail. It runs INSIDE this
                # driver, so it reports through `res` — TESTLONG's `ck` does not exist here.
                _chain_expect = os.environ.get("FORGE_LAB_TESTKF_CHAIN") == "1"
                _chain_modes = ([res["__chain_%d__" % _k][1][0] for _k in range(3)]
                                if _chain_expect else [])
                _chain_ok = (all(res["__chain_%d__" % _k][0] for _k in range(3))
                             if _chain_expect else True)
                ok = (bool(want) and not s.get("still_error")
                      and all(v[0] and v[1] == (544, 736) and v[3]
                              for k2, v in res.items() if not k2.startswith("__"))
                      and res.get("__video__", (True,))[0]
                      and _chain_ok
                      and (not _chain_expect or _chain_modes == ["i2v", "flf", "cont"]))
                print("TESTKF failures: %d" % (0 if ok else 1), flush=True)
            except Exception as exc:
                print("  TESTKF EXCEPTION %s: %s" % (type(exc).__name__, exc), flush=True)
                print("TESTKF failures: 1", flush=True)
            root.after(200, root.destroy)

        root.after(2500, _tkf)

    if os.environ.get("FORGE_LAB_RESTITCH") == "1":
        # Re-runs STAGE 2 on an existing episode. No GPU: the keepers are on disk.
        def _rs():
            print("RESTITCH start", flush=True)
            w = open_long_video_workspace()
            w.load_plan("untitled")
            root.update_idletasks()
            d = w.long_dump()
            print("  kept=%d/%d" % (d["kept"], d["scenes"]), flush=True)
            w.long_stitch()
            root.update_idletasks()
            print("  STITCH: %s" % w.long_dump()["refusal"].replace("\n", " | "), flush=True)
            print("  plan says: runtime=%s cut=%s" % (w.long_plan.get("stitch_runtime"),
                                                      w.long_plan.get("bed_track_cut_s")),
                  flush=True)
            root.after(300, root.destroy)
        root.after(2500, _rs)

    if os.environ.get("FORGE_LAB_RUNTEST") == "1":
        # A REAL run: draft 2 scenes on the GPU through the workspace's OWN worker, keep both,
        # stitch with the bed track, and report what the WRITTEN FILE contains. The expected
        # runtime is 2 x 4.81 = 9.62 s — a stitch that reads 4.81 s is the failure this catches.
        def _rt():
            print("RUNTEST start", flush=True)
            w = open_long_video_workspace()
            here = os.path.dirname(os.path.abspath(__file__))
            audio = os.path.join(here, "inputs/runtest_track.wav")
            ref = os.path.expanduser("~/forge-lab-assets/character-ref.png")
            w.long_set_audio(audio)
            w.long_set_ref(ref)
            pl = w.long_plan
            pl["scenes"] = [
                {"n": 1, "prompt": "A young man sings straight at the camera, mouth moving "
                                   "with the words. Static close-up.", "chunks": 1,
                 "takes": [], "keep": None},
                {"n": 2, "prompt": "The same man turns slightly and keeps singing, small "
                                   "head movement. Static close-up.", "chunks": 1,
                 "takes": [], "keep": None}]
            w.long_state["sel"] = 0
            w.long_refresh()
            print("  plan: %d scenes x 1 chunk (4.81 s each) · bed track %s"
                  % (len(pl["scenes"]), os.path.basename(audio)), flush=True)
            threading.Thread(target=w.long_worker, args=(True,), daemon=True).start()

            def watch(n=0):
                if w.long_state["drafting"] and n < 400:
                    root.after(5000, lambda: watch(n + 1))
                    return
                for sc in pl["scenes"]:
                    for t in sc.get("takes") or []:
                        print("  scene %d: seed=%s status=%s file=%s"
                              % (sc["n"], t.get("seed"), t.get("status"),
                                 os.path.basename(t.get("file") or "-")), flush=True)
                    if sc.get("takes"):
                        sc["keep"] = 0
                w.long_refresh()
                print("  kept=%d/%d  est=%r" % (w.long_dump()["kept"], len(pl["scenes"]),
                                                w.long_dump()["cost"].split("|")[0].strip()),
                      flush=True)
                w.long_stitch()
                root.update_idletasks()
                print("  STITCH RESULT: %s" % w.long_dump()["refusal"].replace("\n", " | "),
                      flush=True)
                print("  stitched file: %s" % pl.get("stitched"), flush=True)
                root.after(500, root.destroy)
            root.after(5000, watch)
        root.after(2500, _rt)

    if os.environ.get("FORGE_LAB_TESTQUALITY") == "1":
        # Guards the bug Ryan hit: the quality menu applied the preset but never set its OWN
        # variable, so the drift check flipped the label to "Custom". The test must drive the
        # MENU ITEM (invoke) — what a click does — because calling apply_quality() directly
        # passes an explicit name and would hide the bug.
        def _tq():
            print("TESTQUALITY start", flush=True)
            bad = 0
            for kind in MODEL_CHOICES:
                kind_var.set(kind)
                on_kind()
                for (md, _var, lab) in MODEL_CHOICES[kind]:
                    pick_model(lab)
                    root.update_idletasks()
                    if md == "wan":
                        # S2V has exactly ONE path, so it must NOT offer the turbo/full pair
                        presets = ([S2V_QUALITY] if st.get("wan_mode") == "s2v"
                                   else list(WAN_QUALITY))
                    elif md in ("ltx", "animate2"):
                        # ⚠️ These BAKE their steps, so the dropdown is a one-entry READOUT. The
                        # test has to expect that or it reports the app as broken when it is right
                        # (it used to fall to `else` and demand the image presets — 8 failures).
                        presets = [fixed_quality_label(md)]
                    elif md == "video":
                        presets = list(VIDEO_QUALITY)
                    else:
                        presets = list(QUALITY)
                    cnt = qmenu["menu"].index("end")
                    cnt = 0 if cnt is None else cnt + 1
                    labels = [qmenu["menu"].entrycget(i, "label") for i in range(cnt)]
                    for p in presets:
                        if md == "wan" and st.get("wan_mode") == "s2v" and cnt != 1:
                            print("  S2V offered %d paths — it must offer exactly 1" % cnt)
                            bad += 1
                        if p not in labels:
                            print("  %-31s preset %r NOT IN MENU" % (lab[:31], p), flush=True)
                            bad += 1
                            continue
                        qmenu["menu"].invoke(labels.index(p))
                        root.update_idletasks()
                        ok = (qvar.get() == p)
                        if not ok:
                            bad += 1
                        print("  %-31s click %-14r -> qvar=%-14r steps=%-3s %s"
                              % (lab[:31], p, qvar.get(), e_steps.get(),
                                 "ok" if ok else "FAIL"), flush=True)
                    # ⚠️ THE FIELD MUST NOT LIE — asserted for EVERY model. `e_steps` is the number
                    # the user READS (and the one the estimate is quoted from); `params()["steps"]`
                    # is what forge is actually told. On a baked engine those two disagreed: the
                    # field showed H3's 20 while params sent 8.
                    try:
                        _shown = int(float(e_steps.get() or 0))
                    except Exception:
                        _shown = -1
                    _runs = (params() or {}).get("steps")
                    if _shown != _runs:
                        print("  %-31s ** STEPS FIELD LIES: shows %s, forge runs %s"
                              % (lab[:31], _shown, _runs), flush=True)
                        bad += 1

                    print("      note: %s" % phelp.cget("text")[:78], flush=True)
            print("\nTESTQUALITY failures: %d" % bad, flush=True)
            root.after(300, root.destroy)
        root.after(2500, _tq)

    if os.environ.get("FORGE_LAB_TESTPOLISH") == "1":
        def _tp_go():
            prompt.delete("1.0", "end")
            prompt.insert("1.0", "bernese mountain dog dressed as a person while "
                                 "driving a car")
            _dbg("TESTPOLISH: firing do_polish")
            do_polish()

        def _tp_report():
            box = prompt.get("1.0", "end").strip()
            line = ("TESTPOLISH report: drain_passes=%s box_len=%d polmsg=%r "
                    "btn=%s revert=%s box_head=%r" % (
                        st.get("drain_n"), len(box), pol_msg.cget("text"),
                        polish_btn.cget("state"), revert_btn.cget("state"),
                        box[:70]))
            _dbg(line)
            print(line, flush=True)
            root.after(500, root.destroy)
        root.after(2500, _tp_go)
        root.after(75000, _tp_report)

    if os.environ.get("FORGE_LAB_TESTCLEAR") == "1":
        # drives CLEAR + REVERT without a human, because "the button is there" and
        # "the button works" are different claims
        def _tc_go():
            prompt.delete("1.0", "end")
            prompt.insert("1.0", "a lighthouse in a storm, oil painting")
            line0 = "TESTCLEAR start: box=%d revert=%s" % (
                len(prompt.get("1.0", "end").strip()), revert_btn.cget("state"))
            _dbg(line0); print(line0, flush=True)
            do_clear()
            line1 = "TESTCLEAR after clear: box=%d revert=%s msg=%r" % (
                len(prompt.get("1.0", "end").strip()), revert_btn.cget("state"),
                pol_msg.cget("text"))
            _dbg(line1); print(line1, flush=True)
            do_clear()          # clearing an empty box must not arm REVERT
            line2 = "TESTCLEAR clear-again: revert=%s msg=%r" % (
                revert_btn.cget("state"), pol_msg.cget("text"))
            _dbg(line2); print(line2, flush=True)
            do_revert()
            line3 = "TESTCLEAR after revert: box=%d text=%r" % (
                len(prompt.get("1.0", "end").strip()),
                prompt.get("1.0", "end").strip()[:40])
            _dbg(line3); print(line3, flush=True)
            root.after(400, root.destroy)
        root.after(2500, _tc_go)

    if os.environ.get("FORGE_LAB_TESTCOLOR") == "1":
        # walks the REAL widget tree: proves the contrast of what is on screen,
        # rather than trusting that the right hex made it into the source
        def _lum(h):
            h = h.lstrip("#")
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            if len(h) != 6:
                return None
            r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            def f(c):
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

        def _ratio(a, b):
            la, lb = _lum(a), _lum(b)
            if la is None or lb is None:
                return None
            hi, lo = max(la, lb), min(la, lb)
            return (hi + 0.05) / (lo + 0.05)

        def _walk(w, out):
            for c in w.winfo_children():
                out.append(c)
                _walk(c, out)
            return out

        def _tc():
            root.update_idletasks()
            print("STATE mode=%s video_mode=%s kind=%s"
                  % (st.get("mode"), st.get("video_mode"), kind_var.get()), flush=True)
            bad = 0
            for w in _walk(root, []):
                if w.winfo_class() != "Button":
                    continue
                bg = w.cget("bg")
                dis = w.cget("state") == "disabled"
                fg = w.cget("disabledforeground") if dis else w.cget("fg")
                r = _ratio(fg, bg)
                rs = ("%.2f:1" % r) if r else "n/a"
                if r and r < 4.5:
                    bad += 1
                print("BUTTON %-16r %-9s bg=%s fg=%s ratio=%-8s %s"
                      % (w.cget("text"), w.cget("state"), bg, fg, rs,
                         "OK" if (r is None or r >= 4.5) else "LOW"), flush=True)
            print("BUTTONS BELOW 4.5:1 = %d" % bad, flush=True)
            root.destroy()
        root.after(2500, _tc)

    # Tk SWALLOWS exceptions raised inside callbacks: it prints them to stderr (which
    # nobody sees in a windowed .app) and carries on. That is how a NameError in
    # set_ref silently did nothing when a reference was chosen. Surface them.
    def _cb_err(exc, val, tb):
        txt = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(os.path.join(DATA, "forge_lab_errors.log"), "a") as fh:
                fh.write("TK CALLBACK %s\n%s\n" % (time.strftime("%H:%M:%S"), txt))
        except Exception:
            pass
        try:
            runst.configure(text="app error: %s: %s" % (exc.__name__, val),
                            fg=C["rust"])
        except Exception:
            pass
        # ⚠️ This handler is installed LATER than _log_exc, so it REPLACES it. Without this
        # fail-fast the two contradicted each other: _log_exc would have exited the process, but
        # _cb_err had already taken over and merely logged, so a driver exception HUNG until its
        # timeout instead of failing. A hang reads as "slow", which is the worst way to break.
        if _DRIVER:
            print("\n!! TK CALLBACK EXCEPTION - failing the driver instead of hanging:", flush=True)
            traceback.print_exception(exc, val, tb)
            os._exit(3)
    root.report_callback_exception = _cb_err

    if os.environ.get("FORGE_LAB_TESTREFS") == "1":
        # Exercises the multi-reference list end-to-end: caps per mode/variant, the
        # ADD limit, removal, and what the panel actually SHOWS.
        def _tr():
            cand = []
            for rootd, dirs, files in os.walk(os.path.join(os.path.expanduser("~"),
                                                           "forge-lab", "runs")):
                cand += [os.path.join(rootd, f) for f in files if f.lower().endswith(".png")]
            if not cand:
                print("TESTREFS: no candidate image", flush=True)
                root.destroy()
                return
            cand.sort(key=os.path.getmtime, reverse=True)
            one = cand[0]

            def show(tag):
                rows = len(ref_list.winfo_children())
                print("  %-22s cap_label=%-9r rows=%d add=%-8s help=%r"
                      % (tag, ref_count.cget("text"), rows, add_btn.cget("state"),
                         ref_help.cget("text")[:52]), flush=True)

            print("TESTREFS start", flush=True)
            pick_model(model_label("video", video_mode="r2v"))
            show("video/r2v")
            add_ref(one)
            add_ref(one)
            show("after 2 adds")
            for _ in range(12):
                add_ref(one)                     # must stop at the cap
            show("after 14 adds")
            before = len(st.get("refs") or [])
            add_ref(one)
            print("  over-cap add refused: %s (still %d)"
                  % (len(st.get("refs") or []) == before, len(st.get("refs") or [])),
                  flush=True)
            pick_model(model_label("video", video_mode="flf"))
            show("video/flf")
            pick_model(model_label("video", video_mode="t2v"))
            show("video/t2v")
            print("  refs block hidden on t2v: %s" % (not refbox.winfo_ismapped()), flush=True)
            # the image models live under KIND Image
            kind_var.set("Image"); on_kind()
            pick_model("Edit image")
            show("image/edit")
            pick_model("Text to image")
            show("image/t2i")
            print("  refs block hidden on t2i: %s" % (not refbox.winfo_ismapped()), flush=True)
            pick_model("Edit image")
            n0 = len(st.get("refs") or [])
            remove_ref(0)
            print("  remove_ref: %d -> %d" % (n0, len(st.get("refs") or [])), flush=True)
            p = params()
            print("  params ref_paths=%d first=%s" % (len(p.get("ref_paths") or []),
                                                      (p.get("ref_paths") or ["-"])[0].split("/")[-1]),
                  flush=True)
            root.after(400, root.destroy)
        root.after(2500, _tr)

    if os.environ.get("FORGE_LAB_TESTPROMOTE") == "1":
        # End-to-end: promote a REAL video take and confirm a NEW take appears with
        # the target dimensions. Picks the smallest source so it exercises a genuine
        # upscale (a 2K source promoted to 1080p would only be a downscale).
        def _tp():
            cands = []
            for i in read_index():
                files = i.get("images") or []
                if not files:
                    continue
                f = (files[0] or {}).get("file") or ""
                if not f.lower().endswith((".mp4", ".webm")):
                    continue
                p = os.path.join(RUNS, i["id"], f)
                if os.path.exists(p):
                    cands.append((i.get("width") or 99999, i, p))
            cands.sort(key=lambda c: c[0])
            if not cands:
                print("TESTPROMOTE: no video take to promote", flush=True)
                root.destroy()
                return
            _w, it, p = cands[0]
            before_ids = {i["id"] for i in read_index()}
            before = len(before_ids)
            print("TESTPROMOTE source: %s  %sx%s  (%d takes in index)"
                  % (os.path.basename(p), it.get("width"), it.get("height"), before),
                  flush=True)
            promote_take(p, "1080p 1920×1088")

            def check(n=0):
                idx = read_index()
                # ONLY the entry this test created — matching any promote entry picks
                # up stale ones from earlier runs and reports a false pass
                hit = [i for i in idx if i.get("id") not in before_ids
                       and i.get("promote") and i.get("status") == "success"]
                if hit:
                    h = hit[0]
                    print("TESTPROMOTE result: index %d -> %d  label=%r  size=%sx%s  "
                          "files=%s  source=%s"
                          % (before, len(idx), h.get("label"), h.get("width"),
                             h.get("height"),
                             [f.get("file") for f in (h.get("images") or [])],
                             h.get("source")), flush=True)
                    root.destroy()
                    return
                if n > 72:                      # ~6 minutes
                    print("TESTPROMOTE: TIMEOUT after %ds; index=%d entries"
                          % (n * 5, len(idx)), flush=True)
                    root.destroy()
                    return
                root.after(5000, lambda: check(n + 1))
            root.after(5000, check)
        root.after(2500, _tp)

    if os.environ.get("FORGE_LAB_TESTDELETE") == "1":
        # Uses a SYNTHETIC take so the test never touches real work, and asserts the
        # safety property that matters: every OTHER take is left untouched.
        def _td():
            tid = "testdelete-0000-0000-0000-000000000000"
            d = os.path.join(RUNS, tid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "synthetic.png"), "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            _index_insert({"id": tid, "mode": "t2i", "label": "SYNTHETIC DELETE TEST",
                           "prompt": "test", "seed": 1, "steps": 1,
                           "started": time.time(), "status": "success",
                           "images": [{"file": "synthetic.png", "node": "x", "bytes": 72}]})
            others = sorted(i["id"] for i in read_index() if i.get("id") != tid)
            print("TESTDELETE before: dir_exists=%s entry_present=%s other_takes=%d"
                  % (os.path.isdir(d), any(i.get("id") == tid for i in read_index()),
                     len(others)), flush=True)
            ok = _do_delete({"id": tid, "label": "SYNTHETIC DELETE TEST",
                             "images": [{"file": "synthetic.png", "bytes": 72}]})
            idx = read_index()
            after_others = sorted(i["id"] for i in idx if i.get("id") != tid)
            print("TESTDELETE after: ok=%s dir_exists=%s entry_present=%s takes=%d "
                  "others_unchanged=%s"
                  % (ok, os.path.isdir(d), any(i.get("id") == tid for i in idx),
                     len(idx), after_others == others), flush=True)
            root.destroy()
        root.after(2500, _td)

    if os.environ.get("FORGE_LAB_TESTPROMOTEIMG") == "1":
        # Image promote end-to-end: smallest real image take -> 2x.
        def _ti():
            cands = []
            for i in read_index():
                files = i.get("images") or []
                if not files or i.get("promote"):
                    continue
                f = (files[0] or {}).get("file") or ""
                if not f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    continue
                p = os.path.join(RUNS, i["id"], f)
                if os.path.exists(p):
                    cands.append((os.path.getsize(p), i, p))
            cands.sort()
            if not cands:
                print("TESTPROMOTEIMG: no image take found", flush=True)
                root.destroy()
                return
            _s, it, p = cands[0]
            before_ids = {i["id"] for i in read_index()}
            wh = image_size(p)
            print("TESTPROMOTEIMG source: %s  %s  (%s bytes)"
                  % (os.path.basename(p), ("%dx%d" % wh) if wh else "size unknown", _s),
                  flush=True)
            promote_take(p, "2x")

            def check(n=0):
                hit = [i for i in read_index() if i.get("id") not in before_ids
                       and i.get("promote") and i.get("status") == "success"]
                if hit:
                    h = hit[0]
                    print("TESTPROMOTEIMG result: label=%r size=%sx%s files=%s status=%s"
                          % (h.get("label"), h.get("width"), h.get("height"),
                             [f.get("file") for f in (h.get("images") or [])],
                             h.get("status")), flush=True)
                    root.destroy()
                    return
                if n > 60:
                    print("TESTPROMOTEIMG: TIMEOUT after %ds" % (n * 5), flush=True)
                    root.destroy()
                    return
                root.after(5000, lambda: check(n + 1))
            root.after(5000, check)
        root.after(2500, _ti)

    if os.environ.get("FORGE_LAB_TESTTAKES") == "1":
        # The failure was width-dependent: the buttons existed but sat outside the
        # visible canvas. So test at several window widths, including narrow ones.
        def _dump_widths(tag):
            root.update_idletasks()
            cw = tcanvas.winfo_width()
            print("[%s] window=%dx%d  tcanvas=%dpx  rows=%d"
                  % (tag, root.winfo_width(), root.winfo_height(), cw, len(take_rows)),
                  flush=True)
            off = 0
            for i, (lbl, btns) in enumerate(take_rows[:5]):
                rx = btns.winfo_rootx() - tinner.winfo_rootx()
                rw = btns.winfo_width()
                right = rx + rw
                if right > cw + 1:
                    off += 1
                print("    row %d  buttons x=%d w=%d right=%d  INSIDE=%s  "
                      "(reqwidth=%d, text wrap=%s)"
                      % (i, rx, rw, right, right <= cw + 1, btns.winfo_reqwidth(),
                         lbl.cget("wraplength")), flush=True)
            print("    buttons off-screen at this width: %d" % off, flush=True)

        def _tt():
            load_takes()
            _dump_widths("default")
            seq = [900, 1100, 1600]

            def step():
                if not seq:
                    root.after(200, root.destroy)
                    return
                wpx = seq.pop(0)
                root.geometry("%dx740" % wpx)
                root.update()            # let the Configure events actually land
                _dump_widths("width=%d" % wpx)
                root.after(300, step)
            root.after(400, step)
        root.after(4000, _tt)

    if os.environ.get("FORGE_LAB_TESTTAKES2") == "1":
        # GPU-free end-to-end for the TAKES upgrade: synthetic index in FORGE_LAB_DATA, rate a
        # take, walk every filter, select two takes, open COMPARE (decoding REAL tiny clips with
        # the app's own ffmpeg command), press PLAY, close. Run with FORGE_LAB_DATA=<dir>
        # FORGE_LAB_NOAUTOSAVE=1 so Ryan's real index is never touched.
        import subprocess as _sp2
        fails2 = []

        def ck2(cond, label):
            print("  %-4s %s" % ("ok" if cond else "FAIL", label), flush=True)
            if not cond:
                fails2.append(label)

        def _pump(sec=1.2):
            t0 = time.time()
            while time.time() - t0 < sec:
                root.update()
                time.sleep(0.03)

        def _setup2():
            picks = [("syntake-a", "video"), ("syntake-b", "video"),
                     ("syntake-c", "image"), ("syntake-d", "image")]
            ents = []
            for n2, (pid, kind) in enumerate(picks):
                d2 = os.path.join(RUNS, pid)
                os.makedirs(d2, exist_ok=True)
                if kind == "video":
                    fn = "clip.mp4"
                    _sp2.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                              "-i", "testsrc=size=320x240:rate=12", "-t", "2",
                              os.path.join(d2, fn)], capture_output=True, timeout=120)
                else:
                    fn = "still.png"
                    _sp2.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                              "-i", "testsrc=size=320x240", "-frames:v", "1",
                              os.path.join(d2, fn)], capture_output=True, timeout=120)
                ents.append({"id": pid, "mode": "video" if kind == "video" else "t2i",
                             "label": "SYNTAKE %s" % pid[-1].upper(),
                             "prompt": "synthetic", "seed": 100 + n2, "steps": 12,
                             "width": 320, "height": 240, "video": kind == "video",
                             "started": time.time(), "status": "success",
                             "images": [{"file": fn, "node": "x", "bytes": 1}]})
            json.dump(ents, open(os.path.join(RUNS, "index.json"), "w"), indent=1)
            return ents

        def _run2():
            _setup2()
            ck2(len(read_index()) == 4, "synthetic index has 4 entries")
            load_takes()
            _pump()
            ck2(len(take_rows) == 4, "panel draws 4 cards unfiltered (got %d)" % len(take_rows))
            # ---- set_rating round-trip ----
            ck2(set_rating("syntake-a", 1) == 1, "set_rating(★) returns 1")
            ck2(next(i for i in read_index() if i["id"] == "syntake-a").get("rating") == 1,
                "★ rating lands in index.json")
            ck2(set_rating("syntake-a", -1) == -1, "set_rating(✕) returns -1")
            ck2(set_rating("syntake-b", 1) == 1, "second take starred")
            # ---- filters ----
            takes_filter_var.set("★ only"); _refilter_takes(); _pump(0.6)
            ck2(len(take_rows) == 1, "★ only shows 1 (got %d)" % len(take_rows))
            takes_filter_var.set("hide ✕"); _refilter_takes(); _pump(0.6)
            ck2(len(take_rows) == 3, "hide ✕ drops the rejected take (got %d)" % len(take_rows))
            takes_filter_var.set("unrated"); _refilter_takes(); _pump(0.6)
            ck2(len(take_rows) == 2, "unrated shows the 2 unrated (got %d)" % len(take_rows))
            takes_filter_var.set("All"); st["take_unkept"] = True
            _refilter_takes(); _pump(0.6)
            ck2(len(take_rows) == 3, "unkept only hides the one ★ take (got %d)" % len(take_rows))
            st["take_unkept"] = False; takes_filter_var.set("All")
            _refilter_takes(); _pump(0.6)
            ck2(len(take_rows) == 4, "back to All shows 4 (got %d)" % len(take_rows))
            # ---- select + COMPARE ----
            a = next(i for i in read_index() if i["id"] == "syntake-a")
            b = next(i for i in read_index() if i["id"] == "syntake-b")
            probe = tk.Button(tinner, text="probe")
            _cmp_toggle(a, probe); _cmp_toggle(b, probe)
            ck2(list(st.get("cmp_sel") or []) == ["syntake-a", "syntake-b"], "two takes selected")
            ck2(str(cmp_btn.cget("state")) == "normal", "COMPARE enabled at 2 selected")
            cwin = open_compare_window()
            ck2(cwin is not None and bool(cwin.winfo_exists()), "COMPARE window opened")
            root.update()
            pls = getattr(cwin, "cmp_players", [])
            ck2(len(pls) == 2, "COMPARE has 2 player columns (got %d)" % len(pls))
            ck2(all(len(p["frames"]) > 0 for p in pls),
                "both players loaded frames (%s)" % [len(p["frames"]) for p in pls])
            ck2(getattr(cwin, "cmp_play_btn", None) is not None and
                str(cwin.cmp_play_btn.cget("text")) == "PLAY", "PLAY starts as PLAY")
            # ---- PLAY ----
            cwin.cmp_toggle_play()
            ck2(str(cwin.cmp_play_btn.cget("text")) == "PAUSE", "PLAY flips to PAUSE")
            i0 = cwin.cmp_players_state["i"]
            _pump(0.6)
            ck2(cwin.cmp_players_state["i"] != i0 or cwin.cmp_players_state["max"] <= 1,
                "frame index advances while playing (i %d -> %d, max %d)"
                % (i0, cwin.cmp_players_state["i"], cwin.cmp_players_state["max"]))
            cwin.cmp_toggle_play()
            ck2(str(cwin.cmp_play_btn.cget("text")) == "PLAY", "PAUSE stops the cycler")
            cwin.destroy()
            root.update()
            ck2(not cwin.winfo_exists(), "COMPARE closes cleanly")
            print("TESTTAKES2: %d failed" % len(fails2), flush=True)
            for fl2 in fails2:
                print("   FAILED: %s" % fl2, flush=True)
            root.destroy()

        root.after(1500, _run2)

    _btn_colors(root)            # dark text, light fill: readable whatever macOS does

    root.mainloop()


# ------------------------------------------------------------------ selftest
def selftest():
    print("resolve forge…", end=" ")
    print(resolve_forge(force=True), "(reachable)" if _ok else "(NOT reachable)")
    if not _ok:
        return 1
    # make a small test image with the stdlib, so the edit path is exercised for real
    import zlib

    def write_png(path, w, h, rgb):
        raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

        def chunk(t, d):
            c = t + d
            return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
        open(path, "wb").write(b"\x89PNG\r\n\x1a\n" +
                               chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                               chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    tpath = os.path.join(DATA, "_selftest.png")
    write_png(tpath, 256, 256, (200, 120, 60))
    print("upload…", end=" ")
    name = upload_image(tpath)
    print(name)
    p = {"prompt": "Add a soft blue rim light to the left edge of <image1>.",
         "resolution": 1024, "steps": 25, "seed": -1, "prefix": "forge_lab_selftest",
         "width": 1024, "height": 1024}
    print("estimate %.1fs" % estimate_seconds("edit", p, tpath))
    pid, seed = submit("edit", p, tpath)
    print("submitted", pid, "seed", seed)
    t0 = time.time()
    while time.time() - t0 < 420:
        time.sleep(4)
        s = run_state(pid)
        pr = PROGRESS.get(pid) or {}
        print("  %s  %.0fs  %s/%s" % (s["state"], time.time() - t0,
                                      pr.get("value"), pr.get("max")), end="\r")
        if s["state"] in ("done", "error", "gone"):
            print()
            break
    meta = collect(pid)
    print(json.dumps(meta, indent=1)[:800] if meta else "no meta")
    return 0 if meta and meta.get("images") else 2


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    launch()