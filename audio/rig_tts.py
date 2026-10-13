#!/usr/bin/env python3
"""Rig-side one-shot TTS for Forge Lab Studio (runs on `forge`, tts venv).

Two jobs, deliberately ONE process each so GPU memory is released on exit:

  * voice DESIGN  (VoiceDesign model)      : --design "<desc>" --text "..." --out out.wav
  * voice CLONE   (Base model, ICL mode)   : --clone ref.wav --ref-text "..." --text "..." --out out.wav

Designed for the consistency workflow: design a voice ONCE, keep the wav, then clone that
wav for every later line so the character's voice is identical from line to line.

Weights are LOCAL (/mnt/models/audio) -- this script never downloads.

API confirmed by reading the installed package source on forge
(`qwen_tts/inference/qwen3_tts_model.py`):
  * Qwen3TTSModel.from_pretrained(path, device_map=..., dtype=..., attn_implementation=...)
  * model.generate_voice_design(text=..., instruct=..., language="English") -> (wavs, sr)
  * model.generate_voice_clone(text=..., language=..., ref_audio=path,
                               ref_text=...) -> (wavs, sr)   # ICL mode needs ref_text

Prints ONE json line on success: {"ok":true,"mode":...,"out":...,"sr":...,"duration":...,"seconds":...}
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
    print(json.dumps(obj), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", help="voice description (VoiceDesign model)")
    ap.add_argument("--clone", help="reference wav path (Base model, ICL voice clone)")
    ap.add_argument("--ref-text", default=None, help="transcript of the reference wav")
    ap.add_argument("--text", required=True, help="text to speak")
    ap.add_argument("--language", default="English")
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cuda:0",
                    help="logical device; the driver sets CUDA_VISIBLE_DEVICES")
    args = ap.parse_args(argv)

    if bool(args.design) == bool(args.clone):
        _emit({"ok": False, "error": "exactly one of --design / --clone is required"})
        return 2

    mode = "design" if args.design else "clone"
    model_path = MODELS[mode]
    if not os.path.isdir(model_path):
        _emit({"ok": False, "error": "weights missing: %s" % model_path})
        return 2

    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(
        model_path,
        device_map=args.device,
        dtype=torch.bfloat16,
    )
    load_s = time.time() - t0

    t1 = time.time()
    if mode == "design":
        wavs, sr = model.generate_voice_design(
            text=args.text, instruct=args.design, language=args.language)
    else:
        if not args.ref_text:
            # ICL mode (the default) requires the reference transcript.
            _emit({"ok": False, "error": "--clone requires --ref-text (ICL mode)"})
            return 2
        if not os.path.isfile(args.clone):
            _emit({"ok": False, "error": "reference audio missing: %s" % args.clone})
            return 2
        wavs, sr = model.generate_voice_clone(
            text=args.text, language=args.language,
            ref_audio=args.clone, ref_text=args.ref_text)
    gen_s = time.time() - t1

    wav = np.asarray(wavs[0])
    sf.write(args.out, wav, sr)
    _emit({
        "ok": True, "mode": mode, "out": os.path.abspath(args.out),
        "sr": int(sr), "samples": int(wav.shape[0]),
        "duration": round(float(wav.shape[0]) / float(sr), 3),
        "load_seconds": round(load_s, 2), "gen_seconds": round(gen_s, 2),
        "seconds": round(time.time() - t0, 2),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())