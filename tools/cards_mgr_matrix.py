#!/usr/bin/env python3
"""GPU-free checks for the CARD MANAGER (cards.py, 2026-10-17). The rig is FAKED at the two
seams (cards._ssh_out, cards._http); the nvidia-smi/ps text is shaped like Ryan's real
2026-10-17 output (a second ComfyUI on :8189 holding 21 GB of card 2).

    python3 tools/cards_mgr_matrix.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.pop("FORGE_LAB_NOCARDCHECK", None)
import cards  # noqa: E402

N, FAILS = [0], []


def check(cond, label):
    N[0] += 1
    print("  %-4s %s" % ("ok" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


U0, U1 = "GPU-aaaa", "GPU-bbbb"
NOW = time.time()


class Rig:
    """A fake forge: cards, processes, two ComfyUI queues, Ollama. Records every call."""

    def __init__(self):
        self.util = {0: 0, 1: 0}
        self.procs = {}          # pid -> {card: mb}, etime, args
        self.queues = {8188: {"queue_running": [], "queue_pending": []},
                       8189: {"queue_running": [], "queue_pending": []}}
        self.ollama = []
        self.calls = []
        self.ssh_fail = False
        self.on_free = {}        # port -> callable when /free is posted
        self.prompt_ids = "same"

    def add(self, pid, args, mem, etime=60):
        self.procs[pid] = {"mem": dict(mem), "etime": etime, "args": args}

    def used(self, card):
        return sum(p["mem"].get(card, 0) for p in self.procs.values())

    def ssh(self, cmd, timeout=12):
        self.calls.append(("ssh", cmd))
        if self.ssh_fail:
            raise RuntimeError("ssh: Could not resolve hostname forge")
        if cmd.startswith("kill "):
            pid = int(cmd.split()[1].rstrip(";"))
            self.procs.pop(pid, None)
            return ""
        out = []
        for c, u in ((0, U0), (1, U1)):
            out.append("%d, %s, %d, 24576, %d" % (c, u, self.used(c), self.util[c]))
        out.append("@@")
        for pid, p in self.procs.items():
            for c, mb in p["mem"].items():
                out.append("%s, %d, %d" % (U0 if c == 0 else U1, pid, mb))
        out.append("@@")
        for pid, p in self.procs.items():
            out.append("%7d %9d %s" % (pid, p["etime"], p["args"]))
        return "\n".join(out)

    def http(self, method, url, body=None, timeout=8):
        self.calls.append((method, url, body))
        port = int(url.split(":")[2].split("/")[0]) if url.count(":") >= 2 else 0
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        if port == 11434:
            if path == "/api/ps":
                return {"models": [{"name": m} for m in self.ollama]}
            if path == "/api/generate" and (body or {}).get("keep_alive") == 0:
                self.ollama = [m for m in self.ollama if m != body.get("model")]
                for pid, p in list(self.procs.items()):
                    if "ollama" in p["args"] and not self.ollama:
                        self.procs.pop(pid)
                return {}
        q = self.queues.get(port)
        if q is None:
            raise OSError("connection refused")
        if path == "/queue" and method == "GET":
            return json.loads(json.dumps(q))
        if path == "/queue":
            ids = set((body or {}).get("delete") or [])
            q["queue_pending"] = [it for it in q["queue_pending"] if it[1] not in ids]
            return {}
        if path == "/interrupt":
            q["queue_running"] = []
            return {}
        if path == "/history":
            return {}
        if path == "/free":
            fn = self.on_free.get(port)
            if fn:
                fn()
            return {}
        if path == "/prompt":
            pid = body.get("prompt_id") if self.prompt_ids == "same" else "new-" + body.get(
                "prompt_id", "?")
            q["queue_pending"].append([99, pid, body["prompt"], body.get("extra_data"), []])
            return {"prompt_id": pid}
        raise OSError("unexpected %s %s" % (method, url))


def fresh(**kw):
    rig = Rig()
    cards._ssh_out = rig.ssh
    cards._http = rig.http
    cards.HELD.clear()
    cards.FOLLOW.clear()
    cards._CACHE["snap"] = None
    cards.CFG.update({"held_path": None})
    INDEX.clear()
    cards.configure(ssh="name@forge", host="forge", main_port=8188,
                    ollama="http://forge:11434", index_lookup=lambda pid: INDEX.get(pid))
    return rig


INDEX = {}
COMFY = ".venv/bin/python main.py --listen 0.0.0.0 --port 8188"
COMFY2 = (".venv/bin/python main.py --listen 0.0.0.0 --port 8189 --cuda-device 1 "
          "--temp-directory /home/ryan/comfy-gpu1/temp")
TURBO_G = {"1": {"class_type": "RayInitializer", "inputs": {}}, "2": {"class_type": "X"}}
PLAIN_G = {"1": {"class_type": "KSampler", "inputs": {}}}

# ------------------------------------------------------------------ §1 reading the rig
print("§1 reading the rig: who holds each card")
rig = fresh()
rig.add(1760815, "/home/ryan/ComfyUI/.venv/bin/python main.py --listen 0.0.0.0 --port 8188",
        {0: 9906, 1: 256}, etime=61000)
rig.add(880101, COMFY2, {1: 21266}, etime=4 * 86400 + 8 * 3600)
s = cards.snapshot()
c0, c1 = s["cards"][0], s["cards"][1]
check(c1["used"] == 21522 and c1["free"] == 24576 - 21522, "card 2 used/free from nvidia-smi")
h2 = [h for h in c1["holders"] if h["pid"] == 880101][0]
check(h2["kind"] == "comfy" and h2["port"] == 8189 and h2["mb"] == 21266,
      "the :8189 ComfyUI is recognised by its --port, with its 21 GB on card 2")
check(h2["who"] == "ComfyUI (card 2)" and h2["freeable"] and not h2["busy"]
      and "keeping a model loaded" in h2["doing"],
      "an idle ComfyUI reads as 'nothing right now, just keeping a model loaded' + freeable")
h1 = [h for h in c0["holders"] if h["pid"] == 1760815][0]
check(h1["port"] == 8188 and h1["who"] == "ComfyUI", "no --port… or --port 8188 is the main one")
check(cards.ago(4 * 86400 + 8 * 3600) == "4 days" and cards.ago(150) == "2 min"
      and cards.ago(40) == "40 s" and cards.ago(7500) == "2 h 5 min", "durations read naturally")

rig = fresh()
rig.add(500, "/home/ryan/qwen-audio/tts/.venv/bin/python /home/ryan/forge-lab-audio/"
             "rig_dialogue.py --job job.json", {1: 2440}, etime=40)
rig.add(501, "/usr/local/bin/ollama runner --model /x", {1: 19000}, etime=300)
rig.ollama = ["qwen3:32b"]
rig.add(502, "python3 something_else.py --flag", {1: 4000}, etime=20 * 60)
s = cards.snapshot()
hs = {h["pid"]: h for h in s["cards"][1]["holders"]}
check(hs[500]["kind"] == "voice" and hs[500]["busy"]
      and hs[500]["doing"] == "making a voice line with Qwen3-TTS, running 40 s",
      "a voice job: 'making a voice line with Qwen3-TTS, running 40 s' (%r)" % hs[500]["doing"])
check(hs[501]["who"] == "the script writer (Ollama)" and hs[501]["freeable"]
      and "qwen3:32b" in hs[501]["doing"], "the writer is named, its model listed, freeable")
check(hs[502]["stuck"] and "looks stuck" in hs[502]["doing"] and not hs[502]["busy"],
      "an unknown program holding 4 GB, card idle, 20 min -> STUCK")
rig.util[1] = 80
cards._CACHE["snap"] = None
hs = {h["pid"]: h for h in cards.snapshot()["cards"][1]["holders"]}
check(not hs[502]["stuck"] and hs[502]["busy"], "...but not stuck while the card is working")

rig = fresh()
rig.add(1, COMFY, {0: 9000, 1: 18000}, etime=600)
rig.queues[8188]["queue_running"] = [[5, "run-1", TURBO_G, {"client_id": "c"}, []]]
rig.queues[8188]["queue_pending"] = [[6, "p-1", PLAIN_G, {}, []]]
INDEX["run-1"] = {"id": "run-1", "video": True, "prompt": "Gustav sniffs the loaf of rye",
                  "started": time.time() - 130}
h = cards.snapshot()["cards"][1]["holders"][0]
check(h["busy"] and h["turbo"] and h["doing"] == "making a video on both cards (Turbo) — "
      "“Gustav sniffs the loaf of rye”, started 2 min ago; 1 more waiting behind it",
      "a running Turbo take is described from the run index (%r)" % h["doing"])
INDEX.clear()
cards._CACHE["snap"] = None
h = cards.snapshot()["cards"][1]["holders"][0]
check("didn't start" in h["doing"], "a render the app didn't start says so")

# ------------------------------------------------------------------ §2 make_room
print("§2 make_room: free what is idle, name what is working")
rig = fresh()
rig.add(1, COMFY, {0: 9000, 1: 256})
r = cards.make_room("voice", 1)
check(r["ok"] and not r["freed"] and not [c for c in rig.calls if c[0] == "POST"],
      "room already: nothing is touched")

rig = fresh()
rig.add(880101, COMFY2, {1: 21266}, etime=4 * 86400)
rig.on_free[8189] = lambda: rig.procs[880101]["mem"].update({1: 300})
r = cards.make_room("voice", 1, settle_s=3)
frees = [c for c in rig.calls if c[0] == "POST" and c[1].endswith("/free")]
check(r["ok"] and r["freed"] == ["ComfyUI (card 2)"] and len(frees) == 1
      and "8189" in frees[0][1] and frees[0][2] == {"unload_models": True, "free_memory": True},
      "TODAY'S BUG: the idle :8189 ComfyUI is told to /free, and the voice line goes ahead")

rig = fresh()
rig.add(501, "/usr/local/bin/ollama runner", {1: 19000})
rig.ollama = ["qwen3:32b"]
r = cards.make_room("voice", 1, settle_s=3)
check(r["ok"] and r["freed"] == ["the script writer (Ollama)"] and not rig.ollama,
      "an idle writer is unloaded (every resident model)")

rig = fresh()
rig.add(1, COMFY, {0: 9000, 1: 21000})
rig.queues[8188]["queue_running"] = [[5, "run-1", TURBO_G, {}, []]]
INDEX["run-1"] = {"id": "run-1", "video": True, "prompt": "Gustav sniffs", "started": time.time()}
try:
    cards.make_room("voice", 1)
    cb = None
except cards.CardBusy as e:
    cb = e
check(cb is not None and str(cb).startswith("Card 2 is busy, so your voice line can't start yet."
                                             " ComfyUI is making a video on both cards (Turbo)"),
      "a WORKING Turbo render -> CardBusy in plain words (%r)" % (str(cb)[:120],))
check(cb is not None and [k for k, _ in cb.options()] == ["wait", "first", "stop", "cancel"]
      and dict(cb.options())["first"] == "Do mine first (the video goes back in line)"
      and dict(cb.options())["stop"] == "Stop the video",
      "a render can be waited for, put back in line, or stopped")
check(not [c for c in rig.calls if c[0] == "POST"], "nothing working is touched by make_room")
r = cards.make_room("turbo", 1, same_queue_port=8188)
check(r["ok"], "a Turbo run on the SAME ComfyUI just queues behind it (not a blocker)")

rig = fresh()                       # the 2026-10-17 screenshot: a small program on a busy card
rig.add(1, COMFY, {0: 9000, 1: 21000})
rig.add(13, "python3 mystery.py", {1: 2000}, etime=3600)
rig.util[1] = 97
rig.queues[8188]["queue_running"] = [[5, "run-1", TURBO_G, {}, []]]
try:
    cards.make_room("voice", 1)
    cb = None
except cards.CardBusy as e:
    cb = e
check(cb is not None and [h["pid"] for h in cb.holders] == [1] and "mystery" not in str(cb)
      and "first" in dict(cb.options()),
      "only what EXPLAINS the shortage is named (the render, not a 2 GB bystander), so "
      "Do mine first stays on offer")
s = cards.snapshot()
h_c1 = [h for h in s["cards"][0]["holders"] if h["pid"] == 1][0]
h_c2 = [h for h in s["cards"][1]["holders"] if h["pid"] == 1][0]
check(h_c2.get("same_as") == 0 and h_c1.get("same_as") is None
      and h_c2["doing"].startswith("making ") and "same work" not in h_c2["doing"],
      "one Turbo render on both cards: the panel can point card 2 at card 1, the prompt "
      "keeps the full sentence")

rig = fresh()
rig.add(500, "x/rig_fish.py --job j", {1: 20000}, etime=30)
try:
    cards.make_room("voice", 1)
    cb = None
except cards.CardBusy as e:
    cb = e
check(cb is not None and [k for k, _ in cb.options()] == ["wait", "stop", "cancel"]
      and "making a voice line with Fish" in str(cb),
      "a voice process can be waited for or stopped, not 'put back in line'")

rig = fresh()
rig.ssh_fail = True
r = cards.make_room("voice", 1)
check(r["ok"] and "couldn't read" in r["note"], "rig unreadable: let the job try (old behaviour)")
os.environ["FORGE_LAB_NOCARDCHECK"] = "1"
rig = fresh()
rig.add(1, COMFY, {1: 24000})
r = cards.make_room("voice", 1)
check(r["ok"] and not rig.calls, "FORGE_LAB_NOCARDCHECK=1: no rig calls at all")
os.environ.pop("FORGE_LAB_NOCARDCHECK")
cards.CFG["enabled"] = False
r = cards.make_room("voice", 1)
check(r["ok"] and not rig.calls, "not configured (tools, the rig): a no-op")

# ------------------------------------------------------------------ §3 do mine first
print("§3 DO MINE FIRST: hold, run mine, put it back")
for same in (True, False):
    rig = fresh()
    rig.prompt_ids = "same" if same else "new"
    rig.add(1, COMFY, {0: 9000, 1: 21000})
    rig.queues[8188]["queue_running"] = [[5, "R1", TURBO_G, {"client_id": "app"}, []]]
    rig.queues[8188]["queue_pending"] = [[7, "P2", PLAIN_G, {}, []], [6, "P1", TURBO_G, {}, []]]
    td = tempfile.mkdtemp()
    cards.CFG["held_path"] = os.path.join(td, "held.json")
    try:
        cards.make_room("voice", 1)
    except cards.CardBusy as e:
        cb = e
    n = cards.hold(cb.holders, 1, "dialogue")
    posts = [(c[1].rsplit("/", 1)[1], c[2]) for c in rig.calls if c[0] == "POST"]
    if same:
        check(n == 2 and cards.follow("R1") is None and cards.follow("P1") is None
              and cards.follow("P2") == "P2",
              "the running Turbo take + the queued Turbo take are HELD; the plain one stays")
        check(posts[:3] == [("queue", {"delete": ["P1"]}), ("interrupt", {}),
                            ("history", {"delete": ["R1"]})],
              "order: drop the queued one, interrupt, wipe the interrupted record (%s)"
              % posts[:3])
        check(rig.queues[8188]["queue_pending"] == [[7, "P2", PLAIN_G, {}, []]],
              "the card-1-only render keeps its place in line")
        check(json.load(open(cards.CFG["held_path"]))["held"]["dialogue"][0]["id"] == "R1",
              "held work is saved to disk (survives a quit)")
    back = cards.release("dialogue")
    re_q = [c[2] for c in rig.calls if c[0] == "POST" and c[1].endswith("/prompt")]
    if same:
        check(back == 2 and [b["prompt_id"] for b in re_q] == ["R1", "P1"]
              and re_q[0]["client_id"] == "app" and re_q[0]["prompt"] == TURBO_G,
              "release re-queues them IN ORDER, same id, same graph, same client")
        check(cards.follow("R1") == "R1" and not cards.HELD, "...and they are no longer held")
    else:
        check(cards.follow("R1") == "new-R1" and cards.follow("P1") == "new-P1",
              "a ComfyUI that assigns new ids: the old take follows its new id")
check(cards.release("nobody") == 0, "release with nothing held is a no-op")

rig = fresh()
td = tempfile.mkdtemp()
cards.CFG["held_path"] = os.path.join(td, "held.json")
json.dump({"held": {"episode": [{"port": 8188, "id": "Z9", "graph": PLAIN_G, "extra": {}}]},
           "follow": {"Z9": None}}, open(cards.CFG["held_path"], "w"))
n = cards.load_held()
check(n == 1 and cards.follow("Z9") == "Z9" and rig.queues[8188]["queue_pending"][-1][1] == "Z9",
      "work held when the app quit goes back in line at the next launch")

# ------------------------------------------------------------------ §4 stop it
print("§4 STOP IT")
rig = fresh()
rig.add(500, "x/rig_dialogue.py --job j", {1: 20000})
rig.add(1, COMFY, {1: 300})
rig.queues[8188]["queue_running"] = [[1, "R", PLAIN_G, {}, []]]
s = cards.snapshot()
hs = {h["pid"]: h for h in s["cards"][1]["holders"]}
cards.stop([hs[500], hs[1]])
check(500 not in rig.procs and any(c[0] == "ssh" and c[1].startswith("kill 500") for c in rig.calls),
      "a voice process is killed by pid on the rig")
check(any(c[0] == "POST" and c[1].endswith(":8188/interrupt") for c in rig.calls),
      "a render is interrupted on ITS ComfyUI")

# ------------------------------------------------------------------ §5 readable errors
print("§5 errors people can read")
oom = ("Warning: flash-attn is not installed.\n/bin/sh: 1: sox: not found\n"
       "c10 CUDACachingAllocator.cpp:3934] memory allocation failed with OOM on device 0\n"
       "Traceback (most recent call last):\n  File \"rig_dialogue.py\", line 207\n"
       "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 24.00 MiB")
check(cards.explain(oom).startswith("the card ran out of memory"),
      "today's OOM traceback -> one sentence")
check(cards.explain("RuntimeError: ssh: Could not resolve hostname forge")
      == "couldn't reach forge (is the rig on and on the network?)", "rig unreachable")
check(cards.explain("Traceback\n  File x\nValueError: no shot 4 in project") == "no shot 4 in project",
      "otherwise: the last error line, without the traceback")
check(len(cards.explain("x" * 5000)) <= 160, "never a wall of text")

# ------------------------------------------------------------------ §6 the hooks
print("§6 every card-2 model load asks first")
SRC = {n: open(os.path.join(ROOT, n), encoding="utf-8").read()
       for n in ("forge_lab.py", "dialogue_ui.py", "episode_ui.py", "director.py",
                 os.path.join("audio", "client.py"), "Forge Lab.spec")}
AC = SRC[os.path.join("audio", "client.py")]
for fn in ("tts_design", "tts_clone", "align", "transcribe", "tts_batch", "transcribe_many"):
    body = AC.split("def %s(" % fn, 1)[1].split("\ndef ", 1)[0]
    check("_room(" in body, "audio.client.%s asks for room" % fn)
check('cards.preflight("writer"' in SRC["director.py"].split("def load(")[1][:900],
      "Director.load asks for room before warming the writer")
check('_cards.preflight("turbo", card=1, same_queue_port=F_PORT)' in SRC["forge_lab.py"],
      "a Turbo run asks for room on card 2")
check(SRC["forge_lab.py"].count("_cards_follow(pid)") >= 3,
      "run_state, collect and the orphan sweep follow held runs")
check("_cards.configure(" in SRC["forge_lab.py"] and "_cards.load_held" in SRC["forge_lab.py"],
      "the app turns the manager on and re-queues held work at launch")
check("'cards', 'cards_ui'" in SRC["Forge Lab.spec"], "both modules ship in the .app")
for n in ("dialogue_ui.py", "episode_ui.py"):
    check("cards_ui.handle(" in SRC[n] and "_cards.release" in SRC[n] and "explain(" in SRC[n],
          "%s: the busy prompt, release after every job, short errors" % n)

# behaviour: the audio client really raises CardBusy (and only when the app turned it on)
sys.path.insert(0, os.path.join(ROOT, "audio"))
import audio.client as ac  # noqa: E402
rig = fresh()
rig.add(1, COMFY, {1: 23000})
rig.queues[8188]["queue_running"] = [[5, "run-1", TURBO_G, {}, []]]
os.environ["FORGE_AUDIO_LOCAL"] = "0"
ac._DEPLOYED = True
try:
    ac.tts_design("a voice", "hello", "/tmp/x.wav")
    got = None
except cards.CardBusy as e:
    got = e
except Exception as e:
    got = e
check(isinstance(got, cards.CardBusy), "tts_design on a busy card 2 -> CardBusy before any ssh "
      "job (%r)" % (type(got).__name__,))
check(not [c for c in rig.calls if c[0] == "ssh" and "mkdir" in c[1]],
      "...and no rig job directory was even made")

# behaviour: run_state reports a HELD run as queued without asking forge
os.environ["FORGE_LAB_DATA"] = tempfile.mkdtemp()
import forge_lab as fl  # noqa: E402
cards.FOLLOW["HELD1"] = None
fl.get_json = lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked forge"))
check(fl.run_state("HELD1") == {"state": "queued", "position": 1, "held": True},
      "run_state: a held take is QUEUED (never error, never asks forge)")
check(fl.collect("HELD1") in (None, {"error": "no history yet"}), "collect waits for it too")

# ------------------------------------------------------------------ §7 auto-unload when idle
print("§7 AUTO-UNLOAD: idle models are let go after the setting")
rig = fresh()
td = tempfile.mkdtemp()
cards.CFG["held_path"] = os.path.join(td, "held.json")
check(cards.idle_minutes() == 10 and cards.keep_alive() == "10m",
      "default: 10 minutes, and the writer's keep_alive follows it")
rig.add(1, COMFY, {0: 9700, 1: 256})
cards.LAST_BUSY.clear()
del cards.REAPED[:]
T = 1000000.0
s = cards.snapshot()
check(cards.reap(s, now=T) == [] and cards.LAST_BUSY.get(8188) == T,
      "first sight STARTS the clock — never unload something not yet watched going idle")
check(cards.reap(cards.snapshot(), now=T + 9 * 60) == [], "9 idle minutes: still loaded")
frees = lambda: [c for c in rig.calls if c[0] == "POST" and c[1].endswith(":8188/free")]
check(cards.reap(cards.snapshot(), now=T + 10 * 60) == ["ComfyUI"] and len(frees()) == 1
      and cards.REAPED[-1]["who"] == "ComfyUI" and cards.REAPED[-1]["mb"] == 9956,
      "10 idle minutes: ComfyUI is told to /free, and it's recorded")
check(cards.reap(cards.snapshot(), now=T + 11 * 60) == [] and len(frees()) == 1,
      "...once — the clock restarts after an unload")
rig.queues[8188]["queue_running"] = [[1, "R", PLAIN_G, {}, []]]
cards._CACHE["snap"] = None
cards.reap(cards.snapshot(), now=T + 30 * 60)
rig.queues[8188]["queue_running"] = []
cards._CACHE["snap"] = None
check(cards.reap(cards.snapshot(), now=T + 35 * 60) == [] and cards.LAST_BUSY.get(8188) == T + 30 * 60,
      "work resets the clock (busy at 30 min -> not idle enough at 35)")
rig.queues[8188]["queue_pending"] = [[2, "P", PLAIN_G, {}, []]]
check(cards.reap(cards.snapshot(), now=T + 60 * 60) == [], "waiting work counts as busy")
rig.queues[8188]["queue_pending"] = []
cards.set_idle_minutes(None)
check(cards.reap(cards.snapshot(), now=T + 600 * 60) == [] and cards.keep_alive() == "24h",
      "'never': nothing is unloaded; the writer stays a day (not pinned forever)")
cards.set_idle_minutes(5)
check(json.load(open(os.path.join(td, "cards_settings.json")))["idle_min"] == 5
      and cards.idle_minutes() == 5, "the setting is saved")
rig.procs[1]["mem"] = {0: 600, 1: 256}
cards._CACHE["snap"] = None
check(cards.reap(cards.snapshot(), now=T + 900 * 60) == [], "under 1 GB held: left alone")
try:
    cards.set_idle_minutes(7)
    bad = False
except ValueError:
    bad = True
check(bad, "only the offered choices are accepted")
cards.CFG["enabled"] = False
check(cards.keep_alive() == "15m", "card manager off: the writer keeps the old 15m")
DSRC = open(os.path.join(ROOT, "director.py"), encoding="utf-8").read()
check('"keep_alive": _keep_alive()' in DSRC.split("def load(")[1][:1400],
      "Director.load asks cards for its keep_alive")

# ------------------------------------------------------------------ §8 BOTH cards: routing
print("§8 BOTH: each take goes to the card with less work")
rig = fresh()
td = tempfile.mkdtemp()
cards.CFG["held_path"] = os.path.join(td, "held.json")
rig.add(1, COMFY, {0: 9000, 1: 256})
rig.add(2, COMFY2, {1: 9000})
check(cards.route() == "one" and cards.pick_port("video") == 8188,
      "default ('one'): every take goes to card 1's ComfyUI")
cards.set_route("both")
check(json.load(open(os.path.join(td, "cards_settings.json")))["route"] == "both",
      "BOTH is saved")
check(cards.pick_port("video") == 8188, "both idle: card 1 (warm) wins the tie")
rig.queues[8188]["queue_running"] = [[1, "A", PLAIN_G, {}, []]]
cards._CACHE["snap"] = None
del rig.calls[:]
check(cards.pick_port("video") == 8189, "card 1 busy, card 2 idle: card 2")
check(not [c for c in rig.calls if c[0] == "POST" and c[1].endswith(":8189/free")],
      "...and card 2's OWN ComfyUI is not unloaded to make room for itself")
rig.add(501, "/usr/local/bin/ollama runner", {1: 14000})
rig.ollama = ["qwen3:32b"]
cards._CACHE["snap"] = None
check(cards.pick_port("video") == 8189 and not rig.ollama,
      "an idle writer on card 2 is unloaded so the take fits there")
rig.add(500, "x/rig_dialogue.py --job j", {1: 9000}, etime=20)
cards._CACHE["snap"] = None
check(cards.pick_port("video") == 8188,
      "card 2 busy with a voice job: the take goes to card 1's line instead")
rig.procs.pop(500)
rig.queues[8189]["queue_running"] = [[1, "B", PLAIN_G, {}, []]]
rig.queues[8188]["queue_pending"] = [[2, "C", PLAIN_G, {}, []]]
cards._CACHE["snap"] = None
check(cards.pick_port("video") == 8189, "card 1: 2 in line, card 2: 1 -> card 2")
rig.queues[8189]["queue_pending"] = [[2, "D", PLAIN_G, {}, []], [3, "E", PLAIN_G, {}, []]]
check(cards.pick_port("video") == 8188, "card 2 longer -> card 1")
s = cards.snapshot()
check(s["cards"][0]["queue"] == (1, 1) and s["cards"][1]["queue"] == (1, 2),
      "each card's line (making, waiting) is known for the panel")
# card 2's ComfyUI not running: start it, meanwhile everything goes to card 1
rig = fresh()
cards.CFG["held_path"] = os.path.join(td, "held.json")
del rig.queues[8189]
check(cards.route() == "both" and cards.pick_port("image") == 8188,
      "card 2's ComfyUI not up: the take goes to card 1...")
time.sleep(0.3)
check(any(c[0] == "ssh" and "--port 8189 --cuda-device 1" in c[1] for c in rig.calls),
      "...and card 2's ComfyUI is started (the same command Hermes used)")
rig.queues[8189] = {"queue_running": [], "queue_pending": []}
check(cards.stop_second() is True and any(c[0] == "ssh" and "pkill" in c[1]
                                          for c in rig.calls), "leaving BOTH stops it when idle")
rig.queues[8189]["queue_running"] = [[1, "X", PLAIN_G, {}, []]]
n_kill = len([c for c in rig.calls if c[0] == "ssh" and "pkill" in c[1]])
check(cards.stop_second() is False and len([c for c in rig.calls if c[0] == "ssh"
                                             and "pkill" in c[1]]) == n_kill,
      "...but never while it is making something")
cards.remember("T2", 8189)
INDEX["T3"] = {"id": "T3", "port": 8189}
check(cards.port_of("T2") == 8189 and cards.port_of("T3") == 8189 and cards.port_of("T1") == 8188,
      "every take remembers its ComfyUI (memory, then the index, else card 1)")
cards.set_route("one")
check(cards.pick_port("video") == 8188, "back to one card: card 1 only")

# forge_lab asks the RIGHT ComfyUI about a take on card 2
seen = []


def fake_get(path, timeout=15, base=None):
    seen.append((path, base))
    if path.startswith("/history/"):
        return {}
    return {"queue_running": [], "queue_pending": [["9", "T2"]]}


fl.get_json = fake_get
fl.post_json = lambda path, obj, timeout=60, base=None: seen.append(("POST " + path, base))
cards.FOLLOW.clear()
fl.run_state("T2")
check(("/history/T2", "http://forge:8189") in seen and ("/queue", "http://forge:8189") in seen,
      "run_state asks card 2's ComfyUI about a take sent there (%s)" % seen[:2])
del seen[:]
fl.run_state("OLD")
check(seen and all(b is None for _p, b in seen), "a take with no port asks card 1's (as before)")
del seen[:]
fl.cancel_queued(["T2"])
check(("POST /queue", "http://forge:8189") in seen, "cancel removes it from card 2's line")
fl.F_PORT = 8188
p_ = {"gpus": 2}
check(fl._route_for("video", p_) == 8188, "a Turbo (2-card) take always runs on the main one")
FLSRC = open(os.path.join(ROOT, "forge_lab.py"), encoding="utf-8").read()
check('blob = get_bytes("/view?" + q, base=_b)' in FLSRC and '"port": _port' in FLSRC
      and "cards.remember(pid, _port)" in FLSRC,
      "collect downloads from the take's ComfyUI; submit records where it went")

# a take in flight on CARD 2 is not an orphan (the sweep used to ask card 1's queue only)
fl.RUNS = tempfile.mkdtemp()
fl._index_insert({"id": "INFLIGHT2", "status": "running", "images": [], "port": 8189})
cards.remember("INFLIGHT2", 8189)
cards.configure(ssh="name@forge", host="forge", main_port=8188, ollama="http://forge:11434",
                index_lookup=lambda pid: None)


def fake_get2(path, timeout=15, base=None):
    if path == "/queue":
        return ({"queue_running": [[1, "INFLIGHT2"]], "queue_pending": []}
                if base == "http://forge:8189" else {"queue_running": [], "queue_pending": []})
    return {}


fl.get_json = fake_get2
fl.F_HOST = "forge"
try:
    fl.recover_orphans()
except Exception:
    pass
it = [i for i in fl.read_index() if i["id"] == "INFLIGHT2"][0]
check(it.get("status") == "running" and not it.get("orphan_checked"),
      "the orphan sweep checks card 2's line too: a take making there is left alone")

# the run index survives two writers at once (BOTH renders two Episode shots together)
import threading  # noqa: E402
fl.RUNS = tempfile.mkdtemp()
os.makedirs(fl.RUNS, exist_ok=True)
errs = []


def ins(k):
    try:
        for j in range(15):
            fl._index_insert({"id": "w%d-%d" % (k, j)})
            fl._index_update("w%d-%d" % (k, j), status="done")
    except Exception as e:
        errs.append(e)


ths = [threading.Thread(target=ins, args=(k,)) for k in range(6)]
shrank = []
stop_r = [False]


def reader():                  # the takes list reads WITHOUT the lock, like the window does
    last = 0
    while not stop_r[0]:
        n_ = len(fl.read_index())
        if n_ < last:
            shrank.append((last, n_))
        last = max(last, n_)


rt = threading.Thread(target=reader)
rt.start()
[t.start() for t in ths]
[t.join() for t in ths]
stop_r[0] = True
rt.join()
idx = fl.read_index()
check(not errs and len(idx) == 90 and all(i.get("status") == "done" for i in idx),
      "6 threads x 15 takes written at once: all 90 kept, all updated (%d)" % len(idx))
check(not shrank, "a reader never sees the index half-written (it shrank %d times: %s)"
      % (len(shrank), shrank[:3]))

print("\n%d checks, %d failed" % (N[0], len(FAILS)))
for f in FAILS:
    print("   FAILED: %s" % f)
sys.exit(1 if FAILS else 0)
