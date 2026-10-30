#!/usr/bin/env python3
"""The app must find Homebrew tools (ffmpeg) even when launched from Finder with launchd's PATH.

    /opt/homebrew/bin/python3 tools/path_matrix.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


code = ("import os, sys; sys.path.insert(0, %r); import forge_lab; print(os.environ['PATH'])" % ROOT)
env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/tmp"),
       "FORGE_LAB_DATA": tempfile.mkdtemp(), "FORGE_LAB_NOAUTOSAVE": "1"}
out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
path = (out.stdout.strip().splitlines() or [""])[-1].split(os.pathsep)
check(out.returncode == 0, "forge_lab imports under launchd's PATH (%s)" % out.stderr.strip()[-120:])
check(path[:4] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"], "the original PATH is kept first")
for b in ("/opt/homebrew/bin", "/usr/local/bin"):
    if os.path.isdir(b):
        check(b in path, "%s is added when it exists" % b)
check(len(path) == len(set(path)), "nothing is added twice")
print("\n%s - %d checks failed" % ("ALL GREEN" if not FAILS else "FAIL", len(FAILS)))
sys.exit(1 if FAILS else 0)
