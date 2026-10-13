#!/usr/bin/env python3
"""Mac-side client for Forge Lab Studio audio (stdlib only).

Calls the Qwen3 speech rig on `forge` over ssh/scp. Each call is a ONE-SHOT remote
process that loads the model, does its job, writes an artifact, and exits -- so GPU1
memory is released between calls and never blocks the LLM or a Raylight render.

Flow per call:
  mkdir /tmp/forge-audio/<uuid>  ->  scp inputs up  ->  ssh run the rig script
  (CUDA_VISIBLE_DEVICES=1, the venv's own python)  ->  scp results down  ->  rm -rf the job dir

Public API:
  deploy()                                   copy rig_*.py to /home/ryan/forge-lab-audio/
  tts_design(desc, text, out_path)           voice design            -> writes wav
  tts_clone(ref_wav, ref_text, text, out_path) voice clone (ICL)     -> writes wav
  align(wav, text)                           forced alignment        -> list of {word,start,end}
  transcribe(wav)                            ASR intelligibility     -> text

Rig-side scripts and their real APIs (read from the installed packages on forge; see the
rig_*.py docstrings) live in the same directory as this file.
"""
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import uuid

HOST = "ryan@forge"
RIG_DIR = "/home/ryan/forge-lab-audio"
JOBS_ROOT = "/tmp/forge-audio"

# Two venvs because qwen-tts (transformers==4.57.3) and qwen-asr (==4.57.6) cannot coexist.
VENV_TTS = "/home/ryan/qwen-audio/tts/.venv/bin/python"
VENV_ASR = "/home/ryan/qwen-audio/asr/.venv/bin/python"

_HERE = os.path.dirname(os.path.abspath(__file__))
RIG_SCRIPTS = ("rig_tts.py", "rig_align.py", "rig_asr.py",
               "rig_dialogue.py", "rig_asr_batch.py")

_DEPLOYED = False


def _run(argv):
    """Run a subprocess; raise on non-zero with captured output."""
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("command failed (%d): %s\n%s\n%s"
                           % (p.returncode, " ".join(argv), p.stdout, p.stderr))
    return p


def _ssh(remote_cmd):
    """Run a shell command on the rig. The command is quoted as a single ssh argument."""
    return _run(["ssh", HOST, remote_cmd])


def _scp_up(local, remote):
    _run(["scp", "-q", local, "%s:%s" % (HOST, remote)])


def _scp_down(remote, local):
    _run(["scp", "-q", "%s:%s" % (HOST, remote), local])


# ---------------------------------------------------------------------------
# LOCAL mode — run the rig scripts HERE (the pipeline runs on `forge` overnight).
#
# ⚠️ The Mac always reaches the rig over ssh; the rig cannot ssh to itself. So when
# FORGE_AUDIO_LOCAL=1 (or we ARE `forge`), skip ssh/scp entirely and invoke the rig
# script's own venv python with CUDA_VISIBLE_DEVICES=1.
# ---------------------------------------------------------------------------
def _local():
    """True when the rig scripts must run on THIS machine."""
    v = os.environ.get("FORGE_AUDIO_LOCAL", "").strip()
    if v == "1":
        return True
    if v == "0":
        return False
    try:
        return socket.gethostname().split(".")[0] == "forge"
    except Exception:
        return False


def _run_local(venv_python, script_name, argv, cwd):
    """Run one rig script directly, exactly as the ssh path would word the command."""
    quoted = " ".join(shlex.quote(a) for a in argv)
    cmd = "cd %s && CUDA_VISIBLE_DEVICES=1 %s %s/%s %s" % (
        shlex.quote(cwd), venv_python, RIG_DIR, script_name, quoted)
    return _run(["bash", "-lc", cmd])


def _local_job_dir():
    return tempfile.mkdtemp(prefix="forge-audio-")


