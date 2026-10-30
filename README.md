# Forge Lab

A macOS desktop app (tkinter) that drives image, video, music and voice models on a home GPU
server — a dual RTX 3090 Ubuntu box I built — running ComfyUI and Ollama.

I'm not a software engineer. I'm an internal auditor, and I built this with AI coding agents
(mostly [Hermes Agent](https://github.com/NousResearch/hermes-agent)) doing the typing while I
directed, tested and decided. It's public because the way it was built is the interesting part,
not because it's a polished product.

## Built on the Comfy Developer Platform

Every render the app submits goes through the **Comfy API v2 / Comfy SDK**, not a hand-rolled
HTTP POST to `/prompt`. Two files carry that:

- **`comfy_route.py`** hands the workflow graph to `comfy_sdk.Comfy` and turns input files
  (stills, voice references, guide audio) into SDK **assets** — hashed, and uploaded only if the
  rig doesn't already have them.
- On the rig, **`comfy-api-proxy`** sits in front of each ComfyUI instance
  (card 1 `:8188` ← proxy `:8288`; card 2 `:8189` ← proxy `:8289`), so the SDK job id becomes the
  ComfyUI `prompt_id` and an uploaded asset lands in ComfyUI's input folder under its own name.

`comfy_route` will fall back to the old raw `/prompt` path (`comfy_route: "direct"`) when no proxy
token is present, so the app still runs against a plain ComfyUI. **The SDK path is the one it is
built for and the one the demo shows.**

**Open weights only, for everything that renders.** Images are Qwen-Image, video is MiniMax H3 /
Wan 2.2 / LTX, songs are YuE2, voices are Qwen3-TTS and Fish S2-Pro — all running locally on the
3090s.

The one optional exception is the **Writer**, which drafts *text*, not media: it defaults to
**Ollama on the rig**, and the hosted presets in `director.py` (Anthropic, OpenAI, OpenRouter,
Nous) are opt-in — they do nothing unless you set a key in the environment or in a git-ignored
`writer_keys.json`. **No keys are in this repository**, and no render path touches them.

## What it does

- **Images:** text-to-image, image edit and background removal (Qwen-Image), with a prompt
  "polish" step that runs on the rig.
- **Restyle:** 35 styles in seven groups, picked from one sideways-scrolling strip.
- **Video:** several engines behind one interface (MiniMax H3, Wan 2.2, LTX 2.3), including a
  two-GPU path (Raylight), video-to-video motion transfer, and audio-driven video.
- **Songs:** style + lyrics → a finished song with vocals (YuE2), plus covers from a reference
  recording.
- **Dialogue:** design a character voice once, reuse it on every line (Qwen3-TTS / Fish / Drama 3),
  with an automatic speech-recognition check that the line came out right.
- **Long video / Series / Episode Cut:** scene queues, keepers, stitch, a show bible, and a
  timeline cut on the beat.
- **Writer:** an LLM drafts the script, and **you pick the provider and model** — rig Ollama,
  Anthropic, OpenRouter, Nous, OpenAI, or any custom URL.
- **Queue and dispatcher:** every job goes through one queue. A job takes the free card; when both
  are busy it waits, can be reordered, paused or deleted, and a finished job drops off by itself.
- **Overnight runner:** a systemd service on the rig that works through queued briefs unattended.
- **Estimate first:** every render is priced in seconds before it's submitted. The estimates are
  fitted to timings measured on this hardware, not guessed — and the UI says when a job is outside
  the measured range.

## What's worth looking at

- **`AGENTS.md`**: the rules for more than one AI agent editing the same very large file at once.
  Content-anchored patches only, a stability check before and after every write, a commit per
  change, and *"a test is not evidence until it has been seen to fail."*
- **`tools/*_matrix.py`**: GPU-free suites that guard the rules each engine depends on — frame
  counts, resolutions, which settings cannot be half-switched — so a mistake fails loudly instead
  of producing a subtly wrong clip.
- **`tools/estimate_check.py`**: checks the app's time estimates against every measured render.
- **`tools/flatten_fidelity.py`**: checks that a workflow graph the app builds is faithful to the
  template it came from.
- **`jobqueue.py` / `dispatch.py`**: the two-dimensional part — a job may be pinned to a card
  *or* to "any card", and a job left `running` by a dead process is failed on load rather than
  holding a card for ever.

## Honest limitations

- **It won't run for you as-is.** It expects a ComfyUI server with specific model files
  (`FORGE_URL`, default `http://forge:8188`), SSH access to that box (`FORGE_SSH`), and ComfyUI's
  own models in place. It was built for one machine.
- **The SSH target in this copy is `name@forge`**, a placeholder. Set `FORGE_SSH=you@yourbox`.
  One file, `audio/client.py`, still hardcodes `name@forge` and would need the same edit.
- **`forge_lab.py` is one very large file.** That was a deliberate trade-off for agent editing
  (one region map, anchored patches), not a recommendation.
- **The live tools in `tools/`** (anything not named `*_matrix.py`) submit real GPU work and need
  the rig.
- **Test suites: 38 of the 46 included here pass** on the machine this was exported from. Eight
  need something a clean checkout may not have — macOS Tk (`ui_matrix`, `apps_ui_matrix`,
  `dock_matrix`, `queue_ui_matrix`), a working numpy (`beat_matrix`), `FORGE_LAB_DATA`
  (`core_matrix`), local render assets (`render_matrix`) or `sips` plus git history (`v2v_matrix`).
  Those eight also fail on the original machine, so they are not regressions.
- **Not included:** my creative projects (characters, episodes, scripts), generated media, my
  working notes, and the git history. Test fixtures use a generic placeholder cast.

## Running it

No virtualenv and no requirements file — the app is Python's standard library plus Tk.

```sh
brew install python@3.14 python-tk@3.14      # Tk 9; the system Python ships Tk 8.5, which has no PNG support
export FORGE_LAB_DATA="$HOME/forge-lab"     # where characters, takes and episodes are written
/opt/homebrew/bin/python3 forge_lab.py
```

It launches with **no rig reachable**: the window opens and the queue simply has nowhere to
dispatch to. To point it at a ComfyUI box, set the host and prove the route:

```sh
export FORGE_SSH="you@your-rig"             # defaults to name@forge
/opt/homebrew/bin/python3 forge_lab.py --selftest
```

`--selftest` resolves the rig, writes a small PNG with the standard library, and submits one tiny
edit job through the same code path the app uses — so it fails loudly if the route is wrong.

Every call goes through ComfyUI's own HTTP API. `comfy_route.py` speaks **Comfy API v2** via
`comfy-api-proxy`, which sits in front of each card slot (ComfyUI `:8188` → proxy `:8288`).

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

`./build.sh` (PyInstaller, from `Forge Lab.spec`). Build from the spec, not the script. The spec
carries the macOS Local Network permission without which every call to the rig is silently blocked.

## Outputs

The clips in the demo were made with this app: a 60-second spot, a 62-second short, and a 66-second
piece, each cut by the app's own assembler from takes it rendered. The demo video walks a still
from an image, to a character, to a shot, to a cut piece.

MIT licensed. The bundled Inter font is under the SIL Open Font License (`assets/fonts/OFL-Inter.txt`).
