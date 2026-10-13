#!/usr/bin/env python3
"""Rig-side BATCH ASR for Forge Lab Studio dialogue (runs on `forge`, asr venv).

Loads the ASR model ONCE, transcribes every audio file, outputs one JSON line.

Usage:
  rig_asr_batch.py --audio a.wav b.wav ... [--language English]

Outputs ONE json line:
  {"ok": true,
   "items": [{"audio": "a.wav", "text": "..."},
             {"audio": "b.wav", "text": "..."}],
   "load_seconds": 6.5,
   "seconds": 12.3}

  Per-item errors recorded as {"audio": "a.wav", "error": "..."}.
  Order matches the --audio arguments.
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
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, nargs="+",
                    help="one or more audio file paths to transcribe")
    ap.add_argument("--language", default="English")
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args(argv)

    if not args.audio:
        _emit({"ok": False, "error": "no audio files specified"})
        return 2

    # Check all audio files exist
    for path in args.audio:
        if not os.path.isfile(path):
            _emit({"ok": False, "error": "audio missing: %s" % path})
            return 2

    t0 = time.time()
    model = Qwen3ASRModel.from_pretrained(
        ASR_MODEL, device_map=args.device, dtype=torch.bfloat16)
    load_s = round(time.time() - t0, 2)

    items = []
    for path in args.audio:
        try:
            results = model.transcribe(audio=path, language=args.language)
            r = results[0]
            text = getattr(r, "text", None)
            if text is None:
                text = str(r)
            items.append({"audio": path, "text": text})
        except Exception as e:
            items.append({"audio": path, "error": str(e)})

    _emit({
        "ok": True,
        "items": items,
        "load_seconds": load_s,
        "seconds": round(time.time() - t0, 2),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())