def deploy():
    """Copy the rig scripts into place on forge (idempotent, cheap)."""
    global _DEPLOYED
    files = [os.path.join(_HERE, s) for s in RIG_SCRIPTS if os.path.isfile(os.path.join(_HERE, s))]
    if not files:
        raise RuntimeError("no rig scripts found next to client.py")
    if _local():
        os.makedirs(RIG_DIR, exist_ok=True)
        for f in files:
            shutil.copyfile(f, os.path.join(RIG_DIR, os.path.basename(f)))
        _DEPLOYED = True
        return
    _ssh("mkdir -p %s" % shlex.quote(RIG_DIR))
    _run(["scp", "-q"] + files + ["%s:%s/" % (HOST, RIG_DIR)])
    _DEPLOYED = True


def _ensure_deployed():
    if not _DEPLOYED:
        deploy()


def _job_dir():
    jid = uuid.uuid4().hex
    remote = "%s/%s" % (JOBS_ROOT, jid)
    _ssh("mkdir -p %s" % shlex.quote(remote))
    return remote


def _cleanup(remote):
    try:
        _ssh("rm -rf %s" % shlex.quote(remote))
    except Exception:
        pass  # best effort; a leftover dir under /tmp is harmless


def _parse_json_line(stdout):
    """The rig scripts print exactly one json line; find and parse it."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise RuntimeError("no json line in rig output:\n%s" % stdout)


def _remote_tts_cmd(job_remote, venv_python, argv):
    """Build the remote command for a TTS run. argv tokens are shell-quoted."""
    quoted = " ".join(shlex.quote(a) for a in argv)
    return "cd %s && CUDA_VISIBLE_DEVICES=1 %s %s/%s %s" % (
        shlex.quote(job_remote), venv_python, RIG_DIR, "rig_tts.py", quoted)


def _remote_dialogue_cmd(job_remote, venv_python, argv):
    """Build the remote command for rig_dialogue.py batch TTS run."""
    quoted = " ".join(shlex.quote(a) for a in argv)
    return "cd %s && CUDA_VISIBLE_DEVICES=1 %s %s/%s %s" % (
        shlex.quote(job_remote), venv_python, RIG_DIR, "rig_dialogue.py", quoted)


def _remote_asr_batch_cmd(job_remote, argv):
    """Build the remote command for rig_asr_batch.py run."""
    quoted = " ".join(shlex.quote(a) for a in argv)
    return "cd %s && CUDA_VISIBLE_DEVICES=1 %s %s/%s %s" % (
        shlex.quote(job_remote), VENV_ASR, RIG_DIR, "rig_asr_batch.py", quoted)


def _remote_align_cmd(job_remote, argv):
    quoted = " ".join(shlex.quote(a) for a in argv)
    return "cd %s && CUDA_VISIBLE_DEVICES=1 %s %s/%s %s" % (
        shlex.quote(job_remote), VENV_ASR, RIG_DIR, "rig_align.py", quoted)


def _remote_asr_cmd(job_remote, argv):
    quoted = " ".join(shlex.quote(a) for a in argv)
    return "cd %s && CUDA_VISIBLE_DEVICES=1 %s %s/%s %s" % (
        shlex.quote(job_remote), VENV_ASR, RIG_DIR, "rig_asr.py", quoted)


def tts_design(desc, text, out_path, language="English"):
    """Design a voice from a text description and speak `text`. Writes `out_path`."""
    _ensure_deployed()
    argv = ["--design", desc, "--text", text, "--language", language, "--out", "out.wav"]
    if _local():
        job = _local_job_dir()
        try:
            p = _run_local(VENV_TTS, "rig_tts.py", argv, job)
            info = _parse_json_line(p.stdout)
            if not info.get("ok"):
                raise RuntimeError("rig TTS failed: %s" % info.get("error", info))
            shutil.copyfile(os.path.join(job, "out.wav"), out_path)
            return info
        finally:
            shutil.rmtree(job, ignore_errors=True)
    job = _job_dir()
    try:
        cmd = _remote_tts_cmd(job, VENV_TTS, argv)
        p = _ssh(cmd)
        info = _parse_json_line(p.stdout)
        if not info.get("ok"):
            raise RuntimeError("rig TTS failed: %s" % info.get("error", info))
        _scp_down("%s/out.wav" % job, out_path)
        return info
    finally:
        _cleanup(job)


def tts_clone(ref_wav, ref_text, text, out_path, language="English"):
    """Clone the voice in `ref_wav` (with its transcript `ref_text`) and speak `text`."""
    _ensure_deployed()
    if _local():
        job = _local_job_dir()
        try:
            argv = ["--clone", os.path.abspath(ref_wav), "--ref-text", ref_text, "--text", text,
                    "--language", language, "--out", os.path.abspath(out_path)]
            p = _run_local(VENV_TTS, "rig_tts.py", argv, job)
            info = _parse_json_line(p.stdout)
            if not info.get("ok"):
                raise RuntimeError("rig clone failed: %s" % info.get("error", info))
            return info
        finally:
            shutil.rmtree(job, ignore_errors=True)
    job = _job_dir()
    try:
        _scp_up(ref_wav, "%s/ref.wav" % job)
        argv = ["--clone", "ref.wav", "--ref-text", ref_text, "--text", text,
                "--language", language, "--out", "out.wav"]
        cmd = _remote_tts_cmd(job, VENV_TTS, argv)
        p = _ssh(cmd)
        info = _parse_json_line(p.stdout)
        if not info.get("ok"):
            raise RuntimeError("rig clone failed: %s" % info.get("error", info))
        _scp_down("%s/out.wav" % job, out_path)
        return info
    finally:
        _cleanup(job)


def _parse_words(raw):
    """Validate the aligner output. Raises ValueError on anything malformed."""
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError("aligner output is not valid JSON: %s" % e)
    if not isinstance(data, list):
        raise ValueError("aligner output must be a list, got %s" % type(data).__name__)
    out = []
    for i, it in enumerate(data):
        if not isinstance(it, dict):
            raise ValueError("aligner item %d is not an object" % i)
        if "word" not in it or "start" not in it or "end" not in it:
            raise ValueError("aligner item %d missing word/start/end: %r" % (i, it))
        if not isinstance(it["start"], (int, float)) or not isinstance(it["end"], (int, float)):
            raise ValueError("aligner item %d has non-numeric times: %r" % (i, it))
        out.append({"word": str(it["word"]), "start": float(it["start"]), "end": float(it["end"])})
    return out


def align(wav, text, language="English"):
    """Return [{'word','start','end'}, ...] for `wav` against transcript `text`."""
    _ensure_deployed()
    if _local():
        job = _local_job_dir()
        try:
            outj = os.path.join(job, "words.json")
            argv = ["--audio", os.path.abspath(wav), "--text", text, "--language", language,
                    "--out", outj]
            p = _run_local(VENV_ASR, "rig_align.py", argv, job)
            info = _parse_json_line(p.stdout)
            if not info.get("ok"):
                raise RuntimeError("rig align failed: %s" % info.get("error", info))
            with open(outj, "r", encoding="utf-8") as f:
                raw = f.read()
            return _parse_words(raw)
        finally:
            shutil.rmtree(job, ignore_errors=True)
    job = _job_dir()
    try:
        _scp_up(wav, "%s/in.wav" % job)
        argv = ["--audio", "in.wav", "--text", text, "--language", language, "--out", "words.json"]
        cmd = _remote_align_cmd(job, argv)
        p = _ssh(cmd)
        info = _parse_json_line(p.stdout)
        if not info.get("ok"):
            raise RuntimeError("rig align failed: %s" % info.get("error", info))
        fd, tmp = tempfile.mkstemp(prefix="words_", suffix=".json")
        os.close(fd)
        try:
            _scp_down("%s/words.json" % job, tmp)
            with open(tmp, "r", encoding="utf-8") as f:
                raw = f.read()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return _parse_words(raw)
    finally:
        _cleanup(job)


def transcribe(wav, language="English"):
    """Return the transcript text Qwen3-ASR hears in `wav` (intelligibility check)."""
    _ensure_deployed()
    if _local():
        job = _local_job_dir()
        try:
            argv = ["--audio", os.path.abspath(wav), "--language", language]
            p = _run_local(VENV_ASR, "rig_asr.py", argv, job)
            info = _parse_json_line(p.stdout)
            if not info.get("ok"):
                raise RuntimeError("rig ASR failed: %s" % info.get("error", info))
            return info.get("text", "")
        finally:
            shutil.rmtree(job, ignore_errors=True)
    job = _job_dir()
    try:
        _scp_up(wav, "%s/in.wav" % job)
        argv = ["--audio", "in.wav", "--language", language]
        cmd = _remote_asr_cmd(job, argv)
        p = _ssh(cmd)
        info = _parse_json_line(p.stdout)
        if not info.get("ok"):
            raise RuntimeError("rig ASR failed: %s" % info.get("error", info))
        return info.get("text", "")
    finally:
        _cleanup(job)


def _ref_name(i, path):
    """Unique job-dir name for the i-th reference wav.

    ⚠️ NOT the bare basename: every voice's sample is called `neutral.wav`, so two characters
    in one batch uploaded to the SAME file and the second silently became both voices.
    """
    return "ref%02d_%s" % (i, os.path.basename(path))


def tts_batch(job, out_dir, language="English"):
    """Run a batch TTS job on the rig.

    Args:
        job: dict with design/clone/voices per rig_dialogue.py protocol.
        out_dir: local directory where output wavs are written.
        language: default language (overridden per job dict if present).

    Returns:
        The parsed rig JSON (dict). Successful items have ``out`` rewritten to
        a local absolute path inside ``out_dir``. Items with errors keep the
        original rig response (no ``out`` or with an ``error`` key).
    """
    _ensure_deployed()
    job = json.loads(json.dumps(job))  # deep copy: never rewrite the CALLER's ref paths
    job.setdefault("language", language)
    voices = job.get("voices", {})

    def _local_batch():
        jd = _local_job_dir()
        try:
            # Copy ref wavs into job dir, rewrite paths
            for i, (vk, v) in enumerate(list(voices.items())):
                if os.path.isfile(v.get("ref_wav", "")):
                    name = _ref_name(i, v["ref_wav"])
                    shutil.copyfile(v["ref_wav"], os.path.join(jd, name))
                    v["ref_wav"] = name
            # Write job.json
            jpath = os.path.join(jd, "job.json")
            with open(jpath, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False)
            argv = ["--job", "job.json"]
            p = _run_local(VENV_TTS, "rig_dialogue.py", argv, jd)
            info = _parse_json_line(p.stdout)
            if not info.get("ok"):
                raise RuntimeError("rig dialogue failed: %s" % info.get("error", info))
            # Copy outputs to out_dir, rewrite paths
            _rewrite_out_paths(info, jd, out_dir)
            return info
        finally:
            shutil.rmtree(jd, ignore_errors=True)

    def _remote_batch():
        remote = _job_dir()
        try:
            # Upload ref wavs, rewrite paths in job dict
            remote_wavs = {}
            for i, (vk, v) in enumerate(list(voices.items())):
                rp = v.get("ref_wav", "")
                if os.path.isfile(rp):
                    basename = _ref_name(i, rp)
                    _scp_up(rp, "%s/%s" % (remote, basename))
                    remote_wavs[vk] = basename
                    v["ref_wav"] = basename
            # Write job.json locally, scp it up
            jpath = _local_job_dir()
            try:
                jfile = os.path.join(jpath, "job.json")
                with open(jfile, "w", encoding="utf-8") as f:
                    json.dump(job, f, ensure_ascii=False)
                _scp_up(jfile, "%s/job.json" % remote)
            finally:
                shutil.rmtree(jpath, ignore_errors=True)
            # Run
            argv = ["--job", "job.json"]
            cmd = _remote_dialogue_cmd(remote, VENV_TTS, argv)
            p = _ssh(cmd)
            info = _parse_json_line(p.stdout)
            if not info.get("ok"):
                raise RuntimeError("rig dialogue failed: %s" % info.get("error", info))
            # scp outputs down
            _rewrite_out_paths(info, remote, out_dir)
            return info
        finally:
            _cleanup(remote)

    if _local():
        return _local_batch()
    return _remote_batch()


def _rewrite_out_paths(info, rig_job_dir, out_dir):
    """Rewrite ``out`` in each info item from rig absolute to local path.

    For remote: scps the file down first, then rewrites.
    For local: copies the file, then rewrites.
    Mutates info in place, returns None.
    """
    for item in info.get("items", []):
        rpath = item.get("out")
        if not rpath or "error" in item:
            continue
        basename = os.path.basename(rpath)
        local_dst = os.path.join(out_dir, basename)
        if _local():
            shutil.copyfile(rpath, local_dst)
        else:
            _scp_down("%s/%s" % (rig_job_dir, basename), local_dst)
        item["out"] = os.path.abspath(local_dst)


def transcribe_many(paths, language="English"):
    """Transcribe multiple audio files on the rig. Order preserved.

    Args:
        paths: list of local wav paths.
        language: ASR language.

    Returns:
        list[str] of transcripts, same order as ``paths``.
    """
    _ensure_deployed()

    def _local_impl():
        jd = _local_job_dir()
        try:
            # Copy wavs into job dir
            basenames = []
            for p in paths:
                dst = os.path.join(jd, os.path.basename(p))
                shutil.copyfile(p, dst)
                basenames.append(os.path.basename(p))
            argv = ["--audio"] + basenames + ["--language", language]
            p = _run_local(VENV_ASR, "rig_asr_batch.py", argv, jd)
            info = _parse_json_line(p.stdout)
            if not info.get("ok"):
                raise RuntimeError("rig batch ASR failed: %s" % info.get("error", info))
            return _extract_texts(info, basenames)
        finally:
            shutil.rmtree(jd, ignore_errors=True)

    def _remote_impl():
        remote = _job_dir()
        try:
            basenames = []
            for p in paths:
                basename = os.path.basename(p)
                _scp_up(p, "%s/%s" % (remote, basename))
                basenames.append(basename)
            argv = ["--audio"] + basenames + ["--language", language]
            cmd = _remote_asr_batch_cmd(remote, argv)
            p = _ssh(cmd)
            info = _parse_json_line(p.stdout)
            if not info.get("ok"):
                raise RuntimeError("rig batch ASR failed: %s" % info.get("error", info))
            return _extract_texts(info, basenames)
        finally:
            _cleanup(remote)

    if _local():
        return _local_impl()
    return _remote_impl()


def _extract_texts(info, basenames):
    """Map rig ASR items back to the original order by basename."""
    by_name = {os.path.basename(it.get("audio", "")): it.get("text", "")
               for it in info.get("items", [])}
    return [by_name.get(bn, "") for bn in basenames]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Forge Lab Studio audio client")
    ap.add_argument("--deploy", action="store_true", help="copy rig scripts to forge")
    ap.add_argument("--design")
    ap.add_argument("--clone")
    ap.add_argument("--ref-text")
    ap.add_argument("--text")
    ap.add_argument("--out", default="out.wav")
    ap.add_argument("--align", help="wav to align (with --text)")
    ap.add_argument("--asr", help="wav to transcribe")
    args = ap.parse_args()

    if args.deploy:
        deploy()
        print("deployed to %s" % RIG_DIR)
    if args.design and args.text:
        print(tts_design(args.design, args.text, args.out))
    if args.clone and args.text:
        print(tts_clone(args.clone, args.ref_text, args.text, args.out))
    if args.align and args.text:
        print(json.dumps(align(args.align, args.text), indent=2))
    if args.asr:
        print(transcribe(args.asr))