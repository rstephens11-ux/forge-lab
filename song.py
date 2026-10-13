"""song.py — YuE2 song generation for Forge Lab.  Tk-FREE, stdlib only.

WHY A MODULE: `forge_lab.py` is a 14k-line single-file tkinter app shared with a second
agent. New engine work goes in a Tk-free module beside it (same shape as `director.py`,
`charpack.py`, `studio_pipeline.py`) so the app can import it inside a function and the
window stays the only thing that touches tkinter.

WHAT YUE2 IS: a 3B open-weights music model (HKUST / M-A-P, Sept 2026) — style prompt +
lyrics -> a full song with vocals and accompaniment, 48 kHz stereo. It first writes a
symbolic plan in ABC notation (melody, optionally chords), then renders audio from it.
The plan is what makes it steerable and it is what the cover workflow reuses.

THE GRAPH is ComfyUI's NATIVE YuE2 support (core since v0.35.0; forge runs 0.37.0) — no
custom node. Node-for-node it mirrors Comfy-Org's `audio_yue2_text2music` template, which
is itself a SUBGRAPH; these are the internals of that subgraph in API form. Do not
"improve" the sampler: the template pins dpm_2 / sgm_uniform / cfg 1.0 / 32 steps, and
cfg>1 only applies to the no-plan path (see YUE2_MUSIC_DEFAULTS below).

WEIGHTS (Comfy-Org/YuE2, CC BY-NC 4.0 with an individual-creator carve-out — you may sell
your own songs; a COMPANY using the weights commercially needs a separate licence):
  checkpoints/yue2_3b_int8_convrot.safetensors  3.96 GB  <- the one the docs use
  checkpoints/yue2_3b_bf16.safetensors          7.80 GB  (full precision, alternative)
  audio_encoders/sheetsage2_bf16.safetensors    1.39 GB  (only needed for COVERS)
"""

import random

# --------------------------------------------------------------------------- registry
YUE2_CKPT = "yue2_3b_int8_convrot.safetensors"
YUE2_CKPT_BF16 = "yue2_3b_bf16.safetensors"
YUE2_SHEETSAGE = "sheetsage2_bf16.safetensors"

YUE2_PLAN_MODES = ("full", "melody")          # "off" is not a mode here — see yue2_graph()
YUE2_PLAN_LABEL = {"full": "full (melody + chords)", "melody": "melody only"}

YUE2_DEFAULT_SECONDS = 120.0                  # what the ComfyUI template ships with
YUE2_COVER_SECONDS = 360.0                    # what the cover template uses
YUE2_MIN_SECONDS = 10.0
YUE2_MAX_SECONDS = 900.0                      # the node's own max
YUE2_TOKENS_PER_SECOND = 25.0                 # semantic tokens per second of duration

# Node defaults, copied from the Comfy-Org template so a run here IS that workflow.
# ⚠️ These are the template's, not invented: ABC 8192/0.7/0.9/30/1.005/100 and
# music 1.0/0.95/100/1.2. cfg_scale is deliberately ABSENT (its default 1.0 disables
# guidance, which is what the ABC templates use; 1.01 only matches the pre-option
# behaviour of the no-plan path).
YUE2_ABC_DEFAULTS = {"max_abc_tokens": 8192, "temperature": 0.7, "top_p": 0.9,
                     "top_k": 30, "repetition_penalty": 1.005, "penalty_window": 100}
YUE2_MUSIC_DEFAULTS = {"temperature": 1.0, "top_p": 0.95, "top_k": 100,
                       "repetition_penalty": 1.2}
YUE2_SAMPLER = {"steps": 32, "cfg": 1.0, "sampler_name": "dpm_2",
                "scheduler": "sgm_uniform", "denoise": 1.0}

