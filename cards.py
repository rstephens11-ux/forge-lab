"""THE CARD MANAGER (2026-10-17) — who is using each 3090 on forge, in plain words, and
making room before a job loads a model.

WHY: card 2 (GPU1) is shared by FOUR things and the app only knew about one of them —
  · the script writer (Ollama, ~19 GB, stays loaded for keep_alive)
  · voice jobs (Qwen3-TTS / Fish / the aligner — one-shot processes, a few GB each)
  · the second half of every Turbo (Raylight 2-card) render
  · anything else started on the rig (a second ComfyUI on :8189 held 21 GB for 4 days and
    every voice line died with a CUDA OOM traceback pasted over the Dialogue window).
So every job that loads a model on a card asks make_room() first:
  1. look (ONE ssh call: per-card memory, per-process memory, what each process is),
  2. free what is IDLE (a ComfyUI keeping an old model warm, the writer waiting) — silently,
  3. if something is WORKING, raise CardBusy with a plain sentence naming it ("card 2 is
     making a video on both cards (Turbo) — “Gustav sniffs the loaf” — started 2 min ago").
The window then offers WAIT / DO MINE FIRST / STOP IT (cards_ui.ask).

DO MINE FIRST cannot pause a render (ComfyUI has no pause), so it HOLDS it: the running
prompt (and anything queued behind it that also needs this card) is copied, removed, and
interrupted; your job runs; release() puts the held work back in line. While held, follow()
tells run_state() the take is still QUEUED, so the window waiting on it never sees "error".
Held work is saved to disk and re-queued on the next launch if the app quits first.

Tk-free and stdlib-only. Two seams for tests: _ssh_out(cmd) and _http(method, url, body).
Disabled (every call a no-op) until the app calls configure(); tools and the rig never do.
"""
import json
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- configuration
CFG = {"enabled": False, "ssh": "name@forge", "host": "forge", "main_port": 8188,
       "ollama": "http://forge:11434", "held_path": None, "index_lookup": None}
_LOCK = threading.RLock()

# What each kind of job needs free on its card, in MB. ⚠️ ESTIMATES with headroom, not
# measurements, except where noted: Qwen3-TTS had 2.44 GB in use when it OOMed (2026-10-17),
# the writer is ~19 GB (director.py), a Raylight Turbo render ~21 GB on card 2 (forge_lab).
NEED_MB = {"voice": 6000, "fish": 12000, "align": 5000, "writer": 20000, "turbo": 21500,
           # Drama 3 int4 weights are 20.8 GB and its engine will not share card 1 with
           # anything: ask for essentially the whole card.
           "drama3": 23000,
           # a take routed to card 2 in BOTH mode (estimates: H3 peaks near 20 GB on a 3090)
           "video": 20000, "image": 12000, "song": 12000}
PURPOSE = {"video": "your video", "image": "your image", "song": "your song",
           "voice": "your voice line", "fish": "your voice line (Fish)",
           "drama3": "your voice line (Drama 3, both cards)",
           "align": "lining up the words", "writer": "the script writer",
           "turbo": "your 2-card Turbo video"}
STUCK_AFTER_S = 15 * 60
CARD_NAME = {0: "Card 1", 1: "Card 2"}


def configure(**kw):
    """Turn the manager on (the app does this in launch(); nothing else should)."""
    CFG.update(kw)
    CFG["enabled"] = True


def enabled():
    return bool(CFG.get("enabled")) and os.environ.get("FORGE_LAB_NOCARDCHECK") != "1"


def card_name(i):
    return CARD_NAME.get(i, "Card %d" % (i + 1))


# ---------------------------------------------------------------- seams
def _ssh_out(cmd, timeout=12):
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", CFG["ssh"],
                        cmd], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 and not p.stdout:
        raise RuntimeError((p.stderr or "ssh failed").strip()[:200])
    return p.stdout


def _http(method, url, body=None, timeout=8):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def _comfy(port):
    return "http://%s:%d" % (CFG["host"], int(port))


# ---------------------------------------------------------------- reading the rig
SNAP_CMD = (
    "nvidia-smi --query-gpu=index,uuid,memory.used,memory.total,utilization.gpu "
    "--format=csv,noheader,nounits; echo @@; "
    "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits; "
    "echo @@; P=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | tr -d ' ' "
    "| sort -u | paste -sd, -); [ -n \"$P\" ] && ps -o pid=,etimes=,args= -p \"$P\"; true")


def _num(s, d=0):
    try:
        return int(float(str(s).strip()))
    except (TypeError, ValueError):
        return d


