# Forge Lab Studio — audio (headless TTS + alignment on `forge`)

Speech for characters, run on the rig's second GPU and driven from the Mac. Nothing here
touches `forge_lab.py`; it is a standalone service the Studio can call.

## What it does

| need | model | call |
|---|---|---|
| design a voice from a description | Qwen3-TTS-12Hz-1.7B-**VoiceDesign** | `tts_design(desc, text, out)` |
| reuse that voice on every later line | Qwen3-TTS-12Hz-1.7B-**Base** (ICL clone) | `tts_clone(ref_wav, ref_text, text, out)` |
| per-word timings for lip-sync/subtitles | **Qwen3-ForcedAligner-0.6B** | `align(wav, text) -> [{word,start,end}]` |
| objective intelligibility check | Qwen3-ASR-1.7B | `transcribe(wav) -> text` |

**Character consistency workflow:** `tts_design()` a voice once, keep the wav, then
`tts_clone()` that wav for *every* later line. The clone reuses the exact voice (speaker
embedding + reference codes), so lines stay consistent instead of drifting per generation.

## Layout

- `rig_tts.py`, `rig_align.py`, `rig_asr.py` — run ON forge, one venv each job:
  - tts venv `/home/ryan/qwen-audio/tts/.venv/bin/python` (qwen-tts)
  - asr venv `/home/ryan/qwen-audio/asr/.venv/bin/python` (qwen-asr)
  - They cannot share a venv (`transformers==4.57.3` vs `==4.57.6`).
  - Weights are LOCAL `/mnt/models/audio/*` — the scripts never download.
- `client.py` — Mac side, **stdlib only**. scp's inputs to `/tmp/forge-audio/<uuid>`,
  runs the rig script over ssh with `CUDA_VISIBLE_DEVICES=1`, scp's results back, cleans up.
- `../tools/audio_matrix.py` — GPU-free assertions on the command shape (venv, GPU pin,
  quote round-trip) and the align-output parser. Run after any change.
- `../tools/audio_live_proof.py` — the real end-to-end run (touches GPU1).

## GPU discipline

Each call is a **one-shot process**: model loads, job runs, process exits → GPU1 memory is
released, so it never blocks the LLM (Ollama) or a Raylight render. Measured peak **~5.1 GB**
on GPU1, released to idle after. GPU0 is ComfyUI's and is never touched.

## Usage (Mac)

```python
import sys; sys.path.insert(0, "<repo>/audio")
import client
client.deploy()                                   # copy rig scripts to forge once
client.tts_design("a flat, nasal man, deadpan", "I'd like a hat.", "line1.wav")
client.tts_clone("line1.wav", "I'd like a hat.", "No, bigger than that.", "line2.wav")
words = client.align("line2.wav", "No, bigger than that.")
```

```bash
/opt/homebrew/bin/python3 tools/audio_matrix.py        # GPU-free, must be green
/opt/homebrew/bin/python3 tools/audio_live_proof.py    # real run -> ~/forge-lab-work/audio-sample
```
