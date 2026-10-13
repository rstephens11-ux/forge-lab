#!/usr/bin/env python3
"""finish_seamless.py — the post pass for a ONE-TAKE piece.

Four things, and the reason for each:

  1. GRADE   — one look applied to the WHOLE piece, so nothing about the image shifts part-way
               through. A single unified grade is most of what makes a clip read as "shot"
               rather than "generated".
  2. GRAIN   — temporal film grain over everything. It hides the tell-tale smoothness of a
               diffusion render and gives the eye a consistent texture to hold on to.
  3. CARD    — the catchphrase as on-screen text (these characters are silent by design, so the
               catchphrase is the only dialogue there is). Fades in, never hard-pops.
  4. BED     — a music bed under H3's own ambience. H3 still generates the room tone, so it is
               kept low (it sells "real location") and the bed sits on top of it. ONE continuous
               track for the whole piece, which is the thing a stitched 6-shot piece cannot have.

    finish_seamless.py <in.mp4> <out.mp4> --catchphrase "..." [--bed bed.mp3] [--card-at 10.5]

⚠️ Both audio sources are mixed and then loudnorm'd ONCE, at the end — normalising the pieces
separately is what produced the 9 dB swings across the old stitched batch.
"""
import argparse
import os
import shutil
import subprocess
import sys

W, H, FPS = 1080, 1920, 24
FONT = "Arial"


def write_card_ass(path, catchphrase, start, end, w=W, h=H):
    """One styled card event. Same house style as the word captions, one notch bigger."""
    def t(s):
        s = max(0.0, float(s))
        return "%d:%02d:%05.2f" % (int(s // 3600), int((s % 3600) // 60), s % 60)

    txt = (catchphrase or "").replace("\n", r"\N")
    lines = [
        "[Script Info]", "ScriptType: v4.00+",
        "PlayResX: %d" % w, "PlayResY: %d" % h, "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Card,%s,88,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,"
        "0,0,1,5,3,2,80,80,300,1" % FONT,
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,%s,%s,Card,,0,0,0,,{\\fad(220,420)}%s" % (t(start), t(end), txt),
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def build_cmd(in_mp4, out_mp4, ass_path, bed, seconds, ambience=0.42, bed_level=0.5,
              look="film"):
    """The filter graph. Video: upscale -> grade -> grain -> vignette -> card. Audio: mix once.

    `look` picks the grade profile, and they are genuinely different jobs:
      film  — cinematic: contrast curve, muted colour, film grain, vignette. For a "shot on
              35mm" piece.
      phone — vertical iPhone footage shot by a bystander: MILD contrast, slightly lifted and
              slightly cool, mild DIGITAL noise (not film grain), a touch of extra sharpening
              (phones over-sharpen), and NO vignette. The imperfections are the point — they
              are what a diffusion render does not naturally produce.
    """
    v = "scale=%d:%d:flags=lanczos,setsar=1," % (W, H)
    if look == "phone":
        # ⚠️ A grade cannot turn a film-flavoured render into phone footage — the look has to
        # come from the H3 prompt. What post CAN do is make it read like a phone rather than a
        # LUT: phones are PUNCHY (more saturation and contrast, not less), they crush blacks
        # slightly instead of lifting them, and they over-sharpen. The old muted/lifted curve
        # here read as "cinematic filter", which is the opposite of the brief.
        v += ("eq=contrast=1.06:saturation=1.07:gamma=1.0:brightness=0.004,"
              "curves=r='0/0 0.5/0.52 1/1.0':g='0/0 0.5/0.52 1/1.0':b='0/0.006 0.5/0.52 1/1.0',"
              "unsharp=5:5:0.5:5:5:0.0,noise=alls=6:allf=t")
    else:
        v += ("eq=contrast=1.06:saturation=0.95:gamma=1.015:brightness=-0.005,"
              "curves=r='0/0 0.25/0.235 0.5/0.515 0.75/0.775 1/0.985':"
              "g='0/0 0.25/0.24 0.5/0.52 0.75/0.78 1/0.985':"
              "b='0/0.012 0.25/0.245 0.5/0.515 0.75/0.765 1/0.975',"
              "noise=alls=5:allf=t,vignette=PI/6")
    if ass_path:
        v += ",ass=%s" % _esc(ass_path)
    fc = "[0:v]%s[v]" % v

    if bed:
        fc += (";[1:a]atrim=0:%s,asetpts=N/SR/TB,volume=%s,"
               "afade=t=out:st=%.2f:d=1.8[bed]"
               % (seconds, bed_level, max(0.0, seconds - 1.8)))
        fc += (";[0:a]volume=%s[amb];[bed][amb]amix=inputs=2:duration=first:"
               "dropout_transition=0:normalize=0,"
               "loudnorm=I=-14:TP=-1.5:LRA=11[a]" % ambience)
    else:
        fc += ";[0:a]volume=0.85,loudnorm=I=-14:TP=-1.5:LRA=11[a]"

    cmd = ["ffmpeg", "-v", "error", "-y", "-i", in_mp4]
    if bed:
        cmd += ["-i", bed]
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
            "-r", str(FPS), "-c:v", "libx264", "-preset", "slow",
            # ⚠️ The phone look adds temporal noise + sharpening, which the encoder pays for
            # dearly (42 MB at crf 18 for 15 s, vs 18 MB for the film look). crf 21 lands a
            # 15 s phone clip near a real iPhone file.
            "-crf", "21" if look == "phone" else "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-t", "%.3f" % seconds, out_mp4]
    return cmd


def _esc(p):
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def duration(path):
    try:
        o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True).stdout
        return float(o.strip())
    except Exception:
        return 0.0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--catchphrase", default="")
    ap.add_argument("--bed", default="")
    ap.add_argument("--card-at", type=float, default=None,
                    help="when the card appears (default: last 4.5 s)")
    ap.add_argument("--card-for", type=float, default=4.5)
    ap.add_argument("--look", default="film", choices=("film", "phone"),
                    help="film = cinematic 35mm grade; phone = handheld iPhone footage")
    ap.add_argument("--keep-ass", default="")
    a = ap.parse_args(argv)

    secs = duration(a.src)
    if not secs:
        raise SystemExit("cannot read duration of %s" % a.src)
    print("source %.2f s" % secs)

    ass = a.keep_ass or os.path.join(os.path.dirname(os.path.abspath(a.out)), ".card.ass")
    if a.catchphrase:
        at = a.card_at if a.card_at is not None else max(0.0, secs - a.card_for)
        write_card_ass(ass, a.catchphrase, at, min(secs - 0.05, at + a.card_for))
        print("card: %r  %.2f -> %.2f s" % (a.catchphrase, at, at + a.card_for))
    else:
        ass = ""

    bed = a.bed if (a.bed and os.path.exists(a.bed)) else ""
    if a.bed and not bed:
        print("!! bed not found: %s — finishing without it" % a.bed)

    cmd = build_cmd(a.src, a.out, ass, bed, secs, look=a.look)
    print("look: %s" % a.look)
    print("running ffmpeg…")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2500:])
        raise SystemExit("ffmpeg failed (%d)" % r.returncode)
    if ass and not a.keep_ass:
        try:
            os.remove(ass)
        except OSError:
            pass
    print("wrote %s  (%.2f s, %.1f MB)" % (a.out, duration(a.out),
                                           os.path.getsize(a.out) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())