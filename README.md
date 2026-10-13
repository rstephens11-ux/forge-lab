# Forge Lab

A macOS desktop app (tkinter) that drives image, video, music and voice models on a home GPU server, a dual RTX 3090 Ubuntu box I built, running ComfyUI and Ollama.

I'm not a software engineer. I'm an internal auditor, and I built this with AI coding agents (mostly [Hermes Agent](https://github.com/NousResearch/hermes-agent)) doing the typing while I directed, tested and decided. It's public because the way it was built is the interesting part, not because it's a polished product. shout out @bonegpt for the inspo with Slop Cannon, wanted to make a local version that cost me no credits. 

## What it does

- **Images:** text-to-image, image edit and background removal (Qwen-Image), with a prompt "polish" step that runs on the rig.
- **Video:** several engines behind one interface (Wan 2.2, LTX 2.3, MiniMax H3), including a two-GPU path, video-to-video motion transfer, and audio-driven video.
- **Songs:** style + lyrics → a finished song with vocals (YuE2), plus covers from a reference recording.
- **Dialogue:** design a character voice once, reuse it on every line (Qwen3-TTS), with an automatic speech-recognition check that the line came out right.
- **Director:** a local LLM (via Ollama on the rig) that turns a short brief into a shot plan.
- **Overnight runner:** a systemd service on the rig that works through queued briefs unattended.
- **Estimate first:** every render is priced in seconds before it's submitted. The estimates are fitted to timings measured on this hardware, not guessed.

## What's worth looking at

- **`AGENTS.md`**: the rules for more than one AI agent editing the same 16,500-line file at once. Content-anchored patches only, a stability check before and after every write, a commit per change, and "a test is not evidence until it has been seen to fail."
- **`tools/*_matrix.py`**: 17 GPU-free test suites that guard the rules each model engine depends on (frame counts, resolutions, which settings can't be half-switched), so a mistake fails loudly instead of producing a subtly wrong clip.
- **`tools/estimate_check.py`**: checks the app's time estimates against every measured render.
- **`tools/flatten_fidelity.py`**: checks that a workflow graph the app builds is faithful to the template it came from.

## Honest limitations

- **It won't run for you as-is.** It expects a ComfyUI server with specific models at `http://forge:8188` (override with `FORGE_URL`), SSH access as `FORGE_SSH`, and paths on the rig under `/home/ryan/`. It was built for one machine.
- **`forge_lab.py` is one very large file.** That was a deliberate trade-off for agent editing (one region map, anchored patches), not a recommendation.
- **The live tools in `tools/`** (anything not named `*_matrix.py`) submit real GPU work and need the rig.
- **Not included:** my creative projects (characters, episodes, scripts), generated media, my working notes, and the git history. Test fixtures use generic placeholder characters and lines.

## Running the tests

```sh
# Homebrew Python with Tk 9 (macOS's system Python ships Tk 8.5, which has no PNG support)
brew install python@3.14 python-tk@3.14
export FORGE_LAB_DATA="$(mktemp -d)"     # keeps the suites away from any real data
for t in tools/*_matrix.py; do
  [ "$t" = tools/render_matrix.py ] && continue   # that one submits real renders
  /opt/homebrew/bin/python3 "$t" | tail -1
done
```

## Building the app

`./build.sh` (PyInstaller, from `Forge Lab.spec`). Build from the spec, not the script. The spec carries the macOS Local Network permission without which every call to the rig is silently blocked.

MIT licensed.
