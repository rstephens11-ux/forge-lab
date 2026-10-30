#!/usr/bin/env python3
"""dispatch — the SCHEDULER that lifts jobs off the rig queue and puts them on a ComfyUI lane.

WHY THIS EXISTS
    Everything in Forge Lab that needs a GPU is meant to arrive here: you click Make it move
    while a video is already rendering, and instead of being refused ("card 2 is busy") your
    job waits in the queue you can see on the Home tab — reorderable, pausable, deletable —
    and starts by itself when a card frees. Two one-card jobs run at the SAME TIME, one per
    card; a 2-card Turbo waits until both are free.

WHAT THIS MODULE OWNS (and what it does not)
    It owns: the ~3 s loop, the question "which lane has room THIS SECOND", claiming a card,
    choosing the lane, running the job's callable on a worker thread, and reporting back to
    jobqueue with mark_running()/finish().
    It does NOT own the order, the pausing or the storage — that is jobqueue.py — and it does
    not know how to render anything: a RUNNER does that, registered by kind, so images, video,
    songs or ssh rig jobs can be added later without touching the scheduler (see RUNNERS).

THE TWO LANES
    | lane | port | card | for |
    | A | 8188 | card 1 (+ card 2 when a Turbo does) | the main ComfyUI |
    | B | 8189 | card 2 only | a second job in parallel |
    Lane B is only offered when the CARD MODE setting is "both" (cards.route()). In "one" mode
    everything goes to card 1 on purpose and this module must not quietly overrule that.

⚠️ Tk-FREE on purpose (the app's window imports this, and so can a test): no tkinter here,
    ever. The queue is the process-wide singleton from jobqueue.app_queue(), so the Home tab's
    panel, this scheduler and the window that submitted a job all see the same entries.

⚠️ A lane is "free" only when its own /prompt says nothing is running or waiting on it. That
    is the honest reading — a render started from the Make tab, by Quick, or by another program
    is not in this queue but IS on the card, and the queue must not be blind to it.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import cards
import jobqueue as jq

LANE_NAME = {}                       # port -> "comfy_a" / "comfy_b" (filled below)
TICK_S = 3.0                         # how often the loop looks at the rig (a kick is instant)
RETRY_S = 20.0                       # a job whose card went busy under it waits this long
LOG_KEEP = 60

# ------------------------------------------------------------------ seams
# Tests and in-app drivers replace these two instead of touching the rig:
PROBE = {"fn": None}                 # fn(port) -> queue_remaining on that lane, or None
SPAWN = {"fn": None}                 # fn(callable) -> run it (a test runs it inline)
ROOM = {"fn": None}                  # fn(card, need, snap=None) -> cards.room_now()'s dict

LOG = []
LIVE = {}                            # jid -> {"msg", "t0", "lane", "card", "port"} while running
RESULTS = {}                         # jid -> whatever the runner returned ({"file", ...})
ERRORS = {}                          # jid -> the sentence for a failed job


def log(msg):
    LOG.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
    del LOG[:-LOG_KEEP]
    return msg


# ------------------------------------------------------------------ the rig, read-only
def queue():
    """The ONE queue for this process (jobqueue.app_queue)."""
    return jq.app_queue()


def host():
    return cards.CFG.get("host") or "forge"


def remaining(port, probe=None):
    """How many prompts this lane has running or waiting — None when it is NOT answering.

    /prompt is the tiny endpoint: {"exec_info": {"queue_remaining": n}}. /queue would work
    too but it ships every prompt's whole graph, and this runs every few seconds.
    """
    fn = probe or PROBE.get("fn")
    if fn is not None:
        try:
            return fn(int(port))
        except Exception:
            return None
    try:
        url = "http://%s:%d/prompt" % (host(), int(port))
        with urllib.request.urlopen(url, timeout=4) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        return int((body.get("exec_info") or {}).get("queue_remaining") or 0)
    except Exception:
        return None


def lane_ports():
    """[(card, port)] of the lanes that EXIST: always card 1's, plus card 2's in BOTH mode."""
    out = [(0, int(cards.CFG.get("main_port") or 8188))]
    try:
        if cards.route() == "both":
            out.append((1, int(cards.SECOND_PORT)))
    except Exception:
        pass
    return out


def capacity(probe=None):
    """{card: port} — the lanes with NOTHING running or waiting on them right now.

    This is the capacity jobqueue.next_job() is handed. What it does not say is whether the
    CARD itself has room: that is cards.room_now(), asked per job just before it starts,
    because it needs the job's size and it can free an idle tenant to make that room.
    """
    out = {}
    for card, port in lane_ports():
        rem = remaining(port, probe)
        if rem is None or rem > 0:
            continue
        out[card] = port
    return out


def lanes(probe=None, snap=None):
    """What each lane is doing, in plain words — for a panel, a driver or a log line."""
    out = []
    for card, port in lane_ports():
        rem = remaining(port, probe)
        room = cards.room_now(card=card, need=None, snap=snap) if rem is not None else None
        out.append({"card": card, "port": port, "name": LANE_NAME.get(port, "comfy %s" % port),
                    "running": rem,
                    "up": rem is not None,
                    "room": (room or {}).get("ok"),
                    "why": (room or {}).get("why"),
                    "note": (room or {}).get("note"),
                    "free_gb": round(((room or {}).get("usable") or 0) / 1024.0, 1)})
    return out


def safe_snapshot(max_age=2.0):
    """cards.snapshot() that never raises or blocks the loop for long."""
    try:
        return cards.snapshot(max_age=max_age)
    except Exception:
        return None


# ------------------------------------------------------------------ the runners
RUNNERS = {}


def register(kind, fn):
    """Teach the scheduler how to run a kind of job.

    fn(payload, report, lane_port) -> a result dict the window can show ({"file", ...}).
    `report(msg)` says what it is doing (it shows up in the queue row and the window's status
    line); `lane_port` is the ComfyUI the job was given, so the runner can pin its submit to
    that exact instance instead of letting the app pick again and double-book the other card.
    """
    RUNNERS[kind] = fn
    return fn


def _render_runner(payload, report, lane_port=None):
    """The queue's runner for anything that goes through forge_lab.submit(): submit, wait for
    the render, collect the take. Used by every window that is NOT the Make tab (which watches
    its own run and reports through a Gate instead)."""
    import forge_core as c
    p = dict(payload.get("params") or {})
    if lane_port:
        p["port"] = int(lane_port)          # the lane the dispatcher checked and claimed
    pid, seed = c.submit(payload.get("mode"), p, payload.get("refs") or [])
    meta = c.wait_and_collect(pid, timeout=int(payload.get("timeout") or 1800),
                              progress=report)
    return {"pid": pid, "seed": seed, "meta": meta}


register("render", _render_runner)


def render(title, mode, params, refs=None, est_s=0.0, need=None, timeout=1800, poll=2.0):
    """Do ONE render through the queue and return the collected take — the shape every engine
    module wants (quick.py, studio_pipeline.py, places.py, charpack.py).

    With no scheduler running (a test, a headless script, a driver run) it submits straight
    through exactly as before, so a caller never has to care which mode it is in. This BLOCKS
    the calling thread while the job waits for a card — that is the point: the wait is visible
    on the Home tab and the entry can be reordered, paused or deleted there. Deleting it makes
    this raise.
    """
    if not running():
        import forge_core as c
        pid, _seed = c.submit(mode, params, refs)
        return c.wait_and_collect(pid, timeout=int(timeout or 1800))
    n = need_for(mode, params)
    if need:
        n = dict(n, need=need)
    e = enqueue(title, {"mode": mode, "params": params, "refs": list(refs or []),
                        "timeout": int(timeout or 1800)},
                need=n["need"], one_of=n["one_of"], cards=n["cards"], est_s=est_s,
                kind="render")
    jid = e["id"]
    while True:
        ent = queue().get(jid) or {}
        state = ent.get("state")
        if state == "done":
            return (result(jid) or {}).get("meta") or {}
        if state in ("failed", "cancelled"):
            raise RuntimeError(error(jid) or "the queue dropped that job")
        if state is None:
            raise RuntimeError("that job is no longer in the queue")
        time.sleep(poll)


def _apps_runner(payload, report, lane_port=None):
    """The APPS jobs (talking photo, make it move, outfit swap, …) — apps.run() IS already
    submit-and-wait, so the dispatcher only has to call it with its lane pinned."""
    import apps
    try:
        return apps.run(payload.get("key"), payload.get("args") or {}, progress=report,
                        quality=payload.get("quality") or "Draft", port=lane_port)
    finally:
        try:
            # anything this window HELD for a "Do mine first" goes back in ComfyUI's line
            cards.release("apps")
        except Exception:
            pass


register("apps", _apps_runner)


# ------------------------------------------------------------------ entries
def need_kind(e):
    return (e.get("payload") or {}).get("need") or "video"


def need_mb(e):
    """How much card memory this entry wants — None when it needs no card at all."""
    k = need_kind(e)
    if k in (None, "none", "local"):
        return None
    return int(cards.NEED_MB.get(k, cards.NEED_MB["video"]))


def cards_to_check(e, card):
    """The cards that must all have room for this entry to start on `card`."""
    cs = [int(c) for c in (e.get("cards") or [])]
    return cs or [int(card)]


def candidate_lanes(e, cap):
    """The lanes this entry may use, best first.

    one_of -> either lane, card 1 first (it is usually the warm one). cards=[0, 1] -> the MAIN
    lane only, because a Turbo render spans both cards and only lane A sees both.
    """
    one = [int(c) for c in (e.get("one_of") or [])]
    if one:
        return [(c, cards.lane_port(c)) for c in sorted(one) if c in cap]
    cs = [int(c) for c in (e.get("cards") or [])]
    if cs and set(cs) <= set(cap):
        return [(cs[0], cards.lane_port(cs[0]))]
    return []


def enqueue(title, payload, need="video", one_of=(0, 1), cards=None, est_s=0.0, kind="apps"):
    """Put a job on the queue (never refused) and wake the scheduler. Returns the entry.

    `need` picks the room the job wants ("video"/"image"/"song"/"turbo"/…, or "local" for a job
    that runs on this Mac and needs no card). `one_of` is the one-card-any-card shape; pass
    cards=[0, 1] instead for a job that needs BOTH.
    """
    local = need in ("local", "none", None)
    e = jq.new_entry(title, kind, [] if local else (cards or []),
                     est_s=est_s, payload=dict(payload or {}, need=need),
                     one_of=None if local else one_of)
    queue().add(e)
    log("queued %s (%s, %s)" % (title, need, e["id"]))
    kick()
    return e


# ------------------------------------------------------------------ the dispatcher
class Dispatcher:
    """The loop. One instance for the process (see start()); a test builds its own."""

    def __init__(self, q=None, tick_s=TICK_S, spawn=None, probe=None, room=None):
        self.queue = q or None
        self.tick_s = float(tick_s)
        self.spawn = spawn
        self.probe = probe
        self.room = room
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self._cooldown = {}                 # jid -> do not retry before this time
        self.started_count = 0

    # ---------------------------------------------------------------- the loop itself
    def start(self):
        if self.running():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="forge-dispatch", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=2.0):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None

    def running(self):
        t = self._thread
        return bool(t is not None and t.is_alive())

    def kick(self):
        """Wake the loop NOW instead of waiting out the tick (a finish, or a new job)."""
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:                      # noqa: BLE001 — the loop must survive
                log("tick failed: %r" % (exc,))
            self._wake.wait(self.tick_s)
            self._wake.clear()

    def _q(self):
        return self.queue or queue()

    def _snap(self, box):
        if not box[1]:
            box[0] = safe_snapshot()
            box[1] = True
        return box[0]

    def tick(self, now=None):
        """One pass: start every job that can start. Returns the ids started.

        A refusal is normal here, not an error: the job keeps its place and the loop looks
        again in a few seconds. `skip` stops one blocked job from hiding the ones behind it.
        """
        now = time.time() if now is None else now
        q = self._q()
        self.sweep_orphan_gates(q)
        if not any(e.get("state") == jq.STATE_QUEUED for e in q.live()):
            return []                                    # nothing to do: no rig calls at all
        started = []
        with self._lock:
            expired = [jid for jid, until in self._cooldown.items() if until <= now]
            for jid in expired:
                self._cooldown.pop(jid, None)
            refused = set(self._cooldown)
            box = [None, False]
            passes = 0
            while True:
                # A safety cap: every pass either STARTS a job or refuses one, so it cannot
                # loop for ever — unless a rule above is broken, and a hung scheduler thread
                # is far worse than a tick that gives up. (Seen: ignoring `skip` did exactly
                # that, and the whole suite hung instead of failing.)
                passes += 1
                if passes > len(q.entries()) + 2:
                    log("tick gave up after %d passes" % passes)
                    break
                cap = self.capacity()
                # ⚠️ Subtract what THIS queue is already running: capacity() is the rig's own
                # answer and knows nothing about our claims, so without this the very next
                # job in the same pass would be handed the card we just took.
                cap = {c: p for c, p in cap.items() if c in (set(cap) - q.cards_busy())}
                e = q.next_job(set(cap), skip=refused)
                if e is None:
                    break
                if self._start_one(e, cap, box, now):
                    started.append(e["id"])
                else:
                    refused.add(e["id"])
        return started

    def capacity(self):
        return capacity(probe=self.probe)

    def sweep_orphan_gates(self, q=None):
        """Cancel gate entries NOTHING is waiting on — a window that closed, or the last app
        run: a gate job can only be done by the thread that asked for it, so starting one would
        hold a card for ever. Runs before capacity is even looked at, so a pair of ghost gates
        can never keep both cards out of use."""
        n = 0
        for e in (q or self._q()).entries():
            if (e.get("state") in (jq.STATE_QUEUED, jq.STATE_PAUSED)
                    and (e.get("payload") or {}).get("gate") and e["id"] not in GATES):
                if (q or self._q()).cancel(e["id"],
                                           note="the window that asked for this is gone"):
                    n += 1
                    log("dropped a gate job nothing is waiting on (%s)" % e["id"])
        return n

    def _start_one(self, e, cap, box, now):
        """Try to start `e`. True when it started (or is already gone)."""
        need = need_mb(e)
        if need is None:                                  # runs on this Mac: no card at all
            return self._start(e, None, None, now)
        for card, port in candidate_lanes(e, cap):
            for c in cards_to_check(e, card):
                room = self._room(c, need, self._snap(box))
                if not room["ok"]:
                    log("%s waits: %s %s" % (e.get("title"), cards.card_name(c), room["note"]))
                    break
            else:
                return self._start(e, card, port, now)
        return False

    def _room(self, card, need, snap=None):
        """Is there room on `card`? cards.room_now() unless a test replaced it.

        ⚠️ cards.room_now's THIRD parameter is `job`, not `snap` — never call it positionally.
        """
        fn = self.room or ROOM.get("fn")
        if fn is None:
            return cards.room_now(card=card, need=need, snap=snap)
        try:
            return fn(card, need, snap)
        except TypeError:
            return fn(card, need)

    # ---------------------------------------------------------------- starting a job
    def _spawn(self, fn):
        sp = self.spawn or SPAWN.get("fn")
        if sp is not None:
            return sp(fn)
        t = threading.Thread(target=fn, daemon=True, name="forge-job")
        t.start()
        return t

    def _start(self, e, card, port, now):
        jid = e["id"]
        q = self._q()
        is_gate = bool((e.get("payload") or {}).get("gate"))
        gate = GATES.get(jid) if is_gate else None
        if is_gate and gate is None:
            # A gate entry with nothing waiting on it (the app restarted, or the window that
            # asked closed) must never start: a card held by a job nothing will ever finish is
            # the worst state the rig can be in.
            q.cancel(jid, note="the window that asked for this is gone")
            log("dropped a gate job nothing is waiting on (%s)" % jid)
            return True
        if card is None:
            lane = "this Mac"
        else:
            if e.get("one_of"):
                q.claim_card(jid, int(card))      # "any card" -> the card it really took
            # ⚠️ NOT for a cards=[0, 1] job: that footprint is the whole point of Turbo, and
            # shrinking it to [0] would tell the next tick that card 2 is free.
            lane = LANE_NAME.get(int(port), "comfy %s" % port)
        # ⚠️ The pin goes on the GATE **before** the entry is marked running. wait() returns the
        # moment it sees STATE_RUNNING, so a window could otherwise wake in the gap, read
        # gate.port == None and submit UNPINNED — onto whichever lane the app's own balancing
        # fancied, while the queue believed this job's card was claimed (seen: TESTSONG2, and
        # the Make tab's real-rig pin check was too loose to notice).
        if gate is not None:
            gate.port = None if port is None else int(port)
        if q.mark_running(jid, lane) is None:
            return True                           # someone else moved it on: nothing to do
        LIVE[jid] = {"msg": "", "t0": now or time.time(), "lane": lane, "card": card,
                     "port": port}
        RESULTS.pop(jid, None)
        ERRORS.pop(jid, None)
        self.started_count += 1
        log("start %s on %s (%s)" % (e.get("title"), lane, jid))
        if gate is not None:
            gate.release(port=port)               # the window's own thread does the work
            return True
        self._spawn(lambda: self._work(jid, e, port))
        return True

    def _work(self, jid, e, port):
        q = self._q()
        payload = dict(e.get("payload") or {})
        try:
            fn = RUNNERS.get(e.get("kind"))
            if fn is None:
                raise RuntimeError("nothing is registered to run %r jobs" % (e.get("kind"),))
            out = fn(payload, _reporter(jid), port)
            RESULTS[jid] = out
            note = ""
            if isinstance(out, dict) and out.get("file"):
                note = os.path.basename(str(out["file"]))[:120]
            q.finish(jid, ok=True, note=note)
            log("done %s (%s)" % (e.get("title"), jid))
        except Exception as exc:                          # noqa: BLE001 — one job, not the app
            ERRORS[jid] = str(exc) or exc.__class__.__name__
            RESULTS.pop(jid, None)
            q.finish(jid, ok=False, error=ERRORS[jid])
            if _is_card_busy(exc):
                # a card went busy under us (someone else's render, a voice job): wait, and
                # do NOT count it as a failure of the job
                if q.requeue(jid, note="was busy — waiting for a card"):
                    with self._lock:
                        self._cooldown[jid] = time.time() + RETRY_S
                    log("requeued %s (card busy)" % (e.get("title"),))
            else:
                log("FAILED %s: %s" % (e.get("title"), ERRORS[jid][:200]))
        finally:
            LIVE.pop(jid, None)
            self._wake.set()                              # look for the next job at once


def _is_card_busy(exc):
    try:
        return isinstance(exc, cards.CardBusy) or type(exc).__name__ == "CardBusy"
    except Exception:
        return False


def _reporter(jid):
    def report(msg):
        try:
            if jid in LIVE:
                LIVE[jid]["msg"] = "%s" % (msg or "")[:160]
        except Exception:
            pass
    return report


# ------------------------------------------------------------------ gate entries
# A job whose WORKER ALREADY EXISTS: every window in the app has its own render thread with its
# own watch/collect/progress code, and rewriting six of those to suit the queue is how you break
# six windows. So the queue lends a job a TURN and a CARD instead: the window asks for one
# (admit), its thread blocks in wait() while the entry sits in the queue like any other — visible,
# reorderable, pausable, deletable — and when the dispatcher hands it a card the thread wakes and
# does exactly what it did before. done()/fail() report the outcome when the take is finished.
#
# ⚠️ Mark done() when the RENDER finishes, not when submit() returns: the entry holds the card
# until then, which is what stops the next job landing on the same card.
GATES = {}          # jid -> Gate  (in-process only: a gate entry cannot survive a restart)


class Gate:
    """The handle a window's thread holds while it waits for, and then uses, a card."""

    def __init__(self, jid, q):
        self.id = jid
        self.port = None                 # the lane the dispatcher gave it (pin your submit)
        self.cancelled = False
        self.running = False
        self._q = q
        self._go = threading.Event()

    def wait(self, report=None, poll=0.5, timeout=6 * 3600):
        """Block until this job may start. True when it may, False if it was deleted/cancelled.

        `report(msg)` is called while it waits, so the window can say "waiting for a card —
        #2 in the queue" in its own status line instead of looking frozen.

        ⚠️ The entry's state is checked BEFORE each wait, and RUNNING counts as "you are up":
        the dispatcher marks the entry running and then releases the gate, so a waiter that
        woke up in between must not read "running" as "someone else took it".
        """
        t0 = time.time()
        while True:
            e = self._q.get(self.id)
            if e is None or e.get("state") in (jq.STATE_CANCELLED, jq.STATE_FAILED,
                                               jq.STATE_DONE):
                self.cancelled = True
                return False
            if self.running or e.get("state") == jq.STATE_RUNNING:
                return True
            if self._go.wait(poll):
                return True
            if report:
                try:
                    if e.get("state") == jq.STATE_PAUSED:
                        report("paused — it keeps its place in the queue")
                    else:
                        ahead = self._q.queued_ahead(self.id)
                        report("waiting for a card%s"
                               % (" — #%d in the queue" % (ahead + 1) if ahead > 0
                                  else " — next up"))
                except Exception:
                    pass
            if time.time() - t0 > timeout:
                self.cancelled = True
                return False

    def release(self, port=None, lane=None):
        """The dispatcher's call: a card is free and this job has it."""
        self.port = None if port is None else int(port)
        self.running = True
        self._go.set()

    def go_now(self, lane="direct"):
        """No scheduler in this process (a driver run): run now, unpinned — and keep the queue
        row honest, so the window's own done()/fail() still lands somewhere real."""
        self._q.mark_running(self.id, lane)
        self.running = True
        self._go.set()
        return True

    def done(self, note=None):
        """The take is FINISHED — the card goes back to the queue."""
        GATES.pop(self.id, None)
        if note is not None:
            e = self._q.get(self.id) or {}
            if e.get("state") == jq.STATE_RUNNING:
                self._q.note(self.id, note)
        return self._q.finish(self.id, ok=True)

    def fail(self, error=None):
        GATES.pop(self.id, None)
        return self._q.finish(self.id, ok=False, error=error or "failed")