def ago(sec):
    """'40 s' / '3 min' / '2 h 5 min' / '4 days' — how people say durations."""
    sec = int(max(0, sec or 0))
    if sec < 60:
        return "%d s" % sec
    if sec < 3600:
        return "%d min" % (sec // 60)
    if sec < 86400:
        h, m = sec // 3600, (sec % 3600) // 60
        return ("%d h %d min" % (h, m)) if m else "%d h" % h
    d = sec // 86400
    return "%d day%s" % (d, "" if d == 1 else "s")


def _classify(args):
    """(kind, port, engine) from a process's command line."""
    a = args or ""
    if "main.py" in a and ("ComfyUI" in a or "--listen" in a or "comfy" in a.lower()):
        port = CFG["main_port"]
        parts = a.split()
        if "--port" in parts:
            try:
                port = int(parts[parts.index("--port") + 1])
            except (ValueError, IndexError):
                pass
        return "comfy", port, None
    if "ollama" in a:
        return "writer", None, None
    for script, eng in (("rig_drama3.py", "Drama 3"), ("rig_fish.py", "Fish"),
                        ("sgl-omni serve", "Drama 3 (server)"),
                        ("rig_dialogue.py", "Qwen3-TTS"),
                        ("rig_tts.py", "Qwen3-TTS"), ("rig_align.py", "align"),
                        ("rig_asr", "asr")):
        if script in a:
            return "voice", None, eng
    return "other", None, None


def parse_snapshot(out):
    """nvidia-smi + ps text -> {cards: [{index, used, total, util, holders: [...]}]}."""
    parts = (out or "").split("@@")
    while len(parts) < 3:
        parts.append("")
    cards, by_uuid = [], {}
    for line in parts[0].strip().splitlines():
        c = [x.strip() for x in line.split(",")]
        if len(c) >= 5 and c[0].isdigit():
            card = {"index": int(c[0]), "uuid": c[1], "used": _num(c[2]), "total": _num(c[3]),
                    "util": _num(c[4]), "holders": []}
            cards.append(card)
            by_uuid[c[1]] = card
    procs = {}
    for line in parts[2].strip().splitlines():
        bits = line.strip().split(None, 2)
        if len(bits) >= 2 and bits[0].isdigit():
            procs[int(bits[0])] = {"etime": _num(bits[1]), "args": bits[2] if len(bits) > 2
                                   else ""}
    for line in parts[1].strip().splitlines():
        c = [x.strip() for x in line.split(",")]
        if len(c) < 3 or not c[1].isdigit() or c[0] not in by_uuid:
            continue
        pid = int(c[1])
        pr = procs.get(pid, {"etime": 0, "args": ""})
        kind, port, eng = _classify(pr["args"])
        by_uuid[c[0]]["holders"].append({"pid": pid, "mb": _num(c[2]), "kind": kind,
                                         "port": port, "engine": eng, "since": pr["etime"],
                                         "args": pr["args"], "card": by_uuid[c[0]]["index"]})
    for card in cards:
        card["free"] = max(0, card["total"] - card["used"])
    return {"cards": cards, "t": time.time()}


def _queue(port):
    try:
        return _http("GET", _comfy(port) + "/queue")
    except Exception:
        return None


def _is_turbo(graph):
    try:
        return any(str((n or {}).get("class_type", "")).startswith("Ray")
                   for n in (graph or {}).values())
    except Exception:
        return False


def _short(text, n=46):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _lookup(prompt_id):
    fn = CFG.get("index_lookup")
    if not fn:
        return None
    try:
        return fn(prompt_id)
    except Exception:
        return None


def _what(meta):
    if not meta:
        return "something"
    if meta.get("song"):
        return "a song"
    if meta.get("video"):
        return "a video"
    return "an image"


def describe(snap):
    """Fill each holder's `who`, `doing`, `busy`, `freeable`, `stuck` in plain words.
    Adds the ComfyUI queue and Ollama models it learns from HTTP."""
    queues = {}
    writer_models = None
    for card in snap["cards"]:
        for h in card["holders"]:
            if h["kind"] == "comfy" and h["port"] not in queues:
                queues[h["port"]] = _queue(h["port"])
            if h["kind"] == "writer" and writer_models is None:
                try:
                    ps = _http("GET", CFG["ollama"].rstrip("/") + "/api/ps")
                    writer_models = [m.get("name") or m.get("model")
                                     for m in (ps.get("models") or [])]
                except Exception:
                    writer_models = []
    snap["queues"] = queues
    for card in snap["cards"]:
        for h in card["holders"]:
            h.update(busy=False, freeable=False, stuck=False, turbo=False, running=None)
            if h["kind"] == "comfy":
                main = h["port"] == CFG["main_port"]
                h["who"] = ("ComfyUI" if main else "ComfyUI (card 2)"
                            if h["port"] == SECOND_PORT else
                            "a second ComfyUI (port %d)" % h["port"])
                q = queues.get(h["port"])
                run = (q or {}).get("queue_running") or []
                pend = (q or {}).get("queue_pending") or []
                if run:
                    item = run[0]
                    pid_ = item[1] if len(item) > 1 else None
                    graph = item[2] if len(item) > 2 else {}
                    meta = _lookup(pid_)
                    h["turbo"] = _is_turbo(graph)
                    h["running"] = pid_
                    h["busy"] = True
                    if meta:
                        what = _what(meta)
                        if h["turbo"]:
                            what += " on both cards (Turbo)"
                        lab = _short(meta.get("prompt") or meta.get("label") or "")
                        t0 = meta.get("started")
                        h["doing"] = "making %s%s%s" % (
                            what, (" — “%s”" % lab) if lab else "",
                            (", started %s ago" % ago(time.time() - t0)) if t0 else "")
                    else:
                        h["doing"] = ("making something Forge Lab didn't start (another app "
                                      "or the overnight runner)")
                    if pend:
                        h["doing"] += "; %d more waiting behind it" % len(pend)
                elif q is None:
                    h["doing"] = "not answering, holding %.1f GB" % (h["mb"] / 1024.0)
                    h["busy"] = card["util"] > 5
                else:
                    h["doing"] = ("nothing right now, just keeping a model loaded from an "
                                  "earlier render")
                    h["freeable"] = True
            elif h["kind"] == "writer":
                h["who"] = "the script writer (Ollama)"
                names = ", ".join(n for n in (writer_models or []) if n)
                h["doing"] = ("loaded and waiting%s" % ((" (%s)" % names) if names else ""))
                h["freeable"] = True
            elif h["kind"] == "voice":
                h["who"] = "a voice job"
                h["doing"] = {"Fish": "making a voice line with Fish",
                              "Qwen3-TTS": "making a voice line with Qwen3-TTS",
                              "align": "lining up words to a voice line",
                              "asr": "listening back to check the words"}.get(h["engine"],
                                                                              "working on audio")
                h["doing"] += ", running %s" % ago(h["since"])
                h["busy"] = True
            else:
                h["who"] = "a program Forge Lab doesn't know"
                cmd = _short(os.path.basename((h["args"] or "?").split()[0]) + " "
                             + " ".join((h["args"] or "").split()[1:3]), 40)
                h["doing"] = "%s, running %s" % (cmd, ago(h["since"]))
                h["busy"] = card["util"] > 5
            # STUCK: holding real memory, the card idle, nothing of its own running, for long
            if (h["mb"] >= 1000 and card["util"] <= 2 and not h["running"]
                    and h["kind"] in ("voice", "other") and h["since"] >= STUCK_AFTER_S):
                h["stuck"] = True
                h["busy"] = False
                h["doing"] += " — looks stuck (idle the whole time)"
    # each card's LINE: the ComfyUI that lives on it (main -> card 1, :8189 -> card 2)
    for card in snap["cards"]:
        port = CFG["main_port"] if card["index"] == 0 else SECOND_PORT
        q = queues.get(port)
        card["queue"] = None if q is None else (len(q.get("queue_running") or []),
                                                len(q.get("queue_pending") or []))
    # one process on BOTH cards (a Turbo render): describe it once, point at it on the other
    seen = {}
    for card in snap["cards"]:
        for h in card["holders"]:
            if h["pid"] in seen and seen[h["pid"]] != card["index"]:
                h["same_as"] = seen[h["pid"]]         # the panel shortens it; prompts don't
            else:
                seen.setdefault(h["pid"], card["index"])
    return snap


_CACHE = {"snap": None}


def snapshot(max_age=0.0):
    """The described state of both cards, or None if the rig can't be read."""
    with _LOCK:
        s = _CACHE["snap"]
        if s and max_age and time.time() - s["t"] <= max_age:
            return s
    try:
        s = describe(parse_snapshot(_ssh_out(SNAP_CMD)))
    except Exception:
        return None
    with _LOCK:
        _CACHE["snap"] = s
    return s


def _card(snap, idx):
    for c in (snap or {}).get("cards") or []:
        if c["index"] == idx:
            return c
    return None


# ---------------------------------------------------------------- freeing idle holders
def free_holder(h):
    """Ask an IDLE holder to let go. ComfyUI keeps running; the writer just unloads."""
    try:
        if h["kind"] == "comfy":
            _http("POST", _comfy(h["port"]) + "/free",
                  {"unload_models": True, "free_memory": True})
            return True
        if h["kind"] == "writer":
            base = CFG["ollama"].rstrip("/")
            ps = _http("GET", base + "/api/ps")
            for m in ps.get("models") or []:
                _http("POST", base + "/api/generate",
                      {"model": m.get("name") or m.get("model"), "keep_alive": 0}, timeout=30)
            return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- CardBusy
class CardBusy(RuntimeError):
    """A card does not have room and what is on it is WORKING. str() is the plain sentence."""

    def __init__(self, job, card, need, free, holders):
        self.job, self.card, self.need, self.free = job, card, need, free
        self.holders = list(holders)
        super().__init__(self.sentence())

    def sentence(self):
        bits = ["%s is %s" % (h["who"][0].upper() + h["who"][1:], h["doing"])
                for h in self.holders] or ["Something is using it"]
        return ("%s is busy, so %s can't start yet. %s." % (
            card_name(self.card), PURPOSE.get(self.job, self.job), ". ".join(bits)))

    def thing(self):
        """What the busy work IS, for button labels ('the video', 'the voice job')."""
        h = self.holders[0] if self.holders else {}
        if h.get("kind") == "comfy":
            d = h.get("doing", "")
            for w in ("video", "song", "image"):
                if (" %s" % w) in d:
                    return "the %s" % w
            return "the render"
        if h.get("kind") == "voice":
            return "the voice job"
        return "that program"

    def options(self):
        """[(key, label)] — what the window may offer. DO MINE FIRST only where the work can
        be put back in line (a ComfyUI render); a one-shot process can only be waited for or
        stopped."""
        t = self.thing()
        opts = [("wait", "Wait for it")]
        if self.holders and all(h["kind"] == "comfy" and h.get("running")
                                for h in self.holders):
            opts.append(("first", "Do mine first (%s goes back in line)" % t))
        opts.append(("stop", "Stop %s" % t))
        opts.append(("cancel", "Cancel"))
        return opts


def make_room(job, card=1, need=None, same_queue_port=None, settle_s=10.0):
    """Make sure `card` has room for `job` (a NEED_MB key). Frees IDLE holders by itself.
    Returns {"ok", "freed": [who...], "note"}; raises CardBusy when a WORKING holder blocks.
    `same_queue_port`: a ComfyUI job on that instance just queues behind its own work, so
    that instance never counts as a blocker. Never raises anything else: if the rig can't
    be read, it lets the job try (the old behaviour) and says so."""
    if not enabled():
        return {"ok": True, "freed": [], "note": "card check off"}
    need = int(need or NEED_MB.get(job, 6000))
    snap = snapshot()
    c = _card(snap, card)
    if c is None:
        return {"ok": True, "freed": [], "note": "couldn't read the cards; trying anyway"}
    if c["free"] >= need:
        return {"ok": True, "freed": [], "note": ""}
    freed = []
    for h in c["holders"]:
        if (same_queue_port and h["kind"] == "comfy" and h["port"] == same_queue_port):
            continue          # never unload the very ComfyUI this job is about to use
        if h.get("freeable") and h["mb"] >= 300 and free_holder(h):
            freed.append(h["who"])
    if freed:
        t0 = time.time()
        while time.time() - t0 < settle_s:
            time.sleep(1.0)
            snap = snapshot()
            c = _card(snap, card) or c
            if c["free"] >= need:
                return {"ok": True, "freed": freed, "note": "freed %s" % ", ".join(freed)}
    cand = [h for h in c["holders"] if h["mb"] >= 500 and not h.get("freeable")
            and not (same_queue_port and h["kind"] == "comfy"
                     and h["port"] == same_queue_port)]
    # Name only what EXPLAINS the shortage: the biggest working holders first, until moving
    # them would make room. (A small program on a card that is busy because of a Turbo
    # render is not what is in the way, and naming it would also hide DO MINE FIRST.)
    cand.sort(key=lambda h: (not h.get("busy"), -h["mb"]))
    blockers, room = [], c["free"]
    for h in cand:
        if room >= need:
            break
        blockers.append(h)
        room += h["mb"]
    if not blockers:
        # nothing we can name is in the way (or only our own queue): let it try
        return {"ok": True, "freed": freed, "note": "only %.1f GB free; trying anyway"
                % (c["free"] / 1024.0)}
    raise CardBusy(job, card, need, c["free"], blockers)


def lane_port(card=0):
    """The ComfyUI port that LIVES ON card `card` (0-based): card 1 -> 8188, card 2 -> 8189."""
    return int(CFG["main_port"]) if int(card) == 0 else SECOND_PORT


def room_now(card=1, need=None, job="video", snap=None):
    """Is there room on `card` for `need` MB RIGHT NOW? Reads the rig; NEVER raises.

    This is the question the DISPATCHER asks before it lifts a job off the queue ("the queue
    answers what waits; preflight answers is there room this second"). It differs from
    make_room() deliberately:

      * it returns a dict instead of raising CardBusy, so the caller can try the other card
        or leave the job waiting — a refusal is a normal answer here, not an error;
      * it NEVER unloads the lane's own ComfyUI. A model already warm on that card is the
        fast path (ComfyUI evicts its own cache when a render needs the memory), so its
        memory counts as USABLE rather than as an obstacle;
      * it DOES free an idle tenant that would otherwise fight for the card — the writer
        (Ollama) on card 2, a stray idle process. That is make_room's "free what is IDLE"
        step without the settle loop;
      * a WORKING holder that is not this lane's own ComfyUI is a blocker (a Turbo render
        spilling onto card 2, a voice job, an unknown program mid-run).

    -> {"ok", "why": "ok"|"busy"|"full"|"unreadable", "usable": MB, "need": MB,
        "blockers": [holder...], "freed": [who...], "note": str}
    """
    need = int(need if need is not None else NEED_MB.get(job, 6000))
    out = {"ok": True, "why": "ok", "usable": 0, "need": need, "blockers": [], "freed": [],
           "note": ""}
    if not enabled():
        out["note"] = "card check off"
        return out
    port = lane_port(card)
    c = _card(snap if snap is not None else snapshot(), card)
    if c is None:
        out["note"] = "couldn't read the cards; trying anyway"
        out["why"] = "unreadable"
        return out
    usable = float(c.get("free") or 0)
    others = []
    for h in c["holders"]:
        mine = h.get("kind") == "comfy" and int(h.get("port") or 0) == int(port)
        if h.get("busy"):
            if not mine:
                out["blockers"].append(h)
        elif h.get("kind") == "comfy":
            usable += h["mb"]          # an idle ComfyUI (ours or the other lane's) evicts on demand
        elif h.get("freeable"):
            others.append(h)           # an idle tenant we may unload ourselves
        elif h["mb"] >= 500:
            out["blockers"].append(h)
    out["usable"] = round(usable, 1)
    for h in others:
        if h["mb"] >= 300 and free_holder(h):
            out["freed"].append(h["who"])
    if out["blockers"]:
        out["ok"] = False
        out["why"] = "busy"
        out["note"] = "%s is working" % CardBusy("job", card, need, c["free"],
                                                 out["blockers"]).thing()
        return out
    if usable < need:
        out["ok"] = False
        out["why"] = "full"
        out["note"] = "%.1f GB free of the %.1f GB needed" % (usable / 1024.0, need / 1024.0)
    return out


def wait_for_room(job, card=1, need=None, cancel=None, on_tick=None, poll=5.0,
                  timeout=3 * 3600):
    """WAIT FOR IT: keep checking until there is room (freeing idle holders as they appear).
    `cancel()` -> True stops waiting (returns False). on_tick(CardBusy) reports progress."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cancel and cancel():
            return False
        try:
            make_room(job, card, need)
            return True
        except CardBusy as cb:
            if on_tick:
                on_tick(cb)
        time.sleep(poll)
    return False


# ---------------------------------------------------------------- STOP IT
def stop(holders):
    """Stop the working holders. A ComfyUI render is interrupted (its take ends 'stopped');
    a process (voice job, unknown program) is terminated."""
    done = []
    for h in holders:
        try:
            if h["kind"] == "comfy":
                _http("POST", _comfy(h["port"]) + "/interrupt", {})
            else:
                _ssh_out("kill %d; sleep 3; kill -0 %d 2>/dev/null && kill -9 %d; true"
                         % (h["pid"], h["pid"], h["pid"]), timeout=20)
            done.append(h["who"])
        except Exception:
            pass
    return done


# ---------------------------------------------------------------- DO MINE FIRST
HELD = {}          # owner -> [{"port", "id", "graph", "extra"}...]   (in queue order)
FOLLOW = {}        # held/re-queued prompt id -> None (held) | new id


def _save_held():
    path = CFG.get("held_path")
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"held": HELD, "follow": FOLLOW}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def follow(pid):
    """For run_state()/collect(): the prompt id to look up now. Returns None while the run is
    HELD (report it as queued), a new id if it was re-queued under one, else `pid`."""
    with _LOCK:
        seen = 0
        while pid in FOLLOW and seen < 8:
            nxt = FOLLOW[pid]
            if nxt is None:
                return None
            if nxt == pid:
                break
            pid, seen = nxt, seen + 1
    return pid


def hold(holders, card, owner):
    """Take the running render(s) — and anything queued behind them that needs `card` — out
    of ComfyUI, remembering them so release(owner) can put them back. Returns how many."""
    n = 0
    for h in holders:
        if h["kind"] != "comfy":
            continue
        port = h["port"]
        q = _queue(port) or {}
        run = (q.get("queue_running") or [])[:1]
        pend = q.get("queue_pending") or []
        # a second ComfyUI pinned to this card: everything queued there needs it. The MAIN
        # one renders on card 1 too, so only its Turbo (Ray*) work needs card 2.
        keep = [it for it in pend if port != CFG["main_port"] or card != 1
                or _is_turbo(it[2] if len(it) > 2 else {})]
        items = run + sorted(keep, key=lambda it: it[0])
        if not items:
            continue
        rec = [{"port": port, "id": it[1], "graph": it[2] if len(it) > 2 else {},
                "extra": it[3] if len(it) > 3 else {}} for it in items]
        with _LOCK:
            for r in rec:
                FOLLOW[r["id"]] = None              # BEFORE the interrupt: never seen as error
            HELD.setdefault(owner, []).extend(rec)
            _save_held()
        if keep:
            try:
                _http("POST", _comfy(port) + "/queue", {"delete": [it[1] for it in keep]})
            except Exception:
                pass
        if run:
            try:
                _http("POST", _comfy(port) + "/interrupt", {})
            except Exception:
                pass
            t0 = time.time()
            while time.time() - t0 < 60:
                q2 = _queue(port) or {}
                if not any(len(it) > 1 and it[1] == run[0][1]
                           for it in q2.get("queue_running") or []):
                    break
                time.sleep(0.5)
            try:      # the interrupted record would otherwise read as an error to collect()
                _http("POST", _comfy(port) + "/history", {"delete": [run[0][1]]})
            except Exception:
                pass
        n += len(rec)
    return n


def release(owner=None):
    """Put held work back in ComfyUI's line, in its original order. owner=None releases all.
    Re-queues under the SAME prompt id when ComfyUI accepts it; otherwise FOLLOW maps the old
    id to the new one, so the take is still collected."""
    with _LOCK:
        keys = [owner] if owner is not None else list(HELD)
        recs = []
        for k in keys:
            recs.extend(HELD.pop(k, []))
    back = 0
    for r in recs:
        body = {"prompt": r["graph"], "extra_data": r.get("extra") or {},
                "prompt_id": r["id"]}
        cid = (r.get("extra") or {}).get("client_id")
        if cid:
            body["client_id"] = cid
        try:
            out = _http("POST", _comfy(r["port"]) + "/prompt", body, timeout=30)
            new = out.get("prompt_id") or r["id"]
            with _LOCK:
                if new == r["id"]:
                    FOLLOW.pop(r["id"], None)
                else:
                    FOLLOW[r["id"]] = new
            back += 1
        except Exception:
            with _LOCK:                       # could not re-queue: keep it held for next time
                HELD.setdefault("_retry", []).append(r)
    with _LOCK:
        _save_held()
    return back


def load_held():
    """At launch: anything held when the app last quit goes back in line."""
    path = CFG.get("held_path")
    if not path or not os.path.exists(path):
        return 0
    try:
        d = json.load(open(path))
    except Exception:
        return 0
    with _LOCK:
        for k, v in (d.get("held") or {}).items():
            HELD.setdefault(k, []).extend(v)
        FOLLOW.update(d.get("follow") or {})
    return release()


# ---------------------------------------------------------------- AUTO-UNLOAD when idle
# Models stay loaded while you work (the next take skips the load), and are let go after
# IDLE_CHOICES minutes of nothing. The clock runs in the APP (the CARDS panel's poll), so it
# only ticks while Forge Lab is open; Ollama's own keep_alive covers the writer either way.
IDLE_CHOICES = (5, 10, 20, 30, 60, None)          # None = never
IDLE_DEFAULT = 10
LAST_BUSY = {}          # ComfyUI port -> when it last had work (running or waiting)
REAPED = []             # [{"t", "who", "mb"}] newest last — what auto-unload did


def _settings_path():
    hp = CFG.get("held_path")
    return os.path.join(os.path.dirname(hp), "cards_settings.json") if hp else None


def idle_minutes():
    """The setting: minutes before an idle model is unloaded, or None for never."""
    p = _settings_path()
    try:
        v = json.load(open(p)).get("idle_min", IDLE_DEFAULT) if p and os.path.exists(p) \
            else IDLE_DEFAULT
    except Exception:
        v = IDLE_DEFAULT
    return v if v in IDLE_CHOICES else IDLE_DEFAULT


def set_idle_minutes(v):
    if v not in IDLE_CHOICES:
        raise ValueError("idle minutes must be one of %s" % (IDLE_CHOICES,))
    p = _settings_path()
    if p:
        d = {}
        try:
            d = json.load(open(p)) if os.path.exists(p) else {}
        except Exception:
            d = {}
        d["idle_min"] = v
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, p)
    return v


def keep_alive():
    """What the writer (Ollama) is told when it loads: the same idle setting. 'Never' keeps
    it for a day (Ollama's -1 would pin it forever, past the app closing)."""
    m = idle_minutes() if enabled() else 15
    return "24h" if m is None else "%dm" % m


def reap(snap, now=None):
    """AUTO-UNLOAD: free each ComfyUI that has had no work for the idle setting and is still
    holding >= 1 GB. Called after every panel poll (on its thread). Returns what it freed."""
    now = time.time() if now is None else now
    mins = idle_minutes()
    freed = []
    if not snap or not enabled():
        return freed
    by_port = {}
    for card in snap["cards"]:
        for h in card["holders"]:
            if h["kind"] == "comfy":
                b = by_port.setdefault(h["port"], {"mb": 0, "h": h, "work": False})
                b["mb"] += h["mb"]
                q = (snap.get("queues") or {}).get(h["port"]) or {}
                if h.get("running") or q.get("queue_pending"):
                    b["work"] = True
    for port, b in by_port.items():
        if b["work"] or port not in LAST_BUSY:
            LAST_BUSY[port] = now                 # first sight starts the clock: never reap
            continue                              # something we have not watched go idle
        if mins is None or b["mb"] < 1024:
            continue
        if now - LAST_BUSY[port] >= mins * 60 and b["h"].get("freeable"):
            if free_holder(b["h"]):
                freed.append(b["h"]["who"])
                REAPED.append({"t": now, "who": b["h"]["who"], "mb": b["mb"]})
                del REAPED[:-20]
                LAST_BUSY[port] = now
    return freed


def idle_for(port, now=None):
    """Seconds this ComfyUI has been idle, as far as the app has watched (0 if busy/unseen)."""
    t = LAST_BUSY.get(port)
    return 0 if t is None else max(0, (time.time() if now is None else now) - t)


# ---------------------------------------------------------------- BOTH CARDS: routing
# Card mode BOTH: two takes at once, one per card. Card 1 is the main ComfyUI (:8188); card 2
# gets a SECOND ComfyUI pinned to it (:8189), started by the app with the same command Hermes
# used on the rig (it ran there for days). Each new take goes to whichever has less work —
# ties to card 1 (it is usually warm) — and to card 2 only when card 2 has room (an idle
# writer is freed; a busy voice job sends the take to card 1 instead). Every take remembers
# its ComfyUI (PORT_OF + the index's "port"), so its status/history/download ask THAT one.
SECOND_PORT = 8189
SECOND_START = (
    "mkdir -p ~/comfy-gpu1/temp ~/comfy-gpu1/user && cd ~/ComfyUI && nohup .venv/bin/python "
    "main.py --listen 0.0.0.0 --port 8189 --cuda-device 1 --temp-directory "
    "/home/ryan/comfy-gpu1/temp --database-url sqlite:////home/ryan/comfy-gpu1/comfyui.db "
    "> ~/comfy-gpu1/server.log 2>&1 & echo started $!")
SECOND_STOP = "pkill -f 'main.py --listen 0.0.0.0 --port 8189' ; true"
PORT_OF = {}            # prompt id -> ComfyUI port it was sent to
INDEX_LOCK = threading.RLock()   # forge_lab's run-index writers (both module copies) share it
_SECOND = {"starting": False}


def _setting(key, default):
    p = _settings_path()
    try:
        return json.load(open(p)).get(key, default) if p and os.path.exists(p) else default
    except Exception:
        return default


def _set_setting(key, value):
    p = _settings_path()
    if not p:
        CFG["_" + key] = value
        return
    d = {}
    try:
        d = json.load(open(p)) if os.path.exists(p) else {}
    except Exception:
        d = {}
    d[key] = value
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def route():
    """'both' (two takes at once) or 'one' (everything on card 1's ComfyUI)."""
    v = CFG.get("_route") if _settings_path() is None else _setting("route", "one")
    return v if v in ("one", "both") else "one"


def set_route(v):
    if v not in ("one", "both"):
        raise ValueError("route is 'one' or 'both'")
    _set_setting("route", v)
    return v


def second_up():
    try:
        _http("GET", _comfy(SECOND_PORT) + "/system_stats", timeout=3)
        return True
    except Exception:
        return False


def ensure_second(wait_s=150, poll=3.0):
    """Start the card-2 ComfyUI if it is not answering; wait until it does. True when up."""
    if second_up():
        return True
    with _LOCK:
        if _SECOND["starting"]:
            return False
        _SECOND["starting"] = True
    try:
        _ssh_out(SECOND_START, timeout=20)
        t0 = time.time()
        while time.time() - t0 < wait_s:
            time.sleep(poll)
            if second_up():
                return True
        return False
    finally:
        _SECOND["starting"] = False


def stop_second():
    """Leaving BOTH: stop the card-2 ComfyUI — only when nothing is running or waiting there."""
    q = _queue(SECOND_PORT)
    if q is None:
        return True
    if q.get("queue_running") or q.get("queue_pending"):
        return False
    _ssh_out(SECOND_STOP, timeout=20)
    return True


def remember(pid, port):
    with _LOCK:
        PORT_OF[pid] = int(port)


def port_of(pid):
    """The ComfyUI port a take was sent to (main if unknown)."""
    with _LOCK:
        if pid in PORT_OF:
            return PORT_OF[pid]
    meta = _lookup(pid)
    if meta and meta.get("port"):
        return int(meta["port"])
    return CFG["main_port"]


def _load(port):
    q = _queue(port)
    if q is None:
        return None
    return len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])


