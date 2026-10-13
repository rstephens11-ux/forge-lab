#!/usr/bin/env python3
"""Rig-side one-shot forced alignment for Forge Lab Studio (runs on `forge`, asr venv).

  --audio x.wav --text "..." --out words.json

Produces [{"word": str, "start": sec, "end": sec}, ...] using Qwen3-ForcedAligner.

API confirmed by reading the installed package source on forge
(`qwen_asr/inference/qwen3_forced_aligner.py`):
  * Qwen3ForcedAligner.from_pretrained(path, device_map=..., dtype=...)
  * aligner.align(audio=path, text=..., language="English") -> List[ForcedAlignResult]
    each result is iterable of items with .text / .start_time (sec) / .end_time (sec)

Weights are LOCAL (/mnt/models/audio) -- never downloads.

Prints ONE json line on success: {"ok":true,"out":...,"count":N,"seconds":...}
"""
import argparse
import json
import os
import sys
import time

import torch

from qwen_asr import Qwen3ForcedAligner

ALIGNER = "/mnt/models/audio/Qwen3-ForcedAligner-0.6B"


def _emit(obj):
    print(json.dumps(obj), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--text", required=True, help="transcript to align the audio to")
    ap.add_argument("--language", default="English")
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.audio):
        _emit({"ok": False, "error": "audio missing: %s" % args.audio})
        return 2
    if not os.path.isdir(ALIGNER):
        _emit({"ok": False, "error": "weights missing: %s" % ALIGNER})
        return 2

    t0 = time.time()
    aligner = Qwen3ForcedAligner.from_pretrained(
        ALIGNER, device_map=args.device, dtype=torch.bfloat16)

    results = aligner.align(audio=args.audio, text=args.text, language=args.language)
    res = results[0]

    words = [{"word": str(it.text),
              "start": round(float(it.start_time), 3),
              "end": round(float(it.end_time), 3)} for it in res]

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)

    _emit({"ok": True, "out": os.path.abspath(args.out), "count": len(words),
           "seconds": round(time.time() - t0, 2)})
    return 0


if __name__ == "__main__":
    sys.exit(main())