# --------------------------------------------------------------------------- cost law
# CALIBRATED on forge against REAL renders, 2026-10-09. One song at a time; ONE 3090 is
# enough (peak VRAM ~11 GiB). Every row below is a real run — the produced duration is
# ffprobe's number on the delivered flac, not the number that was asked for:
#
#   asked  plan  produced  wall   note
#    45 s  full    45.0 s   17.7   first run of the session (warm page cache)
#   120 s  full    67.8 s   23.0   ENDED EARLY at the model's own end token
#    60 s  full    60.0 s   25.2   COLD: ComfyUI restarted + vm.drop_caches=3
#    90 s  full    90.0 s   25.2   warm
#   240 s  full    67.4 s   20.2   ended early even with 3x the lyrics
#    60 s  off     60.0 s   15.1   warm
#
# Fitted on the rows that actually filled their request — 45 s (17.7) and 90 s (25.2) warm,
# and 60 s (15.1) with planning off — which put the slope at 0.167 s per second of song
# (≈6x real time) and the fixed term at 10 s (5 s with no plan pass). It then predicts the
# 120/240 rows to 23.0 s (it RAN 23.0) and 20.2 s (it RAN 20.2) once you use what they
# actually produced instead of what they asked for.
#
# THE SHAPE IS THE POINT, and it is the OPPOSITE of the video engines': the FIXED cost
# dominates and duration is a small term. A longer song is cheap and a SHORT one does not
# get much cheaper — a "15 second song" is not instant, it is ~12 s of GPU.
#
# ⚠️ Built from the REQUESTED length, so it is an UPPER BOUND: a song may end at its own end
# token well before it (67.8 s when asked for 120; 67.4 s with three times the lyrics). The
# requested length is the only thing known BEFORE the run. The UI labels the number as an
# upper bound rather than dressing it as exact — do not "fix" that into a promise.
YUE2_LOAD_S = 10.0             # fixed cost, plan=full, warm engine (measured)
YUE2_OFF_LOAD_S = 5.0          # no-plan path: the ABC pass is most of what the 10 s buys
YUE2_SEC_PER_AUDIO_SEC = 0.167  # ≈6x real time; the fixed term is what dominates


def yue2_estimate_seconds(seconds, plan=True):
    """Wall seconds for ONE song, excluding the engine-switch term (the caller adds it)."""
    s = max(YUE2_MIN_SECONDS, min(YUE2_MAX_SECONDS, float(seconds or YUE2_DEFAULT_SECONDS)))
    load = YUE2_LOAD_S if plan else YUE2_OFF_LOAD_S
    return load + YUE2_SEC_PER_AUDIO_SEC * s


# --------------------------------------------------------------------------- the graph
def yue2_graph(style, lyrics, mode="full", seconds=YUE2_DEFAULT_SECONDS, seed=None,
               ckpt=YUE2_CKPT, plan=True, prefix="songs/yue2", fmt="flac"):
    """The API graph for one song. Returns a dict of node_id -> node.

    `plan=True` runs the ABC planning pass (YuE2GenerateABC) and feeds its score into
    YuE2GenerateMusic. `plan=False` passes an empty `abc`, which the node documents as
    "use off mode automatically" — generate straight from style + lyrics, no score.

    ⚠️ When plan is False the ABC node is OMITTED rather than left dangling: ComfyUI
    prunes to what the output nodes need, so an unlinked node would be dead weight in
    every prompt — and a node that cannot be reached is a node that cannot fail loudly.
    """
    if seed in (None, "", -1):
        seed = random.randint(0, 2 ** 31 - 1)
    seed = int(seed)
    if mode not in YUE2_PLAN_MODES:
        mode = YUE2_PLAN_MODES[0]
    seconds = max(YUE2_MIN_SECONDS, min(YUE2_MAX_SECONDS, float(seconds or YUE2_DEFAULT_SECONDS)))
    style = style or ""
    lyrics = lyrics or ""

    g = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "3": {"class_type": "YuE2GenerateMusic",
              "inputs": dict(clip=["1", 1], style=style, lyrics=lyrics,
                             abc=(["2", 0] if plan else ""),
                             seed=seed, mode=mode, max_duration=seconds,
                             **YUE2_MUSIC_DEFAULTS)},
        "4": {"class_type": "ConditioningZeroOut",
              "inputs": {"conditioning": ["3", 0]}},
        # the latent length follows the model's OWN reported seconds (["3", 1]) — that is
        # how a song that ends early gets a short latent instead of a padded one.
        "6": {"class_type": "EmptyYuE2LatentAudio",
              "inputs": {"seconds": ["3", 1], "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": dict(model=["1", 0], positive=["3", 0], negative=["4", 0],
                             latent_image=["6", 0], seed=seed, **YUE2_SAMPLER)},
        "7": {"class_type": "VAEDecodeAudio",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveAudioAdvanced",
              "inputs": {"audio": ["7", 0], "filename_prefix": prefix, "format": fmt}},
    }
    if plan:
        g["2"] = {"class_type": "YuE2GenerateABC",
                  "inputs": dict(clip=["1", 1], style=style, lyrics=lyrics,
                                 seed=seed, mode=mode, **YUE2_ABC_DEFAULTS)}
    return g


