#!/usr/bin/env python3
"""Rig-side BATCH TTS for Forge Lab Studio dialogue (runs on `forge`, tts venv).

Loads a model ONCE per batch, not once per line. Two phases:

  1. VoiceDesign (if any `design` items) -- freed before phase 2
  2. Base + voice clone (if any `clone` items) -- prompts built ONCE per voice key

Usage:
  rig_dialogue.py --job job.json

Where job.json:
  {"design": [{"id": "d1", "instruct": "...", "text": "...", "out": "d1.wav"}],
   "clone": [{"id": "c1", "voice": "avery", "text": "...", "out": "c1.wav"},
             {"id": "c2", "voice": "avery", "text": "...", "out": "c2.wav"}],
   "voices": {"avery": {"ref_wav": "avery_neutral.wav", "ref_text": "..."}},
   "language": "English"}

Outputs ONE json line:
  {"ok": true,
   "items": [{"id": "d1", "out": "/abs/path/d1.wav", "duration": 2.1,
              "load_seconds": {"design": 8.5}, "gen_seconds": 1.2,
              "seconds": 10.3},
             {"id": "c1", "out": "/abs/path/c1.wav", ...}],
   "load_seconds": {"design": 8.5, "clone": 6.2},
   "seconds": 25.0}

  On a per-item exception records {"id": "d1", "error": "..."} and continues.

Defers to the same model paths and APIs as rig_tts.py. Peak-normalise NOT done here.
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import soundfile as sf
import torch

from qwen_tts import Qwen3TTSModel

ROOT = "/mnt/models/audio"
MODELS = {
    "design": os.path.join(ROOT, "Qwen3-TTS-12Hz-1.7B-VoiceDesign"),
    "clone": os.path.join(ROOT, "Qwen3-TTS-12Hz-1.7B-Base"),
}


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _process_design(items, model, language, out_dir):
    """Process a list of design items. Returns list of result dicts."""
    results = []
    for item in items:
        t0 = time.time()
        try:
            wavs, sr = model.generate_voice_design(
                text=item["text"], instruct=item["instruct"], language=language)
            gen_s = time.time() - t0
            wav = np.asarray(wavs[0])
            out_path = os.path.join(out_dir, item["out"])
            sf.write(out_path, wav, sr)
            results.append({
                "id": item["id"],
                "out": os.path.abspath(out_path),
                "sr": int(sr),
                "samples": int(wav.shape[0]),
                "duration": round(float(wav.shape[0]) / float(sr), 3),
                "gen_seconds": round(gen_s, 2),
                "seconds": round(time.time() - t0, 2),
            })
        except Exception as e:
            results.append({"id": item["id"], "error": str(e)})
    return results


def _build_prompts(model, clone_items, voices):
    """Build voice clone prompts once per unique voice key that has valid ref_wav."""
    prompts = {}
    for item in clone_items:
        vk = item["voice"]
        if vk in prompts:
            continue
        if vk in voices:
            v = voices[vk]
            if os.path.isfile(v.get("ref_wav", "")):
                try:
                    ref_text = v.get("ref_text")
                    prompt = model.create_voice_clone_prompt(
                        ref_audio=v["ref_wav"], ref_text=ref_text)
                    prompts[vk] = prompt
                except Exception:
                    prompts[vk] = None
            else:
                prompts[vk] = None
        else:
            prompts[vk] = None
    return prompts


def _process_clone(items, prompts, model, language, out_dir):
    """Process a list of clone items using pre-built voice clone prompts."""
    results = []
    for item in items:
        t0 = time.time()
        try:
            prompt = prompts.get(item["voice"])
            if prompt is None:
                # ⚠️ Never generate without a reference: it would come back in SOME voice and
                # look like success. A missing/unreadable ref is that line's error, said plainly.
                raise RuntimeError("no usable reference audio for voice %r" % item["voice"])
            wavs, sr = model.generate_voice_clone(
                text=item["text"], language=language, voice_clone_prompt=prompt)
            gen_s = time.time() - t0
            wav = np.asarray(wavs[0])
            out_path = os.path.join(out_dir, item["out"])
            sf.write(out_path, wav, sr)
            results.append({
                "id": item["id"],
                "out": os.path.abspath(out_path),
                "sr": int(sr),
                "samples": int(wav.shape[0]),
                "duration": round(float(wav.shape[0]) / float(sr), 3),
                "gen_seconds": round(gen_s, 2),
                "seconds": round(time.time() - t0, 2),
            })
        except Exception as e:
            results.append({"id": item["id"], "error": str(e)})
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="path to job.json")
    ap.add_argument("--device", default="cuda:0",
                    help="logical device; the driver sets CUDA_VISIBLE_DEVICES")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.job):
        _emit({"ok": False, "error": "job file missing: %s" % args.job})
        return 2

    with open(args.job, "r", encoding="utf-8") as f:
        job = json.load(f)

    out_dir = os.path.dirname(os.path.abspath(args.job))
    language = job.get("language", "English")
    design_items = job.get("design", [])
    clone_items = job.get("clone", [])
    voices = job.get("voices", {})

    if not design_items and not clone_items:
        _emit({"ok": False, "error": "job has no design or clone items"})
        return 2

    all_items = []
    load_seconds = {}
    overall_t0 = time.time()

    # ---- Phase 1: VoiceDesign ------------------------------------------------
    if design_items:
        model_path = MODELS["design"]
        if not os.path.isdir(model_path):
            _emit({"ok": False, "error": "VoiceDesign weights missing: %s" % model_path})
            return 2
        t0 = time.time()
        model = Qwen3TTSModel.from_pretrained(
            model_path, device_map=args.device, dtype=torch.bfloat16)
        load_seconds["design"] = round(time.time() - t0, 2)
        all_items.extend(_process_design(design_items, model, language, out_dir))
        del model
        torch.cuda.empty_cache()

    # ---- Phase 2: Voice clone (ICL) ------------------------------------------
    if clone_items:
        model_path = MODELS["clone"]
        if not os.path.isdir(model_path):
            _emit({"ok": False, "error": "Base weights missing: %s" % model_path})
            return 2
        t0 = time.time()
        model = Qwen3TTSModel.from_pretrained(
            model_path, device_map=args.device, dtype=torch.bfloat16)
        load_seconds["clone"] = round(time.time() - t0, 2)

        # Build prompts once per voice key
        prompts = _build_prompts(model, clone_items, voices)
        all_items.extend(_process_clone(clone_items, prompts, model, language, out_dir))
        del model
        torch.cuda.empty_cache()

    total_s = round(time.time() - overall_t0, 2)

    _emit({
        "ok": True,
        "items": all_items,
        "load_seconds": load_seconds,
        "seconds": total_s,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())