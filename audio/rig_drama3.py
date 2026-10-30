#!/usr/bin/env python3
"""Rig-side BATCH TTS for Forge Lab dialogue — the DRAMA 3 engine, served by SGLang-Omni.

Runs on `forge`. Same job protocol and same one-JSON-line output as rig_fish.py, so
`plan_render()`'s output is consumed unchanged:

    {"clone": [{"id": "line0", "voice": "<key>", "text": "...", "out": "line000.wav"}],
     "group": [{"id": "scene0", "out": "scene000.wav", "voices": ["avery", "thomas"],
                "text": "<|speaker:0|> ... <|speaker:1|> ..."}],
     "voices": {"<key>": {"ref_wav": "...", "ref_text": "..."}},
     "language": "English"}

WHY THIS IS NOT rig_fish.py
  The bf16/FP8 Drama 3 checkpoint cannot load on the 3090s: its FP8 experts need SM89+, so it
  dequantizes to ~70 GB of bf16 and OOMs (measured 2026-10-14). The *int4* checkpoint does fit,
  but only under the SGLang-Omni runtime (GPTQ-Marlin kernels, CC >= 8.0), which is a resident
  HTTP SERVER — not an in-process model. So this script is a thin client:

      POST /v1/audio/speech  {"input", "references": [{"audio_path", "text"}], "response_format"}

  Everything else — tags, cloning, multi-speaker, the item/error shape — is unchanged, which is
  what keeps Forge Lab's dialogue pipeline unaware that the engine underneath moved.

CARD OWNERSHIP (the one real difference from every other rig script)
  The server owns ALL of card 1 (weights ~20.8 GB of 24) and 5-6 GB of card 2 (vocoder +
  preprocessing). Nothing else can load there while it runs. So this script:
    * reuses a server that is already up (never claims someone else's),
    * otherwise starts one and WAITS for /v1/models (first start also compiles the int4 kernel),
    * and stops the one it started when the batch ends, unless --keep-server.
  It never starts a second server: two would OOM each other.

MODEL LIMITS (measured on forge 2026-10-21, against the patched server)
  * 2 speakers in one request work: each segment matches its own reference (cosine 0.82/0.86 vs
    ~0.58 cross). THREE OR MORE truncates: the take comes back with only the first line, on every
    retry. So a `group` is capped at 2 speakers here and 3+ is a per-item ERROR, not silent
    garbage. The SGLang-Omni request path also had every speaker id but 0 rejected upstream; that
    guard is patched on the rig (see the skill reference) and this script depends on it.
  * Reference audio: 60 s max per reference (server-enforced, HTTP 400).
  * ~0.9x realtime on the two 3090s with CUDA graphs off.
  * A take TRUNCATES at random (~half of runs) and a truncated scene loses a whole character.
    Every item is therefore re-taken until its duration matches the text, up to --retries;
    `takes` in the item record says how many it cost.

Peak-normalise is NOT done here (the app's assemble/peak_normalise path owns that).
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave

DEFAULT_ENDPOINT = "http://127.0.0.1:8091"
DEFAULT_SERVE_SCRIPT = "/home/ryan/d3_serve.sh"
DEFAULT_LOG = "/home/ryan/d3-serve.log"
MAX_SPEAKERS = 2          # measured ceiling; 3+ truncates (see docstring)
SPEAKER_TAG = re.compile(r"<\|speaker:(\d+)\|>")


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


# --------------------------------------------------------------------------- server lifecycle
def _healthy(endpoint, timeout=3.0):
    try:
        with urllib.request.urlopen(endpoint + "/v1/models", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _start_server(script, log, timeout_s):
    """Start the serve script in its own session and wait for /v1/models. Returns the Popen."""
    if not os.path.isfile(script):
        raise RuntimeError("serve script missing: %s" % script)
    logf = open(log, "ab", buffering=0)
    # ⚠️ The Forge Lab client runs every rig script with CUDA_VISIBLE_DEVICES=1 (one voice model
    # on card 2). The SERVER must not inherit that: its placement flags name absolute GPU ids
    # (engine 0, vocoder/preprocessing 1), and with one visible device it dies at startup with
    # "Stage 'preprocessing': GPU id 1 out of range; only 1 visible device(s)".
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = os.environ.get("DRAMA3_CUDA_VISIBLE_DEVICES", "0,1")
    proc = subprocess.Popen([script], stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                            start_new_session=True, env=env)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if proc.poll() is not None:
            raise RuntimeError("server exited during startup (see %s)" % log)
        if _healthy(os.environ.get("DRAMA3_ENDPOINT", DEFAULT_ENDPOINT)):
            return proc, round(time.time() - t0, 1)
        time.sleep(3)
    raise RuntimeError("server did not become healthy within %ds (see %s)" % (timeout_s, log))


def _stop_server(proc, grace=45):
    """Stop a server WE started: SIGTERM the process group, then SIGKILL if it lingers."""
    if proc is None or proc.poll() is not None:
        return 0.0
    t0 = time.time()
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            proc.kill()
        proc.wait(timeout=15)
    return round(time.time() - t0, 1)


# --------------------------------------------------------------------------- synthesis
def _post_speech(endpoint, text, refs, timeout_s):
    """One /v1/audio/speech call. Returns the raw WAV bytes."""
    payload = {"input": text, "response_format": "wav"}
    if refs:
        payload["references"] = refs
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(endpoint + "/v1/audio/speech", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("server HTTP %s: %s" % (e.code, detail))


def _one_reference(voices, key):
    """The single reference for a `clone` item. A missing ref must be an ERROR: the server would
    happily speak in some other voice and it would look exactly like success."""
    v = voices.get(key) or {}
    ref_wav = v.get("ref_wav") or ""
    if not ref_wav or not os.path.isfile(ref_wav):
        raise RuntimeError("no usable reference audio for voice %r" % key)
    return {"audio_path": os.path.abspath(ref_wav), "text": v.get("ref_text") or ""}


def _scene_references(voices, keys, text):
    """One reference per speaker index: <|speaker:N|> index = position in `keys`."""
    if not keys:
        raise RuntimeError("group item names no speakers")
    if len(keys) > MAX_SPEAKERS:
        raise RuntimeError("%d speakers requested; this server is measured at %d (3+ truncates)"
                           % (len(keys), MAX_SPEAKERS))
    used = {int(m.group(1)) for m in SPEAKER_TAG.finditer(text)}
    if not used:
        raise RuntimeError("group text carries no <|speaker:N|> tags")
    unmapped = sorted(i for i in used if i >= len(keys))
    if unmapped:
        raise RuntimeError("speaker tags %s have no reference (only %d given)"
                           % (unmapped, len(keys)))
    refs = []
    for i, key in enumerate(keys):
        v = voices.get(key) or {}
        ref_wav = v.get("ref_wav") or ""
        if not ref_wav or not os.path.isfile(ref_wav):
            raise RuntimeError("no usable reference audio for speaker %d (%r)" % (i, key))
        refs.append({"audio_path": os.path.abspath(ref_wav), "text": v.get("ref_text") or ""})
    return refs


def _wav_duration(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        return (w.getnframes() / float(sr)) if sr else 0.0


def _expected_seconds(text):
    """Rough speech length for `text`, used only to spot a TRUNCATED take.

    ~2.5 words/s (150 wpm) => 0.4 s per word. Real reads land between 0.30 and 0.45 s/word, so
    the 0.72 ratio below leaves a wide margin: a genuinely brisk take is not retried, but a take
    that lost a whole sentence (ratio ~0.45) is.
    """
    # ⚠️ Strip the bracket TAGS as well as the speaker tags before counting. The tags are
    # directions, not speech ("[quiet, heavy, sorrowful] This is not the Shenandoah." is 6
    # spoken words, not 9), and counting them made the floor too high: measured on the real
    # 101-pilot scene, 12 of 34 lines were re-taken for nothing and 3 were flagged as truncated
    # when ASR showed every word present.
    spoken = SPEAKER_TAG.sub(" ", re.sub(r"\[[^\]]*\]", " ", text))
    words = len(re.findall(r"[A-Za-z0-9']+", spoken))
    # Floor 0.8 s, not 1.2: a two-word line ("Why not?") legitimately comes back in 0.79 s, and
    # truncation drops whole sentences rather than half of a short one.
    return max(0.8, 0.4 * max(words, 1))


def _post_with_retry(endpoint, text, refs, timeout_s, tries):
    """One request, retried while the take looks TRUNCATED. Returns (wav_bytes, takes_used).

    ⚠️ This is not paranoia, it is required: the same request measured 2.55 s / 3.11 s / 6.36 s
    on three consecutive runs for text that needs ~6.4 s (forge, 2026-10-21) — the take ends
    early after the first speaker, so a two-speaker scene silently loses a character. The model
    card says the same thing ("prompts occasionally truncate a take — regenerate if needed").
    Retrying is the documented remedy, and duration is the only signal available here without
    running ASR per take.
    """
    need = _expected_seconds(text) * 0.72
    best = None
    for take in range(1, tries + 1):
        wav = _post_speech(endpoint, text, refs, timeout_s)
        dur = _wav_duration(wav)
        if best is None or dur > _wav_duration(best):
            best = wav
        if dur >= need:
            return wav, take
    return best, tries


def _save(out_dir, name, wav_bytes):
    out_path = os.path.join(out_dir, name)
    with open(out_path, "wb") as f:
        f.write(wav_bytes)
    with wave.open(out_path, "rb") as w:
        sr, frames = w.getframerate(), w.getnframes()
    return {
        "out": os.path.abspath(out_path),
        "sr": sr,
        "samples": int(frames),
        "duration": round(float(frames) / float(sr), 3) if sr else 0.0,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="path to job.json")
    ap.add_argument("--endpoint", default=os.environ.get("DRAMA3_ENDPOINT", DEFAULT_ENDPOINT))
    ap.add_argument("--serve-script", default=DEFAULT_SERVE_SCRIPT,
                    help="script that execs the SGLang-Omni server")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--start-timeout", type=int, default=420,
                    help="seconds to wait for a cold server (first start compiles the int4 kernel)")
    ap.add_argument("--request-timeout", type=int, default=900)
    ap.add_argument("--retries", type=int, default=4,
                    help="max takes per item while the take looks truncated (see _post_with_retry)")
    ap.add_argument("--keep-server", action="store_true",
                    help="leave a server this script started running afterwards")
    ap.add_argument("--no-autostart", action="store_true",
                    help="refuse to start a server; fail instead (card is someone else's)")
    args = ap.parse_args(argv)

    os.environ.setdefault("DRAMA3_ENDPOINT", args.endpoint)

    if not os.path.isfile(args.job):
        _emit({"ok": False, "error": "job file missing: %s" % args.job})
        return 2

    with open(args.job, "r", encoding="utf-8") as f:
        job = json.load(f)

    out_dir = os.path.dirname(os.path.abspath(args.job))
    clone_items = job.get("clone", [])
    group_items = job.get("group", [])
    voices = job.get("voices", {})

    if not clone_items and not group_items:
        _emit({"ok": False, "error": "job has no clone or group items"})
        return 2

    overall_t0 = time.time()
    load_seconds = {}
    proc = None
    started_here = False

    if _healthy(args.endpoint):
        load_seconds["clone"] = 0.0          # already resident
    elif args.no_autostart:
        _emit({"ok": False, "error": "no Drama 3 server at %s and --no-autostart was given"
               % args.endpoint})
        return 2
    else:
        try:
            proc, wait_s = _start_server(args.serve_script, args.log, args.start_timeout)
            started_here = True
            load_seconds["clone"] = wait_s
        except Exception as e:
            _emit({"ok": False, "error": "drama3 server start failed: %s" % e})
            return 2

    results = []
    try:
        for item in clone_items:
            t0 = time.time()
            try:
                refs = [_one_reference(voices, item["voice"])]
                wav, takes = _post_with_retry(args.endpoint, item["text"], refs,
                                              args.request_timeout, args.retries)
                gen_s = time.time() - t0
                rec = _save(out_dir, item["out"], wav)
                rec.update({"id": item["id"], "gen_seconds": round(gen_s, 2),
                            "takes": takes,
                            "seconds": round(time.time() - t0, 2)})
                results.append(rec)
            except Exception as e:
                results.append({"id": item.get("id"), "error": str(e)})

        for item in group_items:
            t0 = time.time()
            try:
                refs = _scene_references(voices, item.get("voices") or [], item.get("text") or "")
                wav, takes = _post_with_retry(args.endpoint, item["text"], refs,
                                              args.request_timeout, args.retries)
                gen_s = time.time() - t0
                rec = _save(out_dir, item["out"], wav)
                rec.update({"id": item["id"], "gen_seconds": round(gen_s, 2),
                            "takes": takes,
                            "seconds": round(time.time() - t0, 2)})
                results.append(rec)
            except Exception as e:
                results.append({"id": item.get("id"), "error": str(e)})
    finally:
        if started_here and not args.keep_server:
            load_seconds["server_stop"] = _stop_server(proc)

    _emit({
        "ok": True,
        "model": "drama3",
        "items": results,
        "load_seconds": load_seconds,
        "server_started": started_here,
        "seconds": round(time.time() - overall_t0, 2),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
