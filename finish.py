"""FINISH — a last pass on a finished video, run on this Mac with ffmpeg (roadmap
2026-10-18, item 2). The cut is already 1080 wide; this is about how it FEELS:

    "sharper" : contrast-adaptive sharpening (cas; unsharp when this ffmpeg has no cas)
                — the H3 clips are upscaled about 2x into the cut and read a little soft
    "grain"   : a light, moving film grain — hides banding and the "too clean AI" look
    "smooth"  : motion interpolation to 48 fps (minterpolate) — smoother camera moves, but
                SLOW (several times the video's length) and it can smear fast hands

    finish(src, out, looks=("sharper",), progress=None) -> out

The original is never touched; the finished copy sits beside it (<name>_finished.mp4).
An AI upscaler on the rig (a model run in ComfyUI) is NOT here: which one is worth it needs
side-by-side renders judged by eye first.
"""
import os
import subprocess

import studio_pipeline as sp

LOOKS = (("sharper", "Sharper"), ("grain", "Film grain"),
         ("smooth", "Smooth motion (48 fps, slow)"))


def filters(looks, has=None):
    """The -vf chain for `looks` (in a fixed order: smooth, then sharpen, then grain)."""
    has = has or sp.ffmpeg_has
    looks = set(looks or ())
    vf = []
    if "smooth" in looks:
        vf.append("minterpolate=fps=48:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    if "sharper" in looks:
        vf.append("cas=0.45" if has("cas") else "unsharp=5:5:0.7:5:5:0.0")
    if "grain" in looks:
        vf.append("noise=c0s=7:c0f=t+u")
    return ",".join(vf)


def out_path(src):
    base, ext = os.path.splitext(src)
    return base + "_finished" + (ext or ".mp4")


def finish(src, out=None, looks=("sharper",), progress=None, timeout=3600):
    if not src or not os.path.exists(src):
        raise ValueError("there's no video to finish")
    vf = filters(looks)
    if not vf:
        raise ValueError("pick at least one: sharper, film grain or smooth motion")
    out = out or out_path(src)
    if progress:
        progress("finishing (%s)" % ", ".join(dict(LOOKS)[k].split(" (")[0].lower()
                                             for k, _n in LOOKS if k in set(looks)))
    tmp = out + ".part.mp4"
    subprocess.run([sp.FFMPEG, "-v", "error", "-y", "-i", src, "-vf", vf, "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "18", "-c:a", "copy",
                    "-movflags", "+faststart", tmp], check=True, capture_output=True,
                   timeout=timeout)
    os.replace(tmp, out)
    return out