def admit(title, need="video", one_of=(0, 1), cards=None, est_s=0.0, q=None):
    """Put a job on the queue whose work the CALLER's own thread will do. Returns a Gate."""
    q = q or queue()
    e = jq.new_entry(title, "gate", cards or [], est_s=est_s,
                     payload={"need": need, "gate": True},
                     one_of=None if need in ("local", "none", None) else one_of)
    g = Gate(e["id"], q)
    GATES[e["id"]] = g
    q.add(e)
    log("queued %s (%s, gate %s)" % (title, need, e["id"]))
    kick()
    return g


def need_for(mode, p=None):
    """What a forge_lab MODE needs to start: {"need", "one_of", "cards"}.

    One place, so every window asks the same question: a 2-card Turbo needs BOTH cards and the
    main lane; a song is its own size; every video engine is "video"; what is left is an image.
    """
    p = p or {}
    if int(p.get("gpus", 1) or 1) >= 2:
        return {"need": "turbo", "one_of": None, "cards": [0, 1]}
    try:
        import forge_lab as _fl
        m = _fl.MODES.get(mode) or {}
    except Exception:
        m = {}
    if m.get("song"):
        return {"need": "song", "one_of": [0, 1], "cards": None}
    if m.get("video") or m.get("wan"):
        return {"need": "video", "one_of": [0, 1], "cards": None}
    return {"need": "image", "one_of": [0, 1], "cards": None}


