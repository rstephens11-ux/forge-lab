#!/usr/bin/env python3
"""episode_cut.py - assemble the finished YOUNG AVERY pilot episode from rendered
H3 shot clips, title cards, a text overlay and a voice-over.

Usage:
    episode_cut.py SHOTS.json CLIPDIR OUT.mp4 [--allow-missing]

This machine's ffmpeg has NO drawtext and NO libass, so every piece of text is
rendered to a PNG with Pillow and composited with ffmpeg 'overlay'.

Optional self-test flags:
    --test-overlay CLIP [--test-overlay-out OUT]   render the 10_3 chat-bubble
                                                   overlay onto an existing clip
    --test-vo A B OUT                              render the line-32 voice-over
                                                   mix over two existing clips

Nothing here talks to the GPU server; it only consumes clips that already exist.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- constants ---
W, H = 1344, 768                # full-quality output frame
FPS = 24
CRF = 18
AUDIO_BR = "192k"
AUDIO_SR = 48000
LOUDNESS_I = -16                # target integrated loudness (LUFS)

TITLE_BIG = "YOUNG AVERY"
TITLE_SUB = "I Cannot Tell a Lyft"          # episode title, verified in yg-101-pilot.md
CARD_AFTER = "8_6"                          # insert the time-jump card after this shot
CARD_AFTER_TEXT = "THAT WAS THREE WEEKS AGO."   # exact script wording (verified)
CARD_SECS = 3.0
CARD_AFTER_SECS = 2.5
SLATE_SECS = 2.0
MISSING_SLATE_RGB = (96, 96, 96)

VO_SEGMENTS = ("10_1", "10_2")   # V.O. runs across these two shots
VO_START = 0.5                   # V.O. starts 0.5 s into 10_1
VO_LINE_DEFAULT = 32             # Avery's journal V.O. line
VO_EXPECT = "Survey of the Modern World"
AMBIENCE_DUCK = 0.30             # duck clip ambience to 30% under the V.O.

BUBBLE_SHOT = "10_3"             # phone on the windowsill, blank glowing screen
BUBBLE_1 = "yo is the tricorn guy still on our couch."   # exact script wording
BUBBLE_2 = "yeah. no cap."                               # exact script wording
BUBBLE_2_DELAY = 1.5             # second bubble appears ~1.5 s after the first
BUBBLE_GREY = (229, 229, 234, 255)   # incoming message
BUBBLE_GREY_TXT = (24, 24, 27, 255)
BUBBLE_BLUE = (10, 132, 255, 255)    # outgoing message
BUBBLE_BLUE_TXT = (255, 255, 255, 255)

FONT_CANDIDATES_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]
FONT_CANDIDATES_REG = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    raise SystemExit("no usable font found among: %s" % ", ".join(paths))


FONT_BOLD_PATH = _first_existing(FONT_CANDIDATES_BOLD)
FONT_REG_PATH = _first_existing(FONT_CANDIDATES_REG)


def font_bold(size):
    return ImageFont.truetype(FONT_BOLD_PATH, size)


def font_reg(size):
    return ImageFont.truetype(FONT_REG_PATH, size)


# ------------------------------------------------------------- shell helpers ---
def run(cmd, quiet=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write("\nCOMMAND FAILED:\n  %s\n" % " ".join(cmd))
        sys.stderr.write(p.stderr or "")
        raise SystemExit("ffmpeg/ffprobe failed (see above)")
    if not quiet and p.stdout:
        print(p.stdout)
    return p


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True,
    ).stdout
    return json.loads(out or "{}")


def probe_duration(path):
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError):
        for s in info.get("streams", []):
            if s.get("codec_type") == "video" and s.get("duration"):
                return float(s["duration"])
        return 0.0


def has_audio(path):
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def ffmpeg_has_drawtext():
    p = subprocess.run(["ffmpeg", "-filters"], capture_output=True, text=True)
    return "drawtext" in p.stdout


# ---------------------------------------------------------------- PNG makers ---
def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_title_card(path):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    big = font_bold(150)
    sub = font_reg(58)
    d.text((W / 2, H * 0.40), TITLE_BIG, font=big, fill=(255, 255, 255), anchor="mm")
    d.text((W / 2, H * 0.60), TITLE_SUB, font=sub, fill=(200, 200, 200), anchor="mm")
    img.save(path)


def make_card(path, text, size=76):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font_bold(size)
    d.text((W / 2, H / 2), text, font=f, fill=(255, 255, 255), anchor="mm")
    img.save(path)


def make_slate(path, label):
    img = Image.new("RGB", (W, H), MISSING_SLATE_RGB)
    d = ImageDraw.Draw(img)
    f = font_bold(84)
    d.text((W / 2, H / 2), label, font=f, fill=(255, 255, 255), anchor="mm")
    img.save(path)


def _bubble_size(draw, text, font, max_text_w=560, pad_x=34, pad_y=26):
    lines = _wrap(draw, text, font, max_text_w)
    tw = max(draw.textlength(l, font=font) for l in lines)
    lh = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    line_h = int(lh * 1.35)
    bw = int(tw) + 2 * pad_x
    bh = line_h * len(lines) + 2 * pad_y
    return lines, bw, bh, line_h, pad_x, pad_y


def make_bubbles(b1_path, b2_path):
    """Two full-frame transparent PNGs: b1 draws the first bubble (space left
    below for the second), b2 draws ONLY the second bubble in that space."""
    # Pre-compute layout on a scratch canvas so both PNGs agree on positions.
    scratch = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(scratch)
    fs = 46
    fb = font_bold(fs)

    l1, w1, h1, lh1, px1, py1 = _bubble_size(dd, BUBBLE_1, fb)
    l2, w2, h2, lh2, px2, py2 = _bubble_size(dd, BUBBLE_2, fb)

    gap = 18
    total_h = h1 + gap + h2
    top = int(H * 0.50 - total_h / 2)
    cx1 = int(W * 0.50 - w1 / 2)           # first bubble, centered
    cx2 = int(W * 0.62 - w2 / 2)           # second bubble, nudged right (outgoing)

    # --- PNG 1: bubble 1 only
    img1 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d1 = ImageDraw.Draw(img1)
    d1.rounded_rectangle([cx1, top, cx1 + w1, top + h1], radius=26, fill=BUBBLE_GREY)
    y = top + py1
    for line in l1:
        d1.text((cx1 + px1, y), line, font=fb, fill=BUBBLE_GREY_TXT)
        y += lh1
    img1.save(b1_path)

    # --- PNG 2: bubble 2 only, positioned just under bubble 1
    top2 = top + h1 + gap
    img2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(img2)
    d2.rounded_rectangle([cx2, top2, cx2 + w2, top2 + h2], radius=26, fill=BUBBLE_BLUE)
    y = top2 + py2
    for line in l2:
        d2.text((cx2 + px2, y), line, font=fb, fill=BUBBLE_BLUE_TXT)
        y += lh2
    img2.save(b2_path)


# ----------------------------------------------------------- video building ---
def _base_chain(inlab, outlab):
    return (f"[{inlab}]scale={W}:{H}:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={FPS}[{outlab}]")


def _aformat(lab_in, lab_out, extra=""):
    return (f"[{lab_in}]aresample={AUDIO_SR},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo{extra}[{lab_out}]")


def encode_segment(out, inputs, filter_complex, extra=None):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for inp in inputs:
        cmd += inp
    cmd += ["-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", str(CRF), "-preset", "medium",
            "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(FPS),
            "-c:a", "aac", "-b:a", AUDIO_BR, "-ar", str(AUDIO_SR), "-ac", "2"]
    if extra:
        cmd += extra
    cmd += ["-movflags", "+faststart", out]
    run(cmd)
    return out


def seg_plain_clip(src, out):
    """Normalised clip segment, silent audio added if the clip has none."""
    if has_audio(src):
        inputs = [["-i", src]]
        fc = _base_chain("0:v", "vout") + ";" + _aformat("0:a", "aout")
        extra = None
    else:
        inputs = [["-i", src],
                  ["-f", "lavfi", "-i",
                   f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}"]]
        fc = _base_chain("0:v", "vout") + ";" + _aformat("1:a", "aout")
        extra = ["-shortest"]
    return encode_segment(out, inputs, fc, extra)


def seg_image_card(png, out, secs):
    inputs = [["-loop", "1", "-framerate", str(FPS), "-i", png],
              ["-t", str(secs), "-f", "lavfi", "-i",
               f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}"]]
    fc = (f"[0:v]scale={W}:{H},setsar=1,fps={FPS},format=yuv420p[vout];"
          + _aformat("1:a", "aout"))
    return encode_segment(out, inputs, fc, ["-t", str(secs), "-shortest"])


def seg_vo_clip(src, vo_wav, out, dur, vo_ss, vo_to, delay_ms, duck=AMBIENCE_DUCK):
    """Clip with its ambience ducked and a slice of the V.O. laid over it."""
    vo_ss = float(vo_ss)
    vo_to = float(vo_to)
    inputs = [["-i", src], ["-i", vo_wav]]
    src_has_audio = has_audio(src)
    if not src_has_audio:
        inputs.append(["-t", str(dur), "-f", "lavfi", "-i",
                       f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}"])
        amb_in = "2:a"
    else:
        amb_in = "0:a"
    parts = [_base_chain("0:v", "vout")]
    parts.append(_aformat(amb_in, "amb", extra=f",volume={duck}"))
    parts.append(
        f"[1:a]aresample={AUDIO_SR},"
        f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"atrim=start={vo_ss:.3f}:end={vo_to:.3f},asetpts=PTS-STARTPTS,"
        f"adelay={delay_ms}:all=1[vo]")
    parts.append("[amb][vo]amix=inputs=2:duration=first:normalize=0,"
                 "alimiter=limit=0.97[aout]")
    return encode_segment(out, inputs, ";".join(parts), ["-t", f"{dur:.3f}"])


def seg_with_bubbles(src, b1, b2, out, dur):
    inputs = [["-i", src],
              ["-loop", "1", "-framerate", str(FPS), "-i", b1],
              ["-loop", "1", "-framerate", str(FPS), "-i", b2]]
    parts = [_base_chain("0:v", "base"),
             f"[1:v]scale={W}:{H},format=rgba[b1v]",
             f"[2:v]scale={W}:{H},format=rgba[b2v]",
             "[base][b1v]overlay=0:0[o1]",
             f"[o1][b2v]overlay=0:0:enable='gte(t,{BUBBLE_2_DELAY})'[vout]"]
    if has_audio(src):
        parts.append(_aformat("0:a", "aout"))
        extra = ["-t", f"{dur:.3f}"]
    else:
        inputs.append(["-t", f"{dur:.3f}", "-f", "lavfi", "-i",
                       f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}"])
        parts.append(_aformat("3:a", "aout"))
        extra = ["-t", f"{dur:.3f}"]
    return encode_segment(out, inputs, ";".join(parts), extra)


# ------------------------------------------------------------------- V.O. -----
def resolve_vo(project_dir, line_idx):
    pj = os.path.join(project_dir, "project.json")
    with open(pj) as fh:
        proj = json.load(fh)
    lines = proj["lines"]
    match = [l for l in lines if l["idx"] == line_idx]
    if not match:
        raise SystemExit(f"V.O. line idx {line_idx} not found in {pj}")
    line = match[0]
    takes = line.get("takes") or []
    if not takes:
        raise SystemExit(f"line {line_idx} has no takes")
    take = takes[line.get("kept", 0)]
    wav = os.path.join(project_dir, take["file"])
    if not os.path.exists(wav):
        raise SystemExit(f"V.O. wav missing: {wav}")
    text = line.get("text", "")
    print(f"V.O. line {line_idx} ({line.get('speaker')}): \"{text}\"")
    print(f"V.O. take: {wav}")
    if VO_EXPECT.lower() not in text.lower():
        print(f"WARNING: line {line_idx} text does not contain \"{VO_EXPECT}\" - "
              f"check the line index before trusting the V.O.")
    return wav, text


# ------------------------------------------------------------------ concat -----
def concat_and_normalise(segment_paths, workdir, out):
    listfile = os.path.join(workdir, "concat.txt")
    with open(listfile, "w") as fh:
        for p in segment_paths:
            fh.write("file '%s'\n" % os.path.abspath(p).replace("'", "'\\''"))
    joined = os.path.join(workdir, "joined.mp4")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", "-movflags", "+faststart", joined])
    # single-pass loudness normalisation of the whole episode; video is copied
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", joined, "-c:v", "copy",
         "-af", f"loudnorm=I={LOUDNESS_I}:TP=-1.5:LRA=11",
         "-c:a", "aac", "-b:a", AUDIO_BR, "-ar", str(AUDIO_SR), "-ac", "2",
         "-movflags", "+faststart", out])
    return out


# ------------------------------------------------------------------- main ------
def build(shots_path, clipdir, out, allow_missing, project_dir, vo_line,
          workdir, keep_work):
    with open(shots_path) as fh:
        shots = json.load(fh)

    vo_wav, _ = resolve_vo(project_dir, vo_line)
    vo_dur = probe_duration(vo_wav)
    print(f"V.O. duration: {vo_dur:.2f} s")

    # --- figure out which clips exist
    missing = [s["n"] for s in shots
               if not os.path.exists(os.path.join(clipdir, f"shot_{s['n']}.mp4"))]
    if missing and not allow_missing:
        sys.stderr.write("ERROR: %d missing clip(s):\n" % len(missing))
        for n in missing:
            sys.stderr.write(f"  shot_{n}.mp4\n")
        sys.stderr.write("re-run with --allow-missing to substitute 2 s slates.\n")
        raise SystemExit(1)
    if missing:
        print(f"--allow-missing: {len(missing)} slate(s): {', '.join(missing)}")

    # --- plan segments
    plan = [("card", "title", None, CARD_SECS)]
    for s in shots:
        n = s["n"]
        src = os.path.join(clipdir, f"shot_{n}.mp4")
        plan.append(("clip" if os.path.exists(src) else "slate", n, src,
                     s.get("secs") or 0.0))
        if n == CARD_AFTER:
            plan.append(("card", "weeks", None, CARD_AFTER_SECS))
    if not keep_work:
        pass  # workdir already created by caller

    # --- V.O. budget check
    vo_span = sum(clip_dur(plan, n, clipdir) for n in VO_SEGMENTS
                  if os.path.exists(os.path.join(clipdir, f"shot_{n}.mp4")))
    if vo_span:
        need = VO_START + vo_dur
        if need > vo_span + 1e-3:
            print(f"WARNING: V.O. is longer than 10_1+10_2: needs "
                  f"{need:.2f} s (0.5 s lead-in + {vo_dur:.2f} s take) but the "
                  f"two shots total {vo_span:.2f} s; the tail will be cut off.")

    # --- render segments
    png_dir = os.path.join(workdir, "png")
    os.makedirs(png_dir, exist_ok=True)

    title_png = os.path.join(png_dir, "title.png")
    make_title_card(title_png)
    weeks_png = os.path.join(png_dir, "card_weeks.png")
    make_card(weeks_png, CARD_AFTER_TEXT)

    b1_png = os.path.join(png_dir, "bubble1.png")
    b2_png = os.path.join(png_dir, "bubble2.png")
    make_bubbles(b1_png, b2_png)

    # pre-compute the per-shot V.O. split across 10_1 / 10_2
    vo_plan = {}
    if any(os.path.exists(os.path.join(clipdir, f"shot_{n}.mp4"))
           for n in VO_SEGMENTS):
        consumed = 0.0
        for n in VO_SEGMENTS:
            src = os.path.join(clipdir, f"shot_{n}.mp4")
            if not os.path.exists(src):
                continue
            clip_len = probe_duration(src)
            lead = VO_START if consumed == 0.0 else 0.0
            avail = max(0.0, clip_len - lead)
            ss = consumed
            to = min(consumed + avail, vo_dur)
            if to > ss:
                vo_plan[n] = dict(dur=clip_len, ss=ss, to=to,
                                  delay_ms=int(round(lead * 1000)))
                consumed = to
            if consumed >= vo_dur:
                break

    rows = []
    seg_paths = []
    for i, (kind, name, src, secs) in enumerate(plan):
        seg = os.path.join(workdir, f"seg_{i:03d}_{name}.mp4")
        if kind == "card":
            if name == "title":
                seg_image_card(title_png, seg, CARD_SECS)
                rows.append((name, "title card", "-", probe_duration(seg)))
            else:
                seg_image_card(weeks_png, seg, CARD_AFTER_SECS)
                rows.append((name, "card", "-", probe_duration(seg)))
        elif kind == "slate":
            png = os.path.join(png_dir, f"slate_{name}.png")
            make_slate(png, f"MISSING SHOT {name}")
            seg_image_card(png, seg, SLATE_SECS)
            rows.append((name, "SLATE", "-", probe_duration(seg)))
        else:
            if name in vo_plan:
                v = vo_plan[name]
                seg_vo_clip(src, vo_wav, seg, v["dur"], f"{v['ss']:.3f}",
                            f"{v['to']:.3f}", v["delay_ms"])
                rows.append((name, "clip+VO", os.path.basename(src),
                             probe_duration(seg)))
            elif name == BUBBLE_SHOT:
                dur = probe_duration(src)
                seg_with_bubbles(src, b1_png, b2_png, seg, dur)
                rows.append((name, "clip+bubbles", os.path.basename(src),
                             probe_duration(seg)))
            else:
                seg_plain_clip(src, seg)
                rows.append((name, "clip", os.path.basename(src),
                             probe_duration(seg)))
        seg_paths.append(seg)

    concat_and_normalise(seg_paths, workdir, out)

    # --- report
    w0 = max(len(r[0]) for r in rows + [("segment",)])
    print("\n%-*s  %-13s %-26s %8s" % (w0, "segment", "role", "source", "secs"))
    print("-" * (w0 + 54))
    total = 0.0
    for name, role, srcname, dur in rows:
        total += dur
        print("%-*s  %-13s %-26s %8.2f" % (w0, name, role, srcname, dur))
    print("-" * (w0 + 54))
    print("%-*s  %-13s %-26s %8.2f" % (w0, "TOTAL", "", "", total))
    final_dur = probe_duration(out)
    info = probe(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
    print(f"\nwrote {out}")
    print(f"  duration {final_dur:.2f} s")
    print(f"  video {v.get('codec_name')} {v.get('width')}x{v.get('height')} "
          f"{v.get('r_frame_rate')} {v.get('pix_fmt')}")
    print(f"  audio {a.get('codec_name')} {a.get('sample_rate')}Hz "
          f"{a.get('channels')}ch")
    return out


def clip_dur(plan, n, clipdir):
    """Duration of shot n as it will appear (real clip length, else shot secs)."""
    for kind, name, src, secs in plan:
        if name == n:
            if kind == "clip":
                return probe_duration(src)
            return secs
    return 0.0


# --------------------------------------------------------------- self-tests ----
def test_overlay(clip, out, project_dir):
    workdir = tempfile.mkdtemp(prefix="episode_cut_overlay_")
    try:
        b1 = os.path.join(workdir, "bubble1.png")
        b2 = os.path.join(workdir, "bubble2.png")
        make_bubbles(b1, b2)
        dur = probe_duration(clip)
        seg_with_bubbles(clip, b1, b2, out, dur)
        print(f"overlay test: {clip} -> {out} ({dur:.2f}s)")
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_vo(clip_a, clip_b, out, project_dir, vo_line):
    workdir = tempfile.mkdtemp(prefix="episode_cut_vo_")
    try:
        vo_wav, _ = resolve_vo(project_dir, vo_line)
        vo_dur = probe_duration(vo_wav)
        da, db = probe_duration(clip_a), probe_duration(clip_b)
        span = da + db
        print(f"VO test: clips {da:.2f}s + {db:.2f}s = {span:.2f}s, "
              f"V.O. {vo_dur:.2f}s, lead-in {VO_START}s")
        if VO_START + vo_dur > span:
            print(f"WARNING: V.O. longer than the two clips: needs "
                  f"{VO_START + vo_dur:.2f} s, have {span:.2f} s")
        a_out = os.path.join(workdir, "a.mp4")
        b_out = os.path.join(workdir, "b.mp4")
        to_a = min(da - VO_START, vo_dur)
        seg_vo_clip(clip_a, vo_wav, a_out, da, "0.000", f"{to_a:.3f}",
                    int(VO_START * 1000))
        seg_vo_clip(clip_b, vo_wav, b_out, db, f"{to_a:.3f}",
                    f"{min(to_a + (db), vo_dur):.3f}", 0)
        concat_and_normalise([a_out, b_out], workdir, out)
        print(f"VO test written: {out}")
        print(f"  duration {probe_duration(out):.2f} s")
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shots", nargs="?", help="shots.json")
    ap.add_argument("clipdir", nargs="?", help="directory of shot_<n>.mp4 clips")
    ap.add_argument("out", nargs="?", help="output episode mp4")
    ap.add_argument("--allow-missing", action="store_true",
                    help="substitute 2 s grey slates for missing clips")
    ap.add_argument("--project", default=os.path.expanduser(
        "~/forge-lab/dialogue/101-pilot"), help="dialogue project dir")
    ap.add_argument("--vo-line", type=int, default=VO_LINE_DEFAULT)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--test-overlay", metavar="CLIP",
                    help="render the 10_3 bubble overlay onto CLIP and exit")
    ap.add_argument("--test-overlay-out", metavar="OUT")
    ap.add_argument("--test-vo", nargs=3, metavar=("CLIP_A", "CLIP_B", "OUT"),
                    help="render the line-32 V.O. mix over two clips and exit")
    args = ap.parse_args()

    if not ffmpeg_has_drawtext():
        print("note: this ffmpeg has no drawtext filter -> all text is rendered "
              "with Pillow and composited with 'overlay'. (expected)")

    if args.test_overlay:
        out = args.test_overlay_out or os.path.join(
            os.getcwd(), "overlay_test.mp4")
        test_overlay(args.test_overlay, out, args.project)
        return 0
    if args.test_vo:
        test_vo(args.test_vo[0], args.test_vo[1], args.test_vo[2], args.project,
                args.vo_line)
        return 0

    if not (args.shots and args.clipdir and args.out):
        ap.error("SHOTS.json, CLIPDIR and OUT.mp4 are required "
                 "(or use --test-overlay / --test-vo)")

    own_workdir = args.workdir is None
    workdir = args.workdir or tempfile.mkdtemp(prefix="episode_cut_")
    os.makedirs(workdir, exist_ok=True)
    try:
        build(os.path.expanduser(args.shots), os.path.expanduser(args.clipdir),
              os.path.expanduser(args.out), args.allow_missing,
              os.path.expanduser(args.project), args.vo_line, workdir,
              args.keep_work)
    finally:
        if own_workdir and not args.keep_work:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
