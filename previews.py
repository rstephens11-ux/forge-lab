"""PREVIEWS — moving thumbnails for the choices you'd otherwise read as words (roadmap
2026-10-18, item 5: Higgsfield's best idea is a grid of looping previews you click).

Made locally, no GPU, cached in DATA/previews/:
  * camera_move(name)  -> frames of a still doing each "Make it move" camera move (zoompan /
                           crop on the picture you picked, or a sample still)
  * format_loop(fmt)   -> frames from YOUR latest finished Quick video in that format (so a
                           format card shows what you actually made with it); None if none yet
  * caption looks are drawn live in Tk (CaptionLoop), no files

and a Tk helper:
  Loop(label, frames, play="hover"|"always") — cycles the frames on a Label; on hover it plays
  and on leave it rests on the first frame (a grid of 8 loops all playing at once costs CPU).

Frames are small PNGs (w ~ 150 px) so a grid of them stays light.
"""
import hashlib
import os
import subprocess
import threading

import forge_core as c

FFMPEG = "ffmpeg"
N_FRAMES = 18
FPS = 12
W = 150

# zoompan expressions per camera move, on an input already scaled to 2x the output; `on` is
# the output frame number (0..N-1), so every move runs once across the loop
_Z = "(on/%d)" % (N_FRAMES - 1)
CAMERA = {
    "Slow push-in":     ("1+0.22*%s" % _Z, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "Pull back reveal": ("1.30-0.30*%s" % _Z, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "Orbit":            ("1.18", "(iw-iw/zoom)*(0.5+0.45*sin(2*PI*%s))" % _Z,
                         "ih/2-(ih/zoom/2)"),
    "Handheld":         ("1.10", "(iw-iw/zoom)*(0.5+0.06*sin(on*1.7)+0.03*sin(on*4.1))",
                         "(ih-ih/zoom)*(0.5+0.05*sin(on*1.3+1)+0.03*sin(on*3.7))"),
    "Dolly zoom":       ("1+0.30*%s" % _Z, "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*0.42"),
    "Crane up":         ("1.25", "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*(1-%s)" % _Z),
    "Whip pan":         ("1.20", "(iw-iw/zoom)*min(1,max(0,(%s-0.25)*4))" % _Z,
                         "ih/2-(ih/zoom/2)"),
    "Static, alive":    ("1.0", "0", "0"),
}


def _dir(*parts):
    d = os.path.join(c.DATA if hasattr(c, "DATA") else os.path.expanduser("~/forge-lab"),
                     "previews", *parts)
    os.makedirs(d, exist_ok=True)
    return d


def _key(*bits):
    return hashlib.sha1("|".join(str(b) for b in bits).encode()).hexdigest()[:12]


def _frames_in(d):
    try:
        fs = sorted(f for f in os.listdir(d) if f.startswith("f") and f.endswith(".png"))
    except OSError:
        return []
    return [os.path.join(d, f) for f in fs] if len(fs) >= 2 else []


def sample_still():
    """A picture to demonstrate camera moves on: a made-up little scene (so the previews look
    the same for everyone and need nothing on the rig)."""
    out = os.path.join(_dir(), "sample.png")
    if not os.path.exists(out):
        vf = ("drawbox=x=0:y=ih*0.62:w=iw:h=ih*0.38:color=0x3a2f26:t=fill,"
              "drawbox=x=iw*0.10:y=ih*0.18:w=iw*0.16:h=ih*0.44:color=0x6b5a48:t=fill,"
              "drawbox=x=iw*0.74:y=ih*0.26:w=iw*0.14:h=ih*0.36:color=0x5a4a3a:t=fill,"
              "drawbox=x=iw*0.43:y=ih*0.30:w=iw*0.14:h=ih*0.40:color=0xd9a35a:t=fill,"
              "drawbox=x=iw*0.455:y=ih*0.20:w=iw*0.09:h=ih*0.12:color=0xe8c9a0:t=fill")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "gradients=s=540x960:c0=0x24324a:c1=0x6a5a7a:d=1:speed=0,format=rgb24",
                        "-frames:v", "1", "-vf", vf, out], capture_output=True, timeout=60)
        if not os.path.exists(out):              # an ffmpeg without gradients: plain colour
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                            "color=c=0x2c3550:s=540x960:d=1", "-frames:v", "1", "-vf", vf,
                            out], capture_output=True, timeout=60)
    return out if os.path.exists(out) else None


def camera_move(name, src=None, w=112, h=112):
    """Frames of `src` (default: the sample scene) doing camera move `name`, as a small
    square (a grid of eight tall previews pushed the Make it button off the screen)."""
    if name not in CAMERA:
        return []
    src = src if src and os.path.exists(src) else sample_still()
    if not src:
        return []
    try:
        mt = os.path.getmtime(src)
    except OSError:
        mt = 0
    d = _dir("camera", _key(name, src, mt, w, h))
    have = _frames_in(d)
    if have:
        return have
    z, x, y = CAMERA[name]
    vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
          "zoompan=z='%s':x='%s':y='%s':d=%d:s=%dx%d:fps=%d"
          % (w * 2, h * 2, w * 2, h * 2, z, x, y, N_FRAMES, w, h, FPS))
    if name == "Whip pan":
        vf += ",tmix=frames=3"                   # the smear of a whip
    if name == "Static, alive":
        vf += ",eq=brightness='0.03*sin(2*PI*n/%d)'" % N_FRAMES
    subprocess.run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-i", src, "-vf", vf,
                    "-frames:v", str(N_FRAMES), os.path.join(d, "f%02d.png")],
                   capture_output=True, timeout=120)
    return _frames_in(d)