def yue2_plan_params(style, lyrics, mode="full", seconds=YUE2_DEFAULT_SECONDS, seed=None,
                     plan=True, prefix="songs/yue2"):
    """The `p` dict the app's submit("song", p) expects. One place, so the window and any
    headless caller cannot drift apart.

    ⚠️ The prefix is "songs/yue2", not the app-wide "forge_lab": ComfyUI reads the part before
    the slash as a SUBFOLDER on the rig, so songs land in forge's output/songs/ instead of
    piling up beside every video, and the delivered file is named yue2_00001.flac — which is
    how you find a song in Takes among 400 video frames.
    """
    return {"prompt": style or "", "style": style or "", "lyrics": lyrics or "",
            "song_mode": mode if mode in YUE2_PLAN_MODES else YUE2_PLAN_MODES[0],
            "plan": bool(plan), "max_duration": float(seconds or YUE2_DEFAULT_SECONDS),
            "seed": -1 if seed in (None, "") else int(seed),
            "prefix": prefix, "steps": YUE2_SAMPLER["steps"]}


# --------------------------------------------------------------------------- COVERS
# "Cover an existing melody in another style" — the model's own second workflow, and the one
# that makes a COVER rather than a pastiche: the melody is TRANSCRIBED from a recording and
# re-performed in the new style, so the tune survives and the arrangement/vocal are new.
#
# The chain is Comfy-Org's `audio_yue2_music_cover` template, node for node:
#   LoadAudio -> SheetSage2AudioToABC(mode="melody") -> YuE2GenerateMusic(mode="melody")
# with the ABC score fed straight into the generator instead of the planner. The ABC
# planner (YuE2GenerateABC) is NOT part of a cover — supplying an outside score is exactly
# what bypasses it, and a cover graph that still contained it would be planning a second
# melody over the top of the transcribed one.
#
# ⚠️ mode="melody" on the TRANSCRIBER is what keeps chord symbols out of the score. The
# model's guide warns that cot=melody "does not remove chord symbols automatically" — that
# is about handing it an EXTERNAL abc file. Ask SheetSage2 for melody-only and the question
# does not arise; tools/song_matrix.py asserts the delivered score really is chord-free
# rather than taking that on faith.
YUE2_SHEETSAGE = "sheetsage2_bf16.safetensors"
YUE2_COVER_DEFAULT_SECONDS = 360.0   # what the cover template ships with


