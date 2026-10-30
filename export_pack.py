"""EXPORT PACK — one click on a finished video -> everything you need to post it, in one
folder (roadmap 2026-10-18, item 8: "tall, square and wide versions, a cover image with the
hook text, a subtitle file and the post caption"). Tk-free.

    export(ep_slug, aspects=("9:16", "1:1", "16:9"), progress=None) -> {
        "dir", "videos": {aspect: mp4}, "cover", "cover_hook", "srt", "caption", "errors"}

How each shape is made:
  * the video's own shape: the finished cut, copied.
  * another shape: the SAME shots cut again by studio_pipeline.assemble at that size, so the
    captions are laid out for the new frame instead of being cropped or shrunk.
      - square from tall or wide: a crop aimed at the speaker, a little above centre (faces
        sit high).
      - the opposite orientation (tall <-> wide): the whole shot over a blurred copy of
        itself; cropping a tall clip to wide would keep a sliver of it.
    The re-cut works in <project>/export/<shape>/ and never touches the project itself.
  * captions.srt: every caption phrase with its time in the finished cut (for YouTube /
    uploads that want a subtitle file instead of burned-in words).
  * cover.png: a frame; cover_hook.png: the same frame with the hook text on it.
  * caption.txt: the post copy (title, caption, hashtags incl. #aigenerated).
"""
import copy
import json
import os
import shutil
import subprocess
import time

import studio_pipeline as sp

TAG = {"9:16": "tall", "1:1": "square", "16:9": "wide"}
FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts",
                    "Inter-Black.otf")


def _orient(aspect):
    w, h = sp.OUT_SIZE.get(aspect, sp.OUT_SIZE["9:16"])
    return "square" if w == h else ("tall" if h > w else "wide")


def recut_plan(src_aspect, aspect):
    """(fit, crop_y) for cutting a `src_aspect` project at `aspect`."""
    a, b = _orient(src_aspect), _orient(aspect)
    if b == "square" or a == b or a == "square":
        return "fill", 0.38
    return "blur", 0.5


def _safe(name):
    out = "".join(c if c.isalnum() or c in "-_ " else "" for c in (name or "")).strip()
    return out.replace(" ", "-")[:48] or "video"


def _ts(t):
    t = max(0.0, float(t))
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60,
                                    ms % 1000)


def srt_text(pj):
    """The cut's captions as SRT: the same phrases build_ass burns in, timed in the cut
    (needs the shots' start / cut_in / cut_out from the last assemble)."""
    w, h = sp.OUT_SIZE.get(pj.get("aspect", "9:16"), sp.OUT_SIZE["9:16"])
    cap = sp.caption_chars(w, h)[0]
    events = []
    for sh in pj.get("shots") or []:
        if sh.get("is_reaction") or sh.get("skip") or sh.get("start") is None:
            continue
        start = float(sh.get("start") or 0.0)
        lo, hi = sh.get("cut_in"), sh.get("cut_out")
        for g in sp._group_words(sp._shot_words(sh), cap=cap):
            t0 = start + float(g[0].get("start") or 0.0)
            t1 = start + float(g[-1].get("end") or g[-1].get("start") or 0.0)
            if lo is not None:
                t0 = max(t0, float(lo))
            if hi is not None:
                t1 = min(t1, float(hi))
            if t1 - t0 < 0.2:
                t1 = t0 + 0.4
            txt = " ".join((w_.get("word") or "").strip() for w_ in g).strip()
            if txt:
                events.append((t0, t1, txt))
    events.sort()
    for i in range(len(events) - 1):                 # never overlap
        if events[i][1] > events[i + 1][0]:
            events[i] = (events[i][0], max(events[i][0] + 0.2, events[i + 1][0]),
                         events[i][2])
    return "".join("%d\n%s --> %s\n%s\n\n" % (i + 1, _ts(a), _ts(b), t)
                   for i, (a, b, t) in enumerate(events))


def cover_with_hook(cover, hook, out, width=None):
    """The cover frame with the hook text across it (white on a dark band, Inter Black)."""
    if not (cover and os.path.exists(cover)):
        return None
    hook = (hook or "").strip()
    if not hook:
        shutil.copyfile(cover, out)
        return out
    words, lines, cur = hook.split(), [], ""
    per = 18
    for w_ in words:
        if cur and len(cur) + 1 + len(w_) > per:
            lines.append(cur)
            cur = w_
        else:
            cur = (cur + " " + w_).strip()
    if cur:
        lines.append(cur)
    txt = os.path.splitext(out)[0] + "_hook.txt"
    with open(txt, "w") as f:
        f.write("\n".join(lines[:4]).upper())
    font = ("fontfile='%s':" % FONT.replace("'", "\\'")) if os.path.exists(FONT) else ""
    try:
        wh = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=width,height", "-of", "csv=p=0", cover],
                            capture_output=True, text=True, timeout=30).stdout.strip()
        cw, ch = (int(x) for x in wh.split(","))
    except Exception:
        cw, ch = 1080, 1920
    fs = int(min(ch * 0.045, cw * 0.075))
    vf = ("drawbox=x=0:y=ih*0.10:w=iw:h=ih*0.22:color=black@0.55:t=fill,"
          "drawtext=%stextfile='%s':fontcolor=white:fontsize=%d:line_spacing=%d:"
          "x=(w-text_w)/2:y=h*0.21-text_h/2:borderw=3:bordercolor=black"
          % (font, txt.replace("'", "\\'"), fs, int(fs * 0.25)))
    sp._run([sp.FFMPEG, "-v", "error", "-y", "-i", cover, "-vf", vf, "-frames:v", "1", out],
            timeout=120)
    try:
        os.remove(txt)
    except OSError:
        pass
    return out if os.path.exists(out) else None


