#!/usr/bin/env python3
"""Mirror Forge Lab outputs into the "Lab Results" folder — and track deletions.

Forge Lab (the Mac app) submits renders to ComfyUI on `forge` and `collect()`
pulls the results back into ~/forge-lab/runs/<run-id>/. This script MIRRORS those
into the Lab Results folder:

  * new media  -> copied in
  * a take deleted in the app (source gone) -> removed from Lab Results
  * and, unless --no-forge-delete, the same file is deleted on forge
    (~/ComfyUI/output) so the rig stops accumulating.

It is a MIRROR, not an archive: Lab Results ends up matching the app's takes.
Deleting a file by hand in Lab Results is also healed (re-copied) on the next pass.

Deleting on forge is DESTRUCTIVE and is only ever done for a file this script
itself recorded copying, matched by name AND byte size, and only inside the
forge output dir. Anything ambiguous is skipped and logged, never guessed.

Usage: lab_results_sync.py [--src DIR] [--dst DIR] [--state FILE]
                           [--forge-ssh name@forge] [--forge-output ~/ComfyUI/output]
                           [--no-forge-delete] [-v]
"""
import argparse
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys

MEDIA_EXTS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif",
    ".mp4", ".webm", ".mov", ".mkv",
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg",
)

DEFAULT_SRC = os.path.expanduser("~/forge-lab/runs")
DEFAULT_DST = os.path.expanduser("~/Desktop/Lab Results")
DEFAULT_STATE = os.path.expanduser(
    "~/Library/Application Support/ForgeLab/lab_results_state.json")
DEFAULT_FORGE_SSH = "name@forge"
DEFAULT_FORGE_OUTPUT = "~/ComfyUI/output"

# Runs on forge: delete the given (name, size) pairs from the output dir, but
# ONLY when exactly one file matches both. Prints DEL/MISS/AMBIG lines.
_REMOTE_SCRIPT = r'''
import base64, json, os, sys
payload = json.loads(base64.b64decode(sys.argv[-1]).decode())
out = os.path.expanduser(payload["out"])
for name, size in payload["items"]:
    hits = []
    for root, _dirs, files in os.walk(out):
        if name in files:
            p = os.path.join(root, name)
            try:
                if size is None or os.path.getsize(p) == size:
                    hits.append(p)
            except OSError:
                pass
    if len(hits) == 1:
        try:
            os.remove(hits[0])
            print("DEL " + hits[0])
        except OSError as e:
            print("ERR %s %s" % (name, e))
    elif not hits:
        print("MISS " + name)
    else:
        print("AMBIG " + name + " " + repr(hits))
'''


def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return _normalise(data)
    except Exception:
        pass
    return {"done": {}, "taken": {}, "pending_forge": {}}


def _normalise(data):
    done = data.get("done") or {}
    fixed = {}
    for src, rec in done.items():
        if isinstance(rec, str):                 # v1 -> v2 migration
            fixed[src] = {"dst": rec, "size": None}
        elif isinstance(rec, dict):
            fixed[src] = {"dst": rec.get("dst"), "size": rec.get("size")}
    data["done"] = fixed
    data.setdefault("taken", {})
    data.setdefault("pending_forge", {})
    return data


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def is_media(name):
    return name.lower().endswith(MEDIA_EXTS)


def unique_name(dst_dir, base, pid8, taken):
    if base in taken:
        return None
    stem, ext = os.path.splitext(base)
    candidate = "%s_%s%s" % (pid8, stem, ext)
    if candidate not in taken and not os.path.exists(os.path.join(dst_dir, candidate)):
        return candidate
    n = 2
    while True:
        candidate = "%s_%s_%d%s" % (pid8, stem, n, ext)
        if candidate not in taken and not os.path.exists(os.path.join(dst_dir, candidate)):
            return candidate
        n += 1


