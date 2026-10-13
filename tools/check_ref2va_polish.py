"""End-to-end check: does a REAL polish() call now produce a Ref2VA-conformant prompt?

This is the step the commit message flagged as unverified. It loads DIRECTOR on forge's Ollama
(GPU1) — no ComfyUI render is submitted.
"""
import sys
import os

sys.path.insert(0, os.path.expanduser("~/forge-lab"))
import director as D

d = D.Director()
st = d.status()
print("director status:", st)

try:
    out = d.polish("a knight strides forward through a vaulted stone hall, sword upright",
                   engine="h3", video_mode="r2v")
except Exception as exc:
    print("POLISH FAILED: %s: %s" % (type(exc).__name__, str(exc)[:400]))
    sys.exit(1)

print("\n--- returned prompt: %d chars / %d words ---" % (len(out), len(out.split())))
print(out[:1500])
print("..." if len(out) > 1500 else "")
miss = D.missing_ref2va_sections(out)
print("\nmissing sections: %s" % (miss or "NONE — conformant"))
print("carries the old tag: %s" % ("<Picture 1> is the character" in out))
try:
    d.unload()
    print("\nunloaded (GPU1 released)")
except Exception as exc:
    print("unload failed: %s" % exc)