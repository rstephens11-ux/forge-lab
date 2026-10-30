# forge-lab — AGENTS.md

**More than one agent edits this repo. Read this before you touch anything.**

Last refreshed 2026-10-16 against `forge_lab.py` @ 16,761 lines (commit `d140802`).
Everything below was re-derived from the file as it actually is — not carried over.

## What the app is

`~/forge-lab/` — Ryan's **tkinter desktop app**, built into `/Applications/Forge Lab.app`
with PyInstaller. It drives ComfyUI on the rig `forge` (`http://forge:8188`). It is NOT one
file any more:

| file | lines | what |
|---|---|---|
| `forge_lab.py` | **16,761** | the window + engine graphs + estimates + the app's whole UI |
| `director.py` | 2,896 | rig-side LLM: `Director.dialogue()`, `validate_dialogue()`, `format_dialogue()` |
| `dialogue_ui.py` | 1,879 | the DIALOGUE window + PURE `plan_render(pj, voices, mode, engine)` |
| `dialogue.py` | 1,125 | Tk-free dialogue engine: script parse, voice bank, assembly, estimate |
| `studio_pipeline.py` | 1,096 | the STUDIO window's script→scene pipeline |
| `charpack.py` | 573 | character asset packing |
| `song.py` | 344 | YuE2 song/cover engine |
| `forge_core.py` | 89 | shared rig plumbing |
| `jobqueue.py` | ~410 | **the rig's ONE queue** (order/pause/delete/card footprints, `one_of`) |
| `dispatch.py` | ~430 | **the scheduler**: lifts a job off that queue onto a lane (Tk-free) |
| `home_queue.py` / `home_page.py` | 180 / 174 | the Home tab: queue panel, takes feed, machine room |

⚠️ **The Mac copy is the ONLY canonical one.** The rig carries a **STALE MIRROR** at
`~/forge-studio/forge_lab.py` on forge (~14.5k lines, different lineage, **not a git repo**).
Editing it does NOT change the app. Verify by line count before editing if ever unsure.

⚠️ **The rig's ComfyUI models dir is a SYMLINK**: forge's `ComfyUI/models` → `/mnt/models/comfy`.
Never `git reset --hard` or `git checkout` over it — the link dies and every model path breaks
(repair: `rm -rf models && ln -s /mnt/models/comfy models`). Record `git rev-parse HEAD` before
any ComfyUI update; the rig can be many commits behind, and a jump can break the `raylight`
custom node and the H3/Qwen pipelines — re-verify both afterwards.

SSH user on the rig is **`name@forge`** — not the Mac username. A bare `ssh forge` fails.

When Ryan reports an error from the running app ("I tried X and got an error"), the **on-screen
wording is usually the only evidence that exists** — the logs rarely carry it. Ask for the exact
text before deep-diving logs. Procedure: the `forge-lab-development` skill →
`references/error-report-triage.md`.

## Editing rules

1. **Content-anchored patches only.** Assert the anchor matches exactly once (`src.count(old) == 1`)
   and abort the entire edit if it doesn't. Never patch by line number — line numbers move under you.
2. **Stability check before and after each write:** stat size + mtime, sleep 2–3 s, re-stat. If it
   moved, another agent is mid-write — stop and retry rather than writing into it.
3. **Verify after every change:** `py_compile`, then the GPU-free suites — at minimum
   `core_matrix.py`, `wan_matrix.py`, `v2v_matrix.py`, `character_matrix.py`, plus the suite for the
   area you touched. For UI work, actually launch the app.
   ⚠️ **Run the Tk-touching suites with `/opt/homebrew/bin/python3`, NOT `python3`.**
   `/usr/bin/python3` is 3.9.6 with **Tk 8.5, which has NO PNG support at all**, so `v2v_matrix`
   reports 3 spurious `thumb_photo` failures on ANY revision — including clean HEAD. Homebrew's
   python3.14.8 ships **Tk 9.0** and the suite is green. The app itself is unaffected; only the
   suite invocation is wrong.