def admit_render(mode, p, title=None, est_s=0.0, q=None, extra_payload=None):
    """admit() for anything that goes through forge_lab.submit(): the window keeps submit(); the
    queue just decides WHEN. Returns a Gate (read gate.port and put it in your params)."""
    n = need_for(mode, p)
    g = admit(title or ("%s render" % mode), need=n["need"], one_of=n["one_of"],
              cards=n["cards"], est_s=est_s, q=q)
    return g


# ------------------------------------------------------------------ the process-wide one
_DEFAULT = Dispatcher()


def start(tick_s=TICK_S):
    """Start the process-wide scheduler. launch() calls this; a driver run does not."""
    _DEFAULT.tick_s = float(tick_s)
    ok = _DEFAULT.start()
    if ok:
        log("dispatcher started (tick %.1fs)" % _DEFAULT.tick_s)
    return ok


def stop():
    return _DEFAULT.stop()


def running():
    return _DEFAULT.running()


def kick():
    _DEFAULT.kick()


def tick(now=None):
    return _DEFAULT.tick(now)


def run_directly(entry, port=None):
    """Run ONE entry right now, on its own worker, with no scheduler involved.

    This is the fallback for when the loop is not up (a driver run, or a dispatcher that
    failed to start): the click still does what it says. Nothing is claimed and no lane is
    pinned, so the job routes exactly the way every other job in the app routes."""
    jid = entry["id"]
    q = _DEFAULT._q()
    if q.mark_running(jid, "direct") is None:
        return False
    LIVE[jid] = {"msg": "", "t0": time.time(), "lane": "direct", "card": None, "port": port}
    RESULTS.pop(jid, None)
    ERRORS.pop(jid, None)
    log("start %s direct (no scheduler)" % (entry.get("title"),))
    _DEFAULT._spawn(lambda: _DEFAULT._work(jid, entry, port))
    return True


# ------------------------------------------------------------------ what the UI reads
def progress(jid):
    """What a running job is doing right now, or None."""
    return (LIVE.get(jid) or {}).get("msg")


def started_at(jid):
    return (LIVE.get(jid) or {}).get("t0")


def result(jid):
    return RESULTS.get(jid)


def error(jid):
    return ERRORS.get(jid)


def state(jid):
    """queued / paused / running / done / failed / None — read off the queue, not from here."""
    e = queue().get(jid) or {}
    return e.get("state")


def summary():
    """'2 running · 1 waiting' — the same words the Home panel uses."""
    st = queue().stats()
    parts = []
    for key, word in (("running", "running"), ("queued", "waiting"), ("paused", "paused")):
        if st.get(key):
            parts.append("%d %s" % (st[key], word))
    return " · ".join(parts) or "nothing waiting"


def waiting_count(jid=None):
    return queue().queued_ahead(jid) if jid else len(queue().live())


LANE_NAME.update({int(cards.CFG.get("main_port") or 8188): "comfy_a",
                  int(cards.SECOND_PORT): "comfy_b"})
