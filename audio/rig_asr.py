#!/usr/bin/env python3
"""Rig-side one-shot ASR (runs on `forge`, asr venv). Objective intelligibility check.

  --audio x.wav --out asr.json [--language English]

Returns the transcript Qwen3-ASR hears in the wav. Used to prove generated speech is
intelligible (we cannot listen to it on the Mac): the ASR text should match what was asked
for. Weights LOCAL (/mnt/models/audio).

API confirmed by reading `qwen_asr/inference/qwen3_asr.py` on forge:
  * Qwen3ASRModel.from_pretrained(path, device_map=..., dtype=...)
  * model.transcribe(audio=path, language="English") -> [ASRTranscription(text=..., language=...)]

Prints ONE json line: {"ok":true,"text":...,"language":...,"seconds":...}
"""
import argparse
import json
import os
import sys
import time

import torch

from qwen_asr import Qwen3ASRModel

ASR_MODEL = "/mnt/models/audio/Qwen3-ASR-1.7B"


def _emit(obj):
    print(json.dumps(obj), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--language", default="English")
    ap.add_argument("--out", default=None, help="optional path to write the transcript json")
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.audio):
        _emit({"ok": False, "error": "audio missing: %s" % args.audio})
        return 2

    t0 = time.time()
    model = Qwen3ASRModel.from_pretrained(
        ASR_MODEL, device_map=args.device, dtype=torch.bfloat16)
    results = model.transcribe(audio=args.audio, language=args.language)
    r = results[0]
    text = getattr(r, "text", None)
    if text is None:
        text = str(r)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"text": text, "language": getattr(r, "language", args.language)}, f,
                      ensure_ascii=False, indent=2)

    _emit({"ok": True, "text": text, "language": getattr(r, "language", args.language),
           "seconds": round(time.time() - t0, 2)})
    return 0


if __name__ == "__main__":
    sys.exit(main())