def yue2_cover_graph(audio_name, style, lyrics, seconds=YUE2_DEFAULT_SECONDS, seed=None,
                     ckpt=YUE2_CKPT, encoder=YUE2_SHEETSAGE, prefix="covers/yue2",
                     fmt="flac", show_score=True):
    """The API graph for one COVER. `audio_name` is the file already uploaded to forge.

    `show_score=True` adds a PreviewAny on the transcribed ABC, which costs nothing and is
    the only way the score reaches the Mac: collect() writes it out as a .txt beside the
    audio, so the melody that was actually extracted is inspectable instead of assumed.
    """
    if seed in (None, "", -1):
        seed = random.randint(0, 2 ** 31 - 1)
    seed = int(seed)
    seconds = max(YUE2_MIN_SECONDS, min(YUE2_MAX_SECONDS,
                                       float(seconds or YUE2_DEFAULT_SECONDS)))
    g = {
        "15": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        # the audio encoder is its OWN loader — SheetSage2 is not the checkpoint's CLIP
        "43": {"class_type": "AudioEncoderLoader",
               "inputs": {"audio_encoder_name": encoder}},
        "45": {"class_type": "LoadAudio", "inputs": {"audio": audio_name}},
        "41": {"class_type": "SheetSage2AudioToABC",
               "inputs": {"audio_encoder": ["43", 0], "audio": ["45", 0], "mode": "melody"}},
        "25": {"class_type": "YuE2GenerateMusic",
               "inputs": dict(clip=["15", 1], style=style or "", lyrics=lyrics or "",
                              abc=["41", 0], seed=seed,
                              # a cover is melody-led: this is the template's own setting
                              mode="melody", max_duration=seconds,
                              **YUE2_MUSIC_DEFAULTS)},
        "18": {"class_type": "ConditioningZeroOut",
               "inputs": {"conditioning": ["25", 0]}},
        "6": {"class_type": "EmptyYuE2LatentAudio",
              "inputs": {"seconds": ["25", 1], "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": dict(model=["15", 0], positive=["25", 0], negative=["18", 0],
                             latent_image=["6", 0], seed=seed, **YUE2_SAMPLER)},
        "7": {"class_type": "VAEDecodeAudio",
              "inputs": {"samples": ["5", 0], "vae": ["15", 2]}},
        "8": {"class_type": "SaveAudioAdvanced",
              "inputs": {"audio": ["7", 0], "filename_prefix": prefix, "format": fmt}},
    }
    if show_score:
        g["44"] = {"class_type": "PreviewAny", "inputs": {"source": ["41", 0]}}
    return g


def yue2_cover_params(style, lyrics, audio_path, seconds=YUE2_DEFAULT_SECONDS, seed=None,
                      prefix="covers/yue2"):
    """The `p` dict for a cover. `audio_path` is a LOCAL file; submit() uploads it."""
    p = yue2_plan_params(style, lyrics, mode="melody", seconds=seconds, seed=seed,
                         plan=False, prefix=prefix)
    p.update({"cover": True, "audio_path": audio_path})
    return p


def is_chordless_abc(abc):
    """True when a transcribed score carries NO chord symbols.

    A melody-only SheetSage2 score is `X:1 ... K:C` + note lines. Chord symbols appear as
    quoted annotations ("C", "Gm7") in the body. Used by the suite to assert the delivered
    score really is melody-only, because the model's guide explicitly warns that
    `cot="melody"` will NOT strip chords from a score you hand it yourself.
    """
    if not abc:
        return False
    for line in str(abc).splitlines():
        s = line.strip()
        if not s or s[0] in "XTMKLRQPV%" or s.startswith("]"):
            continue
        if '"' in s:                      # "Gm7" — a chord symbol annotation
            return False
    return True


def abc_note_count(abc):
    """A rough count of sounding notes in an ABC score, for "did the transcription actually
    produce a melody" checks. Deliberately crude: it counts note letters in the body."""
    n = 0
    for line in str(abc or "").splitlines():
        s = line.strip()
        if not s or s[0] in "XTMKLRQPV%" or s.startswith("]"):
            continue
        for ch in s:
            if ch.isalpha() and ch.lower() in "abcdefg":
                n += 1
    return n


# --------------------------------------------------------------------- the cover cost law
# A cover pays for TWO passes: SheetSage2 transcribing the recording, then the melody-mode
# generation. Measured 2026-10-09 on a 120 s source: 30.2 s wall for the whole cover,
# transcription included, and the cover filled 120.0 s.
YUE2_TRANSCRIBE_LOAD_S = 4.0            # encoder load + the transcription stage
YUE2_TRANSCRIBE_SEC_PER_AUDIO_SEC = 0.04  # transcription scales with the SOURCE length


def yue2_cover_estimate_seconds(seconds):
    """Wall seconds for ONE cover, excluding the engine-switch term (the caller adds it).

    Built on the SOURCE length, because that is what the transcription and the cover both
    follow — and the slider is set from the recording's own duration when you pick it.
    """
    s = max(YUE2_MIN_SECONDS, min(YUE2_MAX_SECONDS, float(seconds or YUE2_DEFAULT_SECONDS)))
    return (YUE2_OFF_LOAD_S + YUE2_SEC_PER_AUDIO_SEC * s
            + YUE2_TRANSCRIBE_LOAD_S + YUE2_TRANSCRIBE_SEC_PER_AUDIO_SEC * s)


# --------------------------------------------------------------------------- writing help
# The house rule from the model's own guide: KEEP MUSICAL INSTRUCTIONS IN STYLE, and put
# ONLY the words to be sung in the lyrics. Section tags structure the song.
LYRIC_TEMPLATE = """[Verse]
Morning light across the window
City waking down below
I can hear the streets are calling
Feels like somewhere we should go

[Chorus]
Run with me into the sunlight
Leave the shadows far behind
We don't need to know tomorrow
Tonight the whole world feels alive

[Bridge]

[Outro]
"""

STYLE_RECIPE = ("language + genre + vocal character + tempo + instruments + production")

# Ryan's own lanes first (the retro-futurist terminal look, Maine, the 1999 Tacoma), then
# a few workhorse genres. ALL of these are meant to be edited — they are a starting line,
# not a cage, which is why the window drops the text into the field rather than locking it.
STYLE_PRESETS = [
    ("Retro-futurist synth",
     "English, retro-futurist synthwave, 104 BPM, analog synth bass, gated reverb drums, "
     "bright arpeggios, male vocal with a slight vocoder edge, neon night-drive production"),
    ("Maine folk / Americana",
     "English, Americana folk, 88 BPM, acoustic guitar, upright bass, brushed snare, pedal "
     "steel, warm male vocal, close-miked, live-room production"),
    ("90s alt rock",
     "English, 90s alternative rock, 122 BPM, distorted electric guitars, driving bass, "
     "live drums, gritty male vocal, raw analog production"),
    ("Cinematic orchestral",
     "English, cinematic orchestral pop, 76 BPM, strings, piano, timpani, choir pads, "
     "soaring female vocal, wide film-score production"),
    ("Gospel / hymn",
     "English, traditional gospel hymn, 72 BPM, church organ, piano, upright bass, "
     "four-part choir, reverent and warm, live sanctuary production"),
    ("Country road",
     "English, country, 96 BPM, acoustic and electric guitar, fiddle, upright bass, "
     "male vocal with a story-teller's phrasing, warm Nashville production"),
    ("Lo-fi study",
     "Instrumental, lo-fi hip hop, 80 BPM, dusty piano sample, mellow upright bass, soft "
     "brushed drums, vinyl crackle, no vocals"),
    ("Industrial metal",
     "English, industrial metal, 140 BPM, downtuned guitars, distorted synth bass, "
     "programmed double-kick, shouted male vocal, cold machine production"),
]

HOW_TO_WRITE = (
    "STYLE is the band and the room: language, genre, vocal, tempo, instruments, production. "
    "LYRICS are only the words to be sung, in [Verse] / [Chorus] / [Bridge] / [Outro] blocks. "
    "Keep the two separate — musical directions typed into the lyrics get SUNG."
)