def export(ep_slug, aspects=("9:16", "1:1", "16:9"), out_root=None, progress=None,
           title=None, post_text=None, voice_engine=None, own_video=None):
    """Build the export pack for a finished Episode project. Shapes that fail are listed in
    "errors"; the rest are still written."""
    say = progress or (lambda _m: None)
    pj = sp.load_project(ep_slug)
    if not pj or not (pj.get("cut") or {}).get("mp4") or \
            not os.path.exists(pj["cut"]["mp4"]):
        raise ValueError("cut the video first — there's no finished video to export")
    src_aspect = pj.get("aspect", "9:16")
    name = _safe(title or pj.get("title") or ep_slug)
    root = out_root or os.path.join(os.path.dirname(sp.studio_root()), "exports")
    d = os.path.join(root, "%s-%s" % (name, time.strftime("%Y%m%d-%H%M")))
    n, dd = 2, d
    while os.path.exists(dd):
        dd, n = "%s-%d" % (d, n), n + 1
    d = dd
    os.makedirs(d)
    res = {"dir": d, "videos": {}, "cover": None, "cover_hook": None, "srt": None,
           "caption": None, "licence": None, "errors": {}}
    for asp in aspects:
        dst = os.path.join(d, "%s_%s.mp4" % (name, TAG.get(asp, asp.replace(":", "x"))))
        try:
            if asp == src_aspect:              # (the finished copy when there is one)
                shutil.copyfile(own_video if own_video and os.path.exists(own_video)
                                else pj["cut"]["mp4"], dst)
            else:
                say("cutting the %s version" % TAG.get(asp, asp))
                cp = copy.deepcopy(pj)
                cp["slug"] = os.path.join(ep_slug, "export", TAG.get(asp, "x"))
                cp["aspect"] = asp
                cp["fit"], cp["crop_y"] = recut_plan(src_aspect, asp)
                cp["ai_label"] = pj.get("ai_label", True)
                # assemble names its output <project dir>/<slug>_final.mp4: with a nested
                # slug that file sits one folder deeper, which must exist
                os.makedirs(os.path.join(sp.project_dir(cp["slug"]),
                                         os.path.dirname(cp["slug"])), exist_ok=True)
                cut = _assemble_quietly(cp)
                shutil.copyfile(cut["mp4"], dst)
            res["videos"][asp] = dst
        except Exception as exc:
            res["errors"][asp] = "%s" % exc
    say("cover, subtitles and caption")
    # the cut's own cover.png is a frame of the FINISHED video, captions and hook burned in;
    # the pack's cover is a clean frame (from the cut before captions) + one with the hook
    cov = pj["cut"].get("cover")
    clean = os.path.join(sp.project_dir(ep_slug), "cut", "concat.mp4")
    if os.path.exists(clean):
        try:
            sp._cover(clean, os.path.join(d, "_clean.png"), at=1.0)
            if os.path.exists(os.path.join(d, "_clean.png")):
                cov = os.path.join(d, "_clean.png")
        except Exception:
            pass
    if cov and os.path.exists(cov):
        res["cover"] = os.path.join(d, "cover.png")
        shutil.copyfile(cov, res["cover"])     # (copy first: cov may be the temp frame)
        try:
            res["cover_hook"] = cover_with_hook(cov, pj.get("hook_text"),
                                                os.path.join(d, "cover_hook.png"))
        except Exception as exc:
            res["errors"]["cover"] = "%s" % exc
        if cov.endswith("_clean.png"):
            os.remove(cov)
    srt = srt_text(pj)
    if srt:
        res["srt"] = os.path.join(d, "%s.srt" % name)
        with open(res["srt"], "w") as f:
            f.write(srt)
    post = post_text
    if post is None and pj["cut"].get("post") and os.path.exists(pj["cut"]["post"]):
        post = open(pj["cut"]["post"]).read()
    if post:
        res["caption"] = os.path.join(d, "caption.txt")
        with open(res["caption"], "w") as f:
            f.write(post)
    try:                                   # what it was made with, and whether that's paid-OK
        import licences
        res["licence"] = os.path.join(d, "licence.txt")
        with open(res["licence"], "w") as f:
            f.write(licences.note(voice_engine))
    except Exception:
        res["licence"] = None
    with open(os.path.join(d, "pack.json"), "w") as f:
        json.dump(dict(res, project=ep_slug, made=time.time()), f, indent=1)
    return res


def _assemble_quietly(cp):
    """assemble() on a throw-away copy: it writes into <project>/export/<shape>/ and saves
    THAT copy's project.json there, never the real project. The post copy is the real
    project's (no second writer call)."""
    keep = sp.get_director
    sp.get_director = _no_director
    try:
        return sp.assemble(cp)
    finally:
        sp.get_director = keep


def _no_director():
    raise RuntimeError("export: post copy comes from the original cut")
