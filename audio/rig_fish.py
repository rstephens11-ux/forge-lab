#!/usr/bin/env python3
"""Rig-side BATCH TTS for Forge Lab dialogue — the FISH engine. Runs on `forge`.

Speaks EITHER checkpoint: S2-Pro (one card) or Drama 3 (both cards). Which one is read from the
checkpoint's own config, so the `--checkpoint` path chooses, not a flag.

Same job protocol as rig_dialogue.py, so `plan_render()`'s output is consumed unchanged:

    {"clone": [{"id": "line0", "voice": "<key>", "text": "...", "out": "line000.wav"}],
     "voices": {"<key>": {"ref_wav": "...", "ref_text": "..."}},
     "language": "English"}

⚠️ TWO WAYS FISH IS NOT QWEN, and the caller owns both:

  1. **There is no VoiceDesign here.** A fish voice IS a reference wav plus its transcript.
     Voice references are still MADE by Qwen's VoiceDesign (that step stays on the qwen rig);
     this script only ever *clones*.
  2. **Mood is not a separate reference sample — it is an inline `[tag]` in the text.**
     Both checkpoints accept free-form descriptions, e.g. "[whisper] ...", "[angry] ...".
     The caller puts the tag in front of the line; this script sends the text through untouched.

DRAMA 3 ALSO TAKES A WHOLE SCENE IN ONE REQUEST — its reason to exist — via `"group"`:

    {"group": [{"id": "scene0", "out": "scene000.wav", "voices": ["avery", "thomas"],
                "text": "<|speaker:0|> You will not complain? <|speaker:1|> It is a website!"}],
     "voices": {...}}

  Each name in `group[].voices` becomes ONE reference at that `<|speaker:N|>` index (index 0 is
  `voices[0]`), and the `<|speaker:N|>` tags in the text decide who speaks — Drama 3's reference
  ORDER does not matter (unlike S2-Pro, where order is the role).
  ⚠️ Multi-speaker is Drama 3 ONLY: a group sent to an S2-Pro checkpoint is a per-item error.
  ⚠️ Do NOT force `single_chunk` on a scene. Drama 3's own chunker splits long text at sentence
  ends with at most THREE speakers per chunk and feeds the first + latest chunk back as context —
  that IS the long-form multi-speaker feature. `--single-chunk` is available to force one pass
  (its own limit is 2048 frames, about 95 s), but the default chunked path is the one built for
  scenes.

Outputs ONE json line, the same shape rig_dialogue.py prints:

    {"ok": true, "model": "s2"|"drama3",
     "items": [{"id","out","sr","samples","duration","gen_seconds","seconds"}],
     "load_seconds": {"clone": 12.3}, "seconds": 40.1}

A per-item exception records {"id": ..., "error": ...} and the batch continues.
⚠️ A clone with NO usable reference must be a per-item ERROR: generating with no reference
still returns audio in SOME voice and looks exactly like success.
Peak-normalise is NOT done here (the app's assemble/peak_normalise path owns that).
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import soundfile as sf

# The repo is editable-installed in the venv, but `tools.*` needs the root on sys.path
# (api_server.py does the same via pyrootutils).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import torch  # noqa: E402
from fish_speech.utils.schema import ServeReferenceAudio, ServeTTSRequest  # noqa: E402
from tools.server.model_manager import ModelManager  # noqa: E402

DEFAULT_ROOT = "/mnt/models/audio"
DEFAULT_CKPT = "s2-pro"


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _load(ckpt_dir, device, compile_):
    """Load one checkpoint (S2-Pro or Drama 3) + its codec once.

    Returns (engine, model_family, load_seconds). The codec path is deliberately left None:
    ModelManager's resolve_codec_path finds <ckpt>/codec.pth and otherwise falls back to the
    sibling s2-pro/codec.pth — which is exactly what Drama 3 needs.
    """
    if not os.path.isdir(ckpt_dir):
        raise RuntimeError("checkpoint dir missing: %s" % ckpt_dir)
    t0 = time.time()
    mm = ModelManager(
        mode="tts",
        device=device,
        half=False,
        compile=compile_,
        llama_checkpoint_path=ckpt_dir,
        decoder_checkpoint_path=None,
        decoder_config_name="modded_dac_vq",
    )
    return mm.tts_inference_engine, mm.model_family, round(time.time() - t0, 2)


def _generate(engine, text, references, max_new_tokens=None, single_chunk=False):
    """One request (single line OR multi-speaker scene). Returns (float_audio, sample_rate)."""
    req = ServeTTSRequest(text=text, references=references, format="wav", streaming=False)
    if max_new_tokens:
        req.max_new_tokens = int(max_new_tokens)
    if single_chunk:
        req.single_chunk = True

    final = None
    for result in engine.inference(req):
        if result.code == "error":
            raise RuntimeError(str(result.error))
        if result.code == "final":
            final = result.audio
    if final is None:
        raise RuntimeError("no audio returned")
    sr, audio = final
    return np.asarray(audio, dtype=np.float32), int(sr)


def _ref_bytes(ref_wav):
    with open(ref_wav, "rb") as f:
        return f.read()


def _one_reference(voices, key):
    """The single ServeReferenceAudio for a `clone` item. Raises when there is no usable ref —
    see the module docstring: generating without one looks exactly like success."""
    v = voices.get(key) or {}
    ref_wav = v.get("ref_wav") or ""
    if not ref_wav or not os.path.isfile(ref_wav):
        raise RuntimeError("no usable reference audio for voice %r" % key)
    return ServeReferenceAudio(audio=_ref_bytes(ref_wav), text=v.get("ref_text") or "")


def _scene_references(voices, keys):
    """One reference per speaker index for a `group` item: <|speaker:N|> index = list position."""
    refs = []
    for i, key in enumerate(keys):
        v = voices.get(key) or {}
        ref_wav = v.get("ref_wav") or ""
        if not ref_wav or not os.path.isfile(ref_wav):
            raise RuntimeError("no usable reference audio for speaker %d (%r)" % (i, key))
        refs.append(ServeReferenceAudio(audio=_ref_bytes(ref_wav),
                                        text=v.get("ref_text") or "", speaker=i))
    if not refs:
        raise RuntimeError("group item names no speakers")
    return refs


def _save(out_dir, name, audio, sr):
    out_path = os.path.join(out_dir, name)
    sf.write(out_path, audio, sr)
    return {
        "out": os.path.abspath(out_path),
        "sr": sr,
        "samples": int(audio.shape[0]),
        "duration": round(float(audio.shape[0]) / float(sr), 3),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="path to job.json")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="directory holding the checkpoints")
    ap.add_argument("--checkpoint", default=DEFAULT_CKPT,
                    help="an absolute path, or a name under --root (default s2-pro). "
                         "drama-3 selects the 2-card model.")
    ap.add_argument("--device", default="cuda:0",
                    help="logical device; the driver sets CUDA_VISIBLE_DEVICES. Drama 3 wants a "
                         "list, e.g. cuda:0,cuda:1 — the LAST one holds the tower + codec")
    ap.add_argument("--max-new-tokens", type=int, default=0,
                    help="0 = the model's own cap")
    ap.add_argument("--single-chunk", action="store_true",
                    help="generate a long single-speaker text in one pass (groups always do)")
    args = ap.parse_args(argv)

    ckpt = args.checkpoint if os.path.isabs(args.checkpoint) \
        else os.path.join(args.root, args.checkpoint)

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
    try:
        engine, family, load_s = _load(ckpt, args.device, compile_=False)
        load_seconds["clone"] = load_s
    except Exception as e:
        _emit({"ok": False, "error": "fish model load failed (%s): %s" % (ckpt, e)})
        return 2

    results = []

    for item in clone_items:
        t0 = time.time()
        try:
            refs = [_one_reference(voices, item["voice"])]
            audio, sr = _generate(engine, item["text"], refs,
                                  max_new_tokens=args.max_new_tokens,
                                  single_chunk=args.single_chunk)
            gen_s = time.time() - t0
            rec = _save(out_dir, item["out"], audio, sr)
            rec.update({"id": item["id"], "gen_seconds": round(gen_s, 2),
                        "seconds": round(time.time() - t0, 2)})
            results.append(rec)
        except Exception as e:
            results.append({"id": item.get("id"), "error": str(e)})

    for item in group_items:
        t0 = time.time()
        try:
            if family != "drama3":
                raise RuntimeError(
                    "multi-speaker groups need a Drama 3 checkpoint (this is %r)" % family)
            refs = _scene_references(voices, item.get("voices") or [])
            audio, sr = _generate(engine, item["text"], refs,
                                  max_new_tokens=args.max_new_tokens,
                                  single_chunk=args.single_chunk)
            gen_s = time.time() - t0
            rec = _save(out_dir, item["out"], audio, sr)
            rec.update({"id": item["id"], "gen_seconds": round(gen_s, 2),
                        "seconds": round(time.time() - t0, 2)})
            results.append(rec)
        except Exception as e:
            results.append({"id": item.get("id"), "error": str(e)})

    _emit({
        "ok": True,
        "model": family,
        "items": results,
        "load_seconds": load_seconds,
        "seconds": round(time.time() - overall_t0, 2),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
