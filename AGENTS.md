# forge-lab — AGENTS.md

**More than one agent edits `forge_lab.py`. Read this before you touch it.**

## The file

- `forge_lab.py` is a single ~7,400-line file and TWO agents edit it. Never rewrite it
  wholesale, and never reflow or reformat a region you are not changing — it will collide.
- Do not "tidy up" a region you don't own, even if it looks like it wants tidying.

## Editing rules

1. **Content-anchored patches only.** Assert the anchor matches exactly once, and abort the
   entire edit if it doesn't. Never patch by line number — line numbers move under you.
2. **Stability check before and after each write:** stat size + mtime, sleep 2–3 s, re-stat. If
   it moved, another agent is mid-write — stop and retry rather than writing into it.
3. **Verify after every change:** `wan_matrix.py`, `v2v_matrix.py` and `character_matrix.py`
   (all GPU-free), plus `py_compile` — then, for UI work, actually launch the app.
   ⚠️ **Run the Tk-touching suites with `/opt/homebrew/bin/python3`, NOT `python3`.**
   `/usr/bin/python3` is 3.9 with **Tk 8.5, which has NO PNG support at all**, so
   `v2v_matrix` reports 3 spurious `thumb_photo` failures on ANY revision — including a clean
   HEAD. Homebrew's python3.14 ships **Tk 9.0** and the suite is green. Cost an hour of
   chasing a "regression" that was never there. The app itself is unaffected; only the suite
   invocation was wrong.
4. **Commit per slice.** The commit messages ARE the coordination channel: `git log` is how the
   other agent learns what moved. Say what changed, what you verified, and what you did NOT.
5. **A test is not evidence until it has been seen to FAIL.** When you add an assertion, break
   the exact thing it guards and confirm it goes red before you trust it.
6. Check the physical thing, never a summary of it: read the file, run the command, call the
   function. A success message is not a result.

## Region map (as of 2026-09-27)

- `~944–1140` — `estimate_seconds`, `submit`: engine dispatch, uploads. Shared; anchored patches only.
- `~1500–1950` — the two v2v engines: `ltx_graph` / `ltx_rules` / `animate2_graph` /
  `animate2_rules` and their measured constants.
- `~4190–4700` — UI construction, `open_long_video_workspace`, long-video/storyboard work.
- `tools/*_matrix.py` — the GPU-free assertion suites. Extend them; don't loosen them.

## Hard-won specifics

- **LTX-2 frame counts must be `8n + 1`** (65 / 97 / 121 / 129 / 257…). Official, from three
  independent sources: Lightricks' LTX-2 model card ("frame count must be divisible by 8 + 1"),
  the LTX docs ("Length: 97 frames | frame count must be 1 + a multiple of 8"), and the shipped
  template itself (`EmptyLTXVLatentVideo length = 97`). Resolution must be divisible by 32 —
  and **64 on the IC-LoRA path**, because `reference_downscale_factor=2` applies to LTX's /32
  latent. A length that violates this fails or degrades with no useful error.
  ⚠️ **But `8n+1` compliance alone proves nothing.** 73 satisfies it and is still the wrong
  length: the bug was PROVENANCE, not legality — `ltx_graph()` took the latent length from
  `["dsize", 2]` (the MoGe DEPTH image's frame count) instead of from the control clip. Assert
  where the number COMES FROM, not just that it is legal.
- **A shifted widget value is invisible to every other check.** The graph still validates against
  `/object_info`, the class types exist, the render succeeds — and the clip is subtly wrong. Run
  `tools/flatten_fidelity.py TEMPLATE.json FLATTENED.json --ssh ryan@forge` before trusting any
  flattened graph. (`--ssh` is the SSH target; `forge:8188` is the ComfyUI URL, not a host.)
- **Engine dispatch order is a bug class.** `ltx` and `animate2` both carry `video=True`, so a
  missing branch sends them into H3's curve and quotes a confident wrong estimate.
- **Values are measured, not guessed.** Each engine's timings were measured on the 3090 and the
  fits live next to the estimates. A constant that was never measured says so in its comment.

## Don't

- Don't route around a blocked write. Silence on a timed-out approval is not consent.
- Don't post anything public (issues, upstream reports) without the user reviewing the draft.
- Don't bake a template value "verbatim" without recording where it came from — and check it
  against the template with `tools/flatten_fidelity.py`.