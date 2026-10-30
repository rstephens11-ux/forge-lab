#!/usr/bin/env python3
"""GPU-free proof for the SONG section (YuE2) in Forge Lab.

No GPU, no network, no Tk: submit()'s upload/post are monkeypatched and every index write
goes to a throwaway FORGE_LAB_DATA dir, never Ryan's real ~/forge-lab. Mirrors
tools/core_matrix.py.

Guards five things, each of which was either a REAL bug caught here or a claim that would
otherwise be unfalsifiable:

  A · THE REGISTRY. "song" exists, is NOT a video mode (the broad-flag trap), and reports
      engine "yue2" so the estimate's engine-switch term keys on the right model set.

  B · THE COST LAW, against the REAL measured rows. The law is only allowed to be an UPPER
      bound on a song that actually filled its requested length, and it must not exceed a
      filled run by more than a few seconds. A law that under-promises is a bug, not a
      pleasant surprise — the whole point is that Ryan is never surprised by the clock.

  C · THE GRAPH, node for node against the Comfy-Org template. plan=True has the ABC node
      and wires its STRING into YuE2GenerateMusic; plan=False OMITS it and passes "". Every
      link must resolve. The sampler must be the template's (32 steps, cfg 1.0, dpm_2,
      sgm_uniform) — "better" sampler settings here are a different workflow, not an
      improvement. The seed must be ONE seed across ABC, music and KSampler.

  D · SUBMIT/INDEX HONESTY. A song take records song=True, the engine's REAL step count, no
      canvas, and the STYLE as its prompt — not H3's fields (there are no frames in a song).

  E · THE AUDIO OUTPUT KEY. collect() used to read only images/gifs/videos, so a song's flac
      was never pulled off forge: the run said success and delivered an empty folder. The
      real history shape for a SaveAudio node is {"audio": [...]} and nothing else, so this
      feeds collect() exactly that and checks the bytes land on disk.

    /opt/homebrew/bin/python3 tools/song_matrix.py       # exits 1 on any failure
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

FAILS = []


def ck(cond, label):
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)
    return bool(cond)


def load_app(tmp_data):
    """Import forge_lab with a throwaway DATA dir, so nothing here touches Ryan's runs."""
    os.environ["FORGE_LAB_DATA"] = tmp_data
    os.environ["FORGE_URL"] = "http://127.0.0.1:8188"     # never dialled: post_json is stubbed
    spec = importlib.util.spec_from_file_location("forge_lab_songtest",
                                                 os.path.join(REPO, "forge_lab.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["forge_lab_songtest"] = mod
    sys.path.insert(0, REPO)
    spec.loader.exec_module(mod)
    return mod


def main():
    tmp = tempfile.mkdtemp(prefix="songtest_data_")
    fl = load_app(tmp)
    import song

    print("A · the registry")
    m = fl.MODES.get("song")
    ck(m is not None, "MODES['song'] exists")
    ck((m or {}).get("label") == "Song", "it is labelled Song")
    ck(not (m or {}).get("video"), "it is NOT a video mode (the broad-flag trap)")
    ck((m or {}).get("ref") is False, "it needs no reference still")
    ck(fl.engine_of("song") == "yue2", "engine_of('song') == 'yue2' (got %r)" % fl.engine_of("song"))
    ck(fl.engine_load_s("song") == fl.YUE2_SWITCH_S,
       "engine_load_s('song') is the yue2 switch term (%s s)" % fl.YUE2_SWITCH_S)
    ck(fl.kind_label("song") == "Song", "kind_label('song') == 'Song'")
    ck(fl.kind_label("t2i") == "Image" and fl.kind_label("video") == "Video",
       "and it did not disturb the other labels")

    print("B · the cost law vs the REAL measured rows")
    # (requested, plan, wall, filled, switch) — every row is a real forge run, 2026-10-09.
    # `filled` = the song actually ran to the requested length (so the law must be tight);
    # `switch` = the run paid the engine load (cold page cache / restarted ComfyUI).
    ROWS = [(45.0, True, 17.7, True, False),     # first of the session, warm cache
            (120.0, True, 23.0, False, False),   # ended at the model's own end token
            (60.0, True, 25.2, True, True),      # COLD: restarted + vm.drop_caches=3
            (90.0, True, 25.2, True, False),     # warm
            (240.0, True, 20.2, False, False),   # ended early even with 3x the lyrics
            (60.0, False, 15.1, True, False)]    # plan off
    for req, plan, wall, filled, switch in ROWS:
        pred = song.yue2_estimate_seconds(req, plan) + (fl.YUE2_SWITCH_S if switch else 0.0)
        ck(pred >= wall - 1.0,
           "req %5.0fs plan=%-5s pred %5.1fs does not UNDER-promise (ran %.1fs)"
           % (req, plan, pred, wall))
        if filled:
            # the tight half of the guard: on a song that filled its request the law must
            # land within 1.5 s. Loosening THIS is how a cost law quietly rots.
            ck(pred <= wall + 1.5,
               "req %5.0fs plan=%-5s pred %5.1fs is within 1.5s of the run (%.1fs)"
               % (req, plan, pred, wall))
    ck(song.yue2_estimate_seconds(15, True) > song.yue2_estimate_seconds(15, False),
       "the plan pass costs more than no plan")
    ck(abs(song.yue2_estimate_seconds(900) - song.yue2_estimate_seconds(9000)) < 0.01,
       "the law clamps at the node's own 900 s ceiling instead of growing forever")

    print("C · the graph, node for node")
    STYLE = "English, warm female vocal, 96 BPM"
    LYR = "[Verse]\nline one\n\n[Chorus]\nline two\n"
    g = song.yue2_graph(STYLE, LYR, mode="full", seconds=120, seed=831001, plan=True)
    ck(set(g) == {"1", "2", "3", "4", "5", "6", "7", "8"},
       "plan=full builds all eight nodes (got %s)" % sorted(g))
    ck(g["1"]["class_type"] == "CheckpointLoaderSimple"
       and g["1"]["inputs"]["ckpt_name"] == song.YUE2_CKPT,
       "node 1 loads the int8 checkpoint the ComfyUI docs use")
    ck(g["3"]["inputs"]["abc"] == ["2", 0], "the ABC score is wired into YuE2GenerateMusic")
    ck(g["3"]["inputs"]["max_duration"] == 120.0, "the requested length reaches the model")
    ck(g["6"]["inputs"]["seconds"] == ["3", 1],
       "the latent length follows the MODEL's own reported seconds, not the request")
    ck(g["8"]["class_type"] == "SaveAudioAdvanced" and g["8"]["inputs"]["audio"] == ["7", 0],
       "the audio goes through SaveAudioAdvanced")
    ck(g["8"]["inputs"]["format"] == "flac", "saved as flac (lossless, what the template uses)")
    ck(g["8"]["inputs"]["filename_prefix"] == "songs/yue2",
       "under a songs/ subfolder on the rig, named yue2_* (findable in Takes)")
    ks = g["5"]["inputs"]
    ck((ks["steps"], ks["cfg"], ks["sampler_name"], ks["scheduler"])
       == (32, 1.0, "dpm_2", "sgm_uniform"),
       "the sampler is the TEMPLATE's (32 / cfg 1.0 / dpm_2 / sgm_uniform), not a 'better' one")
    ck(g["2"]["inputs"]["seed"] == g["3"]["inputs"]["seed"] == ks["seed"] == 831001,
       "ONE seed feeds the ABC pass, the music pass and the sampler")
    ck(g["5"]["inputs"]["negative"] == ["4", 0] and g["4"]["class_type"] == "ConditioningZeroOut",
       "the negative branch is a zeroed-out conditioning, as the template has it")
    ck(fl._check_graph(g) is True, "every link resolves (no dangling node)")

    g2 = song.yue2_graph(STYLE, LYR, plan=False, seconds=60, seed=99)
    ck("2" not in g2, "plan=off OMITS the ABC node rather than leaving it dangling")
    ck(g2["3"]["inputs"]["abc"] == "", "and passes an empty score, which is the node's OFF mode")
    ck(set(g2) == {"1", "3", "4", "5", "6", "7", "8"}, "seven nodes with planning off")
    ck(fl._check_graph(g2) is True, "the off-path graph resolves too")
    ck(song.yue2_graph(STYLE, LYR, seconds=5)["3"]["inputs"]["max_duration"] == song.YUE2_MIN_SECONDS,
       "a too-short request is clamped up to the node's minimum")
    ck(song.yue2_graph(STYLE, LYR, mode="off", plan=True)["3"]["inputs"]["mode"] == "full",
       "an 'off' mode string cannot leak into a node that only knows full/melody")

    print("D · submit + the index entry")
    posted = {}
    _ids = {"n": 0}

    def _fake_post(path, obj, timeout=60, base=None):
        posted.update(obj)
        _ids["n"] += 1
        return {"prompt_id": "song-test-pid-%d" % _ids["n"]}

    fl.post_json = _fake_post
    p = song.yue2_plan_params(STYLE, LYR, mode="full", seconds=90, seed=4242, plan=True)
    pid, seed = fl.submit("song", p, None)
    ck(pid == "song-test-pid-1" and seed == 4242, "submit() accepts a song and keeps the seed")
    ck("8" in posted.get("prompt", {}) and posted["prompt"]["8"]["class_type"] == "SaveAudioAdvanced",
       "the graph that reached forge is the song graph")
    it = next((i for i in fl.read_index() if i.get("id") == pid), None)
    ck(it is not None, "the run landed in the index like any other take")
    if it:
        ck(it.get("song") is True and it.get("audio") is True, "flagged as a SONG, not a video")
        ck(it.get("steps") == fl.MODES["song"]["steps"],
           "it records the engine's REAL steps (%s), not a form default" % fl.MODES["song"]["steps"])
        ck(not it.get("width") and not it.get("height"), "a song has no canvas")
        ck(it.get("seconds") == 90.0, "the requested length is recorded")
        ck(it.get("prompt") == STYLE, "the take's prompt is the STYLE")
        ck(it.get("label") == "Song", "labelled Song in Takes")
        ck(fl.take_specs(it).startswith("yue2"), "take_specs says yue2 (got %r)" % fl.take_specs(it))

    print("E · collect() pulls the AUDIO output key")
    # The REAL shape a SaveAudio node reports — no images, no videos, no gifs key at all.
    # ⚠️ Keyed by the PROMPT ID, which is what collect() looks up; the first version of this
    # fixture keyed it by node id, the lookup missed, and the test failed for its own reason.
    _pid = pid          # the id submit() actually returned in section D
    fake_hist = {_pid: {
        "outputs": {"8": {"audio": [
            {"filename": "song_test_00001.flac", "subfolder": "songs", "type": "output"}]}},
        "status": {"status_str": "success", "messages": []}}}
    fl.get_json = lambda path, timeout=30, base=None: (fake_hist if path.startswith("/history") else {})
    fl.get_bytes = lambda path, timeout=180, base=None: b"AUDIO-BYTES-0123456789"
    meta = fl.collect(_pid)
    ck(meta is not None and not meta.get("error"), "collect() did not error (%r)" % (meta or {}).get("error"))
    got = [f for f in (meta or {}).get("images", []) if f["file"] == "song_test_00001.flac"]
    ck(len(got) == 1, "the flac is in the take's file list")
    ck(got and got[0]["bytes"] == len(b"AUDIO-BYTES-0123456789"),
       "with the real byte count from /view")
    dest = os.path.join(fl.RUNS, _pid, "song_test_00001.flac")
    ck(os.path.exists(dest), "and the bytes are on disk at %s" % dest)
    ck((meta or {}).get("status") == "success", "the take is marked success, not stuck 'running'")

    print("F · one seed, one graph (the double-_seed_for trap)")
    gw, sd = fl.build_graph("song", {"style": STYLE, "lyrics": LYR, "seed": -1,
                                     "max_duration": 60, "plan": True})
    ck(gw["2"]["inputs"]["seed"] == sd and gw["5"]["inputs"]["seed"] == sd,
       "build_graph returns the seed the graph ACTUALLY used (both -1 draws agree)")

    print("G · COVERS — the melody comes from a file, not the planner")
    abc_src = os.path.join(tmp, "src.flac")
    with open(abc_src, "wb") as f:
        f.write(b"fake-audio")
    cg = song.yue2_cover_graph("uploaded_src.flac", STYLE, LYR, seconds=120, seed=5150)
    ck(set(cg) == {"15", "43", "45", "41", "25", "18", "6", "5", "7", "8", "44"},
       "the cover graph is the cover template's chain (got %s)" % sorted(cg))
    ck("2" not in cg,
       "⚠️ the ABC PLANNER is absent — supplying an outside score is what bypasses it, and "
       "leaving it in would plan a second melody over the transcribed one")
    ck(cg["41"]["class_type"] == "SheetSage2AudioToABC", "SheetSage2 does the transcription")
    ck(cg["41"]["inputs"]["mode"] == "melody",
       "asked for MELODY-ONLY (that is what keeps chord symbols out of the score)")
    ck(cg["41"]["inputs"]["audio"] == ["45", 0], "the transcription reads the loaded recording")
    ck(cg["43"]["class_type"] == "AudioEncoderLoader"
       and cg["43"]["inputs"]["audio_encoder_name"] == song.YUE2_SHEETSAGE,
       "and the encoder has its OWN loader (SheetSage2 is not the checkpoint's CLIP)")
    ck(cg["45"]["class_type"] == "LoadAudio"
       and cg["45"]["inputs"]["audio"] == "uploaded_src.flac",
       "LoadAudio takes the UPLOADED file name")
    ck(cg["25"]["inputs"]["abc"] == ["41", 0], "the transcribed score feeds the generator")
    ck(cg["25"]["inputs"]["mode"] == "melody", "and the generator is in melody mode too")
    ck(cg["25"]["inputs"]["seed"] == cg["5"]["inputs"]["seed"] == 5150,
       "one seed across the generator and the sampler")
    ck(cg["44"]["class_type"] == "PreviewAny", "the score is exported so it can be inspected")
    ck(fl._check_graph(cg) is True, "every cover link resolves")
    ck("2" not in song.yue2_cover_graph("x.flac", STYLE, LYR, show_score=False)
       and "44" not in song.yue2_cover_graph("x.flac", STYLE, LYR, show_score=False),
       "show_score=False drops the exporter without disturbing the chain")

    # the score really is melody-only — asserted on a synthetic pair, not on faith
    ck(song.is_chordless_abc("X:1\nT:test\nK:C\nV: Vocal\ncdef gag2|\n") is True,
       "a melody-only score is recognised as chordless")
    ck(song.is_chordless_abc('X:1\nK:C\nV: Vocal\n"Gm7"cdef "C"gag2|\n') is False,
       "a score WITH chord symbols is NOT (the model's guide warns cot=melody will not strip them)")
    ck(song.abc_note_count("X:1\nK:C\nV: Vocal\ncdef gag2|\n") == 7,
       "the note count sees the melody (got %d)" % song.abc_note_count("X:1\nK:C\nV: Vocal\ncdef gag2|\n"))

    # submit() must UPLOAD the recording and put its name in the graph
    fl.upload_image = lambda p: "up_" + os.path.basename(p)
    posted.clear()
    cp = song.yue2_cover_params(STYLE, LYR, abc_src, seconds=120, seed=777)
    pid2, _s2 = fl.submit("song", cp, None)
    cg2 = posted.get("prompt") or {}
    ck(cg2.get("45", {}).get("inputs", {}).get("audio") == "up_src.flac",
       "submit() uploaded the recording and the graph loads THAT name")
    ck("41" in cg2 and cg2["41"]["class_type"] == "SheetSage2AudioToABC",
       "and what reached forge is the cover graph")
    it2 = next((i for i in fl.read_index() if i.get("id") == pid2), None)
    ck(bool(it2 and it2.get("cover")), "the take is flagged as a COVER")
    ck(bool(it2 and it2.get("source")) == True,
       "and records the source recording it followed (%r)" % (it2 or {}).get("source"))
    ck(bool(it2 and it2.get("song_mode") == "melody"), "its mode is recorded as melody")
    ck(fl.take_specs(it2).startswith("yue2"), "take_specs still names the engine")

    # refusing properly beats building a graph that cannot run
    try:
        fl.submit("song", song.yue2_cover_params(STYLE, LYR, ""), None)
        ck(False, "a cover with no recording is refused")
    except ValueError as e:
        ck("SOURCE RECORDING" in str(e), "a cover with no recording is refused (%s)" % e)
    try:
        fl.build_graph("song", {"style": STYLE, "lyrics": LYR, "cover": True, "seed": 1})
        ck(False, "build_graph refuses a cover with nothing uploaded")
    except ValueError as e:
        ck("uploaded" in str(e), "build_graph refuses a cover with nothing uploaded (%s)" % e)
    ck(song.yue2_cover_estimate_seconds(120) > song.yue2_estimate_seconds(120, False),
       "a cover estimates MORE than the same song without a transcription")

    print("H · collect() also saves the transcribed score (inline text, not a download)")
    _pid3 = "cover-test-pid"
    fl._index_insert({"id": _pid3, "mode": "song", "label": "Song · cover", "prefix": "covers/yue2",
                      "started": 1.0, "status": "running", "images": []})
    fl.get_json = lambda path, timeout=30, base=None: ({_pid3: {
        "outputs": {"8": {"audio": [{"filename": "yue2_00001.flac", "subfolder": "covers",
                                     "type": "output"}]},
                    "44": {"text": ["X:1\nK:C\nV: Vocal\ncdef gag2|\n"]}},
        "status": {"status_str": "success", "messages": []}}} if path.startswith("/history") else {})
    fl.get_bytes = lambda path, timeout=180, base=None: b"COVER-AUDIO"
    m3 = fl.collect(_pid3) or {}
    tf = m3.get("text_files") or []
    ck(len(tf) == 1, "the score was written out as a text file (%r)" % tf)
    ck(tf and os.path.exists(os.path.join(fl.RUNS, _pid3, tf[0])),
       "and it is on disk beside the audio")
    if tf:
        got_abc = open(os.path.join(fl.RUNS, _pid3, tf[0]), encoding="utf-8").read()
        ck("V: Vocal" in got_abc, "with the score's own content in it")
        ck(song.is_chordless_abc(got_abc), "and it is the chordless melody-only score")

    print("I · the words come back — nothing is retyped")
    # ⚠️ RESET what this block depends on. Sections D and G already inserted song takes into
    # this throwaway index, so "nothing saved yet" is only true on a CLEARED one — the first
    # version of this block inherited their state and two assertions failed for that reason
    # alone. A test that inherits the previous block's state is a test that lies.
    with open(fl._index_path(), "w") as fh:
        json.dump([], fh)
    ck(fl.last_song_take() is None, "with no songs yet it returns None (not a crash)")
    fl._index_insert({"id": "w1", "mode": "t2i", "label": "Text to image", "started": 1.0,
                      "style": "a photo of a lighthouse", "lyrics": ""})
    ck(fl.last_song_take() is None, "an IMAGE take is skipped, even with words on it")
    fl._index_insert({"id": "w2", "mode": "song", "label": "Song", "started": 2.0,
                      "style": "English, lo-fi", "lyrics": "[Verse]\nfirst words\n"})
    _w = fl.last_song_take()
    ck(bool(_w and _w.get("id") == "w2"), "a song's words are found")
    ck(bool(_w and _w.get("lyrics") == "[Verse]\nfirst words\n"), "with the lyrics intact")
    fl._index_insert({"id": "w3", "mode": "song", "label": "Song", "started": 3.0,
                      "style": "English, rock", "lyrics": "[Verse]\nnewer words\n"})
    ck(fl.last_song_take().get("id") == "w3",
       "⚠️ the NEWEST song wins — _index_insert PREPENDS, so an implementation that iterated "
       "read_index() REVERSED would restore the OLDEST words. That bug shipped once.")
    fl._index_insert({"id": "w4", "mode": "song", "label": "Song", "started": 4.0})
    ck(fl.last_song_take().get("id") == "w3",
       "and a newer song with NO words does not hide the one that has them")

    # ---- the COVER case: an audio file back to the take that made it --------------------
    # Ryan: "not for the last song for the cover song" — a DIFFERENT question, answered by a
    # different lookup. The take's audio IS the recording being covered.
    os.makedirs(os.path.join(fl.RUNS, "w5"), exist_ok=True)
    _rec = os.path.join(fl.RUNS, "w5", "yue2_00009.flac")
    with open(_rec, "wb") as fh:
        fh.write(b"audio")
    fl._index_insert({"id": "w5", "mode": "song", "label": "Song \u00b7 cover", "started": 5.0,
                      "style": "English, lo-fi", "lyrics": "[Verse]\ncovered words\n",
                      "images": [{"file": "yue2_00009.flac"}]})
    _c = fl.words_of_recording(_rec)
    ck(bool(_c and _c.get("id") == "w5"), "a recording maps back to the take that made it")
    ck(bool(_c and _c.get("lyrics") == "[Verse]\ncovered words\n"),
       "and ITS words come with it \u2014 the COVER case, which is not the last-song case")
    ck(fl.words_of_recording(os.path.join(fl.RUNS, "w5", "..", "w5", "yue2_00009.flac"))
       is not None, "the path is RESOLVED, so ../ or a symlink still matches")
    ck(fl.words_of_recording(os.path.join(fl.RUNS, "w5", "nope.flac")) is None,
       "a file we did not make returns None (a normal answer, not a guess)")
    ck(fl.words_of_recording("") is None, "an empty path returns None")
    ck(fl.words_of_recording(os.path.join(REPO, "forge_lab.py")) is None,
       "a file that exists but is not a take returns None")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nsong_matrix %s" % ("FAILED: %d" % len(FAILS) if FAILS else "done — all assertions pass"))
    for f in FAILS:
        print("  FAILED:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())