def pick_port(kind="video"):
    """Where a new single-card take goes. 'one' -> card 1 always. 'both' -> the ComfyUI with
    less work (ties -> card 1), card 2 only if it has room. Never raises."""
    main = CFG["main_port"]
    if not enabled() or route() != "both":
        return main
    try:
        l2 = _load(SECOND_PORT)
        if l2 is None:                         # not running yet: start it for the next take
            threading.Thread(target=ensure_second, daemon=True).start()
            return main
        l1 = _load(main) or 0
        if l1 <= l2:
            return main
        try:
            make_room(kind if kind in NEED_MB else "video", card=1,
                      same_queue_port=SECOND_PORT)
        except CardBusy:
            return main
        return SECOND_PORT
    except Exception:
        return main


def queue_summary():
    """[(card name, running, waiting)] for the panel — None for a ComfyUI that is not up."""
    out = []
    for name, port in (("Card 1", CFG["main_port"]), ("Card 2", SECOND_PORT)):
        q = _queue(port)
        if q is None:
            out.append((name, None, None))
        else:
            out.append((name, len(q.get("queue_running") or []),
                        len(q.get("queue_pending") or [])))
    return out


# ---------------------------------------------------------------- errors people can read
def explain(text):
    """A rig traceback -> one sentence. Card-busy messages pass through unchanged."""
    t = str(text or "")
    low = t.lower()
    if ("out of memory" in low or "outofmemoryerror" in low or "oom" in low.split()
            or "with oom" in low or "cudacachingallocator" in low):
        return ("the card ran out of memory: something else was using it. Open the Cards "
                "panel in the main window to see what, or try again.")
    if "could not resolve hostname" in low or "no route to host" in low \
            or "connection refused" in low or "timed out" in low:
        return "couldn't reach forge (is the rig on and on the network?)"
    if " is busy, so " in t:
        return t
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ("Error" in ln or "Exception" in ln) and ":" in ln and not ln.startswith("File "):
            return _short(ln.split("Error:", 1)[-1].strip() if "Error:" in ln else ln, 160)
    return _short(lines[-1] if lines else t, 160)


# ---------------------------------------------------------------- the one call jobs make
def preflight(job, card=1, need=None, same_queue_port=None):
    """make_room() for code that cannot ask the person (audio client, the Director): frees
    idle holders, raises CardBusy with a readable sentence when something is working."""
    return make_room(job, card, need, same_queue_port=same_queue_port)
