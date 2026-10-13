#!/usr/bin/env python3
"""GPU-free assertions for the Forge Lab audio client (audio/client.py).

Stubs subprocess so nothing touches the rig. Asserts the SHAPE of the remote command the
client builds: the right venv python, CUDA_VISIBLE_DEVICES=1, the rig script, and that
shell quoting survives text containing double quotes and apostrophes. Also asserts the
aligner output parser rejects malformed JSON.

Run: /opt/homebrew/bin/python3 tools/audio_matrix.py   (exit 0 = green, 1 = failure)
"""
import json
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT_PATH = os.path.join(ROOT, "audio", "client.py")

FAILS = []
CHECKS = [0]


def check(cond, msg):
    CHECKS[0] += 1
    if cond:
        print("  ok  %s" % msg)
    else:
        print("FAIL  %s" % msg)
        FAILS.append(msg)


def load_client():
    """Load audio/client.py by compiling its SOURCE TEXT, never a .pyc.

    ⚠️ Do NOT use importlib's SourceFileLoader here: it trusts a cached .pyc keyed on
    source mtime+size, and a mutation that keeps the same byte count (e.g. flipping
    CUDA_VISIBLE_DEVICES 1<->0) leaves a stale .pyc that makes the suite read OLD code
    while the file on disk looks correct -- green-washing a broken guard.
    """
    import types
    with open(CLIENT_PATH, "rb") as f:
        src = f.read()
    mod = types.ModuleType("audio_client")
    mod.__file__ = CLIENT_PATH
    exec(compile(src, CLIENT_PATH, "exec"), mod.__dict__)
    return mod


class FakeProc:
    def __init__(self, argv):
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""
        self.argv = argv


class FakeSubprocess:
    """Records argv; emulates `scp ... down` by writing the supplied payload to dest."""

    def __init__(self):
        self.calls = []
        self.down_payload = {}  # remote basename -> (bytes|str)
        self.rig_stdout = ""

    def run(self, argv, capture_output=True, text=True, **kw):
        self.calls.append(list(argv))
        p = FakeProc(argv)
        # scp down: ["scp","-q","host:path","local"]
        if argv and argv[0] == "scp" and len(argv) == 4 and ":" in argv[2] and ":" not in argv[3]:
            name = os.path.basename(argv[2].split(":", 1)[1])
            if name in self.down_payload:
                with open(argv[3], "w", encoding="utf-8") as f:
                    f.write(self.down_payload[name])
        # ssh run of a rig script: return the rig json line
        elif argv and argv[0] == "ssh":
            p.stdout = self.rig_stdout
        # LOCAL mode: "bash -lc 'cd <job> && ... rig script ... '"
        elif argv and argv[0] == "bash" and len(argv) >= 3:
            self._emulate_local(argv[2])
            p.stdout = self.rig_stdout  # read AFTER emulate (it may update rig_stdout)
        return p

    def _emulate_local(self, cmd):
        """Emulate the rig writing its artifacts, so the client's copy-back works.

        Handles both --out (one-shot) and --job (batch) modes.
        """
        import re as _re
        try:
            info = json.loads(self.rig_stdout or "{}")
        except ValueError:
            info = {}
        m = _re.search(r"cd (\S+) &&", cmd)
        d = m.group(1) if m else "."
        if not info.get("ok"):
            return
        # --out mode (one-shot): create the single output file
        mm = _re.search(r"--out (\S+)", cmd)
        if mm:
            target = mm.group(1).strip("'\"")
            path = target if os.path.isabs(target) else os.path.join(d, target)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"RIFFlocal")
        # --job mode (batch): read job.json and create each output file
        mm = _re.search(r"--job (\S+)", cmd)
        if mm and os.path.isdir(d):
            jfile = mm.group(1).strip("'\"")
            jpath = jfile if os.path.isabs(jfile) else os.path.join(d, jfile)
            if os.path.isfile(jpath):
                with open(jpath, "r") as _f:
                    _job = json.load(_f)
                for _lst in [_job.get("design", []), _job.get("clone", [])]:
                    for _item in _lst:
                        _op = os.path.join(d, _item["out"])
                        os.makedirs(os.path.dirname(_op) or ".", exist_ok=True)
                        with open(_op, "wb") as _fw:
                            _fw.write(b"RIFFbatch")
                # Update rig_stdout item out paths to match actual files so
                # _rewrite_out_paths can copy them
                try:
                    _rd = json.loads(self.rig_stdout)
                    for _ri in _rd.get("items", []):
                        _oid = _ri.get("id")
                        for _lst2 in [_job.get("design", []), _job.get("clone", [])]:
                            for _ji in _lst2:
                                if _ji.get("id") == _oid:
                                    _ri["out"] = os.path.join(d, _ji["out"])
                    self.rig_stdout = json.dumps(_rd)
                except Exception:
                    pass

    def bash_calls(self):
        return [c for c in self.calls if c and c[0] == "bash"]

    def ssh_calls(self):
        return [c for c in self.calls if c and c[0] == "ssh"]

    def scp_calls(self):
        return [c for c in self.calls if c and c[0] == "scp"]


