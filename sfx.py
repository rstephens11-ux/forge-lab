"""SOUND EFFECTS — one short sound under a line when the action calls for it (roadmap
2026-10-18, item 4: room tone alone sounds thin next to Higgsfield). Tk-free.

Where the sounds come from, best first:
  1. YOUR sounds: any audio file in DATA/sfx/ (named for what it is: "door knock.wav",
     "crowd laugh.mp3"...). Drop better ones in any time; they win over the built-ins.
  2. A small BUILT-IN set made by ffmpeg on first use (DATA/sfx/builtin/): knock, thud,
     pop, ding, whoosh, glass clink, phone buzz, footsteps, slide whistle, rimshot,
     cash register, record scratch. Honest placeholders, not foley.

    find(cue)   -> the best file for a cue like "a knock at the door", or None
    library()   -> [(name, path, "yours"|"built-in")]

The writer's acting pass (quick.stage_directions) names at most a couple of sounds per
script; studio_pipeline.sound_design lays each one in under its line.
"""
import os
import re
import subprocess

import forge_core as c

AUDIO_EXT = (".wav", ".mp3", ".m4a", ".aif", ".aiff", ".flac", ".ogg", ".aac")

# name -> (keywords, ffmpeg lavfi graph producing it)
BUILTIN = {
    "knock": ("knock knocking door rap tap",
              "aevalsrc='0.9*sin(2*PI*120*t)*exp(-35*mod(t,0.22))*lt(t,0.62)':s=44100:d=0.7,"
              "lowpass=f=900"),
    "thud": ("thud thump fall drop bump bang slam",
             "aevalsrc='sin(2*PI*70*t)*exp(-12*t)':s=44100:d=0.45,lowpass=f=400"),
    "pop": ("pop cork bubble plop",
            "aevalsrc='sin(2*PI*(900-1800*t)*t)*exp(-40*t)':s=44100:d=0.15"),
    "ding": ("ding bell chime notification ping elevator",
             "aevalsrc='0.6*sin(2*PI*1320*t)*exp(-4*t)+0.3*sin(2*PI*2640*t)*exp(-6*t)'"
             ":s=44100:d=1.2"),
    "whoosh": ("whoosh swish swoosh woosh rush wind",
               "anoisesrc=color=pink:amplitude=0.5:duration=0.5:sample_rate=44100,"
               "bandpass=f=1500:width_type=o:w=1.5,afade=t=in:d=0.3,afade=t=out:st=0.3:d=0.2"),
    "glass clink": ("glass clink toast cheers cup mug",
                    "aevalsrc='0.5*sin(2*PI*3100*t)*exp(-9*t)+0.4*sin(2*PI*4650*t)*exp(-12*t)'"
                    ":s=44100:d=0.8"),
    "phone buzz": ("phone buzz vibrate buzzing text message call ring",
                   "aevalsrc='0.4*sgn(sin(2*PI*160*t))*lt(mod(t,0.5),0.3)':s=44100:d=1.0,"
                   "lowpass=f=1200"),
    "footsteps": ("footsteps steps walk walking footstep stomp",
                  "aevalsrc='sin(2*PI*90*t)*exp(-30*mod(t,0.45))':s=44100:d=1.8,"
                  "lowpass=f=700"),
    "slide whistle": ("slide whistle cartoon comedy fall slip",
                      "aevalsrc='0.4*sin(2*PI*(1600*t-500*t*t))':s=44100:d=0.9,"
                      "afade=t=out:st=0.7:d=0.2"),
    "rimshot": ("rimshot ba dum tss joke drum",
                "aevalsrc='0.6*sin(2*PI*180*t)*exp(-25*t)+0.6*sin(2*PI*180*(t-0.18))*"
                "exp(-25*(t-0.18))*gte(t,0.18)':s=44100:d=0.9,"
                "highpass=f=60"),
    "cash register": ("cash register ka-ching money sale buy kaching",
                      "aevalsrc='0.5*sin(2*PI*2093*t)*exp(-5*t)+0.4*sin(2*PI*2637*(t-0.08))*"
                      "exp(-5*(t-0.08))*gte(t,0.08)':s=44100:d=1.0"),
    "record scratch": ("record scratch stop freeze wait what",
                       "anoisesrc=color=white:amplitude=0.5:duration=0.4:sample_rate=44100,"
                       "bandpass=f=900:width_type=o:w=2,vibrato=f=12:d=1,"
                       "afade=t=out:st=0.25:d=0.15"),
}


def sfx_dir():
    d = os.path.join(c.DATA if hasattr(c, "DATA") else os.path.expanduser("~/forge-lab"),
                     "sfx")
    os.makedirs(d, exist_ok=True)
    return d


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def _words(text):
    return {_stem(w) for w in re.findall(r"[a-z]+", (text or "").lower())}


def builtin(name):
    """The built-in sound `name`, made by ffmpeg the first time."""
    if name not in BUILTIN:
        return None
    d = os.path.join(sfx_dir(), "builtin")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, name.replace(" ", "_") + ".wav")
    if not os.path.exists(out):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", BUILTIN[name][1],
                        "-ac", "2", "-ar", "44100", out], capture_output=True, timeout=60)
    return out if os.path.exists(out) else None


def library():
    out = []
    for fn in sorted(os.listdir(sfx_dir())):
        p = os.path.join(sfx_dir(), fn)
        if os.path.isfile(p) and fn.lower().endswith(AUDIO_EXT):
            out.append((os.path.splitext(fn)[0].replace("_", " "), p, "yours"))
    for name in BUILTIN:
        out.append((name, os.path.join(sfx_dir(), "builtin", name.replace(" ", "_") + ".wav"),
                    "built-in"))
    return out


def find(cue):
    """The best sound for `cue`: your file whose name shares the most words with it, else a
    built-in whose keywords do; None when nothing matches (no sound is better than a wrong
    one)."""
    want = _words(cue) - {"a", "an", "the", "of", "at", "on", "in", "sound", "sfx", "loud",
                          "soft", "small", "big", "some", "and"}
    if not want:
        return None
    best, best_s = None, 0
    for name, path, kind in library():
        if kind != "yours":
            continue
        sc = len(_words(name) & want)
        if sc > best_s:
            best, best_s = path, sc
    if best:
        return best
    for name, (kw, _g) in BUILTIN.items():
        sc = len((_words(kw) | _words(name)) & want)
        if sc > best_s:
            best, best_s = name, sc
    return builtin(best) if best else None