4. **Commit per slice.** Commit messages ARE the coordination channel — `git log` is how the other
   agent learns what moved. Say what changed, what you verified, and what you did NOT.
   ⚠️ HUNKS, never a path (a path consumes the peer's slice).
5. **A test is not evidence until it has been seen to FAIL.** Break the exact thing a new assertion
   guards and confirm it goes red before you trust it.
6. Check the physical thing, never a summary of it: read the file, run the command, call the function.

⚠️ **Re-check stability before assuming you are creating a file.** `.gitignore`, `STATUS.md` and this
file are shared and tracked too — a blind write clobbers the other agent's version, and the "modified
by sibling agent" warning arrives *after* the damage.

### Two small edits that avoid needless collisions in the shared header

- **`re` is NOT imported** in `forge_lab.py`. Don't add it to the header to get a slug or regex —
  hand-roll it. The import block is a shared surface.
- **`messagebox` / `simpledialog` are not imported either.** Import them INSIDE the handler that
  needs them, never at construction time — a construction-time import failure takes the whole window
  down, while a handler-time one costs a single button.

## Region map in `forge_lab.py` (re-derived 2026-10-16)

Line ranges are approximate — **anchor on the symbol name, not the number.**

| lines | contents |
|---|---|
| `359–504` | `kind_label`, `model_help` — mode/engine naming and the help text |
| `578–787` | `turbo_preflight`, `director_polish` |
| `851–1075` | `h3_graph`, `h3_to_raylight` — the H3 video path |
| `1214–1259` | `_png_has_alpha` |
| **`1340–1515`** | **`build_graph` — the engine dispatch. Where a new engine branches.** |
| `1516–1601` | `estimate_seconds` — the estimate-first cost path |
| `1648–1828` | `submit` — uploads + engine dispatch on the way out |
| `1849–2112` | `promote_graph`, `s2v_graph` |
| `2143–2632` | `wan_graph`, `ltx_graph`, `animate2_graph` — the v2v engines + their rules |
| `2960–3187` | `open_compare`, `recover_orphans` |
| `3359–4053` | characters: `delete_character`, `char_still_size`, `roll_weirdo`, `weirdo_video_params`, `compose_character_prompt` |
| `4252–4374` | `_polish_once`, `_ws_once` |
| `4445–4456` | `ENGINES` dict, `H3_CANVAS` |
| `4511–4766` | `long_scene_new`, `audio_peaks`, `scoped_wheel` |
| **`4767–6808`** | **`open_long_video` (2,042 lines) — long-video / storyboard workspace** |
| `6809–7292` | `open_weirdo` |
| `7332–8580` | `studio_text_to_script`, `open_studio` |
| `8581–9282` | `open_characters` |
| `9283–10065` | `open_song` — YuE2 song + cover |
| **`10066–16714`** | **`launch` (6,649 lines) — the bulk of the UI construction and every widget handler** |
| `16715–16761` | `selftest` |

⚠️ `launch()` is where most new UI lands and it is the biggest, busiest region in the repo.
`open_long_video` is second. Anchored patches in these two, always.

## Engine families

**`engine_of(mode)` is the single dispatcher** — returns `"h3" | "wan" | "ltx" | "animate2" |
"qwen" | "krea2" | "flux3"`. New code must use it.

⚠️ **The broad-flag dispatch trap.** `ltx` and `animate2` both carry `video=True`. Every consumer
that tests that broad flag must check the **specific engines FIRST**, or the engine silently
inherits H3's rules — nothing raises, nothing logs, the numbers are just wrong. Six sites were found
in one session: `estimate_seconds()` (quoted 71.3 s for a run measured at 102.1 s), `submit()`,
`_ref_cap()` (offered 9 stills to an engine that takes 1), `_ref_help_text()`, `dim_lbl`, `params()`.
Module-level sites are fixed; **the UI sites inside `launch()` still test the broad flag — adding an
engine still means grepping `get("video")` in `launch()`.**

### Image engines — `MODEL_CHOICES["Image"]` is no longer Qwen-only (added 2026-10-15)

| mode | engine | graph shape |
|---|---|---|
| t2i / edit / bg | qwen | `TextEncodeQwenImage21` (prompt + negative + resolution, emits a latent) |
| krea2, krea2raw | krea2 | plain `CLIPTextEncode` ×2 → `KSampler` |
| flux3 | flux3 | the same plain shape, 16 steps |
| flux3edit | flux3 | `ReferenceMultiLatent` supplies positive/negative/LATENT — no width/height, canvas follows ref 1 |

- **Adding an image engine means editing SIX sites.** `build_graph()` branches on
  `MODES[mode].get("engine")`, NOT on node ids. A new engine needs: a MODES entry carrying `engine`
  (+ `default_steps`), a graph template, a `build_graph` branch, and keys in `engine_of()` /
  `engine_load_s()` / `estimate_seconds()` — or the estimate raises.
- **Dotted keys bite twice**: `images.imageN` (autogrow) and `resolution.megapixels` (a dynamic-combo
  sub-input). A nested dict VALIDATES and is then silently DROPPED. Note flux3's autogrow is
  **0-BASED** (`image0`), unlike Qwen's 1-based `image_N`.
- LoRAs: the shared LORA row is shown+enabled for krea2/flux3 too; `build_graph` inserts
  `LoraLoaderModelOnly` when `p["lora"]` is set. POLISH PROMPT picks `PE_MODELS["flux3"]` for flux3.
- Weights on the rig: krea2 `*_int8_convrot` + `qwen3vl_4b_fp8_scaled` + `qwen_image_vae`; flux3
  `flux3_image_klein_sd_int8_convrot` + `qwen3vl_4b_int8_convrot` + `flux2-vae`.
- ⚠️ **Pick the build the CARD can run.** `int8_convrot` / `fp8_scaled` run on RTX 30-series;
  **NVFP4 is Blackwell-only** — a smaller file that cannot run is not the faster choice.

### Video engines

`h3` (MiniMax H3, 24fps, LoRA, audio guide — the main path), `wan` (WAN 2.2 S2V, 16fps),
`ltx` (LTX-2), `animate2`. Constants are **measured on the 3090** and live next to the estimates;
a constant that was never measured says so in its comment.

### Voice engines (DIALOGUE) — added 2026-10-14

Step 5 MAKE has a **VOICE ENGINE** dropdown: `Qwen3-TTS` (default) or `Fish S2-Pro`.
`pj["engine"]` persists per project. Two structural facts drive the design:

1. **Fish has NO VoiceDesign.** A fish voice IS a reference wav + its transcript — so the DESIGN
   batch always stays on the qwen rig under both engines. Fish only ever clones.
2. **A fish mood is an INLINE `[tag]`** (`[whisper]`, `[angry]`), not a reference sample. Fish keys its
   job by `<slug>`; qwen keys `<slug>:<mood>`. Fish does NOT cap moods to designed samples — that cap
   is a **qwen-era rule** and applying it to fish was a bug the tests caught.

⚠️ **Licence differs and it matters commercially:** S2-Pro is **Research/Non-Commercial only**.
Drama 3 is the commercially-licensed one (free under $500k/yr) and is **NOT wired** — 35B, needs
≥48 GB FP8 = both 3090s with ComfyUI stopped. If dialogue ever feeds a paid ad or client work,
that's the line. Full detail: the `forge-lab-development` skill →
`references/dialogue-voices-section.md`; stack facts in `local-speech-models`.

### YuE2 (SONG window) — added 2026-10-09

Song + COVERS in their own window. YuE2 is a **music** model — it cannot read a spoken line.

## Hard-won specifics

- **LTX-2 frame counts must be `8n + 1`** (65 / 97 / 121 / 129 / 257…), from three independent
  sources. Resolution divisible by 32 — and **64 on the IC-LoRA path**.
  ⚠️ **`8n+1` compliance alone proves nothing.** 73 satisfies it and is still wrong: the bug was
  PROVENANCE — `ltx_graph()` took the latent length from the MoGe DEPTH image's frame count instead
  of from the control clip. **Assert where the number COMES FROM, not just that it is legal.**
- **A shifted widget value is invisible to every other check.** The graph validates against
  `/object_info`, the class types exist, the render succeeds — and the clip is subtly wrong. Run
  `tools/flatten_fidelity.py TEMPLATE.json FLATTENED.json --ssh name@forge` before trusting any
  flattened graph. (`--ssh` is the SSH target; `forge:8188` is the ComfyUI URL, not a host.)
- **Engine dispatch order is a bug class.** See the broad-flag trap above.
- ⚠️ **A new rig script must be named in `Forge Lab.spec`'s `datas` too.** The `audio/rig_*.py` files
  ship as DATA (the client copies them to forge), not compiled modules. Miss it and the installed
  .app deploys 5 of 6 rig scripts and every fish render dies on the rig — while the Mac still looks
  green. Verify with `find "dist/Forge Lab.app" -name "rig_*.py"`.
- **A driver must leave NO footprint on Ryan's state.** `_autosave()` runs during `launch()` — before
  any driver has done anything — so a driver run silently rewrote his real `settings.json` and his
  window came back `200x200`. Guarded by `FORGE_LAB_NOAUTOSAVE=1`; verify by hashing the file across
  a driver run, don't assume.
- **A driver that hardcodes a label silently no-ops and then prints a plausible state.**
- **A GREEN GPU-FREE SUITE CAN SIT ON TOP OF A BROKEN WINDOW.** "App hangs on startup / won't open
  anything" — suspect a **TRANSPARENT PNG** first.

## Test suites (`tools/`)

20 `*_matrix.py` suites + 3 standalone test scripts, all GPU-free. Run the **neighbouring** drivers
after touching a shared helper, and always the one for the area you changed. Core set:
`core_matrix.py` (proves refactors byte-identical against `git show`), `wan_matrix.py`,
`v2v_matrix.py`, `character_matrix.py`, `ui_matrix.py`.

Others: `audio_`, `beat_`, `cards_`, `charpack_`, `dialogue_`, `dialogue_ui_`, `director_`, `net_`,
`overnight_`, `pipeline_`, `render_`, `scene_`, `song_`, `takes_`, `weirdo_` `_matrix.py`, plus
`h3_audio_test.py`, `s2v_test.py`, `seamless_test.py`.

⚠️ **Extend them; don't loosen them.** A test that inherits state from the test before it is a test
that lies — reset what you depend on at the start of the block. And **an assertion that PERFORMS the
effect it is checking is a test that lies too**: the side effect under test must come from the code
under test.

⚠️ **Attributing a FAILING suite — use a git worktree, never a stash.** Stashing the other agent's
uncommitted slice is exactly the loss this file exists to prevent:

```bash
git worktree add /tmp/fl-head HEAD   # clean checkout; working tree untouched
cd /tmp/fl-head && /opt/homebrew/bin/python3 tools/v2v_matrix.py
git worktree remove /tmp/fl-head
```

## Don't

- Don't route around a blocked write. Silence on a timed-out approval is not consent.
- Don't "tidy up" a region you don't own, even if it looks like it wants tidying.
- Don't rewrite the file wholesale, or reflow a region you aren't changing — it will collide.
- Don't post anything public (issues, upstream reports, the public repo) without Ryan reviewing the draft.
- Don't bake a template value "verbatim" without recording where it came from — and check it against
  the template with `tools/flatten_fidelity.py`.

## The queue and the dispatcher (2026-10-22)

Every GPU job goes through ONE queue in front of TWO ComfyUI lanes. Nothing is ever refused
for a busy card: it waits, visibly, on the Home tab. **If you add a window that renders, put it
through the queue** — see the two shapes below.

- **`jobqueue.py` is the queue, `dispatch.py` is the scheduler.** The queue owns order, pause,
  card footprints and persistence; the dispatcher owns the ~3 s loop, "which lane has room this
  second", claiming a card and running the job. Keep that split — it is what lets the ordering
  rules be tested with no Tk and no GPU (`tools/queue_matrix.py`, `tools/dispatch_matrix.py`).
- **The GATE shape** — for a window that already watches its own run (the Make tab, the Song
  window): `g = dispatch.admit_render(mode, p, …)` → `g.wait(report)` (blocks the window's
  worker while it waits) → submit with `p["port"] = g.port` → `g.done()` when the take finishes.
  The entry holds the card for the whole render, so `done()` belongs where the run ENDS, not
  where submit() returns. A gate entry with no live gate (a restart) is cancelled, never started.
- **The `dispatch.render()` shape** — for a Tk-free engine module (`quick.py`,
  `studio_pipeline.py`, `places.py`, `charpack.py`): `meta = dispatch.render(title, mode, p, refs)`
  replaces `submit()` + `wait_and_collect()` and returns the collected take. With no scheduler
  running it submits straight through, so tests and headless scripts are unaffected.
- **`one_of`** is the shape "a ONE-card job, any card": `new_entry(..., one_of=[0, 1])` with
  `cards=[]`. `cards=[0, 1]` means the OPPOSITE (both are needed, Turbo). The scheduler calls
  `claim_card(jid, card)` to pin it to the card it took — WITHOUT that call the entry keeps
  claiming nothing and a second job can be handed the same card.
- **A lane is free only when its own `/prompt` says nothing is running or waiting on it.** A
  render started from another program is not in this queue but IS on the card.
- **Room is asked per job, not per lane:** `cards.room_now(card, need)` reads the rig, frees an
  idle tenant (Ollama, a stray) and NEVER unloads the lane's own warm model — but it also never
  raises, so a refusal leaves the job waiting instead of failing it. `make_room()` stays the
  raising version used on the direct-submit paths.
- **The queue's own claims must be subtracted from capacity inside a tick** (`set(cap) -
  q.cards_busy()`), or two jobs in the same pass land on one card. There is a test for exactly
  that, and a pass cap so a broken rule can never hang the scheduler thread.
- **`p["port"]` in a submit's params PINS the lane** (`_route_for` honours it). Only the
  dispatcher sets it: it has already checked that card, and re-picking would double-book.
- **A RUNNING entry in a file being LOADED is stale** (`jobqueue.load` marks it failed): its
  worker died with the last process, and left alone it would claim its card for ever.
- **Lane B is only offered when CARD MODE is `both`** (`cards.route()`, cards_settings.json).
- ⚠️ **The dispatcher does NOT autostart in a driver run** (`if not _DRIVER`). Drivers fake the
  lanes instead: `FORGE_LAB_TESTDISPATCH` (fake lanes), `FORGE_LAB_TESTMAKERUN` (the Make tab's
  RUN press, fake), `FORGE_LAB_TESTREAL` (real renders, needs BOTH mode).
- ⚠️ **`dispatch.enqueue()`/`render()` use the process-wide queue** (`jobqueue.app_queue()`), the
  same object the Home panel reads. Never build a second Queue for the app data dir.
- ⚠️ **`dispatch.render()` tests the PROCESS-WIDE scheduler** (`running()`), so a test that wants
  the queue path must start THAT one with fake lanes (`dsp.PROBE["fn"]`/`ROOM["fn"]` + `start()`);
  a private Dispatcher instance leaves render() taking the direct path — which in a "GPU-free"
  suite means a real prompt lands on the rig (seen: a 64px render while writing the test).

### A panel that is BUILT is not a panel that is SHOWN

`home_queue.build_queue()` and `cards_ui.build_panel()` return their frames and the OWNER packs
them. `home_page.build()` called both and packed neither, so the Home tab shipped with an
invisible queue and an invisible machine room — and the driver passed, because
`winfo_children()` sees unmapped widgets. Assert what a person sees:
`w.winfo_ismapped() and w.winfo_height() > 60 and w.winfo_width() > 60`.

## Windows + modules added 2026-10-17 (the "Higgsfield / slop-cannon" push)

Every window below is **Tk in `<name>_ui.py` + a Tk-free engine** with its own GPU-free suite.
They all submit through `forge_core` / `studio_pipeline`, so the card manager, Takes and the
busy-card prompt apply unchanged. Drivers: `FORGE_LAB_TEST<NAME>=1` (add `FORGE_LAB_SHOTDIR=dir`
to TESTCHAR3 / TESTQUICK / TESTAPPS for screenshots).

| window (sidebar) | Tk | engine | suite / driver | what |
|---|---|---|---|---|
| **Quick** (MAKE, first) | `quick_ui.py` | `quick.py`, `formats.py` | `quick_matrix` / TESTQUICK | one idea -> a finished video: cast -> (looks) -> voices -> script -> lines -> set -> shots -> cut. 8 formats incl. **Music video** (lyrics -> YuE2 song -> sung shots sliced to H3 clip length). Jobs in `DATA/quick/*.json`, resumable; check-each-step reviews; copies; Draft/Final |
| **Apps** (MAKE) | `apps_ui.py` | `apps.py` | `apps_matrix` / TESTAPPS | one-click: talking photo, make it move (8 camera moves), outfit swap, new angle, place, expression, restyle, product in hand, make it vertical (local ffmpeg) |
| **Voices** (BUILD) | `voices_ui.py` | `voicelab.py` | `voicelab_matrix` / TESTVOICES | build a voice (74 accents, sliders) -> audition 3 -> keep; clone a clip; test in Qwen AND Fish; Fish direction; link to a character |
| Characters: **Angles**, **Voice** tabs | `characters_ui.py` | `charpack.py`, `voicelab.py` | TESTCHAR3 | the multi-angle sheet (feeds every r2v shot) + one-click voice |
| Dialogue: **3 steps** | `dialogue2_ui.py` | `scene_engine.py` | TESTDIALOGUE3 | 1 Lines -> 2 Voices -> 3 Make & play |
| Episode: **the set**, camera chips | `episode_ui.py` | `studio_pipeline.make_set`, `set_camera`, `render_many` | `pipeline_matrix` / TESTEPISODE | a two-hander is shot on ONE plate, built ONE PERSON PER PASS (both stills in one edit blended the people) |

Later the same day: Quick **one video per line** (several ideas, one per line, ticked ->
one job each), **CAPTIONS** looks (`studio_pipeline.CAPTION_LOOKS`: Clean / Pop — the word
being said lights up / Big — one word at a time; `pj["caption_look"]`, old projects stay
Clean; chips in Quick and in Episode CUT), and **MORE LIKE THIS** on a finished run
(`quick.again(job, "script" | "final")`: another script with the same cast, or the SAME
script/voices/set re-shot at Final as a `<slug>-final` Episode copy — the Draft is kept).

### After the review panel (2026-10-17 evening)

Six reviewers (a new user, a motion designer, a cartoonist, a director, a comedy writer and
a short-form editor) went over the UI and two real runs. Their findings are summarised in
the claude.ai project doc `review-2026-10-17-panel.md`. What landed:

**THE EDIT (`studio_pipeline.assemble`)**
- Word times come first: aligned on the rig, or estimated from the voice's real speech span.
- Each clip is trimmed to its words using a **PACE**: `PACES` snappy / natural / breathe /
  full, set per format in `formats.pace`. A line whose direction says "beat" keeps a pause.
- The voice is trimmed by the same amount (`atrim`), so lip sync holds.
- Frames are filled by crop with lanczos (no more pad bars).
- Camera moves are done in post with `zoompan` (`camera_moves`, `_move_filter`, `speaker_x`):
  eased pushes, a punch-in on the last line's last word.
- After the button, the cut goes to the listener's reaction shot (`add_reactions`,
  `is_reaction` — NOT `reaction`, which is each line's acting text; `line_shots()` excludes
  reaction shots).
- `sound_design`: room tone, plus a whoosh before the punch-in.
- The music bed ducks under the voices (formats pinned; a plain mix is the fallback).
- `loudnorm_af` does a two-pass loudnorm to -14 LUFS.

**CAPTIONS (`build_ass`)**
- 9:16 captions: 96 px, MarginV 700, two lines of about 16 characters (`caption_chars`).
- Font is `CAPTION_FONT`.
- The Pop look scales Y only.
- Timing: a 2-frame lead, held across gaps under 0.4 s, never overlapping, clamped to the
  shot's cut.
- An optional **hook** line: `pj["hook_text"]`, top box, 0-2.5 s.

**THE SET**
- The wide is shot at 28 mm with deep focus.
- **Clean** close-ups: the speaker ALONE, 85 mm, eyes toward the others' side
  (`_look_dir`), built from [the wide, their own still].
- `_only_theirs()` names whose gag and clothes are whose.
- `_gag()` drops similes ("like heavy wet towels" drew a towel).
- Every shot names each gag to keep.
- Acting per line comes from `quick.stage_directions`, applied by `sp.apply_acting`.

**WRITING (`writers_room.py`, Tk-free, suite `room_matrix`)**
- The steps (the game step retired 2026-10-24 - see THE WRITER below): game, then 3 drafts written button-first, then each scored alone on a rubric
  (with penalties for ignoring the idea and for banned lines), then punch-up, then a cut
  that keeps the hook and the button.
- `formats.CRAFT` and each format's shape come from the comedy writer.
- Invented characters play `quick.idea_roles` (odd one first). They have
  pov / want / blind_spot / habit (weirdos), and the second one clashes with the first.

**THE WRITER, after the bake-off (2026-10-24; claude.ai project doc `writer-bakeoff-2026-10-23.md`)**
- `director.DEFAULT_MODEL` is **`gemma4:31b`** (blind panel: 6.53 vs Qwen3.8 27B 5.45 vs the old
  uncensored Qwen 5.3-5.75). Fallback unchanged (the 9B, for when the card is busy).
- Drafts are **PLAN FIRST** (`writers_room.DRAFT_ASK`: situation / joke / three concrete
  escalations / button, then the lines, plain words, no jargon). The separate GAME step is
  retired (`find_game` kept, unused). Quick writes from `formats.writer_brief` (shape + idea +
  `IDEA_FIRST`); the one-shot fallback keeps `formats.brief` (+ CRAFT).
- **THE IDEA beats the format** (`formats.IDEA_FIRST`, in both briefs): a format is the look
  and tone; who is there and what happens is the idea's.
- **VOICE-ONLY speakers** (`quick.idea_cast` -> `job["voice_roles"]`): an AI doorbell, a taxi,
  an intercom - heard, never drawn. `step_voices` designs each a voice (`design_voice`, slug in
  `voice_slug`); the script marks it VOICE ONLY; `sp.from_dialogue(voices=)` films its lines as
  the on-screen person LISTENING (`voice_only`, `voice_action`, intent character_action, no
  guide audio, no `<d>` line in the H3 prompt); `_mix_audio` lays the voice in like any line.
  Long takes stop at a voice-only line; `vary_sizes` never picks one.
- **No sounds or actions as lines:** `director.noise_line` in `validate_dialogue` rejects
  "Woof!" / "*wags tail*"; `quick._spoken` moves any *action* left in a line to its direction.

**PLACES (`places.py` + `places_ui.py`, suite `places_matrix`, driver TESTPLACES)**
- Kept locations live in `DATA/places/<slug>/`.
- `pj["place"]` builds the set IN that picture: it is `<image2>` in pass 1, and the set is
  rebuilt when the place changes.

**FRONT DOOR**
- Quick opens at launch (settings `open_on`, never in a driver run). A big sidebar button.
- The header reads "Computer: ready / busy / not connected"; the CARDS panel is behind
  "Show the machine room".
- Quick's left column scrolls and Make it is pinned.
- "More options" holds length / look / versions / captions / pace / hook / reactions /
  checks.
- Steps are named as a story; the cast shows as faces.

**Other new pieces**
- `TESTTOUR` screenshots every window.
- On macOS the secondary buttons use dark text (`_MAC`): Aqua ignores `bg`.

⚠️ **New modules must be named in `Forge Lab.spec` `hiddenimports`** — they are imported inside
functions, so PyInstaller won't find them on its own (formats, quick, quick_ui, apps, apps_ui,
voicelab, voices_ui, writers_room, places, places_ui are listed).

⚠️ **`apps.py`, `quick.py` and `studio_pipeline.py` import `forge_lab` as a module** — in the
app that is a SECOND copy beside `__main__`. Drivers that fake the rig must patch
`forge_core.submit` / `wait_and_collect` (what these engines call), not `__main__`'s globals.

### Overnight 2026-10-17/18

- **TABS (`dock.py`, suite `dock_matrix`, driver TESTSIDEBAR).** Every workspace opens as a
  tab in the main window. `dock.window(root)` replaces `tk.Toplevel(root)` in each workspace:
  it returns a `DockedWindow` (a Frame answering title / lift / protocol / destroy). ⚠️ A
  Frame is NOT in its children's bindtags: key and wheel bindings made on the window go
  through `DockedWindow.bind`, which routes them via ONE root binding to the shown tab.
  Bind structure events (`<Configure>`, `<Destroy>`) as usual. Dialogs stay real Toplevels.
  Settings `"tabs": false` gives the old windows back. A driver that interacts with a
  workspace after opening another must `w.lift()` it first (hidden tabs are unmapped).
- **Take check:** `studio_pipeline.qc_shot / qc_shots` (ffmpeg: short / frozen / black;
  `Director.qc_frame` vision read when the model takes images), `quick.flagged / reroll /
  ok_shot`; Quick's done panel lists the flagged shots. Nothing rerolls without Ryan.
- **People, not weirdos:** Quick invents ordinary people (`weirdos.invent_person`,
  `compose_person`) unless the More options box or the idea asks (`quick.wants_weirdos`).
- **Long takes:** one-person videos merge lines into takes up to ~15 s
  (`merge_long_takes`, "jumps" camera move); a single line over 8 s gets a long shot.
- **`_normalise` seeks BEFORE `-i`** (zoompan's in_time is trimmed time). Don't move it back.
- **`new_project` never overwrites:** a used title gets `-2`. `make_set(resume=True)` keeps
  plates already made; each pass is saved.
- **`thumb_photo` caches** by (path, size, mtime, box); never decode a picture per repaint.
- **Captions** use Inter Black from `assets/fonts` (OFL), passed to libass with fontsdir
  (local and on the rig). New formats: doorbell (`POST_LOOKS["cctv"]` + a stamp), nature,
  ifvlog. Post copy adds `#aigenerated`.
- **Worker threads never call `win.after()` / `root.after()`.** They call
  `forge_lab.ui_later(widget, fn)`; `start_ui_pump(root)` (in launch) runs it on the Tk
  thread. `ui_matrix` §5 fails on any `win/root.after(0, ...)` inside a worker body.
- **A second scrolling canvas in one window:** `scoped_wheel(win, canvas, add=True)`
  (without `add` it replaces the first canvas's handler).
- **Quick, after a video is done:** `quick.history(query)` (All my videos),
  `quick.recut(job, captions, pace, hook)` (cut again, no renders; the old mp4 is kept in
  `job["old_cuts"]`), `quick.shot_list(job)` + `reroll` (re-film any shot),
  `music.pick_for(idea, fmt)` (MUSIC "Pick for me"), `quick.guess_format/guess_seconds`.
- **Shot sizes:** `studio_pipeline.is_wide(sh)` (shot 1 or `sh["wide"]`) decides the wide
  plate everywhere; `vary_sizes(pj)` puts one mid-scene line of a 5+ line two-hander back
  on the wide. Don't test `sh["n"] == 1` for "the wide" any more.

### Sunday 2026-10-18 (the roadmap: claude.ai project doc `review-2026-10-18-roadmap.md`)

Each piece is a Tk-free module + its own GPU-free suite (all listed in `Forge Lab.spec`
hiddenimports):

| module | suite | what |
|---|---|---|
| `export_pack.py` | `export_matrix` | Quick "Export for posting": tall / square / wide (other shapes are RE-CUT by `assemble` at that size: square = crop aimed high, the opposite orientation = blur-fit), clean cover + hook cover, .srt, caption, licence.txt |
| `nightshift.py` | `nightshift_matrix` | Quick "+ add to tonight's list" / "Tonight": unattended queue, retry once, `caffeinate`, an HTML contact sheet per night |
| `previews.py` | `previews_matrix` | moving thumbnails: Make it move camera moves (of the picked picture), your format loops, live caption-look previews (`Loop`, `CaptionLoop`) |
| `series.py` | in `quick_matrix` §11 | the show bible (cast / format / place / tone / rules / gags / episodes); `job["series"]` puts it in the writer's brief |
| `licences.py` | `licence_matrix` | the PAID WORK switch: `audio/client.tts_batch` refuses Fish (non-commercial) first |
| `beats.py` | `beats_matrix` | pure-Python beat tracking; music videos cut on bar lines (`sh["slice_len"]`) |
| `finish.py` | `finish_matrix` | local last pass: sharper / film grain / smooth motion -> `<video>_finished.mp4` |
| `sfx.py` | `sfx_matrix` | one sound per line from the acting pass (`sh["sfx"]`); your DATA/sfx files win over the built-ins |

Edit-code fields `assemble` now honours: `pj["fit"]` / `pj["crop_y"]` (export re-cuts),
`sh["skip"]`, `sh["nudge_in"]` / `sh["nudge_out"]` (Episode Cut page TIMELINE),
`sh["slice_len"]` (music videos), `sh["sfx"]`; `OUT_SIZE["1:1"]`. Quick also gained:
Product -> ad (`product_pass`, honest `product_brief`, `HOOK_ANGLES`), stars
(`rate`, `taste_notes` in the brief), `undo_recut`, Cmd/Ctrl+Return. `save_settings` MERGES
(it used to drop "tabs" / "open_on" on every autosave).

- The public GitHub copy (`rstephens11-ux/forge-lab`) is a SCRUBBED export: code + GPU-free
  tests only, character names replaced. Never push the private repo there, and never push
  anything without Ryan reviewing it.

## Deeper detail lives in the skill, not here

The `forge-lab-development` skill holds the long-form knowledge — engine constants, episode
lessons, the app-review checklist, per-area procedures. This file is the short version for whoever
is about to edit the repo. When in doubt, read the skill.