def remote_cmd_for(fs, needle):
    for c in fs.ssh_calls():
        if needle in c[2]:
            return c[2]
    return None


def safe_split(cmd):
    """shlex.split that returns None instead of raising on a malformed command."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def main():
    client = load_client()
    fs = FakeSubprocess()
    client.subprocess.run = fs.run

    # ---- deploy() copies the rig scripts to the rig dir ------------------------------
    client.deploy()
    scp_up = [c for c in fs.scp_calls() if c[-1].endswith(client.RIG_DIR + "/")]
    check(bool(scp_up), "deploy() scp's rig scripts to %s/" % client.RIG_DIR)
    if scp_up:
        names = {os.path.basename(f) for f in scp_up[0][1:-1] if f.endswith(".py")}
        check("rig_tts.py" in names and "rig_align.py" in names,
              "deploy ships rig_tts.py and rig_align.py (%r)" % sorted(names))
        check(scp_up[0][-1] == "%s:%s/" % (client.HOST, client.RIG_DIR),
              "deploy targets %s:%s/" % (client.HOST, client.RIG_DIR))

    # ---- tts_design: venv, GPU pin, script, and quoting survive ----------------------
    desc = 'a flat, nasal man; deadpan "Maine"'
    text = 'He said "it\'s fine"'
    fs.down_payload["out.wav"] = "RIFFfake"
    fs.rig_stdout = json.dumps({"ok": True, "mode": "design", "duration": 1.5})
    lpath = os.path.join(client.tempfile.gettempdir(), "audio_matrix_design.wav")
    client._DEPLOYED = True  # skip re-deploy noise
    info = client.tts_design(desc, text, lpath)

    cmd = remote_cmd_for(fs, "rig_tts.py")
    check(cmd is not None, "tts_design() runs rig_tts.py over ssh")
    check(cmd is not None and client.VENV_TTS in cmd, "tts_design uses the tts venv python")
    check(cmd is not None and "CUDA_VISIBLE_DEVICES=1" in cmd, "tts_design pins CUDA_VISIBLE_DEVICES=1")
    check(info.get("ok") is True, "tts_design returns the rig json (ok=True)")

    # Reconstruct the argv the rig will see by splitting the ssh payload.
    if cmd is not None:
        toks = safe_split(cmd)
        check(toks is not None, "the ssh command is well-formed shell (%r)" % cmd)
        check(toks is not None and "--design" in toks and toks[toks.index("--design") + 1] == desc,
              "design description survives quoting (%r)" % desc)
        check(toks is not None and "--text" in toks and toks[toks.index("--text") + 1] == text,
              "text with double quotes AND an apostrophe survives quoting (%r)" % text)

    # ---- wrong-venv guard: tts never uses the asr venv -------------------------------
    check(cmd is not None and client.VENV_ASR not in cmd,
          "tts_design does NOT use the asr venv")

    # ---- tts_clone -------------------------------------------------------------------
    ref = os.path.join(client.tempfile.gettempdir(), "audio_matrix_ref.wav")
    with open(ref, "wb") as f:
        f.write(b"RIFFref")
    ref_text = "I'd like a hat."
    line2 = "No, bigger than that."
    fs.down_payload["out.wav"] = "RIFFfake2"
    client.tts_clone(ref, ref_text, line2, lpath)
    cmd2 = remote_cmd_for(fs, "--clone")
    check(cmd2 is not None and "--clone" in cmd2 and client.VENV_TTS in cmd2,
          "tts_clone runs rig_tts.py --clone in the tts venv")
    up = [c for c in fs.scp_calls() if len(c) == 4 and ":" in c[-1] and "ref.wav" in c[-1]]
    check(bool(up), "tts_clone scp's the reference wav up to the job dir")
    if cmd2 is not None:
        toks2 = safe_split(cmd2)
        check(toks2 is not None, "the clone ssh command is well-formed shell")
        check(toks2 is not None and toks2[toks2.index("--ref-text") + 1] == ref_text,
              "clone ref-text with an apostrophe survives quoting (%r)" % ref_text)
        check(toks2 is not None and toks2[toks2.index("--text") + 1] == line2,
              "clone text survives quoting")

    # ---- align -----------------------------------------------------------------------
    fs.down_payload["words.json"] = json.dumps(
        [{"word": "I'd", "start": 0.0, "end": 0.3}, {"word": "like", "start": 0.3, "end": 0.6}])
    fs.rig_stdout = json.dumps({"ok": True, "count": 2})
    wav = os.path.join(client.tempfile.gettempdir(), "audio_matrix_in.wav")
    with open(wav, "wb") as f:
        f.write(b"RIFFin")
    words = client.align(wav, text)
    cmd3 = remote_cmd_for(fs, "rig_align.py")
    check(cmd3 is not None and client.VENV_ASR in cmd3, "align uses the asr venv python")
    check(cmd3 is not None and "CUDA_VISIBLE_DEVICES=1" in cmd3, "align pins CUDA_VISIBLE_DEVICES=1")
    check(cmd3 is not None and client.VENV_TTS not in cmd3, "align does NOT use the tts venv")
    check(isinstance(words, list) and len(words) == 2, "align returns a list of words")
    check(words[0] == {"word": "I'd", "start": 0.0, "end": 0.3},
          "align parses word timings (%r)" % (words[0],))

    # ---- align output parser rejects malformed json ----------------------------------
    bad = [
        ("not json at all", "non-JSON"),
        ('{"count": 1}', "a JSON object, not a list"),
        ('[{"word": "x"}]', "an item missing start/end"),
        ('[{"word": "x", "start": "a", "end": 1}]', "non-numeric start"),
        ('["just a string"]', "a non-object item"),
    ]
    for raw, why in bad:
        try:
            client._parse_words(raw)
            check(False, "_parse_words rejects %s" % why)
        except ValueError:
            check(True, "_parse_words rejects %s" % why)

    # ---- good json still parses ------------------------------------------------------
    ok = client._parse_words('[{"word": "hi", "start": 1, "end": 2.5}]')
    check(ok == [{"word": "hi", "start": 1.0, "end": 2.5}], "valid aligner output parses")

    # ---- LOCAL mode: the rig runs THESE scripts itself, no ssh/scp ------------------
    os.environ["FORGE_AUDIO_LOCAL"] = "1"
    check(client._local() is True, "FORGE_AUDIO_LOCAL=1 selects LOCAL mode")
    fs2 = FakeSubprocess()
    client.subprocess.run = fs2.run
    client._DEPLOYED = True
    fs2.rig_stdout = json.dumps({"ok": True, "mode": "design", "duration": 1.2})
    client.tts_design(desc, text, lpath)
    check(not fs2.ssh_calls(), "LOCAL tts_design makes NO ssh calls")
    check(not fs2.scp_calls(), "LOCAL tts_design makes NO scp calls")
    bcs = fs2.bash_calls()
    check(bool(bcs), "LOCAL tts_design runs the rig via bash -lc")
    lc = bcs[-1][2] if bcs else ""
    check(client.VENV_TTS in lc and "rig_tts.py" in lc,
          "LOCAL tts_design uses the tts venv + rig_tts.py")
    check("CUDA_VISIBLE_DEVICES=1" in lc, "LOCAL tts_design STILL pins CUDA_VISIBLE_DEVICES=1")
    check("--design" in lc, "LOCAL tts_design passes --design")
    check(os.path.exists(lpath) and open(lpath, "rb").read() == b"RIFFlocal",
          "LOCAL tts_design copies the artifact back to out_path")

    # align local
    fs2.rig_stdout = json.dumps({"ok": True, "count": 2})
    fs2.down_payload = {}
    client._parse_words = client._parse_words  # unchanged
    # write the words.json the align copy-back expects
    orig_emulate = fs2._emulate_local

    def _emulate_align(cmd):
        import re as _re
        m = _re.search(r"cd (\S+) &&", cmd)
        d = m.group(1) if m else "."
        mm = _re.search(r"--out (\S+)", cmd)
        if mm:
            tgt = mm.group(1).strip("'\"")
            path = tgt if os.path.isabs(tgt) else os.path.join(d, tgt)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(json.dumps([{"word": "hat", "start": 0.1, "end": 0.4}]))
    fs2._emulate_local = _emulate_align
    fs2.rig_stdout = json.dumps({"ok": True, "count": 1})
    wav2 = os.path.join(client.tempfile.gettempdir(), "audio_matrix_local_in.wav")
    with open(wav2, "wb") as f:
        f.write(b"RIFFin")
    words2 = client.align(wav2, "hat")
    check(not fs2.ssh_calls() and not fs2.scp_calls(), "LOCAL align makes NO ssh/scp calls")
    check(words2 == [{"word": "hat", "start": 0.1, "end": 0.4}], "LOCAL align parses words")
    fs2._emulate_local = orig_emulate

    # local deploy copies instead of scp; keep it out of the real rig dir
    old_rig = client.RIG_DIR
    client.RIG_DIR = os.path.join(client.tempfile.gettempdir(), "audio_matrix_local_rigdir")
    fs3 = FakeSubprocess()
    client.subprocess.run = fs3.run
    client._DEPLOYED = False
    client.deploy()
    check(not fs3.scp_calls() and not fs3.ssh_calls(), "LOCAL deploy copies files, no ssh/scp")
    deployed = os.path.join(client.RIG_DIR, "rig_tts.py")
    check(os.path.exists(deployed), "LOCAL deploy actually writes rig_tts.py to RIG_DIR")
    client.RIG_DIR = old_rig

    # FORGE_AUDIO_LOCAL=0 forces the remote path even if the hostname looks like forge
    os.environ["FORGE_AUDIO_LOCAL"] = "0"
    check(client._local() is False, "FORGE_AUDIO_LOCAL=0 forces REMOTE mode")
    os.environ.pop("FORGE_AUDIO_LOCAL", None)

    # ---- tts_batch (remote) --------------------------------------------------------------
    fs5 = FakeSubprocess()
    client.subprocess.run = fs5.run
    client._DEPLOYED = True

    # Create a ref wav for voice key
    ref_wav_b = os.path.join(client.tempfile.gettempdir(), "audio_matrix_alice.wav")
    with open(ref_wav_b, "wb") as f:
        f.write(b"RIFFalice")
    out_dir_b = os.path.join(client.tempfile.gettempdir(), "audio_matrix_batch_out")
    os.makedirs(out_dir_b, exist_ok=True)

    batch_job = {
        "design": [{"id": "d1", "instruct": "A 40-year-old male voice",
                     "text": "Hello.", "out": "d1.wav"}],
        "clone": [{"id": "c1", "voice": "alice", "text": "Good morning.",
                    "out": "c1.wav"}],
        "voices": {"alice": {"ref_wav": ref_wav_b, "ref_text": "A reference."}},
        "language": "English",
    }
    rig_items_b = [
        {"id": "d1", "out": "/tmp/forge-audio/abc/d1.wav", "duration": 2.0},
        {"id": "c1", "out": "/tmp/forge-audio/abc/c1.wav", "duration": 1.5},
    ]
    fs5.rig_stdout = json.dumps({"ok": True, "items": rig_items_b, "seconds": 15.0})
    for item in rig_items_b:
        fs5.down_payload[os.path.basename(item["out"])] = "RIFFbatch"

    result_b = client.tts_batch(batch_job, out_dir_b)
    check(result_b.get("ok") is True, "tts_batch returns ok=True")

    # Check ref wav was scp'd up (look for alice.wav in any scp arg)
    all_scp_args_b = " ".join(" ".join(c) for c in fs5.scp_calls())
    check("alice.wav" in all_scp_args_b or "audio_matrix_alice" in all_scp_args_b,
          "tts_batch scp's ref wav up to job dir (basename only)")

    # The CALLER's job is never rewritten (it is the UI's live plan), and the UPLOADED name is a
    # bare job-dir name (no absolute Mac path can reach the rig's job.json).
    rw = batch_job["voices"]["alice"]["ref_wav"]
    check(rw == ref_wav_b, "tts_batch leaves the caller's job untouched (%r)" % rw)
    up_dests = [c[-1].split(":", 1)[1] for c in fs5.scp_calls()
                if len(c) >= 4 and ":" in c[-1] and c[-2].endswith(".wav")]
    check(up_dests and all("/" not in os.path.basename(d) and d.rsplit("/", 1)[-1].startswith("ref")
                           for d in up_dests),
          "tts_batch rewrites ref_wav to a bare job-dir name (%r)" % up_dests)

    # ⚠️ Two characters, both with a sample called neutral.wav: they must upload as TWO files.
    fs6 = FakeSubprocess()
    client.subprocess.run = fs6.run
    d_a = client.tempfile.mkdtemp(); d_b = client.tempfile.mkdtemp()
    for d in (d_a, d_b):
        with open(os.path.join(d, "neutral.wav"), "wb") as f:
            f.write(b"RIFF" + d.encode())
    two_job = {"design": [], "clone": [
        {"id": "l0", "voice": "alice:neutral", "text": "Hooray.", "out": "l0.wav"},
        {"id": "l1", "voice": "bob:neutral", "text": "Sure.", "out": "l1.wav"}],
        "voices": {"alice:neutral": {"ref_wav": os.path.join(d_a, "neutral.wav"), "ref_text": "a"},
                   "bob:neutral": {"ref_wav": os.path.join(d_b, "neutral.wav"), "ref_text": "b"}}}
    fs6.rig_stdout = json.dumps({"ok": True, "items": [], "seconds": 1.0})
    client.tts_batch(two_job, out_dir_b)
    names = [c[-1].rsplit("/", 1)[-1] for c in fs6.scp_calls()
             if len(c) >= 4 and ":" in c[-1] and c[-2].endswith("neutral.wav")]
    check(len(names) == 2 and len(set(names)) == 2,
          "two voices' neutral.wav upload as two DIFFERENT files (%r)" % names)

    # Check the ssh command uses the batch script, tts venv, and CUDA_VISIBLE_DEVICES=1
    cmd_b = remote_cmd_for(fs5, "rig_dialogue.py")
    check(cmd_b is not None and "rig_dialogue.py" in cmd_b,
          "tts_batch runs rig_dialogue.py over ssh")
    check(cmd_b is not None and client.VENV_TTS in cmd_b,
          "tts_batch uses the tts venv python")
    check(cmd_b is not None and "CUDA_VISIBLE_DEVICES=1" in cmd_b,
          "tts_batch pins CUDA_VISIBLE_DEVICES=1")

    # Check outputs scp'd back and out rewritten to local path
    for item in result_b.get("items", []):
        check("out" in item and os.path.isabs(item["out"])
              and item["out"].startswith(out_dir_b),
              "tts_batch item out rewritten to local absolute path (%s)" % item.get("out"))

    # ---- tts_batch: per-item error does NOT raise ---------------------------------------
    fs6 = FakeSubprocess()
    client.subprocess.run = fs6.run
    client._DEPLOYED = True
    batch_with_err = {
        "clone": [{"id": "bad1", "voice": "alice", "text": "Oops.",
                    "out": "b.wav"}],
        "voices": {"alice": {"ref_wav": ref_wav_b, "ref_text": ""}},
    }
    fs6.rig_stdout = json.dumps({
        "ok": True,
        "items": [{"id": "bad1", "error": "model OOM"}],
        "seconds": 1.0,
    })
    result_err = client.tts_batch(batch_with_err, out_dir_b)
    check(result_err.get("ok") is True,
          "tts_batch with per-item error still returns ok=True")
    err_items = [i for i in result_err.get("items", []) if "error" in i]
    check(len(err_items) == 1 and err_items[0].get("error") == "model OOM",
          "tts_batch per-item error surfaced in items list (%r)" % err_items)

    # ---- transcribe_many (remote) -------------------------------------------------------
    fs7 = FakeSubprocess()
    client.subprocess.run = fs7.run
    client._DEPLOYED = True
    wav_a = os.path.join(client.tempfile.gettempdir(), "audio_matrix_a.wav")
    wav_b = os.path.join(client.tempfile.gettempdir(), "audio_matrix_b.wav")
    with open(wav_a, "wb") as f:
        f.write(b"RIFFa")
    with open(wav_b, "wb") as f:
        f.write(b"RIFFb")
    fs7.rig_stdout = json.dumps({
        "ok": True,
        "items": [
            {"audio": "audio_matrix_a.wav", "text": "hello world"},
            {"audio": "audio_matrix_b.wav", "text": "good morning"},
        ],
        "seconds": 5.0,
    })
    texts = client.transcribe_many([wav_a, wav_b])
    check(len(texts) == 2, "transcribe_many returns %d texts" % len(texts))
    check(texts[0] == "hello world", "transcribe_many text[0]=%r" % texts[0])
    check(texts[1] == "good morning", "transcribe_many text[1]=%r" % texts[1])

    cmd_asr = remote_cmd_for(fs7, "rig_asr_batch.py")
    check(cmd_asr is not None and "rig_asr_batch.py" in cmd_asr,
          "transcribe_many runs rig_asr_batch.py over ssh")
    check(cmd_asr is not None and client.VENV_ASR in cmd_asr,
          "transcribe_many uses the asr venv python")
    check(cmd_asr is not None and "CUDA_VISIBLE_DEVICES=1" in cmd_asr,
          "transcribe_many pins CUDA_VISIBLE_DEVICES=1")

    # ---- tts_batch (LOCAL mode) ----------------------------------------------------------
    os.environ["FORGE_AUDIO_LOCAL"] = "1"
    fs8 = FakeSubprocess()
    client.subprocess.run = fs8.run
    client._DEPLOYED = True
    out_dir_loc = os.path.join(client.tempfile.gettempdir(), "audio_matrix_batch_loc")
    os.makedirs(out_dir_loc, exist_ok=True)
    fs8.rig_stdout = json.dumps({
        "ok": True,
        "items": [{"id": "d1", "out": "/tmp/xx/d1.wav", "duration": 1.0}],
        "seconds": 8.0,
    })
    result_loc = client.tts_batch({
        "design": [{"id": "d1", "instruct": "desc", "text": "Hi", "out": "d1.wav"}],
    }, out_dir_loc)
    check(not fs8.ssh_calls(), "LOCAL tts_batch makes NO ssh calls")
    check(not fs8.scp_calls(), "LOCAL tts_batch makes NO scp calls")
    bc_loc = fs8.bash_calls()
    check(bool(bc_loc), "LOCAL tts_batch runs via bash -lc")
    lc_loc = bc_loc[-1][2] if bc_loc else ""
    check(client.VENV_TTS in lc_loc and "rig_dialogue.py" in lc_loc,
          "LOCAL tts_batch uses tts venv + rig_dialogue.py")
    check("CUDA_VISIBLE_DEVICES=1" in lc_loc,
          "LOCAL tts_batch still pins CUDA_VISIBLE_DEVICES=1")
    check(result_loc.get("ok") is True, "LOCAL tts_batch returns ok=True")

    # ---- transcribe_many (LOCAL mode) ----------------------------------------------------
    fs9 = FakeSubprocess()
    client.subprocess.run = fs9.run
    client._DEPLOYED = True
    fs9.rig_stdout = json.dumps({
        "ok": True,
        "items": [
            {"audio": "audio_matrix_a.wav", "text": "hello there"},
            {"audio": "audio_matrix_b.wav", "text": "good day"},
        ],
        "seconds": 4.0,
    })
    texts_loc = client.transcribe_many([wav_a, wav_b])
    check(not fs9.ssh_calls(), "LOCAL transcribe_many makes NO ssh calls")
    check(not fs9.scp_calls(), "LOCAL transcribe_many makes NO scp calls")
    bc_loc2 = fs9.bash_calls()
    check(bool(bc_loc2), "LOCAL transcribe_many runs via bash -lc")
    lc_loc2 = bc_loc2[-1][2] if bc_loc2 else ""
    check(client.VENV_ASR in lc_loc2 and "rig_asr_batch.py" in lc_loc2,
          "LOCAL transcribe_many uses asr venv + rig_asr_batch.py")
    check(len(texts_loc) == 2 and texts_loc[0] == "hello there",
          "LOCAL transcribe_many preserves order (%r)" % texts_loc)
    os.environ.pop("FORGE_AUDIO_LOCAL", None)

    # ---- RIG_SCRIPTS includes the new batch scripts -------------------------------------
    check("rig_dialogue.py" in client.RIG_SCRIPTS,
          "RIG_SCRIPTS includes rig_dialogue.py")
    check("rig_asr_batch.py" in client.RIG_SCRIPTS,
          "RIG_SCRIPTS includes rig_asr_batch.py")

    # ---- rig_dialogue.py source guards (it imports torch, so it cannot run here) -----------
    with open(os.path.join(ROOT, "audio", "rig_dialogue.py"), encoding="utf-8") as fh:
        rd = fh.read()
    clone_body = rd[rd.find("def _process_clone"):rd.find("def main")]
    check("prompt is None" in clone_body and "raise RuntimeError" in clone_body,
          "rig_dialogue: a clone with no usable reference is an ERROR, never an unvoiced line")
    check("voice_clone_prompt=prompt" in clone_body and "ref_audio" not in clone_body,
          "rig_dialogue: clones always pass the prebuilt prompt")
    check("t0 + gen_s" not in rd, "rig_dialogue: per-item seconds are not double-counted")

    print("\n%d checks, %d failures" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for m in FAILS:
            print("  FAILED: %s" % m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())