def format_loop(fmt, jobs=None, w=W, h=84):
    """Frames from your newest finished Quick video of format `fmt` ([]: you haven't made
    one yet), as a short band across the picture (the format cards are a grid of four)."""
    if jobs is None:
        import quick
        jobs = quick.list_jobs()
    vids = [j for j in jobs if j.get("format") == fmt and j.get("state") == "done" and
            j.get("final") and os.path.exists(j["final"])]
    if not vids:
        return []
    fin = max(vids, key=lambda j: os.path.getmtime(j["final"]))["final"]
    d = _dir("formats", _key(fin, os.path.getmtime(fin), w, h))
    have = _frames_in(d)
    if have:
        return have
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                    "format=duration", "-of", "csv=p=0", fin],
                                   capture_output=True, text=True, timeout=30).stdout or 0)
    except Exception:
        dur = 0.0
    if dur <= 0:
        return []
    # N frames spread over the video: a flick-book of what it looks like
    rate = N_FRAMES / max(1.0, dur)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", fin, "-vf",
                    "fps=%.4f,scale=%d:-2,crop=%d:min(ih\\,%d):0:(ih-min(ih\\,%d))*0.35"
                    % (rate, w, w, h, h), "-frames:v", str(N_FRAMES),
                    os.path.join(d, "f%02d.png")], capture_output=True, timeout=180)
    return _frames_in(d)


def in_background(fn, done, *args):
    """fn(*args) on a worker; done(result) is called with it (the caller marshals to Tk)."""
    def run():
        try:
            r = fn(*args)
        except Exception:
            r = []
        done(r)
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------------ Tk helpers
class Loop:
    """Cycle `frames` (PNG paths) on a Tk Label. play="hover": still until the pointer is
    over it; "always": keeps playing (one at a time, e.g. the selected choice)."""

    def __init__(self, label, frames, play="hover", ms=None, hover_widgets=()):
        import tkinter as tk
        self.label, self.ms, self.play = label, ms or int(1000 / FPS), play
        self.imgs = []
        for f in frames:
            try:
                self.imgs.append(tk.PhotoImage(file=f))
            except Exception:
                pass
        self.i, self.on, self.job = 0, play == "always", None
        if self.imgs:
            label.configure(image=self.imgs[0])
            label.image = self.imgs[0]
        for w in (label,) + tuple(hover_widgets):
            w.bind("<Enter>", lambda _e: self.start(), add="+")
            w.bind("<Leave>", lambda _e: self.stop(), add="+")
        if self.on:
            self._tick()

    def start(self):
        if not self.on:
            self.on = True
            self._tick()

    def stop(self):
        if self.play == "always":
            return
        self.on = False
        self.i = 0
        if self.imgs:
            try:
                self.label.configure(image=self.imgs[0])
            except Exception:
                pass

    def _tick(self):
        if not self.on or len(self.imgs) < 2:
            return
        try:
            if not self.label.winfo_exists():
                return
            self.i = (self.i + 1) % len(self.imgs)
            self.label.configure(image=self.imgs[self.i])
            self.label.after(self.ms, self._tick)
        except Exception:
            self.on = False


class CaptionLoop:
    """A caption LOOK, drawn live on a small Canvas: Clean (the phrase in white), Pop (the
    word being said lights up), Big (one word at a time, large). No files."""
    WORDS = ("YOUR", "EARS", "ARE", "IN", "MY", "SOUP")

    def __init__(self, parent, look, w=150, h=84, bg="#100e0c", font_family=None):
        import tkinter as tk
        self.cv = tk.Canvas(parent, width=w, height=h, bg=bg, highlightthickness=0)
        self.look, self.w, self.h, self.i = look, w, h, 0
        self.fam = font_family or "Helvetica"
        self._tick()

    def _tick(self):
        try:
            if not self.cv.winfo_exists():
                return
        except Exception:
            return
        cv, ws, i = self.cv, self.WORDS, self.i
        cv.delete("all")
        phrase = ws[:3] if i % len(ws) < 3 else ws[3:]
        k = (i % len(ws)) % 3
        if self.look == "big":
            cv.create_text(self.w / 2, self.h / 2, text=ws[i % len(ws)], fill="white",
                           font=(self.fam, 20, "bold"))
        else:
            txt_ids, x = [], 0
            pieces = []
            for j, word in enumerate(phrase):
                col = "#FFE500" if (self.look == "pop" and j == k) else "white"
                size = 13 if (self.look == "pop" and j == k) else 12
                pieces.append((word, col, size))
            total = sum(len(p[0]) * 9 + 6 for p in pieces)
            x = (self.w - total) / 2
            for word, col, size in pieces:
                txt_ids.append(cv.create_text(x, self.h * 0.72, text=word, fill=col,
                                              anchor="w", font=(self.fam, size, "bold")))
                x += len(word) * 9 + 6
        self.i += 1
        cv.after(420, self._tick)