def forge_delete(ssh_target, forge_output, items, verbose=False):
    """Delete (name, size) pairs on forge. Returns the names confirmed handled."""
    payload = base64.b64encode(json.dumps(
        {"out": forge_output, "items": items}).encode()).decode()
    script_b64 = base64.b64encode(_REMOTE_SCRIPT.encode()).decode()
    remote = "python3 -c %s %s %s" % (
        shlex.quote("import base64,sys;exec(base64.b64decode(sys.argv[1]).decode())"),
        shlex.quote(script_b64), shlex.quote(payload))
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target, remote]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        print("sync: forge-delete ssh failed: %s" % e, file=sys.stderr)
        return set()
    if r.returncode != 0:
        print("sync: forge-delete ssh rc=%d: %s" % (r.returncode, r.stderr.strip()),
              file=sys.stderr)
        return set()
    handled = set()
    for line in r.stdout.splitlines():
        print("forge: " + line)
        if line.startswith("DEL "):
            handled.add(os.path.basename(line[4:].strip()))
        elif line.startswith("MISS "):
            handled.add(line[5:].strip())
    return handled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--dst", default=DEFAULT_DST)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--forge-ssh", default=DEFAULT_FORGE_SSH)
    ap.add_argument("--forge-output", default=DEFAULT_FORGE_OUTPUT)
    ap.add_argument("--no-forge-delete", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    src = os.path.abspath(os.path.expanduser(args.src))
    dst = os.path.abspath(os.path.expanduser(args.dst))
    state_path = os.path.abspath(os.path.expanduser(args.state))

    if not os.path.isdir(src):
        print("sync: source missing: %s" % src, file=sys.stderr)
        return 1
    try:
        os.makedirs(dst, exist_ok=True)
    except Exception as e:
        print("sync: cannot create %s: %s" % (dst, e), file=sys.stderr)
        return 2

    state = load_state(state_path)
    done = state["done"]        # src path -> {"dst": name, "size": int|None}
    taken = state["taken"]      # dst basename -> src path
    pending = state["pending_forge"]  # forge basename -> size|None

    # --- inventory the source ------------------------------------------------
    present = {}                # src path -> (run_id, size)
    for run in sorted(os.listdir(src)):
        run_dir = os.path.join(src, run)
        if not os.path.isdir(run_dir):
            continue
        for root, _dirs, files in os.walk(run_dir):
            for fn in sorted(files):
                if is_media(fn):
                    p = os.path.join(root, fn)
                    try:
                        sz = os.path.getsize(p)
                    except OSError:
                        sz = None
                    present[p] = (run, sz)

    for s, rec in done.items():
        taken.setdefault(rec["dst"], s)
        if rec.get("size") is None and s in present:
            rec["size"] = present[s][1]      # backfill for pre-v2 entries

    copied = healed = pruned = errors = 0

    # --- prune: source gone -> remove mirror + queue forge delete ------------
    for s in list(done.keys()):
        if s in present:
            continue
        rec = done.pop(s)
        name = rec["dst"]
        taken.pop(name, None)
        dstp = os.path.join(dst, name)
        try:
            os.remove(dstp)
        except FileNotFoundError:
            pass
        except OSError as e:
            errors += 1
            print("sync: could not remove %s: %s" % (dstp, e), file=sys.stderr)
        pruned += 1
        fname = os.path.basename(s)
        pending[fname] = rec.get("size")
        if args.verbose:
            print("- %s" % name)

    # --- heal: mirror missing but source present -> re-copy ------------------
    for s, rec in list(done.items()):
        if s not in present:
            continue
        dstp = os.path.join(dst, rec["dst"])
        if os.path.exists(dstp):
            continue
        try:
            shutil.copy2(s, dstp + ".part")
            os.replace(dstp + ".part", dstp)
            healed += 1
            if args.verbose:
                print("~ %s" % rec["dst"])
        except Exception as e:
            errors += 1
            print("sync: heal failed %s: %s" % (s, e), file=sys.stderr)

    # --- copy: new source -> mirror -----------------------------------------
    for s in sorted(present):
        if s in done:
            continue
        run, sz = present[s]
        pid8 = run.split("-")[0][:8]
        name = unique_name(dst, os.path.basename(s), pid8, taken)
        if name is None:
            continue
        dstp = os.path.join(dst, name)
        try:
            shutil.copy2(s, dstp + ".part")
            os.replace(dstp + ".part", dstp)
        except Exception as e:
            errors += 1
            print("sync: copy failed %s: %s" % (s, e), file=sys.stderr)
            try:
                os.remove(dstp + ".part")
            except OSError:
                pass
            continue
        done[s] = {"dst": name, "size": sz}
        taken[name] = s
        copied += 1
        if args.verbose:
            print("+ %s" % name)

    # --- flush queued forge deletions ---------------------------------------
    forge_done = 0
    if pending and not args.no_forge_delete:
        items = list(pending.items())
        handled = forge_delete(args.forge_ssh, args.forge_output, items, args.verbose)
        for name in handled:
            if name in pending:
                pending.pop(name)
                forge_done += 1

    save_state(state_path, state)

    print("sync: copied=%d pruned=%d healed=%d forge_del=%d pending=%d errors=%d dst=%s"
          % (copied, pruned, healed, forge_done, len(pending), errors, dst))
    return 0 if errors == 0 else 3


if __name__ == "__main__":
    sys.exit(main())